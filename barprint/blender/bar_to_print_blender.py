from __future__ import annotations

import argparse
import bmesh
import importlib.util
import json
import math
import os
import shutil
import struct
import sys
import traceback
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from barprint.compare_viewer import debug_stage_paths, write_compare_viewer_html
from barprint.coordinate_transforms import GLB_TO_PRINT_AXIS_CORRECTION, glb_imported_to_print_point

import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree


WARNINGS: list[str] = []
DEBUG_OBJECT_PREFIX = "barprint_debug_"
OPAQUE_BEIGE_RGBA = (0.84, 0.815, 0.745, 1.0)
DEBUG_VIEW_DIRECTION = Vector((-1.1, 1.45, 0.82)).normalized()
MESH_CLOSURE_WELD_EPSILON_MM = 0.001
MESH_CLOSURE_SHORT_NON_MANIFOLD_EDGE_MM = 0.02
MESH_CLOSURE_PLANAR_CAP_MAX_DEVIATION_MM = 0.05
MESH_CLOSURE_FAN_CAP_MAX_EDGES = 128
MESH_CLOSURE_SMALL_OPEN_BOUNDARY_REPAIR_MAX_SPAN_MM = 0.25
MESH_CLOSURE_BOUNDARY_FILL_PASSES = 3
MESH_CLOSURE_OVERUSED_FACE_REMOVAL_LIMIT = 128
MESH_CLOSURE_CONVEX_HULL_MAX_BOUNDS_DRIFT_MM = 0.01
MESH_CLOSURE_CONVEX_HULL_MAX_INPUT_FACES = 320
MESH_CLOSURE_GEOMETRIC_CONVEX_HULL_MAX_FACES = 96
MESH_CLOSURE_PLANAR_SHEET_SOLIDIFY_THICKNESS_MM = 0.8
MESH_CLOSURE_PLANAR_SHEET_MAX_DEVIATION_MM = 0.01
MESH_CLOSURE_PLANAR_SHEET_MAX_FACES = 128
LOCAL_FACE_REPAIR_MAX_CANDIDATE_FACES = 24
LOCAL_FACE_REPAIR_MAX_REMOVE_FACES = 10
LOCAL_FACE_REPAIR_MAX_PASSES = 64
LOCAL_FACE_REPAIR_MAX_COMBINATIONS_PER_CLUSTER = 50000
GEOMETRIC_CLEANUP_QUANTIZATION_MM = 1e-5
GEOMETRIC_CLEANUP_MAX_CANDIDATE_FACES = 24
GEOMETRIC_CLEANUP_MAX_REMOVE_FACES = 10
GEOMETRIC_CLEANUP_COMPONENT_HULL_MAX_FACES = 320
GEOMETRIC_CLEANUP_COMPONENT_NUDGE_MM = 0.01
GEOMETRIC_CLEANUP_MAX_NUDGE_PASSES = 16

DEBUG_STAGE_LABELS = {
    "A": "Imported S3O with original materials",
    "B": "Imported S3O forced opaque beige",
    "C": "After pose/profile",
    "D": "After visual transforms",
    "E": "After scale/ground",
    "F": "After GLB export/reimport",
    "G": "After GLB-to-print axis correction",
    "G2": "After mesh closure",
    "H": "After join",
    "I": "Final STL reimported",
}


