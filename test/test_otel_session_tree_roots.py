"""The ``session.tree.roots`` observable gauge: the collect-time read and its
registration.

The read folds the current session tree each collect (no dirty flag, no cache) on the
SDK's own collection thread, reports ``None`` (no observation) when the tree is not
trustworthy, and never raises. The gauge is registered through the same
``meter.create_observable_gauge`` path the inventory and process gauges use.
"""

from __future__ import annotations

from kiro_crew.crew_log import otel_session_tree_roots as roots
from kiro_crew.crew_log.session_tree import TreeNode


def _node(slot: str, parent: str | None = None, cycle: bool = False) -> TreeNode:
    return TreeNode(slot=slot, parent_slot=parent, cycle=cycle)


class _FakeReading:
    def __init__(self, nodes: dict[str, TreeNode], incomplete: bool = False) -> None:
        self.nodes = nodes
        self.incomplete = incomplete


class _FakeProjection:
    def __init__(self, nodes, *, seeded: bool = True, incomplete: bool = False) -> None:
        self._nodes = nodes
        self.seeded_for_current_store = seeded
        self._incomplete = incomplete
        self.reading_calls = 0

    def reading(self):
        self.reading_calls += 1
        return _FakeReading(self._nodes, self._incomplete)


def _install_projection(monkeypatch, proj: _FakeProjection) -> None:
    monkeypatch.setattr("kiro_crew.crew_log.session_tree_projection.projection", lambda: proj)


def _one_branched() -> dict[str, TreeNode]:
    # a (root) <- b. One branched root.
    return {"a": _node("a"), "b": _node("b", parent="a")}


class TestTheRead:
    def test_a_branched_root_counts_one(self, monkeypatch) -> None:
        _install_projection(monkeypatch, _FakeProjection(_one_branched()))
        assert roots.read_roots_with_children() == 1

    def test_two_branched_roots_count_two(self, monkeypatch) -> None:
        nodes = {
            "a": _node("a"),
            "b": _node("b", parent="a"),
            "x": _node("x"),
            "y": _node("y", parent="x"),
        }
        _install_projection(monkeypatch, _FakeProjection(nodes))
        assert roots.read_roots_with_children() == 2

    def test_a_seeded_empty_tree_reports_zero(self, monkeypatch) -> None:
        # A real 0: seeded + complete, just no branched roots.
        _install_projection(monkeypatch, _FakeProjection({"a": _node("a")}))
        assert roots.read_roots_with_children() == 0

    def test_it_folds_the_tree_on_every_call(self, monkeypatch) -> None:
        # No cache: each read reads the projection again (the SDK collects off-thread).
        proj = _FakeProjection(_one_branched())
        _install_projection(monkeypatch, proj)
        assert roots.read_roots_with_children() == 1
        assert roots.read_roots_with_children() == 1
        assert proj.reading_calls == 2

    def test_an_unseeded_projection_reports_none_without_folding(self, monkeypatch) -> None:
        proj = _FakeProjection(_one_branched(), seeded=False)
        _install_projection(monkeypatch, proj)
        assert roots.read_roots_with_children() is None
        assert proj.reading_calls == 0

    def test_an_incomplete_reading_reports_none(self, monkeypatch) -> None:
        proj = _FakeProjection(_one_branched(), incomplete=True)
        _install_projection(monkeypatch, proj)
        assert roots.read_roots_with_children() is None

    def test_a_raising_projection_reports_none(self, monkeypatch) -> None:
        def _raise():
            raise RuntimeError("projection unavailable")

        monkeypatch.setattr("kiro_crew.crew_log.session_tree_projection.projection", _raise)
        assert roots.read_roots_with_children() is None


class TestTheCallback:
    def test_the_callback_yields_one_untagged_observation(self, monkeypatch) -> None:
        _install_projection(monkeypatch, _FakeProjection(_one_branched()))
        obs = roots._roots_callback(None)
        assert len(obs) == 1
        assert obs[0].value == 1
        assert dict(obs[0].attributes) == {}

    def test_the_callback_yields_nothing_when_unavailable(self, monkeypatch) -> None:
        _install_projection(monkeypatch, _FakeProjection(_one_branched(), seeded=False))
        assert roots._roots_callback(None) == []

    def test_the_callback_yields_a_real_zero(self, monkeypatch) -> None:
        _install_projection(monkeypatch, _FakeProjection({"a": _node("a")}))
        obs = roots._roots_callback(None)
        assert len(obs) == 1 and obs[0].value == 0


class TestRegistration:
    def test_register_creates_the_gauge_on_the_meter(self) -> None:
        created: list[tuple[str, tuple]] = []

        class _Meter:
            def create_observable_gauge(self, name, callbacks, **kwargs):
                created.append((name, tuple(callbacks)))

        roots.register_session_tree_gauge(_Meter())
        assert [n for n, _ in created] == [roots.SESSION_TREE_ROOTS_METRIC]
        # The registered callback is the module's own.
        assert created[0][1] == (roots._roots_callback,)

    def test_the_metric_name_validates(self) -> None:
        from kiro_crew.metrics.schema import validate_name

        # Core namespace, no raise.
        assert validate_name(roots.SESSION_TREE_ROOTS_METRIC) == roots.SESSION_TREE_ROOTS_METRIC
