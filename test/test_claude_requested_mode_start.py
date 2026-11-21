"""The start of a claude session that requested ``auto``: the ceiling, the drain, the ids.

The enterprise ``approval_modes`` ceiling decides whether a requested ``auto`` is
applied at all. A deny installed while a live session is in an approving mode stops the
captured process and then records its unsettled unasked calls; the next start and the
pre-prompt check read the ceiling again. In ``auto`` Claude's classifier approves a call
without asking anyone, which is what a session Trust grants, so the ceiling is asked
for ``trust``. Such a start also keeps the start drain's idle window on a harness that
otherwise skips it (``ACP_BACKENDS_NO_MCP_INIT_FRAMES``), so a mode report that follows
an answer its start read is undone before the first prompt. The wire cases run on the
``_WireAdapter`` of ``test/test_claude_requested_mode.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gc
import inspect
import json
import logging
import threading
import types
import weakref
from unittest import mock

import pytest
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _no_ambient_pin_setting  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _write_spec
from test_claude_requested_mode import (
    _LOGGER,
    _UNATTRIBUTED_DEFAULT,
    _an_unasked_call_completes,
    _apply,
    _attributions,
    _client,
    _harness,
    _mode_report,
    _modes,
    _option_report,
    _WireAdapter,
    _wired_client,
)

from kiro_crew import approval_mode_policy as amp
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import client_claude_session as session_mod
from kiro_crew.acp import client_transport as transport_mod
from kiro_crew.acp import harness_notices
from kiro_crew.acp.client import AcpClient, ClaudeAcpClient
from kiro_crew.acp.client_timeouts import _DRAIN_IDLE_EXIT
from kiro_crew.testing.ids import unallocatable_pids
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

#: The record ``_deny_approval_mode`` writes for a policy-refused approval mode.
_DENIED = "approval_mode_denied_by_policy"


class _Ceiling:
    """The ``approval_modes`` verdict for ``trust``, settable mid-test."""

    def __init__(self, monkeypatch, *, permits_trust: bool) -> None:
        self.permits_trust = permits_trust
        monkeypatch.setattr(amp, "session_mode_permitted", self._permitted)

    def _permitted(self, mode: str) -> bool:
        return mode != "trust" or self.permits_trust


def _sel(monkeypatch) -> tuple[list[dict], list[dict]]:
    """The SEL ``tool_invocation`` rows and ``mode_change`` records the client writes."""
    rows: list[dict] = []
    mode_changes: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            rows.append(kw)

        def log_api_access(self, **kw):
            mode_changes.append(kw)

    monkeypatch.setattr(client_mod, "sel", lambda: _Sel())
    return rows, mode_changes


def _policy_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "approval_modes policy" in r.getMessage()
    ]


def _outcomes(mode_changes: list[dict]) -> list[str]:
    assert {m["operation"] for m in mode_changes} <= {"mode_change:claude_auto"}
    return [m["outcome"] for m in mode_changes]


@pytest.fixture
def _live_requested_auto_registry(monkeypatch):
    """Give each policy-hook test its own weak live-client registry."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    registry = weakref.WeakKeyDictionary()
    monkeypatch.setattr(mode_mod, "_live_requested_auto_clients", registry)
    return registry


def _watch_live_client(client, confirmed: str):
    process = mock.MagicMock()
    process.returncode = None
    client._process = process
    client._session_id = "s-1"
    client._track_claude_starting_mode({"modes": _modes(confirmed)})
    return process


