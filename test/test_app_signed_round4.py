"""Malformed policy and signed cache failures remain contained at their owners."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from test_app_signed_round3 import force_posix_lifecycle, plant_cache, sign
from test_app_signed_round3 import signed_home as _signed_home
from test_app_signed_round3 import write_app

from kiro_crew import platform_compat
from kiro_crew.apps import admission, lifecycle_scripts, module_loader, signed_imports
from kiro_crew.testing.links import make_dir_link
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

signed_home = _signed_home


@pytest.mark.parametrize(
    "document",
    [
        {"trust_keys": ["publisher"]},
        {"trust_keys": "publisher"},
        {"trust_keys": 7},
        {"trust_keys": []},
        {"trust_keys": ""},
        {"trust_keys": False},
    ],
)
def test_malformed_policy_fails_closed_without_raising(tmp_path, monkeypatch, document):
    monkeypatch.setattr(admission, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(admission, "sel", Mock(side_effect=OSError("audit unavailable")))
    (tmp_path / "app_admission.json").write_text(json.dumps(document), encoding="utf-8")
    policy = admission.load_app_admission_policy()
    assert policy == admission._fail_closed_policy()
    assert admission.signature_required()


@pytest.mark.parametrize("value", [None, "false", 0, [], {}])
def test_malformed_signature_switch_keeps_restriction(value):
    assert admission.AppAdmissionPolicy.from_dict({"require_signature": value}).require_signature


def test_no_policy_does_not_prepare_any_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "config_dir", lambda: tmp_path)
    clear = Mock(side_effect=AssertionError("no-policy path touched cache"))
    monkeypatch.setattr(signed_imports, "clear_app_bytecode", clear)
    assert admission.load_app_admission_policy() == admission.AppAdmissionPolicy.open_default()
    assert signed_imports.pycache_prefix_env("probe", tmp_path) == {}
    assert signed_imports.registry_pycache_prefix_env("probe", tmp_path) == {}
    clear.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["on_enable", "on_disable", "on_uninstall"])
@pytest.mark.parametrize("failure", [OSError("cache held"), ValueError("cache unavailable")])
async def test_cache_refusal_is_failed_result_and_cleans_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, failure: Exception
):
    root = tmp_path / "probe"
    root.mkdir()
    wrapper = tmp_path / "sandbox-profile"
    wrapper.write_text("profile", encoding="utf-8")
    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle_scripts, "_installed_content_refusal", lambda *a: None)
    monkeypatch.setattr(lifecycle_scripts, "_signed_execution_root", lambda *a: root)
    monkeypatch.setattr(lifecycle_scripts.platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(
        lifecycle_scripts, "wrap_argv_async", AsyncMock(return_value=(["unused"], str(wrapper)))
    )
    monkeypatch.setattr(lifecycle_scripts, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(lifecycle_scripts, "_pycache_prefix_env", Mock(side_effect=failure))
    spawn = AsyncMock(side_effect=AssertionError("refused script executed"))
    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", spawn)
    result = await asyncio.wait_for(
        lifecycle_scripts.run_lifecycle_script("probe", "unused", action=action),
        FALLBACK_TIMEOUT_SECS,
    )
    assert result["failed"] is True
    assert "cache" in result["output"]
    spawn.assert_not_awaited()
    assert not wrapper.exists()


@pytest.mark.parametrize("directory", ["data", ".venv", "node_modules"])
def test_pinned_cache_walk_propagates_listing_failure(tmp_path, monkeypatch, directory):
    root = tmp_path / "app"
    cache = root / directory / "__pycache__"
    cache.mkdir(parents=True)
    marker = cache / "keep.pyc"
    marker.write_bytes(b"cached")
    blocked = str(cache.parent)
    names = signed_imports.PinnedDirectory.names

    def fail_names(held):
        if held.path == blocked:
            raise PermissionError("cache listing refused")
        return names(held)

    monkeypatch.setattr(signed_imports.PinnedDirectory, "names", fail_names)
    with pytest.raises(PermissionError, match="cache listing refused"):
        signed_imports.clear_app_bytecode(root)
    assert marker.read_bytes() == b"cached"


def test_pinned_mirror_unlink_failure_is_not_swallowed(tmp_path, monkeypatch):
    import importlib.util
    import sys

    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "mirror"))
    mirror = Path(importlib.util.cache_from_source(str(root / "helper.py"))).parent
    mirror.mkdir(parents=True)
    marker = mirror / "keep.pyc"
    marker.write_bytes(b"cached")
    unlink = signed_imports.PinnedDirectory.unlink

    def fail_unlink(held, name):
        if held.path == str(mirror) and name == marker.name:
            raise PermissionError("mirror cleanup refused")
        return unlink(held, name)

    monkeypatch.setattr(signed_imports.PinnedDirectory, "unlink", fail_unlink)
    with pytest.raises(PermissionError, match="mirror cleanup refused"):
        signed_imports.clear_app_bytecode(root)
    assert marker.read_bytes() == b"cached"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["enable", "uninstall"])
async def test_cache_refusal_reaches_lifecycle_compensation(tmp_path, monkeypatch, operation):
    from aiohttp.test_utils import TestClient, TestServer
    from apps_routes_coverage_helpers import APP, _install, _make_app, _setup_env

    from kiro_crew.apps import manager, routes

    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, setup={"onEnable": "unused", "onUninstall": "unused"})
    root = manager.app_dir(APP)
    monkeypatch.setattr(lifecycle_scripts, "_installed_content_refusal", lambda *a: None)
    monkeypatch.setattr(lifecycle_scripts, "_signed_execution_root", lambda *a: root)
    force_posix_lifecycle(monkeypatch)
    monkeypatch.setattr(
        lifecycle_scripts, "wrap_argv_async", AsyncMock(return_value=(["unused"], None))
    )
    monkeypatch.setattr(lifecycle_scripts, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(
        lifecycle_scripts, "_pycache_prefix_env", Mock(side_effect=PermissionError("cache held"))
    )
    spawn = AsyncMock(side_effect=AssertionError("refused script executed"))
    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", spawn)
    monkeypatch.setattr(routes, "start_app_backend", Mock())
    stop = Mock()
    monkeypatch.setattr(routes, "stop_app_backend", stop)
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        response = await client.post(f"/api/apps/{APP}/{operation}", json={})
        body = await response.json()
    if operation == "enable":
        assert response.status == 400, body
        assert body["code"] == "on_enable_failed"
        assert manager.get_app(APP)["enabled"] is False
    else:
        assert response.status == 200, body
        assert "cache held" in body["uninstall_log"]
        assert manager.get_app(APP) is None
    stop.assert_called()
    spawn.assert_not_awaited()


@pytest.mark.parametrize("policy", [True, False])
@pytest.mark.parametrize("prepared", [True, False])
def test_linked_ancestor_hook_clears_lexical_cache_mirror(
    tmp_path: Path, signed_home: Path, monkeypatch: pytest.MonkeyPatch, policy: bool, prepared: bool
) -> None:
    root = signed_home / "apps" / "probe"
    write_app(root)
    (root / "helper.py").write_text("VALUE = 'source'\n", encoding="utf-8")
    (root / "hook.py").write_text(
        "import importlib.util\nfrom pathlib import Path\n"
        "spec = importlib.util.spec_from_file_location(\n"
        "    'lexical_helper', Path(__file__).parent / 'helper.py')\n"
        "helper = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(helper)\n"
        "def run():\n    return helper.VALUE\n",
        encoding="utf-8",
    )
    sign(root)
    link = tmp_path / "linked-home"
    make_dir_link(link, signed_home)
    lexical_root = link / "apps" / "probe"
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "mirror"))
    # Keep the post-cleanup assertion independent of whether Python writes a fresh cache.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    plant_cache(lexical_root / "helper.py", tmp_path)
    plant_cache(root / "helper.py", tmp_path)
    caches = [
        Path(importlib.util.cache_from_source(str(path / "helper.py")))
        for path in (root, lexical_root)
    ]
    assert all(cache.is_file() for cache in caches)
    if not policy:
        (signed_home / "app_admission.json").unlink()
    monkeypatch.setattr(module_loader, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(module_loader, "_prepared_verdicts", {})
    try:
        assert admission.signed_tree_applies("probe", lexical_root / "hook.py") is policy
        if prepared:
            refusal, manifest = module_loader.verify_hook_source("probe", lexical_root, "hook:run")
            assert refusal is None
            assert (manifest is not None) is policy
        run = module_loader.load_app_module("probe", lexical_root, "hook:run")
        assert run() == ("source" if policy else "cached")
        assert all(cache.exists() is (not policy) for cache in caches)
    finally:
        module_loader.unload_app_modules("probe")


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX directory permissions")
@pytest.mark.parametrize("location", ["app", "mirror"])
def test_traverse_only_ancestor_does_not_block_cache_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    ancestor = tmp_path / "traverse-only"
    ancestor.mkdir()
    root = (ancestor if location == "app" else tmp_path) / "app"
    root.mkdir()
    prefix = (ancestor if location == "mirror" else tmp_path) / "mirror"
    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    mirror = Path(importlib.util.cache_from_source(str(root / "helper.py"))).parent
    mirror.mkdir(parents=True)
    (mirror / "helper.pyc").write_bytes(b"cached")
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "helper.pyc").write_bytes(b"cached")
    ancestor.chmod(0o111)
    try:
        with pytest.raises(PermissionError):
            list(ancestor.iterdir())
        signed_imports.clear_app_bytecode(root)
        assert not cache.exists()
        assert not mirror.exists()
    finally:
        ancestor.chmod(0o700)


_ADMISSION_INSTALLS = [
    "genuine-builtin",
    "adopted-builtin",
    "forged-builtin",
    "forged-shadow",
    "forged-backendless",
    "local-shadow",
    "registry-shadow",
    "third-party",
]


def _admission_install(home, monkeypatch, kind, *, enabled):
    from kiro_crew.apps import builtin_adoption, execution, manager
    from kiro_crew.apps.manager_records import InstalledApp

    shipped = home / "shipped"
    shipped.mkdir()
    if kind not in {"forged-builtin", "third-party"}:
        write_app(shipped / "probe", backend={"entryPoint": "probe.server"})
        (shipped / "probe" / "server.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(execution, "_builtin_manifest_sources", lambda: (shipped,))
    monkeypatch.setattr(builtin_adoption, "builtin_released", lambda n: kind == "adopted-builtin")
    if kind == "forged-backendless":
        # A forged record that drops the shipped backend from its own manifest.
        manifest = write_app(home / "apps" / "probe")
    else:
        manifest = write_app(
            home / "apps" / "probe",
            backend={"entryPoint": "probe.server" if kind == "genuine-builtin" else "server.py"},
        )
    (home / "apps" / "probe" / "server.py").write_text("", encoding="utf-8")
    origin = (
        "builtin"
        if kind in {"genuine-builtin", "forged-builtin", "forged-shadow", "forged-backendless"}
        else "registry" if kind in {"adopted-builtin", "registry-shadow"} else "local"
    )
    source = (
        "builtin"
        if kind in {"genuine-builtin", "forged-backendless"}
        else "registry:probe" if origin == "registry" else "local"
    )
    manager._write_installed(
        "probe",
        InstalledApp(name="probe", version="1.0.0", origin=origin, source=source, enabled=enabled),
    )
    return manifest


def _ban_probe(home):
    (home / "app_admission.json").write_text(json.dumps({"banned": ["probe"]}), encoding="utf-8")


@pytest.mark.parametrize("kind", _ADMISSION_INSTALLS)
@pytest.mark.parametrize("policy", [True, False])
def test_enable_admission_uses_active_builtin_install(signed_home, monkeypatch, kind, policy):
    from kiro_crew.apps import manager

    _admission_install(signed_home, monkeypatch, kind, enabled=False)
    if policy:
        _ban_probe(signed_home)
    else:
        (signed_home / "app_admission.json").unlink()
    monkeypatch.setattr(manager, "_app_activation_denied", lambda *a, **k: None)
    monkeypatch.setattr(manager, "app_execution_denied", lambda *a, **k: None)
    admit = Mock(wraps=admission.app_admission_denied)
    monkeypatch.setattr(manager, "app_admission_denied", admit)

    result = manager.enable_app("probe")

    allowed = not policy or kind == "genuine-builtin"
    assert result.ok is allowed
    assert manager._read_installed("probe").enabled is allowed
    assert admit.call_count == (0 if kind == "genuine-builtin" else 1)
    if not allowed:
        assert "banned" in result.error


@pytest.mark.parametrize("kind", _ADMISSION_INSTALLS)
@pytest.mark.parametrize("policy", [True, False])
@pytest.mark.parametrize("ban_at", ["resource-pass", "backend-pass"])
def test_boot_admission_uses_active_builtin_install(signed_home, monkeypatch, kind, policy, ban_at):
    from kiro_crew.apps import bridges, manager
    from kiro_crew.apps.backend_runtime import startup

    if kind == "forged-backendless" and ban_at == "backend-pass":
        pytest.skip("a backendless install has no backend pass to re-vet")
    manifest = _admission_install(signed_home, monkeypatch, kind, enabled=True)
    (signed_home / "app_admission.json").unlink()
    if policy and ban_at == "resource-pass":
        _ban_probe(signed_home)
    apps = [{"name": "probe", "enabled": True, "manifest": manifest.to_dict()}]
    monkeypatch.setattr(startup, "list_apps", lambda: apps)
    monkeypatch.setattr(startup, "_reap_stale_app_backends", lambda: None)
    monkeypatch.setattr(startup, "_app_activation_denied", lambda *a, **k: None)
    monkeypatch.setattr(startup, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(startup, "_app_enabled_state", manager.app_enabled_state)
    admit = Mock(wraps=admission.app_admission_denied)
    monkeypatch.setattr(startup, "app_admission_denied", admit)

    def register(name):
        if policy and ban_at == "backend-pass":
            _ban_probe(signed_home)
        return bridges.RegistrationResult()

    registration = Mock(side_effect=register)
    monkeypatch.setattr(bridges, "register_app", registration)
    monkeypatch.setattr(bridges, "reconcile_app_skills", lambda n: [])
    revoked = [Mock(), Mock(), Mock()]
    for name, revoke in zip(
        ("_deregister_agents", "_deregister_skills", "_deregister_mcp_servers"), revoked
    ):
        monkeypatch.setattr(bridges, name, revoke)
    spawned = Mock(return_value=[])
    monkeypatch.setattr(startup, "_start_backends_concurrently", spawned)

    startup.start_enabled_app_backends()

    allowed = not policy or kind == "genuine-builtin"
    has_backend = kind != "forged-backendless"
    assert spawned.call_args.args[0] == (["probe"] if allowed and has_backend else [])
    assert apps[0]["enabled"] is allowed
    checks = 1 if (policy and ban_at == "resource-pass") or not has_backend else 2
    assert admit.call_count == (0 if kind == "genuine-builtin" else checks)
    assert registration.call_count == (0 if not allowed and ban_at == "resource-pass" else 1)
    for revoke in revoked:
        assert revoke.call_count == (0 if allowed else 1)


def _activation_reader_app(home, monkeypatch, *, builtin, policy):
    from kiro_crew.apps import bridges, execution, manager
    from kiro_crew.apps.manager_records import InstalledApp

    shipped = home / "shipped"
    shipped.mkdir()
    monkeypatch.setattr(execution, "_builtin_manifest_sources", lambda: (shipped,))
    root = shipped / "probe" if builtin else home / "app-sources" / "probe"
    write_app(
        root,
        backend={"hooks": {"routes": "hook:routes", "on_startup": "hook:start"}},
        crons=[{"name": "tick", "every": 300, "message": "test"}],
    )
    (root / "hook.py").write_text(
        "def routes(ctx): return []\ndef start(ctx): pass\n", encoding="utf-8"
    )
    manifest = sign(root)
    metadata = home / "apps" / "probe"
    metadata.mkdir(parents=True)
    (metadata / "app.json").write_bytes((root / "app.json").read_bytes())
    manager._write_installed(
        "probe",
        InstalledApp(
            name="probe",
            version="1.0.0",
            enabled=True,
            origin="builtin" if builtin else "registry",
            source="builtin" if builtin else "registry:probe",
            resources="gateway" if builtin else "app",
        ),
    )
    bridges._register_crons("probe", manifest)
    if policy:
        document = json.loads((home / "app_admission.json").read_text(encoding="utf-8"))
        document["banned"] = ["probe"]
        (home / "app_admission.json").write_text(json.dumps(document), encoding="utf-8")
    else:
        (home / "app_admission.json").unlink()
    assert execution.is_installed_builtin_app("probe") is builtin
    return manifest, metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["boot", "enable", "reconcile"])
@pytest.mark.parametrize("builtin", [False, True])
@pytest.mark.parametrize("policy", [False, True])
async def test_hook_activation_readers_reapply_admission(
    signed_home, monkeypatch, reader, builtin, policy
):
    from types import SimpleNamespace

    from kiro_crew.apps import hooks_integration as hooks
    from kiro_crew.apps import manager
    from kiro_crew.apps.manager_records import InstalledApp

    manifest, metadata = _activation_reader_app(
        signed_home, monkeypatch, builtin=builtin, policy=policy
    )
    before = (metadata / "installed.json").read_bytes()
    info = {"name": "probe", "enabled": True, "manifest": manifest.to_dict()}
    healthy_root = signed_home / "apps" / "z-healthy"
    healthy_manifest = write_app(healthy_root, name="z-healthy")
    sign(healthy_root)
    manager._write_installed("z-healthy", InstalledApp(name="z-healthy", enabled=True))
    healthy_info = {"name": "z-healthy", "enabled": True, "manifest": healthy_manifest.to_dict()}
    registry = Mock(register_app_routes=AsyncMock(return_value=[]))
    dispatcher = Mock(_invoke=AsyncMock(return_value=True), cache_shutdown_for=AsyncMock())
    register = AsyncMock(return_value=[])
    disarm = AsyncMock(return_value=1)
    monkeypatch.setattr(hooks, "_route_registry", registry)
    monkeypatch.setattr(hooks, "_lifecycle_dispatcher", dispatcher)
    monkeypatch.setattr(hooks, "list_apps", lambda: [info, healthy_info])
    monkeypatch.setattr(hooks, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(hooks, "register_app_crons_with_service", register)
    monkeypatch.setattr(hooks, "disarm_app_crons_for_execution", disarm)
    monkeypatch.setattr(hooks, "record_hook_antichurn_signature", AsyncMock())
    monkeypatch.setattr(hooks, "record_loaded_hook_signature", AsyncMock())
    monkeypatch.setattr(hooks, "_publish_hook_health", lambda *a: None)
    monkeypatch.setattr(hooks, "reconcile_all", lambda: 0)
    monkeypatch.setattr(
        hooks,
        "_prepare_app_context_from_info",
        AsyncMock(return_value=SimpleNamespace(health=SimpleNamespace(status="healthy"), job=None)),
    )
    allowed = builtin or not policy
    if reader == "reconcile":
        assert (hooks.hook_enable_denied("probe") is None) is allowed
    else:
        service = object()
        operation = (
            hooks.on_gateway_startup(cron_service=service)
            if reader == "boot"
            else hooks.on_app_enable("probe", info, cron_service=service)
        )
        await asyncio.wait_for(operation, FALLBACK_TIMEOUT_SECS)
        assert registry.register_app_routes.await_count == int(allowed)
        assert dispatcher._invoke.await_count == int(allowed)
        assert [call.args[0] for call in register.await_args_list] == (
            (["probe"] if allowed else []) + (["z-healthy"] if reader == "boot" else [])
        )
        assert disarm.await_count == int(not allowed)
    assert (metadata / "installed.json").read_bytes() == before


@pytest.mark.parametrize("reader", ["register", "restore"])
@pytest.mark.parametrize("builtin", [False, True])
@pytest.mark.parametrize("policy", [False, True])
def test_cron_activation_readers_reapply_admission(
    signed_home, monkeypatch, reader, builtin, policy
):
    from kiro_crew.apps import bridges

    _, metadata = _activation_reader_app(signed_home, monkeypatch, builtin=builtin, policy=policy)
    before = (metadata / "installed.json").read_bytes()
    monkeypatch.setattr(bridges, "app_execution_denied", lambda *a, **k: None)
    allowed = builtin or not policy
    if reader == "register":
        defs, _ = bridges._admitted_app_cron_defs("probe")
        assert [d["name"] for d in defs] == (["probe/tick"] if allowed else [])
    else:
        assert bridges._cron_restore_denied("probe") is (not allowed)
    assert (metadata / "installed.json").read_bytes() == before


@pytest.mark.parametrize("kind", _ADMISSION_INSTALLS)
def test_later_activation_admission_exempts_only_active_builtin(signed_home, monkeypatch, kind):
    from kiro_crew.apps import bridges

    _admission_install(signed_home, monkeypatch, kind, enabled=True)
    _ban_probe(signed_home)
    reason = bridges._activation_admission_denied("probe", action="cron_register", caller="test")
    assert (reason is None) is (kind == "genuine-builtin")


@pytest.mark.parametrize("audit_fails", [False, True])
@pytest.mark.parametrize("vet_fails", [False, True])
def test_later_activation_admission_contains_failures(
    signed_home, monkeypatch, audit_fails, vet_fails
):
    from kiro_crew import sel as audit_module
    from kiro_crew.apps import bridges, manager

    _, metadata = _activation_reader_app(signed_home, monkeypatch, builtin=False, policy=True)
    before = (metadata / "installed.json").read_bytes()
    audit = Mock()
    if audit_fails:
        audit.log_api_access.side_effect = OSError("audit unavailable")
    monkeypatch.setattr(audit_module, "sel", lambda: audit)
    if vet_fails:
        monkeypatch.setattr(
            admission, "app_admission_denied", Mock(side_effect=OSError("unreadable"))
        )
    denied = bridges._activation_admission_denied(
        "probe", action="hook_boot_register", caller="gateway"
    )
    assert denied is not None
    assert ("unavailable" if vet_fails else "banned") in denied
    audit.log_api_access.assert_called_once_with(
        caller="gateway",
        operation="app_execution_admission",
        outcome="denied",
        resources="app=probe action=hook_boot_register",
        error=denied,
    )
    assert manager._read_installed("probe").enabled
    assert (metadata / "installed.json").read_bytes() == before


def test_a_bom_prefixed_manifest_can_be_signed(signed_home):
    """``kirocrew app sign`` reads app.json the way install and verification do."""
    from kiro_crew.apps.admission import _signature_valid

    root = signed_home / "apps" / "probe"
    write_app(root)
    path = root / "app.json"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    manifest = sign(root)
    assert manifest.signature
    assert _signature_valid(manifest, admission.load_app_admission_policy())


@pytest.mark.asyncio
async def test_a_signed_self_managed_apps_startup_hook_loads_from_its_checkout(
    signed_home, monkeypatch
):
    """Lifecycle hooks load from the publisher checkout routes use, not the
    metadata-only ``apps/<name>`` directory."""
    from types import SimpleNamespace

    from kiro_crew.apps.lifecycle import LifecycleDispatcher

    _activation_reader_app(signed_home, monkeypatch, builtin=False, policy=True)
    policy_path = signed_home / "app_admission.json"
    document = json.loads(policy_path.read_text(encoding="utf-8"))
    document["banned"] = []
    policy_path.write_text(json.dumps(document), encoding="utf-8")
    assert admission.signature_required()
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)
    dispatcher = LifecycleDispatcher()
    ctx = SimpleNamespace(health=Mock())
    ok = await dispatcher._invoke("probe", "hook:start", ctx, phase="startup")
    assert ok is True
