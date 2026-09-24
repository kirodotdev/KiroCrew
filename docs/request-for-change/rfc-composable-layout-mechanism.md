---
title: Composable Layout Mechanism — a reusable pane-layout foundation, first surfaced as custom member layouts
status: draft
author: gjjnn
created: 2026-09-24
last-audited: 2026-09-24
audited-at: a305e4fba
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Composable Layout Mechanism

- Status: draft — nothing in this document is implemented. It is a proposal and a
  record of the decision to build the mechanism described below and to surface it
  first as custom member layouts, to be landed as the sequence of small PRs in §7.
- Author: gjjnn
- Created: 2026-09-24
- Related: `rfc-chat-core-extraction.md` (owns the ChatPane / ChatInput
  extraction the proposed cells would reuse), `rfc-navigation-placement-seam.md`
  (a sibling data-driven-registry pattern), `docs/system-specs/modules/crew-mode.md`
  and the Crew Members page (the first surface this mechanism drives).

## 1. Summary

This RFC proposes a **composable layout mechanism**: a small, reusable foundation
for arranging panes — chat, files, git, a terminal, notes, and so on — into a
saved layout that then drives how a surface renders. The mechanism is deliberately
generic: a layout is a tree of placed panes, each pane reads a subject to know
what to show, and the whole thing is data (§5).

To avoid boiling the ocean, the mechanism is **surfaced first, and only, as custom
member layouts.** Today, when you open a crew member (a "crewmate") on the Crew
Members page, the thread area is a single fixed arrangement: a header, the chat
transcript, and a docked side panel — every crewmate looks identical, no matter
what kind of work it does. The first surfacing lets a user build their own
arrangement of panes per crewmate and have it persist and drive that crewmate's
page. Because a crewmate can stand for a *type of task*, that layout becomes "the
workspace for the kind of work this member performs."

Several other surfacings are real goals but are **deliberately deferred** to keep
this first step shippable: AI-generated layouts via fast decision models like Jev, focused report surfaces, and
admin-authored layouts for teammates (§8). This RFC's job is to build the
foundation those will reuse, and to prove it end-to-end on one surface — not to
build them.

## 2. This is a generalization, not a new abstraction

**The proposed layout mechanism
is a generalization of two things the dashboard already does today.** Understanding
that is understanding the whole design. Both facts below are verified on main at
`a305e4fba`.

### 2.1 The dashboard already lets you assign views into a surface

The chat side panel is a tab strip whose tabs you assign from a fixed vocabulary
of "views." That vocabulary lives in `website/src/hooks/usePanelTabs.ts` as
`ViewKind`:

    'changes' | 'issues' | 'links' | 'files' | 'artifacts' | 'subagents' |
    'workflows' | 'logs' | 'crewlog' | 'context' | 'side' | 'browser' | 'git' |
    'summary' | 'pins'

Some of these views are pinned to the front of the strip
(`PINNED_VIEWS = ['changes', 'artifacts', 'files']`), others are opened on demand
from a `+` menu, and any can be withheld from a given host. In other words, the
dashboard **already has the act of "assign a view to a surface."** It just
constrains that surface to a one-dimensional tab strip.

The proposed layout mechanism reuses that exact idea and promotes the surface from
a 1-D tab strip to a 2-D grid of cells. Each pane a user can place — chat, files,
git, changes, subagents, terminal, notes — is one of these existing views. The
layout's element vocabulary is `ViewKind` promoted from "which tab" to "which
cell." A grouped-tabs pane inside a layout is simply the tab strip's own model
re-expressed as one placeable element, which is why "you can assign tabs to a
pane" falls out for free.

`ViewKind`'s sibling type `TabKind` already carries an extension arm
(`app:${string}`) that admits new kinds without breaking the exhaustive
`Record<ViewKind, …>` tables the code relies on. That the vocabulary was designed
to be widened one entry at a time is what makes the incremental rollout in §7 safe.

