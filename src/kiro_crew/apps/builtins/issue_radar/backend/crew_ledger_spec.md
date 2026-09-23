# Crew ledger + agent write path

Two kinds of state live here, and they are stored differently.

**Operator configuration** -- the crew record and the per-repo settings -- follows
the app's existing convention: `app_data_dir("issue-radar")`, per-repo namespace,
`atomic_write` under an exclusive `platform_compat.file_lock` for every
read-modify-write.

```
<data>/repos/<owner>/<repo>/crews/<crew_id>.json          # crew record
<data>/repos/<owner>/<repo>/crews/<crew_id>.unit-order    # the crew log units this crew
                                                          # recorded into, in that order
<data>/repos/<owner>/<repo>/crews/settings.json            # per-repo protocol constants
```

The record and the settings carry `schema: 1`. Issue Radar's existing versioning strategy --
"schema mismatch => treat as a cache miss and refetch from GitHub" -- does **not**
transfer here: a crew record has no upstream to refetch from, so a forward
migration is required from the first release.

**The ledger** -- work items, progress lines and the repository-shared skip index
-- is not a file of its own. It is a **projection of the crew log**: every
`issue_radar_crew_record` call appends exactly ONE `radar/recorded` entry to the
crew log of the session the crew runs on (`crew_log/entry_types.py` declares the
shape; `docs/reference/crew-log/session-types.md` documents it), and every read is
the `radar` fold of those entries (`crew_log/projection.py`, registered in the fold
registry as a SLOT-keyed fold). The store module (`crew_store.py`) keeps the same
function names its callers had; what they do is fold.

```
<data home>/crew-log/session/<unit>/                     # one unit per ACP session the
                                                          # crew's slot ran under
```

Why a projection and not a store:

- *One entry per update.* The item's delta, the event that explains it and -- when
  the phase is `skipped` -- the skip row ride on one appended line, so the two rules
  this spec has always had ("a phase never moves without a logged reason", "an issue
  is never skipped without being indexed") are properties of one append, not of
  three files under three locks and a rollback.
- *One record.* The crew log is already the session's record; a ledger beside it was
  a second document about the same work, kept in sync by hand.
- *No lock order.* The crew log writer serializes appends per unit; there is nothing
  left for two crews to race on.

**Units and the join.** A crew's slot (`slot_key = crew-<id>`) owns one ACP session
id at a time, and every unit it ran under is headed with that slot. A read folds
every unit of the slot in the order the crew RECORDED into them, with the LIVE unit
last when the caller is inside one. The order is the crew's own: each write appends
the unit it records into to `crews/<crew_id>.unit-order` (fsynced, once the entry is
IN the file and not before -- the order file's claim is "among the units that
recorded, this one holds the newest entry", which only a landed append earns; a unit
recording again after another is moved to the end, never listed
twice; the newest 64 kept, older ids and units nothing recorded into keep header
order and apply first). The file is never appended in place or staged under a
predictable name: it is rewritten whole through a uniquely named temp file
renamed over the name, under a PINNED parent directory where the platform
supports it, and read refusing a link at its name -- the data home is where a
sandboxed agent may be able to plant a link, and neither the write nor the read
follows one. The file's authority is bounded as well: only units whose HEADER the
store proves belong to this crew's slot are taken from it, so a rewritten file can
at most permute the crew's own retired units, and the live unit applies last
whatever it says. Header clocks alone would not do: a clock stepped backward
before a replacement unit was created sorts the replacement before its
predecessor and applies a retired session's phases over the current ones, and a
read has no caller inside a unit to pin the live one last. A crew whose order file
cannot be written falls back to header order -- what every read did before the file
existed. `crew_log_units()` lists the units; a crew whose slot has no crew log
reads as the empty record, and every failure to LIST units also reads as empty,
because this runs on the read path of a crew's every cycle. A failure to FOLD them
reads as empty too, for the same reason and to keep the contract the pre-projection
reader kept when its index would not parse: the repository's skip memory and its
pre-investigate briefing read ACROSS crews, so one crew's damaged log would
otherwise raise into the crew page of every crew in the repository. The write path
is the other half -- it lists and folds STRICTLY, because a write validated against
an empty record could admit a second editor.

**Prerequisite: the crew log is ON.** The crew log is switched on by
`KIROCREW_CREW_LOG=1` and is OFF by default (the append-only ledger RFC pins it off
until it graduates). A gateway with it off has NO crew ledger: every record call is
refused **409 `crew_log_unavailable`** with a message that names the variable, every
read folds the empty record, and the one-time carry (below) does not run -- the
pre-projection files are left untouched until the first successful write, so a
gateway that switches the log on later loses nothing. A crew running on such a
gateway learns this on its first record, not at creation: the refusal's message
names the variable to set.

**Writes need a live unit.** The write route resolves the crew's live unit from its
slot key -- never from the caller's session, so the dashboard writing on a crew's
behalf lands in the same unit the crew's own agent does. A crew whose slot has no
live session, or whose session has not run its first turn yet, or a gateway with
the crew log switched off, is refused with **409 `crew_log_unavailable`**: the
ledger DEPENDS on the log and keeps no document of its own. An update that cannot
fit one crew log entry is **413 `ledger_entry_too_large`**; it can never land,
however often it is retried. Every other refusal (unknown phase or kind, a second
item entering an editing phase, a crew-level kind with a number) is checked against
the FOLDED record before anything is appended -- an append-only log cannot take a
line back -- and stays a 409 `crew_conflict`.

**One write per crew at a time.** The refusals above are decided against the fold,
so two requests for one crew that folded the same record could both pass the
one-editor rule and both append. The store holds one lock per crew across a write's
fold, refusals, append and answer (the route runs writes on worker threads, so it is
a thread lock, held through the drain). A write whose append the writer had not
drained inside the flush budget answers `durable: false` and marks the crew; the
crew's next write drains again BEFORE it folds, so it does not validate against a
record missing that entry. A writer STILL not draining refuses that next write --
**503 `ledger_not_recorded`**, nothing changed, send it again -- because a fold
taken then would be missing the queued entry and the one-editor rule checked
against it could admit a second editor; the mark stays until a drain succeeds.
Reads take no lock.

**An explicit null is a clear.** An omitted field means "unchanged"; a field named
in the request's `clear` list means "empty it" (`clear: ["pr_number"]` after a pull
request is closed). The record tool has the same `clear` argument, an enum of the
clearable fields (`decision`, `why`, `next`, `worktree`, `branch`, `base_sha`,
`pr_number`, `claim_comment_id`, `ci_state`, `labels_applied`, `outcome`); a name
outside it is refused **400 `invalid_clear`** rather than ignored, so a typo cannot
pass for a clear. The route turns each name into an explicit null in the patch; a
typed entry field cannot hold a null, so the writer carries the cleared names in the
entry's own `clear` list and the fold empties each one before applying the fields
the same update sets -- a call that clears and sets one field keeps the set value.

