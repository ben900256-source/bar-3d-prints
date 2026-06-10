from pathlib import Path

import pytest

from barprint.blender_runner import BlenderRunnerError, _extract_progress_message, find_blender


def test_find_blender_discovers_versioned_program_files_install(tmp_path: Path, monkeypatch) -> None:
    blender = tmp_path / "Program Files" / "Blender Foundation" / "Blender 5.1" / "blender.exe"
    blender.parent.mkdir(parents=True)
    blender.write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "Program Files (x86)"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("BLENDER_EXE", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: None)

    assert find_blender(None) == str(blender)


def test_find_blender_discovers_local_app_data_install(tmp_path: Path, monkeypatch) -> None:
    blender = tmp_path / "Local" / "Programs" / "Blender Foundation" / "Blender 5.1" / "blender.exe"
    blender.parent.mkdir(parents=True)
    blender.write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "missing"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "missing-x86"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("BLENDER_EXE", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: None)

    assert find_blender(None) == str(blender)


def test_find_blender_accepts_install_directory(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "Blender 5.1"
    blender = install_dir / "blender.exe"
    install_dir.mkdir()
    blender.write_text("", encoding="utf-8")
    monkeypatch.delenv("BLENDER_EXE", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: None)

    assert find_blender(str(install_dir)) == str(blender)


def test_find_blender_reports_missing_explicit_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)

    with pytest.raises(BlenderRunnerError, match="Blender executable not found"):
        find_blender(str(tmp_path / "missing"))


def test_extract_progress_message_reads_structured_blender_lines() -> None:
    assert _extract_progress_message("BARPRINT_PROGRESS: Importing S3O\n") == "Importing S3O"
    assert _extract_progress_message("Blender 5.1") is None
