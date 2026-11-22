"""The per-test guards of ``test/conftest.py``, run behind five fixture names.

A guard here is a plain function marked ``@per_test_guard``; the five
``_*_guards`` fixtures below run every one of them around every test, in the
order pytest ran them when each was its own autouse fixture. The runner, and
what a guard keeps from being a fixture, are in ``per_test_guards``.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest
registers each fixture as the suite conftest's own; see the import block there.
Plain module, never collected.
"""

from __future__ import annotations

import pytest
from per_test_guards import GuardSet

#: The guards of ``test/conftest.py``. Its bounds are the autouse fixtures there
#: that a test overrides or requests by name. A guard patches through
#: ``_floor_monkeypatch``, never the shared ``monkeypatch``.
SUITE = GuardSet(
    "test/conftest.py",
    bounds=(
        "_a_shared_monkeypatch_first",
        "_drop_live_config_snapshot",
        "_git_identity",
        "_isolate_context_store_caches",
        "_no_live_catalog_network",
        "_windows_restrict_to_owner_stub",
    ),
    fixtures=("request", "_floor_monkeypatch"),
)

per_test_guard = SUITE.guard
seal_guards = SUITE.seal


# Each fixture is named to sort between the two bounds it runs between, which is
# what puts its guards where they ran as fixtures of their own.


@pytest.fixture(autouse=True)
def _b_guards(request, _floor_monkeypatch):
    """The guards named between ``_a_shared_monkeypatch_first`` and ``_drop_live_config_snapshot``."""
    yield from SUITE.run_segment(0, request, _floor_monkeypatch=_floor_monkeypatch)


@pytest.fixture(autouse=True)
def _e_guards(request, _floor_monkeypatch):
    """The guards named between ``_drop_live_config_snapshot`` and ``_git_identity``."""
    yield from SUITE.run_segment(1, request, _floor_monkeypatch=_floor_monkeypatch)


@pytest.fixture(autouse=True)
def _h_guards(request, _floor_monkeypatch):
    """The guards named between ``_git_identity`` and ``_isolate_context_store_caches``."""
    yield from SUITE.run_segment(2, request, _floor_monkeypatch=_floor_monkeypatch)


@pytest.fixture(autouse=True)
def _j_guards(request, _floor_monkeypatch):
    """The guards named between ``_isolate_context_store_caches`` and ``_no_live_catalog_network``."""
    yield from SUITE.run_segment(3, request, _floor_monkeypatch=_floor_monkeypatch)


@pytest.fixture(autouse=True)
def _o_guards(request, _floor_monkeypatch):
    """The guards named between ``_no_live_catalog_network`` and ``_windows_restrict_to_owner_stub``."""
    yield from SUITE.run_segment(4, request, _floor_monkeypatch=_floor_monkeypatch)


#: The fixture that runs each ``SUITE`` segment, in bounds order.
SEGMENT_FIXTURES = ("_b_guards", "_e_guards", "_h_guards", "_j_guards", "_o_guards")