def main() -> int:
    args = parse_args()
    try:
        out_path = Path(args.out)
        progress("Cleaning scene")
        clean_scene()
        progress("Loading S3O importer")
        enable_s3o_importer(Path(args.s3o_importer))
        progress("Importing S3O")
        import_s3o(Path(args.s3o))
        if args.inspect_pieces:
            progress("Inspecting pieces")
            write_piece_inspection(out_path, Path(args.s3o))
            progress("Done")
            return 0

        debugger = None
        if args.debug_stages and args.format == "glb":
            WARNINGS.append("--debug-stages captures the print pipeline and is skipped for direct GLB export.")
        elif args.debug_stages:
            debugger = StageDebugger(
                out_path,
                debug_dir=Path(args.debug_output).expanduser() if args.debug_output else None,
                default_stl=Path(args.debug_default_stl).expanduser() if args.debug_default_stl else out_path,
            )
            debugger.capture("A")
            debugger.capture("B", opaque=True)

        progress("Loading pose profile")
        profile = load_profile(Path(args.pose_profile))
        progress(f"Applying pose {args.pose}")
        apply_pose(profile, args.pose)
        apply_piece_transforms(profile)
        delete_pieces(profile)
        if args.format == "glb":
            progress("Applying visual transforms")
            apply_visual_transforms()
            progress("Scaling model")
            scale_to_target_height(float(profile.get("scale_mm", 45)))
            move_to_ground()
            progress("Preparing GLB textures")
            glb_texture_dir = prepare_images_for_gltf(out_path)
            progress("Exporting GLB")
            try:
                export_model(out_path, "glb")
            finally:
                if glb_texture_dir is not None:
                    shutil.rmtree(glb_texture_dir, ignore_errors=True)
            progress("Writing manifest")
            write_manifest(
                out_path,
                {
                    "source_s3o": str(Path(args.s3o).resolve()),
                    "pose_name": args.pose,
                    "pose_profile_name": profile.get("name"),
                    "pose_archetype": profile.get("pose_archetype"),
                    "pose_source": profile.get("pose_source"),
                    "variant_name": profile.get("variant_name", "standard"),
                    "scale_mm": profile.get("scale_mm"),
                    "scale": profile.get("scale", {}),
                    "export_mode": "textured_game_glb",
                    "warnings": WARNINGS,
                    "blender_version": bpy.app.version_string,
                },
            )
            progress("Done")
            return 0
        add_printable_markers(profile)
        if debugger:
            debugger.capture("C")
        apply_visual_transforms()
        if debugger:
            debugger.capture("D")
        progress("Scaling model")
        scale_to_target_height(float(profile.get("scale_mm", 45)))
        move_to_ground()
        if debugger:
            debugger.capture("E")
            debugger.export_game_glb()
        progress("Writing GLB print source")
        print_source = reload_scene_from_glb_print_source(out_path, debugger=debugger)
        progress("Closing mesh boundary loops")
        mesh_closure = apply_mesh_closure()
        if debugger:
            debugger.capture("G2")
        progress("Thickening thin features")
        thin_features = apply_thin_features(profile)
        if debugger:
            debugger.export_post_thickening_stl()
        progress("Joining meshes")
        joined = join_meshes()
        if debugger:
            debugger.capture("H")
        base = profile.get("base") or {}
        if base.get("enabled", False):
            progress("Adding base")
            joined = add_base(joined, float(base.get("diameter_mm", 32)), float(base.get("height_mm", 2.4)))
        progress("Grounding joined model")
        move_to_ground()
        progress("Cleaning joined geometric overlaps")
        post_join_cleanup = clean_joined_geometric_overlaps(joined)
        if args.keep_raw:
            raw_path = out_path.with_name(out_path.stem + "_raw.stl")
            progress("Exporting raw STL")
            export_model(raw_path, "stl")
        progress(f"Exporting {args.format.upper()}")
        export_model(out_path, args.format)
        debug_manifest = None
        if debugger:
            debugger.capture_final_export(out_path, args.format)
            debug_manifest = debugger.finalize()
        progress("Writing manifest")
        manifest = {
            "source_s3o": str(Path(args.s3o).resolve()),
            "pose_name": args.pose,
            "pose_profile_name": profile.get("name"),
            "pose_archetype": profile.get("pose_archetype"),
            "pose_source": profile.get("pose_source"),
            "variant_name": profile.get("variant_name", "standard"),
            "scale_mm": profile.get("scale_mm"),
            "scale": profile.get("scale", {}),
            "thin_features": thin_features,
            "print_source": print_source,
            "mesh_closure": mesh_closure,
            "post_join_cleanup": post_join_cleanup,
            "base": profile.get("base", {}),
            "warnings": WARNINGS,
            "blender_version": bpy.app.version_string,
        }
        if debug_manifest is not None:
            manifest["debug_stages"] = debug_manifest
        write_manifest(out_path, manifest)
        progress("Done")
        return 0
    except Exception as exc:
        print(f"BARPRINT_ERROR: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


def progress(message: str) -> None:
    print(f"BARPRINT_PROGRESS: {message}", flush=True)


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    parser = argparse.ArgumentParser()
    parser.add_argument("--s3o", required=True)
    parser.add_argument("--s3o-importer", required=True)
    parser.add_argument("--pose-profile", required=True)
    parser.add_argument("--pose", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--format", choices=["stl", "3mf", "glb"], default="stl")
    parser.add_argument("--keep-raw", action="store_true")
    parser.add_argument("--inspect-pieces", action="store_true")
    parser.add_argument("--debug-stages", action="store_true")
    parser.add_argument("--debug-output")
    parser.add_argument("--debug-default-stl")
    return parser.parse_args(argv)


def clean_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def enable_s3o_importer(importer_path: Path) -> None:
    if not importer_path.is_file():
        raise RuntimeError(f"S3O importer missing: {importer_path}")
    if importer_path.suffix.lower() == ".zip":
        bpy.ops.preferences.addon_install(filepath=str(importer_path), overwrite=True)
        module_name = importer_path.stem
        try:
            bpy.ops.preferences.addon_enable(module=module_name)
        except Exception as exc:
            WARNINGS.append(f"Could not enable addon module '{module_name}' automatically: {exc}")
        return

    module_name = f"barprint_s3o_importer_{abs(hash(str(importer_path)))}"
    spec = importlib.util.spec_from_file_location(module_name, importer_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load importer from {importer_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    register = getattr(module, "register", None)
    if callable(register):
        register()


def import_s3o(s3o_path: Path) -> None:
    operators = []
    if hasattr(bpy.ops.import_scene, "s3o"):
        operators.append(bpy.ops.import_scene.s3o)
    if hasattr(bpy.ops.import_mesh, "s3o"):
        operators.append(bpy.ops.import_mesh.s3o)
    if not operators:
        available = sorted(
            name
            for namespace in (bpy.ops.import_scene, bpy.ops.import_mesh)
            for name in dir(namespace)
            if not name.startswith("_")
        )
        raise RuntimeError(
            "S3O import operator not registered. Tried bpy.ops.import_scene.s3o and "
            f"bpy.ops.import_mesh.s3o. Available import operators: {available}"
        )

    before = set(bpy.data.objects)
    errors: list[str] = []
    for operator in operators:
        try:
            operator(filepath=str(s3o_path))
            break
        except TypeError:
            try:
                operator(path=str(s3o_path))
                break
            except Exception as exc:
                errors.append(str(exc))
        except Exception as exc:
            errors.append(str(exc))
    else:
        raise RuntimeError(f"S3O import failed for {s3o_path}: {'; '.join(errors)}")

    imported = [obj for obj in bpy.data.objects if obj not in before]
    if not imported:
        WARNINGS.append("Importer completed but did not create new objects.")
    if not mesh_objects():
        raise RuntimeError("Model imported but no meshes found.")


def import_glb(glb_path: Path) -> None:
    if not glb_path.is_file():
        raise RuntimeError(f"GLB intermediate missing: {glb_path}")
    if not hasattr(bpy.ops.import_scene, "gltf"):
        raise RuntimeError("glTF import is unavailable in this Blender installation.")
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(glb_path))
    imported = [obj for obj in bpy.data.objects if obj not in before]
    if not any(obj.type == "MESH" for obj in imported):
        raise RuntimeError(f"GLB import produced no mesh objects: {glb_path}")
    bpy.ops.object.select_all(action="DESELECT")
    for obj in imported:
        obj.select_set(True)


def import_stl(stl_path: Path) -> None:
    if not stl_path.is_file():
        raise RuntimeError(f"STL missing: {stl_path}")
    before = set(bpy.data.objects)
    if hasattr(bpy.ops.wm, "stl_import"):
        bpy.ops.wm.stl_import(filepath=str(stl_path))
    elif hasattr(bpy.ops.import_mesh, "stl"):
        bpy.ops.import_mesh.stl(filepath=str(stl_path))
    else:
        raise RuntimeError("STL import operator is unavailable in this Blender installation.")
    imported = [obj for obj in bpy.data.objects if obj not in before]
    if not any(obj.type == "MESH" for obj in imported):
        raise RuntimeError(f"STL import produced no mesh objects: {stl_path}")
    bpy.ops.object.select_all(action="DESELECT")
    for obj in imported:
        obj.select_set(True)


def reload_scene_from_glb_print_source(out_path: Path, debugger=None) -> dict:
    face_count_before = sum(len(obj.data.polygons) for obj in mesh_objects())
    bounds_before_export = scene_bounds()
    glb_path = print_source_glb_path(out_path)
    original_slots = force_opaque_materials()
    try:
        export_model(glb_path, "glb")
    finally:
        restore_material_slots(original_slots)
    if not glb_path.is_file() or glb_path.stat().st_size == 0:
        raise RuntimeError(f"GLB print source was not created: {glb_path}")

    clean_scene()
    import_glb(glb_path)
    bounds_after_import = scene_bounds()
    if debugger:
        debugger.capture("F")
    bounds_expected_after_reload = glb_expected_print_bounds_after_reload()
    require_bounds_height_close(
        bounds_before_export,
        bounds_expected_after_reload,
        "GLB print source import",
    )
    apply_glb_print_axis_correction()
    move_to_ground()
    bounds_after_reload = scene_bounds()
    if debugger:
        debugger.capture("G")
        debugger.export_opaque_print_source_glb()
    require_bounds_height_close(
        bounds_expected_after_reload,
        bounds_after_reload,
        "GLB print source reload correction",
    )
    return {
        "mode": "opaque_glb_intermediate",
        "path": str(glb_path.resolve()),
        "material_mode": "forced_opaque_beige",
        "material_rgba": list(OPAQUE_BEIGE_RGBA),
        "face_count_before": face_count_before,
        "face_count_after": sum(len(obj.data.polygons) for obj in mesh_objects()),
        "axis_correction": {
            "mapping": "(x, y, z) -> (x, z, -y)",
            "matrix": GLB_TO_PRINT_AXIS_CORRECTION,
        },
        "bounds_before_export": bounds_before_export,
        "bounds_after_import": bounds_after_import,
        "bounds_expected_after_reload": bounds_expected_after_reload,
        "bounds_after_reload": bounds_after_reload,
    }


def print_source_glb_path(out_path: Path) -> Path:
    return out_path.with_name(f"{out_path.stem}_print_source.glb")


class StageDebugger:
    def __init__(self, out_path: Path, *, debug_dir: Path | None = None, default_stl: Path | None = None) -> None:
        self.out_path = out_path
        self.default_stl = default_stl or out_path
        self.paths = debug_stage_paths(out_path, debug_dir)
        self.stages_dir = self.paths.debug_dir / "stages"
        self.paths.debug_dir.mkdir(parents=True, exist_ok=True)
        self.stages_dir.mkdir(parents=True, exist_ok=True)
        self.report = {
            "version": 1,
            "output_path": str(out_path.resolve()),
            "default_stl": str(self.default_stl.resolve()),
            "debug_dir": str(self.paths.debug_dir.resolve()),
            "stage_order": list(DEBUG_STAGE_LABELS),
            "diagnosis_rule": {
                "opaque_print_source_vs_default_stl": (
                    "Default STL is generated from an opaque print-source GLB. If the opaque print-source pane "
                    "and Default STL diverge, suspect join/export corruption after the print-source stage."
                )
            },
            "assets": {},
            "stages": [],
            "errors": [],
        }

    def capture(self, stage_id: str, *, opaque: bool = False, snapshot_glb: bool = True) -> None:
        progress(f"Debug stage {stage_id}: {DEBUG_STAGE_LABELS[stage_id]}")
        if opaque:
            self._with_opaque_materials(lambda: self._capture(stage_id, snapshot_glb=snapshot_glb))
        else:
            self._capture(stage_id, snapshot_glb=snapshot_glb)

    def export_game_glb(self) -> None:
        self._write_asset(
            "in_game_glb",
            self.paths.game_glb,
            "glb",
            lambda: export_glb_snapshot(self.paths.game_glb),
        )

    def export_post_thickening_stl(self) -> None:
        self._write_asset(
            "post_thickening_stl",
            self.paths.post_thickening_stl,
            "stl",
            lambda: export_model(self.paths.post_thickening_stl, "stl"),
        )

    def export_opaque_print_source_glb(self) -> None:
        self._write_asset(
            "opaque_print_source_glb",
            self.paths.opaque_print_source_glb,
            "glb",
            lambda: self._with_opaque_materials(
                lambda: export_glb_snapshot(self.paths.opaque_print_source_glb, with_textures=False)
            ),
        )

    def capture_final_export(self, out_path: Path, format_name: str) -> None:
        if format_name != "stl":
            self._append_error_stage("I", f"Final stage reimport requires STL output, got {format_name}.")
            return
        try:
            clean_scene()
            import_stl(out_path)
        except Exception as exc:
            self._append_error_stage("I", f"Could not reimport final STL: {exc}")
            return
        self.capture("I", opaque=True)

    def finalize(self) -> dict:
        opaque_glb = self.paths.opaque_print_source_glb if self.paths.opaque_print_source_glb.is_file() else None
        try:
            write_compare_viewer_html(
                self.paths.viewer_html,
                default_stl=self.default_stl,
                game_glb=self.paths.game_glb,
                post_thickening_stl=self.paths.post_thickening_stl,
                opaque_print_source_glb=opaque_glb,
            )
            self._record_asset("debug_viewer_html", self.paths.viewer_html, "html")
        except Exception as exc:
            self.report["errors"].append(f"Could not write debug viewer: {exc}")
        legacy_viewer_config = self.paths.debug_dir / "viewer_config.json"
        if legacy_viewer_config.is_file():
            legacy_viewer_config.unlink()

        self.report["pipeline_warnings"] = list(WARNINGS)
        self._record_asset("stage_report", self.paths.stage_report, "json")
        self.paths.stage_report.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        return {
            "directory": str(self.paths.debug_dir.resolve()),
            "stage_report": str(self.paths.stage_report.resolve()),
            "viewer_html": str(self.paths.viewer_html.resolve()),
            "in_game_glb": str(self.paths.game_glb.resolve()),
            "post_thickening_stl": str(self.paths.post_thickening_stl.resolve()),
            "opaque_print_source_glb": str(self.paths.opaque_print_source_glb.resolve()),
            "stage_ids": [stage["id"] for stage in self.report["stages"]],
        }

    def _capture(self, stage_id: str, *, snapshot_glb: bool) -> None:
        entry = {
            "id": stage_id,
            "label": DEBUG_STAGE_LABELS[stage_id],
            "metrics": empty_stage_metrics(),
            "assets": {},
            "warnings": [],
            "errors": [],
        }
        try:
            entry["metrics"] = collect_scene_metrics()
        except Exception as exc:
            entry["errors"].append(f"Could not collect metrics: {exc}")

        png_path = self._stage_asset_path(stage_id, "png")
        try:
            render_debug_stage_png(png_path)
            entry["assets"]["render_png"] = self._debug_relative(png_path)
        except Exception as exc:
            entry["errors"].append(f"Could not render PNG: {exc}")

        if snapshot_glb:
            glb_path = self._stage_asset_path(stage_id, "glb")
            try:
                export_glb_snapshot(glb_path)
                entry["assets"]["snapshot_glb"] = self._debug_relative(glb_path)
            except Exception as exc:
                entry["errors"].append(f"Could not write GLB snapshot: {exc}")

        self.report["stages"].append(entry)

    def _append_error_stage(self, stage_id: str, error: str) -> None:
        self.report["stages"].append(
            {
                "id": stage_id,
                "label": DEBUG_STAGE_LABELS[stage_id],
                "metrics": empty_stage_metrics(),
                "assets": {},
                "warnings": [],
                "errors": [error],
            }
        )

    def _write_asset(self, key: str, path: Path, kind: str, writer) -> None:
        try:
            writer()
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError(f"{kind.upper()} asset was not created: {path}")
            self._record_asset(key, path, kind)
        except Exception as exc:
            self.report["errors"].append(f"Could not write {key}: {exc}")

    def _record_asset(self, key: str, path: Path, kind: str) -> None:
        self.report["assets"][key] = {
            "kind": kind,
            "path": str(path.resolve()),
            "relative_path": self._debug_relative(path),
        }

    def _stage_asset_path(self, stage_id: str, suffix: str) -> Path:
        label = safe_debug_filename(DEBUG_STAGE_LABELS[stage_id])
        return self.stages_dir / f"stage_{stage_id}_{label}.{suffix}"

    def _debug_relative(self, path: Path) -> str:
        return Path(os.path.relpath(path, self.paths.debug_dir)).as_posix()

    def _with_opaque_materials(self, callback):
        original_slots = force_opaque_materials()
        try:
            return callback()
        finally:
            restore_material_slots(original_slots)


def empty_stage_metrics() -> dict:
    return {
        "object_count": 0,
        "mesh_count": 0,
        "vertex_count": 0,
        "face_count": 0,
        "bounding_box": None,
        "non_manifold_edge_count": 0,
        "boundary_edge_count": 0,
        "alpha_material_count": 0,
        "thin_sheet_component_count": 0,
        "objects": [],
    }


def export_glb_snapshot(out_path: Path, *, with_textures: bool = True) -> None:
    texture_dir = None
    try:
        if with_textures:
            texture_dir = prepare_images_for_gltf(out_path)
        export_model(out_path, "glb")
    finally:
        if texture_dir is not None:
            shutil.rmtree(texture_dir, ignore_errors=True)


def render_debug_stage_png(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_out = out_path.resolve()
    camera = ensure_debug_camera()
    ensure_debug_lights()
    fit_debug_camera(camera)
    scene = bpy.context.scene
    scene.camera = camera
    scene.render.filepath = str(resolved_out.with_suffix(""))
    scene.render.resolution_x = 1200
    scene.render.resolution_y = 900
    scene.render.film_transparent = False
    scene.render.use_file_extension = True
    scene.render.image_settings.file_format = "PNG"
    set_debug_render_engine(scene)
    bpy.ops.render.render(write_still=True)
    if resolved_out.is_file() and resolved_out.stat().st_size > 0:
        return
    candidates = sorted(resolved_out.parent.glob(f"{resolved_out.stem}*.png"))
    if candidates:
        os.replace(candidates[0], resolved_out)
    if not resolved_out.is_file() or resolved_out.stat().st_size == 0:
        raise RuntimeError(f"PNG render was not created: {resolved_out}")


def ensure_debug_camera() -> bpy.types.Object:
    camera = bpy.data.objects.get(f"{DEBUG_OBJECT_PREFIX}camera")
    if camera is None:
        camera_data = bpy.data.cameras.new(f"{DEBUG_OBJECT_PREFIX}camera_data")
        camera = bpy.data.objects.new(f"{DEBUG_OBJECT_PREFIX}camera", camera_data)
        bpy.context.collection.objects.link(camera)
    camera.data.lens = 55
    camera.data.angle = math.radians(35)
    return camera


def ensure_debug_lights() -> None:
    light_specs = [
        ("key", "SUN", (34.0, -42.0, 58.0), 2.8),
        ("fill", "SUN", (-44.0, 26.0, 34.0), 0.9),
    ]
    for name, light_type, location, energy in light_specs:
        object_name = f"{DEBUG_OBJECT_PREFIX}{name}_light"
        light = bpy.data.objects.get(object_name)
        if light is None:
            light_data = bpy.data.lights.new(f"{object_name}_data", light_type)
            light = bpy.data.objects.new(object_name, light_data)
            bpy.context.collection.objects.link(light)
        light.location = location
        light.data.energy = energy


def fit_debug_camera(camera: bpy.types.Object) -> None:
    min_v, max_v = bounds(mesh_objects())
    center = (min_v + max_v) / 2
    dimensions = max_v - min_v
    max_size = max(float(dimensions.x), float(dimensions.y), float(dimensions.z), 1.0)
    distance = (max_size / (2 * math.tan(math.radians(35) / 2))) * 1.65
    camera.location = center + DEBUG_VIEW_DIRECTION * distance
    direction = center - camera.location
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    camera.data.clip_start = max(distance / 1000, 0.01)
    camera.data.clip_end = distance * 12


def set_debug_render_engine(scene: bpy.types.Scene) -> None:
    for engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_WORKBENCH"):
        try:
            scene.render.engine = engine
            return
        except Exception:
            continue


def force_opaque_materials() -> list[tuple[bpy.types.Mesh, list[bpy.types.Material]]]:
    material = debug_opaque_material()
    original_slots = []
    seen_meshes = set()
    for obj in mesh_objects():
        mesh = obj.data
        if mesh.name in seen_meshes:
            continue
        seen_meshes.add(mesh.name)
        original_slots.append((mesh, list(mesh.materials)))
        mesh.materials.clear()
        mesh.materials.append(material)
    return original_slots


def restore_material_slots(original_slots: list[tuple[bpy.types.Mesh, list[bpy.types.Material]]]) -> None:
    for mesh, materials in original_slots:
        mesh.materials.clear()
        for material in materials:
            mesh.materials.append(material)


def debug_opaque_material() -> bpy.types.Material:
    material = bpy.data.materials.get(f"{DEBUG_OBJECT_PREFIX}opaque_beige")
    if material is None:
        material = bpy.data.materials.new(f"{DEBUG_OBJECT_PREFIX}opaque_beige")
    material.diffuse_color = OPAQUE_BEIGE_RGBA
    material.blend_method = "OPAQUE"
    material.use_nodes = True
    if material.node_tree is not None:
        for node in material.node_tree.nodes:
            if node.bl_idname != "ShaderNodeBsdfPrincipled":
                continue
            if "Base Color" in node.inputs:
                node.inputs["Base Color"].default_value = OPAQUE_BEIGE_RGBA
            if "Alpha" in node.inputs:
                node.inputs["Alpha"].default_value = 1.0
    return material


def collect_scene_metrics() -> dict:
    meshes = mesh_objects()
    topology = [mesh_topology_counts(obj.data) for obj in meshes]
    return {
        "object_count": len(diagnostic_objects()),
        "mesh_count": len(meshes),
        "vertex_count": sum(len(obj.data.vertices) for obj in meshes),
        "face_count": sum(len(obj.data.polygons) for obj in meshes),
        "bounding_box": scene_bounds() if meshes else None,
        "non_manifold_edge_count": sum(item["non_manifold_edge_count"] for item in topology),
        "boundary_edge_count": sum(item["boundary_edge_count"] for item in topology),
        "alpha_material_count": alpha_material_count(meshes),
        "thin_sheet_component_count": thin_sheet_component_count(meshes),
        "objects": [diagnostic_object_metrics(obj) for obj in meshes],
    }


def diagnostic_objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.data.objects if not obj.name.startswith(DEBUG_OBJECT_PREFIX)]


def diagnostic_object_metrics(obj: bpy.types.Object) -> dict:
    topology = mesh_topology_counts(obj.data)
    return {
        "name": obj.name,
        "vertex_count": len(obj.data.vertices),
        "face_count": len(obj.data.polygons),
        "bounds": object_bounds(obj) if obj.data.vertices else None,
        **topology,
    }


def mesh_topology_counts(mesh: bpy.types.Mesh) -> dict:
    edge_face_counts = {tuple(sorted(edge.vertices)): 0 for edge in mesh.edges}
    for polygon in mesh.polygons:
        for edge_key in polygon.edge_keys:
            key = tuple(sorted(edge_key))
            edge_face_counts[key] = edge_face_counts.get(key, 0) + 1
    return {
        "non_manifold_edge_count": sum(1 for count in edge_face_counts.values() if count != 2),
        "boundary_edge_count": sum(1 for count in edge_face_counts.values() if count == 1),
    }


def mesh_geometric_topology_counts(mesh: bpy.types.Mesh) -> dict:
    edge_face_counts: dict[tuple[tuple[int, int, int], tuple[int, int, int]], int] = {}
    for polygon in mesh.polygons:
        vertex_positions = [mesh.vertices[index].co for index in polygon.vertices]
        for index, start in enumerate(vertex_positions):
            end = vertex_positions[(index + 1) % len(vertex_positions)]
            edge_key = tuple(
                sorted(
                    (
                        quantized_vector(start, GEOMETRIC_CLEANUP_QUANTIZATION_MM),
                        quantized_vector(end, GEOMETRIC_CLEANUP_QUANTIZATION_MM),
                    )
                )
            )
            edge_face_counts[edge_key] = edge_face_counts.get(edge_key, 0) + 1
    return {
        "non_manifold_edge_count": sum(1 for count in edge_face_counts.values() if count != 2),
        "boundary_edge_count": sum(1 for count in edge_face_counts.values() if count == 1),
    }


def alpha_material_count(objects: list[bpy.types.Object]) -> int:
    materials = {
        material.name: material
        for obj in objects
        for material in obj.data.materials
        if material is not None and not material.name.startswith(DEBUG_OBJECT_PREFIX)
    }
    return sum(1 for material in materials.values() if material_has_alpha(material))


def material_has_alpha(material: bpy.types.Material) -> bool:
    if getattr(material, "blend_method", "OPAQUE") != "OPAQUE":
        return True
    diffuse = getattr(material, "diffuse_color", None)
    if diffuse is not None and len(diffuse) >= 4 and diffuse[3] < 0.999:
        return True
    node_tree = getattr(material, "node_tree", None)
    if not material.use_nodes or node_tree is None:
        return False
    for node in node_tree.nodes:
        if node.bl_idname == "ShaderNodeBsdfPrincipled" and principled_node_has_alpha(node):
            return True
        if node.bl_idname == "ShaderNodeTexImage" and image_node_may_use_alpha(node):
            return True
    return False


def principled_node_has_alpha(node) -> bool:
    alpha_input = node.inputs.get("Alpha")
    if alpha_input is None:
        return False
    if alpha_input.is_linked:
        return True
    return getattr(alpha_input, "default_value", 1.0) < 0.999


def image_node_may_use_alpha(node) -> bool:
    image = getattr(node, "image", None)
    if image is None:
        return False
    alpha_mode = getattr(image, "alpha_mode", "NONE")
    if alpha_mode not in {"NONE", "CHANNEL_PACKED"}:
        return True
    return getattr(image, "depth", 0) in {32, 64}


def thin_sheet_component_count(objects: list[bpy.types.Object]) -> int:
    count = 0
    for obj in objects:
        for component in mesh_loose_components(obj.data):
            if len(component) < 3:
                continue
            dimensions = sorted(component_world_dimensions(obj, component))
            longest = dimensions[-1]
            if longest <= 0:
                continue
            near_zero_threshold = max(longest * 0.005, 0.02)
            if dimensions[0] <= near_zero_threshold and dimensions[1] > near_zero_threshold * 2:
                count += 1
    return count


def component_world_dimensions(obj: bpy.types.Object, component: list[int]) -> tuple[float, float, float]:
    coordinates = [obj.matrix_world @ obj.data.vertices[index].co for index in component]
    return (
        max(co.x for co in coordinates) - min(co.x for co in coordinates),
        max(co.y for co in coordinates) - min(co.y for co in coordinates),
        max(co.z for co in coordinates) - min(co.z for co in coordinates),
    )


def safe_debug_filename(name: str) -> str:
    safe = "".join(char.lower() if char.isalnum() else "_" for char in name)
    safe = "_".join(part for part in safe.split("_") if part)
    return safe[:80] or "stage"


def load_profile(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_piece(alias: str, aliases: dict[str, list[str]], *, warn_missing: bool = True) -> bpy.types.Object | None:
    candidates = aliases.get(alias, [alias])
    objects = [obj for obj in bpy.data.objects if obj.type in {"EMPTY", "MESH"}]
    ranked: list[tuple[int, int, bpy.types.Object]] = []
    for obj in objects:
        name = obj.name.casefold()
        for needle in candidates:
            needle_cf = needle.casefold()
            if name == needle_cf:
                ranked.append((0, len(name), obj))
            elif name.startswith(needle_cf):
                ranked.append((1, len(name), obj))
            elif needle_cf in name:
                ranked.append((2, len(name), obj))
    if not ranked:
        if warn_missing:
            WARNINGS.append(f"No piece matched alias '{alias}' with candidates {candidates}")
        return None
    ranked.sort(key=lambda item: (item[0], item[1], item[2].name))
    return ranked[0][2]


def apply_pose(profile: dict, pose_name: str) -> None:
    poses = {pose["name"]: pose for pose in profile.get("poses", [])}
    if pose_name not in poses:
        raise RuntimeError(f"Pose '{pose_name}' not found in profile")
    aliases = profile.get("piece_aliases", {})
    for alias, rotation in poses[pose_name].get("pieces", {}).items():
        obj = find_piece(alias, aliases)
        if obj is None:
            continue
        obj.rotation_euler.rotate_axis("X", math.radians(float(rotation.get("rot_x_deg", 0))))
        obj.rotation_euler.rotate_axis("Y", math.radians(float(rotation.get("rot_y_deg", 0))))
        obj.rotation_euler.rotate_axis("Z", math.radians(float(rotation.get("rot_z_deg", 0))))


def apply_piece_transforms(profile: dict) -> None:
    aliases = profile.get("piece_aliases", {})
    for alias, transform in (profile.get("piece_transforms") or {}).items():
        apply_piece_transform(alias, transform, aliases, warn_missing=True)
    for alias, transform in (profile.get("optional_piece_transforms") or {}).items():
        apply_piece_transform(alias, transform, aliases, warn_missing=False)


def apply_piece_transform(alias: str, transform: dict, aliases: dict[str, list[str]], *, warn_missing: bool) -> None:
    obj = find_piece(alias, aliases | {alias: [alias]}, warn_missing=warn_missing)
    if obj is None:
        return
    obj.location.x += float(transform.get("translate_x", 0))
    obj.location.y += float(transform.get("translate_y", 0))
    obj.location.z += float(transform.get("translate_z", 0))


def delete_pieces(profile: dict) -> None:
    aliases = profile.get("piece_aliases", {})
    for alias in profile.get("delete_piece_aliases", []):
        delete_piece_alias(alias, aliases, warn_missing=True)
    for alias in profile.get("optional_delete_piece_aliases", []):
        delete_piece_alias(alias, aliases, warn_missing=False)


def delete_piece_alias(alias: str, aliases: dict[str, list[str]], *, warn_missing: bool) -> None:
    obj = find_piece(alias, aliases | {alias: [alias]}, warn_missing=warn_missing)
    if obj is None:
        return
    children = list(obj.children_recursive)
    for child in children:
        bpy.data.objects.remove(child, do_unlink=True)
    bpy.data.objects.remove(obj, do_unlink=True)


def add_printable_markers(profile: dict) -> None:
    marker_config = profile.get("printable_markers") or {}
    if not marker_config.get("enabled", False):
        return
    aliases = profile.get("piece_aliases", {})
    muzzle_aliases = marker_config.get("muzzle_aliases", ["flare", "muzzle"])
    source_units_per_mm = marker_source_units_per_mm(profile)
    radius = float(marker_config.get("radius_mm", 0.9)) * source_units_per_mm
    length = float(marker_config.get("length_mm", 3.0)) * source_units_per_mm
    overlap = float(marker_config.get("overlap_mm", 1.2)) * source_units_per_mm
    sphere_radius = float(marker_config.get("sphere_radius_mm", marker_config.get("radius_mm", 0.9) * 1.2)) * source_units_per_mm
    bridge_to_parent = bool(marker_config.get("bridge_to_parent", False))

    for alias in muzzle_aliases:
        obj = find_piece(alias, aliases | {alias: [alias]})
        if obj is None or obj.type != "EMPTY":
            continue
        marker = make_marker_for_empty(obj, radius, length, sphere_radius, overlap, bridge_to_parent)
        marker.name = f"barprint_marker_{obj.name}"
        WARNINGS.append(f"Materialized empty weapon marker '{obj.name}' as printable geometry.")


def marker_source_units_per_mm(profile: dict) -> float:
    target_mm = float(profile.get("scale_mm", 45))
    if target_mm <= 0:
        return 1.0
    min_v, max_v = bounds(mesh_objects())
    source_height = max_v.z - min_v.z
    if source_height <= 0:
        return 1.0
    return source_height / target_mm


def make_marker_for_empty(
    empty: bpy.types.Object,
    radius: float,
    length: float,
    sphere_radius: float,
    overlap: float,
    bridge_to_parent: bool,
) -> bpy.types.Object:
    world_pos = empty.matrix_world.translation
    parent_pos = empty.parent.matrix_world.translation if empty.parent else world_pos - Vector((0, length, 0))
    direction = world_pos - parent_pos
    if direction.length < 0.001:
        direction = Vector((0, 1, 0))
    direction.normalize()
    marker_length = length + overlap
    if bridge_to_parent and empty.parent is not None:
        marker_length = max(marker_length, (world_pos - parent_pos).length + overlap)
    center = world_pos - direction * (marker_length / 2)

    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=radius, depth=marker_length, location=center)
    cylinder = bpy.context.object
    cylinder.name = f"barprint_marker_body_{empty.name}"
    cylinder.rotation_euler = direction.to_track_quat("Z", "Y").to_euler()

    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=sphere_radius, location=world_pos)
    sphere = bpy.context.object
    sphere.name = f"barprint_marker_tip_{empty.name}"

    bpy.ops.object.select_all(action="DESELECT")
    cylinder.select_set(True)
    sphere.select_set(True)
    bpy.context.view_layer.objects.active = cylinder
    bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


LOCAL_FACE_REPAIR_COUNT_KEYS = (
    "clusters_considered",
    "clusters_repaired",
    "clusters_skipped_large",
    "candidate_faces",
    "edges_split",
    "vertices_nudged",
    "faces_removed",
    "boundary_edge_count_before",
    "non_manifold_edge_count_before",
    "boundary_edge_count_after",
    "non_manifold_edge_count_after",
)


def local_face_repair_manifest_fields(prefix: str) -> dict:
    fields = {f"{prefix}_{key}": value for key, value in local_face_repair_fields().items()}
    return fields


def local_face_repair_fields() -> dict:
    fields = {key: 0 for key in LOCAL_FACE_REPAIR_COUNT_KEYS}
    fields["area_removed_mm2"] = 0.0
    return fields


def merge_local_face_repair_manifest_fields(target: dict, prefix: str, repair: dict) -> None:
    for key in LOCAL_FACE_REPAIR_COUNT_KEYS:
        target[f"{prefix}_{key}"] += repair[key]
    target[f"{prefix}_area_removed_mm2"] = round(
        target[f"{prefix}_area_removed_mm2"] + repair["area_removed_mm2"],
        6,
    )


def local_face_repair_changed(repair: dict) -> bool:
    return bool(
        repair["faces_removed"]
        or repair["edges_split"]
        or repair["vertices_nudged"]
        or repair["clusters_repaired"]
    )


def apply_mesh_closure(weld_epsilon_mm: float = MESH_CLOSURE_WELD_EPSILON_MM) -> dict:
    if weld_epsilon_mm <= 0:
        raise RuntimeError("mesh_closure weld epsilon must be greater than zero")

    objects = mesh_objects()
    topology_before = topology_counts_for_objects(objects)
    result = {
        "mode": "weld_and_cap_boundary_loops",
        "weld_epsilon_mm": weld_epsilon_mm,
        "mesh_count": len(objects),
        "boundary_edge_count_before": topology_before["boundary_edge_count"],
        "non_manifold_edge_count_before": topology_before["non_manifold_edge_count"],
        "boundary_edge_count_after": None,
        "non_manifold_edge_count_after": None,
        "boundary_fill_passes": MESH_CLOSURE_BOUNDARY_FILL_PASSES,
        "secondary_boundary_fill_passes": 0,
        "vertices_merged": 0,
        "loops_filled": 0,
        "faces_added": 0,
        "faces_removed": 0,
        "short_non_manifold_edge_collapse_threshold_mm": MESH_CLOSURE_SHORT_NON_MANIFOLD_EDGE_MM,
        "short_non_manifold_edges_collapsed": 0,
        "planar_cap_max_deviation_mm": MESH_CLOSURE_PLANAR_CAP_MAX_DEVIATION_MM,
        "fan_cap_max_edges": MESH_CLOSURE_FAN_CAP_MAX_EDGES,
        "fan_caps_filled": 0,
        "small_open_boundary_repair_max_span_mm": MESH_CLOSURE_SMALL_OPEN_BOUNDARY_REPAIR_MAX_SPAN_MM,
        "small_open_boundary_components_repaired": 0,
        "open_boundary_chains_capped": 0,
        "branched_components_decomposed": 0,
        "planar_caps_filled": 0,
        "loose_edges_removed": 0,
        "overused_faces_removed": 0,
        "convex_hull_fallback": "residual_boundary_or_non_manifold_objects",
        "convex_hull_max_bounds_drift_mm": MESH_CLOSURE_CONVEX_HULL_MAX_BOUNDS_DRIFT_MM,
        "convex_hull_max_input_faces": MESH_CLOSURE_CONVEX_HULL_MAX_INPUT_FACES,
        "convex_hull_rebuilt_objects": 0,
        "convex_hull_vertices_removed": 0,
        "convex_hull_faces_removed": 0,
        "planar_sheet_solidify_thickness_mm": MESH_CLOSURE_PLANAR_SHEET_SOLIDIFY_THICKNESS_MM,
        "planar_sheet_solidified_objects": 0,
        "planar_sheet_vertices_added": 0,
        "planar_sheet_faces_added": 0,
        **local_face_repair_manifest_fields("local_mesh_repair"),
        **local_face_repair_manifest_fields("local_geometric_repair"),
        "bounds_before": scene_bounds() if objects else None,
        "bounds_after": None,
        "objects": [],
        "warnings": [],
    }

    for obj in objects:
        object_result = close_mesh_object_boundary_loops(obj, weld_epsilon_mm=weld_epsilon_mm)
        result["vertices_merged"] += object_result["vertices_merged"]
        result["secondary_boundary_fill_passes"] += object_result["secondary_boundary_fill_passes"]
        result["loops_filled"] += object_result["loops_filled"]
        result["faces_added"] += object_result["faces_added"]
        result["faces_removed"] += object_result["faces_removed"]
        result["short_non_manifold_edges_collapsed"] += object_result["short_non_manifold_edges_collapsed"]
        result["fan_caps_filled"] += object_result["fan_caps_filled"]
        result["small_open_boundary_components_repaired"] += object_result[
            "small_open_boundary_components_repaired"
        ]
        result["open_boundary_chains_capped"] += object_result["open_boundary_chains_capped"]
        result["branched_components_decomposed"] += object_result["branched_components_decomposed"]
        result["planar_caps_filled"] += object_result["planar_caps_filled"]
        result["loose_edges_removed"] += object_result["loose_edges_removed"]
        result["overused_faces_removed"] += object_result["overused_faces_removed"]
        merge_local_face_repair_manifest_fields(result, "local_mesh_repair", object_result["local_mesh_repair"])
        merge_local_face_repair_manifest_fields(
            result,
            "local_geometric_repair",
            object_result["local_geometric_repair"],
        )
        if object_result["convex_hull_rebuilt"]:
            result["convex_hull_rebuilt_objects"] += 1
            result["convex_hull_vertices_removed"] += max(
                0,
                object_result["convex_hull_vertex_count_before"] - object_result["convex_hull_vertex_count_after"],
            )
            result["convex_hull_faces_removed"] += max(
                0,
                object_result["convex_hull_face_count_before"] - object_result["convex_hull_face_count_after"],
            )
        if object_result["planar_sheet_solidified"]:
            result["planar_sheet_solidified_objects"] += 1
            result["planar_sheet_vertices_added"] += max(
                0,
                object_result["planar_sheet_vertex_count_after"] - object_result["planar_sheet_vertex_count_before"],
            )
            result["planar_sheet_faces_added"] += max(
                0,
                object_result["planar_sheet_face_count_after"] - object_result["planar_sheet_face_count_before"],
            )
        result["warnings"].extend(object_result["warnings"])
        if closure_object_result_is_reportable(object_result):
            result["objects"].append(object_result)

    topology_after = topology_counts_for_objects(mesh_objects())
    result["boundary_edge_count_after"] = topology_after["boundary_edge_count"]
    result["non_manifold_edge_count_after"] = topology_after["non_manifold_edge_count"]
    result["bounds_after"] = scene_bounds() if mesh_objects() else None
    if result["warnings"]:
        WARNINGS.extend(f"Mesh closure: {warning}" for warning in result["warnings"])
    return result


def topology_counts_for_objects(objects: list[bpy.types.Object]) -> dict:
    topology = [mesh_topology_counts(obj.data) for obj in objects]
    return {
        "boundary_edge_count": sum(item["boundary_edge_count"] for item in topology),
        "non_manifold_edge_count": sum(item["non_manifold_edge_count"] for item in topology),
    }


def closure_object_result_is_reportable(result: dict) -> bool:
    return bool(
        result["vertices_merged"]
        or result["loops_filled"]
        or result["faces_added"]
        or result["faces_removed"]
        or result["short_non_manifold_edges_collapsed"]
        or result["secondary_boundary_fill_passes"]
        or result["fan_caps_filled"]
        or result["small_open_boundary_components_repaired"]
        or result["open_boundary_chains_capped"]
        or result["branched_components_decomposed"]
        or result["planar_caps_filled"]
        or result["loose_edges_removed"]
        or result["overused_faces_removed"]
        or local_face_repair_changed(result["local_mesh_repair"])
        or local_face_repair_changed(result["local_geometric_repair"])
        or result["convex_hull_rebuilt"]
        or result["planar_sheet_solidified"]
        or result["warnings"]
        or result["boundary_edge_count_before"] != result["boundary_edge_count_after"]
        or result["non_manifold_edge_count_before"] != result["non_manifold_edge_count_after"]
    )


def close_mesh_object_boundary_loops(obj: bpy.types.Object, *, weld_epsilon_mm: float) -> dict:
    mesh = obj.data
    topology_before = mesh_topology_counts(mesh)
    result = {
        "name": obj.name,
        "vertex_count_before": len(mesh.vertices),
        "vertex_count_after": len(mesh.vertices),
        "face_count_before": len(mesh.polygons),
        "face_count_after": len(mesh.polygons),
        "boundary_edge_count_before": topology_before["boundary_edge_count"],
        "non_manifold_edge_count_before": topology_before["non_manifold_edge_count"],
        "boundary_edge_count_after": topology_before["boundary_edge_count"],
        "non_manifold_edge_count_after": topology_before["non_manifold_edge_count"],
        "geometric_boundary_edge_count_before_hull": 0,
        "geometric_non_manifold_edge_count_before_hull": 0,
        "geometric_boundary_edge_count_after": 0,
        "geometric_non_manifold_edge_count_after": 0,
        "geometric_convex_hull_max_faces": MESH_CLOSURE_GEOMETRIC_CONVEX_HULL_MAX_FACES,
        "boundary_loops_detected": 0,
        "secondary_boundary_fill_passes": 0,
        "vertices_merged": 0,
        "loops_filled": 0,
        "faces_added": 0,
        "faces_removed": 0,
        "short_non_manifold_edges_collapsed": 0,
        "fan_caps_filled": 0,
        "small_open_boundary_components_repaired": 0,
        "open_boundary_chains_capped": 0,
        "branched_components_decomposed": 0,
        "planar_caps_filled": 0,
        "loose_edges_removed": 0,
        "overused_faces_removed": 0,
        "convex_hull_rebuilt": False,
        "convex_hull_max_input_faces": MESH_CLOSURE_CONVEX_HULL_MAX_INPUT_FACES,
        "convex_hull_vertex_count_before": 0,
        "convex_hull_vertex_count_after": 0,
        "convex_hull_face_count_before": 0,
        "convex_hull_face_count_after": 0,
        "convex_hull_bounds_before": None,
        "convex_hull_bounds_after": None,
        "planar_sheet_solidified": False,
        "planar_sheet_solidify_thickness_mm": MESH_CLOSURE_PLANAR_SHEET_SOLIDIFY_THICKNESS_MM,
        "planar_sheet_vertex_count_before": 0,
        "planar_sheet_vertex_count_after": 0,
        "planar_sheet_face_count_before": 0,
        "planar_sheet_face_count_after": 0,
        "planar_sheet_bounds_before": None,
        "planar_sheet_bounds_after": None,
        "local_mesh_repair": local_face_repair_fields(),
        "local_geometric_repair": local_face_repair_fields(),
        "warnings": [],
    }
    if not mesh.vertices or not mesh.polygons:
        return result

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        vertex_count_before_weld = len(bm.verts)
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=weld_epsilon_mm)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bmesh.ops.dissolve_degenerate(bm, edges=list(bm.edges), dist=weld_epsilon_mm)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.edges.index_update()
        bm.faces.index_update()
        result["vertices_merged"] = max(0, vertex_count_before_weld - len(bm.verts))

        face_count_before_fill = len(bm.faces)
        for fill_pass in range(MESH_CLOSURE_BOUNDARY_FILL_PASSES):
            boundary_components = boundary_edge_components(bm)
            if fill_pass == 0:
                result["boundary_loops_detected"] = len(boundary_components)
            elif boundary_components:
                result["secondary_boundary_fill_passes"] += 1

            generated_faces: list[bmesh.types.BMFace] = []
            fill_changed = False
            for component in boundary_components:
                component = valid_bmesh_edges(component)
                if not component:
                    continue
                fillable_components = boundary_component_fillable_loops(component)
                if not fillable_components:
                    open_chain_faces = cap_open_boundary_chain_with_existing_edge(bm, component)
                    if open_chain_faces:
                        result["open_boundary_chains_capped"] += 1
                        result["loops_filled"] += 1
                        generated_faces.extend(open_chain_faces)
                        fill_changed = True
                        continue
                    if repair_small_open_boundary_component(
                        bm,
                        component,
                        max_span_mm=MESH_CLOSURE_SMALL_OPEN_BOUNDARY_REPAIR_MAX_SPAN_MM,
                    ):
                        result["small_open_boundary_components_repaired"] += 1
                        fill_changed = True
                        continue
                    append_boundary_warning(result, obj.name, component, "not a closed loop")
                    continue
                if len(fillable_components) > 1 or fillable_components[0] is not component:
                    result["branched_components_decomposed"] += 1
                for fillable_component in fillable_components:
                    fillable_component = valid_bmesh_edges(fillable_component)
                    if not fillable_component:
                        continue
                    filled_faces, fill_mode = fill_boundary_component(bm, fillable_component)
                    if not filled_faces:
                        append_boundary_warning(result, obj.name, fillable_component, "fill created no faces")
                        continue
                    if fill_mode == "planar_cap":
                        result["planar_caps_filled"] += 1
                    elif fill_mode == "fan_cap":
                        result["fan_caps_filled"] += 1
                    result["loops_filled"] += 1
                    generated_faces.extend(filled_faces)
                    fill_changed = True

            generated_faces = [face for face in generated_faces if face.is_valid]
            if generated_faces:
                bmesh.ops.triangulate(bm, faces=generated_faces)
                bm.faces.ensure_lookup_table()
            cleanup_result = clean_non_manifold_artifacts_after_closure(bm)
            result["faces_removed"] += cleanup_result["faces_removed"]
            result["short_non_manifold_edges_collapsed"] += cleanup_result["short_non_manifold_edges_collapsed"]
            result["loose_edges_removed"] += cleanup_result["loose_edges_removed"]
            result["overused_faces_removed"] += cleanup_result["overused_faces_removed"]

            cleanup_changed = any(cleanup_result.values())
            if bmesh_topology_counts(bm)["boundary_edge_count"] == 0:
                break
            if not fill_changed and not cleanup_changed:
                break
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.faces.ensure_lookup_table()
        result["faces_added"] = max(0, len(bm.faces) - face_count_before_fill)

        bm.to_mesh(mesh)
    finally:
        bm.free()

    mesh.update()
    recalculate_mesh_normals(mesh)
    topology_after = mesh_topology_counts(mesh)
    sheet_result = solidify_planar_non_manifold_sheet_object(
        obj,
        topology_after=topology_after,
        thickness_mm=MESH_CLOSURE_PLANAR_SHEET_SOLIDIFY_THICKNESS_MM,
    )
    result.update({key: value for key, value in sheet_result.items() if key != "warnings"})
    result["warnings"].extend(sheet_result["warnings"])

    mesh_repair = repair_mesh_object_residual_bad_edge_clusters(obj, target="mesh")
    result["local_mesh_repair"] = mesh_repair
    result["faces_removed"] += mesh_repair["faces_removed"]

    geometric_repair = repair_mesh_object_residual_bad_edge_clusters(obj, target="geometric")
    result["local_geometric_repair"] = geometric_repair
    result["faces_removed"] += geometric_repair["faces_removed"]

    topology_after = mesh_topology_counts(obj.data)
    geometric_topology_after = mesh_geometric_topology_counts(obj.data)
    result["geometric_boundary_edge_count_before_hull"] = geometric_topology_after["boundary_edge_count"]
    result["geometric_non_manifold_edge_count_before_hull"] = geometric_topology_after[
        "non_manifold_edge_count"
    ]
    topology_for_hull = topology_after
    if (
        topology_after["boundary_edge_count"] == 0
        and topology_after["non_manifold_edge_count"] == 0
        and (
            geometric_topology_after["boundary_edge_count"] != 0
            or geometric_topology_after["non_manifold_edge_count"] != 0
        )
        and len(obj.data.polygons) <= MESH_CLOSURE_GEOMETRIC_CONVEX_HULL_MAX_FACES
    ):
        topology_for_hull = geometric_topology_after
    hull_result = rebuild_closed_residual_non_manifold_as_convex_hull(
        obj,
        topology_after=topology_for_hull,
        weld_epsilon_mm=weld_epsilon_mm,
    )
    result.update({key: value for key, value in hull_result.items() if key != "warnings"})
    result["warnings"].extend(hull_result["warnings"])
    topology_after = mesh_topology_counts(obj.data)
    geometric_topology_after = mesh_geometric_topology_counts(obj.data)
    result["vertex_count_after"] = len(obj.data.vertices)
    result["face_count_after"] = len(obj.data.polygons)
    result["boundary_edge_count_after"] = topology_after["boundary_edge_count"]
    result["non_manifold_edge_count_after"] = topology_after["non_manifold_edge_count"]
    result["geometric_boundary_edge_count_after"] = geometric_topology_after["boundary_edge_count"]
    result["geometric_non_manifold_edge_count_after"] = geometric_topology_after[
        "non_manifold_edge_count"
    ]
    return result


def solidify_planar_non_manifold_sheet_object(
    obj: bpy.types.Object,
    *,
    topology_after: dict,
    thickness_mm: float,
) -> dict:
    result = {
        "planar_sheet_solidified": False,
        "planar_sheet_solidify_thickness_mm": thickness_mm,
        "planar_sheet_vertex_count_before": 0,
        "planar_sheet_vertex_count_after": 0,
        "planar_sheet_face_count_before": 0,
        "planar_sheet_face_count_after": 0,
        "planar_sheet_bounds_before": None,
        "planar_sheet_bounds_after": None,
        "warnings": [],
    }
    if topology_after["boundary_edge_count"] != 0 or topology_after["non_manifold_edge_count"] == 0:
        return result
    if thickness_mm <= 0:
        result["warnings"].append(f"{obj.name}: planar sheet solidify skipped because thickness is invalid")
        return result

    mesh = obj.data
    if len(mesh.polygons) > MESH_CLOSURE_PLANAR_SHEET_MAX_FACES:
        return result
    sheet_plane = planar_sheet_plane(mesh)
    if sheet_plane is None:
        return result
    center, normal, axis_u, axis_v, max_deviation = sheet_plane
    if max_deviation > MESH_CLOSURE_PLANAR_SHEET_MAX_DEVIATION_MM:
        return result

    hull_points = planar_sheet_convex_hull_points(mesh, axis_u, axis_v)
    if len(hull_points) < 3:
        result["warnings"].append(f"{obj.name}: planar sheet solidify skipped because the sheet hull is degenerate")
        return result

    result["planar_sheet_vertex_count_before"] = len(mesh.vertices)
    result["planar_sheet_face_count_before"] = len(mesh.polygons)
    result["planar_sheet_bounds_before"] = object_bounds(obj)
    materials = list(mesh.materials)
    half_thickness = thickness_mm / 2

    bm = bmesh.new()
    try:
        bottom = [bm.verts.new(point - normal * half_thickness) for point in hull_points]
        top = [bm.verts.new(point + normal * half_thickness) for point in hull_points]
        bm.verts.ensure_lookup_table()

        faces: list[bmesh.types.BMFace] = []
        bottom_face = new_bmesh_poly_face(bm, list(reversed(bottom)))
        top_face = new_bmesh_poly_face(bm, top)
        if bottom_face:
            faces.append(bottom_face)
        if top_face:
            faces.append(top_face)
        for index in range(len(hull_points)):
            next_index = (index + 1) % len(hull_points)
            side_face = new_bmesh_poly_face(
                bm,
                [bottom[index], bottom[next_index], top[next_index], top[index]],
            )
            if side_face:
                faces.append(side_face)
        if len(faces) < len(hull_points) + 2:
            result["warnings"].append(f"{obj.name}: planar sheet solidify skipped because side faces could not be built")
            return result

        bmesh.ops.triangulate(bm, faces=faces)
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        trial_topology = bmesh_topology_counts(bm)
        if trial_topology["boundary_edge_count"] != 0 or trial_topology["non_manifold_edge_count"] != 0:
            result["warnings"].append(
                f"{obj.name}: planar sheet solidify rejected because it produced "
                f"{trial_topology['boundary_edge_count']} boundary edge(s) and "
                f"{trial_topology['non_manifold_edge_count']} non-manifold edge(s)"
            )
            return result

        new_mesh = bpy.data.meshes.new(f"{mesh.name}_planar_sheet_solid")
        bm.to_mesh(new_mesh)
    finally:
        bm.free()

    for material in materials:
        new_mesh.materials.append(material)
    new_mesh.update()
    recalculate_mesh_normals(new_mesh)
    geometric_topology = mesh_geometric_topology_counts(new_mesh)
    if geometric_topology["boundary_edge_count"] != 0 or geometric_topology["non_manifold_edge_count"] != 0:
        bpy.data.meshes.remove(new_mesh)
        result["warnings"].append(
            f"{obj.name}: planar sheet solidify rejected because STL-rounded topology has "
            f"{geometric_topology['boundary_edge_count']} boundary edge(s) and "
            f"{geometric_topology['non_manifold_edge_count']} non-manifold edge(s)"
        )
        return result

    obj.data = new_mesh
    result["planar_sheet_solidified"] = True
    result["planar_sheet_vertex_count_after"] = len(obj.data.vertices)
    result["planar_sheet_face_count_after"] = len(obj.data.polygons)
    result["planar_sheet_bounds_after"] = object_bounds(obj)
    return result


def planar_sheet_plane(mesh: bpy.types.Mesh) -> tuple[Vector, Vector, Vector, Vector, float] | None:
    points = [mesh.vertices[index].co.copy() for index in mesh_surface_vertex_indices(mesh)]
    if len(points) < 3:
        return None
    axis_u = farthest_points_axis(points)
    if axis_u is None:
        return None

    origin = points[0]
    best_normal = Vector((0.0, 0.0, 0.0))
    best_length = 0.0
    for point in points:
        candidate = axis_u.cross(point - origin)
        if candidate.length > best_length:
            best_normal = candidate
            best_length = candidate.length
    if best_length <= 1e-9:
        return None
    normal = best_normal.normalized()
    axis_u = axis_u.normalized()
    axis_v = normal.cross(axis_u)
    if axis_v.length <= 1e-9:
        return None
    axis_v.normalize()

    center = Vector((0.0, 0.0, 0.0))
    for point in points:
        center += point
    center /= len(points)
    max_deviation = max(abs((point - center).dot(normal)) for point in points)
    return center, normal, axis_u, axis_v, max_deviation


def farthest_points_axis(points: list[Vector]) -> Vector | None:
    best_axis = Vector((0.0, 0.0, 0.0))
    best_length_sq = 0.0
    for first_index, first in enumerate(points):
        for second in points[first_index + 1 :]:
            axis = second - first
            length_sq = axis.length_squared
            if length_sq > best_length_sq:
                best_axis = axis
                best_length_sq = length_sq
    if best_length_sq <= 1e-12:
        return None
    return best_axis


def planar_sheet_convex_hull_points(
    mesh: bpy.types.Mesh,
    axis_u: Vector,
    axis_v: Vector,
) -> list[Vector]:
    projected: dict[tuple[int, int], tuple[float, float, Vector]] = {}
    for index in mesh_surface_vertex_indices(mesh):
        point = mesh.vertices[index].co.copy()
        x = float(point.dot(axis_u))
        y = float(point.dot(axis_v))
        key = (
            round(x / GEOMETRIC_CLEANUP_QUANTIZATION_MM),
            round(y / GEOMETRIC_CLEANUP_QUANTIZATION_MM),
        )
        projected.setdefault(key, (x, y, point))
    items = sorted(projected.values(), key=lambda item: (item[0], item[1]))
    if len(items) <= 1:
        return [item[2] for item in items]

    def cross(origin: tuple[float, float, Vector], first: tuple[float, float, Vector], second: tuple[float, float, Vector]) -> float:
        return (first[0] - origin[0]) * (second[1] - origin[1]) - (first[1] - origin[1]) * (second[0] - origin[0])

    lower: list[tuple[float, float, Vector]] = []
    for item in items:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], item) <= 1e-10:
            lower.pop()
        lower.append(item)

    upper: list[tuple[float, float, Vector]] = []
    for item in reversed(items):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], item) <= 1e-10:
            upper.pop()
        upper.append(item)

    hull = lower[:-1] + upper[:-1]
    return [item[2] for item in hull]


