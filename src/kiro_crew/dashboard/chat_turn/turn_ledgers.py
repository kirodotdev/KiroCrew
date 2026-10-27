"""What a dashboard turn records about itself: its consumption reports, the recovery it queues
and the completion it owes, the terminal directive, and the coding-activity cadence."""

from __future__ import annotations

import inspect
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_delivery import attachment_meta
from kiro_crew.dashboard.chat_utils import RecoveryPayload
from kiro_crew.dashboard.recovery_replays import replays_of
from kiro_crew.wakatime.heartbeats import CADENCE_MIN_INTERVAL_SECS

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        COMMANDS_OFF_META_KEY,
        SUBAGENT_COMPLETION_KIND,
        KiroCrewConfig,
        ReplayFamily,
        _redact_tool_field,
        logger,
        note_coding_cadence,
    )
    from kiro_crew.dashboard.chat_turn.file_changes import _turn_line_changes
    from kiro_crew.dashboard.chat_turn.queue_drain import (
        _RECOVERY_TURN_META_KEY,
        _REPLAYS_COMPLETION_KEY,
        TURN_ACTOR_META_KEY,
    )
    from kiro_crew.dashboard.chat_turn.recipient import (
        MIRROR_ECHO_OWED_META_KEY,
        _mirror_echo_owed,
    )
    from kiro_crew.dashboard.chat_turn.recovery import _session_stop_generation_for
    from kiro_crew.dashboard.session_directive_apply import DirectiveOutcome


async def _report_consumed(turn, consumed: bool = True, *, irreversible: bool = False) -> None:
    _on_consumed = turn._on_consumed
    _on_irreversibly_consumed = turn._on_irreversibly_consumed
    slot = turn.slot
    if irreversible and not turn._irreversible_consumption_reported:
        turn._irreversible_consumption_reported = True
        if _on_irreversibly_consumed is not None:
            try:
                result = _on_irreversibly_consumed()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug(
                    "irreversible consumption report failed for slot %s",
                    slot.key,
                    exc_info=True,
                )
    if turn._consumed_reported != consumed:
        turn._consumed_reported = consumed
        if _on_consumed is not None:
            try:
                _on_consumed(consumed)
            except Exception:
                logger.debug("consumption report failed for slot %s", slot.key, exc_info=True)


