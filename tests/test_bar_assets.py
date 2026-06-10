import gzip
import hashlib
from pathlib import Path

import pytest

from barprint.bar_assets import (
    classify_unit_faction,
    classify_unit_kind,
    classify_unit_types,
    extract_buildoptions,
    extract_buildpic,
    extract_objectname,
    find_bar_root,
    group_units_by_factory,
    group_units_by_faction,
    group_units_by_type,
    is_production_factory,
    list_units,
    parse_unit_lua,
    resolve_unit_to_s3o,
)


def make_bar_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "BAR.sdd"
    (root / "units" / "Core").mkdir(parents=True)
    (root / "objects3d" / "Units").mkdir(parents=True)
    (root / "units" / "Core" / "corak.lua").write_text(
        """
        return {
          corak = {
            objectname = "Units/CORAK.s3o",
          }
        }
        """,
        encoding="utf-8",
    )
    (root / "objects3d" / "Units" / "CORAK.s3o").write_bytes(b"s3o")
    return root


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('objectname = "Units/CORAK.s3o"', "Units/CORAK.s3o"),
        ("objectName='Units/foo.s3o'", "Units/foo.s3o"),
        (r'objectname = "Units\\BAR.s3o"', "Units/BAR.s3o"),
        ("name = 'not_a_model'", None),
    ],
)
def test_extract_objectname(text: str, expected: str | None) -> None:
    assert extract_objectname(text) == expected


def test_parse_unit_lua_metadata() -> None:
    text = """
    return {
      armflea = {
        objectname = "Units/ARMFLEA.s3o",
        buildpic = "ARMFLEA.DDS",
        canmove = true,
        movementclass = "KBOT2",
        speed = 93,
        buildoptions = {
          [1] = "armmex",
          [2] = "armsolar",
          [3] = "armmex",
        },
      }
    }
    """

    metadata = parse_unit_lua(text)

    assert metadata.objectname == "Units/ARMFLEA.s3o"
    assert metadata.buildpic == "ARMFLEA.DDS"
    assert metadata.buildoptions == ("armmex", "armsolar")
    assert metadata.fields["canmove"] is True
    assert metadata.fields["movementclass"] == "KBOT2"
    assert metadata.fields["speed"] == 93
    assert extract_buildpic(text) == "ARMFLEA.DDS"
    assert extract_buildoptions(text) == ("armmex", "armsolar")


def test_list_units_resolves_existing_s3o(tmp_path: Path) -> None:
    root = make_bar_fixture(tmp_path)
    units = list_units(root)
    assert len(units) == 1
    assert units[0].unit_code == "corak"
    assert units[0].faction == "Cortex"
    assert units[0].s3o_path == root / "objects3d" / "Units" / "CORAK.s3o"


def test_list_units_reads_unpacked_metadata(tmp_path: Path) -> None:
    root = tmp_path / "BAR.sdd"
    add_unit(
        root,
        "ArmBots",
        "armflea",
        "ARMFLEA",
        extra='buildpic = "ARMFLEA.DDS", canmove = true, movementclass = "KBOT2",',
    )
    (root / "unitpics").mkdir(parents=True)
    (root / "unitpics" / "ARMFLEA.DDS").write_bytes(b"dds")
    write_language(root, {"armflea": "Flea"}, {"armflea": "Fast scout bot"})

    unit = list_units(root)[0]

    assert unit.display_name == "Flea"
    assert unit.description == "Fast scout bot"
    assert unit.buildpic == "ARMFLEA.DDS"
    assert unit.icon_path == root / "unitpics" / "ARMFLEA.DDS"
    assert unit.kind == "unit"
    assert unit.unit_types == ("bot",)


def test_resolve_unit_to_s3o_case_insensitive(tmp_path: Path) -> None:
    root = make_bar_fixture(tmp_path)
    assert resolve_unit_to_s3o(root, "CORAK") == root / "objects3d" / "Units" / "CORAK.s3o"


@pytest.mark.parametrize(
    ("unit_code", "folder", "expected"),
    [
        ("armflea", "Unknown", "Armada"),
        ("corak", "Unknown", "Cortex"),
        ("legrail", "Unknown", "Legion"),
        ("scavboss", "Unknown", "Scavengers"),
        ("raptorh1", "Unknown", "Raptors"),
        ("flea", "ArmBots", "Armada"),
        ("ak", "Core", "Cortex"),
        ("boss", "Scavengers", "Scavengers"),
        ("h1", "Raptors", "Raptors"),
        ("scout", "Other", "Other"),
    ],
)
def test_classify_unit_faction(unit_code: str, folder: str, expected: str, tmp_path: Path) -> None:
    root = tmp_path / "BAR.sdd"
    lua_path = root / "units" / folder / f"{unit_code}.lua"
    assert classify_unit_faction(unit_code, lua_path, root) == expected


