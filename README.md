# skill-router

`skill-router` keeps task skills outside the agent context until a local search selects one.

The router does not scan native Codex or Claude skill directories. It scans only explicit vault roots.
This prevents a full skill catalog from entering every session.

## Design

The first version uses a local BM25 search over skill metadata.
It reads `SKILL.md` files from configured roots and returns a small ranked list.
The `load` command returns the full body after explicit selection.

```text
task -> local metadata search -> selected skill ID -> load SKILL.md
```

The router has no network dependency and sends no task or skill data to a remote model.

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

