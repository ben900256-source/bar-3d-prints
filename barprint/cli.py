from __future__ import annotations

from importlib import resources
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .bar_assets import (
    FACTION_FILTERS,
    FACTION_ORDER,
    KIND_FILTERS,
    UNIT_TYPE_FILTERS,
    UNIT_TYPE_ORDER,
    BarAssetError,
    UnitAsset,
    filter_units_by_kind,
    filter_units_by_type,
    filter_units_by_faction,
    format_factory_label,
    group_units_by_factory,
    find_bar_root,
    group_units_by_faction,
    group_units_by_kind,
    group_units_by_type,
    list_units,
    resolve_unit_to_s3o,
)
from .blender_runner import BlenderRunnerError, find_blender, run_blender_export
from .compare_viewer import CompareViewerRow, debug_stage_paths, write_compare_viewer_rows_html
from .config import (
    BarPrintConfigError,
    cache_dir,
    config_path_for_write,
    data_dir_for_scope,
    discovered_config_paths,
    load_config,
    portable_home,
    portable_config_path,
    save_config_values,
    user_cache_dir,
    user_config_path,
    user_data_dir,
)
from .icons import IconRenderError, render_unit_icon
from .pose_profiles import (
    PoseProfileError,
    add_random_poses,
    apply_variant,
    apply_overrides,
    load_profile,
    load_profile_for_source,
    pose_names,
    variant_names,
    write_temp_profile,
)
from .s3o_bounds import S3OBoundsError, find_tallest_unit_model, find_unit_model_bounds, read_s3o_bounds


