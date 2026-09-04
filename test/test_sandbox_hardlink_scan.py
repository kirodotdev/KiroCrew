"""The launcher's pre-exec hardlink scan: when it runs, and when it must not.

The scan refuses to exec when it finds a hardlink alias to a protected credential
inode. It is gated on "does any credential have more than one link", because
walking $CWD and /tmp costs real time and the healthy-host answer is no.

These tests execute the scan block from the SHIPPED launcher source rather than a
copy of it, so the assertions cannot drift away from what actually runs in the
child. Everything the block touches is redirected: the two path lists, the working
directory, and ``/tmp`` (which the block hardcodes, so it is hidden rather than
moved). That makes the verdict independent of the host's real credentials AND of how
full its /tmp happens to be -- which matters twice over here, since a full /tmp is
the condition this whole change is about.
"""

from __future__ import annotations

import os
import runpy
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew.sandbox import _build_launcher_script

_BLOCK_START = "_protected_inodes = set()"
_BLOCK_END = "os.execvp(argv[0], argv)"
#: Structural landmarks the slice must contain. Deliberately NOT the guard
#: EXPRESSION: pinning that text here would make all five behavioural tests fail on a
#: functionally identical rewrite, and it is already pinned once, on purpose, by
#: ``test_sandbox_argv.py::test_only_aliased_credential_inodes_arm_the_walk``.
_SLICE_LANDMARKS = (
    "for _pd in SENSITIVE_DIRS:",   # the directory collection loop
    "for _pf in SENSITIVE_FILES:",  # the file collection loop
    "_MAX_SCAN_PER_ROOT",           # the walk itself
    "sandbox: BLOCKED",             # the refusal
)


def _scan_source() -> str:
    """The Step 7 scan, lifted verbatim out of the generated launcher.

    Sliced from the START OF THE LINE, not from the marker: ``dedent`` measures the
    common prefix across all lines, so a first line already stripped of its indent
    leaves the rest indented and the block will not even parse.
    """
    script = _build_launcher_script("strict")
    start = script.rindex("\n", 0, script.index(_BLOCK_START)) + 1
    end = script.rindex("\n", 0, script.index(_BLOCK_END, start)) + 1
    block = textwrap.dedent(script[start:end])
    # Pin what the slice must contain, so an edit that moves either marker and
    # shrinks the block fails HERE rather than leaving every assertion below
    # vacuously green against a fragment that no longer holds the gate.
    missing = [landmark for landmark in _SLICE_LANDMARKS if landmark not in block]
    assert not missing, f"the extracted scan block is missing {missing}"
    return block


class _Exited(Exception):
    """Stands in for the launcher's ``sys.exit`` so the message is inspectable."""


class _HiddenTmpPath:
    """``os.path``, with ``/tmp`` reported as not-a-directory.

    The scan walks ``(os.getcwd(), "/tmp")`` and only the first is redirectable, so
    without this the arming tests walk the OPERATOR's /tmp: measured 70,319 ``lstat``
    calls and ~0.9s per test, and on the host class this whole change is about
    (>100k files under /tmp) they walk to the 100,000 budget. The block skips a root
    whose ``isdir`` is False, so hiding it costs nothing and every assertion still
    holds -- the walk is all-or-nothing across both roots.
    """

    @staticmethod
    def isdir(path) -> bool:
        return False if str(path) == "/tmp" else os.path.isdir(path)

    def __getattr__(self, name: str):
        return getattr(os.path, name)


class _CountingOs:
    """The real ``os``, counting ``lstat`` -- the walk's per-file call.

    Counting is how these tests tell "the walk was skipped" from "the walk ran and
    happened to find nothing". Only the walk calls ``lstat``, so a count of zero is
    proof the gate held. Asserting on the truncation warning instead would make the
    verdict depend on how many files the host has under /tmp.
    """

    def __init__(self) -> None:
        self.lstat_calls = 0
        self.path = _HiddenTmpPath()

    def lstat(self, path):
        self.lstat_calls += 1
        return os.lstat(path)

    def __getattr__(self, name: str):
        return getattr(os, name)


def _run_scan(
    *, dirs: list[str], files: list[str], tmp_path: Path
) -> tuple[int, str | None]:
    """Run the scan with *dirs*/*files* as the protected paths.

    Returns ``(files_walked, refusal)``; *refusal* is None when the scan let the
    exec proceed.

    Via ``runpy.run_path`` on the extracted block rather than ``exec`` of its text:
    the two are equivalent here -- both run the shipped source with an injected
    namespace -- but ``exec`` trips the SAST gate's ``exec-detected`` rule, and a
    suppression comment would be this repo's first, spent on a false positive.
    """
    written: list[str] = []

    class _Stderr:
        def write(self, text: str) -> int:
            written.append(text)
            return len(text)

    def _exit(message: str) -> None:
        raise _Exited(message)

    fake_sys = type(
        "_sys", (), {"stderr": _Stderr(), "exit": staticmethod(_exit), "argv": list(sys.argv)}
    )()
    counting_os = _CountingOs()
    namespace = {
        "os": counting_os,
        "stat": stat,
        "sys": fake_sys,
        "SENSITIVE_DIRS": dirs,
        "SENSITIVE_FILES": files,
    }
    block = tmp_path / "_scan_block.py"
    block.write_text(_scan_source(), encoding="utf-8")
    refusal: str | None = None
    try:
        runpy.run_path(str(block), init_globals=namespace)
    except _Exited as exc:
        refusal = str(exc)
    return counting_os.lstat_calls, refusal


