"""The rows the runner puts into a slot's queue and transcript, and how they are told apart.

The synthetic recovery instructions the runner injects, the structural kinds of its
notices, app messages, cron notifications and sub-agent deliveries, the recovery payload
a replay carries, and the queue operations that pick the next message or edit and
remove a queued placeholder.

Served through :mod:`kiro_crew.dashboard.chat_utils`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from kiro_crew.dashboard.slot_queue_repository import ATTACHMENT_META_KEYS
from kiro_crew.dashboard.state import (
    BUSY_RECOVERY_PREFIX,
    COMPACTION_RECOVERY_PREFIX,
    CONN_RECOVERY_PREFIX,
    CRON_NOTIFY_PREFIX,
    EMPTY_RESPONSE_RECOVERY_PREFIX,
    POSTTOKEN_RECOVERY_PREFIX,
    PROMISE_ONLY_RECOVERY_PREFIX,
    SUBAGENT_COMPLETION_PREFIXES,
)


def _remove_queued_by_id(messages: list[dict], queue_id: str) -> bool:
    """Remove a 'queued' placeholder by its queue id.

    Two spellings, matching the two twin shapes: the cron twin stores
    ``queue_id`` in a JSON ``cls``, while the app twin wears a plain CSS
    ``cls`` and carries ``queueId`` in ``meta`` (the hydration spelling) —
    a cls-only match leaves an unreapable ghost row for every app message
    queued behind a live turn.
    """
    for i, m in enumerate(messages):
        if m.get("role") != "queued":
            continue
        meta = m.get("meta")
        if isinstance(meta, dict) and meta.get("queueId") == queue_id:
            del messages[i]
            return True
        try:
            cls = json.loads(m.get("cls", "{}"))
            if cls.get("queue_id") == queue_id:
                del messages[i]
                return True
        except (json.JSONDecodeError, TypeError):
            pass
    return False


def _edit_queued_by_id(messages: list[dict], queue_id: str, content: str) -> bool:
    """Update the content of a 'queued' placeholder by queue_id stored in cls JSON."""
    for m in messages:
        if m.get("role") != "queued":
            continue
        try:
            cls = json.loads(m.get("cls", "{}"))
            if cls.get("queue_id") == queue_id:
                m["content"] = content
                return True
        except (json.JSONDecodeError, TypeError):
            pass
    return False


# Runner-injected synthetic recovery instructions (defined here — the shared
# utils layer — so BOTH the runner's turn logic and the queue/merge predicates
# below classify them from one source of truth; chat_runner re-exports them).
# The connection-loss and post-transient continuations resume interrupted turns;
# the empty-response nudge breaks the repeated-empty-generation pattern. All are
# orchestration, not user speech.
#
# Each carries a bracketed marker line, matching the recovery prefixes in
# state_parts/injected_turns.py. The marker is what the dashboard matches to fold the row into a
# one-line RecoveryCard instead of printing the machine-facing prose as a
# full-width bubble; it also labels the injection for the model, which reads
# these the same way it reads the refusal/stall continuations.
_CONN_RECOVER_MSG = (
    f"{CONN_RECOVERY_PREFIX}\n"
    "Your previous turn was interrupted by a lost backend connection and has "
    "been automatically recovered. This was NOT a user action — do not treat "
    "it as a cancellation or interruption by the user. The work already done "
    "above is preserved in the conversation. Continue from where it stopped "
    "and finish the request — do not restart it or repeat steps or tools that "
    "already completed successfully. Some of that work may have taken effect "
    "without appearing above, so check the current state (files, commands, "
    "external systems) before repeating any step."
)
_BUSY_RECOVER_MSG = (
    f"{BUSY_RECOVERY_PREFIX}\n"
    "Your previous turn was interrupted because the backend session was still "
    "busy, so the session was reset and the turn automatically recovered. This "
    "was NOT a user action — do not treat it as a cancellation or interruption "
    "by the user. The work already done above is preserved in the "
    "conversation. Continue from where it stopped and finish the request — do "
    "not restart it or repeat steps or tools that already completed "
    "successfully."
)
_POSTTOKEN_RECOVER_MSG = (
    f"{POSTTOKEN_RECOVERY_PREFIX}\n"
    "The previous response was interrupted partway through by a transient "
    "backend error. The work already done above (including any completed tool "
    "results) is preserved in the conversation. Continue from where it stopped "
    "to finish the original request — do NOT restart from scratch and do NOT "
    "re-run steps or tools that already completed successfully."
)
_EMPTY_AUTO_CONTINUE_MSG = (
    f"{EMPTY_RESPONSE_RECOVERY_PREFIX}\n"
    "Your previous turn produced no output (the model returned an empty "
    "response twice). Continue working on the pending request from the "
    "conversation above and respond now — do NOT restart from scratch and do "
    "NOT re-run steps or tools that already completed successfully."
)
_ACTIVITY_NO_REPLY_CONTINUE_MSG = (
    f"{EMPTY_RESPONSE_RECOVERY_PREFIX}\n"
    "Your previous turn did work — it streamed text, called tools, or reasoned "
    "— but ended without a closing reply, so the request looks unanswered. "
    "Everything above already happened and its results are in the conversation: "
    "answer now from what is there. Do NOT restart the request, and do NOT "
    "re-run any tool or step that already completed."
)
#: Shares :data:`EMPTY_RESPONSE_RECOVERY_PREFIX` with
#: :data:`_EMPTY_AUTO_CONTINUE_MSG` rather than minting a marker of its own. The
#: marker is what ``RecoveryCard.tsx`` classifies a transcript row by, and both
#: bodies are the same event to a reader ("the turn ended without an answer, and
#: the runner continued it once"); a second marker would need a card row and a
#: locale pair in twelve catalogs to say nothing new. The BODIES must differ,
#: because this one is read by the MODEL: telling a turn that ran tools that it
#: "produced no output" invites it to redo work whose side effects already
#: landed, which is the failure this whole path exists to prevent.
_PROMISE_ONLY_CONTINUE_MSG = (
    f"{PROMISE_ONLY_RECOVERY_PREFIX}\n"
    "Your previous turn ended right after you said you would perform an action "
    "immediately (for example, opening a PR or running a tool), but the turn "
    "yielded before that action was carried out, so nothing actually happened. "
    "Carry out that action now by making the tool call you announced. But if you "
    "were actually waiting on the user's approval or confirmation, or on a "
    "condition that is not yet satisfied, do NOT perform the action — say what you "
    "are waiting for instead. If it turns out you cannot, say what is blocking it "
    "and what you need instead — do NOT just restate the intention. Do NOT re-run "
    "any tool that already completed successfully above."
)
_COMPACTION_CONTINUE_MSG = (
    f"{COMPACTION_RECOVERY_PREFIX}\n"
    "The conversation above was summarized mid-turn because the context window "
    "filled up, and your previous turn then ended without finishing the request. "
    "The summary is authoritative — earlier messages are gone, so work from what "
    "remains above. Continue the pending request now and respond. Do NOT restart "
    "from scratch, and do NOT re-run any tool or step that already completed "
    "successfully — the summary records what was done. If the summary left you "
    "without something you need, say what is missing instead of guessing."
)
_SYNTHETIC_RECOVERY_MSGS = (
    _CONN_RECOVER_MSG,
    _BUSY_RECOVER_MSG,
    _POSTTOKEN_RECOVER_MSG,
    _EMPTY_AUTO_CONTINUE_MSG,
    _ACTIVITY_NO_REPLY_CONTINUE_MSG,
    _PROMISE_ONLY_CONTINUE_MSG,
    _COMPACTION_CONTINUE_MSG,
)


def is_system_injection(content: str) -> bool:
    """True when a queued message is a system injection (sub-agent completion
    or cron notification) rather than a plain user message.

    .. deprecated::
        Content-only classification is spoofable — a user typing the prefix
        text would be misclassified. Prefer :func:`is_system_injection_item`
        which checks the structural ``kind`` tag first. This function is kept
        only as a backwards-compatibility fallback for queue items enqueued
        before the kind tag was introduced.

    Single source of truth for the predicate that decides which queued
    messages keep draining during a sub-agent run (`_dequeue_next_system_message`),
    which break a user-message merge (`_dequeue_next_message`), and which must
    not consume the session-reset notice (chat_runner drain loop).

    Both sub-agent shapes count: the per-agent event and the wave digest, whose
    prefix is a sibling of the per-agent one rather than an extension of it.
    """
    return content.startswith(SUBAGENT_COMPLETION_PREFIXES) or content.startswith(
        CRON_NOTIFY_PREFIX
    )


#: Structural queue-entry kind for runner-injected recovery instructions.
SYNTHETIC_RECOVERY_KIND = "synthetic_recovery"

#: Structural kind for a false-tool-blocker retry whose TEXT is the authenticated
#: user's original request.  It is recovery orchestration (so it breaks merges and
#: can be purged), but its ``RecoveryPayload.ORIGINAL`` remains user-authored.
FALSE_TOOL_BLOCKER_REPLAY_KIND = "false_tool_blocker_replay"

#: Row-level kind for the `error` notice appended when a recovery has ALREADY
#: been queued, so the frontend can tell a pending retry from a terminal failure.
TRANSIENT_RETRY_KIND = "transient_retry"
#: ``meta.kind`` of the runner's empty-response recovery notice cards (the
#: continue and give-up rungs of the ladder, and the post-compaction resume).
#: They are the runner talking about its own machinery, so a surface that draws
#: only what a person must act on -- the Crewmate chat -- drops them by this
#: tag (``crewmateBubbles.isCrewmateChatRow``), never by matching their words.
EMPTY_TURN_NOTICE_KIND = "empty_turn"

#: ``meta.kind`` of the session-start notice that informs the user their agent
#: spec's ``hooks`` are withheld (project spec not trusted) or never run (backend
#: has no hook channel). Sessions draws them; crewmate chat drops them
#: (``crewmateBubbles.isCrewmateChatRow``), because that chat draws no runtime
#: notice of any kind (the "crewmate chat draws messages only" decision record).
SPEC_HOOKS_NOTICE_KIND = "spec_hooks"

#: ``meta.kind`` of the session-start notice that says Crew's tools are off in
#: this session because a settings file Crew does not govern holds its path.
#: Sessions draws it; crewmate chat drops it (``crewmateBubbles.isCrewmateChatRow``)
#: for the same reason as :data:`SPEC_HOOKS_NOTICE_KIND`.
SEED_DECLINED_NOTICE_KIND = "seed_declined"


#: Row-level kind for the terminal `error` row a prompt-time MODEL ENTITLEMENT
#: rejection produces ("Your account does not have access to model 'X'"), so
#: the frontend can offer the fix (open the model picker / change the default
#: under Settings -> Chat) instead of a Continue button that re-runs the same
#: rejection. No recovery is queued for this kind: a retry cannot earn an
#: entitlement.
MODEL_UNENTITLED_KIND = "model_unentitled"

#: Row-level kind for the terminal `error` row an ``AcpAuthRequired`` turn
#: produces (the agent process reported it is not signed in). Like
#: MODEL_UNENTITLED_KIND, no recovery is queued -- a retry hits the same wall --
#: and the frontend uses the kind to offer the fix that does end it: a deep link
#: to the dashboard's Kiro sign-in card (Settings > Agent Harness), where the
#: user signs in to Kiro Crew's own identity again. The prose stays as the
#: backend formatted it.
AUTH_REQUIRED_KIND = "auth_required"

#: Row-level kind for the terminal `error` row a SPENT PLAN ALLOWANCE produces
#: ("The monthly usage limit has been reached"). Like the two above, no
#: recovery is queued -- the allowance does not come back until it resets, so a
#: retry reproduces the rejection -- and the prose stays as the backend
#: formatted it. The kind exists so a surface that has a NON-inference way to
#: finish what the turn was for can offer it on the row: the header's "Request
#: a Feature" action is an agent turn by design, and without this tag the one
#: moment a user has no inference left was the one moment that action
#: dead-ended (the frontend must never infer the limit from the prose, which a
#: copy edit or a translation moves).
USAGE_LIMIT_KIND = "usage_limit"

#: Row-level kind for the terminal `error` row a SESSION START that never
#: answered produces (``session/new`` / ``session/load`` timed out -- the
#: ``session_start_failed`` tag both ACP exception families carry). Unlike the
#: three kinds above a retry CAN help here once: a cold start under load is
#: host weather, so the first Resume keeps its button and its behaviour. What
#: the kind exists for is the SECOND failure in a row. A start that timed out
#: registered no session, so ``SessionManager.record_failure`` has nothing to
#: count against and every Resume re-issued the identical ``session/new`` with
#: no exit condition -- the transcript IS the count. The Continue endpoint
#: refuses the re-run once two tagged rows sit at the tail with nothing but
#: recovery rows between them, and the error card swaps Resume for the remedy
#: (restart the gateway). Decided from the exception's tag, never from the
#: prose, which a reword or a translation moves.
SESSION_START_FAILED_KIND = "session_start_failed"

#: Row-level kind for the terminal `error` row a CONTEXT OVERFLOW produces: the
#: provider refused the turn because the conversation does not fit in one
#: request (kiro-cli's "context window overflowed", the claude adapter's "Input
#: is too long" -- the ``context_overflow`` tag ``_raise_acp_error`` sets). The
#: native history is what overflowed, so a retry, a Resume or another model
#: re-sends the same history and fails the same way. The card swaps Resume for
#: the fix that ends it: a fresh native conversation on the same slot
#: (``reset-conversation``), which Crew seeds from the transcript's TEXT -- the
#: replay strips image references, so the pictures that usually fill the
#: window are not carried over.
CONTEXT_OVERFLOW_KIND = "context_overflow"

#: Row-level kind for the terminal `error` row a refused app-owned agent turn
#: produces (``apps.manager.app_command_agent_refusal``: the app's agent is
#: reserved, ambiguous or not installed), and for a session start whose agent has
#: no definition at all (``AcpAgentUnavailable``, typically a disabled app's
#: Crew Member). A retry re-runs the same check against the same files, so the
#: frontend withholds Resume on this row and the composer does not urge it; the
#: prose names the fix.
APP_AGENT_REFUSED_KIND = "app_agent_refused"

#: Row-level kind for the terminal `error` row a session start produces when the
#: kiro-cli binary is not installed (the ``kiro_cli_missing`` tag both ACP
#: exception families carry, set where ``kiro_cli_not_found_message`` is
#: raised). The prose names the fix (install it, or set KIROCREW_KIRO_BIN). The
#: kind exists for the same reason as USAGE_LIMIT_KIND: the header's "Request a
#: Feature" action is an agent turn, so on a host with no kiro-cli it dead-ended,
#: and the frontend offers the repository's feature-request form on this row
#: instead. Decided from the exception's tag, never from the prose.
KIRO_CLI_MISSING_KIND = "kiro_cli_missing"

#: Structural queue-entry kinds for system injections.  Classification by kind
#: tag — set at enqueue time — is unforgeable: a user typing the same prefix
#: text will not have the kind tag and will correctly classify as plain input.
SUBAGENT_COMPLETION_KIND = "subagent_completion"
CRON_NOTIFICATION_KIND = "cron_notification"
#: An embedded MCP App's ui/message delivery (SEP-1865 return channel) — see
#: ``dashboard.handlers.mcp_apps.api_mcp_apps_message``. System injection like
#: the two above: app-authored, never user speech, must break user-message
#: merges (folding it into a merged user turn would flip server-authored text
#: into user-authored, persisted, channel-mirrored history).
MCP_APP_MESSAGE_KIND = "mcp_app_message"

#: A runner notice re-routed into a crew member's own next turn — today the
#: automation arm/revision/stop refusal (``session_directive_apply``). A member
#: DM draws messages only and never replays a ``notice`` row to the model, so a
#: refused directive there would reach neither the person nor the crewmate; this
#: kind delivers it as an ``inject`` row the crewmate's turn carries. System
#: injection like the kinds above: runner-authored, never user speech, must
#: break a user-message merge (folding it into a user turn would flip
#: runner-authored text into user-authored, persisted history).
AUTOMATION_NOTICE_KIND = "automation_notice"

#: Provenance banner wrapped around every app-originated message. Structural
#: classification is by the queue entry's ``kind`` tag (unforgeable, set at
#: enqueue time); the banner exists for the MODEL and the transcript reader,
#: so the turn's text says who authored it. Mirrors ``CRON_NOTIFY_PREFIX``.
APP_MESSAGE_PREFIX = "[MCP app message from "
APP_MESSAGE_END = "[End of MCP app message]"

#: Queue-entry kinds that can carry sub-agent delivery debt.
SUBAGENT_DELIVERY_KINDS = frozenset((SUBAGENT_COMPLETION_KIND, SYNTHETIC_RECOVERY_KIND))


#: All system-injection kinds (for set-membership checks).
_SYSTEM_INJECTION_KINDS = SUBAGENT_DELIVERY_KINDS | frozenset(
    (
        CRON_NOTIFICATION_KIND,
        MCP_APP_MESSAGE_KIND,
        AUTOMATION_NOTICE_KIND,
        FALSE_TOOL_BLOCKER_REPLAY_KIND,
    )
)


def app_inject_row(label: str) -> tuple[str, str, dict]:
    """The ONE builder for an app-delivery transcript row.

    Both delivery paths (direct dispatch in ``handlers/mcp_apps.py`` and the
    queue drain in ``chat_runner.py``) call this, so the row cannot diverge
    between them. ``cls`` is a plain CSS class — NEVER a JSON payload: a JSON
    ``cls`` makes ``_prepare_messages`` replace the stored meta with the
    cls-derived dict on the HTTP rebuild path, silently dropping
    ``injectKind`` and with it the row's collapse/fold exemptions. The label
    travels in ``meta`` only, and is never re-derived from the banner text.
    """
    return "inject", "msg msg-inject", {"injectKind": "mcp_app", "appLabel": label}


def is_synthetic_recovery_item(item: dict) -> bool:
    """True when a queue ENTRY is runner-injected recovery orchestration.

    The false-tool-blocker replay carries the authenticated user's own text, but
    its distinct kind still identifies the runner-owned retry for queue ordering
    and late-cancellation purge. Payload provenance remains a separate question.
    """
    return item.get("kind") in (
        SYNTHETIC_RECOVERY_KIND,
        FALSE_TOOL_BLOCKER_REPLAY_KIND,
    )


class RecoveryPayload(str, Enum):
    """Whether a recovery entry's TEXT is runner-authored or the user's own words.

    ``build_recovery_requeue`` already draws this line — a continuation once the
    turn emitted output, the original request before that. Most re-queue under
    ``SYNTHETIC_RECOVERY_KIND``; false-tool-blocker user replays use their own
    purgeable kind. Neither kind can answer whether text may be mirrored to a
    linked thread as user speech, so payload remains the authority classifier.

    ``str`` mixin (not ``StrEnum``) for Py3.10 compat, matching ``ResetCause``.
    """

    CONTINUATION = "continuation"
    ORIGINAL = "original"


# Named by the path callers import it from: the facade that serves it.
RecoveryPayload.__module__ = "kiro_crew.dashboard.chat_utils"


def payload_for_replay(message_is_synthetic: bool) -> RecoveryPayload:
    """The payload tag for a requeue that replays the incoming ``message`` verbatim.

    Asks the only question such a site has: were these the user's words, or the
    runner's? Branching on ``turn_emitted`` instead was wrong — a recovery turn that
    dies before emitting replays the runner's own continuation, and labelling that
    ORIGINAL mirrors internal orchestration to a linked thread as user speech.
    """
    return RecoveryPayload.CONTINUATION if message_is_synthetic else RecoveryPayload.ORIGINAL


def is_synthetic_payload_item(item: dict) -> bool:
    """True when a queue ENTRY's text was written by the runner, not the user.

    Separate question from :func:`is_synthetic_recovery_item`, which answers where
    the entry came from. An untagged entry falls back to the kind because the two
    errors are not symmetric: mirroring runner text as if the user typed it
    misattributes machine orchestration, while suppressing a mirror only loses an
    echo of something the user can already see.

    An MCP-App message entry (``MCP_APP_MESSAGE_KIND``) is synthetic by the same
    asymmetry: its text is app-authored (server-authored), so mirroring it to a
    linked channel as the human's own words would attribute machine speech to a
    person.
    """
    payload = item.get("payload")
    if payload:
        return payload == RecoveryPayload.CONTINUATION
    if item.get("kind") == MCP_APP_MESSAGE_KIND:
        return True
    return is_synthetic_recovery_item(item)


def is_system_injection_item(item: dict) -> bool:
    """Item-aware system-injection predicate for queue-entry consumers.

    Prefers the **structural** ``kind`` tag (set at enqueue time, unforgeable)
    over content-prefix inspection. Content fallback is removed to fully close
    the spoofing gap — classification is exclusively by kind tag.

    Synthetic recovery instructions are orchestration, not user speech: they
    must BREAK a user-message merge (folding one into a "[N queued messages
    merged]" turn would flip it back into user-authored, persisted,
    channel-mirrored history), keep draining during sub-agent runs, and never
    consume the session-reset notice — same treatment as sub-agent completion
    and cron injections."""
    kind = item.get("kind", "")
    if kind in _SYSTEM_INJECTION_KINDS:
        return True
    return False


#: Queue-entry meta key: a requeued steer whose RPC died ambiguously may already
#: have been delivered. The drain prefixes the turn's model input with
#: ``STEER_POSSIBLY_DELIVERED_NOTE``; the user's row keeps the text as typed.
STEER_POSSIBLY_DELIVERED_META = "steer_possibly_delivered"
STEER_POSSIBLY_DELIVERED_NOTE = (
    "[The message below was sent into your previous turn, which then lost its "
    "backend connection before confirming it. It may already have been "
    "delivered: check what was already done, and do not act on it a second "
    "time.]\n\n"
)


#: Queue-entry meta key: the session's own human sent this message asking it to
#: act NOW (the composer's Steer), and it reached the queue only because the
#: running turn could not take it -- the steer was unavailable, or the turn ended
#: before consuming it. The sub-agent hold releases such an entry like a system
#: injection: the sender already chose not to wait for the wave, and parking it
#: behind the children would turn that choice into a Queue they never picked.
STEER_INTENT_META = "steer_intent"

#: Queue-entry meta key: this entry is an AUTOMATION steer (a work-ledger wake)
#: that the running turn ended before taking. Its value is the row's
#: ``meta.nudge`` (``{"cycle", "loop_id"}``). The drain writes the entry as a
#: ``nudge`` row instead of a ``user`` row, and the entry drains alone: merged,
#: its text would be glued onto the human's own queued words in one row.
AUTOMATION_STEER_META = "automation_steer"


def releases_past_subagent_hold(item: dict) -> bool:
    """Whether the sub-agent hold lets *item* drain while children still run.

    System injections keep flowing (see :func:`is_system_injection_item`), and so
    does a human's steer that degraded into a queue entry
    (:data:`STEER_INTENT_META`). Every other user message waits for the wave.
    """
    if is_system_injection_item(item):
        return True
    meta = item.get("meta")
    return isinstance(meta, dict) and meta.get(STEER_INTENT_META) is True


def carries_attachments(item: dict) -> bool:
    """Whether a queue entry's meta names attachment lists (``files``/``dirs``).

    Such an entry drains ALONE. Its text indexes those lists by marker number
    (``[attached_file 1]`` is ``files[0]``), and a merged row has one meta for
    several texts: whichever entry's list won, every other entry's markers
    would resolve against it -- an attachment card that opens a DIFFERENT
    file, not merely a truncated path. The renumbering a correct merge would
    need is not worth building for a message shape the merge feature was never
    about.
    """
    meta = item.get("meta")
    if not isinstance(meta, dict):
        return False
    # A whole-message quote (``meta.quote``) drains alone for the same reason:
    # the row's card strips the quote's block from the START of the content,
    # and a merged row would open with another entry's text instead.
    if isinstance(meta.get("quote"), dict) and meta.get("quote"):
        return True
    return any(isinstance(meta.get(k), list) and meta.get(k) for k in ATTACHMENT_META_KEYS)


def _stamped_turn_actor(item: dict) -> Any:
    """The turn actor stamped on a queue entry's meta, or ``""`` for none."""
    # circular import: chat_delivery imports chat_utils, which loads this module.
    from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY

    meta = item.get("meta")
    return meta.get(TURN_ACTOR_META_KEY, "") if isinstance(meta, dict) else ""


