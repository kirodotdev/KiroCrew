# Ops Mission Control

An autonomous ops first responder shipped as a **builtin app** (`origin: builtin`,
`defaultEnabled: false`). It polls signal providers, claims what is firing,
investigates it in a chat session mirrored to Slack, matches it against a
compounding knowledge ledger, and proposes an action. Read-only by default.

## Durable contracts

These are persisted-data or security contracts. Changing one requires updating
this spec in the same commit.

### 1. Signal fingerprint (persisted in the ledger)

`compute_fingerprint(source, resource, title)` = `sha256(source|normalized-shape)`
truncated to 16 hex chars. The shape substitutes out timestamps, uuids, long hex
runs, `i-`/`vol-`-style resource suffixes, and **all bare numbers**
(`models._VOLATILE_PATTERNS`).

This is load-bearing: a fingerprint that drifts per occurrence means a repeat
failure never matches its ledger ancestor, and the app keeps working while
silently no longer learning. Changing the normalization invalidates every stored
`LedgerEntry.fingerprints` entry.

**It also provably over-merges, and that is why it cannot be the only key.** Because
every bare number is stripped, distinct failures on one resource collide — verified and
pinned in `TestShapeHashOverMerges`:

| title A | title B | shared fingerprint |
|---|---|---|
| `4xx error rate above 5` | `5xx error rate above 1` | `413280c6ee0b5afa` |
| `p99 latency above 500ms` | `p50 latency above 100ms` | `c4dbf4e759b19ceb` |

The stripping is deliberate (a DLQ at 500 and at 900 *should* match), so the fix is not
to sharpen the hash — a fingerprint match means "looks like this", and a ledger that
presents it as "is this" hands a responder a fix learned from a different problem, which
is worse than no match. See contract 1a.

### 1a. Provider identity (persisted, additive)

`Signal.provider_key` carries the identity the **provider** computed — an Alertmanager
`fingerprint`, a Datadog monitor id, a CloudWatch `region/alarm-name`, a `repo#number`.
It is namespaced `"<source>:<key>"` so two providers cannot collide on a bare numeric id,
and it is set from **explicit adapter input, never derived**: a derived value would be
another heuristic wearing the word "exact".

`LedgerEntry.provider_keys` stores them, unions on merge exactly as `fingerprints` does
(preserving the git reconciliation property in contract 2), and is **bounded** by
`MAX_KEYS_PER_ENTRY` keeping the newest. The bound is required, not tidiness: PagerDuty
mints a new incident id per occurrence, so an unbounded list would grow one JSONL line
forever in a file that is git-synced and read into a model prompt.

`ledger.match(fingerprint, provider_key=...)` ranks exact hits **above** shape hits
regardless of trust or use count, and `dispatch` reports which kind matched in the brief
(`ClaimedIncident.exact_match_ids`) so the agent can weigh them differently.

Both fields default empty, so every incident and ledger line written before they existed
stays valid and keeps matching by shape alone.

### 1b. Provider-side suppression (persisted, additive)

`VALID_STATES` gains **`STATE_SUPPRESSED`** — *a human already parked this at the
provider* (an Alertmanager silence or inhibition, a Zabbix maintenance window, an Icinga
downtime, a Sentry archive). `normalize_state` maps
`suppressed`/`silenced`/`inhibited`/`muted`/`snoozed`/`downtime`/`in downtime` onto it.

**Why it is a state of its own.** If those words read as `unknown`, `suppressed` and `banana`
would be indistinguishable, and an adapter facing `status.state = "suppressed"` would have two
options, both wrong: report `firing`, and
the app investigates something an operator explicitly parked (the fastest way to lose
trust in an autonomous responder); or drop it, and "the app ignored my alarm" becomes
indistinguishable from "someone silenced it".

**A state, not a label**, argued against the two filters that consume state:

- `dispatch.run_cycle` claims `state == firing` in ONE place, so a new state is unclaimable
  by construction — no second predicate for a future edit to forget. A label would leave
  `state == firing` and force `run_cycle` to grow a label-reading condition, which is the
  *every adapter reimplements the filter privately* failure moved into core.
- `/signals` splits buckets BY STATE, and the reconcile SOP is written against those
  buckets. A label cannot produce a bucket, so a parked signal would keep arriving inside
  `firing` and reconcile would keep treating it as live work.
- `unknown` means "we could not read the state"; `suppressed` means "we read it and a human
  parked it". Collapsing the two is the defect, not the fix.

**Two decisions this does NOT reopen.** `acknowledged` still maps to `firing` — an
acknowledged page is unresolved and the point is to be working it (`pagerduty.py`
`_OPEN_STATUSES`). And no-data stays settled: CloudWatch `INSUFFICIENT_DATA` is an opt-in,
default-off detection-*sensitivity* choice with the truth kept in `labels['state']`.
Suppression is not a sensitivity knob — it is a fact about a person's action, and there is
no honest default-off reading of "someone silenced this".

**Distinct from `ACTION_SILENCE`.** That is a bounded suppression the app *issues*; this is
one somebody else applied, which we *read*. One word for both would merge our intent with
another party's decision.