def rebuild_closed_residual_non_manifold_as_convex_hull(
    obj: bpy.types.Object,
    *,
    topology_after: dict,
    weld_epsilon_mm: float,
) -> dict:
    result = {
        "convex_hull_rebuilt": False,
        "convex_hull_vertex_count_before": 0,
        "convex_hull_vertex_count_after": 0,
        "convex_hull_face_count_before": 0,
        "convex_hull_face_count_after": 0,
        "convex_hull_bounds_before": None,
        "convex_hull_bounds_after": None,
        "warnings": [],
    }
    if topology_after["boundary_edge_count"] == 0 and topology_after["non_manifold_edge_count"] == 0:
        return result

    mesh = obj.data
    if len(mesh.vertices) < 4:
        result["warnings"].append(f"{obj.name}: convex hull fallback skipped because the mesh has fewer than 4 vertices")
        return result

    result["convex_hull_vertex_count_before"] = len(mesh.vertices)
    result["convex_hull_face_count_before"] = len(mesh.polygons)
    result["convex_hull_bounds_before"] = object_bounds(obj)
    if len(mesh.polygons) > MESH_CLOSURE_CONVEX_HULL_MAX_INPUT_FACES:
        result["warnings"].append(
            f"{obj.name}: convex hull fallback skipped because the mesh has {len(mesh.polygons)} faces; "
            f"max is {MESH_CLOSURE_CONVEX_HULL_MAX_INPUT_FACES}"
        )
        return result
    materials = list(mesh.materials)
    coordinates = [vertex.co.copy() for vertex in mesh.vertices]

    bm = bmesh.new()
    try:
        for coordinate in coordinates:
            bm.verts.new(coordinate)
        bm.verts.ensure_lookup_table()
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=weld_epsilon_mm)
        bm.verts.ensure_lookup_table()
        if len(bm.verts) < 4:
            result["warnings"].append(
                f"{obj.name}: convex hull fallback skipped because weld left fewer than 4 unique vertices"
            )
            return result

        hull_result = bmesh.ops.convex_hull(bm, input=list(bm.verts), use_existing_faces=False)
        delete_geom = unique_valid_bmesh_geom(
            item
            for key in ("geom_interior", "geom_unused", "geom_holes")
            for item in hull_result.get(key, [])
        )
        if delete_geom:
            bmesh.ops.delete(bm, geom=delete_geom, context="VERTS")
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        if not bm.faces:
            result["warnings"].append(f"{obj.name}: convex hull fallback produced no faces")
            return result

        bmesh.ops.triangulate(bm, faces=list(bm.faces))
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        trial_topology = bmesh_topology_counts(bm)
        if trial_topology["boundary_edge_count"] != 0 or trial_topology["non_manifold_edge_count"] != 0:
            result["warnings"].append(
                f"{obj.name}: convex hull fallback rejected because it produced "
                f"{trial_topology['boundary_edge_count']} boundary edge(s) and "
                f"{trial_topology['non_manifold_edge_count']} non-manifold edge(s)"
            )
            return result

        bounds_after = bounds_payload(*bounds_from_points([vert.co for vert in bm.verts]))
        bounds_drift = max_bounds_delta(result["convex_hull_bounds_before"], bounds_after)
        if bounds_drift > MESH_CLOSURE_CONVEX_HULL_MAX_BOUNDS_DRIFT_MM:
            result["warnings"].append(
                f"{obj.name}: convex hull fallback rejected because bounds drifted by {bounds_drift:.5f}mm"
            )
            return result

        new_mesh = bpy.data.meshes.new(f"{mesh.name}_convex_hull")
        bm.to_mesh(new_mesh)
        for material in materials:
            new_mesh.materials.append(material)
        obj.data = new_mesh
        obj.data.update()
        recalculate_mesh_normals(obj.data)
        result["convex_hull_rebuilt"] = True
        result["convex_hull_vertex_count_after"] = len(obj.data.vertices)
        result["convex_hull_face_count_after"] = len(obj.data.polygons)
        result["convex_hull_bounds_after"] = object_bounds(obj)
        return result
    except Exception as exc:
        result["warnings"].append(f"{obj.name}: convex hull fallback failed: {exc}")
        return result
    finally:
        bm.free()


