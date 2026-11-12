"""What a harness does inside a running turn: take a steer, compact, run a slash
command, and report how the turn ended.

The three steering channels, the three answers to a full context and the inline
``/compact`` done signal, the slash-command memberships (``_kiro.dev`` commands,
``/todos``, a locally answered ``/clear``), how a refusal or a terminal provider
failure is reported, whether a turn reports what it cost, and whether a turn the agent
starts between prompts is read.

One of the owners of the harness vocabulary in :mod:`kiro_crew.agent_sdk.backends`,
which re-exports every name defined here and is the import path callers use. Like that
module it is a leaf: nothing here imports ``kiro_crew.acp``, ``kiro_crew.config`` or
``kiro_crew.platform`` at module scope. ``scripts/check_harness_parity.py`` lists this file
in ``VOCABULARY_PATHS``, which is what lets its ``ACP_BACKEND*`` names be defined here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
)


def escape_user_message_markup(text: str) -> str:
    """Escape delimiters so they cannot occur inside the text they delimit."""
    return text.replace("</user_message>", "&lt;/user_message&gt;").replace(
        "<user_message>", "&lt;user_message&gt;"
    )


def wrap_user_message(text: str) -> str:
    """Wrap escaped text so its echo splits one way and cannot close the model's wrapper."""
    return f"<user_message>\n{escape_user_message_markup(text)}\n</user_message>"


