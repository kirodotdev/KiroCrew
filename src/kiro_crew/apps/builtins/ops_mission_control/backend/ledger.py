"""Ops Mission Control — knowledge ledger.

The compounding-memory mechanism. When an investigation figures out why something
broke and what fixed it, that becomes a ``LedgerEntry`` keyed by the signal's
fingerprint. The next time a signal with the same fingerprint fires, the
investigation starts from the answer instead of rediscovering it.

This is the part that makes the whole app worth having: institutional memory that
actually compounds, so a fix pattern that would have taken a new on-call engineer
hours to rediscover is already written down.

Three design choices carry the weight:

**Append-only JSONL.** Never rewritten in place except by the hygiene pass. A
crashed writer can truncate at most the last line, and history is auditable.

**Content-addressed ids.** ``entry_id = sha256(pattern + fix)``. Two engineers who
independently learn the same lesson produce the same id, so a git merge of two
ledgers is a dedupe rather than a conflict — which is what makes optional
team sync viable without a server.

**Confidence decay.** An entry that stops being useful loses confidence rather
than lingering forever at "high". A ledger that only ever accumulates becomes
noise, and noise is what kills the undocumented, word-of-mouth approach this replaces.

See ``docs/system-specs/modules/ops-mission-control.md`` (knowledge ledger).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    CONFIDENCE_DECAY,
    CONFIDENCE_HIGH,
    TRUST_VERIFIED,
    LedgerEntry,
    utc_now_iso,
)
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

APP_NAME = "ops-mission-control"

_LEDGER_FILENAME = "ledger.jsonl"

#: Cap on ledger size. Beyond this the hygiene pass prunes lowest-value entries
#: (least used, weakest confidence, oldest). Bounded because the ledger is read
#: into a model prompt on every investigation — an unbounded ledger silently
#: turns into an unbounded context cost.
MAX_LEDGER_ENTRIES = 500

#: Matches returned to one investigation. Small on purpose: the point is the
#: two or three patterns most likely to be the answer, not a reading list.
MAX_MATCHES_PER_SIGNAL = 3

#: Days without a use before confidence decays one step.
DECAY_AFTER_DAYS = 90

#: Cap on the keys bound to ONE entry, applied to ``fingerprints`` and
#: ``provider_keys`` alike, keeping the newest.
#:
#: Needed because not every provider's identity is per-FAILURE: PagerDuty mints a new
#: incident id per occurrence, so a recurring alert would append a key every single time
#: and grow one JSONL line without limit — in a file that is git-synced across a team
#: and read into a model prompt. Keeping the newest is right for matching: an identity
#: that has not appeared in the last 200 occurrences is not the one about to recur.
MAX_KEYS_PER_ENTRY = 200

#: A verified, high-confidence match is the "known-pattern fast path" — the
#: investigation can propose its fix directly instead of re-deriving it.
FAST_PATH_CONFIDENCE = CONFIDENCE_HIGH
FAST_PATH_TRUST = TRUST_VERIFIED

#: Uses an entry must have accumulated before the fast path unlocks, on top of
#: verified+high.
#:
#: 2, and **1 would be vacuous** — worth stating because 1 is the obvious value to reach
#: for when someone later decides this is too strict. ``attach_ledger_matches`` calls
#: ``record_use`` BEFORE ``is_fast_path``, so at the moment of judgement ``use_count``
#: already includes the incident being judged: every match whatsoever has
#: ``use_count >= 1``, including a hand-POSTed entry matching for the very first time,
#: which is the exact case the floor exists to exclude. 2 is therefore the smallest floor
#: that says anything at all — "some incident OTHER than this one has been handed this
#: entry before".
#:
#: It also lands on the same line ``handover.MIN_USES_TO_RECUR`` already draws ("used once
#: is an incident, used twice is a pattern"), and drawing it differently in the two places
#: would let the digest call something recurring that the engine calls unproven.
#:
#: **The cost, stated rather than glossed:** the fast path now unlocks on the THIRD
#: occurrence of a failure instead of the second (occurrence 1 authors the entry,
#: occurrence 2 is its first use, occurrence 3 clears the floor). That is one extra
#: investigation that reads its matches as hypotheses. It is affordable because a
#: non-fast-path match is not withheld — the brief still carries the full pattern and fix,
#: and the only difference is that the agent is told to confirm before proposing.
#:
#: What the missing floor cost, and why the exact-identity layer made it worse rather
#: than better: nothing stopped one hand-POSTed entry arriving as ``verified``/``high``
#: with ``use_count == 0`` and immediately unlocking "propose this fix directly" —
#: ``POST /ledger`` takes ``confidence`` and ``trust`` verbatim. Then ``record_use``
#: BINDS the provider key on the first match, so from the second occurrence onward that
#: same entry matches EXACTLY, presenting a strictly stronger-looking claim on the very
#: same single piece of evidence. The floor is what makes the fast path mean "this has
#: worked before" rather than "somebody typed high".
MIN_USES_FOR_FAST_PATH = 2

#: Misses tolerated before the fast path re-locks. Zero, deliberately.
#:
#: The asymmetry with ``MIN_USES_FOR_FAST_PATH`` is the point: unlocking needs
#: corroboration, re-locking needs one counterexample. A fix that visibly did not hold
#: once must stop being the thing an agent proposes without checking — that is a
#: downgrade to "hypothesis", not a deletion, and the entry keeps its full text so the
#: next responder still reads what somebody learned. Deleting a used entry is forbidden
#: (``ledger-hygiene.md``) precisely because a fix that works sometimes is still worth
#: more than nothing.
MAX_MISSES_FOR_FAST_PATH = 0

#: Misses per use at which the nightly hygiene pass demotes confidence one step.
#:
#: A ratio rather than an absolute count so a heavily-used entry is not condemned by one
#: bad night: at 0.5, an entry used 8× tolerates 3 misses and demotes on the 4th, while a
#: brand-new one demotes on its first. That is the right shape — the more evidence an
#: entry has that it works, the more it takes to overturn.
#:
#: Applied by HYGIENE, never on the hot path, and it reports what it did. Demotion is one
#: step along ``CONFIDENCE_DECAY`` and never touches ``trust``: "somebody saw this work"
#: stays true even after it failed elsewhere, and rewriting that would be the ledger
#: editorialising about a human's own observation.
MISS_RATIO_FOR_DECAY = 0.5


def ledger_path() -> Path:
    return app_data_dir(APP_NAME) / _LEDGER_FILENAME


_LOCK_FILENAME = ".ledger.lock"


class _LedgerLock:
    """Exclusive lock around a read-modify-write of the ledger, mirroring ``store._IndexLock``.

    Every mutation that RE-READS the ledger and REWRITES the whole file — ``upsert``,
    ``record_use``, ``record_miss``, ``remove``/decay, and the hygiene pass — must hold this,
    or one clobbers another's snapshot. The hygiene pass in particular reads, dedupes/prunes,
    and calls ``_write_all``; a ledger POST that ran ``upsert`` in between was silently erased,
    because ``_write_all`` overwrites rather than appends. `store` already learned this for the
    incident index; the ledger had no lock at all. Found in review.

    ``_append`` alone (the raw write behind an already-locked ``upsert``) is git-merge-safe by
    construction — append-only, deduped on read — but ``_write_all`` is not, which is why the
    lock guards the read-modify-write span, not just the write.

    Routed through ``platform_compat.file_lock`` so it works on Windows, where ``fcntl`` does
    not exist.
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    def __enter__(self) -> _LedgerLock:
        lock_file = app_data_dir(APP_NAME) / _LOCK_FILENAME
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        platform_compat.acquire_lock(self._fd, exclusive=True)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            try:
                platform_compat.release_lock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None


