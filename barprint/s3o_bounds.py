from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct

from .bar_assets import BarAssetError, UnitAsset, ensure_unit_s3o, list_units


HEADER_FORMAT = "<12sI5f4I"
PIECE_FORMAT = "<10I3f"
VERTEX_FORMAT = "<8f"


class S3OBoundsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Bounds3D:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    @property
    def height(self) -> float:
        return self.max_z - self.min_z

    def include_point(self, x: float, y: float, z: float) -> "Bounds3D":
        return Bounds3D(
            min(self.min_x, x),
            min(self.min_y, y),
            min(self.min_z, z),
            max(self.max_x, x),
            max(self.max_y, y),
            max(self.max_z, z),
        )

    def merge(self, other: "Bounds3D") -> "Bounds3D":
        return Bounds3D(
            min(self.min_x, other.min_x),
            min(self.min_y, other.min_y),
            min(self.min_z, other.min_z),
            max(self.max_x, other.max_x),
            max(self.max_y, other.max_y),
            max(self.max_z, other.max_z),
        )


@dataclass(frozen=True)
class TallestModel:
    unit: UnitAsset
    bounds: Bounds3D


def read_s3o_bounds(path: Path) -> Bounds3D:
    data, root_offset = _read_s3o_data(path)
    bounds = _read_piece_bounds(data, root_offset, (0.0, 0.0, 0.0), set())
    if bounds is None:
        raise S3OBoundsError(f"S3O has no mesh vertices: {path}")
    return bounds


def read_s3o_piece_names(path: Path) -> tuple[str, ...]:
    data, root_offset = _read_s3o_data(path)
    names: list[str] = []
    _read_piece_names(data, root_offset, set(), names)
    return tuple(names)


def _read_s3o_data(path: Path) -> tuple[bytes, int]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise S3OBoundsError(f"Could not read S3O file: {path}") from exc
    if len(data) < struct.calcsize(HEADER_FORMAT):
        raise S3OBoundsError(f"S3O file is too small: {path}")

    header = struct.unpack_from(HEADER_FORMAT, data, 0)
    magic = header[0].decode("ascii", errors="ignore").replace("\x00", "").strip()
    if magic != "Spring unit":
        raise S3OBoundsError(f"Not an S3O file: {path}")
    if header[1] != 0:
        raise S3OBoundsError(f"Unsupported S3O version {header[1]} in {path}")

    return data, header[7]


def find_tallest_unit_model(bar_root: Path) -> TallestModel:
    tallest: TallestModel | None = None
    seen: set[str] = set()
    for unit in list_units(bar_root):
        key = str(unit.s3o_path.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            candidate = _unit_model_bounds(unit)
        except S3OBoundsError:
            continue
        if tallest is None or candidate.bounds.height > tallest.bounds.height:
            tallest = candidate

    if tallest is None:
        raise S3OBoundsError(f"Could not compute S3O bounds for any units under {bar_root}")
    return tallest


def find_unit_model_bounds(bar_root: Path, unit_query: str) -> TallestModel:
    query = unit_query.casefold()
    units = list_units(bar_root)
    for unit in units:
        if unit.unit_code.casefold() == query:
            return _unit_model_bounds(unit)

    for unit in units:
        haystack = f"{unit.unit_code} {unit.display_name} {unit.description} {unit.objectname}".casefold()
        if query in haystack:
            return _unit_model_bounds(unit)

    sample = ", ".join(unit.unit_code for unit in units[:12])
    suffix = f" Available examples: {sample}" if sample else ""
    raise S3OBoundsError(f"Reference unit not found for query '{unit_query}'.{suffix}")


def _unit_model_bounds(unit: UnitAsset) -> TallestModel:
    try:
        s3o_path = ensure_unit_s3o(unit)
    except BarAssetError as exc:
        raise S3OBoundsError(str(exc)) from exc
    bounds = read_s3o_bounds(s3o_path)
    if bounds.height <= 0:
        raise S3OBoundsError(f"Reference unit '{unit.unit_code}' has invalid height: {unit.s3o_path}")
    return TallestModel(unit=unit, bounds=bounds)


def _read_piece_bounds(
    data: bytes,
    offset: int,
    parent_location: tuple[float, float, float],
    visited: set[int],
) -> Bounds3D | None:
    if offset in visited:
        raise S3OBoundsError(f"Cycle detected in S3O piece table at offset {offset}")
    visited.add(offset)

    piece_size = struct.calcsize(PIECE_FORMAT)
    if offset < 0 or offset + piece_size > len(data):
        raise S3OBoundsError(f"Invalid S3O piece offset {offset}")

    piece = struct.unpack_from(PIECE_FORMAT, data, offset)
    child_count = piece[1]
    children_offset = piece[2]
    vertex_count = piece[3]
    vertices_offset = piece[4]

    # Match the FluidPlay importer coordinate remapping.
    location = (
        parent_location[0] - piece[10],
        parent_location[1] + piece[12],
        parent_location[2] + piece[11],
    )

    bounds: Bounds3D | None = None
    vertex_size = struct.calcsize(VERTEX_FORMAT)
    for index in range(vertex_count):
        vertex_offset = vertices_offset + index * vertex_size
        if vertex_offset < 0 or vertex_offset + vertex_size > len(data):
            raise S3OBoundsError(f"Invalid S3O vertex offset {vertex_offset}")
        vertex = struct.unpack_from(VERTEX_FORMAT, data, vertex_offset)
        point = (
            location[0] - vertex[0],
            location[1] + vertex[2],
            location[2] + vertex[1],
        )
        if bounds is None:
            bounds = Bounds3D(*point, *point)
        else:
            bounds = bounds.include_point(*point)

    if child_count:
        if children_offset < 0 or children_offset + child_count * 4 > len(data):
            raise S3OBoundsError(f"Invalid S3O children offset {children_offset}")
        for index in range(child_count):
            child_offset = struct.unpack_from("<I", data, children_offset + index * 4)[0]
            child_bounds = _read_piece_bounds(data, child_offset, location, visited)
            if child_bounds is None:
                continue
            bounds = child_bounds if bounds is None else bounds.merge(child_bounds)

    return bounds


def _read_piece_names(data: bytes, offset: int, visited: set[int], names: list[str]) -> None:
    if offset in visited:
        raise S3OBoundsError(f"Cycle detected in S3O piece table at offset {offset}")
    visited.add(offset)

    piece_size = struct.calcsize(PIECE_FORMAT)
    if offset < 0 or offset + piece_size > len(data):
        raise S3OBoundsError(f"Invalid S3O piece offset {offset}")

    piece = struct.unpack_from(PIECE_FORMAT, data, offset)
    name = _read_c_string(data, piece[0])
    if name:
        names.append(name)

    child_count = piece[1]
    children_offset = piece[2]
    if child_count:
        if children_offset < 0 or children_offset + child_count * 4 > len(data):
            raise S3OBoundsError(f"Invalid S3O children offset {children_offset}")
        for index in range(child_count):
            child_offset = struct.unpack_from("<I", data, children_offset + index * 4)[0]
            _read_piece_names(data, child_offset, visited, names)


def _read_c_string(data: bytes, offset: int) -> str:
    if offset <= 0 or offset >= len(data):
        return ""
    end = data.find(b"\0", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("ascii", errors="ignore").strip()

