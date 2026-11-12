"""``forget_home_bound_caches`` really empties what the pre-boot witness checks.

Upstream issue #33928: the boot smoke test asserts ``_process_is_clean()``, which
calls ``home_bound_globals_are_clear()``, BEFORE it boots. Several of those globals are
caches that a unit test fills by doing real work (running a hook, loading an embedder,
resolving app permissions) and that production never clears, so whichever test ran
first on an xdist worker decided whether the boot smoke failed. ``#34112`` added
``forget_home_bound_caches`` and the ``pytest_runtest_teardown`` call that runs it
after every test.

This module proves the function itself. It cannot be proved the way
``test_host_isolation_floor_restores.py`` proves a floor fixture, because the leak is
an assignment another module made behind the test's back rather than something a
``monkeypatch`` can undo. The proof is a real second test that inherits the leak: an
inner pytest session runs a leaking file followed by a victim that requires every
cache empty, once without the teardown call (the victim fails) and once with it (the
victim passes).
"""

from __future__ import annotations

import pytest

# ``pytester`` is not registered by default; a test module may request it by name.
pytest_plugins = ("pytester",)

#: Every cache ``forget_home_bound_caches`` empties, as a leak site and its check.
_LEAKS = """\
import kiro_crew.dashboard.token_auth as token_auth
import kiro_crew.embeddings as embeddings
import kiro_crew.platform.governance as governance
import kiro_crew.sandbox_rlimit as sandbox_rlimit


def test_real_work_fills_the_caches():
    # The shapes production leaves: a memo keyed on a home, a resolved argv, a loaded
    # backend, and a resolved permission set.
    governance._CONFIRM_PATTERNS_MEMO = (("fingerprint", 0), ("x__*",))
    sandbox_rlimit._SHIM_ARGV_CACHE["tool"] = ("/some/where/python", "-I", "-S")
    embeddings._shared_embedder = object()
    embeddings._download_manager = object()
    token_auth._app_perms_cache = {"app": frozenset({"tool"})}
    token_auth._app_perms_denied = {"app": frozenset({"tool"})}
    assert governance._CONFIRM_PATTERNS_MEMO is not None
"""

_VICTIM = """\
import kiro_crew.dashboard.token_auth as token_auth
import kiro_crew.embeddings as embeddings
import kiro_crew.platform.governance as governance
import kiro_crew.sandbox as sandbox


def test_the_caches_are_empty():
    # Reads the names the integration layer's witness reads, so a leftover that would
    # fail a real boot fails here first.
    assert governance._CONFIRM_PATTERNS_MEMO is None
    assert not sandbox._SHIM_ARGV_CACHE
    assert embeddings._shared_embedder is None
    assert embeddings._download_manager is None
    assert not token_auth._app_perms_cache
    assert not token_auth._app_perms_denied
"""

#: Mirror the flags ``test_socket_tripwire.py`` passes: no cache, no reshuffle, one worker.
_ARGS = ("-p", "no:cacheprovider", "-p", "no:randomly", "-n0", "-o", "addopts=")


def _teardown_reset() -> object:
    """The suite's reset, wired the way ``test/conftest.py`` wires it.

    ``forget_home_bound_caches`` is a plain function that the rootdir conftest calls
    from its ``pytest_runtest_teardown`` hook, so the inner session gets the same
    wrapper rather than a fixture: the call has to land after every fixture, the
    test's own ``monkeypatch`` included.
    """
    import conftest_resets

    def pytest_runtest_teardown(item, nextitem):
        conftest_resets.forget_home_bound_caches()

    return type(
        "HomeBoundCaches", (), {"pytest_runtest_teardown": staticmethod(pytest_runtest_teardown)}
    )()


@pytest.fixture(autouse=True)
def _no_caches_left_in_this_process():
    """The inner sessions import the same modules, so their leaks are this process's."""
    from conftest_resets import forget_home_bound_caches

    try:
        yield
    finally:
        forget_home_bound_caches()


def test_a_leak_that_would_fail_the_boot_smoke_is_cleared_between_tests(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(test_leaks=_LEAKS, test_victim=_VICTIM)
    # Root the inner session in its own directory, not at this checkout's setup.cfg.
    pytester.makeini("[pytest]\n")

    # Control: no teardown call, so the victim inherits every cache the first file filled.
    # ``test_leaks.py`` sorts before ``test_victim.py``, and ``-p no:randomly`` keeps it so.
    control = pytester.runpytest_inprocess(*_ARGS)
    control.assert_outcomes(passed=1, failed=1)
    control.stdout.fnmatch_lines(["*FAILED test_victim.py::test_the_caches_are_empty*"])

    # Treatment: the real function on the real hook, wired as the rootdir conftest wires it.
    treatment = pytester.runpytest_inprocess(*_ARGS, plugins=[_teardown_reset()])
    treatment.assert_outcomes(passed=2)
