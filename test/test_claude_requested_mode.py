"""A claude session that asked for ``auto`` is put in ``auto`` before its first prompt.

The seed writes the request as ``permissions.defaultMode`` in the session's
``settings.local.json``, and the Claude Agent SDK drops it from that tier, so
``session/new`` reports ``default``. ``_apply_claude_requested_mode``
sends ``session/set_mode`` ``auto`` then, and only then: every other session, mode or
answer leaves the session as it started. The handshake tests replay a live
claude-agent-acp 0.100.0 capture, ``test/fixtures/claude_mode_pin/requested-auto-applied.jsonl``.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from unittest import mock

import pytest
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _no_ambient_pin_setting  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _PIN_SETTING_ENV, _write_spec

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, JsonRpcMessage

_FIXTURE = Path(__file__).parent / "fixtures" / "claude_mode_pin" / "requested-auto-applied.jsonl"
_LOGGER = "kiro_crew.acp.client"


def _recorded() -> dict:
    """The adapter's answers from the capture, keyed by the request they answered."""
    rows = [json.loads(line) for line in _FIXTURE.read_text(encoding="utf-8").splitlines()]
    head = rows[0]["_meta"]
    assert head["agent_version"] == "0.100.0" and head["recorded"] == "live"
    sent = [label.split(" ")[0] for label in head["client_sent"]]
    results = [row["result"] for row in rows[1:] if "id" in row and "result" in row]
    assert len(results) == len(sent)
    return dict(zip(sent, results))


def _modes(current: str, available=("default", "acceptEdits", "plan", "auto")) -> dict:
    return {"currentModeId": current, "availableModes": [{"id": m} for m in available]}


def _client(tmp_path, *, permission_mode="auto", **kw) -> AcpClient:
    return AcpClient(
        work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, permission_mode=permission_mode, **kw
    )


def _harness(client, monkeypatch, *, fail=None, mode_update=None, mode_update_session=None):
    """Record what the client sends; ``fail`` raises from the answer, and
    ``mode_update`` is a ``current_mode_update`` buffered before it, as the
    adapter sends one ahead of the ``set_mode`` answer when it falls back.
    ``mode_update_session`` names another session for that update."""
    sent: list[tuple[str, dict]] = []
    killed: list[bool] = []

    async def _send(method, params):
        sent.append((method, params))
        return len(sent)

    async def _wait(_rid, **_kw):
        if fail is not None:
            raise fail
        if mode_update is not None:
            client._mcp_notifications.append(
                JsonRpcMessage(
                    method="session/update",
                    params={
                        "sessionId": mode_update_session or sent[-1][1]["sessionId"],
                        "update": {
                            "sessionUpdate": "current_mode_update",
                            "currentModeId": mode_update,
                        },
                    },
                )
            )
        return {}

    async def _kill(*, force=False):
        killed.append(force)

    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    monkeypatch.setattr(client, "_kill_process", _kill)
    return sent, killed


@pytest.mark.asyncio
async def test_a_requested_auto_session_reported_default_is_moved_to_auto(
    tmp_path, monkeypatch, caplog
):
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await client._apply_claude_requested_mode({"sessionId": "s-1", "modes": _modes("default")})
    assert sent == [("session/set_mode", {"sessionId": "s-1", "modeId": "auto"})]
    assert killed == []
    assert [r.levelno for r in caplog.records if "requested permission mode" in r.message] == [
        logging.INFO
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resp",
    [
        {"modes": _modes("auto")},
        {"modes": _modes("plan")},
        {"modes": _modes("acceptEdits")},
        {"modes": _modes("dontAsk")},
        {"modes": _modes("bypassPermissions")},
        {"modes": {"availableModes": [{"id": "auto"}]}},
        {"modes": "default"},
        {},
    ],
    ids=["auto", "plan", "acceptEdits", "dontAsk", "bypass", "no-current", "not-a-dict", "none"],
)
async def test_any_other_reported_mode_is_left_alone(tmp_path, monkeypatch, resp):
    """Only ``default`` is moved: an operator's own posture is never replaced."""
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch)
    await client._apply_claude_requested_mode({"sessionId": "s-1", **resp})
    assert sent == []
    assert killed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kw",
    [
        {"permission_mode": None},
        {"permission_mode": "bypassPermissions"},
        {"side_read_only": True},
        {"zero_tools": True},
    ],
    ids=["not-requested", "other-request", "side-chat", "zero-tools"],
)
async def test_a_session_that_must_stay_narrow_sends_nothing(tmp_path, monkeypatch, kw):
    """No request, a Side Chat session or a zero-tool spec never gains ``auto``."""
    zero_tools = kw.pop("zero_tools", False)
    client = _client(tmp_path, **kw)
    client._spec_zero_tools = zero_tools
    sent, killed = _harness(client, monkeypatch)
    await client._apply_claude_requested_mode({"sessionId": "s-1", "modes": _modes("default")})
    assert sent == []
    assert killed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "available", [("default", "acceptEdits", "plan"), ()], ids=["without-auto", "empty"]
)
async def test_auto_not_offered_warns_once_and_keeps_default(
    tmp_path, monkeypatch, caplog, available
):
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await client._apply_claude_requested_mode(
            {"sessionId": "s-1", "modes": _modes("default", available)}
        )
    assert sent == []
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "keeps 'default'" in warnings[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [client_mod.AcpError("set_mode refused"), client_mod.AcpTimeoutError("no answer")],
    ids=["refused", "timed-out"],
)
async def test_a_failed_set_mode_warns_and_the_session_continues(
    tmp_path, monkeypatch, caplog, error
):
    """It fails toward the narrower mode: no raise, and the harness is not stopped."""
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch, fail=error)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await client._apply_claude_requested_mode({"sessionId": "s-1", "modes": _modes("default")})
    assert [m for m, _ in sent] == ["session/set_mode"]
    assert killed == []
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "keeps 'default'" in warnings[0]


