"""``SandboxPolicy.credential_file_grants`` — the edition seam for granting a
credential FILE to an agent spawn.

Pins the contract the platform-context spec describes:

* the Default adapter grants nothing, and a companion that predates the method is
  treated as granting nothing;
* the resolver validates every entry and refuses the crew data home and the kiro
  trees whatever the edition asks for, failing closed (no grant) on any error;
* grants are Linux-namespace only and dropped on every other platform;
* the planner places each grant under the innermost mask that hides it, and the
  launcher stages it on a namespace-private tmpfs, never through the host-visible
  ``expose_files`` copy;
* ``wrap_argv`` / ``wrap_argv_async`` thread the grant to the namespace builder.
"""

from __future__ import annotations

import ctypes
import errno
import inspect
import os
import sys
from pathlib import Path
from typing import List

import pytest
from test_sandbox_launcher_program import CoveringLibc, launch
from test_sandbox_launcher_program import payload as launcher_payload

import kiro_crew.sandbox as sb
from kiro_crew import sandbox_launcher_program as program
from kiro_crew import sandbox_plan
from kiro_crew.platform.defaults import DefaultSandboxPolicy

_MS_RDONLY = 1
_MS_REMOUNT = 32

# The mechanism is the Linux namespace launcher; the macOS cases below only need a
# POSIX path layout, which Windows does not have.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="credential file grants are a POSIX mechanism"
)


class _GrantingPolicy:
    def __init__(self, grants: object) -> None:
        self._grants = grants

    def strict_dirs(self) -> List[str]:
        return list(sb._STRICT_DIRS)

    def cc_dirs(self) -> List[str]:
        return list(sb._CC_DIRS)

    def credential_file_grants(self) -> object:
        return self._grants


class _LegacyPolicy:
    """A companion written before ``credential_file_grants`` existed."""

    def strict_dirs(self) -> List[str]:
        return list(sb._STRICT_DIRS)

    def cc_dirs(self) -> List[str]:
        return list(sb._CC_DIRS)


class _RaisingPolicy(_LegacyPolicy):
    def credential_file_grants(self) -> List[str]:
        raise RuntimeError("grant store unreadable")


@pytest.fixture
def on_linux(monkeypatch):
    monkeypatch.setattr(sb.sys, "platform", "linux")


def _use_policy(monkeypatch, policy) -> None:
    monkeypatch.setattr(sb, "_sandbox_policy", lambda: policy)


def _home(*parts: str) -> str:
    return os.path.join(str(Path.home()), *parts)


# --- Default and legacy adapters grant nothing ---


def test_default_policy_grants_nothing():
    assert DefaultSandboxPolicy().credential_file_grants() == []


def test_default_policy_yields_no_grants(monkeypatch, on_linux):
    _use_policy(monkeypatch, DefaultSandboxPolicy())
    assert sb.credential_file_grants() == ()


def test_a_companion_without_the_method_grants_nothing(monkeypatch, on_linux):
    _use_policy(monkeypatch, _LegacyPolicy())
    assert sb.credential_file_grants() == ()


def test_an_adapter_failure_grants_nothing(monkeypatch, on_linux):
    _use_policy(monkeypatch, _RaisingPolicy())
    assert sb.credential_file_grants() == ()


# --- Validation ---


def test_a_valid_grant_resolves_under_home(monkeypatch, on_linux):
    _use_policy(monkeypatch, _GrantingPolicy([".docker/config.json"]))
    assert sb.credential_file_grants() == (_home(".docker", "config.json"),)


def test_duplicate_grants_collapse(monkeypatch, on_linux):
    _use_policy(monkeypatch, _GrantingPolicy([".docker/config.json", ".docker/config.json"]))
    assert sb.credential_file_grants() == (_home(".docker", "config.json"),)


