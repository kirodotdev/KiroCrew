"""Coverage-focused tests for the Dev Fleet standalone backend.

Targets uncovered helper branches, pod-guard refusals, request-handler
validation, the audit decorator's non-success paths, and the lifecycle
hooks of ``kiro_crew.apps.builtins.dev_fleet.server``.

Everything is injected: no real git, no real subprocess, no network, and no
writes outside ``tmp_path``. Where the module reads its config home the
loader's ``config_dir`` is patched, so nothing touches the real one.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    gateway_service,
    http_api,
    live,
    repository,
    runtime,
    worktree_ops,
)

# The launchd label derivation imports the pod launchd module for its label
# prefix; on a host where that optional module is unavailable the function
# falls back to the live label and the pod branch cannot be observed.
_LAUNCHD_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="launchd live-program layout is POSIX-only",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class _CapturingSel:
    """Minimal SEL stand-in that records every audit call."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_tool_invocation(self, **kw) -> None:
        self.events.append(kw)


def _sel_capture(monkeypatch) -> _CapturingSel:
    sink = _CapturingSel()
    monkeypatch.setattr(runtime, "_sel", lambda: sink)
    return sink


def _json_request(payload: dict) -> MagicMock:
    """A request double shaped like the one the audit decorator expects."""
    raw = json.dumps(payload).encode()
    request = MagicMock()
    request.read = AsyncMock(return_value=raw)
    request.json = AsyncMock(return_value=payload)
    request.content_length = len(raw)
    request.can_read_body = True
    return request


def _raw_request(raw: bytes, *, json_error: Exception | None = None) -> MagicMock:
    request = MagicMock()
    request.read = AsyncMock(return_value=raw)
    if json_error is not None:
        request.json = AsyncMock(side_effect=json_error)
    else:
        try:
            request.json = AsyncMock(return_value=json.loads(raw or b"{}"))
        except ValueError as exc:
            request.json = AsyncMock(side_effect=exc)
    request.content_length = len(raw)
    request.can_read_body = True
    return request


def _same(a: str, b: str) -> bool:
    """Path equality that survives Windows 8.3 short paths and /tmp symlinks."""
    return os.path.realpath(a) == os.path.realpath(b)


@pytest.fixture(autouse=True)
def _pin_module_state(monkeypatch):
    """Neutralise the module's cached globals so tests never share state."""
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    monkeypatch.setattr(fleet_state, "_HTML_BASE", None)
    monkeypatch.setattr(fleet_state, "_PR_CACHE", {})
    monkeypatch.setattr(repository, "_FALLBACK_REPOS", [])
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {})
    monkeypatch.setattr(fleet_state, "_PROVISION_INFLIGHT", {})
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_BUILD_PATH_CACHE", "/usr/bin")
    monkeypatch.setattr(runtime, "_warm_build_path", AsyncMock())
    monkeypatch.setattr(worktree_ops, "_pod_env", lambda: {})
    # Shutdown admission state: each test starts with a clean (non-shutdown) process.
    monkeypatch.setattr(runtime, "_SHUTDOWN_IN_PROGRESS", False)
    monkeypatch.setattr(runtime, "_SHUTDOWN_ADMISSION_LOCK", asyncio.Lock())


# --------------------------------------------------------------------------
# _load_dev_fleet_cfg
# --------------------------------------------------------------------------
def test_load_dev_fleet_cfg_overlay_wins(monkeypatch, tmp_path):
    """config.local.json overlays config.json for the dev_fleet section."""
    (tmp_path / "config.json").write_text(
        json.dumps({"dev_fleet": {"a": 1, "keep": "yes"}}), encoding="utf-8", newline="\n"
    )
    (tmp_path / "config.local.json").write_text(
        json.dumps({"dev_fleet": {"a": 2, "b": 3}}), encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

    assert repository._load_dev_fleet_cfg() == {"a": 2, "keep": "yes", "b": 3}


def test_load_dev_fleet_cfg_ignores_unusable_files(monkeypatch, tmp_path):
    """Invalid JSON and a non-dict dev_fleet value are skipped, never raised."""
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8", newline="\n")
    (tmp_path / "config.local.json").write_text(
        json.dumps({"dev_fleet": "nope"}), encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

    assert repository._load_dev_fleet_cfg() == {}


def test_load_dev_fleet_cfg_config_dir_failure_is_empty(monkeypatch):
    """A config home that cannot be resolved yields {} rather than an error."""
    def _boom():
        raise RuntimeError("no home")

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", _boom)
    assert repository._load_dev_fleet_cfg() == {}


# --------------------------------------------------------------------------
# _launchd_live_worktree
# --------------------------------------------------------------------------
@_LAUNCHD_ONLY
def test_launchd_live_worktree_missing_launcher_is_none(monkeypatch, tmp_path):
    missing = tmp_path / "absent" / "live-gateway"
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: missing)
    )
    assert live._launchd_live_worktree() is None


@_LAUNCHD_ONLY
@pytest.mark.parametrize(
    "script",
    [
        "#!/bin/sh\nexport FOO=1\n",  # no exec line at all
        "#!/bin/sh\nexec '/usr/local/bin/kirocrew' gateway\n",  # not a venv binary
    ],
)
def test_launchd_live_worktree_unusable_exec_is_none(monkeypatch, tmp_path, script):
    launcher = tmp_path / "live-gateway"
    launcher.write_text(script, encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: launcher)
    )
    assert live._launchd_live_worktree() is None


@_LAUNCHD_ONLY
def test_launchd_live_worktree_resolves_checkout(monkeypatch, tmp_path):
    """A venv binary in the exec line resolves to its checkout grandparent."""
    checkout = tmp_path / "kirocrew-wt-alpha"
    kcbin = checkout / ".venv" / "bin" / "kirocrew"
    kcbin.parent.mkdir(parents=True)
    kcbin.write_text("", encoding="utf-8", newline="\n")
    launcher = tmp_path / "live-gateway"
    launcher.write_text(f"#!/bin/sh\nexec '{kcbin}' gateway\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: launcher)
    )

    resolved = live._launchd_live_worktree()
    assert resolved is not None
    assert _same(resolved, str(checkout))


# --------------------------------------------------------------------------
# _load_fallback_repos
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_load_fallback_repos_collects_ancestor_remotes(monkeypatch):
    """A differently named repo whose base is an ancestor becomes a fallback repo."""

    async def fake_run(cmd, **kw):
        if cmd[-1] == "remote":
            return 0, "origin\nold\nstale\n", ""
        if "--is-ancestor" in cmd:
            return (0 if "old/main" in cmd else 1), "", ""
        if "get-url" in cmd:
            remote = cmd[-1]
            # The ancestor remote is a genuinely different REPOSITORY — a
            # pre-rename name, not a fork of upstream under another owner — so
            # its ancestor main still qualifies as a fallback repo.
            if remote == "origin":
                return 0, "git@github.com:kirodotdev/KiroCrew.git\n", ""
            return 0, "git@github.com:someone/kirocrew-old.git\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(repository, "_repo", lambda: "/fake/repo")
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await repository._load_fallback_repos()
    assert repository._FALLBACK_REPOS == ["someone/kirocrew-old"]


@pytest.mark.asyncio
async def test_load_fallback_repos_empty_when_remote_listing_fails(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(1, "", "boom")))
    await repository._load_fallback_repos()
    assert repository._FALLBACK_REPOS == []


@pytest.mark.asyncio
async def test_load_fallback_repos_skips_duplicate_upstream_alias(monkeypatch):
    """A remote that merely aliases the upstream repo is NOT a fallback repo.

    This is the fleet-misflag defect: an ``origin`` left in place after the
    tracking remote was renamed points at the SAME repo as upstream, so
    ``merge-base --is-ancestor`` is trivially true. Upstream's own name must
    not enter the fallback list (which would flag every worktree as legacy).
    """

    async def fake_run(cmd, **kw):
        if cmd[-1] == "remote":
            return 0, "kirocrew\norigin\n", ""
        if "--is-ancestor" in cmd:
            return 0, "", ""  # identical refs -> trivially an ancestor
        if "get-url" in cmd:
            # Both remotes point at the SAME upstream repository.
            return 0, "https://github.com/kirodotdev/KiroCrew.git\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(repository, "_repo", lambda: "/fake/repo")
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "kirocrew")
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await repository._load_fallback_repos()
    assert repository._FALLBACK_REPOS == []


@pytest.mark.asyncio
async def test_load_fallback_repos_recognizes_scp_and_git_suffix_as_same(monkeypatch):
    """scp-style and .git-suffixed spellings of the upstream URL are one repo.

    The alias uses ``git@github.com:...`` while upstream uses the https,
    ``.git``-suffixed spelling; both normalize to the same identity, so the
    alias is skipped rather than treated as a distinct fork.
    """

    async def fake_run(cmd, **kw):
        if cmd[-1] == "remote":
            return 0, "kirocrew\norigin\n", ""
        if "--is-ancestor" in cmd:
            return 0, "", ""
        if "get-url" in cmd:
            remote = cmd[-1]
            if remote == "kirocrew":
                return 0, "https://github.com/kirodotdev/KiroCrew.git\n", ""
            return 0, "git@github.com:KiroDotDev/kirocrew\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(repository, "_repo", lambda: "/fake/repo")
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "kirocrew")
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await repository._load_fallback_repos()
    assert repository._FALLBACK_REPOS == []


@pytest.mark.asyncio
async def test_load_fallback_repos_dedupes_multiple_aliases_of_one_repo(monkeypatch):
    """Two aliases of one genuine pre-rename repo collapse to a single entry."""

    async def fake_run(cmd, **kw):
        if cmd[-1] == "remote":
            return 0, "kirocrew\nold-a\nold-b\n", ""
        if "--is-ancestor" in cmd:
            # Both aliases' mains are ancestors of upstream's.
            return 0, "", ""
        if "get-url" in cmd:
            remote = cmd[-1]
            if remote == "kirocrew":
                return 0, "https://github.com/kirodotdev/KiroCrew.git\n", ""
            if remote == "old-a":
                return 0, "git@github.com:someone/kirocrew-old.git\n", ""
            return 0, "https://github.com/someone/KiroCrew-Old\n", ""  # same repo, other spelling
        return 1, "", "unexpected"

    monkeypatch.setattr(repository, "_repo", lambda: "/fake/repo")
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "kirocrew")
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await repository._load_fallback_repos()
    assert repository._FALLBACK_REPOS == ["someone/kirocrew-old"]


@pytest.mark.asyncio
async def test_load_fallback_repos_skips_same_named_fork(monkeypatch):
    """A fork of upstream under another owner is NOT a fallback repo.

    A fork's main passes ``merge-base --is-ancestor`` against upstream's until it
    diverges, and its repo NAME is upstream's own. The fallback list is consumed
    by repo name alone, so admitting the fork yields the ``<name>-wt-`` prefix
    that every current-convention worktree matches — flagging the whole fleet as
    legacy.
    """

    async def fake_run(cmd, **kw):
        if cmd[-1] == "remote":
            return 0, "kirocrew\nfork\n", ""
        if "--is-ancestor" in cmd:
            return 0, "", ""  # the fork's main has not diverged yet
        if "get-url" in cmd:
            remote = cmd[-1]
            if remote == "kirocrew":
                return 0, "https://github.com/kirodotdev/KiroCrew.git\n", ""
            # Same repo NAME, different owner — a fork, not a pre-rename repo.
            return 0, "git@github.com:someone/KiroCrew.git\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(repository, "_repo", lambda: "/fake/repo")
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "kirocrew")
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await repository._load_fallback_repos()
    assert repository._FALLBACK_REPOS == []


@pytest.mark.asyncio
async def test_load_fallback_repos_keeps_renamed_repo_alongside_a_fork(monkeypatch):
    """The name guard discriminates: the fork is dropped, the renamed repo kept.

    Both candidates' mains are ancestors of upstream's, so only the repo name
    tells them apart — proving the guard is not a blanket skip of every ancestor.
    """

    async def fake_run(cmd, **kw):
        if cmd[-1] == "remote":
            return 0, "kirocrew\nfork\nold\n", ""
        if "--is-ancestor" in cmd:
            return 0, "", ""
        if "get-url" in cmd:
            remote = cmd[-1]
            if remote == "kirocrew":
                return 0, "https://github.com/kirodotdev/KiroCrew.git\n", ""
            if remote == "fork":
                return 0, "git@github.com:someone/KiroCrew.git\n", ""
            return 0, "git@github.com:kirodotdev/kirocrew-old.git\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(repository, "_repo", lambda: "/fake/repo")
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "kirocrew")
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await repository._load_fallback_repos()
    assert repository._FALLBACK_REPOS == ["kirodotdev/kirocrew-old"]


def test_normalize_repo_identity_spellings_and_host():
    """Same repo across https/scp/.git spellings is one identity; host matters."""
    https = repository._normalize_repo_identity("https://github.com/kirodotdev/KiroCrew.git")
    scp = repository._normalize_repo_identity("git@github.com:KiroDotDev/kirocrew")
    assert https == scp == ("github.com", "kirodotdev/kirocrew")
    # Same owner/repo on a different forge is a DISTINCT identity.
    other = repository._normalize_repo_identity("https://gitlab.com/kirodotdev/kirocrew.git")
    assert other == ("gitlab.com", "kirodotdev/kirocrew")
    assert other != https
    # Unparseable URL yields None.
    assert repository._normalize_repo_identity("not-a-url") is None


# --------------------------------------------------------------------------
# Git info identity
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_git_info_returns_full_oid_and_short_display_head(monkeypatch):
    full_head = "a" * 40

    async def fake_git(_path, *args, **_kwargs):
        values = {
            ("rev-parse", "--abbrev-ref", "HEAD"): "feat/cache",
            ("rev-parse", "HEAD"): full_head,
            ("status", "--porcelain"): "",
            ("rev-list", "--count", "HEAD..origin/main"): "0",
            ("log", "-1", "--format=%ct"): "123",
        }
        return values.get(args)

    monkeypatch.setattr(repository, "_git", fake_git)
    monkeypatch.setattr(repository, "_upstream_remote", AsyncMock(return_value="origin"))

    info = await repository._git_info("/wt")

    assert info["head_oid"] == full_head
    assert info["head"] == full_head[:7]


# --------------------------------------------------------------------------
# PR cache + html base
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pr_status_cached_skips_base_branch(monkeypatch):
    fetch = AsyncMock(return_value={"state": "OPEN"})
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    assert await fleet_state._pr_status_cached(repository.BASE_BRANCH) is None
    assert await fleet_state._pr_status_cached("") is None
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_status_cached_merged_entry_is_terminal(monkeypatch):
    """A MERGED entry is served regardless of age — no refetch."""
    fetch = AsyncMock(return_value={"state": "OPEN"})
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    monkeypatch.setattr(
        fleet_state, "_PR_CACHE", {"feat": {"data": {"state": "MERGED"}, "ts": 0.0}}
    )

    assert (await fleet_state._pr_status_cached("feat"))["state"] == "MERGED"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_status_cached_stale_closed_entry_refetches(monkeypatch):
    """A CLOSED entry can be reopened, so it expires on the normal TTL."""
    fetch = AsyncMock(return_value={"state": "OPEN"})
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    monkeypatch.setattr(
        fleet_state, "_PR_CACHE", {"feat": {"data": {"state": "CLOSED"}, "ts": 0.0}}
    )

    assert (await fleet_state._pr_status_cached("feat"))["state"] == "OPEN"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_pr_status_cached_fresh_entry_is_served(monkeypatch):
    fetch = AsyncMock(return_value={"state": "MERGED"})
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    monkeypatch.setattr(
        fleet_state, "_PR_CACHE", {"feat": {"data": {"state": "OPEN"}, "ts": time.time()}}
    )

    assert (await fleet_state._pr_status_cached("feat"))["state"] == "OPEN"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_status_cached_merged_same_head_retains_terminal_cache(monkeypatch):
    """Same branch + same head OID → MERGED entry remains terminal (no refetch)."""
    fetch = AsyncMock(return_value={"state": "OPEN"})
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    monkeypatch.setattr(
        fleet_state,
        "_PR_CACHE",
        {"feat": {"data": {"state": "MERGED"}, "ts": 0.0, "cached_head": "a" * 40}},
    )

    result = await fleet_state._pr_status_cached("feat", head_oid="a" * 40)
    assert result is not None and result["state"] == "MERGED"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_status_cached_merged_new_head_invalidates_and_refetches(monkeypatch):
    """Same branch + new head OID → stale MERGED entry is discarded; fresh lookup runs."""
    fetch = AsyncMock(return_value={"state": "OPEN"})
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    monkeypatch.setattr(
        fleet_state,
        "_PR_CACHE",
        {"feat": {"data": {"state": "MERGED"}, "ts": 0.0, "cached_head": "a" * 40}},
    )

    result = await fleet_state._pr_status_cached("feat", head_oid="b" * 40)
    # The stale MERGED entry must NOT be returned after a head change.
    assert result is not None and result["state"] == "OPEN"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_pr_status_cached_rejects_old_merged_pr_for_reused_head(monkeypatch):
    old_head = "a" * 40
    new_head = "b" * 40
    fetch = AsyncMock(return_value={"state": "MERGED", "_head_oid": old_head})
    contained = AsyncMock(return_value=False)
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", contained)
    monkeypatch.setattr(
        fleet_state,
        "_PR_CACHE",
        {"feat": {"data": {"state": "MERGED"}, "ts": 0.0, "cached_head": old_head}},
    )

    result = await fleet_state._pr_status_cached("feat", head_oid=new_head)

    assert result is None
    contained.assert_awaited_once_with(repository._repo(), new_head, old_head)
    assert fleet_state._PR_CACHE["feat"]["data"] is None
    assert fleet_state._PR_CACHE["feat"]["cached_head"] == new_head


