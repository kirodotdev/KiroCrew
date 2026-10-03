# Page Layout Guide

Every dashboard page follows one layout pattern. Copy the skeleton below rather
than inventing a custom layout: a page that diverges costs a reader the
orientation cues (title block position, scroll container, section rhythm) that
every other page gives them for free.

Components come from [`src/components/ui.tsx`](../src/components/ui.tsx); the
conventions around them (a11y, data fetching, typography) live in
[frontend-conventions](frontend-conventions.md).

## Page skeleton

### Dashboard in chat and Crew

Long-run status uses the existing side-panel dock, expansion and narrow-screen
overlay, not another drawer. In chat, a dock above the composer, in the
composer's own column (`--mc-input-width`), shows three tiles: progress (done/total
with a plan, else "N running"), blocked
and Needs you. Each tile is a disclosure button for the short list beneath it (a
second click closes it), and the row wraps in a narrow column; beside the tiles
sit two actions, open the **Dashboard** tab (a labelled "Open Dashboard" button) and
hide. Hidden, the dock is one pill at the row's end. It carries the hide glyph,
or a red count while something needs the user (`mc-task-dashboard-hidden`, per
browser); the dock is one element in both forms. It is removed once every run rests,
no request waits and the plan is complete. Crew reuses its single permanent **Dashboard** tab instead: its compact
entrance focuses that tab, and its + menu withholds the parallel chat view.
The existing member publication and task artifacts share a Published view selector, with
human-readable titles, while retaining their separate renderer sandboxes. A
pipeline board is one publication, not another product. Visited Crew dashboards
stay mounted across Notes/Work log switches and panel hiding; a pending member
thread revalidation hides controls without discarding the last confirmed body.
The task
page itself is model-authored HTML and may choose any appropriate responsive
layout. Host chrome owns scope, current activity, questions and one-shot tool
approvals. Keep native answer drafts mounted when hiding the panel or switching
tabs, but unload model-authored iframe documents while inactive to cap resources.
This does not change the existing Crew protected-template renderer's lifecycle.
Approval counts and exact session identity stay outside the
sandboxed page, so a model redesign cannot hide or impersonate those controls.
The panel keeps its approval count on the Approvals tab; the dock's Needs you
tile and its hidden pill carry the same count.

The Sessions header's three-dot menu offers **All Dashboards**, a standalone
`/session-dashboards` page with currently open sessions' saved summaries and authored
dashboards. Native pending questions and approvals appear in a central **Needs
you** inbox above the summary gallery, with each request labeled by its exact
session. Approval headings name the requested action, with the secondary session
name explicitly labeled “From session”. Both the fleet inbox and task panel show
recorded session context when available, never an invented approval reason.
Dashboard questions use “Send answer”;
the shared chat control retains its default label. Unverified requests explain
that this view cannot verify the request and offer
Open session, without in-page approval or submission guidance. Native approvals
carry the host permission-row identity bound to the current request, not just a
reusable connection ID. Replacements get separate card keys and cannot inherit
a previous card's submitted or expired state. Snapshots missing this identity
use the same Open session fallback. Verified approvals
share a per-request rejection explanation without changing permission mode.
Successfully responding resolves the same shared request; other views show the result when
refreshed, not necessarily immediately.
Summary cards do not repeat these controls. Unavailable inventory shows
a stale notice rather than an empty-inbox claim. Input-needed sessions appear
first in the gallery, then blocked and running ones.
Search and Needs you filter the cards and inbox without discarding answer drafts.
The filter and inbox count pending requests, not distinct sessions. The
current page of at most 12 active summaries reads summary/dashboard contents;
Next sessions and Previous sessions move between pages. Wrappers remain mounted for
the bounded live inventory (`MAX_LIVE_SLOTS`, 500), preserving published-view
selection. There are at most 24 iframe documents, including both frame types.
Each session can have one automatic status frame and one selected saved view.
The task panel mounts at most twelve status frames. Needs you precedes status
cards in both surfaces. The explicit **Automatic cards for all sessions** opt-in
leads with summaries of recent work and next steps. Secondary copy retains the
default-off state, background model use and shared attempt limits, including
failures; merely opening the page never starts generation. Each
frame shows its successful content publication time, separate from host runtime
state and inventory read freshness. The model may reuse HTML and update only
text data; host-native answer and approval controls are never inside the frame.
Automatic-card CSS is parsed in detached CSSOM, never attached to the host.
Parsed `content`, `list-style`, `list-style-type`, `quotes` and
`hyphenate-character` declarations are removed,
including nested rules and keyframes; custom counter styles and rules that
cannot be inspected are omitted. Inline styles use the same generated-text
boundary; native quotation and hyphenation cannot consume model-authored strings.
Ordinary browser quotation and hyphenation defaults remain available.
Safe layout styles and literal text bindings remain available. If
isolated parsing fails, the affected model style is omitted, not passed through.
Saved published views retain their existing CSS behavior. This is a rendering
restriction, not a claim that the backend cache contains no encoded text.
This is an explicit fleet view, never a fallback for an unresolved task scope.
It uses the standard page header and scroll container, with one column narrow
and two wide, and never generates a summary merely because the page opened.
Verify at both 320px and 390px: keep native approval targets at least 44px tall
and the page free of horizontal overflow. Main labels use meaningful names or
localized numbered session/task labels; opaque IDs remain routing data, not
headings. The artifact authoring contract asks models for the same readable,
phone-friendly treatment without prescribing their layout.
The Crew entry remains **Dashboard**. The agent-authored publication's collapsed
and expanded chrome says **published view**, including loading and error states.

