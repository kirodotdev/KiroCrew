"""Ops Mission Control — the dispatch engine.

This is the module that turns the parts into a working first responder. Without
it the registry polls into the void: signals are normalized but nothing claims
them, and ``Incident.ledger_matches`` stays empty forever — which would leave the
compounding-memory mechanism structurally present but functionally dead.

One cycle:

1. Poll every configured ``SignalSource`` concurrently.
2. Diff against the dispatch index and claim what is unowned (atomically — see
   ``store.claim``; a losing claimant skips rather than duplicating work).
3. **Match each claim's fingerprint against the knowledge ledger** and attach the
   hits, so the investigation opens already knowing what this failure was last
   time. This step is the whole point.
4. Release investigations that have gone idle, so a dead agent cannot hold a
   signal claimed and therefore unworked.

The cycle is deliberately *not* an agent turn. It is deterministic Python the cron
calls once, which keeps the expensive part (an actual investigation) to signals
that genuinely need one and keeps the heartbeat's cost flat. A heartbeat that stays
silent and cheap is what makes a polling loop tolerable to live with.

See ``docs/system-specs/modules/ops-mission-control.md`` (dispatch cycle).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import (
    ledger,
    notify_out,
    rotation,
    slack_out,
    store,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    OPEN_VERIFICATIONS,
    STATE_FIRING,
    STATE_SUPPRESSED,
    STATUS_STALE,
    TERMINAL_STATUSES,
    VERIFY_CLEARED,
    VERIFY_STILL_FIRING,
    VERIFY_UNKNOWN,
    Incident,
    LedgerEntry,
    Signal,
    utc_now_iso,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config, webhook
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    Evidence,
    EvidenceBudget,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.registry import get_registry
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Claims per cycle. A provider that fans out 200 alarms at once must not be able
#: to spawn 200 investigation sessions — the cap turns a storm into a queue that
#: drains over successive heartbeats instead of a thundering herd.
DEFAULT_MAX_CLAIMS_PER_CYCLE = 3

#: Seconds of inactivity before a claimed incident is released for re-pickup.
#: Two hours: long enough that a genuinely slow investigation is not yanked away,
#: short enough that a crashed one is noticed the same shift.
DEFAULT_STALE_AFTER_SECS = 2 * 60 * 60

_CONFIG_MAX_CLAIMS = "max_claims_per_cycle"
_CONFIG_STALE_AFTER = "stale_after_secs"
_CONFIG_NEEDS_HUMAN_STALE_AFTER = "needs_human_stale_after_secs"

#: Total characters of provider evidence rendered into one investigation brief.
#:
#: The per-item ``EvidenceBudget`` (64 KB) bounds what an ADAPTER may return, which is
#: the right cap for a spool but far too large for a prompt: six calls at 64 KB is
#: ~384 KB, and a measured brief reached 37k chars from just two items
#: against a documented 50k TOTAL session context budget (see ``context.py``). Evidence
#: only started reaching the prompt when brokering landed, so this cap is new work, not
#: a regression. 8k is roughly the conversation budget — enough for an alarm history
#: plus a screenful of log lines, which is what a first diagnosis actually reads.
MAX_BRIEF_EVIDENCE_CHARS = 8000

#: Characters of any single evidence item, so one huge log dump cannot crowd out the
#: alarm history that would have explained it.
MAX_BRIEF_EVIDENCE_ITEM_CHARS = 4000


@dataclass
class ClaimedIncident:
    """One newly-claimed incident plus the context an investigation needs."""

    incident: Incident
    matches: list[LedgerEntry] = field(default_factory=list)
    fast_path: bool = False
    #: Redacted provider context, gathered by the GATEWAY and handed to the agent.
    #: See ``investigation_brief`` for why the agent is not given credentials.
    evidence: list[Evidence] = field(default_factory=list)
    #: Ledger entries that are semantically SIMILAR to this signal but whose fingerprint
    #: does NOT match. Kept separate from ``matches`` deliberately: a fingerprint match is
    #: evidence this exact failure recurred, while a similar one is a lead. Merging them
    #: would let a near-miss inherit "used 4x, verified" authority it has not earned, and
    #: would make ``record_use`` inflate the use count of an entry this incident never
    #: actually used — corrupting the one number that tells a responder how proven a fix is.
    similar: list[LedgerEntry] = field(default_factory=list)
    #: Entry ids among ``matches`` that matched on the PROVIDER's own identity rather
    #: than on our shape hash. Kept as ids rather than a flag on the entry because a
    #: ``LedgerEntry`` is a stored record and "how did we find you this time" is a
    #: property of this lookup, not of the entry.
    exact_match_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident": self.incident.to_dict(),
            "matches": [m.to_dict() for m in self.matches],
            "similar": [m.to_dict() for m in self.similar],
            "exact_match_ids": list(self.exact_match_ids),
            "evidence": [
                {"source": e.source, "kind": e.kind, "title": e.title, "body": e.body}
                for e in self.evidence
            ],
            # True when a verified, high-confidence pattern matched: the
            # investigation can propose that fix directly instead of re-deriving
            # it. This is the "known-pattern fast path".
            "fast_path": self.fast_path,
        }


@dataclass
class CycleResult:
    """Everything one dispatch cycle did. Empty means the cron must stay silent."""

    claimed: list[ClaimedIncident] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    polled: int = 0
    unclaimed_remaining: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    skipped_reason: str = ""
    #: Post-action verification verdicts this cycle reached, ``{incident_id: verdict}``.
    #:
    #: Only incidents whose recheck came DUE this cycle appear. An empty map is the
    #: normal case — most cycles have nothing to verify — and is not the same as "every
    #: action worked".
    verifications: dict[str, str] = field(default_factory=dict)
    #: Signals a human already parked at the provider, which this cycle saw and
    #: deliberately did not claim.
    #:
    #: Counted, not just filtered. Without this the suppressed signals vanish from
    #: ``CycleResult`` entirely — not in ``polled``, not in ``errors``, nowhere — so the
    #: dashboard's "Polled N firing signal(s); nothing new to claim" line reports a
    #: SMALLER world than the cycle actually saw, and an operator reading it cannot tell
    #: a genuinely quiet estate from one where three alarms are parked. That is the same
    #: looks-deliberate-does-nothing failure the state itself exists to fix.
    suppressed: int = 0

    @property
    def changed(self) -> bool:
        """Whether anything happened worth reporting.

        The dispatch cron checks this and emits NOTHING when it is false.
        Silence-by-default is a hard requirement, not an optimization: a polling
        heartbeat only stays tolerable if it never speaks unless there is news.

        ``suppressed`` is deliberately NOT part of this. A suppression is the provider
        reporting that somebody already handled the alarm's disposition — the least
        newsworthy thing a cycle can find, and announcing it would make the heartbeat
        speak on exactly the signals an operator asked to stop hearing about.

        ``verifications`` counts only where the verdict is ``still_firing``. That verdict
        means the app previously reported an action as applied and the alarm is still
        going — a claim it made that turned out not to be true, which is the single most
        newsworthy thing this cycle can discover. ``cleared`` is the expected outcome and
        announcing it would make the heartbeat congratulate itself, and ``unknown`` is
        "we could not look", which a later cycle retries and which must never be
        broadcast as a finding.
        """
        return bool(
            self.claimed
            or self.released
            or any(v == VERIFY_STILL_FIRING for v in self.verifications.values())
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "claimed": [c.to_dict() for c in self.claimed],
            "released": self.released,
            "polled": self.polled,
            "unclaimed_remaining": self.unclaimed_remaining,
            "errors": self.errors,
            "changed": self.changed,
            "skipped_reason": self.skipped_reason,
            "suppressed": self.suppressed,
            "verifications": dict(self.verifications),
        }


def _config_int(key: str, default: int) -> int:
    raw = read_config().get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _attach_similar_safely(claimed: ClaimedIncident) -> None:
    """Resolve the shared vector store and attach similar lessons. Never raises.

    The store is resolved HERE rather than threaded through ``run_cycle`` because it is
    an optional enhancement: an instance with no vector store (model still downloading,
    or a deliberately minimal install) must dispatch exactly as before. Resolving lazily
    also keeps ``dispatch`` importable without pulling in SQLite/FAISS.

    Runs on a worker thread — the caller wraps it in ``asyncio.to_thread`` — because both
    the import and the search touch synchronous SQLite.
    """
    store_obj = None
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.vector_memory import VectorMemoryStore

        # Constructed per call, matching the convention in cli_commands/onboarding_import:
        # there is no shared singleton, and holding one open across cycles would keep a
        # SQLite handle alive for a feature that may never be used on this install.
        store_obj = VectorMemoryStore(embedding_dim=KiroCrewConfig.load().memory.embedding_dim)
        store_obj.init()
        attach_similar_lessons(claimed, store_obj)
    except Exception:  # noqa: BLE001 — no store, or a broken one, is a supported state
        logger.debug(
            "ops-mission-control: semantic recall unavailable; fingerprint matches stand",
            exc_info=True,
        )
    finally:
        if store_obj is not None:
            try:
                store_obj.close()
            except Exception:  # noqa: BLE001 — a close fault must not surface here
                logger.debug("ops-mission-control: vector store close failed")


def attach_similar_lessons(
    claimed: ClaimedIncident, store: Any, *, limit: int = 3
) -> ClaimedIncident:
    """Attach semantically similar ledger entries that the fingerprint missed.

    This is the payoff of indexing the ledger: a fingerprint match only fires when the
    SAME failure shape recurs, so a teammate's lesson about an equivalent failure on a
    different resource is invisible to it. Semantic recall surfaces that lead.

    Deliberately does NOT call ``record_use``. A similar hit is not a use — inflating the
    count would corrupt the signal that decides ``is_fast_path``, which is the one thing
    standing between a remembered fix and a confidently-wrong one.

    Fingerprint matches are excluded from the result, so the brief never lists the same
    entry twice under two different confidence framings.

    ``store`` is injected (never imported here) so dispatch has no hard dependency on the
    vector store: a caller without one passes ``None`` and this is a no-op.
    """
    if store is None:
        return claimed
    query = f"{claimed.incident.signal.title} {claimed.incident.signal.resource}".strip()
    if not query:
        return claimed
    try:
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_index

        rows = ledger_index.search_similar(store, query, limit=limit + len(claimed.matches))
    except Exception:  # noqa: BLE001 — semantic recall is additive, never required
        logger.exception("ops-mission-control: similar-lesson lookup failed")
        return claimed

    if not rows:
        return claimed

    # Map the indexed text back to real ledger entries. The index stores
    # "<pattern> — fix: <fix>", so match on the pattern prefix rather than trying to
    # reverse the format — a text round-trip would break the moment entry_text changes.
    matched_ids = {m.entry_id for m in claimed.matches}
    by_text: dict[str, LedgerEntry] = {}
    for entry in ledger.read_entries():
        if entry.entry_id in matched_ids:
            continue
        by_text[ledger_index.entry_text(entry)] = entry

    found: list[LedgerEntry] = []
    for row in rows:
        # Distinct name: `entry` is already bound as a LedgerEntry by the loop above,
        # and reusing it for an Optional is what mypy flagged.
        hit = by_text.get(str(row.get("text", "")))
        if hit is not None and hit not in found:
            found.append(hit)
        if len(found) >= limit:
            break
    claimed.similar = found
    return claimed


def attach_ledger_matches(incident: Incident) -> ClaimedIncident:
    """Bind what we already know about this failure to a fresh incident.

    The fingerprint lookup is what makes the second occurrence of a failure
    cheaper than the first. Matching also *records the use*, so an entry that
    keeps proving useful climbs the ranking and an entry nobody needs decays out
    during hygiene — the ledger stays a working index rather than an archive.
    """
    provider_key = incident.signal.provider_key
    matches = ledger.match(incident.signal.fingerprint, provider_key=provider_key)
    # Whether the best match was decided by the provider's own identity or by our shape
    # hash, captured BEFORE record_use binds the key (which would make every match look
    # exact from the second occurrence onward and erase the distinction).
    exact_ids = {m.entry_id for m in matches if ledger.is_exact_match(m, provider_key)}
    # Record the use and keep the UPDATED entry: rendering the pre-increment copy
    # would show "used 0×" for a pattern this very incident just used, which
    # misreports the one number that tells a responder how proven a fix is.
    recorded: list[LedgerEntry] = []
    for entry in matches:
        updated = ledger.record_use(entry.entry_id, incident.signal.fingerprint, provider_key)
        recorded.append(updated or entry)
    matches = recorded

    fast_path = ledger.is_fast_path(matches)
    if matches:
        store.update_fields(incident.incident_id, ledger_matches=[m.entry_id for m in matches])
        incident.ledger_matches = [m.entry_id for m in matches]
    return ClaimedIncident(
        incident=incident,
        matches=matches,
        fast_path=fast_path,
        exact_match_ids=sorted(exact_ids),
    )


def verify_pending_actions(
    signals: list[Signal],
    health: dict[str, dict[str, Any]],
    *,
    now: str | None = None,
) -> dict[str, str]:
    """Re-read the world for every incident whose post-action recheck has come due.

    **The gap this closes.** ``routes._handle_action`` awaited ``sink.execute``, audited
    the call, returned, and stopped. Nothing ever looked again, so ``ActionResult.ok``
    meant exactly one thing: the provider returned 2xx. That is not the claim the board
    was making. Checkmk dispatches commands asynchronously through Livestatus and its own
    docs warn that a 2xx "only indicates whether the request was successfully
    transmitted, NOT whether it was in fact successfully executed"; Nagios's command pipe
    returns nothing at all. So the app could report a suppression or a resolve as applied
    while the alarm kept firing, with no code anywhere in a position to notice.

    **A failed poll is NOT evidence the action worked, and that is the whole risk here.**
    This function reuses the signals and ``poll_health`` from the poll the cycle already
    did, and refuses to reach any verdict for a source whose poll did not succeed —
    exactly the rule ``reconcile.md`` Pass 1 step 3 states for resolving on absence, for
    exactly the same reason. Reading "absent from firing" as "the fix landed" would be
    that bug in a new place, and here it would be worse: it would also feed a FALSE
    success into the ledger's track record, making a fix that never worked look proven.
    ``unknown`` is therefore recorded and left OPEN, so a later cycle retries it.

    **Only some actions are verifiable, and the rest say so.** An ``ack`` leaves an alert
    firing by design (``normalize_state`` maps ``acknowledged`` onto ``firing`` on
    purpose), so firing state carries no information about whether the ack landed —
    those are marked ``not_checkable`` at execution time in ``routes._handle_action`` and
    never reach this function.

    Runs on a worker thread (the caller wraps it): it reads the dispatch index and may
    write both the index and the ledger.

    Returns ``{incident_id: verdict}`` for the incidents it decided this cycle only.
    """
    stamp = now or utc_now_iso()
    firing_ids = {s.id for s in signals if s.state == STATE_FIRING}
    # Parked signals are tracked separately from both buckets. See the branch below: they
    # are the one absence that must not be read as recovery, and after a `silence` this app
    # issued they are what SUCCESS looks like — which made `cleared` a self-congratulation.
    # Keyed by id and carrying the SIGNAL, not just its id, because the attribution below
    # has to come from THIS poll. The incident's own `signal` is the snapshot taken when it
    # was claimed — before anybody parked it — so reading `suppressed_by` off the incident
    # would reliably produce an empty attribution in exactly the case that needs one.
    suppressed_now = {s.id: s for s in signals if s.state == STATE_SUPPRESSED}
    verdicts: dict[str, str] = {}

    for incident in store.read_index().values():
        if incident.verification not in OPEN_VERIFICATIONS:
            continue
        if not incident.verify_after or incident.verify_after > stamp:
            # Not due yet. String comparison is sound because every timestamp in this app
            # is the same fixed-width UTC ``%Y-%m-%dT%H:%M:%SZ`` form (``utc_now_iso``).
            continue

        source_health = health.get(incident.signal.source)
        if not source_health or not source_health.get("ok"):
            reason = str((source_health or {}).get("detail") or "it has not been polled")
            verdict = VERIFY_UNKNOWN
            detail = (
                f"Could not verify: the last poll of {incident.signal.source} did not "
                f"succeed ({reason}), so the signal's absence proves nothing. Will "
                f"re-check on a later cycle."
            )
        elif not source_health.get("snapshot", True) and incident.signal.id not in firing_ids:
            # A non-snapshot source (the webhook spool) answered, and the signal is absent —
            # which proves nothing. `poll` now PEEKS rather than drains, but the conclusion
            # is unchanged: a signal leaves the spool as soon as an incident claims it
            # (`webhook.ack`), and the incident being verified here is precisely one that did
            # claim. So its own claim is what removed it, and it is missing from every cycle
            # after that whether or not anything changed at the sender. "Polled ok and
            # absent" is still not the recovery it is for a polled API — a push source never
            # re-asserts a fault, it only announces one.
            #
            # This is the same absence-is-not-evidence rule as the failed-poll branch above,
            # reached through a SUCCESSFUL poll — which is exactly why the `ok` guard could
            # not catch it, and why one cycle after any webhook delivery an action verified
            # as `cleared` ("the resolve held") with the fault still live.
            verdict = VERIFY_UNKNOWN
            detail = (
                f"Could not verify: {incident.signal.source} delivers by push and its spool "
                f"is drained by each poll, so this signal's absence is expected whether or "
                f"not the {incident.last_action} worked. It can only be confirmed by the "
                f"sender delivering the signal again, or not."
            )
        elif incident.signal.id in suppressed_now:
            # A signal a human parked at the provider is the THIRD reason it is absent from
            # `firing`, and reading it as `cleared` was strictly the worst of the three
            # readings: after a `silence` this app itself issued, "the provider now reports
            # it suppressed" is exactly what a SUCCESSFUL silence looks like — so the
            # recheck congratulated the app on hiding a live fault, and `use_count` was the
            # number that grew. Verified before fixing: silence a firing webhook signal,
            # re-poll with `state=suppressed`, and the verdict was `cleared` with the detail
            # "the silence held".
            #
            # `unknown` rather than `still_firing`, because the underlying condition is
            # genuinely unobservable while the alarm is muted — the provider has stopped
            # evaluating it into a firing state. `unknown` is in OPEN_VERIFICATIONS, so the
            # recheck retries once the suppression lifts, which is the only moment the
            # question can actually be answered.
            verdict = VERIFY_UNKNOWN
            # From THIS poll, not from the incident's claim-time snapshot — which predates
            # the parking and would name nobody.
            parked_by = suppressed_now[incident.signal.id].suppressed_by
            detail = (
                f"Could not verify: {incident.signal.source} now reports this signal as "
                f"parked at the provider"
                + (f" by {parked_by}" if parked_by else "")
                + f", so it is not being evaluated into a firing state and its absence "
                f"says nothing about whether the {incident.last_action} worked. Will "
                f"re-check once the suppression lifts."
            )
        elif incident.signal.id in firing_ids:
            verdict = VERIFY_STILL_FIRING
            detail = (
                f"Still firing at {incident.signal.source} after the {incident.last_action} "
                f"reported success. The provider accepted the request; the condition did "
                f"not change."
            )
        else:
            verdict = VERIFY_CLEARED
            detail = (
                f"No longer firing at {incident.signal.source}, which polled successfully "
                f"— the {incident.last_action} held."
            )

        try:
            store.update_fields(
                incident.incident_id,
                verification=verdict,
                verification_detail=detail,
            )
        except (KeyError, ValueError):
            # Pruned or raced away between the read and the write. Not an error worth
            # failing a cycle for, and nothing else in the cycle depends on it.
            logger.debug(
                "ops-mission-control: could not record verification for %s",
                incident.incident_id,
                exc_info=True,
            )
            continue
        verdicts[incident.incident_id] = verdict

        if verdict == VERIFY_STILL_FIRING:
            _record_verification_misses(incident)

    return verdicts


def _record_verification_misses(incident: Incident) -> None:
    """Charge a miss to every ledger entry this incident's fix was drawn from.

    This is the ONLY producer of ``miss_count``, and the standard of evidence is
    deliberately narrow: an action executed, the recheck ran against a source that
    actually answered, and the signal is still firing. Anything looser — an incident that
    merely recurred, a poll we could not make — would demote entries on inference, and a
    knowledge base that demotes on inference is as harmful as one that never demotes.

    Charged to every entry in ``ledger_matches`` rather than to a single "the one we
    used", because nothing records which match the investigation actually applied
    (``Incident.proposed_action`` is declared and never assigned). Attributing the miss to
    a guess would be worse than attributing it to all of them: ``MAX_MATCHES_PER_SIGNAL``
    is 3, so the blast radius is bounded, and a match that keeps being shown for a failure
    that keeps coming back has genuinely not earned the fast path either.

    Never raises — a ledger fault must not fail the verification that already got written.
    """
    for entry_id in incident.ledger_matches:
        try:
            ledger.record_miss(entry_id)
        except OSError:
            logger.exception("ops-mission-control: could not record a ledger miss for %s", entry_id)


async def _pull_shared_repo_safely() -> str:
    """Fetch the shared ledger repo, tolerating every failure. Returns an outcome string.

    Exists so EVERY instance refreshes ``rotation.yaml`` on its own heartbeat, not just
    the primary. Deferred import because ``ledger_sync`` pulls in the git/sandbox
    machinery and this module is imported at gateway start.

    Returns "" when sync is not configured — the overwhelmingly common case for a solo
    install, where this must cost nothing at all. ``sync_safely`` already checks that and
    swallows its own faults; the extra guard here is so an import failure (a partial
    install, a missing optional dependency) also degrades to a no-op rather than taking
    down the dispatch cycle, which is the one thing that must keep running.
    """
    try:
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        return await ledger_sync.sync_safely(direction="pull")
    except Exception:  # noqa: BLE001 — a sync fault must never stop incident response
        logger.exception("ops-mission-control: shared-repo pull failed before the cycle")
        return ""


async def run_cycle(
    *,
    max_claims: int | None = None,
    slack_client: Any | None = None,
    state: Any | None = None,
) -> CycleResult:
    """Run one dispatch cycle.

    Safe to call concurrently with itself: claims are atomic, so a second caller
    simply finds nothing left to claim.

    ``slack_client`` is the gateway's live Slack client, passed in by the caller
    (Kiro Crew has no global state accessor). None simply means the pin board is
    not mirrored this cycle.

    ``state`` is the gateway's ``DashboardState``, threaded in for the same reason and
    used only for the local notification bus (``notify_out``). None means no desktop
    notification this cycle, which is what every non-gateway caller gets.
    """
    registry = get_registry()

    # Refresh the shared repo BEFORE reading the shift, not after.
    #
    # ``rotation.yaml`` travels in the ledger repo, and the only other caller of
    # ``sync_safely`` is the daily hygiene pass — which is now correctly gated to the
    # primary instance. So a NON-primary instance had no code path that ever fetched the
    # schedule: it kept arming (or not) off whatever it last saw, which is exactly the
    # double-claim the single-owner model exists to prevent, reintroduced by the
    # transport rather than by the model.
    #
    # Ordering is the whole point. Pulling after ``resolve_shift`` would still gate this
    # cycle on the stale file and only help the NEXT one — so a shift swap would take
    # effect one heartbeat late on every instance, and the window where two teammates
    # both believe they are on call is precisely the window that matters.
    #
    # Pull-only, and never fatal: ``sync_safely`` swallows every fault and returns a
    # short outcome string, so an unreachable remote degrades to "work from what we
    # already know". Shared memory improving an investigation is worth having; it is
    # never worth losing a claim over. No push here — publishing is the primary's job,
    # and a per-cycle push from every instance is the concurrent-write problem again.
    await _pull_shared_repo_safely()

    # Stop asking about proposals nobody answered. Runs on the heartbeat rather than
    # in a cron of its own because a TTL that only advances when a separate job fires
    # is a TTL that silently stops existing when that job is paused — and every other
    # cron here is pausable by design.
    expired = await asyncio.to_thread(store.expire_stale_proposals)
    if expired:
        logger.info(
            "ops-mission-control: %d proposal(s) expired unanswered: %s",
            len(expired),
            ", ".join(expired),
        )

    # Respect the rotation gate here as well as in the cron tier. The tier is the
    # cheap outer gate (paused crons cost nothing); this is the correctness one,
    # since a manual or misconfigured trigger must not dispatch off-shift.
    shift = await registry.resolve_shift()
    # Off the loop: `tier_states` -> `is_primary` -> `_schedule_me` -> `resolve_login` can shell
    # out to `gh api user` (a 10s-timeout subprocess) when a committed schedule names a
    # `leader:` and no `schedule-file.github_login` is set. `run_cycle` is the dispatch
    # heartbeat, so run inline it freezes the whole loop — chat turn and liveness heartbeat
    # included — for up to 10s. `_login_cache` is only a mitigation: `resolve_shift` wraps each
    # source in `asyncio.wait_for`, and a timeout cancels the awaiter while the worker keeps
    # running, so the cache may still be cold here. Found in review.
    tiers = await asyncio.to_thread(rotation.tier_states, shift)
    if not tiers.get(rotation.TIER_ON_SHIFT, True):
        return CycleResult(skipped_reason="off shift — on_shift tier is disarmed")

    # Say WHY nothing happened when no source is configured. Every caller otherwise
    # has to infer it from `polled == 0`, and the two conclusions are opposites:
    # "nothing is wrong" versus "nothing is watching". The dashboard derived this
    # itself, but an agent hitting POST /dispatch on a fresh install got a silent
    # empty result — the first thing a new user does, and the one moment the app most
    # needs to admit it is not set up yet.
    if not registry.configured_signal_sources():
        return CycleResult(
            skipped_reason=(
                "No signal source is configured, so nothing is being watched. "
                "Connect one in Settings → Providers."
            )
        )

    # Snapshot health BEFORE the poll so the notification below can fire on the EDGE.
    # Without the before-picture the only available test is "is it failing now", which
    # is true on every one of the 30 heartbeats an hour-long outage spans — the
    # "unchanged condition" SKILL.md forbids re-notifying for.
    health_before = registry.poll_health()

    signals, errors = await registry.poll_all()
    firing = [s for s in signals if s.state == STATE_FIRING]
    # Counted here, and that is the ONLY thing done with it. The claim filter above is
    # unchanged on purpose: "a suppressed signal must not be claimed" holds by
    # construction because a new state simply is not `firing`, so there is no second
    # predicate for a future edit to forget.
    suppressed = sum(1 for s in signals if s.state == STATE_SUPPRESSED)

    # Off-loop: a full parse of the incident index, and `run_cycle` is a coroutine driven by
    # the heartbeat. Measured elsewhere in this app at 42ms/1k and 188ms/5k incidents, so on a
    # busy install this stalled the loop on every cycle.
    #
    # `routes.py` was swept for exactly this class last round and this file was not — the
    # narrowness my own guard test was written to prevent, one module over. The guard now
    # covers `dispatch.py` too. Found in review.
    index = await asyncio.to_thread(store.read_index)
    # A signal is "owned" only by an OPEN incident. A closed one (resolved/escalated) must
    # not suppress a fresh firing — `signal.id` is stable for the alarm's lifetime, so
    # treating terminal as owned means the app permanently stops responding to any failure
    # it has already handled once, and the compounding-memory fast path (which can only pay
    # off on a SECOND occurrence) becomes unreachable.
    #
    # This is a CHEAP PRE-FILTER in front of `store.claim`, and fixing `claim` alone was not
    # enough: this line discarded the recurrence before `claim` ever saw it. Caught only by
    # driving a real gateway — 408 unit tests passed because they call `claim` directly and
    # never go through `run_cycle`'s filter. Two places encoded the same rule; both needed
    # it. `stale` stays claimable for its own reason (re-pickup in place).
    owned = {
        inc.signal.id
        for inc in index.values()
        if inc.status != STATUS_STALE and inc.status not in TERMINAL_STATUSES
    }
    candidates = [s for s in firing if s.id not in owned]

    limit = (
        max_claims
        if max_claims is not None
        else _config_int(_CONFIG_MAX_CLAIMS, DEFAULT_MAX_CLAIMS_PER_CYCLE)
    )

    claimed: list[ClaimedIncident] = []
    for signal in candidates[:limit]:
        result = await asyncio.to_thread(_claim_one, signal)
        if result is not None:
            # Gather evidence HERE, on the credentialed gateway, rather than letting
            # the investigating agent fetch it — the agent has no AWS credentials and
            # deliberately gets none (see ``investigation_brief``). Bounded by the
            # budget and redacted at the registry chokepoint. Failure is non-fatal: an
            # investigation with no evidence is worse than one with, but far better
            # than a claim we drop because a provider was slow.
            result.evidence = await gather_evidence_safely(registry, signal)
            # Semantic recall from the (git-synced) ledger index. Off the event loop
            # because it touches SQLite/FAISS, and best-effort: a missing or broken
            # index must leave the fingerprint matches untouched.
            await asyncio.to_thread(_attach_similar_safely, result)
            claimed.append(result)

    # Acknowledge the push-delivered signals we took DURABLE ownership of, and only those.
    # This is the one place the webhook spool shrinks; `poll` peeks (see `webhook.peek`).
    #
    # Placed after the claim loop rather than inside it so the unit of consumption is "an
    # incident exists on disk for this id". Everything else stays spooled on purpose:
    # - signals past `[:limit]`, which this cycle never looked at;
    # - signals filtered out as already `owned` (their incident already acked them);
    # - signals whose `_claim_one` returned None (another instance won the race — it acks
    #   its own copy in its own cycle).
    #
    # Acking ids rather than the whole batch is what makes the per-cycle cap safe: before,
    # a burst larger than `limit` had its remainder destroyed by the very poll that
    # delivered it.
    # Ack what we claimed THIS cycle **and** everything an open incident already owns.
    #
    # `owned` ids are filtered out of `candidates` above, so they are never claimed and — with
    # only the `claimed` set acked — never left the spool. A sender that redelivers while an
    # investigation is in flight (Alertmanager repeats every group_interval; a webhook script
    # retries) therefore accumulated copies of a signal already being worked, and on a full
    # 200-entry spool those evicted a NEW unclaimed alert. Found in review, and the same shape
    # as the manual-claim gap one round earlier: every place a signal becomes or already IS
    # durable has to acknowledge it, not just the place that claims it.
    #
    # Safe by the same argument the durability rule rests on: an id in `owned` has an incident
    # on disk, so dropping the spooled copy loses nothing — and if that incident later goes
    # terminal or stale, the id leaves `owned` and the next genuine delivery is claimable again.
    acked = webhook.ack({result.incident.signal.id for result in claimed} | owned)
    if acked:
        logger.debug("ops-mission-control: acked %d webhook signal(s) after claim", acked)

    # Post-action verification rides on the poll this cycle ALREADY made — no extra
    # provider call, which is what keeps "re-read the signal after acting" inside the
    # heartbeat's flat cost instead of needing a cron of its own. It reads the FULL signal
    # list rather than `firing`, because a source that returned a signal in any other
    # state still answered, and it consults the same `poll_health` that decides whether
    # absence means anything at all.
    verifications = await asyncio.to_thread(verify_pending_actions, signals, registry.poll_health())

    stale_after = _config_int(_CONFIG_STALE_AFTER, DEFAULT_STALE_AFTER_SECS)
    released = await asyncio.to_thread(
        store.sweep_stale,
        stale_after,
        # 0 / unset means "derive from the working threshold", which is what
        # ``sweep_stale`` does for ``None``.
        _config_int(_CONFIG_NEEDS_HUMAN_STALE_AFTER, 0) or None,
    )

    # Mirror newly-claimed incidents onto the Slack pin board. After the claim, so
    # a Slack outage can never cost us a claim; each send is already failure-
    # tolerant internally.
    if claimed:
        await slack_out.publish_all([c.incident for c in claimed], slack_client)

    # Desktop notifications for the two things this cycle can discover that nobody is
    # otherwise told about. Deliberately NOT one per claim: a claim is the heartbeat
    # working, it already shows on the board and in Slack, and notifying it would make
    # this channel the heartbeat feed the design refuses. After the claim and the Slack
    # mirror, so a bus fault can cost neither.
    _notify_cycle_changes(state, health_before, registry.poll_health(), released)

    if claimed or released:
        sel().log_api_access(
            caller="core:ops-mission-control",
            operation="dispatch_cycle",
            outcome="success",
            resources=(
                f"claimed={[c.incident.incident_id for c in claimed]} " f"released={released}"
            ),
        )

    return CycleResult(
        claimed=claimed,
        released=released,
        polled=len(firing),
        unclaimed_remaining=max(0, len(candidates) - len(claimed)),
        errors=errors,
        suppressed=suppressed,
        verifications=verifications,
    )


def _notify_cycle_changes(
    state: Any | None,
    health_before: dict[str, dict[str, Any]],
    health_after: dict[str, dict[str, Any]],
    released: list[str],
) -> None:
    """Push a desktop notification for each STATE CHANGE this cycle produced.

    Two conditions, both edge-triggered:

    - a source that answered (or had never been polled) and now does not. A source
      that was ALREADY failing pushes nothing: that is the unchanged condition
      ``SKILL.md``'s noise discipline forbids re-notifying for, and at a 120-second
      heartbeat an hour of downtime would otherwise be 30 identical toasts.
    - each incident ``sweep_stale`` released. Release is a one-shot event, so there is
      no edge to compute.

    A source that has never been polled counts as "was ok" on purpose: its FIRST
    failure is news (the operator just configured it and it does not work), and
    treating unknown as already-failing would swallow exactly that notification.

    Never raises — the cycle's result must not depend on the notification centre.
    """
    if state is None:
        return
    try:
        for source_id, entry in health_after.items():
            if entry.get("ok"):
                continue
            if health_before.get(source_id, {}).get("ok") is False:
                continue
            notify_out.notify_source_unhealthy(
                state, source_id, str(entry.get("detail") or "the last poll failed")
            )
        if released:
            notify_out.notify_incidents_released(state, list(released))
    except Exception:  # noqa: BLE001 — notifying is not the work
        logger.exception("ops-mission-control: cycle notifications failed")


async def gather_evidence_safely(registry: Any, signal: Signal) -> list[Evidence]:
    """Gather provider evidence, treating any fault as "no evidence".

    ``gather_evidence`` already isolates per-adapter failures and timeouts, so this
    only catches a fault in the fan-out itself. Kept separate so the claim loop reads
    as one line and the non-fatal intent is explicit.
    """
    try:
        return await registry.gather_evidence(signal, EvidenceBudget())
    except Exception:  # noqa: BLE001 — evidence is context, never a gate
        logger.exception("ops-mission-control: evidence gathering failed for %s", signal.id)
        return []


def _claim_one(signal: Signal) -> ClaimedIncident | None:
    """Claim one signal and attach its ledger context (runs off the event loop)."""
    mode = rotation.resolve_mode(signal)
    incident = store.claim(signal, operating_mode=mode)
    if incident is None:
        # Lost the race — normal, not an error. Another heartbeat owns it.
        return None
    try:
        return attach_ledger_matches(incident)
    except Exception:  # noqa: BLE001 — a ledger fault must not lose the claim
        logger.exception("ops-mission-control: ledger match failed for %s", incident.incident_id)
        return ClaimedIncident(incident=incident)


def _safe_field(text: str) -> str:
    """Redaction floor for a provider-controlled signal field before the MODEL sees it.

    ``registry.gather_evidence`` already redacts every evidence BODY centrally, but the brief
    also prints the signal's own metadata — title, resource, url — and those were rendered raw.
    A signed webhook is accepted from anything able to POST JSON, and a console link can carry
    a token in its query string, so provider metadata is exactly as untrusted as a log line
    fetched from a provider. The brief then goes into the agent's context, and from there into
    the transcript and any session artifact. Found in review; the evidence on the same code path
    was covered while the metadata beside it was not.

    Both passes, and ``redact_via_context`` rather than ``security.redact`` directly, for the
    reasons ``gather_evidence`` documents: the two redactors cover different token families, and
    the context shim makes a loaded companion's declared patterns apply while an enterprise host
    that fails to compose its companion fails CLOSED.
    """
    if not text:
        return text
    from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import redact_tokens
    from kiro_crew.platform import redact_via_context

    return redact_tokens(redact_via_context(text))


def investigation_brief(claimed: ClaimedIncident) -> str:
    """Render the context an investigating agent should start from.

    Kept here rather than in the SOP prompt so the *facts* are assembled
    deterministically and only the reasoning is left to the model — a prompt that
    asks an agent to go fetch its own context spends a turn on work Python
    already did.

    **Evidence is brokered, not delegated.** The investigating agent's sandbox has no
    AWS credentials, so it cannot read alarm history or logs itself — and the answer
    is NOT to give it any. The gateway already holds the operator's profile and
    already redacts every gathered body at a single chokepoint
    (``registry.gather_evidence``); handing the agent scoped, redacted *text* gives it
    what it needs to diagnose while keeping credentials in one place, which is what
    least-privilege guidance asks for. An agent with its own AWS profile would be a
    second credential holder whose reads nothing redacts and whose scope nothing
    bounds.

    So the flow is: gateway gathers (credentialed, bounded, redacted) → brief carries
    the text → agent reasons. Before this the brief carried no evidence at all, so an
    AWS investigation had signal metadata and ledger hints and nothing else.
    """
    inc = claimed.incident
    sig = inc.signal
    # PROVIDER-controlled fields go through `_safe_field`; ours do not need to.
    # `source`/`severity`/`fired_at`/`fingerprint` are values this app assigns or derives,
    # `operating_mode` is the operator's own setting — redacting them would only risk
    # corrupting a value the agent needs to reason about.
    lines = [
        f"Incident {inc.incident_id} — {_safe_field(sig.title)}",
        "",
        f"Source:      {sig.source}",
        f"Severity:    {sig.severity}",
        f"Resource:    {_safe_field(sig.resource) or '—'}",
        f"Fired at:    {sig.fired_at}",
        f"Fingerprint: {sig.fingerprint}",
        f"Mode:        {inc.operating_mode}",
    ]
    if sig.url:
        lines.append(f"Provider:    {_safe_field(sig.url)}")

    lines.append("")
    if not claimed.matches:
        lines.append(
            "No prior pattern matched this fingerprint — this failure is new to the "
            "ledger. If you work out a reusable fix, record it so the next "
            "occurrence is cheap."
        )
    else:
        if claimed.fast_path:
            lines.append(
                "KNOWN PATTERN (verified, high confidence, and it has worked before) — "
                "confirm it still applies, then propose this fix rather than "
                "re-deriving it:"
            )
        else:
            lines.append(
                "Possible prior patterns — treat these as hypotheses to test, not "
                "answers (none has cleared the fast-path bar: verified, high "
                "confidence, used at least "
                f"{ledger.MIN_USES_FOR_FAST_PATH}×, and never observed to fail):"
            )
        exact = set(claimed.exact_match_ids)
        for entry in claimed.matches:
            # Say HOW this matched. An exact hit is the provider's own identity for the
            # failure; a shape hit is our text heuristic, which strips bare numbers and
            # therefore cannot distinguish a 4xx alarm from a 5xx one on the same
            # resource. An agent told only "matched" cannot weigh those differently.
            how = (
                "exact provider identity"
                if entry.entry_id in exact
                else "same shape (heuristic — verify it is really this failure)"
            )
            lines.append(
                f"  • [{entry.confidence}/{entry.trust}, used {entry.use_count}×, "
                f"{how}] {entry.pattern}"
            )
            lines.append(f"      fix: {entry.fix}")
            if ledger.is_demoted(entry):
                # Stated on its own line, in the imperative, because the ranked list
                # itself reads as an endorsement. This entry has been cited and the same
                # failure came back — an agent handed only "used 4×" would read the count
                # as corroboration when part of it is the record of this fix not holding.
                lines.append(
                    f"      WARNING: this fix was applied and the signal was still "
                    f"firing afterwards {entry.miss_count}× (most recently "
                    f"{entry.last_miss or 'unknown'}). Do not propose it without "
                    f"establishing why it failed, or say plainly that you are retrying "
                    f"something that has already failed."
                )

    if claimed.similar:
        # Framed as leads, NOT patterns. These reached the brief by semantic similarity,
        # so their fingerprints do NOT match this signal — the wording has to stop the
        # agent applying one as though this failure had recurred, which is exactly the
        # mistake a ranked list invites.
        lines.append("")
        lines.append(
            "Related lessons from elsewhere in the ledger (semantic match — the "
            "fingerprints do NOT match this signal, so treat each as a lead worth "
            "checking, never as a fix to apply):"
        )
        for entry in claimed.similar:
            lines.append(f"  • [{entry.confidence}/{entry.trust}] {entry.pattern}")
            lines.append(f"      fix: {entry.fix}")

    # The no-credentials statement is UNCONDITIONAL, and deliberately so. It used to
    # live only inside the ``if claimed.evidence`` branch below, which meant the one
    # case that most needs it — no evidence gathered — was the one case that never got
    # it. An agent handed an AWS incident and no explanation reasonably assumes it
    # should go look itself, and then spends its whole turn re-running
    # ``aws … --profile …`` against a credential chain it cannot reach (observed on
    # INV-1/INV-2: repeated NoCredentials, no diagnosis). Saying it once, always, costs
    # two lines and removes a guaranteed dead end.
    lines.append("")
    lines.append(
        "Credentials: you have NONE for the systems in this incident, by design. The "
        "gateway holds the operator's profile and brokers reads to you already "
        "redacted, so it stays the single credential holder. Do not run `aws`, "
        "`datadog`, or any provider CLI — it will fail, and a failure loop is not a "
        "diagnosis. If a read you need is missing, say which one and why; an operator "
        "configures it as an evidence source rather than handing you a profile."
    )

    if claimed.evidence:
        lines.append("")
        lines.append("Provider evidence, already gathered for you (redacted):")
        # Bounded, and SAY SO when truncating: an agent that silently receives half a
        # log dump will reason confidently about a partial picture, which is worse
        # than knowing the view is clipped.
        spent = 0
        for item in claimed.evidence:
            if spent >= MAX_BRIEF_EVIDENCE_CHARS:
                lines.append("")
                lines.append(
                    f"  (evidence truncated at {MAX_BRIEF_EVIDENCE_CHARS} chars — "
                    "further items omitted; narrow the configured log groups if you "
                    "need to see them)"
                )
                break
            body = item.body[:MAX_BRIEF_EVIDENCE_ITEM_CHARS]
            clipped = len(item.body) > len(body)
            lines.append("")
            lines.append(f"  --- {item.title} ({item.source}/{item.kind}) ---")
            for line in body.splitlines():
                if line.strip():
                    lines.append(f"      {line}")
            if clipped:
                lines.append("      (item truncated)")
            spent += len(body)

    lines.append("")
    lines.append(
        "Authority reminder: only 'act' mode may execute a provider write, and "
        "only where a user rule grants it. Never run a remediation command "
        "against infrastructure — diagnose and propose; the human applies."
    )
    return "\n".join(lines)
