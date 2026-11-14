"""A claude session that asked for ``auto`` is put in ``auto`` before its first prompt.

The seed writes the request as ``permissions.defaultMode`` in the session's
``settings.local.json``, and the Claude Agent SDK drops it from that tier, so
``session/new`` reports ``default``. ``_apply_claude_requested_mode``
sends ``session/set_mode`` ``auto`` then, after the startup model, and only then: every
other session, mode or answer leaves the session as it started. From then on the
session's mode reports are followed, and one that lands it in a mode that approves on
its own is undone before the next prompt. The handshake tests replay a live
claude-agent-acp 0.100.0 capture, ``test/fixtures/claude_mode_pin/requested-auto-applied.jsonl``.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import types
from collections import deque
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
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

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


async def _apply(client, resp: dict, pinned=None) -> None:
    """The start's two steps: the mode recorded after the pin, then the request."""
    client._track_claude_starting_mode(resp, pinned)
    await client._apply_claude_requested_mode(resp)


def _harness(
    client,
    monkeypatch,
    *,
    fail=None,
    fail_send=None,
    fail_default=None,
    mode_update=None,
    mode_update_session=None,
):
    """Record what the client sends; ``fail`` raises from the answer to ``set_mode``
    ``auto``, ``fail_send`` from sending it, and ``fail_default`` from the answer to
    ``set_mode`` ``default``, and ``mode_update`` is a ``current_mode_update``
    buffered before it, as the adapter sends one ahead of the ``set_mode`` answer when
    it falls back. ``mode_update_session`` names another session for that update."""
    sent: list[tuple[str, dict]] = []
    killed: list[bool] = []

    async def _send(method, params):
        if fail_send is not None and params.get("modeId") == "auto":
            raise fail_send
        sent.append((method, params))
        return len(sent)

    async def _wait(rid, **_kw):
        mode = sent[rid - 1][1].get("modeId")
        if fail is not None and mode == "auto":
            raise fail
        if fail_default is not None and mode == "default":
            raise fail_default
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
        await _apply(client, {"sessionId": "s-1", "modes": _modes("default")})
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
    await _apply(client, {"sessionId": "s-1", **resp})
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
    await _apply(client, {"sessionId": "s-1", "modes": _modes("default")})
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
        await _apply(client, {"sessionId": "s-1", "modes": _modes("default", available)})
    assert sent == []
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "keeps 'default'" in warnings[0]


#: A refusal as ``_wait_for_response`` raises it: a JSON-RPC error frame, with its code.
_REFUSED = functools.partial(client_mod.AcpError, code=-32603)

#: Failures that are not an answer, as the transport raises them: none carries a code.
_DEAD_CHILD = {
    "exited": lambda: client_mod.AcpError("ACP process exited (code=1)"),
    "not-running": lambda: client_mod.AcpError("ACP process not running"),
    "shutdown": lambda: client_mod.AcpError("Shutdown in progress"),
    "pipe-broken": lambda: client_mod.AcpProcessDied("ACP process pipe broken: EPIPE"),
}


@pytest.mark.asyncio
async def test_a_refused_set_mode_warns_and_the_session_continues(tmp_path, monkeypatch, caplog):
    """An error answer means the request did not land: nothing moved, so nothing is
    undone, no raise, and the harness is not stopped."""
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch, fail=_REFUSED("JSON-RPC error: refused"))
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _apply(client, {"sessionId": "s-1", "modes": _modes("default")})
    assert [m for m, _ in sent] == ["session/set_mode"]
    assert killed == []
    assert client._claude_confirmed_mode == "default"
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "keeps 'default'" in warnings[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["send", "wait"])
@pytest.mark.parametrize("failure", sorted(_DEAD_CHILD))
async def test_a_dead_child_during_set_mode_auto_is_not_a_refusal(
    tmp_path, monkeypatch, caplog, where, failure
):
    """Only an error frame is the adapter refusing ``auto``. A child that exits, a
    broken pipe or a shutdown while the request is sent or awaited is no answer: it
    propagates to the start, whose caller retries or restarts the session."""
    client = _client(tmp_path)
    error = _DEAD_CHILD[failure]()
    kw = {"fail_send": error} if where == "send" else {"fail": error}
    _sent, killed = _harness(client, monkeypatch, **kw)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        with pytest.raises(type(error)) as raised:
            await _apply(client, {"sessionId": "s-1", "modes": _modes("default")})
    assert raised.value is error
    assert killed == []
    assert not [r for r in caplog.records if "keeps 'default'" in r.message]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "default_error",
    [None, client_mod.AcpError("refused"), client_mod.AcpTimeoutError("no answer")],
    ids=["moved-back", "refused", "timed-out"],
)
async def test_an_unanswered_set_mode_auto_is_undone_or_the_session_stopped(
    tmp_path, monkeypatch, caplog, default_error
):
    """No answer does not mean the request did not land: the adapter may still apply
    ``auto`` or its ``acceptEdits`` fallback after the wait gives up. The session is
    moved back to ``default``, and stopped when that move fails, as a failed pin is."""
    client = _client(tmp_path)
    sent, killed = _harness(
        client,
        monkeypatch,
        fail=client_mod.AcpTimeoutError("no answer"),
        fail_default=default_error,
    )
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        if default_error is None:
            await _apply(client, {"sessionId": "s-1", "modes": _modes("default")})
        else:
            with pytest.raises(client_mod.AcpError, match="the session was stopped"):
                await _apply(client, {"sessionId": "s-1", "modes": _modes("default")})
    assert [p["modeId"] for m, p in sent if m == "session/set_mode"] == ["auto", "default"]
    assert client._claude_confirmed_mode == "default"
    assert killed == ([] if default_error is None else [True])
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "moved back to 'default'" in warnings[0]


