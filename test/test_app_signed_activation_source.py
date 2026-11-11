"""Self-managed registry activation verifies the publisher tree, not metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_app_signed_round3 import force_posix_lifecycle, sign
from test_app_signed_round3 import signed_home as _signed_home
from test_app_signed_round3 import write_app

from kiro_crew.apps import admission, manager
from kiro_crew.apps.manager_records import InstalledApp
from kiro_crew.testing.links import make_dir_link

signed_home = _signed_home


def record(home: Path, *, resources: str = "app", subdirectory: str = "") -> Path:
    root = home / "apps" / "probe"
    root.mkdir(parents=True, exist_ok=True)
    meta = InstalledApp(
        name="probe", resources=resources, source="registry:probe", sourceSubdirectory=subdirectory
    )
    (root / "installed.json").write_text(json.dumps(meta.to_dict()), encoding="utf-8")
    return root


@pytest.mark.parametrize("action", ["boot", "enable", "restart"])
@pytest.mark.parametrize("subdirectory", ["", "apps/probe"])
@pytest.mark.parametrize("tampered", [False, True])
def test_registry_self_managed_activation_checks_checkout(
    signed_home: Path, action: str, subdirectory: str, tampered: bool
) -> None:
    publisher = signed_home / "app-sources" / "probe" / subdirectory
    write_app(publisher)
    source = publisher / "helper.py"
    source.write_text("VALUE = 'publisher'\n", encoding="utf-8")
    manifest = sign(publisher)
    metadata = record(signed_home, subdirectory=subdirectory)
    (metadata / "app.json").write_bytes((publisher / "app.json").read_bytes())
    if tampered:
        source.write_text("VALUE = 'changed'\n", encoding="utf-8")
    refusal = admission.app_admission_denied("probe", manifest, action, check_version=False)
    if tampered:
        assert refusal is not None and admission._is_signature_refusal(refusal)
        assert "helper.py" in refusal
    else:
        assert refusal is None
    assert not (metadata / "helper.py").exists()


def test_gateway_managed_activation_still_checks_installed_copy(signed_home: Path) -> None:
    root = record(signed_home, resources="gateway", subdirectory="apps/probe")
    write_app(root)
    (root / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest = sign(root)
    assert admission._activation_root("probe") == root
    assert admission.app_admission_denied("probe", manifest, "boot", check_version=False) is None


@pytest.mark.parametrize("subdirectory", ["", "apps/probe", "../outside", "/outside"])
def test_unavailable_self_managed_source_refuses_without_signature_disable(
    signed_home: Path, subdirectory: str
) -> None:
    metadata = record(signed_home, subdirectory=subdirectory)
    write_app(metadata)
    (metadata / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest = sign(metadata)
    refusal = admission.app_admission_denied("probe", manifest, "boot", check_version=False)
    assert refusal is not None
    assert not admission._is_signature_refusal(refusal)


def test_linked_source_subdirectory_cannot_escape_checkout(signed_home: Path) -> None:
    outside = signed_home / "outside"
    write_app(outside)
    manifest = sign(outside)
    checkout = signed_home / "app-sources" / "probe"
    checkout.mkdir(parents=True)
    make_dir_link(checkout / "linked", outside)
    record(signed_home, subdirectory="linked")
    refusal = admission.app_admission_denied("probe", manifest, "enable", check_version=False)
    assert refusal is not None and not admission._is_signature_refusal(refusal)


def test_without_policy_activation_does_not_resolve_publisher_tree(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (signed_home / "app_admission.json").unlink()

    def unexpected(name):
        raise AssertionError("no-policy activation read publisher checkout")

    monkeypatch.setattr(admission, "_activation_root", unexpected)
    assert admission.app_admission_denied("probe", None, "boot", check_version=False) is None


def test_source_subdirectory_is_persisted_with_install_provenance(signed_home: Path) -> None:
    record(signed_home)
    assert manager.set_app_provenance(
        "probe",
        source="registry:probe",
        url="https://github.com/example/apps",
        subdirectory="apps/probe",
    )
    installed = manager._read_installed("probe")
    assert installed is not None
    assert installed.sourceSubdirectory == "apps/probe"
    assert InstalledApp.from_dict(installed.to_dict()).sourceSubdirectory == "apps/probe"


@pytest.mark.parametrize("where", ["metadata", "publisher", "file"])
@pytest.mark.parametrize("tampered", [False, True])
def test_self_managed_executable_verification_uses_publisher(
    signed_home: Path, where: str, tampered: bool
) -> None:
    publisher = signed_home / "app-sources" / "probe" / "apps" / "probe"
    write_app(publisher, setup={"onEnable": "python helper.py"})
    helper = publisher / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    manifest = sign(publisher)
    metadata = record(signed_home, subdirectory="apps/probe")
    (metadata / "app.json").write_bytes((publisher / "app.json").read_bytes())
    if tampered:
        helper.write_text("VALUE = 2\n", encoding="utf-8")
    executed = {"metadata": metadata, "publisher": publisher, "file": helper}[where]
    refusal, verified = admission.verified_installed_manifest(
        "probe", executed, manifest, "python helper.py"
    )
    assert (refusal is not None) is tampered
    assert (verified is not None) is (not tampered)


def test_metadata_code_is_not_authenticated_by_a_valid_checkout(signed_home: Path) -> None:
    publisher = signed_home / "app-sources" / "probe"
    write_app(publisher)
    manifest = sign(publisher)
    metadata = record(signed_home)
    rogue = metadata / "rogue.py"
    rogue.write_text("VALUE = 'metadata code'\n", encoding="utf-8")
    refusal, verified = admission.verified_installed_manifest("probe", rogue, manifest)
    assert refusal is not None and "outside its signed publisher tree" in refusal
    assert verified is None


def test_self_managed_publisher_manifest_must_match_installed_snapshot(signed_home: Path) -> None:
    publisher = signed_home / "app-sources" / "probe"
    write_app(publisher)
    manifest = sign(publisher)
    record(signed_home)
    document = json.loads((publisher / "app.json").read_text(encoding="utf-8"))
    document["description"] = "changed publisher document"
    (publisher / "app.json").write_text(json.dumps(document), encoding="utf-8")
    refusal, verified = admission.verified_installed_manifest("probe", publisher, manifest)
    assert refusal is not None and "publisher manifest differs" in refusal
    assert verified is None


@pytest.mark.parametrize("required", [False, True])
def test_resource_and_hook_roots_match_signed_execution(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch, required: bool
) -> None:
    from kiro_crew.apps import bridges_sources, hooks_integration

    publisher = signed_home / "app-sources" / "probe"
    write_app(publisher)
    sign(publisher)
    metadata = record(signed_home)
    (metadata / "app.json").write_bytes((publisher / "app.json").read_bytes())
    if not required:
        (signed_home / "app_admission.json").unlink()
    expected = publisher if required else metadata
    assert bridges_sources._app_resource_root("probe") == expected
    assert bridges_sources._registration_source("probe")[1] == expected
    assert hooks_integration._app_hook_root("probe") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["onEnable", "onDisable", "onUninstall"])
async def test_self_managed_lifecycle_runs_in_verified_publisher_directory(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.apps import lifecycle_scripts
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    publisher = signed_home / "app-sources" / "probe"
    script = "python helper.py"
    write_app(publisher, setup={field: script})
    (publisher / "helper.py").write_text("print('publisher')\n", encoding="utf-8")
    sign(publisher)
    metadata = record(signed_home)
    (metadata / "app.json").write_bytes((publisher / "app.json").read_bytes())
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)
    force_posix_lifecycle(monkeypatch)
    wrap = AsyncMock(return_value=(["fake-shell"], None))
    monkeypatch.setattr(lifecycle_scripts, "wrap_argv_async", wrap)
    monkeypatch.setattr(lifecycle_scripts, "cgroup_scope_argv", lambda args: args)
    proc = SimpleNamespace(communicate=AsyncMock(return_value=(b"publisher", b"")), returncode=0)
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", spawn)
    result = await asyncio.wait_for(
        lifecycle_scripts.run_lifecycle_script("probe", script, timeout=FALLBACK_TIMEOUT_SECS),
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert not result["failed"]
    assert spawn.call_args.kwargs["cwd"] == str(publisher)
    assert "PYTHONPYCACHEPREFIX" in spawn.call_args.kwargs["env"]


def test_unavailable_publisher_verification_retains_io_kind(signed_home: Path) -> None:
    metadata = record(signed_home, subdirectory="../outside")
    write_app(metadata)
    manifest = sign(metadata)
    refusal, verified = admission.verified_installed_manifest("probe", metadata, manifest)
    assert isinstance(refusal, admission._InventoryUnavailable)
    assert verified is None


def unavailable_source(home: Path, kind: str, **extra: object):
    """An enabled app whose signed publisher root cannot be safely resolved."""
    publisher = home / "outside"
    write_app(publisher, **extra)
    manifest = sign(publisher)
    checkout = home / "app-sources" / "probe"
    checkout.mkdir(parents=True)
    subdirectory = "../outside"
    if kind == "linked":
        make_dir_link(checkout / "linked", publisher)
        subdirectory = "linked"
    metadata = record(home, subdirectory=subdirectory)
    (metadata / "app.json").write_bytes((publisher / "app.json").read_bytes())
    installed = InstalledApp(
        name="probe",
        enabled=True,
        resources="app",
        source="registry:probe",
        sourceSubdirectory=subdirectory,
    )
    (metadata / "installed.json").write_text(json.dumps(installed.to_dict()), encoding="utf-8")
    return manifest, metadata


@pytest.mark.parametrize("kind", ["unsafe", "linked"])
def test_unavailable_source_disarms_cron_restore_and_registers_nothing(
    signed_home: Path, kind: str
) -> None:
    from kiro_crew.apps import bridges_crons

    unavailable_source(signed_home, kind)
    assert bridges_crons._cron_restore_denied("probe")
    assert bridges_crons._admitted_app_cron_defs("probe")[0] == []
    assert manager._read_installed("probe").enabled


@pytest.mark.parametrize("kind", ["unsafe", "linked"])
@pytest.mark.parametrize("reader", ["reregister", "scrub", "direct"])
def test_unavailable_source_scrubs_mcp_without_provisioning(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch, kind: str, reader: str
) -> None:
    from unittest.mock import Mock

    from kiro_crew.apps import bridges, bridges_mcp

    manifest, _ = unavailable_source(
        signed_home, kind, mcpServers={"probe": {"command": "python", "args": ["server.py"]}}
    )
    removed = Mock(return_value=1)
    provision = Mock()
    monkeypatch.setattr(bridges, "_deregister_mcp_servers", removed)
    monkeypatch.setattr(bridges_mcp, "_maybe_provision_backendless_deps", provision)
    failures: list[str] = []
    if reader == "reregister":
        result = bridges_mcp.reregister_app_mcp_servers("probe", io_failures=failures)
    elif reader == "scrub":
        result = bridges_mcp.scrub_backend_mcp_url("probe", unreconciled=failures)
    else:
        result = bridges_mcp._register_mcp_servers("probe", manifest)
    assert result == []
    removed.assert_called_once_with("probe")
    provision.assert_not_called()
    if reader != "direct":
        assert failures == ["probe: publisher tree unavailable"]
    assert manager._read_installed("probe").enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unsafe", "linked"])
@pytest.mark.parametrize("with_hooks", [False, True])
async def test_hook_boot_skips_unavailable_app_and_keeps_healthy_neighbor(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch, kind: str, with_hooks: bool
) -> None:
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from kiro_crew.apps import hooks_integration as hooks
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    manifest, metadata = unavailable_source(
        signed_home,
        kind,
        backend={"hooks": {"routes": "hook:routes"}} if with_hooks else {},
    )
    # The neighbor is a real signed install, so startup admission admits it.
    healthy_root = signed_home / "apps" / "z-healthy"
    write_app(healthy_root, name="z-healthy")
    healthy_manifest = sign(healthy_root)
    manager._write_installed("z-healthy", InstalledApp(name="z-healthy", enabled=True))
    infos = [
        {"name": "probe", "enabled": True, "manifest": manifest.to_dict()},
        {"name": "z-healthy", "enabled": True, "manifest": healthy_manifest.to_dict()},
    ]
    before = (metadata / "installed.json").read_bytes()
    registry = Mock()
    registry.register_app_routes = AsyncMock()
    dispatcher = Mock()
    dispatcher._invoke = AsyncMock()
    admitted = Mock(return_value=None)
    disarm = AsyncMock(return_value=1)
    register = AsyncMock(return_value=[])
    antichurn = AsyncMock()
    monkeypatch.setattr(hooks, "_route_registry", registry)
    monkeypatch.setattr(hooks, "_lifecycle_dispatcher", dispatcher)
    monkeypatch.setattr(hooks, "list_apps", lambda: infos)
    monkeypatch.setattr(hooks, "app_execution_denied", admitted)
    monkeypatch.setattr(hooks, "disarm_app_crons_for_execution", disarm)
    monkeypatch.setattr(hooks, "register_app_crons_with_service", register)
    monkeypatch.setattr(hooks, "record_hook_antichurn_signature", antichurn)
    monkeypatch.setattr(hooks, "reconcile_all", lambda: 0)
    monkeypatch.setattr(hooks, "sel", lambda: Mock())
    service = object()
    await asyncio.wait_for(hooks.on_gateway_startup(cron_service=service), FALLBACK_TIMEOUT_SECS)
    disarm.assert_awaited_once_with("probe", service)
    register.assert_awaited_once_with("z-healthy", service)
    registry.deregister_app_routes.assert_called_once_with("probe")
    registry.register_app_routes.assert_not_awaited()
    dispatcher._invoke.assert_not_awaited()
    assert [call.args[0] for call in admitted.call_args_list] == ["z-healthy"]
    assert antichurn.await_count == int(with_hooks)
    assert (metadata / "installed.json").read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unsafe", "linked"])
async def test_hook_enable_and_reconcile_refuse_unavailable_source(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from kiro_crew.apps import hooks_integration as hooks
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    manifest, metadata = unavailable_source(signed_home, kind)
    info = {"name": "probe", "enabled": True, "manifest": manifest.to_dict()}
    before = (metadata / "installed.json").read_bytes()
    registry = Mock()
    disarm = AsyncMock(return_value=1)
    register = AsyncMock()
    admitted = Mock(return_value=None)
    monkeypatch.setattr(hooks, "_route_registry", registry)
    monkeypatch.setattr(hooks, "app_execution_denied", admitted)
    monkeypatch.setattr(hooks, "disarm_app_crons_for_execution", disarm)
    monkeypatch.setattr(hooks, "register_app_crons_with_service", register)
    monkeypatch.setattr(hooks, "record_hook_antichurn_signature", AsyncMock())
    service = object()
    assert hooks.hook_enable_denied("probe") is not None
    result = await asyncio.wait_for(
        hooks.on_app_enable("probe", info, cron_service=service), FALLBACK_TIMEOUT_SECS
    )
    assert result == {}
    disarm.assert_awaited_once_with("probe", service)
    registry.deregister_app_routes.assert_called_once_with("probe")
    admitted.assert_not_called()
    register.assert_not_awaited()
    assert (metadata / "installed.json").read_bytes() == before


@pytest.mark.asyncio
async def test_cron_restore_failure_is_owner_scoped_even_when_audit_fails(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from kiro_crew.apps import bridges as crons
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    unavailable_source(signed_home, "unsafe")
    monkeypatch.setattr(
        crons,
        "list_apps",
        lambda: [
            {"name": "probe", "enabled": True},
            {"name": "healthy", "enabled": True},
        ],
    )
    source = crons._registration_source
    monkeypatch.setattr(
        crons,
        "_registration_source",
        lambda name: (source(name) if name == "probe" else (None, signed_home / "apps" / name)),
    )
    monkeypatch.setattr(crons, "_registration_denied", lambda *a, **k: None)
    audit = Mock()
    audit.log_api_access.side_effect = OSError("audit unavailable")
    monkeypatch.setattr(crons, "sel", lambda: audit)
    remove = AsyncMock(return_value=[object()])
    service = SimpleNamespace(
        list_jobs=lambda **kwargs: [
            SimpleNamespace(created_by="app:probe"),
            SimpleNamespace(created_by="app:healthy"),
        ],
        remove_jobs_by_owner=remove,
    )
    disarmed = await asyncio.wait_for(
        crons.reconcile_app_crons_for_execution(service), FALLBACK_TIMEOUT_SECS
    )
    assert disarmed == ["probe"]
    remove.assert_awaited_once_with("app:probe")
    assert manager._read_installed("probe").enabled