# Backends implementing the ``_session/steer`` extension (mid-turn steer).
# claude-agent-acp does not implement it, so a steer sent there is answered with
# method-not-found rather than reaching the turn. It takes a user's steer over
# ``_session/steering`` instead (see ``ACP_BACKENDS_STEERING_REQUEST``).
# codex-acp (measured on 1.11.0) is not a member: its steer is ``_session/steering``
# (``ACP_BACKENDS_STEERING_REQUEST``), and it cannot carry a deny reason into the
# denied turn. Its command approval offers ``cancel`` as the ONLY reject option, and
# answering it aborts the turn with ``stopReason: "cancelled"``, discarding any steer
# injected while the request was pending. So codex takes the refusal-recovery
# continuation (``dashboard.state.should_queue_refusal_recovery``) instead.
# opencode is not a member: it has no steer verb, and a deny ends its turn. It takes
# a user's mid-turn message as a second ``session/prompt``
# (``ACP_BACKENDS_STEER_CONCURRENT_PROMPT``).
# pi is not a member: pi-acp advertises no steering extension. It takes a user's
# steer through Crew's own gate extension (``ACP_BACKENDS_STEERING_REQUEST``), and a
# pi denial reaches the model as the blocked call's error result inside the turn.
# deepseek is not a member: it advertises close, list and resume only, and permits
# one in-flight prompt per session, so a mid-turn steer has no verb to travel on.
# goose is not a member: its steer is its own request, and it is not shown to
# carry a deny notice (see ``ACP_BACKENDS_STEERING_REQUEST``).
#
# KAS is a member on a LIVE capture against KAS 0.66.43 (kiro-cli 2.32.0):
# ``test/fixtures/acp_frames/kas/steering-live.jsonl``. A ``_session/steer`` sent
# while a shell call ran was answered ``{queued: true}``, echoed as
# ``steering_queued`` then ``steering_injected`` (a ``session_info_update`` with the
# kind under ``_meta.kiro``), and the model obeyed it before that turn's
# ``stopReason``. No lane re-runs it, so a later kiro-cli is not re-measured.
#
# kiro-cli is a member on a LIVE capture against kiro-cli 2.32.0:
# ``test/fixtures/acp_frames/kiro/steering-live.jsonl``. The same steer, sent while a
# shell call ran, was answered ``{queued: true}``, echoed as
# ``AgentExecutionUserMessageQueued`` then ``AgentExecutionSteeringInjected`` (on
# ``_kiro.dev/session/update``), and obeyed before that turn's ``stopReason``.
#
# A member's steer can also carry a refusal reason into the refused turn. A backend
# that takes a user's mid-turn message but not a deny notice belongs in
# ``ACP_BACKENDS_STEERING_REQUEST``; the two are read by ``supports_steer`` and
# ``supports_refusal_steer``.
ACP_BACKENDS_STEER = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends that take a USER's mid-turn message over a REQUEST whose answer is
# awaited, rather than kiro-cli's fire-and-forget ``_session/steer``, and that can
# drop an accepted steer with its turn. Two verbs: ``_session/steering``, which
# ``AcpSessionHandle`` speaks for codex-acp and ``AcpClient`` for
# claude-agent-acp, and goose's ``_goose/unstable/session/steer``, spoken by
# ``AcpClient`` (``_CLIENT_STEERING_REQUEST_METHODS``).
#
# goose, measured against a live goose 1.54.0 (and its ``acp/server.rs``): the
# request is ``{sessionId, expectedRunId, prompt: [ContentBlock]}``, where the run
# id is the one goose announces in ``session_info_update._meta.goose.activeRunId``
# when a prompt starts (null when it ends). While that run is live the answer is
# ``{runId, messageId}`` within milliseconds; with no run, or a stale id, it is
# ``-32602`` ("no active run to steer"). goose drains queued steers between model
# calls and echoes each as a ``user_message_chunk`` with ``_meta.goose.steer`` and
# the same ``messageId``, and the model reads it in that turn. A steer accepted
# and then left queued when the run ends (a cancel, or the loop's last exit) is
# discarded: no echo, and no later model call sees it. So ``AcpClient`` reports a
# goose steer consumed on its echo alone, and otherwise the composer's pending
# entry is queued by the turn's teardown, exactly as for codex below. goose
# advertises no capability for the method; an older goose without it answers
# ``-32601`` and the steer is queued.
#
# codex, measured on codex-acp 1.11.0 and on 2.2.2 in the Real Adapter Contract lane
# (``test_real_codex_acp_steer_is_injected_into_the_running_turn``; frames in
# ``test/fixtures/acp_frames/codex/steering-live.jsonl``): ``initialize`` advertises
# ``_meta.steering.supported: true``; a steer sent mid-turn is answered
# ``{outcome: "injected"}`` and the running turn reads it; with no turn running it is
# answered ``{outcome: "startedNewTurn"}`` and the adapter runs a turn no
# ``session/prompt`` owns. There is no consumption echo, so the answer is the
# delivery evidence.
#
# So ``AcpSessionHandle``, whose reader routes the answer without competing with the
# turn for stdout, AWAITS the answer until it arrives or the turn ends:
# ``injected`` settles the steer in that turn; ``failed`` or no answer reports it
# undelivered so the caller queues it; ``startedNewTurn`` is cancelled first so the
# text cannot run twice. Delivery is at-least-once: codex does not order the answer
# against the turn's terminal, so a steer injected as the turn ends can also run
# again, visibly, as its own turn.
#
# Not ``ACP_BACKENDS_STEER``: codex discards an injected steer with a turn its
# approval answer cancels, and the same happens to an accepted user steer when a
# later approval is denied or the turn is cancelled. So a codex steer is reported
# consumed only when its turn ends cleanly; otherwise the turn's teardown queues the
# caller's pending entry. Only the dashboard composer keeps such an entry, so
# membership also means ``steer_needs_loss_recovery``: the messaging channels, Side
# Chat and ``spawn_steer`` refuse the steer and queue instead.
#
# claude-agent-acp takes the same request, on its own evidence: read from the
# adapter's ``dist/acp-agent.js`` and driven live on 0.65.0 and 0.89.1 (the real
# adapter, Agent SDK and native ``claude`` binary, with the model API served
# locally). ``initialize`` advertises ``_meta.steering.supported: true``.
# ``steer()`` finds the session's unsettled turn and pushes the text onto the
# streaming input the Agent SDK query reads, answering ``injected``; the turn is
# held open until a result answers the steer, so the model's next request carries
# the steered text and the ``session/prompt`` terminal comes after its reply. A
# steer sent with ``_meta.steering.idleBehavior: "promptRequired"`` to a session
# with no running turn is answered ``promptRequired`` and starts nothing. 0.64.x
# answers ``injected`` and then ends the turn without running the text, which is
# why ``AcpClient`` speaks the request only from 0.65.0. A permission
# awaiting the user is not cancelled by a steer (it is delivered at the SDK's
# ``later`` priority), and Crew's reject is the adapter's ``reject`` option, a
# ``deny`` without ``interrupt`` that does not end the turn. So unlike codex a
# denial drops nothing, and the steer is reported consumed when the turn ends
# with ``end_turn``; a cancelled or failed turn queues it. claude runs one adapter per
# session, so ``AcpClient`` speaks the request for it, and only when the handshake
# advertised it.
#
# pi is a member on a LIVE capture against pi-acp 0.0.34 over pi 1.4.0, driven through
# Crew's own ``AcpClient``: ``test/fixtures/acp_frames/pi/steering-live.jsonl``. pi-acp
# has no steer verb, and a second ``session/prompt`` lands in its turn queue as a new
# turn after the running one, so no request reaches the turn; the steer bypasses the
# adapter. ``AcpClient`` writes it as a file sealed for Crew's gate extension inside
# pi (``acp.harness.pi_steer``: owner-only directory the sandbox mounts read-only,
# AES-256-GCM under a per-launch key bound to the gate nonce, the pi session id and a
# strictly increasing sequence number). At each ``turn_end`` that did not abort or
# fail, the extension puts the oldest undelivered steer on pi's steering queue with
# ``pi.sendMessage(message, {deliverAs: "steer"})``, which queues it synchronously
# while pi streams and which pi hands the model as a user message; pi's agent
# loop reads that queue right after the boundary and before its next model call in
# the same run (``agent-loop.js``; no ``finishTurn`` in 1.3.0 or 1.4.0 ends a run there).
# ``pi.sendUserMessage`` is not used: it awaits pi's input handlers before it queues,
# so its steer could miss that read and surface in a later run. When pi starts the message the extension notifies an envelope naming the
# steer, which pi-acp forwards as an ``agent_message_chunk`` with
# ``_meta.piAcp.notify``; that echo is reported as ``EVENT_STEER_CONSUMED``. In the
# capture a steer written during a running ``bash`` call was echoed after the tool
# result, the model's next request carried it as the latest user message, and its
# answer named the steered word before that ``session/prompt`` ended ``end_turn``.
# A steer the run settles without reading -- the turn was cancelled, or failed, or
# the steer arrived after its last ``turn_end`` -- is retired by the extension and
# removed by the client at the turn's terminal, never read into a later turn, and the
# caller queues it: membership here means ``steer_needs_loss_recovery``, like goose.
ACP_BACKENDS_STEERING_REQUEST = frozenset(
    {ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE, ACP_BACKEND_GOOSE, ACP_BACKEND_PI}
)

