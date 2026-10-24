"""A read-only seal holds even when its PARENT directory is renamed.

The sandbox seals a read-only path (the kiro agents tree with fork-governance
specs, the crew-home ceilings, the packaged install tree) so a confined agent
cannot rewrite what a later spawn trusts. Each backend seals the path itself --
Seatbelt with a path-based deny, the namespace launcher with a bind over the path
remounted read-only. Neither survives a rename of a PARENT directory: Seatbelt's
rule names a path the object has left, and the namespace bind moves with
the renamed parent (the kernel refuses ``rename(2)`` only of a directory that IS a
mountpoint, not of one that merely contains one). Either way the sealed name can
then refer to a different object, which the gateway reads next.

The fix: every ancestor of every read-only seal that the agent's uid could rename
(``readonly_ancestor_guards``). Seatbelt write-denies each one literally; the
namespace launcher binds each one over itself (``parent_pins``) so it is a
mountpoint and the rename fails with EBUSY. Each seal's chain is walked to the
filesystem root on its own and filtered by renameability -- no shared ceiling, so a
``KIRO_HOME`` relocated into an operator-owned folder keeps that folder pinned too.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import CoveringLibc, launch, payload

from kiro_crew import sandbox, sandbox_launcher_program, sandbox_seatbelt
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.sandbox import userns_available
from kiro_crew.sandbox_plan import (
    BACKEND_NAMESPACE,
    BACKEND_SEATBELT,
    SandboxRequest,
    namespace_payload,
    plan_confinement,
)

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")
_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the launcher stages pin with os.O_PATH and mount through /proc/self/fd",
)

_MS_BIND = 4096
_MS_REC = 16384

AGENTS = "/home/alice/.kiro/agents"


def _renameable_under(*roots: str):  # noqa: ANN202
    """A stand-in for ``_caller_can_rename``: a dir moves iff its parent is under a root."""

    def _check(path: str) -> bool:
        parent = os.path.dirname(os.path.normpath(path))
        return any(parent == r or parent.startswith(r + os.sep) for r in roots)

    return _check


def _host(
    monkeypatch: pytest.MonkeyPatch, backend: str, *, agents: list[str], install=()
):  # noqa: ANN001, ANN202
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/alice")))
    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: list(install))
    monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: list(agents))
    return sandbox._live_plan_host(SandboxRequest(tier="strict", backend=backend))


# --------------------------------------------------------------------------- #
# Which directories count as renameable.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_a_directory_in_a_parent_this_uid_owns_is_renameable_even_without_write(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    parent.chmod(stat.S_IRUSR | stat.S_IXUSR)  # owner may chmod u+w back at any time
    try:
        assert sandbox._caller_can_rename(str(child))
    finally:
        parent.chmod(stat.S_IRWXU)


@_POSIX_ONLY
@pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0, reason="root can rename anything"
)
def test_a_directory_in_a_root_owned_unwritable_parent_is_not_renameable() -> None:
    assert os.stat("/").st_uid != os.geteuid()
    assert not sandbox._caller_can_rename("/usr")


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("entry_uid", "expected"), [(4242, False), (1000, True)], ids=["foreign", "own"]
)
def test_a_sticky_parent_decides_by_the_entry_owner(
    monkeypatch: pytest.MonkeyPatch, entry_uid: int, expected: bool
) -> None:
    """A writable sticky ``/tmp`` lets this uid rename only entries it owns."""
    import kiro_crew.sandbox_workspace as workspace

    sticky_tmp = os.stat_result((stat.S_IFDIR | 0o1777, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    entry = os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 0, entry_uid, 0, 0, 0, 0, 0))
    monkeypatch.setattr(workspace.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(workspace.os, "stat", lambda p: sticky_tmp)
    monkeypatch.setattr(workspace.os, "lstat", lambda p: entry)
    monkeypatch.setattr(workspace.os, "access", lambda p, mode: True)
    assert sandbox._caller_can_rename("/tmp/crew-home") is expected


@_POSIX_ONLY
def test_the_walk_keeps_a_renameable_folder_above_a_fixed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # /data is the operator's; /data/locked sits in it but is root's. /data/locked/crew is
    # NOT renameable (its parent is locked), yet /data/locked IS (its parent is /data), and
    # renaming it frees the seal just the same -- so the walk must not stop at the first
    # fixed directory.
    monkeypatch.setattr(sandbox, "_caller_can_rename", lambda p: p == "/data/locked")
    assert sandbox._renameable_ancestor_guards(("/data/locked/crew",)) == ("/data/locked",)


# --------------------------------------------------------------------------- #
# Seatbelt: a literal write-deny on every renameable ancestor.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_seatbelt_denies_renaming_a_parent_of_the_sealed_agents_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "_caller_can_rename", _renameable_under("/home/alice"))
    host = _host(monkeypatch, BACKEND_SEATBELT, agents=[AGENTS])
    plan = plan_confinement(SandboxRequest(tier="strict", backend=BACKEND_SEATBELT), host)
    profile = sandbox_seatbelt.render_seatbelt_profile(plan)

    assert f'(deny file-write* (subpath "{AGENTS}"))' in profile
    assert '(deny file-write* (literal "/home/alice/.kiro"))' in profile
    # $HOME's parent is /home, which this uid cannot write, so $HOME cannot move and
    # nothing at or above it needs a rule from the seal walk.
    assert "/home/alice" not in host.readonly_ancestor_guards
    assert "/home" not in host.readonly_ancestor_guards
    # A literal guard pins the directory ENTRY only; siblings stay writable.
    assert '(deny file-write* (subpath "/home/alice/.kiro"))' not in profile


@_POSIX_ONLY
def test_seatbelt_keeps_the_install_tree_full_walk_and_adds_every_other_seal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "_caller_can_rename", _renameable_under("/home/alice"))
    install = "/opt/kirocrew/lib/python3.12/site-packages/kiro_crew"
    host = _host(monkeypatch, BACKEND_SEATBELT, agents=[AGENTS], install=[install])
    guards = set(host.readonly_ancestor_guards)
    assert {"/opt/kirocrew/lib/python3.12/site-packages", "/opt"} <= guards
    assert {"/home/alice/.kiro", "/home/alice/.kiro/crew", "/home/alice/.kiro/crew/apps"} <= guards


@_POSIX_ONLY
@pytest.mark.parametrize("backend", (BACKEND_SEATBELT, BACKEND_NAMESPACE))
def test_an_agents_tree_relocated_into_an_operator_folder_keeps_that_folder_pinned(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    # KIRO_HOME relocated to /private/tmp/agent-instance/kiro, the data home left at its
    # default. A ceiling shared across seals would stop at `kiro` and leave the
    # operator-owned `agent-instance` renameable: rename it aside, edit the specs, rename
    # it back. /private/tmp is sticky and world-writable, so whatever this uid owns in it
    # can move; /private/tmp itself cannot.
    agents = "/private/tmp/agent-instance/kiro/agents"
    monkeypatch.setattr(
        sandbox, "_caller_can_rename", _renameable_under("/home/alice", "/private/tmp")
    )
    host = _host(monkeypatch, backend, agents=[agents])
    guards = set(host.readonly_ancestor_guards)
    assert {"/private/tmp/agent-instance/kiro", "/private/tmp/agent-instance"} <= guards
    assert "/private/tmp" not in guards and "/private" not in guards
    plan = plan_confinement(SandboxRequest(tier="strict", backend=backend), host)
    if backend == BACKEND_SEATBELT:
        profile = sandbox_seatbelt.render_seatbelt_profile(plan)
        assert '(deny file-write* (literal "/private/tmp/agent-instance"))' in profile
    else:
        assert "/private/tmp/agent-instance" in namespace_payload(plan)["parent_pins"]


def _symlinked_agents_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, ...]:
    """``~/.kiro -> ~/agent-store/current``, the stow/chezmoi layout, with the agents tree
    sealed through the link spelling the gateway reads. Returns (home, store, current)."""
    home = (tmp_path / "alice").resolve()
    store = home / "agent-store"
    current = store / "current"
    (current / "agents").mkdir(parents=True)
    (home / ".kiro").symlink_to(current, target_is_directory=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(sandbox, "config_dir", lambda: elsewhere)
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: [])
    monkeypatch.setattr(
        sandbox, "_resolved_kiro_agents_targets", lambda: [str(home / ".kiro" / "agents")]
    )
    return home, store, current


@_POSIX_ONLY
@pytest.mark.parametrize("backend", (BACKEND_SEATBELT, BACKEND_NAMESPACE))
def test_a_symlinked_agents_home_still_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    # ``~/.kiro -> ~/agent-store/current`` (stow, chezmoi) plans on both backends with the
    # REAL renameability check, and the link entry itself gets no rule of its own.
    home, store, current = _symlinked_agents_home(tmp_path, monkeypatch)
    request = SandboxRequest(tier="strict", backend=backend)
    host = sandbox._live_plan_host(request)
    assert {str(current), str(store)} <= set(host.readonly_ancestor_guards)
    assert str(home / ".kiro") not in host.readonly_ancestor_guards
    plan_confinement(request, host)


@_POSIX_ONLY
@pytest.mark.parametrize("backend", (BACKEND_SEATBELT, BACKEND_NAMESPACE))
def test_an_install_tree_reached_through_a_venv_lib64_link_still_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    # Fedora/RHEL venvs import through ``<venv>/lib64 -> lib``, a link this uid owns. The
    # install tree's real directories are pinned through its canonical chain.
    home = (tmp_path / "alice").resolve()
    (home / ".kiro" / "agents").mkdir(parents=True)
    venv = home / "venv"
    package = venv / "lib" / "python3.12" / "site-packages" / "kiro_crew"
    package.mkdir(parents=True)
    (venv / "lib64").symlink_to("lib", target_is_directory=True)
    lexical = venv / "lib64" / "python3.12" / "site-packages" / "kiro_crew"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(sandbox, "config_dir", lambda: elsewhere)
    monkeypatch.setattr(
        sandbox, "_resolved_install_root_targets", lambda: [str(lexical), str(package)]
    )
    monkeypatch.setattr(
        sandbox, "_resolved_kiro_agents_targets", lambda: [str(home / ".kiro" / "agents")]
    )
    host = sandbox._live_plan_host(SandboxRequest(tier="strict", backend=backend))
    assert str(venv / "lib") in host.readonly_ancestor_guards


@_POSIX_ONLY
@pytest.mark.parametrize("backend", (BACKEND_SEATBELT, BACKEND_NAMESPACE))
def test_a_real_directory_agents_home_still_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    home = (tmp_path / "alice").resolve()
    (home / ".kiro" / "agents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(sandbox, "config_dir", lambda: elsewhere)
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: [])
    monkeypatch.setattr(
        sandbox, "_resolved_kiro_agents_targets", lambda: [str(home / ".kiro" / "agents")]
    )
    host = sandbox._live_plan_host(SandboxRequest(tier="strict", backend=backend))
    assert str(home / ".kiro") in host.readonly_ancestor_guards
    plan_confinement(SandboxRequest(tier="strict", backend=backend), host)


@_POSIX_ONLY
@pytest.mark.parametrize("backend", (BACKEND_SEATBELT, BACKEND_NAMESPACE))
def test_a_fixed_symlink_still_pins_the_directories_it_really_sits_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    # The seal really sits under ~/agent-store, which this uid can rename: renaming it
    # aside carries the seal away while the link keeps resolving to whatever takes its
    # place. So the canonical chain is guarded as well as the lexical one.
    home, store, current = _symlinked_agents_home(tmp_path, monkeypatch)
    link = str(home / ".kiro")
    renameable = _renameable_under(str(home))
    monkeypatch.setattr(sandbox, "_caller_can_rename", lambda p: p != link and renameable(p))
    host = sandbox._live_plan_host(SandboxRequest(tier="strict", backend=backend))
    guards = set(host.readonly_ancestor_guards)
    assert {str(current), str(store)} <= guards
    plan = plan_confinement(SandboxRequest(tier="strict", backend=backend), host)
    if backend == BACKEND_SEATBELT:
        profile = sandbox_seatbelt.render_seatbelt_profile(plan)
        assert f'(deny file-write* (literal "{store}"))' in profile
    else:
        assert str(store) in namespace_payload(plan)["parent_pins"]


@_POSIX_ONLY
def test_a_relocated_crew_seal_under_home_is_guarded_through_the_real_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A data home relocated under $HOME (e.g. ~/crew-data) resolves to spellings that
    # differ from the default; its `apps` sidecar parent must still be guarded. Uses the
    # REAL renameability check: every directory here is this uid's.
    home = tmp_path / "alice"
    relocated = home / "crew-data"
    relocated.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sandbox, "config_dir", lambda: relocated)
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: [])
    monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
    host = sandbox._live_plan_host(SandboxRequest(tier="strict", backend=BACKEND_SEATBELT))
    guards = set(host.readonly_ancestor_guards)
    assert {str(relocated / "apps"), str(relocated), str(home)} <= guards
    plan = plan_confinement(SandboxRequest(tier="strict", backend=BACKEND_SEATBELT), host)
    profile = sandbox_seatbelt.render_seatbelt_profile(plan)
    assert f'(deny file-write* (literal "{relocated / "apps"}"))' in profile


# --------------------------------------------------------------------------- #
# Namespace: the planner hands the launcher the pins, and the launcher binds them.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_namespace_plans_a_pin_for_every_renameable_parent_of_a_seal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The leaf self-bind does NOT stop `mv ~/.kiro ~/.kiro.moved`: the bind moves with
    # the parent. So the namespace backend pins the parents as well.
    monkeypatch.setattr(sandbox, "_caller_can_rename", _renameable_under("/home/alice"))
    host = _host(monkeypatch, BACKEND_NAMESPACE, agents=[AGENTS])
    assert "/home/alice/.kiro" in host.readonly_ancestor_guards
    plan = plan_confinement(SandboxRequest(tier="strict", backend=BACKEND_NAMESPACE), host)
    assert "/home/alice/.kiro" in plan.parent_pins
    assert namespace_payload(plan)["parent_pins"] == list(plan.parent_pins)


@_LINUX_ONLY
def test_the_launcher_binds_each_pin_over_itself_recursively_before_the_seal(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    pins = [str(home / ".kiro"), str(home), str(home / "absent")]
    libc = CoveringLibc()
    run = launch(tmp_path, payload(parent_pins=pins, readonly_dirs=[str(agents)]), libc=libc)
    sandbox_launcher_program.place_masks(run)

    def _id(path: Path) -> tuple[int, int]:
        info = os.stat(path)
        return (info.st_dev, info.st_ino)

    recursive_self_binds = [
        c.target_id
        for c in libc.calls
        if c.flags == _MS_BIND | _MS_REC and c.source_id == c.target_id
    ]
    # Shallowest first, the absent name skipped.
    assert recursive_self_binds == [_id(home), _id(home / ".kiro")]
    seal_index = next(
        i for i, c in enumerate(libc.calls) if c.flags == _MS_BIND and c.target_id == _id(agents)
    )
    assert seal_index > max(
        i for i, c in enumerate(libc.calls) if c.flags == _MS_BIND | _MS_REC
    ), "the parents must be pinned before the seal beneath them"


@_LINUX_ONLY
def test_a_failed_pin_refuses_the_spawn(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    run = launch(tmp_path, payload(parent_pins=[str(home)]), libc=CoveringLibc(fail_at=1))
    with pytest.raises(SystemExit, match="pinning seal parent"):
        sandbox_launcher_program.pin_seal_parents(run)


@_LINUX_ONLY
def test_the_launcher_reenters_its_working_directory_by_name_after_the_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The inherited cwd references the mount beneath the pins, so
    # a relative lookup from it does not meet the seals placed on top. run_child records the
    # cwd before place_masks and re-enters it by name after.
    order: list[str] = []
    for stage in (
        "pick_stand_in_root",
        "check_crew_home_aliases",
        "preread_exposed_files",
        "confirm_crew_home_aliases",
        "scrub_env",
        "drop_privileges",
        "install_seccomp",
        "refuse_hardlinked_credentials",
        "enter_exec_cwd",
    ):
        monkeypatch.setattr(sandbox_launcher_program, stage, lambda *_a, _n=stage: order.append(_n))
    monkeypatch.setattr(
        sandbox_launcher_program, "place_masks", lambda *_a: order.append("place_masks")
    )
    monkeypatch.setattr(
        sandbox_launcher_program, "exec_agent", lambda *_a: order.append("exec_agent")
    )
    monkeypatch.chdir(tmp_path)
    entered: list[str] = []
    real_chdir = os.chdir
    monkeypatch.setattr(
        sandbox_launcher_program.os,
        "chdir",
        lambda p: (order.append("chdir"), entered.append(p), real_chdir(p)),
    )
    sandbox_launcher_program.run_child(object(), ["agent"])
    assert entered == [str(tmp_path)]
    assert order.index("place_masks") < order.index("chdir") < order.index("exec_agent")


@_LINUX_ONLY
def test_a_launch_directory_that_cannot_be_reentered_refuses_the_spawn(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="cannot re-enter the launch directory"):
        sandbox_launcher_program.reenter_launch_cwd(object(), str(tmp_path / "gone"))


@_LINUX_ONLY
def test_an_unresolvable_launch_directory_refuses_before_any_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A launch directory deleted under a running gateway makes getcwd fail. With no name
    # to re-enter, the agent would start on the mount beneath the seals, so the launcher
    # refuses before it places anything.
    placed: list[str] = []
    for stage in ("pick_stand_in_root", "check_crew_home_aliases", "preread_exposed_files"):
        monkeypatch.setattr(sandbox_launcher_program, stage, lambda *_a: None)
    monkeypatch.setattr(sandbox_launcher_program, "place_masks", lambda *_a: placed.append("m"))
    monkeypatch.setattr(sandbox_launcher_program, "exec_agent", lambda *_a: placed.append("x"))

    def _gone() -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(sandbox_launcher_program.os, "getcwd", _gone)
    with pytest.raises(SystemExit, match="cannot resolve the launch directory"):
        sandbox_launcher_program.run_child(object(), ["agent"])
    assert placed == []


@_LINUX_ONLY
def test_an_empty_launch_directory_name_refuses_the_spawn() -> None:
    with pytest.raises(SystemExit, match="cannot re-enter the launch directory"):
        sandbox_launcher_program.reenter_launch_cwd(object(), "")


# --------------------------------------------------------------------------- #
# Namespace, LIVE: a child under the real sandbox cannot rename the sealed tree's
# parent, and its write never reaches the host file.
# --------------------------------------------------------------------------- #

_CHILD = r"""
import errno, json, os, sys

