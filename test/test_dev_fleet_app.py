"""Tests for the dev-fleet native dashboard handler."""

from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac  # noqa: F401
import hmac as _hmac_mod
import os
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yarl
from aiohttp import web  # noqa: F401  (used by builtin re-shell tests)
from aiohttp.test_utils import TestClient, TestServer  # noqa: F401
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from git_maintenance_helpers import git_maintenance_off_entries

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import http_api as http_api_mod
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops as worktree_ops_mod

# ---- builtin re-shell: HMAC middleware, R35, app-process tests ----


def _make_hmac_app():
    app = web.Application(middlewares=[mod.hmac_proxy_middleware])
    app.router.add_get("/health", mod.api_health)
    app.router.add_get("/api/fleet", mod.api_health)  # reuse for HMAC testing
    return app


def _sign_request(secret: str, method: str, path: str, body: bytes = b"") -> dict:
    ts = str(int(time.time()))
    body_hash = hashlib.sha256(body).hexdigest()
    msg = f"{ts}:{method}:{path}:{body_hash}"
    sig = _hmac_mod.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"X-KiroCrew-Proxy": f"{ts}:{sig}"}


def test_is_pr_merged():
    assert mod._is_pr_merged({"state": "MERGED", "number": 42}) is True
    assert mod._is_pr_merged({"state": "OPEN"}) is False
    assert mod._is_pr_merged(None) is False
    assert mod._is_pr_merged({}) is False


# =============================================================================
# Redaction (sync)
# =============================================================================


def test_redact_pr_redacts_strings():
    pr = {"url": "https://example.com/secret", "state": "OPEN", "number": 42}
    result = mod._redact_pr(pr)
    assert result is not None
    assert isinstance(result["number"], int)
    assert result["state"] == "OPEN"


def test_redact_pr_none():
    assert mod._redact_pr(None) is None


# =============================================================================
# _get_owner_repo retry logic (R35c)
# =============================================================================


@pytest.mark.asyncio
async def test_get_owner_repo_caches_success():
    fleet_state_mod._OWNER_REPO = None
    fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0
    try:
        with patch.object(
            fleet_state_mod, "_repo_owner_name", new=AsyncMock(return_value="org/repo")
        ):
            result = await mod._get_owner_repo()
            assert result == "org/repo"
            # Second call uses cache
            result2 = await mod._get_owner_repo()
            assert result2 == "org/repo"
    finally:
        fleet_state_mod._OWNER_REPO = None
        fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0


@pytest.mark.asyncio
async def test_get_owner_repo_retries_on_failure():
    """R35(c): failures retry after 60s backoff, not cached permanently."""
    fleet_state_mod._OWNER_REPO = None
    fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0
    call_count = 0

    async def failing():
        nonlocal call_count
        call_count += 1
        return None

    try:
        with patch.object(fleet_state_mod, "_repo_owner_name", new=failing):
            result = await mod._get_owner_repo()
            assert result is None
            assert call_count == 1

            # Within 60s window, should NOT retry
            result = await mod._get_owner_repo()
            assert result is None
            assert call_count == 1  # blocked by retry_at
    finally:
        fleet_state_mod._OWNER_REPO = None
        fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0


# =============================================================================
# _discover_worktrees sandbox error (QA finding)
# =============================================================================


@pytest.mark.asyncio
async def test_discover_worktrees_sandbox_error_raises():
    """sandbox RuntimeError propagates as RuntimeError, not silent empty."""
    with patch.object(
        runtime_mod,
        "_run_cmd",
        new=AsyncMock(return_value=(-1, "", "sandbox unavailable: RuntimeError(no backend)")),
    ):
        with pytest.raises(RuntimeError, match="sandbox unavailable"):
            await mod._discover_worktrees()


@pytest.mark.asyncio
async def test_discover_worktrees_sandbox_error_keeps_remedy():
    """The actionable remedy must survive into the propagated message.

    The sandbox layer appends its guidance AFTER a ~180-char preamble, so an
    over-eager length cap here delivered the diagnosis and dropped the fix — the
    Discovery Error banner would end mid-word at "Probe". Guard the tail, not
    just the prefix (the pre-existing test only checked the prefix, which is why
    the truncation went unnoticed).
    """
    stderr = (
        "sandbox unavailable: Sandbox backend unavailable and "
        "allow_unsandboxed_exec is not set. No OS-level sandbox backend is "
        "available on this host, and the agent subprocess cannot be safely "
        "isolated. Probe detail: sandbox-exec probe failed (exit 71). This "
        "host's sandbox is NOT broken: the kernel reports this process is "
        "already inside a macOS Seatbelt sandbox that KiroCrew did not create. "
        'Set {"sandbox": false} in ~/.kiro/settings/amazon-internal.json so '
        "KiroCrew's own profile owns isolation, then restart the gateway."
    )
    assert len(stderr) > 200, "fixture must exceed the old cap to be meaningful"
    with patch.object(runtime_mod, "_run_cmd", new=AsyncMock(return_value=(-1, "", stderr))):
        with pytest.raises(RuntimeError) as exc:
            await mod._discover_worktrees()
    msg = str(exc.value)
    assert "amazon-internal.json" in msg
    assert not msg.endswith("Probe")
    assert len(msg) <= mod._SANDBOX_ERR_MAX


