"""The ``kiro_crew.pod.runtime`` namespace is the pod suite's one patch seam.

Every pod test, the pod CLI and Dev Fleet reach the pod runtime as ``rt.<name>``,
and roughly seven hundred test sites patch collaborators there. Whichever module
defines a name, a patch of ``rt.<name>`` has to reach the code that reads it.

Each row below patches ONE name on the runtime namespace with a stub that raises
``_Reached`` and then drives a consumer that must read that name before anything
else can happen. ``_Reached`` derives from ``BaseException`` on purpose: several
consumers wrap their collaborators in ``except Exception`` (``port_owner`` turns
any failure into ``OWNER_UNPROVEN``), and a stub those handlers could swallow
would let a patch that MISSED read as one that landed.
"""

from __future__ import annotations

import ast
import importlib
import os
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Callable
from unittest import mock

import pytest

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.pod import launchd
from kiro_crew.pod import provision as prov
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import EXIT_REFUSED_UNRECOVERABLE, PodConfig

_HOST_IS_WINDOWS = platform_compat.IS_WINDOWS
_POD = "wt"
_PORT = 7811


class _Reached(BaseException):
    """Raised by a stub to prove the consumer read the patched name."""


def _raiser(label: str) -> Callable[..., object]:
    def _stub(*_args: object, **_kwargs: object) -> object:
        raise _Reached(label)

    return _stub


def _value(result: object) -> Callable[..., object]:
    return lambda *_args, **_kwargs: result


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pod-root"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", False)
    return PodConfig.load()


def _ready_checkout(root: Path) -> Path:
    checkout = root / "checkout"
    binary = prov.venv_bin(checkout)
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    (checkout / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)
    return checkout


def _pin(cfg: PodConfig, checkout: Path) -> None:
    rt.write_env_file(cfg, _POD, {"CHECKOUT": str(checkout)})


# --------------------------------------------------------------------------- #
# Each case: (patched name, setup(cfg, tmp_path, monkeypatch) -> consumer thunk).
# The setup pins every EARLIER collaborator so the consumer reaches the patched
# name without touching the host; the patched name itself is installed by the
# test body, after setup, so a setup cannot accidentally satisfy it.
# --------------------------------------------------------------------------- #


def _port_owner_linux(cfg, tmp_path, mp):
    return lambda: rt.port_owner(cfg, _POD, _PORT)


def _port_owner_after_record(cfg, tmp_path, mp):
    mp.setattr(rt, "_pod_recorded_pid", _value(None))
    return _port_owner_linux(cfg, tmp_path, mp)


def _port_owner_after_main_pid(cfg, tmp_path, mp):
    mp.setattr(rt, "_pod_recorded_pid", _value(None))
    mp.setattr(rt, "main_pid", _value(None))
    return _port_owner_linux(cfg, tmp_path, mp)


def _port_owner_after_tool(cfg, tmp_path, mp):
    mp.setattr(rt, "_pod_recorded_pid", _value(None))
    mp.setattr(rt, "main_pid", _value(None))
    mp.setattr(rt, "listening_pid_tool_available", _value(True))
    return _port_owner_linux(cfg, tmp_path, mp)


def _port_owner_windows(cfg, tmp_path, mp):
    mp.setattr(rt, "IS_WINDOWS", True)
    mp.setattr(rt, "_pod_recorded_pid", _value(None))
    mp.setattr(rt, "main_pid", _value(4242))
    return _port_owner_linux(cfg, tmp_path, mp)


def _port_owner_windows_tree(cfg, tmp_path, mp):
    _port_owner_windows(cfg, tmp_path, mp)
    mp.setattr(rt, "process_start_time", _value("start-token"))
    return _port_owner_linux(cfg, tmp_path, mp)


def _health(cfg, tmp_path, mp):
    return lambda: rt.health(cfg, _POD, _PORT, timeout=1)


class _Answer:
    status = 200

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _health_after_probe(cfg, tmp_path, mp):
    mp.setattr(rt, "loopback_urlopen", _value(_Answer()))
    return _health(cfg, tmp_path, mp)


def _mint(cfg, tmp_path, mp):
    return lambda: rt.mint_token(cfg, _POD)


def _mint_after_port(cfg, tmp_path, mp):
    mp.setattr(rt, "derive_port", _value(_PORT))
    home = cfg.home_dir(_POD)
    home.mkdir(parents=True)
    (home / ".local_secret").write_text("secret")
    return _mint(cfg, tmp_path, mp)


def _pod_api(cfg, tmp_path, mp):
    return lambda: rt.pod_api(cfg, _POD, "GET", "/api/health")


def _pod_api_after_active(cfg, tmp_path, mp):
    mp.setattr(rt, "is_active", _value(True))
    return _pod_api(cfg, tmp_path, mp)


