"""Publisher-content checks after npm and recoverable boot policy refusals."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from apps_backend_coverage_helpers import _capture_popen, _StopSpawn
from apps_backend_coverage_helpers import spawn_root_fixture as _spawn_root
from test_app_signed_round3 import SECRET, content_mismatch_refusal, sign
from test_app_signed_round3 import signed_home as _signed_home
from test_app_signed_round3 import write_app

from kiro_crew.apps import admission, backend, bridges, execution, manager
from kiro_crew.apps import manifest as manifest_module
from kiro_crew.apps import signed_content
from kiro_crew.apps.backend_runtime import startup
from kiro_crew.apps.manager_records import InstalledApp
from kiro_crew.apps.manifest import AppManifest

spawn_root = _spawn_root
signed_home = _signed_home


@pytest.fixture
def node_app(spawn_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "policy"
    home.mkdir()
    monkeypatch.setattr(admission, "config_dir", lambda: home)
    (home / "app_admission.json").write_text(
        json.dumps({"require_signature": True, "trust_keys": {"test-publisher": SECRET}}),
        encoding="utf-8",
    )
    write_app(spawn_root, backend={"entryPoint": "backend/server.js", "type": "node"})
    nested = spawn_root / "backend"
    nested.mkdir()
    (nested / "server.js").write_text("// published\n", encoding="utf-8")
    for directory in (spawn_root, nested):
        (directory / "package.json").write_text('{"name":"probe"}', encoding="utf-8")
    monkeypatch.setattr(admission, "_installed_root", lambda name: spawn_root)
    monkeypatch.setattr(
        manager,
        "get_app_manifest",
        lambda name: AppManifest.from_json_file(spawn_root / "app.json"),
    )
    monkeypatch.setattr(backend, "_find_node_binary", lambda: str(tmp_path / "node"))
    monkeypatch.setattr(backend, "_find_npm_binary", lambda: str(tmp_path / "npm"))
    monkeypatch.setattr(backend, "is_builtin_app", lambda **kwargs: False)
    monkeypatch.setattr(execution, "is_builtin_app", lambda **kwargs: False)
    monkeypatch.setattr(backend, "_pycache_prefix_env", lambda *args: {})
    sink = Mock()
    monkeypatch.setattr(backend, "sel", lambda: sink)
    monkeypatch.setattr(admission, "sel", lambda: sink)
    return spawn_root


@pytest.mark.parametrize("directory", ["root", "nested"])
@pytest.mark.parametrize("lock", [None, "package-lock.json", "npm-shrinkwrap.json"])
@pytest.mark.parametrize("outcome", ["success", "nonzero", "exception"])
@pytest.mark.parametrize("mutation", ["server", "manifest", None])
def test_post_npm_verifies_signed_content_before_spawn(
    node_app: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
    lock: str | None,
    outcome: str,
    mutation: str | None,
) -> None:
    directories = [node_app.resolve(), (node_app / "backend").resolve()]
    if lock:
        for directory_path in directories:
            (directory_path / lock).write_text('{"lockfileVersion":3}', encoding="utf-8")
    held = sign(node_app)
    mutate_at = directories[0 if directory == "root" else 1]
    attempted: list[tuple[Path, list[str]]] = []

    def npm(argv, **kwargs):
        cwd = Path(kwargs["cwd"])
        attempted.append((cwd, list(argv)))
        if cwd == mutate_at:
            if mutation == "server":
                (node_app / "backend" / "server.js").write_text("// changed\n", encoding="utf-8")
            elif mutation == "manifest":
                document = json.loads((node_app / "app.json").read_text(encoding="utf-8"))
                document["description"] = "changed by lifecycle script"
                (node_app / "app.json").write_text(json.dumps(document), encoding="utf-8")
            if outcome == "exception":
                raise OSError("npm interrupted after lifecycle")
            if outcome == "nonzero":
                return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(backend, "run_limited", npm)
    verify = Mock(wraps=admission.installed_content_refusal)
    monkeypatch.setattr(backend, "_installed_content_refusal", verify)
    spawned = _capture_popen(monkeypatch)
    if mutation:
        assert backend._start_app_backend_body("probe", held) is None
        assert spawned == {}
        assert admission.sel().log_api_access.call_args.kwargs["outcome"] == "denied"
    else:
        with pytest.raises(_StopSpawn):
            backend._start_app_backend_body("probe", held)
        assert spawned["argv"]
    assert [cwd for cwd, _argv in attempted] == directories
    for _cwd, argv in attempted:
        assert argv[1] == ("ci" if lock else "install")
        assert ("--no-package-lock" in argv) is (lock is None)
    assert verify.call_count == 2
    assert verify.call_args.args == ("probe", node_app / "backend" / "server.js")


@pytest.mark.parametrize(
    "control", ["ready", "no_package", "no_npm", "wrap_error", "builtin", "no_policy"]
)
def test_post_npm_check_preserves_fast_paths(
    node_app: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, control: str
) -> None:
    if control == "ready":
        for directory in (node_app, node_app / "backend"):
            (directory / "node_modules").mkdir()
    elif control == "no_package":
        for directory in (node_app, node_app / "backend"):
            (directory / "package.json").unlink()
    elif control == "no_npm":
        monkeypatch.setattr(backend, "_find_npm_binary", lambda: None)
    elif control == "builtin":
        monkeypatch.setattr(backend, "is_builtin_app", lambda **kwargs: True)
        monkeypatch.setattr(execution, "is_builtin_app", lambda **kwargs: True)
    elif control == "no_policy":
        monkeypatch.setattr(admission, "config_dir", lambda: tmp_path / "absent-policy")
    held = sign(node_app)

    def npm(argv, **kwargs):
        (node_app / "backend" / "server.js").write_text("// changed\n", encoding="utf-8")
        return SimpleNamespace(returncode=1)

    attempted = Mock(side_effect=npm)
    monkeypatch.setattr(backend, "run_limited", attempted)
    if control == "wrap_error":

        def wrap(argv, **kwargs):
            if argv[0] == str(tmp_path / "npm"):
                raise OSError("npm sandbox unavailable")
            return list(argv), None

        monkeypatch.setattr(backend, "wrap_argv", wrap)
    verify = Mock(wraps=admission.installed_content_refusal)
    monkeypatch.setattr(backend, "_installed_content_refusal", verify)
    _capture_popen(monkeypatch)
    with pytest.raises(_StopSpawn):
        backend._start_app_backend_body("probe", held)
    assert attempted.call_count == (2 if control in {"builtin", "no_policy"} else 0)
    assert verify.call_count == (0 if control == "builtin" else 2 if control == "no_policy" else 1)


def test_post_npm_verifier_exception_refuses_spawn(
    node_app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = sign(node_app)
    monkeypatch.setattr(
        backend, "run_limited", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    verify = Mock(side_effect=[None, OSError("verification unavailable")])
    monkeypatch.setattr(backend, "_installed_content_refusal", verify)
    spawned = _capture_popen(monkeypatch)
    assert backend._start_app_backend_body("probe", held) is None
    assert verify.call_count == 2
    assert spawned == {}


@pytest.fixture
def boot_app(signed_home: Path, monkeypatch: pytest.MonkeyPatch):
    root = signed_home / "apps" / "probe"
    write_app(root, backend={"entryPoint": "server.py"})
    (root / "server.py").write_text("VALUE = 'published'\n", encoding="utf-8")
    sign(root)
    manager._write_installed("probe", InstalledApp(name="probe", version="1.0.0", enabled=True))
    snapshots = []

    def listed():
        rows = [
            {
                "name": "probe",
                "enabled": manager._read_installed("probe").enabled,
                "manifest": manager.get_app_manifest("probe").to_dict(),
            }
        ]
        snapshots.append(rows)
        return rows

    monkeypatch.setattr(startup, "list_apps", listed)
    monkeypatch.setattr(startup, "_reap_stale_app_backends", lambda: None)
    monkeypatch.setattr(startup, "_app_activation_denied", lambda *args: None)
    monkeypatch.setattr(admission, "app_version_refusal", lambda *args: None)
    monkeypatch.setattr(startup, "app_execution_denied", lambda *args, **kwargs: None)
    monkeypatch.setattr(startup, "shipped_builtin_app_root", lambda name: None)
    monkeypatch.setattr(execution, "is_builtin_app", lambda **kwargs: False)
    monkeypatch.setattr(
        startup, "_app_enabled_state", lambda name: manager._read_installed(name).enabled
    )
    monkeypatch.setattr(startup, "sel", lambda: Mock())
    registered = Mock(return_value=bridges.RegistrationResult())
    monkeypatch.setattr(bridges, "register_app", registered)
    monkeypatch.setattr(bridges, "reconcile_app_skills", lambda name: [])
    revoked = Mock(return_value=[])
    for name in ("_deregister_agents", "_deregister_skills", "_deregister_mcp_servers"):
        monkeypatch.setattr(bridges, name, revoked)
    spawned = Mock(side_effect=lambda names: names)
    monkeypatch.setattr(startup, "_start_backends_concurrently", spawned)
    return root, snapshots, registered, revoked, spawned


@pytest.mark.parametrize("require_signature", [False, True])
@pytest.mark.parametrize("cause", ["ban", "allowlist", "exception"])
@pytest.mark.parametrize(
    "phase,has_backend", [("resources", False), ("resources", True), ("backend", True)]
)
def test_boot_policy_refusals_recover_without_persisted_disable(
    boot_app,
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_signature: bool,
    cause: str,
    phase: str,
    has_backend: bool,
) -> None:
    root, snapshots, registered, revoked, spawned = boot_app
    if not has_backend:
        write_app(root, backend={})
        sign(root)
    policy = {"require_signature": require_signature, "trust_keys": {"test-publisher": SECRET}}
    if cause == "ban":
        policy["banned"] = ["probe"]
    elif cause == "allowlist":
        policy["approved"] = []
    (signed_home / "app_admission.json").write_text(json.dumps(policy), encoding="utf-8")
    original = admission.app_admission_denied
    calls = []

    def admit(*args, **kwargs):
        calls.append(args)
        if phase == "backend" and len(calls) == 1:
            return None
        if cause == "exception":
            raise OSError("temporary admission failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(startup, "app_admission_denied", admit)
    disable = Mock(wraps=manager.disable_app)
    monkeypatch.setattr(manager, "disable_app", disable)
    assert startup.start_enabled_app_backends() == []
    assert not snapshots[-1][0]["enabled"]
    assert manager._read_installed("probe").enabled
    disable.assert_not_called()
    assert revoked.call_count == 3
    assert registered.call_count == (1 if phase == "backend" else 0)
    policy.pop("banned", None)
    policy.pop("approved", None)
    (signed_home / "app_admission.json").write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(startup, "app_admission_denied", original)
    assert startup.start_enabled_app_backends() == (["probe"] if has_backend else [])
    assert snapshots[-1][0]["enabled"]
    assert manager._read_installed("probe").enabled
    disable.assert_not_called()


@pytest.mark.parametrize(
    "phase,has_backend", [("resources", False), ("resources", True), ("backend", True)]
)
@pytest.mark.parametrize("mutation", ["server", "manifest"])
def test_signed_tamper_persists_disable_only_for_packaged_files(
    boot_app, monkeypatch: pytest.MonkeyPatch, phase: str, has_backend: bool, mutation: str
) -> None:
    root, snapshots, registered, revoked, spawned = boot_app
    if not has_backend:
        write_app(root, backend={})
        sign(root)
    original = admission.app_admission_denied
    calls = []

    def admit(*args, **kwargs):
        calls.append(args)
        if phase == "resources" or len(calls) == 2:
            if mutation == "server":
                (root / "server.py").write_text("VALUE = 'changed'\n", encoding="utf-8")
            else:
                document = json.loads((root / "app.json").read_text(encoding="utf-8"))
                document["description"] = "changed"
                (root / "app.json").write_text(json.dumps(document), encoding="utf-8")
                kwargs["manifest"] = manager.get_app_manifest("probe")
        return original(*args, **kwargs)

    monkeypatch.setattr(startup, "app_admission_denied", admit)
    assert startup.start_enabled_app_backends() == []
    assert not snapshots[-1][0]["enabled"]
    # A changed app.json fails to verify, which is indistinguishable from an
    # older-scheme signature, so only a changed packaged file is persisted.
    assert manager._read_installed("probe").enabled is (mutation == "manifest")
    assert revoked.call_count == 3


def test_signature_refusal_classification_does_not_match_prose(signed_home: Path) -> None:
    refusal = content_mismatch_refusal(signed_home / "apps" / "probe")
    assert isinstance(refusal, str)
    assert admission._is_signature_refusal(refusal)
    assert not admission._is_signature_refusal(str(refusal))
    assert not admission._is_signature_refusal(None)


_IO_PHASES = [
    "open",
    "fdopen",
    "fstat",
    "read",
    "close",
    "scandir",
    "iteration",
    "entry_stat",
    "root_stat",
]


def _inject_inventory_io_failure(patched: pytest.MonkeyPatch, root: Path, phase: str) -> None:
    """Fail only the inventory's real filesystem seam, leaving other readers intact."""
    import contextlib

    real_os = signed_content.os
    owned_fds: set[int] = set()

    class FileRead:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            data = self.stream.read(size)
            if phase == "read":
                raise OSError("temporary read failure")
            return data

    class Entry:
        def __init__(self, entry):
            self.entry = entry
            self.name, self.path = entry.name, entry.path

        def stat(self, **kwargs):
            if self.name == "server.py" and phase == "entry_stat":
                raise OSError("temporary entry stat failure")
            return self.entry.stat(**kwargs)

    class InventoryOS:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def open(self, path, *args, **kwargs):
            if Path(path) == root / "server.py":
                if phase == "open":
                    raise OSError("temporary file open failure")
                fd = real_os.open(path, *args, **kwargs)
                owned_fds.add(fd)
                return fd
            return real_os.open(path, *args, **kwargs)

        def fdopen(self, fd, *args, **kwargs):
            if fd in owned_fds and phase == "fdopen":
                raise OSError("temporary fdopen failure")
            stream = real_os.fdopen(fd, *args, **kwargs)
            if fd not in owned_fds:
                return stream

            @contextlib.contextmanager
            def opened():
                with stream:
                    yield FileRead(stream)
                if phase == "close":
                    raise OSError("temporary close failure")

            return opened()

        def fstat(self, fd):
            if fd in owned_fds and phase == "fstat":
                raise OSError("temporary fstat failure")
            return real_os.fstat(fd)

        @contextlib.contextmanager
        def scandir(self, directory):
            if Path(directory) == root and phase == "scandir":
                raise OSError("temporary scandir failure")
            with real_os.scandir(directory) as entries:

                def streamed():
                    for entry in entries:
                        if Path(directory) == root and phase == "iteration":
                            raise OSError("temporary directory iteration failure")
                        yield Entry(entry)

                yield streamed()

    patched.setattr(signed_content, "os", InventoryOS())
    if phase == "root_stat":
        real_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == root:
                raise OSError("temporary root stat failure")
            return real_stat(path, *args, **kwargs)

        patched.setattr(Path, "stat", stat)