@pytest.mark.asyncio
async def test_pr_status_cached_accepts_local_ancestor_of_merged_pr_head(monkeypatch):
    local_head = "a" * 40
    pr_head = "b" * 40
    fetch = AsyncMock(return_value={"state": "MERGED", "_head_oid": pr_head})
    contained = AsyncMock(return_value=True)
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", contained)
    monkeypatch.setattr(fleet_state, "_PR_CACHE", {})

    result = await fleet_state._pr_status_cached("feat", head_oid=local_head)

    assert result is not None and result["state"] == "MERGED"
    contained.assert_awaited_once_with(repository._repo(), local_head, pr_head)
    assert fleet_state._PR_CACHE["feat"]["cached_head"] == local_head


@pytest.mark.asyncio
async def test_pr_status_cached_accepts_fetched_merged_pr_for_same_head(monkeypatch):
    head = "a" * 40
    fetch = AsyncMock(return_value={"state": "MERGED", "_head_oid": head})
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    monkeypatch.setattr(fleet_state, "_PR_CACHE", {})

    result = await fleet_state._pr_status_cached("feat", head_oid=head)

    assert result is not None and result["state"] == "MERGED"
    assert fleet_state._PR_CACHE["feat"]["cached_head"] == head


@pytest.mark.asyncio
async def test_pr_status_cached_merged_no_head_oid_remains_terminal(monkeypatch):
    """Callers that omit head_oid continue to treat MERGED as terminal (fail-soft)."""
    fetch = AsyncMock(return_value={"state": "OPEN"})
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    monkeypatch.setattr(
        fleet_state,
        "_PR_CACHE",
        {"feat": {"data": {"state": "MERGED"}, "ts": 0.0, "cached_head": "a" * 40}},
    )

    result = await fleet_state._pr_status_cached("feat")  # no head_oid
    assert result is not None and result["state"] == "MERGED"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_status_cached_lookup_failure_does_not_cache_stale_merged(monkeypatch):
    """When the fresh lookup returns None (gh failure), the cache stores None safely."""
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    monkeypatch.setattr(
        fleet_state,
        "_PR_CACHE",
        {"feat": {"data": {"state": "MERGED"}, "ts": 0.0, "cached_head": "a" * 40}},
    )

    result = await fleet_state._pr_status_cached("feat", head_oid="b" * 40)
    assert result is None
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_pr_status_cached_null_cached_head_invalidated_by_new_head(monkeypatch):
    """Entry written without head_oid (cached_head=None) is invalidated when a head-bearing
    caller provides a head_oid — prevents re-armed staleness after no-head write-back."""
    fetch = AsyncMock(return_value={"state": "OPEN"})
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)
    # Simulate a cache entry that was written by a no-head caller (cached_head missing/None).
    monkeypatch.setattr(
        fleet_state,
        "_PR_CACHE",
        {"feat": {"data": {"state": "MERGED"}, "ts": 0.0, "cached_head": None}},
    )

    result = await fleet_state._pr_status_cached("feat", head_oid="a" * 40)
    # An unknown cached identity cannot match a known full head, so refetch.
    assert result is not None and result["state"] == "OPEN"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_html_repo_base_from_remote_url(monkeypatch):
    monkeypatch.setattr(
        runtime, "_run_cmd", AsyncMock(return_value=(0, "git@github.com:o/r.git\n", ""))
    )
    assert await fleet_state._html_repo_base() == "https://github.com/o/r"


@pytest.mark.asyncio
async def test_html_repo_base_falls_back_to_owner_repo(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(1, "", "no remote")))
    monkeypatch.setattr(fleet_state, "_get_owner_repo", AsyncMock(return_value="o/r"))
    assert await fleet_state._html_repo_base() == "https://github.com/o/r"


@pytest.mark.asyncio
async def test_html_repo_base_cached_value_short_circuits(monkeypatch):
    run = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(runtime, "_run_cmd", run)
    monkeypatch.setattr(fleet_state, "_HTML_BASE", "https://example.invalid/o/r")
    assert await fleet_state._html_repo_base() == "https://example.invalid/o/r"
    run.assert_not_awaited()


# --------------------------------------------------------------------------
# _read_pin_strict
# --------------------------------------------------------------------------
def _pin_cfg(tmp_path: Path, text: str | None) -> SimpleNamespace:
    pods = tmp_path / "pods"
    pods.mkdir(exist_ok=True)
    env_path = pods / "feat.env"
    if text is not None:
        env_path.write_text(text, encoding="utf-8", newline="\n")
    return SimpleNamespace(pods_dir=pods, env_file=lambda name: env_path)


def test_read_pin_strict_no_env_file_is_unpinned(tmp_path):
    cfg = _pin_cfg(tmp_path, None)
    assert worktree_ops._read_pin_strict(cfg, "feat") == (False, None)


def test_read_pin_strict_refused_read_raises(monkeypatch, tmp_path):
    """A hooks-gate refusal is a DENY, never 'unpinned'."""
    cfg = _pin_cfg(tmp_path, "CHECKOUT=/x\n")
    monkeypatch.setattr(worktree_ops.hooks, "safe_read_file_bytes_nolink", lambda *a, **k: None)
    with pytest.raises(OSError, match="refused by hooks read gate"):
        worktree_ops._read_pin_strict(cfg, "feat")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("# a comment\nnovalue\nCHECKOUT='/repo/wt'\n", (True, "/repo/wt")),
        ('OTHER=1\nCHECKOUT="/repo/wt"\n', (True, "/repo/wt")),
        ("CHECKOUT=\n", (True, None)),
        ("OTHER=1\n", (True, None)),
    ],
)
def test_read_pin_strict_parses_checkout(monkeypatch, tmp_path, body, expected):
    cfg = _pin_cfg(tmp_path, body)
    monkeypatch.setattr(
        worktree_ops.hooks,
        "safe_read_file_bytes_nolink",
        lambda *a, **k: body.encode(),
    )
    assert worktree_ops._read_pin_strict(cfg, "feat") == expected


# --------------------------------------------------------------------------
# _pod_checkout_guard
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pod_guard_unknown_worktree(monkeypatch):
    monkeypatch.setattr(repository, "_find_worktree", AsyncMock(return_value=(None, None)))
    assert "unknown worktree" in (await worktree_ops._pod_checkout_guard("ghost") or "")


@pytest.mark.asyncio
async def test_pod_guard_no_pod_subsystem_allows(monkeypatch):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    monkeypatch.setattr(runtime, "_load_cfg", lambda: None)
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", False)
    assert await worktree_ops._pod_checkout_guard("feat") is None


@pytest.mark.asyncio
async def test_pod_guard_unloadable_config_denies(monkeypatch):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    monkeypatch.setattr(runtime, "_load_cfg", lambda: None)
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    assert await worktree_ops._pod_checkout_guard("feat") == (
        "cannot load pod configuration to verify pod identity"
    )


@pytest.mark.asyncio
async def test_pod_guard_unreadable_pin_denies(monkeypatch):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)

    def _boom(cfg, name):
        raise OSError("pin unreadable")

    monkeypatch.setattr(worktree_ops, "_read_pin_strict", _boom)
    assert "cannot verify pod checkout pin" in (
        await worktree_ops._pod_checkout_guard("feat") or ""
    )


@pytest.mark.asyncio
async def test_pod_guard_active_pod_without_pin_denies(monkeypatch):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    monkeypatch.setattr(worktree_ops, "_read_pin_strict", lambda cfg, name: (False, None))
    monkeypatch.setattr(
        runtime, "rt", SimpleNamespace(active_names=lambda cfg: {"feat"}), raising=False
    )
    assert "unattributable pod identity" in (await worktree_ops._pod_checkout_guard("feat") or "")


@pytest.mark.asyncio
async def test_pod_guard_inactive_pod_without_pin_allows(monkeypatch):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    monkeypatch.setattr(worktree_ops, "_read_pin_strict", lambda cfg, name: (False, None))
    monkeypatch.setattr(
        runtime, "rt", SimpleNamespace(active_names=lambda cfg: set()), raising=False
    )
    assert await worktree_ops._pod_checkout_guard("feat") is None


@pytest.mark.asyncio
async def test_pod_guard_active_names_failure_denies(monkeypatch):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    monkeypatch.setattr(worktree_ops, "_read_pin_strict", lambda cfg, name: (False, None))

    def _boom(cfg):
        raise RuntimeError("systemctl gone")

    monkeypatch.setattr(runtime, "rt", SimpleNamespace(active_names=_boom), raising=False)
    assert "cannot verify active pods" in (await worktree_ops._pod_checkout_guard("feat") or "")


@pytest.mark.asyncio
async def test_pod_guard_pin_without_checkout_denies(monkeypatch):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    monkeypatch.setattr(worktree_ops, "_read_pin_strict", lambda cfg, name: (True, None))
    assert "ambiguous pod identity" in (await worktree_ops._pod_checkout_guard("feat") or "")


@pytest.mark.asyncio
async def test_pod_guard_foreign_checkout_denies(monkeypatch, tmp_path):
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"
    mine.mkdir()
    theirs.mkdir()
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": str(mine)}, None))
    )
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    monkeypatch.setattr(worktree_ops, "_read_pin_strict", lambda cfg, name: (True, str(theirs)))
    assert "cross-repository pod operation" in (
        await worktree_ops._pod_checkout_guard("feat") or ""
    )


@pytest.mark.asyncio
async def test_pod_guard_matching_checkout_allows(monkeypatch, tmp_path):
    mine = tmp_path / "mine"
    mine.mkdir()
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": str(mine)}, None))
    )
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    monkeypatch.setattr(worktree_ops, "_read_pin_strict", lambda cfg, name: (True, str(mine)))
    assert await worktree_ops._pod_checkout_guard("feat") is None


# --------------------------------------------------------------------------
# pod operations
# --------------------------------------------------------------------------
@pytest.fixture
def allow_pod(monkeypatch):
    """Pod guard passes and pod state verification is opt-in per test."""
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: None)
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", False)


@pytest.mark.asyncio
async def test_pod_up_refused_by_guard(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value="nope"))
    assert await worktree_ops._pod_up("feat") == {"ok": False, "error": "nope"}


@pytest.mark.asyncio
async def test_pod_up_cli_failure_is_reported(monkeypatch, allow_pod):
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(1, "", "boom")))
    assert await worktree_ops._pod_up("feat") == {"ok": False, "error": "boom"}


@pytest.mark.asyncio
async def test_pod_up_non_json_output_still_ok(monkeypatch, allow_pod):
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "not json", "")))
    assert await worktree_ops._pod_up("feat") == {"ok": True, "output": "not json"}


@pytest.mark.asyncio
async def test_pod_up_json_output_is_merged(monkeypatch, allow_pod):
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, '{"port": 9999}', "")))
    assert await worktree_ops._pod_up("feat") == {"ok": True, "port": 9999}


@pytest.mark.asyncio
async def test_pod_up_inactive_after_start_fails_closed(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "{}", "")))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    monkeypatch.setattr(
        runtime, "rt", SimpleNamespace(active_names=lambda cfg: set()), raising=False
    )
    assert await worktree_ops._pod_up("feat") == {
        "ok": False,
        "error": "pod not active after start",
    }


@pytest.mark.asyncio
async def test_pod_up_unverifiable_start_fails_closed(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "{}", "")))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)

    def _boom(cfg):
        raise RuntimeError("no bus")

    monkeypatch.setattr(runtime, "rt", SimpleNamespace(active_names=_boom), raising=False)
    res = await worktree_ops._pod_up("feat")
    assert res["ok"] is False
    assert "cannot verify pod start" in res["error"]


@pytest.mark.asyncio
async def test_pod_down_refused_by_guard(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value="denied"))
    assert await worktree_ops._pod_down("feat") == {"ok": False, "error": "denied"}


@pytest.mark.asyncio
async def test_pod_down_cli_failure_is_reported(monkeypatch, allow_pod):
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(2, "out", "")))
    assert await worktree_ops._pod_down("feat") == {"ok": False, "error": "out"}


@pytest.mark.asyncio
async def test_pod_down_still_active_fails_closed(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    monkeypatch.setattr(
        runtime, "rt", SimpleNamespace(active_names=lambda cfg: {"feat"}), raising=False
    )
    assert await worktree_ops._pod_down("feat") == {
        "ok": False,
        "error": "pod still active after shutdown",
    }


@pytest.mark.asyncio
async def test_pod_down_unverifiable_shutdown_fails_closed(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)

    def _boom(cfg):
        raise RuntimeError("no bus")

    monkeypatch.setattr(runtime, "rt", SimpleNamespace(active_names=_boom), raising=False)
    res = await worktree_ops._pod_down("feat")
    assert res["ok"] is False
    assert "cannot verify pod shutdown" in res["error"]


@pytest.mark.asyncio
async def test_pod_down_success(monkeypatch, allow_pod):
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    assert await worktree_ops._pod_down("feat") == {"ok": True, "error": None}


@pytest.mark.asyncio
async def test_pod_restart_stops_on_failed_shutdown(monkeypatch):
    monkeypatch.setattr(
        worktree_ops, "_pod_down", AsyncMock(return_value={"ok": False, "error": "stuck"})
    )
    up = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(worktree_ops, "_pod_up", up)

    res = await worktree_ops._pod_restart("feat")
    assert res == {"ok": False, "error": "pod shutdown failed: stuck"}
    up.assert_not_awaited()


@pytest.mark.asyncio
async def test_pod_restart_starts_after_clean_shutdown(monkeypatch):
    monkeypatch.setattr(
        worktree_ops, "_pod_down", AsyncMock(return_value={"ok": True, "error": None})
    )
    monkeypatch.setattr(worktree_ops, "_pod_up", AsyncMock(return_value={"ok": True, "port": 1}))
    assert await worktree_ops._pod_restart("feat") == {"ok": True, "port": 1}


@pytest.mark.asyncio
async def test_pod_token_refused_by_guard(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value="denied"))
    assert await worktree_ops._pod_token("feat") == {"ok": False, "error": "denied"}


@pytest.mark.asyncio
async def test_pod_token_without_config(monkeypatch, allow_pod):
    assert await worktree_ops._pod_token("feat") == {"ok": False, "error": "PodConfig unavailable"}


