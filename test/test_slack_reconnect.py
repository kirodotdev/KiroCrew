"""Slack Reconnect: ``GatewayOrchestrator.reconnect_slack`` + ``POST /api/slack/reconnect``.

Slack credentials are hoisted once, in ``GatewayOrchestrator.__init__``, and
nothing reassigned them afterwards, so a token or owner ID saved from the
dashboard could take effect only through ``POST /api/restart``. These tests
pin the in-place alternative:

* the orchestrator re-reads the store, recomputes ``_slack_enabled`` from the
  tokens now on disk (a stale ``False`` would make ``init_socket_mode`` a
  silent no-op), tears the old socket client down, re-awaits
  ``init_socket_mode`` on the running loop and records the outcome where the
  settings badge reads it;
* a store that cannot be read leaves the live connection untouched;
* the dashboard's Slack client mirror is cleared with the old socket and
  published again only behind a connected one, and the dashboard's
  ``owner_id`` follows the saved owner -- so a rejected workspace or a former
  owner never keeps dashboard access through a reconnect;
* the handler module's authorization subject (owner + allowlist) follows the
  saved owner on EVERY reconnect, including the ones that stop before a
  handshake, and an old socket client that will not close aborts the attempt
  with that subject cleared -- so a listener that outlives its credentials
  accepts no privileged command from the former owner;
* concurrent callers share one handshake;
* the route carries the PUT's direct-local gate and answers the
  ``connected`` / ``connect_error`` shape ``GET /api/slack/config`` documents.

The orchestrator is built through ``__new__`` (its ``__init__`` boots the
world); ``init_socket_mode`` and ``_connect_slack`` are replaced by recorders
because a real handshake needs Slack.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config.loader import CRED_OWNER_ID, CRED_SLACK_APP_TOKEN, CRED_SLACK_BOT_TOKEN

# Obvious placeholders: nothing here reaches Slack.
NEW_CREDS = {
    CRED_SLACK_APP_TOKEN: "xapp-new-not-a-real-value",
    CRED_SLACK_BOT_TOKEN: "xoxb-new-not-a-real-value",
    CRED_OWNER_ID: "U0NEWOWNER",
}
FORMER_OWNER = "U0FORMEROWNER"


_STATE_DIRS: list[tempfile.TemporaryDirectory[str]] = []


@pytest.fixture(autouse=True)
def _remove_state_dirs() -> Any:
    """Every ``_orch()`` state dir of a test goes with the test."""
    yield
    while _STATE_DIRS:
        _STATE_DIRS.pop().cleanup()


def _orch(creds: dict[str, str] | Exception = NEW_CREDS, *, old_client: Any = None) -> Any:
    """A GatewayOrchestrator in the state a failed boot leaves it in.

    ``_slack_enabled`` is False and the tokens are stale, exactly the state the
    issue describes: ``init_socket_mode`` early-returns on that flag, so a
    reconnect that does not recompute it is a no-op.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._cfg = MagicMock()
    if isinstance(creds, Exception):
        orch._cfg.load_credentials.side_effect = creds
    else:
        orch._cfg.load_credentials.return_value = dict(creds)
    orch._app_token = "xapp-stale"
    orch._bot_token = "xoxb-stale"
    orch._owner_id = ""
    orch._allowed_users = set()
    orch._slack_enabled = False
    orch._slack_connect_error = "invalid_auth"
    orch.slack = None
    orch._socket_client = old_client
    orch._slack_seen = MagicMock(name="boot-seen-cache")
    orch._slack_reconnect_task = None
    orch.dashboard_state = MagicMock()
    orch.dashboard_state.slack_socket_connected = False
    orch.dashboard_state.slack_connect_error = "invalid_auth"
    orch.dashboard_state.slack_client = None
    orch.dashboard_state.owner_id = "U0FORMEROWNER"
    orch._tracking_channels = set()
    orch._background_tasks = set()
    orch._slack_links_team_id = ""
    orch._slack_workspace_pending = None
    orch._slack_workspace_switch = None
    orch._slack_workspace_record_loaded = True  # the fixture's in-memory identity is the record
    # The publish re-schedules the inbound spool replay; the double records the call.
    orch._schedule_inbound_replay = MagicMock(name="_schedule_inbound_replay")
    orch._slack_workspace_record_damaged = False
    orch._slack_socket_authority = None
    orch._slack_client_settled = asyncio.Event()
    orch._slack_client_settled.set()
    orch.cron_svc = None
    # A private, empty state dir: the workspace record starts absent, and a
    # test can read back what a switch persisted. Removed after the test by
    # ``_remove_state_dirs``.
    state_dir = tempfile.TemporaryDirectory()
    _STATE_DIRS.append(state_dir)
    orch._slack_workspace_state_path = Path(state_dir.name) / "slack_workspace.json"
    orch.sessions = MagicMock(name="sessions")
    orch.sessions.clear_all_slack_links.return_value = []
    orch.sessions.freeze_slack_links.return_value = []
    orch.sessions.restore_slack_links.return_value = []
    orch.sessions.aflush = AsyncMock(name="aflush")
    return orch


class _Recorder:
    """Stand-in for ``init_socket_mode`` that behaves like the real one's edges.

    On ``owner_missing`` it mirrors the real early return (flag off, no
    client); otherwise it installs a fresh client and records where it ran.
    """

    def __init__(self, *, owner_missing: bool = False, hold: asyncio.Event | None = None):
        self.calls: list[tuple[Any, Any]] = []
        self.web_api_clients: list[Any] = []
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.threads: list[threading.Thread] = []
        self.owner_missing = owner_missing
        self.hold = hold
        self.client = MagicMock(name="new-socket-client")

    async def __call__(self, orch: Any, seen: Any, *, web_api_client: Any = None) -> None:
        self.calls.append((orch, seen))
        self.web_api_clients.append(web_api_client)
        self.loops.append(asyncio.get_running_loop())
        self.threads.append(threading.current_thread())
        if self.hold is not None:
            await self.hold.wait()
        if not orch._slack_enabled:
            return
        if self.owner_missing or not orch._owner_id:
            orch._slack_enabled = False
            orch.slack = None
            return
        orch._socket_client = self.client
        # As the real one: the socket's authority, for the gateway to revoke.
        from kiro_crew.slack.affinity import SocketAuthority

        orch._slack_socket_authority = SocketAuthority(orch._owner_id)


def _patches(recorder: _Recorder, connect: Any = None, client_cls: Any = None):
    """Patch the handshake seams; ``connect`` replaces ``_connect_slack``."""
    connect_mock = connect if connect is not None else AsyncMock(return_value=True)
    return (
        patch("kiro_crew.slack.events.init_socket_mode", recorder),
        patch("kiro_crew.slack.gateway.RealSlackClient", client_cls or MagicMock()),
        patch("kiro_crew.slack.gateway.GatewayOrchestrator._connect_slack", connect_mock),
    )


async def _run(
    orch: Any, recorder: _Recorder, connect: Any = None, client_cls: Any = None
) -> dict[str, object]:
    p_init, p_client, p_connect = _patches(recorder, connect, client_cls)
    with p_init, p_client, p_connect:
        return await orch.reconnect_slack()


# ── orchestrator ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconnect_recomputes_enabled_and_hoists_new_credentials() -> None:
    old = MagicMock(name="old-socket-client")
    old.close = AsyncMock()
    orch = _orch(old_client=old)
    rec = _Recorder()
    client_cls = MagicMock(name="RealSlackClient")

    result = await _run(orch, rec, client_cls=client_cls)

    # The stale False is recomputed from the tokens now on disk -- the whole
    # point: without it init_socket_mode returns before doing anything.
    assert orch._slack_enabled is True
    assert orch._app_token == NEW_CREDS[CRED_SLACK_APP_TOKEN]
    assert orch._bot_token == NEW_CREDS[CRED_SLACK_BOT_TOKEN]
    assert orch._owner_id == NEW_CREDS[CRED_OWNER_ID]
    assert orch._allowed_users == {NEW_CREDS[CRED_OWNER_ID]}
    # Old client torn down before the new handshake; new one installed.
    old.close.assert_awaited_once()
    assert orch._socket_client is rec.client
    # The boot-time dedup cache is reused, so redelivered envelopes stay deduped.
    assert rec.calls == [(orch, orch._slack_seen)]
    # Web API client rebuilt on the new bot token and, the socket being
    # connected, mirrored to the dashboard; the dashboard's owner follows.
    client_cls.assert_called_once_with(NEW_CREDS[CRED_SLACK_BOT_TOKEN])
    assert orch.slack is client_cls.return_value
    assert orch.dashboard_state.slack_client is orch.slack
    assert orch.dashboard_state.owner_id == NEW_CREDS[CRED_OWNER_ID]
    # Outcome recorded where GET /api/slack/config reads it, and returned.
    assert orch.dashboard_state.slack_socket_connected is True
    assert orch.dashboard_state.slack_connect_error == ""
    assert result == {"connected": True, "connect_error": ""}


@pytest.mark.asyncio
async def test_reconnect_awaits_init_socket_mode_on_the_running_loop() -> None:
    """WSSocketModeClient needs a current loop in the constructing thread."""
    orch = _orch()
    rec = _Recorder()

    await _run(orch, rec)

    assert rec.loops == [asyncio.get_running_loop()]
    assert rec.threads == [threading.current_thread()]


@pytest.mark.asyncio
async def test_load_failure_leaves_the_live_connection_untouched() -> None:
    old = MagicMock(name="old-socket-client")
    old.close = AsyncMock()
    orch = _orch(OSError("store unreadable"), old_client=old)
    rec = _Recorder()

    with pytest.raises(OSError):
        await _run(orch, rec)

    old.close.assert_not_awaited()
    assert orch._socket_client is old
    assert orch._app_token == "xapp-stale"
    assert orch._slack_enabled is False
    assert rec.calls == []
    # Nothing was recorded either: the badge still describes the live state.
    assert orch.dashboard_state.slack_connect_error == "invalid_auth"


@pytest.mark.asyncio
async def test_missing_tokens_disable_slack_without_a_handshake() -> None:
    old = MagicMock(name="old-socket-client")
    old.close = AsyncMock()
    orch = _orch({CRED_OWNER_ID: "U0NEWOWNER"}, old_client=old)
    rec = _Recorder()

    result = await _run(orch, rec)

    old.close.assert_awaited_once()  # a cleared token must not keep the old socket alive
    assert orch._socket_client is None
    assert orch._slack_enabled is False
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None
    assert rec.calls == []
    assert result == {"connected": False, "connect_error": "tokens_missing"}
    assert orch.dashboard_state.slack_socket_connected is False
    assert orch.dashboard_state.slack_connect_error == "tokens_missing"


@pytest.mark.asyncio
async def test_missing_owner_is_named_for_the_badge() -> None:
    creds = {k: v for k, v in NEW_CREDS.items() if k != CRED_OWNER_ID}
    orch = _orch(creds)
    rec = _Recorder()

    result = await _run(orch, rec)

    assert len(rec.calls) == 1  # init_socket_mode ran (the flag was recomputed True)
    assert orch._socket_client is None
    assert result == {"connected": False, "connect_error": "owner_id_missing"}


@pytest.mark.asyncio
async def test_connect_failure_reason_is_surfaced() -> None:
    orch = _orch()
    rec = _Recorder()

    async def _connect(self: Any) -> bool:
        self._slack_connect_error = "invalid_auth"
        return False

    result = await _run(orch, rec, connect=_connect)

    assert result == {"connected": False, "connect_error": "invalid_auth"}
    assert orch.dashboard_state.slack_socket_connected is False
    assert orch.dashboard_state.slack_connect_error == "invalid_auth"


def _bind_former_owner() -> None:
    """Put the handler module in the state a booted gateway leaves it in."""
    from kiro_crew.slack import handler

    handler.set_allowed_users({FORMER_OWNER})
    handler.set_owner_id(FORMER_OWNER)
    assert handler.is_allowed_user(FORMER_OWNER)


def _unbind_handler() -> None:
    from kiro_crew.slack import handler

    handler.set_allowed_users(set())
    handler.set_owner_id("")


@pytest.mark.asyncio
async def test_old_client_close_failure_aborts_and_revokes_the_former_owner() -> None:
    """GPT F1: a close that fails leaves a listener up; the reconnect must not
    then hoist new credentials around it (tokens now missing -> no handshake ->
    the old listener keeps the former owner). Abort, keep the client referenced,
    clear the authorization subject, name the outcome."""
    from kiro_crew.slack import handler

    old = MagicMock(name="old-socket-client")
    old.close = AsyncMock(side_effect=RuntimeError("websocket would not close"))
    orch = _orch({CRED_OWNER_ID: "U0NEWOWNER"}, old_client=old)  # tokens cleared on disk
    rec = _Recorder()
    _bind_former_owner()
    try:
        result = await _run(orch, rec)

        assert result == {"connected": False, "connect_error": "previous_client_close_failed"}
        assert orch._socket_client is old  # still referenced: retry / shutdown close it again
        assert rec.calls == []  # no handshake attempted around a live listener
        # Nothing from the store was hoisted.
        assert orch._app_token == "xapp-stale"
        assert orch._owner_id == ""
        assert orch._slack_enabled is False
        # The surviving listener authorizes nobody.
        assert handler.is_allowed_user(FORMER_OWNER) is False
        assert handler.is_owner(FORMER_OWNER) is False
        # The badge reads the failure; the mirror stays empty.
        assert orch.dashboard_state.slack_socket_connected is False
        assert orch.dashboard_state.slack_connect_error == "previous_client_close_failed"
        assert orch.dashboard_state.slack_client is None
    finally:
        _unbind_handler()


@pytest.mark.asyncio
async def test_close_timeout_is_a_failed_close() -> None:
    """The bounded close's timeout is a failed close too (it is an Exception)."""
    old = MagicMock(name="old-socket-client")  # close() is a plain MagicMock: wait_for is patched
    orch = _orch(old_client=old)
    rec = _Recorder()

    with patch("kiro_crew.slack.gateway.asyncio.wait_for", side_effect=asyncio.TimeoutError):
        result = await _run(orch, rec)

    assert result["connect_error"] == "previous_client_close_failed"
    assert orch._socket_client is old
    assert rec.calls == []


@pytest.mark.asyncio
async def test_tokens_missing_rebinds_the_handler_subject_to_the_saved_owner() -> None:
    """The path that never reaches init_socket_mode must still move the
    handler module off the former owner (init_socket_mode is the only other
    writer of those globals)."""
    from kiro_crew.slack import handler

    old = MagicMock(name="old-socket-client")
    old.close = AsyncMock()
    orch = _orch({CRED_OWNER_ID: "U0NEWOWNER"}, old_client=old)
    rec = _Recorder()
    _bind_former_owner()
    try:
        result = await _run(orch, rec)

        assert result["connect_error"] == "tokens_missing"
        assert rec.calls == []
        assert handler.is_allowed_user(FORMER_OWNER) is False
        assert handler.is_owner("U0NEWOWNER") is True
    finally:
        _unbind_handler()


@pytest.mark.asyncio
async def test_cleared_owner_leaves_no_handler_subject() -> None:
    from kiro_crew.slack import handler

    creds = {k: v for k, v in NEW_CREDS.items() if k != CRED_OWNER_ID}
    orch = _orch(creds)
    rec = _Recorder()
    _bind_former_owner()
    try:
        result = await _run(orch, rec)

        assert result["connect_error"] == "owner_id_missing"
        assert handler.is_allowed_user(FORMER_OWNER) is False
        assert handler._owner_id == ""
        assert handler._allowed_users == set()
    finally:
        _unbind_handler()


@pytest.mark.asyncio
async def test_rejected_workspace_leaves_no_client_in_the_dashboard() -> None:
    """The enterprise gate's decline must not leave a sendable client behind.

    ``init_socket_mode`` clears ``orch.slack`` on that path but never touched
    the dashboard mirror, so a reconnect that published the mirror before the
    handshake would hand every dashboard Slack sender a client on a workspace
    the gate rejected.
    """
    old_web = MagicMock(name="old-web-client")
    orch = _orch()
    orch.slack = old_web
    orch.dashboard_state.slack_client = old_web
    seen: list[Any] = []

    class _Rejecting(_Recorder):
        async def __call__(self, orch: Any, seen_cache: Any, *, web_api_client: Any = None) -> None:
            # What the dashboard AND the orchestrator held while the handshake ran.
            seen.append((orch.dashboard_state.slack_client, orch.slack))
            orch._slack_enabled = False
            orch.slack = None  # the real early return does this too

    result = await _run(orch, _Rejecting())

    assert seen == [(None, None)]  # the old client was gone BEFORE validation, everywhere
    assert result == {"connected": False, "connect_error": "enterprise_validation_failed"}
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_socket_connected is False


@pytest.mark.asyncio
async def test_failed_handshake_publishes_no_client() -> None:
    """A client whose socket did not connect is published nowhere: not to the
    dashboard and not as ``orch.slack`` either. The candidate stays private,
    so a cron delivery or a dashboard send during or after the failed attempt
    finds no client to pair with a persisted destination."""
    orch = _orch()

    async def _connect(self: Any) -> bool:
        self._slack_connect_error = "invalid_auth"
        return False

    await _run(orch, _Recorder(), connect=_connect)

    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None


@pytest.mark.asyncio
async def test_owner_change_moves_the_dashboard_owner() -> None:
    """``DashboardState.owner_id`` is the owner-only handlers' subject.

    It is set once from the boot-time owner; a reconnect that hoists a new
    owner without moving it would leave the former owner authorised.
    """
    orch = _orch()
    assert orch.dashboard_state.owner_id == "U0FORMEROWNER"

    await _run(orch, _Recorder())

    assert orch.dashboard_state.owner_id == NEW_CREDS[CRED_OWNER_ID]


@pytest.mark.asyncio
async def test_cleared_owner_clears_the_dashboard_owner() -> None:
    creds = {k: v for k, v in NEW_CREDS.items() if k != CRED_OWNER_ID}
    orch = _orch(creds)

    await _run(orch, _Recorder())

    assert orch.dashboard_state.owner_id == ""
    assert orch.dashboard_state.slack_client is None


@pytest.mark.asyncio
async def test_connected_reconnect_runs_the_tracked_channel_probe() -> None:
    """Boot warns about a tracked private channel the install cannot read; a
    reconnect that connects is the same moment and must warn the same way."""
    orch = _orch()
    orch._tracking_channels = {"C0TRACKED"}
    probe = AsyncMock()

    with patch("kiro_crew.slack.gateway.warn_unreadable_tracked_channels", probe):
        await _run(orch, _Recorder())
        await asyncio.sleep(0)  # let the fire-and-forget task start

    probe.assert_awaited_once()
    args, kwargs = probe.await_args
    assert args[0] is orch.slack
    assert args[1] == {"C0TRACKED"}
    assert kwargs["notify"] is orch.dashboard_state.notify


@pytest.mark.asyncio
async def test_failed_reconnect_skips_the_tracked_channel_probe() -> None:
    orch = _orch()
    orch._tracking_channels = {"C0TRACKED"}
    probe = AsyncMock()

    async def _connect(self: Any) -> bool:
        self._slack_connect_error = "invalid_auth"
        return False

    with patch("kiro_crew.slack.gateway.warn_unreadable_tracked_channels", probe):
        await _run(orch, _Recorder(), connect=_connect)
        await asyncio.sleep(0)

    probe.assert_not_awaited()


# ── workspace switch: persisted Slack destinations ───────────────────────────


def _team(team_id: str):
    """Pin the workspace the handshake 'validated' (what ``auth.test`` named)."""
    return patch(
        "kiro_crew.slack.gateway.GatewayOrchestrator._slack_validated_team_id",
        staticmethod(lambda: team_id),
    )


def _recorded_team(orch: Any) -> str | None:
    """What the workspace record beside the session map says, None when absent."""
    path = orch._slack_workspace_state_path
    return json.loads(path.read_text())["team_id"] if path.exists() else None


def _pending_team(orch: Any) -> str | None:
    """The switch marker's target workspace, None when no switch is in flight."""
    path = orch._slack_workspace_state_path
    if not path.exists():
        return None
    pending = json.loads(path.read_text()).get("pending")
    return pending["team_id"] if pending else None


@pytest.mark.asyncio
async def test_workspace_switch_sweeps_persisted_links_before_publishing_the_client() -> None:
    """Credentials for ANOTHER workspace: every persisted Slack thread /
    channel link named a channel in the former workspace, so all of them go
    -- and are ON DISK -- before the dashboard is handed the new client: never
    a moment where a dashboard turn could combine the new client with an old
    destination, and never a crash window after publishing that a restart
    would fill with the swept rows restored under the new client."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    order: list[str] = []

    def _sweep() -> list[str]:
        order.append(f"sweep client={orch.dashboard_state.slack_client!r}")
        return ["dashboard:one", "slack:171.2"]

    async def _flush() -> None:
        order.append(
            f"flush client={orch.dashboard_state.slack_client!r} "
            f"recorded={_recorded_team(orch)!r} pending={_pending_team(orch)!r}"
        )

    orch.sessions.clear_all_slack_links.side_effect = _sweep
    orch.sessions.aflush.side_effect = _flush

    with _team("T0NEW"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    # Sweep, then the awaited flush, both while the mirror is still empty and
    # the record still names the former workspace (with the switch marker
    # already beside it); the identity moves last.
    assert order == [
        "sweep client=None",
        "flush client=None recorded='T0FORMER' pending='T0NEW'",
    ]
    assert _pending_team(orch) is None  # the adopting write dropped the marker
    assert orch.dashboard_state.slack_client is orch.slack  # published afterwards
    assert orch._slack_links_team_id == "T0NEW"
    assert _recorded_team(orch) == "T0NEW"


@pytest.mark.asyncio
async def test_switch_flush_failure_keeps_the_former_identity_and_publishes_nothing() -> None:
    """The sweep is durable or the switch did not happen: a flush that raises
    leaves the record on the former workspace, so the next connect sweeps
    again instead of adopting the new workspace over unflushed rows. And the
    socket the handshake built does NOT stay live behind the failure: the
    attempt is refused like an unwritable record, the socket retired, the
    dashboard mirror left empty -- not an exception that skips the retirement
    and answers 500 while the listener keeps running.

    And the rows the sweep removed from MEMORY are put back before the raise
    leaves: the map is already dirty with the deletion, so its next deferred
    write (or ``aclose`` at shutdown) would land it under an identity that was
    never adopted -- and if the operator then reverts to the former workspace's
    tokens, the next connect sees no switch and never restores. The restore is
    in memory only (this disk just refused a write); the map's deferred flush
    lands it."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    swept = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    orch.sessions.freeze_slack_links.return_value = swept
    orch.sessions.clear_all_slack_links.return_value = ["dashboard:one"]
    orch.sessions.restore_slack_links.return_value = ["dashboard:one"]
    orch.sessions.aflush.side_effect = OSError("disk full")
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"):
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unrecorded"}
    assert orch._slack_links_team_id == "T0FORMER"
    orch.sessions.restore_slack_links.assert_called_once_with(swept)
    orch.sessions.aflush.assert_awaited_once()  # the sweep's; the restore is not re-flushed here
    # The identity was not adopted; the switch marker stays for the next connect.
    assert _recorded_team(orch) == "T0FORMER"
    assert _pending_team(orch) == "T0NEW"
    recorder.client.close.assert_awaited_once()
    assert orch._socket_client is None
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_socket_connected is False
    assert orch.dashboard_state.slack_connect_error == "workspace_identity_unrecorded"


@pytest.mark.asyncio
async def test_first_recorded_identity_keeps_existing_links() -> None:
    """Nothing recorded yet but Slack destinations persist: every install that
    predates the record is in this state on its first boot after upgrading.
    The links are KEPT and the workspace only recorded -- sweeping here would
    end every live mirror on installs whose workspace never changed. The
    record then protects every switch from this boot on."""
    orch = _orch()

    with _team("T0NEW"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    orch.sessions.clear_all_slack_links.assert_not_called()
    orch.sessions.aflush.assert_not_awaited()
    assert orch.dashboard_state.slack_client is orch.slack
    assert orch._slack_links_team_id == "T0NEW"
    assert _recorded_team(orch) == "T0NEW"


@pytest.mark.asyncio
async def test_first_recorded_identity_on_a_fresh_install_is_quiet() -> None:
    """No record (a fresh install, or one upgrading onto the record): nothing
    is logged as cleared -- nothing was -- and the workspace is simply
    recorded so later connects have something to compare against."""
    orch = _orch()

    with _team("T0NEW"), patch("kiro_crew.slack.gateway.logger") as log:
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    assert orch._slack_links_team_id == "T0NEW"
    assert _recorded_team(orch) == "T0NEW"
    assert not [
        c for c in log.warning.call_args_list if "cleared" in str(c.args[0])
    ], "nothing was cleared, so nothing should say so"


@pytest.mark.asyncio
async def test_same_workspace_keeps_persisted_links() -> None:
    """A token rotation inside one workspace is not a switch: the links still
    name reachable channels and stripping them would silently end every mirror."""
    orch = _orch()
    orch._slack_links_team_id = "T0SAME"

    with _team("T0SAME"):
        await _run(orch, _Recorder())

    orch.sessions.clear_all_slack_links.assert_not_called()
    assert orch.dashboard_state.slack_client is orch.slack
    assert orch._slack_links_team_id == "T0SAME"
    assert _recorded_team(orch) is None  # unchanged identity, nothing rewritten


@pytest.mark.asyncio
async def test_unknown_identity_with_nothing_recorded_is_refused_too() -> None:
    """No workspace was ever recorded and the handshake names none either.
    Accepting that would leave the install with no identity ever recorded --
    a gate that keeps its own state and names no workspace is in this state
    for its whole life -- so the first switch of credentials could never be
    detected and the former workspace's destinations would be published under
    the new client. Refused as any unverified identity is; nothing is swept
    or recorded, and the badge names the reason."""
    orch = _orch()
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team(""):
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unverified"}
    recorder.client.close.assert_awaited_once()
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_connect_error == "workspace_identity_unverified"
    orch.sessions.clear_all_slack_links.assert_not_called()
    assert orch._slack_links_team_id == ""
    assert _recorded_team(orch) is None


@pytest.mark.asyncio
async def test_unverified_workspace_with_recorded_destinations_refuses_to_publish() -> None:
    """Destinations recorded under a KNOWN workspace, and the handshake could
    not say which workspace the new tokens reach (``auth.test`` failed; the
    default gate passes that open): a switch would slip through unswept, so
    the socket is torn down again, nothing is published, nothing is swept, the
    record stays with the former workspace, and the badge names the reason."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team(""):
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unverified"}
    recorder.client.close.assert_awaited_once()
    assert orch._socket_client is None
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_socket_connected is False
    assert orch.dashboard_state.slack_connect_error == "workspace_identity_unverified"
    orch.sessions.clear_all_slack_links.assert_not_called()
    orch.sessions.aflush.assert_not_awaited()
    assert orch._slack_links_team_id == "T0FORMER"
    assert _recorded_team(orch) is None


@pytest.mark.asyncio
async def test_unverified_socket_that_will_not_close_is_dropped_anyway() -> None:
    """The refused client is discarded either way: a close that raises is
    logged, not propagated, and the attempt still ends unpublished."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    recorder = _Recorder()
    recorder.client.close = AsyncMock(side_effect=RuntimeError("websocket gone"))

    with _team(""):
        result = await _run(orch, recorder)

    assert result["connected"] is False
    assert result["connect_error"] == "workspace_identity_unverified"
    assert orch._socket_client is None
    assert orch.slack is None


@pytest.mark.asyncio
async def test_boot_sweeps_when_the_record_names_another_workspace() -> None:
    """Boot binds through the same step as reconnect: credentials replaced
    while the gateway was down name another workspace than the record beside
    the session map, so the rows are swept and flushed and the record moves."""
    orch = _orch()
    orch._slack_workspace_state_path.write_text(json.dumps({"team_id": "T0FORMER"}))
    from kiro_crew.slack.gateway import _load_slack_links_team_id

    orch._slack_links_team_id = _load_slack_links_team_id(orch._slack_workspace_state_path)
    assert orch._slack_links_team_id == "T0FORMER"

    with _team("T0NEW"):
        assert await orch._adopt_slack_workspace(source="boot") == ""

    orch.sessions.clear_all_slack_links.assert_called_once_with()
    orch.sessions.aflush.assert_awaited_once()
    # Swept and marked, not yet adopted: the switch waits for the socket.
    assert orch._slack_workspace_switch is not None
    assert (orch._slack_links_team_id, _recorded_team(orch), _pending_team(orch)) == (
        "T0FORMER",
        "T0FORMER",
        "T0NEW",
    )
    assert await orch._commit_slack_workspace_switch(source="boot") == ""
    assert orch._slack_workspace_switch is None
    assert (orch._slack_links_team_id, _recorded_team(orch), _pending_team(orch)) == (
        "T0NEW",
        "T0NEW",
        None,
    )


def test_missing_record_reads_as_nothing_recorded(tmp_path: Any) -> None:
    from kiro_crew.slack.gateway import _load_slack_links_team_id, _store_slack_links_team_id

    path = tmp_path / "slack_workspace.json"
    assert _load_slack_links_team_id(path) == ""
    _store_slack_links_team_id(path, "T0NEW")
    assert _load_slack_links_team_id(path) == "T0NEW"


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"team_id": 7}', '{"other": "x"}'])
def test_damaged_record_reads_as_damaged_not_absent(tmp_path: Any, content: str) -> None:
    """A record that exists but does not parse is None, never "": taken for
    absent, the next bind would record whatever workspace the handshake names
    as the first and skip the switch check it exists for."""
    from kiro_crew.slack.gateway import _load_slack_links_team_id

    path = tmp_path / "slack_workspace.json"
    path.write_text(content)
    assert _load_slack_links_team_id(path) is None


