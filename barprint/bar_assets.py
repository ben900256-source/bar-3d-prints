from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable
import gzip
import json
import os
import re
import struct

from .config import cache_dir


OBJECTNAME_RE = re.compile(r"objectname\s*=\s*[\"']([^\"']+\.s3o)[\"']", re.IGNORECASE)
BUILDPIC_RE = re.compile(r"buildpic\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
FACTION_ORDER = ("Armada", "Cortex", "Legion", "Scavengers", "Raptors", "Other")
FACTION_BY_PREFIX = {
    "arm": "Armada",
    "cor": "Cortex",
    "leg": "Legion",
    "scav": "Scavengers",
    "raptor": "Raptors",
}
FACTION_FILTERS = {faction.casefold(): faction for faction in FACTION_ORDER}
FACTION_FILTERS.update(
    {
        "scav": "Scavengers",
        "scavs": "Scavengers",
        "scavenger": "Scavengers",
        "raptor": "Raptors",
    }
)
SKIP_DISCOVERY_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "cache",
    "code cache",
    "dawncache",
    "gpucache",
    "node_modules",
    "temp",
    "tmp",
    "venv",
}
KIND_ORDER = ("unit", "building", "other")
KIND_FILTERS = {kind: kind for kind in KIND_ORDER}
KIND_FILTERS["all"] = "all"
UNIT_TYPE_ORDER = ("bot", "aircraft", "naval", "vehicle", "experimental")
UNIT_TYPE_FILTERS = {unit_type: unit_type for unit_type in UNIT_TYPE_ORDER}
UNIT_TYPE_FILTERS["all"] = "all"
CHASSIS_UNIT_TYPES = ("bot", "aircraft", "naval", "vehicle")
BUILDING_FOLDER_HINTS = (
    "building",
    "buildings",
    "defense",
    "defence",
    "economy",
    "factory",
    "factories",
    "gantry",
    "lab",
    "labs",
    "mine",
    "platform",
    "plant",
    "shipyard",
    "storage",
    "turret",
    "wall",
)
OTHER_FOLDER_HINTS = ("dummy", "dummies", "feature", "features", "misc", "other", "weapon", "weapons")
PRODUCTION_FACTORY_WORDS = ("factory", "gantry", "lab", "laboratory", "platform", "plant", "shipyard")
PRODUCTION_FACTORY_PATH_HINTS = ("factories", "landfactories", "seafactories")
PRODUCTION_FACTORY_CODE_SUFFIXES = (
    "aap",
    "ahp",
    "alab",
    "ap",
    "asy",
    "avp",
    "gan",
    "gant",
    "gantry",
    "hp",
    "lab",
    "sy",
    "vp",
)


@dataclass(frozen=True)
class LuaUnitMetadata:
    objectname: str | None = None
    buildpic: str | None = None
    buildoptions: tuple[str, ...] = ()
    fields: dict[str, str | bool | float] | None = None


@dataclass(frozen=True)
class UnitAsset:
    unit_code: str
    lua_path: Path
    objectname: str
    s3o_path: Path
    faction: str
    archive_package: Path | None = None
    archive_s3o_entry: str | None = None
    display_name: str = ""
    description: str = ""
    buildpic: str | None = None
    icon_path: Path | None = None
    kind: str = "unit"
    unit_types: tuple[str, ...] = ()
    buildoptions: tuple[str, ...] = ()
    built_by: tuple[str, ...] = ()
    archive_icon_entry: str | None = None

    def __post_init__(self) -> None:
        if not self.display_name:
            object.__setattr__(self, "display_name", self.unit_code)
        if not self.description:
            object.__setattr__(self, "description", self.objectname)


@dataclass(frozen=True)
class RapidPackageEntry:
    path: str
    hash_hex: str
    size: int


class BarAssetError(RuntimeError):
    pass


def find_bar_root(explicit: str | None) -> Path:
    candidates = _candidate_bar_roots(explicit)
    for candidate in candidates:
        root = _resolve_bar_root_candidate(candidate)
        if root:
            return root

    discovered: list[Path] = []
    if not explicit:
        discovered = list(discover_bar_roots())
        for candidate in discovered:
            root = _resolve_bar_root_candidate(candidate)
            if root:
                return root

    install_candidates: list[Path] = []
    if not explicit:
        install_candidates = _default_program_install_roots()
        for candidate in install_candidates:
            root = _resolve_bar_root_candidate(candidate)
            if root:
                return root

    searched_paths = [*candidates, *discovered, *install_candidates]
    if not explicit:
        searched_paths.extend(root / "**" / "BAR.sdd" for root in _bar_search_roots())
    searched = "\n".join(f"  - {path}" for path in _unique_paths(searched_paths))
    raise BarAssetError(
        "Could not find BAR.sdd. Pass --bar-root explicitly.\n"
        f"Searched:\n{searched}"
    )


def discover_bar_roots() -> Iterable[Path]:
    seen: set[str] = set()
    for search_root in _bar_search_roots():
        for candidate in _walk_for_bar_roots(search_root):
            key = str(candidate.resolve()).casefold()
            if key in seen:
                continue
            seen.add(key)
            yield candidate