def read_entries() -> list[LedgerEntry]:
    """All ledger entries, reconciled by id. A malformed line is skipped, never fatal.

    **Duplicate ids are merged on read, because a git merge produces them.** The whole
    argument for content-addressed ids on an append-only JSONL file is that two people
    who learn the same lesson write the same id, so merging two ledgers is a dedupe
    rather than a conflict — but git resolves that as *both lines present*. Appending
    every line meant one shared lesson counted twice: ``stats()`` inflated, ``match()``
    returned the same entry twice, and the handover digest listed one pattern as two.

    Reconciled the same way ``upsert`` merges (fingerprints union, strongest confidence
    and trust, highest use count), so a read after a merge agrees with what a local
    upsert of the same two entries would have produced. First occurrence keeps its
    position, so ordering stays stable for callers that rank by it.
    """
    path = ledger_path()
    if not path.exists():
        return []
    ordered: list[str] = []
    by_id: dict[str, LedgerEntry] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = LedgerEntry.from_dict(json.loads(line))
                except (json.JSONDecodeError, TypeError, ValueError):
                    logger.warning(
                        "ops-mission-control: skipping malformed ledger line %d", line_no
                    )
                    continue
                prior = by_id.get(entry.entry_id)
                if prior is None:
                    ordered.append(entry.entry_id)
                    by_id[entry.entry_id] = entry
                else:
                    by_id[entry.entry_id] = _reconcile(prior, entry)
    except OSError:
        logger.exception("ops-mission-control: failed to read ledger")
        return []
    return [by_id[eid] for eid in ordered]


