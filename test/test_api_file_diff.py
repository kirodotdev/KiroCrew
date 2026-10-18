"""Tests for api_file_diff handler in dashboard/file_api/editor.py (served by handlers/files.py)."""

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
    assert "[REDACTED: suspicious URL to collect.example.com]" in body["original"]
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


# --- The served patch is a diff of masked bodies, never a masked raw patch. ---
#
# Every value below is synthetic: seeded noise in a credential's shape, which the
# redactor matches but which authenticates nowhere.

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_ALNUM = _B64[:62]


def _noise(rng, alphabet: str, n: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def _fragments(secret: str, width: int = 8) -> set[str]:
    """Every *width*-char window of *secret*: what a split could leave behind."""
    return {secret[i:i + width] for i in range(len(secret) - width + 1)}


def _assert_no_fragment(body: dict, secrets: list[str]) -> None:
    served = json.dumps(body)
    for secret in secrets:
        leaked = sorted(f for f in _fragments(secret) if f in served)
        assert not leaked, f"credential fragment served: {leaked[:3]}"


def _commit_then_write(tmp_path, name: str, old: str, new: str):
    _git_repo(tmp_path)
    f = tmp_path / name
    f.write_text(old)
    subprocess.run(["git", "add", name], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text(new)
    return f


def _pem(kind: str, body: list[str]) -> str:
    """A key block in armour assembled at run time, so no literal sits here."""
    label = f"{kind} " + "PRI" + "VATE " + "KEY" if kind else "PRI" + "VATE " + "KEY"
    dash = "-" * 5
    return f"{dash}BEGIN {label}{dash}\n" + "\n".join(body) + f"\n{dash}END {label}{dash}\n"


@pytest.mark.asyncio
@requires_git
@pytest.mark.parametrize("kind", ["RSA", "OPENSSH", ""])
@pytest.mark.parametrize("edited", [3, 12, 21])
async def test_an_edit_inside_a_key_block_serves_no_key_text(tmp_path, kind, edited):
    """An edit inside a key block serves no part of the key.

    Each edited line puts the hunk somewhere else in the block: near its start,
    its middle and its end.
    """
    import random
    rng = random.Random(f"{kind}-{edited}")
    body = [_noise(rng, _B64, 64) for _ in range(25)]
    new_body = list(body)
    new_body[edited] = _noise(rng, _B64, 64)
    filler = "\n".join(f"line {i}" for i in range(20)) + "\n"
    f = _commit_then_write(
        tmp_path, "key.txt", filler + _pem(kind, body) + filler, filler + _pem(kind, new_body) + filler
    )

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        body_json = json.loads((await api_file_diff(_req(str(f)))).body)

    assert body_json["status"] == "modified"
    _assert_no_fragment(body_json, body + new_body)


@pytest.mark.asyncio
@requires_git
@pytest.mark.parametrize("shape", ["github", "aws"])
async def test_a_long_line_above_a_hunk_serves_no_token_text(tmp_path, shape):
    """A long line above a hunk serves no part of a token it holds."""
    import random
    rng = random.Random(shape)
    # Prefixes are joined at run time so no literal token shape sits here.
    if shape == "github":
        token = "gh" + "p_" + _noise(rng, _ALNUM, 36)
    else:
        token = "AK" + "IA" + _noise(rng, _ALNUM[:36].upper(), 16)
    assert redact_credentials(token)[0] != token, "fixture must be a shape the redactor masks"
    line = "x" * (80 - len(" = ") - 8) + " = " + token
    rows = [line] + [f"  row {i}" for i in range(30)]
    old = "\n".join(rows) + "\n"
    f = _commit_then_write(tmp_path, "cfg.txt", old, old.replace("  row 20\n", "  row 20 edited\n"))

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        body = json.loads((await api_file_diff(_req(str(f)))).body)

    assert body["status"] == "modified"
    assert "row 20 edited" in body["diff"]
    _assert_no_fragment(body, [token])


@pytest.mark.asyncio
@requires_git
async def test_an_untracked_key_is_masked_whole(tmp_path):
    """The untracked branch diffs the masked body too, not git's raw patch."""
    import random
    rng = random.Random("untracked")
    _git_repo(tmp_path)
    (tmp_path / "init.txt").write_text("x")
    subprocess.run(["git", "add", "init.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    body = [_noise(rng, _B64, 64) for _ in range(10)]
    f = tmp_path / "new.pem"
    f.write_text(_pem("RSA", body))

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = json.loads((await api_file_diff(_req(str(f)))).body)

    assert resp["status"] == "untracked"
    assert resp["diff"].startswith("diff --git")
    assert "--- /dev/null" in resp["diff"]
    _assert_no_fragment(resp, body)


@pytest.mark.asyncio
@requires_git
async def test_a_failed_masked_diff_serves_no_patch(tmp_path):
    """If diffing the masked bodies fails, the raw patch is never the fallback."""
    f = _commit_then_write(tmp_path, "a.txt", "one\n", "two\n")
    orig_run = subprocess.run

    def failing_no_index(cmd, **kwargs):
        if "--no-index" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=2, stdout="", stderr="boom")
        return orig_run(cmd, **kwargs)

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()), \
         patch("kiro_crew.dashboard.handlers.files.subprocess.run", side_effect=failing_no_index):
        body = json.loads((await api_file_diff(_req(str(f)))).body)

    assert body["status"] == "error"
    assert body["diff"] == ""


@pytest.mark.asyncio
@requires_git
async def test_a_refused_checked_open_serves_no_patch(tmp_path):
    """The working file is read through the checked, no-follow open.

    A refusal there (a path swapped for a link after the first check) ends in
    ``error`` with no patch, never in a plain read of whatever is there now.
    """
    f = _commit_then_write(tmp_path, "a.txt", "one\n", "two\n")
    denied = files_mod._OpenDenied("symlink_refused", str(f))

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()), \
         patch("kiro_crew.dashboard.handlers.files._open_checked_file", return_value=denied) as opened:
        body = json.loads((await api_file_diff(_req(str(f)))).body)

    opened.assert_called_once()
    assert body["status"] == "error"
    assert body["diff"] == ""


@pytest.mark.asyncio
@requires_git
async def test_a_crlf_file_diffs_only_the_edited_line(tmp_path):
    """HEAD comes through git's text-mode stdout, so the file is read the same way."""
    _git_repo(tmp_path)
    f = tmp_path / "win.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    subprocess.run(["git", "-c", "core.autocrlf=false", "add", "win.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.write_bytes(b"a\r\nB\r\nc\r\n")

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        body = json.loads((await api_file_diff(_req(str(f)))).body)

    changed = [ln for ln in body["diff"].splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    assert changed == ["-b", "+B"]


@pytest.mark.asyncio
@requires_git
async def test_a_form_feed_line_stays_one_patch_line(tmp_path):
    """Only "\\n" ends a patch line; a form feed inside one does not split it."""
    f = _commit_then_write(tmp_path, "ff.txt", "a\n\x0cb\nc\n", "a\n\x0cb\nC\n")

    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        body = json.loads((await api_file_diff(_req(str(f)))).body)

    lines = body["diff"].split("\n")
    first = next(i for i, ln in enumerate(lines) if ln.startswith("@@"))
    assert lines[first + 1:] == [" a", " \x0cb", "-c", "+C"]


@pytest.mark.asyncio
@requires_git
@pytest.mark.parametrize("tracked", [False, True])
async def test_binary_diff_does_not_open_working_file(tmp_path, monkeypatch, tracked):
    """Binary changes keep a redacted summary without opening the working body."""
    name = "fixture-name.bin"
    if tracked:
        f = _commit_then_write(tmp_path, name, "old\0", "new\0")
    else:
        _git_repo(tmp_path)
        f = tmp_path / name
    f.write_bytes(b"new\0" * 16)
    monkeypatch.setattr(files_mod, "_FILE_DIFF_MAX_BYTES", 8)
    monkeypatch.setattr(files_mod, "redact", lambda text: text.replace(name, "masked.bin"))

    with patch.object(files_mod, "_sel", return_value=_mock_sel()), \
         patch.object(files_mod, "_open_checked_file") as opened:
        body = json.loads((await api_file_diff(_req(str(f)))).body)

    opened.assert_not_called()
    assert body["status"] == ("modified" if tracked else "untracked")
    assert body["diff"] == (
        "diff --git a/masked.bin b/masked.bin\n"
        "Binary files a/masked.bin and b/masked.bin differ"
    )


@pytest.mark.asyncio
@requires_git
@pytest.mark.parametrize("source", ["untracked", "working", "head"])
@pytest.mark.parametrize("size", [8, 9])
async def test_text_diff_size_bound(tmp_path, monkeypatch, source, size):
    """Whole bodies at the byte cap work; overflow is never redacted as a prefix."""
    cap = 8
    monkeypatch.setattr(files_mod, "_FILE_DIFF_MAX_BYTES", cap)
    text = "x" * size
    if source == "untracked":
        _git_repo(tmp_path)
        f = tmp_path / "text.txt"
        f.write_text(text, encoding="utf-8")
    else:
        old, new = (text, "new\n") if source == "head" else ("old\n", text)
        f = _commit_then_write(tmp_path, "text.txt", old, new)

    with patch.object(files_mod, "_sel", return_value=_mock_sel()), \
         patch.object(files_mod, "redact", wraps=files_mod.redact) as redacted:
        body = json.loads((await api_file_diff(_req(str(f)))).body)

    if size > cap and source != "head":
        assert body["status"] == "error"
        assert body["diff"] == ""
        assert all(call.args[0] not in (text, text[:cap]) for call in redacted.call_args_list)
    elif source == "head":
        # The cap bounds the working-file read only. HEAD is redacted whole,
        # as on main, and is never cut to a prefix before redaction.
        assert body["status"] == "modified"
        assert body["original"] == text
        assert text in body["diff"]
        assert all(call.args[0] != text[:cap] for call in redacted.call_args_list if size > cap)
    else:
        assert body["status"] == ("untracked" if source == "untracked" else "modified")
        assert text in body["diff"]


@pytest.mark.asyncio
@requires_git
async def test_unchanged_file_over_the_cap_is_clean(tmp_path, monkeypatch):
    """The size bound guards the masked text diff only, not the clean verdict."""
    monkeypatch.setattr(files_mod, "_FILE_DIFF_MAX_BYTES", 8)
    text = "x" * 32 + "\n"
    f = _commit_then_write(tmp_path, "text.txt", text, text)

    with patch.object(files_mod, "_sel", return_value=_mock_sel()):
        body = json.loads((await api_file_diff(_req(str(f)))).body)

    assert body["status"] == "clean"
    assert body["diff"] == ""
    assert body["original"] == text


@pytest.mark.asyncio
@requires_git
async def test_changed_binary_with_head_over_the_cap_keeps_the_summary(tmp_path, monkeypatch):
    """A large committed binary still gets its summary, not an error."""
    name = "fixture-name.bin"
    f = _commit_then_write(tmp_path, name, "old\0" * 16, "new\0")
    monkeypatch.setattr(files_mod, "_FILE_DIFF_MAX_BYTES", 8)
    monkeypatch.setattr(files_mod, "redact", lambda text: text.replace(name, "masked.bin"))

    with patch.object(files_mod, "_sel", return_value=_mock_sel()), \
         patch.object(files_mod, "_open_checked_file") as opened:
        body = json.loads((await api_file_diff(_req(str(f)))).body)

    opened.assert_not_called()
    assert body["status"] == "modified"
    assert body["diff"] == (
        "diff --git a/masked.bin b/masked.bin\n"
        "Binary files a/masked.bin and b/masked.bin differ"
    )
