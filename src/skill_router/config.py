"""Persistent skill-router settings."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
import tomllib


class ConfigError(ValueError):
    """A configuration file is missing or invalid."""


@dataclass(frozen=True)
class RouterConfig:
    """Settings that control optional providers."""

    jev_enabled: bool = False


def config_path() -> Path:
    """Return the persistent configuration path."""

    configured = os.environ.get("SKILL_ROUTER_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "skill-router" / "config.toml"


def load_config(path: Path | None = None) -> RouterConfig:
    """Load settings, using safe local defaults when no file exists."""

    target = path or config_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return RouterConfig()
    except OSError as error:
        raise ConfigError(f"cannot read config: {target}: {error}") from error

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid config: {target}: {error}") from error
    jev = data.get("jev", {})
    if not isinstance(jev, dict):
        raise ConfigError("config section 'jev' must be a table")
    enabled = jev.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("config value 'jev.enabled' must be true or false")
    return RouterConfig(jev_enabled=enabled)


def save_config(config: RouterConfig, path: Path | None = None) -> Path:
    """Write settings to disk and return the target path."""

    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(f"[jev]\nenabled = {'true' if config.jev_enabled else 'false'}\n")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target
