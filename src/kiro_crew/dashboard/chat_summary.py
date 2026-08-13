"""Generate intent-level session summaries after a turn completes.

The pass runs on the shared background session, off the turn's critical path,
and is best-effort throughout: every failure path leaves the previous cached
summary in place rather than replacing it with something worse. Nothing here
raises into the caller.

The prompt carries an explicit trap list. Each entry is a transcript shape that
produced a confidently wrong summary when this was prototyped against real
sessions -- not a hypothetical. The mechanically detectable ones (automation
posting under the user role, verbatim resends) are already labelled by
:mod:`kiro_crew.session_summary` before the model sees them; the rest are
judgement calls the prompt has to name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.types import STOP_REASON_END_TURN
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.session_summary import (
    count_user_turns,
    extract_turns,
    last_activity_ts,
    normalize_payload,
    render_input,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

# Role used to pick the model. Summarization is unattended background work, so
# it must not ride the interactive chat flagship on every turn.
_SUMMARY_ROLE = "background"

# Memory modes that forbid deriving durable artifacts from the conversation.
# Mirrors history.INCOGNITO_MEMORY_MODES: a temporary session's transcript is
# discarded, so persisting a summary of it to the .intents sidecar would leave
# conversation content on disk after the conversation itself is gone.
_NO_DERIVED_ARTIFACT_MODES = frozenset({"incognito", "temporary"})

_PROMPT = """\
Summarize this chat session by INTENT, for a panel whose only job is to make \
returning to the session cheap.

An INTENT is a goal the person was pursuing. It can span many turns. It is NOT \
one message, and it is NOT a topic label. A session with a pivot has several \
intents. Turn numbers below refer to the USER turn numbers shown in the \
transcript.

Return ONLY a JSON object, no prose around it, in exactly this shape:

{
  "intents": [
    {
      "title": "short goal, in the person's terms",
      "ranges": [[first_user_turn, last_user_turn]],
      "status": "active" | "completed" | "abandoned",
      "verified": true | false | null,
      "origin_turn": <user turn that triggered this intent, or null>,
      "initial_intent": "why the work started, stated as the person would",
      "progress": ["what is actually true now", "..."],
      "next_steps": [
        {"what": "the action", "why": "why it matters", "expect": "what happens if they do it"}
      ]
    }
  ],
  "constraints": ["how this project has to be run -- recurring operational facts"]
}

FIELD RULES

- "ranges" is a LIST. Use several when an intent went dormant and resumed. \
Ranges may overlap another intent's: an intent pursued inside a longer one is \
real, and forcing them apart would misdescribe the session.
- "status" is about the WORK: active if it is still being pursued, completed if \
it was carried to an end, abandoned if it was dropped.
- "verified" is about the RESULT, and is independent of status. false means the \
outcome was never actually confirmed -- shipped but never looked at, diagnosed \
but never fixed, merged but never run. null means confirmation does not apply. \
This distinction is the single most useful thing in the summary: work whose \
discussion ended while the goal was never reached is exactly what a person \
forgets.
- "progress" is a RUNBOOK, not a history. Say what is true now and what is \
known to work, not a turn-by-turn replay. Length is inversely proportional to \
value here; a long progress list is a worse summary.
- "next_steps" are YOUR inferences, and will be shown as such. Only include \
steps that are genuinely open. An intent that needs nothing gets an empty list.
- "constraints" are session-scoped operational facts about this project -- the \
things the person would otherwise re-learn the hard way (a required flag, a \
step that has to happen after a change, a name they corrected you on). Not \
general preferences. At most a handful, or leave it empty.

BOUNDARY SIGNALS, most reliable first

- A commit, merge, or PR being opened punctuates a session more reliably than \
any phrasing.
- Timestamps separate a routine that is re-run daily from a request that failed \
and was retried. A message repeated across days is a routine.
- A correction from the person ("no", "revert that", "that broke it") is the \
highest-value signal per character in the whole transcript. Weight it heavily \
and never summarize away a correction.

TRAPS -- each of these has produced a wrong summary before

