# Frontend conventions

Shared components, accessibility, security, data fetching, live-collection
identity, animation, styling, typography, and how a builtin app gets discovered.
Page structure is in [page-layout](page-layout.md); color and CSS-var rules are
in [theming-contract](theming-contract.md); user-facing strings are in
[i18n-catalog](i18n-catalog.md).

## The stack

React 18, Redux Toolkit, React Query (`@tanstack/react-query`), React Router v7,
Framer Motion, Tailwind CSS 4, Lucide React, DOMPurify, highlight.js, the Pierre
editor (`src/pierre/`), TypeScript, Vite 8. Read the pins from `website/package.json` rather than this list.

Prefer the library already here over a new dependency. Every addition is bytes in a
bundle a user downloads and a supply-chain surface someone has to review, and two
libraries doing one job is how a codebase ends up with two animation systems whose
transitions do not compose.

## Sovereign and transient surfaces

The dashboard is a **sovereign** surface: used daily with full attention, so it is
dense, keyboard-friendly and muted. Dialogs, settings and onboarding are
**transient** surfaces: used occasionally, so they are spacious and self-evident.
Judge a design against the kind of surface it is; the UX review lane applies the
same standard.

## Browser support

Chrome, Firefox, Safari and Edge. Use standard Web APIs only, and guard the
browser-specific ones (`typeof Notification !== 'undefined'`): an unguarded API
throws at module scope, so the page renders blank rather than degrading.

Text inside a React-rendered subtree (react-markdown output, the chat bubbles,
the markdown preview) is painted with the CSS Custom Highlight API
(`CSS.highlights` + `Range`, styled by `::highlight()`), never by inserting a
`<mark>` around it: splitting a React-owned text node breaks the next React
commit that touches it. The API needs Chrome/Edge 105, Safari 17.2 or Firefox
140; older browsers paint no highlight, while match counting and stepping to
the current match keep working (`utils/domHighlight.ts`).

Pierre draws a fenced code block into a shadow root. A `TreeWalker` never
enters it, and a document selection reports a drag inside it as collapsed.
`utils/pierreShadow.ts` therefore reads the block's source lines for the preview
find, which needs no API newer than the one above, and resolves a selection with
`Selection.getComposedRanges`, which needs Chrome/Edge 137, Safari 17 or
Firefox 142; older browsers open no selection toolbar over a code block.
`SelectionToolbar` takes such a selection only from a host that passes
`codeSelection` (the file panel's markdown preview), because the other hosts'
comment anchors cannot resolve it.

## Shared components

`src/components/ui.tsx` is the primitive set. Compose from it rather than
hand-rolling:

`Card`, `CardTitle`, `Btn`, `SendBtn`, `IconButton`, `IconButtonGroup`, `Input`,
`SearchInput`, `Badge`, `SourceBadge`, `StatCard`, `Skeleton`,
`ContentSkeleton`, `SkeletonToggleRow`, `SkeletonField`, `SkeletonInfoRow`,
`FormSkeleton`, `EmptyState`, `PanelSectionHeader`, `PageHeader`, `Toggle`,
`Slider`, `Checkbox`, `FilteredEmpty`.

There is deliberately no `Select` primitive: use `SimpleSelect`,
`SettingsSelect`, `SearchableSelect`, or `SettingsMultiSelect` for a searchable
checkbox list in Settings.

`SimpleSelect` accepts optional decorative `optionIcons` alongside its text
labels. Desktop rows and the selected value show those identities; touch devices
retain the native text option list and show the selected icon beside the control.
An icon does not replace the option's accessible name or typeahead text.

The provenance pill is **`SourceBadge`**, not a badge named after any one source.
Two implementations exist on purpose:
`ui.tsx`'s takes a required `source` string plus optional `children` (a label
shown instead of the raw value), `tone` (`'auto'` | `'neutral'`, which forces the
grey style) and `title`; `SimpleSelect`'s `optionBadges` renders it with a badge's
`hint` forwarded to `title`;
`components/SourceBadge.tsx`'s takes an optional `source` plus `children`, so a
caller can render highlighted or translated label content over the same color
mapping. Both fall back to a neutral pill for an unrecognized source, so a new
source value degrades rather than throwing.

`PanelSectionHeader` is the one idiom for a counted list-section header inside a
side panel (label, count node, hairline rule). Route a new panel section through
it. The Files and Artifacts tabs each grew their own and silently diverged on
case, size, color, and whether the count was a node or punctuation baked into the
translated label.

Other shared modules:

- `ErrorNotice.tsx` (error text and its opt-in agent hand-off). In a
  height-constrained block notice, `scrollMessage` makes only the message
  scroll, keeping the built-in hand-off outside that scroller. Use
  `actionPlacement="below"` to keep the hand-off on its own line; the host owns
  the height constraint and any separate recovery controls. The option defaults
  to false and has no effect on inline notices. `messageClassName` alone cannot
  constrain the text column's height while reserving room for the hand-off.
  `messagePlacement` is `'beside'` by default; `'below'` puts the raw message on
  its own line under a plain-language `title`, and is ignored without a `title`.
- `Clickable.tsx` (accessible clickable div; see below)
- `SegmentedControl.tsx` (sliding pill, Framer Motion) — see the switcher rule below.
  It is a WAI-ARIA radiogroup: each segment is `role="radio"` with `aria-checked`
  and a roving `tabIndex`; arrow keys, Home and End move focus without committing
  a value, and disabled segments are skipped. `ariaLabel` / `ariaDescribedBy`
  label the group, and `wrap` lets full labels wrap rather than hiding them
- `ui/tabs.tsx`, `Tablist.tsx`, `ui/tabsPill.ts` (the other two switchers and their
  shared class recipe) — see the switcher rule below
- `DetailPanel.tsx` (resizable side panel with animated open/close)
- `SidePanelLayout.tsx` (shared side-panel page layout)
- `AgentSelector.tsx` (portal dropdown with ARIA)
- `layout.ts` (`LAYOUT` numeric constants: nav widths, sidebar width, max message
  width, topbar height, log line cap)