def test_unreadable_record_reads_as_damaged(tmp_path: Any) -> None:
    from kiro_crew.slack.gateway import _load_slack_links_team_id

    path = tmp_path / "slack_workspace.json"
    path.mkdir()  # exists, but read_text raises IsADirectoryError (an OSError)
    assert _load_slack_links_team_id(path) is None


@pytest.mark.asyncio
async def test_damaged_record_refuses_to_publish_and_sweeps_nothing() -> None:
    """Boot found a record it could not read: the bind re-reads it, still
    cannot, and refuses -- socket retired, nothing published, nothing swept,
    nothing recorded over the damaged file. A damaged record is not an absent
    one: adopting the validated workspace as the first would skip the switch
    check for good, on the one boot where it may matter most."""
    orch = _orch()
    orch._slack_workspace_state_path.write_text("not json")
    orch._slack_workspace_record_damaged = True
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"):
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_record_unreadable"}
    orch.sessions.clear_all_slack_links.assert_not_called()
    assert orch._slack_workspace_state_path.read_text() == "not json"
    assert orch._slack_links_team_id == ""
    assert orch._slack_workspace_record_damaged is True
    recorder.client.close.assert_awaited_once()
    assert orch._socket_client is None
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_connect_error == "workspace_record_unreadable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repair", "expect_sweep", "recorded_after"),
    [
        ("remove", False, "T0NEW"),  # removed: nothing recorded, first record, links kept
        ("same", False, "T0SAME"),  # repaired to the same workspace: no switch
        ("former", True, "T0NEW"),  # repaired to the former one: the switch is caught
    ],
)
async def test_repaired_record_recovers_on_the_next_reconnect_without_a_restart(
    repair: str, expect_sweep: bool, recorded_after: str
) -> None:
    """The damaged reading is not cached: once the operator repaired or removed
    the file, the next Reconnect re-reads it and binds through the ordinary
    path -- and what it then does is decided by what the repaired record says."""
    orch = _orch()
    path = orch._slack_workspace_state_path
    path.write_text("not json")
    orch._slack_workspace_record_damaged = True
    validated = "T0SAME" if repair == "same" else "T0NEW"

    with _team(validated):
        first = await _run(orch, _Recorder())
        if repair == "remove":
            path.unlink()
        else:
            path.write_text(json.dumps({"team_id": "T0SAME" if repair == "same" else "T0FORMER"}))
        second = await _run(orch, _Recorder())

    assert first["connect_error"] == "workspace_record_unreadable"
    assert second["connected"] is True
    assert orch._slack_workspace_record_damaged is False
    assert orch._slack_links_team_id == recorded_after
    assert _recorded_team(orch) == recorded_after
    assert orch.sessions.clear_all_slack_links.called is expect_sweep
    assert orch.dashboard_state.slack_client is orch.slack


def test_orchestrator_boot_defers_the_record_read_to_the_first_bind() -> None:
    """Source pin: the record is NOT read on the boot path -- it can carry a
    switch marker whose size scales with the link count -- and the first bind
    reads it off-loop, recording the loader's None as ``damaged`` (and "" as
    the identity) instead of collapsing both into "nothing recorded"."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator.__init__)
    assert "_load_slack_workspace_record(" not in src
    assert 'self._slack_links_team_id: str = ""' in src
    assert "self._slack_workspace_record_loaded: bool = False" in src
    adopt = inspect.getsource(GatewayOrchestrator._adopt_slack_workspace)
    assert (
        "if not self._slack_workspace_record_loaded or self._slack_workspace_record_damaged:"
        in adopt
    )
    read = adopt.index("_load_slack_workspace_record, self._slack_workspace_state_path")
    assert "asyncio.to_thread(" in adopt[read - 60 : read]


@pytest.mark.asyncio
async def test_failed_connect_after_the_bind_undoes_the_switch() -> None:
    """The workspace is bound BEFORE the socket connects (the handshake --
    ``auth.test`` through ``init_socket_mode`` -- already named it), but the
    switch is made DURABLE only once the socket connected. A bot token that
    validates to another workspace beside an app token that cannot open a
    socket would otherwise adopt the new identity and clear the marker -- the
    only copy an undo restores from -- with nothing published and the former
    workspace's mirrors gone for good. So a failed connect undoes the switch:
    the map's rows and the cron destinations come back, the dashboard's slot
    fields and thread index are rebuilt from the restored map (never from a
    copy of the fields, so a channel-born slot's self-reference is not
    indexed), the former identity stays recorded, the marker is cleared, the
    freeze ends, and the client is withheld."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    rows = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    orch.sessions.freeze_slack_links.return_value = rows
    orch.cron_svc = _cron_svc(("j1", "C0CHAN", "171.1"))
    order: list[str] = []
    orch.sessions.clear_all_slack_links.side_effect = lambda: order.append("sweep") or []
    orch.sessions.restore_slack_links.side_effect = lambda r: order.append(
        f"restore {[x['key'] for x in r]}"
    ) or ["dashboard:one"]
    orch.sessions.thaw_slack_links.side_effect = lambda: order.append("thaw")
    orch.dashboard_state.forget_slack_links.side_effect = lambda: order.append("forget") or [
        "chat-1"
    ]
    orch.dashboard_state.rehydrate_slack_links.side_effect = lambda: order.append(
        "dashboard rehydrate"
    ) or ["chat-1"]

    async def _connect(self: Any) -> bool:
        order.append(f"connect frozen_pending={_pending_team(orch)!r}")
        self._slack_connect_error = "invalid_auth"
        return False

    with _team("T0NEW"):
        result = await _run(orch, _Recorder(), connect=_connect)

    assert result == {"connected": False, "connect_error": "invalid_auth"}
    assert order == [
        "sweep",
        "forget",
        "connect frozen_pending='T0NEW'",
        "restore ['dashboard:one']",
        "dashboard rehydrate",
        "thaw",
    ]
    calls = [c.args + (c.kwargs,) for c in orch.cron_svc.update_job_async.await_args_list]
    assert calls == [("j1", _clear("C0CHAN", "171.1")), ("j1", _put_back("C0CHAN", "171.1"))]
    assert orch._slack_links_team_id == "T0FORMER"
    assert (_recorded_team(orch), _pending_team(orch)) == ("T0FORMER", None)
    assert orch._slack_workspace_switch is None
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None


@pytest.mark.asyncio
async def test_an_undo_that_cannot_clear_the_marker_leaves_it_for_the_next_connect() -> None:
    """The undo's record write fails: the rows are back, the freeze ends, and
    the marker stays -- the next connect finishes or undoes the switch from
    it, exactly as after a crash. The failed connect is still what is reported."""
    from kiro_crew.slack import gateway

    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.sessions.freeze_slack_links.return_value = [
        {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}
    ]
    real = gateway._store_slack_links_team_id
    stores: list[str] = []

    def _store(path: Path, team_id: str, *, pending: Any = None) -> None:
        stores.append("marker" if pending else f"record {team_id}")
        if pending is None:
            raise OSError(28, "No space left on device")
        real(path, team_id, pending=pending)

    async def _connect(self: Any) -> bool:
        self._slack_connect_error = "invalid_auth"
        return False

    with _team("T0NEW"), patch.object(gateway, "_store_slack_links_team_id", _store):
        result = await _run(orch, _Recorder(), connect=_connect)

    assert result == {"connected": False, "connect_error": "invalid_auth"}
    assert stores == ["marker", "record T0FORMER"]
    orch.sessions.restore_slack_links.assert_called_once()
    orch.sessions.thaw_slack_links.assert_called_once_with()
    assert (_recorded_team(orch), _pending_team(orch)) == ("T0FORMER", "T0NEW")
    assert orch._slack_workspace_switch is None


@pytest.mark.asyncio
async def test_a_switch_is_committed_only_after_the_socket_connected() -> None:
    """Order pin for the whole admitted connect: marker and sweep, connect,
    THEN the adopting record write, then the thaw, then publication."""
    from kiro_crew.slack import gateway

    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.sessions.freeze_slack_links.return_value = [
        {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}
    ]
    order: list[str] = []
    orch.sessions.clear_all_slack_links.side_effect = lambda: order.append("sweep") or []
    orch.sessions.thaw_slack_links.side_effect = lambda: order.append("thaw")
    real = gateway._store_slack_links_team_id

    def _store(path: Path, team_id: str, *, pending: Any = None) -> None:
        order.append("marker" if pending else f"adopt {team_id}")
        real(path, team_id, pending=pending)

    async def _connect(self: Any) -> bool:
        order.append("connect")
        return True

    with _team("T0NEW"), patch.object(gateway, "_store_slack_links_team_id", _store):
        result = await _run(orch, _Recorder(), connect=_connect)

    assert result["connected"] is True
    assert order == ["marker", "sweep", "connect", "adopt T0NEW", "thaw"]
    assert (orch._slack_links_team_id, _recorded_team(orch), _pending_team(orch)) == (
        "T0NEW",
        "T0NEW",
        None,
    )
    assert orch._slack_workspace_switch is None


@pytest.mark.asyncio
async def test_a_refused_bind_never_connects_the_socket() -> None:
    """The admission gate is the ORDER: the socket connects only once the
    workspace it validated to is adopted, so no envelope can reach the
    listener from a workspace whose binding is still running -- or refused.
    Here the record is damaged, the bind refuses, and the socket the handshake
    built is torn down without ever having connected."""
    orch = _orch()
    orch._slack_workspace_record_loaded = False
    orch._slack_workspace_state_path.write_text("{not json")
    connects: list[str] = []

    async def _connect(self: Any) -> bool:
        connects.append("connect")
        return True

    with _team("T0NEW"):
        result = await _run(orch, _Recorder(), connect=_connect)

    assert result == {"connected": False, "connect_error": "workspace_record_unreadable"}
    assert connects == []
    assert orch._socket_client is None
    assert orch.slack is None


def _gate_context(gate: Any):
    """Pin the enterprise gate the orchestrator reads the identity from."""
    from types import SimpleNamespace

    return patch(
        "kiro_crew.slack.gateway.current_context",
        lambda: SimpleNamespace(slack_gate=gate),
    )


def test_validated_team_id_reads_the_default_gate_which_answers_from_the_cache() -> None:
    """The default gate validates through ``slack.enterprise`` and reports the
    id that module cached; read through the gate, the answer is the same."""
    from kiro_crew.platform.defaults import DefaultSlackEnterpriseGate
    from kiro_crew.slack import enterprise
    from kiro_crew.slack.gateway import GatewayOrchestrator

    with patch.object(enterprise, "_validated_team_id", "T0CACHED"):
        assert enterprise.validated_team_id() == "T0CACHED"
        assert DefaultSlackEnterpriseGate().validated_team_id() == "T0CACHED"
        with _gate_context(DefaultSlackEnterpriseGate()):
            assert GatewayOrchestrator._slack_validated_team_id() == "T0CACHED"


def test_validated_team_id_is_the_active_gates_not_the_module_caches() -> None:
    """An edition's gate that keeps its own validation state never writes the
    ``slack.enterprise`` cache; the identity it validated is what it REPORTS,
    and that -- not the cache's "" -- is the workspace the destinations bind
    to. A gate that names none, a gate without the method, and a gate whose
    answer is not a string all read as no identity."""
    from types import SimpleNamespace

    from kiro_crew.slack import enterprise
    from kiro_crew.slack.gateway import GatewayOrchestrator

    with patch.object(enterprise, "_validated_team_id", ""):
        with _gate_context(SimpleNamespace(validated_team_id=lambda: "T0EDITION")):
            assert GatewayOrchestrator._slack_validated_team_id() == "T0EDITION"
        with _gate_context(SimpleNamespace(validated_team_id=lambda: "")):
            assert GatewayOrchestrator._slack_validated_team_id() == ""
        with _gate_context(SimpleNamespace(validate_enterprise=lambda *a, **k: True)):
            assert GatewayOrchestrator._slack_validated_team_id() == ""
        with _gate_context(SimpleNamespace(validated_team_id=lambda: None)):
            assert GatewayOrchestrator._slack_validated_team_id() == ""
    # And the cache naming a workspace does not leak past a gate that names none.
    with patch.object(enterprise, "_validated_team_id", "T0CACHED"):
        with _gate_context(SimpleNamespace(validated_team_id=lambda: "")):
            assert GatewayOrchestrator._slack_validated_team_id() == ""


def test_slack_enterprise_gate_protocol_names_validated_team_id() -> None:
    """The identity read is part of the gate CONTRACT, so an edition's gate
    knows to report it; the default gate implements it."""
    from kiro_crew.platform.defaults import DefaultSlackEnterpriseGate
    from kiro_crew.platform.interfaces import SlackEnterpriseGate

    assert callable(getattr(SlackEnterpriseGate, "validated_team_id", None))
    assert callable(DefaultSlackEnterpriseGate.validated_team_id)


def test_stale_link_generation_is_refused(tmp_path: Any) -> None:
    """The in-flight-turn fence: a Slack turn that captured the generation
    before a workspace switch cannot re-persist its former-workspace thread
    after the sweep. A fresh capture writes; an unfenced write (no
    generation) is the dashboard's and always writes."""
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("slack:171.1", "sid-1")
        smap.set_slack_link("slack:171.1", "171.1", "C0FORMER")
        at_receipt = smap.slack_links_generation()

        # The switch lands while the turn is suspended.
        assert smap.clear_all_slack_links() == ["slack:171.1"]
        assert smap.slack_links_generation() == at_receipt + 1

        # The resumed turn presents its receipt generation: refused, nothing
        # written, nothing indexed.
        smap.set_slack_link("slack:171.1", "171.1", "C0FORMER", generation=at_receipt)
        assert smap.get_slack_link("slack:171.1") == (None, None)
        assert smap.get_session_for_thread("171.1") is None
        assert "slack_link_nonce" not in smap._data["slack:171.1"]

        # A turn received AFTER the switch writes; so does an unfenced writer.
        smap.set_slack_link(
            "slack:171.2", "171.2", "C0NEW", generation=smap.slack_links_generation()
        )
        assert smap.get_slack_link("slack:171.2") == ("171.2", "C0NEW")
        smap.set_slack_link("dashboard:one", "171.3", "C0NEW")
        assert smap.get_slack_link("dashboard:one") == ("171.3", "C0NEW")

        # The sweep bumps even with nothing to clear: the fence is about the
        # workspace having changed, not about how many rows named it.
        before = smap.slack_links_generation()
        smap.clear_all_slack_links()
        assert smap.slack_links_generation() == before + 1
        smap.set_slack_link("slack:171.2", "171.2", "C0NEW", generation=before)
        assert smap.get_slack_link("slack:171.2") == (None, None)


@pytest.mark.asyncio
async def test_manager_threads_the_generation_to_every_link_writer() -> None:
    """``SessionManager.set_slack_link`` / ``set_channel`` hand the generation
    to the map, and ``slack_links_generation`` reads it from there."""
    from kiro_crew.session import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._session_map = MagicMock()
    mgr._session_map.slack_links_generation.return_value = 4
    mgr._session_map.get_slack_link.return_value = ("171.1", None)

    assert mgr.slack_links_generation() == 4
    mgr.set_slack_link("slack:171.1", "171.1", "C1", generation=3)
    mgr._session_map.set_slack_link.assert_called_with("slack:171.1", "171.1", "C1", generation=3)
    await mgr.set_channel("slack:171.1", "C1", generation=3)
    mgr._session_map.set_slack_link.assert_called_with("slack:171.1", "171.1", "C1", generation=3)
    mgr.set_slack_link("dashboard:one", "171.2", "C1")
    mgr._session_map.set_slack_link.assert_called_with(
        "dashboard:one", "171.2", "C1", generation=None
    )


def test_clear_all_slack_links_sweeps_only_slack_destinations(tmp_path: Any) -> None:
    """The map sweep: every row that names a Slack thread goes (fields,
    reverse index, mute marker), a non-Slack mirror and a legacy namespaced
    ``slack_channel_id`` with no thread -- ``set_channel`` bookkeeping, not a
    destination -- are left alone, and the result is on disk."""
    from kiro_crew.messaging.link import ChannelLink
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("dashboard:one", "sid-1")
        smap.set_slack_link("dashboard:one", "171.1", "C0FORMER")
        smap.set_slack_paused("dashboard:one", True)
        smap.set("slack:171.2", "sid-2")
        smap.set_slack_link("slack:171.2", "171.2", "C0FORMER")
        smap.set("dashboard:tg", "sid-3")
        smap.set_mirror_link("dashboard:tg", ChannelLink("telegram", channel_id="99"))
        smap.set("discord:55", "sid-4")
        smap._data["discord:55"]["slack_channel_id"] = "discord:55"  # legacy bucket, no thread

        cleared = smap.clear_all_slack_links()

        assert sorted(cleared) == ["dashboard:one", "slack:171.2"]
        assert smap.get_slack_link("dashboard:one") == (None, None)
        assert smap.get_slack_link("slack:171.2") == (None, None)
        assert smap.get_session_for_thread("171.1") is None
        assert smap.get_session_for_thread("171.2") is None
        assert smap.is_slack_paused("dashboard:one") is False
        assert smap.get_mirror_link("dashboard:tg") == ChannelLink("telegram", channel_id="99")
        assert smap._data["discord:55"]["slack_channel_id"] == "discord:55"
        assert smap._data["dashboard:one"]["sid"] == "sid-1"  # the sessions themselves survive

        reloaded = SessionMap()
        assert reloaded.get_slack_link("dashboard:one") == (None, None)
        assert reloaded.get_session_for_thread("171.2") is None
        assert reloaded.get_mirror_link("dashboard:tg") == ChannelLink("telegram", channel_id="99")


def test_snapshot_then_restore_undoes_the_sweep_on_disk(tmp_path: Any) -> None:
    """The undo of a sweep whose workspace switch did not complete: the copy
    holds exactly the rows the sweep removes (thread, channel, nonce, mute --
    not the non-Slack mirror, not the legacy bucket), and restoring it brings
    the fields, the reverse index and the mute back, on disk, without moving
    the link generation back."""
    from kiro_crew.messaging.link import ChannelLink
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("dashboard:one", "sid-1")
        smap.set_slack_link("dashboard:one", "171.1", "C0FORMER")
        smap.set_slack_paused("dashboard:one", True)
        smap.set("slack:171.2", "sid-2")
        smap.set_slack_link("slack:171.2", "171.2", "C0FORMER")
        smap.set("dashboard:tg", "sid-3")
        smap.set_mirror_link("dashboard:tg", ChannelLink("telegram", channel_id="99"))
        smap.set("discord:55", "sid-4")
        smap._data["discord:55"]["slack_channel_id"] = "discord:55"
        nonce = smap.slack_link_nonce("dashboard:one")
        generation = smap.slack_links_generation()

        rows = smap.snapshot_slack_links()
        assert sorted(r["key"] for r in rows) == ["dashboard:one", "slack:171.2"]
        assert sorted(smap.clear_all_slack_links()) == ["dashboard:one", "slack:171.2"]
        assert smap.get_slack_link("dashboard:one") == (None, None)

        restored = smap.restore_slack_links(rows)

        assert sorted(restored) == ["dashboard:one", "slack:171.2"]
        assert smap.get_slack_link("dashboard:one") == ("171.1", "C0FORMER")
        assert smap.get_slack_link("slack:171.2") == ("171.2", "C0FORMER")
        assert smap.get_session_for_thread("171.1") == "dashboard:one"
        assert smap.get_session_for_thread("171.2") == "slack:171.2"
        assert smap.is_slack_paused("dashboard:one") is True
        assert smap.slack_link_nonce("dashboard:one") == nonce
        assert smap.slack_links_generation() == generation + 1  # the fence is not rewound
        assert smap._data["discord:55"]["slack_channel_id"] == "discord:55"

        reloaded = SessionMap()
        assert reloaded.get_slack_link("dashboard:one") == ("171.1", "C0FORMER")
        assert reloaded.get_session_for_thread("171.2") == "slack:171.2"
        assert reloaded.is_slack_paused("dashboard:one") is True


def test_restore_announces_each_binding_to_the_bind_listener(tmp_path: Any) -> None:
    """A restored row is a binding COMMITTED again after the sweep removed it,
    so the class recorder hears one bind per restored key -- after the save,
    and none for rows the restore skipped. Without it the sweep's unbind would
    be the last thing on record for a link that is live."""
    from kiro_crew import session_map as session_map_module
    from kiro_crew.session_map import SessionMap

    heard: list[str] = []
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("dashboard:one", "sid-1")
        smap.set_slack_link("dashboard:one", "171.1", "C0FORMER")
        smap.set("dashboard:gone", "sid-2")
        smap.set_slack_link("dashboard:gone", "171.9", "C0FORMER")
        rows = smap.snapshot_slack_links()
        smap.clear_all_slack_links()
        smap.delete("dashboard:gone")

        def listener(key: str) -> None:
            heard.append(key)
            # Announced after the commit: the binding is already readable.
            assert smap.get_slack_link(key) == ("171.1", "C0FORMER")

        session_map_module.set_bind_listener(listener)
        try:
            assert smap.restore_slack_links(rows) == ["dashboard:one"]
        finally:
            session_map_module.set_bind_listener(None)

    assert heard == ["dashboard:one"]


def test_restore_skips_gone_sessions_and_keeps_newer_links(tmp_path: Any) -> None:
    """Between the sweep's flush and the restore the loop ran: a session the
    copy names may have been deleted (stays gone) or linked to another thread
    (keeps the newer link). Neither row is restored; the rest are."""
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        for key, ts in (("a", "1.1"), ("b", "1.2"), ("c", "1.3")):
            smap.set(f"dashboard:{key}", f"sid-{key}")
            smap.set_slack_link(f"dashboard:{key}", ts, "C0FORMER")
        rows = smap.snapshot_slack_links()
        smap.clear_all_slack_links()
        smap.delete("dashboard:a")
        smap.set_slack_link("dashboard:b", "9.9", "C0FORMER")

        restored = smap.restore_slack_links(rows)

        assert restored == ["dashboard:c"]
        assert smap.get("dashboard:a") is None
        assert smap.get_slack_link("dashboard:b") == ("9.9", "C0FORMER")
        assert smap.get_session_for_thread("1.2") is None
        assert smap.get_slack_link("dashboard:c") == ("1.3", "C0FORMER")


def test_restore_of_nothing_writes_nothing(tmp_path: Any) -> None:
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        with patch.object(smap, "_save") as save:
            assert smap.restore_slack_links([]) == []
            assert smap.restore_slack_links([{"key": "dashboard:x", "slack_thread_ts": ""}]) == []
        save.assert_not_called()


def test_manager_passes_snapshot_and_restore_through() -> None:
    from kiro_crew.session import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._session_map = MagicMock()
    mgr._session_map.snapshot_slack_links.return_value = [{"key": "k"}]
    mgr._session_map.restore_slack_links.return_value = ["k"]

    assert mgr.snapshot_slack_links() == [{"key": "k"}]
    assert mgr.restore_slack_links([{"key": "k"}]) == ["k"]
    mgr._session_map.restore_slack_links.assert_called_once_with([{"key": "k"}])


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_handshake() -> None:
    """A double-click must not race two Socket Mode handshakes."""
    orch = _orch()
    gate = asyncio.Event()
    rec = _Recorder(hold=gate)
    p_init, p_client, p_connect = _patches(rec)
    with p_init, p_client, p_connect:
        first = asyncio.create_task(orch.reconnect_slack())
        await asyncio.sleep(0)  # first attempt is now parked inside init_socket_mode
        second = asyncio.create_task(orch.reconnect_slack())
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(first, second)

    assert len(rec.calls) == 1
    assert results[0] == results[1] == {"connected": True, "connect_error": ""}
    assert orch._cfg.load_credentials.call_count == 1
    # The slot is released, so a later click starts a fresh attempt.
    assert orch._slack_reconnect_task is None


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_the_shared_attempt() -> None:
    orch = _orch()
    gate = asyncio.Event()
    rec = _Recorder(hold=gate)
    p_init, p_client, p_connect = _patches(rec)
    with p_init, p_client, p_connect:
        first = asyncio.create_task(orch.reconnect_slack())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        gate.set()
        second = await orch.reconnect_slack()

    assert len(rec.calls) == 1  # the in-flight attempt finished and was reused
    assert second["connected"] is True


def test_gateway_wires_the_callback_onto_dashboard_state() -> None:
    """Source pin: ``run`` publishes reconnect_slack for the route (after boot's
    own bind -- see ``test_reconnect_route_is_wired_only_after_boots_own_bind``)."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator.run)
    assert "self.dashboard_state._slack_reconnect = self.reconnect_slack" in src


# ── route ────────────────────────────────────────────────────────────────────


def _request(state: Any) -> web.Request:
    app = web.Application()
    app["state"] = state
    return make_mocked_request("POST", "/api/slack/reconnect", app=app)


@pytest.mark.asyncio
async def test_route_answers_the_config_get_shape() -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    state = MagicMock()
    state._slack_reconnect = AsyncMock(
        return_value={"connected": False, "connect_error": "invalid_auth"}
    )
    sel = MagicMock()
    with (
        patch.object(mod, "is_direct_local_request", lambda req: True),
        patch.object(mod, "_sel", lambda: sel),
    ):
        resp = await mod.api_slack_reconnect(_request(state))

    assert resp.status == 200
    assert resp.text is not None
    import json

    assert json.loads(resp.text) == {"connected": False, "connect_error": "invalid_auth"}
    state._slack_reconnect.assert_awaited_once_with()
    kw = sel.log_api_access.call_args.kwargs
    assert kw["operation"] == "slack.reconnect"
    assert kw["outcome"] == "failed"
    assert kw["error"] == "invalid_auth"


@pytest.mark.asyncio
async def test_route_denies_remote_sessions_like_the_put() -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    state = MagicMock()
    state._slack_reconnect = AsyncMock()
    with (
        patch.object(mod, "is_direct_local_request", lambda req: False),
        patch.object(mod, "_sel", lambda: MagicMock()),
    ):
        resp = await mod.api_slack_reconnect(_request(state))

    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "remote_read_only"
    state._slack_reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_is_503_when_no_gateway_owns_a_socket() -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    state = MagicMock()
    state._slack_reconnect = None
    with (
        patch.object(mod, "is_direct_local_request", lambda req: True),
        patch.object(mod, "_sel", lambda: MagicMock()),
    ):
        resp = await mod.api_slack_reconnect(_request(state))

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "slack_reconnect_unavailable"


@pytest.mark.asyncio
async def test_route_is_500_when_the_store_cannot_be_read() -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    state = MagicMock()
    state._slack_reconnect = AsyncMock(side_effect=OSError("store unreadable"))
    sel = MagicMock()
    with (
        patch.object(mod, "is_direct_local_request", lambda req: True),
        patch.object(mod, "_sel", lambda: sel),
    ):
        resp = await mod.api_slack_reconnect(_request(state))

    assert resp.status == 500
    assert json.loads(resp.body)["code"] == "credential_store_unreadable"
    assert sel.log_api_access.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_two_concurrent_requests_share_one_attempt_through_the_route() -> None:
    """The route must not serialize concurrent clicks into two full attempts.

    A lock held by the HTTP handler would make the second click wait for the
    first attempt to finish and then run a fresh handshake, tearing down the
    socket the first had just established. Both requests have to reach the
    orchestrator while the first attempt is still running, so its coalescing
    (``test_concurrent_callers_share_one_handshake``) can fold them.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    in_flight = 0
    peak = 0
    release = asyncio.Event()

    async def slow_reconnect() -> dict[str, object]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await release.wait()
            return {"connected": True, "connect_error": ""}
        finally:
            in_flight -= 1

    state = MagicMock()
    state._slack_reconnect = slow_reconnect
    with (
        patch.object(mod, "is_direct_local_request", lambda req: True),
        patch.object(mod, "_sel", lambda: MagicMock()),
    ):
        first = asyncio.create_task(mod.api_slack_reconnect(_request(state)))
        second = asyncio.create_task(mod.api_slack_reconnect(_request(state)))
        for _ in range(5):
            await asyncio.sleep(0)
        assert peak == 2  # both reached the orchestrator before either finished
        release.set()
        r1, r2 = await asyncio.wait_for(asyncio.gather(first, second), 5)

    assert r1.status == r2.status == 200