@pytest.mark.parametrize(
    "entry",
    [
        "/etc/shadow",
        "~/.docker/config.json",
        ".docker/../.ssh/id_rsa",
        "../outside",
        ".docker//config.json",
        "./.docker/config.json",
        "",
        ".docker/config.json\x00",
    ],
)
def test_malformed_entries_are_dropped(monkeypatch, on_linux, entry):
    _use_policy(monkeypatch, _GrantingPolicy([entry, ".kube/config"]))
    assert sb.credential_file_grants() == (_home(".kube", "config"),)


@pytest.mark.parametrize(
    "entry",
    [
        ".kiro/crew/.vault/.vault_key",
        ".kiro/crew/security_policy.json",
        ".kiro/agents/kirocrew.json",
        ".kirocrew/.env",
    ],
)
def test_the_crew_home_and_kiro_trees_are_never_granted(monkeypatch, on_linux, entry):
    _use_policy(monkeypatch, _GrantingPolicy([entry]))
    assert sb.credential_file_grants() == ()


def test_a_relocated_crew_home_is_never_granted(monkeypatch, on_linux):
    relocated = Path.home() / "relocated-crew-home"
    monkeypatch.setattr(sb, "config_dir", lambda: relocated)
    _use_policy(monkeypatch, _GrantingPolicy(["relocated-crew-home/sel_hmac.key"]))
    assert sb.credential_file_grants() == ()


@pytest.mark.parametrize("raw", [".docker/config.json", b".docker/config.json", 42, None])
def test_a_non_sequence_answer_grants_nothing(monkeypatch, on_linux, raw):
    _use_policy(monkeypatch, _GrantingPolicy(raw))
    assert sb.credential_file_grants() == ()


def test_non_string_entries_are_dropped(monkeypatch, on_linux):
    _use_policy(monkeypatch, _GrantingPolicy([42, None, ".kube/config"]))
    assert sb.credential_file_grants() == (_home(".kube", "config"),)


# --- Platform gate ---


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_grants_are_linux_only(monkeypatch, platform):
    monkeypatch.setattr(sb.sys, "platform", platform)
    _use_policy(monkeypatch, _GrantingPolicy([".docker/config.json"]))
    assert sb.credential_file_grants() == ()


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_wrap_argv_drops_grants_off_linux(monkeypatch, platform):
    monkeypatch.setattr(sb.sys, "platform", platform)
    assert sb._secret_files_for_backend((_home(".docker", "config.json"),), "standard") == ()


def test_wrap_argv_drops_grants_when_the_sandbox_is_off(on_linux):
    assert sb._secret_files_for_backend((_home(".docker", "config.json"),), "off") == ()


def test_wrap_argv_keeps_grants_on_linux(on_linux):
    path = _home(".docker", "config.json")
    assert sb._secret_files_for_backend((path,), "standard") == (path,)


# --- Plan ---


def _plan(level: str = "standard", **kwargs):
    return sb._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, level, **kwargs)


def test_no_grant_leaves_the_plan_without_secret_files():
    assert sandbox_plan.namespace_payload(_plan())["secret_files"] == []


def test_a_grant_is_planned_as_a_secret_under_its_mask_not_an_expose_copy():
    """The grant must ride the private-tmpfs path, never ``expose_files``.

    ``expose_files`` writes its copy into the hidden parent's stand-in, a directory on a
    host-visible tmpfs, which is acceptable for ``.aws/config`` and not for a registry
    login.
    """
    path = _home(".docker", "config.json")
    payload = sandbox_plan.namespace_payload(_plan(extra_secret_files=(path,)))
    assert payload["secret_files"] == [[path, _home(".docker")]]
    assert payload["expose_files"] == []
    assert payload["secret_file_max_bytes"] == sandbox_plan.SECRET_FILE_MAX_BYTES


def test_a_path_in_both_lists_is_staged_only_as_a_secret():
    path = _home(".docker", "config.json")
    plan = _plan(extra_expose_files=(path,), extra_secret_files=(path,))
    assert plan.secrets == ((path, _home(".docker")),)
    assert all(source != path for source, _name in plan.expose)


