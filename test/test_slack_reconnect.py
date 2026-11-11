"""Slack reconnect: saved credentials take effect without a gateway restart.

Covers ``kiro_crew.slack.reconnect`` (the in-place handshake) and the
``POST /api/slack/reconnect`` route that calls it. The orchestrator is a small
stand-in carrying only the attributes the reconnect reads and writes; the
handshake seams (``init_socket_mode``, ``_connect_slack``, the workspace probe)
are fakes, so nothing here talks to Slack.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.messaging as msg
from kiro_crew.slack import client as slack_client_mod
from kiro_crew.slack import events as slack_events
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack import reconnect as rc
from kiro_crew.testing.wait import async_wait_until, until_parked

#: Lost-run ceiling for an await on a task this file releases itself (an event
#: it sets, a gate it opens). A regression that leaves the task parked fails at
#: that line instead of hanging the xdist worker.
_RELEASED_TASK_TIMEOUT_SECS = 30.0

GOOD = {
    "SLACK_APP_TOKEN": "xapp-placeholder",
    "SLACK_BOT_TOKEN": "xoxb-placeholder",
    "KIROCREW_OWNER_ID": "U0NEWOWNER",
}


def _rejected(code: str) -> Exception:
    exc = RuntimeError("rejected")
    exc.response = {"error": code}  # type: ignore[attr-defined]
    return exc


class _State:
    def __init__(self) -> None:
        self.slack_client: Any = "old-client"
        self.slack_socket_connected = True
        self.slack_connect_error = ""
        self.owner_id = "U0OLDOWNER"
        self.notify = MagicMock()


class _Orch:
    """The attributes ``reconnect_slack`` touches, nothing else."""

    def __init__(self, *, connect_ok: bool = True) -> None:
        self.old_socket = MagicMock()
        self.old_socket.close = AsyncMock()
        self._socket_client: Any = self.old_socket
        self.slack: Any = "old-client"
        self.dashboard_state: Any = _State()
        self._app_token = "xapp-old"
        self._bot_token = "xoxb-old"
        # The saved owner: Reconnect never changes it in place.
        self._owner_id = "U0NEWOWNER"
        self._allowed_users = {"U0NEWOWNER"}
        # A previous failure left the flag off: the reconnect must recompute it.
        self._slack_enabled = False
        self._slack_connect_error = "invalid_auth"
        self._slack_seen: Any = None
        self._slack_team_id = "T0SAME"
        self._slack_workspace_pinned = True
        self._slack_reconnect_task = None
        self._slack_reconnecting = False
        self._tracking_channels: set[str] = set()
        self._background_tasks: set[asyncio.Task] = set()
        self._handler_tasks: set[asyncio.Task] = set()
        self.connect_ok = connect_ok
        self.connect_calls = 0
        self.connect_saw_slack: list[Any] = []
        self.connect_saw_window: list[bool] = []

    async def _connect_slack(self) -> bool:
        self.connect_calls += 1
        self.connect_saw_slack.append(self.slack)
        self.connect_saw_window.append(rc.reconnect_in_progress(self))
        if not self.connect_ok:
            self._slack_connect_error = "ConnectionError"
        return self.connect_ok


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Fake every seam the reconnect crosses and record what each saw."""
    seen = SimpleNamespace(
        creds=dict(GOOD),
        creds_error=None,
        unresolved=False,
        unresolved_asked=[],
        team="T0SAME",
        team_error=None,
        teams_by_token={},
        token_errors={},
        init_calls=[],
        init_admits=True,
        owner_ids=[],
        allowed=[],
        init_gate=None,
        propagate_calls=[],
    )

    class _Cfg:
        def load_credentials(self, *, propagate: bool = True) -> dict[str, str]:
            seen.propagate_calls.append(propagate)
            if seen.creds_error is not None:
                raise seen.creds_error
            return dict(seen.creds)

    monkeypatch.setattr(rc, "live", SimpleNamespace(snapshot=lambda: _Cfg()))

    def _unresolved(missing: list[str]) -> bool:
        seen.unresolved_asked.append(list(missing))
        return seen.unresolved

    monkeypatch.setattr(rc, "_unresolved_reference", _unresolved)

    async def _probe(token: str) -> str:
        if token in seen.token_errors:
            raise seen.token_errors[token]
        if seen.team_error is not None:
            raise seen.team_error
        return str(seen.teams_by_token.get(token, seen.team))

    monkeypatch.setattr(rc, "probe_workspace", _probe)

    async def _init(orch: Any, cache: Any, *, declared_yolo: bool = True) -> None:
        seen.init_calls.append(
            {
                "declared_yolo": declared_yolo,
                "loop": asyncio.get_running_loop(),
                "enabled": orch._slack_enabled,
                "app": orch._app_token,
                "bot": orch._bot_token,
                "owner": orch._owner_id,
                "published": orch.dashboard_state.slack_client,
                "orch_slack": orch.slack,
                "cache": cache,
            }
        )
        if seen.init_gate is not None:
            await seen.init_gate.wait()
        if seen.init_admits:
            orch._socket_client = MagicMock(close=AsyncMock())
        else:
            orch._slack_enabled = False
            orch.slack = None

    monkeypatch.setattr(slack_events, "init_socket_mode", _init)
    monkeypatch.setattr(slack_client_mod, "RealSlackClient", lambda token: f"client:{token}")
    monkeypatch.setattr(slack_handler, "set_owner_id", seen.owner_ids.append)
    monkeypatch.setattr(slack_handler, "set_allowed_users", seen.allowed.append)
    return seen


def _run(orch: _Orch) -> dict[str, Any]:
    return asyncio.run(rc.reconnect_slack(orch))  # type: ignore[arg-type]


# ── the handshake ────────────────────────────────────────────────────────────


def test_reconnect_recomputes_enabled_and_hoists_new_credentials(seams) -> None:
    orch = _Orch()
    result = _run(orch)

    assert result == {"connected": True, "connect_error": ""}
    (call,) = seams.init_calls
    assert call["enabled"] is True
    assert (call["app"], call["bot"], call["owner"]) == (
        "xapp-placeholder",
        "xoxb-placeholder",
        "U0NEWOWNER",
    )
    assert orch._allowed_users == {"U0NEWOWNER"}
    assert orch.dashboard_state.owner_id == "U0NEWOWNER"
    assert seams.owner_ids == ["U0NEWOWNER"]
    # A post-boot read: it never writes a credential back into os.environ.
    assert seams.propagate_calls == [False]


