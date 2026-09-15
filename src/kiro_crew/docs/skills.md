# Skills

Skills are directories containing `SKILL.md` files. Global skills live in `~/.kiro/crew/skills/`.

## How Skills Work

- **Always-on skills**: `always: true` injects full content into every eligible session.
- **On-demand skills**: the session starts with a summary; the agent can load the full file when it applies.
- **Triggered skills**: when `skills.max_triggered` is positive, matching positive triggers inject the skill; the default is `0`, which disables per-turn trigger matching.

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

Each skill is a directory containing at least `SKILL.md`. Nested directories are supported.

## SKILL.md Format

```markdown
---
name: my-skill
description: What this skill does (shown in summaries)
always: false
triggers: keyword1, keyword2, multi word trigger
---

# Skill Content

Instructions, examples, and reference material that the agent reads when this skill is activated.
```

### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | No | Display name; the loader uses the directory-relative path when it is absent. |
| `description` | No | Summary used in skill listings; the loader falls back to the directory-relative path. |
| `always` | No | `true` to inject full content every eligible session. |
| `triggers` | No | Comma-separated phrases. A positive phrase matches when at least 70% of its words appear in the user text. Prefix with `!` for a negative trigger; every negative-trigger word must appear to exclude the skill. |
| `inject_on_trigger` | No | Defaults to `true`. For non-project skills, `false` contributes a one-line pointer instead of the full body. Trusted project skills always inject their body. |
| `repo_scope` | No | Restricts injection to a session whose active project or an ancestor contains the specified relative path. |

## Creating Skills

### Via Dashboard

Overview → Skills tab → "+ New" button → enter name and content.

### Via Chat

Ask Kiro Crew: "Create a skill called X that does Y"

### Manually

Create `~/.kiro/crew/skills/my-skill/SKILL.md` with frontmatter and content.

## Built-in Skills

Kiro Crew ships with built-in skills that are synced from the packaged skills directory on startup. These cover common workflows like URL shortening, code search, and writing assistance.

## Skill Sources and Priority

1. `~/.kiro/crew/skills/` — global skills.
2. `skills.extra_paths` — read-only extra directories; a global skill wins on a duplicate name.
3. `<active-project>/.kiro/skills/` — loaded last, only when project skills are enabled and the user has granted that exact project directory trust.

The startup sync also copies `$KIROCREW_PROJECT_DIR/skills/` and packaged built-in skills into the global directory, preserving newer user files unless the source is newer.

### Project-skill trust

The dashboard can grant trust only to the requesting chat's active project. Before recording a grant, Kiro Crew canonicalizes the path, requires an existing readable directory, and verifies that the reviewed canonical key still matches; trusted project skills are then read from `<project>/.kiro/skills/`. Revoking the grant stops those project skills from loading.

## Skill Discovery Tools

- `skill_search(query, limit?)` searches installed skills by key, name, and description, then searches bodies only if metadata has no matches. It defaults to 20 results and caps `limit` at 50.
- `skill_discover(query, provider?, limit?)` searches the public registry (including skills.sh); it does not install anything. It defaults to 10 results and caps `limit` at 50.
- `skill_fetch(id, provider?)` reads one discovered registry skill without installing it. It returns the main instruction file only; bundled sibling files are not available until installation.
