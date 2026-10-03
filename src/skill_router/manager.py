"""Skill assignment and native target synchronization."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import shutil
import sys
import textwrap
from typing import Callable, Iterable

from .catalog import Skill, scan_roots
from .config import (
    TARGETS,
    ManagedLink,
    RouterConfig,
    SkillAssignment,
    effective_native_targets,
)
from .state import SkillState, claude_plugin_roots, inspect_skills


COLUMN_TARGETS: tuple[tuple[str, str], ...] = (
    ("router", "claude"),
    ("router", "codex"),
    ("native", "codex"),
    ("native", "claude"),
)


@dataclass(frozen=True)
class SyncAction:
    """One planned or applied target change."""

    action: str
    target: str
    skill_id: str
    path: Path
    source: Path
    detail: str = ""


@dataclass(frozen=True)
class RipwireAdoptionPlan:
    """A plan to move Ripwire exposure under router control."""

    config: RouterConfig
    shared_links: tuple[Path, ...]


@dataclass(frozen=True)
class NativeAdoptionPlan:
    """A plan to move one native skill into router-owned storage."""

    config: RouterConfig
    skill_id: str
    source_dir: Path
    router_dir: Path
    native_paths: tuple[Path, ...]
    shared_paths: tuple[Path, ...]


def ripwire_source_root(home: Path | None = None) -> Path:
    """Return Ripwire's installed skill source root."""

    return _local_share_source_root(home, "ripwire")


def router_source_root(home: Path | None = None) -> Path:
    """Return the router-owned skill source root."""

    return _local_share_source_root(home, "skill-router")


def _local_share_source_root(home: Path | None, owner: str) -> Path:
    """Return one owner directory under the local data root."""

    return (home or Path.home()) / ".local" / "share" / owner / "skills"


def _provider_source_roots(home: Path) -> list[Path]:
    ripwire = ripwire_source_root(home)
    return [ripwire] if ripwire.is_dir() else []


def _native_source_roots() -> list[Path]:
    home = Path.home()
    return claude_plugin_roots() + [
        home / ".agents" / "skills",
        home / ".codex" / "skills",
        home / ".claude" / "skills",
    ]


def default_source_roots() -> list[Path]:
    """Return all personal, provider, native, and installed skill roots."""

    router = router_source_root(Path.home())
    roots = _provider_source_roots(Path.home())
    if router.is_dir():
        roots.append(router)
    return roots + _native_source_roots()


def selector_source_roots() -> list[Path]:
    """Return router and provider roots for the default selector view."""

    home = Path.home()
    roots = _provider_source_roots(home)
    router = router_source_root(home)
    if router.is_dir():
        roots.append(router)
    return roots


def _same_directory(left: Path, right: Path) -> bool:
    """Return whether two directories contain the same files and bytes."""

    left_files = {
        path.relative_to(left).as_posix()
        for path in left.rglob("*")
        if path.is_file()
    }
    right_files = {
        path.relative_to(right).as_posix()
        for path in right.rglob("*")
        if path.is_file()
    }
    if left_files != right_files:
        return False
    for relative in left_files:
        try:
            if left.joinpath(relative).read_bytes() != right.joinpath(relative).read_bytes():
                return False
        except (OSError, UnicodeError):
            return False
    return True


def _native_skill_paths(skill_id: str, home: Path, config: RouterConfig) -> tuple[Path, ...]:
    """Return existing direct native paths for one skill."""

    roots = [home / ".agents" / "skills", *config.roots().values()]
    values: list[Path] = []
    for root in dict.fromkeys(roots):
        path = root / skill_id
        if path.is_dir() or path.is_symlink():
            values.append(path)
    return tuple(values)


def native_skill(
    skill_id: str,
    config: RouterConfig,
    *,
    home: Path | None = None,
) -> Skill | None:
    """Find a skill from a direct native path, including a safe symlink target."""

    home = home or Path.home()
    wanted = skill_id.casefold()
    for native_path in _native_skill_paths(skill_id, home, config):
        source_dir = native_path.resolve()
        if ".claude" in source_dir.parts and "plugins" in source_dir.parts:
            continue
        candidate = source_dir / "SKILL.md"
        if not candidate.is_file():
            continue
        for skill in scan_roots([source_dir.parent]):
            if skill.skill_id.casefold() == wanted and skill.path.parent.resolve() == source_dir:
                return skill
    return None