def unique_valid_bmesh_geom(items) -> list:
    result = []
    seen: set[int] = set()
    for item in items:
        if not bmesh_item_is_valid(item):
            continue
        item_id = id(item)
        if item_id in seen:
            continue
        seen.add(item_id)
        result.append(item)
    return result


def bmesh_item_is_valid(item) -> bool:
    try:
        return bool(getattr(item, "is_valid", False))
    except ReferenceError:
        return False


def valid_bmesh_edges(edges: list[bmesh.types.BMEdge]) -> list[bmesh.types.BMEdge]:
    return [edge for edge in edges if bmesh_item_is_valid(edge)]


def max_bounds_delta(before: dict, after: dict) -> float:
    return max(
        abs(float(before[key][index]) - float(after[key][index]))
        for key in ("min", "max")
        for index in range(3)
    )


def clean_non_manifold_artifacts_after_closure(bm: bmesh.types.BMesh) -> dict:
    result = {
        "faces_removed": 0,
        "short_non_manifold_edges_collapsed": 0,
        "loose_edges_removed": 0,
        "overused_faces_removed": 0,
    }
    result["loose_edges_removed"] += remove_loose_edges(bm)
    result["faces_removed"] += remove_fully_overused_faces(bm)
    result["short_non_manifold_edges_collapsed"] += collapse_short_non_manifold_edges(
        bm,
        max_edge_length_mm=MESH_CLOSURE_SHORT_NON_MANIFOLD_EDGE_MM,
    )
    if result["short_non_manifold_edges_collapsed"]:
        bmesh.ops.dissolve_degenerate(bm, edges=list(bm.edges), dist=MESH_CLOSURE_WELD_EPSILON_MM)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        result["faces_removed"] += remove_fully_overused_faces(bm)
    overused_faces_removed = remove_overused_faces_without_opening_boundaries(bm)
    result["overused_faces_removed"] += overused_faces_removed
    result["faces_removed"] += overused_faces_removed
    return result


def repair_mesh_object_residual_bad_edge_clusters(obj: bpy.types.Object, *, target: str) -> dict:
    mesh = obj.data
    result = local_face_repair_fields()
    if not mesh.vertices or not mesh.polygons:
        return result

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        if target == "mesh":
            result = repair_mesh_bad_edge_clusters_with_face_removal(bm)
        elif target == "geometric":
            result = repair_geometric_bad_edge_clusters_with_face_removal(bm)
        else:
            raise ValueError(f"unknown local residual repair target: {target}")
        if local_face_repair_changed(result):
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
            bm.to_mesh(mesh)
    finally:
        bm.free()

    if local_face_repair_changed(result):
        mesh.update()
        recalculate_mesh_normals(mesh)
    return result


def repair_mesh_bad_edge_clusters_with_face_removal(bm: bmesh.types.BMesh) -> dict:
    result = local_face_repair_fields()
    before_counts = bmesh_topology_counts(bm)
    result["boundary_edge_count_before"] = before_counts["boundary_edge_count"]
    result["non_manifold_edge_count_before"] = before_counts["non_manifold_edge_count"]
    result["boundary_edge_count_after"] = before_counts["boundary_edge_count"]
    result["non_manifold_edge_count_after"] = before_counts["non_manifold_edge_count"]
    if before_counts["boundary_edge_count"] == 0 and before_counts["non_manifold_edge_count"] == 0:
        return result

    for _repair_pass in range(LOCAL_FACE_REPAIR_MAX_PASSES):
        current_counts = bmesh_topology_counts(bm)
        if current_counts["boundary_edge_count"] == 0 and current_counts["non_manifold_edge_count"] == 0:
            break
        edge_faces = bmesh_edge_faces_by_index(bm)
        clusters = bad_edge_face_clusters(edge_faces)
        if not clusters:
            break
        result["clusters_considered"] += len(clusters)
        result["candidate_faces"] += sum(len(cluster) for cluster in clusters)
        before_geometric_counts = bmesh_geometric_topology_counts(bm)

        best: tuple[tuple, tuple[int, ...], float] | None = None
        for cluster in clusters:
            if len(cluster) > LOCAL_FACE_REPAIR_MAX_CANDIDATE_FACES:
                result["clusters_skipped_large"] += 1
                continue
            candidate = best_mesh_cluster_face_removal(
                bm,
                cluster,
                before_mesh_counts=current_counts,
                before_geometric_counts=before_geometric_counts,
            )
            if candidate is None:
                continue
            score, removal, area_removed = candidate
            if best is None or score < best[0]:
                best = (score, removal, area_removed)

        if best is None:
            split_nudge = best_mesh_bad_edge_split_nudge(
                bm,
                before_mesh_counts=current_counts,
                before_geometric_counts=before_geometric_counts,
            )
            if split_nudge is None:
                break
            _score, bad_edge_indices, face_indices, direction, vertices_nudged = split_nudge
            if not split_nudge_bad_edge_component(bm, bad_edge_indices, face_indices, direction):
                break
            result["clusters_repaired"] += 1
            result["edges_split"] += len(bad_edge_indices)
            result["vertices_nudged"] += vertices_nudged
            continue

        _score, removal, area_removed = best
        if not delete_bmesh_faces_by_index(bm, removal):
            break
        result["clusters_repaired"] += 1
        result["faces_removed"] += len(removal)
        result["area_removed_mm2"] = round(result["area_removed_mm2"] + area_removed, 6)

    after_counts = bmesh_topology_counts(bm)
    result["boundary_edge_count_after"] = after_counts["boundary_edge_count"]
    result["non_manifold_edge_count_after"] = after_counts["non_manifold_edge_count"]
    return result


def repair_geometric_bad_edge_clusters_with_face_removal(bm: bmesh.types.BMesh) -> dict:
    result = local_face_repair_fields()
    before_counts = bmesh_geometric_topology_counts(bm)
    result["boundary_edge_count_before"] = before_counts["boundary_edge_count"]
    result["non_manifold_edge_count_before"] = before_counts["non_manifold_edge_count"]
    result["boundary_edge_count_after"] = before_counts["boundary_edge_count"]
    result["non_manifold_edge_count_after"] = before_counts["non_manifold_edge_count"]
    if before_counts["boundary_edge_count"] == 0 and before_counts["non_manifold_edge_count"] == 0:
        return result

    for _repair_pass in range(LOCAL_FACE_REPAIR_MAX_PASSES):
        face_edges = bmesh_geometric_face_edges(bm)
        current_counts, edge_faces = geometric_topology_counts_from_face_edges(face_edges)
        if current_counts["boundary_edge_count"] == 0 and current_counts["non_manifold_edge_count"] == 0:
            break
        clusters = bad_edge_face_clusters(edge_faces)
        if not clusters:
            break
        result["clusters_considered"] += len(clusters)
        result["candidate_faces"] += sum(len(cluster) for cluster in clusters)
        before_mesh_counts = bmesh_topology_counts(bm)

        best: tuple[tuple, tuple[int, ...], float] | None = None
        for cluster in clusters:
            if len(cluster) > LOCAL_FACE_REPAIR_MAX_CANDIDATE_FACES:
                result["clusters_skipped_large"] += 1
                continue
            candidate = best_geometric_cluster_face_removal(
                bm,
                cluster,
                face_edges=face_edges,
                before_geometric_counts=current_counts,
                before_mesh_counts=before_mesh_counts,
            )
            if candidate is None:
                continue
            score, removal, area_removed = candidate
            if best is None or score < best[0]:
                best = (score, removal, area_removed)

        if best is None:
            break

        _score, removal, area_removed = best
        if not delete_bmesh_faces_by_index(bm, removal):
            break
        result["clusters_repaired"] += 1
        result["faces_removed"] += len(removal)
        result["area_removed_mm2"] = round(result["area_removed_mm2"] + area_removed, 6)

    after_counts = bmesh_geometric_topology_counts(bm)
    result["boundary_edge_count_after"] = after_counts["boundary_edge_count"]
    result["non_manifold_edge_count_after"] = after_counts["non_manifold_edge_count"]
    return result


def best_mesh_cluster_face_removal(
    bm: bmesh.types.BMesh,
    candidate_faces: list[int],
    *,
    before_mesh_counts: dict[str, int],
    before_geometric_counts: dict[str, int],
) -> tuple[tuple, tuple[int, ...], float] | None:
    candidate_faces = valid_bmesh_face_indices(bm, candidate_faces)
    if not candidate_faces:
        return None
    face_edges = bmesh_geometric_face_edges(bm)
    bad_edge_faces = bad_edge_faces_for_candidate_faces(
        bmesh_edge_faces_by_index(bm),
        candidate_faces,
    )
    candidate_areas = bmesh_face_areas_by_index(bm, candidate_faces)
    max_remove = min(LOCAL_FACE_REPAIR_MAX_REMOVE_FACES, len(candidate_faces))
    best: tuple[tuple, tuple[int, ...], float] | None = None
    combinations_checked = 0
    for remove_count in range(1, max_remove + 1):
        for removal in combinations(candidate_faces, remove_count):
            combinations_checked += 1
            if combinations_checked > LOCAL_FACE_REPAIR_MAX_COMBINATIONS_PER_CLUSTER:
                return best
            if not removal_can_improve_bad_edges(removal, bad_edge_faces):
                continue
            mesh_counts = bmesh_topology_counts_after_face_delete_and_cleanup(bm, removal)
            if mesh_counts is None:
                continue
            if not topology_counts_are_acceptable(
                mesh_counts,
                before_mesh_counts,
                require_improvement=True,
            ):
                continue
            geometric_counts, _edge_faces = geometric_topology_counts_from_face_edges(
                face_edges,
                removed_faces=frozenset(removal),
            )
            if not topology_counts_are_acceptable(
                geometric_counts,
                before_geometric_counts,
                require_improvement=False,
            ):
                continue
            area_removed = sum(candidate_areas[face_index] for face_index in removal)
            score = (
                mesh_counts["boundary_edge_count"],
                mesh_counts["non_manifold_edge_count"],
                geometric_counts["boundary_edge_count"],
                geometric_counts["non_manifold_edge_count"],
                area_removed,
                len(removal),
                removal,
            )
            if best is None or score < best[0]:
                best = (score, removal, area_removed)
        if best is not None:
            return best
    return None


def best_geometric_cluster_face_removal(
    bm: bmesh.types.BMesh,
    candidate_faces: list[int],
    *,
    face_edges: dict[int, tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...]],
    before_geometric_counts: dict[str, int],
    before_mesh_counts: dict[str, int],
) -> tuple[tuple, tuple[int, ...], float] | None:
    candidate_faces = valid_bmesh_face_indices(bm, candidate_faces)
    if not candidate_faces:
        return None
    _current_counts, edge_faces = geometric_topology_counts_from_face_edges(face_edges)
    bad_edge_faces = bad_edge_faces_for_candidate_faces(edge_faces, candidate_faces)
    candidate_areas = bmesh_face_areas_by_index(bm, candidate_faces)
    max_remove = min(LOCAL_FACE_REPAIR_MAX_REMOVE_FACES, len(candidate_faces))
    best: tuple[tuple, tuple[int, ...], float] | None = None
    combinations_checked = 0
    for remove_count in range(1, max_remove + 1):
        for removal in combinations(candidate_faces, remove_count):
            combinations_checked += 1
            if combinations_checked > LOCAL_FACE_REPAIR_MAX_COMBINATIONS_PER_CLUSTER:
                return best
            if not removal_can_improve_bad_edges(removal, bad_edge_faces):
                continue
            geometric_counts, _edge_faces = geometric_topology_counts_from_face_edges(
                face_edges,
                removed_faces=frozenset(removal),
            )
            if not topology_counts_are_acceptable(
                geometric_counts,
                before_geometric_counts,
                require_improvement=True,
            ):
                continue
            mesh_counts = bmesh_topology_counts_after_face_delete_and_cleanup(bm, removal)
            if mesh_counts is None:
                continue
            if not topology_counts_are_acceptable(
                mesh_counts,
                before_mesh_counts,
                require_improvement=False,
            ):
                continue
            area_removed = sum(candidate_areas[face_index] for face_index in removal)
            score = (
                geometric_counts["boundary_edge_count"],
                geometric_counts["non_manifold_edge_count"],
                mesh_counts["boundary_edge_count"],
                mesh_counts["non_manifold_edge_count"],
                area_removed,
                len(removal),
                removal,
            )
            if best is None or score < best[0]:
                best = (score, removal, area_removed)
        if best is not None:
            return best
    return None


def bad_edge_faces_for_candidate_faces(edge_faces: dict, candidate_faces: list[int]) -> list[set[int]]:
    candidate_face_set = set(candidate_faces)
    return [
        set(faces)
        for faces in edge_faces.values()
        if len(faces) > 2 and faces.intersection(candidate_face_set)
    ]


def removal_can_improve_bad_edges(removal: tuple[int, ...], bad_edge_faces: list[set[int]]) -> bool:
    removal_set = set(removal)
    improved = False
    for faces in bad_edge_faces:
        removed_count = len(faces.intersection(removal_set))
        if removed_count == 0:
            continue
        remaining_count = len(faces) - removed_count
        if remaining_count < 2:
            return False
        if remaining_count == 2:
            improved = True
    return improved


def best_mesh_bad_edge_split_nudge(
    bm: bmesh.types.BMesh,
    *,
    before_mesh_counts: dict[str, int],
    before_geometric_counts: dict[str, int],
) -> tuple[tuple, tuple[int, ...], tuple[int, ...], Vector, int] | None:
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.edges.index_update()
    bm.faces.index_update()
    bad_edge_indices = tuple(sorted(edge.index for edge in bm.edges if edge.is_valid and len(edge.link_faces) > 2))
    if not bad_edge_indices:
        return None

    components = mesh_face_components_excluding_edges(bm, set(bad_edge_indices))
    if not components:
        return None
    mesh_center = bmesh_vertices_center(list(bm.verts))
    faces_by_index = {face.index: face for face in bm.faces if face.is_valid}

    best: tuple[tuple, tuple[int, ...], tuple[int, ...], Vector, int] | None = None
    for face_indices in components:
        if len(face_indices) == len(faces_by_index):
            continue
        component_faces = [faces_by_index[index] for index in face_indices if index in faces_by_index]
        if not component_faces:
            continue
        component_verts = unique_faces_vertices(component_faces)
        if not component_verts or len(component_verts) == len(bm.verts):
            continue
        directions = component_nudge_directions(component_faces, component_verts, mesh_center)
        for direction_index, direction in enumerate(directions):
            trial_counts = topology_counts_after_split_nudge(
                bm,
                bad_edge_indices,
                face_indices,
                direction,
            )
            if trial_counts is None:
                continue
            mesh_counts = trial_counts["mesh"]
            geometric_counts = trial_counts["geometric"]
            if not topology_counts_are_acceptable(
                mesh_counts,
                before_mesh_counts,
                require_improvement=True,
            ):
                continue
            if not topology_counts_are_acceptable(
                geometric_counts,
                before_geometric_counts,
                require_improvement=False,
            ):
                continue
            score = (
                mesh_counts["boundary_edge_count"],
                mesh_counts["non_manifold_edge_count"],
                geometric_counts["boundary_edge_count"],
                geometric_counts["non_manifold_edge_count"],
                len(component_verts),
                len(face_indices),
                direction_index,
                face_indices,
            )
            if best is None or score < best[0]:
                best = (score, bad_edge_indices, face_indices, direction, len(component_verts))
    return best


