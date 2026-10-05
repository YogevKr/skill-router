"""Read trusted skill files from explicit, non-native roots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from typing import Iterable


@dataclass(frozen=True)
class Skill:
    """A parsed skill and its local source."""

    skill_id: str
    name: str
    description: str
    path: Path
    body: str


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text

    end = text.find("\n---", 4)
    if end < 0:
        return {}, text

    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        value = value.strip().strip('"\'')
        if key.strip() and value:
            fields[key.strip().lower()] = value
    return fields, text[end + len("\n---") :].lstrip("\n")


def _parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    fields, body = _parse_front_matter(text)
    skill_id = path.parent.name
    name = fields.get("name", skill_id)
    description = fields.get("description", "")
    if not description:
        description = _first_paragraph(body) or name
    return Skill(skill_id=skill_id, name=name, description=description, path=path, body=body)


def _first_paragraph(body: str) -> str:
    lines = []
    for line in body.splitlines():
        value = line.strip()
        if not value:
            if lines:
                break
            continue
        if value.startswith("#") or value.startswith("```"):
            continue
        lines.append(value)
    return " ".join(lines)


def _safe_skill_path(root: Path, candidate: Path) -> Path | None:
    if root.is_symlink() or candidate.is_symlink():
        return None
    try:
        root_resolved = root.resolve(strict=True)
        candidate_resolved = candidate.resolve(strict=True)
        candidate_resolved.relative_to(root_resolved)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return candidate_resolved


def _skill_files(root: Path) -> Iterable[Path]:
    """Yield safe skill files from one directory or one exact skill path."""

    root = root.expanduser()
    if root.is_symlink():
        return
    if root.is_file() and root.name == "SKILL.md":
        candidates = [root]
        anchor = root.parent
    elif root.is_dir():
        candidates = sorted(root.rglob("SKILL.md"))
        anchor = root
    else:
        return
    for candidate in candidates:
        safe_path = _safe_skill_path(anchor, candidate)
        if safe_path is not None:
            yield safe_path


def scan_roots(roots: Iterable[str | os.PathLike[str]]) -> list[Skill]:
    """Scan directories or exact skill paths, preserving the first skill ID."""

    found: dict[str, Skill] = {}
    for raw_root in roots:
        for safe_path in _skill_files(Path(raw_root)):
            try:
                skill = _parse_skill(safe_path)
            except (OSError, UnicodeError):
                continue
            found.setdefault(skill.skill_id, skill)
    return sorted(found.values(), key=lambda skill: skill.skill_id.casefold())


def load_skill(skill_id: str, roots: Iterable[str | os.PathLike[str]]) -> Skill:
    """Load one exact skill ID or name from explicit roots."""

    wanted = skill_id.casefold()
    for skill in scan_roots(roots):
        if skill.skill_id.casefold() == wanted or skill.name.casefold() == wanted:
            return skill
    raise KeyError(f"skill not found: {skill_id}")