def _validate_native_paths(
    native_paths: Iterable[Path],
    source_dir: Path,
    router_dir: Path,
    skill_id: str,
) -> None:
    """Validate direct native copies before adoption changes files."""

    _validate_native_source(native_paths, source_dir, router_dir, skill_id)
    _validate_router_source(source_dir, router_dir)
    for path in native_paths:
        _validate_native_path(path, source_dir, router_dir)


def _validate_native_source(
    native_paths: Iterable[Path],
    source_dir: Path,
    router_dir: Path,
    skill_id: str,
) -> None:
    """Require the selected source to be a direct native path."""

    allowed_sources = {path.resolve() for path in native_paths if path.is_dir()}
    if source_dir != router_dir.resolve() and source_dir not in allowed_sources:
        raise ValueError(f"skill is not a direct native skill: {skill_id}")


def _validate_router_source(source_dir: Path, router_dir: Path) -> None:
    """Reject a different existing router copy."""

    if router_dir.exists() and router_dir.resolve() != source_dir:
        if not _same_directory(router_dir, source_dir):
            raise ValueError(f"router source already differs: {router_dir}")


def _validate_native_path(path: Path, source_dir: Path, router_dir: Path) -> None:
    """Validate one native copy or link."""

    if path.is_symlink():
        target = path.resolve()
        if target != source_dir and target != router_dir.resolve():
            raise ValueError(f"native link points elsewhere: {path}")
    elif path.resolve() != source_dir and not _same_directory(path, source_dir):
        raise ValueError(f"native copy differs: {path}")


def _native_exposed_targets(
    config: RouterConfig,
    skill_id: str,
    native_paths: Iterable[Path],
    shared_root: Path,
) -> frozenset[str]:
    """Return targets that currently expose a direct native skill."""

    values = set()
    if any(path.parent == shared_root for path in native_paths):
        values.update(TARGETS)
    else:
        values.update(
            target
            for target, root in config.roots().items()
            if root / skill_id in native_paths
        )
    return frozenset(values & set(TARGETS))


def plan_native_adoption(
    config: RouterConfig,
    skill: Skill,
    *,
    home: Path | None = None,
) -> NativeAdoptionPlan:
    """Plan ownership of one direct native skill without changing files."""

    home = home or Path.home()
    source_dir = skill.path.resolve().parent
    router_dir = router_source_root(home) / skill.skill_id
    native_paths = _native_skill_paths(skill.skill_id, home, config)
    _validate_native_paths(native_paths, source_dir, router_dir, skill.skill_id)

    shared_root = home / ".agents" / "skills"
    shared_paths = tuple(path for path in native_paths if path.parent == shared_root)
    assignments = config.assignment_map()
    current = assignments.get(skill.skill_id)
    exposed_targets = _native_exposed_targets(config, skill.skill_id, native_paths, shared_root)
    if current is None:
        router_targets = exposed_targets
    else:
        router_targets = current.targets
    assignments[skill.skill_id] = SkillAssignment(
        skill_id=skill.skill_id,
        source=router_dir / "SKILL.md",
        targets=router_targets,
        enabled=bool(router_targets or exposed_targets),
        native_targets=exposed_targets,
    )
    updated = replace(
        config,
        target_roots=tuple(sorted(config.roots().items())),
        assignments=tuple(
            sorted(assignments.values(), key=lambda item: item.skill_id.casefold())
        ),
    )
    return NativeAdoptionPlan(
        updated,
        skill.skill_id,
        source_dir,
        router_dir,
        tuple(sorted(native_paths)),
        tuple(sorted(shared_paths)),
    )