def _pod_api_after_port(cfg, tmp_path, mp):
    _pod_api_after_active(cfg, tmp_path, mp)
    mp.setattr(rt, "derive_port", _value(_PORT))
    return _pod_api(cfg, tmp_path, mp)


def _pod_api_after_socket(cfg, tmp_path, mp):
    _pod_api_after_port(cfg, tmp_path, mp)
    sock = tmp_path / "dashboard.sock"
    sock.write_text("")
    mp.setattr(rt, "pod_socket_path", _value(sock))
    return _pod_api(cfg, tmp_path, mp)


def _pod_api_after_mint(cfg, tmp_path, mp):
    _pod_api_after_socket(cfg, tmp_path, mp)
    mp.setattr(rt, "mint_token", _value("tok"))
    mp.setattr(rt, "_pod_recorded_pid", _value(4242))
    return _pod_api(cfg, tmp_path, mp)


def _peer_verify(cfg, tmp_path, mp):
    mp.setattr(rt, "_pod_recorded_pid", _value(4242))
    verify = rt._attested_gateway_verifier(cfg, _POD, _PORT, tmp_path / "dashboard.sock")
    return lambda: verify(object())  # type: ignore[arg-type]


def _allocate(cfg, tmp_path, mp):
    return lambda: rt.allocate_port(cfg, _POD)


def _derive(cfg, tmp_path, mp):
    return lambda: rt.derive_port(cfg, _POD)


def _probe_port(cfg, tmp_path, mp):
    return lambda: rt._port_is_free(_PORT)


def _stop(cfg, tmp_path, mp):
    return lambda: rt.stop_pod(cfg, _POD)


def _stop_after_hook(cfg, tmp_path, mp):
    mp.setattr(rt, "loaded_teardown_hook", _value(False))
    return _stop(cfg, tmp_path, mp)


def _stop_after_cgroup(cfg, tmp_path, mp):
    _stop_after_hook(cfg, tmp_path, mp)
    mp.setattr(rt, "cgroup_procs_file", _value(None))
    mp.setattr(
        rt, "systemctl", _value(subprocess.CompletedProcess(args=[], returncode=0, stdout=""))
    )
    return _stop(cfg, tmp_path, mp)


def _stop_launchd(cfg, tmp_path, mp):
    mp.setattr(launchd, "stop", _raiser("launchd.stop"))
    return _stop(cfg, tmp_path, mp)


def _drain(cfg, tmp_path, mp):
    procs = tmp_path / "cgroup.procs"
    procs.write_text("4242\n")
    return lambda: rt.drain_cgroup(procs, timeout=60)


def _install(cfg, tmp_path, mp):
    return lambda: rt.install_backend(cfg)


def _install_after_gate(cfg, tmp_path, mp):
    mp.setattr(rt, "require_backend", _value(None))
    return _install(cfg, tmp_path, mp)


def _orphans(cfg, tmp_path, mp):
    cfg.pod_root.mkdir(parents=True)
    return lambda: rt.orphan_homes(cfg)


def _resolve(cfg, tmp_path, mp):
    return lambda: rt.resolve_checkout(cfg, _POD, cwd=tmp_path)


def _write_env(cfg, tmp_path, mp):
    return lambda: rt.write_env_file(cfg, _POD, {"PORT": "7811"})


def _cleanup(cfg, tmp_path, mp):
    return lambda: rt.cleanup_home(cfg, _POD)


def _boot(cfg, tmp_path, mp):
    return lambda: rt.boot(cfg, _POD)


def _boot_pinned(cfg, tmp_path, mp):
    _pin(cfg, _ready_checkout(tmp_path))
    return _boot(cfg, tmp_path, mp)


def _boot_after_port(cfg, tmp_path, mp):
    _boot_pinned(cfg, tmp_path, mp)
    mp.setattr(rt, "derive_port", _value(_PORT))
    return _boot(cfg, tmp_path, mp)


def _boot_after_os_home(cfg, tmp_path, mp):
    _boot_after_port(cfg, tmp_path, mp)
    mp.setattr(rt, "_seed_pod_os_home", _value(None))
    return _boot(cfg, tmp_path, mp)


def _refuse(cfg, tmp_path, mp):
    return lambda: rt._refuse(cfg, _POD, EXIT_REFUSED_UNRECOVERABLE, "why")


def _terminal_code(cfg, tmp_path, mp):
    mp.setattr(rt, "IS_MACOS", True)
    return lambda: rt.terminal_exit_code(cfg, _POD, EXIT_REFUSED_UNRECOVERABLE)


def _exec(cfg, tmp_path, mp):
    return lambda: rt.exec_in_pod(cfg, _POD, ["status"])