def iter_unit_lua_files(bar_root: Path) -> Iterable[Path]:
    units_dir = bar_root / "units"
    if not units_dir.is_dir():
        raise BarAssetError(f"BAR units directory not found: {units_dir}")
    yield from sorted(units_dir.rglob("*.lua"))


def extract_objectname(lua_text: str) -> str | None:
    match = OBJECTNAME_RE.search(lua_text)
    if not match:
        return None
    return _normalize_objectname(match.group(1))


def extract_buildpic(lua_text: str) -> str | None:
    match = BUILDPIC_RE.search(lua_text)
    if not match:
        return None
    return _normalize_buildpic(match.group(1))


def extract_buildoptions(lua_text: str) -> tuple[str, ...]:
    table_text = _extract_lua_table_text(lua_text, "buildoptions")
    if not table_text:
        return ()
    options: list[str] = []
    for match in re.finditer(r"[\"']([^\"']+)[\"']", table_text):
        value = match.group(1).strip().casefold()
        if value:
            options.append(value)
    return tuple(dict.fromkeys(options))


def parse_unit_lua(lua_text: str) -> LuaUnitMetadata:
    fields: dict[str, str | bool | float] = {}
    for field_name in (
        "activatewhenbuilt",
        "builder",
        "canfly",
        "canmove",
        "category",
        "description",
        "footprintx",
        "footprintz",
        "levelground",
        "maxvelocity",
        "movementclass",
        "name",
        "speed",
        "unitname",
        "workertime",
        "yardmap",
    ):
        value = _extract_lua_scalar_field(lua_text, field_name)
        if value is not None:
            fields[field_name.casefold()] = value
    return LuaUnitMetadata(
        objectname=extract_objectname(lua_text),
        buildpic=extract_buildpic(lua_text),
        buildoptions=extract_buildoptions(lua_text),
        fields=fields,
    )


def classify_unit_faction(unit_code: str, lua_path: Path | None = None, bar_root: Path | None = None) -> str:
    faction = _faction_from_prefix(unit_code)
    if faction:
        return faction

    if lua_path is not None:
        for token in _unit_folder_tokens(lua_path, bar_root):
            faction = _faction_from_prefix(token)
            if faction:
                return faction

    return "Other"


def classify_unit_kind(
    unit_code: str,
    lua_path: Path | None = None,
    bar_root: Path | None = None,
    *,
    objectname: str | None = None,
    lua_metadata: LuaUnitMetadata | None = None,
) -> str:
    metadata = lua_metadata or LuaUnitMetadata()
    fields = metadata.fields or {}
    tokens = [token.casefold() for token in _unit_folder_tokens(lua_path, bar_root)] if lua_path else []
    text = " ".join([unit_code, objectname or metadata.objectname or "", *tokens]).casefold()

    if any(hint in tokens or hint in text for hint in OTHER_FOLDER_HINTS):
        return "other"
    if metadata.buildoptions and (
        _has_factory_hint(unit_code, text) or _has_factory_structure_hint(fields, text)
    ):
        return "building"
    if _has_mobile_hint(fields):
        return "unit"
    if any(hint in tokens or hint in text for hint in BUILDING_FOLDER_HINTS):
        return "building"
    if _has_static_structure_hint(fields):
        return "building"
    return "unit"


def classify_unit_types(
    unit_code: str,
    lua_path: Path | None = None,
    bar_root: Path | None = None,
    *,
    objectname: str | None = None,
    display_name: str | None = None,
    description: str | None = None,
    lua_metadata: LuaUnitMetadata | None = None,
) -> tuple[str, ...]:
    metadata = lua_metadata or LuaUnitMetadata()
    fields = metadata.fields or {}
    tokens = [token.casefold() for token in _unit_folder_tokens(lua_path, bar_root)] if lua_path else []
    path_text = " ".join(tokens)
    movement_class = _field_string(fields.get("movementclass")).casefold()
    category = _field_string(fields.get("category")).casefold()
    text = " ".join(
        [
            unit_code,
            objectname or metadata.objectname or "",
            display_name or "",
            description or "",
            category,
            *tokens,
        ]
    ).casefold()
    types: set[str] = set()

    if _has_bot_type_hint(text, path_text, movement_class):
        types.add("bot")
    if _has_aircraft_type_hint(text, path_text, fields):
        types.add("aircraft")
    if _has_naval_type_hint(text, path_text, movement_class, category):
        types.add("naval")
    if _has_vehicle_type_hint(text, path_text, movement_class):
        types.add("vehicle")
    if _has_experimental_type_hint(text, path_text):
        types.add("experimental")

    return _ordered_unit_types(types)


def normalize_faction_filter(value: str) -> str:
    faction = FACTION_FILTERS.get(value.casefold())
    if not faction:
        allowed = ", ".join(FACTION_FILTERS)
        raise BarAssetError(f"Unknown faction '{value}'. Allowed factions: {allowed}")
    return faction


def normalize_kind_filter(value: str) -> str:
    kind = KIND_FILTERS.get(value.casefold())
    if not kind:
        allowed = ", ".join(KIND_FILTERS)
        raise BarAssetError(f"Unknown kind '{value}'. Allowed kinds: {allowed}")
    return kind


