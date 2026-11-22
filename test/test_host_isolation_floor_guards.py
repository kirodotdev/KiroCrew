"""The rootdir conftest's per-test floors run for every test, where they ran as fixtures.

``conftest_floor_guards`` runs most of the rootdir floors behind six autouse
fixtures. These tests pin, on the LIVE rootdir conftest, that every floor is in
a segment a function-scoped autouse fixture runs, that each segment sits between
the same fixtures its floors sorted between, and that a floor of each segment has
taken effect in the running test. What the runner itself guarantees (teardown on a
failed setup, every teardown attempted) is pinned in ``test_conftest_guards.py``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: The floor each of these names as a parameter, which therefore runs first, in
#: the namer's segment and not the one its own name sorts into.
_PULLED_FORWARD = {"_isolate_kirocrew_home": "_close_skills_loaders_the_test_opened"}


@pytest.fixture
def root(request):
    """The live rootdir ``conftest.py`` plugin."""
    return request.config.pluginmanager.get_plugin(str(_REPO_ROOT / "conftest.py"))


def _autouse_marker(definition):
    return getattr(definition, "_fixture_function_marker", None) or getattr(
        definition, "_pytestfixturefunction", None
    )


def test_each_segment_fixture_is_an_autouse_function_fixture_of_the_rootdir_conftest(root):
    for name in root.FLOOR_SEGMENT_FIXTURES:
        marker = _autouse_marker(getattr(root, name))
        assert marker is not None and marker.autouse, f"{name} is not autouse: its floors never run"
        assert marker.scope == "function", f"{name} would not run its floors around every test"


def test_every_floor_guard_is_bound_and_in_exactly_one_segment(root):
    segments = root.FLOORS.sealed_segments()
    ran = [guard.__name__ for segment in segments for guard in segment]
    assert sorted(ran) == sorted(root.FLOORS.registry), "a floor is missing or repeated"
    assert len(ran) >= 29, "the registry lost floors"
    for name in ran:
        assert getattr(root, name) is root.FLOORS.registry[name]


def test_each_segment_sorts_where_its_floors_did(root):
    """pytest sets up one conftest's autouse fixtures in name order.

    So a segment's fixture name, and every floor in it, sorts between the same two
    fixtures that stayed fixtures. A floor another floor names runs just before
    the one that names it, as pytest ran it.
    """
    bounds = root.FLOORS.bounds
    segments = root.FLOORS.sealed_segments()
    assert len(segments) == len(root.FLOOR_SEGMENT_FIXTURES) == len(bounds) - 1
    for low, high, fixture, segment in zip(
        bounds, bounds[1:], root.FLOOR_SEGMENT_FIXTURES, segments
    ):
        assert segment, f"no floor sorts between {low} and {high}"
        assert low < fixture < high
        names = [guard.__name__ for guard in segment]
        own = [name for name in names if name not in _PULLED_FORWARD]
        assert own == sorted(own) and all(low < name < high for name in own)
        for pulled in set(names) & set(_PULLED_FORWARD):
            assert names.index(pulled) == names.index(_PULLED_FORWARD[pulled]) - 1


def test_the_fixtures_that_cut_the_segments_are_still_fixtures(root):
    """Each bound that is always defined is an autouse fixture, and none is a guard."""
    always = [
        "_block_host_service_mutation",
        "_isolate_agent_spec_home",
        "_isolate_kiro_sessions_dir",
        "_reset_platform_context",
    ]
    for name in always:
        marker = _autouse_marker(getattr(root, name))
        assert marker is not None and marker.autouse and marker.scope == "function", name
    assert not set(root.FLOORS.bounds) & set(root.FLOORS.registry)


def test_a_floor_of_each_segment_ran_for_this_test(root, request):
    placed = [{guard.__name__ for guard in segment} for segment in root.FLOORS.sealed_segments()]
    for index, name in enumerate(root.FLOOR_SEGMENT_FIXTURES):
        assert name in request.fixturenames, f"segment {index} is not one of this test's fixtures"

    assert "_isolate_kirocrew_home" in placed[0]
    home = os.environ["KIROCREW_HOME"]
    assert Path(home) != Path.home() / ".kiro" / "crew"

    assert "_git_maintenance_off" in placed[1]
    assert int(os.environ.get("GIT_CONFIG_COUNT", "0")) >= 1

    assert "_isolate_kiro_cli_sign_in_store" in placed[2]
    assert "_isolate_shared_kiro_paths" in placed[3]

    assert "_isolate_xdg_config_home" in placed[4]
    assert os.environ["XDG_CONFIG_HOME"] != str(Path.home() / ".config")

    assert "_restore_logger_levels" in placed[5]


def test_a_logger_level_left_by_a_test_is_put_back_by_the_live_segment(root, request):
    """The last segment's teardown, driven through the live fixture's own function."""
    logger = logging.getLogger("kiro_crew.floor_guard_probe")
    before = logger.level
    runner = getattr(root, root.FLOOR_SEGMENT_FIXTURES[-1])
    function = getattr(runner, "__wrapped__", runner)
    with pytest.MonkeyPatch.context() as floor_monkeypatch:
        cycle = function(request, floor_monkeypatch)
        next(cycle)
        logger.setLevel(logging.CRITICAL)
        with pytest.raises(StopIteration):
            next(cycle)
    assert logger.level == before