**A refusal is not a commit.** The write drains the writer and reads its entry back
from the log. A writer that drained WITHOUT the entry showing in the log did not
record it -- the session's log deleted under the write, the writer gave up on it,
or the log unreadable on two tries (the read-back is retried once, so one transient
read failure does not turn a landed entry into a refusal) -- and the write answers
**503 `ledger_not_recorded`**: nothing changed and the same update may be sent
again. An append of THIS session's that the writer's buffer rejected at its memory
ceiling while this write was in flight, and that is not in the log, is answered
the same way whatever the drain says: that rejection is counted per session and
apart from a storage refusal, and is checked apart, so another session's rejection
cannot turn an accepted append into a false refusal. A write also lists the crew's
units STRICTLY: a listing that failed, and one that is INCOMPLETE because a unit
already holding entries cannot prove its header, are both a refusal (503), never a
shorter record -- a read that cannot list them, or can list only some, answers what
it could read, but a write validated against a record missing a unit could admit a
second editor. A unit directory whose header is not published yet is not that case:
it holds no entries, so leaving it out loses nothing. The synthetic answer -- the fold advanced over the entry as sent, with
`durable: false` -- is made ONLY while the writer still holds the entry past the
flush budget; a drained writer either shows the entry or is answered as a refusal.

**The answer is the line the log holds.** The write drains the writer (up to five
seconds, off the event loop) and reads the landed entry back, so the `event` a crew
is handed carries the id and `ts` every later reader sees. Only when the writer did
not drain in time is the answer the fold advanced over the pending entry with this
side's clock, and the response says so with `durable: false`.

**The skip index is the one read made ACROSS crews.** It is the union of every
crew's folded passes for the repository, retired crews included, and the FIRST
decision on a number stands -- the same first-decision-wins rule the index has
always had. Which decision is first is the writer's own observation, not a clock: a
pass recorded while another crew's decision on the number already stood carries
`deferred: true` in its entry (the writer reads the index before it appends) and
never stands over the decision it saw, so a clock stepped backward on the later
crew cannot put its pass in front of an established one. Only two passes neither of
which saw the other -- recorded inside the staleness bound below -- fall back to
`decided_at` then crew id, and between two concurrent decisions neither was
established. A crew that re-skips a number is told what stands (the first crew's
reason and id), not what it sent. *Staleness bound:* a pass another crew recorded is
visible once that crew's append has drained to its log. The write waits the writer's
flush budget (five seconds) for that drain; an append still queued past the budget
is answered as not durable and drains when the writer catches up, so there is no
upper bound on how late it becomes visible, only the writer's own progress. Each
crew's fold is re-checked against its log
on every read, so nothing is cached past a write. A pass whose unit crew-log
retention has collected is gone from the index; that is the bound every fold of the
crew log lives under. Retention collects a CLOSED session's log
`session.archive_retention_days` after it closes (30 by default; `null` disables the
sweep), so a pass outlives the session that recorded it by that window, not for
the life of the repository as the old file did. The carried pre-projection index
concentrates this: every historic pass is re-stated into the ONE unit of the crew
that wrote first after the upgrade, and all of them leave together when that unit
is collected. This is accepted: the index bounds re-investigation within the window
crews actually work in; it is not the repository's permanent record, which the
crews' public claim comments and the issues themselves remain. The coupling is
stated where an operator touches the knob: the setting's own help text
(`Archive Retention (days)`) says the window collects crew logs and, with the crew
log on, Issue Radar's shared skip memory and open work items -- so shortening it
for disk reasons is a choice made knowing what else it shortens.