@pytest.mark.asyncio
async def test_pod_token_mints_url(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(
        runtime,
        "rt",
        SimpleNamespace(
            mint_token=lambda cfg, name, ttl: "SECRET",
            derive_port=lambda cfg, name: 9123,
        ),
        raising=False,
    )
    res = await worktree_ops._pod_token("feat")
    assert res["ok"] is True
    assert res["url"].endswith("9123/?token=SECRET")


@pytest.mark.asyncio
async def test_pod_token_reports_mint_failure(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())

    def _boom(cfg, name, ttl):
        raise RuntimeError("keyring locked")

    monkeypatch.setattr(runtime, "rt", SimpleNamespace(mint_token=_boom), raising=False)
    assert await worktree_ops._pod_token("feat") == {"ok": False, "error": "keyring locked"}


@pytest.mark.asyncio
async def test_pod_logs_refused_by_guard(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value="denied"))
    assert await worktree_ops._pod_logs("feat") == {"ok": False, "error": "denied"}


@pytest.mark.asyncio
async def test_pod_logs_without_config(monkeypatch, allow_pod):
    assert await worktree_ops._pod_logs("feat") == {"ok": False, "error": "PodConfig unavailable"}


@pytest.mark.asyncio
async def test_pod_logs_returns_redacted_journal(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(
        runtime,
        "rt",
        SimpleNamespace(recent_journal=lambda cfg, name, n: f"lines={n}"),
        raising=False,
    )
    assert await worktree_ops._pod_logs("feat", 7) == {"ok": True, "logs": "lines=7"}


@pytest.mark.asyncio
async def test_pod_provision_refused_by_guard(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value="denied"))
    assert await worktree_ops._pod_provision("feat") == {"ok": False, "error": "denied"}


@pytest.mark.asyncio
async def test_pod_provision_single_flights_running_build(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(fleet_state, "_PROVISION_INFLIGHT", {"feat": "run-1"})
    monkeypatch.setattr(runtime, "_RUNS", {"run-1": {"status": "running"}})
    start = AsyncMock(return_value="run-2")
    monkeypatch.setattr(runtime, "_start_run", start)

    assert await worktree_ops._pod_provision("feat") == {
        "ok": False,
        "error": "provision already running",
        "run_id": "run-1",
    }
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_pod_provision_starts_run_and_records_it(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(fleet_state, "_PROVISION_INFLIGHT", {"feat": "run-0"})
    monkeypatch.setattr(runtime, "_RUNS", {"run-0": {"status": "done"}})
    monkeypatch.setattr(
        worktree_ops,
        "sandboxed_spawn_argv",
        lambda argv, tier, env=None: (list(argv), {}, None),
    )
    monkeypatch.setattr(runtime, "_start_run", AsyncMock(return_value="run-9"))

    assert await worktree_ops._pod_provision("feat") == {"ok": True, "run_id": "run-9"}
    assert fleet_state._PROVISION_INFLIGHT["feat"] == "run-9"


@pytest.mark.asyncio
async def test_pod_provision_dismiss_forgets_matching_terminal_run(monkeypatch):
    monkeypatch.setattr(fleet_state, "_PROVISION_INFLIGHT", {"feat": "run-1"})
    monkeypatch.setattr(runtime, "_RUNS", {"run-1": {"status": "done", "exit_code": 1}})

    assert await worktree_ops._pod_provision_dismiss("feat", "run-1") == {
        "ok": True,
        "dismissed": True,
    }
    assert "feat" not in fleet_state._PROVISION_INFLIGHT


@pytest.mark.asyncio
async def test_pod_provision_dismiss_cannot_clear_replacement_run(monkeypatch):
    monkeypatch.setattr(fleet_state, "_PROVISION_INFLIGHT", {"feat": "run-new"})

    assert await worktree_ops._pod_provision_dismiss("feat", "run-old") == {
        "ok": True,
        "dismissed": False,
    }
    assert fleet_state._PROVISION_INFLIGHT["feat"] == "run-new"


@pytest.mark.asyncio
async def test_pod_provision_dismiss_refuses_running_run(monkeypatch):
    monkeypatch.setattr(fleet_state, "_PROVISION_INFLIGHT", {"feat": "run-1"})
    monkeypatch.setattr(runtime, "_RUNS", {"run-1": {"status": "running"}})

    result = await worktree_ops._pod_provision_dismiss("feat", "run-1")
    assert result == {"ok": False, "error": "cannot dismiss a running provision"}
    assert fleet_state._PROVISION_INFLIGHT["feat"] == "run-1"


# --------------------------------------------------------------------------
# _disk
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_disk_in_progress_returns_snapshot(monkeypatch):
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "computing", "total_mb": None, "per": {}})
    assert (await fleet_state._disk())["status"] == "computing"


@pytest.mark.asyncio
async def test_disk_done_snapshot_resets_to_idle(monkeypatch):
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "done", "total_mb": 42, "per": {"a": 42}})
    snap = await fleet_state._disk()
    assert snap["total_mb"] == 42
    assert fleet_state._DISK["status"] == "idle"


@pytest.mark.asyncio
async def test_disk_idle_starts_background_aggregation(monkeypatch):
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "idle", "total_mb": None, "per": {}})
    monkeypatch.setattr(
        repository,
        "_discover_worktrees",
        AsyncMock(return_value=[{"path": "/repo/wt-a"}, {"path": "/repo/wt-b"}]),
    )

    async def fake_run(cmd, **kw):
        return (0, "12\t" + cmd[-1], "") if cmd[-1].endswith("wt-a") else (1, "", "err")

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)

    assert await fleet_state._disk() == {"status": "computing", "total_mb": None, "per": {}}
    for _ in range(50):
        if fleet_state._DISK["status"] == "done":
            break
        await asyncio.sleep(0.01)
    assert fleet_state._DISK["per"] == {"wt-a": 12}
    assert fleet_state._DISK["total_mb"] == 12


@pytest.mark.asyncio
async def test_disk_aggregation_failure_reports_unknown(monkeypatch):
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "idle", "total_mb": None, "per": {}})
    monkeypatch.setattr(
        repository, "_discover_worktrees", AsyncMock(side_effect=RuntimeError("git"))
    )

    await fleet_state._disk()
    for _ in range(50):
        if fleet_state._DISK["status"] == "done":
            break
        await asyncio.sleep(0.01)
    assert fleet_state._DISK == {"status": "done", "total_mb": None, "per": {}}


# --------------------------------------------------------------------------
# rebase
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rebase_unknown_worktree(monkeypatch):
    monkeypatch.setattr(repository, "_find_worktree", AsyncMock(return_value=(None, "gone")))
    assert await worktree_ops._rebase("feat") == {"ok": False, "error": "gone"}


@pytest.mark.asyncio
async def test_rebase_refuses_main_checkout(monkeypatch):
    monkeypatch.setattr(
        repository,
        "_find_worktree",
        AsyncMock(return_value=({"path": "/r", "is_main": True}, None)),
    )
    assert await worktree_ops._rebase("main") == {
        "ok": False,
        "error": "refusing to rebase the main checkout",
    }


@pytest.mark.asyncio
async def test_rebase_rejects_concurrent_run(monkeypatch):
    lock = asyncio.Lock()
    await lock.acquire()
    try:
        monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {"feat": lock})
        monkeypatch.setattr(
            repository, "_find_worktree", AsyncMock(return_value=({"path": "/r"}, None))
        )
        assert await worktree_ops._rebase("feat") == {
            "ok": False,
            "error": "rebase already running for this worktree",
        }
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_rebase_locked_unverifiable_state(monkeypatch):
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value=None))
    assert await worktree_ops._rebase_locked({"path": "/r"}) == {
        "ok": False,
        "error": "cannot verify worktree state (git status failed)",
    }


@pytest.mark.asyncio
async def test_rebase_locked_refuses_dirty_worktree(monkeypatch):
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value=" M file.py"))
    # The untracked half of _dirt_report -> _dirty_split now goes through
    # _run_cmd; feed it one untracked path so the detail tail is populated.
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "scratch.log\0", "")))
    res = await worktree_ops._rebase_locked({"path": "/r"})
    assert res["ok"] is False
    # The message no longer equals the bare legacy string: it now appends a
    # dirt-detail tail. The legacy prefix is preserved for clients keying on it.
    assert res["error"].startswith("worktree has uncommitted changes")
    assert "uncommitted changes" in res["error"]
    # And the refusal now carries the structured dirt fields.
    assert res["dirty_tracked"] is True
    assert res["dirty_untracked"] == 1
    assert res["dirty_untracked_paths"] == ["scratch.log"]


@pytest.mark.asyncio
async def test_rebase_locked_fetch_failure(monkeypatch):
    async def fake_git(path, *args, **kw):
        return "" if args[0] == "status" else None

    monkeypatch.setattr(repository, "_git", fake_git)
    res = await worktree_ops._rebase_locked({"path": "/r"})
    assert res["ok"] is False
    assert res["error"] == "git fetch origin main failed"


@pytest.mark.asyncio
async def test_rebase_locked_success(monkeypatch):
    async def fake_git(path, *args, **kw):
        return "" if args[0] == "status" else "ok"

    monkeypatch.setattr(repository, "_git", fake_git)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(
        repository, "_git_info", AsyncMock(return_value={"head": "abc1234", "behind": 0})
    )
    assert await worktree_ops._rebase_locked({"path": "/r"}) == {
        "ok": True,
        "rebased": True,
        "head": "abc1234",
        "behind": 0,
    }


@pytest.mark.asyncio
async def test_rebase_locked_conflict_aborted(monkeypatch):
    async def fake_git(path, *args, **kw):
        return "" if args[0] == "status" else "ok"

    monkeypatch.setattr(repository, "_git", fake_git)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(1, "CONFLICT", "in f.py")))
    res = await worktree_ops._rebase_locked({"path": "/r"})
    assert res["ok"] is False and res["conflict"] is True
    assert "aborted" in res["error"]


@pytest.mark.asyncio
async def test_rebase_locked_conflict_with_failed_abort(monkeypatch):
    """A failed --abort must never be reported as 'aborted'."""

    async def fake_git(path, *args, **kw):
        if args[0] == "status":
            return ""
        if args[0] == "rebase":
            return None
        return "ok"

    monkeypatch.setattr(repository, "_git", fake_git)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(1, "CONFLICT", "")))
    res = await worktree_ops._rebase_locked({"path": "/r"})
    assert res["conflict"] is True
    assert "manual recovery required" in res["error"]


# --------------------------------------------------------------------------
# prune verdicts
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prunable_dirty_check_failure(monkeypatch):
    monkeypatch.setattr(fleet_state, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=None))
    v = await worktree_ops._prunable("/nope/missing", "feat")
    assert v == {**v, "ok": False, "code": "dirty_check_failed"}
    assert v["age_h"] is None


@pytest.mark.asyncio
async def test_prunable_merged_but_dirty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=1))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=True))
    assert (await worktree_ops._prunable(str(tmp_path), "feat"))["code"] == "merged_dirty"


@pytest.mark.asyncio
async def test_prunable_merged_unverified_when_oid_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value=None))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", AsyncMock(return_value=None))
    assert (await worktree_ops._prunable(str(tmp_path), "feat"))["code"] == "merged_unverified"


@pytest.mark.asyncio
async def test_prunable_merged_with_new_commits(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value="aaa"))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", AsyncMock(return_value="bbb"))
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", AsyncMock(return_value=False))
    assert (await worktree_ops._prunable(str(tmp_path), "feat"))["code"] == "merged_new_commits"


@pytest.mark.asyncio
async def test_prunable_merged_clean_is_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value="aaa"))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", AsyncMock(return_value="aaa"))
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", AsyncMock(return_value=True))
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is True and v["code"] == "merged"


@pytest.mark.asyncio
async def test_prunable_fresh_empty_worktree_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(fleet_state, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is False and v["code"] == "fresh"


@pytest.mark.asyncio
async def test_prunable_active_worktree_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "OPEN"}))
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=3))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    assert (await worktree_ops._prunable(str(tmp_path), "feat"))["code"] == "active"


def _bind_closed_head(monkeypatch, *, contained=True, pr_oid="pr-head"):
    """Satisfy the closed verdict's head binding.

    The `closed` verdict is bound to the branch head the same way `merged` is:
    a closed PR is looked up by branch NAME, so a reused branch would otherwise
    inherit a stale CLOSED verdict. These two seams are what that guard reads.
    """
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value="local-head"))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", AsyncMock(return_value=pr_oid))
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", AsyncMock(return_value=contained))


@pytest.mark.asyncio
async def test_prunable_closed_clean_is_candidate_not_merged(monkeypatch, tmp_path):
    """A CLOSED-PR worktree with a clean tree is a `closed` candidate — a
    distinct class from `merged`, so the manual checklist can group and warn on
    it separately."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    _bind_closed_head(monkeypatch)
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is True
    assert v["code"] == "closed"
    # It must NOT be classified as merged — a merged tree's content is on the
    # base branch by definition, a closed one's is not.
    assert v["code"] != "merged"


@pytest.mark.asyncio
async def test_prunable_closed_reused_branch_is_refused(monkeypatch, tmp_path):
    """REGRESSION PIN: a branch REUSED after its PR closed must not inherit the
    stale CLOSED verdict.

    GitHub resolves a closed PR by branch NAME, so a branch that kept moving —
    new commits the closed PR never saw, perhaps a replacement PR not opened yet
    — still returns that CLOSED record. Offering the tree as prunable on that
    basis would destroy work unrelated to the PR that was declined, which is the
    exact data loss this whole class exists to prevent. The local head not being
    contained in the closed PR's head is what detects it."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=5))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    _bind_closed_head(monkeypatch, contained=False)
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is False
    assert v["code"] == "closed_new_commits"
    # The ancestry warning still travels with the refusal.
    assert v["unmerged_commits"] is True


@pytest.mark.asyncio
async def test_prunable_closed_unverifiable_head_is_refused(monkeypatch, tmp_path):
    """Fail-closed: when the closed PR's head OID cannot be established the
    verdict withholds the candidate rather than trusting the name-based lookup.
    An unverifiable guard must never read as a pass."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=1))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    _bind_closed_head(monkeypatch, pr_oid=None)
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is False
    assert v["code"] == "closed_unverified"


@pytest.mark.asyncio
async def test_prunable_closed_ahead_of_base_flags_unmerged_commits(monkeypatch, tmp_path):
    """A clean closed tree whose branch is ahead of base carries the ancestry
    warning (`unmerged_commits`) — a stronger signal than a merged candidate."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=4))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    _bind_closed_head(monkeypatch)
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is True and v["code"] == "closed"
    assert v["unmerged_commits"] is True


@pytest.mark.asyncio
async def test_prunable_closed_clean_no_own_commits_no_unmerged_warning(monkeypatch, tmp_path):
    """A clean closed tree with no commits ahead of base is still a candidate,
    but the unmerged-commits alarm is off."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    _bind_closed_head(monkeypatch)
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["code"] == "closed"
    assert v["unmerged_commits"] is False


@pytest.mark.asyncio
async def test_prunable_closed_dirty_is_refused_with_loss_summary(monkeypatch, tmp_path):
    """A CLOSED-PR worktree with a dirty tree is REFUSED without force
    (code `closed_dirty`), and the verdict names what a removal would lose:
    the tracked-modification flag and the untracked-file count."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=2))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=True))
    # One modified tracked file plus three untracked files — the loss summary.
    monkeypatch.setattr(
        repository,
        "_dirty_split",
        AsyncMock(return_value=(True, ["a.py", "b.py", "c.py"])),
    )
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is False
    assert v["code"] == "closed_dirty"
    # Loss summary: modified tracked files + untracked count are surfaced so the
    # operator decides informed instead of blind-confirming.
    assert v["dirty"] is True
    assert v["dirty_tracked"] is True
    assert v["dirty_untracked"] == 3
    # And the ancestry warning still rides along on the refusal.
    assert v["unmerged_commits"] is True