async def _run_claude_trust_deny() -> None:
    """Run Claude's registered hook on the ceiling-installing thread."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    hook = amp._deny_hooks.get(mode_mod._CLAUDE_POLICY_HOOK_OWNER)
    assert hook is mode_mod._on_claude_trust_denied
    await asyncio.to_thread(hook, frozenset({"trust"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", ["auto", "acceptEdits", "bypassPermissions"])
async def test_a_trust_deny_stops_an_idle_approving_session_and_next_start_keeps_default(
    tmp_path, monkeypatch, caplog, confirmed, _live_requested_auto_registry
):
    """The hook sends no mode request; a fresh start reads the deny and stays asking."""
    rows, mode_changes = _sel(monkeypatch)
    _Ceiling(monkeypatch, permits_trust=False)
    client = _client(tmp_path)
    process = _watch_live_client(client, confirmed)
    sent, killed = _harness(client, monkeypatch)

    await _run_claude_trust_deny()
    await async_wait_until(
        lambda: killed or sent,
        describe=lambda: (killed, sent),
    )
    assert sent == []
    await asyncio.wait_for(client._claude_policy_stop_task, FALLBACK_TIMEOUT_SECS)

    assert killed == [True]
    assert process.kill.call_count == 0
    assert client._claude_confirmed_mode == confirmed
    assert _outcomes(mode_changes) == [_DENIED]
    assert rows == []

    process.returncode = 0
    client._process = mock.MagicMock(returncode=None)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _apply(client, {"sessionId": "s-2", "modes": _modes("default")})

    assert sent == []
    assert client._claude_confirmed_mode == "default"
    assert len(_policy_warnings(caplog)) == 1
    assert _outcomes(mode_changes) == [_DENIED, _DENIED]


@pytest.mark.asyncio
async def test_a_trust_deny_stops_then_records_each_unsettled_unasked_call(
    tmp_path, monkeypatch, _live_requested_auto_registry
):
    """Every unasked current-turn call is settled before the stop and gets one
    interrupted row after it; a call that asked gets none."""
    rows, mode_changes = _sel(monkeypatch)
    _Ceiling(monkeypatch, permits_trust=False)
    client = _client(tmp_path)
    _watch_live_client(client, "auto")
    sent, killed = _harness(client, monkeypatch)
    client._observed_tool_calls.update(
        {
            "tc-read": ("Read", "read"),
            "tc-asked": ("Bash", "execute"),
            "tc-edit": ("Edit", "edit"),
        }
    )
    client._sel_settled_tool_calls.add("tc-asked")

    async def _kill_before_rows(*, force=False):
        assert rows == []
        assert client._sel_settled_tool_calls == {"tc-read", "tc-asked", "tc-edit"}
        killed.append(force)

    monkeypatch.setattr(client, "_kill_process", _kill_before_rows)
    await _run_claude_trust_deny()
    await async_wait_until(
        lambda: killed or sent,
        describe=lambda: (killed, sent),
    )
    assert sent == []
    await asyncio.wait_for(client._claude_policy_stop_task, FALLBACK_TIMEOUT_SECS)

    assert killed == [True]
    assert client._sel_settled_tool_calls == {"tc-read", "tc-asked", "tc-edit"}
    assert {row["request_id"] for row in rows} == {"tc-read", "tc-edit"}
    assert {row["outcome"] for row in rows} == {"interrupted_by_policy"}
    assert {row["metadata"]["tool_status"] for row in rows} == {"interrupted_by_policy"}
    assert {row["metadata"]["decided_by"] for row in rows} == {"claude_unattributed"}
    assert {row["metadata"]["reason"] for row in rows} == {"claude_unasked_call"}
    assert _outcomes(mode_changes) == [_DENIED]


@pytest.mark.asyncio
async def test_a_trust_deny_stops_the_process_before_any_sel_write_is_awaited(
    tmp_path, monkeypatch, _live_requested_auto_registry
):
    """An observed turn does not run on while SEL is written: a writer that
    blocks until released finds the process already stopped, and its rows carry what the
    session held before the stop even though the stop dropped that state."""
    _rows, mode_changes = _sel(monkeypatch)
    _Ceiling(monkeypatch, permits_trust=False)
    client = _client(tmp_path)
    _watch_live_client(client, "auto")
    sent, killed = _harness(client, monkeypatch)
    client._observed_tool_calls.update({"tc-read": ("Read", "read"), "tc-edit": ("Edit", "edit")})
    released = threading.Event()
    stopped_when_written: list[bool] = []
    written: list[dict] = []

    class _BlockingSel:
        def log_tool_invocation(self, **kw):
            stopped_when_written.append(bool(killed))
            released.wait(FALLBACK_TIMEOUT_SECS)
            written.append(kw)

        def log_api_access(self, **kw):
            mode_changes.append(kw)

    async def _kill_dropping_state(*, force=False):
        killed.append(force)
        client._observed_tool_calls.clear()
        client._claude_confirmed_mode = None

    monkeypatch.setattr(client_mod, "sel", lambda: _BlockingSel())
    monkeypatch.setattr(client, "_kill_process", _kill_dropping_state)
    try:
        await _run_claude_trust_deny()
        await async_wait_until(
            lambda: len(stopped_when_written) == 2,
            describe=lambda: (stopped_when_written, killed),
        )
        assert stopped_when_written == [True, True]
        assert killed == [True]
        assert not client._claude_policy_stop_task.done()
    finally:
        released.set()
    await asyncio.wait_for(client._claude_policy_stop_task, FALLBACK_TIMEOUT_SECS)

    assert sent == []
    assert sorted((row["request_id"], row["tool_name"]) for row in written) == [
        ("tc-edit", "Edit"),
        ("tc-read", "Read"),
    ]
    assert {row["metadata"]["decided_by"] for row in written} == {"claude_unattributed"}
    assert {row["metadata"]["reason"] for row in written} == {"claude_unasked_call"}
    assert {row["metadata"]["permission_mode"] for row in written} == {"auto"}
    assert _outcomes(mode_changes) == [_DENIED]


@pytest.mark.asyncio
async def test_a_trust_deny_during_startup_stops_without_a_second_stdout_reader(
    tmp_path, monkeypatch, _live_requested_auto_registry
):
    """After the mode pin, a startup reader and the policy stop never read together."""
    _rows, mode_changes = _sel(monkeypatch)
    _Ceiling(monkeypatch, permits_trust=False)
    client = _client(tmp_path)
    process = _watch_live_client(client, "auto")
    stream = asyncio.StreamReader()
    process.stdout = stream
    sent: list[tuple[str, dict]] = []
    killed: list[bool] = []

    async def _send(method, params):
        sent.append((method, params))
        return len(sent)

    async def _kill(*, force=False):
        killed.append(force)

    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_kill_process", _kill)
    startup_read = asyncio.create_task(
        client._read_message(timeout=FALLBACK_TIMEOUT_SECS, check_cancel=False)
    )
    await async_wait_until(
        lambda: getattr(stream, "_waiter", None) is not None,
        describe=lambda: getattr(stream, "_waiter", None),
    )
    try:
        await _run_claude_trust_deny()
        await async_wait_until(
            lambda: killed or sent,
            describe=lambda: (killed, sent),
        )
        assert sent == []
        await asyncio.wait_for(client._claude_policy_stop_task, FALLBACK_TIMEOUT_SECS)
    finally:
        stream.feed_eof()
    assert await asyncio.wait_for(startup_read, FALLBACK_TIMEOUT_SECS) is None

    assert sent == []
    assert killed == [True]
    assert _outcomes(mode_changes) == [_DENIED]


@pytest.mark.asyncio
async def test_a_trust_deny_stops_only_the_requested_session_in_an_approving_mode(
    tmp_path, monkeypatch, _live_requested_auto_registry
):
    """A non-requesting session and a requested session in ``default`` stay live."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    _rows, mode_changes = _sel(monkeypatch)
    not_requesting = _client(tmp_path / "not-requesting", permission_mode=None)
    asking = _client(tmp_path / "asking")
    approving = _client(tmp_path / "approving")
    _watch_live_client(not_requesting, "auto")
    _watch_live_client(asking, "default")
    _watch_live_client(approving, "auto")
    not_requesting_sent, not_requesting_killed = _harness(not_requesting, monkeypatch)
    asking_sent, asking_killed = _harness(asking, monkeypatch)
    approving_sent, approving_killed = _harness(approving, monkeypatch)
    hook = amp._deny_hooks.get(mode_mod._CLAUDE_POLICY_HOOK_OWNER)
    assert hook is mode_mod._on_claude_trust_denied
    assert not_requesting not in _live_requested_auto_registry
    assert asking in _live_requested_auto_registry
    assert approving in _live_requested_auto_registry

    await asyncio.to_thread(hook, frozenset({"trust"}))
    await async_wait_until(
        lambda: approving_killed or approving_sent,
        describe=lambda: (approving_killed, approving_sent),
    )
    assert approving_sent == []
    await asyncio.wait_for(approving._claude_policy_stop_task, FALLBACK_TIMEOUT_SECS)

    assert not_requesting._claude_confirmed_mode == "auto"
    assert asking._claude_confirmed_mode == "default"
    assert not_requesting_sent == [] and not_requesting_killed == []
    assert asking_sent == [] and asking_killed == []
    assert approving_sent == [] and approving_killed == [True]
    assert _outcomes(mode_changes) == [_DENIED]


def test_a_trust_deny_with_a_closed_loop_kills_only_the_captured_process(
    tmp_path, monkeypatch, _live_requested_auto_registry
):
    """A loop-closing race cannot redirect the direct kill to a replacement process."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    _rows, mode_changes = _sel(monkeypatch)
    client = _client(tmp_path)
    captured = mock.MagicMock(returncode=None)
    replacement = mock.MagicMock(returncode=None)
    client._process = captured
    client._session_id = "s-1"
    client._claude_confirmed_mode = "auto"
    client._claude_mode_watched = True

    class _ClosingLoop:
        @staticmethod
        def is_closed() -> bool:
            client._process = replacement
            return True

        @staticmethod
        def is_running() -> bool:
            raise AssertionError("a closed loop has no running-state check")

    _live_requested_auto_registry[client] = _ClosingLoop()
    hook = amp._deny_hooks.get(mode_mod._CLAUDE_POLICY_HOOK_OWNER)
    assert hook is mode_mod._on_claude_trust_denied

    hook(frozenset({"trust"}))

    captured.kill.assert_called_once_with()
    replacement.kill.assert_not_called()
    assert _outcomes(mode_changes) == [_DENIED]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replaced",
    ["before-the-callback-exited", "before-the-callback-unreaped", "before-the-task-runs"],
)
async def test_a_stop_scheduled_for_a_replaced_process_does_not_touch_the_replacement(
    tmp_path, monkeypatch, replaced, _live_requested_auto_registry
):
    """The callback and its task close over the old process: neither stops a later client
    process, whether the old one has exited or is not yet reaped when it is replaced."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    _rows, mode_changes = _sel(monkeypatch)
    client = _client(tmp_path)
    old_process = mock.MagicMock(returncode=None)
    new_process = mock.MagicMock(returncode=None)
    client._process = old_process
    client._session_id = "s-1"
    client._claude_confirmed_mode = "auto"
    client._claude_mode_watched = True
    sent, killed = _harness(client, monkeypatch)
    scheduled: list[tuple[object, tuple]] = []

    class _QueuedLoop:
        @staticmethod
        def is_closed() -> bool:
            return False

        @staticmethod
        def is_running() -> bool:
            return True

        @staticmethod
        def call_soon_threadsafe(callback, *args) -> None:
            scheduled.append((callback, args))

    _live_requested_auto_registry[client] = _QueuedLoop()
    mode_mod._on_claude_trust_denied(frozenset({"trust"}))
    assert len(scheduled) == 1
    callback, args = scheduled[0]

    if replaced == "before-the-task-runs":
        callback(*args)
        assert client._claude_policy_stop_task is not None
        client._process = new_process
        await asyncio.wait_for(client._claude_policy_stop_task, FALLBACK_TIMEOUT_SECS)
    else:
        client._process = new_process
        if replaced == "before-the-callback-exited":
            old_process.returncode = 0
        callback(*args)
        assert getattr(client, "_claude_policy_stop_task", None) is None

    assert killed == [] and sent == []
    old_process.kill.assert_not_called()
    new_process.kill.assert_not_called()
    assert _outcomes(mode_changes) == [_DENIED]


