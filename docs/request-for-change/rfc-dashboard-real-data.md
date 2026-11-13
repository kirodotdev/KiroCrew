---
title: Dashboard real data -- a crewmate's Dashboard page shows real values or says it does not, never an invented or stale value presented as real
status: draft
author: kirocrew-worker, for the crewmate dashboard audit of 2026-10-30
created: 2026-10-30
last-audited: 2026-10-30
audited-at: 5e7214b942
doc-pr: null
implementation-prs: []
tracking-issues: [26259]
supersedes: []
superseded-by: []
---

# RFC: Dashboard real data

- Status: draft. Acceptance requested from a maintainer; the status flips to
  `accepted` when one records it here.
- This document lands first because several of its fixes change what a
  crewmate is told and what a tool reply says (`dashboard_write`,
  `dashboard_save`, the daily review), and one adds a page contract (`data.error`)
  that script crons and pages both follow. The First Principles lane reads that
  decision off the base.
- Related: [rfc-crewmate-dynamic-dashboard.md](rfc-crewmate-dynamic-dashboard.md)
  (the page, the frame, the folds),
  [rfc-crewmate-dashboard-upkeep-skill.md](rfc-crewmate-dashboard-upkeep-skill.md)
  (what each agentic field means and when to write it).
- Measured at `5e7214b942`.

## 1. Problem

A crewmate's Dashboard page is what the person reads instead of the chat
(`builtin_skills/dashboard/SKILL.md`: "the page is what they read"). So a value
on the page is taken as true. Today a page can show a value that is invented,
stale, or a placeholder, and nothing on the page tells the person so.

An audit of the update path on 2026-10-30 sorted the gaps into three layers, by
who enforces the fix:

| layer | enforced by | gaps | here |
|---|---|---|---|
| A. frame and gateway | code the agent cannot skip | A1-A4 | out of scope, in flight |
| B. tools | a check at write or save time | B5-B8 | §4 |
| C. process and meaning | contract, prompt, page | C9-C12 | §5 |

The A layer is the strongest and is being fixed in code by other work, so this
document only names it (§3). This document covers B and C.

## 2. Goals and non-goals

Goals:

1. A value an agent writes carries where it came from and when it was seen.
2. A save reply flags numbers a page hard-codes instead of reading from a field.
3. A save reply never reads as "the data is real" when the check ran on invented
   data.
4. A script cron that fails says so in a shape a page can draw, never as `0`.
5. A page never presents a worker's own `done` claim as an accepted result.
6. A page that is fed a capped list says how much was left out.
7. An agentic cell shows how old it is.
8. The daily review leaves a record and looks at the whole page.

Non-goals:

- The frame's missing-value display and fold staleness reporting (A1-A4, §3).
- Changing the fold data model. Every fix reads data the folds already carry.
- Refusing a page save. Page checks stay advisory warnings, as
  `dashboard_page_checks.py` already says ("never refuse the save").

## 3. In flight elsewhere (A1-A4)

Other workers are fixing these in code. They are named so this document does not
repeat them:

- A1, A2: the frame's display of a missing or unset value (`dashboard_frame.py`).
- A3, A4: staleness of `custom` records and reporting a fold the page has not
  seen (`FOLD_UNSEEN`).

Where a fix below needs one of them, it says so.

## 4. Tool layer (B5-B8)

### 4.1 B5 -- `dashboard_write` checks shape, not source

Problem. `dashboard_agentic.check_write` (`src/kiro_crew/dashboard_agentic.py`)
asks, in order: is there a dashboard, does it declare the field, is the field
agentic, is the value the declared type (`type_holds`), is it shallow enough
(`AGENTIC_VALUE_DEPTH`), does it match the shape (`shape_problems`), is it JSON,
is it under `DASHBOARD_VALUE_BYTES`. It then redacts the value. Nothing asks
where the value came from or when it was seen. A guessed number of the right
type lands exactly like a measured one, and the page draws both the same.

Fix. `dashboard_write` takes two more arguments:

- `source`: required, a short string (at most 200 characters) naming where the
  value was read: a tool and target (`gh pr view 123`), a fold path, a file, or
  the literal `judgment` for a field whose value is the crewmate's own opinion
  (`verdict`, `for_you`).
- `observed_at`: optional ISO-8601 time the value was read. Defaults to the write
  time. Refused when it is in the future by more than a minute.

