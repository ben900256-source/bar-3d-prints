from __future__ import annotations

from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any
import json
import random
import tempfile

from .s3o_bounds import S3OBoundsError, read_s3o_piece_names


AUTO_POSE_SOURCE = "builtin_bar_source_ranges"
DEFAULT_AUTO_PROFILE = "bot_small"
BUILTIN_PROFILE_FILES = {
    "bot_small": "bot_small.json",
    "bot_large": "bot_large.json",
    "building": "building.json",
    "raptor_biped": "raptor_biped.json",
    "raptor_multileg": "raptor_multileg.json",
    "tick": "tick.json",
    "turret": "turret.json",
    "vehicle_tank": "vehicle_tank.json",
}


class PoseProfileError(RuntimeError):
    pass


def default_profile_path() -> Path:
    return builtin_profile_path(DEFAULT_AUTO_PROFILE)


def builtin_profile_path(profile_name: str) -> Path:
    try:
        filename = BUILTIN_PROFILE_FILES[profile_name]
    except KeyError as exc:
        available = ", ".join(sorted(BUILTIN_PROFILE_FILES))
        raise PoseProfileError(f"Unknown built-in pose profile '{profile_name}'. Available: {available}") from exc
    return Path(str(resources.files("barprint") / "profiles" / filename))


def load_profile(path: str | Path | None) -> dict[str, Any]:
    profile_path = Path(path).expanduser() if path else default_profile_path()
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PoseProfileError(f"Pose profile not found: {profile_path}") from exc
    except json.JSONDecodeError as exc:
        raise PoseProfileError(f"Invalid pose profile JSON at {profile_path}: {exc}") from exc
    validate_profile(profile, profile_path)
    return profile


def load_profile_for_source(
    path: str | Path | None,
    *,
    unit: Any | None = None,
    s3o_path: Path | None = None,
) -> dict[str, Any]:
    if path:
        return load_profile(path)
    return load_builtin_profile(select_auto_profile(unit=unit, s3o_path=s3o_path))


def load_builtin_profile(profile_name: str) -> dict[str, Any]:
    profile = load_profile(builtin_profile_path(profile_name))
    profile["pose_archetype"] = profile_name
    profile["pose_source"] = AUTO_POSE_SOURCE
    return profile


def select_auto_profile(*, unit: Any | None = None, s3o_path: Path | None = None) -> str:
    pieces = _piece_name_set(s3o_path)
    text = _unit_text(unit)
    unit_types = {str(unit_type).casefold() for unit_type in getattr(unit, "unit_types", ())}
    kind = str(getattr(unit, "kind", "") or "").casefold()

    if _looks_like_raptor(text, pieces):
        if _has_any_piece(pieces, "foot1l", "thigh1l", "lbthigh", "lfthigh", "rfthigh", "rbthigh"):
            return "raptor_multileg"
        return "raptor_biped"
    if {"rrleg", "rfleg", "lrleg", "lfleg"}.issubset(pieces) or "armflea" in text or " tick " in f" {text} ":
        return "tick"
    if _looks_like_vehicle(text, unit_types):
        return "vehicle_tank"
    if kind == "building":
        return "building"
    if "experimental" in unit_types:
        return "bot_large"
    if "bot" in unit_types or _has_biped_legs(pieces):
        return "bot_small"
    if _has_any_piece(pieces, "turret", "sleeve", "barrel"):
        return "turret"
    return DEFAULT_AUTO_PROFILE


def _piece_name_set(s3o_path: Path | None) -> set[str]:
    if s3o_path is None:
        return set()
    try:
        return {name.casefold() for name in read_s3o_piece_names(s3o_path)}
    except (OSError, S3OBoundsError):
        return set()


def _unit_text(unit: Any | None) -> str:
    if unit is None:
        return ""
    parts = [
        getattr(unit, "unit_code", ""),
        getattr(unit, "display_name", ""),
        getattr(unit, "description", ""),
        getattr(unit, "objectname", ""),
        getattr(unit, "faction", ""),
        getattr(unit, "kind", ""),
        " ".join(str(unit_type) for unit_type in getattr(unit, "unit_types", ())),
    ]
    lua_path = getattr(unit, "lua_path", None)
    if lua_path is not None:
        parts.append(Path(lua_path).as_posix())
    return " ".join(str(part) for part in parts if part).casefold()


def _looks_like_raptor(text: str, pieces: set[str]) -> bool:
    return "raptor" in text or _has_any_piece(pieces, "tail", "lshin", "rshin", "lbthigh", "rfthigh")


def _looks_like_vehicle(text: str, unit_types: set[str]) -> bool:
    return "vehicle" in unit_types or any(token in text for token in ("tank", "vehicle", "hover"))


