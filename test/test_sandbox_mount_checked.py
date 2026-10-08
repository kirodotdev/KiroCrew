"""Every mount in the namespace launcher refuses to exec when it fails.

Each ``mount(2)`` in the launcher IS a security control: three hide credential
paths, one pins mount propagation so the hiding cannot escape, and a pair exposes
the governance cache read-only (bind, then remount MS_RDONLY -- the remount is
what withholds the write, since MS_RDONLY is ignored on the initial bind).
Discarding the return value would make them all fail OPEN -- the path stays visible,
or stays WRITABLE, and the agent runs anyway -- and nothing downstream would notice,
because there is no post-mount emptiness check, the launcher has no logger, and the
pre-exec hardlink scan only fires when a credential happens to carry an extra link.

These tests drive the launcher program's own stages
(:func:`~kiro_crew.sandbox_launcher_program.enter_namespaces`, then
:func:`~kiro_crew.sandbox_launcher_program.place_masks`) with the shared stand-in libc
from ``test_sandbox_launcher_program``, whose ``mount`` fails on a chosen call. That is
the only way to exercise the failure path at all: this test process cannot create a
user namespace (a nested ``unshare`` is seccomp-denied inside an agent sandbox), and
even outside one a real EPERM would need an LSM mount rule the test cannot install. The
stand-in covers each bind's target as a real mount would, so every check the stages
make after a mount runs for real.

One check has no behavioural form: that no mount site anywhere in the program skips
the guard, including sites a given run does not reach. That one is an AST scan of the
program source, and of the launcher each tier renders from it.
"""

from __future__ import annotations

import ast
import collections
import errno
import os
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from test_sandbox_launcher_program import CoveringLibc, identity, launch, payload, refusal

from kiro_crew import sandbox, sandbox_launcher, sandbox_launcher_program

program = sandbox_launcher_program

# The source scan builds every tier's launcher, and ``_build_launcher_script`` calls
# POSIX-only ``os.getuid``/``os.getgid`` (the namespace launcher is Linux-only), so it
# raises AttributeError on Windows. Guarded rather than listed in
# ``test/windows-expected-failures.txt``: that list is a burn-down backlog of gaps to
# close, and a POSIX-only launcher is a permanent platform boundary. The sibling
# launcher suites take the same route -- see ``test_sandbox_argv.py``.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="_build_launcher_script uses POSIX-only os.getuid (#2041)",
)

#: The stages pin every target through ``O_PATH`` and mount it as ``/proc/self/fd/<n>``,
#: which only Linux has, so the stage-driven tests run there alone.
_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the launcher stages pin with os.O_PATH and mount through /proc/self/fd, "
    "neither of which exists outside Linux",
)

_MS_RDONLY = 1
_MS_REMOUNT = 32
_MS_BIND = 4096
_MNT_DETACH = 2


@pytest.fixture(autouse=True)
def _pin_ssh_accept_new(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``_ssh_supports_accept_new`` at the seam ``_build_launcher_script`` reads.

    The real probe runs the host's ``ssh -V``. It is ``lru_cache``d, but any test
    that clears the cache (``TestSshSupportsAcceptNew`` does) hands the next
    launcher-building test in the process a real spawn -- a host program none of the
    launcher suites is about (test-hygiene class 7). ``True`` is what a modern host
    answers.
    """
    monkeypatch.setattr("kiro_crew.sandbox._ssh_supports_accept_new", lambda: True)


class _Libc(CoveringLibc):
    """The covering libc, with each ``umount2`` recorded together with its flags.

    Kept off ``calls`` on purpose: ``fail_at`` numbers the MOUNTS in the order the
    stages make them, and counting an unmount there would renumber every case below.
    """

    def __init__(self, *, fail_at: int | None, err: int = errno.EPERM) -> None:
        super().__init__(fail_at=fail_at, fail_errno=err)
        self.unmounts: list[tuple[object, int]] = []

    def umount2(self, target, flags):  # noqa: ANN001, ANN201
        self.unmounts.append((target, flags))
        return super().umount2(target, flags)


def _child_mounts(run: program.Launch, stages: Iterable[Callable[[program.Launch], None]]) -> None:
    """Enter the namespaces with the parent's maps already written, then run *stages*."""
    c2p_r, c2p_w = os.pipe()
    p2c_r, p2c_w = os.pipe()
    os.write(p2c_w, b"x")
    try:
        # Closes c2p_w and p2c_r itself, before its first mount.
        program.enter_namespaces(run, c2p_w, p2c_r)
    finally:
        os.close(c2p_r)
        os.close(p2c_w)
    for stage in stages:
        stage(run)


def _run(
    tmp_path: Path,
    *,
    fail_at: int | None,
    err: int = errno.EPERM,
    writable_dirs: list[str] | None = None,
    private_dirs: list[str] | None = None,
    sensitive_dirs: list[str] | None = None,
    readonly_dirs: list[str] | None = None,
    private_dir_ids: dict[str, list[int]] | None = None,
    stages: Iterable[Callable[[program.Launch], None]] = (program.place_masks,),
) -> tuple[_Libc, str | None, program.Launch]:
    """Run the launcher's mounts over a fake home.

    Returns ``(libc, refusal_message_or_None, launch)``. By default every mount the
    child makes before it scrubs the environment runs: the propagation mount, then
    :func:`~kiro_crew.sandbox_launcher_program.place_masks`. With no overrides that is
    six sites, in this order: 1 = propagation, 2 = read-only bind, 3 = read-only
    remount, 4 = first credential dir, 5 = first sensitive file, 6 = ~/.ssh. The seal
    pair comes BEFORE the credential hide on purpose: a hidden leaf under a sealed
    parent must be hidden on top of the parent's self-bind, or the non-recursive bind
    masks the hide.
    """
    home = tmp_path / "home"
    aws = home / ".aws"
    # ``exist_ok``: a case that needs a private window inside this mask root creates the
    # window first, because the staging stage resolves it before this setup would run.
    aws.mkdir(parents=True, exist_ok=True)
    (aws / "credentials").write_text("[default]\n")
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "known_hosts").write_text("example.com ssh-rsa AAAA\n")
    lone = home / ".netrc"
    lone.write_text("machine example.com\n")
    # The governance cache: exposed read-only rather than hidden, so it is the one
    # target whose rule is a REAL bind of itself plus a sealing remount.
    cache = home / ".kiro" / "crew" / "policy_cache"
    cache.mkdir(parents=True)
    (cache / "policy.json").write_text("{}\n")

    libc = _Libc(fail_at=fail_at, err=err)
    plan = payload(
        # Overridable so the nesting test can hand the stages a hidden leaf that lives
        # INSIDE a sealed parent; the default keeps the six-site numbering.
        sensitive_dirs=[str(aws)] if sensitive_dirs is None else list(sensitive_dirs),
        # Empty by default: a private window stages its own bind, which would shift the
        # call numbering, and these cases vouch for no window identity.
        private_dirs=list(private_dirs or []),
        private_dir_ids=dict(private_dir_ids or {}),
        readonly_dirs=[str(cache)] if readonly_dirs is None else list(readonly_dirs),
        # Empty by default so the six-site call numbering above stays stable; the
        # carve-out tests inject their own entry.
        writable_dirs=list(writable_dirs or []),
        sensitive_files=[str(lone)],
        ssh_dir=str(ssh),
        ssh_known_hosts=str(ssh / "known_hosts"),
        hide_ssh=1,
    )
    run = launch(tmp_path, plan, libc=libc, environ={"HOME": str(home)})
    return libc, refusal(_child_mounts, run, stages), run


def _staged(libc: _Libc, run: program.Launch) -> list[bytes]:
    """Targets of the window staging mounts: binds INTO a fresh stage under the stand-in root.

    Every hiding mount sources from a descriptor path or a stand-in as well, so the
    source alone cannot tell a stage from a mask; the TARGET can.
    """
    return [
        call.target
        for call in libc.calls
        if isinstance(call.source, bytes)
        and call.source.startswith(b"/proc/self/fd/")
        and isinstance(call.target, bytes)
        and call.target.startswith(os.fsencode(run.tmpfs_src))
    ]


# --------------------------------------------------------------------------
# Behavioural assertions
# --------------------------------------------------------------------------


@_LINUX_ONLY
def test_all_mounts_succeeding_lets_the_exec_proceed(tmp_path: Path) -> None:
    """The guard must not turn a healthy spawn into a refusal."""
    libc, message, _ = _run(tmp_path, fail_at=None)
    assert message is None
    # propagation + read-only bind + its sealing remount + credential dir + file + ssh
    assert len(libc.calls) == 6


@_LINUX_ONLY
@pytest.mark.parametrize(
    ("fail_at", "expect_in_message"),
    [
        (1, "propagation"),
        (2, "exposing read-only path"),
        (3, "sealing read-only path"),
        (4, "credential directory"),
        (5, "sensitive file"),
        (6, "ssh key directory"),
    ],
    ids=[
        "propagation",
        "readonly-bind",
        "readonly-seal",
        "credential-dir",
        "sensitive-file",
        "ssh-dir",
    ],
)
def test_a_failed_mount_refuses_to_exec(
    tmp_path: Path, fail_at: int, expect_in_message: str
) -> None:
    """Each of the six sites refuses, and says which control failed.

    ``readonly-seal`` is the one whose failure is least visibly a security
    failure and most needs the refusal: the bind succeeded, so the directory is
    THERE and readable, and only the remount that withholds write did not land.
    Proceeding would hand the child exactly the write access the pair exists to
    deny, with nothing observably wrong.
    """
    libc, message, _ = _run(tmp_path, fail_at=fail_at)
    assert message is not None, f"site {fail_at} let the spawn proceed"
    assert "sandbox: BLOCKED" in message
    assert expect_in_message in message
    # Stops AT the failure: no mount is attempted after the one that failed.
    assert len(libc.calls) == fail_at


@_LINUX_ONLY
def test_the_refusal_names_the_hidden_path(tmp_path: Path) -> None:
    """An operator needs the path, not just 'a mount failed'.

    The path the operator must act on is the NAME they configured, which is what
    the label carries. The mount target itself is a descriptor path pinning the
    object that name resolved to, and would tell them nothing.
    """
    _libc, message, _ = _run(tmp_path, fail_at=4)
    assert message is not None
    assert str(tmp_path / "home" / ".aws") in message


@_LINUX_ONLY
def test_the_refusal_carries_the_errno(tmp_path: Path) -> None:
    """errno is the only thing that distinguishes an LSM denial from ENOMEM."""
    _libc, message, _ = _run(tmp_path, fail_at=2, err=errno.ENOMEM)
    assert message is not None
    assert str(errno.ENOMEM) in message
    assert os.strerror(errno.ENOMEM) in message


@_LINUX_ONLY
def test_the_refusal_names_the_deliberate_opt_out(tmp_path: Path) -> None:
    """Refusing is only defensible if the message says how to opt out.

    It names the real operator setting, ``agent.sandbox``.
    """
    _libc, message, _ = _run(tmp_path, fail_at=1)
    assert message is not None
    assert "agent.sandbox" in message


@_LINUX_ONLY
def test_a_private_windows_stage_does_not_outlive_the_mask(tmp_path: Path) -> None:
    """The staging mount is a SECOND path to the window's real tree, and must not survive.

    The stage carries the window's inode across the bind that hides its parent, so the
    window can be bound back at its own path. What it leaves behind is the same tree
    reachable under the stand-in root, which nothing masks -- and a masked leaf INSIDE a
    window is re-hidden at the window's path only, because a non-recursive bind carries no
    submount. So the leaf would be readable through the stage with the mask otherwise
    fully applied.
    """
    window = tmp_path / "home" / ".aws" / "alpha" / "data"
    window.mkdir(parents=True)

    libc, message, run = _run(tmp_path, fail_at=None, private_dirs=[str(window)])

    assert message is None
    staged = _staged(libc, run)
    assert len(staged) == 1, f"expected one staging mount, got {staged}"
    assert libc.unmounts == [(staged[0], _MNT_DETACH)], "the stage was not detached"
    assert not os.path.exists(staged[0].decode()), "the stage directory survived"


@_LINUX_ONLY
def test_a_vouched_window_whose_identity_changed_refuses_the_spawn(tmp_path: Path) -> None:
    """A pinned window that is not the approved directory must end the spawn.

    Skipping it would leave the parent's mask over the path, and that mask is an empty
    WRITABLE bind: the child's writes under it succeed and disappear with the namespace,
    so the app loses exactly the data the window exists to keep durable.
    """
    window = tmp_path / "home" / ".aws" / "alpha" / "data"
    window.mkdir(parents=True)
    real = os.lstat(window)

    libc, message, run = _run(
        tmp_path,
        fail_at=None,
        private_dirs=[str(window)],
        private_dir_ids={str(window): [real.st_dev, real.st_ino + 1]},
    )

    assert message is not None, "a mismatched window identity was accepted"
    assert "approved as a data window" in message
    staged = _staged(libc, run)
    assert staged == [], f"the mismatched window was staged anyway: {staged}"


@_LINUX_ONLY
def test_a_vouched_window_that_cannot_be_opened_refuses_the_spawn(tmp_path: Path) -> None:
    """The producer opened this window, so a child that cannot is in the same raced state."""
    (tmp_path / "home" / ".aws").mkdir(parents=True, exist_ok=True)
    window = tmp_path / "home" / ".aws" / "alpha" / "data"

    _libc, message, _ = _run(
        tmp_path,
        fail_at=None,
        private_dirs=[str(window)],
        private_dir_ids={str(window): [1, 2]},
    )

    assert message is not None, "an unopenable vouched window was skipped silently"
    assert "cannot open" in message


@_LINUX_ONLY
def test_a_window_whose_identity_matches_is_staged(tmp_path: Path) -> None:
    """The control: the ordinary case must still get its window."""
    window = tmp_path / "home" / ".aws" / "alpha" / "data"
    window.mkdir(parents=True)
    real = os.lstat(window)

    libc, message, run = _run(
        tmp_path,
        fail_at=None,
        private_dirs=[str(window)],
        private_dir_ids={str(window): [real.st_dev, real.st_ino]},
    )

    assert message is None, f"a matching window was refused: {message}"
    staged = _staged(libc, run)
    assert len(staged) == 1, f"expected one staging mount, got {staged}"


@_LINUX_ONLY
def test_a_window_no_one_vouched_for_is_skipped_not_refused(tmp_path: Path) -> None:
    """The second control: absence of an identity is not a mismatch.

    The window producers that pass paths only must keep an unopenable window a skip --
    nothing vouched for it, and refusing would turn an ordinary absent scratch directory
    into a dead spawn.
    """
    (tmp_path / "home" / ".aws").mkdir(parents=True, exist_ok=True)
    window = tmp_path / "home" / ".aws" / "alpha" / "data"

    _libc, message, _ = _run(tmp_path, fail_at=None, private_dirs=[str(window)])

    assert message is None, f"an unvouched window was refused: {message}"


# --------------------------------------------------------------------------
# No mount site skips the guard
# --------------------------------------------------------------------------


def _is_mount_call(node: ast.AST) -> bool:
    """A raw ``<libc>.mount(...)`` call: the one way the program reaches mount(2)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "mount"
    )


class _MountSites(ast.NodeVisitor):
    """Where a source calls ``mount(2)`` raw and whether each call's result is checked."""

    def __init__(self) -> None:
        self._functions: list[str] = []
        #: Raw mount calls, by the function that makes them.
        self.raw: collections.Counter[str] = collections.Counter()
        #: Each ``if <mount>(...) != 0:`` test, normalised, with the function it is in.
        self.checked: collections.Counter[tuple[str, str]] = collections.Counter()
        #: Calls of the refusing guard ``_mount_or_die``.
        self.guarded = 0

    def _where(self) -> str:
        return self._functions[-1] if self._functions else "<module>"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and _is_mount_call(test.left)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.NotEq)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == 0
        ):
            self.checked[(self._where(), ast.unparse(test))] += 1
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _is_mount_call(node):
            self.raw[self._where()] += 1
        elif isinstance(node.func, ast.Name) and node.func.id == "_mount_or_die":
            self.guarded += 1
        self.generic_visit(node)


def _mount_sites(tree: ast.AST) -> tuple[dict[str, int], dict[tuple[str, str], int], int]:
    sites = _MountSites()
    sites.visit(tree)
    return dict(sites.raw), dict(sites.checked), sites.guarded


#: Every raw mount the program may make, by function, each exactly once: the refusing
#: guard, its degrade-open sibling for the write carve-outs, and the unreadable mask's
#: private tmpfs.
_PERMITTED_RAW_MOUNTS = {"_mount_or_die": 1, "_mount_or_warn": 1, "_mount_private_tmpfs": 1}

#: Each of those calls IS the test of an ``if ... != 0:``, in exactly this form.
_CHECKED_RAW_MOUNTS = {
    ("_mount_or_die", "launch.libc.mount(source, target, None, flags, None) != 0"): 1,
    ("_mount_or_warn", "launch.libc.mount(source, target, None, flags, None) != 0"): 1,
    (
        "_mount_private_tmpfs",
        "launch.libc.mount(b'tmpfs', target, b'tmpfs', _MS_NOSUID | _MS_NODEV | _MS_NOEXEC, "
        "b'mode=0700,size=16k') != 0",
    ): 1,
}

#: The call sites of ``_mount_or_die``: propagation, the read-only bind and its sealing
#: remount, credential dirs, the private window's two -- staging its real contents out
#: before the parent is masked, then binding them onto the placeholder inside the
#: stand-in -- the sealing remount that makes a private window read-only after it is
#: bound, the nested re-mask that re-hides a masked leaf sitting INSIDE such a
#: window, sensitive files and the read-only seal on an unreadable mask, and ~/.ssh.
_GUARDED_SITES = 11

_EXPECTED_SITES = (_PERMITTED_RAW_MOUNTS, _CHECKED_RAW_MOUNTS, _GUARDED_SITES)


def test_every_tier_routes_all_eight_mounts_through_the_guard() -> None:
    """No tier may keep a raw, unchecked mount call site.

    The raw calls are pinned as an exact multiset, by function and by the normalised
    form of the check around each, not filtered by pattern, so any other raw call, a
    second copy of one of these, or one whose result goes unchecked goes red. Scanned in
    the program source and in the launcher each tier renders from it, which is what the
    child runs.
    """
    source = sandbox_launcher.launcher_program_source()
    assert _mount_sites(ast.parse(source)) == _EXPECTED_SITES
    for level in ("strict", "cc", "standard"):
        rendered = sandbox._build_launcher_script(level)
        assert _mount_sites(ast.parse(rendered)) == _EXPECTED_SITES, level


class _UnguardOneSite(ast.NodeTransformer):
    """Turn the first ``_mount_or_die(launch, src, tgt, flags, what)`` in *function* raw."""

    def __init__(self, function: str) -> None:
        self.function = function
        self._inside = False
        self.done = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        outer = self._inside
        self._inside = node.name == self.function
        self.generic_visit(node)
        self._inside = outer
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if not (
            self._inside
            and not self.done
            and isinstance(node.func, ast.Name)
            and node.func.id == "_mount_or_die"
        ):
            return node
        self.done = True
        launch_arg, src, tgt, flags, _what = node.args
        libc = ast.Attribute(value=launch_arg, attr="libc", ctx=ast.Load())
        none = ast.Constant(value=None)
        return ast.Call(
            func=ast.Attribute(value=libc, attr="mount", ctx=ast.Load()),
            args=[src, tgt, none, flags, none],
            keywords=[],
        )


def test_break_arm_reintroduce_raw_is_caught_by_the_tier_sweep() -> None:
    """The no-raw-call-sites sweep must fail when a raw call comes back.

    The same predicate the sweep uses: the sites must equal the permitted set on the
    program as it ships, and differ from it once the credential-dir hide is reverted
    to a raw, unchecked mount, so this arm cannot pass vacuously.
    """
    tree = ast.parse(sandbox_launcher.launcher_program_source())
    assert _mount_sites(tree) == _EXPECTED_SITES
    mutant = _UnguardOneSite("mask_sensitive")
    mutant.visit(tree)
    assert mutant.done
    raw, checked, guarded = _mount_sites(ast.fix_missing_locations(tree))
    assert (raw, checked, guarded) != _EXPECTED_SITES
    assert raw["mask_sensitive"] == 1
    assert not [where for where, _test in checked if where == "mask_sensitive"]
    assert guarded == _GUARDED_SITES - 1


# --------------------------------------------------------------------------
# Seal before hide: a hidden leaf under a sealed parent
# --------------------------------------------------------------------------


def _nested_pair(tmp_path: Path) -> tuple[str, str]:
    """A sealed parent and a hidden leaf inside it -- the ``run`` /
    ``run/voice-runtime`` shape, on paths under pytest's tmp_path."""
    parent = tmp_path / "home" / "run"
    leaf = parent / "voice-runtime"
    leaf.mkdir(parents=True)
    (leaf / "marker.txt").write_text("decoder image\n")
    return str(parent), str(leaf)


def _seal_and_hide_positions(
    libc: _Libc, parent_id: object, leaf_id: object
) -> tuple[int, int, int]:
    """Call indexes of the parent's self-bind, its sealing remount, and the
    leaf's hide, in the order the stages issued them.

    The stages pin their targets as ``/proc/self/fd/<n>`` descriptor paths, so a
    target is matched by the OBJECT it reached at mount time rather than by the
    spelling. The identities are taken before the run: once the leaf is masked its
    name reaches the stand-in instead.
    """
    calls = libc.calls
    self_bind = next(
        i for i, call in enumerate(calls) if call.target_id == parent_id and call.flags == _MS_BIND
    )
    remount = next(
        i
        for i, call in enumerate(calls)
        if call.target_id == parent_id and call.flags & _MS_REMOUNT
    )
    hide = next(
        i for i, call in enumerate(calls) if call.target_id == leaf_id and call.flags == _MS_BIND
    )
    return self_bind, remount, hide


@_LINUX_ONLY
def test_a_hidden_leaf_under_a_sealed_parent_is_hidden_after_the_seal(
    tmp_path: Path,
) -> None:
    """The leaf's empty-dir hide must be issued AFTER both halves of the
    parent's seal.

    A non-recursive ``MS_BIND`` does not replicate submounts, so a parent
    self-bind issued after the leaf's hide masks it: lookups through the new
    parent mount reach the REAL leaf, and the hide degrades to read-only
    visible (container measured on the shipped launcher: the marker inside
    ``run/voice-runtime`` was ``cat``-readable, writes EROFS). Issued after the
    seal, the hide is a mount ON the sealed parent and stays reachable through
    it -- the same property the write carve-outs rely on.
    """
    parent, leaf = _nested_pair(tmp_path)
    parent_id, leaf_id = identity(parent), identity(leaf)
    libc, message, _ = _run(tmp_path, fail_at=None, sensitive_dirs=[leaf], readonly_dirs=[parent])
    assert message is None
    self_bind, remount, hide = _seal_and_hide_positions(libc, parent_id, leaf_id)
    assert self_bind < remount < hide, [call.target_path for call in libc.calls]


@_LINUX_ONLY
def test_seal_before_hide_keeps_the_carveout_after_the_seal(tmp_path: Path) -> None:
    """The ordering must not disturb the carve-out's own constraint:
    the write carve-out is still issued after the parent's seal, and
    after the leaf hide, so neither the hide nor the carve-out is masked."""
    parent, leaf = _nested_pair(tmp_path)
    scratch = Path(parent) / "mcp-tmp" / "probe-x" / "tmp"
    scratch.mkdir(parents=True)
    parent_id, leaf_id = identity(parent), identity(leaf)
    libc, message, _ = _run(
        tmp_path,
        fail_at=None,
        sensitive_dirs=[leaf],
        readonly_dirs=[parent],
        writable_dirs=[str(scratch)],
    )
    assert message is None
    _self_bind, remount, hide = _seal_and_hide_positions(libc, parent_id, leaf_id)
    carve = next(
        i
        for i, call in enumerate(libc.calls)
        if call.target == str(scratch).encode() and call.flags == _MS_BIND
    )
    assert remount < hide < carve, [call.target_path for call in libc.calls]


@_LINUX_ONLY
def test_break_arm_hide_before_seal_is_caught(tmp_path: Path) -> None:
    """Running the hide stage ahead of the seal stage must falsify the ordering assertion.

    The same stages, composed in the other order, so the run differs from
    :func:`~kiro_crew.sandbox_launcher_program.place_masks` in ORDER only.
    """
    parent, leaf = _nested_pair(tmp_path)
    parent_id, leaf_id = identity(parent), identity(leaf)
    libc, message, _ = _run(
        tmp_path,
        fail_at=None,
        sensitive_dirs=[leaf],
        readonly_dirs=[parent],
        stages=(program.stage_private_windows, program.mask_sensitive, program.seal_readonly),
    )
    assert message is None
    self_bind, remount, hide = _seal_and_hide_positions(libc, parent_id, leaf_id)
    assert hide < self_bind < remount, "the composed stages did not reorder the mounts"


# --------------------------------------------------------------------------
# Write carve-out: the ONE access-WIDENING pair, and it fails OPEN
# --------------------------------------------------------------------------


def _carveout_home(tmp_path: Path) -> str:
    scratch = tmp_path / "home" / "run" / "mcp-tmp" / "probe-x" / "tmp"
    scratch.mkdir(parents=True)
    return str(scratch)


@_LINUX_ONLY
def test_carveout_mounts_run_and_spawn_proceeds(tmp_path: Path) -> None:
    """Healthy path: the pair runs (bind + rw remount) and nothing refuses."""
    scratch = _carveout_home(tmp_path)
    libc, message, _ = _run(tmp_path, fail_at=None, writable_dirs=[scratch])
    assert message is None
    # six guarded sites + the carve-out bind + its rw remount
    assert len(libc.calls) == 8
    bind, remount = libc.calls[4], libc.calls[5]
    assert bind.target == scratch.encode() and remount.target == scratch.encode()
    # The remount clears the seal: MS_RDONLY must NOT be re-passed.
    assert remount.flags & _MS_REMOUNT
    assert not remount.flags & _MS_RDONLY


@_LINUX_ONLY
@pytest.mark.parametrize("fail_at", [5, 6], ids=["carveout-bind", "carveout-remount"])
def test_a_failed_carveout_mount_degrades_open(
    tmp_path: Path, fail_at: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """The carve-out pair WIDENS access, so its failure must not refuse.

    A refused carve-out means the path stays sealed -- the default behavior,
    whose one consequence is an unwritable probe temp dir. The spawn must
    proceed (the remaining hiding mounts still run and still refuse on their
    own failures), and the operator gets the classifier's ADVISORY severity,
    not a fatal one.
    """
    scratch = _carveout_home(tmp_path)
    libc, message, _ = _run(tmp_path, fail_at=fail_at, writable_dirs=[scratch])
    assert message is None, "an access-widening mount failure must not refuse"
    # The hiding mounts AFTER the carve-out still ran: sensitive file + ssh,
    # and on a failed bind the pointless remount is skipped.
    expected_calls = 7 if fail_at == 5 else 8
    assert len(libc.calls) == expected_calls
    advisory = capsys.readouterr().err
    assert "sandbox: WARNING" in advisory
    assert "writable carve-out" in advisory
    assert "sandbox: BLOCKED" not in advisory
