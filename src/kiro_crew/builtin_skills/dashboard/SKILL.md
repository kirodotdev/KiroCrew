---
name: dashboard
description: "Load this when somebody asks about their crewmate's Dashboard tab: a page that shows something particular, a change to the one they have, or going back. dashboard_save stores your page, dashboard_apply puts it live; every change is a version. Every number comes from a fold, never from your typing."
triggers: dashboard, my dashboard, dashboard tab, change my dashboard, another dashboard, dashboard page, show me a different, go back to the old dashboard, build a dashboard, dashboard gallery, dashboard template
---

# The Dashboard Tab

ONE page per crewmate. You write it, the person owns it, and the gateway keeps
it fed.

Three parts, and knowing which is which is most of the job:

| Part | Who | Rule |
|---|---|---|
| the page | you write the HTML | changes only when you apply it, and only you can |
| the values | the crew log folds | pushed in as they move, never typed by you |
| the frame | the gateway | no network, text-only bindings, nothing you write runs outside it |

## 1. You own it, and every change is one rollback away

Your page goes live on apply, and only you can apply it:

```
dashboard_save -> dashboard_apply -> live, as a new version
```

1. **Save.** `dashboard_save` takes the package's `model`, `view` and `theme`.
   `view.html` carries your markup, your CSS and your script, and it is the only
   view a crewmate page has: a `view` of `blocks` is refused. `model` names every
   field you bind, and it is also the list the controller pushes, so a binding
   the model does not declare is refused at the save and told to you by name.
   The gateway fills `kind` and `bound_to` from who you are, so the package is
   always your own. Saving stores it; the page in force does not move yet.
   The save then RUNS the page once, offline,
   over a SYNTHETIC copy of your crewmate's current data: the same fields,
   nesting, list lengths, nulls and types, with invented values (booleans and
   the schema's fixed words are kept, and an `enum` field shows one of its own
   labels at random). A key inside a value is kept only if your page spells it
   out, as a word or in quotes, or the dashboard type catalog lists it for
   that value, so reach data by the names you write in the page. In the rows
   of a list read from the crew log, only a row's own keys are kept: the keys
   of an object nested inside a row are invented. The page never sees the
   record, so a value quoted in an error is not a real one. The reply carries a `verification`
   block: `passed`, `failed` with each script error and the `view.html` line and
   column it was thrown at, `timed_out` when the run hit its time limit, or
   `skipped` with the reason (no system-installed browser on the machine, for
   one). Fix a failed page and save again before you apply it.
   An error that depends on how two invented values compare (a count above its
   total) may come from the copy; one that depends on a missing field, a null or
   a wrong type does not. The page is run as the dashboard shows it: the
   gateway's secret filter rewrites text in a page that looks like a key (a long
   run of letters and digits can), and when it changed yours the reply says how
   many places and the line of the first, never what was replaced. A script that
   depends on such a string breaks on the dashboard and here, and nowhere else;
   write the string in shorter pieces and join them in code. The run has no
   network, so a library the page loads
   from the CDN allowlist is not fetched. A page whose script stops at that
   library's name (`Chart is not defined`) comes back `skipped`, not `failed`,
   because the rest of that script did not run; any other error is still
   `failed`. It checks the first paint only: a refill that arrives later is not
   exercised. The save waits for the run, which usually takes about a second and
   never more than about twelve. One page is checked at a time across the gateway,
   so a save that finds another check running, or a machine short of memory, is
   `skipped` at once and does not wait.
2. **Apply.** `dashboard_apply` with no arguments puts your saved package live
   as a new version. Your `dashboard_write` fields keep working on it, and
   `dashboard_fields` lists them.
3. **Say what changed**, not what the page shows -- they are looking at it.

There is no preview step. A person cannot see an exfiltration script by looking
at a page, so a "please approve" step would add a click and no safety. What
keeps the page safe is the frame (section 4). What keeps the person in charge is
the history: every apply is a version.

A TEMPLATE page goes live the same way: `dashboard_apply` with a `template_id`
from `dashboard_templates`. `dashboard_preview` is only a look, for when the
person asks to see one first; it changes nothing.

**Going back** is `dashboard_rollback`. `dashboard_fields` lists the versions a
rollback can reach; a version is either a template page or one of your saved
packages, and the one you restore is the one that draws. Going back moves
FORWARD: restoring version 1 over version 2 writes version 3, so it is itself
undoable.

**When the layout is locked.** The person can lock the page's layout with the
**Lock dashboard layout** switch on their Profile tab, and the Dashboard tab
then shows a small **Layout locked -- unlock on Profile** badge-button. The lock
is a ceiling over you: while it holds, the tab keeps the page that was in force
when they locked it, and `dashboard_apply` and `dashboard_rollback` answer
`dashboard_locked`. Your `[DASHBOARD]` block says LOCKED when it is. Then do
not apply or propose another page: ask them to
unlock it, and wait until they say they have. Only the person can unlock it, and
no tool of yours can. You can still preview a template for them to look at, and
`dashboard_write` values still land.