def normalize_unit_type_filter(value: str) -> str:
    unit_type = UNIT_TYPE_FILTERS.get(value.casefold())
    if not unit_type:
        allowed = ", ".join(UNIT_TYPE_FILTERS)
        raise BarAssetError(f"Unknown type '{value}'. Allowed types: {allowed}")
    return unit_type


def group_units_by_faction(units: Iterable[UnitAsset]) -> dict[str, list[UnitAsset]]:
    groups = {faction: [] for faction in FACTION_ORDER}
    for unit in units:
        groups.setdefault(unit.faction, []).append(unit)
    for grouped_units in groups.values():
        grouped_units.sort(key=lambda unit: unit.unit_code)
    return groups


def group_units_by_kind(units: Iterable[UnitAsset]) -> dict[str, list[UnitAsset]]:
    groups = {kind: [] for kind in KIND_ORDER}
    for unit in units:
        groups.setdefault(unit.kind, []).append(unit)
    for grouped_units in groups.values():
        grouped_units.sort(key=lambda unit: (unit.faction, unit.display_name.casefold(), unit.unit_code))
    return groups


def group_units_by_type(units: Iterable[UnitAsset]) -> dict[str, list[UnitAsset]]:
    groups = {unit_type: [] for unit_type in UNIT_TYPE_ORDER}
    groups["unclassified"] = []
    for unit in units:
        if not unit.unit_types:
            groups["unclassified"].append(unit)
            continue
        for unit_type in unit.unit_types:
            groups.setdefault(unit_type, []).append(unit)
    for grouped_units in groups.values():
        grouped_units.sort(key=lambda unit: (unit.faction, unit.display_name.casefold(), unit.unit_code))
    return groups


def group_units_by_factory(
    units: Iterable[UnitAsset],
    all_units: Iterable[UnitAsset] | None = None,
) -> dict[str, list[UnitAsset]]:
    unit_list = list(units)
    lookup_units = list(all_units) if all_units is not None else unit_list
    factory_by_code = {unit.unit_code: unit for unit in lookup_units if is_production_factory(unit)}
    ordered_factory_codes = sorted(factory_by_code, key=lambda code: _factory_sort_key(factory_by_code[code]))
    groups: dict[str, list[UnitAsset]] = {}
    no_factory: list[UnitAsset] = []

    for factory_code in ordered_factory_codes:
        grouped = [
            unit
            for unit in unit_list
            if factory_code in unit.built_by and unit.unit_code != factory_code
        ]
        if grouped:
            groups[format_factory_label(factory_by_code[factory_code])] = sorted(
                grouped,
                key=lambda unit: (unit.faction, unit.display_name.casefold(), unit.unit_code),
            )

    for unit in unit_list:
        if not unit.built_by and unit.unit_code not in factory_by_code:
            no_factory.append(unit)
    if no_factory:
        groups["No production factory"] = sorted(
            no_factory,
            key=lambda unit: (unit.faction, unit.display_name.casefold(), unit.unit_code),
        )
    return groups


def filter_units_by_faction(units: Iterable[UnitAsset], faction_filter: str) -> list[UnitAsset]:
    faction = normalize_faction_filter(faction_filter)
    return [unit for unit in units if unit.faction == faction]


def filter_units_by_kind(units: Iterable[UnitAsset], kind_filter: str) -> list[UnitAsset]:
    kind = normalize_kind_filter(kind_filter)
    if kind == "all":
        return list(units)
    return [unit for unit in units if unit.kind == kind]


def filter_units_by_type(units: Iterable[UnitAsset], type_filter: str) -> list[UnitAsset]:
    unit_type = normalize_unit_type_filter(type_filter)
    if unit_type == "all":
        return list(units)
    return [unit for unit in units if unit_type in unit.unit_types]


def is_production_factory(unit: UnitAsset) -> bool:
    if not unit.buildoptions:
        return False
    text = _factory_text(unit)
    if _is_excluded_factory_builder(unit.unit_code, text):
        return False
    return _has_factory_hint(unit.unit_code, text) or unit.kind == "building"


def format_factory_label(unit: UnitAsset | None, fallback_code: str | None = None) -> str:
    if unit is None:
        return fallback_code or "Unknown factory"
    if unit.display_name.casefold() == unit.unit_code.casefold():
        return unit.unit_code
    return f"{unit.display_name} ({unit.unit_code})"


def read_unit_icon_bytes(unit: UnitAsset) -> bytes | None:
    if unit.archive_package and unit.archive_icon_entry:
        entries = _read_sdp_index(str(unit.archive_package))
        entry = entries.get(unit.archive_icon_entry.casefold())
        if not entry:
            return None
        return _read_rapid_entry_bytes(unit.archive_package, entry)
    if unit.icon_path and unit.icon_path.is_file():
        return unit.icon_path.read_bytes()
    return None


