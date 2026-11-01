"""The host-grants seam (``kiro_crew.apps.builtin_host_grants``).

The core names no built-in app; what a shipped built-in asks of the host is data in its
own ``host_grants`` submodule. These tests pin three things: the seam is empty when no
built-in ships grants, a row can grant only its own app inside its own namespace, and
the answers the core reads for Dev Fleet are exactly the ones the gateway applies.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

from kiro_crew.apps import builtin_host_grants as seam

_DEV_FLEET_STRICT = frozenset(
    {
        "/api/apps/dev-fleet/pod/up",
        "/api/apps/dev-fleet/pod/restart",
        "/api/apps/dev-fleet/pod/down",
        "/api/apps/dev-fleet/pod/status",
        "/api/apps/dev-fleet/pod/list",
    }
)
_DEV_FLEET_RELAY = frozenset({"/api/apps/dev-fleet/pod-token", "/apps/dev-fleet/api/pod/token"})


@pytest.fixture
def fresh_cache():
    """The loader caches once per process; a case that changes its inputs re-reads."""
    seam.builtin_host_grants.cache_clear()
    yield
    seam.builtin_host_grants.cache_clear()


def test_empty_when_no_builtin_ships_grants(monkeypatch, fresh_cache) -> None:
    from kiro_crew.apps import builtins

    monkeypatch.setattr(builtins, "BUILTIN_NAMES", ["auto_research", "meetings"])
    assert seam.builtin_host_grants() == ()
    assert seam.build_rlimit_apps() == frozenset()
    assert seam.bound_port_apps() == frozenset()
    assert seam.backend_env_names() == ()
    assert seam.backend_env_prefixes() == ()
    assert seam.config_top_keys() == frozenset()
    assert seam.strict_internal_paths() == frozenset()
    assert seam.relay_credential_paths() == frozenset()


def test_a_listed_package_absent_from_the_build_grants_nothing(monkeypatch, fresh_cache) -> None:
    from kiro_crew.apps import builtins

    monkeypatch.setattr(builtins, "BUILTIN_NAMES", ["no_such_builtin"])
    assert seam.builtin_host_grants() == ()


def test_dev_fleet_grants_are_what_the_gateway_applies(fresh_cache) -> None:
    assert seam.build_rlimit_apps() == frozenset({"dev-fleet"})
    assert seam.bound_port_apps() == frozenset({"dev-fleet"})
    assert seam.backend_env_names() == ("KIROCREW_DEVFLEET_REPO",)
    assert seam.backend_env_prefixes() == ("KIROCREW_DEVFLEET_BIN_",)
    assert seam.config_top_keys() == frozenset({"dev_fleet"})
    assert seam.strict_internal_paths() == _DEV_FLEET_STRICT
    assert seam.relay_credential_paths() == _DEV_FLEET_RELAY


def test_the_core_tables_carry_the_grants() -> None:
    """The three core tables a grant feeds hold it, read at their own import."""
    from kiro_crew.config import validation
    from kiro_crew.dashboard import server
    from kiro_crew.dashboard.kiro_relay import limits

    assert _DEV_FLEET_STRICT <= server._STRICT_INTERNAL_API_PATHS
    assert "/api/apps/dev-fleet/pod" not in server._STRICT_INTERNAL_API_PATHS
    assert _DEV_FLEET_RELAY <= limits._LOCAL_CREDENTIAL_EXACT
    assert "dev_fleet" in validation._APP_OWNED_TOP_KEYS


def test_loading_grants_does_not_import_the_route_modules() -> None:
    """The seam is read in the MCP servers and the config loader: Dev Fleet's package
    ``__init__`` must stay light, loading its route modules only when they mount.
    A fresh interpreter, because this worker has long since imported them."""
    probe = (
        "import sys\n"
        "from kiro_crew.apps import builtin_host_grants as g\n"
        "assert g.bound_port_apps() == {'dev-fleet'}\n"
        "heavy = [m for m in sys.modules if m.startswith('kiro_crew.apps.builtins.dev_fleet.')]\n"
        "print(sorted(heavy))\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "['kiro_crew.apps.builtins.dev_fleet.host_grants']"


_VALID = {"app": "dev-fleet"}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (["not", "a", "mapping"], "must be a mapping"),
        ({**_VALID, "surprise": True}, "unknown keys"),
        ({"app": "issue-radar"}, "may grant only itself"),
        ({**_VALID, "bound_port": "yes"}, "must be a bool"),
        ({**_VALID, "config_top_keys": "dev_fleet"}, "sequence of non-empty strings"),
        ({**_VALID, "backend_env_names": ("AWS_SECRET_ACCESS_KEY",)}, "only KIROCREW_"),
        ({**_VALID, "backend_env_prefixes": ("KIROCREW_",)}, "only KIROCREW_"),
        ({**_VALID, "strict_internal_paths": ("/api/send-message",)}, "own namespace"),
        (
            {**_VALID, "strict_internal_paths": ("/api/apps/issue-radar/x",)},
            "own namespace",
        ),
        ({**_VALID, "strict_internal_paths": ("/api/apps/dev-fleet/",)}, "own namespace"),
        ({**_VALID, "relay_credential_paths": ("/api/token/local",)}, "own namespace"),
    ],
)
def test_a_malformed_or_overreaching_row_raises(raw, message) -> None:
    with pytest.raises(ValueError, match=message):
        seam._parse("dev_fleet", raw)


def test_a_grants_module_without_the_mapping_raises(monkeypatch, fresh_cache) -> None:
    """A shipped grants file that stopped defining ``HOST_GRANTS`` is a loud fault, not
    an app silently losing what it asked for."""
    real = seam.importlib.import_module

    def fake(name: str):
        if name.endswith(".dev_fleet.host_grants"):
            return types.ModuleType(name)
        return real(name)

    monkeypatch.setattr(seam.importlib, "import_module", fake)
    with pytest.raises(ValueError, match="must be a mapping"):
        seam.builtin_host_grants()


def test_every_deferred_bound_port_backend_is_rechecked_and_spawned_in_order(monkeypatch) -> None:
    """The deferral is per app, not one flag: each held-back backend is re-checked on its
    own, a disabled one is skipped, and the rest spawn together in boot order."""
    import kiro_crew.apps.backend as bmod
    from kiro_crew.apps import manager

    spawned: list[list[str]] = []
    monkeypatch.setattr(bmod, "_DEFERRED_BOUND_PORT_APPS", ("app-a", "app-off", "app-b"))
    monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: name != "app-off")
    monkeypatch.setattr(manager, "_app_activation_denied", lambda name: None)
    monkeypatch.setattr(
        bmod, "_start_backends_concurrently", lambda names: spawned.append(names) or names
    )
    assert bmod.start_deferred_app_backends() == ["app-a", "app-b"]
    assert spawned == [["app-a", "app-b"]]
    assert bmod._DEFERRED_BOUND_PORT_APPS == ()


def test_the_boot_wave_holds_back_exactly_the_bound_port_apps(monkeypatch) -> None:
    import kiro_crew.apps.backend as bmod

    monkeypatch.setattr(bmod, "_DEFERRED_BOUND_PORT_APPS", ())
    monkeypatch.setattr(bmod._host_grants, "bound_port_apps", lambda: frozenset({"app-b"}))
    monkeypatch.setattr(bmod, "_reap_stale_app_backends", lambda: 0)
    monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
    monkeypatch.setattr(bmod, "get_app_manifest", lambda name: None)
    apps = [
        {
            "name": n,
            "enabled": True,
            "origin": "builtin",
            "manifest": {"backend": {"entryPoint": "s.py"}},
        }
        for n in ("app-a", "app-b", "app-c")
    ]
    monkeypatch.setattr(bmod, "list_apps", lambda: apps)
    started: list[list[str]] = []
    monkeypatch.setattr(
        bmod, "_start_backends_concurrently", lambda names: started.append(names) or names
    )
    bmod.start_enabled_app_backends()
    assert started == [["app-a", "app-c"]]
    assert bmod._DEFERRED_BOUND_PORT_APPS == ("app-b",)
