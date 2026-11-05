"""``slots_diff`` frames rebuild the slot list a full frame carries.

A ``slot_diff.1`` socket gets each coalesced slot list as the rows that changed
since the last list it was sent (``dashboard/state_parts/slots_diff.py``). These
tests drive one state through a sequence of mutations, rebuild the list each diff
socket holds the way the client does (``website/src/hooks/websocket/slotList.ts``,
``applySlotsDiff``), and compare it, as JSON text, with the list a legacy socket of
the same audience received and with a fresh serialization. The rest pins when a
full frame is sent instead and which frames each socket gets.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.state_parts import slots_diff
from kiro_crew.dashboard.websocket_hub import SLOT_DIFF_WS_FLAG, SLOT_PATCH_WS_FLAG

_ENVELOPE_SKIP = ("type", "data", "slotsSeq")


class _WS(dict):
    """A socket double: the flag mapping ``api_ws`` writes, plus what it was sent."""

    def __init__(self, *, diff: bool, patch: bool = True) -> None:
        super().__init__({"_is_dashboard_user": True, SLOT_PATCH_WS_FLAG: patch})
        self[SLOT_DIFF_WS_FLAG] = diff
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    # Identity, as for a real socket: the owner set holds sockets.
    __hash__ = object.__hash__

    def __eq__(self, other: object) -> bool:
        return self is other


class _Client:
    """Rebuilds the slot list from the frames one socket received, as the SPA does."""

    def __init__(self) -> None:
        self.seq: int | None = None
        self.rows: list[dict[str, Any]] = []
        self.envelope: dict[str, Any] = {}
        self.resyncs = 0

    def apply(self, frame: dict[str, Any]) -> None:
        if frame["type"] == "slots":
            self.seq = frame.get("slotsSeq")
            self.rows = frame["data"]
            self.envelope = {k: v for k, v in frame.items() if k not in _ENVELOPE_SKIP}
            return
        if frame["type"] != "slots_diff":
            return
        data = frame["data"]
        if self.seq is None or data["base"] != self.seq:
            self.seq = None
            self.resyncs += 1
            return
        removed = set(data["removed"])
        changed = {row["key"]: row for row in data["rows"]}
        rows = []
        for row in self.rows:
            if row["key"] in removed:
                continue
            delta = changed.pop(row["key"], None)
            rows.append(row if delta is None else delta["row"])
        assert not changed, "a delta named a row the client does not hold"
        self.rows = rows
        self.envelope = {**self.envelope, **data["envelope"]}
        self.seq = data["seq"]


@pytest.fixture(name="state")
def _state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers._session_card_chips_snapshot", True
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.state._cached_check_status",
        lambda url: {"ci": "passed", "state": "OPEN"},
    )
    monkeypatch.setattr("kiro_crew.dashboard.state._repo_is_public", lambda url: "public" in url)
    monkeypatch.setattr("kiro_crew.dashboard.state._audit_public_status_grant", lambda url: None)
    monkeypatch.setattr("kiro_crew.dashboard.state._audit_public_status_denied", lambda url: None)
    sessions = MagicMock(count=0)
    sessions.get_provider.return_value = None
    state = DashboardState(
        sessions=sessions, crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    monkeypatch.setattr(state, "is_yolo_active", lambda: state._yolo)
    monkeypatch.setattr(state, "_spawn_ws_send", lambda ws, msg: ws.sent.append(json.loads(msg)))
    return state


def _connect(state: DashboardState, ws: _WS, *, owner: bool) -> _WS:
    state._ws_clients.append(ws)  # type: ignore[arg-type]
    if owner:
        state._owner_ws_clients.add(ws)  # type: ignore[arg-type]
    return ws


def _flush(state: DashboardState, sockets: list[_WS]) -> None:
    for ws in sockets:
        ws.sent.clear()
    state._do_slots_broadcast()


def _only_slot_frame(ws: _WS) -> dict[str, Any]:
    frames = [f for f in ws.sent if f["type"] in ("slots", "slots_diff")]
    assert len(frames) == 1, [f["type"] for f in ws.sent]
    return frames[0]


def _text(value: Any) -> str:
    return json.dumps(value)


def _seed(state: DashboardState) -> None:
    a = state.get_or_create_slot("chat-a")
    a.append("user", "open a PR please", "msg")
    a.append("assistant", "opened https://github.com/acme/public/pull/1", "msg")
    b = state.get_or_create_slot("chat-b")
    b.append("user", "which one?", "msg")
    b.append("assistant", "pick https://github.com/acme/private/pull/2", "msg")
    state.get_or_create_slot("chat-c").append("user", "hello", "msg")


def test_rebuilt_lists_equal_full_frames_across_mutations(state: DashboardState) -> None:
    _seed(state)
    owner_diff = _connect(state, _WS(diff=True), owner=True)
    owner_legacy = _connect(state, _WS(diff=False, patch=False), owner=True)
    user_diff = _connect(state, _WS(diff=True), owner=False)
    user_legacy = _connect(state, _WS(diff=False, patch=False), owner=False)
    sockets = [owner_diff, owner_legacy, user_diff, user_legacy]
    clients = {id(owner_diff): _Client(), id(user_diff): _Client()}
    pairs = [(owner_diff, owner_legacy), (user_diff, user_legacy)]

    a, b, c = (state._slots[k] for k in ("chat-a", "chat-b", "chat-c"))

    def retitle() -> None:
        c.title = "Renamed chat"
        c._titled = True

    steps: list[tuple[str, Any]] = [
        ("first flush", lambda: None),
        ("nothing changed", lambda: None),
        ("append", lambda: a.append("assistant", "and https://github.com/acme/x/issues/9", "msg")),
        ("in-place edit", lambda: b.messages[-1].update(content="now [OPTIONS: left | right]")),
        ("title", retitle),
        ("running", lambda: setattr(a, "_task", MagicMock(done=lambda: False))),
        ("question", lambda: setattr(c, "_question_pending", True)),
        ("stopped", lambda: setattr(a, "_task", None)),
        ("yolo", lambda: setattr(state, "_yolo", True)),
        ("folder generation", lambda: setattr(state, "_folders_generation", 7)),
        ("pin", lambda: (setattr(b, "pinned", True), state.bump_pinned_order_rev())),
        ("slot removed", lambda: state._slots.pop("chat-b")),
        ("new slot", lambda: state.get_or_create_slot("chat-d").append("user", "hi", "msg")),
        ("after new slot", lambda: c.append("assistant", "back", "msg")),
    ]
    kinds: set[str] = set()
    applied = {id(ws): 0 for ws in sockets}
    for label, step in steps:
        step()
        # A step can broadcast on its own (a new slot pushes at once), so every
        # frame since the last check is applied, in order.
        state._do_slots_broadcast()
        bare, user_view, owner_view = state.serialize_slot_views(owner=True)
        fresh = {id(owner_diff): owner_view, id(user_diff): user_view}
        for diff_ws, legacy_ws in pairs:
            client = clients[id(diff_ws)]
            for frame in diff_ws.sent[applied[id(diff_ws)] :]:
                if frame["type"] in ("slots", "slots_diff"):
                    kinds.add(frame["type"])
                client.apply(frame)
            applied[id(diff_ws)] = len(diff_ws.sent)
            legacy = [f for f in legacy_ws.sent if f["type"] == "slots"][-1]
            legacy_envelope = {k: v for k, v in legacy.items() if k not in _ENVELOPE_SKIP}
            assert _text(client.rows) == _text(legacy["data"]), label
            assert _text(client.rows) == _text(fresh[id(diff_ws)]), label
            assert _text(client.envelope) == _text(legacy_envelope), label
    # Both shapes were exercised, so the equality is not only full-frame equality.
    assert kinds == {"slots", "slots_diff"}
    assert all(client.resyncs == 0 for client in clients.values())


def _steady(state: DashboardState) -> _WS:
    _seed(state)
    ws = _connect(state, _WS(diff=True), owner=True)
    _flush(state, [ws])
    assert _only_slot_frame(ws)["type"] == "slots"
    return ws


def test_a_routine_change_sends_only_the_changed_row(state: DashboardState) -> None:
    ws = _steady(state)
    state._slots["chat-a"].append("assistant", "one more", "msg")
    _flush(state, [ws])
    frame = _only_slot_frame(ws)
    assert frame["type"] == "slots_diff"
    assert [row["key"] for row in frame["data"]["rows"]] == ["chat-a"]
    assert frame["data"]["removed"] == [] and frame["data"]["envelope"] == {}


def test_an_unchanged_list_still_sends_an_empty_delta(state: DashboardState) -> None:
    ws = _steady(state)
    _flush(state, [ws])
    frame = _only_slot_frame(ws)
    assert frame["type"] == "slots_diff"
    assert frame["data"]["rows"] == [] and frame["data"]["removed"] == []


def test_a_new_socket_gets_the_full_list(state: DashboardState) -> None:
    ws = _steady(state)
    late = _connect(state, _WS(diff=True), owner=True)
    _flush(state, [ws, late])
    assert _only_slot_frame(ws)["type"] == "slots_diff"
    assert _only_slot_frame(late)["type"] == "slots"
    _flush(state, [ws, late])
    assert _only_slot_frame(late)["type"] == "slots_diff"


def test_a_resync_is_answered_from_the_held_base_to_that_socket_only(
    state: DashboardState, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _steady(state)
    other = _connect(state, _WS(diff=True), owner=True)
    legacy = _connect(state, _WS(diff=False, patch=False), owner=True)
    _flush(state, [ws, other, legacy])
    base = state._slot_diff_bases[slots_diff.OWNER_VIEW]  # type: ignore[index]
    held_list = _only_slot_frame(legacy)["data"]
    for sock in (ws, other, legacy):
        sock.sent.clear()
    pushes: list[bool] = []
    monkeypatch.setattr(state, "push_slots_update", lambda **_kw: pushes.append(True))
    monkeypatch.setattr(
        state, "serialize_slot_views", lambda **_kw: pytest.fail("a resync re-serialized")
    )
    slots_diff.answer_resync(state, ws)  # type: ignore[arg-type]
    frame = _only_slot_frame(ws)
    assert frame["type"] == "slots" and frame["slotsSeq"] == base.seq
    assert _text(frame["data"]) == _text(held_list)
    assert other.sent == [] and legacy.sent == [] and pushes == []
    assert ws[slots_diff.SLOT_DIFF_SEQ_KEY] == (slots_diff.OWNER_VIEW, base.seq)


def test_a_resync_after_a_patch_rebuilds_instead_of_replaying_the_base(
    state: DashboardState,
) -> None:
    ws = _steady(state)
    slot = state._slots["chat-c"]
    slot.title = "Renamed chat"
    slot._titled = True
    ws.sent.clear()
    state.push_slot_patch("chat-c", ("title",))
    [patch] = [f for f in ws.sent if f["type"] == "slot_patch"]
    assert patch["data"]["slots"][0]["title"] == "Renamed chat"
    ws.sent.clear()
    # No ordinary broadcast in between: the held base still has the old title.
    slots_diff.answer_resync(state, ws)  # type: ignore[arg-type]
    frame = _only_slot_frame(ws)
    assert frame["type"] == "slots"
    [row] = [r for r in frame["data"] if r["key"] == "chat-c"]
    assert row["title"] == "Renamed chat"
    # The rebuilt list is the new base, so the next resync can replay it again.
    assert not state._slot_diff_bases[slots_diff.OWNER_VIEW].patched  # type: ignore[index]


def test_a_resync_with_no_base_falls_back_to_a_push(
    state: DashboardState, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _connect(state, _WS(diff=True), owner=True)
    pushes: list[bool] = []
    monkeypatch.setattr(state, "push_slots_update", lambda **_kw: pushes.append(True))
    slots_diff.answer_resync(state, ws)  # type: ignore[arg-type]
    assert pushes == [True] and ws.sent == []


def test_a_dropped_frame_ends_in_a_resync_and_the_right_list(state: DashboardState) -> None:
    _seed(state)
    ws = _connect(state, _WS(diff=True), owner=True)
    legacy = _connect(state, _WS(diff=False, patch=False), owner=True)
    client = _Client()
    _flush(state, [ws, legacy])
    client.apply(_only_slot_frame(ws))
    state._slots["chat-a"].append("assistant", "lost", "msg")
    _flush(state, [ws, legacy])
    assert _only_slot_frame(ws)["type"] == "slots_diff"  # never reaches the client
    state._slots["chat-b"].append("assistant", "after", "msg")
    _flush(state, [ws, legacy])
    client.apply(_only_slot_frame(ws))
    assert client.resyncs == 1 and client.seq is None
    ws.sent.clear()
    slots_diff.answer_resync(state, ws)  # type: ignore[arg-type]
    client.apply(_only_slot_frame(ws))
    assert _text(client.rows) == _text(_only_slot_frame(legacy)["data"])
    state._slots["chat-c"].append("assistant", "next", "msg")
    _flush(state, [ws, legacy])
    client.apply(_only_slot_frame(ws))
    assert client.resyncs == 1
    assert _text(client.rows) == _text(_only_slot_frame(legacy)["data"])


def test_a_delta_over_the_size_cap_is_sent_in_full(
    state: DashboardState, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _steady(state)
    state._slots["chat-a"].append("assistant", "one more", "msg")
    monkeypatch.setattr(slots_diff, "_MAX_DIFF_SHARE", 0.0001)
    _flush(state, [ws])
    assert _only_slot_frame(ws)["type"] == "slots"


def test_a_legacy_only_push_reaches_no_diff_socket(state: DashboardState) -> None:
    ws = _steady(state)
    legacy = _connect(state, _WS(diff=False, patch=False), owner=True)
    ws.sent.clear()
    # No serving loop, so the push broadcasts at once instead of arming a timer.
    state.push_slots_update(legacy_only=True)
    assert ws.sent == []
    assert _only_slot_frame(legacy)["type"] == "slots"


def test_the_generic_frame_is_skipped_only_without_its_audience(
    state: DashboardState, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _steady(state)
    notes: list[dict[str, Any]] = []
    real = state._broadcast
    monkeypatch.setattr(
        state, "_broadcast", lambda note: (notes.append(note), real(note))  # type: ignore[func-returns-value]
    )
    _flush(state, [ws])
    assert not [n for n in notes if n.get("_type") == "slots"]
    user = _connect(state, _WS(diff=False), owner=False)
    _flush(state, [ws, user])
    assert [n for n in notes if n.get("_type") == "slots"]
    assert _only_slot_frame(user)["type"] == "slots"
    assert _only_slot_frame(ws)["type"] == "slots_diff"


def test_bases_are_dropped_when_no_diff_socket_is_open(state: DashboardState) -> None:
    ws = _steady(state)
    assert state._slot_diff_bases
    ws.closed = True
    _flush(state, [ws])
    assert state._slot_diff_bases is None


def test_one_broadcast_gives_every_slots_frame_the_same_envelope_keys(
    state: DashboardState,
) -> None:
    _seed(state)
    state._tags_generation = 3
    owner_legacy = _connect(state, _WS(diff=False, patch=False), owner=True)
    owner_diff = _connect(state, _WS(diff=True), owner=True)
    user_legacy = _connect(state, _WS(diff=False, patch=False), owner=False)
    _flush(state, [owner_legacy, owner_diff, user_legacy])
    frames = [_only_slot_frame(ws) for ws in (owner_legacy, owner_diff, user_legacy)]
    assert "slotsSeq" in frames[1]
    key_sets = [set(frame) - set(_ENVELOPE_SKIP) for frame in frames]
    assert key_sets[0] == key_sets[1] == key_sets[2]
    assert "tagsGeneration" in key_sets[0]
    assert all(frame["tagsGeneration"] == 3 for frame in frames)


def test_frames_built_from_row_texts_match_a_fresh_dump(
    state: DashboardState, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.state_parts.slots_frame import _slots_ws_frame

    _seed(state)
    raw: list[tuple[Any, str]] = []
    monkeypatch.setattr(state, "_spawn_ws_send", lambda ws, msg: raw.append((ws, msg)))
    owner_legacy = _connect(state, _WS(diff=False, patch=False), owner=True)
    user_legacy = _connect(state, _WS(diff=False, patch=False), owner=False)
    state._do_slots_broadcast()
    sent = {id(ws): msg for ws, msg in raw}
    envelope = {
        k: v for k, v in json.loads(sent[id(owner_legacy)]).items() if k not in ("type", "data")
    }
    _bare, user_view, owner_view = state.serialize_slot_views(owner=True)

    def fresh(rows: Any) -> str:
        return _slots_ws_frame(
            rows,
            yolo=envelope["yolo"],
            channel_trusted=envelope["channelTrusted"],
            gitlab_hosts_gen=envelope["gitlabHostsGeneration"],
            folders=envelope["folders"],
            folders_gen=envelope["foldersGeneration"],
            governance_gen=envelope["governanceGeneration"],
            tags_gen=envelope["tagsGeneration"],
        )

    assert sent[id(owner_legacy)] == fresh(owner_view)
    assert sent[id(user_legacy)] == fresh(user_view)


def test_an_app_socket_with_the_flag_is_not_a_diff_socket(state: DashboardState) -> None:
    app_ws = _WS(diff=True)
    app_ws["_is_dashboard_user"] = False
    _connect(state, app_ws, owner=False)
    user_ws = _connect(state, _WS(diff=True), owner=False)
    assert slots_diff.diff_sockets(state) == ([], [user_ws])


def test_a_user_view_diff_send_passes_the_chokepoint(
    state: DashboardState, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard import ws_event_scope

    granted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ws_event_scope, "_audit_allow", lambda auditee, event: granted.append((auditee, event))
    )
    _seed(state)
    user_ws = _connect(state, _WS(diff=True), owner=False)
    _flush(state, [user_ws])
    assert _only_slot_frame(user_ws)["type"] == "slots"
    assert (ws_event_scope.DASHBOARD_USER_AUDITEE, "slots") in granted


def _base(keys: list[str], envelope: dict[str, str]) -> slots_diff.DiffBase:
    return slots_diff.DiffBase(
        seq=1, keys=keys, rows={k: _text({"key": k}) for k in keys}, envelope=envelope
    )


def test_delta_full_frame_reasons() -> None:
    env = {"yolo": "false"}
    base = _base(["a", "b", "c"], env)
    texts = [_text({"key": k}) for k in ("a", "b", "c")]
    reordered = [_text({"key": k}) for k in ("b", "a", "c")]
    assert slots_diff._delta(base, ["b", "a", "c"], reordered, env) == "rows_added_or_moved"
    added = texts + [_text({"key": "d"})]
    assert slots_diff._delta(base, ["a", "b", "c", "d"], added, env) == "rows_added_or_moved"
    assert (
        slots_diff._delta(base, ["a", "b", "c"], texts, {**env, "slotsHeldKeys": "[]"})
        == "envelope_keys"
    )
    rows, removed, moved = slots_diff._delta(  # type: ignore[misc]
        base, ["a", "c"], [texts[0], _text({"key": "c", "x": 1})], {"yolo": "true"}
    )
    assert rows == [("c", _text({"key": "c", "x": 1}))]
    assert removed == ["b"] and moved == [("yolo", "true")]


@pytest.mark.parametrize("flip", ["held_keys", "rebuilding"])
def test_a_hold_flip_sends_a_full_frame_counted_as_envelope_keys(
    state: DashboardState, monkeypatch: pytest.MonkeyPatch, flip: str
) -> None:
    ws = _steady(state)
    before = slots_diff.FULL_FRAME_REASONS["envelope_keys"]
    if flip == "held_keys":
        monkeypatch.setattr(state, "slot_list_held_keys", lambda: ["chat-z"])
    else:
        monkeypatch.setattr(type(state), "slot_list_rebuilding", property(lambda _self: True))
    _flush(state, [ws])
    frame = _only_slot_frame(ws)
    assert frame["type"] == "slots"
    assert slots_diff.FULL_FRAME_REASONS["envelope_keys"] == before + 1


def test_overlapping_passes_mint_distinct_seqs(state: DashboardState) -> None:
    import asyncio
    import threading

    _seed(state)
    _connect(state, _WS(diff=True), owner=True)
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    # Thread.start() returns before run_forever marks the loop running; a caller
    # that ran first would see is_running() False and broadcast on its own thread.
    ready = threading.Event()
    loop.call_soon_threadsafe(ready.set)
    assert ready.wait(timeout=10)
    sends: list[tuple[int, dict[str, Any]]] = []
    state._spawn_ws_send = lambda w, msg: sends.append(  # type: ignore[method-assign]
        (threading.get_ident(), json.loads(msg))
    )
    try:
        state.bind_serving_loop(loop)
        callers = [threading.Thread(target=state._do_slots_broadcast) for _ in range(4)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=10)
        done = threading.Event()
        loop.call_soon_threadsafe(done.set)
        assert done.wait(timeout=10)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=10)
        loop.close()
    assert {ident for ident, _ in sends} == {runner.ident}
    seqs = [f["slotsSeq"] if f["type"] == "slots" else f["data"]["seq"] for _, f in sends]
    assert len(seqs) == 4 and len(set(seqs)) == 4
    base = state._slot_diff_bases[slots_diff.OWNER_VIEW]  # type: ignore[index]
    assert base.seq == seqs[-1]
