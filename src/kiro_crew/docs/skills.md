# Skills

Skills are markdown files that give Kiro Crew specialized knowledge for specific
workflows. They live in `~/.kiro/crew/skills/` as `SKILL.md` files.

## How Skills Work

- **Always-on skills**: full content injected into every session (use sparingly)
- **On-demand skills**: summary loaded at session start; full content read when
  the topic comes up
- **Triggered skills**: automatically loaded when the user's message matches
  trigger words (≥70% word overlap)

## Skill Structure

```
~/.kiro/crew/skills/
├── my-skill/
│   └── SKILL.md
├── utils/
│   └── url-shortener/
│       ├── SKILL.md
│       └── shorten.sh    # auxiliary scripts
└── code/
    └── git-workflow/
        └── SKILL.md
```

Each skill is a directory containing at least `SKILL.md`. Nested directories
are supported.

## SKILL.md Format

```markdown
---
name: my-skill
description: What this skill does (shown in summaries)
always: false
triggers: keyword1, keyword2, multi word trigger
---

# Skill Content

Instructions, examples, and reference material that the agent reads
when this skill is activated.
```

### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Display name |
| `description` | Yes | One-line summary (shown in skill listings) |
| `always` | No | `true` to inject full content every session |
| `triggers` | No | Comma-separated trigger phrases. A match injects this skill's full content into the turn. Prefix with `!` for negative triggers (e.g. `!test` excludes when "test" appears) |
| `inject_on_trigger` | No | Defaults to `true`. Set to `false` when this skill is an *offer* rather than a mandate: a match then contributes only a one-line pointer naming the skill and its path, and the agent reads the file if it applies. Saves the skill's full size on every match, at the cost of the read being the agent's choice — so leave it at the default for a procedure that must be followed whenever its topic comes up |

## Creating Skills

### Via Dashboard

Overview → Skills tab → "+ New" button → enter name and content.

### Via Chat

Ask Kiro Crew: "Create a skill called X that does Y"

### Manually

Create `~/.kiro/crew/skills/my-skill/SKILL.md` with frontmatter and content.

## Built-in Skills

Kiro Crew ships with built-in skills that are synced from the project's
`skills/` directory on startup. These cover common workflows like URL
shortening, code search, and writing assistance.

## Skill Sources (Priority Order)

1. `$KIROCREW_PROJECT_DIR/skills/` — project-level (edit without rebuilding)
2. Built-in skills bundled in the Python package

Both are synced into `~/.kiro/crew/skills/` on startup. Newer source files
overwrite older ones (mtime-based). User-created skills in
`~/.kiro/crew/skills/` persist as long as they don't share a name with a
project-level or built-in skill — if they do, the source version wins when
it's newer.
