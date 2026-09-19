"""Fail before tests when a Windows runner would lose real filesystem coverage."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from kiro_crew import platform_compat, windows_acl


def check_paths() -> None:
    sid = platform_compat.current_user_sid()
    assert sid, "Windows process token identity is unavailable"
    print(f"token_sid={sid} elevated={bool(ctypes.windll.shell32.IsUserAnAdmin())}")
    for key in ("GITHUB_WORKSPACE", "RUNNER_TEMP"):
        with tempfile.TemporaryDirectory(prefix="windows-ci-", dir=os.environ[key]) as tmp:
            root = Path(tmp)
            target = root / "probe.txt"
            target.write_text("probe", encoding="utf-8")
            # _allocator_owner requires exact token ownership BEFORE lockdown.
            owner = windows_acl.describe(target).owner_sid
            assert owner == sid, f"{key}: file owner_sid={owner!r}, token_sid={sid!r}"
            platform_compat.restrict_to_owner(target)
            assert target.read_text(encoding="utf-8") == "probe"
            link = root / "probe-link.txt"
            link.symlink_to(target)
            assert link.read_text(encoding="utf-8") == "probe"
            link.unlink()
            target.rename(root / "renamed.txt")
        assert not root.exists(), f"{key}: probe directory cleanup failed"
        print(f"{key}: owner, ACL, write, symlink, rename, cleanup OK")


def main() -> None:
    assert sys.platform == "win32", "Tests require native Windows"
    assert sys.version_info[:2] == (3, 12), "Tests require Python 3.12"
    print(f"Python {sys.version.split()[0]}; Windows {sys.getwindowsversion().build}")
    # These tools otherwise cause test skips. gh calls in the backend are stubbed,
    # so gh is deliberately not a prerequisite. No image/package changes here.
    for tool in ("git", "uv", "jq", "node"):
        subprocess.run([tool, "--version"], check=True, timeout=30)
    subprocess.run(
        ["node", "-e", "if (Number(process.versions.node.split('.')[0]) < 22) process.exit(1)"],
        check=True,
        timeout=30,
    )
    check_paths()


if __name__ == "__main__":
    main()
