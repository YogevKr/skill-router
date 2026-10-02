"""Command line interface for local skill search and loading."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from .catalog import load_skill, scan_roots
from .jev import JevProvider, Recommendation, recommend_local
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
    if args.provider == "jev":
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
    recommend.add_argument("--provider", choices=("local", "jev"), default="local")
    recommend.add_argument("--root", action="append", help="skill vault root; repeatable")
    recommend.add_argument("--limit", type=int, default=3)
    recommend.add_argument("--shortlist-limit", type=int, default=8)
    recommend.add_argument("--model", default="jev-latest")
    recommend.add_argument("--timeout", type=float, default=5.0)
    recommend.add_argument("--confidence-threshold", type=float, default=0.30)
    recommend.add_argument("--fit-threshold", type=float, default=0.30)
    recommend.add_argument("--json", action="store_true")

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

    if args.command == "recommend":
        return _recommend(args, roots)

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