`Signal.suppressed_by` (the provider's own attribution — an Alertmanager `silencedBy` id,
the alert in `inhibitedBy`) and `Signal.suppressed_reason` (`silenced` vs `inhibited`, since
a person's silence and an alert masking another alert need different next moves) are
**explicit adapter input, never derived**, and both **default empty** so every incident
already on disk stays valid. Unlike `provider_key` they are NOT namespaced by source: they
are display text for a human, not a match key.

The webhook adapter reads both Alertmanager status shapes — the v4 scalar `status`, and the
v2 `gettableAlert` OBJECT `{"state": "suppressed", "silencedBy": [...]}`. The previous
scalar-only read stringified that object, so it normalized to `unknown` and dropped
`silencedBy` entirely.

`normalize_state` therefore covers the **whole** v2 `alertStatus.state` enum —
`unprocessed` and `active` map to `firing`, `suppressed` to `suppressed`. Admitting only the
parked value would have been worse than not reading the object at all: the v2 payload would
parse a *silenced* alert correctly while dropping a *live* one into `unknown`, i.e. the app
going quiet on real work in exchange for reading a mute. The flat native envelope accepts `suppressed_by`/`suppressed_reason`
too, because Zabbix (`suppressed=1`) and Icinga (`downtime_depth`) do not speak
Alertmanager's shape and a forwarder normalizing them needs somewhere to put the
attribution.

`CycleResult.suppressed` counts what a cycle saw and left alone. It is deliberately excluded
from `changed` (a suppression is not news, and silence-by-default is a hard requirement) but
present in the payload, because `polled` counts firing signals only — without the count a
cycle reports a smaller world than it saw.

### 2. Ledger entry id (content-addressed, persisted)

`LedgerEntry.compute_id(pattern, fix)` = `sha256(lower(pattern)|lower(fix))[:16]`.

Content addressing is what makes the append-only `ledger.jsonl` mergeable across
git-synced team members: two people who learn the same lesson produce the same id.
Changing the basis orphans existing entries.

**Corrected against a real `git merge`:** the earlier claim that a merge is "a dedupe
rather than a conflict" was wrong at the git level. Two divergent ledgers conflict —
both branches appended to the same region, so git emits `<<<<<<< HEAD` / `=======` /
`>>>>>>>`. What content addressing actually buys is that the *entries* are reconcilable,
not that git resolves them for you. Two things make it work:

- The malformed-line skip in `read_entries` tolerates conflict markers, so the app stays
  usable while a user's tree is mid-merge (verified against a genuine conflicted file).
- `read_entries` **reconciles duplicate ids on read**, using the same algebra as
  `upsert` — **both** identity lists union and both are capped. Unioning `fingerprints`
  alone would let a real `git merge` permanently write away one branch's `provider_keys`;
  since `match()` treats a provider key as the EXACT-identity signal, the next recurrence on
  the dropped alert would match by shape hash alone or not at all — the same silent knowledge
  loss the fingerprint union exists to prevent. The cap applies on this path too (two
  already-capped lists unioned are up to 2× the cap, which `upsert` bounds). A merge test that goes through `upsert` cannot catch this, because `upsert`
  bounds the list itself. Pinned by
  `TestLedgerGitMerge::{test_reconcile_unions_provider_keys_not_only_fingerprints,
  test_reconcile_caps_both_identity_lists}`, which write two RAW lines — the git-merge
  shape — rather than calling `upsert`. Confidence and trust take the strongest of the two
  and `use_count` the highest, exactly as `upsert` does.

  Without reconciliation a read would append every line, so one shared lesson would count
  twice: `stats()` inflated, `match()` returning the same entry twice, and the handover digest
  listing one pattern as two. Identity union is the load-bearing part —
  dropping one branch's fingerprint or provider key means that recurrence stops matching,
  and the ledger keeps working while silently no longer recognizing half its own history.

End to end: two divergent ledgers → real `git merge` → conflicted file → 4 raw entries read as
**3**, the shared lesson collapsed with both fingerprints preserved.

### 2a. Record format version (`LedgerEntry.v`, `LEDGER_RECORD_V1 = 1`)

`ledger.jsonl` is the one artifact that **leaves the machine**: `ledger_sync` git-pushes it
and teammates on *different Kiro Crew builds* pull it, so an older instance can be handed a
row a newer one wrote. Without a version stamp there is no way to notice — the reader
coerces the fields it recognises and defaults the ones it does not, so a row it only partly
understands reads as fully understood. This is the nearest thing in the app to a
**one-way door**: the retrofit is only free while exactly one version exists.

- Every new line is stamped `"v": 1`. A line **without** `v` predates the field and reads as
  v1 — the standard retrofit for an optional field in append-only JSONL, and the reason
  adding this costs nothing now.
- An unparseable `v` also reads as v1 rather than raising. This reader's job is to salvage a
  git-merged team ledger, not to reject it, and a row whose version cannot be parsed is
  still a row every field of which is understood.
- A **future** version survives the round trip unchanged, so a later reader can act on it.
- Nothing gates on the value today — with one version a gate would be dead code. The field
  exists so the next format change has somewhere to say so.
- **Bump it only for a change a reader must know about** (a field whose *meaning* changed,
  or one that cannot be safely defaulted), never for an added optional field: every field on
  this record already defaults, which is what lets one version cover the whole history.

The field default is spelled as the literal `1`, not as `LEDGER_RECORD_V1`. The constant
remains the single source of truth for readers, and `test_the_field_default_matches_the_constant`
pins the two equal.

`test_ledger_sync_git` simulates two instances in one process without re-importing anything:
each instance gets its own `KIROCREW_HOME` and empty `ledger_sync` refusal latches through a
private `MonkeyPatch`, and asserts its ledger really resolves under its own data home.

### 2b. The fast-path bar, and the track record behind it (persisted, additive)

`ledger.is_fast_path` is what decides whether the investigation brief says **"KNOWN
PATTERN — propose this fix"** or **"hypotheses to test"**. It delegates per entry to
`ledger.entry_unlocks_fast_path`, which requires FOUR things:

| condition | constant | why |
|---|---|---|
| `trust == verified` | `FAST_PATH_TRUST` | a human saw it work |
| `confidence == high` | `FAST_PATH_CONFIDENCE` | and was sure |
| `use_count >= 2` | `MIN_USES_FOR_FAST_PATH` | some incident OTHER than this one used it |
| `miss_count == 0` | `MAX_MISSES_FOR_FAST_PATH` | it has never been observed to fail |

**Why the first two alone were not enough.** `POST /ledger` takes `confidence` and `trust`
verbatim, so one hand-authored entry could arrive as `verified`/`high` and immediately
unlock "propose this fix directly" for a production failure, having never been applied to
anything. Contract 1a made that strictly worse rather than better: `record_use` **binds**
the provider key on the first match, so from the second occurrence onward that same single
piece of evidence presents as an *exact* match — a stronger-looking claim with nothing new
behind it.

**Why 1 would be vacuous.** `dispatch.attach_ledger_matches` calls `record_use` *before*
`is_fast_path`, so at the moment of judgement `use_count` already counts the incident being
judged. Every match whatsoever has `use_count >= 1`. 2 is the smallest floor that says
anything, and it lands on the same line `handover.MIN_USES_TO_RECUR` already draws.

**The accepted cost:** the fast path unlocks on the third occurrence, not the second.
A non-fast-path match is not withheld — the brief carries the full pattern and fix either
way; the only difference is that the agent is told to confirm before proposing.

**The mechanical downward path.** Two new persisted, default-zero fields plus a bookkeeping
one:

- `LedgerEntry.miss_count` / `.last_miss` — times the fix was cited and the failure came
  back. Written by `ledger.record_miss`, whose **only** caller is
  `dispatch._record_verification_misses` (see contract 3b), so the standard of evidence
  lives in one place.
- `LedgerEntry.decayed_at_miss_count` — the `miss_count` value hygiene last spent on a
  demotion, so one failure costs exactly one confidence step. Required because hygiene runs
  nightly and its demotion test is a *ratio*, which stays true once true: without it a
  single miss would walk an entry `high → medium → low` across three nights on no new
  evidence.

All three take the **max** on every merge path (`_reconcile` on read, `upsert`, and
hygiene's dedupe). That asymmetry against `confidence`'s strongest-wins is deliberate: a
git pull, or a re-POST of the same pattern+fix, must not be able to launder away a
teammate's evidence that the fix failed. The re-POST case is the sharp one — that is
exactly how `ledger-hygiene.md` promotes `observed → verified`, so an accepted
`miss_count: 0` there would clear every recorded failure with one curl, on precisely the
entries most likely to have them. `POST /ledger` therefore does not read the three fields
from a body at all.

`hygiene()` demotes one confidence step when `miss_count >= max(1, use_count *
MISS_RATIO_FOR_DECAY)` and reports `demoted` **separately from `decayed`**: "nobody needed
this" and "this did not work" are opposite findings, and one number for both would let a
staleness report and a correctness report arrive as the same sentence. `trust` is never
rewritten — "somebody saw this work" stays true even after it failed elsewhere.

The prune order changed from `-use_count` to `-(use_count - miss_count)`. Before, an entry
that kept matching the *wrong* failure climbed the ranking on every mismatch and was
therefore the **last** thing the cap dropped: the ledger preferentially kept its most
misleading rows.

`stats()` gains `proven` / `demoted` / `total_misses`. `verified` and `high_confidence` are
each one HALF of the bar, so neither answers "how much of this ledger would an agent
propose without checking" — showing only those two overstated the ledger's authority.

### 2c. The sync loop, and where it is driven from

The daily `ledger-hygiene` pass (`POST /ledger/hygiene`) is the only caller of the git
transport and the vector index. Order is load-bearing: **pull → hygiene → index → push**.
Deduping before the merge leaves freshly-arrived duplicates for tomorrow; indexing before
hygiene embeds rows hygiene is about to prune; pushing before hygiene makes every instance
re-derive the same dedupe locally so the repo never converges. Pinned by
`test_stage_order_is_pull_hygiene_index_push`.

When `memory.persistence_enabled` is false, the pass preserves that stage order
but the index stage is a no-op decided before `VectorMemoryStore` construction.
Pull, ledger hygiene, incident pruning, and push still run: they mutate the
app's own durable state, while only the projection into shared episodic memory
is an automatic persistent-memory write.

Before this, **both halves were wired to nothing.** `ledger_sync` had no caller anywhere,
and `dispatch`'s semantic recall queried an index `import_pending` never populated — so on
a real install recall returned zero hits forever while every unit test passed. Two modules
can be individually correct and collectively dead; only an integration caller proves
otherwise.

The index stage builds its store with the install's `memory.episodic_max_count` and
`memory.episodic_dedup_threshold`, and `import_pending` cursors an entry only when the store
wrote it or reported it as a duplicate or unacceptable. A V1 store at its cap refuses
merge-only writes; `import_pending` reads that verdict from `write_episodic_outcome()`
(`AT_CAPACITY`, decided inside the transaction that refused the row, so a concurrent cap
raise or eviction cannot turn it into a duplicate), stops, and leaves that entry and the
rest off the cursor. A later pass indexes them once `memory.episodic_max_count` is raised or
an unrelated delete (an operator delete, stale retirement) drops the active count below the
cap; cap eviction by other writers always leaves the count AT the cap, so it never frees a
slot for the importer. The pause is logged as a warning naming how many entries are left
pending, because the result counts alone match a caught-up run. Pinned by `TestCapacityRefusal` in `test_ledger_index.py`.

Dispatch prepares the other half of semantic retrieval: after claiming an incident it
binds the process-wide cached embed callable to its short-lived store, attempts one
interactive query embedding with a five-second queue/admission budget, and passes that
vector explicitly into `ledger_index.search_similar`. Native inference already running
cannot be interrupted and may finish after that budget. `VectorMemoryStore.search_episodic` does
not embed `query_text`; without that boundary the advertised semantic leads are only FTS5
keyword matches. A cold, unavailable, stale-space, busy, or failing embedder yields no
query vector and dispatch immediately retains the same tag-scoped keyword search. The
model loads in the background; dispatch never calls `wait_ready`, and semantic recall
remains optional to claiming and fingerprint matches.

**Five rules only a real two-instance roundtrip against a bare remote exercises** — mocked
git passes without any of them (`test/test_omc_ledger_sync_coverage.py`):

1. **The first push in a fresh process retries a transient spawn fault.** The sandbox backend
   probe defers off the event loop on a cold cache and raises a self-described *transient*
   error saying "retry", so `sync_safely` retries **once** on a transient spawn fault,
   re-running only an idempotent git step.
2. **Local work is committed before merging.** `git merge` refuses when an untracked
   working-tree file would be overwritten, so an install that recorded even one lesson before
   its first pull would otherwise be *permanently* cut off from the team's. Staging and
   committing local work first is also the correct semantic.
3. **Unrelated histories merge.** Every instance runs its own `git init`, so their roots are
   genuinely unrelated and git refuses outright — the *ordinary* multi-instance case. `--allow-unrelated-histories` is therefore required,
   and is safe here **only** because the tracked content is a content-addressed union and
   the conflict path reconciles rather than picking a side. On a normal source repo the
   flag would be reckless.
4. **`rotation.yaml` is committed too.** `TRACKED_FILES` names the whole shared set; a
   `push` staging `ledger.jsonl` alone would leave the on-call schedule — un-ignored
   *specifically* so it could sync — reaching nobody.

5. **The local repo sits on the configured branch.** A `git init` with no `-b` lets git pick its
   own default, and if `branch()` were used **only** inside refspecs (`fetch origin <b>`,
   `merge origin/<b>`, `push HEAD:<b>`, `rev-list origin/<b>..HEAD`) sync would work *by
   accident of those explicit refspecs* while nothing moved HEAD or wrote tracking config — the
   app's signature "machinery that looks deliberate" shape, invisible to a test that only asks
   whether the content arrived. The costs are not cosmetic: (a) `status()` would claim
   "Syncing … on branch b" while HEAD sat elsewhere; (b) **manual recovery would be blocked** —
   with no upstream, `git pull` fails with "no tracking information for the current branch" and
   `git push` with "has no upstream branch", and a conflicted `rotation.yaml` is *refused* by
   push precisely so a human fixes it by hand, in that directory; (c) changing
   `ledger_sync_branch` later would re-point fetch/merge/push at a new remote ref while HEAD
   kept accumulating on the old one, so the first push to the new branch is either rejected
   non-fast-forward or publishes the old branch's history onto it; (d) `git status` and any
   agent reading the branch name would report a branch nobody configured.

   **Rename in place, then write tracking explicitly.** `_align_branch` runs
   from `_ensure_repo`, so one call site covers pull, push, and the operator changing the
   branch later. `git branch -m --` is the primitive because (verified against real git) it
   succeeds on an *unborn* branch, keeps the same sha on a born one, leaves a dirty tree
   untouched, and preserves an in-progress conflicted merge — none of which `checkout` /
   `switch` do. It **refuses** on a detached HEAD (moving refs under one can lose the
   operator's work) and when a *different* branch of that name already exists (`git branch
   -M` would delete it and every commit only it holds — the exact lesson-stranding this
   prevents). Tracking is written with `git config branch.<n>.remote/.merge`, **after** the
   rename and **not** with `--set-upstream-to`: the rename migrates `.remote` but leaves
   `.merge` on the old ref, and `--set-upstream-to` fails in both ordinary first-sync states
   (no `origin/<b>` fetched yet; unborn local branch) — an empty remote is how a team
   *starts*. A refusal is never a sync failure: publishing always worked through the
   refspecs, so the reason is surfaced through `status()` instead of failing `_ensure_repo`.

   `status()` therefore carries `local_branch` (what `.git/HEAD` points at, `""` when
   detached or uninitialized), `branch_matches` (the only field a UI should gate a warning
   on; **true** when uninitialized, since there is nothing yet to disagree with) and
   `detached`. `branch` keeps meaning the *configured* branch. The detail sentence only
   claims "Syncing … on branch b" when that is true of the local repo too. Rendered in
   Settings as a `wrong local branch` / `detached HEAD` badge plus a row naming the branch
   the repo actually sits on — shown only when they disagree, because two rows that usually
   agree invite exactly this conflation.

A clean tree is not proof everything is shared. A run that committed and then failed to reach
the remote would otherwise leave `push` reporting "nothing to push" forever, stranding that
lesson locally; `_has_unpushed` distinguishes the two and treats an unknown answer as
"push anyway" (a redundant push is cheap, a skipped one loses knowledge).

End to end: A records → pushes → B pulls and sees it → B adds → A pulls both. In the concurrent
case — both write without seeing each other — the stale push is **rejected**, the pull reconciles to 3 entries preserving both sides, and both instances
converge on identical ledgers with no entry lost.

#### Where an operator sets it, and what they are told

Config keys (all non-secret, so plain `data/config.json`, same tier as the Slack channel):
`ledger_sync_enabled`, `ledger_sync_remote`, `ledger_sync_branch`. Written through
`PUT /settings`; read back through `ledger_sync.status()` on `GET /state` (`ledger_sync`).

A remote URL is **not** a credential — auth is the operator's own SSH key, credential
helper or `gh` login — which is what makes it eligible for that file at all. The converse
constrains the UI: `config.json` is served **unauthenticated**, the write path only
length-caps the remote, and `redact_tokens` has no pattern for a PAT inside a URL. So the
Settings card asks for an SSH remote, states plainly that there is no credential to enter,
and strips a `userinfo@` component before *displaying* a remote — which is about not being
a second place a token is shown, **not** a claim that the stored value was sanitised.

`status()` reports `conflict` (the ledger) and `schedule_conflict` (`rotation.yaml`)
separately, because they are opposite severities:

- A **ledger** conflict is reconcilable and sync keeps publishing (content-addressed ids,
  `read_entries` skips markers, the next push rewrites the union).
- A **schedule** conflict makes `push` **refuse outright**, so nothing new reaches the team
  at all. `sync_safely` swallows that refusal into a warning, so if only the log and a SEL
  audit line carried it, `status()` would keep saying "Syncing …" through an indefinite
  publishing outage, with the on-call file unparseable for everyone who pulled it. `status()`
  names the refusal, and Settings renders it as an error rather than a note.

`_ledger_sync_status`'s failure fallback carries the **same key set** as a real status, so
the UI can read every field instead of guarding each one, and `undefined` is never a
rendered remote.

### 3. Incident status grammar (persisted in the dispatch index)

`models.LEGAL_TRANSITIONS` is the whole grammar; `store.transition` is the only
door and raises `ValueError` on an illegal move.

`TERMINAL_STATUSES` is **derived** from that grammar — a status with no outgoing
transition is terminal by definition — so a future status cannot disagree with a
hand-maintained second list.

**A closed incident does not own its signal.** `signal.id` is stable for the alarm's
lifetime (`cloudwatch:alarm/DlqDepth` forever), so if `claim` treated *any* existing incident
as "accounted for", including a closed one, the app would **permanently stop responding to any
failure it had already handled once**. That would also make the app's central premise
unreachable: the compounding-memory fast path can only pay off on a *second* occurrence, and a
second occurrence could never be claimed. The grammar says the same ("Re-opening is a new
signal, not a transition — a resolved incident that 'comes back' is a fresh firing with its own
timeline").

A recurrence is a **new** incident, never a reopening: the first one owns its
diagnosis, resolution, and Slack thread, and overwriting those would destroy the record
that makes the ledger trustworthy. An OPEN incident — including `needs_human`, which is
*waiting on a person*, not closed — still blocks a duplicate claim; that dedupe is
what stops two heartbeats double-investigating one alarm, and a subtest covers all
three open statuses.

**The same rule lives in TWO places.** `run_cycle` keeps a cheap pre-filter in front of
`claim` — `owned = {signal.id for non-stale incident}` — and it excludes `TERMINAL_STATUSES`
too; otherwise it would discard the recurrence *before* `claim` ever saw it, and unit tests
that call `store.claim` directly would never notice.
`test_a_resolved_alarm_refiring_is_claimed_through_run_cycle` exercises the path the cron
actually takes: cycle 1 → INV-1 `fast_path=False`; cycle 2 (same alarm) → INV-2, `matches=1`,
`fast_path=True`, remembered fix carried, INV-1 still `resolved`.

The rule generalizes: **a duplicated invariant needs a test at the outermost caller**, because a unit test aimed at the inner function proves nothing about the filter
in front of it.

End to end: 1st occurrence → 0 matches, `fast_path=False`, brief says "new
to the ledger". 2nd occurrence → claimed as a fresh incident, 1 match,
`fast_path=True`, brief carries the verified fix; the first incident stays `resolved`.

**A recurrence is a new incident, so retention bounds the index.** A genuinely flapping alarm
on the 2-minute cadence mints one incident per flap, and every claim re-reads and re-writes
the **whole** index, at a cost that grows superlinearly with its size. A month of one flapping
alarm projects to **~21,600** incidents, and an unbounded `/incidents` would serialize every one
of them on each dashboard poll.

Two bounds, neither on the hot path:

- `store.prune_closed(keep=MAX_CLOSED_INCIDENTS)` (500) retires the oldest **closed**
  incidents, ordered by when they closed (`updated_at`) so a long-running incident that
  just finished counts as recent. **Open work is never pruned at any age** — live work
  vanishing because history is long would be far worse than a large index. It runs from
  the daily hygiene pass, not from `claim`, so an ordinary claim never pays for a large
  rewrite. Investigation *logs* are separate files and deliberately untouched: retiring
  an index row does not destroy the written record.
- `/incidents` is capped at `MAX_INCIDENTS_RESPONSE` (200) and sets `truncated` +
  `total` when it clipped. Silent truncation is how someone concludes an incident
  vanished; the frontend types both fields.

```
unclaimed → dispatched → investigating → {needs_human, resolved, escalated}
dispatched|investigating|needs_human → stale   (idle past that status's window)
dispatched → resolved                   (signal cleared before the first turn)
dispatched → needs_human
stale → dispatched                      (re-claim, same incident id)
stale → resolved                        (signal cleared while released)
needs_human → investigating|resolved|escalated
resolved, escalated                     (terminal — no exits)
```

`unclaimed → resolved` is deliberately absent: resolving requires a claim first, so
every resolution has an incident timeline behind it.

**`dispatched → resolved` and `stale → resolved` exist for reconcile.** A signal can
stop firing between the claim and the agent's first turn (a flapping alarm; a GitHub
issue closed a minute later), and the reconcile SOP's entire job is to close
incidents whose signal cleared. Without these edges it has no legal move for that
case: the incident sticks at `dispatched` until the stale sweep hours later, so the
board asserts work is in progress on a problem that no longer exists — and from
`stale` the only move would be re-dispatching a dead signal, spending a whole
investigation to conclude nothing is wrong. So "a resolved incident asserts an investigation happened" is narrower than it
sounds: a claimed incident may resolve
without one when the underlying signal simply went away, which the SOP requires be
stated in the `resolution` text rather than implying a fix. Both edges are pinned by
`test_models.py::TestTransitionGrammar`.

**`needs_human → stale` is traversed by the sweep.** The edge exists because "an incident
nobody ever answers must not pin a signal as claimed forever": `needs_human` is in
`store._SWEEPABLE_STATUSES`, since `run_cycle`'s pre-filter counts every non-stale non-terminal
incident as owning its signal, and an unanswered question would otherwise mean the alarm is
never re-claimed. A test that asserts only that the transition is *legal* does not run the
sweep — the same "test the outermost caller" rule as above.

It gets its **own, longer window** (`needs_human_stale_after_secs`, defaulting to
`DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER` × the working one, so 12 h at the 2 h default).
Waiting on a person is legitimately slower than an agent dying, and releasing a question
discards the investigation's context — but an *abandoned* question must not hold the
signal forever.

#### Where an operator sees these, and in what unit

`max_claims_per_cycle`, `stale_after_secs` and `needs_human_stale_after_secs` are written
through `PUT /settings` and read back through `rotation.sweep_windows()` on
`GET /rotation` (`sweep`). Without that read path an operator could set a window and never see
it again, and the defaults governing every untouched install would be reachable only by reading
the source — the same looks-deliberate-does-nothing shape this module exists to prevent, in the
settings layer.

Two rules the response encodes, both load-bearing for the UI:

- **`needs_human_stale_after_secs` is reported RESOLVED, never as the stored `0`.** Unset
  does not mean "never released" — `store.sweep_stale` derives it from the working
  threshold — so returning the raw value would state the opposite of what the sweep does.
- **`needs_human_derived` says whose number it is.** A derived window moves when
  `stale_after_secs` changes; a pinned one does not, and an operator choosing between them
  needs to know which they currently have.

`sweep` is optional on the client type: an older gateway omits it, and the Settings card
then reports that the values were not sent rather than substituting the defaults, which
would confidently display 2 h against an instance possibly running something else.
`rotation` duplicates the three config-key strings (importing them from `dispatch` would
close an import cycle); `test_store_and_gate.py` asserts them equal to `dispatch`'s own, so
a rename cannot leave the panel displaying a default while the heartbeat uses a real value.

### 3a. Suppression is always time-boxed (safety)

`ACTION_SILENCE` exists because every low-risk provider write in the landscape is a
time-boxed suppression (Alertmanager silence with a mandatory `endsAt`, Datadog mute with
an `end`, PagerDuty snooze with a required `duration`, Sentry archive with an
`ignoreDuration`), and the vocabulary had no word for it — so an adapter had to express a
mute as `resolve`, asserting something false and hiding a live fault *permanently* rather
than temporarily.

The contract: **an action in `EXPIRING_ACTIONS` always carries a positive, bounded
expiry**, clamped by `resolve_silence_secs` into `(0, MAX_SILENCE_SECS]` at the
authorization boundary in the `/incident/action` handler (`_handle_action`) — not in each adapter. A sink must not
be able to opt out of the bound by forgetting to check, because an unbounded suppression
is the single outcome the verb exists to prevent. Unparseable or non-positive input
yields the DEFAULT, never "no expiry".

Why this matters beyond tidiness: **a wrong silence expires by itself.** That is what
makes granting `act` a bounded bet rather than an all-or-nothing one, which is what
"autonomy is earned per rule" requires in practice.

`datadog.py` always sends an `end` with `/mute`: Datadog reads a missing `end` as *mute
forever*, so the board would show an incident resolved while the metric stayed bad,
recoverable only by a human noticing. `resolve` is retained as an
alias onto the same bounded mute, because silently dropping it would revoke a capability
an existing act-rule already grants. `github-issues` deliberately does **not** advertise
`silence`: an issue tracker has no snooze, and claiming one would be a lie.

### 3b. An executed action is re-read, and a failed poll is not a success (persisted, additive)

`ActionResult.ok` means **"the provider returned 2xx"** and nothing more. That is not the
claim the board was making. Checkmk dispatches commands asynchronously through Livestatus
and its own docs warn a 2xx "only indicates whether the request was successfully
transmitted, NOT whether it was in fact successfully executed"; Nagios's command pipe
returns nothing at all. So the app could report a suppression or a resolve as applied while
the alarm kept firing, with no code anywhere in a position to notice — the silent lie an
ops agent must not tell, and the reason `use_count` meant "was shown" rather than "worked".

**Five persisted, default-empty fields on `Incident`:** `last_action`, `last_action_at`,
`verify_after`, `verification`, `verification_detail`. Empty `verification` means *no action
was ever executed* — which is the truth for every incident already on disk, and is
deliberately **not** back-filled to a verdict.

**The verdict vocabulary** (`VALID_VERIFICATIONS`), and why each exists separately:

| verdict | meaning |
|---|---|
| `pending` | an action landed a 2xx; the recheck is scheduled and has not run |
| `cleared` | the recheck ran against a **successful** poll and the signal is gone |
| `still_firing` | the recheck ran against a successful poll and it is **still firing** |
| `unknown` | the recheck was due and **we could not look** |
| `not_checkable` | executed, but its success is not observable here |

`OPEN_VERIFICATIONS = {pending, unknown}` — `unknown` is **not terminal**. "We could not
look" is a statement about us, not about the world, so a later cycle where the source
answers replaces it. Freezing it would be the absence-is-evidence bug in a new place.

**Absence is not evidence, restated at a second boundary.**
`dispatch.verify_pending_actions` refuses to reach any verdict for a source whose
`poll_health` entry is missing or `ok: false` — the same rule `reconcile.md` Pass 1 step 3
states for resolving on absence. Here it is worse than a wrong board row: reading a failed
poll as "the fix landed" would feed a **false positive** into the ledger's track record,
making a fix that never worked look proven.

**Three absences, not one — the other two also reach `unknown`.** A missing signal from a
poll that SUCCEEDED still proves nothing in two further cases, and neither may read as
`cleared`:

- **A push spool** (`snapshot: false`, see § Absence is not evidence). Absence is the steady
  state for the webhook source — a claim removes the signal (`webhook.ack`) and nothing ever
  re-asserts it — so reading absence as cleared would verify an action as `cleared` one cycle
  after any delivery, with the fault live.
- **A signal now `suppressed`.** This is the worst of the three to misread, because after a
  `silence` *this app itself issued*, "the provider reports it suppressed" is precisely what
  SUCCESS looks like — so reading it as cleared would congratulate the app for muting a live fault
  and grow `use_count` on the entry that recommended it. `unknown` rather than `still_firing`
  because the condition is genuinely unobservable while muted: the provider has stopped
  evaluating it into a firing state, and only the suppression lifting can answer the
  question. The attribution in the detail comes from **this poll**, not from
  `Incident.signal` — that snapshot was taken at claim time, before anyone parked it, so it
  names nobody in exactly the case that needs a name.

**A SIMULATED action schedules no recheck at all.** `ActionResult.simulated` (default
`False`) marks a sink that RECORDED the intent instead of performing it — `NoopActionSink`,
the observe-only default. `ok=True` there means "we successfully did nothing", which the
recheck cannot distinguish from a real write: it would read the still-firing alarm as the
action having failed and charge a `miss_count` to every entry in `ledger_matches`. On a default
install that is the ONLY path, because `cloudwatch` and `webhook` register no `ActionSink`
and every action falls through to `noop` — so exercising the proposal flow, which is exactly
what an operator is told to do before granting real authority, would **demote their own proven
knowledge for a write nobody made**. The `/incident/action` handler therefore gates on
`result.ok and not result.simulated`.

**Only some verbs are verifiable** (`VERIFIABLE_ACTIONS = {resolve, silence}`). An `ack`
leaves an alert firing *by design* — `normalize_state` maps `acknowledged` onto `firing` on
purpose (see `providers/pagerduty.py`) — so firing state carries no information about
whether the ack landed. Those are stamped `not_checkable` with **no** due date rather than
judged against the wrong evidence, and the board says "not checked" instead of leaving a
blank that reads as success. That is an admitted gap: no adapter here reports
acknowledgement state back.

**Two schedules.** A SUPPRESSION is rechecked at the END of its own window — the schedule
contract 3a's mandatory expiry buys, and the more interesting moment, because a suppression
that expires straight back into the same firing condition is positive evidence nothing was
fixed. Everything else waits `DEFAULT_VERIFY_AFTER_SECS` (5 minutes), long enough for a
provider evaluating on a period to catch up.

"A suppression", not "a `silence`" — the schedule is not keyed on the VERB, because the verb
is not always the truth about what happened. Datadog implements `resolve` as an alias onto the
same bounded mute (a monitor cannot be "resolved" through the API), and only
`EXPIRING_ACTIONS` (i.e. `silence`) receives a `duration_secs` from the route — so a resolve
keyed on its verb would establish a four-hour mute, be scheduled on the five-minute default,
recheck INSIDE its own suppression, read the monitor as still Alert, and charge a `miss` to
every ledger entry the investigation cited. That is the same false-miss accounting the `simulated` flag exists
to prevent, arriving through the schedule instead of through the sink.

The sink reports the window it actually established (`ActionResult.suppressed_secs`,
default 0) and BOTH execution paths schedule from that, preferring it over the requested
duration. "Both" is load-bearing: `/incident/action` and the approved-proposal path
(`_execute_stored_proposal`) are two call sites, and if one read the payload alone an approved
Datadog resolve would get a five-minute recheck against a four-hour mute. A structural test
therefore pins the CONVERGENCE — it parses
the whole HTTP surface (`routes.py` and every `http_routes` module) and fails if the two call
sites pass different duration expressions, which is cheaper
than rediscovering the drift from a false ledger miss.
Reported by the ADAPTER rather than inferred at the boundary because only the adapter knows
its provider aliased one verb onto another. Dropping `ACTION_RESOLVE` from Datadog's
supported actions instead would revoke a capability from every act-rule that
already grants it on upgrade, which the alias exists to avoid.

**No new cron.** The recheck rides on the poll `run_cycle` already made, so it costs zero
extra provider calls and stays inside the heartbeat's flat cost. Verification is a **read**,
so it sits entirely inside the read-only-by-default posture.

`CycleResult.verifications` is `{incident_id: verdict}` for the incidents decided *this
cycle only*; an empty map is the normal case and is **not** "every action worked". It feeds
`changed` only for `still_firing` — the app discovering a claim it made was untrue is the
most newsworthy thing a cycle can find, while announcing `cleared` would make the heartbeat
congratulate itself and announcing `unknown` would broadcast a non-finding.

**A `still_firing` verdict charges a miss** to every entry in `ledger_matches`, not to "the
one we used": nothing records which match the investigation applied (`proposed_action` records
the proposed action, not which ledger match was applied), and `MAX_MATCHES_PER_SIGNAL` is 3 so the blast radius is
bounded. That join is what makes `use_count` mean "worked" (contract 2b).

The postmortem carries the verdict too (`store._verification_line`), because that artifact
is what a colleague reads with no access to the board, and "Actions taken: silenced the
alarm" is the sentence most likely to be believed as an *outcome*. It renders nothing at
all when no action was taken — a "not applicable" line on every incident would bury the
cases where it matters. `verification_detail` goes through the redactor (it quotes a
provider's poll-failure text); `verification` and `last_action` do not, being closed enums
we assign.

### 4. Keystone secret path (security)

`ops_mission_control_secrets.json` on the crew home, registered in
`security._CREW_SECRET_LEAVES`. That places it on the shared **read+write**
sensitive-path floor, so the agent's own file tools (`is_sensitive_path`) and shell
forms (`is_sensitive_bash_command` — `cat`, `>`, `tee`, `tar -C`/`unzip -d`) can
neither read nor overwrite it.

Do NOT move these tokens into `config.json` or the app's `data/config.json`: the
latter is served over `/api/apps/<name>/config` **without session auth**, and the
former is writable by any auto-approved agent shell. The owner-only dashboard
PUT handler is the only writer and opens the path directly, bypassing the gate, so
Settings still works.

`test_security.py` asserts `SECRETS_FILENAME in security._CREW_SECRET_LEAVES`, so a
rename that forgets the registration fails the build rather than silently dropping
the protection.

**Retention: credentials outlive an uninstall, deliberately — and it is disclosed.**
The file sits at the crew-home ROOT, which is what puts it on the sensitive-path floor.
The consequence is that `uninstall_app` (which removes `apps/<name>/`) cannot reach it,
so a PagerDuty/Datadog token survives uninstall. Moving it under the app dir would hand
the agent its own credentials; silently wiping tokens would break the legitimate
uninstall/reinstall flow. So the behavior stays, and Settings states it plainly next to
the Revoke button — the only control that changes it. Two tests pin both halves
(`test_secrets_live_outside_the_app_dir_so_uninstall_cannot_reach_them`,
`test_settings_discloses_that_uninstall_keeps_credentials`), the first of which also
fails if a future change moves the file under `apps/` and thereby drops the keystone
protection.

**Disable is clean** (verified live): crons fully deregister — `/api/crons` shows zero
for this app — and every route 403s via `_require_enabled`. Credentials are kept, which
is right for a pause.

## Provider seam (`backend/providers/base.py`)

Four narrow Protocols, each with a shipped default, following the CPP pattern in
`platform/interfaces.py`:

| Protocol | Question | Public adapters |
|---|---|---|
| `SignalSource` | What is firing? | `cloudwatch`, `pagerduty`, `incidentio`, `datadog`, `github-issues`, `webhook` |
| `RotationSource` | Who is on shift? | `pagerduty`, `incidentio`, `schedule-file`, `always-on` (fallback) |
| `ActionSink` | Ack / resolve / comment / silence | `pagerduty`, `incidentio`, `datadog`, `github-issues`, `noop` (default) |
| `EvidenceSource` | Surrounding context | `cloudwatch-evidence`, `datadog-evidence` |

Split four ways rather than one fat interface because real providers cover
different subsets — CloudWatch has alarms and metrics but no rotation and nothing
to resolve.

A sink also covers only the verbs its provider actually has. `incidentio` offers
`resolve` and `comment` and nothing else: an incident.io alert's status is a strict
`firing`/`resolved` enum with no acknowledged state, and the API has no snooze, mute
or suppress call for a single alert (a maintenance window is account-level config).
Advertising a verb the provider cannot perform would pass the autonomy gate and then
fail at execute time, after the board had recorded the action as granted.

### Evidence is brokered to the agent, never delegated

The investigating agent's sandbox has **no** AWS/provider credentials, and
deliberately gets none. The gateway already holds the operator's profile and already
redacts at a single chokepoint, so it gathers the evidence and the brief carries the
resulting *text*: **gateway reads (credentialed, bounded, redacted) → brief → agent
reasons**. Giving the agent its own profile would create a second credential holder
whose reads nothing redacts and whose scope nothing bounds — the opposite of the
least-privilege guidance that says prefer scoped access over distributing credentials.

Wired at both claim paths (`run_cycle` and the manual `/incident/claim`) via
`gather_evidence_safely`, which treats any fault as "no evidence": an investigation
without evidence is worse than one with, and far better than a dropped claim.

A brief carrying only signal metadata and ledger hints would leave an AWS investigation with no
alarm history and no logs, and an agent that correctly reports it cannot proceed reads like a
credentials gap when it is a plumbing one.

**The no-credentials statement is unconditional.** It is emitted for every brief, including the
case that most needs it — *no* evidence gathered (unconfigured source, provider outage, empty
poll). Without it an agent spends its turn re-running `aws … --profile <name>`, collecting
`NoCredentials` each time, and concludes the profile is a hollow stub, when the profile is healthy
and the gateway reads that same account fine. The statement names the dead end concretely
(`Do not run aws …`), because "you lack credentials" alone still leaves one
`sts get-caller-identity` looking worth a try. Pinned by
`test_brief_always_states_it_has_no_credentials`, which asserts with evidence **empty** — a test
with evidence present would pass on a conditional statement too.

For diagnosis: the sandbox layer is **not** what blocks the
agent's AWS access here. `agent.sandbox` defaults to `auto`, which engages OS isolation at the standard
tier, and that tier leaves `~/.aws` visible by design (`_STANDARD_DIRS` does not hide it), and `security.py` blocks
credential-file *content reads* (`cat`/`grep` on `~/.aws/`) but not AWS CLI invocation.
The agent's bash children are isolated by **kiro-cli's own** internal sandbox
(`~/.kiro/settings/amazon-internal.json` → `{"sandbox": true}`), a layer Kiro Crew
delegates to rather than controls. Brokering is what makes that irrelevant: the gateway
holds the credential, so the agent never needs one.

**The brief bounds evidence separately from the adapter budget.**
`EvidenceBudget.max_bytes` (64 KB) caps what an adapter may *return* — right for a
spool, far too large for a prompt (6 calls × 64 KB ≈ 384 KB, against the background
allowance `_CONTEXT_BUDGET_BASE` in `context_assembly/budget.py`); two raw items alone can run to tens of
thousands of characters. `MAX_BRIEF_EVIDENCE_CHARS` (8k total) and
`MAX_BRIEF_EVIDENCE_ITEM_CHARS` (4k per item) bound the rendered text, and the brief
**says** when it truncates — an agent silently handed half a log dump will reason
confidently about a partial picture. A bounded brief still carries both the alarm history and
the root cause.

### Per-adapter evidence budgets

One `EvidenceBudget` for every adapter does not match how they behave: a CloudWatch Logs
Insights query is submit-then-poll and legitimately wants ~25s, while a Datadog REST call either
answers in seconds or is broken, and an adapter-local ceiling applied as
`min(own, budget.timeout_secs)` is **unreachable dead code** above the global.

An adapter may declare `evidence_budget_hint`, and `EvidenceBudget.for_source`
resolves it **clamped with `min` on every field**. The hint says "this is what I need";
the operator's configured value stays the authority. An adapter that could raise its own
spend ceiling would be an adapter that sets its own cost — the same reason the autonomy
gate is resolved outside the adapter: an operator 30s with a 25s hint resolves to 25s, and an
operator 20s or 8s wins.

No hint means no change, so this is opt-in. The fan-out waits on the **same** resolved value the adapter was handed —
passing one timeout into `gather` and enforcing another outside it kills an adapter
mid-call while it believes it has budget left.

Expose a hint as `MappingProxyType`, not a bare dict: a mutable class attribute shared
across instances is one accidental assignment away from an adapter rewriting its own
ceiling at runtime. `for_source` therefore checks `Mapping`, not `dict` — checking `dict`
silently ignored every correctly-written hint, which the tests caught only because they
assert the clamped **value** rather than that the call returned something.

### Evidence redaction is a single chokepoint

`OpsProviderRegistry.gather_evidence` is the **only** caller of any adapter's
`gather()`, and it redacts every `Evidence.body` through
`redact_tokens(security.redact(...))`. That is what lets `Evidence`'s docstring
promise an adapter "cannot forget" — a second call site would silently bypass it, so
`test_providers.py::test_redaction_is_the_only_path_out_of_an_adapter` pins the funnel
by source inspection. This matters because evidence is largely **log content**, and
governance guidance on logging is explicit that logs must not contain secrets —
which is precisely why credentials turn up there by accident.

Redaction runs **before** the byte cap, not after. A redaction marker is longer than
most of what it replaces, so capping first would let the emitted body exceed
`budget.max_bytes` (an all-credential body grows past it) — and that budget
exists to bound what reaches the model's context, so it has to bound the text
actually emitted. A `_REDACT_HEADROOM` pre-trim still bounds the regex work so a
misbehaving adapter cannot hand us unbounded text to scan.

### Evidence config resolves in its own namespace

`CloudWatchEvidenceSource` advertises `config_fields` under its own id
(`cloudwatch-evidence`), so Settings writes to `providers["cloudwatch-evidence"]` —
and `log_groups` exists **only** on the evidence adapter, so gather code reading
`providers["cloudwatch"]` would find nothing and log evidence would be silently always
empty. `_evidence_value` / `_evidence_list` read the adapter's own namespace and fall back to the signal source's, so a
single-account install that configured `region`/`profile` on `cloudwatch` keeps
working. `configured()` accepts either namespace's enable for the same reason.

Generalized as a test: every advertised `config_fields` entry must actually be read
somewhere in the module. A field the UI renders an input for but the code never
resolves is a lie to the operator.

Verified live: both branches return real data (alarm
history showing a flapping OK↔ALARM pattern, and Logs Insights returning the actual
`ValueError: File processing failed` root cause).

### Boolean provider config is parsed, not `bool()`-cast

`config_flag` reads boolean provider-config fields. Two real bugs it fixes:

- `include_insufficient_data` was compared against the literal string `"true"`, so
  `yes` / `1` / `True ` / a real JSON boolean all read as **false, silently** — the
  operator saw the setting applied and stale-metric detection stayed off.
- `provider_enabled` used `bool(...)`, and `bool("false")` is `True` — so a config
  carrying `"enabled": "false"` (hand-edited, or written by a form that stringifies)
  would **enable** the provider, the opposite of what it says.

An unrecognized value falls back to the caller's `default` rather than guessing:
treating garbage as false silently disables a detection the operator believes is on,
and treating it as true silently enables one they never asked for. `_FALSY` is
therefore listed explicitly rather than inferred as "not truthy".

`INSUFFICIENT_DATA` is the CloudWatch equivalent of a *table-freshness*
check — a pipeline that silently stopped running looks healthy when you only watch
`ALARM`. It stays opt-in (noisy on accounts with idle resources), but the provider
`detail` says so, because an opt-in nobody is told about is one nobody uses.

### Registry is ADD-only

`OpsProviderRegistry.register_*` refuses an id that already exists and logs a
warning; the incumbent wins. A companion package can ADD adapters, never repoint a
core one — otherwise auditing the public core would require auditing every
companion. The core never imports a companion and never branches on edition.

### Fan-out resilience

`poll_all` runs configured sources concurrently with a per-source timeout and
returns `(signals, errors)`. One unreachable provider yields a per-source error
entry, never an exception — the heartbeat must survive a dead provider.

### Absence is not evidence (`registry.poll_health`)

**A signal missing from a poll means one of THREE opposite things: it cleared, we could
not look, or this source structurally cannot tell us.** Without a record of which, a 429, a
timeout, an expired token, or a storm that pushed a signal off the first page would all read
exactly like "resolved", and reconcile would close live incidents into a *terminal* status
carrying "signal cleared at the provider", recoverable only when the alarm fired again as
brand-new work.

Two mechanisms, both in `registry`:

- **`poll_health()`** — per source, whether the LAST poll attempt succeeded, with the
  reason and timestamp. A source **absent** from the map has not been polled and must be
  treated as "cannot conclude", not as healthy. Surfaced at `/signals` alongside
  `all_sources_healthy`; `reconcile.md` requires consulting it before resolving on
  absence, and resolving directly only for signals in the `cleared` list (an explicit
  provider `ok` is positive evidence rather than an inference).
- **`poll_health()[src]["snapshot"]`** — whether that successful poll was a COMPLETE
  picture, i.e. whether absence from it is evidence at all. `ok` answers "did we look";
  this answers "did we see everything", and for one source they differ. Read off
  `SignalSource.is_snapshot`, defaulting `DEFAULT_IS_SNAPSHOT = True` so every polled
  provider API — and every companion adapter that does not set the flag — keeps its
  correct behaviour, and only a queue-draining source opts out.

  `WebhookSignalSource.is_snapshot = False` is the sole exception and the reason the flag
  exists: it is a **push spool**, so a delivered signal leaves the spool once an incident
  claims it (`webhook.ack`) and is absent from every cycle after that whether or not the
  fault is still live at the sender — a push source announces a fault, it never re-asserts
  one. Recorded only as `{"ok": True, "signals": 0}`, that empty result would read to
  `dispatch.verify_pending_actions` as "the source answered and the signal is gone", so one cycle
  after any webhook delivery an action against that signal would verify as `cleared` ("the
  resolve held") with the fault untouched — the same class as resolving on a failed poll, reached
  through a **successful** one, which the `ok` guard cannot catch. Both consumers gate on
  it: `verify_pending_actions` returns `unknown` (open, so a later cycle retries) and
  `reconcile.md` Pass 1 step 3 requires `snapshot != false` on top of `ok`. Rendered on the
  Signals source row and qualified into the "every source answered" banner, because that
  banner is the line an operator reads before trusting a quiet board.
- **An adapter must RAISE on a fault, never return a short list.** `poll_all` records a
  source as unhealthy only when `poll()` raises, so an adapter that catches its own failure
  and returns `[]` is recorded as a *successful* poll that saw nothing — which is precisely
  the "it cleared" reading this whole section exists to prevent, reached through the `ok`
  guard rather than around it.

  `CloudWatchSignalSource._poll_sync` raises on both of its failure paths, with a message
  naming the likely cause: a `None` client (boto3 missing, bad profile, **expired
  credentials**), and a `describe_alarms` exception (which, with `include_insufficient_data`
  on, would otherwise silently truncate the estate in the second pass). Returning `[]` or the
  signals gathered so far would render expired credentials as an all-clear over a live estate,
  with `all_sources_healthy` promising absence-means-recovery on top of it.

  This costs nothing on a default install: `provider_enabled` defaults to **False**, so
  nothing polls CloudWatch until the operator turns it on — at which point a credential
  fault is exactly what they need to be told. `configured()` remains the honest place to
  say "no AWS here". Pinned by
  `test_providers.py::TestACloudWatchFaultIsNotAQuietEstate`, which asserts the registry
  records the fault as unhealthy rather than asserting only that an exception escapes.

  **Rule for any new adapter:** catching an exception inside `poll()` to keep the cycle
  alive converts "we could not look" into "nothing is firing". Let it propagate; the
  registry already turns it into per-source health plus a backoff window.
- **Backoff.** A failed source is skipped for a window and says so in `errors`, honouring
  the provider's own `Retry-After` when sent (clamped by `MAX_RETRY_AFTER_SECS`) and a
  flat `DEFAULT_BACKOFF_SECS` otherwise. `HttpError` carries `status` + `retry_after`
  and `is_retryable`, and the status is read: a rate-limited provider re-polled at full rate
  every 120 s is how a rate limit becomes a ban. A success clears the backoff. Only `RETRYABLE_STATUSES` get the
  provider's own delay: a 404 or 401 is a config fault that waiting will not fix.

  **A TIMEOUT arms the window too**: `asyncio.TimeoutError` *is* an `Exception`, so a
  separate `except` clause for it must call `_note_backoff` as well, or it shadows the generic
  one and leaves the single most expensive failure mode as the only unthrottled one. A source that fails fast costs a socket; a hung one burns the full
  `DEFAULT_POLL_TIMEOUT_SECS` (15 s) out of **every** 120 s heartbeat for as long as it
  stays hung.

A related boundary: `/signals` returns every signal regardless of state under `signals`,
while `dispatch.run_cycle` claims only firing ones. The webhook can emit `ok`, so the route
also exposes a state-filtered `firing` list (and `unclaimed` is derived from it), or an already-recovered signal would appear as
apparent work in the very list reconcile reads as "still firing".

`/signals` carries a **third** bucket, `suppressed` (contract 1b) — the third reason a
signal can be absent from `firing`, and neither of the other two: it did not clear, and we
did look. It must not be resolved on absence (nothing was fixed) and must not be folded into
`cleared` (which asserts recovery). The raw `signals` key is unchanged for compatibility,
which is exactly why the bucket is required: a UI counting `signals` under a column headed
"Firing" would render "3 firing" above an empty queue with no explanation. `reconcile.md`
Pass 1 step 5 states the rule for the agent side.

`gather_evidence` passes every body through `security.redact` **and**
`secrets.redact_tokens` centrally, so an adapter author cannot leak a credential
into a model prompt by forgetting to redact.

**The evidence TITLE gets the same passes, and did not.** `gather_evidence` redacted each
`Evidence.body` at this chokepoint but copied `item.title` raw — and an evidence title is
provider-controlled too (a CloudWatch alarm name, a Datadog monitor title), flowing into
`ClaimedIncident.to_dict()`, i.e. the dashboard JSON and the model brief. The same title/body
asymmetry the Slack sink and the brief each had in earlier rounds. Titles are short, so no
headroom/budget dance — just both redactors.

**The brief's own metadata needed the same floor and did not have it.**
`investigation_brief` prints the signal's `title`, `resource` and `url` alongside that
redacted evidence, and rendered them raw. A signed webhook is accepted from anything able to
POST JSON and a console link can carry a token in its query string, so provider metadata is
exactly as untrusted as a fetched log line — and the brief goes into the agent's context, and
from there into the transcript and any session artifact, so the metadata needs the same floor as
the evidence beside it. `dispatch._safe_field` applies
both passes (via `redact_via_context`, for the companion-seam reason `gather_evidence`
documents). Fields this app owns — `source`, `severity`, `fired_at`, `fingerprint`,
`operating_mode` — are deliberately not redacted: they are values we assign, and masking one
could only corrupt something the agent needs to reason about.

## Security model

### The autonomy ceiling lives on the keystone floor

`mode` (observe/propose/act) and the per-signal `autonomy_rules` are the app's SECURITY
CEILING: `effective = min(app_mode, rule_mode)` is only a ceiling if the party it constrains —
the agent — cannot raise it.

**When several rules match one signal, the TIGHTEST wins** (`min` over `MODE_ORDER`, then
clamped again by the app ceiling). A second rule exists to carve out an exception: an operator
who grants `act` across a service and then adds a narrow `observe` rule to hold back one
critical queue expects the narrow rule to hold, and `max` (the most permissive match) would
let the broad grant authorize the write anyway. `min` also matches the algebra used everywhere
else in the module (`effective_mode`, the governance ceiling), so overlap resolves the same
direction throughout. Asserted through `authorize_action` — the decision that actually reaches
a provider — and the test fails under `max`.

`mode` and `autonomy_rules` therefore do not live in `data/config.json`, which is served over
`/api/apps/<name>/config` **without session auth** and is writable by any auto-approved agent
shell; there a prompt-injected agent could PUT `mode=act` with a rule matching a signal and
unlock a provider write the operator never granted. They live in
`ops_mission_control_policy.json` on the `security._CREW_SECRET_LEAVES` floor
(`backend/policy_store.py`), exactly like the provider secrets — the agent can neither read nor
overwrite it. The owner-only dashboard `PUT /settings` is the sole writer and opens the path
directly. The keystone is read ONLY from itself; a test pins the policy path onto
`is_sensitive_path` and the filename equal to the fence entry.

**The ceiling and provider-config writers are owner-only.** `PUT /settings`,
`PUT /providers/{id}/config`, and `PUT` / `DELETE /providers/{id}/secret` call
`require_owner_dashboard_request` (`dashboard/handlers/_shared.py`) after the app's enable gate
and before reading the body. A non-owner subject, and every app token including this app's own,
gets `403` `owner_only` (a stale pre-owner bootstrap session gets the 401 re-auth answer); the
denial is SEL-audited. The agent-facing routes are not owner-gated. Pinned by
`test_config_routes.py::TestTheCeilingAndConfigWritersAreOwnerOnly` and
`test_provider_secret_owner_gate.py`.

**An owner-only PUT only holds if the agent cannot mint the owner's token.** The dashboard token
comes from `kirocrew token`, which reads the sensitive-path-fenced `.local_secret` in a SUBPROCESS
the tool-call fence does not cover, and prints a URL that authenticates every gateway route —
including this PUT. So the ceiling is only as strong as the deny-command rules that block the
mint, and those are a generic security control in `security.BUILTIN_DENIED_RULES`, not an ops
one: every authenticated route shares the exposure. `credential-exfil-kirocrew-token` is the
raw-text half (a command-position pattern that also accepts the `-`/`.` separator, so the
`python -m kiro_crew token` module form is covered too) and is enforced together with the
argv-structural floor `_is_credential_mint` — a union, so neither can fail open alone.
`credential-exfil-kirocrew-token-argv` covers an interpreter payload that spawns the CLI through a
library call. See [security](security.md) for the matcher detail.

The inline-program form needs its own rules, because `python -c "from kiro_crew.cli import
main; main()" token` reaches the identical mint with the import name buried in an inline
program. Two parsing rules apply together, and either alone still permits the mint:
`_is_self_module_invocation` treats the `-c` payload as a program rather than a script name, and
the verb scan in `_is_credential_mint` does not read a `;` INSIDE the quoted payload as a
top-level command separator. Both `-c` spellings (separate and attached operand) are matched.
On the `-c` path the VERB REQUIREMENT IS NOT ENFORCEABLE: the payload is arbitrary Python with the
interpreter's full authority, so it can BUILD the verb rather than pass it —
`python -c "import sys; sys.argv.append('token'); from kiro_crew.cli import main; main()"` names
no `token` argv word at all, and neither does `main(['token'])`. So a `-c` payload matching
`_SELF_IMPORT_RE` is denied on the IMPORT alone (`_has_self_importing_inline_program`), while the
verb stays the trigger everywhere else, because `kirocrew doctor` is legitimate and only
`kirocrew token` mints.

Both rules match the payload RAW rather than through `_normalize_operand`: that helper truncates
at the first control operator, which is right for an operand the shell will split and wrong for a
quoted Python program whose `;` is a statement separator — it would reduce
`"import sys; …; from kiro_crew.cli import main"` to `import sys` and hide the import entirely.

The `-c`/`-m` tests pin a blocked set and an allowed set, the latter including `python -c 'print(1)' token` and
`grep -r kiro_crew src/` to show the deny is scoped to code we are about to run, and a payload
that imports the package WITHOUT the mint verb (`python -c "import kiro_crew.cli" && echo ok`)
still allowed — the verb is what the rule blocks, not the import.

Obfuscation is where this matcher meets its limit, and the rule draws the line honestly rather
than pretending to close it. `\bkiro_crew\b` misses a name assembled at runtime —
`__import__('kiro'+'_crew')`, `importlib.import_module(name)`, `exec(base64.b64decode(...))` — so
an inline-program payload combining an interpreter with any of a NARROW list of dynamic-exec
primitives (`__import__(`, `importlib`, `exec(`, `eval(`, `compile(`, `b64decode`, `marshal`,
`getattr(`) is treated as opaque and denied: "I cannot tell what this imports" is the fail-closed
reading on the mint path. Deliberately NOT a blanket deny on all inline Python — `python -c
"print(1)"` and routine one-liners stay allowed — because the blanket version has a large blast
radius AND is still incomplete. The genuine residual (a name built with `chr()` arithmetic, read
from a file, fetched over a socket, or a written-then-run script) is arbitrary code as the same OS
user, which is outside any command-text rule's reach. That residual is why the UN-disableable
guarantee for this credential is the sensitive-path floor over the signing key — `.local_secret`
is read+write blocked on both the tool and shell gates — and these deny rules are documented
defense-in-depth on top of it, not the boundary. Two tests pin both halves: the dynamic-exec forms
are denied, and the `chr()`/two-step forms are the acknowledged gap.
The STDIN forms are the same escape with no operand at all: `python -` and a bare interpreter
read the program from stdin, so `python - <<'PY' … PY` and `echo '…' | python -` reach the CLI
with the payload nowhere in argv. When that program text is visible on the command line the
import is matched in the tokens that actually CARRY it, and nowhere else in the frame. The
carriers are enumerated from the shell grammar rather than by example: a heredoc body
(`<<TAG` / `<<-TAG`), a here-string operand (`<<<WORD`, whose word IS the program), a redirected
file (`<WORD`), a process substitution (`< <(cmd)`, whose command text spans tokens to its
closing paren), and a pipe producer. A redirection may appear ANYWHERE in a simple command, the
program name included, so the whole frame is walked in ONE pass and a redirect glued to a word is
classified from its first `<` onward — `<<'PY' python -`, `<prog.py python3 -`, `python3<<<'…'`
and `<<EOF python - … EOF` (marker and body straddling the program name) are all ordinary bash
reaching the identical mint. Only redirect OPERANDS are yielded, so a neighbouring command's
ordinary argument is still never program text. `<&N` carries no text on the
command line and is a stated residual. A heredoc's body ends at the LAST token equal to its
tag: bash closes a heredoc only on a line holding the delimiter ALONE, and line structure does
not survive tokenizing, so ending at the first match would let a body line that merely CONTAINS
the word (`# EOF`, an ordinary comment) close it early and leave the real payload unscanned. A redirect OPERAND that opens a
substitution (`$( )`, `<( )`, `${ }`, backticks) is one shell WORD whose text carries
whitespace, so it too spans tokens — to the LAST matching closer, because `normalize_shell_command`
strips quoting before this code runs, so a quoted delimiter is indistinguishable from a real one
and balancing the count is not decidable. `_python_reads_stdin` consumes redirect operands
through the same helper, so the detector and the carrier scope agree on where an operand ends;
it answers True for `python < prog.py`, which does read its program from that file. Only
operands are scanned, not the whole frame: a frame is not split on a newline, so a neighbouring
command naming the package in a FILE PATH (`isort src/kiro_crew/mcp_core.py` followed by any
harmless heredoc) would otherwise read as a mint with no `token` word present. The pipe
is detected as a CHARACTER left of or glued into the interpreter token, not as a standalone `|`
word: the tokenizer splits on whitespace only, so `echo '…'|python -` hands the operator over
glued to a neighbour and `_program_basename` resolves the program from the last control-operator
segment. Any pipe to the left qualifies the whole left side — a deliberate over-block, since a
missed producer is a bypass while an extra token is a visible refusal. When the program text is
NOT on the command line (a bare `python -` fed by an unseen producer, or a file written earlier
and then redirected in) there is nothing to match and the residual is noted rather than claimed
as covered — the same residual the written-then-run script form already has.
`_python_reads_stdin` is precise (it consumes operand-flags and skips a heredoc's marker, body
and closing tag, and a here-string's operand, read off the raw token because the operand
normaliser strips a redirection to the empty string; a heredoc's closing tag ENDS the command,
so a following `echo ok` is not read as this interpreter's script) so `python script.py`,
`python -c …`, and `cat kiro_crew_notes.txt | python -`
do not trip it, and the inline-program scan bails at the interpreter's first positional so the
ReDoS-resistance budget still holds on spam input.

**There is deliberately NO migration from `config.json`, and adding one is the trap.** A
migration that, on first read with no keystone file, lifted `mode`/`autonomy_rules`/the
destination keys out of `config.json` onto the fenced floor would BE the hole the floor closes.
`config.json` is on no sensitive-path list (`_SENSITIVE_HOME_DIRS` has no `apps` entry;
`_WRITE_PROTECTED_HOME_PATHS` covers only the crew-home-root `config.json`), so an auto-approved
agent shell can write `{"mode":"act","autonomy_rules":[{"source":"pagerduty",
"mode":"act","resource_glob":"*"}]}` there. The next `app_mode()`/`load_rules()` — reached from
`authorize_action` on every `POST /incident/action` — would promote those values onto the
keystone and make them authoritative: `effective` would resolve to `act`, the gate would grant a
real resolve/snooze against production paging, and the same write would redirect
`ledger_sync_remote` and `slack_channel`. The constrained party would set its own ceiling. No
`config.json` ever legitimately held the ceiling, so there is nothing to migrate, and a test pins
that `policy_store` has no `migrate_from_config_if_needed` (re-adding one silently re-opens
the raise).

### Authoring an act-rule (`PUT /settings` → `autonomy_rules`)

**The ceiling is written LAST, after every other field in the request has validated.** More
precisely, the handler is **two phases with nothing interleaved**: phase 1 parses and validates
every field into locals and can only `return 400`; phase 2 performs the writes and cannot fail
validation. A rejected request changes NOTHING. The question "which field is dangerous to
half-apply?" has an answer that keeps growing — `mode=act` plus one malformed rule would leave
the instance in `act` from a request the operator was told had FAILED; `mode=act` plus an
over-long `ledger_sync_remote`, a credential-bearing remote, an option-like branch ref, or a
non-integer tuning value would do the same; `{"primary_instance": false, "ledger_sync_branch":
"--bad"}` would flip leadership, which decides who passes the `not_primary` gate on
`POST /ledger/hygiene` — so the rule is about the request, not about a field list.
`rotation.validate_rules` is split out of `save_rules` so the route validates both halves of the
pair before writing either (both paths share the validation code, so they cannot disagree).

The test pins the PROPERTY rather than the field list: it submits every writable field valid,
poisons one, and asserts across eight poison cases that neither the fenced floor nor
`config.json` moved. It fails against a handler that writes any field before the last
validation.

The two-phase discipline cannot close the concurrent half: `set_mode` and `set_rules` each take
the file lock SEPARATELY, and each call being individually atomic does not stop a CONCURRENT
settings PUT from interleaving between them, so request A's `act` would land with request B's
broader rules and authorize a provider write neither operator asked for. Ordering inside one
request cannot fix that, because the interleaving comes from another request.

The two values are ONE authorization decision (`effective = min(app_mode, rule_mode)`), so they
commit under ONE acquisition via `policy_store.set_ceiling(mode=..., rules=...)`;
`set_mode`/`set_rules` remain as thin single-field wrappers over it. The test asserts an
INVARIANT rather than trying to hit the race: it observes every `atomic_write` publish and
asserts each published (mode, rules) pair is one of the two coherent pairs, so no schedule of
concurrent readers and writers can observe a cross it does not see. It uses no threads and runs
on every platform. The "every writer holds the lock" structural test accepts delegation only
when the wrapper does no read-modify-write of its own.

Fencing the ceiling left it with **no write path at all**: `policy_store.set_rules` had zero
callers, `PUT /settings` handled `mode` but not `autonomy_rules`, and `GET /rotation` returned
only a rule *count*. So the app's headline `act` tier was unreachable — Settings said grants
came from "patterns you have explicitly allowlisted with a rule", rendered an un-actionable
empty state, and offered nothing to click; the manual said to edit `data/config.json`, which
the keystone store does not read at all (and must not — see above). Every act-mode adopter
silently got Propose behavior, with no error anywhere.

The write path is `rotation.save_rules` → `policy_store.set_rules`, reached from the
owner-only `PUT /settings` and nowhere else:

- **Validation reuses `AutonomyRule.from_dict`** rather than restating the rules, because that
  is where "an act-rule may not be a blanket grant" already lives.
- **A rejected rule is refused with a 400, not stored and dropped on the next read.**
  `load_rules` skips unparseable entries, so persisting one would show the operator a grant
  that looks saved and never matches — the exact failure the two-key design exists to prevent.
  The error code names the offending index (`rule_1_invalid`).
- **All-or-nothing:** a submission containing one bad rule leaves the previous grants intact,
  so a bad edit cannot partially revoke working authority.
- **`GET /rotation` returns `rules_detail`** — the parsed rules in the same shape the PUT
  accepts, so the editor reads, edits and PUTs back the same object. A count could not be
  rendered, reviewed or revoked. Entries that failed validation are absent rather than
  displayed as if live.
- The Settings card offers only **configured signal-role providers** (a rule naming an
  unconfigured provider can never match), requires the pattern before enabling the grant
  button, and has a per-rule revoke — authority must be retractable, and saving an empty list
  clears every grant.

**Outbound DESTINATIONS are operator-only for the same reason.** In the agent-writable file
these two keys would give an agent:

| key | what an agent gains by rewriting it |
|---|---|
| `ledger_sync_remote` | redirects the shared-ledger **git push** to a repo it controls — straight exfiltration of the team's accumulated incident knowledge, and `POST /ledger/hygiene` (which the agent's own hygiene cron calls) performs the push |
| `slack_channel` | redirects the incident mirror — every title, diagnosis and resource name — to a channel it chose |

`config.json` is neither path-fenced (`is_sensitive_path`) nor shell-write-blocked
(`is_sensitive_bash_command("echo x > …")`), so both live in `policy_store.OPERATOR_ONLY_KEYS` on the fenced floor, with their `*_enabled` flags
— being able to turn an exchange ON is most of the way to redirecting it.

**The rotation IDENTITY is fenced too.** `mode` and `autonomy_rules` are only two thirds of
the authorization decision. The third is *who this instance is on the rotation*, and in
`config.json` the schedule-file identity would give an agent this:

| key | what an agent gains by rewriting it |
|---|---|
| `schedule-file.github_login` | claims to BE the current on-call member. `authorize_action` → `_definitely_off_shift` → `resolve_now` then matches the live `rotation.yaml` window, the off-shift refusal is defeated, and an off-shift instance performs a real ack/resolve/silence against tooling the on-call instance owns. The same forgery makes `is_primary()` true against a `leader:` it does not hold, bypassing the 409 `not_primary` gate on `POST /ledger/hygiene` so a non-leader prunes the SHARED ledger |
| `schedule-file.strict_gating` | the same effect without needing anyone's login. Setting it false restores fail-open gating, so an *indeterminate* schedule reports `on_shift=True` — and an agent that can write `config.json` can also make the schedule indeterminate, since `rotation.yaml` lives in a repo it can edit. "Break the file, then disable strict gating" reproduces the forgery's result |

Both live on the fenced floor. Fencing the READ alone is not enough: `PUT
/provider/<id>/config` accepts exactly the keys an adapter declares in `config_fields` and
writes them to `config.json`, so `ScheduleFileRotationSource.config_fields` is **empty** —
otherwise the generic provider route would be a second, non-keystone writer for an authorization
input. Their sole writer is the owner-only `PUT /settings`
(`schedule_github_login` / `schedule_strict_gating`), and Settings' on-call card posts there.
The rule generalizes past this key: **fencing two thirds of a decision is not fencing it** —
the audit question is "what inputs does this refusal depend on?", not "which keys look like
policy?" The off-shift refusal depends on these:

| # | input | how it would defeat the refusal if agent-controlled |
|---|---|---|
| 1 | `schedule-file.github_login` | claim to BE the on-call member (above) |
| 2 | `schedule-file.strict_gating` | disable it and an indeterminate schedule permits |
| 3 | `config_fields` advertising the login | a second, non-keystone writer via `PUT /provider/<id>/config` |
| 4 | `providers.<id>.enabled` | a vote that skipped any source where `configured()` is false would read the flag — so one flip would make the source abstain, nothing would answer, and the refusal would stop firing |
| 5 | `pagerduty.user_id` | the same identity as #1, one adapter over — this app's OTHER rotation source |
| 6 | a source with no PRIVATE sync core | a vote that probed only `_on_shift_sync`/`resolve_now` — implementation details of the adapters this repo ships — would abstain when neither existed. `async on_shift()` IS the whole public `RotationSource` contract, so a correctly-written companion source reporting off shift would be counted as not answering, and a matching act-rule would execute a real provider write from an off-shift instance |
| 7 | `pagerduty.schedule_ids` | the LAST agent-writable input this source consults. With `user_id` fenced, an empty schedule list that always reported `unknown` — a non-vote — would let clearing the list in `config.json` make PagerDuty stop answering and the refusal stop firing, with nothing about who is on call having changed |
| 8 | `incidents/index.json` | not an input to the off-shift vote at all, but to WHICH SIGNAL is being authorized. `/incident/action` hands `incident.signal` (read from this file) to the gate, whose rules key on `source`/`resource`/`labels` — so an agent can pair a resource an operator's rule authorizes with a different provider target and have the gate approve one signal while the sink mutates another |
| 9 | `primary_instance` | reaches the ledger-prune gate with NO identity at all: when the schedule names no `leader:`, `is_primary()` falls back to this local flag, so writing `{"primary_instance": true}` into `config.json` self-promotes past the 409 `not_primary` gate and prunes and pushes a shared ledger this instance does not own |

\#4 is not fixed by fencing a key, because the flag legitimately belongs in `config.json` — an
operator does enable and disable providers. It is handled by the gate **not asking**: the vote
consults every non-fallback source and lets each report its own inability to answer as
`unknown`, which the tally already treats as a non-vote. That is strictly safer and also
simpler, since every shipped source already implements that contract (absent or unparseable
schedule, PagerDuty with no `schedule_ids`, a raising request). Genuine indeterminacy still
permits the action — the documented design, so a broken rotation cannot lock an operator out —
but the agent cannot MANUFACTURE indeterminacy: PagerDuty reporting `on_shift=False` refuses
the write whatever its `enabled` flag says.

\#5 is fenced exactly like #1 (`policy_store.PAGERDUTY_USER_KEY`, dropped from `config_fields`,
written by `PUT /settings`). All three identities are reported back on `GET /rotation` under
`identities` so Settings can render and edit them — the provider catalog does not carry them,
and an operator who cannot see which identity is stored cannot tell a wrong one from an unset
one. An identity is not a credential, and `roster.me` already publishes the resolved login.

\#7 is handled by letting the FENCED input decide which kind of absence an unfenced one is: with
an operator-set `user_id` on the keystone, an empty `schedule_ids` is a source that should be able
to answer and cannot, which is an off-shift VOTE (the same reading a raise gets). With neither set
— the normal state of a solo install — it still abstains, so a missing config cannot silently
refuse every manual action. That asymmetry is the general shape: when a decision reads one fenced
and one unfenced input, the fenced one has to arbitrate.

\#8 is handled by PLACEMENT, like the schedule: the index is on `_WRITE_PROTECTED_HOME_PATHS` (one list, which
feeds both the tool and the shell write gates), so the agent's file and shell tools cannot rewrite it while every
instance still READS it (it is the board). Resolving the signal server-side — what `/incident/claim` does for
the same class — cannot help here, because the store IS the server's copy.

\#6 needs no fence — it is not a writable input at all, which is why it is worth listing beside
the five that are: the audit question "what does this refusal depend on?" has an answer that is
not always a config key. Here the dependency would be on a PRIVATE method existing, so implementing
the documented interface correctly would be enough to make the refusal inapplicable. `_shift_sync`
therefore falls back to awaiting the public coroutine on a fresh event loop — safe on this path specifically,
because `authorize_action` already runs in a worker thread (`_authorize` puts it there so
a blocking `gh api user` cannot freeze the loop), and a worker thread has no loop to re-enter. If
a loop IS running the source is skipped with a warning rather than raising inside a security
gate. A companion's coroutine is bounded by `_ASYNC_SHIFT_TIMEOUT_SECS`, and a timeout or raise
PROPAGATES so the caller records `faulted` — a hung or failing source is a positive off-shift
vote, not an abstention, per the fault-vs-absence split above. Six tests pin it, including an
off-shift companion source that offers only the coroutine.

\#9 is fenced the same way (`policy_store.PRIMARY_KEY`, written by `PUT /settings`, read by
`is_primary()`); `GET /rotation` reports it as `primary`, so the Settings toggle
round-trips. A solo install with nothing stored still defaults to primary — fencing the
flag must not stop a single-instance install running ledger hygiene.

**The class is guarded structurally, not per key.** Two structural tests: one walks the REAL registry and fails if any rotation source declares a
config-writable field whose name suggests identity or gating (`login`, `user`, `identity`,
`owner`, `strict`, `gating`), and one parses `_definitely_off_shift` and fails if it calls
`configured()` or `provider_enabled` at all. A name heuristic will false-positive eventually; that costs one line plus a decision
about where the field belongs, which is the conversation that should happen anyway.

Neither structural test covers #9 — the flag is not a provider field and not read by the
off-shift vote, so the heuristics do not see it. What generalizes is the audit question, stated
as one rule: **a security refusal must not depend on an input the constrained party can
write.** #9 also widens it past identity: the gate it
defeats is authorization to prune shared state, and it reaches that gate by asserting a ROLE
rather than an identity.

`ledger_sync_branch` deliberately stays in plain `config.json`: it selects a ref inside a remote
the operator already chose, is shape-validated at the write path, and cannot move data off-box.
The rest of `config.json` — regions, prefixes, alarm filters — is config the agent legitimately
reads and it stays there. `policy_store.put`/`get` refuse any key outside the allow-list, so a
typo cannot silently create a fenced key or quietly write a security value to the wrong file.

### Every read-modify-write on a shared file holds a lock

This app has FIVE files that a request reads, mutates one key of, and rewrites whole
(`atomic_write` replaces the inode): the dispatch index, the ledger, the keystone policy, the
non-secret config, and the keystone secret store. Each has a `platform_compat.file_lock`-based
guard — `store._IndexLock`, `ledger._LedgerLock`, `policy_store._PolicyLock`,
`providers._ConfigLock`, `secrets._SecretLock` — because without one a concurrent update is lost.
The pattern (`data = _read(); mutate; _write(data)`) is the tell, not the file.

Config writers go through `set_top_level`, which holds `_ConfigLock` (`ledger_sync.set_settings`
and `notify_out.set_settings` included), and a structural test forbids any module outside the
store's own definition from calling `write_config` at all, so the only way to write config is
through a locked setter.

The cost of a lost update differs by store: on the config store it is a dropped setting, and on
the SECRET store it is a lost credential — two concurrent saves for different providers, both
returning 200, one of them gone. A structural test asserts each writer holds its lock, because
the behavioural race passes by luck on a fast machine.

#### Deciding a proposal is a compare-and-set

`decide_proposal` runs its whole read → state-check → write inside one `_IndexLock`, so exactly
one caller can move a proposal out of `pending`. The write half of `update_fields` →
`transition` also takes `_IndexLock` and re-reads the index, so two decisions are serialised *at
the write* regardless; what the outer lock adds is that the second caller's state check sees the
first caller's write, instead of both passing the check and the second write landing on top of
the first (`transition` overwrites `proposed_action` without re-checking the proposal state it
was handed).

Pinned **structurally** (the state check is inside the lock; the locked write does not re-enter
it through `update_fields`) rather than by a concurrency test: because of that serialisation at
the write, a harness pausing on `utc_now_iso`, `get_incident`, `_read_index_unlocked` or
`update_fields` produces a single winner even without the outer lock, and a concurrency test
that passes against the bug it names is worse than no test.

`expire_stale_proposals` follows the same rule. Reading the index, testing each proposal and
writing through `update_fields` as separate accesses would let the heartbeat read an expired
draft, a concurrent `/incident/proposal` request revise or decide it, and the stale write stamp
`expired` over the newer state — reverting an operator's decision, or replacing a re-proposed
draft with a dead one. The whole sweep therefore runs under one `_IndexLock`, re-reads each
proposal from the locked index, and mutates in place via `dataclasses.replace` rather than
re-entering the lock through `update_fields`. Pinned structurally, for the reason above.

#### The knowledge ledger

`ledger.hygiene` reads, dedupes/decays/prunes, and calls `_write_all`, which OVERWRITES the file,
so a `POST /ledger` (`upsert`) or a `record_use` landing between its read and its write would be
erased. `_append` alone is git-merge-safe (append-only, deduped on read), but a whole-file rewrite
from a stale snapshot is not. `_LedgerLock` (mirroring `store._IndexLock`, routed through
`platform_compat.file_lock`) guards every read-modify-write path: `upsert`, `record_use`,
`record_miss`, `remove`, and `hygiene`. A test pins that each takes the lock, and another drives
an append into the window hygiene's read opens and asserts it survives.

Every ledger read-modify-write — `ledger_sync.resolve_conflict` included — reads through
`ledger.read_entries_for_update` rather than the lenient `read_entries`, so a file that fails to
open raises `OSError` instead of reading as an empty list, and `sync_safely` skips the cycle
rather than rewriting the shared ledger from nothing.

### The keystone has a lock; every egress sink shares one seam

**`policy_store` holds `_PolicyLock`.** All three writers (`set_mode`, `set_rules`, `put`) are
read-modify-writes, and `atomic_write` REPLACES the file — so without a lock two concurrent
settings PUTs would each write their own key onto a stale snapshot and the later one would
silently revert the other. On this file that is a security defect rather than a lost-update
annoyance — an operator disabling `act` concurrently with any other settings change could have
`act` restored, both requests returning 200. `_PolicyLock` mirrors `store._IndexLock` and
`ledger._LedgerLock`. A structural test asserts every writer holds it,
because the behavioural test can pass by luck on a fast machine.

**All five redaction sinks go through the CPP seam.** `registry.gather_evidence`,
`dispatch.investigation_brief`, the Slack board, desktop notifications and the postmortem redact
through `platform.redact_via_context` rather than calling `security.redact` directly. The seam's
own documented reason applies to all five: a loaded companion's declared patterns apply, and an
enterprise host that fails to compose its companion fails **closed** instead of silently
falling back to public patterns. The test is structural on
purpose — in the public edition the shim just calls the core, so a behavioural test would pass
either way and fail only on a companion host CI never runs.

### A rotation FAULT is off-shift; an ABSENCE is not

If `_definitely_off_shift` swallowed a raising source and moved on, a configured PagerDuty that
timed out would count as "no information" and an off-shift instance could write — during exactly
the window a rotation API is down, which is also when a real incident is most likely.

The distinction: a source that is **configured and failing** would have answered, whereas a
source that is **absent or unconfigured** has nothing to say. A raise is a positive off-shift vote; `unknown` stays a non-vote. `faulted` is tracked separately
from `answered` so a second source's `unknown` cannot mask the fault — two individually-correct
branches combining into a wrong result is the shape of bug that flag separation prevents.

**The gate does not deny whenever any source reports `unknown`.** An
unconfigured source, an absent `rotation.yaml`, and a PagerDuty with no `schedule_ids` all
legitimately report `unknown`, and on a solo install that is the NORMAL state — denying there
makes a missing config silently disable every manual action. Both call sites of
`authorize_action` are authenticated dashboard handlers, so permitting on absence means "a
broken rotation does not block the human", not "the agent gets a free write". Four tests pin
both halves plus the masking and precedence cases.

### The committed schedule is WRITE-protected, not sensitive

`rotation.yaml` decides who is on call, so it is an input to an authorization decision: an
agent that names its own login in it makes `authorize_action` → `_definitely_off_shift` accept
a forged shift and execute a production write against a teammate's incident tooling. Unlike
`config.json`, whose inflated values the loader clamps at load time, nothing downstream
neutralizes a forged schedule entry.

It is on `_WRITE_PROTECTED_HOME_PATHS`, the one list feeding both the tool and shell write
gates, **not**
`_SENSITIVE_HOME_DIRS`. That asymmetry is deliberate and is asserted in both directions: every
teammate's instance must READ the file to answer "am I on call?", and it holds no secret, so
classifying it read+write sensitive would break the feature it exists to serve. Legitimate
writers are unaffected — the app reads with `path.read_text()` and `ledger_sync` converges the
file with a direct `git checkout`, neither of which routes through the agent gates.

**`schedule-file` keeps its authorization vote.** Excluding it would delete the app's
single-owner model: for a team without a rotation service the committed schedule IS the
rotation, so ignoring it means every instance claims every alarm — the exact double-claim the
file exists to prevent. Protection is a matter of PLACEMENT, not logic, so the voting algebra is
untouched.

The entry shape matters: `_WRITE_PROTECTED_HOME_PATHS` is expanded once per
`_CREW_HOME_PREFIXES` entry into `<crew-prefix>/apps/ops-mission-control/data/<file>`, and the
shell matcher builds `<home>/<crew-prefix>/<entry>`, so an entry must carry its
`apps/.../data/` subpath. A bare `rotation.yaml` would match **nothing** while reading exactly
like a finished fix — the tool gate would block writes and the shell path would stay open. A
test pins the entry's shape, not just the behaviour.

### A rotation source without an identity ABSTAINS

`PagerDutyAdapter._on_shift_sync` filters the `oncalls` query by `user_id` and re-checks each
entry against it. With a blank id, a conditional filter would report `on_shift=True` for **any**
teammate's shift, and `_definitely_off_shift` would read a colleague's rotation as this
instance's own and permit a production write off shift. So a blank id returns
`ShiftStatus(unknown=True)` before querying, and the loop's check is unconditional (a blank id
cannot reach it, and a conditional guard there would imply otherwise).