def resolve_unit_to_s3o(bar_root: Path, unit_query: str) -> Path:
    query = unit_query.casefold()
    units = list_units(bar_root, require_existing=False)

    for unit in units:
        if unit.unit_code.casefold() == query:
            return _require_s3o(unit)

    for unit in units:
        haystack = (
            f"{unit.unit_code} {unit.display_name} {unit.description} "
            f"{unit.lua_path.as_posix()} {unit.objectname}"
        ).casefold()
        if query in haystack:
            return _require_s3o(unit)

    sample = ", ".join(unit.unit_code for unit in units[:12])
    suffix = f" Available examples: {sample}" if sample else ""
    raise BarAssetError(f"Unit not found for query '{unit_query}'.{suffix}")


def list_units(bar_root: Path, require_existing: bool = True) -> list[UnitAsset]:
    root = bar_root.resolve()
    if _looks_like_rapid_root(root):
        return _list_rapid_units(root, require_existing=require_existing)

    assets: list[UnitAsset] = []
    language = _load_language_metadata_from_root(root)
    for lua_path in iter_unit_lua_files(root):
        text = lua_path.read_text(encoding="utf-8", errors="ignore")
        metadata = parse_unit_lua(text)
        objectname = metadata.objectname
        if not objectname:
            continue
        s3o_path = _objectname_to_s3o_path(root, objectname)
        if require_existing and not s3o_path.is_file():
            continue
        unit_code = lua_path.stem.lower()
        buildpic = metadata.buildpic
        display_name = _display_name(unit_code, objectname, metadata, language)
        description = _description(unit_code, objectname, metadata, language)
        assets.append(
            UnitAsset(
                unit_code=unit_code,
                lua_path=lua_path,
                objectname=objectname,
                s3o_path=s3o_path,
                faction=classify_unit_faction(unit_code, lua_path, root),
                display_name=display_name,
                description=description,
                buildpic=buildpic,
                icon_path=_buildpic_to_icon_path(root, buildpic) if buildpic else None,
                kind=classify_unit_kind(
                    unit_code,
                    lua_path,
                    root,
                    objectname=objectname,
                    lua_metadata=metadata,
                ),
                unit_types=classify_unit_types(
                    unit_code,
                    lua_path,
                    root,
                    objectname=objectname,
                    display_name=display_name,
                    description=description,
                    lua_metadata=metadata,
                ),
                buildoptions=metadata.buildoptions,
            )
        )
    return _with_factory_metadata(assets)


def _candidate_bar_roots(explicit: str | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
        return candidates

    cwd = Path.cwd()
    candidates.extend(_default_bar_roots())
    candidates.extend(
        [
            cwd / "BAR.sdd",
            cwd / "data" / "games" / "BAR.sdd",
            cwd,
        ]
    )
    return _unique_paths(candidates)


def _faction_from_prefix(value: str) -> str | None:
    lowered = value.casefold()
    for prefix, faction in FACTION_BY_PREFIX.items():
        if lowered.startswith(prefix):
            return faction
    return None


def _unit_folder_tokens(lua_path: Path, bar_root: Path | None) -> list[str]:
    if bar_root is not None:
        try:
            return list(lua_path.relative_to(bar_root / "units").parts[:-1])
        except ValueError:
            pass

    parts = list(lua_path.parts)
    for index, part in enumerate(parts):
        if part.casefold() == "units":
            return parts[index + 1 : -1]
    return parts[:-1]


def _extract_lua_scalar_field(lua_text: str, field_name: str) -> str | bool | float | None:
    pattern = re.compile(rf"\b{re.escape(field_name)}\s*=\s*([^,\n\r}}]+)", re.IGNORECASE)
    match = pattern.search(lua_text)
    if not match:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    if raw[0] in {"'", '"'}:
        string_match = re.match(r"[\"']([^\"']*)[\"']", raw)
        return string_match.group(1) if string_match else None
    lowered = raw.casefold()
    if lowered.startswith("true"):
        return True
    if lowered.startswith("false"):
        return False
    number_match = re.match(r"[-+]?\d+(?:\.\d+)?", raw)
    if number_match:
        return float(number_match.group(0))
    word_match = re.match(r"[A-Za-z0-9_./-]+", raw)
    if word_match:
        return word_match.group(0)
    return None


def _extract_lua_table_text(lua_text: str, field_name: str) -> str | None:
    match = re.search(rf"\b{re.escape(field_name)}\s*=\s*\{{", lua_text, re.IGNORECASE)
    if not match:
        return None

    start = lua_text.find("{", match.start())
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(lua_text)):
        char = lua_text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return lua_text[start + 1 : index]
    return None


def _has_mobile_hint(fields: dict[str, str | bool | float]) -> bool:
    if fields.get("canmove") is True or fields.get("canfly") is True:
        return True
    movement_class = fields.get("movementclass")
    if isinstance(movement_class, str) and movement_class.strip().casefold() not in {"", "none"}:
        return True
    for field_name in ("speed", "maxvelocity"):
        value = fields.get(field_name)
        if isinstance(value, (int, float)) and value > 0:
            return True
    return False