def _reconcile(prior: LedgerEntry, other: LedgerEntry) -> LedgerEntry:
    """Merge two records of the same content-addressed entry.

    Mirrors ``upsert``'s algebra deliberately: learning a lesson again must never
    *weaken* what is known, so confidence and trust take the strongest of the two and
    ``use_count`` the highest. Keeping ``max`` on use_count rather than summing is the
    conservative choice — two branches that each recorded the same 3 uses did not
    between them see 6 occurrences.

    ``miss_count`` takes the max for the OPPOSITE reason, and the asymmetry is the point:
    strongest-wins on confidence means a merge must not be able to launder away a
    teammate's evidence that the fix failed. ``min`` or a reset would make "pull the
    team ledger" a way to clear a demotion, which is the one direction a shared,
    append-only knowledge base must not move on its own.
    """
    order = list(CONFIDENCE_DECAY.keys())  # high, medium, low

    def _rank(value: str) -> int:
        return order.index(value) if value in order else len(order)

    # BOTH identity lists union here, and both are capped — mirroring `upsert` exactly.
    #
    # `provider_keys` was missing entirely: this function reconciles duplicate ids produced
    # by a real `git merge` of two teammates' ledgers, so dropping one side's provider keys
    # permanently wrote incomplete identity data — and `match()` treats a provider key as
    # the EXACT-identity signal, so the very next recurrence on that alert would have been
    # matched by shape hash alone, or not at all. That is the same class of silent
    # knowledge loss the fingerprint union exists to prevent. Found in review.
    #
    # The cap was missing too: two already-capped lists unioned are up to 2× the cap, which
    # `upsert` bounds and this path did not, so a merge could grow a list past the limit and
    # keep it there.
    prior.fingerprints = list(dict.fromkeys([*prior.fingerprints, *other.fingerprints]))[
        -MAX_KEYS_PER_ENTRY:
    ]
    prior.provider_keys = list(dict.fromkeys([*prior.provider_keys, *other.provider_keys]))[
        -MAX_KEYS_PER_ENTRY:
    ]
    prior.confidence = min((prior.confidence, other.confidence), key=_rank)
    prior.trust = TRUST_VERIFIED if TRUST_VERIFIED in {prior.trust, other.trust} else prior.trust
    prior.use_count = max(prior.use_count, other.use_count)
    prior.miss_count = max(prior.miss_count, other.miss_count)
    prior.last_miss = max(prior.last_miss, other.last_miss)
    # Also max: this counts evidence somebody's hygiene pass has already SPENT on a
    # demotion. Taking the lower value would let a git pull hand back a miss to be spent
    # a second time, walking one failure two steps down the confidence scale.
    prior.decayed_at_miss_count = max(prior.decayed_at_miss_count, other.decayed_at_miss_count)
    prior.last_used = max(prior.last_used, other.last_used)
    prior.first_seen = min(
        (x for x in (prior.first_seen, other.first_seen) if x), default=prior.first_seen
    )
    return prior