Only the owner writes the page or its values. A worker is refused on every
artifact write path to another crewmate's package and told to `work_report`
instead; the ledger fold then carries its report onto the page by itself.

Three things not to do:

- Do not change the page without telling the person what changed.
- Do not report what the page shows. They are looking at it. Say what changed.
- Do not write a number into the markup. Bind it.

## 2. The design is yours; the honesty is not optional

Choose the information architecture and the visual form the work needs, and
change it when the work changes. A timeline, a dependency map, an experiment
comparison, a release board, a deliverable gallery: examples, not required
panels and not a template.

What is fixed is what the page is allowed to claim:

- **Real evidence only.** Real states and real counts. No invented percentage,
  no made-up elapsed time, no fabricated approval, no "complete" from an idle
  worker.
- **A projection, not a second store.** The page presents what the ledger and
  the folds already hold. It is never the place a status lives.
- **One page, updated in place.** Change the page the person has; do not mint a
  new one per milestone.
- **Numbers come from bindings.** A number you type is true once, at the moment
  you typed it, and wrong every time the page is opened afterwards. A number
  read from a fold is true tomorrow.
- **No imitation controls.** Decisions go through the real `ask_question`
  surface and the runtime's own approval flow. A button you draw in HTML cannot
  change anybody's permissions, and a page that implies it can is lying about
  authority it does not have.
- **Say which claims are yours.** A value you asserted and a value the log
  recorded are different kinds of claim. The frame marks yours; do not blur it.

### A page that updates itself

Every field on the page reads a crew-log fold, so the page moves the moment an
event lands and stays true when you are not writing to it:

| what the page shows | the fold |
|---|---|
| the goal, each milestone's target and latest reading, met or open | `goal` |
| what waits on the user: open questions and cards waiting on a button | `waiting` |
| your worker's items, their state and what each produced | `work`, `workstreams` |
| spend, activity | `usage`, `timeline` |

When the page needs a fact no fold carries, the fact belongs in the crew log as
an event type of its own, recorded where it changes (by the gateway, or by your
worker through the tool that records it), rather than a value you keep typing.
A hand-written field (`{"agentic": true}`) is for prose only you know, such as
your own verdict. The `dashboard_save` reply lists every field with its source
and names the hand-written ones under `manual_fields`, so you can see which
cells go stale the moment you stop writing them.

Two checks the gateway makes on every save, because the page run is skipped on
a machine with no browser (the reply's `verification.note` then says nothing
was checked): `shape_warnings` names each list, record or time you bound to a
`data-dashboard-field` cell, which shows it raw (`[]`, an ISO string), so draw
it in your script (`dashboard_types` and `dashboard_fields` carry `rendering`
for each type); `theme_warnings` says your fixed colours cover one colour
scheme only, so the page is unreadable in the other.

### A look that belongs to the work

Take the page's look from your own context: the project you work on (its
README, its branding, its domain, its language, what the code does) and your
goal (its subject and milestones), so each crewmate's page reads as part of its
own work rather than a stock layout. A style the user asks for, in chat or on a
card, wins over that and stays on every later rebuild. Either way the colours
come from the dashboard's theme variables, so light and dark both read.

Search the Kiro Crew documentation with `search_docs` (and `find_ui` for where
a control is) before guessing how a fold, a field or a setting works.

## 3. The manager view: five questions on one screen

A manager opens this page to know where the work stands. They do not care how
the system works.

| # | Question | Where the answer comes from |
|---|---|---|
| 1 | Is it done? | a count out of a total, from the ledger fold |
| 2 | What did it cost? | the usage fold |
| 3 | What is left? | open items, from the ledger fold |
| 4 | What is stuck? | blocked items and never-ran lanes |
| 5 | What needs me? | anything waiting on a person, FIRST and in a warm colour |

Hard limits, and they are limits rather than advice:

- One screen at 1280x800 with no scroll.
- Needs-you first whenever anything waits on a person; with nothing waiting,
  say "Nothing right now" rather than hiding the block.
- A headline of one line, at most ten words.
- No sentences inside a block. Numbers over words.
- Detail on demand, folded in `details`, not spread across the screen.

Write the vocabulary down before you draw: what one item is, what done means,
what waiting means. A page whose words drift from the work reads as wrong even
when every number is right.

## 4. The frame contract

Your page is a body fragment in a frame the gateway owns. It may run JS.

Three ways a value reaches it, and you choose per element:

1. `data-dashboard-field="<name>"` on an element: the host sets that element's
   **text**. No markup, no attributes -- a text node, so a value can never
   become an element. This is the path for every cell a reader reads.
