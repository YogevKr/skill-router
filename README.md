# skill-router

`skill-router` keeps task skills outside the agent context until a local search selects one.

The router does not scan native Codex or Claude skill directories. It scans only explicit vault roots.
This prevents a full skill catalog from entering every session.

## Design

The router uses local BM25 search over skill metadata.
It reads `SKILL.md` files from configured roots and returns a small ranked list.
The `load` command returns the full body after explicit selection.

An optional Jev provider can rerank a local shortlist.
Jev receives the request and candidate metadata.
It never receives skill bodies during routing.

```text
task -> local metadata search -> optional Jev choice -> selected skill ID -> load SKILL.md
```

Local routing has no network dependency.
Jev routing uses the TypeSafe API only when you select the Jev provider.

## Quick start

Run from this checkout:

```sh
PYTHONPATH=src python -m skill_router.cli search "debug a Python traceback" \
  --root ~/.agents/skill-vault \
  --root .agents/skill-vault
```

Load one selected skill:

```sh
PYTHONPATH=src python -m skill_router.cli load python-debug \
  --root ~/.agents/skill-vault
```

Set `SKILL_ROUTER_ROOT` to a colon-separated list of roots for repeated use.
The default roots are `~/.agents/skill-vault` and `.agents/skill-vault`.

## Persistent provider setting

The default provider is local BM25.
The setting file is `~/.config/skill-router/config.toml`.
Jev stays disabled until you enable it:

```sh
skill-router config set jev enabled
skill-router config show
```

Disable Jev again with:

```sh
skill-router config set jev disabled
```

`recommend` uses this saved setting when `--provider auto` is active.
Use `--provider local` or `--provider jev` to override it for one command.
Set `SKILL_ROUTER_CONFIG` to use another configuration path.

## Jev recommendations

Install the optional provider:

```sh
python -m pip install 'skill-router[jev]'
```

Set `TYPESAFE_API_KEY` in the command environment.
The SDK reads this variable and the router does not accept keys as arguments.

Ask the local provider to select one skill:

```sh
skill-router recommend "debug a Python traceback" --provider local
```

Ask Jev to choose from the local BM25 shortlist:

```sh
skill-router recommend "debug a Python traceback" --provider jev --json
```

The result has one of these statuses: `route`, `no_tool`, or `fallback`.
Missing keys, missing SDKs, timeouts, and invalid responses use local fallback.
Jev never loads a skill or grants tool access.

## Skill format

The router accepts normal `SKILL.md` files. It reads `name` and `description` from optional YAML front matter.
The parent directory becomes the skill ID when `name` is absent.

```markdown
---
name: python-debug
description: Debug Python tracebacks and failing tests.
---

# Python debugging

...
```

## Safety boundary

Roots are explicit. Symlinked files and directories are skipped.
The router returns guidance only. It does not execute skill scripts or grant tool access.

## Status

This repository contains the local routing core.
Codex hook and MCP adapters will use this core after routing behavior is measured.
