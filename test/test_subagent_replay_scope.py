"""The Subagents panel's durable replay does work in proportion to what a socket views.

Before this, every ``subscribe_subagents`` (sent on every dashboard connect and
reconnect) folded every crew-log unit of every open slot, twice, inline in the
socket's receive loop. These tests bound that work on real crew logs:

* only the slots a socket names or focuses are replayed;
* a unit with no ``subagent/*`` entry is never folded, and is read whole at most
  once per process;
* an unchanged unit is not folded again on a reconnect;
* one socket never has two replays in flight, and two readers of one unit share
  one fold.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.dashboard import subagent_replay_scope as scope
from kiro_crew.dashboard import ws as dashboard_ws
from kiro_crew.testing.wait import await_until, finished, wait_until

GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    scope.UNIT_INDEX.clear()
    scope.UNIT_ROWS.clear()
    yield
    scope.UNIT_INDEX.clear()
    scope.UNIT_ROWS.clear()


def _unit(unit_id: str, slot: str, *, children: int = 0, turns: int = 20) -> CrewLog:
    handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": slot,
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
            "class": {"memory": "persistent"},
        },
        src=GATEWAY,
    )
    for n in range(turns):
        handle.append("turn/started", {"turn": n + 1, "actor": "user", "depth": 0}, src=GATEWAY)
    for n in range(children):
        agent_id = f"{unit_id}-child-{n}"
        handle.append("subagent/spawned", {"agent_id": agent_id, "task": "t"}, src=GATEWAY)
        handle.append("subagent/completed", {"agent_id": agent_id, "ms": 5}, src=GATEWAY)
    return handle


def _home(n_slots: int, units_per_slot: int, holding: set[tuple[int, int]]) -> list[str]:
    """``n_slots`` slots of ``units_per_slot`` units; only *holding* carry children."""
    slots = [f"chat-{i}" for i in range(n_slots)]
    for i, slot in enumerate(slots):
        for j in range(units_per_slot):
            _unit(f"acp-{i}-{j}", slot, children=2 if (i, j) in holding else 0)
    return slots


class _Counter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.units: list[str] = []

    def wrap(self, real):
        def counted(unit_id, *args, **kwargs):
            with self.lock:
                self.units.append(unit_id)
            return real(unit_id, *args, **kwargs)

        return counted


@pytest.fixture
def folds(monkeypatch) -> _Counter:
    from kiro_crew.crew_log import projection

    counter = _Counter()
    monkeypatch.setattr(projection, "fold_session", counter.wrap(projection.fold_session))
    return counter


def _replay(slots: list[str]) -> list[dict]:
    """What the handler's worker does for *slots*: resolve, skip, correct, fold."""
    pairs = scope.UNIT_INDEX.holding_subagents(dashboard_ws.crew_log_panel_units(slots))
    already = dashboard_ws.backfill_legacy_panel_dismissals(pairs)
    records, _overflow = dashboard_ws.read_fold_subagent_records(
        pairs,
        keep=50,
        max_age_secs=86_400.0,
        exclude_ids=already,
        admit=lambda _record: True,
    )
    return records