2. `window.kirocrew.fields` in a script: the same values as data, for a chart to
   draw from. It also carries `agentic` (the values you wrote rather than the
   gateway folded), `seq` (the fold watermark) and `stale`.
3. `window.kirocrew.subscribe(name, cb)`: `cb(value, name)` runs once when you
   subscribe and again on every push that carries that field, and it returns a
   disposer. This is the path for anything you DRAW -- a tree, a chart, a scene.

The host fills (1) itself from the same object it hands to (2) and (3), so a
page cannot show one number in a cell and a different one in its chart.

Where your own script builds rows, create elements and set `textContent`. A
value out of the crew log is whatever somebody wrote there, so assigning a
template string to `innerHTML` is how a recorded value becomes markup on your
page. The bindings in (1) cannot do that to you; your own markup can.

**Hearing an update.** Subscribe during parse and keep drawing: the callback
fires immediately against the read the page already holds, so first paint has
the numbers, and the registry outlives each refill even though the host
replaces that object wholesale. A field counts as present or absent,
not truthy, so a counter reaching `0` still reaches your callback. A throw in
one callback costs only that callback, not the others on the page. There is
also a plain `kirocrew-dashboard` event on `window`, but it only says that
something landed and leaves you to diff a whole read to find out what;
`subscribe` is the better entry point. Bindings update whether or not you
subscribe, so subscribe only for what you draw.

What the frame refuses:

- **No egress.** `connect-src 'none'`. No fetch, no XHR, no websocket, no remote
  font, no beacon. Publish fresh content through the tool instead.
- **No dynamic exec.** The frame mounts with `allow-scripts` and nothing else,
  on an opaque origin with no access to the dashboard's cookies, storage or DOM.
- **A fixed library allowlist**, and it can be used for nothing else, because
  nothing can be sent to it.

Egress is closed harder here than a chat card needs, and the reason is what this
page HOLDS: real numbers out of the operator's own crew log. A page that can both
read those and open a channel is an exfiltration path with a data source
attached.

If your page needs a script to draw itself, carry a `<noscript>` fallback saying
so, and ship anything a script fills HIDDEN so the script reveals it.
`allow-scripts` is withheld wherever an admin ceiling restricts the owner
agent's network egress, and a page that shows a placeholder or an empty-state
line in that mode states something about the owner's data that is not true.

**Staleness.** A field whose source did not resolve is reported, not guessed at.
The page keeps the last values that did resolve and the host shows a band saying
the data is stale. A half-filled page is the one state a status surface must
never reach, because a reader cannot tell it from a current one.

## 5. Which values exist

Fold names available today: `status`, `usage`, `timeline`, `tools`, `approvals`,
`subagents`, `class`, `outline`, `ledger`, `radar`, `work`, `panel`, `agentic`,
`mistakes`, `workstreams`, `goal`, `waiting`, and a tree-keyed `worktree`.

Each is a list of paths, and a path is dotted keys only -- there is no index
syntax, so a per-row value has no path and a list's LENGTH is what you bind.

Read the live list rather than this paragraph. `dashboard_types` says which
folds exist and what each one holds, so you read a number from a real path
instead of guessing one, and the `dashboard-template` skill's `FOLDS.md` and
`folds.json` are generated from the product's own projections. `dashboard_fields`
tells you which fields the page you are on declares, which of them are yours,
and which of your past writes were refused. Read it before you write.

Every fold here is available. None is required. A page that needs two of them is
a correct page.

## 6. Theme and reach

The `widgets` skill owns the theme contract and this page follows it: theme
variables, never hard-coded colour, so the page tracks light and dark without a
second design.

- Readable at 320px wide, through to an expanded view. No fixed-width canvas and
  no table that forces the whole page to scroll sideways; stack or disclose dense
  content on a phone.
- Motion off under `prefers-reduced-motion`.
- Colour is never the only carrier of a state: pair it with a word.
- Every link gets `target="_blank" rel="noopener noreferrer"`.
- Plain-language names. A session or run id belongs in folded detail, never as a
  main label.

## 7. Pages to start from

`gallery/` beside this file holds whole working packages. Copy one, pass its
`model`, `view` and `theme` (the gateway fills `kind` and `bound_to`), change
what it binds, and save it with `dashboard_save`; nothing loads a gallery page by
id, so editing your copy cannot reach anybody else's.

`GALLERY.md`, also beside this file, lists those packages and the pages that
shipped together, with the reader question each one answers. Copy one and adapt
it, mix two, or write something none of them describes. They are source to
read, not pages to pick.

One rule a copied package will teach you the hard way: the package's `model` is
the list of fields the controller pushes, so a name only your markup knows is a
cell nothing ever fills, and the save is refused and says so. Declare every
field you bind.
