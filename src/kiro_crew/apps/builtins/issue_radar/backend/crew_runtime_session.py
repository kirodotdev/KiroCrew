"""A crew's app-owned session.

Its slot key and title, liveness and the freshly read crew record, the scoped
auto-approve grant it runs under, attaching or launching it with its turn model, and
dispatching a turn. Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.crew_runtime`.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import Any

from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.safety_override import safety_override

from . import crew_model, crew_store, provider
from .crew_runtime_prompt import compose_turn_prompt_async

try:  # the autonudge service is feature-flagged; the runtime degrades without it
    from kiro_crew.autonudge import get_instance as _autonudge_instance
except ImportError:  # pragma: no cover - defensive
    _autonudge_instance = None  # type: ignore[assignment]

try:
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    _HAS_SECURITY = True
except ImportError:  # pragma: no cover - defensive
    _HAS_SECURITY = False


logger = logging.getLogger("kirocrew.app.issue-radar")


APP_NAME = "issue-radar"


#: Crews get a turn on this idle gap when the sweep has nothing to report. The
#: watcher is the real scheduler (it fires on an actual signal); this is the
#: fallback clock that lets an idle crew pick up NEW work, so it is deliberately
#: slow. ``autonudge`` clamps it to [15s, 24h].
DEFAULT_IDLE_SECS = 300


# ── session launch / attach ─────────────────────────────────────────────────


def stop_sentinel_path(owner: str, repo: str, crew_id: str, root: Path | None = None) -> Path:
    """Kill switch for the crew's loop. Lives in the crew's own item dir, which
    ``list_work_items`` globs for ``*.json`` only — so this file is invisible to it."""
    d = crew_store.crews_dir(owner, repo, root) / crew_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "STOP"


def _slot_title(owner: str, repo: str, crew: dict[str, Any], slot_key: str) -> str:
    """A human title for the worker slot, redacted.

    The crew name is FREE TEXT in the create dialog, and a title is persisted and
    broadcast, so it gets the same treatment as ``auto_research``'s campaign name:
    redact, and if the redactors are unavailable fail CLOSED to the slot key, which
    carries no user content.
    """
    raw = f"{crew.get('name') or slot_key} · {owner}/{repo}"
    if not _HAS_SECURITY:
        return slot_key
    raw, _ = redact_exfiltration_urls(raw)
    raw, _ = redact_credentials(raw)
    return raw


# ── the auto-approve grant ──────────────────────────────────────────────────
#
# A crew runs with nobody watching, so its tool calls have to be approved by
# something other than a person. What that something must NOT be is
# ``slot._trust``: that flag is the interactive "trust this session" grant, it
# never expires, and nothing audits it as a grant because the human's click IS
# the record. A backend stamping it produces an unbounded auto-approval with no
# activation trail — which is why ``spec_builder`` was made to stop doing exactly
# that. Crews take the TASK RUNNER's shape instead (scope
# ``taskrunner:{task_id}:autoapprove``): the record's ``unattended`` flag is only
# the operator's intent, and the live decision is a ``SafetyOverride`` scoped
# grant that is SEL-audited fail-closed before it exists, expires by itself, and
# is re-checked on every single approval.

# Recorded on every SEL entry for a crew grant, so an auditor can tell an
# unattended crew's grant from a human's dashboard click.
GRANT_SOURCE = "issue-radar-crew"

# How long ONE crew grant lives. This is not the renewal interval — the watchdog
# re-derives the grant every ``watch.POLL_INTERVAL_SEC`` (60s) — it is how long
# the grant outlives a watchdog that has STOPPED, which is the hole a bare
# in-memory flag left open: nothing clears a granted flag when the sweep dies but
# the process keeps running. Fifteen cycles of slack, so a stalled or slow sweep
# never strips trust from a crew mid-turn, while a dead watchdog stops the crew in
# minutes instead of at ``SafetyOverride``'s 6h ad-hoc default or its 24h ceiling.
TRUST_TTL_SECS = 900


def autoapprove_scope(crew_id: str) -> str:
    """``SafetyOverride`` scope key holding one crew's auto-approve grant.

    Deliberately shaped like ``taskrunner:{task_id}:autoapprove``. Enforcement is
    ``safety_override().is_scope_active(scope)``; the crew record's ``unattended``
    flag carries no authority of its own.
    """
    return f"crew:{crew_id}:autoapprove"


def _hold_grant(scope: str) -> bool:
    """Extend a live grant, or mint a freshly-audited one. True if usable after.

    RENEWAL IS A SLIDE, NOT A RE-ACTIVATION, and that choice is load-bearing twice
    over. Re-activating every 60s would write a critical SEL entry per cycle —
    1,440 a day per crew, burying the one activation an auditor came to find — and
    it would reset ``activated_at``, so ``SafetyOverride``'s 24h ceiling could
    never be reached and "expiring" would be a word rather than a bound.
    ``renew_scoped`` is documented as not audited per call for the same reason: it
    extends an already-audited grant within its already-audited ceiling.

    When the ceiling IS reached the slide is refused, and rather than let the grant
    lapse under a crew that is mid-turn, a NEW grant is minted — which goes through
    ``activate_scoped`` and so is freshly audited. A crew running for a week
    therefore leaves one activation record per day, not one for all time.
    """
    so = safety_override()
    if so.is_scope_active(scope):
        if so.renew_scoped(scope, source=GRANT_SOURCE, ttl=TRUST_TTL_SECS).renewed:
            return True
    return bool(so.activate_scoped(scope, source=GRANT_SOURCE, ttl=TRUST_TTL_SECS).active)


def sync_trust(slot: Any, crew: dict[str, Any]) -> bool:
    """Hold the crew's auto-approve grant in step with its record. Returns trust.

    Called at launch AND from the watchdog every cycle, and it is an ASSIGNMENT,
    not a one-way set: a crew whose ``unattended`` flag is turned off, or which
    stops being live, loses the grant within one cycle. Liveness is re-checked here
    and not only by the caller, so this function cannot hand a grant to a paused
    crew whatever path reaches it.

    ``slot._trust_scope`` carries the scope key onto the slot, because the consumer
    of the grant is the shared dashboard approval path — a grant nothing consults
    is a control in name only. That attribute is the whole opt-in: a slot without
    it is unaffected by any of this, and the interactive ``slot._trust`` flag is
    never written here.

    What bounds the grant, stated plainly because it is a security control: it is
    SEL-audited fail-closed before it exists, it expires ``TRUST_TTL_SECS`` after
    the last watchdog cycle that touched it, it is re-checked on every approval
    rather than sampled once, and it cannot be extended past ``SafetyOverride``'s
    24h ceiling without a fresh audited activation. If the audit write fails there
    is no grant at all and the crew falls back to the ordinary approval path.
    """
    scope = autoapprove_scope(str(crew.get("id") or ""))
    want = bool(crew.get("unattended")) and is_live(crew)
    had = bool(getattr(slot, "_trust_scope", ""))
    if not want:
        safety_override().deactivate_scope(scope)
        slot._trust_scope = ""
        if had:
            logger.info("issue-radar crew %s: trust revoked", crew.get("id"))
        return False
    granted = _hold_grant(scope)
    slot._trust_scope = scope if granted else ""
    if not granted:
        # ``activate_scoped`` audits fail-closed, so the only way here is a SEL
        # write that failed. Fail closed too: no grant, and say so loudly, because
        # the visible symptom is otherwise just a crew that mysteriously stalls on
        # an approval prompt nobody is watching.
        logger.error(
            "issue-radar crew %s: auto-approve grant REFUSED (its audit could not "
            "be written); the crew will fall back to interactive approval",
            crew.get("id"),
        )
    elif not had:
        logger.info("issue-radar crew %s: trust established", crew.get("id"))
    return granted


async def ensure_crew_session(state: Any, owner: str, repo: str, crew: dict[str, Any]) -> Any:
    """Attach to (or create) the crew's app-owned slot and return it.

    Agent, workspace and model all come from the crew record. ``model`` OVERRIDES
    whatever the chosen agent pins, because ``get_or_create_slot`` takes it as an
    explicit argument — that is the intended precedence: the crew's config is the
    operator's last word.
    """
    slot_key = _slot_key(crew)
    slot = state.get_or_create_slot(
        name=slot_key,
        agent=str(crew.get("agent") or "kirocrew"),
        workspace=str(crew.get("workspace") or "default"),
        model=str(crew.get("model") or ""),
        app=APP_NAME,
    )
    title = _slot_title(owner, repo, crew, slot_key)
    if slot.title != title or not getattr(slot, "_titled", False):
        slot.title = title
        # Lock the title: the loop's messages arrive as nudge/user rows on an
        # app-owned slot nobody named, and an auto-titler that fired here would
        # rename the crew's session after whatever the first turn happened to do.
        slot._titled = True
        log = getattr(state, "conversation_log", None)
        if log is not None:
            try:
                await asyncio.to_thread(log.set_title, slot_history_key(slot), title)
            except Exception:  # pragma: no cover - persistence is best-effort
                logger.warning(
                    "issue-radar: could not persist crew slot title for %s",
                    slot_key,
                    exc_info=True,
                )
        _call_if_present(state, "push_slot_title", slot.key, title)
    await asyncio.to_thread(sync_trust, slot, crew)
    _call_if_present(state, "push_slots_update")
    return slot


async def _turn_model_items(
    slot: Any,
    owner: str,
    repo: str,
    crew: dict[str, Any],
    root: Path | None = None,
) -> list[dict[str, Any]] | None:
    """The open work items the next turn's model is chosen from, read off the loop.

    ``None`` when there is nothing to choose: a crew with no triage model, and no
    triage model to switch off, never reads.
    """
    if slot is None:
        return None
    if not crew_model.triage_model(crew) and not crew_model.is_managed(slot):
        return None
    if crew_model.is_held(crew):
        # A person's pick holds the slot's model: there is nothing to choose.
        return None
    return await asyncio.to_thread(
        partial(crew_store.list_work_items, owner, repo, str(crew.get("id")), root, open_only=True)
    )


async def _record_model_hold(
    owner: str, repo: str, crew: dict[str, Any], root: Path | None = None
) -> None:
    """Put a hand-picked model hold seen in this process on the crew record.

    So a gateway restart keeps the person's pick rather than switching over it.
    Off the loop, and compare-and-set on what the hold was seen under -- the model
    settings and the crew's hold release count -- so a release that lands while the
    write is queued wins.
    """
    seen = crew_model.hold_seen_at(crew)
    if seen is None or not crew_model.hold_to_record(crew):
        return
    model, triage = crew_model.work_model(crew), crew_model.triage_model(crew)
    try:
        await asyncio.to_thread(
            partial(
                crew_store.set_crew_model_held,
                owner,
                repo,
                str(crew.get("id")),
                model=model,
                triage_model=triage,
                seen_releases=seen,
                root=root,
            )
        )
    except Exception:  # pragma: no cover - the in-process hold still applies
        logger.warning(
            "issue-radar crew %s: could not record the hand-picked model hold",
            crew.get("id"),
            exc_info=True,
        )


async def _apply_turn_model(
    state: Any,
    slot: Any,
    owner: str,
    repo: str,
    crew: dict[str, Any],
    root: Path | None = None,
) -> bool:
    """Point the crew's slot at the model its next turn runs on (see ``crew_model``).

    The decision and the write are one synchronous step after the read, so a turn
    that started during the read is seen and left alone.
    """
    items = await _turn_model_items(slot, owner, repo, crew, root)
    if items is None:
        return False
    moved = crew_model.apply_turn_model(state, slot, crew, items)
    await _record_model_hold(owner, repo, crew, root)
    return moved


async def launch_crew(
    state: Any,
    owner: str,
    repo: str,
    crew: dict[str, Any],
    root: Path | None = None,
    key: provider.RepoKey | None = None,
) -> Any:
    """Bring a crew online: ensure its session, then arm its nudge loop.

    ``max_cycles=0`` — a crew is not a bounded errand. Its brakes are the record's
    ``enabled``/``retired_at`` flags, the STOP sentinel, and the app's own enabled
    gate, all of which the watchdog re-reads every cycle.
    """
    slot = await ensure_crew_session(state, owner, repo, crew)
    await _apply_turn_model(state, slot, owner, repo, crew, root)
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is None:
        logger.warning(
            "issue-radar crew %s: autonudge unavailable — session exists but no loop",
            crew.get("id"),
        )
        return slot
    await svc.add(
        slot_key=slot.key,
        message=await compose_turn_prompt_async(slot, owner, repo, crew, root, key),
        idle_secs=DEFAULT_IDLE_SECS,
        max_cycles=0,
        stop_sentinel_path=str(stop_sentinel_path(owner, repo, str(crew.get("id")), root)),
        admission_check=lambda: state.get_slot(slot.key) is slot,
    )
    return slot


# ── turn dispatch ───────────────────────────────────────────────────────────

#: What a crew's transcript says when its turn never got a permit. Rendered as an
#: error row in the crew's own session, not only logged: a human who opens a crew
#: that looks stalled must find the reason there, and the alternative is a silent
#: gap where a turn should have been.
NO_PERMIT_CARD = (
    "This crew's turn never started: it waited for a free background-turn slot "
    "and gave up. Nothing ran and nothing was rolled back. The crew gets another "
    "turn on its next signal or idle cycle; raise "
    "`dashboard.max_background_turns` if the fleet is queueing at the cap."
)


async def _capped_run_chat(state: Any, slot: Any, prompt: str) -> None:
    """One crew turn, charged against the app-owned background-turn cap.

    Handed to ``enqueue_or_run_prompt`` in place of ``_run_chat`` itself. That
    keeps the method's queue-vs-run decision intact — it checks ``running`` and
    mutates with no ``await`` in between, so two concurrent wakes cannot both start
    a turn — and wraps the cap around the turn it starts. Calling ``_run_chat``
    directly skipped ``run_background_turn`` entirely, which is the whole point of
    the cap: crews are the only fleet in the product that arms N loops firing
    independently, so simultaneous wakes could put more turns on the runtime than
    the cap allows while its counters reported the truth about a smaller number.

    ``run_background_turn`` QUEUES at the cap rather than rejecting, so the only
    failure it reports is a turn that never ran at all — after its own wait budget
    expires. That is reported here rather than swallowed: a refused turn and a
    finished one must not look the same from the outside.
    """
    try:
        await state.run_background_turn(
            slot,
            # The prompt is composed by the crew runtime, so the ledger records
            # the crew as the actor rather than a user.
            _run_chat(state, slot, prompt, _directive_user_origin=False, _turn_actor="crew"),
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "issue-radar: crew turn on %s never got a background-turn permit",
            getattr(slot, "key", "?"),
        )
        try:
            slot.append("error", NO_PERMIT_CARD, "msg msg-err")
        except Exception:  # pragma: no cover - the card is never load-bearing
            logger.debug("issue-radar: could not render the no-permit card", exc_info=True)
        _call_if_present(state, "push_slots_update")


def dispatch_crew_turn(state: Any, slot: Any, prompt: str) -> bool:
    """Start — or queue — one crew turn. Returns True when a turn actually started.

    THE dispatch path for this app: every route by which a crew gets a turn comes
    through here — the sweep's wake, and anything a request injects into a crew's
    session — so the cap covers all of them. A second copy of this call is how one
    of them ships uncapped again.
    """
    return bool(slot.enqueue_or_run_prompt(prompt, _capped_run_chat, state))


def _slot_key(crew: dict[str, Any]) -> str:
    """The crew's session key. One definition, because a second spelling of the
    fallback would silently address a different slot."""
    return str(crew.get("slot_key") or f"crew-{crew.get('id')}")


async def _rehydrate(state: Any, slot_key: str) -> Any:
    """Rebuild a slot the gateway does not hold in memory (tab closed, restart).

    ``autonudge`` cannot do this for us: arming and firing both gate on the slot
    being resident, so a crew whose slot left ``_slots`` is unreachable by nudge
    alone. Reads are hoisted off the event loop by the async form.
    """
    if not slot_key:
        return None
    try:
        return await rehydrate_slot_from_history_async(state, slot_key)
    except Exception:  # pragma: no cover - defensive
        logger.debug("issue-radar: rehydrate failed for %s", slot_key, exc_info=True)
        return None


async def _current_crew(
    owner: str, repo: str, crew: dict[str, Any], root: Path | None = None
) -> dict[str, Any] | None:
    """The crew's record as the store holds it NOW, or ``None`` if it cannot be read.

    Liveness is durable state and every caller here holds a SNAPSHOT of it. A pause
    writes ``paused_reason`` through ``crew_store`` and can reach no snapshot any
    caller is holding, so ``is_live`` on a snapshot taken before an ``await`` reads
    live for a crew that is already stopped — and the two things that follow, the
    auto-approve grant and the turn itself, are exactly what a pause exists to
    prevent. Re-reading immediately before them is what makes the check current.

    OFF the loop, like every other store read here, and that is not a weakness of
    the check: :func:`wake_crew` documents why the read's own thread hop cannot hide
    a stop. The record carries operator free text (``extra_prompt``) that the write
    path length-checks no further than "is a string", so parsing it on the loop would
    put an unbounded blocking read on the gateway's only thread — precisely the
    hazard ``no-blocking-call-on-event-loop`` exists for.

    ``None``, never the caller's snapshot, when there is no readable record — FAIL
    CLOSED. Falling back to the snapshot would answer a governance question with the
    very value whose staleness is in question, so a record that is missing, corrupt
    or unreadable would go on authorizing turns from it. Every caller here is handed
    crews listed from the same store and root, so a readable record is the ordinary
    case and ``None`` means something is genuinely wrong; treating that as stopped
    costs at most one cycle's turn and repairs itself on the next read.
    """
    crew_id = str(crew.get("id") or "")
    if not crew_id:
        return None
    fresh = await asyncio.to_thread(crew_store.read_crew, owner, repo, crew_id, root)
    return fresh if isinstance(fresh, dict) else None


def is_live(crew: dict[str, Any]) -> bool:
    """Whether this crew should be worked at all.

    ``paused_reason`` counts as not-live even though the crew record is enabled: the
    brief tells a paused crew to read the ledger and end the turn immediately, so
    waking one buys a turn whose only outcome is the cost of the turn.
    """
    return bool(
        crew.get("enabled") and not crew.get("retired_at") and not crew.get("paused_reason")
    )


def _call_if_present(obj: Any, name: str, *args: Any) -> None:
    """Call an optional method on the dashboard state (test stubs omit most of them)."""
    fn = getattr(obj, name, None)
    if callable(fn):
        try:
            fn(*args)
        except Exception:  # pragma: no cover - UI push is never load-bearing
            logger.debug("issue-radar: %s failed", name, exc_info=True)
