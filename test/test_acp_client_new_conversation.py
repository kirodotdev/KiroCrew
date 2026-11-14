"""``AcpClient.new_conversation``: a fresh claude conversation on the live adapter.

The replies are the ones claude-agent-acp 0.94.0 gave on one process
(``acp_frames/claude/new-conversation-live.jsonl``): ``session/close`` answers ``{}``
and ``session/new`` answers a new id. The settings seed and the array are prepared
again before the old session is closed, the new session is set up as a cold start sets
one up, and any failure on the way ends the process instead of leaving a half-set-up
session behind.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import client as client_facade
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError, AcpProcessDied
from kiro_crew.acp.types import METHOD_SESSION_CLOSE, METHOD_SESSION_NEW, METHOD_SET_MODE
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_OPENCODE,
    CC_PERMISSION_MODE_ENV,
    resolve_cc_permission_mode,
)
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

#: A live claude-agent-acp 0.100.0 capture of a session that requested ``auto``.
_REQUESTED_AUTO = (
    Path(__file__).parent / "fixtures" / "claude_mode_pin" / "requested-auto-applied.jsonl"
)


def _live_claude(tmp_path, monkeypatch, steps: list[str]) -> AcpClient:
    """A claude client on session ``sess-1`` whose wire and setup steps are recorded."""
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE, work_dir=tmp_path)
    client._session_id = "sess-1"
    client.last_prompt_stats.context_pct = 60.0
    client._between_turn_credits = 2.0
    client._buffer.append(MagicMock(name="a frame the old session left"))
    client._is_process_alive = lambda: True  # type: ignore[method-assign]

    def step(name: str) -> AsyncMock:
        return AsyncMock(side_effect=lambda *a, **k: steps.append(name))

    async def send_request(method: str, params: dict) -> int:
        steps.append(f"{method} {params.get('sessionId')}")
        return 4

    async def new_session() -> dict:
        steps.append(f"{METHOD_SESSION_NEW} (buffer {len(client._buffer)})")
        return {"sessionId": "sess-2"}

    monkeypatch.setattr(
        client_facade,
        "require_unchanged_derived_spec",
        lambda snapshot: steps.append("derived spec"),
    )
    client._seed_session_settings = step("seed")  # type: ignore[method-assign]
    client._prepare_claude_side_read_denies = step("side denies")  # type: ignore[method-assign]
    client._prepare_session_mcp = step("mcp array")  # type: ignore[method-assign]
    client._send_request = send_request  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]
    client._new_session_following_substitution = new_session  # type: ignore[method-assign]
    client._persist_advertised_models_if_changed = AsyncMock()  # type: ignore[method-assign]
    client._reseed_after_capture = AsyncMock()  # type: ignore[method-assign]
    client._pin_claude_starting_mode = step("pin mode")  # type: ignore[method-assign]
    client._start_mount_check = MagicMock()  # type: ignore[method-assign]
    client.confirm_member_projection = step("confirm projection")  # type: ignore[method-assign]
    client._apply_startup_model = step("model")  # type: ignore[method-assign]
    client._drain_notifications = step("drain")  # type: ignore[method-assign]
    client._cleanup_failed_live_spawn = step("kill")  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_the_old_claude_session_is_closed_before_the_new_one_is_set_up(tmp_path, monkeypatch):
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)

    await client.new_conversation()

    assert steps == [
        "seed",
        "side denies",
        "mcp array",
        f"{METHOD_SESSION_CLOSE} sess-1",
        f"{METHOD_SESSION_NEW} (buffer 0)",
        "pin mode",
        "derived spec",
        "confirm projection",
        "model",
        "drain",
    ]
    assert client._session_id == "sess-2"
    assert client.last_prompt_stats.context_pct == 0.0
    assert client.last_prompt_stats.cost_baseline_sid == "sess-2"
    assert client._between_turn_credits == 0.0


def _recorded_requested_auto_modes() -> dict:
    """The ``modes`` of the recorded ``session/new`` answer: ``default``, offering ``auto``."""
    rows = [json.loads(line) for line in _REQUESTED_AUTO.read_text(encoding="utf-8").splitlines()]
    modes = next(row["result"]["modes"] for row in rows if row.get("id") == 2)
    assert modes["currentModeId"] == "default"
    assert "auto" in {m["id"] for m in modes["availableModes"]}
    return modes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("started", "pinned"),
    [("default", None), ("bypassPermissions", "default")],
    ids=["reported-default", "pinned-to-default"],
)
@pytest.mark.parametrize("opted_in", [True, False], ids=["opt-in", "no-opt-in"])
async def test_a_requested_auto_is_applied_to_the_fresh_session_after_its_model(
    tmp_path, monkeypatch, opted_in, started, pinned
):
    """``KIROCREW_CC_PERMISSION_MODE=auto`` reaches a fresh session as it reaches a cold one.

    The new session answers as the recorded 0.100.0 session did. With the opt-in it is
    moved to ``auto`` after the startup model, from the mode the pin returned when the
    pin moved it; without the opt-in no ``session/set_mode`` is sent.
    """
    if opted_in:
        monkeypatch.setenv(CC_PERMISSION_MODE_ENV, "auto")
    else:
        monkeypatch.delenv(CC_PERMISSION_MODE_ENV, raising=False)
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._permission_mode = resolve_cc_permission_mode(None, ACP_BACKEND_CLAUDE)
    modes = {**_recorded_requested_auto_modes(), "currentModeId": started}
    sent: list[tuple[str, dict]] = []
    record_step = client._send_request

    async def send_request(method: str, params: dict) -> int:
        sent.append((method, params))
        return await record_step(method, params)

    async def new_session() -> dict:
        steps.append(f"{METHOD_SESSION_NEW} (buffer {len(client._buffer)})")
        return {"sessionId": "sess-2", "modes": modes}

    async def pin(_resp: dict) -> str | None:
        steps.append("pin mode")
        return pinned

    client._send_request = send_request  # type: ignore[method-assign]
    client._new_session_following_substitution = new_session  # type: ignore[method-assign]
    client._pin_claude_starting_mode = pin  # type: ignore[method-assign]

    await client.new_conversation()

    applied = [f"{METHOD_SET_MODE} sess-2"] if opted_in else []
    assert steps == [
        "seed",
        "side denies",
        "mcp array",
        f"{METHOD_SESSION_CLOSE} sess-1",
        f"{METHOD_SESSION_NEW} (buffer 0)",
        "pin mode",
        "derived spec",
        "confirm projection",
        "model",
        *applied,
        "drain",
    ]
    expected = [{"sessionId": "sess-2", "modeId": "auto"}] if opted_in else []
    assert [p for m, p in sent if m == METHOD_SET_MODE] == expected
    assert client._session_id == "sess-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True], ids=["applied", "refused"])
async def test_the_fresh_session_is_audited_as_auto_only_once_it_is_confirmed(
    tmp_path, monkeypatch, refused
):
    """The ``auto`` the replaced session was confirmed in does not carry over.

    A fresh session whose ``set_mode`` ``auto`` is refused keeps ``default``, so a call
    that never asked gets a row that names no approver, not Claude's classifier; one
    the adapter moves to ``auto`` gets the classifier's row.
    """
    rows: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            rows.append(kw)

    monkeypatch.setattr(client_facade, "sel", lambda: _Sel())
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._permission_mode = "auto"
    modes = _recorded_requested_auto_modes()
    client._track_claude_starting_mode(
        {"sessionId": "sess-1", "modes": {**modes, "currentModeId": "auto"}}
    )

    async def new_session() -> dict:
        return {"sessionId": "sess-2", "modes": modes}

    async def wait_for_response(_rid, **kw) -> dict:
        if refused and kw.get("method") == METHOD_SET_MODE:
            raise AcpError("JSON-RPC error: set_mode refused", code=-32603)
        return {}

    client._new_session_following_substitution = new_session  # type: ignore[method-assign]
    client._wait_for_response = wait_for_response  # type: ignore[method-assign]

    await client.new_conversation()
    client._observed_tool_calls["tc1"] = ("Read", "read")
    await client._maybe_audit_unasked_tool_call(
        types.SimpleNamespace(tool_call_id="tc1", tool_status="completed")
    )

    assert f"{METHOD_SET_MODE} sess-2" in steps
    assert [(r["outcome"], r["metadata"]["decided_by"]) for r in rows] == [
        ("auto_approved", "claude_unattributed" if refused else "claude_classifier")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [AcpError("ACP process exited (code=1)"), AcpProcessDied("ACP process pipe broken: EPIPE")],
    ids=["exited", "pipe-broken"],
)
async def test_a_child_that_dies_during_the_fresh_sessions_set_mode_auto_ends_the_process(
    tmp_path, monkeypatch, failure
):
    """No answer from a dead child is not a refused ``auto``: the fresh conversation
    fails and ends the process, as a failure in its model step does, so the caller's
    kill-and-respawn fallback starts a fresh one."""
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._permission_mode = "auto"
    modes = _recorded_requested_auto_modes()

    async def new_session() -> dict:
        return {"sessionId": "sess-2", "modes": modes}

    async def wait_for_response(_rid, **kw) -> dict:
        if kw.get("method") == METHOD_SET_MODE:
            raise failure
        return {}

    client._new_session_following_substitution = new_session  # type: ignore[method-assign]
    client._wait_for_response = wait_for_response  # type: ignore[method-assign]

    with pytest.raises(type(failure)) as raised:
        await client.new_conversation()

    assert raised.value is failure
    assert f"{METHOD_SET_MODE} sess-2" in steps
    assert "drain" not in steps
    assert steps[-1] == "kill"
    assert client._session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_model", [True, False], ids=["with-auto", "no-auto"])
@pytest.mark.parametrize("point", ["after-the-write", "in-the-wait", "in-the-drain"])
async def test_a_cancel_before_the_fresh_sessions_requested_auto_is_settled_ends_the_process(
    tmp_path, monkeypatch, auto_model, point
):
    """A cancel once the fresh session's ``set_mode`` ``auto`` is on the wire and
    before its mode reports are read ends the process, so no later prompt runs in the
    mode the adapter put it in (``acceptEdits`` on a model without Auto mode support)."""
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._permission_mode = "auto"
    modes = _recorded_requested_auto_modes()
    adapter_mode: list[str] = []
    record_step = client._send_request

    async def send_request(method: str, params: dict) -> int:
        rid = await record_step(method, params)
        if method == METHOD_SET_MODE and params.get("modeId") == "auto":
            adapter_mode.append("auto" if auto_model else "acceptEdits")
            if point == "after-the-write":
                raise asyncio.CancelledError
        return rid

    async def new_session() -> dict:
        return {"sessionId": "sess-2", "modes": modes}

    async def wait_for_response(_rid, **kw) -> dict:
        if point == "in-the-wait" and kw.get("method") == METHOD_SET_MODE:
            raise asyncio.CancelledError
        return {}

    async def drain() -> None:
        steps.append("drain")
        if point == "in-the-drain":
            raise asyncio.CancelledError

    client._send_request = send_request  # type: ignore[method-assign]
    client._new_session_following_substitution = new_session  # type: ignore[method-assign]
    client._wait_for_response = wait_for_response  # type: ignore[method-assign]
    client._drain_notifications = drain  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await client.new_conversation()

    assert adapter_mode == ["auto" if auto_model else "acceptEdits"]
    assert steps[-1] == "kill"
    assert client._session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("default_refused", [False, True], ids=["moved-back", "refused"])
async def test_a_fresh_session_left_in_the_fallback_is_moved_back_or_ended(
    tmp_path, monkeypatch, default_refused
):
    """The fresh session's ``set_mode`` ``auto`` lands in ``acceptEdits``, as on a model
    without Auto mode support: it is moved back to ``default`` before the drain, and a
    refused move ends the process as any other failure on the way does."""
    from kiro_crew.acp.types import JsonRpcMessage

    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._permission_mode = "auto"
    modes = _recorded_requested_auto_modes()
    sent: list[dict] = []
    record_step = client._send_request

    async def send_request(method: str, params: dict) -> int:
        await record_step(method, params)
        sent.append({"method": method, **params})
        return len(sent)

    async def new_session() -> dict:
        steps.append(f"{METHOD_SESSION_NEW} (buffer {len(client._buffer)})")
        return {"sessionId": "sess-2", "modes": modes}

    async def wait_for_response(rid, **_kw) -> dict:
        params = sent[rid - 1]
        if params.get("modeId") == "auto":
            client._mcp_notifications.append(
                JsonRpcMessage(
                    method="session/update",
                    params={
                        "sessionId": "sess-2",
                        "update": {
                            "sessionUpdate": "current_mode_update",
                            "currentModeId": "acceptEdits",
                        },
                    },
                )
            )
        if default_refused and params.get("modeId") == "default":
            raise AcpError("set_mode refused")
        return {}

    client._send_request = send_request  # type: ignore[method-assign]
    client._new_session_following_substitution = new_session  # type: ignore[method-assign]
    client._wait_for_response = wait_for_response  # type: ignore[method-assign]

    if default_refused:
        with pytest.raises(AcpError):
            await client.new_conversation()
        assert steps[-1] == "kill"
        assert "drain" not in steps
    else:
        await client.new_conversation()
        assert client._claude_confirmed_mode == "default"
        assert steps.index("drain") > steps.index("model")
    assert [p["modeId"] for p in sent if p["method"] == METHOD_SET_MODE] == ["auto", "default"]


@pytest.mark.asyncio
async def test_the_fresh_conversation_holds_no_picture_the_old_one_was_sent(tmp_path, monkeypatch):
    client = _live_claude(tmp_path, monkeypatch, [])
    staged = client._sent_images.staging()
    staged.stage(["a" * 64])
    staged.commit()

    await client.new_conversation()

    assert not client._sent_images.holds("a" * 64)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [AcpError("model refused"), asyncio.CancelledError()])
async def test_a_failure_on_the_way_ends_the_process(tmp_path, monkeypatch, failure):
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._apply_startup_model = AsyncMock(side_effect=failure)  # type: ignore[method-assign]

    with pytest.raises(type(failure)):
        await client.new_conversation()

    assert steps[-1] == "kill"
    assert client._session_id is None


@pytest.mark.asyncio
async def test_a_replaced_settings_file_gives_way_to_a_cold_start(tmp_path, monkeypatch):
    """Only a fresh client leaves a replaced settings file out and pins the asking mode."""
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._claude_settings_authored = True

    async def reseed() -> None:
        steps.append("seed")
        client._claude_settings_authored = False

    client._seed_session_settings = reseed  # type: ignore[method-assign]

    with pytest.raises(AcpError, match="settings file was replaced"):
        await client.new_conversation()

    assert steps == ["seed", "kill"]


@pytest.mark.asyncio
async def test_a_backend_outside_the_set_raises_naming_it_and_starts_nothing(tmp_path):
    client = AcpClient(acp_backend=ACP_BACKEND_OPENCODE, work_dir=tmp_path)
    client._new_session_following_substitution = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(NotImplementedError, match="'opencode' backend has no fresh-conversation"):
        await client.new_conversation()

    client._new_session_following_substitution.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_the_closed_sessions_connection_bridges_go_after_session_close(
    tmp_path, monkeypatch, fails
):
    """The fresh array projects its own bridges; the closed session's would only pile
    up in the gateway's shared bridge table until the process ended."""
    from kiro_crew.acp import kas_env_launch

    steps: list[str] = []
    discarded: list[list[str]] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._crew_bridge_snapshots = ["old-bridge"]
    monkeypatch.setattr(
        kas_env_launch, "discard_batch", lambda agents, written: discarded.append(list(written))
    )

    async def fresh_array() -> None:
        steps.append("mcp array")
        client._crew_bridge_snapshots.append("new-bridge")

    client._prepare_session_mcp = fresh_array  # type: ignore[method-assign]
    if fails:
        client._wait_for_response = AsyncMock(side_effect=AcpError("close refused"))  # type: ignore[method-assign]
        with pytest.raises(AcpError):
            await client.new_conversation()
        # Never closed: the process end removes both batches together.
        assert discarded == [["new-bridge", "old-bridge"]]
    else:
        await client.new_conversation()
        assert discarded == [["old-bridge"]]
        assert client._crew_bridge_snapshots == ["new-bridge"]


