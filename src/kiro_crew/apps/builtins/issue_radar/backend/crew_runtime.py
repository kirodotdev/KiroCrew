"""Crew runtime — the session a crew lives in, the brief it carries, the nudge that
drives every turn, and the zero-LLM change detector that wakes it.

Four things live here and nothing else:

1. **Session launch/attach** (:func:`ensure_crew_session`, :func:`launch_crew`).
   A crew is an app-owned dashboard slot keyed ``crew-<id>``, which is the only
   worker shape without a wall-clock ceiling shorter than one issue's lifecycle.
   ``auto_research``'s campaign worker is the working precedent for the session
   shape, including the parts that look redundant: the explicit title with
   ``_titled = True`` (the loop's messages never trigger the auto-titler), and
   re-deriving the crew's auto-approve grant from the watchdog every cycle because
   the grant is in-memory only and a gateway restart drops it. The grant itself
   follows the TASK RUNNER instead: a scoped, expiring, SEL-audited
   ``SafetyOverride`` grant (:func:`autoapprove_scope`), never the unbounded
   ``slot._trust`` flag a human clicks.

2. **Brief injection by presence check** (:func:`brief_is_present`). Not a
   schedule, not a turn counter — one rule that covers session start,
   post-compaction, gateway restart and any truncation mechanism nobody has
   written yet.

3. **Nudge composition** (:func:`compose_nudge`). A volatile snapshot the crew
   must not guess at, plus the brief's ``Never`` list compressed to ~80 words.

4. **The sweep** (:func:`sweep_repo`), driven from ``watch.py``'s poll loop — the
   only always-on loop in this app. Zero LLM, zero credits: it compares the six
   unblock signals against a stored fingerprint and wakes the owning crew when one
   moves.

Nothing here writes to the forge, and nothing here writes a work item: a change
detector that touched ``last_progress_at`` would renew the very claim TTL the
protocol measures from it. Its own state lives in ``crew-signals.json``, beside
the crews dir rather than inside it — ``crew_store.list_crews`` globs ``*.json``
there, so a state file dropped in that directory would read back as a phantom crew.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import Any

from kiro_crew.apps import teardown
from kiro_crew.apps.teardown import register_slot_close_hook, register_slot_close_undo_hook
from kiro_crew.atomic_write import atomic_write
from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.safety_override import safety_override

from . import crew_store, provider, store

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

#: Every crew's slot key starts with this. It is what identifies a crew session in
#: the gateway's slot registry when no crew RECORD is available to ask — which is
#: the case in :func:`revoke_crew_grants`, whose job includes catching a session
#: whose record is already gone.
_CREW_SLOT_PREFIX = "crew-"


def _is_crew_slot_key(key: str) -> bool:
    """Whether *key* names a CREW session, by id shape and not by prefix alone.

    The prefix on its own is a claim about a namespace this app does not own: a
    person may name an ordinary chat tab ``crew-notes``, and the app has no right
    to touch that tab's auto-nudge loop just because the string starts the same
    way. Validating the SUFFIX against the store's own id grammar is the same rule
    the record enumerator already follows (``is_crew_id(path.stem)`` rather than
    excluding known sibling filenames), applied to the one identifier that arrives
    from the slot registry instead of from disk.

    Where a slot object is in hand, ``_app == APP_NAME`` is the stronger check and
    is used in addition; a nudge LOOP carries no owning app, so this grammar is
    the only thing standing between "disable Issue Radar" and silently stopping an
    unrelated monitoring loop the user set up by hand.
    """
    if not str(key).startswith(_CREW_SLOT_PREFIX):
        return False
    return crew_store.is_crew_id(str(key)[len(_CREW_SLOT_PREFIX):])


#: Crews get a turn on this idle gap when the sweep has nothing to report. The
#: watcher is the real scheduler (it fires on an actual signal); this is the
#: fallback clock that lets an idle crew pick up NEW work, so it is deliberately
#: slow. ``autonudge`` clamps it to [15s, 24h].
DEFAULT_IDLE_SECS = 300


# ── the brief ───────────────────────────────────────────────────────────────

#: First line of ``crew_brief.md``. The injection rule keys on this string.
BRIEF_SENTINEL = "<!-- kirocrew-crew-brief v1 -->"

_BRIEF_PATH = Path(__file__).with_name("crew_brief.md")
_brief_cache: str | None = None


def brief_text() -> str:
    """The brief, read once per process.

    A missing file is returned as an empty string rather than raising: a crew with
    no brief is a bad crew, but a crash in the always-on poll loop is worse.
    """
    global _brief_cache
    if _brief_cache is None:
        try:
            _brief_cache = _BRIEF_PATH.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - defensive
            logger.warning("crew brief unreadable at %s", _BRIEF_PATH, exc_info=True)
            _brief_cache = ""
    return _brief_cache


def brief_is_present(slot: Any) -> bool:
    """Whether this session still carries the brief.

    TWO conditions, and the second one is the whole point: a message must contain
    the sentinel AND be at least as long as the brief itself. A compaction summary
    routinely quotes a marker it saw ("the session opened with
    ``<!-- kirocrew-crew-brief v1 -->`` and a work list…"), and a sentinel-only
    check would read that as a hit and leave the crew running for the rest of the
    day on a paraphrase of its own instructions. The carrying message is always
    brief + nudge, so it is strictly longer than the brief; nothing that merely
    mentions the sentinel can be.

    One rule, four situations: session start, post-compaction, gateway restart,
    and whatever truncates a window next. No heuristic and no schedule to keep in
    sync with the truncation mechanism.

    COST, measured on this install: context runs ~0.154 credits per 1k tokens on
    claude-opus-5 and the brief is ~4.2k tokens, i.e. ~0.6 credits to inject.
    Anything appended to ``slot.messages`` ACCUMULATES — it is re-sent as context
    on every later turn — so re-sending the brief on all ~80 turns of a crew's day
    costs ~2100 credits/day/crew (80 turns × the growing prefix), against a handful
    of injections for the presence check. That is the entire reason this is a
    presence check and not "every turn" or "every N turns".
    """
    brief = brief_text()
    if not brief:
        return True  # nothing to inject; never loop on a missing file
    for msg in getattr(slot, "messages", None) or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str) or BRIEF_SENTINEL not in content:
            continue
        if len(content) >= len(brief):
            return True
    return False


# ── the nudge ───────────────────────────────────────────────────────────────

#: The claim label, identical in every install. It is what one crew reads to know
#: another is already working an issue, so it is deliberately NOT configurable: an
#: install that renamed it would be invisible to every crew that did not, and both
#: would work the same issue.
CLAIM_LABEL = "crew: in progress"


def writable_labels(settings: dict[str, Any] | None = None) -> tuple[str, ...]:
    """Every label a crew in this repository may write — resolved, not a constant.

    Two of them: the claim, and the one that says a human has to look at this. The
    second is the repo's ``needs_human_label`` setting, which is the whole reason
    this is a function. Both directions of getting it wrong are load-bearing, and a
    module constant can express neither: a crew told it may NOT write the label the
    settings name has no way to hand an issue to a human, and a crew told it MAY
    write a label the settings do not name puts a label of its own invention on a
    stranger's issue — the one prohibition in the nudge with a public blast radius.

    Falls back to the store's own default, and to the claim label alone if even
    that is absent, because naming an empty label reads as permission to write one.
    """
    raw = (settings or {}).get("needs_human_label")
    needs_human = raw.strip() if isinstance(raw, str) else ""
    if not needs_human:
        fallback = crew_store.DEFAULT_SETTINGS.get("needs_human_label")
        needs_human = fallback.strip() if isinstance(fallback, str) else ""
    return (CLAIM_LABEL, needs_human) if needs_human else (CLAIM_LABEL,)


def never_block(labels: Sequence[str] = ()) -> str:
    """The brief's ``Never`` list, compressed, naming this repo's writable labels.

    Repeated on EVERY turn, unlike the brief. It is cheap (~80 words) and it buys
    back authority the brief does not have: an injected brief arrives as a USER
    message, not a system prompt, so it is the weakest kind of instruction in the
    window and the furthest from the turn's actual work. Prohibitions kept adjacent
    to the instruction are the version that holds.

    The label clause names the resolved set rather than the ``crew:`` prefix,
    because a prefix rule cannot express this permission: ``needs_human_label`` is
    free text and need not carry the prefix at all, while any other
    ``crew:``-prefixed label — one left behind on an issue, or one a differently
    configured install writes — does carry it and is still not this crew's to write.
    """
    allowed = ", ".join(f"`{lab}`" for lab in (labels or writable_labels()))
    return (
        "Never: modify CI or gate configuration — `.github/` or whatever this repo "
        "uses, plus any rule file those gates read. They judge you. "
        f"Never write any label other than {allowed}. "
        "Never push to main or whichever branch this repo defaults to, and never "
        "merge a PR yourself. Never edit another crew's "
        "claim comment. Never hold uncommitted changes in two worktrees. Never end a "
        "turn without writing the ledger. Never put an absolute path, a host name or "
        "anything else about this machine into a progress line — progress lines go "
        "public. Never report a gate as passing when you have not seen its exit code."
    )


def build_snapshot(
    owner: str, repo: str, crew: dict[str, Any], root: Path | None = None
) -> dict[str, Any]:
    """The volatile facts a crew must never guess — read from the store, no API calls."""
    crew_id = str(crew.get("id") or "")
    items = crew_store.list_work_items(owner, repo, crew_id, root, open_only=True)
    # The writable set belongs with everything else volatile rather than in a
    # constant: an operator can rename ``needs_human_label`` between two turns of
    # the same session, and the nudge is the only place the crew is told which
    # labels it may write.
    settings = crew_store.read_settings(owner, repo, root)
    return {
        "name": crew.get("name") or crew_id,
        "id": crew_id,
        "owner": owner,
        "repo": repo,
        "labels": list(crew.get("labels") or []),
        "writable_labels": list(writable_labels(settings)),
        "open_count": crew_store.open_slot_count(owner, repo, crew_id, root),
        "max_open": int(crew.get("max_open") or 0),
        "items": [
            {
                "number": it.get("number"),
                "phase": it.get("phase") or "",
                "next": (it.get("next") or "").strip(),
                "pr_number": it.get("pr_number"),
            }
            for it in items
        ],
    }


def compose_nudge(snapshot: dict[str, Any]) -> str:
    """The per-turn message: a volatile snapshot (~120 words) then the Never block.

    Everything in the first part can change between two turns of the same session —
    the crew may have been renamed, re-scoped, re-limited, or had an item picked up
    by a human — which is why it is re-sent every turn instead of living in the
    brief. The brief says "your name, your repository, your label scope and your
    limits arrive in the nudge"; this is that promise.
    """
    # An empty label list means EVERY open issue, not none. The editor defaults a
    # new crew to no labels and does not require any, so the opposite reading —
    # which this line used to give — told every default-configured crew to pick up
    # nothing, and it would idle for its whole life without an error anywhere. No
    # backend code filters on this list; it is advisory text the crew self-applies
    # from the brief, so the wording here IS the contract, and the brief's filter
    # step is conditioned to match.
    scope = ", ".join(snapshot.get("labels") or []) or "(no label filter — every open issue)"
    allowed = [str(lab) for lab in (snapshot.get("writable_labels") or ()) if str(lab).strip()]
    if not allowed:
        # A snapshot assembled without a resolved set still has to be told something
        # true, and the store's defaults are what a repo with no settings file
        # resolves to anyway. Naming nothing here would leave the one prohibition
        # with a public blast radius unstated.
        allowed = list(writable_labels())
    lines = [
        f"[crew turn] {snapshot.get('name')} · {snapshot.get('owner')}/"
        f"{snapshot.get('repo')} · id {snapshot.get('id')}",
        f"Label scope: {scope}.",
        "Writable labels: " + ", ".join(f"`{lab}`" for lab in allowed) + " — no others, ever.",
        f"Open {snapshot.get('open_count')}/{snapshot.get('max_open')}",
    ]
    items = snapshot.get("items") or []
    if items:
        lines.append("Open work items:")
        for it in items:
            pr = f" (PR #{it['pr_number']})" if it.get("pr_number") else ""
            nxt = it.get("next") or "no next step recorded — decide one and record it"
            lines.append(f"- #{it.get('number')} {it.get('phase')}{pr} — next: {nxt}")
    else:
        lines.append("Open work items: none. Pick up new work if you are under your limit.")
    lines.append(
        "Read the ledger first, reconcile every open item against the six unblock "
        "signals, advance ONE item, and write the ledger before the turn ends."
    )
    return "\n".join(lines) + "\n\n" + never_block(allowed)


def compose_turn_prompt(
    slot: Any, owner: str, repo: str, crew: dict[str, Any], root: Path | None = None
) -> str:
    """The full prompt for the next turn: the brief when it is missing, then the nudge.

    Injection rides on the prompt rather than being appended to ``slot.messages``
    on its own, because an appended message is transcript only — the agent process
    holds its own context and sees a prompt. One consequence worth knowing: the
    message that carries the brief IS the nudge message, which is what makes the
    length guard in :func:`brief_is_present` sound.

    BLOCKING — :func:`build_snapshot` globs the crew's item dir and parses every
    open item. Async callers use :func:`compose_turn_prompt_async`.
    """
    return _assemble_turn_prompt(slot, build_snapshot(owner, repo, crew, root))


async def compose_turn_prompt_async(
    slot: Any, owner: str, repo: str, crew: dict[str, Any], root: Path | None = None
) -> str:
    """:func:`compose_turn_prompt` with the store reads off the event loop.

    Every launch and every wake composes a prompt, and the work behind it grows
    with the crew: one directory glob plus a JSON parse per open work item. A crew
    that has been running for a while would stall the gateway — and with it the
    always-on poll loop this app lives in, the one thing that must never block,
    since the sweep is what wakes a crew when CI turns red.

    Only the snapshot moves. The presence check stays on the loop because it walks
    ``slot.messages``, which a running turn appends to; that is the same split
    ``rehydrate_slot_from_history_async`` documents for slot state.
    """
    snapshot = await asyncio.to_thread(partial(build_snapshot, owner, repo, crew, root))
    return _assemble_turn_prompt(slot, snapshot)


def _assemble_turn_prompt(slot: Any, snapshot: dict[str, Any]) -> str:
    """Nudge from an already-read snapshot, with the brief in front when it is gone."""
    nudge = compose_nudge(snapshot)
    if brief_is_present(slot):
        return nudge
    return brief_text() + "\n\n---\n\n" + nudge


# ── session launch / attach ─────────────────────────────────────────────────


def stop_sentinel_path(
    owner: str, repo: str, crew_id: str, root: Path | None = None
) -> Path:
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


async def revoke_crew_execution(
    state: Any, crew: dict[str, Any], reason: str = ""
) -> bool:
    """Take away everything that could give this crew another turn. Idempotent.

    The inverse of :func:`sync_trust` plus the loop, and it exists as ONE function
    because stopping a crew means clearing two grants that the crew RECORD does not
    express: the autonudge loop is a live timer owned by another service, and
    ``slot._trust_scope`` plus its ``SafetyOverride`` grant are in-memory. Writing
    ``enabled``/``paused_reason``/``retired_at`` changes neither of them.

    That gap is a real window, not a tidiness point. Until something revokes, an
    idle timer that fires runs one more turn — auto-approved and unattended — on a
    crew a human just stopped. The watchdog notices, but it runs on the app's poll
    interval, so anything that only writes the record leaves that whole interval
    open. The pause and retire routes call this BEFORE they answer, which is what
    makes "stopped" true by the time the response lands; the watchdog's own call
    stays as the backstop for a crew stopped by a direct record edit or by a
    restart.

    Best-effort by design: the record is the durable truth and it is already
    written when a route gets here, so a failure to reach the in-memory grants must
    not turn a successful pause into a 500. Returns whether anything was actually
    revoked, which is what tests assert on.
    """
    slot_key = str(crew.get("slot_key") or f"crew-{crew.get('id')}")
    scope = autoapprove_scope(str(crew.get("id") or ""))
    revoked = False
    slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
    # ``scope_remaining_secs`` and not ``is_scope_active``: the latter EXPIRES a
    # lapsed grant and emits a ``scope_expired`` SEL event, so probing with it here
    # would forge an expiry record for a grant this call is about to revoke anyway.
    if safety_override().scope_remaining_secs(scope) > 0:
        revoked = True
    safety_override().deactivate_scope(scope)
    if slot is not None:
        if getattr(slot, "_trust_scope", ""):
            slot._trust_scope = ""
            revoked = True
        # Also clear an interactive grant a human may have clicked onto this slot.
        # "Stop this crew" has to mean stopped, and unlike :func:`sync_trust` — which
        # runs unprompted every cycle and so must leave a human's choice alone — this
        # runs only because a human or the watchdog decided the crew must not run.
        if getattr(slot, "_trust", False):
            slot._trust = False
            revoked = True
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is not None:
        try:
            loop = svc.get_by_slot(slot_key)
            if loop is not None and loop.active:
                await svc.update(loop.id, active=False)
                revoked = True
        except Exception:  # pragma: no cover - cleanup must not raise
            logger.warning(
                "issue-radar crew %s: could not deactivate its loop (%s)",
                crew.get("id"),
                reason or "stopped",
                exc_info=True,
            )
    if revoked:
        logger.info(
            "issue-radar crew %s: execution revoked (%s)",
            crew.get("id"),
            reason or "stopped",
        )
    return revoked


async def ensure_crew_session(
    state: Any, owner: str, repo: str, crew: dict[str, Any]
) -> Any:
    """Attach to (or create) the crew's app-owned slot and return it.

    Agent, workspace and model all come from the crew record. ``model`` OVERRIDES
    whatever the chosen agent pins, because ``get_or_create_slot`` takes it as an
    explicit argument — that is the intended precedence: the crew's config is the
    operator's last word.
    """
    slot_key = str(crew.get("slot_key") or f"crew-{crew.get('id')}")
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
    sync_trust(slot, crew)
    _call_if_present(state, "push_slots_update")
    return slot


async def launch_crew(
    state: Any, owner: str, repo: str, crew: dict[str, Any], root: Path | None = None
) -> Any:
    """Bring a crew online: ensure its session, then arm its nudge loop.

    ``max_cycles=0`` — a crew is not a bounded errand. Its brakes are the record's
    ``enabled``/``retired_at`` flags, the STOP sentinel, and the app's own enabled
    gate, all of which the watchdog re-reads every cycle.
    """
    slot = await ensure_crew_session(state, owner, repo, crew)
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is None:
        logger.warning(
            "issue-radar crew %s: autonudge unavailable — session exists but no loop",
            crew.get("id"),
        )
        return slot
    await svc.add(
        slot_key=slot.key,
        message=await compose_turn_prompt_async(slot, owner, repo, crew, root),
        idle_secs=DEFAULT_IDLE_SECS,
        max_cycles=0,
        stop_sentinel_path=str(stop_sentinel_path(owner, repo, str(crew.get("id")), root)),
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
        await state.run_background_turn(slot, _run_chat(state, slot, prompt))
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


async def wake_crew(
    state: Any,
    owner: str,
    repo: str,
    crew: dict[str, Any],
    reason: str = "",
    root: Path | None = None,
) -> bool:
    """Give the crew a turn NOW because a signal moved. Returns whether a turn started.

    Two writes, both needed. The armed loop's message is refreshed so an idle-timer
    fire that lands later carries the CURRENT snapshot instead of the one composed
    at launch; and the prompt is dispatched immediately, because the whole point of
    the sweep is that the crew does not wait out an idle gap after CI turns red.
    ``enqueue_or_run_prompt`` queues instead of racing when the crew is mid-turn.
    """
    slot = state.get_slot(str(crew.get("slot_key") or f"crew-{crew.get('id')}"))
    if slot is None:
        slot = await _rehydrate(state, str(crew.get("slot_key") or ""))
    if slot is None:
        logger.info(
            "issue-radar crew %s: no session to wake (%s)", crew.get("id"), reason or "signal"
        )
        return False
    sync_trust(slot, crew)
    prompt = await compose_turn_prompt_async(slot, owner, repo, crew, root)
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is not None:
        loop = svc.get_by_slot(slot.key)
        if loop is not None:
            try:
                await svc.update(loop.id, message=prompt)
            except Exception:  # pragma: no cover - refresh is best-effort
                logger.debug("issue-radar: nudge refresh failed", exc_info=True)
    if getattr(slot, "running", False):
        # DROPPED, not queued — the same call autonudge's own fire path makes, and
        # for a stronger reason here: a queued prompt can carry the whole brief, so
        # a crew that stayed busy across three sweeps would come back to three
        # stacked copies of its own instructions. Nothing is lost by dropping it.
        # The loop's message was just refreshed with the current snapshot, and the
        # crew reconciles EVERY open item against the six signals at the top of each
        # turn anyway — the wake buys latency, it does not carry information.
        logger.info(
            "issue-radar crew %s: mid-turn, wake dropped (%s)",
            crew.get("id"),
            reason or "signal",
        )
        return False
    tagged = f"[crew wake: {reason}]\n{prompt}" if reason else prompt
    started = dispatch_crew_turn(state, slot, tagged)
    _call_if_present(state, "push_slots_update")
    logger.info(
        "issue-radar crew %s woken (%s): turn %s",
        crew.get("id"),
        reason or "signal",
        "started" if started else "queued",
    )
    return bool(started)


async def _rehydrate(state: Any, slot_key: str) -> Any:
    """Rebuild a slot the gateway no longer holds in memory (tab closed, restart).

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


# ── the watchdog (one pass per poll cycle) ──────────────────────────────────


def is_live(crew: dict[str, Any]) -> bool:
    """Whether this crew should be worked at all.

    ``paused_reason`` counts as not-live even though the crew record is enabled: the
    brief tells a paused crew to read the ledger and end the turn immediately, so
    waking one buys a turn whose only outcome is the cost of the turn.
    """
    return bool(
        crew.get("enabled") and not crew.get("retired_at") and not crew.get("paused_reason")
    )


#: Why a crew is paused when the user closes its chat tab. Stored in
#: ``paused_reason``, so the roster explains the stop instead of showing a crew
#: that is enabled and inexplicably idle.
DISMISSED_PAUSE_REASON = "session closed"


async def _on_slot_closed(slot_key: str, root: Path | None = None) -> None:
    """The user dismissed a crew's chat tab: PAUSE that crew.

    Pausing rather than silently un-arming, because a paused crew is a state the
    product already has a first-class representation for — ``is_live`` is already
    False for it, the sweep's existing revocation branch already takes its trust
    away, the roster already shows ``paused_reason``, and the existing pause route
    already resumes it. An autonomous worker that stops has to SAY it stopped.

    Reached only from a deliberate ✕ (see ``apps.teardown.notify_slot_closed``),
    never from idle archival — so this cannot mistake a crew that was merely quiet
    for one the user asked to stop.
    """
    found = await asyncio.to_thread(_crew_for_slot_key, slot_key, root)
    if found is None:
        return
    owner, repo, crew, scope = found
    if crew.get("retired_at") or crew.get("paused_reason"):
        # Already stopped, and for a reason someone else recorded. Overwriting it
        # would replace a specific explanation with a generic one.
        return
    await asyncio.to_thread(
        crew_store.set_crew_paused,
        owner,
        repo,
        str(crew.get("id") or ""),
        True,
        DISMISSED_PAUSE_REASON,
        scope,
    )
    logger.info(
        "issue-radar crew %s paused: its session was closed (%s)", crew.get("id"), slot_key
    )


async def _on_slot_close_undone(slot_key: str, root: Path | None = None) -> None:
    """The dismissal did not happen: RESUME the crew this hook paused.

    Guarded on the reason string, which is the only evidence that THIS hook is what
    stopped the crew. ``_on_slot_closed`` refuses to overwrite an existing
    ``paused_reason``, so a crew paused for any other cause never got touched and
    must not be resumed here — un-pausing that would turn a failed tab close into a
    worker restart nobody asked for, which is worse than the state being repaired.
    """
    found = await asyncio.to_thread(_crew_for_slot_key, slot_key, root)
    if found is None:
        return
    owner, repo, crew, scope = found
    if crew.get("retired_at") or crew.get("paused_reason") != DISMISSED_PAUSE_REASON:
        return
    await asyncio.to_thread(
        crew_store.set_crew_paused,
        owner,
        repo,
        str(crew.get("id") or ""),
        False,
        "",
        scope,
    )
    logger.info(
        "issue-radar crew %s resumed: the close that paused it failed (%s)",
        crew.get("id"),
        slot_key,
    )


def _crew_for_slot_key(
    slot_key: str, root: Path | None = None
) -> tuple[str, str, dict[str, Any], Path | None] | None:
    """Find ``(owner, repo, crew)`` for a slot key. Blocking — call in a thread.

    A slot key carries no repo, so this walks the connected repos. Retired crews are
    included: the caller decides what to do about one, and reporting "no such crew"
    for a crew that plainly exists reads as a bug.

    EVERY PROVIDER, not just one. The watchdog registers the close hook once per
    swept repo and passes that repo's PROVIDER root, and the registry holds one hook
    per app — so the last sweep's provider won, and closing the tab of a crew on any
    other provider found nothing: the crew stayed live and the next sweep re-armed
    its auto-approved session. A slot key is global, so its lookup has to be too.
    The given root is still tried first (tests and explicit callers pass a data root
    directly); the provider sweep is a fallback, so it costs an extra pass only when
    a close does not match, which is the rare path.
    """
    if not slot_key:
        return None
    for scope in _lookup_scopes(root):
        for entry in store.list_connected_repos(scope):
            owner = str(entry.get("owner") or "")
            repo = str(entry.get("repo") or "")
            if not owner or not repo:
                continue
            for crew in crew_store.list_crews(owner, repo, root=scope, include_retired=True):
                if str(crew.get("slot_key") or "") == slot_key:
                    # The SCOPE comes back too: the pause has to be written where
                    # the crew actually lives. Writing it under the hook's own root
                    # would file the pause in a different provider's store and leave
                    # the real crew running.
                    return owner, repo, crew, scope
    return None


def _lookup_scopes(root: Path | None) -> list[Path | None]:
    """The data roots to search for a crew, in order: the given one, then every
    connected repo's own provider root.

    Derived per entry rather than assumed, because a mixed GitHub/GitLab install
    keeps each provider's records under its own root and no single root sees both.
    """
    scopes: list[Path | None] = [root]
    try:
        for entry in store.list_connected_repos(None):
            # Only pass what the entry actually carries: `provider_root` defaults
            # to github/github.com, and a legacy entry omits both — passing "" would
            # bypass those defaults and resolve a root nothing lives under.
            kw: dict[str, str] = {}
            if entry.get("provider"):
                kw["provider"] = str(entry["provider"])
            if entry.get("host"):
                kw["host"] = str(entry["host"])
            scope = store.provider_root(root=None, **kw)
            if scope not in scopes:
                scopes.append(scope)
    except Exception:  # pragma: no cover - defensive; never block a close
        logger.debug("issue-radar: could not enumerate provider roots", exc_info=True)
    return scopes


def install_slot_close_hook(root: Path | None = None) -> None:
    """Register the dismissal hook. Idempotent; safe to call every cycle.

    Called from the watchdog rather than once at import, for the same reason the
    watchdog re-establishes trust: the registry is process memory, so a gateway
    restart empties it and a one-shot registration would leave the ✕ silently
    ignored for the rest of that process's life.
    """
    register_slot_close_hook(APP_NAME, partial(_on_slot_closed, root=root))
    # Registered TOGETHER with the close hook, never separately: the pause is only
    # safe to take because it can be taken back, so a process that can pause but
    # not resume is the half-applied state this pair exists to avoid.
    register_slot_close_undo_hook(APP_NAME, partial(_on_slot_close_undone, root=root))


async def on_app_disabled(state: Any, app_name: str = "") -> int:
    """The operator switched Issue Radar off: STOP the crews now, in the request.

    THE FIRST LINE. Turning an app off is a human saying stop, and it has to mean
    stopped by the time the disable returns — not stopped within a poll interval.
    The sweep in ``watch.py`` suspends crews too, but it wakes on
    ``POLL_INTERVAL_SEC``, so on its own it left up to a full minute in which an
    already-armed nudge could fire a whole auto-approved turn for an app the
    operator had just disabled. Same reasoning, and the same shape, as pause and
    retire revoking inline before they answer (:func:`revoke_crew_execution`).

    Registered rather than called: core must be able to stop an app's work without
    importing the app, so this is handed to the hook registry in
    ``apps/teardown.py`` by :func:`install_app_disable_hook`, exactly as the
    dismissal hook is. ``app_name`` is accepted and unused so the registry may pass
    the disabled app's name; the registration is already per-app, so there is
    nothing to filter on here.

    Ordering note for a reader tracing the window: core's teardown runs BEFORE the
    ``enabled`` flag is written, so this revokes while the app is still nominally
    on. That is deliberate and it is why nothing here consults ``is_app_enabled`` —
    a hook that waited for the flag would reintroduce the gap it closes.
    """
    return await suspend_crews(state)


def install_app_disable_hook(state: Any) -> bool:
    """Ask to be told the moment the app is disabled. Returns whether a seam exists.

    Idempotent and safe every cycle, for the reason
    :func:`install_slot_close_hook` documents: the registry is process memory, so a
    gateway restart empties it.

    Feature-detected with ``getattr`` rather than an import, because this app must
    keep working against a core build that has no such registry — and there the
    ``watch.py`` sweep is the only suspension there is, which is the pre-hook
    behaviour rather than a regression. The caller reports a missing seam once (see
    ``watch._watch_loop``); this function stays quiet so the per-cycle
    re-registration cannot turn a known limitation into a log flood.
    """
    # ``getattr`` rather than importing the name directly: the lookup happens per
    # call, so a gateway whose teardown module predates this seam degrades to the
    # ``watch.py`` sweep instead of failing at import time.
    register = getattr(teardown, "register_app_disable_hook", None)
    if register is None:
        return False
    register(APP_NAME, partial(on_app_disabled, state))
    return True


async def watchdog_cycle(
    state: Any,
    owner: str,
    repo: str,
    crews: list[dict[str, Any]],
    root: Path | None = None,
) -> None:
    """Re-establish what the process does not persist. Zero API calls, zero LLM.

    Three things, every cycle, for the same reason ``auto_research``'s watchdog does
    them: they are all in-memory state that a restart drops.

    * **Trust.** A crew's auto-approve grant lives in memory and expires on its own
      (:func:`sync_trust`), so an unattended crew silently loses it on restart, and
      on a watchdog that stopped — which is the point — and the next tool call parks
      it in an approval prompt nobody is watching. Trust needs a slot to hold the
      scope key, so a crew whose slot the gateway no longer holds is rehydrated
      first — an armed loop fires against the slot key regardless, and it must land
      on a trusted session.
    * **The loop.** A crew whose autonudge loop is missing or deactivated has no
      clock at all; the sweep still wakes it on a signal, but it would never pick up
      new work again. Re-arming here makes "enabled" mean enabled.
    * **Revocation.** A crew that was disabled, retired or paused while its slot was
      resident keeps its trust until something takes it away. The BACKSTOP, not the
      first line: pause and retire revoke inline before they answer, and this cycle
      catches what they cannot — a record edited directly, and a restart that
      re-armed a loop for a crew that is no longer live.
    * **Both hooks.** The dismissal hook and the app-disable hook are process
      memory, so a restart empties them. Re-registering here is what keeps a ✕ and
      an app disable from going quiet for the rest of a process's life.
    """
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    install_slot_close_hook(root)
    install_app_disable_hook(state)
    for crew in crews:
        slot_key = str(crew.get("slot_key") or f"crew-{crew.get('id')}")
        slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
        if not is_live(crew):
            await revoke_crew_execution(state, crew, "not live")
            continue
        if slot is None:
            # A live crew with no resident slot: a restart, or a tab someone
            # closed. Rehydrate HERE and not only in the ``loop is None`` branch
            # below, because a PERSISTED loop fires against the slot key whether
            # or not the gateway still holds it — so without this the crew's first
            # post-restart turn runs with its grant gone and an unattended
            # crew parks on an approval nobody is there to answer. Rehydration
            # first, so the crew keeps its own transcript (and its brief).
            slot = await _rehydrate(state, slot_key)
        if slot is not None:
            sync_trust(slot, crew)
        if svc is None:
            continue
        loop = svc.get_by_slot(slot_key)
        if loop is None:
            # No loop for a live crew: either it has never been launched or a
            # restart lost it. Launching is idempotent on the slot key.
            await launch_crew(state, owner, repo, crew, root)
            continue
        if slot is None:
            # Loop but still no slot — nothing on disk to rehydrate from (a crew
            # armed and then never given a turn). ``ensure_crew_session`` creates
            # the session and establishes trust the same way launch does, without
            # re-arming a loop that already exists.
            await ensure_crew_session(state, owner, repo, crew)
        if not loop.active:
            await svc.update(loop.id, active=True)


def revoke_crew_grants(state: Any) -> int:
    """Take every resident crew session's auto-approve grant away. Returns how many.

    SYNCHRONOUS ON PURPOSE, and that is the whole security property: it holds no
    ``await``, so once it is entered nothing else runs on the event loop until every
    grant is gone. Its caller can therefore promise that a crew is untrusted by the
    time the caller's own first suspension point is reached — which is what lets
    :func:`on_app_disabled` close the disable window rather than narrow it.

    Works entirely from IN-MEMORY state — the resident slots — and reads nothing from
    disk. It walks slots rather than records so it also catches a crew session whose
    record was deleted while the session was live, which a record-driven walk would
    miss. Idempotent.
    """
    cleared = 0
    for key, slot in list((getattr(state, "_slots", None) or {}).items()):
        if not str(key).startswith(_CREW_SLOT_PREFIX) or getattr(slot, "_app", "") != APP_NAME:
            continue
        touched = False
        # The scope key is read off the SLOT rather than rebuilt from the record:
        # this walk exists precisely to catch a crew session whose record is gone.
        scope = str(getattr(slot, "_trust_scope", "") or "")
        if scope:
            safety_override().deactivate_scope(scope)
            slot._trust_scope = ""
            touched = True
        if getattr(slot, "_trust", False):
            slot._trust = False
            touched = True
        if touched:
            cleared += 1
    if cleared:
        logger.info("issue-radar disabled — cleared trust on %d crew session(s)", cleared)
    return cleared


async def suspend_crews(state: Any) -> int:
    """Turning the app off must STOP the crews. Returns how many slots were cleared.

    Two things keep a crew going once armed, and neither is expressed by any record
    an app-enabled flag could speak for: the crew's auto-approve grant, keyed off
    its slot, and its autonudge loop, which fires regardless of any app's enabled
    flag. Without this, a disabled Issue Radar keeps running unattended,
    auto-approved turns.

    TWO callers, and which is which matters:

    * :func:`on_app_disabled` — the FIRST LINE, awaited inside the disable request
      itself, so "stopped" is true by the time the operator's disable returns.
    * ``watch._poll_once``'s disabled branch — the BACKSTOP, for the disables this
      module can never be told about: ``kirocrew app disable`` in another process,
      an ``installed.json`` edited on disk, and a gateway restart that re-armed
      loops for an app that is off. It runs on the poll interval by nature.

    GRANTS BEFORE LOOPS, and this order is load-bearing rather than tidy. The grant
    is the thing that lets a turn act unattended, and deactivating loops is the only
    step here that awaits — so doing loops first would hand the event loop back with
    the grants still live, and an armed timer that fired in that gap would get its
    auto-approved turn from the very call that was stopping it. Reversing these two
    statements reopens the window this function exists to close.

    Reads nothing from disk: a disabled app must stay silent, and that includes not
    walking the connected-repo config on every one of its idle cycles. Idempotent,
    as the precedent in ``auto_research`` is.
    """
    if state is None:
        return 0
    cleared = revoke_crew_grants(state)
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is not None:
        for loop in svc.list_all():
            if not _is_crew_slot_key(loop.slot_key):
                continue
            if loop.active:
                try:
                    await svc.update(loop.id, active=False)
                except Exception:  # pragma: no cover - cleanup must not raise
                    logger.warning(
                        "issue-radar: could not deactivate crew loop %s on disable", loop.id
                    )
    return cleared


def _call_if_present(obj: Any, name: str, *args: Any) -> None:
    """Call an optional method on the dashboard state (test stubs omit most of them)."""
    fn = getattr(obj, name, None)
    if callable(fn):
        try:
            fn(*args)
        except Exception:  # pragma: no cover - UI push is never load-bearing
            logger.debug("issue-radar: %s failed", name, exc_info=True)


# ── unblock signals ─────────────────────────────────────────────────────────
#
# The six signals from the brief's table, and the field each one is read from:
#
#   requester replied      issue comment count            (issue detail)
#   CI state changed       check rollup + bucket counts   (batched enrichment)
#   approved / changes req review verdict                 (PR timeline, gated)
#   merge conflict         mergeable / merge state        (batched enrichment)
#   PR merged              merged flag                    (PR detail)
#   post-merge comment     PR comment count while merged  (PR detail)
#
# Missing one means an item silently stalls forever, which is why they are named
# individually here rather than collapsed into "something changed".

SIG_REPLY = "requester-replied"
SIG_CI = "ci-changed"
SIG_REVIEW = "review-verdict"
SIG_CONFLICT = "merge-conflict"
SIG_MERGED = "pr-merged"
SIG_POST_MERGE = "post-merge-comment"

UNBLOCK_SIGNALS = (
    SIG_REPLY,
    SIG_CI,
    SIG_REVIEW,
    SIG_CONFLICT,
    SIG_MERGED,
    SIG_POST_MERGE,
)

SIGNALS_SCHEMA = 1

#: How often an item in each phase is re-read. The forge is the mover for some
#: phases and the crew is the mover for others, and paying a CI-grade cadence for
#: a phase waiting on a human is pure rate limit. ``selected`` is pre-claim and
#: purely local — there is nothing public to watch yet.
RECHECK_SEC = {
    "awaiting-ci": 60,
    "addressing-review": 60,
    "awaiting-merge": 120,
    "claimed": 300,
    "investigating": 300,
    "implementing": 300,
    "awaiting-reply": 300,
}
_DEFAULT_RECHECK_SEC = 300

#: Phases where a fresh review verdict is worth one extra (paginated) timeline
#: read. Every other signal is already summarized on the issue/PR object; a review
#: is not, and a bodyless approval moves no counter at all.
_REVIEW_PHASES = frozenset({"awaiting-ci", "addressing-review", "awaiting-merge"})


def signals_path(owner: str, repo: str, root: Path | None = None) -> Path:
    """Where the fingerprints live — NOT inside ``crews/`` (see the module docstring)."""
    return store.repo_data_dir(owner, repo, root) / "crew-signals.json"


def read_signals(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    path = signals_path(owner, repo, root)
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(doc, dict) or doc.get("schema") != SIGNALS_SCHEMA:
        # A schema mismatch is a cache miss here, exactly as it is for the issue
        # caches: the next sweep re-seeds and reports nothing, which loses at most
        # one cycle of latency and cannot report a stale signal.
        return {}
    items = doc.get("items")
    return items if isinstance(items, dict) else {}


def write_signals(
    owner: str, repo: str, items: dict[str, Any], root: Path | None = None
) -> None:
    atomic_write(
        signals_path(owner, repo, root),
        json.dumps({"schema": SIGNALS_SCHEMA, "items": items}, indent=2),
    )


def _item_key(crew_id: str, number: Any) -> str:
    return f"{crew_id}:{number}"


def detect_unblocks(prev: dict[str, Any] | None, cur: dict[str, Any]) -> list[str]:
    """Which of the six signals moved between two fingerprints.

    A FIRST observation reports nothing: it seeds the mark, the same discipline the
    new-issue watcher uses so connecting a repo does not announce its whole
    backlog. Here the equivalent mistake would wake every crew on every item the
    moment the gateway restarts.
    """
    if not prev:
        return []
    out: list[str] = []
    if _as_int(cur.get("issue_comments")) > _as_int(prev.get("issue_comments")):
        out.append(SIG_REPLY)
    if (cur.get("checks"), cur.get("check_counts")) != (
        prev.get("checks"),
        prev.get("check_counts"),
    ):
        # Only when the current reading is a real one — a failed enrichment call
        # reports None, and None-vs-known must not masquerade as "CI moved".
        if cur.get("checks") is not None:
            out.append(SIG_CI)
    verdict = cur.get("review_decision") or ""
    if verdict and verdict != (prev.get("review_decision") or ""):
        out.append(SIG_REVIEW)
    if cur.get("conflicted") and not prev.get("conflicted"):
        out.append(SIG_CONFLICT)
    if cur.get("merged") and not prev.get("merged"):
        out.append(SIG_MERGED)
    if cur.get("merged") and _as_int(cur.get("pr_comments")) > _as_int(
        prev.get("pr_comments")
    ):
        out.append(SIG_POST_MERGE)
    return out


def _as_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _is_conflicted(mergeable: Any, merge_state: Any) -> bool:
    """A REAL conflict, not an uncomputed one.

    GitHub answers ``mergeable: null`` / ``mergeable_state: "unknown"`` on a cold
    read and computes the merge commit in the background, so truthiness cannot be
    used here: ``not mergeable`` would report every cold read as a conflict and
    wake the crew to resolve one that does not exist.
    """
    if mergeable is False:
        return True
    return str(merge_state or "").lower() == "dirty"


def fingerprint_item(
    key: provider.RepoKey,
    item: dict[str, Any],
    enriched: dict[int, dict[str, Any]],
    prev: dict[str, Any] | None,
) -> dict[str, Any]:
    """Read the six signals for ONE work item. Blocking ``gh`` calls — run off-loop.

    Cost is one REST call for the issue, one more for the PR when the item has one,
    and (only when the PR's ``updated_at`` moved and the phase makes a review
    plausible) one paginated timeline read. The check rollup and merge state come
    from ``enriched``, which the caller fetched for the whole repo in two batched
    GraphQL calls.
    """
    client = provider.client_for(key)
    kwargs = provider.call_kwargs(key)
    number = item.get("number")
    fp: dict[str, Any] = {"phase": item.get("phase") or ""}
    if not isinstance(number, int) or number <= 0:
        return fp  # a record with no issue number has nothing on the forge to read

    issue = client.get_issue_detail(key.owner, key.repo, int(number), **kwargs)
    if isinstance(issue, dict):
        fp["issue_comments"] = _as_int(issue.get("comments"))
        fp["issue_state"] = issue.get("state")

    pr = item.get("pr_number")
    if not isinstance(pr, int) or pr <= 0:
        return fp

    fp["pr_number"] = pr
    row = enriched.get(pr) or {}
    fp["checks"] = row.get("checks_state")
    counts = row.get("checks_counts")
    fp["check_counts"] = counts if isinstance(counts, dict) else None

    # ``resolve_mergeable=False``: the batched readiness call above already supplies
    # a COMPUTED merge state, so paying this call's retry-plus-sleep for the lazy
    # one would buy a second answer to a question already answered.
    detail = client.get_pr_detail(
        key.owner, key.repo, pr, resolve_mergeable=False, **kwargs
    )
    detail = detail if isinstance(detail, dict) else {}
    fp["pr_comments"] = _as_int(detail.get("comments"))
    fp["pr_updated_at"] = detail.get("updated_at")
    fp["merged"] = bool(detail.get("merged") or detail.get("merged_at"))
    mergeable = (
        row.get("mergeable") if row.get("mergeable") is not None else detail.get("mergeable")
    )
    merge_state = row.get("mergeable_state") or detail.get("mergeable_state")
    fp["conflicted"] = _is_conflicted(mergeable, merge_state)
    fp["merge_state"] = merge_state

    # Carry the previous verdict forward when the read is skipped, so a skipped
    # cycle reads as "unchanged" instead of as a verdict being withdrawn.
    fp["review_decision"] = (prev or {}).get("review_decision") or ""
    moved = prev is not None and detail.get("updated_at") != prev.get("pr_updated_at")
    if not fp["merged"] and fp["phase"] in _REVIEW_PHASES and (prev is None or moved):
        fp["review_decision"] = _latest_review_decision(client, key, pr, kwargs)
    return fp


def _latest_review_decision(
    client: Any, key: provider.RepoKey, pr: int, kwargs: dict[str, str]
) -> str:
    """``approved`` / ``changes_requested`` / ``""`` from the PR's newest review.

    The one signal REST does not summarize anywhere on the PR object: an approval
    with no body increments no counter, and ``mergeable_state`` reports both "no
    review yet" and "changes requested" as ``blocked``. So it is read from the
    timeline, and the read is gated on a cheap field having moved first.
    """
    try:
        events = client.list_issue_timeline(key.owner, key.repo, pr, **kwargs)
    except Exception:
        logger.debug("issue-radar: review read failed for #%s", pr, exc_info=True)
        return ""
    latest = ""
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get("kind") != "reviewed":
            continue
        state = str(ev.get("review_state") or "").lower()
        if state in ("approved", "changes_requested"):
            latest = state  # timeline is chronological; the last one wins
    return latest


# ── the sweep ───────────────────────────────────────────────────────────────


def _is_due(item: dict[str, Any], stored: dict[str, Any] | None, now: float) -> bool:
    """Whether this item's phase is due for a re-read on this tick."""
    phase = str(item.get("phase") or "")
    if phase == "selected":
        return False  # pre-claim, local only — nothing public to watch
    if not stored:
        return True
    interval = RECHECK_SEC.get(phase, _DEFAULT_RECHECK_SEC)
    last = stored.get("checked_at")
    if not isinstance(last, (int, float)):
        return True
    # A mark in the FUTURE is due immediately. The mark is wall-clock, so a clock
    # correction (or a machine that resumed with a bad clock) can leave one ahead
    # of now — and an unconditional ``elapsed >= interval`` would then park that
    # item forever, which is the exact silent stall this sweep exists to prevent.
    if now < float(last):
        return True
    return (now - float(last)) >= interval


async def sweep_repo(app: Any, key: provider.RepoKey, root: Path | None = None) -> dict[str, Any]:
    """One zero-LLM pass over every crew's open work items in ``key``'s repo.

    Returns ``{crew_id: [signal, ...]}`` for the crews that were woken (the return
    value is what the tests assert on; the loop ignores it).

    Deliberately NOT gated on the per-repo ``notify_on_new_issue`` setting. That
    flag is a notification preference — whether the bell rings for a new issue —
    and a crew that stopped reconciling its own PRs because the user muted
    notifications would stall every open item with no trace. The app's enabled gate
    is the switch that stops crews, and it stays in ``watch.py``.
    """
    scope = root if root is not None else store.provider_root(
        root=None, provider=key.provider, host=key.host
    )
    crews = await asyncio.to_thread(
        partial(crew_store.list_crews, key.owner, key.repo, scope)
    )
    if not crews:
        return {}
    state = app.get("state") if hasattr(app, "get") else None
    if state is not None:
        # Runs BEFORE the signal pass and over ALL crews, not just the ones with a
        # due item: re-establishing trust and re-arming a lost loop is exactly what
        # a crew with nothing due needs after a restart.
        await watchdog_cycle(state, key.owner, key.repo, crews, scope)
    live = [c for c in crews if is_live(c)]
    if not live:
        return {}

    stored = await asyncio.to_thread(
        partial(read_signals, key.owner, key.repo, scope)
    )
    # WALL clock, not the loop's monotonic one: this value is persisted, and a
    # monotonic reading restarts near zero on the next gateway launch — every
    # stored mark would then sit in the future and no item would ever come due
    # again until the loop clock caught up.
    now = time.time()

    # Pass 1 — which items are due, and which PRs need the batched enrichment.
    due: list[tuple[dict[str, Any], dict[str, Any]]] = []
    open_keys: set[str] = set()
    for crew in live:
        items = await asyncio.to_thread(
            partial(
                crew_store.list_work_items,
                key.owner,
                key.repo,
                str(crew.get("id")),
                scope,
                open_only=True,
            )
        )
        for item in items:
            ikey = _item_key(str(crew.get("id")), item.get("number"))
            open_keys.add(ikey)
            entry = stored.get(ikey)
            if _is_due(item, entry if isinstance(entry, dict) else None, now):
                due.append((crew, item))

    # A mark for an item that is no longer open is dead weight — a crew that works
    # for a month would otherwise carry every issue it ever finished in a file it
    # rewrites every minute. Dropping it also makes a REOPENED item re-seed, which
    # is right: its old fingerprint describes a different state of the world.
    stale = [k for k in stored if k not in open_keys]
    for k in stale:
        stored.pop(k, None)

    if not due:
        if stale:
            await asyncio.to_thread(partial(write_signals, key.owner, key.repo, stored, scope))
        return {}

    prs = sorted(
        {
            pr
            for _, it in due
            if isinstance(pr := it.get("pr_number"), int) and pr > 0
        }
    )
    enriched = await _enrich(key, prs) if prs else {}

    # Pass 2 — fingerprint each due item, compare, then wake each crew ONCE.
    woken: dict[str, Any] = {}
    reasons: dict[str, list[str]] = {}
    crews_by_id = {str(c.get("id")): c for c in live}
    for crew, item in due:
        crew_id = str(crew.get("id"))
        ikey = _item_key(crew_id, item.get("number"))
        entry = stored.get(ikey) if isinstance(stored.get(ikey), dict) else None
        prev = (entry or {}).get("fp") if isinstance((entry or {}).get("fp"), dict) else None
        try:
            fp = await asyncio.to_thread(
                partial(fingerprint_item, key, item, enriched, prev)
            )
        except Exception:
            # A per-item failure leaves its mark UNTOUCHED, so the change is still
            # pending next cycle rather than being silently consumed by the error.
            logger.warning(
                "issue-radar crew sweep failed for %s#%s",
                key.owner,
                item.get("number"),
                exc_info=True,
            )
            continue
        signals = detect_unblocks(prev, fp)
        stored[ikey] = {"fp": fp, "checked_at": now}
        if not signals:
            continue
        woken.setdefault(crew_id, []).extend(signals)
        reasons.setdefault(crew_id, []).append(
            f"#{item.get('number')} {', '.join(signals)}"
        )

    await asyncio.to_thread(
        partial(write_signals, key.owner, key.repo, stored, scope)
    )

    # ONE wake per crew, carrying every reason. Two items signalling in the same
    # sweep is one turn's worth of work, and the second call would be dropped as
    # mid-turn anyway — so the crew would have been told about only the first.
    if state is not None:
        for crew_id, why in reasons.items():
            try:
                await wake_crew(
                    state, key.owner, key.repo, crews_by_id[crew_id], "; ".join(why), scope
                )
            except Exception:  # pragma: no cover - defensive
                logger.warning("issue-radar: waking crew %s failed", crew_id, exc_info=True)
    return woken


async def _enrich(key: provider.RepoKey, prs: list[int]) -> dict[int, dict[str, Any]]:
    """Check rollup + merge state for every watched PR in the repo, batched.

    Two GraphQL calls for the whole repo, not two per PR — and it is the only way
    to see CI at all: a check-run completing does not touch the PR record, so no
    ``updated_at`` anywhere reflects it.
    """
    client = provider.client_for(key)
    kwargs = provider.call_kwargs(key)
    rows = [{"number": n} for n in prs]
    try:
        enriched = await asyncio.to_thread(
            partial(client.enrich_pulls_by_number, key.owner, key.repo, rows, **kwargs)
        )
    except Exception:
        # Best-effort, like every other enrichment caller: an unknown rollup reads
        # as None, which `detect_unblocks` refuses to interpret as a CI change.
        logger.debug("issue-radar: crew sweep enrichment failed", exc_info=True)
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in enriched or []:
        num = row.get("number") if isinstance(row, dict) else None
        if isinstance(num, int):
            out[num] = row
    return out
