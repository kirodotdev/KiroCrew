"""The per-test guards of ``test/conftest.py`` run for every test, in name order.

``conftest_guards`` runs the suite's per-test resets and pins behind five autouse
fixtures. These tests pin what each guard had as a fixture of its own: it runs for
every test, in the place its name sorts to, its teardown runs whatever the others
do, and a test cannot believe it replaced one.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import conftest_guards
import pytest

import conftest


def _autouse_marker(definition):
    return getattr(definition, "_fixture_function_marker", None) or getattr(
        definition, "_pytestfixturefunction", None
    )


def test_each_segment_fixture_is_an_autouse_function_fixture_of_the_suite_conftest():
    for name in conftest_guards.SEGMENT_FIXTURES:
        definition = getattr(conftest, name)
        assert definition is getattr(conftest_guards, name)
        marker = _autouse_marker(definition)
        assert (
            marker is not None and marker.autouse
        ), f"{name} is not autouse, so its guards never run"
        assert marker.scope == "function", f"{name} would not run its guards around every test"


def test_every_guard_is_in_exactly_one_segment_and_runs_in_name_order():
    segments = conftest_guards.guard_segments()
    ran = [guard.__name__ for segment in segments for guard in segment]
    assert ran == sorted(conftest_guards._REGISTRY), "a guard is missing, repeated or out of order"
    assert len(ran) > 80, "the registry lost most of its guards"
    for guard in (guard for segment in segments for guard in segment):
        assert getattr(conftest, guard.__name__) is guard


def test_each_segment_fixture_sorts_where_its_guards_did():
    """pytest sets up one conftest's autouse fixtures in name order.

    So a segment's fixture name, and every guard in it, sorts between the same
    two fixtures that stayed fixtures, and each of those is still autouse.
    """
    bounds = conftest_guards.SEGMENT_BOUNDS
    for name in bounds:
        marker = _autouse_marker(getattr(conftest, name))
        assert marker is not None and marker.autouse and marker.scope == "function"
    segments = conftest_guards.guard_segments()
    assert len(segments) == len(conftest_guards.SEGMENT_FIXTURES) == len(bounds) - 1
    for low, high, fixture, segment in zip(
        bounds, bounds[1:], conftest_guards.SEGMENT_FIXTURES, segments
    ):
        assert segment, f"no guard sorts between {low} and {high}"
        assert low < fixture < high
        assert all(low < guard.__name__ < high for guard in segment)


def test_a_guard_of_each_segment_ran_for_this_test():
    from kiro_crew import env_krb5_keyring, session_start_sizing
    from kiro_crew.slack import enterprise
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    placed = [{guard.__name__ for guard in segment} for segment in conftest_guards.guard_segments()]
    assert "_disable_dev_fleet_background_tasks" in placed[0]
    assert os.environ.get("KIROCREW_DEVFLEET_NO_BACKGROUND") == "1"
    assert "_enterprise_bypass" in placed[1]
    assert enterprise._validated_team_id == "TTEST"
    assert "_inline_taskq_pump" in placed[2]
    assert SpawnAdmissionCoordinator.pump_off_loop is False
    assert "_krb5_keyring_check_done" in placed[3]
    assert env_krb5_keyring._KRB5_KEYRING_CHECKED is True
    assert "_small_default_host" in placed[4]
    assert session_start_sizing._host_cached.cpus == 8


@pytest.fixture
def guard(monkeypatch):
    """``per_test_guard`` writing into a registry of this test's own."""
    monkeypatch.setattr(conftest_guards, "_REGISTRY", {})
    return conftest_guards.per_test_guard


def _drive(guards, request=None, floor_monkeypatch=None):
    cycle = conftest_guards.run_guards(tuple(guards), request, floor_monkeypatch)
    next(cycle)
    return cycle


def _finish(cycle):
    with pytest.raises(StopIteration):
        next(cycle)


def test_guards_set_up_in_order_and_tear_down_in_reverse(guard):
    events: list[str] = []

    @guard
    def first():
        events.append("first up")
        yield
        events.append("first down")

    @guard
    def setup_only():
        events.append("setup only")

    @guard
    def last():
        events.append("last up")
        yield
        events.append("last down")

    cycle = _drive([first, setup_only, last])
    assert events == ["first up", "setup only", "last up"]
    _finish(cycle)
    assert events[3:] == ["last down", "first down"]


