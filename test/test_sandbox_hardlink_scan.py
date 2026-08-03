"""Regression tests for the sandbox pre-exec hardlink scan (issue #646).

The scan is the sole control against a hardlink planted outside the namespace
that keeps a credential inode reachable after the bind-mount hides its original
path. Before the fix it used a single shared file budget: exhausting it (trivial
on any long-lived host with a large ``/tmp``) silently fell through to ``exec``
as if the scan had passed, and because the agent CWD was walked first a large
worktree could consume the whole budget and leave ``/tmp`` unscanned.

These tests exercise ``_scan_credential_hardlinks`` directly with tiny trees and
a small injectable budget, so the exhaustion path is covered WITHOUT creating
thousands of files, and assert that budget exhaustion is now DISTINGUISHABLE
from a clean scan (a non-empty ``truncated_roots``) and that each root gets its
own independent budget.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox

# _build_launcher_script() reads os.getuid()/os.getgid(), which do not exist on
# Windows. Production never reaches the launcher off Linux, so building it
# elsewhere tests nothing. The direct _scan_credential_hardlinks tests above
# stay unmarked: they are pure os.walk/lstat and must run everywhere.
_linux_only = pytest.mark.skipif(
    sys.platform != "linux",
    reason="launcher script generation uses Linux-only APIs (os.getuid/getgid)",
)


def _mk_files(directory: Path, count: int, prefix: str = "f") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (directory / f"{prefix}{i}").write_text("x")


def _inode_key(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


class TestHardlinkScan:
    def test_clean_scan_returns_no_links_and_no_truncation(self, tmp_path: Path) -> None:
        _mk_files(tmp_path / "root", 3)
        dangerous, truncated = sandbox._scan_credential_hardlinks(
            [str(tmp_path / "root")], {(1, 1)}, max_scan=100
        )
        assert dangerous == []
        assert truncated == []

    def test_detects_hardlink_to_protected_inode(self, tmp_path: Path) -> None:
        secret = tmp_path / "secret"
        secret.write_text("creds")
        link = tmp_path / "root" / "planted"
        (tmp_path / "root").mkdir()
        os.link(secret, link)  # nlink now 2, same inode as secret
        dangerous, truncated = sandbox._scan_credential_hardlinks(
            [str(tmp_path / "root")], {_inode_key(secret)}, max_scan=100
        )
        assert dangerous == [str(link)]
        assert truncated == []

    def test_budget_exhaustion_is_reported_not_silent(self, tmp_path: Path) -> None:
        # 5 files, budget 3 -> the scan is incomplete and MUST say so.
        root = tmp_path / "root"
        _mk_files(root, 5)
        dangerous, truncated = sandbox._scan_credential_hardlinks([str(root)], {(1, 1)}, max_scan=3)
        assert dangerous == []
        assert truncated == [(str(root), 3)]  # examined exactly the budget

    def test_exactly_full_budget_is_not_truncated(self, tmp_path: Path) -> None:
        # Exactly ``max_scan`` files must count as a COMPLETE scan (no false
        # truncation), else every spawn near the boundary would warn spuriously.
        root = tmp_path / "root"
        _mk_files(root, 4)
        _, truncated = sandbox._scan_credential_hardlinks([str(root)], {(1, 1)}, max_scan=4)
        assert truncated == []

    def test_per_root_budget_large_cwd_does_not_starve_tmp(self, tmp_path: Path) -> None:
        # The core bug: a large first root (agent CWD) consumed the shared
        # budget so the second root (/tmp) was never scanned. With per-root
        # budgets, a dangerous hardlink in the SECOND root is still found even
        # though the first root exhausts its own budget.
        big_cwd = tmp_path / "cwd"
        _mk_files(big_cwd, 5)  # exceeds max_scan below
        tmp_root = tmp_path / "tmp"
        tmp_root.mkdir()
        secret = tmp_path / "secret"
        secret.write_text("creds")
        planted = tmp_root / "planted"
        os.link(secret, planted)

        dangerous, truncated = sandbox._scan_credential_hardlinks(
            [str(big_cwd), str(tmp_root)], {_inode_key(secret)}, max_scan=3
        )
        assert str(planted) in dangerous  # /tmp still scanned
        assert (str(big_cwd), 3) in truncated  # CWD truncated on its OWN budget
        assert all(root != str(tmp_root) for root, _ in truncated)  # /tmp complete

    def test_missing_root_is_skipped(self, tmp_path: Path) -> None:
        dangerous, truncated = sandbox._scan_credential_hardlinks(
            [str(tmp_path / "does-not-exist")], {(1, 1)}, max_scan=10
        )
        assert dangerous == []
        assert truncated == []

    def test_depth_limit_prunes_deep_trees(self, tmp_path: Path) -> None:
        # A file 7 levels deep is beyond the depth-5 prune and is not examined.
        deep = tmp_path / "root" / "a" / "b" / "c" / "d" / "e" / "f" / "g"
        deep.mkdir(parents=True)
        (deep / "buried").write_text("x")
        _, truncated = sandbox._scan_credential_hardlinks(
            [str(tmp_path / "root")], {(1, 1)}, max_scan=100
        )
        assert truncated == []  # pruned, budget never approached


class TestLauncherEmbedsScan:
    @_linux_only
    def test_launcher_injects_scan_and_escalates_on_truncation(self) -> None:
        script = sandbox._build_launcher_script("strict")
        compile(script, "<launcher>", "exec")  # must be valid Python
        assert "\ndef _scan_credential_hardlinks(" in script  # injected verbatim
        assert "hardlink scan INCOMPLETE" in script  # loud escalate, not silent
        assert str(sandbox._HARDLINK_SCAN_BUDGET) in script
        # The old silent shared-counter implementation is gone.
        assert "_MAX_SCAN" not in script
        assert "_scan_count" not in script