def mesh_face_components_excluding_edges(
    bm: bmesh.types.BMesh,
    blocked_edge_indices: set[int],
) -> list[tuple[int, ...]]:
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.edges.index_update()
    bm.faces.index_update()

    seed_face_indices = {
        face.index
        for edge in bm.edges
        if edge.is_valid and edge.index in blocked_edge_indices
        for face in edge.link_faces
        if face.is_valid
    }
    components: list[tuple[int, ...]] = []
    seen_components: set[tuple[int, ...]] = set()
    for seed_index in sorted(seed_face_indices):
        if seed_index >= len(bm.faces) or not bm.faces[seed_index].is_valid:
            continue
        stack = [bm.faces[seed_index]]
        component: set[int] = set()
        while stack:
            face = stack.pop()
            if not face.is_valid or face.index in component:
                continue
            component.add(face.index)
            for edge in face.edges:
                if not edge.is_valid or edge.index in blocked_edge_indices:
                    continue
                for neighbor in edge.link_faces:
                    if neighbor.is_valid and neighbor.index not in component:
                        stack.append(neighbor)
        key = tuple(sorted(component))
        if key and key not in seen_components:
            seen_components.add(key)
            components.append(key)
    components.sort(key=lambda item: (len(item), item))
    return components


def topology_counts_after_split_nudge(
    bm: bmesh.types.BMesh,
    bad_edge_indices: tuple[int, ...],
    face_indices: tuple[int, ...],
    direction: Vector,
) -> dict[str, dict[str, int]] | None:
    trial = bm.copy()
    try:
        if not split_nudge_bad_edge_component(trial, bad_edge_indices, face_indices, direction):
            return None
        return {
            "mesh": bmesh_topology_counts(trial),
            "geometric": bmesh_geometric_topology_counts(trial),
        }
    finally:
        trial.free()


def split_nudge_bad_edge_component(
    bm: bmesh.types.BMesh,
    bad_edge_indices: tuple[int, ...],
    face_indices: tuple[int, ...],
    direction: Vector,
) -> bool:
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    if any(edge_index >= len(bm.edges) or not bm.edges[edge_index].is_valid for edge_index in bad_edge_indices):
        return False
    if any(face_index >= len(bm.faces) or not bm.faces[face_index].is_valid for face_index in face_indices):
        return False

    edges_to_split = [bm.edges[edge_index] for edge_index in bad_edge_indices]
    bmesh.ops.split_edges(bm, edges=edges_to_split)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    faces_to_nudge = [bm.faces[face_index] for face_index in face_indices if face_index < len(bm.faces)]
    if not faces_to_nudge or any(not face.is_valid for face in faces_to_nudge):
        return False
    verts_to_nudge = unique_faces_vertices(faces_to_nudge)
    if not verts_to_nudge:
        return False
    offset = direction.normalized() * GEOMETRIC_CLEANUP_COMPONENT_NUDGE_MM
    for vert in verts_to_nudge:
        vert.co += offset

    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=MESH_CLOSURE_WELD_EPSILON_MM)
    bmesh.ops.dissolve_degenerate(bm, edges=list(bm.edges), dist=MESH_CLOSURE_WELD_EPSILON_MM)
    remove_loose_edges(bm)
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.index_update()
    bm.faces.index_update()
    return True


def topology_counts_are_acceptable(
    after: dict[str, int],
    before: dict[str, int],
    *,
    require_improvement: bool,
) -> bool:
    if after["boundary_edge_count"] > before["boundary_edge_count"]:
        return False
    if after["non_manifold_edge_count"] > before["non_manifold_edge_count"]:
        return False
    if require_improvement and (
        after["boundary_edge_count"] == before["boundary_edge_count"]
        and after["non_manifold_edge_count"] >= before["non_manifold_edge_count"]
    ):
        return False
    return True


def valid_bmesh_face_indices(bm: bmesh.types.BMesh, face_indices: list[int]) -> list[int]:
    bm.faces.ensure_lookup_table()
    return sorted(
        {
            face_index
            for face_index in face_indices
            if 0 <= face_index < len(bm.faces) and bm.faces[face_index].is_valid
        }
    )


def bmesh_face_areas_by_index(bm: bmesh.types.BMesh, face_indices: list[int]) -> dict[int, float]:
    bm.faces.ensure_lookup_table()
    return {face_index: bmesh_face_area(bm.faces[face_index]) for face_index in face_indices}


def bmesh_topology_counts_after_face_delete_and_cleanup(
    bm: bmesh.types.BMesh,
    face_indices: tuple[int, ...],
) -> dict[str, int] | None:
    trial = bm.copy()
    try:
        if not delete_bmesh_faces_by_index(trial, face_indices, recalc_normals=False):
            return None
        return bmesh_topology_counts(trial)
    finally:
        trial.free()


def delete_bmesh_faces_by_index(
    bm: bmesh.types.BMesh,
    face_indices: tuple[int, ...],
    *,
    recalc_normals: bool = True,
) -> bool:
    bm.faces.ensure_lookup_table()
    if any(face_index >= len(bm.faces) or not bm.faces[face_index].is_valid for face_index in face_indices):
        return False
    faces_to_remove = [bm.faces[face_index] for face_index in face_indices]
    bmesh.ops.delete(bm, geom=faces_to_remove, context="FACES_ONLY")
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    remove_loose_edges(bm)
    if recalc_normals:
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    return True


def bmesh_edge_faces_by_index(bm: bmesh.types.BMesh) -> dict[int, set[int]]:
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.edges.index_update()
    bm.faces.index_update()
    return {
        edge.index: {face.index for face in edge.link_faces if face.is_valid}
        for edge in bm.edges
        if edge.is_valid
    }


def bad_edge_face_clusters(edge_faces: dict) -> list[list[int]]:
    bad_edges = {
        edge_key: set(faces)
        for edge_key, faces in edge_faces.items()
        if len(faces) > 2
    }
    if not bad_edges:
        return []

    face_to_edges: dict[int, set] = {}
    for edge_key, faces in bad_edges.items():
        for face_index in faces:
            face_to_edges.setdefault(face_index, set()).add(edge_key)

    clusters: list[list[int]] = []
    remaining = set(bad_edges)
    while remaining:
        first = remaining.pop()
        cluster_faces = set(bad_edges[first])
        stack = [first]
        while stack:
            edge_key = stack.pop()
            for face_index in bad_edges[edge_key]:
                for neighbor in face_to_edges.get(face_index, set()):
                    if neighbor not in remaining:
                        continue
                    remaining.remove(neighbor)
                    cluster_faces.update(bad_edges[neighbor])
                    stack.append(neighbor)
        clusters.append(sorted(cluster_faces))
    clusters.sort(key=lambda cluster: (len(cluster), cluster))
    return clusters


def bmesh_geometric_topology_counts(bm: bmesh.types.BMesh) -> dict[str, int]:
    return geometric_topology_counts_from_face_edges(bmesh_geometric_face_edges(bm))[0]


def bmesh_geometric_face_edges(
    bm: bmesh.types.BMesh,
) -> dict[int, tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...]]:
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    return {
        face.index: tuple(
            geometric_edge_key(face.verts[index].co, face.verts[(index + 1) % len(face.verts)].co)
            for index in range(len(face.verts))
        )
        for face in bm.faces
        if face.is_valid and len(face.verts) >= 3
    }


def remove_loose_edges(bm: bmesh.types.BMesh) -> int:
    bm.edges.ensure_lookup_table()
    loose_edges = [edge for edge in bm.edges if edge.is_valid and len(edge.link_faces) == 0]
    if not loose_edges:
        return 0
    bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    return len(loose_edges)


def remove_fully_overused_faces(bm: bmesh.types.BMesh) -> int:
    removed = 0
    while True:
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        candidates = [
            face
            for face in bm.faces
            if face.is_valid and face.edges and all(len(edge.link_faces) > 2 for edge in face.edges)
        ]
        if not candidates:
            return removed
        candidates.sort(key=bmesh_face_area)
        face = candidates[0]
        bmesh.ops.delete(bm, geom=[face], context="FACES_ONLY")
        removed += 1


def collapse_short_non_manifold_edges(
    bm: bmesh.types.BMesh,
    *,
    max_edge_length_mm: float,
) -> int:
    collapsed = 0
    while True:
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        candidate = next(
            (
                edge
                for edge in bm.edges
                if edge.is_valid
                and len(edge.link_faces) > 2
                and (edge.verts[0].co - edge.verts[1].co).length <= max_edge_length_mm
            ),
            None,
        )
        if candidate is None:
            return collapsed
        merge_co = (candidate.verts[0].co + candidate.verts[1].co) / 2
        bmesh.ops.pointmerge(bm, verts=list(candidate.verts), merge_co=merge_co)
        collapsed += 1


def remove_overused_faces_without_opening_boundaries(bm: bmesh.types.BMesh) -> int:
    removed = 0
    while removed < MESH_CLOSURE_OVERUSED_FACE_REMOVAL_LIMIT:
        before = bmesh_topology_counts(bm)
        if before["non_manifold_edge_count"] == 0:
            return removed
        candidates = best_overused_face_removal_candidates(bm, before)
        if not candidates:
            return removed
        bmesh.ops.delete(bm, geom=candidates, context="FACES_ONLY")
        removed += len(candidates)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
    return removed


def best_overused_face_removal_candidates(
    bm: bmesh.types.BMesh,
    before: dict[str, int],
) -> list[bmesh.types.BMFace]:
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    candidates: dict[tuple[int, ...], list[bmesh.types.BMFace]] = {}
    for edge in bm.edges:
        if not edge.is_valid or len(edge.link_faces) <= 2:
            continue
        faces = [face for face in edge.link_faces if face.is_valid]
        for face_count in range(1, min(len(faces) - 1, 2) + 1):
            for face_group in combinations(faces, face_count):
                key = tuple(sorted(face.index for face in face_group))
                candidates.setdefault(key, list(face_group))
    face_by_index = {
        face.index: face
        for edge in bm.edges
        if edge.is_valid and len(edge.link_faces) > 2
        for face in edge.link_faces
        if face.is_valid
    }
    for face in face_by_index.values():
        candidates.setdefault((face.index,), [face])
    best: tuple[int, int, float, int] | None = None
    best_faces: list[bmesh.types.BMFace] = []
    for face_indices, faces in candidates.items():
        after = bmesh_topology_counts_after_face_delete(bm, face_indices)
        if after is None:
            continue
        if after["boundary_edge_count"] > before["boundary_edge_count"]:
            continue
        if after["non_manifold_edge_count"] >= before["non_manifold_edge_count"]:
            continue
        score = (
            after["boundary_edge_count"],
            after["non_manifold_edge_count"],
            sum(bmesh_face_area(face) for face in faces),
            sum(face_indices),
        )
        if best is None or score < best:
            best = score
            best_faces = faces
    return best_faces


def bmesh_topology_counts_after_face_delete(
    bm: bmesh.types.BMesh,
    face_indices: tuple[int, ...],
) -> dict[str, int] | None:
    trial = bm.copy()
    try:
        trial.faces.ensure_lookup_table()
        if any(face_index >= len(trial.faces) for face_index in face_indices):
            return None
        faces = [trial.faces[face_index] for face_index in face_indices]
        if any(not face.is_valid for face in faces):
            return None
        bmesh.ops.delete(trial, geom=faces, context="FACES_ONLY")
        return bmesh_topology_counts(trial)
    finally:
        trial.free()


def bmesh_topology_counts(bm: bmesh.types.BMesh) -> dict[str, int]:
    bm.edges.ensure_lookup_table()
    edge_face_counts = [len(edge.link_faces) for edge in bm.edges if edge.is_valid]
    return {
        "boundary_edge_count": sum(1 for count in edge_face_counts if count == 1),
        "non_manifold_edge_count": sum(1 for count in edge_face_counts if count != 2),
    }


def bmesh_face_area(face: bmesh.types.BMFace) -> float:
    return sum(
        triangle_area(face.verts[0].co, face.verts[index].co, face.verts[index + 1].co)
        for index in range(1, len(face.verts) - 1)
    )


def triangle_area(first: Vector, second: Vector, third: Vector) -> float:
    return ((second - first).cross(third - first)).length / 2


def boundary_edge_components(bm: bmesh.types.BMesh) -> list[list[bmesh.types.BMEdge]]:
    bm.verts.index_update()
    bm.edges.index_update()
    boundary_edges = [edge for edge in bm.edges if edge.is_boundary]
    adjacency: dict[int, list[bmesh.types.BMEdge]] = {}
    for edge in boundary_edges:
        for vert in edge.verts:
            adjacency.setdefault(vert.index, []).append(edge)

    components: list[list[bmesh.types.BMEdge]] = []
    remaining = {edge.index: edge for edge in boundary_edges}
    while remaining:
        _, first = remaining.popitem()
        component = [first]
        stack = [first]
        while stack:
            edge = stack.pop()
            for vert in edge.verts:
                for neighbor in adjacency.get(vert.index, []):
                    neighbor_id = neighbor.index
                    if neighbor_id not in remaining:
                        continue
                    remaining.pop(neighbor_id)
                    component.append(neighbor)
                    stack.append(neighbor)
        components.append(component)
    return components


def boundary_component_degrees(component: list[bmesh.types.BMEdge]) -> dict[int, int]:
    degrees: dict[int, int] = {}
    for edge in valid_bmesh_edges(component):
        for vert in edge.verts:
            vertex_id = vert.index
            degrees[vertex_id] = degrees.get(vertex_id, 0) + 1
    return degrees


def boundary_component_is_closed(component: list[bmesh.types.BMEdge]) -> bool:
    degrees = boundary_component_degrees(component)
    return bool(component) and all(degree == 2 for degree in degrees.values())


def boundary_component_fillable_loops(component: list[bmesh.types.BMEdge]) -> list[list[bmesh.types.BMEdge]]:
    if boundary_component_is_closed(component):
        return [component]
    return simple_boundary_cycles(component)


def simple_boundary_cycles(component: list[bmesh.types.BMEdge]) -> list[list[bmesh.types.BMEdge]]:
    component = valid_bmesh_edges(component)
    if not component:
        return []
    adjacency, _verts_by_index = boundary_component_adjacency(component)
    cycles: dict[tuple[int, ...], list[bmesh.types.BMEdge]] = {}
    max_depth = len(component)

    def walk(
        start_vertex: int,
        current_vertex: int,
        path_vertices: list[int],
        path_edges: list[bmesh.types.BMEdge],
        used_edges: set[int],
    ) -> None:
        if len(path_edges) > max_depth:
            return
        for next_vertex, edge in adjacency.get(current_vertex, []):
            edge_id = edge.index
            if edge_id in used_edges:
                continue
            if next_vertex == start_vertex:
                cycle_edges = path_edges + [edge]
                if len(cycle_edges) >= 3:
                    key = tuple(sorted(item.index for item in cycle_edges))
                    cycles.setdefault(key, cycle_edges)
                continue
            if next_vertex in path_vertices:
                continue
            walk(
                start_vertex,
                next_vertex,
                path_vertices + [next_vertex],
                path_edges + [edge],
                used_edges | {edge_id},
            )

    for start_vertex in sorted(adjacency):
        for next_vertex, edge in sorted(adjacency[start_vertex], key=lambda item: (item[0], item[1].index)):
            walk(start_vertex, next_vertex, [start_vertex, next_vertex], [edge], {edge.index})

    selected: list[list[bmesh.types.BMEdge]] = []
    covered_edges: set[int] = set()
    for cycle in sorted(cycles.values(), key=lambda item: (len(item), [edge.index for edge in item])):
        cycle_edges = {edge.index for edge in cycle}
        if cycle_edges & covered_edges:
            continue
        selected.append(cycle)
        covered_edges.update(cycle_edges)
    return selected


def boundary_component_adjacency(
    component: list[bmesh.types.BMEdge],
) -> tuple[dict[int, list[tuple[int, bmesh.types.BMEdge]]], dict[int, bmesh.types.BMVert]]:
    adjacency: dict[int, list[tuple[int, bmesh.types.BMEdge]]] = {}
    verts_by_index: dict[int, bmesh.types.BMVert] = {}
    for edge in valid_bmesh_edges(component):
        first, second = edge.verts
        verts_by_index[first.index] = first
        verts_by_index[second.index] = second
        adjacency.setdefault(first.index, []).append((second.index, edge))
        adjacency.setdefault(second.index, []).append((first.index, edge))
    return adjacency, verts_by_index


def fill_boundary_component(
    bm: bmesh.types.BMesh,
    component: list[bmesh.types.BMEdge],
) -> tuple[list[bmesh.types.BMFace], str | None]:
    component = valid_bmesh_edges(component)
    if len(component) < 3:
        return [], None
    bm.faces.index_update()
    faces_before = {face.index for face in bm.faces}
    try:
        fill_result = bmesh.ops.holes_fill(bm, edges=list(component), sides=0)
    except Exception:
        fill_result = {}
    filled_faces = filled_faces_from_result(bm, faces_before, fill_result)
    if filled_faces:
        return filled_faces, "holes_fill"
    filled_faces = planar_cap_boundary_component(bm, component)
    if filled_faces:
        return filled_faces, "planar_cap"
    filled_faces = fan_cap_boundary_component(bm, component)
    if filled_faces:
        return filled_faces, "fan_cap"
    return [], None


def filled_faces_from_result(
    bm: bmesh.types.BMesh,
    faces_before: set[int],
    fill_result: dict,
) -> list[bmesh.types.BMFace]:
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    result_faces = [
        item
        for key in ("faces", "geom")
        for item in fill_result.get(key, [])
        if isinstance(item, bmesh.types.BMFace) and item.is_valid
    ]
    if result_faces:
        return result_faces
    return [face for face in bm.faces if face.index not in faces_before and face.is_valid]


def planar_cap_boundary_component(
    bm: bmesh.types.BMesh,
    component: list[bmesh.types.BMEdge],
) -> list[bmesh.types.BMFace]:
    ordered_vertices = ordered_boundary_loop_vertices(component)
    if len(ordered_vertices) < 3:
        return []

    center, normal, max_deviation = boundary_loop_plane(ordered_vertices)
    if normal.length <= 0 or max_deviation > MESH_CLOSURE_PLANAR_CAP_MAX_DEVIATION_MM:
        return []
    return fan_cap_ordered_vertices(bm, ordered_vertices, center, normal)


def fan_cap_boundary_component(
    bm: bmesh.types.BMesh,
    component: list[bmesh.types.BMEdge],
) -> list[bmesh.types.BMFace]:
    if len(component) > MESH_CLOSURE_FAN_CAP_MAX_EDGES:
        return []
    ordered_vertices = ordered_boundary_loop_vertices(component)
    if len(ordered_vertices) < 3:
        return []

    center, normal, _max_deviation = boundary_loop_plane(ordered_vertices)
    if normal.length <= 0:
        return []
    return fan_cap_ordered_vertices(bm, ordered_vertices, center, normal)


def fan_cap_ordered_vertices(
    bm: bmesh.types.BMesh,
    ordered_vertices: list[bmesh.types.BMVert],
    center: Vector,
    normal: Vector,
) -> list[bmesh.types.BMFace]:
    center_vert = bm.verts.new(center)

    faces: list[bmesh.types.BMFace] = []
    for index, first in enumerate(ordered_vertices):
        second = ordered_vertices[(index + 1) % len(ordered_vertices)]
        triangle_normal = (second.co - first.co).cross(center_vert.co - first.co)
        verts = (first, second, center_vert)
        if triangle_normal.length > 0 and triangle_normal.dot(normal) < 0:
            verts = (second, first, center_vert)
        face = new_bmesh_face(bm, verts)
        if face is not None:
            faces.append(face)
    if not faces:
        bm.verts.remove(center_vert)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    return faces


def cap_open_boundary_chain_with_existing_edge(
    bm: bmesh.types.BMesh,
    component: list[bmesh.types.BMEdge],
) -> list[bmesh.types.BMFace]:
    component = valid_bmesh_edges(component)
    if len(component) < 2:
        return []

    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    ordered_vertices = ordered_open_boundary_chain_vertices(component)
    if len(ordered_vertices) < 3:
        return []

    endpoint_edge = bmesh_edge_between(ordered_vertices[0], ordered_vertices[-1])
    if endpoint_edge is None or endpoint_edge in component or len(endpoint_edge.link_faces) == 0:
        return []

    before = bmesh_topology_counts(bm)
    if not open_boundary_chain_cap_improves_topology(bm, ordered_vertices, before):
        return []

    face = new_bmesh_poly_face(bm, ordered_vertices)
    if face is None:
        return []
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    return [face]