**`unknown`, not `on_shift=False`.** False is the wrong direction. The
vote in `_definitely_off_shift` treats False as a real off-shift ballot, so an operator who set
`schedule_ids` and never set a user id would find every manual action refused with nothing
explaining why — a configuration omission silently disabling the app, which is the failure the
neighbouring "no `schedule_ids`" branch already avoids the same way. `unknown` is a non-vote:
this source steps aside and any other configured rotation still decides. Same reasoning as the
`configured()` rule above: **an input the refusal depends on must be present, and its absence
must abstain rather than guess.**

### A boolean field is never coerced — `bool("false")` is True

`bool(body[field])` on a string is true for ANY non-empty text, so every spelling of "no" a
client might plausibly send — `"false"`, `"False"`, `"no"`, `"0"`, `"off"` — would arrive as
True.

On `/incident/proposal/decide` that would invert an ANSWER rather than a setting: a request
meaning "reject this proposal" would reach `decide_proposal(approve=True)` and execute the
authorized action against the operator's production tooling. Five settings fields carry the
same risk, two of them safety-relevant — `schedule_strict_gating` gates the off-shift refusal, and
`primary_instance` decides who may prune the SHARED ledger.

All six go through `_require_bool`, which accepts the JSON booleans and **nothing else**,
and a non-boolean is a 400. Refusing is the only defensible behavior: there is no safe guess
about which way an operator meant an ambiguous answer to "may I write to production?", so the
request fails and they re-send it unambiguously. `approve` is additionally REQUIRED — absent is
not "no", because defaulting either way invents an answer nobody gave.

