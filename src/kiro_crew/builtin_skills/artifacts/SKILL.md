---
name: artifacts
description: Persist, version, and iterate on LLM-generated UI (widgets, HTML, markdown). Load when the user wants to save, find, update, or iterate on a previously-rendered widget — anything that should outlive the chat scrollback.
triggers: artifact, save widget, save this, iterate, iterate on, update the widget, change the widget, version, library, find widget, what have we built, iterate again, redo the
---

# Artifacts (`@kirocrew-core/artifact_*`)

A widget rendered inline in chat (`<mcwidget>`) is **transient** — it scrolls
away with the conversation. An **artifact** is a widget (or other content)
that's been given a stable identity, a version history, and a URL the user
can open from `/artifacts/<slug>` in the dashboard.

Artifacts exist so the user can build a library of durable, named work — useful
UIs (CR queue, pipeline health, ticket triage, dashboards) and substantial
documents (plans, design docs, analyses, reports) alike — to return to, iterate
on across sessions without losing prior versions, or share with peers for
feedback. The library is curated: it holds work worth keeping, not a mirror of
every file or a copy of the chat.

## Mental model

| Concept | Means |
|---|---|
| **Slug** | URL-safe identifier like `cr-queue` (auto-derived from name). Stable across versions. The user references artifacts by slug. |
| **Version** | Monotonic integer. Every content change bumps it. The 50 most-recent versions are retained; older ones get pruned (configurable via `MAX_VERSIONS`). |
| **Save** | Persist the artifact for the first time. Picks a slug, returns it. |
| **Update / Iterate** | Modify content of an existing artifact. Bumps version, preserves history. |
| **List / Find** | Discover what's saved. Filter by tag, kind, name substring. |

## Tools

| Tool | Purpose |
|---|---|
| `artifact_save` | Create a new artifact, returns slug |
| `artifact_get` | Load content + metadata (optional version) |
| `artifact_update` | Modify content/metadata; bumps version on content change |
| `artifact_list` | Filter by `tag`, `kind`, `q` (name substring) |
| `artifact_versions` | List version numbers for a slug |
| `artifact_delete` | Permanently remove |

All under the `@kirocrew-core` MCP server.

## When to save proactively

When you produce work worth keeping — something the user would plausibly want
later — save it:

- A **widget** with its own identity (CR queue, pipeline dashboard, ticket
  card): save it without asking, and note it in one line — *"Saved as artifact
  `cr-queue`."*
- A substantial **document** (plan, design, analysis, report, reference): if you
  wrote it to a workspace file, save it file-backed (`artifact_save` with
  `source_path`, and `kind="markdown"` for markdown); if it lives only inline in
  chat, offer to save it.

Don't save throwaway output: one-shot answers, quick demo widgets, scratch
notes, or project/package code that belongs in a CR.

## Reuse before starting fresh