def _write_all(entries: list[LedgerEntry]) -> None:
    """Rewrite the whole ledger. Only the hygiene pass should call this."""
    payload = "".join(json.dumps(entry.to_dict(), sort_keys=True) + "\n" for entry in entries)
    atomic_write(ledger_path(), payload)


def _append(entry: LedgerEntry) -> None:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")


def upsert(entry: LedgerEntry) -> LedgerEntry:
    """Add ``entry``, or merge it into the existing entry with the same id.

    Because ids are content-addressed, "the same lesson learned twice" merges:
    fingerprints union, use_count carries forward, and the stronger confidence and
    trust win. Learning a lesson again should never *weaken* what we know.
    """
    with _LedgerLock():
        return _upsert_locked(entry)


def _upsert_locked(entry: LedgerEntry) -> LedgerEntry:
    existing = {e.entry_id: e for e in read_entries()}
    prior = existing.get(entry.entry_id)
    if prior is None:
        _append(entry)
        return entry

    merged_fps = list(dict.fromkeys([*prior.fingerprints, *entry.fingerprints]))
    # Provider keys union on the same terms as fingerprints: two teammates who learn the
    # same lesson against the same provider-side alert must converge, not conflict.
    merged_keys = list(dict.fromkeys([*prior.provider_keys, *entry.provider_keys]))
    order = list(CONFIDENCE_DECAY.keys())  # high, medium, low
    best_confidence = min(
        (prior.confidence, entry.confidence),
        key=lambda c: order.index(c) if c in order else len(order),
    )
    # Bounded on merge too: a git pull brings in a teammate's keys, and two capped lists
    # unioned are up to 2× the cap.
    prior.fingerprints = merged_fps[-MAX_KEYS_PER_ENTRY:]
    prior.provider_keys = merged_keys[-MAX_KEYS_PER_ENTRY:]
    prior.confidence = best_confidence
    prior.trust = TRUST_VERIFIED if TRUST_VERIFIED in {prior.trust, entry.trust} else prior.trust
    # Miss evidence survives a re-POST, and stated explicitly rather than left to the
    # fact that we happen to mutate ``prior``. ``POST /ledger`` is how the hygiene SOP
    # promotes observed → verified (re-post the same pattern+fix), so without this the
    # promotion route would double as a way to erase every recorded failure — the exact
    # laundering ``_reconcile``'s max guards against, reachable with one curl.
    prior.miss_count = max(prior.miss_count, entry.miss_count)
    prior.last_miss = max(prior.last_miss, entry.last_miss)
    prior.decayed_at_miss_count = max(prior.decayed_at_miss_count, entry.decayed_at_miss_count)
    prior.last_used = utc_now_iso()
    existing[entry.entry_id] = prior
    _write_all(list(existing.values()))
    return prior


def match(
    fingerprint: str,
    *,
    provider_key: str = "",
    limit: int = MAX_MATCHES_PER_SIGNAL,
) -> list[LedgerEntry]:
    """Entries that have previously matched this failure.

    Two keys, tried in order of how much they can be trusted:

    1. **``provider_key``** — the identity the PROVIDER computed (Alertmanager
       fingerprint, Sentry issue id, Zabbix trigger id). An exact hit means *this same
       failure*, decided by the system that owns the grouping.
    2. **``fingerprint``** — this app's shape hash over rendered text. A hit means
       *something that looks like this*, which is weaker than it appears: the hash
       strips every bare digit, so "4xx error rate above 5" and "5xx error rate above 1"
       on one resource are indistinguishable to it.

    Exact matches are ranked ABOVE shape matches regardless of trust or use count,
    because a provider-confirmed identity beats a heuristic that a well-used entry
    merely got lucky with. Within each tier the existing ordering applies: trust, then
    confidence, then use count.

    Passing no ``provider_key`` reproduces the previous fingerprint-only behaviour
    exactly, which is what every adapter that publishes no stable identity still gets.
    """
    if not fingerprint and not provider_key:
        return []
    order = list(CONFIDENCE_DECAY.keys())

    def rank(entry: LedgerEntry) -> tuple[int, int, int, int]:
        exact = bool(provider_key) and provider_key in entry.provider_keys
        return (
            0 if exact else 1,
            0 if entry.trust == TRUST_VERIFIED else 1,
            order.index(entry.confidence) if entry.confidence in order else len(order),
            -entry.use_count,
        )

    candidates = [
        e
        for e in read_entries()
        if (fingerprint and fingerprint in e.fingerprints)
        or (provider_key and provider_key in e.provider_keys)
    ]
    candidates.sort(key=rank)
    return candidates[:limit]