@pytest.mark.asyncio
async def test_the_adapters_accept_edits_fallback_is_named_and_undone(
    tmp_path, monkeypatch, caplog
):
    """On a model without Auto mode support the adapter answers ``{}`` but reports
    ``acceptEdits`` first (claude-agent-acp ``SessionModeManager.selectMode``); the
    warning names it, and the session is moved back to ``default``."""
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch, mode_update="acceptEdits")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _apply(client, {"sessionId": "s-1", "modes": _modes("default")})
    assert [p["modeId"] for m, p in sent if m == "session/set_mode"] == ["auto", "default"]
    assert killed == []
    assert client._claude_confirmed_mode == "default"
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
        await _apply(client, {"sessionId": "s-1", "modes": _modes("default")})
    assert [m for m, _ in sent] == ["session/set_mode"]
    lines = [r for r in caplog.records if "requested permission mode" in r.message]
    assert [r.levelno for r in lines] == [logging.INFO]


def test_both_session_establishment_paths_track_the_pin_and_apply_after_the_model():
    """session/load and session/new each record the mode the pin returned right after
    the pin, and the request goes on after the startup model: one call inside the
    requested-``auto`` branch's cancel guard and one, in its ``else``, without it."""
    source = inspect.getsource(client_mod.AcpClient._initialize_session)
    lines = [line.strip() for line in source.splitlines()]
    pins = [
        i
        for i, line in enumerate(lines)
        if line.startswith("pinned = await self._pin_claude_starting_mode(")
    ]
    tracks = [
        i
        for i, line in enumerate(lines)
        if line.startswith("self._track_claude_starting_mode(") and line.endswith(", pinned)")
    ]
    applies = [
        i
        for i, line in enumerate(lines)
        if line.startswith("await self._apply_claude_requested_mode(")
    ]
    model = lines.index("await self._apply_startup_model()")
    assert len(pins) == 2
    assert tracks == [i + 1 for i in pins]
    assert len(applies) == 2 and min(applies) > model


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
    await _apply(client, {"sessionId": "s-1", **resp}, "default")
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
        await _apply(client, {"sessionId": "s-1"}, "default")
    assert sent == []
    assert killed == []
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "keeps 'default'" in warnings[0]


@pytest.mark.asyncio
async def test_a_pinned_value_that_is_not_a_mode_is_ignored(tmp_path, monkeypatch):
    """A stand-in pin (a test double) returns no ``str``; the response decides."""
    client = _client(tmp_path)
    sent, _killed = _harness(client, monkeypatch)
    await _apply(
        client, {"sessionId": "s-1", "modes": _modes("bypassPermissions")}, mock.MagicMock()
    )
    assert sent == []


def _adapter_transport(
    client,
    monkeypatch,
    *,
    started_mode=None,
    available=None,
    set_mode_error=None,
    set_mode_reports=(),
    default_error=None,
):
    """Answer the handshake with the recorded 0.100.0 frames.

    A load is answered with the recorded ``session/new`` result under the loaded id:
    both report the session's mode the same way (the 0.84.0 capture in this directory
    shows ``session/load`` carrying ``modes.currentModeId``). ``started_mode``
    replaces the recorded ``modes.currentModeId`` and ``available`` the ids of its
    ``availableModes``. Each update in ``set_mode_reports`` is buffered for the
    session before the answer to ``set_mode`` ``auto``, as the adapter sends its mode
    reports, and ``set_mode_error`` is raised in place of that answer.
    ``default_error`` is raised in place of the answer to a ``set_mode`` ``default``
    sent after ``set_mode`` ``auto`` (the pin's own, before it, is answered).
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
            if available is not None:
                resp["modes"]["availableModes"] = [{"id": m} for m in available]
            return resp
        if method == "session/set_mode" and params.get("modeId") == "auto":
            for update in set_mode_reports:
                client._mcp_notifications.append(
                    JsonRpcMessage(
                        method="session/update",
                        params={"sessionId": params["sessionId"], "update": update},
                    )
                )
            if set_mode_error is not None:
                raise set_mode_error("set_mode not applied")
            return recorded["session/set_mode"]
        if method == "session/set_mode":
            after_auto = any(p.get("modeId") == "auto" for m, p in sent[: rid - 1])
            if default_error is not None and after_auto:
                raise default_error("set_mode default not applied")
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


def _sel_rows(monkeypatch) -> list[dict]:
    """The SEL ``tool_invocation`` rows this test's client writes."""
    rows: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            rows.append(kw)

    monkeypatch.setattr(client_mod, "sel", lambda: _Sel())
    return rows


