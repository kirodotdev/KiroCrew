# Skills

Skills are directories containing `SKILL.md` files. Global skills live in `~/.kiro/crew/skills/`.

For project skills and what kiro-cli shares, see [Where Skills, Steering and Agents Live](#where-skills-steering-and-agents-live).

## How Skills Work

- **Always-on skills**: `always: true` injects full content into every eligible session. A project's own always-on skills share a smaller project budget; one that does not fit is left out and listed in the session with a pointer to read it instead.
- **On-demand skills**: the session starts with a summary; the agent can load the full file when it applies.
- **Triggered skills**: when `skills.max_triggered` is positive, matching positive triggers inject the skill; the default is `0`, which disables per-turn trigger matching.

## Auto-generated skills

Open **Settings > Skills** and turn on **Auto-generate skills from sessions**.
It is off by default. Eligible session excerpts need at least
`skills.auto_min_tool_calls` tool calls (five by default), must pass the
sensitive-content checks, and must demonstrate a procedure a future session
could reuse on a different target. Incognito and Temporary chats do not produce
candidates. A long session alone does not qualify, and duplicate procedures do
not become new skills.

**Require approval before generated skills go live** is on by default. Review
pending candidates under **Customize > Skills** before approving them. Turning
that setting off lets new prose-only skills go live without review. Skills
containing scripts always require approval, and so does an update to a skill
that is already live: it waits in the same queue, marked **Update**.

The separate `skills.auto_refine_on_deviation` setting, off by default, works
differently: when it is on, a session that finds a better procedure for a
generated skill it used rewrites that skill in place, without review.

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
| `inclusion` | No | Not read on a skill: it is the [steering](steering-and-hooks.md) key. A skill that sets `inclusion: always` without `always: true` stays on demand, and the log names its file once. Add `always: true`; the two keys can sit side by side. |
| `triggers` | No | Comma-separated phrases. A positive phrase matches when at least 70% of its words appear in the user text. Case is ignored. Prefix a phrase with `=` to match it exactly as written, for a name that is also an ordinary word: `=Word` fires on "Word" but not on "word". Prefix with `!` for a negative trigger; every negative-trigger word must appear to exclude the skill. |
| `inject_on_trigger` | No | Defaults to `true`. For non-project skills, `false` contributes a one-line pointer instead of the full body. Trusted project skills always inject their body. |
| `repo_scope` | No | Restricts injection to a session whose active project or an ancestor contains the specified relative path. |
| `app` | No | The name of the app this skill belongs to, as its `app.json` spells it (`ops-mission-control`, not the display name). While that app is disabled the skill is withheld everywhere, as the app's own bundled skills are. Needed only by an app skill shipped outside its app's folder; ignored on a trusted project skill. |

`SKILL.md` must be UTF-8 text; a UTF-8 byte-order mark is accepted. A file in any
other encoding (for example UTF-16 from PowerShell) is skipped, with one log
warning naming the file. Re-save it as UTF-8 to load it.

## Creating Skills

### Via Dashboard

Customize → Skills → Create New Skill → enter name and content.

### Via Chat

Ask Kiro Crew: "Create a skill called X that does Y"

### Manually

Create `~/.kiro/crew/skills/my-skill/SKILL.md` with frontmatter and content.

## Built-in Skills

Kiro Crew ships with built-in skills that are synced from the packaged skills directory on startup. These cover common workflows like URL shortening, code search, and writing assistance.

## Telling Where a Skill Came From

A script that copies only your own skills out of `~/.kiro/crew/skills/` (to a backup or a dotfiles repository, say) can tell them apart from the ones Kiro Crew put there, with no gateway running. These markers are the supported way to do it.

| Marker | Where | Means |
|---|---|---|
| `.builtin-skill-provenance` | a file in the skill's folder | a built-in skill (a hidden `.<name>.superseded` copy carries it too) |
| `.kirocrew-managed` | a file in the skill's folder | a deploy skill Kiro Crew manages (`artifact-deploy`, `deploy-web`) |
| `.app-skill-links.json` | one file in `~/.kiro/crew/skills/` | the app that owns each app-skill link |
| `app:` | a line in the frontmatter of a skill that also carries `.builtin-skill-provenance` | that built-in skill belongs to the named app |

An app skill is a link (a junction on Windows) into the app's own folder, so it has no folder of its own to hold a marker. `.app-skill-links.json` lists each such link by its path under the skills folder, the `<app>/<skill>` link and the flat `<skill>` link alike, with the app that owns it:

```json
{"format": 1, "links": {"dev-fleet/pod-e2e": {"app": "dev-fleet"}, "pod-e2e": {"app": "dev-fleet"}}}
```

Kiro Crew rewrites it from the links on disk whenever it links or unlinks an app's skills, so a hand edit does not last.

## Skill Sources and Priority

1. `~/.kiro/crew/skills/` — global skills.
2. `skills.extra_paths` — read-only extra directories; a global skill wins on a duplicate name. Edit them in Settings → Skills → Extra skill paths: one path per line, and spaces around a path and blank lines are ignored (order and duplicates are kept). The list is saved when you leave the field and applies without a restart; a path that does not exist or is not allowed is skipped, not rejected. From a terminal, `kirocrew config set skills.extra_paths '["/opt/skills"]'` works too; if your shell mangles the quotes (Windows PowerShell does), pass the JSON as Base64 in one attached argument: `kirocrew config set skills.extra_paths --value-base64=WyIvb3B0L3NraWxscyJd`.
3. `<active-project>/.kiro/skills/` — loaded last, only when project skills are enabled and the user has granted that exact project directory trust.

The startup sync also copies `$KIROCREW_PROJECT_DIR/skills/` and packaged built-in skills into the global directory, preserving newer user files unless the source is newer.

### Project-skill trust

The dashboard can grant trust only to the requesting chat's active project. Before recording a grant, Kiro Crew canonicalizes the path, requires an existing readable directory, and verifies that the reviewed canonical key still matches; trusted project skills are then read from `<project>/.kiro/skills/`. Revoking the grant stops those project skills from loading.

## Where Skills, Steering and Agents Live

The default folders for each of these, in user and project scope. Project scope is the chat's project folder. For skills, the table describes the default agent's skill catalog, the one its `skill_search` reads; what a custom agent gets depends on its mapping and the harness ([Mapping Skills to an Agent](agents.md#mapping-skills-to-an-agent)). There is no separate rules folder: steering files are the rules, and they live under `~/.kiro/`, not `~/.kiro/crew/`.

| Asset | User scope | Project scope | Shared with kiro-cli? |
|---|---|---|---|
| Skills | `~/.kiro/crew/skills/` (moved by `KIROCREW_HOME`), plus any extra skill paths | `<project>/.kiro/skills/`, once you [trust that project](#project-skill-trust) | The project folder, yes. kiro-cli's `~/.kiro/skills/`, no: it is not in the catalog until you add it under **Settings → Skills → Extra skill paths**. |
| Steering | `~/.kiro/steering/**/*.md` | `<project>/.kiro/steering/**/*.md` | Yes, the same folders ([Where they live](steering-and-hooks.md#where-they-live)) |
| `AGENTS.md` | none | `<project>/AGENTS.md` | Yes, the same file |
| Agents | `~/.kiro/agents/` | `<project>/.kiro/agents/` | Yes, the same folders kiro-cli looks up `--agent` in ([Custom agents](agents.md#custom-agents)) |

MCP servers and your kiro-cli login carry over as [What carries over](coming-from-kiro-cli.md#what-carries-over) describes. This table covers what Kiro Crew and kiro-cli read, not Kiro IDE.

## Importing Skills from a GitHub Repository

Customize → Skills → Add Skill searches a public registry and, when what you
type looks like a repository address, your own GitHub repositories:

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

- `skill_search(query?, action?, key?, limit?, offset?)` works on installed skills. The default `action=search` matches every query word against names, descriptions and bodies together and ranks the results; `action=list` browses the whole scope in pages with `offset`; `action=read` loads one exact `key` from a result. For search and list, `limit` defaults to 20 and caps at 50. A body too large for one answer (about 99,000 bytes) is read in pages: `offset` is the first line and `limit` the line count, and each page names the next offset. While the body index is still being built a search can say it is incomplete; repeat it to continue.
- `skill_discover(query, provider?, limit?)` searches the public registry (including skills.sh) and resolves a `owner/repo[@ref][:path]` query against GitHub; it does not install anything. It defaults to 10 results and caps `limit` at 50. When every provider failed it returns an Error, not "no matches"; when only some failed it returns its results under a Warning.
- `skill_fetch(id, provider?)` reads one discovered registry skill without installing it. It returns the main instruction file only; bundled sibling files are not available until installation.