@pytest.mark.asyncio
async def test_prune_candidates_surfaces_closed_unmerged_flag(monkeypatch):
    """`_prune_candidates` carries the closed candidate's `unmerged_commits`
    flag onto its row (and only onto closed rows, not merged ones)."""
    monkeypatch.setattr(
        repository,
        "_discover_worktrees",
        AsyncMock(
            return_value=[
                {"path": "/r", "is_main": True},
                {"path": "/r/wt-closed", "branch": "c"},
                {"path": "/r/wt-merged", "branch": "m"},
            ]
        ),
    )

    async def fake_prunable(path, branch):
        if branch == "c":
            return {"ok": True, "code": "closed", "unmerged_commits": True}
        return {"ok": True, "code": "merged"}

    monkeypatch.setattr(worktree_ops, "_prunable", fake_prunable)
    out = await worktree_ops._prune_candidates()
    by_name = {c["name"]: c for c in out["candidates"]}
    assert by_name["wt-closed"]["code"] == "closed"
    assert by_name["wt-closed"]["unmerged_commits"] is True
    # A merged row carries no unmerged_commits key (frontend reads its absence
    # as "not applicable").
    assert "unmerged_commits" not in by_name["wt-merged"]


@pytest.mark.asyncio
async def test_prune_candidates_closed_dirty_lands_in_kept_with_loss_counts(monkeypatch):
    """A `closed_dirty` worktree is a KEPT row (refused by default), and its
    dirt breakdown is surfaced so the checklist can show the loss summary."""
    monkeypatch.setattr(
        repository,
        "_discover_worktrees",
        AsyncMock(
            return_value=[
                {"path": "/r", "is_main": True},
                {"path": "/r/wt-cd", "branch": "cd"},
            ]
        ),
    )

    async def fake_prunable(path, branch):
        return {
            "ok": False,
            "code": "closed_dirty",
            "dirty": True,
            "dirty_tracked": True,
            "dirty_untracked": 2,
            "dirty_untracked_paths": ["x", "y"],
            "unmerged_commits": True,
        }

    monkeypatch.setattr(worktree_ops, "_prunable", fake_prunable)
    out = await worktree_ops._prune_candidates()
    assert [c["name"] for c in out["candidates"]] == []
    kept = {k["name"]: k for k in out["kept"]}
    assert kept["wt-cd"]["code"] == "closed_dirty"
    assert kept["wt-cd"]["dirty"] is True
    assert kept["wt-cd"]["dirty_tracked"] is True
    assert kept["wt-cd"]["dirty_untracked"] == 2


@pytest.mark.asyncio
async def test_prunable_passes_full_head_oid_to_pr_status_cached(monkeypatch, tmp_path):
    """Prune cache invalidation uses full commit identity for branch reuse."""
    full_head = "a" * 40
    cache = AsyncMock(return_value=None)
    monkeypatch.setattr(fleet_state, "_pr_status_cached", cache)
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    git = AsyncMock(return_value=full_head)
    monkeypatch.setattr(repository, "_git", git)

    await worktree_ops._prunable(str(tmp_path), "feat")

    git.assert_awaited_once_with(str(tmp_path), "rev-parse", "HEAD")
    cache.assert_awaited_once_with("feat", full_head)


@pytest.mark.asyncio
async def test_prune_candidates_splits_and_skips_main(monkeypatch):
    monkeypatch.setattr(
        repository,
        "_discover_worktrees",
        AsyncMock(
            return_value=[
                {"path": "/r", "is_main": True},
                {"path": "/r/wt-a", "branch": "a"},
                {"path": "/r/wt-b", "branch": "b"},
            ]
        ),
    )

    async def fake_prunable(path, branch):
        ok = branch == "a"
        return {"ok": ok, "code": "merged" if ok else "active"}

    monkeypatch.setattr(worktree_ops, "_prunable", fake_prunable)
    out = await worktree_ops._prune_candidates()
    assert out["scanned"] == 2
    assert [c["name"] for c in out["candidates"]] == ["wt-a"]
    assert [k["name"] for k in out["kept"]] == ["wt-b"]


# --------------------------------------------------------------------------
# fleet cache helpers
# --------------------------------------------------------------------------
def test_drop_worktrees_ignores_malformed_payload():
    assert fleet_state._drop_worktrees({"worktrees": "nope"}, {"x"}) == {"worktrees": "nope"}


def test_drop_worktrees_returns_same_object_when_nothing_matches():
    data = {"worktrees": [{"name": "a"}]}
    assert fleet_state._drop_worktrees(data, {"z"}) is data


def test_drop_worktrees_copies_without_named_rows():
    data = {"worktrees": [{"name": "a"}, {"name": "b"}], "other": 1}
    out = fleet_state._drop_worktrees(data, {"a"})
    assert out["worktrees"] == [{"name": "b"}]
    assert out["other"] == 1
    assert data["worktrees"] == [{"name": "a"}, {"name": "b"}]


@pytest.mark.asyncio
async def test_log_fleet_rebuild_failure_ignores_cancellation():
    async def _never():
        await asyncio.sleep(30)

    task = asyncio.create_task(_never())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    fleet_state._log_fleet_rebuild_failure(task)  # must not raise


@pytest.mark.asyncio
async def test_log_fleet_rebuild_failure_warns_on_exception(caplog):
    async def _boom():
        raise RuntimeError("rebuild died")

    task = asyncio.create_task(_boom())
    try:
        await task
    except RuntimeError:
        pass
    with caplog.at_level("WARNING", logger=runtime.logger.name):
        fleet_state._log_fleet_rebuild_failure(task)
    assert "rebuild died" in caplog.text


# --------------------------------------------------------------------------
# GET handlers
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fleet_handler_reports_discovery_error(monkeypatch):
    monkeypatch.setattr(fleet_state, "_fleet_cached", AsyncMock(side_effect=RuntimeError("no git")))
    resp = await http_api.api_dev_fleet_fleet(make_mocked_request("GET", "/api/fleet"))
    assert json.loads(resp.text) == {"worktrees": [], "error": "no git"}


@pytest.mark.asyncio
async def test_fleet_handler_fresh_bypasses_cache(monkeypatch):
    refresh = AsyncMock(return_value={"worktrees": [{"name": "a"}]})
    monkeypatch.setattr(fleet_state, "_fleet_refresh", refresh)
    monkeypatch.setattr(fleet_state, "_fleet_cached", AsyncMock(return_value={"worktrees": []}))
    resp = await http_api.api_dev_fleet_fleet(make_mocked_request("GET", "/api/fleet?fresh=1"))
    # The row also carries the request-time `provision_run_id` overlay, which is
    # authoritative: a provision that finished after the snapshot was built has
    # no reattachable run, so the pointer must read None rather than the id the
    # snapshot froze.
    assert json.loads(resp.text)["worktrees"] == [{"name": "a", "provision_run_id": None}]
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_fleet_handler_overlays_runs_started_after_snapshot(monkeypatch):
    """A run started AFTER the cached snapshot was built is still reported.

    The fleet cache is stale-while-revalidate, so a run pointer baked into the
    snapshot left a freshly-mounted page with nothing to reattach its progress
    stepper to for a full cache cycle plus a rebuild -- no progress, and a button
    still inviting a second press. Both pointers share that cause: the sync's,
    and each row's provision run.
    """
    cached = {
        "worktrees": [
            {"name": "main", "provision_run_id": None},
            {"name": "feature-x", "provision_run_id": None},
        ],
        "sync_run_id": None,
    }
    monkeypatch.setattr(fleet_state, "_fleet_cached", AsyncMock(return_value=cached))
    monkeypatch.setattr(worktree_ops, "_SYNC_RID", "rid-after-snapshot")
    monkeypatch.setattr(
        fleet_state, "_provision_reattach_ids", AsyncMock(return_value={"feature-x": "prov-rid-7"})
    )
    resp = await http_api.api_dev_fleet_fleet(make_mocked_request("GET", "/api/fleet"))
    body = json.loads(resp.text)
    assert body["sync_run_id"] == "rid-after-snapshot"
    rows = {w["name"]: w["provision_run_id"] for w in body["worktrees"]}
    assert rows == {"main": None, "feature-x": "prov-rid-7"}
    # The snapshot and its rows are the cache's own objects, shared with every
    # other in-flight request, so the overlay must copy rather than write through.
    assert cached["sync_run_id"] is None
    assert [w["provision_run_id"] for w in cached["worktrees"]] == [None, None]


@pytest.mark.asyncio
async def test_worktree_handler_requires_name():
    resp = await http_api.api_dev_fleet_worktree(make_mocked_request("GET", "/api/worktree"))
    assert resp.status == 400
    assert "missing 'name'" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_worktree_handler_rejects_unknown_name(monkeypatch):
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"other"}))
    resp = await http_api.api_dev_fleet_worktree(make_mocked_request("GET", "/api/worktree?name=x"))
    assert resp.status == 400
    assert "unknown worktree" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_worktree_handler_returns_detail(monkeypatch):
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"x"}))
    monkeypatch.setattr(fleet_state, "_worktree_detail", AsyncMock(return_value={"name": "x"}))
    resp = await http_api.api_dev_fleet_worktree(make_mocked_request("GET", "/api/worktree?name=x"))
    assert json.loads(resp.text) == {"name": "x"}


@pytest.mark.asyncio
async def test_pod_logs_handler_requires_name():
    resp = await http_api.api_dev_fleet_pod_logs(make_mocked_request("GET", "/api/pod/logs"))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_pod_logs_handler_rejects_unknown_name(monkeypatch):
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value=set()))
    resp = await http_api.api_dev_fleet_pod_logs(make_mocked_request("GET", "/api/pod/logs?name=x"))
    assert resp.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_n"),
    [("", 120), ("&n=notanint", 120), ("&n=0", 1), ("&n=99999", 1000), ("&n=25", 25)],
)
async def test_pod_logs_handler_clamps_line_count(monkeypatch, query, expected_n):
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"x"}))
    seen: list[int] = []

    async def fake_logs(name, n):
        seen.append(n)
        return {"ok": True, "logs": ""}

    monkeypatch.setattr(worktree_ops, "_pod_logs", fake_logs)
    await http_api.api_dev_fleet_pod_logs(
        make_mocked_request("GET", f"/api/pod/logs?name=x{query}")
    )
    assert seen == [expected_n]


@pytest.mark.asyncio
async def test_run_handler_requires_id():
    resp = await http_api.api_dev_fleet_run(make_mocked_request("GET", "/api/run"))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_run_handler_unknown_id_is_404(monkeypatch):
    monkeypatch.setattr(runtime, "_RUNS", {})
    resp = await http_api.api_dev_fleet_run(make_mocked_request("GET", "/api/run?id=nope"))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_run_handler_tails_and_redacts_output(monkeypatch):
    lines = [f"line {i}" for i in range(80)]
    monkeypatch.setattr(runtime, "_RUNS", {"r1": {"status": "running", "output": lines}})
    resp = await http_api.api_dev_fleet_run(make_mocked_request("GET", "/api/run?id=r1"))
    payload = json.loads(resp.text)
    assert len(payload["output"]) == 60
    assert payload["output"][0] == "line 20"


@pytest.mark.asyncio
async def test_prune_and_disk_handlers_pass_through(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_prune_candidates", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(worktree_ops, "_prune_status", AsyncMock(return_value={"running": False}))
    monkeypatch.setattr(fleet_state, "_disk", AsyncMock(return_value={"status": "idle"}))

    r1 = await http_api.api_dev_fleet_prune_candidates(
        make_mocked_request("GET", "/api/prune-candidates")
    )
    r2 = await http_api.api_dev_fleet_prune_status(make_mocked_request("GET", "/api/prune-status"))
    r3 = await http_api.api_dev_fleet_disk(make_mocked_request("GET", "/api/disk"))
    assert json.loads(r1.text) == {"ok": True}
    assert json.loads(r2.text) == {"running": False}
    assert json.loads(r3.text) == {"status": "idle"}


# --------------------------------------------------------------------------
# _json_body + POST handler validation
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_json_body_rejects_invalid_json():
    body, err = await http_api._json_body(_raw_request(b"{oops", json_error=ValueError("bad")))
    assert body is None and err is not None and err.status == 400


@pytest.mark.asyncio
async def test_json_body_rejects_non_object():
    body, err = await http_api._json_body(_raw_request(b"[1, 2]"))
    assert body is None and err is not None
    assert "must be an object" in json.loads(err.text)["error"]


@pytest.mark.asyncio
async def test_json_body_empty_request_is_empty_dict():
    request = MagicMock()
    request.content_length = 0
    body, err = await http_api._json_body(request)
    assert body == {} and err is None


@pytest.mark.asyncio
async def test_json_body_unknown_charset_is_400_not_500():
    # An unknown ``charset=`` codec makes aiohttp's decode step raise LookupError,
    # not JSONDecodeError. The catch was ValueError-only, so this used to escape as
    # a 500; it is a client-input mistake and must answer 400. Guards the widened
    # (LookupError, RecursionError, ValueError) catch against a regression.
    body, err = await http_api._json_body(
        _raw_request(b"{}", json_error=LookupError("unknown encoding: bogus-codec"))
    )
    assert body is None and err is not None and err.status == 400


@pytest.mark.asyncio
async def test_worktree_remove_handler_rejects_non_bool_force(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    resp = await http_api.api_dev_fleet_worktree_remove(
        _json_request({"name": "feat", "force": "yes"})
    )
    assert resp.status == 400
    assert "force must be a boolean" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_worktree_remove_handler_forwards_force(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    remove = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(worktree_ops, "_worktree_remove", remove)

    resp = await http_api.api_dev_fleet_worktree_remove(
        _json_request({"name": "feat", "force": True})
    )
    assert resp.status == 200
    remove.assert_awaited_once_with("feat", True, discard_untracked_paths=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["\x00", "a\x00b", "", 7])
async def test_make_live_handler_rejects_malformed_expected_staged(monkeypatch, bad):
    """A NUL byte in expected_staged would reach Path.resolve() and raise
    ValueError into a 500; the handler must refuse it (and the other
    malformed shapes) with a 400 before _make_live ever runs."""
    _sel_capture(monkeypatch)
    make_live = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(live, "_make_live", make_live)

    resp = await http_api.api_dev_fleet_make_live(
        _json_request({"path": "/w/x", "expected_staged": bad})
    )
    assert resp.status == 400
    assert "expected_staged" in json.loads(resp.text)["error"]
    make_live.assert_not_awaited()


def test_same_path_survives_unresolvable_operands(tmp_path):
    """Defense in depth behind the handler gate: an operand Path.resolve()
    rejects (embedded NUL, or a symlink loop — RuntimeError on some
    platform/version combinations) means "not the same path", never a crash."""
    assert repository._same_path("\x00", "/tmp") is False
    assert repository._same_path("/tmp", "a\x00b") is False
    if sys.platform != "win32":
        a, b = tmp_path / "loop-a", tmp_path / "loop-b"
        a.symlink_to(b)
        b.symlink_to(a)
        assert repository._same_path(str(a), str(tmp_path)) is False


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_invalid_json(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(_raw_request(b"{", json_error=ValueError("bad")))
    assert resp.status == 400
    assert json.loads(resp.text)["ok"] is False


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_non_object_body(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(_raw_request(b"[]"))
    assert resp.status == 400
    assert "must be an object" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_non_string_names(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(_json_request({"names": ["a", 7]}))
    assert resp.status == 400
    assert "list of strings" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_when_no_name_is_valid(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"other"}))
    resp = await http_api.api_dev_fleet_prune_run(_json_request({"names": ["ghost"]}))
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "no valid names"


@pytest.mark.asyncio
async def test_prune_run_handler_filters_to_valid_names(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"a"}))
    run = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(worktree_ops, "_prune_run", run)

    resp = await http_api.api_dev_fleet_prune_run(_json_request({"names": ["a", "ghost"]}))
    assert resp.status == 200
    run.assert_awaited_once_with(["a"])


@pytest.mark.asyncio
async def test_pod_name_action_rejects_empty_name(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_pod_up(_json_request({"name": ""}))
    assert resp.status == 400
    assert "non-empty string" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_pod_name_action_rejects_ambiguous_basename(monkeypatch):
    """_find_worktree, not set membership, is the validator (collision safety)."""
    _sel_capture(monkeypatch)
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=(None, "ambiguous name 'feat'"))
    )
    resp = await http_api.api_dev_fleet_pod_restart(_json_request({"name": "feat"}))
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "ambiguous name 'feat'"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "action_name"),
    [
        ("api_dev_fleet_pod_up", "_pod_up"),
        ("api_dev_fleet_pod_down", "_pod_down"),
        ("api_dev_fleet_pod_restart", "_pod_restart"),
        ("api_dev_fleet_pod_token", "_pod_token"),
        ("api_dev_fleet_pod_provision", "_pod_provision"),
        ("api_dev_fleet_rebase", "_rebase"),
    ],
)
async def test_pod_handlers_dispatch_to_their_action(monkeypatch, handler_name, action_name):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    action = AsyncMock(return_value={"ok": True, "via": action_name})
    monkeypatch.setattr(worktree_ops, action_name, action)

    resp = await getattr(mod, handler_name)(_json_request({"name": "feat"}))
    assert json.loads(resp.text) == {"ok": True, "via": action_name}
    action.assert_awaited_once_with("feat")