@pytest.mark.asyncio
async def test_discover_worktrees_sandbox_error_is_still_bounded():
    """An unbounded stderr is still clipped before reaching the UI."""
    stderr = "sandbox unavailable: " + ("x" * 5000)
    with patch.object(runtime_mod, "_run_cmd", new=AsyncMock(return_value=(-1, "", stderr))):
        with pytest.raises(RuntimeError) as exc:
            await mod._discover_worktrees()
    assert len(str(exc.value)) == mod._SANDBOX_ERR_MAX


@pytest.mark.asyncio
async def test_discover_worktrees_missing_repo_raises_actionable_error(tmp_path):
    """A missing/non-git MAIN_REPO raises with the path and the remedy.

    A silent [] here would render as the
    "No worktrees found" empty state. On packaged installs (where
    KIROCREW_PROJECT_DIR points at the app bundle and discovery falls through
    to the hardcoded ~/kirocrew) that empty state told users they had no
    worktrees when the app was simply looking at a path that does not exist.
    """
    missing = tmp_path / "does-not-exist"
    with (
        patch.object(repository_mod, "MAIN_REPO", str(missing)),
        patch.object(
            runtime_mod,
            "_run_cmd",
            new=AsyncMock(return_value=(128, "", f"fatal: cannot change to '{missing}'")),
        ),
    ):
        with pytest.raises(RuntimeError) as exc:
            await mod._discover_worktrees()
    msg = str(exc.value)
    assert str(missing) in msg  # names the path it tried
    assert "KIROCREW_DEVFLEET_REPO" in msg  # names the remedy


@pytest.mark.asyncio
async def test_discover_worktrees_git_failure_raises_with_stderr(tmp_path):
    """When the repo exists but git fails, git's own message is surfaced."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with (
        patch.object(repository_mod, "MAIN_REPO", str(repo)),
        patch.object(
            runtime_mod,
            "_run_cmd",
            new=AsyncMock(return_value=(128, "", "fatal: index file corrupt")),
        ),
    ):
        with pytest.raises(RuntimeError, match="index file corrupt"):
            await mod._discover_worktrees()


@pytest.mark.asyncio
async def test_discover_worktrees_git_failure_is_bounded(tmp_path):
    """An unbounded git stderr is clipped before reaching the UI."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with (
        patch.object(repository_mod, "MAIN_REPO", str(repo)),
        patch.object(
            runtime_mod, "_run_cmd", new=AsyncMock(return_value=(128, "", "fatal: " + "x" * 5000))
        ),
    ):
        with pytest.raises(RuntimeError) as exc:
            await mod._discover_worktrees()
    # bounded: the git portion is clipped to _GIT_ERR_MAX plus the fixed prefix
    assert len(str(exc.value)) <= mod._GIT_ERR_MAX + 200


@pytest.mark.asyncio
async def test_discover_worktrees_unresolved_git_blames_host_not_repo(tmp_path):
    """No trusted git => the error names the tool + override, not the repo.

    Without the fix this failure surfaces as "git worktree discovery
    failed in <repo>: no trusted executable for 'git' in <PATH>" — blaming a
    healthy checkout, echoing the whole trusted PATH into the UI, and never
    naming KIROCREW_DEVFLEET_BIN_GIT, the override that is the actual remedy.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    stderr = f"{mod._UNRESOLVED_TOOL_PREFIX}'git' in {mod._TRUSTED_PATH}"
    with (
        patch.object(repository_mod, "MAIN_REPO", str(repo)),
        patch.object(runtime_mod, "_run_cmd", new=AsyncMock(return_value=(-1, "", stderr))),
    ):
        with pytest.raises(RuntimeError) as exc:
            await mod._discover_worktrees()
    msg = str(exc.value)
    assert "'git'" in msg  # names the tool that could not be resolved
    assert "KIROCREW_DEVFLEET_BIN_GIT" in msg  # names the remedy
    assert mod._TRUSTED_PATH not in msg  # PATH stays in the log, not the UI
    assert "worktree discovery failed" not in msg  # not blamed on the repo
    assert str(repo) not in msg  # the checkout is not implicated at all


@pytest.mark.asyncio
async def test_discover_worktrees_real_git_error_not_misclassified(tmp_path):
    """A git failure merely MENTIONING the sentinel text mid-string is not
    reclassified: only a stderr `_run_cmd` itself synthesized (prefix at
    position 0) takes the unresolved-tool branch; everything else still
    surfaces git's own redacted, bounded message."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with (
        patch.object(repository_mod, "MAIN_REPO", str(repo)),
        patch.object(
            runtime_mod,
            "_run_cmd",
            new=AsyncMock(return_value=(128, "", f"fatal: {mod._UNRESOLVED_TOOL_PREFIX}'hook'")),
        ),
    ):
        with pytest.raises(RuntimeError, match="worktree discovery failed"):
            await mod._discover_worktrees()


@pytest.mark.asyncio
async def test_sync_unresolved_git_names_override_not_path(monkeypatch):
    """/api/sync with no trusted git returns the same remedy-first message."""
    with (
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=mod.BASE_BRANCH),
        patch.object(worktree_ops_mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")),
        patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: None),
        patch.object(worktree_ops_mod.dep_sync, "locked_console_scripts", return_value=[]),
    ):
        worktree_ops_mod._SYNC_RID = None
        res = await mod._sync()
    assert res["ok"] is False
    assert "'git'" in res["error"]
    assert "KIROCREW_DEVFLEET_BIN_GIT" in res["error"]
    assert mod._TRUSTED_PATH not in res["error"]