@pytest.mark.asyncio
async def test_the_adapters_accept_edits_fallback_is_named(tmp_path, monkeypatch, caplog):
    """On a model without Auto mode support the adapter answers ``{}`` but reports
    ``acceptEdits`` first (claude-agent-acp ``SessionModeManager.selectMode``)."""
    client = _client(tmp_path)
    sent, _killed = _harness(client, monkeypatch, mode_update="acceptEdits")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await client._apply_claude_requested_mode({"sessionId": "s-1", "modes": _modes("default")})
    assert [m for m, _ in sent] == ["session/set_mode"]
    lines = [r for r in caplog.records if "requested permission mode" in r.message]
    assert [r.levelno for r in lines] == [logging.WARNING]
    assert "'acceptEdits' instead" in lines[0].message


@pytest.mark.asyncio
async def test_only_this_sessions_update_during_the_wait_counts(tmp_path, monkeypatch, caplog):
    """A mode update buffered before the request, or one for another session,
    does not decide where this session landed."""
    client = _client(tmp_path)
    client._mcp_notifications.append(
        JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "s-1",
                "update": {"sessionUpdate": "current_mode_update", "currentModeId": "acceptEdits"},
            },
        )
    )
    sent, _killed = _harness(
        client, monkeypatch, mode_update="acceptEdits", mode_update_session="s-other"
    )
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await client._apply_claude_requested_mode({"sessionId": "s-1", "modes": _modes("default")})
    assert [m for m, _ in sent] == ["session/set_mode"]
    lines = [r for r in caplog.records if "requested permission mode" in r.message]
    assert [r.levelno for r in lines] == [logging.INFO]


def test_both_session_establishment_paths_apply_the_request_after_the_pin():
    """session/load and session/new each call it right after the pin, with the
    mode the pin returned."""
    source = inspect.getsource(client_mod.AcpClient._initialize_session)
    lines = [line.strip() for line in source.splitlines()]
    pins = [
        i
        for i, line in enumerate(lines)
        if line.startswith("pinned = await self._pin_claude_starting_mode(")
    ]
    applies = [
        i
        for i, line in enumerate(lines)
        if line.startswith("await self._apply_claude_requested_mode(")
        and line.endswith(", pinned)")
    ]
    assert len(pins) == 2
    assert applies == [i + 1 for i in pins]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resp",
    [
        {"modes": _modes("bypassPermissions")},
        {"modes": _modes("auto")},
    ],
    ids=["reported-bypass", "reported-auto"],
)
async def test_a_session_the_pin_moved_to_default_is_moved_on_to_auto(tmp_path, monkeypatch, resp):
    """The pin's return value, not the stale response, is the mode the session is in."""
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch)
    await client._apply_claude_requested_mode({"sessionId": "s-1", **resp}, "default")
    assert sent == [("session/set_mode", {"sessionId": "s-1", "modeId": "auto"})]
    assert killed == []


@pytest.mark.asyncio
async def test_a_pinned_session_that_reported_no_modes_warns_and_keeps_default(
    tmp_path, monkeypatch, caplog
):
    """With no ``availableModes`` there is no ``auto`` known to be offered."""
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await client._apply_claude_requested_mode({"sessionId": "s-1"}, "default")
    assert sent == []
    assert killed == []
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "keeps 'default'" in warnings[0]