@pytest.mark.asyncio
async def test_pod_provision_dismiss_handler_validates_and_dispatches(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    dismiss = AsyncMock(return_value={"ok": True, "dismissed": True})
    monkeypatch.setattr(worktree_ops, "_pod_provision_dismiss", dismiss)

    resp = await http_api.api_dev_fleet_pod_provision_dismiss(
        _json_request({"name": "feat", "run_id": "run-1"})
    )

    assert json.loads(resp.text) == {"ok": True, "dismissed": True}
    dismiss.assert_awaited_once_with("feat", "run-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw, json_error",
    [
        (b"{oops", ValueError("bad json")),
        (b"[1, 2]", None),
    ],
)
async def test_pod_provision_dismiss_handler_codes_a_malformed_body(monkeypatch, raw, json_error):
    """Every rejection from this endpoint carries a machine-readable code.

    A malformed or non-object body is rejected by the shared body parser, whose
    default 400 has no ``code``; the dismiss handler asks for one so a client
    can branch on the failure instead of matching prose.
    """
    _sel_capture(monkeypatch)
    dismiss = AsyncMock()
    monkeypatch.setattr(worktree_ops, "_pod_provision_dismiss", dismiss)

    resp = await http_api.api_dev_fleet_pod_provision_dismiss(
        _raw_request(raw, json_error=json_error)
    )

    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_body"
    dismiss.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_handler_maps_already_running_to_409(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(
        worktree_ops,
        "_sync",
        AsyncMock(return_value={"ok": False, "error": "sync already running"}),
    )
    request = MagicMock()
    request.content_length = 0
    request.can_read_body = False
    resp = await http_api.api_dev_fleet_sync(request)
    assert resp.status == 409


@pytest.mark.asyncio
async def test_sync_handler_success_is_200(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(worktree_ops, "_sync", AsyncMock(return_value={"ok": True}))
    request = MagicMock()
    request.content_length = 0
    request.can_read_body = False
    resp = await http_api.api_dev_fleet_sync(request)
    assert resp.status == 200


# --------------------------------------------------------------------------
# make-live handler
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_make_live_handler_requires_path(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_make_live(_json_request({"path": ""}))
    assert resp.status == 400
    assert "non-empty string" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_make_live_handler_rejects_non_bool_dry_run(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_make_live(_json_request({"path": "/w", "dry_run": "1"}))
    assert resp.status == 400
    assert "dry_run must be a boolean" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_make_live_handler_forwards_dry_run(monkeypatch):
    sink = _sel_capture(monkeypatch)
    make_live = AsyncMock(return_value={"ok": True, "dry_run": True})
    monkeypatch.setattr(live, "_make_live", make_live)

    resp = await http_api.api_dev_fleet_make_live(_json_request({"path": "/w", "dry_run": True}))
    assert resp.status == 200
    make_live.assert_awaited_once_with("/w", True, expected_staged=None)
    assert sink.events[0]["resources"] == "/w"


# --------------------------------------------------------------------------
# _audited
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_audited_bodyless_request_has_empty_target(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    request = MagicMock()
    request.content_length = 0
    request.can_read_body = False
    await handler(request)
    assert sink.events[0]["resources"] == ""
    assert sink.events[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_audited_joins_list_target(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    await handler(_json_request({"names": ["a", "b", "c"]}))
    assert sink.events[0]["resources"] == "a,b,c"


@pytest.mark.asyncio
async def test_audited_ignores_unparsable_body(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    await handler(_raw_request(b"not json at all"))
    assert sink.events[0]["resources"] == ""


@pytest.mark.asyncio
async def test_audited_non_dict_body_has_empty_target(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    await handler(_raw_request(b"[1,2,3]"))
    assert sink.events[0]["resources"] == ""


@pytest.mark.asyncio
async def test_audited_read_failure_does_not_break_handler(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    request = MagicMock()
    request.content_length = 5
    request.can_read_body = True
    request.read = AsyncMock(side_effect=RuntimeError("stream gone"))
    await handler(request)
    assert sink.events[0]["resources"] == ""


@pytest.mark.asyncio
async def test_audited_handler_exception_is_audited_and_reraised(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        raise KeyError("kaboom")

    with pytest.raises(KeyError):
        await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "failure"
    assert sink.events[0]["error"] == "KeyError"


@pytest.mark.asyncio
async def test_audited_server_error_is_failure(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({"error": "internal"}, status=500)

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "failure"
    assert sink.events[0]["error"] == "internal"


@pytest.mark.asyncio
async def test_audited_non_json_response_still_audits_success(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.Response(text="plain body")

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_audited_non_dict_json_response_is_success(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response([1, 2])

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_audited_error_free_denial_falls_back_to_status(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @http_api._audited("probe")
    async def handler(request):
        return web.json_response({}, status=403)

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "denied"
    assert sink.events[0]["error"] == "http_403"


@pytest.mark.asyncio
async def test_audited_preserves_handler_identity():
    async def original(request):
        """Docstring stays."""
        return web.json_response({})

    wrapped = http_api._audited("probe")(original)
    assert wrapped.__name__ == "original"
    assert wrapped.__doc__ == "Docstring stays."


# --------------------------------------------------------------------------
# HMAC middleware denials
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hmac_health_path_is_exempt(monkeypatch):
    called: list[bool] = []

    async def handler(request):
        called.append(True)
        return web.json_response({"status": "ok"})

    await http_api.hmac_proxy_middleware(make_mocked_request("GET", "/health"), handler)
    assert called == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "secret", "reason"),
    [
        ({}, "", "no app secret configured"),
        ({}, "s3cr3t", "missing X-KiroCrew-Proxy header"),
        ({"X-KiroCrew-Proxy": "nocolon"}, "s3cr3t", "malformed X-KiroCrew-Proxy header"),
        ({"X-KiroCrew-Proxy": "abc:sig"}, "s3cr3t", "invalid timestamp in proxy header"),
        ({"X-KiroCrew-Proxy": "1:sig"}, "s3cr3t", "proxy signature expired"),
    ],
)
async def test_hmac_denials(monkeypatch, headers, secret, reason):
    sink = _sel_capture(monkeypatch)
    monkeypatch.setattr(http_api, "_load_app_secret", lambda: secret)

    async def handler(request):  # pragma: no cover - must never run
        raise AssertionError("handler must not be reached")

    request = make_mocked_request("GET", "/api/fleet", headers=headers)
    resp = await http_api.hmac_proxy_middleware(request, handler)
    assert resp.status == 401
    assert reason in json.loads(resp.text)["error"]
    assert sink.events[0]["outcome"] == "denied"
    assert sink.events[0]["tool_name"] == "dev-fleet:proxy-hmac"


@pytest.mark.asyncio
async def test_hmac_denial_survives_audit_sink_failure(monkeypatch, caplog):
    """A broken SEL sink must never mask the 401."""

    def _boom():
        raise RuntimeError("sel down")

    monkeypatch.setattr(runtime, "_sel", _boom)
    monkeypatch.setattr(http_api, "_load_app_secret", lambda: "s3cr3t")

    async def handler(request):  # pragma: no cover - must never run
        raise AssertionError("handler must not be reached")

    with caplog.at_level("WARNING", logger=runtime.logger.name):
        resp = await http_api.hmac_proxy_middleware(
            make_mocked_request("GET", "/api/fleet"), handler
        )
    assert resp.status == 401
    assert "SEL emit failed" in caplog.text


# --------------------------------------------------------------------------
# gateway identity helpers
# --------------------------------------------------------------------------
def test_gateway_unit_name_defaults_to_live_unit(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path / "home")
    assert live._gateway_unit_name() == live._LIVE_GATEWAY_UNIT


def test_gateway_unit_name_uses_pod_instance(monkeypatch, tmp_path):
    home = tmp_path / ".kirocrew-pods" / "feat"
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)
    assert live._gateway_unit_name() == "kirocrew-pod@feat.service"


def test_gateway_unit_name_falls_back_when_home_unresolvable(monkeypatch):
    def _boom():
        raise RuntimeError("no home")

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", _boom)
    assert live._gateway_unit_name() == live._LIVE_GATEWAY_UNIT


def test_gateway_label_defaults_to_live_agent(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path / "home")
    assert live._gateway_label() == live._LIVE_GATEWAY_LABEL


def test_gateway_label_uses_pod_agent(monkeypatch, tmp_path):
    launchd = pytest.importorskip("kiro_crew.pod.launchd")
    pod_config = pytest.importorskip("kiro_crew.pod.config")
    home = tmp_path / ".kirocrew-pods" / "feat"
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)
    monkeypatch.delenv("KIROCREW_POD_UNIT_PREFIX", raising=False)
    expected = f"{launchd.LABEL_PREFIX}.{pod_config.DEFAULT_UNIT_PREFIX}.feat"
    assert live._gateway_label() == expected


def test_gateway_label_honours_unit_prefix_override(monkeypatch, tmp_path):
    launchd = pytest.importorskip("kiro_crew.pod.launchd")
    home = tmp_path / ".kirocrew-pods" / "feat"
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)
    monkeypatch.setenv("KIROCREW_POD_UNIT_PREFIX", "altplane")
    assert live._gateway_label() == f"{launchd.LABEL_PREFIX}.altplane.feat"


@pytest.mark.parametrize(
    ("home_parts", "expected"),
    [((".kirocrew-pods", "feat"), True), (("home", "kirocrew"), False)],
)
def test_in_pod_detection(monkeypatch, tmp_path, home_parts, expected):
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_dir", lambda: tmp_path.joinpath(*home_parts)
    )
    assert live._in_pod() is expected


def test_in_pod_is_none_when_home_unresolvable(monkeypatch):
    def _boom():
        raise RuntimeError("no home")

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", _boom)
    assert live._in_pod() is None


def test_foreground_backend_is_none_off_posix(monkeypatch):
    monkeypatch.setattr(live.sys, "platform", "win32")
    assert live._foreground_backend() is None


# --------------------------------------------------------------------------
# drop-in rollback + path selector
# --------------------------------------------------------------------------
def _systemd_backend(dropin: Path) -> gateway_service.SystemdBackend:
    """A SystemdBackend whose drop-in path is *dropin*.

    ``rollback`` touches only the injected drop-in path and the module-level
    ``atomic_write_text``, so the remaining seams stay inert stubs.
    """
    return gateway_service.SystemdBackend(
        AsyncMock(return_value=(0, "", "")),
        lambda: "kirocrew.service",
        platform="linux",
        which=lambda _name: "/usr/bin/systemctl",
        dropin_path=lambda: dropin,
        dropin_content=lambda _wt, _kcbin: "",
    )


def test_service_rollback_deletes_the_dropin_when_there_was_none(tmp_path):
    """No prior drop-in -> the file staged over it is removed, not left behind."""
    dropin = tmp_path / "make-live.conf"
    dropin.write_text("[Service]\n", encoding="utf-8", newline="\n")
    assert _systemd_backend(dropin).rollback(None) is True
    assert not dropin.exists()


def test_service_rollback_rewrites_prior_content(tmp_path):
    """A prior drop-in -> its exact content is restored over the staged one."""
    dropin = tmp_path / "make-live.conf"
    dropin.write_text("new\n", encoding="utf-8", newline="\n")
    assert _systemd_backend(dropin).rollback("prior\n") is True
    assert dropin.read_text(encoding="utf-8") == "prior\n"


def test_service_rollback_reports_failure(monkeypatch, tmp_path):
    """A failed restore returns False so the caller can report
    ``rolled_back: false`` rather than claiming a rollback that did not land."""

    def _boom(path, content):
        raise OSError("read-only fs")

    monkeypatch.setattr(gateway_service, "atomic_write_text", _boom)
    backend = _systemd_backend(tmp_path / "make-live.conf")
    assert backend.rollback("prior") is False


@pytest.mark.asyncio
async def test_find_worktree_by_path_requires_path():
    wt, err = await repository._find_worktree_by_path("")
    assert wt is None
    assert err is not None and "non-empty string" in err


@pytest.mark.asyncio
async def test_find_worktree_by_path_unknown_path(monkeypatch, tmp_path):
    monkeypatch.setattr(repository, "_discover_worktrees", AsyncMock(return_value=[]))
    wt, err = await repository._find_worktree_by_path(str(tmp_path / "nope"))
    assert wt is None
    assert err is not None and "not a known worktree" in err


@pytest.mark.asyncio
async def test_find_worktree_by_path_matches_known_worktree(monkeypatch, tmp_path):
    wanted = tmp_path / "kirocrew-wt-alpha"
    wanted.mkdir()
    monkeypatch.setattr(
        repository,
        "_discover_worktrees",
        AsyncMock(return_value=[{"path": str(tmp_path / "other")}, {"path": str(wanted)}]),
    )
    wt, err = await repository._find_worktree_by_path(str(wanted))
    assert err is None
    assert wt is not None and _same(wt["path"], str(wanted))


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cleanup_kills_runs_and_cancels_workers(monkeypatch):
    async def _never():
        await asyncio.sleep(30)

    run_task = asyncio.create_task(_never())
    refresher = asyncio.create_task(_never())
    await asyncio.sleep(0)

    proc = MagicMock()
    proc.returncode = None
    proc.pid = 4242
    kill_tree = AsyncMock()
    monkeypatch.setattr(runtime, "_kill_tree", kill_tree)
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {"r1": (run_task, proc)})
    monkeypatch.setattr(worktree_ops, "_refresher_task", refresher)
    monkeypatch.setattr(worktree_ops, "_warm_task", None)
    monkeypatch.setattr(worktree_ops, "_reaper_task", None)

    await mod.dev_fleet_cleanup(MagicMock())

    kill_tree.assert_awaited_once_with(4242)
    proc.kill.assert_called_once()
    assert runtime._ACTIVE_RUNS == {}
    assert run_task.cancelled() or run_task.done()
    assert refresher.cancelled() or refresher.done()
    assert worktree_ops._refresher_task is None


@pytest.mark.asyncio
async def test_cleanup_tolerates_already_dead_process(monkeypatch):
    async def _never():
        await asyncio.sleep(30)

    run_task = asyncio.create_task(_never())
    await asyncio.sleep(0)

    proc = MagicMock()
    proc.returncode = None
    proc.pid = 77
    proc.kill.side_effect = ProcessLookupError
    monkeypatch.setattr(runtime, "_kill_tree", AsyncMock())
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {"r1": (run_task, proc)})
    monkeypatch.setattr(worktree_ops, "_refresher_task", None)
    monkeypatch.setattr(worktree_ops, "_warm_task", None)
    monkeypatch.setattr(worktree_ops, "_reaper_task", None)

    await mod.dev_fleet_cleanup(MagicMock())
    assert runtime._ACTIVE_RUNS == {}


@pytest.mark.asyncio
async def test_cleanup_skips_finished_process(monkeypatch):
    async def _done():
        return None

    run_task = asyncio.create_task(_done())
    await run_task

    proc = MagicMock()
    proc.returncode = 0
    kill_tree = AsyncMock()
    monkeypatch.setattr(runtime, "_kill_tree", kill_tree)
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {"r1": (run_task, proc)})
    monkeypatch.setattr(worktree_ops, "_refresher_task", None)
    monkeypatch.setattr(worktree_ops, "_warm_task", None)
    monkeypatch.setattr(worktree_ops, "_reaper_task", None)

    await mod.dev_fleet_cleanup(MagicMock())
    kill_tree.assert_not_awaited()
    proc.kill.assert_not_called()


# --------------------------------------------------------------------------
# app factory + entry point
# --------------------------------------------------------------------------
def test_create_app_registers_lifecycle_hooks_by_name():
    app = mod.create_app()
    startup_names = [getattr(h, "__name__", "") for h in app.on_startup]
    cleanup_names = [getattr(h, "__name__", "") for h in app.on_cleanup]
    assert "dev_fleet_startup" in startup_names
    assert "dev_fleet_cleanup" in cleanup_names


def test_create_app_exposes_health_on_both_paths():
    app = mod.create_app()
    paths = {r.resource.canonical for r in app.router.routes() if r.resource is not None}
    assert {"/health", "/api/health"} <= paths
    assert "/api/make-live" in paths


def test_main_runs_the_app_on_loopback(monkeypatch):
    seen: dict = {}

    def fake_run_app(app, host=None, port=None, print=None):
        seen.update({"app": app, "host": host, "port": port})

    monkeypatch.setattr(mod.web, "run_app", fake_run_app)
    assert mod.main() == 0
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == http_api.PORT


# --------------------------------------------------------------------------
# worktree detail
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worktree_detail_unknown_name(monkeypatch):
    monkeypatch.setattr(repository, "_find_worktree", AsyncMock(return_value=(None, "gone")))
    assert await fleet_state._worktree_detail("ghost") == {"error": "gone"}


@pytest.mark.asyncio
async def test_worktree_detail_includes_pod_state_and_design_docs(monkeypatch, tmp_path):
    wt = {"path": str(tmp_path), "branch": "feat", "is_main": False}
    monkeypatch.setattr(repository, "_find_worktree", AsyncMock(return_value=(wt, None)))
    monkeypatch.setattr(
        repository,
        "_git_info",
        AsyncMock(
            return_value={
                "branch": "feat",
                "head": "abc1234",
                "dirty": False,
                "ahead": 0,
                "behind": 0,
                "last_updated_at": None,
            }
        ),
    )
    monkeypatch.setattr(fleet_state, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=1))
    monkeypatch.setattr(fleet_state, "_context_cached", AsyncMock(return_value={}))

    async def fake_git(path, *args, **kw):
        if args[0] == "log":
            return "abc1234\x1ffeat: do a thing\x1f2 hours ago\nmalformed line"
        if args[0] == "diff":
            return "docs/design/plan.md\nsrc/app.py\ndocs/design/plan.md\n"
        return ""

    monkeypatch.setattr(repository, "_git", fake_git)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "31\t.", "")))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    name = Path(str(tmp_path)).name
    monkeypatch.setattr(
        runtime,
        "rt",
        SimpleNamespace(
            active_names=lambda cfg: {name},
            derive_port=lambda cfg, nm: 9321,
        ),
        raising=False,
    )

    detail = await fleet_state._worktree_detail(name)
    assert detail["pod_running"] is True
    assert detail["pod_port"] == 9321
    assert detail["disk_mb"] == 31
    assert detail["commits"] == [
        {"hash": "abc1234", "subject": "feat: do a thing", "when": "2 hours ago"}
    ]
    assert detail["design_docs"] == ["docs/design/plan.md"]


@pytest.mark.asyncio
async def test_worktree_detail_survives_pod_probe_failure(monkeypatch, tmp_path):
    wt = {"path": str(tmp_path), "branch": None, "is_main": True}
    monkeypatch.setattr(repository, "_find_worktree", AsyncMock(return_value=(wt, None)))
    monkeypatch.setattr(
        repository,
        "_git_info",
        AsyncMock(
            return_value={
                "branch": "main",
                "head": "abc1234",
                "dirty": False,
                "ahead": 0,
                "behind": 0,
                "last_updated_at": None,
            }
        ),
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(fleet_state, "_context_cached", AsyncMock(return_value={}))
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value=""))
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(1, "", "du failed")))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: None)

    detail = await fleet_state._worktree_detail(Path(str(tmp_path)).name)
    assert detail["pod_running"] is False
    assert detail["disk_mb"] is None
    assert detail["commits"] == []


