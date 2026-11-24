"""The route client belongs to the app backend's own process.

A process the backend starts (an agent session, a review worker, a relaunched gateway)
inherits the backend's variables, including the per-app secret. If that were enough to
get the route client, its gate and governance rows would reach the log as the app's
``api_access`` claims. The backend writes its own process id on first use; any other
process holding the same variables reads a different id and keeps the real writer.
"""

from __future__ import annotations

import os

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew.apps import sel_client
from kiro_crew.apps.sel_client import KIROCREW_SEL_CLIENT_PID_ENV, from_env

_BACKEND_ENV = {
    "KIROCREW_SPAWNED": "1",
    "KIROCREW_APP_NAME": "pin-app",
    "KIROCREW_GATEWAY_ORIGIN": "http://127.0.0.1:8123",
    "KIROCREW_PROXY_SECRET": "per-app-secret",
}


@pytest.fixture(autouse=True)
def _backend_env(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _BACKEND_ENV.items():
        _floor_monkeypatch.setenv(name, value)
    _floor_monkeypatch.delenv(KIROCREW_SEL_CLIENT_PID_ENV, raising=False)
    _floor_monkeypatch.setattr(sel_client, "_clients", {})


def test_the_backend_process_gets_the_client_and_claims_it() -> None:
    client = from_env()

    assert client is not None
    assert os.environ[KIROCREW_SEL_CLIENT_PID_ENV] == str(os.getpid())
    assert from_env() is client


def test_a_child_holding_the_backend_variables_gets_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The backend's id, as a child process reads it from the inherited environment.
    monkeypatch.setenv(KIROCREW_SEL_CLIENT_PID_ENV, str(os.getpid() + 1))

    assert from_env() is None


def test_a_child_writes_its_own_security_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Writer:
        pass

    monkeypatch.setattr(sel_mod, "SecurityEventLog", _Writer)
    monkeypatch.setenv(KIROCREW_SEL_CLIENT_PID_ENV, str(os.getpid() + 1))

    assert type(sel_mod.sel()) is _Writer


def test_the_backend_sel_is_the_route_client() -> None:
    assert sel_mod.sel() is from_env()
