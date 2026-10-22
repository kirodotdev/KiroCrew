---
name: dashboard
description: "Build or change a crewmate's Dashboard tab: a page worth returning to, with real work woven into its design. dashboard_save stores it; dashboard_apply puts it live. Values arrive through declared fields, independently of the page."
triggers: dashboard, my dashboard, dashboard tab, change my dashboard, another dashboard, dashboard page, show me a different, go back to the old dashboard, build a dashboard, dashboard gallery, dashboard template
---

# The Dashboard Tab

One page per crewmate. You design it, the person owns it, and the gateway feeds
its declared fields. The page, the values and the frame have different jobs:

| Part | Job |
|---|---|
| page | your HTML, CSS and script, versioned when applied |
| values | recorded facts, pushed through the declared model |
| frame | isolation, text bindings and the subscription bridge |

## 1. Save, check, apply

Read `dashboard_fields` for the page in force and its fields, and
`dashboard_types` for the available folds and row keys. Tell the person what
changed when the new page is live. Work that has grown a lot -- more workstreams, a
longer history, a new kind of reading -- is a good reason to redesign the page
once; it is never a reason to redraw it every turn.

`dashboard_save` takes `model`, `view` and an optional `theme`. `view.html`
holds the markup, CSS and script; it is the only view, and a stored view of
blocks is read back as `view.html`. Every field read by a binding or a script belongs in `model`.
The gateway derives `kind` and `bound_to` from the caller. Saving stores a
draft; `dashboard_apply` with no arguments makes it a new live version.

The save runs the page once, offline, over a synthetic copy of the current
record: the same fields, list lengths, nulls and types, with invented values.
Booleans and fixed vocabulary words are kept; an enum uses its declared labels.
Keys are kept when the page spells them out or the type catalog names them.
Nested objects inside list rows can have invented keys. Read fields by their
published names. The browser sees the copy, not the record.

The `verification` result is `passed`, `failed` with script error positions,
`timed_out`, or `skipped` with a reason. A failed page needs a repair and a new
save before apply. A count above its total can be a synthetic-data mismatch;
a missing field, null or wrong type is still a case the page needs to handle.
A pass means the first paint raised no script errors: later pushes, small
screens, visual quality and colour contrast still need their own checks.

**Look at the page.** `dashboard_snapshot` draws your applied page in the
same offline run, over your real data, and returns a PNG file path; open it
with your file-read tool. It is one 1280x800 view of the top of the page, as
the person's tab shows it in the default light theme, so judge what it says as
well as layout, colour, type and fit; their own theme pack or dark mode can
show other colours. A page you only saved has no picture: apply it first. When the
run cannot start it answers "no image" with the reason and no file; an
earlier picture is not the page as it is now. A template page has no picture.
The file holds real values: leave it where it is.

The gateway's secret filter also processes markup and script. A note reports
how many places changed and the first line, without revealing the replaced
text. A long identifier-like string can trigger it; shorter pieces joined in
code avoid accidental identifier matches. The offline check does not fetch
CDN libraries. A script that stops only because such a library is missing
returns `skipped`; other errors remain failures. One check runs at a time.
Busy or low-memory hosts skip it; the save waits at most about twelve seconds.

A template from `dashboard_templates` uses `dashboard_apply(template_id=...)`.
`dashboard_preview` is an optional look when the person asks, not an approval
gate for authored code. `dashboard_rollback` restores a version listed by
`dashboard_fields`; restoring an old version itself creates a new version.

**Progress goes on the page, not in chat.** People check a crewmate once or
twice a day, and the page is what they read. Keep where your work stands on
your Dashboard page; the chat gets few reports, and when it gets one it covers
several things in one message, never a stream of small updates. No goal or
milestone is needed for a page: the first time you have work to report is
reason enough to build one.

**The daily review.** Your daily goal wake asks you to review your page:
read it against the latest data (`dashboard_fields` for each field and its
source, `dashboard_types` for the folds behind it), check that the page as a
whole still makes sense for the work you do now, and update what is stale --
a field with `dashboard_write`, the page with `dashboard_save` and
`dashboard_apply` when a section no longer fits. A page that is current stays
as it is. Then find one way to make the page better -- clearer, better made,
nicer to look at -- by sections 2 to 6 below, and make it: one small, steady
improvement a day rather than a redesign, every number still read from a fold
and none invented.