FACTION_CHOICES = tuple(sorted(FACTION_FILTERS))
KIND_CHOICES = tuple(sorted(KIND_FILTERS))
UNIT_TYPE_CHOICES = tuple(sorted(UNIT_TYPE_FILTERS))
GROUP_BY_CHOICES = ("none", "faction", "kind", "type", "factory")
DEFAULT_SCALE_REFERENCE_UNIT = "armcom"
DEFAULT_SCALE_REFERENCE_HEIGHT_MM = 45.0
DEFAULT_S3O_IMPORTER_URL = (
    "https://raw.githubusercontent.com/FluidPlay/s3o-Blender-plugins-2022/main/s3o_import.py"
)
DEFAULT_S3O_IMPORTER_RELATIVE_PATH = Path("vendor") / "s3o-Blender-plugins-2022" / "s3o_import.py"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.config_values = (
            load_config(getattr(args, "config", None), include_user=True)
            if hasattr(args, "config")
            else {}
        )
        return args.func(args)
    except (
        BarAssetError,
        BarPrintConfigError,
        BlenderRunnerError,
        PoseProfileError,
        S3OBoundsError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="barprint")
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure_parser = subparsers.add_parser(
        "configure",
        help="Discover local dependencies and write configuration.",
    )
    add_config_arg(configure_parser)
    configure_scope = configure_parser.add_mutually_exclusive_group()
    configure_scope.add_argument(
        "--local",
        dest="config_scope",
        action="store_const",
        const="local",
        help="Write ./barprint.local.json. This is the default.",
    )
    configure_scope.add_argument(
        "--user",
        dest="config_scope",
        action="store_const",
        const="user",
        help="Write the per-user config file for installed use.",
    )
    configure_scope.add_argument(
        "--portable",
        nargs="?",
        const=".",
        metavar="DIR",
        help="Write DIR/barprint.portable.json and install bundled tools under DIR.",
    )
    configure_parser.set_defaults(config_scope="local")
    configure_parser.add_argument("--bar-root")
    configure_parser.add_argument("--blender")
    configure_parser.add_argument("--s3o-importer")
    configure_parser.add_argument(
        "--no-install-importer",
        action="store_true",
        help="Do not download the default FluidPlay S3O importer if none is found.",
    )
    configure_parser.set_defaults(func=cmd_configure)

    doctor_parser = subparsers.add_parser("doctor", help="Check configuration, dependencies, and writable paths.")
    add_config_arg(doctor_parser)
    doctor_parser.add_argument("--bar-root")
    doctor_parser.add_argument("--blender")
    doctor_parser.add_argument("--s3o-importer")
    doctor_parser.add_argument("--json", action="store_true", help="Write machine-readable status JSON.")
    doctor_parser.set_defaults(func=cmd_doctor)

    view_parser = subparsers.add_parser("view", help="Serve and open a generated debug viewer over local HTTP.")
    view_parser.add_argument("path", help="Viewer HTML, debug directory, output directory, or exported STL.")
    view_parser.add_argument("--host", default="127.0.0.1")
    view_parser.add_argument("--port", type=int, default=0, help="HTTP port. Defaults to an available port.")
    view_parser.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser.")
    view_parser.set_defaults(func=cmd_view)

    list_parser = subparsers.add_parser("list-units", help="List BAR units and resolved S3O paths.")
    add_config_arg(list_parser)
    list_parser.add_argument("--bar-root", required=False)
    list_parser.add_argument(
        "--by-faction",
        action="store_true",
        help="Group unit output by faction. Alias for --group-by faction.",
    )
    list_parser.add_argument("--faction", choices=FACTION_CHOICES, help="Only show units from one faction.")
    list_parser.add_argument("--kind", choices=KIND_CHOICES, default="all", help="Only show one unit kind.")
    list_parser.add_argument("--type", choices=UNIT_TYPE_CHOICES, default="all", help="Only show one unit type.")
    list_parser.add_argument("--group-by", choices=GROUP_BY_CHOICES, default="none")
    list_parser.add_argument("--with-icons", action="store_true", help="Render terminal color thumbnails.")
    list_parser.add_argument("--icon-size", type=int, default=16, help="Terminal icon thumbnail size in pixels.")
    list_parser.set_defaults(func=cmd_list_units)

    info_parser = subparsers.add_parser("info", help="Show metadata for one BAR unit.")
    add_config_arg(info_parser)
    info_parser.add_argument("--bar-root")
    info_parser.add_argument("--unit", required=True)
    info_parser.add_argument("--with-icon", action="store_true")
    info_parser.add_argument("--icon-size", type=int, default=16)
    info_parser.set_defaults(func=cmd_info)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect a unit or S3O path.")
    add_config_arg(inspect_parser)
    add_source_args(inspect_parser)
    inspect_parser.add_argument("--with-pieces", action="store_true", help="Run Blender and output imported object names.")
    inspect_parser.add_argument("--blender")
    inspect_parser.add_argument("--s3o-importer")
    inspect_parser.set_defaults(func=cmd_inspect)

    export_parser = subparsers.add_parser("export", help="Export printable STL/3MF from a BAR S3O model.")
    add_config_arg(export_parser)
    add_source_args(export_parser)
    export_parser.add_argument("--pose-profile")
    export_parser.add_argument("--pose", default="neutral")
    export_parser.add_argument(
        "--variant",
        default="standard",
        help="Print variant to export from the pose profile. Use 'all' to export every variant.",
    )
    export_parser.add_argument("--random-poses", type=int, default=0)
    export_parser.add_argument("--scale-mm", type=float)
    export_parser.add_argument(
        "--scale-mode",
        choices=["profile", "absolute", "game-relative"],
        help="Use profile scale_mm, explicit absolute scale, or scale relative to a BAR reference unit.",
    )
    export_parser.add_argument(
        "--scale-reference-unit",
        help="For --scale-mode game-relative, scale against this unit. Defaults to config, profile, or armcom.",
    )
    export_parser.add_argument(
        "--scale-reference-height-mm",
        type=float,
        help="For --scale-mode game-relative, set the reference unit to this height. Defaults to config, profile, or 45.",
    )
    export_parser.add_argument(
        "--max-unit-height-mm",
        type=float,
        help="Deprecated alias for --scale-reference-height-mm.",
    )
    base_group = export_parser.add_mutually_exclusive_group()
    base_group.add_argument("--base", dest="base_enabled", action="store_true")
    base_group.add_argument("--no-base", dest="base_enabled", action="store_false")
    export_parser.set_defaults(base_enabled=None)
    export_parser.add_argument("--base-diameter-mm", type=float)
    export_parser.add_argument("--base-height-mm", type=float)
    thin_feature_group = export_parser.add_mutually_exclusive_group()
    thin_feature_group.add_argument(
        "--thin-features",
        dest="thin_features_enabled",
        action="store_true",
        help="Thicken features that are below the target printable thickness.",
    )
    thin_feature_group.add_argument(
        "--no-thin-features",
        dest="thin_features_enabled",
        action="store_false",
        help="Disable thin feature thickening.",
    )
    export_parser.set_defaults(thin_features_enabled=None)
    export_parser.add_argument(
        "--min-feature-mm",
        type=float,
        help="Target minimum feature thickness before export. Defaults to profile or 0.8mm.",
    )
    export_parser.add_argument(
        "--thin-feature-max-inflate-mm",
        type=float,
        help="Maximum outward expansion applied to any thin feature vertex.",
    )
    export_parser.add_argument("--format", choices=["stl", "3mf"], default="stl")
    export_parser.add_argument("--out")
    export_parser.add_argument("--open", action="store_true", help="Open each exported file with the OS default app.")
    export_parser.add_argument("--blender")
    export_parser.add_argument(
        "--s3o-importer",
        help="Path to a compatible S3O importer. Defaults to config or known local vendor paths.",
    )
    export_parser.add_argument("--keep-raw", action="store_true")
    export_parser.add_argument(
        "--debug-stages",
        action="store_true",
        help="Write opt-in diagnostic stage renders, snapshots, reports, and a debug viewer beside the export.",
    )
    export_parser.add_argument("--verbose", action="store_true")
    export_parser.add_argument("--no-selector-icons", action="store_true", help="Disable icons in interactive unit selection.")
    export_parser.set_defaults(func=cmd_export)

    return parser


def add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        help=(
            "Path to a JSON config file. Defaults to BARPRINT_CONFIG, ./barprint.local.json, "
            "portable config, or per-user config."
        ),
    )


def add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bar-root")
    parser.add_argument("--unit")
    parser.add_argument("--s3o")


