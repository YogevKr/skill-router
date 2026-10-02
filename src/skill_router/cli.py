"""Command line interface for local skill search and loading."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .catalog import load_skill, scan_roots
from .search import search_skills


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search and load skills from explicit local vaults.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="rank skill metadata")
    search.add_argument("query")
    search.add_argument("--root", action="append", help="skill vault root; repeatable")
    search.add_argument("--limit", type=int, default=3)
    search.add_argument("--json", action="store_true")

    load = subparsers.add_parser("load", help="load one skill body")
    load.add_argument("skill_id")
    load.add_argument("--root", action="append", help="skill vault root; repeatable")
    load.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = _roots(args.root)
    if args.command == "search":
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


if __name__ == "__main__":
    raise SystemExit(main())