@pytest.mark.asyncio
async def test_a_pinned_value_that_is_not_a_mode_is_ignored(tmp_path, monkeypatch):
    """A stand-in pin (a test double) returns no ``str``; the response decides."""
    client = _client(tmp_path)
    sent, _killed = _harness(client, monkeypatch)
    await client._apply_claude_requested_mode(
        {"sessionId": "s-1", "modes": _modes("bypassPermissions")}, mock.MagicMock()
    )
    assert sent == []


def _adapter_transport(client, monkeypatch, *, started_mode=None):
    """Answer the handshake with the recorded 0.100.0 frames.

    A load is answered with the recorded ``session/new`` result under the loaded id:
    both report the session's mode the same way (the 0.84.0 capture in this directory
    shows ``session/load`` carrying ``modes.currentModeId``). ``started_mode``
    replaces the recorded ``modes.currentModeId``.
    """
    recorded = _recorded()
    sent: list[tuple[str, dict]] = []
    killed: list[bool] = []

    async def _send(method, params):
        sent.append((method, params))
        return len(sent)

    async def _wait(rid, **_kw):
        method, params = sent[rid - 1]
        if method == "initialize":
            return json.loads(json.dumps(recorded["initialize"]))
        if method in ("session/new", "session/load"):
            resp = json.loads(json.dumps(recorded["session/new"]))
            resp["sessionId"] = params.get("sessionId", "s-new")
            if started_mode is not None:
                resp["modes"]["currentModeId"] = started_mode
            return resp
        if method == "session/set_mode":
            return recorded["session/set_mode"]
        return {}

    async def _kill(*, force=False):
        killed.append(force)

    async def _work_dir():
        return str(client._work_dir)

    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    monkeypatch.setattr(client, "_kill_process", _kill)
    monkeypatch.setattr(client, "_session_work_dir", _work_dir)
    monkeypatch.setattr(client, "_drain_notifications", mock.AsyncMock())
    return sent, killed


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("requested", ["auto", None], ids=["opt-in", "no-opt-in"])
async def test_the_handshake_applies_a_requested_auto_on_both_paths(
    tmp_path, agents_dir, monkeypatch, path, requested
):
    """With the opt-in, exactly one ``set_mode`` ``auto`` follows the session's
    creation; without it, nothing is sent and the session stays in ``default``."""
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = _client(tmp_path, agent="kirocrew", permission_mode=requested)
    client._write_claude_local_settings()
    assert client._claude_settings_authored is True
    if path == "load":
        client._resume_session_id = "s-old"
    sent, killed = _adapter_transport(client, monkeypatch)

    await client._initialize_session()

    assert client._session_id == ("s-old" if path == "load" else "s-new")
    assert client._resumed is (path == "load")
    assert killed == []
    set_modes = [p for m, p in sent if m == "session/set_mode"]
    if requested:
        assert set_modes == [{"sessionId": client._session_id, "modeId": "auto"}]
    else:
        assert set_modes == []


#: (requested, pin setting on, starting mode) -> the mode the session is in at its
#: first prompt. A request for ``auto`` ends in ``auto`` with the pin on or off;
#: with no request the pin off keeps the starting mode, and the pin on moves an
#: approving one to ``default``. A requested ``auto`` that starts in
#: ``bypassPermissions`` with the pin off is the one start left as it is.
_ENDS_IN = {
    ("auto", False, "default"): "auto",
    ("auto", False, "auto"): "auto",
    ("auto", False, "bypassPermissions"): "bypassPermissions",
    ("auto", True, "default"): "auto",
    ("auto", True, "auto"): "auto",
    ("auto", True, "bypassPermissions"): "auto",
    (None, False, "default"): "default",
    (None, False, "auto"): "auto",
    (None, False, "bypassPermissions"): "bypassPermissions",
    (None, True, "default"): "default",
    (None, True, "auto"): "default",
    (None, True, "bypassPermissions"): "default",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize(("requested", "pin_on", "started"), sorted(_ENDS_IN, key=str))
async def test_the_pin_and_the_requested_mode_together_on_both_paths(
    tmp_path, agents_dir, monkeypatch, path, requested, pin_on, started
):
    """``KIROCREW_CC_PIN_INHERITED_MODE`` off and on, with and without a request."""
    if pin_on:
        monkeypatch.setenv(_PIN_SETTING_ENV, "1")
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = _client(tmp_path, agent="kirocrew", permission_mode=requested)
    client._write_claude_local_settings()
    if path == "load":
        client._resume_session_id = "s-old"
    sent, killed = _adapter_transport(client, monkeypatch, started_mode=started)

    await client._initialize_session()

    assert killed == []
    set_modes = [p["modeId"] for m, p in sent if m == "session/set_mode"]
    assert (set_modes[-1] if set_modes else started) == _ENDS_IN[(requested, pin_on, started)]
    # Each step sends at most once, and the pin always goes first.
    assert set_modes in ([], ["default"], ["auto"], ["default", "auto"])