1. A row labelled "[automation, not the user]" is not the person. Never treat \
it as intent, and never create an intent for it.
2. A row labelled as a resend is not insistence and not evidence that the \
request was ignored. The first attempt usually succeeded.
3. A later message can RETRACT an earlier one ("I was wrong", "revert this -- \
it broke my notes"). The retraction is the truth. Never report a feature that \
was withdrawn, or a bug that was walked back, as current.
4. Lines offering the person choices are options they were given, and mostly \
DECLINED. Never read an offer as something the person asked for.
5. Work being merged is not work being verified. If the transcript shows \
something shipped without being run or seen, that intent is completed with \
"verified": false.
6. A question is not a work order. "How hard would it be to..." or "do not \
change anything yet" is not an intent -- if a question caused the next goal, \
record it as that intent's "origin_turn" instead.
7. Facts go stale inside one session: a version, size, path or count stated \
early may have changed by the end. Prefer the latest statement.
8. A context reset or compaction mid-session is not a topic boundary.
9. The session's own title covers only its beginning and is often wrong about \
the whole. Do not anchor on it.

TRANSCRIPT

"""


def _should_summarize(cfg: KiroCrewConfig, slot: Any, user_turns: int | None) -> str:
    """Return "" when a summary should be generated, else the reason to skip.

    Returning a reason rather than a bool keeps the decision auditable in logs:
    a feature that silently declines to run is indistinguishable from one that
    is broken.

    Pass ``user_turns=None`` to run only the cheap slot-level gates -- the
    generator does that before reading the transcript from disk, so the common
    skip cases (disabled, unclean stop) cost no IO.
    """
    if not cfg.session_summary.enabled:
        return "disabled"
    if getattr(slot, "_summary_in_flight", False):
        return "in_flight"
    if getattr(slot, "memory_mode", "") in _NO_DERIVED_ARTIFACT_MODES:
        return "memory_mode"
    # Require EXACTLY a clean end_turn. The marker is cleared at turn start,
    # so an empty value means the turn never reached EVENT_COMPLETE (ACP
    # failure, transport drop) -- summarizing that transcript would cache an
    # incomplete turn as if it finished.
    stop = getattr(slot, "_last_stop_reason", "") or ""
    if stop != STOP_REASON_END_TURN:
        return f"stop_reason:{stop or 'missing'}"
    if user_turns is None:
        return ""
    if user_turns < cfg.session_summary.min_user_turns:
        return "too_few_turns"
    mark = getattr(slot, "_summary_turn_mark", 0) or 0
    if mark and user_turns - mark < cfg.session_summary.regenerate_after_turns:
        return "cadence"
    return ""


def _parse_reply(text: str) -> object:
    """Pull a JSON object out of a model reply, tolerating fenced output."""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        # Strip a fence and any language tag, then the trailing fence.
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[: -len("```")]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        logger.debug("Session summary: reply was not valid JSON")
        return None


async def generate_session_summary(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    cfg: KiroCrewConfig | None = None,
) -> bool:
    """Generate and cache an intent summary for *slot*. Never raises.

    Returns True when a new summary was stored. The in-flight guard is taken
    before the first await so a fast follow-up turn cannot start a second pass
    over the same transcript.
    """
    log = state.conversation_log
    if log is None:
        return False
    # Off-loop: KiroCrewConfig.load() stats and reads config files, and this
    # coroutine runs on the gateway's single event loop. Tests pass cfg in
    # directly to keep the decision path free of file IO.
    if cfg is None:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    # The TRANSCRIPT key, not the slot key. They differ for a channel-born slot
    # the dashboard could not bind, and keying the sidecar on the slot key would
    # stat a file no read path addresses -- the summary would be written to a
    # phantom transcript and the panel would never find it.
    key = slot_history_key(slot)

    # Cheap slot-level gates BEFORE touching the transcript: the common cases
    # (feature disabled, unclean stop) must cost no disk IO.
    skip = _should_summarize(cfg, slot, None)
    if skip:
        logger.debug("Session summary skipped for %s: %s", key, skip)
        return False

    # Land this slot's pending transcript write BEFORE capturing the signature.
    # ``_ChatSlot.append`` only marks the slot dirty; the bytes reach disk on the
    # 5s ``_flush_loop``. This pass is dispatched from ``_finish_queue_cycle`` in
    # the same synchronous block that just appended the turn's final assistant
    # message, so the flush is always still pending here: the signature would be
    # captured from a transcript that is about to change, and the write guard
    # would refuse EVERY summary, on every turn. Not advancing the turn mark does
    # not recover it either -- the next turn reproduces the same ordering.
    #
    # Slot-scoped rather than flushing every dirty slot: a summary has no
    # business writing other sessions' transcripts. ``flush_slot_now`` carries
    # the flush loop's dirty-bit bookkeeping, which matters here — a write that
    # left the slot dirty would just be re-saved by the loop moments later,
    # moving the mtime again and refusing the payload regardless.
    await asyncio.to_thread(state.flush_slot_now, slot)

    # Capture the cache signature BEFORE reading the transcript. The signature
    # must be at least as old as the snapshot it stamps: any append landing
    # after this point advances the mtime, so the write guard in
    # ``set_cached_intent_summary`` refuses the then-stale payload. Captured
    # the other way around, an append between the read and the capture would
    # pair old records with the new mtime and store an incomplete summary that
    # the cache then serves as fresh.
    sig = await asyncio.to_thread(log.session_mtime, key)
    if sig is None:
        # No transcript on disk yet means nothing to key a cache entry on.
        return False

    # Read the FULL chained transcript from disk, not ``slot.messages``: a
    # restored slot keeps only the most recent 500 messages in memory
    # (chat_persistence caps the restore), so summarizing the in-memory tail of
    # a long session would regenerate from a truncated view and overwrite the
    # sidecar -- earlier intents would silently vanish from the panel. Disk is
    # the same source the history endpoint serves, and extract_turns bounds
    # what the model actually reads.
    records = await asyncio.to_thread(log.read_messages_chained, key)
    turns = extract_turns(
        records,
        assistant_excerpt_chars=cfg.session_summary.assistant_excerpt_chars,
    )
    user_turns = count_user_turns(turns)

    skip = _should_summarize(cfg, slot, user_turns)
    if skip:
        logger.debug("Session summary skipped for %s: %s", key, skip)
        return False

    slot._summary_in_flight = True
    try:
        cached = await asyncio.to_thread(log.get_cached_intent_summary, key)
        if cached is not None:
            # The transcript has not changed since the last pass, so the stored
            # summary is still exactly right and the pass would cost tokens for
            # an identical result.
            slot._summary_turn_mark = user_turns
            return False

        prompt = _PROMPT + render_input(turns)
        model = cfg.agent.resolve_model(_SUMMARY_ROLE)
        text = await run_bg_oneliner(
            state.sessions,
            prompt,
            model=model,
            sel_source="session_summary",
        )
        payload = normalize_payload(
            _parse_reply(text),
            max_intents=cfg.session_summary.max_intents,
            max_constraints=cfg.session_summary.max_constraints,
        )
        if payload is None:
            logger.info("Session summary: no usable intents for %s", key)
            return False

        payload["generated_at"] = time.time()
        payload["user_turns"] = user_turns
        payload["last_activity"] = last_activity_ts(turns)
        stored = await asyncio.to_thread(log.set_cached_intent_summary, key, payload, sig)
        if not stored:
            # The transcript was deleted or changed while the model call was in
            # flight; the write was refused so a permanent delete stays deleted.
            # Don't push a WS update for a summary that was never stored, and
            # don't advance the turn mark -- the next turn should retry.
            logger.info("Session summary discarded for %s: transcript gone or moved", key)
            return False
        slot._summary_turn_mark = user_turns
        logger.info(
            "Session summary stored for %s (%d intents, %d user turns)",
            key,
            len(payload["intents"]),
            user_turns,
        )
        # Notify with the SLOT key, not the transcript key used for storage.
        # Two identifiers for two purposes: the sidecar is keyed to the
        # transcript file, while a UI notification has to carry the identifier
        # the dashboard addresses slots by (the same one push_slot_title uses).
        # Sending the transcript key here would broadcast an id no client has a
        # cache entry for, so the panel would silently never refresh.
        state.push_session_summary(slot.key)
        return True
    except Exception:
        # A summary is a convenience. Losing one must never surface as a failed
        # turn, and the previous cached summary stays valid.
        logger.warning("Session summary generation failed for %s", key, exc_info=True)
        return False
    finally:
        slot._summary_in_flight = False