@pytest.mark.asyncio
async def test_the_live_requested_auto_registry_does_not_keep_a_client_alive(
    tmp_path, _live_requested_auto_registry
):
    client = _client(tmp_path)
    _watch_live_client(client, "auto")
    reference = weakref.ref(client)
    assert client in _live_requested_auto_registry

    del client
    gc.collect()

    assert reference() is None
    assert list(_live_requested_auto_registry) == []


@pytest.mark.asyncio
async def test_a_requested_auto_session_already_in_auto_needs_no_write_or_warning(
    tmp_path, monkeypatch, caplog
):
    _rows, mode_changes = _sel(monkeypatch)
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _apply(client, {"sessionId": "s-1", "modes": _modes("auto")})
    assert sent == [] and killed == [] and mode_changes == []
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("started", "approves"),
    [("plan", False), ("dontAsk", False), ("acceptEdits", True), ("bypassPermissions", True)],
)
async def test_a_requested_auto_warns_for_every_inherited_mode_it_keeps(
    tmp_path, monkeypatch, caplog, started, approves
):
    _rows, mode_changes = _sel(monkeypatch)
    client = _client(tmp_path, session_key="sk")
    client._session_id = "s-1"
    sent, killed = _harness(client, monkeypatch)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _apply(client, {"sessionId": "s-1", "modes": _modes(started)})
    assert sent == [] and killed == []
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1 and started in warnings[0]
    if approves:
        assert "which approves calls without asking; Crew keeps that mode" in warnings[0]
        assert "KIROCREW_CC_PIN_INHERITED_MODE=1" in warnings[0]
        assert "'bypassPermissions' to 'default' first" in warnings[0]
        assert _outcomes(mode_changes) == ["kept_inherited_mode"]
        assert mode_changes[0]["error"] == "requested_mode_not_applied"
        assert mode_changes[0]["caller"] == "sk" and mode_changes[0]["resources"] == "s-1"
    else:
        assert "asking or narrower" in warnings[0]
        assert mode_changes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("modes", [{"availableModes": [{"id": "auto"}]}, "default", None])
async def test_a_requested_auto_warns_when_the_current_mode_is_not_reported(
    tmp_path, monkeypatch, caplog, modes
):
    _rows, mode_changes = _sel(monkeypatch)
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _apply(client, {"sessionId": "s-1", "modes": modes})
    assert sent == [] and killed == [] and mode_changes == []
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1 and "did not report its current mode" in warnings[0]


async def _restart(client, wire) -> None:
    """A respawn of the same conversation: the next start resumes the session it ran."""
    client._resume_session_id = client._session_id
    await client._initialize_session()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load", "respawn"])
async def test_a_ceiling_that_denies_trust_keeps_the_session_in_default(
    tmp_path, agents_dir, monkeypatch, caplog, path
):
    """No ``set_mode`` ``auto`` is sent, one warning names the policy, an SEL record
    says the policy refused it, and the session asks: an unasked call gets the row of
    a session confirmed in ``default``. A respawn into a session the adapter resumes in
    ``auto`` (a transcript left there before the policy landed) is moved to
    ``default`` before its first prompt."""
    rows, mode_changes = _sel(monkeypatch)
    ceiling = _Ceiling(monkeypatch, permits_trust=path == "respawn")
    client, wire = _wired_client(
        tmp_path, agents_dir, monkeypatch, "new" if path == "respawn" else path
    )
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())
    if path == "respawn":
        await client._initialize_session()
        assert wire.mode == "auto" and _outcomes(mode_changes) == ["enabled"]
        mode_changes.clear()
        ceiling.permits_trust = False

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        if path == "respawn":
            await _restart(client, wire)
        else:
            await client._initialize_session()

    assert wire.set_modes == (["auto", "default"] if path == "respawn" else [])
    assert wire.mode == "default"
    assert client._claude_confirmed_mode == "default"
    assert client._claude_mode_watched is True
    assert wire.killed == []
    assert len(_policy_warnings(caplog)) == 1
    assert _outcomes(mode_changes) == [_DENIED]
    await _an_unasked_call_completes(client)
    assert _attributions(rows) == [_UNATTRIBUTED_DEFAULT]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
async def test_a_ceiling_that_permits_trust_applies_auto_and_records_it(
    tmp_path, agents_dir, monkeypatch, caplog, path
):
    rows, mode_changes = _sel(monkeypatch)
    _Ceiling(monkeypatch, permits_trust=True)
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, path)
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await client._initialize_session()
        await client.ensure_ready()

    assert wire.set_modes == ["auto"]
    assert client._claude_confirmed_mode == "auto"
    assert _policy_warnings(caplog) == []
    assert _outcomes(mode_changes) == ["enabled"]
    assert mode_changes[0]["caller"] == "sk" and mode_changes[0]["source"] == "acp"
    await _an_unasked_call_completes(client)
    assert [(o, d) for o, d, _m in _attributions(rows)] == [("auto_approved", "claude_classifier")]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("refuse_default", [False, True], ids=["moved-back", "move-refused"])
async def test_a_deny_installed_while_the_session_is_in_auto_moves_it_before_the_next_prompt(
    tmp_path, agents_dir, monkeypatch, caplog, path, refuse_default
):
    """The ceiling is read again before every prompt (``ensure_ready``'s warm path), so
    a deny installed after the session reached ``auto`` moves it to ``default`` first,
    once; a move that is refused stops the session, as a failed pin does."""
    _rows, mode_changes = _sel(monkeypatch)
    ceiling = _Ceiling(monkeypatch, permits_trust=True)
    client, wire = _wired_client(
        tmp_path, agents_dir, monkeypatch, path, refuse_default=refuse_default
    )
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())
    await client._initialize_session()
    assert wire.mode == "auto"
    mode_changes.clear()

    ceiling.permits_trust = False
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        if refuse_default:
            with pytest.raises(client_mod.AcpError, match="the session was stopped"):
                await client.ensure_ready()
            assert wire.killed == [True]
        else:
            await client.ensure_ready()
            await client.ensure_ready()
            assert wire.mode == "default"
            assert client._claude_confirmed_mode == "default"
            assert wire.killed == []

    assert wire.set_modes == ["auto", "default"]
    assert len(_policy_warnings(caplog)) == 1
    assert _outcomes(mode_changes) == [_DENIED]


