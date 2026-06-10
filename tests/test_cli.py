import json
from pathlib import Path
import sys

from rich.text import Text

from barprint.cli import main
from barprint.config import portable_config_path, user_config_path


def test_export_requires_source(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    code = main(["export", "--s3o-importer", "missing.py", "--out", "out"])
    captured = capsys.readouterr()
    assert code == 2
    assert "Either --s3o or --unit is required" in captured.err


def test_export_rejects_mixed_sources(tmp_path: Path, capsys) -> None:
    s3o = tmp_path / "model.s3o"
    s3o.write_bytes(b"s3o")
    code = main(
        [
            "export",
            "--s3o",
            str(s3o),
            "--bar-root",
            str(tmp_path),
            "--unit",
            "corak",
            "--s3o-importer",
            "missing.py",
            "--out",
            "out",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "Use either --s3o" in captured.err


def test_list_units_uses_configured_bar_root(tmp_path: Path, capsys) -> None:
    bar_root = make_bar_fixture(tmp_path)
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")

    code = main(["list-units", "--config", str(config)])

    captured = capsys.readouterr()
    assert code == 0
    assert "corak" in captured.out
    assert "Cortex" in captured.out
    assert "Units/CORAK.s3o" in captured.out


def test_list_units_by_faction_groups_units(tmp_path: Path, capsys) -> None:
    bar_root = make_bar_fixture(tmp_path)
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")

    code = main(["list-units", "--config", str(config), "--by-faction"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Armada (1)" in captured.out
    assert "Cortex (1)" in captured.out
    assert "Scavengers (1)" in captured.out
    assert "Raptors (1)" in captured.out
    assert "armflea" in captured.out
    assert "corak" in captured.out
    assert "boss" in captured.out
    assert "h1" in captured.out


def test_list_units_filters_faction(tmp_path: Path, capsys) -> None:
    bar_root = make_bar_fixture(tmp_path)
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")

    code = main(["list-units", "--config", str(config), "--faction", "armada"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Armada (1)" in captured.out
    assert "armflea" in captured.out
    assert "corak" not in captured.out


def test_list_units_filters_scavs_and_raptors(tmp_path: Path, capsys) -> None:
    bar_root = make_bar_fixture(tmp_path)
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")

    scavs_code = main(["list-units", "--config", str(config), "--faction", "scavs"])
    scavs_output = capsys.readouterr().out
    raptors_code = main(["list-units", "--config", str(config), "--faction", "raptors"])
    raptors_output = capsys.readouterr().out

    assert scavs_code == 0
    assert "Scavengers (1)" in scavs_output
    assert "boss" in scavs_output
    assert "Scavengers" in scavs_output
    assert "h1" not in scavs_output
    assert raptors_code == 0
    assert "Raptors (1)" in raptors_output
    assert "h1" in raptors_output
    assert "Raptors" in raptors_output
    assert "boss" not in raptors_output


def test_list_units_filters_kind(tmp_path: Path, capsys) -> None:
    bar_root = make_bar_fixture(tmp_path)
    add_unit(
        bar_root,
        "ArmBuildings",
        "armsolar",
        "ARMSOLAR",
        extra="footprintx = 4, footprintz = 4,",
    )
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")

    code = main(["list-units", "--config", str(config), "--kind", "building"])

    captured = capsys.readouterr()
    assert code == 0
    assert "armsolar" in captured.out
    assert "armflea" not in captured.out


def test_list_units_filters_type(tmp_path: Path, capsys) -> None:
    bar_root = make_bar_fixture(tmp_path)
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")

    code = main(["list-units", "--config", str(config), "--type", "bot"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Type" in captured.out
    assert "Bot" in captured.out
    assert "armflea" in captured.out
    assert "corak" not in captured.out


def test_list_units_filters_experimental_type(tmp_path: Path, capsys) -> None:
    bar_root = tmp_path / "BAR.sdd"
    add_unit(bar_root, "ArmVehicles", "armstump", "ARMSTUMP", extra='movementclass = "TANK3",')
    add_unit(
        bar_root,
        "ArmVehicles",
        "armthor",
        "ARMTHOR",
        extra='name = "Thor", description = "Experimental tank", movementclass = "HTANK3",',
    )
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")

    code = main(["list-units", "--config", str(config), "--type", "experimental"])

    captured = capsys.readouterr()
    assert code == 0
    assert "armthor" in captured.out
    assert "Experimental" in captured.out
    assert "armstump" not in captured.out


def test_list_units_filters_kind_and_type(tmp_path: Path, capsys) -> None:
    bar_root = tmp_path / "BAR.sdd"
    add_unit(bar_root, "ArmBots", "armflea", "ARMFLEA", extra='movementclass = "KBOT2",')
    add_unit(
        bar_root,
        "ArmBuildings",
        "armlab",
        "ARMLAB",
        extra='name = "Bot Lab", buildoptions = { "armflea" }, footprintx = 8, footprintz = 8,',
    )
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")

    code = main(["list-units", "--config", str(config), "--kind", "building", "--type", "bot"])

    captured = capsys.readouterr()
    assert code == 0
    assert "armlab" in captured.out
    assert "armflea" not in captured.out


def test_list_units_groups_by_type_with_overlap(tmp_path: Path, capsys) -> None:
    bar_root = tmp_path / "BAR.sdd"
    add_unit(
        bar_root,
        "ArmVehicles",
        "armthor",
        "ARMTHOR",
        extra='description = "Experimental tank", movementclass = "HTANK3",',
    )
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")

    code = main(["list-units", "--config", str(config), "--group-by", "type"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Vehicle (1)" in captured.out
    assert "Experimental (1)" in captured.out
    assert captured.out.count("armthor") >= 2


def test_list_units_groups_by_factory(tmp_path: Path, capsys) -> None:
    bar_root = tmp_path / "BAR.sdd"
    add_unit(bar_root, "ArmBots", "armflea", "ARMFLEA", extra="canmove = true,")
    add_unit(
        bar_root,
        "ArmBuildings",
        "armlab",
        "ARMLAB",
        extra='buildoptions = { "armflea" }, footprintx = 8, footprintz = 8,',
    )
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")

    code = main(["list-units", "--config", str(config), "--group-by", "factory", "--kind", "unit"])

    captured = capsys.readouterr()
    assert code == 0
    assert "armlab (1)" in captured.out
    assert "armflea" in captured.out


def test_list_units_with_icons_flag_shows_icon_column(tmp_path: Path, capsys, monkeypatch) -> None:
    bar_root = make_bar_fixture(tmp_path)
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")
    monkeypatch.setattr("barprint.cli.render_unit_icon", lambda unit, size, **kwargs: Text("ICON"))

    code = main(["list-units", "--config", str(config), "--with-icons", "--icon-size", "4"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Icon" in captured.out
    assert "ICON" in captured.out
    assert "Source" in captured.out
    assert "Model" not in captured.out


def test_info_unit_outputs_metadata_and_factories(tmp_path: Path, capsys) -> None:
    bar_root = tmp_path / "BAR.sdd"
    add_unit(bar_root, "ArmBots", "armflea", "ARMFLEA", extra="canmove = true,")
    add_unit(
        bar_root,
        "ArmBuildings",
        "armlab",
        "ARMLAB",
        extra='buildoptions = { "armflea" }, footprintx = 8, footprintz = 8,',
    )
    write_language(bar_root, {"armflea": "Flea"}, {"armflea": "Fast scout bot"})
    config = tmp_path / "barprint.local.json"
    config.write_text(json.dumps({"bar_root": str(bar_root)}), encoding="utf-8")

    code = main(["info", "--config", str(config), "--unit", "armflea"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Flea (armflea)" in captured.out
    assert "Fast scout bot" in captured.out
    assert "Built by" in captured.out
    assert "Source" in captured.out
    assert "Type" in captured.out
    assert "Bot" in captured.out
    assert "Model" not in captured.out
    assert "armlab" in captured.out


def test_list_units_prompts_to_save_discovered_bar_root(tmp_path: Path, monkeypatch, capsys) -> None:
    local_app_data = tmp_path / "Local"
    bar_root = make_bar_fixture(local_app_data / "Custom" / "Install" / "data" / "games")
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    code = main(["list-units", "--faction", "scavs"])

    captured = capsys.readouterr()
    config = json.loads((workdir / "barprint.local.json").read_text(encoding="utf-8"))
    assert code == 0
    assert "Saved BAR data path" in captured.out
    assert "boss" in captured.out
    assert "Scavengers" in captured.out
    assert config["bar_root"] == bar_root.resolve().as_posix()


def test_export_prompts_to_save_discovered_blender(tmp_path: Path, monkeypatch) -> None:
    s3o = tmp_path / "model.s3o"
    importer = tmp_path / "s3o_import.py"
    workdir = tmp_path / "work"
    workdir.mkdir()
    s3o.write_bytes(b"s3o")
    importer.write_text("# importer", encoding="utf-8")
    answers = iter(["y"])
    calls = {}

    monkeypatch.chdir(workdir)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "C:/Blender/blender.exe")

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        calls["blender_exe"] = blender_exe

    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(["export", "--s3o", str(s3o), "--scale-mm", "45", "--s3o-importer", str(importer)])

    config = json.loads((workdir / "barprint.local.json").read_text(encoding="utf-8"))
    assert code == 0
    assert calls["blender_exe"] == "C:/Blender/blender.exe"
    assert config["blender"] == "C:/Blender/blender.exe"


def test_configure_installs_default_importer_and_saves_config(tmp_path: Path, monkeypatch, capsys) -> None:
    bar_root = make_bar_fixture(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    installed_importer = workdir / "vendor" / "s3o-Blender-plugins-2022" / "s3o_import.py"

    monkeypatch.chdir(workdir)
    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "C:/Blender/blender.exe")

    def fake_install_default_importer(args=None) -> Path:
        installed_importer.parent.mkdir(parents=True)
        installed_importer.write_text("# importer", encoding="utf-8")
        return installed_importer.resolve()

    monkeypatch.setattr("barprint.cli._install_default_s3o_importer", fake_install_default_importer)

    code = main(["configure", "--bar-root", str(bar_root), "--blender", "C:/Blender/blender.exe"])

    captured = capsys.readouterr()
    config = json.loads((workdir / "barprint.local.json").read_text(encoding="utf-8"))
    assert code == 0
    assert "Saved local configuration" in captured.out
    assert config["bar_root"] == bar_root.resolve().as_posix()
    assert config["blender"] == "C:/Blender/blender.exe"
    assert config["s3o_importer"] == installed_importer.resolve().as_posix()


def test_configure_user_writes_user_config(tmp_path: Path, monkeypatch, capsys) -> None:
    bar_root = make_bar_fixture(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()

    monkeypatch.chdir(workdir)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "C:/Blender/blender.exe")

    def fake_install_default_importer(args=None) -> Path:
        installed_importer = tmp_path / "Local" / "barprint" / "vendor" / "s3o-Blender-plugins-2022" / "s3o_import.py"
        installed_importer.parent.mkdir(parents=True)
        installed_importer.write_text("# importer", encoding="utf-8")
        return installed_importer.resolve()

    monkeypatch.setattr("barprint.cli._install_default_s3o_importer", fake_install_default_importer)

    code = main(["configure", "--user", "--bar-root", str(bar_root), "--blender", "C:/Blender/blender.exe"])

    captured = capsys.readouterr()
    config = json.loads(user_config_path().read_text(encoding="utf-8"))
    assert code == 0
    assert "Saved user configuration" in captured.out
    assert config["bar_root"] == bar_root.resolve().as_posix()
    assert config["s3o_importer"].startswith((tmp_path / "Local" / "barprint").as_posix())


def test_configure_portable_writes_portable_config(tmp_path: Path, monkeypatch, capsys) -> None:
    bar_root = make_bar_fixture(tmp_path)
    portable_home = tmp_path / "portable"

    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "C:/Blender/blender.exe")

    def fake_install_default_importer(args=None) -> Path:
        installed_importer = portable_home / "vendor" / "s3o-Blender-plugins-2022" / "s3o_import.py"
        installed_importer.parent.mkdir(parents=True)
        installed_importer.write_text("# importer", encoding="utf-8")
        return installed_importer.resolve()

    monkeypatch.setattr("barprint.cli._install_default_s3o_importer", fake_install_default_importer)

    code = main(
        [
            "configure",
            "--portable",
            str(portable_home),
            "--bar-root",
            str(bar_root),
            "--blender",
            "C:/Blender/blender.exe",
        ]
    )

    captured = capsys.readouterr()
    config = json.loads(portable_config_path(portable_home).read_text(encoding="utf-8"))
    assert code == 0
    assert "Saved portable configuration" in captured.out
    assert config["bar_root"] == bar_root.resolve().as_posix()
    assert config["s3o_importer"].startswith(portable_home.as_posix())


def test_doctor_reports_configured_dependencies(tmp_path: Path, monkeypatch, capsys) -> None:
    bar_root = make_bar_fixture(tmp_path)
    importer = tmp_path / "s3o_import.py"
    config = tmp_path / "barprint.local.json"
    importer.write_text("# importer", encoding="utf-8")
    config.write_text(
        json.dumps(
            {
                "bar_root": str(bar_root),
                "blender": "C:/Blender/blender.exe",
                "s3o_importer": str(importer),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("barprint.cli.find_blender", lambda value: value)

    code = main(["doctor", "--config", str(config), "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["ok"] is True
    assert {check["name"] for check in payload["checks"]} >= {"BAR data", "Blender", "S3O importer", "Cache"}


def test_view_serves_resolved_debug_viewer(tmp_path: Path, monkeypatch) -> None:
    debug_dir = tmp_path / "out" / "armcom_debug"
    viewer = debug_dir / "armcom_debug_viewer.html"
    debug_dir.mkdir(parents=True)
    viewer.write_text("<html></html>", encoding="utf-8")
    calls = {}

    def fake_serve_viewer(viewer_html, *, host, port, open_browser):
        calls["viewer_html"] = viewer_html
        calls["host"] = host
        calls["port"] = port
        calls["open_browser"] = open_browser

    monkeypatch.setattr("barprint.cli.serve_viewer", fake_serve_viewer)

    code = main(["view", str(tmp_path / "out"), "--host", "127.0.0.1", "--port", "8123", "--no-open"])

    assert code == 0
    assert calls == {
        "viewer_html": viewer.resolve(),
        "host": "127.0.0.1",
        "port": 8123,
        "open_browser": False,
    }


def test_export_prompts_to_install_and_save_missing_importer(tmp_path: Path, monkeypatch) -> None:
    s3o = tmp_path / "model.s3o"
    workdir = tmp_path / "work"
    workdir.mkdir()
    s3o.write_bytes(b"s3o")
    installed_importer = workdir / "vendor" / "s3o-Blender-plugins-2022" / "s3o_import.py"
    answers = iter(["y", "y"])
    calls = {}

    monkeypatch.chdir(workdir)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "C:/Blender/blender.exe")

    def fake_install_default_importer(args=None) -> Path:
        installed_importer.parent.mkdir(parents=True)
        installed_importer.write_text("# importer", encoding="utf-8")
        return installed_importer.resolve()

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        calls["importer_path"] = importer_path

    monkeypatch.setattr("barprint.cli._install_default_s3o_importer", fake_install_default_importer)
    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(
        [
            "export",
            "--s3o",
            str(s3o),
            "--scale-mm",
            "45",
            "--blender",
            "C:/Blender/blender.exe",
        ]
    )

    config = json.loads((workdir / "barprint.local.json").read_text(encoding="utf-8"))
    assert code == 0
    assert calls["importer_path"] == installed_importer.resolve()
    assert config["s3o_importer"] == installed_importer.resolve().as_posix()


def test_export_uses_configured_blender_and_importer(tmp_path: Path, monkeypatch) -> None:
    s3o = tmp_path / "model.s3o"
    importer = tmp_path / "s3o_import.py"
    config = tmp_path / "barprint.local.json"
    s3o.write_bytes(b"s3o")
    importer.write_text("# importer", encoding="utf-8")
    config.write_text(
        json.dumps({"blender": "configured-blender", "s3o_importer": str(importer)}),
        encoding="utf-8",
    )
    calls = {}

    def fake_find_blender(value):
        calls["blender"] = value
        return "configured-blender"

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        calls["export"] = {
            "blender_exe": blender_exe,
            "s3o_path": s3o_path,
            "importer_path": importer_path,
            "pose_name": pose_name,
            "out_path": out_path,
            "export_format": export_format,
        }

    monkeypatch.setattr("barprint.cli.find_blender", fake_find_blender)
    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(
        [
            "export",
            "--config",
            str(config),
            "--s3o",
            str(s3o),
            "--scale-mm",
            "45",
            "--out",
            str(tmp_path / "out"),
        ]
    )

    assert code == 0
    assert calls["blender"] == "configured-blender"
    assert calls["export"]["s3o_path"] == s3o.resolve()
    assert calls["export"]["importer_path"] == importer.resolve()
    assert calls["export"]["pose_name"] == "neutral"


def test_export_debug_stages_passes_blender_flag(tmp_path: Path, monkeypatch) -> None:
    s3o = tmp_path / "model.s3o"
    importer = tmp_path / "s3o_import.py"
    s3o.write_bytes(b"s3o")
    importer.write_text("# importer", encoding="utf-8")
    calls = {}

    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "configured-blender")

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        calls["extra_args"] = extra_args

    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(
        [
            "export",
            "--s3o",
            str(s3o),
            "--scale-mm",
            "45",
            "--s3o-importer",
            str(importer),
            "--out",
            str(tmp_path / "out"),
            "--debug-stages",
        ]
    )

    assert code == 0
    assert calls["extra_args"]["debug_stages"] is True


def test_export_debug_stages_writes_multi_pose_viewer(tmp_path: Path, monkeypatch) -> None:
    s3o = tmp_path / "model.s3o"
    importer = tmp_path / "s3o_import.py"
    out_base = tmp_path / "out" / "model"
    s3o.write_bytes(b"s3o")
    importer.write_text("# importer", encoding="utf-8")
    calls = {"exports": []}

    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "configured-blender")

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        calls["exports"].append((pose_name, out_path))

    def fake_write_compare_viewer_rows_html(viewer_html, *, rows):
        calls["viewer_html"] = viewer_html
        calls["rows"] = rows

    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)
    monkeypatch.setattr("barprint.cli.write_compare_viewer_rows_html", fake_write_compare_viewer_rows_html)

    code = main(
        [
            "export",
            "--s3o",
            str(s3o),
            "--scale-mm",
            "45",
            "--s3o-importer",
            str(importer),
            "--out",
            str(out_base),
            "--pose",
            "all",
            "--debug-stages",
        ]
    )

    assert code == 0
    assert [pose for pose, _out_path in calls["exports"]] == [
        "neutral",
        "aim_left",
        "aim_right",
        "stride_left",
        "stride_right",
        "brace",
        "advance",
    ]
    assert calls["viewer_html"] == out_base / "model_debug_viewer.html"
    assert [row.label for row in calls["rows"]] == [
        "neutral",
        "aim_left",
        "aim_right",
        "stride_left",
        "stride_right",
        "brace",
        "advance",
    ]
    assert calls["rows"][0].default_stl == out_base / "model_neutral.stl"
    assert calls["rows"][1].game_glb == out_base / "model_aim_left_debug" / "model_aim_left_game.glb"


def test_export_without_pose_profile_auto_selects_unit_profile(tmp_path: Path, monkeypatch) -> None:
    bar_root = make_bar_fixture(tmp_path)
    importer = tmp_path / "s3o_import.py"
    importer.write_text("# importer", encoding="utf-8")
    calls = {}

    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "configured-blender")

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        calls["profile"] = json.loads(pose_profile_path.read_text(encoding="utf-8"))

    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(
        [
            "export",
            "--bar-root",
            str(bar_root),
            "--unit",
            "armflea",
            "--scale-mm",
            "45",
            "--s3o-importer",
            str(importer),
        ]
    )

    assert code == 0
    assert calls["profile"]["name"] == "tick"
    assert calls["profile"]["pose_archetype"] == "tick"
    assert calls["profile"]["pose_source"] == "builtin_bar_source_ranges"


def test_export_writes_thin_feature_overrides_to_profile(tmp_path: Path, monkeypatch) -> None:
    s3o = tmp_path / "model.s3o"
    importer = tmp_path / "s3o_import.py"
    s3o.write_bytes(b"s3o")
    importer.write_text("# importer", encoding="utf-8")
    calls = {}

    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "configured-blender")

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        calls["profile"] = json.loads(pose_profile_path.read_text(encoding="utf-8"))

    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(
        [
            "export",
            "--s3o",
            str(s3o),
            "--scale-mm",
            "45",
            "--s3o-importer",
            str(importer),
            "--min-feature-mm",
            "1.1",
            "--thin-feature-max-inflate-mm",
            "0.45",
            "--no-thin-features",
        ]
    )

    assert code == 0
    assert calls["profile"]["thin_features"]["enabled"] is False
    assert calls["profile"]["thin_features"]["min_thickness_mm"] == 1.1
    assert calls["profile"]["thin_features"]["max_inflate_mm"] == 0.45


def test_export_variant_writes_variant_profile_and_output_suffix(tmp_path: Path, monkeypatch) -> None:
    s3o = tmp_path / "model.s3o"
    importer = tmp_path / "s3o_import.py"
    out_base = tmp_path / "out" / "model"
    s3o.write_bytes(b"s3o")
    importer.write_text("# importer", encoding="utf-8")
    calls = {}

    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "configured-blender")

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        calls["profile"] = json.loads(pose_profile_path.read_text(encoding="utf-8"))
        calls["out_path"] = out_path

    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(
        [
            "export",
            "--s3o",
            str(s3o),
            "--scale-mm",
            "45",
            "--s3o-importer",
            str(importer),
            "--out",
            str(out_base),
            "--variant",
            "decorated",
        ]
    )

    assert code == 0
    assert calls["profile"]["variant_name"] == "decorated"
    assert calls["profile"]["optional_piece_transforms"]["crown"]["translate_z"] == 100
    assert calls["profile"]["optional_delete_piece_aliases"] == ["antenna", "medalsilver", "medalbronze"]
    assert calls["out_path"] == out_base / "model_decorated.stl"


def test_export_uses_local_vendor_importer(tmp_path: Path, monkeypatch) -> None:
    s3o = tmp_path / "model.s3o"
    importer = tmp_path / "vendor" / "s3o-Blender-plugins-2022" / "s3o_import.py"
    s3o.write_bytes(b"s3o")
    importer.parent.mkdir(parents=True)
    importer.write_text("# importer", encoding="utf-8")
    calls = {}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "configured-blender")

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        calls["importer_path"] = importer_path

    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(["export", "--s3o", str(s3o), "--scale-mm", "45", "--out", str(tmp_path / "out")])

    assert code == 0
    assert calls["importer_path"] == importer.resolve()


def test_export_prints_async_progress_messages(tmp_path: Path, monkeypatch, capsys) -> None:
    s3o = tmp_path / "model.s3o"
    importer = tmp_path / "vendor" / "s3o-Blender-plugins-2022" / "s3o_import.py"
    s3o.write_bytes(b"s3o")
    importer.parent.mkdir(parents=True)
    importer.write_text("# importer", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "configured-blender")

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        assert progress_callback is not None
        progress_callback("Importing S3O")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("stl", encoding="utf-8")

    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(["export", "--s3o", str(s3o), "--scale-mm", "45", "--out", str(tmp_path / "out")])

    captured = capsys.readouterr()
    assert code == 0
    assert "Export [1/1] neutral ->" in captured.out
    assert "Export [1/1] neutral: Importing S3O" in captured.out
    assert "Export [1/1] complete ->" in captured.out


def test_export_open_opens_completed_file(tmp_path: Path, monkeypatch) -> None:
    s3o = tmp_path / "model.s3o"
    importer = tmp_path / "vendor" / "s3o-Blender-plugins-2022" / "s3o_import.py"
    out_path = tmp_path / "out" / "model.stl"
    s3o.write_bytes(b"s3o")
    importer.parent.mkdir(parents=True)
    importer.write_text("# importer", encoding="utf-8")
    opened = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "configured-blender")
    monkeypatch.setattr("barprint.cli.open_with_default_app", lambda path: opened.append(path))

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("stl", encoding="utf-8")

    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(
        [
            "export",
            "--s3o",
            str(s3o),
            "--scale-mm",
            "45",
            "--out",
            str(out_path),
            "--open",
        ]
    )

    assert code == 0
    assert opened == [out_path]


def test_export_reports_local_vendor_importer_candidates(tmp_path: Path, monkeypatch, capsys) -> None:
    s3o = tmp_path / "model.s3o"
    s3o.write_bytes(b"s3o")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "configured-blender")

    code = main(["export", "--s3o", str(s3o), "--scale-mm", "45", "--out", str(tmp_path / "out")])

    captured = capsys.readouterr()
    assert code == 2
    assert "known local vendor path" in captured.err
    assert "s3o-Blender-plugins-2022" in captured.err


def test_export_prompts_for_unit_and_uses_default_out(tmp_path: Path, monkeypatch, capsys) -> None:
    bar_root = make_bar_fixture(tmp_path)
    importer = tmp_path / "s3o_import.py"
    config = tmp_path / "barprint.local.json"
    importer.write_text("# importer", encoding="utf-8")
    config.write_text(
        json.dumps({"bar_root": str(bar_root), "s3o_importer": str(importer)}),
        encoding="utf-8",
    )
    answers = iter(["1", "1", "n"])
    calls = {}

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "configured-blender")

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        calls["export"] = {
            "s3o_path": s3o_path,
            "importer_path": importer_path,
            "out_path": out_path,
            "pose_name": pose_name,
        }

    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(["export", "--config", str(config), "--scale-mm", "45"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Factions:" in captured.out
    assert "Selected unit: armflea" in captured.out
    assert calls["export"]["s3o_path"] == bar_root / "objects3d" / "Units" / "ARMFLEA.s3o"
    assert calls["export"]["importer_path"] == importer.resolve()
    assert calls["export"]["out_path"] == Path("out") / "armflea" / "armflea.stl"
    assert calls["export"]["pose_name"] == "neutral"


def test_export_missing_source_noninteractive_errors(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    code = main(["export", "--scale-mm", "45"])

    captured = capsys.readouterr()
    assert code == 2
    assert "Either --s3o or --unit is required" in captured.err


def test_export_explicit_s3o_uses_default_out(tmp_path: Path, monkeypatch) -> None:
    s3o = tmp_path / "Model.s3o"
    importer = tmp_path / "s3o_import.py"
    s3o.write_bytes(b"s3o")
    importer.write_text("# importer", encoding="utf-8")
    calls = {}

    monkeypatch.setattr("barprint.cli.find_blender", lambda value: "configured-blender")

    def fake_run_blender_export(
        blender_exe,
        script_path,
        s3o_path,
        importer_path,
        pose_profile_path,
        pose_name,
        out_path,
        export_format,
        extra_args,
        progress_callback=None,
    ):
        calls["out_path"] = out_path

    monkeypatch.setattr("barprint.cli.run_blender_export", fake_run_blender_export)

    code = main(["export", "--s3o", str(s3o), "--scale-mm", "45", "--s3o-importer", str(importer)])

    assert code == 0
    assert calls["out_path"] == Path("out") / "model" / "model.stl"


def make_bar_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "BAR.sdd"
    add_unit(root, "ArmBots", "armflea", "ARMFLEA")
    add_unit(root, "Core", "corak", "CORAK")
    add_unit(root, "Legion", "legrail", "LEGRAIL")
    add_unit(root, "Scavengers", "boss", "SCAVBOSS")
    add_unit(root, "Raptors", "h1", "RAPTORH1")
    return root


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
    (language / "units.json").write_text(
        json.dumps({"units": {"names": names, "descriptions": descriptions}}),
        encoding="utf-8",
    )