def ordered_open_boundary_chain_vertices(component: list[bmesh.types.BMEdge]) -> list[bmesh.types.BMVert]:
    degrees = boundary_component_degrees(component)
    endpoint_ids = [vertex_id for vertex_id, degree in degrees.items() if degree == 1]
    if len(endpoint_ids) != 2 or any(degree not in {1, 2} for degree in degrees.values()):
        return []

    adjacency, verts_by_index = boundary_component_adjacency(component)
    start_vertex = min(endpoint_ids)
    previous_vertex: int | None = None
    current_vertex = start_vertex
    seen_vertices: set[int] = set()
    ordered: list[bmesh.types.BMVert] = []

    while True:
        if current_vertex in seen_vertices:
            return []
        seen_vertices.add(current_vertex)
        ordered.append(verts_by_index[current_vertex])
        candidates = [
            next_vertex
            for next_vertex, _edge in sorted(adjacency[current_vertex], key=lambda item: item[0])
            if next_vertex != previous_vertex
        ]
        if not candidates:
            if current_vertex not in endpoint_ids:
                return []
            return ordered if len(ordered) == len(adjacency) else []
        if len(candidates) > 1:
            return []
        previous_vertex, current_vertex = current_vertex, candidates[0]


def bmesh_edge_between(
    first: bmesh.types.BMVert,
    second: bmesh.types.BMVert,
) -> bmesh.types.BMEdge | None:
    first_edges = set(first.link_edges)
    for edge in second.link_edges:
        if edge in first_edges:
            return edge
    return None


def open_boundary_chain_cap_improves_topology(
    bm: bmesh.types.BMesh,
    verts: list[bmesh.types.BMVert],
    before: dict[str, int],
) -> bool:
    trial = bm.copy()
    try:
        trial.verts.ensure_lookup_table()
        trial_verts = [trial.verts[vert.index] for vert in verts]
        try:
            trial.faces.new(tuple(trial_verts))
        except ValueError:
            try:
                trial.faces.new(tuple(reversed(trial_verts)))
            except ValueError:
                return False
        trial.edges.ensure_lookup_table()
        trial.faces.ensure_lookup_table()
        after = bmesh_topology_counts(trial)
        if after["boundary_edge_count"] >= before["boundary_edge_count"]:
            return False
        if after["non_manifold_edge_count"] > before["non_manifold_edge_count"]:
            return False
        return True
    finally:
        trial.free()


def repair_small_open_boundary_component(
    bm: bmesh.types.BMesh,
    component: list[bmesh.types.BMEdge],
    *,
    max_span_mm: float,
) -> bool:
    component = valid_bmesh_edges(component)
    if len(component) > 2:
        return False

    verts = unique_component_vertices(component)
    if len(verts) < 2:
        return False
    if component_span_mm(verts) > max_span_mm:
        return False

    merge_co = Vector((0.0, 0.0, 0.0))
    for vert in verts:
        merge_co += vert.co
    merge_co /= len(verts)

    bmesh.ops.pointmerge(bm, verts=verts, merge_co=merge_co)
    bmesh.ops.dissolve_degenerate(bm, edges=list(bm.edges), dist=MESH_CLOSURE_WELD_EPSILON_MM)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    return True


def unique_component_vertices(component: list[bmesh.types.BMEdge]) -> list[bmesh.types.BMVert]:
    result: list[bmesh.types.BMVert] = []
    seen: set[int] = set()
    for edge in valid_bmesh_edges(component):
        for vert in edge.verts:
            vert_id = id(vert)
            if vert_id in seen:
                continue
            seen.add(vert_id)
            result.append(vert)
    return result


def component_span_mm(verts: list[bmesh.types.BMVert]) -> float:
    span = 0.0
    for first_index, first in enumerate(verts):
        for second in verts[first_index + 1 :]:
            span = max(span, (first.co - second.co).length)
    return span


def ordered_boundary_loop_vertices(component: list[bmesh.types.BMEdge]) -> list[bmesh.types.BMVert]:
    if not boundary_component_is_closed(component):
        return []
    adjacency, verts_by_index = boundary_component_adjacency(component)
    start_vertex = min(adjacency)
    previous_vertex: int | None = None
    current_vertex = start_vertex
    seen_vertices: set[int] = set()
    ordered: list[bmesh.types.BMVert] = []

    while True:
        if current_vertex in seen_vertices:
            return []
        seen_vertices.add(current_vertex)
        ordered.append(verts_by_index[current_vertex])
        candidates = [
            next_vertex
            for next_vertex, _edge in sorted(adjacency[current_vertex], key=lambda item: item[0])
            if next_vertex != previous_vertex
        ]
        if not candidates:
            return []
        next_vertex = candidates[0]
        if next_vertex == start_vertex:
            return ordered if len(ordered) == len(adjacency) else []
        previous_vertex, current_vertex = current_vertex, next_vertex


def boundary_loop_plane(ordered_vertices: list[bmesh.types.BMVert]) -> tuple[Vector, Vector, float]:
    center = Vector((0.0, 0.0, 0.0))
    for vert in ordered_vertices:
        center += vert.co
    center /= len(ordered_vertices)

    normal = newell_loop_normal([vert.co for vert in ordered_vertices])
    if normal.length <= 0:
        normal = fallback_loop_normal([vert.co for vert in ordered_vertices])
    if normal.length <= 0:
        return center, normal, float("inf")
    normal.normalize()
    max_deviation = max(abs((vert.co - center).dot(normal)) for vert in ordered_vertices)
    return center, normal, max_deviation


def newell_loop_normal(points: list[Vector]) -> Vector:
    normal = Vector((0.0, 0.0, 0.0))
    for index, point in enumerate(points):
        next_point = points[(index + 1) % len(points)]
        normal.x += (point.y - next_point.y) * (point.z + next_point.z)
        normal.y += (point.z - next_point.z) * (point.x + next_point.x)
        normal.z += (point.x - next_point.x) * (point.y + next_point.y)
    return normal


def fallback_loop_normal(points: list[Vector]) -> Vector:
    for first_index, first in enumerate(points):
        for second_index, second in enumerate(points):
            if second_index == first_index:
                continue
            for third_index, third in enumerate(points):
                if third_index in {first_index, second_index}:
                    continue
                normal = (second - first).cross(third - first)
                if normal.length > 0:
                    return normal
    return Vector((0.0, 0.0, 0.0))


def new_bmesh_face(
    bm: bmesh.types.BMesh,
    verts: tuple[bmesh.types.BMVert, bmesh.types.BMVert, bmesh.types.BMVert],
) -> bmesh.types.BMFace | None:
    try:
        return bm.faces.new(verts)
    except ValueError:
        try:
            return bm.faces.new((verts[1], verts[0], verts[2]))
        except ValueError:
            return None


def new_bmesh_poly_face(
    bm: bmesh.types.BMesh,
    verts: list[bmesh.types.BMVert],
) -> bmesh.types.BMFace | None:
    if len(verts) < 3:
        return None
    try:
        return bm.faces.new(tuple(verts))
    except ValueError:
        try:
            return bm.faces.new(tuple(reversed(verts)))
        except ValueError:
            return None


def boundary_component_warning(obj_name: str, component: list[bmesh.types.BMEdge], reason: str) -> str:
    component = valid_bmesh_edges(component)
    degrees = boundary_component_degrees(component)
    degree_values = sorted(set(degrees.values()))
    span = component_span_mm(unique_component_vertices(component))
    return (
        f"{obj_name}: could not fill boundary component with {len(component)} edge(s), "
        f"{len(degrees)} vertex/vertices, boundary degrees {degree_values}, "
        f"span {span:.5f}mm: {reason}"
    )


def append_boundary_warning(
    result: dict,
    obj_name: str,
    component: list[bmesh.types.BMEdge],
    reason: str,
) -> None:
    warning = boundary_component_warning(obj_name, component, reason)
    if warning not in result["warnings"]:
        result["warnings"].append(warning)


def apply_thin_features(profile: dict) -> dict:
    config = dict(profile.get("thin_features") or {})
    min_thickness_mm = float(config.get("min_thickness_mm", 0.8))
    max_inflate_mm = float(config.get("max_inflate_mm", min_thickness_mm / 2))
    ray_epsilon_mm = float(config.get("ray_epsilon_mm", 0.005))
    result = {
        "enabled": bool(config.get("enabled", True)),
        "mode": "raycast_local_thickness",
        "min_thickness_mm": min_thickness_mm,
        "max_inflate_mm": max_inflate_mm,
        "ray_epsilon_mm": ray_epsilon_mm,
        "objects_checked": 0,
        "vertices_checked": 0,
        "vertices_moved": 0,
        "thin_components": 0,
        "small_planar_components_solidified": 0,
        "small_planar_component_faces_replaced": 0,
        "small_planar_component_faces_after": 0,
        "max_inflate_applied_mm": 0.0,
        "bounds_before": scene_bounds(),
        "bounds_after": None,
        "objects": [],
    }
    if not result["enabled"]:
        result["bounds_after"] = result["bounds_before"]
        return result
    if min_thickness_mm <= 0:
        raise RuntimeError("thin_features.min_thickness_mm must be greater than zero")
    if max_inflate_mm <= 0:
        raise RuntimeError("thin_features.max_inflate_mm must be greater than zero")
    if ray_epsilon_mm <= 0:
        raise RuntimeError("thin_features.ray_epsilon_mm must be greater than zero")

    for obj in mesh_objects():
        object_result = thicken_mesh_object_thin_features(
            obj,
            min_thickness_mm=min_thickness_mm,
            max_inflate_mm=max_inflate_mm,
            ray_epsilon_mm=ray_epsilon_mm,
        )
        result["objects_checked"] += 1
        result["vertices_checked"] += object_result["vertices_checked"]
        result["vertices_moved"] += object_result["vertices_moved"]
        result["thin_components"] += object_result["thin_components"]
        result["small_planar_components_solidified"] += object_result["small_planar_components_solidified"]
        result["small_planar_component_faces_replaced"] += object_result["small_planar_component_faces_replaced"]
        result["small_planar_component_faces_after"] += object_result["small_planar_component_faces_after"]
        result["max_inflate_applied_mm"] = max(
            result["max_inflate_applied_mm"],
            object_result["max_inflate_applied_mm"],
        )
        if (
            object_result["vertices_moved"]
            or object_result["thin_components"]
            or object_result["small_planar_components_solidified"]
        ):
            result["objects"].append(object_result)
    result["bounds_after"] = scene_bounds()
    return result


def thicken_mesh_object_thin_features(
    obj: bpy.types.Object,
    *,
    min_thickness_mm: float,
    max_inflate_mm: float,
    ray_epsilon_mm: float,
) -> dict:
    mesh = obj.data
    result = {
        "name": obj.name,
        "vertices_checked": 0,
        "vertices_moved": 0,
        "ray_hits": 0,
        "thin_components": 0,
        "small_planar_components_solidified": 0,
        "small_planar_component_faces_replaced": 0,
        "small_planar_component_faces_after": 0,
        "max_inflate_applied_mm": 0.0,
    }
    if not mesh.vertices or not mesh.polygons:
        return result

    recalculate_mesh_normals(mesh)
    vertices = [vertex.co.copy() for vertex in mesh.vertices]
    polygons = [tuple(polygon.vertices) for polygon in mesh.polygons]
    bvh = BVHTree.FromPolygons(vertices, polygons)
    inflate_by_vertex = [0.0] * len(mesh.vertices)

    for vertex in mesh.vertices:
        normal = vertex.normal.copy()
        if normal.length <= 0:
            continue
        normal.normalize()
        result["vertices_checked"] += 1
        thickness = probe_local_thickness(
            mesh,
            bvh,
            vertex.index,
            normal,
            max_distance_mm=min_thickness_mm,
            ray_epsilon_mm=ray_epsilon_mm,
        )
        if thickness is None:
            continue
        result["ray_hits"] += 1
        if thickness >= min_thickness_mm:
            continue
        inflate_by_vertex[vertex.index] = max(
            inflate_by_vertex[vertex.index],
            min(max_inflate_mm, (min_thickness_mm - thickness) / 2),
        )

    result["thin_components"] = inflate_loose_thin_components(
        mesh,
        inflate_by_vertex,
        min_thickness_mm=min_thickness_mm,
        max_inflate_mm=max_inflate_mm,
    )

    for vertex in mesh.vertices:
        inflate = inflate_by_vertex[vertex.index]
        if inflate <= 0:
            continue
        normal = vertex.normal.copy()
        if normal.length <= 0:
            continue
        normal.normalize()
        vertex.co += normal * inflate
        result["vertices_moved"] += 1
        result["max_inflate_applied_mm"] = max(result["max_inflate_applied_mm"], inflate)

    if result["vertices_moved"]:
        mesh.update()

    planar_result = solidify_small_planar_thin_components(
        mesh,
        min_thickness_mm=min_thickness_mm,
        max_inflate_mm=max_inflate_mm,
    )
    result["small_planar_components_solidified"] = planar_result["components_solidified"]
    result["small_planar_component_faces_replaced"] = planar_result["faces_replaced"]
    result["small_planar_component_faces_after"] = planar_result["faces_after"]
    if planar_result["components_solidified"]:
        mesh.update()
        recalculate_mesh_normals(mesh)
    return result


def probe_local_thickness(
    mesh: bpy.types.Mesh,
    bvh: BVHTree,
    vertex_index: int,
    normal: Vector,
    *,
    max_distance_mm: float,
    ray_epsilon_mm: float,
) -> float | None:
    vertex = mesh.vertices[vertex_index]
    direction = -normal
    origin = vertex.co + normal * ray_epsilon_mm
    remaining = max_distance_mm + ray_epsilon_mm * 4
    traveled = 0.0
    min_usable_hit = ray_epsilon_mm * 2.5

    while remaining > ray_epsilon_mm:
        location, _hit_normal, face_index, distance = bvh.ray_cast(origin, direction, remaining)
        if location is None or face_index is None or distance is None:
            return None
        traveled += distance
        if vertex_index not in mesh.polygons[face_index].vertices and traveled > min_usable_hit:
            return max(0.0, traveled - ray_epsilon_mm)
        step = ray_epsilon_mm
        origin = location + direction * step
        traveled += step
        remaining -= distance + step
    return None


def inflate_loose_thin_components(
    mesh: bpy.types.Mesh,
    inflate_by_vertex: list[float],
    *,
    min_thickness_mm: float,
    max_inflate_mm: float,
) -> int:
    thin_components = 0
    for component in mesh_loose_components(mesh):
        if len(component) < 12:
            continue
        dimensions = sorted(component_dimensions(mesh, component))
        thin_axes = sum(1 for dimension in dimensions if dimension < min_thickness_mm)
        if thin_axes < 2:
            continue
        shortest = dimensions[0]
        if shortest >= min_thickness_mm:
            continue
        longest = dimensions[-1]
        if longest <= 0:
            continue
        inflate = min(max_inflate_mm, (min_thickness_mm - shortest) / 2)
        if inflate <= 0:
            continue
        for vertex_index in component:
            inflate_by_vertex[vertex_index] = max(inflate_by_vertex[vertex_index], inflate)
        thin_components += 1
    return thin_components


def solidify_small_planar_thin_components(
    mesh: bpy.types.Mesh,
    *,
    min_thickness_mm: float,
    max_inflate_mm: float,
) -> dict:
    result = {
        "components_solidified": 0,
        "faces_replaced": 0,
        "faces_after": 0,
    }
    if min_thickness_mm <= 0 or max_inflate_mm <= 0:
        return result

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        candidates: list[tuple[list[bmesh.types.BMFace], list[bmesh.types.BMVert], dict]] = []
        for faces in all_face_components(bm):
            candidate = small_planar_component_prism_candidate(
                faces,
                min_thickness_mm=min_thickness_mm,
                max_inflate_mm=max_inflate_mm,
            )
            if candidate is None:
                continue
            candidates.append(candidate)

        if not candidates:
            return result

        for faces, verts, prism in candidates:
            valid_faces = [face for face in faces if face.is_valid]
            if not valid_faces:
                continue
            material_index = valid_faces[0].material_index
            result["faces_replaced"] += len(valid_faces)
            bmesh.ops.delete(bm, geom=valid_faces, context="FACES_ONLY")
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

            loose_verts = [vert for vert in verts if vert.is_valid and len(vert.link_faces) == 0]
            if loose_verts:
                bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()

            new_faces = add_prism_to_bmesh(bm, prism, material_index=material_index)
            result["faces_after"] += len(new_faces)
            result["components_solidified"] += 1

        remove_loose_edges(bm)
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.to_mesh(mesh)
    finally:
        bm.free()
    return result


def small_planar_component_prism_candidate(
    faces: list[bmesh.types.BMFace],
    *,
    min_thickness_mm: float,
    max_inflate_mm: float,
) -> tuple[list[bmesh.types.BMFace], list[bmesh.types.BMVert], dict] | None:
    faces = [face for face in faces if face.is_valid]
    if not faces or len(faces) > 8:
        return None
    verts = unique_faces_vertices(faces)
    if len(verts) < 4 or len(verts) > 8:
        return None
    points = [vert.co.copy() for vert in verts]
    frame = planar_component_frame(points)
    if frame is None:
        return None
    center, length_axis, width_axis, normal, length_span, width_span, max_deviation = frame
    if max_deviation > MESH_CLOSURE_PLANAR_SHEET_MAX_DEVIATION_MM:
        return None
    if length_span <= 0 or length_span > min_thickness_mm * 12:
        return None
    if width_span >= min_thickness_mm and max_deviation >= min_thickness_mm * 0.5:
        return None
    if width_span <= 0 and max_deviation <= 0:
        return None

    final_width = expanded_feature_span(width_span, min_thickness_mm, max_inflate_mm)
    final_thickness = expanded_feature_span(max_deviation, min_thickness_mm, max_inflate_mm)
    if final_width <= width_span and final_thickness <= max_deviation:
        return None
    if final_width <= 0 or final_thickness <= 0:
        return None
    return (
        faces,
        verts,
        {
            "center": center,
            "length_axis": length_axis,
            "width_axis": width_axis,
            "normal": normal,
            "length": length_span,
            "width": final_width,
            "thickness": final_thickness,
        },
    )


def planar_component_frame(
    points: list[Vector],
) -> tuple[Vector, Vector, Vector, Vector, float, float, float] | None:
    if len(points) < 4:
        return None
    length_axis = farthest_points_axis(points)
    if length_axis is None or length_axis.length <= 0:
        return None
    length_axis.normalize()

    origin = points[0]
    best_normal = Vector((0.0, 0.0, 0.0))
    best_length = 0.0
    for point in points:
        candidate = length_axis.cross(point - origin)
        if candidate.length > best_length:
            best_normal = candidate
            best_length = candidate.length
    if best_length <= 1e-9:
        return None
    normal = best_normal.normalized()
    width_axis = normal.cross(length_axis)
    if width_axis.length <= 1e-9:
        return None
    width_axis.normalize()

    center = Vector((0.0, 0.0, 0.0))
    for point in points:
        center += point
    center /= len(points)
    length_values = [float((point - center).dot(length_axis)) for point in points]
    width_values = [float((point - center).dot(width_axis)) for point in points]
    normal_values = [float((point - center).dot(normal)) for point in points]
    length_span = max(length_values) - min(length_values)
    width_span = max(width_values) - min(width_values)
    normal_span = max(normal_values) - min(normal_values)
    return center, length_axis, width_axis, normal, length_span, width_span, normal_span


def expanded_feature_span(current_span: float, target_span: float, max_inflate_mm: float) -> float:
    if current_span >= target_span:
        return current_span
    return current_span + min(max_inflate_mm * 2, target_span - current_span)


