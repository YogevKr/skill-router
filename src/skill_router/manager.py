"""Skill assignment and native target synchronization."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import shutil
import sys
import textwrap
from typing import Callable, Iterable

from .catalog import Skill
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


def default_source_roots() -> list[Path]:
    """Return the personal skill roots used by the manager."""

    home = Path.home()
    return claude_plugin_roots() + [
        home / ".agents" / "skills",
        home / ".codex" / "skills",
        home / ".claude" / "skills",
    ]


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
        f"Skill manager: edit {active_layer} target {active_target} ({len(visible)} shown)"
    )
    terminal_width = shutil.get_terminal_size(fallback=(120, 24)).columns
    skill_width = max([len("skill"), *(len(skill.skill_id) for skill in visible)], default=5)
    output_fn(
        f"{'':>3}  {'skill':<{skill_width}}  {'router claude':<13}  {'router codex':<12}  "
        f"{'codex native':<12}  {'claude native':<13}  description"
    )
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
        "f text | s save | q quit"
    )
    output_fn(
        "Native off removes only router links during sync --prune; direct and plugin folders stay."
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
        screen.addnstr(
            0,
            0,
            "Up/Down row | Left/Right column | Space toggle | a all | n none | "
            "Enter save | / filter | q quit",
            max(1, width - 1),
            curses.A_DIM,
        )
        skill_width = max([len("skill"), *(len(skill.skill_id) for skill in visible)], default=5)
        header_prefix = f"{'':>3}  {'skill':<{skill_width}}  "

        def add_header(value: str, column: int, attribute: int) -> None:
            if column >= width or not value:
                return
            screen.addnstr(1, column, value, min(len(value), width - column), attribute)

        add_header(header_prefix, 0, curses.A_DIM)
        header_column_widths = (13, 12, 12, 13)
        header_column = len(header_prefix)
        for column, ((layer, target), column_width) in enumerate(
            zip(COLUMN_TARGETS, header_column_widths)
        ):
            label = f"{layer} {target}"
            if column == active_column:
                label = label.upper()
                attribute = curses.A_DIM | curses.A_REVERSE
            else:
                attribute = curses.A_DIM
            add_header(f"{label:<{column_width}}", header_column, attribute)
            header_column += column_width
            if column < len(COLUMN_TARGETS) - 1:
                add_header("  ", header_column, curses.A_DIM)
                header_column += 2
        add_header("description", header_column, curses.A_DIM)
        row_limit = max(1, height - 4)
        offset = min(max(0, cursor - row_limit + 1), max(0, len(visible) - row_limit))
        for row, skill in enumerate(visible[offset : offset + row_limit]):
            index = offset + row
            prefix = (
                f"{index + 1:>3}. {skill.skill_id:<{skill_width}}  "
                f"{_router_mark(skill.skill_id, router_selected, 'claude'):<13}  "
                f"{_router_mark(skill.skill_id, router_selected, 'codex'):<12}  "
                f"{_router_mark(skill.skill_id, native_selected, 'codex'):<12}  "
                f"{_router_mark(skill.skill_id, native_selected, 'claude'):<13}  "
            )
            description_width = max(8, width - len(prefix) - 3)
            description = textwrap.shorten(skill.description, width=description_width, placeholder="...")
            line = prefix + "— " + description
            if index == cursor:
                screen.attron(curses.A_REVERSE)
            screen.addnstr(row + 2, 0, line, max(1, width - 1))
            if index == cursor:
                screen.attroff(curses.A_REVERSE)
        if not visible:
            screen.addnstr(1, 0, "No matching skills.", max(1, width - 1))
        status = f"Filter: {filter_text or '-'}"
        screen.addnstr(max(0, height - 1), 0, status, max(1, width - 1))
        screen.refresh()
        return visible

    def loop(screen: object) -> RouterConfig | None:
        nonlocal active_column, cursor, filter_text

        def read_key() -> int:
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

        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        while True:
            visible = draw(screen)
            key = read_key()
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
            elif key in (10, 13, curses.KEY_ENTER):
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
    change target, ``f text`` to filter, ``s`` to save, and ``q`` to quit.
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