# Backends that take a USER's mid-turn message as a second ``session/prompt`` sent
# while the first is still running. Spoken only by ``AcpClient``, the driver these
# harnesses run on.
#
# Read from opencode 1.19.0's source and measured against it live. Its ACP
# ``session/prompt`` hands the text to the server's own prompt call, which writes
# the user message and then JOINS the session's running loop instead of starting
# a second one. That loop re-reads the whole history before every step, and it
# exits only once its last answer replies to the LATEST user message. A steer sent
# while a bash tool ran was obeyed by that same turn's final answer, and both
# requests were answered together at the same idle, with the same usage. A steer
# sent after the turn ended ran a turn of its own and was answered seconds later.
#
# There is no consumption echo, so that timing is the delivery evidence: a steer
# whose answer comes with the turn's own, with no output of its own before it, was
# read inside the turn. ``AcpClient`` waits a short grace for it at the turn's
# clean end; a steer that ran a loop of its own is cancelled and left pending, and
# the composer queues it. A denied approval ends opencode's loop without reading
# what came after it, so nothing is reported consumed in a turn with a denial.
# That makes membership here mean ``steer_needs_loss_recovery`` as well, like
# codex.
#
# The live measurement was first taken on 1.19.0, and is captured on 1.19.1, the
# Real Adapter Contract lane's pin (``test/real_adapters/package.json``):
# ``test/fixtures/acp_frames/opencode/concurrent-prompt-steer-live.jsonl``. A
# second ``session/prompt`` sent while the turn's bash ran reached the model in
# the turn's one remaining request, beside the bash result, and the two prompts
# were answered ``end_turn`` together. ``test/test_opencode_concurrent_prompt_steer.py``
# drives a scripted stdout, so no lane re-measures this membership against a real
# opencode.
#
# Not ``ACP_BACKENDS_STEER``: a deny notice cannot ride this verb, because the
# denial already ended the turn it was meant for.
#
# copilot is NOT a member, on measurement against Copilot CLI 1.0.100: a second
# ``session/prompt`` sent four seconds into a running turn REPLACED it -- the first
# request answered ``stopReason: cancelled`` the moment the second arrived, and only
# the second ran -- so it interrupts rather than joins. It is in no steer set:
# ``_session/steering`` answered ``-32601``, and it advertises no other channel.
ACP_BACKENDS_STEER_CONCURRENT_PROMPT = frozenset({ACP_BACKEND_OPENCODE})

# Backends whose ``_session/steer/clear`` drops the steers the running turn has not
# read yet and names exactly those, so a pending steer can be taken back. Read by
# ``supports_steer_retract``; the dashboard's Unsend, Edit and Queue instead on a
# pending steer exist only on a member.
#
# kiro-cli is a member on a LIVE capture against kiro-cli 2.33.1:
# ``test/fixtures/acp_frames/kiro/steering-clear-live.jsonl``. Two steers sent while
# a shell call ran were each answered ``{queued: true}`` and echoed as
# ``AgentExecutionUserMessageQueued``. ``_session/steer/clear`` was answered
# ``{cleared: true}`` and followed by ``AgentExecutionUserMessageCleared`` naming
# both, and the turn's answer obeyed neither. A steer sent after the clear was
# injected and obeyed. The clear drops EVERY pending steer, so the caller sends
# again the ones it did not mean to take back.
# ``test/fixtures/acp_frames/kiro/steering-clear-after-inject-live.jsonl`` is the
# other order: a steer already reported by ``AgentExecutionSteeringInjected`` is not
# named by a later clear, which names only the steer still pending, and a clear
# with nothing pending sends no ``AgentExecutionUserMessageCleared`` at all. The
# answer is the same ``{cleared: true}`` either way, so only that notification is
# evidence of a drop.
#
# KAS is not a member, measured against KAS through kiro-cli 2.33.1
# (``--agent-engine v3``): its clear answered ``{cleared: true, messageIds: [...]}``
# naming two steers ``steering_injected`` had already reported, and the turn obeyed
# both. Its answer and its ``steering_cleared`` name steers the turn already read,
# so neither can tell a dropped steer from a delivered one.
ACP_BACKENDS_STEER_RETRACT = frozenset({ACP_BACKEND_KIRO})

