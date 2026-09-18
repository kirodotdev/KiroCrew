---
name: artifacts
description: Persist, version, and iterate on LLM-generated UI (widgets, HTML, markdown). Load when the user wants to save, find, update, or iterate on a previously-rendered widget — anything that should outlive the chat scrollback.
triggers: artifact, save widget, save this, iterate, iterate on, update the widget, change the widget, version, library, find widget, what have we built, iterate again, redo the
---

# Artifacts (`@kirocrew-core/artifact_*`)

A widget rendered inline in chat (`<mcwidget>`) is auto-registered as an
**unpinned** artifact when its response segment finalizes — a record, not a
library entry: unpinned auto-registered widgets are pruned oldest-first, and
widgets from an incognito or temporary session are never registered at all.

Saving and pinning are two different things. `artifact_save` creates a **named,
durable** artifact: it is never swept, because the sweep only ever considers
auto-registered records. **Pinning** is the separate star (`pinned`, false by
default) the user flips in chat or the library; it is what takes an
auto-registered widget out of the sweep, and it is not something a save does for
you. Either way the artifact has a stable identity, a version history, and a URL
the user can open from `/artifacts/<slug>` in the dashboard.

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
| **Version** | Monotonic integer. Every content change bumps it. The 50 most-recent versions are retained; older ones are pruned. The cap is a fixed constant with no config or env override. |
| **Save** | Persist the artifact for the first time. Picks a slug, returns it. |
| **Update / Iterate** | Modify content of an existing artifact. Bumps version, preserves history. |
| **List / Find** | Discover what's saved. Filter by tag, kind, name substring. |

## Tools

| Tool | Purpose |
|---|---|
| `artifact_save` | Create a new artifact, returns slug |
| `artifact_get` | Load content + metadata (optional version) |
| `artifact_update` | Modify content/metadata; bumps version on content change |
| `artifact_revert` | Restore a prior version as the new live state, snapshotting the rollback |
| `artifact_list` | Filter by `tag`, `kind`, `q` (name substring) |
| `artifact_versions` | List version numbers for a slug |
| `artifact_delete` | Permanently remove |
| `artifact_move` | File an existing artifact into a folder, or unfile it |
| `artifact_folder_list` | Read the folder tree — ids, paths, item counts |
| `artifact_folder_create` | Create a folder; missing path segments are auto-created |
| `artifact_folder_rename` | Rename a folder |
| `artifact_folder_move` | Reparent a folder (cycle-guarded) |
| `artifact_folder_delete` | Remove a folder; safe by default, destructive with `delete_contents=true` |
| `artifact_get_comments` | Read every comment thread on an artifact |
| `artifact_post_comment` | Open a thread, optionally anchored to a quoted span |
| `artifact_reply_comment` | Reply in an existing thread |
| `artifact_mark_review` | Advance a thread to REVIEW — addressed, awaiting human check |
| `artifact_delete_comment` | Delete a thread you demonstrably applied; requires a reason |
| `deploy_artifact` | Preview-only deploy of a static artifact (`widget`/`html`/`markdown`) or a local built directory; a `kind=webapp` slug is rejected |

All under the `@kirocrew-core` MCP server.

## When to save proactively

When you produce work worth keeping — something the user would plausibly want
later — save it:

- A **widget** you just emitted: do NOT call `artifact_save` on it. Its
  auto-registration already happened, so a save creates a second record and the
  tool answers with a duplicate warning. Emitting it is enough; the user's star
  pins it. Reach for `artifact_save` on widget content only when it was never
  emitted in a message.
- A substantial **document** (plan, design, analysis, report, reference): save
  it with `artifact_save(..., kind="markdown")`. `artifact_save` has no
  `source_path` parameter — `source` is a provenance marker only
  (`chat` | `cron` | `subagent` | `manual` | `import`), and a file-backed
  artifact is created by the dashboard's file and knowledge paths, not by this
  tool. If the document lives only inline in chat, offer to save it.

Don't save throwaway output: one-shot answers, quick demo widgets, scratch
notes, or project/package code that belongs in a CR.

## Reuse before starting fresh