@pytest.mark.parametrize(
    ("unit_code", "folder", "lua_text", "expected"),
    [
        ("armflea", "ArmBots", 'objectname = "Units/ARMFLEA.s3o", canmove = true, speed = 90', "unit"),
        ("armsolar", "ArmBuildings", 'objectname = "Units/ARMSOLAR.s3o", footprintx = 4, footprintz = 4', "building"),
        (
            "armlab",
            "ArmBuildings",
            'objectname = "Units/ARMLAB.s3o", buildoptions = { "armflea" }',
            "building",
        ),
        ("debugtarget", "Other", 'objectname = "Units/DEBUG.s3o"', "other"),
    ],
)
def test_classify_unit_kind(unit_code: str, folder: str, lua_text: str, expected: str, tmp_path: Path) -> None:
    root = tmp_path / "BAR.sdd"
    lua_path = root / "units" / folder / f"{unit_code}.lua"
    metadata = parse_unit_lua(f"return {{ {unit_code} = {{ {lua_text} }} }}")

    assert classify_unit_kind(unit_code, lua_path, root, lua_metadata=metadata) == expected


@pytest.mark.parametrize(
    ("unit_code", "folder", "lua_text", "display_name", "description", "expected"),
    [
        ("armflea", "ArmBots", 'objectname = "Units/ARMFLEA.s3o"', "", "", ("bot",)),
        ("armaak", "ArmBots", 'objectname = "Units/ARMAAK.s3o"', "Archangel", "Anti-Air Bot", ("bot",)),
        ("armawac", "ArmAircraft", 'objectname = "Units/ARMAWAC.s3o", canfly = true', "", "", ("aircraft",)),
        ("armstump", "Unknown", 'objectname = "Units/ARMSTUMP.s3o", movementclass = "TANK3"', "", "", ("vehicle",)),
        ("corsub", "Unknown", 'objectname = "Units/CORSUB.s3o"', "Serpent", "Attack submarine", ("naval",)),
        (
            "armmar",
            "ArmBots/T3",
            'objectname = "Units/ARMMAR.s3o", movementclass = "KBOT2"',
            "Marathon",
            "Experimental assault bot",
            ("bot", "experimental"),
        ),
        (
            "armthor",
            "ArmVehicles",
            'objectname = "Units/ARMTHOR.s3o", movementclass = "HTANK3"',
            "Thor",
            "Experimental tank",
            ("vehicle", "experimental"),
        ),
    ],
)
def test_classify_unit_types(
    unit_code: str,
    folder: str,
    lua_text: str,
    display_name: str,
    description: str,
    expected: tuple[str, ...],
    tmp_path: Path,
) -> None:
    root = tmp_path / "BAR.sdd"
    lua_path = root / "units" / folder / f"{unit_code}.lua"
    metadata = parse_unit_lua(f"return {{ {unit_code} = {{ {lua_text} }} }}")

    assert (
        classify_unit_types(
            unit_code,
            lua_path,
            root,
            lua_metadata=metadata,
            display_name=display_name,
            description=description,
        )
        == expected
    )


def test_group_units_by_faction(tmp_path: Path) -> None:
    root = tmp_path / "BAR.sdd"
    add_unit(root, "ArmBots", "armflea", "ARMFLEA")
    add_unit(root, "CorBots", "corak", "CORAK")
    add_unit(root, "Scavengers", "boss", "SCAVBOSS")
    add_unit(root, "Raptors", "h1", "RAPTORH1")

    groups = group_units_by_faction(list_units(root))

    assert [unit.unit_code for unit in groups["Armada"]] == ["armflea"]
    assert [unit.unit_code for unit in groups["Cortex"]] == ["corak"]
    assert [unit.unit_code for unit in groups["Scavengers"]] == ["boss"]
    assert [unit.unit_code for unit in groups["Raptors"]] == ["h1"]