# Backends that can serve a MANUAL ``/compact`` (the user-typed slash command).
# Every member acts on the ``/compact`` prompt that ``AcpProvider.compact()``
# sends. They do not all answer it the same way, and that split is
# ``ACP_BACKENDS_INLINE_COMPACTION``: kiro-cli ACKs the prompt then emits
# ``_kiro.dev/compaction/status``, which ``wait_for_compaction()`` picks up, while
# claude-agent-acp, codex-acp, opencode, goose and pi finish the whole compaction inside
# the ``session/prompt`` turn.
#
# opencode is a member on a LIVE capture although its ``available_commands_update``
# lists no ``compact``: it serves ``/compact`` from its PROMPT handler, so the command
# list cannot answer the question. On 1.19.1, the Real Adapter Contract lane's pin,
# ``test/fixtures/acp_frames/opencode/compact-live.jsonl`` climbs 7566 -> 12528 over
# seven turns, reads ``used: 3720`` on the ``/compact`` turn, ends it ``end_turn``
# with no status frame -- the ABSENCE this membership rests on -- and reads 10135 on
# the next ordinary turn.
#
# codex is a member on a capture. codex-acp advertises ``compact`` in its
# ``available_commands_update``, intercepts the ``/compact`` prompt as that command,
# and answers the same ``session/prompt`` once the compaction is done. 1.11.0 sends a
# tool-call pair MARKED with ``_meta.contextCompaction``; 2.2.2 and 2.3.0 send it
# UNMARKED (Real Adapter Contract lane,
# ``test/fixtures/acp_frames/codex/compact-live.jsonl``).
# ``_dispatch.parse_codex_compaction_update`` reads both shapes as a compaction
# status, so the waiter is satisfied inside the turn. Its own auto-compaction fires
# only with ``model_auto_compact_token_limit`` set (measured on 1.11.0, not on the
# lane's pin): without it a session held at 50k tokens over three turns never
# compacted.
#
# goose is a member on a LIVE capture, measured on goose 1.50.1. Driven over three turns,
# its ``usage_update.used`` climbed 1529 -> 1991 -> 2453. A ``/compact`` prompt then
# answered ``Compaction complete`` inside the turn, read ``used: 224``, and returned
# ``stopReason: end_turn`` with no status frame after it. The next ORDINARY turn read
# 1749, below the pre-compact peak. The drive is committed as evidence:
# ``test/fixtures/acp_frames/goose/compact-live.jsonl``. Its source agreed before the
# drive did: ``Agent::reply`` routes ``/compact`` through ``execute_command`` to
# ``handle_compact_command``, and ``command_starts_turn("/compact")`` is false.
#
# pi is a member on a LIVE drive against pi-acp 0.0.34 over pi 1.4.0, taken with
# ``scripts/capture_acp_compaction.py`` and a locally served model named in pi's own
# ``models.json``. Over four ordinary turns its ``usage_update.used`` climbed
# 13484 -> 25572 -> 37611 -> 49649. A ``/compact`` prompt then answered pi's own
# ``Compaction completed`` chunk and returned ``stopReason: end_turn`` with no
# ``usage_update`` and no status frame after it, and the next ORDINARY turn -- which
# added another ~12k-token prompt -- read 13597, below the pre-compact peak. The
# drive is committed as evidence: ``test/fixtures/acp_frames/pi/compact-live.jsonl``.
# The source agrees: pi-acp intercepts ``/compact`` in ``prompt()``, awaits
# ``session.proc.compact(...)`` and returns ``{ stopReason: "end_turn" }``.
#
# kas is a member, and the one member not handed the ``/compact`` prompt: as prompt
# text it makes the model WRITE a summary and the meter does not move. What compacts
# is the engine's own verb, ``_kiro/session/compact``, advertised in ``initialize``'s
# ``_meta.kiro.extensionMethods``; it answers ``{"success": false}`` while a turn is
# running. So ``AcpSessionHandle.prompt`` sends the verb for a KAS ``/compact``, and
# the outcome arrives as a compaction status inside that turn. KAS stays in
# :data:`ACP_BACKENDS_HARNESS_MANAGED_COMPACTION` too: the global threshold leaves it
# to its own summarization, and an explicit threshold (the slider or an agent's
# ``autocompact_pct``) is served through the same verb.
# LIVE on KAS 0.66.43 (kiro-cli 2.32.0), ``test/fixtures/acp_frames/kas/compact-live.jsonl``:
# after three turns read 6.31% -> 8.64% -> 10.76%, the verb answered
# ``{"success": true}`` after ~12s with ``summarization_completed`` before it, and
# the next turn read 4.51%. No lane re-runs it, so a later kiro-cli is not re-measured.
#
# kiro is a member on a LIVE capture against kiro-cli 2.32.0:
# ``test/fixtures/acp_frames/kiro/compaction-live.jsonl``. A ``/compact`` prompt is
# answered ``end_turn`` first; ``_kiro.dev/compaction/status`` then sends
# ``{type: started}`` and ``{type: completed}`` with the summary. Over three turns
# ``contextUsagePercentage`` read 21.99 -> 30.40 -> 38.82, and the next ordinary
# turn read 30.47. The one metadata frame right after ``completed`` read 81.03.
#
# claude is a member on a LIVE capture against claude-agent-acp 0.94.0:
# ``test/fixtures/acp_frames/claude/compact-live.jsonl``. After two ordinary turns
# (``used`` 31250), the ``/compact`` turn is one ``tool_call`` stamped
# ``_meta.claudeCode.toolName: compact``, completed inside the same
# ``session/prompt`` with ``rawOutput`` ``preTokens`` 31250 -> ``postTokens`` 4148,
# then ``end_turn`` with no status frame. The next ordinary turn read 26171.
#
# copilot is a member on a LIVE drive against Copilot CLI 1.0.100. Over three large
# turns its ``usage_update.used`` climbed 22058 -> 30579 -> 39041. A ``/compact``
# prompt then answered its own ``Compacted conversation history ...`` chunk, read
# ``used: 19913`` and returned ``stopReason: end_turn`` with no status frame after
# it, and the next ORDINARY turn read 20843, below the pre-compact peak
# (``test/fixtures/acp_frames/copilot/compact-live.jsonl``). ``compact`` is in its
# ``available_commands_update`` and in ``ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST``, so
# the prompt guard lets it through.
#
# deepseek is the one non-member:
# :data:`ACP_BACKENDS_CONTEXT_RECYCLE` carries the consequence, because deepseek
# reports no compaction at all, so a decline leaves its context unbounded.
ACP_BACKENDS_COMPACT = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_PI,
        ACP_BACKEND_COPILOT,
    }
)

# Backends that compact on their OWN initiative and report it on their ACP surface,
# so Crew's context meter falls back below the threshold without Crew acting.
#
# This set makes a decline HONEST: a member is left alone at the global
# ``session.autocompact_pct`` because the context is bounded anyway. A threshold set
# for the session or its agent is Crew's to honour, so Crew compacts there through
# :data:`ACP_BACKENDS_COMPACT`.
#
# KAS is the member. It runs auto-summarization and emits ``summarization_started``
# / ``summarization_completed``, which ``acp.kas_wire`` maps to a compaction status
# that calls ``reset_after_compaction()`` on the meter (``acp/session_handle_kas.py``),
# so a reading that crossed the threshold drops on its own. LIVE on KAS 0.66.70
# (kiro-cli 2.36.0) with a low threshold, one turn crosses it and the engine sends
# both frames with no client request (acp_frames/kas/auto-compaction-live.jsonl).
#
# A backend outside both this set and ``ACP_BACKENDS_COMPACT`` has no compaction Crew
# can see, so nothing bounds its context. :data:`ACP_BACKENDS_CONTEXT_RECYCLE` decides
# what happens then: a member is recycled at the threshold; a backend in none of the
# three is DECLINED and the gate logs a WARNING naming the memberships it lacks,
# because the session-ending arm must not be granted by exclusion.
#
# deepseek is not a member. Its frames
# (``test/fixtures/acp_frames/deepseek/handshake-live.jsonl`` and ``turn-live.jsonl``)
# show no compaction on its ACP surface: no ``available_commands_update``,
# ``session/load`` answers ``"Method not found"``, and no compaction capability is
# advertised. Whether it summarizes for itself behind that surface is claimed in
# neither direction (the capture stops at ``used`` 7695 of ``size`` 8192); the
# membership rests on what Crew can act on, which is no status to wait for and no
# command to send. Because this is an ABSENCE, the frames are enough; a positive
# claim in ``ACP_BACKENDS_COMPACT`` needs a harness DRIVEN.
#
# kiro-cli is not a member: no capture shows it compacting on its own, and its
# ``/compact`` is served through ``ACP_BACKENDS_COMPACT``.
ACP_BACKENDS_HARNESS_MANAGED_COMPACTION = frozenset({ACP_BACKEND_KAS})