def test_bin_override_var_derivation():
    """The advertised override var matches what _trusted_bin actually reads,
    including dash-to-underscore mapping for non-git tools."""
    assert mod._bin_override_var("git") == "KIROCREW_DEVFLEET_BIN_GIT"
    assert mod._bin_override_var("some-tool") == "KIROCREW_DEVFLEET_BIN_SOME_TOOL"
    assert "KIROCREW_DEVFLEET_BIN_GIT" in mod._unresolved_tool_message("git")


# =============================================================================
# HMAC middleware tests
# =============================================================================


@pytest.mark.asyncio
async def test_hmac_valid_signature_passes():
    secret = "test-secret"
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            headers = _sign_request(secret, "GET", "/api/fleet")
            resp = await client.get("/api/fleet", headers=headers)
            assert resp.status == 200


@pytest.mark.asyncio
async def test_hmac_missing_header_returns_401():
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value="secret"):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet")
            assert resp.status == 401
            body = await resp.json()
            assert "missing" in body["error"]


@pytest.mark.asyncio
async def test_hmac_invalid_signature_returns_401():
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value="secret"):
        async with TestClient(TestServer(app)) as client:
            ts = str(int(time.time()))
            headers = {"X-KiroCrew-Proxy": f"{ts}:badbadbadbad"}
            resp = await client.get("/api/fleet", headers=headers)
            assert resp.status == 401
            body = await resp.json()
            assert "invalid" in body["error"]


@pytest.mark.asyncio
async def test_hmac_expired_timestamp_returns_401():
    secret = "test-secret"
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            old_ts = str(int(time.time()) - 120)  # 2 min old
            body_hash = hashlib.sha256(b"").hexdigest()
            msg = f"{old_ts}:GET:/api/fleet:{body_hash}"
            sig = _hmac_mod.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
            headers = {"X-KiroCrew-Proxy": f"{old_ts}:{sig}"}
            resp = await client.get("/api/fleet", headers=headers)
            assert resp.status == 401
            body = await resp.json()
            assert "expired" in body["error"]


@pytest.mark.asyncio
async def test_hmac_health_bypasses_verification():
    """Health endpoint does not require HMAC (used by the backend health loop in
    ``apps/backend_runtime/supervision.py``)."""
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value=""):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire_target",
    [
        "/api/fleet?name=my%20worktree",  # space
        "/api/fleet?name=caf%C3%A9",  # non-ASCII
        "/api/fleet?name=a+b",  # '+' (decodes to space in query)
    ],
)
async def test_hmac_gateway_signed_escapable_query_passes(wire_target: str):
    """The gateway signs the RAW percent-encoded request-target; the middleware
    must recompute over the same wire bytes, not aiohttp's decoded
    path + query_string reconstruction, or every escapable query value 401s."""
    secret = "test-secret"
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            headers = _sign_request(secret, "GET", wire_target)
            # encoded=True keeps the exact signed bytes on the wire, mirroring
            # the gateway's yarl.URL(..., encoded=True) forwarding.
            resp = await client.get(yarl.URL(wire_target, encoded=True), headers=headers)
            assert resp.status == 200, await resp.text()


@pytest.mark.asyncio
async def test_hmac_gateway_signed_no_query_target_passes():
    """Without a query string neither side appends a '?' — raw and decoded
    spellings coincide and the signature must still verify."""
    secret = "test-secret"
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            headers = _sign_request(secret, "GET", "/api/fleet")
            resp = await client.get("/api/fleet", headers=headers)
            assert resp.status == 200, await resp.text()


# =============================================================================
# R35(b): worktree removal verdict_oid gate
# =============================================================================


@pytest.mark.asyncio
async def test_worktree_remove_refuses_cherry_ahead_at_pinned_oid():
    """Non-forced removal with merged PR must fail if branch OID != PR headRefOid."""
    fake_wt = {"path": "/tmp/fake-wt", "branch": "feat-x", "is_main": False}

    with (
        patch.object(repository_mod, "_find_worktree", new=AsyncMock(return_value=(fake_wt, None))),
        patch.object(repository_mod, "_git", new=AsyncMock(return_value="local_oid_aaa")),
        patch.object(repository_mod, "_real_dirty", new=AsyncMock(return_value=False)),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new=AsyncMock(return_value={"state": "MERGED", "number": 1}),
        ),
        patch.object(repository_mod, "_own_commits_count", new=AsyncMock(return_value=0)),
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new=AsyncMock(return_value="pr_oid_bbb")
        ),
        patch.object(repository_mod, "_upstream_remote", new=AsyncMock(return_value="origin")),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
    ):
        result = await mod._worktree_remove("feat-x", force=False)
        assert result["ok"] is False
        assert "OID diverged" in result["error"]


# =============================================================================
# Fleet handler catches sandbox RuntimeError
# =============================================================================


@pytest.mark.asyncio
async def test_fleet_handler_sandbox_error_returns_error_payload():
    """api_dev_fleet_fleet returns error payload when sandbox is unavailable."""

    async def boom():
        raise RuntimeError("sandbox unavailable: no backend")

    with (
        patch.object(fleet_state_mod, "_fleet_refresh", new=boom),
        patch.object(fleet_state_mod, "_fleet_cached", new=boom),
    ):
        # Use a minimal app to test the handler
        app = web.Application()
        app.router.add_get("/api/fleet", mod.api_dev_fleet_fleet)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet")
            assert resp.status == 200
            body = await resp.json()
            assert body["worktrees"] == []
            assert "sandbox unavailable" in body["error"]


