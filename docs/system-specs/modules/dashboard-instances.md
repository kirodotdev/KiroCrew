# Mate dashboard instances

A template is shared. An **instance** is one mate's copy of one template, and the
copy is the whole design. A built-in template shipping a new version does not change
what a mate's Dashboard tab shows, and a mate editing its page is editing its
own copy rather than everybody's template.

The instance store is two modules and one route, each with one job:

| Piece | Where | What it owns |
|---|---|---|
| registry | `dashboard_templates/catalog.py` | the built-in templates and the operator's own, one loader |
| instance | `dashboard_templates/instance.py` | the copy, its versions, rollback, state, history |
| route | `dashboard/handlers/member_dashboard.py` | what the frame reads |

The agent-facing surface is separate and MCP-only: `dashboard/handlers/agent_panel_dashboard.py`, behind the `dashboard/handlers/agent_panel.py` facade, serves the `/api/agent-panel/dashboard/{fields,write,templates,preview,apply,rollback}` routes, `dashboard_agentic.py` checks and records agentic field writes and the mistake book, and `dashboard_feed.py` is the O(fields) subscription channel a filled page reads fold values through.

An array or object field is emptied with `dashboard_write` `clear: true` and no
`value`. The MCP server (`mcp_panel.py`) reads the field's declared type from the
`/api/agent-panel/dashboard/fields` route, then posts `value: {}` for an object field
and `value: []` for any other field, so the write route and its body schema
(`DASHBOARD_WRITE_SCHEMA`) see an ordinary write and type-check it the same way; the
tool's own argument schema is `DASHBOARD_WRITE_TOOL_SCHEMA`. The MCP server does not
refuse a clear for the field's type or source: a string, number or boolean field, a
fold-sourced field and an unknown field all reach the write route, which refuses them
(`wrong_type`, `field_not_agentic`, `unknown_field`) and records each refusal in the
mistake book. When the field list cannot be read, the server posts `value: []`, so
emptying an array field does not depend on the fields route. The flag exists because
an MCP client can drop an empty list, an empty object or a top-level null from a tool
call's arguments before the server reads them, so `value: []` or `value: {}` arrives
as no value at all and is refused as missing; a boolean arrives intact.

The sharing and snapshot modules are designed in the RFC and built behind the surface
that calls each -- a chooser, an export button -- because a module with no caller is
one nobody can review against a use. Neither is in the tree yet, so this table names
no file for them: a path to a file that does not exist reads as a broken document
rather than as work still to come.

The template FORMAT is `dashboard_templates/manifest.py` and nothing here restates it:
every path above -- a built-in, an edited copy, a restored version, and the imported
file a later phase adds -- goes through `load_template` or the `parse_manifest` +
`check_parity` pair it is built from. One loader means one place a malformed template is
refused, and it is why an agent-written page cannot become a dashboard that renders an
unowned empty cell.

## Three versions never mix

Each is spelled out and none is called "the" version, because conflating any two
produces a page a reader cannot place:

**Template version** is the version of the template that was copied. Frozen at adopt.
An edit never moves it: editing a copy does not make the mate the template's author,
and advancing it would make the instance claim a parity with a template it no longer
matches.

**Instance version** is the copy's own counter, `+1` per accepted change. It never goes
backwards -- see the rollback rule below.

**Data seq** is the crew-log position the rendered VALUES were read at. It belongs to the
data channel; `instance.py` neither reads nor stores it, and a snapshot is the only place
all three appear together.

## A rollback moves forward

Rolling an instance at version 2 back to version 1 writes version 1's payload as version
**3**. Rewinding the counter would make the instance version ambiguous -- two different
pages would both have been "version 2" -- and a client caching by version would keep
serving the page it was told was replaced. The restored payload is re-checked before it
is committed: it passed when it was written, and a format since tightened must refuse it
rather than install a page that no longer loads.

Payloads are kept per version under the member's own space, which is what makes a
rollback a read rather than a reconstruction. `MAX_RETAINED_VERSIONS` bounds them, and a
rollback to one that has aged out says which versions are still kept instead of silently
restoring the oldest it has.

## Four states, derived

`empty | live | stale | error`, and the state is DERIVED at read time rather than
stored. A stored flag would be a claim about the registry made when the instance was
last written, and the registry moves on its own: a template shipping a new version makes
every copy of it stale without touching one instance file, so the flag would say `live`
forever.

