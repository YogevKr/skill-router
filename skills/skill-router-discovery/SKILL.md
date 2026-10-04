---
name: skill-router-discovery
description: This skill should be used before any task that may need a specialised skill, tool, or workflow. Always run skill-router recommend first, even when the user does not mention skills.
---

# Skill router discovery

Use this skill before work when a request may need a domain skill, tool skill, or
other specialised workflow. Route first. Do not guess from skill names.

## Required route step

Run one metadata search at the start of the task:

```sh
skill-router recommend "describe the user's task" --provider auto --json \
  --root ~/.local/share/skill-router/skills
```

Use the user's words in the query. Keep the query short and specific. The
router returns metadata only. It does not load instructions or grant access.

Read the result before doing specialised work:

- `route`: use the returned `skill` ID and load that skill.
- `no_tool`: continue without a skill.
- `fallback`: inspect the candidates, then choose one or continue without one.

Do not select `skill-router-discovery` as the task skill. It only teaches this
routing step. Search again with the user's actual task.

Do not load more than one skill unless the selected skill tells you to do so.
Do not treat a recommendation as permission to run commands.

Use `--provider local` for a local-only result. Use `--provider jev` only when
the user enabled Jev and the key exists. The router sends Jev metadata only.

If the installed command supports `--target`, pass the current agent target:
`codex` for Codex or `claude` for Claude. Omit it on older releases.

## Load the selected skill

Load the full instructions only after you select the skill:

```sh
skill-router load SKILL_ID --root ~/.local/share/skill-router/skills
```

Follow the loaded instructions for the task.

## Use Codex's selector

When the user works in the Codex terminal:

- Use `/skills` to search and load a skill.
- Use `$` to search the combined apps and skills list.
- Choose `Enter` for `Insert reference`.
- Choose `Ctrl+Enter` for `Use now`.
- Use `/skills QUERY` to open a filtered result.

The selector controls the current Codex session.
The router controls persistent exposure for Codex and Claude.