Deliberately strict in both directions: `"true"` is refused too. Accepting it while refusing
`"false"` would be the worst outcome, because it teaches a client that strings work and the
next `"false"` that client sends is the one that silently approves. `1`/`0` are refused for the
same reason. A test drives all six fields plus every falsy-looking spelling, and fails against
the old coercion.

### A manual claim authorizes against the provider's signal, not the caller's

`resolve_mode` matches rules on a signal's `source`/`resource`/`labels`, so if `/incident/claim`
took a fully caller-supplied `Signal`, a caller controlling the whole object could pair a resource
an operator's rule authorizes with a different provider's target in `labels` — the resource
passes the gate while another field drives the sink, so the authorization describes a signal
that does not exist. The route polls and resolves the claimed `id` to the provider's OWN
signal, refusing (`signal_not_firing`, 409) if that id is not currently firing; the caller's
other fields are discarded. The board already sends a signal it got from `/signals`, so this
rejects only a fabricated or stale one.

**"Currently firing" needs the STATE filter.** `poll_all` returns every state — firing, `ok`
and `suppressed` — so a lookup matching on id alone would take a signal that recovered between
the board's poll and this one, come back with `ok`, and mint an incident for a fault that had
already cleared. Like the other `poll_all` consumers (`dispatch.run_cycle`, `GET /signals`),
this one filters explicitly. `suppressed` is excluded by the
same predicate and must be — somebody parked that signal at the provider, so claiming it is
precisely what they asked not to happen.

### Autonomy gate (`backend/rotation.py`)

`effective = min(app_mode, matching_rule_mode)` over `observe < propose < act`.

- Default `app_mode` is **`observe`** — nothing is written to any provider.
- `act` requires **both** `app_mode == act` AND a user-authored rule whose
  predicate matches the specific signal.
- **No blanket grants.** `AutonomyRule.from_dict` refuses an `act` rule that names
  only a source, with no `resource_glob` or `label_match`. "Act on everything from
  CloudWatch" is not expressible.

  An **all-wildcard glob is the same grant spelled differently**, so asking only whether a glob
  is PRESENT is not enough: `resource_glob: "*"` is truthy, and `fnmatch` matches every resource
  including the empty string — the provider-wide act grant this bullet calls inexpressible,
  authorable straight from Settings. A glob must carry at least one **literal** character;
  `"*"`, `"**"`, `"?"`, `"*?*"` and whitespace-padded variants (`"*  *"` — no resource id
  is whitespace) are refused for exactly the same reason omitting it is. `observe`/`propose`
  rules may still be broad, because they authorize no write.
- **A malformed `actions` scope is REFUSED, not widened.** `authorize_action` reads
  `not rule.actions or action in rule.actions`, so an EMPTY set means *every* action. A parser
  that filtered unrecognised verbs out silently would invert the operator's intent:
  `actions: ["resovle"]` would filter down to nothing and the rule would then authorize ack,
  resolve, comment AND silence — one typo turning a narrow grant into the blanket one the bullet
  above refuses to let anyone express. A present-but-malformed `actions` (not a
  list, empty, or holding any unknown verb) rejects the whole rule, and `save_rules`
  surfaces that as a 400 naming the index so the operator fixes the typo instead of unknowingly
  running with more authority than they asked for. Omitting the key entirely still means every
  action — that is a choice made by omission, and is what the manual documents.
- A rule can only NARROW the app ceiling, so it cannot escalate an instance the
  operator pinned to `observe`.
- Every authorize decision — allow and deny — is SEL-audited as
  `ops-mission-control.action_authorize`.

This deliberately diverges from a common shortcut: auto-resolving alert types believed
to be always benign. A team that built those alerts itself can reason about which are
safe to close unread; a public install has no such basis.

Remediation *execution* (running a fix) is out of scope. The app diagnoses and
proposes; the human applies.

#### The gate is a chokepoint, not a convention

