"""The Discord leg: Discord's declared drift run on its real renderer.

The turn cases Discord's own suite drove through ``handle_message``, run on the real
``DiscordRenderer`` over a fake REST client with
:data:`~kiro_crew.messaging.dispatch.DISCORD_DRIFT`. The pipeline itself is in
``test_channel_turns.py``; the shared keys, autouse stubs and builders are in
``channel_turns_helpers.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from channel_turn_fakes import (
    FakeDiscordClient,
    FakeSessions,
    RecordingCtxBuilder,
    ScriptedProvider,
    answer,
    complete,
)
from channel_turns_helpers import governance_fixture  # noqa: F401
from channel_turns_helpers import published_fixture  # noqa: F401
from channel_turns_helpers import spooled_fixture  # noqa: F401
from channel_turns_helpers import KEY, ROUTE, _answer, _turns

from kiro_crew.acp.types import STOP_REASON_CANCELLED
from kiro_crew.discord.renderer import DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging import dispatch as D
from kiro_crew.messaging.dispatch import DISCORD_DRIFT, Verdict

# ── the Discord leg: Discord's drift on its real renderer ────────────────────


class _LoggingDiscordRenderer(DiscordRenderer):
    """The real renderer, writing its turn start to a session ledger and, when
    asked, failing its close the way a flush can."""

    def __init__(self, client: FakeDiscordClient, ledger: list[Any], *, close_raises: bool = False):
        super().__init__(client, "c1", DISCORD_CAPABILITIES, session_key=KEY)
        self._ledger = ledger
        self._close_raises = close_raises

    async def on_turn_start(self) -> None:
        self._ledger.append(("on_turn_start", ""))
        await super().on_turn_start()

    async def close(self) -> None:
        await super().close()
        if self._close_raises:
            raise RuntimeError("renderer finalization failed")


class TestTheDiscordLeg:
    """The turn cases Discord's own suite drove through ``handle_message``, run at
    the pipeline with ``DISCORD_DRIFT`` and the real ``DiscordRenderer`` over a fake
    REST client, so what is asserted is what the conversation could read."""

    @staticmethod
    def _run(sessions: FakeSessions, client: FakeDiscordClient, **kw: Any) -> Any:
        renderer = kw.pop("renderer", None) or _LoggingDiscordRenderer(client, sessions.ledger)
        return _answer(
            _turns(sessions, drift=DISCORD_DRIFT, channel="discord", **kw), renderer=renderer
        )

    def test_a_turn_streams_its_reply_and_releases_once(self) -> None:
        client = FakeDiscordClient()
        sessions = FakeSessions(ScriptedProvider(answer("the reply")))

        outcome = self._run(sessions, client)

        assert outcome.verdict is Verdict.ANSWERED and "the reply" in client.shown()
        assert sessions.count("release") == 1 and sessions.held == 0

    def test_the_typing_indicator_starts_before_the_cold_start(self) -> None:
        sessions = FakeSessions()

        self._run(sessions, FakeDiscordClient())

        names = sessions.names()
        assert names.index("on_turn_start") < names.index("get_or_create")

    def test_a_disconnected_conversation_posts_nothing(self) -> None:
        client = FakeDiscordClient()
        provider = ScriptedProvider(answer("the reply"))
        sessions = FakeSessions(provider)
        # Both flags: which one a turn reads depends on whether its key is native
        # to the channel (that rule has its own test in test_channel_turns.py).
        sessions.muted.update({(KEY, True), (KEY, False)})

        outcome = self._run(sessions, client)

        assert provider.prompts == ["hi"], "the message still lands in the session"
        assert client.sent == [] and client.edits == []
        assert outcome.verdict is Verdict.ANSWERED and sessions.count("release") == 1

    def test_a_member_memory_refusal_is_posted_redacted(self, monkeypatch) -> None:
        secret = "ghp_" + "x" * 36

        async def _refuse(_ctx: Any, _key: str) -> str:
            raise UnknownMemoryStore(
                f"Member memory unavailable: cannot read /srv/alice/memory.db; token={secret}. "
                "Repair this member's memory. Global Memory V1 was not used."
            )

        monkeypatch.setattr(D, "session_store_for_turn", _refuse)
        client = FakeDiscordClient()
        sessions = FakeSessions()

        outcome = self._run(sessions, client)

        shown = client.shown()
        assert outcome.verdict is Verdict.MEMORY_REFUSED
        assert "Repair this member's memory" in shown
        assert "alice" not in shown and secret not in shown
        assert sessions.count("get_or_create") == 0 and sessions.count("release") == 0

    def test_a_shutdown_never_opens_the_turn_and_spools_its_route(self, spooled) -> None:
        provider = ScriptedProvider()
        sessions = FakeSessions(provider, closing=True)

        outcome = self._run(sessions, FakeDiscordClient())

        assert outcome.verdict is Verdict.SHUTTING_DOWN and provider.prompts == []
        assert spooled == [("discord", ROUTE)] and sessions.count("release") == 1

    def test_a_restricted_shutdown_is_not_spooled(self, spooled) -> None:
        async def _restricted(_key: str) -> bool:
            return True

        self._run(FakeSessions(closing=True), FakeDiscordClient(), restricted=_restricted)

        assert spooled == []

    def test_a_cold_start_failure_releases_and_charges_nothing(self) -> None:
        sessions = FakeSessions(acquire_raises=RuntimeError("cold start failed"))

        outcome = self._run(sessions, FakeDiscordClient())

        assert outcome.verdict is Verdict.FAILED
        assert sessions.count("release") == 0 and sessions.count("record_failure") == 0

    def test_the_session_is_released_when_the_close_raises(self) -> None:
        client = FakeDiscordClient()
        sessions = FakeSessions(ScriptedProvider(answer("the reply")))
        renderer = _LoggingDiscordRenderer(client, sessions.ledger, close_raises=True)

        outcome = self._run(sessions, client, renderer=renderer)

        assert outcome.verdict is Verdict.ANSWERED and sessions.count("release") == 1

    @pytest.mark.parametrize("fail_sends", [False, True])
    def test_a_compacted_session_forwards_the_flag_and_a_landed_turn_keeps_it(
        self, fail_sends
    ) -> None:
        """A send that failed after the provider took the context is a delivery
        failure, not a lost prompt: the flag stays consumed either way."""
        client = FakeDiscordClient(fail_sends=fail_sends)
        sessions = FakeSessions(ScriptedProvider(answer("the reply")), reinjection=True)
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)

        outcome = self._run(sessions, client, ctx=ctx)

        assert (outcome.verdict is Verdict.UNDELIVERED) is fail_sends
        assert ctx.builds[0]["needs_reinjection"] is True
        assert sessions.count("mark_reinjection") == 0

    @pytest.mark.parametrize(
        "script",
        [
            pytest.param([RuntimeError("provider fell over")], id="raised"),
            pytest.param([complete(STOP_REASON_CANCELLED)], id="cancelled"),
        ],
    )
    def test_a_consuming_turn_that_never_landed_puts_the_flag_back(self, script) -> None:
        sessions = FakeSessions(ScriptedProvider(script), reinjection=True)
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)

        self._run(sessions, FakeDiscordClient(), ctx=ctx)

        assert ctx.builds[0]["needs_reinjection"] is True
        assert sessions.count("mark_reinjection") == 1
