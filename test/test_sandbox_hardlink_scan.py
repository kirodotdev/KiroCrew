"""The launcher's pre-exec hardlink scan: when it runs, and when it must not.

The scan refuses to exec when it finds a hardlink alias to a protected credential
inode. It is gated on "does any credential have more than one link", because
walking $CWD and /tmp costs real time and the healthy-host answer is no.

These tests call the shipped stage, ``refuse_hardlinked_credentials`` in
``kiro_crew.sandbox_launcher_program``, on a ``Launch`` whose protected lists name
paths under ``tmp_path``, and hand it ``tmp_path`` as its only walk root -- so the
verdict is independent of the host's real credentials AND of how full its /tmp
happens to be, which matters twice over here, since a full /tmp is the condition
this whole gate is about.

Whether the walk RAN is read off the scan's own output: given a budget of zero
files, a walk that starts reports itself truncated at the first file it meets, and
the root always holds one. That tells "the walk was skipped" from "the walk ran and
happened to find nothing" without counting anything inside the stage.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_sandbox_launcher_program import RecordingLibc, launch, payload, refusal

from kiro_crew import sandbox_launcher_program

program = sandbox_launcher_program

#: A walk budget no root here comes near, so a walk that runs reaches every alias.
_FULL_BUDGET = 100000


def _truncated(root: Path) -> str:
    """The warning a walk of *root* prints once it has used a zero-file budget."""
    return f"pre-exec hardlink scan truncated at 0 files in {root}"


@pytest.fixture(autouse=True)
def _walkable(tmp_path: Path) -> Path:
    """Put one file in ``tmp_path``, the only root these walks are given.

    The sentinel is what makes "the walk did not run" MEAN something: the scan's
    budget counts files, and a root holding nothing but directories reports no
    truncation whether it was walked or not -- which would leave every "did not arm"
    assertion passing against a walk that ran in full.
    """
    (tmp_path / "walkable.txt").write_text("something for the walk to stat\n", encoding="utf-8")
    return tmp_path


def _scan(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    *,
    dirs: list[str],
    files: list[str],
    alias_ids: list[tuple[int, int]] | None = None,
    budget: int = _FULL_BUDGET,
) -> tuple[str | None, str]:
    """Run the scan with *dirs*/*files* as the protected paths, walking ``tmp_path``.

    *alias_ids* are the ``(device, inode)`` pairs the PARENT collected, which is the
    only form a credential one level below a masked root can reach the child in --
    the level the ``sensitive_dirs`` walk stops above. Empty by default, which is what
    a caller that pins no credential supplies.

    Returns ``(refusal, stderr)``; *refusal* is None when the scan let the exec
    proceed.
    """
    run = launch(
        tmp_path,
        payload(
            sensitive_dirs=dirs,
            sensitive_files=files,
            alias_credential_ids=[list(pair) for pair in (alias_ids or ())],
        ),
        libc=RecordingLibc(),
    )
    capfd.readouterr()
    message = refusal(program.refuse_hardlinked_credentials, run, [str(tmp_path)], budget)
    return message, capfd.readouterr().err


class TestTheGateOnWhetherToWalkAtAll:
    def test_a_directory_named_as_a_credential_file_does_not_arm_the_scan(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """Every directory has nlink >= 2 for ``.`` and ``..``.

        ``sensitive_files`` deliberately carries hidden paths of BOTH kinds -- the
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

        refused, err = _scan(
            tmp_path,
            capfd,
            dirs=[],
            files=[str(cred_dir), str(tmp_path / "absent")],
            budget=0,
        )
        # The WALK is the discriminating assertion. `refused is None` alone would not
        # catch it: nothing under the walk root aliases the directory's inode, because
        # Linux has no such alias to make.
        assert _truncated(tmp_path) not in err, "a directory must not arm the walk"
        assert refused is None

    def test_a_single_linked_credential_does_not_arm_the_scan(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        cred = tmp_path / "credentials"
        cred.write_text("[default]\n", encoding="utf-8")
        assert cred.stat().st_nlink == 1

        refused, err = _scan(tmp_path, capfd, dirs=[], files=[str(cred)], budget=0)
        assert _truncated(tmp_path) not in err
        assert refused is None

    def test_a_symlink_to_a_directory_does_not_arm_the_scan(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
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

        refused, err = _scan(tmp_path, capfd, dirs=[], files=[str(link)], budget=0)
        assert _truncated(tmp_path) not in err
        assert refused is None

    def test_an_armed_scan_walks_the_working_directory_by_default(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The child walks where the agent will work: its working directory, then /tmp.

        The control for the zero-budget reading above, too: an armed scan given no
        roots of its own walks the working directory and reports the truncation the
        disarmed cases must not.
        """
        cred = tmp_path / "credentials"
        cred.write_text("[default]\n", encoding="utf-8")
        os.link(cred, tmp_path / "second-name")
        monkeypatch.chdir(tmp_path)
        run = launch(tmp_path, payload(sensitive_files=[str(cred)]), libc=RecordingLibc())
        capfd.readouterr()

        refused = refusal(program.refuse_hardlinked_credentials, run, None, 0)

        assert refused is None, "a scan cut short by its budget degrades open"
        assert _truncated(Path(os.getcwd())) in capfd.readouterr().err


class TestTheRefusalStillFires:
    def test_an_alias_to_a_hardlinked_credential_file_is_refused(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """The control itself: two links to one credential inode, one of them ours."""
        cred = tmp_path / "credentials"
        cred.write_text("[default]\naws_secret_access_key = x\n", encoding="utf-8")
        alias = tmp_path / "workspace-alias"
        os.link(cred, alias)
        assert cred.stat().st_nlink == 2

        refused, _ = _scan(tmp_path, capfd, dirs=[], files=[str(cred)])
        assert refused is not None, "an aliased credential must arm the walk"
        assert "BLOCKED" in refused
        assert "credential" in refused
        assert str(alias) in refused

    def test_a_credential_inside_a_protected_DIR_also_arms_it(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        cred_dir = tmp_path / "dot-aws"
        cred_dir.mkdir()
        cred = cred_dir / "credentials"
        cred.write_text("[default]\n", encoding="utf-8")
        os.link(cred, tmp_path / "leaked")

        refused, _ = _scan(tmp_path, capfd, dirs=[str(cred_dir)], files=[])
        assert refused is not None
        assert "BLOCKED" in refused
        assert str(tmp_path / "leaked") in refused

    def test_an_aliased_per_app_secret_one_level_down_is_refused(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """The masked tree's own depth-1 walk stops ABOVE this credential.

        ``apps/<app>/.app_secret`` is a bearer credential one directory below the tree a
        cron spawn masks, so naming the tree as a protected DIR leaves it unseen -- the
        collection walk there reads only the root's own files. An alias to it at a path no
        mask covers is read through that path, so the scan is what has to answer it.

        Break-arm: ``drop_the_alias_scan_root_loop``.
        """
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        secret = apps / "alpha" / ".app_secret"
        secret.write_text("bearer", encoding="utf-8")
        os.link(secret, tmp_path / "leaked-secret")
        assert secret.stat().st_nlink == 2

        # The control FIRST: naming the tree the way the mask does finds nothing, which is
        # why the inodes have to be supplied separately.
        refused, err = _scan(tmp_path, capfd, dirs=[str(apps)], files=[], budget=0)
        assert _truncated(tmp_path) not in err, "the depth-1 walk cannot reach one level down"
        assert refused is None

        secret_st = secret.stat()
        refused, _ = _scan(
            tmp_path,
            capfd,
            dirs=[str(apps)],
            files=[],
            alias_ids=[(secret_st.st_dev, secret_st.st_ino)],
        )
        assert refused is not None
        assert "BLOCKED" in refused
        assert str(tmp_path / "leaked-secret") in refused

    def test_the_credential_inodes_arm_the_walk_with_the_tree_unreadable(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """The case a tree-reading collection cannot pass: the tree is GONE by now.

        In production this child masks the apps tree in its own process, binding an empty
        directory over it, so anything it stats under that path reports ENOENT. A pass that
        collected here would arm on nothing and the walk would never run, while a test that
        pointed it at a readable directory still went green. Supplying inodes the parent read
        is what survives the crossing, and removing the tree is how that is asserted.
        """
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        secret = apps / "alpha" / ".app_secret"
        secret.write_text("bearer", encoding="utf-8")
        os.link(secret, tmp_path / "leaked-secret")
        secret_st = secret.stat()
        # Stand in for the mask: the path the child would read does not resolve, while the
        # credential inode is untouched and keeps its second name -- which is exactly what
        # an empty bind over the tree leaves behind.
        (apps / "alpha").rename(tmp_path / "hidden-alpha")
        assert not (apps / "alpha" / ".app_secret").exists()
        assert (tmp_path / "leaked-secret").stat().st_nlink == 2

        refused, _ = _scan(
            tmp_path,
            capfd,
            dirs=[],
            files=[],
            alias_ids=[(secret_st.st_dev, secret_st.st_ino)],
        )

        assert refused is not None, "the walk still arms: the inode came from the parent"
        assert "BLOCKED" in refused
        assert str(tmp_path / "leaked-secret") in refused

    def test_no_supplied_inode_leaves_the_walk_skipped(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """A healthy host pays nothing: the parent found no second name, so nothing arms."""
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / "alpha" / ".app_secret").write_text("bearer", encoding="utf-8")

        refused, err = _scan(tmp_path, capfd, dirs=[], files=[], alias_ids=[], budget=0)

        assert _truncated(tmp_path) not in err
        assert refused is None