def _queue_recovery(
    turn,
    index: int,
    content: str,
    *,
    kind: str,
    payload: str = "",
    extra_meta: dict | None = None,
) -> str:
    """Queue a retry without losing a producer's consumption settlement.

    Stamps FRESH admission context: a recovery entry replays
    externally admitted content verbatim under a new queue id, so without
    its own stamp the drain would either wave it past a link that appeared
    during the retry window (exemption) or destroy every recovery in a
    channel-born session (fail-closed). The requeue is the moment its
    admission is re-affirmed, and the turn's directive provenance rides
    along so the audience exemption follows the original author.

    A requeue of the sender's own text, unchanged, also keeps the sender's
    drop-notice addresses (the channel conversation or the sending session),
    so a retry the drain drops is reported to the same sender the first
    queueing would have told. The text is compared with the row the drain
    handed this turn, never with ``message``: the runner prepends context
    and failure notes to ``message`` before streaming, and a notice quoting
    that text would show the sender words that are not theirs. The
    addresses are read only off that handed row, which this process
    admitted; a row found by scanning the transcript may have been restored
    from disk, and a write target is never taken from there.
    """
    _commands_off = turn._commands_off
    _crew_log_actor = turn._crew_log_actor
    _current_message = turn._current_message
    _current_replay_message = turn._current_replay_message
    _directive_channel_origin = turn._directive_channel_origin
    _directive_user_origin = turn._directive_user_origin
    _on_consumed = turn._on_consumed
    _on_irreversibly_consumed = turn._on_irreversibly_consumed
    _recovery_turn_id = turn._recovery_turn_id
    _turn_is_completion = turn._turn_is_completion
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import (
        CHANNEL_RECIPIENT_META_KEY,
        SEND_ORIGIN_META_KEY,
        containment_meta,
    )

    _address_keys = (CHANNEL_RECIPIENT_META_KEY, SEND_ORIGIN_META_KEY)
    _recovery_meta = containment_meta(state, slot)
    # A caller's extra meta can be the handed row's whole meta, so its
    # addresses are dropped here: the comparison below is their one writer.
    _recovery_meta.update((k, v) for k, v in (extra_meta or {}).items() if k not in _address_keys)
    if isinstance(_current_message, dict) and content == _current_message.get("content"):
        _row_meta = _current_message.get("meta")
        if isinstance(_row_meta, dict):
            for _key in _address_keys:
                if _key in _row_meta:
                    _recovery_meta[_key] = _row_meta[_key]
    if _commands_off:
        # A recovery is a second turn of the SAME message: a channel entry that
        # arrived as turn content stays turn content when its retry drains, or
        # the retry would run the words the original turn was told not to.
        _recovery_meta[COMMANDS_OFF_META_KEY] = True
    if payload == RecoveryPayload.ORIGINAL and isinstance(_current_replay_message, dict):
        # ORIGINAL replays must preserve the triggering row's attachment
        # lists. The provider prompt already carries the full markers, but
        # without these lists the replayed transcript row falls back to a
        # whitespace-bounded path and truncates spaced attachment names.
        _recovery_meta.update(attachment_meta(_current_replay_message.get("meta")))
    if payload == RecoveryPayload.ORIGINAL and _mirror_echo_owed(turn):
        # The person's own words, replayed because the turn died before its
        # mirror echo point: the replay owes that echo (``_prompt_typed_in_dashboard``).
        _recovery_meta[MIRROR_ECHO_OWED_META_KEY] = True
    _recovery_qid = slot.queue_insert(
        index,
        content,
        kind=kind,
        payload=payload,
        # The turn's actor rides along with the admission stamp, and for the
        # same reason: a recovery is a SECOND TURN OF THE SAME ACTOR, so
        # letting it fall back to the default would record an autonudge's or a
        # cron's retry as a user turn. The requeue is the only moment that
        # actor is still known -- the drain sees a fresh queue id and, for a
        # recovery, no kind that names a producer. The ORIGINAL-replay
        # attachment lists merged above ride along in _recovery_meta.
        meta={
            **_recovery_meta,
            TURN_ACTOR_META_KEY: _crew_log_actor,
            _RECOVERY_TURN_META_KEY: _recovery_turn_id,
        },
        on_consumed=_on_consumed if not turn._consumed_reported else None,
        on_irreversibly_consumed=(
            _on_irreversibly_consumed if not turn._irreversible_consumption_reported else None
        ),
        directive_user_origin=_directive_user_origin,
        directive_channel_origin=_directive_channel_origin,
    )
    if content == turn.message and _turn_is_completion:
        # A verbatim requeue still replays the completion; runner-written
        # text queued after it (a continuation, a retry prompt) does not.
        for _entry in slot._queue:
            if _entry.get("id") == _recovery_qid:
                _entry[_REPLAYS_COMPLETION_KEY] = True
                break
    if turn._is_refusal_retry_turn and index == 0 and content == turn.message:
        # A verbatim requeue of the refusal retry's own message REPLACES
        # the consumed replay, whichever recovery family issued it: carry
        # the retry identity and fresh stop snapshots onto the new entry.
        # The per-family guards run at REQUEUE time and cannot see a
        # correction or Stop landing in the requeue-to-dispatch window;
        # the identity puts the requeued entry under the drain's
        # refusal-specific validation (stop generations, rebind,
        # supersession) and the consume-seam re-check, and the re-run
        # stays the episode's one retry (allowance spent) instead of
        # presenting as a genuine turn. It keeps the binding the swap ran
        # under (``_refusal_fallback_session_key``).
        _refusal_key = getattr(slot, "_refusal_fallback_session_key", "")
        replays_of(slot).arm(
            ReplayFamily.CONTENT_FILTER,
            entry_id=_recovery_qid,
            session_key=_refusal_key,
            stop_gen=getattr(slot, "_stop_generation", 0),
            session_stop_gen=_session_stop_generation_for(
                getattr(state, "sessions", None), _refusal_key or session_key
            ),
        )
    return _recovery_qid