@pytest.mark.asyncio
async def test_attempt_waits_for_the_config_lock_the_save_holds() -> None:
    """Reconnect must not read credentials while a save is writing them.

    Outside ``_get_config_lock()`` a Reconnect that lands mid-save snapshots
    the credentials the operator is replacing and hoists them AFTER the save
    commits: the former owner stays authorized on the live socket. The shared
    attempt takes the lock the PUT holds, so the read sees only a completed
    save.
    """
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    orch = _orch()
    rec = _Recorder()
    p_init, p_client, p_connect = _patches(rec)
    with p_init, p_client, p_connect:
        async with _get_config_lock():  # a save in flight
            attempt = asyncio.create_task(orch.reconnect_slack())
            for _ in range(5):
                await asyncio.sleep(0)
            orch._cfg.load_credentials.assert_not_called()  # blocked behind the save
        result = await asyncio.wait_for(attempt, 5)

    assert result == {"connected": True, "connect_error": ""}
    orch._cfg.load_credentials.assert_called_once_with()


@pytest.mark.asyncio
async def test_shared_attempt_keeps_the_lock_until_it_ends() -> None:
    """A caller that gives up mid-handshake must not release the lock early.

    The attempt is shielded from the caller's cancel and keeps running; if the
    lock followed the caller, a save could commit under that still-running
    read -- the same window.
    """
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    orch = _orch()
    gate = asyncio.Event()
    rec = _Recorder(hold=gate)
    p_init, p_client, p_connect = _patches(rec)
    with p_init, p_client, p_connect:
        caller = asyncio.create_task(orch.reconnect_slack())
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(rec.calls) == 1  # parked inside init_socket_mode
        caller.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert _get_config_lock().locked()  # still held while the attempt runs
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 5)
        shared = orch._slack_reconnect_task
        if shared is not None:
            await asyncio.wait_for(shared, 5)

    assert not _get_config_lock().locked()


def test_route_is_registered_as_post_beside_the_put() -> None:
    from kiro_crew.dashboard import handlers
    from kiro_crew.dashboard.routes import messaging as routes

    app = web.Application()
    routes.register(app)
    reconnect = [
        r
        for r in app.router.routes()
        if r.resource is not None and r.resource.canonical == "/api/slack/reconnect"
    ]
    assert [r.method for r in reconnect] == ["POST"]
    assert reconnect[0].handler is handlers.api_slack_reconnect


# ── Queued turns keep the client that received them across a reconnect ──


def _queue_orch(live_client: Any) -> MagicMock:
    """Orchestrator double for ``_dispatch_queued`` / ``_route_message``.

    ``orch.slack`` is whatever a reconnect made it; the test decides what the
    queue entry remembers. Mirrors test_message_queue's harness: transport off
    so the native ``handle_message`` patch is the one that runs.
    """
    from kiro_crew.config.loader import ACTIVATION_ALWAYS, KiroCrewConfig, MessagingConfig

    orch = MagicMock()
    orch._cfg = KiroCrewConfig(
        slack_channels={},
        slack_dm_activation=ACTIVATION_ALWAYS,
        messaging=MessagingConfig(use_transport=False),
    )
    orch.channel_history = MagicMock()
    orch.slack = live_client
    orch.sessions = MagicMock()
    orch.sessions.is_busy.return_value = False
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.sessions.cancel_queued = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.clear_queue = MagicMock()
    orch.sessions.has_session = MagicMock(return_value=False)
    orch.ctx_builder = None
    orch.cron_svc = None
    orch.conv_log = None
    orch.consolidator = None
    orch.subagent_mgr = None
    orch.task_runner = None
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


@pytest.mark.asyncio
async def test_queued_turn_answers_through_the_client_that_received_it() -> None:
    """A reconnect to workspace B between enqueue and drain must not carry a
    workspace-A turn onto B's client: the reaction removal and the turn itself
    both use the client the queue entry bound at enqueue."""
    from kiro_crew.slack import events

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _queue_orch(workspace_b)  # the reconnect already happened
    kwargs = {"channel": "C_A", "thread_ts": "1.0", "slack_client": workspace_a}

    with patch.object(events, "handle_message", new_callable=AsyncMock) as hm:
        await events._dispatch_queued(orch, "1.0", "2.0", "follow up", kwargs)

    workspace_a.remove_reaction.assert_awaited_once_with("C_A", "2.0", "hourglass_flowing_sand")
    workspace_b.remove_reaction.assert_not_awaited()
    assert hm.await_args.args[0] is workspace_a


@pytest.mark.asyncio
async def test_queue_entry_without_a_bound_client_uses_the_live_one() -> None:
    """Entries queued before the key existed keep working."""
    from kiro_crew.slack import events

    live = AsyncMock(name="live")
    orch = _queue_orch(live)

    with patch.object(events, "handle_message", new_callable=AsyncMock) as hm:
        await events._dispatch_queued(orch, "1.0", "2.0", "follow up", {"channel": "C1"})

    live.remove_reaction.assert_awaited_once()
    assert hm.await_args.args[0] is live


@pytest.mark.asyncio
async def test_every_enqueue_site_binds_the_receiving_client() -> None:
    """All three queue writers in ``_route_message`` record ``orch.slack`` as
    it was when the message arrived: the session queue (busy task), the
    pre-session pending queue, and the semaphore-locked session queue."""
    from kiro_crew.slack.events import SeenCache, _route_message

    received_by = AsyncMock(name="received_by")
    patches = [
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock),
    ]
    for p in patches:
        p.start()
    try:
        # 1. Busy task, session object exists -> sessions.enqueue.
        orch = _queue_orch(received_by)
        orch._session_tasks["ts1"] = MagicMock()
        orch.sessions.enqueue.return_value = True
        event = {
            "user": "U1",
            "text": "q",
            "ts": "ts1",
            "channel": "D1",
            "channel_type": "im",
            "team": "T1",
        }
        await _route_message(orch, event, SeenCache(), is_mention=True)
        assert orch.sessions.enqueue.call_args.kwargs["slack_client"] is received_by

        # 2. Busy task, no session object yet -> orch._pending_queue.
        orch = _queue_orch(received_by)
        orch._session_tasks["thr"] = MagicMock()
        orch.sessions.enqueue.return_value = False
        event = {
            "user": "U1",
            "text": "q",
            "ts": "ts2",
            "thread_ts": "thr",
            "channel": "C1",
            "channel_type": "channel",
            "team": "T1",
        }
        await _route_message(orch, event, SeenCache(), is_mention=True)
        _ts, _text, kw = orch._pending_queue["thr"][0]
        assert kw["slack_client"] is received_by

        # 3. No task, but the session semaphore is locked -> sessions.enqueue.
        orch = _queue_orch(received_by)
        orch.sessions.enqueue.return_value = True
        event = {
            "user": "U1",
            "text": "q",
            "ts": "ts3",
            "channel": "D1",
            "channel_type": "im",
            "team": "T1",
        }
        await _route_message(orch, event, SeenCache(), is_mention=True)
        assert orch.sessions.enqueue.call_args.kwargs["slack_client"] is received_by
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_queue_binds_the_socket_client_even_when_routing_suspends() -> None:
    """``_route_message`` suspends before it enqueues (governance gate,
    ``users.info``, file downloads); a Reconnect in that gap swaps
    ``orch.slack`` to another workspace. The queue entry must carry the client
    of the socket that received the event, which the listener passes in --
    not whatever ``orch.slack`` is when the enqueue line runs."""
    from kiro_crew.slack.events import SeenCache, _route_message

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _queue_orch(workspace_a)
    orch._session_tasks["ts1"] = MagicMock()
    orch.sessions.enqueue.return_value = True

    async def _gate_that_reconnects(_channel: str) -> bool:
        orch.slack = workspace_b  # POST /api/slack/reconnect landed mid-route
        return True

    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.channel_inbound_permitted", _gate_that_reconnects),
        patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock),
    ):
        event = {
            "user": "U1",
            "text": "q",
            "ts": "ts1",
            "channel": "D1",
            "channel_type": "im",
            "team": "T1",
        }
        await _route_message(orch, event, SeenCache(), is_mention=True, slack_client=workspace_a)

    assert orch.slack is workspace_b  # the swap did happen mid-route
    assert orch.sessions.enqueue.call_args.kwargs["slack_client"] is workspace_a
    # The hourglass the drain removes through workspace_a was added through it.
    workspace_a.add_reaction.assert_awaited_once_with("D1", "ts1", "hourglass_flowing_sand")
    workspace_b.add_reaction.assert_not_awaited()


def test_listener_passes_its_own_client_to_route_message() -> None:
    """``init_socket_mode`` captures ``orch.slack`` once, beside the socket it
    builds, and hands it to every ``_route_message`` call."""
    import inspect

    from kiro_crew.slack import events

    src = inspect.getsource(events.init_socket_mode)
    assert "received_by = web_api_client if web_api_client is not None else orch.slack" in src
    assert "slack_client=received_by," in src


def _swap_event() -> dict:
    return {
        "user": "U1",
        "text": "q",
        "ts": "ts1",
        "channel": "D1",
        "channel_type": "im",
        "team": "T1",
    }


@pytest.mark.asyncio
async def test_immediate_native_dispatch_uses_the_client_that_received_the_event() -> None:
    """The idle-session path dispatches ``handle_message`` straight away. A
    Reconnect that lands while routing is suspended must not make that turn
    answer through the new workspace: the handler gets the receiving socket's
    client, not ``orch.slack`` as it is at dispatch time."""
    from kiro_crew.slack.events import SeenCache, _route_message

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _queue_orch(workspace_a)

    async def _gate_that_reconnects(_channel: str) -> bool:
        orch.slack = workspace_b  # POST /api/slack/reconnect landed mid-route
        return True

    handled = AsyncMock()
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.channel_inbound_permitted", _gate_that_reconnects),
        patch("kiro_crew.slack.events.handle_message", handled),
    ):
        await _route_message(
            orch, _swap_event(), SeenCache(), is_mention=True, slack_client=workspace_a
        )
        await asyncio.gather(*orch._handler_tasks)

    assert orch.slack is workspace_b
    handled.assert_awaited_once()
    assert handled.await_args.args[0] is workspace_a


@pytest.mark.asyncio
async def test_immediate_transport_dispatch_uses_the_client_that_received_the_event() -> None:
    """Same invariant on the transport path (``handle_message_transport``)."""
    from kiro_crew.config.loader import MessagingConfig
    from kiro_crew.slack.events import SeenCache, _route_message

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _queue_orch(workspace_a)
    orch._cfg.messaging = MessagingConfig(use_transport=True)

    async def _gate_that_reconnects(_channel: str) -> bool:
        orch.slack = workspace_b
        return True

    handled = AsyncMock()
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.channel_inbound_permitted", _gate_that_reconnects),
        patch("kiro_crew.slack.events.handle_message_transport", handled),
    ):
        await _route_message(
            orch, _swap_event(), SeenCache(), is_mention=True, slack_client=workspace_a
        )
        await asyncio.gather(*orch._handler_tasks)

    assert orch.slack is workspace_b
    handled.assert_awaited_once()
    assert handled.await_args.args[0] is workspace_a


def test_route_message_never_reads_the_live_client_after_binding() -> None:
    """Enumeration guard: after ``received_by`` is bound on entry, no code line
    in ``_route_message`` reads ``orch.slack`` -- every event-scoped Web API
    call (user lookup, ephemeral denials, file download, stop/queue replies,
    both immediate dispatch branches, all enqueue sites) goes through the
    client of the socket that received the event."""
    import inspect

    from kiro_crew.slack import events

    lines = inspect.getsource(events._route_message).splitlines()
    bind = next(i for i, line in enumerate(lines) if "received_by = slack_client" in line)
    offenders = [
        line.strip()
        for line in lines[bind + 1 :]
        if "orch.slack" in line
        and not line.strip().startswith("#")
        and "orch.slack_command" not in line
    ]
    assert offenders == []


# ── in-flight turns: link generation captured at receipt ─────────────────────


def _generation_orch(client: Any, generation: int) -> MagicMock:
    orch = _queue_orch(client)
    orch.sessions.slack_links_generation = MagicMock(return_value=generation)
    return orch


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [False, True])
async def test_immediate_dispatch_carries_the_generation_captured_at_receipt(
    transport: bool,
) -> None:
    """Both dispatch branches hand the handler the link generation as of the
    event's RECEIPT, not as of dispatch: a workspace switch that sweeps and
    bumps while routing is suspended must leave this turn holding the OLD
    value, which is what ``set_slack_link`` refuses later."""
    from kiro_crew.config.loader import MessagingConfig
    from kiro_crew.slack.events import SeenCache, _route_message

    client = AsyncMock(name="workspace_a")
    orch = _generation_orch(client, 7)
    if transport:
        orch._cfg.messaging = MessagingConfig(use_transport=True)

    async def _gate_that_switches(_channel: str) -> bool:
        orch.sessions.slack_links_generation.return_value = 8  # the sweep landed
        return True

    handled = AsyncMock()
    target = "handle_message_transport" if transport else "handle_message"
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.channel_inbound_permitted", _gate_that_switches),
        patch(f"kiro_crew.slack.events.{target}", handled),
    ):
        await _route_message(orch, _swap_event(), SeenCache(), is_mention=True, slack_client=client)
        await asyncio.gather(*orch._handler_tasks)

    handled.assert_awaited_once()
    assert handled.await_args.kwargs["links_generation"] == 7


@pytest.mark.asyncio
async def test_every_enqueue_site_carries_the_generation_captured_at_receipt() -> None:
    """All three queue writers record the receipt generation beside the
    receiving client, and ``_dispatch_queued`` hands it on to the handler."""
    from kiro_crew.slack import events
    from kiro_crew.slack.events import SeenCache, _route_message

    client = AsyncMock(name="received_by")
    patches = [
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock),
    ]
    for p in patches:
        p.start()
    try:
        orch = _generation_orch(client, 3)
        orch._session_tasks["ts1"] = MagicMock()
        orch.sessions.enqueue.return_value = True
        await _route_message(orch, dict(_swap_event(), ts="ts1"), SeenCache(), is_mention=True)
        assert orch.sessions.enqueue.call_args.kwargs["links_generation"] == 3

        orch = _generation_orch(client, 3)
        orch._session_tasks["thr"] = MagicMock()
        orch.sessions.enqueue.return_value = False
        event = dict(_swap_event(), ts="ts2", thread_ts="thr", channel="C1", channel_type="channel")
        await _route_message(orch, event, SeenCache(), is_mention=True)
        _ts, _text, kw = orch._pending_queue["thr"][0]
        assert kw["links_generation"] == 3

        orch = _generation_orch(client, 3)
        orch.sessions.enqueue.return_value = True
        await _route_message(orch, dict(_swap_event(), ts="ts3"), SeenCache(), is_mention=True)
        assert orch.sessions.enqueue.call_args.kwargs["links_generation"] == 3
    finally:
        for p in patches:
            p.stop()

    orch = _queue_orch(client)
    with patch.object(events, "handle_message", new_callable=AsyncMock) as hm:
        await events._dispatch_queued(
            orch, "1.0", "2.0", "follow up", {"channel": "C_A", "links_generation": 3}
        )
    assert hm.await_args.kwargs["links_generation"] == 3


@pytest.mark.asyncio
async def test_sessions_double_without_a_generation_means_an_unfenced_turn() -> None:
    """A ``sessions`` that does not model the generation (older doubles, a
    stand-in) yields ``None`` -- an unfenced write -- never a bogus value the
    map would refuse."""
    from kiro_crew.slack.events import SeenCache, _route_message

    client = AsyncMock(name="received_by")
    orch = _queue_orch(client)
    orch.sessions.slack_links_generation = MagicMock(return_value=MagicMock())  # not an int
    handled = AsyncMock()
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.handle_message", handled),
    ):
        await _route_message(orch, _swap_event(), SeenCache(), is_mention=True, slack_client=client)
        await asyncio.gather(*orch._handler_tasks)

    assert handled.await_args.kwargs["links_generation"] is None


def test_every_slack_turn_link_write_is_fenced() -> None:
    """Enumeration guard over the three Slack-turn link writers: every
    ``set_slack_link`` / ``set_channel`` call in ``handler.py`` and
    ``transport_dispatch.py`` presents ``generation=links_generation``; and
    every ``handle_message`` call ``interactions.py`` makes -- the forward
    modal, action buttons and selects, both OPTIONS resolutions, the review-
    revise modal -- forwards ``links_generation`` into that turn, since those
    writers fence only what the turn hands them."""
    import inspect
    import re

    from kiro_crew.slack import handler, interactions, transport_dispatch

    offenders: list[str] = []
    for module in (handler, transport_dispatch):
        for line in inspect.getsource(module).splitlines():
            if re.search(r"sessions\.set_(slack_link|channel)\(", line) and (
                "generation=links_generation" not in line
            ):
                offenders.append(f"{module.__name__}: {line.strip()}")
    assert offenders == []
    src = inspect.getsource(interactions)
    calls = [
        m.group(0)
        for m in re.finditer(r"(?<![\w.])handle_message\((?:[^()]|\([^()]*\))*\)", src)
        # Code only: a comment that mentions ``handle_message()`` is not a call.
        if "#" not in src[src.rfind("\n", 0, m.start()) + 1 : m.start()]
    ]
    assert len(calls) == 5, len(calls)
    assert all("links_generation=links_generation" in c for c in calls), [
        c.splitlines()[0] for c in calls if "links_generation=links_generation" not in c
    ]


# ── workspace record: persisted before it is adopted ─────────────────────────


def _store_fails():
    """Make the workspace record unwritable (a full or read-only data dir)."""
    return patch(
        "kiro_crew.slack.gateway._store_slack_links_team_id",
        side_effect=OSError(28, "No space left on device"),
    )


def _final_store_fails():
    """Let the switch MARKER reach disk but fail the write that adopts the new
    identity -- the disk giving out between the two."""
    from kiro_crew.slack import gateway

    real = gateway._store_slack_links_team_id

    def _store(path: Path, team_id: str, *, pending: Any = None) -> None:
        if pending is None:
            raise OSError(28, "No space left on device")
        real(path, team_id, pending=pending)

    return patch("kiro_crew.slack.gateway._store_slack_links_team_id", _store)


@pytest.mark.asyncio
async def test_unwritable_record_on_a_switch_refuses_and_keeps_the_former_identity() -> None:
    """The identity is PERSISTED before it is adopted: a record that cannot be
    written leaves ``_slack_links_team_id`` on the former workspace and the
    attempt fails closed (socket torn down, nothing published). Adopting it in
    memory first would let the next boot re-read the former record, take the
    new workspace for a switch, and sweep every link it wrote since."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    swept = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    orch.sessions.freeze_slack_links.return_value = swept
    orch.sessions.clear_all_slack_links.return_value = ["dashboard:one"]
    orch.sessions.restore_slack_links.return_value = ["dashboard:one"]
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"), _final_store_fails():
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unrecorded"}
    # The switch did not happen, so its sweep is undone: the rows still name
    # the workspace this install remains bound to, and they are back on disk.
    orch.sessions.clear_all_slack_links.assert_called_once_with()
    orch.sessions.restore_slack_links.assert_called_once_with(swept)
    assert orch.sessions.aflush.await_count == 2  # the sweep's flush, then the restore's
    # Nothing adopted, nothing published, the new socket closed again. The
    # marker stays: the next connect finishes or undoes the switch from it.
    assert orch._slack_links_team_id == "T0FORMER"
    assert _recorded_team(orch) == "T0FORMER"
    assert _pending_team(orch) == "T0NEW"
    recorder.client.close.assert_awaited_once()
    assert orch._socket_client is None
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_socket_connected is False
    assert orch.dashboard_state.slack_connect_error == "workspace_identity_unrecorded"


@pytest.mark.asyncio
async def test_failed_record_restores_the_swept_links_before_refusing() -> None:
    """Order pin for the undo: snapshot before the sweep (the copy is of the
    rows the sweep removes), and on the failed record write the restore and
    ITS flush both complete before the attempt is refused -- a refusal that
    returned first would leave the rows deleted for a switch that never took
    effect, which is the loss an unwritable crew home would otherwise cause."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    rows = [{"key": "slack:171.1", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    order: list[str] = []
    orch.sessions.freeze_slack_links.side_effect = lambda: order.append("freeze") or rows
    orch.sessions.clear_all_slack_links.side_effect = lambda: order.append("sweep") or [
        "slack:171.1"
    ]
    # The undo hands the map ITS rows (the cron store's ``cronjob:`` rows are
    # split off first), so equality, not identity.
    orch.sessions.restore_slack_links.side_effect = lambda got: order.append(
        f"restore {got == rows}"
    ) or ["slack:171.1"]

    async def _flush() -> None:
        order.append("flush")

    orch.sessions.aflush.side_effect = _flush

    with _team("T0NEW"), _final_store_fails():
        result = await _run(orch, _Recorder())

    assert result["connect_error"] == "workspace_identity_unrecorded"
    assert order == ["freeze", "sweep", "flush", "restore True", "flush"]
    assert orch._slack_links_team_id == "T0FORMER"


@pytest.mark.asyncio
async def test_restore_flush_failure_still_refuses_with_the_former_identity() -> None:
    """The restore's flush hits the same disk the record write failed on and
    may fail too. That is the same refusal (socket retired, nothing published,
    former identity kept) -- not an exception past the retirement -- with the
    rows restored in memory for the map's own deferred write to land."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.sessions.freeze_slack_links.return_value = [
        {"key": "slack:171.1", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}
    ]
    orch.sessions.aflush.side_effect = [None, OSError("disk full")]
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"), _final_store_fails():
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unrecorded"}
    orch.sessions.restore_slack_links.assert_called_once()
    assert orch._slack_links_team_id == "T0FORMER"
    assert _recorded_team(orch) == "T0FORMER"
    recorder.client.close.assert_awaited_once()
    assert orch.slack is None


@pytest.mark.asyncio
async def test_nothing_swept_means_nothing_to_restore() -> None:
    """A first record (no former workspace) or an empty sweep has no rows to
    put back: the failed write refuses without a restore or a second flush."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"

    with _team("T0NEW"), _final_store_fails():
        result = await _run(orch, _Recorder())

    assert result["connect_error"] == "workspace_identity_unrecorded"
    orch.sessions.restore_slack_links.assert_not_called()
    orch.sessions.aflush.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_record_that_cannot_be_written_refuses_too() -> None:
    """Same on the very first record: without it the next boot cannot tell a
    switch from a rotation, so an unrecorded identity is not adopted."""
    orch = _orch()
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"), _store_fails():
        result = await _run(orch, recorder)

    assert result["connect_error"] == "workspace_identity_unrecorded"
    assert orch._slack_links_team_id == ""
    assert orch.slack is None
    recorder.client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_recorded_identity_is_on_disk_before_it_is_adopted_in_memory() -> None:
    """Order pin: at the moment the record is written the in-memory identity
    still names the former workspace; it moves only after the write returned."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    seen: list[str] = []

    def _store(path: Path, team_id: str, *, pending: Any = None) -> None:
        marker = f" pending={pending[0]}" if pending else ""
        seen.append(f"store {team_id}{marker} while memory={orch._slack_links_team_id}")
        path.write_text(json.dumps({"team_id": team_id}))

    with _team("T0NEW"), patch("kiro_crew.slack.gateway._store_slack_links_team_id", _store):
        await _run(orch, _Recorder())

    # The marker first (former identity, switch target), then the adopting
    # write; the in-memory identity still names the former workspace at both.
    assert seen == [
        "store T0FORMER pending=T0NEW while memory=T0FORMER",
        "store T0NEW while memory=T0FORMER",
    ]
    assert orch._slack_links_team_id == "T0NEW"


@pytest.mark.asyncio
async def test_retry_after_a_failed_record_does_not_sweep_the_new_workspace() -> None:
    """Because the identity stayed with the former workspace, the retry sees
    the same switch: it re-runs the (now empty) sweep and records the identity
    -- rather than, had the identity moved in memory, taking the new workspace
    for the recorded one and skipping the record for good."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"

    with _team("T0NEW"):
        with _store_fails():
            first = await _run(orch, _Recorder())
        second = await _run(orch, _Recorder())

    assert first["connect_error"] == "workspace_identity_unrecorded"
    assert second["connected"] is True
    assert orch._slack_links_team_id == "T0NEW"
    assert _recorded_team(orch) == "T0NEW"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous", "current", "code"),
    [
        ("T0FORMER", "", "workspace_identity_unverified"),
        ("T0FORMER", "T0NEW", "workspace_identity_unrecorded"),
        ("", "T0NEW", "workspace_identity_unrecorded"),
    ],
)
async def test_boot_binding_refuses_through_the_same_step(
    previous: str, current: str, code: str
) -> None:
    """Boot and reconnect share ``_bind_slack_workspace``: every refusal the
    reconnect step knows tears the boot socket down again, drops the live
    client and records the code where the settings badge reads it."""
    orch = _orch()
    orch._slack_links_team_id = previous
    socket = MagicMock(name="boot-socket")
    socket.close = AsyncMock(name="close")
    orch._socket_client = socket
    orch.slack = MagicMock(name="boot-web-client")

    with _team(current), _store_fails():
        result = await orch._bind_slack_workspace(source="boot")

    assert result == code
    socket.close.assert_awaited_once()
    assert orch._socket_client is None
    assert orch.slack is None
    assert orch._slack_connect_error == code
    assert orch._slack_links_team_id == previous
    assert _recorded_team(orch) is None


@pytest.mark.asyncio
async def test_boot_binding_that_succeeds_publishes_nothing_and_refuses_nothing() -> None:
    orch = _orch()
    socket = MagicMock(name="boot-socket")
    socket.close = AsyncMock(name="close")
    orch._socket_client = socket
    web = MagicMock(name="boot-web-client")
    orch.slack = web

    with _team("T0NEW"):
        assert await orch._bind_slack_workspace(source="boot") == ""

    socket.close.assert_not_awaited()
    assert orch._socket_client is socket
    assert orch.slack is web
    assert orch._slack_links_team_id == "T0NEW"


def test_boot_binds_through_the_refusing_step() -> None:
    """Source pin: ``run()`` binds through ``_bind_slack_workspace`` (which
    refuses) and publishes the boot client -- ``self.slack`` and the dashboard
    mirror -- only AFTER it and only behind ``connected``; a bare
    ``_adopt_slack_workspace`` call there would publish past a refusal, and a
    publish before the bind would pair a client with stale destinations for
    the whole pre-bind window (dashboard auto-open, MCP probe)."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator.run)
    assert 'await self._connect_admitted_slack_socket(source="boot")' in src
    assert '_adopt_slack_workspace(source="boot")' not in src
    admitted = inspect.getsource(GatewayOrchestrator._connect_admitted_slack_socket)
    assert "await self._bind_slack_workspace(source=source)" in admitted
    assert "_adopt_slack_workspace" not in admitted
    bind = src.index('_connect_admitted_slack_socket(source="boot")')
    publish = src.index("self.slack = self._slack_boot_client")
    mirror = src.index("self.dashboard_state.slack_client = self.slack if connected else None")
    assert bind < publish < mirror
    assert src[publish - 40 : publish].rstrip().endswith("if connected:")
    assert src.count("self.slack = ") == 1  # no other publish in run()
    assert "init_socket_mode(self, seen, web_api_client=self._slack_boot_client)" in src


def test_init_services_withholds_the_boot_client() -> None:
    """Source pin: ``_init_services`` builds the client into the private
    candidate and leaves ``self.slack`` None; nothing between it and the boot
    bind publishes a client."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator._init_services)
    assert "self._slack_boot_client = RealSlackClient(self._bot_token)" in src
    assert "self.slack = None" in src
    assert "self.slack = RealSlackClient" not in src
    init_dashboard = inspect.getsource(GatewayOrchestrator._init_dashboard)
    # The dashboard start mirrors whatever ``self.slack`` is -- None at boot --
    # so the mirror is empty until ``run`` publishes behind the bind.
    assert "self.dashboard_state.slack_client = self.slack" in init_dashboard


# ── client affinity: work in flight keeps the client that received it ────────


def _affinity_orch(live: Any) -> Any:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.slack = live
    return orch


