from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import gzip
import hashlib
import struct

from barprint.cli import apply_scale_mode
from barprint.bar_assets import list_units
from barprint.config import cache_dir
from barprint.s3o_bounds import find_tallest_unit_model, find_unit_model_bounds, read_s3o_bounds, read_s3o_piece_names


def write_s3o(path: Path, root_height: float, child_z: float = 0.0, child_height: float = 0.0) -> None:
    header_size = struct.calcsize("<12sI5f4I")
    piece_size = struct.calcsize("<10I3f")
    vertex_size = struct.calcsize("<8f")

    root_piece_offset = header_size
    child_piece_offset = root_piece_offset + piece_size
    children_offset = child_piece_offset + piece_size
    root_vertices_offset = children_offset + 4
    child_vertices_offset = root_vertices_offset + 2 * vertex_size

    header = struct.pack(
        "<12sI5f4I",
        b"Spring unit\0",
        0,
        0.0,
        max(root_height, child_z + child_height),
        0.0,
        0.0,
        0.0,
        root_piece_offset,
        0,
        0,
        0,
    )
    root_piece = struct.pack(
        "<10I3f",
        0,
        1,
        children_offset,
        2,
        root_vertices_offset,
        0,
        0,
        0,
        0,
        0,
        0.0,
        0.0,
        0.0,
    )
    child_piece = struct.pack(
        "<10I3f",
        0,
        0,
        0,
        1,
        child_vertices_offset,
        0,
        0,
        0,
        0,
        0,
        0.0,
        child_z,
        0.0,
    )
    child_pointer = struct.pack("<I", child_piece_offset)

    def vertex(z: float) -> bytes:
        # Importer mapping: x=-raw_x, y=raw_z, z=raw_y.
        return struct.pack("<8f", 0.0, z, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)

    path.write_bytes(header + root_piece + child_piece + child_pointer + vertex(0.0) + vertex(root_height) + vertex(child_height))


