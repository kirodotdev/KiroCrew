"""The rootdir conftest's per-test floors, run behind six fixture names.

Each floor is a plain function marked ``@floor_guard`` in the part that owns it;
the six ``_*_floors`` fixtures below run every one of them around every test, for
every testpath, in the order pytest ran them when each was its own autouse
fixture. The runner, and what a guard keeps from being a fixture, are in
``per_test_guards``.

Part of the rootdir ``conftest.py``, which loads it by path before the parts
that mark their floors and binds these fixtures into its own namespace. Plain
module, never collected.
"""

from __future__ import annotations

import pytest
from per_test_guards import GuardSet

#: The rootdir floors. Its bounds are the function-scoped autouse fixtures of the
#: rootdir conftest that stay fixtures: the first one (which creates
#: ``_floor_monkeypatch`` before ``_isolation_dirs`` exists), the two a fixture
#: requests by name, the one a test module replaces, and the two that are bound
#: only under an opt-in switch.
FLOORS = GuardSet(
    "the rootdir conftest.py",
    bounds=(
        "_block_host_service_mutation",
        "_crew_log_read_budget_check",
        "_isolate_agent_spec_home",
        "_isolate_kiro_sessions_dir",
        "_isolate_tempfile_base_per_test",
        "_reset_platform_context",
        "_zz",
    ),
    fixtures=(
        "request",
        "_floor_monkeypatch",
        "_isolation_dirs",
        "_record_skill_search_indexes",
        "_no_real_cgroup_reclaim_write",
        "_git_global_config",
        "_xdg_config_root",
        "_sandbox_mount_source_root",
    ),
)

floor_guard = FLOORS.guard
seal_floor_guards = FLOORS.seal


# Each fixture is named to sort between the two bounds it runs between, and
# requests only what its own guards name, so pytest creates a fixture a guard
# depends on where it created it before.


@pytest.fixture(autouse=True)
def _c_floors(request, _floor_monkeypatch, _isolation_dirs, _record_skill_search_indexes):
    """The floors named between ``_block_host_service_mutation`` and ``_crew_log_read_budget_check``."""
    yield from FLOORS.run_segment(
        0,
        request,
        _floor_monkeypatch=_floor_monkeypatch,
        _isolation_dirs=_isolation_dirs,
        _record_skill_search_indexes=_record_skill_search_indexes,
    )


@pytest.fixture(autouse=True)
def _f_floors(request, _no_real_cgroup_reclaim_write, _floor_monkeypatch, _git_global_config):
    """The floors named between ``_crew_log_read_budget_check`` and ``_isolate_agent_spec_home``."""
    yield from FLOORS.run_segment(
        1,
        request,
        _no_real_cgroup_reclaim_write=_no_real_cgroup_reclaim_write,
        _floor_monkeypatch=_floor_monkeypatch,
        _git_global_config=_git_global_config,
    )


@pytest.fixture(autouse=True)
def _isolate_b_floors(request, _isolation_dirs, _floor_monkeypatch, _xdg_config_root):
    """The floors named between ``_isolate_agent_spec_home`` and ``_isolate_kiro_sessions_dir``."""
    yield from FLOORS.run_segment(
        2,
        request,
        _isolation_dirs=_isolation_dirs,
        _floor_monkeypatch=_floor_monkeypatch,
        _xdg_config_root=_xdg_config_root,
    )


@pytest.fixture(autouse=True)
def _isolate_l_floors(
    request, _xdg_config_root, _floor_monkeypatch, _sandbox_mount_source_root, _isolation_dirs
):
    """The floors named between ``_isolate_kiro_sessions_dir`` and ``_isolate_tempfile_base_per_test``."""
    yield from FLOORS.run_segment(
        3,
        request,
        _xdg_config_root=_xdg_config_root,
        _floor_monkeypatch=_floor_monkeypatch,
        _sandbox_mount_source_root=_sandbox_mount_source_root,
        _isolation_dirs=_isolation_dirs,
    )


@pytest.fixture(autouse=True)
def _j_floors(request, _xdg_config_root, _floor_monkeypatch, _isolation_dirs):
    """The floors named between ``_isolate_tempfile_base_per_test`` and ``_reset_platform_context``."""
    yield from FLOORS.run_segment(
        4,
        request,
        _xdg_config_root=_xdg_config_root,
        _floor_monkeypatch=_floor_monkeypatch,
        _isolation_dirs=_isolation_dirs,
    )


@pytest.fixture(autouse=True)
def _rest_floors(request, _floor_monkeypatch):
    """The floors named after ``_reset_platform_context``."""
    yield from FLOORS.run_segment(5, request, _floor_monkeypatch=_floor_monkeypatch)


#: The fixture that runs each ``FLOORS`` segment, in bounds order.
FLOOR_SEGMENT_FIXTURES = (
    "_c_floors",
    "_f_floors",
    "_isolate_b_floors",
    "_isolate_l_floors",
    "_j_floors",
    "_rest_floors",
)