# Backends whose FULL context is answered by recycling the session, because no
# compaction reaches them from either side.
#
# The third of three answers to "what happens when this context fills", and the
# only destructive one, which is why it is a membership rather than the leftover.
# A member is a harness Crew cannot hand ``/compact`` to
# (:data:`ACP_BACKENDS_COMPACT`) AND that reports no compaction of its own
# (:data:`ACP_BACKENDS_HARNESS_MANAGED_COMPACTION`), so its context grows until the
# harness's own window ends the conversation for it. Recycling at
# ``session.autocompact_pct`` bounds it, at the cost of what the agent remembered
# — the same cost the window exacts anyway, taken while the session is still
# usable.
#
# deepseek is the one member. Its ACP surface emits no
# ``available_commands_update`` at all (``session/load`` is already
# ``Method not found``) and its ``session/update`` vocabulary carries no compaction
# status, while its ``usage_update`` reports a real meter (``used`` 7554 -> 7695 of
# ``size`` 8192 in one captured turn) — a reading with nowhere to go.
#
# A harness in NONE of the three sets is not a member: granting this by exclusion
# would hand a conversation-ending behaviour to every harness added later without
# anyone deciding it. Such a harness declines, is told so in its own words
# (:func:`compact_unsupported_reply` has a sentence for this case), and the gate
# logs a WARNING naming the gap.
ACP_BACKENDS_CONTEXT_RECYCLE = frozenset({ACP_BACKEND_DEEPSEEK})

# Backends that finish a manual ``/compact`` INSIDE the ``session/prompt`` turn,
# so the turn's terminal frame is the done signal and there is no asynchronous
# compaction status to await. A NON-member emits the result separately and the
# caller must wait for it (``AcpProvider.wait_for_compaction``); awaiting a
# member instead strands the waiter for the full timeout, and telling a
# non-member it is done leaves the user's ``/compact`` silently unacknowledged.
#
# A STRICT SUBSET of ``ACP_BACKENDS_COMPACT``. kas is not a member: its compaction
# is a separate request whose outcome is a compaction status the turn carries.
# kiro-cli is not a member: it ACKs the prompt and then emits
# ``_kiro.dev/compaction/status``, the asynchronous result a non-member has.
#
# A set rather than an ``is_claude_backend`` check, so the synchronous arm goes to
# the harnesses that demonstrate it and not by identity (harness-parity H6).
#
# Every member, on the captures recorded on ``ACP_BACKENDS_COMPACT``, ends its
# ``/compact`` turn with ``stopReason: end_turn`` and sends no compaction status
# after it, so awaiting one would strand the waiter: claude; codex, whose completed
# compaction ``tool_call_update`` arrives BEFORE the ``session/prompt`` response;
# opencode, which sends no status frame at all; goose (``Compaction complete``
# chunk); pi (``Compaction completed`` chunk); copilot, whose chunk and
# ``usage_update`` land before its ``end_turn``.
ACP_BACKENDS_INLINE_COMPACTION = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_PI,
        ACP_BACKEND_COPILOT,
    }
)

# Inline-compaction members whose compaction pair may arrive UNMARKED, and so are
# read by ``_dispatch.CODEX_COMPACTION_TITLE`` on a ``think`` call plus the start's
# ``toolCallId``. codex is the member: codex-acp 2.3.0 sends the pair with no
# ``_meta``, captured in ``test/fixtures/acp_frames/codex/compact-live.jsonl``.
# Every other member is read by the marker alone, so its own ``think`` call with
# that title is never taken for a compaction.
ACP_BACKENDS_UNMARKED_COMPACTION_PAIR = frozenset({ACP_BACKEND_CODEX})

# The harnesses whose sessions keep a ``prompt_blocks.SentImageLedger``: the ones
# whose compaction, started by Crew or by the harness itself, sends a frame this
# client reads, so the record is forgotten whenever the conversation drops its
# pictures. A harness not shown to report its own compaction stays out and inlines
# a named picture on every turn, so the model is never told it holds a picture a
# silent compaction dropped (harness-parity H6).
#
# - kiro: ``_kiro.dev/compaction/status`` (``acp_frames/kiro/compaction-live.jsonl``).
# - kas: ``summarization_started`` / ``summarization_completed`` on its own
#   (``acp_frames/kas/auto-compaction-live.jsonl``).
# - codex: the marked compaction ``tool_call_update`` on a manual ``/compact``
#   and on a native auto-compaction alike (``acp_frames/codex/compact-live.jsonl``).
# - claude: the standard ACP ``compaction_update`` the adapter sends because Crew
#   declares ``session.compaction`` (acp-client spec, "Native adapter compaction
#   lifecycle"), and the ``Compacting...`` text notice of an adapter that sends none.
#
# Out: opencode, goose and pi send nothing this client reads for a compaction
# they start on their own; copilot, strands and deepseek have no capture of one.
ACP_BACKENDS_SENT_IMAGE_RECORD = frozenset(
    {ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE}
)

# How long each backend's provider keeps a conversation's prompt prefix cached
# after the last request, in seconds, for ``session.idle_compaction``. A backend
# absent from the table, or whose resolver answers ``None``, has a lifetime Crew
# cannot know and is never idle-compacted: guessing too long compacts after the
# cache is gone, guessing too short spends a model call for nothing.
#
# kiro: 300 s, measured from recorded kiro-cli 2.32.0 turns. A turn starting at
# 290-300 s after the previous one ended paid more than twice the warm cost on 21%
# of turns, and one starting at 300-310 s on 99%.
# codex: 1800 s. The provider documents a 30 minute minimum after the last write or
# reuse, and codex-acp sends a stable cache key with no retention override.
# claude: only when ``CLAUDE_CODE_PROMPT_CACHE_TTL`` names the tier. Claude Code
# picks a one hour or a five minute cache from the account's billing state, which
# Crew cannot see, so with the variable unset the backend is skipped.
# kas manages its own compaction, deepseek can only be recycled, and opencode,
# goose and pi cache at whatever provider the user configured: all absent.
CLAUDE_PROMPT_CACHE_TTL_ENV = "CLAUDE_CODE_PROMPT_CACHE_TTL"
#: The values of ``CLAUDE_PROMPT_CACHE_TTL_ENV`` Crew reads, in seconds.
CLAUDE_PROMPT_CACHE_TTLS: Mapping[str, float] = MappingProxyType({"5m": 300.0, "1h": 3600.0})