### Standard page composition

```tsx
<>
  <PageHeader title="PageName" subtitle="Short description" />
  <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
    {/* optional StatCard row, then Cards with tables/forms */}
  </div>
</>
```

`PageHeader` owns its own `px-4 md:px-6 pt-2 pb-3` — the SAME horizontal gutter as the
content container below it, so the title shares a left edge with the cards and rows it
labels. Keep them equal: a header that drifts off the container's gutter insets the
title from its own content, and the top bar above is a separate layer that does not
have to match (see "The title belongs to the content column" below).
`overflow-y-auto flex-1 min-h-0` is what makes the content the scrolling region while
the header stays put: the shell is height-locked, so without `min-h-0` the flex child
refuses to shrink and the whole page scrolls instead.

`PageHeader` also takes an `actions` node, rendered right-aligned on the title
row. Put page-level buttons there rather than in the first `Card`.

### Read every class here narrow-first

**The unprefixed value is the PHONE's. `md:` adds the desktop.** `px-4 md:px-6` is a
gutter that starts at 16px and widens; `p-5 max-md:px-4` is the same intent written
backwards. Both render identically, and only the first one is maintainable.

This is not a style preference. Tailwind is built narrow-first, so a rule whose
unprefixed value is the desktop one forces every narrow fix to claw the width back
with `max-md:` and, usually, a negative margin hand-pinned to a number owned in
another file. That pairing is invisible when it breaks: the pane slides past the
screen edge, and on a script that breaks between characters nothing overflows, so no
scroll assertion sees it. Writing the phone value unprefixed removes the second
number instead of documenting how to keep it in sync.

So the sections below are not exceptions to a desktop standard. They are the
baseline, and the desktop is what `md:` adds to it.

### The narrow-viewport inset budget

These are **recommendations for the narrow branch only** — nothing here changes a
page from `md` up, and `AUTOSDE.yaml` does not enforce a gutter value. A page that
keeps one gutter at every width is conformant.

**Recommended below `md`: a 16px page gutter (`px-4`) and an 8px `Card` horizontal
inset (`px-2 … md:px-5`)**, serving a budget of **no more than ~25px of stacked inset
before body text** (16 + a 1px border + 8). 16px is the screen margin Material, Apple
HIG, Fluent, Carbon, Polaris, Primer and Atlassian all converge on, and two surfaces
here already ran it before this was written down (`SidePanelLayout`, the Knowledge
page), so the default is the number the rest of the industry and this app had already
picked rather than a new one.