@pytest.mark.asyncio
@pytest.mark.parametrize("started", ["acceptEdits", "bypassPermissions"])
async def test_a_deny_moves_a_requested_auto_session_out_of_any_mode_that_approves(
    tmp_path, agents_dir, monkeypatch, started
):
    """A requested-``auto`` session that starts in a mode which approves on its own
    keeps it as on main while ``trust`` is permitted, and is moved to ``default``
    before its first prompt once the ceiling denies ``trust``."""
    _sel(monkeypatch)
    _Ceiling(monkeypatch, permits_trust=False)
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, "load", started=started)
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())

    await client._initialize_session()

    assert wire.set_modes == ["default"]
    assert wire.mode == "default" and client._claude_confirmed_mode == "default"


@pytest.fixture
def _real_ceiling(tmp_path, _floor_monkeypatch):
    """Install a real ``approval_modes`` ceiling, as ``test_approval_modes_trust_policy``."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.governance import parse_policy

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    ctx_mod.reset_context()
    amp.reset_state()

    def _install(rule: dict) -> None:
        base = build_default_context(KiroCrewConfig.load())
        policy = {"version": 1, "boot": {"fail_closed": True}, "approval_modes": rule}
        ctx_mod.set_context(dataclasses.replace(base, governance=parse_policy(policy)))

    yield _install
    gp.reset_store()
    ctx_mod.reset_context()
    amp.reset_state()


def _install_finishes_during_the_wait(monkeypatch) -> list[bool]:
    """``ensure_resolved`` as an install that completes while it is waited for.

    Returns, per wait, whether the verdict was still pending when the wait began."""
    real = amp.ensure_resolved
    waits: list[bool] = []

    def _ensure_resolved() -> None:
        waits.append(amp.verdict_pending())
        amp._on_ceiling_installed(object())
        real()

    monkeypatch.setattr(amp, "ensure_resolved", _ensure_resolved)
    return waits


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["installed", "install-finishes", "install-outlasts-the-wait"])
async def test_a_ceiling_install_in_progress_is_waited_out_before_the_request(
    tmp_path, agents_dir, monkeypatch, caplog, _real_ceiling, window
):
    """A ceiling whose rule narrows the scope (here it refuses only ``trust_reads``)
    permits ``trust`` once installed. While a new one is being installed the verdict
    answers deny as a placeholder, which the start does not commit: it waits for the
    final answer and then applies ``auto``. An install that outlasts the wait keeps
    ``default``, with a warning and no denial record, since no policy refused it."""
    _rows, mode_changes = _sel(monkeypatch)
    _real_ceiling({"mode": "allow", "allow": ["normal", "trust"]})
    assert amp.session_mode_permitted("trust") is True
    waits = _install_finishes_during_the_wait(monkeypatch) if window == "install-finishes" else []
    if window != "installed":
        amp._on_ceiling_invalidating()
    if window == "install-outlasts-the-wait":
        monkeypatch.setattr(amp, "_INSTALL_WAIT_SECS", 0.0)
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, "new")
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await client._initialize_session()

    outlasted = window == "install-outlasts-the-wait"
    assert wire.set_modes == ([] if outlasted else ["auto"])
    assert client._claude_confirmed_mode == ("default" if outlasted else "auto")
    assert waits == ([True] if window == "install-finishes" else [])
    assert _outcomes(mode_changes) == ([] if outlasted else ["enabled"])
    pending = [r for r in caplog.records if "is not final yet" in r.getMessage()]
    assert len(pending) == (1 if outlasted else 0)
    assert _policy_warnings(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["install-finishes", "install-outlasts-the-wait"])
async def test_a_session_in_auto_waits_out_a_ceiling_install_before_its_next_prompt(
    tmp_path, agents_dir, monkeypatch, _real_ceiling, window
):
    """The ceiling is re-read before each prompt, and a placeholder deny is waited out
    there too: an install that finishes permitting ``trust`` leaves the session in
    ``auto``; one that outlasts the wait moves it to ``default`` (fail closed) with no
    denial record."""
    _rows, mode_changes = _sel(monkeypatch)
    _real_ceiling({"mode": "allow", "allow": ["normal", "trust"]})
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, "new")
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())
    await client._initialize_session()
    assert wire.mode == "auto"
    mode_changes.clear()
    waits = _install_finishes_during_the_wait(monkeypatch) if window == "install-finishes" else []
    amp._on_ceiling_invalidating()
    if window == "install-outlasts-the-wait":
        monkeypatch.setattr(amp, "_INSTALL_WAIT_SECS", 0.0)

    await client.ensure_ready()

    outlasted = window == "install-outlasts-the-wait"
    assert wire.set_modes == (["auto", "default"] if outlasted else ["auto"])
    assert wire.mode == ("default" if outlasted else "auto")
    assert waits == ([] if outlasted else [True])
    assert mode_changes == []


def _real_drain(client, monkeypatch) -> list[dict]:
    """Run Crew's own ``_drain_notifications`` on the wire; return each call's kwargs."""
    real = types.MethodType(AcpClient._drain_notifications, client)
    calls: list[dict] = []

    async def _drain(bound_client, **kwargs):
        assert bound_client is client
        calls.append(kwargs)
        await real(**kwargs)

    monkeypatch.setattr(transport_mod, "_drain_notifications", _drain)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
async def test_a_start_in_auto_whose_model_switch_reports_late_is_undone_before_the_first_prompt(
    tmp_path, agents_dir, monkeypatch, path
):
    """A requested-``auto`` session that starts in ``auto`` sends no ``set_mode``
    ``auto``; its startup model switch to a model without Auto mode support is answered
    before the adapter reports ``acceptEdits``. The drain keeps its idle window, reads
    that report, and the session is moved back to ``default`` before the start returns."""
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, path, started="auto", late=True)
    monkeypatch.setattr(
        client, "_apply_startup_model", lambda: wire.switch_model(_WireAdapter.NO_AUTO_MODEL)
    )
    drains = _real_drain(client, monkeypatch)

    await client._initialize_session()

    assert [call["idle_exit"] for call in drains] == [_DRAIN_IDLE_EXIT]
    assert wire.set_modes == ["default"]
    assert wire.mode == "default" and client._claude_confirmed_mode == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize(
    "model", [_WireAdapter.AUTO_MODEL, _WireAdapter.NO_AUTO_MODEL], ids=["with-auto", "no-auto"]
)
async def test_a_landing_reported_after_the_timeouts_move_back_is_undone_before_the_first_prompt(
    tmp_path, agents_dir, monkeypatch, path, model
):
    """``set_mode`` ``auto`` goes unanswered, so it is undone with ``set_mode``
    ``default``; the adapter answers that and only then reports where the ``auto``
    landed: ``auto`` itself on a model with Auto mode support, ``acceptEdits`` on one
    without. Crew withdrew that request, so either is moved back before the first
    prompt, and no ``enabled`` record is written for an ``auto`` Crew did not keep."""
    _rows, mode_changes = _sel(monkeypatch)
    monkeypatch.setattr(session_mod, "_INIT_TIMEOUT", 0.3)
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, path, withhold_auto=True)
    monkeypatch.setattr(client, "_apply_startup_model", lambda: wire.switch_model(model))
    drains = _real_drain(client, monkeypatch)

    await client._initialize_session()

    assert [call["idle_exit"] for call in drains] == [_DRAIN_IDLE_EXIT]
    assert wire.set_modes == ["auto", "default", "default"]
    assert wire.mode == "default" and client._claude_confirmed_mode == "default"
    assert wire.killed == []
    assert mode_changes == []


def test_only_a_withdrawn_auto_is_due_when_it_is_reported(tmp_path):
    """An ``auto`` report marks the session for a move back only after Crew withdrew a
    ``set_mode`` ``auto``; it is the landing of the request otherwise."""
    from test_claude_requested_mode import _mode_report, _modes, _report

    for withdrawn in (False, True):
        client = _client(tmp_path)
        client._session_id = "s-1"
        client._track_claude_starting_mode({"modes": _modes("default")})
        client._claude_auto_withdrawn = withdrawn
        client._note_claude_mode_report(_report("s-1", _mode_report("auto")))
        assert client._claude_confirmed_mode == "auto"
        assert client._claude_mode_restore_due is withdrawn


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
async def test_a_marked_session_the_ceiling_denies_records_the_denial(
    tmp_path, agents_dir, monkeypatch, caplog, path
):
    """A mode report marked the session (a switch to a model without Auto mode
    support put it in ``acceptEdits``) and the ceiling denies ``trust`` at the same
    prompt: one move back to ``default``, and the policy's denial is recorded."""
    from test_claude_requested_mode import _running_in_auto

    _rows, mode_changes = _sel(monkeypatch)
    ceiling = _Ceiling(monkeypatch, permits_trust=True)
    client, wire = await _running_in_auto(tmp_path, agents_dir, monkeypatch, path)
    await wire.switch_model(_WireAdapter.NO_AUTO_MODEL)
    assert wire.mode == "acceptEdits" and client._claude_mode_restore_due is True
    mode_changes.clear()
    ceiling.permits_trust = False

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await client.ensure_ready()

    assert wire.set_modes == ["auto", "default"]
    assert wire.mode == "default"
    assert _outcomes(mode_changes) == [_DENIED]
    assert len(_policy_warnings(caplog)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("requested", ["auto", None], ids=["requested-auto", "no-request"])
async def test_only_a_requested_auto_start_keeps_the_drain_idle_window(
    tmp_path, agents_dir, monkeypatch, path, requested
):
    """The claude start drain with no idle window (``idle_exit`` ``0.0``) holds for every
    claude start except one that requested ``auto``, whatever its request's outcome."""
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = _client(tmp_path, permission_mode=requested, agent="kirocrew", session_key="sk")
    client._write_claude_local_settings()
    if path == "load":
        client._resume_session_id = "s-old"
    wire = _WireAdapter(client, monkeypatch)
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())

    await client._initialize_session()

    assert wire.drains == ([_DRAIN_IDLE_EXIT] if requested == "auto" else [0.0])