def claude_prompt_cache_lifetime(env: Mapping[str, str]) -> float | None:
    """The claude cache tier *env* names, or ``None`` when it names none Crew knows."""
    return CLAUDE_PROMPT_CACHE_TTLS.get(str(env.get(CLAUDE_PROMPT_CACHE_TTL_ENV, "")).strip())


PROMPT_CACHE_LIFETIMES: Mapping[str, Callable[[Mapping[str, str]], float | None]] = (
    MappingProxyType(
        {
            ACP_BACKEND_KIRO: lambda _env: 300.0,
            ACP_BACKEND_CODEX: lambda _env: 1800.0,
            ACP_BACKEND_CLAUDE: claude_prompt_cache_lifetime,
        }
    )
)

#: A lifetime at or under this many seconds fires a fixed margin early; a longer
#: one fires at a fraction of itself.
PROMPT_CACHE_SHORT_LIFETIME_SECS = 600.0
#: How early an idle compaction fires on a short lifetime. It covers the time from
#: the trigger to the request reaching the provider. The lifetime counts from the
#: start of the last request, so the timer is measured from the provider's request
#: floor (the start of the turn's last model request), not from the turn end. This
#: one value sets kiro's fire time, 255 s, which the idle-compaction RFC's Phase 1
#: entry measurement kept; the 255 to 285 s sweep is still open there.
PROMPT_CACHE_SHORT_MARGIN_SECS = 45.0
#: The fraction of a long lifetime after which an idle compaction fires.
PROMPT_CACHE_LONG_FIRE_FRACTION = 0.9


def prompt_cache_lifetime_secs(backend: str, env: Mapping[str, str]) -> float | None:
    """The prompt-cache lifetime *backend* runs with under launch *env*, or ``None``."""
    resolver = PROMPT_CACHE_LIFETIMES.get(backend)
    return None if resolver is None else resolver(env)


def idle_compaction_delay_secs(lifetime_secs: float) -> float:
    """Seconds of idleness after which a chat with this cache lifetime is compacted."""
    if lifetime_secs <= PROMPT_CACHE_SHORT_LIFETIME_SECS:
        return max(lifetime_secs - PROMPT_CACHE_SHORT_MARGIN_SECS, 0.0)
    return lifetime_secs * PROMPT_CACHE_LONG_FIRE_FRACTION


# Backends implementing ``_kiro.dev/commands/execute`` — the kiro extension that
# runs a slash command as an RPC. Non-members have no equivalent verb, so their
# slash commands go through ``session/prompt`` and are interpreted by the adapter
# (or degrade to prompt text) instead of returning -32601 for the whole call.
#
# Membership means "Crew may SEND this verb", and nothing wider: the kiro-family
# reads (Crew's agent spec, the native set-model request, the effort live path)
# stand on ``ACP_BACKENDS_KIRO_FAMILY`` below.
# KAS is not a member: it refuses the verb with -32603 "has no persistence
# classification" for every command tried (``tools``, ``context``, ``effort``),
# captured in ``test/fixtures/acp_frames/kas/commands-refused-live.jsonl``. Its
# effort travels as the ``effortLevel`` config option, and the dashboard answers
# its other harness commands itself.
# opencode is not a member: it has no ``_kiro.dev`` verb, and it publishes its own
# command list as an ``available_commands_update`` on ``session/update`` instead.
# pi is not a member for the same reason: pi-acp publishes its built-ins the same
# way and has no ``_kiro.dev`` verb.
# deepseek is not a member and publishes no command list either: it carries commands
# internally and its ACP surface rejects them, so it exposes none over the wire.
#
# kiro-cli is a member on a LIVE capture against kiro-cli 2.32.0:
# ``test/fixtures/acp_frames/kiro/commands-execute-live.jsonl``, where the object form
# ``{command: effort, args: {level: high}}`` answered ``{success: true}``.
ACP_BACKENDS_KIRO_SLASH_COMMANDS = frozenset({ACP_BACKEND_KIRO})

# The kiro family: kiro-cli and KAS, the two harnesses Crew spawns through
# ``kiro-cli``. Membership is not a capability of its own. It is the family the
# readers below mean when they mean "kiro-cli or KAS", so none of them stands on a
# capability set (the command verb above, or the runtime set, which codex shares
# for TRANSPORT reasons) that happens to hold the same two members today:
#
# - the card's crew-tools, reasoning-effort and model-switch lines, where the
#   family loads Crew's agent spec and takes the native set-model request;
# - ``AcpProvider``'s effort live path, which re-applies an unpinned level and
#   pushes a cleared default down whichever channel the capability layer names;
# - the gate in front of the workspace ``cli.json`` overlay writers, each of which
#   is scoped again to its own narrower reader set.
ACP_BACKENDS_KIRO_FAMILY = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends that interpret ``/todos`` as a native prompt command. This is separate
# from ``ACP_BACKENDS_KIRO_SLASH_COMMANDS``: that set names the private RPC used by
# the kiro family, while this command is implemented by claude-agent-acp inside
# ``session/prompt``. A new backend stays out until its adapter demonstrates the
# same command, so an unknown harness is refused rather than receiving prompt text
# it may interpret as an ordinary user request.
ACP_BACKENDS_NATIVE_TODOS = frozenset({ACP_BACKEND_CLAUDE})