async def _an_unasked_call_completes(client, kind: str = "read") -> None:
    """A call of ACP ``kind`` that sent no ``session/request_permission`` completes."""
    client._observed_tool_calls["tc1"] = ("Edit" if kind == "edit" else "Read", kind)
    await client._maybe_audit_unasked_tool_call(
        types.SimpleNamespace(tool_call_id="tc1", tool_status="completed")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("started", ["default", "auto"])
@pytest.mark.parametrize("foreign_file", [True, False], ids=["excluded", "crew-owned"])
async def test_an_excluded_requested_auto_session_keeps_the_pinned_mode(
    tmp_path, agents_dir, monkeypatch, path, started, foreign_file
):
    """A requested ``auto`` beside a foreign ``settings.local.json`` stays in ``default``.

    That session keeps Crew's tools, is pinned to ``default`` and is told so in the
    chat; nothing after the pin may move it back to ``auto``, and with no
    classifier approving its calls none gets an ``auto_approved`` row. The same
    request with a Crew-owned file still ends in ``auto`` and gets that row.
    """
    calls = _sel_rows(monkeypatch)
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    if foreign_file:
        local = tmp_path / ".claude" / "settings.local.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
    client = _client(tmp_path, agent="kirocrew", session_key="sk")
    client._write_claude_local_settings()
    assert client._claude_local_settings_excluded is foreign_file
    if path == "load":
        client._resume_session_id = "s-old"
    sent, killed = _adapter_transport(client, monkeypatch, started_mode=started)

    await client._initialize_session()

    assert killed == []
    set_modes = [p["modeId"] for m, p in sent if m == "session/set_mode"]
    ends_in = set_modes[-1] if set_modes else started
    await _an_unasked_call_completes(client)
    if foreign_file:
        assert "auto" not in set_modes
        assert ends_in == "default"
        assert "Default mode" in client.claude_mode_pinned_notice
        assert calls == []
    else:
        assert ends_in == "auto"
        assert client.claude_mode_pinned_notice == ""
        assert [c["outcome"] for c in calls] == ["auto_approved"]


def _mode_report(mode: str) -> dict:
    return {"sessionUpdate": "current_mode_update", "currentModeId": mode}


def _option_report(mode: str) -> dict:
    """The shape of the recorded ``config_option_update`` the adapter sends on a mode change."""
    return {
        "sessionUpdate": "config_option_update",
        "configOptions": [{"id": "mode", "category": "mode", "currentValue": mode}],
    }


_BY_CLASSIFIER = ("claude_classifier", "auto")

#: The row an unasked call gets in a session confirmed in ``default``: no approver.
_UNATTRIBUTED_DEFAULT = ("auto_approved", "claude_unattributed", "default")


def _attributions(rows: list[dict]) -> list[tuple]:
    """``(outcome, decided_by, permission_mode)`` of each SEL row."""
    return [
        (r["outcome"], r["metadata"]["decided_by"], r["metadata"]["permission_mode"]) for r in rows
    ]


def _unattributed(mode: str | None) -> tuple[str, str | None]:
    return ("claude_unattributed", mode)


#: case -> (pin setting on, starting mode, transport answers, attribution). The last is
#: the ``(decided_by, permission_mode)`` of the row a completed call that never asked
#: gets: Claude's classifier in a session confirmed in ``auto``, the mode itself in one
#: confirmed in a mode that approves on its own, and no approver in one whose unasked
#: calls the asking rules or Claude's own rules let run.
_AUDIT_CASES = {
    "applied": (False, "default", {}, _BY_CLASSIFIER),
    "applied-and-reported": (
        False,
        "default",
        {"set_mode_reports": [_option_report("auto")]},
        _BY_CLASSIFIER,
    ),
    "started-in-auto": (False, "auto", {}, _BY_CLASSIFIER),
    "pinned-then-applied": (True, "bypassPermissions", {}, _BY_CLASSIFIER),
    "refused": (False, "default", {"set_mode_error": _REFUSED}, _unattributed("default")),
    "timed-out": (
        False,
        "default",
        {"set_mode_error": client_mod.AcpTimeoutError},
        _unattributed("default"),
    ),
    "auto-not-offered": (
        False,
        "default",
        {"available": ("default", "acceptEdits")},
        _unattributed("default"),
    ),
    "fallback-mode-report": (
        False,
        "default",
        {"set_mode_reports": [_mode_report("acceptEdits")]},
        _unattributed("default"),
    ),
    "fallback-option-report": (
        False,
        "default",
        {"set_mode_reports": [_option_report("acceptEdits")]},
        _unattributed("default"),
    ),
    "started-in-bypass": (
        False,
        "bypassPermissions",
        {},
        ("claude_permission_mode", "bypassPermissions"),
    ),
    "started-in-accept-edits": (
        False,
        "acceptEdits",
        {},
        ("claude_unattributed", "acceptEdits"),
    ),
    "started-in-accept-edits-editing": (
        False,
        "acceptEdits",
        {"kind": "edit"},
        ("claude_permission_mode", "acceptEdits"),
    ),
    "pinned-in-accept-edits": (True, "acceptEdits", {}, ("claude_unattributed", "acceptEdits")),
    "pinned-in-accept-edits-editing": (
        True,
        "acceptEdits",
        {"kind": "edit"},
        ("claude_permission_mode", "acceptEdits"),
    ),
    "started-in-plan": (False, "plan", {}, _unattributed("plan")),
    "pinned-then-refused": (
        True,
        "bypassPermissions",
        {"set_mode_error": _REFUSED},
        _unattributed("default"),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("case", sorted(_AUDIT_CASES))
async def test_an_unasked_call_is_recorded_naming_the_mode_that_approved_it(
    tmp_path, agents_dir, monkeypatch, path, case
):
    """A requested ``auto`` session's unasked call is attributed to what approved it.

    Confirmed in ``auto``, Claude's classifier. Started in ``bypassPermissions`` and
    not moved, that mode, which approves every call. Started in ``acceptEdits``, that
    mode for an edit, which it approves; any other unasked call there (a read) gets a
    row that names no approver, since ``acceptEdits`` does not approve it. A
    refused ``set_mode``, an unanswered one or the ``acceptEdits`` fallback (both
    moved back to ``default``), an adapter that does not offer ``auto``, or a
    ``plan`` start leaves a mode whose unasked calls the asking rules or Claude's own
    rules let run: the row stays, as on main, and names no approver. With every move
    back answered, the session is never stopped here.
    """
    pin_on, started, answers, attribution = _AUDIT_CASES[case]
    answers = dict(answers)
    kind = answers.pop("kind", "read")
    if pin_on:
        monkeypatch.setenv(_PIN_SETTING_ENV, "1")
    rows = _sel_rows(monkeypatch)
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = _client(tmp_path, agent="kirocrew", session_key="sk")
    client._write_claude_local_settings()
    if path == "load":
        client._resume_session_id = "s-old"
    _sent, killed = _adapter_transport(client, monkeypatch, started_mode=started, **answers)

    await client._initialize_session()
    await _an_unasked_call_completes(client, kind)

    assert killed == []
    assert _attributions(rows) == [("auto_approved", *attribution)]


@pytest.mark.asyncio
async def test_a_later_start_that_skips_the_mode_read_back_is_not_audited_as_auto(
    tmp_path, agents_dir, monkeypatch
):
    """The ``auto`` an earlier session of this client confirmed does not carry over.

    The second start restores a session whose set-up fails after it is named, so it
    never reaches the pin and the read-back; nothing then says which mode it runs in,
    so its unasked call keeps its row but names no approver and no mode.
    """
    rows = _sel_rows(monkeypatch)
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = _client(tmp_path, agent="kirocrew", session_key="sk")
    client._write_claude_local_settings()
    sent, killed = _adapter_transport(client, monkeypatch)
    await client._initialize_session()
    await _an_unasked_call_completes(client)
    assert [r["outcome"] for r in rows] == ["auto_approved"]

    rows.clear()
    client._sel_settled_tool_calls.clear()
    client._resume_session_id = "s-old"
    store = client._store_session_config

    def _store(resp):
        if resp.get("sessionId") == "s-old":
            raise client_mod.AcpError("session config refused")
        store(resp)

    monkeypatch.setattr(client, "_store_session_config", _store)
    sent.clear()
    await client._initialize_session()
    await _an_unasked_call_completes(client)

    assert client._session_id == "s-old"
    assert [m for m, _ in sent if m == "session/set_mode"] == []
    assert killed == []
    assert _attributions(rows) == [("auto_approved", "claude_unattributed", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("report", [_mode_report, _option_report], ids=["mode", "option"])
@pytest.mark.parametrize(
    "default_error",
    [None, client_mod.AcpError, client_mod.AcpTimeoutError],
    ids=["moved-back", "refused", "timed-out"],
)
async def test_a_set_mode_auto_that_lands_elsewhere_is_moved_back_to_default(
    tmp_path, agents_dir, monkeypatch, path, report, default_error
):
    """Crew's own ``set_mode`` ``auto`` must not leave the session in its fallback.

    On a model without Auto mode support the adapter answers ``{}`` and reports
    ``acceptEdits``, where an edit runs without asking Crew's gate. Crew moved the
    session there, so it moves it back to ``default`` before the first prompt, and
    stops the session when that move fails, as the starting-mode pin does.
    """
    rows = _sel_rows(monkeypatch)
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = _client(tmp_path, agent="kirocrew", session_key="sk")
    client._write_claude_local_settings()
    if path == "load":
        client._resume_session_id = "s-old"
    sent, killed = _adapter_transport(
        client,
        monkeypatch,
        set_mode_reports=[report("acceptEdits")],
        default_error=default_error,
    )

    if default_error is None:
        await client._initialize_session()
        await _an_unasked_call_completes(client)
        assert killed == []
        assert _attributions(rows) == [_UNATTRIBUTED_DEFAULT]
    else:
        with pytest.raises(client_mod.AcpError, match="the session was stopped"):
            await client._initialize_session()
        assert killed == [True]
    assert [p["modeId"] for m, p in sent if m == "session/set_mode"] == ["auto", "default"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize(
    ("pin_on", "started"),
    [(False, "default"), (True, "bypassPermissions")],
    ids=["reported-default", "pinned-to-default"],
)
@pytest.mark.parametrize(
    "default_error",
    [None, client_mod.AcpError, client_mod.AcpTimeoutError],
    ids=["moved-back", "refused", "timed-out"],
)
async def test_a_set_mode_auto_that_times_out_is_moved_back_to_default(
    tmp_path, agents_dir, monkeypatch, path, pin_on, started, default_error
):
    """An unanswered ``set_mode`` ``auto`` may still be applied after the wait.

    So the session is moved back to ``default`` before the first prompt: it continues
    there, confirmed in ``default``, with a row for an unasked call that names no
    approver, or it is stopped when that move is refused or goes unanswered too.
    """
    if pin_on:
        monkeypatch.setenv(_PIN_SETTING_ENV, "1")
    rows = _sel_rows(monkeypatch)
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = _client(tmp_path, agent="kirocrew", session_key="sk")
    client._write_claude_local_settings()
    if path == "load":
        client._resume_session_id = "s-old"
    sent, killed = _adapter_transport(
        client,
        monkeypatch,
        started_mode=started,
        set_mode_error=client_mod.AcpTimeoutError,
        default_error=default_error,
    )

    if default_error is None:
        await client._initialize_session()
        await _an_unasked_call_completes(client)
        assert killed == []
        assert client._claude_confirmed_mode == "default"
        assert _attributions(rows) == [_UNATTRIBUTED_DEFAULT]
    else:
        with pytest.raises(client_mod.AcpError, match="the session was stopped"):
            await client._initialize_session()
        assert killed == [True]
    pinned = ["default"] if pin_on else []
    assert [p["modeId"] for m, p in sent if m == "session/set_mode"] == [
        *pinned,
        "auto",
        "default",
    ]


class _WireAdapter:
    """claude-agent-acp on the wire, keeping the session's mode as the adapter does.

    Crew's own ``_wait_for_response`` and ``_read_message`` read its frames off a fake
    stdout, so every frame takes the path a live adapter's takes. It answers with the
    recorded 0.100.0 frames and follows ``SessionModeManager``: ``set_mode`` ``auto``
    on a model without Auto mode support lands in ``acceptEdits``, and a switch to
    such a model moves an ``auto`` session there. Each change is reported as a
    ``current_mode_update``, before the answer or, with ``late``, after it; the start
    drain reads what is left. ``refuse_default`` refuses a ``set_mode`` ``default``
    sent once ``auto`` has been asked for, and ``refuse_models`` a switch to any model it names,
    with claude-agent-acp's value-refusal message. ``mode_at_switch`` records the
    session's mode as each model switch arrives. ``die_at`` (``model`` or ``auto``)
    names the request the child dies on: with ``death`` ``exits`` it exits without an
    answer, and with ``pipe-broken`` the write itself fails.
    """

    AUTO_MODEL = "model-with-auto"
    NO_AUTO_MODEL = "model-without-auto"

    def __init__(
        self,
        client,
        monkeypatch,
        *,
        started="default",
        late=False,
        refuse_default=False,
        refuse_models=(),
        die_at=None,
        death="exits",
    ):
        self.client = client
        self.recorded = _recorded()
        self.mode = started
        self.auto_model = True
        self.late = late
        self.refuse_default = refuse_default
        self.refuse_models = frozenset(refuse_models)
        self.die_at = die_at
        self.death = death
        self.mode_at_switch: list[str] = []
        self.sent: list[tuple[str, dict]] = []
        self.killed: list[bool] = []
        self.lines: deque[bytes] = deque()
        process = mock.MagicMock()
        process.returncode = None
        process.stdout.readline = self._readline
        client._process = process
        monkeypatch.setattr(client, "_send_request", self._send)
        monkeypatch.setattr(client, "_kill_process", self._kill)
        monkeypatch.setattr(client, "_session_work_dir", self._work_dir)
        monkeypatch.setattr(client, "_drain_notifications", self._drain)

    async def _readline(self) -> bytes:
        return self.lines.popleft() if self.lines else b""

    def _frame(self, frame: dict) -> None:
        self.lines.append((json.dumps(frame) + "\n").encode())

    def _move(self, mode: str, session_id: str, reports: list[dict]) -> None:
        if mode != self.mode:
            self.mode = mode
            reports.append(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"sessionId": session_id, "update": _mode_report(mode)},
                }
            )

    async def _send(self, method, params):
        self.sent.append((method, params))
        rid = len(self.sent)
        session_id = params.get("sessionId", "s-new")
        dies = (self.die_at == "model" and method == "session/set_config_option") or (
            self.die_at == "auto" and method == "session/set_mode" and params["modeId"] == "auto"
        )
        if dies and self.death == "pipe-broken":
            raise client_mod.AcpProcessDied("ACP process pipe broken: EPIPE")
        if dies:
            self.client._process.returncode = 1
            return rid
        reports: list[dict] = []
        reply: dict = {"jsonrpc": "2.0", "id": rid, "result": {}}
        if method == "initialize":
            reply["result"] = json.loads(json.dumps(self.recorded["initialize"]))
        elif method in ("session/new", "session/load"):
            result = json.loads(json.dumps(self.recorded["session/new"]))
            result["sessionId"] = session_id
            result["modes"]["currentModeId"] = self.mode
            reply["result"] = result
        elif method == "session/set_mode":
            asked_auto = any(p.get("modeId") == "auto" for _m, p in self.sent[:-1])
            if params["modeId"] == "default" and self.refuse_default and asked_auto:
                reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "no"}}
            else:
                landed = params["modeId"]
                if landed == "auto" and not self.auto_model:
                    landed = "acceptEdits"
                self._move(landed, session_id, reports)
        elif method == "session/set_config_option" and params["value"] in self.refuse_models:
            self.mode_at_switch.append(self.mode)
            message = f"Invalid value for config option model: {params['value']}"
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": message}}
        elif method == "session/set_config_option":
            self.mode_at_switch.append(self.mode)
            self.auto_model = params["value"] == self.AUTO_MODEL
            if self.mode == "auto" and not self.auto_model:
                self._move("acceptEdits", session_id, reports)
        for frame in ([] if self.late else reports) + [reply] + (reports if self.late else []):
            self._frame(frame)
        return rid

    async def switch_model(self, model: str) -> None:
        """The wire shape of a model switch on this adapter: the ``model`` config option."""
        rid = await self.client._send_request(
            "session/set_config_option",
            {"sessionId": self.client._session_id, "configId": "model", "value": model},
        )
        await self.client._wait_for_response(
            rid, timeout=FALLBACK_TIMEOUT_SECS, method="session/set_config_option"
        )

    async def _kill(self, *, force=False):
        self.killed.append(force)

    async def _work_dir(self):
        return str(self.client._work_dir)

    async def _drain(self, *_args, **_kw):
        """The start drain: every frame the adapter sent after its answers is read."""
        while self.lines:
            await self.client._read_message(timeout=FALLBACK_TIMEOUT_SECS)

    @property
    def set_modes(self) -> list[str]:
        return [p["modeId"] for m, p in self.sent if m == "session/set_mode"]


def _wired_client(tmp_path, agents_dir, monkeypatch, path, **wire_kw):
    """A requested-``auto`` claude client on the ``path`` start, on a ``_WireAdapter``."""
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = _client(tmp_path, agent="kirocrew", session_key="sk")
    client._write_claude_local_settings()
    if path == "load":
        client._resume_session_id = "s-old"
    return client, _WireAdapter(client, monkeypatch, **wire_kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize(
    ("model", "ends_in"),
    [(_WireAdapter.AUTO_MODEL, "auto"), (_WireAdapter.NO_AUTO_MODEL, "default")],
    ids=["model-with-auto", "model-without-auto"],
)
async def test_the_requested_auto_goes_on_after_the_startup_model(
    tmp_path, agents_dir, monkeypatch, path, model, ends_in
):
    """``auto`` is asked for on the model the session will run.

    Applied before the startup model selection, a switch to a model without Auto
    mode support would move it to ``acceptEdits``, where an edit asks nothing. After
    it, such a model answers the request with that fallback, which is undone.
    """
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, path)
    monkeypatch.setattr(client, "_apply_startup_model", lambda: wire.switch_model(model))

    await client._initialize_session()

    order = [(m, p.get("modeId") or p.get("value")) for m, p in wire.sent]
    assert order.index(("session/set_config_option", model)) < order.index(
        ("session/set_mode", "auto")
    )
    assert wire.mode == ends_in
    assert client._claude_confirmed_mode == ends_in
    assert wire.killed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("refuse_default", [False, True], ids=["moved-back", "move-refused"])
async def test_a_fallback_reported_after_the_set_mode_answer_is_undone_before_the_first_prompt(
    tmp_path, agents_dir, monkeypatch, path, refuse_default
):
    """The ``acceptEdits`` report can come after the ``{}`` answer to ``set_mode``
    ``auto``: the start drain reads it, and the session is moved back to ``default``
    before the start returns, or stopped when that move is refused."""
    client, wire = _wired_client(
        tmp_path, agents_dir, monkeypatch, path, late=True, refuse_default=refuse_default
    )

    async def _startup_model():
        await wire.switch_model(_WireAdapter.NO_AUTO_MODEL)

    monkeypatch.setattr(client, "_apply_startup_model", _startup_model)

    if refuse_default:
        with pytest.raises(client_mod.AcpError, match="the session was stopped"):
            await client._initialize_session()
        assert wire.killed == [True]
    else:
        await client._initialize_session()
        assert wire.mode == "default"
        assert client._claude_confirmed_mode == "default"
        assert wire.killed == []
    assert wire.set_modes == ["auto", "default"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("death", ["exits", "pipe-broken"])
@pytest.mark.parametrize(
    "step", ["model", "auto"], ids=["in-the-startup-model", "in-set-mode-auto"]
)
async def test_a_child_that_dies_during_the_start_ends_it_in_either_step(
    tmp_path, agents_dir, monkeypatch, path, death, step
):
    """A child that dies while ``set_mode`` ``auto`` is sent or awaited ends the start
    exactly as one that dies during the startup model does: the error leaves
    ``_initialize_session``, so ``ensure_ready``'s retry or the caller's restart takes
    it, instead of a refused mode that lets a dead session look started."""
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, path, die_at=step, death=death)
    monkeypatch.setattr(
        client, "_apply_startup_model", lambda: wire.switch_model(_WireAdapter.AUTO_MODEL)
    )

    with pytest.raises(client_mod.AcpError, match=r"ACP process (exited|pipe broken)"):
        await client._initialize_session()

    assert wire.set_modes == ([] if step == "model" else ["auto"])
    assert wire.killed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
async def test_a_cancel_before_the_move_back_leaves_it_due_for_the_next_prompt(
    tmp_path, agents_dir, monkeypatch, path
):
    """A prompt cancelled while ``ensure_ready`` sends the move back to ``default`` has
    moved nothing, so the session stays marked and the next prompt moves it."""
    client, wire = await _running_in_auto(tmp_path, agents_dir, monkeypatch, path)
    await wire.switch_model(_WireAdapter.NO_AUTO_MODEL)
    assert wire.mode == "acceptEdits" and client._claude_mode_restore_due is True
    cancelled: list[str] = []

    async def _cancel_once(method, params):
        if params.get("modeId") == "default" and not cancelled:
            cancelled.append(method)
            raise asyncio.CancelledError
        return await wire._send(method, params)

    monkeypatch.setattr(client, "_send_request", _cancel_once)

    with pytest.raises(asyncio.CancelledError):
        await client.ensure_ready()
    assert wire.mode == "acceptEdits"
    assert client._claude_mode_restore_due is True

    await client.ensure_ready()

    assert wire.mode == "default"
    assert client._claude_confirmed_mode == "default"
    assert client._claude_mode_restore_due is False
    assert wire.set_modes == ["auto", "default"]
    assert wire.killed == []


class _FreshStart(Exception):
    """Raised by a patched ``_spawn``: ``ensure_ready`` started a new child."""


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize(
    "model", [_WireAdapter.AUTO_MODEL, _WireAdapter.NO_AUTO_MODEL], ids=["with-auto", "no-auto"]
)
@pytest.mark.parametrize("point", ["after-the-write", "in-the-wait", "in-the-drain"])
async def test_a_cancel_before_the_requested_auto_is_settled_ends_the_session(
    tmp_path, agents_dir, monkeypatch, path, model, point
):
    """A cancel that lands once ``set_mode`` ``auto`` is on the wire and before its
    mode reports are read leaves the adapter in a mode Crew has not confirmed:
    ``acceptEdits`` on a model without Auto mode support. ``ensure_ready``'s handler
    does not see a cancel, so the start ends the session itself, and the next
    ``ensure_ready`` starts a new child instead of taking the warm return into it."""
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, path, late=True)
    monkeypatch.setattr(client, "_apply_startup_model", lambda: wire.switch_model(model))
    auto_sent: list[int] = []
    send, read = client._send_request, client._read_message

    async def _send(method, params):
        rid = await send(method, params)
        if method == "session/set_mode" and params.get("modeId") == "auto":
            auto_sent.append(rid)
            if point == "after-the-write":
                raise asyncio.CancelledError
        return rid

    async def _read(*args, **kwargs):
        if auto_sent and point == "in-the-wait":
            raise asyncio.CancelledError
        return await read(*args, **kwargs)

    async def _cancelled_drain(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_read_message", _read)
    if point == "in-the-drain":
        monkeypatch.setattr(client, "_drain_notifications", _cancelled_drain)

    with pytest.raises(asyncio.CancelledError):
        await client.ensure_ready()

    assert auto_sent
    assert wire.mode == ("auto" if model == _WireAdapter.AUTO_MODEL else "acceptEdits")
    assert wire.killed == [True]
    assert client._session_id is None

    async def _spawn():
        raise _FreshStart

    monkeypatch.setattr(client, "_spawn", _spawn)
    with pytest.raises(_FreshStart):
        await client.ensure_ready()


def _record_cancel_guard_entries(monkeypatch) -> list[object]:
    """Record each client that enters the start's cancel guard; the guard still runs."""
    from kiro_crew.acp import client_start as client_start_mod

    entered: list[object] = []
    real = client_start_mod._ended_on_cancel

    def _guard(client, *args, **kwargs):
        entered.append(client)
        return real(client, *args, **kwargs)

    monkeypatch.setattr(client_start_mod, "_ended_on_cancel", _guard)
    return entered


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("requested", ["auto", None], ids=["requested-auto", "no-request"])
async def test_only_a_requested_auto_start_enters_the_cancel_guard(
    tmp_path, agents_dir, monkeypatch, path, requested
):
    """The guard is entered around the request and the drain of a requested-``auto``
    claude start only; a claude start that asked for nothing awaits both steps
    directly, as every other harness does (harness-parity H13)."""
    entered = _record_cancel_guard_entries(monkeypatch)
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = _client(tmp_path, permission_mode=requested, agent="kirocrew", session_key="sk")
    client._write_claude_local_settings()
    if path == "load":
        client._resume_session_id = "s-old"
    wire = _WireAdapter(client, monkeypatch)
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())

    await client._initialize_session()

    assert entered == ([client, client] if requested == "auto" else [])
    assert wire.mode == ("auto" if requested == "auto" else "default")
    assert wire.killed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("late", [False, True], ids=["read-in-the-switch", "read-in-a-turn"])
@pytest.mark.parametrize("refuse_default", [False, True], ids=["moved-back", "move-refused"])
async def test_a_live_model_switch_into_accept_edits_is_undone_before_the_next_prompt(
    tmp_path, agents_dir, monkeypatch, path, late, refuse_default
):
    """A session running in ``auto`` is switched to a model without Auto mode support
    by a write that does not pass through ``set_model``.

    claude-agent-acp moves it to ``acceptEdits``. Whether that report is read while
    the switch is answered or later, inside a turn, the next prompt's ``ensure_ready``
    moves the session back to ``default`` first, or stops it when that move is
    refused, and an unasked edit afterwards gets a row that names no approver.
    """
    rows = _sel_rows(monkeypatch)
    client, wire = _wired_client(
        tmp_path, agents_dir, monkeypatch, path, late=late, refuse_default=refuse_default
    )
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())
    await client._initialize_session()
    assert wire.mode == "auto" and client._claude_confirmed_mode == "auto"

    await wire.switch_model(_WireAdapter.NO_AUTO_MODEL)
    if late:
        # The turn after the switch reads the report off the wire.
        await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)
    assert wire.mode == "acceptEdits"

    if refuse_default:
        with pytest.raises(client_mod.AcpError, match="the session was stopped"):
            await client.ensure_ready()
        assert wire.killed == [True]
    else:
        await client.ensure_ready()
        assert wire.mode == "default"
        assert client._claude_confirmed_mode == "default"
        assert wire.killed == []
        await _an_unasked_call_completes(client, "edit")
        assert _attributions(rows) == [_UNATTRIBUTED_DEFAULT]
    assert wire.set_modes == ["auto", "default"]