@pytest.mark.asyncio
async def test_a_mode_id_that_is_not_a_string_is_skipped(tmp_path, monkeypatch, caplog):
    """An ``availableModes`` entry whose ``id`` is a list or an object names no mode:
    it neither raises out of the start nor stops a string ``auto`` from being found."""
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch)
    malformed = [{"id": ["auto"]}, {"id": {"auto": True}}, {"id": 7}]

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _apply(
            client,
            {
                "sessionId": "s-1",
                "modes": {
                    "currentModeId": "default",
                    "availableModes": [*malformed, {"id": "default"}],
                },
            },
        )
    assert sent == []
    assert any("does not offer it" in r.getMessage() for r in caplog.records)

    await _apply(
        client,
        {
            "sessionId": "s-1",
            "modes": {"currentModeId": "default", "availableModes": [*malformed, {"id": "auto"}]},
        },
    )
    assert [p["modeId"] for _m, p in sent] == ["auto"]
    assert killed == []


#: A narrowed worker needs the session options, so it is refused below the floor, and
#: unlike a Side Chat or a zero-tool spec it is not skipped by the requested ``auto``.
_NARROWED = ("Read", "Grep")


@pytest.mark.asyncio
async def test_a_requested_auto_session_refused_before_spawn_never_reaches_the_request(
    tmp_path, monkeypatch
):
    """An installed adapter below the session-options floor refuses an option-bound
    session before any process starts. A requested ``auto`` on it sends nothing, asks
    no ceiling, writes no ``mode_change:claude_auto`` record and is not watched."""
    from kiro_crew.acp.harness import claude as claude_mod
    from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
    from kiro_crew.agent_sdk.drivers.acp import forget_cached_resolution

    _rows, mode_changes = _sel(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(amp, "session_mode_permitted", lambda mode: asked.append(mode) or True)
    monkeypatch.setattr(
        claude_mod, "_resolve_claude_acp_bin", lambda: (["/opt/bin/claude-agent-acp"], "")
    )
    monkeypatch.setattr(claude_mod, "_claude_adapter_installed_version", lambda _argv: "0.33.1")
    forget_cached_resolution(ACP_BACKEND_CLAUDE)
    client = _client(tmp_path)

    async def project_array() -> None:
        client._claude_worker_builtin_tools = _NARROWED

    monkeypatch.setattr(client, "_seed_session_settings", mock.AsyncMock())
    monkeypatch.setattr(client, "_prepare_session_mcp", project_array)
    monkeypatch.setattr(client, "_prepare_spawn_workspace", lambda: None)
    initialized = mock.AsyncMock()
    monkeypatch.setattr(client, "_initialize_session", initialized)
    try:
        with pytest.raises(AcpToolGateUnroutable, match="0.33.1"):
            await client.ensure_ready()
    finally:
        forget_cached_resolution(ACP_BACKEND_CLAUDE)

    initialized.assert_not_awaited()
    assert client._claude_mode_watched is False
    assert asked == [] and mode_changes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize(
    "version", ["0.33.1", None], ids=["handshake-below-floor", "handshake-at-floor"]
)
async def test_a_requested_auto_session_the_handshake_refuses_never_reaches_the_request(
    tmp_path, agents_dir, monkeypatch, path, version
):
    """A wrapper's version is read only at the handshake. One below the floor stops an
    option-bound requested-``auto`` session in the pin, before the request: no
    ``set_mode``, no ceiling read, no ``mode_change:claude_auto`` record. The same
    narrowed session on an adapter at the floor gets the requested ``auto``."""
    from kiro_crew.acp.transport_errors import AcpToolGateUnroutable

    _rows, mode_changes = _sel(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(amp, "session_mode_permitted", lambda mode: asked.append(mode) or True)
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, path)
    project = client._resolve_session_mcp_servers

    def narrowed_projection():
        # What the spec projection records for a worker whose built-ins are narrowed.
        servers = project()
        client._claude_worker_builtin_tools = _NARROWED
        return servers

    monkeypatch.setattr(client, "_resolve_session_mcp_servers", narrowed_projection)
    if version is not None:
        wire.recorded["initialize"]["agentInfo"]["version"] = version
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())

    if version is not None:
        with pytest.raises(AcpToolGateUnroutable, match="not verified to honour"):
            await client._initialize_session()
        assert wire.killed == [True]
        assert wire.set_modes == []
        assert asked == [] and mode_changes == []
    else:
        await client._initialize_session()
        assert client._claude_worker_builtin_tools == _NARROWED
        assert wire.set_modes == ["auto"]
        assert set(asked) == {"trust"}
        assert _outcomes(mode_changes) == ["enabled"]


