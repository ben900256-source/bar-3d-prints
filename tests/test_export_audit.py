from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import os
import struct

import pytest

from barprint.bar_assets import BarAssetError, UnitAsset, ensure_unit_s3o, find_bar_root, list_units
from barprint.blender_runner import BlenderRunnerError, find_blender, run_blender_export
from barprint.cli import _blender_script_path
from barprint.compare_viewer import debug_stage_paths
from barprint.config import BarPrintConfigError, load_config
from barprint.pose_profiles import apply_overrides, load_profile_for_source, write_temp_profile


AUDIT_ENABLED = os.environ.get("BARPRINT_EXPORT_AUDIT") == "1"
TOPOLOGY_QUANTIZATION_MM = 1e-5


@dataclass
class AuditPaths:
    output: Path
    manifest: Path
    print_source: Path
    metrics: Path
    debug_output: Path
    debug_stage_report: Path
    debug_viewer: Path


@dataclass
class UnitAuditResult:
    unit: str
    status: str
    reason: str
    output_path: str
    manifest_path: str
    print_source_path: str
    metrics_path: str
    debug_stage_report: str | None = None
    debug_viewer: str | None = None
    triangle_count: int = 0
    vertex_count: int = 0
    boundary_edge_count: int = 0
    non_manifold_edge_count: int = 0
    height_mm: float | None = None


@dataclass
class ExportAuditSession:
    root: Path
    results: list[UnitAuditResult]


@pytest.fixture(scope="session")
def export_audit_session(tmp_path_factory: pytest.TempPathFactory):
    state = ExportAuditSession(
        root=tmp_path_factory.mktemp("export_audit"),
        results=[],
    )
    yield state
    if state.results:
        summary_path = state.root / "export_audit_summary.json"
        summary_path.write_text(
            json.dumps([asdict(result) for result in state.results], indent=2),
            encoding="utf-8",
        )


def _setting(config: dict, env_name: str, config_key: str) -> str | None:
    return os.environ.get(env_name) or config.get(config_key)


def _load_audit_config() -> dict:
    try:
        return load_config(None)
    except BarPrintConfigError as exc:
        pytest.fail(f"Could not load barprint config for export audit: {exc}")


def _audit_context() -> tuple[str, Path, Path]:
    config = _load_audit_config()
    try:
        blender = find_blender(_setting(config, "BLENDER_EXE", "blender"))
    except BlenderRunnerError as exc:
        pytest.fail(f"Export audit requires Blender. Set BLENDER_EXE or config 'blender': {exc}")
    try:
        bar_root = find_bar_root(config.get("bar_root"))
    except BarAssetError as exc:
        pytest.fail(f"Export audit requires BAR data. Set config 'bar_root': {exc}")
    importer_value = _setting(config, "S3O_IMPORTER_PATH", "s3o_importer")
    if not importer_value:
        pytest.fail("Export audit requires an S3O importer. Set S3O_IMPORTER_PATH or config 's3o_importer'.")
    importer = Path(importer_value).expanduser().resolve()
    if not importer.is_file():
        pytest.fail(f"Export audit S3O importer does not exist: {importer}")
    return blender, bar_root, importer


def _selected_units(units: list[UnitAsset]) -> list[UnitAsset]:
    selected = os.environ.get("BARPRINT_EXPORT_AUDIT_UNITS")
    if not selected:
        return units
    requested = [item.strip().casefold() for item in selected.split(",") if item.strip()]
    by_code = {unit.unit_code.casefold(): unit for unit in units}
    missing = [unit_code for unit_code in requested if unit_code not in by_code]
    if missing:
        pytest.fail(f"BARPRINT_EXPORT_AUDIT_UNITS contains unknown unit(s): {', '.join(missing)}")
    return [by_code[unit_code] for unit_code in requested]


def _audit_params() -> list:
    if not AUDIT_ENABLED:
        return [pytest.param(None, marks=pytest.mark.skip(reason="set BARPRINT_EXPORT_AUDIT=1 to run export audit"))]
    _, bar_root, _ = _audit_context()
    units = _selected_units(list_units(bar_root))
    if not units:
        pytest.fail(f"Export audit found no units under {bar_root}")
    return [pytest.param(unit, id=unit.unit_code) for unit in units]


@pytest.mark.parametrize("unit", _audit_params())
def test_unit_exports_cleanly(unit: UnitAsset | None, export_audit_session: ExportAuditSession) -> None:
    if unit is None:
        pytest.skip("set BARPRINT_EXPORT_AUDIT=1 to run export audit")

    blender, _, importer = _audit_context()
    paths = audit_paths(export_audit_session.root, unit.unit_code)
    result = UnitAuditResult(
        unit=unit.unit_code,
        status="failed",
        reason="audit did not complete",
        output_path=str(paths.output),
        manifest_path=str(paths.manifest),
        print_source_path=str(paths.print_source),
        metrics_path=str(paths.metrics),
    )

    try:
        s3o_path = ensure_unit_s3o(unit)
        profile = audit_profile(unit, s3o_path)
        profile_path = write_temp_profile(profile)
        run_blender_export(
            blender,
            _blender_script_path(),
            s3o_path,
            importer,
            profile_path,
            "neutral",
            paths.output,
            "stl",
            {"export_support_files": True},
        )
        validate_audit_output(paths, profile, result)
    except Exception as exc:
        result.reason = str(exc)
        add_debug_artifacts(unit, blender, importer, paths, result)
        write_result(paths.metrics, result)
        export_audit_session.results.append(result)
        pytest.fail(format_failure(result))

    result.status = "passed"
    result.reason = ""
    write_result(paths.metrics, result)
    export_audit_session.results.append(result)