def _report(session: str, update: dict) -> JsonRpcMessage:
    return JsonRpcMessage(method="session/update", params={"sessionId": session, "update": update})


@pytest.mark.parametrize(
    ("confirmed", "update", "session", "ends_in", "due"),
    [
        ("auto", _mode_report("acceptEdits"), "s-1", "acceptEdits", True),
        ("auto", _option_report("acceptEdits"), "s-1", "acceptEdits", True),
        ("auto", _mode_report("bypassPermissions"), "s-1", "bypassPermissions", True),
        ("acceptEdits", _mode_report("acceptEdits"), "s-1", "acceptEdits", False),
        ("auto", _mode_report("default"), "s-1", "default", False),
        ("default", _option_report("auto"), "s-1", "auto", False),
        ("auto", _mode_report("plan"), "s-1", "plan", False),
        ("auto", _mode_report("acceptEdits"), "s-other", "auto", False),
        ("auto", {"sessionUpdate": "usage_update", "used": 1, "size": 2}, "s-1", "auto", False),
    ],
    ids=[
        "auto-to-accept-edits",
        "auto-to-accept-edits-option",
        "auto-to-bypass",
        "the-start-restated",
        "to-default",
        "to-auto",
        "to-plan",
        "another-session",
        "no-mode",
    ],
)
def test_a_mode_report_moves_the_confirmed_mode_and_marks_an_approving_one(
    tmp_path, confirmed, update, session, ends_in, due
):
    """Only a change of this session's mode counts, and only a change to a mode that
    approves on its own marks the session for a move back to ``default``."""
    client = _client(tmp_path)
    client._session_id = "s-1"
    client._track_claude_starting_mode({"modes": _modes(confirmed)})
    client._note_claude_mode_report(_report(session, update))
    assert client._claude_confirmed_mode == ends_in
    assert client._claude_mode_restore_due is due