def test_factory_reverse_index_excludes_generic_builders(tmp_path: Path) -> None:
    root = tmp_path / "BAR.sdd"
    add_unit(root, "ArmBots", "armflea", "ARMFLEA", extra="canmove = true,")
    add_unit(root, "ArmBots", "armwar", "ARMWAR", extra="canmove = true,")
    add_unit(
        root,
        "ArmBuildings",
        "armlab",
        "ARMLAB",
        extra='buildoptions = { "armflea", "armwar" }, footprintx = 8, footprintz = 8,',
    )
    add_unit(
        root,
        "ArmBots",
        "armck",
        "ARMCK",
        extra='name = "Construction Bot", canmove = true, buildoptions = { "armsolar" },',
    )
    add_unit(
        root,
        "ArmBots",
        "armcom",
        "ARMCOM",
        extra='name = "Commander", canmove = true, buildoptions = { "armsolar" },',
    )

    units = list_units(root)
    by_code = {unit.unit_code: unit for unit in units}
    groups = group_units_by_factory(units)

    assert is_production_factory(by_code["armlab"])
    assert not is_production_factory(by_code["armck"])
    assert not is_production_factory(by_code["armcom"])
    assert by_code["armflea"].built_by == ("armlab",)
    assert by_code["armwar"].built_by == ("armlab",)
    assert "armlab" in groups
    assert [unit.unit_code for unit in groups["armlab"]] == ["armflea", "armwar"]


def test_factory_types_come_from_buildoptions_and_experimental_inheritance(tmp_path: Path) -> None:
    root = tmp_path / "BAR.sdd"
    add_unit(root, "ArmBots", "armflea", "ARMFLEA", extra='movementclass = "KBOT2",')
    add_unit(root, "ArmVehicles", "armthor", "ARMTHOR", extra='movementclass = "HTANK3",')
    add_unit(
        root,
        "ArmBuildings",
        "armlab",
        "ARMLAB",
        extra='name = "Factory", buildoptions = { "armflea" }, footprintx = 8, footprintz = 8,',
    )
    add_unit(
        root,
        "ArmGantry",
        "armgant",
        "ARMGANT",
        extra='name = "Experimental Gantry", buildoptions = { "armthor" }, footprintx = 10, footprintz = 10,',
    )

    units = list_units(root)
    by_code = {unit.unit_code: unit for unit in units}

    assert by_code["armlab"].kind == "building"
    assert by_code["armlab"].unit_types == ("bot",)
    assert by_code["armgant"].unit_types == ("vehicle", "experimental")
    assert by_code["armthor"].unit_types == ("vehicle", "experimental")


def test_units_built_by_standard_and_experimental_factories_do_not_inherit_experimental(tmp_path: Path) -> None:
    root = tmp_path / "BAR.sdd"
    add_unit(
        root,
        "ArmBots",
        "armaak",
        "ARMAAK",
        extra='description = "Advanced Amphibious Anti-Air Bot", movementclass = "KBOT2",',
    )
    add_unit(
        root,
        "ArmBuildings",
        "armalab",
        "ARMALAB",
        extra='name = "Advanced Bot Lab", buildoptions = { "armaak" }, footprintx = 8, footprintz = 8,',
    )
    add_unit(
        root,
        "ArmBuildings/SeaFactories",
        "armamsub",
        "ARMAMSUB",
        extra='name = "Amphibious Complex", canmove = true, yardmap = "oooooo", buildoptions = { "armaak" }, footprintx = 6, footprintz = 6,',
    )
    add_unit(
        root,
        "ArmBuildings",
        "armhalab",
        "ARMHALAB",
        extra='name = "Experimental Bot Lab", buildoptions = { "armaak" }, footprintx = 8, footprintz = 8,',
    )

    by_code = {unit.unit_code: unit for unit in list_units(root)}

    assert by_code["armamsub"].kind == "building"
    assert is_production_factory(by_code["armamsub"])
    assert by_code["armaak"].built_by == ("armalab", "armamsub", "armhalab")
    assert by_code["armaak"].unit_types == ("bot",)


def test_mobile_builders_are_not_production_factories(tmp_path: Path) -> None:
    root = tmp_path / "BAR.sdd"
    add_unit(root, "ArmBots", "armflea", "ARMFLEA", extra='movementclass = "KBOT2",')
    add_unit(
        root,
        "ArmBots",
        "armhack",
        "ARMFARK",
        extra='name = "Advanced Construction Bot", builder = true, canmove = true, movementclass = "BOT3", speed = 75, buildoptions = { "armflea" }, footprintx = 2, footprintz = 2,',
    )

    by_code = {unit.unit_code: unit for unit in list_units(root)}

    assert not is_production_factory(by_code["armhack"])
    assert by_code["armflea"].built_by == ()


def test_group_units_by_type_allows_overlapping_membership(tmp_path: Path) -> None:
    root = tmp_path / "BAR.sdd"
    add_unit(root, "ArmBots/T3", "armmar", "ARMMAR", extra='movementclass = "KBOT2",')
    add_unit(root, "Misc", "marker", "MARKER")

    groups = group_units_by_type(list_units(root))

    assert [unit.unit_code for unit in groups["bot"]] == ["armmar"]
    assert [unit.unit_code for unit in groups["experimental"]] == ["armmar"]
    assert [unit.unit_code for unit in groups["unclassified"]] == ["marker"]