# Backends whose adapter runs ANY leading-slash ``session/prompt`` as one of its own
# commands, so the dashboard forwards every leading-slash word raw (no context
# prefix) rather than only the kiro-cli names in ``_SLASH_COMMANDS``. claude is the
# member: claude-agent-acp hands the prompt to the Agent SDK, which resolves
# ``/review``, ``/init``, ``/security-review``, ``.claude/commands`` and skills by
# name. Its ``/compact`` capture, ``test/fixtures/acp_frames/claude/compact-live.jsonl``,
# shows a leading-slash prompt run as a command inside ``session/prompt``.
#
# kiro and KAS are not members: kiro-cli runs only its own command names over
# ``_kiro.dev/commands/execute``, and KAS answers its commands through the
# dashboard. Every other harness stays out until its adapter is shown to run an
# arbitrary slash name, because a word forwarded raw loses the turn's context.
ACP_BACKENDS_HARNESS_OWNED_SLASH_COMMANDS = frozenset({ACP_BACKEND_CLAUDE})

# Backends where the dashboard answers ``/clear`` itself, by dropping the
# conversation the way ``reset_conversation`` does, instead of sending it on.
#
# A non-kiro harness has no ``_kiro.dev/commands/execute``, so ``stream_command``
# hands it ``/clear`` through ``session/prompt``. A member is a harness whose own
# prompt handler has no ``clear`` command: the text reaches the model as a user
# message, the model may answer "Context cleared", and the context stays. claude
# is the one member for a different reason, given at its entry below.
#
# - kas: driven live, context usage read 8.42% -> 8.44% across ``/clear``.
# - codex: codex-acp's ``tryHandleCommand`` knows plan, mcp, skills, status,
#   review, review-branch, review-commit, compact, goal, rename, logout; any other
#   name returns ``handled: false`` and the prompt goes to the model. The 1.11.0
#   capture's ``available_commands_update`` lists no ``clear``.
# - opencode: its ACP prompt step special-cases only ``compact``; any other slash
#   name resolves against the configured command catalog (``init``, ``review``,
#   ``customize-opencode`` on the 1.18.30 capture) or is sent as text.
# - pi: pi-acp's built-ins are compact, autocompact, export, session, name,
#   steering, follow-up, changelog; anything else goes to ``session.prompt``.
# - deepseek: the ``dsh-acp`` bridge admits every prompt block as a user message
#   with no slash parsing at all.
# - copilot: Copilot CLI 1.0.100's ``available_commands_update`` lists no ``clear``,
#   and its ACP reference says a command it does not list is sent to the model as
#   an ordinary prompt. ``clear`` is also outside its prompt-command allowlist, so a
#   ``/clear`` that reached the client would be refused rather than sent.
#
# goose is NOT a member: its prompt handler runs ``/clear`` itself
# (``CompactionOperation.run_command`` returns an empty conversation), so it keeps
# the prompt path. kiro is not a member: it runs ``/clear`` natively over
# ``_kiro.dev/commands/execute``. A harness added later stays out until its
# ``/clear`` is checked.
ACP_BACKENDS_LOCAL_CLEAR = frozenset(
    {
        ACP_BACKEND_KAS,
        # claude-agent-acp sends no clear status frame and has no
        # ``_kiro.dev/commands/execute``, so Crew cannot see whether the Agent SDK
        # ran a ``/clear`` it was handed as prompt text, and Crew's history, todo
        # pill and next-turn replay would not follow it either way. claude runs
        # one adapter per session, so the dashboard's discard drops that adapter's
        # conversation whatever the SDK would have done with the text.
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_COPILOT,
        # strands sends every prompt to its model as text in project mode, ``/clear``
        # included, so the dashboard answers it.
        ACP_BACKEND_STRANDS,
    }
)

# Backends where the dashboard answers every harness command itself, because the
# harness has no way to run one. A member's harness command never reaches it:
# ``/clear`` is dropped the way ``reset_conversation`` does, any other command
# gets a notice, and ``GET /api/slash-commands`` lists only the commands the
# dashboard serves without the harness (``/agent <name>``, ``/clear``, ``/goal``,
# ``/prompts``, ``/side``, ``/workflow``), plus ``/compact``, which has a gate of
# its own.
#
# - kas: refuses ``_kiro.dev/commands/execute`` with -32603 for every command
#   (``test/fixtures/acp_frames/kas/commands-refused-live.jsonl``), and its
#   ``available_commands_update`` lists only custom agents and skills, so a
#   ``/help`` or ``/context`` sent as prompt text reaches the model as a message.
# - strands: the Strands CLI's project-mode ACP serves initialize, session/new,
#   session/load, authenticate, session/prompt and session/cancel, and nothing
#   else. It sends no ``available_commands_update`` and its prompt handler parses
#   no slash at all, so every command (``/clear``, ``/help``, ``/context``) reaches
#   the model as text (Strands CLI 0.2.3, ``tui/project/acp.js``).
#
# kiro is not a member: it runs its commands over ``_kiro.dev/commands/execute``.
# claude is not a member: its harness owns its own command set. codex, opencode,
# pi, deepseek, goose and copilot are not members: each serves the commands it
# knows from its own prompt handler. Codex's commands are checked, so the dashboard
# answers its other commands too (``ACP_BACKEND_PROMPT_HANDLER_COMMANDS``). The
# others' commands keep the prompt path (``ACP_BACKENDS_LOCAL_CLEAR`` covers the
# ``/clear`` most of them lack). A harness added later stays out until its command
# handling is checked.
ACP_BACKENDS_LOCAL_HARNESS_COMMANDS = frozenset({ACP_BACKEND_KAS, ACP_BACKEND_STRANDS})

