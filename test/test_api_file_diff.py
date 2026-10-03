"""Tests for api_file_diff handler in dashboard/handlers/files.py."""

from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.handlers import files as files_mod
from kiro_crew.dashboard.handlers.files import api_file_diff
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security.redaction import redact_credentials

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

# A documented placeholder, not a real credential: the redactor matches it anyway.
PLACEHOLDER = "lp_your_token_here"
BEARER_LINE = '"Authorization": "Bearer ' + PLACEHOLDER + '"'
# `redact` composes credential redaction with exfiltration-URL redaction, so the
# tests pin one fixture per pass; a fixture both passes match could not tell which
# one is still wired up. A long opaque query is the discriminator: the exfil pass
# masks it and the credential pass leaves it alone. A bare URL is not -- that pass
# masks a URL only when it carries an exfil signal.
EXFIL_URL = "https://collect.example.com/p?d=" + "A" * 900


def _req(path: str = "") -> make_mocked_request:
    """Create a mocked GET request with ?path= query param."""
    url = f"/api/file-diff?path={path}" if path else "/api/file-diff"
    req = make_mocked_request("GET", url, app=as_owner(web.Application()))
    # The owner: /api/file-diff is owner-gated, and these tests are about the
    # diff, not the gate.
    req["user"] = "local-app"
    req["app"] = ""
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
    """Non-existent file returns empty diff and original."""
    req = _req("/tmp/nonexistent_file_abc123.txt")
    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"diff": "", "original": ""}


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
async def test_file_not_in_git_repo(tmp_path, monkeypatch):
    """File outside a git repo returns not_git status."""
    f = tmp_path / "standalone.txt"
    f.write_text("hello")
    # "Outside a git repo" is a property of the fixture, not of where pytest
    # keeps its temp root: a `TMPDIR` under a checkout lets git's upward
    # discovery find THAT repository and answer "untracked". The handler
    # inherits the environment, so git's own ceiling stops the walk above
    # `tmp_path` (the ceiling entry itself is never descended into).
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
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
    """Untracked file returns untracked status with diff."""
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
    assert body["diff"] != ""
    assert body["original"] == ""


@pytest.mark.asyncio
@requires_git
async def test_failed_git_diff_reports_error_not_clean(tmp_path):
    """A non-zero `git diff` exit must surface as status "error", never "clean".

    Mapping the failure to an empty diff would fall through to the clean
    branch, reporting a git failure to the user as "no changes" — a false
    negative on a question people act on.
    """
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    f = tmp_path / "committed.txt"
    f.write_text("original content")
    subprocess.run(["git", "add", "committed.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)

    orig_run = subprocess.run

    def failing_diff_run(cmd, **kwargs):
        # Fail exactly the `git diff HEAD -- <path>` invocation; everything
        # else (rev-parse preflight, `git show` for the baseline) stays real.
        if "diff" in cmd and "HEAD" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr="fatal: boom")
        return orig_run(cmd, **kwargs)

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()), \
         patch("kiro_crew.dashboard.handlers.files.subprocess.run", side_effect=failing_diff_run):
        req = _req(str(f))
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "error"
    assert body["diff"] == ""
    # The baseline read succeeded before the diff failed; keep what we have.
    assert body["original"] == "original content"


@pytest.mark.asyncio
@requires_git
async def test_empty_repo_file_is_untracked_not_error(tmp_path):
    """A freshly-initialized repo with no commits keeps the untracked verdict.

    `git diff HEAD` exits 128 there (`fatal: bad revision 'HEAD'`), which is the
    dominant real-world non-zero exit — the untracked probe must claim the file
    before the failure branch turns a healthy repo into a false "git failed".
    """
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    f = tmp_path / "first.txt"
    f.write_text("the very first file, no commit yet")

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        req = _req(str(f))
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "untracked"
    assert "the very first file" in body["diff"]


