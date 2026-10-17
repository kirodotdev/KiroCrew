"""Coverage-focused tests for the Dev Fleet standalone backend.

Targets uncovered helper branches and pod-guard refusals of
``kiro_crew.apps.builtins.dev_fleet.server``.

Everything is injected: no real git, no real subprocess, no network, and no
writes outside ``tmp_path``. Where the module reads its config home the
loader's ``config_dir`` is patched, so nothing touches the real one.

This module keeps the config loader, launchd checkout resolution, fallback-repo
discovery, git info, the PR-status cache and HTML repo base, the pod pin and
checkout guard, the pod operations, disk aggregation and rebase. Its siblings own
the rest: ``test_dev_fleet_server_coverage_prune.py`` (prune verdicts and the
candidate scan), ``test_dev_fleet_server_coverage_http_api.py`` (the GET and POST
handlers, the audit decorator, the HMAC middleware and worktree detail),
``test_dev_fleet_server_coverage_lifecycle.py`` (live gateway identity, the drop-in
rollback, cleanup, the app factory and the entry point),
``test_dev_fleet_server_coverage_untracked_discard.py`` (the untracked-discard
contract of worktree removal) and ``test_dev_fleet_server_coverage_discard_files.py``
(``_discard_untracked_files`` on a real filesystem). The autouse state pin and the
doubles more than one module reads live in ``dev_fleet_server_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dev_fleet_server_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _pin_module_state,
    _same,
)

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    gateway_service,
    live,
    repository,
    runtime,
    worktree_ops,
)
from kiro_crew.testing.wait import async_wait_until

# The launchd label derivation imports the pod launchd module for its label
# prefix; on a host where that optional module is unavailable the function
# falls back to the live label and the pod branch cannot be observed.
_LAUNCHD_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="launchd live-program layout is POSIX-only",
)


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
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value="nope"))
    assert await worktree_ops._pod_up("feat") == {"ok": False, "error": "nope"}


@pytest.mark.asyncio
async def test_pod_up_cli_failure_is_reported(monkeypatch, allow_pod):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(1, "", "boom")))
    assert await worktree_ops._pod_up("feat") == {"ok": False, "error": "boom"}


@pytest.mark.asyncio
async def test_pod_up_non_json_output_still_ok(monkeypatch, allow_pod):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "not json", "")))
    assert await worktree_ops._pod_up("feat") == {"ok": True, "output": "not json"}


@pytest.mark.asyncio
async def test_pod_up_json_output_is_merged(monkeypatch, allow_pod):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, '{"port": 9999}', "")))
    assert await worktree_ops._pod_up("feat") == {"ok": True, "port": 9999}


@pytest.mark.asyncio
async def test_pod_up_inactive_after_start_fails_closed(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
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
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None))
    )
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
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/worktrees/feat"}, None))
    )
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value="denied"))
    assert await worktree_ops._pod_down("feat") == {"ok": False, "error": "denied"}


@pytest.mark.asyncio
async def test_pod_down_cli_failure_is_reported(monkeypatch, allow_pod):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/worktrees/feat"}, None))
    )
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(2, "out", "")))
    assert await worktree_ops._pod_down("feat") == {"ok": False, "error": "out"}


@pytest.mark.asyncio
async def test_pod_down_still_active_fails_closed(monkeypatch):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/worktrees/feat"}, None))
    )
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
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/worktrees/feat"}, None))
    )
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
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=({"path": "/worktrees/feat"}, None))
    )
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
async def test_disk_fresh_result_served_without_rescan(monkeypatch):
    """A completed result inside the TTL is a cache, not a one-read handoff."""
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "done", "total_mb": 42, "per": {"a": 42}})
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTING", False)
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTED_AT", time.monotonic())
    monkeypatch.setattr(fleet_state, "_DISK_EPOCH", 0)
    monkeypatch.setattr(
        repository, "_discover_worktrees", AsyncMock(side_effect=AssertionError("must not scan"))
    )
    for _ in range(2):
        snap = await fleet_state._disk()
        assert snap == {"status": "done", "total_mb": 42, "per": {"a": 42}}
    assert fleet_state._DISK["status"] == "done"


@pytest.mark.asyncio
async def test_disk_six_sequential_polls_run_one_aggregation(monkeypatch):
    """An open dashboard polling /disk must not re-scan.

    Six sequential endpoint reads on main ran three full aggregations because
    the done branch reset the state machine to idle on every snapshot. With
    the TTL cache the same six reads run exactly one.
    """
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "idle", "total_mb": None, "per": {}})
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTING", False)
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTED_AT", 0.0)
    monkeypatch.setattr(fleet_state, "_DISK_EPOCH", 0)
    discoveries = 0
    du_calls: list = []

    async def fake_discover():
        nonlocal discoveries
        discoveries += 1
        return [{"path": "/repo/wt-a"}]

    async def fake_run(cmd, **kw):
        du_calls.append(cmd)
        return (0, "12\t/repo/wt-a", "")

    monkeypatch.setattr(repository, "_discover_worktrees", fake_discover)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/du")
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)

    assert (await fleet_state._disk())["status"] == "computing"
    await async_wait_until(
        lambda: fleet_state._DISK["status"] == "done",
        describe=lambda: f'disk status: {fleet_state._DISK.get("status")}',
    )
    for _ in range(5):
        snap = await fleet_state._disk()
        assert snap == {"status": "done", "total_mb": 12, "per": {"wt-a": 12}}
        await asyncio.sleep(0)  # give any wrongly-spawned refresh a chance to run
    assert discoveries == 1
    assert len(du_calls) == 1
    assert fleet_state._DISK["status"] == "done"  # never reset back to idle


@pytest.mark.asyncio
async def test_disk_stale_cache_coalesces_one_refresh(monkeypatch):
    """Concurrent polls past the TTL start exactly one background refresh."""
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "done", "total_mb": 42, "per": {"a": 42}})
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTING", False)
    monkeypatch.setattr(
        fleet_state, "_DISK_COMPUTED_AT", time.monotonic() - fleet_state._DISK_TTL - 1
    )
    monkeypatch.setattr(fleet_state, "_DISK_EPOCH", 0)
    started = 0
    release = asyncio.Event()

    async def fake_discover():
        nonlocal started
        started += 1
        await release.wait()
        return [{"path": "/repo/wt-a"}]

    monkeypatch.setattr(repository, "_discover_worktrees", fake_discover)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/du")
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "7\t/repo/wt-a", "")))

    snaps = await asyncio.gather(*(fleet_state._disk() for _ in range(6)))
    # Every concurrent poll keeps observing the stale numbers, never a reset.
    assert all(s == {"status": "done", "total_mb": 42, "per": {"a": 42}} for s in snaps)
    await asyncio.sleep(0)  # let the refresh task reach the discovery await
    assert started == 1
    release.set()
    await async_wait_until(
        lambda: fleet_state._DISK["total_mb"] == 7,
        describe=lambda: f'disk total_mb: {fleet_state._DISK.get("total_mb")}',
    )
    assert fleet_state._DISK == {"status": "done", "total_mb": 7, "per": {"wt-a": 7}}
    assert started == 1


@pytest.mark.asyncio
async def test_disk_invalidate_forces_refresh_on_next_read(monkeypatch):
    """A worktree mutation drops the freshness stamp; the next read re-scans."""
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "done", "total_mb": 42, "per": {"a": 42}})
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTING", False)
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTED_AT", time.monotonic())
    monkeypatch.setattr(fleet_state, "_DISK_EPOCH", 0)
    monkeypatch.setattr(
        repository, "_discover_worktrees", AsyncMock(return_value=[{"path": "/repo/wt-b"}])
    )
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/du")
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "9\t/repo/wt-b", "")))

    fleet_state._disk_invalidate()
    # The stale numbers keep serving while the refresh runs behind them.
    assert await fleet_state._disk() == {"status": "done", "total_mb": 42, "per": {"a": 42}}
    await async_wait_until(
        lambda: fleet_state._DISK["total_mb"] == 9,
        describe=lambda: f'disk total_mb: {fleet_state._DISK.get("total_mb")}',
    )
    assert fleet_state._DISK == {"status": "done", "total_mb": 9, "per": {"wt-b": 9}}


@pytest.mark.asyncio
async def test_disk_mid_flight_invalidation_leaves_result_stale(monkeypatch):
    """An aggregation overlapped by a mutation must not be stamped fresh."""
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "idle", "total_mb": None, "per": {}})
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTING", False)
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTED_AT", 0.0)
    monkeypatch.setattr(fleet_state, "_DISK_EPOCH", 0)
    release = asyncio.Event()

    async def fake_discover():
        await release.wait()
        return [{"path": "/repo/wt-a"}]

    monkeypatch.setattr(repository, "_discover_worktrees", fake_discover)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "5\t/repo/wt-a", "")))

    await fleet_state._disk()  # starts the aggregation
    fleet_state._disk_invalidate()  # a mutation lands mid-flight
    release.set()
    await async_wait_until(
        lambda: fleet_state._DISK["status"] == "done",
        describe=lambda: f'disk status: {fleet_state._DISK.get("status")}',
    )
    # The measurement is kept for display but left stale: the next read starts
    # a fresh aggregation instead of serving pre-mutation numbers for a TTL.
    assert fleet_state._DISK_COMPUTED_AT == 0.0


@pytest.mark.asyncio
async def test_disk_idle_starts_background_aggregation(monkeypatch):
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "idle", "total_mb": None, "per": {}})
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTING", False)
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTED_AT", 0.0)
    monkeypatch.setattr(fleet_state, "_DISK_EPOCH", 0)
    monkeypatch.setattr(
        repository,
        "_discover_worktrees",
        AsyncMock(return_value=[{"path": "/repo/wt-a"}, {"path": "/repo/wt-b"}]),
    )

    async def fake_run(cmd, **kw):
        return (0, "12\t" + cmd[-1], "") if cmd[-1].endswith("wt-a") else (1, "", "err")

    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/du")
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)

    assert await fleet_state._disk() == {"status": "computing", "total_mb": None, "per": {}}
    await async_wait_until(
        lambda: fleet_state._DISK["status"] == "done",
        describe=lambda: f'disk status: {fleet_state._DISK.get("status")}',
    )
    assert fleet_state._DISK["per"] == {"wt-a": 12}
    assert fleet_state._DISK["total_mb"] == 12


@pytest.mark.asyncio
async def test_disk_aggregation_failure_reports_unknown(monkeypatch):
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "idle", "total_mb": None, "per": {}})
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTING", False)
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTED_AT", 0.0)
    monkeypatch.setattr(fleet_state, "_DISK_EPOCH", 0)
    monkeypatch.setattr(
        repository, "_discover_worktrees", AsyncMock(side_effect=RuntimeError("git"))
    )

    await fleet_state._disk()
    await async_wait_until(
        lambda: fleet_state._DISK["status"] == "done",
        describe=lambda: f'disk status: {fleet_state._DISK.get("status")}',
    )
    assert fleet_state._DISK == {"status": "done", "total_mb": None, "per": {}}


@pytest.mark.asyncio
async def test_disk_transient_failure_preserves_numbers_and_retries(monkeypatch):
    """A failed refresh keeps the previous result and is never cached fresh."""
    monkeypatch.setattr(fleet_state, "_DISK", {"status": "done", "total_mb": 42, "per": {"a": 42}})
    monkeypatch.setattr(fleet_state, "_DISK_COMPUTING", False)
    monkeypatch.setattr(
        fleet_state, "_DISK_COMPUTED_AT", time.monotonic() - fleet_state._DISK_TTL - 1
    )
    monkeypatch.setattr(fleet_state, "_DISK_EPOCH", 0)
    monkeypatch.setattr(
        repository, "_discover_worktrees", AsyncMock(side_effect=RuntimeError("git timeout"))
    )

    # Stale read starts the refresh, which fails behind the scenes.
    assert await fleet_state._disk() == {"status": "done", "total_mb": 42, "per": {"a": 42}}
    await async_wait_until(
        lambda: not fleet_state._DISK_COMPUTING,
        describe=lambda: f"disk still computing: {fleet_state._DISK_COMPUTING}",
    )
    # Good numbers survive the failure, and the stamp stays 0.0 so the next
    # read retries instead of serving the failure for a full TTL.
    assert fleet_state._DISK == {"status": "done", "total_mb": 42, "per": {"a": 42}}
    assert fleet_state._DISK_COMPUTED_AT == 0.0


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
    # The message does not equal the bare legacy string: it appends a
    # dirt-detail tail. The legacy prefix is preserved for clients keying on it.
    assert res["error"].startswith("worktree has uncommitted changes")
    assert "uncommitted changes" in res["error"]
    # And the refusal now carries the structured dirt fields.
    assert res["dirty_tracked"] is True
    assert res["dirty_untracked"] == 1
    assert res["dirty_untracked_paths"] == ["scratch.log"]


async def _stated_main_snapshot() -> tuple[str | None, bool, str]:
    """A stand-in for ``_resolve_base_snapshot`` returning a STATED ``main`` on origin.

    ``_rebase_locked`` re-resolves the base into a LOCAL snapshot before reading its
    gate, and fetches/rebases from the SAME remote the snapshot verified against. These
    tests exercise the rebase mechanics PAST the gate, so the resolution is stubbed to a
    positive ``main`` on ``origin`` and the mechanics run on it.
    """
    return "main", True, "origin"


@pytest.mark.asyncio
async def test_rebase_locked_fetch_failure(monkeypatch):
    async def fake_git(path, *args, **kw):
        return "" if args[0] == "status" else None

    monkeypatch.setattr(repository, "_git", fake_git)
    # The base-branch gate sits before the fetch, and the rebase re-resolves
    # first; these exercise the rebase mechanics past it, so the resolution is
    # stubbed and the base pinned as STATED.
    monkeypatch.setattr(repository, "_resolve_base_snapshot", _stated_main_snapshot)
    res = await worktree_ops._rebase_locked({"path": "/r"})
    assert res["ok"] is False
    assert res["error"] == "git fetch origin main failed"


@pytest.mark.asyncio
async def test_rebase_locked_success(monkeypatch):
    async def fake_git(path, *args, **kw):
        return "" if args[0] == "status" else "ok"

    monkeypatch.setattr(repository, "_git", fake_git)
    # The base-branch gate sits before the fetch, and the rebase re-resolves
    # first; these exercise the rebase mechanics past it, so the resolution is
    # stubbed and the base pinned as STATED.
    monkeypatch.setattr(repository, "_resolve_base_snapshot", _stated_main_snapshot)
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
    # The base-branch gate sits before the fetch, and the rebase re-resolves
    # first; these exercise the rebase mechanics past it, so the resolution is
    # stubbed and the base pinned as STATED.
    monkeypatch.setattr(repository, "_resolve_base_snapshot", _stated_main_snapshot)
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
    # The base-branch gate sits before the fetch, and the rebase re-resolves
    # first; these exercise the rebase mechanics past it, so the resolution is
    # stubbed and the base pinned as STATED.
    monkeypatch.setattr(repository, "_resolve_base_snapshot", _stated_main_snapshot)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(1, "CONFLICT", "")))
    res = await worktree_ops._rebase_locked({"path": "/r"})
    assert res["conflict"] is True
    assert "manual recovery required" in res["error"]