#: A project ``settings.json`` that refuses the exclusion: Crew cannot leave the foreign
#: ``settings.local.json`` out without dropping what it sets, so the file loads.
_DECLINED_SHAPES = {
    "hooks": {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}},
    "sandbox": {"sandbox": {"enabled": True}},
    "unreadable": None,
}


def _declined_folder(work_dir, shape: str | None) -> None:
    """A hand-made ``settings.local.json`` beside a ``settings.json`` of *shape*.

    ``None`` writes neither file, so Crew authors the session's own seed."""
    if shape is None:
        return
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.local.json").write_text(
        json.dumps({"permissions": {}}), encoding="utf-8"
    )
    project = _DECLINED_SHAPES[shape]
    if project is None:
        # Not a regular file: the bytes cannot be examined, so nothing is left out.
        (claude_dir / "settings.json").mkdir()
    else:
        (claude_dir / "settings.json").write_text(json.dumps(project), encoding="utf-8")


def _governance_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "could not be left out" in r.getMessage()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load", "fresh-conversation"])
@pytest.mark.parametrize("shape", ["hooks", "sandbox", "unreadable", None], ids=str)
async def test_a_session_crew_does_not_govern_is_not_switched_to_auto(
    tmp_path, agents_dir, monkeypatch, caplog, path, shape
):
    """A foreign ``settings.local.json`` Crew could not leave out loads, so Crew does not
    govern the session's permissions and withholds its MCP tools. Its requested ``auto``
    is not applied either: no ``set_mode`` ``auto``, one warning, the exclusion path's
    chat notice and no record; the session keeps ``default`` and its unasked call gets
    the row of a session confirmed there. A fresh conversation on the same process
    re-seeds and decides the same. With no foreign file Crew governs it, as today."""
    rows, mode_changes = _sel(monkeypatch)
    _declined_folder(tmp_path, shape)
    client, wire = _wired_client(
        tmp_path, agents_dir, monkeypatch, "load" if path == "load" else "new"
    )
    assert client._claude_local_settings_excluded is False
    assert client._permission_surface_governed is (shape is None)
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await client._initialize_session()
        if path == "fresh-conversation":
            await client.new_conversation()

    expected_notice = (
        "" if shape is None else session_mod._CLAUDE_MODE_PINNED_NOTICE.format(mode="auto")
    )
    assert client.claude_mode_pinned_notice == expected_notice
    if shape is None:
        # The fake keeps the adapter's mode, so a fresh session there already reports it.
        assert wire.set_modes == ["auto"]
        assert client._claude_confirmed_mode == "auto"
        assert _outcomes(mode_changes) == ["enabled"] * len(wire.set_modes)
        assert _governance_warnings(caplog) == []
    else:
        assert wire.set_modes == []
        assert wire.mode == "default" and client._claude_confirmed_mode == "default"
        assert mode_changes == []
        starts = 2 if path == "fresh-conversation" else 1
        assert len(_governance_warnings(caplog)) == starts
        await _an_unasked_call_completes(client)
        assert _attributions(rows) == [_UNATTRIBUTED_DEFAULT]
    assert wire.killed == []


#: Existing ``AcpClient`` seams that the Claude subclass overrides for requested mode.
_CLAUDE_OWNED_SEAMS = (
    "_initialize_params",
    "_drain_notifications",
    "_read_message",
    "_push_model_config_option",
    "ensure_ready",
)


def test_requested_mode_sequencing_stays_off_the_shared_client_paths():
    """The Claude subclass owns every requested-mode step; the shared start,
    frame read and model switch contain none of its conditionals or awaits."""
    from kiro_crew.acp import client_claude_mode as mode_mod
    from kiro_crew.acp import client_claude_mode_seams as mode_seams
    from kiro_crew.acp import client_model_selection as model_mod
    from kiro_crew.acp import client_start as start_mod

    forbidden_by_function = {
        start_mod._initialize_session: (
            "_apply_requested_mode",
            "_restore_asking_mode_if_due",
            "_follows_requested_mode",
            "mode_unsettled",
            "_ended_on_cancel",
        ),
        start_mod.ensure_ready: (
            "_restore_asking_mode_if_due",
            "_follows_requested_mode",
        ),
        transport_mod._read_message: (
            "_note_mode_report",
            "_follows_requested_mode",
        ),
        model_mod.set_model: (
            "_leave_approving_mode_for_model_switch",
            "_follows_requested_mode",
        ),
    }
    for function, forbidden in forbidden_by_function.items():
        source = inspect.getsource(function)
        assert [name for name in forbidden if name in source] == []

    removed_no_op_hooks = (
        "_follows_requested_mode",
        "_forget_confirmed_mode",
        "_apply_requested_mode",
        "_restore_asking_mode_if_due",
        "_note_mode_report",
        "_leave_approving_mode_for_model_switch",
    )
    assert [name for name in removed_no_op_hooks if name in vars(AcpClient)] == []
    assert {name: vars(ClaudeAcpClient)[name].__module__ for name in _CLAUDE_OWNED_SEAMS} == {
        name: mode_seams.__name__ for name in _CLAUDE_OWNED_SEAMS
    }
    assert vars(ClaudeAcpClient)["_forget_confirmed_mode"].__module__ == mode_seams.__name__
    assert vars(ClaudeAcpClient)["_unasked_call_attribution"].__module__ == mode_mod.__name__
    assert ClaudeAcpClient._pins_starting_mode is True


@pytest.mark.asyncio
async def test_a_non_claude_unasked_call_keeps_mains_sel_row(tmp_path, monkeypatch):
    """``AcpClient``'s attribution is main's: an unasked call of a non-claude client asked
    to run in ``auto`` gets the row it got before this change, with no ``permission_mode``."""
    rows: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            rows.append(kw)

    monkeypatch.setattr(client_mod, "sel", lambda: _Sel())
    client = AcpClient(
        work_dir=tmp_path, session_key="sk", agent="kirocrew", permission_mode="auto"
    )
    assert type(client) is AcpClient
    client._observed_tool_calls["tc1"] = ("Bash", "execute")
    await client._maybe_audit_unasked_tool_call(
        types.SimpleNamespace(tool_call_id="tc1", tool_status="completed")
    )
    assert [row["metadata"] for row in rows] == [
        {
            "reason": "claude_auto_mode",
            "decided_by": "claude_classifier",
            "tool_status": "completed",
        }
    ]