@pytest.mark.asyncio
async def test_fleet_handler_missing_repo_returns_error_payload():
    """The missing-repo RuntimeError reaches the client as the error payload
    (the frontend renders it as the Discovery Error banner), not a silent
    empty fleet."""

    async def boom():
        raise RuntimeError(
            "main checkout not found: /nope/kirocrew is missing or not a git "
            "checkout. Set KIROCREW_DEVFLEET_REPO to your Kiro Crew checkout, "
            "or clone it to ~/kirocrew."
        )

    with (
        patch.object(fleet_state_mod, "_fleet_refresh", new=boom),
        patch.object(fleet_state_mod, "_fleet_cached", new=boom),
    ):
        app = web.Application()
        app.router.add_get("/api/fleet", mod.api_dev_fleet_fleet)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet")
            assert resp.status == 200
            body = await resp.json()
            assert body["worktrees"] == []
            assert "main checkout not found" in body["error"]
            assert "KIROCREW_DEVFLEET_REPO" in body["error"]


# =============================================================================
# main-checkout discovery (no invented default path)
# =============================================================================


def _make_checkout(root: Path) -> Path:
    """Create a directory carrying every Kiro Crew checkout marker."""
    (root / ".git").mkdir(parents=True)
    (root / "src" / "kiro_crew").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'kiro-crew'\n")
    return root


def test_is_kirocrew_checkout_requires_every_marker(tmp_path):
    """A bare git repo is refused: adopting it would run Pull+Build inside it."""
    bare = tmp_path / "some-other-repo"
    (bare / ".git").mkdir(parents=True)
    assert mod._is_kirocrew_checkout(str(bare)) is False
    assert mod._is_kirocrew_checkout(str(_make_checkout(tmp_path / "kirocrew"))) is True
    assert mod._is_kirocrew_checkout("") is False


def test_default_main_repo_returns_empty_when_nothing_is_found(monkeypatch):
    """No checkout resolves to "" — never to a path nobody asked for.

    A synthesized default makes a first run report a missing checkout the user
    never chose, which reads as a broken app rather than an open question.
    """
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with patch.object(repository_mod, "_own_source_checkout", return_value=None):
        assert mod._default_main_repo() == ""


def test_default_main_repo_skips_a_project_dir_that_is_not_kirocrew(monkeypatch, tmp_path):
    """A project directory that is some other git repo is not adopted."""
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(other))
    with patch.object(repository_mod, "_own_source_checkout", return_value=None):
        assert mod._default_main_repo() == ""


def test_default_main_repo_falls_back_to_the_running_checkout(monkeypatch, tmp_path):
    """A gateway running from source manages that source tree, unconfigured."""
    own = _make_checkout(tmp_path / "kirocrew")
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with patch.object(repository_mod, "_own_source_checkout", return_value=str(own)):
        assert mod._default_main_repo() == str(own)


def test_discover_main_repo_honors_the_config_repo_path(monkeypatch):
    """``dev_fleet.repo_path`` is a supported alternative to the env var."""
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={"repo_path": "/opt/kc"}):
        assert mod._discover_main_repo() == "/opt/kc"


def test_discover_main_repo_takes_an_explicit_path_verbatim(monkeypatch):
    """A configured path is NOT marker-tested: a typo must surface as an error
    naming that path, not be silently swapped for a discovered checkout."""
    monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/typo/kirocrew")
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={"repo_path": "/opt/kc"}):
        assert mod._discover_main_repo() == "/typo/kirocrew"


def test_discover_main_repo_finds_a_conventional_clone_location(monkeypatch, tmp_path):
    """A real checkout in a conventional location is found without configuration."""
    home = tmp_path / "home"
    checkout = _make_checkout(home / "Repos" / "KiroCrew")  # brand-ok: clone dir name
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with (
        patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}),
        patch.object(repository_mod, "_own_source_checkout", return_value=None),
        patch.object(repository_mod.Path, "home", staticmethod(lambda: home)),
    ):
        assert mod._discover_main_repo() == str(checkout)


def test_discover_main_repo_uses_the_filesystem_spelling(monkeypatch, tmp_path):
    """The returned path is spelled the way the directory is, not the way the
    probe list guesses — a case-variant does not match what git reports."""
    home = tmp_path / "home"
    checkout = _make_checkout(home / "repos" / "KIROCREW")
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with (
        patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}),
        patch.object(repository_mod, "_own_source_checkout", return_value=None),
        patch.object(repository_mod.Path, "home", staticmethod(lambda: home)),
    ):
        assert mod._discover_main_repo() == str(checkout)


def test_discover_main_repo_ignores_an_unmarked_conventional_location(monkeypatch, tmp_path):
    """An empty ~/kirocrew directory does not count as a checkout."""
    home = tmp_path / "home"
    (home / "kirocrew").mkdir(parents=True)
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with (
        patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}),
        patch.object(repository_mod, "_own_source_checkout", return_value=None),
        patch.object(repository_mod.Path, "home", staticmethod(lambda: home)),
    ):
        assert mod._discover_main_repo() == ""


