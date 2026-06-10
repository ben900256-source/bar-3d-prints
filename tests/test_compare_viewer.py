from pathlib import Path

from barprint.compare_viewer import (
    CompareViewerRow,
    compare_viewer_paths,
    debug_stage_paths,
    write_compare_viewer_html,
    write_compare_viewer_rows_html,
)


def test_compare_viewer_asset_paths_for_explicit_and_default_outputs(tmp_path: Path) -> None:
    explicit = compare_viewer_paths(tmp_path / "exports" / "armliche.stl")
    default = compare_viewer_paths(Path("out") / "armliche" / "armliche.stl")
    pre_named = compare_viewer_paths(tmp_path / "exports" / "armliche_pre.stl")

    assert explicit.default_stl == tmp_path / "exports" / "armliche.stl"
    assert explicit.game_glb == tmp_path / "exports" / "armliche_game.glb"
    assert explicit.post_thickening_stl == tmp_path / "exports" / "armliche_post.stl"
    assert explicit.viewer_html == tmp_path / "exports" / "armliche_viewer.html"
    assert default.default_stl == Path("out") / "armliche" / "armliche.stl"
    assert default.game_glb == Path("out") / "armliche" / "armliche_game.glb"
    assert default.post_thickening_stl == Path("out") / "armliche" / "armliche_post.stl"
    assert pre_named.default_stl == tmp_path / "exports" / "armliche_pre.stl"
    assert pre_named.game_glb == tmp_path / "exports" / "armliche_game.glb"
    assert pre_named.post_thickening_stl == tmp_path / "exports" / "armliche_post.stl"
    assert pre_named.viewer_html == tmp_path / "exports" / "armliche_viewer.html"


def test_debug_stage_paths_for_explicit_default_and_pre_outputs(tmp_path: Path) -> None:
    explicit = debug_stage_paths(tmp_path / "exports" / "armliche.stl")
    default = debug_stage_paths(Path("out") / "armliche" / "armliche.stl")
    pre_named = debug_stage_paths(tmp_path / "exports" / "armliche_pre.stl")

    assert explicit.debug_dir == tmp_path / "exports" / "armliche_debug"
    assert explicit.stage_report == tmp_path / "exports" / "armliche_debug" / "stage_report.json"
    assert explicit.viewer_html == tmp_path / "exports" / "armliche_debug" / "armliche_debug_viewer.html"
    assert explicit.opaque_print_source_glb == (
        tmp_path / "exports" / "armliche_debug" / "armliche_opaque_print_source.glb"
    )
    assert default.debug_dir == Path("out") / "armliche" / "armliche_debug"
    assert default.viewer_html == Path("out") / "armliche" / "armliche_debug" / "armliche_debug_viewer.html"
    assert pre_named.debug_dir == tmp_path / "exports" / "armliche_debug"
    assert pre_named.viewer_html == tmp_path / "exports" / "armliche_debug" / "armliche_debug_viewer.html"


def test_write_compare_viewer_html_references_single_three_pane_viewer(tmp_path: Path) -> None:
    viewer = tmp_path / "out" / "armliche_viewer.html"

    write_compare_viewer_html(
        viewer,
        default_stl=tmp_path / "out" / "armliche_pre.stl",
        game_glb=tmp_path / "out" / "armliche_game.glb",
        post_thickening_stl=tmp_path / "out" / "armliche_post.stl",
    )

    html = viewer.read_text(encoding="utf-8")
    assert "Default" in html
    assert "In-Game" in html
    assert "Post-Thickening" in html
    assert "Opaque Print Source" not in html
    assert "armliche_pre.stl" in html
    assert "armliche_game.glb" in html
    assert "armliche_post.stl" in html
    assert "postThickeningStl" in html
    assert "__barprintViewerStarted" in html
    assert "Promise.allSettled" in html
    assert "registerLoadedBox" in html
    assert "LOAD_TIMEOUT_MS" in html
    assert "WebGL" in html
    assert "timed out after" in html
    assert "Failed to load" in html
    assert "Browser file access can block model loads" in html
    assert "cdn.jsdelivr.net" not in html
    assert "importmap" not in html
    assert 'from "three"' not in html
    assert "Detailed" not in html
    assert "Detail Highlights" not in html
    assert "detailedStl" not in html
    assert "highlightsJson" not in html
    assert "armliche_highlights.json" not in html


def test_write_compare_viewer_html_includes_opaque_pane_when_debug_asset_is_passed(tmp_path: Path) -> None:
    viewer = tmp_path / "out" / "armliche_debug" / "armliche_debug_viewer.html"

    write_compare_viewer_html(
        viewer,
        default_stl=tmp_path / "out" / "armliche.stl",
        game_glb=tmp_path / "out" / "armliche_debug" / "armliche_game.glb",
        post_thickening_stl=tmp_path / "out" / "armliche_debug" / "armliche_post.stl",
        opaque_print_source_glb=tmp_path / "out" / "armliche_debug" / "armliche_opaque_print_source.glb",
    )

    html = viewer.read_text(encoding="utf-8")
    assert "Opaque Print Source" in html
    assert "opaquePrintSourceGlb" in html
    assert "armliche_opaque_print_source.glb" in html
    assert "../armliche.stl" in html


def test_write_compare_viewer_rows_html_groups_poses_into_rows(tmp_path: Path) -> None:
    viewer = tmp_path / "out" / "armcom_debug_viewer.html"

    write_compare_viewer_rows_html(
        viewer,
        rows=[
            CompareViewerRow(
                label="neutral",
                default_stl=tmp_path / "out" / "armcom_neutral.stl",
                game_glb=tmp_path / "out" / "armcom_neutral_debug" / "armcom_neutral_game.glb",
                post_thickening_stl=tmp_path / "out" / "armcom_neutral_debug" / "armcom_neutral_post.stl",
                opaque_print_source_glb=(
                    tmp_path / "out" / "armcom_neutral_debug" / "armcom_neutral_opaque_print_source.glb"
                ),
            ),
            CompareViewerRow(
                label="aim_left",
                default_stl=tmp_path / "out" / "armcom_aim_left.stl",
                game_glb=tmp_path / "out" / "armcom_aim_left_debug" / "armcom_aim_left_game.glb",
                post_thickening_stl=tmp_path / "out" / "armcom_aim_left_debug" / "armcom_aim_left_post.stl",
                opaque_print_source_glb=(
                    tmp_path / "out" / "armcom_aim_left_debug" / "armcom_aim_left_opaque_print_source.glb"
                ),
            ),
        ],
    )

    html = viewer.read_text(encoding="utf-8")
    assert 'data-row="row0"' in html
    assert 'data-row="row1"' in html
    assert "neutral" in html
    assert "aim_left" in html
    assert "row-panes" in html
    assert "grid-auto-rows" in html
    assert "armcom_neutral.stl" in html
    assert "armcom_aim_left_debug/armcom_aim_left_game.glb" in html
