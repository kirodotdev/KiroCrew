"""The pin scan reads each mount view once (``sandbox_mount_sweep._MountViews``).

A task's mountinfo is rendered from its mount namespace and its root, so tasks
that share both are shown one table. These tests build fake proc trees whose
tasks carry that structure the way procfs does -- ``ns/mnt`` a hard link to one
file per namespace, ``root`` a symlink to one directory per root -- and pin that
the scan reads one task per view, still reads every task whose view differs or
does not resolve, and answers exactly what it answers with views turned off.
"""

from __future__ import annotations

import builtins
import errno
import os
import sys
from pathlib import Path

import pytest

from kiro_crew import platform_compat, sandbox_mount_sweep
from kiro_crew.sandbox import _mount_pinned_source_names, _PinScanCoverage

# The trees model namespaces and roots with hard links and symlinks; the sweep they
# exercise only runs where the Linux namespace launcher does.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links model the views")

PIN_A = "kirocrew_sb_4194305_alpha"
PIN_B = "kirocrew_sb_4194305_bravo"


def _line(mount_id: int, name: str, target: str) -> str:
    return f"{mount_id} 99 0:40 /{name} {target} rw - tmpfs tmpfs rw\n"


TABLE_A = _line(100, PIN_A, "/root/a")
TABLE_AB = TABLE_A + _line(101, PIN_B, "/root/b")


@pytest.fixture
def views_resolve(tmp_path: Path) -> None:
    """Skip where a path's mount identity is unknown (not Linux, or Linux < 5.8)."""
    if platform_compat.mount_identity(str(tmp_path)) is None:
        pytest.skip("statx does not report a mount id here")