@pytest.mark.asyncio
@pytest.mark.parametrize("default_refused", [False, True], ids=["moved-back", "refused"])
async def test_a_fresh_session_whose_set_mode_auto_times_out_is_moved_back_or_ended(
    tmp_path, monkeypatch, default_refused
):
    """The fresh session's ``set_mode`` ``auto`` goes unanswered, so the adapter may
    still apply it: the session is moved back to ``default`` before the drain, and a
    refused move ends the process as any other failure on the way does."""
    from kiro_crew.acp.transport_errors import AcpTimeoutError

    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._permission_mode = "auto"
    modes = _recorded_requested_auto_modes()
    sent: list[dict] = []
    record_step = client._send_request

    async def send_request(method: str, params: dict) -> int:
        await record_step(method, params)
        sent.append({"method": method, **params})
        return len(sent)

    async def new_session() -> dict:
        steps.append(f"{METHOD_SESSION_NEW} (buffer {len(client._buffer)})")
        return {"sessionId": "sess-2", "modes": modes}

    async def wait_for_response(rid, **_kw) -> dict:
        mode = sent[rid - 1].get("modeId")
        if mode == "auto":
            raise AcpTimeoutError("no answer")
        if default_refused and mode == "default":
            raise AcpError("set_mode refused")
        return {}

    client._send_request = send_request  # type: ignore[method-assign]
    client._new_session_following_substitution = new_session  # type: ignore[method-assign]
    client._wait_for_response = wait_for_response  # type: ignore[method-assign]

    if default_refused:
        with pytest.raises(AcpError):
            await client.new_conversation()
        assert steps[-1] == "kill"
        assert "drain" not in steps
    else:
        await client.new_conversation()
        assert client._claude_confirmed_mode == "default"
        assert steps.index("drain") > steps.index("model")
    assert [p["modeId"] for p in sent if p["method"] == METHOD_SET_MODE] == ["auto", "default"]