def test_slack_client_is_a_property_on_the_orchestrator() -> None:
    """Enumeration guard: every ``orch.slack`` read in the Slack package --
    ``interactions.py`` alone has dozens -- goes through ONE accessor, so the
    binding below covers all of them without touching each site."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    assert isinstance(GatewayOrchestrator.__dict__["slack"], property)


def test_bound_context_wins_over_the_live_client() -> None:
    """Inside a binding ``orch.slack`` is the bound client, however the live
    one changes; outside it is the live one. ``None`` is a real binding."""
    from kiro_crew.slack import affinity

    workspace_a = MagicMock(name="workspace_a")
    workspace_b = MagicMock(name="workspace_b")
    orch = _affinity_orch(workspace_a)

    assert orch.slack is workspace_a
    with affinity.client_scope(workspace_a):
        orch.slack = workspace_b  # POST /api/slack/reconnect landed
        assert orch.slack is workspace_a
        with affinity.client_scope(None):
            assert orch.slack is None
        assert orch.slack is workspace_a
    assert orch.slack is workspace_b
    assert affinity.bound_client() is affinity.UNBOUND


@pytest.mark.asyncio
async def test_tasks_spawned_inside_the_binding_keep_it_across_a_reconnect() -> None:
    """The agent turn an envelope starts runs in tasks of its own; they inherit
    the binding, so their final post after a reconnect still goes through the
    client that received the envelope."""
    from kiro_crew.slack import affinity

    workspace_a = MagicMock(name="workspace_a")
    workspace_b = MagicMock(name="workspace_b")
    orch = _affinity_orch(workspace_a)
    reconnected = asyncio.Event()

    async def _turn() -> Any:
        await reconnected.wait()  # the agent is thinking; the operator clicks Reconnect
        return orch.slack

    with affinity.client_scope(workspace_a):
        task = asyncio.create_task(_turn())
    await asyncio.sleep(0)
    orch.slack = workspace_b
    reconnected.set()

    assert await task is workspace_a
    assert orch.slack is workspace_b


def _envelope(req_type: str, payload: dict | None = None) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(type=req_type, payload=payload or {}, envelope_id="env-1")


async def _install_listener(orch: Any) -> Any:
    """Run the real ``init_socket_mode`` against a mocked Socket Mode client
    and return the listener it installed."""
    from kiro_crew.slack import events

    client_cls = MagicMock(name="WSSocketModeClient")
    client_cls.return_value.socket_mode_request_listeners = []
    ctx = MagicMock()
    ctx.return_value.slack_gate.validate_enterprise.return_value = True
    with (
        patch("kiro_crew.slack.events.WSSocketModeClient", client_cls),
        patch("kiro_crew.slack.events.AsyncWebClient", MagicMock()),
        patch("kiro_crew.slack.events.current_context", ctx),
        patch("kiro_crew.slack.events.set_allowed_users"),
        patch("kiro_crew.slack.events.set_tracking_channels"),
        patch("kiro_crew.slack.events.set_open_channels"),
        patch("kiro_crew.slack.events.set_owner_id"),
        patch("kiro_crew.slack.events.set_orch_cfg"),
        patch("kiro_crew.slack.events.set_dashboard_state"),
        patch("kiro_crew.slack.events.set_yolo_mode"),
    ):
        await events.init_socket_mode(orch, events.SeenCache())
    return orch._socket_client.socket_mode_request_listeners[0]


def _listener_orch(live_client: Any) -> MagicMock:
    orch = _queue_orch(live_client)
    orch._slack_enabled = True
    orch._bot_token = "xoxb-not-a-real-value"
    orch._app_token = "xapp-not-a-real-value"
    orch._owner_id = "U_OWNER"
    orch._allowed_users = {"U_OWNER"}
    orch._tracking_channels = set()
    orch._open_channels = set()
    orch._approval_mode = ""
    orch.slack_command = "kirocrew"
    orch._socket_client = None
    orch.sessions.is_paused_for_update = MagicMock(return_value=False)
    return orch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("req_type", "payload", "target"),
    [
        ("interactive", {"type": "block_actions"}, "dispatch_interactive"),
        ("slash_commands", {"command": "/kirocrew", "user_id": "U_OWNER"}, "_handle_slash"),
    ],
)
async def test_listener_binds_the_receiving_client_to_every_envelope(
    req_type: str, payload: dict, target: str
) -> None:
    """Interactions and slash commands answer through ``orch.slack`` reads
    scattered over ``interactions.py``; the listener binds the client that
    received the envelope to the envelope's task, so those reads resolve to
    it even after a reconnect swapped the live client mid-handler."""
    from kiro_crew.slack import affinity

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _listener_orch(workspace_a)
    seen: list[Any] = []
    reconnected = asyncio.Event()

    async def _handler(*_a: Any, **_k: Any) -> None:
        await reconnected.wait()
        seen.append(affinity.bound_client())

    socket = MagicMock(name="socket")
    socket.send_socket_mode_response = AsyncMock()
    on_event = await _install_listener(orch)
    with (
        patch("kiro_crew.slack.events.admit_inbound_callback", AsyncMock(return_value=True)),
        patch(f"kiro_crew.slack.events.{target}", _handler),
    ):
        await on_event(socket, _envelope(req_type, payload))
        await asyncio.sleep(0)
        orch.slack = workspace_b  # POST /api/slack/reconnect landed mid-handler
        reconnected.set()
        await asyncio.gather(*orch._handler_tasks)

    assert seen == [workspace_a]
    assert affinity.bound_client() is affinity.UNBOUND  # nothing leaked out of the envelope


@pytest.mark.asyncio
async def test_listener_binds_the_receiving_client_to_message_events() -> None:
    from kiro_crew.slack import affinity

    workspace_a = AsyncMock(name="workspace_a")
    orch = _listener_orch(workspace_a)
    seen: list[Any] = []

    async def _route(*_a: Any, **_k: Any) -> None:
        seen.append(affinity.bound_client())

    socket = MagicMock(name="socket")
    socket.send_socket_mode_response = AsyncMock()
    on_event = await _install_listener(orch)
    with (
        patch("kiro_crew.slack.events.admit_inbound_callback", AsyncMock(return_value=True)),
        patch("kiro_crew.slack.events._route_message", _route),
    ):
        await on_event(
            socket,
            _envelope("events_api", {"event": {"type": "message", **_swap_event()}}),
        )

    assert seen == [workspace_a]


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep_during", ["admission", "ack"])
async def test_listener_reads_the_generation_before_its_first_await(sweep_during: str) -> None:
    """The admission gate and the ACK both suspend before a message reaches
    ``_route_message``; a workspace switch that sweeps in either gap bumps
    the generation. The listener reads it at envelope ENTRY, so the turn
    carries the pre-sweep value and ``set_slack_link`` refuses its
    former-workspace thread -- a read after those awaits would be the current
    value and pass the row through."""
    workspace_a = AsyncMock(name="workspace_a")
    orch = _listener_orch(workspace_a)
    orch.sessions.slack_links_generation = MagicMock(return_value=5)
    carried: list[Any] = []

    async def _route(*_a: Any, **kw: Any) -> None:
        carried.append(kw.get("links_generation"))

    async def _admit(*_a: Any, **_k: Any) -> bool:
        if sweep_during == "admission":
            orch.sessions.slack_links_generation.return_value = 6
        return True

    async def _ack(*_a: Any, **_k: Any) -> None:
        if sweep_during == "ack":
            orch.sessions.slack_links_generation.return_value = 6

    socket = MagicMock(name="socket")
    socket.send_socket_mode_response = _ack
    on_event = await _install_listener(orch)
    with (
        patch("kiro_crew.slack.events.admit_inbound_callback", _admit),
        patch("kiro_crew.slack.events._route_message", _route),
    ):
        await on_event(
            socket,
            _envelope("events_api", {"event": {"type": "message", **_swap_event()}}),
        )

    assert carried == [5]
    assert orch.sessions.slack_links_generation.return_value == 6  # the sweep did land


@pytest.mark.asyncio
async def test_route_message_keeps_the_generation_it_was_handed() -> None:
    """Given a receipt value, ``_route_message`` does not read a fresh one:
    what the listener captured is what reaches the handler."""
    from kiro_crew.slack.events import SeenCache, _route_message

    client = AsyncMock(name="workspace_a")
    orch = _generation_orch(client, 6)  # the live value has already moved on
    handled = AsyncMock()
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.channel_inbound_permitted", AsyncMock(return_value=True)),
        patch("kiro_crew.slack.events.handle_message", handled),
    ):
        await _route_message(
            orch,
            _swap_event(),
            SeenCache(),
            is_mention=True,
            slack_client=client,
            links_generation=5,
        )
        await asyncio.gather(*orch._handler_tasks)

    assert handled.await_args.kwargs["links_generation"] == 5
    orch.sessions.slack_links_generation.assert_not_called()


def test_listener_reads_the_generation_before_the_binding_scope() -> None:
    """Source pin: in ``_on_envelope`` the generation read precedes the
    affinity scope and the ``_on_event`` await, and ``_on_event`` passes it
    down to ``_route_message`` instead of reading its own."""
    import inspect

    from kiro_crew.slack import events

    src = inspect.getsource(events.init_socket_mode)
    envelope = src.split("async def _on_envelope(", 1)[1].split("async def _on_event(", 1)[0]
    read_at = envelope.index("_links_generation_at_receipt(orch)")
    assert read_at < envelope.index("client_scope(")
    assert read_at < envelope.index("await ")
    on_event = src.split("async def _on_event(", 1)[1]
    assert "_links_generation_at_receipt" not in on_event
    assert "links_generation=links_generation" in on_event


@pytest.mark.asyncio
async def test_queued_dispatch_binds_the_entry_client_for_the_whole_turn() -> None:
    """The drain runs in its own task, so the listener's binding does not
    reach it: ``_dispatch_queued`` binds the queue entry's client itself, and
    the handler (and whatever it spawns) reads that client back."""
    from kiro_crew.slack import affinity, events

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _queue_orch(workspace_b)  # the reconnect already happened
    seen: list[Any] = []

    async def _handler(*_a: Any, **_k: Any) -> None:
        seen.append(affinity.bound_client())

    with patch.object(events, "handle_message", _handler):
        await events._dispatch_queued(
            orch, "1.0", "2.0", "follow up", {"channel": "C_A", "slack_client": workspace_a}
        )

    assert seen == [workspace_a]
    assert affinity.bound_client() is affinity.UNBOUND


def test_every_envelope_kind_is_dispatched_inside_the_binding() -> None:
    """Source pin: the listener binds BEFORE any dispatch -- the ack, the
    interactive task, the slash task and the message routing all live in the
    bound body (``_on_event``), none in the registered wrapper
    (``_on_envelope``); and the wrapper is what the socket gets."""
    import inspect

    from kiro_crew.slack import events

    src = inspect.getsource(events.init_socket_mode)
    outer = src.split("async def _on_envelope(", 1)[1].split("async def _on_event(", 1)[0]
    assert "slack_affinity.client_scope(received_by)," in outer
    # The authorization subject is bound beside the client, from the same
    # socket-build instant (``received_owner``), never read from the module
    # global a Reconnect rebinds.
    assert "slack_affinity.owner_scope(received_owner)," in outer
    assert "await _on_event(client, req, links_generation=links_generation)" in outer
    assert "create_task" not in outer and "_route_message" not in outer
    assert "socket_mode_request_listeners.append(_on_envelope)" in src
    # The reserve-before-ack contract (test_update_check_install_aware) reads
    # ``_on_event``; the wrapper must add no suspension ahead of it.
    assert outer.count("await") == 1


# ── workspace record: rides in the config snapshot beside the map ─────────────


def test_workspace_record_is_a_config_component_file_beside_the_session_map() -> None:
    """A config snapshot restored to a replacement host whose credentials name
    another workspace must carry the workspace record WITH the session map:
    without it the restored rows have no identity beside them, the boot's
    switch detection treats "no record" as a first boot, and every
    workspace-A destination is kept under workspace B. Same component, so a
    selective restore cannot separate the two; and JSON-object validated, so a
    misshapen restore is refused rather than read as a damaged record that
    takes Slack down."""
    from kiro_crew import snapshot_components as sc
    from kiro_crew.slack.gateway import SLACK_WORKSPACE_STATE_FILENAME

    config_files = sc.CORE_FILES["config"]
    assert SLACK_WORKSPACE_STATE_FILENAME in config_files
    # And it travels ONLY beside the map: the portable export/import
    # (``portability``) leaves the session map behind and validates none of
    # the config component's files, so the record stays behind with it --
    # imported alone over a surviving live map it would make the next
    # handshake a switch that sweeps every persisted Slack link.
    from kiro_crew.portability import EXPORT_EXCLUDE, IMPORT_ROOT_EXCLUDE

    assert "session_map.json" in EXPORT_EXCLUDE
    assert SLACK_WORKSPACE_STATE_FILENAME in IMPORT_ROOT_EXCLUDE
    # At the archive ROOT only: ``EXPORT_EXCLUDE`` is matched by basename over
    # the user trees (workspace/, plan_memory/, skills/), where the name would
    # silently drop a user's own ``slack_workspace.json`` from every export.
    assert SLACK_WORKSPACE_STATE_FILENAME not in EXPORT_EXCLUDE
    assert EXPORT_EXCLUDE <= IMPORT_ROOT_EXCLUDE
    # Staged record FIRST: a copy of the record naming the new workspace can
    # then only have been read after the switch's sweep landed, so the map
    # copied after it holds no former-workspace link (a map copied first could
    # pair pre-sweep links with a post-adopt record, which a restore under the
    # new workspace would keep and never sweep).
    assert config_files.index(SLACK_WORKSPACE_STATE_FILENAME) < config_files.index(
        "session_map.json"
    )
    assert "session_map.json" in config_files
    assert SLACK_WORKSPACE_STATE_FILENAME in sc.COMPONENT_JSON_OBJECTS
    assert SLACK_WORKSPACE_STATE_FILENAME in sc.CORE_FILES_FLAT
    assert SLACK_WORKSPACE_STATE_FILENAME in sc.COMPONENT_HELP["config"]


# ── interactive callbacks present the receipt generation ─────────────────────


def _resume_orch(generation_reads: list[int]) -> MagicMock:
    """An interactions orchestrator whose map reports *generation_reads* in
    turn (the last value repeats), with a resumable session and a dashboard."""
    orch = MagicMock(name="orch")
    orch.slack = AsyncMock(name="workspace_a")
    orch.slack.post_message = AsyncMock(return_value="ts1")
    orch.slack.open_dm = AsyncMock(return_value="D1")
    orch.sessions = MagicMock(name="sessions")
    orch.sessions.get_slack_link = MagicMock(return_value=("", ""))
    orch.sessions.set_slack_link = MagicMock()
    reads = iter(generation_reads)
    last = generation_reads[-1]

    def _gen() -> int:
        nonlocal last
        last = next(reads, last)
        return last

    orch.sessions.slack_links_generation = MagicMock(side_effect=_gen)
    orch.dashboard_state = MagicMock(name="dashboard_state")
    # The resumed session has an OPEN tab: only then is there a dashboard copy
    # to redraw (``link_slack``); a resume without one redraws nothing.
    orch.dashboard_state._slots = {_RESUME_KEY: MagicMock(name="slot")}
    return orch


_RESUME_KEY = "dashboard_r16"


def _resume_action(key: str = _RESUME_KEY) -> dict:
    return {"action_id": "mc_resume_thread_x", "value": json.dumps({"key": key, "title": "T"})}


@pytest.fixture
def _own_resume_lock() -> Any:
    """Drop this module's entry from ``interactions._resume_locks`` afterwards:
    the map is module-global and a sibling test counts it."""
    from kiro_crew.slack import interactions as ix

    yield
    ix._resume_locks.pop(_RESUME_KEY, None)


def _resume_payload() -> dict:
    return {"user": {"id": "U_OWNER"}, "channel": {"id": "C1"}, "message": {"ts": "m1"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["thread", "dm"])
async def test_resume_choice_refuses_both_link_writes_when_the_workspace_switched(
    mode: str, _own_resume_lock: Any
) -> None:
    """The resume posted its header through several awaits; a workspace-switching
    Reconnect inside them swept every persisted destination and bumped the
    generation. Presenting the receipt value, the callback skips the map write
    AND the dashboard link together -- half a link is the two-owner state the
    batched save exists to prevent -- and records the refusal."""
    from kiro_crew.slack import interactions as ix

    orch = _resume_orch([7])  # receipt was 5; the map now reads 7

    async def _post(*_a: Any, **_k: Any) -> str:
        return "ts1"

    orch.slack.post_message = AsyncMock(side_effect=_post)
    sel = MagicMock()
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "is_owner", lambda uid: True),
        patch.object(ix, "channel_inbound_permitted", AsyncMock(return_value=True)),
        patch.object(ix, "sel", lambda: sel),
    ):
        await ix._handle_resume_choice(
            _resume_payload(),
            _resume_action(),
            "C1",
            "m1",
            "U_OWNER",
            mode=mode,
            links_generation=5,
        )

    orch.sessions.set_slack_link.assert_not_called()
    orch.dashboard_state.link_slack.assert_not_called()
    refused = [
        c.kwargs for c in sel.log_api_access.call_args_list if c.kwargs.get("outcome") == "refused"
    ]
    assert refused and refused[0]["operation"] == "slack.session_resume"


@pytest.mark.asyncio
async def test_resume_choice_links_with_the_receipt_generation_when_unchanged(
    _own_resume_lock: Any,
) -> None:
    """Same workspace throughout: both writes land, and the map write presents
    the receipt generation so the map's own fence can judge it too."""
    from kiro_crew.slack import interactions as ix

    orch = _resume_orch([5])
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "is_owner", lambda uid: True),
        patch.object(ix, "channel_inbound_permitted", AsyncMock(return_value=True)),
        patch.object(ix, "sel", lambda: MagicMock()),
    ):
        await ix._handle_resume_choice(
            _resume_payload(),
            _resume_action(),
            "C1",
            "m1",
            "U_OWNER",
            mode="thread",
            links_generation=5,
        )

    orch.sessions.set_slack_link.assert_called_once_with(_RESUME_KEY, "ts1", "C1", generation=5)
    # The dashboard redraw carries the same receipt generation as the map write.
    orch.dashboard_state.link_slack.assert_called_once_with(_RESUME_KEY, "ts1", "C1", generation=5)


@pytest.mark.asyncio
async def test_resume_choice_without_a_receipt_generation_is_unfenced(
    _own_resume_lock: Any,
) -> None:
    """``None`` means no generation was captured; the fence is opt-in per turn,
    as it is for message turns, so the write goes through (with ``None``)."""
    from kiro_crew.slack import interactions as ix

    orch = _resume_orch([9])
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "is_owner", lambda uid: True),
        patch.object(ix, "channel_inbound_permitted", AsyncMock(return_value=True)),
        patch.object(ix, "sel", lambda: MagicMock()),
    ):
        await ix._handle_resume_choice(
            _resume_payload(), _resume_action(), "C1", "m1", "U_OWNER", mode="thread"
        )

    orch.sessions.set_slack_link.assert_called_once_with(_RESUME_KEY, "ts1", "C1", generation=None)
    orch.dashboard_state.link_slack.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action_id", ["mc_resume_thread_x", "mc_resume_dm_x"])
async def test_dispatch_threads_the_generation_to_the_resume_choice(action_id: str) -> None:
    from kiro_crew.slack import interactions as ix

    orch = _resume_orch([5])
    handled = AsyncMock()
    payload = {**_resume_payload(), "actions": [{"action_id": action_id, "value": "{}"}]}
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "is_allowed_user", lambda uid: True),
        patch.object(ix, "channel_inbound_permitted", AsyncMock(return_value=True)),
        patch.object(ix, "_handle_resume_choice", handled),
    ):
        await ix.dispatch(payload, links_generation=5)

    assert handled.await_args.kwargs["links_generation"] == 5


@pytest.mark.asyncio
async def test_dispatch_threads_the_generation_to_the_link_dashboard_import() -> None:
    from kiro_crew.slack import interactions as ix

    orch = _resume_orch([5])
    orch.dashboard_state.get_or_create_slot = MagicMock()
    importer = AsyncMock(return_value=None)
    payload = {
        **_resume_payload(),
        "message": {"ts": "m1", "thread_ts": "200.0"},
        "actions": [{"action_id": ix.LINK_DASHBOARD_ACTION, "value": ""}],
    }
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "is_allowed_user", lambda uid: True),
        patch.object(ix, "channel_inbound_permitted", AsyncMock(return_value=True)),
        patch.object(ix, "sel", lambda: MagicMock()),
        patch.object(ix, "_import_thread_to_slot", importer),
    ):
        await ix.dispatch(payload, links_generation=5)

    assert importer.await_args.kwargs["links_generation"] == 5


@pytest.mark.asyncio
async def test_thread_import_refuses_the_link_when_the_workspace_switched_mid_fetch() -> None:
    """The import's await is ``fetch_thread_replies``; a switch inside it bumps
    the generation. Decided after the fetch and before the slot exists, so a
    refused import leaves nothing behind: no slot, no dashboard link."""
    from kiro_crew.slack import interactions as ix

    ds = MagicMock(name="dashboard_state")
    ds.get_linked_slot = MagicMock(return_value=None)
    ds.sessions.slack_links_generation = MagicMock(return_value=5)
    slack = MagicMock(name="workspace_a")

    async def _fetch(*_a: Any, **_k: Any) -> list[dict]:
        ds.sessions.slack_links_generation.return_value = 6  # the sweep landed here
        return [{"user": "U1", "text": "hello"}]

    slack.fetch_thread_replies = AsyncMock(side_effect=_fetch)

    result = await ix._import_thread_to_slot(slack, ds, "C1", "100.0", links_generation=5)

    assert result is None
    ds.get_or_create_slot.assert_not_called()
    ds.link_slack.assert_not_called()


@pytest.mark.asyncio
async def test_thread_import_links_when_the_generation_is_unchanged() -> None:
    from kiro_crew.slack import interactions as ix

    ds = MagicMock(name="dashboard_state")
    ds.get_linked_slot = MagicMock(return_value=None)
    ds.sessions.slack_links_generation = MagicMock(return_value=5)
    ds._self_bot_id = "B1"
    slot = MagicMock(name="slot")
    slot.key = "s1"
    ds.get_or_create_slot = MagicMock(return_value=slot)
    slack = MagicMock(name="workspace_a")
    slack.fetch_thread_replies = AsyncMock(return_value=[{"user": "U1", "text": "hello"}])

    with patch("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", AsyncMock()):
        result = await ix._import_thread_to_slot(slack, ds, "C1", "100.0", links_generation=5)

    assert result is slot
    ds.link_slack.assert_called_once_with("s1", "100.0", "C1", generation=5)


@pytest.mark.asyncio
async def test_listener_hands_the_receipt_generation_to_interactive_dispatch() -> None:
    """The interactive envelope kind carries the same receipt value the message
    kind does; a resume or link-to-dashboard callback is one Slack turn."""
    workspace_a = AsyncMock(name="workspace_a")
    orch = _listener_orch(workspace_a)
    orch.sessions.slack_links_generation = MagicMock(return_value=5)
    carried: list[Any] = []

    async def _dispatch(*_a: Any, **kw: Any) -> None:
        carried.append(kw.get("links_generation"))

    socket = MagicMock(name="socket")
    socket.send_socket_mode_response = AsyncMock()
    on_event = await _install_listener(orch)
    with (
        patch("kiro_crew.slack.events.admit_inbound_callback", AsyncMock(return_value=True)),
        patch("kiro_crew.slack.events.dispatch_interactive", _dispatch),
    ):
        await on_event(socket, _envelope("interactive", {"type": "block_actions"}))
        await asyncio.gather(*orch._handler_tasks)

    assert carried == [5]


def test_slash_command_threads_the_generation_to_the_thread_import() -> None:
    """Source pin over ``handler.py``: every ``_handle_slash_command`` call in
    ``handle_message`` forwards ``links_generation``, and the one command that
    writes a Slack link (``!link-to-dashboard``) presents it to the import."""
    import inspect
    import re

    from kiro_crew.slack import handler

    body = inspect.getsource(handler.handle_message)
    calls = re.findall(r"_handle_slash_command\((?:[^()]|\([^()]*\))*\)", body, re.S)
    assert len(calls) == 2, calls
    assert all("links_generation=links_generation" in c for c in calls)
    slash = inspect.getsource(handler._handle_slash_command)
    imports = re.findall(r"_import_thread_to_slot\((?:[^()]|\([^()]*\))*\)", slash, re.S)
    assert imports and all("links_generation=links_generation" in c for c in imports)


def test_every_interactive_link_write_is_fenced() -> None:
    """Enumeration guard over ``interactions.py``, the module the message-path
    guard (``test_every_slack_turn_link_write_is_fenced``) does not cover.
    Every ``sessions.set_slack_link`` there presents ``generation=``, and every
    function that links through ``dashboard_state.link_slack`` -- which has no
    generation to present -- gates on ``_slack_links_stale`` first.

    Enumerated and deliberately NOT fenced: ``gateway.py``'s cron post
    (``self.sessions.set_channel(session_key, channel)``) records the job's own
    configured channel after posting to it. It is not a Slack turn -- no envelope,
    no receipt generation -- and the destination it writes comes from the job
    record, not from a swept row, so a switch cannot make it republish one."""
    import inspect
    import re

    from kiro_crew.slack import interactions

    src = inspect.getsource(interactions)
    offenders = [
        line.strip()
        for line in src.splitlines()
        if re.search(r"sessions\.set_slack_link\(", line) and "generation=" not in line
        # A multi-line call carries the kwarg on a later line; pin those by the
        # call's opening line ending in "(".
        and not line.rstrip().endswith("(")
    ]
    assert offenders == []
    linkers = [
        obj
        for name, obj in vars(interactions).items()
        if inspect.isfunction(obj)
        and obj.__module__ == interactions.__name__
        and ".link_slack(" in inspect.getsource(obj)
    ]
    assert {f.__name__ for f in linkers} == {"_import_thread_to_slot", "_handle_resume_choice"}
    for fn in linkers:
        body = inspect.getsource(fn)
        assert body.index("_slack_links_stale(") < body.index(".link_slack("), fn.__name__


# ── the candidate client is private until the workspace is adopted ───────────


@pytest.mark.asyncio
async def test_orchestrator_client_is_withheld_until_the_switch_is_durable() -> None:
    """``self.slack`` -- what a cron delivery or a dashboard send reads -- is
    None from the teardown of the former client until the new workspace's
    sweep and record are on disk. A concurrent reader in that window finds no
    client at all, so it cannot pair the new workspace's client with a
    persisted former-workspace channel. The listener still gets the candidate
    explicitly, so the socket answers through the client that belongs to it."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    old_web = MagicMock(name="old-web-client")
    orch.slack = old_web
    seen: list[str] = []
    rec = _Recorder()
    client_cls = MagicMock(name="RealSlackClient")

    def _sweep() -> list[str]:
        seen.append(f"sweep slack={orch.slack!r}")
        return ["dashboard:one"]

    async def _flush() -> None:
        seen.append(f"flush slack={orch.slack!r}")

    orch.sessions.clear_all_slack_links.side_effect = _sweep
    orch.sessions.aflush.side_effect = _flush

    async def _connect(self: Any) -> bool:
        seen.append(f"connect slack={self.slack!r}")
        return True

    with _team("T0NEW"):
        result = await _run(orch, rec, connect=_connect, client_cls=client_cls)

    assert result["connected"] is True
    # Every step between teardown and publish saw no orchestrator client --
    # and the socket connected only AFTER the switch was durable.
    assert seen == ["sweep slack=None", "flush slack=None", "connect slack=None"]
    # The listener was handed the candidate the orchestrator was withholding.
    assert rec.web_api_clients == [client_cls.return_value]
    # Published together, last.
    assert orch.slack is client_cls.return_value
    assert orch.dashboard_state.slack_client is client_cls.return_value


@pytest.mark.asyncio
async def test_refused_adoption_publishes_the_candidate_nowhere() -> None:
    """A connected socket whose adoption is refused (here: the switch's flush
    fails) leaves ``self.slack`` None too, not just the dashboard mirror."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.sessions.clear_all_slack_links.return_value = ["dashboard:one"]
    orch.sessions.aflush.side_effect = OSError(28, "No space left on device")
    client_cls = MagicMock(name="RealSlackClient")

    with _team("T0NEW"):
        result = await _run(orch, _Recorder(), client_cls=client_cls)

    assert result["connected"] is False
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None


@pytest.mark.asyncio
async def test_aborted_teardown_keeps_the_former_web_client_withdrawn() -> None:
    """``previous_client_close_failed`` keeps the former SOCKET referenced (a
    retry closes it again, shutdown reaches it) but does NOT put the former Web
    API client back: the abort happens before the store's owner is hoisted, so
    the former owner is still the bound authorization subject, and a restored
    client is what every unbound outbound reader -- heartbeat, cron, dashboard
    send -- would resolve to. Down, for a named reason, with nothing to send
    through, like every other failure path of the attempt."""
    old = MagicMock(name="old-socket-client")
    old.close = AsyncMock(side_effect=RuntimeError("websocket already gone"))
    old_web = MagicMock(name="old-web-client")
    orch = _orch(old_client=old)
    orch.slack = old_web
    orch.dashboard_state.slack_client = old_web

    result = await _run(orch, _Recorder())

    assert result["connect_error"] == "previous_client_close_failed"
    assert orch._socket_client is old
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None


def test_no_failure_path_of_the_attempt_publishes_a_client() -> None:
    """Source pin: inside ``_reconnect_slack_once`` the only assignment of
    ``self.slack`` to anything but ``None`` is the gated publish of the
    candidate; no abort branch restores a client."""
    import inspect
    import re

    from kiro_crew.slack.gateway import GatewayOrchestrator

    # The attempt is two halves: the read + withdrawal, and the withdrawn body.
    src = inspect.getsource(GatewayOrchestrator._reconnect_slack_once) + inspect.getsource(
        GatewayOrchestrator._reconnect_slack_withdrawn
    )
    assigns = re.findall(r"self\.slack = (\w+)", src)
    assert sorted(set(assigns)) == ["None", "candidate"], assigns
    assert "old_web" not in src