def test_a_grant_outside_every_mask_is_not_restored():
    """Outside every mask the real file is already visible; a snapshot would only go stale."""
    assert _plan(extra_secret_files=(_home(".not-a-masked-file"),)).secrets == ()


def test_a_grant_takes_the_innermost_mask():
    roots = ("/k/outer", "/k/outer/inner")
    assert sandbox_plan.granted_secrets(["/k/outer/inner/f"], roots, ()) == (
        ("/k/outer/inner/f", "/k/outer/inner"),
    )


def test_a_grant_inside_a_private_window_is_not_restored():
    """A window is the host's REAL tree; the placeholder must never be created there."""
    assert sandbox_plan.granted_secrets(["/k/mask/win/f"], ("/k/mask",), ("/k/mask/win",)) == ()


@pytest.mark.parametrize("level", ["standard", "cc", "strict"])
def test_the_launcher_with_a_grant_is_valid_python(level):
    script = sb._build_launcher_script(level, extra_secret_files=(_home(".docker", "config.json"),))
    compile(script, "<launcher>", "exec")


# --- Launcher stages ---
#
# Driven in-process with the shared stand-in libc from ``test_sandbox_launcher_program``:
# a real bind needs a user namespace, which a nested sandbox cannot create. The libc
# accepts the private tmpfs without making one, so ``_stage_is_fresh_mount`` is patched to
# read the stage as freshly mounted, as the unreadable-mask tests do.

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)


class _GrantLibc(CoveringLibc):
    """A covering libc that accepts the private tmpfs, or refuses it when asked."""

    def __init__(self, *, refuse_tmpfs: bool = False) -> None:
        super().__init__()
        self.refuse_tmpfs = refuse_tmpfs
        self.tmpfs_options: list[object] = []

    def mount(self, source, target, fstype, flags, data):  # noqa: ANN001, ANN201
        if fstype == b"tmpfs":
            self.tmpfs_options.append(data)
        return super().mount(source, target, fstype, flags, data)

    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        if fstype == b"tmpfs":
            if self.refuse_tmpfs:
                ctypes.set_errno(errno.EPERM)
                return -1
            return 0
        return super().bound(source, target, fstype, flags)


@pytest.fixture
def _fresh_stage(monkeypatch):
    monkeypatch.setattr(program, "_stage_is_fresh_mount", lambda _dfd, _parent: True)


def _granted(tmp_path: Path, *, content: bytes = b'{"auths": {}}', refuse_tmpfs=False):
    """Mask a fake ``~/.docker`` holding *content* as ``config.json`` and grant that file."""
    docker = tmp_path / "home" / ".docker"
    docker.mkdir(parents=True)
    config = docker / "config.json"
    config.write_bytes(content)
    (docker / "other").write_text("stays hidden")
    libc = _GrantLibc(refuse_tmpfs=refuse_tmpfs)
    run = launch(
        tmp_path,
        launcher_payload(
            sensitive_dirs=[str(docker)],
            secret_files=[[str(config), str(docker)]],
            secret_file_max_bytes=sandbox_plan.SECRET_FILE_MAX_BYTES,
        ),
        libc=libc,
    )
    run.nondumpable = True
    return run, libc, docker, config


def _restore(run) -> None:
    program.preread_granted_files(run)
    program.mask_sensitive(run)
    program.restore_granted_files(run)


@linux_only
def test_a_granted_file_is_restored_read_only_from_a_private_stage(tmp_path, _fresh_stage):
    run, libc, docker, config = _granted(tmp_path)
    _restore(run)

    assert sorted(os.listdir(docker)) == ["config.json"]
    assert config.read_bytes() == b'{"auths": {}}'
    seal = [c for c in libc.calls if c.flags & _MS_REMOUNT and c.target == os.fsencode(config)]
    assert len(seal) == 1 and seal[0].flags & _MS_RDONLY
    assert libc.tmpfs_options[0].startswith(b"mode=0700,size=")
    assert len(libc.detached) == 1, "the private stage is retired after the bind"