class TestWorkScalesWithViewedSlotsAndUnitsHoldingChildren:
    N_SLOTS, UNITS = 12, 6
    HOLDING = {(0, 1), (0, 4), (7, 2)}

    def test_only_units_with_children_in_the_viewed_slot_are_folded(self, folds):
        slots = _home(self.N_SLOTS, self.UNITS, self.HOLDING)
        records = _replay([slots[0]])
        assert sorted(r["unit"] for r in records) == ["acp-0-1"] * 2 + ["acp-0-4"] * 2
        # One fold per unit holding children in the viewed slot (the class and the
        # subagents projection share it through the cache), and nothing for the 70
        # other units in the home.
        assert sorted(set(folds.units)) == ["acp-0-1", "acp-0-4"]
        # Each unit of the viewed slot was read whole once to learn it holds no
        # child; no unit of any other slot was read at all.
        assert scope.UNIT_INDEX.scans == self.UNITS

    def test_a_reconnect_over_unchanged_logs_folds_and_reads_nothing_again(self, folds):
        slots = _home(self.N_SLOTS, self.UNITS, self.HOLDING)
        first = _replay([slots[0], slots[7]])
        folded, scans = len(folds.units), scope.UNIT_INDEX.scans
        for _ in range(5):
            assert _replay([slots[0], slots[7]]) == first
        assert len(folds.units) == folded
        assert scope.UNIT_INDEX.scans == scans

    def test_an_append_refolds_only_that_unit_and_reads_only_the_new_bytes(self, folds):
        slots = _home(2, 3, {(0, 0)})
        _replay([slots[0]])
        before_folds, before_scans = list(folds.units), scope.UNIT_INDEX.scans
        handle = CrewLog.open(lg.KIND_SESSION, "acp-0-2")
        handle.append("subagent/spawned", {"agent_id": "late", "task": "t"}, src=GATEWAY)
        handle.append("subagent/completed", {"agent_id": "late", "ms": 1}, src=GATEWAY)
        records = _replay([slots[0]])
        assert "late" in {r["id"] for r in records}
        # The append was found by reading the tail, not the whole unit again.
        assert scope.UNIT_INDEX.scans == before_scans
        # Its class and subagents folds ran again; no other unit's did.
        assert set(folds.units[len(before_folds) :]) == {"acp-0-2"}

    def test_the_cost_does_not_grow_with_slots_nobody_views(self, folds):
        small = _home(2, 4, {(0, 0)})
        _replay([small[0]])
        small_cost = (len(folds.units), scope.UNIT_INDEX.scans)
        scope.UNIT_INDEX.clear()
        scope.UNIT_ROWS.clear()
        folds.units.clear()
        # 20x the slots in the same home: the viewed slot's cost is unchanged.
        for i in range(2, 40):
            for j in range(4):
                _unit(f"acp-{i}-{j}", f"chat-{i}", children=2 if j == 0 else 0)
        _replay([small[0]])
        assert (len(folds.units), scope.UNIT_INDEX.scans) == small_cost


class TestTheIndexIsConservative:
    def test_a_unit_whose_front_was_trimmed_has_no_drawable_row(self, tmp_path):
        """The class read refuses such a log, so no row of it is ever drawn."""
        from kiro_crew.crew_log.store_paths import crew_log_dir

        _unit("acp-t", "chat-1", children=1)
        directory = crew_log_dir(lg.KIND_SESSION, "acp-t")
        (directory / "log.jsonl").rename(directory / "log.2.jsonl")
        assert scope.UNIT_INDEX.peek("acp-t") is False
        assert dashboard_ws.read_fold_subagent_records(
            [("chat-1", "acp-t")],
            keep=5,
            max_age_secs=0,
            exclude_ids=(),
            admit=lambda _r: True,
        ) == ([], 0)

    def test_an_unreadable_unit_is_kept_for_the_fold_to_decide(self, monkeypatch):
        def _boom(_unit_id):
            raise OSError("unreadable")

        monkeypatch.setattr(scope.UNIT_INDEX, "peek", _boom)
        assert scope.UNIT_INDEX.holding_subagents([("chat-1", "acp-x")]) == [("chat-1", "acp-x")]

    def test_a_needle_split_across_read_chunks_is_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(scope, "_READ_CHUNK", 7)
        path = tmp_path / "seg"
        path.write_bytes(b"x" * 13 + b'"type":"subagent/spawned"' + b"y" * 5)
        assert scope._contains_needle(path, 0, path.stat().st_size)
        path.write_bytes(b"x" * 40)
        assert not scope._contains_needle(path, 0, path.stat().st_size)


class _CountingEvent(threading.Event):
    """A ``threading.Event`` that counts the threads parked in ``wait``."""

    parked = 0
    lock = threading.Lock()

    def wait(self, timeout=None):
        with _CountingEvent.lock:
            _CountingEvent.parked += 1
        return super().wait(timeout)


def _held(monkeypatch, real, held_name: str):
    """Wrap *real* so its first call blocks until the returned event is set."""
    release = threading.Event()
    entered = threading.Event()
    calls: list = []

    def blocking(*args, **kwargs):
        calls.append(args[0] if args else None)
        if len(calls) == 1:
            entered.set()
            release.wait(30)
        return real(*args, **kwargs)

    _CountingEvent.parked = 0
    monkeypatch.setattr(
        scope, "threading", SimpleNamespace(Lock=threading.Lock, Event=_CountingEvent)
    )
    return blocking, release, entered, calls