def is_exact_match(entry: LedgerEntry, provider_key: str) -> bool:
    """Whether ``entry`` matched by provider-computed identity rather than by shape.

    Exposed so a caller can TELL a responder which kind of match it is showing them.
    Presenting a shape match with the same confidence as an exact one is how a ledger
    starts asserting more than it knows.
    """
    return bool(provider_key) and provider_key in entry.provider_keys


def entry_unlocks_fast_path(entry: LedgerEntry) -> bool:
    """Whether ONE entry has earned "propose this fix directly".

    Four conditions, and the two new ones are the track record: verified trust, high
    confidence, at least ``MIN_USES_FOR_FAST_PATH`` uses, and no recorded miss.

    Split out of ``is_fast_path`` so a caller that must EXPLAIN a decision per entry —
    the board, which now shows each match's record — asks the same predicate the engine
    asks instead of restating it. A UI that re-derived "proven" would be free to disagree
    with the brief the agent was handed, and the operator would have no way to tell which
    of the two was lying.
    """
    return (
        entry.trust == FAST_PATH_TRUST
        and entry.confidence == FAST_PATH_CONFIDENCE
        and entry.use_count >= MIN_USES_FOR_FAST_PATH
        and entry.miss_count <= MAX_MISSES_FOR_FAST_PATH
    )


def is_demoted(entry: LedgerEntry) -> bool:
    """Whether evidence has been recorded that this entry's fix did not hold.

    Separate from ``entry_unlocks_fast_path`` because the two answer different
    questions and an operator needs both. "Not fast path" is the ordinary state of most
    of the ledger (new entries, honest guesses). "Demoted" is the specific, much louder
    fact that this fix was cited and then the failure came back — a hypothesis that has
    already been tested and lost, which is worth strictly less than one nobody has tried.
    """
    return entry.miss_count > 0


def is_fast_path(entries: list[LedgerEntry]) -> bool:
    """True when a match is trustworthy enough to propose its fix directly.

    Requires BOTH verified trust and high confidence, AND a track record: see
    ``entry_unlocks_fast_path``. Proposing a remembered fix for a production failure on
    weaker evidence than that is how a knowledge base starts doing harm.
    """
    return any(entry_unlocks_fast_path(e) for e in entries)


def record_use(entry_id: str, fingerprint: str = "", provider_key: str = "") -> LedgerEntry | None:
    """Mark an entry as used, optionally binding a new fingerprint/provider key to it.

    Binding lets a pattern generalize: the same root cause surfacing through a
    differently-worded alarm gets attached to the entry that already knows the
    fix. Binding the ``provider_key`` is what turns the FIRST fuzzy match into an
    exact one for every later occurrence — the entry learns the provider's own identity
    for the failure, so the next recurrence no longer depends on the shape hash.

    Locked read-modify-write: a bare ``read_entries``/``_write_all`` here would clobber a
    concurrent ``upsert`` or hygiene rewrite. See ``_LedgerLock``.
    """
    with _LedgerLock():
        return _record_use_locked(entry_id, fingerprint, provider_key)