def _has_biped_legs(pieces: set[str]) -> bool:
    return _has_any_piece(pieces, "lthigh", "rthigh") and _has_any_piece(pieces, "lleg", "rleg", "lfoot", "rfoot")


def _has_any_piece(pieces: set[str], *names: str) -> bool:
    return any(name.casefold() in pieces for name in names)


def validate_profile(profile: dict[str, Any], source: Path | None = None) -> None:
    where = f" in {source}" if source else ""
    if not isinstance(profile.get("name"), str) or not profile["name"]:
        raise PoseProfileError(f"Pose profile missing non-empty 'name'{where}")
    poses = profile.get("poses")
    if not isinstance(poses, list) or not poses:
        raise PoseProfileError(f"Pose profile '{profile['name']}' must include at least one pose")
    for index, pose in enumerate(poses):
        if not isinstance(pose, dict):
            raise PoseProfileError(f"Pose #{index} must be an object")
        if not isinstance(pose.get("name"), str) or not pose["name"]:
            raise PoseProfileError(f"Pose #{index} missing non-empty 'name'")
        if "pieces" in pose and not isinstance(pose["pieces"], dict):
            raise PoseProfileError(f"Pose '{pose['name']}' has non-object 'pieces'")
    validate_string_list(profile, "delete_piece_aliases")
    validate_string_list(profile, "optional_delete_piece_aliases")
    if "piece_aliases" in profile and not isinstance(profile["piece_aliases"], dict):
        raise PoseProfileError("'piece_aliases' must be an object")
    validate_piece_transforms(profile.get("piece_transforms"), "piece_transforms")
    validate_piece_transforms(profile.get("optional_piece_transforms"), "optional_piece_transforms")
    variants = profile.get("variants")
    if variants is not None:
        if not isinstance(variants, list):
            raise PoseProfileError("'variants' must be an array")
        seen_variants = {"standard"}
        for index, variant in enumerate(variants):
            if not isinstance(variant, dict):
                raise PoseProfileError(f"Variant #{index} must be an object")
            name = variant.get("name")
            if not isinstance(name, str) or not name:
                raise PoseProfileError(f"Variant #{index} missing non-empty 'name'")
            if name == "standard":
                raise PoseProfileError("Variant name 'standard' is reserved")
            if name in seen_variants:
                raise PoseProfileError(f"Duplicate variant name '{name}'")
            seen_variants.add(name)
            validate_string_list(variant, "delete_piece_aliases", prefix=f"Variant '{name}' ")
            validate_string_list(variant, "optional_delete_piece_aliases", prefix=f"Variant '{name}' ")
            validate_piece_transforms(variant.get("piece_transforms"), f"Variant '{name}' piece_transforms")
            validate_piece_transforms(
                variant.get("optional_piece_transforms"),
                f"Variant '{name}' optional_piece_transforms",
            )
    if "base" in profile and not isinstance(profile["base"], dict):
        raise PoseProfileError("'base' must be an object")
    if "scale" in profile:
        scale = profile["scale"]
        if not isinstance(scale, dict):
            raise PoseProfileError("'scale' must be an object")
        mode = scale.get("mode")
        if mode is not None and mode not in {"profile", "absolute", "game-relative", "game_relative"}:
            raise PoseProfileError("'scale.mode' must be profile, absolute, or game-relative")
        reference_unit = scale.get("reference_unit")
        if reference_unit is not None and (
            not isinstance(reference_unit, str) or not reference_unit.strip()
        ):
            raise PoseProfileError("'scale.reference_unit' must be a non-empty string")
        reference_height_mm = scale.get("reference_height_mm")
        if reference_height_mm is not None and (
            not isinstance(reference_height_mm, int | float) or reference_height_mm <= 0
        ):
            raise PoseProfileError("'scale.reference_height_mm' must be greater than zero")
        max_unit_height_mm = scale.get("max_unit_height_mm")
        if max_unit_height_mm is not None and (
            not isinstance(max_unit_height_mm, int | float) or max_unit_height_mm <= 0
        ):
            raise PoseProfileError("'scale.max_unit_height_mm' must be greater than zero")
    if "thin_features" in profile:
        thin_features = profile["thin_features"]
        if not isinstance(thin_features, dict):
            raise PoseProfileError("'thin_features' must be an object")
        enabled = thin_features.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise PoseProfileError("'thin_features.enabled' must be a boolean")
        for field in ("min_thickness_mm", "max_inflate_mm", "ray_epsilon_mm"):
            value = thin_features.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int | float) or value <= 0):
                raise PoseProfileError(f"'thin_features.{field}' must be greater than zero")


