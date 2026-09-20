# Frontend conventions

Shared components, accessibility, security, data fetching, live-collection
identity, animation, styling, typography, and how a builtin app gets discovered.
Page structure is in [page-layout](page-layout.md); color and CSS-var rules are
in [theming-contract](theming-contract.md); user-facing strings are in
[i18n-catalog](i18n-catalog.md).

## The stack

React 18, Redux Toolkit, React Query (`@tanstack/react-query`), React Router v7,
Framer Motion, Tailwind CSS 4, Lucide React, DOMPurify, highlight.js, Monaco,
TypeScript, Vite 8. Read the pins from `website/package.json` rather than this list.

Prefer the library already here over a new dependency. Every addition is bytes in a
bundle a user downloads and a supply-chain surface someone has to review, and two
libraries doing one job is how a codebase ends up with two animation systems whose
transitions do not compose.

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
`ui.tsx`'s takes a required `source` string and renders it as the label;
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

- `Clickable.tsx` (accessible clickable div; see below)
- `SegmentedControl.tsx` (sliding pill, Framer Motion) — see the switcher rule below
- `ui/tabs.tsx`, `Tablist.tsx`, `ui/tabsPill.ts` (the other two switchers and their
  shared class recipe) — see the switcher rule below
- `DetailPanel.tsx` (resizable side panel with animated open/close)
- `SidePanelLayout.tsx` (shared side-panel page layout)
- `AgentSelector.tsx` (portal dropdown with ARIA)
- `layout.ts` (`LAYOUT` numeric constants: nav widths, sidebar width, max message
  width, topbar height, log line cap)
- `InfoTip.tsx`, `MarkdownRenderer.tsx` (highlight.js syntax highlighting),
  `TypewriterText.tsx`

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
`onKeyDown` on the dialog **panel**, so every one of its ~24 call sites gets it
without wiring anything.

**The boundary follows the REACT tree — not the DOM tree, and not the stacking
order.** React routes synthetic events through the React tree even across a
portal, so what decides coverage is where a component sits in JSX:

```tsx
<Modal open={open} onClose={close} title="…">
  …
  <SimpleSelect … />   {/* COVERED: a React descendant. Its popup portals to    */}
</Modal>                {/* document.body at z-[9999], and is still covered,     */}
                        {/* because coverage is about the React tree.            */}
{pickerOpen && (
  <ProjectPicker … />   {/* NOT COVERED: a React SIBLING. It paints above the    */}
)}                      {/* dialog but Modal's panel handler is not an ancestor  */}
                        {/* on its dispatch path, so it needs its OWN boundary.  */}
```

Both of those overlays portal to `document.body` and both paint above the dialog
at the same `z-[9999]`. Only one of them is inside the boundary. **Sharing a
stacking context is a paint-order fact and implies nothing about event
routing** — conflating the two is what kept #6833 open, so do not reason about
coverage from a z-index.

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
return createPortal(<div onKeyDown={isolateKeys} …>…</div>, document.body)
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

One deliberate, key-scoped exception exists: a Windows absolute path
(`WINDOWS_ABS_PATH_RE` — drive letter or UNC) is passed through **for image
`src` only**, because `defaultUrlTransform` parses `C:` as an unknown scheme and
would blank the sender's own uploaded image (issue #3497). The invariant that
makes it safe: `ImgWithFallback` routes every local path to the same-origin
`/api/file-raw` endpoint, so the raw filesystem path never reaches the DOM, and
the shape (single letter + separator) cannot express `javascript:`/`data:`
payloads. Widening that regex or its key scope is a security change — the same
constant also decides which paths are treated as local file reads, so the two
decisions must stay on the one exported copy in `urlTransform.ts`.

## Data fetching

The shared memory editor keeps the existing global V1 Key/Value/Set action
inside the lazily loaded Overview memory drill-in. The shell shows the shared
`ContentSkeleton` while that chunk loads; the member/store URL remains the
navigation owner throughout loading. This keeps record editing and recovery
tools out of the initial dashboard bundle. The create action remains
beside the paged browser. Its unscoped semantic writer is available only when
the selected store is global and the surface is not private. The narrow form
stacks its inputs and submit button; its draft joins the store-switch guard,
pending submission disables the fields, and an error retains them for retry.

Member-scoped recall presents the returned fact and experience snippets as compact
evidence cards. Exact serialized model context and source diagnostics live in
the collapsed Source and retrieval details disclosure. Rules have their own
indicator and full context there; fact snippets do not represent the rules
included in recall. The disclosure accepts the recall API's structured copy
origin as well as the record browser's serialized origin.

Memory V2 uses member-scoped language (成员记忆 in Chinese), without a lock badge
or a promise of confidentiality between members. Database errors remain distinct
from embedding-model errors; configured and active models, keyword and vector
status, reload/rebuild confirmations, checkpoint failure evidence, and counts
with their actual units remain visible.