@pytest.mark.asyncio
@pytest.mark.parametrize("default_refused", [False, True], ids=["moved-back", "refused"])
async def test_a_fresh_session_whose_fallback_is_reported_late_is_moved_back_or_ended(
    tmp_path, monkeypatch, default_refused
):
    """The ``acceptEdits`` report for the fresh session's ``set_mode`` ``auto`` comes
    after the ``{}`` answer, so the drain reads it off the wire: the session is moved
    back to ``default`` before ``new_conversation`` returns, and a refused move ends
    the process as any other failure on the way does."""
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._permission_mode = "auto"
    client._kill_process = AsyncMock()  # type: ignore[method-assign]
    modes = _recorded_requested_auto_modes()
    sent: list[dict] = []
    record_step = client._send_request

    async def send_request(method: str, params: dict) -> int:
        await record_step(method, params)
        sent.append({"method": method, **params})
        return len(sent)

    async def new_session() -> dict:
        steps.append(f"{METHOD_SESSION_NEW} (buffer {len(client._buffer)})")
        return {"sessionId": "sess-2", "modes": modes}

    async def wait_for_response(rid, **_kw) -> dict:
        if default_refused and sent[rid - 1].get("modeId") == "default":
            raise AcpError("set_mode refused")
        return {}

    late = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "sess-2",
            "update": {"sessionUpdate": "current_mode_update", "currentModeId": "acceptEdits"},
        },
    }
    process = MagicMock()
    process.returncode = None
    process.stdout.readline = AsyncMock(return_value=(json.dumps(late) + "\n").encode())
    client._process = process

    async def drain() -> None:
        steps.append("drain")
        await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)

    client._send_request = send_request  # type: ignore[method-assign]
    client._new_session_following_substitution = new_session  # type: ignore[method-assign]
    client._wait_for_response = wait_for_response  # type: ignore[method-assign]
    client._drain_notifications = drain  # type: ignore[method-assign]

    if default_refused:
        with pytest.raises(AcpError):
            await client.new_conversation()
        assert steps[-1] == "kill"
    else:
        await client.new_conversation()
        assert client._claude_confirmed_mode == "default"
    assert [p["modeId"] for p in sent if p["method"] == METHOD_SET_MODE] == ["auto", "default"]
    assert steps.index("drain") < max(
        i for i, s in enumerate(steps) if s == f"{METHOD_SET_MODE} sess-2"
    )