### 2.2 A "slot" already decides how each view renders — and that is the whole input contract

The second existing pattern is that every view already renders against a **slot** —
the identifier for one conversation/session. `usePanelTabs`'s first argument is the
slot, and a companion table (`VIEW_DATA_SOURCE`) records that each view draws its
data from that slot: its artifacts, its sub-agents, its workflow runs, its git
state, its project files. The `ActivityViewer` component, for instance, renders the
git / changes / subagents views entirely from a `slot` prop.

The proposed mechanism reuses this **without changing it.** A layout carries a
"subject" at its root, and each placed cell reads that subject to know what to
render — and that subject is **exactly a slot, nothing more.** It is the same input
a tab view takes today; the layout introduces no new per-element input, and in
particular it does **not** require a member id or couple an element to the Crew
Members page. The roster (which is not part of the layout) is what sets the slot
when you pick a crewmate, exactly as selecting a conversation sets the active slot
today.

The subject carries a **pointer, not a record.** The slot is an id; the live
session record it names — `ChatSlot`, a ~50-field frontend type carrying
`project`, `agent`, `running`, `source_links` and much else — is re-resolved at
render (`slots.find(s => s.key === slot)`), never persisted and never placed on
the scope. `VIEW_DATA_SOURCE`'s `'slot'` classification means a view's data is
*derivable from* that session context, **not** that the id alone is sufficient:
`git`, `files`, and `terminal` need the resolved `project` path, which lives on
the `ChatSlot` record, not on the id. That resolution happens in the host (§4),
so the subject stays a bare slot even though the element needs more.

Keeping the contract at "just a slot" is a deliberate decision: an element
declares only what it needs, and today every element's need reduces to the slot
(or to props the host resolves for it). A future direction (§8) is to consider
**widening** this interface as new element types demand it — a typed subject in
which each element declares a *narrow slice* of its need, with the host as the one
place that resolves the fat `ChatSlot` record down to that slice — rather than
either pre-coupling every element to a session id it may not use, or ever putting
the whole volatile record on the bus. Starting from the minimal, already-proven
contract is what keeps the AI-generated, report, and admin-template explorations
from having to fabricate inputs they do not have.

### 2.3 What this framing buys the design

1. **Reuse over reinvention.** Each pane wraps a component that already exists
   (`ActivityViewer`, `FolderPanel` for files, `CliPanel` for the terminal,
   `CrewNotesTab` for notes) rather than reimplementing it (§4).
2. **A proven vocabulary.** Every layout element is already a `ViewKind`, so it
   already has a data source and a rendering component.
3. **A proven way to grow.** Adding one element at a time mirrors how `TabKind`
   already admits new kinds — which is what makes the one-element-per-PR plan in §7
   low-risk.

### 2.4 The core shift: layout wiring moves from source to config

The clearest way to say what this mechanism does: it moves layout wiring **from
the developer's source code into user-authored config.** Two facts about a view
live at two different layers, authored by two different people:

| | Authored by | Encoded in | Changes when |
|---|---|---|---|
| A view's data source (`VIEW_DATA_SOURCE`) | the developer | source code (a frozen `Record<ViewKind, …>`) | the codebase is edited |
| A view's placement (the layout tree) | the user | config (the persisted `LayoutTree`) | the user rearranges their layout |

`VIEW_DATA_SOURCE` is a fact about the *kind* — "a `git` view is slot-fed" — true
everywhere and never a user's to change; it stays dev-authored in source. Today a
page's *placement* is also dev-authored in source: the hardcoded thread area is
JSX a developer wrote (`<ChatPane slotKey=… />`, a docked side panel). This
mechanism lifts that one thing — placement, the wiring of which pane sits where and
reads which subject — out of the developer's JSX and into a `LayoutTree` the user
authors by dragging. Placement *is* the wiring (a cell reads the nearest scope
upward, decided by where it sits in the tree), and placement is now the user's.