def test_main_boots_platform_before_serving(monkeypatch):
    """main() must install the platform context BEFORE create_app/run_app.

    The app backend launches this module as its own subprocess, which inherits
    no installed platform context. If it served without booting, the first read
    of current_context() (e.g. the sandbox floor resolved while wrapping the
    app's own git worktree scan) would raise on a non-standalone edition and the
    error would surface verbatim in the UI. The ORDER is the invariant: boot
    resolves the profile and installs the context, so create_app never runs
    against a cold context.
    """
    calls: list[str] = []

    def _fake_boot(_cfg):
        calls.append("boot")
        return SimpleNamespace()

    monkeypatch.setattr(mod, "boot_platform", _fake_boot)
    monkeypatch.setattr(mod.KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace()))
    monkeypatch.setattr(mod, "create_app", lambda: calls.append("create_app") or MagicMock())
    monkeypatch.setattr(mod.web, "run_app", lambda *a, **k: calls.append("run_app"))

    assert mod.main() == 0
    assert calls == ["boot", "create_app", "run_app"]


def test_main_fails_closed_when_platform_cannot_compose(monkeypatch):
    """A composition failure must ABORT main(), never serve a cold backend.

    Mirrors the CLI entry point's fail-closed posture: a non-standalone profile
    whose companion cannot compose must not fall through to serving the backend
    with no security overlay.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    def _boom(_cfg):
        raise PlatformCompositionError("companion missing")

    served: list[str] = []
    monkeypatch.setattr(mod, "boot_platform", _boom)
    monkeypatch.setattr(mod.KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace()))
    monkeypatch.setattr(mod, "create_app", lambda: served.append("create_app") or MagicMock())
    monkeypatch.setattr(mod.web, "run_app", lambda *a, **k: served.append("run_app"))

    with pytest.raises(PlatformCompositionError):
        mod.main()
    assert served == []


# ==========================================================================
# Untracked-discard contract: _dirty_split classification, the refusal
# payload builders, and the late-executed `git clean` discard path.
#
# Every helper the removal touches is stubbed with AsyncMock; no real git and
# no real subprocess. `_git` is driven by a per-subcommand fake so a test can
# say "status -uno is clean, ls-files lists two files" without parsing argv by
# position.
# ==========================================================================
from contextlib import ExitStack, contextmanager  # noqa: E402
from unittest.mock import patch  # noqa: E402


def _git_by_subcommand(mapping, default=""):
    """Build an async `_git` double keyed on the git subcommand.

    `mapping` maps a subcommand token (``"status"``, ``"ls-files"``,
    ``"rev-parse"``, ``"clean"``) to the string (or None) `_git` should return.
    The first positional arg is the worktree path; the next is the subcommand.
    """

    async def _fake_git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub in mapping:
            return mapping[sub]
        return default

    return _fake_git


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_out, lsfiles_out, expect_tracked, expect_untracked",
    [
        # tracked-only: status prints a change, ls-files finds nothing
        (" M src/a.py\n", "", True, []),
        # untracked-only: status is empty, ls-files lists paths (NUL-joined)
        ("", "note.txt\0probe.sh\0", False, ["note.txt", "probe.sh"]),
        # both: a tracked change AND untracked files
        (" M src/a.py\n", "scratch.log\0", True, ["scratch.log"]),
        # status could not answer -> None (unverifiable, never "clean")
        (None, "", None, []),
    ],
)
async def test_dirty_split_classifies_tracked_vs_untracked(
    status_out, lsfiles_out, expect_tracked, expect_untracked
):
    # The tracked half still goes through `_git status`; the untracked half now
    # goes through `_run_cmd ls-files` directly (returning (rc, stdout, stderr))
    # so a leading/trailing space on a filename survives the `_git` strip.
    git = _git_by_subcommand({"status": status_out})

    async def run_cmd(cmd, timeout=None, **kw):
        assert "ls-files" in cmd, cmd
        return (0, lsfiles_out, "")

    with (
        patch.object(repository, "_git", side_effect=git),
        patch.object(runtime, "_run_cmd", side_effect=run_cmd),
    ):
        tracked, untracked = await repository._dirty_split("/wt/x")
    assert tracked is expect_tracked
    assert untracked == expect_untracked


@pytest.mark.asyncio
async def test_dirty_split_lsfiles_failure_is_fail_closed_empty():
    """`ls-files` returning nonzero rc must yield an EMPTY untracked list -- an
    empty list is "nothing approved to discard", never a promise the tree is
    clean. tracked_dirty stays authoritative from the status query."""
    git = _git_by_subcommand({"status": " M a.py\n"})

    async def run_cmd(cmd, timeout=None, **kw):
        assert "ls-files" in cmd, cmd
        return (1, "", "fatal: not a git repo")

    with (
        patch.object(repository, "_git", side_effect=git),
        patch.object(runtime, "_run_cmd", side_effect=run_cmd),
    ):
        tracked, untracked = await repository._dirty_split("/wt/x")
    assert tracked is True
    assert untracked == []


@contextmanager
def _remove_stubs(
    *,
    git,
    run_cmd,
    pr_state,
    own=1,
    contained=True,
    target=None,
):
    """Stub every helper `_worktree_remove_locked` calls on the happy path up to
    the removal, leaving `_git` and `_run_cmd` to the caller so ordering and
    argv can be asserted.

    ``target`` overrides the discovered worktree record, for cases that turn on
    a flag git reports there (a `locked` tree, for instance).
    """
    stack = ExitStack()
    stack.enter_context(
        patch.object(
            repository,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=(
                target or {"path": "/wt/feat", "branch": "feat/x", "is_main": False},
                None,
            ),
        )
    )
    stack.enter_context(
        patch.object(live, "_live_worktree_path", new_callable=AsyncMock, return_value=None)
    )
    stack.enter_context(patch.object(live, "_own_checkout_path", return_value=None))
    stack.enter_context(
        patch.object(repository, "_real_dirty", new_callable=AsyncMock, return_value=True)
    )
    stack.enter_context(
        patch.object(
            fleet_state,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": pr_state},
        )
    )
    stack.enter_context(
        patch.object(repository, "_own_commits_count", new_callable=AsyncMock, return_value=own)
    )
    stack.enter_context(
        patch.object(
            fleet_state, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="a" * 40
        )
    )
    stack.enter_context(
        patch.object(
            fleet_state, "_head_contained_in_pr", new_callable=AsyncMock, return_value=contained
        )
    )
    stack.enter_context(patch.object(runtime, "_load_cfg", return_value=None))
    stack.enter_context(patch.object(runtime, "_POD_AVAILABLE", False))
    stack.enter_context(patch.object(repository, "_git", side_effect=git))
    stack.enter_context(patch.object(runtime, "_run_cmd", side_effect=run_cmd))
    try:
        yield
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_discard_refused_when_a_tracked_file_is_modified():
    """discard_untracked_paths set but a TRACKED file is modified -> still
    refused; the refusal carries dirty_tracked True. A discard authorizes
    destroying ONLY untracked files, never a tracked edit."""
    # status -uno prints a tracked change, so _dirty_split -> (True, [...]);
    # pending_discard stays None (tracked_dirty is not False), _dirty_now falls
    # back to _real_dirty (True), and the not-force gate refuses.
    git = _git_by_subcommand({"status": " M src/a.py\n"})

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "scratch.log\0", "")
        # a `worktree remove` here would mean the refusal was bypassed
        raise AssertionError("removal must not run for a refused discard")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN"):
        res = await worktree_ops._worktree_remove_locked(
            "feat", discard_untracked_paths=["scratch.log"]
        )
    assert res["ok"] is False
    assert res["dirty_tracked"] is True
    assert "uncommitted changes" in res["error"]


@pytest.mark.asyncio
async def test_discard_untracked_forced_unmerged_runs_clean_before_removal(monkeypatch):
    """untracked-only + discard_untracked_paths + force=True on an UNMERGED
    branch proceeds. `_discard_untracked_files` runs BEFORE the `worktree
    remove` argv, and `--force` is NOT in the removal argv (the discard path
    always removes without git --force)."""
    order: list[str] = []
    remove_argv: list[list[str]] = []
    discarded: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        order.append("discard")
        discarded.append((worktree, list(rel_paths)))
        return None  # success

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            # clean of tracked changes both before and after the discard
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # untracked before the discard; empty after the discard ran
            return (0, "note.txt\0" if not discarded else "", "")
        if "worktree" in cmd and "remove" in cmd:
            order.append("remove")
            remove_argv.append(list(cmd))
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN", own=1):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt"]
        )
    assert res.get("ok") is True, res
    assert order == ["discard", "remove"], order
    assert remove_argv, "worktree remove never ran"
    assert "--force" not in remove_argv[0]


@pytest.mark.asyncio
async def test_discard_ordering_refused_request_destroys_nothing(monkeypatch):
    """ORDERING INVARIANT: discard_untracked_paths set, untracked-only, PR NOT
    merged, force=False. The PR-not-merged gate refuses AND the discard never
    ran. A refused request must destroy nothing."""
    cleaned = {"ran": False}

    def discard_spy(worktree, rel_paths):
        cleaned["ran"] = True  # pragma: no cover - must never run
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "note.txt\0", "")  # untracked-only -> discard approved
        if "worktree" in cmd and "remove" in cmd:
            raise AssertionError("removal must not run when the PR-not-merged gate refuses")
        return (0, "", "")

    # force=False + unmerged (OPEN) + own>0 -> the PR-not-merged gate returns
    # before the discard/removal block is ever reached.
    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN", own=1):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=False, discard_untracked_paths=["note.txt"]
        )
    assert res["ok"] is False
    assert "PR not merged" in res["error"]
    assert cleaned["ran"] is False, "the discard ran on a request that was refused"


@pytest.mark.asyncio
async def test_discard_aborts_removal_when_clean_fails():
    """discard requested but `git clean` fails (or dirt remains after it) ->
    error contains 'removal aborted' and `git worktree remove` never ran."""
    ran_remove = {"v": False}

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean throughout
        if sub == "clean":
            return None  # git clean FAILED
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "note.txt\0", "")  # still untracked even after failed clean
        if "worktree" in cmd and "remove" in cmd:
            ran_remove["v"] = True  # pragma: no cover - must not run
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN", own=1):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt"]
        )
    assert res["ok"] is False
    assert "removal aborted" in res["error"]
    assert ran_remove["v"] is False


@pytest.mark.asyncio
async def test_refusal_payload_carries_dirt_fields_and_legacy_substring():
    """A refusal payload carries dirty_tracked/dirty_untracked/
    dirty_untracked_paths, and the message still contains 'uncommitted
    changes' (other tests and the client depend on that substring)."""
    git = _git_by_subcommand({"status": " M a.py\n", "rev-parse": "a" * 40})

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "s1\0s2\0", "")
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN"):
        res = await worktree_ops._worktree_remove_locked("feat", force=False)
    assert res["ok"] is False
    assert res["dirty_tracked"] is True
    assert res["dirty_untracked"] == 2
    assert res["dirty_untracked_paths"] == ["s1", "s2"]
    assert "uncommitted changes" in res["error"]


def test_dirt_detail_never_suggests_force_and_is_empty_when_unverifiable():
    """_dirt_detail never says 'use force to override' (force is refused for
    tracked edits too), and returns '' when tracked_dirty is None."""
    assert repository._dirt_detail(None, []) == ""
    assert repository._dirt_detail(None, ["a", "b"]) == ""
    detail = repository._dirt_detail(True, ["a.py"])
    assert "use force to override" not in detail
    assert "tracked files are modified" in detail


def test_dirt_fields_caps_paths_at_20_but_counts_the_true_total():
    """dirty_untracked_paths is capped at 20 for payload size while
    dirty_untracked reports the real total."""
    untracked = [f"f{i}.txt" for i in range(37)]
    fields = repository._dirt_fields(False, untracked)
    assert fields["dirty_untracked"] == 37
    assert len(fields["dirty_untracked_paths"]) == 20
    assert fields["dirty_untracked_paths"] == untracked[:20]