def test_reconnect_closes_the_old_socket_before_the_handshake(seams) -> None:
    orch = _Orch()
    _run(orch)

    orch.old_socket.close.assert_awaited_once()
    # Nothing is published while the new socket is still being built: not to
    # the dashboard, and not to the gateway's own senders either.
    assert seams.init_calls[0]["published"] is None
    assert seams.init_calls[0]["orch_slack"] is None
    assert orch.connect_saw_slack == [None]
    assert orch.slack == "client:xoxb-placeholder"


def test_reconnect_publishes_the_client_only_behind_a_connected_socket(seams) -> None:
    orch = _Orch()
    _run(orch)

    state = orch.dashboard_state
    assert state.slack_client == "client:xoxb-placeholder"
    assert state.slack_socket_connected is True
    assert state.slack_connect_error == ""
    assert orch._slack_team_id == "T0SAME"


def test_reconnect_awaits_init_on_the_running_loop_with_one_seen_cache(seams) -> None:
    orch = _Orch()
    cache = object()
    orch._slack_seen = cache

    async def _go() -> asyncio.AbstractEventLoop:
        await rc.reconnect_slack(orch)  # type: ignore[arg-type]
        return asyncio.get_running_loop()

    loop = asyncio.run(_go())
    assert seams.init_calls[0]["loop"] is loop
    assert seams.init_calls[0]["cache"] is cache


def test_unreadable_store_raises_and_leaves_the_connection_alone(seams) -> None:
    seams.creds_error = OSError("vault locked")
    orch = _Orch()

    with pytest.raises(rc.CredentialStoreUnreadable):
        _run(orch)
    assert orch._socket_client is orch.old_socket
    orch.old_socket.close.assert_not_awaited()
    assert orch.dashboard_state.slack_client == "old-client"
    assert seams.init_calls == []


def test_an_unresolved_vault_reference_leaves_the_connection_alone(seams) -> None:
    # load_credentials drops a secret:// reference the vault could not read,
    # so the token reads as absent; the reconnect must not close the live
    # socket and report tokens_missing for it.
    seams.creds = {"KIROCREW_OWNER_ID": "U0NEWOWNER"}
    seams.unresolved = True
    orch = _Orch()

    with pytest.raises(rc.CredentialStoreUnreadable):
        _run(orch)
    assert seams.unresolved_asked == [["SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"]]
    assert orch._socket_client is orch.old_socket
    orch.old_socket.close.assert_not_awaited()
    assert seams.init_calls == []


def test_complete_credentials_skip_the_reference_check(seams) -> None:
    _run(_Orch())
    assert seams.unresolved_asked == []


def test_reconnect_skips_the_start_up_yolo_grant(seams) -> None:
    _run(_Orch())
    assert [c["declared_yolo"] for c in seams.init_calls] == [False]


