# Skills

Skills are markdown files that teach the LLM agent how to use Kiro Crew capabilities.

## This directory is repo-checkout-only

Skills here are synced into `~/.kiro/crew/skills` only when `KIROCREW_PROJECT_DIR`
points at this checkout, and they are not part of the wheel. **A skill that any
shipped feature, prompt, or doc references MUST live in
`src/kiro_crew/builtin_skills/` instead** — that tree is packaged and copied into every
install's skills directory on gateway start. Use this directory for skills whose
audience is someone working in this checkout.

## Directory Layout

```
skills/
├── grill/SKILL.md
├── self-update/SKILL.md
└── my-custom/SKILL.md      ← add your own here
```

Each skill is a directory containing a `SKILL.md` file.

## Adding a New Skill

1. Create a directory: `skills/<name>/`
2. Add a `SKILL.md` file with YAML frontmatter and instructions

### SKILL.md Format

```markdown
---
name: my-skill
description: One-line summary shown in skill listings
always: false
triggers: keyword1, keyword2, keyword3
---
# My Skill

Instructions for the LLM agent...
```

### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Repository convention for the display name; the runtime falls back to the relative directory key if omitted |
| `description` | yes | One-line summary — the LLM sees this to decide relevance |
| `always` | no | `true` to inject into every session (default: `false`) |
| `triggers` | no | Comma-separated phrases used by opt-in trigger matching (`skills.max_triggered > 0`). Prefix with `!` for negative triggers (e.g. `!test` excludes when "test" appears) |
| `repo_scope` | no | Relative path (e.g. `src/kiro_crew`) that must exist in the session's active project directory, or an ancestor of it, for the skill to be eligible. Mechanically suppresses repo-specific skills outside their repo — use for skills whose instructions would be wrong or destructive elsewhere. Fails closed: a session that names no project, and one whose project cannot be resolved, get neither the skill's body nor its index entry. The process working directory is never consulted. |
| `inject_on_trigger` | no | `false` keeps a trigger match from injecting the full body — the skill stays index-only and is read on demand. Use for a skill whose body is too large to spend context on speculatively. |

Provenance keys (`source`, `session_key`, `created_at`, `refined_at`,
`reuse_count`, `pinned`, `version`) are written by the skill installer and editor;
do not hand-edit them. A staged candidate's `kind` and `base_version` live in its
`.meta.json` sidecar rather than in frontmatter. The reader keeps any other key it
finds and no consumer looks at it, so an unknown field is ignored rather than
rejected — a typo in a field name fails silently, so check the spelling against this
table.

### Loading Behavior

- `always: true` — full content is injected at session start.
- `$skillname` or an exact `skill_search` read — full content is loaded on demand.
- `triggers` — full content is eligible for automatic injection only when
  `skills.max_triggered` is greater than `0` (the default is `0`); a skill with
  `inject_on_trigger: false` remains pointer-only.
- A bounded subset of eligible skills appears in the Available Skills index; all
  mapped eligible skills remain discoverable with `skill_search`.

See [Memory, skills, and hooks](../docs/system-specs/modules/memory-skills-hooks.md#skills-skillspy)
for the canonical discovery, precedence, trust, and loading contract.

## No Rebuild Required

Skills in this directory are read at runtime. Edit, add, or remove skills
without rebuilding the package — changes take effect on the next session.