def _replay_owes_delivery(turn) -> bool:
    """Whether a verbatim requeue of this turn replays a completion still owed.

    Decided once, at the requeue. A sub-agent completion the model never
    consumed is a result the parent is still owed, so its replay is queued
    as the completion it is (``SUBAGENT_COMPLETION_KIND``, as the sign-in
    retry does) with no recovery-family record: no Stop, rebind or newer
    message cancels it, the admission sweep exempts it, and it runs like
    any queued completion. Only a turn whose message IS the completion
    (``_turn_is_completion``) qualifies: the synthesis turn, a continuation
    or a retry prompt is runner-written, and a turn after the completion
    was consumed owes nothing, so each stays an ordinary, cancellable
    recovery. A hard kill discards it like everything else queued.
    """
    _on_consumed = turn._on_consumed
    _turn_is_completion = turn._turn_is_completion
    slot = turn.slot
    return (
        _turn_is_completion
        and _on_consumed is not None
        and not turn._consumed_reported
        and slot._stop_state != "killing"
    )


def _requeue_owed_completion(turn) -> None:
    """Queue this turn's owed completion again, as a completion."""
    _current_message = turn._current_message
    _queue_recovery(
        turn,
        0,
        turn.message,
        kind=SUBAGENT_COMPLETION_KIND,
        extra_meta=(
            _current_message.get("meta")
            if _current_message is not None and isinstance(_current_message.get("meta"), dict)
            else None
        ),
    )


def _record_terminal_directive(turn, kind: str, outcome: DirectiveOutcome, tcid: str) -> None:
    _terminal_directive_tcids = turn._terminal_directive_tcids
    if not outcome.ends_turn:
        return
    if tcid:
        _terminal_directive_tcids.add(_redact_tool_field(tcid))
    if not turn._terminal_directive_applied:
        turn._terminal_directive_applied = True
        turn._terminal_directive_kind = kind
        turn._terminal_directive_text = outcome.text


def _wt_maybe_cadence(turn) -> None:
    """Emit a throttled cadence heartbeat for CONFIRMED coding activity.

    Fires only after a coding call is confirmed by an approved permission
    decision, never at pre-decision announcement, so a denied tool never
    produces a row. Same scope as the end-of-turn beat: interactive user
    turns only, never a restricted or app slot, and never a gateway-restart
    replay whose actor stamp was lost. Throttled per turn so a burst of
    coding calls yields one beat, giving WakaTime intra-turn timestamps to
    accrue duration without flooding its ~2-minute buckets.
    """
    _crew_log_actor = turn._crew_log_actor
    _turn_provenance_restored = turn._turn_provenance_restored
    slot = turn.slot
    if _crew_log_actor != "user" or _turn_provenance_restored or slot.is_restricted or slot._app:
        return
    _wt_now = time.monotonic()
    if turn._wt_last_cadence == 0.0 or _wt_now - turn._wt_last_cadence >= CADENCE_MIN_INTERVAL_SECS:
        turn._wt_last_cadence = _wt_now
        note_coding_cadence(turn._turn_project or None)


def _wt_note_approved(turn, event) -> None:  # noqa: ANN001 -- ACP event union
    """Count one approved coding call without retaining its id."""
    if turn._wt_deciding_coding:
        turn._wt_coded_this_turn = True
        _wt_maybe_cadence(turn)
    turn._wt_deciding_coding = False


# The opt-in gate, config load, per-file snapshot reads,
# and line-change diff touch disk or run quadratic work,
# so they run off the event loop. Destination credentials
# are deliberately resolved later, inside the send task.
def _wt_collect(turn) -> tuple[Any, int] | None:
    slot = turn.slot
    _cfg = KiroCrewConfig.load()
    if not (_cfg.wakatime.enabled and _cfg.wakatime.send_heartbeats):
        return None
    return _cfg, _turn_line_changes(getattr(slot, "_file_changes", None))
