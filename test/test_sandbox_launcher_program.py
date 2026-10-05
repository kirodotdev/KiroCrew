"""The namespace launcher program, driven through its stages with a stand-in libc.

``kiro_crew.sandbox_launcher_program`` is the program a sandboxed spawn runs first on
Linux. This test process cannot create a user namespace (a nested ``unshare`` is
seccomp-denied inside an agent sandbox), so the stages run in-process against real
files under ``tmp_path`` with :class:`CoveringLibc` in place of libc: a bind of a fresh
stand-in over a name moves the name's object aside and renames the stand-in onto it, on
the same filesystem, so the name then reports the stand-in's device and inode exactly as
``stat`` reports a real mount point. Every other check the launcher makes -- the
no-follow pins, the post-mount read-back, the carried identities -- runs for real.

The helpers here (:func:`payload`, :class:`RecordingLibc`, :class:`CoveringLibc`,
:func:`launch`) are shared with the other launcher suites, which import them.
"""

from __future__ import annotations

import ast
import ctypes
import errno
import importlib.util
import os
import stat
import sys
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import sandbox_launcher, sandbox_launcher_program, sandbox_plan

try:
    import fcntl
except ImportError:  # Windows: the launcher never runs there, and the suites skip
    fcntl = None  # type: ignore[assignment]

program = sandbox_launcher_program

#: The stages pin targets through ``O_PATH`` and address them as ``/proc/self/fd/<n>``,
#: which only Linux has; the namespace launcher runs nowhere else.
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)

_MS_BIND = 4096
_MS_REMOUNT = 32


def payload(**overrides: Any) -> dict[str, Any]:
    """A launcher payload with nothing to mask, plus *overrides*.

    The same keys :func:`kiro_crew.sandbox_plan.namespace_payload` emits, so a test
    states only the lists its case is about.
    """
    base: dict[str, Any] = {
        "real_uid": os.getuid(),
        "real_gid": os.getgid(),
        "sensitive_dirs": [],
        "sensitive_dir_ids": {},
        "private_dirs": [],
        "private_dir_ids": {},
        "readonly_dirs": [],
        "writable_dirs": [],
        "sensitive_files": [],
        "fail_closed_file_masks": [],
        "alias_credential_ids": [],
        "required_mask_targets": [],
        "mask_occupants": {},
        "crew_home_aliases": [],
        "expose_files": [],
        "env_prefixes": [],
        "ssh_dir": "/nonexistent-ssh-dir",
        "ssh_known_hosts": "/nonexistent-ssh-dir/known_hosts",
        "hide_ssh": 0,
        "sandbox_level": "strict",
        "unreadable_masks": [],
        "strict_host_key_opt": "",
        "stand_in_roots": [],
    }
    unknown = set(overrides) - set(base)
    assert not unknown, f"not a payload key: {sorted(unknown)}"
    base.update(overrides)
    return base


def rendered_payload(script: str) -> dict[str, Any]:
    """The plan data a rendered launcher carries: its one ``_PLAN = {...}`` line.

    For a test that reads back the file ``namespace_argv`` wrote; a test that builds the
    plan itself reads :class:`~kiro_crew.sandbox_plan.ConfinementPlan` fields instead.
    """
    lines = [line for line in script.splitlines() if line.startswith("_PLAN = {")]
    assert len(lines) == 1, "a rendered launcher carries exactly one plan line"
    return ast.literal_eval(lines[0][len("_PLAN = ") :])


class Call:
    """One ``mount(2)`` a stage made, with what its source and target reached then."""

    def __init__(self, source: object, target: object, fstype: object, flags: int) -> None:
        self.source = source
        self.target = target
        self.fstype = fstype
        self.flags = flags
        self.source_id = identity(source)
        self.target_id = identity(target)
        #: The path the target named when the mount was made.
        self.target_path = _resolved(target) if target is not None else None


_FD_PREFIX = "/proc/self/fd/"


def identity(path: object) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of what *path* names right now, or ``None``.

    A ``/proc/self/fd/<n>`` spelling is resolved through its DESCRIPTOR, which is open
    while the stage calls ``mount``: ``fstat`` reaches the same object on every POSIX
    host, while the spelling itself resolves only where procfs exists.
    """
    if path is None:
        return None
    spelling = os.fsdecode(path) if isinstance(path, bytes) else str(path)
    try:
        if spelling.startswith(_FD_PREFIX):
            info = os.fstat(int(spelling[len(_FD_PREFIX) :]))
        else:
            info = os.stat(spelling)
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino)


class RecordingLibc:
    """A libc that records every call and succeeds, or fails one chosen mount.

    *fail_at* is 1-based over ``mount`` calls; the failing call sets *fail_errno*.
    *prctl* may be set to ``None`` to stand in for a libc without prctl(2).
    """

    def __init__(self, fail_at: int | None = None, fail_errno: int = errno.EPERM) -> None:
        self.calls: list[Call] = []
        self.detached: list[str] = []
        self.unshared: list[int] = []
        self.prctls: list[tuple[int, ...]] = []
        self.fail_at = fail_at
        self.fail_errno = fail_errno

    def mount(self, source, target, fstype, flags, data):  # noqa: ANN001, ANN201
        self.calls.append(Call(source, target, fstype, flags))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            ctypes.set_errno(self.fail_errno)
            return -1
        return self.bound(source, target, fstype, flags)

    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        """What a successful mount does to the filesystem: nothing, here."""
        return 0

    def umount2(self, target, flags):  # noqa: ANN001, ANN201
        self.detached.append(os.fsdecode(target))
        return 0

    def unshare(self, flags):  # noqa: ANN001, ANN201
        self.unshared.append(flags)
        return 0

    def prctl(self, option, a2, a3, a4, a5):  # noqa: ANN001, ANN201
        self.prctls.append((option, a2, a3, a4, a5))
        return 0


def _resolved(path: object) -> str:
    """The path *path* names; a ``/proc/self/fd/<n>`` spelling is read off its descriptor."""
    spelling = os.fsdecode(path) if isinstance(path, bytes) else str(path)
    if not spelling.startswith(_FD_PREFIX):
        return spelling
    fd = int(spelling[len(_FD_PREFIX) :])
    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is not None:  # Darwin: no procfs, but the descriptor knows its path
        return os.fsdecode(fcntl.fcntl(fd, getpath, bytes(1024)).split(b"\0", 1)[0])
    return os.readlink(spelling)


class CoveringLibc(RecordingLibc):
    """A libc whose binds HIDE their target, as a real mount does.

    A bind of a fresh stand-in over a name moves the object at that name aside and
    renames the stand-in onto it, on the same filesystem, so the name takes the
    stand-in's identity and everything beneath it is gone from every later look. A
    window's staging bind records the window; binding the stage back at the window's
    path inside a stand-in moves the window's real tree there. A bind of a path over
    itself (a seal, a carve-out) and every remount change nothing a ``stat`` can see.
    A private tmpfs is refused, so the unreadable-mask fallback is what runs.
    """

    def __init__(self, fail_at: int | None = None, fail_errno: int = errno.EPERM) -> None:
        super().__init__(fail_at, fail_errno)
        self.covered: list[str] = []
        self.aside: dict[str, str] = {}
        self.stages: dict[str, str] = {}
        #: Where a masked object is moved to, outside every tree a test inspects.
        self.aside_root: str | None = None

    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        if fstype == b"tmpfs":
            ctypes.set_errno(errno.ENOSYS)
            return -1
        if source is None or not flags & _MS_BIND or flags & _MS_REMOUNT:
            return 0
        src, tgt = _resolved(source), _resolved(target)
        if os.path.abspath(src) == os.path.abspath(tgt):
            return 0
        if src in self.stages:
            os.rename(self._current(self.stages.pop(src)), tgt)
            return 0
        if os.path.basename(tgt).startswith("kirocrew_sb_"):
            self.stages[tgt] = src  # a window staged: remember where its tree lives
            return 0
        assert self.aside_root is not None, "launch() sets the aside root"
        moved = os.path.join(self.aside_root, str(len(self.covered)))
        os.rename(tgt, moved)
        os.rename(src, tgt)
        self.aside[tgt] = moved
        self.covered.append(tgt)
        return 0

    def _current(self, path: str) -> str:
        """Where the object first seen at *path* lives now that masks moved its parents."""
        for original, moved in sorted(self.aside.items(), key=lambda kv: -len(kv[0])):
            if path == original or path.startswith(original + "/"):
                return moved + path[len(original) :]
        return path


def launch(
    tmp_path: Path,
    plan: dict[str, Any] | None = None,
    *,
    libc: RecordingLibc | None = None,
    environ: dict[str, str] | None = None,
) -> program.Launch:
    """A :class:`~kiro_crew.sandbox_launcher_program.Launch` whose stand-ins live in *tmp_path*.

    The stand-in root is set directly rather than chosen among ``/run/user`` and
    ``/dev/shm``, so every bind source shares a filesystem with the targets and
    :class:`CoveringLibc` can rename it into place.
    """
    stand_ins = tmp_path / "stand-ins"
    stand_ins.mkdir(exist_ok=True)
    libc = libc or CoveringLibc()
    if isinstance(libc, CoveringLibc) and libc.aside_root is None:
        aside = tmp_path / "under-masks"
        aside.mkdir(exist_ok=True)
        libc.aside_root = str(aside)
    execs: list[list[str]] = []
    run = program.Launch(
        plan or payload(),
        libc,
        environ={} if environ is None else environ,
        execvp=lambda file, args: execs.append([file, *args]),
    )
    run.tmpfs_src = str(stand_ins)
    run.src_prefix = "kirocrew_sb_%d_" % os.getpid()
    run.execs = execs  # type: ignore[attr-defined]
    return run


def refusal(stage, *args: object) -> str | None:  # noqa: ANN001
    """Run *stage* and return its refusal message, or ``None`` when it did not refuse."""
    try:
        stage(*args)
    except SystemExit as exc:
        return str(exc.code)
    return None


class _Home:
    """A credential tree: a key dir, a policy cache, a ceiling, a secret file, ~/.ssh."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "home"
        self.aws = self.root / ".aws"
        self.aws.mkdir(parents=True)
        (self.aws / "credentials").write_text("[default]\n")
        (self.aws / "config").write_text("[profile x]\n")
        self.crew = self.root / ".kiro" / "crew"
        self.cache = self.crew / "policy_cache"
        self.cache.mkdir(parents=True)
        (self.cache / "meta.json").write_text("{}")
        self.ceiling = self.crew / "security_policy.json"
        self.ceiling.write_text("{}")
        self.run = self.crew / "run"
        self.probe = self.run / "mcp-tmp" / "probe"
        self.probe.mkdir(parents=True)
        self.apps = self.crew / "apps"
        self.window = self.apps / "alpha" / "data"
        self.window.mkdir(parents=True)
        (self.window / "state.db").write_text("db")
        (self.apps / "alpha" / ".app_secret").write_text("secret")
        self.nested = self.window / "edits"
        self.nested.mkdir()
        (self.nested / "draft").write_text("draft")
        self.secret = self.root / ".netrc"
        self.secret.write_text("machine x\n")
        self.signing_key = self.crew / "token_signing.key"
        self.signing_key.write_bytes(b"k" * 32)
        self.ssh = self.root / ".ssh"
        self.ssh.mkdir()
        (self.ssh / "id_rsa").write_text("key")
        (self.ssh / "known_hosts").write_text("host ssh-ed25519 AAAA\n")

    def plan(self, **overrides: Any) -> dict[str, Any]:
        """The payload the planner would hand a strict spawn over this tree."""
        fields: dict[str, Any] = {
            "sensitive_dirs": [str(self.aws), str(self.cache), str(self.apps), str(self.nested)],
            "readonly_dirs": [str(self.run), str(self.ceiling)],
            "writable_dirs": [str(self.probe)],
            "sensitive_files": [str(self.secret), str(self.signing_key)],
            "private_dirs": [str(self.window)],
            "expose_files": [[str(self.aws / "config"), "config"]],
            "unreadable_masks": ["token_signing.key"],
            "ssh_dir": str(self.ssh),
            "ssh_known_hosts": str(self.ssh / "known_hosts"),
            "hide_ssh": 1,
            "env_prefixes": ["AWS_SECRET", "SLACK_BOT_TOKEN"],
            "strict_host_key_opt": " -o StrictHostKeyChecking=accept-new",
        }
        fields.update(overrides)
        return payload(**fields)


# --------------------------------------------------------------------------- #
# The rendered program is this module, plus the plan.
# --------------------------------------------------------------------------- #


def _plan(tmp_path: Path) -> sandbox_plan.ConfinementPlan:
    host = sandbox_plan.PlanHost(home=str(tmp_path), tier_dirs=(".aws",), uid=11, gid=12)
    return sandbox_plan.plan_confinement(sandbox_plan.SandboxRequest(tier="strict"), host)


def test_the_rendered_program_is_the_module_with_one_substitution(tmp_path: Path) -> None:
    source = Path(program.__file__).read_text(encoding="utf-8")
    rendered = sandbox_launcher.render_namespace_launcher(_plan(tmp_path))
    placeholder = sandbox_launcher.PLAN_PLACEHOLDER
    assert source.count(placeholder) == 1
    head, tail = source.split(placeholder)
    assert rendered.startswith(head) and rendered.endswith(tail)
    line = rendered[len(head) : len(rendered) - len(tail)]
    assert line.startswith("_PLAN = {") and line.endswith("}\n") and line.count("\n") == 1


def test_the_rendered_plan_line_is_the_payload(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    rendered = sandbox_launcher.render_namespace_launcher(plan)
    line = next(ln for ln in rendered.splitlines() if ln.startswith("_PLAN = {"))
    assert ast.literal_eval(line[len("_PLAN = ") :]) == sandbox_plan.namespace_payload(plan)


def _renderer_loaded_over(package: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A private copy of the renderer module, imported while *package* is the package dir."""
    files = resources.files
    monkeypatch.setattr(
        resources, "files", lambda name: package if name == "kiro_crew" else files(name)
    )
    spec = importlib.util.spec_from_file_location("renderer_under_test", sandbox_launcher.__file__)
    assert spec is not None and spec.loader is not None
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    return renderer


def test_the_program_is_the_one_on_disk_when_the_renderer_was_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A package replaced under a running gateway must not pair the new program with the
    plan its already-imported code computes: the renderer keeps the text it loaded with."""
    package = tmp_path / "kiro_crew"
    package.mkdir()
    on_disk = package / "sandbox_launcher_program.py"
    # A sentinel only this copy carries, so the render shows which file it was read from.
    source = Path(program.__file__).read_text(encoding="utf-8") + "IMPORTED_COPY = 1\n"
    on_disk.write_text(source, encoding="utf-8")
    renderer = _renderer_loaded_over(package, monkeypatch)
    on_disk.write_text(on_disk.read_text(encoding="utf-8") + "UPGRADED = 1\n", encoding="utf-8")
    plan = _plan(tmp_path)
    rendered = renderer.render_namespace_launcher(plan)
    assert "IMPORTED_COPY = 1\n" in rendered and "UPGRADED" not in rendered
    assert rendered.replace(
        "IMPORTED_COPY = 1\n", ""
    ) == sandbox_launcher.render_namespace_launcher(plan)


def test_a_renderer_loaded_without_its_program_imports_and_refuses_every_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A package assembled without the program still imports: the spawn refuses instead."""
    package = tmp_path / "kiro_crew"
    package.mkdir()
    renderer = _renderer_loaded_over(package, monkeypatch)
    with pytest.raises(RuntimeError, match="could not be read"):
        renderer.render_namespace_launcher(_plan(tmp_path))


@pytest.mark.parametrize("placeholders", [0, 2])
def test_a_program_without_exactly_one_plan_placeholder_is_not_rendered(
    placeholders: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan has one place to go; a program with none or two refuses every spawn."""
    from kiro_crew import sandbox

    body = (
        Path(program.__file__)
        .read_text(encoding="utf-8")
        .replace(sandbox_launcher.PLAN_PLACEHOLDER, "")
    )
    monkeypatch.setattr(
        sandbox_launcher, "_PROGRAM_SOURCE", body + sandbox_launcher.PLAN_PLACEHOLDER * placeholders
    )
    with pytest.raises(RuntimeError, match="exactly one plan placeholder"):
        sandbox_launcher.render_namespace_launcher(_plan(tmp_path))
    with pytest.raises(RuntimeError, match="exactly one plan placeholder"):
        sandbox._build_launcher_script("strict")


class _ProcWrites:
    """``open`` for the parent's ``/proc/<pid>/...`` writes: records each file's text."""

    def __init__(self) -> None:
        self.written: list[tuple[str, str]] = []

    def __call__(self, path: str, mode: str = "r") -> _ProcWrites:
        assert mode == "w", (path, mode)
        self.written.append((path, ""))
        return self

    def __enter__(self) -> _ProcWrites:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def write(self, text: str) -> int:
        path, before = self.written[-1]
        self.written[-1] = (path, before + text)
        return len(text)


_CHILD_PID = 99_999_999_999


@pytest.mark.parametrize(
    ("status", "code"),
    [(7 << 8, 7), (0, 0), (9, 1)],
    ids=["child-exits-7", "child-exits-0", "child-killed-by-signal"],
)
def test_the_parent_maps_the_child_to_its_own_ids_and_exits_with_its_code(
    status: int, code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent half of ``main``: deny setgroups, map the caller's own uid and gid onto
    themselves and nothing else, release the child, and exit with the child's code."""
    proc = _ProcWrites()
    real_pipe = os.pipe
    child_ends: list[int] = []

    def _pipe() -> tuple[int, int]:
        read_end, write_end = real_pipe()
        if not child_ends:
            os.write(write_end, b"x")  # the child's "unshare done", already sent
        # The end the child keeps, which the parent closes on its own side.
        child_ends.append(os.dup(read_end))
        return read_end, write_end

    reaped: list[int] = []

    def _waitpid(pid: int, _options: int) -> tuple[int, int]:
        reaped.append(pid)
        return pid, status

    monkeypatch.setenv("KIROCREW_HOST_PID", "0")
    monkeypatch.setattr(program.os, "pipe", _pipe)
    monkeypatch.setattr(program.os, "fork", lambda: _CHILD_PID)
    monkeypatch.setattr(program.os, "waitpid", _waitpid)
    monkeypatch.setattr(program, "open", proc, raising=False)
    try:
        with pytest.raises(SystemExit) as exited:
            program.main(payload(real_uid=1234, real_gid=5678), libc=RecordingLibc(), argv=["/a"])
        released = os.read(child_ends[1], 1)
    finally:
        for fd in child_ends:
            os.close(fd)
    assert exited.value.code == code
    assert proc.written == [
        (f"/proc/{_CHILD_PID}/setgroups", "deny"),
        (f"/proc/{_CHILD_PID}/uid_map", "1234 1234 1\n"),
        (f"/proc/{_CHILD_PID}/gid_map", "5678 5678 1\n"),
    ]
    assert reaped == [_CHILD_PID]
    assert released == b"x"  # the child was told the maps are written


# --------------------------------------------------------------------------- #
# A whole child run.
# --------------------------------------------------------------------------- #


def _ran(tmp_path: Path, **overrides: Any) -> tuple[program.Launch, _Home, CoveringLibc]:
    home = _Home(tmp_path)
    libc = CoveringLibc()
    environ = {
        "HOME": str(home.root),
        "AWS_SECRET_ACCESS_KEY": "x",
        "SLACK_BOT_TOKEN": "y",
        "KEEP_ME": "1",
        "GRADLE_OPTS": "-Dorg.gradle.daemon=true",
    }
    run = launch(tmp_path, home.plan(**overrides), libc=libc, environ=environ)
    program.run_child(run, ["/bin/agent", "--flag"])
    return run, home, libc


def _listing(path: Path) -> list[str]:
    return sorted(os.listdir(path))


def test_a_child_run_masks_seals_carves_and_execs(tmp_path: Path) -> None:
    run, home, libc = _ran(tmp_path)

    # Credential dirs show their empty stand-in; the cc expose copy is restored read-only.
    assert _listing(home.aws) == ["config"]
    assert (home.aws / "config").read_text() == "[profile x]\n"
    assert stat.S_IMODE(os.stat(home.aws / "config").st_mode) == 0o444
    assert _listing(home.cache) == []
    # The masked apps tree keeps only the window, bound back on its real inode, and the
    # masked leaf nested inside the window is hidden again after the bind.
    assert _listing(home.apps) == ["alpha"]
    assert _listing(home.apps / "alpha") == ["data"]
    assert _listing(home.window) == ["edits", "state.db"]
    assert _listing(home.nested) == []
    # Single files are empty stand-ins; ~/.ssh keeps only the known_hosts copy.
    assert home.secret.read_text() == ""
    assert home.signing_key.read_bytes() == b""
    assert _listing(home.ssh) == ["known_hosts"]
    assert (home.ssh / "known_hosts").read_text() == "host ssh-ed25519 AAAA\n"
    # Every window stage was retired.
    assert run.private_stage == {} and len(libc.detached) == 1
    # The environment the agent inherits.
    assert run.environ["KEEP_ME"] == "1"
    assert "AWS_SECRET_ACCESS_KEY" not in run.environ and "SLACK_BOT_TOKEN" not in run.environ
    assert run.environ["KIROCREW_SANDBOX_ACTIVE"] == "1"
    assert run.environ["KIROCREW_SANDBOX_LEVEL"] == "strict"
    assert run.environ["GIT_SSH_COMMAND"] == (
        "ssh -F /dev/null -o IdentityFile=~/.ssh/id_rsa -o IdentityFile=~/.ssh/id_ecdsa"
        " -o IdentityFile=~/.ssh/id_ed25519 -o UserKnownHostsFile=~/.ssh/known_hosts"
        " -o StrictHostKeyChecking=accept-new"
    )
    assert run.environ["GRADLE_OPTS"] == "-Dorg.gradle.daemon=true -Dorg.gradle.daemon=false"
    # Confined, then exec'd.
    assert [p[0] for p in libc.prctls].count(24) == 64  # every capability dropped
    assert (38, 1, 0, 0, 0) in libc.prctls  # NO_NEW_PRIVS
    assert libc.prctls[-1][0] == 22  # the seccomp filter, last
    assert run.execs == [["/bin/agent", "/bin/agent", "--flag"]]


def test_seals_land_before_hides_and_carve_outs_after_them(tmp_path: Path) -> None:
    """A non-recursive bind of a parent placed AFTER a hide of its leaf masks that hide,
    and a carve-out bound before its parent's seal vanishes under it."""
    _, home, libc = _ran(tmp_path)
    targets = [call.target_path for call in libc.calls if call.flags & _MS_BIND]
    seal = targets.index(str(home.run))
    hide = targets.index(str(home.aws))
    carve = max(i for i, t in enumerate(targets) if t == str(home.probe))
    assert seal < hide < carve


#: Every step a child run takes, in order: ``run_child``'s own, with ``place_masks``'s in
#: the middle. Each is a control, so dropping one or moving it is a change to the sandbox.
_CHILD_STAGES = (
    "pick_stand_in_root",
    "check_crew_home_aliases",
    "preread_exposed_files",
    "stage_private_windows",
    "seal_readonly",
    "mask_sensitive",
    "apply_carveouts",
    "restore_exposed_files",
    "verify_fail_closed_aliases",
    "mask_sensitive_files",
    "mask_ssh_keys",
    "confirm_crew_home_aliases",
    "scrub_env",
    "drop_privileges",
    "install_seccomp",
    "refuse_hardlinked_credentials",
    "exec_agent",
)


def test_a_child_run_takes_every_stage_once_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: list[str] = []
    for name in _CHILD_STAGES:
        monkeypatch.setattr(program, name, lambda *_args, name=name: taken.append(name))
    program.run_child(launch(tmp_path), ["/bin/agent"])
    assert taken == list(_CHILD_STAGES)


def _scan_only_the_workspace(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> str:
    """Make *workspace* the cwd the pre-exec scan walks, and keep it off the host's /tmp."""
    monkeypatch.chdir(workspace)
    isdir = os.path.isdir
    monkeypatch.setattr(program.os.path, "isdir", lambda path: path != "/tmp" and isdir(path))
    return os.getcwd()


def test_a_child_run_refuses_a_hardlinked_credential_before_it_execs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _Home(tmp_path)
    credential = tmp_path / "app-credential"
    credential.write_text("s")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.link(credential, workspace / "copy")
    info = os.stat(credential)
    cwd = _scan_only_the_workspace(monkeypatch, workspace)
    plan = home.plan(alias_credential_ids=[[info.st_dev, info.st_ino]])
    run = launch(tmp_path, plan, environ={"HOME": str(home.root)})
    message = refusal(program.run_child, run, ["/bin/agent"])
    assert message is not None and "found hardlink(s) to protected credential inodes" in message
    assert os.path.join(cwd, "copy") in message
    assert run.execs == []


def test_a_child_run_refuses_a_credential_alias_swapped_since_the_pass(tmp_path: Path) -> None:
    home = _Home(tmp_path)
    alias = tmp_path / "alias"
    alias.write_text("secret")
    entry = _alias_entry(alias)
    alias.rename(tmp_path / "renamed")  # still on disk, so the substitute is a new inode
    alias.write_text("substitute")
    plan = home.plan(fail_closed_file_masks=[entry])
    run = launch(tmp_path, plan, environ={"HOME": str(home.root)})
    message = refusal(program.run_child, run, ["/bin/agent"])
    assert message is not None and "names a different inode" in message
    assert run.execs == []


def test_every_stand_in_carries_the_pid_prefix_the_janitor_reclaims(tmp_path: Path) -> None:
    """The mount-source sweep reclaims by the pid in each source's name, so every bind
    source this launcher creates must sit under the stand-in root with that prefix."""
    run, _, libc = _ran(tmp_path)
    sources = [
        os.fsdecode(call.source)
        for call in libc.calls
        if isinstance(call.source, bytes)
        and call.flags & _MS_BIND
        and not os.fsdecode(call.source).startswith("/proc/self/fd/")
    ]
    staged = [s for s in sources if os.path.dirname(s) == run.tmpfs_src]
    assert staged, "no stand-in was created under the stand-in root"
    assert all(os.path.basename(s).startswith(run.src_prefix) for s in staged)
    assert run.src_prefix == "kirocrew_sb_%d_" % os.getpid()


#: Every mount a child run over :class:`_Home` makes, in order.
_MOUNTS_PER_RUN = 16


@pytest.mark.parametrize("fail_at", range(1, _MOUNTS_PER_RUN + 1), ids=lambda n: f"mount-{n}")
def test_a_failed_hiding_mount_refuses_and_a_failed_carve_out_degrades(
    tmp_path: Path, fail_at: int, capfd: pytest.CaptureFixture[str]
) -> None:
    home = _Home(tmp_path)
    libc = CoveringLibc(fail_at=fail_at)
    run = launch(tmp_path, home.plan(), libc=libc, environ={"HOME": str(home.root)})
    message = refusal(program.place_masks, run)
    failed = libc.calls[fail_at - 1]
    if failed.target_path == str(home.probe):
        assert message is None
        assert "sandbox: WARNING -- writable carve-out" in capfd.readouterr().err
    elif failed.fstype == b"tmpfs":
        assert message is None
        assert "could not mount a private tmpfs" in capfd.readouterr().err
    else:
        assert message is not None and message.startswith("sandbox: BLOCKED -- ")
        assert "errno %d" % errno.EPERM in message


def test_the_mount_count_covers_every_site(tmp_path: Path) -> None:
    """The refusal sweep above stops at the last mount a run makes."""
    _, _, libc = _ran(tmp_path)
    assert len(libc.calls) == _MOUNTS_PER_RUN


# --------------------------------------------------------------------------- #
# Namespaces and the stand-in root.
# --------------------------------------------------------------------------- #


def test_entering_namespaces_signals_the_parent_and_pins_propagation(tmp_path: Path) -> None:
    libc = RecordingLibc()
    run = launch(tmp_path, libc=libc)
    c2p_r, c2p_w = os.pipe()
    p2c_r, p2c_w = os.pipe()
    os.write(p2c_w, b"x")
    try:
        program.enter_namespaces(run, c2p_w, p2c_r)
        assert os.read(c2p_r, 1) == b"x"
    finally:
        os.close(c2p_r)
        os.close(p2c_w)
    assert libc.unshared == [program._CLONE_NEWUSER, program._CLONE_NEWNS]
    assert run.nondumpable is True
    assert libc.calls[0].target == b"/" and libc.calls[0].flags == (1 << 18) | 16384


@pytest.mark.parametrize("step, expected", [(0, "unshare(NEWUSER)"), (1, "unshare(NEWNS)")])
def test_a_failed_unshare_refuses(tmp_path: Path, step: int, expected: str) -> None:
    class _Refusing(RecordingLibc):
        def unshare(self, flags):  # noqa: ANN001, ANN201
            self.unshared.append(flags)
            if len(self.unshared) == step + 1:
                ctypes.set_errno(errno.EPERM)
                return -1
            return 0

    run = launch(tmp_path, libc=_Refusing())
    c2p_r, c2p_w = os.pipe()
    p2c_r, p2c_w = os.pipe()
    os.write(p2c_w, b"x")
    try:
        message = refusal(program.enter_namespaces, run, c2p_w, p2c_r)
    finally:
        for fd in (c2p_r, c2p_w, p2c_r, p2c_w):
            try:
                os.close(fd)
            except OSError:
                pass
    assert message == f"sandbox: {expected} failed: errno {errno.EPERM}"


def test_the_stand_in_root_is_the_first_candidate_off_the_home_filesystem(tmp_path: Path) -> None:
    same_fs = tmp_path / "same-fs"
    other_fs = "/proc/self"  # a different filesystem from tmp_path, but not writable
    good = tmp_path / "good"
    same_fs.mkdir()
    good.mkdir()
    run = launch(
        tmp_path,
        payload(stand_in_roots=[str(same_fs), other_fs, str(good)]),
        environ={"HOME": str(tmp_path)},
    )
    run.tmpfs_src = None
    program.pick_stand_in_root(run)
    # same-fs is skipped for sharing HOME's filesystem, /proc/self refuses the probe,
    # and good shares HOME's filesystem too -- so nothing qualifies.
    assert run.tmpfs_src is None
    run.environ["HOME"] = "/proc"
    program.pick_stand_in_root(run)
    assert run.tmpfs_src == str(same_fs)
    assert os.listdir(same_fs) == []  # the probe directory was removed again


# --------------------------------------------------------------------------- #
# The crew-home alias checks.
# --------------------------------------------------------------------------- #


def _alias_bed(tmp_path: Path) -> tuple[Path, Path, list[Any]]:
    real = tmp_path / "real-crew"
    real.mkdir()
    alias = tmp_path / "alias-crew"
    alias.symlink_to(real, target_is_directory=True)
    info = os.stat(real)
    return real, alias, [str(alias), str(real), info.st_dev, info.st_ino]


def test_an_alias_that_still_reaches_the_data_home_passes_both_checks(tmp_path: Path) -> None:
    _, _, entry = _alias_bed(tmp_path)
    run = launch(tmp_path, payload(crew_home_aliases=[entry]))
    assert refusal(program.check_crew_home_aliases, run) is None
    assert refusal(program.confirm_crew_home_aliases, run) is None


def test_a_re_aimed_alias_is_refused_before_and_after_the_masks(tmp_path: Path) -> None:
    real, alias, entry = _alias_bed(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    alias.unlink()
    alias.symlink_to(elsewhere, target_is_directory=True)
    run = launch(tmp_path, payload(crew_home_aliases=[entry]))
    before = refusal(program.check_crew_home_aliases, run)
    after = refusal(program.confirm_crew_home_aliases, run)
    assert before is not None and "reaches a different directory now" in before
    assert after is not None and "now that the masks are placed" in after


def test_a_swapped_canonical_and_an_unreadable_alias_are_refused(tmp_path: Path) -> None:
    real, alias, entry = _alias_bed(tmp_path)
    real.rename(tmp_path / "moved")
    real.mkdir()
    run = launch(tmp_path, payload(crew_home_aliases=[entry]))
    swapped = refusal(program.check_crew_home_aliases, run)
    assert swapped is not None and "holds a different directory now" in swapped
    real.rmdir()
    gone = refusal(program.check_crew_home_aliases, run)
    assert gone is not None and "cannot be read now" in gone
    gone_after = refusal(program.confirm_crew_home_aliases, run)
    assert gone_after is not None and "cannot be read back after masking" in gone_after


# --------------------------------------------------------------------------- #
# Exposed files, credential aliases, ~/.ssh.
# --------------------------------------------------------------------------- #


def test_an_unreadable_exposed_file_degrades_to_absent_with_a_warning(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "config"
    source.write_text("x")
    source.chmod(0)
    try:
        if os.access(source, os.R_OK):
            pytest.skip("this host can read a mode-0 file")
        run = launch(tmp_path, payload(expose_files=[[str(source), "config"]]))
        program.preread_exposed_files(run)
    finally:
        source.chmod(0o600)
    assert run.expose_data == {}
    assert "sandbox: WARNING — cannot read" in capfd.readouterr().err


def _alias_entry(path: Path) -> list[Any]:
    info = os.lstat(path)
    return [str(path), info.st_dev, info.st_ino]


def test_a_credential_alias_must_still_be_the_discovered_regular_file(tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.write_text("secret")
    entry = _alias_entry(alias)
    run = launch(tmp_path, payload(fail_closed_file_masks=[entry]))
    assert refusal(program.verify_fail_closed_aliases, run) is None
    alias.rename(tmp_path / "renamed")  # still on disk, so the substitute is a new inode
    alias.write_text("other")
    replaced = refusal(program.verify_fail_closed_aliases, run)
    assert replaced is not None and "names a different inode" in replaced
    alias.unlink()
    gone = refusal(program.verify_fail_closed_aliases, run)
    assert gone is not None and "could not be read before masking it" in gone
    alias.mkdir()
    run = launch(tmp_path, payload(fail_closed_file_masks=[_alias_entry(alias)]))
    not_file = refusal(program.verify_fail_closed_aliases, run)
    assert not_file is not None and "is no longer a regular file" in not_file


def test_an_ssh_dir_the_pass_saw_that_is_gone_now_is_refused(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    seen = os.lstat(ssh)
    ssh.rmdir()
    occupants = {str(ssh): [seen.st_dev, seen.st_ino, 0, 1, seen.st_dev, seen.st_ino]}
    run = launch(
        tmp_path,
        payload(
            hide_ssh=1,
            ssh_dir=str(ssh),
            ssh_known_hosts=str(ssh / "known_hosts"),
            mask_occupants=occupants,
        ),
    )
    message = refusal(program.mask_ssh_keys, run)
    assert message is not None and "vanished" in message


def test_a_dangling_ssh_link_is_skipped_with_a_warning(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    ssh = tmp_path / ".ssh"
    ssh.symlink_to(tmp_path / "nowhere")
    run = launch(
        tmp_path,
        payload(hide_ssh=1, ssh_dir=str(ssh), ssh_known_hosts=str(ssh / "known_hosts")),
    )
    assert refusal(program.mask_ssh_keys, run) is None
    assert "is not a directory" in capfd.readouterr().err


def test_an_unreadable_known_hosts_aborts_rather_than_trusting_every_host(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    known = ssh / "known_hosts"
    known.write_text("h")
    known.chmod(0)
    try:
        if os.access(known, os.R_OK):
            pytest.skip("this host can read a mode-0 file")
        run = launch(tmp_path, payload(hide_ssh=1, ssh_dir=str(ssh), ssh_known_hosts=str(known)))
        with pytest.raises(PermissionError):
            program.mask_ssh_keys(run)
    finally:
        known.chmod(0o600)
    assert "sandbox: FATAL — cannot read" in capfd.readouterr().err


def test_no_ssh_mask_outside_the_strict_tier(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("key")
    libc = CoveringLibc()
    run = launch(
        tmp_path,
        payload(hide_ssh=0, ssh_dir=str(ssh), ssh_known_hosts=str(ssh / "known_hosts")),
        libc=libc,
    )
    program.mask_ssh_keys(run)
    assert libc.calls == [] and _listing(ssh) == ["id_rsa"]


# --------------------------------------------------------------------------- #
# The unreadable stand-in.
# --------------------------------------------------------------------------- #


def test_an_unreadable_leaf_falls_back_to_an_empty_mask_when_tmpfs_is_refused(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "token_signing.key"
    key.write_bytes(b"k")
    run = launch(
        tmp_path,
        payload(sensitive_files=[str(key)], unreadable_masks=["token_signing.key"]),
    )
    run.nondumpable = True
    program.mask_sensitive_files(run)
    assert key.read_bytes() == b""
    assert "could not mount a private tmpfs for the unreadable mask" in capfd.readouterr().err
    assert run.libc.prctls[-1] == (program._PR_SET_DUMPABLE, 1, 0, 0, 0)


def test_an_unreadable_leaf_without_a_non_dumpable_launcher_gets_the_plain_mask(
    tmp_path: Path,
) -> None:
    key = tmp_path / "token_signing.key"
    key.write_bytes(b"k")
    libc = CoveringLibc()
    run = launch(
        tmp_path,
        payload(sensitive_files=[str(key)], unreadable_masks=["token_signing.key"]),
        libc=libc,
    )
    program.mask_sensitive_files(run)
    assert key.read_bytes() == b"" and libc.prctls == []
    assert all(call.fstype is None for call in libc.calls)


# --------------------------------------------------------------------------- #
# Confinement.
# --------------------------------------------------------------------------- #


def test_without_prctl_the_spawn_is_refused(tmp_path: Path) -> None:
    libc = RecordingLibc()
    libc.prctl = None  # type: ignore[assignment]
    run = launch(tmp_path, libc=libc)
    message = refusal(program.drop_privileges, run)
    assert message is not None and "exposes no prctl(2)" in message
    assert refusal(program.install_seccomp, run) is None


def test_a_failed_no_new_privs_or_seccomp_install_is_refused(tmp_path: Path) -> None:
    class _Failing(RecordingLibc):
        def __init__(self, failing_option: int) -> None:
            super().__init__()
            self.failing_option = failing_option

        def prctl(self, option, a2, a3, a4, a5):  # noqa: ANN001, ANN201
            self.prctls.append((option, a2, a3, a4, a5))
            return -1 if option == self.failing_option else 0

    nnp = refusal(program.drop_privileges, launch(tmp_path, libc=_Failing(38)))
    assert nnp == "sandbox: BLOCKED — failed to set NO_NEW_PRIVS (prctl returned -1)"
    seccomp = refusal(program.install_seccomp, launch(tmp_path, libc=_Failing(22)))
    assert seccomp == (
        "sandbox: BLOCKED — failed to install seccomp-BPF filter (prctl returned -1)"
    )


def test_an_unknown_architecture_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(program._plat, "machine", lambda: "riscv64")
    message = refusal(program.install_seccomp, launch(tmp_path))
    assert message is not None and "no seccomp syscall table for machine 'riscv64'" in message
    with pytest.raises(ValueError):
        program.seccomp_program("riscv64")


def _bpf_verdict(insns: list[bytes], nr: int, arg0: int, arch: int) -> int:
    """Run the seccomp program on one syscall: the classic-BPF subset it uses."""
    import struct

    data = {0: nr, 4: arch, 16: arg0 & 0xFFFFFFFF}
    acc = 0
    pc = 0
    while True:
        code, jt, jf, k = struct.unpack("<HBBI", insns[pc])
        if code == 0x20:  # LD | W | ABS
            acc = data[k]
            pc += 1
        elif code == 0x15:  # JMP | JEQ | K
            pc += 1 + (jt if acc == k else jf)
        elif code == 0x06:  # RET | K
            return k
        else:  # pragma: no cover - an opcode the program does not use
            raise AssertionError(f"unexpected opcode {code:#x}")


_ALLOW, _EPERM, _KILL = 0x7FFF0000, 0x00050001, 0


@pytest.mark.parametrize(
    "machine, arch, denied, kill_nr, other",
    [
        ("x86_64", 0xC000003E, (165, 166, 272, 308, 155), 62, 0),
        ("aarch64", 0xC00000B7, (40, 39, 97, 268, 41), 129, 63),
    ],
)
def test_the_seccomp_filter_denies_namespace_escape_and_the_kill_broadcast(
    machine: str, arch: int, denied: tuple[int, ...], kill_nr: int, other: int
) -> None:
    insns = program.seccomp_program(machine)
    for nr in denied:
        assert _bpf_verdict(insns, nr, 0, arch) == _EPERM
    assert _bpf_verdict(insns, kill_nr, -1, arch) == _EPERM
    # Only the low 32 bits name the pid: kill(-1) zero-extended is still the broadcast.
    assert _bpf_verdict(insns, kill_nr, 0x00000000FFFFFFFF, arch) == _EPERM
    assert _bpf_verdict(insns, kill_nr, 1234, arch) == _ALLOW
    assert _bpf_verdict(insns, kill_nr, -1234, arch) == _ALLOW  # a process group
    assert _bpf_verdict(insns, other, 0, arch) == _ALLOW
    # A foreign architecture's syscall table is killed outright (the int 0x80 bypass).
    assert _bpf_verdict(insns, other, 0, 0x40000003) == _KILL


# --------------------------------------------------------------------------- #
# The pre-exec hardlink scan.
# --------------------------------------------------------------------------- #


def test_a_hardlink_to_a_protected_credential_refuses_the_exec(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("s")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.link(secret, workspace / "copy")
    run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
    message = refusal(program.refuse_hardlinked_credentials, run, [str(workspace)])
    assert message is not None and "found hardlink(s) to protected credential inodes" in message


def test_the_scan_is_skipped_when_no_credential_has_a_second_link(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("s")
    run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
    assert refusal(program.refuse_hardlinked_credentials, run, ["/nonexistent-root"]) is None


def test_an_alias_inode_the_parent_carried_arms_the_scan(tmp_path: Path) -> None:
    hidden = tmp_path / "hidden"
    hidden.write_text("s")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.link(hidden, workspace / "alias")
    info = os.stat(hidden)
    run = launch(tmp_path, payload(alias_credential_ids=[[info.st_dev, info.st_ino]]))
    message = refusal(program.refuse_hardlinked_credentials, run, [str(workspace)])
    assert message is not None and str(workspace / "alias") in message


def test_a_scan_past_its_budget_degrades_open_with_a_warning(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    secret = tmp_path / "creds" / "key"
    secret.parent.mkdir()
    secret.write_text("s")
    os.link(secret, tmp_path / "creds" / "key2")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("a", "b", "c"):
        (workspace / name).write_text(name)
    run = launch(tmp_path, payload(sensitive_dirs=[str(secret.parent)]))
    assert refusal(program.refuse_hardlinked_credentials, run, [str(workspace)], 2) is None
    assert "pre-exec hardlink scan truncated at 2 files" in capfd.readouterr().err


# --------------------------------------------------------------------------- #
# Locked mount flags.
# --------------------------------------------------------------------------- #


def test_the_seal_reasserts_every_locked_mount_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Vfs:
        f_flag = os.ST_NOSUID | os.ST_NODEV | os.ST_NOEXEC

    monkeypatch.setattr(program.os, "statvfs", lambda _target: _Vfs())
    assert program._locked_mount_flags(b"/x") == 2 | 4 | 8

    def _broken(_target: object) -> object:
        raise OSError(errno.ENOENT, "gone")

    monkeypatch.setattr(program.os, "statvfs", _broken)
    assert program._locked_mount_flags(b"/x") == 0


def test_the_rendered_program_runs_isolated_with_no_site_packages(tmp_path: Path) -> None:
    """The child runs the file as ``python -I -S``: no site-packages, so the program must
    start on the standard library alone and refuse an empty command before forking."""
    import subprocess

    script = tmp_path / "kirocrew_sandbox_probe.py"
    script.write_text(sandbox_launcher.render_namespace_launcher(_plan(tmp_path)), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
        timeout=60,
        check=False,
    )
    assert done.returncode == 1
    assert done.stderr.strip() == "sandbox_launcher: no command given"