Padding stacks, and the eye reads the SUM. The gutter is 24px from `md` up, where
it is comfortable. At 390px the same 24px plus a 20px card inset put card text
44px from the screen — 88px of a 390px screen, **22.6%**, spent on nothing, and
the line that pays is the longest content in the card.

Which layer yields is not arbitrary:

- **Both layers yield, the one against a DRAWN border yields less.** `Card` goes to
  8px horizontally — narrowed, never flushed. Its inset is often the only gutter its
  own rows have, so flushing a card to 0 is still wrong (see below). The VERTICAL
  inset stays 20px: horizontal is the axis a phone cannot spare, and changing the
  vertical would move every card's height.
- **The layer against the SCREEN EDGE yields more.** A phone bezel is not a drawn
  line, so a gutter narrower than the 24px desktop one reads as intentional rather
  than cramped. 16px is where that stops being true in the other direction: this
  was tried at 8px and then at 12px, and 8px read as content pressed against the
  bezel rather than as a deliberately dense page. 16px keeps most of the width the
  narrow gutter buys while leaving the page visibly inset.

This MATCHES the 16px screen margin that Material, Apple HIG, Fluent, Carbon,
Polaris, Primer and Atlassian all converge on. Their 16px is content-to-edge for
content that is **not already inside a bordered container**: a Material list item at
a 16px margin puts its text at 16px, not at 36px. So this app hits that number
exactly for content ON the gutter -- a heading, a row, a tab strip -- and a `Card`
then charges its own 8px on top, putting card text at 25px. The chat transcript runs
the SAME 16px -- its message row and its composer are both `px-4` -- so a page's
uncontained content and the agent's own text sit on one vertical line across the whole
app, and only a `Card` steps inside it. That a card cannot also land on 16px under one
gutter is arithmetic, not an oversight: the card inset absorbs the difference, which is
one more reason to reach for a `Card` less often on a phone (see below).

At 390px the card goes from 342px wide (the 24px gutter) to 358px and its text line
from 300px to 340px, **+13.3%**; at 320px the card goes from 272px to 288px and the
text line from 230px to 270px, **+17.4%**. Nothing changes from `md` up.

That is the comparison against the DESKTOP gutter. Against the 8px narrow gutter and
10px card inset this replaced, the same card is 16px narrower and its text line 12px
shorter (390px: 374 -> 358 wide, 352 -> 340 of text). Measured, not derived. The 12px
buys a 16px screen margin on every uncontained surface and one shared left edge; a
page that would rather have the width should drop the `Card`, not re-cut the gutter.
Both sets of figures follow
from the box arithmetic -- viewport minus two gutters, then minus two 1px borders and
two card insets -- and the text-line figures subtract the borders because a border is
opaque to text the same way padding is.

The one exception is `OnboardingChapterShell`, a full-page surface with its own
`sm:px-10` scale rather than a `PageHeader` + container page.

### The title belongs to the content column, not to the chrome

Going down the left of a phone screen there are two layers, and they are allowed to
differ:

| | narrow | made of |
|---|---|---|
| **content column** — `PageHeader`, page rows, `Card` boxes | **16px** | the page gutter, `px-4` |
| `Card` body text | 25px | 16 + 1px border + the `Card`'s own 8 |
| top bar icon BOX (chrome) | 16px | header `pl-2` (8) + each icon button's own 8 |
| top bar hamburger INK | 16px | that 16px box, less a 2.5px optical correction, plus `Menu`'s own 2.5px of empty box |

The title shares the container's 16px so it sits directly above the left edge of the
cards and rows it labels. That is the rule, and it is what decides the number: the
title follows its CONTENT, never the chrome above it. An earlier round tried the
opposite — moving the header out to meet the top bar — and it read worse, because the
title then sat inside the very cards beneath it.

At 16px the chrome happens to land on the same line, and that is a consequence rather
than the reason. The top bar's icon BOXES are header inset plus each icon button's own
8px, so an 8px header inset puts them at 16px: the hamburger, the page title, the
chat session-list toggle and every card's left edge become one vertical line. Only the
LEFT cluster is tuned this way — `.tb-right` carries a padding/negative-margin pair
that keeps the notification badge's 4px overhang from being clipped, and re-tuning it
needs a real WebKit check rather than a local one. Two things make this line easy to
break silently: a mobile-only `px-2` on the left cluster once stacked on the header's
own inset and pushed the hamburger out past the page's own edge, and the glyph position
is never the container's `className` — measure the rendered glyph with
`getBoundingClientRect`, not the class.

