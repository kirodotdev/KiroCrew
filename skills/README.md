# Skills

Skills are markdown files that teach the LLM agent how to use KiroCrew capabilities.

## Directory Layout

```
skills/
├── learn/SKILL.md
├── subagent/SKILL.md
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
| `name` | yes | Skill name (defaults to directory name if omitted) |
| `description` | yes | One-line summary — the LLM sees this to decide relevance |
| `always` | no | `true` to inject into every session (default: `false`) |
| `triggers` | no | Comma-separated keywords that auto-inject the skill. Prefix with `!` for negative triggers (e.g. `!test` excludes when "test" appears) |
| `repo_scope` | no | Relative path (e.g. `src/kiro_crew`) that must exist in the CWD or an ancestor for the skill to be eligible. Mechanically suppresses repo-specific skills outside their repo — use for skills whose instructions would be wrong or destructive elsewhere. Fails closed. |

### Loading Behavior

- `always: true` — full content injected at session start
- `triggers` match — full content injected when keywords appear in user message
- No match — summary shown; LLM can `cat` the file on demand

## No Rebuild Required

Skills in this directory are read at runtime. Edit, add, or remove skills
without rebuilding the package — changes take effect on the next session.
