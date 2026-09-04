# Page Layout Guide

Every dashboard page follows one layout pattern. Copy the skeleton below rather
than inventing a custom layout: a page that diverges costs a reader the
orientation cues (title block position, scroll container, section rhythm) that
every other page gives them for free.

Components come from [`src/components/ui.tsx`](../src/components/ui.tsx); the
conventions around them (a11y, data fetching, typography) live in
[frontend-conventions](frontend-conventions.md).

## Page skeleton

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

A dismissible banner above the content:

```tsx
<div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
```

## Animations

`animate-rise` on cards and banners, `animate-scale-in` on inline reveals. Both
are Tailwind utilities defined in `tailwind.config.js`, and both use
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