**A correctly placed box does not mean a correctly placed glyph.** An icon's artwork
need not fill its own viewBox, and the eye sees the INK, not the box. `Menu` is the one
icon here that does not fill it: lucide draws its three rules from `x=4` in a 24-unit
viewBox and the round cap reaches half a stroke further, leaving 3 units — at `size={20}`
that is 3 × 20/24 = **2.5px** — empty on the left. Measured at 390px, its box sat
correctly at 16px while the visible glyph drew at 18.5px, reading as indented against a
card border directly beneath it. It carries a `-translate-x-[2.5px]` correction so the
ink lands at 16px; a transform rather than a margin, so the box, the hit target and the
hover pill all stay on the 8px grid and no sibling in the cluster shifts.

The correction is per-icon and most icons need none — the chat session-list toggle's
`MessageSquare` starts at `x=2`, i.e. 0.67px at `size={16}`, which is already on the
line. Do not generalise this into one shared offset. Note also what the correction
trades: ink now agrees with hard edges (a card border, a divider) and sits ~2px left of
the page TITLE's ink, because text carries its own left side bearing — 2px for `N` at
24px bold. Two things cannot both be true at once, and hard edges won: a border is a
crisp line the eye measures against, while a letter's bearing varies per glyph and per
platform font.

Chat is on this line too, not beside it: the transcript's message row and the composer
are `px-4` with no responsive variant. So the hamburger glyph, the page title, a page
row, a card's left edge and the agent's own text all start at 16px, and a `Card`'s body
text is the one thing that steps inside (25px). Chat is where a phone user spends most
of their time, which is why it is the surface the rest is lined up with rather than the
other way round.

`src/test/narrowFirstBaseline.test.ts` pins the header to the container gutter the
skeleton above documents, and separately pins the top bar's left cluster against the
redundant inset coming back.

### If you write a shared primitive, a breakpoint-scoped base padding is a trap

This one is for primitive authors rather than page authors, and it cost this repo a
silent desktop regression before it was written down.

`twMerge` only collapses classes that collide at the **same** breakpoint. So the
moment a primitive spells its base inset with a prefix — `md:px-5` — a caller's
plain `p-3` no longer displaces it. The two sit side by side, the caller gets its
12px on a phone, and from `md` up the primitive's 20px quietly wins. The call site
reads as 12px everywhere and is not.

Making every caller spell both halves (`p-3 md:p-3`) does close it, but it is the
wrong shape twice over: it is a permanent obligation on every future caller, and any
guard for it has to be lexical, so a computed `className={cond ? 'p-3' : ''}` or a
class list held in a module const walks straight past.

What `Card` does instead: if the incoming `className` names a padding on an axis,
the base inset for THAT axis is dropped rather than merged, decided from the final
string at render time. The caller owns the axis it asked for, at every width, and no
call site has to know the trap exists. `src/test/cardInsetYield.test.tsx` pins it by
rendering, including the computed-`className` case.

Any new primitive that pairs a `md:`-prefixed base padding with `twMerge` re-opens
the same hole, so either yield the axis the same way or keep the base unprefixed.
Stated honestly: `Card` is currently the ONLY primitive in `ui.tsx` with a
breakpoint-scoped base padding — `Btn`, `Input` and `StatCard` are all
unprefixed — so this note has no other instance to fix today. It is here because the
failure is silent and desktop-only, which is exactly the kind a reader will not
re-derive when they reach for `md:px-*` in a new primitive.

### Where the narrow-viewport rules live

The measurement record sits in [narrow-viewport.md](narrow-viewport.md), one hop
away. Everything in it is a recommendation rather than a gate, and each item
carries the measurement that settled it, so reach for the measurement before
arguing with the rule.

