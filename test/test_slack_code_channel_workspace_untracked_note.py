"""The Code tab's truncation note names the real reason a new file is missing.

Where the walk cannot be pinned (Windows) the gateway reads no untracked file
at all; the note must say the platform is why, never the size limit or a
credential, while those reasons keep their own wording.

Split from ``test_slack_code_channel_workspace`` (the file-line cap); the
harness, fixtures and fakes are that module's, imported by name so its tests
are not collected twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import test_slack_code_channel_workspace as _ws

from kiro_crew import pinned_fs
from kiro_crew.slack.code_channels.board import model
from kiro_crew.slack.code_channels.workspace import service
from kiro_crew.slack.code_channels.workspace.diff import (
    UNREAD,
    DiffGroup,
    assemble,
    assemble_groups,
    render_untracked,
    truncation_note,
)

Harness = _ws.Harness
_bound = _ws._bound
_git = _ws._git
# Fixtures, by name: pytest finds a fixture through the module's namespace.
_no_sandbox = _ws._no_sandbox
_feature_on = _ws._feature_on
audits = _ws.audits
_repo_template = _ws._repo_template
repo = _ws.repo

_PLATFORM = (
    "New files are not shown on this platform until they are added to git:"
    " ask the agent to `git add` them."
)
_SIZE = "1,000,000-byte view limit"
_CREDENTIAL = "may hold a credential"


def _note(h: Harness) -> str:
    return h.client.of("set_diff_view")[-1]["content"].split("\n", 1)[0]


def test_the_size_and_credential_wording_is_unchanged_when_nothing_was_unread():
    assert truncation_note(2) == (
        "Diff truncated: 2 file(s) shown. Other changes are not included: they are over "
        "Slack's 1,000,000-byte view limit, or were withheld because they may hold a "
        "credential.\n"
    )


def test_render_untracked_reads_nothing_where_the_walk_cannot_be_pinned(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hi\n", newline="\n")
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    assert render_untracked(str(tmp_path), "a.txt", budget=10_000) == UNREAD
    assert render_untracked(str(tmp_path), "a.txt", budget=1) == UNREAD


def test_an_unread_new_file_alone_names_only_the_platform():
    built = assemble(
        b"", tracked_truncated=False, untracked=[], untracked_truncated=False, untracked_unread=True
    )
    assert built.truncated and built.files == 0
    assert _PLATFORM in built.content
    assert _SIZE not in built.content and _CREDENTIAL not in built.content


def test_an_unread_new_file_beside_a_lost_one_names_both_reasons():
    built = assemble(
        b"", tracked_truncated=False, untracked=[], untracked_truncated=True, untracked_unread=True
    )
    assert _PLATFORM in built.content and _SIZE in built.content and _CREDENTIAL in built.content


def test_a_grouped_diff_names_the_platform_for_an_unread_group_only():
    tracked = b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    unread = DiffGroup(header="channel", tracked=tracked, untracked_unread=True)
    built = assemble_groups([unread])
    note = built.content.split("\n", 1)[0]
    assert built.truncated and built.files == 1 and "+b" in built.content
    assert _PLATFORM in note and _SIZE not in note
    lost = DiffGroup(header="worker", tracked=b"", tracked_truncated=True)
    note = assemble_groups([unread, lost]).content.split("\n", 1)[0]
    assert _PLATFORM in note and _SIZE in note
    assert assemble_groups([DiffGroup(header="w", tracked=b"", untracked_unread=True)]).truncated


def test_a_small_new_file_on_an_unpinned_platform_blames_the_platform(
    tmp_path, repo, audits, monkeypatch
):
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    (Path(rec.worktree) / "a.txt").write_text("hi\n", newline="\n")
    h.run(h.svc.refresh(binding))
    note = _note(h)
    assert note.startswith("Diff truncated: 0 file(s) shown.") and _PLATFORM in note
    assert _SIZE not in note and _CREDENTIAL not in note


def test_a_tracked_edit_beside_an_unread_new_file_keeps_its_section(
    tmp_path, repo, audits, monkeypatch
):
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    (tree / "README.md").write_text("changed\n", newline="\n")
    (tree / "a.txt").write_text("NEW-CONTENT\n", newline="\n")
    h.run(h.svc.refresh(binding))
    body = h.client.of("set_diff_view")[-1]["content"]
    note = _note(h)
    assert note.startswith("Diff truncated: 1 file(s) shown.") and _PLATFORM in note
    assert _SIZE not in note and "+changed" in body and "NEW-CONTENT" not in body


def test_a_work_board_diff_names_the_platform_for_the_channels_unread_new_file(
    tmp_path, repo, audits, monkeypatch
):
    """The grouped (work board) branch of ``_build_diff`` carries the flag too."""
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    h = Harness(tmp_path)
    _binding, rec = _bound(h, repo)
    worker_tree = tmp_path / "worker-wt"
    _git("worktree", "add", "-q", "-b", "feat/w1", str(worker_tree), "main", cwd=repo)
    (worker_tree / "login.py").write_text("WORKER = 1\n", newline="\n")
    _git("add", "login.py", cwd=worker_tree)
    _git("commit", "-q", "-m", "w1", cwd=worker_tree)
    (Path(rec.worktree) / "a.txt").write_text("NEW-CONTENT\n", newline="\n")
    row = model.WorkerRow(
        item_id="it_00000001", title="One", state=model.WORKING, branch="feat/w1", session_key="w1"
    )
    built = h.svc._build_diff(rec, model.BoardSnapshot(rows=(row,)), {"w1": str(worker_tree)})
    assert built is not None
    content = built[0]
    note = content.split("\n", 1)[0]
    assert note.startswith("Diff truncated: 1 file(s) shown.") and _PLATFORM in note
    assert _SIZE not in note and _CREDENTIAL not in note
    assert "+WORKER = 1" in content and "NEW-CONTENT" not in content


def test_a_credential_shaped_new_file_keeps_the_credential_wording_when_unpinned(
    tmp_path, repo, audits, monkeypatch
):
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    real = service.is_sensitive_path
    monkeypatch.setattr(service, "is_sensitive_path", lambda p: p.endswith("id_ed25519") or real(p))
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    (tree / "id_ed25519").write_text("PRIVATE KEY MATERIAL\n", newline="\n")
    (tree / "a.txt").write_text("hi\n", newline="\n")
    h.run(h.svc.refresh(binding))
    body = h.client.of("set_diff_view")[-1]["content"]
    note = _note(h)
    assert _CREDENTIAL in note and _PLATFORM in note and "PRIVATE KEY" not in body


def test_an_oversized_tracked_change_keeps_the_size_wording_when_unpinned(
    tmp_path, repo, audits, monkeypatch
):
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    for i in range(3):
        (tree / f"big{i}.txt").write_text(
            "".join(f"line {n} {'x' * 60}\n" for n in range(9000)), newline="\n"
        )
    _git("add", ".", cwd=tree)
    h.run(h.svc.refresh(binding))
    note = _note(h)
    assert _SIZE in note and _PLATFORM not in note


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="no dir_fd support on this platform"
)
def test_a_small_new_file_where_the_walk_is_pinned_renders_with_no_note(tmp_path, repo, audits):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    (Path(rec.worktree) / "a.txt").write_text("hi\n", newline="\n")
    h.run(h.svc.refresh(binding))
    body = h.client.of("set_diff_view")[-1]["content"]
    assert body.startswith("diff --git a/a.txt b/a.txt\nnew file mode 100644")
    assert "Diff truncated" not in body
