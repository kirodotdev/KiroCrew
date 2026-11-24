"""A gateway started from inside an app backend does not inherit the backend's identity.

The Dev Fleet backend restarts the gateway through detached spawns that pass no
environment, so the new gateway process inherits the backend's variables. With
``KIROCREW_PROXY_SECRET`` set, ``sel()`` returns the backend's route client, and the
gateway would then post its own security rows to its own route instead of writing them.
``cli.main`` drops the four variables on the ``gateway`` command only: any other
command keeps them, because an app backend runs through the same entry point.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

_BACKEND_ENV = {
    "KIROCREW_SPAWNED": "1",
    "KIROCREW_APP_NAME": "plain-app",
    "KIROCREW_GATEWAY_ORIGIN": "http://127.0.0.1:8123",
    "KIROCREW_PROXY_SECRET": "per-app-secret",
}


@pytest.fixture(autouse=True)
def _no_route_client(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Another process owns the route client, so no ``sel()`` call here can start one.

    A client built from these variables queues its rows for a gateway that is not
    there, and its sender thread logs the dropped row into whichever test runs next.
    """
    _floor_monkeypatch.setenv("KIROCREW_SEL_CLIENT_PID", str(os.getpid() + 1))


def _run_main(argv: list[str], target: str) -> dict[str, str | None]:
    """Run ``cli.main`` up to *target*, which records the backend variables and stops."""
    seen: dict[str, str | None] = {}

    def _capture(*_a: object, **_k: object) -> None:
        seen.update({name: os.environ.get(name) for name in _BACKEND_ENV})
        raise SystemExit(0)

    with patch.object(sys, "argv", argv), patch(target, _capture):
        from kiro_crew.cli import main

        with pytest.raises(SystemExit):
            main()
    return seen


def test_gateway_command_drops_the_backend_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _BACKEND_ENV.items():
        monkeypatch.setenv(name, value)

    seen = _run_main(["kirocrew", "gateway"], "kiro_crew.cli._refuse_unpersistable_data_home")

    assert seen == {name: None for name in _BACKEND_ENV}


def test_gateway_command_writes_its_own_security_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.sel as sel_mod

    class _Writer:
        pass

    for name, value in _BACKEND_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sel_mod, "SecurityEventLog", _Writer)

    def _stop(*_a: object, **_k: object) -> None:
        raise SystemExit(0)

    with (
        patch.object(sys, "argv", ["kirocrew", "gateway"]),
        patch("kiro_crew.cli._refuse_unpersistable_data_home", _stop),
    ):
        from kiro_crew.cli import main

        with pytest.raises(SystemExit):
            main()

    assert type(sel_mod.sel()) is _Writer


def test_other_commands_keep_the_backend_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _BACKEND_ENV.items():
        monkeypatch.setenv(name, value)

    seen = _run_main(["kirocrew", "cron", "list"], "kiro_crew.cli_commands._cron")

    assert seen == _BACKEND_ENV
