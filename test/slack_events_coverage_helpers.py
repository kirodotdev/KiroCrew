"""Fixtures and builders shared by the :mod:`kiro_crew.slack.events` coverage tests.

``test_slack_events_coverage.py`` and its ``_message_helpers``, ``_route_guards`` and
``_route_flow`` siblings import these by name. The three autouse fixtures keep SEL
writes in memory, reset the trusted-bot turn ledger and isolate the home directory
for every test in each of them; the two that tests also request by parameter are
declared with ``name=`` and imported under their function names, so a parameter
never shadows an import.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import (
    ACTIVATION_ALWAYS,
    ChannelConfig,
    KiroCrewConfig,
    MessagingConfig,
)
from kiro_crew.slack import events as ev


@pytest.fixture(name="_mock_sel", autouse=True)
def _mock_sel_fixture():
    """Keep every SEL audit write in memory (no filesystem side effects)."""
    fake = MagicMock()
    with patch("kiro_crew.slack.events.sel", return_value=fake):
        yield fake


@pytest.fixture(autouse=True)
def _fresh_trusted_bot_turn_ledger(_floor_monkeypatch):
    """Give every test a fresh ledger and restore the process global afterward."""
    _floor_monkeypatch.setattr(ev, "_trusted_bot_turns", ev.TrustedBotTurnLedger())


@pytest.fixture(name="_isolated_home", autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_slack_events_coverage*.py modules calls monkeypatch.undo()
def _isolated_home_fixture(tmp_path, monkeypatch):
    """Point every home-derived path at ``tmp_path`` so nothing touches real HOME."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".kiro" / "crew"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / ".kiro"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _make_orch(
    channels: dict[str, ChannelConfig] | None = None,
    dm_activation: str = ACTIVATION_ALWAYS,
    use_transport: bool = False,
) -> MagicMock:
    """Minimal mock ``GatewayOrchestrator`` (mirrors ``test_channel_activation``)."""
    orch = MagicMock()
    orch._cfg = KiroCrewConfig(
        slack_channels=channels or {},
        slack_dm_activation=dm_activation,
        messaging=MessagingConfig(use_transport=use_transport),
    )
    orch.slack_command = "kirocrew"
    orch._owner_id = "U_OWNER"
    orch._allowed_users = {"U_OWNER"}
    orch._tracking_channels = set()
    orch._open_channels = set()
    orch._approval_mode = ""
    orch.channel_history = MagicMock()
    orch.channel_history._user_names = {}
    orch.slack = AsyncMock()
    # Sync accessor on an AsyncMock would return an un-awaited coroutine.
    orch.slack.record_channel_team = MagicMock()
    # Same for the home-team resolver the inbound path awaits.
    orch.slack.ensure_channel_team = AsyncMock()
    # A bare AsyncMock's return_value is itself an AsyncMock, so `info.get(...)`
    # in the display-name lookup would hand back a coroutine. Return a real dict.
    orch.slack.get_user_info = AsyncMock(return_value={})
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.is_busy = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.has_session = MagicMock(return_value=False)
    orch.sessions.get_session_for_thread = MagicMock(return_value=None)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.sessions.clear_queue = MagicMock()
    # Synchronous on the manager: a !stop partitions what these hand back.
    orch.sessions.detach_queue = MagicMock(return_value=())
    orch.sessions.restore_queue = MagicMock()
    orch.sessions.cancel_queued = MagicMock(return_value=False)
    orch.ctx_builder = None
    orch.cron_svc = None
    orch.conv_log = None
    orch.consolidator = None
    orch.subagent_mgr = None
    orch.task_runner = None
    orch.dashboard_state = None
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


async def _drain(orch: MagicMock) -> None:
    """Let fire-and-forget handler tasks run to completion."""
    for _ in range(3):
        # flake-ok: three bounded zero-delay yields let fire-and-forget tasks start before the gather, moved verbatim
        await asyncio.sleep(0)
    tasks = list(orch._handler_tasks) + list(ev._bg_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class _capture_respond:
    """Capture ``_handle_slash``'s ``response_url`` POST bodies without networking."""

    def __init__(self, sink: list[dict], response_url: str = "https://hooks.example.invalid/x"):
        self._sink = sink
        self._response_url = response_url
        self._patches: list = []

    def __enter__(self) -> _capture_respond:
        sink = self._sink

        class _Session:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def post(self_inner, url, json=None):  # noqa: A002 - aiohttp kwarg name
                sink.append(json or {})
                return MagicMock()

        p = patch("kiro_crew.slack.events.aiohttp.ClientSession", _Session)
        p.start()
        self._patches.append(p)
        # _handle_slash reads response_url straight off the payload, so inject it
        # by patching the module's dict lookup target instead: simplest is to let
        # callers pass the URL through the payload. Tests using this helper set a
        # response_url on the payload via _patch_payload below.
        self._orig = ev._handle_slash
        url = self._response_url

        async def _wrapped(orch, payload):
            payload = dict(payload)
            payload.setdefault("response_url", url)
            return await self._orig(orch, payload)

        p2 = patch("kiro_crew.slack.events._handle_slash", _wrapped)
        p2.start()
        self._patches.append(p2)
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


def _event(**over: object) -> dict:
    # A DM channel ("D…") so the default slack_dm_activation=always applies —
    # a "C…" group channel defaults to activation=mention and would drop a
    # plain message before reaching the branch under test.
    base = {
        "user": "U_OWNER",
        "channel": "D1",
        "text": "hello",
        "ts": "100.0",
        "team": "T1",
    }
    base.update(over)  # type: ignore[arg-type]
    return base
