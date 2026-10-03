# Built-in dynamic-dashboard templates

Four templates in the format `kiro_crew.dashboard_templates.manifest` defines: one
directory per template, holding `manifest.json` and `template.html`. The registry
discovers this directory; `load_template` is the only gate, and a user template passes
the same one.

| id | folds it reads | what it answers |
|---|---|---|
| `project-report` | `workstreams` | what this crewmate did, what each thing cost, what came of it, what needs the reader |
| `goal-board` | `work` | the work ledger it conducts: items, states, verdicts |
| `work-kanban` | `work` | that same board as columns, including the one waiting on the conductor's own verdict |
| `session-ledger` | `ledger` | what a long-running session is carrying, and what it would resume from |

`project-report` is the DEFAULT: a crewmate that never adopted a template renders it,
so the first thing anybody sees is the report rather than an empty frame
(`kiro_crew.dashboard_templates.instance.DEFAULT_TEMPLATE_ID`). The three round-3 pages
it replaced -- `crewmate-overview`, `work-map`, `spend-and-tools` -- were each one slice
of the same question asked three ways, and a reader had to assemble the answer from
three pages of engineering counters. One page answering the four questions in order
replaces them.

## The two rules every page here follows

**Absent is not zero.** Each fold reports how many turns reported a measurement next to
the measurement itself: `usage.turns.credits_reported`, `usage.turns.tokens_reported`,
`usage.turns.duration_reported`, and `usage.credits_by_source.*.reported`. A total of 0
beside a reporter count of 0 means nobody said, so the page draws a dash. Printing 0
would state an amount the record never claimed. The same applies to a fold's empty
string, which is a key the fold declares and nothing has filled.

Session-wide credits gate on the SUM of `usage.credits_by_source.*.reported`, not on
`usage.turns.credits_reported`. A crewmate that only delegates has a turn-scoped
reporter count of zero beside a real total.

**A truncated list says so.** Every fold here is bounded and reports what it dropped
(`timeline.dropped`, `tools.names_omitted`, `work.omitted`). Each page draws that number
beside the list it belongs to, so a partial picture is never shown as a whole one.

## How a page gets its values

The host fills every `data-dashboard-field` element by `textContent` and sets
`window.kirocrew = {fields, agentic, seq, stale}`. A field whose type is `array` or
`object` therefore reaches its bound element as JSON, which is unreadable in a cell, so
each page overwrites that one cell with a count and draws the real thing from
`window.kirocrew.fields` in its own script. Every page renders on load and again on each
`message`, because the order of the host's first fill against the script is not the
page's to assume.

No page fetches anything: the frame's CSP blocks the network, so a chart drawn from a CDN
renders as a hole. The charts are plain elements sized in the page's own JS.

## Agentic fields

`session-ledger` declares exactly one, `headline`: the crewmate's own one-sentence read
of where the work stands. No fold records a human-facing headline, which is the bar for
an agentic field. The page marks it from `window.kirocrew.agentic` rather than from its
own belief, so a value the crewmate wrote never looks like one folded from the record.

`project-report` declares one for the same reason, `for_you`: the lines the lead wants
its reader to act on next. The record holds which items are blocked or asking a
question, and the page folds those itself; what it cannot fold is the lead's own
judgment about what matters, so that half is written and marked as written. The page
draws the `crewmate wrote this` tag only when the host says the field IS agentic, so a
value arriving some other way cannot borrow the label.

`goal-board` and `work-kanban` declare none, because every number they show has a fold.

## Checking and rendering them

```
env -u KIROCREW_HOME .venv/bin/pytest -q -n 0 test/test_dashboard_templates_builtin.py
python scripts/render_dashboard_builtin.py \
    src/kiro_crew/dashboard_templates/builtin/<id> \
    test/fixtures/dashboard_templates/pod_folds.json out.png
```

The test resolves every declared `{fold, path}` against
`test/fixtures/dashboard_templates/pod_folds.json`, the eight folds a real pod session
served. Nothing in `load_template` can tell whether a path EXISTS, so a plausible path
that resolves to nothing would otherwise ship and render a blank cell forever.

## Adding one

Add the directory with both files, add its id to `EXPECTED_IDS` in the test, and check
the packaging globs still cover it (`[options.package_data]` in `setup.cfg` and the
`recursive-include` lines in `MANIFEST.in`). A template absent from the wheel leaves the
loader shipped and working while the registry discovers nothing, with every test green.