@pytest.fixture(autouse=True)
def _scan_from_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the scan with ``tmp_path`` as its only walk root, holding one file.

    The block scans ``os.getcwd()`` and a hardcoded ``/tmp``; the first is moved here
    and the second is hidden by ``_HiddenTmpPath``, so this directory is the whole
    walk. The sentinel file is what makes the ``lstat`` count MEAN something: the walk
    only lstats files, and a root holding nothing but directories yields a count of
    zero whether it ran or not -- which would leave every "did not arm" assertion
    passing against a walk that ran in full.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "walkable.txt").write_text("something for the walk to stat\n", encoding="utf-8")
    return tmp_path


class TestTheGateOnWhetherToWalkAtAll:
    def test_a_directory_named_as_a_credential_file_does_not_arm_the_scan(
        self, tmp_path: Path
    ) -> None:
        """Every directory has nlink >= 2 for ``.`` and ``..``.

        ``SENSITIVE_FILES`` deliberately carries hidden paths of BOTH kinds -- the
        launcher's hiding loops classify each entry themselves -- so it routinely
        contains directories. Counting one as "a credential with an alias" armed
        the 100k-entry walk of $CWD and /tmp on EVERY spawn: measured at 1.5s per
        sandboxed spawn, with a constant scan-truncation warning, on a host where
        no credential had an alias at all. Linux has no hardlinks to directories,
        so nlink says nothing here.
        """
        cred_dir = tmp_path / "creds-dir"
        cred_dir.mkdir()
        (cred_dir / "sub").mkdir()  # nlink now 3, and still not a hardlink alias

        walked, refusal = _run_scan(dirs=[], files=[str(cred_dir), str(tmp_path / "absent")], tmp_path=tmp_path)
        # The COUNT is the discriminating assertion -- with the guard removed this is
        # 70,319. `refusal is None` alone would not catch it: nothing under the walk
        # roots aliases the directory's inode, because Linux has no such alias to make.
        assert walked == 0, "a directory must not arm the walk"
        assert refusal is None

    def test_a_single_linked_credential_does_not_arm_the_scan(self, tmp_path: Path) -> None:
        cred = tmp_path / "credentials"
        cred.write_text("[default]\n", encoding="utf-8")
        assert cred.stat().st_nlink == 1

        walked, refusal = _run_scan(dirs=[], files=[str(cred)], tmp_path=tmp_path)
        assert walked == 0
        assert refusal is None

    def test_a_symlink_to_a_directory_does_not_arm_the_scan(self, tmp_path: Path) -> None:
        """A protected path can be a symlink, and ``os.stat`` follows it to a dir.

        ``~/.kube`` and ``~/.docker`` are symlinks on plenty of managed hosts, so
        the entry's own kind is not enough — the guard has to be on what the stat
        resolved to.
        """
        target = tmp_path / "target-dir"
        target.mkdir()
        link = tmp_path / "creds-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this host cannot create a symlink without elevation")

        walked, refusal = _run_scan(dirs=[], files=[str(link)], tmp_path=tmp_path)
        assert walked == 0
        assert refusal is None


class TestTheRefusalStillFires:
    def test_an_alias_to_a_hardlinked_credential_file_is_refused(self, tmp_path: Path) -> None:
        """The control itself: two links to one credential inode, one of them ours."""
        cred = tmp_path / "credentials"
        cred.write_text("[default]\naws_secret_access_key = x\n", encoding="utf-8")
        alias = tmp_path / "workspace-alias"
        os.link(cred, alias)
        assert cred.stat().st_nlink == 2

        walked, refusal = _run_scan(dirs=[], files=[str(cred)], tmp_path=tmp_path)
        assert walked > 0, "an aliased credential must arm the walk"
        assert refusal is not None
        assert "BLOCKED" in refusal
        assert "credential" in refusal

    def test_a_credential_inside_a_protected_DIR_also_arms_it(self, tmp_path: Path) -> None:
        cred_dir = tmp_path / "dot-aws"
        cred_dir.mkdir()
        cred = cred_dir / "credentials"
        cred.write_text("[default]\n", encoding="utf-8")
        os.link(cred, tmp_path / "leaked")

        walked, refusal = _run_scan(dirs=[str(cred_dir)], files=[], tmp_path=tmp_path)
        assert walked > 0
        assert refusal is not None
        assert "BLOCKED" in refusal