@pytest.mark.asyncio
async def test_discover_worktrees_without_a_repo_never_runs_git():
    """`git -C ""` answers for the backend's own working directory, so an
    unresolved repo must not reach git at all."""
    run = AsyncMock(return_value=(0, "", ""))
    with (
        patch.object(repository_mod, "MAIN_REPO", ""),
        patch.object(runtime_mod, "_run_cmd", new=run),
    ):
        with pytest.raises(mod.RepoNotConfigured):
            await mod._discover_worktrees()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_upstream_remote_without_a_repo_never_runs_git():
    run = AsyncMock(return_value=(0, "", ""))
    with (
        patch.object(repository_mod, "MAIN_REPO", ""),
        patch.object(repository_mod, "_UPSTREAM_REMOTE", None),
        patch.object(runtime_mod, "_run_cmd", new=run),
    ):
        assert await mod._upstream_remote() == "origin"
    run.assert_not_awaited()


def test_build_pending_without_a_repo_is_false():
    """Path("") is Path("."), which would stat this process's own tree."""
    with patch.object(repository_mod, "MAIN_REPO", ""):
        assert mod._build_pending() is False


@pytest.mark.asyncio
async def test_fleet_handler_reports_needs_setup_without_an_error():
    """No checkout found is a setup state, not a failure: the payload carries
    ``needs_setup`` and NO ``error``, so the page asks where the checkout is
    instead of rendering a red banner against a path the user never chose."""

    async def unconfigured():
        raise mod.RepoNotConfigured("no Kiro Crew checkout found to manage")

    with (
        patch.object(fleet_state_mod, "_fleet_refresh", new=unconfigured),
        patch.object(fleet_state_mod, "_fleet_cached", new=unconfigured),
    ):
        app = web.Application()
        app.router.add_get("/api/fleet", mod.api_dev_fleet_fleet)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet")
            assert resp.status == 200
            body = await resp.json()
            assert body == {"worktrees": [], "needs_setup": True}


@pytest.mark.asyncio
async def test_discover_worktrees_refuses_a_configured_non_kirocrew_repo():
    """A configured path that is a readable git repo but NOT this project is
    refused by name, never operated on. Tiers 1-2 skip the marker test at
    DISCOVERY so a typo is not silently replaced by a found checkout — that must
    not also mean the wrong tree gets `worktree remove` run inside it."""
    run = AsyncMock(return_value=(0, "worktree /some/other/repo\n", ""))
    with (
        patch.object(repository_mod, "MAIN_REPO", "/some/other/repo"),
        patch.object(
            repository_mod, "_REPO_INVALID_MSG", "not a Kiro Crew checkout: /some/other/repo ..."
        ),
        patch.object(runtime_mod, "_run_cmd", new=run),
    ):
        with pytest.raises(mod.RepoUnreadable) as exc:
            await mod._discover_worktrees()
    # Refused BEFORE git ran: a readable wrong repo answers `worktree list`
    # happily, so the guard cannot rely on a non-zero exit.
    run.assert_not_awaited()
    assert "/some/other/repo" in str(exc.value)
    assert "not a Kiro Crew checkout" in str(exc.value)


@pytest.mark.asyncio
async def test_auto_prune_reaper_idles_for_a_configured_non_kirocrew_repo():
    """An enabled reaper must not run a cycle against an unusable checkout.

    MAIN_REPO is TRUTHY for a configured path that fails the marker test, so a
    truthiness guard let the cycle proceed: _prune_candidates raised
    RepoUnreadable, `except Exception` logged a traceback, and the reaper recorded
    outcome="failure" in the SEL trail every interval — a tamper-evident audit
    asserting a failure that never happened.
    """
    prune = AsyncMock()
    sel = MagicMock()
    with (
        patch.object(repository_mod, "MAIN_REPO", "/some/other/repo"),
        patch.object(repository_mod, "_REPO_INVALID_MSG", "not a Kiro Crew checkout: ..."),
        patch.object(worktree_ops_mod, "_auto_prune_cfg", return_value=(True, 0.01)),
        patch.object(worktree_ops_mod, "_auto_prune_once", new=prune),
        patch.object(runtime_mod, "_sel", return_value=sel),
        patch.object(worktree_ops_mod, "asyncio", wraps=asyncio) as aio,
    ):
        aio.sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        with pytest.raises(asyncio.CancelledError):
            await mod._auto_prune_reaper()
    prune.assert_not_awaited()
    sel.log_tool_invocation.assert_not_called()


@pytest.mark.asyncio
async def test_sync_refuses_a_configured_non_kirocrew_repo():
    """The gate lives in the accessor, not the discovery funnel: sync and the
    refresher never pass through _discover_worktrees, and `pull --ff-only` plus
    `pip install -e` inside an unrelated repository is the worst outcome here."""
    with (
        patch.object(repository_mod, "MAIN_REPO", "/some/other/repo"),
        patch.object(
            repository_mod, "_REPO_INVALID_MSG", "not a Kiro Crew checkout: /some/other/repo ..."
        ),
    ):
        res = await mod._sync_start_locked()
    assert res["ok"] is False
    assert "not a Kiro Crew checkout" in res["error"]


@pytest.mark.asyncio
async def test_status_refresher_idles_for_a_configured_non_kirocrew_repo():
    run = AsyncMock(return_value=(0, "", ""))
    with (
        patch.object(repository_mod, "MAIN_REPO", "/some/other/repo"),
        patch.object(
            repository_mod, "_REPO_INVALID_MSG", "not a Kiro Crew checkout: /some/other/repo ..."
        ),
        patch.object(runtime_mod, "_run_cmd", new=run),
    ):
        await mod._status_refresher()
    # Returned without fetching: no git ran against the unrelated repository.
    run.assert_not_awaited()