The corollary binds whoever edits a shipped page: a change a copy should pick up -- a
bug fix in the page, a new bound field -- raises the manifest `version`. A copy turns
`stale` only when the registry's version is higher than the one it froze at adopt, so
an unbumped fix reaches new adoptions alone, and every page adopted before it keeps the
old behaviour while the frame says `live`.

`empty` and `error` are deliberately separate, and that distinction is the reason the
read does not 404. A mate that never adopted a template answers 200 with `empty`,
because having no dashboard yet is the ordinary first state of every mate and the
frame's empty state IS that answer. `error` is a record that was damaged or hand-edited
after it landed. The two need opposite answers from a human -- one is a fresh mate,
the other is a dashboard that stopped working -- so a 404, or one word for both, would
hide the second behind the first.

An `empty` mate is SHOWN a page only when one is named for it, from
`default_instance(slug)`: the shipped page whose id is the mate's slug. Any other
`empty` mate has no page in force: a generic page nobody chose would read as the
mate's own. An `empty` root session is shown `DEFAULT_TEMPLATE_ID`. Only shipped pages
match by name; an operator's page never does. The read route and the `[DASHBOARD]` turn
block both resolve through it, and nothing is written. No shipped page may be named
`default`.
With no page in force, the mate's Dashboard tab on `/members` shows the suggestions
page (`CrewDashboardEmpty`), and the turn block says so and lists no fields and no upkeep
pointer. A shipped page named for the mate is drawn as before. The decision is made
ONCE, by `dashboard_agentic.shows_suggestions` (template instance served, record `empty`,
no page named for the slug, no package on screen): the read route sends it as
`suggestions: true` and the website draws what that says, and `turn_block` asks the same
function, with the same package lookup (`dashboard_agentic.bound_package`) and the same
lock rule as the route. There is no field to write either: `dashboard_fields` says the
mate has no dashboard yet, `dashboard_write` is refused `no_instance`, and the
profile's upkeep view shows nothing. A value written before a mate lost its default
stays in the crew log; nothing here deletes or migrates it.

`stale` covers two cases with different sentences: the template moved ahead of the copy,
and the template left the registry. Nothing is missing from the copy in either case --
it still renders.

Each state carries a `state_reason`: one sentence a surface can show a person. The four
words are what a client BRANCHES on; a client cannot branch on prose and a person cannot
act on `stale`.

## The history is a fold, the record is a file

`instance.json` is the current value. Every accepted change ALSO appends one
`dashboard/instance_changed` entry to the mate's own DM session crew log, and the
history is a fold over those entries, resumed from a savepoint beside the record.

The entry carries what CHANGED and never the page. That is what makes it bounded by
construction -- it can never be refused for size however large the page is -- and it is
why the page is kept per version on disk instead: a log entry holding every page would
make the one durable record of a change the thing most likely to be rejected.

The record being the file and not the log is what makes a change with the emitter off
still succeed, the same posture `panel/published` takes. The savepoint is stepped
whether or not the append lands, because the committed version must not be a version
its own history does not mention.

`action` is a CLOSED enum, and the vocabulary lives beside the entry type in
`crew_log/entry_types_surfaces.py` with the store importing it. One tuple, two readers: a fourth
action added in the store alone would be a change the store reports as taken and the log
drops.

## Share: one file, validated inbound

A template is a DIRECTORY of two files, which is right on disk and wrong to send: two
files that must stay together is a pair that arrives separated. The share format is one
JSON document carrying both halves, self-identified by `format` and `format_version` so
a reader that finds another value stops rather than assembling a page nobody wrote.

Validation is on the way IN, not out. An export writes what the registry already
loaded, so it is valid by construction; an import is the untrusted direction and is
parsed and parity-checked before anything is written, so a malformed share never becomes
a directory the registry then has to report as broken.

An import does not overwrite. A colliding id is refused and names the collision, and
`as_id` is how both are kept. Overwriting silently would replace the template a
mate's dashboard was copied from -- and because the instance holds its own copy, the
dashboard would keep rendering while the registry no longer held what it was made from,
which is the one failure nothing downstream could detect.

An imported template declares `shared`, so a reader asking where it came from gets an
answer other than "somebody here wrote it". None of this is in the tree, and it cannot
land on its own: the registry scans only the built-in directory and the operator's own
folder, and `RENDERABLE_SOURCES` does not hold `shared`, so share ships with the same P2
wrapper document that lets a page nobody here reviewed render at all. Import without that wrapper would be a second route
to the hole the preview refusal closes.

