"""The socket-state tripwire in ``test/conftest.py`` restores a leak and names the test."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

# ``pytester`` is not registered by default; a test module may request it by name.
pytest_plugins = ("pytester",)


def _tripwire(config: pytest.Config) -> object:
    """The two hooks of THIS directory's conftest, as a plugin for an inner session."""
    here = Path(__file__).resolve().with_name("conftest.py")
    conftest = next(
        p
        for p in config.pluginmanager.get_plugins()
        if getattr(p, "__file__", None) and Path(p.__file__).resolve() == here
    )
    hooks = {
        name: staticmethod(getattr(conftest, name))
        for name in ("pytest_runtest_setup", "pytest_runtest_teardown")
    }
    return type("SocketTripwire", (), hooks)()


def test_a_leaked_address_family_fails_that_test_and_is_restored(
    pytester: pytest.Pytester, request: pytest.FixtureRequest
) -> None:
    original = socket.AF_INET
    pytester.makepyfile("""
        import socket

        def test_leaks():
            socket.AF_INET = 99

        def test_monkeypatched_is_not_a_leak(monkeypatch):
            monkeypatch.setattr(socket, "AF_INET", 98)
        """)
    # Root the inner session in its own directory, not at this checkout's setup.cfg.
    pytester.makeini("[pytest]\n")
    result = pytester.runpytest_inprocess(
        "-p", "no:cacheprovider", "-n0", "-o", "addopts=", plugins=[_tripwire(request.config)]
    )
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*ERROR at teardown of test_leaks*", "*socket.AF_INET rebound*"])
    assert socket.AF_INET is original


def test_a_monkeypatched_socket_attribute_is_not_a_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    # Passes only if this suite's tripwire reads the state after ``monkeypatch`` undoes.
    monkeypatch.setattr(socket, "socketpair", lambda *a, **k: None)
