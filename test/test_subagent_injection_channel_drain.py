"""A message queued during a channel completion-injection turn is drained after it.

The channel route claims the parent's session to inject a sub-agent completion. A
person who writes during that hidden turn finds the session busy, so the channel's
dispatcher queues the message and sends a "queued" receipt. A dispatcher drains its
queue only at the tail of its OWN turn, and the injection turn is not one of those,
so the injection's release must wake the drains of the channels whose entries are
queued on the key. Otherwise the message sits until the person writes again.

Drives the real ``SessionManager`` lease and the real drain registry. No sleeps: the
release's wake runs as a tracked background task and the test awaits it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging.queue_drain import register_drain, reset_drains, tag_entry
from kiro_crew.session import SessionManager
from kiro_crew.subagent import SubagentInfo

#: Ceiling for a wait that only fails on a hang, so a regression fails by name.
_HANG_CEILING_S = 30


def _provider_factory() -> Any:
    def factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **kwargs: Any):
        m = AsyncMock()
        m.start = AsyncMock()
        m.memory_mode = kwargs.get("memory_mode", "persistent")
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.disown_work_dir = MagicMock()
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_abort_target = lambda: None
        m.spawn_identity = ""
        m._runtime = None
        m.cancel = AsyncMock(return_value="acked")
        return m

    return factory


def _gateway(sessions: SessionManager) -> Any:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.sessions = sessions
    gateway.ctx_builder = None
    gateway.conv_log = None
    gateway.dashboard_state = None
    gateway.slack = None
    gateway._owner_id = ""
    gateway.subagent_mgr = MagicMock()
    gateway._channel_reply_link = MagicMock(return_value=None)
    gateway._deliver_channel_reply = AsyncMock()
    return gateway


@pytest.fixture(autouse=True)
def _clean_drains() -> Iterator[None]:
    reset_drains()
    yield
    reset_drains()


async def _inject(gateway: Any, key: str) -> None:
    info = SubagentInfo(id="child-1", task="work", parent_session_key=key)
    info.result = "done"
    info.done = True
    with patch("kiro_crew.slack.gateway._MAX_INJECT_ATTEMPTS", 1):
        await asyncio.wait_for(
            gateway._inject_subagent_into_channel_parent(
                info, announce="child done", body="b", title="t", parent_key=key
            ),
            _HANG_CEILING_S,
        )


async def _settle_background(sessions: SessionManager) -> None:
    """Run every wake the release scheduled off its synchronous call to completion."""
    pending = list(sessions._background_tasks)
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), _HANG_CEILING_S)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "key"),
    [("discord", "discord:dm:42"), ("telegram", "telegram:dm:7")],
)
async def test_channel_injection_wakes_drain_for_message_queued_mid_turn(
    channel: str, key: str
) -> None:
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    gateway = _gateway(sessions)
    drained = asyncio.Event()
    drained_keys: list[str] = []
    busy_when_drained: list[bool] = []

    async def drain(session_key: str) -> None:
        drained_keys.append(session_key)
        busy_when_drained.append(sessions.is_busy(session_key))
        drained.set()

    register_drain(channel, drain)

    async def inject_while_user_writes(info, client, msg, parent_key, label):
        # The person writes while the hidden turn holds the lease; the dispatcher's
        # busy path queues it, tagged with its channel as every producer does.
        assert sessions.is_busy(parent_key)
        assert sessions.enqueue(
            parent_key, "m1", "are you there?", **tag_entry({}, channel, "owner-1")
        )
        return "synthesized reply"

    gateway._inject_with_retry = inject_while_user_writes
    try:
        await _inject(gateway, key)
        await _settle_background(sessions)
        assert drained.is_set(), "nothing woke the channel drain for the queued message"
        assert drained_keys == [key]
        # Woken after the release, so the drain can take the lease for the queued turn.
        assert busy_when_drained == [False]
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_channel_injection_with_empty_queue_wakes_no_drain() -> None:
    key = "discord:dm:42"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    gateway = _gateway(sessions)
    drain = AsyncMock()
    register_drain("discord", drain)
    gateway._inject_with_retry = AsyncMock(return_value="reply")
    try:
        await _inject(gateway, key)
        await _settle_background(sessions)
        drain.assert_not_awaited()
    finally:
        await sessions.close_all()
