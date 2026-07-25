"""Tests for api_file_diff handler in dashboard/handlers/files.py."""

from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers.files import api_file_diff

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _req(path: str = "") -> make_mocked_request:
    """Create a mocked GET request with ?path= query param."""
    url = f"/api/file-diff?path={path}" if path else "/api/file-diff"
    req = make_mocked_request("GET", url)
    return req


def _mock_sel():
    sel = MagicMock()
    sel.log_api_access = MagicMock()
    return sel


@pytest.mark.asyncio
async def test_empty_path_returns_empty():
    """No path param returns empty diff and original."""
    req = _req("")
    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"diff": "", "original": ""}


@pytest.mark.asyncio
async def test_nonexistent_file_returns_empty():
    """Non-existent file outside any repo returns empty diff (status not_git).

    Missing files are no longer rejected up front — a tracked-but-deleted path
    must reach git to produce its deletion patch. A missing file that git does
    not know about falls out as an empty not_git result.
    """
    req = _req("/tmp/nonexistent_file_abc123.txt")
    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["diff"] == ""
    assert body["original"] == ""


@pytest.mark.asyncio
async def test_sensitive_path_returns_403():
    """Sensitive paths are rejected with 403."""
    req = _req("/home/user/.ssh/id_rsa")
    with patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True), \
         patch("kiro_crew.dashboard.handlers.files.os.path.isfile", return_value=True), \
         patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = await api_file_diff(req)
    assert resp.status == 403
    body = json.loads(resp.body)
    assert body["error"] == "Access denied"


@pytest.mark.asyncio
async def test_file_not_in_git_repo(tmp_path):
    """File outside a git repo returns not_git status."""
    f = tmp_path / "standalone.txt"
    f.write_text("hello")
    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        req = _req(str(f))
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "not_git"
    assert body["diff"] == ""
    assert body["original"] == ""


@pytest.mark.asyncio
@requires_git
async def test_clean_file_in_git_repo(tmp_path):
    """Committed file with no changes returns clean status."""
    # Set up a real git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    f = tmp_path / "clean.txt"
    f.write_text("original content")
    subprocess.run(["git", "add", "clean.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        req = _req(str(f))
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "clean"
    assert body["diff"] == ""
    assert body["original"] == "original content"


@pytest.mark.asyncio
@requires_git
async def test_modified_file_in_git_repo(tmp_path):
    """Modified file returns diff and original content."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    f = tmp_path / "modified.txt"
    f.write_text("original")
    subprocess.run(["git", "add", "modified.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    # Modify the file
    f.write_text("modified content")

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        req = _req(str(f))
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "modified"
    assert body["diff"] != ""
    assert "modified content" in body["diff"]
    assert body["original"] == "original"


@pytest.mark.asyncio
@requires_git
async def test_untracked_file_in_git_repo(tmp_path):
    """Untracked file returns untracked status with a synthesized all-added patch."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    # Need at least one commit for HEAD to exist
    (tmp_path / "init.txt").write_text("x")
    subprocess.run(["git", "add", "init.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    # Create untracked file
    f = tmp_path / "untracked.txt"
    f.write_text("new file content")

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        req = _req(str(f))
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "untracked"
    # difflib-synthesized patch (portable — no /dev/null git invocation).
    assert "+new file content" in body["diff"]
    assert "@@" in body["diff"]
    assert body["original"] == ""


@pytest.mark.asyncio
@requires_git
async def test_deleted_file_returns_deletion_patch(tmp_path):
    """A tracked file deleted from the working tree returns its deletion patch."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    f = tmp_path / "doomed.txt"
    f.write_text("soon gone")
    subprocess.run(["git", "add", "doomed.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.unlink()

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        req = _req(str(f))
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "deleted"
    assert "-soon gone" in body["diff"]
    assert body["original"] == "soon gone"


@pytest.mark.asyncio
@requires_git
async def test_textconv_hardening(tmp_path):
    """Git commands use textconv/filter hardening flags."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    f = tmp_path / "test.txt"
    f.write_text("content")
    subprocess.run(["git", "add", "test.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text("changed")

    calls = []
    orig_run = subprocess.run

    def spy_run(cmd, **kwargs):
        calls.append(cmd)
        return orig_run(cmd, **kwargs)

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()), \
         patch("kiro_crew.dashboard.handlers.files.subprocess.run", side_effect=spy_run):
        req = _req(str(f))
        resp = await api_file_diff(req)

    assert resp.status == 200
    # Find the diff command and verify hardening flags
    diff_cmds = [c for c in calls if "diff" in c and "HEAD" in c]
    assert len(diff_cmds) >= 1
    diff_cmd = diff_cmds[0]
    assert "-c" in diff_cmd
    assert "diff.textconv=" in diff_cmd
    assert "core.attributesFile=/dev/null" in diff_cmd


@pytest.mark.asyncio
@requires_git
async def test_git_env_nosystem(tmp_path):
    """GIT_ATTR_NOSYSTEM=1 is set in environment for git diff commands."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    f = tmp_path / "test.txt"
    f.write_text("content")
    subprocess.run(["git", "add", "test.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text("changed")

    envs = []
    orig_run = subprocess.run

    def spy_run(cmd, **kwargs):
        if "env" in kwargs:
            envs.append(kwargs["env"])
        return orig_run(cmd, **kwargs)

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()), \
         patch("kiro_crew.dashboard.handlers.files.subprocess.run", side_effect=spy_run):
        req = _req(str(f))
        await api_file_diff(req)

    # At least one call should have GIT_ATTR_NOSYSTEM
    assert any(e.get("GIT_ATTR_NOSYSTEM") == "1" for e in envs)


@pytest.mark.asyncio
async def test_timeout_returns_not_git(tmp_path):
    """Subprocess timeout returns not_git status."""
    f = tmp_path / "timeout.txt"
    f.write_text("content")

    def timeout_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()), \
         patch("kiro_crew.dashboard.handlers.files.subprocess.run", side_effect=timeout_run), \
         patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=False):
        req = _req(str(f))
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "not_git"


@pytest.mark.asyncio
async def test_sel_audit_logging_on_success(tmp_path):
    """SEL audit log is called on successful access."""
    f = tmp_path / "audit.txt"
    f.write_text("content")

    mock_sel = _mock_sel()
    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=mock_sel), \
         patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=False), \
         patch("kiro_crew.dashboard.handlers.files.subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
        req = _req(str(f))
        await api_file_diff(req)

    mock_sel.log_api_access.assert_called_once()
    call_kwargs = mock_sel.log_api_access.call_args
    assert call_kwargs[1]["operation"] == "file_diff"
    assert call_kwargs[1]["outcome"] == "allowed"