## Registry rules

**A broken template is reported, not raised.** `list_templates` returns the entries it
could load AND the problems it could not. A registry that raises on the worst member of
a set stops working the first time somebody hand-edits a file, and a directory omitted
rather than reported makes a template somebody just got wrong indistinguishable from one
that was never written.

**The directory name is the id.** A directory holding a manifest naming a different id
is two names for one template, and a lookup would find or miss it depending on which
name the caller happened to hold.

**`source` is checked against where the file actually is.** A template in the built-in
directory may declare only `builtin`, and one in the operator folder only `user`.
Without that check the field would be worth nothing to a surface reading it to mean
"this shipped in the product".

**Two roots are scanned, and no agent can write either.** A page that renders runs its
own script against the mate's values inside a frame that can navigate itself, so a
registry that scanned an agent-writable directory would let the agent choose what a
mate's dashboard executes. The two roots:

| Root | Origin | Why a person put the page there |
|---|---|---|
| `dashboard_templates/builtin/<id>/` | `builtin` | it is in this repo and went through review |
| `<data home>/panel-templates/<id>/` | `user` | the operator placed it; every agent write path is refused on this folder |

The operator folder is the one the panel's own template overrides use (`<id>.html`
files). A dashboard page there is a DIRECTORY, so the two never collide: panel
discovery reads only `.html` files and the catalog reads only directories. Its fences
are the panel's, and each is pinned for a page one level down by
`test_dashboard_operator_pages.py`: the sandbox seals the folder read-only and
pre-creates it, the folder name refuses an alias, the file-tool gate refuses a write
under it, and it is classified child-readable.

Three more rules hold for the operator root only:

* **A built-in id wins.** An operator page with a shipped id is reported in `problems`
  and never served in its place.
* **A linked folder is refused** (`agent_panel.override_templates_dir`), reported under
  `panel-templates`, and the built-in pages are still served.
* **A linked entry is refused.** The page directory, `manifest.json` and
  `template.html` must each be a real file or directory under that name.

`RENDERABLE_SOURCES` is `{builtin, user}`. A page an agent writes, or one imported as
`shared`, renders from neither root; see the RFC.

## Snapshots: all five parts or nothing

A live dashboard answers "what is true now", which is the wrong tool for "what was true
when this went wrong". A snapshot is the second question, and it is the field values plus
everything needed to know what they meant: template id, template version, instance
version, values, seq.

Each is required. A bag of numbers with no template is a page nobody can redraw; a page
with no seq cannot be placed against the log it came from; values with no instance
version cannot be told from values the mate's next edit would have laid out
differently. A snapshot that cannot name one is refused rather than stored with a gap a
later reader fills in by guessing.

The versions come from the INSTANCE, never from the caller: a snapshot's whole value is
that the values and the thing that laid them out were read together, and a
caller-supplied version could name a page these values never appeared on.

Frozen means frozen. There is no function that changes a snapshot, and writing one twice
under the same id is refused. A snapshot the present can rewrite is worthless as
evidence. Ids are MINTED, never supplied -- the id is also the filename, so a
caller-chosen id is a path a caller chose, and a stamp-ordered id makes a directory
listing a chronology without opening every file.

## The HTTP boundary

`GET /api/members/{slug}/dashboard` is the one route the frame reads, and its body is
`{instance_version, template: {id, version}, html, manifest, state}` plus
`state_reason` and `structure_locked`.

`?member=` is REQUIRED on every route, exactly as the briefing and rules reads require
it. Slugification is lossy, so two crew names can reach one slug; an instance is one
directory per slug, so for a colliding slug the instance belongs to neither mate, and
serving it to both -- with an editor -- would let them overwrite each other's dashboard.
The exact name must derive this slug, exist in config, and be the only name that does.

Both the READ and every WRITE are owner-gated, the same boundary `work_ledger_board`
draws: the body carries this mate's work-ledger and crew-log data (task titles,
summaries, PR links), so arriving as rendered html does not make it a wider audience's. A
non-owner is refused rather than served an empty read, which would show its numbers as
stale when the truth is that they were withheld. A write mutates a stored page the
gateway later renders. App tokens are denied outright -- an app
token scoped to `/api/members` reaches these by prefix.

