"""Gateway boot registers each enabled app's resources once, and only after its vet.

Runs the real :func:`~kiro_crew.dashboard.server.start_dashboard` through the
``test_dashboard_server_startup_coverage`` harness (no listener, outside-process work
stubbed), with the real ``start_enabled_app_backends`` in place of its spy. The spawn
collaborators are stubbed as ``test_apps_backend_coverage_boot.py::boot_env`` stubs
them, and every bridge step that writes outside the data home (prune, register,
legacy-MCP scrub, skill reconcile, revoke) is a recorder, so the count of
``register_app`` calls per app across the whole boot is the thing under test.
"""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest
import test_dashboard_server_startup_coverage as startup_cov

import kiro_crew.apps.backend as bmod

_ADMITTED = "boot-admitted"
_DENIED = "boot-denied"


def _app(name: str) -> dict[str, Any]:
    return {"name": name, "enabled": True, "origin": "builtin", "manifest": {}}


@pytest.fixture()
def boot_registrations(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record every app registration and revocation the boot makes, by app name."""

    seen: dict[str, Any] = {"register": Counter(), "revoked": Counter()}
    apps = [_app(_ADMITTED), _app(_DENIED)]

    def _register(name: str, *_a: Any, **_kw: Any) -> Any:
        seen["register"][name] += 1
        return SimpleNamespace(agents=[], skills=[], errors=[])

    def _revoke(name: str) -> int:
        seen["revoked"][name] += 1
        return 0

    # Both readers of the enabled-app list: the bridges family and the backend pass.
    monkeypatch.setattr("kiro_crew.apps.bridges.list_apps", lambda: list(apps))
    monkeypatch.setattr(bmod, "list_apps", lambda: list(apps))
    monkeypatch.setattr("kiro_crew.apps.bridges.register_app", _register)
    monkeypatch.setattr("kiro_crew.apps.bridges._registration_source", lambda _n: (None, None))
    monkeypatch.setattr("kiro_crew.apps.bridges._scrub_legacy_shared_mcp", lambda _n: 0)
    monkeypatch.setattr("kiro_crew.apps.bridges.reconcile_app_skills", lambda _n: None)
    monkeypatch.setattr("kiro_crew.apps.bridges._deregister_agents", _revoke)
    monkeypatch.setattr("kiro_crew.apps.bridges._deregister_skills", lambda _n: 0)
    monkeypatch.setattr("kiro_crew.apps.bridges._deregister_mcp_servers", lambda _n: 0)

    monkeypatch.setattr(bmod, "_reap_stale_app_backends", lambda: 0)
    monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
    monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: None)
    monkeypatch.setattr(bmod, "shipped_builtin_app_root", lambda _name: None)
    monkeypatch.setattr(bmod, "app_admission_denied", lambda _name, **_kw: None)
    monkeypatch.setattr(bmod, "app_execution_denied", lambda _name, **_kw: None)
    monkeypatch.setattr(bmod, "start_app_backend", lambda _name: None)
    monkeypatch.setattr(
        bmod,
        "_app_activation_denied",
        lambda name, **_kw: "not in allowlist" if name == _DENIED else None,
    )

    real_start = bmod.start_enabled_app_backends
    real_neutralise = startup_cov._neutralise_outside_process_work

    def _neutralise_but_run_the_real_backend_pass(mp: pytest.MonkeyPatch) -> dict[str, Any]:
        spies = real_neutralise(mp)
        spies["start_enabled_app_backends"].side_effect = real_start
        return spies

    monkeypatch.setattr(
        startup_cov,
        "_neutralise_outside_process_work",
        _neutralise_but_run_the_real_backend_pass,
    )
    return seen


@pytest.mark.asyncio
async def test_boot_registers_an_admitted_app_once_and_a_denied_app_never(
    tmp_path, monkeypatch, boot_registrations
) -> None:
    async with startup_cov._dashboard(tmp_path, monkeypatch) as (_runner, _state, spies):
        assert spies["start_enabled_app_backends"].call_count == 1
    registered = boot_registrations["register"]
    assert registered[_ADMITTED] == 1, (
        f"an admitted enabled app must be registered exactly once per boot, got "
        f"{registered[_ADMITTED]}"
    )
    assert registered[_DENIED] == 0, "a governance-denied app must never be registered"
    assert boot_registrations["revoked"][_DENIED] == 1
    assert boot_registrations["revoked"][_ADMITTED] == 0
