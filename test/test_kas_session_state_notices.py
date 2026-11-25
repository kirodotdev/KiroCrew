"""kiro-cli's informational ``_kiro/*`` notices at session start are not routing loss.

KAS sends them on every ``session/new`` and ``session/load``. Those that name the
session arrive before the session is registered (measured on kiro-cli 3.5.0), and
those that name none arrive while a fresh runtime has no session yet. Crew reads
none of them (``classify_notification`` skips them on a registered session), so
counting them as unroutable frames buried real routing loss in
``kirocrew.acp.dropped_frames{method_class=other}``.
"""

import pytest
from acp_runtime_helpers import _drain, _feed, _make_runtime, _register, _start_reader, _stop_reader

# The payloads kiro-cli 3.5.0 sends, with the inventories emptied.
_NOTICES = {
    "_kiro/powers/items_changed": {"sessionId": "sess-new", "status": "success", "powers": []},
    "_kiro/steering/documents_changed": {
        "sessionId": "sess-new",
        "status": "success",
        "documents": [],
    },
    "_kiro/sessions/changed": {"upserted": [], "deleted": []},
    "_kiro/memory/syncStatus": {"state": "off"},
}


def _frame(method: object, params: dict) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", sorted(_NOTICES))
async def test_a_session_start_notice_nobody_can_read_is_not_a_dropped_frame(method):
    rt, reader, _ = _make_runtime()
    task = await _start_reader(rt)
    try:
        _feed(reader, _frame(method, _NOTICES[method]))
        await _drain(reader)
        assert rt._dropped_frames == {}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_a_registered_session_still_receives_the_notices():
    rt, reader, _ = _make_runtime()
    queues = _register(rt, "sess-new")
    task = await _start_reader(rt)
    try:
        for method, params in _NOTICES.items():
            _feed(reader, _frame(method, params))
        await _drain(reader)
        got = []
        while not queues["sess-new"].empty():
            got.append(queues["sess-new"].get_nowait().method)
        assert got == list(_NOTICES)
        assert rt._dropped_frames == {}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [[], {"m": 1}], ids=["list", "object"])
@pytest.mark.parametrize("params", [{"sessionId": "sess-new"}, {}], ids=["session", "no_session"])
async def test_a_non_string_method_is_still_counted_and_the_reader_survives(method, params):
    """``method`` is unvalidated backend JSON: an unhashable one must not reach the set lookup."""
    rt, reader, _ = _make_runtime()
    task = await _start_reader(rt)
    try:
        _feed(reader, _frame(method, params))
        await _drain(reader)
        assert not rt._dead
        assert sum(rt._dropped_frames.values()) == 1
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_another_unroutable_kiro_notice_is_still_counted():
    rt, reader, _ = _make_runtime()
    task = await _start_reader(rt)
    try:
        _feed(reader, _frame("_kiro/some/new_notice", {"sessionId": "sess-new"}))
        _feed(reader, _frame("_kiro/other/new_notice", {}))
        await _drain(reader)
        assert rt._dropped_frames == {
            ("sess-new", "_kiro/some/new_notice"): 1,
            ("-", "_kiro/other/new_notice"): 1,
        }
    finally:
        await _stop_reader(task)
