"""The session-tree fold and its export diff.

``TestTheFold`` drives ``_otel_session_tree_start/_step/_render`` directly and pins the
render's field set, the way ``test_crew_log_otel_usage_export.py`` pins ``otel_usage``'s.
``TestTheDepthDiff`` drives ``_otel_session_tree_diff`` for the depth counter: one add
per new session, bucketed by depth, and silence on everything else. ``TestTheChildDiff``
drives the child counter: one add per child, bucketed by its ordinal among its creator's
children and attributed by the creator's kind, with the never-over-count baseline on a
first sight the process cannot prove is new. ``TestTheSubscriber`` drives the installed
subscriber through the bus, and ``TestTheRealAppendPath`` drives the fold through a real
crew log, the eager worker and the real bus.

The roots gauge is NOT exercised here: its source is the whole session tree, which no
session-keyed ``FoldAdvanced`` carries (see ``test_otel_session_tree_roots.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import bus as crew_log_bus
from kiro_crew.crew_log import eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.projection_otel_session_tree import (
    OTEL_SESSION_TREE_FOLD_NAME,
    SESSION_OPENED_TYPE,
    SESSION_SPAWNED_TYPE,
    _otel_session_tree_copy,
    _otel_session_tree_render,
    _otel_session_tree_start,
    _otel_session_tree_step,
)
from kiro_crew.crew_log.schema import Entry
from kiro_crew.metrics import crew_log_export as export

GATEWAY = "gateway"
DEPTH = export.SESSION_TREE_DEPTH_METRIC
CHILD = export.SESSION_TREE_CHILD_METRIC


def _entry(seq: int, data: dict[str, Any], entry_type: str = SESSION_OPENED_TYPE) -> Entry:
    return Entry(seq=seq, time=1_000 + seq, type=entry_type, src=GATEWAY, data=data)


def _opened(depth: Any = "__absent__", agent: str = "kirocrew") -> dict[str, Any]:
    data: dict[str, Any] = {
        "agent": agent,
        "slot": "chat-7",
        "model": "",
        "cwd": "",
        "owner": "raymond",
        "resumed": False,
    }
    if depth != "__absent__":
        data["depth"] = depth
    return data


def _spawned(seq: int, child: str = "chat-child") -> Entry:
    return _entry(seq, {"child": {"slot": child}}, entry_type=SESSION_SPAWNED_TYPE)


def _render(
    depth: Any = None, opened_ts: int = 2_000, kind: str = "conductor", children: int = 0
) -> dict[str, Any]:
    return {"depth": depth, "opened_ts": opened_ts, "agent_kind": kind, "children": children}


def _anchor(monkeypatch: pytest.MonkeyPatch, ts: int = 1_000) -> None:
    # The "new" test compares opened_ts against _evicted_max_opened_ts, which starts at
    # _install_ms; pin both so opened_ts > anchor is deterministic.
    monkeypatch.setattr(export, "_install_ms", ts)
    monkeypatch.setattr(export, "_evicted_max_opened_ts", ts)


class TestTheFold:
    def test_render_starts_with_a_null_depth_and_no_children(self) -> None:
        assert _otel_session_tree_render(_otel_session_tree_start()) == {
            "depth": None,
            "opened_ts": 0,
            "agent_kind": "",
            "children": 0,
        }

    def test_a_depth_and_the_closed_kind_are_rendered(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened(depth=2, agent="kirocrew-worker")))
        assert _otel_session_tree_render(state) == {
            "depth": 2,
            "opened_ts": 1_001,
            "agent_kind": "worker",
            "children": 0,
        }

    def test_depth_zero_is_rendered(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened(depth=0)))
        assert _otel_session_tree_render(state)["depth"] == 0

    def test_no_depth_renders_null(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened()))
        assert _otel_session_tree_render(state)["depth"] is None

    def test_the_raw_agent_and_slot_never_reach_the_render(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened(agent="my-private-agent")))
        rendered = _otel_session_tree_render(state)
        assert rendered["agent_kind"] == "other"
        assert "my-private-agent" not in repr(rendered)
        assert "chat-7" not in repr(rendered)
        assert "my-private-agent" not in repr(_otel_session_tree_copy(state))

    def test_a_reattach_does_not_overwrite_the_first_depth_or_kind(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened(depth=3, agent="kirocrew-conductor")))
        # A second opened (resumed re-attach) with a different depth is ignored.
        _otel_session_tree_step(state, _entry(2, _opened(depth=9, agent="kirocrew-worker")))
        rendered = _otel_session_tree_render(state)
        assert rendered["depth"] == 3
        assert rendered["agent_kind"] == "conductor"

    def test_each_spawned_entry_adds_one_child(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened(depth=0)))
        for seq in range(2, 6):
            _otel_session_tree_step(state, _spawned(seq, child=f"chat-{seq}"))
        rendered = _otel_session_tree_render(state)
        assert rendered["children"] == 4
        # No child slot is kept anywhere.
        assert "chat-2" not in repr(rendered)
        assert "chat-2" not in repr(_otel_session_tree_copy(state))

    def test_a_malformed_spawned_entry_is_not_counted(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(
            state, _entry(1, {"child": "chat-x"}, entry_type=SESSION_SPAWNED_TYPE)
        )
        _otel_session_tree_step(state, _entry(2, {}, entry_type=SESSION_SPAWNED_TYPE))
        assert _otel_session_tree_render(state)["children"] == 0

    def test_an_unrelated_entry_moves_nothing(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, {"actor": "user"}, entry_type="turn/started"))
        assert _otel_session_tree_render(state) == _otel_session_tree_render(
            _otel_session_tree_start()
        )

    def test_the_copy_does_not_share_state_with_the_original(self) -> None:
        state = _otel_session_tree_start()
        copied = _otel_session_tree_copy(state)
        _otel_session_tree_step(state, _spawned(1))
        assert copied["children"] == 0

    def test_render_field_set_is_pinned(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened(depth=1)))
        rendered = _otel_session_tree_render(state)
        assert set(rendered) == {"depth", "opened_ts", "agent_kind", "children"}


class TestTheDepthDiff:
    """The diff counts depth once, on the first sight (previous is None) of a session
    whose opened_ts is after the install anchor -- new to this process.
    """

    @pytest.mark.parametrize(
        "depth,bucket", [(0, "0"), (1, "1"), (2, "2"), (3, "3+"), (4, "3+"), (40, "3+")]
    )
    def test_first_depth_emits_one_counter_add_in_its_bucket(
        self, monkeypatch: pytest.MonkeyPatch, depth: int, bucket: str
    ) -> None:
        _anchor(monkeypatch)
        metrics = export._otel_session_tree_diff(None, _render(depth=depth))
        assert metrics == [export.Metric(DEPTH, 1, {"depth": bucket})]

    def test_no_depth_on_first_sight_emits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _anchor(monkeypatch)
        assert export._otel_session_tree_diff(None, _render(depth=None)) == []

    def test_a_session_opened_before_install_emits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Resumed session: opened_ts not after the anchor, so it is baselined.
        _anchor(monkeypatch, 2_000)
        assert export._otel_session_tree_diff(None, _render(depth=2, opened_ts=1_000)) == []

    def test_a_later_delivery_emits_no_depth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _anchor(monkeypatch)
        prev = _render(depth=2)
        assert export._otel_session_tree_diff(prev, _render(depth=2)) == []

    def test_a_malformed_current_depth_emits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _anchor(monkeypatch)
        for bad in (True, -1, "2"):
            assert export._otel_session_tree_diff(None, _render(depth=bad)) == []


class TestTheChildDiff:
    def test_each_new_child_is_one_add_at_its_ordinal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _anchor(monkeypatch)
        prev = _render(depth=0, children=0)
        first = export._otel_session_tree_diff(prev, _render(depth=0, children=1))
        assert first == [export.Metric(CHILD, 1, {"parent_kind": "conductor", "nth": "1"})]
        second = export._otel_session_tree_diff(
            _render(depth=0, children=1), _render(depth=0, children=2)
        )
        assert second == [export.Metric(CHILD, 1, {"parent_kind": "conductor", "nth": "2"})]

    def test_a_jump_is_split_across_buckets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _anchor(monkeypatch)
        metrics = export._otel_session_tree_diff(
            _render(children=2, kind="worker"), _render(children=10, kind="worker")
        )
        # Children 3..10: one at nth 3, four in 4-7, three in 8+.
        assert metrics == [
            export.Metric(CHILD, 1, {"parent_kind": "worker", "nth": "3"}),
            export.Metric(CHILD, 4, {"parent_kind": "worker", "nth": "4-7"}),
            export.Metric(CHILD, 3, {"parent_kind": "worker", "nth": "8+"}),
        ]

    def test_width_is_recoverable_from_the_adds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Three creators with 1, 3 and 5 children. Creators with >= k children is the
        # sum of adds at nth >= k.
        _anchor(monkeypatch)
        totals: dict[str, int] = {}
        for children in (1, 3, 5):
            for metric in export._otel_session_tree_diff(None, _render(children=children)):
                if metric.name == CHILD:
                    totals[str(metric.attrs["nth"])] = totals.get(
                        str(metric.attrs["nth"]), 0
                    ) + int(metric.value)
        assert totals == {"1": 3, "2": 2, "3": 2, "4-7": 2}
        assert totals["1"] == 3  # creators with >= 1 child
        assert totals["2"] == 2  # creators with >= 2 children
        assert totals["4-7"] == 2  # children at ordinals 4..5, all from the 5-child one

    def test_a_new_creators_first_render_counts_its_children_against_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _anchor(monkeypatch)
        metrics = export._otel_session_tree_diff(None, _render(depth=1, children=2))
        assert metrics == [
            export.Metric(DEPTH, 1, {"depth": "1"}),
            export.Metric(CHILD, 1, {"parent_kind": "conductor", "nth": "1"}),
            export.Metric(CHILD, 1, {"parent_kind": "conductor", "nth": "2"}),
        ]

    def test_a_resumed_creator_is_baselined_then_counts_at_its_true_ordinal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # After a restart the fold resumes children=4 from its savepoint; the first
        # sight baselines (no re-add of children already exported), and the next child
        # is counted as the 5th, not the 1st.
        _anchor(monkeypatch, 5_000)
        resumed = _render(opened_ts=1_000, children=4)
        assert export._otel_session_tree_diff(None, resumed) == []
        metrics = export._otel_session_tree_diff(resumed, _render(opened_ts=1_000, children=5))
        assert metrics == [export.Metric(CHILD, 1, {"parent_kind": "conductor", "nth": "4-7"})]

    def test_a_count_that_did_not_grow_emits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _anchor(monkeypatch)
        assert export._otel_session_tree_diff(_render(children=3), _render(children=3)) == []
        assert export._otel_session_tree_diff(_render(children=3), _render(children=2)) == []

    @pytest.mark.parametrize("bad_kind", ["", "kirocrew-conductor", "raw-name", 7])
    def test_parent_kind_outside_the_closed_set_is_other(
        self, monkeypatch: pytest.MonkeyPatch, bad_kind: Any
    ) -> None:
        _anchor(monkeypatch)
        current = {**_render(children=1), "agent_kind": bad_kind}
        metrics = export._otel_session_tree_diff(_render(children=0), current)
        assert metrics == [export.Metric(CHILD, 1, {"parent_kind": "other", "nth": "1"})]

    def test_a_malformed_children_value_emits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _anchor(monkeypatch)
        for bad in (True, -3, "4", None):
            current = {**_render(), "children": bad}
            assert export._otel_session_tree_diff(_render(children=0), current) == []


class _RecordingRecorder:
    """A recorder stub that captures counter and histogram calls, no OTel SDK."""

    enabled = True

    def __init__(self) -> None:
        self.histograms: list[tuple[str, float]] = []
        self.counters: list[tuple[str, float, dict[str, Any]]] = []

    def histogram(self, name: str, value: float, **kwargs: Any) -> None:
        self.histograms.append((name, value))

    def counter(self, name: str, value: float, **kwargs: Any) -> None:
        self.counters.append((name, value, dict(kwargs.get("attrs") or {})))


class TestTheSubscriber:
    def setup_method(self) -> None:
        crew_log_bus.reset_for_tests()
        export.reset_for_tests()

    def teardown_method(self) -> None:
        export.reset_for_tests()
        crew_log_bus.reset_for_tests()

    def _event(self, key: str, rev: int, value: dict[str, Any]) -> Any:
        return crew_log_bus.FoldAdvanced(
            scope=crew_log_bus.SCOPE_SESSION,
            key=key,
            fold=OTEL_SESSION_TREE_FOLD_NAME,
            revision=rev,
            value=value,
            seq=rev,
        )

    def _install(self, monkeypatch: pytest.MonkeyPatch) -> _RecordingRecorder:
        rec = _RecordingRecorder()
        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: rec)
        assert export.install_otel_crew_log_subscriber() is True
        return rec

    def _hits(self, rec: _RecordingRecorder, name: str) -> list[tuple[str, float, dict]]:
        return [hit for hit in rec.counters if hit[0] == name]

    def test_a_new_sessions_depth_is_counted_once_on_first_sight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = self._install(monkeypatch)
        assert export._install_ms is not None
        new_ts = export._install_ms + 1  # opened after install -> new to this process
        export.on_fold_advanced(self._event("s-depth-1", 1, _render(depth=2, opened_ts=new_ts)))
        assert self._hits(rec, DEPTH) == [(DEPTH, 1, {"depth": "2"})]
        # A later delivery for the same session does not re-count.
        export.on_fold_advanced(self._event("s-depth-1", 2, _render(depth=2, opened_ts=new_ts)))
        assert self._hits(rec, DEPTH) == [(DEPTH, 1, {"depth": "2"})]
        # The depth is never recorded as a histogram any more.
        assert [h for h in rec.histograms if h[0] == DEPTH] == []

    def test_a_resumed_sessions_depth_and_children_are_not_recounted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = self._install(monkeypatch)
        assert export._install_ms is not None
        old_ts = export._install_ms - 10_000  # opened before install -> resumed
        export.on_fold_advanced(
            self._event("resumed", 1, _render(depth=2, opened_ts=old_ts, children=3))
        )
        assert self._hits(rec, DEPTH) == []
        assert self._hits(rec, CHILD) == []
        export.on_fold_advanced(
            self._event("resumed", 2, _render(depth=2, opened_ts=old_ts, children=4))
        )
        assert self._hits(rec, DEPTH) == []
        assert self._hits(rec, CHILD) == [(CHILD, 1, {"parent_kind": "conductor", "nth": "4-7"})]

    def test_the_otel_session_tree_row_is_registered_with_its_diff(self) -> None:
        row = next(e for e in export._EXPORTERS if e.fold_name == OTEL_SESSION_TREE_FOLD_NAME)
        assert row.diff is export._otel_session_tree_diff

    def test_metric_names_are_pinned(self) -> None:
        assert DEPTH == "kirocrew.crew.session.tree.depth"
        assert CHILD == "kirocrew.crew.session.tree.child"


class TestTheRealAppendPath:
    """The creator's own ``session/spawned`` entries driven through a REAL ``CrewLog``,
    the eager worker and the real bus, to a recorder's counter adds.
    """

    UNIT = "s-otel-tree-creator"

    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path: Any, _floor_monkeypatch: pytest.MonkeyPatch) -> Any:
        _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        eager.stop_for_tests()
        crew_log.forget_slot_folds()
        crew_log.forget_session_folds()
        crew_log_bus.reset_for_tests()
        emit.reset_caches()
        export.reset_for_tests()
        yield
        eager.stop_for_tests()
        crew_log.forget_slot_folds()
        crew_log.forget_session_folds()
        crew_log_bus.reset_for_tests()
        emit.reset_caches()
        export.reset_for_tests()

    def _wake(self, entry_type: str, seq: int) -> None:
        eager.note_commit(self.UNIT, entry_type, seq)
        assert eager.drain(timeout=10.0)

    def test_spawned_children_reach_the_child_counter_by_ordinal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _RecordingRecorder()
        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: rec)
        assert export.install_otel_crew_log_subscriber() is True
        # Pin the anchors below any real timestamp the appends stamp.
        _anchor(monkeypatch, 1)

        handle = CrewLog.create(
            lg.KIND_SESSION, self.UNIT, owner="raymond", agent="kirocrew-conductor"
        )
        opened = handle.append(
            "session/opened",
            {
                "agent": "kirocrew-conductor",
                "slot": "chat-creator",
                "model": "",
                "cwd": "",
                "owner": "raymond",
                "resumed": False,
                "depth": 0,
            },
            src=GATEWAY,
        )
        self._wake("session/opened", int(opened.seq))
        for n in range(1, 4):
            spawned = handle.append(
                "session/spawned", {"child": {"slot": f"chat-child-{n}"}}, src=GATEWAY
            )
            self._wake("session/spawned", int(spawned.seq))

        assert [hit for hit in rec.counters if hit[0] == DEPTH] == [(DEPTH, 1, {"depth": "0"})]
        assert [hit for hit in rec.counters if hit[0] == CHILD] == [
            (CHILD, 1, {"parent_kind": "conductor", "nth": "1"}),
            (CHILD, 1, {"parent_kind": "conductor", "nth": "2"}),
            (CHILD, 1, {"parent_kind": "conductor", "nth": "3"}),
        ]
        value = crew_log.read_projection(self.UNIT, OTEL_SESSION_TREE_FOLD_NAME).value
        assert value["children"] == 3
        assert "chat-child-1" not in repr(value)
