"""Open-slots tests: a slots read path that fails to serialize names its offender.

The slots flush, the dashboard WS frame, the WS connect snapshot and
GET /api/chat/slots serialize the same projection; each failure carries a note
naming the slot and field, and healthy outputs stay unchanged.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

# ── a serialization failure in the slots flush must name its offender ────────
#
# The evidenced failure class behind a slots-broadcast 500 is a non-serializable
# value in slot state: json.dumps raises deep in the flush, every slots read
# path is equally broken, and the stock TypeError names neither the slot nor
# the field. Worse, when the flush fails while a suspend block is unwinding
# over the body's own exception, the flush's exception REPLACES the body's in
# the caller's view (the original demoted to __context__), which is how such a
# failure reads as a broadcast bug. These pin the two diagnostics: the offender
# note on BOTH coalescing branches, and the unwinding-over note.
# Semantics stay untouched: same exception types, same propagation, same
# chaining, no caught-and-swallowed anything.


def _poison_slots_projection(state):
    """Make serialize_slots return one slot whose ``title`` cannot be dumped."""
    entry = {"key": "chat-poison", "title": object()}
    state.serialize_slots = lambda **kw: [dict(entry)]  # type: ignore[method-assign]


def test_leading_edge_serialization_failure_names_the_offender(tmp_path, monkeypatch):
    """The immediate (leading-edge) broadcast annotates the raising TypeError."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _poison_slots_projection(state)

    with pytest.raises(TypeError) as excinfo:
        state.push_slots_update()

    notes = "\n".join(getattr(excinfo.value, "__notes__", []))
    assert "'chat-poison'" in notes, f"note must name the slot key, got: {notes!r}"
    assert "'title'" in notes, "note must name the offending field"
    assert "object" in notes, "note must name the value's type"
    assert "value withheld" in notes, "note must never carry the value itself"


def test_trailing_flush_serialization_failure_names_the_offender(tmp_path, monkeypatch):
    """The trailing-edge callback funnels through the same annotated dump.

    Both timing branches converge on _do_slots_broadcast, so the diagnostic
    covers them by construction — this pins the trailing half of that claim.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _poison_slots_projection(state)

    with pytest.raises(TypeError) as excinfo:
        state._trailing_slots_flush()

    notes = "\n".join(getattr(excinfo.value, "__notes__", []))
    assert "'chat-poison'" in notes
    assert "'title'" in notes


def test_flush_failure_during_unwind_names_the_masked_exception(tmp_path, monkeypatch):
    """A flush failing during exception unwind must say whose funeral it crashed.

    The body's RuntimeError is the actual fault; the flush's TypeError merely
    reports a broken projection. Today's chaining (body as __context__) is
    preserved and now NAMED, so the top of the traceback stops eating the lede.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _poison_slots_projection(state)

    with pytest.raises(TypeError) as excinfo:
        with state.suspend_slots_push():
            state.push_slots_update()  # queue the owed push
            raise RuntimeError("boom")  # the body's actual fault

    exc = excinfo.value
    assert isinstance(exc.__context__, RuntimeError), "implicit chaining must survive"
    notes = "\n".join(getattr(exc, "__notes__", []))
    assert "unwinding over" in notes and "RuntimeError" in notes
    assert "__context__" in notes, "note must point at where the original went"
    assert "'chat-poison'" in notes, "offender note must also be present"
    assert state._slots_push_suspend == 0, "depth must still unwind"


def test_flush_failure_without_inflight_exception_has_no_unwind_note(tmp_path, monkeypatch):
    """A plain flush failure (body exited normally) gets the offender note only."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _poison_slots_projection(state)

    with pytest.raises(TypeError) as excinfo:
        with state.suspend_slots_push():
            state.push_slots_update()  # owed flush raises on a clean exit

    notes = "\n".join(getattr(excinfo.value, "__notes__", []))
    assert "'chat-poison'" in notes
    assert "unwinding over" not in notes, "no body exception, so no unwind note"


def test_healthy_flush_is_unchanged_by_the_diagnostics(tmp_path, monkeypatch):
    """Benign control: the hoisted dump changes nothing on the happy path."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    seen: list[str] = []
    state._broadcast = lambda note: seen.append(note.get("_type"))  # type: ignore[method-assign]

    state.push_slots_update()

    assert seen.count("slots") == 1


# ── every REMAINING slots read path carries the same offender note ───────────
#
# The sibling read paths serialize the SAME projection, so without the note they
# fail with the same bare TypeError: the
# dashboard-user WS frame (``_slots_ws_frame``, both its send sites), the WS
# connect snapshot, and ``GET /api/chat/slots``. These pin the note on each
# path, the ``path`` label that names which one raised, and the benign controls
# proving healthy outputs are unchanged.


def _poisoned_slots() -> list[dict]:
    return [{"key": "chat-poison", "title": object()}]


def test_ws_frame_serialization_failure_names_the_offender():
    """The dashboard-user WS frame annotates its dump failure with the offender."""
    from kiro_crew.dashboard.state import _slots_ws_frame

    with pytest.raises(TypeError) as excinfo:
        _slots_ws_frame(
            _poisoned_slots(),
            yolo=False,
            channel_trusted=False,
            gitlab_hosts_gen=0,
            folders=[],
            folders_gen=0,
            governance_gen=0,
        )

    notes = "\n".join(getattr(excinfo.value, "__notes__", []))
    assert "[ws-frame]" in notes, f"note must name the raising path, got: {notes!r}"
    assert "'chat-poison'" in notes and "'title'" in notes
    assert "value withheld" in notes