def cmd_list_units(args: argparse.Namespace) -> int:
    bar_root = _find_bar_root(args)
    all_units = list_units(bar_root)
    units = all_units
    if args.faction:
        units = filter_units_by_faction(units, args.faction)
    units = filter_units_by_kind(units, args.kind)
    units = filter_units_by_type(units, args.type)

    group_by = "faction" if args.by_faction else args.group_by
    if args.faction and group_by == "none":
        group_by = "faction"

    if group_by == "faction":
        _print_units_by_faction(units, with_icons=args.with_icons, icon_size=args.icon_size)
    elif group_by == "kind":
        _print_units_by_kind(units, with_icons=args.with_icons, icon_size=args.icon_size)
    elif group_by == "type":
        _print_units_by_type(units, with_icons=args.with_icons, icon_size=args.icon_size)
    elif group_by == "factory":
        _print_units_by_factory(units, all_units, with_icons=args.with_icons, icon_size=args.icon_size)
    else:
        _print_unit_table(units, with_icons=args.with_icons, icon_size=args.icon_size)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    bar_root = _find_bar_root(args)
    unit, all_units = _find_unit_asset(bar_root, args.unit)
    console = _console(with_color=args.with_icon)
    console.print(_unit_info_panel(unit, all_units, with_icon=args.with_icon, icon_size=args.icon_size))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    s3o_path = resolve_source(args, allow_interactive=False)
    if not args.with_pieces:
        print(json.dumps({"s3o_path": str(s3o_path)}, indent=2))
        return 0

    blender_exe = _find_blender(args)
    importer = _find_s3o_importer(args, allow_install=True)
    script_path = _blender_script_path()
    out_json = Path.cwd() / "barprint_inspect_pieces.json"
    profile_path = write_temp_profile(load_profile(None))
    run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer,
        profile_path,
        "neutral",
        out_json,
        "stl",
        {"inspect_pieces": True},
    )
    print(out_json.read_text(encoding="utf-8"))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    s3o_path = resolve_source(args, allow_interactive=True)
    unit_for_profile = _unit_for_profile(args)
    profile = load_profile_for_source(args.pose_profile, unit=unit_for_profile, s3o_path=s3o_path)
    if args.verbose and not args.pose_profile:
        print(f"Auto pose profile: {profile.get('pose_archetype', profile.get('name'))}")
    profile = apply_overrides(
        profile,
        scale_mm=args.scale_mm,
        base_enabled=args.base_enabled,
        base_diameter_mm=args.base_diameter_mm,
        base_height_mm=args.base_height_mm,
        thin_features_enabled=args.thin_features_enabled,
        min_feature_mm=args.min_feature_mm,
        thin_feature_max_inflate_mm=args.thin_feature_max_inflate_mm,
    )
    profile = apply_scale_mode(profile, args, s3o_path)
    if args.random_poses:
        profile = add_random_poses(profile, args.random_poses)
    selected_poses = pose_names(profile, args.pose)
    selected_variants = variant_names(profile, args.variant)

    blender_exe = _find_blender(args)
    importer = _find_s3o_importer(args, allow_install=True)
    script_path = _blender_script_path()
    out_base = _configured_output_base(args, s3o_path)

    total_exports = len(selected_poses) * len(selected_variants)
    export_index = 0
    debug_viewer_rows: list[CompareViewerRow] = []
    for variant_name in selected_variants:
        variant_profile = apply_variant(profile, variant_name)
        profile_path = write_temp_profile(variant_profile)
        for pose_name in selected_poses:
            export_index += 1
            label = _export_label(pose_name, variant_name)
            out_path = _output_path(
                out_base,
                pose_name,
                args.format,
                len(selected_poses) > 1,
                variant_name=variant_name,
                variant_batch=len(selected_variants) > 1,
            )

            print(f"Export [{export_index}/{total_exports}] {label} -> {out_path}", flush=True)
            export_pose_output(
                args,
                blender_exe,
                script_path,
                s3o_path,
                importer,
                profile_path,
                pose_name,
                out_path,
                progress_callback=_export_progress_printer(export_index, total_exports, label),
            )
            print(f"Export [{export_index}/{total_exports}] complete -> {out_path}", flush=True)

            if args.debug_stages and args.format == "stl":
                debug_paths = debug_stage_paths(out_path)
                opaque_glb = (
                    debug_paths.opaque_print_source_glb if debug_paths.opaque_print_source_glb.is_file() else None
                )
                debug_viewer_rows.append(
                    CompareViewerRow(
                        label=label,
                        default_stl=out_path,
                        game_glb=debug_paths.game_glb,
                        post_thickening_stl=debug_paths.post_thickening_stl,
                        opaque_print_source_glb=opaque_glb,
                    )
                )

            if args.open:
                open_with_default_app(out_path)
    if len(debug_viewer_rows) > 1:
        viewer_path = _multi_debug_viewer_path(out_base)
        write_compare_viewer_rows_html(viewer_path, rows=debug_viewer_rows)
        print(f"Debug viewer [{len(debug_viewer_rows)} rows] -> {viewer_path}", flush=True)
        if args.open:
            open_with_default_app(viewer_path)
    return 0


def export_pose_output(
    args: argparse.Namespace,
    blender_exe: str,
    script_path: Path,
    s3o_path: Path,
    importer: Path,
    profile_path: Path,
    pose_name: str,
    out_path: Path,
    *,
    progress_callback,
) -> None:
    extra_args = {"keep_raw": args.keep_raw}
    if args.debug_stages:
        extra_args["debug_stages"] = True
    run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer,
        profile_path,
        pose_name,
        out_path,
        args.format,
        extra_args,
        progress_callback=progress_callback,
    )