@pytest.mark.asyncio
async def test_listener_answers_through_the_handed_client_not_orch_slack() -> None:
    """``init_socket_mode(web_api_client=...)`` binds THAT client to every
    envelope; ``orch.slack`` -- empty during a reconnect -- is not consulted."""
    from kiro_crew.slack import affinity, events

    candidate = AsyncMock(name="candidate")
    orch = _listener_orch(None)
    orch.slack = None
    seen: list[Any] = []

    async def _route(*_a: Any, **_k: Any) -> None:
        seen.append(affinity.bound_client())

    socket = MagicMock(name="socket")
    socket.send_socket_mode_response = AsyncMock()
    client_cls = MagicMock(name="WSSocketModeClient")
    client_cls.return_value.socket_mode_request_listeners = []
    ctx = MagicMock()
    ctx.return_value.slack_gate.validate_enterprise.return_value = True
    with (
        patch("kiro_crew.slack.events.WSSocketModeClient", client_cls),
        patch("kiro_crew.slack.events.AsyncWebClient", MagicMock()),
        patch("kiro_crew.slack.events.current_context", ctx),
        patch("kiro_crew.slack.events.set_allowed_users"),
        patch("kiro_crew.slack.events.set_tracking_channels"),
        patch("kiro_crew.slack.events.set_open_channels"),
        patch("kiro_crew.slack.events.set_owner_id"),
        patch("kiro_crew.slack.events.set_orch_cfg"),
        patch("kiro_crew.slack.events.set_dashboard_state"),
        patch("kiro_crew.slack.events.set_yolo_mode"),
    ):
        await events.init_socket_mode(orch, events.SeenCache(), web_api_client=candidate)
    on_event = orch._socket_client.socket_mode_request_listeners[0]
    with (
        patch("kiro_crew.slack.events.admit_inbound_callback", AsyncMock(return_value=True)),
        patch("kiro_crew.slack.events._route_message", _route),
    ):
        await on_event(
            socket,
            _envelope("events_api", {"event": {"type": "message", **_swap_event()}}),
        )

    assert seen == [candidate]


