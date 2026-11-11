"""Source-only admission, off-loop hook preparation and isolated boot refusals."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from test_app_signed_round3 import content_mismatch_refusal, sign
from test_app_signed_round3 import signed_home as _signed_home
from test_app_signed_round3 import write_app

from kiro_crew.apps import admission

signed_home = _signed_home


@pytest.mark.parametrize("entry", ["data/server.py", "app-crons.json"])
@pytest.mark.parametrize("operation", ["install", "update"])
@pytest.mark.parametrize("policy", [True, False])
def test_local_source_cannot_seed_unsigned_runtime_entries(
    tmp_path: Path,
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    operation: str,
    policy: bool,
) -> None:
    from kiro_crew.apps import manager

    source = tmp_path / "source"
    write_app(source)
    sign(source)
    if not policy:
        (signed_home / "app_admission.json").unlink()
    if operation == "update":
        assert manager.install_app(source).ok
        (manager.app_dir("probe") / "data" / "kept").write_text("runtime", encoding="utf-8")
    marker = source / entry
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("unsigned source marker", encoding="utf-8")
    result = getattr(manager, f"{operation}_app")(source)
    assert result.ok is (not policy), result.error
    if policy:
        assert entry.split("/")[0] in result.error
        assert not (manager.app_dir("probe") / entry).exists()
    if operation == "update":
        assert (manager.app_dir("probe") / "data" / "kept").read_text("utf-8") == "runtime"
    elif not policy:
        assert (manager.app_dir("probe") / entry).read_text("utf-8") == "unsigned source marker"


@pytest.mark.parametrize("action", ["install", "update", "install_from_registry"])
@pytest.mark.parametrize("entry", ["data", "app-crons.json"])
def test_source_entry_shape_does_not_escape_admission(
    tmp_path: Path,
    signed_home: Path,
    action: str,
    entry: str,
) -> None:
    root = tmp_path / "source"
    write_app(root)
    manifest = sign(root)
    (root / entry).mkdir()
    reason = admission.app_admission_denied("probe", manifest, action, app_root=root)
    assert reason is not None and entry in reason


@pytest.mark.parametrize("fresh", [True, False])
@pytest.mark.parametrize("entry", ["data/marker", "app-crons.json", ".app_secret"])
def test_registry_publisher_root_entries_are_refused(
    tmp_path: Path,
    signed_home: Path,
    fresh: bool,
    entry: str,
) -> None:
    root = tmp_path / "checkout"
    write_app(root)
    manifest = sign(root)
    marker = root / entry
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("publisher marker", encoding="utf-8")
    reason = admission.app_admission_denied(
        "probe",
        manifest,
        "install_from_registry",
        app_root=root,
        fresh_checkout=fresh,
    )
    assert reason is not None and entry.split("/")[0] in reason


def test_reused_checkout_dependencies_remain_outside_source_rule(
    tmp_path: Path,
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.apps import signed_checkout

    monkeypatch.setattr(signed_checkout, "tracked_unpackaged_entry", lambda root: None)
    root = tmp_path / "checkout"
    write_app(root)
    manifest = sign(root)
    for name in (".venv", "node_modules", "__pycache__", ".kirocrew-deps"):
        (root / name).mkdir()
        (root / name / "marker").write_text("generated", encoding="utf-8")
    assert (
        admission.app_admission_denied(
            "probe",
            manifest,
            "install_from_registry",
            app_root=root,
        )
        is None
    )


def test_local_secret_is_replaced_and_installed_runtime_is_preserved(
    tmp_path: Path,
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.apps import manager
    from kiro_crew.dashboard.token_auth import read_app_secret

    source = tmp_path / "source"
    write_app(source)
    sign(source)
    (source / ".app_secret").write_text("publisher marker", encoding="utf-8")
    root = manager.app_dir("probe")
    (root / "data").mkdir(parents=True)
    (root / "data" / "kept").write_text("runtime", encoding="utf-8")
    assert manager.install_app(source).ok
    # The publisher's file never becomes the secret: the gateway's lives outside the tree.
    assert not (root / ".app_secret").exists()
    secret = read_app_secret("probe")
    assert secret and secret != "publisher marker"
    assert manager.update_app(source).ok
    assert read_app_secret("probe") == secret
    assert (root / "data" / "kept").read_text("utf-8") == "runtime"
    (root / "app-crons.json").write_text("runtime marker", encoding="utf-8")
    for action in ("boot", "enable", "restart"):
        assert (
            admission.app_admission_denied(
                "probe",
                manager.get_app_manifest("probe"),
                action,
            )
            is None
        )


def test_empty_cron_registration_does_not_overwrite_source_bytes(
    tmp_path: Path,
    signed_home: Path,
) -> None:
    from kiro_crew.apps import bridges, manager

    source = tmp_path / "source"
    manifest = write_app(source)
    (source / "app-crons.json").write_text("source marker", encoding="utf-8")
    sign(source)
    (signed_home / "app_admission.json").unlink()
    assert manager.install_app(source).ok
    assert bridges._register_crons("probe", manifest) == []
    assert (manager.app_dir("probe") / "app-crons.json").read_text("utf-8") == "source marker"


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["route", "lifecycle"])
@pytest.mark.parametrize("policy", [True, False])
@pytest.mark.parametrize("cleanup_fails", [True, False])
async def test_hook_preparation_cleans_once_off_loop(
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    policy: bool,
    cleanup_fails: bool,
) -> None:
    from aiohttp import web

    from kiro_crew.apps import lifecycle, manager, module_loader, route_registry

    root = signed_home / "apps" / "probe"
    write_app(root, backend={"hooks": {"on_startup": "hook:run"}})
    (root / "hook.py").write_text(
        "def run(ctx):\n    ctx.called()\n    return []\n",
        encoding="utf-8",
    )
    sign(root)
    caches = [root / name / "__pycache__" for name in ("data", ".venv", "node_modules", "nested")]
    for cache in caches:
        cache.mkdir(parents=True)
        (cache / "marker.pyc").write_bytes(b"cache")
    if not policy:
        (signed_home / "app_admission.json").unlink()
    monkeypatch.setattr(module_loader, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(module_loader, "_prepared_verdicts", {})
    original = module_loader._signed_imports.clear_app_bytecode
    loops = []

    def cleanup(path):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        loops.append(loop)
        if cleanup_fails:
            raise OSError("cache held open")
        original(path)

    monkeypatch.setattr(module_loader._signed_imports, "clear_app_bytecode", cleanup)
    ctx: Any = SimpleNamespace(called=Mock(), health=SimpleNamespace(mark_degraded=Mock()))
    escaped = None
    try:
        if site == "route":
            await route_registry.RouteRegistry(web.Application()).register_app_routes(
                "probe",
                root,
                "hook:run",
                ctx,
            )
        else:
            dispatcher = lifecycle.LifecycleDispatcher()
            await dispatcher._invoke("probe", "hook:run", ctx, phase="startup")
    except OSError as exc:
        escaped = exc
    finally:
        module_loader.unload_app_modules("probe")
    assert escaped is None, f"cleanup failure escaped route refusal: {escaped}"
    assert loops == ([None] if policy else []), "cleanup must run once off the event loop"
    assert ctx.called.call_count == (0 if policy and cleanup_fails else 1)
    assert ctx.health.mark_degraded.called is (policy and cleanup_fails)
    assert all(cache.exists() is (not policy or cleanup_fails) for cache in caches)
    assert module_loader._prepared_verdicts == {}
    assert manager.get_app_manifest("probe") is not None


@pytest.mark.asyncio
async def test_shipped_builtin_lifecycle_does_not_prepare_data_home_verdict(
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.apps import lifecycle, module_loader
    from kiro_crew.apps.execution import _shipped_builtin_apps

    root, name = next(_shipped_builtin_apps())
    called = Mock()
    imported = Mock(return_value=SimpleNamespace(run=called))
    monkeypatch.setattr(lifecycle, "importlib", SimpleNamespace(import_module=imported))
    monkeypatch.setattr(module_loader, "_prepared_verdicts", {})
    prepared = Mock(wraps=module_loader.verify_hook_source)
    monkeypatch.setattr(lifecycle, "verify_hook_source", prepared)
    ctx: Any = SimpleNamespace(health=SimpleNamespace(mark_degraded=Mock()))
    assert await lifecycle.LifecycleDispatcher()._invoke(name, "hook:run", ctx, phase="startup")
    called.assert_called_once_with(ctx)
    imported.assert_called_once_with(f"kiro_crew.apps.builtins.{root.name}.hook")
    assert module_loader._prepared_verdicts == {}
    prepared.assert_not_called()


@pytest.mark.parametrize(
    "phase,has_backend", [("resources", False), ("resources", True), ("backend", True)]
)
@pytest.mark.parametrize("failure", ["raise", "result"])
def test_boot_disable_failure_keeps_denied_app_down_and_continues(
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    phase: str,
    has_backend: bool,
    failure: str,
) -> None:
    from kiro_crew.apps import bridges, manager
    from kiro_crew.apps.backend_runtime import startup
    from kiro_crew.apps.manager_records import AppResult

    apps = [
        {
            "name": "denied",
            "enabled": True,
            "manifest": {"backend": {"entryPoint": "server.py"} if has_backend else {}},
        },
        {"name": "healthy", "enabled": True, "manifest": {"backend": {"entryPoint": "server.py"}}},
    ]
    monkeypatch.setattr(startup, "list_apps", lambda: apps)
    monkeypatch.setattr(startup, "_reap_stale_app_backends", lambda: None)
    monkeypatch.setattr(startup, "_app_activation_denied", lambda *a, **k: None)
    monkeypatch.setattr(startup, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(startup, "shipped_builtin_app_root", lambda n: None)
    monkeypatch.setattr(startup, "get_app_manifest", lambda n: None)
    monkeypatch.setattr(startup, "_app_enabled_state", lambda n: True)
    calls = []
    signature_refusal = content_mismatch_refusal(signed_home / "apps" / "denied", "denied")

    def admit(name, **kwargs):
        calls.append(name)
        if name == "denied" and (phase == "resources" or calls.count(name) > 1):
            return signature_refusal
        return None

    monkeypatch.setattr(startup, "app_admission_denied", admit)
    disable = Mock(
        side_effect=OSError("record read-only") if failure == "raise" else None,
        return_value=AppResult(ok=False, error="record read-only"),
    )
    monkeypatch.setattr(manager, "disable_app", disable)
    registered = Mock(return_value=bridges.RegistrationResult())
    monkeypatch.setattr(bridges, "register_app", registered)
    monkeypatch.setattr(bridges, "reconcile_app_skills", lambda n: [])
    revoked = Mock(return_value=[])
    for binding in ("_deregister_agents", "_deregister_skills", "_deregister_mcp_servers"):
        monkeypatch.setattr(bridges, binding, revoked)
    spawned = Mock(side_effect=lambda names: names)
    monkeypatch.setattr(startup, "_start_backends_concurrently", spawned)
    escaped = None
    try:
        started = startup.start_enabled_app_backends()
    except OSError as exc:
        escaped = exc
        started = []
    assert escaped is None, f"disable failure aborted other apps: {escaped}"
    assert started == ["healthy"]
    assert "healthy" in [call.args[0] for call in registered.call_args_list]
    disable.assert_called_once_with("denied")
    assert "record read-only" in caplog.text
    if phase == "resources":
        assert not apps[0]["enabled"]
        assert revoked.call_count == 3


@pytest.mark.parametrize("policy", [True, False])
def test_signed_backend_cannot_be_seeded_under_source_data(
    tmp_path: Path,
    signed_home: Path,
    policy: bool,
) -> None:
    from kiro_crew.apps import manager

    source = tmp_path / "source"
    write_app(source, backend={"entryPoint": "data/server.py", "type": "python"})
    (source / "data").mkdir()
    marker = source / "data" / "server.py"
    marker.write_text("VALUE = 'published'\n", encoding="utf-8")
    sign(source)
    marker.write_text("VALUE = 'altered'\n", encoding="utf-8")
    if not policy:
        (signed_home / "app_admission.json").unlink()
    result = manager.install_app(source)
    assert result.ok is (not policy), result.error
    installed = manager.app_dir("probe") / "data" / "server.py"
    assert installed.exists() is (not policy)
    if policy:
        assert "data" in result.error
    else:
        assert installed.read_text("utf-8") == "VALUE = 'altered'\n"


def test_unreadable_source_root_is_a_clean_refusal(
    tmp_path: Path,
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.apps import signed_content

    root = tmp_path / "source"
    write_app(root)
    manifest = sign(root)
    monkeypatch.setattr(
        signed_content, "os", SimpleNamespace(scandir=Mock(side_effect=OSError("cannot list")))
    )
    reason = admission.app_admission_denied("probe", manifest, "install", app_root=root)
    assert reason is not None and "cannot inspect source root" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", ["data", ".venv", "node_modules"])
@pytest.mark.parametrize("policy", [True, False])
async def test_traversal_error_refuses_signed_route_hook(
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
    policy: bool,
) -> None:
    import errno

    from aiohttp import web

    from kiro_crew.apps import module_loader, route_registry

    root = signed_home / "apps" / "probe"
    write_app(root)
    (root / "hook.py").write_text(
        "def run(ctx):\n    ctx.called()\n    return []\n", encoding="utf-8"
    )
    sign(root)
    blocked = root / directory
    cache = blocked / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "marker.pyc").write_bytes(b"harmless")
    if not policy:
        (signed_home / "app_admission.json").unlink()
    monkeypatch.setattr(module_loader, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(module_loader, "_prepared_verdicts", {})
    from kiro_crew.apps import signed_imports

    original_names = signed_imports.PinnedDirectory.names
    attempted = []

    def names(held):
        if held.path == str(blocked):
            attempted.append(held.path)
            raise OSError(errno.EACCES, "traversal refused", str(blocked))
        return original_names(held)

    ctx: Any = SimpleNamespace(called=Mock(), health=SimpleNamespace(mark_degraded=Mock()))
    try:
        with monkeypatch.context() as scoped:
            # Fail the real pinned listing, not a mocked cleanup result.
            scoped.setattr(signed_imports.PinnedDirectory, "names", names)
            routes = await route_registry.RouteRegistry(web.Application()).register_app_routes(
                "probe", root, "hook:run", ctx
            )
        assert routes == []
        assert attempted == ([str(blocked)] if policy else [])
        assert ctx.called.call_count == (0 if policy else 1)
        if policy:
            ctx.health.mark_degraded.assert_called_once()
            error = ctx.health.mark_degraded.call_args.args[0]
            assert "bytecode cleanup failed" in error
            assert "traversal refused" in error and repr(str(blocked)) in error
        else:
            ctx.health.mark_degraded.assert_not_called()
        assert (cache / "marker.pyc").read_bytes() == b"harmless"
        assert module_loader._prepared_verdicts == {}
    finally:
        module_loader.unload_app_modules("probe")