All five rules live in one `_resolve` chokepoint, so a route added later cannot forget
one of them, and a test reads the handlers' own syntax trees to assert every one calls it
and every write calls the owner gate.

The page is scrubbed before it is served, in both halves of the body (the raw `html` and the
composed `rendered_html`), by `_template_text_safe`. Markup, attributes and prose get every
redaction pass. The text of a `<script>` or `<style>` element gets every pass but the
bare-secret guess for two shapes, so a page's code arrives as its author wrote it (a long alphabet to index
into, a base64 data URI) while a key id, PEM block, JWT or prefixed token written into that
code is still masked.

`dashboard_frame.map_page_text` takes the full pass over the whole page as its baseline, and the
one change it allows is to put the code pass's result for a script or style element's text where
the baseline had the full pass's. So the page differs from the baseline only inside that text,
and only by an ordered alphabet and a data URI payload, the two shapes the code pass spares. The baseline is cut at that text and the pieces are
scrubbed one by one; a rule that reaches across a cut (a PEM block, a labelled value) makes them
differ from the baseline, and then the baseline is the answer. The browser reads the page that
comes out, so the result is scanned again, and unless the kept text is still exactly its script
and style text the baseline is the answer as well. A page the full pass leaves alone, or with no
such text, is the baseline.

`dashboard_frame.code_spans` finds that text, and what it finds is a licence to apply a
weaker rule, so it reads the page the way the HTML tokenizer does where that decides what is
code: a `<script` inside a comment, a quoted attribute value or a `<textarea>` opens nothing,
a `>` inside a quoted value does not end its tag, a bare value runs to white space or `>`, a
script that holds `<!--` ends where the tokenizer's escape states end it (a `</script>` that
closes a nested `<script>` does not), and names fold ASCII case and split on ASCII white space
only. Where the tree builder decides
instead, the scan reads a simple `<svg>` or `<select>` (text and ordinary tags up to its end
tag) whole and goes on; any other `<svg>` or `<select>`, and every `<math>` and `<template>`
(a column group inside one ignores a `<style>`), ends the scan.
A frame may run with scripting on or off, and `<noscript>` holds text in one and markup in the
other, so the scan ends at it too. It also ends at anything it cannot read (an element or tag
that never ends, `<script/>` in HTML, `<plaintext>`) and after `_MAX_SCAN_STEPS` constructs.
Everything after that point keeps every pass, as does every element past the first
`MAX_CODE_PIECES`, so a page the scan doubts gets more masking, never less. A key in any other shape, labelled or not, is masked as it is anywhere. A key-shaped window inside
a data URI payload of 64 characters or more is served as written, and a shuffled table is rewritten
as before. A base64 data URI in an attribute (an `img` `src`) is markup, so it still gets every
pass.

## The staged preview is beside the record, never in it

A preview is a page somebody is being SHOWN before anything is recorded, and the store
keeps it in `preview.json` next to `instance.json` rather than as a version.

It cannot be a version. A version is what a rollback can reach, so staging one would put
a page the person then declined into their own history as a change they made; and
`instance_version` would move, invalidating every client that caches by it, for a page
that was only looked at.

`stage_preview` takes a `template_id` the catalog serves, and nothing else can be
staged. It still ACCEPTS `html` and `manifest` parameters, and refuses any call that
carries either with `AUTHORED_PAGE_REFUSAL` -- one sentence, defined once, saying that a
page written here cannot render yet and that custom templates come later. Keeping the
parameters is what makes the refusal total: a caller added later cannot stage an
authored page by forgetting a check of its own, because the check is in the store rather
than in each route into it.

The staged pair is validated by the same `parse_manifest` and `check_parity` every
committed version runs. A template directory hand-edited into a page whose bindings no
longer match its manifest is therefore refused before anybody is asked to look at it,
rather than at apply, after a person has already said yes.

Staging REPLACES any previous preview. One page is offered at a time because one
question is asked at a time: "keep this one" has to name something, and a store holding
three staged pages makes that answer ambiguous.

A preview older than `MAX_PREVIEW_AGE_MS` reads as nothing staged. Staging and applying
are two agent cycles with a person in between, so the window is minutes; past it a "yes"
would install the page they saw rather than the page they agreed to, because the fold
values behind it have moved.

## Apply: one step, because the person said one thing

