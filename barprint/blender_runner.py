from __future__ import annotations

from pathlib import Path
from typing import Literal, Any, Callable
import os
import shutil
import subprocess
import threading
import uuid


class BlenderRunnerError(RuntimeError):
    pass


PROGRESS_PREFIX = "BARPRINT_PROGRESS:"


def find_blender(explicit: str | None) -> str:
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path)
        if path.is_dir():
            candidate = _find_blender_in_roots([path])
            if candidate:
                return candidate
        found = shutil.which(explicit)
        if found:
            return found
        raise BlenderRunnerError(f"Blender executable not found: {explicit}")

    env = os.environ.get("BLENDER_EXE")
    if env:
        return find_blender(env)

    found = shutil.which("blender")
    if found:
        return found

    candidate = _find_blender_in_roots(_default_blender_roots())
    if candidate:
        return candidate

    raise BlenderRunnerError(
        "Blender not found. Install Blender, set BLENDER_EXE, or pass --blender."
    )


def _default_blender_roots() -> list[Path]:
    roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Blender Foundation",
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.extend(
            [
                Path(local_app_data) / "Programs" / "Blender Foundation",
                Path(local_app_data) / "Blender Foundation",
            ]
        )
    return roots


def _find_blender_in_roots(roots: list[Path]) -> str | None:
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.append(root / "blender.exe")
            candidates.extend(root.glob("Blender*/*blender.exe"))
            candidates.extend(root.glob("*/blender.exe"))
    for candidate in sorted(set(candidates), reverse=True):
        if candidate.is_file():
            return str(candidate)
    return None


def run_blender_export(
    blender_exe: str,
    script_path: Path,
    s3o_path: Path,
    importer_path: Path,
    pose_profile_path: Path,
    pose_name: str,
    out_path: Path,
    export_format: Literal["stl", "3mf", "glb"],
    extra_args: dict[str, Any] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> None:
    if not Path(blender_exe).exists() and shutil.which(blender_exe) is None:
        raise BlenderRunnerError(f"Blender executable not found: {blender_exe}")
    if not script_path.is_file():
        raise BlenderRunnerError(f"Blender export script not found: {script_path}")
    if not s3o_path.is_file():
        raise BlenderRunnerError(f"S3O file not found: {s3o_path}")
    if not importer_path.is_file():
        raise BlenderRunnerError(
            f"S3O importer missing: {importer_path}. Pass --s3o-importer with a compatible importer file."
        )
    if export_format not in {"stl", "3mf", "glb"}:
        raise BlenderRunnerError(f"Unsupported export format: {export_format}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        blender_exe,
        "--background",
        "--factory-startup",
        "--python",
        str(script_path),
        "--",
        "--s3o",
        str(s3o_path),
        "--s3o-importer",
        str(importer_path),
        "--pose-profile",
        str(pose_profile_path),
        "--pose",
        pose_name,
        "--out",
        str(out_path),
        "--format",
        export_format,
    ]
    for key, value in (extra_args or {}).items():
        flag = f"--{key.replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                cmd.append(flag)
        elif value is not None:
            cmd.extend([flag, str(value)])

    stdout, stderr = _run_blender_command(
        cmd,
        temp_parent=out_path.parent,
        progress_callback=progress_callback,
        failure_label="Blender export failed.",
    )
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise BlenderRunnerError(
            "Blender completed but did not create a non-empty export file.\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}"
        )


def _run_blender_command(
    cmd: list[str],
    *,
    temp_parent: Path,
    progress_callback: Callable[[str], None] | None,
    failure_label: str,
) -> tuple[str, str]:
    temp_root = temp_parent.resolve() / "_barprint_blender_tmp"
    temp_env_dir = temp_root / uuid.uuid4().hex
    temp_env_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in ("TMP", "TEMP", "TMPDIR"):
        env[key] = str(temp_env_dir)
    try:
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise BlenderRunnerError(f"Could not start Blender: {exc}") from exc

        readers = [
            threading.Thread(
                target=_collect_process_output,
                args=(process.stdout, stdout_lines, progress_callback),
                daemon=True,
            ),
            threading.Thread(
                target=_collect_process_output,
                args=(process.stderr, stderr_lines, progress_callback),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        return_code = process.wait()
        for reader in readers:
            reader.join()

        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        if return_code != 0:
            raise BlenderRunnerError(
                f"{failure_label}\n"
                f"Command: {' '.join(cmd)}\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}"
            )
        return stdout, stderr
    finally:
        shutil.rmtree(temp_env_dir, ignore_errors=True)
        try:
            temp_root.rmdir()
        except OSError:
            pass


def _collect_process_output(
    stream,
    output: list[str],
    progress_callback: Callable[[str], None] | None,
) -> None:
    if stream is None:
        return
    try:
        for line in stream:
            output.append(line)
            if progress_callback:
                message = _extract_progress_message(line)
                if message:
                    progress_callback(message)
    finally:
        stream.close()


def _extract_progress_message(line: str) -> str | None:
    text = line.strip()
    if not text.startswith(PROGRESS_PREFIX):
        return None
    message = text[len(PROGRESS_PREFIX) :].strip()
    return message or None

