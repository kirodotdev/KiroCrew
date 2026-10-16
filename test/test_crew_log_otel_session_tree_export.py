"""The session-tree depth fold and its export diff.

``TestTheFold`` drives ``_otel_session_tree_start/_step/_render`` directly and pins the
render's field set, the way ``test_crew_log_otel_usage_export.py`` pins ``otel_usage``'s.
``TestTheDepthDiff`` drives ``_otel_session_tree_diff`` -- the histogram observation it
emits on the first depth, and the silence on everything else. ``TestTheSubscriber`` drives
the installed subscriber through the real crew-log bus and asserts a depth reaches
``recorder.histogram``.

The roots counter is NOT exercised here: its source is the whole session tree, which no
session- or slot-keyed ``FoldAdvanced`` carries, so it is not wired through this export
yet (see the evidence note / conductor question).
"""

from __future__ import annotations

from typing import Any

from kiro_crew.crew_log.projection_otel_session_tree import (
    OTEL_SESSION_TREE_FOLD_NAME,
    SESSION_OPENED_TYPE,
    _otel_session_tree_render,
    _otel_session_tree_start,
    _otel_session_tree_step,
)
from kiro_crew.crew_log.schema import Entry
from kiro_crew.metrics import crew_log_export as export

GATEWAY = "gateway"


def _entry(seq: int, data: dict[str, Any], entry_type: str = SESSION_OPENED_TYPE) -> Entry:
    return Entry(seq=seq, time=1_000 + seq, type=entry_type, src=GATEWAY, data=data)


def _opened(depth: Any = "__absent__") -> dict[str, Any]:
    data: dict[str, Any] = {
        "agent": "kirocrew",
        "slot": "chat-7",
        "model": "",
        "cwd": "",
        "owner": "raymond",
        "resumed": False,
    }
    if depth != "__absent__":
        data["depth"] = depth
    return data


class TestTheFold:
    def test_render_starts_with_a_null_depth(self) -> None:
        assert _otel_session_tree_render(_otel_session_tree_start()) == {
            "depth": None,
            "opened_ts": 0,
        }

    def test_a_depth_is_rendered(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened(depth=2)))
        assert _otel_session_tree_render(state) == {"depth": 2, "opened_ts": 1_001}

    def test_depth_zero_is_rendered(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened(depth=0)))
        assert _otel_session_tree_render(state) == {"depth": 0, "opened_ts": 1_001}

    def test_no_depth_renders_null(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened()))
        assert _otel_session_tree_render(state) == {"depth": None, "opened_ts": 1_001}

    def test_a_reattach_does_not_overwrite_the_first_depth(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened(depth=3)))
        # A second opened (resumed re-attach) with a different depth is ignored.
        _otel_session_tree_step(state, _entry(2, _opened(depth=9)))
        assert _otel_session_tree_render(state) == {"depth": 3, "opened_ts": 1_001}

    def test_a_non_opened_entry_moves_nothing(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, {"actor": "user"}, entry_type="turn/started"))
        assert _otel_session_tree_render(state) == {"depth": None, "opened_ts": 0}

    def test_render_field_set_is_pinned(self) -> None:
        state = _otel_session_tree_start()
        _otel_session_tree_step(state, _entry(1, _opened(depth=1)))
        rendered = _otel_session_tree_render(state)
        assert set(rendered) == {"depth", "opened_ts"}


class TestTheDepthDiff:
    """The diff observes depth once, on the first sight (previous is None) of a session
    whose opened_ts is at or after install -- new to this process. ``_install_ms`` is
    pinned so opened_ts comparisons are deterministic.
    """

    def _new(self, depth: Any) -> dict[str, Any]:
        # opened_ts >= the install anchor the tests pin -> a new session.
        return {"depth": depth, "opened_ts": 2_000}

    def _anchor(self, monkeypatch, ts: int = 1_000) -> None:
        # The "new" test compares opened_ts against _evicted_max_opened_ts, which
        # starts at _install_ms; pin both so opened_ts > anchor is deterministic.
        monkeypatch.setattr(export, "_install_ms", ts)
        monkeypatch.setattr(export, "_evicted_max_opened_ts", ts)

    def test_first_depth_emits_one_histogram_observation(self, monkeypatch) -> None:
        self._anchor(monkeypatch)
        metrics = export._otel_session_tree_diff(None, self._new(2))
        # unit="1": a dimensionless depth count, not milliseconds.
        assert metrics == [
            export.Metric(export.SESSION_TREE_DEPTH_METRIC, export.KIND_HISTOGRAM, 2, {}, unit="1")
        ]

    def test_depth_zero_is_observed(self, monkeypatch) -> None:
        self._anchor(monkeypatch)
        metrics = export._otel_session_tree_diff(None, self._new(0))
        assert metrics == [
            export.Metric(export.SESSION_TREE_DEPTH_METRIC, export.KIND_HISTOGRAM, 0, {}, unit="1")
        ]

    def test_no_depth_on_first_sight_emits_nothing(self, monkeypatch) -> None:
        self._anchor(monkeypatch)
        assert export._otel_session_tree_diff(None, {"depth": None, "opened_ts": 2_000}) == []

    def test_a_session_opened_before_install_emits_nothing(self, monkeypatch) -> None:
        # Resumed session: opened_ts not after the anchor, so its depth is not observed.
        self._anchor(monkeypatch, 2_000)
        assert export._otel_session_tree_diff(None, {"depth": 2, "opened_ts": 1_000}) == []

    def test_a_later_delivery_emits_nothing(self, monkeypatch) -> None:
        # previous not None -> not a first sight, so no observation even for a new one.
        self._anchor(monkeypatch)
        prev = {"depth": 2, "opened_ts": 2_000}
        assert export._otel_session_tree_diff(prev, {"depth": 2, "opened_ts": 2_000}) == []

    def test_a_malformed_current_depth_emits_nothing(self, monkeypatch) -> None:
        self._anchor(monkeypatch)
        assert export._otel_session_tree_diff(None, {"depth": True, "opened_ts": 2_000}) == []
        assert export._otel_session_tree_diff(None, {"depth": -1, "opened_ts": 2_000}) == []
        assert export._otel_session_tree_diff(None, {"depth": "2", "opened_ts": 2_000}) == []