`apply_preview` commits the staged page as a new instance version and returns that
instance. It writes to no catalog: the only page that can be staged is one the catalog
already serves, so there is nothing to save.

The page is re-checked against the manifest that will actually be stored. The staged
pair passed at staging, but staging and applying are two cycles with a person between
them, and the parity rule is about the pair being committed.

**One size ceiling, and it is the frame's.** `MAX_INSTANCE_HTML_BYTES` reads
`dashboard_frame.MAX_PAGE_BYTES` (3 MiB) rather than stating a number of its own. The
`MAX_RETAINED_VERSIONS` (20) versions one mate keeps therefore hold at most 60 MiB
of pages. A page passes
two size checks -- this store's before it becomes a version, and the frame's before the
document that renders it is assembled -- and two independent numbers meant the store
could refuse a page the frame would happily draw. It did: `project-report` is over
147,000 bytes and is `DEFAULT_TEMPLATE_ID`, so under a private 64 KiB ceiling no
mate could adopt it, and a root session that adopted anything else could never come
back to the page it starts on. The frame is where the cost is actually paid, which
makes it the honest place for the number; `test_dashboard_templates_builtin.py` asserts
the identity and takes every shipped template through preview and apply.

The recorded action is `adopted`, which keeps the history's vocabulary closed -- the
entry type declares `action` with `enum_closed=True`, so no fourth value ships to
describe a change the existing three already name.

The preview is discarded once the version lands, so "keep this one" cannot be answered
twice and quietly write two versions of one page.

