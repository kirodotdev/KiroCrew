# Crewmate dashboard instances

A template is shared. An **instance** is one crewmate's copy of one template, and the
copy is the whole design. A built-in template shipping a new version does not change
what a crewmate's Dashboard tab shows, and a crewmate editing its page is editing its
own copy rather than everybody's template.

Four modules and one route group, each with one job:

| Piece | Where | What it owns |
|---|---|---|
| registry | `dashboard_templates/catalog.py` | built-ins + user templates, one loader |
| instance | `dashboard_templates/instance.py` | the copy, its versions, rollback, state, history |
| share | `dashboard_templates/share.py` | export and import of one file |
| snapshot | `dashboard_templates/snapshot.py` | a frozen dashboard |
| routes | `dashboard/handlers/member_dashboard.py` | what the frame and a chooser read |

The template FORMAT is `dashboard_templates/manifest.py` and nothing here restates it:
every path above -- a built-in, a user template, an imported file, an edited copy, a
restored version -- goes through `load_template` or the `parse_manifest` +
`check_parity` pair it is built from. One loader means one place a malformed template is
refused, and it is why an agent-written page cannot become a dashboard that renders an
unowned empty cell.

## Three versions never mix

Each is spelled out and none is called "the" version, because conflating any two
produces a page a reader cannot place:

**Template version** is the version of the template that was copied. Frozen at adopt.
An edit never moves it: editing a copy does not make the crewmate the template's author,
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

`empty` and `error` are deliberately separate, and that distinction is the reason the
read does not 404. A crewmate that never adopted a template answers 200 with `empty`,
because having no dashboard yet is the ordinary first state of every crewmate and the
frame's empty state IS that answer. `error` is a record that was damaged or hand-edited
after it landed. The two need opposite answers from a human -- one is a fresh crewmate,
the other is a dashboard that stopped working -- so a 404, or one word for both, would
hide the second behind the first.

`stale` covers two cases with different sentences: the template moved ahead of the copy,
and the template left the registry. Nothing is missing from the copy in either case --
it still renders -- which is exactly what the frame's stale band says.

Each state carries a `state_reason`: one sentence a surface can show a person. The four
words are what a client BRANCHES on; a client cannot branch on prose and a person cannot
act on `stale`.

## The history is a fold, the record is a file

`instance.json` is the current value. Every accepted change ALSO appends one
`dashboard/instance_changed` entry to the crewmate's own DM session crew log, and the
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
`crew_log/entry_types.py` with the store importing it. One tuple, two readers: a fourth
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
crewmate's dashboard was copied from -- and because the instance holds its own copy, the
dashboard would keep rendering while the registry no longer held what it was made from,
which is the one failure nothing downstream could detect.

An imported template declares `shared`. It lives in the user directory and is editable
like any user template, but a reader asking where it came from gets an answer other than
"somebody here wrote it". The registry enforces that this is a legal claim for that
directory, so nothing has to trust the field.

## Registry rules

**A broken template is reported, not raised.** `list_templates` returns the entries it
could load AND the problems it could not. A registry that raises on the worst member of
a set stops working the first time somebody hand-edits a file, and a directory omitted
rather than reported makes a template somebody just got wrong indistinguishable from one
that was never written.

**The directory name is the id.** A directory holding a manifest naming a different id
is two names for one template, and a lookup would find or miss it depending on which
name the caller happened to hold.

**`source` is checked against where the file actually is.** A user directory may declare
`user` or `shared`; the built-in directory may declare only `builtin`. Without that
check a user template could claim to be a built-in, and a surface trusting `source` to
mean "this shipped in the product" would be reading an agent-written field.

**A built-in id wins.** A user template colliding with a built-in is not loaded and the
collision is reported. A built-in is what the product's own documentation refers to, and
a template quietly replacing one makes those docs wrong with nothing to read.

## Snapshots: all five parts or nothing

A live dashboard answers "what is true now", which is the wrong tool for "what was true
when this went wrong". A snapshot is the second question, and it is the field values plus
everything needed to know what they meant: template id, template version, instance
version, values, seq.

Each is required. A bag of numbers with no template is a page nobody can redraw; a page
with no seq cannot be placed against the log it came from; values with no instance
version cannot be told from values the crewmate's next edit would have laid out
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
`state_reason`.

`?member=` is REQUIRED on every route, exactly as the briefing and rules reads require
it. Slugification is lossy, so two crew names can reach one slug; an instance is one
directory per slug, so for a colliding slug the instance belongs to neither crewmate, and
serving it to both -- with an editor -- would let them overwrite each other's dashboard.
The exact name must derive this slug, exist in config, and be the only name that does.

The READ is open to any dashboard caller and every WRITE is owner-gated, the same
boundary `api_member_panel` draws: the frame is reachable by a non-owner dashboard
subject, so gating the read would turn their Dashboard tab into a 403, while a write
mutates a stored page the gateway later renders. App tokens are denied outright -- an app
token scoped to `/api/members` reaches these by prefix.

All five rules live in one `_resolve` chokepoint, so a route added later cannot forget
one of them, and a test reads the handlers' own syntax trees to assert every one calls it
and every write calls the owner gate.
