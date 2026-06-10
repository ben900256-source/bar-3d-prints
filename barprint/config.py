from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import sys


CONFIG_ENV_VAR = "BARPRINT_CONFIG"
PORTABLE_HOME_ENV_VAR = "BARPRINT_PORTABLE_HOME"
LEGACY_PORTABLE_HOME_ENV_VAR = "BARPRINT_HOME"
CACHE_DIR_ENV_VAR = "BARPRINT_CACHE_DIR"
APP_NAME = "barprint"
LOCAL_CONFIG_NAME = "barprint.local.json"
PORTABLE_CONFIG_NAME = "barprint.portable.json"
USER_CONFIG_NAME = "config.json"

ALLOWED_CONFIG_KEYS = frozenset(
    {
        "bar_root",
        "blender",
        "s3o_importer",
        "scale_mode",
        "scale_reference_height_mm",
        "scale_reference_unit",
        "test_s3o_path",
    }
)
NUMERIC_CONFIG_KEYS = frozenset({"scale_reference_height_mm"})


class BarPrintConfigError(RuntimeError):
    pass


def load_config(
    explicit: str | Path | None,
    *,
    cwd: Path | None = None,
    auto_discover: bool = True,
    include_user: bool = False,
) -> dict[str, Any]:
    config_path = _select_config_path(
        explicit,
        cwd=cwd,
        auto_discover=auto_discover,
        include_user=include_user,
    )
    if config_path is None:
        return {}
    raw = _read_config_object(config_path)
    return _normalize_config(raw, config_path)


def save_config_values(
    values: dict[str, str],
    explicit: str | Path | None,
    *,
    cwd: Path | None = None,
    scope: str = "local",
    portable_home_path: str | Path | None = None,
) -> Path:
    config_path = config_path_for_write(
        explicit,
        cwd=cwd,
        scope=scope,
        portable_home_path=portable_home_path,
    )
    if config_path.is_file():
        raw = _read_config_object(config_path)
    else:
        raw = {}
    config = _normalize_config(raw, config_path)
    for key, value in values.items():
        if key not in ALLOWED_CONFIG_KEYS:
            allowed = ", ".join(sorted(ALLOWED_CONFIG_KEYS))
            raise BarPrintConfigError(f"Unknown config key '{key}'. Allowed keys: {allowed}")
        if not isinstance(value, str) or not value.strip():
            raise BarPrintConfigError(f"Config value '{key}' must be a non-empty string")
        config[key] = value
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path


