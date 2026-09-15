"""Teams mid-turn routing: /stop, the queue drain, and the command vocabulary.

These cover the affordances Teams gains from being able to EDIT its own
activities. WeCom and Weixin cannot have them because their reply is bound to the
inbound request, so a held message could not be acknowledged and answered later;
Teams can (``PUT .../activities/{id}``), which is what makes the shared
collapsing queue receipt reachable here.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from kiro_crew.teams.client import TeamsInbound
from kiro_crew.teams.commands import COMMAND_SPEC, build_help_text, parse_command
from kiro_crew.teams.transport_dispatch import TeamsDispatcher

_SVC = "https://smba.trafficmanager.net/teams"
_EMAIL = "me@example.com"


def _inbound(text: str) -> TeamsInbound:
    return TeamsInbound(
        conversation_id="CONV",
        conversation_type="personal",
        service_url=_SVC,
        text=text,
        user_email=_EMAIL,
        activity_id="act-1",
    )


class _Client:
    """Records sends and honours the edit contract (update by activity id)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.updates: list[tuple[str, str]] = []
        self._next_id = 0

    async def send_message(self, conversation_id, content, service_url):
        self.sent.append((conversation_id, content, service_url))
        self._next_id += 1
        return f"mid-{self._next_id}"

    async def update_message(self, conversation_id, activity_id, content, service_url):
        self.updates.append((activity_id, content))
        return True

    async def send_typing(self, conversation_id, service_url) -> None:
        return None


class _Provider:
    def __init__(self, *, cancels: bool = True) -> None:
        self.supports_steer = True
        self.cancelled: list[float] = []
        self._cancels = cancels

    def has_active_turn(self) -> bool:
        return True

    async def steer(self, text: str) -> bool:
        return False

    async def cancel(self, *, wait_ack_timeout: float = 0) -> None:
        if not self._cancels:
            raise RuntimeError("cancel exploded")
        self.cancelled.append(wait_ack_timeout)


class _Sessions:
    def __init__(self, provider, *, busy: bool = True) -> None:
        self._p = provider
        self._busy = busy
        self.queues: dict[str, list] = {}
        self.cleared: list[str] = []
        self.mirror_links: dict = {}
        self.opt_outs: dict = {}

    async def aflush(self) -> None:
        # The resume release flushes the session map before it reports success; a
        # double without this correctly surfaces as a release FAILURE.
        return None

    def clear_mirror_links_at(self, link, *, reason: str = "") -> list:
        return []

    def find_mirror_sessions(self, link, *, inbound_only: bool = False) -> list:
        # No resumed dashboard session in these tests, so routing is a no-op. Present
        # because Teams routes EVERY message through the resume resolver.
        return []

    def is_busy(self, key) -> bool:
        return self._busy

    def get_provider(self, key):
        return self._p

    def max_generation(self, bucket: str) -> int:
        return -1

    def enqueue(self, key, msg_ts, text, *, force=False, **kwargs) -> bool:
        if not force and not self._busy:
            return False
        self.queues.setdefault(key, []).append((msg_ts, text, kwargs))
        return True

    def dequeue(self, key):
        queue = self.queues.get(key) or []
        return queue.pop(0) if queue else None

    def clear_queue(self, key) -> None:
        self.cleared.append(key)
        self.queues.pop(key, None)

    def mirror_opt_out(self, key) -> bool:
        return bool(self.opt_outs.get(key))

    def set_mirror_opt_out(self, key, value) -> None:
        self.opt_outs[key] = value

    def get_mirror_link(self, key):
        return self.mirror_links.get(key)

    def set_mirror_link(self, key, link, *, reason="") -> None:
        self.mirror_links[key] = link

    def clear_mirror_link(self, key, *, reason="") -> bool:
        return self.mirror_links.pop(key, None) is not None

    def is_mirror_paused(self, key, *, origin=False) -> bool:
        return False

    def batched_save(self):
        return contextlib.nullcontext()


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        messaging=SimpleNamespace(
            queue_mode="steer", dm_scope="per_user", idle_reset_minutes=0, daily_reset_hour=-1
        ),
        agent=SimpleNamespace(default_agent="kirocrew", approval_mode="interactive"),
        teams=SimpleNamespace(soft_threshold_pct=80, hard_threshold_pct=95),
    )