def apply_native_adoption(plan: NativeAdoptionPlan) -> tuple[RouterConfig, list[SyncAction]]:
    """Move one native skill into router storage and link configured targets."""

    plan.router_dir.parent.mkdir(parents=True, exist_ok=True)
    if not plan.router_dir.exists():
        shutil.copytree(plan.source_dir, plan.router_dir)
    backup_root = plan.router_dir.parent.parent / "backups" / plan.skill_id
    roots = plan.config.roots()
    for path in plan.native_paths:
        if path.resolve() == plan.router_dir.resolve():
            continue
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            label = path.parent.parent.name
            backup = backup_root / label
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                shutil.rmtree(backup)
            path.rename(backup)
        if path.parent == roots.get("codex") or path.parent == roots.get("claude"):
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(plan.router_dir.resolve(), path, target_is_directory=True)
    actions = sync_assignments(plan.config, apply=True, prune=True)
    return config_after_sync(plan.config, actions), actions


def plan_ripwire_adoption(
    config: RouterConfig,
    skills: Iterable[Skill],
    *,
    home: Path | None = None,
) -> RipwireAdoptionPlan:
    """Plan ownership of installed Ripwire skills without changing files."""

    home = home or Path.home()
    source_root = ripwire_source_root(home).resolve()
    shared_root = home / ".agents" / "skills"
    assignments = config.assignment_map()
    shared_links: list[Path] = []
    for skill in skills:
        try:
            skill_source = skill.path.resolve()
            skill_source.relative_to(source_root)
        except (FileNotFoundError, OSError, ValueError):
            continue
        shared_link = shared_root / skill.skill_id
        if shared_link.is_symlink() and shared_link.resolve() == skill_source.parent:
            shared_links.append(shared_link)
        current = assignments.get(skill.skill_id)
        if current is None:
            exposed = frozenset({"codex", "claude"}) if shared_link in shared_links else frozenset()
            assignments[skill.skill_id] = SkillAssignment(
                skill_id=skill.skill_id,
                source=skill.path,
                targets=exposed,
                enabled=bool(exposed),
                native_targets=exposed,
            )
        else:
            assignments[skill.skill_id] = replace(current, source=skill.path)
    updated = replace(
        config,
        target_roots=tuple(sorted(config.roots().items())),
        assignments=tuple(
            sorted(assignments.values(), key=lambda item: item.skill_id.casefold())
        ),
    )
    return RipwireAdoptionPlan(updated, tuple(sorted(shared_links)))


def apply_ripwire_adoption(plan: RipwireAdoptionPlan) -> tuple[RouterConfig, list[SyncAction]]:
    """Remove shared Ripwire links and create router-managed links."""

    planned = sync_assignments(plan.config, prune=True)
    conflicts = [
        action
        for action in planned
        if action.skill_id.startswith("ripwire-")
        and action.action in {"conflict", "invalid-source", "invalid-target"}
    ]
    if conflicts:
        details = "; ".join(
            f"{action.target}/{action.skill_id}: {action.detail or action.action}"
            for action in conflicts
        )
        raise ValueError(f"cannot adopt Ripwire safely: {details}")
    for path in plan.shared_links:
        path.unlink()
    actions = sync_assignments(plan.config, apply=True, prune=True)
    return config_after_sync(plan.config, actions), actions


def assignment_config(
    config: RouterConfig,
    skills: Iterable[Skill],
    selected: dict[str, set[str]],
    native_selected: dict[str, set[str]] | None = None,
) -> RouterConfig:
    """Apply router and native menu selections while preserving saved assignments."""

    discovered = {skill.skill_id: skill for skill in skills}
    assignments = config.assignment_map()
    for skill_id, skill in discovered.items():
        targets = frozenset(selected.get(skill_id, set()) & set(TARGETS))
        if native_selected is None:
            current = assignments.get(skill_id)
            native_values = (
                effective_native_targets(current)
                if current is not None and current.enabled
                else frozenset()
            )
        else:
            native_values = frozenset(native_selected.get(skill_id, set()) & set(TARGETS))
        assignments[skill_id] = SkillAssignment(
            skill_id=skill_id,
            source=skill.path,
            targets=targets,
            enabled=bool(targets or native_values),
            native_targets=native_values,
        )
    return RouterConfig(
        jev_enabled=config.jev_enabled,
        target_roots=tuple(sorted(config.roots().items())),
        assignments=tuple(sorted(assignments.values(), key=lambda item: item.skill_id.casefold())),
        managed_links=config.managed_links,
    )