Before writing a plan/doc/analysis from scratch, check whether one already
exists: `artifact_list(q="<topic>")` finds your own artifacts — offer to iterate
on a strong match instead of duplicating it. (Use `artifact_list` for this;
don't run a knowledge search just to find artifacts.)

## Always check before `artifact_save` (kind=widget)

Before every `artifact_save(kind="widget")`, call
`artifact_list(kind="widget", q="<name>")`. Update a name match with
`artifact_update` instead; save only when none exists. This applies to explicit
"save this" requests and proactive saves alike.

A duplicate-warning hint means the save already created a duplicate. Update the
existing slug; delete the new duplicate only with explicit user direction, as
required under **Don't** below.

## Re-emitting a saved widget — slug attribute is REQUIRED

Whenever you emit an `<mcwidget>` body that came from (or just became) a
saved artifact, include the slug as an attribute on the opening tag:

```html
<mcwidget title="CR Queue" slug="cr-queue">
…body…
</mcwidget>
```

This binds the impression to the saved artifact, fills the bookmark, and links
the title to `/artifacts/<slug>`; a bookmark click un-saves rather than duplicates.
Include the slug on the first render after `artifact_save`, every render after
`artifact_update`, and every re-emission across sessions (discover with
`artifact_list` when needed).

`artifact_save`, `artifact_get`, and `artifact_update` return the exact
`<mcwidget title="..." slug="...">` re-emit hint: copy it verbatim, not from memory.
The frontend's title-based bookmark dedup binds the most recently updated name
match as a legacy backstop, not a substitute for threading the slug.

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

Bookmark save/un-save goes straight to the API and updates the icon; **it emits
no chat event**, so never wait for `[UI] saved-as-artifact`. For a later request,
use `artifact_list` (most recent first, optionally filtered by `q`) and the
"iterate without a slug" flow. The server owns bookmark state: impressions GET
`/api/artifacts/<slug>` on mount and tab visibility change, keeping tabs and
sessions in sync without agent action.

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
        │   ├── YES → find its auto-registered slug via artifact_list,
        │   │        then run the iterate flow. Only if no record exists
        │   │        and saving is allowed, use the pre-save check above
        │   │        to save the old body as v1 before updating to v2.
        │   └── NO ──── call artifact_list (most recent first); if a
        │                strong match exists, confirm with the user
        │                ("Did you mean `cr-queue` (last updated 2m ago)?")
        │                before iterating. If no match, ask which artifact
        │                they mean.
        └── (fallthrough) ─→ ask the user to disambiguate.
```

Critical: **never tell the user "the widget wasn't saved, so I can't
iterate"**. Recover or, where permitted, save its identity, then iterate and
report the slug. Incognito and temporary sessions forbid artifact writes; do not
try to save there, and say plainly that the revision is not persisted.

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

### Folders

The library is a tree. `artifact_save` and `artifact_move` both take `folder`
as a folder id OR a `/`-separated human path (`Reports/Q3`), and missing
segments are created for you — so file an artifact at save time whenever it
belongs with others instead of leaving it at the top level. `''` or `root`
unfiles one. `artifact_folder_list` gives ids, paths and item counts, which is
what you read before moving anything; `artifact_folder_create` /
`artifact_folder_rename` / `artifact_folder_move` reshape the tree, and a move
cannot make a folder its own descendant.

`artifact_folder_delete` defaults to SAFE: it re-parents the folder's children
up to its parent and removes only the folder. `delete_contents=true`
permanently deletes the whole subtree INCLUDING every descendant artifact —
echo the affected count to the user and get agreement before calling it that
way.

## Versioning rules

- `artifact_update(slug, content=X)` ALWAYS bumps the version when content changes.
- Metadata-only updates (rename, retag, edit description) do NOT bump.
- Old versions are preserved up to the 50-version cap; older ones get pruned.
- The user can browse versions in the dashboard at `/artifacts/<slug>` (dropdown).
- To roll back, call `artifact_revert(slug, target_version=N)`. It reads version
  N and writes it as the new live state, creating a fresh snapshot tagged
  `reverted` so the activity timeline shows the rollback. Do not hand-roll a
  rollback with `artifact_get` + `artifact_update`.

## Tags and kinds

Tags are free-form, ≤ 16 per artifact. Useful tag conventions:
- Workflow scope: `cr`, `pipeline`, `ticket`, `oncall`, `op`
- Data source: `slack`, `web`, `upload`
- State: `wip`, `archived`

Kind on the agent tools is one of `widget` (default), `html`, `markdown`, `svg`,
`json`, `text`, or `webapp` — that is the enum `artifact_save` and
`artifact_list` accept. Use `widget` for `<mcwidget>` bodies; the others for raw
content the dashboard renders differently. `svg` stores vector source, versioned
and revertible like any other kind. `webapp` is a deployed web application,
rendered as an infra control card and carrying `webapp_metadata`.

There is an eighth store-level kind, `image`, and you cannot create it with
these tools: raster artifacts are registered by the backend when your finalized
message embeds `![alt](/absolute/path.png)`. So to keep a generated diagram,
chart or screenshot, embed it that way rather than passing `kind="image"` —
which the tool does not offer and which would store your text under an image
label.

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
User: change the badge on artifact today-s-status to red.
You: artifact_get("today-s-status")
     [modify the badge]
     artifact_update("today-s-status", content=new_html)
     <mcwidget title="Today's status" slug="today-s-status">…new body…</mcwidget>
```


## Showing diffs that the dashboard can act on

For each file-backed artifact change (`artifact_update`, `artifact_revert`, or
an edit), show a fenced `diff` block with unified headers so the dashboard can
render its **Open file** button:

```
--- <source_path>
+++ <source_path>
@@ -<oldStart>,<oldLines> +<newStart>,<newLines> @@
```

Read `source_path` from `artifact_get` at the start and reuse it. Use `/dev/null`
on the `---` line for a new file or the `+++` line for a deletion. Prefer plain
paths; git's `a/` and `b/` prefixes also work. Compare the shown versions for line
numbers; if unknown, `@@ -1 +1 @@` is accepted because the button uses the path.
For chat-backed artifacts (`source_path` is empty), headerless `diff` blocks are
fine: there is no file to open.
