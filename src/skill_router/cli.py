"""Command line interface for local skill search and loading."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sys

from .catalog import load_skill, scan_roots
from .config import ConfigError, RouterConfig, config_path, load_config, save_config
from .jev import JevProvider, Recommendation, recommend_local
from .manager import (
    config_after_sync,
    default_source_roots,
    run_menu,
    sync_assignments,
)
from .search import search_skills
from .state import claude_plugin_roots, inspect_skills


def default_roots(cwd: Path | None = None) -> list[Path]:
    """Return only the external skill vault roots."""

    configured = os.environ.get("SKILL_ROUTER_ROOT", "")
    if configured:
        return [Path(value).expanduser() for value in configured.split(os.pathsep) if value]
    base = cwd or Path.cwd()
    return [Path.home() / ".agents" / "skill-vault", base / ".agents" / "skill-vault"]


def _roots(values: list[str] | None) -> list[Path]:
    return [Path(value).expanduser() for value in values] if values else default_roots()


def _skill_json(skill: object) -> dict[str, object]:
    return {
        "id": skill.skill_id,
        "name": skill.name,
        "description": skill.description,
        "path": str(skill.path),
    }


def _recommendation_json(result: Recommendation) -> dict[str, object]:
    return {
        "status": result.status,
        "provider": result.provider,
        "skill": _skill_json(result.skill) if result.skill is not None else None,
        "confidence": result.confidence,
        "candidates": [
            {**_skill_json(candidate.skill), "score": round(candidate.score, 6)}
            for candidate in result.candidates
        ],
        "error": result.error,
        "metrics": asdict(result.metrics) if result.metrics is not None else None,
    }


def _print_recommendation(result: Recommendation, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_recommendation_json(result), indent=2))
        return
    if result.skill is None:
        suffix = f"\t{result.error}" if result.error else ""
        print(f"{result.status}{suffix}")
        return
    confidence = (
        f"\tconfidence={result.confidence:.3f}"
        if result.confidence is not None
        else ""
    )
    suffix = f"\t{result.error}" if result.error else ""
    print(f"{result.status}\t{result.skill.skill_id}\t{result.provider}{confidence}{suffix}")


def _recommend(args: argparse.Namespace, roots: list[Path]) -> int:
    skills = scan_roots(roots)
    provider = args.provider
    if provider == "auto":
        try:
            provider = "jev" if load_config().jev_enabled else "local"
        except ConfigError as error:
            print(str(error), file=sys.stderr)
            return 2
    if provider == "jev":
        result = JevProvider(
            model=args.model,
            timeout=args.timeout,
            shortlist_limit=args.shortlist_limit,
            confidence_threshold=args.confidence_threshold,
            fit_threshold=args.fit_threshold,
        ).recommend(skills, args.query, limit=args.limit)
    else:
        result = recommend_local(skills, args.query, limit=args.limit)
    _print_recommendation(result, as_json=args.json)
    return 0


def _config_command(args: argparse.Namespace) -> int:
    try:
        current = load_config()
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    target = config_path()
    if args.config_action == "show":
        return _show_config(current, target, args.json)

    if args.config_action == "target":
        return _target_command(current, target, args)

    updated = replace(current, jev_enabled=args.state == "enabled")
    save_config(updated)
    return _print_jev_state(target, updated.jev_enabled, args.json)


def _show_config(current: RouterConfig, target: Path, as_json: bool) -> int:
    payload = {
        "path": str(target),
        "jev": {"enabled": current.jev_enabled},
        "targets": {name: str(path) for name, path in current.roots().items()},
        "assigned_skills": sum(assignment.enabled for assignment in current.assignments),
    }
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"path\t{target}")
        print(f"jev.enabled\t{str(current.jev_enabled).lower()}")
    return 0


def _target_command(current: RouterConfig, target: Path, args: argparse.Namespace) -> int:
    if args.target_action == "show":
        payload = {
            "path": str(target),
            "targets": {name: str(path) for name, path in current.roots().items()},
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            for name, path in current.roots().items():
                print(f"{name}\t{path}")
        return 0
    roots = current.roots()
    roots[args.target_name] = Path(args.target_path).expanduser()
    save_config(replace(current, target_roots=tuple(sorted(roots.items()))))
    print(f"saved\t{args.target_name}\t{roots[args.target_name]}")
    return 0


def _print_jev_state(target: Path, enabled: bool, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"path": str(target), "jev": {"enabled": enabled}}, indent=2))
    else:
        print(f"saved\t{target}")
        print(f"jev.enabled\t{str(enabled).lower()}")
    return 0


def _manage_command(args: argparse.Namespace) -> int:
    try:
        current = load_config()
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    roots = [Path(value).expanduser() for value in args.root] if args.root else default_source_roots()
    skills = scan_roots(roots)
    saved = run_menu(skills, current, target=args.target, search=args.search)
    if saved is None:
        print("not saved")
        return 0
    path = save_config(saved)
    print(f"saved\t{path}")
    return 0


def _assignments_command(args: argparse.Namespace) -> int:
    try:
        current = load_config()
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    values = [
        {
            "id": assignment.skill_id,
            "source": str(assignment.source),
            "enabled": assignment.enabled,
            "targets": sorted(assignment.targets),
        }
        for assignment in current.assignments
    ]
    if args.json:
        print(json.dumps(values, indent=2))
    else:
        for assignment in values:
            print(
                f"{assignment['id']}\t{','.join(assignment['targets']) or '-'}\t"
                f"{'enabled' if assignment['enabled'] else 'disabled'}\t{assignment['source']}"
            )
    return 0


def _status_command(args: argparse.Namespace) -> int:
    try:
        current = load_config()
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    roots = (
        [Path(value).expanduser() for value in args.root]
        if args.root
        else claude_plugin_roots() + default_source_roots()
    )
    rows = inspect_skills(scan_roots(roots), current)
    values = [
        {
            "id": row.skill.skill_id,
            "source": row.source,
            "router": list(row.router_targets),
            "codex": row.codex_exposure,
            "claude": row.claude_exposure,
            "claude_mode": row.claude_mode,
            "claude_override": row.claude_override,
            "claude_lock": row.claude_lock,
            "path": str(row.skill.path),
        }
        for row in rows
    ]
    if args.json:
        print(json.dumps(values, indent=2))
        return 0
    print("skill\tsource\trouter\tcodex\tclaude\tclaude-mode\tclaude-lock")
    for row in values:
        print(
            f"{row['id']}\t{row['source']}\t{','.join(row['router']) or '-'}\t"
            f"{row['codex']}\t{row['claude']}\t{row['claude_mode']}\t{row['claude_lock']}"
        )
    return 0


def _sync_command(args: argparse.Namespace) -> int:
    try:
        current = load_config()
        actions = sync_assignments(current, apply=args.apply, prune=args.prune)
        if args.apply:
            save_config(config_after_sync(current, actions))
    except (ConfigError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([
            {
                "action": action.action,
                "target": action.target,
                "id": action.skill_id,
                "path": str(action.path),
                "source": str(action.source),
                "detail": action.detail,
            }
            for action in actions
        ], indent=2))
    else:
        mode = "applied" if args.apply else "plan"
        print(f"{mode}\t{len(actions)} actions")
        for action in actions:
            detail = f"\t{action.detail}" if action.detail else ""
            print(f"{action.action}\t{action.target}\t{action.skill_id}\t{action.path}{detail}")
    return 0


def _search_command(args: argparse.Namespace, roots: list[Path]) -> int:
    results = search_skills(scan_roots(roots), args.query, limit=args.limit)
    if args.json:
        print(json.dumps([
            {**_skill_json(result.skill), "score": round(result.score, 6)}
            for result in results
        ], indent=2))
    else:
        for result in results:
            print(f"{result.skill.skill_id}\t{result.score:.3f}\t{result.skill.description}")
    return 0


def _load_command(args: argparse.Namespace, roots: list[Path]) -> int:
    try:
        skill = load_skill(args.skill_id, roots)
    except KeyError as error:
        print(str(error), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({**_skill_json(skill), "body": skill.body}, indent=2))
    else:
        print(skill.body, end="" if skill.body.endswith("\n") else "\n")
    return 0


def _dispatch_persistent_command(args: argparse.Namespace) -> int | None:
    if args.command == "config":
        return _config_command(args)
    if args.command == "manage":
        return _manage_command(args)
    if args.command == "assignments":
        return _assignments_command(args)
    if args.command == "status":
        return _status_command(args)
    if args.command == "sync":
        return _sync_command(args)
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search and load skills from explicit local vaults.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="rank skill metadata")
    search.add_argument("query")
    search.add_argument("--root", action="append", help="skill vault root; repeatable")
    search.add_argument("--limit", type=int, default=3)
    search.add_argument("--json", action="store_true")

    recommend = subparsers.add_parser(
        "recommend", help="select at most one skill with local search or Jev"
    )
    recommend.add_argument("query")
    recommend.add_argument("--provider", choices=("auto", "local", "jev"), default="auto")
    recommend.add_argument("--root", action="append", help="skill vault root; repeatable")
    recommend.add_argument("--limit", type=int, default=3)
    recommend.add_argument("--shortlist-limit", type=int, default=8)
    recommend.add_argument("--model", default="jev-latest")
    recommend.add_argument("--timeout", type=float, default=5.0)
    recommend.add_argument("--confidence-threshold", type=float, default=0.30)
    recommend.add_argument("--fit-threshold", type=float, default=0.30)
    recommend.add_argument("--json", action="store_true")

    config = subparsers.add_parser("config", help="manage persistent router settings")
    config_subparsers = config.add_subparsers(dest="config_action", required=True)
    show = config_subparsers.add_parser("show", help="show saved settings")
    show.add_argument("--json", action="store_true")
    set_config = config_subparsers.add_parser("set", help="set a saved setting")
    set_config.add_argument("setting", choices=("jev",))
    set_config.add_argument("state", choices=("enabled", "disabled"))
    set_config.add_argument("--json", action="store_true")
    target_config = config_subparsers.add_parser("target", help="manage agent skill roots")
    target_subparsers = target_config.add_subparsers(dest="target_action", required=True)
    target_show = target_subparsers.add_parser("show", help="show agent skill roots")
    target_show.add_argument("--json", action="store_true")
    target_set = target_subparsers.add_parser("set", help="set an agent skill root")
    target_set.add_argument("target_name", choices=("codex", "claude"))
    target_set.add_argument("target_path")

    manage = subparsers.add_parser("manage", help="select skills and agent targets")
    manage.add_argument("--root", action="append", help="skill root; repeatable")
    manage.add_argument("--target", choices=("codex", "claude"), default="codex")
    manage.add_argument("--search", default="", help="initial skill filter")

    assignments = subparsers.add_parser("assignments", help="show saved skill assignments")
    assignments.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="show router and native agent skill state")
    status.add_argument("--root", action="append", help="skill root; repeatable")
    status.add_argument("--json", action="store_true")

    sync = subparsers.add_parser("sync", help="plan or apply selected skill links")
    sync.add_argument("--apply", action="store_true", help="create safe links")
    sync.add_argument("--prune", action="store_true", help="remove only managed links")
    sync.add_argument("--json", action="store_true")

    load = subparsers.add_parser("load", help="load one skill body")
    load.add_argument("skill_id")
    load.add_argument("--root", action="append", help="skill vault root; repeatable")
    load.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    persistent_result = _dispatch_persistent_command(args)
    if persistent_result is not None:
        return persistent_result
    roots = _roots(args.root)
    if args.command == "search":
        return _search_command(args, roots)

    if args.command == "recommend":
        return _recommend(args, roots)
    return _load_command(args, roots)


if __name__ == "__main__":
    raise SystemExit(main())
