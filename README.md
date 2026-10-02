# skill-router

`skill-router` keeps task skills outside the agent context until a local search selects one.

Search and recommend commands scan only explicit vault roots.
The separate manager can inspect native Codex and Claude skill roots when you run it.
This keeps normal routing scoped to a small catalog.

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

## Manage skill assignments

Use the manager to review skills from these roots:

```text
~/.agents/skills
~/.codex/skills
~/.claude/skills
```

Open the menu with:

```sh
skill-router manage
```

The menu shows one skill per row.
In a terminal, use Up and Down to move.
Press Space to select, Enter to save, and `q` to quit without saving.
Press `t` to switch targets, `a` to select all shown rows, and `n` to clear them.
Press `/` to filter the list.
When input is not interactive, the menu accepts number and text commands.

Use `--root` to choose other source roots.
Use `--target` to choose the first target.
Use `--search` to set the first filter.

View saved assignments with:

```sh
skill-router assignments
skill-router assignments --json
```

Compare each skill in one view:

```sh
skill-router status
skill-router status --json
```

`router` lists saved targets. The native columns show direct exposure.
The JSON view also includes Claude modes, locks, sources, and paths.

Assignments persist in `~/.config/skill-router/config.toml`.
The manager does not change native skill directories when it saves.

Set target roots when you need a non-default location:

```sh
skill-router config target show
skill-router config target set codex ~/.codex/skills
skill-router config target set claude ~/.claude/skills
```

## Sync selected skills

Review a sync plan before you apply it:

```sh
skill-router sync
```

Create missing symlinks with an explicit apply flag:

```sh
skill-router sync --apply
```

The sync command never replaces an existing file, directory, or different symlink.
Use `--prune --apply` to remove only symlinks that this tool recorded and that still point to their source.
The default mode makes no filesystem changes.

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