`check_write` refuses `source_missing` when `source` is absent or blank, with the
same "what to do instead" sentence every refusal carries. The entry stores both.
The agentic fold keeps them beside the value, and the frame hands them to the page
as `window.kirocrew` `sources[field]` and `observed_at[field]`, next to the
existing `written_at` map.

Enforcement: tool (write-time refusal). A crewmate can still write a false
`source`, but it can no longer write a value with no claim at all, and the claim
is on the page for the person to check.

Tests (red before the change):

1. A write with no `source` is refused `source_missing`.
2. A write with `source` lands, and `dashboard_fields` returns `source` and
   `observed_at` for that field.
3. A future `observed_at` is refused.
4. Each shipped helper (upkeep RFC §4.3) prints a payload with `source`, and it
   passes `check_write`.

Rollout: one release where a missing `source` is accepted with a
`source_missing` warning in the reply, then the refusal. The `[DASHBOARD]` turn
block and the dashboard skill say `source` is required from the first release.

### 4.2 B6 -- hard-coded numbers in `view.html` are not flagged

Problem. `dashboard_save` (`api_dashboard_save` in
`src/kiro_crew/dashboard/handlers/agent_panel_dashboard.py`) runs the page once
(`dashboard_verify.verify_page`) and adds `shape_warnings`, `theme_warnings` and
font warnings (`dashboard/handlers/dashboard_page_checks.py`). None of them looks
for a number written into the page's visible text. A page that says "12 open
PRs" in its markup shows 12 forever, and it looks like data.

Fix. A new `literal_warnings(package)` in `dashboard_page_checks.py`. It scans
the page's text nodes (not attributes, CSS or script) for a number of two or more
digits, or any number next to a unit or count word (`%`, `PRs`, `tasks`,
`items`, a currency sign), that is not inside a `data-dashboard-field` cell. Each
hit becomes one line in the reply's `literal_warnings`: the line number, the text,
and "read it from a field, or mark it `data-dashboard-static` if it is a fixed
label". Years, version strings and anything inside an element marked
`data-dashboard-static` are skipped.

Enforcement: tool (save-time warning). It never refuses the save, so a false hit
costs one line.

Tests:

1. A page with `<p>12 open PRs</p>` gets one `literal_warnings` line naming line
   and text.
2. The same number inside a `data-dashboard-field` cell, inside
   `data-dashboard-static`, or in script gets none.
3. Every built-in template page saves with no `literal_warnings`.

Rollout: one PR. The dashboard skill gains one sentence on
`data-dashboard-static`.

### 4.3 B7 -- "passed" on synthetic data reads as "the data is real"

Problem. The save check runs the page over a synthetic copy of the record
(`dashboard_synth.synthesize`), never the record itself. The reply's
`verification` block (`Verification.wire` in `src/kiro_crew/dashboard_verify.py`)
already carries `"data": "synthetic"`, so the fact is there. But the field a
reader looks at is `"status": "passed"`, and no sentence in the reply says what
passed means. A crewmate then tells the person "the page is verified", and the
person hears "the numbers are right".

Fix. When `data` is `synthetic`, the reply's `verification` gains a fixed
`note`, the way a skipped run already gains `skipped_note`:

```
passed means the page ran without a script error over invented values shaped
like your data. It does not check that any value on the page is real or current.
Call dashboard_snapshot after dashboard_apply to see the page over the real record.
```

The dashboard skill says the same in its save section, and tells the crewmate not
to report a passed save as "verified data".

Enforcement: tool reply wording. No data or contract changes.

Tests:

1. A passed save over a synthetic copy carries the note.
2. A `dashboard_snapshot` run (`data: "record"`) does not.
3. A skipped run keeps its `skipped_note` and gets no second note.

Rollout: one PR, with the skill sentence.

### 4.4 B8 -- no contract for a script cron's error

Problem. A script cron writes a page value with `ctx.crew_log_append(type, data)`
(`src/kiro_crew/cron_script_context.py`). The `custom` fold
(`crew_log/projection_dashboard.py`) keeps the latest record per type with its
`at` and `writer`. Nothing says how a script reports that its fetch failed. A
script that catches the error and writes `{"count": 0}`, or a page that reads a
missing key as `0`, draws a failure as a real zero.

Fix. A `data.error` convention, written into the `crew_log_append` docstring, the
cron skill and the dashboard skill:

- A script that could not get its data writes the same `type` with
  `{"error": "<one line, at most 240 characters>"}` and no value keys. It does not
  write a default value.