class _RecordingRecorder:
    """A recorder stub that captures histogram calls, no OTel SDK."""

    enabled = True

    def __init__(self) -> None:
        self.histograms: list[tuple[str, float]] = []
        self.counters: list[tuple[str, float]] = []

    def histogram(self, name: str, value: float, **kwargs: Any) -> None:
        self.histograms.append((name, value))

    def counter(self, name: str, value: float, **kwargs: Any) -> None:
        self.counters.append((name, value))


class TestTheSubscriber:
    def setup_method(self) -> None:
        from kiro_crew.crew_log import bus as crew_log_bus

        crew_log_bus.reset_for_tests()
        export.reset_for_tests()

    def teardown_method(self) -> None:
        from kiro_crew.crew_log import bus as crew_log_bus

        export.reset_for_tests()
        crew_log_bus.reset_for_tests()

    def _event(self, key: str, rev: int, depth, opened_ts: int):
        from kiro_crew.crew_log import bus as crew_log_bus

        return crew_log_bus.FoldAdvanced(
            scope=crew_log_bus.SCOPE_SESSION,
            key=key,
            fold=OTEL_SESSION_TREE_FOLD_NAME,
            revision=rev,
            value={"depth": depth, "opened_ts": opened_ts},
            seq=rev,
        )

    def _install(self, monkeypatch) -> _RecordingRecorder:
        rec = _RecordingRecorder()
        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: rec)
        assert export.install_otel_crew_log_subscriber() is True
        return rec

    def test_a_new_sessions_depth_is_observed_once_on_first_sight(self, monkeypatch) -> None:
        rec = self._install(monkeypatch)
        new_ts = export._install_ms + 1  # opened after install -> new to this process
        export.on_fold_advanced(self._event("s-depth-1", 1, 2, new_ts))
        depth_hits = [h for h in rec.histograms if h[0] == export.SESSION_TREE_DEPTH_METRIC]
        assert depth_hits == [(export.SESSION_TREE_DEPTH_METRIC, 2)]
        # A later delivery for the same session does not re-observe.
        export.on_fold_advanced(self._event("s-depth-1", 2, 2, new_ts))
        depth_hits = [h for h in rec.histograms if h[0] == export.SESSION_TREE_DEPTH_METRIC]
        assert depth_hits == [(export.SESSION_TREE_DEPTH_METRIC, 2)]

    def test_a_resumed_sessions_depth_is_not_observed_on_restart(self, monkeypatch) -> None:
        """A resumed session's opened_ts predates install, so its depth is not observed
        -- the restart guard: a histogram bucket is not re-filled on every boot.
        """
        rec = self._install(monkeypatch)
        old_ts = export._install_ms - 10_000  # opened before install -> resumed
        export.on_fold_advanced(self._event("resumed", 1, 2, old_ts))
        depth_hits = [h for h in rec.histograms if h[0] == export.SESSION_TREE_DEPTH_METRIC]
        assert depth_hits == [], "a resumed session's depth was re-observed on restart"
        export.on_fold_advanced(self._event("resumed", 2, 2, old_ts))
        depth_hits = [h for h in rec.histograms if h[0] == export.SESSION_TREE_DEPTH_METRIC]
        assert depth_hits == []

    def test_the_otel_session_tree_row_is_registered_with_its_diff(self) -> None:
        row = next(e for e in export._EXPORTERS if e.fold_name == OTEL_SESSION_TREE_FOLD_NAME)
        assert row.diff is export._otel_session_tree_diff

    def test_depth_metric_literal_in_record_matches_the_constant(self) -> None:
        # _record_metrics spells the depth metric name as a literal so the provider's
        # bucket-view AST scan can read its unit="1"; pin that literal to the constant.
        assert export.SESSION_TREE_DEPTH_METRIC == "kirocrew.crew.session.tree.depth"