**Writing your page, and asking first on a template.** With nothing on your
Dashboard tab, having work to report, or a wake that tells you to build your
page, is the go-ahead:
write it, `dashboard_save` it and `dashboard_apply` it, then post one short
message that a Dashboard page is ready on the Dashboard tab -- no preview, no
card, and no second message. On a template page (one the user adopted, or the
page named for you) you never replace it unasked. When a Kiro Crew note in your
turn says you can offer your own page, call `offer_dashboard_page` once: it
raises one card asking whether to build a page around your goal in place of
that template, and writes nothing. If the user says yes you are woken to write,
save and apply it, with no second question, and post the same one message; a
locked layout refuses the apply, and you say so once and stop. If they say no,
or the card expires, nothing changes and you are not asked again. A page the
user asks for in chat needs no card: write it then.

**Layout lock.** The person's **Lock dashboard layout** switch on Profile
holds the current page. A `[DASHBOARD]` block marked LOCKED means apply and
rollback answer `dashboard_locked`. The person can unlock it; you can explain
that path and wait. Field writes and template previews remain available.
Only the owner writes its page or values. A worker reports through
`work_report`, whose record feeds the owner's folds.

## 2. A page worth returning to

Aim for a page the person enjoys opening, even before reading a number.
Its whole composition and the way the work appears within it create that
appeal. Clear data and a distinctive visual character belong together.

**Make the design your own.** Carry the user's chosen style through later
changes. When they leave the style open, choose a coherent direction from
the work and its context. That freedom includes a theme, atmosphere and
visual metaphor, as well as an abstract design. Let that character reach
across the page rather than stop at a heading or an icon.

**Give the whole page visual depth.** Backgrounds, wallpaper, pattern,
texture, expressive frames and layered surfaces are welcome parts of the
design. Their purpose can be atmosphere rather than measurement. Give the
data equal care: its placement, scale and visual form belong to the same
composition. A decorative layer can enrich the work without pretending to
be evidence about it.

**Let the evidence suggest the picture.** Look for change, comparison,
proportion, relationships and progress in what the page can actually read.
Charts reveal recorded changes over time; comparisons and distributions
can reveal a great deal from current values alone. Progress draws on a
known target or a defined total. A state without a measure can still have
a vivid visual presence and motion that means active, waiting or finished.
Choose the form that helps this work make sense, with detail available
where it is useful. A count can be part of a larger picture as well as a
readable number. A distribution of worker states is a measured comparison,
not a claim that each worker has completed a fraction of its task.

**Make it feel alive.** Subtle ambient motion and animation run constantly
while the page is in view, not only when a value changes, and lift the whole
experience. Data-driven motion on top helps the reader notice an arrival, a
change or a finish. Give those two
kinds of motion a clear purpose and a shared rhythm. Keep long-running
effects light on resources, without repeated layout work, and pause them
while the page is hidden. The still composition keeps its character and
meaning.

**Reduced motion is for the reader who set it.** Under
`prefers-reduced-motion: reduce` every value jumps to its target, ambient
layers hold still and no entrance plays; the page still answers input.
Every other reader gets the motion, so do not hold it back by default.

**Keep every claim true.** Numbers, labels and data-driven geometry agree
with their sources and with each other. Proportions have a stated meaning
and a real denominator. Time charts show recorded samples and their time
range. Missing readings, incomplete history and unmeasured progress stay
distinct from zero. Running or idle alone says nothing about how much of
a task is complete. Decoration remains distinct from measured evidence,
and an animation's intermediate positions are not new measurements.

**Make the work easy to read.** Use plain words and useful names, keeping
internal ids, session keys and file-system paths out of the visible page.
Map each phase, state and event type to its meaning; an unknown type gets a
truthful general phrase. Free text an agent wrote into a fold -- a ledger's
`next`, a report's summary, a waiting item's text -- can carry a session name,
a path or a whole build report: show it as a short plain sentence (its first
sentence, the rest behind a disclosure), with ids and paths left out. Important changes and things waiting on the person remain easy
to find. Colour works with words or shape to carry meaning. Decorative
layers leave text and useful controls clear, and long content fits from
a narrow view to a wide one: at 320px a label and its value wrap or stack,
and nothing is clipped or scrolls sideways.

