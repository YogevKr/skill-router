"""Persistent skill-router settings."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import tomllib


class ConfigError(ValueError):
    """A configuration file is missing or invalid."""


TARGETS = ("codex", "claude")


def default_target_roots() -> dict[str, Path]:
    """Return the native skill roots for supported agents."""

    home = Path.home()
    return {"codex": home / ".codex" / "skills", "claude": home / ".claude" / "skills"}


@dataclass(frozen=True)
class SkillAssignment:
    """A skill source and the agents that should receive it."""

    skill_id: str
    source: Path
    targets: frozenset[str]
    enabled: bool = True
    native_targets: frozenset[str] | None = None


def effective_native_targets(assignment: SkillAssignment) -> frozenset[str]:
    """Return native targets, preserving legacy assignments without that field."""

    return assignment.targets if assignment.native_targets is None else assignment.native_targets


@dataclass(frozen=True)
class ManagedLink:
    """A link created by this tool and safe to remove during pruning."""

    target: str
    skill_id: str
    path: Path
    source: Path


@dataclass(frozen=True)
class RouterConfig:
    """Settings that control optional providers."""

    jev_enabled: bool = False
    target_roots: tuple[tuple[str, Path], ...] = ()
    assignments: tuple[SkillAssignment, ...] = ()
    managed_links: tuple[ManagedLink, ...] = ()

    def roots(self) -> dict[str, Path]:
        """Return configured target roots with native defaults."""

        values = default_target_roots()
        values.update(dict(self.target_roots))
        return values

    def assignment_map(self) -> dict[str, SkillAssignment]:
        """Return assignments keyed by skill ID."""

        return {assignment.skill_id: assignment for assignment in self.assignments}


def config_path() -> Path:
    """Return the persistent configuration path."""

    configured = os.environ.get("SKILL_ROUTER_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "skill-router" / "config.toml"


def _parse_jev(data: dict[str, object]) -> bool:
    jev = data.get("jev", {})
    if not isinstance(jev, dict):
        raise ConfigError("config section 'jev' must be a table")
    enabled = jev.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("config value 'jev.enabled' must be true or false")
    return enabled


def _parse_target_roots(data: dict[str, object]) -> tuple[tuple[str, Path], ...]:
    targets = data.get("targets", {})
    if not isinstance(targets, dict):
        raise ConfigError("config section 'targets' must be a table")
    values: list[tuple[str, Path]] = []
    for target, raw_path in targets.items():
        if target not in TARGETS:
            raise ConfigError(f"unknown target: {target}")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ConfigError(f"config target path must be text: {target}")
        values.append((target, Path(raw_path).expanduser()))
    return tuple(sorted(values))


def _parse_assignment(skill_id: object, raw: object) -> SkillAssignment:
    if not isinstance(skill_id, str) or not skill_id or "/" in skill_id:
        raise ConfigError(f"invalid skill ID: {skill_id}")
    if not isinstance(raw, dict):
        raise ConfigError(f"config skill must be a table: {skill_id}")
    source = raw.get("source")
    raw_targets = raw.get("targets", [])
    raw_native_targets = raw.get("native_targets")
    enabled = raw.get("enabled", True)
    if not isinstance(source, str) or not source.strip():
        raise ConfigError(f"config skill source must be text: {skill_id}")
    if not isinstance(raw_targets, list) or any(target not in TARGETS for target in raw_targets):
        raise ConfigError(f"config skill targets are invalid: {skill_id}")
    if raw_native_targets is not None and (
        not isinstance(raw_native_targets, list)
        or any(target not in TARGETS for target in raw_native_targets)
    ):
        raise ConfigError(f"config skill native targets are invalid: {skill_id}")
    if not isinstance(enabled, bool):
        raise ConfigError(f"config skill enabled must be true or false: {skill_id}")
    return SkillAssignment(
        skill_id=skill_id,
        source=Path(source).expanduser(),
        targets=frozenset(raw_targets),
        enabled=enabled,
        native_targets=(
            frozenset(raw_native_targets)
            if raw_native_targets is not None
            else None
        ),
    )


def _parse_assignment_table(data: dict[str, object]) -> tuple[SkillAssignment, ...]:
    skills = data.get("skills", {})
    if not isinstance(skills, dict):
        raise ConfigError("config section 'skills' must be a table")
    assignments = [_parse_assignment(skill_id, raw) for skill_id, raw in skills.items()]
    return tuple(sorted(assignments, key=lambda item: item.skill_id.casefold()))


def _parse_managed_link(raw: object) -> ManagedLink:
    if not isinstance(raw, dict):
        raise ConfigError("each managed link must be a table")
    target = raw.get("target")
    skill_id = raw.get("skill_id")
    path = raw.get("path")
    source = raw.get("source")
    if (
        target not in TARGETS
        or not isinstance(skill_id, str)
        or not isinstance(path, str)
        or not isinstance(source, str)
    ):
        raise ConfigError("managed link fields are invalid")
    return ManagedLink(
        target=target,
        skill_id=skill_id,
        path=Path(path).expanduser(),
        source=Path(source).expanduser(),
    )


def _parse_managed_link_list(data: dict[str, object]) -> tuple[ManagedLink, ...]:
    links = data.get("managed_links", [])
    if not isinstance(links, list):
        raise ConfigError("config value 'managed_links' must be an array")
    return tuple(_parse_managed_link(raw) for raw in links)


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
    return RouterConfig(
        jev_enabled=_parse_jev(data),
        target_roots=_parse_target_roots(data),
        assignments=_parse_assignment_table(data),
        managed_links=_parse_managed_link_list(data),
    )


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
            file.write("\n[targets]\n")
            for target_name, root in sorted(config.roots().items()):
                file.write(f"{target_name} = {json.dumps(root.as_posix())}\n")
            for assignment in sorted(config.assignments, key=lambda item: item.skill_id.casefold()):
                file.write(f"\n[skills.{json.dumps(assignment.skill_id)}]\n")
                file.write(f"source = {json.dumps(assignment.source.as_posix())}\n")
                file.write(f"enabled = {'true' if assignment.enabled else 'false'}\n")
                targets = ", ".join(json.dumps(target) for target in sorted(assignment.targets))
                file.write(f"targets = [{targets}]\n")
                if assignment.native_targets is not None:
                    native_targets = ", ".join(
                        json.dumps(target) for target in sorted(assignment.native_targets)
                    )
                    file.write(f"native_targets = [{native_targets}]\n")
            for link in config.managed_links:
                file.write("\n[[managed_links]]\n")
                file.write(f"target = {json.dumps(link.target)}\n")
                file.write(f"skill_id = {json.dumps(link.skill_id)}\n")
                file.write(f"path = {json.dumps(link.path.as_posix())}\n")
                file.write(f"source = {json.dumps(link.source.as_posix())}\n")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target