def test_repo_accessor_gates_both_unusable_states():
    """Both states raise from the accessor, and both share a base so a degrading
    caller cannot enumerate only the reason that existed when it was written."""
    with (
        patch.object(repository_mod, "MAIN_REPO", ""),
        patch.object(repository_mod, "_REPO_INVALID_MSG", None),
    ):
        with pytest.raises(mod.RepoNotConfigured):
            mod._repo()
    with (
        patch.object(repository_mod, "MAIN_REPO", "/x"),
        patch.object(repository_mod, "_REPO_INVALID_MSG", "bad"),
    ):
        with pytest.raises(mod.RepoUnreadable):
            mod._repo()
    assert issubclass(mod.RepoNotConfigured, mod.RepoUnavailable)
    assert issubclass(mod.RepoUnreadable, mod.RepoUnavailable)
    with (
        patch.object(repository_mod, "MAIN_REPO", "/good"),
        patch.object(repository_mod, "_REPO_INVALID_MSG", None),
    ):
        assert mod._repo() == "/good"


@pytest.mark.asyncio
async def test_discover_worktrees_proceeds_for_a_validated_checkout():
    with (
        patch.object(repository_mod, "MAIN_REPO", "/good/kirocrew"),
        patch.object(repository_mod, "_REPO_INVALID_MSG", None),
        patch.object(
            runtime_mod,
            "_run_cmd",
            new=AsyncMock(
                return_value=(0, "worktree /good/kirocrew\nbranch refs/heads/main\n", "")
            ),
        ),
    ):
        entries = await mod._discover_worktrees()
    assert entries and entries[0]["is_main"] is True


@pytest.mark.asyncio
async def test_sync_refuses_without_a_repo():
    with patch.object(repository_mod, "MAIN_REPO", ""):
        res = await mod._sync_start_locked()
    assert res["ok"] is False
    assert "no Kiro Crew checkout" in res["error"]


@pytest.mark.asyncio
async def test_unconfigured_repo_answers_a_coded_409_not_a_500():
    """A route that resolves worktrees must not answer a first-run click with an
    uncaught 500 and a generic failure toast. The boundary lives in the
    middleware, so a new route cannot forget the case, and the body carries a
    machine-readable code rather than only prose."""
    secret = "s" * 32

    async def boom(request):
        raise mod.RepoNotConfigured("no Kiro Crew checkout found to manage")

    app = web.Application(middlewares=[mod.hmac_proxy_middleware])
    app.router.add_get("/api/prune-candidates", boom)
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            headers = _sign_request(secret, "GET", "/api/prune-candidates")
            resp = await client.get("/api/prune-candidates", headers=headers)
            assert resp.status == 409
            body = await resp.json()
    assert body["code"] == "repo_not_configured"
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_unreadable_repo_answers_a_coded_409_too():
    """A named-but-unreadable checkout has the same consequence for every route
    except /fleet — no fleet to act on — so it gets the same boundary with its own
    code, instead of an uncaught 500 behind a "Prune preview failed" toast."""
    secret = "s" * 32

    async def boom(request):
        raise mod.RepoUnreadable(
            "main checkout not found: /opt/kc is missing or not a git checkout."
        )

    app = web.Application(middlewares=[mod.hmac_proxy_middleware])
    app.router.add_get("/api/prune-candidates", boom)
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            headers = _sign_request(secret, "GET", "/api/prune-candidates")
            resp = await client.get("/api/prune-candidates", headers=headers)
            assert resp.status == 409
            body = await resp.json()
    assert body["code"] == "repo_unreadable"


@pytest.mark.asyncio
async def test_fleet_handler_still_reports_an_unreadable_repo_as_an_error():
    """/fleet is the one route that distinguishes them: unreadable keeps the
    error payload (the Discovery Error banner names the path the user chose)."""

    async def boom():
        raise mod.RepoUnreadable(
            "main checkout not found: /opt/kc is missing or not a git checkout."
        )

    with (
        patch.object(fleet_state_mod, "_fleet_refresh", new=boom),
        patch.object(fleet_state_mod, "_fleet_cached", new=boom),
    ):
        app = web.Application()
        app.router.add_get("/api/fleet", mod.api_dev_fleet_fleet)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet")
            body = await resp.json()
    assert body["worktrees"] == []
    assert "main checkout not found" in body["error"]
    assert "needs_setup" not in body


def test_repo_source_hint_names_the_env_var(monkeypatch):
    monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/typo/kirocrew")
    assert "KIROCREW_DEVFLEET_REPO" in mod._repo_source_hint()
    assert "config.json" not in mod._repo_source_hint()


def test_repo_source_hint_names_the_config_key(monkeypatch):
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    with patch.object(
        repository_mod, "_load_dev_fleet_cfg", return_value={"repo_path": "/typo/kc"}
    ):
        hint = mod._repo_source_hint()
    assert "config.json" in hint
    assert "environment variable" not in hint


def test_repo_source_hint_offers_both_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}):
        hint = mod._repo_source_hint()
    assert "KIROCREW_DEVFLEET_REPO" in hint and "config.json" in hint


# =============================================================================
# GIT_CONFIG_COUNT env neutralizers
# =============================================================================