home, agents, spec, control, relative_ceiling = sys.argv[1:6]
replacement = '{"replacement": true}'

def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}

def write(path):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as fh:
        fh.write(replacement)
    os.replace(temp, path)
    return True

def rename_parent():
    os.rename(agents_parent, agents_parent.rstrip("/") + ".moved")
    return True

agents_parent = os.path.dirname(agents.rstrip("/"))
report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "direct_write": attempt(lambda: write(spec)),
    "rename_parent": attempt(rename_parent),
    "control": attempt(lambda: write(control)),
    # From the inherited working directory, by a RELATIVE path: a lookup that starts
    # at the cwd must meet the same seals an absolute one does.
    "relative_ceiling": attempt(lambda: write(relative_ceiling)),
}
print("REPORT " + json.dumps(report), flush=True)
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


@pytest.mark.parametrize("tier", ("standard", "cc", "strict"), ids=lambda t: t)
@pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only")
@pytest.mark.skipif(
    not userns_available(),
    reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
    "namespaces)",
)
def test_a_sandboxed_agent_cannot_rename_the_agents_parent(home, tier):
    agents = Path(str(kiro_agents_dir()))
    agents.mkdir(parents=True)
    real = agents / "real.json"
    real.write_text('{"real": true}', encoding="utf-8")
    saved = real.read_bytes()
    spec = agents / "replacement.json"
    control = home / "control.json"

    # A sealed crew leaf the platform does not parse at boot: ``security_policy.json``
    # would be loaded as the governance ceiling, and a stub there fails composition.
    ceiling = home / "registry_trust.json"
    ceiling.write_text("{}", encoding="utf-8")
    ceiling_saved = ceiling.read_bytes()
    launch_dir = home / "workspace"
    launch_dir.mkdir(exist_ok=True)
    argv = [
        sys.executable,
        "-c",
        _CHILD,
        str(home),
        str(agents),
        str(spec),
        str(control),
        os.path.join("..", "registry_trust.json"),
    ]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.run(
        sandbox.namespace_argv(argv, tier),
        cwd=str(launch_dir),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])

    assert report["marker"] == "1", out
    assert report["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), out
    assert report["control"] == {"ok": True, "value": True}, report
    assert report["direct_write"]["ok"] is False, report
    assert report["direct_write"]["errno"] in {"EROFS", "EACCES"}, report
    # The parent is pinned as a mountpoint of its own, so the rename is refused and the
    # sealed name is never freed.
    assert report["rename_parent"]["ok"] is False, report
    assert report["rename_parent"]["errno"] == "EBUSY", report
    assert real.read_bytes() == saved
    assert not spec.exists()
    # The launch directory sits under a pinned parent; the agent must stand on the pins,
    # so the relative write reaches the sealed ceiling and is refused. The child stages
    # its temp file in the writable home and renames it over the sealed file, which is
    # a bind mountpoint, so the refusal is EBUSY; from the mount beneath the pins that
    # rename would land and the assertion below on the host bytes would fail.
    assert report["relative_ceiling"]["ok"] is False, report
    assert report["relative_ceiling"]["errno"] in {"EROFS", "EACCES", "EBUSY"}, report
    assert ceiling.read_bytes() == ceiling_saved