def _child_probe(cfg, tmp_path, mp):
    from kiro_crew.agent_sdk import pod_child_probe

    def _capture(_env: object, *, timeout_secs: float) -> object:
        raise _Reached(timeout_secs)

    mp.setattr(pod_child_probe, "probe_pod_child_bootstrap", _capture)
    return lambda: rt._probe_pod_child_bootstrap({})


def _seed_scenario(cfg, tmp_path, mp):
    return lambda: rt.seed_home_from_scenario(cfg, _POD, "minimal")


def _seed_scenario_windows(cfg, tmp_path, mp):
    mp.setattr(rt, "resolve_seed_scenario", _value("minimal"))
    mp.setattr(rt, "IS_WINDOWS", True)
    mp.setattr(pinned_fs, "supports_pinned_tree_walk", _value(False))
    return _seed_scenario(cfg, tmp_path, mp)


def _seeded_marker_windows(cfg, tmp_path, mp):
    mp.setattr(rt, "IS_WINDOWS", True)
    return lambda: rt.seeded_scenario_in_home(cfg, _POD)


def _os_home(cfg, tmp_path, mp):
    os_home = tmp_path / "pod" / "os-home"
    os_home.parent.mkdir(parents=True)
    return lambda: rt._seed_pod_os_home(os_home)


def _os_home_windows(cfg, tmp_path, mp):
    mp.setattr(rt, "IS_WINDOWS", True)
    mp.setattr(pinned_fs, "supports_pinned_walk", _value(False))
    return _os_home(cfg, tmp_path, mp)


def _prepare_seeded_fd(cfg, tmp_path, mp):
    home = tmp_path / "seeded"
    home.mkdir()
    fd = os.open(home, pinned_fs.dir_flags())

    def _call() -> None:
        try:
            rt._prepare_seeded_home_fd(fd)
        finally:
            os.close(fd)

    return _call


def _write_config(cfg, tmp_path, mp):
    return lambda: rt.write_pod_config(cfg.home_dir(_POD), "")


def _read_capped(cfg, tmp_path, mp):
    class _Stream:
        def read(self, _n: int) -> bytes:
            return b"0123456789"

    return lambda: rt._read_capped(_Stream(), "GET", "/api/x", _POD)


_POSIX_WALK = pytest.mark.skipif(
    _HOST_IS_WINDOWS, reason="needs O_DIRECTORY/dir_fd descriptors, which win32 lacks"
)