Before writing a plan/doc/analysis from scratch, check whether one already
exists: `artifact_list(q="<topic>")` finds your own artifacts — offer to iterate
on a strong match instead of duplicating it. (Use `artifact_list` for this;
don't run a knowledge search just to find artifacts.)

## Always check before `artifact_save` (kind=widget)

Before calling `artifact_save` with `kind=widget`, **always** call
`artifact_list(kind="widget", q="<name>")` first to check for an existing
artifact with the same name. If a match exists, do **not** call
`artifact_save` — call `artifact_update` on the existing slug instead so
the new content captures as a new version of the same artifact identity.
Only call `artifact_save` after `artifact_list` returns no name match.

This rule applies regardless of how the user phrased the request. "Save
this version", "save this", "remember this", and silent auto-saves all
go through the same pre-save check. The artifact-store backend also
attaches a duplicate-warning hint to the `artifact_save` response when
a same-named widget artifact already exists — if you see that hint, the
save just created a duplicate and you should `artifact_delete` the new
one and `artifact_update` the existing one to recover.

The frontend's bookmark click runs an equivalent dedup probe before
POSTing, so a user clicking the bookmark icon won't create duplicates
even if you forget the rule above. The rule still matters for
explicit "save this" turns where the user goes through you, not the
bookmark.

## Re-emitting a saved widget — slug attribute is REQUIRED

Whenever you emit an `<mcwidget>` body that came from (or just became) a
saved artifact, include the slug as an attribute on the opening tag:

```html
<mcwidget title="CR Queue" slug="cr-queue">
…body…
</mcwidget>
```

This binds the impression to the saved artifact. The bookmark icon
renders filled, the title links to `/artifacts/<slug>`, and clicking
the bookmark un-saves rather than creating a duplicate.

**Always emit the slug on:**

- The first re-render right after `artifact_save` returns
- Every re-render after `artifact_update` (iteration)
- Any re-emission of a previously-saved widget across sessions
  (find it via `artifact_list(q="...")` — see "iterate without a slug" below)

The tool responses for `artifact_save`, `artifact_get`, and `artifact_update`
all return a re-emit hint with the exact `<mcwidget title="..." slug="...">`
opening tag — copy it verbatim. If you find yourself typing the tag from
memory you're doing it wrong.

**If you forget the slug**, the user clicking save creates a duplicate
artifact. The frontend has a title-based safety net that catches most
cases (it searches for an existing artifact with the same name on save
click and binds to the most recently updated one), but the safety net
is a backstop for legacy widgets and agent compliance failures — not a
substitute for threading the slug correctly.

## Slug semantics

- Slugs are opaque server-managed identifiers. For artifacts you create,
  the response from `artifact_save` carries the slug — preserve it.
- Slugs decouple from titles. A user can rename an artifact ("CR Queue"
  → "Pull Request Dashboard"); the slug stays the same. Find the slug
  for a possibly-renamed artifact via `artifact_list(q="...")` — version
  snapshots also capture historical titles.
- For brand-new widgets you've never saved, you may omit `slug=` and the
  frontend will derive a stable identity from the message location. This
  works because the same chat message renders the same derived slug on
  every load — so if the user clicks bookmark, refreshes, and clicks
  again, the second click hits the same slug (idempotent, no duplicate).
- The skill rule for re-emissions still applies: once an artifact exists,
  always thread its slug.

## When the user clicks the bookmark icon

The frontend bookmark POSTs directly to the API and updates its own UI
state — the icon flips between filled (saved) and unfilled (not saved).
**You don't get a chat event for this.** The save and un-save are
intentionally silent so the conversation history stays clean.

What this means in practice:

- Don't expect or wait for a `[UI] saved-as-artifact` message after the
  user clicks the bookmark.
- If the user later asks to iterate on something they bookmarked silently,
  use `artifact_list` (most recent first; filter by `q` if you have a name
  hint). The "iterate without a slug" decision tree below covers this case.
- Server is the source of truth for "is it saved?" — every widget impression
  GETs `/api/artifacts/<slug>` on mount and on tab visibility change, so
  bookmark state stays consistent across tabs / sessions / refreshes
  without you doing anything.

## The "iterate" flow

The user says "iterate on artifact <slug> — change X". Flow:

1. `artifact_get(slug)` → read current.html
2. Modify the HTML to address the change
3. `artifact_update(slug, content=new_html)` → version bumps to vN+1
4. Re-emit the same widget body in chat (so the user sees the result inline)

### Companion chat sessions

Some sessions are **artifact-bound companions**: the user opened them from an
artifact's detail page, and your session context includes an injected entry
("Companion chat for artifact \`<slug>\` …") naming the slug, kind, version,
and open-comment count. In a companion session the user speaks naturally
("summarize this", "make the header sticky") without repeating the slug —
resolve every artifact reference to the slug from that context entry. Your
behavior is otherwise unchanged: the same iterate flow and the same comment
triage rules apply, and the user sees your `artifact_update` results live in
the page next to the chat.

### "Iterate" without a slug

When the user says "iterate on the widget" / "update the date widget" /
"change the badge to red" without specifying a slug, follow this decision
tree:

```
Did the conversation already establish an artifact slug
(via a prior artifact_save call by you, or a slug= attribute on a widget
you re-emitted earlier this session)?
├── YES → use that slug, run the iterate flow above
└── NO ──┬── Did you emit a widget in a recent turn that the user
        │   is plausibly referring to?
        │   ├── YES → save the previous widget body as v1 with a
        │   │        descriptive name, THEN apply the change as v2
        │   │        via artifact_update. Surface the slug to the user
        │   │        ("Saved as `<slug>` and updated to v2 with the change.")
        │   └── NO ──── call artifact_list (most recent first); if a
        │                strong match exists, confirm with the user
        │                ("Did you mean `cr-queue` (last updated 2m ago)?")
        │                before iterating. If no match, ask which artifact
        │                they mean.
        └── (fallthrough) ─→ ask the user to disambiguate.
```

Critical: **never tell the user "the widget wasn't saved, so I can't
iterate"** — that contradicts the entire point of the feature. Save it
yourself, iterate on it, and tell them what slug you assigned.

## Comment triage when addressing feedback

Comments delegate work: once a comment's directive is carried out, the
comment has done its job. When you address artifact comments (the user asked
you to iterate / "address the comments"), triage EVERY open comment as part
of the same pass — never leave the human to re-read and clean up stale
annotations by hand.

A comment may be **anchored**: `artifact_get_comments` returns the exact quoted
span it was attached to, because the human selected that text in the artifact
before writing the note. Treat an anchored comment as an instruction *about that
span* — resolve it there rather than applying it globally, and re-read the span
before editing, since a prior edit may have moved it. An anchor whose quote no
longer exists in the content comes back flagged as orphaned; say so instead of
guessing where it used to point.

| Case | Action |
|---|---|
| Unambiguous directive, fully applied ("delete this", "fix typo", a clear reframe) | `artifact_delete_comment` with a reason ("applied in vN: <what you did>") |
| Applied with interpretation or judgment the human may want to check | `artifact_mark_review` + short `artifact_reply_comment` stating what was done |
| Not applied / you disagree / needs discussion | `artifact_reply_comment` with your reasoning; leave the thread open |
| Anchor text deleted *as part of* applying the comment | Same as row 1 — delete |

Rules:

- Delete means the comment's job is done and re-reading it adds zero value.
  When in doubt between delete and REVIEW, choose REVIEW.
- Never delete provider-synced comments (the tool refuses); mark those
  REVIEW instead.
- Resolution (`resolved` status) is human-only — never attempt it.
- Do the triage in the SAME turn as the `artifact_update`, comment by
  comment. Deletions are audited and appear in the artifact's activity feed
  with your reason, so nothing disappears without a trace.
- In your summary to the user, account for the comments in one line:
  "Applied 5 comments (4 deleted as done, 1 marked for your review)."

## Naming and slugs

- The user-facing **name** is human-readable: "CR Queue Dashboard".
- The **slug** is auto-derived (lowercase, hyphens) and is the stable handle.
- If the user provides a slug explicitly, validate it matches `^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$`.
- For auto-saves, pick a name that reflects the widget's purpose, not the literal title bar text. "Today's status" is fine; "Untitled widget" is not.

## Discovery

When the user asks "what have we built?" / "what artifacts do we have?" /
"show me my widgets", call `artifact_list` and present the results — slug,
name, kind, version, updated_at. Group by tag if that aids comprehension.

`artifact_list` accepts `tag`, `kind`, and `q` (name substring) filters.
Use them to narrow when the user gives constraints.

## Versioning rules

- `artifact_update(slug, content=X)` ALWAYS bumps the version when content changes.
- Metadata-only updates (rename, retag, edit description) do NOT bump.
- Old versions are preserved up to `MAX_VERSIONS = 50`; older ones get pruned.
- The user can browse versions in the dashboard at `/artifacts/<slug>` (dropdown).
- To roll back: `artifact_get(slug, version=N)` to read, then `artifact_update(slug, content=that_html)` to make it the new current.

## Tags and kinds

Tags are free-form, ≤ 16 per artifact. Useful tag conventions:
- Workflow scope: `cr`, `pipeline`, `ticket`, `oncall`, `op`
- Data source: `slack`, `web`, `upload`
- State: `wip`, `archived`

Kind is one of `widget` (default), `html`, `markdown`, `svg`, `json`, `text`.
Use `widget` for `<mcwidget>` bodies; the others for raw content the
dashboard renders differently.

## Theme safety (widget / html kinds)

Widget and html artifacts render inside the dashboard's **themed iframe**
in whatever theme the user runs. The one rule that prevents unreadable
artifacts: **never set one half of a foreground/background pair with a
literal color** — the iframe injects
`body{background:var(--bg);color:var(--text)}` defaults, so a lone
`color:#111` sits on the injected dark canvas in dark mode. This applies
to script-generated pages pushed via the tools or CLI just as much as to
inline `<mcwidget>` bodies. The full contract — the variable table and
the `color:var(--text,#111)` fallback pattern for HTML that must also
render standalone — lives in the `widgets` skill ("The theme contract").
`artifact_save` / `artifact_update` responses attach a `⚠️` warning when
widget/html content carries hardcoded colors and no `var(--…)` reference.

## Don't

- Don't save mcwidget output as `kind: html` — `widget` is correct.
- Don't include the surrounding `<mcwidget title="...">` tag in `content`.
  Save the *inner* HTML body so the artifact page can wrap it in the same
  iframe sandbox.
- Don't hardcode a color palette in widget/html artifacts — see "Theme
  safety" above.
- Don't churn versions on cosmetic re-renders. If you're emitting the same
  widget for display purposes (no change), don't call `artifact_update`.
- Don't `artifact_delete` without explicit user direction. Deletes are
  permanent.

## Worked example

```
User: render a small mcwidget showing today's date and a colored status
       badge.

You: <mcwidget title="Today's status">… body …</mcwidget>
     (Heuristic: probable ongoing dashboard intent → auto-save.)
     [calls artifact_save(name="Today's status", content="<inner html>",
       kind="widget")]
     Saved as `today-s-status`.

User: change the badge to red.

You: [calls artifact_get("today-s-status")]
     [modifies the badge color]
     [calls artifact_update("today-s-status", content=new_html)]
     <mcwidget title="Today's status" slug="today-s-status">
       … body with red badge …
     </mcwidget>
     Updated to v2.
```


## Showing diffs that the dashboard can act on

When you summarise a content change to a file-backed artifact (after
`artifact_update`, `artifact_revert`, or any edit), the dashboard renders
fenced ```diff blocks specially — including an **Open file** button in
the diff header that drops the user into the file in the side panel.
That button only appears when the diff contains standard unified-diff
file headers. For artifact reverts, iterations, and any edit where you
have a `source_path` available, **always include those headers** so the
affordance works.

Required header lines, in order, at the top of the diff body:

```
--- <source_path>
+++ <source_path>
@@ -<oldStart>,<oldLines> +<newStart>,<newLines> @@
```

Use `/dev/null` on the `---` line for new files, and on the `+++`
line for deletions. The dashboard's diff renderer accepts both this
plain form and git's `--- a/<path>` / `+++ b/<path>` form, but the
plain form matches `KiroCrew`'s system prompt (`config/prompt.md`)
so emit it consistently.

Example for an artifact-revert summary where the artifact's
`source_path` is `~/notes/test-doc.md` and you reverted to v2:

````
Reverted `test-doc-md` to v2's content, saved as v4.

```diff
--- ~/notes/test-doc.md
+++ ~/notes/test-doc.md
@@ -1,6 +1,6 @@
 # Hello

 This is **bold**

 I am editing this from the side panel.
-
+This edit is from the Artifact's detail page.
```
````

For chat-backed artifacts (no `source_path`), there's no file to open,
so plain ```diff blocks without headers are fine — the dashboard simply
doesn't render the Open file button.

How to obtain the source path:

* The full `Artifact` returned by `artifact_get` includes `source_path`
  (empty string for chat-backed artifacts). Read it once at the start
  of an edit and reuse for any diff you summarise.
* Diff line numbers come from comparing the two versions you're showing
  (e.g. v2 vs v4). If you don't know exact line numbers, a single
  `@@ -1 +1 @@` hunk header is acceptable — the file path is what
  matters for the Open file button.