def cmd_configure(args: argparse.Namespace) -> int:
    if getattr(args, "portable", None):
        args.config_scope = "portable"
        args.portable_home = args.portable
    if args.config_scope in {"portable", "user"} and not args.s3o_importer:
        args.config_values = {key: value for key, value in args.config_values.items() if key != "s3o_importer"}
    root = _find_bar_root(args, allow_prompt=True, prompt_save=False)
    blender = Path(_find_blender(args, prompt_save=False))
    importer = _find_s3o_importer(
        args,
        allow_install=not args.no_install_importer,
        auto_install=not args.no_install_importer,
        prompt_save=False,
    )

    values = {
        "bar_root": root.as_posix(),
        "blender": blender.as_posix(),
        "s3o_importer": importer.as_posix(),
    }
    saved_path = save_config_values(
        values,
        getattr(args, "config", None),
        scope=args.config_scope,
        portable_home_path=getattr(args, "portable_home", None),
    )
    args.config_values.update(values)
    print(f"Saved {args.config_scope} configuration to {saved_path}")
    print(f"BAR data: {root}")
    print(f"Blender: {blender}")
    print(f"S3O importer: {importer}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, str | bool]] = []

    config_detail = _doctor_config_detail(args)
    checks.append({"name": "Config", "ok": True, "detail": config_detail})

    root = _doctor_probe("BAR data", lambda: str(_find_bar_root(args, allow_prompt=False)))
    blender = _doctor_probe("Blender", lambda: _find_blender(args, prompt_save=False))
    importer = _doctor_probe(
        "S3O importer",
        lambda: str(_find_s3o_importer(args, allow_install=False, prompt_save=False)),
    )
    checks.extend([root, blender, importer])

    checks.append(_doctor_probe("Cache", _ensure_cache_dir))
    checks.append({"name": "Viewer", "ok": True, "detail": "Embedded no-CDN debug viewer; use `barprint view PATH`."})

    if args.json:
        print(json.dumps({"ok": all(bool(check["ok"]) for check in checks), "checks": checks}, indent=2))
    else:
        _print_doctor_table(checks)
    return 0 if all(bool(check["ok"]) for check in checks) else 2


def cmd_view(args: argparse.Namespace) -> int:
    viewer_html = _resolve_viewer_html(Path(args.path).expanduser())
    serve_viewer(
        viewer_html,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )
    return 0


def resolve_source(args: argparse.Namespace, *, allow_interactive: bool) -> Path:
    if args.s3o and (args.bar_root or args.unit):
        raise ValueError("Use either --s3o or --bar-root + --unit, not both.")
    if args.s3o:
        path = Path(args.s3o).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"S3O file not found: {path}")
        return path
    if not args.unit:
        if allow_interactive and _stdin_is_interactive():
            args.unit = _select_unit_interactively(args)
        else:
            raise ValueError("Either --s3o or --unit is required.")
    bar_root = _find_bar_root(args)
    return resolve_unit_to_s3o(bar_root, args.unit)


def _unit_for_profile(args: argparse.Namespace) -> UnitAsset | None:
    if args.s3o or not args.unit:
        return None
    bar_root = _find_bar_root(args)
    unit, _ = _find_unit_asset(bar_root, args.unit)
    return unit


def apply_scale_mode(profile: dict, args: argparse.Namespace, s3o_path: Path) -> dict:
    if args.scale_mm is not None:
        profile.setdefault("scale", {})["mode"] = "absolute"
        return profile

    scale_config = dict(profile.get("scale") or {})
    mode = args.scale_mode or _configured_value(args, "scale_mode") or scale_config.get("mode") or "profile"
    if (
        args.max_unit_height_mm is not None
        or getattr(args, "scale_reference_height_mm", None) is not None
        or getattr(args, "scale_reference_unit", None)
    ) and args.scale_mode is None:
        mode = "game-relative"
    mode = mode.replace("_", "-")

    if mode in {"profile", "absolute"}:
        return profile
    if mode != "game-relative":
        raise ValueError(f"Unsupported scale mode: {mode}")

    reference_unit = (
        getattr(args, "scale_reference_unit", None)
        or _configured_value(args, "scale_reference_unit")
        or scale_config.get("reference_unit")
        or DEFAULT_SCALE_REFERENCE_UNIT
    )
    if not isinstance(reference_unit, str) or not reference_unit.strip():
        raise ValueError("--scale-reference-unit must be a non-empty string")
    reference_unit = reference_unit.strip()

    reference_height_mm = _first_number(
        getattr(args, "scale_reference_height_mm", None),
        args.max_unit_height_mm,
        _configured_value(args, "scale_reference_height_mm"),
        scale_config.get("reference_height_mm"),
        scale_config.get("max_unit_height_mm"),
        DEFAULT_SCALE_REFERENCE_HEIGHT_MM,
    )
    if reference_height_mm <= 0:
        raise ValueError("--scale-reference-height-mm must be greater than zero")

    bar_root_config = _configured_value(args, "bar_root")
    bar_root = _find_bar_root(args, allow_prompt=False) if bar_root_config else infer_bar_root_from_path(s3o_path)
    source_bounds = read_s3o_bounds(s3o_path)
    reference = (
        find_tallest_unit_model(bar_root)
        if reference_unit.casefold() == "tallest"
        else find_unit_model_bounds(bar_root, reference_unit)
    )

    target_height_mm = source_bounds.height * reference_height_mm / reference.bounds.height
    profile["scale_mm"] = target_height_mm
    profile["scale"] = {
        **scale_config,
        "mode": "game-relative",
        "reference_unit": reference.unit.unit_code,
        "reference_height_mm": reference_height_mm,
        "source_height": source_bounds.height,
        "reference_s3o": str(reference.unit.s3o_path),
        "reference_source_height": reference.bounds.height,
    }
    if args.verbose:
        print(
            "Game-relative scale: "
            f"{reference.unit.unit_code}={reference_height_mm:g}mm, "
            f"{s3o_path.stem}={target_height_mm:.2f}mm"
        )
    return profile


def infer_bar_root_from_path(path: Path) -> Path:
    for candidate in [path.parent, *path.parents]:
        if (candidate / "units").is_dir() and (candidate / "objects3d").is_dir():
            return candidate
    raise ValueError(
        "Game-relative scaling requires --bar-root/--unit, or an explicit --s3o inside a BAR.sdd tree."
    )


