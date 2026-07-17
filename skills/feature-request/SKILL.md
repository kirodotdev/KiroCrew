---
name: feature-request
description: Conversational workflow for gathering user feedback and filing GitHub Issues on the KiroCrew repository. Load when the user clicks "Request a Feature", wants to report a bug, or suggest an improvement.
triggers: request a feature, request feature, feature request, report a bug, bug report, file an issue, github issue, I have an idea, something's broken, suggestion
---

# Feature Request / Issue Report

Conversational workflow for gathering user feedback and creating GitHub Issues
on the KiroCrew repository.

**Trigger:** User clicks "Request a Feature" button, or says "report a bug",
"feature request", "I have an idea", "something's broken".

## Repository

```
https://github.com/kirodotdev/KiroCrew
```

## Shell safety (READ FIRST)

Everything the user types is **untrusted**. Never interpolate raw user text
(titles, descriptions, search keywords) into a shell command string, and never
put it in a shell **heredoc** — a heredoc ends on a line equal to its delimiter,
so a body containing a line that is exactly `EOF` (or whatever delimiter you
pick) would terminate it early and let the following lines execute as shell.

Follow these rules for every `gh` invocation below:

- **Body & title:** write them to temp files using *your own file-writing tool*
  (not a shell heredoc, not `echo`/`cat >`), then feed those files to `gh`.
  Create the files with `mktemp` so the path is unpredictable and
  per-invocation (no fixed `/tmp/...` name to clobber or symlink-attack).
- Pass the body with `--body-file "$BODY_FILE"` (never `--body "..."`).
- Load the title via command substitution into a double-quoted variable —
  `TITLE="$(cat "$TITLE_FILE")"` — then pass `--title "$TITLE"`. Command
  substitution assigns the text literally (it is not re-parsed as shell) and
  the double quotes contain word-splitting/globbing.
- **Search keywords:** derive a few plain alphanumeric words yourself from the
  conversation and pass them as a double-quoted literal. Do not paste raw user
  text (with its punctuation/metacharacters) into the search string.
- If you cannot safely pass a value, fall back to the copy/paste option
  (Option 2) instead of shelling out.

## Workflow

### 1. Greet & Identify

Ask the user what they'd like — a feature request or a bug report. Keep it
casual. Don't present a form.

### 2. Gather Details Conversationally

Guide the user to describe:
- **What** they want (or what's broken)
- **Why** it matters (what problem it solves)
- **Any context** (how they hit it, what they tried)

Don't force structure. Ask follow-up questions if the description is vague.
Two to three exchanges is usually enough.

### 3. Check for Duplicates

Search existing issues to avoid duplicates. Derive plain keywords yourself (a
few alphanumeric words) — do not paste raw user text:

```bash
gh issue list --repo kirodotdev/KiroCrew \
  --search "your derived keywords" --state open --limit 10
```

If you find related issues, show them to the user and ask if any cover their
need. They may want to comment on an existing issue instead.

### 4. Draft the Issue

Compose a clean title and markdown body from the conversation. Structure:

```markdown
## What

[One paragraph describing the feature/bug]

## Why

[Why this matters / what problem it solves]

## Additional Context

[Any extra details, reproduction steps, environment info]
```

Show the draft to the user for confirmation before submitting.

### 5. Submit — Offer Three Options

Present all three and let the user choose:

**Option 1: Pre-filled URL** (if body ≤ 2000 chars)

Build a GitHub new-issue URL with query params:

```
https://github.com/kirodotdev/KiroCrew/issues/new?title=URL_ENCODED_TITLE&body=URL_ENCODED_BODY&labels=enhancement
```

Use `enhancement` label for features, `bug` label for bugs.

Note: URL-encode the title and body. If the total URL exceeds ~4000 chars,
warn the user it may be truncated and recommend Option 2.

**Option 2: Copy/paste**

Show the formatted title and body in a code block the user can copy into
the GitHub new issue form at:
`https://github.com/kirodotdev/KiroCrew/issues/new`

**Option 3: Direct creation via `gh` CLI**

If the user prefers, create it directly. Do **not** hand-write the title/body
into the shell — use your file-writing tool to drop them into `mktemp` files,
then reference those files (see **Shell safety** above):

1. `BODY_FILE=$(mktemp -t kc-issue-body.XXXXXX.md)` — then write the confirmed
   markdown body into it with your file-writing tool.
2. `TITLE_FILE=$(mktemp -t kc-issue-title.XXXXXX.txt)` — then write the
   confirmed title into it with your file-writing tool.
3. Create the issue, loading both from files so no untrusted text is parsed by
   the shell:

```bash
TITLE="$(cat "$TITLE_FILE")"
gh issue create --repo kirodotdev/KiroCrew \
  --title "$TITLE" \
  --body-file "$BODY_FILE" \
  --label enhancement
```

This requires `gh auth` on the user's machine. If it fails with auth errors,
fall back to Option 2.

## Labels

- `enhancement` — feature requests
- `bug` — bug reports

## Guidelines

- Keep the conversation light — this isn't a support ticket form
- Two to three exchanges max before drafting
- Always show the draft before submitting
- If the user just wants to vent without filing, that's fine too — acknowledge
  and offer to file if they want