def _race(target, entered, release, others: int = 2) -> list:
    """Run *target* in 1 + *others* threads; the first holds until the rest are parked."""
    results: list = []
    first = threading.Thread(target=lambda: results.append(target()))
    first.start()
    wait_until(entered.is_set)
    rest = [threading.Thread(target=lambda: results.append(target())) for _ in range(others)]
    for t in rest:
        t.start()
    wait_until(lambda: _CountingEvent.parked >= others)
    release.set()
    for t in [first, *rest]:
        t.join(timeout=30)
    return results


class TestSharedWork:
    def test_two_readers_of_one_unit_share_one_fold(self, monkeypatch):
        from kiro_crew.crew_log import projection

        _unit("acp-s", "chat-1", children=1)
        blocking, release, entered, calls = _held(
            monkeypatch, projection.fold_session, "fold_session"
        )
        monkeypatch.setattr(projection, "fold_session", blocking)
        results = _race(lambda: scope.UNIT_ROWS.rows("acp-s"), entered, release)
        assert len(results) == 3
        assert scope.UNIT_ROWS.folds == 1
        assert set(calls) == {"acp-s"}

    def test_a_recreated_log_is_not_served_from_the_cache(self):
        """The cache uses the crew log's own identity, which a recreation changes."""
        import shutil

        from kiro_crew.crew_log.store_paths import crew_log_dir

        _unit("acp-x", "chat-1", children=1)
        first = scope.UNIT_ROWS.rows("acp-x")[1]
        assert set(first) == {"acp-x-child-0"}
        from kiro_crew.crew_log import eager

        with eager.paused():
            shutil.rmtree(crew_log_dir(lg.KIND_SESSION, "acp-x"))
        handle = _unit("acp-x", "chat-1")
        handle.append("subagent/spawned", {"agent_id": "other", "task": "t"}, src=GATEWAY)
        handle.append("subagent/completed", {"agent_id": "other", "ms": 1}, src=GATEWAY)
        assert set(scope.UNIT_ROWS.rows("acp-x")[1]) == {"other"}

    def test_a_unit_with_no_readable_owner_is_not_refolded_on_every_connect(self, monkeypatch):
        """No class record: skipped as unowned, and the answer is reused for a while."""
        handle = CrewLog.create(
            lg.KIND_SESSION, "acp-nc", owner="raymond", agent="kirocrew", slot="chat-1"
        )
        handle.append("subagent/spawned", {"agent_id": "c1", "task": "t"}, src=GATEWAY)
        handle.append("subagent/completed", {"agent_id": "c1", "ms": 1}, src=GATEWAY)
        assert scope.UNIT_ROWS.rows("acp-nc")[0] is None
        assert scope.UNIT_ROWS.rows("acp-nc")[0] is None
        assert scope.UNIT_ROWS.folds == 1
        # Past the retry window the class is read again, once.
        monkeypatch.setattr(scope, "UNOWNED_RETRY_SECS", 0.0)
        scope.UNIT_ROWS.rows("acp-nc")
        assert scope.UNIT_ROWS.folds == 2

    def test_two_readers_of_one_cold_unit_share_one_whole_read(self, monkeypatch):
        _unit("acp-c", "chat-1")
        blocking, release, entered, _calls = _held(
            monkeypatch, scope._contains_needle, "_contains_needle"
        )
        monkeypatch.setattr(scope, "_contains_needle", blocking)
        _race(lambda: scope.UNIT_INDEX.scan("acp-c"), entered, release)
        assert scope.UNIT_INDEX.scans == 1

    def test_a_rewrite_in_place_past_the_old_size_is_read_again(self):
        """A tail repair truncates, and the next append regrows the same inode."""
        from kiro_crew.crew_log.store_paths import crew_log_dir

        _unit("acp-r", "chat-1")
        assert scope.UNIT_INDEX.scan("acp-r") is False
        segment = crew_log_dir(lg.KIND_SESSION, "acp-r") / "log.jsonl"
        data = segment.read_bytes()
        cut = data.rindex(b"\n", 0, len(data) - 1) + 1
        with open(segment, "r+b") as handle:
            handle.truncate(cut)
            handle.seek(cut)
            handle.write(b'{"type":"subagent/spawned"}' + b" " * (len(data) - cut) + b"\n")
        assert scope.UNIT_INDEX.peek("acp-r") is None
        assert scope.UNIT_INDEX.scan("acp-r") is True

    def test_a_rewrite_in_place_to_the_same_size_is_read_again(self):
        """A rollback can regrow a truncated segment to exactly its old size."""
        from kiro_crew.crew_log.store_paths import crew_log_dir

        _unit("acp-e", "chat-1")
        assert scope.UNIT_INDEX.scan("acp-e") is False
        segment = crew_log_dir(lg.KIND_SESSION, "acp-e") / "log.jsonl"
        data = segment.read_bytes()
        cut = data.rindex(b"\n", 0, len(data) - 1) + 1
        line = b'{"type":"subagent/spawned"}'
        tail = line + b" " * (len(data) - cut - len(line) - 1) + b"\n"
        assert len(tail) == len(data) - cut
        with open(segment, "r+b") as handle:
            handle.seek(cut)
            handle.write(tail)
        assert segment.stat().st_size == len(data)
        assert scope.UNIT_INDEX.peek("acp-e") is None
        assert scope.UNIT_INDEX.scan("acp-e") is True