def _menu_selection(config: RouterConfig) -> dict[str, set[str]]:
    return {
        assignment.skill_id: set(assignment.targets) if assignment.enabled else set()
        for assignment in config.assignments
    }


def _native_selection(
    config: RouterConfig,
    states: dict[str, SkillState] | None = None,
) -> dict[str, set[str]]:
    """Return saved native selections, with direct exposure as a safe baseline."""

    assignments = config.assignment_map()
    selected: dict[str, set[str]] = {}
    for skill_id, assignment in assignments.items():
        selected[skill_id] = (
            set(effective_native_targets(assignment)) if assignment.enabled else set()
        )
    if states is None:
        return selected
    for skill_id, state in states.items():
        assignment = assignments.get(skill_id)
        if assignment is not None and (
            assignment.enabled or assignment.native_targets is not None
        ):
            continue
        targets = selected.setdefault(skill_id, set())
        if state.codex_exposure in {"native", "managed"}:
            targets.add("codex")
        if state.claude_exposure in {"native", "managed"}:
            targets.add("claude")
    return selected


def _visible_skills(skills: list[Skill], filter_text: str) -> list[Skill]:
    return [
        skill
        for skill in skills
        if not filter_text
        or filter_text in skill.skill_id.casefold()
        or filter_text in skill.name.casefold()
        or filter_text in skill.description.casefold()
    ]


def _menu_states(skills: list[Skill], config: RouterConfig) -> dict[str, SkillState]:
    return {row.skill.skill_id: row for row in inspect_skills(skills, config)}


def _layer_selection(
    router_selected: dict[str, set[str]],
    native_selected: dict[str, set[str]],
    layer: str,
) -> dict[str, set[str]]:
    return native_selected if layer == "native" else router_selected


def _router_mark(skill_id: str, selected: dict[str, set[str]], target: str) -> str:
    return "yes" if target in selected.get(skill_id, set()) else "no"


def _render_menu(
    visible: list[Skill],
    active_layer: str,
    active_target: str,
    router_selected: dict[str, set[str]],
    native_selected: dict[str, set[str]],
    output_fn: Callable[[str], None],
) -> None:
    output_fn("")
    output_fn(
        f"Skill selector: edit {active_layer} target {active_target} ({len(visible)} shown)"
    )
    terminal_width = shutil.get_terminal_size(fallback=(120, 24)).columns
    skill_width = max([len("skill"), *(len(skill.skill_id) for skill in visible)], default=5)
    labels = ("router claude", "router codex", "codex native", "claude native")
    heading = "  ".join(
        f"{('> ' if layer == active_layer and target == active_target else '  ')}{label:<{width - 2}}"
        for label, (layer, target), width in zip(
            labels, COLUMN_TARGETS, (15, 14, 14, 15)
        )
    )
    output_fn(f"{'':>3}  {'skill':<{skill_width}}  {heading}  description")
    for index, skill in enumerate(visible, 1):
        prefix = (
            f"{index:>3}. {skill.skill_id:<{skill_width}}  "
            f"{_router_mark(skill.skill_id, router_selected, 'claude'):<13}  "
            f"{_router_mark(skill.skill_id, router_selected, 'codex'):<12}  "
            f"{_router_mark(skill.skill_id, native_selected, 'codex'):<12}  "
            f"{_router_mark(skill.skill_id, native_selected, 'claude'):<13}  "
        )
        available = terminal_width - len(prefix) - 2
        if available < 3:
            output_fn(prefix.rstrip() + "...")
            continue
        description_width = max(8, available)
        description = textwrap.shorten(skill.description, width=description_width, placeholder="...")
        output_fn(prefix + "— " + description)
    output_fn(
        "Commands: number(s) toggle | m router/native | t target | a all | n none | "
        "f text | s save+sync | q quit"
    )
    output_fn(
        "Save+sync removes only managed links; direct and plugin folders stay."
    )