def _record_use_locked(entry_id: str, fingerprint: str, provider_key: str) -> LedgerEntry | None:
    entries = read_entries()
    changed = False
    hit: LedgerEntry | None = None
    for entry in entries:
        if entry.entry_id != entry_id:
            continue
        entry.use_count += 1
        entry.last_used = utc_now_iso()
        if fingerprint and fingerprint not in entry.fingerprints:
            entry.fingerprints.append(fingerprint)
            del entry.fingerprints[:-MAX_KEYS_PER_ENTRY]
        if provider_key and provider_key not in entry.provider_keys:
            entry.provider_keys.append(provider_key)
            del entry.provider_keys[:-MAX_KEYS_PER_ENTRY]
        hit = entry
        changed = True
        break
    if changed:
        _write_all(entries)
    return hit


def record_miss(entry_id: str) -> LedgerEntry | None:
    """Record that this entry's fix was cited and the failure came back anyway.

    The mechanical downward path §5.9 asked for. Called from ONE place —
    ``dispatch.record_verification_misses``, when a post-action recheck observed the
    signal STILL FIRING against a source whose poll actually succeeded — so the standard
    of evidence is fixed in one spot rather than negotiated per caller.

    Two things it deliberately does NOT do:

    - **It does not decay confidence.** That is the nightly hygiene pass's job (see
      ``hygiene``), for the reason the whole app applies to expensive or judgement-shaped
      work: the hot path stays cheap and deterministic, and a demotion that happens where
      an operator is watching a report is a demotion they can see and argue with. A
      confidence rewrite buried inside a claim would move the ledger silently.
    - **It does not increment ``use_count``.** A miss is not a use. Letting a failure
      inflate the number that ranks the entry is the original defect turned inside out.

    Returns the updated entry, or ``None`` when the id is not in the ledger — a pruned
    entry is a normal outcome, not an error.

    Locked read-modify-write; see ``_LedgerLock``.
    """
    with _LedgerLock():
        return _record_miss_locked(entry_id)


def _record_miss_locked(entry_id: str) -> LedgerEntry | None:
    entries = read_entries()
    hit: LedgerEntry | None = None
    for entry in entries:
        if entry.entry_id != entry_id:
            continue
        entry.miss_count += 1
        entry.last_miss = utc_now_iso()
        hit = entry
        break
    if hit is not None:
        _write_all(entries)
        logger.info(
            "ops-mission-control: ledger entry %s missed (%d miss / %d use)",
            entry_id,
            hit.miss_count,
            hit.use_count,
        )
    return hit


def remove(entry_id: str) -> bool:
    # Locked read-modify-write; see ``_LedgerLock``.
    with _LedgerLock():
        entries = read_entries()
        remaining = [e for e in entries if e.entry_id != entry_id]
        if len(remaining) == len(entries):
            return False
        _write_all(remaining)
        return True


def find_contradictions(entries: list[LedgerEntry] | None = None) -> list[dict[str, Any]]:
    """Entry pairs that claim DIFFERENT fixes for the SAME failure fingerprint.

    A consolidation pass has to "resolve contradictions" somehow, and
    ours asks the same of the hygiene agent. But finding them was left entirely to the
    model's eye across the whole ledger — an O(n²) scan over text, which is exactly the
    mechanical work that should not cost model turns and is exactly the kind a model skims
    once the ledger is more than a screenful.

    So this DETECTS and does not decide. Two entries sharing a fingerprint with different
    fixes usually means the failure has more than one cause, and the right answer is to
    split the pattern descriptions so each is distinguishable — a judgement call about what
    the two causes actually are, which needs the model. Deleting one would silently discard
    a real, working fix.

    Ordered most-proven-first (by combined use count) so a responder reviewing a long list
    sees the pairs that are actively misleading people before the speculative ones.
    """
    rows = entries if entries is not None else read_entries()
    by_fingerprint: dict[str, list[LedgerEntry]] = {}
    for entry in rows:
        for fingerprint in entry.fingerprints:
            by_fingerprint.setdefault(fingerprint, []).append(entry)

    seen_pairs: set[tuple[str, str]] = set()
    found: list[dict[str, Any]] = []
    for fingerprint, group in by_fingerprint.items():
        if len(group) < 2:
            continue
        for i, left in enumerate(group):
            for right in group[i + 1 :]:
                # Same fix reached by two entries is not a contradiction — that is the
                # duplicate case dedupe already merges by content-addressed id.
                if left.fix.strip() == right.fix.strip():
                    continue
                # Explicit 2-tuple: `tuple(sorted(...))` widens to tuple[str, ...] and
                # loses the arity the set's type declares.
                first, second = sorted((left.entry_id, right.entry_id))
                key = (first, second)
                if key in seen_pairs:
                    # Two entries can share more than one fingerprint; report the pair
                    # once rather than once per shared fingerprint.
                    continue
                seen_pairs.add(key)
                found.append(
                    {
                        "fingerprint": fingerprint,
                        "entries": [left.to_dict(), right.to_dict()],
                        "uses": left.use_count + right.use_count,
                    }
                )
    found.sort(key=lambda row: (-int(row["uses"]), str(row["fingerprint"])))
    return found