@pytest.mark.asyncio
async def test_prunable_verdict_carries_dirt_fields_for_dirty_tree():
    """_prunable's verdict for a dirty tree carries the three dirt fields so a
    preview can tell real edits apart from leftover session scratch."""
    git = _git_by_subcommand({"status": " M a.py\n"})

    async def run_cmd(cmd, timeout=None, **kw):
        assert "ls-files" in cmd, cmd
        return (0, "scratch.log\0note.txt\0", "")

    with (
        patch.object(
            fleet_state, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "OPEN"}
        ),
        patch.object(repository, "_own_commits_count", new_callable=AsyncMock, return_value=2),
        patch.object(repository, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(runtime, "_run_cmd", side_effect=run_cmd),
        patch.object(repository, "_git", side_effect=git),
    ):
        v = await worktree_ops._prunable("/wt/feat", "feat/x")
    assert v["ok"] is False
    assert v["dirty_tracked"] is True
    assert v["dirty_untracked"] == 2
    assert v["dirty_untracked_paths"] == ["scratch.log", "note.txt"]


@pytest.mark.asyncio
async def test_remove_handler_rejects_non_bool_discard_untracked(monkeypatch):
    """discard_untracked_paths='yes' (not a list of non-empty strings) on the
    single-remove handler -> 400, carrying a machine-readable `code`.

    The code is the contract and the prose is advisory: the dashboard renders
    `error` verbatim into a localized UI, so a sentence alone is untranslatable.
    The repo ratchets this (test_error_code_contract), and it caught this very
    response missing its code.
    """
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    remove = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(worktree_ops, "_worktree_remove", remove)
    resp = await http_api.api_dev_fleet_worktree_remove(
        _json_request({"name": "feat", "discard_untracked_paths": "yes"})
    )
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["code"] == "invalid_discard_paths"
    assert "discard_untracked_paths must be a list of non-empty strings" in body["error"]
    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_malformed_discard_paths(monkeypatch):
    """discard_untracked_paths='x' (not a name->list map) -> 400 with code
    invalid_discard_paths."""
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(
        _json_request({"names": [], "discard_untracked_paths": "x"})
    )
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["code"] == "invalid_discard_paths"


@pytest.mark.asyncio
async def test_prune_run_handler_guards_protected_worktree_in_discard_paths(monkeypatch):
    """A protected (live) worktree named ONLY in discard_untracked_paths is
    screened by the same guard as force_names -> 400 protected_worktree."""
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"live-wt"}))
    monkeypatch.setattr(live, "_live_worktree_path", AsyncMock(return_value="/repo/live-wt"))
    monkeypatch.setattr(live, "_staged_target", lambda: None)
    monkeypatch.setattr(
        repository,
        "_find_worktree",
        AsyncMock(return_value=({"path": "/repo/live-wt", "is_main": False}, None)),
    )
    resp = await http_api.api_dev_fleet_prune_run(
        _json_request({"names": [], "discard_untracked_paths": {"live-wt": ["n.txt"]}})
    )
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["code"] == "protected_worktree"
    assert "live-wt" in body["error"]


@pytest.mark.asyncio
async def test_prune_run_discard_only_name_reaches_remove_and_skips_recheck(monkeypatch):
    """A name present ONLY in discard_untracked_paths is now RE-CHECKED via
    _prunable (force's blanket bypass is no longer inherited), but a refusal
    whose code is in _DISCARD_OVERRIDABLE_CODES is overridden, so it still
    reaches _worktree_remove with the caller's consented path list."""
    prunable_calls: list[str] = []
    remove_kwargs: list[dict] = []

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        # An untracked-only tree previews as a REFUSAL; merged_dirty is one of
        # the codes a discard approval is allowed to override.
        prunable_calls.append(path)
        return {"ok": False, "code": "merged_dirty"}

    async def fake_remove(
        nm,
        force=False,
        progress=None,
        _caller="handler",
        discard_untracked_paths=None,
    ):
        remove_kwargs.append({"name": nm, "force": force, "discard": discard_untracked_paths})
        return {"ok": True, "removed": True}

    monkeypatch.setattr(worktree_ops, "_PRUNE_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        worktree_ops,
        "_PRUNE_STATE",
        {"running": False, "total": 0, "done": 0, "current": None, "results": [], "items": {}},
    )
    with (
        patch.object(repository, "_find_worktree", side_effect=fake_find),
        patch.object(worktree_ops, "_prunable", side_effect=fake_prunable),
        patch.object(live, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live, "_staged_target", return_value=None),
        patch.object(worktree_ops, "_worktree_remove", side_effect=fake_remove),
    ):
        r = await worktree_ops._prune_run([], discard_paths={"wt-scratch": ["note.txt"]})
        assert r == {"ok": True, "total": 1}
        for _ in range(500):
            if not worktree_ops._PRUNE_STATE["running"]:
                break
            await asyncio.sleep(0)

    assert worktree_ops._PRUNE_STATE["running"] is False
    # _prunable IS now consulted for a discard-only name...
    assert prunable_calls == ["/wt/wt-scratch"]
    # ...and its overridable refusal did not stop the removal: the name still
    # reached _worktree_remove carrying the exact consented list, force=False.
    assert remove_kwargs == [{"name": "wt-scratch", "force": False, "discard": ["note.txt"]}]


@pytest.mark.asyncio
async def test_discard_rel_paths_scoped_to_approved_paths(monkeypatch):
    """CHANGE 1: the discard is no longer a blanket sweep. `_discard_untracked_files`
    is handed EXACTLY the enumerated untracked paths, and a path that was never
    enumerated (never approved) is never handed to it."""
    discarded: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        discarded.append((worktree, list(rel_paths)))
        return None  # success

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # two approved untracked paths before the discard; empty after
            return (0, "" if discarded else "note.txt\0probe.sh\0", "")
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN", own=1):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt", "probe.sh"]
        )
    assert res.get("ok") is True, res
    assert len(discarded) == 1, discarded
    _worktree, rel_paths = discarded[0]
    assert rel_paths == ["note.txt", "probe.sh"], rel_paths
    # A path the caller never approved is never handed to the discard.
    assert "secret.env" not in rel_paths


@pytest.mark.asyncio
async def test_discard_file_appearing_after_approval_is_not_destroyed():
    """CHANGE 1: a file created AFTER approval is outside the clean pathspec, so
    the post-clean `_dirty_split` still finds it untracked and the removal is
    ABORTED rather than widened to destroy it. `git worktree remove` never
    runs."""
    ran_remove = {"v": False}
    cleaned = {"v": False}

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean throughout
        if sub == "clean":
            cleaned["v"] = True
            return ""  # clean itself succeeds
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # BEFORE clean: one approved path. AFTER clean: a DIFFERENT untracked
            # path appears -- one that was never approved and never cleaned.
            return (0, "note.txt\0" if not cleaned["v"] else "appeared-after.txt\0", "")
        if "worktree" in cmd and "remove" in cmd:
            ran_remove["v"] = True  # pragma: no cover - must not run
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN", own=1):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt"]
        )
    assert res["ok"] is False
    assert "removal aborted" in res["error"]
    assert ran_remove["v"] is False, "worktree remove ran despite a file appearing after approval"


@pytest.mark.asyncio
async def test_dirty_split_untracked_paths_survive_byte_exact():
    """CHANGE 1 regression guard: the untracked half is read through `_run_cmd`
    precisely so filenames survive byte-exact -- a leading space on the first
    entry and a trailing space on the last would be eaten by the `_git` helper's
    `.strip()`. Feed such a payload and assert both survive intact."""
    git = _git_by_subcommand({"status": ""})

    async def run_cmd(cmd, timeout=None, **kw):
        assert "ls-files" in cmd, cmd
        # first entry has a LEADING space, last has a TRAILING space
        return (0, " leading.txt\0middle.txt\0trailing.txt \0", "")

    with (
        patch.object(repository, "_git", side_effect=git),
        patch.object(runtime, "_run_cmd", side_effect=run_cmd),
    ):
        tracked, untracked = await repository._dirty_split("/wt/x")
    assert tracked is False
    assert untracked == [" leading.txt", "middle.txt", "trailing.txt "]
    # The whitespace is preserved, not stripped (which the _git helper would do).
    assert untracked[0].startswith(" ")
    assert untracked[-1].endswith(" ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code, should_reach_remove",
    [
        # NOT overridable by a discard: the refusal stands, removal never runs.
        ("dirty_check_failed", False),
        ("fresh", False),
        ("merged_unverified", False),
        ("merged_new_commits", False),
        # Overridable: the discard clears the very dirt that withheld it.
        ("merged_dirty", True),
        ("active", True),
    ],
)
async def test_discard_override_respects_overridable_codes(monkeypatch, code, should_reach_remove):
    """CHANGE 2: a discard-only approval overrides a prune refusal ONLY when the
    verdict code is in `_DISCARD_OVERRIDABLE_CODES`. A non-overridable code is
    refused with `not prunable: <code>` and never reaches `_worktree_remove`; an
    overridable code reaches it."""
    remove_kwargs: list[dict] = []

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        return {"ok": False, "code": code}

    async def fake_remove(
        nm,
        force=False,
        progress=None,
        _caller="handler",
        discard_untracked_paths=None,
    ):
        remove_kwargs.append({"name": nm, "force": force, "discard": discard_untracked_paths})
        return {"ok": True, "removed": True}

    monkeypatch.setattr(worktree_ops, "_PRUNE_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        worktree_ops,
        "_PRUNE_STATE",
        {"running": False, "total": 0, "done": 0, "current": None, "results": [], "items": {}},
    )
    with (
        patch.object(repository, "_find_worktree", side_effect=fake_find),
        patch.object(worktree_ops, "_prunable", side_effect=fake_prunable),
        patch.object(live, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live, "_staged_target", return_value=None),
        patch.object(worktree_ops, "_worktree_remove", side_effect=fake_remove),
    ):
        r = await worktree_ops._prune_run([], discard_paths={"wt-x": ["note.txt"]})
        assert r == {"ok": True, "total": 1}
        for _ in range(500):
            if not worktree_ops._PRUNE_STATE["running"]:
                break
            await asyncio.sleep(0)

    if should_reach_remove:
        assert remove_kwargs == [{"name": "wt-x", "force": False, "discard": ["note.txt"]}]
    else:
        assert remove_kwargs == [], f"{code} must not reach _worktree_remove"
        results = worktree_ops._PRUNE_STATE["items"]["wt-x"]
        assert results["status"] == "failed"
        assert results["error"] == f"not prunable: {code}"


# ---------------------------------------------------------------------------
# CONSENT-BY-PATH-LIST invariants (the reshaped discard API)
#
# A discard is honoured only when the caller submits the EXACT set it was
# shown: tracked-clean, a non-empty fresh untracked set, at most
# _DIRTY_PATH_SAMPLE of them, every path unchanged by _redact (a lossy
# rendering cannot identify one file, so a rewritten name is refused), and
# set(submitted) == set(fresh). The `git clean` pathspec then names those raw
# paths, each spelled `:(literal)` so no filename is read as pathspec magic.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discard_refused_when_submitted_omits_a_file_now_on_disk():
    """CONSENT MISMATCH: the caller's list omits a file that is now untracked
    on disk. The fresh set is larger than what was consented to, so the discard
    is refused with 'untracked files changed since they were listed'; neither
    `git clean` nor `git worktree remove` runs."""
    cleaned = {"ran": False}
    ran_remove = {"v": False}

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean
        if sub == "clean":
            cleaned["ran"] = True  # pragma: no cover - must not run
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # disk carries TWO untracked files; the caller consented to one.
            return (0, "note.txt\0appeared.txt\0", "")
        if "worktree" in cmd and "remove" in cmd:
            ran_remove["v"] = True  # pragma: no cover - must not run
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt"]
        )
    assert res["ok"] is False
    assert "untracked files changed since they were listed" in res["error"]
    assert cleaned["ran"] is False, "git clean ran on a consent mismatch"
    assert ran_remove["v"] is False, "worktree remove ran on a consent mismatch"


@pytest.mark.asyncio
async def test_discard_refused_when_submitted_names_a_file_no_longer_there():
    """CONSENT MISMATCH (reverse): the caller names a file that is no longer on
    disk. The submitted set differs from the fresh set, so the discard is
    refused with the same message; nothing is cleaned or removed."""
    cleaned = {"ran": False}
    ran_remove = {"v": False}

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean
        if sub == "clean":
            cleaned["ran"] = True  # pragma: no cover - must not run
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # only note.txt survives; gone.txt the caller listed is no longer here
            return (0, "note.txt\0", "")
        if "worktree" in cmd and "remove" in cmd:
            ran_remove["v"] = True  # pragma: no cover - must not run
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt", "gone.txt"]
        )
    assert res["ok"] is False
    assert "untracked files changed since they were listed" in res["error"]
    assert cleaned["ran"] is False, "git clean ran on a consent mismatch"
    assert ran_remove["v"] is False, "worktree remove ran on a consent mismatch"


@pytest.mark.asyncio
async def test_discard_refused_when_more_than_sample_untracked_files():
    """TRUNCATION: 21 untracked files exceed _DIRTY_PATH_SAMPLE (20), so the
    caller was handed a truncated list and cannot have consented to the whole
    set. Refuse with 'too many untracked files to confirm individually';
    nothing cleaned or removed."""
    cleaned = {"ran": False}
    ran_remove = {"v": False}
    n = repository._DIRTY_PATH_SAMPLE + 1
    files = [f"f{i}.txt" for i in range(n)]

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        if sub == "clean":
            cleaned["ran"] = True  # pragma: no cover - must not run
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "\0".join(files) + "\0", "")
        if "worktree" in cmd and "remove" in cmd:
            ran_remove["v"] = True  # pragma: no cover - must not run
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=files
        )
    assert res["ok"] is False
    assert "too many untracked files to confirm individually" in res["error"]
    assert res["dirty_untracked"] == n
    assert cleaned["ran"] is False, "git clean ran on an oversized untracked set"
    assert ran_remove["v"] is False, "worktree remove ran on an oversized set"


@pytest.mark.asyncio
async def test_discard_accepts_exactly_sample_untracked_files(monkeypatch):
    """TRUNCATION boundary: exactly _DIRTY_PATH_SAMPLE (20) files are still
    within the shown list, so a matching consent set is ACCEPTED, the discard
    runs against all 20, and the removal proceeds."""
    discarded: list[tuple] = []
    n = repository._DIRTY_PATH_SAMPLE
    files = [f"f{i}.txt" for i in range(n)]

    def discard_spy(worktree, rel_paths):
        discarded.append((worktree, list(rel_paths)))
        return None  # success

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # all 20 before the discard; empty afterwards
            return (0, ("\0".join(files) + "\0") if not discarded else "", "")
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=files
        )
    assert res.get("ok") is True, res
    assert len(discarded) == 1, discarded
    _worktree, rel_paths = discarded[0]
    assert rel_paths == files, rel_paths
    assert len(rel_paths) == n


def test_dirt_fields_and_detail_pass_paths_through_redact(monkeypatch):
    """REDACTION: `_dirt_fields` and `_dirt_detail` emit paths passed through
    `_redact`. Patch `_redact` with a recognisable transform and assert the
    emitted values carry it."""
    monkeypatch.setattr(runtime, "_redact", lambda s: f"REDACTED::{s}")
    untracked = ["a.txt", "b.txt", "c.txt", "d.txt"]

    fields = repository._dirt_fields(False, untracked)
    assert fields["dirty_untracked_paths"] == [f"REDACTED::{p}" for p in untracked]
    # the count is the true total, unredacted
    assert fields["dirty_untracked"] == 4

    detail = repository._dirt_detail(False, untracked)
    # _dirt_detail names the first three, each redacted
    assert "REDACTED::a.txt" in detail
    assert "REDACTED::b.txt" in detail
    assert "REDACTED::c.txt" in detail
    # the raw (unredacted) name must not leak into the detail string
    assert "a.txt" in detail  # substring of REDACTED::a.txt, expected
    assert detail.count("REDACTED::") == 3