def _set_visible(
    visible: list[Skill],
    selected: dict[str, set[str]],
    target: str,
    enabled: bool,
) -> None:
    for skill in visible:
        targets = selected.setdefault(skill.skill_id, set())
        if enabled:
            targets.add(target)
        else:
            targets.discard(target)


def _toggle_numbers(
    command: str,
    visible: list[Skill],
    selected: dict[str, set[str]],
    active_target: str,
    output_fn: Callable[[str], None],
) -> None:
    try:
        numbers = [int(value) for value in command.replace(",", " ").split()]
    except ValueError:
        output_fn("Enter skill numbers or a menu command.")
        return
    if not numbers or any(number < 1 or number > len(visible) for number in numbers):
        output_fn("Skill number is outside the displayed list.")
        return
    for number in numbers:
        skill_id = visible[number - 1].skill_id
        targets = selected.setdefault(skill_id, set())
        if active_target in targets:
            targets.remove(active_target)
        else:
            targets.add(active_target)


def _read_curses_key(screen: object, curses: object) -> int:
    key = screen.getch()
    if key != 27:
        return key
    screen.timeout(50)
    try:
        if screen.getch() != ord("["):
            return 27
        code = screen.getch()
        return {
            ord("A"): curses.KEY_UP,
            ord("B"): curses.KEY_DOWN,
            ord("C"): curses.KEY_RIGHT,
            ord("D"): curses.KEY_LEFT,
        }.get(code, 27)
    finally:
        screen.timeout(-1)


def _review_body_lines(skill: Skill, width: int) -> list[str]:
    lines: list[str] = []
    for source_line in skill.body.splitlines() or [""]:
        lines.extend(
            textwrap.wrap(
                source_line,
                width=width,
                replace_whitespace=False,
                drop_whitespace=False,
            )
            or [""]
        )
    return lines


def _review_curses_skill(screen: object, skill: Skill, curses: object) -> None:
    offset = 0
    while True:
        height, width = screen.getmaxyx()
        line_width = max(1, width - 1)
        body_lines = _review_body_lines(skill, line_width)
        row_limit = max(1, height - 3)
        max_offset = max(0, len(body_lines) - row_limit)
        offset = min(offset, max_offset)
        screen.erase()
        screen.addnstr(
            0,
            0,
            f"Review: {skill.skill_id} | Enter/Esc back | Up/Down scroll | q back",
            line_width,
            curses.A_BOLD,
        )
        screen.addnstr(1, 0, f"Source: {skill.path}", line_width, curses.A_DIM)
        for row, line in enumerate(body_lines[offset : offset + row_limit], start=2):
            screen.addnstr(row, 0, line, line_width)
        end = min(len(body_lines), offset + row_limit)
        screen.addnstr(
            height - 1,
            0,
            f"Lines {offset + 1}-{end} of {len(body_lines)}",
            line_width,
            curses.A_DIM,
        )
        screen.refresh()
        key = _read_curses_key(screen, curses)
        if key in (10, 13, curses.KEY_ENTER, 27, ord("q"), ord("Q")):
            return
        if key == curses.KEY_UP:
            offset = max(0, offset - 1)
        elif key == curses.KEY_DOWN:
            offset = min(max_offset, offset + 1)
        elif key == curses.KEY_PPAGE:
            offset = max(0, offset - row_limit)
        elif key == curses.KEY_NPAGE:
            offset = min(max_offset, offset + row_limit)
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = max_offset