| Rule | Where it is stated |
|---|---|
| Page zoom off on touch, and the surfaces that own their own zoom | [narrow-viewport.md](narrow-viewport.md#layout-and-sizing) |
| The 44px touch-target rule and its two-tier grading | [narrow-viewport.md](narrow-viewport.md#layout-and-sizing) |
| The drag-widget `touch-action: none` exemption | [narrow-viewport.md](narrow-viewport.md#a-horizontal-drag-on-mobile-belongs-to-the-nav-drawer-unless-a-page-claims-it) |
| The 16px gutter derivation and the field floor that was not adopted | [narrow-viewport.md](narrow-viewport.md#layout-and-sizing) |
| The nav-drawer swipe contract and `data-owns-swipe` | [narrow-viewport.md](narrow-viewport.md#a-horizontal-drag-on-mobile-belongs-to-the-nav-drawer-unless-a-page-claims-it) |
| Binding a panel's gesture live to its offset | [narrow-viewport.md](narrow-viewport.md#a-panel-that-gains-a-gesture-must-be-bound-live-to-its-offset) |
| Horizontal insets below the breakpoint, and `Card`'s measured budget | [narrow-viewport.md](narrow-viewport.md#horizontal-insets-below-the-breakpoint) |
| The phone chat page's single top bar (`topbar-single`), its portal slots and the drawer rail | [narrow-viewport.md](narrow-viewport.md#the-phone-chat-page-has-one-top-bar) |

### The chat transcript scroller and who moves it

The chat transcript is a page's scrolling region like any other, with one
difference: it is windowed. Only the rows near the viewport are real DOM, and
spacers stand in for the rest. The hook that does this,
`website/src/hooks/virtualizer/useVirtualChat.ts`, keeps the reader's position
through every change to that window, so a host wires it rather than working
around it.

A host provides the scroller (`scrollerRef`), one `measureRef(index)` element
per mounted row, the two sentinels at the list ends, and the `offsetBefore` /
`offsetAfter` spacers. Chrome it renders inside the scroller above the rows (a
paging bar, a header band) needs nothing extra: the hook measures that leading
offset itself and carries a bottom-parked reader through it. A host that steers
toward a row that may not be mounted (the pinned-prompt glide) asks the hook
through `mountIndex` and `estimateRowTop`. The page keeps `overflow-anchor: auto`
on the scroller as the browser's own stabiliser. WebKit ships none, so the hook
carries its own anchors as well.

The pinned-prompt card is an overlay beside the scroller, not a row in it, and
it paints above the composer dock, so nothing but geometry bounds it. Its
ceiling is the transcript FLOOR: the scroller's bottom less the scroller's own
`padding-bottom`, which is each host's statement of where readable rows stop
(the main chat pads by the dock's height plus a clearance). `usePinnedPrompt`
measures that floor off the scroller rather than taking it as a prop, clamps the
fold's live height to it and hands it to the card as `maxH`, which lands as
`max-height` on the bubble; the body is a shrinkable flex column so the cap
scrolls the prompt instead of clipping it. A host that moves its floor (a dock
that grows a status bar) only has to keep its `padding-bottom` honest. A prompt
whose part still below the band is taller than the resting card is not pinned
at all: the real bubble stays in the transcript, so a long prompt reads and
scrolls as itself, and the card takes over only once what remains fits it. The
resting height belongs to one prompt's card (an image-only card is two lines
tall, a text card one), so it falls back to the default whenever the pin
candidate changes, until the card reports it for that prompt — the host hands
the card the candidate's identity as `promptKey`, so a card that stays mounted
across the change re-measures and reports too.

| What moves the scroller | Owner (`website/src/hooks/virtualizer/`) |
|---|---|
| Following the live turn, the jump-to-latest pill, scrolling to a row | `followPolicy.ts` (every write goes through its `writeScrollTop`) |
| Holding a scrolled-up reader still when rows or heights change above them | `shiftCompensation.ts` |
| Reopening a session where the reader left it, and re-placing after a hidden tab returns | `readingPosition.ts` |
| Which rows are mounted as the reader scrolls, and the spacer heights around them | `windowRange.ts` over `measurement.ts` |
| When a new row height is allowed to move the page | `geometryScheduling.ts` |
| Listening to the scroller, its rows and its own box | `observers.ts` |

The full owner map is in
[history](../../docs/system-specs/modules/history.md#the-dashboard-transcript-window-frontend).

### Decided: the transcript scrolls under the composer glass

This is a design decision, not a defect, and it is settled
([`docs/decisions/2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md`](../../docs/decisions/2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md)).
On the main chat page the composer dock floats over the bottom of the transcript
scroller (the iOS toolbar layout): the scroller runs the full height of the pane,
the conversation passes under the dock's translucent glass at every scroll
position, and the scroller pays for the covered strip with its `padding-bottom`
(the dock's height plus a clearance, measured from the dock by a
`ResizeObserver`). That holds whether or not the status stack above the composer
holds a bar and whether or not the jump-to-bottom pill is showing; the pill
floats over the transcript. Do not make the scroller's box end above the dock,
reserve the dock's height with a margin, or otherwise clip the transcript so that
"no text is read through glass": [#15820](https://github.com/kirodotdev/KiroCrew/pull/15820)
did exactly that and its transcript half was reverted on the maintainer's
decision. Legibility of what sits over the transcript is the glass recipe's job
(blur and tint), never the scroller's.

The welcome hero (`key="welcome-hero"`) is the one box that ENDS above the dock
(`marginBottom: dockH`, never padding under it): its suggestion cards and the
Refresh link are controls, and a control under the glass is an ambiguous tap.
It is `isolate` so WelcomeView's own z-indexes order its cards against each other
and never against the composer's, and its column uses `safe center` and compact
rows under 600px tall so a short window still fits both rows and the link above
the dock (this is #15820's hero half, kept). The side-panel `ChatPane.tsx` keeps
its bars in flow and needs none of this.

## Stat cards

OPTIONAL summary metrics above the content. Add a row only when a number is not
already visible in the content below it: a rolled-up total, a rate, an error
count. Do NOT add one that restates `items.length` for a list rendered on the
same screen; it costs roughly 90px above the fold and carries no action. A page
with no stat card row is conformant.

```tsx
<div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
  <StatCard label="Total" value={count} accent />
  <StatCard label="Active" value={active} />
</div>
```

`StatCard` renders a pulsing skeleton when `value` is `undefined` or `null`, so
pass the query result straight through instead of branching on a loading flag.
Pass `delay` (in ms) to join the grid's stagger. Give it `onClick` only when the
card is really actionable; it then wires `role="button"`, `tabIndex` and
Enter/Space itself.

## Data sections

`Card` + `CardTitle` + `InfoTip`:

```tsx
<Card>
  <CardTitle>Section Name <InfoTip text="Explanation." /></CardTitle>
  <SearchInput placeholder="Filter…" value={filter} onChange={…} />
  {items.length === 0
    ? <EmptyState icon={<Anchor className="lucide-inline" />} title="None yet" />
    : <table className="w-full border-collapse table-striped">…</table>}
</Card>
```

Inside a **side panel**, a counted list-section header is `PanelSectionHeader`
(label + count node + hairline rule), never a hand-rolled one. Hierarchy comes
from weight and size, never from an opacity modifier, and the label is not
uppercased (`text-transform` is a no-op on CJK).

## Tables

Striped body, one header cell style:

```tsx
<th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">
```

`table-striped` shades even rows with `var(--card-hl)`.

## Forms

Inline within a `Card`, built from the shared primitives:

- `Input` for text fields.
- `SendBtn` for the primary action (accent-colored).
- `Btn` for secondary actions, `Btn danger` for destructive ones.
- `Checkbox` from `ui.tsx` for a boolean box.
- **Dropdowns: never a native `<select>`.** Its popup is drawn by the OS, so it
  ignores every theme token, cannot be styled per row, and looks nothing like
  the rest of the app. Pick by list length and purpose:
  - `SettingsSelect` (`components/settings.tsx`) on a Settings page — label +
    description + dropdown as one field. The choke point for that surface.
  - `SimpleSelect` (`components/SimpleSelect.tsx`) anywhere else, up to roughly
    fifteen options. Radix Select under the hood; takes `options` /
    `optionLabels` / `value` / `onChange(value)`, and `action` for a trailing
    "+ New…" row.
  - `SearchableSelect` (`components/SearchableSelect.tsx`) past that, or any
    list a user would want to filter (timezones, file lists). Radix Popover plus
    a filter box.
  - `DropdownMenu` (`components/ui/dropdown-menu.tsx`) for a menu of *commands*
    rather than a bound value.
  - `AgentSelector` for agent dropdowns specifically (portal-based, ARIA-wired).

  These render a `<button>`, not a `<select>`, so an external
  `<label htmlFor>` does **not** name them — pass `aria-label`.

  **The one exception is touch, and it is not yours to make.** `SimpleSelect`
  routes to `NativeSelect` (`components/ui/native-select.tsx`) on a coarse
  pointer, so the OS draws the list there. The reason above is theming, and
  theming does not reach a phone: the Radix popup's list is a `position:fixed`
  overflow scroller inside react-remove-scroll's lock, and iOS Safari does not
  reliably hand a finger drag to that shape — Settings → Voice → Language showed
  7 of its ~41 codes with the rest unreachable. A themed list nobody can scroll
  is worse than an OS-drawn list that works. Because the choice lives inside
  `SimpleSelect`, no call site makes it — and `SettingsSelect` inherits it by
  wrapping `SimpleSelect`. It goes no further: `SearchableSelect`,
  `DropdownMenu` and `AgentSelector` keep the themed popup on a coarse pointer,
  since a native `<select>` cannot host a filter box, per-option sublabels or a
  command menu. Reaching for one of those does not mean the touch case has been
  handled for you; whether that scroller is a real defect on a phone is
  unresolved in #5551. `NativeSelect` is the single file exempted from the
  `no-restricted-syntax` rule; do not add a second.
- `Toggle` for a boolean switch. It carries `role="switch"`, `aria-checked` and
  `aria-disabled` itself, so do not re-add them.

## Status indicators

- `Badge variant="ok" | "err" | "warn" | "aim" | "muted"`.
- `SourceBadge source="…"` for provenance (where an agent, app, or skill came
  from). It maps known sources to colors and falls back to a neutral pill for an
  unknown one, so pass the raw source string.

## Errors

Render through the shared notice instead of hand-rolling a banner:

```tsx
<ErrorNotice message={error} onDismiss={() => setError(null)} askAgent />
```

Enable `askAgent` only when navigating away cannot discard an unsaved draft; otherwise
leave it off and document what must stay in place.

## Animations

`animate-rise` on cards and banners, `animate-scale-in` on inline reveals. Both
are Tailwind utilities declared in `src/tailwind-theme.css`, and both use
`backwards` fill so an `animationDelay` holds the element hidden until its turn.

## Do NOT

- Wrap a page in `<div className="p-6 max-w-[960px] mx-auto">`. Use
  `PageHeader` + the `px-4 md:px-6 pb-8` container.
- Use a raw `<input>` / `<button>`. Use `Input`, `Btn`, `SendBtn`,
  `SearchInput`, `Checkbox`.
- Use a native `<select>`. There is no styled wrapper for one any more — see
  §Forms for which dropdown component to reach for, and for the one touch-only
  exception `SimpleSelect` already makes for you. Enforced by
  `no-restricted-syntax` in `eslint.config.js`.
- Use raw status text. Use `Badge` or `SourceBadge`.
- Use `text-xs`. Use `text-[13px]`.
- Add a new CSS `@keyframes`. Use Framer Motion, or an existing utility.



### Chat workspace title rows and panel controls

The Sessions sidebar, main chat, and right SidePanel share one control baseline:
action cells are 28px square, icons are 14px, and each row is 40px high with
6px gaps. The SidePanel keeps its fused browser-tab strip and half-card frame;
alignment is achieved by placing that strip on the same header line, not by
changing the tabs into pills or flattening the panel frame.

The browser tabs are 32px chips fused to the strip's bottom edge, so 8px of strip
stays visible above them and their center sits 4px below the strip's 28px action
cells. The `+` (new tab) trigger belongs to the tab row and follows the chips'
center, the way Chrome bottom-aligns its new-tab button with its tabs; the
trailing action group (Fullscreen, Bottom panel, Side panel) keeps the row's
center. This is a recorded decision. A 40px chip would share the action cells'
center but fills the whole strip and stops reading as a tab; a self-centered `+`
floats 4px above the tab glyphs beside it; moving the trailing group down would
move the Side toggle away from its closed-state position.

When the SidePanel is closed, the main title row's trailing actions are Pop out,
Split view, Bottom panel, then Side panel. The session-window pair and the
workspace-visibility pair are separate regions, divided by a hairline and an 8px
inner inset. When the SidePanel is open, its dock menu and Fullscreen control form
the panel-shape region, while Bottom panel and Side panel remain the separate
workspace-visibility region. Every region therefore contains at most two actions
without moving an action into a menu. The divider and inset sit only on the
visibility region's leading edge, so the Side panel toggle's bounding box is
identical before and after the panel opens.

The Bottom-panel toggle in these rows (named by what the click does, for both its
tooltip and its accessible name: "Show terminal" or "Hide terminal", as the Side-panel
toggle is "Show side panel" or "Hide side panel" and the Sessions toggle in the same
row is "Show sessions" or "Hide sessions"; a name that states the action carries no
`aria-pressed`; while the terminal is popped out the control reads "Focus popped-out
window", because the click then focuses that window and toggles nothing here) is the chat
page's pointer entry point to
the docked terminal; the rebindable `terminal` panel chord is the keyboard one. It sits next
to the Side-panel toggle because the two panels share the workspace edge and are
opened and closed together. The nav rail keeps its Terminal row: it is the only pointer
entry on routes other than chat and in the phone drawer, where neither host of these
controls renders, so without it the docked terminal has no pointer entry there. Both entries
read the panel's one open flag, so their lit states cannot disagree. Workspace fullscreen
covers the docked terminal, so while it is on both entries, and the chord, read an open
terminal as hidden, and activating one leaves fullscreen to bring that terminal back
rather than closing it.

Workspace fullscreen expands the existing SidePanel subtree across the shell's
content grid while retaining the topbar. It must not remount tab bodies, editor
drafts, browser frames, or PTY sessions. A focused terminal owns Escape; the
visible fullscreen control exits while focus is inside a PTY. It is desktop-only:
the phone has no activity-bar slot to expand, so a file there keeps its own
full-screen action. Only focus mode puts the fullscreen strip at the window's top
corner, so only focus mode reserves the Windows and Linux caption band on it.
The computer-use live view stays above the fullscreen layer and in reach, since it
shows and controls an agent driving the real desktop.

### Split view: leading edge and focus

The surface's top-left is the sessions-sidebar toggle's. In single chat the
title row carries it (mobile) or clears the shell's stationary button with a
60px inset and a hairline at 52px (desktop, sidebar collapsed). Split view does
not render that row, so `SessionGridLayout` hands `ownsTopLeft` to the one
geometric top-left leaf and `SessionGridView` gives that pane `leading`:
`inset` reserves the toggle's column on the pane's own row (`ChatPane`:
`pl-[49px]`, hairline at 41px; the picker card: `pl-[44px]`, hairline at 36px —
both are container x 52 and 44, the single-chat row's columns, measured from
where each pane's content starts), `control` renders the toggle inline ahead
of the title. The shell's toggle keeps its normal rect (`TOGGLE_RECT`) in split
view: the pane title row is the same height as the single-chat row, so it
already centres on it.

Split view marks focus by dimming every other pane as well as by the pane's
accent border: `PaneDim` lays a background-coloured rectangle over the pane at
`--pane-dim-opacity` (0.4), the way Ghostty fades an unfocused split, so
message text, code highlighting and status colours keep their own values
underneath and only the whole pane reads as "not the one with focus". The
overlay is always mounted while the pane knows its focus state (opacity 0 when
focused, so the cue fades both ways), sits at `z-20` above the message chrome
and below the drop overlay and every shell layer, and takes no pointer events,
so the click that claims focus lands on the pane. A pane outside split view
(`focused` undefined) never mounts it. The placeholder pane keeps its accent
dot as well.
