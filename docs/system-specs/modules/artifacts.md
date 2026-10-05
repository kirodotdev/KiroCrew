# Artifacts Module

## Overview

Artifacts give chat-rendered LLM-generated UI a persistent identity, version
history, and a stable handle the agent can iterate on across sessions.

A typical flow:

1. Agent emits an `<mcwidget>` in chat ("here's your CR queue")
2. When the assistant segment finalizes, the backend auto-registers the widget
   as an unpinned artifact under `~/.kiro/crew/artifacts/<slug>/current.html`
3. Days later, in a fresh session, the user asks to iterate on that stable slug
   and add an age column
4. Agent calls `artifact_get("<slug>")` to read the current HTML, modifies it,
   then calls `artifact_update("<slug>", content=…)` to publish a new version
5. The previous version is preserved under `versions/v1.html` for rollback

The dashboard provides a `/artifacts` library page for browse/search and a
`/artifacts/<slug>` standalone view with a version dropdown.

### Library scrolling

Small galleries and single-column virtualized lists scroll with the page.
A multi-column gallery with at least 30 entries fills the remaining page height
when no discovery-capable provider is available. With an available discovery
provider, the page keeps its scroll axis and the saved masonry uses a bounded
60vh viewport. Chat documents and remote lists stay in normal page flow, so
expanding a section cannot compress the toolbar or hide later provider rows.
Provider capability selects the layout, not the result of a remote fetch.
Loading, empty results, filtering, and read errors therefore keep the same mode.

## Storage Layout

### Dynamic Dashboard presentation

Automatic session status cards and saved task views have different owners.
With `dashboard.dynamic_dashboard_cards` enabled, host session events queue a
bounded background update using only that session's recent, redacted messages.
Team workers (sessions another session created) get no automatic card.
The model chooses the card's HTML/CSS and flat text fields; subsequent updates
can omit HTML and reuse the prior layout with exactly the same data field names.
A field-name change requires explicit replacement HTML; invalid data-only output
does not blank the valid prior publication. Large prior layouts stay on the host
when necessary to leave room for recent evidence in the bounded model input.
Generation failure retains prior content only while its source and privacy remain
valid. These cards are transient, not saved
artifacts, and do not require a conductor or worker to call `artifact_update`.
The host shows their content publication time separately from live run state.
The event, privacy and resource contract is in
[learn-cron-dashboard](learn-cron-dashboard.md#automatic-session-status-cards).
The Needs you inbox precedes cards, and all answer/approval authority stays in
native controls. Disabling automatic content does not disable those controls.

### The root session's automatic card: numbers from folds, sentences from the model

Only a ROOT session gets an automatic card. Root is
`card_lifecycle.is_root_session`: an empty `_created_by` (the birth-time edge) AND no
parent in the crew log's session tree (the edge an adopt or release moves later, the
same `parent_slot is None` the sidebar reads through `parent_payload`). A worker gets no
card; the browser's `SessionStatusFrame` mirrors both edges (`created_by` and `parent`)
and never fetches one for it.

Every number on that card comes from the session's own crew log. `build_crew_main` in
`kiro_crew.crew_main_contract` folds four renders -- `status`, `work`, `usage`,
`approvals` -- into `CrewMainDerived`, every value a finished string. Absence is
three-state in words: a missing key reads `not recorded`, a fold that could not be read
reads `could not be read`. No value is a percentage, and every count states its
denominator.

The model still designs the card's layout, as before, but writes no number. It receives
the folded values under `facts` as read-only text and binds each one by field name with
`data-dashboard-field`; its own data is exactly `lede`, `you` and `notes`. The publish
seam (`_root_card_output`) refuses the whole card when the model's part carries a digit
-- in a sentence, in any text the layout shows, or as a JSON number -- when it writes a
field it does not own, when its layout binds a name outside the contract, or when its
layout leaves any fact unbound (`_layout_hides_a_fact`), since a layout of three
sentences would publish a card with no numbers. Digits in CSS are layout and pass. Only then does `merge_crew_main`, which names every field, put
the folded values beside the three sentences.

The three sentences are written in one language, which the host names under
`language` in the evidence (`_card_language`): the configured dashboard UI language
(`context.ui_language_tag`) when there is one, as for the session title, otherwise a
short sample of the newest text the user typed, with pasted code and links cut out.
The facts, the previous card, assistant replies and automation rows each carry a
language of their own, so the prompt tells the model never to take the language from
them.

The session folds are read from the crew log UNIT the slot writes now
(`crew_log.emit.slot_previous_store`), never from the slot's session key: a fold of a
name no unit carries is an empty record whose counts read as zero. No unit of the slot
at all reads `not recorded`; units the store cannot rank read `could not be read`.

Numbers follow the log between generations. Each batch the crew-log writer commits
(`emit.add_growth_listener`) for a slot whose card is already published re-folds and
re-binds the numbers on a task of its own, with no model
call, no permit and none of the hourly budget; the layout and the sentences stay as the
model last returned them. The opt-in and the budget therefore pace the sentences only.

An HTML/widget artifact tagged `task-dashboard` is a model-authored task view,
not a fixed dashboard schema. The chat's **Dynamic Dashboard** side-panel tab
(labelled **Dashboard**; the three-tile dock above the composer opens it) and Crew's
single **Dashboard** tab select
only artifacts whose recorded originating slot is the current slot or a durable
`created_by` descendant. A presentation-only child session can therefore publish
without impersonating its conductor. The same slug is updated at milestones;
visible hosts re-read the artifact inventory on each `artifact_update` frame and
load new revisions.

The side panel's Dashboard view hands its whole **Overview** to that published
view: the host draws the header (title, help, permission mode), the Overview /
Questions / Approvals segments with their counts, and the stale / missing-source
notices, then renders the selected published view and nothing native beside it.
The automatic card (`SessionStatusFrame`) shows only while no published view
exists; progress bars, status tiles, blocked and work-item lists are not drawn
in the panel (the dock above the composer keeps its native tiles). Questions and
Approvals remain host-rendered `AttentionCard`s — the sandboxed page can name a
decision but never answer or approve one. The request that asks the agent for a
page (`commandCenter.prompt.ts`, `REQUEST_PUBLISHED_VIEW`) recommends, without
enforcing, a layout for that whole-Overview placement: what needs the user first
with the decision named or linked (answering happens in the Questions tab), one
line per work item with a status word and details folded, dependencies shown when
tasks wait on others, cost and technical detail inside the folds, theme CSS
variables. The artifacts skill repeats the recommendation.

The whole Dynamic Dashboard surface is a developer Feature Preview
(`PREVIEW_DASHBOARD`, `website/src/utils/previewFlags.ts`), default OFF and
gating INGRESS only: with the flag off the dock, the + menu entry, a persisted
Dashboard tab, the Crew chat's Dashboard tab and the Sessions menu's All
Dashboards item are withheld, while `/session-dashboards` stays routable and
every API above is unchanged. The **Automatic cards for all sessions** switch
lives inside that preview's card in Settings > Developer > Feature Previews,
shown only while the flag is on.
Session matching strips the dashboard scope and normalizes registered channel
keys with the history safe-key rules, retaining the channel namespace. Unknown
prefixes are not folded; missing task roots remain fail-closed.
Models choose the layout and task-specific content; no particular board or graph
is mandatory. The `artifacts` skill documents this publishing contract.
Crew's existing member-published webview shares this presentation selector, not
its renderer or permissions. Its member-panel API and sandbox remain unchanged;
a pipeline publication is one view within the same dashboard. The global session
dashboard supplies the cross-session summary and Needs you inbox, while questions
and approvals remain native host controls outside every published document.
The Crew entry stays **Dashboard**; the publication's expand/collapse, dialog,
loading and error chrome consistently names the **published view**.

The host independently projects live sessions, subagents, workflows and accepted
conductor work. It never treats idle sessions as completed work, nor worker
`done` reports as conductor acceptance. Missing/failed sources are shown as
unknown or stale, not as an empty successful run. Questions and approvals have
native host controls, exact session/request identities and explicit submission;
Normal/Reads/Trust/YOLO mode is explicitly labeled as the permission mode, never
changed by the dashboard. Native Reject once addresses both the owning slot and
exact request ID through the slot approval endpoint, which preserves the
`rejected_once` decision without rejecting the remaining batch. Connection-scoped
request IDs may collide across unrelated sessions and must never be resolved by
a global ID scan. Dashboard actions bind the request's origin as well: native
actions send `origin: native` and require that exact slot's live request; coordinator
actions send `origin=coordinator` and the inventory record's exact raw slot. The
server checks the coordinator record and resolves its state-only future without
an intervening await. Stale/missing/mismatched targets return 404, never fall
through to another origin, and retire the displayed controls without claiming
success. Origin-qualified card identity prevents a coordinator's delivered state
from hiding a colliding native request after the inventory changes. Existing
callers that omit origin retain their legacy fallback behavior.
Command input stays verbatim in a keyboard-accessible scrolling
preview, including on narrow screens. No bulk approval is implied. A failed or
uncertain send retains the answer and
does not automatically retry. Native answers steer their own waiting turn when
either live run state or that slot's reloaded dashboard snapshot is running;
sibling sessions never determine this decision. Approvals are separate from informational blockers.

`TaskDashboardFrame` uses the sandbox-document service with an empty sandbox:
no scripts, same-origin, forms, popups or control bridge. A dedicated document
builder removes executable code, resource hints, nested documents and outbound
navigation before rendering. It inspects actual attributes irrespective of SVG
namespace and keeps only fragment hrefs; empty hrefs are navigation too.
Models freely design supported HTML/CSS/SVG layouts and native
disclosures; dynamic evidence arrives through published revisions, not model
JavaScript. Deny-by-default CSP permits only inline styling and data fonts; no
image loads, since an image is bytes the browser decodes for display and the
backend text scan cannot read them, so image-source attributes are removed too.
An automatic card is held to the text the backend scanned: its CSP also refuses
fonts and its `@font-face` rules are deleted (a font remaps the glyphs shown),
declarations that draw characters absent from the markup (`content`, `quotes`,
`list-style*`, `hyphenate-character`, `text-emphasis*`, `text-overflow`) are
removed, and so are the `alt`, `title`, `start` and `value` attributes the
browser displays as text. Saved views keep their authored CSS and attributes.
The page receives no credentials or host state. Model-authored status is labeled a
published view; it never replaces the host's trusted approval inventory.
Automatic card data binds through `data-dashboard-field` text containers using
`textContent`, never HTML interpolation or an executable update script. An absent
field clears the old text. Only visible frames obtain a sandbox document; hiding
or paging them out releases it. The fleet keeps wrappers for the bounded live
slot inventory (`MAX_LIVE_SLOTS`, 500) to retain each saved-view selection. Twelve
session summaries are active on a page, with one automatic card and at most one
selected saved view each: at most 24 iframe documents, not twelve mounted wrappers.
Native attention controls remain mounted independently to preserve drafts across
filters and pages. The task panel
mounts its own session's automatic card (a worker carries none) plus its selected
task publication. Under Progress it shows the work items whenever the board has
any, and adds the running runs, uncapped, only when the board is not the progress
source (absent, or with omitted entries): that is when the dock's Progress
list shows runs, and that list caps its rows and hands its overflow to the panel,
so the rest must be readable there. It carries no live run roster beyond that; idle and done runs
stay with the sidebar's Subagents and Workflows tabs; Crew's
existing protected-template renderer retains its own lifecycle.
The optional creation request is a model-facing English prompt; translated UI
copy names the published view, and the artifacts skill owns its technical
publishing contract. Source failures render through the shared error notice in
both the dock and panel, with no navigation hand-off beside unsent answer drafts.
The chat dock above the composer, in the composer's own column, is three tiles
and nothing else: progress (accepted or checked-off count, else the running
count written as "N running", under one Progress label either way), blocked, and Needs you. Each tile
is a disclosure button for its own short list (`aria-expanded`; the open tile
points at its region with `aria-controls`, and a second click closes it), grouped
under the Dashboard name; in a narrow column the tiles wrap onto further rows
rather than truncating their labels. Beside them sit two actions, open the
Dashboard tab (icon plus its "Open Dashboard" text) and hide. A tile discloses a list read from the same source as its
number: the running work items when a board exists (a blocked item is the
Blocked tile's row, a waiting one is nobody's progress), else the running runs;
the blocked runs and items; the requests. Each row hands off to the panel (the
row is the button, named by its item) and never mounts an answer or approval
control, so a draft has one home: the panel, which the dock's labelled Open
Dashboard button also reaches. The panel loads lazily with its tab, so the shell chunk carries only the
dock; a panel chunk that fails to load is caught by a boundary local to the tab,
which says so through the shared error notice with no navigation hand-off, and
never reaches the route boundary that would replace the chat page. Opening the
panel moves focus to the panel heading once it shows, unless the user
has moved focus elsewhere meanwhile. The dock hides to a single pill that
carries the hide glyph, or a red count while something needs the user (persisted
per browser as `mc-task-dashboard-hidden`), and it is one element in both forms;
the toggle unmounts the pressed control, so a hide or show made from the dock's
own controls hands focus to the counterpart (hide lands on the pill, show on the
hide button), while a mount or a persisted value takes no focus. It is removed
once a complete, current read shows every run at rest, no request waiting and
the plan complete; a paused workflow rests (no tile counts it), a queued worker
or an item's open question does not; a work board settles when every item is
accepted, rejected or abandoned, a half-loaded or disconnected inventory is
never settled, and a session with an open plan stays shown even when a board
supplies the progress number. That verdict is retained in page memory per
root across dock remounts, but is not persisted across a page reload: one entry
per root that ever settled in this page lifetime, released when that root shows
new work, cleared by reload; it arms only
after a complete read of a readable scope: before the first slot list lands
nothing is loading or stale and the empty model is vacuously settled, which
must not count. Once a
complete read has settled the task, a later connection drop, source error or
remount's loading window does not bring the dock back, and only evidence of new
work releases it — a complete read showing something running, blocked or asking,
or a live slot state that already says someone is waiting on the user. The panel header carries the same three
tiles; its explanatory copy (scope, permission-mode note, containment statement,
last-checked time) lives behind one info control.
No command-center source polls. The dock, panel and all-session view read each
source once and re-read it on the frame that announces its change: `approval` and
`approval_resolved` for both approval systems, `question_card` and its retirement
for questions, `artifact_update` for a task dashboard, the crew log's
`slot_projection` for the work board of a team holding that slot, and workflow
events into the store, with a finished, failed or cancelled run also re-reading
the workflow snapshot the store's live runs are laid over, and the store's own
workflow heal read replacing that snapshot; a reconnect re-reads all of them.
Window focus re-reads a command-center source only while it has failed, since the frame
that would refresh it may never come; a healthy source is left to its frames. The dock, mounted in every chat, reads the work
board only for a team (a slot with sessions created under it) or whenever a published
view keeps the dock relevant for that slot, so a verdict never settles over an item the
unread board still holds open; the panel always
does. Only questions and approvals decide the stale notice and the "updated"
clock, so an optional source that fails (workflows answer 503 while their service
starts) cannot hide a fresh decision; the dock, panel and all-session view show
one notice listing every failed source (workflow runs, the work items under Live
activity, published views) beside those decisions, with the reassurance said once,
so a missing source is never read as an empty one. A work board the dock does not read contributes nothing, even when an
open panel cached one. Approvals share the app shell's
`global-approvals` cache, which keeps its own 30-second refresh and is re-read on
reconnect. A `slot_projection` frame never cancels a work read in flight; one
more read follows it once it settles. The shared model's session-state rule — a
session is running while its turn runs, while subagents run, or while it
holds queued messages; a paused workflow waits and a planning
one runs — also governs the all-session view's Running badge and its sort
priority, which read the same model rather than the slot's turn flag alone. The all-session view takes its sort order
when the set of sessions, what needs attention, the filter or the page changes,
not on activity, since moving a card reloads its iframes and their single-use
documents; a card shows the published views of its whole
`created_by` team, as the task panel does. The work board is the one host source the crew log
owns, a checkpointed slot fold. Pending approvals and questions stay on the live
host inventory rather than a crew-log projection: a card needs the request's tool
input, which the crew log only digests, and a decision needs the live future the
resolve endpoints check, which a recorded request cannot prove still exists.
Incognito/temporary artifact persistence restrictions remain unchanged.