def validate_string_list(container: dict[str, Any], key: str, *, prefix: str = "") -> None:
    value = container.get(key)
    if value is None:
        return
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise PoseProfileError(f"{prefix}'{key}' must be an array of non-empty strings")


def validate_piece_transforms(value: Any, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise PoseProfileError(f"'{label}' must be an object")
    for alias, transform in value.items():
        if not isinstance(alias, str) or not alias:
            raise PoseProfileError(f"'{label}' keys must be non-empty strings")
        if not isinstance(transform, dict):
            raise PoseProfileError(f"'{label}.{alias}' must be an object")
        for field in ("translate_x", "translate_y", "translate_z"):
            number = transform.get(field)
            if number is not None and (isinstance(number, bool) or not isinstance(number, int | float)):
                raise PoseProfileError(f"'{label}.{alias}.{field}' must be a number")


def apply_overrides(
    profile: dict[str, Any],
    *,
    scale_mm: float | None = None,
    base_enabled: bool | None = None,
    base_diameter_mm: float | None = None,
    base_height_mm: float | None = None,
    thin_features_enabled: bool | None = None,
    min_feature_mm: float | None = None,
    thin_feature_max_inflate_mm: float | None = None,
) -> dict[str, Any]:
    updated = deepcopy(profile)
    if scale_mm is not None:
        updated["scale_mm"] = scale_mm
    base = dict(updated.get("base") or {})
    if base_enabled is not None:
        base["enabled"] = base_enabled
    if base_diameter_mm is not None:
        base["diameter_mm"] = base_diameter_mm
    if base_height_mm is not None:
        base["height_mm"] = base_height_mm
    updated["base"] = base
    thin_features = dict(updated.get("thin_features") or {})
    if thin_features_enabled is not None:
        thin_features["enabled"] = thin_features_enabled
    if min_feature_mm is not None:
        thin_features["min_thickness_mm"] = min_feature_mm
    if thin_feature_max_inflate_mm is not None:
        thin_features["max_inflate_mm"] = thin_feature_max_inflate_mm
    if thin_features:
        updated["thin_features"] = thin_features
    validate_profile(updated)
    return updated


def pose_names(profile: dict[str, Any], selection: str) -> list[str]:
    names = [pose["name"] for pose in profile["poses"]]
    if selection == "all":
        return names
    if selection not in names:
        raise PoseProfileError(
            f"Pose '{selection}' not found in profile '{profile['name']}'. "
            f"Available poses: {', '.join(names)}"
        )
    return [selection]


def variant_names(profile: dict[str, Any], selection: str) -> list[str]:
    names = ["standard", *(variant["name"] for variant in profile.get("variants", []))]
    if selection == "all":
        return names
    if selection not in names:
        raise PoseProfileError(
            f"Variant '{selection}' not found in profile '{profile['name']}'. "
            f"Available variants: {', '.join(names)}"
        )
    return [selection]


def apply_variant(profile: dict[str, Any], variant_name: str) -> dict[str, Any]:
    updated = deepcopy(profile)
    updated["variant_name"] = variant_name
    if variant_name == "standard":
        validate_profile(updated)
        return updated

    variants = {variant["name"]: variant for variant in profile.get("variants", [])}
    if variant_name not in variants:
        raise PoseProfileError(f"Variant '{variant_name}' not found in profile '{profile['name']}'")

    variant = variants[variant_name]
    for key in (
        "delete_piece_aliases",
        "optional_delete_piece_aliases",
        "piece_transforms",
        "optional_piece_transforms",
    ):
        if key in variant:
            updated[key] = deepcopy(variant[key])
    validate_profile(updated)
    return updated


def add_random_poses(profile: dict[str, Any], count: int, seed: int = 12345) -> dict[str, Any]:
    if count < 0:
        raise PoseProfileError("--random-poses must be >= 0")
    updated = deepcopy(profile)
    aliases = list((updated.get("piece_aliases") or {}).keys())
    if not aliases and count:
        raise PoseProfileError("Cannot generate random poses without piece_aliases")
    rng = random.Random(seed)
    for idx in range(1, count + 1):
        pieces: dict[str, dict[str, float]] = {}
        for alias in aliases:
            if rng.random() > 0.55:
                continue
            pieces[alias] = {
                "rot_x_deg": round(rng.uniform(-15, 15), 2),
                "rot_y_deg": round(rng.uniform(-5, 5), 2),
                "rot_z_deg": round(rng.uniform(-20, 20), 2),
            }
        updated["poses"].append({"name": f"random_{idx:03d}", "pieces": pieces})
    validate_profile(updated)
    return updated


def write_temp_profile(profile: dict[str, Any]) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", prefix="barprint_profile_", delete=False, encoding="utf-8")
    with handle:
        json.dump(profile, handle, indent=2)
    return Path(handle.name)
