---
name: feature-request
description: Conversational workflow for gathering user feedback and filing GitHub Issues on the Kiro Crew repository. Load when the user clicks "Request a Feature", wants to report a bug, or suggest an improvement.
triggers: request a feature, request feature, feature request, report a bug, bug report, file an issue, github issue, I have an idea, something's broken, suggestion
---

# Feature Request / Issue Report

Conversational workflow for gathering user feedback and creating GitHub Issues
on the Kiro Crew repository.

**Trigger:** User says "report a bug", "feature request", "I have an idea",
"something's broken", or names `$feature-request`. The dashboard's "Request a
Feature" button does NOT load this skill: it seeds the self-contained prompt
`FEATURE_REQUEST_PROMPT_FALLBACK` in `website/src/prompts/featureRequest.ts`,
which must be kept in step with the workflow below.

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
  (Option 1) instead of shelling out.

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

Every `gh` call below targets Kiro Crew's own public repository — a fixed target on
every install, not something resolved from whatever project the user is in. Set it
once; `gh` accepts a full URL wherever it accepts `OWNER/REPO`:

```bash
REPO=https://github.com/kirodotdev/KiroCrew
```

Search existing issues to avoid duplicates. Derive plain keywords yourself (a
few alphanumeric words) — do not paste raw user text:

```bash
gh issue list --repo "$REPO" \
  --search "your derived keywords" --state open --limit 10
```

If you find related issues, show them to the user and ask if any cover their
need. They may want to comment on an existing issue instead.

**This search is also your up-front capability check.** It is the first `gh`
call in the flow, so its outcome tells you — *before* you draft anything —
whether direct submission (Option 3) will work on this host:

- If it succeeds, `gh` is installed and authenticated, so Option 3 is available.
- If it fails with `command not found`, `gh` is not installed on this host.
- If it fails with an auth error, `gh` is installed but not authenticated.

In either failure case **say so now, before drafting**, so the user is not
surprised after approving a draft. Nothing is lost: the flow still drafts the
issue and hands it back as copy/paste text (Option 1), which needs neither `gh`
nor Browser — only that the user is signed in to GitHub in their own browser to
submit it. Do not silently proceed as if submission will work and only reveal
the gap at the submit step. The duplicate search can still be skipped on failure;
losing it does not block filing.

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

### 5. Pick Labels From the Repo's Live List

**Never hard-code the label vocabulary here.** Read it from the repository at
submit time, so labels added later are picked up without editing this skill:

```bash
gh label list --repo "$REPO" --limit 100
```

Choose from what that command returns:

- **Exactly one type label** — the defect label for bug reports, the feature
  label for requests. These are mutually exclusive; never apply both.
- **At most one `area: ` label and at most one `platform: ` label** when one
  clearly matches — the same two prefixed dimensions the triage classifier may
  choose from. No other prefixed dimension is yours to set. Apply an OS label
  only when the issue is genuinely specific to that OS — cross-platform issues
  get none.
- If no value in a dimension fits, **leave that dimension off**. An unlabeled
  dimension is better than a wrong one, and some issues legitimately belong to
  no component.

Rules:

- **Never create a new label.** If the right value does not exist, mention the
  gap to the user and submit without it — extending the taxonomy is a maintainer
  decision, not a side effect of filing an issue.
- **Do not apply automation-owned or triage-owned labels** — review/readiness
  process markers, severity or release-blocking markers, follow-up or
  blocked markers, the release-channel labels (`channel: *`, derived by
  triage from the bug form), and the sizing labels (`tier:*`, `pending-triage`,
  `triaged`) that the issue gate reads to let a PR merge. A freshly filed
  request has no way to know those apply, and the workflows that own them will
  set them.

Collect the chosen names for the submit step below.

If `gh` is unavailable or unauthenticated, `gh label list` fails and you cannot
read the taxonomy. Still apply a type label in that case — bug for defects,
enhancement for feature requests, the two that have always existed — and skip
the grouping dimensions, which are the part that grows. Do not guess a grouping
value you could not read.

### 6. Submit — Offer the Options

What you can offer depends on the capability check in step 3. If `gh` worked,
all three options below may apply. If it did not, Option 3 is unavailable on this
host; offer Option 1, and Option 2 only when its query string is short (see
below).

Lead with the routes that reliably reach the user. Crew's chat redacts any
model-written URL whose query string is 200 characters or longer, with no
exception for issue links (this is intentional — injected content could hide
private context in a prefilled `body=`, and the issue it creates is public). A
prefilled link carrying a drafted title and body almost always crosses that
length and would render as `[REDACTED: suspicious URL]` rather than a clickable
link, so Option 1 is the primary route and Option 2 is offered only in the rare
short-query case where it survives redaction.

**Option 1: Copy/paste** (always works, nothing to install)

Show the formatted title and body in a code block the user can copy, and give
them the plain new-issue form link — it has no query string, so it is never
redacted:

```
https://github.com/kirodotdev/KiroCrew/issues/new
```

The user pastes the title and body into that form and submits. This needs only
that they are signed in to GitHub in their own browser. You can also point them
at the feature-request template form, whose only query parameter is short enough
to survive redaction:

```
https://github.com/kirodotdev/KiroCrew/issues/new?template=feature_request.yml
```

**Option 2: Pre-filled URL** (only when its query string is short)

Build a GitHub new-issue URL with query params:

```
https://github.com/kirodotdev/KiroCrew/issues/new?title=URL_ENCODED_TITLE&body=URL_ENCODED_BODY
```

Do **not** add a `labels=` parameter: GitHub answers 404 to a `labels` query
from anyone without permission to label issues in this repo, which is most
users (chosen labels apply only through Option 3, and triage labels the rest).
**Percent-encode the title and body in full**, not just their spaces: an
unencoded `&` starts a new query param and an unencoded `#` pushes the remainder
into the URL fragment, either of which silently drops the drafted body from the
pre-filled issue.

You build this URL, so you know its length: offer it **only when its query
string is under 200 characters** (a short title and little or no body). At or
above 200, Crew redacts it in chat as a suspicious URL (intentional, no
exception for issue links), so do not offer it — leave it out silently and use
Option 1 rather than handing the user a link that renders as a
`[REDACTED: suspicious URL]` placeholder. (A URL over ~4000 chars may also be
truncated by GitHub, another reason the short-query case is the only one worth
offering here.)

**Option 3: Direct creation via `gh` CLI** (only if the step 3 check succeeded)

On the user's choice, allocate files, then use the file-writing tool to write
`BODY_FILE` with the confirmed body and `TITLE_FILE` with the confirmed title,
per **Shell safety** above:

```bash
BODY_FILE=$(mktemp -t kc-issue-body.XXXXXX.md)
TITLE_FILE=$(mktemp -t kc-issue-title.XXXXXX.txt)
```

Then run:

```bash
TITLE="$(cat "$TITLE_FILE")"
gh issue create --repo "$REPO" \
  --title "$TITLE" \
  --body-file "$BODY_FILE" \
  --label '<type label>' \
  --label '<grouping label, if one was chosen>'
```

Pass one `--label` flag per name chosen in step 5, each **single**-quoted. Label
names can contain spaces, and single quotes also keep a `$` or backtick in a name
literal — double quotes would let the shell expand it. Omit the extra flags when
no grouping label applies.

This requires `gh auth` on the user's machine. If it fails with auth errors,
fall back to Option 1 (copy/paste).

## Guidelines

- Keep the conversation light — this isn't a support ticket form
- Two to three exchanges max before drafting
- Always show the draft before submitting
- If the user just wants to vent without filing, that's fine too — acknowledge
  and offer to file if they want