def test_a_later_report_of_an_asking_mode_clears_the_mark(tmp_path):
    client = _client(tmp_path)
    client._session_id = "s-1"
    client._track_claude_starting_mode({"modes": _modes("auto")})
    client._note_claude_mode_report(_report("s-1", _mode_report("acceptEdits")))
    client._note_claude_mode_report(_report("s-1", _mode_report("auto")))
    assert client._claude_confirmed_mode == "auto"
    assert client._claude_mode_restore_due is False


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["auto", None], ids=["requested-auto", "no-request"])
async def test_the_wire_reader_follows_mode_reports_only_for_a_requested_auto_session(
    tmp_path, requested
):
    """``_read_message`` hands each frame off the wire to the follower, and only while
    a session that requested ``auto`` is watched; any other session reads one flag."""
    client = _client(tmp_path, permission_mode=requested)
    client._session_id = "s-1"
    client._track_claude_starting_mode({"modes": _modes("auto")})
    frame = {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s-1"}}
    frame["params"]["update"] = _mode_report("acceptEdits")
    process = mock.MagicMock()
    process.returncode = None
    process.stdout.readline = mock.AsyncMock(return_value=(json.dumps(frame) + "\n").encode())
    client._process = process

    msg = await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)

    assert msg is not None and msg.method == "session/update"
    assert client._claude_mode_watched is (requested == "auto")
    assert client._claude_confirmed_mode == ("acceptEdits" if requested else "auto")
    assert client._claude_mode_restore_due is (requested == "auto")


