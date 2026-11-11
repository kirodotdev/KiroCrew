"""Signed imports, registration snapshots and builtin admission boundaries."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import json
import py_compile
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from apps_backend_coverage_helpers import spawn_root_fixture  # noqa: F401

from kiro_crew.apps import admission
from kiro_crew.apps.manifest import AppManifest

SECRET = "publisher-test-secret"


def write_app(root: Path, **extra: object) -> AppManifest:
    root.mkdir(parents=True, exist_ok=True)
    document = {
        "name": "probe",
        "version": "1.0.0",
        "displayName": "Probe",
        "description": "Test app",
        "permissions": {},
        **extra,
    }
    (root / "app.json").write_text(json.dumps(document), encoding="utf-8")
    return AppManifest.from_json_file(root / "app.json")


def sign(root: Path) -> AppManifest:
    from kiro_crew.apps.signed_content import sign_app_dir

    sign_app_dir(root, signer="test-publisher", secret=SECRET)
    return AppManifest.from_json_file(root / "app.json")


def content_mismatch_refusal(root: Path, name: str = "probe", **extra: object) -> str:
    """A refusal for *root* signed by the trusted key, then a packaged file changed."""
    write_app(root, **extra)
    (root / "helper.py").write_text("VALUE = 'signed'\n", encoding="utf-8")
    manifest = sign(root)
    (root / "helper.py").write_text("VALUE = 'changed'\n", encoding="utf-8")
    refusal = admission.app_admission_denied(name, manifest=manifest, action="boot", app_root=root)
    assert refusal is not None and admission._is_signature_refusal(refusal)
    return refusal


@pytest.fixture
def signed_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(admission, "config_dir", lambda: home)
    monkeypatch.setattr(admission, "_installed_root", lambda n: home / "apps" / n)
    (home / "app_admission.json").write_text(
        json.dumps({"require_signature": True, "trust_keys": {"test-publisher": SECRET}}),
        encoding="utf-8",
    )
    return home


def force_posix_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let lifecycle_scripts take its POSIX path without faking POSIX process-wide.

    Native Windows refuses lifecycle hooks before any cache preparation, so a test
    that exercises a hook there simulates POSIX for this one module only. Patching
    ``platform_compat.IS_POSIX`` itself would also send the real pinned-directory
    and lock code down POSIX branches on a Windows filesystem.
    """
    from kiro_crew.apps import lifecycle_scripts

    real = lifecycle_scripts.platform_compat

    class _PosixView:
        IS_POSIX = True

        def __getattr__(self, name: str) -> object:
            return getattr(real, name)

    monkeypatch.setattr(lifecycle_scripts, "platform_compat", _PosixView())


def plant_cache(source: Path, tmp_path: Path) -> None:
    evil = tmp_path / "cached.py"
    evil.write_text("VALUE = 'cached'\n", encoding="utf-8")
    py_compile.compile(
        str(evil),
        cfile=importlib.util.cache_from_source(str(source)),
        dfile=str(source),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        doraise=True,
    )


