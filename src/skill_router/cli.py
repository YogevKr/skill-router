"""Command line interface for local skill search and loading."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sys

from .catalog import load_skill, scan_roots
from .config import (
    ConfigError,
    RouterConfig,
    config_path,
    effective_native_targets,
    load_config,
    save_config,
)
from .jev import JevProvider, Recommendation, recommend_local
from .manager import (
    NativeAdoptionPlan,
    apply_native_adoption,
    apply_ripwire_adoption,
    config_after_sync,
    default_source_roots,
    plan_native_adoption,
    plan_ripwire_adoption,
    ripwire_source_root,
    run_menu,
    selector_source_roots,
    sync_assignments,
    SyncAction,
    native_skill,
)
from .search import search_skills
from .state import claude_plugin_roots, inspect_skills


def default_roots(cwd: Path | None = None) -> list[Path]:
    """Return only the external skill vault roots."""

    configured = os.environ.get("SKILL_ROUTER_ROOT", "")
    if configured:
        return [Path(value).expanduser() for value in configured.split(os.pathsep) if value]
    base = cwd or Path.cwd()
    roots = [Path.home() / ".agents" / "skill-vault", base / ".agents" / "skill-vault"]
    ripwire = ripwire_source_root()
    if ripwire.is_dir():
        roots.insert(0, ripwire)
    return roots


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
    if args.root:
        roots = [Path(value).expanduser() for value in args.root]
    elif args.all:
        roots = default_source_roots()
    else:
        roots = selector_source_roots()
    skills = scan_roots(roots)
    saved = run_menu(skills, current, target=args.target, search=args.search)
    if saved is None:
        print("not saved")
        return 0
    try:
        actions = sync_assignments(saved, apply=True, prune=True)
        saved = config_after_sync(saved, actions)
        path = save_config(saved)
    except (ConfigError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"saved\t{path}")
    print(f"synced\t{len(actions)} actions")
    for action in actions:
        if action.action == "keep":
            continue
        detail = f"\t{action.detail}" if action.detail else ""
        print(f"{action.action}\t{action.target}\t{action.skill_id}\t{action.path}{detail}")
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
            "native_targets": sorted(effective_native_targets(assignment)),
        }
        for assignment in current.assignments
    ]
    if args.json:
        print(json.dumps(values, indent=2))
    else:
        for assignment in values:
            print(
                f"{assignment['id']}\trouter={','.join(assignment['targets']) or '-'}\t"
                f"native={','.join(assignment['native_targets']) or '-'}\t"
                f"{'enabled' if assignment['enabled'] else 'disabled'}\t{assignment['source']}"
            )
    return 0


def _print_status_rows(values: list[dict[str, object]]) -> int:
    skill_width = max([len("skill"), *(len(str(row["id"])) for row in values)], default=5)
    print(
        f"{'skill':<{skill_width}}  {'router claude':<13}  {'router codex':<12}  "
        f"{'codex exposure':<14}  claude exposure"
    )
    for row in values:
        router = row["router"]
        router_claude = "yes" if "claude" in router else "no"
        router_codex = "yes" if "codex" in router else "no"
        codex_native = str(row["codex"])
        claude_native = str(row["claude"])
        claude_mode = str(row.get("claude_mode", "-"))
        if claude_mode not in {"-", "on"} and claude_native != "-":
            claude_native = f"{claude_native}/{claude_mode}"
        print(
            f"{row['id']:<{skill_width}}  {router_claude:<13}  {router_codex:<12}  "
            f"{codex_native:<14}  {claude_native}"
        )
    return 0


def _status_command(args: argparse.Namespace) -> int:
    try:
        current = load_config()
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    values = _status_values(current, args)
    if args.json:
        print(json.dumps(values, indent=2))
        return 0
    return _print_status_rows(values)


def _status_values(current: RouterConfig, args: argparse.Namespace) -> list[dict[str, object]]:
    """Build status rows from configured and discovered skills."""

    roots = _status_roots(args)
    assignments = current.assignment_map()
    return [
        {
            "id": row.skill.skill_id,
            "source": row.source,
            "router": list(row.router_targets),
            "native_targets": sorted(effective_native_targets(assignments[row.skill.skill_id]))
            if row.skill.skill_id in assignments and assignments[row.skill.skill_id].enabled
            else [],
            "codex": row.codex_exposure,
            "claude": row.claude_exposure,
            "claude_mode": row.claude_mode,
            "claude_override": row.claude_override,
            "claude_lock": row.claude_lock,
            "path": str(row.skill.path),
        }
        for row in inspect_skills(scan_roots(roots), current)
    ]


def _status_roots(args: argparse.Namespace) -> list[Path]:
    """Return explicit status roots, or the managed roots plus plugins."""

    if args.root:
        return [Path(value).expanduser() for value in args.root]
    return default_source_roots() + claude_plugin_roots()


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


def _adopt_command(args: argparse.Namespace) -> int:
    if args.provider != "ripwire":
        print(f"unsupported provider: {args.provider}", file=sys.stderr)
        return 2
    source_root = ripwire_source_root()
    if not source_root.is_dir():
        print(f"Ripwire skill root not found: {source_root}", file=sys.stderr)
        return 2
    try:
        current = load_config()
        skills = scan_roots([source_root])
        plan = plan_ripwire_adoption(current, skills)
        planned = sync_assignments(plan.config, prune=True)
    except (ConfigError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print("provider\tripwire")
    print(f"skills\t{len(skills)}")
    print(f"shared-links\t{len(plan.shared_links)}")
    for path in plan.shared_links:
        print(f"unlink\tshared\t{path.name}\t{path}")
    for action in planned:
        if not action.skill_id.startswith("ripwire-"):
            continue
        detail = f"\t{action.detail}" if action.detail else ""
        print(f"{action.action}\t{action.target}\t{action.skill_id}\t{action.path}{detail}")
    if not args.apply:
        print("dry-run\tuse --apply to adopt")
        return 0
    try:
        saved, actions = apply_ripwire_adoption(plan)
        path = save_config(saved)
    except (ConfigError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"saved\t{path}")
    print(f"applied\t{len(actions)} sync actions")
    return 0


def _adopt_native_command(args: argparse.Namespace) -> int:
    if not args.skill_id:
        print("native adoption needs a skill ID", file=sys.stderr)
        return 2
    try:
        current = load_config()
        plan, planned = _native_adoption_plan(current, args.skill_id)
    except (ConfigError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    _print_native_adoption_plan(plan, planned)
    if not args.apply:
        print("dry-run\tuse --apply to adopt")
        return 0
    try:
        saved, actions = apply_native_adoption(plan)
        path = save_config(saved)
    except (ConfigError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"saved\t{path}")
    print(f"applied\t{len(actions)} sync actions")
    return 0


def _native_adoption_plan(
    current: RouterConfig,
    skill_id: str,
) -> tuple[NativeAdoptionPlan, list[SyncAction]]:
    """Build one native adoption plan and its sync preview."""

    skill = native_skill(skill_id, current)
    if skill is None:
        skill = next(
            (
                value
                for value in scan_roots(default_source_roots())
                if value.skill_id.casefold() == skill_id.casefold()
            ),
            None,
        )
    if skill is None:
        raise ValueError(f"skill not found: {skill_id}")
    plan = plan_native_adoption(current, skill)
    if (plan.router_dir / "SKILL.md").is_file():
        return plan, sync_assignments(plan.config, prune=True)
    targets = effective_native_targets(plan.config.assignment_map()[plan.skill_id])
    actions = [
        SyncAction(
            "link",
            target,
            plan.skill_id,
            plan.config.roots()[target] / plan.skill_id,
            plan.router_dir / "SKILL.md",
        )
        for target in sorted(targets)
    ]
    return plan, actions


def _print_native_adoption_plan(plan: NativeAdoptionPlan, actions: list[SyncAction]) -> None:
    """Print a native adoption plan."""

    print("provider\tnative")
    print(f"skill\t{plan.skill_id}")
    print(f"source\t{plan.source_dir}")
    print(f"router-source\t{plan.router_dir}")
    for path in plan.shared_paths:
        print(f"remove\tshared\t{path}")
    for action in actions:
        if action.skill_id != plan.skill_id:
            continue
        detail = f"\t{action.detail}" if action.detail else ""
        print(f"{action.action}\t{action.target}\t{action.skill_id}\t{action.path}{detail}")


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
    if args.command in {"manage", "select"}:
        return _manage_command(args)
    if args.command == "assignments":
        return _assignments_command(args)
    if args.command == "status":
        return _status_command(args)
    if args.command == "sync":
        return _sync_command(args)
    if args.command == "adopt":
        return _adopt_native_command(args) if args.provider == "native" else _adopt_command(args)
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

    manage = subparsers.add_parser(
        "manage",
        aliases=("select",),
        help="select skills and agent targets",
    )
    manage.add_argument("--root", action="append", help="skill root; repeatable")
    manage.add_argument("--target", choices=("codex", "claude"), default="codex")
    manage.add_argument("--search", default="", help="initial skill filter")
    manage.add_argument(
        "--all",
        action="store_true",
        help="include plugin, sync, native, and bundled sources",
    )

    assignments = subparsers.add_parser("assignments", help="show saved skill assignments")
    assignments.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="show router and native agent skill state")
    status.add_argument("--root", action="append", help="skill root; repeatable")
    status.add_argument("--json", action="store_true")

    sync = subparsers.add_parser("sync", help="plan or apply selected skill links")
    sync.add_argument("--apply", action="store_true", help="create safe links")
    sync.add_argument("--prune", action="store_true", help="remove only managed links")
    sync.add_argument("--json", action="store_true")

    adopt = subparsers.add_parser("adopt", help="take ownership of external skill links")
    adopt.add_argument("provider", choices=("ripwire", "native"))
    adopt.add_argument("skill_id", nargs="?", help="skill ID for native adoption")
    adopt.add_argument("--apply", action="store_true", help="apply the adoption plan")

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
