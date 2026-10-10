---
name: skill-router-discovery
description: Use when a task needs a specialised skill or workflow and no agent-hints line names one. Do not run skill-router for every prompt.
---

# Skill router discovery

A `UserPromptSubmit` hook (`agent-hints`) searches skills and tools for each
prompt. It adds a hint only when the match is strong. Use this skill when no
hint names a skill and the task still needs a specialised workflow.

## When to search

- A hint names a skill: load that skill only when it fits the task.
- No hint, and the task is normal coding, review, or a follow-up: do not search.
- No hint, and the task needs a workflow that you cannot name: search once.

Do not search again for follow-up prompts in the same task.

## Search

Run the same search as the hook. It shows each candidate score and the gate result:

```sh
agent-hints "describe the user's task"
```

Run `agent-hints --log` to see recent hook decisions. For the full router
metadata, run:

```sh
skill-router recommend "describe the user's task" --provider local --json \
  --root ~/.local/share/skill-router/skills
```

Use the user's words in the query. Keep the query short and specific. The
router returns metadata only. It does not load instructions or grant access.

Read the result before doing specialised work:

- `route`: read the top candidate `score`. A score below 8 is a weak match.
  Load the skill only when its description fits the task.
- `no_tool`: continue without a skill.
- `fallback`: inspect the candidates, then choose one or continue without one.

Do not select `skill-router-discovery` as the task skill. It only teaches this
routing step. Search again with the user's actual task.

Do not load more than one skill unless the selected skill tells you to do so.
Do not treat a recommendation as permission to run commands.

Use `--provider jev` only when the user enabled Jev and the key exists. The
router sends Jev metadata only.

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