def _find_bar_root(
    args: argparse.Namespace,
    *,
    allow_prompt: bool = True,
    prompt_save: bool = True,
) -> Path:
    cached = getattr(args, "resolved_bar_root", None)
    if cached:
        return Path(cached)

    configured = _configured_value(args, "bar_root")
    try:
        root = find_bar_root(configured)
    except BarAssetError:
        if configured or not allow_prompt or not _stdin_is_interactive():
            raise
        root = _prompt_for_bar_root()

    args.resolved_bar_root = str(root)
    if allow_prompt and prompt_save and not configured:
        _prompt_save_path(args, "bar_root", root, "BAR data")
    return root


def _find_blender(args: argparse.Namespace, *, prompt_save: bool = True) -> str:
    configured = _configured_value(args, "blender")
    blender = find_blender(configured)
    if prompt_save and not configured:
        _prompt_save_path(args, "blender", Path(blender), "Blender")
    return blender


def _prompt_for_bar_root() -> Path:
    while True:
        try:
            raw = input("BAR.sdd was not found automatically. Enter BAR.sdd path, or blank to cancel: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise ValueError("BAR.sdd selection cancelled.") from exc
        if not raw:
            raise ValueError("Could not find BAR.sdd. Pass --bar-root explicitly.")
        try:
            return find_bar_root(raw.strip('"'))
        except BarAssetError as exc:
            print(f"{exc}")


def _prompt_save_path(args: argparse.Namespace, key: str, path: Path, label: str) -> None:
    if not _stdin_is_interactive():
        return
    prompted = getattr(args, "config_save_prompts", set())
    if key in prompted:
        return
    prompted.add(key)
    args.config_save_prompts = prompted

    config_path = config_path_for_write(getattr(args, "config", None))
    try:
        answer = input(f"Found {label} at {path}. Save this to {config_path}? [Y/n] ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer not in {"", "y", "yes"}:
        return
    saved_value = path.as_posix()
    saved_path = save_config_values({key: saved_value}, getattr(args, "config", None))
    args.config_values[key] = saved_value
    print(f"Saved {label} path to {saved_path}")


def _print_unit_table(units: list[UnitAsset], *, with_icons: bool = False, icon_size: int = 16) -> None:
    console = _console(with_color=with_icons)
    if not units:
        console.print("No units found.")
        return
    console.print(
        _unit_table(
            units,
            include_faction=True,
            with_icons=with_icons,
            icon_size=icon_size,
        )
    )


def _unit_table(
    units: list[UnitAsset],
    *,
    title: str | None = None,
    include_faction: bool,
    with_icons: bool = False,
    icon_size: int = 16,
    include_index: bool = False,
) -> Table:
    table = Table(
        title=title,
        box=box.ASCII,
        expand=True,
        show_lines=False,
    )
    if include_index:
        table.add_column("#", justify="right", no_wrap=True)
    if with_icons:
        table.add_column("Icon", no_wrap=True)
    table.add_column("Code", style="bold", no_wrap=True, max_width=24)
    table.add_column("Name", overflow="fold", ratio=2, max_width=22)
    table.add_column("Description", overflow="fold", ratio=3, max_width=32)
    if include_faction:
        table.add_column("Faction", no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    table.add_column("Type", overflow="fold", ratio=2, max_width=32)
    table.add_column("Source", overflow="fold", ratio=3, min_width=32)
    for index, unit in enumerate(units, start=1):
        row: list[str | Text] = []
        if include_index:
            row.append(str(index))
        if with_icons:
            row.append(_unit_icon_cell(unit, icon_size))
        row.extend([unit.unit_code, unit.display_name, unit.description])
        if include_faction:
            row.append(unit.faction)
        row.extend([unit.kind, _unit_type_display(unit), _unit_s3o_display(unit)])
        table.add_row(*row)
    return table


def _print_units_by_faction(units: list[UnitAsset], *, with_icons: bool = False, icon_size: int = 16) -> None:
    groups = group_units_by_faction(units)
    console = _console(with_color=with_icons)
    printed = False
    for faction in FACTION_ORDER:
        grouped_units = groups.get(faction, [])
        if not grouped_units:
            continue
        if printed:
            console.print()
        console.print(
            _unit_table(
                grouped_units,
                title=f"{faction} ({len(grouped_units)})",
                include_faction=False,
                with_icons=with_icons,
                icon_size=icon_size,
            )
        )
        printed = True
    if not printed:
        console.print("No units found.")


def _print_units_by_kind(units: list[UnitAsset], *, with_icons: bool = False, icon_size: int = 16) -> None:
    groups = group_units_by_kind(units)
    console = _console(with_color=with_icons)
    printed = False
    for kind in ("unit", "building", "other"):
        grouped_units = groups.get(kind, [])
        if not grouped_units:
            continue
        if printed:
            console.print()
        console.print(
            _unit_table(
                grouped_units,
                title=f"{kind.title()} ({len(grouped_units)})",
                include_faction=True,
                with_icons=with_icons,
                icon_size=icon_size,
            )
        )
        printed = True
    if not printed:
        console.print("No units found.")


def _print_units_by_type(units: list[UnitAsset], *, with_icons: bool = False, icon_size: int = 16) -> None:
    groups = group_units_by_type(units)
    console = _console(with_color=with_icons)
    printed = False
    for unit_type in (*UNIT_TYPE_ORDER, "unclassified"):
        grouped_units = groups.get(unit_type, [])
        if not grouped_units:
            continue
        if printed:
            console.print()
        console.print(
            _unit_table(
                grouped_units,
                title=f"{_unit_type_title(unit_type)} ({len(grouped_units)})",
                include_faction=True,
                with_icons=with_icons,
                icon_size=icon_size,
            )
        )
        printed = True
    if not printed:
        console.print("No units found.")


def _print_units_by_factory(
    units: list[UnitAsset],
    all_units: list[UnitAsset],
    *,
    with_icons: bool = False,
    icon_size: int = 16,
) -> None:
    groups = group_units_by_factory(units, all_units)
    console = _console(with_color=with_icons)
    printed = False
    for title, grouped_units in groups.items():
        if printed:
            console.print()
        console.print(
            _unit_table(
                grouped_units,
                title=f"{title} ({len(grouped_units)})",
                include_faction=True,
                with_icons=with_icons,
                icon_size=icon_size,
            )
        )
        printed = True
    if not printed:
        console.print("No units found.")


def _select_unit_interactively(args: argparse.Namespace) -> str:
    bar_root = _find_bar_root(args)
    units = list_units(bar_root)
    groups = group_units_by_faction(units)
    factions = [faction for faction in FACTION_ORDER if groups.get(faction)]
    if not factions:
        raise ValueError(f"No exportable units found under {bar_root}")

    print("Factions:")
    for index, faction in enumerate(factions, start=1):
        print(f"  {index}. {faction} ({len(groups[faction])})")
    faction_index = _prompt_for_index("Select faction: ", len(factions))
    faction = factions[faction_index]

    faction_units = groups[faction]
    print(f"\n{faction} units:")
    console = _console(with_color=not args.no_selector_icons)
    console.print(
        _unit_table(
            faction_units,
            include_faction=False,
            with_icons=not args.no_selector_icons,
            icon_size=16,
            include_index=True,
        )
    )
    unit_index = _prompt_for_index("Select unit: ", len(faction_units))
    selected = faction_units[unit_index]
    print(f"Selected unit: {selected.unit_code}")
    return selected.unit_code


def _prompt_for_index(prompt: str, count: int) -> int:
    while True:
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise ValueError("Interactive selection cancelled.") from exc
        try:
            value = int(raw)
        except ValueError:
            print(f"Enter a number from 1 to {count}.")
            continue
        if 1 <= value <= count:
            return value - 1
        print(f"Enter a number from 1 to {count}.")


def _stdin_is_interactive() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


def _configured_output_base(args: argparse.Namespace, s3o_path: Path) -> Path:
    if args.out:
        return Path(args.out).expanduser()
    return Path("out") / s3o_path.stem.lower()


def _output_path(
    out_base: Path,
    pose_name: str,
    fmt: str,
    pose_batch: bool,
    *,
    variant_name: str = "standard",
    variant_batch: bool = False,
) -> Path:
    suffix = f".{fmt}"
    if out_base.suffix.casefold() == suffix:
        return out_base
    name_parts = []
    if pose_batch:
        name_parts.append(pose_name)
    if variant_batch or variant_name != "standard":
        name_parts.append(variant_name)
    if name_parts:
        return out_base / f"{out_base.name}_{'_'.join(name_parts)}{suffix}"
    return out_base / f"{out_base.name}{suffix}"


def _multi_debug_viewer_path(out_base: Path) -> Path:
    if out_base.suffix:
        return out_base.with_name(f"{out_base.stem}_debug_viewer.html")
    return out_base / f"{out_base.name}_debug_viewer.html"


def _export_label(pose_name: str, variant_name: str) -> str:
    return pose_name if variant_name == "standard" else f"{pose_name}/{variant_name}"


def _export_progress_printer(index: int, total: int, label: str):
    last_message: str | None = None

    def print_progress(message: str) -> None:
        nonlocal last_message
        if message == last_message:
            return
        last_message = message
        print(f"Export [{index}/{total}] {label}: {message}", flush=True)

    return print_progress


def open_with_default_app(path: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"Cannot open missing export file: {resolved}")
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(resolved))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(resolved)])
        else:
            subprocess.Popen(["xdg-open", str(resolved)])
    except OSError as exc:
        raise ValueError(f"Could not open exported file: {resolved}") from exc
    print(f"Opened {resolved}", flush=True)


