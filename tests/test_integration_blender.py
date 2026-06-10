from pathlib import Path
import os
import json
import struct

import pytest

from barprint.bar_assets import BarAssetError, find_bar_root, resolve_unit_to_s3o
from barprint.blender_runner import run_blender_export
from barprint.cli import _blender_script_path
from barprint.compare_viewer import debug_stage_paths
from barprint.config import BarPrintConfigError, load_config
from barprint.pose_profiles import apply_overrides, load_profile, write_temp_profile


def _integration_config() -> dict[str, str]:
    try:
        return load_config(None)
    except BarPrintConfigError:
        return {}


def _setting(config: dict[str, str], env_name: str, config_key: str) -> str | None:
    return os.environ.get(env_name) or config.get(config_key)


def _integration_s3o_path(config: dict[str, str]) -> str | None:
    explicit = _setting(config, "TEST_S3O_PATH", "test_s3o_path")
    if explicit:
        return explicit
    try:
        bar_root = find_bar_root(config.get("bar_root"))
        reference_unit = str(config.get("scale_reference_unit") or "armcom")
        return str(resolve_unit_to_s3o(bar_root, reference_unit))
    except BarAssetError:
        return None


INTEGRATION_CONFIG = _integration_config()
BLENDER_EXE = _setting(INTEGRATION_CONFIG, "BLENDER_EXE", "blender")
TEST_S3O_PATH = _integration_s3o_path(INTEGRATION_CONFIG)
S3O_IMPORTER_PATH = _setting(INTEGRATION_CONFIG, "S3O_IMPORTER_PATH", "s3o_importer")


@pytest.mark.skipif(
    not BLENDER_EXE or not TEST_S3O_PATH or not S3O_IMPORTER_PATH,
    reason=(
        "Set BLENDER_EXE, TEST_S3O_PATH, and S3O_IMPORTER_PATH, "
        "or set BARPRINT_CONFIG / barprint.local.json."
    ),
)
def test_blender_export_creates_stl(tmp_path: Path) -> None:
    profile = apply_overrides(
        load_profile(None),
        base_enabled=False,
        thin_features_enabled=False,
    )
    profile_path = write_temp_profile(profile)
    out_path = tmp_path / "model.stl"
    run_blender_export(
        BLENDER_EXE,
        _blender_script_path(),
        Path(TEST_S3O_PATH),
        Path(S3O_IMPORTER_PATH),
        profile_path,
        "neutral",
        out_path,
        "stl",
        {},
    )
    assert out_path.is_file()
    assert out_path.stat().st_size > 0

    stl_bounds = read_stl_bounds(out_path)
    expected_height = float(profile["scale_mm"])
    assert abs(stl_bounds["height"] - expected_height) <= expected_height * 0.05

    manifest = json.loads(out_path.with_name("model_manifest.json").read_text(encoding="utf-8"))
    print_source = manifest["print_source"]
    assert print_source["mode"] == "opaque_glb_intermediate"
    assert print_source["material_mode"] == "forced_opaque_beige"
    assert abs(print_source["bounds_after_reload"]["height"] - expected_height) <= expected_height * 0.05
    assert (
        abs(
            print_source["bounds_after_reload"]["height"]
            - print_source["bounds_expected_after_reload"]["height"]
        )
        <= print_source["bounds_expected_after_reload"]["height"] * 0.05
    )
    mesh_closure = manifest["mesh_closure"]
    assert mesh_closure["mode"] == "weld_and_cap_boundary_loops"
    assert mesh_closure["weld_epsilon_mm"] == 0.001
    assert "boundary_edge_count_before" in mesh_closure
    assert "boundary_edge_count_after" in mesh_closure
    assert "non_manifold_edge_count_before" in mesh_closure
    assert "non_manifold_edge_count_after" in mesh_closure
    assert "convex_hull_rebuilt_objects" in mesh_closure
    assert "convex_hull_vertices_removed" in mesh_closure
    assert "convex_hull_faces_removed" in mesh_closure


@pytest.mark.skipif(
    not (BLENDER_EXE and TEST_S3O_PATH and S3O_IMPORTER_PATH),
    reason=(
        "Set BLENDER_EXE, TEST_S3O_PATH, and S3O_IMPORTER_PATH, "
        "or configure Blender, BAR data, and an S3O importer in barprint.local.json."
    ),
)
def test_blender_debug_stages_create_report_and_viewer(tmp_path: Path) -> None:
    profile = apply_overrides(
        load_profile(None),
        base_enabled=False,
        thin_features_enabled=False,
    )
    profile_path = write_temp_profile(profile)
    out_path = tmp_path / "debug_model.stl"

    run_blender_export(
        BLENDER_EXE,
        _blender_script_path(),
        Path(TEST_S3O_PATH),
        Path(S3O_IMPORTER_PATH),
        profile_path,
        "neutral",
        out_path,
        "stl",
        {"debug_stages": True},
    )

    paths = debug_stage_paths(out_path)
    report = json.loads(paths.stage_report.read_text(encoding="utf-8"))
    stage_ids = [stage["id"] for stage in report["stages"]]

    assert out_path.is_file()
    assert paths.stage_report.is_file()
    assert paths.viewer_html.is_file()
    assert paths.opaque_print_source_glb.is_file()
    assert paths.post_thickening_stl.is_file()
    assert paths.game_glb.is_file()
    assert stage_ids == ["A", "B", "C", "D", "E", "F", "G", "G2", "H", "I"]
    for stage in report["stages"]:
        metrics = stage["metrics"]
        for key in (
            "object_count",
            "mesh_count",
            "vertex_count",
            "face_count",
            "bounding_box",
            "non_manifold_edge_count",
            "boundary_edge_count",
            "alpha_material_count",
            "thin_sheet_component_count",
        ):
            assert key in metrics
        assert "render_png" in stage["assets"]
    viewer_html = paths.viewer_html.read_text(encoding="utf-8")
    assert "opaquePrintSourceGlb" in viewer_html

    manifest = json.loads(out_path.with_name("debug_model_manifest.json").read_text(encoding="utf-8"))
    assert manifest["debug_stages"]["stage_report"] == str(paths.stage_report.resolve())


def read_stl_bounds(path: Path) -> dict[str, float]:
    data = path.read_bytes()
    points = read_binary_stl_points(data) or read_ascii_stl_points(data)
    assert points
    min_z = min(point[2] for point in points)
    max_z = max(point[2] for point in points)
    return {"height": max_z - min_z}


def read_binary_stl_points(data: bytes) -> list[tuple[float, float, float]]:
    if len(data) < 84:
        return []
    triangle_count = struct.unpack_from("<I", data, 80)[0]
    if len(data) != 84 + triangle_count * 50:
        return []
    points: list[tuple[float, float, float]] = []
    offset = 84
    for _ in range(triangle_count):
        offset += 12
        points.extend(struct.unpack_from("<3f", data, offset + index * 12) for index in range(3))
        offset += 38
    return points


def read_ascii_stl_points(data: bytes) -> list[tuple[float, float, float]]:
    points = []
    for line in data.decode("utf-8", errors="ignore").splitlines():
        parts = line.strip().split()
        if len(parts) != 4 or parts[0] != "vertex":
            continue
        points.append((float(parts[1]), float(parts[2]), float(parts[3])))
    return points

