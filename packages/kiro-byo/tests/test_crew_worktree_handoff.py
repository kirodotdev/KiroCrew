"""Crew 0.12 worktrees and session handoff: BYO must not silently change cwd.

Source-only regression: no claim this tests an actual Crew handoff or git checkout.
"""
from pathlib import Path

import pytest

from kiro_byo.acp_server import AcpServer


def test_session_restores_only_its_original_worker_worktree(tmp_path: Path):
    original = tmp_path / "mate-A-worktree"
    other = tmp_path / "mate-B-worktree"
    original.mkdir()
    other.mkdir()
    cwd, directories = AcpServer._session_paths_from_params({"cwd": str(original)})
    assert cwd == str(original.resolve()) and directories == ()

    with pytest.raises(ValueError, match="persisted session root"):
        AcpServer._session_paths_from_params({"cwd": str(other)}, expected_cwd=cwd)

    resumed, extras = AcpServer._session_paths_from_params({"cwd": str(original)}, expected_cwd=cwd)
    assert resumed == cwd and extras == ()


def test_session_restore_fails_closed_when_checkout_removed(tmp_path: Path):
    original = tmp_path / "mate-worktree"
    original.mkdir()
    saved = str(original.resolve())
    original.rmdir()
    with pytest.raises(ValueError, match="unavailable"):
        AcpServer._session_paths_from_params({}, expected_cwd=saved)


def test_session_handoff_does_not_inherit_prior_additional_roots(tmp_path: Path):
    main = tmp_path / "worktree"
    extra = tmp_path / "external-dir"
    main.mkdir()
    extra.mkdir()
    first, roots = AcpServer._session_paths_from_params({
        "cwd": str(main), "additionalDirectories": [str(extra)]
    })
    assert roots == (str(extra.resolve()),)
    resumed, roots_after = AcpServer._session_paths_from_params({"cwd": str(main)}, expected_cwd=first)
    assert resumed == first and roots_after == ()
