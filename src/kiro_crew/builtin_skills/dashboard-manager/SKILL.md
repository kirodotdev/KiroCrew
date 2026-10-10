---
name: dashboard-manager
description: "Load this whenever somebody asks about their crewmate's Dashboard tab: a page showing something particular, a change to the one they have, or what it should show. No templates to pick - you compose the page from the data-type catalog, answer five fixed questions, and MEASURE the one-screen limits."
triggers: dashboard, my dashboard, dashboard tab, change my dashboard, another dashboard, dashboard page, build a dashboard, compose a dashboard, manager view, dashboard for this work, what should the dashboard show, dashboard package
---

# Dashboard: the manager view

A manager opens this page to know where the work stands. They do not care how
the system works. They care about five things.

There is **no template to adopt**. You declare a Model (which values the page
reads), a View (which blocks draw them) and a theme, store it as one artifact of
`kind="dashboard"`, and the product's own renderers draw it. You never write the
markup and no code of yours runs in the page.

## 1. The five questions (always answer these)

| # | Question | Where the answer comes from |
|---|---|---|
| 1 | What got done? | `work` / `workstreams` fold: items the conductor accepted |
| 2 | What did it cost? | `usage` fold, or `workstreams` credits per item |
| 3 | What is left? | Open items and how far along they are |
| 4 | What is stuck? | Items blocked or waiting, how long, and on whom |
| 5 | What needs me? | Questions and decisions only the person can make |

The page passes when someone can answer all five in 10 seconds, from one screen,
without reading a single sentence.

## 2. Structure

1. **A headline that states the conclusion** ("Eight tasks shipped. Six wait on you.").
2. **One band with the five answers** as large figures.
3. **Progress**: how far along, as a share rather than a count of rows.
4. **"Needs you"**: the few things only the person can clear, most urgent first.
5. **Spend**: what it cost, split into done and still-open work.
6. **Freshness**: what the numbers were read from and when.

You may reorder, merge or replace a block that fits the subject better. Never
drop one of the five answers.

## 2a. Hard limits (a dashboard is not a report)

Too much text and too much content is the worst failure. These are limits, not
suggestions; break one and the page fails the self-check.

- **One screen.** Everything fits in one viewport at 1280x800. No scrolling.
  If it does not fit, cut blocks, do not shrink type.
- **At most 5 blocks** besides the headline.
- **Headline: one line, at most 10 words.** No paragraph under it.
- **No sentences in blocks.** Labels of 1-4 words and numbers only.
  A block may carry one caption of at most 8 words.
- **"Needs you": at most 3 items**, each a title of at most 6 words plus its
  buttons. No explanation text; details live in chat.
- **Detail on demand.** Anything longer goes behind a hover, a click to expand,
  or a link to the artifact (venues.md, the PR). Not on the page.
- **Numbers over words.** If a fact can be a number, a bar or a dot, it is not
  a sentence.

Measure these; do not assert them. See section 7.

## 3. Words fit the subject

Write a small vocabulary before you build: what one "item" is, what "done"
means, what "waiting" means here. A hiring loop says "candidate" and "offer
sent"; a launch says "deliverable" and "shipped"; code says "PR" and "merged".
Use those words everywhere on the page.

## 4. The catalog: what you may actually bind

Read it live with the `dashboard_types` tool rather than from this file. The
names below are what exists today; nothing else is admissible, and inventing a
type gets the write refused.

### The package has five top-level keys

`kind` (always `"dashboard"`), `bound_to` (`crewmate:<slug>` or
`session:<slot key>`), `model`, `view` and `theme`. `model.types` maps a field
name to its type and source; `view.blocks` is a list, **and its order is the
layout**; `theme` carries `tokens` and `css`.

### The headline is a `note` block, placed first

There is no headline key. Put the sentence in a field of type `text` with an
`agentic` source and give it to a `note` block - the note draws a value in the
display face, larger than its own title. Writing the sentence into the block's
`title` instead makes it part of the layout, so every rewrite cuts a new
artifact version; **data never versions, layout does**.

So a page at the limit has six blocks: the headline note plus five.

### Field value types - five, and every one is ONE SCALAR

| `type` | holds | extra keys |
|---|---|---|
| `number` | one numeric reading | `unit`, `precision` |
| `text` | one short single-line string | `max_len` |
| `timestamp` | one ISO-8601 instant | - |
| `enum` | one of a fixed set of labels | `choices` (required) |
| `bool` | a yes/no flag | - |

**There is no array and no series in a package.** A fold's list cannot be bound
as a list. Express a list as: its length as a `number`, the top one to three
rows as separate `text` fields, and each row's state as an `enum`. This is the
single biggest difference from the old templates, which bound `items` whole.

### Where a value comes from

Each field carries `source`, spelled one of two ways:

- `{"fold": "<name>", "path": "<dotted keys>"}` - folded from the record.
- `{"agentic": true}` - a value you write yourself with `dashboard_write`.

Use `agentic` only for a fact no fold holds: your own headline, your reading of
a code host's check board, your judgment about what matters next. A number that
has a fold must come from the fold.

Fold names that exist. Keyed by one session: `status`, `usage`, `timeline`,
`tools`, `approvals`, `subagents`. Keyed by a slot: `ledger`, `radar`, `work`,
`panel`, `agentic`, `mistakes`, `workstreams`. Keyed by a tree root:
`worktree`. A `path` is dotted keys only - it has no index syntax, so nothing
inside a list is reachable.