SEAMS = [
    # The port_owner collaborators test_pod_api's TestPodPidAttestation stubs: each
    # one must still reach port_owner, or a test run spawns a real service-manager
    # or process-table query.
    pytest.param("_pod_recorded_pid", _port_owner_linux, id="port_owner<-_pod_recorded_pid"),
    pytest.param("main_pid", _port_owner_after_record, id="port_owner<-main_pid"),
    pytest.param(
        "listening_pid_tool_available",
        _port_owner_after_main_pid,
        id="port_owner<-listening_pid_tool_available",
    ),
    pytest.param(
        "find_port_listeners", _port_owner_after_tool, id="port_owner<-find_port_listeners"
    ),
    pytest.param("process_start_time", _port_owner_windows, id="port_owner<-process_start_time"),
    pytest.param(
        "attributed_descendants", _port_owner_windows_tree, id="port_owner<-attributed_descendants"
    ),
    pytest.param("loopback_urlopen", _health, id="health<-loopback_urlopen"),
    pytest.param("port_owner", _health_after_probe, id="health<-port_owner"),
    pytest.param("derive_port", _mint, id="mint_token<-derive_port"),
    pytest.param("port_owner", _mint_after_port, id="mint_token<-port_owner"),
    pytest.param("validate_name", _pod_api, id="pod_api<-validate_name"),
    pytest.param("is_active", _pod_api, id="pod_api<-is_active"),
    pytest.param("derive_port", _pod_api_after_active, id="pod_api<-derive_port"),
    pytest.param("pod_socket_path", _pod_api_after_port, id="pod_api<-pod_socket_path"),
    pytest.param("mint_token", _pod_api_after_socket, id="pod_api<-mint_token"),
    pytest.param("unix_socket_urlopen", _pod_api_after_mint, id="pod_api<-unix_socket_urlopen"),
    pytest.param("get_peer_pid", _peer_verify, id="verifier<-get_peer_pid"),
    pytest.param("read_env_file", _derive, id="derive_port<-read_env_file"),
    pytest.param("_port_is_free", _allocate, id="allocate_port<-_port_is_free"),
    pytest.param("pod_name_mutex", _stop, id="stop_pod<-pod_name_mutex"),
    pytest.param("loaded_teardown_hook", _stop, id="stop_pod<-loaded_teardown_hook"),
    pytest.param("cgroup_procs_file", _stop_after_hook, id="stop_pod<-cgroup_procs_file"),
    pytest.param("systemctl", _stop_after_hook, id="stop_pod<-systemctl"),
    pytest.param("cleanup_home", _stop_after_cgroup, id="stop_pod<-cleanup_home"),
    pytest.param("require_backend", _install, id="install_backend<-require_backend"),
    pytest.param(
        "_write_and_load_unit", _install_after_gate, id="install_backend<-_write_and_load_unit"
    ),
    pytest.param("active_names", _orphans, id="orphan_homes<-active_names"),
    pytest.param("_git_worktrees", _resolve, id="resolve_checkout<-_git_worktrees"),
    pytest.param("pod_name_mutex", _write_env, id="write_env_file<-pod_name_mutex"),
    pytest.param("validate_name", _cleanup, id="cleanup_home<-validate_name"),
    pytest.param("_boot_unguarded", _boot, id="boot<-_boot_unguarded"),
    pytest.param("read_env_file", _boot, id="boot<-read_env_file"),
    pytest.param("derive_port", _boot_pinned, id="boot<-derive_port"),
    pytest.param("write_pod_config", _boot_after_port, id="boot<-write_pod_config"),
    pytest.param("_seed_pod_os_home", _boot_after_port, id="boot<-_seed_pod_os_home"),
    pytest.param("build_pod_env", _boot_after_os_home, id="boot<-build_pod_env"),
    pytest.param(
        "_probe_pod_child_bootstrap", _boot_after_os_home, id="boot<-_probe_pod_child_bootstrap"
    ),
    pytest.param("_record_refusal", _refuse, id="_refuse<-_record_refusal"),
    pytest.param("refusal_reason", _terminal_code, id="terminal_exit_code<-refusal_reason"),
    pytest.param("pod_context", _exec, id="exec_in_pod<-pod_context"),
    pytest.param(
        "resolve_seed_scenario", _seed_scenario, id="seed_home_from_scenario<-resolve_seed_scenario"
    ),
    pytest.param(
        "_seed_home_windows",
        _seed_scenario_windows,
        id="seed_home_from_scenario<-_seed_home_windows",
    ),
    pytest.param(
        "pin_directory", _seeded_marker_windows, id="seeded_scenario_in_home<-pin_directory"
    ),
    pytest.param(
        "_runtime_auth_store_mappings",
        _os_home,
        id="_seed_pod_os_home<-_runtime_auth_store_mappings",
        marks=_POSIX_WALK,
    ),
    pytest.param(
        "_seed_pod_os_home_windows",
        _os_home_windows,
        id="_seed_pod_os_home<-_seed_pod_os_home_windows",
    ),
    pytest.param("atomic_write", _write_config, id="write_pod_config<-atomic_write"),
    pytest.param(
        "atomic_write_at",
        _prepare_seeded_fd,
        id="_prepare_seeded_home_fd<-atomic_write_at",
        marks=_POSIX_WALK,
    ),
]


