"""The real ``A2AProvider`` against the in-process fake A2A server.

Loopback only, no network beyond 127.0.0.1, no auth. These are the tests the
proof of concept ran by hand against a kiro-cli-wrapping shim; here they are
deterministic. Each test pins one clause of the module contract in
``docs/system-specs/modules/a2a-subagents.md``.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from kiro_crew.providers.a2a import A2AProvider, A2AStreamError
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.testing import fake_a2a_server

pytestmark = pytest.mark.asyncio


@contextlib.asynccontextmanager
async def _fake():
    """Serve the fake inside the test's own loop, stop it on exit.

    An async CONTEXT MANAGER rather than an ``@pytest_asyncio.fixture``: the
    suite pins pytest-asyncio 0.20.3, whose async-fixture wrapper reads the
    ``fixturedef.unittest`` attribute pytest 8.1 removed -- on CI every
    async-generator fixture errors at setup. The repo avoids the decorator by
    convention (see test_denied_commands_api.py's module docstring).
    """
    runner, card_url, server = await fake_a2a_server.serve(port=0)
    try:
        yield card_url, server
    finally:
        await runner.cleanup()


async def _drain(provider: A2AProvider, prompt: str) -> tuple[str, list]:
    text = ""
    events = []
    async for ev in provider.stream(prompt):
        events.append(ev)
        if ev.kind == EVENT_TEXT_CHUNK:
            text += ev.text or ""
    return text, events


async def _provider(card_url: str, context_id: str | None = None) -> A2AProvider:
    p = A2AProvider(name="remote-demo", agent_card_url=card_url, context_id=context_id)
    await p.start()
    return p


class TestConversationContinuity:
    async def test_first_turn_adopts_server_minted_ids(self):
        async with _fake() as (card_url, server):
            p = await _provider(card_url)
            try:
                text, events = await _drain(p, "Reply with only the word OSPREY-3")
                assert text == "OSPREY-3"
                assert events[-1].kind == EVENT_COMPLETE
                assert p.context_id and p.context_id in server.conversations
                # The wire carried no contextId on the first turn; the server minted it.
                first = server.requests[0]["params"]["message"]
                assert "contextId" not in first and "referenceTaskIds" not in first
            finally:
                await p.shutdown()

    async def test_continuation_carries_context_and_reference(self):
        """spawn_continue's shape: a NEW provider built from the stored contextId."""
        async with _fake() as (card_url, server):
            p1 = await _provider(card_url)
            text, _ = await _drain(p1, "Remember the codeword HERON-9. Reply only with OK.")
            assert text == "OK"
            ctx = p1.context_id
            await p1.shutdown()

            p2 = await _provider(card_url, context_id=ctx)
            try:
                text, _ = await _drain(p2, "What was the codeword? Reply with just the codeword.")
                assert text == "HERON-9"
                second = server.requests[-1]["params"]["message"]
                assert second["contextId"] == ctx
                assert len(server.conversations) == 1
            finally:
                await p2.shutdown()

    async def test_second_turn_same_provider_references_prior_task(self):
        async with _fake() as (card_url, server):
            p = await _provider(card_url)
            try:
                await _drain(p, "Remember the codeword TERN-2. Reply only with OK.")
                first_task = server.requests[-1]
                await _drain(p, "What was the codeword?")
                msg = server.requests[-1]["params"]["message"]
                assert msg["contextId"] == p.context_id
                # Anchored to the previous task in the same conversation.
                assert (
                    msg["referenceTaskIds"] == [list(server.tasks)[0]]
                    or len(msg["referenceTaskIds"]) == 1
                )
                assert first_task["method"] == "SendStreamingMessage"
            finally:
                await p.shutdown()

    async def test_fresh_context_does_not_recall(self):
        async with _fake() as (card_url, _):
            p1 = await _provider(card_url)
            await _drain(p1, "Remember the codeword GULL-5. Reply only with OK.")
            await p1.shutdown()
            p2 = await _provider(card_url)  # no context_id: a new conversation
            try:
                text, _ = await _drain(p2, "What was the codeword?")
                assert text == "I have no codeword for this conversation"
            finally:
                await p2.shutdown()


class TestFailureDiscipline:
    async def test_drop_mid_stream_raises_non_transient(self):
        """[[DROP]]: chunks, then the connection is cut with no terminal state."""
        async with _fake() as (card_url, _):
            p = await _provider(card_url)
            try:
                with pytest.raises(A2AStreamError) as ei:
                    await _drain(p, "[[DROP]] Write ten numbered facts about bridges.")
                assert ei.value.transient is False
                assert "partial output before failure" in str(ei.value)
            finally:
                await p.shutdown()

    async def test_clean_end_without_terminal_state_raises(self):
        """[[NEVER_TERMINAL]]: well-formed HTTP, broken protocol contract."""
        async with _fake() as (card_url, _):
            p = await _provider(card_url)
            try:
                with pytest.raises(A2AStreamError):
                    await _drain(p, "[[NEVER_TERMINAL]] anything")
            finally:
                await p.shutdown()

    async def test_failed_terminal_state_raises_with_the_remote_reason(self):
        """A FAILED terminal state is a failed run: raise, reason preserved, never complete."""
        async with _fake() as (card_url, _):
            p = await _provider(card_url)
            try:
                with pytest.raises(A2AStreamError) as excinfo:
                    await _drain(p, "[[FAIL]] anything")
                assert "task_state_failed" in str(excinfo.value)
                assert "[[FAIL]] requested" in str(excinfo.value)
                assert excinfo.value.transient is False
            finally:
                await p.shutdown()

    async def test_jsonrpc_error_frame_is_a_failed_run(self):
        async with _fake() as (card_url, _):
            p = await _provider(card_url)
            try:
                with pytest.raises(A2AStreamError) as excinfo:
                    await _drain(p, "[[ERROR]] anything")
                assert "[[ERROR]] requested" in str(excinfo.value)
            finally:
                await p.shutdown()


class TestSlowAndCancel:
    async def test_cancel_ends_a_slow_turn_as_cancelled(self, monkeypatch):
        monkeypatch.setattr(fake_a2a_server, "SLOW_INTERVAL", 0.2)
        async with _fake() as (card_url, server):
            p = await _provider(card_url)
            try:

                async def _consume():
                    text = ""
                    async for ev in p.stream("[[SLOW]] Write twenty numbered facts about rivers."):
                        if ev.kind == EVENT_TEXT_CHUNK:
                            text += ev.text or ""
                            if text.count("\n") >= 2:
                                outcome = await p.cancel()
                                assert outcome == "acked"
                    return text

                # A cancelled task is a FAILED turn (the runner would otherwise
                # record success on the completion event); the partial output
                # was streamed live and its size rides in the message.
                with pytest.raises(A2AStreamError, match="cancelled") as ei:
                    await asyncio.wait_for(_consume(), timeout=10)
                assert "partial output before failure" in str(ei.value)
                task = list(server.tasks.values())[-1]
                assert task.cancelled.is_set()
            finally:
                await p.shutdown()


class TestCard:
    async def test_card_declares_streaming_and_jsonrpc_endpoint(self):
        async with _fake() as (card_url, _):
            p = await _provider(card_url)
            try:
                assert p._agent_card is not None
                assert p._agent_card["capabilities"]["streaming"] is True
                assert p._message_endpoint.endswith("/")
            finally:
                await p.shutdown()

    async def test_canned_reply_shapes(self):
        conv = fake_a2a_server._Conversation(context_id="c")
        assert (
            fake_a2a_server.canned_reply("Reply with only the word FROM-REMOTE", conv)
            == "FROM-REMOTE"
        )
        assert (
            fake_a2a_server.canned_reply("Remember the codeword X-1. Reply only with OK.", conv)
            == "OK"
        )
        assert conv.codeword == "X-1"
        assert fake_a2a_server.canned_reply("What was the codeword?", conv) == "X-1"
        assert fake_a2a_server.canned_reply("[[SPAWN:remote-demo]] hello there", conv).endswith(
            "hello there"
        )


class TestBearerOnTheWire:
    """The credential actually reaches the server -- on the card fetch and the message."""

    async def test_bearer_configured_client_is_admitted(self, monkeypatch):
        from kiro_crew.agent_sdk.drivers import a2a as drv
        from kiro_crew.config.sections import A2aAgentConfig, A2aAuthConfig

        runner, card_url, server = await fake_a2a_server.serve(port=0, require_bearer="s3cret-1")
        try:
            # Token and its pinned origin both come from the environment (the
            # operator's side); config carries only the variable NAME.
            monkeypatch.setenv("KIROCREW_A2A_FAKE_CRED", "s3cret-1")
            monkeypatch.setenv("KIROCREW_A2A_FAKE_CRED_ORIGIN", drv.card_origin(card_url))
            entry = A2aAgentConfig(
                name="remote-demo",
                agent_card_url=card_url,
                auth=A2aAuthConfig(scheme="bearer", token_env="KIROCREW_A2A_FAKE_CRED"),
            )
            p = drv.create_a2a_provider(entry, context_id=None)
            await p.start()
            try:
                text, _ = await _drain(p, "Reply with only the word LAPWING-2")
                assert text == "LAPWING-2"
                assert server.unauthorized == 0
            finally:
                await p.shutdown()
        finally:
            await runner.cleanup()

    async def test_edition_registered_scheme_is_what_the_provider_sends(self):
        # The extension point end to end: a scheme registered by an edition,
        # selected by config, built through create_a2a_provider (the run path's
        # constructor), and its header accepted by a card that requires bearer.
        from kiro_crew.agent_sdk.drivers import a2a as drv
        from kiro_crew.config.sections import A2aAgentConfig, A2aAuthConfig

        runner, card_url, server = await fake_a2a_server.serve(
            port=0, require_bearer="from-the-edition"
        )
        origin = drv.card_origin(card_url)
        drv.register_auth_scheme(
            drv.A2aAuthScheme(
                "edition-sso",
                frozenset({"bearer"}),
                lambda auth: drv.ResolvedCredential(
                    lambda: {"Authorization": "Bearer from-the-edition"}, origin
                ),
            )
        )
        try:
            entry = A2aAgentConfig(
                name="remote-demo",
                agent_card_url=card_url,
                auth=A2aAuthConfig(scheme="edition-sso"),
            )
            p = drv.create_a2a_provider(entry, context_id=None)
            await p.start()
            try:
                text, _ = await _drain(p, "Reply with only the word GANNET-4")
                assert text == "GANNET-4"
                assert server.unauthorized == 0
            finally:
                await p.shutdown()
        finally:
            drv._SCHEMES.pop("edition-sso", None)
            await runner.cleanup()

    async def test_unconfigured_client_cannot_read_a_protected_card(self):
        runner, card_url, server = await fake_a2a_server.serve(port=0, require_bearer="s3cret-1")
        try:
            p = A2AProvider(name="remote-demo", agent_card_url=card_url)
            with pytest.raises(Exception):  # 401 on the card fetch: raise_for_status
                await p.start()
            assert server.unauthorized == 1
            await p.shutdown()
        finally:
            await runner.cleanup()

    async def test_wrong_credential_is_refused_and_never_retried_unauthenticated(self, monkeypatch):
        runner, card_url, server = await fake_a2a_server.serve(port=0, require_bearer="right")
        try:
            p = A2AProvider(
                name="remote-demo",
                agent_card_url=card_url,
                credentials=lambda: {"Authorization": "Bearer wrong"},
                supported_schemes=frozenset({"bearer"}),
                credential_origin=card_url.rsplit("/.well-known", 1)[0],
            )
            with pytest.raises(Exception):
                await p.start()
            assert server.unauthorized == 1
            await p.shutdown()
        finally:
            await runner.cleanup()

    async def test_card_declares_bearer_only_when_required(self):
        for required, expect in (("", False), ("x", True)):
            runner, card_url, _ = await fake_a2a_server.serve(port=0, require_bearer=required)
            try:
                card = fake_a2a_server.agent_card("http://h/", require_bearer=bool(required))
                assert ("securityRequirements" in card) is expect
            finally:
                await runner.cleanup()