def open_url_with_default_app(url: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
    except OSError as exc:
        raise ValueError(f"Could not open URL: {url}") from exc
    print(f"Opened {url}", flush=True)


def serve_viewer(
    viewer_html: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    viewer_html = viewer_html.expanduser().resolve()
    if not viewer_html.is_file():
        raise ValueError(f"Viewer HTML not found: {viewer_html}")

    serve_root = _viewer_serve_root(viewer_html)
    relative_viewer = viewer_html.relative_to(serve_root).as_posix()
    handler = partial(SimpleHTTPRequestHandler, directory=str(serve_root))
    httpd = ThreadingHTTPServer((host, port), handler)
    bound_host, bound_port = httpd.server_address
    url_host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else bound_host
    url_path = urllib.parse.quote(relative_viewer)
    url = f"http://{url_host}:{bound_port}/{url_path}"
    print(f"Serving {serve_root}")
    print(f"Viewer URL: {url}")
    if open_browser:
        open_url_with_default_app(url)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()


def _viewer_serve_root(viewer_html: Path) -> Path:
    parent = viewer_html.parent.resolve()
    if parent.name.casefold().endswith("_debug"):
        return parent.parent
    return parent


def _resolve_viewer_html(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_file():
        if resolved.suffix.casefold() == ".html":
            return resolved
        if resolved.suffix.casefold() == ".stl":
            candidates = [
                debug_stage_paths(resolved).viewer_html,
                resolved.with_name(f"{resolved.stem}_viewer.html"),
            ]
            for candidate in candidates:
                if candidate.is_file():
                    return candidate.resolve()
        raise ValueError(f"Could not infer a debug viewer from file: {resolved}")
    if resolved.is_dir():
        direct = sorted(
            [
                *resolved.glob("*_debug_viewer.html"),
                *resolved.glob("*_viewer.html"),
            ]
        )
        if len(direct) == 1:
            return direct[0].resolve()
        if len(direct) > 1:
            sample = "\n".join(f"  - {candidate}" for candidate in direct[:8])
            raise ValueError(f"Multiple viewer HTML files found. Pass one explicitly:\n{sample}")
        recursive = sorted(resolved.rglob("*_debug_viewer.html"))
        if len(recursive) == 1:
            return recursive[0].resolve()
        if len(recursive) > 1:
            sample = "\n".join(f"  - {candidate}" for candidate in recursive[:8])
            raise ValueError(f"Multiple debug viewer HTML files found. Pass one explicitly:\n{sample}")
    raise ValueError(f"Viewer path not found: {resolved}")


def _doctor_config_detail(args: argparse.Namespace) -> str:
    if getattr(args, "config", None):
        return f"explicit {Path(args.config).expanduser()}"
    if args.config_values:
        return "loaded auto-discovered config"
    candidates = ", ".join(str(path) for path in discovered_config_paths())
    return f"no config loaded; checked {candidates}"


def _doctor_probe(name: str, probe) -> dict[str, str | bool]:
    try:
        detail = probe()
    except Exception as exc:
        return {"name": name, "ok": False, "detail": str(exc)}
    return {"name": name, "ok": True, "detail": str(detail)}


def _ensure_cache_dir() -> str:
    path = cache_dir()
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return str(path)


def _print_doctor_table(checks: list[dict[str, str | bool]]) -> None:
    table = Table(box=box.ASCII, expand=True)
    table.add_column("Check", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    for check in checks:
        ok = bool(check["ok"])
        table.add_row(str(check["name"]), "OK" if ok else "Missing", str(check["detail"]))
    console = _console()
    console.print(table)
    console.print(f"User config: {user_config_path()}")
    console.print(f"User data: {user_data_dir()}")
    console.print(f"User cache: {user_cache_dir()}")
    console.print(f"Portable config for cwd: {portable_config_path()}")


def _require_importer(value: str | None) -> Path:
    if not value:
        searched = "\n".join(f"  - {path}" for path in _local_s3o_importer_candidates())
        raise ValueError(
            "S3O importer missing. Pass --s3o-importer PATH, set s3o_importer in config, "
            "place it in a known local vendor path, or run `python -m barprint configure`.\n"
            f"Searched:\n{searched}"
        )
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"S3O importer missing: {path}")
    return path


def _find_s3o_importer(
    args: argparse.Namespace,
    *,
    allow_install: bool,
    auto_install: bool = False,
    prompt_save: bool = True,
) -> Path:
    configured = _configured_value(args, "s3o_importer")
    if configured:
        return _require_importer(configured)

    discovered = _find_local_s3o_importer(args)
    if discovered:
        if prompt_save:
            _prompt_save_path(args, "s3o_importer", discovered, "S3O importer")
        return discovered

    if allow_install and (auto_install or _prompt_install_default_s3o_importer(args)):
        importer = _install_default_s3o_importer(args)
        if prompt_save:
            _prompt_save_path(args, "s3o_importer", importer, "S3O importer")
        return importer

    return _require_importer(None)


def _prompt_install_default_s3o_importer(args: argparse.Namespace | None = None) -> bool:
    if not _stdin_is_interactive():
        return False
    destination = _default_s3o_importer_install_path(args)
    try:
        answer = input(
            "S3O importer was not found. Install FluidPlay s3o-Blender-plugins-2022 "
            f"to {destination}? [Y/n] "
        ).strip().casefold()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"", "y", "yes"}


def _install_default_s3o_importer(args: argparse.Namespace | None = None) -> Path:
    destination = _default_s3o_importer_install_path(args)
    if destination.is_file():
        return destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        DEFAULT_S3O_IMPORTER_URL,
        headers={"User-Agent": "barprint"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise ValueError(
            "Could not download the FluidPlay S3O importer. "
            "Pass --s3o-importer PATH, set s3o_importer in config, or try again with network access."
        ) from exc
    if b"ImportS3O" not in data or b"bl_idname" not in data:
        raise ValueError("Downloaded S3O importer did not look like a compatible Blender importer.")
    destination.write_bytes(data)
    return destination.resolve()


def _default_s3o_importer_install_path(args: argparse.Namespace | None = None) -> Path:
    scope = getattr(args, "config_scope", "local") if args is not None else "local"
    portable_home_path = getattr(args, "portable_home", None) if args is not None else None
    return data_dir_for_scope(scope, portable_home_path=portable_home_path) / DEFAULT_S3O_IMPORTER_RELATIVE_PATH


def _find_local_s3o_importer(args: argparse.Namespace | None = None) -> Path | None:
    for candidate in _local_s3o_importer_candidates(args):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _local_s3o_importer_candidates(args: argparse.Namespace | None = None) -> list[Path]:
    root = Path.cwd()
    local_candidates = [
        root / "vendor" / "s3o-Blender-plugins-2022" / "s3o_import.py",
        root / "vendor" / "s3o_import.py",
        root / "barprint" / "vendor" / "s3o_import.py",
    ]
    user_candidates = [
        user_data_dir() / "vendor" / "s3o-Blender-plugins-2022" / "s3o_import.py",
        user_data_dir() / "vendor" / "s3o_import.py",
    ]
    portable_candidates: list[Path] = []
    configured_portable_home = portable_home()
    if configured_portable_home is not None:
        portable_candidates.extend(
            [
                configured_portable_home / "vendor" / "s3o-Blender-plugins-2022" / "s3o_import.py",
                configured_portable_home / "vendor" / "s3o_import.py",
            ]
        )
    if args is not None:
        portable_home_path = getattr(args, "portable_home", None)
        if portable_home_path:
            portable_root = data_dir_for_scope("portable", portable_home_path=portable_home_path)
            portable_candidates.extend(
                [
                    portable_root / "vendor" / "s3o-Blender-plugins-2022" / "s3o_import.py",
                    portable_root / "vendor" / "s3o_import.py",
                ]
            )
    scope = getattr(args, "config_scope", "local") if args is not None else "local"
    command = getattr(args, "command", None) if args is not None else None
    if command == "configure" and scope == "portable":
        candidates = portable_candidates
    elif command == "configure" and scope == "user":
        candidates = user_candidates
    elif scope == "portable":
        candidates = [*portable_candidates, *local_candidates, *user_candidates]
    elif scope == "user":
        candidates = [*user_candidates, *local_candidates, *portable_candidates]
    else:
        candidates = [*local_candidates, *portable_candidates, *user_candidates]
    return _unique_cli_paths(candidates)


def _unique_cli_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _configured_value(args: argparse.Namespace, key: str):
    value = getattr(args, key, None)
    if value is not None:
        return value
    return getattr(args, "config_values", {}).get(key)


def _first_number(*values) -> float:
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            raise ValueError("Scale height must be a number")
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value)
            except ValueError as exc:
                raise ValueError(f"Scale height must be a number: {value}") from exc
        raise ValueError("Scale height must be a number")
    raise ValueError("Scale height is required")


def _find_unit_asset(bar_root: Path, query: str) -> tuple[UnitAsset, list[UnitAsset]]:
    units = list_units(bar_root, require_existing=False)
    query_lower = query.casefold()
    for unit in units:
        if unit.unit_code.casefold() == query_lower:
            return unit, units
    for unit in units:
        haystack = f"{unit.unit_code} {unit.display_name} {unit.description} {unit.objectname}".casefold()
        if query_lower in haystack:
            return unit, units
    sample = ", ".join(unit.unit_code for unit in units[:12])
    suffix = f" Available examples: {sample}" if sample else ""
    raise BarAssetError(f"Unit not found for query '{query}'.{suffix}")


def _unit_info_panel(unit: UnitAsset, all_units: list[UnitAsset], *, with_icon: bool, icon_size: int) -> Panel:
    factory_by_code = {candidate.unit_code: candidate for candidate in all_units}
    built_by = ", ".join(
        format_factory_label(factory_by_code.get(factory_code), factory_code)
        for factory_code in unit.built_by
    )
    details = Table.grid(padding=(0, 1))
    details.add_column(style="bold", no_wrap=True)
    details.add_column(ratio=1)
    details.add_row("Code", unit.unit_code)
    details.add_row("Name", unit.display_name)
    details.add_row("Description", unit.description)
    details.add_row("Faction", unit.faction)
    details.add_row("Kind", unit.kind)
    details.add_row("Type", _unit_type_display(unit))
    details.add_row("Source", _unit_s3o_display(unit))
    details.add_row("Built by", built_by or "No production factory")

    renderable: Table | Text
    if with_icon:
        layout = Table.grid(padding=(0, 2))
        layout.add_column(no_wrap=True)
        layout.add_column(ratio=1)
        layout.add_row(_unit_icon_cell(unit, icon_size), details)
        renderable = layout
    else:
        renderable = details

    return Panel(
        renderable,
        title=f"{unit.display_name} ({unit.unit_code})",
        box=box.ASCII,
        expand=True,
    )


def _unit_type_display(unit: UnitAsset) -> str:
    return ", ".join(_unit_type_title(unit_type) for unit_type in unit.unit_types)


def _unit_type_title(unit_type: str) -> str:
    if unit_type == "unclassified":
        return "Unclassified"
    return unit_type.title()


def _unit_icon_cell(unit: UnitAsset, icon_size: int) -> Text:
    try:
        return render_unit_icon(unit, icon_size, use_half_blocks=_stdout_supports("\u2580"))
    except IconRenderError:
        return Text("")


def _unit_s3o_display(unit: UnitAsset) -> str:
    if unit.archive_package and unit.archive_s3o_entry:
        return unit.archive_s3o_entry
    return _compact_model_path(unit.s3o_path)


def _compact_model_path(path: Path) -> str:
    parts = list(path.parts)
    for index, part in enumerate(parts):
        if part.casefold() == "objects3d":
            return Path(*parts[index:]).as_posix()
    return str(path)


def _console(*, with_color: bool = False) -> Console:
    width = None if _stdout_is_interactive() else 180
    return Console(file=sys.stdout, highlight=False, color_system="truecolor" if with_color else None, width=width)


def _stdout_supports(value: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        value.encode(encoding)
    except UnicodeEncodeError:
        return False
    return True


def _stdout_is_interactive() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _blender_script_path() -> Path:
    return Path(str(resources.files("barprint") / "blender" / "bar_to_print_blender.py"))


if __name__ == "__main__":
    raise SystemExit(main())
