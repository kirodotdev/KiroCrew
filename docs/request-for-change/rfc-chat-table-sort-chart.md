---
title: Chat Markdown tables -- sort by column, right-align numeric columns, and chart a column from the action row
status: draft
author: garohop, with Kiro Crew
created: 2026-11-22
last-audited: 2026-11-22
audited-at: 31b3f1e744
doc-pr:
implementation-prs: []
tracking-issues: [40451]
supersedes: []
superseded-by: []
---

# RFC: Chat Markdown tables -- sort by column, right-align numeric columns, and chart a column from the action row

- Status: draft. Nothing is built on main. A working prototype of every
  behaviour below is `garohop/KiroCrew@91ca20de0`, four commits on the author's
  fork, and becomes the implementation pull request once this document is
  accepted. Symbols named as new below are on that commit; the gate on this
  directory refuses line numbers, so each is cited by name.
- Tracking issue: [#40451](https://github.com/kirodotdev/KiroCrew/issues/40451)
  (`tier:T3`, `parked`; triage asked for a design through this process).
- Related: [#37976](https://github.com/kirodotdev/KiroCrew/issues/37976) (the
  dashboard's own tables show their sort direction) and
  [#40452](https://github.com/kirodotdev/KiroCrew/issues/40452) (a chart fence
  the agent writes on purpose, a separate request).
- Why an RFC: the change adds the first interactive view state to a rendered
  chat message, and the First Principles lane reads a new user-facing capability
  from the base branch.
- Measured at `31b3f1e744` (main). Every citation below is on that commit unless
  it names the prototype commit.

## 1. Summary

Give the chat's Markdown table three things the reader can do without another
model turn: click a header to sort, read numbers that line up, and open a bar
chart of one numeric column from the table's existing action row. All three are
view state in the dashboard. The table's markup while it streams, its Copy
Markdown and Copy CSV output, and what reaches Slack, Telegram and the other
channels do not change.

## 2. Motivation

### 2.1 What exists today

- `MarkdownTable` (`website/src/components/markdown/MarkdownTable.tsx`) wraps
  every table the renderer produces and gives it an action row of Copy Markdown,
  Copy CSV and Widen, plus column grips. No header is a control, and nothing in the file reads a column's
  values.
- The `th` and `td` overrides in `ELEMENT_OVERRIDES` (`elements.tsx`) emit plain
  cells with a fixed class each: no alignment varies by content.
- The copy actions (`hastTableToMarkdown`, `hastTableToCsv` in `tableClipboard.ts`)
  already read the table's hast node through a private `plainCell` and a
  private `PLAIN_NUMBER` shape in the same file, so the data is available to
  the component without parsing the DOM.
- The renderer (`MarkdownRenderer.tsx`) publishes `PathProbeCtx` (declared in
  `contexts.ts`) as `!typing` per block in `MarkdownBlock`, and `BlockRenderer`
  draws a `MermaidBlock` only once `block.complete` is set.
- The dashboard's own tables sort through `SortableHeader`
  (`SortableHeader.tsx`), which renders its own `<th>` from a string label, and
  `SortArrow` is private to that file.
- No chat table renders a chart.

### 2.2 The problem

A Markdown table is the most common structured thing an agent puts in a chat
reply, and it is static:

- **No sorting.** To find the largest row the reader scans, or spends a model
  turn asking for the same table re-sorted. In a scheduled digest there is
  nobody to ask.
- **Numbers read as prose.** A numeric column is left-aligned with whatever
  decimals the model wrote, so `9.4` and `10.1` do not line up.
- **A chart costs a turn.** The only way to see a column as a chart is to ask
  the agent, which writes a hand-made widget: another turn, a few KB of HTML and
  JS, often a library from a CDN, and a result with no copy or source controls.
  The data is already in the table.

## 3. Goals

1. Sort a settled table by any column, ascending then descending then source
   order, with the direction announced to assistive technology.
2. Detect numeric columns from the table's own text and right-align them with
   tabular figures.
3. Show a bar chart of one numeric column, in the table's current sort order,
   drawn as inline SVG in theme variables, from a button on the existing action
   row.
4. Change nothing for a table that is still streaming, a one-row table, the
   copy actions, or any channel surface.

## 4. Non-goals

- Rewriting cell text (decimal normalisation, thousands separators, unit
  suffixes). Deferred; see open question 1.
- Chart types other than a bar of one column; download or expand controls on
  the chart panel.
- Persisting a sort across reloads or across the same table in two messages.
- A chart fence the agent writes on purpose
  ([#40452](https://github.com/kirodotdev/KiroCrew/issues/40452)).
- Any change to how an agent is told to write tables.

## 5. Design

The prototype adds three files beside `MarkdownTable.tsx` and edits five
existing source files and every locale catalog. For a table such as

| City | Country | Population |
|---|---|---|
| Lagos | Nigeria | 15,388,000 |
| Lima | Peru | 10,092,000 |
| Oslo | Norway | 709,000 |

the reader sees Population right-aligned, every header marked sortable, and a
Show chart button on the action row.

### 5.1 Reading the table

A new `tableData.ts` reads the hast `<table>` into a header row and body rows of
plain text (`readTable`), reusing the CSV copy's `plainCell`, which the
prototype exports from `tableClipboard.ts`. `parseNumeric` accepts the CSV
copy's `PLAIN_NUMBER` shape, also exported, plus a leading currency symbol. A column is
numeric when every non-blank body cell parses and at least one does
(`numericColumns`).
`sortedRowOrder` returns a permutation
of row indices: numeric columns by value, text columns by the app locale's
collator, blanks last in both directions, ties stable. Nothing here touches
React.

### 5.2 Sort state and where it lives

`MarkdownTable` gains a `useTableView` hook, which
holds the sort state and the numeric-column flags and publishes them through a
new `MarkdownTableCtx` in `contexts.ts`, keyed by the table's hast node the
way the renderer's other per-element contexts are. The `th` and `td` overrides
delegate to two new cell components, `MdTableHeadCell` and `MdTableCell` in
`MarkdownTableCells.tsx`.
Inside the context, a header cell wraps its label in a sort button carrying
`aria-sort`, a muted at-rest chevron pair (Lucide `ChevronsUpDown`) and the
solid arrow on the sorted column; a numeric body cell adds
`text-right tabular-nums` (`NUMERIC_CELL_CLS`). Outside the context both emit exactly the markup
main emits today, which is how the renderer's DOM golden test passes unchanged.

Rows are reordered by cloning the rendered `tbody` children into the sorted
order (`orderRows`), not by rebuilding the table from
hast. The hast node, and therefore Copy Markdown and Copy CSV, keep the source
order; the chart follows the view order.

### 5.3 The streaming gate

Sorting and the chart appear only once the block has settled (`PathProbeCtx`
read in `useTableView`, the table's analogue of the Mermaid gate) and the table
has at least two body rows (`MIN_ROWS_TO_SORT` in `tableData.ts`, applied in
the same hook). A streaming table and a one-row table
render byte-for-byte as before.

### 5.4 The chart

A Show chart / Hide chart button with the Lucide `ChartColumn` icon joins the action row when the table has at least one
numeric column. It opens a panel under the table (`MarkdownTableChart.tsx`): a
column picker using the project's `SimpleSelect` (lint forbids a native
`<select>`) when more than one column is numeric, and one
inline `<svg role="img">` of bars filled with `var(--accent)` over a zero
line in `var(--border)`, labelled by the first text column. Labels and
values are HTML lists beside the SVG rather than `<text>` in a `viewBox`, so
type never scales below the readable floor on a phone. The reveal respects
`useReducedMotion`.

### 5.5 Accessibility and i18n

Header buttons form one roving tab stop per table; Arrow Left and Arrow Right
move between columns. `aria-sort` on the `th` reports the direction, and the
`th` keeps its own name as the column text through `aria-labelledby`. Five new strings
(`show_chart`, `hide_chart`, `chart_column`, `chart_of_column`,
`sort_by_column`) live under the renderer's catalog namespace in every locale
(`components.markdownRenderer` in `en.manual.json`), with `en-XA.json`
regenerated by `npm run i18n:pseudo` as `website/docs/i18n-catalog.md` asks.
Icons are `lucide-react`; no emoji, no hand-drawn glyphs.

### 5.6 What does not change

The table markup before the gate opens, the copy output, the chat config's widen
setting, and the Markdown the agent wrote. Channel renderers never see the
dashboard component, so Slack, Telegram and the others are untouched.

## 6. Migration plan

One phase, one pull request: the prototype as it stands, rebased on main and
opened from the author's fork after this document is accepted. No flag: the
gate in section 5.3 keeps every table that is still arriving, and every
one-row table, unchanged.

Exit criteria, each a test the pull request carries (prototype
`MarkdownTable.sortChart.test.tsx`, twelve tests):

- Clicking a header cycles ascending, descending, source order, and the arrow
  and `aria-sort` follow; Copy Markdown after a sort still yields the source
  order.
- A text column sorts by locale with blanks last; a numeric column sorts by
  value.
- The header buttons are one tab stop and Arrow keys move between them; a
  two-row table still stops once.
- A table inside a typing block, and a one-row table, render no sort button and
  no chart button; the renderer's DOM golden test is unchanged.
- A column with one text cell is not numeric; a column of plain numbers is, and
  its cells carry `text-right tabular-nums`.
- Show chart renders one `role="img"` SVG of bars in the view order, filled
  with the theme variable; no numeric column, no button; blank and negative
  values draw a zero line and no bar for the blank.
- Every unsorted header shows the at-rest hint, and the button is named
  "Sort by {column}".
- `catalogParity` passes with the five new keys in every locale.

## 7. Backward compatibility

Additive. A reader who never clicks a header sees right-aligned numbers, a
muted hint on each header and one more button on the action row. Tables that
pin their markup in tests (the DOM golden, the table-pinning files, the
clipboard tests) pass without change because the one-row and streaming paths
are byte-identical.

## 8. Security considerations

The chart renders cell text the model wrote. Labels and values are React text
nodes in HTML lists, never `dangerouslySetInnerHTML` and never SVG `<text>`
built from a string; bar geometry is computed from `parseNumeric`'s numbers, so
a cell that does not parse contributes no bar. The SVG carries no `<script>`,
no external `href`, no `<foreignObject>`, and loads nothing over the network.
Sorting reorders React elements the renderer already produced; it introduces
no new parse of the Markdown source. Nothing is persisted.

## 9. Alternatives considered

- **Reuse the dashboard's `SortableHeader` and its table hook.** It emits its
  own `<th>` with the dashboard's uppercase label style from a string, and the
  hook persists per table id in local storage. A chat table's header is
  arbitrary Markdown and its sort should die with the rendered message. The
  prototype reuses the sort-state type, the arrow glyphs (`SortArrow`, which the
  prototype exports from `SortableHeader.tsx`) and the tri-state cycle, not the
  component.
- **Rewrite numeric cells with `fmtNumber`** (`website/src/i18n/format.ts`) to
  a consistent decimal count. Rejected for this cut: the `td` override is
  shared by every table, and a year or an id would become `2,026`. Alignment
  and tabular figures get most of the benefit with no text change.
- **A chart fence.** Complementary, filed as #40452. It serves the agent that
  intends a chart; this document serves the tables it already writes.
- **Ask the agent.** Today's answer: a model turn per view change, and no one
  to ask in a digest.

## 10. Open questions

1. Decimal normalisation: should a later cut format numeric cells (max decimals
   per column, capped), and which columns are exempt (years, ids)?
2. Further chart shapes: a second numeric series, a line for a time-like first
   column, download SVG / PNG as the Mermaid block offers.
3. Sort persistence: should a sort survive a reload of the transcript, or stay
   component state as proposed?