**Design colour as a whole.** The tab's theme variables are a useful base;
custom variables, textures and independently coloured surfaces can also
belong to the design. Pair each surface with text that reads on it, and
check the complete result in both light and dark. Treat a colour warning
as a place to inspect and improve, rather than a reason to remove the
page's character.

**Pair type, do not name it.** The frame gives three role variables,
`var(--font-sans)`, `var(--font-serif)` and `var(--font-mono)`, each built
from generic families so every system draws its own face. Use at most two
families on a page, each with a clear job: one that reads, for headings and
body, and one for numbers, code or ids. Monospace is for code, ids and
aligned figures, never for body text or headings. Build hierarchy with
size, weight and colour, not with another family; `font-variant-numeric:
tabular-nums` aligns figures without switching to monospace. Text in any
one script stays in one style; the generic families already pick each
script's system face. A style the user names wins.

Judge the result both as a whole picture and as a view of the work. It
should remain worth looking at when the values hold still, and make a
real change in the work easy to see when they move. A tidy set of numbers
alone does not establish that quality.

## 3. What a glance should answer

The page has both visual character and a useful view of the work:

| Question | Available evidence |
|---|---|
| Where does the work stand? | work items, workstreams, relevant goal readings |
| What is running right now? | workers, workstreams, helpers in this session |
| What is stuck? | blocked work, failed helpers, outstanding dependencies |
| What needs me? | waiting items and approvals |

Anything waiting on the person is easy to find, never hidden. When nothing
waits, the page says so. Include `waiting.count` and `waiting.items` in the
model so a request arriving after the first paint is visible too. Choose
vocabulary that keeps worker sessions,
accepted work items and project outcomes distinct. A closed session does not
establish that its task was accepted. Decisions use the real `ask_question`
and approval surfaces; a drawn control carries no approval authority.

## 4. Choose evidence before drawing it

The page presents recorded facts rather than becoming a second status store.
Fold-backed fields update by themselves. Choose evidence that explains this
project, including relevant goals and milestones when they reveal progress;
repeating the Profile form is different from drawing a useful project view.

| Evidence | Fold and shape |
|---|---|
| worker sessions and current states | `workers.items`, `counts`, `total`, `omitted` |
| work items and their outcomes | `work`, `workstreams.items` |
| hourly accepted work and measured spend | `workstreams.series`: `hour`, `accepted`, `credits` |
| parallel work and task spans | each workstream's `tasks`: `created_at`, `closed_at`, `status` |
| goal readings and milestone counts | `goal.milestones` (`target`, `current`), `milestones_total` |
| recent recorded events | `timeline.moments`: `time`, `type`, optional event details |
| things waiting on the person | `waiting.items`, `count`; `approvals` |
| helpers, tool use and spend of the live session, since it started | `subagents`, `tools`, `usage` |

`dashboard_types` publishes paths, `row_keys`, `names_the_row` and nested row
hints. A path ends at an array: bind the whole array and read its rows in
script. Array length is a useful derived count, not the only possible binding.
A map is read by its published keys. `folded_through` reports how far a fold
has consumed its log; it is not a sample count or proof of nonempty data.

`workers` has states, not per-worker completion percentages. `goal` keeps a
latest snapshot: its `target` and `current` strings may be prose rather than
numbers. Show each milestone's reading beside its target, as text. Whether a
milestone is reached is what you recorded in its reading with
`key_result_progress` ("met", "done", a number), never something the page
works out; an arbitrary prose target is not a denominator. With no milestones confirmed, show that state rather
than a 0/0 ratio. A goal's text can carry a repository path: show the project
by its name, and a source by the file's name within the project, never an
absolute or home-directory path. A source list can lag a newer measurement: resolve differences against
the actual project before presenting an old issue as still open.
`workstreams` is the measured project history a page most often misses: each
board's `accepted` of `total`, its `tasks` with their spans, and `series`, hourly
buckets of accepted items and recorded spend -- a bounded set, not guaranteed
continuous history. `usage`, `tools`, `timeline` and `subagents` describe the
live session only, from when it last started; spend across the project is each
workstream's `credits`, with `credits_reported` false where nothing was measured. Lists expose omitted counts; retain that
meaning when drawing a subset. Unreported spend differs from measured zero.