def _has_static_structure_hint(fields: dict[str, str | bool | float]) -> bool:
    if fields.get("canmove") is False:
        return True
    if fields.get("levelground") is True or fields.get("activatewhenbuilt") is True:
        return True
    if isinstance(fields.get("yardmap"), str) and str(fields["yardmap"]).strip():
        return True
    footprint_x = fields.get("footprintx")
    footprint_z = fields.get("footprintz")
    return (
        isinstance(footprint_x, (int, float))
        and footprint_x > 0
        and isinstance(footprint_z, (int, float))
        and footprint_z > 0
    )


def _field_string(value: str | bool | float | None) -> str:
    return value if isinstance(value, str) else ""


def _has_bot_type_hint(text: str, path_text: str, movement_class: str) -> bool:
    return (
        "bots" in path_text
        or "kbot" in path_text
        or any(hint in movement_class for hint in ("bot", "kbot", "hbot", "habot"))
        or _has_any_word(text, ("bot", "bots", "kbot", "kbots", "hbot", "habot"))
    )


def _has_aircraft_type_hint(
    text: str,
    path_text: str,
    fields: dict[str, str | bool | float],
) -> bool:
    return (
        "aircraft" in path_text
        or fields.get("canfly") is True
        or _has_any_word(
            text,
            (
                "aircraft",
                "airplane",
                "airplanes",
                "bomber",
                "bombers",
                "fighter",
                "fighters",
                "gunship",
                "gunships",
                "plane",
                "planes",
                "vtol",
            ),
        )
    )


def _has_naval_type_hint(text: str, path_text: str, movement_class: str, category: str) -> bool:
    naval_path_hints = ("ships", "seafactories", "sub")
    naval_movement_hints = ("boat", "ship", "sub", "water")
    return (
        any(hint in path_text for hint in naval_path_hints)
        or any(hint in movement_class for hint in naval_movement_hints)
        or any(hint in category for hint in ("ship", "sub", "water"))
        or _has_any_word(
            text,
            (
                "boat",
                "boats",
                "naval",
                "sea",
                "seaplane",
                "ship",
                "ships",
                "shipyard",
                "shipyards",
                "sub",
                "submarine",
                "submarines",
                "submersible",
                "underwater",
            ),
        )
    )


def _has_vehicle_type_hint(text: str, path_text: str, movement_class: str) -> bool:
    return (
        "vehicles" in path_text
        or any(hint in movement_class for hint in ("tank", "htank", "hover"))
        or _has_any_word(
            text,
            (
                "hover",
                "hovercraft",
                "rover",
                "tank",
                "tanks",
                "vehicle",
                "vehicles",
            ),
        )
    )


def _has_experimental_type_hint(text: str, path_text: str) -> bool:
    return (
        "gantry" in path_text
        or "experimental" in path_text
        or _has_any_word(text, ("experimental", "gantry", "t3"))
    )


def _has_any_word(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(word.casefold())}\b", text) for word in words)


def _ordered_unit_types(unit_types: Iterable[str]) -> tuple[str, ...]:
    values = {unit_type.casefold() for unit_type in unit_types}
    return tuple(unit_type for unit_type in UNIT_TYPE_ORDER if unit_type in values)


def _has_factory_hint(unit_code: str, text: str) -> bool:
    if any(hint in text for hint in PRODUCTION_FACTORY_PATH_HINTS):
        return True
    if any(re.search(rf"\b{re.escape(word)}s?\b", text) for word in PRODUCTION_FACTORY_WORDS):
        return True
    lowered = unit_code.casefold()
    for prefix in FACTION_BY_PREFIX:
        if lowered.startswith(prefix):
            suffix = lowered[len(prefix) :]
            if suffix in PRODUCTION_FACTORY_CODE_SUFFIXES:
                return True
            if any(suffix.endswith(factory_suffix) for factory_suffix in PRODUCTION_FACTORY_CODE_SUFFIXES):
                return True
    return any(lowered.endswith(factory_suffix) for factory_suffix in PRODUCTION_FACTORY_CODE_SUFFIXES)


def _is_excluded_factory_builder(unit_code: str, text: str) -> bool:
    lowered = unit_code.casefold()
    commander_codes = ("armcom", "corcom", "legcom")
    if lowered.startswith(commander_codes) or re.search(r"\bcommander\b", text):
        return True
    return bool(
        re.search(
            r"\b(assist|assistant|builder|construction|constructor|drone|engineer|repair|resurrect)\b",
            text,
        )
        and not _has_factory_hint(unit_code, text)
    )


def _has_factory_structure_hint(fields: dict[str, str | bool | float], text: str) -> bool:
    if any(hint in text for hint in PRODUCTION_FACTORY_PATH_HINTS):
        return True
    return isinstance(fields.get("yardmap"), str) and str(fields["yardmap"]).strip() != ""


def _factory_text(unit: UnitAsset) -> str:
    parts = [
        unit.unit_code,
        unit.display_name,
        unit.description,
        unit.objectname,
        unit.lua_path.as_posix(),
    ]
    return " ".join(parts).casefold()


def _factory_sort_key(unit: UnitAsset) -> tuple[int, str, str]:
    try:
        faction_index = FACTION_ORDER.index(unit.faction)
    except ValueError:
        faction_index = len(FACTION_ORDER)
    return (faction_index, unit.display_name.casefold(), unit.unit_code)