@pytest.mark.asyncio
@pytest.mark.parametrize("due", [True, False])
async def test_the_move_back_is_sent_only_when_due(tmp_path, monkeypatch, due):
    client = _client(tmp_path)
    client._session_id = "s-1"
    client._claude_confirmed_mode = "acceptEdits"
    client._claude_mode_restore_due = due
    sent, killed = _harness(client, monkeypatch)

    await client._restore_claude_asking_mode_if_due()

    assert [p["modeId"] for m, p in sent if m == "session/set_mode"] == (["default"] if due else [])
    assert client._claude_confirmed_mode == ("default" if due else "acceptEdits")
    assert client._claude_mode_restore_due is False
    assert killed == []


async def _running_in_auto(tmp_path, agents_dir, monkeypatch, path, **wire_kw):
    """A requested-``auto`` session past its start, confirmed in ``auto``."""
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, path, **wire_kw)
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())
    await client._initialize_session()
    assert wire.mode == "auto" and client._claude_confirmed_mode == "auto"
    return client, wire


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("late", [False, True], ids=["reports-first", "reports-late"])
@pytest.mark.parametrize(
    "model", [_WireAdapter.AUTO_MODEL, _WireAdapter.NO_AUTO_MODEL], ids=["with-auto", "no-auto"]
)
async def test_a_live_model_switch_leaves_the_session_in_default(
    tmp_path, agents_dir, monkeypatch, caplog, path, late, model
):
    """A requested ``auto`` is applied at session start only.

    Crew moves the session to ``default`` before a live switch and does not ask for
    ``auto`` again, so no mode report can follow the switch: the adapter only moves
    an ``auto`` session. What the idle reader then reads off the wire, before a turn
    the agent starts on its own (which skips ``ensure_ready``), leaves the session in
    ``default``, and an unasked edit there gets a row that names no approver. One
    warning says so, for the session, however many switches follow.
    """
    rows = _sel_rows(monkeypatch)
    client, wire = await _running_in_auto(tmp_path, agents_dir, monkeypatch, path, late=late)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await client.set_model(model)
        while wire.lines:
            await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)

        assert wire.mode == "default"
        assert client._claude_confirmed_mode == "default"
        assert client._claude_mode_restore_due is False
        await _an_unasked_call_completes(client, "edit")
        assert _attributions(rows) == [_UNATTRIBUTED_DEFAULT]

        await client.set_model(_WireAdapter.AUTO_MODEL)

    assert wire.mode_at_switch == ["default", "default"]
    assert wire.set_modes == ["auto", "default"]
    assert wire.mode == "default"
    assert wire.killed == []
    warnings = [r.message for r in caplog.records if "not applied again" in r.message]
    assert len(warnings) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
