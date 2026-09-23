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

## Importing Skills from a GitHub Repository

Settings → Skills → Discover searches a public registry and, when what you type
looks like a repository address, your own GitHub repositories:

```
acme/widgets                           every skill in the repo, default branch
acme/widgets@v2                        at the tag, branch or commit v2
acme/widgets:skills/reviewer           one skill directory
acme/widgets@v2:skills/reviewer        both
https://github.com/acme/widgets/tree/main/skills/reviewer   a pasted tree URL
```

A skill is any directory holding a `SKILL.md`, so one repository can carry many;
each is listed separately and imported on its own. A pasted tree URL takes its
first path segment after `tree` as the ref, so a branch name containing `/` needs
the `@ref` form.

**Importing is a copy, not a subscription.** Installing writes the files into
`~/.kiro/crew/skills/github/<name>-<id>/` -- the skill's own directory name plus a
short id derived from the repository and path, so two repositories can both give you
a skill called `reviewer` without one replacing the other. Re-importing the same
skill lands on the same directory and asks you to confirm the update. You own the
result from then on. It is
pinned to the commit it came from, recorded in `.skill-import-source.json` beside
the skill, and nothing ever looks upstream again — so a repository that changes
cannot change a skill you already imported. Re-import the same address to pick up
newer content. There is no branch tracking or automatic refresh.

An import is all-or-nothing, and the accepted file names are narrow on purpose. A
name must use only ASCII letters, digits, `.`, `_` and `-`, start with a letter or
digit, stay under 64 characters, and sit at most four folders deep. If any file is
outside that, cannot be read, is not text, is too large, or would collide with
another on a filesystem that ignores case, the whole import is refused and the
message names the file — rather than landing a skill that is missing part of itself.

So a repository with `rules/my file.md` will not import. That is the trade: you get a
clear refusal you can fix instead of a skill that looks installed and quietly lacks
one of its own files. `.gitignore`, `.gitattributes` and `.gitmodules` are skipped
silently, because they are never skill content.

Installing is always a person's action from the dashboard; the agent can read a
repository skill with `skill_fetch` but cannot install one. Requests are
unauthenticated, which GitHub rate-limits to 60 per hour per IP — enough for
ordinary importing, but not for repeated searching. Private repositories are not
supported for the same reason.

## Skill Discovery Tools

- `skill_search(query, limit?)` searches installed skills by key, name, and description, then searches bodies only if metadata has no matches. It defaults to 20 results and caps `limit` at 50.
- `skill_discover(query, provider?, limit?)` searches the public registry (including skills.sh) and resolves a `owner/repo[@ref][:path]` query against GitHub; it does not install anything. It defaults to 10 results and caps `limit` at 50.
- `skill_fetch(id, provider?)` reads one discovered registry skill without installing it. It returns the main instruction file only; bundled sibling files are not available until installation.