class _Tree:
    """A fake proc tree whose tasks share namespaces and roots on purpose."""

    def __init__(self, base: Path) -> None:
        self.proc = base / "proc"
        self._ns = base / "namespaces"
        self._roots = base / "roots"
        for d in (self.proc, self._ns, self._roots):
            d.mkdir()
        self.task("1", table="", ns="init", root="/")

    def _ns_file(self, name: str) -> Path:
        path = self._ns / name
        if not path.exists():
            path.write_text("")
        return path

    def _root_dir(self, name: str) -> Path:
        path = self._roots / name.strip("/").replace("/", "_")
        path.mkdir(exist_ok=True)
        return path

    def task(
        self,
        rel: str,
        *,
        table: str | None,
        ns: str | None = None,
        root: str | None = None,
    ) -> Path:
        """Plant ``proc/<rel>``; ``table=None`` reads as a departed task, and a
        missing ``ns`` or ``root`` as a view that does not resolve."""
        d = self.proc / rel
        d.mkdir(parents=True, exist_ok=True)
        if table is not None:
            (d / "mountinfo").write_text(table)
        if ns is not None:
            (d / "ns").mkdir()
            os.link(self._ns_file(ns), d / "ns" / "mnt")
        if root is not None:
            os.symlink(self._root_dir(root), d / "root")
        return d

    def group(self, pid: str, *, table: str, ns: str, root: str, threads: int) -> None:
        self.task(pid, table=table, ns=ns, root=root)
        self.task(f"{pid}/task/{pid}", table=table, ns=ns, root=root)
        for tid in range(int(pid) + 1, int(pid) + 1 + threads):
            self.task(f"{pid}/task/{tid}", table=table, ns=ns, root=root)


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every mountinfo the scan opens, in order."""
    opened: list[str] = []
    real_open = builtins.open

    def _open(file, *args, **kwargs):  # noqa: ANN001, ANN202
        if str(file).endswith("/mountinfo"):
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _open)
    return opened


def _scan(tree: _Tree) -> tuple[set[str], bool, bool]:
    coverage = _PinScanCoverage()
    pinned, complete = _mount_pinned_source_names(str(tree.proc), coverage=coverage)
    return pinned, complete, coverage.covered


def _scan_without_views(
    tree: _Tree, monkeypatch: pytest.MonkeyPatch
) -> tuple[set[str], bool, bool]:
    with monkeypatch.context() as patch:
        patch.setattr(sandbox_mount_sweep, "_mount_view", lambda _task_path: None)
        return _scan(tree)


@pytest.mark.usefixtures("views_resolve")
class TestOneReadPerView:
    def test_tasks_that_share_a_view_are_read_once(self, tmp_path: Path, reads: list[str]):
        tree = _Tree(tmp_path)
        tree.group("500", table=TABLE_AB, ns="sandbox", root="/", threads=20)
        tree.group("600", table=TABLE_AB, ns="sandbox", root="/", threads=3)

        assert _scan(tree) == ({PIN_A, PIN_B}, True, True)
        # pid 1's view and the sandbox view, one read each.
        assert len(reads) == 2

    def test_a_task_chrooted_inside_the_namespace_is_read_on_its_own(
        self, tmp_path: Path, reads: list[str]
    ):
        """A mount outside a chrooted root is left out of that task's table, so
        a narrower root is a different view even in the same namespace. The
        leader is read before its threads, so the narrower table comes first
        and a key on the namespace alone would lose ``PIN_B``."""
        tree = _Tree(tmp_path)
        tree.task("500", table=TABLE_A, ns="sandbox", root="/jail")
        tree.task("500/task/500", table=TABLE_A, ns="sandbox", root="/jail")
        tree.task("500/task/501", table=TABLE_AB, ns="sandbox", root="/")

        assert _scan(tree) == ({PIN_A, PIN_B}, True, True)
        assert sum(r.endswith("/task/501/mountinfo") for r in reads) == 1

    def test_a_thread_in_another_namespace_is_read(self, tmp_path: Path, reads: list[str]):
        tree = _Tree(tmp_path)
        tree.task("500", table="", ns="host", root="/")
        tree.task("500/task/500", table="", ns="host", root="/")
        tree.task("500/task/501", table=TABLE_A, ns="sandbox", root="/")

        assert _scan(tree) == ({PIN_A}, True, True)
        assert any(r.endswith("/task/501/mountinfo") for r in reads)

    def test_a_task_whose_view_does_not_resolve_is_read_every_time(
        self, tmp_path: Path, reads: list[str]
    ):
        """No ``ns/mnt`` (another user's task, a non-dumpable one) or no root
        (a task past ``exit_fs``) answers no view, and such a task is read."""
        tree = _Tree(tmp_path)
        tree.task("500", table=TABLE_A)
        tree.task("600", table=TABLE_A, ns="sandbox")
        tree.task("700", table=TABLE_A, root="/")

        assert _scan(tree) == ({PIN_A}, True, True)
        assert sorted(Path(r).parent.name for r in reads) == ["1", "500", "600", "700"]

    def test_a_view_whose_read_failed_is_not_recorded(
        self, tmp_path: Path, reads: list[str], monkeypatch: pytest.MonkeyPatch
    ):
        """The leader's read fails (``EINVAL``, as for a zombie leader), so the
        sibling sharing its view is still read and its pin still counts."""
        tree = _Tree(tmp_path)
        leader = tree.task("500", table=TABLE_A, ns="sandbox", root="/")
        tree.task("500/task/501", table=TABLE_A, ns="sandbox", root="/")
        real_open = builtins.open
        target = str(leader / "mountinfo")

        def _einval(file, *args, **kwargs):  # noqa: ANN001, ANN202
            if str(file) == target:
                raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), target)
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _einval)

        pinned, _complete, _covered = _scan(tree)

        assert pinned == {PIN_A}
        assert any(r.endswith("/task/501/mountinfo") for r in reads)

    def test_a_task_that_left_its_view_during_the_read_records_nothing(
        self, tmp_path: Path, reads: list[str], monkeypatch: pytest.MonkeyPatch
    ):
        """A view is recorded only if the task still shows it once its table
        was read; one that moved meanwhile leaves the next task to be read."""
        tree = _Tree(tmp_path)
        tree.task("500", table=TABLE_A, ns="sandbox", root="/")
        tree.task("500/task/501", table=TABLE_A, ns="sandbox", root="/")
        real_view = sandbox_mount_sweep._mount_view
        leader = str(tree.proc / "500")
        asked: list[str] = []

        def _moving(task_path: str):  # noqa: ANN202
            asked.append(task_path)
            if task_path == leader and asked.count(leader) > 1:
                return None  # after its read the leader shows a different view
            return real_view(task_path)

        monkeypatch.setattr(sandbox_mount_sweep, "_mount_view", _moving)

        assert _scan(tree) == ({PIN_A}, True, True)
        assert any(r.endswith("/task/501/mountinfo") for r in reads)

    def test_past_the_held_view_limit_every_task_is_read(
        self, tmp_path: Path, reads: list[str], monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(sandbox_mount_sweep, "_MOUNT_VIEW_MAX_HELD", 0)
        tree = _Tree(tmp_path)
        tree.group("500", table=TABLE_A, ns="sandbox", root="/", threads=2)

        assert _scan(tree) == ({PIN_A}, True, True)
        # pid 1, the leader, and its two threads (the leader's own tid is skipped).
        assert len(reads) == 4

    @pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
    def test_no_descriptor_outlives_the_scan(self, tmp_path: Path):
        tree = _Tree(tmp_path)
        tree.group("500", table=TABLE_A, ns="sandbox", root="/", threads=2)
        tree.group("600", table=TABLE_AB, ns="other", root="/", threads=1)
        before = sorted(os.listdir("/proc/self/fd"))

        _scan(tree)

        assert sorted(os.listdir("/proc/self/fd")) == before


@pytest.mark.usefixtures("views_resolve")
@pytest.mark.parametrize(
    "shape",
    ["shared", "chrooted", "thread_unshared", "unresolved", "departed", "zombie_leader"],
)
def test_views_answer_what_reading_every_task_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
):
    tree = _Tree(tmp_path)
    tree.group("500", table=TABLE_A, ns="sandbox", root="/", threads=4)
    if shape == "shared":
        tree.group("600", table=TABLE_A, ns="sandbox", root="/", threads=2)
    elif shape == "chrooted":
        tree.task("600", table=TABLE_A, ns="wide", root="/jail")
        tree.task("600/task/600", table=TABLE_A, ns="wide", root="/jail")
        tree.task("600/task/601", table=TABLE_AB, ns="wide", root="/")
    elif shape == "thread_unshared":
        tree.task("600", table="", ns="host", root="/")
        tree.task("600/task/601", table=TABLE_AB, ns="private", root="/")
    elif shape == "unresolved":
        tree.task("600", table=TABLE_AB)
    elif shape == "departed":
        tree.task("600", table=None, ns="sandbox", root="/")
        tree.task("700", table=TABLE_AB, ns="late", root="/")
    elif shape == "zombie_leader":
        tree.task("600", table=TABLE_A)
        tree.task("600/task/601", table=TABLE_AB, ns="sandbox2", root="/")
        real_open = builtins.open
        target = str(tree.proc / "600" / "mountinfo")

        def _einval(file, *args, **kwargs):  # noqa: ANN001, ANN202
            if str(file) == target:
                raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), target)
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _einval)

    assert _scan(tree) == _scan_without_views(tree, monkeypatch)


def test_the_scan_still_works_where_no_view_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reads: list[str]
):
    """Off Linux, or on a kernel without mount ids, every task is read."""
    monkeypatch.setattr(platform_compat, "mount_identity", lambda *_a, **_k: None)
    tree = _Tree(tmp_path)
    tree.group("500", table=TABLE_A, ns="sandbox", root="/", threads=1)

    assert _scan(tree) == ({PIN_A}, True, True)
    # pid 1, the leader, and its thread (the leader's own tid is skipped).
    assert len(reads) == 3