`ActionSink.execute` does not police its own authority — by design, see [Autonomy gate](#autonomy-gate-backendrotationpy) — and the gate
runs for two independent call sites (`/incident/action` and the approved-proposal path). A
docstring alone (*"callers MUST have resolved the autonomy gate first"*) joining them would let
**a third caller silently skip the gate** with no code disagreeing, so authority is a value
rather than a comment:

- **`_authorize(signal, action)`** (in `backend/http_routes/actions.py`, re-exported as
  `routes._authorize`) runs the gate and is the ONLY place an `_Authorized` permit is
  constructed. A test scanning `routes.py` and every `http_routes` module pins that, because
  a permit minted next to the write would be a rubber stamp.
- **`_execute_authorized(sink, permit, payload)`**, beside it, is the ONLY caller of
  `ActionSink.execute`. A test walking the whole `backend/` tree asserts that call site count
  is exactly one, so a future third caller — in any module, at any depth — fails CI instead of
  shipping an ungated write.
- The permit carries the signal and action it was minted for, and the executor reads the
  write's target **from the permit** rather than from a parallel argument — so spending a
  `comment` permit on a `resolve` is unrepresentable, not merely rejected.
- **Both execution paths must also share the FOLLOW-UP, not just the executor.** The direct
  action (`_handle_action`) and the approved proposal (`_execute_stored_proposal`) converge on
  `_execute_authorized`, and both record `last_action` and arm `_schedule_verification`;
  otherwise a resolve or silence approved from the queue would execute the real provider write
  and leave the incident with `last_action` empty and no recheck scheduled — the record and
  postmortem showing a write that "never happened". Both paths schedule the recheck on `result.ok and not result.simulated` (a `noop`-simulated write changed nothing, so
  rechecking it would charge a false miss to the cited ledger entries). Sharing the executor is
  necessary but not sufficient; the paths also converge on what happens after the write.
- **The capability probe (`_sink_refuses`) FAILS CLOSED.** `supported_actions()` narrows which
  verbs a sink can perform — GitHub Issues supports only `{resolve, comment}`, so an authorized
  `ack` there is an undefined `execute` call against a real repo. It first failed OPEN in three
  ways — a missing method, a raising probe, and an EMPTY set all returned allow — which is that
  exact undefined call reached three other ways. "Could not confirm support" is not "supported":
  authorization says the operator permits the verb, not that this sink can perform it, so a
  broken probe against a production write refuses. `supported_actions` is part of the
  `ActionSink` protocol, so absence is a broken adapter, not a legacy one, and a raising probe
  refuses rather than crashing the action path.

#### The write gate consults EVERY rotation, not just the schedule file

`_definitely_off_shift` does not stop at `rotation.yaml`: reading only that file and returning
False when it is absent would make a PagerDuty rotation reporting "someone else is on call"
invisible here, so with no schedule file on disk `/incident/action` would execute a production
write against a provider this operator is not on call for — the rotation consulted for TIER
arming (through `registry.resolve_shift`) and ignored for **authorization**, the one path where
it matters most.

It iterates `registry.rotation_sources()` and mirrors `resolve_shift`'s algebra deliberately:

- any real source reporting on-shift means on-shift (a person on two rotations is on call);
- `is_fallback` sources are skipped entirely — `AlwaysOnRotationSource` is always configured
  and always on-shift, so counting it would make every real rotation unhearable — the same rule
  `resolve_shift` applies;
- `unknown` is not an off-shift vote, and one exploding source does not decide it;
- True only when at least one real source answered and none said on-shift.

It stays **synchronous**. `authorize_action` is sync by design and its one caller already
dispatches it through `asyncio.to_thread`, so each source's sync core runs off the loop; making
this async would push the await up through the whole gate for nothing. `_shift_sync` prefers a source's sync core
(`_on_shift_sync` / `resolve_now`); a source offering *only* the coroutine is still asked, run on
a fresh event loop in the worker thread and bounded by `_ASYNC_SHIFT_TIMEOUT_SECS` (10 s). It is
skipped only when a loop is already running on that thread.

**The sync core is found by attribute lookup, which is a sharp edge.** A source that keeps its
logic in a module-level function with only an async `on_shift` method would expose no sync core,
so `ScheduleFileRotationSource` defines `_on_shift_sync` as an explicit METHOD, and a test walks
the REAL registered sources asserting each non-fallback one exposes a sync core.

Tests built on fakes that define `_on_shift_sync` cannot catch a shipped class that does not —
the fake is more cooperative than the product. When a
mechanism discovers behaviour by duck-typing, at least one test has to run against the real
objects, or the suite verifies the fake's contract instead of the product's.

#### Registry discovery is warmed at startup, not on the first request

`get_registry()` populates lazily — entry-point enumeration, signed-plugin admission I/O and
companion import all run on its first call. Every producer of that first call is a request
handler (`_handle_signals`, `_handle_claim`, …), so lazily the discovery cost would land on
the event loop: the gateway's first `/signals` poll would stall the heartbeat and every other
task for the length of a filesystem plugin scan. `register_routes` runs synchronously at gateway
startup, before the loop serves anything, so it warms the registry there — the cost is paid once,
off the request path. Wrapped fail-open: this app is default-disabled, and an install that
never enables it must not crash gateway startup on a discovery fault. Two
tests pin that `register_routes` leaves the registry non-None and that a warm-up exception does
not propagate.

#### `ledger_sync._ensure_repo`'s `.gitignore` I/O is off the loop too

`_ensure_repo` is awaited from `sync_safely` DIRECTLY on the event loop (not through
`to_thread`), and it reads and writes the `.gitignore`. The file is tiny and fixed-size, so an
inline stall would be microseconds rather than the hundreds of ms a ledger parse costs — but it
is the same class the off-loop guard exists to keep out, and "small today" is how a growing file
ends up read inline. The read-compare-write is one `to_thread` hop. The AST guard flags a synchronous `Path.read_text`/`write_text` directly in
a coroutine body, skipping nested `def`s (wrapping the I/O in an inner function and handing it
to `to_thread` is the remedy, so flagging it there would forbid the fix).

#### `tier_states` is offloaded wherever an async caller reaches it

`tier_states` → `is_primary` → `_schedule_me` → `resolve_login` can shell out to `gh api user`
(a 10s-timeout subprocess) when a committed `rotation.yaml` names a `leader:` and no
`schedule-file.github_login` is set. Two ASYNC callers reach it: `dispatch.run_cycle`
(the 120s heartbeat) and `rotation.apply_tiers` (awaited from the default-enabled 300s
rotation-check cron via `POST /rotation/arm`). Run inline, either would freeze the loop — chat turn
and liveness heartbeat included — for up to 10s.

`_login_cache` is only a mitigation, not a guard: `registry.resolve_shift` wraps each source in
`asyncio.wait_for`, and a timeout cancels the awaiter while the `to_thread` worker keeps running,
so the cache can still be cold at these sites. Both `await asyncio.to_thread(tier_states,
shift)`. The third `tier_states` caller, `describe`, is SYNC and its callers already offload it —
so an AST guard that walks only `async def`s pins the two that matter without flagging it.

#### The ledger CONFLICT probes are off the loop too

`has_conflict`, `schedule_has_conflict` and `resolve_conflict` are called from `async def
pull`, `push` and `_resolve_schedule_conflict`. They read like predicates, but `has_conflict()`
reads the WHOLE ledger and scans every line for markers, and `resolve_conflict()` re-parses it and
REWRITES it — on the one file in this app that grows without bound, from coroutines the heartbeat
and the hygiene pass await. Inline, a conflicted team ledger would stall the gateway — every chat
token, every other app — for a full parse-and-rewrite. All four sites go through `to_thread`, and
the three helpers are on `FILE_PARSING_LOCALS` in the off-loop AST guard (the guard's bare-name
list, because a module-local call is invisible to its attribute walk — the same reason
`_credential_bearing_lines` is listed by hand).

#### The counter-rule: loop-owned state must NOT be pushed off the loop

`to_thread` is the right answer for file I/O and the **wrong** answer for loop-owned state.
`slack_out.link_thread_to_investigation` and `notify_out.notify_needs_human` sit beside the store
parses, but they mutate `DashboardState` — the slot dicts, the Slack reverse index — and the notify path ends in
`_deliver_note` → `_broadcast`, which does `asyncio.Queue.put_nowait` per SSE client plus
`asyncio.Event.set()`. `Event.set` resolves its waiter futures through `loop.call_soon`, whose
contract is loop-thread-only; `call_soon_threadsafe` is the cross-thread door.

Two things make this a trap. First, **off the loop it appears to work** — the waiter future is
marked done synchronously and the loop notices on its next poll — so nothing fails. Second, a
thread hop buys **no** I/O isolation: `_deliver_note` offloads its own disk append via
`run_in_executor`, but only when it can see a running loop, so from a worker thread it takes the
`RuntimeError` fallback and writes to disk INLINE in that thread. Running these on the loop is both thread-correct and better for
the I/O.

Deleting both calls would remove the replyable-thread link and the needs-human alert — shipped
features, not incidental work — so they are marshalled onto the loop instead. An AST guard
(`LOOP_OWNED_STATE_HELPERS`) is the inverse of the off-loop one, so a "wrap the blocking calls"
sweep cannot push them off it.

#### Anything that can reach `resolve_login()` runs off the loop

`schedule_file._resolve_login_sync` spawns `gh api user` **synchronously** with a 10s
timeout whenever `schedule-file.github_login` is unset — the documented default, because
the provider resolves identity from `gh`. Two independent paths reach it:

- `rotation.authorize_action` → `_definitely_off_shift` → `resolve_now`
- `rotation.describe` → `is_primary` → `_schedule_me` → `resolve_login`

Both go through `asyncio.to_thread` at **every** call site (`_authorize`, and the three
`rotation.describe` sites in `/state`, `/handover` and `/rotation`). Run inline, one request
in a fresh gateway would freeze the whole event loop for up to 10s — the user's chat turn and
the liveness heartbeat with it.

**The trap: "the cache is already warm".** Each `describe()` site is preceded by an awaited
`registry.resolve_shift()` that warms the login cache off-loop, but that does not make an
inline `describe()` safe:
`resolve_shift` wraps each source in `asyncio.wait_for(..., DEFAULT_POLL_TIMEOUT_SECS)`
(15s), and a timeout cancels the *awaiting coroutine* while the `to_thread` worker keeps
running — so the poll can give up with `_login_cache` still unset, and the inline
`describe()` then pays the full spawn on the loop. "Something upstream probably warmed the
cache" is not a guarantee; `to_thread` is.

One shape is subtler than the others:
`await asyncio.to_thread(handover.build, providers, rotation.describe(shift))` would move
*`build`* off the loop while still evaluating `describe()` on it, because arguments are
computed before the call. Moving a slow call off-loop does not move its arguments.

Guarded two ways: a static check that no `rotation.describe` call on the HTTP surface
(`routes.py` and every `http_routes` module) sits outside a `to_thread` — and that it sees
exactly those three sites, so it cannot pass by finding none — and a behavioural one that ticks a heartbeat coroutine during a
`/rotation` request and fails if the loop stalls.

#### Every stored-file read on a request path is off-loop

`companion.companion_summary()` walks `importlib.metadata.entry_points()`, enumerating every
installed distribution's metadata from disk (~9.3 ms). The store and ledger helpers parse a
JSON/JSONL file whose size grows with use:

| helper | empty | 100 | 1k | 5k | 20k |
|---|---|---|---|---|---|
| `ledger.stats()` | 0.1 ms | 1.8 ms | 13 ms | 93 ms | 275 ms |
| `store.counts_by_status()` | 0.1 ms | 4.3 ms | 42 ms | 188 ms | — |
| `store.open_incidents()` | 0.2 ms | 2.3 ms | 70 ms | 251 ms | — |

All of them, on every request path, go through `asyncio.to_thread`.

**"Negligible" measured on an EMPTY store is not a measurement.** ~0.03 ms is the cost on the
single input where the cost is zero by construction; the table above is the growing case, and a
flapping alarm minting hundreds of incidents is documented elsewhere in this spec. That covers
`ledger.stats`, `store.counts_by_status`, `store.open_incidents` (which `/state` needs twice) and
the index parses in `/handover`, `/incidents` and `/signals`.

**The rule:** for anything that parses an accumulating file, the empty case is not the case
worth measuring. And a helper that RETURNS one record does not necessarily DO one record's
work — `store.get_incident` and `find_by_signal` are `read_index().get(...)`, so each pays the
full parse, and both are on the guard's helper list along with their call sites in the
incident, transition, action and proposal handlers. If it touches disk, it goes in the list.

**WRITES belong in the list too.** `store.update_fields` in `slack_out.publish` is a full
read-modify-write of the incident index, on a coroutine `run_cycle` awaits through
`publish_all`. A write parses the same file a read does and then rewrites it, so it is strictly
worse; "reads a growing file" is the wrong frame for the list. `update_fields`, `transition`,
`claim` and `decide_proposal` are covered. That call is reached **only on a successful Slack
post** — a branch no test without a Slack client exercises — which is why the guard is
structural.

#### A provider-supplied URL is never rendered as a live link

`Signal.url` comes from a provider — including the HMAC-signed webhook, which accepts anything
able to POST JSON. Rendered straight into `href={s.url}`, a signal carrying
`javascript:alert(document.cookie)` would produce an executable link **in the dashboard's own
origin**, on an element labelled "Provider" that an operator is invited to click.

Every one of the four surfaces goes through `lib/safeUrl.safeHttpUrl`, which rejects any
non-http(s) scheme and any userinfo, and a rejected URL renders **no link** rather than a dead
one. Sibling apps (`issue-radar`, `ArtifactDeployPage`) use the same helper, and the guard
asserts the class: no `href={…url}` in
the app without `safeHttpUrl` on the same line, and the gate condition must read the VALIDATED
value (gating on the raw URL would render `href="null"` for a rejected one — visibly broken
rather than absent). A third test pins the helper's own behaviour, so loosening `safeHttpUrl`
cannot silently re-expose the app while the first two still pass.

Two guards, both asserting the class rather than the known sites:

- no `store.*`/`ledger.*` file-parsing call in any coroutine under `backend/` — the HTTP surface
  included — sits outside a `to_thread`;
- no slow call is *evaluated as an argument* to `to_thread`. `to_thread(f, g())` moves `f`
  off-loop and runs `g` on it, and reads as fixed at a glance
  (`to_thread(handover.build, providers, rotation.describe(shift))` is the shape).

### AWS access

Ambient credential chain only — profile, role, or instance role. The app never
accepts, stores, or transmits an AWS access key. `boto3` is an **optional lazy
import**: the module imports cleanly without it and the adapter reports
unconfigured. Read-only permissions requested (`cloudwatch:DescribeAlarms`,
`GetMetricStatistics`, `GetMetricData`, `logs:StartQuery`, `GetQueryResults`,
`DescribeLogGroups`); no write permission.

### Route gating

Builtin routes are registered at gateway startup and exist while the app is
disabled, so **every** handler is wrapped in `_require_enabled` (403 when
disabled). `test_routes.py::test_every_registered_handler_is_gated` walks the
router and fails if a route lacks the wrapper, so a newly added route cannot ship
ungated.

**`routes.py` is the composition root; the handler bodies are its projections.** The gate,
the SEL audit writer (`_audit`), the redaction floor (`_safe_outbound`), the ledger indexer
and the literal registration table stay in `routes.py`, because the gateway, the manifest,
the security posture registry and the tests all address that module. Each route's body lives
in a `backend/http_routes/` projection (see Files). A projection never imports `routes`; one
that calls a facade seam — `get_registry`, `_audit`, `_safe_outbound`, `put_secret`,
`delete_secret`, `merge_provider_config`, `_index_ledger_safely` — takes it as a keyword-only
parameter with no default, and the same-named `routes` handler passes its CURRENT binding on
every call, so `mock.patch.object(routes, <seam>)` reaches every call site that reads it
(`is_app_enabled` is read by the gate itself). A helper behind a handler receives the seam
from that handler the same way. The six routes whose projection calls no seam register the
projection itself. Every other name `routes.py` defined is a re-export of its owner's object, or one of
three adapters that keep `_schedule_verification`, `_execute_stored_proposal` and
`_settings_write_or_refuse` callable with their historic signatures: patching it on `routes`
changes what `routes.<name>` returns, not what a projection calls. `test_routes_composition_contract.py` pins the seams site by site, the registration
table, every name `routes.py` defined and its signature, and the structure (one-statement handlers, no
module-scope seam binding in a projection, no import of `routes`, no redactor call in a
projection, one logger name for the whole surface).

Secrets are **write-only** over HTTP: `PUT .../secret` accepts a value, and no read
endpoint ever returns one (`describe_secrets` reports set/unset only). Unknown
secret field names are refused so the keystone file cannot become arbitrary
agent-inaccessible storage.

**The config route refuses secret fields.** `PUT /providers/<id>/config` writes
`data/config.json`, which is served over `/api/apps/<name>/config` **without
session auth**. A settings form that posted a token there would put a live
credential behind nothing but the gateway port, so the route rejects any key
matching the adapter's `secret_fields` (400, SEL-audited) and accepts only keys the
adapter declares in `config_fields`. Pinned by
`test_config_routes.py::test_secret_field_is_refused`.

**A config value that names a HOST is a credential-exfiltration surface.** Refusing to
store secrets in `config_fields` is not sufficient on its own: an ordinary, non-secret
config value can still decide *where the stored secrets are sent*. Datadog's
`site` is region-specific config (`datadoghq.eu`, `us3.datadoghq.com`, …) that forms the
request host, while `_headers()` attaches **both** `DD-API-KEY` and `DD-APPLICATION-KEY` to
every request — so a prompt-injected agent writing `site` through the config route would have
both keys posted to a host it chose, on the next poll, with no user-visible step, if `site` were
interpolated verbatim.

`site` therefore passes through `datadog._site()`, which admits only Datadog's published site
domains (`_ALLOWED_SITES`) and otherwise logs and falls back to the US default. An
**allowlist**, deliberately: a `.datadoghq.com` suffix test is defeated by
`evil-datadoghq.com`, and a "looks like a domain" test admits every domain there is. It
falls back rather than raising, because "we talked to Datadog US and your monitors were
not there" is visible and harmless, while sending credentials to an unvetted host is
neither. The monitor *link* is gated by the same function — no credential rides it, but an
attacker-controlled URL rendered as a clickable link on the incident board is a phishing
target inside the operator's own tooling. Pinned by
`test_providers.py::TestDatadogCredentialsOnlyReachDatadog`, including the userinfo
(`datadoghq.com@attacker.example`) and path/port injection shapes.

**The general rule this establishes:** when adding a `config_fields` entry, ask whether it
can influence a request URL, and if it can, constrain it at the point of use rather than
trusting the writer.

**`cloudwatch.region` is the second instance.** It is interpolated into the console HOSTNAME
(`https://{region}.console.aws.amazon.com/…`), which becomes an "open in provider" link on
the incident board. Under `urlsplit`, `region="evil#"` renders
`https://evil#.console.aws.amazon.com/…` whose real host is **`evil`** (the `#` starts the
fragment and discards the rest), and `region="attacker.example.com"` yields
`attacker.example.com.console.aws.amazon.com`. No credential rides this URL — it is a
phishing vector rather than an exfiltration one, the same reason the equivalent Datadog monitor
link is gated.

`_validated_region()` applies a SHAPE check (`^[a-z]{2,}(?:-[a-z0-9]+)+$`), not an allowlist:
AWS adds regions regularly and a stale list would silently break a legitimate install, which
is the failure mode an allowlist is only acceptable for when the set is genuinely closed
(Datadog's published sites are; AWS regions are not). Every injection character that matters
(`/ # ? @ :` and `.`) is excluded by construction. An unrecognised value degrades to "no
link", never to "a link somewhere else". Case is normalised first so an operator's
`US-EAST-1` typo works, which cannot widen the gate. Validated in BOTH namespaces that expose
the field — the signal source's and the evidence adapter's via `_evidence_value` — because
guarding one read leaves the same field open one namespace over.

**A remote URL is refused, not stored, when it embeds a password.** A `ledger_sync_remote` of
`https://user:ghp_xxx@github.com/org/repo.git` stored as-is would sit in a file served
**without session auth**, and `redact_tokens` has no pattern for a PAT inside a URL; the
frontend's `displayRemote()` strips userinfo for display only. `PUT /settings` rejects it (`remote_has_credentials`, 400) with a message telling the operator
to rotate the token and use a credential helper or SSH.

Refused rather than silently stripped: the pasted token is compromised either way, and a
remote quietly rewritten to an unauthenticated URL would fail to push later with no hint why.
`_url_has_userinfo` parses with `urlsplit` and only flags a **password** on an http(s)
remote, so the two legitimate `@`-bearing shapes keep working — scp-style
`git@github.com:org/repo.git` (no `://`, so no userinfo) and `ssh://git@host/...` (a username
authenticating by key, not a secret).

`PUT /settings` refuses an unrecognized `mode` rather than falling back — a typo
must not quietly change what the agent is allowed to do.

### The postmortem is a redaction sink

`store.write_log` is a registered `security_posture` redaction sink alongside
`slack_out.py` (the Slack board) and `registry.py` (provider evidence), and it runs
**both** passes: core `security.redact` (credential + exfiltration-URL scanners) and
the app's `secrets.redact_tokens` (provider-token shapes). Both are needed and it is
worth naming why, because picking one looks sufficient: core alone leaves a bare-hex
Datadog key and a prefix-less `Bearer` token in place, and the app pass alone leaves
an AWS access key id. `registry.gather_evidence` composes them the same way, so the
two paths provider text leaves this app sanitize to one standard.

**The pre-push ledger scan follows the same rule.** `ledger_sync._credential_bearing_lines` is
the last gate before bytes leave the machine for the team's shared git remote, and the asymmetry
documented here for the postmortem applies verbatim: the core patterns
(`security.get_credential_patterns()`) miss `ddapp_0123…` while `redact_tokens` misses
`AKIAIOSFODNN7EXAMPLE`, so a scan using one of them would commit and push a legacy row (written
by an older build, or by any path other than `POST /ledger`) carrying a provider token. The scan
flags a line if EITHER detector does, and a structural test asserts both are called so a private
regex copy cannot drift the push guard away from the write path — the silent half of that
failure, where the writer redacts a shape the publisher still ships.

What is redacted: the signal title, id, source and resource (provider text), the
model-authored `diagnosis` and `resolution`, and each matched ledger id. What is not:
the incident id, severity, status, operating mode, `fired_at` and the fingerprint —
our own identifiers, closed enums, a timestamp, and a hash we compute. Running the
scanners over those is a verified no-op.

**What this does and does not promise.** It masks credential *shapes*: recognized
vendor key formats, the carrier forms (`Bearer …`, `token=…`, `DD-APPLICATION-KEY:
…`), and exfiltration URLs. It does **not** understand meaning, so it will not remove
an internal hostname, a customer identifier, a stack trace naming private code paths,
or a secret with no recognizable shape. The artifact is safe to share in the sense
that a credential is very unlikely to ride out inside it; it is **not** a
declassification pass, and an operator sending it outside their organisation should
still read it. That distinction matters here more than at any other sink, because
this is the one output a human is expected to forward by hand.

### Subprocess spawn

`github_issues._run_gh` is the provider layer's GitHub spawn (the app also spawns
`git` for ledger sync and `gh` for the rotation login — see "Windows
compatibility" below) and is routed through
**`sandboxed_spawn_argv`** (OS filesystem isolation + credential-scrubbed env) with
a kernel resource ceiling from `create_subprocess_limited`. The repo, label set, and
comment body all come from agent-influenceable config, and `gh` reads the target
repo's own config on the way — so this is an agent-influenced spawn in the sense
`test/test_spawn_audit.py` polices, and it is routed rather than allowlisted.

That audit also scans test files, so async tests use
`unittest.IsolatedAsyncioTestCase` rather than bare `asyncio.run` (which the
scanner reads as an `asyncio.<spawn attr>` call).

### Webhook ingress

`POST /api/apps/ops-mission-control/webhook` on the authenticated gateway surface,
requiring an HMAC-SHA256 signature (`X-OMC-Signature`) over the raw body keyed by a
keystone secret and compared with `hmac.compare_digest`. **Fail-closed**: not
enabled, or no configured secret, means reject everything. Accepted deliveries land
in a bounded (200-entry) spool. No public ingress or tunnel is shipped.

**A read never consumes the spool — only a claim does.** `poll()` calls `peek()`, and
`dispatch.run_cycle` calls `webhook.ack({claimed ids})` after the claim loop. `poll_all`
has three callers and only ONE of them claims: the heartbeat, `GET /signals` (the Signals
tab's "Poll now"), and the `POST /incident/claim` authorization re-poll. A draining `poll()`
would let the other two destroy delivered alerts outright — an operator refreshing the board
while five Alertmanager alerts sat spooled would see them rendered once and then permanently
gone: signature-verified, 200-accepted, no incident, no trace — and claiming one signal would
discard every *other* queued delivery.

Even the heartbeat cannot safely drain: `run_cycle` claims at most `max_claims` per cycle, so a
burst larger than the cap would have its remainder destroyed by the very poll that delivered
it. Tying consumption to "an incident owns this id" is what makes the cap safe.
`maxlen` still bounds the spool, so a sender that permanently outruns the heartbeat drops
oldest-first, and a read never triggers that.
`webhook.reset_spool()` exists for test isolation only and is named so it cannot be
mistaken for a consumer.

**`ack` ROTATES; it must never `clear()` + re-extend.** `enqueue` runs in a WORKER THREAD —
the webhook route awaits `asyncio.to_thread(webhook.enqueue, ...)` — so ingestion genuinely
interleaves with the heartbeat's `ack`. Building a `keep` list and then `clear()`ing would
destroy anything appended in between — a signature-verified, 200-accepted alert vanishing with no
incident and no trace, the failure class peek/ack exists to prevent — and the race can also raise
`RuntimeError: deque mutated during iteration` out of `run_cycle`, taking the whole heartbeat
cycle with it.

`ack` `popleft()`s exactly `len(_queue)` entries as observed on entry and `append()`s back
the ones it keeps. Bounding the loop to the entry length is what makes it safe: anything
appended while it runs sits behind the rotation window, is never examined this pass, and stays
spooled for the next cycle.

**A single deque op is atomic under the GIL, but the popleft-then-append PAIR is not.** `enqueue` runs in an `asyncio.to_thread` worker
while `ack` runs on the loop, so they genuinely race; at `maxlen`, an `enqueue` landing between
`ack`'s popleft and its matching append fills the deque, and that append then evicts the
oldest — which can be the alert `enqueue` just accepted. So `ack` holds a module-level
`threading.Lock` (`_queue_lock`) around the WHOLE rotation, and `enqueue` takes the same lock
around its `extend`. A plain `threading.Lock`, not the cross-process file lock the store and
ledger use: this is same-process loop-vs-thread contention over an in-memory spool, so there is
nothing on disk to serialize. A test pins both `enqueue` and `ack` holding it.

The test harness hooks `signal.id in signal_ids` — the one operation **both** a rotating and an
iterating implementation perform per entry — not the traversal. Hooking `popleft` alone would
make the test fail on iterating code for the wrong reason ("the interleaving did not happen"):
green on the very bug it names. When a guard must hold across two possible
implementations, hook what they share, not what one of them happens to call.

**And everything an open incident ALREADY owns is acked too.** `owned` ids are filtered out of
`candidates`, so they are never claimed — and if only the `claimed` set were acked they would
never leave the spool either. A sender that redelivers while an investigation is in flight
(Alertmanager repeats every `group_interval`; a webhook script retries) would accumulate copies
of a signal already being worked, and on a full 200-entry spool those would evict a NEW unclaimed
alert. The rule: every place a signal becomes **or
already is** durable has to acknowledge it, not just the place that claims it. Safe by the same
durability argument — an id in `owned` has an incident on disk, so dropping the spooled copy
loses nothing, and once that incident goes terminal or stale the id leaves `owned` and the next
genuine delivery is claimable again.

**Both places a claim becomes durable ack.** `dispatch.run_cycle` and `POST /incident/claim` —
the board's manual claim — both do; otherwise a hand-claimed webhook signal would stay spooled
forever, and on a full (200-entry) spool the next signed delivery would evict the OLDEST
unclaimed entry to make room for a duplicate nobody needed, losing a real alert. The ack there is unconditional and needs no source check — `ack` on
an id that is not spooled removes nothing, so a future push provider is covered for free.

**Check order is load-bearing** (`webhook.enqueue`): enabled → secret → size →
signature → parse. Nothing unauthenticated is ever handed to `json.loads`, and an
oversized body is refused *before* it is hashed. `test_webhook.py` pins the order by
asserting an unsigned malformed body is rejected for its **signature**, not its
syntax.

**The size refusal is a MEMORY bound, which requires streaming.** `enqueue`'s
`len(raw_body) > MAX_BODY_BYTES` check can only run on a body already in memory, and these
routes register on the shared gateway application whose `client_max_size` is 60 MiB (it
carries file uploads) — so `await request.read()` would buffer up to 60 MiB per concurrent
delivery in order to refuse 256 KiB of it. `_read_capped` reads incrementally and stops ONE
byte past the cap, so "exactly at the limit" is still accepted while the refusal peak is
`cap + chunk` rather than whatever the sender chose. `Content-Length` is a fast path when
present (an honest oversized delivery is refused before a single chunk is read); the
streaming count is the authority, so a lying or absent header changes nothing. The tests
assert **bytes actually read**, not the status code — a handler that buffers everything and
then returns 413 passes a status-only test while the exhaustion still happens.

**Every polled source distinguishes "full" from "capped".** This is one class across five
pollers, and the registry's own post-slice check only catches the sources it slices. An adapter
requesting exactly `DEFAULT_POLL_LIMIT` would make `len(result) == limit` ambiguous, `poll_all`
would record `snapshot=True` regardless, and on an estate larger than the cap the omitted
still-firing signals would be terminally resolved as cleared. The detector differs by API, so it lives
in the adapter:

- **CloudWatch** pages via `NextToken` to `limit + 1` (above).
- **Datadog** and **GitHub Issues** fetch `POLL_FETCH_LIMIT` (`limit + 1`) and key off the RAW
  page size, because both filter client-side (Datadog keeps only open monitors, GitHub drops
  assigned issues) so the post-filter count is not the truncation signal.
- **PagerDuty** reads its response `more` flag — 100 is that endpoint's maximum `limit`, so a
  `limit + 1` request would be clamped and read back as a full page.
- **incident.io** follows the `pagination_meta.after` cursor and derives the verdict after the
  walk from the final count, because a page can both overshoot the cap and be terminal; a page
  ceiling (`_MAX_ALERT_PAGES`) refuses a cursor walk that never terminates rather than reporting
  a partial estate as complete.

All five return `providers.base.TruncatedSignals` (a `list` subclass) when the source had more
than a poll can carry, and `poll_all` marks the poll non-authoritative — the same
`snapshot=False` channel, honoured even when a client-side filter brought the surviving count back
under the cap.

**A capped poll is not a complete snapshot.** `poll_all` slices each source's result to
`limit`, and the omitted signals are simply ABSENT from that poll — which for a snapshot source
is how `reconcile` and `verify_pending_actions` infer recovery. So for a provider returning
`limit + 1` firing alarms, the surplus would verify as CLEARED while still firing, and `resolved`
is terminal. The cap stays (it bounds memory and prompt size); truncation reports
`snapshot: False` through the channel built for the webhook spool's drain, because it is the same
fact — this poll did not see everything — plus a `detail` string so the operator sees the cap was
hit. Carried as `providers.base.TruncatedSignals`, a `list` subclass, so every existing consumer (`extend`, `len`,
iteration) is unchanged and only the health builder checks the type.

**`describe_alarms` is PAGED, because one page is not the estate.** The same rule one layer
deeper: reading only the first page would stop at `MaxRecords` with nothing indicating more
existed, so an account with more firing alarms than the cap would under-return while the registry
recorded `snapshot=True` — and the omitted live alarms would be terminally resolved. The
registry check above only catches truncation *we* perform; here every call succeeded and the provider simply had more.
Paged to `DEFAULT_POLL_LIMIT + 1` rather than to exhaustion (one item past the cap is all the
registry needs to flag the poll; draining an unbounded estate would trade this failure for the
memory/rate-limit one the cap prevents), bounded by `_MAX_ALARM_PAGES = 20`. Bounding out with
pages still pending and UNDER the cap RAISES instead of returning short: nothing downstream would
notice that shortfall, so the honest answer is the one `poll_all` already renders as "cloudwatch
did not answer".

**A full spool is REFUSED, not silently evicted.** The spool is a
`deque(maxlen=MAX_QUEUED_SIGNALS)`, so `extend` past capacity drops the OLDEST entries. Left to
that, a burst would give every sender HTTP 200 while the earliest accepted alerts were discarded
before any dispatch cycle claimed them — an alert paged, acknowledged as received, and never seen
again. Bounding the spool is right; lying about the outcome is not. `enqueue` checks capacity
**inside `_queue_lock`** (checking before acquiring would be a TOCTOU where a concurrent delivery
fills the last slot between the test and the extend, i.e. the very eviction being prevented) and
refuses the WHOLE batch, so one Alertmanager fan-out stays atomic rather than partially accepted.
The route answers **503 with `Retry-After: 120`** (one dispatch interval, when the spool next
drains): a 4xx would tell a sender to stop, but the delivery was well-formed and trusted — we are
the ones who cannot take it — and senders retry on 5xx, so a full spool becomes a delay instead of
a lost page.

**Two accepted envelope shapes** (`signals_from_payload`), with the check order above
untouched:

- **Alertmanager / Grafana v4** — `{status, alerts: [...], commonLabels, ...}`. Each
  entry of `alerts` becomes its **own** Signal: a raw Alertmanager body carries no top-level
  `title`/`summary`, only `annotations`/`labels`, and Grafana *does* send a top-level `title`,
  so a top-level-only reading would reject the first and collapse the second into **one** board
  row, losing every per-alert instance in a group. Title falls back `annotations.summary` →
  `description` → `labels.alertname`; resource from `instance`/`job`/`pod`; url from
  `generatorURL`; `commonLabels` merge *under* per-alert labels; Grafana's per-alert
  `values` (the actual breaching numbers) are kept as a label, which is free evidence
  for a source that otherwise arrives with none. The provider's own `fingerprint`
  becomes `provider_key` (contract 1a) and the `native_id`, so re-deliveries dedupe.
  Fan-out is bounded by the existing `MAX_QUEUED_SIGNALS`, and one malformed entry is
  skipped rather than failing the whole delivery — one bad alert in a group of forty
  must not discard the thirty-nine that are fine.
- **The flat native envelope** — unchanged, so every existing sender keeps working.

**A sender can report a clearance.** The payload's `state`/`status` key is read; a literal
`STATE_FIRING` would let a sender create work but never retract it, leaving reconcile to infer
recovery from absence, which is the inference that closes live work when a poll fails. Read
through `normalize_state`, so an unrecognized
value becomes `unknown` and cannot manufacture phantom firing work — and a *recognized
suppression* vocabulary becomes `suppressed` rather than `unknown` (contract 1b), read from
either the v4 scalar `status` or the v2 status OBJECT with its `silencedBy`/`inhibitedBy`.

**Shape is necessary, not sufficient, for those two senders.** Alertmanager's
`webhook_config` supports basic/bearer/authorization headers but **cannot** HMAC-sign the
raw body, and Grafana signs into `X-Grafana-Alerting-Signature`, not `X-OMC-Signature`. So
neither works out of the box against this fail-closed ingress without an accepted
signature-header list or a bearer verification mode — a **security posture decision,
deliberately not made here**. Both are reachable today via any forwarder that can sign.

**Rejection status codes are differentiated** (`_webhook_reject_status`): 401 for
trust failures (not enabled / no secret / signature mismatch), 413 for an oversized
body, 400 for payload faults (malformed JSON / non-object / no title). A single 401 for everything
would tell a sender debugging a bad payload "Unauthorized", so they would re-check credentials
that were fine, while a real signature failure would look identical to a typo. An *unrecognized* reason deliberately falls
through to **401**, not 400 — a refusal we cannot classify should not be advertised
as "your request was fine". A test derives the reason set from `enqueue`'s source, so
adding a rejection without classifying it fails CI.

**Two input-handling rules this ingress depends on**, both pinned by `test_webhook.py`:

- Labels are type-checked before they are iterated. A type check placed in a comprehension's
  `if` clause runs *per item*, after `.items()` has already been called on the raw value, so a
  payload with `"labels": "text"` would raise `AttributeError`, escape `enqueue`'s `except`
  (JSON/Unicode only) and **500 the ingress** — a correctly-signed sender crashing the endpoint
  with one malformed field. `_normalize_labels` guards the type first and caps key/value
  lengths and pair count (`MAX_LABELS`), since labels reach the model's context and the
  fingerprint.
- `KeystoneFileBackend` resolves `secrets_path()` per access (an explicitly-passed path is still
  pinned). The backend is a module-level singleton, so a path snapshotted in `__init__` would
  freeze the data home at import and make the whole process share one secrets file — silently
  defeating per-test home isolation, so that "no secret configured must reject" could pass only
  because a sibling test had written a secret. This is a **testability** rule with a security
  consequence: it is what makes the fail-closed assertion protecting this endpoint test anything.

## Tier model

| Tier | Gate | Armed by | Crons |
|---|---|---|---|
| `always` | always armed — **never pausable** | nothing (ships enabled) | `ops-mission-control/rotation-check` (5m) |
| `on_shift` | `RotationSource.on_shift()` | `POST /rotation/arm` | `ops-mission-control/dispatch` (2m), `.../reconcile` (15m) |
| `primary` | `rotation.is_primary()`, enforced in `POST /ledger/hygiene` (409 `not_primary`) | nothing (ships enabled) | `ops-mission-control/ledger-hygiene` (daily 03:17) |

`reconcile` is on `on_shift`, not `always`: it POSTs `incident/transition` and edits Slack
messages, so on a team every instance would race to resolve the same incidents. The
"Armed by" column is load-bearing — only one tier is scheduler-armed, and the other two are
gated elsewhere or not at all.

**Cron names are the namespaced ones the scheduler actually registers**
(`<app-name>/<manifest cron name>`), NOT bare `omc-*`. A bare name matches no
registered job, so every pause/resume the tier mechanism emitted would silently target nothing
and tier arming would be entirely inert. `test_tier_cron_names_match_the_manifest` derives the expected set from
`app.json`, so adding or renaming a manifest cron fails the suite instead of quietly
breaking arming.

`rotation-check` lives on the `always` tier by necessity — on the gated tier an
off-shift instance could never re-arm itself (`test_store_and_gate.py` asserts this).

**Only an `on_shift` cron may ship paused.** Nothing in the codebase flips a manifest
`enabled: false`, and `POST /rotation/arm` arms *only* the `on_shift` tier — it cannot
pause an `always` job and deliberately leaves `primary` alone (see "Arming is
server-side"). So a cron on any other tier that ships disabled stays disabled **forever**.
"It must not fire before a provider is configured" is answered by the step-0 cheap exit, not
by shipping paused.

What ships enabled is therefore exactly the non-`on_shift` crons: `rotation-check` (`always`)
and `ledger-hygiene` (`primary`), each with an explicit step-0 guard (`configured=true` → else
`NO output`), so a fresh install still pays nothing. `ledger-hygiene` is the ONLY caller of the
git ledger sync, the vector-index import, and closed-incident pruning, so a paused one would
leave all three dead in production. `reconcile` and `dispatch` are `on_shift` crons
(`rotation.TIER_CRONS`): they ship paused (`enabled: false`) and `POST /rotation/arm` arms them,
because `reconcile` POSTs `incident/transition` and edits the incident's Slack message, and on
a team every armed instance would race to do the same. Two tests pin the rule generically —
every non-`on_shift` cron must ship enabled, and every *enabled* cron must carry the cheap
exit — rather than naming one job.

### Rotation without a rotation service (`providers/schedule_file.py`)

A team with PagerDuty has a rotation API. For everyone else the schedule is
`rotation.yaml`, committed to the **same git repo the ledger syncs through**, with
identity resolved from the operator's **GitHub login** — read from the KEYSTONE
(`policy_store.SCHEDULE_LOGIN_KEY`, never `config.json`; see above for the forgery that
required), else the local `gh` CLI, cached per gateway lifetime so a rotation tick every 5
minutes does not re-spawn `gh` forever — including on a cache *miss*.

```yaml
timezone: America/Los_Angeles     # optional; UTC when absent
shifts:
  - from: 2026-08-01
    to: 2026-08-08
    who: octocat                  # scalar or list (co-primary allowed)
```

Reusing the ledger transport means no second integration and no second credential: a
shift swap is a reviewable diff, and the schedule arrives on the same pull that brings
teammates' lessons. `ledger_sync`'s generated `.gitignore` therefore un-ignores
`rotation.yaml` alongside `ledger.jsonl` — a schedule that never syncs is *worse* than
none, because it looks configured while disagreeing with everyone else.

Sharing the transport also means sharing its branch, and that is why bug 5 above mattered
here specifically: both the ledger **and** `rotation.yaml` publish through a refspec, so a
HEAD sitting on an unconfigured branch left the operator unable to `git pull` in the one
directory where a *refused* conflicted schedule has to be resolved by hand. The exchange
looked healthy the whole time.

Two decisions worth keeping:

- **A date-only `to` means through the END of that day.** `to: 2026-08-08` read as 00:00
  would silently drop the last day of every shift written date-only — the most likely
  misreading of this format, so it is handled in the parser.
- **It arms tiers; it never grants authority.** Any teammate can push a schedule, so it
  is wired to the cheap decision (when to look) and not the expensive one (what to do).
  `effective = min(app_mode, rule_mode)` still governs every action.

Every degradation — missing file, invalid YAML, unresolvable login, expired schedule,
reversed window — resolves to `unknown=True`, which the tier gate treats as ARMED. The
file arrives by `git pull` from a shared repo, so it is untrusted input: size-capped
(256 KB), shift-count-capped (5000, and it *logs* when truncating), and parsed with
`yaml.safe_load` — asserted by a test that greps for `yaml.load(`.

**The always-on default never masks a real rotation.**
`AlwaysOnRotationSource` is always configured and always on-shift, and `resolve_shift`
returns the first on-shift answer — so consulted like any source it would discard a real
source reporting "someone else is on call" and arm the `on_shift` tier permanently for
everyone, which is precisely the failure a rotation exists to prevent. Fallbacks declare
`is_fallback = True` and are consulted **only when no real source can answer**. Pinned by
`test_the_always_on_default_does_not_mask_a_real_rotation` plus a companion test that the
floor still arms a solo operator.

### Arming is server-side: `POST /rotation/arm`

**The agent does not choose which crons to pause, and no longer holds `cron_pause` at
all.** `rotation.apply_tiers(shift, cron_service)` computes the tier map and moves the
app's jobs itself; the rotation-check cron just POSTs `/rotation/arm` and reports whether
`changed` came back non-empty.

This replaced an agent-driven loop, and the reason is worth keeping: off shift the armed
set still legitimately contains `ops-mission-control/rotation-check`, because that is an
`always` job — and it is the *only* job that can re-arm a gated instance. An agent told
to "pause the armed crons" would pause exactly the cron that turns the instance back on,
silently ending the team's incident response until a human noticed. The only thing
preventing it was a sentence of SOP prose saying "never pause an always-tier cron".

Prose is not an enforcement mechanism, and neither is the manifest: `permissions.mcpTools`
is **declarative only** — `check_tool_permission` exists but nothing calls it at runtime —
so listing `cron_pause` there never gated anything either. One misfollowed LLM turn was
the whole distance between "armed" and "permanently dead", which is the same
quiet-versus-broken conflation this app refuses everywhere else ("a source that failed is
shown as failed, never as quiet").

So the decision moved into code the model does not mediate:

- `protected_cron_names()` is **derived** from `TIER_CRONS[always]`, not restated — a job
  that moves onto the always tier is protected by that move alone. A second hand-kept list
  could protect the old name and forget the new one.
- `apply_tiers` skips a protected name unconditionally, even if the tier map says to pause
  it. That branch is unreachable through `tier_states` today (`always` is hardcoded `True`)
  and is deliberately still there and still tested: it is the invariant, not a consequence
  of how one caller happens to be written. An `on_shift or unknown` fail-open in
  `tier_states` would defeat strict gating, which is the class this guards.
- Jobs outside `TIER_CRONS` are never touched, so a user's unrelated paused cron is not
  resumed as a side effect of a shift starting.
- `cron_pause`/`cron_resume` are removed from `app.json`, with a test pinning their absence
  and another pinning the cron prompt to `/rotation/arm` — so a future SOP edit cannot
  quietly hand the capability back.

`GET /rotation` still returns `armed_crons` (flat union across every armed tier — "what is
running now") and `tier_crons` (the per-tier breakdown) for explaining a transition to the
operator. It is read-only context, not a work list.

### The chat-slot key is derived, not trusted

Same objection, one path over: a prompt sentence ("EXACTLY `ops-mission-control-<incident_id>`
… any other key leaves the user watching an empty conversation") cannot enforce a slot key, and
a misfollowed turn would produce an incident whose panel silently shows nothing — a failure with
no error anywhere, which is the shape this app treats as a defect.

`canonical_slot_key(incident_id)` (in `backend/http_routes/board.py`) computes it and **no
resolution path reads `incident.slot_key`**: the frontend derives the key from the incident id
(`IncidentChat.incidentSlotKey`) and never reads the field, and both backend call sites use this
exact expression. The field stays on the record
for forensics — what the agent *reported* using is worth having when a panel came up empty —
but nothing resolves a slot through it. Two tests: one pinning the backend expression against
the frontend's own literal so the two cannot drift, one structural (`inc.slot_key` appears
nowhere in a resolution path).

**Fail-open:** `ShiftStatus.unknown` (no source, or every source errored) arms the
`on_shift` tier. Wrongly arming costs API polls; wrongly disarming means nobody
notices an outage.

With the default `always-on` rotation source, `on_shift` is permanently armed, so a
solo operator gets continuous coverage rather than a tier that never fires.

## Dispatch engine (`backend/dispatch.py`)

`run_cycle()` is the loop that makes the app function. It is **deterministic
Python called once by the cron**, not an agent turn — the expensive part (an
actual investigation) is reserved for signals that need one, which keeps the
heartbeat's cost flat at a 2-minute cadence.

1. Rotation gate: off-shift returns immediately with `skipped_reason`. This is
   checked here as well as by the cron tier, so a manual trigger cannot dispatch
   off-shift.
2. `poll_all` across configured sources; drop `state != firing`. Three distinct reasons a
   signal is dropped, and they are not interchangeable: it is `ok` (the provider reports
   recovery), it is `suppressed` (a human parked it — **counted** in
   `CycleResult.suppressed` and deliberately never claimed, see contract 1b), or it is
   `unknown` (we could not read its state).
3. Diff against the dispatch index by `Signal.id` (a `stale` incident is
   re-claimable).
4. Claim up to `max_claims_per_cycle` (default 3) — `store.claim` takes an
   exclusive `platform_compat.file_lock` and compare-and-sets, so exactly one
   caller wins and the loser skips. The cap turns an alarm storm into a queue that
   drains over successive cycles instead of spawning 200 sessions.
5. **`attach_ledger_matches`** — match `Signal.fingerprint` against the ledger,
   record the use, and persist `ledger_matches`. This is the step that makes the
   second occurrence of a failure cheaper than the first; without it the ledger is
   decorative. `record_use` returns the UPDATED entry so a brief cannot report
   "used 0×" for a pattern the same incident just used.
5b. **`webhook.ack({claimed ids})`** — the ONE place the push spool shrinks, and only for
   ids that have an incident on disk. Anything the cap deferred, anything already
   owned, and anything lost to a claim race stays spooled for a later cycle. See § Webhook ingress
   for why draining on `poll` loses data.
6. **`verify_pending_actions`** — for every incident whose post-action recheck has come
   due, re-read whether its signal is still firing, using the poll THIS cycle already
   made (so no extra provider call) and the same `poll_health` map. A source that did not
   answer yields `unknown`, never a success. See contract 3b; a `still_firing` verdict
   charges a `miss_count` to every entry the incident cited, which is the join that makes
   `use_count` mean "worked".
7. Sweep incidents idle past the window back to `stale` for re-pickup.
8. **If nothing changed, `CycleResult.changed` is false and the cron emits
   nothing.** Silence-by-default is a hard requirement, not an optimization — it
   is why the modeled channel stayed readable. `changed` counts a `still_firing`
   verification (the app retracting a claim it made) and deliberately not a `cleared`
   or `unknown` one.

`investigation_brief()` renders the claim's facts (signal, mode, matched patterns,
fast-path flag, authority reminder) deterministically, so an investigating agent
does not spend its first turn re-fetching context Python already has. A match with a
non-zero `miss_count` carries an explicit `WARNING` line naming how many times the fix was
applied while the signal kept firing — stated separately because a ranked list reads as an
endorsement, and an agent told only "used 4×" reads the count as corroboration when part of
it is the record of the fix not holding.

`POST /dispatch` is the endpoint the cron calls; the board's **Check now** button
calls the same cycle so a user who just entered a token can verify it immediately
rather than waiting a heartbeat to discover a typo.

### A fresh install explains itself

`run_cycle` returns early with a `skipped_reason` when **no signal source is
configured**, before polling. `polled == 0` is ambiguous — "nothing is wrong" and
"nothing is watching" are opposite conclusions, and a new user's very first action is
the moment the app most needs to admit it is not set up. The skip reason is what an agent
calling `POST /dispatch` on a fresh install sees, instead of a silent empty result.

`configured_signal_sources()` treats a `configured()` that **raises** as not
configured: an adapter whose own readiness check is broken cannot be trusted to poll,
and counting it as ready converts "nothing is watching" into a source-level error every
cycle — noise the operator cannot act on.

On a genuinely empty data home the handover digest leads with "the board is quiet because
nothing is being watched", dispatch is silent (`changed: False`) but says why, and a configured
install still polls normally.

## Incident status is derived, not stored

`backend/slot_watch.py` reconciles each open incident against its investigation
slot on every `/state` read:

| Slot state | Status | `blocked_reason` |
|---|---|---|
| pending approval, **or** a trailing `permission` message | `needs_human` | `awaiting_approval` |
| waiting for input | `needs_human` | `awaiting_input` |
| running | `investigating` | — |
| idle, turns taken, no diagnosis recorded | `needs_human` | `awaiting_diagnosis` |
| no slot | unchanged | unchanged |

**Derived rather than stored** so it cannot go stale: approving from the embedded
chat clears the block on the next read, with no flag anyone has to remember to
reset. The trailing-`permission`-message check matters because the slot's
`pending_approval` flag LAGS the transcript — relying on the flag alone leaves the
board wrong for that gap.

**Read through the slot's PUBLIC contract.** `_slot_state` (`backend/http_routes/board.py`) asks
`_ChatSlot.to_dict()`, which the core keeps correct, rather than deriving
`pending_approval` from `slot._approval_futures` itself: a private attribute of another module
is not a contract, so a core
refactor renaming it would silently turn *"waiting on you"* into *"progressing"* on this
board — the operator stops being told an incident needs them, and nothing anywhere fails
to say so. A serializer fault degrades to the public `pending_approval` attribute rather
than 500ing, because this read paints the whole board; the fallback is deliberately not
the old private access. Pinned by a test that drives the real `_ChatSlot` through
baseline → pending → resolved and asserts our answer equals the core's at each step.

`dispatched → needs_human` is a legal edge because an agent can block on its very
first action (observed live: the opening move was a read-only AWS probe that parked
on an approval). `needs_human → stale` is legal too, so an unanswered incident does
not pin its signal as claimed forever.

The board renders `blocked_reason` INSTEAD of the bare status ("Approve to
continue", "Waiting on you", "Stopped, no diagnosis") because "Needs human" reads
identically whether the agent wants one click or has run out of ideas — and the
operator's next action differs completely.

**Phase 4 closes the loop.** `awaiting_diagnosis` clears only when the
investigation records its finding via `POST /incident/transition` with a
`diagnosis`. The investigate SOP carries that call verbatim, and the dispatch
kickoff names it as mandatory: an agent that writes its analysis in chat and stops
leaves the board reporting a finished investigation as a dead end.

## Embedded incident chat

The board's expanded row mounts `ChatEmbed` against the incident's slot
(`ops-mission-control-<incident_id>` — the SOP and cron prompt both state the key,
since a mismatch shows an empty conversation beside a live investigation). It needs
its own `AppApiProvider`: builtin pages have none, and the provider is
permission-scoped, so `/api/chat*` **and** `/api/approvals*` must both be in
`allowedApiPaths` or the approval buttons 403 with no visible error.

Approvals from an embed depend on three core behaviours, all upstream of this app:

- `ChatEmbed` passes `onApprove`; without it approval cards would render with buttons that do
  nothing and an embedded agent would stall forever behind an interactive-looking card.
- `CollapsibleToolGroup` renders the approval row (preview + buttons) in both disclosure
  states, because a group with a live pending approval auto-expands and the one turn waiting
  on the user must be answerable. Pinned by
  `website/src/test/collapsibleToolGroupApproval.test.tsx`.
- A **failed** approval does not render as "Approved". `submitDecision` optimistically
  flips the card and relies on the promise returned by `onApprove` to reject so its catch
  can restore the buttons. `ChatEmbed` returns `approveMutation.mutateAsync(...)`, and
  `ChatMessageListProps.onApprove` requires `Promise<unknown>`, so a failed POST reaches
  that rollback instead of leaving the agent parked behind an undelivered decision. Pinned
  through the real message-list and tool-group chain by
  `website/src/test/ChatEmbed.approvalRollback.test.tsx`.

Layout: the embed scrolls via `h-full` + an inner `flex-1 overflow-y-auto`, so an
ancestor MUST bound its height (`IncidentChat` owns a fixed-height flex column with
`min-h-0`). Without the bound the transcript grows without limit and pushes the
input row out of reach; without `min-h-0` a flex child's default
`min-height: auto` refuses to shrink below content and silently defeats the
overflow.

## Slack output — the pin board (`backend/slack_out.py`)

Mirrors incidents to a Slack channel as a **board, not a feed**: one message per
incident whose glyph tracks its state (`⏳` dispatched, `🔍` investigating, `🧑`
needs human, `✅` resolved, `🚨` escalated, `💤` stale), with detail in its thread.
This makes an ops channel the live dashboard. Emoji is correct here and only here — the no-emoji rule in
`website/AGENTS.md` governs rendered dashboard UI, and `slack/blocks.py` already
uses them.

### Slack-bound text passes BOTH redaction passes

`slack_out._safe()` is the single chokepoint for everything outbound and applies
`security.redact` **and** `secrets.redact_tokens`. Neither is a superset of the other:
`redact` knows AWS keys and exfiltration URLs, `redact_tokens` knows the PROVIDER-specific
token shapes this app handles.

A Datadog app-key shape and a PagerDuty `u+` token both pass through `redact` **completely
unchanged** and are masked only by `redact_tokens`, so a single pass would republish a
provider-authored alarm title carrying a key into the channel verbatim. A test using only an
`AKIA` key, which `redact` *does* catch, would stay green either way.

One function rather than three call sites: three sites each remembering two passes is three
chances to forget the second. A test asserts the only
`redact(` in the module is the one inside `_safe`, and the token tests assert their own
premise (that core `redact` really does miss these shapes) so they cannot quietly become
vacuous if `redact` later learns them.

**`_safe` also mrkdwn-escapes.** Every string reaching it is content this
app does not control — an alarm name, a GitHub issue title, an HMAC-signed webhook body —
rendered into a Slack message as mrkdwn. Unescaped, a title of
`<https://attacker.example|runbook>` would paint an attacker-chosen hyperlink into the team's
incident channel, labelled however the attacker liked. Redaction does not help: the payload
contains no credential.

Exactly the three characters Slack's own rules name (`&`, `<`, `>`), with `&` first so the
ampersands the other two introduce are not re-escaped. `*`/`_`/backtick are deliberately NOT
escaped: they affect only emphasis, cost readability on every ordinary title containing an
underscore, and cannot fabricate a link or a mention — the actual harm.

**Two positions, two chokepoints.** `signal.url` goes through `_safe_link_target()`, not
`_safe()`, because it lands in the TARGET half of `<target|label>` where escaping `<`/`>` would
corrupt the URL while doing nothing about what actually matters there. It redacts (a console
link or signed webhook URL can carry a token in its query string), requires `http`/`https`, and
refuses any value holding `|`, `<`, `>` or whitespace rather than trying to repair it —
otherwise a URL of `https://x|label> <https://attacker.example` closes our own link and opens
one the attacker controls. An unusable URL yields `""` and the link is omitted rather than
rendered broken, matching the dashboard, which drops a `javascript:` signal URL via
`lib/safeUrl.safeHttpUrl`. The chokepoint guard permits exactly these two `redact(` calls
and the per-field guard accepts either, so a third bare call still fails.

**A URL is not exempt.** `signal.url` is interpolated into a Slack block, and a signed webhook
or console link can carry a token in its query string. A guard test that asserted only that no
bare `redact(` remained would not notice a field that skipped the chokepoint, so the test
asserts on the fields, which is the property that actually matters.

### Text this app SENDS to a provider has the same floor

`routes._safe_outbound()` = `redact_tokens(redact_via_context(...))`, applied to every action
note before it reaches an `ActionSink`. A note becomes an acknowledgement comment, a resolve
reason or a mute note **on someone else's system**, where we cannot unpublish it — and it is
agent- or operator-authored free text, so an agent that pasted a provider token into its
diagnosis would publish that token into the provider's own comment thread; this third outbound
surface carries the same floor as the Slack sink and the ledger write path.

Redaction happens **before** the `_MAX_NOTE_LEN` clip: truncating first can sever a token so
the pattern no longer matches, whereas clipping after masking only ever shortens a
placeholder. Pinned by a test that asserts the ordering, not just the presence of the call.

**`diagnosis` and `resolution` get the same floor**, and did not. They are agent-authored free
text that `POST /incident/transition` persists and this app then renders on the board, in the
handover digest and in the Slack mirror — so an investigating agent that pasted a provider
token into its writeup stored that token in the incident index and painted it on the dashboard.
The action-note, Slack and ledger sinks were covered while this fourth one was not. Found in
review. `slot_key` and `slack_thread_ts` on the same request are deliberately NOT redacted:
they are machine ids, shape-checked downstream, and masking one that happened to match a token
pattern would corrupt it. The test asserts on what `store` actually holds rather than on the
response, because the durable copy is what every other surface reads.

`redact_via_context` rather than `security.redact` directly, for the reason the ledger write
path documents: a loaded companion's declared patterns apply, and an enterprise host that
fails to compose its companion fails CLOSED rather than silently falling back to public
patterns.

**One floor, defined in `routes.py`.** The ledger write (`POST /ledger` redacts pattern and fix
before the content-addressed id is computed) goes through the same `_safe_outbound`: it is the
identical two-pass composition, so one definition serves the action note, the approved
proposal's stored note, the transition's `diagnosis`/`resolution` and the ledger entry. It stays
in `routes.py` because that module is the registered `security_posture` redaction sink for the
ledger write, and the call-site guard classifies redaction by module; the projections that
redact receive it from the facade as a seam, and none of them names a redactor itself (a
composition test pins that no `http_routes` module matches the guard's pattern).

### It stores no Slack credential — by design

The app has **no** bot-token field and adds nothing to its keystone secret store.
Kiro Crew already holds a Slack token for its own gateway, and the live
`SlackClientOps` is reused. Governance guidance on credential storage puts "prefer
no secret to rotate" ahead of storing a third-party token, and permits the latter
only where no such path exists; here one does, so a second copy would be duplicated
credential material with a second rotation obligation and a second thing to leak,
for zero capability gain. `test_slack_out.py::TestNoTokenOfItsOwn` pins this against
a future "just add a token field" regression.

The consequence is a real dependency rather than a hidden one: with Slack
unconfigured on Kiro Crew itself, this channel is unavailable, and `status()`
distinguishes the three cases (off / no channel / no host Slack) because each has a
different fix. The channel ID **is** stored in plain app config — it is not a
credential.

### Explicit client, no global

There is no module-level gateway-state accessor in Kiro Crew (state is per
`web.Application`), so the client is threaded in from the route layer:
`_slack_client(request)` (`backend/http_routes/_shared.py`) → `slack_out.client_from_state(...)` →
`publish/post_detail/publish_all(..., client)`, and `dispatch.run_cycle(
slack_client=...)`. `None` is always a quiet no-op, which is what lets every send
be tested without a gateway.

### Invariants

- **Never fatal.** Every send is wrapped; a Slack outage must not fail the dispatch
  cycle or the transition that triggered it. Notifying is not the work. Sends happen
  *after* the claim/transition is durable.
- **Edited in place.** The first post records `slack_thread_ts` on the incident;
  later changes `chat_update` that message. If the update fails (message deleted,
  channel changed) it **reposts** rather than going silent — a duplicate line is
  cosmetic, a missing alarm is not.
- **Redacted.** Titles, resources, and diagnoses pass through both `security.redact`
  and `secrets.redact_tokens`, plus mrkdwn escaping (`slack_out._safe`, with link
  targets through `_safe_link_target`), before leaving. This is a separate egress boundary from `slack/handler.py`: the
  text originates in a third-party alarm payload rather than a model turn, and the
  channel audience is usually wider than the dashboard's. Registered in
  `security_posture._REDACTION_SINKS`.
- **Blocked reason beats bare status** in the summary line, for the same reason the
  board shows it: "Needs human" does not say whether a click or a decision is wanted.

Wired at three call sites: new claims in `dispatch.run_cycle`, manual claims in
`_handle_claim`, and every status change in `_handle_transition` (which also threads
a new `diagnosis`/`resolution` into the thread). The investigate SOP therefore tells
the agent **not** to hand-post to Slack — doing so would duplicate the finding.

## Local notifications (`backend/notify_out.py`)

The second output channel, and the only one that needs **no credential and no inbound
URL**. `app.json` declared the `notification` event permission from the app's first
commit and the app produced nothing, so this was inert machinery: every operator-facing
fact the app computes required an open dashboard tab or a Slack workspace it holds no
token for.

**Three declared channels** (`app.json` → `notifications.channels`; the cap is 8). This
is a manifest contract — `register_builtin_apps` persists `app.json` into the data home
and the bus refuses an id the persisted manifest does not declare:

| id | default priority | fires on |
|---|---|---|
| `waiting-on-you` | `critical` | the transition INTO `needs_human` |
| `source-health` | `default` | the poll where a source flips ok → failing |
| `incident-released` | `passive` (24 h TTL) | each id `sweep_stale` released |

`critical` on `waiting-on-you` is defensible — it is the one state in this app that
blocks an agent turn — but it is **not** `system.approval` and is deliberately NOT in
`notifications.settings.PROTECTED_CHANNELS`: an app must not be able to hand itself a
channel the operator cannot mute.

**In-process, and therefore both HTTP guards replicated.** `POST
/api/notifications/push` is unreachable here, twice over. It authenticates with an app
token whose secret lives at `~/.kiro/crew/apps/<name>/.app_secret`, and
`register_builtin_apps` writes that file only for a manifest declaring
`backend.entryPoint`; this app declares `backend.routes`, so no secret exists (verified
on disk — `dev-fleet`/`file-explorer`/`workflows` have one, this app does not). And even
with a secret, a handler that HTTP-calls its own gateway needs an auth token and can
deadlock the loop — the same reason `_slot_state` and
`slack_out.link_thread_to_investigation` read through `DashboardState`.

So `notify_out._push` re-implements what the handler owns, in the handler's order:
enablement (`is_app_enabled`) → the channel is declared in the installed manifest → lazy
one-time `register_channel` → `payload.validate()` **before** the limiter (an invalid
payload delivers nothing and must not drain the budget) → `state.notification_rate_limiter`
→ `bus.push`. The limiter is the **state-owned instance**, not a fresh one, so the
in-process path and any future HTTP push share one 30-per-300 s budget instead of two. A
local-first app must not gain an unthrottled notification path.

**One push per STATE CHANGE, never per tick.** `dispatch.run_cycle` snapshots
`registry.poll_health()` **before** `poll_all` and diffs; a source that was already
failing pushes nothing, because at a 120-second heartbeat an hour of downtime would
otherwise be 30 identical toasts — the unchanged condition `SKILL.md`'s noise discipline
forbids. A source absent from the *before* map counts as "was ok" on purpose: its first
failure is news, and it is usually a provider the operator has just configured.
The transition handler (`_handle_transition`) captures the pre-transition status for the same reason —
`update_fields` re-enters `transition` with the same status.

**Nothing is pushed on a claim.** A claim is the heartbeat working correctly and already
shows on the board and in Slack; notifying it would make this the heartbeat feed the
design refuses. Recorded in the module docstring and pinned by a test so it does not read
as an omission to a later reader.

**`group_key`** is the incident id (the source id for `source-health`, since consecutive
failures of one source are one condition), so repeats collapse into one feed row instead
of a column of near-identical notes. **`url`** is `/ops-mission-control` — path-only,
which is all `bus._validate_internal_url` accepts, and deliberately not a per-incident
deep link: the page selects an incident from React state and reads no query parameter, so
`?id=` would promise a jump the UI cannot make.

**Default off.** `notify_enabled` absent reads as False, so every existing install stays
silent until an operator flips the toggle. Not a credential, so it lives in plain
`config.json` alongside the Slack channel id.

**Redacted at the producer, both passes**, matching `store.write_log`,
`registry.gather_evidence`, and `slack_out._safe`. Core `security.redact` leaves `401 from https://api.datadoghq.com?api_key=<hex>`
untouched and `secrets.redact_tokens` catches it. `DashboardState._deliver_note` also
redacts centrally, so this is belt-and-braces — and it is what earns the row in
`security_posture._REDACTION_SINKS`, which is mandatory rather than optional: the posture
drift guard walks every module matching the redactor regex and fails on one that is
neither a registered sink nor allowlisted.

`dispatch._safe_field` (the investigation brief's provider-metadata floor) makes `dispatch.py` a
redaction call site, so it carries its own `_REDACTION_SINKS` row; that is the guard's whole
point — a new redaction site is a new
output boundary, and the posture panel must count it or it reports a smaller surface than the
app actually has.

**Builtin manifests carry `notifications` through discovery.** `notifications` is a
`_KNOWN_FIELDS` member, so it does not survive in `manifest.extra`, and
`apps/discovery._manifest_to_builtin_dict` copies it explicitly. `register_builtin_apps`
persists that dict as the app's on-disk `app.json` and `get_app_manifest` reads that file, so
without the explicit copy a builtin's declared channels would be silently dropped for every
consumer: `_resolve_app_channels`, `GET /api/notifications/channels`, the Settings rail, and
this app's own manifest check.

**No persisted-schema change.** Nothing new is stored on an `Incident` or a
`LedgerEntry`; the only new persisted key is the `notify_enabled` config flag, whose
absence is False.

### Where an operator sees it

`/state` returns `notify` beside `slack` (readiness depends on live gateway state, not
config alone, so it cannot be answered from the unauthenticated config file). The Settings
tab's "Desktop notifications" card owns the app-level on/off and lists the DECLARED
channels with what each fires on.

It deliberately has **no per-channel mute**: Kiro Crew renders that centrally (`GET
/api/notifications/channels` → `pages/settings/NotificationsPanel.tsx`, one row per
channel with a mute switch and a priority override, grouped under an app-badged header),
and a second copy would be two controls that can disagree about one stored setting. The
card names that location instead — and lists the declaration precisely because the central
rail cannot: it shows channels that are *registered*, and registration is lazy, so a
freshly installed app appears there only after its first notification fires.

## Shift handover (`backend/handover.py`)

`GET /handover` returns a digest of what an incoming responder needs: the one-line
headline, work **waiting on a person**, work that stopped without recording anything,
recurring patterns ranked by `use_count`, and which sources are **not** configured.
Plus a pre-rendered `text` field, so a Slack paste and the Handover tab cannot word the
same shift differently.

This replaces the hand-maintained handover document — among the most-used artifacts a
rotation has, and one that costs hours of upkeep and goes stale between edits.
Everything *generic* in it was already data this app owns; the ledger ranked by
`use_count` **is** the "recurring issues by frequency" section, because that count is
the only honest frequency signal (it counts real fingerprint matches, not what someone
thought was important).

**Deliberately omitted:** rosters, per-person assignments, ticket ids, runbook links.
Those are the organization-specific half of a real handover doc, and inventing a schema
for a stranger's org would be guessing. The digest is a synthesis of observed behavior,
not a CMDB — and the SOP forbids the agent inventing an owner, because a fabricated
assignment is worse than an absent one.

Invariants:

- **Read-only projection, computed fresh.** Stores nothing and decides nothing; a
  cached handover goes stale between shifts, which is worse than none.
- **Headline ordering is load-bearing:** no coverage → work waiting on you → the
  ordinary case. "Nothing is watching" must outrank everything, because a board with no
  configured source looks calm and reporting "all quiet" would be actively misleading.
- **Unproven patterns are visibly unproven.** `proven` reuses the ledger's own
  `FAST_PATH_*` constants rather than restating "verified/high" — a digest that
  disagreed with the engine about what counts as proven would tell a responder to trust
  the wrong entry.
- **Escalated is read from the index, not the open set.** It is a terminal status, so
  it is correctly absent from `open_incidents` — but "we passed this to another owner"
  is exactly what gets lost at shift change. It is therefore NOT subtracted from the
  `progressing` remainder, which counts open work only (doing so went negative once
  several incidents were escalated).
- **`waiting_on_you` requires a fresh slot reconcile**, so the route reconciles first
  exactly as `/state` does; `blocked_reason` is only true if it has just been derived.

Not a cron. A handover is read by a person at a moment they choose, and a scheduled one
nobody reads is the noise this app exists to avoid — so `sops/handover.md` ships with
`cron: null` and `test_config_routes.py` pins that it still reaches an install.

## Crons (manifest-declared)

**`rotation-check` and `ledger-hygiene` ship ENABLED; `dispatch` and `reconcile`
ship paused.** Only `on_shift` jobs may ship paused because `/rotation/arm` only changes
that tier. `rotation-check` must start live to arm it; otherwise a user can enable the app
and configure CloudWatch while dispatch never starts. `ledger-hygiene` starts live and
is primary-gated by its route instead of by cron arming.

Safe to arm because its SOP's **step 0** exits with no output when no provider reports
`configured: true`, so a fresh install pays nothing for a 5-minute poller. Both halves
are pinned (`test_rotation_check_ships_enabled_or_nothing_ever_arms`,
`test_rotation_check_exits_cheaply_when_unconfigured`) plus the Playwright cron check.

Note the registration semantics this depends on: the app bridge writes manifest
`enabled` verbatim to `apps/<name>/app-crons.json` **on install/enable**, and the
CronService then preserves live user intent. So changing a manifest default reaches
existing installs only on a disable/enable cycle — correct (it must not silently
un-pause a cron an operator paused), but it means this fix helps new installs and
re-enables, not a running one.

The four SOPs are declared as crons in `app.json`, so the enable-time bridge
(`bridges._register_crons` → `register_app_crons_with_service`) promotes them into
the running scheduler. All are `silent: true` and `persistent_session: false` (a poller must
not accumulate session context).

**The two gates that carry work ship PAUSED; the two that only observe or arm ship live.**
`reconcile` is on the `on_shift` tier, not `always`: on an ungated tier every instance would
race to resolve the same incidents and rewrite the same Slack thread. The table below is
generated from `rotation.TIER_CRONS` + `app.json` and a test asserts it stays in step.

| Cron | Cadence | Tier | Ships |
|---|---|---|---|
| `dispatch` | 120s | on_shift | paused — it claims and acts, so it must not fire before a provider is configured |
| `reconcile` | 900s | **on_shift** | paused — it POSTs `incident/transition` and edits Slack, so exactly ONE instance may correct the shared board |
| `rotation-check` | 300s | always | **live** — it is what re-arms the gated tiers, so on a gated tier an off-shift instance could never recover |
| `ledger-hygiene` | `17 3 * * *` | primary | **live** — nightly, primary-gated, and 409s on a non-leader |

Caveat inherited from the App Kit: disabling the app deletes its crons and
re-enabling re-registers them from the manifest, so a cron the user resumed
returns to paused after a disable→enable cycle.

## On-disk layout

```
<crew_home>/apps/ops-mission-control/data/
├── config.json            # NON-SECRET only (served unauthenticated)
├── incidents/index.json   # dispatch index — {incident_id: Incident}
├── incidents/<id>.md      # postmortem, written when the incident CLOSES (0o600)
└── ledger.jsonl           # append-only LedgerEntry stream
<crew_home>/ops_mission_control_secrets.json   # KEYSTONE (see contract 4)
```

All writes go through `atomic_write`. File locking goes through
`platform_compat` (never raw `fcntl` — Windows support).

### The per-incident postmortem (`store.write_log`)

Written by `store.transition` when the resulting status is in `TERMINAL_STATUSES`,
and only then — an open incident has no artifact, which is a different fact from an
artifact that is blank. `transition` is the only door to a terminal status
(`sweep_stale` writes only `stale`, and `slot_watch.derive_status` never returns a
terminal one), so that single call site covers every close there is.

**It is the only thing this app produces for a reader who does not run Kiro Crew** —
attachable to a ticket, pasteable into a review. Its content is sourced from the
persisted `Incident` (`diagnosis`, `resolution`), never from the closing call's
kwargs, so an unrelated later field update cannot blank a finished record. The
`Next steps` section renders `_none_` on purpose: no `Incident` field carries one
(`proposed_action` records the proposed action, not the next steps), and a postmortem that invents its
own follow-ups is worse than one that admits it has none.

Failing to write it can never fail the close. The index write is already durable by
that point, so an `OSError` is logged and swallowed — the same rule the Slack mirror
in `_handle_transition` already follows: a record of a state change must not be able
to fail the state change.

It also carries a **verification** line when an action was executed (contract 3b), because
"Actions taken: silenced the alarm" is the sentence in this file most likely to be believed
as an *outcome* by a colleague with no access to the board. It renders nothing at all when
no action was taken.

Readable over HTTP through `GET /incident`, which returns `log` (the text) and
`log_path` (where the file is, `""` when there is none). There is deliberately **no
download route**: a non-JSON response would be a second egress boundary needing its
own redaction pass and its own `security_posture` row, and the JSON field already
makes the artifact readable.

**It never git-syncs.** `ledger_sync` tracks exactly `.gitignore`, `ledger.jsonl` and
`rotation.yaml` — the shared *lessons* and the schedule, not one team's raw incident
narratives. So the artifact is local-only, and a postmortem reaches a colleague
because a human sent it, not because a repo replicated it.

`prune_closed` bounds the dispatch INDEX and deliberately leaves these files alone.
That means a flapping alarm accumulates one file per flap, which is a real (small,
bounded, owner-only) disk cost accepted knowingly: pruning an index row must not
destroy the written record, least of all when a long history is what someone is
looking through.

### Windows compatibility (`TestCrossPlatform`)

This app is portable, and the three places that could break it are pinned by tests rather
than left to review:

- **Resource limits come from the shim wrappers, not a raw `preexec_fn`.** All three
  external-binary spawn paths (`git` for ledger sync and `gh` for the rotation login or
  GitHub Issues) route through `create_subprocess_limited` / `run_limited`, which deliver
  the resource caps
  after `exec` via the spawn shim and fall back to `resource_limit_preexec()` only on a
  host with no usable shim. That fallback returns `None` off POSIX, which is what makes
  the spawns portable — `preexec_fn` is unsupported on Windows and passing *any*
  callable, even a no-op, raises `ValueError`. A hand-rolled `preexec_fn=lambda: ...`
  would work locally and fail on every Windows spawn.
- **No raw POSIX process calls** (`os.killpg`, `os.getpgid`, `os.getuid`, `fcntl.`,
  `signal.SIGKILL`), no `/bin/sh`, no `shell=True`, no hardcoded `/tmp`.
- **Timezone lookup degrades to UTC.** `rotation.yaml` may name an IANA zone, and Windows
  ships no system tz database, so `ZoneInfo(...)` can raise. `tzdata` is a declared
  Windows dependency, but an install missing it must still resolve a rotation rather than
  crash the 5-minute cron. Verified by making the `zoneinfo` **import itself** fail — the
  real failure shape — and asserting the shift still resolves definitively
  (`on_shift=True, unknown=False`, correct `who`), just in UTC.

Asserted from source because CI here is POSIX: the goal is to catch a raw POSIX call at
review time, not to simulate the platform.

## Files

- `src/kiro_crew/apps/builtins/ops_mission_control/app.json` — manifest
- `.../__init__.py` — **re-exports `register_routes`** (the startup loop checks the
  PACKAGE, not `backend.routes`; without it routes silently never register)
- `.../backend/models.py` — `Signal`, `Incident`, `LedgerEntry`, transition grammar,
  fingerprinting, `effective_mode`
- `.../backend/store.py` — dispatch index, atomic claim, transitions, stale sweep, the
  closing postmortem (redacted, incl. its verification line)
- `.../backend/ledger.py` — append-only ledger, matching, the four-condition fast-path bar,
  `record_miss`, hygiene (decay + evidence-based demotion + prune)
- `.../backend/secrets.py` — keystone token store, `SecretBackend` seam, redaction
- `.../backend/registry.py` — ADD-only registry, fan-out
- `.../backend/rotation.py` — autonomy gate, tier arming
- `.../backend/dispatch.py` — **the cycle**: poll → claim → ledger-match → sweep,
  plus `investigation_brief`
- `.../backend/notify_out.py` — the local notification bus as an output channel: three
  declared channels, the replicated manifest + rate-limit guards, edge-triggered pushes
- `.../backend/routes.py` — the HTTP surface's composition root: `register_routes(app)` with
  every full path, the `_require_enabled` gate, `_audit`, the `_safe_outbound` redaction floor
  (the registered sink for the ledger write), `_index_ledger_safely`, a handler for each of the
  nineteen routes whose projection calls a seam (the other six register the projection
  itself), three signature-preserving adapters, and re-exports of every other name `routes.py`
  defined. It must stay this plain module at this path: `app.json`, the package `__init__`, the
  security posture registry and the SOP→route scanner all address it.
- `.../backend/http_routes/` — the handler bodies, one module per slice (none imports `routes`):
  - `_shared.py` — `_json_body`, `_require_bool`, `_store_read_refusal`, `_slack_client`, the
    surface's logger (named for `backend.routes`, so a log filter keeps matching) and the
    seam types;
  - `board.py` — the read-only projections the dashboard polls: `/state`, `/handover`,
    `/incidents`, `/incident`, `/signals`, `/providers`, `/rotation`, with
    `canonical_slot_key`, `_slot_state` and `_ledger_sync_status`;
  - `lifecycle.py` — incident lifecycle writes: `/incident/transition`, `/incident/claim`,
    `/dispatch`;
  - `actions.py` — provider-write authority: `/incident/action`, the propose loop and its
    `/proposals` queue, the `_Authorized` permit, `_authorize`, `_execute_authorized`,
    `_sink_refuses`, `_execute_stored_proposal` and `_schedule_verification`;
  - `configuration.py` — writes to this instance's configuration: provider config and
    secrets, `/settings` (with `_settings_write_or_refuse` and the remote/branch/login
    guards) and `/rotation/arm`;
  - `ledger.py` — `/ledger` read, write and delete, `/ledger/contradictions`,
    `/ledger/hygiene`;
  - `webhook.py` — the bounded, signed `/webhook` ingress: `_read_capped` and
    `_webhook_reject_status`.
- `.../backend/providers/` — the four Protocols + public adapters; the package
  `__init__` also owns config read/merge (`merge_provider_config`, `set_top_level`)
- `src/kiro_crew/builtin_skills/ops-mission-control/` — the agent skill AND the
  six SOPs (`sops/dispatch|handover|investigate|reconcile|rotation-check|ledger-hygiene.md`).
  **They live here, not under the app**, because `register_builtin_apps` copies only
  `app.json` + `installed.json` into the data home for a builtin — so a
  `manifest.skills` entry pointing at an app-local dir silently registers nothing
  (verified: `code_review_sage` has the same latent gap). `builtin_skills/**/*` is
  packaged (`setup.cfg`) and `_ensure_builtin_skills` copytrees it into every
  install, which is the only path that reaches end users. The cron prompts
  reference `~/.kiro/crew/skills/ops-mission-control/sops/<name>.md` accordingly.

  **Every SOP names the credentialed tool, not a token recipe.** The SKILL and all six SOPs
  told the agent to call HTTP endpoints and never said how to authenticate. An
  unattended `rotation-check` run therefore improvised: it hardcoded a port belonging to
  a different gateway, collected `{"error": "Token required"}` **65 times**, spent **41
  tool calls** hunting for a token the cron runner *deliberately destroys* before the
  first tool call, and hit the 1800s cron timeout without ever reaching the API. That
  reads to an operator as "the app is broken".

  A token recipe cannot fix this — the builtin security rules block agents from minting
  gateway tokens, by design. Agent access goes through the `ops_mission_control_api`
  MCP tool instead: the MCP server process holds the gateway's internal secret and
  forwards only a frozen (method, path) allowlist; the agent never sees a credential
  (the `issue_radar_record_investigation` precedent). Each SOP names the tool and its
  paths because a cron agent may read **only** its own SOP. Tests pin the three planes
  to one surface: the allowlist in `validation.py`, the schema rejecting off-surface
  calls, and the gateway's mixed-internal path set admitting exactly the allowlisted
  routes — see `apps/builtins/ops_mission_control/tests/test_agent_api_tool.py`.

  **The SOP→route contract scanner matches the *path*, not a literal prefix.** Filtering lines
  on a literal `GATEWAY/api/apps/...` prefix would leave routes unguarded once the SOPs derive
  `$BASE`, while the test stays green — a renamed route would 404 mid-investigation with
  nothing failing at build time, which is the exact failure the test exists to prevent. A
  companion test pins a floor on the scanner's own yield. A test whose input filter can quietly shrink is worse than no test, because the
  green tick still claims the coverage.

  **A same-named app must never touch this directory.** Because the skill and the
  app share the name `ops-mission-control`, the packaged skill lands at
  `skills/ops-mission-control/` — the exact path the App Kit's skill bridge treats
  as an app-owned link farm, and emptying it is silent — a missing SOP file errors
  nowhere, and every cron prompt would point at SOPs that no longer exist. So the
  bridge acts only on what registration created: `_register_skills` creates no
  namespaced directory for a manifest that declares no skills, and `_deregister_skills`
  (called for any skill-less manifest, to clean stale symlinks) removes symlinks only
  and never a real file or the directory. Pinned by
  `test_app_bridges.py::{test_no_skills_creates_no_directory,
  test_deregister_preserves_a_same_named_packaged_skill}`. The manifest deliberately
  declares **no** `skills` key.
- `website/src/apps/ops-mission-control/` — board page, Settings panel, API client
- Wiring: `apps/builtins/__init__.py::BUILTIN_NAMES`,
  `website/src/apps/builtinRegistry.ts`
- The app's development journals (research sweeps, feature ledger, parity report) are
  deliberately NOT in the repository. They lived in the package tree
  (`apps/builtins/ops_mission_control/planning/`) and were pruned from the sdist by a
  `MANIFEST.in` special case, because `recursive-include src/kiro_crew/apps *.md` — which
  exists to ship ATTRIBUTION/README credit — swept them in as a side effect. They were moved
  out of the package (removing the special case rather than papering over it) and then dropped
  altogether: they carried provenance narrative that does not belong in a public
  repository. **This system spec is the single source of truth for the app's design.**
  In-package `tests/` is deliberately NOT pruned or moved — `code_review_sage` and
  `issue_radar` ship theirs, so that is the convention for a builtin app.

## Known debt

**i18n: keys complete, translation partial.** All five components plus `api.ts` route through
`i18nT`, and the `apps.opsMissionControl` keys (about 380) are mirrored into all twelve
non-English catalogs so `catalogParity.test.ts` passes. Many catalog values are still the
**English fallback** rather than real translations: that is the interim state the
`i18n-translate.mjs` pipeline is built to replace, and parity checks key sets, placeholders and
non-emptiness rather than translation quality. Do NOT hand-edit `en.json` to add keys — it is
generated by `website/scripts/i18n-codemod.mjs`.

**An INTERPOLATED English fragment is worse than an untranslated key**: a key can be translated
later, but no catalog value can repair a sentence with English spliced into the middle of it. So:

- Whole sentences (the verification banner) are keys, singular and plural.
- Glued clauses are ONE key each, so a translator can reorder them: `sent {{age}} ago` rather
  than a translated "sent" welded to an English " ago"; `{{login}} (this instance)`;
  `{{name}} (not answering)`.
- Bare badge values (`unlocked`/`locked`, `proven`, `answered`) are keys.
- **No English computed params.** A noun or verb passed *into* a translated sentence is the
  worst shape, because the surrounding text is already localized; those sentences are
  `_one`/`_other` variants of the whole sentence, registered in `pluralKeys.json`.

Plural forms are **per-language**: `pluralCategories()` in `catalogParity.test.ts` requires
`few`/`many` for `ru`, `many` for the Romance locales, and only `other` for `zh-CN` — a form a
language never selects is unreachable dead weight and also fails. Adding a plural base means
generating exactly that language's categories, not copying `_one`/`_other` everywhere.

**String-comparison operands are exempt by one shared callee pattern.** `eslint-plugin-i18next`
would report the argument of `.startsWith()` as copy, so `callees.exclude` in
`website/eslint.i18n.config.js` carries a single pattern anchored to an identifier/property-chain
receiver and ending in `(startsWith|endsWith|includes|indexOf|lastIndexOf|localeCompare)$`. It
must stay anchored: a callee exemption suppresses the WHOLE call subtree, and the plugin's
`withDottedPrefix` compiles a bare name to `/^(?:.*\.)?startsWith$/`, whose `.*` absorbs any
receiver — `(c ? 'Save changes' : 'Delete item').startsWith(s)` would go unreported.
`i18nLintExemptions.test.ts` pins this. `api.ts`'s throttle check is the predicate
`isBackoffNotice()`, so its literal sits inside that call rather than in an ALL-CAPS constant
the base rule cannot see.

**Label tables hold catalog KEYS, not English.** The `Record<…, string>` label tables
(`STATUS_LABEL_KEY`, `MODE_HELP_KEY`, `CHANNEL_WHEN_KEY`, `BLOCKED_LABEL_KEY`, and the two
`SegmentedControl` segment lists) follow the `FILTER_LABEL_KEY` pattern from
`pages/ChatSidebar.tsx`, because `eslint-plugin-i18next` exempts anything inside an ALL-CAPS
module constant by default, and English literals there would be invisible to the i18n gate.
Three non-obvious constraints:

- The map needs `as const` and **no** `Record<K, string>` annotation — the annotation widens
  every value to `string`, and `check-i18n-keys` can then no longer prove the keys exist.
- Keys must be indexed **directly** inside `i18nT(...)`. A local (`const key = MAP[x]; i18nT(key)`)
  or a closure parameter (`MAP.map(s => i18nT(s.labelKey))`) is unresolvable, and an
  unresolvable site is exempt from *every* downstream check — so the two segment lists are
  written as inline literal keys instead of mapped tables.
- The resolver matches by shape within one file and does **not** follow imports, so
  `BLOCKED_LABEL_KEY` is consumed through `blockedLabel()` / `isKnownBlockedReason()` exported
  beside it rather than indexed from `OpsMissionControlPage.tsx`.

Permission-scope paths such as `'/api/apps/ops-mission-control/*'` are not copy and are exempt
by the shared path patterns in `eslint.i18n.config.js` (`words.exclude` patterns are
full-match, so a prefix-only anchor would exempt nothing longer than `'/'`);
`i18nLintExemptions.test.ts` lists this string among the machine shapes it pins.

**The untranslated ceiling is `website/src/i18n/untranslated-strict-baseline.json`.** Re-measure
it after touching this class rather than carrying a number forward; an upward-only ratchet
quietly keeps a number that is no longer true. The shared `untranslated-baseline.json` is not
re-snapshotted from here (`--update` rewrites it wholesale and causes the cross-branch conflict
its own comment warns about).

The `channel_when_*` strings render standalone in a `<dd>`, so each is a whole sentence rather
than a fragment starting mid-sentence.

**`jsx-a11y/label-has-for`.** `SettingsPanel.tsx` carries one warning of this rule per labelled
field, including the act-rule pattern input. The labels are correct — the input is both nested
AND `htmlFor`/`id`-bound, which is what the rule asks for — but the rule cannot see through the
shared `Input` wrapper component. Other files in the repo, including another builtin app page,
carry the same warning: it is the accepted baseline. These are warnings, not errors.

## Companion adapters

Adapters for ticketing / on-call / pipeline systems that are not public products can
live in a **separate companion package**, developed out of tree, reaching the core only
through the ADD-only registry. This repo contains no reference to any such package
beyond the neutral extension point; the `internal-content-scan` check gates the
public tree.

### The discovery seam (`backend/companion.py`)

The ADD-only rule was enforced and tested from the start, but `get_registry()`
installed only public adapters and nothing ever looked for a companion — the seam
was **a door with no handle**: an out-of-tree package could implement every Protocol
correctly and still never be reached. `companion.py` is the handle.

**Entry points, not a config path.** A filesystem path to import would be a new,
unaudited code-loading channel in an app whose security story is that the agent
cannot reach its own configuration. Contribution therefore requires *installing a
package* — outside the agent's reach and visible to `pip list`. Mirrors
`platform/discovery.py`, because a companion silently invisible to discovery is the
worst failure mode — everything appears to work.

Group is `kirocrew.ops_providers`, deliberately **distinct** from
`platform.discovery.PLUGIN_GROUP`: contributing an ops adapter must not require or
imply authority over the platform edition seam.

```toml
[project.entry-points."kirocrew.ops_providers"]
my-company = "my_pkg.ops:register_adapters"
```

```python
def register_adapters(registry) -> None:
    registry.register_signal_source(MyTicketSource())
```

**Admission is reused, never reinvented.** Importing a separately-installed
package's code into the gateway is a supply-chain decision, and governance guidance
on third-party packages requires 3P code to arrive through a reviewed channel. Every
candidate runs through the SAME fleet `AdmissionPolicy` that gates platform plugins,
evaluated **before `ep.load()`** so rejected code never executes, and each decision
(allow and deny) lands on the SEL trail. A companion is not more trusted for being
ours — and a fleet that banned a package must not be able to have that bypassed by
installing it as an ops adapter instead.

**Fail-OPEN here, unlike platform discovery** — a deliberate product divergence, not
an oversight. `platform/discovery.py` fails closed because a missing companion there
could drop a *security overlay*; running without it is less safe. A companion here
only ADDS signal sources, so a missing one means fewer alarms are watched — visible
on the Signals tab — and aborting boot over it would take down a working public
install (chat, crons, every other app) to punish an optional integration. Rejected /
unimportable / throwing companions are logged, audited, and skipped. One bad
companion does not block a good one.

The single fail-CLOSED path inside the module is the admission check itself: if the
evaluator raises, the candidate is **denied**. "The gate broke" must never read as
"the gate said yes".

**Order is load-bearing.** `get_registry()` installs public adapters *first*, then
companions. Since ADD-only means the incumbent wins, that ordering is what makes a
core id un-shadowable — pinned directly by
`test_companion.py::test_public_adapters_are_installed_before_companions`.

`/state` reports `companions` (name + target, read WITHOUT loading plugin code) and
Settings renders it under Providers only when one is installed. This exists because
"no companion installed" and "companion installed but rejected at admission" look
identical in the provider list and need completely different fixes.

### What remains out of scope here

The **team mesh** (multiple Ops agents sharing work with rotating responsibility) is
NOT unblocked by this seam and must not be built on it. The claim index is
per-instance (`incidents/index.json` + a local file lock), which stops one instance
double-claiming but does **not** stop two instances claiming the same signal. The
ledger is append-only and content-addressed so it merges cleanly; the dispatch index
does not. A mesh needs cross-instance claim arbitration designed first — that is a
new contract, not a new adapter.

The one allowlist entry this app needs is the **public** AWS console host
(`<region>.console.aws.amazon.com`) in the CloudWatch adapter's deep link, matched
only because `INTERNAL_PATTERN` carries a broad `amazon\.com` alternative. It is
line-anchored so a genuinely internal reference in that file is still caught.

## Tests

`src/kiro_crew/apps/builtins/ops_mission_control/tests/` — 24 test files, including:

- `test_models.py` — fingerprint stability, normalization fallbacks, transition
  grammar, mode algebra
- `test_security.py` — keystone floor incl. bash forms, redaction, write-only
  secret store, owner-only mode
- `test_store_and_gate.py` — claim atomicity, illegal transitions, stale sweep,
  autonomy gate incl. blanket-rule refusal, ledger dedupe/decay
- `test_providers.py` — ADD-only registry, fan-out resilience, central redaction,
  adapters unconfigured-not-raising, webhook fail-closed
- `test_routes.py` — namespace containment, every-route-gated, secrets never echoed; its
  source guards (permit minting, the single `sink.execute` caller, off-loop reads, the note
  floor, the slot key, verification convergence) scan the whole HTTP surface — `routes.py`
  plus every `http_routes` module, enumerated from the package directory — and carry floors
  on what they must find, so a moved call site cannot turn one into a vacuous pass
- `test_routes_composition_contract.py` — the facade/projection contract: the frozen
  registration table, the enable gate answering before any body is read on every route,
  webhook ingress order (size before enqueue, signature before parse, a retriable 503 with
  `Retry-After`), an approved proposal executing exactly once under a double or concurrent
  approval, a stale client unable to move a replacement incident, cancellation leaving no
  half-done write, every name `routes.py` defined with its signature, every seam controlling every call site
  (through the helpers a handler calls, too), and the structure that keeps it so
- `test_dispatch.py` — cycle silence (an unchanged firing signal must not
  re-announce), claim cap under a 50-alarm storm, ledger matching + fast path +
  post-increment use count, recurrence-matches-ancestor, rotation gate, one broken
  provider not fatal, and **post-action verification** (contract 3b): a still-firing
  recheck charges a miss, a cleared one does not, and a FAILED poll charges nothing and
  reaches no verdict. That last one is the important guard — the property is invisible in
  the code, so a later "simplify the health check" would look harmless and would start
  reporting every unreachable source as a confirmed fix.
- `test_store_and_gate.py::TestLedger` also pins the **track record** (contract 2b): the
  use floor, one-miss re-lock, and the three laundering doors — a re-POST, a simulated real
  `git merge` producing duplicate ids, and hygiene's own dedupe — each of which must take
  the MAX rather than the incoming value.
- `test_config_routes.py` — **secret field refused on the config route**, unknown
  field/provider refused, merge preserves untouched fields, invalid mode refused,
  and manifest-cron assertions (all four present, only `on_shift` jobs paused,
  all silent and stateless, exactly one schedule each);
  `TestTheCeilingAndConfigWritersAreOwnerOnly` pins the owner gate on `PUT /settings` and
  `PUT /providers/{id}/config`
- `test_provider_secret_owner_gate.py` — the owner gate on `PUT` / `DELETE
  /providers/{id}/secret`: a non-owner subject and every app token are refused `403`
  `owner_only` before the body is read

Frontend: `website/src/test/opsMissionControl.test.ts` (route registration, panel-parity
assertions read from the .tsx source, and the pure helpers `describeSourceHealth` /
`describeVerification` / `entryIsProven` exercised directly).