- `InfoTip.tsx` (the one help-tip pattern: hover or focus reveals, click or tap
  pins, Escape dismisses, and the tip text is the glyph's accessible
  description — see [below](#help-text-description-or-infotip)),
  `MarkdownRenderer.tsx` (the markdown renderer, with highlight.js
  syntax highlighting; its owners are mapped [below](#the-markdown-renderer)),
  `TypewriterText.tsx`
- `ResizeHandle.tsx` + `hooks/useColumnResize` (the drag grip between two PANES)
- `ColumnResizer.tsx` + `hooks/useTableColumnWidths` (the drag grip on a TABLE
  column) — see below

### User-resizable table columns

A data table whose values get truncated lets the user drag its column
boundaries: `useTableColumnWidths(storageKey, specs)` holds the overrides (one
`localStorage` key per table) and `<ColumnResizer>` is the grip. On a `ui/table`
table, render the header cell as `ResizableTableHead`, or pass `style` and
`resizer` to `SortableTableHead`; both live in `SortableHeader.tsx`. Rendered markdown tables are the
second `ColumnResizer` consumer, through `components/markdown/useMarkdownTableColumns.tsx`.

It assumes a **fixed-layout** table in the shape the Schedule jobs table
documents: every resizable column declares a px width, exactly one column
declares none and absorbs the spare, and the table's `min-width` is the px
columns plus a floor for that residual. Three rules follow, and the first two
are what a review should check:

- **Move the table's `min-width` by `cols.extra`.** A fixed layout does not
  shrink content to fit, so a column that grows while `min-width` stands still
  takes its pixels out of the residual column and draws that column's content
  over its neighbour. Widening a column must cost horizontal scroll, never
  another column.
- **Each `base` restates that column's `w-[Npx]` class, and a test holds them
  equal** (`SchedulePage.columnContract.test.ts` is the model). The classes stay
  the source of the defaults, so an untouched table renders exactly as it did
  before it was resizable: `style()` returns `undefined` and `extra` is `0`.
- **Leave the residual, a checkbox gutter and a pinned `sticky` column fixed.**
  The residual has no width to drag, and a pinned column's overflow cue anchors
  on its literal width.

An **auto-layout** table cannot adopt this by adding grips: there a width is a
hint the browser renegotiates against content, so a drag would not track the
pointer. Migrate it to the fixed-layout shape first.

A resizable header cell spells `relative` in its own class string, literally:
the grip is absolutely positioned and resolves against the nearest positioned
ancestor, and `shadcn/require-static-classes` rejects a className a
design-system component builds from an opaque value -- so the header components
pass `className` through untouched and the column-contract test holds every
resizable header to it.

The grip is the ARIA window-splitter widget (focusable, arrow keys, Enter or a
double-click to reset one column). Because it is focusable content with its own
label, a header cell that hosts one needs `aria-labelledby` pointing at its
visible label, or the grip's name is appended to the column header and announced
with every cell. The two header components above already do this.

`src/kirocrew-ui/index.ts` re-exports the subset that apps may import as
`@kirocrew/app-sdk/ui`. Adding a primitive there makes it app-facing API, so add
deliberately.

`src/app-sdk/ChatEmbed.tsx` keeps its composer markup small but delegates draft
behavior to `app-sdk/useComposerDraft`. Its textarea attaches the hook's
`textareaRef`, so `Enter` sends, `Shift+Enter` inserts a newline, IME commits do
not send, and the draft grows to the shared 240px cap before scrolling. Do not
reimplement those key or sizing rules inside the component.

Stories for these primitives live in `src/stories/` and render them in isolation
under every theme (`npm run storybook`); see
[testing § Component stories](testing.md#choosing-a-layer). Seven primitives have
one today. A story is the cheapest place to look at a new variant or prop, so add
or update one when you touch a primitive that has one; a per-primitive
requirement is not in force until the change that makes CI render stories.

### Help text: `description` or `InfoTip`

A control's caption carries two kinds of prose, and the Settings primitives
(`settings.tsx`) give each its own prop. `description` stays on the row at all
times; it is for the sentence a reader needs to MAKE the choice — a consequence,
a cost, where data goes, a status such as "disabled by instance config". `hint`
renders an `InfoTip` beside the label; it is for what the control IS, which a
reader wants once and never again. A row that always shows two lines of prose
spends attention whether or not it is being read, so the default for an
explanatory sentence is `hint`, one catalog string per tip. An error is neither: it renders through
`ErrorNotice` (`AUTOSDE.yaml`, `errors-use-error-notice`), never behind a tip.

`InfoTip` is accessible by construction — keyboard focusable, named by a short
generic phrase, described by the tip text (the open tooltip, or `title` while it
is closed), pinnable by tap for touch — so a surface outside Settings reaches
for the same component rather than a bare `title` attribute or a hand-rolled
hover.

`InstantTip` does not show its hint on a touch tap: the mouse events a browser
replays for a tap are ignored, because a hint appearing mid-tap can cost the click
(a real mouse on a touch device still hovers). An anchor whose bubble outranks its
click — one with no click action, or whose tip carries a warning the user must see
before acting — opts in with `openOnTap`.

### The markdown renderer

`components/MarkdownRenderer.tsx` is the renderer's only import path. Its default
export (the memoized component), its named exports and its module id are what
the consumers import and what about 150 specs mock, so all three stay there. It
is also the composition root: what has to be decided in one place lives in it,
and every other concern has one owner under `components/markdown/`.

| Owner | Holds |
|---|---|
| `MarkdownRenderer.tsx` | the ordered remark and rehype chains, and the parser built from them that the source repairs read (`AUTOLINK_PARSER`); `MD_COMPONENTS`, the element-to-renderer map, with the fence `code` override and the link overrides `MdAnchor` / `MdParagraph`; the per-block source passes and their order (`MarkdownBlock`); fence dispatch (`BlockRenderer`); the root component and its providers |
| `markdown/contexts.ts` | every context the pipeline's modules share, each created once; the facade re-exports the six public ones (`MediaApprovedCtx` stays private to `markdown/remoteMedia.tsx`, which both provides and reads it) |
| `markdown/linkTargets.ts`, `markdown/pathReferences.ts` | what a link or code span points at: artifact routes, unfurl eligibility, open sessions, sidebar folder paths (folder-chip resolution); path candidates, `file:line` suffixes, probe resolution and activation |
| `markdown/sanitize.ts` | the tag and attribute allowlist (`rehypeSanitize`) and `remarkVerbatimUnknownTags` |
| `markdown/treeTransforms.ts` | fenced-code marking, block unwrapping, soft breaks, source positions, position-stable root keys |
| `markdown/streamingEffects.ts` | the streaming tail's glow, reveal and caret |
| `markdown/linkBoundaryRepair.ts` | the source-level link repairs gated on remark's own parse: CJK autolink boundaries and refused link destinations |
| `markdown/elements.tsx` | the restyle-only element overrides and the sanitizer-derived attribute forwarding they share (`sp` / `spa`) |
| `markdown/typography.ts` | the heading, list, table-cell and inline-run class strings `markdown/elements.tsx` and the Office preview's `OfficeBlockList` (`FileRenderers.tsx`) both render; `markdownTypographyParity.test.tsx` pins the two surfaces to it |
| `markdown/InlineCode.tsx`, `markdown/copyFeedback.tsx` | the inline-code chips (path, session, work item, sidebar folder (`FolderChip`), copy) and the copy outcome every chip shares |
| `markdown/MarkdownTable.tsx` | a table, its Markdown / CSV copy row, and the Widen / Narrow toggle |
| `markdown/useMarkdownTableColumns.tsx` | resizable table columns (the second `ColumnResizer` consumer) |
| `markdown/markdownSniff.ts` | whether an untagged or generic-tagged fence holds a markdown document, so the transcript may offer it the Formatted \| Raw card |
| `markdown/ImgWithFallback.tsx`, `markdown/remoteMedia.tsx` | images (local-path routing, the layout reserve, the broken-image chip) and the click-to-load gate for remote images, video and audio |
| `markdown/MermaidBlock.tsx` | lazily loaded mermaid, its `initialize` config (`securityLevel: 'strict'`), and the diagram's box and font gates, source view and downloads |
| `markdown/Lightbox.tsx` | the image viewer and `dispatchLightbox` |

Imports run one way. The facade imports the owners, and nothing under
`components/markdown/` imports the facade: a module that did would receive the
stub in every spec that mocks the renderer. Consumers keep importing from the
facade for the same reason. The one exception is `markdown/typography.ts`, which
`FileRenderers.tsx` imports directly: it is plain data with no imports of its
own, and reading it through the facade would hand the Office preview the
renderer's stub in every spec that mocks the renderer. Three things must stay in the facade's own text: the
seven remark/rehype package imports (`test/test_source_providers.py` pins that
set against the backend converter), `import '../utils/hljs'`
(`hljsCoreOnly.test.ts`), and the two lines the i18n added-line gate counts, in
`MdAnchor` and `stripStrayToolUseTags`.

### The file panel, the chat pane and the browser panel

`components/MarkdownPanel.tsx` (a side-panel file tab), `components/ChatPane.tsx`
(one chat session in split view or a mate's chat) and
`components/WebPreviewPanel.tsx` (the Browser tab) are facades over sibling owners
in `components/`. Each facade keeps its module id, its default export and every
named export: hosts import it, and the specs that mount a host mock it by that id,
so a consumer never imports an owner. Imports run one way, from the facade to its
owners. Each owner hook is called once per render at a fixed place in its facade,
so the panel's effects keep one order.

| Owner | Holds |
|---|---|
| `MarkdownPanel.tsx` | `Props`, the panel and its two layouts (side panel, full screen), the edit, save, close and navigate guards, comment drafts and their anchor highlight, the line reveal |
| `MarkdownPanelHeader.tsx` | the header's controls: the breadcrumb, the artifact and knowledge toggles, the ⋯ menu |
| `useMarkdownPanelFileState.ts` | the file's knowledge-library and artifact-library state |
| `MarkdownPanelBody.tsx` | the body's blocks: comment hint, missing-file banner, empty-diff notice, diff view, comment overlay, find bar |
| `markdownPanelSourcePos.ts` | where a selection sits in the source (`resolveSourcePos`, `findCoords`) |
| `useMarkdownPanelFind.ts` | the CSS Custom Highlight registry and preview find (Cmd+F) |
| `useMarkdownPanelDisk.ts` | the disk truth (the panel's reads, the catch-up latch, the watch, the missing-file retry, the last-copy rescue) and the working-tree diff against HEAD |
| `useImageBoxComments.tsx` | image box comments: the box draft and its typed text (`useImageBoxDraft`), and the host the rendered images read plus the box composer (`useImageBoxHost`) |
| `useCommentHint.ts` | who sees the comment hint, with which wording, and its New badge |
| `ChatPane.tsx` | the pane's layout and dock, its store reads, the follow-up chips, the queue-card actions, and the transcript mount (`transcript={{ … }}`) |
| `ChatPaneProps.ts` | the props interface and `PaneLeading` |
| `useChatPaneDraft.ts` | the pane-local composer draft, parked and restored per slot |
| `useChatPaneUploads.ts` | uploads, drops, and their per-slot notices |
| `useChatPanePickers.ts`, `ChatPanePickerMenus.tsx` | the agent and model pickers' data and actions, and their two portaled menus |
| `useChatPaneSend.ts` | send, steer, the slash commands the pane intercepts, the message quote, the question card's one-click answer (its 404 fallback, `onFallbackSend`, stays in `ChatPane.tsx`) |
| `useChatPaneTurnControl.ts` | the Stop press protocol and the run-state reconciliation |
| `useChatPaneContinue.ts` | the pane's side of Resume: the composer's and the newest error row's offer for the pane's own slot, and the notice when the gateway refuses it. The press and its spinner are `hooks/useContinuePress.ts`, the one ChatPage's `useTurnRecovery` uses too |
| `ChatPaneTranscript.tsx` | the transcript row set and the rows above the first message |
| `WebPreviewPanel.tsx` | the panel: address bar, frame, device presets, liveness probe, launcher, the CLI browser view |
| `webPreviewPanelUrl.ts` | what a target may be: URL coercion, the loopback and embed rules, the self-origin check, the session feed (`setSessionPreviewUrl`, `setSessionPreviewPending`) |
| `useWebPreviewPanelAnnotate.ts` | Annotate: per-slot notes on picks in the native page |
| `WebPreviewPanelNativeSurface.tsx` | the native browse surface |

New work for a responsibility in the table goes to its owner, and work that
belongs to none of them starts in the facade. Two kinds of line stay in a
facade's own text: the ones the i18n added-line gate counts (the comment-highlight
paint rules in `MarkdownPanel.tsx`; `DEVICE_PRESETS` and `RELOAD_PARAM` in
`WebPreviewPanel.tsx`), and the constructs a source-reading spec pins there (in
`ChatPane.tsx` the transcript and dock wiring and the question card's
`onFallbackSend` with `answerRejectedMessage`; in `MarkdownPanel.tsx` the reveal
effect, the header's `onToggleDiffSplit` call site and the `pathBasename` use).

### Which switcher

Three components render the same pill, because a user should see one control for
"change what I am looking at". They are not interchangeable, and the choice is
about ACCESSIBILITY SHAPE, not looks:

| Use | When | Why not the others |
|---|---|---|
| `ui/tabs.tsx` (Radix) | Each tab owns its own panel | The only one that wires `aria-controls` ⇄ `aria-labelledby`, so the panel is announced as the tab's. It emits `aria-controls` UNCONDITIONALLY, so a `TabsList` with no matching `TabsContent` points every trigger at an element that does not exist |
| `Tablist.tsx` | Navigation, but the body below is ONE shared subtree parameterised by the active tab (see `WebhooksPage`) | A tablist and nothing else. Use it exactly where Radix's unconditional `aria-controls` would dangle; `aria-controls` is recommended by WAI-ARIA, not required |
| `SegmentedControl.tsx` | A FILTER over one view — which subset am I looking at | Not navigation: no panel relationship, and it measures its parent to collapse to icons then a dropdown, which the two above do not |

All three take their metrics from `ui/tabsPill.ts`, so they cannot drift apart
visually — `src/test/tabsPillParity.test.tsx` pins that. Do NOT hand-roll a
fourth: a `border-b-2` row of buttons has no keyboard model and no selected state
for assistive tech, which is the defect this consolidation removed.

Radix Tabs uses a static selected indicator when reduced motion is requested.
Setting a shared-layout spring's duration to zero still permits projection
transforms; those can cover another tab after a layout change. Memory record
selection panels and cards likewise disable layout projection in this mode.
The rail owns the stacking context for its indicators, so a sliding background
stays behind every tab label while crossing between segments.

A navigation rail sits in `TABS_RAIL_ROW_CLASS` (rail, rule, then content). The
rule is load-bearing rather than decoration: it is the only thing telling a
navigation rail apart from a filter pill, and the System page stacks both.

### The About, Chat and MCP settings panels

`pages/settings/AboutPanel.tsx` (Settings ▸ About), `pages/settings/ChatPanel.tsx`
(Settings ▸ Chat) and `pages/settings/McpManagement.tsx` (Settings ▸ Developer ▸
MCP Management) are facades over sibling owners in `pages/settings/`. Each facade keeps its
module id and every export, so hosts and the specs that mock a panel keep naming the
facade. Nothing outside a panel's own files imports one of its owners, and no owner
imports its facade; the only edges between owners are `AboutPanelGatewayUpdate.tsx` to
`AboutPanelUpdateFlow.tsx`, the three `useChatPanel*` hooks to `chatPanelOptions.ts`, and
the two MCP views to `mcpManagementModel.ts`. A facade calls each owner hook once per
render, in a fixed place among its own hooks, so each panel registers its hooks in one
fixed order.

| Owner | Holds |
|---|---|
| `AboutPanel.tsx` | the panel: the identity hero and both channel switchers, the desktop update card and its errors (`UPDATE_ERROR_KEYS`), the auto-download and auto-update switches |
| `AboutPanelUpdateFlow.tsx` | `InAppUpdateFlow` (arm, approve on the host, apply), `AgentUpdateRequestCard`, `RestartGatewayButton`, `copyCommand` |
| `AboutPanelGatewayUpdate.tsx` | a gateway install's update flow: its state hook, the Updates card body, the apply confirm |
| `ChatPanel.tsx` | the rail (`railItems`), every page's `case` with its controls, the save-error banner, the composer, About-you and session toggles' state |
| `chatPanelOptions.ts` | option tables, bounds and the config shapes the controls read |
| `ChatPanelLinkPatternsEditor.tsx` | the `dashboard.link_patterns` rule editor |
| `useChatPanelDashboard.ts` | the `dashboardConfig` read and writes, Feature Tips and the feature-video readout |
| `useChatPanelTypedFields.ts` | the draft-backed number and path fields (Advanced, Sessions) |
| `useChatPanelModels.ts` | the Model page: fallback models, defaults, selectable models, the agent-pin notice, per-role rows |
| `McpManagement.tsx` | the page: the view tabs, the sharing and pre-resolve cards, the servers table, the Share all list |
| `mcpManagementModel.ts` | wire types, error-key tables, the row-state derivation and the sharing-evidence verdict |
| `McpManagementLaunchReview.tsx` | a row's share button, its Details (the agents that declare it), and its hint |
| `McpManagementAssessment.tsx` | the Sharing assessment view and its Measure control |

New work for a responsibility in the table goes to its owner, and work that belongs
to none of them starts in the facade. Three kinds of construct stay in a facade's own
text, because a generator or a gate reads them there: every Settings* primitive and
settings-search anchor (`website/scripts/settingsExtract.ts` maps files by name), the
Chat rail's `railItems` (`website/scripts/gen-ui-index.mjs`), and the lines the i18n
and unit-literal added-line gates count. The tier labels and the contrary-tier set
that `test/test_mcp_shareability_frontend_parity.py` pins live in
`mcpManagementModel.ts`.

### The Security and Remote Crew settings panels

`pages/settings/SecurityPanel.tsx` (Settings ▸ Security) and
`pages/settings/RemoteCrewPanel.tsx` (Settings ▸ Remote Crew) are facades over sibling
owners in `pages/settings/`, with the same contract as the panels above: each facade
keeps its module id and re-exports every name callers import from it, nothing outside
a panel's own files imports one of its owners, and no owner imports its facade. Every
component moved whole, so each one registers its hooks in its BASE order.

| Owner | Holds |
|---|---|
| `SecurityPanel.tsx` | the rail: `SECURITY_SECTIONS`, `SECTION_LABEL_KEY` (read by `gen-ui-index.mjs`), `SECTION_GROUP_KEY`, the rail summaries, the panel, and the Documentation section |
| `SecurityPanelPosture.tsx` | Live security posture, and the status row and chip the tailnet card reuses |
| `SecurityPanelApprovals.tsx` | the ad-hoc auto-approve duration card |
| `SecurityPanelDeniedCommands.tsx` | Denied commands: the built-in rules by category, the custom deny patterns, the pinned and floor tooltip keys |
| `SecurityPanelTailnet.tsx` | the tailnet origin card and `TAILNET_STATE_KEY`, which the rail summary reads |
| `SecurityPanelTrust.tsx` | third-party app trust grants, per-registry Git-identity trust grants, and their failure messages |
| `SecurityPanelRedaction.tsx` | the credential-redaction switch, the allowed long-query hosts, `RedactionSection` |
| `SecurityPanelLayers.tsx` | the defense-in-depth layer registry (`FEATURES` and its catalog keys) and its section |
| `SecurityPanelGovernance.tsx` | the governance-policy viewer, its scope labels and planes, the central-distribution block |
| `RemoteCrewPanel.tsx` | the panel component: both tabs, every query and mutation, the launch form |
| `remoteCrewPanelModel.ts` | launch-job predicates, the remembered launch-form keys, the size tiers, the sign-in failure sentences |
| `RemoteCrewPanelLaunch.tsx` | the setup tab's size, provisioner and prerequisite cards, the launch progress card, the sign-in block |
| `RemoteCrewPanelCrewRow.tsx` | one crew row (badges, lifecycle actions, inline edit) and the fargate turn-API URL field |

The edges between owners are `SecurityPanelTailnet.tsx` to `SecurityPanelPosture.tsx`,
`SecurityPanelGovernance.tsx` to `SecurityPanelDeniedCommands.tsx`,
`SecurityPanelDeniedCommands.tsx` to `SecurityPanelTrust.tsx`, and the two Remote Crew
component owners to `remoteCrewPanelModel.ts` (with `RemoteCrewPanelCrewRow.tsx` to
`RemoteCrewPanelLaunch.tsx` for the sign-in block). Unlike the panels above, these
owners keep their own Settings* primitives and settings-search anchors: each `.tsx`
owner is mapped to its facade's tab in `PANEL_TAB_MAP`
(`website/scripts/settingsExtract.ts`), so the extractor and the manual-anchor check
read them where they are. The size tiers `test/test_cloud_sizes_ui_parity.py` pins
live in `remoteCrewPanelModel.ts`.

## Accessibility

Every interactive element MUST be keyboard accessible. Use `Clickable` from
`src/components/Clickable.tsx` instead of `<div onClick>`; it applies
`role="button"`, `tabIndex`, Enter/Space handling and `aria-disabled` together, so
the three can never drift apart.

```tsx
// Good
import Clickable from '../components/Clickable'
<Clickable onClick={handler} className="...">Click me</Clickable>

// Bad: not keyboard accessible, fails jsx-a11y lint
<div onClick={handler} className="...">Click me</div>
```

`Clickable` self-activates only on keydowns whose `target` is the element itself,
never ones bubbling up from a focusable descendant. Without that guard a container
would hijack a nested control's native activation, and its `preventDefault()`
would swallow spaces typed into a nested input.

For an animated interactive element, wrap `Clickable` with Framer Motion. It
forwards refs and spreads props, so animation and a11y compose:

```tsx
import { motion } from 'framer-motion'
import Clickable from '../components/Clickable'
const MotionClickable = motion.create(Clickable)
```

Rules:

- Never `<div onClick>` or `<span onClick>` without `role="button"` + `tabIndex` +
  `onKeyDown`. Prefer `Clickable`, which handles all three.
- Every icon-only button needs an `aria-label` describing the action.
- Modals need `role="dialog"`, `aria-modal="true"`, an `aria-label`, Escape
  dismissal, and a focus trap. `Modal` carries all four, plus keyboard isolation
  from the page's global chords — but that isolation follows the React tree, so
  an overlay rendered as a *sibling* of `<Modal>` is outside it. See
  [Keyboard isolation](#keyboard-isolation-dialogs-and-the-overlays-above-them).
- Dynamic content that updates in place (streaming messages, notifications) uses
  `aria-live="polite"`.
- A navigation rail (a column of buttons that each open a section or page) gives the
  row of the open destination `aria-current="page"` and no other row. A class or an
  inline style alone paints the row, and assistive technology cannot read it. A list
  that has opened nothing yet, such as the phone's root list in `SidePanelLayout`,
  has no current row. The open row of a list inside one view takes
  `aria-current="true"`.
- Do not use a raw `<button>`. Use `Btn` / `SendBtn` / `IconButton` (which carry
  the styling), or `Clickable` for a div-based control.

Tooling: `eslint-plugin-jsx-a11y` reports violations at lint time, and
`@axe-core/react` scans the live DOM in dev mode (findings land in the browser
console). Neither replaces a keyboard pass over a new control.

## Keyboard isolation: dialogs, and the overlays above them

The page binds its global shortcuts on a **bubble-phase `document` keydown**
listener (`useKeyboardShortcuts`), and several chords deliberately fire while an
input has focus — the Ctrl+digit session jumps and the Settings chord among
them. A dialog holding unsaved input must stop those chords, or one mistyped
Ctrl+digit navigates away and unmounts the dialog with the draft still in it.

`Modal` owns that boundary for its consumers: `ModalDialog` puts a bubble-phase
`onKeyDown` on the dialog **panel**, so every `Modal` consumer inherits it
without wiring anything.

**The boundary follows the REACT tree — not the DOM tree, and not the stacking
order.** React routes synthetic events through the React tree even across a
portal, so what decides coverage is where a component sits in JSX:

```tsx
<Modal open={open} onClose={close} title="…">
  …
  <SimpleSelect … />   {/* COVERED: a React descendant. Its popup portals to    */}
</Modal>                {/* the body-level overlay root at z-[9999], and is      */}
                        {/* still covered,                                        */}
                        {/* because coverage is about the React tree.            */}
{pickerOpen && (
  <ProjectPicker … />   {/* NOT COVERED: a React SIBLING. It paints above the    */}
)}                      {/* dialog but Modal's panel handler is not an ancestor  */}
                        {/* on its dispatch path, so it needs its OWN boundary.  */}
```

Both of those overlays portal to the body-level overlay root (see *Floating
layers and Crew switches* below) and both paint above the dialog
at the same `z-[9999]`. Only one of them is inside the boundary. **Sharing a
stacking context is a paint-order fact and implies nothing about event
routing** — conflating the two is what kept #6833 open, so do not reason about
coverage from a z-index.

A dialog rendered inside the App root rather than portalled to the body level
shares the shell's stacking context, so it uses the `z-[65]` band: above the chat
chrome (at most `z-[63]`) and below the `z-[100]` shell takeover layer, which a tie
would otherwise cover by document order. `src/components/AppRootDialogZIndex.test.tsx`
pins it. `Modal`'s own `z-[100]` is correct because it portals.

When you add an overlay that must appear above a dialog:

1. **Prefer rendering it inside the `<Modal>`'s children.** It then inherits the
   boundary, and nothing further is needed. A portal still escapes an
   ancestor's `clip-path` / `transform` / `filter`, so being a React descendant
   costs you no stacking freedom.
2. **If it must be a sibling** — because it anchors to something outside the
   dialog, or its lifecycle is owned above it — give its portal root the same
   guard. `ProjectPicker` is the reference implementation:

```tsx
const isolateKeys = (e: React.KeyboardEvent) => {
  if (e.key === 'Escape') { ime.claimKey(e); return }
  e.stopPropagation()
}
return <OverlayPortal><div onKeyDown={isolateKeys} …>…</div></OverlayPortal>
```

Three properties of that guard are load-bearing:

- **Bubble phase, on the overlay's own root.** Capture-phase listeners must keep
  receiving keys: the Tab trap (`useDialogFocusTrap`, window capture) and list
  navigation (`useListKeyboardNav`, document capture) both run before the event
  reaches the target. A guard moved to capture phase, or onto `document`, would
  pass a naive test while silently killing arrow-key navigation and the trap.
- **Escape is excepted.** `Modal`'s own dismissal is a bubble-phase `window`
  listener, and `stopPropagation()` on a synthetic event stops the native event
  too — so a blanket stop breaks dismissal rather than isolating it. Leave
  Escape exactly as you found it and let the overlay's own dismissal path own
  it.
- **An Escape the IME owns is claimed, not forwarded.** Mid-composition it is
  cancelling a candidate list, not the dialog. Reuse the component's existing
  IME guard (`useImeGuard`) or `useDocumentImeLatch` when the composing input
  can be anywhere inside the overlay; do not hand-roll a second latch.

Focus containment is a **separate** mechanism with a **different** scope: the Tab
trap tests DOM containment (`container.contains(document.activeElement)`), so it
reclaims focus from a sibling portal back into the dialog regardless of the
keyboard boundary. A sibling overlay's own Tab handling therefore has to expect
the trap to have run first.

Pinned by `Modal.keyboardIsolation.test.tsx` and
`ProjectPicker.keyboardIsolation.test.tsx`. Both open with a control that fires
the same chord where the boundary is known to work — every other assertion in
them is a negative, and a negative is worthless if the harness never delivered
the key.

## Security: sanitize every HTML sink

All `dangerouslySetInnerHTML` content goes through DOMPurify, via
`src/api/helpers.ts`:

- `md(text)` renders markdown-like formatting and sanitizes the result.
- `sanitize(html)` is the DOMPurify wrapper for already-built HTML.
- `esc(text)` escapes plain text (use this when you do not need markup at all).

A bypass is an XSS bug, so there is no "just this once" case.

The markdown renderer sets no `dangerouslySetInnerHTML`. Raw HTML in markdown
prose enters its tree only through `rehype-raw`. In the chain
`MarkdownRenderer.tsx` composes (`rehypeBoundRawDepth`, `rehype-raw`,
`rehypeMarkFencedCode`, `rehypeUnwrapBlocks`, `rehypeSanitize`, `rehype-katex`)
the two passes between `rehype-raw` and `rehypeSanitize` only mark and restructure
the tree; they add no attribute the sanitizer would not judge. Every pass that injects
elements of its own (the streaming effects, the redaction markers, the stable
root keys) is appended after sanitize. Two fences take other paths: a widget
renders in `WidgetFrame`'s sandboxed iframe, and `MermaidBlock` inserts the SVG
mermaid drew under `securityLevel: 'strict'`. The allowlist and the verbatim pass
below live in `components/markdown/sanitize.ts`; the facade re-exports both, so a
second surface that admits raw HTML reuses the one policy instead of carrying a
copy. The allowlist admits `aria-*` and `data-*` attributes wholesale with one
exception: `data-message-*`, in any casing or dash spelling (the key is compared
with dashes stripped), is reserved for the transcript's own hooks and is dropped
from content before that allow.

The shared markdown pass `remarkVerbatimUnknownTags` preserves unknown single
tags as inert source text, including their case, bare attributes and quoted `>`
characters. Its single-tag recognizer scans each character with a fixed set of
attribute states; it must not backtrack over an entire raw HTML node. Malformed
block HTML reaches this check before the tag allowlist, so even an allowlisted
tag can carry an adversarial attribute sequence. This preserves the existing
permissive empty/unquoted-value handling without normalizing placeholders or
changing the separate sanitizer, executable-tag and multi-tag-block behavior.

## URL sanitization

`react-markdown` strips protocols it does not know. `src/utils/urlTransform.ts`
re-allows the editor deep links, `vscode:` and `vscode-insiders:`, and delegates
everything else to `defaultUrlTransform`. It also requires the URL to carry more
than the bare scheme, so `vscode://` alone is not treated as a link.

Add a new protocol to `ALLOWED_PROTOCOLS` in that file, and only there. Each
addition widens what a model-authored or user-pasted link can launch on the host,
so treat it as a security change, not a formatting one.

How a refused destination RENDERS is part of the contract (issue #9925): the
transform's rejection sentinel is `''`, and `MdAnchor`'s `!href` guard renders
the label as inert text with **no anchor** — never `<a href="">`, whose empty
href resolves to the current page — matching `md-notebook/Preview.tsx`'s
`href ? <a …> : <span>` trade. A test that pins an anchor existing for a
destination the transform rejects is pinning a defect.

One deliberate, key-scoped exception exists: a Windows absolute path
(`WINDOWS_ABS_PATH_RE` — a drive-letter path) is passed through **for image
`src` only**, because `defaultUrlTransform` parses `C:` as an unknown scheme and
would blank the sender's own uploaded image (issue #3497). The invariant that
makes it safe: `ImgWithFallback` (`components/markdown/ImgWithFallback.tsx`)
routes every local path to the same-origin `/api/file-raw` endpoint, so the raw
filesystem path never reaches the DOM, and
the shape (single letter + separator) cannot express `javascript:`/`data:`
payloads. Widening that regex or its key scope is a security change — the same
constant also decides which paths are treated as local file reads, so the two
decisions must stay on the one exported copy in `urlTransform.ts`.

A root-relative image (`/images/a.png`) inside a previewed file (a
`BasePathCtx` is set) is tried as an absolute path first, then against each
directory above the file, nearest first, up to eight levels
(`siteRootImageCandidates`). The leading `/` there usually names the root of the
repository or site the markdown belongs to — GitHub, Zenn and Docusaurus all
write images that way (issue #23623) — and nothing in the file says where that
root is. A same-named image in a nearer directory therefore wins over one at
the real root. Every candidate still goes through `/api/file-raw`, so the fallback
reads nothing the absolute path could not already ask for. When every candidate
fails, the broken-image fallback names and probes the path as written, as it
does for any other image, so its "no longer exists" wording speaks for that path
alone. That fallback states the failure through an inline `ErrorNotice` beside
a chip that copies the on-disk path. The notice offers no agent hand-off: the
renderer also previews unsaved drafts (the meeting note's Preview), and the
navigation would unmount one. Every surface that
provides `BasePathCtx` gets this (the file preview, the file explorer, the
meeting notes sidebar); outside them (chat, artifacts) a leading-slash path
stays absolute only.

To SHOW a gateway path's file name, use `pathBasename` (and
`stripTrailingSeparators` for a directory) from `utils/pathBasename.ts`, never
`.split('/').pop()`: a Windows gateway reports `C:\repo\a.ts`, which a `/`-only
split shows whole. The helper treats `\` as a separator only in a drive-rooted
or backslash-UNC path, because on POSIX a backslash is a legal file-name
character. It is display-only and decides nothing about which paths may be read.

## Serving under a URL sub-path (`KIROCREW_BASE_PATH`)

The dashboard can be built to live under a URL prefix (e.g. behind a reverse
proxy at `/proxy/kirocrew/`) with `KIROCREW_BASE_PATH=/proxy/kirocrew npm run
build`; see [Behind a reverse proxy at a sub-path](../../docs/guides/remote-and-mobile.md#behind-a-reverse-proxy-at-a-sub-path).
The value is read once at build time and becomes Vite's `base`. `src/lib/basePath.ts`
exposes it as `BASE_PATH` (`''` in the stock root build, so every helper is the
identity).

The transport shims (`installBasePathShims`) and DOM shims
(`installBasePathDomShims`) rebase `fetch`, `WebSocket`, `EventSource` and
URL-valued attributes automatically, so most call sites need no change. But a
dynamic `import()`, a `window.location` navigation, an `AudioWorklet.addModule`
load and a direct `window.location.pathname` read reach neither hook: wrap a
root-relative URL passed to those in `withBase()`, and read the app route with
`appPathname()` instead of `window.location.pathname`. `basePath.test.ts`
(*call sites the shims cannot reach*) scans the source and fails the build if an
unwrapped one slips in.

## Data fetching

The memory editor, member recall, Memory V2 and Crew Manager memory surfaces are
specified in [memory, skills and hooks](../../docs/system-specs/modules/memory-skills-hooks.md)
(*Memory dashboard surfaces*).

Always React Query (`useQuery` / `useMutation`) for server state. Do NOT use
manual `useState` + `useEffect` + `useCallback` for an API call. Prefer optimistic
updates through `queryClient.setQueryData`.

Query keys are arrays whose first element names the resource, kebab-case:
`['mcp-servers']`, `['agents-installed']`, `['agent-detail', name]`. Append the
parameters a fetch varies on, so a stale entry cannot serve a different subject.

Real-time updates arrive on a single WebSocket at `/api/ws`, read through
`useWebSocket`, which reconnects with capped exponential backoff (1s doubling to a
10s ceiling) and re-fetches state through Redux on reconnect instead of reloading
the page.

`src/hooks/useWebSocket.ts` is the composition point and the only import path:
it holds the frame routing table (one `case` per frame type), the silence
watchdog and the connect wiring, and it composes the owners in
`src/hooks/websocket/`. `connection.ts` owns the socket, its backoff and its
best-effort sends, and exposes the connection-scoped refs the open sequence and
the arms share; `reconnectCatchUp.ts` owns the first-connect and reconnect
sequences, in order, including their subscribe and focus frames;
`streamBuffers.ts` owns the per-frame coalescing of chat, reasoning, subagent
and sidebar-recency streams. Frame families live with their domain:
`chatStream.ts` and `turnCompletion.ts` (transcript frames, and what `chat_done`
means after its row), `approvals.ts` and `composerCards.ts` (coordinator
approvals; question, follow-up and folder cards), `slotList.ts`,
`bundleReload.ts` (the `dashboard` status frame), `serverState.ts` (the
server-owned caches), `automationSeed.ts` and `voicePlayback.ts`;
`workflowRuns.ts` reconciles the workflow rows those frames fold, `attention.ts`
owns the unread / read-relay rules and the focus senders, and `browserEvents.ts`
owns the window events that re-broadcast a frame. `frames.ts` decodes the
`/api/ws` envelope and types its `FrameData` for the router, and `retiredIds.ts`
holds the watermarked retired-id logs `approvals.ts` and `composerCards.ts`
share (and `resolvedSince`). `sessionProjection.ts` and `slotProjection.ts` apply
the `session_projection` and `slot_projection` frames to their caches,
`contextTraceRefresh.ts` decides when the Context tab re-reads its trace, and
`rowDeliveryWatchdog.ts` re-hydrates the active slot when a running turn stops
delivering rows over an open socket. The complete owner list is `OWNER_MODULES`
in `src/test/useWebSocket.ownership.test.ts`. The router's other arms are written
inline. A new
frame gets its `case` in the router; when it needs state an owner keeps, the arm
calls that owner (or reads a ref the owner exposes) rather than reaching into
it. An owner receives its dependencies (`dispatch`, `queryClient`, the socket
connection and peer owners) as arguments and never imports the facade; outside
`src/hooks/websocket/` only the facade imports an owner, and owners import each
other only along the edges `src/test/useWebSocket.ownership.test.ts` lists.

`src/types/index.ts` is the one import path for the shared types it serves, most
of them the shapes of REST and `/api/ws` payloads. That file declares nothing. It
re-exports each owner below with an explicit `export type { … } from` list
(`isolatedModules` is on, so a plain `export { … }` of a type does not compile).
A new type for one of these areas goes to its owner and gets a line in that
owner's list; `src/types/index.owners.test.tsx` fails until it has one. No owner
imports the facade. An owner that needs another owner's type imports it with
`import type` from that owner and imports nothing else, and nothing outside the
family imports an owner. A module beside them in `src/types/` that is not named
`index.<area>.ts` is not an owner and keeps its own import path.

| Owner (`src/types/`) | Holds |
|---|---|
| `index.gateway.ts` | the status push (`StatusData`), the update check, host metrics, session storage and its inventory, and each connected crew's version, capabilities, approval state and remote update |
| `index.jobs.ts` | scheduled jobs (`CronJob`), task-runner projects and their steps, dynamic-workflow runs |
| `index.capabilities.ts` | lessons and memory stores, skills and their budget, steering files, skill and MCP discovery, configured MCP servers and their scopes |
| `index.chat.ts` | the chat slot row (`ChatSlot`) with its turn identity and the `chat_done` payload, its todo list, MCP report and channel links; sidebar folders, tags and board lanes; transcript messages and the slot detail read; subagent and tool activity; content blocks; notifications and their channels; approvals; history session rows |
| `index.sources.ts` | pull requests, issues, the source provider id, an app repository's contributors |
| `index.artifacts.ts` | artifacts and their publication, publishing providers, forks, remote listings, session docs, folders, events, comments, web-app metadata |
| `index.sharedCrew.ts` | Shared Crew attribution: the browser-facing principal and its display snapshot, a session's creator, availability and trigger |

Redux Toolkit (`src/store/index.ts`) holds the cross-page shell state in **four**
slices:

| Slice | Owns |
|---|---|
| `dashboard` | SSE/WS connection state, chat slots, approval mode, optimistic slot add/remove, thunks for slot fetch and approval-mode change |
| `chat` | active slot, messages, session history with pagination, WS chunk/done handling, thunks for slot CRUD and history fetch/resume/delete |
| `notifications` | notification list with add/delete/clear plus their thunks |
| `instances` | the known Kiro Crew instances a user can switch between |

Server data belongs in React Query, not in a slice. Reach for Redux only when the
state is shell-wide and not a cached server read.

## Live-updating collections: merge, don't replace

A slice holding a collection the server re-broadcasts **in full** must merge the
incoming list into the one it already holds, never assign it. Assigning hands
every row a new object reference on every frame, so one row's change invalidates
every selector over the collection, every `useMemo` keyed on the array, and every
memoized child — and inside a Framer `LayoutGroup` it re-measures the entire list.
The symptom is a collection that visibly reloads when one member changed, which
reads as a bug rather than as an update. Broadcasts are coalesced server-side but
not suppressed (slots on a 200ms floor that stretches with the frame's size, up to
2s for a long list), so an active session delivers several full lists per second
and the effect is continuous rather than incidental.

What a merge has to hold:

- **Membership and order come from the incoming list.** The server stays
  authoritative on both; only per-row identity is carried across.
- **Reuse a row only when it is structurally equal**, so no consumer can read
  stale content off a kept reference.
- **Leave the array itself alone when nothing moved.** This is the half that
  pays: an equal-but-new array still reruns every downstream filter and sort.
- **Compare field-agnostically and independently of key order.** A comparator
  that enumerates the type's fields stops seeing a newly added one and pins a
  stale row — a correctness bug, where a redundant re-render is only a cost. A
  serialization compare calls a locally patched row unequal forever, because an
  in-place patch can append a key the server payload spells earlier. Use the
  shared `jsonEqual` (`utils/structuralEqual.ts`) rather than writing a second
  comparator — it already holds both properties, and a private copy is a second
  set of guarantees to keep in sync.

`applySlots` in `dashboardSlice.ts` is the reference implementation, driving both
authoritative writers (`sseSlots` and the `fetchSlots` refetch);
`dashboardSlice.slotIdentity.test.ts` pins the contract. Reusing an Immer draft
row inside a freshly assigned array is safe — a draft found in the assigned value
is finalized within the same scope, so an untouched row resolves back to its base
object and keeps its identity.

This is a convention for new and touched code, not a description of the current
store. One dashboard collection still assigns wholesale and is a known gap rather
than a counterexample: `sseStatus` replaces `state.status` as a whole object that
several panels subscribe to entirely. Converting it is worthwhile but is its own
change: its consumers need narrowing first, or the merge buys nothing.

Two habits belong to the same concern:

- **Subscribe narrowly.** A selector returning a whole map re-renders its
  component on any write to any member; pass `shallowEqual` when the component
  only reads members out of it, as the sidebar does for `slotStatusDetail`.
- **Don't re-rank a list on mid-turn activity.** An ordering key should move on
  settled events only, or rows swap under the pointer while several sessions work.

## Animations

- Framer Motion for orchestrated component transitions: enter/exit, layout
  animations, gesture-driven motion.
- Tailwind `transition-*` for simple state changes (hover, toggle, color).
- Tailwind `animate-*` for simple indicators (spin, pulse) and the shared
  `animate-rise` / `animate-scale-in` entrances.
- **An always-visible indicator never runs an unbounded animation.** An infinite
  `animate-pulse` / `animate-ping` / `animate-bounce` on something that stays on
  screen keeps the browser producing frames on an idle dashboard. Bound it with
  the important longhand `[animation-iteration-count:3]!` (the `!` makes the bound
  win whatever order Tailwind emits the rules in) plus `motion-reduce:animate-none`,
  or keep it static. `src/test/idleAnimations.test.ts` pins the top-bar and rail
  indicators.
- Do NOT add a new CSS `@keyframes`. The existing ones in the `src/styles/`
  partials back specific low-level effects (skeleton pulse, caret blink,
  indeterminate progress); a new component animation goes through Framer Motion.
- **Hover PAINTS; it never moves or resizes.** A hover state may change colour,
  border, brightness or shadow, but not `scale` or `translate` — growing a row
  under the cursor nudges its neighbours and reads as a layout change rather
  than "you are pointing at this". Press feedback (`whileTap`, `active:scale-*`)
  is fine: that answers an action the user took. A selected-state scale applied
  by STATE is an indicator, not a hover effect. A hover *rotation* is out of
  scope — it leaves the element's box where it is.
  - Removing a hover transform is only half the job: check the control still has
    SOME hover cue. On a small swatch or dot the scale is often the only one, and
    taking it away leaves a clickable thing that answers nothing.
  - Pick the cue by what the element can actually show. `brightness` is a no-op
    on a `transparent` fill (tint the border instead), and a class-based cue
    cannot beat an inline `style`, so an element whose colour is animated needs
    its cue on a property nothing animates.
  - **A hover cue must not reuse a colour the control uses for its SELECTED
    state — differ in colour, not merely strength.** A dimmer shade of the
    selected colour still reads as "selected" at a glance (a bright or accent
    mark is selection-grammar whatever its exact lightness), so hover must paint
    in a genuinely different colour, not a fainter one. The colour swatches show
    this: selection speaks in `--text-strong` (a near-white border) and
    `--accent` (a border or `ring-1 ring-accent`), so their hover cue paints in
    a neutral `--muted` outline — which is neither, and the lightest neutral
    token with enough contrast on the darkest fill — and the memory-record card
    hovers to `border-border-strong`, never its accent selected border. Where
    selection is an offset accent ring, the hover outline must also CLEAR it
    geometrically (`outline-offset:-3px` insets the line inside the fill) rather
    than sit at the ring's radius and mask it; do this structurally, not with an
    `:not([aria-pressed])` guard that silently misses a selected swatch marked
    by a conditional class alone. This is the same lesson as the left rail,
    where a full-strength `bg-bg-hover` read as selection and was fixed by
    weakening it to `/60` — applied to colour rather than strength.
  - `src/test/hoverNoScale.guard.test.ts` enforces this and names the fix in its
    failure message. Deliberate exceptions live in that file's ALLOWLIST with a
    written reason.

## Styling

Tailwind CSS 4, configured in CSS rather than a JavaScript config file. Two
files own it:

- `src/tailwind-theme.css` — the utility ↔ token bridge. Every `--color-*`,
  `--radius-*`, `--shadow-*`, `--font-*` and `--animate-*` theme key maps a
  utility (`bg-accent`, `rounded-md`, `shadow-sm`, `font-mono`, `animate-rise`) to
  the runtime design token of the same stem, so `text-muted/40` renders a
  translucent `var(--muted)`. It also declares the `dark:` variant
  (`@custom-variant dark ([data-theme="dark"] …)`, so dark mode follows the
  `data-theme` attribute rather than the OS media query alone), leaves `hover:` at
  Tailwind's default `@media (hover: hover)` gate (a tap that reveals content makes
  iOS drop the click, so a hover-revealed control carries its own
  `[@media(hover:none)]:` visible state instead; `src/test/hoverVariantPolicy.test.ts`
  enforces it; content revealed from a JS `onMouseEnter` or focus handler follows
  the same rule through `hooks/useTouchReplay.ts`: spread its `pointerProps` on
  the anchor and return early while `fromTouch()` is true, as `useNavTip`,
  `useHoverIntent`, `RefLink` and `MarkdownToc` do), and
  emits the iOS safe-area utilities (`p-safe`, `top-safe-offset-*`, …) as
  `@utility` blocks. Adding a utility for a new token means adding one
  `--color-<token>: var(--<token>)` line here; `scripts/check-phantom-classes.mjs`
  compiles against this file to catch a utility whose token was never declared.
- `src/index.css` — the entry, read top to bottom in cascade order. First the
  Tailwind header: it imports Tailwind's theme and Preflight into their cascade
  layers, emits `@tailwind utilities` UNLAYERED (see the header comment there:
  the component CSS after it was written against v3's unlayered utilities and
  must keep competing with them on plain specificity), and lists the template
  sources with `@source`. Then the Preflight compensation, three v3 Preflight
  defaults restored as token-backed base rules (default border colour
  `var(--border)`, placeholder colour `var(--muted)`, `cursor: pointer` on
  enabled buttons). Then the imports of `font-faces.css`, `design-tokens.css` and
  `theme-palettes.css`; then the `@layer base` element defaults (box model, the
  global `:focus-visible` ring, the reduced-motion cut, the scroll-only
  scrollbar)
  and, unlayered, the ambient backdrop and the app-icon tints; then the imports
  of the other eight partials. Tailwind inlines each `@import` where it stands,
  so the import order IS the cascade order: never reorder the imports, and put a
  new rule in the partial that owns its surface.
- `src/styles/*.css` — the partials `index.css` imports, listed here in import
  order. Each is one contiguous slice of the cascade, so some also hold a
  neighbouring rule that has no file of its own; those strays are named:
  - `font-faces.css` owns every `@font-face`: the script-fallback and
    straight-quote aliases, the brand faces and OpenDyslexic.
  - `design-tokens.css` owns the custom properties every theme shares and the
    default dark and light palette.
  - `theme-palettes.css` owns one `[data-theme]` block pair per named theme.
  - `chat-transcript.css` owns what a chat message paints and how it streams in.
    Strays: the composer's prompt-optimize overlay and approval glow, the update
    card's indeterminate progress bar, `.animate-shimmer`, the custom-font
    ligature toggle, the Kiro-theme overrides of nav, session-row and shell
    chrome, and the workspace-tree token mapping.
  - `scrollbars-and-touch-hit.css` owns the horizontal edge-fade mask for code
    and diff blocks and the 44px touch hit areas. Strays: the send-button sweep (`.btn-sweep`), the card
    hover glow (`.card-glow`) and the stat top-line accent (`.stat-accent`).
  - `shell-chrome.css` owns the top bar and its collapse ladder, the frameless
    Electron window chrome and focus mode. Stray: `.scrollbar-none`.
  - `focus-and-state-cues.css` owns the scroll-edge fades, the skeleton shimmer,
    the focus rings and the swatch hover cue. Strays: table striping, the
    follow-up chip width cap, the skill-load heading sizes and the highlight.js
    colours.
  - `sidebar-and-side-panel.css` owns session and folder rows, the list dock and
    the side-panel tab strip. Strays: the inline lucide icon size and the
    panel-icon pane morph.
  - `highlights-and-page-zoom.css` owns search, find and comment highlights and
    the coarse-pointer rules that stop page zoom. Strays: the artifact toolbar
    button heights and the mobile sessions-drawer width.
  - `liquid-glass.css` owns the glass material and every solid fallback
    (no `backdrop-filter`, reduced transparency, more contrast). Strays: the
    settings icon-tile and goal-mark tokens, the dock pass-through and the
    global reduced-motion transition reset.
  - `content-fit.css` owns the rules that make content fit its box: font-metric
    compensation on the nav rail, the crew-chip cut edge, image and spacer
    reservations, the pane-dim token and the markdown-table breakout.

  `cli-mode.css`, `message-font-size.css` and `crew-notes.css` in the same
  directory are imported by `main.tsx` instead. A test that measures the
  stylesheet's source text reads it through `src/test/indexCssSource.ts`
  (`readIndexCss()`), which splices every partial in at its import.

The build runs through `@tailwindcss/vite`; there is no PostCSS config. A
downstream edition's sources are added to the content scan by
`editionExtensionPlugin` in `vite.config.ts`, which swaps the
`/* @kirocrew-edition-source */` marker in `index.css` for an `@source` line.
Utility names follow Tailwind v4: `outline-hidden` (not `outline-none`) is the
accessible outline suppressor, `backdrop-blur-xs` is the 4px blur, and the
`shadcn/ui` primitives animate through `tw-animate-css`.

Colors come from CSS custom properties defined in `src/styles/design-tokens.css`
and `src/styles/theme-palettes.css`, including the
semantic roles `--aim`, `--clarify`, and the `--diff-*` family. Never a hardcoded
`#hex` / `rgb()` / `rgba()` literal, and never a raw palette class
(`text-green-500`, `bg-amber-400`): state colors are `text-ok` / `text-warn` /
`text-danger` / `text-info`, and a running state is `text-accent`. See
[theming-contract](theming-contract.md) for the variable set, the stable class
hooks, and the checkers.

No scrollbar is visible at rest, anywhere in the dashboard
([decision](../../docs/decisions/2026-10-23-scrollbars-are-never-always-visible.md)).
A scroll box either hides its scrollbar entirely (`scrollbar-none`: the sidebar
lanes, the nav rail, the tab strips) or shows a thin thumb only while it is being
scrolled, in a column it reserves anyway so the reveal moves no content. The
second form is the default and needs no class: the base layer in `index.css`
keeps the 6px WebKit column with a transparent thumb and paints it under
`[data-scrolling]`, which `lib/scrollReveal.ts` sets on the scrolling element
and clears after a short linger (Firefox takes the same two states through
`scrollbar-width: thin` and `scrollbar-color`, fenced behind the `-moz-appearance`
probe because Blink drops the pseudo-elements once those are set). Never add a
`::-webkit-scrollbar-thumb` colour or a `scrollbar-color` whose selector lacks
`[data-scrolling]`, never `scrollbar-width: auto`, and never a hover-revealed
thumb: the pointer rests over the chat transcript most of the time, so hover is
"at rest" there. The global rule and the document scroll listener both stop at a
shadow boundary; Pierre's code, diff and tree surfaces are reached through
`unsafeCSS` keyed on the same attribute (`PIERRE_SCROLL_REVEAL_CSS`, folded into
both option factories in `src/pierre/config.ts`, and the tree's
`PIERRE_TREE_STATE_ROW_CSS`), and `scrollReveal` attaches to those hosts' shadow
roots itself (`SHADOW_SCROLL_HOSTS`). When a box's edge does not make it obvious
that it scrolls, the cue is an edge-fade mask (`.edge-fade-x`, `useScrollEdges`)
or a "more below" affordance (`MORE_BELOW_MASK`, `JumpToBottomButton`), never a
resting thumb. `src/test/scrollbarRevealOnly.test.ts` scans `src/` for every
shape that paints one at rest and measures the revealed thumb's contrast per
theme; the `no-scrollbar-at-rest` rule in `AUTOSDE.yaml` blocks it in review.

Three of those rules are enforced by `@shadcn/lint` inside the blocking
`eslint src/ --max-warnings 0` gate (configured in `eslint.config.js`, the
`shadcn` block):

- `shadcn/no-raw-colors` — a palette class or a literal SVG `fill`/`stroke`
  where a token belongs. Use the token; a logo whose colors are the artwork's
  own gets a file-level override (see `KiroGhost.tsx`).
- `shadcn/no-unknown-classes` — a class Tailwind emits no CSS for. Usually a
  typo, a v3 spelling (`outline-none`, `resize-vertical`), or a class whose
  stylesheet was deleted. A class that IS real but lives outside the theme's
  import graph — an app stylesheet authored as a TS template string, a
  selector hook a Playwright spec locates by — is listed in the rule's `allow`
  with the file that owns it; the entry allows a name, it generates no CSS.
- `shadcn/require-static-classes` — a `className` on a `ui/` primitive built
  from a value the linter cannot read (an imported constant, a function call,
  an array `join`). Keep the class strings in the file that applies them: a
  shared class string becomes a small wrapper component (`FilterMenuLabel`),
  a helper call gets a `cn(...)`.

The same gate refuses browser-native dialogs: `confirm`, `alert` and `prompt`,
bare or as `window.*`, are errors through `no-restricted-globals` and
`no-restricted-properties`. Use `useConfirm()` / `ConfirmDialog` or an in-app
notice instead. A file allowlist holds the standing inventory and only shrinks:
delete a path when its last native dialog is gone, never add one. Tests
(`*.test.{ts,tsx}`, `src/test/`) are exempt.

`shadcn/no-restyle` — a `className` that changes what a `ui/` primitive owns
(its color, spacing, shape, typography) — is off in that gate: a few hundred
call sites restyle primitives today and the gate is a hard zero, so turning it
on is a design decision (fix the sites or write per-component contracts), not a
lint toggle. What IS enforced is that the backlog cannot grow.
`scripts/check-restyle-ratchet.mjs` (`npm run lint:restyle-ratchet`, run by
CI beside the phantom-classes gate) lints with the rule through its own config
(`allow: ['layout']`, so margins and widths pass) and holds every file at the
count recorded in `scripts/restyle-baseline.json`: a file whose count rises, or
a file with findings and no entry, fails the build; a file whose count fell
fails too, until you record the drop with
`npm run lint:restyle-ratchet -- --update-baseline`, which only ever lowers a
number or prunes an entry that reached 0 — so progress is locked in, not left
to a log line. Lowering a count is the only edit the script makes; the one hand
edit is moving an entry to a file's new path when the file moves (the count may
not grow). It is a separate script rather than ESLint's bulk suppressions
because editors lint through the Node API, which ignores the suppressions
file, and the CLI loads that file for every config, which would fail the i18n
eslint run on "unused" entries. Adding a restyle to a file at its
ceiling means using the primitive's own variant or size prop, keeping layout
classes at the call site, or wrapping the primitive in a small named component
that carries the class in the component file — not raising the number.
`no-inline-styles` and `no-arbitrary-values` stay off by design — inline
`style={}` is the mandated method for apps, and translucent theme surfaces are
`bg-[color-mix(…)]` because the color tokens carry no alpha channel.

Built-in themes are picked in Settings, Display tab, and the choice syncs across
instances. Each theme has a dark and a light block, and the default theme's
`data-theme` is the bare `dark` / `light` rather than a prefixed slug.

Shared CSS utilities: `.top-bar-pill` and `.topbar-glass`
(`src/styles/shell-chrome.css`), `.scroll-shadow`, `.table-striped`, `.skeleton`
and `.focus-ring` (`src/styles/focus-and-state-cues.css`). A theme change
crossfades through a `transition` on `body` (`src/index.css`).

## Large file-pair diffs

All old/new source pairs render through `PierreFilePair` in `src/pierre/index.tsx`.
That wrapper owns a layout-independent renderer-thread budget before the lazy
Pierre chunk loads, because Pierre constructs the raw diff synchronously before
its worker pool or row virtualizer participates. Inputs outside the budget keep
both complete files, header controls, native selection, wrapping, and theme
styling in a bounded plain side-by-side or sequential surface. A translated
status identifies the simplified view; it omits syntax colour and hunk
interleaving by default, and a "Show line-by-line diff" control in a strip
between the header and the scroller opts one pair into the real diff: the
computation runs in a Web Worker (`src/pierre/diffOffThread.ts`), so the
renderer never blocks, and the result renders through the hunk-based patch
path with unchanged ranges folded. The content limit is measured in JavaScript UTF-16
code units rather than encoded bytes so the guard stays allocation-free while an
editor changes. Editable live diffs use the same
predicate and degrade to the ordinary editable file surface rather than becoming
read-only. Do not duplicate or weaken the limits at call sites.

## Typography scale

Body is 14px (`0.875rem`, set on `body`). Descriptions and details use
`text-sm` (14px); labels, buttons and sidebar entries use `text-[13px]`; badges
and captions use `text-[12px]`; decorative icons `text-[10px]` to `text-[11px]`.
Code blocks are 13px mono.

Minimum readable text is 11px, and **nothing goes below 10px**. Do not use
`text-xs` (use `text-[13px]`), and do not use `text-[9px]` or smaller.

## Builtin app auto-discovery

A builtin app does not need a `NAV_ITEMS` entry, and `App.tsx` does not need a
route for it. `BuiltinAppRoute` resolves the catch-all `/:builtinApp` against the
registry in `src/apps/builtinRegistry.ts`.

To add one:

1. Create the page component under `src/apps/<name>/` (or `src/pages/`).
2. Export it as the module default.
3. Add one entry to `BUILTIN_COMPONENT_REGISTRY` (a `BuiltinAppEntry`):
   `'/my-app': { component: lazy(() => import('./my-app/MyAppPage')), appId: '<app.json name>' }`.
   `appId` must equal the `name` in the app's `app.json` manifest.
4. Declare `ui.pages` in the app's `app.json` manifest, and its `ui.icon` name.
5. If the icon is not already in `src/apps/builtinIcons.tsx`, add it to
   `BUILTIN_ICON_REGISTRY` (Lucide element, `size={16}`).

Components are lazy so a builtin app does not weigh on the initial bundle. The
route must be a single plain top-level path segment: the registry is matched
against `location.pathname` only, so a multi-segment, query, or hash route would
register and then never resolve. The same constraint and the reasoning behind it
are in [extension-seams](extension-seams.md), which covers registering routes and
icons from a downstream edition instead of editing the seed maps.

## Crew capability drafts

The Crew editor's Capabilities pane and its draft, preview and save contract are
specified in [crew mode](../../docs/system-specs/modules/crew-mode.md) (*Crew
capability drafts*).

## Floating layers and Crew switches

Switching to a remote Crew hides the local dashboard without unmounting it, so
a floating layer must not portal straight to `document.body` (lint refuses
`createPortal(…, document.body)` and Radix imports outside `components/ui/`).
`src/components/overlayRoot.tsx` is the reference; in short:

- Portal through `<OverlayPortal>` or `useOverlayRoot()`. Inside the local pane
  that resolves to a body-level container hidden with the pane; anywhere else it
  is `document.body`.
- A switch counts as a click outside for layers that hold no input: the shared
  Radix menus, context menus and selects close (`useCloseWhenCrewHidden`). A new
  shared Radix wrapper passes `container={useOverlayRoot()}` inside
  `MountAfterCommit`, then either closes the same way or, if it can hold input,
  stays mounted and refuses dismissal while hidden (`standDownWhileHidden`, as
  popovers do). A popover is modal only inside a Radix dialog.
- Every other layer stays mounted and hidden, and hiding stops painting, not
  listening. Required: every document- or window-level key handler of a layer
  rendered under `<OverlayPortal>` that commits, discards or claims a key reads
  `useLocalCrewHidden()` and stands down while it is true (as `Modal`,
  `useDialogFocusTrap`, `useListKeyboardNav` and `useMenuKeyboard` do).
  `src/test/overlayKeyGuard.inventory.test.ts` lists every such listener and
  fails on a new one until it is guarded or exempted with a reason.
- A Radix dialog and what it opens stay on `document.body` across a switch
  (`GlobalOverlayScope`), as does the notification banner.
