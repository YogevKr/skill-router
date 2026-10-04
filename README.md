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

Open the selector with:

```sh
skill-router manage
# `skill-router select` and `skill-router skills` are aliases.
```

Open filtered results directly:

```sh
skill-router select spreadsheet
```

The selector shows one skill per row.
By default, it shows router and provider sources.
Use `--all` to include plugin, sync, native, and bundled sources.
In a terminal, use Up and Down to move between skills.
Use Left and Right to move between the four columns.
The active header starts with `>` and the active cell uses reverse color.
Press Space to change the active cell.
Press Enter to review the selected skill, `s` to save and sync, and `q` to quit.
Review mode supports Up, Down, Page Up, Page Down, Home, and End.
Press `a` to select all shown rows in the active column.
Press `n` to clear all shown rows in the active column.
Press `/` to filter the list.
When input is not interactive, use `m` to switch router or native layers.
Use `t codex` or `t claude` to choose the target.
The line menu also accepts number and text commands.

The four columns store separate selections:

| Column | Meaning |
| --- | --- |
| `router claude` | The router can assign this skill to Claude. |
| `router codex` | The router can assign this skill to Codex. |
| `codex native` | Sync may expose this skill in Codex's native skill root. |
| `claude native` | Sync may expose this skill in Claude's native skill root. |

The active column marks the cell that Space changes.
For example, `> NATIVE CODEX` means Space changes `codex native`.

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

`router claude` and `router codex` show saved router assignments.
The native columns show direct exposure from filesystem links or native folders.
The JSON view also includes Claude modes, locks, sources, and paths.

Assignments persist in `~/.config/skill-router/config.toml`.
Enter or `s` saves assignments and runs a safe sync.
The sync creates missing links and prunes only managed links.
The manager never removes direct native or plugin folders.

## Skill ownership

Router-owned personal skills live in `~/.local/share/skill-router/skills`.
The selector manages links from that store into the Codex and Claude skill roots.

Codex bundled skills stay under `~/.codex/skills/.system`.
Claude plugin skills stay in their plugin cache and require plugin commands for enablement.
Claude.ai synced skills stay in their synced directory and require `skillOverrides` for runtime control.

Use `skill-router status --json` to inspect source ownership, filesystem exposure, and Claude mode.
The `status` table reports `managed`, `native`, or `-` for each agent exposure.

Run the drift check:

```sh
skill-router doctor
skill-router doctor --json
```

The doctor reports broken links, duplicate names, unmanaged native skills, plugin ownership,
and disabled Claude.ai sync skills. It exits with code `1` for actionable drift.

## Codex selector note

Codex owns its `/skills` and `$` menus.
Use `/skills` for skills. The `$` menu lists apps and does not search skill names.
The router cannot rename or merge those Codex menus.

Set target roots when you need a non-default location:

```sh
skill-router config target show
skill-router config target set codex ~/.codex/skills
skill-router config target set claude ~/.claude/skills
```

## Sync selected skills

Review a sync plan without changing files:

```sh
skill-router sync
```

Apply selected links outside the manager:

```sh
skill-router sync --apply
```

The sync command never replaces an existing file, directory, or different symlink.
Use `--prune --apply` to remove only symlinks that this tool recorded and that still point to their source.
It never removes direct native folders or their source files.
The default mode makes no filesystem changes.

## Adopt Ripwire skills

Ripwire installs its skills in `~/.local/share/ripwire/skills` and exposes them with symlinks in
`~/.agents/skills`. Adopt those links so the router controls exposure in Codex and Claude:

```sh
skill-router adopt ripwire
skill-router adopt ripwire --apply
```

The first command prints the plan. The second removes only Ripwire symlinks from `~/.agents/skills`,
creates router-managed links in the configured agent roots, and records the assignments. It never removes
Ripwire source files. Run `skill-router manage` after adoption to change the four exposure columns.
If Ripwire runs `skills/install.sh --codex` again, run the adoption command again.

## Adopt a native skill

Move one direct skill into router-owned storage and preserve its native exposure:

```sh
skill-router adopt native local-tools
skill-router adopt native local-tools --apply
```

The command copies the skill to `~/.local/share/skill-router/skills`, keeps backups of existing
native directories, removes the shared native copy, and creates managed links for its configured targets.
It also accepts a direct native symlink, such as a project-owned Claude skill link.
Use this command for a skill that has no plugin owner. Plugin cache sources remain external.
The command refuses different copies of the same skill.

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