def add_prism_to_bmesh(
    bm: bmesh.types.BMesh,
    prism: dict,
    *,
    material_index: int,
) -> list[bmesh.types.BMFace]:
    center = prism["center"]
    length_axis = prism["length_axis"]
    width_axis = prism["width_axis"]
    normal = prism["normal"]
    half_length = prism["length"] / 2
    half_width = prism["width"] / 2
    half_thickness = prism["thickness"] / 2

    coordinates = []
    for normal_sign in (-1, 1):
        for width_sign in (-1, 1):
            for length_sign in (-1, 1):
                coordinates.append(
                    center
                    + length_axis * (half_length * length_sign)
                    + width_axis * (half_width * width_sign)
                    + normal * (half_thickness * normal_sign)
                )
    verts = [bm.verts.new(coordinate) for coordinate in coordinates]
    bm.verts.ensure_lookup_table()

    face_indices = (
        (0, 1, 3, 2),
        (4, 6, 7, 5),
        (0, 4, 5, 1),
        (2, 3, 7, 6),
        (0, 2, 6, 4),
        (1, 5, 7, 3),
    )
    faces: list[bmesh.types.BMFace] = []
    for indices in face_indices:
        face = new_bmesh_poly_face(bm, [verts[index] for index in indices])
        if face is None:
            continue
        face.material_index = material_index
        faces.append(face)
    return faces


def mesh_loose_components(mesh: bpy.types.Mesh) -> list[list[int]]:
    adjacency = [[] for _ in mesh.vertices]
    for edge in mesh.edges:
        first, second = edge.vertices
        adjacency[first].append(second)
        adjacency[second].append(first)

    components: list[list[int]] = []
    seen = [False] * len(mesh.vertices)
    for start in range(len(mesh.vertices)):
        if seen[start]:
            continue
        stack = [start]
        seen[start] = True
        component: list[int] = []
        while stack:
            index = stack.pop()
            component.append(index)
            for neighbor in adjacency[index]:
                if seen[neighbor]:
                    continue
                seen[neighbor] = True
                stack.append(neighbor)
        components.append(component)
    return components


def component_dimensions(mesh: bpy.types.Mesh, component: list[int]) -> tuple[float, float, float]:
    coordinates = [mesh.vertices[index].co for index in component]
    return (
        max(co.x for co in coordinates) - min(co.x for co in coordinates),
        max(co.y for co in coordinates) - min(co.y for co in coordinates),
        max(co.z for co in coordinates) - min(co.z for co in coordinates),
    )


def recalculate_mesh_normals(mesh: bpy.types.Mesh) -> None:
    calc_normals = getattr(mesh, "calc_normals", None)
    if callable(calc_normals):
        calc_normals()
    else:
        mesh.update()


def apply_visual_transforms() -> None:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    baked: list[tuple[bpy.types.Object, bpy.types.Mesh]] = []
    for obj in list(mesh_objects()):
        world_matrix = obj.matrix_world.copy()
        materials = list(obj.data.materials)
        evaluated = obj.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(evaluated, depsgraph=depsgraph)
        if materials and len(mesh.materials) == 0:
            for material in materials:
                mesh.materials.append(material)
        mesh.transform(world_matrix)
        mesh.update()
        baked.append((obj, mesh))

    for obj, mesh in baked:
        obj.parent = None
        obj.data = mesh
        obj.matrix_world = Matrix.Identity(4)
    bpy.context.view_layer.update()


def apply_glb_print_axis_correction() -> None:
    corrected: list[tuple[bpy.types.Object, bpy.types.Mesh]] = []
    for obj in list(mesh_objects()):
        world_matrix = obj.matrix_world.copy()
        mesh = obj.data.copy()
        for vertex in mesh.vertices:
            vertex.co = Vector(glb_imported_to_print_point(world_matrix @ vertex.co))
        mesh.update()
        recalculate_mesh_normals(mesh)
        corrected.append((obj, mesh))

    for obj, mesh in corrected:
        obj.parent = None
        obj.data = mesh
        obj.matrix_world = Matrix.Identity(4)
    bpy.context.view_layer.update()


def scale_to_target_height(target_mm: float) -> None:
    objects = mesh_objects()
    min_v, max_v = bounds(objects)
    height = max_v.z - min_v.z
    if height <= 0:
        raise RuntimeError("Cannot scale model: imported mesh has zero height.")
    factor = target_mm / height
    for obj in objects:
        obj.scale = (obj.scale.x * factor, obj.scale.y * factor, obj.scale.z * factor)
    apply_all_mesh_transforms()


def move_to_ground() -> None:
    objects = mesh_objects()
    min_v, max_v = bounds(objects)
    center_x = (min_v.x + max_v.x) / 2
    center_y = (min_v.y + max_v.y) / 2
    for obj in objects:
        obj.location.x -= center_x
        obj.location.y -= center_y
        obj.location.z -= min_v.z
    apply_all_mesh_transforms()


def join_meshes() -> bpy.types.Object:
    objects = mesh_objects()
    if not objects:
        raise RuntimeError("Model imported but no meshes found.")
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.object.join()
    joined = bpy.context.view_layer.objects.active
    joined.name = "barprint_model"
    return joined


def add_base(model: bpy.types.Object, diameter_mm: float, height_mm: float) -> bpy.types.Object:
    min_v, _ = bounds([model])
    bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=diameter_mm / 2, depth=height_mm, location=(0, 0, height_mm / 2))
    base = bpy.context.object
    base.name = "barprint_base"
    model.location.z += height_mm - min_v.z
    apply_all_mesh_transforms()
    bpy.ops.object.select_all(action="DESELECT")
    model.select_set(True)
    base.select_set(True)
    bpy.context.view_layer.objects.active = model
    bpy.ops.object.join()
    joined = bpy.context.view_layer.objects.active
    joined.name = "barprint_model_with_base"
    return joined


def clean_joined_geometric_overlaps(obj: bpy.types.Object) -> dict:
    result = {
        "mode": "remove_smallest_face_set_for_position_edge_manifold",
        "quantization_mm": GEOMETRIC_CLEANUP_QUANTIZATION_MM,
        "max_candidate_faces": GEOMETRIC_CLEANUP_MAX_CANDIDATE_FACES,
        "max_remove_faces": GEOMETRIC_CLEANUP_MAX_REMOVE_FACES,
        "component_nudge_mm": GEOMETRIC_CLEANUP_COMPONENT_NUDGE_MM,
        "max_nudge_passes": GEOMETRIC_CLEANUP_MAX_NUDGE_PASSES,
        "boundary_edge_count_before": 0,
        "non_manifold_edge_count_before": 0,
        "boundary_edge_count_after": 0,
        "non_manifold_edge_count_after": 0,
        "candidate_faces": 0,
        "faces_removed": 0,
        "topology_faces_removed": 0,
        "topology_overused_faces_removed": 0,
        "topology_cleanup_boundary_edge_count_after": 0,
        "topology_cleanup_non_manifold_edge_count_after": 0,
        "duplicate_components_removed": 0,
        "duplicate_component_faces_removed": 0,
        "duplicate_component_vertices_removed": 0,
        "component_hull_max_faces": GEOMETRIC_CLEANUP_COMPONENT_HULL_MAX_FACES,
        "component_hull_candidate_components": 0,
        "component_hull_trials": 0,
        "component_hull_rejected_mesh_topology": 0,
        "component_hull_rejected_boundary": 0,
        "component_hull_rejected_not_improved": 0,
        "component_hull_best_mesh_non_manifold_after": None,
        "component_hull_best_geometric_non_manifold_after": None,
        "component_hulls_rebuilt": 0,
        "component_hull_faces_replaced": 0,
        "component_hull_faces_after": 0,
        "components_nudged": 0,
        "vertices_nudged": 0,
        **local_face_repair_manifest_fields("local_geometric_repair"),
        "area_removed_mm2": 0.0,
        "triangulated": False,
        "mesh_topology_before": mesh_topology_counts(obj.data),
        "mesh_topology_after": None,
        "warnings": [],
    }
    mesh = obj.data
    if not mesh.vertices or not mesh.polygons:
        return finalize_joined_geometric_cleanup_result(result, mesh)

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        if any(len(face.verts) != 3 for face in bm.faces):
            bmesh.ops.triangulate(bm, faces=list(bm.faces))
            result["triangulated"] = True
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            topology_cleanup_result = clean_post_join_triangulation_topology_artifacts(bm)
            result["topology_faces_removed"] += topology_cleanup_result["faces_removed"]
            result["topology_overused_faces_removed"] += topology_cleanup_result["overused_faces_removed"]
            result["faces_removed"] += topology_cleanup_result["faces_removed"]
            if topology_cleanup_result["faces_removed"]:
                bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
            topology_after_cleanup = bmesh_topology_counts(bm)
            result["topology_cleanup_boundary_edge_count_after"] = topology_after_cleanup["boundary_edge_count"]
            result["topology_cleanup_non_manifold_edge_count_after"] = topology_after_cleanup[
                "non_manifold_edge_count"
            ]

        duplicate_result = remove_duplicate_geometric_components(bm)
        result["duplicate_components_removed"] += duplicate_result["components_removed"]
        result["duplicate_component_faces_removed"] += duplicate_result["faces_removed"]
        result["duplicate_component_vertices_removed"] += duplicate_result["vertices_removed"]
        result["faces_removed"] += duplicate_result["faces_removed"]
        if duplicate_result["faces_removed"]:
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

        after_counts = {"boundary_edge_count": 0, "non_manifold_edge_count": 0}
        for cleanup_pass in range(GEOMETRIC_CLEANUP_MAX_NUDGE_PASSES + 1):
            face_vertices = geometric_face_vertices(bm)
            current_counts, edge_faces = geometric_topology_counts_from_faces(face_vertices)
            if cleanup_pass == 0:
                result["boundary_edge_count_before"] = current_counts["boundary_edge_count"]
                result["non_manifold_edge_count_before"] = current_counts["non_manifold_edge_count"]
                result["boundary_edge_count_after"] = current_counts["boundary_edge_count"]
                result["non_manifold_edge_count_after"] = current_counts["non_manifold_edge_count"]
            after_counts = current_counts
            if current_counts["boundary_edge_count"] == 0 and current_counts["non_manifold_edge_count"] == 0:
                break

            candidate_faces = sorted(
                {
                    face_index
                    for faces in edge_faces.values()
                    if len(faces) > 2
                    for face_index in faces
                }
            )
            if cleanup_pass == 0:
                result["candidate_faces"] = len(candidate_faces)
            if not candidate_faces:
                result["warnings"].append("geometric cleanup stopped because no overused position edges were found")
                break

            local_repair = repair_geometric_bad_edge_clusters_with_face_removal(bm)
            merge_local_face_repair_manifest_fields(result, "local_geometric_repair", local_repair)
            if local_repair["faces_removed"]:
                result["faces_removed"] += local_repair["faces_removed"]
                result["area_removed_mm2"] = round(
                    result["area_removed_mm2"] + local_repair["area_removed_mm2"],
                    6,
                )
                continue

            removal: tuple[int, ...] = ()
            if len(candidate_faces) <= GEOMETRIC_CLEANUP_MAX_CANDIDATE_FACES:
                removal = best_geometric_face_removal(face_vertices, candidate_faces)
            if removal:
                bm.faces.ensure_lookup_table()
                faces_to_remove = [bm.faces[face_index] for face_index in removal]
                result["faces_removed"] += len(faces_to_remove)
                result["area_removed_mm2"] = round(
                    result["area_removed_mm2"] + sum(bmesh_face_area(face) for face in faces_to_remove),
                    6,
                )
                bmesh.ops.delete(bm, geom=faces_to_remove, context="FACES_ONLY")
                remove_loose_edges(bm)
                bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
                continue

            nudge_result = nudge_geometric_candidate_component(bm, candidate_faces, current_counts)
            if nudge_result["accepted"]:
                result["components_nudged"] += 1
                result["vertices_nudged"] += nudge_result["vertices_nudged"]
                continue

            hull_result = rebuild_geometric_candidate_component_as_convex_hull(
                bm,
                candidate_faces,
                current_counts,
            )
            result["component_hull_candidate_components"] += hull_result["candidate_components"]
            result["component_hull_trials"] += hull_result["trials"]
            result["component_hull_rejected_mesh_topology"] += hull_result["rejected_mesh_topology"]
            result["component_hull_rejected_boundary"] += hull_result["rejected_boundary"]
            result["component_hull_rejected_not_improved"] += hull_result["rejected_not_improved"]
            merge_optional_min(
                result,
                "component_hull_best_mesh_non_manifold_after",
                hull_result["best_mesh_non_manifold_after"],
            )
            merge_optional_min(
                result,
                "component_hull_best_geometric_non_manifold_after",
                hull_result["best_geometric_non_manifold_after"],
            )
            if hull_result["accepted"]:
                result["component_hulls_rebuilt"] += 1
                result["component_hull_faces_replaced"] += hull_result["faces_replaced"]
                result["component_hull_faces_after"] += hull_result["faces_after"]
                continue

            result["warnings"].append("geometric cleanup found no bounded face-removal or component-nudge solution")
            break

        final_face_vertices = geometric_face_vertices(bm)
        after_counts, _edge_faces = geometric_topology_counts_from_faces(final_face_vertices)
        if after_counts["boundary_edge_count"] != 0 or after_counts["non_manifold_edge_count"] != 0:
            result["warnings"].append(
                "geometric cleanup stopped with "
                f"{after_counts['boundary_edge_count']} boundary and "
                f"{after_counts['non_manifold_edge_count']} non-manifold position edge(s)"
            )
        result["boundary_edge_count_after"] = after_counts["boundary_edge_count"]
        result["non_manifold_edge_count_after"] = after_counts["non_manifold_edge_count"]
        bm.to_mesh(mesh)
    finally:
        bm.free()

    mesh.update()
    recalculate_mesh_normals(mesh)
    return finalize_joined_geometric_cleanup_result(result, mesh)


def finalize_joined_geometric_cleanup_result(result: dict, mesh: bpy.types.Mesh) -> dict:
    result["mesh_topology_after"] = mesh_topology_counts(mesh)
    if result["warnings"]:
        WARNINGS.extend(f"Post-join cleanup: {warning}" for warning in result["warnings"])
    return result


def clean_post_join_triangulation_topology_artifacts(bm: bmesh.types.BMesh) -> dict:
    result = {
        "faces_removed": 0,
        "overused_faces_removed": 0,
    }
    if bmesh_topology_counts(bm)["non_manifold_edge_count"] == 0:
        return result
    cleanup = clean_non_manifold_artifacts_after_closure(bm)
    result["faces_removed"] += cleanup["faces_removed"]
    result["overused_faces_removed"] += cleanup["overused_faces_removed"]
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    return result


def remove_duplicate_geometric_components(bm: bmesh.types.BMesh) -> dict:
    result = {
        "components_removed": 0,
        "faces_removed": 0,
        "vertices_removed": 0,
    }
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.faces.index_update()

    components = all_face_components(bm)
    components_by_signature: dict[tuple, list[list[bmesh.types.BMFace]]] = {}
    for component in components:
        if not component or not face_component_is_closed(component):
            continue
        signature = geometric_component_signature(component)
        if signature is None:
            continue
        components_by_signature.setdefault(signature, []).append(component)

    faces_to_delete: list[bmesh.types.BMFace] = []
    verts_to_delete: list[bmesh.types.BMVert] = []
    for duplicate_components in components_by_signature.values():
        if len(duplicate_components) <= 1:
            continue
        duplicate_components.sort(key=lambda faces: min(face.index for face in faces))
        for component in duplicate_components[1:]:
            faces_to_delete.extend(component)
            verts_to_delete.extend(unique_faces_vertices(component))
            result["components_removed"] += 1

    if not faces_to_delete:
        return result

    deleted_face_count = len(faces_to_delete)
    bmesh.ops.delete(bm, geom=faces_to_delete, context="FACES_ONLY")
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    loose_duplicate_verts = [
        vert
        for vert in verts_to_delete
        if vert.is_valid and len(vert.link_faces) == 0
    ]
    result["vertices_removed"] = len(loose_duplicate_verts)
    if loose_duplicate_verts:
        bmesh.ops.delete(bm, geom=loose_duplicate_verts, context="VERTS")
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
    remove_loose_edges(bm)
    result["faces_removed"] = deleted_face_count
    return result


def all_face_components(bm: bmesh.types.BMesh) -> list[list[bmesh.types.BMFace]]:
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    components: list[list[bmesh.types.BMFace]] = []
    seen: set[int] = set()
    for face in bm.faces:
        if not face.is_valid or face.index in seen:
            continue
        component = connected_face_component(face)
        seen.update(item.index for item in component)
        components.append(component)
    return components


def face_component_is_closed(faces: list[bmesh.types.BMFace]) -> bool:
    face_ids = {id(face) for face in faces if face.is_valid}
    edges = {
        edge
        for face in faces
        if face.is_valid
        for edge in face.edges
        if edge.is_valid
    }
    return bool(edges) and all(
        sum(1 for linked_face in edge.link_faces if id(linked_face) in face_ids) == 2
        for edge in edges
    )


def geometric_component_signature(faces: list[bmesh.types.BMFace]) -> tuple | None:
    signature = []
    for face in faces:
        if not face.is_valid or len(face.verts) != 3:
            return None
        signature.append(
            tuple(
                sorted(
                    quantized_vector(vert.co, GEOMETRIC_CLEANUP_QUANTIZATION_MM)
                    for vert in face.verts
                )
            )
        )
    return tuple(sorted(signature))


def optional_min(current: int | float | None, candidate: int | float | None) -> int | float | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return min(current, candidate)


def merge_optional_min(target: dict, key: str, candidate: int | float | None) -> None:
    target[key] = optional_min(target.get(key), candidate)


def nudge_geometric_candidate_component(
    bm: bmesh.types.BMesh,
    candidate_faces: list[int],
    before_counts: dict[str, int],
) -> dict:
    result = {
        "accepted": False,
        "vertices_nudged": 0,
    }
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.faces.index_update()

    components = candidate_face_components(bm, candidate_faces)
    if not components:
        return result
    mesh_center = bmesh_vertices_center(list(bm.verts))

    best: tuple[int, int, int, int, tuple[int, ...], Vector] | None = None
    for component_faces in components:
        component_face_indices = tuple(sorted(face.index for face in component_faces))
        component_verts = unique_faces_vertices(component_faces)
        if not component_verts or len(component_verts) == len(bm.verts):
            continue
        directions = component_nudge_directions(component_faces, component_verts, mesh_center)
        for direction_index, direction in enumerate(directions):
            trial_counts = topology_counts_after_component_nudge(bm, component_verts, direction)
            if trial_counts["boundary_edge_count"] != 0:
                continue
            if trial_counts["non_manifold_edge_count"] >= before_counts["non_manifold_edge_count"]:
                continue
            score = (
                trial_counts["non_manifold_edge_count"],
                len(component_verts),
                len(component_faces),
                direction_index,
                component_face_indices,
                direction,
            )
            if best is None or score[:5] < best[:5]:
                best = score

    if best is None:
        return result

    _non_manifold, _vert_count, _face_count, _direction_index, face_indices, direction = best
    faces_by_index = {face.index: face for face in bm.faces}
    component_faces = [faces_by_index[index] for index in face_indices if index in faces_by_index]
    verts = unique_faces_vertices(component_faces)
    for vert in verts:
        vert.co += direction * GEOMETRIC_CLEANUP_COMPONENT_NUDGE_MM
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    result["accepted"] = True
    result["vertices_nudged"] = len(verts)
    return result