**Reads are checkpointed, not cached.** Folding is O(the log), and a crew's log
carries its message bodies, so the fold per crew is kept in memory as a checkpoint
and ADVANCED over the entries that arrived since, through the same seq-anchored
machinery a cold fold uses. The checkpoint is held against every unit's MARK -- its
log file's creation identity together with its newest seq -- and four things force a
cold rebuild, each of which would otherwise be a wrong answer rather than a slow one:
a changed unit list, a unit whose identity changed (its log was removed and created
again under the same id, whether or not the new log's seq has climbed back past the
cached one) or cannot be read, a unit whose seq went backwards, and growth in any
unit but the newest.

**Every field the fold retains is bounded, and eviction is counted.** Per crew: 500
work items (past it a FINISHED item goes first, oldest finish first, then the open
item longest without progress, never the one just written; an evicted item takes
its phase history with it), 5000 passes (the earliest decided goes first), a
500-line event tail, 200 phase entries per item, 100 rejected approaches per item,
at most 20 labels per item, and a CI reading keeps its declared members only
(`state`, `passed`, `total`, `round`, `inherited_reds`), each re-bounded to the
record tool's own type and ceiling (a 32-character verdict; counters as ints within
the tool's ranges; a member of any other shape dropped) so a reading cannot grow an
item key by key or carry an oversized member into every retained item. Every
numeric field is bounded in MAGNITUDE the same way -- `number` and `pr_number` to
the tool's `1..1_000_000_000`, `claim_comment_id` to its `1..10^18` -- so a
thousand-digit number read off a file is dropped, as a number of the wrong type
already was, rather than kept as an item or skip key and re-served in every later
checkpoint and response. `counts`
reports `evicted_items` and `evicted_skips` beside `open`, so a bounded record is
told from a complete one. The bounds are far above any crew's working set; a crew
that has touched more distinct issues than this has a history, not a working set,
and the working set is what a resume needs. An evicted pass is one the repository
may investigate again, the same acceptance the retention bound below makes.

**Lifetime.** The entries follow the SESSION's life, not the repository's: crew log
retention and a session's deletion remove them with the unit, and disconnecting or
purging a repository removes only the operator files above. A retired crew's units
keep folding (its passes still count in the skip index) until retention collects
them, which is the bound stated for the index. The same bound holds for OPEN work
items, and is accepted by name: an update carries only the fields it set, so a field
recorded in an earlier unit lives only there, and an item still open when retention
collects that unit (30 days after the session it belonged to closed) loses the
fields nothing re-stated since -- its claim stamp, a `decision` or `why` written
early, older rejected approaches. An item open across a session boundary for longer
than the retention window is the exception this trades away; the crew's public claim
comment holds the claim's own record, and the item's `phase`, `next` and `pr_number`
are re-set by the ordinary writes such an item keeps receiving.