@pytest.mark.parametrize("phase", _IO_PHASES)
def test_inventory_io_kind_survives_all_refusal_layers(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    root = signed_home / "apps" / "probe"
    write_app(root)
    (root / "server.py").write_text("VALUE = 'published'\n", encoding="utf-8")
    held = sign(root)
    sink = Mock()
    monkeypatch.setattr(admission, "sel", lambda: sink)
    monkeypatch.setattr(execution, "is_builtin_app", lambda **kwargs: False)
    with monkeypatch.context() as patched:
        _inject_inventory_io_failure(patched, root, phase)
        mismatch = signed_content.inventory_mismatch(root, held.fileDigests)
        assert isinstance(mismatch, signed_content._InventoryUnavailable)
        refusal = admission.signed_content_refusal("probe", held, root)
        assert isinstance(refusal, signed_content._InventoryUnavailable)
        denied = admission.app_admission_denied("probe", held, "boot")
        assert isinstance(denied, signed_content._InventoryUnavailable)
        assert not admission._is_signature_refusal(denied)
        assert isinstance(denied, str) and json.loads(json.dumps(denied)) == str(denied)
        assert admission.verified_installed_manifest("probe", root, held) == (refusal, None)
        assert sink.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert sink.log_api_access.call_args.kwargs["error"] == refusal
    assert signed_content.inventory_mismatch(root, held.fileDigests) is None
    assert admission.app_admission_denied("probe", held, "boot") is None


@pytest.mark.parametrize("io_phase", _IO_PHASES)
@pytest.mark.parametrize(
    "boot_phase,has_backend", [("resources", False), ("resources", True), ("backend", True)]
)
def test_inventory_io_refuses_one_boot_and_recovers(
    boot_app, monkeypatch: pytest.MonkeyPatch, io_phase: str, boot_phase: str, has_backend: bool
) -> None:
    root, snapshots, registered, revoked, spawned = boot_app
    if not has_backend:
        write_app(root, backend={})
        sign(root)
    original = admission.app_admission_denied
    calls = []

    def admit(*args, **kwargs):
        calls.append(args)
        if boot_phase == "resources" or len(calls) == 2:
            with monkeypatch.context() as patched:
                _inject_inventory_io_failure(patched, root, io_phase)
                return original(*args, **kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(startup, "app_admission_denied", admit)
    disable = Mock(wraps=manager.disable_app)
    monkeypatch.setattr(manager, "disable_app", disable)
    assert startup.start_enabled_app_backends() == []
    assert not snapshots[-1][0]["enabled"]
    assert manager._read_installed("probe").enabled
    disable.assert_not_called()
    assert revoked.call_count == 3
    monkeypatch.setattr(startup, "app_admission_denied", original)
    assert startup.start_enabled_app_backends() == (["probe"] if has_backend else [])
    assert snapshots[-1][0]["enabled"]
    assert manager._read_installed("probe").enabled
    disable.assert_not_called()


@pytest.mark.parametrize("unreadable", ["manifest", "policy"])
@pytest.mark.parametrize(
    "boot_phase,has_backend", [("resources", False), ("resources", True), ("backend", True)]
)
def test_unreadable_manifest_or_policy_does_not_persist_disable(
    boot_app,
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    unreadable: str,
    boot_phase: str,
    has_backend: bool,
) -> None:
    root, snapshots, registered, revoked, spawned = boot_app
    if not has_backend:
        write_app(root, backend={})
        sign(root)
    target = root / "app.json" if unreadable == "manifest" else signed_home / "app_admission.json"
    real_read = Path.read_text
    real_manifest_read = manifest_module.read_bytes_with_retry
    original = admission.app_admission_denied
    calls = []

    def read(path, *args, **kwargs):
        if path == target:
            raise OSError("temporary document read failure")
        return real_read(path, *args, **kwargs)

    def read_manifest(path, *args, **kwargs):
        if path == target:
            raise OSError("temporary manifest read failure")
        return real_manifest_read(path, *args, **kwargs)

    def admit(*args, **kwargs):
        calls.append(args)
        if boot_phase == "resources" or len(calls) == 2:
            with monkeypatch.context() as patched:
                if unreadable == "manifest":
                    patched.setattr(manifest_module, "read_bytes_with_retry", read_manifest)
                    kwargs["manifest"] = manager.get_app_manifest("probe")
                else:
                    patched.setattr(Path, "read_text", read)
                return original(*args, **kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(startup, "app_admission_denied", admit)
    monkeypatch.setattr(admission, "sel", lambda: Mock())
    disable = Mock(wraps=manager.disable_app)
    monkeypatch.setattr(manager, "disable_app", disable)
    assert startup.start_enabled_app_backends() == []
    assert not snapshots[-1][0]["enabled"]
    assert manager._read_installed("probe").enabled
    assert revoked.call_count == 3
    disable.assert_not_called()
    monkeypatch.setattr(startup, "app_admission_denied", original)
    assert startup.start_enabled_app_backends() == (["probe"] if has_backend else [])
    assert manager._read_installed("probe").enabled
    disable.assert_not_called()


@pytest.mark.parametrize("invalid", ["inventory_type", "declared_name", "declared_digest"])
@pytest.mark.parametrize("has_backend", [False, True])
def test_proven_invalid_inventory_still_persists_disable(
    boot_app, has_backend: bool, invalid: str
) -> None:
    root, snapshots, registered, revoked, spawned = boot_app
    if not has_backend:
        write_app(root, backend={})
        sign(root)
    path = root / "app.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if invalid == "inventory_type":
        document["fileDigests"] = ["server.py"]
    elif invalid == "declared_name":
        document["fileDigests"] = {"../server.py": "0" * 64}
    elif invalid == "declared_digest":
        document["fileDigests"]["server.py"] = "not-a-digest"
    document["signature"] = signed_content.app_signature(SECRET, AppManifest.from_dict(document))
    path.write_text(json.dumps(document), encoding="utf-8")
    held = manager.get_app_manifest("probe")
    assert held is not None and admission._signature_valid(
        held, admission.load_app_admission_policy()
    )
    assert startup.start_enabled_app_backends() == []
    assert not snapshots[-1][0]["enabled"]
    assert not manager._read_installed("probe").enabled
    assert revoked.call_count == 3


def test_unknown_manifest_refusal_is_not_proof_of_bad_signature(signed_home: Path) -> None:
    refused = admission.app_admission_denied("probe", manifest=None, action="boot")
    assert refused and isinstance(refused, str)
    assert not admission._is_signature_refusal(refused)


def test_non_directory_inventory_is_proven_invalid(boot_app) -> None:
    root, _snapshots, _registered, _revoked, _spawned = boot_app
    held = manager.get_app_manifest("probe")
    denied = admission.app_admission_denied("probe", held, "boot", app_root=root / "server.py")
    assert admission._is_signature_refusal(denied)
    assert "not a directory" in denied


@pytest.mark.parametrize("has_backend", [False, True])
def test_actual_directory_link_persists_disable(
    boot_app, tmp_path: Path, has_backend: bool
) -> None:
    from kiro_crew.testing.links import make_dir_link

    root, snapshots, _registered, revoked, _spawned = boot_app
    if not has_backend:
        write_app(root, backend={})
        sign(root)
    target = tmp_path / "outside"
    target.mkdir()
    make_dir_link(root / "linked", target)
    assert startup.start_enabled_app_backends() == []
    assert not snapshots[-1][0]["enabled"]
    assert not manager._read_installed("probe").enabled
    assert revoked.call_count == 3


def test_unreadable_manifest_is_denied_and_audited_by_shared_verifier(
    boot_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _snapshots, _registered, _revoked, _spawned = boot_app
    real_read = manifest_module.read_bytes_with_retry
    sink = Mock()
    monkeypatch.setattr(admission, "sel", lambda: sink)

    def read(path, *args, **kwargs):
        if path == root / "app.json":
            raise OSError("temporary manifest read failure")
        return real_read(path, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(manifest_module, "read_bytes_with_retry", read)
        assert manager.get_app_manifest("probe") is None
        refusal, held = admission.verified_installed_manifest("probe", root)
        assert refusal and held is None
        sink.log_api_access.assert_called_once()
        assert sink.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert sink.log_api_access.call_args.kwargs["error"] == refusal
    assert admission.verified_installed_manifest("probe", root)[0] is None


def test_boot_classification_does_not_repeat_the_inventory_walk(
    boot_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _snapshots, _registered, _revoked, _spawned = boot_app
    held = manager.get_app_manifest("probe")
    (root / "server.py").write_text("VALUE = 'changed'\n", encoding="utf-8")
    inventory = Mock(wraps=signed_content.packaged_inventory)
    monkeypatch.setattr(signed_content, "packaged_inventory", inventory)
    denied = admission.app_admission_denied("probe", held, "boot")
    assert admission._is_signature_refusal(denied)
    inventory.assert_called_once_with(root)


def test_backend_spawn_does_not_log_child_command(
    node_app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = sign(node_app)
    monkeypatch.setattr(backend, "_find_npm_binary", lambda: None)
    marker = "private-child-argument"
    monkeypatch.setattr(backend, "cgroup_scope_argv", lambda cmd: [*cmd, marker])
    info = Mock()
    monkeypatch.setattr(backend.logger, "info", info)
    spawned = _capture_popen(monkeypatch)
    with pytest.raises(_StopSpawn):
        backend._start_app_backend_body("probe", held)
    assert marker in spawned["argv"]
    assert any(call.args[0] == "Spawning app %s backend on port %s" for call in info.call_args_list)
    assert marker not in repr(info.call_args_list)


@pytest.mark.parametrize(
    "phase,has_backend", [("resources", False), ("resources", True), ("backend", True)]
)
@pytest.mark.parametrize("sink_down", [False, True])
def test_each_boot_refusal_emits_one_best_effort_denial(
    boot_app,
    signed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    has_backend: bool,
    sink_down: bool,
) -> None:
    root, snapshots, registered, revoked, spawned = boot_app
    if not has_backend:
        write_app(root, backend={})
        sign(root)
    (signed_home / "app_admission.json").write_text(
        json.dumps({"banned": ["probe"]}), encoding="utf-8"
    )
    original = admission.app_admission_denied
    calls = 0

    def admit(*args, **kwargs):
        nonlocal calls
        calls += 1
        if phase == "backend" and calls == 1:
            return None
        return original(*args, **kwargs)

    monkeypatch.setattr(startup, "app_admission_denied", admit)
    sink = Mock()
    if sink_down:
        sink.log_api_access.side_effect = RuntimeError("audit sink unavailable")
    monkeypatch.setattr(startup, "sel", lambda: sink)
    assert startup.start_enabled_app_backends() == []
    sink.log_api_access.assert_called_once()
    event = sink.log_api_access.call_args.kwargs
    assert event["caller"] == "gateway"
    assert event["operation"] == "app_backend_boot"
    assert event["outcome"] == "denied"
    assert event["resources"] == "probe"
    assert "banned" in event["error"]
    assert not snapshots[-1][0]["enabled"]
    assert manager._read_installed("probe").enabled
    assert revoked.call_count == 3
    assert registered.call_count == (1 if phase == "backend" else 0)