def hygiene(*, now: datetime | None = None) -> dict[str, int]:
    """Dedupe, decay unused confidence, demote what missed, and prune.

    Runs on the ``primary`` tier. Returns a summary of what changed so the SOP can stay
    silent when the answer is "nothing" — silence-by-default applies to maintenance jobs
    too.

    Two independent downward paths, reported separately because they mean opposite
    things and the operator's response differs:

    - ``decayed`` — nobody needed this for ``DECAY_AFTER_DAYS``. Says nothing about
      whether the fix works; the estate moved on.
    - ``demoted`` — the fix was cited and the failure came back (``miss_count``). This is
      evidence AGAINST the entry, which is the movement the ledger previously had no
      mechanism for at all.

    Collapsing them into one number would let "your ledger is going stale" and "your
    ledger is wrong" arrive as the same sentence.

    The whole read → dedupe/decay/demote/prune → ``_write_all`` runs under ``_LedgerLock``.
    ``_write_all`` overwrites the file, so a ``POST /ledger`` (``upsert``) or a ``record_use``
    landing between this pass's ``read_entries`` and its write would be silently erased — the
    ledger analogue of the incident-index race, and the write half of the peek/ack lesson: a
    rewrite from a stale snapshot drops everything appended since the snapshot. Found in review.
    """
    with _LedgerLock():
        return _hygiene_locked(now)