# Harness id -> the harness commands (``_SLASH_COMMANDS`` names) that the
# harness's own prompt handler runs. The dashboard answers every other harness
# command of a member itself, as it does for ``ACP_BACKENDS_LOCAL_HARNESS_COMMANDS``:
# ``/clear`` is dropped the way ``reset_conversation`` does, and any other command
# gets a notice instead of reaching the model as text. ``GET /api/slash-commands``
# lists only the row's commands and the commands the dashboard serves without the
# harness.
#
# - codex: codex-acp 2.3.0's ``classifyPrompt`` runs plan, mcp, skills, status,
#   review, review-branch, review-commit, compact, goal, rename and logout, and
#   sends any other name to the model. Of Crew's harness commands only ``/compact``
#   and ``/mcp`` are in that list, so a ``/help``, ``/context`` or ``/tools`` sent
#   as prompt text reached the model as a bare word. The row does not forward
#   Codex's other built-ins: they are not Crew harness commands, so they stay text.
#
# kiro-cli has no row, because it runs every command over its own command channel.
# claude has no row, because it owns its command set
# (``ACP_BACKENDS_HARNESS_OWNED_SLASH_COMMANDS``). opencode, pi, deepseek, goose and
# copilot have no row: each keeps the prompt path until its own command handling is
# checked. A member of ``ACP_BACKENDS_LOCAL_HARNESS_COMMANDS`` has no row, because it
# runs no command.
ACP_BACKEND_PROMPT_HANDLER_COMMANDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {ACP_BACKEND_CODEX: frozenset({"/compact", "/mcp"})}
)

# Backends whose model-side REFUSAL arrives with a structured reason, not just a
# stop reason. When the Kiro service's content filter declines a turn, kiro-cli
# (and KAS, its relay) emit a ``_kiro.dev/metadata`` notification carrying
# ``stopReason: CONTENT_FILTERED`` plus a ``refusal`` object -- ``category``
# (``CYBER``, ...), the service's canned ``explanation``, and an optional
# ``recommendedModel`` -- milliseconds BEFORE the turn's terminal frame. Nothing
# in the terminal itself says why the turn stopped: the canned explanation is
# streamed as ordinary assistant text, and the ``session/prompt`` result may
# read ``end_turn`` or come back as a bare ``-32603 Internal error``.
#
# Membership decides whether :func:`kiro_crew.acp._dispatch.parse_refusal` is
# consulted on that notification. Every harness still lands on the SAME
# :class:`kiro_crew.acp.types.RefusalInfo` -- claude-agent-acp only reports
# Anthropic's ``stopReason: "refusal"`` with no reason attached, codex-acp
# likewise -- so a non-member is not "unsupported": its refusal card simply has
# no category line. The set exists so a future harness that carries its own
# reason payload is added HERE with a parser, rather than by widening the
# metadata reader to guess at every notification's shape.
# opencode is not a member: it carries no reason payload of its own, so its
# refusal card has no category line.
# pi is not a member: same.
# deepseek is not a member: it maps a turn ending onto a plain ACP ``stopReason`` and
# keeps provider-specific detail off the wire, so its refusal card has no category
# line either.
ACP_BACKENDS_STRUCTURED_REFUSAL = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends that report a TERMINAL provider failure of a prompt turn as an
# ``agent_message_chunk`` with no ``messageId``, and then answer the prompt with
# ``stopReason: "end_turn"`` as though the turn had succeeded.
#
# codex-acp is the member, on every release read so far (1.11.0, 2.0.1, 2.1.1,
# 2.1.2-preview.5). Its ``CodexEventHandler.createErrorEvent`` takes this path for
# a client that did not declare JetBrains AIR's ``sessionFailure`` capability: a
# native ``error`` notification for the current turn with ``willRetry: false``
# becomes ``createAgentTextMessageChunk(message + "\n\n")``, and nothing on the
# prompt response says the turn failed. Crew cannot take the typed path instead:
# the same ``_meta.jetbrains.air`` declaration flips the adapter into AIR
# rendering for every tool call, plan and permission request.
#
# What makes the chunk recognisable is the missing ``messageId``. Model text
# always names its item (``createTextEvent`` passes the native item id), and the
# chunks the adapter writes itself never do. Membership lets the session hold such
# a chunk until the turn ends and, when the turn ended on it, turn it into the
# turn's failure (``AcpSessionHandle._run_turn``). It is read only there.
# kiro-cli is not a member: a provider failure ends its prompt with a JSON-RPC
# error, and model text there carries no ``messageId``, so the missing id would
# say nothing.
# kas is not a member: a provider failure ends its prompt with a JSON-RPC error.
# claude is not a member: claude-agent-acp ends a failed turn with a JSON-RPC
# error rather than with text.
# opencode, goose, pi and deepseek are not members: none of them was read writing a
# provider failure as message text.
ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR = frozenset({ACP_BACKEND_CODEX})

# Each lands its turns' billing or token counts in TurnUsage (agent-host-contract.md §6), so a
# turn reporting none billed none. §6 establishes no count for codex or pi; deepseek reports occupancy.
# Live captures, one per member: kiro-cli 2.32.0 bills each turn as a ``_kiro.dev/metadata``
# meteringUsage unit=credit entry (acp_frames/kiro/compaction-live.jsonl); KAS 0.66.70 as a
# turn_completion promptTurnSummaries unit=credit entry (acp_frames/kas/turn-usage-live.jsonl);
# claude, opencode and goose as token counts on the ``session/prompt`` result's ``usage``
# (acp_frames/claude/session.jsonl, acp_frames/opencode/session-live.jsonl,
# acp_frames/goose/turn-live.jsonl).
ACP_BACKENDS_REPORT_USAGE = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_KAS,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_GOOSE,
    }
)

# Backends whose AcpClient reads stdout between turns, so a turn the agent starts
# on its own (a background task finished) is seen when it happens rather than by
# the next prompt. A member must stream such a turn while no prompt is in flight
# AND mark where it ends: claude-agent-acp puts ``_meta["_claude/origin"]`` on the
# cycle's closing ``usage_update``. A harness without that marker would hold its
# slot until the stale-turn gate or the prompt timeout, so it stays out until its
# end signal is known.
# See docs/request-for-change/rfc-agent-started-turns.md.
ACP_BACKENDS_AGENT_STARTED_TURNS = frozenset({ACP_BACKEND_CLAUDE})