def test_a_guard_gets_the_request_and_the_floor_monkeypatch_it_names(guard):
    seen: list[object] = []

    @guard
    def wants_both(request, _floor_monkeypatch):
        seen.append((request, _floor_monkeypatch))

    @guard
    def wants_the_patcher(_floor_monkeypatch):
        seen.append(_floor_monkeypatch)

    _finish(_drive([wants_both, wants_the_patcher], request="the request", floor_monkeypatch="mp"))
    assert seen == [("the request", "mp"), "mp"]


def test_a_guard_that_fails_at_setup_tears_down_the_earlier_ones_and_stops_the_later(guard):
    events: list[str] = []

    @guard
    def earlier():
        yield
        events.append("earlier down")

    @guard
    def broken():
        raise LookupError("setup failed")
        yield

    @guard
    def later():
        events.append("later up")
        yield

    with pytest.raises(LookupError, match="setup failed"):
        _drive([earlier, broken, later])
    assert events == ["earlier down"]


def test_a_guard_that_fails_at_teardown_does_not_stop_the_others(guard):
    events: list[str] = []

    @guard
    def outer():
        yield
        events.append("outer down")

    @guard
    def broken():
        yield
        raise LookupError("teardown failed")

    cycle = _drive([outer, broken])
    with pytest.raises(LookupError, match="teardown failed"):
        next(cycle)
    assert events == ["outer down"]


def test_every_teardown_failure_is_reported(guard):
    @guard
    def one():
        yield
        raise LookupError("one")

    @guard
    def two():
        yield
        raise KeyError("two")

    cycle = _drive([one, two])
    with pytest.raises(BaseException) as raised:
        next(cycle)
    assert sorted(type(exc).__name__ for exc in raised.value.exceptions) == [
        "KeyError",
        "LookupError",
    ]


def test_a_guard_with_a_second_yield_is_an_error(guard):
    @guard
    def twice():
        yield
        yield

    cycle = _drive([twice])
    with pytest.raises(RuntimeError, match="more than one 'yield'"):
        next(cycle)


def test_a_guard_cannot_take_the_shared_monkeypatch(guard):
    with pytest.raises(TypeError, match="monkeypatch"):

        @guard
        def patches_through_the_shared_instance(monkeypatch):
            """Never registered."""

    assert conftest_guards._REGISTRY == {}


def test_an_unbound_or_misnamed_guard_fails_the_seal(guard, monkeypatch):
    monkeypatch.setattr(conftest_guards, "_SEGMENTS", None)

    @guard
    def _never_imported_by_the_conftest():
        """Bound nowhere."""

    with pytest.raises(RuntimeError, match="does not bind"):
        conftest_guards.seal_guards({})

    @guard
    def _zz_sorts_after_the_last_bound():
        """Bound, but outside every segment."""

    bound = dict(conftest_guards._REGISTRY)
    with pytest.raises(RuntimeError, match="sort outside"):
        conftest_guards.seal_guards(bound)


def test_a_fixture_named_like_a_guard_fails_the_test_that_defines_it(monkeypatch):
    monkeypatch.setattr(conftest_guards, "_SHADOW_CHECKED", (None, None))

    @pytest.fixture
    def _reset_overlay_refresher():
        """A module's attempt to replace the guard."""

    shadowing = SimpleNamespace(
        __name__="test_shadow", _reset_overlay_refresher=_reset_overlay_refresher
    )
    with pytest.raises(pytest.fail.Exception, match="_reset_overlay_refresher"):
        conftest_guards.refuse_shadowing_fixtures(SimpleNamespace(module=shadowing, cls=None))
    # The guard function itself, imported to be driven directly, is not a shadow.
    driving = SimpleNamespace(
        __name__="test_drive", _reset_overlay_refresher=conftest._reset_overlay_refresher
    )
    conftest_guards.refuse_shadowing_fixtures(SimpleNamespace(module=driving, cls=None))