@pytest.mark.parametrize("style", ["sys-path", "direct-file"])
@pytest.mark.parametrize("policy", [True, False])
def test_hook_reads_source_only_under_signature_policy(
    tmp_path: Path,
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    style: str,
    policy: bool,
) -> None:
    from kiro_crew.apps import module_loader

    monkeypatch.setattr(sys, "pycache_prefix", None)
    root = signed_home / "apps" / "probe"
    write_app(root)
    helper = root / "signed_probe_helper.py"
    helper.write_text("VALUE = 'source'\n", encoding="utf-8")
    hook = (
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        "import signed_probe_helper as helper\n"
        if style == "sys-path"
        else "import importlib.util\nfrom pathlib import Path\n"
        "spec = importlib.util.spec_from_file_location(\n"
        "    'signed_probe_helper', Path(__file__).parent / 'signed_probe_helper.py')\n"
        "helper = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(helper)\n"
    )
    (root / "hook.py").write_text(hook + "def run():\n    return helper.VALUE\n", encoding="utf-8")
    sign(root)
    plant_cache(helper, tmp_path)
    if not policy:
        (signed_home / "app_admission.json").unlink()
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest",
        lambda n: AppManifest.from_json_file(root / "app.json"),
    )
    monkeypatch.setattr(module_loader, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    try:
        assert admission.signature_required() is policy
        assert admission.signed_tree_applies("probe", root / "hook.py") is policy
        result = module_loader.load_app_module("probe", root, "hook:run")()
        assert result == ("source" if policy else "cached")
    finally:
        module_loader.unload_app_modules("probe")
        importlib.invalidate_caches()


def test_top_level_finder_uses_verified_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import signed_imports

    root = tmp_path / "app"
    root.mkdir()
    entry = root / "signed_probe_helper.py"
    entry.write_text("VALUE = 'source'\n", encoding="utf-8")
    plant_cache(entry, tmp_path)
    digest = hashlib.sha256(entry.read_bytes()).hexdigest()
    monkeypatch.syspath_prepend(str(root))
    try:
        signed_imports.trust_app_sources("probe", root, {entry.name: digest}, "hook", entry)
        spec = signed_imports._finder.find_spec("signed_probe_helper", None)
        assert spec is not None
        assert isinstance(spec.loader, signed_imports.VerifiedSourceLoader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.VALUE == "source"
    finally:
        signed_imports.forget_app_sources("probe")


def test_last_unregister_removes_finder(tmp_path: Path) -> None:
    from kiro_crew.apps import signed_imports

    entry = tmp_path / "hook.py"
    entry.write_text("x = 1\n", encoding="utf-8")
    digest = hashlib.sha256(entry.read_bytes()).hexdigest()
    try:
        signed_imports.trust_app_sources("probe", tmp_path, {entry.name: digest}, "hook", entry)
        assert signed_imports._finder in sys.meta_path
    finally:
        signed_imports.forget_app_sources("probe")
    assert signed_imports._finder not in sys.meta_path


def test_empty_finder_does_not_search_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.apps import signed_imports

    lookup = Mock(side_effect=AssertionError("empty finder searched a path"))
    monkeypatch.setattr(signed_imports, "_signed_roots", {})
    monkeypatch.setattr(signed_imports, "_signed_lookup", lookup)
    assert signed_imports._finder.find_spec("test", ["/unused"]) is None
    lookup.assert_not_called()


def test_builtin_hook_verdict_is_not_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import module_loader

    monkeypatch.setattr(module_loader, "_is_builtin_app", lambda *a: True)
    monkeypatch.setattr(module_loader, "_prepared_verdicts", {})
    verifier = Mock(return_value=(None, None))
    monkeypatch.setattr(module_loader, "_verified_installed_manifest", verifier)
    assert module_loader.verify_hook_source("builtin", tmp_path, "hook:run") == (None, None)
    assert module_loader._prepared_verdicts == {}
    verifier.assert_not_called()


@pytest.mark.parametrize("site", ["register", "agents", "skills", "mcp"])
def test_registration_verifies_its_own_manifest(
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    from kiro_crew.apps import bridges

    root = signed_home / "apps" / "probe"
    write_app(
        root, agents=["agent.json"], skills=["SKILL.md"], mcpServers={"test": {"command": "python"}}
    )
    (root / "agent.json").write_text('{"name": "test"}', encoding="utf-8")
    (root / "SKILL.md").write_text("Test skill", encoding="utf-8")
    current = sign(root)
    invalid_document = dict(current.source_document)
    invalid_document["description"] = "unsigned change"
    invalid = AppManifest.from_dict(invalid_document)
    monkeypatch.setattr(bridges, "get_app_manifest", lambda n: invalid)
    monkeypatch.setattr(bridges, "get_app", lambda n: {"resources": "gateway"})
    monkeypatch.setattr(bridges, "_registration_source", lambda n: (invalid, root))
    monkeypatch.setattr(bridges, "_app_resource_root", lambda n: root)
    monkeypatch.setattr(bridges, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda n: current)
    marker = Mock(return_value=["registered"])
    for name in (
        "_register_agents",
        "_register_skills",
        "_register_mcp_servers",
        "_register_crons",
    ):
        monkeypatch.setattr(bridges, name, marker)
    for name in ("_deregister_agents", "_deregister_skills", "_deregister_mcp_servers"):
        monkeypatch.setattr(bridges, name, lambda n: [])
    monkeypatch.setattr(bridges, "deregister_app", lambda n: bridges.RegistrationResult())
    result = {
        "register": bridges.register_app,
        "agents": bridges.refresh_app_agents,
        "skills": bridges.reconcile_app_skills,
        "mcp": bridges.scrub_backend_mcp_url,
    }[site]("probe")
    assert result.errors if site == "register" else result == []
    marker.assert_not_called()


@pytest.mark.parametrize("policy", [True, False])
def test_forged_builtin_record_gets_enable_admission(
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: bool,
) -> None:
    from kiro_crew.apps import manager
    from kiro_crew.apps.manager_records import InstalledApp

    root = signed_home / "apps" / "probe"
    write_app(root)
    meta = InstalledApp(name="probe", version="1.0.0", origin="builtin", enabled=False)
    manager._write_installed("probe", meta)
    if not policy:
        (signed_home / "app_admission.json").unlink()
    monkeypatch.setattr(manager, "_app_activation_denied", lambda *a, **k: None)
    monkeypatch.setattr(manager, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(manager, "shipped_builtin_app_root", lambda n: None)
    result = manager.enable_app("probe")
    assert result.ok is (not policy)
    assert manager._read_installed("probe").enabled is (not policy)


@pytest.mark.parametrize("has_backend", [True, False])
@pytest.mark.parametrize("policy", [True, False])
def test_boot_refuses_forged_builtin_that_fails_admission(
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_backend: bool,
    policy: bool,
) -> None:
    from kiro_crew.apps import backend, bridges, manager
    from kiro_crew.apps.backend_runtime import startup
    from kiro_crew.apps.manager_records import InstalledApp

    root = signed_home / "apps" / "probe"
    manifest = write_app(root, backend={"entryPoint": "server.py"} if has_backend else {})
    (root / "server.py").write_text("x = 1\n", encoding="utf-8")
    manager._write_installed(
        "probe", InstalledApp(name="probe", version="1.0.0", origin="builtin", enabled=True)
    )
    if not policy:
        (signed_home / "app_admission.json").unlink()
    apps = [{"name": "probe", "origin": "builtin", "enabled": True, "manifest": manifest.to_dict()}]
    monkeypatch.setattr(startup, "list_apps", lambda: apps)
    monkeypatch.setattr(startup, "_reap_stale_app_backends", lambda: None)
    monkeypatch.setattr(startup, "_app_activation_denied", lambda *a, **k: None)
    monkeypatch.setattr(startup, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(startup, "shipped_builtin_app_root", lambda n: None)
    monkeypatch.setattr(bridges, "register_app", lambda n: bridges.RegistrationResult())
    monkeypatch.setattr(bridges, "reconcile_app_skills", lambda n: [])
    for name in ("_deregister_agents", "_deregister_skills", "_deregister_mcp_servers"):
        monkeypatch.setattr(bridges, name, lambda n: [])
    spawned = Mock(return_value=[])
    monkeypatch.setattr(startup, "_start_backends_concurrently", spawned)
    monkeypatch.setattr(backend, "_app_enabled_state", manager.app_enabled_state)
    startup.start_enabled_app_backends()
    # Unsigned proves nothing about the files: refused for this boot, record kept.
    assert manager._read_installed("probe").enabled is True
    assert spawned.call_args.args[0] == (["probe"] if has_backend and not policy else [])


@pytest.mark.parametrize("policy", [True, False])
def test_node_provisioning_preserves_enablement_across_restart(
    spawn_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: bool,
) -> None:
    from apps_backend_coverage_helpers import _capture_popen, _StopSpawn

    from kiro_crew.apps import backend, manager
    from kiro_crew.apps.manager_records import InstalledApp

    home = tmp_path / "node-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(admission, "config_dir", lambda: home)
    if policy:
        (home / "app_admission.json").write_text(
            json.dumps({"require_signature": True, "trust_keys": {"test-publisher": SECRET}}),
            encoding="utf-8",
        )
    write_app(spawn_root, backend={"entryPoint": "server.js", "type": "node"})
    (spawn_root / "server.js").write_text("// test backend\n", encoding="utf-8")
    (spawn_root / "package.json").write_text(
        '{"name": "probe", "version": "1.0.0"}', encoding="utf-8"
    )
    manifest = sign(spawn_root)
    monkeypatch.setattr(admission, "_installed_root", lambda n: spawn_root)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest",
        lambda n: AppManifest.from_json_file(spawn_root / "app.json"),
    )
    monkeypatch.setattr(manager, "app_dir", lambda n: spawn_root)
    monkeypatch.setattr(manager, "_app_activation_denied", lambda *a, **k: None)
    monkeypatch.setattr(manager, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(manager, "shipped_builtin_app_root", lambda n: None)
    manager._write_installed(
        "probe", InstalledApp(name="probe", version="1.0.0", origin="local", enabled=False)
    )
    monkeypatch.setattr(backend, "_find_node_binary", lambda: "/test/node")
    monkeypatch.setattr(backend, "_find_npm_binary", lambda: "/test/npm")
    commands = []

    def npm(argv, **kwargs):
        commands.append(argv)
        if "--no-package-lock" not in argv:
            (Path(kwargs["cwd"]) / "package-lock.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(backend, "run_limited", npm)
    _capture_popen(monkeypatch)
    assert manager.enable_app("probe").ok
    for _ in range(2):
        with pytest.raises(_StopSpawn):
            backend._start_app_backend_body("probe", manifest)
        assert manager.enable_app("probe").ok
        assert manager._read_installed("probe").enabled
    assert len(commands) == 2
    assert all(("--no-package-lock" in cmd) is policy for cmd in commands)
    assert (spawn_root / "package-lock.json").exists() is (not policy)


@pytest.mark.parametrize("nested_lock", [None, "package-lock.json", "npm-shrinkwrap.json"])
@pytest.mark.parametrize("root_lock", [None, "package-lock.json", "npm-shrinkwrap.json"])
@pytest.mark.parametrize("policy", [True, False])
def test_signed_node_install_honours_publisher_lockfile(
    spawn_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: bool,
    root_lock: str | None,
    nested_lock: str | None,
) -> None:
    from apps_backend_coverage_helpers import _capture_popen, _StopSpawn

    from kiro_crew.apps import backend

    home = tmp_path / "lock-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(admission, "config_dir", lambda: home)
    if policy:
        (home / "app_admission.json").write_text(
            json.dumps({"require_signature": True, "trust_keys": {"test-publisher": SECRET}}),
            encoding="utf-8",
        )
    write_app(spawn_root, backend={"entryPoint": "backend/server.js", "type": "node"})
    nested = spawn_root / "backend"
    nested.mkdir()
    (nested / "server.js").write_text("// test backend\n", encoding="utf-8")
    locks: dict[Path, bytes] = {}
    for pkg_dir, lock in ((spawn_root, root_lock), (nested, nested_lock)):
        (pkg_dir / "package.json").write_text('{"name": "probe"}', encoding="utf-8")
        if lock:
            body = json.dumps({"lockfileVersion": 3, "pinned": pkg_dir.name}).encode()
            (pkg_dir / lock).write_bytes(body)
            locks[pkg_dir / lock] = body
    manifest = sign(spawn_root)
    monkeypatch.setattr(admission, "_installed_root", lambda n: spawn_root)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest",
        lambda n: AppManifest.from_json_file(spawn_root / "app.json"),
    )
    monkeypatch.setattr(backend, "_find_node_binary", lambda: "/test/node")
    monkeypatch.setattr(backend, "_find_npm_binary", lambda: "/test/npm")
    commands: dict[Path, list[str]] = {}

    def npm(argv, **kwargs):
        cwd = Path(kwargs["cwd"])
        commands[cwd] = argv
        if argv[1] == "install" and "--no-package-lock" not in argv:
            (cwd / "package-lock.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(backend, "run_limited", npm)
    _capture_popen(monkeypatch)
    with pytest.raises(_StopSpawn):
        backend._start_app_backend_body("probe", manifest)

    layout = ((spawn_root, root_lock), (nested, nested_lock))
    assert list(commands) == [spawn_root.resolve(), nested.resolve()]
    for (_, lock), argv in zip(layout, commands.values()):
        assert argv[1] == ("ci" if policy and lock else "install")
        assert ("--no-package-lock" in argv) is (policy and not lock)
    if policy:
        for path, body in locks.items():
            assert path.read_bytes() == body
        for pkg_dir, lock in layout:
            if not lock:
                assert not (pkg_dir / "package-lock.json").exists()


def test_uninstall_removes_child_cache(signed_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.apps import manager
    from kiro_crew.apps.manager_records import InstalledApp

    root = signed_home / "apps" / "probe"
    write_app(root)
    manager._write_installed("probe", InstalledApp(name="probe", version="1.0.0", origin="local"))
    cache = signed_home / "run" / "app-pycache" / "probe"
    cache.mkdir(parents=True)
    (cache / "test.pyc").write_bytes(b"test")
    monkeypatch.setattr(manager, "_drop_trust_grant", lambda n: None)
    monkeypatch.setattr(manager, "_has_trust_grant", lambda n: False)
    monkeypatch.setattr(manager, "_trust_grant_repository", lambda n: None)
    monkeypatch.setattr(manager, "_trust_grant_local", lambda n: False)
    assert manager.uninstall_app("probe", keep_data=False).ok
    assert not cache.exists()


def test_builtin_stdio_uses_shipped_root(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import bridges as bridges_mcp

    root = signed_home / "shipped"
    root.mkdir()
    manifest = AppManifest.from_dict(
        {"name": "probe", "mcpServers": {"test": {"command": "python"}}}
    )
    monkeypatch.setattr(bridges_mcp, "_app_resource_root", lambda n: root, raising=False)
    monkeypatch.setattr(bridges_mcp, "app_dir", lambda n: signed_home / "apps" / n)
    monkeypatch.setattr(bridges_mcp, "_live_port_for", lambda *a: None)
    monkeypatch.setattr(bridges_mcp, "_maybe_provision_backendless_deps", lambda *a: None)
    monkeypatch.setattr(bridges_mcp, "_health_reconcile_guard", contextlib.nullcontext)
    monkeypatch.setattr(bridges_mcp, "_mcp_lock", contextlib.nullcontext)
    monkeypatch.setattr(bridges_mcp, "_read_mcp_json_unlocked", lambda **k: {})
    monkeypatch.setattr(bridges_mcp, "_write_mcp_json_unlocked", lambda d: None)
    monkeypatch.setattr(bridges_mcp, "resolve_stdio_command", lambda c, **k: c)
    monkeypatch.setattr(bridges_mcp, "_schedule_unresolvable_warning", lambda *a: None)
    prefix = Mock(wraps=bridges_mcp._pycache_prefix_env)
    monkeypatch.setattr(bridges_mcp, "_pycache_prefix_env", prefix)
    assert bridges_mcp._register_mcp_servers("probe", manifest) == ["probe:test"]
    prefix.assert_called_once_with("probe", root)
    assert not (signed_home / "run" / "app-pycache" / "probe").exists()


def test_shipped_builtin_stays_exempt_at_enable(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import manager
    from kiro_crew.apps.execution import _shipped_builtin_apps
    from kiro_crew.apps.manager_records import InstalledApp

    shipped, name = next(_shipped_builtin_apps())
    manifest = AppManifest.from_json_file(shipped / "app.json")
    manager._write_installed(
        name, InstalledApp(name=name, version="1.0.0", origin="builtin", enabled=False)
    )
    monkeypatch.setattr(manager, "get_app_manifest", lambda n: manifest)
    monkeypatch.setattr(manager, "_app_activation_denied", lambda *a, **k: None)
    monkeypatch.setattr(manager, "app_execution_denied", lambda *a, **k: None)
    assert manager.enable_app(name).ok


def test_cache_cleanup_failure_refuses_signed_hook(
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.apps import module_loader

    root = signed_home / "apps" / "probe"
    write_app(root)
    (root / "hook.py").write_text("def run():\n    return 'source'\n", encoding="utf-8")
    sign(root)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest",
        lambda n: AppManifest.from_json_file(root / "app.json"),
    )
    monkeypatch.setattr(module_loader, "app_execution_denied", lambda *a, **k: None)

    def refused(root):
        raise OSError("cache held open")

    monkeypatch.setattr(module_loader._signed_imports, "clear_app_bytecode", refused, raising=False)
    try:
        with pytest.raises(ImportError, match="cache held open"):
            module_loader.load_app_module("probe", root, "hook:run")
    finally:
        module_loader.unload_app_modules("probe")


def test_boot_backend_pass_checks_admission_again(
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.apps import bridges, manager
    from kiro_crew.apps.backend_runtime import startup
    from kiro_crew.apps.manager_records import InstalledApp

    root = signed_home / "apps" / "probe"
    manifest = write_app(root, backend={"entryPoint": "server.py"})
    manager._write_installed(
        "probe", InstalledApp(name="probe", version="1.0.0", origin="builtin", enabled=True)
    )
    apps = [{"name": "probe", "origin": "builtin", "enabled": True, "manifest": manifest.to_dict()}]
    monkeypatch.setattr(startup, "list_apps", lambda: apps)
    monkeypatch.setattr(startup, "_reap_stale_app_backends", lambda: None)
    monkeypatch.setattr(startup, "_app_activation_denied", lambda *a, **k: None)
    monkeypatch.setattr(startup, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(startup, "shipped_builtin_app_root", lambda n: None)
    signature_refusal = content_mismatch_refusal(signed_home / "tampered")
    admit = Mock(side_effect=[None, signature_refusal])
    monkeypatch.setattr(startup, "app_admission_denied", admit)
    monkeypatch.setattr(bridges, "register_app", lambda n: bridges.RegistrationResult())
    monkeypatch.setattr(bridges, "reconcile_app_skills", lambda n: [])
    spawned = Mock(return_value=[])
    monkeypatch.setattr(startup, "_start_backends_concurrently", spawned)
    startup.start_enabled_app_backends()
    assert admit.call_count == 2
    assert not manager._read_installed("probe").enabled
    assert spawned.call_args.args[0] == []
