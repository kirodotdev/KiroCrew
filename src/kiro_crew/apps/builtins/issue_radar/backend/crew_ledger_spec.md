# Crew ledger + agent write path

Storage root follows the app's existing convention — `app_data_dir("issue-radar")`,
per-repo namespace, `atomic_write` under an exclusive `platform_compat.file_lock`
for every read-modify-write.

```
<data>/repos/<owner>/<repo>/crews/<crew_id>.json          # crew record
<data>/repos/<owner>/<repo>/crews/<crew_id>/<number>.json  # one work item
<data>/repos/<owner>/<repo>/crews/events.jsonl             # append-only event log
<data>/repos/<owner>/<repo>/crews/settings.json            # per-repo protocol constants
```

Every file carries `schema: 1`. Issue Radar's existing versioning strategy —
"schema mismatch ⇒ treat as a cache miss and refetch from GitHub" — does **not**
transfer here: a crew record has no upstream to refetch from, so a forward
migration is required from the first release.

## Crew record

| Field | Type | Notes |
|---|---|---|
| `schema` | int | 1 |
| `id` | str | `c_<8 hex>`. Stable forever. Everything machine-readable keys on this, never on `name` |
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

One file per (crew, issue). Merged per field on write — a patch carrying only
`phase` preserves everything else, same semantics as the existing
`write_investigation`.

| Field | Type | Notes |
|---|---|---|
| `schema` | int | 1 |
| `crew_id` / `owner` / `repo` / `number` | | identity |
| `phase` | enum | see below |
| `outcome` | enum \| null | set only in a terminal phase |
| `decision` / `why` | str | what this crew decided to do and on what grounds |
| `next` | str | **the resumable intent.** "add the Windows branch to `_safe_chmod`, the test already fails" — not "implementing" |
| `tried` | [{`approach`, `rejected_because`}] | append-only, so a resumed turn does not re-walk a dead end |
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

Append-only JSONL, content-addressed id so duplicate lines merge on read rather
than conflict (the ledger pattern from ops-mission-control, which shipped without
a lock and was caught in review — take its `_LedgerLock` too).

```json
{"id":"<sha256(ts|crew|number|kind|text)[:16]>","ts":"2026-08-08T20:44:12Z",
 "crew_id":"c_7f3a","number":2251,"kind":"ci","text":"CI round 3 — 41/47 green, 6 inherited from main"}
```

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
item is created or actually moves. The tail check and the append happen under ONE
hold of the events lock, or two crew turns waking together would both see no
trailing sweep and both append.

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

One write tool that upserts work-item state **and** appends one event, rather than
two tools. Merging them means a phase can never change without a logged reason,
and a progress step costs one call instead of two.

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