def audit_paths(root: Path, unit_code: str) -> AuditPaths:
    unit_dir = root / unit_code
    output = unit_dir / f"{unit_code}.stl"
    debug_output = unit_dir / f"{unit_code}_debug_failure.stl"
    debug_paths = debug_stage_paths(debug_output)
    return AuditPaths(
        output=output,
        manifest=output.with_name(f"{output.stem}_manifest.json"),
        print_source=output.with_name(f"{output.stem}_print_source.glb"),
        metrics=unit_dir / f"{unit_code}_audit_metrics.json",
        debug_output=debug_output,
        debug_stage_report=debug_paths.stage_report,
        debug_viewer=debug_paths.viewer_html,
    )


def validate_audit_output(paths: AuditPaths, profile: dict, result: UnitAuditResult) -> None:
    require_file(paths.output, "final STL")
    require_file(paths.manifest, "manifest")
    require_file(paths.print_source, "opaque print source GLB")

    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    assert_manifest_profile(manifest, profile)
    print_source = manifest.get("print_source")
    if not isinstance(print_source, dict):
        raise AssertionError("manifest is missing print_source object")
    if print_source.get("mode") != "opaque_glb_intermediate":
        raise AssertionError(f"unexpected print_source mode: {print_source.get('mode')}")
    assert_height_close(
        print_source.get("bounds_after_reload", {}).get("height"),
        print_source.get("bounds_expected_after_reload", {}).get("height"),
        "opaque print-source reload height",
    )

    metrics = stl_metrics(paths.output)
    result.triangle_count = metrics["triangle_count"]
    result.vertex_count = metrics["vertex_count"]
    result.boundary_edge_count = metrics["boundary_edge_count"]
    result.non_manifold_edge_count = metrics["non_manifold_edge_count"]
    result.height_mm = metrics["height_mm"]

    expected_height = float(manifest.get("scale_mm") or profile.get("scale_mm") or 45.0)
    assert_height_close(metrics["height_mm"], expected_height, "final STL height")
    if metrics["boundary_edge_count"]:
        raise AssertionError(f"final STL has {metrics['boundary_edge_count']} boundary edge(s)")
    if metrics["non_manifold_edge_count"]:
        raise AssertionError(f"final STL has {metrics['non_manifold_edge_count']} non-manifold edge(s)")


def add_debug_artifacts(
    unit: UnitAsset,
    blender: str,
    importer: Path,
    paths: AuditPaths,
    result: UnitAuditResult,
) -> None:
    try:
        s3o_path = ensure_unit_s3o(unit)
        profile = audit_profile(unit, s3o_path)
        profile_path = write_temp_profile(profile)
        run_blender_export(
            blender,
            _blender_script_path(),
            s3o_path,
            importer,
            profile_path,
            "neutral",
            paths.debug_output,
            "stl",
            {"debug_stages": True, "export_support_files": True},
        )
    except Exception as exc:
        result.debug_stage_report = None
        result.debug_viewer = None
        result.reason = f"{result.reason}; debug rerun failed: {exc}"
        return
    result.debug_stage_report = str(paths.debug_stage_report)
    result.debug_viewer = str(paths.debug_viewer)


def audit_profile(unit: UnitAsset, s3o_path: Path) -> dict:
    return apply_overrides(load_profile_for_source(None, unit=unit, s3o_path=s3o_path))


def assert_manifest_profile(manifest: dict, profile: dict) -> None:
    if manifest.get("pose_profile_name") != profile.get("name"):
        raise AssertionError(
            f"manifest pose profile mismatch: {manifest.get('pose_profile_name')} != {profile.get('name')}"
        )
    if manifest.get("pose_archetype") != profile.get("pose_archetype"):
        raise AssertionError(
            f"manifest pose archetype mismatch: {manifest.get('pose_archetype')} != {profile.get('pose_archetype')}"
        )
    if manifest.get("pose_source") != profile.get("pose_source"):
        raise AssertionError(
            f"manifest pose source mismatch: {manifest.get('pose_source')} != {profile.get('pose_source')}"
        )