def _run_curses_menu(
    skills: list[Skill],
    config: RouterConfig,
    *,
    target: str,
    search: str,
) -> RouterConfig | None:
    """Run the arrow-key menu inside a terminal."""

    import curses

    by_id = {skill.skill_id: skill for skill in skills}
    states = _menu_states(skills, config)
    router_selected = _menu_selection(config)
    native_selected = _native_selection(config, states)
    active_column = 1 if target == "codex" else 0
    filter_text = search.casefold().strip()
    cursor = 0

    def prompt_filter(screen: object) -> str:
        height, width = screen.getmaxyx()
        screen.move(height - 1, 0)
        screen.clrtoeol()
        screen.addnstr(height - 1, 0, "Filter: ", max(1, width - 1))
        curses.echo()
        try:
            value = screen.getstr(height - 1, min(8, max(0, width - 1)), max(1, width - 9))
        finally:
            curses.noecho()
        return value.decode("utf-8", errors="replace").casefold().strip()

    def draw(screen: object) -> list[Skill]:
        nonlocal cursor
        visible = _visible_skills(skills, filter_text)
        cursor = min(cursor, max(0, len(visible) - 1))
        height, width = screen.getmaxyx()
        screen.erase()

        def safe_add(row: int, column: int, value: str, attribute: int = 0) -> None:
            if row < 0 or row >= height or column < 0 or column >= width:
                return
            count = min(len(value), width - column - 1)
            if count < 1:
                return
            try:
                screen.addnstr(row, column, value, count, attribute)
            except curses.error:
                return

        safe_add(
            0,
            0,
            "Skill selector | Up/Down skill | Left/Right column | Space change | Enter review | "
            "s save | / filter | q quit",
            curses.A_DIM,
        )
        skill_width = max([len("skill"), *(len(skill.skill_id) for skill in visible)], default=5)
        header_prefix = f"{'':>3}  {'skill':<{skill_width}}  "

        safe_add(1, 0, header_prefix, curses.A_DIM)
        header_column_widths = (15, 14, 14, 15)
        header_column = len(header_prefix)
        for column, ((layer, target), column_width) in enumerate(
            zip(COLUMN_TARGETS, header_column_widths)
        ):
            label = f"{layer} {target}"
            if column == active_column:
                label = f"> {label.upper()}"
                attribute = curses.A_DIM | curses.A_REVERSE
            else:
                attribute = curses.A_DIM
            safe_add(1, header_column, f"{label:<{column_width}}", attribute)
            header_column += column_width
            if column < len(COLUMN_TARGETS) - 1:
                safe_add(1, header_column, "  ", curses.A_DIM)
                header_column += 2
        safe_add(1, header_column, "description", curses.A_DIM)
        row_limit = max(1, height - 4)
        offset = min(max(0, cursor - row_limit + 1), max(0, len(visible) - row_limit))
        for row, skill in enumerate(visible[offset : offset + row_limit]):
            index = offset + row
            prefix = f"{index + 1:>3}. {skill.skill_id:<{skill_width}}  "
            safe_add(row + 2, 0, prefix, curses.A_BOLD if index == cursor else 0)
            column_position = len(prefix)
            for column, ((layer, target), column_width) in enumerate(
                zip(COLUMN_TARGETS, header_column_widths)
            ):
                selected = _layer_selection(router_selected, native_selected, layer)
                value = _router_mark(skill.skill_id, selected, target)
                cell = f" {value:^5} "
                attribute = curses.A_REVERSE if column == active_column else 0
                safe_add(row + 2, column_position, f"{cell:<{column_width}}", attribute)
                column_position += column_width
                if column < len(COLUMN_TARGETS) - 1:
                    safe_add(row + 2, column_position, "  ")
                    column_position += 2
            description_width = max(8, width - column_position - 3)
            description = textwrap.shorten(skill.description, width=description_width, placeholder="...")
            safe_add(row + 2, column_position, "— " + description)
        if not visible:
            safe_add(1, 0, "No matching skills.")
        active_layer, active_target = COLUMN_TARGETS[active_column]
        status = f"Active: {active_layer} {active_target} | Filter: {filter_text or '-'}"
        safe_add(max(0, height - 1), 0, status, curses.A_DIM)
        screen.refresh()
        return visible

    def loop(screen: object) -> RouterConfig | None:
        nonlocal active_column, cursor, filter_text

        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        while True:
            visible = draw(screen)
            key = _read_curses_key(screen, curses)
            if key == curses.KEY_UP:
                cursor = max(0, cursor - 1)
            elif key == curses.KEY_DOWN:
                cursor = min(max(0, len(visible) - 1), cursor + 1)
            elif key == curses.KEY_PPAGE:
                cursor = max(0, cursor - max(1, screen.getmaxyx()[0] - 3))
            elif key == curses.KEY_NPAGE:
                cursor = min(max(0, len(visible) - 1), cursor + max(1, screen.getmaxyx()[0] - 3))
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = max(0, len(visible) - 1)
            elif key == curses.KEY_LEFT:
                active_column = max(0, active_column - 1)
            elif key == curses.KEY_RIGHT:
                active_column = min(len(COLUMN_TARGETS) - 1, active_column + 1)
            elif key == ord(" ") and visible:
                skill_id = visible[cursor].skill_id
                active_layer, active_target = COLUMN_TARGETS[active_column]
                selected = _layer_selection(router_selected, native_selected, active_layer)
                targets = selected.setdefault(skill_id, set())
                if active_target in targets:
                    targets.remove(active_target)
                else:
                    targets.add(active_target)
            elif key in (ord("a"), ord("n")):
                active_layer, active_target = COLUMN_TARGETS[active_column]
                selected = _layer_selection(router_selected, native_selected, active_layer)
                _set_visible(visible, selected, active_target, enabled=key == ord("a"))
            elif key == ord("/"):
                filter_text = prompt_filter(screen)
                cursor = 0
            elif key in (10, 13, curses.KEY_ENTER) and visible:
                _review_curses_skill(screen, visible[cursor], curses)
            elif key == ord("s"):
                return assignment_config(config, by_id.values(), router_selected, native_selected)
            elif key in (ord("q"), ord("Q"), 3, 27):
                return None

    return curses.wrapper(loop)