Only explicit member creation initializes an empty Memory V2 database. Existing
members retain their current memory; edits offer no provisioning or migration
action. An unavailable member database remains an error and requires restoring
its backup. The Crew Manager notice distinguishes new members from existing
members. A disabled Manage memory
action shows its unsaved-changes reason as visible helper text for keyboard and
touch users. Member status distinguishes an explicitly
different configured owner from an unavailable or unverified binding. Unavailable
memory views retain Retry and offer guarded Crew Manager navigation for inspecting
settings, using an exact catalog owner when available and the manager list
otherwise. This navigation does not grant ownership or promise an automatic repair.

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
not suppressed (slots at 200ms), so an active session delivers several full lists
per second and the effect is continuous rather than incidental.

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
store. Two dashboard collections still assign wholesale and are known gaps rather
than counterexamples: `sseSubagentStatus` rebuilds its `agents` array every frame,
and `sseStatus` replaces `state.status` as a whole object that several panels
subscribe to entirely. Converting them is worthwhile but is its own change —
`state.status` in particular needs its consumers narrowed first, or the merge buys
nothing.

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
- Do NOT add a new CSS `@keyframes`. The existing ones in `index.css` back
  specific low-level effects (skeleton pulse, caret blink, indeterminate
  progress); a new component animation goes through Framer Motion.
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
  `data-theme` attribute rather than the OS media query alone), keeps `hover:` an
  ungated `:hover` so touch devices still reach hover-revealed controls, and
  emits the iOS safe-area utilities (`p-safe`, `top-safe-offset-*`, …) as
  `@utility` blocks. Adding a utility for a new token means adding one
  `--color-<token>: var(--<token>)` line here; `scripts/check-phantom-classes.mjs`
  compiles against this file to catch a utility whose token was never declared.
- `src/index.css` — the entry. It imports Tailwind's theme and Preflight into
  their cascade layers and emits `@tailwind utilities` UNLAYERED (see the header
  comment there: the component CSS below it was written against v3's unlayered
  utilities and must keep competing with them on plain specificity), lists the
  template sources with `@source`, and restores three v3 Preflight defaults as
  token-backed base rules (default border colour `var(--border)`, placeholder
  colour `var(--muted)`, `cursor: pointer` on enabled buttons).

The build runs through `@tailwindcss/vite`; there is no PostCSS config. A
downstream edition's sources are added to the content scan by
`editionExtensionPlugin` in `vite.config.ts`, which swaps the
`/* @kirocrew-edition-source */` marker in `index.css` for an `@source` line.
Utility names follow Tailwind v4: `outline-hidden` (not `outline-none`) is the
accessible outline suppressor, `backdrop-blur-xs` is the 4px blur, and the
`shadcn/ui` primitives animate through `tw-animate-css`.

Colors come from CSS custom properties defined in `src/index.css`, including the
semantic roles `--aim`, `--clarify`, and the `--diff-*` family. Never a hardcoded
`#hex` / `rgb()` / `rgba()` literal, and never a raw palette class
(`text-green-500`, `bg-amber-400`): state colors are `text-ok` / `text-warn` /
`text-danger` / `text-info`, and a running state is `text-accent`. See
[theming-contract](theming-contract.md) for the variable set, the stable class
hooks, and the checkers.

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

`shadcn/no-restyle` is deliberately off: ~300 call sites restyle `ui/`
primitives today and the gate is a hard zero, so turning it on is a design
decision (fix the sites or write per-component contracts), not a lint toggle.
`no-inline-styles` and `no-arbitrary-values` stay off by design — inline
`style={}` is the mandated method for apps, and translucent theme surfaces are
`bg-[color-mix(…)]` because the color tokens carry no alpha channel.

Built-in themes are picked in Settings, Display tab, and the choice syncs across
instances. Each theme has a dark and a light block, and the default theme's
`data-theme` is the bare `dark` / `light` rather than a prefixed slug.

Shared CSS utilities in `index.css`: `.top-bar-pill`, `.topbar-glass`,
`.scroll-shadow`, `.table-striped`, `.skeleton`, `.focus-ring`. A theme change
crossfades through a `transition` on `body`.

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
3. Add one lazy entry to `BUILTIN_COMPONENT_REGISTRY`:
   `'/my-app': lazy(() => import('./my-app/MyAppPage'))`.
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

The Crew editor has an independent Capabilities rail pane with MCP, Tools,
Auto-approved and Skills categories. It stays mounted while hidden so both its
local draft and its signed server preview survive rail changes. Its footer owns
Discard draft and Review/save; the generic crew save cannot discard a capability
draft. Closing or opening chat asks before losing that draft. A capability
request in progress holds dismissal. Dirty and busy state reach the parent in
layout effects, before paint, so an immediate Escape after pasting cannot close
against an older clean state. Browser unload also warns about the draft.
Opening the embedded editor writes an explicit `tab=crews` route, so a resize
cannot replace its ancestry with the mobile root list. On narrow screens the
member identity owns a full header row. The capability form scrolls independently
above a non-overlapping footer. The horizontally scrollable category strip does
not flex-shrink when an expanded transport form exceeds the pane height; all
category labels retain their full height. Review shows values from the server's sanitized
projected rows, never from secret-bearing local drafts. Source validation errors
are distinct from provider loading failures.

The editor reads and writes through `api/crewCapabilities.ts`, using the shared
transport. Preview and save send the same explicit inheritance operations; save
adds only the server-issued preview token. A stale version preserves the draft
and requires reloading and reviewing against the new version. Save success never
stands in for runtime application: runtime status comes from the server and
active sessions are not promised a hot reload.

The legacy template pane keeps its instant-save behavior for independent and
shared definitions. Enrolled definitions direct model and skill edits to
Capabilities instead. Reset and publish remain in the template pane but lock
while a capability draft exists. A mask is never a literal replacement value.
The form can keep unchanged secrets, select a configured connection, or replace the
whole transport using a blank form. MCP set operations carry a complete transport
plus RFC6901 `retain_paths` for unchanged `[REDACTED]` leaves. Each pointer keeps
its original member/revision binding. Editing a hidden value removes that pointer;
a typed mask without a retained pointer blocks preview. Hidden argument positions
and hidden map keys cannot move until their values are replaced explicitly.
Environment and HTTP header values remain password inputs. Managed transport
fields use the row's authoritative `managed` flag, independently of the connection
catalog; their supported enable switch sends only `disabled`. Absent prompt/model
rows can be set, and model choices use the shared advertised-model query. Version
hashes live in a collapsed details section rather than in the main status banner.
Parent-change and impact previews use the server's redacted projection.