def test_git_env_neutralizers_present():
    """_GIT_ENV_NEUTRALIZERS pins protocol and neutralizes execution vectors."""
    n = mod._GIT_ENV_NEUTRALIZERS
    assert n["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert n["GIT_PROTOCOL_FROM_USER"] == "0"
    assert n["GIT_NO_REPLACE_OBJECTS"] == "1"
    # GIT_NO_REPLACE_OBJECTS and GIT_OPTIONAL_LOCKS are env vars in their own
    # right, NOT config pairs, so the count must not have grown to cover them.
    assert n["GIT_OPTIONAL_LOCKS"] == "0"
    assert n["GIT_CONFIG_COUNT"] == "9"
    assert n["GIT_CONFIG_KEY_0"] == "core.fsmonitor"
    assert n["GIT_CONFIG_VALUE_0"] == "false"
    assert n["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert n["GIT_CONFIG_VALUE_1"] == "/dev/null"
    assert n["GIT_CONFIG_KEY_2"] == "credential.helper"
    assert n["GIT_CONFIG_VALUE_2"] == ""
    assert n["GIT_CONFIG_KEY_3"] == "core.sshCommand"
    assert n["GIT_CONFIG_VALUE_3"] == "ssh"
    # Signature verification EXECS the program these name, and `showSignature` is
    # the trigger that makes a plain `git log` reach for one. All four program
    # spellings, because `gpg.openpgp.program` is a synonym that OVERRIDES the bare
    # `gpg.program`, so pinning only the bare key leaves an unpinned way in.
    assert n["GIT_CONFIG_KEY_4"] == "gpg.program"
    assert n["GIT_CONFIG_VALUE_4"] == "true"
    assert n["GIT_CONFIG_KEY_5"] == "gpg.openpgp.program"
    assert n["GIT_CONFIG_VALUE_5"] == "true"
    assert n["GIT_CONFIG_KEY_6"] == "gpg.ssh.program"
    assert n["GIT_CONFIG_VALUE_6"] == "true"
    assert n["GIT_CONFIG_KEY_7"] == "gpg.x509.program"
    assert n["GIT_CONFIG_VALUE_7"] == "true"
    assert n["GIT_CONFIG_KEY_8"] == "log.showSignature"
    assert n["GIT_CONFIG_VALUE_8"] == "false"
    # The count DERIVED, not retyped: an undercount silently drops the tail of the
    # list, so the pins past it would be absent while this test still read green
    # against a hardcoded number that matched the stale count.
    pairs = sum(1 for k in n if k.startswith("GIT_CONFIG_KEY_"))
    assert n["GIT_CONFIG_COUNT"] == str(pairs), "the count must cover every pinned pair"
    for i in range(pairs):
        assert f"GIT_CONFIG_VALUE_{i}" in n, f"key {i} has no value beside it"


def _allowlisted_fixture_git_env() -> dict[str, str]:
    """The fixture git env of the object-graph test below: the module's allowlist, no global config.

    The allowlist drops the suite's ``GIT_CONFIG_*`` entries, so they are added
    back to keep auto maintenance off.
    """
    env = {k: v for k, v in os.environ.items() if mod._is_safe_env_key(k)}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env.update(git_maintenance_off_entries(env))
    return env


@pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required to exercise a real refs/replace graft",
)
def test_the_neutralizers_answer_from_the_real_object_graph(tmp_path, monkeypatch):
    """A ``refs/replace`` graft must not change what Dev Fleet's git reads report.

    Pinned by BEHAVIOUR against a real repository rather than by asserting the
    variable is present, because the claim is about git's own semantics: a
    ``refs/replace/<oid>`` ref substitutes one object for another in EVERY read, so
    ``rev-list --count`` walks the substitute's parents and answers about a history
    no checked-out commit names. Dev Fleet acts on exactly that number -- it is how
    "behind by N commits" and the sync's own decisions are formed -- so the real
    graph is the only one that answers the question asked.

    The unpinned reading is asserted too, so this test fails if git ever stops
    honouring the graft and the pin becomes a no-op nobody would notice.
    """
    import subprocess

    git = shutil.which("git")
    repo = tmp_path / "repo"
    repo.mkdir()

    # An exported GIT_DIR/GIT_WORK_TREE is PLANTED rather than assumed absent,
    # because it is what makes the allowlist below observable instead of a claim.
    # This decoy stands in for the operator's real checkout: drop the filtering
    # and every ``run()`` retargets here, so the suite mutates a repository
    # outside tmp_path and the graft assertions answer about a history this test
    # never built.
    decoy = tmp_path / "decoy.git"
    decoy.mkdir()
    monkeypatch.setenv("GIT_DIR", str(decoy))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "decoy-work"))

    # The base env is the module's OWN allowlist (`_is_safe_env_key`, what
    # `_build_env` filters through) rather than a merge over `os.environ`,
    # because git takes its location from the environment and those variables
    # outrank ``-C <repo>``: an exported ``GIT_DIR``, ``GIT_WORK_TREE`` or
    # ``GIT_INDEX_FILE`` would send the ``add``/``commit``/``replace`` below at an
    # unrelated repository, so running the suite would mutate an operator's real
    # index and refs. ``GIT_REPLACE_REF_BASE`` and ``GIT_NO_REPLACE_OBJECTS``
    # would also decide the graft assertions before they were made. An allowlist
    # removes the whole class in one place, including any variable git adds later.
    #
    # Global and system config are excluded for the mirror-image reason: a
    # ``commit.gpgsign`` or ``core.hooksPath`` in the operator's ``~/.gitconfig``
    # would fail these fixture commits on their machine and nowhere else.
    # Repository-local config still applies, which is what the identity below is
    # set through.
    base_env = _allowlisted_fixture_git_env()

    def run(*args, env=None):
        # ``cwd=repo`` alongside ``-C repo``: the location is pinned twice on
        # purpose. ``-C`` is what the module under test relies on; ``cwd`` keeps
        # this fixture's real git from ever running in the worker's checkout.
        proc = subprocess.run(
            [git, "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env={**base_env, **(env or {})},
            cwd=repo,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    run("init", "-q")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "T")
    oids = []
    for i in range(3):
        (repo / "f.txt").write_text(f"{i}\n", encoding="utf-8")
        run("add", "f.txt")
        run("commit", "-q", "-m", f"c{i}")
        oids.append(run("rev-parse", "HEAD"))

    # Three commits, so HEAD's history is three deep.
    assert run("rev-list", "--count", "HEAD") == "3"
    # Graft HEAD onto the root, which shortens the history git reports.
    run("replace", "--graft", oids[2], oids[0])

    # Ungrafted the reading is the truth; grafted it is not. Both are asserted so
    # neither half of the pin can rot silently.
    assert run("rev-list", "--count", "HEAD") == "2"
    assert run("rev-list", "--count", "HEAD", env={"GIT_NO_REPLACE_OBJECTS": "1"}) == "3"
    assert run("rev-list", "--count", "HEAD", env=mod._GIT_ENV_NEUTRALIZERS) == "3"

    # Nothing reached the decoy, so the allowlist rather than luck is what kept
    # every mutation above inside tmp_path.
    assert list(decoy.iterdir()) == [], "an inherited GIT_DIR retargeted the fixture"


# =============================================================================
# _audited decorator exists and is applied
# =============================================================================


def test_the_object_graph_test_carries_its_git_skip():
    # A mark on a plain helper is inert, so the skip must sit on the test: on a
    # host with no git it would otherwise raise TypeError instead of skipping.
    marks = getattr(test_the_neutralizers_answer_from_the_real_object_graph, "pytestmark", [])
    assert [mark.name for mark in marks] == ["skipif"]
    assert not hasattr(_allowlisted_fixture_git_env, "pytestmark")


def test_audited_decorator_applied_to_mutations():
    """All mutating handlers are wrapped by _audited."""
    import inspect

    for name in [
        "api_dev_fleet_sync",
        "api_dev_fleet_worktree_remove",
        "api_dev_fleet_prune_run",
        "api_dev_fleet_pod_up",
        "api_dev_fleet_pod_down",
        "api_dev_fleet_pod_restart",
        "api_dev_fleet_pod_token",
        "api_dev_fleet_pod_provision",
        "api_dev_fleet_pod_provision_dismiss",
        "api_dev_fleet_rebase",
    ]:
        fn = getattr(mod, name)
        # _audited wraps with __name__ preserved
        assert callable(fn), f"{name} is not callable"
        assert inspect.iscoroutinefunction(fn), f"{name} is not async"


# =============================================================================
# create_app / main structure
# =============================================================================


def test_create_app_returns_aiohttp_application():
    app = mod.create_app()
    assert isinstance(app, web.Application)
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/health" in routes
    assert "/api/fleet" in routes
    assert "/api/sync" in routes
    # Served by the GATEWAY process now (gateway_routes.py): the pointer they touch
    # is masked from this backend and every child it spawns.
    assert "/api/restart-gateway" not in routes
    assert "/api/make-live" not in routes


# ---- platform fixes discovered during pod QA of the builtin re-shell ----


def test_backend_spawn_env_includes_kirocrew_home(monkeypatch):
    """The app backend must resolve the SAME config home as the gateway:
    minimal_env() strips KIROCREW_HOME, so spawn must re-inject it or the
    backend reads the wrong .app_secret and every proxied call 401s."""
    import kiro_crew.apps.registry as registry

    monkeypatch.setenv("KIROCREW_HOME", "/tmp/some-pod-home")
    env = registry.minimal_env(KIROCREW_HOME="/tmp/some-pod-home")
    assert env["KIROCREW_HOME"] == "/tmp/some-pod-home"
    # And the bare strip behavior that motivated the fix:
    assert "KIROCREW_HOME" not in registry.minimal_env()


def test_backend_spawn_env_passes_project_dir(monkeypatch):
    """KIROCREW_PROJECT_DIR is a platform var like KIROCREW_HOME — backends
    (e.g. dev-fleet worktree discovery) need the gateway's source checkout."""
    import inspect

    import kiro_crew.apps.backend as backend_mod

    src = inspect.getsource(backend_mod)
    assert "KIROCREW_HOME=str(config_dir())" in src
    assert '"KIROCREW_PROJECT_DIR"' in src


def test_app_secret_loader_does_not_cache_empty(monkeypatch, tmp_path):
    """A missing secret must NOT be cached: it may be provisioned after the
    backend starts (install race). Empty-cache would 401 forever."""
    monkeypatch.setattr(http_api_mod, "_APP_SECRET", None)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    assert mod._load_app_secret() == ""
    assert mod._APP_SECRET is None  # emptiness NOT latched
    sdir = tmp_path / "apps" / mod.APP_NAME
    sdir.mkdir(parents=True)
    (sdir / ".app_secret").write_text("s3cr3t\n")
    assert mod._load_app_secret() == "s3cr3t"  # picked up on retry