def write_named_s3o(path: Path, root_name: str, child_name: str) -> None:
    header_size = struct.calcsize("<12sI5f4I")
    piece_size = struct.calcsize("<10I3f")
    vertex_size = struct.calcsize("<8f")

    root_piece_offset = header_size
    child_piece_offset = root_piece_offset + piece_size
    children_offset = child_piece_offset + piece_size
    root_vertices_offset = children_offset + 4
    child_vertices_offset = root_vertices_offset + vertex_size
    name_offset = child_vertices_offset + vertex_size
    root_name_offset = name_offset
    child_name_offset = root_name_offset + len(root_name.encode("ascii")) + 1

    header = struct.pack(
        "<12sI5f4I",
        b"Spring unit\0",
        0,
        0.0,
        2.0,
        0.0,
        0.0,
        0.0,
        root_piece_offset,
        0,
        0,
        0,
    )
    root_piece = struct.pack(
        "<10I3f",
        root_name_offset,
        1,
        children_offset,
        1,
        root_vertices_offset,
        0,
        0,
        0,
        0,
        0,
        0.0,
        0.0,
        0.0,
    )
    child_piece = struct.pack(
        "<10I3f",
        child_name_offset,
        0,
        0,
        1,
        child_vertices_offset,
        0,
        0,
        0,
        0,
        0,
        0.0,
        1.0,
        0.0,
    )
    child_pointer = struct.pack("<I", child_piece_offset)
    vertex = struct.pack("<8f", 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
    names = root_name.encode("ascii") + b"\0" + child_name.encode("ascii") + b"\0"
    path.write_bytes(header + root_piece + child_piece + child_pointer + vertex + vertex + names)


def make_bar_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "BAR.sdd"
    (root / "units").mkdir(parents=True)
    (root / "objects3d" / "Units").mkdir(parents=True)
    small = root / "objects3d" / "Units" / "SMALL.s3o"
    big = root / "objects3d" / "Units" / "BIG.s3o"
    armcom = root / "objects3d" / "Units" / "ARMCOM.s3o"
    write_s3o(small, root_height=5.0, child_z=10.0, child_height=2.0)
    write_s3o(big, root_height=25.0)
    write_s3o(armcom, root_height=18.0)
    (root / "units" / "small.lua").write_text('return { small = { objectname = "Units/SMALL.s3o" } }', encoding="utf-8")
    (root / "units" / "big.lua").write_text('return { big = { objectname = "Units/BIG.s3o" } }', encoding="utf-8")
    (root / "units" / "armcom.lua").write_text('return { armcom = { objectname = "Units/ARMCOM.s3o" } }', encoding="utf-8")
    return root, small, big


def make_rapid_fixture(install_root: Path) -> Path:
    data_root = install_root / "data"
    package = data_root / "packages" / "bar-game.sdp"
    records: list[tuple[str, str, int]] = []

    add_rapid_entry(
        data_root,
        records,
        "units/armcom.lua",
        b'return { armcom = { objectname = "Units/ARMCOM.s3o" } }',
    )
    add_rapid_entry(
        data_root,
        records,
        "units/big.lua",
        b'return { big = { objectname = "Units/BIG.s3o" } }',
    )
    add_rapid_entry(
        data_root,
        records,
        "objects3d/units/armcom.s3o",
        make_s3o_bytes(root_height=18.0),
    )
    add_rapid_entry(
        data_root,
        records,
        "objects3d/units/big.s3o",
        make_s3o_bytes(root_height=25.0),
    )

    package.parent.mkdir(parents=True, exist_ok=True)
    table = bytearray()
    for name, hash_hex, size in records:
        encoded_name = name.encode("utf-8")
        table.extend(bytes([len(encoded_name)]))
        table.extend(encoded_name)
        table.extend(bytes.fromhex(hash_hex))
        table.extend(b"\0\0\0\0")
        table.extend(size.to_bytes(4, "big"))
    package.write_bytes(gzip.compress(bytes(table)))
    return data_root


def add_rapid_entry(data_root: Path, records: list[tuple[str, str, int]], name: str, content: bytes) -> None:
    hash_hex = hashlib.md5(content).hexdigest()
    pool_file = data_root / "pool" / hash_hex[:2] / f"{hash_hex[2:]}.gz"
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_bytes(gzip.compress(content))
    records.append((name, hash_hex, len(content)))


def make_s3o_bytes(root_height: float, child_z: float = 0.0, child_height: float = 0.0) -> bytes:
    header_size = struct.calcsize("<12sI5f4I")
    piece_size = struct.calcsize("<10I3f")
    vertex_size = struct.calcsize("<8f")

    root_piece_offset = header_size
    child_piece_offset = root_piece_offset + piece_size
    children_offset = child_piece_offset + piece_size
    root_vertices_offset = children_offset + 4
    child_vertices_offset = root_vertices_offset + 2 * vertex_size

    header = struct.pack(
        "<12sI5f4I",
        b"Spring unit\0",
        0,
        0.0,
        max(root_height, child_z + child_height),
        0.0,
        0.0,
        0.0,
        root_piece_offset,
        0,
        0,
        0,
    )
    root_piece = struct.pack(
        "<10I3f",
        0,
        1,
        children_offset,
        2,
        root_vertices_offset,
        0,
        0,
        0,
        0,
        0,
        0.0,
        0.0,
        0.0,
    )
    child_piece = struct.pack(
        "<10I3f",
        0,
        0,
        0,
        1,
        child_vertices_offset,
        0,
        0,
        0,
        0,
        0,
        0.0,
        child_z,
        0.0,
    )
    child_pointer = struct.pack("<I", child_piece_offset)

    def vertex(z: float) -> bytes:
        return struct.pack("<8f", 0.0, z, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)

    return header + root_piece + child_piece + child_pointer + vertex(0.0) + vertex(root_height) + vertex(child_height)


def test_read_s3o_bounds_includes_child_offsets(tmp_path: Path) -> None:
    path = tmp_path / "model.s3o"
    write_s3o(path, root_height=5.0, child_z=10.0, child_height=2.0)
    bounds = read_s3o_bounds(path)
    assert bounds.min_z == 0.0
    assert bounds.max_z == 12.0
    assert bounds.height == 12.0


def test_read_s3o_piece_names_preserves_tree_order(tmp_path: Path) -> None:
    path = tmp_path / "model.s3o"
    write_named_s3o(path, "pelvis", "rthigh")

    assert read_s3o_piece_names(path) == ("pelvis", "rthigh")


def test_find_tallest_unit_model(tmp_path: Path) -> None:
    root, _, _ = make_bar_fixture(tmp_path)
    tallest = find_tallest_unit_model(root)
    assert tallest.unit.unit_code == "big"
    assert tallest.bounds.height == 25.0


def test_find_unit_model_bounds(tmp_path: Path) -> None:
    root, _, _ = make_bar_fixture(tmp_path)
    reference = find_unit_model_bounds(root, "armcom")
    assert reference.unit.unit_code == "armcom"
    assert reference.bounds.height == 18.0


def test_find_unit_model_bounds_extracts_rapid_reference(tmp_path: Path, monkeypatch) -> None:
    data_root = make_rapid_fixture(tmp_path / "Beyond-All-Reason")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.chdir(tmp_path)

    reference = find_unit_model_bounds(data_root, "armcom")

    assert reference.unit.unit_code == "armcom"
    assert reference.bounds.height == 18.0
    assert reference.unit.s3o_path.is_file()
    assert (cache_dir() / "rapid" / "bar-game" / "unittextures").is_dir()


def test_find_unit_model_bounds_populates_rapid_textures_for_cached_model(tmp_path: Path, monkeypatch) -> None:
    data_root = make_rapid_fixture(tmp_path / "Beyond-All-Reason")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.chdir(tmp_path)
    unit = next(unit for unit in list_units(data_root) if unit.unit_code == "armcom")
    unit.s3o_path.parent.mkdir(parents=True, exist_ok=True)
    unit.s3o_path.write_bytes(make_s3o_bytes(root_height=18.0))

    reference = find_unit_model_bounds(data_root, "armcom")

    assert reference.unit.unit_code == "armcom"
    assert (cache_dir() / "rapid" / "bar-game" / "unittextures").is_dir()


def test_find_tallest_unit_model_extracts_rapid_models(tmp_path: Path, monkeypatch) -> None:
    data_root = make_rapid_fixture(tmp_path / "Beyond-All-Reason")
    monkeypatch.chdir(tmp_path)

    tallest = find_tallest_unit_model(data_root)

    assert tallest.unit.unit_code == "big"
    assert tallest.bounds.height == 25.0


def test_apply_game_relative_scale_mode(tmp_path: Path) -> None:
    root, small, _ = make_bar_fixture(tmp_path)
    profile = {
        "name": "test",
        "scale_mm": 45,
        "scale": {"mode": "game-relative", "reference_unit": "armcom", "reference_height_mm": 45},
        "poses": [{"name": "neutral", "pieces": {}}],
    }
    args = Namespace(
        scale_mm=None,
        scale_mode=None,
        scale_reference_unit=None,
        scale_reference_height_mm=None,
        max_unit_height_mm=None,
        bar_root=str(root),
        unit="small",
        config_values={},
        verbose=False,
    )
    updated = apply_scale_mode(profile, args, small)
    assert updated["scale_mm"] == 30.0
    assert updated["scale"]["reference_unit"] == "armcom"
    assert updated["scale"]["reference_height_mm"] == 45.0
    assert updated["scale"]["reference_source_height"] == 18.0


def test_apply_game_relative_scale_mode_uses_local_config(tmp_path: Path) -> None:
    root, small, _ = make_bar_fixture(tmp_path)
    profile = {
        "name": "test",
        "scale_mm": 45,
        "scale": {"mode": "game-relative", "reference_unit": "armcom", "reference_height_mm": 45},
        "poses": [{"name": "neutral", "pieces": {}}],
    }
    args = Namespace(
        scale_mm=None,
        scale_mode=None,
        scale_reference_unit=None,
        scale_reference_height_mm=None,
        max_unit_height_mm=None,
        bar_root=str(root),
        unit="small",
        config_values={"scale_reference_unit": "small", "scale_reference_height_mm": 80.0},
        verbose=False,
    )
    updated = apply_scale_mode(profile, args, small)
    assert updated["scale_mm"] == 80.0
    assert updated["scale"]["reference_unit"] == "small"


def test_apply_game_relative_scale_mode_can_use_tallest_reference(tmp_path: Path) -> None:
    root, small, _ = make_bar_fixture(tmp_path)
    profile = {
        "name": "test",
        "scale_mm": 45,
        "scale": {"mode": "game-relative", "reference_unit": "tallest", "reference_height_mm": 200},
        "poses": [{"name": "neutral", "pieces": {}}],
    }
    args = Namespace(
        scale_mm=None,
        scale_mode=None,
        scale_reference_unit=None,
        scale_reference_height_mm=None,
        max_unit_height_mm=None,
        bar_root=str(root),
        unit="small",
        config_values={},
        verbose=False,
    )
    updated = apply_scale_mode(profile, args, small)
    assert updated["scale_mm"] == 96.0
    assert updated["scale"]["reference_unit"] == "big"