def _sdk_task_frame(session_id: str, task_id: str) -> dict:
    """A ``_claude/sdkMessage`` background-task start, in the recorded 0.94.0 shape
    (``test/fixtures/acp_frames/claude/raw-sdk-tasks-live.jsonl``)."""
    return {
        "jsonrpc": "2.0",
        "method": "_claude/sdkMessage",
        "params": {
            "sessionId": session_id,
            "message": {
                "type": "system",
                "subtype": "task_started",
                "task_id": task_id,
                "task_type": "local_bash",
                "description": "Sleep then print",
                "session_id": session_id,
            },
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report", [_mode_report("acceptEdits"), _option_report("acceptEdits")], ids=["mode", "option"]
)
async def test_a_mode_report_behind_raw_sdk_task_frames_is_followed(tmp_path, report):
    """``_read_message`` reads Claude Code's raw SDK task frames into the session's state
    and returns none of them (``raw_sdk_tasks.take``). The mode report that follows them
    in the same read is returned, the follower marks the move back, and the turn's
    mode-change notice still reads the same frame (``harness_notices``)."""
    client = _client(tmp_path)
    client._session_id = "s-1"
    client._track_claude_starting_mode({"modes": _modes("auto")})
    # This frame is read by the start drain, where the lifecycle still owns the
    # stream and restores the reported mode before the first prompt.
    client._claude_start_response = {"sessionId": "s-1"}
    mode_frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s-1", "update": report},
    }
    lines = [
        _sdk_task_frame("s-1", "task-a"),
        _sdk_task_frame("s-1", "task-b"),
        mode_frame,
    ]
    process = mock.MagicMock()
    process.returncode = None
    process.stdout.readline = mock.AsyncMock(
        side_effect=[(json.dumps(line) + "\n").encode() for line in lines]
    )
    client._process = process

    msg = await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)

    assert msg is not None and msg.method == "session/update"
    assert msg.params["update"] == report
    assert list(client._raw_sdk.tasks) == ["task-a", "task-b"]
    assert client._claude_confirmed_mode == "acceptEdits"
    assert client._claude_mode_restore_due is True
    notice = harness_notices.mode_notice_event(client, msg)
    if report["sessionUpdate"] == "current_mode_update":
        assert notice is not None and notice.harness_notice.mode_id == "acceptEdits"
    else:
        assert notice is None


@pytest.mark.asyncio
async def test_async_requested_mode_audit_runs_off_the_event_loop(tmp_path, monkeypatch):
    """The first SEL use at session start must not do file I/O on the ACP loop."""
    loop_thread = threading.get_ident()
    write_threads: list[int] = []

    class _Sel:
        def log_api_access(self, **_kw):
            write_threads.append(threading.get_ident())

    monkeypatch.setattr(client_mod, "sel", lambda: _Sel())
    client = _client(tmp_path)
    sent, killed = _harness(client, monkeypatch)

    await _apply(client, {"sessionId": "s-1", "modes": _modes("default")})

    assert [params["modeId"] for method, params in sent if method == "session/set_mode"] == ["auto"]
    assert killed == []
    assert len(write_threads) == 1 and write_threads[0] != loop_thread


@pytest.mark.asyncio
async def test_a_deny_that_lands_during_the_verdict_wait_owns_the_stop(
    tmp_path, monkeypatch, _live_requested_auto_registry
):
    """After the wait, the hook's mark is re-read before any audit or mode request."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    _rows, mode_changes = _sel(monkeypatch)
    _Ceiling(monkeypatch, permits_trust=False)
    client = _client(tmp_path)
    _watch_live_client(client, "auto")
    sent, killed = _harness(client, monkeypatch)
    pending = True

    def _pending() -> bool:
        return pending

    def _finish_install() -> None:
        nonlocal pending
        mode_mod._on_claude_trust_denied(frozenset({"trust"}))
        pending = False

    monkeypatch.setattr(amp, "verdict_pending", _pending)
    monkeypatch.setattr(amp, "ensure_resolved", _finish_install)

    await client._restore_claude_asking_mode_if_due()

    assert sent == []
    assert killed == [True]
    assert _outcomes(mode_changes) == [_DENIED]


def test_closed_loop_fallback_kills_the_captured_tree_and_escaped_children(tmp_path, monkeypatch):
    """The synchronous fallback kills descendants, not only the adapter process."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    root, stored, fresh = unallocatable_pids(3)
    client = _client(tmp_path)
    process = mock.MagicMock(returncode=None)
    process.pid = root
    client._process = process
    client._pid = process.pid
    client._start_time = "ours"
    client._child_pids = {stored: mock.sentinel.stored_child}
    killed_trees: list[tuple[int, int]] = []
    swept: list[dict[int, object]] = []

    monkeypatch.setattr(mode_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(mode_mod.platform_compat, "get_process_start_id", lambda _pid: "ours")
    monkeypatch.setattr(mode_mod.process_identity, "isolated_group_of", lambda pid, _start: pid)
    monkeypatch.setattr(
        mode_mod.platform_compat,
        "kill_process_group",
        lambda group, sig: killed_trees.append((group, sig)) or True,
    )
    monkeypatch.setattr(
        mode_mod.runtime_process_tree, "_get_child_pids", lambda pid: [stored, fresh]
    )
    monkeypatch.setattr(
        mode_mod.runtime_process_tree,
        "_capture_child_records",
        lambda pids: {pid: mock.sentinel.fresh_child for pid in pids},
    )
    monkeypatch.setattr(
        mode_mod.runtime_process_tree,
        "_kill_escaped_children",
        lambda children: swept.append(dict(children)),
    )

    mode_mod._stop_claude_without_its_loop(client, process)

    assert killed_trees == [(root, mode_mod.platform_compat.SIGKILL)]
    assert set(swept[0]) == {stored, fresh}
    process.kill.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["closed", "enqueue-error"])