**The person can answer from the tab too.** A mate's staged page is drawn under
**Keep** and **Discard** buttons in the host chrome (the Dashboard tab on `/members`
and the chat side panel's preview tab), outside the sandboxed frame, so a page can
never press them. Keep posts `POST /api/members/{slug}/dashboard/preview/apply` with
the `staged_ms` the preview read returned; the route calls `apply_preview(...,
expected_staged_ms=...)`, which refuses `PreviewChanged` (409 `preview_changed`)
inside the instance lock when a different page was staged after the one shown. That
is the button's version of `dashboard_apply`'s guarantee: what lands is what was
drawn. Nothing staged answers 404 `no_preview` and a locked layout 423
`dashboard_locked`. Discard posts `.../preview/discard` with the same `staged_ms`;
`discard_shown_preview` drops only that page, refuses `preview_changed` for one staged
since, and answers ok when nothing is staged any more. The route reads back and
answers 500 `dashboard_failed` if a page is still there. The `staged_ms` the host sends
is the one of the document the frame has MOUNTED, never a newer read still being
minted, so both buttons stay disabled until the frame shows the page they would act
on. Both carry the lock route's gates: an internal-secret caller is
refused `human_only` first, then the owner gate, then the CSRF middleware every
cookie POST passes. On a successful Keep the host leaves preview mode at once
(dropping `?dashboard=preview`) and refetches the live page, with one line saying
the page was kept and that the mate can roll it back. When the staged page goes
any other way (the agent's `dashboard_apply`, a discard, expiry), the preview view
hands over to the live page the same way, with a line that says so. Template
previews only: a session's page has no person route.

## The structure lock is the person's, and only theirs

The person's structure lock freezes WHICH page the tab shows. They set it with the
**Lock dashboard layout** switch on the mate's Profile tab (the settings
rows under Permissions, Model and Effort); the Dashboard tab carries only a small
**Layout locked — unlock on Profile** badge-button that opens the Profile tab when clicked, never a toggle. While it is set,
`_commit` refuses every new version with `InstanceLocked`, so adopt, apply, edit
and rollback all fail, and the agent surface answers `dashboard_locked` (423) with a
sentence telling the agent to ask the person. The check sits in `_commit`, under the
instance lock every writer already holds, so a lock set between a caller's read and
its write still refuses it. Staging a preview and writing agentic values are not
structure changes and keep working.

`set_structure_lock` is the only writer of the flag for a live mate, and
`POST /api/members/{slug}/dashboard/lock` is its only caller: owner-gated, and it
refuses an internal-secret caller (`human_only`) before anything else, because that
caller is an MCP server acting for an agent. No MCP tool sets the lock.

Deleting a mate (`DELETE /api/agents/{name}`) removes its lock file through
`structure_lock_removal.remove_structure_lock`, so a later mate deriving the same slug does not start
out locked to the old one's page. The slug comes from the config captured while the
record existed, and the removal is decided the way the crew-log reclaim is: under
`memory_store_namespace_lock` and the lock's own guard, and only while no crew in the
roster on disk still derives that slug. A slug a live mate still claims keeps its
lock. Only the lock file goes; its `.guard` file stays, because another writer may be
holding it. A mate that never locked creates nothing in `crew-panels` on delete.

The lock is a file, `crew-panels/<slug>.dashboard-lock`, and NOT a field of
`instance.json`: `members/<slug>/` is writable by the mate's own sandboxed shell,
so a flag there could be cleared by editing a file. `crew-panels` is masked in the
sandbox, fenced by the file-tool gate and refused when aliased, and the gateway is its
only reader and writer. Locking SNAPSHOTS the current record into that file, and
while it exists `read` serves the snapshot, so copying an old version over
`instance.json` cannot change the locked page. Only a missing file is unlocked; any
other read error reads as locked, and a lock file with no readable snapshot serves
the error state rather than `instance.json`. Unlocking removes the lock file and
writes nothing under `members/<slug>/`, which the mate's shell can redirect.
It is never part of a version, so a rollback cannot restore an old lock state, and
it holds for a mate that never adopted. A lock change is NOT a history row: the
history savepoint lives under `members/<slug>/`, and the lock file is the record.

The lock is keyed by the member slug, which is the mate's immutable `member_id`
wherever one is persisted, so a rename keeps it. It is no part of any DM session or
of `members/<slug>/`, so a cleared chat, a new DM, a wiped member dashboard folder
and a gateway restart all leave it set. The member record does not grow.
While locked, the mate's `[DASHBOARD]` turn block carries a LOCKED line telling
it not to apply, roll back or propose another page, while noting that previewing for
the person to look at still works.
## Every commit syncs the upkeep helpers

A fold field fills itself from the crew log; an agentic field is the mate's to
write. `dashboard_upkeep` supplies two things for the agentic fields, from the field's
`upkeep` key (`means`, `when`, `how`, optional `helper`): the TEXT, which
`dashboard_fields` puts on each agentic row of its reply when it is called
(`dashboard_upkeep.upkeep_rows`), so it cannot go stale and a mate with no file
tools reads it; and the HELPER SCRIPTS, copies of the shipped
`builtin/<id>/helpers/<file>` under `<skills dir>/.dashboard-upkeep/<slug>/scripts/`,
which fetch data the crew log does not hold (PR checks via `gh`) and print a ready
`dashboard_write` payload. The reply names each helper's absolute path. The dot
directory keeps the scripts out of the skills index, and it holds nothing else.

`_commit` calls `dashboard_upkeep.sync` with the record it just wrote, so adopt, apply,
rollback and edit each copy the helpers the new page names and drop one it does not.
`turn_block` calls it too -- a mate on the page named for it never commits, and a gateway
upgrade can change a shipped helper -- and, when the page has fields to write, adds the
one pointer line saying `dashboard_fields` carries the upkeep text, which rides the
member section through every compaction. `sync` writes only what differs, so it also
puts back a hand edit. A root session's own dashboard (`is_session_key`) has no
mate shell and gets no scripts and no helper path.

The text and the helpers are read from the SHIPPED catalog entry. The mate's copy
of the manifest is on the agent-writable side and `edit` may change it, so its `upkeep`
is never read; only a `builtin` template may name a helper. The profile's Notes tab
shows the same text (`GET /api/members/{slug}/briefing` `upkeep`). Decision:
`rfc-crewmate-dashboard-upkeep-skill.md`.


## A field that does not resolve is the mate's to fix, and only the mate is told

The Dashboard frame (`dashboard_frame.compose`) draws NO band about field values, in any
state: no "Some numbers are older" line, no `data-dashboard-stale` attribute. A cell
whose value did not resolve keeps the last value the page drew and is dimmed through
`data-dashboard-missing`; a cell that never resolved stays empty and dimmed. `missing`
wins over presence on every fill, because a patch carries the fields it did not move
forward from the read before it: a cell named in the patch's `missing` is dimmed even
though its old value is still in `window.kirocrew.fields`. The person
cannot fix a binding, so telling them about one only teaches them to ignore the page.
The read carries no `stale` key, and no shipped template draws a freshness or
staleness message of its own: no badge from a read flag, no quiet band from a fold
stamp, no live/"no new entry" indicator (`test_no_shipped_page_reads_stale` and
`test_no_shipped_page_draws_its_own_staleness_band` pin it). A task's own "gone quiet"
status on a row is the task's state, not the page's freshness, and stays; a read that cannot be
serialized is sent as an empty one, and the page draws its own empty state.

`DashboardFeed.read` names each FOLD field that did not resolve in `FieldRead.unresolved`,
with one of three reasons: `fold_unseen` (the fold has produced no value), `path_missing`
(the fold has a value with nothing at the field's path) and `older_value` (the fold has a
value without the field, which resolved at an earlier read on this slot, so a page that
drew it still shows the older number). A fold with no value on this read is always
`fold_unseen`, even for a field that resolved before: the cell may only be cold (a unit
rollover with no savepoint, an evicted cell, a prime still running), and the turn block
must not tell the mate to rebind a working field. `older_value` is decided from a process-wide record of
which fields resolved per slot (`dashboard_feed.remember_resolved`), because a tab is
refilled from a fresh feed per request. It holds at most 256 slots and 256 names per
slot, a read forgets every name the page in force no longer declares, and it is lost on
a restart, which turns an older value into one of the other two reasons for the same
binding. An agentic
field never written is absent, not unresolved: it is in `missing` only, and its upkeep
text already asks the mate to write it. A TREE-keyed fold has no bus scope, so the feed
reports its fields unresolved and each reader that reads the tree
(`LivePage._resolve_trees`, the turn block) takes them back out when they resolve.

The mate hears about it in its `[DASHBOARD]` turn block. `build_member_section` passes the
crew name to `turn_block`, which reads the page in force the way the tab does
(`member_dashboard.read_fields`) and adds one line (`dashboard.unresolved` in
`crewmate_prompts.json`) naming up to three fields with their reason and counting the
rest, then saying what to do: check the folds with `dashboard_types`, then
`dashboard_save` a page that binds a path that exists or drops the field, and
`dashboard_apply` it. Only `path_missing` and `older_value` are named. `fold_unseen` is
left out: a fold with no value yet is not a broken binding, and a fresh mate on its
shipped page is in that state; once the fold has a value, a wrong path shows as
`path_missing`. A `panel` field reads `fold_unseen` until the mate has published, since
the empty record has none of a published record's paths. The line is absent when nothing
fixable is left, so it leaves on the first turn after the fix. No read is made on a
locked page or with `agent.crew_panel` off, where the mate cannot call the tools the line
names. A V1 mate (no `member_id`) carries its member section only at session start,
resume or re-injection, so on its WARM turns `turn_message` adds the line alone through
`dashboard_agentic.unresolved_notice`, under the same gates and the desk gate; a V2 mate's
essentials rebuild the whole section every turn; a read that fails adds nothing; and the apply reply, which builds the block
without a crew name, makes no read.

## The preview link opens the Dashboard tab; the tab reads `?preview=1`

The link `dashboard_preview` hands back (`instance.preview_url`) is a PAGE:
`/members?member=<exact crew name>&dashboard=preview`. The Members page opens that
mate, opens the side panel on its Dashboard tab, and draws the staged page in the
same sandboxed frame as the live one, under the one preview label the chat's side
panel also uses (`DashboardPreviewLabel`: **Preview, not applied**, and that nothing
changes until the person answers in chat). The label drops once the staged page is
applied or expires, and the tab says the preview has closed. Switching
mate drops the query, so the preview never follows a person to another mate.
The tab shows the dynamic dashboard for a preview whether or not the Dynamic Dashboard
Feature Preview is on: the staged page exists only in that form, and the person was
sent to look at it.

The link carries the crew NAME, not just the slug, because slugification is lossy and
the page and the read below both refuse a slug alone (`missing_member`).

To fill the frame, the tab reads
`GET /api/members/{slug}/dashboard?member=<name>&preview=1`, which serves the staged
page through the same renderer, the same masking pass and the same owner gate as the
live one, with `preview: true` in the body and `instance_version` left at the current
version. Nothing staged answers 404 `no_preview`, which the tab says in words (the page
was kept, discarded or expired) with the same way back to the live page.

One route and not two. A staged page carries the same mate's fold values as the page
it would replace, so a route of its own would be a second place to get that owner gate
right; and a capability token would be a second credential for data its holder can
already read. Serving it through a different renderer would be worse than either: the
person would have been shown something other than what applying it gives them.
