"""The suite's home-derived resets in ``test/conftest_resets.py``, each proved by a victim.

Upstream issue #33928: a unit test that reaches ``governance._confirm_tool_patterns``
fills ``governance._CONFIRM_PATTERNS_MEMO`` with the patterns read from ITS home's
``config.json``. Nothing in production clears it, and the integration layer clears it
only BETWEEN boots, so the boot smoke's pre-boot ``_process_is_clean()`` witness fails
for whatever test boots next in the same worker.

``test_host_isolation_floor_restores.py`` drives floor fixtures that undo something a
test patched through ``monkeypatch`` or the environment. This one cannot be proved that
way: the fixture is the ONLY thing that can clear an assignment another module made
behind the test's back, so the proof has to be a real second test that inherits the
leak. Hence the inner session below, run twice against the same two files -- once
without the fixture (the victim inherits the memo and fails) and once with it (the
victim sees ``None`` and passes).
"""

from __future__ import annotations

import pytest

from kiro_crew import sandbox
from kiro_crew.platform import governance

# ``pytester`` is not registered by default; a test module may request it by name.
pytest_plugins = ("pytester",)

#: The memo a leaking test fills: the ``(key, patterns)`` shape the production code stores.
_LEAK_SITE = 'governance._CONFIRM_PATTERNS_MEMO = (("fingerprint", 0), ("x__*",))'

#: What the failing victim's assertion prints, and nothing else in either run does.
_FAILURE_TEXT = "x__*"

_LEAKS = f"""\
import kiro_crew.platform.governance as governance


def test_a_hook_execution_fills_the_memo():
    # Production's own assignment, made by a module global rather than by a
    # ``monkeypatch`` the test could undo: the leak a later test inherits.
    {_LEAK_SITE}
    assert governance._CONFIRM_PATTERNS_MEMO is not None
"""

_VICTIM = """\
import kiro_crew.platform.governance as governance


def test_the_memo_is_gone():
    # Reads the module global production reads, so a leftover written for another
    # home is exactly what a real victim would see.
    assert governance._CONFIRM_PATTERNS_MEMO is None
"""

#: Mirror the flags ``test_socket_tripwire.py`` passes: no cache, no reshuffle, one worker.
_ARGS = ("-p", "no:cacheprovider", "-p", "no:randomly", "-n0", "-o", "addopts=")


def _memo_reset() -> object:
    """The suite's memo fixture, as a plugin for an inner session.

    ``conftest_resets`` is already in ``sys.modules``: ``test/conftest.py`` binds its
    names, so this import binds that module rather than reading ``test/`` off
    ``sys.path``.
    """
    import conftest_resets

    return type(
        "MemoReset",
        (),
        {
            "_reset_confirm_patterns_memo": staticmethod(
                conftest_resets._reset_confirm_patterns_memo
            )
        },
    )()


@pytest.fixture(autouse=True)
def _no_memo_left_in_this_process():
    """The inner runs import the same modules, so a leak there is a leak here too."""
    yield
    governance._CONFIRM_PATTERNS_MEMO = None


def test_the_memo_reset_clears_an_inherited_leak(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_leaks=_LEAKS, test_victim=_VICTIM)
    # Root the inner session in its own directory, not at this checkout's setup.cfg.
    pytester.makeini("[pytest]\n")

    # Control: no fixture, so the victim inherits the memo the first file filled.
    # ``test_leaks.py`` sorts before ``test_victim.py``, and ``-p no:randomly`` keeps it so.
    control = pytester.runpytest_inprocess(*_ARGS)
    control.assert_outcomes(passed=1, failed=1)
    control.stdout.fnmatch_lines(["*FAILED test_victim.py::test_the_memo_is_gone*"])
    assert _FAILURE_TEXT in control.stdout.str()
    governance._CONFIRM_PATTERNS_MEMO = None

    # Treatment: the real fixture, registered as a plugin, clears what the first file
    # assigned. A no-op fixture would leave the victim failing exactly as it does above.
    treatment = pytester.runpytest_inprocess(*_ARGS, plugins=[_memo_reset()])
    treatment.assert_outcomes(passed=2)
    assert _FAILURE_TEXT not in treatment.stdout.str(), treatment.stdout.str()
    governance._CONFIRM_PATTERNS_MEMO = None


#: The cache entry a leaking test leaves: the shape ``sandbox_rlimit`` stores.
_SHIM_LEAK_SITE = 'sandbox._SHIM_ARGV_CACHE["tool"] = ("/some/where/python", "-I", "-S")'

_SHIM_LEAKS = f"""\
import kiro_crew.sandbox as sandbox


def test_a_sandboxed_spawn_resolves_the_shim():
    {_SHIM_LEAK_SITE}
    assert sandbox._SHIM_ARGV_CACHE
"""

_SHIM_VICTIM = """\
import kiro_crew.sandbox as sandbox


def test_the_shim_cache_is_empty():
    # The pre-boot witness asks exactly this of the unit layer's leftovers.
    assert not sandbox._SHIM_ARGV_CACHE
"""


def _shim_clear() -> object:
    """The suite's shim-cache fixture, as a plugin for an inner session."""
    import conftest_resets

    return type(
        "ShimClear",
        (),
        {"_clear_shim_argv_cache": staticmethod(conftest_resets._clear_shim_argv_cache)},
    )()


@pytest.fixture(autouse=True)
def _no_shim_left_in_this_process():
    """The inner runs import the same module, so an entry there is an entry here too."""
    yield
    sandbox._SHIM_ARGV_CACHE.clear()


def test_the_shim_cache_clear_empties_an_inherited_entry(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_leaks=_SHIM_LEAKS, test_victim=_SHIM_VICTIM)
    pytester.makeini("[pytest]\n")

    # Control: no fixture, so the victim inherits the argv the first file resolved.
    control = pytester.runpytest_inprocess(*_ARGS)
    control.assert_outcomes(passed=1, failed=1)
    control.stdout.fnmatch_lines(["*FAILED test_victim.py::test_the_shim_cache_is_empty*"])

    # Treatment: the real fixture empties the cache between the two files.
    treatment = pytester.runpytest_inprocess(*_ARGS, plugins=[_shim_clear()])
    treatment.assert_outcomes(passed=2)