**Carrying the pre-projection files forward.** The files the ledger used to be
(`crews/<crew_id>/<n>.json`, `crews/events.jsonl`, `crews/skipped.json`) are read
ONCE more, on a crew's first write after the upgrade -- the one before any radar
entry has folded into its record --
whichever kind of write that is: an idle sweep is a routine first write, and a
sweep that appended without carrying would leave the crew read as owing nothing
while its open items sat unread. A crew whose every write was an idle sweep holds no
items and no passes for good, so the trigger is what the record has FOLDED and not
what it holds: keying on emptiness would carry the files again on every such write
whenever they are present without their marker, re-stating retired work as live over
a record that has moved on. The files are
re-stated into its log as `carried` entries: each work item with its own stamps
(so a carried claim does not look freshly made) and its newest rejected approach
(the entry holds one, and the carry's event line says how many it left behind); and
-- by whichever crew of the repository writes first -- every row of the shared skip
index, with the crew and time that decided it, recorded as a pass and NOT as a work
item of the carrying crew. Two markers beside the files record the carry: one when
it BEGINS (`crews/<crew_id>/.carrying`, `crews/skipped.json.carrying`) and one when
every carried entry has been read back from the log (`crews/<crew_id>/.carried`,
`crews/skipped.json.carried`). The begun marker is written BEFORE any row is emitted, and
a begun marker that cannot be written refuses the write outright: without it a
partial carry would leave a non-empty fold that never carries the rest. A drained
writer is not proof of landing -- it can
refuse an entry and drain quietly -- so the finished marker waits for the read-back,
and a carry that began without finishing is run again on the crew's next write. The
write that triggered such a carry is itself REFUSED (503 `ledger_not_recorded`,
nothing changed, send it again): the record it would fold is short of the rows still
in the writer or refused by it -- a carried editing item among them -- and the
one-editor rule checked against that record could admit a second editor. A carry
still in the writer also marks the crew undrained, so its next write drains before
it folds. A
carried item re-stated with the same fields folds into the same record, and its
rejected approach is not listed twice. A work item is one entry PER rejected approach
it carries, each re-stating the whole record and differing only in that approach, so
the file's whole `tried` history arrives in the file's own order and a refusal of one
entry still leaves the others folding with the item's real phase. A row that does not
fit one entry is SHRUNK
until it does -- its free text clamped in steps (5000, 2000, 1000, 500, 256
characters; the fold clamps to 5000 on every read, so the first step costs nothing
the fold would have kept, and a later step is taken only because the row is long
non-ASCII text that serializes past the entry ceiling) and its `ci_state` and
labels trimmed to what the fold keeps -- and a row that could not be carried is NOT
carried, does NOT let the carry finish, and REFUSES the write that triggered it
(503, retryable): unreadable, not a record, without a number the file name can
recover inside the tool's range, or too large for one entry at every clamp. The
finished marker would discard that row's stored state for good, since the files are
never read again once marked; and folding without the row is worse than refusing,
because an omitted row holding an editing phase leaves its item out of the record the
one-editor rule reads, so another item can enter that phase while the row still holds
it. The files stay unmarked, the rows are named in the log and in the refusal, and the
next write tries again once they are repaired or moved aside. A re-run emits only the
rows the record LACKS: a carried entry
re-states the file's fields as an update, so re-emitting a row that already landed
would set an item the crew has since worked back to its pre-projection state. A file
that appears later beside a crew whose record has folded a radar entry is left alone.
The files are never
written again and, once marked finished, never read again. The old progress log
(`events.jsonl`) is
not carried: its lines are history the crew page can live without, and the crew's
public claim comments already hold them.

**The read half.** A carry can only run from a write -- it appends, it needs the
crew's write lock, and it refuses its caller when a row does not land -- but the reads
that happen FIRST are the ones a crew acts on: the pre-investigate nudge snapshot
(`list_work_items`, `open_slot_count`) and the repository's shared skip index
(`read_skips`). Answering the empty record there would tell an upgraded crew, on its
first turn, that it has no open items and that no issue has been passed on, and it acts
on that with forge comments that cannot be taken back. So a read PREVIEWS the pending
carry: it folds the very payloads the carry would append, over the same base, through
the same projection, and appends nothing and marks nothing. The answer is the
post-carry record by construction rather than by two implementations agreeing, and the
preview stops the moment the log holds the rows. It is reached on the write path's own
carry trigger, so the two agree on WHEN a carry is owed as well as on what it carries;
a fold that could not be READ is never previewed, because a reader older than the
writer cannot conclude that the upgrade has not happened. Recording being switched off
therefore costs writes, not reads: a crew whose crew log is off still reads its
pre-projection state.

## Crew record

| Field | Type | Notes |
|---|---|---|
| `schema` | int | 1 |
| `id` | str | `c_<8 hex>`, minted at creation under one data-home-wide lock and checked against every repository's crews (under the legacy root and every provider subtree) and the crew log's slot listing before it is taken, because the id names the crew's slot and a slot is a data-home-wide name. Stable forever. Everything machine-readable keys on this, never on `name` |
| `name` | str | galaxy name, unique per repo including retired crews |
| `avatar_seed` | str | separate from `name` so a rename keeps the face |
| `avatar_variant` | int \| null | 0–7 pins one ghost outfit; null = derive from `avatar_seed` |
| `agent` | str | `kirocrew-crew` by default |
| `model` | str | `""` = governed default |
| `extra_prompt` | str | appended after the brief, never replacing it |
| `labels` | [str] | its scope. Empty = every label |
| `auto_resolve_conflicts` | bool | default true, structural files only |
| `auto_merge` | bool | default true |
| `unattended` | bool | default true → per-slot trust, re-established each cycle |
| `max_open` | int | default 3 |
| `worktree_root` | str | one worktree per issue lives under here |
| `slot_key` | str | `crew-<id>`. ASCII, no colon — already normalization-safe |
| `enabled` / `paused_reason` | bool / str | a self-pause records why |
| `created_at` / `retired_at` | ISO8601 Z | retiring keeps the record so the name stays taken |

## Work item

One record per (crew, issue), folded from the `radar/recorded` entries that named
it. Merged per field on write -- an entry carrying only `phase` leaves everything
else as the fold had it (an omitted field means "unchanged"), same semantics as the
existing `write_investigation`.

| Field | Type | Notes |
|---|---|---|
| `schema` | int | 1 |
| `crew_id` / `owner` / `repo` / `number` | | identity |
| `phase` | enum | see below |
| `outcome` | enum \| null | set only in a terminal phase |
| `decision` / `why` | str | what this crew decided to do and on what grounds |
| `next` | str | **the resumable intent.** "add the Windows branch to `_safe_chmod`, the test already fails" — not "implementing" |
| `tried` | [{`approach`, `rejected_because`}] | append-only, so a resumed turn does not re-walk a dead end; the newest 100 rows are kept per item (a repeated pair folds to one row), so a crew that keeps rejecting cannot grow the fold without bound |
| `worktree` / `branch` / `base_sha` | str | local only, never echoed into a comment |
| `pr_number` | int \| null | |
| `ci_state` | {`state`, `passed`, `total`, `round`, `inherited_reds`} | `inherited_reds` is what keeps a crew from rebasing at main's breakage |
| `claim_comment_id` | int \| null | which comment to PATCH. Rediscoverable from the marker if lost |
| `labels_applied` | [str] | so a hand-back knows exactly what to remove |
| `claimed_at` / `last_progress_at` / `finished_at` | ISO8601 Z | `last_progress_at` moves only on real progress |

### Phase enum

```
selected        local only, pre-claim — never public
claimed
investigating
implementing            ← the only editing phase
awaiting-ci
addressing-review
awaiting-merge
resolved                terminal
```

Side states: `awaiting-reply`, `skipped`, `yielded`, `handed-back`, `preempted`.

**No phase means "waiting for a human".** A crew that needs a human decision or a
human investigation does not hold the issue: it says what it needs in a comment,
applies the repo's `needs_human_label`, records `skipped` with the scope
`needs-decision` or `needs-investigation`, and releases its claim. The work then
waits where the person is already looking — their own issue tracker — instead of
inside one crew's slot, and no crew idles against a reply that may never come.

Two independent classifications hang off this enum, and they do not coincide:

- **TTL-active** — `claimed`, `investigating`, `implementing`. Only these age
  toward the claim TTL. Everything else is parked legitimately and is exempt: an
  open pull request is stronger evidence of a live claim than any heartbeat.
- **Editing** — `implementing`, plus `addressing-review` while the worktree has
  uncommitted changes. At most one per crew, enforced by the store: a second item
  entering an editing phase is refused, not warned about.

Every non-terminal phase counts toward `max_open`. There is no exemption, because
there is no phase in which the crew is not the actor.

`preempted` has one meaning and only one: another crew proved this claim dead and
took the issue over. It is terminal for the item — see
[Dead-claim takeover](#dead-claim-takeover).

## Claim marker — the public wire format

One HTML comment at the end of the claim comment carries the machine payload, and
it is the only part of a claim another installation parses:

```
<!-- kirocrew-crew v=1 id=<crew-id> phase=<phase> pr=<n> updated=<ISO8601 Z> -->
```

`v` is the version of this **format**, not of the app, and it comes first so a
reader can decide whether to interpret the rest before it tries. Everything the
marker expresses — the phase vocabulary, which phases age toward the TTL, the
smallest-comment-id tie-break, the `crew:` label names — is read by crews
belonging to *other people*, running a build of this app the local operator does
not control and cannot upgrade. An unversioned wire format is a one-way door: no
later change to any of that can be made without breaking those readers, and there
is no channel through which to warn them. So the field ships from the first
release even though only one value is defined.

`id` is the crew id and never the name, because a crew can be renamed and must
still recognise its own claim. `updated` is written by the crew into the body and
is never read from GitHub's own `updated_at`, because that field moves on *any*
edit — a human fixing a typo in a crew's comment would otherwise silently renew a
dead claim.

### Compatibility rule

A reader that meets a marker whose `v` it does not recognise **treats the claim as
valid and live**: it skips the issue, does not claim it, does not edit the comment,
and never takes it over. A marker with no `v` at all reads as `v=1`, the first
published format.

The two ways to be wrong are not symmetric, and that asymmetry is what makes this
the safe default rather than a preference:

- Read an unknown marker as "not a claim" and two crews work one issue at the same
  time: two branches, two pull requests, two conversations on a stranger's issue,
  and a maintainer reviewing the same fix twice. No later protocol step can undo
  it, because the duplicated work already exists.
- Read it as a live claim and the cost is one candidate issue out of an unbounded
  backlog. A crew that skips an issue has still had a successful turn, and there is
  always another issue.

This is the same asymmetry the label index already rests on — label present means
skip without verifying, label absent still means read the comments — applied to the
case where the comment is readable but not interpretable.

Takeover is excluded for an unknown version specifically because the takeover rules
are the ones most likely to move: which phases are TTL-active, what the TTL is
measured from, and what evidence exempts a waiting phase. A reader that cannot
interpret the version cannot know whether that claim is expired, so it must not act
as though it does.

Within a version a writer MAY add a key, and a reader MUST ignore keys it does not
recognise — `_parse_crew_marker` reads named keys only, so an older crew meeting a
newer marker drops the extra field instead of failing. A new `v` is required for
anything that changes the meaning of an existing key, the phase vocabulary, the TTL
basis, the tie-break, or the label names. Version is a property of the marker and
not of the installation: two markers on one issue may carry different versions, and
each is judged on its own.

### Which marker is the live claim

An issue can legitimately carry several markers, because a claim comment is edited
rather than deleted and the record is worth keeping: a crew that yielded a collision,
one that passed the issue back for a human to answer, one that was taken over. **A
marker in a terminal phase is history and is never a claim** — `resolved`, `skipped`,
`yielded`, `handed-back` and `preempted`. Only a non-terminal marker can hold the
issue, and the smallest-comment-id tie-break ranks only those.

`find_crew_claim` returns every marker it finds, oldest comment id first, and that is
correct — a caller must be able to *see* the history. But the winner is not simply
`[0]`: a preempted or yielded comment is older than the live claim that replaced it,
so a caller that takes the first entry without filtering on phase picks a dead claim
over the live one, and does it deterministically rather than intermittently.

## Dead-claim takeover

The label index is trusted without verification, and a crew never edits another
crew's claim comment. Those two rules together leave nobody able to clear a claim
whose crew is gone: the `crew: in progress` label and the comment both persist,
every other crew skips the issue on the label alone, and the TTL expires against no
one. That is the one direction in which divergence between installations fails
badly rather than merely wastefully, so the TTL needs an actor.

The actor is the next crew that would otherwise have skipped the issue. There is no
sweeper and there cannot be one: crews on other people's machines are the other
participants in this protocol, and no central process can be assumed to exist for
them.

### When a claim is dead

All of the following, together. The first two are arithmetic and the third is
evidence; the arithmetic alone is not enough, because it depends on a TTL the other
side never agreed to.

1. **Its phase is one the crew is expected to be acting in** — `claimed`,
   `investigating`, `implementing` — **or it is a waiting phase whose reason for
   waiting is gone**: `awaiting-ci`, `addressing-review` or `awaiting-merge` naming
   no `pr`, or naming one that was closed without the issue being resolved. A
   waiting phase is exempt from the TTL because an open pull request stands in for
   a heartbeat; when the pull request is not there, nothing does, and the claim
   ages as though it were active.
2. **Its own `updated` is older than the reader's `claim_ttl_hours`.** A missing or
   malformed timestamp fails this test as well: a claim that cannot demonstrate it
   is alive must not be read as alive, which is why the timestamp grammar is
   validated strictly rather than parsed leniently.
3. **The issue has had no activity of any kind since that timestamp** — no comment,
   no cross-referenced commit or pull request, no label change. Work a crew did but
   did not write down still proves the crew is alive, and the timestamp cannot see
   it. This is the condition that protects a crew that was merely slow, and it is
   the whole test when `updated` is absent or unparseable.
4. **Its `v` is a version the reader understands**, and **the claim is not the
   reader's own**. A crew reaching its own expired claim is resuming from the
   ledger, not taking over.

`awaiting-reply` never expires, and neither does a claim whose pull request is still
open. Both are waiting on a human — a reply, a review — and a takeover there would
restart work whose next step was never a crew's to take.

Nothing else waits on a human. An issue whose next step is a human decision or a
human investigation is not a claim at all: it carries the repo's
`needs_human_label` and a `skipped` marker, so no crew holds it and no TTL applies
to it. That is the outcome that actually helps the person who has to answer —
findable in their tracker, with the crew's reasoning already on the issue.

### The carved exception to "never edit another crew's comment"

The successor performs a compare-and-set and then exactly two writes.

**Re-read first.** Immediately before writing, re-read the claim comment and
confirm `updated` still holds the value that was judged. If it moved, the crew is
alive: nothing is touched and the successor picks a different issue. This is the
same post-then-immediately-re-read discipline the collision tie-break already uses,
for the same reason — the window between deciding and writing is exactly where a
live crew can appear.

Then:

1. **Remove the stale `crew:` label.** A label write, and already inside the crew's
   allowed label set.
2. **Append one takeover note to the dead crew's comment and set that marker's
   `phase` to `preempted`.** Append only: not one word of the existing prose or
   progress list is rewritten or deleted, so a human can still read what that crew
   did and audit the takeover against it. `phase` is the single field the successor
   may change, and changing it is what makes the issue unambiguous afterwards —
   exactly one marker on the issue reads as a live claim, so a third crew arriving
   later needs no tie-break to work out which.

```
Claim taken over by **<Successor>** · Kiro Crew Issue Radar
<Original>'s claim was last updated <ISO8601 Z> and the issue has had no
activity since — past this installation's claim TTL.
```

The takeover clears a stale claim; it does not grant one. The successor then claims
normally — its own comment, its own marker, the ordinary tie-break, and
`crew: in progress` back on under its own name — so a second crew that arrives
between the takeover and the claim is resolved by the mechanism that already exists.

**A crew that finds `phase=preempted` on its own claim comment accepts it**: it
records `preempted` on the work item, releases the worktree, and does not re-claim.
Contesting it would produce precisely the two-crews-one-issue outcome the protocol
exists to prevent, and the successor's evidence — no activity anywhere on the issue
for longer than the TTL — is a fact about the issue rather than an opinion about the
crew, so a returning crew has nothing to dispute it with.

## Event log

The progress lines are the `event` / `event_kind` carried on each `radar/recorded`
entry, folded into a bounded newest-first tail per crew (500 lines; the per-item
history of phase ENTRIES is kept separately and is what the pipeline view draws
lanes from, so a busy tail never buries when a lane entered its phase). Each line
keeps the content-addressed id the file-backed log gave it, byte-identical in
formula, so a reader keyed on ids sees the same id for the same line. A duplicated
entry folds to one line and applies its update once: the repeat check keys on the
WHOLE update (crew, number, kind, text and every field) and not on the line id,
because the id carries the timestamp and a retry -- an append re-sent after a crash
or after a 503 -- is stamped when it is retried. Only the item's LAST applied
update is compared, never a window of history: a retry is by construction the
next update for its item (the crew's writes are serialized and the crew waits on
the answer), while an item that returns to an earlier state with identical fields
after other updates is recording a new transition, and it applies.

```json
{"id":"<sha256(ts|crew|number|kind|text)[:16]>","ts":"2026-08-08T20:44:12Z",
 "crew_id":"c_7f3a","number":2251,"kind":"ci","text":"CI round 3 — 41/47 green, 6 inherited from main"}
```

`ts` is the entry's own time as the crew log stamped it, so a line can never claim a
time the log disagrees with.

`number` is OMITTED on a **crew-level** line — a step that belongs to no issue,
which today is only `kind: "sweep"` (the crew checked the queue and took
nothing). The key is absent rather than `null` or `0`: a reader tells "this line
is about no issue" from its absence, and a `0` would be indistinguishable from a
real issue number in every filter and join that keys on it. In the id, an absent
number renders as the empty string, so the formula for a numbered line is
unchanged and the two families cannot collide.

The pairing is enforced in both directions, in the write route and in the store:
a numberless line must carry a crew-level kind, and a crew-level kind must not
carry a number. Neither shape can drift into the other's meaning.

**Consecutive sweeps coalesce.** "Checked, took nothing" is a recurring
latest-value fact, not an event, and a crew is nudged on a timer — so one line per
idle cycle would be an unbounded run. Reads here are capped and discard the OLDEST
line first, so a crew idling a couple of hundred cycles would push its real work
history out of its own work log. The FIRST sweep after real work is written; a
sweep whose crew already has one as its newest line is answered with that existing
line, and the response says `coalesced`. The surviving timestamp therefore marks
when the idle stretch BEGAN, which is the more useful reading. This is the same
record-the-transition discipline `phase` already follows — stamped only when an
item is created or actually moves. The tail check happens against the crew's
folded record BEFORE the append, and the fold applies the same rule to the bytes,
so two crew turns waking together cannot leave two trailing sweeps: the second one
folds away even if both appended.

**What the surviving timestamp does not mean.** It records when the crew last
REPORTED an empty queue, and nothing about the present. Consecutive reports fold,
and a crew that stops — an operator pause, a crash, a lost nudge timer — stops
reporting without saying so, because nothing on the crew record evidences liveness.
So the ledger cannot tell a crew that is still checking from one that quietly died
mid-stretch, and the crew page renders the line as the past instant it is rather
than as a claim about now. An earlier revision qualified it as "checking since …",
which read as present-tense activity and so masked exactly the failure an operator
opens that page to notice. Closing that properly needs a real last-seen datum, and
that is a separate change.

One log feeds **two** surfaces: the work-log table on the crew page, and the
`<details>` progress list inside the public claim comment. A crew-level line
reaches only the first — it has no issue, so there is no claim comment to render
it into.

That dual use imposes the stricter constraint on both: **`text` becomes public**,
so it must never contain an absolute path, a host name, or anything from the
user's environment. Worktree paths live in the work-item fields (local only) and
must not appear in an event. Redact on the way in with
`platform.redact_via_context`, exactly as `issue_radar_record_investigation`
already does — that tool redacts because the prose is re-rendered on a card; here
it is re-rendered on github.com.

## Per-repo settings

Protocol constants shared by every crew in the repo. They cannot be per-crew:
two crews negotiating with different values is how a short-TTL crew steals a
long-TTL crew's live work.

| Field | Default |
|---|---|
| `claim_ttl_hours` | 48 |
| `needs_human_label` | `crew: needs human` |
| `commit_trailer` | `Crew: {name} (Kiro Crew Issue Radar)` |

Editable from the app's settings.

`needs_human_label` is the label a crew applies when it passes an issue back for a
human decision or a human investigation, and it is one of only **two** labels a crew
ever writes — this one and `crew: in progress`. It is configurable because label
vocabularies belong to the repository: a project that already triages with
`needs: maintainer` should not be made to grow a second word for the same thing. Both
free-text settings are trimmed, capped at `crew_store.MAX_SETTING_TEXT`, and fall
back to the default when blank — validated on **read** as well as on write, because
`settings.json` is an ordinary file in the data home and a hand-edit must not decide
what a crew writes to someone's issue tracker.

**These values are per-installation and cannot be relied on to match across
operators.** They are local settings on one person's machine, and nothing in the
comment protocol communicates them — a crew belonging to someone else may run a
shorter `claim_ttl_hours` and consider a claim expired while its owner still
considers it live, or a longer one and refuse to clear a claim that has genuinely
died. The same applies to `needs_human_label`: what *this* operator calls the
condition says nothing about what another one calls it, so never infer anything about
another crew's state from a label you did not write.

That divergence is the reason a takeover requires positive evidence of absence —
no activity anywhere on the issue since the claimed timestamp, re-checked
immediately before the write — and not the timestamp arithmetic alone. The
arithmetic is the one part of the test that turns on a number the other side never
agreed to, so it decides when to *look*, while the evidence decides whether to
*act*. `commit_trailer` is local presentation for the same reason: never infer
anything about another crew from the trailer on its commits.

## Nudge composition

The brief is not carried by an agent spec — it is injected into the conversation
by the backend, so it works with whatever agent the user picked. Two parts go out
each turn:

**The volatile snapshot** (~120 tokens): crew name, repo, crew id, label scope,
limits and current counts, every open work item with its phase and `next`, and the
`crew:` labels it may write. Everything here changes turn to turn, so it is cheap
and correct to resend.

**A compressed Never block** (~80 tokens): the hard prohibitions, restated
verbatim from the brief's Never list. This exists because the injected brief is a
**user** message, not a system prompt, and therefore carries less authority than
the same words would in an agent spec. Keeping the prohibitions adjacent to the
instruction costs about one credit a day and is the cheapest way to buy that
authority back.

### Brief injection — presence check, not a heuristic

The brief itself is injected only when it is **absent from the conversation**, not
on a schedule and not by inferring that a compaction happened. The backend scans
`slot.messages` for the sentinel

```
<!-- kirocrew-crew-brief v1 -->
```

and requires the message carrying it to be at least as long as the brief — a
compaction summary that merely quotes the sentinel is shorter and does not count
as a hit. On a miss, inject.

One rule covers session start, post-compaction, gateway restart, and any future
truncation mechanism, with no detection logic to get wrong. Measured on this
machine's own usage shards, the marginal cost of the brief is 0.154 credits per 1k
of context on `claude-opus-5`; at ~6.4k the brief costs about 1 credit each
time it is injected, and a presence check fires it a handful of times a day rather
than on all ~80 turns.

## Agent write path — two tools, two allowlisted routes

The gate must stay a **full-path** allowlist entry, never the
`/api/apps/issue-radar` prefix. That distinction is deliberate in
`dashboard/server.py`: prefix-matching there would also admit the app's GitHub
write routes (label, close, comment) to anything holding the internal secret.

### `issue_radar_crew_read`

No required args beyond the crew's own identity, which the handler resolves from
the session. Returns the crew record, its per-repo settings, and every
non-terminal work item with the fields above.

The nudge already carries a snapshot, so this exists for the two cases the
snapshot cannot cover: a turn that runs long enough for the snapshot to go stale,
and a resume after compaction or restart where the crew has to re-establish what
it was doing.

### `issue_radar_crew_record`

One write tool that patches work-item state **and** records the event explaining
it -- as ONE `radar/recorded` entry appended to the crew's crew log -- rather than
two tools. Merging them means a phase can never change without a logged reason, a
pass can never be recorded without its index row, and a progress step costs one
call instead of two.

Flat args, following `issue_radar_record_investigation`'s shape (it flattens its
five findings fields the same way). Empty fields are dropped, so a partial patch
preserves what an earlier write stored.

```
number                    optional int — omit ONLY with `event_kind: sweep`
phase                     optional enum
outcome                   optional enum
next                      optional str
decision, why             optional str
tried_approach,
tried_rejected_because    optional pair — appends one `tried` entry
worktree, branch, base_sha  optional str
pr_number                 optional int
ci_state, ci_passed,
ci_total, ci_round,
ci_inherited_reds         optional
claim_comment_id          optional int
labels_applied            optional [str]
clear                     optional [enum] — work-item fields to EMPTY by name (the
                          clearable list above); the only way to erase a field, since
                          empty and omitted fields are dropped
skip_scope                optional enum — why a pass was recorded, including
                          `needs-decision` / `needs-investigation` when the next
                          step belongs to a human
event                     optional str — the public progress line
event_kind                optional enum (claim|investigate|reply|implement|ci|review|conflict|merge|handback|skip|yield|sweep)
```

Nothing is unconditionally required. `number` was, which left a crew that swept
an empty queue no way to record the cycle without inventing an issue number. The
coupling that replaced the requirement is a relation between two fields, which a
per-field schema cannot express, so it lives on the write route and in the store:
a missing `number` is valid ONLY with `sweep`, `sweep` is invalid WITH one, and a
numberless call takes none of the work-item fields (they patch an item this call
does not create, so they are refused rather than dropped). A present-but-invalid
number stays a 400 and is never reinterpreted as "no issue".

Validation lives in `validation.py` alongside the existing schemas. The handler
sends `owner`/`repo` explicitly so a same-numbered issue in another repo cannot
overwrite this record, and refuses a second item entering an editing phase.

Two refusals belong to the storage rather than to the update. **409
`crew_log_unavailable`**: the crew's slot has no live session, the session has not
run its first turn (its crew log is created then), or the gateway runs with the
crew log off -- there is nowhere to record, and the tool says so instead of keeping
a document of its own. **413 `ledger_entry_too_large`**: the update does not fit
one crew log entry (64 KiB), so it can never land; record fewer or shorter fields.
Both are named so an agent can tell them from a refusal of the update itself and
stop retrying the same body. The response also carries `durable`: whether the
append had reached the log when the tool answered (see *The answer is the line the
log holds* above).