async def test_a_refused_move_to_default_before_a_live_switch_stops_the_session(
    tmp_path, agents_dir, monkeypatch, path
):
    """The switch is never sent to a session Crew could not take out of ``auto``."""
    client, wire = await _running_in_auto(
        tmp_path, agents_dir, monkeypatch, path, refuse_default=True
    )

    with pytest.raises(client_mod.AcpError, match="the session was stopped"):
        await client.set_model(_WireAdapter.NO_AUTO_MODEL)

    assert wire.killed == [True]
    assert [m for m, _p in wire.sent if m == "session/set_config_option"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
async def test_a_refused_model_leaves_the_session_in_default(
    tmp_path, agents_dir, monkeypatch, path
):
    """A model the adapter refuses still ends the session's ``auto``: the refusal
    reaches the caller as on main, and the session keeps asking."""
    from kiro_crew.acp.transport_errors import AcpModelUnavailable

    rows = _sel_rows(monkeypatch)
    client, wire = await _running_in_auto(
        tmp_path,
        agents_dir,
        monkeypatch,
        path,
        refuse_models=[_WireAdapter.NO_AUTO_MODEL],
    )

    with pytest.raises(AcpModelUnavailable):
        await client.set_model(_WireAdapter.NO_AUTO_MODEL)

    assert wire.set_modes == ["auto", "default"]
    assert wire.mode == "default" and client._claude_confirmed_mode == "default"
    assert wire.killed == []
    await _an_unasked_call_completes(client, "edit")
    assert _attributions(rows) == [_UNATTRIBUTED_DEFAULT]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confirmed", "moved"),
    [
        ("auto", True),
        ("acceptEdits", True),
        ("bypassPermissions", True),
        ("default", False),
        ("plan", False),
        (None, False),
    ],
)
async def test_only_a_mode_that_approves_on_its_own_is_left_before_a_switch(
    tmp_path, monkeypatch, confirmed, moved
):
    client = _client(tmp_path)
    client._session_id = "s-1"
    client._claude_confirmed_mode = confirmed
    sent, killed = _harness(client, monkeypatch)

    await client._leave_claude_approving_mode_for_model_switch()

    assert [p["modeId"] for m, p in sent if m == "session/set_mode"] == (
        ["default"] if moved else []
    )
    assert client._claude_confirmed_mode == ("default" if moved else confirmed)
    assert killed == []