def _with_factory_metadata(units: list[UnitAsset]) -> list[UnitAsset]:
    factories = [unit for unit in units if is_production_factory(unit)]
    factory_by_code = {unit.unit_code: unit for unit in factories}
    unit_by_code = {unit.unit_code: unit for unit in units}
    built_by: dict[str, list[str]] = {}
    for factory in factories:
        for build_code in factory.buildoptions:
            built_by.setdefault(build_code, []).append(factory.unit_code)

    factory_types_by_code: dict[str, tuple[str, ...]] = {}
    for factory in factories:
        factory_types = set(factory.unit_types)
        for build_code in factory.buildoptions:
            built_unit = unit_by_code.get(build_code)
            if built_unit:
                factory_types.update(
                    unit_type for unit_type in built_unit.unit_types if unit_type in CHASSIS_UNIT_TYPES
                )
        factory_types_by_code[factory.unit_code] = _ordered_unit_types(factory_types)

    units_with_factory_types: dict[str, UnitAsset] = {}
    for unit in units:
        factory_types = factory_types_by_code.get(unit.unit_code)
        units_with_factory_types[unit.unit_code] = (
            replace(unit, unit_types=factory_types) if factory_types is not None else unit
        )

    enriched: list[UnitAsset] = []
    for unit in units:
        factory_codes = sorted(
            dict.fromkeys(built_by.get(unit.unit_code, [])),
            key=lambda code: _factory_sort_key(factory_by_code[code]),
        )
        current = units_with_factory_types[unit.unit_code]
        unit_types = set(current.unit_types)
        if factory_codes and all(
            "experimental" in factory_types_by_code.get(factory_code, ())
            for factory_code in factory_codes
        ):
            unit_types.add("experimental")
        enriched.append(
            replace(
                current,
                unit_types=_ordered_unit_types(unit_types),
                built_by=tuple(factory_codes),
            )
        )
    return enriched


def _load_language_metadata_from_root(bar_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    path = bar_root / "language" / "en" / "units.json"
    if not path.is_file():
        return {}, {}
    try:
        return _load_language_metadata(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return {}, {}


def _load_language_metadata(text: str) -> tuple[dict[str, str], dict[str, str]]:
    data = json.loads(text)
    unit_data = data.get("units") if isinstance(data, dict) else None
    if not isinstance(unit_data, dict):
        return {}, {}
    return (
        _coerce_language_map(unit_data.get("names")),
        _coerce_language_map(unit_data.get("descriptions")),
    )


def _coerce_language_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, str):
            result[key.casefold()] = item
    return result


def _display_name(
    unit_code: str,
    objectname: str,
    metadata: LuaUnitMetadata,
    language: tuple[dict[str, str], dict[str, str]],
) -> str:
    names, _ = language
    for key in _unit_language_keys(unit_code):
        value = names.get(key)
        if value:
            return value
    fields = metadata.fields or {}
    for field_name in ("unitname", "name"):
        value = fields.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return unit_code or objectname


def _description(
    unit_code: str,
    objectname: str,
    metadata: LuaUnitMetadata,
    language: tuple[dict[str, str], dict[str, str]],
) -> str:
    _, descriptions = language
    for key in _unit_language_keys(unit_code):
        value = descriptions.get(key)
        if value:
            return value
    fields = metadata.fields or {}
    value = fields.get("description")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return objectname


def _unit_language_keys(unit_code: str) -> tuple[str, ...]:
    code = unit_code.casefold()
    return (code, f"units.names.{code}", f"units.descriptions.{code}")


def _normalize_buildpic(value: str) -> str:
    normalized = value.strip().replace("\\", "/").lstrip("/")
    normalized = re.sub(r"/+", "/", normalized)
    if "." not in Path(normalized).name:
        normalized = f"{normalized}.dds"
    return normalized


def _buildpic_to_icon_path(bar_root: Path, buildpic: str) -> Path:
    normalized = _normalize_buildpic(buildpic)
    if normalized.casefold().startswith("unitpics/"):
        return bar_root / Path(*normalized.split("/"))
    return bar_root / "unitpics" / Path(*normalized.split("/"))


def _buildpic_to_icon_entry(buildpic: str) -> str:
    normalized = _normalize_buildpic(buildpic).casefold()
    if normalized.startswith("unitpics/"):
        return normalized
    return f"unitpics/{normalized}"


def _looks_like_bar_root(path: Path) -> bool:
    return path.is_dir() and (path / "units").is_dir() and (path / "objects3d").is_dir()


def _resolve_bar_root_candidate(path: Path) -> Path | None:
    root = path.expanduser().resolve()
    for candidate in (root, root / "data"):
        if _looks_like_bar_root(candidate):
            return candidate
        if _looks_like_rapid_root(candidate):
            return candidate
    return None


def _looks_like_rapid_root(path: Path) -> bool:
    return path.is_dir() and (path / "packages").is_dir() and (path / "pool").is_dir()


def _default_bar_roots() -> list[Path]:
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        local_root = Path(local_app_data)
        candidates.extend(
            [
                local_root / "Programs" / "Beyond-All-Reason" / "data" / "games" / "BAR.sdd",
                local_root / "Beyond-All-Reason" / "data" / "games" / "BAR.sdd",
                local_root / "beyond-all-reason" / "data" / "games" / "BAR.sdd",
            ]
        )

    app_data = os.environ.get("APPDATA")
    if app_data:
        candidates.append(Path(app_data) / "Beyond-All-Reason" / "data" / "games" / "BAR.sdd")
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        candidates.append(Path(program_data) / "Beyond-All-Reason" / "data" / "games" / "BAR.sdd")
    return candidates


def _default_program_install_roots() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(env_name)
        if value:
            install_root = Path(value) / "Beyond-All-Reason"
            candidates.extend([install_root / "data", install_root])
    return candidates


def _bar_search_roots() -> list[Path]:
    roots: list[Path] = []
    for env_name in ("LOCALAPPDATA", "APPDATA", "PROGRAMDATA"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value))
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        profile = Path(user_profile)
        roots.extend([profile / "Documents", profile / "Games", profile / "Saved Games"])
    roots.extend([Path.cwd(), Path.cwd().parent])
    return [path for path in _unique_paths(roots) if path.is_dir()]