def test_reconnect_publishes_self_slack_only_after_binding() -> None:
    """Source pin: in ``_reconnect_slack_once`` the only assignment of
    ``self.slack`` to a client follows the workspace binding and is gated on
    ``connected``; the candidate is built into a local, and the listener is
    handed it explicitly."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator._reconnect_slack_withdrawn)
    assert "candidate = RealSlackClient(self._bot_token)" in src
    assert "init_socket_mode(self, seen, web_api_client=candidate)" in src
    assert "self.slack = RealSlackClient" not in src
    bind = src.index('self._connect_admitted_slack_socket(source="reconnect")')
    publish = src.index("self.slack = candidate")
    assert bind < publish
    assert src[publish - 40 : publish].rstrip().endswith("if connected:")


# ── indirect interactive turns forward the receipt generation ────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_id, handler_name",
    [
        ("mc_review_revise_submit", "_handle_review_revise_submit"),
    ],
)
async def test_view_submission_forwards_the_generation_to_aware_handlers(
    callback_id: str, handler_name: str
) -> None:
    from kiro_crew.slack import interactions as ix

    handled = AsyncMock()
    # An AsyncMock has no ``links_generation`` in its signature; register the
    # real handler's name so the dispatcher's signature check sees the real
    # signature, then intercept the call one level down.
    real = ix.VIEW_REGISTRY[callback_id]
    assert real.__name__ == handler_name
    assert ix._accepts_links_generation(real)

    async def _spy(payload: dict, *, links_generation: int | None = None) -> None:
        await handled(payload, links_generation=links_generation)

    with patch.dict(ix.VIEW_REGISTRY, {callback_id: _spy}):
        await ix.dispatch(
            {"type": "view_submission", "view": {"callback_id": callback_id}},
            links_generation=5,
        )
    assert handled.await_args.kwargs["links_generation"] == 5


@pytest.mark.asyncio
async def test_view_submission_calls_plain_handlers_without_the_kwarg() -> None:
    """A registered handler with the plain ``(payload)`` signature -- every
    third-party or test registration -- is called exactly as before."""
    from kiro_crew.slack import interactions as ix

    seen: list[dict] = []

    async def plain(payload: dict) -> None:
        seen.append(payload)

    payload = {"type": "view_submission", "view": {"callback_id": "plain_r17"}}
    with patch.dict(ix.VIEW_REGISTRY, {"plain_r17": plain}):
        await ix.dispatch(payload, links_generation=5)
    assert seen == [payload]


@pytest.mark.asyncio
async def test_forward_modal_fallback_forwards_the_generation() -> None:
    """The live-reconfigured forward callback resolves outside the registry;
    that path hands the generation down too."""
    from kiro_crew.slack import interactions as ix

    handled = AsyncMock()

    async def _spy(payload: dict, *, links_generation: int | None = None) -> None:
        await handled(payload, links_generation=links_generation)

    with (
        patch.object(ix, "_get_forward_callback", lambda: "fwd_r17"),
        patch.object(ix, "_handle_shortcut_submission", _spy),
        patch.dict(ix.VIEW_REGISTRY, {}, clear=False),
    ):
        ix.VIEW_REGISTRY.pop("fwd_r17", None)
        await ix.dispatch(
            {"type": "view_submission", "view": {"callback_id": "fwd_r17"}}, links_generation=5
        )
    assert handled.await_args.kwargs["links_generation"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action_id, handler_name",
    [
        ("mc_options_submit", "_handle_options_submit"),
        ("mc_opt_1_x", "_handle_options"),
    ],
)
async def test_options_resolutions_receive_the_generation(
    action_id: str, handler_name: str
) -> None:
    from kiro_crew.slack import interactions as ix
    from kiro_crew.slack.format import OPTIONS_ACTION_PREFIX, OPTIONS_SUBMIT_ACTION

    resolved = (
        OPTIONS_SUBMIT_ACTION if action_id == "mc_options_submit" else f"{OPTIONS_ACTION_PREFIX}1_x"
    )
    orch = _resume_orch([5])
    handled = AsyncMock()
    payload = {**_resume_payload(), "actions": [{"action_id": resolved, "value": "{}"}]}
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "is_allowed_user", lambda uid: True),
        patch.object(ix, "channel_inbound_permitted", AsyncMock(return_value=True)),
        patch.object(ix, handler_name, handled),
    ):
        await ix.dispatch(payload, links_generation=5)
    assert handled.await_args.kwargs["links_generation"] == 5


@pytest.mark.asyncio
async def test_indirect_turns_hand_the_generation_to_handle_message() -> None:
    """Each indirect path's ``handle_message`` call carries the generation the
    path was handed: the forward modal, the shared action router, both
    OPTIONS resolutions and the review-revise modal."""
    from kiro_crew.slack import interactions as ix

    orch = _resume_orch([5])
    orch.slack.post_message = AsyncMock(return_value="ts9")
    orch.ctx_builder = orch.cron_svc = orch.conv_log = None
    orch.consolidator = orch.subagent_mgr = orch.task_runner = None
    orch._handler_tasks = set()
    handled = AsyncMock()
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "handle_message", handled),
    ):
        await ix._route_action_to_session(
            "C1",
            "m1",
            "t1",
            "U_OWNER",
            "T1",
            "Go",
            "{}",
            "Action button clicked",
            "a1",
            [],
            links_generation=5,
        )
        await asyncio.gather(*orch._handler_tasks)
    assert handled.await_args.kwargs["links_generation"] == 5


# ── the switch marker: crash-recoverable sweep ───────────────────────────────


def _marker(orch: Any, former: str, target: str, rows: list[dict]) -> None:
    orch._slack_workspace_state_path.write_text(
        json.dumps({"team_id": former, "pending": {"team_id": target, "swept": rows}})
    )
    orch._slack_links_team_id = former
    orch._slack_workspace_pending = (target, tuple(rows))


@pytest.mark.asyncio
async def test_switch_marker_is_on_disk_before_the_sweep_reaches_it() -> None:
    """Order pin for crash recovery: the record carries the former identity,
    the switch target and a copy of the rows BEFORE ``clear_all_slack_links``
    runs and before its flush -- a process that dies after the sweep's flush
    leaves a marker the next connect can finish or undo from, instead of a
    landed deletion under an identity that still names the former workspace."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    rows = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    orch.sessions.freeze_slack_links.return_value = rows
    order: list[str] = []

    def _sweep() -> list[str]:
        order.append(f"sweep pending={_pending_team(orch)!r}")
        return ["dashboard:one"]

    async def _flush() -> None:
        order.append(f"flush pending={_pending_team(orch)!r}")

    orch.sessions.clear_all_slack_links.side_effect = _sweep
    orch.sessions.aflush.side_effect = _flush

    with _team("T0NEW"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    assert order == ["sweep pending='T0NEW'", "flush pending='T0NEW'"]
    on_disk = json.loads(orch._slack_workspace_state_path.read_text())
    assert on_disk == {"team_id": "T0NEW"}  # finished: marker gone, identity adopted
    assert orch._slack_workspace_pending is None


@pytest.mark.asyncio
async def test_marker_carries_the_rows_it_is_about_to_sweep() -> None:
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    rows = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    orch.sessions.freeze_slack_links.return_value = rows
    seen: list[dict] = []

    def _sweep() -> list[str]:
        seen.append(json.loads(orch._slack_workspace_state_path.read_text()))
        return ["dashboard:one"]

    orch.sessions.clear_all_slack_links.side_effect = _sweep
    with _team("T0NEW"):
        await _run(orch, _Recorder())
    assert seen == [{"team_id": "T0FORMER", "pending": {"team_id": "T0NEW", "swept": rows}}]


@pytest.mark.asyncio
async def test_unwritable_marker_refuses_before_anything_is_swept() -> None:
    """The marker is what makes the sweep recoverable; without it nothing is
    swept -- the rows and the identity both stay, and the attempt is refused."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.sessions.freeze_slack_links.return_value = [
        {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}
    ]
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")
    with _team("T0NEW"), _store_fails():
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unrecorded"}
    orch.sessions.clear_all_slack_links.assert_not_called()
    orch.sessions.aflush.assert_not_awaited()
    orch.sessions.restore_slack_links.assert_not_called()
    assert orch._slack_links_team_id == "T0FORMER"
    assert orch._slack_workspace_pending is None
    assert orch.slack is None


@pytest.mark.asyncio
async def test_interrupted_switch_is_finished_when_the_target_workspace_connects() -> None:
    """Crash after the sweep's flush, before the adopting write: the next
    connect on the TARGET workspace's credentials finds the marker, re-runs
    the (now empty) sweep and adopts the identity -- the state a completed
    switch would have reached."""
    orch = _orch()
    rows = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    _marker(orch, "T0FORMER", "T0NEW", rows)
    orch.sessions.freeze_slack_links.return_value = []  # the sweep already landed
    orch.sessions.clear_all_slack_links.return_value = []

    with _team("T0NEW"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    orch.sessions.restore_slack_links.assert_not_called()
    orch.sessions.clear_all_slack_links.assert_called_once_with()
    assert orch._slack_links_team_id == "T0NEW"
    assert json.loads(orch._slack_workspace_state_path.read_text()) == {"team_id": "T0NEW"}
    assert orch._slack_workspace_pending is None


@pytest.mark.asyncio
async def test_interrupted_switch_is_undone_when_the_former_workspace_connects() -> None:
    """Same crash, but the operator reverted the credentials: the next connect
    names the RECORDED workspace again, so the swept rows -- this workspace's
    mirrors -- come back from the marker's copy, are flushed, and the marker is
    cleared. Nothing is swept, nothing is a switch."""
    orch = _orch()
    rows = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    _marker(orch, "T0FORMER", "T0NEW", rows)
    orch.sessions.restore_slack_links.return_value = ["dashboard:one"]
    order: list[str] = []
    orch.sessions.restore_slack_links.side_effect = lambda got: order.append(
        f"restore {got == rows}"
    ) or ["dashboard:one"]

    async def _flush() -> None:
        order.append(f"flush pending={_pending_team(orch)!r}")

    orch.sessions.aflush.side_effect = _flush
    orch.dashboard_state.rehydrate_slack_links.side_effect = lambda: order.append(
        f"dashboard rehydrate pending={_pending_team(orch)!r}"
    ) or ["chat-1"]

    with _team("T0FORMER"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    # Restored and flushed while the marker still stands, the dashboard's
    # copies rebuilt from the restored rows (it is hydrated already and reads
    # the map only when a slot is created), then the marker goes.
    assert order == [
        "restore True",
        "flush pending='T0NEW'",
        "dashboard rehydrate pending='T0NEW'",
    ]
    orch.sessions.clear_all_slack_links.assert_not_called()
    assert orch._slack_links_team_id == "T0FORMER"
    assert json.loads(orch._slack_workspace_state_path.read_text()) == {"team_id": "T0FORMER"}
    assert orch._slack_workspace_pending is None
    assert orch.slack is not None  # published: this is the ordinary no-switch case now


@pytest.mark.asyncio
async def test_undoing_an_interrupted_switch_refuses_when_the_marker_cannot_be_cleared() -> None:
    orch = _orch()
    rows = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    _marker(orch, "T0FORMER", "T0NEW", rows)
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0FORMER"), _store_fails():
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unrecorded"}
    orch.sessions.restore_slack_links.assert_called_once_with(rows)
    assert orch._slack_workspace_pending == ("T0NEW", tuple(rows))  # left for the next connect
    assert orch.slack is None


@pytest.mark.asyncio
async def test_interrupted_switch_to_a_third_workspace_sweeps_as_a_switch() -> None:
    """Credentials for yet another workspace: the marked rows were the former
    workspace's and a switch sweeps them anyway; the marker is rewritten for
    the new target and dropped when it is adopted."""
    orch = _orch()
    rows = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    _marker(orch, "T0FORMER", "T0NEW", rows)
    orch.sessions.freeze_slack_links.return_value = []
    orch.sessions.clear_all_slack_links.return_value = []

    with _team("T0THIRD"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    orch.sessions.restore_slack_links.assert_not_called()
    orch.sessions.clear_all_slack_links.assert_called_once_with()
    assert orch._slack_links_team_id == "T0THIRD"
    assert json.loads(orch._slack_workspace_state_path.read_text()) == {"team_id": "T0THIRD"}


def test_marker_is_read_at_boot(tmp_path: Path) -> None:
    from kiro_crew.slack.gateway import _load_slack_workspace_record

    path = tmp_path / "slack_workspace.json"
    rows = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    path.write_text(json.dumps({"team_id": "T0A", "pending": {"team_id": "T0B", "swept": rows}}))
    record = _load_slack_workspace_record(path)
    assert record is not None
    assert (record.team_id, record.pending_team_id, list(record.pending_swept)) == (
        "T0A",
        "T0B",
        rows,
    )
    path.write_text(json.dumps({"team_id": "T0A"}))
    assert _load_slack_workspace_record(path) == type(record)("T0A")


@pytest.mark.parametrize(
    "pending",
    [
        "T0B",  # not an object
        {"team_id": "T0B"},  # no rows
        {"team_id": "", "swept": []},  # empty target
        {"team_id": "T0B", "swept": "rows"},  # rows not a list
        {"team_id": "T0B", "swept": [1]},  # a row that is not an object
    ],
)
def test_half_a_marker_reads_as_a_damaged_record(tmp_path: Path, pending: Any) -> None:
    """A marker that cannot be finished or undone is damage, not absence:
    the loader answers None and the bind refuses until it is repaired."""
    from kiro_crew.slack.gateway import _load_slack_links_team_id, _load_slack_workspace_record

    path = tmp_path / "slack_workspace.json"
    path.write_text(json.dumps({"team_id": "T0A", "pending": pending}))
    assert _load_slack_workspace_record(path) is None
    assert _load_slack_links_team_id(path) is None


def test_marker_record_still_validates_as_a_snapshot_json_object(tmp_path: Path) -> None:
    """The snapshot component validates the record as a JSON object; the
    marker keeps that shape, so a bundle taken mid-switch still restores."""
    from kiro_crew.slack.gateway import _store_slack_links_team_id

    path = tmp_path / "slack_workspace.json"
    _store_slack_links_team_id(
        path, "T0A", pending=("T0B", [{"key": "k", "slack_thread_ts": "1.1"}])
    )
    assert isinstance(json.loads(path.read_text()), dict)


# ── the dashboard's copies of the links go with the switch ───────────────────


@pytest.mark.asyncio
async def test_a_switch_drops_the_dashboard_link_state_with_the_sweep() -> None:
    """Each slot's linked flag / thread / channel and the thread reverse index
    are the dashboard's own copies of the swept rows; they go WITH the sweep,
    before the socket connects and the client is published, so no approval
    prompt or thread lookup can offer a former-workspace destination to the
    new client. The undo rebuilds them from the map (see the failed-connect test)."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.sessions.freeze_slack_links.return_value = [
        {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}
    ]
    seen: list[str] = []

    def _forget() -> list[str]:
        seen.append(
            f"forget recorded={_recorded_team(orch)!r} pending={_pending_team(orch)!r} "
            f"client={orch.dashboard_state.slack_client!r} slack={orch.slack!r}"
        )
        return ["chat-1"]

    orch.dashboard_state.forget_slack_links.side_effect = _forget
    with _team("T0NEW"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    # With the sweep (marker on disk, identity not yet adopted), before publish.
    assert seen == ["forget recorded='T0FORMER' pending='T0NEW' client=None slack=None"]


@pytest.mark.asyncio
async def test_first_record_and_same_workspace_keep_the_dashboard_link_state() -> None:
    for previous, current in (("", "T0NEW"), ("T0SAME", "T0SAME")):
        orch = _orch()
        orch._slack_links_team_id = previous
        with _team(current):
            await _run(orch, _Recorder())
        orch.dashboard_state.forget_slack_links.assert_not_called()


def test_dashboard_forget_slack_links_clears_every_slot_and_the_index() -> None:
    from kiro_crew.dashboard.state import DashboardState

    ds = DashboardState.__new__(DashboardState)
    linked = MagicMock(name="linked")
    linked._slack_linked, linked._slack_thread_ts, linked._slack_channel = True, "171.1", "C0OLD"
    plain = MagicMock(name="plain")
    plain._slack_linked, plain._slack_thread_ts, plain._slack_channel = False, "", ""
    ds._slots = {"chat-1": linked, "chat-2": plain}
    ds._slack_to_slot = {"171.1": "chat-1"}
    ds.push_slots_update = MagicMock()  # type: ignore[method-assign]

    assert ds.forget_slack_links() == ["chat-1"]
    assert (linked._slack_linked, linked._slack_thread_ts, linked._slack_channel) == (
        False,
        "",
        "",
    )
    assert ds._slack_to_slot == {}
    ds.push_slots_update.assert_called_once()
    # Nothing linked: nothing to push.
    ds.push_slots_update.reset_mock()
    assert ds.forget_slack_links() == []
    ds.push_slots_update.assert_not_called()


def test_dashboard_rehydrate_rebuilds_every_slot_from_the_map(tmp_path: Any) -> None:
    """The undo of an interrupted switch puts the map's rows back under a
    running dashboard whose slots read unlinked: every slot is re-read from
    the map -- fields and the thread reverse index -- exactly as a slot is
    hydrated when created; a slot without a map link is cleared; a
    self-referencing channel-born row is not indexed; nothing pushed when
    nothing changed."""
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    one = state.get_or_create_slot("one")
    two = state.get_or_create_slot("two")
    state.get_or_create_slot("plain")
    assert state.link_slack("one", "171.1", "C0ONE") and state.link_slack("two", "172.1", "C0TWO")
    # The dashboard forgets (a switch's sweep) while the map is put back
    # behind its back: the fields are stale, the map is the truth.
    # A channel-born slot: its thread is the one it lives IN, a self-reference
    # the map holds under its own key. Its fields hydrate; it is never indexed.
    born = state.get_or_create_slot("slack_180.1")
    born.linked_session_key = "slack:180.1"
    state.sessions.set_slack_link("slack:180.1", "180.1", "C0BORN")
    state.forget_slack_links()
    state.sessions.set_slack_link("dashboard:two", "", "")  # the map dropped two's link
    state.push_slots_update = MagicMock()

    assert state.rehydrate_slack_links() == ["one", "slack_180.1"]
    assert (one._slack_linked, one._slack_thread_ts, one._slack_channel) == (
        True,
        "171.1",
        "C0ONE",
    )
    assert (two._slack_linked, two._slack_thread_ts, two._slack_channel) == (False, "", "")
    assert born._slack_thread_ts == "180.1"
    assert state._slack_to_slot == {"171.1": "one"}  # the self-reference is NOT indexed
    state.push_slots_update.assert_called_once()
    state.push_slots_update.reset_mock()
    assert state.rehydrate_slack_links() == ["one", "slack_180.1"]  # unchanged: no push
    state.push_slots_update.assert_not_called()


# ── a turn's mirror sends go through the client its destination was read with ─


def test_every_mirror_send_uses_the_client_captured_with_the_destination() -> None:
    """Source pin over ``chat_runner``: the destination (``get_slack_link``) and
    the client are read together into turn-local names, and every Slack call
    the turn makes afterwards -- the echo, the stream, task appends, the
    reply, the OPTIONS card, the stream teardown, the approval prompt and its
    cleanup -- goes through those, never through the live ``state.slack_client``
    a Reconnect may have replaced with another workspace's client mid-turn."""
    import inspect
    import re

    from kiro_crew.dashboard import chat_runner

    src = inspect.getsource(chat_runner)
    live_calls = [
        line.strip() for line in src.splitlines() if re.search(r"state\.slack_client\.\w+\(", line)
    ]
    assert live_calls == []
    # The client is captured from the settled read (``DashboardState
    # .settled_slack_client``), pinned together with the destination: the
    # destination is read right AFTER the client settles, nothing awaited
    # between them, so a switch's sweep is never straddled.
    capture = src.index("_mirror_client = await _settled_slack_client_for_turn(state)")
    read = src.index("_mirror_thread, _mirror_chan = state.sessions.get_slack_link(session_key)")
    assert capture < read < capture + 400  # the same read, the destination AFTER the client
    assert src.count("await _mirror_client.") >= 8
    # The approval prompt's client is resolved the way the mirror leg's is --
    # the live handle, or the settled read while a linked slot's client is
    # withheld -- BEFORE the approval future is registered, and captured with
    # its channel for the cleanup.
    resolve = src.index("_slack_approval_candidate = state.slack_client")
    register = src.index("slot.register_approval(str(event.request_id), fut, permission_row)")
    assert resolve < register
    assert 'getattr(state, "slack_client_withheld", None)' in src[resolve:register]
    assert "await _settled_slack_client_for_turn(state)" in src[resolve:register]
    assert "_slack_approval_client = _slack_approval_candidate" in src
    assert "await _slack_approval_client.delete_message(" in src


# ── a retried switch keeps the earlier marker's rows ─────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["T0NEW", "T0THIRD"])
async def test_retried_switch_carries_the_earlier_markers_rows_forward(target: str) -> None:
    """After a crash between the sweep's flush and the adopting write, the rows
    are gone from the map and live only in the marker's copy. A retried switch
    -- to the same target or another -- snapshots an empty map; its new marker
    must still carry those rows, or the only surviving copy is overwritten by
    an empty sweep and a later return to the former workspace restores nothing
    while reporting success."""
    orch = _orch()
    rows = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    _marker(orch, "T0FORMER", "T0NEW", rows)
    orch.sessions.freeze_slack_links.return_value = []  # already swept
    orch.sessions.clear_all_slack_links.return_value = []
    markers: list[dict] = []

    def _sweep() -> list[str]:
        markers.append(json.loads(orch._slack_workspace_state_path.read_text()))
        return []

    orch.sessions.clear_all_slack_links.side_effect = _sweep
    with _team(target):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    assert markers == [{"team_id": "T0FORMER", "pending": {"team_id": target, "swept": rows}}]
    assert json.loads(orch._slack_workspace_state_path.read_text()) == {"team_id": target}


@pytest.mark.asyncio
async def test_retried_switch_that_fails_to_adopt_restores_the_carried_rows() -> None:
    """The graceful failure of a retried switch (adopting write refused) puts
    back the carried rows too, since the former workspace stays recorded."""
    orch = _orch()
    rows = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    _marker(orch, "T0FORMER", "T0NEW", rows)
    orch.sessions.freeze_slack_links.return_value = []
    orch.sessions.clear_all_slack_links.return_value = []
    orch.sessions.restore_slack_links.return_value = ["dashboard:one"]

    with _team("T0NEW"), _final_store_fails():
        result = await _run(orch, _Recorder())

    assert result["connect_error"] == "workspace_identity_unrecorded"
    orch.sessions.restore_slack_links.assert_called_once_with(rows)
    assert orch._slack_workspace_pending == ("T0NEW", tuple(rows))


@pytest.mark.asyncio
async def test_carry_forward_prefers_the_rows_the_map_still_holds() -> None:
    """A key present in both the marker's copy and the map's current snapshot
    rides once, with the map's (current) fields."""
    orch = _orch()
    old = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    _marker(orch, "T0FORMER", "T0NEW", old)
    current = [
        {"key": "dashboard:one", "slack_thread_ts": "171.9", "slack_channel_id": "C0RELINKED"},
        {"key": "dashboard:two", "slack_thread_ts": "172.1", "slack_channel_id": "C0OLD"},
    ]
    orch.sessions.freeze_slack_links.return_value = current
    markers: list[dict] = []

    def _sweep() -> list[str]:
        markers.append(json.loads(orch._slack_workspace_state_path.read_text())["pending"]["swept"])
        return ["dashboard:one", "dashboard:two"]

    orch.sessions.clear_all_slack_links.side_effect = _sweep
    with _team("T0NEW"):
        await _run(orch, _Recorder())
    assert markers == [current]


# ── a restore rebuilds the reverse index with the load-time tie-break ────────


@pytest.mark.parametrize("fork_first", [True, False])
def test_restore_resolves_a_contested_thread_like_the_load_path(
    tmp_path: Any, fork_first: bool
) -> None:
    """Two rows can claim one thread: the session that created it and the
    ``slack:<ts>`` fork an inbound reply minted. A per-row index write would
    let the rows' ORDER pick the owner; the restore rebuilds the index with the
    same tie-break the load path applies, so the thread routes to the creator
    whichever row came first in the copy."""
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("dashboard:one", "sid-1")
        smap.set("slack:171.1", "sid-2")
        rows = [
            {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0"},
            {"key": "slack:171.1", "slack_thread_ts": "171.1", "slack_channel_id": "C0"},
        ]
        # The rows a sweep took (and stamped): the restore honours only those.
        smap.set_slack_link("dashboard:one", "171.1", "C0")
        smap.set_slack_link("slack:171.1", "171.1", "C0")
        assert sorted(smap.clear_all_slack_links()) == ["dashboard:one", "slack:171.1"]
        if fork_first:
            rows.reverse()

        restored = smap.restore_slack_links(rows)

        assert sorted(restored) == ["dashboard:one", "slack:171.1"]
        assert smap.get_session_for_thread("171.1") == "dashboard:one"
        # And the same answer a fresh load of the file gives.
        reloaded = SessionMap()
        assert reloaded.get_session_for_thread("171.1") == "dashboard:one"


def test_restore_writes_no_index_entry_per_row() -> None:
    """Source pin: the reverse index is rebuilt once after the loop, never
    assigned per restored row."""
    import inspect

    from kiro_crew.session_map import SessionMap

    src = inspect.getsource(SessionMap.restore_slack_links)
    assert "self._thread_to_session[" not in src
    assert src.index("self._rebuild_thread_index()") < src.index("self._save()")


def test_reconnect_route_is_wired_only_after_boots_own_bind() -> None:
    """Source pin: ``dashboard_state._slack_reconnect`` is assigned in ``run``
    AFTER boot's ``_connect_slack`` and ``_bind_slack_workspace(source="boot")``,
    and nowhere in ``_init_dashboard``. A Reconnect admitted before boot's own
    handshake would race it: two Socket Mode clients on one app token, the first
    overwritten and never closed, or -- had that Reconnect failed -- boot's
    listener bound to no Web API client while the badge reads connected. Until
    the wiring lands the route answers ``slack_reconnect_unavailable``."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    init_dashboard = inspect.getsource(GatewayOrchestrator._init_dashboard)
    assert "_slack_reconnect = self.reconnect_slack" not in init_dashboard
    run = inspect.getsource(GatewayOrchestrator.run)
    boot = run.index('self._connect_admitted_slack_socket(source="boot")')
    wired = run.index("self.dashboard_state._slack_reconnect = self.reconnect_slack")
    assert boot < wired
    code = [line for line in run.splitlines() if "#" not in line]
    assert not any("_bind_slack_workspace(" in line or "_connect_slack()" in line for line in code)
    # Boot and Reconnect share the ONE bind-then-connect sequence, guarded on a
    # socket having been built (see ``test_the_admitted_connect_binds_before_it_connects``).
    reconnect = inspect.getsource(GatewayOrchestrator._reconnect_slack_withdrawn)
    assert 'self._connect_admitted_slack_socket(source="reconnect")' in reconnect
    code = [line for line in reconnect.splitlines() if "#" not in line]
    assert not any("_bind_slack_workspace(" in line or "_connect_slack()" in line for line in code)
    assert run.count("_slack_reconnect = self.reconnect_slack") == 1


# ── the record is read on the first bind, off-loop, bounded and well-typed ───


@pytest.mark.asyncio
async def test_first_bind_reads_the_record_from_disk() -> None:
    """Construction leaves the identity empty; the first bind reads the file
    (former identity and marker alike) before comparing workspaces."""
    orch = _orch()
    orch._slack_workspace_record_loaded = False
    rows = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    orch._slack_workspace_state_path.write_text(
        json.dumps({"team_id": "T0FORMER", "pending": {"team_id": "T0NEW", "swept": rows}})
    )
    orch.sessions.restore_slack_links.return_value = ["dashboard:one"]

    with _team("T0FORMER"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    # The marker on disk was seen and undone, so the read happened here.
    orch.sessions.restore_slack_links.assert_called_once_with(rows)
    assert orch._slack_links_team_id == "T0FORMER"
    assert orch._slack_workspace_record_loaded is True


@pytest.mark.asyncio
async def test_first_bind_with_no_record_records_the_first_identity() -> None:
    orch = _orch()
    orch._slack_workspace_record_loaded = False
    with _team("T0NEW"):
        result = await _run(orch, _Recorder())
    assert result["connected"] is True
    orch.sessions.clear_all_slack_links.assert_not_called()
    assert _recorded_team(orch) == "T0NEW"


@pytest.mark.asyncio
async def test_first_bind_on_a_damaged_record_refuses_and_rereads_next_time() -> None:
    orch = _orch()
    orch._slack_workspace_record_loaded = False
    orch._slack_workspace_state_path.write_text("{ not json")
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"):
        first = await _run(orch, recorder)
    assert first["connect_error"] == "workspace_record_unreadable"
    assert orch._slack_workspace_record_damaged is True
    # Repaired (removed) by the operator: the next bind re-reads and proceeds.
    orch._slack_workspace_state_path.unlink()
    with _team("T0NEW"):
        second = await _run(orch, _Recorder())
    assert second["connected"] is True
    assert orch._slack_workspace_record_damaged is False


@pytest.mark.parametrize(
    "row, defect",
    [
        ("not a row", "row is not an object"),
        ({"slack_thread_ts": "171.1"}, "'key' is not a non-empty string"),
        ({"key": "dashboard:one"}, "row names neither a thread nor a Slack conversation"),
        (
            {"key": "dashboard:one", "slack_thread_ts": 171},
            "'slack_thread_ts' is not a string or null",
        ),
        (
            # A thread-less row whose channel is the non-Slack namespaced bucket
            # names nothing the sweep removes.
            {"key": "dashboard:one", "slack_thread_ts": "", "slack_channel_id": "discord:123"},
            "row names neither a thread nor a Slack conversation",
        ),
        (
            {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": {"id": 1}},
            "'slack_channel_id' is not a string or null",
        ),
        (
            {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_link_nonce": 7},
            "'slack_link_nonce' is not a string",
        ),
        (
            {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_paused": "yes"},
            "'slack_paused' is not a boolean",
        ),
        (
            {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C" * 1100},
            "'slack_channel_id' exceeds 1024 characters",
        ),
    ],
)
def test_a_malformed_swept_row_is_a_defect(row: Any, defect: str) -> None:
    """Every field the restore hands a consumer is type-checked, and every
    string is bounded: an object-valued channel id would otherwise reach the
    dashboard's link regex; an unbounded field is a record that grows without
    limit on the connect path."""
    from kiro_crew.slack.workspace_record import slack_switch_row_defect

    assert slack_switch_row_defect(row) == defect


def test_a_well_formed_swept_row_has_no_defect() -> None:
    from kiro_crew.slack.workspace_record import slack_switch_row_defect

    assert (
        slack_switch_row_defect(
            {
                "key": "dashboard:one",
                "slack_thread_ts": "171.1",
                "slack_channel_id": None,
                "slack_link_nonce": "n1",
                "slack_paused": True,
            }
        )
        is None
    )


def test_a_marker_with_a_malformed_row_reads_as_damaged(tmp_path: Path) -> None:
    from kiro_crew.slack.gateway import _load_slack_workspace_record

    path = tmp_path / "slack_workspace.json"
    bad = {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": {"id": 1}}
    path.write_text(json.dumps({"team_id": "T0A", "pending": {"team_id": "T0B", "swept": [bad]}}))
    assert _load_slack_workspace_record(path) is None


def test_the_store_never_filters_or_truncates_the_recovery_copy(tmp_path: Path) -> None:
    """The marker is the ONLY copy an undo restores from: a row the reader
    would refuse, or more rows than the cap, fails the WRITE and nothing is
    written -- never a marker quietly missing a row an undo would then fail to
    restore. The caller refuses such a switch before sweeping anything."""
    from kiro_crew.slack.gateway import _load_slack_workspace_record, _store_slack_links_team_id
    from kiro_crew.slack.workspace_record import SLACK_SWITCH_MARKER_MAX_ROWS

    path = tmp_path / "slack_workspace.json"
    rows: list[dict[str, object]] = [
        {"key": f"dashboard:{i}", "slack_thread_ts": f"{i}.1", "slack_channel_id": "C0"}
        for i in range(3)
    ]
    _store_slack_links_team_id(path, "T0A", pending=("T0B", rows))
    record = _load_slack_workspace_record(path)
    assert record is not None and [r["key"] for r in record.pending_swept] == [
        "dashboard:0",
        "dashboard:1",
        "dashboard:2",
    ]

    with_bad = rows + [{"key": "dashboard:bad", "slack_thread_ts": "9.9", "slack_channel_id": 5}]
    with pytest.raises(ValueError, match="slack_channel_id"):
        _store_slack_links_team_id(path, "T0A", pending=("T0B", with_bad))
    too_many = [
        {"key": f"dashboard:{i}", "slack_thread_ts": f"{i}.1"}
        for i in range(SLACK_SWITCH_MARKER_MAX_ROWS + 1)
    ]
    with pytest.raises(ValueError, match="more than"):
        _store_slack_links_team_id(path, "T0A", pending=("T0B", too_many))
    assert len(json.loads(path.read_text())["pending"]["swept"]) == 3  # untouched


@pytest.mark.asyncio
async def test_a_switch_sweeping_a_row_the_marker_cannot_hold_is_refused_before_sweeping() -> None:
    """A row of a shape the marker does not retain (no in-process writer
    produces one; a hand-edited map can) would be dropped from the only copy
    an undo restores from. The switch is refused instead, with every link and
    the identity intact -- the same refusal as too many rows, named apart."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.sessions.freeze_slack_links.return_value = [
        {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"},
        {"key": "dashboard:odd", "slack_thread_ts": "171.2", "slack_channel_id": {"id": 1}},
    ]
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"):
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_switch_unrecordable"}
    orch.sessions.clear_all_slack_links.assert_not_called()
    orch.sessions.aflush.assert_not_awaited()
    assert orch._slack_links_team_id == "T0FORMER"
    assert _recorded_team(orch) is None
    assert orch.slack is None


def test_a_session_key_at_the_validators_ceiling_fits_the_marker() -> None:
    """The field cap sits above the longest key any writer produces (512), so a
    row the map holds is never one the marker cannot hold."""
    from kiro_crew.slack.workspace_record import (
        SLACK_SWITCH_MARKER_MAX_FIELD_CHARS,
        slack_switch_row_defect,
    )

    assert SLACK_SWITCH_MARKER_MAX_FIELD_CHARS >= 512
    assert slack_switch_row_defect({"key": "k" * 512, "slack_thread_ts": "171.1"}) is None


@pytest.mark.asyncio
async def test_a_switch_that_would_not_fit_the_marker_is_refused_before_sweeping() -> None:
    """The marker is the only copy an undo can restore from and it is bounded:
    a switch whose sweep exceeds the cap is refused with every link and the
    identity intact -- never swept with a marker that forgets the rest."""
    from kiro_crew.slack.workspace_record import SLACK_SWITCH_MARKER_MAX_ROWS

    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.sessions.freeze_slack_links.return_value = [
        {"key": f"dashboard:{i}", "slack_thread_ts": f"{i}.1", "slack_channel_id": "C0OLD"}
        for i in range(SLACK_SWITCH_MARKER_MAX_ROWS + 1)
    ]
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"):
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_switch_too_large"}
    orch.sessions.clear_all_slack_links.assert_not_called()
    orch.sessions.aflush.assert_not_awaited()
    assert orch._slack_links_team_id == "T0FORMER"
    assert _recorded_team(orch) is None  # no marker written either
    assert orch._slack_workspace_pending is None
    recorder.client.close.assert_awaited_once()
    assert orch.slack is None
    assert orch.dashboard_state.slack_connect_error == "workspace_switch_too_large"


@pytest.mark.asyncio
async def test_a_retried_switch_counts_the_carried_rows_against_the_cap() -> None:
    from kiro_crew.slack.workspace_record import SLACK_SWITCH_MARKER_MAX_ROWS

    orch = _orch()
    carried = [
        {"key": f"dashboard:{i}", "slack_thread_ts": f"{i}.1"}
        for i in range(SLACK_SWITCH_MARKER_MAX_ROWS)
    ]
    _marker(orch, "T0FORMER", "T0NEW", carried)
    orch.sessions.freeze_slack_links.return_value = [
        {"key": "dashboard:new", "slack_thread_ts": "999.1"}
    ]
    with _team("T0NEW"):
        result = await _run(orch, _Recorder())
    assert result["connect_error"] == "workspace_switch_too_large"
    orch.sessions.clear_all_slack_links.assert_not_called()


@pytest.mark.parametrize(
    "row, defect",
    [
        (
            {"key": "dashboard:one", "slack_thread_ts": "171.1", "extra": {"big": "x" * 10}},
            "unknown field(s) 'extra'",
        ),
        (
            {"key": "dashboard:one", "slack_thread_ts": "171.1", "a": 1, "b": 2},
            "unknown field(s) 'a', 'b'",
        ),
    ],
)
def test_a_row_with_an_unknown_field_is_a_defect(row: dict, defect: str) -> None:
    """A field the restore never reads is one nothing bounds: a nested
    container of any size would ride in the marker past every string cap."""
    from kiro_crew.slack.workspace_record import slack_switch_row_defect

    assert slack_switch_row_defect(row) == defect


@pytest.mark.parametrize(
    "parsed, defect",
    [
        ({"team_id": "T" * 1100}, "'team_id' exceeds 1024 characters"),
        ({"team_id": "T0A", "junk": [1] * 5}, "unknown field(s) 'junk'"),
        (
            {"team_id": "T0A", "pending": {"team_id": "T0B", "swept": [], "blob": "x"}},
            "'pending' has unknown field(s) 'blob'",
        ),
    ],
)
def test_the_record_bounds_and_allowlists_its_own_fields(parsed: dict, defect: str) -> None:
    from kiro_crew.slack.workspace_record import slack_workspace_record_defect

    assert slack_workspace_record_defect(parsed) == defect


def test_a_marker_over_the_row_cap_reads_as_damaged(tmp_path: Path) -> None:
    from kiro_crew.slack.gateway import _load_slack_workspace_record
    from kiro_crew.slack.workspace_record import SLACK_SWITCH_MARKER_MAX_ROWS

    path = tmp_path / "slack_workspace.json"
    rows = [
        {"key": f"dashboard:{i}", "slack_thread_ts": f"{i}.1"}
        for i in range(SLACK_SWITCH_MARKER_MAX_ROWS + 1)
    ]
    path.write_text(json.dumps({"team_id": "T0A", "pending": {"team_id": "T0B", "swept": rows}}))
    assert _load_slack_workspace_record(path) is None


def test_workspace_record_module_is_a_leaf() -> None:
    """The shape module is imported by the snapshot facade and by the gateway;
    it must pull neither onto the other's path, so it imports only the standard
    library."""
    import ast
    import inspect

    from kiro_crew.slack import workspace_record

    tree = ast.parse(inspect.getsource(workspace_record))
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    assert not any(name.startswith("kiro_crew") for name in imported if name), imported


@pytest.mark.parametrize(
    "row, defect",
    [
        (
            {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": 5},
            "'slack_channel_id' is not a string or null",
        ),
        (
            {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": None},
            None,  # None is a retained value the restore accepts
        ),
    ],
)
def test_every_retained_row_value_is_a_bounded_string_a_bool_or_none(
    row: dict, defect: Any
) -> None:
    from kiro_crew.slack.workspace_record import slack_switch_row_defect

    assert slack_switch_row_defect(row) == defect


def test_no_row_value_type_escapes_the_bound() -> None:
    """Source pin: the value loop refuses anything but str/bool/None, so no
    container can ride in a row whatever its key -- the field allowlist is the
    first fence, this is the second."""
    import inspect

    from kiro_crew.slack import workspace_record

    src = inspect.getsource(workspace_record.slack_switch_row_defect)
    assert "if value is None or isinstance(value, bool):" in src
    assert "if not isinstance(value, str):" in src
    assert "is not a string, a boolean or null" in src
    # Every allowlisted field has its own type check above the loop, so the loop
    # is the second fence for a field the allowlist may gain later; it must
    # refuse, not skip.
    loop = src[src.index("for name, value in row.items():") :]
    non_str = loop[loop.index("if not isinstance(value, str):") :]
    assert non_str.splitlines()[1].strip().startswith("return ")


@pytest.mark.asyncio
async def test_reconnect_publish_reschedules_the_inbound_spool_replay() -> None:
    """A boot without a Slack client left refused-turn entries on the spool;
    once a client is published the replay runs again so senders are told."""
    orch = _orch()
    orch._schedule_inbound_replay = MagicMock(name="_schedule_inbound_replay")

    await _run(orch, _Recorder())
    orch._schedule_inbound_replay.assert_called_once_with()

    orch._schedule_inbound_replay.reset_mock()

    async def _connect(self: Any) -> bool:
        self._slack_connect_error = "invalid_auth"
        return False

    await _run(orch, _Recorder(), connect=_connect)
    orch._schedule_inbound_replay.assert_not_called()  # nothing published, nothing to replay through


def test_boot_reschedules_the_inbound_spool_replay_after_the_publish() -> None:
    """Source pin: the channel-transport start schedules the replay while the
    client is still withheld; ``run`` schedules it once more right after the
    gated publish, and nowhere before it."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    assert "self._schedule_inbound_replay()" in inspect.getsource(
        GatewayOrchestrator._start_channel_transports
    )
    src = inspect.getsource(GatewayOrchestrator.run)
    publish = src.index("self.slack = self._slack_boot_client")
    assert "self._schedule_inbound_replay()" not in src[:publish]
    assert "self._schedule_inbound_replay()" in src[publish:]


def test_approval_cleanup_uses_the_channel_captured_with_its_client() -> None:
    """Source pin: the approval prompt's channel is captured beside its client
    and the cleanup names THAT channel, not the slot's live link field a
    workspace switch has since cleared."""
    import inspect

    from kiro_crew.dashboard import chat_runner

    src = inspect.getsource(chat_runner)
    assert "_slack_approval_channel = slot._slack_channel" in src
    assert "resolve_linked_approval(_slack_approval_channel, _slack_approval_ts)" in src
    assert (
        "delete_message(\n                                    _slack_approval_channel, _slack_approval_ts"
        in src
    )
    assert "resolve_linked_approval(slot._slack_channel" not in src


# ── the switch freezes Slack link writes between its copy and its sweep ──────


def test_freeze_refuses_every_slack_link_write_until_the_thaw(tmp_path: Any) -> None:
    """From the copy the switch marker retains until the switch is durable, no
    Slack link can enter the map -- fenced with the CURRENT generation or
    unfenced alike: before the sweep a row would be swept with no copy in the
    only source an undo restores from; after it, a former-workspace destination
    would be adopted under the new identity with nothing left to remove it. The
    sweep does NOT end the freeze; the thaw does, once the record is written."""
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("slack:171.1", "sid-1")
        smap.set_slack_link("slack:171.1", "171.1", "C0FORMER")
        smap.set("dashboard:late", "sid-2")

        rows = smap.freeze_slack_links()
        assert [r["key"] for r in rows] == ["slack:171.1"]

        # Unfenced (the dashboard's connect row) and fenced with the generation
        # that is still current: both refused, the map unchanged.
        smap.set_slack_link("dashboard:late", "172.1", "C0FORMER")
        smap.set_slack_link(
            "dashboard:late", "172.1", "C0FORMER", generation=smap.slack_links_generation()
        )
        assert smap.get_slack_link("dashboard:late") == (None, None)
        assert smap.snapshot_slack_links() == rows  # exactly the copy the marker holds
        # A CLEAR is not a write the marker could miss and stays allowed.
        assert smap.clear_slack_link("slack:171.1") is True

        assert smap.clear_all_slack_links() == []
        # Still frozen after the sweep: the record is not written yet.
        smap.set_slack_link("dashboard:late", "173.1", "C0NEW")
        assert smap.get_slack_link("dashboard:late") == (None, None)
        smap.thaw_slack_links()
        smap.set_slack_link("dashboard:late", "173.1", "C0NEW")
        assert smap.get_slack_link("dashboard:late") == ("173.1", "C0NEW")


def test_a_restore_puts_back_only_rows_the_sweep_cleared_and_nobody_touched_since(
    tmp_path: Any,
) -> None:
    """A clear is allowed through the freeze, so a user can unlink while a
    switch is in flight -- before the sweep (the row is in the copy but gone
    from the map when the sweep runs) or after it (already empty). The sweep
    stamps every row IT clears; a user's clear and a new binding drop the
    stamp; the rollback restores only stamped rows, so the user's unlink
    stands and a binding made after the thaw is not overwritten. Stamps are
    persisted, so the undo at a later connect honours them too."""
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        for n, name in enumerate(("kept", "unlinked_before", "unlinked_after", "relinked")):
            smap.set(f"dashboard:{name}", f"sid-{name}")
            smap.set_slack_link(f"dashboard:{name}", f"17{n}.1", "C0FORMER")
        rows = smap.freeze_slack_links()
        assert {r["key"] for r in rows} == {
            "dashboard:kept",
            "dashboard:unlinked_before",
            "dashboard:unlinked_after",
            "dashboard:relinked",
        }
        assert smap.clear_slack_link("dashboard:unlinked_before") is True  # inside the freeze
        swept = smap.clear_all_slack_links()
        assert set(swept) == {"dashboard:kept", "dashboard:unlinked_after", "dashboard:relinked"}
        assert smap.clear_slack_link("dashboard:unlinked_after") is False  # already empty
        smap.thaw_slack_links()
        smap.set_slack_link("dashboard:relinked", "199.1", "C0NEW")

        reloaded = SessionMap()  # the stamps survive a restart
        assert reloaded.restore_slack_links(rows) == ["dashboard:kept"]
        assert reloaded.get_slack_link("dashboard:kept") == ("170.1", "C0FORMER")
        assert reloaded.get_slack_link("dashboard:unlinked_before") == (None, None)
        assert reloaded.get_slack_link("dashboard:unlinked_after") == (None, None)
        assert reloaded.get_slack_link("dashboard:relinked") == ("199.1", "C0NEW")
        # The stamp is consumed by the restore: a second restore is a no-op.
        assert reloaded.restore_slack_links(rows) == []


def test_thaw_lets_writes_resume_with_every_link_intact(tmp_path: Any) -> None:
    """A switch refused before sweeping thaws: the copied rows are all still in
    the map, the generation has not moved, and writers resume under it."""
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("slack:171.1", "sid-1")
        smap.set_slack_link("slack:171.1", "171.1", "C0FORMER")
        generation = smap.slack_links_generation()
        rows = smap.freeze_slack_links()
        smap.thaw_slack_links()
        assert smap.snapshot_slack_links() == rows
        assert smap.slack_links_generation() == generation
        smap.set("dashboard:late", "sid-2")
        smap.set_slack_link("dashboard:late", "172.1", "C0FORMER", generation=generation)
        assert smap.get_slack_link("dashboard:late") == ("172.1", "C0FORMER")


def test_set_slack_link_answers_whether_the_binding_stands(tmp_path: Any) -> None:
    """The map's writer returns True when the binding is in the map afterwards
    (written, or already identical) and False on a refusal -- a stale
    generation or the freeze -- so a caller that redraws its own copies can
    stop instead of reporting a link that was never persisted. ``SessionManager``
    forwards the answer, and says WHY through ``slack_links_frozen``."""
    from kiro_crew.session import SessionManager
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("dashboard:one", "sid-1")
        assert smap.set_slack_link("dashboard:one", "171.1", "C0CHAN") is True
        assert smap.set_slack_link("dashboard:one", "171.1", "C0CHAN") is True  # identical
        stale = smap.slack_links_generation()
        smap.clear_all_slack_links()
        assert smap.set_slack_link("dashboard:one", "171.1", "C0CHAN", generation=stale) is False
        assert smap.slack_links_frozen() is False
        smap.freeze_slack_links()
        assert smap.slack_links_frozen() is True
        assert smap.set_slack_link("dashboard:one", "172.1", "C0CHAN") is False
        assert smap.get_slack_link("dashboard:one") == (None, None)
        smap.thaw_slack_links()
        assert smap.set_slack_link("dashboard:one", "172.1", "C0CHAN") is True

    mgr = SessionManager.__new__(SessionManager)
    mgr._session_map = MagicMock(name="session_map")
    mgr._session_map.set_slack_link.return_value = False
    mgr._session_map.slack_links_frozen.return_value = True
    assert mgr.set_slack_link("k", "171.1", "C0CHAN") is False
    assert mgr.slack_links_frozen() is True


def test_a_refused_dashboard_link_changes_nothing_and_says_so(tmp_path: Any) -> None:
    """``DashboardState.link_slack`` writes the map FIRST and returns its answer:
    on a refusal (the freeze) no slot field, no reverse-index entry and no
    slots push -- a slot redrawn as linked over a map that holds no link is a
    report a restart contradicts. The claim that was refused also did not
    evict the thread's previous owner."""
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    owner = state.get_or_create_slot("owner")
    late = state.get_or_create_slot("late")
    assert state.link_slack("owner", "171.1", "C0CHAN") is True
    state.push_slots_update = MagicMock()
    state.sessions.set_slack_link = MagicMock(return_value=False)

    assert state.link_slack("late", "171.1", "C0CHAN") is False

    assert (late._slack_channel, late._slack_thread_ts) == ("", "")
    assert (owner._slack_channel, owner._slack_thread_ts) == ("C0CHAN", "171.1")
    assert state._slack_to_slot["171.1"] == "owner"
    state.push_slots_update.assert_not_called()
    # The refused claim wrote nothing for the previous owner either: the one
    # map call was the claim itself.
    state.sessions.set_slack_link.assert_called_once()
    assert state.link_slack("nope", "171.1", "C0CHAN") is False


@pytest.mark.asyncio
async def test_the_link_route_answers_a_refusal_instead_of_ok(tmp_path: Any) -> None:
    """``POST /api/chat/slots/{slot}/slack-link`` during a workspace switch:
    503, no ``{ok}``, no transcript backfilled into the thread, no slots push,
    and the refusal is in the access log."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_slack

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hello")
    slot.drain()
    state.slack_client = MagicMock()
    state.slack_client.open_dm = AsyncMock(return_value="D0OWNER")
    state.slack_client.post_message = AsyncMock(return_value="171.1")
    state.owner_id = "U0OWNER"
    state.sessions.set_slack_link = MagicMock(return_value=False)
    state.sessions.aflush = AsyncMock(name="aflush")
    state.push_slots_update = MagicMock()
    access = MagicMock(name="sel")

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-link", chat_slack.api_chat_slot_slack_link)
    with (
        patch.object(chat_slack, "sel", lambda: access),
        patch.object(chat_slack, "_spawn_slack_backfill") as backfill,
    ):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 503
            body = await resp.json()
    assert "ok" not in body and "switch" in body["error"]
    backfill.assert_not_called()
    state.sessions.aflush.assert_not_awaited()
    state.push_slots_update.assert_not_called()
    assert (slot._slack_channel, slot._slack_thread_ts) == ("", "")
    assert access.log_api_access.call_args.kwargs["outcome"] == "refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("switched", [False, True])
async def test_the_link_route_fences_its_write_with_the_generation_read_beside_the_client(
    tmp_path: Any, switched: bool
) -> None:
    """The route reads the Slack client and the link generation together, before
    its first await, posts through that client and writes the link with that
    generation: a Reconnect that switched workspace during the awaits bumped
    the generation, so the map refuses the stale write (the thread it posted
    lives in the former workspace) and the route answers 503, not ``{ok}``."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_slack

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hello")
    slot.drain()
    client_a = MagicMock(name="client-a")
    client_a.open_dm = AsyncMock(return_value="D0OWNER")

    async def _post(*a: Any, **k: Any) -> str:
        if switched:
            # The switch lands while the post is awaited: new client, bumped generation.
            state.slack_client = MagicMock(name="client-b")
            state.sessions.slack_links_generation.return_value = 8
        return "171.1"

    client_a.post_message = AsyncMock(side_effect=_post)
    state.slack_client = client_a
    state.owner_id = "U0OWNER"
    state.sessions.slack_links_generation = MagicMock(return_value=7)
    # The map's own fence: a write presenting a generation that is not current is refused.
    state.sessions.set_slack_link = MagicMock(
        side_effect=lambda k, t, c, generation=None: generation
        == state.sessions.slack_links_generation()
    )
    state.sessions.aflush = AsyncMock(name="aflush")
    state.push_slots_update = MagicMock()

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-link", chat_slack.api_chat_slot_slack_link)
    with (
        patch.object(chat_slack, "sel", lambda: MagicMock(name="sel")),
        patch.object(chat_slack, "_spawn_slack_backfill"),
    ):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            body = await resp.json()

    state.sessions.set_slack_link.assert_called_once()
    assert state.sessions.set_slack_link.call_args.kwargs["generation"] == 7
    if switched:
        assert resp.status == 503 and "ok" not in body
        assert (slot._slack_channel, slot._slack_thread_ts) == ("", "")
    else:
        assert resp.status == 200 and body["ok"] is True


def test_the_link_route_reads_client_and_generation_before_its_first_await() -> None:
    """Source pin: the client the route posts through and the generation it
    writes with are captured together at the top, and no later Slack call
    reaches for the live ``state.slack_client`` again."""
    import inspect

    from kiro_crew.dashboard import chat_slack

    src = inspect.getsource(chat_slack.api_chat_slot_slack_link)
    capture = src.index("slack = state.slack_client")
    generation = src.index("links_generation = state.sessions.slack_links_generation()")
    first_await = src.index("await ")
    assert capture < generation < first_await
    assert "await state.slack_client." not in src
    assert "generation=links_generation" in src


@pytest.mark.asyncio
async def test_a_thread_import_whose_link_is_refused_leaves_no_slot() -> None:
    """``_import_thread_to_slot`` mints the slot before linking it; a refused
    link drops that slot again and reports no import, so the dashboard never
    shows an unlinked copy of a thread of the workspace being swept."""
    from kiro_crew.slack.interactions import _import_thread_to_slot

    slack = MagicMock()
    slack.fetch_thread_replies = AsyncMock(return_value=[{"user": "U1", "text": "hello"}])
    slot = MagicMock(name="slot")
    slot.key = "slot_abc"
    ds = MagicMock(name="dashboard_state")
    ds.get_linked_slot.return_value = None
    ds.get_or_create_slot.return_value = slot
    ds._slots = {"slot_abc": slot}
    ds.link_slack.return_value = False

    with (
        patch("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", AsyncMock()) as save,
        patch("kiro_crew.dashboard.chat_utils._sync_dashboard_slots") as sync,
    ):
        assert await _import_thread_to_slot(slack, ds, "C0CHAN", "171.1") is None

    assert ds._slots == {}
    save.assert_not_awaited()
    # ``get_or_create_slot`` published the slot (active-slot set, slots push);
    # the retraction republishes both, so no tab keeps drawing it.
    sync.assert_called_once_with(ds)
    ds.push_slots_update.assert_called_once_with()


def test_session_manager_forwards_freeze_and_thaw() -> None:
    from kiro_crew.session import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._session_map = MagicMock(name="session_map")
    mgr._session_map.freeze_slack_links.return_value = [{"key": "k"}]
    assert mgr.freeze_slack_links() == [{"key": "k"}]
    mgr.thaw_slack_links()
    mgr._session_map.thaw_slack_links.assert_called_once_with()


@pytest.mark.asyncio
async def test_switch_freezes_before_the_marker_write_and_thaws_after_the_adopt() -> None:
    """Order on a switch: the copy is taken WITH the freeze, the marker is
    written from that copy, the sweep follows, the adopting record write lands
    -- and only THEN does the switch thaw, so no unfenced writer can land a
    former-workspace destination between the sweep and the record."""
    from kiro_crew.slack import gateway

    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    rows = [{"key": "slack:171.1", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    order: list[str] = []
    orch.sessions.freeze_slack_links.side_effect = lambda: order.append("freeze") or rows
    orch.sessions.clear_all_slack_links.side_effect = lambda: order.append("sweep") or [
        "slack:171.1"
    ]
    orch.sessions.thaw_slack_links.side_effect = lambda: order.append("thaw")
    real = gateway._store_slack_links_team_id

    def _store(path: Path, team_id: str, *, pending: Any = None) -> None:
        order.append(f"marker rows={len(pending[1])}" if pending else "adopt")
        real(path, team_id, pending=pending)

    with _team("T0NEW"), patch.object(gateway, "_store_slack_links_team_id", _store):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    assert order == ["freeze", "marker rows=1", "sweep", "adopt", "thaw"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["too_large", "marker_unwritable"])
async def test_a_switch_refused_before_sweeping_thaws_the_links(refusal: str) -> None:
    """Both refusals that happen after the copy and before the sweep end the
    freeze, so the writers a refused switch left intact are not refused for the
    rest of the process's life. (A refusal AFTER the sweep -- the adopting
    record write failing -- thaws too, after the undo; see the next test.)"""
    from contextlib import nullcontext

    from kiro_crew.slack.workspace_record import SLACK_SWITCH_MARKER_MAX_ROWS

    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    n = SLACK_SWITCH_MARKER_MAX_ROWS + 1 if refusal == "too_large" else 1
    orch.sessions.freeze_slack_links.return_value = [
        {"key": f"dashboard:{i}", "slack_thread_ts": f"{i}.1", "slack_channel_id": "C0OLD"}
        for i in range(n)
    ]
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"), _store_fails() if refusal == "marker_unwritable" else nullcontext():
        result = await _run(orch, recorder)

    assert result["connected"] is False
    orch.sessions.clear_all_slack_links.assert_not_called()
    orch.sessions.thaw_slack_links.assert_called_once_with()


# ── the record loader bounds every field it retains ──────────────────────────


def test_loader_refuses_an_oversized_team_id_without_a_marker(tmp_path: Path) -> None:
    """The shared shape check runs on EVERY record, not only one carrying a
    ``pending`` marker: an oversized ``team_id`` alone is damage too, since the
    loader retains it and the writer copies it into the next switch marker."""
    from kiro_crew.slack.gateway import _load_slack_workspace_record
    from kiro_crew.slack.workspace_record import SLACK_SWITCH_MARKER_MAX_FIELD_CHARS

    p = tmp_path / "slack_workspace.json"
    p.write_text(json.dumps({"team_id": "T" * (SLACK_SWITCH_MARKER_MAX_FIELD_CHARS + 1)}))
    assert _load_slack_workspace_record(p) is None
    p.write_text(json.dumps({"team_id": "T0A", "extra": 1}))
    assert _load_slack_workspace_record(p) is None
    p.write_text(json.dumps({"team_id": "T" * SLACK_SWITCH_MARKER_MAX_FIELD_CHARS}))
    record = _load_slack_workspace_record(p)
    assert record is not None and record.team_id == "T" * SLACK_SWITCH_MARKER_MAX_FIELD_CHARS


# ── the authorization subject is bound per envelope, like the client ─────────


def test_is_owner_resolves_the_bound_owner_over_the_module_global() -> None:
    """Inside an envelope's binding the owner is the one the RECEIVING socket
    was built with; a Reconnect that rebinds the module global to another
    workspace's owner does not reach a turn already in flight. Outside any
    binding the global applies, as for boot, cron and the HTTP routes."""
    from kiro_crew.slack import affinity, handler

    handler.set_owner_id("U0NEWOWNER")
    former = affinity.SocketAuthority("U0FORMER")
    try:
        assert handler.is_owner("U0NEWOWNER") is True
        with affinity.owner_scope(former):
            # Workspace A's sender whose id equals workspace B's new owner: NOT
            # the owner of the workspace this envelope came from.
            assert handler.is_owner("U0NEWOWNER") is False
            assert handler.is_allowed_user("U0NEWOWNER") is False
            assert handler.is_owner("U0FORMER") is True
            assert handler.is_owner("W0FORMER") is True  # the W/U cross-match still applies
            with affinity.owner_scope(affinity.SocketAuthority("")):
                assert handler.is_owner("U0FORMER") is False  # a socket built with no owner
            assert handler.is_owner("U0FORMER") is True
            # REVOKED (the gateway could not close this socket): nobody, not
            # the former owner and not the new one.
            former.revoke()
            assert former.owner_id == "" and former.revoked
            assert handler.is_owner("U0FORMER") is False
            assert handler.is_owner("U0NEWOWNER") is False
        assert handler.is_owner("U0NEWOWNER") is True
        assert affinity.bound_owner() is affinity.UNBOUND
    finally:
        handler.set_owner_id("")


@pytest.mark.asyncio
async def test_a_queued_turn_carries_and_rebinds_the_owner_it_was_received_under() -> None:
    """A queued message is dispatched from another task later (the previous
    turn's tail drains the queue), so the context binding does not reach it;
    the entry carries the owner (``owner_id``) and ``_dispatch_queued`` binds
    it again. An entry without the key authorizes against the live owner."""
    from kiro_crew.slack import affinity, events, handler

    seen: list[Any] = []

    async def _through(slack: Any, orch: Any, key: str, ts: str, text: str, kwargs: Any) -> None:
        seen.append(affinity.bound_owner())

    orch = MagicMock(name="orch")
    authority = affinity.SocketAuthority("U0FORMER")
    with patch.object(events, "_dispatch_queued_through", _through):
        await events._dispatch_queued(
            orch, "slack:1.1", "1.1", "hi", {"slack_client": MagicMock(), "owner_id": authority}
        )
        await events._dispatch_queued(orch, "slack:1.1", "1.1", "hi", {"slack_client": MagicMock()})
        # A legacy entry carrying a bare string (no such writer remains, but a
        # queue drained across an upgrade could hold one) is not a binding.
        await events._dispatch_queued(
            orch, "slack:1.1", "1.1", "hi", {"slack_client": MagicMock(), "owner_id": "U0X"}
        )
    assert seen == [authority, affinity.UNBOUND, affinity.UNBOUND]

    # The receipt helper the enqueue sites call: the bound AUTHORITY itself
    # (so a later revocation reaches the queued turn), or None outside any
    # binding.
    assert events._owner_at_receipt() is None
    with affinity.owner_scope(authority):
        assert events._owner_at_receipt() is authority
    assert handler.is_owner("U0FORMER") is False  # the global was never touched


def test_every_enqueue_site_carries_the_owner() -> None:
    """Source pin: the three places a message is queued for a busy session
    (the session queue, the pre-session stash, the force branch) all carry
    ``owner_id=_owner_at_receipt()`` beside the client and the generation."""
    import inspect

    from kiro_crew.slack import events

    src = inspect.getsource(events._route_message)
    assert src.count("slack_client=received_by,") == 3
    assert src.count("owner_id=_owner_at_receipt(),") == 3


# ── a flat DM's channel is a Slack destination too ───────────────────────────


def test_a_flat_dm_row_is_swept_copied_restored_and_frozen(tmp_path: Any) -> None:
    """``slack.dm_single_session`` keys a session by its DM channel with no
    thread, and cron / unattended deliveries read that channel back through
    ``get_channel``. Left behind by a workspace switch it would carry
    workspace A's DM id under workspace B's client, so the sweep, the marker
    copy, the undo and the freeze all treat it as a destination -- while the
    non-Slack bucket the dispatcher parks in the same legacy field is left
    alone."""
    from kiro_crew.session_map import SessionMap
    from kiro_crew.slack.workspace_record import (
        is_slack_destination_row,
        names_slack_conversation,
        slack_switch_row_defect,
    )

    assert names_slack_conversation("D0FLATDM") and names_slack_conversation("C0CHAN")
    assert not names_slack_conversation("discord:123") and not names_slack_conversation("")
    assert is_slack_destination_row({"slack_thread_ts": None, "slack_channel_id": "D0FLATDM"})
    assert not is_slack_destination_row({"slack_thread_ts": "", "slack_channel_id": "discord:1"})

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("slack:D0FLATDM", "sid-dm")
        smap.set_slack_link("slack:D0FLATDM", "", "D0FLATDM")  # the flat DM: channel, no thread
        smap.set("slack:171.1", "sid-thread")
        smap.set_slack_link("slack:171.1", "171.1", "C0CHAN")
        smap.set("discord:d1", "sid-discord")
        smap.set_slack_link("discord:d1", "", "discord:123")  # the non-Slack bucket

        rows = smap.freeze_slack_links()
        assert sorted(r["key"] for r in rows) == ["slack:171.1", "slack:D0FLATDM"]
        flat = next(r for r in rows if r["key"] == "slack:D0FLATDM")
        assert flat["slack_thread_ts"] == "" and flat["slack_channel_id"] == "D0FLATDM"
        assert slack_switch_row_defect(flat) is None  # the marker retains it

        # Frozen: a flat-DM channel write is refused like a thread write; the
        # non-Slack bucket and the clear sentinel still pass.
        smap.set("slack:D0LATE", "sid-late")
        smap.set_slack_link("slack:D0LATE", "", "D0LATE")
        assert smap.get_slack_link("slack:D0LATE") == (None, None)
        smap.set_slack_link("discord:d1", "", "discord:456")
        assert smap.get_slack_link("discord:d1") == ("", "discord:456")

        assert sorted(smap.clear_all_slack_links()) == ["slack:171.1", "slack:D0FLATDM"]
        assert smap.get_slack_link("slack:D0FLATDM") == (None, None)
        assert smap.get_slack_link("discord:d1") == ("", "discord:456")  # bucket untouched

        assert sorted(smap.restore_slack_links(rows)) == ["slack:171.1", "slack:D0FLATDM"]
        assert smap.get_slack_link("slack:D0FLATDM") == (None, "D0FLATDM")
        assert smap.get_slack_link("slack:171.1") == ("171.1", "C0CHAN")


def test_the_conversation_id_shape_matches_the_validators() -> None:
    """The leaf module spells the Slack conversation id shape itself (it
    imports nothing of the package); it must not drift from the one operator
    input is validated against."""
    from kiro_crew.slack.workspace_record import SLACK_CONVERSATION_ID_RE
    from kiro_crew.validation import CHANNEL_ID_RE, CHANNEL_MAX_LEN

    for value in ("C0A", "D" + "9" * 19, "G0PRIVATE", "W0CONNECT"):
        assert SLACK_CONVERSATION_ID_RE.match(value) and CHANNEL_ID_RE.match(value)
    for value in ("c0a", "discord:1", "U0USER", "", "C" + "A" * CHANNEL_MAX_LEN):
        leaf = SLACK_CONVERSATION_ID_RE.match(value) is not None
        core = CHANNEL_ID_RE.match(value) is not None and len(value) <= CHANNEL_MAX_LEN
        assert leaf == core, value


@pytest.mark.asyncio
async def test_the_admitted_connect_binds_before_it_connects() -> None:
    """The one sequence boot and Reconnect share: no socket -> nothing at all
    (``init_socket_mode`` declined -- owner missing, workspace refused by the
    enterprise gate -- and a bind run anyway would sweep the persisted
    destinations for a workspace nobody admitted); a socket -> bind FIRST, and
    connect only when the bind admitted the workspace."""
    orch = _orch()
    calls: list[str] = []
    bind_code = ""

    async def _bind(*, source: str) -> str:
        calls.append(f"bind {source}")
        return bind_code

    async def _connect() -> bool:
        calls.append("connect")
        return True

    orch._bind_slack_workspace = _bind
    orch._connect_slack = _connect

    orch._socket_client = None
    assert await orch._connect_admitted_slack_socket(source="boot") is False
    assert calls == []

    orch._socket_client = MagicMock(name="socket")
    assert await orch._connect_admitted_slack_socket(source="boot") is True
    assert calls == ["bind boot", "connect"]

    calls.clear()
    bind_code = "workspace_identity_unverified"
    assert await orch._connect_admitted_slack_socket(source="reconnect") is False
    assert calls == ["bind reconnect"]


@pytest.mark.asyncio
async def test_a_close_that_fails_revokes_the_surviving_sockets_authority() -> None:
    """The abort branch de-authorizes through the module globals, but every
    envelope the surviving listener still delivers is authorized against the
    authority bound to it, which outranks those globals. So the abort revokes
    that authority: the former owner passes nowhere afterwards, in flight or
    queued."""
    from kiro_crew.slack import affinity, handler

    old = MagicMock(name="old-socket")
    old.close = AsyncMock(side_effect=RuntimeError("websocket still live"))
    orch = _orch(old_client=old)
    authority = affinity.SocketAuthority("U0FORMEROWNER")
    orch._slack_socket_authority = authority
    handler.set_owner_id("U0FORMEROWNER")
    try:
        with affinity.owner_scope(authority):
            assert handler.is_owner("U0FORMEROWNER") is True
            result = await orch._reconnect_slack_once()
            assert result == {"connected": False, "connect_error": "previous_client_close_failed"}
            assert authority.revoked
            # An envelope the retained socket delivers now: nobody is the owner.
            assert handler.is_owner("U0FORMEROWNER") is False
            assert handler.is_allowed_user("U0FORMEROWNER") is False
        assert orch._socket_client is old  # retained for the next attempt or shutdown
    finally:
        handler.set_owner_id("")
        handler.set_allowed_users(set())


@pytest.mark.asyncio
async def test_a_refused_bind_revokes_the_unadmitted_sockets_authority() -> None:
    """The socket torn down for a workspace nobody admitted takes its
    authority with it, so an envelope it handed the listener before the
    teardown authorizes nobody."""
    orch = _orch()
    orch._slack_workspace_record_loaded = False
    orch._slack_workspace_state_path.write_text("{not json")
    rec = _Recorder()
    rec.client.close = AsyncMock(name="close")

    with _team("T0NEW"):
        result = await _run(orch, rec)

    assert result["connect_error"] == "workspace_record_unreadable"
    assert orch._slack_socket_authority is not None
    assert orch._slack_socket_authority.revoked
    assert orch._slack_socket_authority.owner_id == ""


@pytest.mark.asyncio
async def test_a_reconnect_that_changes_the_owner_revokes_the_former_sockets_authority() -> None:
    """The former socket's bound authority outranks the module globals the
    Reconnect rebinds, so when the OWNER changes it goes with the former owner
    at the hoist: an operator removing a departed owner and clicking Reconnect
    must not leave that owner's queued turn (its entry carries the authority)
    authorized as the owner. The socket closed cleanly, so nothing new arrives
    under it; what is in flight or queued is authorized against nobody."""
    from kiro_crew.slack import affinity, handler

    old = MagicMock(name="old-socket")
    old.close = AsyncMock()
    orch = _orch(old_client=old)
    orch._owner_id = FORMER_OWNER
    former = affinity.SocketAuthority(FORMER_OWNER)
    orch._slack_socket_authority = former

    result = await _run(orch, _Recorder())

    assert result["connected"] is True
    assert former.revoked and former.owner_id == ""
    with affinity.owner_scope(former):
        assert handler.is_owner(FORMER_OWNER) is False  # the queued turn, drained now
    assert orch._slack_socket_authority is not former  # the new socket's own
    assert orch._slack_socket_authority.owner_id == NEW_CREDS[CRED_OWNER_ID]
    assert not orch._slack_socket_authority.revoked


@pytest.mark.asyncio
async def test_a_same_owner_same_workspace_reconnect_keeps_the_former_sockets_authority() -> None:
    """A routine Reconnect -- same owner, same workspace, credentials admitted
    -- leaves the former socket's authority standing: the socket closed
    cleanly and its turns finish under the owner they were received from, so
    an owner command queued across the Reconnect is still the owner's, not
    "Not authorized"."""
    from kiro_crew.slack import affinity, handler

    old = MagicMock(name="old-socket")
    old.close = AsyncMock()
    orch = _orch(old_client=old)
    orch._owner_id = NEW_CREDS[CRED_OWNER_ID]
    orch._slack_links_team_id = "T0SAME"
    former = affinity.SocketAuthority(NEW_CREDS[CRED_OWNER_ID])
    orch._slack_socket_authority = former

    with _team("T0SAME"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    assert not former.revoked and former.owner_id == NEW_CREDS[CRED_OWNER_ID]
    with affinity.owner_scope(former):
        assert handler.is_owner(NEW_CREDS[CRED_OWNER_ID]) is True
    assert orch._slack_socket_authority is not former


@pytest.mark.asyncio
async def test_an_owner_change_revokes_every_authority_a_former_socket_still_holds() -> None:
    """Two Reconnects with one turn queued across both: the first (same owner,
    same workspace) keeps socket A's authority standing beside socket B's; the
    second changes the owner and must reach A's authority too -- the queued
    turn's entry carries A's, not B's -- so every outstanding authority is
    retained (``_slack_socket_authorities``) and revoked together."""
    from kiro_crew.slack import affinity

    orch = _orch(old_client=MagicMock(name="socket-a", close=AsyncMock()))
    orch._owner_id = NEW_CREDS[CRED_OWNER_ID]
    orch._slack_links_team_id = "T0SAME"
    authority_a = affinity.SocketAuthority(NEW_CREDS[CRED_OWNER_ID])
    orch._slack_socket_authority = authority_a

    with _team("T0SAME"):
        assert (await _run(orch, _Recorder()))["connected"] is True
    authority_b = orch._slack_socket_authority
    assert authority_b is not authority_a and not authority_a.revoked
    assert orch._live_slack_socket_authorities() == [authority_a, authority_b]

    # The operator removes the owner and clicks Reconnect again.
    orch._cfg.load_credentials.return_value = {**NEW_CREDS, CRED_OWNER_ID: "U0REPLACEMENT"}
    orch._socket_client = MagicMock(name="socket-b", close=AsyncMock())
    with _team("T0SAME"):
        assert (await _run(orch, _Recorder()))["connected"] is True
    assert authority_a.revoked and authority_b.revoked
    authority_c = orch._slack_socket_authority
    assert authority_c.owner_id == "U0REPLACEMENT" and not authority_c.revoked
    assert orch._live_slack_socket_authorities() == [authority_c]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["workspace_switch", "bind_refused", "tokens_missing"],
)
async def test_a_reconnect_that_changes_or_refuses_the_identity_revokes_the_authority(
    case: str,
) -> None:
    """Same owner, but the identity the former socket's turns were received
    under is not the one this process serves afterwards -- the credentials
    name another workspace (a switch), the bind refuses them, or they are gone
    -- so the former authority is revoked once the bind has said so."""
    from kiro_crew.slack import affinity

    old = MagicMock(name="old-socket")
    old.close = AsyncMock()
    creds: Any = (
        NEW_CREDS if case != "tokens_missing" else {CRED_OWNER_ID: NEW_CREDS[CRED_OWNER_ID]}
    )
    orch = _orch(creds, old_client=old)
    orch._owner_id = NEW_CREDS[CRED_OWNER_ID]
    orch._slack_links_team_id = "T0FORMER"
    former = affinity.SocketAuthority(NEW_CREDS[CRED_OWNER_ID])
    orch._slack_socket_authority = former

    if case == "workspace_switch":
        with _team("T0NEW"):
            result = await _run(orch, _Recorder())
        assert result["connected"] is True and orch._slack_links_team_id == "T0NEW"
    elif case == "bind_refused":
        orch._slack_workspace_record_loaded = False
        orch._slack_workspace_state_path.write_text("{not json")
        recorder = _Recorder()
        recorder.client.close = AsyncMock(name="close")
        with _team("T0FORMER"):
            result = await _run(orch, recorder)
        assert result["connect_error"] == "workspace_record_unreadable"
    else:
        result = await _run(orch, _Recorder())
        assert result["connect_error"] == "tokens_missing"
    assert former.revoked and former.owner_id == ""


def test_init_socket_mode_hands_the_orchestrator_the_sockets_authority() -> None:
    """Source pin: the authority bound per envelope is the one stored on the
    orchestrator, so the gateway's revoke reaches the listener's binding."""
    import inspect

    from kiro_crew.slack import events

    src = inspect.getsource(events.init_socket_mode)
    assert "received_owner = slack_affinity.SocketAuthority(orch._owner_id)" in src
    assert "orch._slack_socket_authority = received_owner" in src
    assert "slack_affinity.owner_scope(received_owner)," in src
    # The self id the trusted-bot gate excludes is bound to the SAME authority
    # once auth.test validated this socket's workspace, and the gate reads it
    # from the socket -- not from the module-level copy a later Reconnect to
    # another workspace replaces.
    assert "received_owner.bind_self_bot_id(validated_self_bot_id())" in src
    assert "self_bot_id=received_owner.self_bot_id," in src
    assert src.index("received_owner.bind_self_bot_id(") < src.index(
        "self_bot_id=received_owner.self_bot_id,"
    )


def test_socket_authority_binds_the_self_bot_id_and_blanks_it_on_revoke() -> None:
    """The gateway's own bot id is per workspace, so it lives on the socket's
    authority beside the owner: empty until the workspace is validated (which
    fails admission closed), the validated id afterwards, "" once revoked."""
    from kiro_crew.slack.affinity import SocketAuthority

    authority = SocketAuthority("U_OWNER")
    assert authority.self_bot_id == ""
    authority.bind_self_bot_id("B_SELF")
    assert authority.self_bot_id == "B_SELF"
    assert SocketAuthority("U_OWNER", "B_PRESET").self_bot_id == "B_PRESET"
    authority.revoke()
    assert authority.self_bot_id == ""
    assert authority.owner_id == ""


def test_trusted_bot_gate_judges_a_former_sockets_event_by_its_own_self_id() -> None:
    """A Reconnect to another workspace replaces the module-level self id; an
    event of the former workspace still in its ack suspension carries the former
    socket's authority, whose bound self id keeps the former workspace's own bot
    excluded -- the reply loop the exclusion exists to prevent."""
    from kiro_crew.slack import enterprise
    from kiro_crew.slack.affinity import SocketAuthority

    former = SocketAuthority("U_OWNER", "B_OLD_SELF")
    current = SocketAuthority("U_OWNER", "B_NEW_SELF")
    saved = enterprise._validated_self_bot_id
    enterprise._validated_self_bot_id = "B_NEW_SELF"
    try:
        listed = {"B_OLD_SELF", "B_NEW_SELF", "B_PEER"}
        assert enterprise.trusted_bot_admission(
            "B_OLD_SELF", listed, self_bot_id=former.self_bot_id
        ) == (False, "own_bot_id_never_trusted")
        assert enterprise.trusted_bot_admission(
            "B_NEW_SELF", listed, self_bot_id=current.self_bot_id
        ) == (False, "own_bot_id_never_trusted")
        assert enterprise.trusted_bot_admission(
            "B_PEER", listed, self_bot_id=former.self_bot_id
        ) == (True, "")
        # Revoked: the self id blanks, and the gate fails closed for that socket.
        former.revoke()
        assert enterprise.trusted_bot_admission(
            "B_PEER", listed, self_bot_id=former.self_bot_id
        ) == (False, "trusted_bot_requires_verified_self_id")
    finally:
        enterprise._validated_self_bot_id = saved


# ── a cron delivery waits for the client's publication to settle ─────────────


@pytest.mark.asyncio
async def test_settled_slack_client_waits_for_the_attempt_then_reads_the_real_state() -> None:
    """``self.slack`` reads None while boot withholds the client and while a
    Reconnect has withdrawn it -- windows in which Slack is NOT down. A cron
    delivery that took that None at face value would skip its Slack leg and, for
    a one-shot job, be consumed with the post never made; so it waits for the
    attempt to settle and reads the state then, whichever way it went."""
    orch = _orch()
    published = MagicMock(name="published-client")

    # Settled and published: immediate.
    orch.slack = published
    assert await orch._settled_slack_client() is published
    # Settled and down: immediate None.
    orch.slack = None
    assert await orch._settled_slack_client() is None

    # Withdrawn: waits, then sees the publication.
    orch._slack_client_settled.clear()
    reader = asyncio.ensure_future(orch._settled_slack_client())
    await asyncio.sleep(0.05)
    assert not reader.done()
    orch.slack = published
    orch._slack_client_settled.set()
    assert await reader is published

    # Withdrawn, then the attempt fails: waits, then sees it is down.
    orch.slack = None
    orch._slack_client_settled.clear()
    reader = asyncio.ensure_future(orch._settled_slack_client())
    await asyncio.sleep(0.05)
    orch._slack_client_settled.set()
    assert await reader is None

    # The LIVE slot, never a bound client: a cron timer task can inherit an
    # envelope's binding (an interaction acking a job re-arms the timer), and
    # a cron delivery must still see the client as it stands after a Reconnect.
    from kiro_crew.slack import affinity

    orch.slack = published
    with affinity.client_scope(MagicMock(name="withdrawn-former-client")):
        assert await orch._settled_slack_client() is published
    orch.slack = None
    with affinity.client_scope(MagicMock(name="withdrawn-former-client")):
        assert await orch._settled_slack_client() is None


@pytest.mark.asyncio
async def test_settled_slack_client_is_bounded(caplog: Any) -> None:
    """An attempt that never settles cannot hold a cron delivery forever: past
    the bound the client is taken as it stands, and the wait is logged."""
    from kiro_crew.slack import gateway

    orch = _orch()
    orch.slack = None
    orch._slack_client_settled.clear()
    with (
        patch.object(
            gateway.GatewayOrchestrator, "_slack_publication_wait_secs", lambda self: 0.05
        ),
        caplog.at_level("WARNING"),
    ):
        assert await orch._settled_slack_client() is None
    assert any("did not settle" in rec.getMessage() for rec in caplog.records)


def test_the_settle_bound_covers_the_boot_window_it_waits_through() -> None:
    """Boot clears the settle event before cron starts and sets it after the
    admitted connect, so a job due in between waits through the MCP probe
    (``mcp_probe_timeout_secs`` + 15, an operator setting up to 120 s), the
    identity re-validation ladder and the handshake itself. The bound is that
    sum, never a fixed figure a long-but-configured boot can outlast."""
    from kiro_crew.slack import gateway

    orch = _orch()
    for probe in (5, 46, 120):
        orch._cfg.dashboard.mcp_probe_timeout_secs = probe
        expected = (
            probe
            + 15
            + sum(gateway._SLACK_IDENTITY_RETRY_DELAYS)
            + gateway._SLACK_PUBLICATION_WAIT_SECS
        )
        assert orch._slack_publication_wait_secs() == expected
        assert orch._slack_publication_wait_secs() > probe + 15
    # The doubles built with ``__new__`` and no config still get a bound.
    bare = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
    assert bare._slack_publication_wait_secs() >= gateway._SLACK_PUBLICATION_WAIT_SECS


@pytest.mark.asyncio
async def test_reconnect_withdraws_and_settles_the_client_around_the_attempt() -> None:
    """Clear from the teardown of the former client to the publication of the
    new one; set again on every exit -- a success, a refusal, an abort."""
    orch = _orch()
    hold = asyncio.Event()
    rec = _Recorder(hold=hold)
    seen: list[bool] = []

    async def _observe() -> None:
        # Runs while the recorder holds the attempt mid-handshake.
        await asyncio.sleep(0.02)
        seen.append(orch._slack_client_settled.is_set())
        hold.set()

    observer = asyncio.ensure_future(_observe())
    result = await _run(orch, rec)
    await observer
    assert result["connected"] is True
    assert seen == [False]  # withdrawn while the attempt ran
    assert orch._slack_client_settled.is_set()

    # The close-failed abort settles too.
    old = MagicMock(name="old-socket")
    old.close = AsyncMock(side_effect=RuntimeError("websocket still live"))
    orch = _orch(old_client=old)
    result = await orch._reconnect_slack_once()
    assert result["connect_error"] == "previous_client_close_failed"
    assert orch._slack_client_settled.is_set()

    # A raise out of the withdrawn body settles too.
    orch = _orch()
    with patch.object(
        type(orch), "_reconnect_slack_withdrawn", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        with pytest.raises(RuntimeError):
            await orch._reconnect_slack_once()
    assert orch._slack_client_settled.is_set()


def test_boot_withholds_and_settles_the_client_around_its_bind() -> None:
    """Source pin: ``_init_services`` clears the settle when it withholds the
    boot client (Slack enabled), and ``run`` sets it right after the publish
    -- connected or not -- so a cron due in between waits rather than skips."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    init = inspect.getsource(GatewayOrchestrator._init_services)
    withheld = init.index("self.slack = None")
    cleared = init.index("self._slack_client_settled.clear()")
    assert withheld < cleared
    run = inspect.getsource(GatewayOrchestrator.run)
    publish = run.index("self.slack = self._slack_boot_client")
    mirror = run.index("self.dashboard_state.slack_client = self.slack if connected else None")
    settled = run.index("self._slack_client_settled.set()")
    assert publish < mirror < settled
    assert run.count("self._slack_client_settled.set()") == 1


def test_every_cron_slack_leg_reads_the_settled_client() -> None:
    """Source pin: the three cron deliveries that can post to Slack -- the
    result, the failure alert, the post-subagent response -- read the client
    through ``_settled_slack_client`` and never take ``self.slack`` at face
    value for the decision to skip the leg."""
    import inspect
    import re

    from kiro_crew.slack.gateway import GatewayOrchestrator

    for fn in (
        GatewayOrchestrator._deliver_cron_response,
        GatewayOrchestrator._deliver_failure_alert,
        GatewayOrchestrator._init_cron,
    ):
        src = inspect.getsource(fn)
        assert "await self._settled_slack_client()" in src, fn.__name__
        code = "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))
        assert not re.search(r"if self\.slack\b", code), fn.__name__
        assert "self.slack.post_" not in code, fn.__name__


@pytest.mark.asyncio
async def test_a_switch_whose_record_write_fails_thaws_after_the_undo() -> None:
    """The freeze outlives the sweep: it ends only once the record is written
    or -- here -- the sweep is undone from the copy. Order: freeze, marker,
    sweep, the failing adopt, the restore, then the thaw; a writer refused in
    between wrote nothing the undo could not account for."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    rows = [{"key": "slack:171.1", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    order: list[str] = []
    orch.sessions.freeze_slack_links.side_effect = lambda: order.append("freeze") or rows
    orch.sessions.clear_all_slack_links.side_effect = lambda: order.append("sweep") or [
        "slack:171.1"
    ]
    orch.sessions.restore_slack_links.side_effect = lambda r: order.append("restore") or [
        "slack:171.1"
    ]
    orch.sessions.thaw_slack_links.side_effect = lambda: order.append("thaw")
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"), _final_store_fails():
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unrecorded"}
    assert order == ["freeze", "sweep", "restore", "thaw"]
    assert orch._slack_links_team_id == "T0FORMER"


# ── a switch sweeps the cron store's Slack destinations with the links ───────


def _cron_svc(*jobs: Any) -> Any:
    from types import SimpleNamespace

    svc = MagicMock(name="cron_svc")
    listed = [
        SimpleNamespace(
            id=j[0], channel=j[1], thread_ts=j[2], script=j[3] if len(j) > 3 else "", command=""
        )
        for j in jobs
    ]
    # The switch reads the LOCKED, cross-process-fresh snapshot, never the
    # cache-only ``list_jobs`` (a job written by another process inside the
    # cache window would be missed and survive the sweep).
    svc.list_jobs_async = AsyncMock(return_value=listed)
    svc.list_jobs.side_effect = AssertionError("the switch must read list_jobs_async")
    svc.update_job_async = AsyncMock(return_value=MagicMock(name="job"))
    return svc


def _clear(channel: str, thread_ts: str | None) -> dict[str, Any]:
    """The kwargs of the sweep's clear of a job that held *channel*/*thread_ts*:
    a compare-and-swap on that very destination. A channel-only row (no
    thread) names, compares and blanks the channel alone."""
    from kiro_crew.cron import DESTINATION_ANY

    if thread_ts is None:
        return {"channel": None, "expect_destination": (channel, DESTINATION_ANY)}
    return {"channel": None, "thread_ts": None, "expect_destination": (channel, thread_ts)}


def _put_back(channel: str, thread_ts: str | None) -> dict[str, Any]:
    """The kwargs of the undo's restore: the copy goes back only over a job the
    sweep left cleared -- the channel alone for a channel-only row."""
    from kiro_crew.cron import DESTINATION_ANY

    if thread_ts is None:
        return {"channel": channel, "expect_destination": (None, DESTINATION_ANY)}
    return {"channel": channel, "thread_ts": thread_ts, "expect_destination": (None, None)}


@pytest.mark.asyncio
async def test_cron_destination_rows_copy_what_each_cron_kind_posts_to_slack() -> None:
    """One marker row per cron with something that posts to Slack, keyed
    ``cronjob:<id>``. A message cron contributes channel and thread. A script
    or command cron contributes its Slack channel ALONE -- its failure alert
    posts there whatever the kind -- and never its thread, which is bound into
    a grant's fingerprint; one with no Slack channel is left alone, as is any
    job with no Slack destination."""
    from kiro_crew.slack.workspace_record import slack_switch_row_defect

    orch = _orch()
    orch.cron_svc = _cron_svc(
        ("j1", "C0CHAN", "171.1"),
        ("j2", "D0FLAT", None),
        ("j3", None, None),
        ("j4", "C0SCRIPT", "172.1", "crons/x.py:run"),
        ("j5", "discord:123", None),
        ("j6", "discord:123", "173.1", "crons/y.py:run"),
    )
    rows = await orch._snapshot_cron_slack_destinations()
    orch.cron_svc.list_jobs_async.assert_awaited_once_with(include_disabled=True)
    assert rows == [
        {"key": "cronjob:j1", "slack_thread_ts": "171.1", "slack_channel_id": "C0CHAN"},
        {"key": "cronjob:j2", "slack_thread_ts": "", "slack_channel_id": "D0FLAT"},
        {"key": "cronjob:j4", "slack_thread_ts": "", "slack_channel_id": "C0SCRIPT"},
    ]
    assert all(slack_switch_row_defect(r) is None for r in rows)  # the marker retains them
    orch.cron_svc = None
    assert await orch._snapshot_cron_slack_destinations() == []


@pytest.mark.asyncio
async def test_a_switch_clears_cron_slack_destinations_under_the_marker() -> None:
    """The cron store's channel / thread were minted by the former workspace
    too: they ride in the marker, are cleared after the map sweep flushed, and
    the identity is adopted only afterwards."""
    from kiro_crew.slack import gateway

    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.cron_svc = _cron_svc(("j1", "C0CHAN", "171.1"))
    order: list[str] = []
    orch.sessions.clear_all_slack_links.side_effect = lambda: order.append("sweep") or []
    orch.cron_svc.update_job_async.side_effect = (
        lambda *a, **k: order.append(f"cron {a[0]} {sorted(k.items())}") or MagicMock()
    )
    real = gateway._store_slack_links_team_id

    def _store(path: Path, team_id: str, *, pending: Any = None) -> None:
        order.append(f"marker rows={[r['key'] for r in pending[1]]}" if pending else "adopt")
        real(path, team_id, pending=pending)

    with _team("T0NEW"), patch.object(gateway, "_store_slack_links_team_id", _store):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    assert order == [
        "marker rows=['cronjob:j1']",
        "sweep",
        f"cron j1 {sorted(_clear('C0CHAN', '171.1').items())}",
        "adopt",
    ]


@pytest.mark.asyncio
async def test_a_failed_record_write_puts_the_cron_destinations_back() -> None:
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.cron_svc = _cron_svc(("j1", "C0CHAN", "171.1"), ("j2", "D0FLAT", None))
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"), _final_store_fails():
        result = await _run(orch, recorder)

    assert result["connect_error"] == "workspace_identity_unrecorded"
    calls = [c.args + (c.kwargs,) for c in orch.cron_svc.update_job_async.await_args_list]
    assert calls == [
        ("j1", _clear("C0CHAN", "171.1")),
        ("j2", _clear("D0FLAT", None)),
        ("j1", _put_back("C0CHAN", "171.1")),
        ("j2", _put_back("D0FLAT", None)),
    ]


@pytest.mark.asyncio
async def test_an_interrupted_switch_undone_at_connect_restores_cron_destinations() -> None:
    """The marker's ``cronjob:`` rows are put back beside the map's rows when
    the next connect finds the credentials naming the recorded workspace again."""
    orch = _orch()
    orch._slack_workspace_record_loaded = False
    orch.cron_svc = _cron_svc()
    rows = [
        {"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"},
        {"key": "cronjob:j1", "slack_thread_ts": "", "slack_channel_id": "D0FLAT"},
    ]
    orch._slack_workspace_state_path.write_text(
        json.dumps({"team_id": "T0FORMER", "pending": {"team_id": "T0NEW", "swept": rows}})
    )
    orch.sessions.restore_slack_links.return_value = ["dashboard:one"]

    with _team("T0FORMER"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    # The map gets only ITS rows; the cron store gets the cronjob row.
    orch.sessions.restore_slack_links.assert_called_once_with([rows[0]])
    orch.cron_svc.update_job_async.assert_awaited_once_with("j1", **_put_back("D0FLAT", None))
    assert _pending_team(orch) is None


def test_the_cron_post_fences_its_link_write_with_the_delivery_generation() -> None:
    """Source pin: the cron result leg writes the thread it posted in through
    ``set_slack_link(..., generation=links_generation)`` -- the generation read
    with the client, before the awaited post -- never through the unfenced
    ``set_thread`` / ``set_channel`` pair: an A -> B switch landing during the
    post bumps the generation and the write is refused instead of putting A's
    destination back under B."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator._init_cron)
    assert "generation=links_generation" in src
    assert "self.sessions.slack_links_generation()" in src
    code = "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))
    assert "set_thread(" not in code and "set_channel(" not in code


# ── a transient auth.test failure at the bind is retried, bounded ────────────


@pytest.mark.asyncio
async def test_an_unverified_identity_is_revalidated_before_the_socket_is_retired() -> None:
    """A recorded workspace plus a handshake that named none is refused
    (fail-closed) -- but only after the identity was asked for again, a bounded
    number of times: a transient ``auth.test`` failure at boot must not leave
    Slack down for the whole run. Here the second ask succeeds."""
    from kiro_crew.slack import gateway

    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    asks: list[str] = []
    validated = [""]  # what auth.test named: nothing at the handshake

    def _revalidate() -> bool:
        asks.append("auth.test")
        validated[0] = "T0FORMER"  # the retry names the recorded workspace
        return True

    orch._revalidate_slack_identity = _revalidate
    with (
        patch.object(
            gateway.GatewayOrchestrator,
            "_slack_validated_team_id",
            staticmethod(lambda: validated[0]),
        ),
        patch.object(gateway, "_SLACK_IDENTITY_RETRY_DELAYS", (0.0, 0.0)),
    ):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    assert asks == ["auth.test"]
    assert orch._slack_links_team_id == "T0FORMER"


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_verdict", [True, False])
async def test_a_persistently_unverified_identity_is_still_refused(gate_verdict: bool) -> None:
    """The retries are bounded; and a gate that REFUSES the workspace on a retry
    is not a transient failure and ends them at once."""
    from kiro_crew.slack import gateway

    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    asks: list[str] = []
    orch._revalidate_slack_identity = lambda: asks.append("auth.test") or gate_verdict
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")
    with (
        _team(""),
        patch.object(gateway, "_SLACK_IDENTITY_RETRY_DELAYS", (0.0, 0.0)),
    ):
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unverified"}
    assert asks == ["auth.test", "auth.test"] if gate_verdict else ["auth.test"]
    recorder.client.close.assert_awaited_once()
    assert orch._slack_links_team_id == "T0FORMER"


@pytest.mark.asyncio
async def test_a_cron_clear_that_raises_midway_puts_the_cleared_rows_back() -> None:
    """The cron half of the sweep clears row by row, so a store refusal on a
    later row (ordinary lock contention) leaves the earlier rows cleared on
    disk. The sweep's rollback restores them beside the map's rows before the
    raise leaves -- best-effort, so a second refusal cannot cost the map its
    restore -- and the attempt is refused with the former identity intact."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.cron_svc = _cron_svc(("j1", "C0ONE", "171.1"), ("j2", "C0TWO", None))
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _update(job_id: str, **kwargs: Any) -> Any:
        calls.append((job_id, kwargs))
        # The CLEAR of the second row raises; every restore succeeds.
        if job_id == "j2" and kwargs["channel"] is None:
            raise RuntimeError("CronStoreBusy")
        return MagicMock(name="job")

    orch.cron_svc.update_job_async = AsyncMock(side_effect=_update)
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"):
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unrecorded"}
    assert calls == [
        ("j1", _clear("C0ONE", "171.1")),
        ("j2", _clear("C0TWO", None)),  # raised
        ("j1", _put_back("C0ONE", "171.1")),
        ("j2", _put_back("C0TWO", None)),
    ]
    orch.sessions.restore_slack_links.assert_called_once()
    assert orch._slack_links_team_id == "T0FORMER"
    assert _pending_team(orch) == "T0NEW"  # the marker stays for the next connect


@pytest.mark.asyncio
async def test_the_rollbacks_cron_restore_is_best_effort() -> None:
    """A row the store refuses AGAIN during the rollback is logged and skipped;
    the other rows and the map's restore still land, and the raise still leaves."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.cron_svc = _cron_svc(("j1", "C0ONE", None), ("j2", "C0TWO", None))
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _update(job_id: str, **kwargs: Any) -> Any:
        calls.append((job_id, kwargs))
        if job_id == "j2" and kwargs["channel"] is None:
            raise RuntimeError("CronStoreBusy")
        if job_id == "j1" and kwargs["channel"] == "C0ONE":
            raise RuntimeError("CronStoreBusy again")
        return MagicMock(name="job")

    orch.cron_svc.update_job_async = AsyncMock(side_effect=_update)
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"):
        result = await _run(orch, recorder)

    assert result["connect_error"] == "workspace_identity_unrecorded"
    assert calls[-2:] == [
        ("j1", _put_back("C0ONE", None)),  # refused, logged
        ("j2", _put_back("C0TWO", None)),  # still put back
    ]
    orch.sessions.restore_slack_links.assert_called_once()


# ── an operator's concurrent edit of a cron destination wins over the sweep ──


@pytest.mark.asyncio
async def test_a_cron_destination_edited_during_the_switch_is_neither_cleared_nor_overwritten(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sweep's map half is awaited disk work and a PATCH of a job's channel
    or thread can land inside it. The clear is a compare-and-swap on the copied
    destination, so the store's refusal (``CronDestinationMismatch``) leaves the
    edit standing; the switch itself still completes."""
    from kiro_crew.cron import CronDestinationMismatch

    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.cron_svc = _cron_svc(("j1", "C0CHAN", "171.1"), ("j2", "C0KEEP", None))

    async def _update(job_id: str, **kwargs: Any) -> Any:
        if job_id == "j2":
            raise CronDestinationMismatch("delivery destination changed concurrently")
        return MagicMock(name="job")

    orch.cron_svc.update_job_async = AsyncMock(side_effect=_update)

    with _team("T0NEW"), caplog.at_level(logging.INFO, logger="kiro_crew.slack.gateway"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    assert orch._slack_links_team_id == "T0NEW"
    assert "j2" in caplog.text and "edit stands" in caplog.text


@pytest.mark.asyncio
async def test_the_undo_does_not_put_a_copy_back_over_a_job_that_carries_a_destination() -> None:
    """A restore is a compare-and-swap on the CLEARED state: a job the sweep
    never reached still holds its own destination, and a job an operator
    re-pointed since holds theirs. The store refuses the copy for both, the
    row is not reported as restored, and the other rows still go back."""
    from kiro_crew.cron import CronDestinationMismatch

    orch = _orch()
    orch.cron_svc = _cron_svc()

    async def _update(job_id: str, **kwargs: Any) -> Any:
        assert kwargs["expect_destination"][0] is None  # only over a cleared channel
        if job_id == "j1":
            raise CronDestinationMismatch("delivery destination changed concurrently")
        return MagicMock(name="job")

    orch.cron_svc.update_job_async = AsyncMock(side_effect=_update)
    rows = [
        {"key": "cronjob:j1", "slack_thread_ts": "171.1", "slack_channel_id": "C0ONE"},
        {"key": "cronjob:j2", "slack_thread_ts": "", "slack_channel_id": "C0TWO"},
    ]
    assert await orch._restore_cron_slack_destinations(rows) == ["cronjob:j2"]
    # The same refusal is not a store error: best-effort mode does not log it as one.
    assert await orch._restore_cron_slack_destinations(rows, best_effort=True) == ["cronjob:j2"]


def test_a_channel_only_row_touches_the_channel_alone() -> None:
    """The update a marker row stands for: a row with a thread blanks and puts
    back both fields, comparing both; a channel-only row (a flat DM, or a
    script cron's alert channel) names the channel alone and leaves the thread
    uncompared and unwritten."""
    from kiro_crew.cron import DESTINATION_ANY
    from kiro_crew.slack.gateway import GatewayOrchestrator

    fields = GatewayOrchestrator._cron_destination_fields
    threaded = {"key": "cronjob:j1", "slack_thread_ts": "171.1", "slack_channel_id": "C0ONE"}
    flat = {"key": "cronjob:j2", "slack_thread_ts": "", "slack_channel_id": "C0TWO"}
    assert fields(threaded, cleared=False) == (
        {"channel": None, "thread_ts": None},
        ("C0ONE", "171.1"),
    )
    assert fields(threaded, cleared=True) == (
        {"channel": "C0ONE", "thread_ts": "171.1"},
        (None, None),
    )
    assert fields(flat, cleared=False) == ({"channel": None}, ("C0TWO", DESTINATION_ANY))
    assert fields(flat, cleared=True) == ({"channel": "C0TWO"}, (None, DESTINATION_ANY))


def test_a_script_crons_thread_survives_the_sweep_and_the_undo(tmp_path: Path) -> None:
    """End to end through the real store: a script cron's Slack channel is
    cleared and put back, its thread -- part of a grant's fingerprint -- never
    compared and never rewritten."""
    from kiro_crew.cron import CronService
    from kiro_crew.slack.gateway import GatewayOrchestrator

    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(
        "j", "", every_secs=3600, channel="C0CHAN", thread_ts="171.1", script="crons/x.py:run"
    )
    row = {"key": f"cronjob:{job.id}", "slack_thread_ts": "", "slack_channel_id": "C0CHAN"}
    fields, expect = GatewayOrchestrator._cron_destination_fields(row, cleared=False)
    assert svc.update_job(job.id, expect_destination=expect, **fields)
    assert (svc.get_job(job.id).channel, svc.get_job(job.id).thread_ts) == (None, "171.1")
    fields, expect = GatewayOrchestrator._cron_destination_fields(row, cleared=True)
    assert svc.update_job(job.id, expect_destination=expect, **fields)
    assert (svc.get_job(job.id).channel, svc.get_job(job.id).thread_ts) == ("C0CHAN", "171.1")


def test_the_cron_store_checks_the_destination_under_its_lock(tmp_path: Path) -> None:
    """``expect_destination`` is compared against the freshly reloaded record,
    inside the locked update, as stored (falsy is None): the match writes, the
    mismatch raises ``CronDestinationMismatch`` with the job untouched."""
    from kiro_crew.cron import CronDestinationMismatch, CronService

    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("j", "hello", every_secs=3600, channel="C0CHAN", thread_ts="171.1")
    with pytest.raises(CronDestinationMismatch):
        svc.update_job(job.id, channel=None, thread_ts=None, expect_destination=("C0OTHER", None))
    assert (svc.get_job(job.id).channel, svc.get_job(job.id).thread_ts) == ("C0CHAN", "171.1")
    assert svc.update_job(
        job.id, channel=None, thread_ts=None, expect_destination=("C0CHAN", "171.1")
    )
    assert (svc.get_job(job.id).channel, svc.get_job(job.id).thread_ts) == (None, None)
    # A cleared job matches the cleared expectation whether it is spelled None or "".
    assert svc.update_job(job.id, channel="C0CHAN", expect_destination=("", ""))
    with pytest.raises(CronDestinationMismatch):
        svc.update_job(job.id, channel="C0X", expect_destination=(None, None))
    assert svc.get_job(job.id).channel == "C0CHAN"


# ── a cron destination row is bounded by the cron store's own caps ───────────


def test_cron_destination_rows_are_bounded_by_the_cron_stores_caps() -> None:
    """A ``cronjob:`` row goes back through the cron store, whose caps are
    tighter than the marker's field cap. Bounded by the marker's cap alone, a
    row the record validator admits could be one the store refuses -- and the
    undo at the next connect would raise on it every time, the marker never
    cleared and Slack never connected. The leaf module spells the caps itself
    (it imports nothing of the package); they must not drift from the store's."""
    from kiro_crew.cron_service.fields import _CRON_STRING_FIELD_CAPS
    from kiro_crew.slack.gateway import _CRON_DESTINATION_KEY_PREFIX
    from kiro_crew.slack.workspace_record import (
        SLACK_CONVERSATION_ID_MAX_CHARS,
        SLACK_CRON_DESTINATION_KEY_PREFIX,
        SLACK_THREAD_TS_MAX_CHARS,
        slack_switch_row_defect,
    )
    from kiro_crew.validation import CHANNEL_MAX_LEN

    caps = dict(_CRON_STRING_FIELD_CAPS)
    assert SLACK_THREAD_TS_MAX_CHARS == caps["thread_ts"]
    assert SLACK_CONVERSATION_ID_MAX_CHARS == caps["channel"] == CHANNEL_MAX_LEN
    assert _CRON_DESTINATION_KEY_PREFIX == SLACK_CRON_DESTINATION_KEY_PREFIX == "cronjob:"

    ok = {"key": "cronjob:j1", "slack_thread_ts": "1" * 30, "slack_channel_id": "C" + "A" * 19}
    assert slack_switch_row_defect(ok) is None
    assert slack_switch_row_defect({"key": "cronjob:j1", "slack_thread_ts": "1" * 31}) == (
        "cron 'slack_thread_ts' exceeds 30 characters"
    )
    assert (
        slack_switch_row_defect(
            {"key": "cronjob:j1", "slack_thread_ts": "1.1", "slack_channel_id": "C" + "A" * 20}
        )
        == "cron 'slack_channel_id' is not a Slack conversation id"
    )
    # A session-map row keeps the marker's own bound: its channel field also
    # parks non-Slack bookkeeping the cron store never sees.
    assert (
        slack_switch_row_defect(
            {"key": "dashboard:d", "slack_thread_ts": "1" * 31, "slack_channel_id": "x" * 40}
        )
        is None
    )


# ── a dashboard turn reads the client through the settle-wait ────────────────


@pytest.mark.asyncio
async def test_the_dashboard_state_reads_the_client_through_the_settle_wait() -> None:
    """``DashboardState.settled_slack_client`` answers the orchestrator's
    settled read when wired (the client once boot's bind-and-connect or a
    Reconnect has settled), and the plain mirror when not (API-only server,
    tests)."""
    from kiro_crew.dashboard.state import DashboardState

    state = DashboardState.__new__(DashboardState)
    state.slack_client = MagicMock(name="mirror")
    state._slack_client_settle = None
    assert await state.settled_slack_client() is state.slack_client

    orch = _orch()
    published = MagicMock(name="published")
    orch.slack = None
    orch._slack_client_settled.clear()
    state._slack_client_settle = orch._settled_slack_client
    reader = asyncio.ensure_future(state.settled_slack_client())
    await asyncio.sleep(0.05)
    assert not reader.done()  # withheld: the turn waits rather than mirrors nothing
    orch.slack = published
    orch._slack_client_settled.set()
    assert await reader is published


def test_the_dashboard_state_can_tell_a_withheld_client_without_waiting() -> None:
    """``slack_client_withheld`` is the settle read's synchronous half: True
    exactly while ``settled_slack_client`` would suspend (boot or a Reconnect
    has the event clear), False once it is set, and never withheld without a
    wired orchestrator. A turn reads it to skip the wait it has no use for."""
    from kiro_crew.dashboard.state import DashboardState

    state = DashboardState.__new__(DashboardState)
    state._slack_client_withheld = None
    assert state.slack_client_withheld() is False

    orch = _orch()
    state._slack_client_withheld = orch._slack_client_withheld
    assert state.slack_client_withheld() is False
    orch._slack_client_settled.clear()
    assert state.slack_client_withheld() is True
    orch._slack_client_settled.set()
    assert state.slack_client_withheld() is False


@pytest.mark.asyncio
async def test_a_linked_turn_reads_its_destination_after_the_wait_not_before() -> None:
    """The mirror gate of a dashboard turn against a Reconnect that switches
    workspace: the link read BEFORE the wait only decides whether to wait; the
    destination the turn posts to is read again AFTER the client settles,
    when the switch's sweep has already run. A link swept meanwhile is gone,
    so the turn mirrors nothing -- never a former-workspace thread through the
    new workspace's client. An unlinked turn takes no wait at all."""
    import inspect

    from kiro_crew.dashboard import chat_runner

    runner = inspect.getsource(chat_runner)
    withheld = runner.index('getattr(state, "slack_client_withheld", None)')
    pre_read = runner.index("_pre_thread, _pre_chan = state.sessions.get_slack_link(", withheld)
    settle = runner.index("_mirror_client = await _settled_slack_client_for_turn(state)", pre_read)
    post_read = runner.index(
        "_mirror_thread, _mirror_chan = state.sessions.get_slack_link(", settle
    )
    assert withheld < pre_read < settle < post_read
    # The pre-read decides the wait and nothing else: the names it binds are
    # never what the turn posts to.
    assert "_pre_chan," not in runner[post_read:]
    assert "_pre_thread," not in runner[post_read:]

    # Behaviour, on a state whose client is withheld for a switch that sweeps
    # the link: the turn waits, then finds no destination.
    state = MagicMock(name="state")
    state.slack_client = None
    settled = asyncio.Event()
    published = MagicMock(name="published")
    link = {"row": ("171.1", "C0FORMER")}
    state.sessions.get_slack_link = MagicMock(side_effect=lambda key: link["row"])
    state.slack_client_withheld = MagicMock(side_effect=lambda: not settled.is_set())

    async def _settle() -> Any:
        await settled.wait()
        return published

    state.settled_slack_client = _settle

    async def _switch() -> None:
        await asyncio.sleep(0.02)
        link["row"] = (None, None)  # the sweep
        settled.set()

    async def _gate() -> tuple[Any, Any, Any]:
        # The gate's own logic, lifted verbatim in shape from the runner.
        _mirror_client = None
        _withheld = getattr(state, "slack_client_withheld", None)
        if callable(_withheld) and _withheld() is True:
            _pre_thread, _pre_chan = state.sessions.get_slack_link("k")
            if _pre_thread and _pre_chan:
                _mirror_client = await chat_runner._settled_slack_client_for_turn(state)
        else:
            _mirror_client = state.slack_client
        thread, chan = ("", "")
        if _mirror_client:
            thread, chan = state.sessions.get_slack_link("k")
        return _mirror_client, thread, chan

    asyncio.ensure_future(_switch())
    client, thread, chan = await _gate()
    assert client is published and (thread, chan) == (None, None)


@pytest.mark.asyncio
async def test_a_dashboard_turn_waits_a_short_bound_for_the_settle_then_runs_unmirrored(
    caplog: Any,
) -> None:
    """A person is waiting on a dashboard turn, so its settle wait is the length
    of a handshake (``_SLACK_SETTLE_WAIT_SECS``), not the cron leg's boot-sized
    bound: past it the turn runs with no mirror client, logged. A settle that
    lands inside the bound hands the client over; a state without the settled
    read answers its plain mirror."""
    from kiro_crew.dashboard import chat_runner

    published = MagicMock(name="published")
    settled = asyncio.Event()

    async def _settle() -> Any:
        await settled.wait()
        return published

    state = MagicMock(name="state")
    state.settled_slack_client = _settle
    with (
        patch.object(chat_runner, "_SLACK_SETTLE_WAIT_SECS", 0.05),
        caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"),
    ):
        assert await chat_runner._settled_slack_client_for_turn(state) is None
    assert "runs without a Slack mirror" in caplog.text
    settled.set()
    assert await chat_runner._settled_slack_client_for_turn(state) is published
    assert chat_runner._SLACK_SETTLE_WAIT_SECS < 60

    plain = MagicMock(name="plain-state", spec=["slack_client"])
    plain.slack_client = published
    assert await chat_runner._settled_slack_client_for_turn(plain) is published


def test_the_settle_read_is_wired_at_dashboard_init_and_read_by_the_turn() -> None:
    """Source pins: both dashboard starts (full and API-only) wire the settled
    read before any turn can start, and the turn's mirror gate reads through it
    -- never the ``slack_client`` mirror snapshot alone."""
    import inspect

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.slack.gateway import GatewayOrchestrator

    wired = "self.dashboard_state._slack_client_settle = self._settled_slack_client"
    withheld = "self.dashboard_state._slack_client_withheld = self._slack_client_withheld"
    for init in (GatewayOrchestrator._init_dashboard, GatewayOrchestrator._init_api_server):
        src = inspect.getsource(init)
        assert wired in src and withheld in src
    runner = inspect.getsource(chat_runner)
    assert 'getattr(state, "settled_slack_client", None)' in inspect.getsource(
        chat_runner._settled_slack_client_for_turn
    )
    assert "if state.slack_client and not is_slash" not in runner
    # Only a turn that WILL mirror takes the wait: the withheld check and the
    # deciding link read sit in front of the settle read, so a slash turn, a
    # paused mirror and an unlinked session never suspend on a Slack boot or
    # Reconnect.
    gate_start = runner.index("if not is_slash and not slack_mirror_is_paused(state, session_key):")
    settle_read = runner.index("_mirror_client = await _settled_slack_client_for_turn(state)")
    assert gate_start < settle_read
    gate = runner[gate_start:settle_read]
    assert 'getattr(state, "slack_client_withheld", None)' in gate
    assert "if _pre_thread and _pre_chan:" in gate