@linux_only
def test_a_refused_private_tmpfs_leaves_the_file_absent(tmp_path, _fresh_stage, capfd):
    run, _libc, docker, _config = _granted(tmp_path, refuse_tmpfs=True)
    _restore(run)

    assert os.listdir(docker) == [], "no empty placeholder is left behind"
    assert "stays ABSENT" in capfd.readouterr().err


@linux_only
def test_a_dumpable_launcher_grants_nothing(tmp_path, _fresh_stage, capfd):
    run, libc, docker, _config = _granted(tmp_path)
    run.nondumpable = False
    _restore(run)

    assert os.listdir(docker) == []
    assert libc.tmpfs_options == []
    assert "non-dumpable" in capfd.readouterr().err


@linux_only
def test_an_oversized_grant_is_not_read(tmp_path, capfd):
    run, _libc, _docker, _config = _granted(
        tmp_path, content=b"x" * (sandbox_plan.SECRET_FILE_MAX_BYTES + 1)
    )
    program.preread_granted_files(run)
    assert run.secret_data == {}
    assert "exceeds" in capfd.readouterr().err


@linux_only
def test_a_link_at_the_granted_name_is_not_followed(tmp_path, capfd):
    run, _libc, _docker, config = _granted(tmp_path)
    decoy = tmp_path / "elsewhere"
    decoy.write_text("not the grant")
    config.unlink()
    config.symlink_to(decoy)
    program.preread_granted_files(run)
    assert run.secret_data == {}
    assert "cannot be read safely" in capfd.readouterr().err


def test_the_stages_run_in_order():
    """Read before any mask hides the file; bound while the launcher is non-dumpable."""
    run_child = inspect.getsource(program.run_child)
    assert run_child.index("preread_granted_files") < run_child.index("place_masks")
    place = inspect.getsource(program.place_masks)
    assert place.index("restore_granted_files") < place.index("mask_sensitive_files")


# --- Threading through wrap_argv / wrap_argv_async ---


@pytest.mark.skipif(sys.platform != "linux", reason="grants ride the Linux namespace launcher")
def test_wrap_argv_threads_grants_to_the_namespace_builder(monkeypatch):
    captured: dict = {}
    # Not nested: run from inside a KiroCrew sandbox, wrap_argv would skip the wrap.
    monkeypatch.setattr(sb, "_inside_kirocrew_sandbox", lambda: False)
    script = "launcher.py"
    stub = ["launcher", *sb._LAUNCHER_INTERPRETER_FLAGS, script, "/bin/true"]

    def _fake_namespace_argv(argv, level, **kwargs):
        captured.update(kwargs)
        return list(stub)

    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "namespace")
    monkeypatch.setattr(sb, "namespace_argv", _fake_namespace_argv)
    path = _home(".docker", "config.json")

    assert sb.wrap_argv(["/bin/true"], mode="standard", extra_secret_files=(path,)) == (
        stub,
        script,
    )
    assert captured.get("extra_secret_files") == (path,)


@pytest.mark.asyncio
async def test_wrap_argv_async_forwards_grants():
    captured: dict = {}

    def _prepare(argv, **options):
        captured.update(options)
        return list(argv), None

    path = _home(".docker", "config.json")
    await sb.wrap_argv_async(["/bin/true"], extra_secret_files=(path,), _prepare=_prepare)
    assert captured["extra_secret_files"] == (path,)


@pytest.mark.asyncio
async def test_wrap_argv_async_omits_an_empty_grant():
    captured: dict = {}

    def _prepare(argv, **options):
        captured.update(options)
        return list(argv), None

    await sb.wrap_argv_async(["/bin/true"], _prepare=_prepare)
    assert "extra_secret_files" not in captured