def _walk_for_bar_roots(root: Path, max_depth: int = 8) -> Iterable[Path]:
    stack = [root]
    root_depth = len(root.parts)
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if not is_dir:
                continue
            path = Path(entry.path)
            if entry.name.casefold() == "bar.sdd":
                if _looks_like_bar_root(path):
                    yield path
                continue
            if entry.name.casefold() in SKIP_DISCOVERY_DIRS:
                continue
            if len(path.parts) - root_depth >= max_depth:
                continue
            stack.append(path)


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _normalize_objectname(value: str) -> str:
    normalized = value.strip().replace("\\", "/").lstrip("/")
    return re.sub(r"/+", "/", normalized)


def _objectname_to_s3o_path(bar_root: Path, objectname: str) -> Path:
    normalized = _normalize_objectname(objectname)
    if normalized.casefold().startswith("objects3d/"):
        relative = Path(*normalized.split("/"))
    else:
        relative = Path("objects3d", *normalized.split("/"))
    return bar_root / relative


def ensure_unit_s3o(unit: UnitAsset) -> Path:
    return _require_s3o(unit)


def _require_s3o(unit: UnitAsset) -> Path:
    if unit.archive_package and unit.archive_s3o_entry:
        return _extract_rapid_entry(unit.archive_package, unit.archive_s3o_entry)
    if not unit.s3o_path.is_file():
        raise BarAssetError(
            f"Unit '{unit.unit_code}' references '{unit.objectname}', "
            f"but the S3O file does not exist at {unit.s3o_path}"
        )
    return unit.s3o_path


def _list_rapid_units(data_root: Path, require_existing: bool) -> list[UnitAsset]:
    package = _find_rapid_game_package(data_root)
    entries = _read_sdp_index(str(package))
    language = _load_rapid_language_metadata(package, entries)
    assets: list[UnitAsset] = []
    for entry in sorted(entries.values(), key=lambda item: item.path):
        path_key = entry.path.casefold()
        if not path_key.startswith("units/") or not path_key.endswith(".lua"):
            continue
        text = _read_rapid_entry_text(package, entry)
        metadata = parse_unit_lua(text)
        objectname = metadata.objectname
        if not objectname:
            continue
        s3o_entry_key = _rapid_objectname_key(objectname)
        s3o_entry = entries.get(s3o_entry_key)
        if require_existing and not s3o_entry:
            continue
        unit_code = Path(entry.path).stem.lower()
        s3o_path = _rapid_cache_path(package, s3o_entry.path if s3o_entry else s3o_entry_key)
        buildpic = metadata.buildpic
        icon_entry_key = _buildpic_to_icon_entry(buildpic) if buildpic else None
        icon_entry = entries.get(icon_entry_key) if icon_entry_key else None
        display_name = _display_name(unit_code, objectname, metadata, language)
        description = _description(unit_code, objectname, metadata, language)
        assets.append(
            UnitAsset(
                unit_code=unit_code,
                lua_path=data_root / entry.path,
                objectname=objectname,
                s3o_path=s3o_path,
                faction=classify_unit_faction(unit_code, Path(entry.path), data_root),
                archive_package=package,
                archive_s3o_entry=s3o_entry.path if s3o_entry else None,
                display_name=display_name,
                description=description,
                buildpic=buildpic,
                icon_path=_rapid_cache_path(package, icon_entry.path if icon_entry else icon_entry_key)
                if icon_entry_key
                else None,
                kind=classify_unit_kind(
                    unit_code,
                    Path(entry.path),
                    data_root,
                    objectname=objectname,
                    lua_metadata=metadata,
                ),
                unit_types=classify_unit_types(
                    unit_code,
                    Path(entry.path),
                    data_root,
                    objectname=objectname,
                    display_name=display_name,
                    description=description,
                    lua_metadata=metadata,
                ),
                buildoptions=metadata.buildoptions,
                archive_icon_entry=icon_entry.path if icon_entry else None,
            )
        )
    return _with_factory_metadata(assets)