def test_ws_frame_failure_outside_slots_exonerates_the_slot_list():
    """A clean-slots note is evidence too: the offender is in the envelope extras."""
    from kiro_crew.dashboard.state import _slots_ws_frame

    with pytest.raises(TypeError) as excinfo:
        _slots_ws_frame(
            [{"key": "chat-1", "title": "fine"}],
            yolo=False,
            channel_trusted=False,
            gitlab_hosts_gen=0,
            folders=object(),  # the actual offender, outside the slot list
            folders_gen=0,
            governance_gen=0,
        )

    notes = "\n".join(getattr(excinfo.value, "__notes__", []))
    assert "[ws-frame]" in notes
    assert "no offending entry found" in notes, "clean slots must be exonerated"


def test_ws_frame_healthy_roundtrip_unchanged():
    """Benign control: the wrapped dump produces the same frame."""
    from kiro_crew.dashboard.state import _slots_ws_frame

    frame = json.loads(
        _slots_ws_frame(
            [{"key": "chat-1"}],
            yolo=True,
            channel_trusted=False,
            gitlab_hosts_gen=3,
            folders=[{"id": "f1"}],
            folders_gen=7,
            governance_gen=9,
        )
    )

    assert frame == {
        "type": "slots",
        "data": [{"key": "chat-1"}],
        "yolo": True,
        "channelTrusted": False,
        "gitlabHostsGeneration": 3,
        "folders": [{"id": "f1"}],
        "foldersGeneration": 7,
        "governanceGeneration": 9,
    }


@pytest.mark.asyncio
async def test_rest_slots_get_serialization_failure_names_the_offender(tmp_path, monkeypatch):
    """GET /api/chat/slots annotates its dump failure instead of a bare TypeError."""
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.chat_handlers import api_chat_slots

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _poison_slots_projection(state)

    request = make_mocked_request("GET", "/api/chat/slots", app={"state": state})
    request["user"] = "local-app"
    request["app"] = ""

    with pytest.raises(TypeError) as excinfo:
        await api_chat_slots(request)

    notes = "\n".join(getattr(excinfo.value, "__notes__", []))
    assert "[GET /api/chat/slots]" in notes, f"note must name the REST path, got: {notes!r}"
    assert "'chat-poison'" in notes and "'title'" in notes


@pytest.mark.asyncio
async def test_rest_slots_get_healthy_response_is_unchanged(tmp_path, monkeypatch):
    """Benign control: explicit dump serves the same 200/JSON as json_response did."""
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get("/api/chat/slots")
        assert resp.status == 200
        assert resp.content_type == "application/json"
        assert await resp.json() == state.serialize_slots(
            include_check_status=True, dashboard_user=True
        )


@pytest.mark.asyncio
async def test_ws_connect_snapshot_failure_is_logged_with_the_offender(
    tmp_path, monkeypatch, caplog
):
    """The connect snapshot's swallow gets a WARNING carrying the offender note.

    The whole connect block sits under ``except Exception: pass``, so before
    this seam a broken snapshot meant an empty sidebar with zero evidence.
    The exception flow is unchanged (still swallowed); the log is the one new
    observable, and it carries the note through ``exc_info``.
    """
    import logging

    from kiro_crew.dashboard import ws as dashboard_ws

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

    state = MagicMock()
    state.owner_id = "U_OWNER"
    state.serialize_slots = lambda **kw: _poisoned_slots()
    state._yolo = False
    state._folders = [{"id": "f1", "name": "Work", "order": 0}]
    state.folders_generation = MagicMock(return_value=7)

    class Request(dict):
        def __init__(self) -> None:
            super().__init__({"user": "local-app", "app": ""})
            self.setdefault("is_dashboard_user", True)
            self.app = {"state": state}

    class FakeWebSocket:
        def __init__(self) -> None:
            self.closed = True
            self.sent: list = []
            self._flags: dict = {"_is_dashboard_user": True}

        def __setitem__(self, key, value) -> None:
            self._flags[key] = value

        def __getitem__(self, key):
            return self._flags[key]

        def get(self, key, default=None):
            return self._flags.get(key, default)

        async def prepare(self, request) -> None:
            return None

        async def send_json(self, payload) -> None:
            self.sent.append(payload)

        async def send_str(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    fake_ws = FakeWebSocket()
    monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
    monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.ws"):
        result = await dashboard_ws.api_ws(Request())  # type: ignore[arg-type]

    assert result is fake_ws
    slots_frames = [f for f in fake_ws.sent if isinstance(f, dict) and f.get("type") == "slots"]
    assert not slots_frames, "the poisoned snapshot must not have been sent"
    records = [r for r in caplog.records if "connect snapshot" in r.message]
    assert records, "the swallowed failure must leave a WARNING behind"
    exc = records[0].exc_info[1]  # type: ignore[index]
    notes = "\n".join(getattr(exc, "__notes__", []))
    assert "[ws-connect-snapshot]" in notes
    assert "'chat-poison'" in notes and "'title'" in notes


def test_note_helper_path_label_and_degradation():
    """The path label lands in every message; a shape surprise degrades, never raises."""
    from kiro_crew.dashboard.state import _slots_serialization_note

    assert _slots_serialization_note([], path="x") == (
        "[x] slot list fails serialization; no offending entry found"
    )
    # Callers guarantee list-of-dicts; anything else degrades to the generic
    # note via the defensive except (the shrunken shape-check branches).
    assert _slots_serialization_note(42) == (
        "[slots-broadcast] slot projection is not JSON-serializable (offender walk failed)"
    )
    assert _slots_serialization_note({"not": "a list"}, path="y") == (
        "[y] slot projection is not JSON-serializable (offender walk failed)"
    )