class TestTheQueueRunsOneReplayPerSocketAtATime:
    @pytest.mark.asyncio
    async def test_requests_during_a_replay_queue_behind_it_and_none_repeats(self):
        active = peak = 0
        batches: list[list[str]] = []
        release = asyncio.Event()

        async def run(slots: list[str]) -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            batches.append(slots)
            await release.wait()
            active -= 1

        queue = scope.SubagentReplayQueue(run)
        queue.request(["chat-1"])
        await asyncio.sleep(0)
        queue.request(["chat-1", "chat-2"])
        queue.request(["chat-3"])
        await asyncio.sleep(0)
        assert batches == [["chat-1"]]
        release.set()
        await finished(queue.wait_idle(), "the replay queue drains")
        assert batches == [["chat-1"], ["chat-2", "chat-3"]]
        assert peak == 1
        queue.request(["chat-2"])
        assert not queue.running

    @pytest.mark.asyncio
    async def test_after_unsubscribe_nothing_new_is_taken_until_a_new_subscribe(self):
        batches: list[list[str]] = []

        async def run(slots: list[str]) -> None:
            batches.append(slots)

        queue = scope.SubagentReplayQueue(run)
        queue.clear()
        queue.request(["chat-1"])
        assert not queue.running
        queue.reset(run)
        queue.request(["chat-1"])
        await finished(queue.wait_idle(), "the replay queue drains")
        assert batches == [["chat-1"]]


class TestTheQueueKeepsOnlyBoundedRealSlots:
    @pytest.mark.asyncio
    async def test_names_the_socket_may_not_replay_are_never_kept(self):
        batches: list[list[str]] = []

        async def run(slots: list[str]) -> None:
            batches.append(slots)

        live = {"chat-1", "chat-2"}
        queue = scope.SubagentReplayQueue(run, admit=lambda s: s in live)
        for n in range(50):
            queue.request([f"bogus-{n}-{i}" for i in range(256)])
        queue.request(["chat-1", "chat-2"])
        await finished(queue.wait_idle(), "the replay queue drains")
        assert batches == [["chat-1", "chat-2"]]
        assert len(queue._done) == 2

    @pytest.mark.asyncio
    async def test_a_subscription_holds_at_most_the_cap_and_counts_the_rest(self, monkeypatch):
        async def run(slots: list[str]) -> None:
            return None

        monkeypatch.setattr(scope, "MAX_SUBSCRIPTION_SLOTS", 3)
        queue = scope.SubagentReplayQueue(run)
        queue.request(["a", "b", "c", "d", "e"])
        await finished(queue.wait_idle(), "the replay queue drains")
        assert len(queue._done) == 3
        assert queue.dropped == 2

    @pytest.mark.asyncio
    async def test_a_forgotten_slot_can_be_asked_for_again(self):
        batches: list[list[str]] = []

        async def run(slots: list[str]) -> None:
            batches.append(slots)

        queue = scope.SubagentReplayQueue(run)
        queue.request(["chat-1"])
        await finished(queue.wait_idle(), "the replay queue drains")
        queue.forget(["chat-1"])
        queue.request(["chat-1"])
        await finished(queue.wait_idle(), "the replay queue drains")
        assert batches == [["chat-1"], ["chat-1"]]


def test_requested_slots_takes_only_bounded_strings():
    assert scope.requested_slots(None) is None
    assert scope.requested_slots("chat-1") is None
    assert scope.requested_slots(["a", "", 3, "a", "b", "x" * 600]) == ["a", "b"]
    assert len(scope.requested_slots([f"s{i}" for i in range(1000)]) or []) == (
        scope.MAX_REQUEST_SLOTS
    )