- A page that reads `custom.latest.<type>.data` checks `error` first and draws
  the error text in place of the value, with the record's `at`.
- The previous good record is not kept by the fold. A page that wants the last
  good value writes it under a second type (`<type>_last_ok`).

`ctx.crew_log_append` gains `ctx.crew_log_error(type, message)`, a two-line
helper that writes that shape, so a script's `except` branch has one obvious call.
The frame's missing-value display (A1/A2) draws a record with `error` the way it
draws a missing value, once that work lands.

Enforcement: contract (helper, docs) plus frame (after A1/A2). The fold does not
reject a record with both `error` and values, because a record already in the log
must still fold.

Tests:

1. `crew_log_error` writes `{"error": ...}` and nothing else.
2. A custom record with `error` folds with its `at` and `writer`.
3. The built-in templates that read `custom` draw `error` text, not `0`.

Rollout: helper and docs in one PR; frame display follows A1/A2.

## 5. Process and meaning layer (C9-C12)

### 5.1 C9 -- worker `done` is the worker's claim, not acceptance

Problem. The audit wrote that the `workers` fold's `done` comes from turn-end
classification. That is not quite right. In
`src/kiro_crew/crew_log/projection_workers.py`, a turn end
(`_turn_end_state`) only ever yields `idle` or `failed`. A row reads `done` only
when the worker filed `work/recorded` with `status: done` (`work_report`). That
is a claim; the conductor's acceptance verdict is not read by this fold at all.
A page that labels the column "Done" or "Passed" presents a claim as an accepted
result.

Fix:

- The rendered row (`merge_render`) gains `verdict`: the `verdict` of the item
  this worker is bound to, read from the crewmate's own `work` fold
  (`crew_log/projection_work.py`, whose items already carry `verdict` and
  `worker_session_key`), else `null`. No new entry type.
- `dashboard_types` describes `state: done` as "the worker says it is done" and
  `verdict` as "the conductor's acceptance result".
- The dashboard skill tells a page to label `done` with no `verdict` as
  "reported done", and to show "accepted" only on `verdict: pass`.

Enforcement: data (the fold carries both) plus contract (wording). The page still
chooses the label; the type description makes the right one the obvious one.

Tests:

1. A worker that reports `done` reads `state: done, verdict: null`.
2. After the conductor's `verdict: pass` for that item, the row reads
   `verdict: "pass"`.
3. A `progress` report after `done` takes back `done` as today.

Rollout: one PR, fold plus type text plus skill sentence.

### 5.2 C10 -- caps drop rows that pages may not show

Problem. The audit wrote that caps truncate silently. The folds do count what
they drop:

- `workers` keeps `WORKERS_LIMIT = 12` rows and counts the rest in `omitted`
  (`projection_workers.py`).
- `custom` keeps `CUSTOM_TYPE_LIMIT = 64` types
  (`crew_log/entry_types_surfaces.py`), evicts the least recently written, and
  counts them in `types_omitted` (`_custom_render` in `projection_dashboard.py`).

So the loss is reported by the fold. It is silent on the page: a page that draws
`items` and never draws `omitted` shows 12 workers as if there were 12.

Fix. `literal_warnings`' sibling, `omitted_warnings(package)` in
`dashboard_page_checks.py`: when a page binds or subscribes to a fold that has an
omitted count (`workers.items`, `custom.latest`, and any other fold whose
`dashboard_types` row lists an `omitted`-style key) and the page text never names
that count key, the save reply carries one line: "`workers` drops rows past 12
and counts them in `workers.omitted`; draw it so the person sees rows are
missing". The built-in templates draw it.

Enforcement: tool (save-time warning). Static detection is a string match on the
key name, so it can miss a page that builds the key; that page is the author's
choice and the warning is advisory.

Tests:

1. A page that reads `workers.items` and never `omitted` gets one warning.
2. A page that reads both gets none.
3. Built-in templates that read a capped fold draw its count.

Rollout: one PR with B6, since both live in `dashboard_page_checks.py`.

### 5.3 C11 -- `written_at` is available but not shown

Problem. The frame hands every page a `written_at` map, one stamp per agentic
cell (`dashboard_frame.py`, `compose` and the subscribe patch). Nothing asks a
page to show it. An agentic value written a week ago looks like one written a
minute ago.

Fix:

- For a `data-dashboard-field` cell bound to an agentic field, the frame adds a
  small age line ("written 3 days ago") under the cell, unless the cell is marked
  `data-dashboard-age="hidden"`. Fold cells are unchanged; their freshness is
  A3/A4's job.
- A script-drawn page reads `written_at` itself; the dashboard skill says every
  agentic value it draws shows its age, and B5's `observed_at` when present.
- The save reply's `fields` rows, which already list `manual` fields, add "draw
  its age" to `manual_note`.

Enforcement: frame for bound cells (the strong case), contract for script-drawn
pages.

Tests:

1. A bound agentic cell gets an age line from `written_at`.
2. `data-dashboard-age="hidden"` suppresses it.
3. A fold cell gets none.

Rollout: frame change after A1/A2 land, since both touch how a bound cell is
filled in `dashboard_frame.py`.

### 5.4 C12 -- the daily review is advisory, and the snapshot sees one screen

Problem. The daily review is prompt text only: `DAILY_PAGE_REVIEW` in
`src/kiro_crew/dashboard/goal_reminder.py`, mirrored in the dashboard skill's
"The daily review" section. Nothing records whether it ran or what it found. It
also does not ask for `dashboard_snapshot`. And `dashboard_snapshot` captures the
viewport only: `_VIEWPORT` is 1280x800 in `dashboard_verify.py`, and
`dashboard_page_image.capture` sends `captureBeyondViewport: False` with a clip
of that size. Anything below the first screen is never seen by the review.

Fix:

- `dashboard_snapshot` takes `full_page: true`, which captures up to 1280 wide by
  at most 4 viewports tall (3200 px), downscaled by the existing half-scale
  fallback when the PNG is too large. The default stays one screen.
- `DAILY_PAGE_REVIEW` gains one step before "Check": call
  `dashboard_snapshot` with `full_page: true` and compare what it shows with
  `dashboard_fields`.
- The review ends with one `dashboard/reviewed` crew-log entry: snapshot taken
  (yes/no), fields found stale (names), fields rewritten (names). A built-in
  fold `review` keeps the newest one, so a page or the profile can show "last
  reviewed 2 days ago, 1 stale field fixed". The daily wake does not check the
  answer; it records it.

Enforcement: tool (snapshot), data (review entry), prompt (the step). The review
itself stays the crewmate's judgment; what changes is that skipping it is visible.

Tests:

1. `full_page: true` on a 3000 px page returns an image taller than 800.
2. The default snapshot is unchanged.
3. The daily prompt names `dashboard_snapshot`.
4. A `dashboard/reviewed` entry folds into `review`, newest kept.

Rollout: snapshot flag and prompt step in one PR; the review entry and fold in a
second, because adding an entry type is its own registry change.

## 6. Rejected designs

| design | why not |
|---|---|
| Refuse a page save with a hard-coded number or an undrawn omitted count | Static detection has false hits; a refused save blocks a page over a label. Page checks already never refuse (`dashboard_page_checks.py`). |
| Run the save check over the real record so "passed" means more | The dynamic dashboard RFC says a page nobody approved never receives a value; `dashboard_verify` keeps that rule. The snapshot after apply already runs on the record. |
| Make `source` optional with a warning forever | A warning a crewmate can ignore leaves the guessed number on the page, the harm B5 exists to remove. One warning release, then refusal. |
| Have the `workers` fold infer `done` from the last turn | A worker that answered and stopped reads the same as one waiting; the fold already refuses to guess (its module docstring). |
| Keep the last good custom record in the fold on error | Changes the fold for every record; a second type does the same job with no fold change. |
| Gate the daily wake on the review result | The review is judgment; a gate would make the crewmate write whatever passes. Recording it makes a skipped review visible without inviting that. |

## 7. Rollout

| step | content | depends on |
|---|---|---|
| 1 | This document | -- |
| 2 | B7 note + skill sentence | -- |
| 3 | B6 `literal_warnings` + C10 `omitted_warnings` | -- |
| 4 | B5 `source` / `observed_at`, warning release | -- |
| 5 | B8 `crew_log_error` + docs | -- |
| 6 | C9 `verdict` on worker rows + type text | -- |
| 7 | C12 full-page snapshot + prompt step | -- |
| 8 | C12 `dashboard/reviewed` entry + `review` fold | 7 |
| 9 | B5 refusal | 4, one release later |
| 10 | C11 age line + B8 frame display | A1/A2 |

Each step is one PR with its own red-before tests from §4 and §5.