def _dequeue_next_message(slot, merge_enabled: bool) -> tuple:
    """Drain the queue: merge non-cron messages or pop the first one.

    A merge run stops at a system injection, at an attachment-bearing entry
    (see :func:`carries_attachments`), at a possibly-delivered steer
    (``STEER_POSSIBLY_DELIVERED_META``), at a requeued automation steer
    (:data:`AUTOMATION_STEER_META`), at an entry a decision steered
    (``meta.decisions_strip``) and where the stamped turn actor
    changes, so an app's queued send never folds into the user's own words; an
    entry that starts no run pops alone.
    """
    if merge_enabled and len(slot._queue) > 1:
        to_merge: list[dict] = []
        for item in list(slot._queue):
            if (
                is_system_injection_item(item)
                or carries_attachments(item)
                # Its note speaks for one message; merged, it would vouch that
                # messages never written to any pipe may already have run.
                or (item.get("meta") or {}).get(STEER_POSSIBLY_DELIVERED_META)
                # Automation text, never folded into a person's row.
                or (item.get("meta") or {}).get(AUTOMATION_STEER_META)
                # A decision's receipt names one message; a merged row has one meta.
                or (item.get("meta") or {}).get("decisions_strip")
            ):
                break
            if to_merge and _stamped_turn_actor(item) != _stamped_turn_actor(to_merge[0]):
                break
            to_merge.append(item)
        if len(to_merge) > 1:
            del slot._queue[: len(to_merge)]
            merged = "\n\n".join(item["content"] for item in to_merge)
            return f"[{len(to_merge)} queued messages merged]\n\n{merged}", to_merge
    item = slot.queue_pop(0)
    return item["content"], [item]


def _dequeue_next_system_message(slot) -> tuple:
    """Pop the first queued entry the sub-agent hold releases, leaving plain
    user messages queued.

    Implements the (always-on) queue-during-subagents behavior: while background
    sub-agents run for a slot, a tangential user message is held (not drained)
    so it does not start a main turn mid-run, while system injections that must
    keep flowing (sub-agent completions, cron notifications) are still drained,
    and so is a steer that fell back to the queue
    (:func:`releases_past_subagent_hold`). Returns ``(content, [item])`` for the
    drained item, or ``(None, [])`` when only held (user) messages remain queued.
    """
    for i, item in enumerate(slot._queue):
        if releases_past_subagent_hold(item):
            popped = slot.queue_pop(i)
            return popped["content"], [popped]
    return None, []