@pytest.mark.asyncio
async def test_discard_emitted_paths_are_redacted_but_pathspec_is_raw(monkeypatch):
    """REDACTION: with `_redact` patched to a non-identity transform, a refusal
    payload's emitted paths carry the transform. The discard itself is refused
    outright in that case -- a lossy rendering cannot identify the consented
    file -- so `_discard_untracked_files` is never called."""
    monkeypatch.setattr(runtime, "_redact", lambda s: f"R::{s}")
    discarded: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        discarded.append((worktree, list(rel_paths)))  # pragma: no cover - refusal, must not run
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "note.txt\0", "")
        return (0, "", "")

    # Submit the RAW name. Redaction is not the identity here, so the discard is
    # refused BEFORE any set comparison: a rewritten name cannot be pinned to
    # one file. Emitted paths still carry the transform.
    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt"]
        )
    assert res["ok"] is False
    assert "cannot be confirmed safely" in res["error"]
    # emitted dirt paths carry the redaction transform
    assert res["dirty_untracked_paths"] == ["R::note.txt"]
    # and nothing was discarded (the discard was never called)
    assert discarded == [], "the discard ran despite an unconfirmable filename"


@pytest.mark.asyncio
async def test_discard_refused_when_a_filename_is_rewritten_by_redaction(monkeypatch):
    """REDACTION COLLISION: `_redact` is lossy, so two distinct filenames can
    share one displayed rendering. Comparing consent in that space would let a
    file swapped in after display satisfy the equality check and be destroyed
    unapproved. So a discard is refused outright whenever ANY fresh path is
    rewritten on the way out -- even when the client faithfully echoes exactly
    what it was shown. Refusing costs a manual cleanup; the alternative loses a
    file nobody approved."""
    monkeypatch.setattr(runtime, "_redact", lambda s: f"R::{s}")
    discarded: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        discarded.append((worktree, list(rel_paths)))  # pragma: no cover - must never run
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "note.txt\0probe.sh\0", "")
        return (0, "", "")

    # The client echoes exactly the redacted forms `_dirt_fields` showed it.
    # Under the old two-space rule this was ACCEPTED; it is now refused, because
    # `R::note.txt` cannot be traced back to one file on disk.
    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat",
            force=True,
            discard_untracked_paths=["R::note.txt", "R::probe.sh"],
        )
    assert res["ok"] is False
    assert "cannot be confirmed safely" in res["error"]
    assert discarded == [], "the discard ran on a set that redaction had rewritten"


@pytest.mark.asyncio
async def test_discard_accepted_when_redaction_is_identity(monkeypatch):
    """The control for the guard above: with `_redact` left as the identity on
    these filenames, the echoed set matches the raw set and the discard runs,
    handed exactly those raw literal paths (no `:(literal)` wrapping)."""
    monkeypatch.setattr(runtime, "_redact", lambda s: s)
    discarded: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        discarded.append((worktree, list(rel_paths)))
        return None  # success

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, ("note.txt\0probe.sh\0") if not discarded else "", "")
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat",
            force=True,
            discard_untracked_paths=["note.txt", "probe.sh"],
        )
    assert res.get("ok") is True, res
    assert len(discarded) == 1, discarded
    _worktree, rel_paths = discarded[0]
    # exactly the raw approved paths, no `:(literal)` wrapping
    assert rel_paths == ["note.txt", "probe.sh"], rel_paths
    assert not any(p.startswith(":(literal)") for p in rel_paths), rel_paths


# --- Direct tests of _discard_untracked_files against a REAL filesystem. ---
# No mocking, no git repo: the helper is a pure per-file os.unlink walk, so it
# can be exercised against tmp_path. Each maps to a property verified by hand.


# `_discard_untracked_files` refuses outright where openat/O_NOFOLLOW do not
# exist (Windows), because a path-based delete there could follow a swapped
# junction out of the worktree. Its BEHAVIOURAL cases therefore have nothing to
# exercise on such a platform -- the helper never reaches the deletion at all --
# and one of them cannot even be set up, since `:(glob)*` is not a legal Windows
# filename. The refusal itself is what matters there, and
# test_discard_untracked_files_fails_closed_without_openat covers it on every
# platform, so it is deliberately NOT marked.
_fd_safe_discard = hasattr(os, "O_NOFOLLOW") and os.unlink in os.supports_dir_fd
requires_fd_safe_discard = pytest.mark.skipif(
    not _fd_safe_discard,
    reason="_discard_untracked_files refuses without openat/O_NOFOLLOW",
)


def _need_unsymlinked_tmp(tmp_path):
    """Skip when pytest's own tmp_path resolves through a symlink.

    `_discard_untracked_files` refuses ANY symlink in the worktree's path by
    design, so on a runner whose temp root is linked (a linked /tmp, macOS
    /private/tmp) every behavioural case below would get the refusal instead of
    the behaviour it is asserting. That is the helper working, not a defect --
    and the refusal itself is covered by
    test_discard_untracked_files_refuses_a_symlinked_ancestor, which builds its
    own symlink rather than relying on the environment having one.
    """
    if os.path.realpath(str(tmp_path)) != str(tmp_path):
        pytest.skip("pytest tmp_path resolves through a symlink on this runner")


@requires_fd_safe_discard
def test_discard_untracked_files_refuses_a_symlinked_worktree_root(tmp_path):
    """ROOT SWAP: if the worktree root itself is a symlink, the discard refuses.

    Opening the root without O_NOFOLLOW would make the careful per-component
    walk pointless -- the very first open would already have left the checkout,
    and matching relative paths inside the symlink's target would be unlinked.
    """
    _need_unsymlinked_tmp(tmp_path)
    real = tmp_path / "elsewhere"
    real.mkdir()
    victim = real / "probe.py"
    victim.write_text("outside the worktree")
    as_link = tmp_path / "worktree-link"
    as_link.symlink_to(real, target_is_directory=True)

    reason = repository._discard_untracked_files(str(as_link), ["probe.py"])
    assert reason is not None
    assert "is a symlink" in reason
    assert victim.exists(), "followed a symlinked worktree root and deleted an external file"


@requires_fd_safe_discard
def test_discard_untracked_files_refuses_a_symlinked_ancestor(tmp_path):
    """ANCESTOR SWAP: a symlink ANYWHERE in the worktree's own path is refused.

    An earlier revision resolved the path first so that a legitimately linked
    ancestor would still work. That was wrong and a probe proved it: `realpath`
    follows the topology as it stands NOW, so it resolved INTO a swapped ancestor
    and deleted a file outside the checkout. Resolution launders the swap rather
    than refusing it, and from inside this helper a legitimate link and a
    malicious one are indistinguishable.

    So both are refused, and the documented cost is that a host whose worktree
    path contains a linked directory cannot use the discard and must clean by
    hand -- the same trade as the platform check for Windows.
    """
    _need_unsymlinked_tmp(tmp_path)
    real = tmp_path / "realparent"
    (real / "wt").mkdir(parents=True)
    kept = real / "wt" / "probe.py"
    kept.write_text("x")
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real, target_is_directory=True)

    reason = repository._discard_untracked_files(str(link_parent / "wt"), ["probe.py"])
    assert reason is not None
    assert "is a symlink" in reason
    assert kept.exists(), "deleted through a symlinked ancestor instead of refusing"


@requires_fd_safe_discard
def test_discard_untracked_files_plain_path_still_deletes(tmp_path):
    """The control: with no symlink anywhere in the path, the walk from `/` pins
    every component and the approved files are removed, including a nested one."""
    _need_unsymlinked_tmp(tmp_path)
    wt = tmp_path / "wt"
    (wt / "sub").mkdir(parents=True)
    (wt / "probe.py").write_text("x")
    (wt / "sub" / "harness.py").write_text("y")

    reason = repository._discard_untracked_files(str(wt), ["probe.py", "sub/harness.py"])
    assert reason is None, reason
    assert not (wt / "probe.py").exists()
    assert not (wt / "sub" / "harness.py").exists()


def test_discard_untracked_files_fails_closed_without_openat(tmp_path, monkeypatch):
    """PLATFORM: without openat/O_NOFOLLOW the discard is REFUSED, not downgraded
    to a path-based unlink.

    A path-based delete re-resolves every ancestor, so a directory component
    swapped for a symlink -- or a Windows junction, which `os.path.islink` does
    not report as a link at all -- would redirect the deletion outside the
    worktree. Withdrawing the affordance costs a button; approximating it costs
    a file that was never in the worktree.
    """
    victim = tmp_path / "probe.py"
    victim.write_text("x")
    # Simulate a platform whose unlink cannot take a dir_fd.
    monkeypatch.setattr(os, "supports_dir_fd", set())

    reason = repository._discard_untracked_files(str(tmp_path), ["probe.py"])
    assert reason is not None
    assert "cannot discard untracked files safely on this platform" in reason
    # and it refused BEFORE touching anything
    assert victim.exists(), "a file was deleted on a platform we cannot delete safely on"


@requires_fd_safe_discard
def test_discard_untracked_files_type_change_refuses_and_keeps_unapproved(tmp_path):
    """TYPE CHANGE regression guard (the finding that forced the rewrite): the
    approved name is now a DIRECTORY holding an unapproved file. os.unlink
    raises IsADirectoryError, so the helper REFUSES -- it names the type change
    and does NOT recurse into the directory, so the unapproved file survives."""
    _need_unsymlinked_tmp(tmp_path)
    # `scratch` was approved as a regular file, but between consent and
    # execution it became a directory holding a file nobody approved.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    victim = scratch / "not-approved.txt"
    victim.write_text("keep me")

    reason = repository._discard_untracked_files(str(tmp_path), ["scratch"])

    assert reason is not None
    assert "scratch" in reason
    assert "directory" in reason.lower()
    # The unapproved file inside the swapped-in directory is untouched.
    assert victim.exists()
    assert victim.read_text() == "keep me"


@requires_fd_safe_discard
def test_discard_untracked_files_deletes_files_but_leaves_emptied_dir(tmp_path):
    """Approved files, including a nested `sub/harness.py`, are deleted; the
    now-empty `sub/` directory is deliberately left behind (git does not track
    empty dirs and `worktree remove` does not object to them)."""
    _need_unsymlinked_tmp(tmp_path)
    (tmp_path / "top.txt").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "harness.py").write_text("y")

    reason = repository._discard_untracked_files(str(tmp_path), ["top.txt", "sub/harness.py"])

    assert reason is None
    assert not (tmp_path / "top.txt").exists()
    assert not (sub / "harness.py").exists()
    # The emptied directory is intentionally NOT removed.
    assert sub.is_dir()


@requires_fd_safe_discard
def test_discard_untracked_files_refuses_escapes_and_deletes_nothing(tmp_path):
    """Escape attempts are refused with NOTHING deleted: a `..` traversal, an
    absolute path, and a mid-path `..`. An outside file and an inside sibling
    both survive."""
    _need_unsymlinked_tmp(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    inner = tmp_path / "inside.txt"
    inner.write_text("inside")

    for bad in ("../outside.txt", str(outside), "sub/../../x"):
        reason = repository._discard_untracked_files(str(tmp_path), [bad])
        assert reason is not None, bad
        assert "refusing" in reason.lower(), (bad, reason)

    # Neither the outside target nor the inside sibling was touched.
    assert outside.exists() and outside.read_text() == "outside"
    assert inner.exists() and inner.read_text() == "inside"


@requires_fd_safe_discard
def test_discard_untracked_files_missing_path_is_idempotent(tmp_path):
    """A path that no longer exists returns None (idempotent) so a retry after a
    partially-completed discard is not an error."""
    _need_unsymlinked_tmp(tmp_path)
    assert not (tmp_path / "gone.txt").exists()
    reason = repository._discard_untracked_files(str(tmp_path), ["gone.txt"])
    assert reason is None


@requires_fd_safe_discard
def test_discard_untracked_files_magic_filename_is_literal(tmp_path):
    """A filename that looks like pathspec magic (`:(glob)*`) is deleted as a
    LITERAL name -- there is no pathspec for git to interpret it as a pattern --
    while a sibling file that a glob would have swept survives."""
    _need_unsymlinked_tmp(tmp_path)
    magic = tmp_path / ":(glob)*"
    magic.write_text("approved")
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("not approved")

    reason = repository._discard_untracked_files(str(tmp_path), [":(glob)*"])

    assert reason is None
    assert not magic.exists()
    # A real glob would have matched the sibling; a literal unlink does not.
    assert sibling.exists()
    assert sibling.read_text() == "not approved"


@pytest.mark.asyncio
async def test_remove_handler_rejects_discard_bool(monkeypatch):
    """HANDLER VALIDATION: discard_untracked_paths as a bool -> 400, remove
    never called."""
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    remove = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(worktree_ops, "_worktree_remove", remove)
    resp = await http_api.api_dev_fleet_worktree_remove(
        _json_request({"name": "feat", "discard_untracked_paths": True})
    )
    assert resp.status == 400
    assert (
        "discard_untracked_paths must be a list of non-empty strings"
        in json.loads(resp.text)["error"]
    )
    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_handler_rejects_discard_list_with_empty_string(monkeypatch):
    """HANDLER VALIDATION: discard_untracked_paths containing an empty string
    -> 400, remove never called."""
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    remove = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(worktree_ops, "_worktree_remove", remove)
    resp = await http_api.api_dev_fleet_worktree_remove(
        _json_request({"name": "feat", "discard_untracked_paths": ["ok.txt", ""]})
    )
    assert resp.status == 400
    assert (
        "discard_untracked_paths must be a list of non-empty strings"
        in json.loads(resp.text)["error"]
    )
    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_discard_list_instead_of_map(monkeypatch):
    """HANDLER VALIDATION: prune's discard_untracked_paths given as a LIST
    instead of a name->list map -> 400 with code invalid_discard_paths."""
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(
        _json_request({"names": [], "discard_untracked_paths": ["wt-a"]})
    )
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["code"] == "invalid_discard_paths"


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_discard_map_with_empty_string_entry(monkeypatch):
    """HANDLER VALIDATION: prune's discard_untracked_paths map carrying a list
    with an empty string -> 400 with code invalid_discard_paths."""
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(
        _json_request({"names": [], "discard_untracked_paths": {"wt-a": ["ok.txt", ""]}})
    )
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["code"] == "invalid_discard_paths"


@pytest.mark.asyncio
async def test_locked_worktree_refused_before_any_discard_runs(monkeypatch):
    """LOCK ORDERING: git refuses to remove a locked worktree, and it refuses
    LAST -- a discard succeeds on a locked tree, so a discard that ran first
    would already have destroyed the scratch by the time the removal failed.
    The lock is therefore recognised up front and the discard must never run."""
    clean_ran: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        clean_ran.append((worktree, list(rel_paths)))  # pragma: no cover - must never happen
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "probe.py\0", "")
        return (0, "", "")

    with _remove_stubs(
        git=git,
        run_cmd=run_cmd,
        pr_state="MERGED",
        own=0,
        target={
            "path": "/wt/held",
            "branch": "held",
            "is_main": False,
            "locked": "keeping this for the repro",
        },
    ):
        res = await worktree_ops._worktree_remove_locked(
            "held", force=True, discard_untracked_paths=["probe.py"]
        )
    assert res["ok"] is False
    assert "locked" in res["error"]
    assert "git worktree unlock" in res["error"]
    assert clean_ran == [], "the discard ran against a locked worktree"


@pytest.mark.asyncio
async def test_removal_failure_after_discard_says_files_were_discarded(monkeypatch):
    """Every removal failure we can name in advance is refused before the
    discard, but the unnameable ones (a permission change, a file held open)
    still land after it. The error must then SAY the files are gone, instead of
    returning git's bare stderr and reading like a no-op."""

    cleaned: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        cleaned.append((worktree, list(rel_paths)))  # discard succeeds
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        return "a" * 40

    calls: list[list] = []

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # Empty once the discard has run, so the post-discard check passes
            # and execution reaches the removal -- which is where this test's
            # failure is injected.
            return (0, "" if cleaned else "probe.py\0", "")
        if "remove" in cmd:
            calls.append(cmd)
            return (1, "", "fatal: could not remove: Permission denied")
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["probe.py"]
        )
    assert res["ok"] is False
    assert "discarded 1 untracked file(s)" in res["error"]
    assert "could not remove the worktree" in res["error"]
    # git's own reason is preserved, not swallowed by the wrapper text
    assert "Permission denied" in res["error"]