def test_find_bar_root_discovers_nested_appdata_install(tmp_path: Path, monkeypatch) -> None:
    local_app_data = tmp_path / "Local"
    root = make_bar_fixture(local_app_data / "Custom" / "Nested" / "Install" / "data" / "games")
    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.chdir(empty_cwd)

    assert find_bar_root(None) == root.resolve()

def test_find_bar_root_checks_appdata_location(tmp_path: Path, monkeypatch) -> None:
    appdata = tmp_path / "Roaming"
    root = make_bar_fixture(appdata / "Beyond-All-Reason" / "data" / "games")
    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.chdir(empty_cwd)

    assert find_bar_root(None) == root.resolve()


def test_find_bar_root_checks_program_files_rapid_install(tmp_path: Path, monkeypatch) -> None:
    program_files = tmp_path / "Program Files"
    data_root = make_rapid_install(program_files / "Beyond-All-Reason")
    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.chdir(empty_cwd)

    assert find_bar_root(None) == data_root.resolve()


def test_list_units_reads_rapid_install(tmp_path: Path, monkeypatch) -> None:
    data_root = make_rapid_install(tmp_path / "Beyond-All-Reason")
    workdir = tmp_path / "work"
    local_app_data = tmp_path / "LocalAppData"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    units = list_units(data_root)
    corak = next(unit for unit in units if unit.unit_code == "corak")

    assert corak.faction == "Cortex"
    assert corak.objectname == "Units/CORAK.s3o"
    assert corak.display_name == "Grunt"
    assert corak.description == "Light raider"
    assert corak.buildpic == "CORAK.DDS"
    assert corak.archive_icon_entry == "unitpics/CORAK.DDS"
    assert corak.kind == "unit"
    assert corak.unit_types == ("bot",)
    assert corak.archive_package == data_root / "packages" / "bar-game.sdp"
    assert corak.archive_s3o_entry == "objects3d/units/corak.s3o"

    extracted = resolve_unit_to_s3o(data_root, "corak")
    assert extracted == (
        local_app_data / "barprint" / "cache" / "rapid" / "bar-game" / "objects3d" / "units" / "corak.s3o"
    )
    assert extracted.read_bytes() == b"corak s3o"


def add_unit(root: Path, folder: str, code: str, model: str, extra: str = "") -> None:
    (root / "units" / folder).mkdir(parents=True, exist_ok=True)
    (root / "objects3d" / "Units").mkdir(parents=True, exist_ok=True)
    (root / "units" / folder / f"{code}.lua").write_text(
        f'return {{ {code} = {{ objectname = "Units/{model}.s3o", {extra} }} }}',
        encoding="utf-8",
    )
    (root / "objects3d" / "Units" / f"{model}.s3o").write_bytes(b"s3o")


def write_language(root: Path, names: dict[str, str], descriptions: dict[str, str]) -> None:
    language = root / "language" / "en"
    language.mkdir(parents=True, exist_ok=True)
    names_json = ", ".join(f'"{key}": "{value}"' for key, value in names.items())
    descriptions_json = ", ".join(f'"{key}": "{value}"' for key, value in descriptions.items())
    (language / "units.json").write_text(
        f'{{"units": {{"names": {{{names_json}}}, "descriptions": {{{descriptions_json}}}}}}}',
        encoding="utf-8",
    )


def make_rapid_install(install_root: Path) -> Path:
    data_root = install_root / "data"
    package = data_root / "packages" / "bar-game.sdp"
    records: list[tuple[str, str, int]] = []

    add_rapid_entry(
        data_root,
        records,
        "units/corbots/corak.lua",
        b'return { corak = { objectname = "Units/CORAK.s3o", buildpic = "CORAK.DDS", canmove = true } }',
    )
    add_rapid_entry(
        data_root,
        records,
        "units/armbots/armflea.lua",
        b'return { armflea = { objectname = "Units/ARMFLEA.s3o", canmove = true } }',
    )
    add_rapid_entry(data_root, records, "objects3d/units/corak.s3o", b"corak s3o")
    add_rapid_entry(data_root, records, "objects3d/units/armflea.s3o", b"armflea s3o")
    add_rapid_entry(data_root, records, "unitpics/CORAK.DDS", b"corak dds")
    add_rapid_entry(
        data_root,
        records,
        "language/en/units.json",
        b'{"units": {"names": {"corak": "Grunt"}, "descriptions": {"corak": "Light raider"}}}',
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