For a project fact absent from folds, first look for its recording tool or an
existing project artifact. An agentic field can hold a verified snapshot as a
number, text, boolean, array or object, as well as your own verdict. Label its
source and observation time visibly, and use `dashboard_write` when that
source changes. A snapshot remains an observation at that time, not a claim
of automatic freshness. Repeated observations can form a measured series;
values invented for the sake of a chart cannot. The save reply names
`manual_fields`, and the frame marks agent-written claims.

## 5. The frame contract

The page is a body fragment in a gateway-owned iframe. Inline CSS, SVG and JS
can draw it. Network access is closed: `connect-src 'none'`; no fetch, XHR,
websocket, remote fonts or beacons. Libraries are limited to the frame's fixed
allowlist. Embedded imagery is an inline `data:` URL, in `view.html` or in
`theme.css`; `theme.css` refuses a `url()` with an address, which would load
nothing. A page cannot loosen the frame policy.

The iframe has `allow-scripts` only, an opaque origin, and no access to the
host's cookies, storage or DOM. If the owner's network policy withholds script,
a `<noscript>` explanation can say that the view needs it. Start script-filled
regions hidden and reveal them after drawing; an unfilled placeholder is not
evidence about the person's work.

Values arrive in three ways:

1. `data-dashboard-field="name"` fills an element with text. Useful for a
   scalar cell; arrays and objects appear as raw JSON through this path.
2. `window.kirocrew.fields` holds the same declared values, with `agentic`,
   `seq` and `stale` beside them.
3. `window.kirocrew.subscribe(name, cb)` calls `cb(value, name)` immediately
   and on each push. Its returned function unsubscribes. A chart or script-only
   field needs no extra text binding or hidden sink.

Subscribe during parse. The subscription survives a replaced fields object;
zero is a present value and still reaches the callback. A failing callback
leaves the others running. The `kirocrew-dashboard` event is also available,
but only announces a refill rather than naming the field.

Use `createElement` and `textContent` for recorded text. Inserting recorded
strings as `innerHTML` would turn somebody's data into page markup. Numerical
geometry comes from validated numbers. Bindings and graphics read the same
source, and labels say which subset they describe.

The host retains the last resolved fields and shows a stale band when a
source fails. Preserve that distinction rather than replacing an unread
value with a made-up zero. Your words and graphics agree with the folded
numbers; derive changing headlines from those values too.

## 6. Theme and reach

The frame supplies `--bg`, `--bg-elevated`, `--card`, `--card-fg`, `--text`,
`--text-strong`, `--muted`, `--border`, `--accent`, `--accent-subtle`, `--ok`,
`--warn`, `--danger` and other host theme variables. Your CSS and `theme.tokens`
can define custom properties too. An undefined variable uses its fallback;
a defined custom variable is a normal design tool.

Pair surface and text colours, either through host variables or your own
scheme-aware definitions. Fixed colours can also form a readable self-contained
surface when its text is designed with it. A fallback in `var(--text, ...)`
is unused while the tab defines `--text`: that text still changes with the
host scheme. A fixed surface can instead pair with its own text property,
including captions, bound cells and chart labels. The save's theme warnings
are CSS heuristics, not measured contrast failures. Inspect the named surface
in both schemes while preserving the intended design.

A page fits from 320px through an expanded view, with wrapping or disclosure
for dense detail. A banner has space without covering data. Links use
`target="_blank" rel="noopener noreferrer"`. Data-driven changes stay readable
while ambient layers give the page continuity. When a bound value moves or a
new item lands, the `living-ui` skill says how that motion should feel. Skip
the idle tour it describes for explainers: this page stays open all day.

## 7. Pages to start from

`gallery/` and `GALLERY.md` beside this file show how a page declares and binds
its fields, not how a page should look. Read a package for the connection you
need, then compose the page for this work. Its plain appearance is a wiring
example, not a quality target. Your model declares every field your script or
markup reads; the controller pushes only that list.

Search documentation with `search_docs` and locate controls with `find_ui`
when a fold, field or setting needs clarification.