def test_unresolved_reference_reads_the_raw_env_line(tmp_path, monkeypatch) -> None:
    from kiro_crew.config import loader

    env = tmp_path / ".env"
    env.write_text("SLACK_BOT_TOKEN=secret://slack_bot\nSLACK_APP_TOKEN=\n", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("KIROCREW_OWNER_ID", raising=False)

    assert rc._unresolved_reference(["SLACK_BOT_TOKEN"]) is True
    assert rc._unresolved_reference(["SLACK_APP_TOKEN", "KIROCREW_OWNER_ID"]) is False


def test_unresolved_reference_reads_the_environment_first(tmp_path, monkeypatch) -> None:
    # The loader lets a non-empty environment value win over .env, so a
    # reference supplied only through the environment counts too.
    from kiro_crew.config import loader

    env = tmp_path / ".env"
    env.write_text("SLACK_APP_TOKEN=xapp-placeholder\n", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setenv("SLACK_APP_TOKEN", "secret://slack_app")

    assert rc._unresolved_reference(["SLACK_APP_TOKEN"]) is True


def test_missing_tokens_disable_slack_without_a_handshake(seams) -> None:
    seams.creds = {"KIROCREW_OWNER_ID": "U0NEWOWNER"}
    orch = _Orch()
    result = _run(orch)

    assert result == {"connected": False, "connect_error": rc.TOKENS_MISSING}
    assert seams.init_calls == []
    orch.old_socket.close.assert_awaited_once()
    assert orch.dashboard_state.slack_client is None


def test_missing_owner_is_named(seams) -> None:
    # A gateway that has no owner yet, with none saved either.
    seams.creds = {k: v for k, v in GOOD.items() if k != "KIROCREW_OWNER_ID"}
    orch = _Orch()
    orch._owner_id = ""
    orch._allowed_users = set()
    result = _run(orch)

    assert result == {"connected": False, "connect_error": rc.OWNER_ID_MISSING}
    assert seams.init_calls == []
    assert seams.owner_ids == [""]
    assert seams.allowed == [set()]
    assert orch.dashboard_state.owner_id == ""


def test_a_token_slack_rejects_passes_its_code_through(seams) -> None:
    exc = Exception("rejected")
    exc.response = {"error": "invalid_auth"}  # type: ignore[attr-defined]
    seams.team_error = exc
    orch = _Orch()
    orch._bot_token = GOOD["SLACK_BOT_TOKEN"]
    result = _run(orch)

    assert result == {"connected": False, "connect_error": "invalid_auth"}
    assert seams.init_calls == []
    assert orch.dashboard_state.slack_client is None


@pytest.mark.parametrize("error", [OSError("offline"), _rejected("invalid_auth")])
def test_another_token_whose_workspace_cannot_be_named_leaves_the_connection_up(
    seams, error: Exception
) -> None:
    # The pinned workspace is known, but the saved token's probe fails: unnamed,
    # it may belong to another workspace, so nothing is torn down.
    seams.token_errors = {GOOD["SLACK_BOT_TOKEN"]: error}
    orch = _Orch()
    result = _run(orch)

    assert result == {"connected": True, "connect_error": rc._slack_error_code(error)}
    assert seams.init_calls == []
    orch.old_socket.close.assert_not_awaited()
    assert orch.slack == "old-client"
    assert orch.dashboard_state.slack_client == "old-client"
    assert orch._bot_token == "xoxb-old"


def test_a_different_workspace_is_refused_and_the_connection_stays_up(seams) -> None:
    # The workspace gate runs before the teardown: a refused attempt leaves the
    # working socket and its published client as they were.
    seams.team = "T0OTHER"
    orch = _Orch()
    result = _run(orch)

    assert result == {"connected": True, "connect_error": rc.WORKSPACE_SWITCH_NEEDS_RESTART}
    assert seams.init_calls == []
    assert orch._slack_team_id == "T0SAME"
    orch.old_socket.close.assert_not_awaited()
    assert orch._socket_client is orch.old_socket
    assert orch.slack == "old-client"
    assert orch.dashboard_state.slack_client == "old-client"
    assert orch.dashboard_state.slack_socket_connected is True


def test_an_unconfirmed_pin_is_confirmed_by_the_same_token_and_reconnects(seams) -> None:
    # The boot probe failed for a transient reason, so the pinned workspace was
    # never named. Reconnecting with the same saved token names it and connects
    # instead of refusing every attempt as a workspace switch.
    orch = _Orch()
    orch._bot_token = GOOD["SLACK_BOT_TOKEN"]
    orch._slack_team_id = ""
    seams.team = "T0BOOT"
    result = _run(orch)

    assert result == {"connected": True, "connect_error": ""}
    assert orch._slack_team_id == "T0BOOT"
    assert orch._slack_workspace_pinned is True


def test_an_unconfirmed_pin_asks_the_boot_token_before_comparing(seams) -> None:
    # Another saved token: the boot token is asked again, and a workspace that
    # differs from the one it names is still refused, with the connection up.
    orch = _Orch()
    orch._slack_team_id = ""
    seams.teams_by_token = {"xoxb-old": "T0BOOT"}
    seams.team = "T0OTHER"
    result = _run(orch)

    assert result["connect_error"] == rc.WORKSPACE_SWITCH_NEEDS_RESTART
    assert orch._slack_team_id == "T0BOOT"
    assert seams.init_calls == []
    orch.old_socket.close.assert_not_awaited()


def test_an_unconfirmed_pin_that_still_cannot_be_named_refuses_another_token(seams) -> None:
    # Work admitted through the pinned token names its workspace's channels.
    # With that workspace unnamed, another token could belong to a different
    # one, so it is refused before the teardown and the connection stays up.
    orch = _Orch()
    orch._slack_team_id = ""
    seams.token_errors = {"xoxb-old": OSError("offline")}
    seams.team = "T0NEW"
    result = _run(orch)

    assert result == {"connected": True, "connect_error": rc.WORKSPACE_SWITCH_NEEDS_RESTART}
    assert orch._slack_team_id == ""
    assert seams.init_calls == []
    orch.old_socket.close.assert_not_awaited()
    assert orch.slack == "old-client"
    assert orch._bot_token == "xoxb-old"


def test_a_cleared_token_does_not_lose_the_unnamed_pins_token(seams) -> None:
    # The boot probe never named the pinned workspace. A save that clears the
    # bot token reconnects to nothing, and must not erase the token the pin was
    # taken for: restoring it reconnects instead of being refused until restart.
    orch = _Orch()
    orch._slack_team_id = ""
    orch._slack_pin_token = "xoxb-old"
    seams.creds = dict(GOOD, SLACK_BOT_TOKEN="")
    assert _run(orch)["connect_error"] == rc.TOKENS_MISSING
    assert orch._bot_token == ""

    seams.creds = dict(GOOD, SLACK_BOT_TOKEN="xoxb-old")
    seams.team = "T0BOOT"
    result = _run(orch)

    assert result == {"connected": True, "connect_error": ""}
    assert orch._slack_team_id == "T0BOOT"


def test_a_rotated_token_becomes_the_pins_own_token(seams) -> None:
    # Boot pinned token A; a reconnect with token B in the same workspace
    # connects. B is now the pin's token: a later attempt with B whose probe
    # fails once goes on, instead of being refused as "another token".
    orch = _Orch()
    orch._slack_pin_token = "xoxb-old"
    assert _run(orch) == {"connected": True, "connect_error": ""}
    assert orch._slack_pin_token == GOOD["SLACK_BOT_TOKEN"]

    seams.token_errors = {GOOD["SLACK_BOT_TOKEN"]: OSError("timeout")}
    team_id, refused = asyncio.run(
        rc._check_workspace(orch, GOOD["SLACK_BOT_TOKEN"])  # type: ignore[arg-type]
    )
    assert (team_id, refused) == (None, None)


def test_a_gateway_that_never_connected_adopts_the_saved_workspace(seams) -> None:
    seams.team = "T0FIRST"
    orch = _Orch()
    orch._slack_workspace_pinned = False
    orch._slack_team_id = ""
    result = _run(orch)

    assert result == {"connected": True, "connect_error": ""}
    assert orch._slack_team_id == "T0FIRST"
    assert orch._slack_workspace_pinned is True


def test_a_reconnect_attaches_the_agent_session_driver_to_its_client(seams, monkeypatch) -> None:
    # Boot attaches the native agent-session driver to the client it builds;
    # the client a reconnect publishes gets the same, or session status and
    # Stop buttons go quiet until a restart.
    from kiro_crew.slack.agent_sessions import status as agent_status

    attached: list[Any] = []
    monkeypatch.setattr(agent_status, "configure", lambda o: attached.append(o.slack))
    orch = _Orch()
    assert _run(orch) == {"connected": True, "connect_error": ""}
    assert attached == ["client:xoxb-placeholder"]


class _StatusClient:
    """The two attributes the code-channel status driver uses on a Slack client."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._web = SimpleNamespace(api_call=self._api_call)

    async def _api_call(self, method: str, *, params: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(params["status"])
        return {"ok": True}

    def _inject_team(self, channel: str, params: dict[str, Any]) -> None:
        return None


def _code_channel_turn_across_a_reconnect(
    orch: _Orch, monkeypatch: pytest.MonkeyPatch
) -> tuple[_StatusClient, _StatusClient]:
    """Start a code-channel turn on *orch*'s client, reconnect, end the turn."""
    from kiro_crew.slack.code_channels import status as cc_status

    old, new = _StatusClient(), _StatusClient()
    orch.slack = old
    monkeypatch.setattr(slack_client_mod, "RealSlackClient", lambda token: new)
    monkeypatch.setattr(cc_status, "_gate", lambda: True)
    monkeypatch.setattr(cc_status, "_slack_provider", lambda: orch.slack)
    binding = SimpleNamespace(channel_id="C0CODE")

    async def _turn() -> None:
        await cc_status._on_start(binding, None)  # type: ignore[arg-type]
        await rc.reconnect_slack(orch)  # type: ignore[arg-type]
        await cc_status._on_end(binding, None)  # type: ignore[arg-type]

    asyncio.run(_turn())
    return old, new


def test_a_code_channel_turn_across_a_reconnect_clears_through_the_new_client(
    seams, monkeypatch
) -> None:
    orch = _Orch()
    old, new = _code_channel_turn_across_a_reconnect(orch, monkeypatch)
    assert orch.slack is new
    assert old.sent == ["processing"]
    assert new.sent == ["active"]


@pytest.mark.parametrize(
    "attempt, old_sent",
    [
        ("handshake-fails", ["processing"]),
        # Refused before the teardown: the old client stays published and ends the turn.
        ("workspace-switch-refused", ["processing", "active"]),
    ],
)
def test_a_reconnect_that_publishes_no_client_carries_no_status_records(
    seams, monkeypatch, attempt: str, old_sent: list[str]
) -> None:
    orch = _Orch(connect_ok=attempt != "handshake-fails")
    if attempt == "workspace-switch-refused":
        seams.team = "T0OTHER"
    old, new = _code_channel_turn_across_a_reconnect(orch, monkeypatch)
    assert new.sent == []
    assert not hasattr(new, "code_channel_status")
    assert old.sent == old_sent


def test_a_workspace_the_gate_rejects_publishes_no_client(seams) -> None:
    seams.init_admits = False
    orch = _Orch()
    result = _run(orch)

    assert result == {"connected": False, "connect_error": rc.ENTERPRISE_VALIDATION_FAILED}
    assert orch.connect_calls == 0
    assert orch.dashboard_state.slack_client is None
    assert orch.slack is None


def test_a_failed_handshake_names_the_reason_and_retires_the_socket(seams) -> None:
    orch = _Orch(connect_ok=False)
    result = _run(orch)

    assert result == {"connected": False, "connect_error": "ConnectionError"}
    assert orch._socket_client is None
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_connect_error == "ConnectionError"


def test_a_policy_deny_is_named(seams) -> None:
    orch = _Orch()

    built: list[Any] = []

    async def _deny() -> bool:
        built.append(orch._socket_client)
        orch._socket_client = None
        return False

    orch._connect_slack = _deny  # type: ignore[method-assign]
    result = _run(orch)
    assert result == {"connected": False, "connect_error": rc.DENIED_BY_POLICY}
    # The deny drops the new client without closing it; the reconnect closes it.
    built[0].close.assert_awaited_once()


def test_a_close_that_raises_does_not_block_the_new_handshake(seams) -> None:
    orch = _Orch()
    orch.old_socket.close = AsyncMock(side_effect=RuntimeError("socket gone"))
    result = _run(orch)

    assert result["connected"] is True
    assert len(seams.init_calls) == 1


def test_an_unexpected_failure_leaves_slack_down_and_says_why(seams, monkeypatch) -> None:
    async def _boom(orch: Any, cache: Any, *, declared_yolo: bool = True) -> None:
        orch._socket_client = MagicMock(close=AsyncMock())
        raise RuntimeError("socket build failed")

    monkeypatch.setattr(slack_events, "init_socket_mode", _boom)
    orch = _Orch()
    result = _run(orch)

    assert result == {"connected": False, "connect_error": "RuntimeError"}
    assert orch._socket_client is None
    assert orch.dashboard_state.slack_client is None


def test_a_connected_reconnect_runs_the_tracked_channel_probe(seams, monkeypatch) -> None:
    from kiro_crew.slack import scope_probe

    probe = AsyncMock(return_value={})
    monkeypatch.setattr(scope_probe, "warn_unreadable_tracked_channels", probe)
    orch = _Orch()
    orch._tracking_channels = {"C0TRACKED"}

    async def _go() -> None:
        await rc.reconnect_slack(orch)  # type: ignore[arg-type]
        await asyncio.wait_for(
            asyncio.gather(*orch._background_tasks), timeout=_RELEASED_TASK_TIMEOUT_SECS
        )

    asyncio.run(_go())
    probe.assert_awaited_once()
    assert probe.await_args is not None
    assert probe.await_args.args == ("client:xoxb-placeholder", {"C0TRACKED"})


def test_concurrent_callers_share_one_handshake(seams) -> None:
    orch = _Orch()

    async def _go() -> list[dict[str, Any]]:
        seams.init_gate = asyncio.Event()
        calls = [asyncio.create_task(rc.reconnect_slack(orch)) for _ in range(3)]  # type: ignore[arg-type]
        await until_parked(seams.init_gate)
        seams.init_gate.set()
        return await asyncio.wait_for(asyncio.gather(*calls), timeout=_RELEASED_TASK_TIMEOUT_SECS)

    results = asyncio.run(_go())
    assert len(seams.init_calls) == 1
    assert results == [{"connected": True, "connect_error": ""}] * 3


def test_a_cancelled_caller_does_not_cancel_the_attempt(seams) -> None:
    orch = _Orch()

    async def _go() -> dict[str, Any]:
        seams.init_gate = asyncio.Event()
        caller = asyncio.create_task(rc.reconnect_slack(orch))  # type: ignore[arg-type]
        await until_parked(seams.init_gate)
        caller.cancel()
        seams.init_gate.set()
        assert orch._slack_reconnect_task is not None
        return await asyncio.wait_for(
            orch._slack_reconnect_task, timeout=_RELEASED_TASK_TIMEOUT_SECS
        )

    assert asyncio.run(_go()) == {"connected": True, "connect_error": ""}


def test_record_connected_workspace_names_the_boot_bot_token_workspace(seams) -> None:
    seams.team = "T0BOOT"
    orch = _Orch()
    orch._slack_team_id = ""

    asyncio.run(rc.record_connected_workspace(orch))  # type: ignore[arg-type]
    assert orch._slack_team_id == "T0BOOT"
    assert orch._slack_workspace_pinned is True


def test_record_connected_workspace_leaves_it_unknown_on_failure(seams) -> None:
    seams.team_error = OSError("offline")
    orch = _Orch()
    orch._slack_team_id = ""

    asyncio.run(rc.record_connected_workspace(orch))  # type: ignore[arg-type]
    assert orch._slack_team_id == ""
    assert orch._slack_workspace_pinned is True


class _AuthTest:
    def __init__(self, resp: dict[str, Any]) -> None:
        self.resp = resp

    async def auth_test(self) -> dict[str, Any]:
        return self.resp


def test_an_org_wide_grid_install_is_identified_by_its_enterprise() -> None:
    # An org-wide Enterprise Grid install answers auth.test with no team_id:
    # its org is the workspace identity the pin holds, so Reconnect neither
    # fails on it nor treats a workspace token as the same identity.
    org = {"ok": True, "team_id": None, "enterprise_id": "E0ORG", "is_enterprise_install": True}
    assert asyncio.run(rc._team_of(_AuthTest(org))) == "E0ORG"
    workspace = {"ok": True, "team_id": "T0CHILD", "enterprise_id": "E0ORG"}
    assert asyncio.run(rc._team_of(_AuthTest(workspace))) == "T0CHILD"


@pytest.mark.parametrize(
    "resp",
    [
        {"ok": True, "team_id": None},
        {"ok": True, "team_id": ""},
        {"ok": True, "team_id": None, "enterprise_id": "E0ORG"},
        {"ok": True, "team_id": None, "enterprise_id": "", "is_enterprise_install": True},
        {"ok": True, "team_id": 7, "enterprise_id": "E0ORG", "is_enterprise_install": True},
    ],
    ids=["no-team", "empty-team", "enterprise-not-org-install", "org-no-enterprise", "bad-team"],
)
def test_any_other_answer_without_a_team_still_raises(resp: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        asyncio.run(rc._team_of(_AuthTest(resp)))


def test_a_bot_token_slack_rejects_at_boot_releases_the_pin(seams) -> None:
    # The token never authenticated and the socket never connected: no work
    # can be bound to any workspace.
    seams.team_error = _rejected("invalid_auth")
    orch = _Orch()
    orch._slack_team_id = ""
    orch.dashboard_state.slack_socket_connected = False

    asyncio.run(rc.record_connected_workspace(orch))  # type: ignore[arg-type]
    assert orch._slack_workspace_pinned is False


@pytest.mark.parametrize("code", ["token_revoked", "token_expired", "account_inactive"])
def test_a_token_revoked_after_boot_keeps_the_pin(seams, code: str) -> None:
    # The token authenticated once, so the pin stays. Its workspace was never
    # named, and the revoked boot token cannot name it now, so another token
    # is refused: it might belong to a different workspace.
    seams.team_error = _rejected(code)
    orch = _Orch()
    orch._slack_team_id = ""
    orch.dashboard_state.slack_socket_connected = False

    asyncio.run(rc.record_connected_workspace(orch))  # type: ignore[arg-type]
    assert orch._slack_workspace_pinned is True
    seams.team_error = None
    seams.token_errors = {"xoxb-old": _rejected(code)}
    seams.team = "T0OTHER"
    assert _run(orch) == {"connected": False, "connect_error": rc.WORKSPACE_SWITCH_NEEDS_RESTART}
    assert orch._slack_team_id == ""
    assert seams.init_calls == []


@pytest.mark.parametrize("work", ["socket", "pending", "running", "held"])
def test_a_rejected_boot_token_keeps_the_pin_while_work_is_admitted(seams, work: str) -> None:
    # An authentication failure alone does not prove no work was bound: a
    # connected socket delivers messages, and queued, running or held turns
    # name the boot workspace's channels.
    from kiro_crew.slack import events_queue as eq

    seams.team_error = _rejected("invalid_auth")
    orch = _Orch()
    orch._slack_team_id = ""
    orch.dashboard_state.slack_socket_connected = work == "socket"
    if work == "pending":
        orch._pending_queue = {"1.0": [("1.0", "ping", {})]}  # type: ignore[attr-defined]
    if work == "running":
        orch._session_tasks = {"1.0": MagicMock()}  # type: ignore[attr-defined]
    if work == "held":
        eq._held_while_slack_down[orch] = {"1.0"}
    try:
        asyncio.run(rc.record_connected_workspace(orch))  # type: ignore[arg-type]
    finally:
        eq._held_while_slack_down.pop(orch, None)
    assert orch._slack_workspace_pinned is True


def test_a_failed_boot_socket_still_pins_the_published_workspace(seams) -> None:
    # Boot published workspace A's Web API client but the socket never
    # connected; saving workspace B and reconnecting must still be refused.
    seams.team = "T0OTHER"
    orch = _Orch()
    orch._slack_team_id = "T0SAME"
    orch._slack_workspace_pinned = True
    result = _run(orch)

    assert result["connect_error"] == rc.WORKSPACE_SWITCH_NEEDS_RESTART
    assert seams.init_calls == []


def test_a_boot_that_published_no_client_pins_nothing(seams) -> None:
    # A leftover bot token without its app token: boot built no Web API
    # client, so no work can be bound to that workspace and the reconnect
    # adopts the workspace the user saves.
    orch = _Orch()
    orch.slack = None
    orch.dashboard_state.slack_client = None
    orch._slack_workspace_pinned = False
    orch._slack_team_id = ""

    assert rc.pin_boot_workspace(orch) is None  # type: ignore[arg-type]
    assert orch._slack_workspace_pinned is False

    seams.team = "T0CONFIGURED"
    result = _run(orch)
    assert result == {"connected": True, "connect_error": ""}
    assert orch._slack_team_id == "T0CONFIGURED"


def test_a_boot_that_published_a_client_pins_its_workspace(seams) -> None:
    seams.team = "T0BOOT"
    orch = _Orch()
    orch._slack_workspace_pinned = False
    orch._slack_team_id = ""

    async def _go() -> None:
        task = rc.pin_boot_workspace(orch)  # type: ignore[arg-type]
        assert task is not None
        assert orch._slack_workspace_pinned is True
        await task

    asyncio.run(_go())
    assert orch._slack_team_id == "T0BOOT"


def test_a_client_published_only_to_the_dashboard_still_pins(seams) -> None:
    # Boot published the client to the dashboard, then init_socket_mode
    # cleared orch.slack alone (an enterprise or owner check failed); the
    # dashboard senders still post through that client.
    seams.team = "T0BOOT"
    orch = _Orch()
    orch.slack = None
    orch.dashboard_state.slack_client = "client:boot"
    orch._slack_workspace_pinned = False
    orch._slack_team_id = ""

    async def _go() -> None:
        task = rc.pin_boot_workspace(orch)  # type: ignore[arg-type]
        assert task is not None
        await task

    asyncio.run(_go())
    assert orch._slack_workspace_pinned is True
    assert orch._slack_team_id == "T0BOOT"
    seams.team = "T0OTHER"
    assert _run(orch)["connect_error"] == rc.WORKSPACE_SWITCH_NEEDS_RESTART


# ── queued messages while the socket is down ────────────────────────────────


class _QueueOrch:
    """The attributes ``_drain_slack_queue`` touches."""

    def __init__(self, *, reconnecting: bool = True) -> None:
        # By default a reconnect is running and has cleared the client.
        self.slack: Any = None
        self._slack_reconnecting = reconnecting
        self.sessions = _Queue()
        self._session_tasks: dict[str, Any] = {}
        self._pending_queue: dict[str, list[Any]] = {}
        self._handler_tasks: set[asyncio.Task] = set()


class _Queue:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict]] = [("1.0", "ping", {})]

    def dequeue(self, key: str) -> tuple[str, str, dict] | None:
        return self.entries.pop(0) if self.entries else None


async def _settle_handlers(orch: Any) -> None:
    # A finished task leaves ``_handler_tasks`` in its done callback, which
    # runs on a later loop pass; gather() over tasks that are all finished
    # returns without one, so the wait polls under the lost-run bound.
    async def _settled() -> bool:
        if orch._handler_tasks:
            await asyncio.gather(*list(orch._handler_tasks))
        return not orch._handler_tasks

    await async_wait_until(
        _settled, describe=lambda: sorted(t.get_name() for t in orch._handler_tasks)
    )


def test_a_drain_while_slack_is_down_keeps_the_entry_and_resumes_it(monkeypatch) -> None:
    from kiro_crew.slack import events_queue as eq

    dispatched: list[tuple[str, str, Any]] = []

    async def _dispatch(
        orch: Any, key: str, ts: str, text: str, kw: dict, *, slack: Any = None
    ) -> None:
        dispatched.append((key, ts, slack))

    monkeypatch.setattr(slack_events, "_dispatch_queued", _dispatch)
    orch = _QueueOrch()

    async def _go() -> None:
        await eq._drain_slack_queue(orch, "1.0")  # type: ignore[arg-type]
        # Not dequeued: dispatching into the cleared client would lose it.
        assert orch.sessions.entries == [("1.0", "ping", {})]
        assert dispatched == []
        orch.slack = "client:new"
        eq.resume_held_slack_drains(orch)  # type: ignore[arg-type]
        await _settle_handlers(orch)

    asyncio.run(_go())
    assert dispatched == [("1.0", "1.0", "client:new")]
    assert orch.sessions.entries == []


def test_a_drain_with_no_client_outside_a_reconnect_dispatches_as_before(monkeypatch) -> None:
    from kiro_crew.slack import events_queue as eq

    dispatched: list[tuple[str, str, Any]] = []

    async def _dispatch(
        orch: Any, key: str, ts: str, text: str, kw: dict, *, slack: Any = None
    ) -> None:
        dispatched.append((key, ts, slack))

    monkeypatch.setattr(slack_events, "_dispatch_queued", _dispatch)
    orch = _QueueOrch(reconnecting=False)

    async def _go() -> None:
        await eq._drain_slack_queue(orch, "1.0")  # type: ignore[arg-type]
        await _settle_handlers(orch)

    asyncio.run(_go())
    assert dispatched == [("1.0", "1.0", None)]
    assert orch not in eq._held_while_slack_down


def test_the_reconnect_window_is_open_only_while_the_attempt_runs(seams) -> None:
    orch = _Orch()
    assert _run(orch) == {"connected": True, "connect_error": ""}
    assert orch.connect_saw_window == [True]
    assert rc.reconnect_in_progress(orch) is False


def test_the_reconnect_window_closes_after_a_failed_attempt(seams) -> None:
    orch = _Orch(connect_ok=False)
    assert _run(orch)["connected"] is False
    assert rc.reconnect_in_progress(orch) is False


def test_a_connected_reconnect_resumes_the_held_drains(seams, monkeypatch) -> None:
    from kiro_crew.slack import events_queue as eq

    drained: list[tuple[str, Any]] = []

    async def _drain(orch: Any, key: str) -> None:
        drained.append((key, orch.slack))

    monkeypatch.setattr(slack_events, "_drain_slack_queue", _drain)
    orch = _Orch()
    eq._held_while_slack_down[orch] = {"9.0"}

    async def _go() -> None:
        await rc.reconnect_slack(orch)  # type: ignore[arg-type]
        await _settle_handlers(orch)

    asyncio.run(_go())
    assert drained == [("9.0", "client:xoxb-placeholder")]
    assert orch not in eq._held_while_slack_down


def test_a_failed_reconnect_keeps_the_drains_held(seams, monkeypatch) -> None:
    from kiro_crew.slack import events_queue as eq

    drained: list[str] = []

    async def _drain(orch: Any, key: str) -> None:
        drained.append(key)

    monkeypatch.setattr(slack_events, "_drain_slack_queue", _drain)
    orch = _Orch(connect_ok=False)
    eq._held_while_slack_down[orch] = {"9.0"}

    _run(orch)
    assert drained == []
    assert eq._held_while_slack_down[orch] == {"9.0"}


def _with_queues(orch: _Orch, entries: list[tuple[str, str, dict]]) -> _Orch:
    """Give the reconnect stand-in the queue attributes the drains read."""
    queue = _Queue()
    queue.entries = list(entries)
    orch.sessions = queue  # type: ignore[attr-defined]
    orch._session_tasks = {}  # type: ignore[attr-defined]
    orch._pending_queue = {}  # type: ignore[attr-defined]
    return orch


def _record_dispatches(monkeypatch) -> list[tuple[str, str, Any]]:
    dispatched: list[tuple[str, str, Any]] = []

    async def _dispatch(
        orch: Any, key: str, ts: str, text: str, kw: dict, *, slack: Any = None
    ) -> None:
        dispatched.append((key, ts, slack))

    monkeypatch.setattr(slack_events, "_dispatch_queued", _dispatch)
    return dispatched


def test_a_message_queued_across_a_failed_reconnect_is_delivered_once_on_retry(
    seams, monkeypatch
) -> None:
    # The turn that queued it ends after the attempt failed (tokens_missing):
    # its tail drain must hold the entry, not dispatch it into no client.
    from kiro_crew.slack import events_queue as eq

    dispatched = _record_dispatches(monkeypatch)
    orch = _with_queues(_Orch(), [("1.0", "ping", {"channel": "C1"})])
    seams.creds = {"KIROCREW_OWNER_ID": "U0NEWOWNER"}

    async def _go() -> None:
        first = await rc.reconnect_slack(orch)  # type: ignore[arg-type]
        assert first == {"connected": False, "connect_error": rc.TOKENS_MISSING}
        assert rc.reconnect_in_progress(orch) is False
        assert rc.client_lost_to_reconnect(orch) is True
        await eq._drain_slack_queue(orch, "1.0")  # type: ignore[arg-type]
        assert orch.sessions.entries == [("1.0", "ping", {"channel": "C1"})]  # type: ignore[attr-defined]
        assert dispatched == []
        seams.creds = dict(GOOD)
        second = await rc.reconnect_slack(orch)  # type: ignore[arg-type]
        assert second["connected"] is True
        await _settle_handlers(orch)

    asyncio.run(_go())
    assert dispatched == [("1.0", "1.0", "client:xoxb-placeholder")]
    assert orch.sessions.entries == []  # type: ignore[attr-defined]
    assert rc.client_lost_to_reconnect(orch) is False
    assert orch not in eq._held_while_slack_down
    orch.dashboard_state.notify.assert_not_called()


def test_a_reconnect_that_never_recovers_notifies_each_held_message(seams, monkeypatch) -> None:
    from kiro_crew.slack import events_queue as eq

    monkeypatch.setattr(rc, "HELD_WORK_GIVE_UP_SECS", 0.0)
    # The give-up window is held open until the drain below has run: with a
    # zero delay it could otherwise end before the reconnect's caller resumes.
    window_open = asyncio.Event()
    real_give_up = rc._give_up_after

    async def _gated_give_up(o: Any, delay: float) -> None:
        await window_open.wait()
        await real_give_up(o, delay)

    monkeypatch.setattr(rc, "_give_up_after", _gated_give_up)
    dispatched = _record_dispatches(monkeypatch)
    orch = _with_queues(_Orch(connect_ok=False), [("1.0", "ping", {"channel": "C1"})])
    orch._pending_queue["2.0"] = [("2.0", "pong", {"channel": "C2"})]  # type: ignore[attr-defined]

    async def _go() -> None:
        await rc.reconnect_slack(orch)  # type: ignore[arg-type]
        give_up = orch._slack_give_up_task  # type: ignore[attr-defined]
        assert isinstance(give_up, asyncio.Task) and not give_up.done()
        await eq._drain_slack_queue(orch, "1.0")  # type: ignore[arg-type]
        assert dispatched == []
        window_open.set()
        await asyncio.wait_for(give_up, timeout=_RELEASED_TASK_TIMEOUT_SECS)

    asyncio.run(_go())
    assert dispatched == []
    notices = orch.dashboard_state.notify.call_args_list
    assert [c.args[1] for c in notices] == ["Slack: message not delivered"] * 2
    assert sorted(c.kwargs["meta"]["msg_ts"] for c in notices) == ["1.0", "2.0"]
    assert orch.sessions.entries == []  # type: ignore[attr-defined]
    assert orch._pending_queue == {}  # type: ignore[attr-defined]
    assert orch not in eq._held_while_slack_down
    # The hold is released: a later send with no client goes on as before.
    assert rc.client_lost_to_reconnect(orch) is False
    assert eq.hold_if_slack_down(orch, "3.0") is False  # type: ignore[arg-type]


def test_the_undelivered_notice_names_the_channel_and_message_in_words() -> None:
    from kiro_crew.slack import events_queue as eq

    long_text = "deploy " + "x" * 120
    entries = [
        ("1.0", "ingested", {"channel": "C1", "spool_text": "hi <@U0ALICE> see <#C9|ops>"}),
        ("2.0", "ingested", {"channel": "C2", "spool_text": "", "spool_attachments": 1}),
        (
            "3.0",
            "ingested",
            {"channel": "D1", "spool_text": "", "spool_attachments": 1, "spool_voice_memos": 1},
        ),
        ("4.0", "ingested", {"channel": "C1", "spool_text": long_text}),
    ]
    orch = _QueueOrch()
    orch.sessions.entries = list(entries)
    orch.dashboard_state = _State()  # type: ignore[attr-defined]
    orch._cfg = SimpleNamespace(  # type: ignore[attr-defined]
        slack=SimpleNamespace(tracking_channels=[{"channel_id": "C1", "name": "general"}])
    )
    eq._held_while_slack_down[orch] = {"k"}

    assert eq.give_up_held_slack_work(orch) == 4  # type: ignore[arg-type]
    bodies = [c.args[2] for c in orch.dashboard_state.notify.call_args_list]  # type: ignore[attr-defined]
    assert bodies[0].startswith(
        'Slack did not reconnect, so your message "hi @someone see #ops" in #general '
    )
    assert "so an attachment in a Slack channel was not" in bodies[1]
    assert "so a voice memo in a direct message was not" in bodies[2]
    preview = long_text[:80] + "..."
    assert f'your message "{preview}" in #general' in bodies[3]
    for body in bodies:
        for raw in ("U0ALICE", "C1", "C2", "C9", "D1", "1.0", "2.0", "3.0", "4.0"):
            assert raw not in body
    metas = [c.kwargs["meta"] for c in orch.dashboard_state.notify.call_args_list]  # type: ignore[attr-defined]
    assert [(m["channel"], m["msg_ts"]) for m in metas] == [
        ("C1", "1.0"),
        ("C2", "2.0"),
        ("D1", "3.0"),
        ("C1", "4.0"),
    ]


def test_a_failed_reconnect_on_a_gateway_with_no_client_holds_nothing(seams) -> None:
    from kiro_crew.slack import events_queue as eq

    orch = _Orch(connect_ok=False)
    orch.slack = None
    orch.dashboard_state.slack_client = None
    _run(orch)
    assert rc.client_lost_to_reconnect(orch) is False
    assert getattr(orch, "_slack_give_up_task", None) is None
    assert eq.hold_if_slack_down(orch, "1.0") is False  # type: ignore[arg-type]


def test_a_dispatch_keeps_the_client_its_drain_read(monkeypatch) -> None:
    # A reconnect that clears orch.slack while the dispatch awaits the
    # reaction removal must not hand the turn a None client.
    from kiro_crew.slack import events_queue as eq

    seen: list[Any] = []

    async def _handle(client: Any, *args: Any, **kwargs: Any) -> None:
        seen.append(client)

    cfg = SimpleNamespace(
        channel_config=lambda channel: SimpleNamespace(activation="always"),
        messaging=SimpleNamespace(use_transport=False),
    )
    monkeypatch.setattr(slack_events, "handle_message", _handle)
    monkeypatch.setattr(slack_events, "slack_cfg", lambda orch: cfg)
    monkeypatch.setattr(slack_events, "_resolve_approval_mode", lambda orch: "auto")
    orch = MagicMock()
    client = MagicMock()

    async def _remove_reaction(*args: Any) -> None:
        orch.slack = None

    client.remove_reaction = _remove_reaction
    orch.slack = client

    asyncio.run(eq._dispatch_queued(orch, "1.0", "1.0", "ping", {"channel": "C1"}, slack=client))
    assert seen == [client]


# ── the route ────────────────────────────────────────────────────────────────


def _post(
    monkeypatch, *, local: bool = True, reconnect: Any = None, sel: Any = None
) -> tuple[int, dict]:
    monkeypatch.setattr(msg, "is_direct_local_request", lambda req: local)
    sel = sel or MagicMock()
    monkeypatch.setattr(msg, "_sel", lambda: sel)

    async def _go() -> tuple[int, dict]:
        app = web.Application()
        app["state"] = SimpleNamespace(slack_reconnect=reconnect)
        app.router.add_post("/api/slack/reconnect", msg.api_slack_reconnect)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/slack/reconnect")
            return resp.status, json.loads(await resp.text())

    return asyncio.run(_go())


def test_route_answers_the_get_shape(monkeypatch) -> None:
    reconnect = AsyncMock(return_value={"connected": False, "connect_error": "invalid_auth"})
    status, body = _post(monkeypatch, reconnect=reconnect)

    assert status == 200
    assert body == {"connected": False, "connect_error": "invalid_auth"}
    reconnect.assert_awaited_once()


def test_route_logs_a_refusal_that_left_the_connection_up_as_an_error(monkeypatch) -> None:
    sel = MagicMock()
    refused = {"connected": True, "connect_error": rc.WORKSPACE_SWITCH_NEEDS_RESTART}
    status, body = _post(monkeypatch, reconnect=AsyncMock(return_value=refused), sel=sel)

    assert status == 200
    assert body == refused
    assert sel.log_api_access.call_args.kwargs["outcome"] == "error"


def test_route_denies_remote_sessions_like_the_save(monkeypatch) -> None:
    reconnect = AsyncMock()
    status, body = _post(monkeypatch, local=False, reconnect=reconnect)

    assert status == 403
    assert body["code"] == "remote_read_only"
    reconnect.assert_not_awaited()


def test_route_without_a_socket_owner_is_unavailable(monkeypatch) -> None:
    status, body = _post(monkeypatch, reconnect=None)

    assert status == 503
    assert body["code"] == "slack_reconnect_unavailable"


def test_route_reports_an_unreadable_credential_store(monkeypatch) -> None:
    reconnect = AsyncMock(side_effect=rc.CredentialStoreUnreadable("OSError"))
    status, body = _post(monkeypatch, reconnect=reconnect)

    assert status == 500
    assert body["code"] == "credential_store_unreadable"


def test_route_is_registered_as_a_post_beside_the_save() -> None:
    from kiro_crew.dashboard.routes import messaging as routes

    app = web.Application()
    routes.register(app)
    methods = {
        route.method
        for route in app.router.routes()
        if route.resource is not None and route.resource.canonical == "/api/slack/reconnect"
    }
    assert methods == {"POST"}


# ── the owner is never changed in place ─────────────────────────────────────


def test_a_saved_owner_change_is_refused_and_asks_for_a_restart(seams) -> None:
    # Queued, held and running Slack work was admitted under the current owner:
    # Reconnect refuses a different saved owner before anything is torn down.
    seams.creds = dict(GOOD, KIROCREW_OWNER_ID="U0OTHEROWNER")
    orch = _Orch()
    result = _run(orch)

    assert result == {"connected": True, "connect_error": rc.OWNER_CHANGE_NEEDS_RESTART}
    assert seams.init_calls == []
    orch.old_socket.close.assert_not_awaited()
    assert orch.slack == "old-client"
    assert (orch._owner_id, orch._allowed_users) == ("U0NEWOWNER", {"U0NEWOWNER"})
    assert seams.owner_ids == []


@pytest.mark.parametrize("saved_owner", ["", "U0OTHEROWNER"])
def test_removing_or_replacing_the_owner_is_refused(seams, saved_owner: str) -> None:
    seams.creds = dict(GOOD, KIROCREW_OWNER_ID=saved_owner)
    orch = _Orch()
    assert _run(orch)["connect_error"] == rc.OWNER_CHANGE_NEEDS_RESTART
    assert orch._owner_id == "U0NEWOWNER"


def test_a_token_only_save_reconnects_in_place(seams) -> None:
    # New tokens under the same owner: the PR's purpose, applied live.
    seams.creds = dict(GOOD, SLACK_APP_TOKEN="xapp-rotated", SLACK_BOT_TOKEN="xoxb-rotated")
    orch = _Orch()
    result = _run(orch)

    assert result == {"connected": True, "connect_error": ""}
    (call,) = seams.init_calls
    assert (call["app"], call["bot"], call["owner"]) == (
        "xapp-rotated",
        "xoxb-rotated",
        "U0NEWOWNER",
    )
    orch.old_socket.close.assert_awaited_once()


def test_a_gateway_with_no_owner_yet_takes_the_saved_one(seams) -> None:
    # Nothing was admitted without an owner, so the first one applies live.
    orch = _Orch()
    orch._owner_id = ""
    orch._allowed_users = set()
    assert _run(orch) == {"connected": True, "connect_error": ""}
    assert orch._owner_id == "U0NEWOWNER"


def test_the_handshake_runs_under_the_dashboard_config_lock(seams, monkeypatch) -> None:
    # The enterprise re-validation inside reads the allowlist; under the lock
    # every config writer takes, it cannot read a save's file mid-transaction.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    held: list[bool] = []

    async def _init(orch: Any, cache: Any, *, declared_yolo: bool = True) -> None:
        held.append(_get_config_lock().locked())
        orch._socket_client = MagicMock(close=AsyncMock())

    monkeypatch.setattr(slack_events, "init_socket_mode", _init)
    assert _run(_Orch()) == {"connected": True, "connect_error": ""}
    assert held == [True]