This is the whole generalization, and it is why the §8 surfacings fall out for
free: the mechanism does not care *who* authors the config. A user drags it; a
fast decision model emits the same tree; a crew admin writes it for a teammate.
Different author, identical config artifact and identical renderer. The widening
in §8 keeps the same split — a new element's *need* stays dev-authored (a property
of the element's code), while the *value* that satisfies it stays user-config
(which subject the placed cell points at).


## 3. Goal and non-goals

**Goal.** Build the composable layout mechanism (§4–§5) and surface it first as
custom member layouts: let a user compose their own layout for a crewmate — chat
plus whichever of files / git / changes / subagents / terminal / notes / a side
panel they want, arranged in a grid — and have it persist per crewmate and drive
that page. The mechanism is designed to be reused by later surfacings; this RFC
proves it on one.

**Non-goals for this RFC** — each is a real, wanted surfacing of the same
mechanism, deliberately deferred so the first step stays shippable (§8):

- AI-generated layouts, or layouts produced by a fast decision model.
- Report-style or single-purpose focused surfaces.
- An admin authoring layouts on behalf of teammates; any sharing or distribution.

Plus two scope limits that are not future surfacings, just out of scope here:

- Server-side persistence (this proposes local persistence only, §4).
- Reproducing every responsive behavior of today's docked/overlay side panel.

## 4. Proposed design

A new frontend package, `website/src/components/crew/layout/`, would hold:

- **A persisted layout tree.** Nodes are a `grid` (with explicit columns, rows,
  track sizes, and each cell's rectangle), a `tabs` container, a `group`, and a
  `cell` (one placed element from the vocabulary). Geometry is stored explicitly so
  a layout round-trips without distortion — a 2×3 grid stays a 2×3 grid.
- **A subject/scope** carried down the tree (the active slot), which
  each cell reads to render (§2.2). The subject is a slot pointer and nothing
  more — the same slot-only contract §2.2 and §5.3 make normative.
- **A cell registry** mapping each element kind to the component that renders it.
  Elements split into two classes. **Self-contained** elements (`chat`, and the
  `ActivityViewer`-backed `git` / `changes` / `subagents`) need only the slot id
  and read the rest off the store keyed by that id — they touch neither `ChatSlot`
  nor the host. **Host-backed** elements (the side panel, `files`, `terminal`,
  `notes`, `workLog`) need data only the host can assemble — `files` needs the
  resolved `project` path, `notes` the member slug — and receive it through a small
  set of injected **render-prop functions**. Those functions *are* the
  `slot → ChatSlot → props` resolution seam: the host runs `slots.find` and hands
  each cell exactly its narrow props (`projectDir`, `openFile`), so the element
  never reads `ChatSlot`, `slots.find`, or the store directly. This is what keeps
  the heavy, page-specific wiring in the host and lets the real component be reused
  and never reimplemented — at the cost that a host-backed element renders a stub
  on any surface that does not supply its render-prop (§8).
- **A renderer** that draws a saved tree, framing each cell as a bordered, titled
  card so empty panes read as belonging to a named element.
- **An editor** for building the grid by dragging and resizing panes.
- **Local persistence** keyed per crewmate, with a versioned envelope.

**Integration** with the Crew Members page is a single conditional: when the
feature is enabled and a saved layout exists for the current crewmate, the thread
area renders the layout (seeded with the real slot and the render-prop functions);
otherwise it renders exactly today's hardcoded regions. The roster stays visible in
both cases; the hardcoded side panel is suppressed only when a custom layout owns
that area.

## 5. Data model

Every new type is a frontend TypeScript type in the layout package; **no backend
schema, database table, or API payload changes.** The persisted artifact is a
single versioned JSON blob in `localStorage`.

### 5.1 The persisted layout tree

The stored value is a `LayoutTree` — a versioned envelope around a node tree:

    interface LayoutTree { version: number; root: LayoutNode }

`LAYOUT_VERSION` starts at `1`; a load that sees an unknown version drops to the
floor rather than throwing. A `LayoutNode` is one of four shapes:

| Node | Fields | Meaning |
|---|---|---|
| `cell` | `id`, `element: ElementKind`, `config?` | one placed pane |
| `group` | `id`, `dir: 'row'\|'col'`, `children[]`, `sizes?` (fr weights) | a row/column of nodes sharing scope |
| `tabs` | `id`, `children[]`, `active?` | children as tabs over one active child |
| `grid` | `id`, `cols`, `rows`, `children: PlacedNode[]`, `colSizes?`, `rowSizes?` | an absolute cell grid |

A `PlacedNode` is `{ node: LayoutNode; x; y; w; h }` — a node plus its rectangle in
the grid. The `grid` node is what stores geometry losslessly: `cols`/`rows` plus
each child's `{x,y,w,h}` is exactly what makes a 2×3 arrangement survive
save/reopen without collapsing. Only `config` (a static `Record<string, unknown>`
per cell) and structure are persisted; **no slot, subject, or live data is ever
written to storage** — those are re-resolved at render time.

### 5.2 The element vocabulary

`ElementKind` is the closed union of placeable panes: `chat`, `sidePanel`,
`files`, `git`, `changes`, `subagents`, `terminal`, `notes`, `workLog`. As §2.1
notes, this is the existing `ViewKind` promoted to "which cell." `isKnownElement`
guards a loaded tree so an element kind that no longer exists drops its node
instead of crashing.

### 5.3 The runtime scope value (not persisted)

`Subject` is the value a cell reads to know what to render, and it is the entire
per-element input contract (§2.2):

    interface Subject { slot: string }

It is runtime-only — carried in a `Scope` (a `Map<key, Subject>` with a parent
pointer mirroring the tree) and **never serialized**. The `slot` is a *pointer*:
the host resolves it to the live `ChatSlot` record at render (`slots.find`) and
supplies any fat props a host-backed element needs through the render-prop seam
(§4), so the persisted layout never freezes volatile session state. Widening this
type — letting an element declare a narrow slice the host resolves `ChatSlot` down
to, rather than fattening the bus with the record itself — is the §8 future
direction.

### 5.4 The editor's edit-time model (not persisted)

The builder manipulates a richer shape than the stored tree, converted by
`toTree`/`toEditModel`:

    interface GridSpec { cols: number; rows: number; items: GridItem[] }
    interface GridItem extends Rect { id; element; tabs?; activeTab?; grid?; config? }

`GridItem` carries editing-time affordances (a nested `grid` for a group, `tabs`
for a tab container) that the conversion folds into `LayoutNode`s. This type lives
only in the editor and the conversion; it is never stored and never crosses into
the renderer.

### 5.5 Persistence envelope

One `localStorage` key per subject, `experimental-memberlayout:<layoutKey>`, whose
value is the serialized `LayoutTree`. The key is caller-supplied, so
per-crewmate vs per-project is the caller's choice, not baked into the store
(Phase 2 keys by the crewmate's slot). A malformed or wrong-version entry is
dropped on load. Moving off `localStorage` later (for the admin-template
exploration, §8) changes only this envelope, not the tree shape. The idea here is to
key it as experimental so we do not need to worry about backwards compatability during
development, and will drop the experimental prefix once this is stable.

## 6. Key decisions

1. **Floor guarantee.** With no saved layout — or the feature turned off — the page
   renders **byte-for-byte the arrangement it renders today.** A custom layout is
   strictly additive over a fallback that is always the current page. This is the
   top invariant (test in §9).
2. **The roster is permanent chrome, never a layout element.** It is how you select
   the crewmate, so it always renders and cannot be placed, moved, or removed.
3. **The layout governs only the thread + side-panel area**, beside the roster.
4. **Grid geometry is stored losslessly** — columns, rows, track sizes, and each
   cell's rectangle — so editing and reopening never collapses the grid.
5. **Reused components are never reimplemented.** A pane that needs page-owned data
   is rendered through an injected function that returns the real component; its
   body is not copied into the layout package. One source of truth.
6. **A slot drives every cell** (§2.2); the roster is the writer, cells are readers.
7. **Every rendered cell has a titled frame** so empty states are legible; a
   component that already draws its own header suppresses that inner header rather
   than dropping the cell frame (avoiding a doubled header).
8. **The whole path is behind a feature preview flag**; off means none of it runs.

## 7. Rollout — one element per PR

This repo's accepted changes are small: median about 220 lines per commit, ~920 at
the 75th percentile, and at most two commits per PR. The whole feature is far
larger than one PR should be, and it is several logical changes, not one. The
extension shape from §2.3 lets it land as a foundation plus one element at a time,
each addition touching only the layout package (and, for page-owned elements, one
injected function on the Crew Members page).

This document is a **doc-only commit**; the code lands in the PRs below.

| # | PR | Scope | ~Lines |
|---|---|---|---|
| 1 | Layout model + tests | the tree, the vocabulary, the lossless edit-model conversion, unit tests — pure data, no UI | ~700 |
| 2 | Layout editor + dev harness | the drag/resize/tabs builder over `GridSpec`, plus a standalone harness page to exercise it. Depends only on the model (PR 1); it edits a spec and does not render live panes, so it touches nothing on the layout render path | ~500 |
| 3 | Renderer + local store + `chat` element | the read path — draw a saved tree, frame each cell, persist per subject — proven with the first element, behind the flag | ~400 |
| 4 | Crew Members page integration | the single render branch, the edit affordance, the flag/route plumbing, a few new UI strings | ~200 |
| 5 | `sidePanel` element | cell + injected render function + palette entry | ~40 |
| 6 | `files` element | reuses `FolderPanel` | ~35 |
| 7 | `terminal` element | reuses `CliPanel` | ~35 |
| 8 | `git` / `changes` / `subagents` elements | three thin adapters over `ActivityViewer` | ~45 |
| 9 | `notes` element | reuses `CrewNotesTab` | ~30 |
| 10 | `tabs` container + nested grids | the structural element (§2.1) | ~60 |
| 11 | `workLog` element | requires lifting the work-log body (which also carries the member summary) to shared scope so both the current panel and the new cell render from one source — the one refactor of existing page code | ~150 |

Ordering rationale: the editor (PR 2) and the renderer (PR 3) are split because
they are genuinely independent — the editor only produces and edits a `GridSpec`,
and never renders a live pane, so it needs neither the renderer nor any cell to
exist. Building both **beside** the large, actively-edited Crew Members page (PRs
1–3 touch no render-path files), then wiring into it in one small deliberate PR
(4), isolates all integration risk to that one PR. Every element PR after it is
additive and independently revertible, and each lands at or below the repo's median
size. The editor as its own PR keeps the largest single piece reviewable purely as
"does the grid/drag/resize math produce a correct spec," with no rendering concerns
mixed in.

## 8. Deferred surfacings the mechanism enables

These are the reasons the foundation is worth building generic rather than as a
one-off member feature. Each is a **deferred goal**, not a vague someday: the same
tree, renderer, subject/scope, and cell registry drive each one, so the first
surfacing (member layouts) is what proves the foundation they reuse. They are held
back only to keep the first step shippable.

- **AI-generated UI with a fast decision model.** A fast model could emit a layout
  tree instead of a person dragging one. The renderer and element vocabulary are
  unchanged; only the *author* of the tree changes. The subject/scope seam already
  separates "what to show" from "which slot."
- **Focused, single-purpose surfaces such as reports.** A layout constrained to a
  few read-only cells is a report view; the same mechanism composes a focused
  surface as easily as a working one.
- **Admin-authored layouts for teammates.** A crew administrator could compose a
  layout appropriate to a teammate's context and distribute it. The persisted tree
  is the shareable artifact; only the storage backend (local → shared) and a path
  to apply a layout to another member's page would be new.

- **A declared per-element capability contract.** The seam that makes an element
  portable is not the subject — it is the render-prop bundle (§4). Self-contained
  elements travel with just the slot id; host-backed elements (`files`, `terminal`,
  `notes`, `workLog`, the side panel) render a stub on any host that does not
  supply their render-prop, so the *pair* (element + its host renderer), not the
  element alone, is what a new surface must satisfy. The generalization is to let
  each element **declare the capabilities it requires** and have a host prove it
  can supply them — exactly what `VIEW_DATA_SOURCE`'s slot-vs-transcript split does
  today as a two-value ancestor of that contract. An element then depends only on
  its subject, its `config`, and its declared capabilities — never on `ChatSlot`,
  `slots.find`, or the store — and a host that cannot supply a capability simply
  does not offer that element. This is the design principle the member surfacing
  exercises informally (via the fixed `hostRenderers` struct) and a later surfacing
  would formalize.

Each explicitly builds on this RFC's tree model, renderer, and reuse seam.

## 9. Build vs adopt

This mechanism is mostly new frontend code (§7), so the fair question is whether an
off-the-shelf library already provides it. That question has **two independent
axes**, and conflating them is what makes "just use a library" sound decisive when
it is not:

1. **The layout engine** — the grid / drag / resize / tabs / persistence machinery
   that draws and edits a pane tree. Mature libraries exist here.
2. **The generative-UI tree** — an agent (or a person) emitting a declarative,
   host-validated tree the host renders from components it owns. A separate
   industry pattern, relevant only to the §8 future, not to shipping member
   layouts.

The two are evaluated separately below, and the section closes by stating which of
these decisions are **reversible** and which are **locked in** once layouts are
saved — so the choice of whether to build or adopt each axis is made against the
part of the design it actually affects, not as a blanket verdict.

### 9.1 The layout engine — dockview / react-mosaic

Serializable dock/tiling libraries provide most of §7's grid/drag/resize/tabs
budget out of the box, and this frontend already adopts dependencies freely
(`@dnd-kit/*`, Radix, `xterm`), so "we don't take dependencies" is not a reason to
reject one. Two are the credible candidates:

- **dockview** — a full docking framework (splitviews, floating groups, tab
  drag-and-drop) with its own JSON serialization (`toJSON`/`fromJSON`).
- **react-mosaic** — a lighter binary-split tiling tree (`MosaicNode<T>`), where
  each leaf is a key the host renders.

The honest assessment: **either could plausibly render the grid, and if one fits
the contract points below, PRs 1–3 shrink to adapters against it.** The RFC does
not reject them on principle. It requires that whichever path is taken satisfy four
contract points this design already commits to, and the reason to lead with our own
tree is that adopting a library's *serialization format* is what would lock the
saved-layout schema to a third party (§9.3), not the reason to reject the library's
*renderer*:

| Contract point (§ where it is fixed) | What an adopted engine must satisfy |
|---|---|
| **Floor fallback** (§4, §10) — flag off or no saved layout renders today's page byte-unchanged | the engine must be *absent* from the render path when no layout exists, not a wrapper that is always mounted |
| **Lossless geometry** (§5.1, §10) — a 2×3 grid with a spanning cell survives save→reopen | the engine's persisted format must round-trip N-way grids with spans; react-mosaic's binary-split tree cannot express an arbitrary grid without nesting distortion |
| **Render-prop seam** (§4) — host-backed cells receive `slot → ChatSlot → props` from the host, never read the store | the engine must let a leaf render an arbitrary host-supplied component with host-injected props, not just a static registry entry |
| **Bundle cost** — the member page must not pay for the editor when no layout is active | the engine must code-split so a user with no custom layout loads none of it |

The `LayoutTree` (§5.1) and cell registry (§5.2) are defined so that **the engine
underneath them is swappable**: the tree is our schema, and a renderer that draws
it with our own grid code or delegates to dockview is an implementation choice
behind the same seam. That is the deliberate design that keeps the engine choice
reversible (§9.3).

### 9.2 The generative-UI tree — AG-UI / A2UI / Open-JSON-UI / MCP-UI / json-render

A separate industry pattern has converged that is close to the tree model this RFC
proposes: an agent (or a person) emits a **declarative, host-validated UI tree**,
and the host renders it from **components it already owns** — no executable code
crosses the boundary. That is the same shape as `LayoutTree` (§5.1) through the cell
registry (§5.2). It bears only on the §8 agent-authored-layout future, not on
shipping member layouts. The systems, and what each actually is:

- **AG-UI** (CopilotKit) is a backend↔frontend **event/transport protocol** — the
  wire between an agent and a user-facing app. It is a different layer from a layout
  system: it carries bytes, it does not decide what panes render where. Its own
  documentation states it "is not a generative UI specification" and defers the UI
  tree to the schemas below. The capabilities it standardizes — streaming chat,
  tool calls, human-in-the-loop, subagents — the dashboard already has natively over
  its own transport, so it offers no new capability for this design.
- **A2UI** (Google) and **Open-JSON-UI** (OpenAI) are **wire schemas** for the UI
  tree itself: declarative JSON describing components the host renders with its own
  native widgets, explicitly "without executing arbitrary code." Same trust and
  composition model as `LayoutTree` — a schema for the tree, no renderer or state
  opinion attached.
- **MCP-UI / MCP Apps** (Microsoft + Shopify; now an official MCP extension) is a
  different model: the server ships **HTML/JS (or a URL)** rendered in a **sandboxed
  iframe** — isolation-by-sandbox, an opaque embedded box rather than a placed cell.
  It maps to the dashboard's existing `<mcwidget>` / webapp-artifact surface, not to
  this layout mechanism.
- **json-render** (Vercel Labs, Apache-2.0) is the outlier: a **full framework**,
  not just a schema. It pairs a typed **catalog** (`defineCatalog`) with a
  **registry** (`defineRegistry`), a flat **spec** of the exact shape this RFC's
  tree uses (`{ root, elements: { id: { type, props, children } } }`), a state
  expression language (`$state` / `$bindState` / `$computed`), progressive streaming
  (`SpecStream`), and a devtools inspector. It is the closest existing system to the
  mechanism this RFC describes.

**json-render is inspiration, not adoption.** Its catalog-is-the-contract principle
and flat id-keyed spec are the same guardrail and shape this design relies on, and
it ships a Redux state adapter for the store this dashboard uses — strong evidence
this RFC's model is on the right axis. Two things keep it as a reference rather than
a dependency: (1) its `$state`/`$bindState` read from a shared, mutable state
document — the fat-value-on-the-bus shape §5.3 rejects in favor of a slot *pointer*
re-resolved at render; and (2) its catalog is generic UI primitives (`Card`,
`Metric`), while this mechanism's elements are heavyweight, host-backed panes
(`ChatPane`, `GitPanel`, a terminal) that need the `slot → ChatSlot → project`
resolution the host performs through the render-prop bundle (§4) — expressing that
through json-render's generic `$computed` grain works against the framework. Should
the element vocabulary ever decompose a pane into smaller catalog-style parts (§8),
json-render's `@json-render/core` spec and `SpecStream` are the reference to study —
an evolution of this model, not an abandonment, and out of scope here. This RFC's
proposal is not a choice against json-render: it is a more composable form of the
panel-tab mechanism (`usePanelTabs`) the dashboard already has. Nothing prevents
using json-render later for more granular UI; re-expressing the existing heavyweight
components in a json-render catalog would be a large, time-consuming refactor for no
gain at the current grain. json-render remains a good option to consider when the
design decides to render new element types at a finer level.

**The part none of these has an opinion on.** Every system above answers *how does
an agent describe a tree of UI components the host renders safely?* None answers the
question this mechanism turns on: **how the
non-UI code underneath is assembled and bound to those components.** A2UI and
Open-JSON-UI stop at the wire schema; MCP-UI hands it to a sandboxed iframe;
json-render's answer is a generic state store plus `$computed`, deliberately
domain-agnostic and therefore silent on typed, host-specific resolution; AG-UI
carries an untyped (`any`) state document. This design's distinctive contribution
is exactly there: the render-prop seam (§4) and the declared-capability contract
(§8) are a *typed* account of how an element's narrow slice of `ChatSlot` is
declared by the element and supplied by the host. That is the part worth building
rather than importing.

### 9.3 Reversible vs locked-in decisions

The build-vs-adopt answer differs between decisions that can be changed later at low
cost and decisions the persisted format locks in once users have saved layouts.
Naming which is which is what lets the layout-engine choice (§9.1) stay open without
holding up the first shippable slice.

**Reversible — changeable later without touching downstream work:**

- **The rendering engine underneath the tree.** Whether cells are placed by our own
  grid/drag/resize code or by dockview/react-mosaic is an implementation detail
  behind the `LayoutTree` + renderer seam (§5.1, §5.2). Swapping it later touches
  the renderer only — not the element vocabulary, not the subject/scope contract,
  not any downstream PR. If a library proves a better fit after PRs 1–3, those PRs
  become adapters against it and nothing downstream is rewritten. Leading with our
  own grid therefore keeps the first slice shippable while buying the four §9.1
  contract points, and leaves the library choice open.
- **Bundle-cost optimizations, the editor's interaction model, cell chrome.** All
  local to the layout package.

**Locked in once layouts are saved — decide deliberately:**

- **The persisted `LayoutTree` schema (§5.1).** Once users' configs hold saved
  layouts and PRs 5–11 build on the shape, changing it is a migration, not an edit.
- **The `Subject`/scope contract (§5.3)** and **the element vocabulary (§5.2)** —
  every downstream element and the §8 futures are written against them.

The consequence for the build-vs-adopt choice: **the locked-in part is the
serialization schema, not the grid code.** If PRs 1–3 persisted dockview's `toJSON`
shape or react-mosaic's `MosaicNode` as the saved-layout format, the schema would be
tied to a third party's format and its versioning and constraints (react-mosaic's
binary-split tree cannot even express the N-way grid §5.1 requires). Keeping the
persisted schema **ours**, with the renderer free to delegate to a library, puts the
changeable decision where the library is and the locked-in one where this design
controls it — which is why owning the schema and adopting (or not) the engine are
separate calls, and no off-the-shelf layout library defines the schema half for us.

A library, if adopted, is reached through an implementation adapter — a render-time
transform from this schema to the library's node shape (`LayoutTree -> MosaicNode`),
the same in-memory form as the existing `toTree`/`toEditModel` round-trip (§5.1) and
never itself persisted — so the saved bytes remain this design's format regardless of
what renders them. Owning the schema is therefore the choice that *avoids* lock-in,
not a cost of it: it keeps the engine swappable, whereas persisting a library's own
format would not keep the schema swappable.

## 10. Risks and invariants

- **Floor-guarantee regression test** — flag off, or no saved layout, renders the
  current page unchanged.
- **Lossless-geometry test** — a multi-cell grid survives an edit/reopen without
  collapsing.
- **No reimplemented view bodies** — page-owned panes render the real component via
  an injected function.
- **Header doubling** — a self-chromed component suppresses its inner header rather
  than dropping its cell frame.

## 11. Open questions

- Persistence granularity once this moves off local storage (per crewmate, or per
  crewmate per project).