async def test_a_synchronous_trust_deny_fallback_records_one_interrupted_call(
    tmp_path, monkeypatch, _live_requested_auto_registry, fallback
):
    """The synchronous fallback settles before the tree kill and writes after it."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    rows, mode_changes = _sel(monkeypatch)
    client = _client(tmp_path)
    process = _watch_live_client(client, "auto")
    process.pid = unallocatable_pids(1)[0]
    client._start_time = "ours"
    client._observed_tool_calls["tc1"] = ("Read", "read")
    killed: list[int] = []

    class _FallbackLoop:
        @staticmethod
        def is_closed() -> bool:
            return fallback == "closed"

        @staticmethod
        def is_running() -> bool:
            return True

        @staticmethod
        def call_soon_threadsafe(*_args) -> None:
            raise RuntimeError("loop closed during enqueue")

    _live_requested_auto_registry[client] = _FallbackLoop()
    monkeypatch.setattr(mode_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(mode_mod.platform_compat, "get_process_start_id", lambda _pid: "ours")
    monkeypatch.setattr(mode_mod.process_identity, "isolated_group_of", lambda pid, _start: pid)
    monkeypatch.setattr(mode_mod.runtime_process_tree, "_get_child_pids", lambda _pid: [])
    monkeypatch.setattr(mode_mod.runtime_process_tree, "_kill_escaped_children", lambda _pids: None)
    monkeypatch.setattr(
        mode_mod.platform_compat, "kill_process_group", lambda group, _sig: killed.append(group)
    )

    mode_mod._on_claude_trust_denied(frozenset({"trust"}))

    assert killed == [process.pid]
    assert _attributions(rows) == [("interrupted_by_policy", "claude_unattributed", "auto")]
    assert rows[0]["metadata"]["reason"] == "claude_unasked_call"
    await _an_unasked_call_completes(client)
    assert len(rows) == 1
    assert _outcomes(mode_changes) == [_DENIED]


@pytest.mark.asyncio
async def test_idle_reader_stops_before_dispatching_a_turn_after_an_approving_report(
    tmp_path, monkeypatch, _live_requested_auto_registry
):
    """A settled approving report cannot precede an agent-started tool turn."""
    _rows, mode_changes = _sel(monkeypatch)
    turns: list[AcpClient] = []
    client = _client(tmp_path)
    client._install_idle_reader()
    reader = asyncio.StreamReader()
    process = mock.MagicMock(returncode=None)
    process.stdout = reader
    client._process = process
    client._session_id = "s-1"
    client.on_agent_turn = turns.append
    client._track_claude_starting_mode({"modes": _modes("default")})
    client._claude_mode_settling = False
    stopped = asyncio.Event()

    async def _stop() -> None:
        client._cancel_idle_reader()
        process.returncode = 0
        stopped.set()

    monkeypatch.setattr(client, "_claude_stop_session", _stop)
    monkeypatch.setattr(
        client,
        "_send_request",
        mock.AsyncMock(side_effect=AssertionError("an unexpected report sends no mode request")),
    )
    mode_frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s-1", "update": _mode_report("acceptEdits")},
    }
    turn_frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s-1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "starting a tool"},
            },
        },
    }

    client._maybe_start_idle_reader()
    for frame in (mode_frame, turn_frame):
        reader.feed_data((json.dumps(frame) + "\n").encode())
    await async_wait_until(
        lambda: stopped.is_set() or bool(turns),
        describe=lambda: {"stopped": stopped.is_set(), "turns": len(turns)},
    )
    assert stopped.is_set()
    task = client._claude_policy_stop_task
    assert task is not None
    await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)

    assert turns == []
    assert client._agent_turn_pending() is False
    assert client._claude_confirmed_mode == "acceptEdits"
    assert mode_changes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("canceller", ["the-stop", "a-prompt-claim"])
async def test_the_idle_reader_awaiting_its_stop_is_cancelled_and_the_stop_still_ends_the_process(
    tmp_path, monkeypatch, canceller, _live_requested_auto_registry
):
    """The idle reader that reads a settled approving report awaits the stop it
    scheduled, and that stop's own kill cancels the reader (``_cancel_idle_reader``);
    so may a prompt claiming stdout (``_stop_idle_reader``). Neither waits on the
    other: the reader ends cancelled at once, the stop goes on to the client's real
    kill path rather than the fallback, and no agent-started turn is dispatched."""
    _rows, mode_changes = _sel(monkeypatch)
    turns: list[AcpClient] = []
    client = _client(tmp_path)
    reader = asyncio.StreamReader()
    process = mock.MagicMock(returncode=None)
    process.stdout = reader
    client._process = process
    client._session_id = "s-1"
    client.on_agent_turn = turns.append
    kill_gate = asyncio.Event()
    stop_gate = asyncio.Event()
    kills: list[object] = []

    async def _kill(*, force=False):
        kills.append("started")
        await asyncio.wait_for(kill_gate.wait(), FALLBACK_TIMEOUT_SECS)
        process.returncode = -9
        kills.append(force)

    # The idle-reader install wraps the kill it finds, as on a live client.
    monkeypatch.setattr(client, "_kill_process", _kill)
    client._install_idle_reader()
    real_stop = client._claude_stop_session

    async def _gated_stop() -> None:
        await asyncio.wait_for(stop_gate.wait(), FALLBACK_TIMEOUT_SECS)
        await real_stop()

    monkeypatch.setattr(client, "_claude_stop_session", _gated_stop)
    if canceller == "the-stop":
        stop_gate.set()
    client._track_claude_starting_mode({"modes": _modes("default")})
    client._claude_mode_settling = False
    frames = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": "s-1", "update": _mode_report("acceptEdits")},
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "starting a tool"},
                },
            },
        },
    ]

    client._maybe_start_idle_reader()
    idle_reader = client._idle_reader
    assert idle_reader is not None
    for frame in frames:
        reader.feed_data((json.dumps(frame) + "\n").encode())
    await async_wait_until(
        lambda: getattr(client, "_claude_policy_stop_task", None) is not None,
        describe=lambda: client._claude_mode_settling,
    )
    stop = client._claude_policy_stop_task
    try:
        if canceller == "the-stop":
            await async_wait_until(lambda: kills, describe=lambda: kills)
        else:
            await asyncio.wait_for(client._claim_stdout(), FALLBACK_TIMEOUT_SECS)
        await asyncio.wait_for(asyncio.wait({idle_reader}), FALLBACK_TIMEOUT_SECS)
        assert idle_reader.cancelled()
        assert not stop.done()
        stop_gate.set()
        kill_gate.set()
        await asyncio.wait_for(stop, FALLBACK_TIMEOUT_SECS)
    finally:
        stop_gate.set()
        kill_gate.set()
        if canceller == "a-prompt-claim" and client._stdout_claims:
            client._release_stdout()

    assert not stop.cancelled()
    assert kills == ["started", True]
    process.kill.assert_not_called()
    assert turns == [] and client._agent_turn_start is None
    assert client._idle_reader is None or client._idle_reader.done()
    assert mode_changes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "ends_in"),
    [(_WireAdapter.AUTO_MODEL, "auto"), (_WireAdapter.NO_AUTO_MODEL, "default")],
    ids=["model-with-auto", "model-without-auto"],
)
async def test_a_fresh_conversation_that_applies_auto_is_not_stopped_by_its_own_report(
    tmp_path, agents_dir, monkeypatch, model, ends_in
):
    """A fresh conversation's start is not settled until it returns: the report the
    adapter sends ahead of its ``{}`` answer to ``set_mode`` ``auto`` (the recorded
    0.100.0 order) is followed as the start's own, so the fresh session ends in
    ``auto``, or is moved back to ``default`` from the ``acceptEdits`` fallback, and
    is never stopped. Only once it has returned does an approving report stop it."""
    _rows, mode_changes = _sel(monkeypatch)
    _Ceiling(monkeypatch, permits_trust=True)
    client, wire = _wired_client(tmp_path, agents_dir, monkeypatch, "new")
    monkeypatch.setattr(client, "_apply_startup_model", mock.AsyncMock())
    await client._initialize_session()
    assert wire.mode == "auto" and client._claude_mode_settling is False
    wire.mode = "default"  # the fresh session/new starts in ``default``
    wire.auto_model = model == _WireAdapter.AUTO_MODEL
    mode_changes.clear()

    await client.new_conversation()

    assert wire.killed == []
    assert wire.set_modes[-1 if ends_in == "auto" else -2] == "auto"
    assert wire.mode == ends_in and client._claude_confirmed_mode == ends_in
    assert client._claude_mode_settling is False
    assert getattr(client, "_claude_policy_stop_task", None) is None
    assert _outcomes(mode_changes) == (["enabled"] if ends_in == "auto" else [])
