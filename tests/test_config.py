import json
from pathlib import Path

import pytest

from barprint.config import (
    BarPrintConfigError,
    cache_dir,
    load_config,
    portable_config_path,
    save_config_values,
    user_config_path,
)


def test_load_config_returns_empty_without_local_file(tmp_path: Path) -> None:
    assert load_config(None, cwd=tmp_path) == {}


def test_load_config_auto_discovers_local_file(tmp_path: Path) -> None:
    config_path = tmp_path / "barprint.local.json"
    config_path.write_text(
        json.dumps(
            {
                "bar_root": "C:/BAR/BAR.sdd",
                "s3o_importer": "C:/tools/s3o_import.py",
                "scale_reference_unit": "armcom",
                "scale_reference_height_mm": 45,
            }
        ),
        encoding="utf-8",
    )

    assert load_config(None, cwd=tmp_path) == {
        "bar_root": "C:/BAR/BAR.sdd",
        "s3o_importer": "C:/tools/s3o_import.py",
        "scale_reference_unit": "armcom",
        "scale_reference_height_mm": 45.0,
    }


def test_save_config_values_creates_local_file(tmp_path: Path) -> None:
    config_path = save_config_values({"bar_root": "C:/BAR/BAR.sdd"}, None, cwd=tmp_path)

    assert config_path == tmp_path / "barprint.local.json"
    assert load_config(None, cwd=tmp_path) == {"bar_root": "C:/BAR/BAR.sdd"}


def test_save_config_values_creates_user_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    config_path = save_config_values({"bar_root": "C:/BAR/BAR.sdd"}, None, scope="user")

    assert config_path == user_config_path()
    assert load_config(None, cwd=tmp_path, include_user=True) == {"bar_root": "C:/BAR/BAR.sdd"}


def test_save_config_values_creates_portable_file(tmp_path: Path) -> None:
    portable_home = tmp_path / "portable"

    config_path = save_config_values(
        {"bar_root": "C:/BAR/BAR.sdd"},
        None,
        scope="portable",
        portable_home_path=portable_home,
    )

    assert config_path == portable_config_path(portable_home)
    assert load_config(None, cwd=portable_home) == {"bar_root": "C:/BAR/BAR.sdd"}


def test_cache_dir_uses_portable_home(tmp_path: Path, monkeypatch) -> None:
    portable_home = tmp_path / "portable"
    portable_home.mkdir()
    (portable_home / "barprint.portable.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(portable_home)

    assert cache_dir() == portable_home / "cache"


def test_cache_dir_uses_local_cache_when_local_config_exists(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "barprint.local.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert cache_dir() == tmp_path / ".barprint-cache"


def test_cache_dir_uses_user_cache_without_local_or_portable_config(tmp_path: Path, monkeypatch) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    assert cache_dir() == tmp_path / "Local" / "barprint" / "cache"


def test_save_config_values_merges_existing_file(tmp_path: Path) -> None:
    config_path = tmp_path / "barprint.local.json"
    config_path.write_text(json.dumps({"s3o_importer": "C:/tools/s3o_import.py"}), encoding="utf-8")

    save_config_values({"bar_root": "C:/BAR/BAR.sdd"}, config_path)

    assert load_config(config_path) == {
        "bar_root": "C:/BAR/BAR.sdd",
        "s3o_importer": "C:/tools/s3o_import.py",
    }


def test_load_config_rejects_unknown_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "barprint.local.json"
    config_path.write_text(json.dumps({"bar_root": "C:/BAR/BAR.sdd", "unit": "corak"}), encoding="utf-8")

    with pytest.raises(BarPrintConfigError, match="Unknown config key"):
        load_config(config_path)


def test_load_config_rejects_invalid_scale_reference_height(tmp_path: Path) -> None:
    config_path = tmp_path / "barprint.local.json"
    config_path.write_text(
        json.dumps({"scale_reference_height_mm": "large"}),
        encoding="utf-8",
    )

    with pytest.raises(BarPrintConfigError, match="positive number"):
        load_config(config_path)