def _find_rapid_game_package(data_root: Path) -> Path:
    packages = sorted(
        (data_root / "packages").glob("*.sdp"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    best: tuple[int, int, float, Path] | None = None
    for package in packages:
        try:
            entries = _read_sdp_index(str(package))
        except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError, ValueError):
            continue
        unit_count = sum(1 for path in entries if path.startswith("units/") and path.endswith(".lua"))
        model_count = sum(1 for path in entries if path.startswith("objects3d/") and path.endswith(".s3o"))
        if unit_count and model_count:
            candidate = (unit_count, model_count, package.stat().st_mtime, package)
            if best is None or candidate[:3] > best[:3]:
                best = candidate
    if best:
        return best[3]
    raise BarAssetError(
        f"Could not find a BAR rapid game package under {data_root / 'packages'}."
    )


@lru_cache(maxsize=16)
def _read_sdp_index(package: str) -> dict[str, RapidPackageEntry]:
    data = gzip.decompress(Path(package).read_bytes())
    entries: dict[str, RapidPackageEntry] = {}
    position = 0
    while position < len(data):
        name_length = data[position]
        position += 1
        name = data[position : position + name_length].decode("utf-8")
        position += name_length
        hash_hex = data[position : position + 16].hex()
        position += 16
        position += 4
        size = int.from_bytes(data[position : position + 4], "big")
        position += 4
        entries[name.casefold()] = RapidPackageEntry(name, hash_hex, size)
    return entries


def _rapid_objectname_key(objectname: str) -> str:
    normalized = _normalize_objectname(objectname).casefold()
    if normalized.startswith("objects3d/"):
        return normalized
    return f"objects3d/{normalized}"


def _read_rapid_entry_text(package: Path, entry: RapidPackageEntry) -> str:
    return _read_rapid_entry_bytes(package, entry).decode("utf-8", errors="ignore")


def _load_rapid_language_metadata(
    package: Path,
    entries: dict[str, RapidPackageEntry],
) -> tuple[dict[str, str], dict[str, str]]:
    entry = entries.get("language/en/units.json")
    if not entry:
        return {}, {}
    try:
        return _load_language_metadata(_read_rapid_entry_text(package, entry))
    except (BarAssetError, json.JSONDecodeError, UnicodeDecodeError):
        return {}, {}


def _extract_rapid_entry(package: Path, entry_path: str) -> Path:
    entries = _read_sdp_index(str(package))
    entry = entries.get(entry_path.casefold())
    if not entry:
        raise BarAssetError(f"Rapid package entry not found: {entry_path}")
    out_path = _rapid_cache_path(package, entry.path)
    if out_path.is_file() and out_path.stat().st_size == entry.size:
        _extract_rapid_s3o_textures(package, entry, out_path.read_bytes())
        return out_path
    data = _read_rapid_entry_bytes(package, entry)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    _extract_rapid_s3o_textures(package, entry, data)
    return out_path


def _read_rapid_entry_bytes(package: Path, entry: RapidPackageEntry) -> bytes:
    data_root = package.parent.parent
    pool_file = data_root / "pool" / entry.hash_hex[:2] / f"{entry.hash_hex[2:]}.gz"
    if not pool_file.is_file():
        raise BarAssetError(f"Rapid pool file missing for {entry.path}: {pool_file}")
    return gzip.decompress(pool_file.read_bytes())


def _extract_rapid_s3o_textures(package: Path, entry: RapidPackageEntry, s3o_data: bytes) -> None:
    if not entry.path.casefold().endswith(".s3o"):
        return
    textures_dir = _rapid_package_cache_root(package) / "unittextures"
    textures_dir.mkdir(parents=True, exist_ok=True)
    entries = _read_sdp_index(str(package))
    for texture_name in _s3o_texture_names(s3o_data):
        texture_entry = entries.get(f"unittextures/{texture_name.casefold()}")
        if not texture_entry:
            continue
        out_path = _rapid_cache_path(package, texture_entry.path)
        if out_path.is_file() and out_path.stat().st_size == texture_entry.size:
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_read_rapid_entry_bytes(package, texture_entry))


def _s3o_texture_names(data: bytes) -> tuple[str, ...]:
    header_size = struct.calcsize("<12sI5f4I")
    if len(data) < header_size:
        return ()
    try:
        header = struct.unpack_from("<12sI5f4I", data, 0)
    except struct.error:
        return ()
    texture_names: list[str] = []
    for offset in (header[9], header[10]):
        if offset <= 0 or offset >= len(data):
            continue
        end = data.find(b"\0", offset)
        if end < 0:
            end = len(data)
        try:
            name = data[offset:end].decode("ascii", errors="ignore").strip()
        except UnicodeDecodeError:
            continue
        name = Path(*name.replace("\\", "/").split("/")).name
        if name:
            texture_names.append(name)
    return tuple(dict.fromkeys(texture_names))


def _rapid_cache_path(package: Path, entry_path: str) -> Path:
    return _rapid_package_cache_root(package) / Path(*entry_path.split("/"))


def _rapid_package_cache_root(package: Path) -> Path:
    return cache_dir() / "rapid" / package.stem