def _hygiene_locked(now: datetime | None) -> dict[str, int]:
    current = now or datetime.now(timezone.utc)
    entries = read_entries()
    before = len(entries)

    # Dedupe by content-addressed id, merging fingerprints and keeping the
    # highest use_count. Duplicates arrive via git-synced ledgers.
    merged: dict[str, LedgerEntry] = {}
    for entry in entries:
        seen = merged.get(entry.entry_id)
        if seen is None:
            merged[entry.entry_id] = entry
            continue
        seen.fingerprints = list(dict.fromkeys([*seen.fingerprints, *entry.fingerprints]))
        seen.use_count = max(seen.use_count, entry.use_count)
        # Same max-wins rule as ``_reconcile``, and load-bearing HERE too: this is the
        # pass that rewrites the file, so a dedupe that dropped the higher miss_count
        # would make the nightly cron the thing that erases the team's counter-evidence.
        seen.miss_count = max(seen.miss_count, entry.miss_count)
        seen.last_miss = max(seen.last_miss, entry.last_miss)
        seen.decayed_at_miss_count = max(seen.decayed_at_miss_count, entry.decayed_at_miss_count)
        if entry.trust == TRUST_VERIFIED:
            seen.trust = TRUST_VERIFIED
        if entry.last_used > seen.last_used:
            seen.last_used = entry.last_used
    deduped = list(merged.values())
    dupes_removed = before - len(deduped)

    # Decay confidence for entries unused past the window.
    cutoff = current - timedelta(days=DECAY_AFTER_DAYS)
    decayed = 0
    for entry in deduped:
        try:
            last = datetime.strptime(entry.last_used, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (TypeError, ValueError):
            continue
        if last >= cutoff:
            continue
        weaker = CONFIDENCE_DECAY.get(entry.confidence, entry.confidence)
        if weaker != entry.confidence:
            entry.confidence = weaker
            decayed += 1

    # Demote on recorded misses. Separate loop from the decay above and counted
    # separately, because an entry can legitimately be both stale AND wrong and an
    # operator reading "1 decayed" would not know the second thing happened.
    demoted = 0
    for entry in deduped:
        if entry.miss_count <= entry.decayed_at_miss_count:
            # Every miss this entry has was already spent on a previous demotion. The
            # test below is a ratio, which stays true once true, so without this guard a
            # single failure would walk an entry high → medium → low on three successive
            # nights and no new evidence. One piece of evidence, one step.
            continue
        if entry.miss_count < max(1, entry.use_count * MISS_RATIO_FOR_DECAY):
            continue
        weaker = CONFIDENCE_DECAY.get(entry.confidence, entry.confidence)
        # ``decayed_at_miss_count`` advances even at the bottom of the scale, where
        # CONFIDENCE_DECAY maps low → low and there is no step left to take. Otherwise
        # the guard above never trips for a `low` entry and every subsequent night
        # re-reports the same demotion as news, which is the noise discipline this app
        # applies everywhere else.
        entry.decayed_at_miss_count = entry.miss_count
        if weaker != entry.confidence:
            entry.confidence = weaker
            demoted += 1
            logger.info(
                "ops-mission-control: demoted ledger entry %s to %s (%d miss / %d use)",
                entry.entry_id,
                weaker,
                entry.miss_count,
                entry.use_count,
            )

    # Prune to the cap, dropping least-valuable first.
    order = list(CONFIDENCE_DECAY.keys())
    deduped.sort(
        key=lambda e: (
            # Misses subtract from standing in the prune order. Before this the order was
            # ``-use_count`` alone, so a false-matching entry that climbed the ranking on
            # every mismatch was the LAST thing pruned — the ledger preferentially kept
            # its most misleading rows. A net score keeps a genuinely useful entry (8
            # uses, 1 miss) ahead of an unproven one while sinking a 2-use/2-miss entry
            # below it.
            -(e.use_count - e.miss_count),
            0 if e.trust == TRUST_VERIFIED else 1,
            order.index(e.confidence) if e.confidence in order else len(order),
            e.last_used,
        )
    )
    pruned = max(0, len(deduped) - MAX_LEDGER_ENTRIES)
    kept = deduped[:MAX_LEDGER_ENTRIES]

    if dupes_removed or decayed or demoted or pruned:
        _write_all(kept)

    return {
        "before": before,
        "after": len(kept),
        "deduped": dupes_removed,
        "decayed": decayed,
        # Demoted on EVIDENCE rather than on disuse. Reported separately from ``decayed``
        # because "nobody needed this" and "this did not work" are opposite findings.
        "demoted": demoted,
        "pruned": pruned,
        # Detected, never auto-resolved: splitting a pattern needs to know what the two
        # causes ARE. Counted here so the hygiene SOP can jump straight to the pairs
        # instead of re-scanning the ledger by eye, and so a rising count is visible.
        "contradictions": len(find_contradictions(kept)),
    }


def stats() -> dict[str, int]:
    entries = read_entries()
    return {
        "total": len(entries),
        "verified": sum(1 for e in entries if e.trust == TRUST_VERIFIED),
        "high_confidence": sum(1 for e in entries if e.confidence == CONFIDENCE_HIGH),
        "total_uses": sum(e.use_count for e in entries),
        # ``verified`` and ``high_confidence`` are each one HALF of the old fast-path bar,
        # so neither answers "how much of this ledger would an agent actually propose
        # without checking". This does, and it now includes the track-record floor — which
        # is why it can be strictly smaller than either of the two above and why showing
        # them alone overstated the ledger's authority.
        "proven": sum(1 for e in entries if entry_unlocks_fast_path(e)),
        # Entries carrying evidence their fix did not hold. The number an operator most
        # needs and the one the app could not previously produce at all.
        "demoted": sum(1 for e in entries if is_demoted(e)),
        "total_misses": sum(e.miss_count for e in entries),
    }
