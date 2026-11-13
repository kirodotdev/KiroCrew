"""The app's fetches reap orphaned temp packs even when the fetch times out.

A fetch killed by a subprocess timeout leaves its in-progress ``tmp_pack_*`` behind.
The clone setup's branch checkout and the one-click commit's materialize both reap
through core's ``kiro_crew.git_tmp_packs``, and the reap must run on the timeout path.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.git_tmp_packs import TMP_PACK_MAX_AGE_S

_OLD = 10 * TMP_PACK_MAX_AGE_S  # comfortably past any threshold under test


def _plant(pack_dir: Path, name: str, *, age_s: float = 0.0, size: int = 8) -> Path:
    pack_dir.mkdir(parents=True, exist_ok=True)
    target = pack_dir / name
    target.write_bytes(b"\0" * size)
    os.utime(target, (time.time() - age_s, time.time() - age_s))
    return target


def _plain_clone(tmp_path: Path) -> tuple[Path, Path]:
    clone = tmp_path / "clone"
    pack_dir = clone / ".git" / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    return clone, pack_dir


class TestFetchTimeoutCallSites:
    """A fetch killed by its timeout raises ``TimeoutExpired``; the reap must still run.

    The timeout is the case that leaves an orphan, so a reap that only runs when
    the fetch returns would miss every orphan these sites produce.
    """

    @staticmethod
    def _timeout(args: Any, **kwargs: Any) -> Any:
        import subprocess

        if "fetch" in args:
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout") or 0)
        return subprocess.CompletedProcess(args, 0, "", "")

    def test_clone_setup_checkout_branch_reaps_on_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup

        clone, pack_dir = _plain_clone(tmp_path)
        old = _plant(pack_dir, "tmp_pack_killed", age_s=_OLD)
        monkeypatch.setattr(clone_setup, "require_pinned", lambda *_a, **_k: None)
        monkeypatch.setattr(clone_setup, "hook_off_args", lambda *_a, **_k: [])
        monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _c: True)
        monkeypatch.setattr(clone_setup, "_push_disabled", lambda _c: True)
        monkeypatch.setattr(clone_setup.subprocess, "run", self._timeout)
        with pytest.raises(subprocess.TimeoutExpired):
            clone_setup.checkout_branch(clone, "feature/x")
        assert not old.exists(), "a timed-out fetch must not skip the reap"

    def test_commit_materialize_reaps_on_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.backend import commit

        clone, pack_dir = _plain_clone(tmp_path)
        old = _plant(pack_dir, "tmp_pack_killed", age_s=_OLD)
        monkeypatch.setattr(commit, "resolve_origin_url", lambda _c: "https://example.invalid/r")
        monkeypatch.setattr(commit, "_prefer_authenticated_remote", lambda u: u)
        monkeypatch.setattr(commit, "_git", lambda _clone, *args, **kw: self._timeout(args, **kw))
        with pytest.raises(subprocess.TimeoutExpired):
            commit.materialize_queued_diff(
                clone=clone, branch="main", config={}, diff_text="diff --git a/x b/x\n"
            )
        assert not old.exists(), "a timed-out fetch must not skip the reap"
