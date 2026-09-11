"""A test node id must fit in a Windows environment variable.

pytest exports the running item's node id as ``PYTEST_CURRENT_TEST`` on every
setup/call/teardown. Windows caps one environment variable at 32767 characters,
so an item whose node id is longer ERRORS at setup there and passes everywhere
else -- and every report line for it carries the whole id, which is how one
parametrized case with a 700 KB payload as its value ran a Windows shard to its
40-minute cap with the log dropped. The root conftest refuses such an id at
collection, with the id named. These tests pin the guard from both sides.
"""

from __future__ import annotations

import importlib.util
import pathlib
import types

import pytest

_ROOT_CONFTEST = pathlib.Path(__file__).resolve().parents[1] / "conftest.py"


def _load_root_conftest():
    """Import the rootdir conftest under its own module name.

    ``from conftest import ...`` inside ``test/`` binds to ``test/conftest.py``, so
    the rootdir file is loaded by path -- the idiom ``test_ci_failure_annotations.py``
    uses. Its fixtures are inert here because nothing in this namespace collects
    them.
    """
    spec = importlib.util.spec_from_file_location("_kirocrew_nodeid_guard_conftest", _ROOT_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_root = _load_root_conftest()
MAX_NODEID_CHARS = _root.MAX_NODEID_CHARS
_refuse_oversized_nodeids = _root._refuse_oversized_nodeids


def _item(nodeid: str):
    return types.SimpleNamespace(nodeid=nodeid)


class TestTheGuard:
    def test_the_ceiling_leaves_room_under_the_windows_limit(self):
        """`` (setup)`` and an xdist group suffix ride on the exported value."""
        assert MAX_NODEID_CHARS < 32767 - 100

    def test_ordinary_ids_pass(self):
        _refuse_oversized_nodeids([_item("test/test_x.py::TestY::test_z[" + "p" * 500 + "]")])

    def test_an_id_at_the_ceiling_passes(self):
        _refuse_oversized_nodeids([_item("x" * MAX_NODEID_CHARS)])

    def test_an_id_over_the_ceiling_is_refused_and_named(self):
        long_id = "test/test_big.py::test_case[" + "A" * MAX_NODEID_CHARS + "]"
        with pytest.raises(pytest.UsageError) as excinfo:
            _refuse_oversized_nodeids([_item("fine"), _item(long_id), _item("also fine")])
        message = str(excinfo.value)
        assert "1 test node id(s)" in message
        assert "test/test_big.py::test_case[" in message
        assert "PYTEST_CURRENT_TEST" in message
        assert "ids=" in message

    def test_the_longest_offender_is_the_one_named(self):
        shorter = "test/test_a.py::test_one[" + "B" * MAX_NODEID_CHARS + "]"
        longer = "test/test_b.py::test_two[" + "C" * (MAX_NODEID_CHARS * 2) + "]"
        with pytest.raises(pytest.UsageError) as excinfo:
            _refuse_oversized_nodeids([_item(shorter), _item(longer)])
        assert "2 test node id(s)" in str(excinfo.value)
        assert "test/test_b.py::test_two[" in str(excinfo.value)


class TestTheSuiteItself:
    def test_no_collected_id_in_this_session_is_oversized(self, request):
        """The guard runs before this test, so reaching here means it held --
        but say so explicitly, so a future change to the hook cannot silently
        stop calling it."""
        items = request.session.items
        assert items, "collection produced no items"
        assert all(len(item.nodeid) <= MAX_NODEID_CHARS for item in items)