@pytest.mark.asyncio
@requires_git
async def test_timeout_after_preflight_reports_error_not_not_git(tmp_path):
    """A timeout past the repository preflight must not masquerade as not_git.

    The client renders not_git as "there is no baseline" — a statement about
    the file. A slow repo timing out on `git diff` is a computation failure.
    """
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    f = tmp_path / "slow.txt"
    f.write_text("content")
    subprocess.run(["git", "add", "slow.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)

    orig_run = subprocess.run

    def slow_diff_run(cmd, **kwargs):
        if "diff" in cmd and "HEAD" in cmd:
            raise subprocess.TimeoutExpired(cmd, 10)
        return orig_run(cmd, **kwargs)

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()), \
         patch("kiro_crew.dashboard.handlers.files.subprocess.run", side_effect=slow_diff_run):
        req = _req(str(f))
        resp = await api_file_diff(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "error"


@pytest.mark.asyncio
@requires_git
async def test_git_output_is_decoded_as_utf8(tmp_path):
    """Every git text subprocess opts out of the Windows locale code page."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "init.txt").write_text("x")
    subprocess.run(["git", "add", "init.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    target = tmp_path / "unicode.txt"
    target.write_text("こんにちは\n", encoding="utf-8")

    calls = []
    original_run = subprocess.run

    def spy_run(cmd, **kwargs):
        calls.append(kwargs)
        return original_run(cmd, **kwargs)

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()), \
         patch("kiro_crew.dashboard.handlers.files.subprocess.run", side_effect=spy_run):
        response = await api_file_diff(_req(str(target)))

    body = json.loads(response.body)
    assert body["status"] == "untracked"
    assert "こんにちは" in body["diff"]
    assert calls
    assert all(call.get("text") is True for call in calls)
    assert all(call.get("encoding") == "utf-8" for call in calls)
    assert all(call.get("errors") == "replace" for call in calls)


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


def _git_repo(tmp_path):
    """Initialise a committable git repo in *tmp_path*."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)


def _credential_line(text: str) -> str:
    """The single line of *text* mentioning the bearer header."""
    return next(line for line in text.splitlines() if "Authorization" in line or "REDACTED" in line)


@pytest.mark.asyncio
@requires_git
async def test_head_content_is_redacted_like_the_panel_buffer(tmp_path):
    """An unchanged credential line must not render as a diff hunk.

    The file panel's diff view puts ``api_file_read``'s already-redacted buffer
    beside this endpoint's ``original`` and diffs the two strings itself. Serving
    HEAD raw therefore invents a hunk on a line nobody touched, and leaks a
    secret committed in HEAD that ``/api/file-read`` masks.
    """
    _git_repo(tmp_path)
    f = tmp_path / "config.json"
    f.write_text(BEARER_LINE + "\nkeep\n")
    subprocess.run(["git", "add", "config.json"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    # Edit an unrelated line, which is what opens the panel's diff view.
    f.write_text(BEARER_LINE + "\nchanged\n")

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = await api_file_diff(_req(str(f)))
    body = json.loads(resp.body)

    assert body["status"] == "modified"
    assert PLACEHOLDER not in body["original"]
    assert "[REDACTED: credential]" in body["original"]
    # Both panes agree on the untouched line, so it renders as unchanged.
    assert _credential_line(body["original"]) == _credential_line(redact(f.read_text()))


@pytest.mark.asyncio
@requires_git
async def test_untracked_diff_is_redacted(tmp_path):
    """The untracked branch's ``diff`` is the whole file, so it is redacted too."""
    _git_repo(tmp_path)
    (tmp_path / "init.txt").write_text("x")
    subprocess.run(["git", "add", "init.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f = tmp_path / "new.json"
    f.write_text(BEARER_LINE + "\n")

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = await api_file_diff(_req(str(f)))
    body = json.loads(resp.body)

    assert body["status"] == "untracked"
    assert PLACEHOLDER not in body["diff"]
    assert "[REDACTED: credential]" in body["diff"]


@pytest.mark.asyncio
@requires_git
async def test_exfiltration_url_is_redacted_in_both_fields(tmp_path):
    """The exfiltration-URL pass is wired up on both fields, not just credentials.

    Both fields go through one `redact` shim that composes two passes. A
    bearer-only fixture cannot tell them apart, so either field could regress to
    credential-only while a URL an agent could be steered into leaked through.
    """
    _git_repo(tmp_path)
    f = tmp_path / "notes.md"
    f.write_text(EXFIL_URL + "\nkeep\n")
    subprocess.run(["git", "add", "notes.md"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text(EXFIL_URL + "\nchanged\n")

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = await api_file_diff(_req(str(f)))
    body = json.loads(resp.body)

    assert body["status"] == "modified"
    assert EXFIL_URL not in body["original"]
    assert EXFIL_URL not in body["diff"]
    assert "[REDACTED: suspicious URL to collect.example.com (long query, may be legitimate)]" in body["original"]
    # This fixture is a discriminator only while the credential pass ignores it.
    assert redact_credentials(EXFIL_URL)[0] == EXFIL_URL


@pytest.mark.asyncio
@requires_git
async def test_a_credential_straddling_the_read_cap_is_still_masked(tmp_path):
    """Neither field may be truncated before the redaction pass.

    Slicing first cuts the tail a credential pattern needs to match, and the
    surviving prefix is then served as real bytes. The offset is arrangeable by
    whoever writes the file, so this pins the ordering rather than the cap: the
    pass sees whole text, and truncating afterwards is the only safe order.
    """
    _git_repo(tmp_path)
    f = tmp_path / "padded.txt"
    # Places the token across the byte offset the sibling endpoint caps at.
    secret = "AKIAIOSFODNN7EXAMPLE"
    padding = "a" * (files_mod._FILE_READ_CAP - 10)
    f.write_text(padding + secret + "\nkeep\n")
    subprocess.run(["git", "add", "padded.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text(padding + secret + "\nchanged\n")

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = await api_file_diff(_req(str(f)))
    body = json.loads(resp.body)

    assert body["status"] == "modified"
    # Neither the whole token nor a prefix of it long enough to be the secret.
    assert secret not in body["original"]
    assert secret[:14] not in body["original"]
    assert "[REDACTED: credential]" in body["original"]
