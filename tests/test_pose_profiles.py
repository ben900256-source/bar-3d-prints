import pytest
from types import SimpleNamespace

from barprint.pose_profiles import (
    PoseProfileError,
    add_random_poses,
    apply_variant,
    apply_overrides,
    load_profile_for_source,
    load_profile,
    pose_names,
    select_auto_profile,
    variant_names,
    validate_profile,
)


def valid_profile() -> dict:
    return {
        "name": "test",
        "scale_mm": 45,
        "base": {"enabled": True, "diameter_mm": 32, "height_mm": 2.4},
        "piece_aliases": {"head": ["head"]},
        "poses": [{"name": "neutral", "pieces": {}}],
    }


def test_validate_profile_requires_name() -> None:
    profile = valid_profile()
    del profile["name"]
    with pytest.raises(PoseProfileError, match="name"):
        validate_profile(profile)


def test_validate_profile_requires_poses() -> None:
    profile = valid_profile()
    profile["poses"] = []
    with pytest.raises(PoseProfileError, match="at least one pose"):
        validate_profile(profile)


def test_pose_names_all_and_single() -> None:
    profile = valid_profile()
    profile["poses"].append({"name": "aim_left", "pieces": {}})
    assert pose_names(profile, "all") == ["neutral", "aim_left"]
    assert pose_names(profile, "neutral") == ["neutral"]


def test_variant_names_all_and_single() -> None:
    profile = valid_profile()
    profile["variants"] = [{"name": "decorated"}]
    assert variant_names(profile, "all") == ["standard", "decorated"]
    assert variant_names(profile, "standard") == ["standard"]
    assert variant_names(profile, "decorated") == ["decorated"]


def test_apply_variant_replaces_delete_lists_and_piece_transforms() -> None:
    profile = valid_profile()
    profile["optional_delete_piece_aliases"] = ["crown", "medalgold"]
    profile["variants"] = [
        {
            "name": "decorated",
            "optional_delete_piece_aliases": ["medalsilver"],
            "optional_piece_transforms": {"crown": {"translate_z": 100}},
        }
    ]

    decorated = apply_variant(profile, "decorated")

    assert decorated["variant_name"] == "decorated"
    assert decorated["optional_delete_piece_aliases"] == ["medalsilver"]
    assert decorated["optional_piece_transforms"]["crown"]["translate_z"] == 100


def test_default_bot_profile_uses_precise_commander_pose_aliases() -> None:
    profile = load_profile(None)
    aliases = profile["piece_aliases"]

    assert "ra" not in aliases["rarm"]
    assert "la" not in aliases["larm"]
    assert "ruparm" in aliases["rarm"]
    assert "luparm" in aliases["larm"]
    assert aliases["weapon"][0] == "biggun"
    assert "rthigh" in aliases
    assert "lfoot" in aliases

    poses = {pose["name"]: pose["pieces"] for pose in profile["poses"]}
    assert [pose["name"] for pose in profile["poses"]] == [
        "neutral",
        "aim_left",
        "aim_right",
        "stride_left",
        "stride_right",
        "brace",
        "advance",
    ]
    assert "weapon" in poses["aim_left"]
    assert "head" not in poses["aim_left"]
    assert "barrel" not in poses["aim_left"]


def test_select_auto_profile_uses_unit_archetype_metadata() -> None:
    vehicle = SimpleNamespace(
        unit_code="armstump",
        display_name="Stumpy",
        description="Light Tank",
        objectname="Units/ARMSTUMP.s3o",
        faction="Armada",
        kind="unit",
        unit_types=("vehicle",),
        lua_path="units/ArmVehicles/armstump.lua",
    )
    building = SimpleNamespace(
        unit_code="armllt",
        display_name="Light Laser Tower",
        description="Defense turret",
        objectname="Units/ARMLLT.s3o",
        faction="Armada",
        kind="building",
        unit_types=(),
        lua_path="units/ArmBuildings/armllt.lua",
    )

    assert select_auto_profile(unit=vehicle) == "vehicle_tank"
    assert select_auto_profile(unit=building) == "building"


def test_select_auto_profile_uses_s3o_piece_names(tmp_path, monkeypatch) -> None:
    dummy = tmp_path / "model.s3o"
    dummy.write_bytes(b"not a real model")

    monkeypatch.setattr(
        "barprint.pose_profiles.read_s3o_piece_names",
        lambda path: ("base", "rrleg", "rfleg", "lfleg", "lrleg", "turret"),
    )

    assert select_auto_profile(s3o_path=dummy) == "tick"


def test_load_profile_for_source_marks_automatic_profile() -> None:
    unit = SimpleNamespace(
        unit_code="raptor_land_swarmer_basic_t1_v1",
        display_name="Raptor",
        description="Raptor",
        objectname="Units/RAPTOR.s3o",
        faction="Raptors",
        kind="unit",
        unit_types=("bot",),
        lua_path="units/other/raptors/Swarmer/raptor.lua",
    )

    profile = load_profile_for_source(None, unit=unit)

    assert profile["name"] == "raptor_biped"
    assert profile["pose_archetype"] == "raptor_biped"
    assert profile["pose_source"] == "builtin_bar_source_ranges"


def test_load_profile_for_source_leaves_explicit_profile_unmarked() -> None:
    profile = load_profile_for_source("barprint/profiles/tick.json")

    assert profile["name"] == "tick"
    assert "pose_source" not in profile


def test_apply_overrides_updates_base_and_scale() -> None:
    profile = apply_overrides(
        valid_profile(),
        scale_mm=55,
        base_enabled=False,
        base_diameter_mm=40,
    )
    assert profile["scale_mm"] == 55
    assert profile["base"]["enabled"] is False
    assert profile["base"]["diameter_mm"] == 40


def test_validate_profile_ignores_stale_texture_detail_config() -> None:
    profile = valid_profile()
    profile["texture_detail"] = {
        "enabled": "removed",
        "spacing_mm": 0,
    }
    validate_profile(profile)


def test_validate_profile_accepts_thin_features_config() -> None:
    profile = valid_profile()
    profile["thin_features"] = {
        "enabled": True,
        "min_thickness_mm": 0.8,
        "max_inflate_mm": 0.4,
        "ray_epsilon_mm": 0.02,
    }
    validate_profile(profile)


def test_validate_profile_accepts_reference_scale_config() -> None:
    profile = valid_profile()
    profile["scale"] = {
        "mode": "game-relative",
        "reference_unit": "armcom",
        "reference_height_mm": 45,
    }

    validate_profile(profile)


def test_validate_profile_rejects_invalid_thin_feature_numbers() -> None:
    profile = valid_profile()
    profile["thin_features"] = {"min_thickness_mm": 0}
    with pytest.raises(PoseProfileError, match="min_thickness_mm"):
        validate_profile(profile)


def test_apply_overrides_updates_thin_features() -> None:
    profile = apply_overrides(
        valid_profile(),
        thin_features_enabled=False,
        min_feature_mm=1.1,
        thin_feature_max_inflate_mm=0.45,
    )
    assert profile["thin_features"]["enabled"] is False
    assert profile["thin_features"]["min_thickness_mm"] == 1.1
    assert profile["thin_features"]["max_inflate_mm"] == 0.45


def test_add_random_poses_is_deterministic() -> None:
    first = add_random_poses(valid_profile(), 2)
    second = add_random_poses(valid_profile(), 2)
    assert first["poses"] == second["poses"]
    assert first["poses"][-1]["name"] == "random_002"
