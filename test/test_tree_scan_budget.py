"""The per-test timeout a ``tree_scan`` group carries.

The ``tree_scan_*`` xdist groups hold the gates that parse or walk a whole tree: the
package under ``src/kiro_crew`` or the suite under ``test/``. The slow ones take
20-42 s of CPU on a fast host and about three times that on a hosted runner, which
is past the suite-wide ``--timeout=120``.

``test/conftest.py`` gives every test in a ``tree_scan`` group (module, class or
function level) that carries no timeout value of its own the group default,
:data:`conftest.TREE_SCAN_TIMEOUT_SECS`. This module pins that hook.

It does not find scans. A slow scan with no ``tree_scan`` group still runs at 120 s,
and nothing here sees it. The fix for that is to narrow the scan through
``test/source_corpus.py`` (``parsed_candidates`` with the literals a hit must spell),
so only the files that can match are parsed. Where no literal can narrow it, the
scan takes a ``tree_scan`` group; see ``docs/system-specs/common/testing-conventions.md``.
"""

from __future__ import annotations

import re

import pytest
from source_corpus import repo_root

import conftest
from conftest import TREE_SCAN_TIMEOUT_SECS

# ``pytester`` is not registered by default; a test module may request it by name.
pytest_plugins = ("pytester",)

pytestmark = pytest.mark.xdist_group(name="tree_scan_tree_scan_budget")

REPO_ROOT = repo_root()


def test_the_group_default_is_over_the_suite_timeout() -> None:
    """The default lifts tree scans out of the suite-wide ``--timeout`` in setup.cfg."""
    found = re.findall(r"--timeout=(\d+)", (REPO_ROOT / "setup.cfg").read_text(encoding="utf-8"))
    assert found, "setup.cfg no longer sets a suite-wide --timeout"
    assert all(TREE_SCAN_TIMEOUT_SECS > int(value) for value in found)


def test_conftest_gives_a_tree_scan_test_the_group_default(request: pytest.FixtureRequest) -> None:
    """This module is in a tree_scan group with no timeout mark, so it runs at the default."""
    marker = request.node.get_closest_marker("timeout")
    assert marker is not None and marker.args == (TREE_SCAN_TIMEOUT_SECS,)


_HOOKED = """
import pytest
pytestmark = pytest.mark.xdist_group(name="tree_scan_hooked")

def test_plain():
    pass

@pytest.mark.timeout(method="thread")
def test_method_only():
    pass

@pytest.mark.timeout(None, "thread")
def test_positional_none():
    pass

@pytest.mark.timeout(timeout=60)
def test_keyword():
    pass

@pytest.mark.timeout(400)
def test_explicit():
    pass

@pytest.mark.xdist_group(name="serial")
def test_other_group():
    pass
"""

#: A group on one test, not on the module: only that test takes the default.
_FUNCTION_GROUP = """
import pytest

@pytest.mark.xdist_group(name="tree_scan_one_test")
def test_grouped():
    pass

def test_ungrouped():
    pass
"""

#: Module-level budgets: a test with no mark, or a mark with no value, keeps the module's.
_MODULE_BUDGET = """
import pytest
pytestmark = [pytest.mark.xdist_group(name="tree_scan_budgeted"), pytest.mark.timeout(600)]

def test_module_budget():
    pass

@pytest.mark.timeout(method="thread")
def test_valueless_under_a_module_budget():
    pass
"""

_UNDER_A_SHORT_MODULE = """
import pytest
pytestmark = [pytest.mark.xdist_group(name="tree_scan_short"), pytest.mark.timeout(60)]

@pytest.mark.timeout(400)
def test_function_budget():
    pass
"""


def _effective_timeouts(pytester: pytest.Pytester, source: str) -> dict[str, tuple[object, object]]:
    """(timeout, method) pytest-timeout reads for each item of *source*, after the hook."""
    import pytest_timeout

    # Root the inner session at its own directory so the repository's conftest and
    # setup.cfg are not loaded into it (see test_pytest_asyncio_scan_gate.py).
    pytester.makeini("[pytest]\n")
    items = pytester.getitems(source)
    conftest.pytest_collection_modifyitems(items[0].config, items)
    out: dict[str, tuple[object, object]] = {}
    for item in items:
        settings = pytest_timeout._get_item_settings(item)
        out[item.name] = (settings.timeout, settings.method)
    return out


def test_the_hook_gives_a_valueless_timeout_mark_the_default(pytester: pytest.Pytester) -> None:
    """A mark naming no seconds gets the default and keeps its method; a value is kept."""
    got = _effective_timeouts(pytester, _HOOKED)
    default = float(TREE_SCAN_TIMEOUT_SECS)
    assert got["test_plain"][0] == default
    assert got["test_method_only"] == (default, "thread")
    assert got["test_positional_none"] == (default, "thread")
    assert got["test_keyword"][0] == 60.0
    assert got["test_explicit"][0] == 400.0
    # Outside the tree_scan group the hook adds nothing.
    assert got["test_other_group"][0] is None


def test_the_hook_gives_a_function_level_group_the_default(pytester: pytest.Pytester) -> None:
    """A ``tree_scan`` group on one test lifts that test alone; its siblings keep theirs."""
    got = _effective_timeouts(pytester, _FUNCTION_GROUP)
    assert got["test_grouped"][0] == float(TREE_SCAN_TIMEOUT_SECS)
    assert got["test_ungrouped"][0] is None


def test_the_hook_keeps_a_module_budget(pytester: pytest.Pytester) -> None:
    """A module-level budget is not overridden by the default, under a valueless mark too:
    the closest explicit value at any level wins over the default."""
    got = _effective_timeouts(pytester, _MODULE_BUDGET)
    assert got["test_module_budget"][0] == 600.0
    assert got["test_valueless_under_a_module_budget"] == (600.0, "thread")


def test_the_hook_keeps_a_function_budget_over_a_module_one(pytester: pytest.Pytester) -> None:
    """A closer function budget wins over a shorter module one."""
    got = _effective_timeouts(pytester, _UNDER_A_SHORT_MODULE)
    assert got["test_function_budget"][0] == 400.0
