from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from io import StringIO

import kiro_byo.turn_runner as turn_mod
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.capability_seams import (
    DefaultModelRouter,
    EventSessionProjection,
    RuntimeSeams,
)
from kiro_byo.config import ModelSpec, ProviderSpec
from kiro_byo.providers.base import ChatTurn
from kiro_byo.session_events import append_message


MODEL = ModelSpec("p", "P", "m", "M", tool_use=True)
PROVIDER = ProviderSpec("p", "P", "openai-compatible", "https://example.invalid/v1", "", (MODEL,))


class CountingProviderInvoker:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = 0

    def invoke(self, session, **kwargs):
        self.calls += 1
        return self.delegate.invoke(session, **kwargs)


def test_default_runtime_exposes_narrow_capability_seams() -> None:
    server = AcpServer(stdin=StringIO(), stdout=StringIO())
    try:
        seams = server._runtime_seams()
        assert isinstance(seams, RuntimeSeams)
        assert isinstance(seams.model_router, DefaultModelRouter)
        assert isinstance(seams.session_projection, EventSessionProjection)
        assert callable(seams.provider_invoker.invoke)
        assert callable(seams.tool_executor.execute)
        assert callable(seams.post_stage_runner.run)
        assert callable(seams.reasoning_resolver.resolve)
    finally:
        server._executor.shutdown(wait=True)


def test_session_projection_reads_canonical_events_not_mutable_cache() -> None:
    session = Session("s", "", "M")
    append_message(session, {"role": "user", "content": "canonical"})
    # Deliberately poison the convenience projection. The seam must reconstruct
    # from durable events rather than treat this mutable cache as truth.
    session.messages.append({"role": "assistant", "content": "cache-only"})
    snapshot = EventSessionProjection().snapshot(session)
    assert snapshot.messages == [{"role": "user", "content": "canonical"}]


def test_turn_engine_invokes_provider_through_replaceable_seam() -> None:
    tmp = tempfile.TemporaryDirectory()
    old_home = os.environ.get("KIRO_BYO_HOME")
    os.environ["KIRO_BYO_HOME"] = tmp.name
    original_turn = turn_mod.chat_completion_turn
    server = AcpServer(stdin=StringIO(), stdout=StringIO())
    try:
        turn_mod.chat_completion_turn = lambda *args, **kwargs: ChatTurn(text="done")
        session = Session("s", tmp.name, "M")
        server._put_session(session)
        append_message(session, {"role": "user", "content": "hello"})

        base = server._runtime_seams()
        counting = CountingProviderInvoker(base.provider_invoker)
        server._kiro_runtime_seams = replace(base, provider_invoker=counting)
        server._run_provider_tool_loop(
            1, "s", session, PROVIDER, MODEL, 0, 0.0, 0.0, [], None,
        )
        assert counting.calls == 1
        assert session.messages[-1]["content"] == "done"
    finally:
        turn_mod.chat_completion_turn = original_turn
        server._executor.shutdown(wait=True)
        if old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = old_home
        tmp.cleanup()