def write_result(path: Path, result: UnitAuditResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")


def format_failure(result: UnitAuditResult) -> str:
    lines = [
        f"{result.unit} failed export audit: {result.reason}",
        f"output: {result.output_path}",
        f"metrics: {result.metrics_path}",
    ]
    if result.debug_stage_report:
        lines.append(f"debug stage report: {result.debug_stage_report}")
    if result.debug_viewer:
        lines.append(f"debug viewer: {result.debug_viewer}")
    return "\n".join(lines)


def require_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise AssertionError(f"missing non-empty {label}: {path}")


def assert_height_close(actual: float | int | None, expected: float | int | None, label: str) -> None:
    if actual is None or expected is None:
        raise AssertionError(f"{label} is missing")
    actual_float = float(actual)
    expected_float = float(expected)
    if expected_float <= 0:
        raise AssertionError(f"{label} expected height is invalid: {expected_float}")
    if abs(actual_float - expected_float) > expected_float * 0.05:
        raise AssertionError(f"{label} drifted: actual={actual_float:.5f}, expected={expected_float:.5f}")


def stl_metrics(path: Path) -> dict[str, float | int]:
    triangles = read_stl_triangles(path)
    if not triangles:
        raise AssertionError(f"STL contains no triangles: {path}")

    edge_counts: dict[tuple[tuple[int, int, int], tuple[int, int, int]], int] = {}
    vertices = []
    for triangle in triangles:
        vertices.extend(triangle)
        for start, end in ((triangle[0], triangle[1]), (triangle[1], triangle[2]), (triangle[2], triangle[0])):
            edge = tuple(sorted((quantize_point(start), quantize_point(end))))
            edge_counts[edge] = edge_counts.get(edge, 0) + 1

    min_z = min(vertex[2] for vertex in vertices)
    max_z = max(vertex[2] for vertex in vertices)
    return {
        "triangle_count": len(triangles),
        "vertex_count": len(vertices),
        "height_mm": max_z - min_z,
        "boundary_edge_count": sum(1 for count in edge_counts.values() if count == 1),
        "non_manifold_edge_count": sum(1 for count in edge_counts.values() if count != 2),
    }


def quantize_point(point: tuple[float, float, float]) -> tuple[int, int, int]:
    return tuple(round(value / TOPOLOGY_QUANTIZATION_MM) for value in point)  # type: ignore[return-value]


def read_stl_triangles(path: Path) -> list[tuple[tuple[float, float, float], ...]]:
    data = path.read_bytes()
    return read_binary_stl_triangles(data) or read_ascii_stl_triangles(data)


def read_binary_stl_triangles(data: bytes) -> list[tuple[tuple[float, float, float], ...]]:
    if len(data) < 84:
        return []
    triangle_count = struct.unpack_from("<I", data, 80)[0]
    if len(data) != 84 + triangle_count * 50:
        return []
    triangles = []
    offset = 84
    for _ in range(triangle_count):
        offset += 12
        triangles.append(tuple(struct.unpack_from("<3f", data, offset + index * 12) for index in range(3)))
        offset += 38
    return triangles


def read_ascii_stl_triangles(data: bytes) -> list[tuple[tuple[float, float, float], ...]]:
    triangles = []
    current = []
    for line in data.decode("utf-8", errors="ignore").splitlines():
        parts = line.strip().split()
        if len(parts) != 4 or parts[0] != "vertex":
            continue
        current.append((float(parts[1]), float(parts[2]), float(parts[3])))
        if len(current) == 3:
            triangles.append(tuple(current))
            current = []
    return triangles


def test_stl_metrics_counts_closed_tetrahedron(tmp_path: Path) -> None:
    stl = tmp_path / "closed.stl"
    write_ascii_stl(
        stl,
        [
            ((0, 0, 0), (1, 0, 0), (0, 1, 0)),
            ((0, 0, 0), (0, 0, 1), (1, 0, 0)),
            ((0, 0, 0), (0, 1, 0), (0, 0, 1)),
            ((1, 0, 0), (0, 0, 1), (0, 1, 0)),
        ],
    )

    metrics = stl_metrics(stl)

    assert metrics["triangle_count"] == 4
    assert metrics["boundary_edge_count"] == 0
    assert metrics["non_manifold_edge_count"] == 0
    assert metrics["height_mm"] == 1


def test_stl_metrics_counts_boundary_edges(tmp_path: Path) -> None:
    stl = tmp_path / "open.stl"
    write_ascii_stl(stl, [((0, 0, 0), (1, 0, 0), (0, 1, 0))])

    metrics = stl_metrics(stl)

    assert metrics["boundary_edge_count"] == 3
    assert metrics["non_manifold_edge_count"] == 3


def write_ascii_stl(path: Path, triangles: list[tuple[tuple[float, float, float], ...]]) -> None:
    lines = ["solid test"]
    for triangle in triangles:
        lines.extend(["  facet normal 0 0 0", "    outer loop"])
        for vertex in triangle:
            lines.append(f"      vertex {vertex[0]} {vertex[1]} {vertex[2]}")
        lines.extend(["    endloop", "  endfacet"])
    lines.append("endsolid test")
    path.write_text("\n".join(lines), encoding="utf-8")