### Block types - ten

| `type` | draws | fields | accepts |
|---|---|---|---|
| `stat` | the page's largest figure | 1 | any |
| `stat_band` | a divided band of figures | 2-6 | any |
| `table` | one aligned row per field | 1-24 | any |
| `list` | fields stacked label-above-value | 1-16 | any |
| `note` | a callout, values set in the title | 1-4 | any |
| `bars` | horizontal bars, each a share of the largest | 1-12 | `number` |
| `gauge` | one number as a ring; a second is the total | 1-2 | `number` |
| `pills` | chips carrying a shape and a word | 1-16 | `enum`, `bool` |
| `timeline` | events in time order | 1-8 | `timestamp`, `text`, `enum` (needs one `timestamp`) |
| `orbit` | a turnable 3D ring, with the same numbers as text | 2-12 | `number` |

Every block may carry `span` (width in a 12-column grid) and `caption` (one
line). Respect the field counts: a `stat` with two fields is refused, and so is
a `bars` over a `text` field.

### Correction recorded on the interface

`"package"` is **not** added to `instance.RENDERABLE_SOURCES`, and never will be.
The v3 page is gated by `member_dashboard._minted_package_page` instead. Do not
propose widening that set.

## 5. The guides are starting points

`guides/` holds 13 worked examples, one per view the old template set covered
plus `task-pipeline-card` for the fleet view none of them did. Read the one
whose reader's question matches yours.

**A guide is never a template to apply.** Mix two, change the blocks, drop half
of one, or invent a view no guide describes. The guide tells you what a reader
learns from that shape and which types it binds; whether that shape fits *this*
subject is your call, and a page that looks like the guide for a subject it does
not suit is a worse page than one you composed yourself.

## 6. Data rules

- Every number comes from a fold unless no fold holds it. Never invent or round
  a number to look better.
- **Absent is not zero.** If a figure is missing, say "not reported" and draw a
  dash. A total of 0 beside a reporter count of 0 means nobody said.
- **A truncated list says so.** Each bounded fold reports what it dropped
  (`work.omitted`, `timeline.dropped`, `tools.names_omitted`). Draw that number
  beside the count it belongs to, so a partial picture is never shown as whole.
- Text you write yourself is fine; it arrives through the `agentic` source and
  the page marks it as yours. Do not pass a judgment off as a folded fact.

## 7. Theme: never hard-code a colour

Colours and type come from the package `theme` tokens and the host theme
variables. Name a role, never a hex value, in anything you write.

| role | token |
|---|---|
| page and card ground | `--bg`, `--surface` |
| body and emphatic text | `--text`, `--text-strong` |
| quiet and less-quiet text | `--muted`, `--muted-strong` |
| hairlines | `--border`, `--border-strong` |
| the one colour that carries emphasis | `--accent` |
| needs attention / failed / succeeded | `--warn`, `--bad`, `--good` |
| the empty part of a bar or ring | `--track` |
| headline, body and figure faces | `--display-font`, `--text-font`, `--mono-font` |

Every token has a light value and a dark value, so a page is read in both. You
may override any of them in `theme.tokens` (at most 64) and add CSS in
`theme.css` (at most 32 KB, sanitized). `--accent-soft` has no base value on
purpose: it is mixed from `--accent` at the use site, so overriding `--accent`
carries the wash with it.

In SVG set `fill` and `stroke` through `style`, never as presentation attributes
with `var(...)`: an attribute does not resolve a CSS variable and renders black.

## 8. Aesthetics

Read `AESTHETICS.md` next to this file and follow it: start from the subject, be
lively, stay readable, design within the sandbox, and self-check.

## 9. Hand it over only after you have MEASURED it

Render in both themes at exactly 1280x800 and read the numbers out of the page.
A hard limit you asserted is a hard limit you did not check.

1. Store the package, then render it - `render_dashboard(package, read,
   theme="light"|"dark")`. The sandbox has no network: no CDN, no remote fonts,
   no remote images.
2. Screenshot both themes headless at 1280x800 and **look at both**. Reading the
   numbers is not looking at the page: a selector collision, a bar flung to the
   wrong edge or a font silently falling back are visible only in the pixels.
3. Read these four numbers out of the rendered page and record them:
   - `document.scrollingElement.scrollHeight` - must be <= 800;
   - `document.scrollingElement.scrollWidth` - must be <= 1280;
   - the block count - must be <= 5 besides the headline;
   - the headline word count - must be <= 10.
4. Give the probe a positive control before you trust it: inject an oversized
   element, confirm the measured scroll height exceeds 800, remove it. A check
   that has not been shown to see a failure cannot report an absence.
5. Fix and re-render until all four hold in both themes. Then attach both
   screenshot paths with the four numbers.

Running under budget is the right direction to fail. A page that leaves room at
the bottom passes; a page that needs a scrollbar does not.

Screenshots are evidence, not repository content: keep them out of git.

## 10. This skill replaced the template picker

There is no catalog to search and no `template_id` to preview. If you were told
to look for a page that fits, or that you cannot write the page yourself, that
was the retired `dashboard` skill and it is no longer true: composing the page
is the job. `dashboard_types` reads the catalog of TYPES, not of pages.
