"""Only the host's own refusal retires a Bot chat start's record (``host_refused``).

An error reply carries its JSON-RPC frame (``rpc_error``); anything raised on
this side does not: a frame too large to read, a lost connection, a timeout, a
dead runtime. The host may have started a session then, so the start's record
and a retry handle stay. Each case below goes red with one change:

* an abandoned ``session/new``: pass ``isinstance(error, AcpRuntimeError)`` to
  ``finish`` in ``_close_abandoned_new`` instead of ``host_refused(error)``;
* an abandoned load: the same in ``_abandon_load``'s ``_settled``;
* a timed-out start's collector: drop ``self._host_refused = host_refused(exc)``
  (keep it True on any error) in ``StartCollector._run``;
* a failed start on a shared runtime with no id: drop the ``answer_lost`` branch
  from ``_keep_abandoned_close``;
* a failed start a stop ran beside, with no id: drop the ``answer_lost`` branch
  from ``bot_chat_ending_fence._keep_runtime_session``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from test_failed_start_late_session import (
    KEY,
    SNAP,
    _Closer,
    _collector,
    _host_refusal,
    _in_start,
    _Runtime,
)

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew import bot_chat_ending_fence as fence
from kiro_crew.acp import runtime_session_create as create
from kiro_crew.acp import runtime_session_load as load
from kiro_crew.acp.session_handle import AcpFrameTooLarge, AcpRequestTimeout, AcpRuntimeDead
from kiro_crew.acp.session_handle_runtime_contract import answer_lost, host_refused
from kiro_crew.acp.session_provider_shared import RuntimeTreeGone, is_local_close_token
from kiro_crew.testing.wait import await_until


def _too_large():
    return AcpFrameTooLarge("session/new reply over the stdout frame limit")


@pytest.mark.parametrize(
    "error,refused,lost",
    [
        (_host_refusal("no such session"), True, False),
        (_too_large(), False, True),
        (AcpRuntimeDead("gone"), False, True),
        (AcpRequestTimeout("slow"), False, False),
        (TimeoutError("refused here"), False, False),
        (None, False, False),
    ],
)
def test_only_a_frame_the_host_sent_is_a_refusal(error, refused, lost):
    assert host_refused(error) is refused
    assert answer_lost(error) is lost


@pytest.mark.asyncio
@pytest.mark.parametrize("make,closed", [(_too_large, False), (_host_refusal, True)])
async def test_an_abandoned_session_new_is_retired_only_by_a_host_refusal(make, closed):
    seen: list[tuple] = []
    request: asyncio.Future = asyncio.get_running_loop().create_future()
    request.set_exception(make("x") if make is _host_refusal else make())
    create._close_abandoned_new(_Runtime(), request, lambda *a: seen.append(a))
    assert seen == [("", closed)]


@pytest.mark.asyncio
async def test_an_abandoned_load_whose_answer_was_too_large_stays_kept():
    rt = _Closer(confirms=False)
    pending: asyncio.Future = asyncio.get_running_loop().create_future()
    _in_start(lambda: load._abandon_load(rt, "resumed-9", pending))
    barrier = load._PENDING_ABANDONED_CLOSES[(id(rt), "resumed-9")]
    pending.set_exception(_too_large())
    await await_until(barrier, what="the abandoned load settled")
    assert any(is_local_close_token(t) for t in tools._SESSION_APP_SNAPSHOT)
    assert tools.app_has_active_bot_sessions("probe")


@pytest.mark.asyncio
async def test_a_collector_whose_answer_was_too_large_keeps_the_start():
    rt = _Runtime()
    tools._SESSION_APP_SNAPSHOT_PENDING[KEY] = SNAP
    collector, answer = _collector(rt)
    answer.set_exception(_too_large())
    await await_until(collector.settled, what="the start collector settled")
    assert any(is_local_close_token(t) for t in tools._SESSION_APP_SNAPSHOT)


@pytest.mark.parametrize("make,kept", [(_too_large, True), (_host_refusal, False)])
def test_a_failed_shared_start_with_no_id_is_kept_only_when_its_answer_was_lost(
    monkeypatch, make, kept
):
    from kiro_crew.providers import acp_runtime_start as start

    noted: list[tuple] = []
    monkeypatch.setattr(start, "_bot_chat_with_apps", lambda key: True)
    monkeypatch.setattr(
        start,
        "note_unconfirmed_teardown",
        lambda provider, handle, sid, *, session_key: noted.append((handle, sid, session_key)),
    )
    rt = _Runtime()
    provider = SimpleNamespace(_owning_session_key=lambda: KEY)
    start._keep_abandoned_close(provider, rt, None, make("x") if make is _host_refusal else make())
    if kept:
        ((handle, sid, key),) = noted
        assert isinstance(handle, RuntimeTreeGone) and handle.runtime is rt
        assert (sid, key) == (None, KEY)
    else:
        assert noted == []


@pytest.mark.parametrize(
    "error,kept", [(_too_large(), True), (TimeoutError("session/load refused"), False)]
)
def test_a_failed_start_a_stop_ran_beside_is_kept_only_when_its_answer_was_lost(
    monkeypatch, error, kept
):
    noted: list[tuple] = []
    monkeypatch.setattr(tools, "note_unconfirmed_close", lambda k, h, s: noted.append((k, h, s)))
    rt = _Runtime()  # KAS: its close ends the session's servers
    fence._keep_runtime_session(rt, KEY, error)
    if kept:
        ((key, handle, sid),) = noted
        assert key == KEY and sid is None
        assert isinstance(handle, RuntimeTreeGone) and handle.runtime is rt
    else:
        assert noted == []