def _dispatcher(sessions, client) -> TeamsDispatcher:
    d = TeamsDispatcher(
        sessions=sessions,
        ctx_builder=SimpleNamespace(hooks=SimpleNamespace(auto_approve_subagent_spawn=False)),
        cfg=_cfg(),
    )
    d.client = client
    return d


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_cancels_the_turn_and_clears_the_queue(self) -> None:
        provider = _Provider()
        sessions = _Sessions(provider)
        client = _Client()
        d = _dispatcher(sessions, client)
        key = d._session_key(_EMAIL)
        sessions.queues[key] = [("1", "held", {})]

        await d._handle_stop(_inbound("/stop"))

        # wait_ack_timeout=0: the cancel is cooperative and fire-and-forget, so
        # the acknowledgement is immediate and the turn stops at its next safe
        # point. Waiting here would stall the reply.
        assert provider.cancelled == [0]
        assert sessions.cleared == [key]
        assert "Stopped" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_stop_with_nothing_running_still_clears_the_queue(self) -> None:
        sessions = _Sessions(_Provider(), busy=False)
        client = _Client()
        d = _dispatcher(sessions, client)

        await d._handle_stop(_inbound("/stop"))

        assert sessions.cleared == [d._session_key(_EMAIL)]
        assert "Nothing was running" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_failing_cancel_still_clears_and_answers(self) -> None:
        """A provider that cannot cancel must not leave the queue full."""
        sessions = _Sessions(_Provider(cancels=False))
        client = _Client()
        d = _dispatcher(sessions, client)

        await d._handle_stop(_inbound("/stop"))

        assert sessions.cleared == [d._session_key(_EMAIL)]
        assert client.sent, "the user must still get an acknowledgement"

    @pytest.mark.asyncio
    async def test_stop_is_reachable_through_the_command_intercept(self) -> None:
        sessions = _Sessions(_Provider())
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("/cancel"))

        assert sessions.cleared == [d._session_key(_EMAIL)], "/cancel aliases /stop"


class TestQueueReceipt:
    @pytest.mark.asyncio
    async def test_a_burst_grows_ONE_receipt_rather_than_many_bubbles(self) -> None:
        sessions = _Sessions(_Provider())
        client = _Client()
        d = _dispatcher(sessions, client)
        inbound = _inbound("first")

        await d._enqueue_with_receipt(d._session_key(_EMAIL), inbound, "first")
        await d._enqueue_with_receipt(d._session_key(_EMAIL), inbound, "second")

        assert len(client.sent) == 1, "the receipt is created once"
        assert client.updates, "and then EDITED in place as the burst grows"
        assert "Queued (2)" in client.updates[-1][1]

    @pytest.mark.asyncio
    async def test_the_receipt_is_edited_never_deleted_on_cancel(self) -> None:
        """The receipt is the durable record that a message was accepted."""
        sessions = _Sessions(_Provider())
        client = _Client()
        d = _dispatcher(sessions, client)
        inbound = _inbound("held")
        await d._enqueue_with_receipt(d._session_key(_EMAIL), inbound, "held")

        await d._handle_stop(inbound)

        assert any("Cancelled" in body for _, body in client.updates)


class TestCommandVocabulary:
    def test_every_spec_alias_parses_to_its_canonical_name(self) -> None:
        for canonical, aliases, _ in COMMAND_SPEC:
            for alias in aliases:
                assert parse_command(alias) == canonical

    def test_help_lists_every_command_so_it_cannot_drift(self) -> None:
        """A hand-written help card silently stops matching the parser."""
        help_text = build_help_text()
        for canonical, _, _ in COMMAND_SPEC:
            assert f"/{canonical}" in help_text

    def test_an_argument_does_not_defeat_the_parser(self) -> None:
        assert parse_command("/yolo on") == "yolo"

    def test_a_non_command_is_not_a_command(self) -> None:
        assert parse_command("what time is it") is None
        assert parse_command("/nonsense") is None


class TestDrain:
    @pytest.mark.asyncio
    async def test_a_burst_collapses_into_one_turn(self, monkeypatch) -> None:
        """N queued messages become ONE combined turn, not N replayed turns."""
        turns: list[str] = []

        async def _fake_drive(turn, **kw):
            turns.append(turn.user_text)

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
        monkeypatch.setattr(
            "kiro_crew.teams.transport_dispatch.inbound_permitted",
            lambda _c: _true(),
        )
        sessions = _Sessions(_Provider(), busy=False)
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        sessions.queues[key] = [("1", "first", {}), ("2", "second", {})]

        await d._drain_queue(key, _inbound("x"))

        assert turns == ["first\n\nsecond"], "the burst must collapse, order preserved"

    @pytest.mark.asyncio
    async def test_the_drained_turn_does_not_nest_another_drain(self, monkeypatch) -> None:
        """A replay that re-entered the drain would nest one per burst round."""
        seen: list[bool] = []
        real = TeamsDispatcher._drain_queue

        async def _spy(self, session_key, inbound):
            seen.append(True)
            await real(self, session_key, inbound)

        async def _fake_drive(turn, **kw):
            return None

        monkeypatch.setattr(TeamsDispatcher, "_drain_queue", _spy)
        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
        monkeypatch.setattr(
            "kiro_crew.teams.transport_dispatch.inbound_permitted", lambda _c: _true()
        )
        sessions = _Sessions(_Provider(), busy=False)
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        sessions.queues[key] = [("1", "held", {})]

        await d.handle_message(_inbound("live"))

        assert (
            len(seen) == 1
        ), f"drain re-entered {len(seen)} times; the replay must pass drain=False"


async def _true() -> bool:
    return True