def _drawn(sent: list[dict]) -> set[str]:
    """The card ids a socket was sent, single frames and batches alike."""
    ids = {f["data"]["id"] for f in sent if f.get("type") == "subagent_done"}
    for frame in sent:
        if frame.get("type") == "subagent_snapshot_batch":
            ids |= {i["data"]["id"] for i in frame["data"]["items"]}
    return ids


class TestTheHandlerReplaysOnlyWhatTheSocketViews:
    """Through ``api_ws`` itself: subscribe, focus and replay frames on one socket."""

    def _drive(
        self,
        monkeypatch,
        frames: list[dict],
        slots: list[str],
        expect_units: set[str],
        expect_ids: set[str] | None = None,
        app: str = "",
        manager=None,
    ):
        replayed: list[list[str]] = []
        active = {"now": 0, "peak": 0}
        lock = threading.Lock()
        done = threading.Event()
        wake: dict = {}
        if expect_ids is None:
            expect_ids = {f"{u}-child-{n}" for u in expect_units for n in range(2)}
        real_read = dashboard_ws.read_fold_subagent_records

        def recording_read(pairs, **kwargs):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            try:
                return real_read(pairs, **kwargs)
            finally:
                with lock:
                    active["now"] -= 1
                    replayed.append(sorted({unit for _slot, unit in pairs}))

        monkeypatch.setattr(dashboard_ws, "read_fold_subagent_records", recording_read)
        monkeypatch.setattr(dashboard_ws, "persisted_precap_readings", lambda *a, **k: (None, None))
        monkeypatch.setattr(dashboard_ws, "persisted_precap_denial_reason", lambda *a: "")
        monkeypatch.setattr(dashboard_ws, "persisted_replay_denial_reason", lambda *a: "")
        monkeypatch.setattr(
            dashboard_ws, "_handle_slot_focused", lambda state, slot, prev, owner: prev
        )
        monkeypatch.setattr(dashboard_ws, "handle_focus_frame", lambda *a, **k: None)
        monkeypatch.setattr(dashboard_ws, "note_foreground", lambda *a, **k: None)
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        from kiro_crew.dashboard.handlers import source_providers

        monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda request: not app)

        monkeypatch.setattr(source_providers, "schedule_check_refresh", MagicMock())
        monkeypatch.setattr(source_providers, "schedule_visibility_refresh", MagicMock())

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.side_effect = lambda **_kw: []
        state._yolo = True
        state._folders = []
        state.folders_generation.return_value = 1
        state._slots = {name: MagicMock() for name in slots}
        state.native_subagent_snapshots.return_value = []
        state.subagents = manager
        state._ws_client_allowed.return_value = True

        class Msg:
            def __init__(self, payload: dict) -> None:
                from aiohttp import WSMsgType

                self.type = WSMsgType.TEXT
                self.data = json.dumps(payload)

        class FakeWS:
            def __init__(self) -> None:
                self.closed = True
                self.sent: list[dict] = []
                self._flags: dict = {}
                self._queue = list(frames)

            def __setitem__(self, key, value):
                self._flags[key] = value

            def __getitem__(self, key):
                return self._flags[key]

            def get(self, key, default=None):
                return self._flags.get(key, default)

            async def prepare(self, request):
                return None

            async def send_json(self, payload):
                self.sent.append(payload)
                # Done once every expected card has been sent to the socket.
                if _drawn(self.sent) >= expect_ids:
                    done.set()
                    if wake:
                        wake["event"].set()

            async def send_str(self, payload):
                self.sent.append(json.loads(payload))

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._queue:
                    return Msg(self._queue.pop(0))
                # The socket stays open until the replays the frames asked for
                # have run, as a real client's would.
                wake["event"] = asyncio.Event()
                if done.is_set():
                    wake["event"].set()
                await await_until(wake["event"], what="the expected cards were sent")
                raise StopAsyncIteration

        fake = FakeWS()
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake)

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"app": app, "user": "U_OWNER"})
                self["is_dashboard_user"] = not app
                self.app = {"state": state}

        asyncio.run(dashboard_ws.api_ws(Request()))  # type: ignore[arg-type]
        return replayed, active["peak"], fake

    def test_subscribe_focus_and_replay_each_fold_only_their_own_slots(self, monkeypatch, folds):
        slots = _home(10, 4, {(1, 0), (2, 3), (5, 1), (8, 2)})
        frames = [
            {"type": "subscribe_subagents", "slots": [slots[1]]},
            {"type": "slot_focused", "slot": slots[2]},
            {"type": "slot_focused", "slot": slots[1]},  # already replayed
            {"type": "subagent_replay", "slots": [slots[5], "not-a-slot"]},
        ]
        replayed, peak, fake = self._drive(
            monkeypatch, frames, slots, expect_units={"acp-1-0", "acp-2-3", "acp-5-1"}
        )
        units = sorted(u for batch in replayed for u in batch)
        # Slot 8 holds children too, but no frame named it.
        assert units == ["acp-1-0", "acp-2-3", "acp-5-1"]
        assert peak == 1, "two replays ran at once on one socket"
        assert sorted(set(folds.units)) == ["acp-1-0", "acp-2-3", "acp-5-1"]
        assert _drawn(fake.sent) == {
            f"{u}-child-{n}" for u in ("acp-1-0", "acp-2-3", "acp-5-1") for n in range(2)
        }

    def test_a_subscribe_naming_no_slots_folds_nothing_until_a_focus(self, monkeypatch, folds):
        slots = _home(6, 3, {(0, 0), (3, 1)})
        frames = [{"type": "subscribe_subagents"}, {"type": "slot_focused", "slot": slots[3]}]
        replayed, _peak, _fake = self._drive(monkeypatch, frames, slots, expect_units={"acp-3-1"})
        assert replayed == [["acp-3-1"]]
        assert sorted(set(folds.units)) == ["acp-3-1"]

    def test_an_app_naming_a_slot_it_may_not_see_is_audited_and_not_folded(
        self, monkeypatch, folds
    ):
        slots = _home(3, 2, {(0, 0), (1, 0)})
        denied: list[tuple] = []
        monkeypatch.setattr(dashboard_ws, "_audit_deny", lambda *a: denied.append(a))
        monkeypatch.setattr(dashboard_ws, "visible_subagent_slot_keys", lambda *a, **k: {slots[0]})
        monkeypatch.setattr(
            dashboard_ws,
            "load_declared_events_for_connect",
            lambda app: (True, frozenset({"subagent:user"})),
        )
        frames = [
            {"type": "subscribe_subagents", "slots": [slots[0]]},
            {"type": "subagent_replay", "slots": [slots[1]]},
        ]
        _replayed, _peak, _fake = self._drive(
            monkeypatch, frames, slots, expect_units={"acp-0-0"}, app="some-app"
        )
        assert ("some-app", "subagent_replay", "persisted_not_visible") in denied
        assert "acp-1-0" not in set(folds.units)

    def test_a_run_live_state_holds_is_not_replaced_by_a_folded_frame(self, monkeypatch):
        """The live set is read per batch, so a run that started after subscribe counts."""
        slots = _home(2, 1, {(1, 0)})
        live = SimpleNamespace(id="acp-1-0-child-0", done=False)
        manager = SimpleNamespace(running=[], all_agents=[live])
        frames = [
            {"type": "subscribe_subagents", "slots": [slots[0]]},
            {"type": "slot_focused", "slot": slots[1]},
        ]
        _replayed, _peak, fake = self._drive(
            monkeypatch,
            frames,
            slots,
            expect_units={"acp-1-0"},
            expect_ids={"acp-1-0-child-1"},
            manager=manager,
        )
        assert _drawn(fake.sent) == {"acp-1-0-child-1"}

    def test_with_the_crew_log_off_the_folder_replay_still_runs_once(self, monkeypatch, folds):
        """The opt-out keeps its folder reader; the per-slot admit must not refuse it."""
        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        calls: list[dict] = []
        record = {
            "id": "folder-1",
            "task": "t",
            "agent": "a",
            "model": "",
            "app": "",
            "slot": "chat-0",
            "started": 1.0,
            "elapsed": 1.0,
            "outcome": "completed",
            "error": "",
            "stopped": False,
        }

        def fake_read_panel_records(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(records=[record], overflow=0, overflow_is_lower_bound=False)

        monkeypatch.setattr(dashboard_ws, "read_panel_records", fake_read_panel_records)
        frames = [
            {"type": "subscribe_subagents", "slots": ["chat-0"]},
            {"type": "slot_focused", "slot": "chat-0"},
        ]
        _replayed, _peak, fake = self._drive(
            monkeypatch, frames, ["chat-0"], expect_units=set(), expect_ids={"folder-1"}
        )
        assert len(calls) == 1
        assert _drawn(fake.sent) == {"folder-1"}
        assert folds.units == []
