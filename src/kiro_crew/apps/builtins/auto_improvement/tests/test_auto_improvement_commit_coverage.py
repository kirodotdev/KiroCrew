"""Refusal paths of the one-click commit that never reach git.

``materialize_queued_diff`` refuses an empty queued diff before it resolves a base, and
``commit_finding`` refuses a queued finding when no repository is configured. Both are
pure decisions on the inputs, so each test asserts the exact returned refusal and that
no clone was touched.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod


@pytest.mark.parametrize("diff_text", ["", "   \n\t  "])
def test_materialize_refuses_blank_diff_without_touching_clone(tmp_path, diff_text):
    clone = tmp_path / "never-created-clone"

    out = commit_mod.materialize_queued_diff(
        clone=clone, branch="work", config={}, diff_text=diff_text
    )

    assert out == {"ok": False, "error": "the queued diff is empty"}
    assert not clone.exists()


def test_commit_finding_refuses_when_no_repository_is_configured(tmp_path, monkeypatch):
    fp = "abc123def4567890"
    queue = tmp_path / "pr_queue"
    queue.mkdir()
    (queue / f"{fp}.diff").write_text("--- a/x\n+++ b/x\n", encoding="utf-8")

    monkeypatch.setattr(commit_mod.store, "pr_queue_dir", lambda: queue)
    monkeypatch.setattr(commit_mod.store, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(
        commit_mod.store,
        "read_json",
        lambda _path, default=None: {"clone": "   ", "branch": "main"},
    )

    out = commit_mod.commit_finding(fp)

    assert out == {"ok": False, "error": "no repository configured"}


def test_commit_finding_refuses_when_no_queued_change_exists(tmp_path, monkeypatch):
    queue = tmp_path / "pr_queue"
    queue.mkdir()
    monkeypatch.setattr(commit_mod.store, "pr_queue_dir", lambda: queue)

    out = commit_mod.commit_finding("0123456789abcdef")

    assert out == {"ok": False, "error": "no queued change for fingerprint 0123456789abcdef"}