def config_path_for_write(
    explicit: str | Path | None,
    *,
    cwd: Path | None = None,
    scope: str = "local",
    portable_home_path: str | Path | None = None,
) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env_value = os.environ.get(CONFIG_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser()
    if scope == "user":
        return user_config_path()
    if scope == "portable":
        return portable_config_path(portable_home_path, cwd=cwd)
    if scope != "local":
        raise BarPrintConfigError(f"Unsupported config write scope: {scope}")
    return (cwd or Path.cwd()) / LOCAL_CONFIG_NAME


def local_config_path(*, cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / LOCAL_CONFIG_NAME


def portable_home(explicit: str | Path | None = None, *, cwd: Path | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    env_value = os.environ.get(PORTABLE_HOME_ENV_VAR) or os.environ.get(LEGACY_PORTABLE_HOME_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser()
    candidate = (cwd or Path.cwd()) / PORTABLE_CONFIG_NAME
    if candidate.is_file():
        return candidate.parent
    return None


def portable_config_path(
    explicit_home: str | Path | None = None,
    *,
    cwd: Path | None = None,
) -> Path:
    home = portable_home(explicit_home, cwd=cwd)
    if home is None:
        home = cwd or Path.cwd()
    return home / PORTABLE_CONFIG_NAME


def user_config_dir() -> Path:
    if sys.platform.startswith("win"):
        root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        return (Path(root) if root else Path.home() / "AppData" / "Roaming") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    root = os.environ.get("XDG_CONFIG_HOME")
    return (Path(root) if root else Path.home() / ".config") / APP_NAME


def user_data_dir() -> Path:
    if sys.platform.startswith("win"):
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(root) if root else Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    root = os.environ.get("XDG_DATA_HOME")
    return (Path(root) if root else Path.home() / ".local" / "share") / APP_NAME


def user_cache_dir() -> Path:
    if sys.platform.startswith("win"):
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(root) if root else Path.home() / "AppData" / "Local") / APP_NAME / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_NAME
    root = os.environ.get("XDG_CACHE_HOME")
    return (Path(root) if root else Path.home() / ".cache") / APP_NAME


def user_config_path() -> Path:
    return user_config_dir() / USER_CONFIG_NAME


def data_dir_for_scope(
    scope: str = "local",
    *,
    cwd: Path | None = None,
    portable_home_path: str | Path | None = None,
) -> Path:
    if scope == "user":
        return user_data_dir()
    if scope == "portable":
        return portable_config_path(portable_home_path, cwd=cwd).parent
    if scope == "local":
        return cwd or Path.cwd()
    raise BarPrintConfigError(f"Unsupported data scope: {scope}")


def cache_dir(*, cwd: Path | None = None) -> Path:
    explicit = os.environ.get(CACHE_DIR_ENV_VAR)
    if explicit:
        return Path(explicit).expanduser()
    home = portable_home(cwd=cwd)
    if home is not None:
        return home / "cache"
    root = cwd or Path.cwd()
    if local_config_path(cwd=root).is_file():
        return root / ".barprint-cache"
    return user_cache_dir()


def discovered_config_paths(*, cwd: Path | None = None, include_user: bool = True) -> list[Path]:
    paths = [local_config_path(cwd=cwd)]
    portable = portable_home(cwd=cwd)
    if portable is not None:
        paths.append(portable / PORTABLE_CONFIG_NAME)
    if include_user:
        paths.append(user_config_path())
    return _unique_paths(paths)


def _read_config_object(config_path: Path) -> dict:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BarPrintConfigError(f"Config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise BarPrintConfigError(f"Invalid config JSON at {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BarPrintConfigError(f"Config file must contain a JSON object: {config_path}")
    return raw


def _normalize_config(raw: dict, config_path: Path) -> dict[str, Any]:
    unknown = sorted(set(raw) - ALLOWED_CONFIG_KEYS)
    if unknown:
        allowed = ", ".join(sorted(ALLOWED_CONFIG_KEYS))
        raise BarPrintConfigError(
            f"Unknown config key(s): {', '.join(unknown)}. Allowed keys: {allowed}"
        )

    config: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if key in NUMERIC_CONFIG_KEYS:
            if isinstance(value, bool) or not isinstance(value, int | float | str) or not str(value).strip():
                raise BarPrintConfigError(f"Config value '{key}' must be a positive number or numeric string")
            if isinstance(value, str):
                try:
                    parsed = float(value)
                except ValueError as exc:
                    raise BarPrintConfigError(f"Config value '{key}' must be a positive number") from exc
            else:
                parsed = float(value)
            if parsed <= 0:
                raise BarPrintConfigError(f"Config value '{key}' must be greater than zero")
            config[key] = parsed
            continue
        if not isinstance(value, str) or not value.strip():
            raise BarPrintConfigError(f"Config value '{key}' must be a non-empty string or null")
        config[key] = value
    return config


def _select_config_path(
    explicit: str | Path | None,
    *,
    cwd: Path | None,
    auto_discover: bool,
    include_user: bool,
) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()

    env_value = os.environ.get(CONFIG_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser()

    if not auto_discover:
        return None

    candidate = local_config_path(cwd=cwd)
    if candidate.is_file():
        return candidate
    portable = portable_home(cwd=cwd)
    if portable is not None:
        candidate = portable / PORTABLE_CONFIG_NAME
        if candidate.is_file():
            return candidate
    if include_user:
        candidate = user_config_path()
        if candidate.is_file():
            return candidate
    return None


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique
