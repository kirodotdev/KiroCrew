"""``temp_reclaim``: a temp tree is removed only when every check proves it may go.

Every tree lives under the test's own ``tmp_path``, which stands in for ``/tmp``
through the ``roots`` argument, and the process table is injected, so nothing
here reads or touches the real system temp roots. The one test that reads the
real ``/proc`` looks only for a child it started and reaps itself.
"""

from __future__ import annotations

import functools
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kiro_crew import temp_reclaim as tr

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="temp_reclaim is Linux only"
)

NOW = time.time()
QUIET = tr.ProcessView(uses=(), unreadable=0)


def _age(tree: Path, seconds: float = 2 * tr.IDLE_SECONDS) -> None:
    """Backdate every entry under *tree*, *tree* included, by *seconds*."""
    stamp = NOW - seconds
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in dirnames + filenames:
            os.utime(os.path.join(dirpath, name), (stamp, stamp), follow_symlinks=False)
    os.utime(tree, (stamp, stamp))


def _home(root: Path, name: str = "kc-home") -> Path:
    tree = root / name
    (tree / ".cache" / "kirocrew" / "pycache").mkdir(parents=True)
    (tree / ".cache" / "kirocrew" / "pycache" / "x.pyc").write_bytes(b"x")
    _age(tree)
    return tree


def _env_view(env: bytes, *, comm: str = "pytest") -> tr.ProcessView:
    """The view pid 4242, holding *env*, gives (as ``read_processes`` sorts it)."""
    found = tr._env_paths(env)
    return tr.ProcessView(
        uses=tuple((4242, comm, p) for kind, p in found if kind == tr.PATH_REF),
        roots=tuple((4242, comm, p) for kind, p in found if kind == tr.ROOT_REF),
        unreadable=0,
        unplaced=tuple(
            f"pid 4242 ({comm}) has {p}" for kind, p in found if kind == tr.UNPLACED_REF
        ),
    )


def _run(path: Path | str, root: Path, view: tr.ProcessView = QUIET, **kw) -> tr.Outcome:
    """One check-and-remove whose removal re-reads the same injected *view*."""
    kw.setdefault("reread", lambda: view)
    return tr.reclaim_one(str(path), view, now=NOW, roots=(str(root),), **kw)


def test_an_idle_unused_tree_is_removed(tmp_path):
    tree = _home(tmp_path)
    assert _run(tree, tmp_path) == tr.Outcome(str(tree), tr.REMOVED)
    assert not tree.exists()


def test_a_dry_run_reports_and_keeps(tmp_path):
    tree = _home(tmp_path)
    assert _run(tree, tmp_path, dry_run=True).outcome == tr.WOULD_REMOVE
    assert tree.is_dir()


def test_an_idle_regular_file_is_removed(tmp_path):
    leftover = tmp_path / "venv.log"
    leftover.write_text("log")
    _age(leftover)
    assert _run(leftover, tmp_path).outcome == tr.REMOVED
    assert not leftover.exists()


def test_a_recent_write_anywhere_keeps_the_tree(tmp_path):
    tree = _home(tmp_path)
    deep = tree / ".cache" / "kirocrew" / "pycache" / "x.pyc"
    os.utime(deep, (NOW - 60, NOW - 60))
    outcome = _run(tree, tmp_path)
    assert (outcome.outcome, outcome.reason.split(":")[0]) == (tr.KEPT, "recent")
    assert tree.is_dir()


def test_a_repository_inside_keeps_the_tree(tmp_path):
    tree = _home(tmp_path)
    (tree / "work" / ".git").mkdir(parents=True)
    (tree / "work" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    _age(tree)
    assert _run(tree, tmp_path).reason.startswith("holds-repository")
    assert tree.is_dir()


def test_uvs_own_git_under_cache_uv_does_not_count(tmp_path):
    tree = _home(tmp_path)
    (tree / ".cache" / "uv" / "sdists-v10" / ".git").mkdir(parents=True)
    (tree / ".cache" / "uv" / "sdists-v10" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    _age(tree)
    assert _run(tree, tmp_path).outcome == tr.REMOVED


def test_a_clone_elsewhere_under_cache_still_counts(tmp_path):
    tree = _home(tmp_path)
    (tree / ".cache" / "clone" / ".git").mkdir(parents=True)
    (tree / ".cache" / "clone" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    _age(tree)
    assert _run(tree, tmp_path).reason.startswith("holds-repository")


def test_a_bare_repository_is_kept(tmp_path):
    bare = tmp_path / "example.git"
    (bare / "objects").mkdir(parents=True)
    (bare / "refs").mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main\n")
    _age(bare)
    assert _run(bare, tmp_path).reason.startswith("holds-repository")
    assert (bare / "HEAD").is_file() and (bare / "objects").is_dir()


@pytest.mark.parametrize("nested", ["checkout/build", "example.git/objects", "deps/pkg"])
def test_only_a_top_level_entry_of_a_temp_root_is_accepted(tmp_path, nested):
    # Whatever sits between the temp root and a deeper path (a clone, a bare
    # repository, an import root) is never examined, so a deeper path is refused.
    outer, name = nested.split("/")
    tree = _home(tmp_path / outer, name)
    assert _run(tree, tmp_path).reason.startswith("outside-scope: not a top-level entry")
    assert tree.is_dir()


@pytest.mark.parametrize(
    "use",
    [
        "cwd",  # a working directory inside the tree
        "env",  # only named on PYTHONPATH
    ],
)
def test_a_process_naming_the_tree_keeps_it(tmp_path, use):
    tree = _home(tmp_path)
    if use == "cwd":
        view = tr.ProcessView(uses=((4242, "pytest", str(tree / ".cache")),), unreadable=0)
    else:
        view = _env_view(f"PYTHONPATH=/opt/x:{tree}/deps\0HOME=/home/me\0".encode())
    assert _run(tree, tmp_path, view).reason == "in-use: pid 4242 (pytest)"
    assert tree.is_dir()


def test_relative_and_empty_entries_cannot_be_placed():
    env = b"PYTHONPATH=:deps:/opt/x\0UV_CACHE_DIR=.cache\0XDG_DATA_DIRS=:share:/x\0"
    assert tr._env_paths(env) == [
        (tr.UNPLACED_REF, "PYTHONPATH entry ''"),
        (tr.UNPLACED_REF, "PYTHONPATH entry 'deps'"),
        (tr.ROOT_REF, "/opt/x"),
        (tr.UNPLACED_REF, "UV_CACHE_DIR entry '.cache'"),
        (tr.ROOT_REF, "/x"),
    ]


@pytest.mark.parametrize("env", [b"PYTHONPATH=deps\0", b"PYTHONPATH=:/opt/x\0"])
def test_a_relative_or_empty_import_entry_keeps_every_tree(tmp_path, env):
    # Python resolved it against the directory it started in, which /proc does
    # not show: a worker started in /tmp with PYTHONPATH=deps that moved to /
    # still imports from /tmp/deps.
    deps, other = _home(tmp_path, "deps"), _home(tmp_path, "other")
    view = _one_process(tmp_path, env)
    for tree in (deps, other):
        reason = _run(tree, tmp_path, view).reason
        assert reason.startswith(f"unprovable: pid {_FAKE_PIDS[0]} (worker) has PYTHONPATH entry")
        assert tree.is_dir()


def test_scalar_directory_values_are_never_split_at_a_colon(tmp_path):
    base = os.path.realpath(tmp_path)
    env = f"TMPDIR={base}/run:1\0HOME={base}/h:2\0PYTHONPATH={base}/a:{base}/b\0".encode()
    assert tr._env_paths(env) == [
        (tr.PATH_REF, f"{base}/run:1"),
        (tr.ROOT_REF, f"{base}/h:2"),
        (tr.ROOT_REF, f"{base}/a"),
        (tr.ROOT_REF, f"{base}/b"),
    ]


def test_a_tree_holding_a_socket_file_is_kept_and_one_without_is_not(tmp_path):
    # A server bound in before/ that was moved to moved/, with another socket at
    # the old name, leaves /proc/net/unix naming the wrong file. No reading of
    # it is trusted: a socket file anywhere in a tree keeps the tree.
    moved, plain = _home(tmp_path, "moved"), _home(tmp_path, "plain")
    (tmp_path / "before").mkdir()
    # mknod, not bind: a bound path under tmp_path can pass AF_UNIX's 108-byte limit.
    for sock in (moved / "ctl.sock", tmp_path / "before" / "ctl.sock"):
        os.mknod(sock, 0o600 | stat.S_IFSOCK)
    _age(moved)
    reason = "unprovable: holds a socket file, and /proc cannot say whether it is live"
    assert _run(moved, tmp_path).reason == reason
    assert os.path.lexists(moved / "ctl.sock")
    assert _run(plain, tmp_path).outcome == tr.REMOVED


def _mountinfo(tmp_path: Path, monkeypatch, *points: str) -> Path:
    """Point the module at a fake mount table listing *points* (escaped as the kernel does)."""
    table = tmp_path / "mountinfo"
    lines = [
        f"{n} 1 0:{n} / {p.replace(' ', chr(92) + '040')} rw - fuse.rclone remote: rw"
        for n, p in enumerate(points, start=30)
    ]
    table.write_text("".join(f"{line}\n" for line in lines))
    monkeypatch.setattr(tr, "_MOUNTINFO", table)
    return table


@pytest.mark.parametrize("where", ["", "/inner"])
def test_a_mount_point_at_or_below_the_target_is_kept(tmp_path, monkeypatch, where):
    # An rclone or sshfs mount owned by you passes every other check.
    tree = _home(tmp_path, "cloud drive")
    _mountinfo(tmp_path, monkeypatch, "/", f"{tree}{where}")
    outcome = _run(tree, tmp_path)
    assert (outcome.outcome, outcome.reason.split(" ")[0]) == (tr.KEPT, "outside-scope:")
    assert "mount point" in outcome.reason
    assert (tree / ".cache" / "kirocrew" / "pycache" / "x.pyc").is_file()


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x85", "\r", "\x0b", "\x0c", "\x1c"])
def test_a_mount_point_name_holding_a_line_boundary_is_still_found(tmp_path, monkeypatch, sep):
    # The kernel escapes only space, tab, LF and backslash in mountinfo.
    tree = _home(tmp_path, f"archive{sep}backup")
    _mountinfo(tmp_path, monkeypatch, "/", str(tree))
    assert _run(tree, tmp_path).reason == "outside-scope: a mount point"
    assert tree.is_dir()


@pytest.mark.parametrize("sep", ["\u2028", "\r", "\x85"])
def test_a_process_name_cannot_fake_its_uid(tmp_path, sep):
    # comm is the process's own choice and sits on the Name line above Uid.
    status = tmp_path / "status"
    status.write_bytes(f"Name:\tx{sep}Uid:\t0\t0\t0\t0\nUid:\t1000\t1000\t1000\t1000\n".encode())
    assert tr._status_uids(tmp_path) == (1000, 1000)


def test_proc_is_read_as_bytes_and_split_only_at_lf_or_nul():
    # Text mode turns CR into a line break and splitlines() cuts at U+2028 and
    # others the kernel leaves unescaped, so a name could forge a record.
    source = Path(tr.__file__).read_text(encoding="utf-8")
    assert ".read_text(" not in source and ".splitlines(" not in source


def test_a_target_on_another_device_than_its_temp_root_is_a_mount(tmp_path):
    tree = _home(tmp_path)
    other = os.stat(tmp_path).st_dev + 1
    assert tr._mount_doubt(tree, other, tmp_path) == "outside-scope: a mount point"


def test_an_unreadable_mount_table_keeps_the_target(tmp_path, monkeypatch):
    tree = _home(tmp_path)
    monkeypatch.setattr(tr, "_MOUNTINFO", tmp_path / "missing")
    assert _run(tree, tmp_path).reason == "unprovable: the mount table could not be read"


def test_a_bind_mount_made_during_the_removal_keeps_the_tree(tmp_path, monkeypatch):
    tree = _home(tmp_path)
    table = _mountinfo(tmp_path, monkeypatch, "/")

    def _mount_then_read() -> tr.ProcessView:
        table.write_text(
            table.read_text() + f"40 1 8:1 /data {tree}/.cache rw - ext4 /dev/sda1 rw\n"
        )
        return QUIET

    outcome = _run(tree, tmp_path, reread=_mount_then_read)
    assert outcome.reason == f"outside-scope: holds mount point {tree}/.cache"
    assert (tree / ".cache" / "kirocrew" / "pycache" / "x.pyc").is_file()


def test_a_socket_bound_after_the_checks_keeps_the_tree(tmp_path, monkeypatch):
    tree = _home(tmp_path)
    real = tr._pinned_scan

    def _scan(fd):
        # A server binds in the tree after the checks, just before the pinned walk.
        if not os.path.lexists(tree / "late.sock"):
            os.mknod(tree / "late.sock", 0o600 | stat.S_IFSOCK)
            stamp = NOW - 2 * tr.IDLE_SECONDS
            os.utime(tree / "late.sock", (stamp, stamp), follow_symlinks=False)
            os.utime(tree, (stamp, stamp))
        return real(fd)

    monkeypatch.setattr(tr, "_pinned_scan", _scan)
    assert _run(tree, tmp_path).reason == tr._SOCKET_REASON
    assert os.path.lexists(tree / "late.sock")


def test_a_file_opened_before_the_removal_is_kept(tmp_path):
    leftover = tmp_path / "worker.log"
    leftover.write_text("log")
    _age(leftover)
    late = tr.ProcessView(uses=((77, "worker", str(leftover)),), unreadable=0)
    outcome = _run(leftover, tmp_path, reread=lambda: late)
    assert outcome == tr.Outcome(str(leftover), tr.KEPT, "in-use: pid 77 (worker)")
    assert leftover.is_file()


def test_an_error_from_the_pinned_walk_keeps_the_path_and_the_run_goes_on(tmp_path, monkeypatch):
    broken, fine = _home(tmp_path, "broken"), _home(tmp_path, "fine")
    real = tr.pinned_fs.remove_tree_pinned

    def _walk(path, **kw):
        if path == str(broken):
            raise OSError("refusing a scanned directory on another device: 'mnt'")
        return real(path, **kw)

    monkeypatch.setattr(tr.pinned_fs, "remove_tree_pinned", _walk)
    one = functools.partial(tr.reclaim_one, now=NOW, roots=(str(tmp_path),), reread=lambda: QUIET)
    monkeypatch.setattr(tr, "reclaim_one", one)
    outcomes = tr.reclaim([str(broken), str(fine)], view=QUIET)
    assert [o.outcome for o in outcomes] == [tr.KEPT, tr.REMOVED]
    assert outcomes[0].reason.startswith("refused: refusing a scanned directory")
    assert broken.is_dir() and not fine.exists()


def test_a_sibling_name_prefix_is_not_use(tmp_path):
    tree = _home(tmp_path, "kc-home")
    view = tr.ProcessView(uses=((1, "sh", str(tmp_path / "kc-home2")),), unreadable=0)
    assert _run(tree, tmp_path, view).outcome == tr.REMOVED


@pytest.mark.parametrize(
    ("view", "reason"),
    [
        (tr.ProcessView(uses=(), unreadable=3), "unprovable: 3 processes unreadable"),
        (
            tr.ProcessView(uses=(), unreadable=1, unreadable_names=("ssh-agent 42",)),
            "unprovable: 1 process unreadable (ssh-agent 42)",
        ),
        (
            tr.ProcessView(uses=(), unreadable=0, available=False),
            "unprovable: no /proc",
        ),
    ],
)
def test_a_process_table_it_cannot_read_keeps_everything(tmp_path, view, reason):
    tree = _home(tmp_path)
    assert _run(tree, tmp_path, view).reason.startswith(reason)
    assert tree.is_dir()


def test_a_run_that_starts_before_the_removal_keeps_the_tree(tmp_path):
    tree = _home(tmp_path)
    late = tr.ProcessView(uses=((77, "pytest", str(tree)),), unreadable=0)
    outcome = _run(tree, tmp_path, reread=lambda: late)
    assert outcome == tr.Outcome(str(tree), tr.KEPT, "in-use: pid 77 (pytest)")
    assert (tree / ".cache" / "kirocrew" / "pycache" / "x.pyc").is_file()


def test_a_trailing_slash_does_not_follow_a_link(tmp_path):
    target = _home(tmp_path, "real")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert _run(f"{link}/", tmp_path).reason == "not a plain directory or file"
    assert target.is_dir()


# Fake pids above the kernel's largest pid_max (4194304), so none is this process.
_FAKE_PIDS = (9_000_001, 9_000_002, 9_000_003)


def _fake_proc(
    proc: Path,
    pid: int,
    *,
    uid: int,
    comm: str,
    env: bytes = b"",
    readable=True,
    cwd="/",
    maps: bytes = b"",
):
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (entry / "comm").write_text(f"{comm}\n")
    for link in ("root", "exe"):
        (entry / link).symlink_to("/")
    (entry / "cwd").symlink_to(cwd)
    if readable:
        (entry / "fd").mkdir()
    else:
        # listdir on a file fails the way EACCES does on a process it may not inspect.
        (entry / "fd").write_text("")
    (entry / "maps").write_bytes(maps)
    (entry / "environ").write_bytes(env)


def test_reading_a_process_table(tmp_path):
    proc, home = tmp_path / "proc", tmp_path / "home"
    (proc / "self").mkdir(parents=True)
    home.mkdir()
    me = os.getuid()
    mine, other, sealed = _FAKE_PIDS
    _fake_proc(proc, mine, uid=me, comm="pytest", env=f"HOME={home}/./\0LANG=C\0".encode())
    _fake_proc(proc, other, uid=me + 1, comm="other", env=f"HOME={home}\0".encode())
    _fake_proc(proc, sealed, uid=me, comm="sandboxed", readable=False)
    view = tr.read_processes(proc, uid=me)
    real_home = os.path.realpath(home)
    assert view.roots == ((mine, "pytest", real_home),)
    assert {pid for pid, _comm, _path in view.uses} == {mine}
    assert (view.unreadable, view.unreadable_names) == (1, (f"sandboxed {sealed}",))
    assert (view.not_inspected, view.unplaced) == ((), ())


def _one_process(tmp_path: Path, env: bytes, cwd: str = "/", maps: bytes = b"") -> tr.ProcessView:
    """What ``read_processes`` makes of a fake ``/proc`` holding one process of this user."""
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    _fake_proc(proc, _FAKE_PIDS[0], uid=os.getuid(), comm="worker", env=env, cwd=cwd, maps=maps)
    return tr.read_processes(proc, uid=os.getuid())


def test_a_lookup_root_that_is_not_utf8_still_names_its_tree(tmp_path):
    tree = _home(tmp_path, os.fsdecode(b"deps\xff"))
    view = _one_process(tmp_path, b"PYTHONPATH=" + os.fsencode(str(tree)) + b"\0")
    assert _run(tree, tmp_path, view).reason == f"in-use: pid {_FAKE_PIDS[0]} (worker)"
    assert tree.is_dir()


def test_a_mapped_file_whose_name_holds_a_newline_is_still_use(tmp_path):
    # The kernel prints a newline in a mapped path as \012.
    tree = _home(tmp_path, "build\nout")
    lib = os.fsencode(str(tree)).replace(b"\n", b"\\012") + b"/lib.so"
    view = _one_process(tmp_path, b"", maps=b"7f00-7f01 r-xp 00000000 08:01 42 " + lib + b"\n")
    assert _run(tree, tmp_path, view).reason == f"in-use: pid {_FAKE_PIDS[0]} (worker)"
    assert tree.is_dir()


def test_a_working_directory_named_like_a_deleted_one_is_still_use(tmp_path):
    # The kernel appends " (deleted)" to a removed target; a live directory can
    # carry the same text in its name, so both readings count.
    tree = _home(tmp_path, "run (deleted)")
    view = _one_process(tmp_path, b"", cwd=str(tree))
    assert _run(tree, tmp_path, view).reason == f"in-use: pid {_FAKE_PIDS[0]} (worker)"
    assert tree.is_dir()


def test_a_temp_directory_named_with_a_colon_is_in_use_and_its_prefix_is_not(tmp_path):
    tree, prefix = _home(tmp_path, "run:cache"), _home(tmp_path, "run")
    view = _one_process(tmp_path, f"TMPDIR={tree}\0".encode())
    assert _run(tree, tmp_path, view).reason == f"in-use: pid {_FAKE_PIDS[0]} (worker)"
    assert _run(prefix, tmp_path, view).outcome == tr.REMOVED


@pytest.mark.parametrize("key", ["PYTHONPATH", "HOME", "XDG_DATA_HOME", "UV_CACHE_DIR"])
def test_a_lookup_root_keeps_what_is_below_it(tmp_path, key):
    package = _home(tmp_path, "pkg")
    view = _one_process(tmp_path, f"{key}={tmp_path}\0".encode())
    assert _run(package, tmp_path, view).reason == f"in-use: pid {_FAKE_PIDS[0]} (worker)"
    assert package.is_dir()


def test_a_temp_directory_value_does_not_keep_what_is_below_it(tmp_path):
    # TMPDIR says where new files go, and it is often the temp root itself.
    tree = _home(tmp_path)
    view = _one_process(tmp_path, f"TMPDIR={tmp_path}\0".encode())
    assert _run(tree, tmp_path, view).outcome == tr.REMOVED


def test_a_tree_renamed_before_the_last_check_is_kept_whole(tmp_path):
    tree = _home(tmp_path)
    moved = tmp_path / "moved"

    def _rename_then_read() -> tr.ProcessView:
        tree.rename(moved)
        return QUIET

    outcome = _run(tree, tmp_path, reread=_rename_then_read)
    assert outcome == tr.Outcome(str(tree), tr.KEPT, "moved during the check")
    assert (moved / ".cache" / "kirocrew" / "pycache" / "x.pyc").is_file()


def test_the_last_mtime_read_walks_the_pinned_tree_not_the_name(tmp_path):
    tree = _home(tmp_path)
    fd = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        tree.rename(tmp_path / "moved")
        _home(tmp_path)  # an idle tree arrives at the checked name
        os.utime(tmp_path / "moved" / ".cache" / "kirocrew" / "pycache" / "x.pyc", (NOW, NOW))
        assert NOW - tr._pinned_scan(fd)[0] < tr.IDLE_SECONDS
    finally:
        os.close(fd)


def test_outside_the_temp_roots_is_kept(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tree = _home(tmp_path, "elsewhere")
    assert _run(tree, root).reason.startswith("outside-scope")
    assert _run(root, root).reason.startswith("outside-scope")


def test_a_link_is_never_followed(tmp_path):
    target = _home(tmp_path, "real")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert _run(link, tmp_path).reason == "not a plain directory or file"
    assert target.is_dir()


def test_a_tree_swapped_after_the_checks_is_not_removed(tmp_path):
    tree = _home(tmp_path)
    other = _home(tmp_path, "other")
    outcome = tr._remove(tree, os.lstat(other), now=NOW, reread=lambda: QUIET)
    assert outcome.outcome == tr.KEPT
    assert tree.is_dir() and other.is_dir()


def test_reading_proc_finds_a_child_working_in_the_tree(tmp_path):
    tree = _home(tmp_path)
    # Popen returns only once the child has exec'd, and the chdir happens before
    # exec, so one read already sees the working directory: no wait is needed.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tree)
    try:
        view = tr.read_processes()
        assert tr._use_of(str(tree), view).startswith(f"in-use: pid {child.pid}")
    finally:
        child.kill()
        child.wait()


def test_a_not_dumpable_process_is_skipped_and_named(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    # A made-up uid, never 0, so the root-owned entry differs from it even when
    # the test itself runs as root.
    me = 4_242_424
    agent = _FAKE_PIDS[0]
    _fake_proc(proc, agent, uid=me, comm="ssh-agent", readable=False)
    # The kernel gives root a not-dumpable process's /proc entry.
    monkeypatch.setattr(tr, "_dir_owner", lambda p: 0 if p.name == str(agent) else me)
    view = tr.read_processes(proc, uid=me)
    assert (view.unreadable, view.uses, view.not_inspected) == (0, (), (f"ssh-agent {agent}",))
    tree = _home(tmp_path)
    assert _run(tree, tmp_path, view).outcome == tr.REMOVED


def test_the_command_names_skipped_and_unreadable_processes(monkeypatch, capsys):
    view = tr.ProcessView(uses=(), unreadable=2, not_inspected=("ssh-agent 42",))
    monkeypatch.setattr(tr, "read_processes", lambda: view)
    monkeypatch.setattr(tr, "reclaim", lambda paths, dry_run, view: [])
    with pytest.raises(SystemExit):
        tr.run_command([], dry_run=True)
    err = capsys.readouterr().err
    assert "Not inspected: ssh-agent 42." in err
    assert "2 of your processes refused access" in err


@pytest.mark.parametrize(
    ("outcomes", "code"),
    [
        ([tr.Outcome("/tmp/a", tr.REMOVED), tr.Outcome("/tmp/b", tr.WOULD_REMOVE)], 0),
        ([tr.Outcome("/tmp/a", tr.REMOVED), tr.Outcome("/tmp/b", tr.KEPT, "recent: 2 min")], 1),
    ],
)
def test_the_command_exits_one_when_anything_was_kept(monkeypatch, capsys, outcomes, code):
    monkeypatch.setattr(tr, "read_processes", lambda: QUIET)
    monkeypatch.setattr(tr, "reclaim", lambda paths, dry_run, view: outcomes)
    with pytest.raises(SystemExit) as stop:
        tr.run_command(["/tmp/a", "/tmp/b"], dry_run=False)
    assert stop.value.code == code
    assert "/tmp/b" in capsys.readouterr().out
