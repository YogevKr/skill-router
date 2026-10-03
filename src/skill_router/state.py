"""Read local agent skill state without changing agent settings."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable

from .catalog import Skill
from .config import RouterConfig


CLAUDE_MODES = ("on", "name-only", "user-invocable-only", "off")


@dataclass(frozen=True)
class SkillState:
    """One skill's router, native, and Claude state."""

    skill: Skill
    source: str
    router_targets: tuple[str, ...]
    codex_exposure: str
    claude_exposure: str
    claude_mode: str
    claude_override: str | None
    claude_lock: str


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _settings_paths(home: Path, cwd: Path) -> tuple[Path, ...]:
    return (
        home / ".claude" / "settings.json",
        cwd / ".claude" / "settings.json",
        cwd / ".claude" / "settings.local.json",
    )


def claude_plugin_roots(*, home: Path | None = None, cwd: Path | None = None) -> list[Path]:
    """Return installed roots for locally enabled Claude plugins."""

    home = home or Path.home()
    cwd = cwd or Path.cwd()
    enabled: dict[str, bool] = {}
    for path in _settings_paths(home, cwd):
        raw = _read_object(path).get("enabledPlugins")
        if not isinstance(raw, dict):
            continue
        for plugin_id, state in raw.items():
            if isinstance(plugin_id, str) and isinstance(state, bool):
                enabled[plugin_id] = state
    installed = _read_object(home / ".claude" / "plugins" / "installed_plugins.json")
    plugins = installed.get("plugins")
    if not isinstance(plugins, dict):
        return []
    roots: list[Path] = []
    for plugin_id, entries in plugins.items():
        if not enabled.get(plugin_id, False) or not isinstance(entries, list):
            continue
        candidates = [
            entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("installPath"), str)
        ]
        if not candidates:
            continue
        selected = max(candidates, key=lambda entry: str(entry.get("lastUpdated", "")))
        root = Path(str(selected["installPath"])).expanduser()
        if root.is_dir():
            roots.append(root)
    return sorted(set(roots), key=lambda path: path.as_posix())


def claude_skill_overrides(*, home: Path | None = None, cwd: Path | None = None) -> dict[str, str]:
    """Read effective local Claude skill overrides in increasing precedence."""

    home = home or Path.home()
    cwd = cwd or Path.cwd()
    overrides: dict[str, str] = {}
    for path in _settings_paths(home, cwd):
        raw = _read_object(path).get("skillOverrides")
        if not isinstance(raw, dict):
            continue
        for skill_id, mode in raw.items():
            if isinstance(skill_id, str) and isinstance(mode, str) and mode in CLAUDE_MODES:
                overrides[skill_id] = mode
    return overrides


def _source_label(path: Path) -> str:
    parts = path.parts
    if "ripwire" in parts and ".local" in parts:
        return "ripwire"
    if "plugins" in parts and (".claude" in parts or "claude" in parts):
        return "plugin"
    if "synced" in parts and ".claude" in parts:
        return "claude.ai sync"
    if ".claude" in parts or ".agents" in parts:
        return "userSettings"
    if ".codex" in parts:
        return "codex"
    if ".agents" in parts:
        return "agents"
    return "custom"


def _frontmatter_mode(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().casefold()] = value.strip().strip("'\"").casefold()
    if fields.get("disable-model-invocation") == "true":
        return "user-invocable-only"
    if fields.get("user-invocable") == "false":
        return "name-only"
    return None


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.expanduser().resolve())
    except (FileNotFoundError, OSError, ValueError):
        return False
    return True


def _direct_skill(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.expanduser().resolve())
    except (FileNotFoundError, OSError, ValueError):
        return False
    return len(relative.parts) == 2 and relative.parts[-1] == "SKILL.md"


def _exposure(
    skill: Skill,
    target_root: Path,
    managed: set[tuple[str, str]],
    native_roots: tuple[Path, ...] = (),
) -> str:
    if _under(skill.path, target_root) or any(_direct_skill(skill.path, root) for root in native_roots):
        return "native"
    source_dir = skill.path.parent.resolve()
    for root in native_roots:
        destination = root / skill.skill_id
        if not destination.is_symlink():
            continue
        try:
            if destination.resolve() == source_dir:
                return "native"
        except (FileNotFoundError, OSError):
            continue
    destination = target_root / skill.skill_id
    if destination.is_symlink():
        key = (target_root.as_posix(), skill.skill_id)
        return "managed" if key in managed else "native"
    return "native" if destination.exists() else "-"


def inspect_skills(
    skills: Iterable[Skill],
    config: RouterConfig,
    *,
    cwd: Path | None = None,
    home: Path | None = None,
) -> list[SkillState]:
    """Build a read-only view of router and local agent state."""

    roots = config.roots()
    overrides = claude_skill_overrides(home=home, cwd=cwd)
    home = home or Path.home()
    shared_native_roots = (home / ".agents" / "skills",)
    assignments = config.assignment_map()
    managed = {(roots[link.target].as_posix(), link.skill_id) for link in config.managed_links}
    result: list[SkillState] = []
    for skill in skills:
        assignment = assignments.get(skill.skill_id)
        targets = tuple(sorted(assignment.targets)) if assignment and assignment.enabled else ()
        source = _source_label(skill.path)
        claude_exposure = (
            "native"
            if source == "plugin"
            else _exposure(skill, roots["claude"], managed, shared_native_roots)
        )
        override = overrides.get(skill.skill_id) or overrides.get(f"anthropic-skills:{skill.skill_id}")
        frontmatter_mode = _frontmatter_mode(skill.path)
        claude_mode = "-"
        claude_lock = "-"
        if claude_exposure != "-":
            claude_mode = frontmatter_mode or override or "on"
            claude_lock = (
                "plugin"
                if source == "plugin"
                else "frontmatter"
                if frontmatter_mode
                else "settings"
                if override
                else "-"
            )
        result.append(
            SkillState(
                skill=skill,
                source=source,
                router_targets=targets,
                codex_exposure=_exposure(skill, roots["codex"], managed, shared_native_roots),
                claude_exposure=claude_exposure,
                claude_mode=claude_mode,
                claude_override=override,
                claude_lock=claude_lock,
            )
        )
    return sorted(result, key=lambda item: item.skill.skill_id.casefold())
