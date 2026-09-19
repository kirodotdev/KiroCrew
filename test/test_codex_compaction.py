"""Codex compaction: the manual ``/compact`` and Crew's own threshold compaction.

The defect these pin: a codex session got NO compaction at all. codex sat outside
``ACP_BACKENDS_COMPACT``, and that one membership answers both entry points --
the manual command through ``manual_compact_unsupported_backend``, and Crew's
threshold-driven compaction through ``session_compaction._compact_unsupported_backend``
-- so the automatic path declined with ``compact_unsupported`` and the context grew
until the session was recycled. The refusal text a user saw says the backend
"manages compaction automatically", which was a claim about codex nothing had
checked.

What a capture off codex-acp 1.11.0 (driven over stdio, one adapter, one session)
establishes, and what each layer below is tested against:

1. ``compact`` IS advertised. The ``available_commands_update`` carries it with the
   description "Summarize conversation to avoid hitting the context limit".
2. ``/compact`` completes INSIDE the ``session/prompt`` turn. The adapter answers
   the prompt only after the compaction is done, so the turn's terminal is the done
   signal and there is no asynchronous status to await -- codex joins claude in
   ``ACP_BACKENDS_INLINE_COMPACTION``.
3. The compaction is reported as a MARKED ``tool_call`` pair rather than as a
   notification or as prose. ``_meta.contextCompaction`` is the adapter's own
   marker, which makes the translation a structural match instead of the guess
   about text the claude path has to make.
4. There is no ``failed`` status. A compaction that errors emits an
   ``agent_message_chunk`` and then never resolves, leaving the ``session/prompt``
   request unanswered -- so nothing here synthesizes a failure.
5. codex's NATIVE auto-compaction is real but conditional: with
   ``model_auto_compact_token_limit`` configured it fires on its own and emits the
   same marked pair, and with the limit absent a session held at 50k tokens across
   three turns compacted not once. That is why Crew's own threshold compaction is
   turned on for codex rather than delegated to the harness.

The frame dicts below are the captured shapes, copied from the wire.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import textwrap
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
)
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging.commands import compact_unsupported_backend
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session import SessionManager
from kiro_crew.session_compaction import CompactionCoordinator, _compact_unsupported_backend


def parse_codex_compaction_update(update: dict[str, Any]) -> str | None:
    """Resolve the translation off the module at CALL time.

    Named through ``getattr`` rather than imported at module scope so a tree that
    does not have it yet fails these tests with the property that is missing,
    instead of failing collection for the whole file -- an import error proves a
    symbol is absent, not that the behaviour is wrong.
    """
    func = getattr(_dispatch, "parse_codex_compaction_update", None)
    assert func is not None, (
        "acp/_dispatch.py exposes no parse_codex_compaction_update, so a codex "
        "compaction frame reaches no consumer as a compaction status"
    )
    return func(update)


# --------------------------------------------------------------------------- #
# The captured frames
# --------------------------------------------------------------------------- #

#: The first frame of a compaction, manual or automatic alike.
CAPTURED_START: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "01a0b733-496a-7571-82db-9d2d2aa7e3a5",
    "kind": "think",
    "title": "Compact conversation",
    "status": "in_progress",
    "_meta": {"contextCompaction": {"version": 1}},
}

#: The terminal, which arrives BEFORE the ``session/prompt`` response.
CAPTURED_DONE: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "01a0b733-496a-7571-82db-9d2d2aa7e3a5",
    "title": "Compact conversation",
    "status": "completed",
    "_meta": {"contextCompaction": {"version": 1}},
}

#: What a past compaction looks like when a LOADED session's history is replayed:
#: a ``tool_call`` that is already ``completed``. Not a terminal for this turn.
CAPTURED_HISTORY_REPLAY: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "01a0b733-496a-7571-82db-9d2d2aa7e3a5",
    "kind": "think",
    "title": "Compact conversation",
    "status": "completed",
    "_meta": {"contextCompaction": {"version": 1}},
}


def _update_msg(update: dict[str, Any]) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={"sessionId": "s-1", "update": update},
    )


# --------------------------------------------------------------------------- #
# 1. Membership
# --------------------------------------------------------------------------- #


class TestMembership:
    def test_codex_can_serve_a_manual_compact(self) -> None:
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_COMPACT

    def test_codex_compacts_inside_the_prompt_turn(self) -> None:
        """Awaiting an inline backend strands the waiter for its whole timeout."""
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_INLINE_COMPACTION

    def test_the_inline_set_stays_a_subset(self) -> None:
        """A harness cannot compact inline without compacting at all."""
        assert ACP_BACKENDS_INLINE_COMPACTION <= ACP_BACKENDS_COMPACT
        assert ACP_BACKENDS_COMPACT <= ACP_BACKENDS_KNOWN

    def test_the_capability_card_reports_it(self) -> None:
        assert capabilities_for(ACP_BACKEND_CODEX).compacts_inline is True

    def test_no_other_harness_was_granted_anything(self) -> None:
        """One membership edit, not a widening: KAS in particular stays out."""
        assert ACP_BACKEND_KAS not in ACP_BACKENDS_COMPACT
        assert ACP_BACKENDS_COMPACT == frozenset(
            {ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX}
        )
        assert ACP_BACKENDS_INLINE_COMPACTION == frozenset({ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX})


# --------------------------------------------------------------------------- #
# 2. The frame translation
# --------------------------------------------------------------------------- #


class TestParseCodexCompactionUpdate:
    def test_the_captured_pair_classifies(self) -> None:
        assert parse_codex_compaction_update(CAPTURED_START) == "started"
        assert parse_codex_compaction_update(CAPTURED_DONE) == "completed"

    def test_started_and_completed_are_distinguishable(self) -> None:
        """Collapsing them would leave a compacting indicator that never closes."""
        assert parse_codex_compaction_update(CAPTURED_START) != parse_codex_compaction_update(
            CAPTURED_DONE
        )

    def test_a_replayed_history_compaction_is_not_a_terminal(self) -> None:
        """Reading it as one resets the meter against a window nobody summarized."""
        assert parse_codex_compaction_update(CAPTURED_HISTORY_REPLAY) is None

    def test_the_marker_is_required(self) -> None:
        """Matched on ``_meta.contextCompaction``, never on the title -- the title
        is display text the adapter is free to reword or localize."""
        unmarked = {k: v for k, v in CAPTURED_START.items() if k != "_meta"}
        assert parse_codex_compaction_update(unmarked) is None
        other_marker = {**CAPTURED_START, "_meta": {"somethingElse": {"version": 1}}}
        assert parse_codex_compaction_update(other_marker) is None

    def test_a_reworded_title_still_classifies(self) -> None:
        """The other direction of the same rule."""
        reworded = {**CAPTURED_START, "title": "Konversation komprimieren"}
        assert parse_codex_compaction_update(reworded) == "started"

    @pytest.mark.parametrize(
        "update",
        [
            {"sessionUpdate": "agent_message_chunk", "content": {"text": "Compacting..."}},
            {**CAPTURED_START, "sessionUpdate": "usage_update"},
            {**CAPTURED_START, "status": "failed"},
            {**CAPTURED_DONE, "status": "in_progress"},
            {**CAPTURED_START, "_meta": "contextCompaction"},
            {},
        ],
    )
    def test_everything_else_is_an_ordinary_frame(self, update: dict[str, Any]) -> None:
        assert parse_codex_compaction_update(update) is None

    def test_no_failed_status_is_invented(self) -> None:
        """codex-acp emits no failure frame: a compaction that errors leaves the
        ``session/prompt`` request unanswered. Manufacturing ``failed`` from the
        error PROSE is the guess the marker exists to avoid."""
        source = inspect.getsource(_dispatch.parse_codex_compaction_update)
        assert '"failed"' not in source
        error_text = {
            "sessionUpdate": "agent_message_chunk",
            "content": {"text": "Error running remote compact task: stream disconnected"},
        }
        assert parse_codex_compaction_update(error_text) is None


# --------------------------------------------------------------------------- #
# 3. The client-side event
# --------------------------------------------------------------------------- #


def _bare_client(backend: str = ACP_BACKEND_CODEX) -> AcpClient:
    """An AcpClient carrying only the fields the compaction helper touches.

    Built without ``__init__`` because the real one spawns a backend process;
    ``_is_codex`` is a read-only property over ``backend``, so the FIELD it reads
    is set rather than the property shadowed.
    """
    client = AcpClient.__new__(AcpClient)
    client._acp_backend = backend
    client._codex_compaction_pending = False
    client._compaction_failed_at = None
    client.last_prompt_stats = AcpPromptStats()
    return client


class TestCodexCompactionEvent:
    def test_the_pair_becomes_the_shared_status_vocabulary(self) -> None:
        """Every consumer already handles these two events, which is why the
        messaging surfaces and the dashboard need no codex-specific arm."""
        client = _bare_client()
        started = client._codex_compaction_event(_update_msg(CAPTURED_START))
        assert started is not None
        assert (started.kind, started.text) == (EVENT_COMPACTION_STATUS, "started")
        assert client._codex_compaction_pending

        done = client._codex_compaction_event(_update_msg(CAPTURED_DONE))
        assert done is not None
        assert (done.kind, done.text) == (EVENT_COMPACTION_STATUS, "completed")
        assert not client._codex_compaction_pending

    def test_the_terminal_drops_the_stale_context_counts(self) -> None:
        """Without this the meter keeps reporting the pre-compaction window."""
        client = _bare_client()
        client._codex_compaction_event(_update_msg(CAPTURED_START))
        client._codex_compaction_event(_update_msg(CAPTURED_DONE))
        assert client.last_prompt_stats.context_pct_unknown

    def test_a_terminal_without_a_started_is_ignored(self) -> None:
        """A loaded session replays past compactions; only a terminal following a
        ``started`` seen in THIS turn describes work this turn did."""
        client = _bare_client()
        assert client._codex_compaction_event(_update_msg(CAPTURED_DONE)) is None
        assert not client.last_prompt_stats.context_pct_unknown

    def test_a_replayed_history_frame_emits_nothing(self) -> None:
        client = _bare_client()
        assert client._codex_compaction_event(_update_msg(CAPTURED_HISTORY_REPLAY)) is None
        assert not client._codex_compaction_pending

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    def test_a_non_inline_harness_frame_is_never_reinterpreted(self, backend: str) -> None:
        """A harness outside ``ACP_BACKENDS_INLINE_COMPACTION`` is never asked.

        kiro-cli and KAS report compaction out of band, and reading a frame of
        theirs here would double their own status.
        """
        client = _bare_client(backend)
        assert client._codex_compaction_event(_update_msg(CAPTURED_START)) is None

    def test_claude_is_asked_and_its_real_frames_decline(self) -> None:
        """claude IS in the set, and that costs nothing, because the MARKER decides.

        The set bounds who is asked; ``_meta.contextCompaction`` is what answers.
        claude reports compaction as prose and stamps no marker, so every frame it
        really sends declines here -- which is why the set is a safe gate to hand
        the next inline harness.
        """
        client = _bare_client(ACP_BACKEND_CLAUDE)
        for update in (
            {"sessionUpdate": "agent_message_chunk", "content": {"text": "Compacting..."}},
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"text": "\n\nCompacting completed."},
            },
        ):
            assert client._codex_compaction_event(_update_msg(update)) is None

    @pytest.mark.parametrize("params", [None, {}, {"update": "not-a-dict"}])
    def test_a_malformed_frame_is_not_a_compaction(self, params: Any) -> None:
        client = _bare_client()
        msg = JsonRpcMessage(method="session/update", params=params)
        assert client._codex_compaction_event(msg) is None

    def test_no_failure_budget_is_armed_on_a_guess(self) -> None:
        """The post-failure budget exists for a harness that says it failed. codex
        never does, so nothing here may arm it."""
        client = _bare_client()
        client._codex_compaction_event(_update_msg(CAPTURED_START))
        client._codex_compaction_event(_update_msg(CAPTURED_DONE))
        assert client._compaction_failed_at is None


class TestEveryPromptLoopAppliesIt:
    """All three dispatch loops must apply the translation.

    Two of them cannot YIELD the event -- they return ``str`` -- but the context
    counters it drops are what the meter reads on the next turn, so a loop that
    skipped the call would leave one prompt API reporting a compacted session at
    its pre-compaction size.
    """

    @pytest.mark.parametrize(
        "func",
        [
            AcpClient._dispatch_events,
            AcpClient.send_message_stream,
            AcpClient._read_prompt_response,
        ],
    )
    def test_the_loop_calls_the_translation(self, func: Any) -> None:
        assert "_codex_compaction_event" in inspect.getsource(func)


# --------------------------------------------------------------------------- #
# 4. Both entry points stop refusing
# --------------------------------------------------------------------------- #


class TestTheManualCommandIsOffered:
    @pytest.mark.parametrize("impl", [AcpProvider, AcpSessionProvider])
    def test_both_acp_implementations_pass_codex(self, impl: Any) -> None:
        """Answered from set membership on both provider shapes (harness-parity H6)."""
        assert (
            impl.manual_compact_unsupported_backend.fget(_stub_provider(impl, ACP_BACKEND_CODEX))
            is None
        )

    def test_a_real_codex_provider_names_nothing(self) -> None:
        assert AcpProvider(acp_backend=ACP_BACKEND_CODEX).manual_compact_unsupported_backend is None

    def test_the_channel_gate_passes_codex(self) -> None:
        """The messaging surfaces read the same capability, so Slack, Telegram and
        the rest dispatch the command instead of answering with the refusal."""
        provider = AcpProvider(acp_backend=ACP_BACKEND_CODEX)
        assert compact_unsupported_backend(provider) is None

    def test_a_non_member_still_refuses(self) -> None:
        """The gate is not disabled, only widened."""
        provider = AcpProvider(acp_backend=ACP_BACKEND_KAS)
        assert compact_unsupported_backend(provider) == ACP_BACKEND_KAS


class TestCrewsOwnThresholdCompactionRuns:
    def test_the_autocompact_gate_no_longer_declines_codex(self) -> None:
        """The same capability read from the compaction gate ladder. A positively
        named backend declines with ``compact_unsupported`` BEFORE the compaction
        task is scheduled, which is how a codex session grew unbounded."""
        provider = AcpProvider(acp_backend=ACP_BACKEND_CODEX)
        assert _compact_unsupported_backend(provider) is None

    def test_the_gate_still_declines_a_non_member(self) -> None:
        provider = AcpProvider(acp_backend=ACP_BACKEND_KAS)
        assert _compact_unsupported_backend(provider) == ACP_BACKEND_KAS


# --------------------------------------------------------------------------- #
# 4b. A compaction that never reports
# --------------------------------------------------------------------------- #


class TestADanglingStartedIsSettled:
    """The failure path, which sends no frame at all.

    The capture's third run is the evidence: a compaction that errors emits the
    ``started`` frame, then an ``agent_message_chunk`` reading "Error running
    remote compact task: ...", and then the adapter's ``runCompact`` never
    resolves -- the ``session/prompt`` request went unanswered for 240s. So there
    is no terminal to translate, and a consumer that entered a compacting state on
    the ``started`` would never leave it.
    """

    @pytest.mark.parametrize("impl", ["client", "handle"])
    def test_a_started_with_no_terminal_settles_as_failed(self, impl: str) -> None:
        """Both transports, because the hole is in both."""
        subject = _bare_client() if impl == "client" else _bare_handle()
        started = (
            subject._codex_compaction_event(_update_msg(CAPTURED_START))
            if impl == "client"
            else subject._codex_compaction_event(CAPTURED_START)
        )
        assert started is not None and started.text == "started"

        settled = subject._settle_codex_compaction(STOP_REASON_END_TURN)
        assert settled is not None
        assert (settled.kind, settled.text) == (EVENT_COMPACTION_STATUS, "failed")
        assert settled.synthesized is True
        assert settled.title, "a reason, so a surface stops rendering 'unknown error'"
        assert not subject._codex_compaction_pending

    @pytest.mark.parametrize("impl", ["client", "handle"])
    def test_failed_is_reported_not_completed(self, impl: str) -> None:
        """The opposite verdict to the claude twin, and the reason is the capture.

        codex sends its terminal for a manual ``/compact`` AND for a native
        ``model_auto_compact_token_limit`` compaction, so a missing terminal is
        evidence the compaction did NOT finish. Synthesizing ``completed`` here
        would reset the context meter against a window nobody summarized.
        """
        subject = _bare_client() if impl == "client" else _bare_handle()
        if impl == "client":
            subject._codex_compaction_event(_update_msg(CAPTURED_START))
        else:
            subject._codex_compaction_event(CAPTURED_START)
        settled = subject._settle_codex_compaction(STOP_REASON_END_TURN)
        assert settled is not None and settled.text != "completed"
        assert not subject.last_prompt_stats.context_pct_unknown

    @pytest.mark.parametrize("impl", ["client", "handle"])
    def test_it_is_a_no_op_when_nothing_was_compacting(self, impl: str) -> None:
        """Runs at EVERY turn terminal, so the common case must emit nothing."""
        subject = _bare_client() if impl == "client" else _bare_handle()
        assert subject._settle_codex_compaction(STOP_REASON_END_TURN) is None

    @pytest.mark.parametrize("impl", ["client", "handle"])
    def test_a_reported_terminal_disarms_it(self, impl: str) -> None:
        """The normal path must not get a second, contradictory terminal."""
        subject = _bare_client() if impl == "client" else _bare_handle()
        if impl == "client":
            subject._codex_compaction_event(_update_msg(CAPTURED_START))
            subject._codex_compaction_event(_update_msg(CAPTURED_DONE))
        else:
            subject._codex_compaction_event(CAPTURED_START)
            subject._codex_compaction_event(CAPTURED_DONE)
        assert subject._settle_codex_compaction(STOP_REASON_END_TURN) is None

    @pytest.mark.parametrize("reason", [STOP_REASON_END_TURN, STOP_REASON_CANCELLED, "", "refusal"])
    def test_every_turn_ending_settles_it(self, reason: str) -> None:
        """One arm for every reason, unlike the claude twin.

        This is not an inference from HOW the turn ended -- it is the absence of a
        frame codex always sends on success. A turn cancelled mid-compaction did
        not compact either, and leaving the flag armed would carry it into the
        next turn.
        """
        handle = _bare_handle()
        handle._codex_compaction_event(CAPTURED_START)
        settled = handle._settle_codex_compaction(reason)
        assert settled is not None and settled.text == "failed"
        assert not handle._codex_compaction_pending

    @pytest.mark.parametrize("impl", ["client", "handle"])
    def test_no_post_failure_budget_is_armed(self, impl: str) -> None:
        """That budget bounds a wait for a turn that may never END. This runs AT
        the end of the turn, so arming it would charge the NEXT turn's idle clock
        for this one's failure."""
        subject = _bare_client() if impl == "client" else _bare_handle()
        if impl == "client":
            subject._codex_compaction_event(_update_msg(CAPTURED_START))
        else:
            subject._codex_compaction_event(CAPTURED_START)
        subject._settle_codex_compaction(STOP_REASON_END_TURN)
        assert subject._compaction_failed_at is None

    @pytest.mark.parametrize("impl", ["client", "handle"])
    def test_the_failure_is_not_advertised_as_retryable(self, impl: str) -> None:
        """An inferred failure carries no reason to classify, so it must not
        promise a user that trying again will help."""
        subject = _bare_client() if impl == "client" else _bare_handle()
        subject.last_compaction_transient = True
        if impl == "client":
            subject._codex_compaction_event(_update_msg(CAPTURED_START))
        else:
            subject._codex_compaction_event(CAPTURED_START)
        subject._settle_codex_compaction(STOP_REASON_END_TURN)
        assert subject.last_compaction_transient is False

    def test_the_settle_runs_at_the_one_choke_point_every_terminal_uses(self) -> None:
        """``_dispatch_events`` yields ``EVENT_COMPLETE`` from several places,
        including its own timeout arm. ``_run_turn`` is the single ``async for``
        they all funnel through -- the same reason the park accounting is measured
        there -- so settling per producer would be several edits with one missed.
        """
        source = inspect.getsource(AcpSessionHandle._run_turn)
        assert "_settle_codex_compaction" in source
        dispatch = inspect.getsource(AcpSessionHandle._dispatch_events)
        assert "_settle_codex_compaction" not in dispatch

    def test_the_per_turn_reset_stops_it_leaking_into_the_next_turn(self) -> None:
        """A consumer that walks away mid-turn reaches no terminal at all, so the
        flag is also cleared where the turn's other per-turn state is."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(AcpSessionHandle._dispatch_events)))
        cleared = {
            target.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
        }
        assert "_codex_compaction_pending" in cleared


# --------------------------------------------------------------------------- #
# 5. The LIVE transport
# --------------------------------------------------------------------------- #


def _bare_handle(backend: str = ACP_BACKEND_CODEX) -> Any:
    """An ``AcpSessionHandle`` carrying only what the compaction helper reads.

    Built without ``__init__`` for the same reason the client helper is: the real
    one needs a runtime with a live process. The backend is read off the RUNTIME
    (``self._runtime.acp_backend``), which is the seam the method actually uses.
    """
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._runtime = SimpleNamespace(acp_backend=backend)
    handle._session_id = "s-1"
    handle._codex_compaction_pending = False
    handle._compaction_failed_at = None
    handle.last_prompt_stats = AcpPromptStats()
    return handle


def _live_handle(backend: str) -> Any:
    """A REAL ``AcpSessionHandle`` over a real ``AcpRuntime``, with no process.

    The runtime is never started, so nothing is spawned -- but the handle is the
    genuine object with its genuine caches, which is what lets the frame handler
    run for real instead of through a stub.
    """
    runtime = AcpRuntime(work_dir="/tmp", acp_backend=backend)
    queue: asyncio.Queue = asyncio.Queue()
    runtime._session_queues["s-1"] = queue
    return AcpSessionHandle("s-1", queue, runtime)


class TestTheRuntimeTransportTranslatesToo:
    """codex is served by ``AcpRuntime``, so this is the implementation that runs.

    A fix wired only into ``AcpClient`` would leave every real codex session
    exactly as broken as before, which is why the transport is asserted by name.
    """

    def test_codex_is_served_by_the_shared_runtime(self) -> None:
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_ACP_RUNTIME

    def test_the_pair_becomes_the_shared_status_vocabulary(self) -> None:
        handle = _bare_handle()
        started = handle._codex_compaction_event(CAPTURED_START)
        assert started is not None
        assert (started.kind, started.text) == (EVENT_COMPACTION_STATUS, "started")
        done = handle._codex_compaction_event(CAPTURED_DONE)
        assert done is not None
        assert (done.kind, done.text) == (EVENT_COMPACTION_STATUS, "completed")
        assert handle.last_prompt_stats.context_pct_unknown

    def test_a_terminal_without_a_started_is_ignored(self) -> None:
        handle = _bare_handle()
        assert handle._codex_compaction_event(CAPTURED_DONE) is None
        assert not handle.last_prompt_stats.context_pct_unknown

    def test_a_replayed_history_frame_emits_nothing(self) -> None:
        handle = _bare_handle()
        assert handle._codex_compaction_event(CAPTURED_HISTORY_REPLAY) is None

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    def test_a_non_inline_harness_frame_is_never_reinterpreted(self, backend: str) -> None:
        assert _bare_handle(backend)._codex_compaction_event(CAPTURED_START) is None

    def test_the_gate_is_the_set_so_the_next_inline_harness_inherits_it(self) -> None:
        """Read the gate off the source: a membership test, not a codex identity.

        The sibling work adds opencode, pi and goose to
        ``ACP_BACKENDS_INLINE_COMPACTION``, and they must inherit this translation
        by joining the set rather than by editing this method. An
        ``== ACP_BACKEND_CODEX`` here would also fail the runtime path's own
        declaration ratchet.
        """
        source = inspect.getsource(AcpSessionHandle._codex_compaction_event)
        assert "ACP_BACKENDS_INLINE_COMPACTION" in source
        assert "ACP_BACKEND_CODEX" not in source

    def test_no_failure_budget_is_armed_on_a_guess(self) -> None:
        handle = _bare_handle()
        handle._codex_compaction_event(CAPTURED_START)
        handle._codex_compaction_event(CAPTURED_DONE)
        assert handle._compaction_failed_at is None

    @pytest.mark.asyncio
    async def test_a_real_handle_turns_the_captured_pair_into_both(self) -> None:
        """Drive the real frame handler with the captured frames.

        The compaction status rides ALONGSIDE the tool-call events the same frame
        produces. Swallowing the frame would delete the transcript row the user
        watched appear -- in order to report the very thing that row reports.
        """
        handle = _live_handle(ACP_BACKEND_CODEX)

        start_events = handle._handle_update(_update_msg(CAPTURED_START))
        kinds = [ev.kind for ev in start_events]
        assert EVENT_COMPACTION_STATUS in kinds
        assert EVENT_TOOL_CALL in kinds, "the tool-call row must survive"
        assert [ev.text for ev in start_events if ev.kind == EVENT_COMPACTION_STATUS] == ["started"]

        done_events = handle._handle_update(_update_msg(CAPTURED_DONE))
        assert [ev.text for ev in done_events if ev.kind == EVENT_COMPACTION_STATUS] == [
            "completed"
        ]
        assert any(ev.kind == EVENT_TOOL_CALL_UPDATE for ev in done_events)
        # What ``_compact_in_place`` and the dashboard both read next.
        assert handle.last_prompt_stats.context_pct_unknown

    @pytest.mark.asyncio
    async def test_a_real_kiro_handle_reads_the_same_frames_as_plain_tool_calls(self) -> None:
        """The other direction on the real handler: no other harness's frames are
        reinterpreted, so the translation cannot regress a kiro-cli session."""
        handle = _live_handle(ACP_BACKEND_KIRO)
        for update in (CAPTURED_START, CAPTURED_DONE):
            events = handle._handle_update(_update_msg(update))
            assert all(ev.kind != EVENT_COMPACTION_STATUS for ev in events)
        assert not handle.last_prompt_stats.context_pct_unknown

    def test_compact_captures_the_mid_turn_terminal(self) -> None:
        """``compact()`` drains its own prompt turn and caches a terminal it sees,
        which is what lets ``wait_for_compaction()`` answer immediately instead of
        waiting for a notification codex never sends."""
        source = inspect.getsource(AcpSessionHandle.compact)
        assert "EVENT_COMPACTION_STATUS" in source
        assert "_compact_result" in source


# --------------------------------------------------------------------------- #
# 6. The in-place autocompact arm
# --------------------------------------------------------------------------- #

_AUTOCOMPACT_KEY = "dashboard:codex-autocompact"


def _autocompact_provider_factory(backend: str, *, stream_status: str | None) -> Any:
    """A provider for *backend* whose ``/compact`` stream yields *stream_status*.

    ``capabilities`` is the REAL ``SessionCapabilities`` for the backend, because
    that is the object ``capabilities_of`` reads -- a duck-typed stand-in would
    answer ``UNKNOWN_BACKEND_CAPABILITIES`` and take the arm this pins.
    """

    def factory(session_key: Any = None, **kwargs: Any) -> Any:
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.has_active_turn = lambda: False
        m.capabilities = capabilities_for(backend)
        m.manual_compact_unsupported_backend = None
        state = {"compacted": False}
        m.context_usage_pct = lambda: 0.0 if state["compacted"] else 92.0
        m.context_usage_unknown = lambda: state["compacted"]

        async def _stream(_cmd: str) -> Any:
            if stream_status is not None:
                state["compacted"] = True
                yield AcpEvent(kind=EVENT_COMPACTION_STATUS, text=stream_status, title="")

        m.stream_command = MagicMock(side_effect=_stream)

        async def _wait(timeout: Any = None) -> dict[str, str]:
            state["compacted"] = True
            return {"type": "completed"}

        m.wait_for_compaction = AsyncMock(side_effect=_wait)
        return m

    return factory


@contextlib.asynccontextmanager
async def _managed_manager(factory: Any) -> Any:
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2
    mgr = SessionManager(cfg, provider_factory=factory)
    try:
        yield mgr
    finally:
        pending = [t for t in mgr._background_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await mgr.close_all()


class TestTheInPlaceArmStopsWaitingForAnInlineHarness:
    """Crew's own threshold compaction, at the rung that dispatches ``/compact``.

    A codex session reaches ``_compact_in_place``. Two things had to be true for
    it to work, and the second is what this class pins: the stream must carry a
    terminal, and when it does NOT, the path must not spend the whole result
    budget awaiting an asynchronous status an inline harness never sends -- while
    holding the session's turn semaphore, so every queued turn waits it out too.
    """

    @pytest.mark.asyncio
    async def test_an_inline_terminal_in_the_stream_settles_it(self) -> None:
        """The normal codex path: the translated terminal arrives inside the turn,
        so the asynchronous wait is never reached at all."""
        factory = _autocompact_provider_factory(ACP_BACKEND_CODEX, stream_status="completed")
        async with _managed_manager(factory) as mgr:
            provider, _, _ = await mgr.get_or_create(_AUTOCOMPACT_KEY)
            mgr.release(_AUTOCOMPACT_KEY)

            assert await mgr.compact_if_needed(_AUTOCOMPACT_KEY) in ("ok", "reset")

            provider.stream_command.assert_called_once_with("/compact")
            provider.wait_for_compaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_statusless_turn_is_not_awaited_for_an_inline_harness(self) -> None:
        """No terminal in the stream means no compaction happened.

        For an inline harness the turn's terminal WAS the whole answer, so there
        is no second result coming. The path recycles instead of waiting.
        """
        factory = _autocompact_provider_factory(ACP_BACKEND_CODEX, stream_status=None)
        async with _managed_manager(factory) as mgr:
            provider, _, _ = await mgr.get_or_create(_AUTOCOMPACT_KEY)
            mgr.release(_AUTOCOMPACT_KEY)

            assert await mgr.compact_if_needed(_AUTOCOMPACT_KEY) == "recycled"

            provider.wait_for_compaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_non_inline_harness_keeps_its_asynchronous_wait(self) -> None:
        """The other direction: kiro-cli's terminal genuinely arrives afterwards,
        so removing the wait for everyone would break the default harness."""
        factory = _autocompact_provider_factory(ACP_BACKEND_KIRO, stream_status=None)
        async with _managed_manager(factory) as mgr:
            provider, _, _ = await mgr.get_or_create(_AUTOCOMPACT_KEY)
            mgr.release(_AUTOCOMPACT_KEY)

            assert await mgr.compact_if_needed(_AUTOCOMPACT_KEY) in ("ok", "reset")

            provider.wait_for_compaction.assert_awaited()

    def test_the_arm_is_chosen_by_capability_not_by_backend_name(self) -> None:
        """The sibling work adds opencode, pi and goose to
        ``ACP_BACKENDS_INLINE_COMPACTION``, and they must inherit this arm by
        joining the set rather than by editing this method.

        Read off the parsed CODE, not the source text: a comment naming the
        capability would satisfy a substring check while the condition tested
        something else entirely, which makes the substring version pass on a tree
        where the gate has been deleted.
        """
        tree = ast.parse(
            textwrap.dedent(inspect.getsource(CompactionCoordinator._compact_in_place))
        )
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "capabilities_of" in called
        attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert "compacts_inline" in attrs
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert not any(
            n.startswith("ACP_BACKEND_") for n in names
        ), "the arm must not be selected by a harness identity"


def _stub_provider(impl: Any, backend: str) -> Any:
    """The minimum an ``impl``'s capability property reads: a backend id.

    Both properties resolve the backend off a different attribute, so each is
    given what its own source asks for rather than a shared duck type that would
    pass by accident on one of them.
    """

    class _Stub:
        pass

    stub = _Stub()
    source = inspect.getsource(impl.manual_compact_unsupported_backend.fget)
    if "self._client" in source:
        stub._client = type("_C", (), {"backend": backend})()  # type: ignore[attr-defined]
    if "self._handle" in source:
        stub._handle = type("_H", (), {"backend": backend})()  # type: ignore[attr-defined]
    if "self._backend" in source:
        stub._backend = backend  # type: ignore[attr-defined]
    if "self.backend" in source:
        stub.backend = backend  # type: ignore[attr-defined]
    return stub