@pytest.mark.parametrize(("seam", "setup"), SEAMS)
def test_a_patch_on_the_runtime_namespace_reaches_its_reader(
    seam: str,
    setup: Callable[..., Callable[[], object]],
    cfg: PodConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = setup(cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(rt, seam, _raiser(seam))
    with pytest.raises(_Reached) as reached:
        consumer()
    assert reached.value.args == (seam,)


def test_a_platform_flag_patched_on_the_runtime_reaches_the_teardown(
    cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``IS_MACOS`` is read by ``stop_pod`` from the runtime namespace."""
    consumer = _stop_launchd(cfg, Path(), monkeypatch)
    monkeypatch.setattr(rt, "IS_MACOS", True)
    with pytest.raises(_Reached) as reached:
        consumer()
    assert reached.value.args == ("launchd.stop",)


def test_a_module_patched_through_the_runtime_is_the_one_its_readers_use(
    cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rt.time`` / ``rt.socket`` are the shared modules, so patching an attribute
    on them reaches every reader that spells ``time.sleep`` / ``socket.socket``."""
    drain = _drain(cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(rt.time, "sleep", _raiser("time.sleep"))
    with pytest.raises(_Reached) as reached:
        drain()
    assert reached.value.args == ("time.sleep",)

    probe = _probe_port(cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(rt.socket, "socket", _raiser("socket.socket"))
    with pytest.raises(_Reached) as reached:
        probe()
    assert reached.value.args == ("socket.socket",)


def test_a_constant_patched_on_the_runtime_reaches_its_reader(
    cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = _read_capped(cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(rt, "API_BODY_MAX_BYTES", 3)
    with pytest.raises(rt.PodError, match="more than 3 bytes"):
        read()

    probe = _child_probe(cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(rt, "_CHILD_VIABILITY_TIMEOUT_SECS", 1.5)
    with pytest.raises(_Reached) as reached:
        probe()
    assert reached.value.args == (1.5,)


def test_the_package_reexports_are_the_runtime_objects() -> None:
    import kiro_crew.pod as pod

    for name in ("PodError", "derive_port", "pod_home", "pod_unit", "resolve_checkout"):
        assert getattr(pod, name) is getattr(rt, name), name


# --------------------------------------------------------------------------- #
# Platform flags. ``IS_WINDOWS`` / ``IS_MACOS`` are the most-patched names on the
# runtime namespace, and a reader holding its own copy of a flag misses a patch
# silently wherever the host already agrees with the patched value -- which is how a
# miss stays green on Linux and fails only on another platform's shard. So every
# owner that branches on a flag gets a row that drives BOTH values and expects the
# branch to change.
# --------------------------------------------------------------------------- #


class _ProbeSocket:
    """Stands in for ``socket.socket``: says which branch configured it."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _ProbeSocket:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def setsockopt(self, _level: int, option: int, _value: int) -> None:
        if option == rt.socket.SO_REUSEADDR:
            raise _Reached("posix-branch")

    def bind(self, _address: object) -> None:
        raise _Reached("windows-branch")


def _flag_ports(cfg, tmp_path, mp):
    mp.setattr(rt.socket, "socket", _ProbeSocket)
    return lambda: rt._port_is_free(_PORT)


def _flag_client(cfg, tmp_path, mp):
    _mint_after_port(cfg, tmp_path, mp)
    mp.setattr(rt, "port_owner", _value(rt.OWNER_POD))
    mp.setattr(rt, "loopback_urlopen", _raiser("windows-branch"))
    mp.setattr(rt, "pod_socket_path", _raiser("posix-branch"))
    return _mint(cfg, tmp_path, mp)


def _flag_lifecycle(cfg, tmp_path, mp):
    from kiro_crew.pod import windows

    mp.setattr(windows, "start", _raiser("windows-branch"))
    mp.setattr(rt.unit_mod, "unit_is_current", _raiser("posix-branch"))
    return lambda: rt.start_pod(cfg, _POD)


def _flag_boot(cfg, tmp_path, mp):
    def _unguarded(*_args: object) -> int:
        raise OSError("posix-branch")

    mp.setattr(rt, "_boot_unguarded", _unguarded)
    mp.setattr(rt, "_refuse", _raiser("windows-branch"))
    return _boot(cfg, tmp_path, mp)


def _flag_home(cfg, tmp_path, mp):
    mp.setattr(rt, "pin_directory", _raiser("windows-branch"))
    mp.setattr(pinned_fs, "open_dir_pinned", _raiser("posix-branch"))
    return lambda: rt.seeded_scenario_in_home(cfg, _POD)


def _flag_home_macos(cfg, tmp_path, mp):
    (cfg.pod_root / "orphan").mkdir(parents=True)
    mp.setattr(rt, "active_names", _value(set()))
    mp.setattr(launchd, "plist_path", _raiser("macos-branch"))
    return lambda: rt.orphan_homes(cfg)


def _flag_lifecycle_macos(cfg, tmp_path, mp):
    mp.setattr(launchd, "stop", _raiser("macos-branch"))
    mp.setattr(rt, "loaded_teardown_hook", _raiser("posix-branch"))
    return _stop(cfg, tmp_path, mp)


def _flag_boot_macos(cfg, tmp_path, mp):
    mp.setattr(rt, "refusal_reason", _raiser("macos-branch"))
    return lambda: rt.terminal_exit_code(cfg, _POD, EXIT_REFUSED_UNRECOVERABLE)


def _flag_attestation(cfg, tmp_path, mp):
    mp.setattr(rt, "_pod_recorded_pid", _value(None))
    mp.setattr(rt, "main_pid", _value(4242))
    mp.setattr(rt, "process_start_time", _raiser("windows-branch"))
    mp.setattr(rt, "listening_pid_tool_available", _raiser("posix-branch"))
    return _port_owner_linux(cfg, tmp_path, mp)


FLAGS = [
    pytest.param("IS_WINDOWS", _flag_ports, "windows-branch", "posix-branch", id="ports"),
    pytest.param(
        "IS_WINDOWS", _flag_attestation, "windows-branch", "posix-branch", id="attestation"
    ),
    pytest.param("IS_WINDOWS", _flag_client, "windows-branch", "posix-branch", id="client"),
    pytest.param("IS_WINDOWS", _flag_lifecycle, "windows-branch", "posix-branch", id="lifecycle"),
    pytest.param("IS_WINDOWS", _flag_boot, "windows-branch", OSError, id="boot"),
    pytest.param("IS_WINDOWS", _flag_home, "windows-branch", "posix-branch", id="home"),
    pytest.param(
        "IS_MACOS", _flag_lifecycle_macos, "macos-branch", "posix-branch", id="lifecycle-macos"
    ),
    pytest.param("IS_MACOS", _flag_home_macos, "macos-branch", None, id="home-macos"),
    pytest.param("IS_MACOS", _flag_boot_macos, "macos-branch", None, id="boot-macos"),
]


@pytest.mark.parametrize(("flag", "setup", "when_true", "when_false"), FLAGS)
@pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
def test_a_platform_flag_patched_on_the_runtime_reaches_every_owner(
    flag: str,
    setup: Callable[..., Callable[[], object]],
    when_true: str,
    when_false: object,
    value: bool,
    cfg: PodConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = setup(cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(rt, flag, value)
    expected = when_true if value else when_false
    if expected is None:
        consumer()  # the other branch does not reach the stub at all
    elif isinstance(expected, type):
        with pytest.raises(expected):
            consumer()
    else:
        with pytest.raises(_Reached) as reached:
            consumer()
        assert reached.value.args == (expected,)


# --------------------------------------------------------------------------- #
# The facade contract: where each name lives and how the runtime namespace
# reaches it.
# --------------------------------------------------------------------------- #
_POD_DIR = Path(rt.__file__).parent

#: The runtime namespace's surface: the pod CLI, Dev Fleet and the pod suite read
#: these names on ``kiro_crew.pod.runtime``, whichever module defines them, so every
#: one must resolve there. Dropping a name is an API change, made here on purpose.
_RUNTIME_SURFACE = frozenset(
    {
        "API_BODY_MAX_BYTES",
        "API_METHODS",
        "API_READ_METHODS",
        "API_TIMEOUT_SECS",
        "APPROVAL_MODES",
        "AUTO_PORT_KEY",
        "CRONS_TRUE",
        "DRAIN_TIMEOUT_SECS",
        "EMBEDDINGS_FALSE",
        "EMBED_MODEL_OVERRIDE_ENVS",
        "EXIT_PROVISIONING",
        "EXIT_REFUSED_UNRECOVERABLE",
        "HEALTH_FOREIGN",
        "IS_LINUX",
        "IS_MACOS",
        "IS_WINDOWS",
        "OWNER_FOREIGN",
        "OWNER_POD",
        "OWNER_UNPROVEN",
        "Path",
        "PodBackendAbsent",
        "PodConfig",
        "PodError",
        "PodOwnershipUnproven",
        "RECLAIMED_MARKER",
        "SEED_DISABLED_SECTIONS",
        "SKIP_MODEL_DOWNLOAD_ENV",
        "SeedError",
        "StoreMapping",
        "TERMINAL_BOOT_EXIT_CODES",
        "USER_BUS_ERROR",
        "USER_BUS_NO_SESSION",
        "USER_BUS_REACHABLE",
        "USER_BUS_SANDBOXED_AWAY",
        "UTF8_TEXT",
        "UserBusProbe",
        "_CGROUP_ROOT",
        "_CHILD_VIABILITY_TIMEOUT_SECS",
        "_HOME_RECLAIM_ATTEMPTS",
        "_HOME_RECLAIM_PAUSE_SECS",
        "_MAX_ECHOED_DETAIL_LEN",
        "_MAX_PEER_ENV_BYTES",
        "_MAX_PORT_DIGITS",
        "_MINT_403_BODY_CAP",
        "_MUTEX_STATE",
        "_NAME_RE",
        "_PLANE_LOCK_NAME",
        "_POD_EQUIVALENT",
        "_POD_RECREATE",
        "_POD_SAFE_VERBS",
        "_RUNTIME_AUTH_STORE_FILE_CAP",
        "_SQLITE_SIDECAR_SUFFIXES",
        "_SQLITE_SUFFIXES",
        "_USER_BUS_PROBE_TIMEOUT_SECONDS",
        "_address_socket_paths",
        "_apply_seed_config_floor",
        "_attested_gateway_verifier",
        "_authenticated_url",
        "_boot_unguarded",
        "_clear_refusal",
        "_close_fd",
        "_ensure_pod_dir",
        "_fixture_name_from_manifest_text",
        "_force_seed_agent_security",
        "_git_worktrees",
        "_install_pod_dropin",
        "_is_sqlite_sidecar",
        "_mint_403_cause",
        "_no_user_manager_remedy",
        "_open_seed_regular_file",
        "_parse_env_text",
        "_peer_effective_port",
        "_pin_created_dir_windows",
        "_pin_outermost_existing_windows",
        "_pinned_port",
        "_pod_mint_secret",
        "_pod_pid_record_path",
        "_pod_recorded_pid",
        "_pod_secret_path",
        "_port_from_env",
        "_port_is_free",
        "_ports_claimed_by_other_pods",
        "_posix_cksum",
        "_prepare_seeded_home_dir",
        "_prepare_seeded_home_fd",
        "_probe_health",
        "_probe_pod_child_bootstrap",
        "_read_capped",
        "_read_peer_env",
        "_record_refusal",
        "_refresh_stale_unit",
        "_refuse",
        "_refuse_reparse_chain",
        "_rmtree_bounded",
        "_run",
        "_runtime_auth_store_mappings",
        "_scrub_json_tokens",
        "_scrub_token",
        "_scrub_token_string",
        "_seed_home_windows",
        "_seed_pod_os_home",
        "_seed_pod_os_home_windows",
        "_seeded_scenario_from_fd",
        "_seeded_scenario_in_dir",
        "_session_runtime_dir",
        "_snapshot_sqlite_pinned",
        "_stage_runtime_auth_store",
        "_stage_runtime_auth_store_windows",
        "_stop_pod_launchd",
        "_stop_pod_windows",
        "_surviving_entries",
        "_systemctl_env",
        "_terminal_safe_detail",
        "_unproven_remedy",
        "_walk_band_for_free",
        "_write_and_load_unit",
        "active_names",
        "allocate_port",
        "annotations",
        "api_path",
        "atomic_write",
        "atomic_write_at",
        "attributed_descendants",
        "boot",
        "build_pod_env",
        "cgroup_procs_file",
        "cleanup_home",
        "contextlib",
        "dashboard_socket_name",
        "dataclass",
        "derive_port",
        "drain_cgroup",
        "embeddings_disabled",
        "exec_in_pod",
        "file_lock",
        "find_port_listeners",
        "get_peer_pid",
        "has_session_bus",
        "health",
        "http",
        "install_backend",
        "is_active",
        "is_link_or_junction",
        "is_scenario_ref",
        "json",
        "launchd",
        "listening_pid_tool_available",
        "loaded_teardown_hook",
        "loopback_owner_pids",
        "loopback_urlopen",
        "main_pid",
        "mint_token",
        "open_file_no_reparse",
        "open_lock_file",
        "operator_pinned",
        "orphan_homes",
        "os",
        "pin_checkout",
        "pin_directory",
        "pinned_fs",
        "platform_compat",
        "pod_api",
        "pod_context",
        "pod_home",
        "pod_name_mutex",
        "pod_plane_mutex",
        "pod_socket_path",
        "pod_unit",
        "port_owner",
        "probe_user_bus",
        "process_start_time",
        "prov",
        "re",
        "read_env_file",
        "recent_journal",
        "refusal_reason",
        "require_backend",
        "require_pod_safe_verb",
        "require_systemd",
        "resolve_checkout",
        "resolve_seed_scenario",
        "resolved_pod_home",
        "run_marker",
        "sanitized_seed_config",
        "seed_home_from_scenario",
        "seed_mod",
        "seeded_scenario_in_home",
        "session_bus_socket",
        "session_runtime_dir",
        "shutil",
        "socket",
        "start_pod",
        "stat",
        "stop_pod",
        "store_mappings",
        "subprocess",
        "sys",
        "systemctl",
        "systemctl_user_env",
        "target_supports_flag",
        "terminal_exit_code",
        "threading",
        "time",
        "unit_mod",
        "unit_state",
        "unix_socket_urlopen",
        "urllib",
        "user_bus_failure_message",
        "validate_name",
        "win_backend",
        "write_env_file",
        "write_pod_config",
    }
)

#: Names an owner may bind as well as the core: exception types and classes, which
#: are raised, caught and constructed but never patched through the runtime.
_SHARED_IMPORTS = frozenset(
    {"PodError", "PodOwnershipUnproven", "PodConfig", "Path", "annotations"}
)


def _owners() -> dict[str, ModuleType]:
    return {name: importlib.import_module(name) for name in rt._EXPORTS_BY_OWNER}


def test_every_surface_name_resolves_on_the_runtime() -> None:
    missing = sorted(name for name in _RUNTIME_SURFACE if not hasattr(rt, name))
    assert missing == []


def test_a_reexported_name_is_the_owners_object_and_absent_from_the_core() -> None:
    owners = _owners()
    assert set(rt._EXPORT_OWNERS.values()) == set(owners)
    for name, owner_name in rt._EXPORT_OWNERS.items():
        owner = owners[owner_name]
        assert name in vars(owner), f"{owner_name} does not define {name}"
        assert getattr(rt, name) is vars(owner)[name], name
        assert name not in vars(rt), f"{name} is bound in the core and would shadow its owner"
    assert set(rt._EXPORT_OWNERS) <= set(dir(rt))


def test_a_write_through_the_runtime_lands_on_the_owner_and_is_restored() -> None:
    from kiro_crew.pod import runtime_lifecycle, runtime_ports

    original = runtime_ports.derive_port
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(rt, "derive_port", _value(1))
        assert runtime_ports.derive_port(None, _POD) == 1
        assert "derive_port" not in vars(rt)
    assert runtime_ports.derive_port is original

    # mock.patch finds no local binding on the facade, so it restores by delattr
    # followed by setattr -- both of which must reach the owner.
    stop = runtime_lifecycle.stop_pod
    with mock.patch.object(rt, "stop_pod") as stub:
        assert runtime_lifecycle.stop_pod is stub
    assert runtime_lifecycle.stop_pod is stop
    assert "stop_pod" not in vars(rt)


def test_a_write_of_a_core_name_stays_on_the_core() -> None:
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(rt, "IS_WINDOWS", not rt.IS_WINDOWS)
        assert vars(rt)["IS_WINDOWS"] is not platform_compat.IS_WINDOWS


def test_an_owner_binds_no_name_another_module_owns() -> None:
    """A copy of a core seam, or of another owner's name, would miss every patch of
    ``rt.<name>``. Owners reach those names as ``runtime.<name>`` or
    ``<owner>.<name>`` and may bind only modules and never-patched imports."""
    core = vars(rt)
    offenders: list[str] = []
    for owner_name, owner in _owners().items():
        for name, value in vars(owner).items():
            if name.startswith("__") or isinstance(value, ModuleType) or name in _SHARED_IMPORTS:
                continue
            if name in core:
                offenders.append(f"{owner_name}.{name} copies the core binding")
            elif rt._EXPORT_OWNERS.get(name, owner_name) != owner_name:
                offenders.append(f"{owner_name}.{name} copies {rt._EXPORT_OWNERS[name]}'s binding")
    assert offenders == []


def _owner_sources() -> dict[str, ast.Module]:
    return {
        path.stem: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(_POD_DIR.glob("runtime_*.py"))
    }


def test_owners_read_the_core_only_for_names_the_core_defines() -> None:
    """``runtime.<name>`` must name a core binding. A re-exported name read through
    the facade would work, but it would hide which module the dependency is on."""
    trees = _owner_sources()
    assert set(trees) == {name.rsplit(".", 1)[1] for name in rt._EXPORTS_BY_OWNER}
    core = vars(rt)
    reached: list[str] = []
    for stem, tree in trees.items():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "runtime"
                and node.attr not in core
            ):
                reached.append(f"{stem}: runtime.{node.attr}")
    assert reached == []


def test_owners_import_only_submodules_and_exception_types_from_the_pod_package() -> None:
    allowed_from_core = {"PodError", "PodOwnershipUnproven"}
    offenders: list[str] = []
    for stem, tree in _owner_sources().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not (node.module or "").startswith(
                "kiro_crew.pod"
            ):
                continue
            for alias in node.names:
                if node.module == "kiro_crew.pod":
                    if not (_POD_DIR / f"{alias.name}.py").is_file():
                        offenders.append(f"{stem}: from kiro_crew.pod import {alias.name}")
                elif node.module == "kiro_crew.pod.runtime":
                    if alias.name not in allowed_from_core:
                        offenders.append(f"{stem}: from kiro_crew.pod.runtime import {alias.name}")
                elif node.module.startswith("kiro_crew.pod.runtime_"):
                    offenders.append(f"{stem}: from {node.module} import {alias.name}")
    assert offenders == []


def test_the_core_imports_no_owner_while_it_loads() -> None:
    """Every owner imports the core, so the core may reach an owner only lazily."""
    tree = ast.parse(Path(rt.__file__).read_text(encoding="utf-8"))
    loaded: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.If):
            continue  # the TYPE_CHECKING block, which never executes
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom) and "runtime_" in (sub.module or ""):
                loaded.append(sub.module or "")
            if isinstance(sub, ast.ImportFrom) and sub.module == "kiro_crew.pod":
                loaded.extend(a.name for a in sub.names if a.name.startswith("runtime_"))
    assert loaded == []


def test_the_type_checking_names_are_the_owners_reexports() -> None:
    tree = ast.parse(Path(rt.__file__).read_text(encoding="utf-8"))
    guarded = [
        n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "TYPE_CHECKING"
    ]
    assert len(guarded) == 1
    for node in ast.walk(guarded[0]):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert rt._EXPORT_OWNERS.get(alias.name) == node.module, alias.name


@pytest.mark.parametrize("name", ["time", "socket", "launchd", "pinned_fs", "unit_mod"])
def test_rebinding_a_shared_module_through_the_runtime_is_refused(name: str) -> None:
    """Each pod runtime module holds its own binding of a module it imports, so a
    replacement written here would reach one reader; the facade refuses it."""
    before = getattr(rt, name)
    with pytest.raises(AttributeError, match="shared module"):
        setattr(rt, name, object())
    with pytest.raises(AttributeError, match="shared module"):
        delattr(rt, name)
    assert getattr(rt, name) is before
    setattr(rt, name, before)  # re-binding the same object is a no-op, as undo does
    assert getattr(rt, name) is before