def rebuild_geometric_candidate_component_as_convex_hull(
    bm: bmesh.types.BMesh,
    candidate_faces: list[int],
    before_counts: dict[str, int],
) -> dict:
    result = {
        "accepted": False,
        "faces_replaced": 0,
        "faces_after": 0,
        "candidate_components": 0,
        "trials": 0,
        "rejected_mesh_topology": 0,
        "rejected_boundary": 0,
        "rejected_not_improved": 0,
        "best_mesh_non_manifold_after": None,
        "best_geometric_non_manifold_after": None,
    }
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.faces.index_update()

    components = candidate_face_components(bm, candidate_faces)
    result["candidate_components"] = len(components)
    if not components:
        return result

    before_mesh_counts = bmesh_topology_counts(bm)
    best_score: tuple[int, int, int, int, tuple[int, ...]] | None = None
    best_payload: tuple[tuple[int, ...], tuple[int, ...], dict] | None = None
    for component_faces in components:
        if len(component_faces) > GEOMETRIC_CLEANUP_COMPONENT_HULL_MAX_FACES:
            continue
        if len(component_faces) == len(bm.faces):
            continue
        face_indices = tuple(sorted(face.index for face in component_faces))
        component_verts = unique_faces_vertices(component_faces)
        if len(component_verts) < 4 or len(component_verts) == len(bm.verts):
            continue
        vert_indices = tuple(sorted(vert.index for vert in component_verts))
        hull_data = convex_hull_mesh_data_from_points([vert.co.copy() for vert in component_verts])
        if hull_data is None:
            continue

        trial_counts = topology_counts_after_component_hull_rebuild(
            bm,
            face_indices,
            vert_indices,
            hull_data,
        )
        if trial_counts is None:
            continue
        result["trials"] += 1
        mesh_counts = trial_counts["mesh"]
        geometric_counts = trial_counts["geometric"]
        result["best_mesh_non_manifold_after"] = optional_min(
            result["best_mesh_non_manifold_after"],
            mesh_counts["non_manifold_edge_count"],
        )
        result["best_geometric_non_manifold_after"] = optional_min(
            result["best_geometric_non_manifold_after"],
            geometric_counts["non_manifold_edge_count"],
        )
        if (
            mesh_counts["boundary_edge_count"] != 0
            or mesh_counts["non_manifold_edge_count"] > before_mesh_counts["non_manifold_edge_count"]
        ):
            result["rejected_mesh_topology"] += 1
            continue
        if geometric_counts["boundary_edge_count"] != 0:
            result["rejected_boundary"] += 1
            continue
        if geometric_counts["non_manifold_edge_count"] >= before_counts["non_manifold_edge_count"]:
            result["rejected_not_improved"] += 1
            continue

        score = (
            geometric_counts["non_manifold_edge_count"],
            mesh_counts["non_manifold_edge_count"],
            len(hull_data["faces"]),
            len(face_indices),
            face_indices,
        )
        if best_score is None or score < best_score:
            best_score = score
            best_payload = (face_indices, vert_indices, hull_data)

    if best_payload is None:
        return result

    face_indices, vert_indices, hull_data = best_payload
    replace_bmesh_component_with_hull(bm, face_indices, vert_indices, hull_data)
    result["accepted"] = True
    result["faces_replaced"] = len(face_indices)
    result["faces_after"] = len(hull_data["faces"])
    return result


def topology_counts_after_component_hull_rebuild(
    bm: bmesh.types.BMesh,
    face_indices: tuple[int, ...],
    vert_indices: tuple[int, ...],
    hull_data: dict,
) -> dict[str, dict[str, int]] | None:
    trial = bm.copy()
    try:
        if not replace_bmesh_component_with_hull(trial, face_indices, vert_indices, hull_data):
            return None
        return {
            "mesh": bmesh_topology_counts(trial),
            "geometric": geometric_topology_counts_from_faces(geometric_face_vertices(trial))[0],
        }
    finally:
        trial.free()


def replace_bmesh_component_with_hull(
    bm: bmesh.types.BMesh,
    face_indices: tuple[int, ...],
    vert_indices: tuple[int, ...],
    hull_data: dict,
) -> bool:
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    if any(face_index >= len(bm.faces) or not bm.faces[face_index].is_valid for face_index in face_indices):
        return False

    faces_to_delete = [bm.faces[face_index] for face_index in face_indices]
    bmesh.ops.delete(bm, geom=faces_to_delete, context="FACES_ONLY")
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    old_loose_verts = [
        bm.verts[vert_index]
        for vert_index in vert_indices
        if vert_index < len(bm.verts)
        and bm.verts[vert_index].is_valid
        and len(bm.verts[vert_index].link_faces) == 0
    ]
    if old_loose_verts:
        bmesh.ops.delete(bm, geom=old_loose_verts, context="VERTS")
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
    remove_loose_edges(bm)

    new_verts = [bm.verts.new(co.copy()) for co in hull_data["vertices"]]
    bm.verts.ensure_lookup_table()
    for face_indices_after in hull_data["faces"]:
        bm.faces.new(tuple(new_verts[index] for index in face_indices_after))
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    return True


def convex_hull_mesh_data_from_points(points: list[Vector]) -> dict | None:
    if len(points) < 4:
        return None
    bm = bmesh.new()
    try:
        for point in points:
            bm.verts.new(point.copy())
        bm.verts.ensure_lookup_table()
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=MESH_CLOSURE_WELD_EPSILON_MM)
        bm.verts.ensure_lookup_table()
        if len(bm.verts) < 4:
            return None

        hull_result = bmesh.ops.convex_hull(bm, input=list(bm.verts), use_existing_faces=False)
        delete_geom = unique_valid_bmesh_geom(
            item
            for key in ("geom_interior", "geom_unused", "geom_holes")
            for item in hull_result.get(key, [])
        )
        if delete_geom:
            bmesh.ops.delete(bm, geom=delete_geom, context="VERTS")
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        if not bm.faces:
            return None

        bmesh.ops.triangulate(bm, faces=list(bm.faces))
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        mesh_counts = bmesh_topology_counts(bm)
        if mesh_counts["boundary_edge_count"] != 0 or mesh_counts["non_manifold_edge_count"] != 0:
            return None
        geometric_counts = geometric_topology_counts_from_faces(geometric_face_vertices(bm))[0]
        if geometric_counts["boundary_edge_count"] != 0 or geometric_counts["non_manifold_edge_count"] != 0:
            return None

        bm.verts.index_update()
        vertices = [vert.co.copy() for vert in bm.verts]
        faces = [
            tuple(vert.index for vert in face.verts)
            for face in bm.faces
            if face.is_valid and len(face.verts) == 3
        ]
        if not faces:
            return None
        return {
            "vertices": vertices,
            "faces": faces,
        }
    finally:
        bm.free()


def candidate_face_components(
    bm: bmesh.types.BMesh,
    candidate_faces: list[int],
) -> list[list[bmesh.types.BMFace]]:
    bm.faces.ensure_lookup_table()
    seen: set[tuple[int, ...]] = set()
    components: list[list[bmesh.types.BMFace]] = []
    for face_index in candidate_faces:
        if face_index >= len(bm.faces):
            continue
        face = bm.faces[face_index]
        if not face.is_valid:
            continue
        component = connected_face_component(face)
        key = tuple(sorted(item.index for item in component))
        if key in seen:
            continue
        seen.add(key)
        components.append(component)
    components.sort(key=lambda faces: (len(unique_faces_vertices(faces)), len(faces), [face.index for face in faces]))
    return components


def connected_face_component(start_face: bmesh.types.BMFace) -> list[bmesh.types.BMFace]:
    component: list[bmesh.types.BMFace] = []
    seen: set[int] = set()
    stack = [start_face]
    while stack:
        face = stack.pop()
        if not face.is_valid or face.index in seen:
            continue
        seen.add(face.index)
        component.append(face)
        for edge in face.edges:
            for linked_face in edge.link_faces:
                if linked_face.index not in seen:
                    stack.append(linked_face)
    return component


def unique_faces_vertices(faces: list[bmesh.types.BMFace]) -> list[bmesh.types.BMVert]:
    result: list[bmesh.types.BMVert] = []
    seen: set[int] = set()
    for face in faces:
        for vert in face.verts:
            if vert.index in seen:
                continue
            seen.add(vert.index)
            result.append(vert)
    return result


def bmesh_vertices_center(verts: list[bmesh.types.BMVert]) -> Vector:
    center = Vector((0.0, 0.0, 0.0))
    if not verts:
        return center
    for vert in verts:
        center += vert.co
    return center / len(verts)


def component_nudge_directions(
    faces: list[bmesh.types.BMFace],
    verts: list[bmesh.types.BMVert],
    mesh_center: Vector,
) -> list[Vector]:
    directions: list[Vector] = []
    component_center = bmesh_vertices_center(verts)
    away = component_center - mesh_center
    if away.length > 0:
        away.normalize()
        directions.append(away)

    normal = Vector((0.0, 0.0, 0.0))
    for face in faces:
        normal += face.normal * bmesh_face_area(face)
    if normal.length > 0:
        normal.normalize()
        directions.extend([normal, -normal])

    axis_directions = [
        Vector((1.0, 0.0, 0.0)),
        Vector((-1.0, 0.0, 0.0)),
        Vector((0.0, 1.0, 0.0)),
        Vector((0.0, -1.0, 0.0)),
        Vector((0.0, 0.0, 1.0)),
        Vector((0.0, 0.0, -1.0)),
    ]
    directions.extend(axis_directions)

    unique: list[Vector] = []
    seen: set[tuple[int, int, int]] = set()
    for direction in directions:
        if direction.length <= 0:
            continue
        direction = direction.normalized()
        key = tuple(round(value * 1000) for value in direction)
        if key in seen:
            continue
        seen.add(key)
        unique.append(direction)
    return unique


def topology_counts_after_component_nudge(
    bm: bmesh.types.BMesh,
    verts: list[bmesh.types.BMVert],
    direction: Vector,
) -> dict[str, int]:
    trial = bm.copy()
    try:
        trial.verts.ensure_lookup_table()
        for vert in verts:
            trial.verts[vert.index].co += direction * GEOMETRIC_CLEANUP_COMPONENT_NUDGE_MM
        face_vertices = geometric_face_vertices(trial)
        counts, _edge_faces = geometric_topology_counts_from_faces(face_vertices)
        return counts
    finally:
        trial.free()


def geometric_face_vertices(bm: bmesh.types.BMesh) -> dict[int, tuple[Vector, Vector, Vector]]:
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    return {
        face.index: (face.verts[0].co.copy(), face.verts[1].co.copy(), face.verts[2].co.copy())
        for face in bm.faces
        if face.is_valid and len(face.verts) == 3
    }


def geometric_topology_counts_from_faces(
    face_vertices: dict[int, tuple[Vector, Vector, Vector]],
    removed_faces: frozenset[int] = frozenset(),
) -> tuple[dict[str, int], dict[tuple[tuple[int, int, int], tuple[int, int, int]], set[int]]]:
    face_edges = {
        face_index: geometric_triangle_edges(vertices)
        for face_index, vertices in face_vertices.items()
    }
    return geometric_topology_counts_from_face_edges(face_edges, removed_faces=removed_faces)


def geometric_topology_counts_from_face_edges(
    face_edges: dict[int, tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...]],
    removed_faces: frozenset[int] = frozenset(),
) -> tuple[dict[str, int], dict[tuple[tuple[int, int, int], tuple[int, int, int]], set[int]]]:
    edge_faces: dict[tuple[tuple[int, int, int], tuple[int, int, int]], set[int]] = {}
    for face_index, edges in face_edges.items():
        if face_index in removed_faces:
            continue
        for edge in edges:
            edge_faces.setdefault(edge, set()).add(face_index)
    edge_face_counts = [len(faces) for faces in edge_faces.values()]
    return (
        {
            "boundary_edge_count": sum(1 for count in edge_face_counts if count == 1),
            "non_manifold_edge_count": sum(1 for count in edge_face_counts if count != 2),
        },
        edge_faces,
    )


def geometric_triangle_edges(
    vertices: tuple[Vector, Vector, Vector],
) -> tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...]:
    return tuple(
        geometric_edge_key(first, second)
        for first, second in ((vertices[0], vertices[1]), (vertices[1], vertices[2]), (vertices[2], vertices[0]))
    )


def geometric_edge_key(first: Vector, second: Vector) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    return tuple(
        sorted(
            (
                quantized_vector(first, GEOMETRIC_CLEANUP_QUANTIZATION_MM),
                quantized_vector(second, GEOMETRIC_CLEANUP_QUANTIZATION_MM),
            )
        )
    )


def best_geometric_face_removal(
    face_vertices: dict[int, tuple[Vector, Vector, Vector]],
    candidate_faces: list[int],
) -> tuple[int, ...]:
    candidate_areas = {
        face_index: triangle_area(*face_vertices[face_index])
        for face_index in candidate_faces
    }
    _base_counts, edge_faces = geometric_topology_counts_from_faces(face_vertices)
    base_bad_edges = {edge for edge, faces in edge_faces.items() if len(faces) != 2}
    face_edges = {
        face_index: geometric_triangle_edges(vertices)
        for face_index, vertices in face_vertices.items()
    }
    max_remove = min(GEOMETRIC_CLEANUP_MAX_REMOVE_FACES, len(candidate_faces))
    for remove_count in range(1, max_remove + 1):
        solutions: list[tuple[float, tuple[int, ...]]] = []
        for removal in combinations(candidate_faces, remove_count):
            touched_edges: set[tuple[tuple[int, int, int], tuple[int, int, int]]] = set()
            removed_counts: dict[tuple[tuple[int, int, int], tuple[int, int, int]], int] = {}
            for face_index in removal:
                for edge in face_edges[face_index]:
                    touched_edges.add(edge)
                    removed_counts[edge] = removed_counts.get(edge, 0) + 1
            if any(edge not in touched_edges for edge in base_bad_edges):
                continue
            if any(len(edge_faces.get(edge, set())) - removed_counts[edge] != 2 for edge in touched_edges):
                continue
            solutions.append((sum(candidate_areas[face_index] for face_index in removal), removal))
        if solutions:
            solutions.sort(key=lambda item: (item[0], item[1]))
            return solutions[0][1]
    return ()


def quantized_vector(vector: Vector, quantization: float) -> tuple[int, int, int]:
    return tuple(round(stl_float32(float(value)) / quantization) for value in vector)  # type: ignore[return-value]


def stl_float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def prepare_images_for_gltf(out_path: Path) -> Path | None:
    images = [image for image in bpy.data.images if image_has_pixels(image)]
    if not images:
        return None
    texture_dir = out_path.parent / f"{out_path.stem}_gltf_textures"
    texture_dir.mkdir(parents=True, exist_ok=True)
    converted = 0
    for index, image in enumerate(images, start=1):
        target = texture_dir / f"{index:03d}_{safe_image_filename(image.name)}.png"
        try:
            image.filepath_raw = str(target)
            image.file_format = "PNG"
            image.save()
            image.reload()
            converted += 1
        except Exception as exc:
            WARNINGS.append(f"Could not prepare GLB texture '{image.name}' as PNG: {exc}")
    if converted == 0:
        shutil.rmtree(texture_dir, ignore_errors=True)
        return None
    progress(f"Prepared {converted} GLB texture images")
    return texture_dir


def image_has_pixels(image: bpy.types.Image) -> bool:
    try:
        return image.size[0] > 0 and image.size[1] > 0 and len(image.pixels) > 0
    except Exception:
        return False


def safe_image_filename(name: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in name)
    safe = safe.strip("_")[:80]
    return safe or "texture"


def export_model(out_path: Path, format_name: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if format_name in {"stl", "3mf"}:
        for obj in mesh_objects():
            recalculate_mesh_face_winding(obj.data)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in mesh_objects():
        obj.select_set(True)
    if format_name == "stl":
        if hasattr(bpy.ops.wm, "stl_export"):
            bpy.ops.wm.stl_export(filepath=str(out_path), export_selected_objects=True)
        elif hasattr(bpy.ops.export_mesh, "stl"):
            bpy.ops.export_mesh.stl(filepath=str(out_path), use_selection=True)
        else:
            raise RuntimeError("STL export operator is unavailable in this Blender installation.")
    elif format_name == "3mf":
        if hasattr(bpy.ops.wm, "export_3mf"):
            bpy.ops.wm.export_3mf(filepath=str(out_path))
        else:
            raise RuntimeError("3MF export is unavailable in this Blender installation.")
    elif format_name == "glb":
        if hasattr(bpy.ops.export_scene, "gltf"):
            try:
                bpy.ops.export_scene.gltf(
                    filepath=str(out_path),
                    export_format="GLB",
                    use_selection=True,
                    export_yup=False,
                )
            except TypeError:
                bpy.ops.export_scene.gltf(
                    filepath=str(out_path),
                    export_format="GLB",
                    use_selection=True,
                )
        else:
            raise RuntimeError("glTF export is unavailable in this Blender installation.")
    else:
        raise RuntimeError(f"Unsupported format: {format_name}")


def write_manifest(out_path: Path, metadata: dict) -> None:
    manifest_path = out_path.with_name(out_path.stem + "_manifest.json")
    manifest_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def write_piece_inspection(out_path: Path, s3o_path: Path) -> None:
    data = {
        "source_s3o": str(s3o_path.resolve()),
        "objects": [
            {
                "name": obj.name,
                "type": obj.type,
                "parent": obj.parent.name if obj.parent else None,
                "children": [child.name for child in obj.children],
                "location": list(round(value, 5) for value in obj.location),
                "world_location": list(round(value, 5) for value in obj.matrix_world.translation),
                "dimensions": list(round(value, 5) for value in obj.dimensions),
                "bounds": object_bounds(obj) if obj.type == "MESH" else None,
            }
            for obj in sorted(bpy.data.objects, key=lambda item: item.name.casefold())
        ],
        "mesh_count": len(mesh_objects()),
        "blender_version": bpy.app.version_string,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def mesh_objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.data.objects if obj.type == "MESH"]


def recalculate_mesh_face_winding(mesh: bpy.types.Mesh) -> None:
    if not mesh.polygons:
        return
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()


def apply_all_mesh_transforms() -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for obj in mesh_objects():
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        obj.select_set(False)


def bounds(objects: list[bpy.types.Object]) -> tuple[Vector, Vector]:
    if not objects:
        raise RuntimeError("No mesh objects available for bounds calculation.")
    points = [
        obj.matrix_world @ obj.data.vertices[index].co
        for obj in objects
        for index in mesh_surface_vertex_indices(obj.data)
    ]
    if not points:
        points = [obj.matrix_world @ vertex.co for obj in objects for vertex in obj.data.vertices]
    return bounds_from_points(points)


def bounds_from_points(points: list[Vector]) -> tuple[Vector, Vector]:
    if not points:
        raise RuntimeError("No points available for bounds calculation.")
    min_v = Vector((min(point.x for point in points), min(point.y for point in points), min(point.z for point in points)))
    max_v = Vector((max(point.x for point in points), max(point.y for point in points), max(point.z for point in points)))
    return min_v, max_v


def scene_bounds() -> dict:
    return bounds_payload(*bounds(mesh_objects()))


def glb_expected_print_bounds_after_reload() -> dict:
    min_v, max_v = bounds(mesh_objects())
    corrected_points = [
        Vector(glb_imported_to_print_point((x, y, z)))
        for x in (min_v.x, max_v.x)
        for y in (min_v.y, max_v.y)
        for z in (min_v.z, max_v.z)
    ]
    min_corrected, max_corrected = bounds_from_points(corrected_points)
    min_normalized, max_normalized = normalized_print_bounds(min_corrected, max_corrected)
    return bounds_payload(min_normalized, max_normalized)


def normalized_print_bounds(min_v: Vector, max_v: Vector) -> tuple[Vector, Vector]:
    offset = Vector(((min_v.x + max_v.x) / 2, (min_v.y + max_v.y) / 2, min_v.z))
    return min_v - offset, max_v - offset


def bounds_payload(min_v: Vector, max_v: Vector) -> dict:
    dimensions = max_v - min_v
    height = round(float(dimensions.z), 5)
    return {
        "min": rounded_vector(min_v),
        "max": rounded_vector(max_v),
        "center": rounded_vector((min_v + max_v) / 2),
        "dimensions": rounded_vector(dimensions),
        "height": height,
        "height_mm": height,
    }


def rounded_vector(vector: Vector) -> list[float]:
    return [round(float(value), 5) for value in vector]


def require_bounds_height_close(expected: dict, actual: dict, label: str, tolerance_ratio: float = 0.05) -> None:
    expected_height = float(expected["height"])
    actual_height = float(actual["height"])
    if expected_height <= 0:
        raise RuntimeError(f"{label} expected height is invalid: {expected_height:.5f}mm")
    difference_ratio = abs(actual_height - expected_height) / expected_height
    if difference_ratio > tolerance_ratio:
        raise RuntimeError(
            f"{label} height changed from {expected_height:.3f}mm to {actual_height:.3f}mm "
            f"after GLB reload ({difference_ratio * 100:.1f}% difference)."
        )


def object_bounds(obj: bpy.types.Object) -> dict:
    surface_vertex_indices = mesh_surface_vertex_indices(obj.data)
    points = [obj.matrix_world @ obj.data.vertices[index].co for index in surface_vertex_indices]
    if not points:
        points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    min_v, max_v = bounds_from_points(points)
    return bounds_payload(min_v, max_v)


def mesh_surface_vertex_indices(mesh: bpy.types.Mesh) -> list[int]:
    indices: set[int] = set()
    for polygon in mesh.polygons:
        indices.update(polygon.vertices)
    return sorted(indices)


if __name__ == "__main__":
    raise SystemExit(main())