def _run_line_menu(
    skills: list[Skill],
    config: RouterConfig,
    *,
    target: str = "codex",
    search: str = "",
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> RouterConfig | None:
    """Run a line-based checkbox menu and return saved settings.

    Commands are numbers to toggle, ``m`` to switch router or native,
    ``a`` to select all, ``n`` to clear all, ``t codex`` or ``t claude`` to
    change target, ``f text`` to filter, ``s`` to save and sync, and ``q`` to quit.
    """

    if target not in TARGETS:
        raise ValueError(f"unknown target: {target}")
    filter_text = search.casefold().strip()
    active_target = target
    by_id = {skill.skill_id: skill for skill in skills}
    states = _menu_states(skills, config)
    router_selected = _menu_selection(config)
    native_selected = _native_selection(config, states)
    active_layer = "router"

    while True:
        visible = _visible_skills(skills, filter_text)
        _render_menu(
            visible,
            active_layer,
            active_target,
            router_selected,
            native_selected,
            output_fn,
        )
        command = input_fn("manage> ").strip()
        lowered = command.casefold()
        if lowered == "q":
            return None
        if lowered == "s":
            return assignment_config(config, by_id.values(), router_selected, native_selected)
        if lowered in {"a", "n"}:
            selected = _layer_selection(router_selected, native_selected, active_layer)
            _set_visible(visible, selected, active_target, enabled=lowered == "a")
            continue
        if lowered in {"m", "mode", "layer"}:
            active_layer = "native" if active_layer == "router" else "router"
            continue
        parts = command.split(maxsplit=1)
        if len(parts) == 2 and parts[0].casefold() in {"m", "mode", "layer"}:
            if parts[1].casefold() in {"router", "native"}:
                active_layer = parts[1].casefold()
            else:
                output_fn(f"Unknown layer: {parts[1]}")
            continue
        if len(parts) == 2 and parts[0].casefold() == "t":
            if parts[1].casefold() in TARGETS:
                active_target = parts[1].casefold()
            else:
                output_fn(f"Unknown target: {parts[1]}")
            continue
        if len(parts) == 2 and parts[0].casefold() == "f":
            filter_text = parts[1].casefold().strip()
            continue
        if command.casefold() in {"f", "filter"}:
            filter_text = ""
            continue
        selected = _layer_selection(router_selected, native_selected, active_layer)
        _toggle_numbers(command, visible, selected, active_target, output_fn)


def run_menu(
    skills: list[Skill],
    config: RouterConfig,
    *,
    target: str = "codex",
    search: str = "",
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> RouterConfig | None:
    """Run the arrow menu in a terminal and the line menu elsewhere."""

    if target not in TARGETS:
        raise ValueError(f"unknown target: {target}")
    if input_fn is input and output_fn is print and sys.stdin.isatty() and sys.stdout.isatty():
        return _run_curses_menu(skills, config, target=target, search=search)
    return _run_line_menu(
        skills,
        config,
        target=target,
        search=search,
        input_fn=input_fn,
        output_fn=output_fn,
    )


def _assignment_actions(
    assignment: SkillAssignment,
    roots: dict[str, Path],
    active: set[tuple[str, str]],
) -> list[SyncAction]:
    native_targets = effective_native_targets(assignment)
    if not assignment.enabled or not native_targets:
        return []
    source_file = assignment.source.expanduser()
    if not source_file.is_file() or source_file.name != "SKILL.md":
        return [
            SyncAction(
                "invalid-source",
                target,
                assignment.skill_id,
                Path(),
                source_file,
                "SKILL.md missing",
            )
            for target in sorted(native_targets)
        ]
    source_dir = source_file.parent.resolve()
    actions: list[SyncAction] = []
    for target in sorted(native_targets):
        if target not in roots:
            actions.append(
                SyncAction(
                    "invalid-target",
                    target,
                    assignment.skill_id,
                    Path(),
                    source_file,
                    "target root missing",
                )
            )
            continue
        active.add((target, assignment.skill_id))
        path = roots[target].expanduser() / assignment.skill_id
        if path.exists() or path.is_symlink():
            action = "keep" if path.is_symlink() and path.resolve() == source_dir else "conflict"
            detail = "" if action == "keep" else "destination exists"
            actions.append(SyncAction(action, target, assignment.skill_id, path, source_file, detail))
            continue
        actions.append(SyncAction("link", target, assignment.skill_id, path, source_file))
    return actions


def _prune_actions(
    managed: Iterable[ManagedLink],
    active: set[tuple[str, str]],
) -> list[SyncAction]:
    actions: list[SyncAction] = []
    for link in managed:
        if (link.target, link.skill_id) in active:
            continue
        if link.path.is_symlink() and link.path.resolve() == link.source.parent.resolve():
            actions.append(SyncAction("unlink", link.target, link.skill_id, link.path, link.source))
        else:
            actions.append(
                SyncAction(
                    "skip-prune",
                    link.target,
                    link.skill_id,
                    link.path,
                    link.source,
                    "link changed or missing",
                )
            )
    return actions


def _apply_actions(actions: Iterable[SyncAction]) -> None:
    for action in actions:
        if action.action == "link":
            action.path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(action.source.parent.resolve(), action.path, target_is_directory=True)
        elif action.action == "unlink":
            action.path.unlink()


def sync_assignments(
    config: RouterConfig,
    *,
    apply: bool = False,
    prune: bool = False,
) -> list[SyncAction]:
    """Plan or apply safe symlinks for selected skills.

    Existing files and directories are never replaced. Pruning removes only
    links recorded by this tool when they still point to the recorded source.
    """

    roots = config.roots()
    actions: list[SyncAction] = []
    active: set[tuple[str, str]] = set()
    for assignment in config.assignments:
        actions.extend(_assignment_actions(assignment, roots, active))

    if prune:
        actions.extend(_prune_actions(config.managed_links, active))

    if not apply:
        return actions
    _apply_actions(actions)
    return actions


def config_after_sync(config: RouterConfig, actions: Iterable[SyncAction]) -> RouterConfig:
    """Return config with the links confirmed by a sync operation."""

    links = {
        (link.target, link.skill_id): link
        for link in config.managed_links
    }
    for action in actions:
        key = (action.target, action.skill_id)
        if action.action == "unlink":
            links.pop(key, None)
        elif action.action in {"link", "keep"}:
            links[key] = ManagedLink(action.target, action.skill_id, action.path, action.source)
    return replace(
        config,
        managed_links=tuple(
            sorted(links.values(), key=lambda link: (link.target, link.skill_id.casefold()))
        ),
    )
