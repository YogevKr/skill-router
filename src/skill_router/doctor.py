"""Detect skill ownership and exposure drift."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .catalog import Skill, _parse_skill, _safe_skill_path, scan_roots
from .config import RouterConfig
from .manager import default_source_roots, sync_assignments
from .state import SkillState, claude_plugin_roots, inspect_skills


SEVERITIES = ("error", "warning", "info")


@dataclass(frozen=True)
class DoctorFinding:
    """One doctor observation."""

    severity: str
    code: str
    message: str
    skill_id: str | None = None
    path: Path | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready finding."""

        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "skill_id": self.skill_id,
            "path": str(self.path) if self.path is not None else None,
        }


@dataclass(frozen=True)
class DoctorReport:
    """A complete local skill drift report."""

    findings: tuple[DoctorFinding, ...]

    @property
    def errors(self) -> int:
        """Return the number of error findings."""

        return sum(finding.severity == "error" for finding in self.findings)

    @property
    def warnings(self) -> int:
        """Return the number of warning findings."""

        return sum(finding.severity == "warning" for finding in self.findings)

    @property
    def infos(self) -> int:
        """Return the number of informational findings."""

        return sum(finding.severity == "info" for finding in self.findings)

    @property
    def ok(self) -> bool:
        """Return whether no actionable drift exists."""

        return self.errors == 0 and self.warnings == 0

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready report."""

        return {
            "ok": self.ok,
            "counts": {
                "error": self.errors,
                "warning": self.warnings,
                "info": self.infos,
            },
            "findings": [finding.as_dict() for finding in self.findings],
        }


def _skill_occurrences(roots: Iterable[Path]) -> dict[str, list[Skill]]:
    occurrences: dict[str, list[Skill]] = {}
    seen_roots: set[Path] = set()
    for root in roots:
        root = root.expanduser()
        try:
            key = root.resolve()
        except OSError:
            key = root
        if key in seen_roots:
            continue
        seen_roots.add(key)
        if not root.is_dir() or root.is_symlink():
            continue
        for candidate in sorted(root.rglob("SKILL.md")):
            safe_path = _safe_skill_path(root, candidate)
            if safe_path is None:
                continue
            try:
                skill = _parse_skill(safe_path)
            except (OSError, UnicodeError):
                continue
            occurrences.setdefault(skill.skill_id, []).append(skill)
    return occurrences


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.expanduser().resolve())
    except (FileNotFoundError, OSError, ValueError):
        return False
    return True


def _duplicate_severity(skills: Iterable[Skill]) -> str:
    """Return warning for competing personal sources, info for provider overlap."""

    kinds: set[str] = set()
    for skill in skills:
        parts = skill.path.parts
        if "skill-router" in parts and ".local" in parts:
            kinds.add("router")
        elif "ripwire" in parts and ".local" in parts:
            kinds.add("provider")
        elif "plugins" in parts or "synced" in parts or ".system" in parts:
            kinds.add("provider")
        else:
            kinds.add("personal")
    return "warning" if "personal" in kinds or len(kinds) == 1 and "router" in kinds else "info"


def _direct_native_findings(
    config: RouterConfig,
    findings: list[DoctorFinding],
) -> None:
    """Find direct native folders and links outside the saved managed set."""

    roots = config.roots()
    managed_paths = {link.path.expanduser() for link in config.managed_links}
    for target, root in roots.items():
        root = root.expanduser()
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.name.startswith(".") or entry.name == "synced":
                continue
            if entry in managed_paths:
                continue
            if entry.is_symlink():
                try:
                    resolved = entry.resolve(strict=True)
                except (FileNotFoundError, OSError):
                    findings.append(
                        DoctorFinding(
                            "warning",
                            "broken_native_link",
                            f"{target} native link is broken",
                            entry.name,
                            entry,
                        )
                    )
                    continue
                if _under(resolved, root / ".system"):
                    continue
                if (resolved / "SKILL.md").is_file():
                    findings.append(
                        DoctorFinding(
                            "warning",
                            "unmanaged_native_link",
                            f"{target} exposes an unmanaged skill link",
                            entry.name,
                            entry,
                        )
                    )
                continue
            if (entry / "SKILL.md").is_file():
                findings.append(
                    DoctorFinding(
                        "warning",
                        "unmanaged_native",
                        f"{target} contains an unmanaged native skill",
                        entry.name,
                        entry,
                    )
                )


def _managed_link_findings(config: RouterConfig, findings: list[DoctorFinding]) -> None:
    """Find broken, changed, or stale managed links."""

    roots = config.roots()
    for link in config.managed_links:
        path = link.path.expanduser()
        source = link.source.expanduser()
        root = roots.get(link.target)
        if root is None or not root.expanduser().is_dir():
            findings.append(
                DoctorFinding(
                    "error",
                    "managed_target_missing",
                    f"managed target root is missing for {link.target}",
                    link.skill_id,
                    path,
                )
            )
            continue
        expected = root.expanduser() / link.skill_id
        if path != expected:
            findings.append(
                DoctorFinding(
                    "error",
                    "managed_path_mismatch",
                    "managed link path does not match its target root",
                    link.skill_id,
                    path,
                )
            )
        if not source.is_file():
            findings.append(
                DoctorFinding(
                    "error",
                    "managed_source_missing",
                    "managed link source is missing",
                    link.skill_id,
                    source,
                )
            )
        if not path.is_symlink():
            code = "broken_managed_link" if not path.exists() else "managed_path_not_link"
            findings.append(
                DoctorFinding(
                    "error",
                    code,
                    "managed destination is not the saved symlink",
                    link.skill_id,
                    path,
                )
            )
            continue
        try:
            actual = path.resolve(strict=True)
            expected_source = source.parent.resolve(strict=True)
        except (FileNotFoundError, OSError):
            findings.append(
                DoctorFinding(
                    "error",
                    "broken_managed_link",
                    "managed destination points to a missing source",
                    link.skill_id,
                    path,
                )
            )
            continue
        if actual != expected_source:
            findings.append(
                DoctorFinding(
                    "error",
                    "managed_link_drift",
                    "managed destination points to another skill",
                    link.skill_id,
                    path,
                )
            )


def _assignment_findings(config: RouterConfig, findings: list[DoctorFinding]) -> None:
    """Find assignments whose expected native state differs from disk."""

    for action in sync_assignments(config, prune=True):
        if action.action == "keep":
            continue
        severity = "error" if action.action in {"invalid-source", "invalid-target", "conflict"} else "warning"
        code = {
            "link": "missing_managed_link",
            "conflict": "sync_conflict",
            "invalid-source": "assignment_source_invalid",
            "invalid-target": "assignment_target_invalid",
            "unlink": "stale_managed_link",
        }.get(action.action, "sync_drift")
        findings.append(
            DoctorFinding(
                severity,
                code,
                action.detail or f"sync action required: {action.action}",
                action.skill_id,
                action.path if action.path != Path() else action.source,
            )
        )


def _state_findings(states: Iterable[SkillState], findings: list[DoctorFinding]) -> None:
    """Report provider-owned sources that need provider controls."""

    for state in states:
        if state.source == "plugin":
            findings.append(
                DoctorFinding(
                    "info",
                    "plugin_owned",
                    "skill is owned by a Claude plugin",
                    state.skill.skill_id,
                    state.skill.path,
                )
            )
        elif state.source == "claude.ai sync" and state.claude_mode == "off":
            findings.append(
                DoctorFinding(
                    "info",
                    "sync_disabled",
                    "Claude.ai synced skill is disabled by settings",
                    state.skill.skill_id,
                    state.skill.path,
                )
            )


def doctor(
    config: RouterConfig,
    *,
    source_roots: Iterable[Path] | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
) -> DoctorReport:
    """Inspect local skill sources, links, and provider ownership."""

    roots = list(
        source_roots
        or (default_source_roots(home=home, cwd=cwd) + claude_plugin_roots(home=home, cwd=cwd))
    )
    findings: list[DoctorFinding] = []
    occurrences = _skill_occurrences(roots)
    assignments = config.assignment_map()
    router_root = Path(home or Path.home()) / ".local" / "share" / "skill-router" / "skills"

    for skill_id, skills in sorted(occurrences.items()):
        if len(skills) > 1:
            paths = ", ".join(str(skill.path) for skill in skills)
            findings.append(
                DoctorFinding(
                    _duplicate_severity(skills),
                    "duplicate_skill",
                    f"skill appears in multiple sources: {paths}",
                    skill_id,
                )
            )
    router_skills = _skill_occurrences([router_root])
    for skill_id in sorted(router_skills):
        if skill_id not in assignments:
            findings.append(
                DoctorFinding(
                    "warning",
                    "unassigned_router_skill",
                    "router skill has no saved assignment",
                    skill_id,
                    router_skills[skill_id][0].path,
                )
            )

    _managed_link_findings(config, findings)
    _assignment_findings(config, findings)
    _direct_native_findings(config, findings)

    unique_skills = list(scan_roots(roots))
    states = inspect_skills(unique_skills, config, home=home, cwd=cwd)
    _state_findings(states, findings)
    findings.sort(key=lambda finding: (SEVERITIES.index(finding.severity), finding.code, finding.skill_id or ""))
    return DoctorReport(tuple(findings))
