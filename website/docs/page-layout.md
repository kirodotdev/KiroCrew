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
  <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
    {/* optional StatCard row, then Cards with tables/forms */}
  </div>
</>
```

`PageHeader` owns its own `px-6 pt-2 pb-3`, so the content container supplies the
horizontal padding for everything below it. `overflow-y-auto flex-1 min-h-0` is
what makes the content the scrolling region while the header stays put: the shell
is height-locked, so without `min-h-0` the flex child refuses to shrink and the
whole page scrolls instead.

`PageHeader` also takes an `actions` node, rendered right-aligned on the title
row. Put page-level buttons there rather than in the first `Card`.

### Horizontal insets below the breakpoint

Padding stacks, and the eye reads the SUM. On a wide viewport a page gutter plus a card
inset plus a row inset is comfortable; at 390px it is not. The skill-budget row measured
16px (page) + 20px (`Card`) + 16px (row) = **52px** before its text, against 32px for the
same text in chat.

The page container keeps the `px-6 pb-8` the skeleton above prescribes -- that is what
`AUTOSDE.yaml`'s `page-layout-pattern` requires, and it is not the layer to change. The
third layer is the one to drop:

**Below `md`, prefer no horizontal padding on a row that is a DIRECT child of a `Card`.**
The page gutter and the card's own inset already supply it:

```tsx
<div className="… py-2 md:px-4">   {/* row: the card supplies the inset while narrow */}
```

Gate **every** row in that card the same way -- section header, group header, data row,
footnote. Gating only some of them leaves the data rows sitting to the left of the headers
that label them, which reads as rows escaping their own section.

**The direct-child part is the precondition, not a detail.** The rule works because the
card is what supplies the inset the row gives up. Put an unpadded bordered pane between
them and that stops being true:

```tsx
<Card>                                                   {/* 20px */}
  <div className="… border border-border rounded-md">    {/* 0px, draws a visible edge */}
    <div className="… px-4 py-2.5 border-b">             {/* row: px-4 is its ONLY gutter */}
```

Here the row's `px-4` is load-bearing -- gating it puts the text flush against the border.
The excess inset belongs to the card, but the card is NOT what yields: halve the card's inset
below `md` and pull the pane out by exactly that amount, on the shell the pane and its
loading skeleton share so the layout does not jump when data arrives. The two numbers
are ONE number -- changing the inset without the margin pushes the pane past the border:

```tsx
const PANE_SHELL_CLASS = 'flex gap-3 max-md:-mx-2.5 …'  /* cancels the card's max-md:px-2.5 */
```

Measured at 390px on the Skills tab: the pane goes from left 37 / width 316 to left 17 /
width 356, so a row inside it starts at ~33px instead of ~54px, against 32px for the same
text in chat.

**Do not flush the card itself** (`max-md:px-0`). Its padding is also the only gutter the
toolbar above the pane has, and removing it puts the search field's rounded border
directly against the card's border -- measured as a 0px gap, and the first thing a reader
calls ugly. Halving it (`max-md:px-2.5`, a measured 10px) keeps the field off the border
while giving the row back most of the width. An inset toolbar above a full-bleed list is the ordinary phone pattern; the
two do not need to share a left edge.

This does not touch the page container's `px-6 pb-8`, which is what `AUTOSDE.yaml`'s
`page-layout-pattern` names and is not the layer to change. For a pane that must reach the
SCREEN edge, past the page gutter, cancel more inside the pane (`-mx-6` while narrow).

**Status: a direction, not a description of the repo.** Two shapes are migrated --
`SkillContextBudget` (direct-child rows) and the `SkillsTab` / `SteeringTab` split panes
(card flush). A scan for `className="…px-4…py-2"` under `website/src/pages` matches ~27
rows across 15 files, but a hit is not a work item: most are toolbars, banners, sticky
bars and buttons that own the only gutter their content has, and rows inside a bordered
pane must keep theirs. There is no lint gate for this. Read the structure around a hit
before gating it, and see kirodotdev/KiroCrew#3939 for the triage of all 27.

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
  `PageHeader` + the `px-6 pb-8` container.
- Use a raw `<input>` / `<button>`. Use `Input`, `Btn`, `SendBtn`,
  `SearchInput`, `Checkbox`.
- Use a native `<select>`. There is no styled wrapper for one any more — see
  §Forms for which dropdown component to reach for. Enforced by
  `no-restricted-syntax` in `eslint.config.js`.
- Use raw status text. Use `Badge` or `SourceBadge`.
- Use `text-xs`. Use `text-[13px]`.
- Add a new CSS `@keyframes`. Use Framer Motion, or an existing utility.