### Artifact files

```
~/.kiro/crew/artifacts/
└── <slug>/
    ├── meta.json        canonical metadata (no content)
    ├── current.html     latest content
    └── versions/
        ├── v1.html
        ├── v2.html
        └── …
```

`meta.json` schema (serialized by `kiro_crew.artifact_store.records`):

| Field | Type | Notes |
|---|---|---|
| `slug` | string | URL-safe handle. Derived from `name` when not given, resolving a collision by suffixing (`-2`, `-3`, …); an explicitly-passed slug is refused — never renamed — when it is already taken or malformed |
| `name` | string | Human-readable display name |
| `kind` | enum | `widget`, `html`, `markdown`, `svg`, `json`, `text`, `webapp`, `image` — inferred on save when the caller omits it (see [Kind inference](#kind-inference)) |
| `source` | enum | `chat` (default), `cron`, `subagent`, `manual`, `import`, `dashboard`, `slack`, `cli`, `task-runner`, `unknown` |
| `pinned` | bool | "Starred" — user-curated keep flag (default `false`). Drives the Artifacts page **Starred** view. Metadata-only; toggling does NOT bump `version`. |
| `auto_registered` | bool | `true` when the store created this record automatically from a chat-emitted `<mcwidget>` (see [Widget auto-registration](#widget-auto-registration)) rather than from an explicit save. Sweepable by the retention pass while unpinned; tolerant-loaded (pre-existing artifacts default `false`, so they are never swept). |
| `description` | string | Optional, ≤ 2,000 chars |
| `tags` | string[] | ≤ 16 labels, each a well-formed tag (see [Validation & Limits](#validation--limits)) stored in its NFC spelling |
| `version` | int | Latest snapshot version; bumps when a content change is snapshotted |
| `created_at` / `updated_at` | string | ISO 8601 UTC microseconds |

## Public API

### Python (`kiro_crew.artifacts`)

```python
from kiro_crew.artifacts import ArtifactStore, get_default_store

store = get_default_store()
art = store.create(name="CR Queue", content="<table>…</table>", tags=["ops"])
art = store.get(art.slug)
art = store.update(art.slug, content="<table>… age column …</table>", snapshot=True)
versions = store.list_versions(art.slug)
items = store.list(tag="ops")
store.delete(art.slug)

# Reconcile a provider's authoritative comments into the local mirror
# (fetch-on-view). Returns the merged list; leaves origin=="local" untouched.
store.merge_remote_comments(art.slug, "artifactory", remote_comments)
```

The store is thread-safe. A module-level singleton is available via
`get_default_store()`; pass an explicit `root` to `ArtifactStore(root=...)`
for isolated test instances.

#### Code ownership

`kiro_crew.artifacts` is the facade every caller imports. It owns the store and
keeps its whole import surface, re-exporting each moved name with one identity:
`kiro_crew.artifacts.ArtifactFolderStore` and
`kiro_crew.artifact_store.folders.ArtifactFolderStore` are the same class. Its
`__all__` is derived from what the module binds plus its forwarding table
(`_EXPORTS`), minus the forwarding machinery, so a star import exposes every
public name, the moved and forwarded ones included, and no list of names is kept
by hand. The `records` and `comments` helpers the store calls are internal to it
and are imported from their owner.

| Owner | Responsibility |
|---|---|
| `kiro_crew.artifacts` | `ArtifactStore`: the shared per-root lock, the directory layout, the fenced file IO (`_read_text` / `_write_text` / `_read_bytes` / `_write_bytes`), versions and pruning, the live `source_path` pointer and its allowed roots, publication-record reads and writes, comment and retention orchestration, the change listener and the `kirocrew.artifact.created` counter. Also the caps (`MAX_VERSIONS`, `MAX_CONTENT_BYTES`, `MAX_COMMENTS_PER_ARTIFACT`, `MAX_EVENTS_PER_ARTIFACT`, `MAX_AUTO_WIDGET_ARTIFACTS`), the clock (`_now_iso`), the `slug_is_well_formed` predicate and the `get_default_store` / `get_default_folder_store` singletons |
| `kiro_crew.artifact_store.model` | The error hierarchy, the `EXPECT_ABSENT` generation sentinel and the record dataclasses (`Artifact`, `ArtifactPublication`, `ForkMetadata`, `ArtifactComment`, `ImageMetadata`) |
| `kiro_crew.artifact_store.rules` | Field limits and grammar (slug, tag, name, description, `source_path`), `slugify`, the kind policy (`_infer_kind`, `detect_editor_kind`, `USER_SELECTABLE_KINDS`), the theme-colour lint, the document-path test and session-scope matching |
| `kiro_crew.artifact_store.images` | The raster mime allowlist and the standard-library header sniffers |
| `kiro_crew.artifact_store.records` | The persisted formats: `meta.json` and its tolerant load, the lifecycle event entries (`ALLOWED_EVENT_TYPES`), `comments.json`, and the publication and fork-metadata field allowlists |
| `kiro_crew.artifact_store.comments` | The comment-thread rules: the forwarding filter, whole-thread cap pruning, the provider merge, the anchor rescan and root-cascade removal |
| `kiro_crew.artifact_store.folders` | `ArtifactFolderStore` and `artifact_folders.json` |
| `dashboard/handlers/artifacts.py` | The HTTP projection: request parsing, the restricted-session gate, SEL audit, response redaction (`_serialize`), the publish governance gates and the live-refresh broadcast |
| `kiro_crew.mcp_tools.artifacts` | The MCP projection: tool schemas and handlers, which reach the artifact store only through the HTTP API |

No module under `kiro_crew.artifact_store` imports `kiro_crew.artifacts` at import
time (`folders` names `ArtifactStore` for type checking only), and none performs
networking or redaction or touches the filesystem except `ArtifactFolderStore` on
its own file. `rules` imports `kiro_crew.history` and `kiro_crew.messaging.link`
inside `_strip_session_scope` because both import the facade back. The moved
error classes (`ArtifactError` and its five subclasses) keep `kiro_crew.artifacts`
as their `__module__`, so tracebacks and the error types in logs are unchanged;
the cost is that `inspect.getsource` cannot find them through that name and
raises `OSError`, and `inspect.getfile` names `artifacts.py` rather than the owner
file that defines them. Every other moved class -- the record
dataclasses, the class of `EXPECT_ABSENT` and `ArtifactFolderStore` -- reports
the owner module that defines it, so the source lookup and a debugger find its
definition.

The store's seams belong to the facade, which hands them to the owners at call
time, so they are patched on `kiro_crew.artifacts`: `config_dir`, `_now_iso`,
`MAX_VERSIONS`, `MAX_CONTENT_BYTES`, `MAX_COMMENTS_PER_ARTIFACT`,
`MAX_EVENTS_PER_ARTIFACT`, the fence helpers (`_open_pinned_for_read`,
`canonical_path_refusal`, `sensitive_path_refusal`, `is_sensitive_path`) and the
`_default_store` / `_default_folder_store` singletons. `get_default_folder_store`
builds its store on the facade's `config_dir`, so the default
`artifact_folders.json` follows the same patch as the default store's root.

The store calls every owner helper through its facade name, so rebinding one on
`kiro_crew.artifacts` steers the store: `slugify`, `_validate_slug`,
`_validate_name`, `_validate_description`, `_validate_tags`, `_validate_kind`,
`_validate_source`, `_validate_source_path`, `_infer_kind`, `detect_editor_kind`,
`_markdown_misclassification_reason`, `_session_touched` and
`_sniff_image_dimensions`. `slug_is_well_formed` lives in the facade on the same
`_validate_slug` binding, so it cannot disagree with the store. A call a helper
makes inside its owner module resolves there: `_session_touched` calls `rules`'
`_strip_session_scope`, `slugify` calls `rules`' `slug_hash_fallback`, and
`_sniff_image_dimensions` calls `images`' per-format sniffers
(`_sniff_jpeg_dimensions`, `_sniff_webp_dimensions`). Owner modules bind their
own imports too: a directly constructed `ArtifactFolderStore` takes its default
path from `folders`' `config_dir` and logs through `folders`' `logger`, which is
the same `kiro_crew.artifacts` logger object.

Rule data an owner's own code reads has one binding, in the owner, and the
facade forwards it instead of holding a copy: `_EXPORTS` maps each such name to
its owner, the module `__getattr__` answers a read from the owner in
`sys.modules`, and the module's class sends a write or a delete to the owner. A
patch through `kiro_crew.artifacts` (`monkeypatch`, or `mock.patch` without
`create`) and a patch of the owner module therefore both reach every reader that
looks the name up on the owner or the facade when it runs (a module that imports a
forwarded name by name keeps its own copy, as `code_review_sage`'s report module
does with `_SLUG_RE`), and the store reads these names through the owner module too (`create_image`
truncates to `MAX_NAME_LEN` / `MAX_DESCRIPTION_LEN`, and `update` pre-checks
`ALLOWED_EVENT_TYPES`). That covers the field limits and grammar
(`MAX_NAME_LEN`, `MAX_DESCRIPTION_LEN`, `MAX_TAGS`, `MAX_TAG_LEN`,
`MAX_SOURCE_PATH_LEN`, `_SLUG_RE`, `_SLUG_NORMALIZE_RE`), the kind sets and inference tables
(`ALLOWED_KINDS`, `ALLOWED_SOURCES`, `DOC_EXTENSIONS`, `_EXT_KIND_MAP`,
`_HTML_SNIFF_MARKERS`, `_MD_HEADING_RE`, `_SVG_ROOT_RE`), the theme-colour lint
patterns (`_HARDCODED_COLOR_RE`, `_HREF_ATTR_RE`) and `_strip_session_scope` in
`rules`, the event-type vocabulary (`ALLOWED_EVENT_TYPES`) in `records`, the
folder limits (`FOLDER_PATH_SEP`, `MAX_FOLDER_DEPTH`, `_NO_GENERATIONS`) in
`folders`, and the per-format sniffers (`_sniff_jpeg_dimensions`,
`_sniff_webp_dimensions`) in `images`. The moved classes stay ordinary bindings
of the facade, since a class is never rebound and the store's string annotations
resolve through them. The forwarding `__getattr__` is hidden from type checkers,
which see each forwarded name through a `TYPE_CHECKING` import from its owner.
`mock.patch(..., create=True)` on a forwarded name is not supported, because its
exit deletes the owner's binding, and `test_artifacts_refactor_create_guard`
refuses one anywhere in the test trees. `_IMAGE_MIME_EXT` is read only by the
store, so its facade binding is the live one. `MAX_AUTO_WIDGET_ARTIFACTS` is the
default argument of `prune_auto_widgets`, bound when the class is defined.

`test_artifacts_refactor_store_contract` derives, from the tests themselves, every
name a test rebinds on `kiro_crew.artifacts`, and fails when one is neither
forwarded nor held by the facade alone with no owner function reading its own
binding of it. Its one listed exception is `config_dir`, which `folders` reads for
a directly constructed `ArtifactFolderStore`.

`list()` returns newest first on a TOTAL order, `(updated_at, slug)` descending.
The tie-break is load-bearing, not cosmetic: `updated_at` is microsecond ISO, so
two artifacts written inside one microsecond carry the identical stamp, and
sorting on it alone is a stable sort over equal keys that preserves directory
scan order — which differs per platform and per filesystem, making the library
UI, the MCP list tool and the auto-widget pruning sweep disagree about which
artifact is newest on otherwise identical data.

### Kind inference

`store.create()` (and every path that funnels through it — the HTTP create
route, the `artifact_save` MCP tool, the `kirocrew artifact save` CLI) infers
`kind` when the caller omits it (`kind=None`), via
`_infer_kind(content, source_path, explicit)` in `kiro_crew.artifact_store.rules`:

1. **Explicit wins** — a non-empty `kind` argument is used as-is (back-compat).
2. **Extension** — for file-backed artifacts (`source_path` set): `.md` /
   `.markdown` → `markdown`, `.html` / `.htm` → `html`, `.svg` → `svg`,
   `.json` → `json`, `.txt` → `text`, any other extension → `text`.
3. **Content sniff** — for inline content with no `source_path`: HTML-ish
   markup (`<div`, `<span`, `<style`, `<table`, `<mcwidget`, `<html`,
   `<!doctype html`) → `widget`; an empty body → `widget`; a leading markdown
   heading (`#`…`######`) or non-empty content with **no** `<` at all →
   `markdown`; otherwise the legacy `widget` default (ambiguous blobs keep prior
   behavior).

Only `widget` and `markdown` are inferred from inline content; the richer
kinds need the extension signal. This is the safety prerequisite that lets
agents save markdown deliverables without the mis-save footgun (a markdown
doc stored as `widget` renders as raw inner HTML).

### MCP tools (`@kirocrew-core/*`)

| Tool | Purpose |
|---|---|
| `artifact_save` | Create a new artifact, returns slug; optional `folder` (id or `/`-separated human path, mkdir -p) files it in one call |
| `artifact_get` | Read content + metadata (optionally a specific version) |
| `artifact_update` | Modify content/name/description/tags; bumps version on content change |
| `artifact_list` | List artifacts (filter by `tag`, `kind`, name `q`) |
| `artifact_versions` | List version numbers for a slug |
| `artifact_revert` | Revert the live state to a prior version; writes that version's content as a fresh snapshot tagged `reverted`, so the activity timeline shows the rollback |
| `artifact_delete` | Permanent delete (artifact + all versions) |
| `artifact_folder_list` | List the folder tree (id, name, parent_id, path, item_count) |
| `artifact_folder_create` | Create a folder; `parent` = id or path (mkdir -p) |
| `artifact_folder_rename` | Rename a folder (id or path) |
| `artifact_folder_move` | Reparent a folder; cycle-guarded |
| `artifact_folder_delete` | Delete a folder; default keeps contents (re-parent), `delete_contents=true` cascades |
| `artifact_move` | Move an artifact into a folder / unfile it (metadata-only, no version bump) |
| `artifact_get_comments` | Read all comments on an artifact (local + provider-synced); `exclude_resolved=true` omits resolved threads (root-granular) so a mid-review read is not re-handed feedback already addressed |
| `artifact_post_comment` | Post a comment; agent comments carry the structured `is_agent` flag (no emoji stamped into the body — dashboard renders a lucide `Bot` icon, CLI prefixes a plain-text `[agent]` marker) + SEL-audited; `scope='shared'` syncs to the provider |
| `artifact_mark_review` | Advance a comment thread to REVIEW status (agent can mark_review but NEVER resolve) |
| `artifact_reply_comment` | Reply to an existing comment thread; a reply to a provider-origin parent posts back to the provider |
| `artifact_delete_comment` | Delete a fully-applied comment thread (root cascades to replies); provider-synced comments refused; SEL-audited with a `reason` |

Schemas live in `validation.py` (`ARTIFACT_*_SCHEMA`) and are registered in
`MCP_CORE_SCHEMAS`. The MCP tool layer always proxies through the HTTP API so
SEL audit, restricted-session enforcement, and any future authorization
middleware live in one place. The `tags` and `tag` arguments are checked by the
store's own tag rule (`rules.normalize_tag`, called from the schemas' custom
validator) rather than by a pattern of their own, so the tool and the store
cannot disagree about a tag; the schema fields keep only the count and length
caps.

### CLI (`kirocrew artifact`)

```
kirocrew artifact list [--tag T] [--kind K] [--q SUBSTR]
kirocrew artifact show <slug> [--version N] [--meta]
kirocrew artifact save --name N [--slug S] [--kind K] [--content C | --content-file F] [--tags A,B] [--description D]
kirocrew artifact update <slug> [--content C | --content-file F] [--name N] [--description D] [--tags A,B]
kirocrew artifact versions <slug>
kirocrew artifact delete <slug>
```

The CLI proxies through the gateway HTTP API (matches `kirocrew learn`).

### HTTP

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/artifacts` | `?tag&kind&q` filters + `?folder=` scoping (absent = all; empty = unfiled/root; id = that folder) + `?session=` scoping (same absent/empty distinction; validated like `origin_session_key`) + `?pinned=` (tri-state — unrecognized values don't scope); returns `{artifacts: […]}` |
| `POST` | `/api/artifacts` | JSON body — creates, returns full artifact + content; optional `folder` key (id or human path, mkdir -p) |
| `GET` | `/api/artifacts/{slug}` | Returns full artifact + content |
| `PATCH` | `/api/artifacts/{slug}` | Partial update; MCP-authenticated content updates snapshot by default, dashboard saves snapshot only with `snapshot: true`; optional `folder` key is metadata-only |
| `DELETE` | `/api/artifacts/{slug}` | Permanent delete |
| `PATCH` | `/api/artifacts/{slug}/pin` | Star/unstar — body `{pinned: bool}` (strictly boolean; non-booleans rejected). Metadata-only, no version bump |
| `PATCH` | `/api/artifacts/{slug}/relocate` | Point a file-backed artifact at a validated `source_path`; dashboard HTTP surface only (the `artifact_move` MCP tool moves folders instead) |
| `GET` | `/api/artifacts/session-docs` | Virtual, read-only list of non-code documents produced across chat sessions (the "All" firehose). `?session=<slot>` scopes to one session. Creates nothing; each entry carries `saved` (pinned) + `slug`. Registered before the `/{slug}` dynamic route |
| `POST` | `/api/artifacts/materialize` | Turn a recorded chat document into a real, pinned file-backed artifact — body `{path}`. The path MUST be a document recorded in chat `file_changes` (authorization allowlist); the read goes through `hooks.safe_read_file_bytes` (is_sensitive_path + `O_NOFOLLOW` + `MAX_FILE_BYTES` cap). Idempotent by `source_path` |
| `GET` | `/api/artifacts/{slug}/versions` | `{slug, versions: [int]}` |
| `GET` | `/api/artifacts/{slug}/versions/{n}` | Specific version content |
| `GET` | `/api/artifact-folders` | Folder tree with `item_count` + breadcrumb `path` |
| `POST` | `/api/artifact-folders` | Create folder `{name, parent?\|parent_id?, color?}`; spawns background emoji-icon task |
| `PATCH` | `/api/artifact-folders/{id}` | Rename / reparent / reorder / icon / color |
| `DELETE` | `/api/artifact-folders/{id}` | `?delete_contents=` picks keep (re-parent, default) vs cascade (delete subtree incl. artifacts) |
| `PATCH` | `/api/artifacts/{slug}/folder` | Move an artifact into a folder (`{folder}` id/path or `{folder_id}` id-only) |
| `POST` | `/api/artifacts/{slug}/publish/reprobe-notice` | Re-ask the destination whether a delivery notice still applies (`publish_sync.reprobe_notice`) and clear it once the copy is genuinely being served. READ-ONLY at the destination: it resolves through the drive's read path, never the create-capable one, so a re-check cannot provision infrastructure or rewrite a bucket policy |
| `POST` | `/api/artifacts/{slug}/pull-latest` | Pull the tracked upstream (`?source=publication\|origin\|auto`) into a NEW local snapshot via `publish_sync.pull_upstream`; ungated ingress |
| `GET` | `/api/artifacts/{slug}/upstream-status` | Cheap metadata-only drift check (`publish_sync.upstream_status`); best-effort, never blocks on the network |
| `POST` | `/api/artifacts/{slug}/overwrite-remote` | Force-push local content over an upstream-ahead remote (`publish_sync.overwrite_upstream`); **egress — gated by `_publish_governance_denied` on the resolved `publication.provider`** |
| `GET` | `/api/remote-artifacts/{provider}/browse` | Provider-routed discovery: `?q=` → `search_remote`, else `list_remote(?scope=mine\|shared\|public)`; rows annotated with `local_slug`; unregistered provider → 404; registered but unavailable → 503 |
| `POST` | `/api/remote-artifacts/{provider}/clone` | Bidirectional clone (`publish_sync.clone_from_remote`, sets `auto_sync=True` → arms future pushes); **gated by `_publish_governance_denied` on the routed provider**; empty registry → 503. Body: `{ "external_id": ... }` (provider-native ids can contain `/`, which a path segment can't carry) |
| `POST` | `/api/remote-artifacts/{provider}/fork` | Independent copy with pull-only `fork_metadata` lineage (`publish_sync.fork_from_remote`); ungated ingress; empty registry → 503. Body: `{ "external_id": ... }` |
| `GET` | `/api/remote-artifacts/{provider}/{external_id}` | Read-only detail fetch (metadata + content) for a provider-hosted artifact the user has no local copy of — content source for the remote-detail viewer; ungated ingress; passes `_redact_remote_response`; unregistered provider → 404; provider failure → 502 |
| `GET` | `/api/remote-artifacts/{provider}/{external_id}/comments` | List comments on a provider-hosted artifact (`fetch_comments`, `COMMENTS_READ`); TTL-cached in memory; provider failure surfaces as `remote_sync_error`, not a 500; ungated ingress; anchor/body redacted per comment |
| `POST` | `/api/remote-artifacts/{provider}/{external_id}/comments` | Post a top-level comment straight through to the provider (`post_comment`, `COMMENTS_WRITE`, scope=shared); **egress — gated by `_publish_governance_denied` on the routed provider** |
| `POST` | `/api/remote-artifacts/{provider}/{external_id}/comments/{comment_id}/reply` | Reply to a provider thread (`reply_comment`); **egress — gated by `_publish_governance_denied`** |
| `POST` | `/api/remote-artifacts/{provider}/{external_id}/comments/{comment_id}/review` | Advance a provider thread to REVIEW (`mark_review`); **egress — gated by `_publish_governance_denied`** |
| `DELETE` | `/api/remote-artifacts/{provider}/{external_id}/comments/{comment_id}` | Delete a provider comment (`delete_comment`); **egress — gated by `_publish_governance_denied`** |

Detail and comment operations carry `external_id` (and `comment_id`) in path
segments, so the browse/detail ids used there must be slash-free; aiohttp decodes
an encoded slash before route matching. Clone and fork keep `external_id` in the
JSON body specifically so provider-native ids containing `/` round-trip safely.

POST/PATCH/DELETE require an unrestricted session. The HTTP body envelope is
capped at 2 MiB; the store enforces a per-content cap of 25 MiB
(`artifacts.MAX_CONTENT_BYTES`), large enough for cloned/pulled rich artifacts
(HTML reports, CSVs). The number is owned by `constants.ARTIFACT_MAX_CONTENT_BYTES`;
`artifacts.MAX_CONTENT_BYTES` and the MCP save/update field cap
(`validation.ARTIFACT_CONTENT_MAX`) are both that name, so the tool and store
paths never disagree. It lives in the `constants` leaf rather than in `artifacts`
because `validation` importing `artifacts` closed the cycle `artifacts -> hooks
-> webhooks -> validation -> artifacts`, which raised ImportError in any process
whose first `kiro_crew` import reached `artifacts` before `validation`;
`test_agent_import_hoist.py` pins that `validation` never imports `artifacts`.

**Folders:** `Artifact.folder_id` (`""` = unfiled) is an opaque,
rename-safe membership id, tolerant-loaded for legacy meta.json.
`ArtifactStore.set_folder()` is a metadata-only move (NO version bump);
`list(folder=)` filters (None = all, `""` = unfiled, id = that folder).
`ArtifactFolderStore` (`kiro_crew.artifact_store.folders`) keeps a flat `parent_id` tree in
`~/.kiro/crew/artifact_folders.json` — create/rename/reparent (cycle- and
depth-guarded, `MAX_FOLDER_DEPTH` 20)/reorder/delete, breadcrumb, item counts,
and id-or-path resolution with mkdir -p semantics (`resolve_path`, all-or-nothing
rollback). Folder delete is an explicit choice: keep (re-parent direct children
to the parent) vs cascade (permanently delete the whole subtree, incl.
descendant artifacts) — never silent.

**Auth note (fork adaptation):** `"/api/artifact-folders"` is registered in
`token_auth`'s `mixed_internal_paths` in `server.py` — the 5 folder MCP tools
authenticate via `X-Internal-Secret`, and the prefix matcher
(`path == p or path.startswith(p + "/")`) does NOT cover the hyphenated path
via the `"/api/artifacts"` entry. Guarded by a regression test in
`test_artifact_folder_handlers.py`. `"/api/remote-artifacts"` is registered the
same way (same non-coverage reason; the prefix covers every
`/api/remote-artifacts/{provider}/...` sub-route) so `--slack-only` auth stays
at parity with the dashboard — guarded in `test_remote_artifacts.py`.

**Remote artifacts (provider-routed browse / clone / fork — G4).** The
`/api/remote-artifacts/{provider}/...` trio + the upstream sync trio
(`pull-latest` / `upstream-status` / `overwrite-remote`) wire `publish_sync`'s
provider-agnostic orchestration (`pull_upstream` / `clone_from_remote` /
`fork_from_remote` / `upstream_status` / `overwrite_upstream`) to HTTP. The
surface's reach depends on what the registered destination declares. The public
edition registers the personal cloud drive, which serves published bytes but
declares no `CONTENT_PULL` and an all-off `DiscoveryModel`, so browse / clone /
fork stay unavailable while publish works. It registers under its OWN key rather
than `DEFAULT_PROVIDER`, so it is **opt-in**: the picker lists it and a caller can
name it, but a publish that names no destination resolves the default key, which
stays unregistered, and gets the same 503 as an edition with no provider at all.
What holds the default back is not the transfer but a cross-store contract for
whether a publication exists — see the withdrawal rules below and
`personal_drive.PERSONAL_DRIVE_PROVIDER`; until that lands, the windows those rules
guard are reachable only by someone who chose this destination deliberately.

Because the drive pools **one** bucket and **one** distribution across every artifact,
those artifacts share a serving domain, and what keeps them from sharing a browser origin
is a response-headers policy carrying
`Content-Security-Policy: sandbox allow-scripts allow-popups; frame-ancestors 'none'`.
That policy is verified in two places, not one. At CREATE, a same-named policy is reused
only when its CSP matches exactly with `Override` true — anything else fails closed, so a
policy pre-created without the sandbox cannot be attached. At REUSE of an existing drive,
**every transition that starts serving bytes publicly** verifies the distribution still has
that policy attached to its default behaviour — a first publish and a private→public flip
alike, because those reach the drive through two different resolvers and gating the
guarantee on which one an artifact happened to travel through is not a guarantee. A
confirmed mismatch refuses and names the policy to re-attach; an unreadable distribution
config does **not** refuse, since an install whose IAM policy was narrowed would otherwise
have a permissions gap reported to it as tampering.

Three paths are deliberately **not** gated on it. Withdrawal and public→private are
removals: refusing them because a header is missing would strand the copy that header was
containing. Pushing a new version replaces bytes in place and hands out no new link, so if
the header were already gone that artifact was already served without isolation before the
push — refusing would un-expose nothing and would only strand its owner on the old bytes.

A publication's handle is `<key>~<account id>~<profile name>`, and the **account** is what
the withdrawal paths verify. An earlier shape pinned only the profile name, which was a real
exposure rather than a nicety: a profile name is a local alias, so repointing it at another
account and then deleting resolved the name to the new account, removed nothing, reported
success, and cleared the record — leaving the original copy public with the only handle able
to take it down erased. That needed no concurrency and no failure, just a config edit and an
ordinary delete. Every mutation path resolves its credentials through one function, which
compares the pinned account against what `sts:GetCallerIdentity` reports now and refuses on a
mismatch — and refuses too when the live account cannot be read at all, because that call is
gated by no IAM policy, so a failure means credentials did not resolve rather than a tier
being narrow, and acting on an account that cannot be identified risks removing nothing while
discarding the record. The account is read *before* the handle is minted, so a publish that
cannot confirm it fails before uploading. A handle carrying no account field predates the
binding and keeps the older behaviour rather than being refused; the middle field counts as
an account only if it is twelve digits, so a pre-binding handle whose profile name contains
the separator cannot have that name misread as an account.
 The
frontend gates the entire remote section + `UpstreamSyncBanner` on a non-empty
`GET /api/artifacts/publish-providers` result (zero remote pixels / requests
when no provider is registered at all). A companion registers its own provider
via the CPP publish seam. The picker includes a provider whenever
`available() or installable()`
(`PublishProvider.installable()` defaults `False`; a companion provider whose
`ensure_ready()` self-installs on first publish overrides it to `True`), and
each row carries an `available` flag so the FE can hint install-on-first-use for
a not-yet-installed but installable destination. Governance: `publish_sync` has NO internal gate and `push_version` is
ungated, so the two egress-arming routes go through
`_publish_governance_denied` (fail-closed `capabilities.publish ∩
destinations:<provider>`, a module-local alias for the shared
`publish_governance.publish_denied_reason` — the same decision the public-web
deploy path uses, see `governance.md`) BEFORE dispatch — `overwrite-remote` on the resolved
`publication.provider`, and `clone` on the routed provider (a clone sets
`auto_sync=True`, arming every future snapshot push). The four remote comment
WRITE routes (post / reply / review / delete) are outbound egress too, so each
goes through the same `_publish_governance_denied` gate BEFORE the provider call
— a denial is an audited 403 and no bytes leave the box (there is no local
mirror to fall back to, unlike the local shared-comment path). Fork and the
read-only routes (browse, upstream-status, pull-latest, remote-artifact detail,
remote comment list) stay ungated ingress. All remote
payloads pass `_redact_remote_response` (recursive credential/exfil-URL
redaction, depth-capped, `localPath` stripped). Browse rows are annotated with
`local_slug` BEFORE redaction (so a credential-shaped `external_id` isn't
rewritten out of the local-match lookup) using a single off-loop
`ArtifactStore.index_by_artifact_id` scan (not a per-row `find_by_artifact_id`
scan on the event loop) so the UI dedups already-local copies. Browse is
paginated: the response carries the provider's `next_page_token`, the client
forwards it as `?pageToken=`, and `RemoteBrowseSection` drives a
`useInfiniteQuery` with a "Load more" control — so remote artifacts past the
provider's first page are reachable rather than silently truncated.

### Dashboard pages

- `/artifacts` — list page (name / kind / tags / updated_at), tag filter,
  name substring search, click-through to detail
- `/artifacts/<slug>` — full-screen render of the current artifact in a
  sandboxed iframe (same security model as inline `<mcwidget>`), with a
  version dropdown

### Publish panel (`PublishHub`) — reading a publish outcome

`PublishHub` posts to the row's declared `endpoint` and must recognize **two**
response shapes, because two different routes answer that POST:

- `{url}` / `{public_url}` — the deploy shape (`POST /api/deploy/deploy`).
- a serialized artifact carrying a `publication` block — what `POST
  /api/artifacts/{slug}/publish` returns, which is where an app provider lands
  when it hands the confirmed publish to the core route (the supported way to
  reuse the core's single publish authorization + audit trail rather than
  growing a second one). The link, when the destination exposes one, is
  `publication.view_url`.

`readPublishOutcome` is that reader, and it returns an *outcome*, not a url:

- success is signalled by the return SHAPE, never inferred from a non-empty url
  — a destination may publish successfully and expose no browsable link, and
  conflating the two rendered a succeeded publish as the error branch with an
  undefined message (a bare red icon and no text);
- an `error` field wins over anything else in the same body;
- `publication: null` (an unpublished artifact) is not success;
- anything unrecognized is reported as a NAMED error (`unexpected_response`)
  rather than an empty one.

**HTTP 200 is not success on the artifact shape.** `publish_sync.publish()`
treats the version push as best-effort: its re-publish branch runs
`push_version(force=True)`, reads `refreshed.publication.last_error`, persists it
and returns normally — so the route answers 200 with a publication whose remote
content is stale. A non-empty (non-whitespace) `last_error` is therefore an error
outcome carrying the provider's own already-redacted message; whitespace-only
stays success, because the core writes `""` to clear the field. A publish that
SUCCEEDED but whose link is not usable yet (e.g. CloudFront still rolling out)
records that status on the separate `publication.notice` field, never
`last_error` — so it does not render the publish as failed or withhold the URL.

### Withdrawal is NOT best-effort

`publish_sync.unpublish` deletes at the destination and only then clears the local
`publication` block. A failure keeps the block and raises, because that block is the
only handle to content that may still be served: clearing it strands the remote copy
with nothing recording where it lives and no retry able to reach it. Both mouths fail
the same way -- a provider that raises, and a destination reporting
`reachable_for() == False`, which cannot attempt the withdrawal at all. Neither is
allowed to drop the handle: both refuse the delete instead.

Reachability is asked about the PUBLICATION, not the destination. `available()` answers
whether a destination is configured at all, which is the right question for offering a
new publish; a publication is bound to one specific account, recorded in its
`external_id`. Asking the wide question on a withdrawal path reports a destination as
reachable when this artifact's own account is gone, so the call is attempted, fails, and
is classified as a rejection a retry could fix -- leaving an artifact that can be neither
withdrawn nor deleted, only told to retry forever. Providers binding nothing per
publication inherit the wide answer.

Deleting the artifact notifies the destination, but it is NOT a way out of a kept
publication: it refuses unless the withdrawal is confirmed, exactly as described below.
`delete_for_artifact` runs from the delete handler BEFORE the local delete,
using the publication the handler already read for its version capture. The ordering is
chosen for its crash residue: die between the two steps in this order and the copy is
withdrawn while the artifact remains, which the user simply deletes again; die between
them in the reverse order and the local record is already gone while the content is still
public, with nothing left to withdraw it by. `delete()` removes the artifact directory BY
SLUG under the store lock, and a slug is a name the store re-mints identically once freed,
so placing a network round trip ahead of it DOES widen a window: a save landing there is
included in the delete the user asked for, but an artifact deleted and recreated under the
same title in that window takes the name back, and a removal by name alone would destroy
the newcomer. The handler therefore holds `publication_guard` across the round trip and
passes the generation and publication id it read into the removal, which is where the
comparison happens -- see the withdrawal rules below.

The local delete proceeds on ONE rule: only when there is nothing left to withdraw, or
the destination confirmed the withdrawal. Anything else refuses, loudly. A destination
that answers and then rejects the removal blocks the delete with `502`, keeping the
publication because that record is the only handle able to reach a copy that may still be
served. A destination that cannot be reached at all -- a publication naming a destination
this edition does not register, or one whose `reachable_for()` is False -- blocks it too:
unreachable describes THIS PROCESS's access, not the object, so the copy may still be
served to the whole internet, and deleting the record would erase the only thing that
could ever take it down. The one case that would proceed is a CONFIRMED absent destination, signalled by a typed
`DriveNotFound` -- never a substring match on an error message, because a throttled or
unauthorized reply carries the same words while the object is still live. **No site
raises that type today.** The personal drive resolves its destination by TAG, so a lookup
miss says only that the lookup failed: a tag removed by hand or a transient answer from
the tagging API both leave the bucket and distribution serving, and calling that proof
would strand a copy on evidence no stronger than the substring match the type replaced.
Proving absence needs a positive probe that asks the destination about the resource named
in the publication, rather than asking a directory whether it can still find it. Until
that exists nothing releases a handle except a confirmed withdrawal, so the branch is
unreachable and the refusal set is correspondingly wider. That is the safe side of the
trade: a record that will not clear is recoverable, and a public copy whose handle was
erased is not. `delete_for_artifact` never raises either way -- it
reports which of those happened and the handler decides, so provider resolution and the
availability probe failing on an unregistered destination cost a log line rather than an
exception.

The folder cascade obeys the same rule rather than routing around it. `delete_contents`
destroys artifacts through `ArtifactStore.delete`, which knows nothing about
publications, so the cascade withdraws every published copy in the subtree FIRST and
destroys nothing until they are all withdrawn; the first copy that will not come down
refuses the whole cascade, leaving the artifacts, their handles and the folder in place.

That preflight cannot be trusted on its own, because nothing holds a lock across it and
the destruction that follows: the folder tree and the artifact store have independent
locks, and taking both would invite an ordering deadlock. An artifact filed into the
subtree after the preflight enumerated it, or re-published between its withdrawal and its
removal, would otherwise reach the destruction still holding a publication nobody
withdrew.

The refusal is therefore asked of the delete itself rather than checked in the cascade
loop: `ArtifactStore.delete` takes an opt-in `refuse_if_published` flag and re-reads the
record inside the same lock as the removal, since a check in the loop would be a
check-then-act over a snapshot. That re-read is necessary and **not** sufficient on its
own, because the store lock is not the lock publication state is decided under. The
decisive one is `publish_sync.publication_guard`, the per-slug lock every path holds when
it reads whether an artifact has a live publication and then ACTS on that reading. The
cascade holds it across BOTH the withdrawal and the destruction of each artifact, so a
publish cannot land between them. It is taken in exactly three places -- `publish`, the
single-artifact delete route, and the folder-cascade route -- and `unpublish` is
deliberately not one of them: it clears a record after a network withdrawal while holding
no guard, so there the identity comparison below is the ONLY thing standing between it and
clearing a newcomer's handle. The registry backing the guard is refcounted: the count is
taken before the acquire so a waiter keeps the entry alive, and an entry drops only at zero
holders, where a later caller minting a fresh lock excludes nobody. That is what makes
guarding ANY well-formed slug affordable rather than only the ones the store could resolve,
which matters because a slug the store reads as empty is the only slug an artifact created
inside the delete's own window can occupy. A malformed slug still passes through unguarded
so the store answers `4xx`.

Membership in the guarded set names an ARTIFACT, never a name. A freed slug is re-minted
identically and creating an artifact takes no guard, so the cascade carries
`destroyable_generations`, a slug-to-generation map, and each door compares identity under
the lock it removes beneath: `expect_created_at` for the artifact's generation, and
`expect_publication_id` for the publication's own id, because `set_publication` replaces
only the publication block and leaves `created_at` untouched, so a re-publish of the same
artifact is invisible to the generation alone. Expected ABSENCE is its own value,
`EXPECT_ABSENT`, rather than `None`: `None` means there is no identity to compare and so
performs no check at all, which on the absent-slug path is precisely the newcomer the guard
was taken for. An artifact holding a slug the caller did not name therefore survives. The
map defaults to empty, so a caller naming no generations destroys nothing rather than
everything, and the response separates the reasons: `replaced_artifact_slugs` for one
replaced after the caller named it, `kept_published_artifact_slugs` for one still
published, `unguarded_artifact_slugs` for one that joined the subtree outside the guarded
set. The single-artifact door answers `409` on the same comparison.

The folder tree change is already committed by the time the cascade refuses and cannot be
rolled back, so a kept artifact survives with a dangling folder id and degrades to Unfiled,
which readers already tolerate; the response names it so a partly-refused cascade does not
read as a completed one.

`unpublish` is **not** a way out of a kept publication either, though it was designed as
one. It obeys the same absence rule as the delete path: a destination that refuses the
removal keeps the record and stays retryable; a destination that is merely unreachable is
refused before the attempt, and its message must not offer deleting the artifact as the
alternative, because that path refuses on the same destination. Only a confirmed-gone
destination --
typed `DriveNotFound` again, never message text -- releases the record, because there is
nothing left to withdraw. That branch is currently unreachable for the reason given
above: no site can prove absence yet. Its cost while unreachable is real and worth
naming, because `reachable_for` resolves the PROFILE, not the drive: a drive deleted
under a still-registered profile passes the reachability guard, fails inside the
provider, and is now refused rather than released -- so that artifact's record cannot be
cleared until a positive absence probe exists. Refusing is the deliberate choice over
releasing on a tag miss, which could erase the handle to a copy that is still served.

The cost is worth stating plainly: a published artifact whose destination refuses the
withdrawal, or which this process can no longer reach, cannot be deleted until that is
resolved -- and the unreachable half makes that population larger than a narrower reading
would. A profile holding the publish permissions but not `s3:DeleteObject` is exactly that
shape, and no shipped IAM tier grants it yet (see the publish-tier follow-up). The way out
is deliberate rather than accidental: `unpublish` lets the owner state in the open that
they accept the published copy may remain, and then the artifact deletes. The alternative
was dropping the record and stranding a world-readable copy with nothing able to withdraw
it -- failing loudly is recoverable, that is not.

This asymmetry with the publish path above is deliberate. A stale publish leaves the
wrong bytes at a URL the user already knows about; a stale withdrawal leaves content
served that the user believes they took down, which is the worse failure and the one
worth surfacing as an error the user can act on.

The public-exposure warning and the blocking `PublicPublishAckModal` are gated
on the selected destination's `public_reachable` descriptor field
(`PublishProvider.public_reachable`, class attribute, default `True`, carried
on each `GET /api/artifacts/publish-providers` row). A destination whose
published link is served with no authentication gets both, on the clean path
and on a scan override, exactly as before. A destination that declares `False`
-- one that stores content privately behind a login -- gets neither: the
confirm click publishes directly, because both surfaces say the content is
going onto the open internet, and a gate that lies where the destination is
private teaches the user to click past it where it is public. The publish flow
always requests `visibility: PUBLIC`, so `False` asserts that even a
publication the provider files as PUBLIC is served only to an authenticated
reader; a provider whose PUBLIC publications are readable by anyone must leave
it `True`. The default is
`True` and the frontend treats an omitted field as `True`, so a provider must
declare that it needs authentication; the failure mode of the wrong default is
a public link with no warning. App-registered rows from
`GET /api/publish-providers` are the public-web deploy surface and are always
treated as reachable.

## Widget auto-registration

**Every `<mcwidget>` the agent emits becomes an artifact automatically** — no
user gesture required. Registration happens on the backend when the assistant
segment is finalized (`chat_runner._flush_segment` →
`widget_artifacts.register_widgets_off_loop`), and the record is created
**unpinned**: it is a *record*, not a library entry. The star on a rendered
widget is therefore a pure `pinned` flip, not a create.

Why the backend and not `WidgetFrame` on mount: the chat list virtualizes, so a
message never scrolled into view never mounts its widgets. Frontend registration
would make an artifact's existence depend on whether a human happened to look at
it. Finalize-time registration covers every emitted widget exactly once and gets
the originating `session_key` for free — which is what lets the in-session
Artifacts tab list widgets at all (a widget's HTML is inline in the message and
never written to disk, so the file-backed session-docs scan cannot see it).

**Identity — a two-language contract.** The slug is derived from
`(message_ts, body)` — the parent message's timestamp plus a fingerprint of the
widget's body:

- `src/kiro_crew/widget_slug.py` → `derive_widget_body_slug`
- `website/src/lib/widgetSlug.ts` → `deriveWidgetBodySlug`

Both MUST produce identical output (seed `<ts>#w:<body>`, two FNV-1a passes,
32-bit prime, 16 hex chars); the frontend uses it to find the artifact the
backend wrote, with no id exchanged. Because the body is part of the key, a slug
hit implies the same body: a disagreement about *which* spans are widgets can
only cause a probe miss (the star shows unsaved), never a binding to the wrong
artifact. `widget_parse.parse_widgets` mirrors the frontend's `parseBlocks`
widget detection so both sides agree which spans are widgets and what their exact
bodies are. Parity is pinned by shared body-slug vectors in
`test/test_widget_slug.py` and `website/src/test/widgetSlug.test.ts`, and by
shared parser fixtures in `test/test_widget_parse.py` and
`website/src/test/widgetSlug.test.ts` — a change to one side fails the other.

Registration is **idempotent and non-destructive**: an existing slug is left
untouched (a replayed or rehydrated message never duplicates or clobbers content
the user has since iterated on), and a widget carrying an explicit `slug=`
attribute is skipped entirely, since re-emission names an existing artifact
rather than authoring a new one. Failures are logged, never raised — a lost
registration must not break the chat turn that produced the widget.

**Restricted sessions never register.** Incognito and temporary slots
(`slot.is_restricted`, i.e. `memory_mode != "persistent"`) are denied every
artifact write at the HTTP gate (`_is_restricted_session`), so
`_schedule_widget_registration` returns early for them. Without that check the
chat path would be a back door around the ceiling: widget HTML from a session the
user expected to leave no trace would persist under `artifacts/<slug>/` and appear
in the library. Both paths key off the same `slot.is_restricted` signal, so they
cannot drift apart.

**Retention.** Unclaimed auto-registered artifacts are pruned oldest-first past
`MAX_AUTO_WIDGET_ARTIFACTS` (200) by `ArtifactStore.prune_auto_widgets`, which
runs after each registration. Without it, a chat-heavy user accumulates one
three-file artifact directory per throwaway widget forever and every library
listing is an O(N) scan over them.

Because the sweep **deletes user-visible data**, eligibility
(`_is_sweepable_auto_widget`) is deliberately conservative: any sign of human or
agent investment exempts the record permanently. A record is swept only if it is
`auto_registered` **and** all of the following hold — not `pinned`, no
`folder_id` (never filed), no `publication` (never shared — a live URL points at
it), no `fork_metadata`, no `description`/`tags`, `updated_at == created_at`
(never edited), and no `comments.json` sidecar (never commented on). Explicit
saves are never swept at all. Merely *rendering* a widget is not a claim; touching
it in any of those ways is.

The comments check is a separate file stat because `add_comment` writes only the
sidecar — it never touches `meta.json`, so a commented artifact still looks
pristine to every metadata signal above, and the sweep would otherwise delete the
user's comments along with it.

The edit test is `updated_at == created_at`, **not** `version == 1`: `update()`
bumps `version` only when `snapshot=True`, so a non-snapshot dashboard or direct
store save can leave the version at 1 while rewriting the body. Keying on
the version would let the sweep delete freshly-iterated widgets. Conversely
`set_pinned` / `set_folder` deliberately don't touch `updated_at`, which is why
they are separate signals.

Ordering is newest-first on the same total `(updated_at, slug)` order used by
`list()`. The sweep re-sorts defensively so its destructive boundary does not
inherit an ordering assumption from the candidate source. The candidate snapshot
is taken unlocked, so eligibility is **re-checked and the directory removed in a single
lock acquisition** — otherwise a star landing mid-sweep would lose to a stale
verdict and silently delete an artifact the user had just claimed. Note the sweep
deliberately does NOT delegate to `delete()`: re-checking under the lock and then
calling a method that re-acquires it reopens the same window between the two
acquisitions, so the removal is inlined.

**Star semantics in `WidgetFrame`.** `exists` and `pinned` are separate states:
`{exists: true, pinned: false}` is the normal steady state, so the star renders
hollow (offering to save) while the title still links to `/artifacts/<slug>`.
Starring pins; if the artifact is absent (a pre-feature widget, a failed
registration, or one reclaimed by the sweep) it falls back to create + pin, and
tolerates 409 as "already there". Un-starring unpins only — the record and its
version history survive.

## Starred & Session Documents

The Artifacts page is a single unified, searchable table with two conceptual
inputs, distinguished by the leading **star** column:

- **Starred artifacts** — real, saved artifacts with `pinned=true`. The
  **Starred** view shows only these; the star toggles `pinned` via
  `PATCH /api/artifacts/{slug}/pin` (metadata-only, no version bump).
- **Session documents** — a *virtual* firehose of non-code documents the agent
  produced across chats (from message `file_changes`), surfaced only in the
  **All** view via `GET /api/artifacts/session-docs`. Clicking a row opens a
  **read-only preview** (`SessionDocPreview`) that fetches the file through the
  redacting `GET /api/file-read` endpoint — a pure read that registers nothing.
  A document recorded with a **relative path is refused client-side** (no
  request is sent): `resolve=1` would resolve it against the gateway's
  *current* project directory, not the project it was recorded under, so a
  project switch would silently read a same-named file from the wrong project.
  An unresolved **`~name` tilde form is refused the same way** — `expanduser`
  leaves an unknown account name unchanged and the backend then anchors it to
  the process CWD, the same wrong-project read; only the gateway user's own
  `~`/`~/…` (deterministic, project-independent) counts as absolute. The
  refusal renders as a **status, not an error** (nothing failed — the feature
  is declining an unsafe read), and the preview header's save button is
  **disabled in the refusal state**, mirroring the backend materialize
  allowlist, which only trusts paths absolute after expansion.
  Nothing is written to disk
  for these until the user stars one (from the row or from the preview
  header's labeled **Save to artifacts** button — named after the surface the
  save lands on, not the Apps "Library"), which **materializes** it into
  a real,
  pinned, file-backed artifact via `POST /api/artifacts/materialize`
  ("Virtual All + materialize-on-save"); a clean save is acknowledged by a
  transient status notice on the page (a colliding slug shows the collision
  banner instead). Search matches name/source (incl. the
  originating session title); the file-type filter applies to both inputs.

The page opens on the **All** view by default. The Starred/All selection is
persisted per-browser (`localStorage['mc-artifacts-pinned-only']`), so a user
who last chose **Starred** resumes there on their next visit.

The firehose reads lightweight history projections: each JSONL file is streamed
as bytes, lines without the serialized `"file_changes"` key are skipped without
JSON parsing, and only `ts` plus `meta.file_changes` are retained in a bounded
file-stamp cache. It never routes through the full parsed-message cache, so a
dashboard refetch cannot pin or repeatedly decode the complete transcript
corpus while looking for document paths.

Materialization is authorization-gated: the requested path must appear in the
recorded chat `file_changes` (never an arbitrary client path), and the read is
routed through the `hooks.safe_read_file_bytes` keystone. `source` is recorded
as `chat` for materialized documents.

### In-session Artifacts tab

The chat side panel's **Artifacts** tab (`SessionArtifactsTab`) shows everything
one session produced, merging the same two inputs scoped to that session:

1. **Real artifacts** via `GET /api/artifacts?session=<slot>` — including every
   auto-registered widget. Rows open `/artifacts/<slug>`.
2. **Session documents** via `GET /api/artifacts/session-docs?session=<slot>` —
   file-backed, and the only input with a path, so those rows open the file.

A materialized document is both, so artifact rows whose slug already appears in
the document list are dropped in favor of the path-aware row. The star means
"keep in library" for either: a document with no slug materializes, everything
else is a `pinned` flip.

`?session=` is validated through the same grammar as a save's
`origin_session_key`, but a validation **miss keeps the raw value** instead of
collapsing to `""`. Collapsing is correct for a *write* (attributing a save to no
session is safe) and wrong for a *read* filter: `""` is the real no-origin bucket,
so an unvalidatable key would return some **other** session's artifacts — notably
every `artifact_save` from the MCP path, which stores `session_key=""`. Since
`store.list` compares exactly, the raw value matches zero records: an honestly
empty tab rather than a foreign one. This is not only a hostile-input case — a slot
key can legitimately exceed the grammar's 128-char cap, because the artifact
companion-chat flow names a slot `Artifact: <name>` and names run to
`MAX_NAME_LEN` (200).

Like `?folder=`, absent means "don't scope" while present-but-empty means "only
unattributed" — the handler reads the raw key to keep the two distinct. `?pinned=`
is tri-state for the same reason: an unrecognized value does not scope rather than
being read as `false`.

**The session key is the BARE slot key** (`chat-<N>-<ts>`), never a decorated
`dashboard:<key>` form. `ArtifactStore.list` compares `session_key` exactly — it
does no prefix folding, unlike `_collect_session_docs`, which accepts either form.
All three writers must therefore agree on the bare key: widget auto-registration
(`_schedule_widget_registration`), `WidgetFrame`'s fallback create
(`origin_session_key: slotKey`), and materialization. A decorated key on any one
of them silently partitions artifacts into a bucket the tab never queries, with
every write-side unit test still green — so test the round-trip
(`list(session_key=slot.key)` finds it), not the stored string.

## Validation & Limits

The field limits and grammar are `kiro_crew.artifact_store.rules`; the content,
version and auto-widget caps are the store's, in `kiro_crew.artifacts`.

| Field | Limit |
|---|---|
| `slug` | regex `^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$`, ≤ 80 chars |
| `name` | ≤ 200 chars, non-empty |
| `description` | ≤ 2,000 chars |
| `tags` | ≤ 16 tags; each ≤ 64 code points of its NFC form (`MAX_TAG_LEN`), made of Unicode letters, the marks that attach to them (`Mn`/`Mc`, at most 4 on one character) and digits (`Nd`/`Nl`) plus `_`, `:`, `.`, `-`, opening with a letter or digit, every code point visible (no `Default_Ignorable_Code_Point`, no enclosing mark) and its own spelling (no compatibility form of other characters) (`normalize_tag`) |
| `content` | ≤ 25 MiB (`MAX_CONTENT_BYTES`) |
| `kind` | one of `widget` / `html` / `markdown` / `svg` / `json` / `text` / `image` / `webapp` |
| `source` | stored values: `chat` / `cron` / `subagent` / `manual` / `import` / `dashboard` / `slack` / `cli` / `task-runner` / `unknown`; the MCP save schema accepts the first five explicitly |
| `MAX_VERSIONS` | 50 (oldest pruned beyond cap) |
| `MAX_AUTO_WIDGET_ARTIFACTS` | 200 (oldest **unpinned auto-registered** widgets pruned beyond cap) |

A tag is a user-facing label that lives only in `meta.json` — never a file
name, a URL segment or a query identifier, which is the slug's job (`slugify`
keeps slugs ASCII by transliterating) — so its alphabet is the user's: `売上`,
`café`, `München` and `हिन्दी` are tags. Nonspacing and spacing marks (`Mn`,
`Mc`) are admitted because NFC leaves some standing beside their base letter
(Devanagari vowel signs, Thai tone marks) and without them whole scripts could
not be written, and a mark follows a letter, a digit or another mark, never a
separator (an accent on a hyphen is refused the way a leading mark is: it has
no base); enclosing marks (`Me`, the keycap that turns `1` into an emoji,
the enclosing circle) are refused with the symbols, and so are other numbers
(`No`: `½`, `²`, `①`, the numerals of scripts that do not count in decimal
digits), which are symbols drawn from a digit rather than digits. One character
carries at most four combining marks (`MAX_CONSECUTIVE_MARKS`): Hebrew pointing
and a Tibetan syllable stay within that, and `a` under 63 accents is a glyph
that overflows its chip, not writing. Everything else is refused
with a plain-English reason: punctuation other than the four separators, symbols
and emoji, and every control, format (zero-width, bidi override) and whitespace
character. A second test runs before the category test: every code point of the
Unicode `Default_Ignorable_Code_Point` property (`_DEFAULT_IGNORABLE`, the
published table, 4,174 code points) is refused as "renders as nothing", because
the property also holds letters and marks — the variation selectors, the
combining grapheme joiner, the Hangul fillers — that the category test would
admit although a reader cannot see them. Admitting one would give `ops` an
invisible twin `ops<VS16>` that `list(tag="ops")` misses, and would let a
credential planted in a tag (`AKIA<VS16>IOSFODNN7EXAMPLE`) pass the dashboard's
redactor while reading as the bare key; the MCP gate's sanitizer keeps these
marks (they are `Mn`, and emoji text needs them), so the tag rule is the check
that stops them. The table is hand-derived from Unicode 15.0, the version Python
3.12 ships; a test pins `unicodedata.unidata_version` to it, and on a runtime
with a newer Unicode (Python 3.13 ships 15.1) it is skipped with a reason that
names the regeneration step, since the table cannot be checked there. A third test, also
before the category test, refuses every code point whose NFKC form differs from
itself — a compatibility form of other characters: full-width `ｏｐｓ`,
mathematical `𝐨𝐩𝐬`, the `ﬁ` ligature, `µ`, `①`, `Ⅻ` — naming the plain
spelling to write instead, because NFC leaves these alone and each would be a
second stored tag with the look of `ops`, `fi`, `μ`, `1` or `XII`. Two letters
are kept as typed by decision: THAI CHARACTER SARA AM and LAO VOWEL SIGN AM
decompose for compatibility into NIKHAHIT + SARA AA, yet the composed form is
what every Thai and Lao keyboard types (`น้ำ`, water, is written with U+0E33);
they are the only tag-admissible letters whose `<compat>` decomposition opens
with a combining mark, and a test pins the allowlist to exactly them. The
outcome is that a tag is always visible and has one unambiguous spelling, with
two stated residuals the rule does not chase: a cross-script confusable such as
Cyrillic `орѕ` beside Latin `ops`, and the decomposed twin `นํา` of the kept SARA
AM. The confusable is not a compatibility form -- NFKC leaves it, so no
normalization rule tells the two apart -- and telling them apart is a
confusables check (UTS #39 skeletons compared against the tags already stored,
plus its script-mixing restriction, under which a Latin-with-Han-and-Kana tag
such as `日本語-api` is allowed and `орѕ` beside `ops` is caught), a different
mechanism from the spelling rule this change is about and left out of it rather
than half-done. The store writes the NFC form, two spellings of one
label dedupe to one tag, and `list(tag=…)` reads its filter through the same
rule, so a label matches however it was typed and a filter that is not a tag
matches nothing. ASCII tags are unchanged by all of this. The widening is a
one-way door: once non-ASCII tags are stored in `meta.json`, reverting this
rule or narrowing it later strands them — an update that re-sends such a tag is
refused and `list(tag=…)` cannot reach it — so a later narrowing ships with a
migration that rewrites or drops the stranded tags first.

## Security

- **Path traversal** — slugs are regex-validated; the store resolves every
  path and refuses any that escape the artifact root.
- **Sensitive paths** — every read and write goes through
  the sensitive-path fence. The store's own file helpers (`_read_text` /
  `_write_text` / `_read_bytes` / `_write_bytes`) canonicalise the path with
  `os.path.realpath` and ask `security.canonical_path_refusal()` (through
  `_fence_refusal`), the reason-or-None form of the shared entry point for a
  caller-canonicalised path: it answers with `security.sensitive_path_refusal()`
  on the event loop and with
  `security.is_sensitive_resolved_path()` off it, so a caller earns the
  off-pool gate by offloading, never by declaring anything; `GET
  /api/artifacts` runs `store.list()` on a worker for that reason. The two read
  helpers then open through `pinned_fs.open_fenced_for_read` (bound as
  `_open_pinned_for_read`): a link at the final name is refused, the inode must
  be a regular file with one link, and the fence judges the kernel's path for
  the opened inode whenever it differs from the path already judged. The root
  check asks `security.sensitive_path_refusal()`: the store refuses to
  instantiate at any sensitive root, and a resolver stall is refused like a
  match but raised with the producer's own "could not be verified" wording.
  The file-backed `source_path` pointers stay on the bounded
  `security.is_sensitive_path()` and fall back to the snapshot silently.
- **Relocate root confinement** — `PATCH /api/artifacts/{slug}/relocate`
  points a file-backed artifact at a `source_path`; a later GET reads
  that file, so an unconfined relocate would be an agent-reachable
  arbitrary-local-file read primitive. The target is therefore confined to the
  user's home dir by default (an operator can widen to additional absolute roots
  via `publish.relocate_roots`); the resolved path must be `is_relative_to` an
  allowed root (a `..` guard runs first, and the `is_sensitive_path` denylist
  still applies inside every root). The `is_relative_to` barrier is also the
  sanitizer CodeQL's path-injection tracker requires.
- **Restricted sessions** — POST/PATCH/DELETE are denied when the dashboard
  classifies the session as restricted (`_is_restricted_session`).
- **SEL audit** — every mutation emits a `log_tool_invocation` event from the
  HTTP layer (`dashboard/handlers/artifacts.py`). Reads are not audited.
  `_audit` redacts caller-supplied text before it reaches the SEL writer (which
  signs bytes as-written and does NOT redact): the `error` string and every
  string leaf of `extra` metadata pass through `redact_via_context`, so an
  upstream provider exception carrying a credential/signed URL — or a
  provider-controlled `external_id` echoed into `extra` on the remote
  browse/clone/fork/pull/overwrite error paths — cannot leak into the audit log.
  Routing through the platform-seam shim (not the bare `_redact_text`) means a
  loaded companion's extra credential/cookie regexes apply to the audit trail.
- **Atomic writes** — `_write_text()` writes to a `.tmp` sibling and renames,
  so a crash mid-write cannot corrupt `current.html` or `meta.json`.
- **Tolerant load** — `ArtifactStore._read_meta_file()` hands the parsed file to
  `decode_meta` in `kiro_crew.artifact_store.records`, which ignores unknown keys
  and supplies defaults for missing keys, so future schema additions don't break
  existing files.
- **Frontend rendering** — artifact bodies are rendered in the same sandboxed
  iframe that powers `<mcwidget>`, and that frame loads a **real document** from
  `GET /sandbox-doc/{doc_id}/{tok}` rather than a browser-built `blob:` URL. A
  blob URL is refused outright by some WebKit-based in-app browsers (which can
  take the whole page down with it) and a sandboxed `srcdoc` frame blank-renders
  on WebKit, so a plain document URL is the only form that loads everywhere. The
  authed `POST /api/sandbox-doc` stashes the html the caller already holds and
  returns the URL; the path credential is client-bound with a short TTL, and the
  response pins `Content-Security-Policy: sandbox` so the document keeps an
  opaque origin even when opened top-level. Flags match what the embedding frame
  already grants, so nothing is newly permitted or denied. The same response's
  `frame-ancestors` is **`'self'` plus the ancestors `'self'` cannot express**
  (`origin.frame_ancestors_value`), and both halves are load-bearing. `'self'` is
  resolved by the BROWSER against the frame's real URL, so it stays correct behind
  a TLS-terminating tunnel that rewrites `Host` and may not forward
  `X-Forwarded-Proto` — deriving the origin server-side instead names
  `http://localhost:<port>` while the browser is on `https://<tunnel-host>` and
  blanks every frame on that path. `'self'` alone is not enough because the
  directive is matched against EVERY ancestor: the Instances embed nests a remote
  dashboard inside the local one, so a widget sits three levels down (local
  dashboard → embedded dashboard → widget) with a grandparent on a different
  origin, and the browser refuses the embed while the `GET` still returns 200.
  Those extra ancestors come from `server._extra_frame_ancestors` (the embedding
  parent's port, from a validly-signed token claim) and each is re-validated
  against a strict origin form, so a malformed or inexpressible source — a
  bracketed IPv6 literal is not a valid CSP host-source — cannot reach the header.
  See `dashboard/handlers/sandbox_doc.py`. No `dangerouslySetInnerHTML` without
  DOMPurify; no inline event handlers.
- **The detail frame is sized to its document, and promoted to its own
  compositing layer.** Both are corrections to shapes that only misbehave on iOS
  WebKit, so both are invisible in a desktop dev loop and both are pinned by
  tests in `website/src/test/ArtifactBody.iframeBlob.test.tsx`.
  - The frame takes the height its document reports (`includeHeightReporter`, the
    same `mc-widget-height` protocol the chat frame uses, in its own measured-height
    key space) and carries **no minimum**. It previously stood in a fixed
    `calc(100vh - 240px)` box with `minHeight: 480`, which on a phone put a short
    artifact in a frame hundreds of pixels taller than itself and made the reader
    scroll a pane inside a scrolling page. A floor reintroduced above the reported
    height brings both back.
  - `transform: translateZ(0)` is on the frame because iOS WebKit was measured
    **skipping the document's first paint**: it loaded, its injected scripts ran,
    it reported a correct layout height, and it sat in a correctly sized visible
    frame while painting nothing. Four unrelated post-load invalidations each
    made it appear (a 1px resize, a transform toggle, an opacity flip, a display
    toggle), so promotion is the one remedy that needs no timing — anything
    scheduled off the `load` event is a race on a slow connection. Content height
    only ever *correlated* with the symptom because tall content happened to
    trigger one of those invalidations.
- **A frame showing something that is not ours offers a retry.** The document URL
  is single-use, so a navigation the ENGINE starts on its own (memory pressure, a
  back/forward cache eviction) re-requests a spent URL and lands the frame on a
  404 page — which fires `load` like any other navigation and leaves a silent
  empty box, while the failure notice stays hidden because the mint itself
  succeeded. Every document this surface builds carries the height reporter, so
  silence past a grace window is the signal to surface the existing retry (which
  re-mints; re-pointing at the spent URL recovers nothing). Deliberately not an
  automatic re-mint: a second `load` also happens when a link inside an artifact
  navigates the frame, and silently pulling the reader back would fight them.
  `/sandbox-doc/` is on the service worker's skip list for the same single-use
  reason, and because an iframe navigation has `mode === 'navigate'`, so the
  worker's offline fallback would otherwise serve the SPA shell INTO the widget
  frame (`website/src/test/serviceWorkerSkipRules.test.ts` runs the real worker).

## Versioning

Each `create()` writes the initial content to `current.html` and snapshots
it as `versions/v1.html`. `update(slug, content=…, snapshot=False)` updates the
live state without adding a numbered version; `snapshot=True` also increments
`version` and writes `versions/v{N}.html`. The MCP `artifact_update` path defaults
to snapshots, while dashboard Save does not unless it sends `snapshot: true`.
Older versions remain untouched until the prune cap is reached, so any retained
version can be read via `get(slug, version=N)` or restored as a fresh snapshot by
`artifact_revert`.

`list_versions(slug)` returns the sorted set of stored version numbers.
`get(slug, version=N)` reads a specific version. After pruning, lower-numbered
versions may be unavailable; callers must handle `ArtifactNotFoundError` for
out-of-range versions.

## Comments & Lifecycle

Comments live in a per-artifact `comments.json` sidecar (`ArtifactComment`
dataclass; threads are one level deep — replies carry the root's id as
`thread_id`). The file format is `kiro_crew.artifact_store.records`
(`decode_comments` / `encode_comments`); the thread rules — the forwarding filter,
the whole-thread retention cap, the provider merge, the anchor rescan and the
root-cascade delete — are `kiro_crew.artifact_store.comments`; `ArtifactStore`
holds the lock and does the IO. `status` is `open | review | resolved`; `sync_state` tracks
provider push status (`local_only | pending_push | synced | push_failed`).
Provider push/reconcile itself is companion-edition-only behavior behind the
CPP publish seam — the open-source core carries the `sync_state` field and
enforces the provider-origin guards, but ships no remote reconcile loop.

**Inbound comment sync (fetch-on-view).** `GET /api/artifacts/{slug}/comments`
opportunistically pulls the provider's comments (`fetch_comments`, when the
publication provider advertises `COMMENTS_READ`) and reconciles them into the
local mirror via `ArtifactStore.merge_remote_comments(slug, provider, comments)`
before returning. Each merged mirror carries `target_provider`/`target_external_id`
(the publication's provider + artifact id) so a later local edit/review/delete of
that comment routes back to the source — the write handlers gate on
`target_external_id` before calling the provider, so without it those mutations
would silently stay local and be resurrected on the next fetch. The provider is
authoritative for its own comments; the merge
drops mirrors that came back tombstoned (cascade-dropping a whole thread when its
ROOT is deleted upstream), syncs mutable fields (status/body/author) of changed
provider comments, adds newly-seen ones, and leaves `origin == "local"` comments
untouched — while keeping provider comments merely absent from one fetch (a
transient/paginated empty is not a delete). The fetch is network IO and the merge
is blocking filesystem IO, so both run off the event loop; any failure is
best-effort and surfaces as `remote_sync_error` rather than failing the list.
Every awaited remote publish-provider network call is bounded by
`_REMOTE_PROVIDER_TIMEOUT_S` (15s) via `asyncio.wait_for` (CWE-400): a timeout on
the primary read path (`remote_artifact_fetch`) maps to a **504**, while the
best-effort comment-sync paths degrade like any other provider failure
(`remote_sync_error`, local write still succeeds).
With no provider registered (the public default) `get_provider` raises and the
endpoint degrades to local-only comments. Comment `body`, `author`, **and** the
anchor `quote`/`prefix`/`suffix` are run through `_redact_text` (credential +
exfil-URL redaction) at every read boundary — the local list endpoint and the
remote-detail serializer (`_serialize_remote_comment`) — because
provider-controlled comments are merged into the mirror raw, so redaction cannot
live only on the local POST path.

**Agent disposition contract** (owner decision 2026-07-13; rubric ships in
the builtin `artifacts` skill):

- `artifact_delete_comment` (MCP) — for comments that were unambiguous
  directives, fully applied. Requires a `reason` (≤ 500 chars) recorded in
  the SEL audit and the activity feed. Root deletes cascade to replies.
- `artifact_mark_review` — for comments addressed with judgment; human
  verifies and resolves.
- Resolution stays human-only: the resolve endpoint returns 403 for any
  MCP-originated request (actor inferred from the `X-Internal-Secret`
  header, never from a body flag).
- Agents may not delete provider-synced comments (403) — provider
  reconciliation (companion edition) would resurrect or desync them; mark
  REVIEW instead.

**Orphaned anchors** — every content write through `update()` (agent
iterations, dashboard saves, reverts, upstream pulls) rescans open anchored
comments with a plain-substring check (`anchor_quote in content` — the same
exactness contract as the frontend highlighter). Threads whose quote is
gone get `anchor_orphaned=true` (a dedicated field, deliberately not a
`sync_state` value so push status is never clobbered); the flag clears if
the text returns (e.g. a revert). The UI shows a warning and de-emphasizes
orphaned threads.

**Activity feed** — comment lifecycle changes append a `comment` event
(`ALLOWED_EVENT_TYPES`) to the artifact's audit log with
`metadata.action ∈ deleted | reviewed | resolved`, a ≤ 100-char
`comment_snippet`, and the agent's `reason` on deletes, so a deleted
comment never disappears without a trace.

## Knowledge Library Auto-Ingest

Content-bearing local artifacts (markdown/text documents) can be automatically
ingested into the Knowledge Library so they become searchable, stay in sync as
the artifact changes, and are removed when the artifact is deleted. Off by
default, opt in with `knowledge.auto_ingest_artifacts`; the eligible kinds are
`knowledge.auto_ingest_artifact_kinds` (default `["markdown", "text", "html",
"json"]`). `widget` is excluded (widgets/dashboards are UI, not documents — and
a remote widget round-trips back to `kind="widget"` on clone) and `svg` is
excluded (the file reader has no `.svg` support).

The feature plugs into the existing Knowledge **source framework** rather than
adding a parallel watcher (see `kiro_crew.knowledge.artifact_ingest`):

- **One aggregate "Artifacts" source.** A single `sources` row of
  `source_type="artifact"` (uri `artifact://`) appears in the dashboard Sources
  UI alongside the user's folder/upload sources. Items are grouped per-artifact
  in a dedicated `artifact_item_state` table (keyed by `source_id` + `slug`,
  with the artifact's display `name` stored as the group label) — the same
  item-group pattern a folder source uses per file, so one artifact's items can
  be replaced on edit or removed on delete without touching the rest. A per-slug
  `content_hash` makes an unchanged artifact a cheap no-op. The dashboard
  sub-groups this source per-artifact (one row per artifact, labelled by name)
  the same way folder sources sub-group per file: `_attach_file_paths` supplies
  the label and the frontend gates sub-grouping on `source_type` in
  (`local_folder`, `obsidian_vault`, `artifact`).
- **One ingestion path (via the file reader).** Ingestion routes through the
  same `IngestionPipeline.ingest_file` → `FileReader` path as folders/uploads,
  not a parallel raw-text path: the (redacted) artifact content is written to a
  temp file with the kind's real extension (`markdown→.md`, `text→.txt`,
  `html→.html`, `json→.json`) and read back through the reader, so `html`
  artifacts get `_read_html` prose extraction instead of raw markup.
- **Event-driven, no polling.** The gateway is the only process that writes the
  artifact store (the agent's MCP tools, the CLI, the dashboard, and bookmarks
  all HTTP-proxy to the gateway's `/api/artifacts` routes; Artifactory
  pull/clone also funnel through the store). So a single in-process
  change-listener registered via `ArtifactStore.set_change_listener` observes
  every write path. `ArtifactKnowledgeSync.on_change` schedules the work on the
  gateway loop: `upsert` → ingest/replace the artifact's item group; `delete` →
  remove it. The store stays dependency-free — it knows nothing about the
  Knowledge package; it only fires `(action, slug)` after a
  content-affecting mutation (create, content-changing update, delete). A
  metadata-only rename fires a separate `rename` signal that refreshes the
  stored group label without re-ingesting (no chunk churn).
- **Every store take runs in a worker thread.** `on_change` schedules the
  handler on the gateway loop, so `_handle` and `ingest_artifact` run on the
  loop thread — where a contended knowledge connection would busy-wait every
  task (the watchdog heartbeat included) for the connection's whole busy timeout
  and, past 25s, get the gateway killed (see the on-loop guard in
  [knowledge](knowledge.md)). Each store call on these paths is offloaded with
  `asyncio.to_thread`: the `get_source_by_uri` lookups (delete/rename/upsert),
  `ensure_artifact_source`, `refresh_artifact_name`, `ingest_artifact`'s
  `_get_state` read and `release_stale_claim` write, the per-job
  `get_job_status` read in `reconcile_artifacts`, and `remove_artifact` (a
  `delete_items_batch` → graph rebuild). `ingest_artifact`'s post-ingest
  `get_job_status` read travels with the fallback ownership write as one
  `run_to_completion` unit, so no cancellation point separates them. That
  fallback, and the in-hop retry of a failed ownership write, go through
  `_write_ownership_if_intact`: under `BEGIN IMMEDIATE` it names the group only
  while every committed id still exists, so a concurrent dedup verdict on the
  row is never overwritten. The ordering the handler describes is preserved across
  the hops — name refresh before ingest, the kind-change reconcile before the
  ingest — and the deduped/ownership finalizers still run on the pipeline's own
  worker hop, not the loop.
- **Reconcile on every start, not a creation-gated backfill.** The feature is
  opt-in, and while it is off the change-listener is not registered, so writes in
  that window never reach the Library. Tying the catch-up pass to *creation of
  the aggregate source row* cannot repair that: the row outlives the feature
  being switched off, so on any install that ever had it on a later opt-in gets
  `created=False` and the pass never runs — the gap is permanent and silent.
  `ArtifactKnowledgeSync.start()` therefore runs `reconcile_artifacts`
  **unconditionally**, comparing the artifact store against
  `artifact_item_state`: ingest what is missing or changed, drop state for
  artifacts that no longer exist. `created` is now reported for logging only.
  - **Converged is free.** `ingest_artifact` already skips unchanged content, so
    the steady state spends no extraction calls and logs at debug.
  - **Removals are judged against every artifact, not the eligible kinds.**
    Narrowing `auto_ingest_artifact_kinds` makes an artifact ineligible, not
    absent; reaping on that basis would delete content the user never deleted.
    A reap also requires two signals, not one: `get()` raising
    `ArtifactNotFoundError` *and* the artifact's directory being gone. That error
    is also what a missing `meta.json` raises, which a partially-restored
    directory hits while its content is still present, and deletion removes the
    whole directory — so demanding both costs a recoverable stale group instead
    of unrecoverable deleted items.
  - **An emptied artifact is dropped unbudgeted.** Its body is blank, so there is
    nothing to extract and the drop costs no LLM calls. Keeping it behind the
    budget would let obsolete text stay searchable for as many restarts as a
    backlog of newer changes takes to drain. Only tracked artifacts are read, and
    the pass stands down on `source_missing` for the same reason
    `ingest_artifact` does.
  - **Off-window metadata drift is repaired unbudgeted.** A tracked artifact
    whose kind differs from the kind recorded at ingest (`artifact_item_state.kind`)
    had its group produced by the previous kind's reader, so that group is
    reaped exactly as the live `upsert` path does, and the ingest loop rebuilds
    it if the new kind is eligible. The decision reads the *recorded* kind, not
    the current allowlist: "the artifact changed" and "the user narrowed
    `auto_ingest_artifact_kinds`" are indistinguishable from the current kind
    alone, and reaping on the latter would delete content the user never
    touched. A row predating the column carries `NULL`, so its ingested kind is
    unknown; that is resolved by which repair is safe. An eligible artifact is
    re-ingested once (in budget, no deletion), which repairs any undetectable
    drift and backfills the column so it never repeats; an ineligible one is
    left alone, because deletion is the only repair available there and drift
    was never proven. Where the
    removal happens depends on whether anything will rebuild the group: a drift
    into an *ineligible* kind is reaped in the unbudgeted pre-pass (removal is
    the whole repair), while a drift between two *eligible* kinds is replaced
    inside the budgeted loop, so a backlog past the budget keeps its stale group
    and defers rather than being deleted now and restored several starts later.
    An eligible replacement clears only the recorded content hash, never the
    group: that defeats the unchanged-content short-circuit (a byte-identical
    body under a new kind would otherwise keep the previous reader's chunks)
    while leaving `ingest_file` to do its normal atomic replace, so a failed
    extraction keeps the old items and the artifact stays searchable.
    Every tracked artifact's stored group label is also refreshed to its current
    redacted name, so a rename during the off-window stops showing the old
    label. Neither pass touches content hashes — a converged store still spends
    nothing.
- **A dead source pointer neither deletes nor rewrites an index.**
  `ingest_artifact` returns early whenever `get()` reported `source_missing`
  (live file moved / unreadable, so the content is a snapshot fallback). That
  snapshot is not evidence about the live file in either direction: blank does
  not mean the artifact was emptied, and non-blank does not mean it is current,
  so acting on it would either destroy a valid index or replace newer indexed
  text with older. Same rule as the reconcile reap — only provable state acts.
  - **Ingests are bounded per run** by `RECONCILE_INGEST_BUDGET` (a module
    constant, not a config key). `ArtifactStore.list` is newest-first, so a
    backlog from a long off-window drains across successive starts with the most
    recent artifacts first, instead of arriving as one unbounded burst of billed
    extraction calls. Unchanged artifacts do not consume budget.
- **Security.** Ingested text *and* the LLM-originated artifact name (used as
  the source/item title) are passed through `redact_credentials()` and
  `redact_exfiltration_urls()` before landing in the Knowledge store (per
  input-validation guidance — never persist secrets), consistent with the
  chat-ingest path. File-backed
  artifacts whose `source_path` resolves to a sensitive path are refused (with a
  SEL audit event), mirroring the folder-watcher file-read guard.
- **Dedup tie-in.** A file-backed artifact whose `source_path` is also inside a
  synced folder source is the same document under two sources (the aggregate
  `artifact` source and the folder's `local_file` source). The aggregate
  `artifact` source is **excluded from dedup entirely** (`enumerate_docs` and
  `_build_doc_for` skip `_AGGREGATE_SOURCE_TYPES`, and `_delete_doc` refuses a
  cascade on them): treating the whole aggregate as one dedup unit keyed on a
  single item's hash both misidentified it and — when it lost a pair — cascade-
  deleted the user's entire artifact library. The artifact↔folder overlap
  therefore persists (both copies remain retrievable) until per-artifact-slug
  dedup is built; that is a recorded, intentional trade against silent data
  loss on the hard-delete path.

## Companion Chat

The artifact detail page hosts a **companion chat panel**: the artifact renders
on the left and a live agent session bound to it runs on the right, so an
iteration loop never leaves the page.

**Binding** — a chat slot may carry an `artifact` field (a validated artifact
slug) set at slot create (`POST /api/chat/slots` body key `artifact`,
validated against the slug grammar; invalid values are silently dropped).
The field is serialized in `to_dict()` — flowing into `GET /api/chat/slots`
and the WS `slots` snapshot, which is how the frontend resolves the active
bound session with zero extra endpoints — and persisted in the history meta
line so the binding survives gateway restarts and History-page resumes
(resuming a bound session re-establishes it as the artifact's active
companion).

**Tamper gate** — the binding is validated against a single shared slug
grammar (`validation.ARTIFACT_SLUG_RE`, `\Z`-anchored) at EVERY boundary it
crosses: slot create (`chat_handlers`) AND history-metadata restore on both
paths (`chat_persistence` rehydrate + bulk restore) — a tampered history
JSONL cannot inject an arbitrary string that flows into `to_dict()`/WS
broadcasts.

**Invariant** — at most one *active* (non-archived) bound session per slug,
maintained by the frontend flow. The backend accepts any valid slug and does
not enforce uniqueness.

**Live refresh** — the artifact mutation funnel broadcasts a typed
`artifact_update {slug, version, deleted}` WS event
(`DashboardState.push_artifact_update`, called via the handlers'
`_notify_artifact_update` helper) from: create (both the genuine-create and
source_path dedup-bump paths), content-carrying PATCH (Save / Snapshot /
MCP update / revert — metadata-only PATCHes do NOT emit), delete
(`deleted: true`), relocate, and pull-latest (when the pull actually landed a
new snapshot). Fire-and-forget.

**Live refresh — file-backed artifacts** — the funnel above only covers
mutations that pass through a handler, so an agent (or any other tool) writing
a file-backed artifact's `source_path` directly emitted nothing, and the open
surface kept rendering pre-edit content. The frontend closes that gap with
`useArtifactLiveReload(slug, source_path)`, mounted on `ArtifactDetailPage`
(hence also `/popout/artifact/:slug`) and on the side-panel `ArtifactPanel`:

- It subscribes to the existing `GET /api/file-watch` SSE for `source_path` as
  a **change signal only** — the frame's `content` is discarded and the
  artifact is refetched through `GET /api/artifacts/{slug}` instead, so
  redaction, the live/snapshot fallback and the `live_dirty` recompute all keep
  running on the one serving path. This also sidesteps the 512KB cap on what
  the watch stream can *deliver* — the separate cap on what it can *detect*
  still applies, see the known limits below.
- 400ms debounce, then `invalidateQueries(['artifact', slug])` — an active
  refetch, not a staleness mark, because the shared QueryClient runs
  `staleTime: Infinity` and freshness is push-driven.
- **Never refetches while an edit buffer is open** (`isArtifactEditing(slug)`),
  matching the `artifact_update` WS handler: moving the editor's baseline under
  a stale buffer makes the next Save silently overwrite the incoming update.
  Nothing else is refreshed on that branch — an external write produces no
  artifact event and no comment — and the detail page already refetches when
  editing ends.
- Inert for artifacts with no `source_path`, and inert after an SSE failure
  (`useFileWatch` closes the stream rather than reconnecting), which degrades to
  the previous manual-reload behavior.

Known limits, both inherited from the endpoint: `api_file_watch` only emits when
the redacted **first 512,000 bytes** change, so a rewrite touching only bytes
past that cap produces no signal and no reload; and each mounted surface (every
popout is its own window) opens its own EventSource against an HTTP/1.1 gateway,
so nothing is watched unless the artifact is actually file-backed.

**Panel (frontend)** — the comments sidebar and the chat panel are mutually
exclusive flex siblings of the artifact body, icon-toggled from the toolbar
(sparkle = chat, speech bubble = comments); neither overlays the artifact. The
comment-count auto-reveal never switches away from an open chat panel, since the
chat panel opens only on explicit action.

**Session resolution (frontend)** — the active bound session is resolved from
the Redux slots snapshot (`slot.artifact === slug`), so no extra endpoint exists:
the WS `slots` event already carries the binding. The flow keeps it to at most
one *active* bound session per slug by archiving before creating; the resolver
tolerates more by picking the most recently active, so a race or a History-page
resume degrades gracefully rather than erroring.

**Create is one round trip** — the `POST /api/chat/slots` response carries the
binding, so it is dispatched straight into the slots list (`addSlotOptimistic`)
and the panel becomes interactive immediately. The silent context entry POST and
the `fetchSlots` reconciliation run in the background: the context entry is
consumed on the *next* user message, so it always lands before a human can type
and send.

**Chat parity** — the panel embeds the same `ChatPage` component as `/chat`
(`embedded` + `embedMode="chat"` for the single-session chrome), so follow-up
option chips, question cards, steer-send, tool groups and regenerate are
identical by construction. A `noUrlSync` prop gates ChatPage's one URL-write
effect: the host route `/artifacts/:slug` owns the URL, and an in-place
`navigate` would swap the host route out from under the panel.

**Composer staging** — "Ask agent to address" routes into the bound session and
*stages* (never auto-sends) its message through the existing `writePrefill`
sessionStorage channel ChatPage already consumes on slot activation (the
slot-change restore in `website/src/pages/chat/page/composerDrafts.ts`).

## Roadmap

In scope for the foundation:

- ✅ data layer + CLI + MCP tools + HTTP + library page + standalone page
- ✅ "Save as artifact" affordance on rendered widgets
- ✅ widgets are artifacts **by default** — auto-registered unpinned on emission,
  listed in the in-session Artifacts tab, retention-swept while unstarred
- ✅ system prompt context note documenting the iterate flow

Out of scope (separate tasks):

- **Whiteboard layout** — saved arrangements of (artifact_id, x, y, w, h) —
  tracked as follow-on work.
- **Live refresh bindings** — cron / Python script / MCP-tool source types
  that auto-rewrite `current.html` on a schedule — tracked as follow-on work.
  The hook will be a new `meta.json.refresh_binding` field consumed by a
  refresh service.
- **Right-panel inline render** — clicking an `<a>` to an artifact in chat
  opens the artifact in a side panel rather than the standalone page —
  related follow-on work.
- **Cross-user sharing**, **embeddings/full-text search**, **install from
  URL/community widget store** — future expansions.

## WebApp Artifacts (`kind="webapp"`)

A `webapp` artifact represents a *deployed application*. It carries structured
`webapp_metadata` (deploy target, architecture, lifecycle/TTL, cost estimate,
teardown handle, local app tree) and the dashboard renders it as a
browser-framed app card: a live preview of the app plus deploy state, cost,
and TTL panels.

**Preview rendering (local-first fallback chain).** The card and the gallery
thumbnail try, in order: (1) the **local preview channel** — the gateway
serves the app's local copy (`webapp_metadata.app_dir`) through a token-gated
static route, working for every lifecycle state including expired and
not-yet-deployed; (2) a sandboxed iframe of the **live CloudFront deployment**
(`framablePreviewUrl` gate: https + `<dist-id>.cloudfront.net` host shape
only, mirrored by the server CSP `frame-src https://*.cloudfront.net`);
(3) a status hero.

### WebApp Metadata Schema

| Field | Type | Description |
|---|---|---|
| `deploy_target.provider` | string | `"aws"` (default) |
| `deploy_target.account` | string | AWS account ID |
| `deploy_target.region` | string | AWS region |
| `deploy_target.public_url` | string | The live HTTPS URL |
| `deploy_target.profile` | string | Named AWS CLI profile used |
| `app_dir` | string | Absolute path of the local app tree that was/would be deployed. Set by the artifact author (the deploy API never sees the artifact and the directory together, so it cannot back-fill this). LLM-influenceable — re-validated against the allow-listed local roots at serve time. |
| `architecture.tier` | enum | `"static"`, `"api"`, `"stateful"` |
| `architecture.resources` | list | `[{type, id}]` — infrastructure resources |
| `lifecycle.created_at` | string | ISO 8601 creation time |
| `lifecycle.expires_at` | string? | ISO 8601 expiry (null = persistent) |
| `lifecycle.persistent` | bool | Whether the deploy has no TTL |
| `lifecycle.ttl_hours` | int | Original TTL in hours |
| `lifecycle.status` | enum | `"draft"`, `"deploying"`, `"live"`, `"error"`, `"expired"` |
| `teardown.method` | string | `"reaper-lambda"` |
| `teardown.handle` | string | Reaper target handle |

### Local Preview Channel

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/artifacts/{slug}/app-preview` | standard dashboard auth | Validate the artifact + `app_dir` and mint a short-lived (15 min) HMAC path token. Returns `{available, base}`; `{available: false}` for every miss (no oracle). |
| `GET` | `/artifact-app/{slug}/{token}/{path}` | HMAC path token (auth-middleware bypass) | Serve one static file from the app's web root — **`app_dir/public` is mandatory** (deploy-contract layout); an app_dir without a contained `public/` directory reports the preview unavailable, it is never served directly. Sandboxed preview iframes carry no cookies, so the token IS the auth. |

Serve-time security (fail-closed 404 for every rejection): allow-listed local
roots (same list as the deploy publish path); `public` symlink must resolve
inside the validated `app_dir`; full-resolution containment check per file
(traversal + symlink escape); dotfile components never served; sensitive
paths rejected (`is_sensitive_path`); reads go through the inode-pinned
`safe_read_file_bytes_nolink(within_root=webroot)` helper; token HMAC binds
`slug + webroot + exp` with a per-process secret; responses carry
`Content-Security-Policy: sandbox allow-scripts` (opaque origin even outside
the iframe) plus `nosniff` and `no-store`. All filesystem work runs off the
event loop via `asyncio.to_thread`.

### Deploy Routes (`/api/deploy/*`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/deploy/config` | Read deploy config (default profile) |
| `PUT` | `/api/deploy/config` | Update deploy config |
| `GET` | `/api/deploy/profiles` | List registered AWS profiles |
| `POST` | `/api/deploy/profiles` | Add a new profile |
| `PUT` | `/api/deploy/profiles/{name}` | Update a profile |
| `DELETE` | `/api/deploy/profiles/{name}` | Delete a profile |
| `GET` | `/api/deploy/iam-policy` | Get the required IAM policy document |
| `POST` | `/api/deploy/verify` | Verify credentials for a profile |
| `POST` | `/api/deploy/deploy` | Deploy a site (confirm-gated) |
| `POST` | `/api/deploy/recall` | Recall (soft teardown) a site (confirm-gated) |
| `POST` | `/api/deploy/destroy` | Full teardown of infrastructure (confirm-gated) |
| `GET` | `/api/deploy/list` | List deployed sites |
| `POST` | `/api/deploy/teardown/{slug}` | Human-triggered artifact teardown |
| `GET` | `/api/deploy/pending` | List pending (unconfirmed) deploy previews |
| `POST` | `/api/deploy/pending/{id}/confirm` | Execute a pending deploy (cookie/token only; internal-secret denied) |
| `POST` | `/api/deploy/pending/{id}/dismiss` | Dismiss/cancel a pending deploy (cookie/token only; internal-secret denied) |

Mutating routes fall into two categories:
- **Confirm-gated** (two-step preview+confirm): deploy, recall, destroy.
- **Auth-gated CRUD** (cookie/token auth, no confirm step): profile and config
  creation/update/deletion.
- **Pending-confirmation** (cookie/token only; internal-secret sessions are
  explicitly denied): `GET /api/deploy/pending`, `POST .../confirm`,
  `POST .../dismiss`. These routes support the two-step preview→confirm
  deploy flow — the gateway generates a pending entry at preview time and
  the dashboard UI confirms or dismisses it.

All mutating routes require an unrestricted (non-restricted) session.

### Teardown Semantics

Teardown of a `webapp` artifact follows a **tombstone + manifest-expiry + reaper**
model:

1. **Tombstone:** `mark_webapp_expired(slug)` sets `lifecycle.status="expired"`
   in the artifact metadata. The artifact is kept as deploy history.

2. **Manifest expiry (best-effort):** The teardown handler rewrites the S3
   deploy manifest (`.kirocrew-deploy.json`) with `expires_at=now`,
   `persistent=false`. This is a non-destructive S3 PUT using the deployment's
   recorded profile. If credentials are unavailable or the bucket is unreachable,
   the tombstone still stands.

3. **Reaper sweep:** The in-account reaper (`scripts/reaper.sh` or the reaper
   Lambda via EventBridge) scans deploy manifests on a schedule. Manifests with
   `expires_at` in the past are reaped: backend stack deleted, S3 prefix removed,
   CloudFront invalidated. The manifest removal commits the reap.

The gateway's `/api/deploy/destroy` endpoint (confirm-gated) calls
`engine.destroy` under cookie/token auth + confirm + audit to initiate
infrastructure teardown. This is the **direct teardown path** — it performs
destructive AWS calls (DeleteStack, bucket deletion, distribution teardown)
synchronously under the user's own credentials during the request.

Separately, the **reaper path** (the in-account reaper Lambda or
`scripts/reaper.sh` via EventBridge schedule) sweeps for expired manifests
and performs the same cleanup on a schedule. The reaper runs with the user's
own credentials in-account and handles the case where the gateway is unreachable
or the user did not explicitly destroy before TTL expiry.

## Image Artifacts (`kind="image"`)

An `image` artifact is a **raster picture**, not text. Its bytes live in a binary
sidecar beside `meta.json` and are served by a dedicated endpoint; the textual
`current.html` exists but stays **empty**, and `content` in every API response is
`""`. Consumers must therefore never render an image artifact through the text or
widget paths — `ArtifactBodyImage` handles it, bypassing both Monaco and the
sandboxed iframe.

SVG is deliberately **not** an image artifact: it is markup, stored as
`kind="svg"` text and rendered through the sanitizing `SvgViewer`. Serving
agent-authored SVG as an image would reintroduce a same-origin script vector.

### Storage layout

```
~/.kiro/crew/artifacts/
└── <slug>/
    ├── meta.json        includes the `image` block below
    ├── current.html     present but EMPTY (bytes are not text)
    ├── asset.<ext>      the raster bytes (png|jpg|webp|gif|bmp)
    └── versions/
        └── v1.html      empty, mirroring current.html
```

The sidecar's extension is derived **from the allowlisted mime**, never from the
stored `ext` field, on every read (see [Security](#security)). The allowlist and
the header sniffers live in `kiro_crew.artifact_store.images`. `delete` removes
the whole artifact directory, so the sidecar needs no separate cleanup.

### `image` metadata schema

Tolerant-loaded: every field is optional so an older or hand-edited record still
opens, and each consumer degrades gracefully.

| Field | Type | Description |
|---|---|---|
| `mime` | string | One of `image/png`, `image/jpeg`, `image/webp`, `image/gif`, `image/bmp`. Anything else is rejected on create **and** refused on read. |
| `ext` | string | Sidecar extension as written. Informational only — reads re-derive it from `mime`. |
| `size_bytes` | int | Byte length of the sidecar. |
| `width` / `height` | int | Natural pixel size, sniffed from the file header with the stdlib only (no Pillow). `null` when unmeasurable — dimensions are a rendering nicety, not a gate. |
| `sha256` | string | Hex digest of the bytes. |
| `original_filename` | string | Basename of the source file; names the download. **LLM-derived** → redacted in `_serialize`. |
| `alt` | string | Accessible description, from the markdown alt text. **LLM-derived** → redacted in `_serialize`. |

### Asset endpoint

`GET /api/artifacts/{slug}/asset` — returns the raw bytes with the sniffed
`Content-Type`, so an `<img src=…>` can point straight at it and the artifact
JSON never carries base64.

- **Authenticated** like every other artifact route; unauthenticated requests get
  403. No restricted-session gate applies (it is a read) and no `referenced`
  breadcrumb is recorded (an asset fetch is a sub-resource of a detail view that
  was already counted).
- `Cache-Control: private, max-age=31536000, immutable` — `private` because the
  bytes are behind token auth and a shared proxy must never serve a cached copy
  to an unauthenticated requester.
- 404 when the slug does not resolve, is not an image artifact, its sidecar is
  missing, or its mime is not in the allowlist.
- The read is offloaded with `asyncio.to_thread`: the sidecar may be up to
  `MAX_CONTENT_BYTES` and a synchronous read would stall every other gateway
  task, the liveness heartbeat included.

### Auto-registration from chat (`kiro_crew.image_artifacts`)

Finalized assistant messages are scanned for **local** markdown image references
and each one is registered, copying the bytes immediately so temp-file cleanup
cannot strip them.

- **Identity.** Slugs are derived deterministically from `(message_ts, index)`
  via `widget_slug.derive_widget_slug` on the Python side —
  `image_artifacts._derive_image_slug` seeds it with `<ts>#image` so an image and
  a widget in the same message never collide. This ordinal form is backend-only:
  the frontend has no counterpart, because widget identity is keyed on the body
  (see the widget Identity contract above), not on an ordinal. `index` counts
  **every** image match in the message including skipped ones — so an image's
  ordinal is stable regardless of which siblings were skipped. A replayed message
  is therefore idempotent and never clobbers an artifact the user has since
  edited.
- **Destination parsing.** Balanced-paren walk, so `screenshot(1).png` survives;
  `<...>` destinations are unwrapped so a path containing spaces survives;
  backslashes are treated as escapes **only** before markdown-significant
  characters, so a native Windows path (`C:\Users\me\shot.png`) survives; alt
  text accepts escaped brackets (`![Revenue \[Q1\]](…)`) and is unescaped for
  display.
- **Skipped:** remote/`data:`/protocol-relative URLs (never fetched), relative
  paths, unsupported extensions, sensitive paths, and restricted/incognito
  sessions.
- **Budgets (per message):** at most `MAX_IMAGES_PER_MESSAGE` (12) images and
  `MAX_IMAGE_BYTES_PER_MESSAGE` (64 MiB) of copied bytes. Counted over
  *eligible* images rather than successful writes, so a replay cannot walk past
  the cap one batch at a time. Pruning runs after the loop, so without these a
  single message could fill the disk.
- **Retention.** Auto-registered images are `auto_registered=True` and unpinned,
  so they ride the **same** count-based sweep as auto-registered widgets
  (`prune_auto_widgets(keep=MAX_AUTO_WIDGET_ARTIFACTS)`) — the predicate is
  kind-agnostic. Images and widgets therefore share one budget; pinning
  ("Save permanently"), filing, tagging, or commenting exempts a record.
- **Never raises.** A failure to register a chat image is a lost convenience, not
  a reason to fail the turn that produced it; per-image failures are logged and
  skipped individually. Dispatch uses `asyncio.to_thread` rather than the shared
  subprocess pool, so a wedged teardown worker cannot hold registration until
  after the source file is gone.
