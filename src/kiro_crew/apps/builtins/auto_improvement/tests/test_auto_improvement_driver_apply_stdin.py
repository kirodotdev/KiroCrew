"""The loop-side ``git apply`` calls hand git the diff's exact bytes on every OS.

Text-mode stdin writes each ``\\n`` as ``os.linesep``, so on Windows git read CRLF patch
lines that an LF working tree never matches, so every kept winner and bug fix would fail
with "did not apply". Both call sites send bytes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import driver as drv
from kiro_crew.apps.builtins.auto_improvement.spine import driver_commit

DIFF = "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n line1\n-line2\n+line2 changed\n line3\n"


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def info(self, msg, *args):
        self.lines.append(("info", msg % args))

    def error(self, msg, *args):
        self.lines.append(("error", msg % args))


def _driver(clone: Path) -> SimpleNamespace:
    return SimpleNamespace(clone=clone, branch="work", log=_Log())


@pytest.mark.parametrize("stage", ["_stage_winner", "_stage_bug_winner"])
def test_apply_stdin_is_the_diffs_exact_bytes(tmp_path, monkeypatch, stage):
    seen: list[dict] = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs)
        # Fail the first apply so the bug path's --3way retry is captured too.
        rc = 1 if len(seen) == 1 else 0
        err = "error: patch failed"
        if isinstance(kwargs.get("input"), bytes):
            err = err.encode()  # type: ignore[assignment]  # a run without text mode answers in bytes
        return subprocess.CompletedProcess(argv, rc, err[:0], err)

    # Patched through the driver facade, which mirrors both names onto driver_commit.
    monkeypatch.setattr(drv, "_git", lambda *a, **k: None)
    monkeypatch.setattr(drv, "subprocess", SimpleNamespace(run=fake_run))

    getattr(driver_commit, stage)(_driver(tmp_path), SimpleNamespace(diff=DIFF))

    assert seen
    for kwargs in seen:
        assert kwargs["input"] == DIFF.encode("utf-8", "surrogateescape")
        assert b"\r\n" not in kwargs["input"]
        assert not kwargs.get("text") and "encoding" not in kwargs


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
@pytest.mark.parametrize("stage", ["_stage_winner", "_stage_bug_winner"])
def test_lf_diff_applies_to_an_lf_working_tree(tmp_path, monkeypatch, stage):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "t")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "t@t")

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "work")
    git("config", "core.autocrlf", "false")
    (tmp_path / "a.txt").write_bytes(b"line1\nline2\nline3\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    driver = _driver(tmp_path)

    assert getattr(driver_commit, stage)(driver, SimpleNamespace(diff=DIFF)) is True

    assert (tmp_path / "a.txt").read_bytes() == b"line1\nline2 changed\nline3\n"
    # The plain apply landed: no failure line, no --3way retry.
    assert driver.log.lines == []
