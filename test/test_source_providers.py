from __future__ import annotations

import asyncio
import inspect
import json
import os
import pathlib
import re
import sys
import tempfile
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import github_runner
from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.sandbox import spawn_shim_argv


@pytest.fixture(autouse=True)
def _mock_source_sel(monkeypatch):
    audit = MagicMock()
    monkeypatch.setattr(source, "_sel", lambda: audit)
    return audit


def test_parse_github_pull_request() -> None:
    ref = source.parse_source_url("https://github.com/kirodotdev/KiroCrew/pull/58?tab=checks")
    assert ref.provider == "github"
    assert ref.owner == "kirodotdev"
    assert ref.repo == "KiroCrew"
    assert ref.number == 58
    assert ref.url == "https://github.com/kirodotdev/KiroCrew/pull/58"


def test_github_check_active_status_is_pending_even_with_success_conclusion() -> None:
    check = source._github_check({"name": "CI", "status": "IN_PROGRESS", "conclusion": "SUCCESS"})

    assert check["bucket"] == "pending"


def test_github_checks_keep_only_the_latest_run_per_check() -> None:
    """One head sha can carry several runs of the same check (two dispatches of
    the same workflow), which inflated every count the panel reported."""
    rollup = [
        {
            "name": "GPT Review",
            "workflowName": "GPT Review",
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
            "startedAt": "2026-07-28T21:17:23Z",
            "completedAt": "2026-07-28T21:18:00Z",
        },
        {
            "name": "GPT Review",
            "workflowName": "GPT Review",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T21:20:44Z",
            "completedAt": "2026-07-28T21:25:00Z",
        },
        {
            "name": "GPT Review",
            "workflowName": "GPT Review",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-28T21:43:12Z",
            "completedAt": "2026-07-28T21:47:00Z",
        },
    ]

    checks = source._github_checks(rollup)

    assert [(check["name"], check["conclusion"]) for check in checks] == [("GPT Review", "SUCCESS")]


def test_github_checks_superseded_cancellation_does_not_paint_ci_red() -> None:
    """A concurrency-group cancellation whose replacement run passed must not
    roll up to `failed` — that red survived every refresh, because the stale row
    is still genuinely in the provider payload."""
    rollup = [
        {
            "name": "Review",
            "workflowName": "Review",
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
            "startedAt": "2026-07-28T20:56:29Z",
            "completedAt": "2026-07-28T20:57:00Z",
        },
        {
            "name": "Review",
            "workflowName": "Review",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-28T20:58:05Z",
            "completedAt": "2026-07-28T21:00:24Z",
        },
    ]

    buckets = [check["bucket"] for check in source._github_checks(rollup)]

    assert buckets == ["passed"]
    assert source._rollup_ci(buckets) == "passed"


def test_github_checks_queued_rerun_outranks_the_run_it_supersedes() -> None:
    """GitHub leaves `startedAt` null while a check-run is QUEUED. Ranking that
    row below the completed run it replaces would show a stale pass."""
    rollup = [
        {
            "name": "CI",
            "workflowName": "CI",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-28T20:00:00Z",
            "completedAt": "2026-07-28T20:05:00Z",
        },
        {"name": "CI", "workflowName": "CI", "status": "QUEUED", "conclusion": None},
    ]

    checks = source._github_checks(rollup)

    assert [check["bucket"] for check in checks] == ["pending"]


def test_github_checks_do_not_collapse_across_publishers() -> None:
    """Identity is (workflow, name): collapsing on the display name alone would
    let one workflow's later success hide another's failure."""
    rollup = [
        {
            "name": "Lint",
            "workflowName": "Backend",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T20:00:00Z",
        },
        {
            "name": "Lint",
            "workflowName": "Frontend",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-28T20:10:00Z",
        },
        # A legacy commit status has no workflow and keys on ("", context).
        {"context": "Lint", "state": "SUCCESS", "startedAt": "2026-07-28T20:20:00Z"},
    ]

    checks = source._github_checks(rollup)

    assert {(check["workflow"], check["bucket"]) for check in checks} == {
        ("Backend", "failed"),
        ("Frontend", "passed"),
        ("", "passed"),
    }
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_do_not_collapse_a_commit_status_into_a_check_run() -> None:
    """A legacy commit status and an app-published check-run both carry an empty
    workflow, so without the row kind in the identity one publisher's later
    success would hide the other's failure and roll the glyph up green."""
    rollup = [
        {
            "__typename": "CheckRun",
            "name": "CI",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T20:00:00Z",
        },
        {
            "__typename": "StatusContext",
            "context": "CI",
            "state": "SUCCESS",
            "startedAt": "2026-07-28T20:30:00Z",
        },
    ]

    checks = source._github_checks(rollup)

    assert sorted(check["bucket"] for check in checks) == ["failed", "passed"]
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_classify_untyped_rows_by_shape_not_by_name() -> None:
    """A row without `__typename` must still be classified correctly. A status
    row carrying both `context` and `name` used to be read as a check-run and
    collide with a nameless check-run (both normalize to the `"Check"`
    placeholder), letting the status success hide the check-run failure."""
    rollup = [
        {
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T20:00:00Z",
        },
        {
            "context": "Check",
            "name": "Check",
            "state": "SUCCESS",
            "startedAt": "2026-07-28T20:30:00Z",
        },
    ]

    checks = source._github_checks(rollup)

    assert sorted(check["bucket"] for check in checks) == ["failed", "passed"]
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_keep_matrix_legs_of_one_job_distinct() -> None:
    """GitHub appends matrix values to a check-run's name even when the workflow
    sets an explicit `name:`, so sibling shards of one job have distinct names.
    A failing shard must never be folded into a later-starting shard's success.
    """
    rollup = [
        {
            "__typename": "CheckRun",
            "name": "Backend Tests (3.10, 2)",
            "workflowName": "CI",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T20:00:00Z",
        },
        {
            "__typename": "CheckRun",
            "name": "Backend Tests (3.12, 4)",
            "workflowName": "CI",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-28T20:10:00Z",
        },
    ]

    checks = source._github_checks(rollup)

    assert len(checks) == 2
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_never_collapse_workflowless_check_runs() -> None:
    """A check-run with no workflow comes from an app outside Actions, and the
    rollup carries no check-suite or run-attempt id to tell a superseded re-run
    from a same-named check by a different app. Leave such rows uncollapsed:
    over-counting is cosmetic, hiding a red behind another app's green is not."""
    rollup = [
        {
            "__typename": "CheckRun",
            "name": "security/scan",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T20:00:00Z",
            "detailsUrl": "https://app-one.example/run/1",
        },
        {
            "__typename": "CheckRun",
            "name": "security/scan",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-28T20:10:00Z",
            "detailsUrl": "https://app-two.example/run/9",
        },
    ]

    checks = source._github_checks(rollup)

    assert len(checks) == 2
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_preserve_first_appearance_order() -> None:
    """A re-run replaces a row in place instead of reshuffling the list."""
    rollup = [
        {"name": "A", "workflowName": "W", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "B", "workflowName": "W", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {
            "name": "A",
            "workflowName": "W",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T21:00:00Z",
        },
    ]

    assert [check["name"] for check in source._github_checks(rollup)] == ["A", "B"]


def test_safe_error_redacts_credentials_and_exfiltration_urls() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    payload = "x" * 80
    error = source._safe_error(
        f"failed with {secret} at https://attacker.example/c?data={payload}".encode()
    )
    assert secret not in error
    assert payload not in error
    assert "[REDACTED" in error


def test_provider_executable_rejects_agent_writable_tree(monkeypatch, tmp_path) -> None:
    """A gh shim planted inside the project checkout is refused even though it
    is user-owned like every other accepted install — the agent can write there."""
    project = tmp_path / "project"
    (project / "bin").mkdir(parents=True)
    shim = project / "bin" / "gh"
    shim.write_text("#!/bin/sh\nexit 99\n")
    shim.chmod(0o755)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(project))
    monkeypatch.setenv("KIROCREW_GH_BIN", str(shim))

    with pytest.raises(source.SourceProviderError, match="inside the agent-writable tree"):
        source._resolve_provider_executable("gh")


def test_provider_executable_candidates_append_path_hits(monkeypatch, tmp_path) -> None:
    """Well-known dirs are tried first, then whatever PATH resolves — so an
    install the user already runs from their terminal is found."""
    user_bin = tmp_path / "user-bin"
    user_bin.mkdir()
    found = user_bin / "gh"
    found.write_text("#!/bin/sh\nexit 0\n")
    found.chmod(0o755)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setenv("PATH", f"{user_bin}:/usr/bin")
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": ("/usr/local/libexec/kirocrew/gh",), "glab": ("/usr/bin/glab",)},
    )

    candidates = source.provider_executable_candidates("gh")

    assert candidates[0] == "/usr/local/libexec/kirocrew/gh"
    assert str(found) in candidates


def test_provider_executable_candidates_ignore_path_in_strict_mode(monkeypatch, tmp_path) -> None:
    user_bin = tmp_path / "user-bin"
    user_bin.mkdir()
    planted = user_bin / "gh"
    planted.write_text("#!/bin/sh\nexit 0\n")
    planted.chmod(0o755)
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setenv("PATH", str(user_bin))

    assert (
        source.provider_executable_candidates("gh")
        == source._PROVIDER_EXECUTABLE_CANDIDATES["gh"]
    )


def test_provider_executable_not_found_gives_install_guidance(monkeypatch) -> None:
    monkeypatch.delenv("KIROCREW_GH_BIN", raising=False)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": ("/nonexistent-kirocrew/gh",), "glab": ("/nonexistent-kirocrew/glab",)},
    )

    with pytest.raises(source.SourceProviderError) as excinfo:
        source._resolve_provider_executable("gh")

    message = str(excinfo.value)
    # Install-and-sign-in guidance, NOT the old root-owned sudo copy ritual.
    assert "brew install gh" in message
    assert "gh auth login" in message
    assert "sudo cp" not in message
    assert "KIROCREW_GH_BIN" in message
    assert "{executable}" not in message


def test_provider_executable_strict_mode_asks_for_a_root_owned_copy(monkeypatch) -> None:
    monkeypatch.delenv("KIROCREW_GH_BIN", raising=False)
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setattr(
        github_runner,
        "PROVIDER_EXECUTABLE_CANDIDATES",
        {
            "gh": ("/usr/local/libexec/kirocrew/gh",),
            "glab": ("/usr/local/libexec/kirocrew/glab",),
        },
    )
    monkeypatch.setattr(
        source,
        "_validate_provider_executable",
        MagicMock(side_effect=ValueError("executable is not root-owned")),
    )

    with pytest.raises(source.SourceProviderError) as excinfo:
        source._resolve_provider_executable("gh")

    message = str(excinfo.value)
    assert "KIROCREW_PROVIDER_BIN_STRICT" in message
    assert 'sudo cp "$(command -v gh)" /usr/local/libexec/kirocrew/gh' in message
    assert "gh auth login" in message


def test_provider_executable_rejects_relative_override(monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_GH_BIN", "workspace/bin/gh")

    with pytest.raises(source.SourceProviderError, match="path must be absolute"):
        source._resolve_provider_executable("gh")


_tmp_owner_ok = (
    sys.platform == "win32"
    or os.stat(tempfile.gettempdir()).st_uid in (0, os.geteuid())
)


@pytest.mark.skipif(not _tmp_owner_ok, reason="temp dir not owned by root or current user")
def test_provider_executable_accepts_user_owned_install(monkeypatch, tmp_path) -> None:
    """The default policy accepts the user's own gh — the Homebrew case that
    previously forced a `sudo cp` into a root-owned directory."""
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setenv("KIROCREW_GH_BIN", str(executable))

    assert source._resolve_provider_executable("gh") == str(executable.resolve())


@pytest.mark.skipif(not _tmp_owner_ok, reason="temp dir not owned by root or current user")
def test_provider_executable_accepts_symlinked_install(monkeypatch, tmp_path) -> None:
    """Homebrew's layout (bin/gh -> ../Cellar/gh/<v>/bin/gh) resolves through the
    symlink instead of being refused for not being canonical."""
    cellar = tmp_path / "Cellar" / "gh" / "2.0.0" / "bin"
    cellar.mkdir(parents=True)
    target = cellar / "gh"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o555)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "gh"
    link.symlink_to(target)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setenv("KIROCREW_GH_BIN", str(link))

    assert source._resolve_provider_executable("gh") == str(target.resolve())


def test_provider_executable_strict_mode_rejects_user_owned_install(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o500)
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setenv("KIROCREW_GH_BIN", str(executable))

    with pytest.raises(source.SourceProviderError, match="executable is not root-owned"):
        source._resolve_provider_executable("gh")


def test_provider_executable_strict_mode_rejects_symlink(monkeypatch, tmp_path) -> None:
    target = tmp_path / "real-gh"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o500)
    link = tmp_path / "gh"
    link.symlink_to(target)
    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setenv("KIROCREW_GH_BIN", str(link))

    with pytest.raises(source.SourceProviderError, match="canonical.*no symlinks"):
        source._resolve_provider_executable("gh")


def test_provider_executable_refuses_a_root_gateway(monkeypatch, tmp_path) -> None:
    """A root gateway is refused in BOTH modes: every process it spawns (the
    agent's own shell included) is root too, which makes the ownership and
    agent-tree checks vacuous."""
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(github_runner.os, "geteuid", lambda: 0)

    with pytest.raises(ValueError, match="disabled for a root gateway"):
        source._validate_provider_executable(str(executable))


def test_provider_executable_rejects_binary_owned_by_another_user(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    real_stat = executable.stat()
    foreign_stat = github_runner.os.stat_result([*list(real_stat)[:4], 4242, *list(real_stat)[5:]])
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(github_runner, "path_parents", lambda _path: [])
    monkeypatch.setattr(github_runner.Path, "stat", lambda _path: foreign_stat)

    with pytest.raises(ValueError, match="owned by another user"):
        source._validate_provider_executable(str(executable))


def test_provider_executable_rejects_world_writable_binary(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o777)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())

    with pytest.raises(ValueError, match="executable is world-writable"):
        source._validate_provider_executable(str(executable))


def test_provider_executable_rejects_world_writable_parent(monkeypatch, tmp_path) -> None:
    parent = tmp_path / "provider-bin"
    parent.mkdir()
    executable = parent / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    parent.chmod(0o777)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(github_runner, "path_parents", lambda _path: [parent])

    with pytest.raises(ValueError, match="executable parent is world-writable"):
        source._validate_provider_executable(str(executable))


def test_provider_executable_tolerates_a_sticky_world_writable_parent(
    monkeypatch, tmp_path
) -> None:
    """`/tmp`-style 1777 dirs are fine: only the owner can replace an entry, so
    the "owned by another user" check still decides. Linux CI runners put every
    pytest tmp dir under /tmp, so rejecting sticky dirs outright would also make
    the accept-path untestable there."""
    parent = tmp_path / "sticky-bin"
    parent.mkdir()
    executable = parent / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    parent.chmod(0o1777)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(github_runner, "path_parents", lambda _path: [parent])

    assert source._validate_provider_executable(str(executable)) == str(executable.resolve())


def test_provider_executable_strict_mode_rejects_untrusted_ancestor(
    monkeypatch, tmp_path
) -> None:
    """Strict mode keeps the historical root-owned, unwritable-ancestor rule."""
    parent = tmp_path / "provider-bin"
    parent.mkdir()
    executable = parent / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o500)
    executable_stat = executable.stat()
    root_executable_stat = github_runner.os.stat_result(
        [*list(executable_stat)[:4], 0, *list(executable_stat)[5:]]
    )
    real_stat = github_runner.Path.stat

    def fake_stat(path):
        if path == executable:
            return root_executable_stat
        return real_stat(path)

    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setattr(github_runner, "path_parents", lambda _path: [parent])
    monkeypatch.setattr(github_runner.Path, "stat", fake_stat)
    monkeypatch.setattr(github_runner.os, "access", lambda _path, mode: mode == github_runner.os.X_OK)

    with pytest.raises(ValueError, match="executable parent is not root-owned"):
        source._validate_provider_executable(str(executable))


def test_redact_provider_data_recurses_through_external_strings() -> None:
    secret = "ghp_" + "a" * 36
    query = "x" * 80
    raw = {
        "description": f"token={secret}",
        "files": [{"patch": f"+{secret}"}],
        "comments": [{"body": f"see https://attacker.example/c?data={query}"}],
        "count": 1,
    }

    cleaned = source._redact_provider_data(raw)

    serialized = source.json.dumps(cleaned)
    assert secret not in serialized
    assert query not in serialized
    assert serialized.count("[REDACTED") >= 3
    assert cleaned["count"] == 1


@pytest.mark.asyncio
async def test_fetch_rejects_aggregate_payload_over_limit(monkeypatch) -> None:
    source._CACHE.clear()
    fetch = AsyncMock(return_value={"provider": "github", "description": "x" * 200})
    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(source, "_MAX_PAYLOAD_BYTES", 100)
    url = "https://github.com/acme/repo/pull/10"

    with pytest.raises(source.SourceProviderError, match="payload was too large"):
        await source.fetch_pull_request(url)

    assert url not in source._CACHE


@pytest.mark.asyncio
async def test_fetch_cache_evicts_oldest_entry_by_aggregate_weight(monkeypatch) -> None:
    source._CACHE.clear()

    async def fake_fetch(ref):
        return {"provider": "github", "url": ref.url, "description": "x" * 80}

    monkeypatch.setattr(source, "_fetch_github", fake_fetch)
    monkeypatch.setattr(source, "_CACHE_MAX_BYTES", 180)
    monkeypatch.setattr(source, "_MAX_PAYLOAD_BYTES", 1_000)
    first = "https://github.com/acme/repo/pull/10"
    second = "https://github.com/acme/repo/pull/11"

    await source.fetch_pull_request(first)
    await source.fetch_pull_request(second)

    assert first not in source._CACHE
    assert second in source._CACHE
    stored_at, stored_size, stored_payload = source._CACHE[second]
    assert stored_at > 0
    assert stored_size == source._payload_size_bytes(stored_payload)
    assert sum(entry[1] for entry in source._CACHE.values()) <= source._CACHE_MAX_BYTES


# ── Terminal-state retention ─────────────────────────────────────────────────
# A merged or closed pull request never moves on its own, so both caches keep it
# for `_TERMINAL_TTL_SECS` instead of re-reading it on the open-PR cadence for
# as long as its chip sits in a sidebar.


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"state": "OPEN"}, source._CACHE_TTL_SECS),
        ({"state": "OPEN", "draft": True}, source._CACHE_TTL_SECS),
        ({"state": "opened"}, source._CACHE_TTL_SECS),
        ({"state": "MERGED"}, source._TERMINAL_TTL_SECS),
        ({"state": "merged"}, source._TERMINAL_TTL_SECS),
        ({"state": "CLOSED"}, source._CLOSED_TTL_SECS),
        # A closed-while-draft GitLab MR is closed, not draft (see _project_state).
        ({"state": "closed", "draft": True}, source._CLOSED_TTL_SECS),
        # Transient / unknown lifecycles are not terminal.
        ({"state": "locked"}, source._CACHE_TTL_SECS),
        ({}, source._CACHE_TTL_SECS),
    ],
)
def test_full_payload_ttl_is_decided_by_projected_lifecycle(payload, expected) -> None:
    assert source._full_payload_ttl(payload) == expected


def test_terminal_ttl_is_the_longer_one() -> None:
    """The property every retention test below rests on: merged outlives
    closed, and both outlive the open cadence of either cache."""
    assert source._TERMINAL_TTL_SECS > source._CLOSED_TTL_SECS
    assert source._CLOSED_TTL_SECS > source._CACHE_TTL_SECS
    assert source._CLOSED_TTL_SECS > source._CHECK_TTL_SECS
    assert source._TERMINAL_CHIP_STATES == {"merged", "closed"}


@pytest.mark.asyncio
async def test_fetch_pull_request_serves_a_terminal_payload_past_the_open_ttl(monkeypatch) -> None:
    """With no conditional read available (GitLab), a merged MR aged past the
    open TTL is still a cache hit -- no provider read at all."""
    url = "https://gitlab.com/acme/repo/-/merge_requests/21"
    source._CACHE.clear()
    fetch = AsyncMock(return_value={"state": "merged", "checks": []})
    monkeypatch.setattr(source, "_fetch_gitlab", fetch)
    monkeypatch.setattr(source, "_gh_conditional_get", AsyncMock(side_effect=AssertionError))
    stale_for_open = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    cached = {"state": "merged", "url": url, "marker": "cached"}
    source._CACHE[url] = (stale_for_open, 10, cached)
    try:
        assert await source.fetch_pull_request(url) is cached
        fetch.assert_not_awaited()
        # The explicit refresh button still bypasses every TTL.
        result = await source.fetch_pull_request(url, refresh=True)
        assert result.get("marker") is None
        fetch.assert_awaited_once()
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_closed_payload_ages_on_the_closed_clock_without_a_conditional_read(
    monkeypatch,
) -> None:
    """Closed outlives the open TTL but not the merged one: it can be reopened."""
    url = "https://gitlab.com/acme/repo/-/merge_requests/24"
    source._CACHE.clear()
    fetch = AsyncMock(return_value={"state": "closed"})
    monkeypatch.setattr(source, "_fetch_gitlab", fetch)
    cached = {"state": "closed", "marker": "cached"}
    try:
        source._CACHE[url] = (source.time.monotonic() - source._CLOSED_TTL_SECS + 60, 10, cached)
        assert await source.fetch_pull_request(url) is cached
        fetch.assert_not_awaited()
        source._CACHE[url] = (source.time.monotonic() - source._CLOSED_TTL_SECS - 1, 10, cached)
        assert (await source.fetch_pull_request(url)).get("marker") is None
        fetch.assert_awaited_once()
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_expired_terminal_github_payload_is_revalidated_with_the_issue_probe_only(
    monkeypatch,
) -> None:
    """A finished github.com pull request keeps accruing discussion and a closed
    one can be reopened, so it is revalidated on the open cadence -- but its CI
    is over, so only the issue probe is sent (one rate-limit-free request)."""
    url = "https://github.com/acme/repo/pull/21"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    fetch = AsyncMock(return_value={"state": "MERGED", "headSha": "a" * 40, "marker": "fresh"})
    monkeypatch.setattr(source, "_fetch_github", fetch)
    probes = _FakeProbes(
        source._ConditionalRead(304, '"i"'),
        source._ConditionalRead(200, 'W/"never-asked"'),
        source._ConditionalRead(200, 'W/"never-asked"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    source._REVALIDATORS[url] = source._Revalidator('"i"', "", "", "a" * 40, 1.0)
    stale_for_open = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    cached = {"state": "MERGED", "headSha": "a" * 40, "marker": "cached"}
    source._CACHE[url] = (stale_for_open, 10, cached)
    try:
        assert await source.fetch_pull_request(url) is cached
        fetch.assert_not_awaited()
        assert set(probes.sent) == {"issue"}
        assert source._CACHE[url][0] > stale_for_open

        # A post-merge comment (or a reopen) moves the issue ETag: full read.
        moved = _FakeProbes(
            source._ConditionalRead(200, 'W/"i2"'),
            source._ConditionalRead(304, '"c"'),
        )
        monkeypatch.setattr(source, "_gh_conditional_get", moved)
        source._CACHE[url] = (stale_for_open, 10, cached)
        assert (await source.fetch_pull_request(url)).get("marker") == "fresh"
        fetch.assert_awaited_once()
        assert set(moved.sent) == {"issue"}
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_fetch_pull_request_refetches_an_open_payload_past_the_open_ttl(monkeypatch) -> None:
    """The open-PR cadence is unchanged: the same age on an OPEN payload is a miss."""
    url = "https://github.com/acme/repo/pull/22"
    source._CACHE.clear()
    fetch = AsyncMock(return_value={"state": "OPEN", "checks": []})
    monkeypatch.setattr(source, "_fetch_github", fetch)
    stale_for_open = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    source._CACHE[url] = (stale_for_open, 10, {"state": "OPEN", "marker": "cached"})
    try:
        result = await source.fetch_pull_request(url)
        assert result.get("marker") is None
        fetch.assert_awaited_once()
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_terminal_payload_re_reads_once_past_its_own_ttl(monkeypatch) -> None:
    """Terminal retention is long, not infinite: past its own TTL it re-reads."""
    url = "https://gitlab.com/acme/repo/-/merge_requests/23"
    source._CACHE.clear()
    fetch = AsyncMock(return_value={"state": "merged", "checks": []})
    monkeypatch.setattr(source, "_fetch_gitlab", fetch)
    expired = source.time.monotonic() - source._TERMINAL_TTL_SECS - 5
    source._CACHE[url] = (expired, 10, {"state": "merged", "marker": "cached"})
    try:
        result = await source.fetch_pull_request(url)
        assert result.get("marker") is None
        fetch.assert_awaited_once()
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_write_sweep_keeps_a_terminal_entry_older_than_the_open_ttl(monkeypatch) -> None:
    """The on-write expiry sweep ages each entry by its OWN lifecycle TTL."""
    source._CACHE.clear()
    monkeypatch.setattr(source, "_fetch_github", AsyncMock(return_value={"state": "OPEN"}))
    merged = "https://github.com/acme/repo/pull/30"
    open_pr = "https://github.com/acme/repo/pull/31"
    written = "https://github.com/acme/repo/pull/32"
    stale_for_open = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    source._CACHE[merged] = (stale_for_open, 10, {"state": "MERGED"})
    source._CACHE[open_pr] = (stale_for_open, 10, {"state": "OPEN"})
    try:
        await source.fetch_pull_request(written)
        assert merged in source._CACHE
        assert open_pr not in source._CACHE
        assert written in source._CACHE
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.parametrize(
    ("entry_state", "age", "force", "due"),
    [
        # Open PRs: the chip TTL, and force bypasses it.
        ("open", 1, False, False),
        ("open", None, False, True),  # None = past _CHECK_TTL_SECS
        ("open", 1, True, True),
        ("draft", 1, True, True),
        # Closed: the closed clock (shorter than merged), and a turn boundary may
        # re-read it (an agent can reopen).
        ("closed", None, False, False),
        ("closed", None, True, True),
        ("closed", "closed", False, True),  # past _CLOSED_TTL_SECS
        ("closed", "terminal", False, True),  # past _TERMINAL_TTL_SECS
        # Merged: long TTL and NOT force-read — it cannot change.
        ("merged", None, False, False),
        ("merged", None, True, False),
        ("merged", "closed", False, False),  # the closed clock is not merged's
        ("merged", "terminal", False, True),
        ("merged", "terminal", True, True),
        # No lifecycle known yet: chip TTL applies (status may be None).
        ("", 1, False, False),
        ("", None, False, True),
    ],
)
def test_chip_refresh_due_by_lifecycle(entry_state, age, force, due) -> None:
    now = 1_000_000.0
    if age is None:
        age = source._CHECK_TTL_SECS + 1
    elif age == "closed":
        age = source._CLOSED_TTL_SECS + 1
    elif age == "terminal":
        age = source._TERMINAL_TTL_SECS + 1
    status = {"state": entry_state} if entry_state else None
    entry = (now - age, status)
    assert source._chip_refresh_due(entry, now, force=force) is due


def test_chip_refresh_due_for_a_missing_entry() -> None:
    assert source._chip_refresh_due(None, 0.0, force=False) is True
    assert source._chip_refresh_due(None, 0.0, force=True) is True


@pytest.mark.asyncio
async def test_schedule_check_refresh_skips_finished_pull_requests(monkeypatch) -> None:
    """The periodic chip sweep spends no provider read on a merged or closed PR
    whose entry is past the open-PR TTL; a turn boundary re-reads a closed one
    but never a merged one."""
    merged = "https://github.com/acme/repo/pull/40"
    closed = "https://github.com/acme/repo/pull/41"
    open_pr = "https://github.com/acme/repo/pull/42"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    refresh = AsyncMock(return_value=None)
    monkeypatch.setattr(source, "_refresh_check_status", refresh)
    stale = source.time.monotonic() - source._CHECK_TTL_SECS - 1
    source._check_cache[merged] = (stale, {"state": "merged", "ci": "passed"})
    source._check_cache[closed] = (stale, {"state": "closed"})
    source._check_cache[open_pr] = (stale, {"state": "open", "ci": "running"})
    try:
        assert source.schedule_check_refresh([merged, closed, open_pr]) == [open_pr]
        assert source._check_inflight == {open_pr}
        source._check_inflight.clear()

        forced = source.request_check_refresh_now([merged, closed, open_pr])
        assert set(forced) == {closed, open_pr}
        assert merged not in source._check_inflight
        assert merged not in source._check_forced_at
    finally:
        source._check_cache.clear()
        source._check_inflight.clear()
        source._check_forced_at.clear()


# ── Conditional revalidation (If-None-Match probes) ──────────────────────────


def _gh_i_output(status: int, reason: str, etag: str, body: str, sep: str = "\r\n") -> bytes:
    lines = [f"HTTP/2.0 {status} {reason}", "Content-Type: application/json; charset=utf-8"]
    if etag:
        lines.append(f"Etag: {etag}")
    return (sep.join(lines) + sep + sep + body).encode()


def test_parse_conditional_get_reads_status_and_etag_and_ignores_the_body() -> None:
    parse = source._parse_conditional_get("gh")
    read = parse(0, _gh_i_output(200, "OK", 'W/"abc"', '{"updated_at": "x"}'), b"")
    assert read == source._ConditionalRead(200, 'W/"abc"')


def test_parse_conditional_get_treats_304_exit_1_as_success() -> None:
    """`gh` exits 1 on every non-2xx status, so the 304 the request exists for
    arrives as a failure exit code and must be read from the status line."""
    parse = source._parse_conditional_get("gh")
    read = parse(1, _gh_i_output(304, "Not Modified", '"abc"', ""), b"gh: HTTP 304\n")
    assert read == source._ConditionalRead(304, '"abc"')


def test_parse_conditional_get_accepts_lf_separated_headers_and_missing_etag() -> None:
    parse = source._parse_conditional_get("gh")
    read = parse(0, _gh_i_output(200, "OK", "", "[]", sep="\n"), b"")
    assert read == source._ConditionalRead(200, "")


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (1, _gh_i_output(404, "Not Found", "", '{"message": "Not Found"}'), b"gh: Not Found"),
        (1, _gh_i_output(401, "Unauthorized", "", "{}"), b"gh: HTTP 401: Bad credentials"),
        (1, b"", b"gh: authentication required"),
        (0, b"not a status line\r\n\r\n{}", b""),
    ],
)
def test_parse_conditional_get_rejects_everything_but_200_and_304(
    returncode, stdout, stderr
) -> None:
    parse = source._parse_conditional_get("gh")
    with pytest.raises(source.SourceProviderError):
        parse(returncode, stdout, stderr)


def test_parse_conditional_get_appends_login_hint_on_auth_failure() -> None:
    parse = source._parse_conditional_get("gh")
    with pytest.raises(source.SourceProviderError, match="gh auth login"):
        parse(1, b"", b"gh: authentication required")


@pytest.mark.asyncio
async def test_gh_conditional_get_sends_if_none_match_only_when_known(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run_provider(*argv, **kwargs):
        calls.append(argv)
        assert kwargs["parse"] is not None
        return source._ConditionalRead(304, '"e2"')

    monkeypatch.setattr(source, "_run_provider", fake_run_provider)
    await source._gh_conditional_get("repos/acme/repo/issues/7", "")
    await source._gh_conditional_get("repos/acme/repo/issues/7", 'W/"e1"')
    assert calls == [
        ("gh", "api", "repos/acme/repo/issues/7", "-i"),
        ("gh", "api", "repos/acme/repo/issues/7", "-i", "-H", 'If-None-Match: W/"e1"'),
    ]


class _FakeProbes:
    """Scripted `_gh_conditional_get`: answers by path and records the validator
    each probe was sent. `status` defaults to a 304 echo so tests written around
    the issue/check-runs pair stay focused on them."""

    def __init__(
        self,
        issue: source._ConditionalRead,
        checks: source._ConditionalRead,
        status: source._ConditionalRead | None = None,
    ) -> None:
        self.issue = issue
        self.checks = checks
        self.status = status or source._ConditionalRead(304, '"s1"')
        self.sent: dict[str, str] = {}

    async def __call__(self, path: str, etag: str, **_: object) -> source._ConditionalRead:
        if "/check-runs" in path:
            kind = "checks"
        elif path.endswith("/status"):
            kind = "status"
        else:
            kind = "issue"
        self.sent[kind] = etag
        if isinstance(getattr(self, kind), Exception):
            raise getattr(self, kind)
        return getattr(self, kind)


_OPEN_PAYLOAD = {"state": "OPEN", "headSha": "a" * 40, "url": "https://github.com/acme/repo/pull/7"}
_REF = source.parse_source_url("https://github.com/acme/repo/pull/7")


@pytest.mark.asyncio
async def test_payload_unchanged_first_probe_learns_validators_and_is_unknown(monkeypatch) -> None:
    """With nothing to send, a 200 is the only possible answer, and a 200 is
    never read as unchanged -- it returns the validators for the caller to
    commit once the full read has landed."""
    source._REVALIDATORS.clear()
    probes = _FakeProbes(
        source._ConditionalRead(200, 'W/"i1"'),
        source._ConditionalRead(200, 'W/"c1"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    try:
        outcome = await source._probe_github_payload(_REF, _OPEN_PAYLOAD)
        assert outcome.unchanged is False
        assert probes.sent == {"issue": "", "checks": "", "status": ""}
        learned = outcome.learned
        assert learned is not None
        assert (learned.issue_etag, learned.checks_etag, learned.head_sha) == (
            'W/"i1"',
            'W/"c1"',
            "a" * 40,
        )
        # Nothing is committed on a 200 until the fanout that follows succeeds.
        assert _REF.url not in source._REVALIDATORS
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.asyncio
async def test_payload_unchanged_requires_both_probes_to_answer_304(monkeypatch) -> None:
    source._REVALIDATORS.clear()
    source._REVALIDATORS[_REF.url] = source._Revalidator('W/"i1"', 'W/"c1"', 'W/"s1"', "a" * 40, 1.0)
    try:
        both = _FakeProbes(
            source._ConditionalRead(304, '"i1"'), source._ConditionalRead(304, '"c1"')
        )
        monkeypatch.setattr(source, "_gh_conditional_get", both)
        assert (await source._probe_github_payload(_REF, _OPEN_PAYLOAD)).unchanged is True
        assert both.sent == {"issue": 'W/"i1"', "checks": 'W/"c1"', "status": 'W/"s1"'}
        # A 304 echoes the validator in the strong form; whichever form the
        # server sent last is what goes out next.
        assert source._REVALIDATORS[_REF.url].issue_etag == '"i1"'

        # CI moved: check-runs answers 200 while the issue half is still 304.
        ci_moved = _FakeProbes(
            source._ConditionalRead(304, '"i1"'),
            source._ConditionalRead(200, 'W/"c2"'),
        )
        monkeypatch.setattr(source, "_gh_conditional_get", ci_moved)
        moved = await source._probe_github_payload(_REF, _OPEN_PAYLOAD)
        assert moved.unchanged is False
        assert moved.learned is not None and moved.learned.checks_etag == 'W/"c2"'
        # ...and the committed validators still describe the payload the cache holds.
        assert source._REVALIDATORS[_REF.url].checks_etag == '"c1"'

        # A review landed: the issue half answers 200.
        review = _FakeProbes(
            source._ConditionalRead(200, 'W/"i2"'),
            source._ConditionalRead(304, '"c2"'),
        )
        monkeypatch.setattr(source, "_gh_conditional_get", review)
        assert (await source._probe_github_payload(_REF, _OPEN_PAYLOAD)).unchanged is False
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.asyncio
async def test_payload_unchanged_does_not_reuse_check_runs_etag_across_a_push(monkeypatch) -> None:
    """The check-runs validator belongs to a commit; after a push the old one
    would keep answering 304 for a head nobody is looking at."""
    source._REVALIDATORS.clear()
    source._REVALIDATORS[_REF.url] = source._Revalidator('W/"i1"', 'W/"c1"', 'W/"s1"', "a" * 40, 1.0)
    probes = _FakeProbes(
        source._ConditionalRead(304, '"i1"'),
        source._ConditionalRead(200, 'W/"c-new"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    try:
        pushed = {**_OPEN_PAYLOAD, "headSha": "b" * 40}
        outcome = await source._probe_github_payload(_REF, pushed)
        assert outcome.unchanged is False
        assert probes.sent["checks"] == ""
        assert probes.sent["issue"] == 'W/"i1"'
        assert outcome.learned is not None and outcome.learned.head_sha == "b" * 40
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.asyncio
async def test_payload_unchanged_is_unknown_on_probe_failure_or_missing_head(monkeypatch) -> None:
    source._REVALIDATORS.clear()
    source._REVALIDATORS[_REF.url] = source._Revalidator('W/"i1"', 'W/"c1"', 'W/"s1"', "a" * 40, 1.0)
    failing = _FakeProbes(
        source.SourceProviderError("gh: HTTP 500"),  # type: ignore[arg-type]
        source._ConditionalRead(304, '"c1"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", failing)
    try:
        assert await source._probe_github_payload(_REF, _OPEN_PAYLOAD) == source._PROBE_UNKNOWN
        # Validators learned before the failure survive it untouched.
        assert source._REVALIDATORS[_REF.url].issue_etag == 'W/"i1"'
        assert await source._probe_github_payload(_REF, {"state": "OPEN"}) == source._PROBE_UNKNOWN
    finally:
        source._REVALIDATORS.clear()


def test_revalidators_map_is_bounded(monkeypatch) -> None:
    source._REVALIDATORS.clear()
    monkeypatch.setattr(source, "_REVALIDATORS_MAX", 3)
    try:
        for index in range(5):
            source._REVALIDATORS[f"u{index}"] = source._Revalidator("", "", "", "", float(index))
        source._trim_revalidators()
        assert set(source._REVALIDATORS) == {"u2", "u3", "u4"}
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/repo/pull/7", True),
        # GitLab's API is not probed.
        ("https://gitlab.com/acme/repo/-/merge_requests/7", False),
    ],
)
def test_revalidation_applies_to_github_only(url, expected) -> None:
    ref = source.parse_source_url(url)
    assert source._revalidation_applies(ref) is expected


@pytest.mark.asyncio
async def test_terminal_payload_probes_the_issue_only(monkeypatch) -> None:
    """CI is over for a merged or closed pull request, so the two commit-level
    probes are not sent; the issue probe alone decides (reopen, comments)."""
    source._REVALIDATORS.clear()
    probes = _FakeProbes(
        source._ConditionalRead(304, '"i1"'),
        source._ConditionalRead(200, 'W/"c"'),
        source._ConditionalRead(200, 'W/"s"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    source._REVALIDATORS[_REF.url] = source._Revalidator('"i1"', "", "", "a" * 40, 1.0)
    try:
        for state in ("MERGED", "CLOSED"):
            probes.sent.clear()
            payload = {**_OPEN_PAYLOAD, "state": state}
            assert (await source._probe_github_payload(_REF, payload)).unchanged is True
            assert set(probes.sent) == {"issue"}
        # Validators for the commit half are carried, not blanked, by the skip.
        assert source._REVALIDATORS[_REF.url].checks_etag == ""
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.asyncio
async def test_open_payload_requires_the_commit_status_probe_too(monkeypatch) -> None:
    """Legacy commit statuses are a separate resource from check runs and the
    panel's rollup renders both, so a moved status ETag alone re-reads."""
    source._REVALIDATORS.clear()
    source._REVALIDATORS[_REF.url] = source._Revalidator('"i"', '"c"', '"s"', "a" * 40, 1.0)
    probes = _FakeProbes(
        source._ConditionalRead(304, '"i"'),
        source._ConditionalRead(304, '"c"'),
        source._ConditionalRead(200, 'W/"s2"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    try:
        outcome = await source._probe_github_payload(_REF, _OPEN_PAYLOAD)
        assert outcome.unchanged is False
        assert probes.sent == {"issue": '"i"', "checks": '"c"', "status": '"s"'}
        assert outcome.learned is not None and outcome.learned.status_etag == 'W/"s2"'
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.asyncio
async def test_validators_from_a_200_are_committed_only_after_the_fanout_succeeds(
    monkeypatch,
) -> None:
    """A probe that answers 200 describes a payload the cache does not hold
    yet. If the fanout then fails, the old validators must stay: committing the
    new ones would pair the pre-change payload with post-change ETags, and every
    later probe would answer 304 against it and re-stamp the stale payload as
    current for good."""
    url = "https://github.com/acme/repo/pull/23"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    source._REVALIDATORS[url] = source._Revalidator('"i1"', '"c1"', '"s1"', "a" * 40, 1.0)
    probes = _FakeProbes(
        source._ConditionalRead(200, 'W/"i2"'),
        source._ConditionalRead(304, '"c1"'),
        source._ConditionalRead(304, '"s1"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    stale = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    cached = {"state": "OPEN", "headSha": "a" * 40, "marker": "cached"}
    source._CACHE[url] = (stale, 10, cached)
    fetch = AsyncMock(side_effect=source.SourceProviderError("gh: HTTP 503"))
    monkeypatch.setattr(source, "_fetch_github", fetch)
    try:
        with pytest.raises(source.SourceProviderError):
            await source.fetch_pull_request(url)
        assert source._REVALIDATORS[url].issue_etag == '"i1"'

        # The same probe answer followed by a fanout that lands commits them.
        fetch = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40, "marker": "fresh"})
        monkeypatch.setattr(source, "_fetch_github", fetch)
        source._CACHE[url] = (stale, 10, cached)
        assert (await source.fetch_pull_request(url)).get("marker") == "fresh"
        assert source._REVALIDATORS[url].issue_etag == 'W/"i2"'
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_revalidation_forces_a_full_read_past_the_max_age(monkeypatch) -> None:
    """Re-stamping rests on the issue ETag moving for every rendered field,
    which GitHub does not promise; past the ceiling one full read runs without
    probing and the validators are dropped so the next cycle learns afresh."""
    url = "https://github.com/acme/repo/pull/29"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    now = source.time.monotonic()
    aged = now - source._REVALIDATED_MAX_AGE_SECS - 1
    source._REVALIDATORS[url] = source._Revalidator('"i"', '"c"', '"s"', "a" * 40, now, read_at=aged)
    probes = _FakeProbes(source._ConditionalRead(304, '"i"'), source._ConditionalRead(304, '"c"'))
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    fetch = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40, "marker": "fresh"})
    monkeypatch.setattr(source, "_fetch_github", fetch)
    stale = now - source._CACHE_TTL_SECS - 5
    source._CACHE[url] = (stale, 10, {"state": "OPEN", "headSha": "a" * 40, "marker": "cached"})
    try:
        assert (await source.fetch_pull_request(url)).get("marker") == "fresh"
        fetch.assert_awaited_once()
        assert probes.sent == {}
        assert url not in source._REVALIDATORS
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_all_304_carries_read_at_forward_and_a_full_read_resets_it(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/31"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    now = source.time.monotonic()
    source._REVALIDATORS[url] = source._Revalidator('"i"', '"c"', '"s"', "a" * 40, now, read_at=now - 60)
    monkeypatch.setattr(
        source,
        "_gh_conditional_get",
        _FakeProbes(source._ConditionalRead(304, '"i"'), source._ConditionalRead(304, '"c"')),
    )
    stale = now - source._CACHE_TTL_SECS - 5
    cached = {"state": "OPEN", "headSha": "a" * 40, "marker": "cached"}
    source._CACHE[url] = (stale, 10, cached)
    fetch = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40, "marker": "fresh"})
    monkeypatch.setattr(source, "_fetch_github", fetch)
    try:
        assert await source.fetch_pull_request(url) is cached
        assert source._REVALIDATORS[url].read_at == now - 60

        monkeypatch.setattr(
            source,
            "_gh_conditional_get",
            _FakeProbes(source._ConditionalRead(200, 'W/"i2"'), source._ConditionalRead(304, '"c"')),
        )
        source._CACHE[url] = (stale, 10, cached)
        assert (await source.fetch_pull_request(url)).get("marker") == "fresh"
        read_at = source._REVALIDATORS[url].read_at
        assert read_at is not None and read_at >= now
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_expired_open_payload_is_served_when_probes_answer_304(monkeypatch) -> None:
    """The user-visible effect: an idle open PR past its TTL costs two probes
    and no fanout, and its entry is re-stamped fresh."""
    url = "https://github.com/acme/repo/pull/50"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    fanout = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40, "marker": "fresh"})
    monkeypatch.setattr(source, "_fetch_github", fanout)
    monkeypatch.setattr(
        source,
        "_gh_conditional_get",
        _FakeProbes(
            source._ConditionalRead(304, '"i"'), source._ConditionalRead(304, '"c"')
        ),
    )
    source._REVALIDATORS[url] = source._Revalidator('"i"', '"c"', '"s"', "a" * 40, 1.0)
    stale_at = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    cached = {"state": "OPEN", "headSha": "a" * 40, "marker": "cached"}
    source._CACHE[url] = (stale_at, 10, cached)
    try:
        assert await source.fetch_pull_request(url) is cached
        fanout.assert_not_awaited()
        stored_at, size, payload = source._CACHE[url]
        assert stored_at > stale_at and size == 10 and payload is cached
        # ...and a second read inside the TTL is a plain hit: no probes either.
        monkeypatch.setattr(source, "_gh_conditional_get", AsyncMock(side_effect=AssertionError))
        assert await source.fetch_pull_request(url) is cached
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_expired_open_payload_falls_through_to_the_fanout_on_a_change(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/51"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    fanout = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40, "marker": "fresh"})
    monkeypatch.setattr(source, "_fetch_github", fanout)
    monkeypatch.setattr(
        source,
        "_gh_conditional_get",
        _FakeProbes(
            source._ConditionalRead(200, 'W/"i2"'), source._ConditionalRead(304, '"c"')
        ),
    )
    source._REVALIDATORS[url] = source._Revalidator('"i"', '"c"', '"s"', "a" * 40, 1.0)
    stale_at = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    source._CACHE[url] = (stale_at, 10, {"state": "OPEN", "headSha": "a" * 40, "marker": "cached"})
    try:
        result = await source.fetch_pull_request(url)
        assert result["marker"] == "fresh"
        fanout.assert_awaited_once()
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_refresh_and_cold_reads_never_probe(monkeypatch) -> None:
    """An explicit refresh reads in full, and a cache miss has nothing to
    revalidate -- neither spends a probe."""
    url = "https://github.com/acme/repo/pull/52"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    fanout = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40})
    monkeypatch.setattr(source, "_fetch_github", fanout)
    monkeypatch.setattr(source, "_gh_conditional_get", AsyncMock(side_effect=AssertionError))
    try:
        await source.fetch_pull_request(url)  # cold
        source._CACHE[url] = (
            source.time.monotonic() - source._CACHE_TTL_SECS - 5,
            10,
            {"state": "OPEN", "headSha": "a" * 40},
        )
        await source.fetch_pull_request(url, refresh=True)
        assert fanout.await_count == 2
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_revalidation_does_not_restamp_an_entry_a_mutation_dropped(monkeypatch) -> None:
    """Generation guard: a mutation landing while the probes were in flight has
    already dropped the entry; the confirmed payload is still returned, but the
    pre-mutation entry must not be written back."""
    url = "https://github.com/acme/repo/pull/53"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    ref = source.parse_source_url(url)
    cached_entry = (0.0, 10, {"state": "OPEN", "headSha": "a" * 40})

    async def probes_then_mutation(*_args, **_kwargs):
        source._FULL_FETCH_GENERATIONS[url] = 7
        source._CACHE.pop(url, None)
        return source._ConditionalRead(304, '"x"')

    monkeypatch.setattr(source, "_gh_conditional_get", probes_then_mutation)
    source._REVALIDATORS[url] = source._Revalidator('"i"', '"c"', '"s"', "a" * 40, 1.0)
    source._CACHE[url] = cached_entry
    try:
        result = await source._revalidate_pull_request(ref, 0, cached_entry)
        assert result is cached_entry[2]
        assert url not in source._CACHE
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._FULL_FETCH_GENERATIONS.pop(url, None)


@pytest.mark.asyncio
async def test_run_json_kills_process_tree_when_stdout_exceeds_limit(monkeypatch) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.pid = 4242
            self.stdout = source.asyncio.StreamReader()
            self.stderr = source.asyncio.StreamReader()
            self.stdout.feed_data(b"12345")
            self.stderr.feed_eof()
            self.returncode = None
            self.killed = False
            self.done = source.asyncio.Event()

        async def wait(self):
            await self.done.wait()
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.done.set()

        async def communicate(self):
            # The bounded reap drains the pipes via communicate() rather than a
            # bare wait() that a full pipe could hang.
            self.returncode = -9
            self.done.set()
            return b"", b""

    proc = FakeProcess()
    spawn_kwargs = {}

    async def fake_create(*_args, **kwargs):
        spawn_kwargs.update(kwargs)
        return proc

    tree_kills: list[tuple[int, int]] = []

    async def kill_tree(pid, sig):
        tree_kills.append((pid, sig))
        proc.returncode = -sig
        proc.done.set()
        return True

    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(source.platform_compat, "kill_process_tree_async", kill_tree)
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(source.SourceProviderError, match="response was too large"):
        await source._run_json("gh", "api", "repos/acme/repo", max_output_bytes=4)
    # The whole tree is SIGKILLed through the bounded reap (kill_and_reap).
    assert tree_kills == [(proc.pid, source.platform_compat.SIGKILL)]
    assert spawn_kwargs["env"]["GH_HOST"] == "github.com"
    assert spawn_kwargs["start_new_session"] is source.platform_compat.IS_POSIX
    assert spawn_kwargs["creationflags"] == source.platform_compat.CREATE_NEW_PROCESS_GROUP


@pytest.mark.asyncio
async def test_run_json_on_windows_defers_to_the_sandbox_gate(monkeypatch) -> None:
    """Windows is no longer refused by a platform check of its own.

    It has no OS sandbox backend, but neither does a backend-less Linux host, and
    both must reach the same gate: ``sandboxed_spawn_argv`` fail-closes unless the
    operator opted into unsandboxed exec, and its refusal names that opt-in. The
    old blanket check ran BEFORE that gate, so it made the documented escape
    hatch unreachable on Windows alone and left the Changes panel permanently
    dead there. Asserting the resolver is now REACHED is what pins that: it sat
    behind the removed refusal, so a reintroduced platform check fails here.
    """
    resolver = MagicMock(return_value="C:\\gh\\gh.exe")
    sandbox = MagicMock(side_effect=RuntimeError("no OS-level sandbox backend"))
    spawn = AsyncMock()
    monkeypatch.setattr(source.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(source, "_resolve_provider_executable", resolver)
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(source.SourceProviderError, match="could not start securely"):
        await source._run_json("gh", "api", "repos/acme/repo")

    resolver.assert_called_once()
    sandbox.assert_called_once()
    # The sandbox refused, so nothing was ever executed unisolated.
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_json_on_windows_proceeds_once_the_sandbox_gate_allows(monkeypatch) -> None:
    """With the opt-in in force the gate returns argv and the read completes.

    The companion to the test above: together they show Windows now has BOTH
    outcomes the gate defines, rather than one hard-coded refusal.

    **The resolved path is POSIX-shaped on purpose — do not "correct" it to a
    Windows one.** Only ``IS_WINDOWS`` is patched here; CI runs this on a POSIX
    host where ``os.sep`` and ``shutil.which`` are real. A ``C:\\...`` value
    would take ``create_subprocess_limited``'s PATH-search branch (the spawn shim
    is non-empty on POSIX), ``shutil.which`` would return None, and the read
    would die with ``gh could not start`` before reaching the mocked spawn — the
    test would fail deterministically on CI while passing on a Windows dev box.
    What this test pins is the sandbox gate, not path resolution, so it uses the
    same absolute POSIX path every sibling test does.
    """

    class FakeProcess:
        returncode = 0

    monkeypatch.setattr(source.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        source, "_resolve_provider_executable", MagicMock(return_value="/usr/bin/gh")
    )
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))

    assert await source._run_json("gh", "api", "repos/acme/repo") == {}


@pytest.mark.asyncio
async def test_run_json_resolves_the_provider_cli_off_the_event_loop(monkeypatch) -> None:
    """Resolution stats every candidate and the whole parent chain of each hit,
    and the sidebar chip refresh reaches it on a timer with no user present. On
    the loop thread a slow filesystem freezes every task until the loop watchdog
    kills the gateway, so the walk has to happen on a worker thread."""

    class FakeProcess:
        returncode = 0

    resolver_threads: list[int] = []

    def recording_resolver(_name: str) -> str:
        resolver_threads.append(threading.get_ident())
        return "/usr/bin/gh"

    monkeypatch.setattr(source, "_resolve_provider_executable", recording_resolver)
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))

    assert await source._run_json("gh", "api", "repos/acme/repo") == {}

    assert len(resolver_threads) == 1
    assert resolver_threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_run_json_sandboxes_with_minimal_provider_environment(monkeypatch) -> None:
    class FakeProcess:
        returncode = 0

    # An absolute launcher path, as sandboxed_spawn_argv really returns: the
    # spawn shim execs without a PATH search, so a bare name here would not
    # describe anything the chokepoint actually produces.
    sandbox = MagicMock(
        return_value=(["/usr/bin/sandbox-launcher", "/usr/bin/gh", "api"], {"SAFE": "1"}, None)
    )
    spawn = AsyncMock(return_value=FakeProcess())
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "a" * 36)
    monkeypatch.setenv("PATH", "/workspace/attacker-bin")
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))

    assert await source._run_json("gh", "api", "repos/acme/repo") == {}

    base_env = sandbox.call_args.kwargs["env"]
    assert sandbox.call_args.args[0] == ["/usr/bin/gh", "api", "repos/acme/repo"]
    assert sandbox.call_args.kwargs["mode"] == "standard"
    assert base_env["GH_TOKEN"].startswith("ghp_")
    assert base_env["GH_HOST"] == "github.com"
    assert "SLACK_BOT_TOKEN" not in base_env
    assert "AWS_ACCESS_KEY_ID" not in base_env
    assert base_env["PATH"] == source._PROVIDER_SYSTEM_PATH
    assert "/workspace/attacker-bin" not in base_env["PATH"]
    assert spawn.call_args.kwargs["env"] == {"SAFE": "1"}
    # The resource ceiling is delivered AFTER exec by the spawn shim, never by a
    # preexec_fn: one would fork this threaded gateway and run Python in the
    # child, where a wedge blocks the event loop and pins the inherited fds.
    assert spawn.call_args.kwargs["preexec_fn"] is None
    shim = spawn_shim_argv()
    assert shim, "POSIX hosts must have a shim available for this assertion"
    assert spawn.call_args.args[: len(shim)] == shim
    assert spawn.call_args.args[len(shim) :] == (
        "/usr/bin/sandbox-launcher",
        "/usr/bin/gh",
        "api",
    )


@pytest.mark.asyncio
async def test_run_json_globally_bounds_provider_processes(monkeypatch) -> None:
    class FakeProcess:
        returncode = 0

    active = 0
    peak = 0

    async def collect(_proc, _executable, max_output_bytes):
        nonlocal active, peak
        assert max_output_bytes == source._METADATA_OUTPUT_BYTES
        active += 1
        peak = max(peak, active)
        await source.asyncio.sleep(0.01)
        active -= 1
        return b"{}", b""

    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", collect)

    await source.asyncio.gather(
        *(source._run_json("gh", "api", f"repos/acme/repo/{i}") for i in range(10))
    )

    assert peak <= source._PROVIDER_CONCURRENCY


def _prepare_audited_provider_run(monkeypatch, collect) -> None:
    class FakeProcess:
        returncode = 0

    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", collect)


@pytest.mark.asyncio
async def test_run_json_audits_success_without_sensitive_values(
    monkeypatch, _mock_source_sel
) -> None:
    secret = "ghp_" + "a" * 36
    raw_url = "https://github.com/acme/private/pull/12"
    collect = AsyncMock(return_value=(b"{}", b""))
    _prepare_audited_provider_run(monkeypatch, collect)
    monkeypatch.setenv("GH_TOKEN", secret)

    assert await source._run_json("gh", "pr", "view", raw_url) == {}

    calls = _mock_source_sel.log_tool_invocation.call_args_list
    assert [call.kwargs["outcome"] for call in calls] == ["invoked", "completed"]
    assert calls[0].kwargs["critical"] is True
    serialized = str(calls)
    assert raw_url not in serialized
    assert secret not in serialized
    assert "pr view" not in serialized


@pytest.mark.asyncio
async def test_run_json_awaits_critical_audit_off_loop_before_spawn(
    monkeypatch, _mock_source_sel
) -> None:
    audit_started = source.asyncio.Event()
    release_audit = source.asyncio.Event()
    order: list[str] = []

    async def fake_to_thread(func, *args, **kwargs):
        # Only the audit offload is under test; every other offload on this path
        # (executable resolution) has to pass through with its real result.
        if func is not source._audit_provider_cli:
            return func(*args, **kwargs)
        order.append("audit-started")
        audit_started.set()
        await release_audit.wait()
        result = func(*args, **kwargs)
        order.append("audit-completed")
        return result

    class FakeProcess:
        returncode = 0

    async def fake_spawn(*_args, **_kwargs):
        order.append("spawned")
        return FakeProcess()

    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(source.asyncio, "to_thread", fake_to_thread)
    spawn = AsyncMock(side_effect=fake_spawn)
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        source,
        "_collect_process_output",
        AsyncMock(return_value=(b"{}", b"")),
    )

    task = source.asyncio.create_task(source._run_json("gh", "api", "repos/acme/private"))
    await audit_started.wait()
    spawn.assert_not_awaited()

    release_audit.set()
    assert await task == {}
    assert order == ["audit-started", "audit-completed", "spawned"]
    call = _mock_source_sel.log_tool_invocation.call_args_list[0]
    assert call.kwargs["outcome"] == "invoked"
    assert call.kwargs["critical"] is True


@pytest.mark.asyncio
async def test_run_json_cancellation_reconciles_inflight_critical_audit_before_return(
    monkeypatch,
) -> None:
    audit_started = threading.Event()
    release_audit = threading.Event()
    events: list[tuple[str, str, bool]] = []

    def blocking_audit(
        _executable: str,
        outcome: str,
        reason: str,
        *,
        critical: bool = False,
    ) -> None:
        if outcome == "invoked":
            audit_started.set()
            assert release_audit.wait(timeout=2)
        events.append((outcome, reason, critical))

    class FakeProcess:
        returncode = 0

    spawn = AsyncMock(return_value=FakeProcess())
    monkeypatch.setattr(source, "_audit_provider_cli", blocking_audit)
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", spawn)

    task = source.asyncio.create_task(source._run_json("gh", "api", "repos/acme/private"))
    for _ in range(100):
        if audit_started.is_set():
            break
        await source.asyncio.sleep(0.01)
    assert audit_started.is_set()

    task.cancel()
    await source.asyncio.sleep(0)
    assert not task.done()
    spawn.assert_not_awaited()

    release_audit.set()
    with pytest.raises(source.asyncio.CancelledError):
        await task

    spawn.assert_not_awaited()
    assert events == [
        ("invoked", "dispatch", True),
        ("failed", "request_cancelled", False),
    ]


@pytest.mark.asyncio
async def test_run_json_audits_denial_without_rejected_argv(
    _mock_source_sel,
) -> None:
    secret = "ghp_" + "b" * 36

    with pytest.raises(source.SourceProviderError, match="unsupported provider command"):
        await source._run_json("sh", "-c", f"echo {secret}")

    call = _mock_source_sel.log_tool_invocation.call_args
    assert call.kwargs["outcome"] == "denied"
    assert call.kwargs["error"] == "unsupported_provider"
    assert secret not in str(call)
    assert "echo" not in str(call)


@pytest.mark.asyncio
async def test_run_json_audits_spawn_failure_without_exception_text(
    monkeypatch, _mock_source_sel
) -> None:
    secret = "ghp_" + "c" * 36
    _prepare_audited_provider_run(monkeypatch, AsyncMock())
    monkeypatch.setattr(
        source.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=OSError(f"spawn failed {secret}")),
    )

    with pytest.raises(source.SourceProviderError, match="could not start"):
        await source._run_json("gh", "api", "repos/acme/private")

    calls = _mock_source_sel.log_tool_invocation.call_args_list
    assert [call.kwargs["outcome"] for call in calls] == ["invoked", "failed"]
    assert calls[-1].kwargs["error"] == "provider_error"
    assert secret not in str(calls)


@pytest.mark.asyncio
async def test_run_json_audits_cancellation_and_reraises(monkeypatch, _mock_source_sel) -> None:
    collect = AsyncMock(side_effect=source.asyncio.CancelledError())
    _prepare_audited_provider_run(monkeypatch, collect)

    with pytest.raises(source.asyncio.CancelledError):
        await source._run_json("gh", "api", "repos/acme/private")

    calls = _mock_source_sel.log_tool_invocation.call_args_list
    assert [call.kwargs["outcome"] for call in calls] == ["invoked", "failed"]
    assert calls[-1].kwargs["error"] == "request_cancelled"


@pytest.mark.asyncio
async def test_run_json_denies_spawn_when_critical_audit_is_unavailable(
    monkeypatch, _mock_source_sel
) -> None:
    spawn = AsyncMock()
    _prepare_audited_provider_run(monkeypatch, AsyncMock())
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", spawn)
    _mock_source_sel.log_tool_invocation.side_effect = OSError("audit filesystem unavailable")

    with pytest.raises(source.SourceProviderError, match="provider audit unavailable"):
        await source._run_json("gh", "api", "repos/acme/private")

    spawn.assert_not_awaited()
    call = _mock_source_sel.log_tool_invocation.call_args
    assert call.kwargs["outcome"] == "invoked"
    assert call.kwargs["critical"] is True


@pytest.mark.asyncio
async def test_run_json_rejects_non_provider_executable() -> None:
    with pytest.raises(source.SourceProviderError, match="unsupported provider command"):
        await source._run_json("sh", "-c", "echo unsafe")


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}, ("mergeable", "clean")),
        ({"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}, ("conflicting", "dirty")),
        ({"mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND"}, ("mergeable", "behind")),
        ({"mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED"}, ("mergeable", "blocked")),
        ({"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}, ("unknown", "unknown")),
        ({}, ("", "")),
    ],
)
def test_github_merge_state_normalization(details: dict, expected: tuple[str, str]) -> None:
    assert source._github_merge_state(details) == expected


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"detailed_merge_status": "mergeable"}, ("mergeable", "clean")),
        ({"detailed_merge_status": "conflict"}, ("conflicting", "dirty")),
        ({"detailed_merge_status": "need_rebase"}, ("unknown", "need_rebase")),
        ({"detailed_merge_status": "not_approved"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "ci_must_pass"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "status_checks_must_pass"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "policies_denied"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "security_policy_violations"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "merge_request_blocked"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "ci_still_running"}, ("unknown", "unstable")),
        ({"detailed_merge_status": "draft_status"}, ("unknown", "draft")),
        ({"detailed_merge_status": "checking"}, ("unknown", "unknown")),
        # Legacy merge_status is a fallback only when the detail is absent.
        ({"merge_status": "cannot_be_merged"}, ("conflicting", "")),
        ({"merge_status": "can_be_merged"}, ("mergeable", "")),
        # A stale legacy value must never override the authoritative detail:
        # not_approved + cannot_be_merged is blocked, NOT conflicting.
        (
            {"detailed_merge_status": "not_approved", "merge_status": "cannot_be_merged"},
            ("unknown", "blocked"),
        ),
        (
            {"detailed_merge_status": "mergeable", "merge_status": "cannot_be_merged"},
            ("mergeable", "clean"),
        ),
        (
            {"detailed_merge_status": "conflict", "merge_status": "can_be_merged"},
            ("conflicting", "dirty"),
        ),
        ({}, ("", "")),
    ],
)
def test_gitlab_merge_state_normalization(details: dict, expected: tuple[str, str]) -> None:
    assert source._gitlab_merge_state(details) == expected


def test_parse_gitlab_merge_request_with_nested_group() -> None:
    ref = source.parse_source_url("https://gitlab.com/acme/platform/service/-/merge_requests/42")
    assert ref.provider == "gitlab"
    assert ref.project == "acme/platform/service"
    assert ref.repo == "service"
    assert ref.number == 42


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/org/repo/pull/1",
        "https://evil.example/github.com/org/repo/pull/1",
        "https://github.com.evil.example/org/repo/pull/1",
        "https://user@github.com/org/repo/pull/1",
        "https://gitlab.com/group/project/issues/1",
    ],
)
def test_parse_source_url_rejects_untrusted_shapes(url: str) -> None:
    with pytest.raises(ValueError):
        source.parse_source_url(url)


@pytest.mark.asyncio
async def test_fetch_github_normalizes_commits_checks_comments_and_files(monkeypatch) -> None:
    limits: dict[str, int | None] = {}

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        limits[command] = kwargs.get("max_output_bytes")
        if "pr view" in command:
            return {
                "number": 12,
                "title": "Ship source tabs",
                "body": "## Summary\nAdds source tabs.",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "CONFLICTING",
                "mergeStateStatus": "DIRTY",
                "headRefName": "feature/source-tabs",
                "baseRefName": "main",
                "headRefOid": "abc123",
                "url": "https://github.com/acme/repo/pull/12",
                "author": {"login": "octocat"},
                "additions": 20,
                "deletions": 4,
                "changedFiles": 2,
                "commits": [
                    {
                        "oid": "abc123",
                        "messageHeadline": "Add source tabs",
                        "messageBody": "",
                        "authors": [{"login": "octocat"}],
                        "committedDate": "2026-07-13T12:00:00Z",
                    }
                ],
                "comments": [{"id": "c1", "author": {"login": "reviewer"}, "body": "Looks good"}],
                "reviews": [
                    {
                        "id": "r1",
                        "author": {"login": "reviewer"},
                        "body": "Approved",
                        "state": "APPROVED",
                    }
                ],
                "statusCheckRollup": [
                    {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}
                ],
            }
        if "/files?" in command:
            return [
                {
                    "filename": "src/panel.tsx",
                    "status": "modified",
                    "additions": 20,
                    "deletions": 4,
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ]
        if "/comments?" in command:
            return [
                {
                    "id": 3,
                    "user": {"login": "inline-reviewer"},
                    "body": "Nit",
                    "path": "src/panel.tsx",
                    "line": 9,
                }
            ]
        if "graphql" in command:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "PRRT_thread1",
                                        "isResolved": False,
                                        "comments": {"nodes": [{"databaseId": 3}]},
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["provider"] == "github"
    # The plugin contract (`SourceChangePayload`) is defined as "what the
    # built-in fetchers produce", so the two must not drift: a key added to or
    # removed from `_fetch_github` without the schema (or vice versa) breaks
    # every downstream plugin silently. Exact equality, both directions (the
    # GitHub fetcher emits none of the `total=False` extras).
    assert set(data) == set(source.SourceChangePayload.__required_keys__)
    assert set(data["commits"][0]) == set(source.SourceChangeCommit.__annotations__)
    assert set(data["files"][0]) == set(source.SourceChangeFile.__annotations__)
    assert {frozenset(comment) for comment in data["comments"]} == {
        frozenset(source.SourceChangeComment.__annotations__)
    }
    assert data["mergeable"] == "conflicting"
    assert data["mergeStateStatus"] == "dirty"
    assert data["commits"][0]["sha"] == "abc123"
    assert data["checks"][0]["bucket"] == "passed"
    assert {comment["kind"] for comment in data["comments"]} == {"comment", "review", "inline"}
    assert data["files"][0]["patch"].startswith("@@")
    assert data["partialSections"] == ["files"]

    inline = next(comment for comment in data["comments"] if comment["kind"] == "inline")
    assert inline["threadId"] == "PRRT_thread1"
    assert inline["resolvable"] is True
    assert inline["resolved"] is False
    top_level = next(comment for comment in data["comments"] if comment["kind"] == "comment")
    assert top_level["threadId"] == ""
    assert top_level["resolvable"] is False
    assert next(limit for command, limit in limits.items() if "pr view" in command) is None
    assert (
        next(limit for command, limit in limits.items() if "/files?" in command)
        == source._DIFF_OUTPUT_BYTES
    )
    assert (
        next(limit for command, limit in limits.items() if "/comments?" in command)
        == source._DISCUSSION_OUTPUT_BYTES
    )
    assert (
        next(limit for command, limit in limits.items() if "graphql" in command)
        == source._DISCUSSION_OUTPUT_BYTES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_endpoint", "expected_section"),
    [
        ("files", "files"),
        ("comments", "inline review comments"),
        ("threads", "inline review comments"),
    ],
)
async def test_fetch_github_marks_failed_secondary_endpoints_partial(
    monkeypatch, failed_endpoint: str, expected_section: str
) -> None:
    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "pr view" in command:
            return {"number": 12, "changedFiles": 0}
        should_fail = (
            (failed_endpoint == "files" and "/files?" in command)
            or (failed_endpoint == "comments" and "/comments?" in command)
            or (failed_endpoint == "threads" and "graphql" in command)
        )
        if should_fail:
            raise source.SourceProviderError("secondary request failed")
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["partialSections"] == [expected_section]


@pytest.mark.asyncio
async def test_fetch_github_reads_rollup_outside_the_core_field_set(monkeypatch) -> None:
    """The core `pr view` field set must not bundle `statusCheckRollup` (#5115).

    `gh` resolves a `--json` field set atomically, so a bundled rollup made a
    fine-grained token without Checks read access fail the WHOLE panel read.
    """
    commands: list[tuple[str, int | None]] = []

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        commands.append((command, kwargs.get("max_output_bytes")))
        if "statusCheckRollup" in command:
            return {
                "statusCheckRollup": [
                    {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}
                ],
                "headRefOid": "abc123",
            }
        if "pr view" in command:
            return {"number": 12, "title": "Split", "state": "OPEN", "headRefOid": "abc123"}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    core_reads = [
        command for command, _limit in commands if "pr view" in command and "title" in command
    ]
    assert core_reads
    assert all("statusCheckRollup" not in command for command in core_reads)
    rollup_reads = [
        (command, limit) for command, limit in commands if "statusCheckRollup" in command
    ]
    assert len(rollup_reads) == 1
    assert rollup_reads[0][0].endswith("statusCheckRollup,headRefOid")
    assert rollup_reads[0][1] == source._CHECKS_OUTPUT_BYTES
    assert data["checks"][0]["bucket"] == "passed"
    assert "checks" not in data["partialSections"]


@pytest.mark.asyncio
async def test_fetch_github_core_payload_survives_rollup_failure(monkeypatch) -> None:
    """A Checks-blind token costs the checks SECTION, never the panel (#5115)."""

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            raise source.SourceProviderError("gh: Resource not accessible by integration")
        if "pr view" in command:
            return {
                "number": 12,
                "title": "Fine-grained token",
                "state": "OPEN",
                "headRefOid": "abc123",
            }
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["title"] == "Fine-grained token"
    assert data["state"] == "OPEN"
    assert data["checks"] == []
    # The degraded state is distinguishable IN THE PAYLOAD: an empty `checks`
    # list plus the named partial section, never a silent "no checks".
    assert "checks" in data["partialSections"]


@pytest.mark.asyncio
async def test_fetch_github_discards_rollup_from_a_different_head(monkeypatch) -> None:
    """A rollup read that straddled a push must not pin another commit's CI."""

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            return {
                "statusCheckRollup": [
                    {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}
                ],
                "headRefOid": "pushed-after-core-read",
            }
        if "pr view" in command:
            return {"number": 12, "state": "OPEN", "headRefOid": "abc123"}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["checks"] == []
    assert "checks" in data["partialSections"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_endpoint", "expected_section"),
    [
        ("commits", "commits"),
        ("discussions", "review discussions"),
        ("changes", "files"),
        ("pipelines", "checks"),
        ("jobs", "checks"),
    ],
)
async def test_fetch_gitlab_marks_failed_secondary_endpoints_partial(
    monkeypatch, failed_endpoint: str, expected_section: str
) -> None:
    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            return {"iid": 42, "changes_count": "0"}
        should_fail = (
            (failed_endpoint == "commits" and "/commits?" in command)
            or (failed_endpoint == "discussions" and "/discussions?" in command)
            or (failed_endpoint == "changes" and command.endswith("/changes"))
            or (failed_endpoint == "pipelines" and "/pipelines?" in command)
            or (failed_endpoint == "jobs" and "/jobs?" in command)
        )
        if should_fail:
            raise source.SourceProviderError("secondary request failed")
        if "/pipelines?" in command:
            return [{"id": 91}] if failed_endpoint == "jobs" else []
        if command.endswith("/changes"):
            return {"changes": []}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    assert data["partialSections"] == [expected_section]


MERGE_STATE_REREAD_FIELDS = "mergeable,mergeStateStatus"


@pytest.mark.asyncio
async def test_fetch_github_rereads_merge_state_until_the_provider_settles_it(
    monkeypatch,
) -> None:
    """GitHub computes mergeability lazily: the first read says UNKNOWN.

    Without the re-read the panel reports no merge blocker at all on first open,
    and the conflict only surfaces once the user hits refresh.
    """
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    rereads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if MERGE_STATE_REREAD_FIELDS in command:
            rereads.append(command)
            if len(rereads) == 1:
                return {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}
            return {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
        if "pr view" in command:
            return {"number": 12, "mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert (data["mergeable"], data["mergeStateStatus"]) == ("conflicting", "dirty")
    # The re-read asks for the merge fields alone, not another full fanout.
    assert len(rereads) == 2
    assert all("statusCheckRollup" not in command for command in rereads)


@pytest.mark.asyncio
async def test_fetch_github_does_not_reread_settled_merge_state(monkeypatch) -> None:
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    rereads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if MERGE_STATE_REREAD_FIELDS in command:
            rereads.append(command)
            return {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
        if "pr view" in command:
            return {"number": 12, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert (data["mergeable"], data["mergeStateStatus"]) == ("mergeable", "clean")
    assert rereads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unsettled", "provider_error", "invalid_payload"])
async def test_fetch_github_degrades_to_unknown_when_reread_cannot_settle(
    monkeypatch, outcome: str
) -> None:
    """A merge state that stays unknown degrades one banner, never the panel."""
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    rereads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if MERGE_STATE_REREAD_FIELDS in command:
            rereads.append(command)
            if outcome == "provider_error":
                raise source.SourceProviderError("merge state read failed")
            if outcome == "invalid_payload":
                return []
            return {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}
        if "pr view" in command:
            return {"number": 12, "title": "Still checking", "mergeable": "UNKNOWN"}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["mergeable"] == "unknown"
    assert data["title"] == "Still checking"
    # A failed or invalid re-read stops immediately; an unsettled one uses the
    # whole bounded budget and no more.
    assert len(rereads) == (source._MERGE_STATE_REREADS if outcome == "unsettled" else 1)


@pytest.mark.asyncio
async def test_fetch_github_skips_reread_when_provider_omits_merge_fields(monkeypatch) -> None:
    """An absent field is not "still computing" — re-reading it would never settle."""
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    rereads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if MERGE_STATE_REREAD_FIELDS in command:
            rereads.append(command)
            return {}
        if "pr view" in command:
            return {"number": 12}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["mergeable"] == ""
    assert rereads == []


@pytest.mark.asyncio
async def test_fetch_gitlab_rereads_merge_state_until_the_provider_settles_it(
    monkeypatch,
) -> None:
    """GitLab reports ``checking``/``unchecked`` while it evaluates the MR."""
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    detail_reads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            detail_reads.append(command)
            if len(detail_reads) == 1:
                return {"iid": 42, "detailed_merge_status": "checking"}
            return {"iid": 42, "detailed_merge_status": "conflict"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    assert (data["mergeable"], data["mergeStateStatus"]) == ("conflicting", "dirty")
    assert len(detail_reads) == 2


@pytest.mark.asyncio
async def test_fetch_gitlab_degrades_to_unknown_when_reread_cannot_settle(monkeypatch) -> None:
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    detail_reads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            detail_reads.append(command)
            return {"iid": 42, "title": "Still checking", "detailed_merge_status": "unchecked"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    assert data["mergeable"] == "unknown"
    assert data["title"] == "Still checking"
    assert len(detail_reads) == 1 + source._MERGE_STATE_REREADS


@pytest.mark.asyncio
async def test_fetch_github_checks_collapses_superseded_runs(monkeypatch) -> None:
    """The panel polls this endpoint while checks are pending and writes its
    reply over the full payload's `checks`, so an uncollapsed reply would
    resurrect the inflated counts and the superseded failure it just fixed."""

    async def fake_run(*_argv: str, **_kwargs: int):
        return {
            "statusCheckRollup": [
                {
                    "name": "Review",
                    "workflowName": "Review",
                    "status": "COMPLETED",
                    "conclusion": "CANCELLED",
                    "startedAt": "2026-07-28T20:56:29Z",
                },
                {
                    "name": "Review",
                    "workflowName": "Review",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "startedAt": "2026-07-28T20:58:05Z",
                },
            ]
        }

    monkeypatch.setattr(source, "_run_json", fake_run)

    checks = await source._fetch_github_checks(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert [check["bucket"] for check in checks] == ["passed"]


@pytest.mark.asyncio
async def test_github_check_status_collapses_superseded_runs(monkeypatch) -> None:
    """The chip glyph reads the same latest-run-per-check collapse the panel
    does, so a superseded CANCELLED row cannot leave the sidebar red."""

    async def fake_run(*_argv: str, **_kwargs: int):
        return {
            "state": "OPEN",
            "statusCheckRollup": [
                {
                    "name": "Review",
                    "workflowName": "Review",
                    "status": "COMPLETED",
                    "conclusion": "CANCELLED",
                    "startedAt": "2026-07-28T20:56:29Z",
                },
                {
                    "name": "Review",
                    "workflowName": "Review",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "startedAt": "2026-07-28T20:58:05Z",
                },
            ],
        }

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://github.com/acme/repo/pull/12")

    assert status == {"state": "open", "ci": "passed"}


@pytest.mark.asyncio
async def test_github_check_status_carries_settled_merge_state(monkeypatch) -> None:
    """The chip cache carries merge state so a conflict that appears while the
    panel is open lands on a poll instead of waiting for a manual refresh."""

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            return {"statusCheckRollup": [], "headRefOid": "abc123"}
        assert "mergeable,mergeStateStatus" in command
        return {"state": "OPEN", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://github.com/acme/repo/pull/12")

    assert status == {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_mergeable", ["UNKNOWN", None])
async def test_github_check_status_omits_unsettled_merge_state(
    monkeypatch, raw_mergeable: str | None
) -> None:
    """"Still computing" must not overwrite the answer the full payload has."""

    async def fake_run(*_argv: str, **_kwargs: int):
        return {"state": "OPEN", "mergeable": raw_mergeable, "mergeStateStatus": "UNKNOWN"}

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://github.com/acme/repo/pull/12")

    assert status == {"state": "open"}


@pytest.mark.asyncio
async def test_github_check_status_keeps_authorized_fields_when_rollup_fails(
    monkeypatch,
) -> None:
    """The chip renders state/merge under a Checks-blind token (#5115).

    Bundling `statusCheckRollup` into the chip read made the WHOLE read fail
    when the token lacked Checks access; the split keeps the fields the token
    was authorized for and flags only the CI portion as unavailable.
    """

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            raise source.SourceProviderError("gh: Resource not accessible by integration")
        return {"state": "OPEN", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://github.com/acme/repo/pull/12")

    assert status is not None
    assert status.pop(source._CHIP_CI_UNAVAILABLE, None) == "1"
    assert status == {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"}


@pytest.mark.asyncio
async def test_github_check_status_marks_stale_rollup_unavailable(monkeypatch) -> None:
    """A rollup read that straddled a push must not paint another head's CI."""

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            return {
                "statusCheckRollup": [
                    {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}
                ],
                "headRefOid": "pushed-after-core-read",
            }
        return {"state": "OPEN", "headRefOid": "abc123"}

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://github.com/acme/repo/pull/12")

    assert status is not None
    assert status.pop(source._CHIP_CI_UNAVAILABLE, None) == "1"
    assert status == {"state": "open"}


@pytest.mark.asyncio
async def test_github_check_status_still_fails_when_core_read_fails(monkeypatch) -> None:
    """Only the rollup is degradable: a failed CORE read must keep raising, so
    `_refresh_check_status` keeps its keep-previous-wholesale posture."""

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            return {"statusCheckRollup": [], "headRefOid": "abc123"}
        raise source.SourceProviderError("core read failed")

    monkeypatch.setattr(source, "_run_json", fake_run)

    with pytest.raises(source.SourceProviderError):
        await source._fetch_check_status("https://github.com/acme/repo/pull/12")


@pytest.mark.asyncio
async def test_gitlab_check_status_carries_settled_merge_state(monkeypatch) -> None:
    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            return {"state": "opened", "detailed_merge_status": "conflict"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status(
        "https://gitlab.com/acme/repo/-/merge_requests/42"
    )

    assert status == {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"}


@pytest.mark.asyncio
async def test_fetch_gitlab_treats_a_detail_only_answer_as_settled(monkeypatch) -> None:
    """GitLab settles need_rebase with ``mergeable`` still ``unknown``.

    Keying settledness on ``mergeable`` alone re-read a state the provider had
    already answered, then threw the answer away.
    """
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    detail_reads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            detail_reads.append(command)
            return {"iid": 42, "detailed_merge_status": "need_rebase"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    assert (data["mergeable"], data["mergeStateStatus"]) == ("unknown", "need_rebase")
    assert len(detail_reads) == 1


@pytest.mark.parametrize(
    ("pair", "settled"),
    [
        (("conflicting", "dirty"), True),
        (("mergeable", "clean"), True),
        # GitLab: the detail is the answer, mergeable never settles.
        (("unknown", "need_rebase"), True),
        (("unknown", "blocked"), True),
        # Nothing answered yet -> a re-read may settle it.
        (("unknown", "unknown"), False),
        (("unknown", ""), False),
        # Provider omitted the fields -> re-reading cannot settle them.
        (("", ""), True),
    ],
)
def test_merge_state_settled_considers_both_fields(
    pair: tuple[str, str], settled: bool
) -> None:
    assert source._merge_state_settled(*pair) is settled


@pytest.mark.asyncio
async def test_gitlab_check_status_carries_a_detail_only_answer(monkeypatch) -> None:
    """The rebase/blocked banners are driven by the detail field alone.

    Dropping it because ``mergeable`` is unknown left exactly those banners
    invisible to the status poll.
    """

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            return {"state": "opened", "detailed_merge_status": "need_rebase"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status(
        "https://gitlab.com/acme/repo/-/merge_requests/42"
    )

    assert status == {"state": "open", "mergeStateStatus": "need_rebase"}


def test_status_from_full_payload_projects_the_merge_pair() -> None:
    """The write-through must carry the merge pair the chip read records.

    If it dropped the pair, every full fetch would rewrite the chip entry without
    it, the next chip refresh would judge that a change and drop the full payload,
    and the write-through would strip it again — the repeating chip↔full
    transition PR #443's flap damper exists to contain, spun by a projection gap.
    """
    projected = source.status_from_full_payload(
        {
            "state": "OPEN",
            "draft": False,
            "checks": [{"bucket": "passed"}],
            "mergeable": "conflicting",
            "mergeStateStatus": "dirty",
        }
    )

    assert projected == {
        "ci": "passed",
        "state": "open",
        "mergeable": "conflicting",
        "mergeStateStatus": "dirty",
    }


def test_full_payload_and_chip_projections_agree_on_the_merge_pair() -> None:
    """Both surfaces must derive the identical pair, or they flap against each other."""
    chip: dict[str, str] = {}
    source._record_merge_state(chip, "unknown", "need_rebase")
    projected = source.status_from_full_payload(
        {"state": "opened", "mergeable": "unknown", "mergeStateStatus": "need_rebase"}
    )

    assert chip == {"mergeStateStatus": "need_rebase"}
    assert projected is not None
    assert {
        key: value for key, value in projected.items() if key.startswith("merge")
    } == chip


@pytest.mark.asyncio
async def test_refresh_check_status_queues_broadcast_only_when_status_changes(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/12"
    source._check_cache.clear()
    source._check_inflight.clear()
    callback = MagicMock()
    queue_update = MagicMock()
    monkeypatch.setattr(source, "_queue_check_update", queue_update)
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"ci": "passed", "state": "open"}),
    )

    await source._refresh_check_status(url, callback)
    await source._refresh_check_status(url, callback)

    queue_update.assert_called_once_with(callback)
    assert source.get_cached_check_status(url) == {"ci": "passed", "state": "open"}
    source._check_cache.clear()


@pytest.mark.asyncio
async def test_check_update_broadcasts_are_coalesced(monkeypatch) -> None:
    callback = MagicMock()
    source._check_update_callbacks.clear()
    source._check_update_handle = None
    monkeypatch.setattr(source, "_CHECK_UPDATE_DEBOUNCE_SECS", 0)

    source._queue_check_update(callback)
    source._queue_check_update(callback)
    await source.asyncio.sleep(0.01)

    callback.assert_called_once_with()
    assert source._check_update_handle is None


@pytest.mark.asyncio
async def test_status_endpoint_warms_allowlist_before_parsing_self_hosted_urls(
    monkeypatch,
) -> None:
    """The status endpoint parses browser-supplied URLs, so it must warm the
    allowlist first: on a cold snapshot an authorized self-managed URL would be
    dropped as unsupported and never reach scheduling. Existing endpoint tests
    use only gitlab.com/github.com URLs, which never exercise this."""
    url = "https://gitlab.acme.internal/team/api/-/merge_requests/7"
    source._check_cache.clear()
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(source, "_gitlab_hosts_loaded_at", 0.0)

    async def fake_ensure() -> frozenset:
        source._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
        return frozenset({"gitlab.acme.internal"})

    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", fake_ensure)
    refresh = MagicMock(return_value=[url])
    monkeypatch.setattr(source, "schedule_check_refresh", refresh)

    app = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/source/pull-request/status", json={"urls": [url]})
        assert response.status == 200

    # The authorized self-managed URL survived validation and reached scheduling.
    assert refresh.call_args.args[0] == [url]


@pytest.mark.asyncio
async def test_status_endpoint_audits_cancellation_during_allowlist_warm_up(
    monkeypatch, _mock_source_sel
) -> None:
    """The status handler awaits ``ensure_gitlab_hosts_loaded()`` directly (the
    read/checks/resolve endpoints reach it through a helper already wrapped in a
    cancellation guard). A cancellation at that direct await must still emit a
    terminal audit: without the guard the handler unwinds past the ``completed``
    line and an authorized status attempt vanishes from the tamper-evident SEL
    chain. Driven without a TestClient because aiohttp's server turns a handler
    ``CancelledError`` into a connection abort and would mask the re-raise."""

    async def cancel_warm_up() -> "frozenset[str]":
        raise source.asyncio.CancelledError()

    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", cancel_warm_up)

    class _FakeRequest:
        method = "POST"

        def __init__(self) -> None:
            state = MagicMock()
            state.owner_id = "U_OWNER"
            self.app = {"state": state}
            self._claims = {"user": "U_OWNER", "app": ""}

        def get(self, key, default=None):
            return self._claims.get(key, default)

        def __getitem__(self, key):
            return self._claims[key]

        def __contains__(self, key):
            return key in self._claims

        async def json(self):
            return {"urls": ["https://github.com/acme/repo/pull/1"]}

    with pytest.raises(source.asyncio.CancelledError):
        await source.api_pull_request_status(_FakeRequest())

    # Revert-verified: dropping the try/except leaves the CancelledError
    # propagating but with no log_api_access call recorded at all (the happy
    # path emits nothing before ``completed``), so this assertion fails.
    call = _mock_source_sel.log_api_access.call_args
    assert call.kwargs["operation"] == "source.pull_request.status"
    assert call.kwargs["outcome"] == "failed"
    assert call.kwargs["error"] == "request_cancelled"


@pytest.mark.asyncio
async def test_status_endpoint_serves_cached_chip_status_and_kicks_refresh(monkeypatch) -> None:
    """The Changes-tab strip reads cached status for many URLs in one request."""
    known = "https://github.com/acme/repo/pull/12"
    unknown = "https://github.com/acme/repo/pull/13"
    source._check_cache.clear()
    source._check_cache[known] = (source.time.monotonic(), {"ci": "failed", "state": "open"})
    refresh = MagicMock(return_value=[unknown])
    monkeypatch.setattr(source, "schedule_check_refresh", refresh)

    app = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/source/pull-request/status",
            # A www. spelling and a duplicate both normalize to one entry; a
            # non-pull-request URL is dropped instead of failing the request.
            json={
                "urls": [
                    "https://www.github.com/acme/repo/pull/12",
                    known,
                    unknown,
                    "https://example.com/x",
                ]
            },
        )
        assert response.status == 200
        payload = await response.json()

    # Only URLs with a cached status are reported; the rest simply stay absent
    # until a background refresh lands.
    assert payload == {
        "statuses": {known: {"ci": "failed", "state": "open"}},
        # The scheduler's report is passed through verbatim so the client knows
        # to re-poll soon instead of waiting out the TTL, along with the TTL that
        # paces its steady state.
        "refreshing": [unknown],
        "ttlSecs": source.CHECK_STATUS_TTL_SECS,
    }
    assert refresh.call_args.args[0] == [known, unknown]
    source._check_cache.clear()


@pytest.mark.asyncio
async def test_schedule_check_refresh_reports_urls_expected_to_change(monkeypatch) -> None:
    """The status endpoint's ``refreshing`` hint comes from the scheduler itself."""
    fresh = "https://github.com/acme/repo/pull/1"
    stale = "https://github.com/acme/repo/pull/2"
    already = "https://github.com/acme/repo/pull/3"
    deferred = "https://github.com/acme/repo/pull/4"
    source._check_cache.clear()
    source._check_inflight.clear()
    monkeypatch.setattr(source, "_refresh_check_status", AsyncMock(return_value=None))
    now = source.time.monotonic()
    source._check_cache[fresh] = (now, {"state": "open"})
    source._check_cache[stale] = (now - source._CHECK_TTL_SECS - 1, {"state": "open"})
    source._check_inflight.add(already)
    monkeypatch.setattr(source, "_CHECK_PENDING_MAX", 2)

    refreshing = source.schedule_check_refresh([fresh, stale, already, deferred])

    # Fresh entries need nothing; a started and an already-in-flight refresh are
    # both "coming shortly"; the pending-cap deferral was backed off for a TTL,
    # so promising the client a fast update for it would be a lie.
    assert refreshing == [stale, already]
    assert source._check_cache[deferred][1] is None
    source._check_cache.clear()
    source._check_inflight.clear()


def test_status_from_full_payload_projects_lifecycle_and_ci() -> None:
    """The chip projection must speak exactly the chip vocabulary."""
    assert source.status_from_full_payload(
        {"state": "OPEN", "draft": False, "checks": [{"bucket": "passed"}]}
    ) == {"ci": "passed", "state": "open"}
    # A draft with a running check, GitHub spelling.
    assert source.status_from_full_payload(
        {"state": "OPEN", "draft": True, "checks": [{"bucket": "pending"}, {"bucket": "passed"}]}
    ) == {"ci": "running", "state": "draft"}
    # Any failure dominates the rollup.
    assert source.status_from_full_payload(
        {"state": "MERGED", "checks": [{"bucket": "failed"}, {"bucket": "pending"}]}
    ) == {"ci": "failed", "state": "merged"}
    # GitLab spellings.
    assert source.status_from_full_payload({"state": "opened", "checks": []}) == {"state": "open"}
    # `locked` is GitLab's transient mid-merge state — no terminal lifecycle, so
    # the projection returns nothing for it (both paths agree on "no change")
    # rather than painting a false "closed" glyph.
    assert source.status_from_full_payload({"state": "locked"}) is None
    # An MR closed while still in draft (a common way to abandon one): GitLab
    # keeps `draft: true`, but the lifecycle is closed. Draft only applies to an
    # OPEN MR, so this projects "closed" — and the chip path must agree, or the
    # mutual invalidation ping-pongs draft<->closed forever (Arbiter item 1).
    assert source.status_from_full_payload(
        {"state": "closed", "draft": True}
    ) == {"state": "closed"}
    # The generic bucket rollup (GitHub's statusCheckRollup, or any payload
    # without an authoritative `ciStatus`): all-skipped/passed buckets → "passed",
    # no "failed" and no "pending". GitLab instead carries `ciStatus` (see below).
    assert source.status_from_full_payload(
        {"state": "opened", "checks": [{"bucket": "skipped"}, {"bucket": "passed"}]}
    ) == {"ci": "passed", "state": "open"}
    assert source.status_from_full_payload(
        {"state": "opened", "checks": [{"bucket": "skipped"}]}
    ) == {"ci": "passed", "state": "open"}
    # GitLab's authoritative aggregate CI (`ciStatus`) is preferred over — and
    # never diverges from — its faithful per-job buckets: an allow_failure red
    # job buckets "failed" for display, but the aggregate the chip reads is
    # "passed", so `ciStatus` wins and the two caches agree.
    assert source.status_from_full_payload(
        {"state": "opened", "ciStatus": "passed", "checks": [{"bucket": "failed"}]}
    ) == {"ci": "passed", "state": "open"}
    # A blocking manual gate: aggregate is "running", even though the manual job
    # buckets "skipped".
    assert source.status_from_full_payload(
        {"state": "opened", "ciStatus": "running", "checks": [{"bucket": "skipped"}]}
    ) == {"ci": "running", "state": "open"}
    # Nothing knowable → no entry at all, rather than a misleading default.
    assert source.status_from_full_payload({"state": "", "checks": []}) is None
    assert source.status_from_full_payload("nope") is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_full_fetch_writes_through_to_chip_cache(monkeypatch) -> None:
    """One provider read feeds both surfaces, so they cannot disagree."""
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._check_cache.clear()
    source._status_delta_sinks.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    monkeypatch.setattr(
        source,
        "_fetch_github",
        AsyncMock(return_value={"state": "MERGED", "draft": False, "checks": [{"bucket": "passed"}]}),
    )

    try:
        await source.fetch_pull_request(url)

        # The sidebar chip now reads the same lifecycle the detail panel got.
        assert source.get_cached_check_status(url) == {"ci": "passed", "state": "merged"}
        # Tagged 'detail' so the client knows a full fetch produced the value;
        # the client refetches on both origins (cross-window convergence) but the
        # initiator's refetch just hits the warm cache.
        sink.assert_called_once_with(
            {"url": url, "origin": "detail", "ci": "passed", "state": "merged"}
        )
    finally:
        source.unregister_status_delta_sink(sink)
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_chip_refresh_drops_stale_full_payload_and_emits_delta(monkeypatch) -> None:
    """The reverse direction: a chip change invalidates the detail payload."""
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._check_cache.clear()
    source._check_inflight.clear()
    source._status_delta_sinks.clear()
    source._CACHE[url] = (source.time.monotonic(), 10, {"state": "OPEN"})
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"ci": "passed", "state": "merged"}),
    )

    try:
        await source._refresh_check_status(url)

        # The panel's cached "OPEN" payload is gone, so its next read is fresh
        # instead of rendering a lifecycle the chip has already moved past.
        assert url not in source._CACHE
        sink.assert_called_once_with(
            {"url": url, "origin": "chip", "ci": "passed", "state": "merged"}
        )
    finally:
        source.unregister_status_delta_sink(sink)
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_chip_refresh_without_change_keeps_full_payload(monkeypatch) -> None:
    """An unchanged status must not throw away a valid cached payload."""
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_cache[url] = (source.time.monotonic(), {"ci": "passed", "state": "open"})
    source._CACHE[url] = (source.time.monotonic(), 10, {"state": "OPEN"})
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"ci": "passed", "state": "open"}),
    )

    await source._refresh_check_status(url)

    assert url in source._CACHE
    source._CACHE.clear()
    source._check_cache.clear()


@pytest.mark.asyncio
async def test_chip_refresh_keeps_known_ci_when_rollup_alone_fails(monkeypatch) -> None:
    """Mirror of the full-payload keep-known rule for a partial `checks`: a
    degraded rollup must not erase a glyph the chip cache already knows, and
    the internal marker must never reach the cache."""
    url = "https://github.com/acme/repo/pull/12"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_cache[url] = (source.time.monotonic(), {"ci": "passed", "state": "open"})
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"state": "open", source._CHIP_CI_UNAVAILABLE: "1"}),
    )

    try:
        await source._refresh_check_status(url)

        assert source._check_cache[url][1] == {"ci": "passed", "state": "open"}
    finally:
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_chip_refresh_lets_a_clean_empty_rollup_clear_a_stale_ci(monkeypatch) -> None:
    """A rollup that SUCCEEDS with zero checks carries no marker: the CI glyph
    was legitimately withdrawn (no checks configured), so it must clear."""
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_cache[url] = (source.time.monotonic(), {"ci": "passed", "state": "open"})
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"state": "open"}),
    )

    try:
        await source._refresh_check_status(url)

        assert source._check_cache[url][1] == {"state": "open"}
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_fetch_check_status_gitlab_manual_pipeline_matches_projection(monkeypatch) -> None:
    """A manual-gated GitLab pipeline must project the same CI on both caches.

    A `manual` pipeline aggregate is a BLOCKING gate — work is still outstanding
    — so both paths project "running" (not "passed"). The chip reads the
    aggregate directly; the full payload reads the same aggregate via `ciStatus`,
    so they cannot diverge and cannot ping-pong.
    """
    url = "https://gitlab.com/acme/repo/-/merge_requests/7"

    async def fake_run_json(*args, **kwargs):
        path = args[2] if len(args) > 2 else ""
        if "pipelines" in path:
            return [{"id": 99, "status": "manual"}]
        return {"state": "opened", "draft": False}

    monkeypatch.setattr(source, "_run_json", AsyncMock(side_effect=fake_run_json))

    chip = await source._fetch_check_status(url)

    assert chip == {"ci": "running", "state": "open"}
    # Coherence check: the full-payload projection carrying the same aggregate
    # (`ciStatus: "running"`) resolves identically, regardless of the manual
    # job's own "skipped" display bucket.
    assert source.status_from_full_payload(
        {"state": "opened", "ciStatus": "running", "checks": [{"bucket": "skipped"}]}
    ) == {"ci": "running", "state": "open"}


@pytest.mark.asyncio
async def test_fetch_check_status_gitlab_closed_draft_matches_projection(monkeypatch) -> None:
    """A closed-while-draft GitLab MR must agree on both caches; locked yields none.

    GitLab keeps `draft: true` on an MR closed while still in draft (both paths
    must project "closed", not "draft", or the mutual-invalidation ping-pongs),
    and reports `locked` transiently during a merge (both paths project NO
    lifecycle for it — a terminal "closed" glyph on a mid-merge MR is false).
    """
    base = "https://gitlab.com/acme/repo/-/merge_requests/"

    def run_json_for(details):
        async def fake_run_json(*args, **kwargs):
            path = args[2] if len(args) > 2 else ""
            if "pipelines" in path:
                return []
            return details

        return fake_run_json

    # Closed while still in draft.
    monkeypatch.setattr(
        source, "_run_json", AsyncMock(side_effect=run_json_for({"state": "closed", "draft": True}))
    )
    chip = await source._fetch_check_status(base + "7")
    assert chip == {"state": "closed"}
    assert source.status_from_full_payload({"state": "closed", "draft": True}) == chip

    # Locked (transient during merge): no lifecycle projection on either path.
    monkeypatch.setattr(
        source, "_run_json", AsyncMock(side_effect=run_json_for({"state": "locked"}))
    )
    chip = await source._fetch_check_status(base + "8")
    assert chip is None
    assert source.status_from_full_payload({"state": "locked"}) is None


@pytest.mark.asyncio
async def test_fetch_check_status_gitlab_allow_failure_matches_projection(monkeypatch) -> None:
    """An allow_failure red job shows failed in the list but never diverges the glyph.

    GitLab folds an allowed-failure job into a `success` pipeline aggregate. The
    single CI glyph is projected from that aggregate (authoritative, lossless) on
    BOTH paths — the chip reads it directly, the full payload via `ciStatus` — so
    the job's FAITHFUL "failed" display bucket (which must be preserved so the
    Checks tab does not falsely claim "all checks passed") cannot make the two
    caches disagree or ping-pong.
    """
    # `_gitlab_check` buckets are faithful: an allowed failure still shows failed.
    assert source._gitlab_check({"status": "failed", "allow_failure": True})["bucket"] == "failed"
    assert source._gitlab_check({"status": "failed", "allow_failure": False})["bucket"] == "failed"

    url = "https://gitlab.com/acme/repo/-/merge_requests/11"

    async def fake_run_json(*args, **kwargs):
        path = args[2] if len(args) > 2 else ""
        if "pipelines" in path:
            # GitLab reports the aggregate as `success` when only allow_failure
            # jobs failed.
            return [{"id": 42, "status": "success"}]
        return {"state": "opened", "draft": False}

    monkeypatch.setattr(source, "_run_json", AsyncMock(side_effect=fake_run_json))
    chip = await source._fetch_check_status(url)
    assert chip == {"ci": "passed", "state": "open"}

    # The full payload carries the SAME authoritative aggregate as `ciStatus`
    # ("passed"), so it resolves identically even though a real job bucket is
    # "failed" — the glyph comes from the aggregate, not the job rollup.
    payload = {
        "state": "opened",
        "ciStatus": "passed",
        "checks": [
            source._gitlab_check({"status": "success"}),
            source._gitlab_check({"status": "failed", "allow_failure": True}),
        ],
    }
    assert source.status_from_full_payload(payload) == {"ci": "passed", "state": "open"}


@pytest.mark.asyncio
async def test_fetch_gitlab_full_payload_synthesizes_pipeline_when_jobs_empty(monkeypatch) -> None:
    """A pipeline with no materialized jobs must still project CI on both caches.

    When a GitLab pipeline exists but its jobs have not materialized yet, the
    chip path reads the pipeline AGGREGATE directly, but the full payload built
    `checks: []` and thus projected no `ci` — clearing a glyph the chip still
    shows and arming the mutual-invalidation ping-pong. The full payload must
    fall back to the aggregate (distinct from a genuinely pipeline-less MR, which
    keeps `checks` empty). Folds in the empty-jobs case (Arbiter item 2).
    """

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            return {
                "iid": 42,
                "title": "WIP",
                "state": "opened",
                "web_url": "https://gitlab.com/acme/repo/-/merge_requests/42",
                "source_branch": "fix",
                "target_branch": "main",
                "sha": "abc123",
                "author": {"username": "dev"},
            }
        if "/pipelines?" in command:
            return [{"id": 91, "status": "running", "web_url": "https://gitlab.com/p/91"}]
        if "/pipelines/91/jobs" in command:
            return []  # pipeline exists, jobs not yet materialized
        if "/commits?" in command:
            return []
        if "/discussions?" in command:
            return []
        if "/changes" in command:
            return {"changes": []}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    # `checks` is not empty: it carries the synthesized pipeline aggregate...
    assert [c["name"] for c in data["checks"]] == ["Pipeline"]
    assert data["checks"][0]["bucket"] == "pending"  # running aggregate → pending bucket
    # ...so the full-payload projection agrees with the chip aggregate ("running").
    assert source.status_from_full_payload(data) == {"ci": "running", "state": "open"}


@pytest.mark.asyncio
async def test_fetch_gitlab_full_payload_no_pipeline_keeps_checks_empty(monkeypatch) -> None:
    """A genuinely pipeline-less MR must keep `checks` empty (no CI on either side)."""

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/43"):
            return {
                "iid": 43,
                "state": "opened",
                "web_url": "https://gitlab.com/acme/repo/-/merge_requests/43",
                "source_branch": "fix",
                "target_branch": "main",
                "sha": "abc123",
                "author": {"username": "dev"},
            }
        if "/pipelines?" in command:
            return []  # no pipeline at all
        if "/commits?" in command or "/discussions?" in command:
            return []
        if "/changes" in command:
            return {"changes": []}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/43")
    )

    assert data["checks"] == []
    assert source.status_from_full_payload(data) == {"state": "open"}


def test_gitlab_aggregate_ci_vocabulary() -> None:
    """The authoritative aggregate CI mapping used by BOTH projection paths.

    A `manual` aggregate is a blocking gate → running (not passed); an
    allow_failure red job is already folded into a `success` aggregate → passed;
    a wholly skipped pipeline has no failures → passed; unknown/transient →
    running; empty → nothing.
    """
    assert source._gitlab_aggregate_ci("success") == "passed"
    assert source._gitlab_aggregate_ci("skipped") == "passed"
    assert source._gitlab_aggregate_ci("failed") == "failed"
    assert source._gitlab_aggregate_ci("canceled") == "failed"
    assert source._gitlab_aggregate_ci("manual") == "running"
    assert source._gitlab_aggregate_ci("running") == "running"
    assert source._gitlab_aggregate_ci("pending") == "running"
    assert source._gitlab_aggregate_ci("waiting_for_resource") == "running"
    assert source._gitlab_aggregate_ci("waiting_for_callback") == "running"
    assert source._gitlab_aggregate_ci("") is None


def test_gitlab_check_bucket_is_faithful() -> None:
    """Per-job buckets are for the Checks list and stay faithful to the job status."""
    assert source._gitlab_check({"status": "success"})["bucket"] == "passed"
    assert source._gitlab_check({"status": "failed"})["bucket"] == "failed"
    # An allow_failure red job still shows failed — the glyph is protected by the
    # aggregate, so hiding the job would only mislead the Checks tab.
    assert source._gitlab_check({"status": "failed", "allow_failure": True})["bucket"] == "failed"
    assert source._gitlab_check({"status": "manual"})["bucket"] == "skipped"
    assert source._gitlab_check({"status": "running"})["bucket"] == "pending"


@pytest.mark.asyncio
async def test_fetch_gitlab_full_jobs_page_keeps_aggregate_authoritative(monkeypatch) -> None:
    """A truncated (full-page) job list must not poison the CI glyph (#1097).

    When the jobs list comes back as a full page it may be truncated — a failed
    job on a later page would be invisible. The glyph is projected from the
    pipeline AGGREGATE (`ciStatus`), which stays authoritative, and the Checks
    LIST is flagged partial so the panel does not imply it is exhaustive.
    """
    page = source._SECONDARY_PAGE_SIZE

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/50"):
            return {
                "iid": 50,
                "state": "opened",
                "web_url": "https://gitlab.com/acme/repo/-/merge_requests/50",
                "source_branch": "fix",
                "target_branch": "main",
                "sha": "abc",
                "author": {"username": "dev"},
            }
        if "/pipelines?" in command:
            # Aggregate says FAILED (a later-page job failed).
            return [{"id": 77, "status": "failed"}]
        if "/pipelines/77/jobs" in command:
            # A full page of green jobs — the failed one is beyond this page.
            return [{"status": "success", "name": f"job{i}"} for i in range(page)]
        if "/commits?" in command or "/discussions?" in command:
            return []
        if "/changes" in command:
            return {"changes": []}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/50")
    )

    # Checks list is flagged partial (may be truncated)...
    assert "checks" in data["partialSections"]
    # ...but the glyph is the authoritative aggregate ("failed"), NOT a rollup of
    # the visible all-green page.
    assert data["ciStatus"] == "failed"
    assert source.status_from_full_payload(data) == {"ci": "failed", "state": "open"}


def test_record_full_payload_clears_flap_tracker_on_change() -> None:
    """An authoritative full-payload write resets the chip flap counter (#2079).

    The flap damper counts *consecutive identical* chip transitions. A
    full-payload write that changes the status between chip refreshes is a real,
    independent change — it must clear the tracker so three legitimate repeated
    CI runs are not mistaken for a single repeating loop and falsely damped.
    """
    url = "https://github.com/acme/repo/pull/88"
    source._check_cache.clear()
    source._check_flap.clear()
    source._check_flap_damped.clear()
    source._status_delta_sinks.clear()
    # Seed a flap tracker as if a chip transition had been recorded.
    source._check_flap[url] = (("state=open", "state=merged"), 2)
    source._check_flap_damped.add(url)
    try:
        # A full-payload write that changes the cached status clears the tracker.
        source.record_full_payload_status(url, {"state": "MERGED", "checks": [{"bucket": "passed"}]})
        assert url not in source._check_flap
        assert url not in source._check_flap_damped
    finally:
        source._check_cache.clear()
        source._check_flap.clear()
        source._check_flap_damped.clear()


@pytest.mark.asyncio
async def test_forced_refresh_inflight_not_floored_and_requeues(monkeypatch) -> None:
    """An already-in-flight URL is not floor-stamped and gets one follow-up (#2333).

    A TTL-paced chip fetch may be in flight when the turn boundary fires. Its
    result can predate the turn's final push, so the forced call must NOT record
    it as "just forced" (which would satisfy the floor with pre-turn data and
    lock out a corrective read for the floor interval); instead it queues exactly
    one follow-up forced refresh for when the in-flight fetch completes.
    """
    url = "https://github.com/acme/repo/pull/91"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    source._check_force_pending.clear()
    # Pretend a TTL-paced refresh for this URL is already in flight.
    source._check_inflight.add(url)
    try:
        started = source.request_check_refresh_now([url])
        # It was reported as refreshing (in flight)...
        assert url in started
        # ...but NOT floor-stamped (its in-flight result may be pre-turn)...
        assert url not in source._check_forced_at
        # ...and a follow-up forced read is queued for completion.
        assert url in source._check_force_pending
    finally:
        source._check_cache.clear()
        source._check_inflight.clear()
        source._check_forced_at.clear()
        source._check_force_pending.clear()


@pytest.mark.asyncio
async def test_refresh_check_status_issues_pending_follow_up_force(monkeypatch) -> None:
    """When a fetch completes for a URL with a queued force, one follow-up fires (#2333)."""
    url = "https://github.com/acme/repo/pull/92"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_force_pending.clear()
    source._status_delta_sinks.clear()
    source._check_force_pending.add(url)
    monkeypatch.setattr(
        source, "_fetch_check_status", AsyncMock(return_value={"state": "open"})
    )
    schedule = MagicMock(return_value=[])
    monkeypatch.setattr(source, "schedule_check_refresh", schedule)
    try:
        await source._refresh_check_status(url)
        # The queued force was consumed and exactly one follow-up forced refresh
        # was scheduled for this URL.
        assert url not in source._check_force_pending
        schedule.assert_called_once()
        args, kwargs = schedule.call_args
        assert args[0] == [url]
        assert kwargs.get("force") is True
    finally:
        source._check_cache.clear()
        source._check_inflight.clear()
        source._check_force_pending.clear()


@pytest.mark.asyncio
async def test_chip_refresh_damps_projection_flap(monkeypatch) -> None:
    """A URL whose chip status keeps flapping must stop driving the invalidation loop.

    If the chip and full-payload projections ever disagree on a URL's vocabulary
    (a bug), the chip refresh observes the identical changed transition every
    cycle and the mutual-invalidation protocol would spawn provider reads
    forever. After a small number of identical transitions the loop-breaker must
    stop invalidating the full payload and emitting deltas for that URL, so the
    divergence degrades to a stale glyph rather than an unbounded loop (Arbiter
    item 2 / Design CONCERN #1).
    """
    url = "https://gitlab.com/acme/repo/-/merge_requests/9"
    source._CACHE.clear()
    source._check_cache.clear()
    source._check_inflight.clear()
    source._status_delta_sinks.clear()
    source._check_flap.clear()
    source._check_flap_damped.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_full_payload_cache", invalidate)

    # Simulate the flap: the chip always projects "draft" from the provider,
    # while a full-payload write-through keeps resetting the cache to "closed"
    # between refreshes — so every refresh sees the identical closed -> draft
    # changed transition.
    monkeypatch.setattr(
        source, "_fetch_check_status", AsyncMock(return_value={"state": "draft"})
    )

    try:
        for _ in range(source._CHECK_FLAP_DAMP_THRESHOLD + 2):
            source._check_cache[url] = (source.time.monotonic(), {"state": "closed"})
            source._CACHE[url] = (source.time.monotonic(), 10, {"state": "closed"})
            source._check_inflight.discard(url)
            await source._refresh_check_status(url)

        # Once damped, the loop drivers stop firing for this URL.
        assert url in source._check_flap_damped
        # The first (threshold) transitions still invalidated/emitted; after
        # damping, neither fires — so both counts are capped below the number of
        # rounds run.
        assert invalidate.await_count == source._CHECK_FLAP_DAMP_THRESHOLD - 1
        assert sink.call_count == source._CHECK_FLAP_DAMP_THRESHOLD - 1
        # The chip cache still tracks the latest projection (glyph stays live,
        # just no longer drives the loop).
        assert source.get_cached_check_status(url) == {"state": "draft"}
        # A genuinely different transition clears the damp.
        source._check_cache[url] = (source.time.monotonic(), {"state": "draft"})
        source._check_inflight.discard(url)
        monkeypatch.setattr(
            source, "_fetch_check_status", AsyncMock(return_value={"state": "merged"})
        )
        await source._refresh_check_status(url)
        assert url not in source._check_flap_damped
    finally:
        source.unregister_status_delta_sink(sink)
        source._CACHE.clear()
        source._check_cache.clear()
        source._check_inflight.clear()
        source._check_flap.clear()
        source._check_flap_damped.clear()


@pytest.mark.asyncio
async def test_chip_refresh_defers_to_concurrent_full_payload_write(monkeypatch) -> None:
    """A concurrent full fetch that lands during the chip await must win.

    The turn-boundary design fires a full fetch and a forced chip refresh for
    the same URL together. If the full fetch resolves first and writes a fresh
    projection, the chip refresh must not clobber it or emit a redundant delta
    by comparing against its own stale pre-await snapshot (Arbiter item 2).
    """
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._check_cache.clear()
    source._check_inflight.clear()
    source._status_delta_sinks.clear()
    # Pre-await snapshot the chip refresh will capture.
    source._check_cache[url] = (source.time.monotonic(), {"ci": "running", "state": "open"})
    source._CACHE[url] = (source.time.monotonic(), 10, {"state": "OPEN"})
    sink = MagicMock()
    source.register_status_delta_sink(sink)

    async def fetch_and_simulate_concurrent_full(_url):
        # While this chip fetch is "in flight", a concurrent full fetch resolves
        # first and writes the newer projection through record_full_payload_status.
        source._check_cache[_url] = (source.time.monotonic(), {"ci": "passed", "state": "merged"})
        return {"ci": "running", "state": "open"}

    monkeypatch.setattr(
        source, "_fetch_check_status", AsyncMock(side_effect=fetch_and_simulate_concurrent_full)
    )
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_full_payload_cache", invalidate)

    try:
        await source._refresh_check_status(url)

        # The concurrent full-payload projection is preserved, not overwritten by
        # this older chip read; no spurious invalidation or chip delta fired.
        assert source._check_cache[url][1] == {"ci": "passed", "state": "merged"}
        assert url in source._CACHE
        invalidate.assert_not_called()
        sink.assert_not_called()
    finally:
        source.unregister_status_delta_sink(sink)
        source._CACHE.clear()
        source._check_cache.clear()
        source._check_inflight.clear()


@pytest.mark.asyncio
async def test_request_check_refresh_now_floors_rapid_turns(monkeypatch) -> None:
    """Turn boundaries beat the TTL, but a burst of turns cannot beat the floor."""
    url = "https://github.com/acme/repo/pull/12"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    monkeypatch.setattr(source, "_refresh_check_status", AsyncMock(return_value=None))
    # A cache entry well inside its TTL: plain scheduling would skip it entirely.
    source._check_cache[url] = (source.time.monotonic(), {"state": "open"})

    assert source.schedule_check_refresh([url]) == []
    assert source.request_check_refresh_now([url]) == [url]
    source._check_inflight.clear()
    # Second turn inside the floor falls back to TTL pacing (i.e. does nothing)
    # rather than spawning another provider read.
    assert source.request_check_refresh_now([url]) == []

    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()


@pytest.mark.asyncio
async def test_request_check_refresh_now_refreshes_again_after_floor(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/12"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    monkeypatch.setattr(source, "_refresh_check_status", AsyncMock(return_value=None))
    source._check_cache[url] = (source.time.monotonic(), {"state": "open"})

    assert source.request_check_refresh_now([url]) == [url]
    source._check_inflight.clear()
    source._check_forced_at[url] = (
        source.time.monotonic() - source._CHECK_FORCE_MIN_INTERVAL_SECS - 1
    )

    assert source.request_check_refresh_now([url]) == [url]

    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()


def test_status_delta_sinks_are_deduped_and_failure_isolated() -> None:
    source._status_delta_sinks.clear()
    good = MagicMock()
    boom = MagicMock(side_effect=RuntimeError("owner socket died"))
    source.register_status_delta_sink(good)
    source.register_status_delta_sink(good)
    source.register_status_delta_sink(boom)

    try:
        assert len(source._status_delta_sinks) == 2
        source._emit_status_delta("https://github.com/acme/repo/pull/1", {"state": "open"}, "chip")
        # One broken sink must not starve the others.
        good.assert_called_once()
    finally:
        source._status_delta_sinks.clear()


def test_trim_check_cache_bounds_the_force_ledger() -> None:
    source._check_cache.clear()
    source._check_forced_at.clear()
    for index in range(source._CHECK_CACHE_MAX + 5):
        source._check_forced_at[f"https://github.com/acme/repo/pull/{index}"] = float(index)

    source._trim_check_cache()

    assert len(source._check_forced_at) == source._CHECK_CACHE_MAX
    # Oldest forced timestamps are evicted first.
    assert "https://github.com/acme/repo/pull/0" not in source._check_forced_at
    source._check_forced_at.clear()


@pytest.mark.asyncio
async def test_status_endpoint_bounds_urls_and_rejects_non_list_bodies(monkeypatch) -> None:
    refresh = MagicMock(return_value=[])
    monkeypatch.setattr(source, "schedule_check_refresh", refresh)
    source._check_cache.clear()
    urls = [
        f"https://github.com/acme/repo/pull/{index + 1}"
        for index in range(source.STATUS_URLS_MAX + 5)
    ]

    app = _app()
    async with TestClient(TestServer(app)) as client:
        accepted = await client.post("/api/source/pull-request/status", json={"urls": urls})
        assert accepted.status == 200
        rejected = await client.post("/api/source/pull-request/status", json={"urls": "all"})
        assert rejected.status == 400

    assert len(refresh.call_args.args[0]) == source.STATUS_URLS_MAX
    source._check_cache.clear()


@pytest.mark.asyncio
async def test_status_endpoint_requires_owner_identity() -> None:
    app = _app(user="U_OTHER")
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/source/pull-request/status",
            json={"urls": ["https://github.com/acme/repo/pull/12"]},
        )
        assert response.status == 403


@pytest.mark.asyncio
async def test_schedule_check_refresh_backs_off_overflow_without_spawning(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/99"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_inflight.add("https://github.com/acme/repo/pull/1")
    monkeypatch.setattr(source, "_CHECK_PENDING_MAX", 1)
    task_count = len(source._CHECK_TASKS)

    source.schedule_check_refresh([url, url])

    assert len(source._CHECK_TASKS) == task_count
    assert source._check_cache[url][1] is None
    first_timestamp = source._check_cache[url][0]
    source.schedule_check_refresh([url])
    assert source._check_cache[url][0] == first_timestamp
    source._check_cache.clear()
    source._check_inflight.clear()


@pytest.mark.asyncio
async def test_fetch_github_checks_uses_one_call_without_rewriting_cache(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._CACHE[url] = (1.0, 21, {"provider": "github", "checks": []})
    cached = source._CACHE[url]
    run = AsyncMock(
        return_value={
            "statusCheckRollup": [
                {"name": "test", "status": "IN_PROGRESS", "conclusion": "SUCCESS"}
            ]
        }
    )
    monkeypatch.setattr(source, "_run_json", run)

    checks = await source.fetch_pull_request_checks(url)

    run.assert_awaited_once_with(
        "gh",
        "pr",
        "view",
        url,
        "--json",
        "statusCheckRollup,headRefOid",
        max_output_bytes=source._CHECKS_OUTPUT_BYTES,
    )
    assert checks[0]["bucket"] == "pending"
    assert source._CACHE[url] == cached
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_full_fetch_coalesces_concurrent_forced_refreshes(monkeypatch) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    release = source.asyncio.Event()
    started = source.asyncio.Event()
    calls = 0

    async def fetch(ref):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"provider": "github", "url": ref.url}

    monkeypatch.setattr(source, "_fetch_github", fetch)
    url = "https://github.com/acme/repo/pull/12"
    first = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    await started.wait()
    second = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    await source.asyncio.sleep(0)
    release.set()

    assert await first == await second
    assert calls == 1
    await source.asyncio.sleep(0)
    assert url not in source._FULL_FETCH_INFLIGHT
    assert url not in source._FULL_FETCH_TASKS
    assert url not in source._FULL_FETCH_GENERATIONS
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_full_fetch_projects_chip_status_under_cache_lock(monkeypatch) -> None:
    """The write-through projection must run inside ``_CACHE_LOCK``.

    Regression for the TOCTOU where a provider mutation landing between the
    passing generation check and the chip projection could republish
    pre-mutation status into the chip cache — a stale ``source_status`` delta the
    full-cache invalidation cannot undo. Keeping the projection in the same
    locked transaction as the generation check and the ``_CACHE`` write closes
    the window: a mutation needs the same lock to bump the generation.
    """
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._check_cache.clear()

    async def fetch(ref):
        return {"provider": "github", "url": ref.url, "state": "OPEN"}

    monkeypatch.setattr(source, "_fetch_github", fetch)

    observed: dict[str, bool] = {}
    real_record = source.record_full_payload_status

    def spy(url, payload):
        # `_fetch_pull_request_uncached` calls the bare module global, so this
        # patched name is what it resolves at call time.
        observed["locked"] = source._CACHE_LOCK.locked()
        return real_record(url, payload)

    monkeypatch.setattr(source, "record_full_payload_status", spy)

    url = "https://github.com/acme/repo/pull/12"
    await source.fetch_pull_request(url, refresh=True)

    assert observed.get("locked") is True
    source._CACHE.clear()
    source._check_cache.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()


@pytest.mark.asyncio
async def test_resolve_supersedes_active_full_fetch(monkeypatch) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    old_started = source.asyncio.Event()
    release_old = source.asyncio.Event()
    state = {"resolved": False}
    calls = 0

    async def fetch(ref):
        nonlocal calls
        calls += 1
        resolved = state["resolved"]
        if calls == 1:
            old_started.set()
            await release_old.wait()
        return {"provider": "github", "url": ref.url, "resolved": resolved}

    membership = {
        "data": {
            "repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_thread1"}]}}}
        }
    }

    async def run(*argv: str, **_kwargs: int):
        if any("resolveReviewThread" in part and "mutation" in part for part in argv):
            state["resolved"] = True
            return {}
        return membership

    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/12"
    stale_task = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    await old_started.wait()

    await source.resolve_pull_request_thread(url, "PRRT_thread1")
    fresh = await source.asyncio.wait_for(source.fetch_pull_request(url, refresh=True), timeout=0.5)
    assert fresh["resolved"] is True
    assert calls == 2

    release_old.set()
    stale = await stale_task
    assert stale["resolved"] is False
    assert source._CACHE[url][2]["resolved"] is True
    await source.asyncio.sleep(0)
    assert url not in source._FULL_FETCH_INFLIGHT
    assert url not in source._FULL_FETCH_TASKS
    assert url not in source._FULL_FETCH_GENERATIONS
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()


@pytest.mark.asyncio
async def test_checks_fetch_coalesces_concurrent_requests(monkeypatch) -> None:
    source._CHECKS_FETCH_INFLIGHT.clear()
    release = source.asyncio.Event()
    started = source.asyncio.Event()
    calls = 0

    async def fetch(_ref):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return [{"name": "test", "bucket": "pending"}]

    monkeypatch.setattr(source, "_fetch_github_checks", fetch)
    url = "https://github.com/acme/repo/pull/12"
    first = source.asyncio.create_task(source.fetch_pull_request_checks(url))
    await started.wait()
    second = source.asyncio.create_task(source.fetch_pull_request_checks(url))
    await source.asyncio.sleep(0)
    release.set()

    assert await first == await second
    assert calls == 1
    await source.asyncio.sleep(0)
    assert not source._CHECKS_FETCH_INFLIGHT


@pytest.mark.asyncio
async def test_direct_fetch_pending_bound_is_combined_and_coalesces(monkeypatch) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._CHECKS_FETCH_INFLIGHT.clear()
    source._DIRECT_FETCH_RESERVATIONS.clear()
    release = source.asyncio.Event()
    full_started = source.asyncio.Event()
    checks_started = source.asyncio.Event()

    async def fetch_full(ref):
        full_started.set()
        await release.wait()
        return {"provider": "github", "url": ref.url}

    async def fetch_checks(_ref):
        checks_started.set()
        await release.wait()
        return [{"name": "test", "bucket": "pending"}]

    monkeypatch.setattr(source, "_DIRECT_FETCH_PENDING_MAX", 16)
    # Admission now WAITS for room before refusing, so a test asserting the
    # ceiling has to shrink the budget or it would sit out the real one.
    monkeypatch.setattr(source, "_DIRECT_FETCH_WAIT_SECS", 0.05)
    monkeypatch.setattr(
        source,
        "_DIRECT_FETCH_MAX_RESERVED_BYTES",
        source._FULL_FETCH_RESERVATION_BYTES + source._CHECKS_FETCH_RESERVATION_BYTES,
    )
    monkeypatch.setattr(source, "_fetch_github", fetch_full)
    monkeypatch.setattr(source, "_fetch_github_checks", fetch_checks)
    full_url = "https://github.com/acme/repo/pull/20"
    checks_url = "https://github.com/acme/repo/pull/21"
    overflow_url = "https://github.com/acme/repo/pull/22"

    full = source.asyncio.create_task(source.fetch_pull_request(full_url, refresh=True))
    checks = source.asyncio.create_task(source.fetch_pull_request_checks(checks_url))
    await full_started.wait()
    await checks_started.wait()
    duplicate = source.asyncio.create_task(source.fetch_pull_request(full_url, refresh=True))
    await source.asyncio.sleep(0)

    with pytest.raises(source.SourceProviderError, match="requests are pending"):
        await source.fetch_pull_request(overflow_url, refresh=True)
    assert len(source._direct_fetch_tasks()) == 2
    assert len(source._DIRECT_FETCH_RESERVATIONS) == 2
    assert sum(source._DIRECT_FETCH_RESERVATIONS.values()) == (
        source._FULL_FETCH_RESERVATION_BYTES + source._CHECKS_FETCH_RESERVATION_BYTES
    )
    assert len(source._FULL_FETCH_INFLIGHT) <= 2
    assert len(source._CHECKS_FETCH_INFLIGHT) <= 2

    release.set()
    assert await full == await duplicate
    await checks
    await source.asyncio.sleep(0)
    assert not source._FULL_FETCH_INFLIGHT
    assert not source._FULL_FETCH_TASKS
    assert not source._FULL_FETCH_GENERATIONS
    assert not source._CHECKS_FETCH_INFLIGHT
    assert not source._direct_fetch_tasks()
    assert not source._DIRECT_FETCH_RESERVATIONS
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_cancelled_waiter_keeps_shared_fetch_and_reservation(monkeypatch) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._CHECKS_FETCH_INFLIGHT.clear()
    source._DIRECT_FETCH_RESERVATIONS.clear()
    started = source.asyncio.Event()
    release = source.asyncio.Event()
    calls = 0

    async def fetch(ref):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"provider": "github", "url": ref.url}

    monkeypatch.setattr(source, "_fetch_github", fetch)
    url = "https://github.com/acme/repo/pull/22"
    waiter = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    await started.wait()
    waiter.cancel()
    with pytest.raises(source.asyncio.CancelledError):
        await waiter

    assert url in source._FULL_FETCH_INFLIGHT
    assert list(source._DIRECT_FETCH_RESERVATIONS.values()) == [
        source._FULL_FETCH_RESERVATION_BYTES
    ]
    coalesced = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    await source.asyncio.sleep(0)
    assert calls == 1

    release.set()
    assert (await coalesced)["url"] == url
    await source.asyncio.sleep(0)
    assert not source._direct_fetch_tasks()
    assert not source._DIRECT_FETCH_RESERVATIONS
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_stale_and_fresh_full_fetches_fit_exact_reservation_ceiling(
    monkeypatch,
) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._CHECKS_FETCH_INFLIGHT.clear()
    source._DIRECT_FETCH_RESERVATIONS.clear()
    old_started = source.asyncio.Event()
    fresh_started = source.asyncio.Event()
    release_old = source.asyncio.Event()
    release_fresh = source.asyncio.Event()
    calls = 0

    async def fetch(ref):
        nonlocal calls
        calls += 1
        if calls == 1:
            old_started.set()
            await release_old.wait()
        else:
            fresh_started.set()
            await release_fresh.wait()
        return {"provider": "github", "url": ref.url, "call": calls}

    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(
        source,
        "_DIRECT_FETCH_MAX_RESERVED_BYTES",
        2 * source._FULL_FETCH_RESERVATION_BYTES,
    )
    monkeypatch.setattr(source, "_DIRECT_FETCH_WAIT_SECS", 0.05)
    url = "https://github.com/acme/repo/pull/23"
    stale = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    await old_started.wait()
    await source._invalidate_pull_request_cache(url)
    fresh = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    await fresh_started.wait()

    assert len(source._DIRECT_FETCH_RESERVATIONS) == 2
    assert sum(source._DIRECT_FETCH_RESERVATIONS.values()) == (
        source._DIRECT_FETCH_MAX_RESERVED_BYTES
    )
    with pytest.raises(source.SourceProviderError, match="requests are pending"):
        await source.fetch_pull_request_checks("https://github.com/acme/repo/pull/24")

    release_old.set()
    release_fresh.set()
    await stale
    await fresh
    await source.asyncio.sleep(0)
    assert not source._direct_fetch_tasks()
    assert not source._DIRECT_FETCH_RESERVATIONS
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_direct_fetch_bound_counts_detached_stale_full_task(monkeypatch) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._CHECKS_FETCH_INFLIGHT.clear()
    source._DIRECT_FETCH_RESERVATIONS.clear()
    old_started = source.asyncio.Event()
    release_old = source.asyncio.Event()
    state = {"resolved": False}
    calls = 0

    async def fetch(ref):
        nonlocal calls
        calls += 1
        resolved = state["resolved"]
        if calls == 1:
            old_started.set()
            await release_old.wait()
        return {"provider": "github", "url": ref.url, "resolved": resolved}

    membership = {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_1"}]}}}}
    }

    async def run(*argv: str, **_kwargs: int):
        if any("resolveReviewThread" in part and "mutation" in part for part in argv):
            state["resolved"] = True
            return {}
        return membership

    monkeypatch.setattr(source, "_DIRECT_FETCH_PENDING_MAX", 1)
    monkeypatch.setattr(source, "_DIRECT_FETCH_WAIT_SECS", 0.05)
    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/23"
    stale = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    await old_started.wait()
    await source.resolve_pull_request_thread(url, "PRRT_1")

    assert url not in source._FULL_FETCH_INFLIGHT
    assert len(source._direct_fetch_tasks()) == 1
    assert list(source._DIRECT_FETCH_RESERVATIONS.values()) == [
        source._FULL_FETCH_RESERVATION_BYTES
    ]
    with pytest.raises(source.SourceProviderError, match="requests are pending"):
        await source.fetch_pull_request(url, refresh=True)

    release_old.set()
    assert (await stale)["resolved"] is False
    await source.asyncio.sleep(0)
    assert not source._direct_fetch_tasks()
    assert not source._DIRECT_FETCH_RESERVATIONS
    fresh = await source.fetch_pull_request(url, refresh=True)
    assert fresh["resolved"] is True
    await source.asyncio.sleep(0)
    assert not source._FULL_FETCH_INFLIGHT
    assert not source._FULL_FETCH_TASKS
    assert not source._FULL_FETCH_GENERATIONS
    source._CACHE.clear()


def _reset_direct_fetch_state() -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._CHECKS_FETCH_INFLIGHT.clear()
    source._ISSUE_CACHE.clear()
    source._ISSUE_FETCH_INFLIGHT.clear()
    source._ISSUE_FETCH_TASKS.clear()
    source._DIRECT_FETCH_RESERVATIONS.clear()
    source._DIRECT_FETCH_WAITERS.clear()


def _reserved_bytes() -> int:
    tasks = source._direct_fetch_tasks()
    return sum(
        amount
        for task, amount in source._DIRECT_FETCH_RESERVATIONS.items()
        if task in tasks and not task.done()
    )


@pytest.mark.asyncio
async def test_saturated_pool_queues_the_next_fetch_instead_of_refusing(monkeypatch) -> None:
    """A caller arriving at a full pool WAITS for room and then succeeds.

    The panel's read is a user-facing load: refusing it outright turned ordinary
    concurrent use (two windows on two PRs) into an error card with a manual
    Retry. Waiting keeps the memory ceiling intact while removing the dead end.
    """
    _reset_direct_fetch_state()
    release = source.asyncio.Event()
    started = 0

    async def fetch(ref):
        nonlocal started
        started += 1
        await release.wait()
        return {"provider": "github", "url": ref.url}

    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(
        source,
        "_DIRECT_FETCH_MAX_RESERVED_BYTES",
        2 * source._FULL_FETCH_RESERVATION_BYTES,
    )
    first = source.asyncio.create_task(
        source.fetch_pull_request("https://github.com/acme/repo/pull/1", refresh=True)
    )
    second = source.asyncio.create_task(
        source.fetch_pull_request("https://github.com/acme/repo/pull/2", refresh=True)
    )
    while started < 2:
        await source.asyncio.sleep(0)
    assert _reserved_bytes() == source._DIRECT_FETCH_MAX_RESERVED_BYTES

    queued = source.asyncio.create_task(
        source.fetch_pull_request("https://github.com/acme/repo/pull/3", refresh=True)
    )
    for _ in range(10):
        await source.asyncio.sleep(0)
    # Queued, not resolved and not failed, and it did not breach the ceiling to
    # get there.
    assert not queued.done()
    assert source._DIRECT_FETCH_WAITERS
    assert _reserved_bytes() <= source._DIRECT_FETCH_MAX_RESERVED_BYTES

    release.set()
    assert (await source.asyncio.wait_for(queued, timeout=5))["url"] == (
        "https://github.com/acme/repo/pull/3"
    )
    await first
    await second
    await source.asyncio.sleep(0)
    assert not source._DIRECT_FETCH_RESERVATIONS
    assert not source._DIRECT_FETCH_WAITERS
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_queued_fetch_waits_outside_the_cache_lock(monkeypatch) -> None:
    """The admission wait must not hold ``_CACHE_LOCK``.

    An in-flight fetch takes the same lock to write its result, so waiting for
    capacity while holding it would deadlock: the queued caller would block the
    very completion that frees its room. This is the regression guard for that
    shape -- move the wait back inside the lock and this test times out.
    """
    _reset_direct_fetch_state()
    release = source.asyncio.Event()
    started = 0

    async def fetch(ref):
        nonlocal started
        started += 1
        await release.wait()
        return {"provider": "github", "url": ref.url}

    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(
        source, "_DIRECT_FETCH_MAX_RESERVED_BYTES", source._FULL_FETCH_RESERVATION_BYTES
    )
    holder = source.asyncio.create_task(
        source.fetch_pull_request("https://github.com/acme/repo/pull/4", refresh=True)
    )
    while started < 1:
        await source.asyncio.sleep(0)
    queued = source.asyncio.create_task(
        source.fetch_pull_request("https://github.com/acme/repo/pull/5", refresh=True)
    )
    for _ in range(10):
        await source.asyncio.sleep(0)
    assert not queued.done()

    release.set()
    # Both complete: the holder could still take _CACHE_LOCK to write its cache
    # entry while the queued caller was waiting.
    assert await source.asyncio.wait_for(holder, timeout=5)
    assert await source.asyncio.wait_for(queued, timeout=5)
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_capacity_error_is_marked_retryable_for_the_client(monkeypatch) -> None:
    """The wait-budget error carries ``code: source_busy``.

    A generic provider error (auth, missing PR) must stay fail-fast in the UI, so
    the retryable case needs its own machine-readable marker rather than the
    client pattern-matching on prose.
    """
    _reset_direct_fetch_state()
    monkeypatch.setattr(
        source,
        "fetch_pull_request",
        AsyncMock(side_effect=source.SourceCapacityError("Too many source requests are pending.")),
    )
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request", json={"url": "https://github.com/acme/repo/pull/9"}
        )
        assert response.status == 503
        assert (await response.json())["code"] == "source_busy"


@pytest.mark.asyncio
async def test_provider_error_is_not_marked_retryable(monkeypatch) -> None:
    """Revert guard for the marker: a real provider failure must NOT be busy."""
    _reset_direct_fetch_state()
    monkeypatch.setattr(
        source,
        "fetch_pull_request",
        AsyncMock(side_effect=source.SourceProviderError("gh could not authenticate")),
    )
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request", json={"url": "https://github.com/acme/repo/pull/9"}
        )
        assert response.status == 503
        assert (await response.json())["code"] == "provider_error"


@pytest.mark.asyncio
async def test_capacity_error_audits_its_own_reason(monkeypatch, _mock_source_sel) -> None:
    """The audit reason must match the code the caller receives.

    Back-pressure recorded as ``provider_error`` would read, in the audit trail,
    as the provider having failed. The pairing lives in one place so the two
    cannot drift.
    """
    _reset_direct_fetch_state()
    monkeypatch.setattr(
        source,
        "fetch_pull_request",
        AsyncMock(side_effect=source.SourceCapacityError("Too many source requests are pending.")),
    )
    async with TestClient(TestServer(_app())) as client:
        await client.post(
            "/api/source/pull-request", json={"url": "https://github.com/acme/repo/pull/9"}
        )
    reasons = [
        call.kwargs.get("error") for call in _mock_source_sel.log_api_access.call_args_list
    ]
    assert "capacity_exhausted" in reasons
    assert "provider_error" not in reasons


@pytest.mark.asyncio
async def test_capacity_error_remains_a_source_provider_error() -> None:
    """Every existing ``except SourceProviderError`` site must still catch it."""
    assert issubclass(source.SourceCapacityError, source.SourceProviderError)


@pytest.mark.asyncio
async def test_fetch_gitlab_checks_uses_at_most_two_calls(monkeypatch) -> None:
    run = AsyncMock(
        side_effect=[
            [{"id": 91, "status": "running", "web_url": "https://gitlab.com/p/91"}],
            [
                {
                    "name": "test",
                    "stage": "verify",
                    "status": "running",
                    "web_url": "https://gitlab.com/j/7",
                }
            ],
        ]
    )
    monkeypatch.setattr(source, "_run_json", run)

    checks = await source.fetch_pull_request_checks(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42"
    )

    assert run.await_count == 2
    assert (
        "projects/acme%2Fplatform%2Fservice/merge_requests/42/pipelines?per_page=1"
        in run.await_args_list[0].args
    )
    assert (
        "projects/acme%2Fplatform%2Fservice/pipelines/91/jobs?per_page=100"
        in run.await_args_list[1].args
    )
    # host must be forwarded on every glab call or _run_json's guard refuses it.
    assert [call.kwargs.get("host") for call in run.await_args_list] == ["gitlab.com"] * 2
    assert checks[0]["name"] == "test"
    assert checks[0]["bucket"] == "pending"


@pytest.mark.asyncio
async def test_fetch_gitlab_checks_falls_back_to_pipeline_without_jobs(monkeypatch) -> None:
    run = AsyncMock(
        side_effect=[
            [{"id": 91, "status": "success", "web_url": "https://gitlab.com/p/91"}],
            [],
        ]
    )
    monkeypatch.setattr(source, "_run_json", run)

    checks = await source.fetch_pull_request_checks(
        "https://gitlab.com/acme/repo/-/merge_requests/42"
    )

    assert run.await_count == 2
    assert [call.kwargs.get("host") for call in run.await_args_list] == ["gitlab.com"] * 2
    assert checks[0]["name"] == "Pipeline"
    assert checks[0]["bucket"] == "passed"


@pytest.mark.asyncio
async def test_gitlab_chip_status_uses_head_pipeline_without_second_call(monkeypatch) -> None:
    run = AsyncMock(
        return_value={
            "state": "opened",
            "draft": False,
            "head_pipeline": {"status": "running"},
        }
    )
    monkeypatch.setattr(source, "_run_json", run)

    status = await source._fetch_check_status(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42"
    )

    assert run.await_count == 1
    assert run.await_args_list[0].kwargs.get("host") == "gitlab.com"
    assert status == {"state": "open", "ci": "running"}


@pytest.mark.asyncio
async def test_gitlab_chip_status_falls_back_to_pipelines_list(monkeypatch) -> None:
    run = AsyncMock(
        side_effect=[
            {"state": "opened", "draft": True},
            [{"status": "failed"}],
        ]
    )
    monkeypatch.setattr(source, "_run_json", run)

    status = await source._fetch_check_status("https://gitlab.com/acme/repo/-/merge_requests/42")

    assert run.await_count == 2
    assert "merge_requests/42/pipelines?per_page=1" in run.await_args_list[1].args[2]
    assert [call.kwargs.get("host") for call in run.await_args_list] == ["gitlab.com"] * 2
    assert status == {"state": "draft", "ci": "failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "draft", "expected"),
    [
        ("opened", True, "draft"),
        ("opened", False, "open"),
        # Draft must not outrank a terminal state: GitLab keeps `draft` set on a
        # merge request closed while still a draft.
        ("closed", True, "closed"),
        ("merged", False, "merged"),
        ("locked", False, None),
    ],
)
async def test_gitlab_chip_state_precedence(monkeypatch, state, draft, expected) -> None:
    run = AsyncMock(return_value={"state": state, "draft": draft, "head_pipeline": {}})
    monkeypatch.setattr(source, "_run_json", run)

    status = await source._fetch_check_status("https://gitlab.com/acme/repo/-/merge_requests/42")

    assert (status or {}).get("state") == expected


@pytest.mark.asyncio
async def test_gitlab_allowlist_never_reads_config_on_the_event_loop(monkeypatch) -> None:
    """KiroCrewConfig.load() stats/reads/parses config files, so it must only run
    in a worker thread; the sync accessor every URL parse uses is cache-only."""
    calls: list[str] = []

    def fake_load() -> tuple[frozenset[str], frozenset[str], bool]:
        calls.append("load")
        return frozenset({"gitlab.acme.internal"}), frozenset(), True

    monkeypatch.setattr(source, "_load_source_link_settings", fake_load)
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(source, "_gitlab_hosts_loaded_at", 0.0)
    to_thread_calls: list[object] = []
    real_to_thread = source.asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        to_thread_calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(source.asyncio, "to_thread", spy_to_thread)

    # Cold cache fails closed rather than blocking to load.
    assert source._allowed_gitlab_hosts() == frozenset()
    assert calls == []

    assert await source.ensure_gitlab_hosts_loaded() == frozenset({"gitlab.acme.internal"})
    assert to_thread_calls == [fake_load]
    assert source._allowed_gitlab_hosts() == frozenset({"gitlab.acme.internal"})

    # Within the TTL a second call is a pure cache read - no further thread hop.
    assert await source.ensure_gitlab_hosts_loaded() == frozenset({"gitlab.acme.internal"})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_async_entry_points_refresh_the_allowlist_before_parsing(monkeypatch) -> None:
    order: list[str] = []

    async def fake_ensure() -> frozenset[str]:
        order.append("ensure")
        return frozenset()

    def fake_parse(url: str):
        order.append("parse")
        raise ValueError("stop here")

    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", fake_ensure)
    monkeypatch.setattr(source, "parse_source_url", fake_parse)

    for coro in (
        source.fetch_pull_request("https://x/y"),
        source.fetch_pull_request_checks("https://x/y"),
        source.resolve_pull_request_thread("https://x/y", "abc"),
        source._fetch_check_status("https://x/y"),
        # Mutation entry points: a cold snapshot here would reject an authorized
        # self-managed URL as an unsupported host (400) before any dispatch.
        source.enable_pull_request_auto_merge("https://x/y"),
        source.mark_pull_request_ready("https://x/y"),
    ):
        with pytest.raises(ValueError):
            await coro

    assert order == ["ensure", "parse"] * 6


def test_self_hosted_gitlab_accepts_absolute_fqdn_url(monkeypatch) -> None:
    """A trailing dot is the absolute-FQDN form of the same host.

    The allowlist is dot-normalized by the config loader, so the URL side must
    normalize too or the two can never agree and the link is rejected. Existing
    tests cover dotted config ENTRIES; this covers a dotted URL.
    """
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    ref = source.parse_source_url("https://gitlab.acme.internal./team/api/-/merge_requests/7")
    assert ref.host == "gitlab.acme.internal"
    assert ref.url == "https://gitlab.acme.internal/team/api/-/merge_requests/7"


def test_self_hosted_gitlab_treats_default_https_port_as_absent(monkeypatch) -> None:
    """The browser URL API drops :443, so the backend must too or an explicit
    :443 URL builds a Changes tab the backend then refuses to load."""
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    ref = source.parse_source_url("https://gitlab.acme.internal:443/a/b/-/merge_requests/1")
    assert ref.host == "gitlab.acme.internal"
    assert ref.url == "https://gitlab.acme.internal/a/b/-/merge_requests/1"


def test_manual_pipeline_fallback_is_pending_not_skipped() -> None:
    """A pipeline standing in for its jobs keeps PIPELINE-level semantics.

    The synthesized record carries no ``allow_failure``, so the job-level reading
    would call a `manual` pipeline skipped and the frontend would roll that up as
    passed -- contradicting the chip, which reports a blocked pipeline as running.
    """
    manual = source._gitlab_pipeline_as_check({"status": "manual", "web_url": "https://x/1"})
    assert manual["bucket"] == "pending"
    # A terminal pipeline is unaffected.
    assert source._gitlab_pipeline_as_check({"status": "success"})["bucket"] == "passed"
    assert source._gitlab_pipeline_as_check({"status": "skipped"})["bucket"] == "skipped"


def test_job_level_manual_stays_skipped_while_pipeline_level_blocks() -> None:
    """One optional manual job among finished ones is not a blocked build, but a
    pipeline whose own status is `manual` is waiting on a required job."""
    assert source._gitlab_check({"name": "deploy", "status": "manual"})["bucket"] == "skipped"
    assert source._gitlab_aggregate_ci("manual") == "running"


def test_required_manual_job_is_pending_not_skipped() -> None:
    """allow_failure=False makes a manual job a gate: rolling it up as skipped
    would let the Checks tab read green while the build waits on a human."""
    required = source._gitlab_check(
        {"name": "deploy", "status": "manual", "allow_failure": False}
    )
    optional = source._gitlab_check({"name": "deploy", "status": "manual", "allow_failure": True})
    assert required["bucket"] == "pending"
    assert optional["bucket"] == "skipped"


@pytest.mark.asyncio
async def test_github_payload_identity_ignores_provider_supplied_url(monkeypatch) -> None:
    """Same hardening as the GitLab case, asserted on the GitHub path.

    Both providers echo an identity (`url`/`number`) that the browser later
    submits back for refresh and thread resolution. Reverting either provider's
    pin lets a mismatched payload steer an owner-authenticated call at a
    different pull request, so each needs its own regression test.
    """

    async def fake_run(*argv: str, **kwargs):
        if "--json" in argv:
            return {
                "number": 999,
                "url": "https://github.com/victim/repo/pull/1",
                "title": "t",
                "state": "OPEN",
            }
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://github.com/acme/widgets/pull/12")

    data = await source._fetch_github(ref)

    assert data["url"] == "https://github.com/acme/widgets/pull/12"
    assert data["number"] == 12


@pytest.mark.asyncio
async def test_payload_identity_ignores_provider_supplied_url(monkeypatch) -> None:
    """The browser submits the payload url back for refresh/resolve, so a
    compromised or hostile instance echoing someone else's web_url must not be
    able to steer an owner-authenticated call at an unrelated merge request."""
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )

    async def fake_run(*argv: str, **kwargs):
        if argv[-1].endswith("merge_requests/7") or kwargs.get("host"):
            if "merge_requests/7" in " ".join(argv):
                return {
                    "iid": 999,
                    "title": "t",
                    "state": "opened",
                    "web_url": "https://gitlab.com/victim/repo/-/merge_requests/1",
                }
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://gitlab.acme.internal/team/api/-/merge_requests/7")

    data = await source._fetch_gitlab(ref)

    assert data["url"] == "https://gitlab.acme.internal/team/api/-/merge_requests/7"
    assert data["number"] == 7


@pytest.mark.asyncio
async def test_glab_does_not_forward_ambient_token_to_a_self_managed_host(monkeypatch) -> None:
    """GITLAB_TOKEN has no host binding, so forwarding it while GITLAB_HOST points
    at a self-managed instance would hand a gitlab.com PAT to that server."""

    class FakeProcess:
        returncode = 0

    sandbox = MagicMock(return_value=(["/usr/bin/sandbox-launcher", "/usr/bin/glab"], {"SAFE": "1"}, None))
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-" + "a" * 20)
    monkeypatch.setenv("GLAB_CONFIG_DIR", "/home/user/.config/glab-cli")
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/glab")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))

    await source._run_json("glab", "api", "projects/1", host="gitlab.acme.internal")
    env = sandbox.call_args.kwargs["env"]
    assert "GITLAB_TOKEN" not in env
    # The per-host credential store is still reachable -- that is how a
    # self-managed instance is expected to authenticate.
    assert env["GLAB_CONFIG_DIR"] == "/home/user/.config/glab-cli"
    assert env["GITLAB_HOST"] == "gitlab.acme.internal"

    # gitlab.com keeps the ambient token: it is the host the token belongs to.
    sandbox.reset_mock()
    await source._run_json("glab", "api", "projects/1", host="gitlab.com")
    assert sandbox.call_args.kwargs["env"]["GITLAB_TOKEN"].startswith("glpat-")


@pytest.mark.asyncio
async def test_gitlab_mutations_forward_host_to_the_run_json_guard(monkeypatch) -> None:
    """Regression: the auto-merge and mark-ready mutation call sites must thread
    ``host=ref.host`` into :func:`_run_json`.

    ``host`` is REQUIRED for glab, so a call site that drops it is refused at the
    guard (``host_not_specified``) and the mutation dies for every GitLab host,
    including gitlab.com. Every existing mutation test monkeypatches ``_run_json``
    wholesale, which makes exactly that omission invisible. This drives both entry
    points through the REAL ``_run_json`` -- mocking only the pre-mutation read and
    the sandbox -- and asserts the self-managed host reaches ``GITLAB_HOST``. Drop
    the ``host=ref.host`` argument at either site and the guard raises before the
    sandbox is reached, failing this test.
    """

    class FakeProcess:
        returncode = 0

    sandbox = MagicMock(return_value=(["/usr/bin/sandbox-launcher", "/usr/bin/glab"], {"SAFE": "1"}, None))
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/glab")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())

    url = "https://gitlab.acme.internal/team/api/-/merge_requests/7"

    # enable_pull_request_auto_merge: a running pipeline genuinely gates the
    # merge, so no immediate-merge confirmation is required and the PUT dispatches.
    async def _read_armable(_ref):
        return {"merge_when_pipeline_succeeds": False, "head_pipeline": {"status": "running"}}

    monkeypatch.setattr(source, "_gitlab_merge_request", _read_armable)
    sandbox.reset_mock()
    assert await source.enable_pull_request_auto_merge(url) == "pipeline"
    assert sandbox.call_args.kwargs["env"]["GITLAB_HOST"] == "gitlab.acme.internal"

    # mark_pull_request_ready: the MR is a draft, so the set-draft mutation runs.
    async def _read_draft(_ref):
        return {"draft": True}

    monkeypatch.setattr(source, "_gitlab_merge_request", _read_draft)
    sandbox.reset_mock()
    await source.mark_pull_request_ready(url)
    assert sandbox.call_args.kwargs["env"]["GITLAB_HOST"] == "gitlab.acme.internal"


@pytest.mark.asyncio
async def test_gitlab_merge_request_read_forwards_host(monkeypatch) -> None:
    """The pre-mutation read is the third glab call site.

    Kept separate rather than folded into the dispatch test above: that test has
    to stub ``_gitlab_merge_request`` to reach the dispatches, and undoing the
    stub mid-test would also drop this module's autouse fixture patches, running
    the real ``_run_json`` without the suite's normal isolation and audit stub.
    """

    class FakeProcess:
        returncode = 0

    sandbox = MagicMock(return_value=(["/usr/bin/sandbox-launcher", "/usr/bin/glab"], {"SAFE": "1"}, None))
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/glab")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(
        source, "_collect_process_output", AsyncMock(return_value=(b'{"draft": false}', b""))
    )

    ref = source.parse_source_url("https://gitlab.acme.internal/team/api/-/merge_requests/7")
    assert await source._gitlab_merge_request(ref) == {"draft": False}
    assert sandbox.call_args.kwargs["env"]["GITLAB_HOST"] == "gitlab.acme.internal"


@pytest.mark.asyncio
async def test_concurrent_allowlist_refresh_cannot_restore_a_revoked_host(monkeypatch) -> None:
    """Without serialization, a loader holding the PRE-revocation config could
    install its snapshot after the post-revocation one and re-admit the removed
    host for another full TTL."""
    started = source.asyncio.Event()
    release = source.asyncio.Event()
    loads = {"n": 0}

    def slow_stale_load() -> tuple[frozenset[str], frozenset[str], bool]:
        loads["n"] += 1
        # asyncio.Event is not thread-safe: this runs in a worker thread, so the
        # set() must be marshalled back onto the loop.
        loop.call_soon_threadsafe(started.set)
        # Block inside the worker thread so a second waiter queues on the lock.
        source.asyncio.run_coroutine_threadsafe(_noop(), loop).result(timeout=5)
        return frozenset({"gitlab.acme.internal"}), frozenset(), True

    async def _noop() -> None:
        await release.wait()

    loop = source.asyncio.get_running_loop()
    monkeypatch.setattr(source, "_load_source_link_settings", slow_stale_load)
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(source, "_gitlab_hosts_loaded_at", 0.0)
    monkeypatch.setattr(source, "_gitlab_hosts_lock", source.asyncio.Lock())

    first = loop.create_task(source.ensure_gitlab_hosts_loaded())
    await started.wait()
    second = loop.create_task(source.ensure_gitlab_hosts_loaded())
    await source.asyncio.sleep(0)
    release.set()
    await first
    await second

    # The second caller reused the fresh snapshot instead of running its own load.
    assert loads["n"] == 1


@pytest.mark.asyncio
async def test_allowlist_generation_bumps_only_on_content_change(monkeypatch) -> None:
    """Per-slot sidebar caches key off this, so it must change when (and only
    when) the host set actually changes."""
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(source, "_gitlab_hosts_loaded_at", 0.0)
    monkeypatch.setattr(source, "_gitlab_hosts_generation", 0)

    source._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
    first = source.gitlab_hosts_generation()
    assert first == 1

    source._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
    assert source.gitlab_hosts_generation() == first

    source._publish_provider_hosts(frozenset(), frozenset())
    assert source.gitlab_hosts_generation() == first + 1


def test_self_hosted_gitlab_rejected_when_allowlist_empty(monkeypatch) -> None:
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset())
    with pytest.raises(ValueError, match="dashboard.gitlab_hosts"):
        source.parse_source_url("https://gitlab.acme.internal/team/api/-/merge_requests/7")


def test_self_hosted_gitlab_accepted_when_allowlisted(monkeypatch) -> None:
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"}))
    ref = source.parse_source_url(
        "https://gitlab.acme.internal/team/platform/api/-/merge_requests/7"
    )
    assert ref.provider == "gitlab"
    assert ref.host == "gitlab.acme.internal"
    assert ref.project == "team/platform/api"
    assert ref.repo == "api"
    assert ref.number == 7
    # The normalized URL keeps the self-managed host so cache keys, the panel's
    # external link, and the CLI host pin all agree.
    assert ref.url == "https://gitlab.acme.internal/team/platform/api/-/merge_requests/7"


@pytest.mark.parametrize(
    ("allowlist", "url", "allowed"),
    [
        # An entry without a port does not authorize an arbitrary port.
        ({"gitlab.acme.internal"}, "https://gitlab.acme.internal:8443/a/b/-/merge_requests/1", False),
        (
            {"gitlab.acme.internal:8443"},
            "https://gitlab.acme.internal:8443/a/b/-/merge_requests/1",
            True,
        ),
        # Exact match only: no suffix or lookalike widening.
        ({"gitlab.acme.internal"}, "https://evil-gitlab.acme.internal/a/b/-/merge_requests/1", False),
        ({"gitlab.acme.internal"}, "https://gitlab.acme.internal.evil.test/a/b/-/merge_requests/1", False),
        ({"acme.internal"}, "https://gitlab.acme.internal/a/b/-/merge_requests/1", False),
        # www is not stripped for a self-managed host, unlike gitlab.com.
        ({"gitlab.acme.internal"}, "https://www.gitlab.acme.internal/a/b/-/merge_requests/1", False),
    ],
)
def test_self_hosted_gitlab_matches_host_exactly(monkeypatch, allowlist, url, allowed) -> None:
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset(allowlist))
    if allowed:
        assert source.parse_source_url(url).provider == "gitlab"
    else:
        with pytest.raises(ValueError):
            source.parse_source_url(url)


def test_self_hosted_gitlab_still_requires_https_and_mr_path(monkeypatch) -> None:
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"}))
    with pytest.raises(ValueError, match="HTTPS"):
        source.parse_source_url("http://gitlab.acme.internal/a/b/-/merge_requests/1")
    with pytest.raises(ValueError, match="HTTPS"):
        source.parse_source_url("https://user:pw@gitlab.acme.internal/a/b/-/merge_requests/1")
    # An issue path is now a supported shape (kind="issue"), so the rejection
    # case is a path that is neither: a self-managed host does not widen which
    # OBJECTS are readable.
    with pytest.raises(ValueError, match="Expected a GitLab URL"):
        source.parse_source_url("https://gitlab.acme.internal/a/b/-/tree/main")
    with pytest.raises(ValueError, match="Expected a GitLab URL"):
        source.parse_source_url("https://gitlab.acme.internal/a/b/-/snippets/1")


@pytest.mark.asyncio
async def test_run_json_pins_glab_to_the_allowlisted_host(monkeypatch) -> None:
    class FakeProcess:
        returncode = 0

    sandbox = MagicMock(return_value=(["/usr/bin/sandbox-launcher", "/usr/bin/glab"], {"SAFE": "1"}, None))
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"}))
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/glab")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess()))
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))

    assert await source._run_json("glab", "api", "projects/1", host="gitlab.acme.internal") == {}
    assert sandbox.call_args.kwargs["env"]["GITLAB_HOST"] == "gitlab.acme.internal"

    sandbox.reset_mock()
    assert await source._run_json("glab", "api", "projects/1", host="gitlab.com") == {}
    assert sandbox.call_args.kwargs["env"]["GITLAB_HOST"] == "gitlab.com"


@pytest.mark.asyncio
async def test_run_json_refuses_glab_without_an_explicit_host(monkeypatch) -> None:
    """A call site that forgets `host` must fail loudly, not silently target
    gitlab.com: an allowlisted self-managed MR could otherwise be read -- or
    mutated -- on the PUBLIC instance at the same project/IID."""
    resolver = MagicMock()
    sandbox = MagicMock()
    monkeypatch.setattr(source, "_resolve_provider_executable", resolver)
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)

    with pytest.raises(source.SourceProviderError, match="host is required"):
        await source._run_json("glab", "api", "projects/1")

    resolver.assert_not_called()
    sandbox.assert_not_called()


@pytest.mark.asyncio
async def test_run_json_refuses_glab_host_outside_allowlist(monkeypatch) -> None:
    """Defense in depth: a caller cannot reach an unauthorized instance even if a
    future code path skips parse_source_url."""
    resolver = MagicMock()
    sandbox = MagicMock()
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"}))
    monkeypatch.setattr(source, "_resolve_provider_executable", resolver)
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)

    with pytest.raises(source.SourceProviderError, match="not allowlisted"):
        await source._run_json("glab", "api", "projects/1", host="gitlab.evil.test")

    resolver.assert_not_called()
    sandbox.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_gitlab_threads_self_hosted_host_through_every_call(monkeypatch) -> None:
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"}))
    hosts: list[str] = []

    async def fake_run(*_argv: str, **kwargs):
        hosts.append(kwargs.get("host", ""))
        command = " ".join(_argv)
        if command.endswith("merge_requests/7"):
            return {"iid": 7, "title": "t", "state": "opened", "web_url": "https://x/1"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://gitlab.acme.internal/team/api/-/merge_requests/7")

    await source._fetch_gitlab(ref)

    assert hosts and set(hosts) == {"gitlab.acme.internal"}


@pytest.mark.asyncio
async def test_fetch_gitlab_flattens_discussions_with_resolve_fields(monkeypatch) -> None:
    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            return {
                "iid": 42,
                "title": "Fix pipeline",
                "description": "",
                "state": "opened",
                "web_url": "https://gitlab.com/acme/repo/-/merge_requests/42",
                "source_branch": "fix",
                "target_branch": "main",
                "sha": "def456",
                "changes_count": "1",
                "author": {"username": "dev"},
            }
        if "/discussions?" in command:
            return [
                {
                    "id": "a1b2c3",
                    "notes": [
                        {
                            "id": 7,
                            "author": {"username": "reviewer"},
                            "body": "Please fix",
                            "resolvable": True,
                            "resolved": False,
                        },
                        {"id": 8, "system": True, "body": "changed the description"},
                    ],
                }
            ]
        if "/commits?" in command or "/pipelines?" in command:
            return []
        if "/changes" in command:
            return {"changes": []}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    assert data["provider"] == "gitlab"
    assert data["partialSections"] == ["files"]
    assert len(data["comments"]) == 1  # system note filtered out
    comment = data["comments"][0]
    assert comment["threadId"] == "a1b2c3"
    assert comment["resolvable"] is True
    assert comment["resolved"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", ["", "bad id with spaces", "x" * 129, "semi;colon"])
async def test_resolve_rejects_invalid_thread_ids(thread_id: str) -> None:
    with pytest.raises(ValueError):
        await source.resolve_pull_request_thread("https://github.com/acme/repo/pull/12", thread_id)


@pytest.mark.asyncio
async def test_resolve_github_dispatches_graphql_mutation_and_busts_cache(monkeypatch) -> None:
    membership = {
        "data": {
            "repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_thread1"}]}}}
        }
    }
    run = AsyncMock(side_effect=[membership, {}])
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE[url] = (0.0, 21, {"provider": "github"})

    await source.resolve_pull_request_thread(url, "PRRT_thread1")

    assert run.await_count == 2
    membership_argv = run.await_args_list[0].args
    assert "owner=acme" in membership_argv
    assert "repo=repo" in membership_argv
    assert "number=12" in membership_argv
    mutation_argv = run.await_args_list[1].args
    assert any("resolveReviewThread" in part for part in mutation_argv)
    assert "threadId=PRRT_thread1" in mutation_argv
    assert url not in source._CACHE


@pytest.mark.asyncio
async def test_resolve_cancellation_after_dispatch_keeps_cache_invalidated(monkeypatch) -> None:
    membership = {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_1"}]}}}}
    }
    run = AsyncMock(side_effect=[membership, source.asyncio.CancelledError()])
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE[url] = (0.0, 21, {"provider": "github", "stale": True})
    release = source.asyncio.Event()

    async def stale_fetch():
        await release.wait()
        return {"provider": "github", "stale": True}

    stale_task = source.asyncio.create_task(stale_fetch())
    source._FULL_FETCH_INFLIGHT[url] = stale_task
    source._FULL_FETCH_TASKS[url] = {stale_task}

    try:
        with pytest.raises(source.asyncio.CancelledError):
            await source.resolve_pull_request_thread(url, "PRRT_1")

        assert url not in source._CACHE
        assert url not in source._FULL_FETCH_INFLIGHT
        assert source._FULL_FETCH_GENERATIONS[url] == 1
        assert stale_task in source._FULL_FETCH_TASKS[url]
    finally:
        release.set()
        await stale_task
        source._CACHE.clear()
        source._FULL_FETCH_INFLIGHT.clear()
        source._FULL_FETCH_TASKS.clear()
        source._FULL_FETCH_GENERATIONS.clear()


@pytest.mark.asyncio
async def test_resolve_github_rejects_thread_from_another_pull_request(monkeypatch) -> None:
    membership = {
        "data": {
            "repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_other"}]}}}
        }
    }
    run = AsyncMock(return_value=membership)
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match="does not belong"):
        await source.resolve_pull_request_thread(
            "https://github.com/acme/repo/pull/12", "PRRT_thread1"
        )

    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_gitlab_rejects_path_shaped_thread_id() -> None:
    with pytest.raises(ValueError, match="valid thread id"):
        await source.resolve_pull_request_thread(
            "https://gitlab.com/acme/repo/-/merge_requests/42", "../other"
        )


@pytest.mark.asyncio
async def test_resolve_gitlab_dispatches_discussion_put(monkeypatch) -> None:
    run = AsyncMock(return_value={})
    monkeypatch.setattr(source, "_run_json", run)

    await source.resolve_pull_request_thread(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42", "a1b2c3"
    )

    argv = run.call_args.args
    assert argv[0] == "glab"
    assert "PUT" in argv
    assert "projects/acme%2Fplatform%2Fservice/merge_requests/42/discussions/a1b2c3" in argv
    assert "resolved=true" in argv
    # host must be forwarded or _run_json's guard refuses the mutation.
    assert run.call_args.kwargs.get("host") == "gitlab.com"


@pytest.mark.asyncio
async def test_auto_merge_github_picks_allowed_method_and_busts_cache(monkeypatch) -> None:
    node = {
        "data": {
            "repository": {
                "squashMergeAllowed": False,
                "mergeCommitAllowed": True,
                "rebaseMergeAllowed": True,
                "pullRequest": {"id": "PR_node1", "isDraft": False, "state": "OPEN"},
            }
        }
    }
    run = AsyncMock(side_effect=[node, {}])
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE[url] = (0.0, 21, {"provider": "github"})

    try:
        assert await source.enable_pull_request_auto_merge(url) == "merge"
    finally:
        source._CACHE.clear()

    mutation_argv = run.await_args_list[1].args
    assert any("enablePullRequestAutoMerge" in part for part in mutation_argv)
    assert "pullRequestId=PR_node1" in mutation_argv
    assert "mergeMethod=MERGE" in mutation_argv
    assert url not in source._CACHE


@pytest.mark.asyncio
async def test_auto_merge_github_refuses_draft_before_dispatch(monkeypatch) -> None:
    node = {
        "data": {
            "repository": {
                "squashMergeAllowed": True,
                "pullRequest": {"id": "PR_node1", "isDraft": True, "state": "OPEN"},
            }
        }
    }
    run = AsyncMock(return_value=node)
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match="draft"):
        await source.enable_pull_request_auto_merge("https://github.com/acme/repo/pull/12")

    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_merge_github_refuses_when_already_armed(monkeypatch) -> None:
    node = {
        "data": {
            "repository": {
                "squashMergeAllowed": True,
                "pullRequest": {
                    "id": "PR_node1",
                    "isDraft": False,
                    "autoMergeRequest": {"enabledAt": "2026-07-13T10:00:00Z"},
                },
            }
        }
    }
    monkeypatch.setattr(source, "_run_json", AsyncMock(return_value=node))

    with pytest.raises(ValueError, match="already enabled"):
        await source.enable_pull_request_auto_merge("https://github.com/acme/repo/pull/12")


@pytest.mark.asyncio
async def test_auto_merge_github_rejects_unusable_node_id(monkeypatch) -> None:
    node = {"data": {"repository": {"squashMergeAllowed": True, "pullRequest": {"id": "bad id"}}}}
    monkeypatch.setattr(source, "_run_json", AsyncMock(return_value=node))

    with pytest.raises(source.SourceProviderError, match="usable pull-request id"):
        await source.enable_pull_request_auto_merge("https://github.com/acme/repo/pull/12")


@pytest.mark.asyncio
async def test_auto_merge_github_refuses_when_no_method_allowed(monkeypatch) -> None:
    node = {
        "data": {
            "repository": {
                "squashMergeAllowed": False,
                "mergeCommitAllowed": False,
                "rebaseMergeAllowed": False,
                "pullRequest": {"id": "PR_node1", "isDraft": False},
            }
        }
    }
    run = AsyncMock(return_value=node)
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match="merge method"):
        await source.enable_pull_request_auto_merge("https://github.com/acme/repo/pull/12")

    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_merge_gitlab_dispatches_merge_when_pipeline_succeeds(monkeypatch) -> None:
    run = AsyncMock(side_effect=[{"head_pipeline": {"status": "running"}}, {}])
    monkeypatch.setattr(source, "_run_json", run)

    method = await source.enable_pull_request_auto_merge(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42"
    )

    assert method == "pipeline"
    argv = run.await_args_list[1].args
    assert argv[0] == "glab"
    assert "PUT" in argv
    assert "projects/acme%2Fplatform%2Fservice/merge_requests/42/merge" in argv
    assert "merge_when_pipeline_succeeds=true" in argv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"draft": True, "head_pipeline": {"status": "running"}}, "draft"),
        ({"work_in_progress": True, "head_pipeline": {"status": "running"}}, "draft"),
        (
            {"merge_when_pipeline_succeeds": True, "head_pipeline": {"status": "running"}},
            "already enabled",
        ),
        ({"head_pipeline": {"status": "success"}}, "immediately"),
        ({}, "immediately"),
    ],
)
async def test_auto_merge_gitlab_refuses_inapplicable_requests_before_dispatch(
    monkeypatch, details: dict, expected: str
) -> None:
    run = AsyncMock(return_value=details)
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match=expected):
        await source.enable_pull_request_auto_merge(
            "https://gitlab.com/acme/platform/service/-/merge_requests/42"
        )

    # Only the precondition read happened: nothing was merged.
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_merge_gitlab_merges_without_pipeline_only_when_confirmed(monkeypatch) -> None:
    run = AsyncMock(side_effect=[{"head_pipeline": {"status": "success"}}, {}])
    monkeypatch.setattr(source, "_run_json", run)

    method = await source.enable_pull_request_auto_merge(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42",
        confirm_immediate_merge=True,
    )

    assert method == "pipeline"
    assert "merge_when_pipeline_succeeds=true" in run.await_args_list[1].args


@pytest.mark.asyncio
async def test_auto_merge_gitlab_immediate_refusal_is_answerable(monkeypatch) -> None:
    """The no-pipeline refusal is distinguishable from an ordinary rejection.

    A client can only offer a meaningful acknowledgement if it can tell this
    refusal apart from a permanent one, so it carries its own type.
    """
    monkeypatch.setattr(source, "_run_json", AsyncMock(return_value={}))

    with pytest.raises(source.ConfirmationRequired):
        await source.enable_pull_request_auto_merge(
            "https://gitlab.com/acme/platform/service/-/merge_requests/42"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", ["false", "true", 1, 0, {}, [], None, "yes"])
async def test_auto_merge_handler_rejects_non_boolean_confirmation(
    monkeypatch, confirm: object
) -> None:
    """Only a real JSON boolean counts as consent.

    ``bool()`` coercion would read the string ``"false"`` -- and every other
    truthy value -- as an acknowledgement, so a malformed client would satisfy
    the very guard standing between it and an immediate merge.
    """
    action = AsyncMock(return_value="squash")
    monkeypatch.setattr(source, "enable_pull_request_auto_merge", action)
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/auto-merge",
            json={"url": "https://github.com/acme/repo/pull/12", "confirmImmediateMerge": confirm},
        )
        assert response.status == 400
        assert (await response.json())["error"] == "confirmImmediateMerge must be true or false."

    action.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_merge_handler_marks_confirmation_required_refusals(monkeypatch) -> None:
    """The 400 carries a machine-readable marker, not just prose."""
    monkeypatch.setattr(
        source,
        "enable_pull_request_auto_merge",
        AsyncMock(side_effect=source.ConfirmationRequired("No pipeline is pending.")),
    )
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/auto-merge",
            json={"url": "https://gitlab.com/acme/platform/service/-/merge_requests/42"},
        )
        assert response.status == 400
        assert await response.json() == {
            "error": "No pipeline is pending.",
            "confirmationRequired": True,
        }


@pytest.mark.asyncio
async def test_ordinary_rejections_carry_no_confirmation_marker(monkeypatch) -> None:
    """Only an answerable refusal invites a retry with the acknowledgement."""
    monkeypatch.setattr(
        source,
        "enable_pull_request_auto_merge",
        AsyncMock(side_effect=ValueError("Auto-merge is already enabled.")),
    )
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/auto-merge",
            json={"url": "https://github.com/acme/repo/pull/12"},
        )
        assert response.status == 400
        assert await response.json() == {"error": "Auto-merge is already enabled."}


@pytest.mark.asyncio
async def test_ready_github_dispatches_mutation_and_busts_cache(monkeypatch) -> None:
    node = {"data": {"repository": {"pullRequest": {"id": "PR_node1", "isDraft": True}}}}
    run = AsyncMock(side_effect=[node, {}])
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE[url] = (0.0, 21, {"provider": "github"})

    try:
        await source.mark_pull_request_ready(url)
    finally:
        source._CACHE.clear()

    mutation_argv = run.await_args_list[1].args
    assert any("markPullRequestReadyForReview" in part for part in mutation_argv)
    assert "pullRequestId=PR_node1" in mutation_argv
    assert url not in source._CACHE


@pytest.mark.asyncio
async def test_ready_github_refuses_non_draft_before_dispatch(monkeypatch) -> None:
    node = {"data": {"repository": {"pullRequest": {"id": "PR_node1", "isDraft": False}}}}
    run = AsyncMock(return_value=node)
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match="already ready"):
        await source.mark_pull_request_ready("https://github.com/acme/repo/pull/12")

    run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("details", [{"draft": True}, {"work_in_progress": True}])
async def test_ready_gitlab_uses_set_draft_mutation_not_a_title_rewrite(
    monkeypatch, details: dict
) -> None:
    run = AsyncMock(side_effect=[details, {"data": {"mergeRequestSetDraft": {"errors": []}}}])
    monkeypatch.setattr(source, "_run_json", run)

    await source.mark_pull_request_ready(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42"
    )

    argv = run.await_args_list[1].args
    assert "graphql" in argv
    assert any("mergeRequestSetDraft" in part for part in argv)
    assert "projectPath=acme/platform/service" in argv
    assert "iid=42" in argv
    assert "draft=false" in argv
    # The title is never read back out or written, so a concurrent retitle and a
    # title that merely starts with a draft-like word are both left alone.
    assert not any(str(part).startswith("title=") for part in argv)


@pytest.mark.asyncio
async def test_ready_gitlab_refuses_when_not_draft(monkeypatch) -> None:
    run = AsyncMock(return_value={"title": "Drafting widgets", "draft": False})
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match="already ready"):
        await source.mark_pull_request_ready(
            "https://gitlab.com/acme/platform/service/-/merge_requests/42"
        )

    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_gitlab_raises_when_mutation_reports_errors(monkeypatch) -> None:
    run = AsyncMock(
        side_effect=[
            {"draft": True},
            {"data": {"mergeRequestSetDraft": {"errors": ["Not allowed"]}}},
        ]
    )
    monkeypatch.setattr(source, "_run_json", run)

    # GraphQL reports refusals in the body with HTTP 200, so a rejected mutation
    # must not read as success.
    with pytest.raises(source.SourceProviderError, match="refused"):
        await source.mark_pull_request_ready(
            "https://gitlab.com/acme/platform/service/-/merge_requests/42"
        )


@pytest.mark.asyncio
async def test_mutation_invalidates_chip_status_cache(monkeypatch) -> None:
    # The chips read a separate, shorter-lived cache from the full payload, so a
    # mutation must bust both or the chips keep showing pre-mutation state.
    url = "https://github.com/acme/repo/pull/12"
    source._check_cache[url] = (time.monotonic(), {"state": "draft"})
    try:
        await source._invalidate_pull_request_cache(url)
        assert url not in source._check_cache
    finally:
        source._check_cache.pop(url, None)
        source._check_generations.pop(url, None)


@pytest.mark.asyncio
async def test_inflight_status_refresh_cannot_restore_superseded_state(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/12"
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(_url: str) -> dict[str, str]:
        started.set()
        await release.wait()
        return {"state": "draft"}

    monkeypatch.setattr(source, "_fetch_check_status", fetch)
    source._check_cache[url] = (time.monotonic(), {"state": "draft"})
    try:
        task = asyncio.create_task(source._refresh_check_status(url))
        await started.wait()
        # The mutation lands while the refresh is still in flight.
        await source._invalidate_pull_request_cache(url)
        release.set()
        await task

        assert url not in source._check_cache
    finally:
        source._check_cache.pop(url, None)
        source._check_generations.pop(url, None)
        source._check_inflight.discard(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    ["enable_pull_request_auto_merge", "mark_pull_request_ready"],
)
async def test_mutations_reject_unsupported_urls(action: str) -> None:
    with pytest.raises(ValueError):
        await getattr(source, action)("https://example.com/acme/repo/pull/12")


def _app(
    *,
    user: str = "U_OWNER",
    app_name: object = "",
    owner_id: str = "U_OWNER",
    include_user_claim: bool = True,
    include_app_claim: bool = True,
) -> web.Application:
    @web.middleware
    async def fake_auth(request, handler):
        if include_user_claim:
            request["user"] = user
        if include_app_claim:
            request["app"] = app_name
        return await handler(request)

    app = web.Application(middlewares=[fake_auth])
    state = MagicMock()
    state.owner_id = owner_id
    app["state"] = state
    app.router.add_post("/api/source/pull-request", source.api_pull_request_source)
    app.router.add_post("/api/source/pull-request/checks", source.api_pull_request_checks)
    app.router.add_post("/api/source/pull-request/status", source.api_pull_request_status)
    app.router.add_post("/api/source/pull-request/resolve", source.api_pull_request_resolve)
    app.router.add_post(
        "/api/source/pull-request/unresolve", source.api_pull_request_unresolve)
    app.router.add_post("/api/source/pull-request/reply", source.api_pull_request_reply)
    app.router.add_post(
        "/api/source/pull-request/comment", source.api_pull_request_comment)
    app.router.add_post("/api/source/pull-request/auto-merge", source.api_pull_request_auto_merge)
    app.router.add_post("/api/source/pull-request/ready", source.api_pull_request_ready)
    app.router.add_post("/api/source/issue", source.api_issue_source)
    app.router.add_post("/api/source/contributors", source.api_app_contributors)
    return app


@pytest.mark.asyncio
async def test_local_token_uses_configured_owner_subject(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import core

    generate = MagicMock(return_value="owner-token")
    audit = MagicMock()
    monkeypatch.setattr(core, "generate_token", generate)
    monkeypatch.setattr(core, "_sel", lambda: audit)
    app = web.Application()
    app["local_secret"] = "local-secret"
    state = MagicMock()
    state.owner_id = "U_OWNER"
    app["state"] = state
    app.router.add_get("/api/token/local", core.api_token_local)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/token/local?ttl=15m", headers={"X-Local-Secret": "local-secret"}
        )
        assert response.status == 200
        payload = await response.json()

    assert payload == {"token": "owner-token", "expires_in": 900}
    generate.assert_called_once_with("U_OWNER", ttl_seconds=900, extra=None)


@pytest.mark.asyncio
async def test_local_token_carries_embed_parent_port_claim(monkeypatch) -> None:
    """?embed_parent_port=<port> is baked into the token as a signed claim so the
    embedded remote can authorize that loopback parent origin in frame-ancestors."""
    from kiro_crew.dashboard.handlers import core

    generate = MagicMock(return_value="owner-token")
    monkeypatch.setattr(core, "generate_token", generate)
    monkeypatch.setattr(core, "_sel", lambda: MagicMock())
    app = web.Application()
    app["local_secret"] = "local-secret"
    state = MagicMock()
    state.owner_id = "U_OWNER"
    app["state"] = state
    app.router.add_get("/api/token/local", core.api_token_local)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/token/local?ttl=15m&embed_parent_port=5476",
            headers={"X-Local-Secret": "local-secret"},
        )
        assert response.status == 200

    generate.assert_called_once_with(
        "U_OWNER", ttl_seconds=900, extra={"embed_parent_port": "5476"}
    )


@pytest.mark.asyncio
async def test_local_token_uses_local_owner_subject_without_configured_owner(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import core

    generate = MagicMock(return_value="local-token")
    audit = MagicMock()
    monkeypatch.setattr(core, "generate_token", generate)
    monkeypatch.setattr(core, "_sel", lambda: audit)
    app = web.Application()
    app["local_secret"] = "local-secret"
    state = MagicMock()
    state.owner_id = ""
    app["state"] = state
    app.router.add_get("/api/token/local", core.api_token_local)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/token/local?ttl=15m", headers={"X-Local-Secret": "local-secret"}
        )
        assert response.status == 200
        payload = await response.json()

    assert payload == {"token": "local-token", "expires_in": 900}
    generate.assert_called_once_with("local-app", ttl_seconds=900, extra=None)


@pytest.mark.parametrize("subject", ["local-app", "local-startup"])
@pytest.mark.asyncio
async def test_local_dashboard_subjects_can_read_without_configured_owner(
    monkeypatch, subject
) -> None:
    pull = {"url": "https://github.com/acme/repo/pull/12", "checks": []}
    fetch_pull = AsyncMock(return_value=pull)
    fetch_checks = AsyncMock(return_value=[])
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(source, "fetch_pull_request", fetch_pull)
    monkeypatch.setattr(source, "fetch_pull_request_checks", fetch_checks)
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolve)

    app = _app(user=subject, owner_id="")
    async with TestClient(TestServer(app)) as client:
        detail_response = await client.post("/api/source/pull-request", json={"url": pull["url"]})
        checks_response = await client.post(
            "/api/source/pull-request/checks", json={"url": pull["url"]}
        )
        resolve_response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": pull["url"], "threadId": "PRRT_thread1"},
        )

        assert detail_response.status == 200
        assert await detail_response.json() == pull
        assert checks_response.status == 200
        assert await checks_response.json() == {"checks": []}
        assert resolve_response.status == 403

    fetch_pull.assert_awaited_once_with(pull["url"], refresh=False)
    fetch_checks.assert_awaited_once_with(pull["url"])
    resolve.assert_not_awaited()

    request = _ResolveRequest()
    request.app["state"].owner_id = ""
    request._claims["user"] = subject
    assert source.is_owner_dashboard_request(request)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("app_kwargs", "reason"),
    [
        ({"owner_id": "", "user": "U_OTHER"}, "owner_not_configured"),
        ({"include_app_claim": False}, "app_token_not_allowed"),
        ({"app_name": None}, "app_token_not_allowed"),
        ({"app_name": "app-X"}, "app_token_not_allowed"),
        ({"user": "U_OTHER"}, "non_owner"),
        ({"user": ""}, "non_owner"),
        ({"include_user_claim": False}, "non_owner"),
    ],
)
@pytest.mark.parametrize(
    ("endpoint", "fetch_name", "operation"),
    [
        ("/api/source/pull-request", "fetch_pull_request", "source.pull_request.read"),
        (
            "/api/source/pull-request/checks",
            "fetch_pull_request_checks",
            "source.pull_request.checks",
        ),
    ],
)
@pytest.mark.asyncio
async def test_read_handlers_require_explicit_owner_dashboard_claims(
    monkeypatch,
    _mock_source_sel,
    app_kwargs,
    reason,
    endpoint,
    fetch_name,
    operation,
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(source, fetch_name, fetch)
    secret = "ghp_" + "a" * 36
    raw_url = f"https://github.com/acme/repo/pull/1?token={secret}"

    async with TestClient(TestServer(_app(**app_kwargs))) as client:
        response = await client.post(endpoint, json={"url": raw_url})
        payload = await response.json()

    assert response.status == 403
    assert payload == {"error": "forbidden"}
    fetch.assert_not_awaited()
    call = _mock_source_sel.log_api_access.call_args
    assert call.kwargs["operation"] == operation
    assert call.kwargs["outcome"] == "denied"
    assert call.kwargs["error"] == reason
    assert raw_url not in str(call)
    assert secret not in str(call)


@pytest.mark.asyncio
async def test_read_handler_allows_local_token_when_no_owner_configured(
    monkeypatch, _mock_source_sel
) -> None:
    """Local single-user install (no owner): the local dashboard token
    (subject ``local-app``, empty app claim) may use the credential-backed
    provider so viewing a PR diff does not require Slack/owner setup."""
    fetch = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(source, "fetch_pull_request", fetch)
    url = "https://github.com/acme/repo/pull/1"

    async with TestClient(TestServer(_app(owner_id="", user="local-app", app_name=""))) as client:
        response = await client.post("/api/source/pull-request", json={"url": url})
        assert response.status == 200
        assert (await response.json()) == {"ok": True}

    fetch.assert_awaited_once_with(url, refresh=False)


@pytest.mark.asyncio
async def test_read_handler_denies_non_local_subject_when_no_owner(
    monkeypatch, _mock_source_sel
) -> None:
    """No owner + a non ``local-app`` subject (e.g. a stale owner-minted token)
    still fails closed — the fallback is scoped to the genuine local token."""
    fetch = AsyncMock()
    monkeypatch.setattr(source, "fetch_pull_request", fetch)

    async with TestClient(TestServer(_app(owner_id="", user="U_OWNER", app_name=""))) as client:
        response = await client.post(
            "/api/source/pull-request", json={"url": "https://github.com/acme/repo/pull/1"}
        )
        assert response.status == 403
        assert (await response.json()) == {"error": "forbidden"}

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_handler_denies_local_token_when_no_owner(
    monkeypatch, _mock_source_sel
) -> None:
    """The local no-owner fallback is scoped to reads: the resolve *mutation*
    stays owner-only, so a local-app token with no owner still fails closed —
    but the refusal names the remedy with a machine-readable code, because this
    caller class saw live buttons whose reads already succeeded."""
    resolve = AsyncMock()
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolve)

    async with TestClient(TestServer(_app(owner_id="", user="local-app", app_name=""))) as client:
        response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/1", "threadId": "PRRT_1"},
        )
        assert response.status == 403
        body = await response.json()
        assert body["code"] == source.OWNER_NOT_CONFIGURED_CODE
        assert "Owner Slack member ID" in body["error"]

    resolve.assert_not_awaited()


@pytest.mark.parametrize(
    ("handler", "fetch_name", "operation"),
    [
        (
            source.api_pull_request_source,
            "fetch_pull_request",
            "source.pull_request.read",
        ),
        (
            source.api_pull_request_checks,
            "fetch_pull_request_checks",
            "source.pull_request.checks",
        ),
    ],
)
@pytest.mark.asyncio
async def test_read_handlers_audit_cancellation_while_reading_body(
    monkeypatch, handler, fetch_name, operation
) -> None:
    audit = MagicMock()
    fetch = AsyncMock()
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, fetch_name, fetch)
    request = _ResolveRequest(json_error=source.asyncio.CancelledError())

    with pytest.raises(source.asyncio.CancelledError):
        await handler(request)

    fetch.assert_not_awaited()
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation=operation,
        outcome="failed",
        source="dashboard",
        error="request_cancelled",
    )


@pytest.mark.parametrize(
    ("handler", "fetch_name", "operation"),
    [
        (
            source.api_pull_request_source,
            "fetch_pull_request",
            "source.pull_request.read",
        ),
        (
            source.api_pull_request_checks,
            "fetch_pull_request_checks",
            "source.pull_request.checks",
        ),
    ],
)
@pytest.mark.asyncio
async def test_read_handlers_audit_cancellation_during_provider_fetch(
    monkeypatch, handler, fetch_name, operation
) -> None:
    audit = MagicMock()
    fetch = AsyncMock(side_effect=source.asyncio.CancelledError())
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, fetch_name, fetch)
    request = _ResolveRequest({"url": "https://github.com/acme/repo/pull/12"})

    with pytest.raises(source.asyncio.CancelledError):
        await handler(request)

    fetch.assert_awaited_once()
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation=operation,
        outcome="failed",
        source="dashboard",
        error="request_cancelled",
    )


@pytest.mark.parametrize(
    ("handler", "fetch_name"),
    [
        (source.api_pull_request_source, "fetch_pull_request"),
        (source.api_pull_request_checks, "fetch_pull_request_checks"),
    ],
)
@pytest.mark.asyncio
async def test_read_handler_cancellation_survives_source_audit_failure(
    monkeypatch, handler, fetch_name
) -> None:
    audit = MagicMock()
    audit.log_api_access.side_effect = OSError("audit filesystem unavailable")
    fetch = AsyncMock(side_effect=source.asyncio.CancelledError())
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, fetch_name, fetch)
    request = _ResolveRequest({"url": "https://github.com/acme/repo/pull/12"})

    with pytest.raises(source.asyncio.CancelledError):
        await handler(request)

    fetch.assert_awaited_once()
    audit.log_api_access.assert_called_once()


@pytest.mark.asyncio
async def test_owner_denial_survives_source_audit_failure(monkeypatch) -> None:
    audit = MagicMock()
    audit.log_api_access.side_effect = OSError("audit filesystem unavailable")
    fetch = AsyncMock()
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, "fetch_pull_request", fetch)
    request = _ResolveRequest({"url": "https://github.com/acme/repo/pull/12"})
    request._claims["user"] = "U_OTHER"

    response = await source.api_pull_request_source(request)  # type: ignore[arg-type]

    assert response.status == 403
    fetch.assert_not_awaited()
    audit.log_api_access.assert_called_once()


@pytest.mark.asyncio
async def test_handler_returns_validation_error() -> None:
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request", json={"url": "https://example.com/pr/1"}
        )
        assert response.status == 400
        assert "Only github.com" in (await response.json())["error"]


@pytest.mark.asyncio
async def test_handler_returns_provider_error(monkeypatch) -> None:
    monkeypatch.setattr(
        source,
        "fetch_pull_request",
        AsyncMock(side_effect=source.SourceProviderError("gh is not authenticated")),
    )
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request",
            json={"url": "https://github.com/acme/repo/pull/1"},
        )
        assert response.status == 503
        assert (await response.json())["error"] == "gh is not authenticated"


@pytest.mark.asyncio
async def test_checks_handler_returns_normalized_checks(monkeypatch) -> None:
    checks = [
        {
            "name": "test",
            "workflow": "CI",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "bucket": "passed",
            "url": "",
            "startedAt": "",
            "completedAt": "",
        }
    ]
    fetch = AsyncMock(return_value=checks)
    monkeypatch.setattr(source, "fetch_pull_request_checks", fetch)
    url = "https://github.com/acme/repo/pull/12"

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/pull-request/checks", json={"url": url})
        assert response.status == 200
        assert (await response.json()) == {"checks": checks}

    fetch.assert_awaited_once_with(url)


@pytest.mark.asyncio
async def test_checks_handler_returns_validation_error() -> None:
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/checks", json={"url": "https://example.com/pr/1"}
        )
        assert response.status == 400
        assert "Only github.com" in (await response.json())["error"]


@pytest.mark.asyncio
async def test_checks_handler_returns_provider_error(monkeypatch) -> None:
    monkeypatch.setattr(
        source,
        "fetch_pull_request_checks",
        AsyncMock(side_effect=source.SourceProviderError("gh is not authenticated")),
    )
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/checks",
            json={"url": "https://github.com/acme/repo/pull/1"},
        )
        assert response.status == 503
        assert (await response.json())["error"] == "gh is not authenticated"


@pytest.mark.asyncio
async def test_resolve_handler_success(monkeypatch) -> None:
    resolver = AsyncMock(return_value=None)
    audit = MagicMock()
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolver)
    monkeypatch.setattr(source, "_sel", lambda: audit)
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/12", "threadId": "PRRT_thread1"},
        )
        assert response.status == 200
        assert (await response.json())["resolved"] is True
    resolver.assert_awaited_once_with("https://github.com/acme/repo/pull/12", "PRRT_thread1")
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.resolve",
        outcome="completed",
        source="dashboard",
        error="",
    )


@pytest.mark.asyncio
async def test_resolve_handler_audits_provider_failure_without_provider_text(monkeypatch) -> None:
    secret = "ghp_" + "a" * 36
    resolver = AsyncMock(side_effect=source.SourceProviderError(f"provider failed {secret}"))
    audit = MagicMock()
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolver)
    monkeypatch.setattr(source, "_sel", lambda: audit)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/12", "threadId": "PRRT_thread1"},
        )
        assert response.status == 503

    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.resolve",
        outcome="failed",
        source="dashboard",
        error="provider_error",
    )
    assert secret not in str(audit.log_api_access.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "action_name"),
    [
        ("/api/source/pull-request/auto-merge", "enable_pull_request_auto_merge"),
        ("/api/source/pull-request/ready", "mark_pull_request_ready"),
    ],
)
async def test_action_handlers_deny_local_token_when_no_owner(
    monkeypatch, _mock_source_sel, path: str, action_name: str
) -> None:
    """The local no-owner fallback is scoped to reads: these mutations stay
    owner-only, so a local-app token with no owner still fails closed — with
    the coded, actionable body reserved for signed local dashboard sessions."""
    action = AsyncMock()
    monkeypatch.setattr(source, action_name, action)

    async with TestClient(TestServer(_app(owner_id="", user="local-app", app_name=""))) as client:
        response = await client.post(path, json={"url": "https://github.com/acme/repo/pull/1"})
        assert response.status == 403
        body = await response.json()
        assert body["code"] == source.OWNER_NOT_CONFIGURED_CODE
        assert "Owner Slack member ID" in body["error"]

    action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "app_kwargs",
    [
        # A non-local subject must not learn which denial class it hit.
        {"owner_id": "", "user": "U_OTHER", "app_name": ""},
        # Nor an app token, even one carrying a local-shaped subject.
        {"owner_id": "", "user": "local-app", "app_name": "app-X"},
        # Nor an unauthenticated caller with no user claim at all.
        {"owner_id": "", "user": "", "app_name": ""},
    ],
)
async def test_no_owner_mutation_code_reserved_for_signed_local_subjects(
    monkeypatch, _mock_source_sel, app_kwargs: dict
) -> None:
    """The ``owner_not_configured`` discriminator is scoped exactly like
    ``stale_owner_session_response``: every caller that is not a signed
    machine-local dashboard session keeps the generic body."""
    action = AsyncMock()
    monkeypatch.setattr(source, "enable_pull_request_auto_merge", action)

    async with TestClient(TestServer(_app(**app_kwargs))) as client:
        response = await client.post(
            "/api/source/pull-request/auto-merge",
            json={"url": "https://github.com/acme/repo/pull/1"},
        )
        assert response.status == 403
        assert (await response.json()) == {"error": "forbidden"}

    action.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_merge_handler_success_reports_method(monkeypatch) -> None:
    action = AsyncMock(return_value="squash")
    audit = MagicMock()
    monkeypatch.setattr(source, "enable_pull_request_auto_merge", action)
    monkeypatch.setattr(source, "_sel", lambda: audit)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/auto-merge",
            json={"url": "https://github.com/acme/repo/pull/12", "confirmImmediateMerge": True},
        )
        assert response.status == 200
        assert (await response.json()) == {"autoMerge": True, "mergeMethod": "squash"}

    action.assert_awaited_once_with(
        "https://github.com/acme/repo/pull/12", confirm_immediate_merge=True
    )
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.auto_merge",
        outcome="completed",
        source="dashboard",
        error="",
    )


@pytest.mark.asyncio
async def test_ready_handler_success(monkeypatch) -> None:
    action = AsyncMock(return_value=None)
    audit = MagicMock()
    monkeypatch.setattr(source, "mark_pull_request_ready", action)
    monkeypatch.setattr(source, "_sel", lambda: audit)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/ready",
            json={"url": "https://github.com/acme/repo/pull/12"},
        )
        assert response.status == 200
        assert (await response.json()) == {"ready": True}

    action.assert_awaited_once_with("https://github.com/acme/repo/pull/12")
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.ready",
        outcome="completed",
        source="dashboard",
        error="",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "action_name", "operation"),
    [
        (
            "/api/source/pull-request/auto-merge",
            "enable_pull_request_auto_merge",
            "source.pull_request.auto_merge",
        ),
        (
            "/api/source/pull-request/ready",
            "mark_pull_request_ready",
            "source.pull_request.ready",
        ),
    ],
)
async def test_action_handlers_audit_provider_failure_without_provider_text(
    monkeypatch, path: str, action_name: str, operation: str
) -> None:
    secret = "ghp_" + "a" * 36
    audit = MagicMock()
    monkeypatch.setattr(
        source,
        action_name,
        AsyncMock(side_effect=source.SourceProviderError(f"provider failed {secret}")),
    )
    monkeypatch.setattr(source, "_sel", lambda: audit)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(path, json={"url": "https://github.com/acme/repo/pull/12"})
        assert response.status == 503

    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation=operation,
        outcome="failed",
        source="dashboard",
        error="provider_error",
    )
    assert secret not in str(audit.log_api_access.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "action_name"),
    [
        ("/api/source/pull-request/auto-merge", "enable_pull_request_auto_merge"),
        ("/api/source/pull-request/ready", "mark_pull_request_ready"),
    ],
)
async def test_action_handlers_return_400_for_rejected_requests(
    monkeypatch, _mock_source_sel, path: str, action_name: str
) -> None:
    monkeypatch.setattr(source, action_name, AsyncMock(side_effect=ValueError("not allowed here")))

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(path, json={"url": "https://github.com/acme/repo/pull/12"})
        assert response.status == 400
        assert (await response.json()) == {"error": "not allowed here"}


class _ResolveRequest:
    """Minimal authenticated request stub for cancellation audit tests."""

    def __init__(self, body=None, *, json_error=None) -> None:
        state = MagicMock()
        state.owner_id = "U_OWNER"
        self.app = {"state": state}
        self._claims = {"user": "U_OWNER", "app": ""}
        self._body = body
        self._json_error = json_error

    def get(self, key, default=None):
        return self._claims.get(key, default)

    def __contains__(self, key) -> bool:
        return key in self._claims

    def __getitem__(self, key):
        return self._claims[key]

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._body


@pytest.mark.asyncio
async def test_resolve_handler_audits_cancellation_while_reading_body(monkeypatch) -> None:
    audit = MagicMock()
    resolver = AsyncMock()
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolver)
    request = _ResolveRequest(json_error=source.asyncio.CancelledError())

    with pytest.raises(source.asyncio.CancelledError):
        await source.api_pull_request_resolve(request)  # type: ignore[arg-type]

    resolver.assert_not_awaited()
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.resolve",
        outcome="failed",
        source="dashboard",
        error="request_cancelled",
    )


@pytest.mark.asyncio
async def test_resolve_handler_audits_cancellation_during_mutation(monkeypatch) -> None:
    audit = MagicMock()
    resolver = AsyncMock(side_effect=source.asyncio.CancelledError())
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolver)
    request = _ResolveRequest(
        {
            "url": "https://github.com/acme/repo/pull/12",
            "threadId": "PRRT_thread1",
        }
    )

    with pytest.raises(source.asyncio.CancelledError):
        await source.api_pull_request_resolve(request)  # type: ignore[arg-type]

    resolver.assert_awaited_once()
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.resolve",
        outcome="failed",
        source="dashboard",
        error="request_cancelled",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "app_name", "owner_id", "reason"),
    [
        ("U_OTHER", "", "U_OWNER", "non_owner"),
        ("U_OWNER", "source-app", "U_OWNER", "app_token_not_allowed"),
        ("U_OWNER", "", "", "owner_not_configured"),
    ],
)
async def test_resolve_handler_denies_non_owner_app_and_unconfigured_owner(
    monkeypatch, user: str, app_name: str, owner_id: str, reason: str
) -> None:
    resolver = AsyncMock(return_value=None)
    audit = MagicMock()
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolver)
    monkeypatch.setattr(source, "_sel", lambda: audit)

    async with TestClient(
        TestServer(_app(user=user, app_name=app_name, owner_id=owner_id))
    ) as client:
        response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/12", "threadId": "PRRT_thread1"},
        )

    assert response.status == 403
    resolver.assert_not_awaited()
    audit.log_api_access.assert_called_once_with(
        caller=user,
        operation="source.pull_request.resolve",
        outcome="denied",
        source="dashboard",
        error=reason,
    )


@pytest.mark.asyncio
async def test_read_handler_allows_configured_dashboard_owner(monkeypatch) -> None:
    payload = {"provider": "github", "url": "https://github.com/acme/repo/pull/12"}
    fetch = AsyncMock(return_value=payload)
    monkeypatch.setattr(source, "fetch_pull_request", fetch)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/pull-request", json={"url": payload["url"]})
        assert response.status == 200
        assert await response.json() == payload

    fetch.assert_awaited_once_with(payload["url"], refresh=False)


@pytest.mark.asyncio
async def test_resolve_handler_rejects_bad_thread_id(monkeypatch) -> None:
    audit = MagicMock()
    monkeypatch.setattr(source, "_sel", lambda: audit)
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/12", "threadId": "bad id"},
        )
        assert response.status == 400
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.resolve",
        outcome="failed",
        source="dashboard",
        error="invalid_request",
    )


def test_forced_refresh_over_cap_stays_eligible(monkeypatch) -> None:
    """A turn-boundary force deferred by the pending cap must not be locked out.

    Regression for the review finding: recording ``_check_forced_at`` (and
    renewing the cache timestamp) *before* admission meant a URL the pending cap
    rejected was both marked "just forced" (10s floor) and had its TTL renewed —
    so the next turn boundary AND the periodic sweep both skipped it, making the
    chip staler in exactly the contention case force exists for.
    """
    url = "https://github.com/acme/repo/pull/77"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    # Saturate the pending cap with an unrelated in-flight refresh.
    monkeypatch.setattr(source, "_CHECK_PENDING_MAX", 1)
    source._check_inflight.add("https://github.com/acme/repo/pull/1")
    # Seed a known-stale chip entry with an old timestamp.
    old_ts = source.time.monotonic() - 999
    source._check_cache[url] = (old_ts, {"state": "open", "ci": "failed"})

    started = source.request_check_refresh_now([url])

    # Nothing started (cap full) and — crucially — the URL was NOT recorded as
    # forced, so the very next turn boundary can retry it immediately.
    assert url not in started
    assert url not in source._check_forced_at
    # The stale entry's timestamp is untouched, so the periodic sweep still sees
    # it as due rather than freshly refreshed.
    assert source._check_cache[url][0] == old_ts
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()


def test_record_full_payload_preserves_ci_when_checks_partial() -> None:
    """A degraded full fetch must not erase a CI glyph the chip cache knows.

    When a provider's secondary pipelines/jobs call fails, the full payload
    comes back with ``checks: []`` and ``checks`` listed in ``partialSections``.
    The projection then omits ``ci``; without the keep-known-status guard the
    write-through would blank the sidebar's failed-CI glyph everywhere.
    """
    url = "https://github.com/acme/repo/pull/34"
    source._check_cache.clear()
    source._status_delta_sinks.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    source._check_cache[url] = (source.time.monotonic(), {"state": "open", "ci": "failed"})
    try:
        # Same lifecycle, degraded checks section: nothing actually changed once
        # the known CI is carried over, so no spurious delta is emitted.
        source.record_full_payload_status(
            url, {"state": "OPEN", "draft": False, "checks": [], "partialSections": ["checks"]}
        )
        assert source.get_cached_check_status(url) == {"state": "open", "ci": "failed"}
        sink.assert_not_called()

        # Lifecycle moved (open -> merged) but checks are still partial: the new
        # state lands AND the known CI survives, and the delta carries both.
        source.record_full_payload_status(
            url, {"state": "MERGED", "checks": [], "partialSections": ["checks"]}
        )
        assert source.get_cached_check_status(url) == {"state": "merged", "ci": "failed"}
        sink.assert_called_once_with(
            {"url": url, "origin": "detail", "state": "merged", "ci": "failed"}
        )
    finally:
        source.unregister_status_delta_sink(sink)
        source._check_cache.clear()


def test_record_full_payload_keeps_settled_merge_state_when_the_read_is_unsettled() -> None:
    """An unsettled merge read must not erase a settled one.

    Both providers compute mergeability lazily, so a full fetch whose evaluation
    lapsed returns ``unknown`` for a source whose conflict is already known.
    Because every writer replaces the chip entry WHOLESALE, an omitted field is
    destructive rather than neutral: without the keep-known guard this write
    strips the pair, which reads as a changed status and drives the
    chip<->full invalidation loop.
    """
    url = "https://github.com/acme/repo/pull/36"
    source._check_cache.clear()
    source._status_delta_sinks.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    source._check_cache[url] = (
        source.time.monotonic(),
        {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"},
    )
    try:
        source.record_full_payload_status(
            url,
            {"state": "OPEN", "checks": [], "mergeable": "unknown", "mergeStateStatus": "unknown"},
        )

        assert source.get_cached_check_status(url) == {
            "state": "open",
            "mergeable": "conflicting",
            "mergeStateStatus": "dirty",
        }
        # Nothing changed once the known pair is carried over, so the loop that
        # would otherwise refetch the payload never starts.
        sink.assert_not_called()
    finally:
        source.unregister_status_delta_sink(sink)
        source._check_cache.clear()


def test_record_full_payload_lets_a_real_merge_answer_replace_a_settled_one() -> None:
    """Carry-forward fills a gap only — it must never pin a stale verdict."""
    url = "https://github.com/acme/repo/pull/37"
    source._check_cache.clear()
    source._check_cache[url] = (
        source.time.monotonic(),
        {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"},
    )
    try:
        source.record_full_payload_status(
            url,
            {"state": "OPEN", "checks": [], "mergeable": "mergeable", "mergeStateStatus": "clean"},
        )

        assert source.get_cached_check_status(url) == {
            "state": "open",
            "mergeable": "mergeable",
            "mergeStateStatus": "clean",
        }
    finally:
        source._check_cache.clear()


def test_record_full_payload_stops_carrying_merge_state_once_the_source_closes() -> None:
    """A merged/closed source stops being asked about mergeability at all.

    Carrying the pair forward there would pin it permanently, because no later
    read can ever supply a real answer to replace it.
    """
    url = "https://github.com/acme/repo/pull/38"
    source._check_cache.clear()
    source._check_cache[url] = (
        source.time.monotonic(),
        {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"},
    )
    try:
        source.record_full_payload_status(url, {"state": "MERGED", "checks": []})

        assert source.get_cached_check_status(url) == {"state": "merged"}
    finally:
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_chip_refresh_keeps_settled_merge_state_and_starts_no_invalidation_loop(
    monkeypatch,
) -> None:
    """The chip refresh path needs the same keep-known rule as the full writer.

    A settled conflict followed by an unsettled poll is the exact sequence that
    made the pair vanish from the owner-gated sidebar payload (which spreads the
    entry whole) and judged itself "changed", spinning the invalidation loop.
    """
    url = "https://github.com/acme/repo/pull/39"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._status_delta_sinks.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    source._check_cache[url] = (
        source.time.monotonic(),
        {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"},
    )
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"state": "open"}),
    )
    try:
        await source._refresh_check_status(url)

        assert source.get_cached_check_status(url) == {
            "state": "open",
            "mergeable": "conflicting",
            "mergeStateStatus": "dirty",
        }
        sink.assert_not_called()
    finally:
        source.unregister_status_delta_sink(sink)
        source._check_cache.clear()
        source._check_inflight.clear()


def test_record_full_payload_clears_ci_when_checks_genuinely_empty() -> None:
    """The guard must be scoped to PARTIAL checks, not merely-empty ones.

    A PR with no CI configured returns ``checks: []`` and NO ``partialSections``
    entry for checks. That is authoritative "there is no CI", so a previously
    known glyph should clear rather than linger forever.
    """
    url = "https://github.com/acme/repo/pull/35"
    source._check_cache.clear()
    source._status_delta_sinks.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    source._check_cache[url] = (source.time.monotonic(), {"state": "open", "ci": "passed"})
    try:
        source.record_full_payload_status(url, {"state": "OPEN", "checks": []})
        assert source.get_cached_check_status(url) == {"state": "open"}
        sink.assert_called_once_with({"url": url, "origin": "detail", "state": "open"})
    finally:
        source.unregister_status_delta_sink(sink)
        source._check_cache.clear()


# --- Issues -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_issue_cache():
    source._ISSUE_CACHE.clear()
    source._ISSUE_FETCH_INFLIGHT.clear()
    source._ISSUE_FETCH_TASKS.clear()
    yield
    source._ISSUE_CACHE.clear()
    source._ISSUE_FETCH_INFLIGHT.clear()
    source._ISSUE_FETCH_TASKS.clear()


def test_parse_github_issue_url() -> None:
    ref = source.parse_source_url("https://github.com/kirodotdev/KiroCrew/issues/58#issue-1")
    assert ref.provider == "github"
    assert ref.owner == "kirodotdev"
    assert ref.repo == "KiroCrew"
    assert ref.number == 58
    assert ref.kind == "issue"
    assert ref.url == "https://github.com/kirodotdev/KiroCrew/issues/58"


def test_parse_github_pull_request_still_reports_change_kind() -> None:
    ref = source.parse_source_url("https://github.com/acme/repo/pull/12")
    assert ref.kind == "change"


def test_parse_gitlab_issue_url_with_nested_group() -> None:
    ref = source.parse_source_url("https://gitlab.com/acme/platform/service/-/issues/42")
    assert ref.provider == "gitlab"
    assert ref.project == "acme/platform/service"
    assert ref.repo == "service"
    assert ref.number == 42
    assert ref.kind == "issue"
    assert ref.url == "https://gitlab.com/acme/platform/service/-/issues/42"


def test_parse_gitlab_merge_request_still_reports_change_kind() -> None:
    ref = source.parse_source_url("https://gitlab.com/acme/platform/-/merge_requests/9")
    assert ref.kind == "change"


def test_parse_gitlab_issue_rejects_a_traversal_project_path() -> None:
    """The issue marker inherits the MR marker's segment rejection."""
    with pytest.raises(ValueError, match="Invalid GitLab project path"):
        source.parse_source_url("https://gitlab.com/a/../b/-/issues/1")


@pytest.mark.parametrize(
    "url",
    [
        # GitLab issues live under the /-/ scope; the bare form is not a
        # GitLab issue URL and must stay rejected.
        "https://gitlab.com/group/project/issues/1",
        "http://github.com/org/repo/issues/1",
        "https://evil.example/github.com/org/repo/issues/1",
        "https://github.com.evil.example/org/repo/issues/1",
        "https://user@github.com/org/repo/issues/1",
        "https://github.com/org/repo/issues/abc",
        "https://gitlab.com/group/project/-/issues/",
    ],
)
def test_parse_source_url_rejects_untrusted_issue_shapes(url: str) -> None:
    with pytest.raises(ValueError):
        source.parse_source_url(url)


def test_self_hosted_gitlab_issue_rejected_when_allowlist_empty(monkeypatch) -> None:
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset())
    with pytest.raises(ValueError, match="dashboard.gitlab_hosts"):
        source.parse_source_url("https://gitlab.acme.internal/team/api/-/issues/7")


def test_parse_jira_cloud_issue_url() -> None:
    """Atlassian Cloud (*.atlassian.net) is auto-recognized without allowlisting."""
    ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-123")
    assert ref.provider == "jira"
    assert ref.repo == "PROJ"
    assert ref.number == 123
    assert ref.kind == "issue"
    assert ref.url == "https://acme.atlassian.net/browse/PROJ-123"


def test_parse_jira_issue_key_is_canonicalized_uppercase() -> None:
    """Jira treats keys case-insensitively; one case means one dedup-map entry."""
    ref = source.parse_source_url("https://acme.atlassian.net/browse/proj-9")
    assert ref.repo == "PROJ"
    assert ref.url == "https://acme.atlassian.net/browse/PROJ-9"


def test_parse_jira_issue_drops_query_and_deeper_segments() -> None:
    ref = source.parse_source_url(
        "https://acme.atlassian.net/browse/OPS-77/comments?focusedCommentId=1"
    )
    assert ref.url == "https://acme.atlassian.net/browse/OPS-77"
    assert ref.repo == "OPS"
    assert ref.number == 77


def test_parse_jira_issue_preserves_context_path_prefix(monkeypatch) -> None:
    """Data Center installs serve Jira behind a context path; the chip must
    link to the real endpoint, not the host root."""
    monkeypatch.setattr(source, "_jira_hosts_snapshot", frozenset({"jira.acme.internal"}))
    ref = source.parse_source_url("https://jira.acme.internal/jira/browse/CORE-5")
    assert ref.url == "https://jira.acme.internal/jira/browse/CORE-5"
    assert ref.provider == "jira"


def test_self_hosted_jira_rejected_when_allowlist_empty(monkeypatch) -> None:
    """Same fail-closed discipline as self-managed GitLab."""
    monkeypatch.setattr(source, "_jira_hosts_snapshot", frozenset())
    with pytest.raises(ValueError, match="dashboard.jira_hosts"):
        source.parse_source_url("https://jira.acme.internal/browse/PROJ-1")


class TestSourceRefLabel:
    """``source_ref_label`` -- what a sidebar chip is CALLED.

    These assertions were previously spread across the sidebar's own render
    fixtures, where each provider's punctuation was rebuilt by a template
    string. They live here now because this is the side that knows the
    convention, and the renderer prints whatever it is handed.
    """

    def test_github_uses_hash_for_both_namespaces(self) -> None:
        """GitHub writes ``#123`` for a pull request and an issue alike -- the two
        namespaces share one number counter, and the provider does not
        distinguish them in writing either."""
        pull = source.parse_source_url("https://github.com/acme/widgets/pull/123")
        issue = source.parse_source_url("https://github.com/acme/widgets/issues/124")
        assert source.source_ref_label(pull) == "#123"
        assert source.source_ref_label(issue) == "#124"

    def test_gitlab_bangs_only_the_merge_request(self) -> None:
        """``!7`` is GitLab's mark for a MERGE REQUEST specifically; its issues
        are ``#7``. Labelling a GitLab issue ``!7`` names an unrelated object
        that usually also exists, which is why the split is pinned rather than
        left to whichever renderer formats the chip."""
        mr = source.parse_source_url("https://gitlab.com/acme/service/-/merge_requests/7")
        issue = source.parse_source_url("https://gitlab.com/acme/service/-/issues/7")
        assert source.source_ref_label(mr) == "!7"
        assert source.source_ref_label(issue) == "#7"

    def test_jira_label_is_the_whole_key(self) -> None:
        """Jira has no bare number: ``PROJ-123`` is the identifier. This is the
        case that had the serializer shipping a project key purely so the
        renderer could paste it back on."""
        ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-123")
        assert source.source_ref_label(ref) == "PROJ-123"

    def test_unknown_provider_borrows_no_vendor_punctuation(self) -> None:
        """A provider this build does not know gets ``#``, the most widely shared
        convention -- never ``!``, which would assert it is GitLab. Constructed
        directly because ``parse_source_url`` cannot yet produce such a ref; the
        point is that the label function is total over its input rather than
        exhaustive over today's three providers."""
        ref = source.SourceRef(
            "acme-review",
            "https://review.acme.internal/c/4821",
            "review.acme.internal",
            "acme",
            "widgets",
            4821,
            kind="change",
        )
        assert source.source_ref_label(ref) == "#4821"


@pytest.mark.parametrize(
    "url",
    [
        # The bare suffix is not a tenant; only real subdomains are Cloud Jira.
        "https://atlassian.net/browse/PROJ-1",
        "https://acme.atlassian.net.evil.example/browse/PROJ-1",
        "http://acme.atlassian.net/browse/PROJ-1",
        "https://user@acme.atlassian.net/browse/PROJ-1",
        # Keys must be PROJECT-NUMBER: a bare number, a digit-led project part,
        # an overlong project part, and a missing number all fail.
        "https://acme.atlassian.net/browse/123",
        "https://acme.atlassian.net/browse/1PROJ-1",
        "https://acme.atlassian.net/browse/ABCDEFGHIJK-1",
        "https://acme.atlassian.net/browse/PROJ-",
        "https://acme.atlassian.net/browse/",
        "https://acme.atlassian.net/PROJ-1",
    ],
)
def test_parse_source_url_rejects_untrusted_jira_shapes(url: str) -> None:
    with pytest.raises(ValueError):
        source.parse_source_url(url)


def test_jira_ref_never_passes_the_change_gate() -> None:
    """Every provider-CLI entry point gates on _require_change_ref; a Jira ref
    (always kind='issue') must be refused there so it can never reach gh/glab."""
    ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-123")
    with pytest.raises(ValueError, match="issue"):
        source._require_change_ref(ref)


@pytest.mark.asyncio
async def test_fetch_issue_jira_no_credentials(monkeypatch) -> None:
    """Jira issues raise a descriptive error when no credentials are configured."""

    async def no_hosts() -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", no_hosts)
    monkeypatch.setattr(source, "_get_jira_auth", lambda host: None)
    with pytest.raises(ValueError, match="jira_no_credentials"):
        await source.fetch_issue("https://acme.atlassian.net/browse/PROJ-123")


def test_self_hosted_gitlab_issue_accepted_when_allowlisted(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    ref = source.parse_source_url("https://gitlab.acme.internal/team/platform/api/-/issues/7")
    assert ref.kind == "issue"
    assert ref.host == "gitlab.acme.internal"
    assert ref.project == "team/platform/api"
    assert ref.url == "https://gitlab.acme.internal/team/platform/api/-/issues/7"


def test_self_hosted_gitlab_issue_matches_host_exactly(monkeypatch) -> None:
    """An allowlist entry must not widen to a lookalike host for issues either."""
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    for url in (
        "https://evil-gitlab.acme.internal/a/b/-/issues/1",
        "https://gitlab.acme.internal.evil.test/a/b/-/issues/1",
        "https://gitlab.acme.internal:8443/a/b/-/issues/1",
    ):
        with pytest.raises(ValueError):
            source.parse_source_url(url)


# Every pull-request-only entry point. An issue URL parses successfully now, so
# each of these must refuse it explicitly or it would address the PR namespace.
_ISSUE_URL = "https://github.com/acme/repo/issues/12"


@pytest.mark.asyncio
async def test_fetch_pull_request_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.fetch_pull_request(_ISSUE_URL)
# --- Review-thread replies, top-level comments, unresolve -------------------
# Writes to someone else's pull request under the owner's provider identity, so
# each one repeats resolve's contract: validated url, thread-ownership proof,
# cache invalidated BEFORE dispatch.

_THREAD_MEMBERSHIP = {
    "data": {
        "repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_1"}]}}}
    }
}


@pytest.mark.asyncio
async def test_reply_posts_into_the_thread(monkeypatch) -> None:
    calls: list[tuple] = []

    async def run(*argv, **kwargs):
        calls.append(argv)
        if any("reviewThreads" in a for a in argv):
            return _THREAD_MEMBERSHIP
        return {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "1"}}}}

    monkeypatch.setattr(source, "_run_json", run)
    await source.reply_to_review_thread(
        "https://github.com/acme/repo/pull/12", "PRRT_1", "Agreed")

    mutation = calls[-1]
    assert any("addPullRequestReviewThreadReply" in a for a in mutation)
    assert "threadId=PRRT_1" in mutation
    assert "body=Agreed" in mutation


@pytest.mark.asyncio
async def test_reply_rejects_a_thread_from_another_pull_request(monkeypatch) -> None:
    # The thread id comes from the browser: without this an owner-authenticated
    # reply could be steered at an unrelated pull request.
    run = AsyncMock(return_value={
        "data": {
            "repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_x"}]}}}
        }
    })
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="does not belong"):
        await source.reply_to_review_thread(
            "https://github.com/acme/repo/pull/12", "PRRT_1", "Agreed")
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_reply_rejects_a_path_shaped_thread_id(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="valid thread id"):
        await source.reply_to_review_thread(
            "https://github.com/acme/repo/pull/12", "../../etc/passwd", "hi")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_pull_request_checks_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.fetch_pull_request_checks(_ISSUE_URL)


@pytest.mark.asyncio
async def test_reply_refuses_an_empty_body(monkeypatch) -> None:
    # An accidental empty comment is visible to everyone and is not removable
    # from this surface, so it never reaches the provider.
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="comment body is required"):
        await source.reply_to_review_thread(
            "https://github.com/acme/repo/pull/12", "PRRT_1", "   \n ")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_pull_request_thread_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.resolve_pull_request_thread(_ISSUE_URL, "PRRT_thread1")


@pytest.mark.asyncio
async def test_reply_refuses_an_oversized_body(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="at most"):
        await source.reply_to_review_thread(
            "https://github.com/acme/repo/pull/12", "PRRT_1",
            "x" * (source._MAX_COMMENT_CHARS + 1))
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_enable_auto_merge_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.enable_pull_request_auto_merge(_ISSUE_URL)


@pytest.mark.asyncio
async def test_reply_raises_on_a_graphql_refusal(monkeypatch) -> None:
    # GraphQL reports refusals with HTTP 200, so a transport-only check would
    # report a rejected reply as posted.
    async def run(*argv, **kwargs):
        if any("reviewThreads" in a for a in argv):
            return _THREAD_MEMBERSHIP
        return {"errors": [{"message": "not authorized"}]}

    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(source.SourceProviderError, match="could not post the reply"):
        await source.reply_to_review_thread(
            "https://github.com/acme/repo/pull/12", "PRRT_1", "Agreed")


@pytest.mark.asyncio
async def test_reply_is_refused_on_gitlab(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: {"gitlab.com"})
    with pytest.raises(ValueError, match="only supported on GitHub"):
        await source.reply_to_review_thread(
            "https://gitlab.com/acme/repo/-/merge_requests/12", "abc123", "hi")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_ready_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.mark_pull_request_ready(_ISSUE_URL)


@pytest.mark.asyncio
async def test_reply_invalidates_the_cache_before_dispatch(monkeypatch) -> None:
    order: list[str] = []

    async def invalidate(url):
        order.append("invalidate")

    async def run(*argv, **kwargs):
        if any("reviewThreads" in a for a in argv):
            return _THREAD_MEMBERSHIP
        order.append("dispatch")
        return {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "1"}}}}

    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    monkeypatch.setattr(source, "_run_json", run)
    await source.reply_to_review_thread(
        "https://github.com/acme/repo/pull/12", "PRRT_1", "Agreed")
    assert order == ["invalidate", "dispatch"]


@pytest.mark.asyncio
async def test_unresolve_reopens_the_thread(monkeypatch) -> None:
    calls: list[tuple] = []

    async def run(*argv, **kwargs):
        calls.append(argv)
        if any("reviewThreads" in a for a in argv):
            return _THREAD_MEMBERSHIP
        return {"data": {"unresolveReviewThread": {"thread": {"isResolved": False}}}}

    monkeypatch.setattr(source, "_run_json", run)
    await source.unresolve_pull_request_thread(
        "https://github.com/acme/repo/pull/12", "PRRT_1")
    assert any("unresolveReviewThread" in a for a in calls[-1])


@pytest.mark.asyncio
async def test_comment_posts_to_the_issue_timeline(monkeypatch) -> None:
    calls: list[tuple] = []

    async def run(*argv, **kwargs):
        calls.append(argv)
        return {"id": 1}

    monkeypatch.setattr(source, "_run_json", run)
    await source.comment_on_pull_request(
        "https://github.com/acme/repo/pull/12", "Looks good")
    argv = calls[-1]
    assert "repos/acme/repo/issues/12/comments" in argv
    assert "body=Looks good" in argv
    assert "-X" in argv and "POST" in argv


@pytest.mark.asyncio
async def test_comment_refuses_an_empty_body(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="comment body is required"):
        await source.comment_on_pull_request(
            "https://github.com/acme/repo/pull/12", "")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_check_status_refuses_an_issue_url(monkeypatch) -> None:
    """The chip refresh reaches `gh pr view`, so it must refuse an issue too."""
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source._fetch_check_status(_ISSUE_URL)
    run.assert_not_awaited()


_SUBMIT_PR_URL = "https://github.com/acme/repo/pull/7"


def _pending_reviews_payload() -> list[dict]:
    return [
        {"id": 11, "state": "APPROVED", "body": "someone else already reviewed"},
        {"id": 4242, "state": "PENDING", "body": "[code-review-sage] draft",
         "commit_id": _HEAD_SHA},
    ]


_HEAD_SHA = "9f1c2ab7de40aa11bb22cc33dd44ee55ff667788"


def _stub_run_json(monkeypatch, reviews, *, head=_HEAD_SHA, comments=(), submit=None,
                   heads=None, dismiss_fails=False, auto_merge=None,
                   stale_dismissal=True, protection_fails=False):
    """Route the reads submit_pull_request_review makes by their argv.

    Keyed on the request path rather than call order, because the guards changed
    how many reads happen and an order-keyed side_effect list silently mis-pairs
    responses when that count moves.

    List endpoints are returned in the ``--paginate --slurp`` shape (an array of
    per-page arrays) so the tests exercise the flattening the real calls need.
    ``heads`` supplies successive head reads, which is how the post-submit
    head-moved path is driven.
    """
    calls: list[tuple] = []
    head_queue = list(heads or [])

    async def fake(*argv, **kwargs):
        calls.append(argv)
        path = argv[-1] if argv[-1].startswith("repos/") else ""
        for a in argv:
            if a.startswith("repos/"):
                path = a
                break
        if "-X" in argv and "PUT" in argv:
            if dismiss_fails:
                raise source.SourceProviderError("dismissal refused")
            return {}
        if "-X" in argv and "POST" in argv:
            return submit if submit is not None else {}
        if path.endswith("/reviews"):
            return [list(reviews)]                      # one page
        if path.endswith("/comments"):
            return [list(comments)]                     # one page
        # The head read fetches the whole pull-request object (no `--jq`), so the
        # double must return that SHAPE — returning a bare string is what let a
        # json.loads crash hide behind green tests for two rounds.
        if path.endswith("/protection"):
            if protection_fails:
                raise source.SourceProviderError("protection unreadable")
            return {"required_pull_request_reviews": {
                "dismiss_stale_reviews": stale_dismissal}}
        sha = head_queue.pop(0) if head_queue else head
        return {"head": {"sha": sha}, "auto_merge": auto_merge,
                "base": {"ref": "main"}}

    monkeypatch.setattr(source, "_run_json", fake)
    return calls


@pytest.mark.asyncio
async def test_pending_review_returns_the_single_pending_draft(monkeypatch) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(monkeypatch, _pending_reviews_payload())
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    digest = result.pop("contentDigest")
    assert len(digest) == 64, "digest should be a sha256 hex string"
    assert result == {
        "reviewId": "4242", "body": "[code-review-sage] draft",
        # The inline comments come back too: `contentDigest` binds them, so returning
        # only the body would have the digest certify text the reader never saw.
        "comments": [],
        "commitId": _HEAD_SHA, "headSha": _HEAD_SHA,
        "stale": False, "contentRedacted": False, "autoMergeArmed": False,
        "staleDismissalEnabled": True,
    }


@pytest.mark.asyncio
async def test_pending_review_returns_the_inline_comments(monkeypatch) -> None:
    """The digest binds them, so the reader has to be able to see them."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(monkeypatch, _pending_reviews_payload())
    monkeypatch.setattr(
        source, "_github_pending_review_comments",
        AsyncMock(return_value=[
            {"path": "src/auth.py", "line": 42, "body": "widens the token scope"},
            {"path": "src/x.py", "line": None, "body": "no anchor"},
        ]),
    )
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert result["comments"] == [
        {"path": "src/auth.py", "line": 42, "body": "widens the token scope"},
        {"path": "src/x.py", "line": None, "body": "no anchor"},
    ]


@pytest.mark.asyncio
async def test_pending_review_reports_no_draft_when_none_is_pending(monkeypatch) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(monkeypatch, [{"id": 11, "state": "APPROVED"}])
    assert await source.pull_request_pending_review(_SUBMIT_PR_URL) == {
        "reviewId": "", "body": "", "comments": [], "commitId": "", "headSha": "",
        "stale": False, "contentRedacted": False, "autoMergeArmed": False,
        "contentDigest": "", "staleDismissalEnabled": False,
    }


@pytest.mark.asyncio
async def test_pending_review_redacts_a_credential_in_the_draft_body(monkeypatch) -> None:
    """The body is provider-controlled text; a hand-written draft can quote a secret."""
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(monkeypatch, [
        {"id": 4242, "state": "PENDING", "body": f"use {secret} to deploy",
         "commit_id": _HEAD_SHA},
    ])
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert result["reviewId"] == "4242"
    assert secret not in result["body"]
    # Redaction altered the draft, so the publish path must be able to refuse.
    assert result["contentRedacted"] is True


@pytest.mark.asyncio
async def test_pending_review_reports_a_draft_written_against_an_older_head(
    monkeypatch,
) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(monkeypatch, [
        {"id": 4242, "state": "PENDING", "body": "ok", "commit_id": "a" * 40},
    ])
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert result["stale"] is True
    assert result["commitId"] == "a" * 40
    assert result["headSha"] == _HEAD_SHA


@pytest.mark.asyncio
async def test_pending_review_treats_an_unknown_head_as_stale(monkeypatch) -> None:
    """Fail closed: an unanswerable freshness question is not 'current'."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(monkeypatch, [
        {"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA},
    ], head="")
    assert (await source.pull_request_pending_review(_SUBMIT_PR_URL))["stale"] is True


@pytest.mark.asyncio
async def test_pending_review_detects_a_credential_in_an_inline_comment(
    monkeypatch,
) -> None:
    """Submission publishes every stored comment, not just the body this app reads."""
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "clean body", "commit_id": _HEAD_SHA}],
        comments=[{"body": f"token is {secret}"}],
    )
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert result["body"] == "clean body"
    assert result["contentRedacted"] is True


@pytest.mark.asyncio
async def test_pending_review_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.pull_request_pending_review(_ISSUE_URL)
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_posts_the_event_for_the_pending_review(monkeypatch) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(monkeypatch, _pending_reviews_payload())
    result = await source.submit_pull_request_review(
        _SUBMIT_PR_URL, "4242", "approve", _digest("[code-review-sage] draft"))
    assert result == {"submitted": True, "event": "APPROVE"}
    # The cache is dropped BEFORE the mutation, so a cancelled request can never
    # leave a stale generation able to satisfy a post-mutation refresh.
    invalidate.assert_awaited_once()
    submit_calls = [c for c in calls if "POST" in c]
    assert len(submit_calls) == 1
    assert submit_calls[0] == (
        "gh",
        "api",
        "-X",
        "POST",
        "repos/acme/repo/pulls/7/reviews/4242/events",
        "-f",
        "event=APPROVE",
    )
    # A gating verdict re-reads the head AFTER submitting, so the last call is that
    # check rather than the submit itself.
    assert calls[-1] == ("gh", "api", "repos/acme/repo/pulls/7")


@pytest.mark.parametrize("event", ["APPROVE", "REQUEST_CHANGES", "COMMENT"])
@pytest.mark.asyncio
async def test_submit_review_refuses_a_draft_written_against_an_older_head(
    monkeypatch, event
) -> None:
    """A stale APPROVE is the dangerous case, but no verdict is right on a moved head.

    Repositories without stale-approval dismissal count a stale APPROVE as a live
    approval of code nobody read, and inline comments anchor to lines that may be
    gone -- so every event is refused, not just the verdicts.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(monkeypatch, [
        {"id": 4242, "state": "PENDING", "body": "ok", "commit_id": "a" * 40},
    ])
    with pytest.raises(ValueError, match="written against an earlier commit"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", event, _digest("ok"))
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_a_draft_whose_text_needs_redaction(
    monkeypatch,
) -> None:
    """Submission publishes GitHub's stored draft, not the redacted copy we showed.

    So a draft the dashboard rendered as `[REDACTED]` would go out verbatim. Refuse:
    a leak the user was shown as redacted is worse than no publish button.
    """
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(monkeypatch, [
        {"id": 4242, "state": "PENDING", "body": f"use {secret}", "commit_id": _HEAD_SHA},
    ])
    with pytest.raises(ValueError, match="must be redacted"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", "COMMENT", _digest(f"use {secret}"))
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_when_only_an_inline_comment_needs_redaction(
    monkeypatch,
) -> None:
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "clean", "commit_id": _HEAD_SHA}],
        comments=[{"body": f"token {secret}"}],
    )
    with pytest.raises(ValueError, match="must be redacted"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", "COMMENT",
            _digest("clean", [{"body": f"token {secret}"}]))
    assert not any("POST" in c for c in calls)


@pytest.mark.asyncio
async def test_pending_review_scans_every_page_of_inline_comments(monkeypatch) -> None:
    """The comments endpoint returns 30 per page; a page-one-only scan leaks.

    Drives the multi-page `--paginate --slurp` shape directly: the credential sits
    on the SECOND page, which an unpaginated read would clear for publishing.
    """
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())

    async def fake(*argv, **kwargs):
        path = next((a for a in argv if a.startswith("repos/")), "")
        if path.endswith("/reviews"):
            return [[{"id": 4242, "state": "PENDING", "body": "clean",
                      "commit_id": _HEAD_SHA}]]
        if path.endswith("/comments"):
            assert "--paginate" in argv and "--slurp" in argv, "comment scan not paginated"
            return [
                [{"body": f"nit {i}"} for i in range(30)],     # page 1: clean
                [{"body": f"token {secret}"}],                  # page 2: the leak
            ]
        if path.endswith("/protection"):
            return {"required_pull_request_reviews": {"dismiss_stale_reviews": True}}
        return {"head": {"sha": _HEAD_SHA}, "auto_merge": None,
                "base": {"ref": "main"}}

    monkeypatch.setattr(source, "_run_json", fake)
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert result["contentRedacted"] is True


@pytest.mark.asyncio
async def test_pending_review_finds_a_draft_past_the_first_page_of_reviews(
    monkeypatch,
) -> None:
    """The reviews list paginates too -- a draft on page two must not read as absent."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())

    async def fake(*argv, **kwargs):
        path = next((a for a in argv if a.startswith("repos/")), "")
        if path.endswith("/reviews"):
            assert "--paginate" in argv and "--slurp" in argv, "reviews list not paginated"
            return [
                [{"id": i, "state": "APPROVED", "body": ""} for i in range(30)],
                [{"id": 4242, "state": "PENDING", "body": "late draft",
                  "commit_id": _HEAD_SHA}],
            ]
        if path.endswith("/comments"):
            return [[]]
        if path.endswith("/protection"):
            return {"required_pull_request_reviews": {"dismiss_stale_reviews": True}}
        return {"head": {"sha": _HEAD_SHA}, "auto_merge": None,
                "base": {"ref": "main"}}

    monkeypatch.setattr(source, "_run_json", fake)
    assert (await source.pull_request_pending_review(_SUBMIT_PR_URL))["reviewId"] == "4242"


@pytest.mark.parametrize("event", ["APPROVE", "REQUEST_CHANGES"])
@pytest.mark.asyncio
async def test_submit_review_dismisses_a_verdict_whose_head_moved_mid_publish(
    monkeypatch, event
) -> None:
    """GitHub's submit API takes no expected-head, so validate-then-submit is not atomic.

    A force-push landing in that window would otherwise leave a verdict attached to
    a head nobody reviewed. The verdict is dismissed again and the caller is told.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        heads=[_HEAD_SHA, "b" * 40],      # validation sees the old head, re-read sees new
    )
    with pytest.raises(source.SourceProviderError, match="was dismissed again"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", event, _digest("ok"))
    assert any("PUT" in c for c in calls), "the stale verdict was not dismissed"


@pytest.mark.asyncio
async def test_submit_review_reports_loudly_when_a_stale_verdict_cannot_be_dismissed(
    monkeypatch,
) -> None:
    """An undismissable stale approval is precisely what a human must be told about."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        heads=[_HEAD_SHA, "b" * 40],
        dismiss_fails=True,
    )
    with pytest.raises(source.SourceProviderError, match="could NOT be dismissed"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", "APPROVE", _digest("ok"))


@pytest.mark.asyncio
async def test_submit_review_does_not_head_check_a_comment_only_review(
    monkeypatch,
) -> None:
    """A COMMENT carries no verdict, so a moved head costs nothing to gate on."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        heads=[_HEAD_SHA, "b" * 40],
    )
    result = await source.submit_pull_request_review(
        _SUBMIT_PR_URL, "4242", "COMMENT", _digest("ok"))
    assert result == {"submitted": True, "event": "COMMENT"}
    assert not any("PUT" in c for c in calls)


@pytest.mark.asyncio
async def test_head_sha_is_read_from_the_object_not_via_jq(monkeypatch) -> None:
    """`gh api --jq .head.sha` prints a BARE token that `_run_json`'s json.loads rejects.

    That turned every pending-review read into a 503. The regression is invisible to
    a double that replaces `_run_json`, so this test pins BOTH halves: the argv must
    carry no `--jq`, and the value must be decoded out of the nested object.
    """
    seen: list[tuple] = []

    async def fake(*argv, **kwargs):
        seen.append(argv)
        return {"head": {"sha": _HEAD_SHA}, "auto_merge": None, "number": 7,
                "base": {"ref": "main"}}

    monkeypatch.setattr(source, "_run_json", fake)
    ref = source._require_change_ref(source.parse_source_url(_SUBMIT_PR_URL))
    assert await source._github_pull_request_head_sha(ref) == _HEAD_SHA
    assert seen == [("gh", "api", "repos/acme/repo/pulls/7")]
    assert not any("--jq" in a for c in seen for a in c)


@pytest.mark.asyncio
async def test_head_sha_survives_a_payload_without_a_head_object(monkeypatch) -> None:
    """A missing/odd head must read as unknown -- which the caller treats as stale."""
    monkeypatch.setattr(source, "_run_json", AsyncMock(return_value={"number": 7}))
    ref = source._require_change_ref(source.parse_source_url(_SUBMIT_PR_URL))
    assert await source._github_pull_request_head_sha(ref) == ""


@pytest.mark.asyncio
async def test_submit_review_refuses_approve_while_auto_merge_is_armed(
    monkeypatch,
) -> None:
    """The one combination the post-submit dismissal cannot repair.

    Validate-then-submit is not atomic (GitHub offers no expected-head parameter),
    and with auto-merge armed the approval satisfies branch protection and GitHub can
    merge the unreviewed head BEFORE the compensating dismissal lands. Nothing
    repairs a merge, so APPROVE is refused for exactly this case.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        auto_merge={"enabled_by": {"login": "someone"}, "merge_method": "squash"},
    )
    with pytest.raises(ValueError, match="Auto-merge is armed"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", "APPROVE", _digest("ok"))
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.parametrize("event", ["COMMENT", "REQUEST_CHANGES"])
@pytest.mark.asyncio
async def test_submit_review_allows_non_approving_verdicts_under_auto_merge(
    monkeypatch, event
) -> None:
    """Only APPROVE can satisfy protection and let a merge through; the others cannot."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        auto_merge={"merge_method": "squash"},
    )
    result = await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", event, _digest("ok"))
    assert result == {"submitted": True, "event": event}
    assert any("POST" in c for c in calls)


@pytest.mark.asyncio
async def test_pending_review_reports_auto_merge_so_the_ui_can_withhold_approve(
    monkeypatch,
) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        auto_merge={"merge_method": "squash"},
    )
    assert (await source.pull_request_pending_review(_SUBMIT_PR_URL))["autoMergeArmed"] is True


@pytest.mark.asyncio
async def test_pull_request_state_treats_an_odd_auto_merge_shape_as_armed(
    monkeypatch,
) -> None:
    """Fail closed: an unrecognised `auto_merge` value must not read as safe."""
    monkeypatch.setattr(
        source, "_run_json",
        AsyncMock(return_value={"head": {"sha": _HEAD_SHA}, "auto_merge": "yes"}),
    )
    ref = source._require_change_ref(source.parse_source_url(_SUBMIT_PR_URL))
    assert (await source._github_pull_request_state(ref))["autoMergeArmed"] is True


@pytest.mark.asyncio
async def test_submit_review_rejects_an_unknown_event(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="APPROVE, REQUEST_CHANGES, or COMMENT"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", "DISMISS", "d")
    run.assert_not_awaited()


@pytest.mark.parametrize("review_id", ["", "0", "abc", "42; rm -rf /", "../99", "4242 "])
@pytest.mark.asyncio
async def test_submit_review_rejects_a_malformed_review_id(monkeypatch, review_id) -> None:
    """The id is interpolated into the REST path, so only a bare positive int passes."""
    run = AsyncMock()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="valid review id"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, review_id, "COMMENT", "d")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_a_draft_the_caller_did_not_read(monkeypatch) -> None:
    """A stale id must be rejected, never resolved to whatever draft exists now.

    Otherwise a review the human started by hand after the page loaded would be
    published in place of the one the caller was shown.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(monkeypatch, _pending_reviews_payload())
    with pytest.raises(ValueError, match="no longer pending"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "999", "APPROVE", "d")
    assert not any("POST" in c for c in calls)   # reads only, never submit
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.submit_pull_request_review(_ISSUE_URL, "4242", "COMMENT", "d")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_a_gitlab_merge_request(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="only be published on GitHub"):
        await source.submit_pull_request_review(
            "https://gitlab.com/acme/repo/-/merge_requests/7", "4242", "COMMENT", "d"
        )
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_endpoint_drops_issue_urls_before_scheduling(monkeypatch) -> None:
    """An issue URL submitted to the chip-status endpoint is skipped, not scheduled."""
    pr_url = "https://github.com/acme/repo/pull/12"
    source._check_cache.clear()
    refresh = MagicMock(return_value=[])
    monkeypatch.setattr(source, "schedule_check_refresh", refresh)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/status", json={"urls": [_ISSUE_URL, pr_url]}
        )
        assert response.status == 200
        assert await response.json() == {
            "statuses": {},
            "refreshing": [],
            "ttlSecs": source.CHECK_STATUS_TTL_SECS,
        }

    assert refresh.call_args.args[0] == [pr_url]


@pytest.mark.asyncio
async def test_fetch_issue_refuses_a_pull_request_url(monkeypatch) -> None:
    """The refusal runs both ways: the issue reader must not read a PR."""
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="not an issue"):
        await source.fetch_issue("https://github.com/acme/repo/pull/12")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_github_issue_normalizes_the_contract_payload(monkeypatch) -> None:
    commands: list[str] = []

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        commands.append(command)
        if command.endswith("repos/acme/repo/issues/12"):
            return {
                "number": 999,
                "html_url": "https://github.com/attacker/evil/issues/1",
                "title": "Panel drops a link",
                "body": "Steps to reproduce",
                "state": "CLOSED",
                "state_reason": "completed",
                "user": {"login": "reporter"},
                "created_at": "2026-07-01T10:00:00Z",
                "updated_at": "2026-07-02T10:00:00Z",
                "closed_at": "2026-07-03T10:00:00Z",
                "closed_by": {"login": "maintainer"},
                "labels": [
                    {"name": "bug", "color": "d73a4a", "description": "Broken"},
                    {"name": "ui", "color": "", "description": ""},
                ],
                "assignees": [{"login": "octocat"}, {}],
                "milestone": {"title": "v2", "state": "open", "due_on": "2026-08-01T00:00:00Z"},
                "comments": 1,
                "locked": True,
                "reactions": {"total_count": 3, "+1": 2, "-1": 1, "heart": 0},
            }
        if "/comments?" in command:
            return [
                {
                    "id": 77,
                    "user": {"login": "helper"},
                    "body": "Confirmed",
                    "created_at": "2026-07-01T11:00:00Z",
                    "html_url": "https://github.com/acme/repo/issues/12#issuecomment-77",
                }
            ]
        if "/timeline?" in command:
            return [
                {"event": "labeled"},
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "number": 34,
                            "title": "Fix the panel",
                            "state": "open",
                            "html_url": "https://github.com/acme/repo/pull/34",
                            "pull_request": {"url": "x"},
                        }
                    },
                },
                {
                    # A plain issue-to-issue mention is NOT a linked change.
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "number": 35,
                            "title": "Related report",
                            "state": "open",
                            "html_url": "https://github.com/acme/repo/issues/35",
                        }
                    },
                },
            ]
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://github.com/acme/repo/issues/12")
    data = await source._fetch_github_issue(ref)

    assert set(data) == {
        "provider",
        "url",
        "number",
        "title",
        "description",
        "state",
        "stateReason",
        "author",
        "createdAt",
        "updatedAt",
        "closedAt",
        "closedBy",
        "labels",
        "assignees",
        "milestone",
        "commentCount",
        "locked",
        "reactions",
        "comments",
        "linkedChanges",
        "partialSections",
    }
    # Identity is the validated ref, never the provider echo.
    assert data["url"] == "https://github.com/acme/repo/issues/12"
    assert data["number"] == 12
    assert data["state"] == "closed"
    assert data["stateReason"] == "completed"
    assert data["author"] == "reporter"
    assert data["closedBy"] == "maintainer"
    assert data["labels"] == [
        {"name": "bug", "color": "d73a4a", "description": "Broken"},
        {"name": "ui", "color": "", "description": ""},
    ]
    assert data["assignees"] == ["octocat"]
    assert data["milestone"] == {"title": "v2", "state": "open", "dueOn": "2026-08-01T00:00:00Z"}
    assert data["locked"] is True
    assert data["reactions"] == {
        "total": 3,
        "plus1": 2,
        "minus1": 1,
        "laugh": 0,
        "hooray": 0,
        "confused": 0,
        "heart": 0,
        "rocket": 0,
        "eyes": 0,
    }
    assert data["comments"] == [
        {
            "id": "77",
            "author": "helper",
            "body": "Confirmed",
            "createdAt": "2026-07-01T11:00:00Z",
            "url": "https://github.com/acme/repo/issues/12#issuecomment-77",
        }
    ]
    assert data["linkedChanges"] == [
        {
            "provider": "github",
            "url": "https://github.com/acme/repo/pull/34",
            "number": 34,
            "title": "Fix the panel",
            "state": "open",
        }
    ]
    assert data["partialSections"] == []
    assert not any("pr view" in command for command in commands)


@pytest.mark.asyncio
async def test_fetch_github_issue_degrades_failed_sections(monkeypatch) -> None:
    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("repos/acme/repo/issues/12"):
            return {"title": "T", "state": "open", "comments": 4}
        raise source.SourceProviderError("boom")

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_github_issue(
        source.parse_source_url("https://github.com/acme/repo/issues/12")
    )

    assert data["comments"] == []
    assert data["linkedChanges"] == []
    assert data["partialSections"] == ["comments", "linked changes"]
    # The provider's own count survives a failed comment page.
    assert data["commentCount"] == 4


@pytest.mark.asyncio
async def test_fetch_github_issue_rejects_a_non_https_linked_change(monkeypatch) -> None:
    """A cross-reference URL reaches an href, so only https survives."""

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("repos/acme/repo/issues/12"):
            return {"title": "T", "state": "open"}
        if "/comments?" in command:
            return []
        return [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 1,
                        "html_url": "javascript:alert(1)",
                        "pull_request": {},
                    }
                },
            }
        ]

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_github_issue(
        source.parse_source_url("https://github.com/acme/repo/issues/12")
    )

    assert data["linkedChanges"] == []


@pytest.mark.asyncio
async def test_fetch_gitlab_issue_normalizes_the_contract_payload(monkeypatch) -> None:
    hosts: list[str] = []

    async def fake_run(*argv: str, **kwargs):
        command = " ".join(argv)
        hosts.append(kwargs.get("host", ""))
        if "with_labels_details" in command:
            assert command.startswith("glab api projects/acme%2Fplatform%2Fservice/issues/42")
            return {
                "iid": 999,
                "web_url": "https://gitlab.evil.test/x/-/issues/1",
                "title": "MR panel is blank",
                "description": "Long form",
                "state": "opened",
                "author": {"username": "reporter"},
                "created_at": "2026-07-01T10:00:00Z",
                "updated_at": "2026-07-02T10:00:00Z",
                "closed_at": "",
                "closed_by": None,
                "labels": [
                    {"name": "bug", "color": "#d73a4a", "description": "Broken"},
                    "plain-name-only",
                ],
                "assignees": [{"username": "dev"}],
                "milestone": {"title": "v2", "state": "active", "due_date": "2026-08-01"},
                "user_notes_count": 1,
                "discussion_locked": False,
                "upvotes": 4,
                "downvotes": 1,
            }
        if "/notes?" in command:
            return [
                {"id": 5, "system": True, "body": "changed the description", "author": {}},
                {
                    "id": 6,
                    "author": {"username": "helper"},
                    "body": "Reproduced",
                    "created_at": "2026-07-01T11:00:00Z",
                },
            ]
        if command.endswith("/related_merge_requests"):
            return [
                {
                    "iid": 34,
                    "title": "Fix the panel",
                    "state": "opened",
                    "web_url": "https://gitlab.com/acme/platform/service/-/merge_requests/34",
                }
            ]
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://gitlab.com/acme/platform/service/-/issues/42")
    data = await source._fetch_gitlab_issue(ref)

    assert data["provider"] == "gitlab"
    # Identity is the validated ref, not the provider's web_url/iid.
    assert data["url"] == "https://gitlab.com/acme/platform/service/-/issues/42"
    assert data["number"] == 42
    assert data["state"] == "open"
    assert data["stateReason"] == ""
    assert data["author"] == "reporter"
    assert data["closedBy"] == ""
    # #rrggbb is normalized to the bare form GitHub already uses.
    assert data["labels"] == [
        {"name": "bug", "color": "d73a4a", "description": "Broken"},
        {"name": "plain-name-only", "color": "", "description": ""},
    ]
    assert data["assignees"] == ["dev"]
    assert data["milestone"] == {"title": "v2", "state": "active", "dueOn": "2026-08-01"}
    assert data["locked"] is False
    assert data["reactions"] == {
        "total": 5,
        "plus1": 4,
        "minus1": 1,
        "laugh": 0,
        "hooray": 0,
        "confused": 0,
        "heart": 0,
        "rocket": 0,
        "eyes": 0,
    }
    # System notes are lifecycle churn, not discussion.
    assert data["comments"] == [
        {
            "id": "6",
            "author": "helper",
            "body": "Reproduced",
            "createdAt": "2026-07-01T11:00:00Z",
            "url": "https://gitlab.com/acme/platform/service/-/issues/42#note_6",
        }
    ]
    assert data["linkedChanges"] == [
        {
            "provider": "gitlab",
            "url": "https://gitlab.com/acme/platform/service/-/merge_requests/34",
            "number": 34,
            "title": "Fix the panel",
            "state": "open",
        }
    ]
    assert data["partialSections"] == []
    # Every glab call is pinned to the ref's host.
    assert set(hosts) == {"gitlab.com"}


@pytest.mark.asyncio
async def test_fetch_gitlab_issue_passes_the_self_managed_host(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    hosts: list[str] = []

    async def fake_run(*argv: str, **kwargs):
        hosts.append(kwargs.get("host", ""))
        if "with_labels_details" in " ".join(argv):
            return {"title": "T", "state": "opened"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://gitlab.acme.internal/team/api/-/issues/7")
    await source._fetch_gitlab_issue(ref)

    assert hosts == ["gitlab.acme.internal"] * 3


@pytest.mark.asyncio
async def test_fetch_issue_caches_and_coalesces_by_normalized_url(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_fetch(ref):
        calls["count"] += 1
        await asyncio.sleep(0)
        return {"provider": "github", "url": ref.url, "number": ref.number}

    monkeypatch.setattr(source, "_fetch_github_issue", fake_fetch)
    url = "https://github.com/acme/repo/issues/12"

    first, second = await asyncio.gather(source.fetch_issue(url), source.fetch_issue(f"{url}/"))
    assert first is second
    assert calls["count"] == 1

    # Served from cache inside the TTL.
    assert await source.fetch_issue(url) is first
    assert calls["count"] == 1

    # refresh=True bypasses the cache.
    await source.fetch_issue(url, refresh=True)
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_fetch_issue_never_writes_the_chip_status_cache(monkeypatch) -> None:
    """An issue has no CI or merge state, so it must not project a chip status."""
    record = MagicMock()
    monkeypatch.setattr(source, "record_full_payload_status", record)

    async def fake_fetch(ref):
        return {"provider": "github", "url": ref.url, "state": "open"}

    monkeypatch.setattr(source, "_fetch_github_issue", fake_fetch)
    source._check_cache.clear()

    url = "https://github.com/acme/repo/issues/12"
    await source.fetch_issue(url)

    record.assert_not_called()
    assert source.get_cached_check_status(url) is None


@pytest.mark.asyncio
async def test_fetch_issue_rejects_an_oversized_payload(monkeypatch) -> None:
    async def fake_fetch(ref):
        return {"provider": "github", "url": ref.url, "description": "x" * 16}

    monkeypatch.setattr(source, "_fetch_github_issue", fake_fetch)
    monkeypatch.setattr(source, "_MAX_PAYLOAD_BYTES", 8)

    with pytest.raises(source.SourceProviderError, match="issue payload was too large"):
        await source.fetch_issue("https://github.com/acme/repo/issues/12")
    assert source._ISSUE_CACHE == {}


@pytest.mark.asyncio
async def test_fetch_issue_reserves_direct_fetch_capacity(monkeypatch) -> None:
    """The issue task must be visible to the shared pending/byte accounting.

    A task absent from `_direct_fetch_tasks` would hold a reservation nothing
    reads, making the cap decorative for issues.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_fetch(ref):
        started.set()
        await release.wait()
        return {"provider": "github", "url": ref.url}

    monkeypatch.setattr(source, "_fetch_github_issue", fake_fetch)
    monkeypatch.setattr(source, "_DIRECT_FETCH_PENDING_MAX", 1)
    monkeypatch.setattr(source, "_DIRECT_FETCH_WAIT_SECS", 0.05)

    inflight = asyncio.ensure_future(
        source.fetch_issue("https://github.com/acme/repo/issues/12")
    )
    await started.wait()
    try:
        with pytest.raises(source.SourceProviderError, match="pending"):
            await source.fetch_issue("https://github.com/acme/repo/issues/13")
    finally:
        release.set()
        await inflight


@pytest.mark.asyncio
async def test_issue_endpoint_returns_the_payload(monkeypatch) -> None:
    payload = {"provider": "github", "url": _ISSUE_URL, "number": 12}
    fetch = AsyncMock(return_value=payload)
    monkeypatch.setattr(source, "fetch_issue", fetch)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/issue", json={"url": _ISSUE_URL})
        assert response.status == 200
        assert await response.json() == payload

    fetch.assert_awaited_once_with(_ISSUE_URL, refresh=False)


@pytest.mark.asyncio
async def test_issue_endpoint_maps_value_error_to_400(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "fetch_issue", AsyncMock(side_effect=ValueError("An issue URL is required."))
    )

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/issue", json={"url": "nope"})
        assert response.status == 400
        assert await response.json() == {"error": "An issue URL is required.", "code": "invalid_request"}


@pytest.mark.asyncio
async def test_issue_endpoint_maps_provider_error_to_503(monkeypatch) -> None:
    monkeypatch.setattr(
        source,
        "fetch_issue",
        AsyncMock(side_effect=source.SourceProviderError("gh timed out")),
    )

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/issue", json={"url": _ISSUE_URL})
        assert response.status == 503
        # `code` distinguishes a real provider failure from admission pressure, so
        # the client retries only the latter. Issues share the pull-request
        # admission pool, so this endpoint can report either.
        assert await response.json() == {"error": "gh timed out", "code": "provider_error"}


@pytest.mark.asyncio
async def test_issue_endpoint_rejects_a_non_owner(monkeypatch) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(source, "fetch_issue", fetch)

    async with TestClient(TestServer(_app(user="U_OTHER"))) as client:
        response = await client.post("/api/source/issue", json={"url": _ISSUE_URL})
        assert response.status == 403

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_issue_endpoint_rejects_an_app_token(monkeypatch) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(source, "fetch_issue", fetch)

    async with TestClient(TestServer(_app(app_name="issue-radar"))) as client:
        response = await client.post("/api/source/issue", json={"url": _ISSUE_URL})
        assert response.status == 403

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_issue_endpoint_audits_its_own_operation_name(monkeypatch, _mock_source_sel) -> None:
    monkeypatch.setattr(
        source, "fetch_issue", AsyncMock(return_value={"provider": "github", "url": _ISSUE_URL})
    )

    async with TestClient(TestServer(_app())) as client:
        assert (await client.post("/api/source/issue", json={"url": _ISSUE_URL})).status == 200

    operations = {
        call.kwargs.get("operation")
        for call in _mock_source_sel.log_api_access.call_args_list
    }
    assert operations == {"source.issue.read"}
    assert _mock_source_sel.log_api_access.call_args.kwargs["outcome"] == "completed"


@pytest.mark.asyncio
async def test_issue_endpoint_warms_allowlist_before_parsing_self_hosted_urls(
    monkeypatch,
) -> None:
    """A cold snapshot would drop an authorized self-managed issue URL as
    unsupported, so the handler's fetch path must warm the allowlist first."""
    url = "https://gitlab.acme.internal/team/api/-/issues/7"
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(source, "_gitlab_hosts_loaded_at", 0.0)

    async def fake_ensure() -> frozenset:
        source._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
        return frozenset({"gitlab.acme.internal"})

    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", fake_ensure)

    async def fake_fetch(ref):
        return {"provider": "gitlab", "url": ref.url, "number": ref.number}

    monkeypatch.setattr(source, "_fetch_gitlab_issue", fake_fetch)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/issue", json={"url": url})
        assert response.status == 200
        assert (await response.json())["url"] == url


@pytest.mark.asyncio
async def test_reply_and_comment_endpoints_require_the_owner(monkeypatch) -> None:
    # These write to a third-party pull request under the owner's provider
    # identity, so they inherit resolve's owner gate rather than defining their
    # own. An app-scoped caller must not reach them.
    reply = AsyncMock()
    comment = AsyncMock()
    unresolve = AsyncMock()
    monkeypatch.setattr(source, "reply_to_review_thread", reply)
    monkeypatch.setattr(source, "comment_on_pull_request", comment)
    monkeypatch.setattr(source, "unresolve_pull_request_thread", unresolve)

    url = "https://github.com/acme/repo/pull/12"
    app = _app(app_name="some-app")
    async with TestClient(TestServer(app)) as client:
        replied = await client.post(
            "/api/source/pull-request/reply",
            json={"url": url, "threadId": "PRRT_1", "body": "hi"},
        )
        commented = await client.post(
            "/api/source/pull-request/comment", json={"url": url, "body": "hi"}
        )
        unresolved = await client.post(
            "/api/source/pull-request/unresolve",
            json={"url": url, "threadId": "PRRT_1"},
        )

    assert replied.status == 403
    assert commented.status == 403
    assert unresolved.status == 403
    reply.assert_not_awaited()
    comment.assert_not_awaited()
    unresolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_endpoint_passes_the_body_through(monkeypatch) -> None:
    reply = AsyncMock()
    monkeypatch.setattr(source, "reply_to_review_thread", reply)
    url = "https://github.com/acme/repo/pull/12"
    app = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/source/pull-request/reply",
            json={"url": url, "threadId": "PRRT_1", "body": "Agreed"},
        )
        assert response.status == 200
        assert await response.json() == {"posted": True}
    reply.assert_awaited_once_with(url, "PRRT_1", "Agreed")


def _digest(body, comments=()):
    return source._review_content_digest(body, list(comments))


def test_content_digest_changes_with_every_publishable_field() -> None:
    """The digest must move when anything GitHub would publish moves."""
    base = _digest("body", [{"id": 1, "path": "a.py", "line": 3, "body": "nit"}])
    assert base != _digest("edited", [{"id": 1, "path": "a.py", "line": 3, "body": "nit"}])
    assert base != _digest("body", [{"id": 1, "path": "a.py", "line": 3, "body": "changed"}])
    assert base != _digest("body", [{"id": 1, "path": "b.py", "line": 3, "body": "nit"}])
    assert base != _digest("body", [{"id": 1, "path": "a.py", "line": 9, "body": "nit"}])
    assert base != _digest("body", [])                      # comment removed
    assert base != _digest("body", [
        {"id": 1, "path": "a.py", "line": 3, "body": "nit"},
        {"id": 2, "path": "a.py", "line": 4, "body": "more"},
    ])                                                       # comment added


def test_content_digest_is_stable_under_reordering() -> None:
    """A re-ordered read of identical content must not read as an edit."""
    a = {"id": 1, "path": "a.py", "line": 3, "body": "one"}
    b = {"id": 2, "path": "a.py", "line": 4, "body": "two"}
    assert _digest("body", [a, b]) == _digest("body", [b, a])


@pytest.mark.asyncio
async def test_submit_review_refuses_a_draft_edited_since_it_was_displayed(
    monkeypatch,
) -> None:
    """The review id identifies the OBJECT; GitHub lets its content change under it.

    A draft edited after the UI rendered it would otherwise publish text the caller
    never read, with the id guard none the wiser.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "edited since display",
          "commit_id": _HEAD_SHA}],
    )
    with pytest.raises(ValueError, match="changed after it was displayed"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", "COMMENT", _digest("what the caller read"),
        )
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_accepts_the_digest_it_was_shown(monkeypatch) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    reviews = [{"id": 4242, "state": "PENDING", "body": "unchanged",
                "commit_id": _HEAD_SHA}]
    calls = _stub_run_json(monkeypatch, reviews)
    result = await source.submit_pull_request_review(
        _SUBMIT_PR_URL, "4242", "COMMENT", _digest("unchanged"),
    )
    assert result == {"submitted": True, "event": "COMMENT"}
    assert any("POST" in c for c in calls)


@pytest.mark.parametrize("digest", ["", None])
@pytest.mark.asyncio
async def test_submit_review_refuses_a_missing_content_digest(monkeypatch, digest) -> None:
    """An omitted digest must FAIL, never skip the comparison.

    A digest that is only checked when present is a one-parameter bypass of the
    content binding: any caller omitting it publishes an unseen draft.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
    )
    with pytest.raises(ValueError, match="contentDigest is required"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", "COMMENT", digest or "")
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_approve_when_the_branch_keeps_stale_approvals(
    monkeypatch,
) -> None:
    """`dismiss_stale_reviews` is what makes a stale approval harmless.

    Without it, an approval can outlive the commit it reviewed regardless of how our
    own checks are ordered -- so APPROVE is withheld rather than raced.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        stale_dismissal=False,
    )
    with pytest.raises(ValueError, match="does not dismiss approvals"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", "APPROVE", _digest("ok"))
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_approve_when_protection_is_unreadable(
    monkeypatch,
) -> None:
    """Fail closed: no admin rights (or no protection) must not read as safe."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        protection_fails=True,
    )
    with pytest.raises(ValueError, match="does not dismiss approvals"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", "APPROVE", _digest("ok"))


@pytest.mark.parametrize("event", ["COMMENT", "REQUEST_CHANGES"])
@pytest.mark.asyncio
async def test_submit_review_allows_non_approving_verdicts_without_stale_dismissal(
    monkeypatch, event
) -> None:
    """Only an APPROVE can authorize a merge, so only APPROVE needs the setting."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        stale_dismissal=False,
    )
    result = await source.submit_pull_request_review(
        _SUBMIT_PR_URL, "4242", event, _digest("ok"))
    assert result == {"submitted": True, "event": event}
    assert any("POST" in c for c in calls)


@pytest.mark.asyncio
async def test_pending_review_reports_stale_dismissal_for_the_ui(monkeypatch) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        stale_dismissal=False,
    )
    got = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert got["staleDismissalEnabled"] is False


class TestStaleDismissalTwoSurfaces:
    """APPROVE needs a confirmed `dismiss stale reviews`, from whichever read can see it."""

    @staticmethod
    def _ref():
        return source.SourceRef(provider="github", url="https://github.com/o/r/pull/7",
                                host="github.com", owner="o", repo="r",
                                number=7)

    @pytest.mark.asyncio
    async def test_graphql_answers_when_rest_is_forbidden(self, monkeypatch):
        # The non-admin case: REST protection is admin-only, so it raises; GraphQL sees the
        # rule. Withholding here would send a contributor back to github.com to approve.
        async def fake_run_json(*args, **kwargs):
            if "graphql" in args:
                return {"data": {"repository": {"branchProtectionRules": {"nodes": [
                    {"pattern": "main", "dismissesStaleReviews": True},
                ]}}}}
            raise RuntimeError("HTTP 403: admin rights required")

        monkeypatch.setattr(source, "_run_json", fake_run_json)
        assert await source._github_stale_dismissal_enabled(self._ref(), "main") is True

    @pytest.mark.asyncio
    async def test_withheld_when_neither_surface_confirms(self, monkeypatch):
        async def fake_run_json(*args, **kwargs):
            if "graphql" in args:
                return {"data": {"repository": {"branchProtectionRules": {"nodes": []}}}}
            raise RuntimeError("HTTP 404: branch not protected")

        monkeypatch.setattr(source, "_run_json", fake_run_json)
        assert await source._github_stale_dismissal_enabled(self._ref(), "main") is False

    @pytest.mark.asyncio
    async def test_a_rule_for_another_branch_says_nothing(self, monkeypatch):
        # A rule on `releases/*` tells us nothing about `main`; treating any rule as
        # covering any branch would approve against an unprotected base.
        async def fake_run_json(*args, **kwargs):
            if "graphql" in args:
                return {"data": {"repository": {"branchProtectionRules": {"nodes": [
                    {"pattern": "releases/*", "dismissesStaleReviews": True},
                ]}}}}
            raise RuntimeError("HTTP 403")

        monkeypatch.setattr(source, "_run_json", fake_run_json)
        assert await source._github_stale_dismissal_enabled(self._ref(), "main") is False

    @pytest.mark.asyncio
    async def test_a_glob_that_covers_the_branch_counts(self, monkeypatch):
        async def fake_run_json(*args, **kwargs):
            if "graphql" in args:
                return {"data": {"repository": {"branchProtectionRules": {"nodes": [
                    {"pattern": "releases/*", "dismissesStaleReviews": True},
                ]}}}}
            raise RuntimeError("HTTP 403")

        monkeypatch.setattr(source, "_run_json", fake_run_json)
        assert await source._github_stale_dismissal_enabled(self._ref(), "releases/1.2") is True

    @pytest.mark.asyncio
    async def test_a_glob_does_not_reach_a_deeper_branch(self, monkeypatch):
        # GitHub matches protection patterns per segment: `releases/*` covers
        # `releases/1.2` but NOT `releases/1/2`. Python's fnmatch would match both,
        # so this asserts the SITE uses the slash-aware matcher -- a fail-open here
        # lets a stale approval survive on a branch with no dismissal rule at all.
        async def fake_run_json(*args, **kwargs):
            if "graphql" in args:
                return {"data": {"repository": {"branchProtectionRules": {"nodes": [
                    {"pattern": "releases/*", "dismissesStaleReviews": True},
                ]}}}}
            raise RuntimeError("HTTP 403")

        monkeypatch.setattr(source, "_run_json", fake_run_json)
        assert await source._github_stale_dismissal_enabled(
            self._ref(), "releases/1/2") is False


class TestBranchPatternSlashSemantics:
    """GitHub matches protection patterns per path segment; Python's fnmatch does
    not. Getting this wrong treats an unprotected branch as protected, which is a
    fail-OPEN on the APPROVE verdict."""

    def test_single_star_does_not_cross_a_slash(self):
        assert source._branch_pattern_matches("releases/*", "releases/1.0")
        assert not source._branch_pattern_matches("releases/*", "releases/1/0")

    def test_double_star_spans_segments(self):
        assert source._branch_pattern_matches("releases/**", "releases/1/0")
        assert source._branch_pattern_matches("releases/**", "releases/1.0")

    def test_exact_pattern_still_matches_and_is_case_sensitive(self):
        assert source._branch_pattern_matches("main", "main")
        assert not source._branch_pattern_matches("Main", "main")

    def test_deeper_branch_is_not_covered_by_a_shallow_pattern(self):
        # The reported fail-open: a `releases/*` rule must not open APPROVE for
        # `releases/x/y`, which GitHub does not protect.
        assert not source._branch_pattern_matches("*", "releases/x")


# ── Jira issue fetching tests ────────────────────────────────────────────────


class TestAdfToMarkdown:
    """The ADF converter emits markdown for Atlassian Document Format JSON."""

    @staticmethod
    def _doc(*content):
        return {"type": "doc", "version": 1, "content": list(content)}

    @staticmethod
    def _para(*content):
        return {"type": "paragraph", "content": list(content)}

    @staticmethod
    def _text(text, marks=None):
        node = {"type": "text", "text": text}
        if marks is not None:
            node["marks"] = marks
        return node

    def test_simple_paragraph(self):
        adf = self._doc(self._para(self._text("Hello world")))
        assert source._adf_to_markdown(adf) == "Hello world"

    def test_multiple_paragraphs_separated_by_blank_line(self):
        adf = self._doc(self._para(self._text("Line 1")), self._para(self._text("Line 2")))
        assert source._adf_to_markdown(adf) == "Line 1\n\nLine 2"

    def test_heading_becomes_hashes(self):
        adf = self._doc(
            {"type": "heading", "attrs": {"level": 3}, "content": [self._text("Title")]}
        )
        assert source._adf_to_markdown(adf) == "### Title"

    def test_heading_level_is_clamped(self):
        adf = self._doc(
            {"type": "heading", "attrs": {"level": 99}, "content": [self._text("Deep")]}
        )
        assert source._adf_to_markdown(adf) == "###### Deep"

    def test_heading_stays_on_one_line(self):
        """Only a heading's first line carries the `#`, so a hardBreak inside it
        would leave a second line whose `-` renders as a list."""
        adf = self._doc(
            {
                "type": "heading",
                "attrs": {"level": 3},
                "content": [self._text("Title"), {"type": "hardBreak"}, self._text("- x")],
            }
        )
        assert source._adf_to_markdown(adf) == "### Title - x"

    def test_emphasis_marks(self):
        adf = self._doc(
            self._para(
                self._text("bold", [{"type": "strong"}]),
                self._text(" "),
                self._text("italic", [{"type": "em"}]),
                self._text(" "),
                self._text("gone", [{"type": "strike"}]),
            )
        )
        assert source._adf_to_markdown(adf) == "**bold** *italic* ~~gone~~"

    def test_adjacent_identical_marks_are_merged(self):
        """`**a****b**` renders as a bold `a****b` -- the delimiters become
        content -- so equally marked neighbours must merge before wrapping."""
        adf = self._doc(
            self._para(
                self._text("a", [{"type": "strong"}]),
                self._text("b", [{"type": "strong"}]),
            )
        )
        assert source._adf_to_markdown(adf) == "**ab**"

    def test_adjacent_code_marks_are_merged(self):
        """Worse than emphasis: `` `a``b` `` collapses into one span holding
        literal backticks."""
        adf = self._doc(
            self._para(
                self._text("a", [{"type": "code"}]),
                self._text("b", [{"type": "code"}]),
            )
        )
        assert source._adf_to_markdown(adf) == "`ab`"

    def test_adjacent_different_marks_are_not_merged(self):
        adf = self._doc(
            self._para(
                self._text("a", [{"type": "strong"}]),
                self._text("b", [{"type": "em"}]),
            )
        )
        # `_` rather than `*` for the em: it abuts the strong's closing `**`, and
        # two asterisk runs that touch are re-lexed as one. `**a**_b_` parses as
        # <strong>a</strong><em>b</em>.
        assert source._adf_to_markdown(adf) == "**a**_b_"

    def test_an_italic_between_plain_neighbours_keeps_its_emphasis(self):
        """CommonMark will not open underscore emphasis intraword, so `a_b_c`
        would render with visible underscores and no italic."""
        adf = self._doc(
            self._para(
                self._text("a"),
                self._text("b", [{"type": "em"}]),
                self._text("c"),
            )
        )
        assert source._adf_to_markdown(adf) == "a*b*c"

    def test_a_hard_break_keeps_marked_neighbours_apart(self):
        adf = self._doc(
            self._para(
                self._text("a", [{"type": "strong"}]),
                {"type": "hardBreak"},
                self._text("b", [{"type": "strong"}]),
            )
        )
        assert source._adf_to_markdown(adf) == "**a**  \n**b**"

    def test_code_mark_is_literal_and_not_escaped(self):
        adf = self._doc(self._para(self._text("a_b*c", [{"type": "code"}])))
        assert source._adf_to_markdown(adf) == "`a_b*c`"

    def test_code_span_keeps_its_boundary_spaces(self):
        """CommonMark strips one space from each end of ` x `, so pad it."""
        adf = self._doc(self._para(self._text(" foo ", [{"type": "code"}])))
        assert source._adf_to_markdown(adf) == "`  foo  `"

    def test_all_whitespace_code_span_is_not_padded(self):
        """Whitespace-only content is exempt from the strip rule, so padding it
        would silently add two spaces."""
        adf = self._doc(self._para(self._text("   ", [{"type": "code"}])))
        assert source._adf_to_markdown(adf) == "`   `"

    def test_one_sided_space_in_a_code_span_is_not_padded(self):
        adf = self._doc(self._para(self._text(" foo", [{"type": "code"}])))
        assert source._adf_to_markdown(adf) == "` foo`"

    def test_newline_in_a_code_span_cannot_break_out_of_the_fence(self):
        """A code span is inline, so a newline in its content ends the paragraph
        and everything after it is parsed as fresh markdown -- outside the fence,
        and so past the inline escape and the link-target scan."""
        payload = "safe\n\n# Injected\n[x](javascript:alert(1))"
        node = self._text(payload, [{"type": "code"}])
        out = source._adf_to_markdown(self._doc(self._para(node)))
        assert "\n" not in out
        assert out == "`safe  # Injected [x](javascript:alert(1))`"

    def test_carriage_return_in_a_code_span_is_collapsed_too(self):
        """CommonMark ends a line on a bare CR and on CRLF, not only on LF."""
        for raw in ("a\rb", "a\r\nb"):
            out = source._adf_to_markdown(
                self._doc(self._para(self._text(raw, [{"type": "code"}])))
            )
            assert out == "`a b`", raw

    def test_folding_a_long_whitespace_run_is_linear(self):
        """A provider-controlled newline-FREE whitespace run, bounded only by the
        8MiB fetch cap, used to be folded by a pattern whose whitespace runs and
        newline anchor competed for the same characters: 200k spaces took ~45s of
        backtracking, per heading and per table cell. The budget is ~100x the
        linear cost, so this fails only on a return to quadratic scanning."""
        payload = " " * 200_000 + "x"
        start = time.monotonic()
        assert source._md_one_line(payload) == "x"
        assert time.monotonic() - start < 2.0

    def test_empty_marked_text_emits_nothing(self):
        """A marked empty text node must not leave its bare delimiters behind."""
        for mark in ("strong", "em", "strike", "code"):
            adf = self._doc(self._para(self._text("", [{"type": mark}])))
            assert source._adf_to_markdown(adf) == "", mark

    def test_external_media_becomes_a_link_not_an_image(self):
        """A link keeps the URL recoverable without the panel auto-fetching it."""
        adf = self._doc(
            self._para(
                {
                    "type": "media",
                    "attrs": {"type": "external", "url": "https://ex.com/a.png", "alt": "chart"},
                }
            )
        )
        assert source._adf_to_markdown(adf) == "[chart](https://ex.com/a.png)"

    def test_media_without_a_url_contributes_nothing(self):
        """An attachment reference carries no fetchable address."""
        adf = self._doc(
            self._para({"type": "media", "attrs": {"type": "file", "id": "abc", "alt": "shot"}})
        )
        assert source._adf_to_markdown(adf) == ""

    def test_link_mark_keeps_the_url(self):
        adf = self._doc(
            self._para(
                self._text(
                    "the docs",
                    [{"type": "link", "attrs": {"href": "https://example.com/a"}}],
                )
            )
        )
        assert source._adf_to_markdown(adf) == "[the docs](https://example.com/a)"

    def test_link_with_parentheses_uses_the_angle_bracket_form(self):
        adf = self._doc(
            self._para(
                self._text(
                    "wiki",
                    [{"type": "link", "attrs": {"href": "https://ex.com/a(b)"}}],
                )
            )
        )
        assert source._adf_to_markdown(adf) == "[wiki](<https://ex.com/a(b)>)"

    def test_inline_card_becomes_a_link(self):
        adf = self._doc(
            self._para({"type": "inlineCard", "attrs": {"url": "https://example.com"}})
        )
        assert source._adf_to_markdown(adf) == "[https://example.com](https://example.com)"

    def test_code_block_is_fenced_with_its_language(self):
        adf = self._doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [self._text("print(1)\nprint(2)")],
            }
        )
        assert source._adf_to_markdown(adf) == "```python\nprint(1)\nprint(2)\n```"

    def test_code_block_fence_widens_past_inner_backticks(self):
        adf = self._doc({"type": "codeBlock", "content": [self._text("a ``` b")]})
        assert source._adf_to_markdown(adf) == "````\na ``` b\n````"

    def test_code_block_language_cannot_leave_its_fence_line(self):
        """A fence info string runs to end of line, so a newline in the
        `language` attribute would close the fence and inject real markdown."""
        adf = self._doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python\n\n![x](https://evil.example/beacon.png)\n\n```"},
                "content": [self._text("safe")],
            }
        )
        result = source._adf_to_markdown(adf)
        assert result == "```\nsafe\n```"
        assert "evil.example" not in result

    def test_code_block_keeps_a_real_language_token(self):
        adf = self._doc(
            {"type": "codeBlock", "attrs": {"language": "c++"}, "content": [self._text("x;")]}
        )
        assert source._adf_to_markdown(adf) == "```c++\nx;\n```"

    def test_code_block_drops_a_multi_token_language(self):
        adf = self._doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python rm -rf"},
                "content": [self._text("x")],
            }
        )
        assert source._adf_to_markdown(adf) == "```\nx\n```"

    def test_expand_title_stays_on_one_line(self):
        adf = self._doc(
            {
                "type": "expand",
                "attrs": {"title": "Details\n\n# Injected"},
                "content": [self._para(self._text("inner"))],
            }
        )
        assert source._adf_to_markdown(adf) == "**Details # Injected**\n\ninner"

    def test_mention_name_stays_on_one_line(self):
        adf = self._doc(self._para({"type": "mention", "attrs": {"text": "Alice\n# Injected"}}))
        assert source._adf_to_markdown(adf) == "@Alice # Injected"

    def test_bullet_list_gets_markers(self):
        adf = self._doc(
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [self._para(self._text("one"))]},
                    {"type": "listItem", "content": [self._para(self._text("two"))]},
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "- one\n- two"

    def test_nested_list_is_indented_under_its_parent(self):
        adf = self._doc(
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            self._para(self._text("outer")),
                            {
                                "type": "bulletList",
                                "content": [
                                    {
                                        "type": "listItem",
                                        "content": [self._para(self._text("inner"))],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "- outer\n  - inner"

    def test_ordered_list_honours_its_start_number(self):
        adf = self._doc(
            {
                "type": "orderedList",
                "attrs": {"order": 3},
                "content": [
                    {"type": "listItem", "content": [self._para(self._text("a"))]},
                    {"type": "listItem", "content": [self._para(self._text("b"))]},
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "3. a\n4. b"

    def test_task_list_becomes_a_checklist(self):
        adf = self._doc(
            {
                "type": "taskList",
                "content": [
                    {
                        "type": "taskItem",
                        "attrs": {"state": "DONE"},
                        "content": [self._text("shipped")],
                    },
                    {
                        "type": "taskItem",
                        "attrs": {"state": "TODO"},
                        "content": [self._text("pending")],
                    },
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "- [x] shipped\n- [ ] pending"

    def test_blockquote_prefixes_every_line(self):
        adf = self._doc(
            {
                "type": "blockquote",
                "content": [self._para(self._text("first")), self._para(self._text("second"))],
            }
        )
        assert source._adf_to_markdown(adf) == "> first\n>\n> second"

    def test_panel_renders_as_a_blockquote(self):
        adf = self._doc(
            {
                "type": "panel",
                "attrs": {"panelType": "warning"},
                "content": [self._para(self._text("careful"))],
            }
        )
        assert source._adf_to_markdown(adf) == "> careful"

    def test_rule_becomes_a_thematic_break(self):
        adf = self._doc(self._para(self._text("a")), {"type": "rule"}, self._para(self._text("b")))
        assert source._adf_to_markdown(adf) == "a\n\n---\n\nb"

    def test_table_becomes_gfm(self):
        adf = self._doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [self._para(self._text("H1"))]},
                            {"type": "tableHeader", "content": [self._para(self._text("H2"))]},
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [self._para(self._text("a"))]},
                            {"type": "tableCell", "content": [self._para(self._text("b"))]},
                        ],
                    },
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "| H1 | H2 |\n| --- | --- |\n| a | b |"

    def test_short_table_row_emits_only_its_own_cells(self):
        """GFM fills a short row itself, so padding it here buys nothing."""
        adf = self._doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [self._para(self._text("H1"))]},
                            {"type": "tableHeader", "content": [self._para(self._text("H2"))]},
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [self._para(self._text("only"))]}
                        ],
                    },
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "| H1 | H2 |\n| --- | --- |\n| only |"

    def test_a_ragged_table_does_not_amplify_its_output(self):
        """A wide header plus many narrow rows must stay linear in cell count."""
        cells = 200
        wide = {
            "type": "tableRow",
            "content": [
                {"type": "tableHeader", "content": [self._para(self._text("h"))]}
                for _ in range(cells)
            ],
        }
        narrow = [
            {
                "type": "tableRow",
                "content": [{"type": "tableCell", "content": [self._para(self._text("c"))]}],
            }
            for _ in range(cells)
        ]
        rendered = source._adf_to_markdown(self._doc({"type": "table", "content": [wide, *narrow]}))
        # 400 real cells. Padding every short row to the widest emits 200*200.
        assert rendered.count("|") < 2000

    def test_mention_gets_an_at_prefix_without_doubling_it(self):
        adf = self._doc(
            self._para(
                {"type": "mention", "attrs": {"text": "Alice"}},
                self._text(" and "),
                {"type": "mention", "attrs": {"text": "@Bob"}},
            )
        )
        assert source._adf_to_markdown(adf) == "@Alice and @Bob"

    def test_hard_break_is_a_markdown_line_break(self):
        adf = self._doc(self._para(self._text("a"), {"type": "hardBreak"}, self._text("b")))
        assert source._adf_to_markdown(adf) == "a  \nb"

    def test_literal_markdown_in_text_is_escaped(self):
        adf = self._doc(self._para(self._text("**not bold** and <b>tag</b> and _u_")))
        result = source._adf_to_markdown(adf)
        assert result == r"\*\*not bold\*\* and \<b\>tag\</b\> and \_u\_"

    def test_literal_html_entity_in_text_is_escaped(self):
        """rehypeRaw would otherwise decode `&copy;` to a copyright sign."""
        adf = self._doc(self._para(self._text("&copy; 2026 &amp; friends")))
        assert source._adf_to_markdown(adf) == r"\&copy; 2026 \&amp; friends"

    def test_a_credential_in_text_is_redacted_by_the_converter_itself(self):
        secret = "Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4Yz7Bc0Ef3Gh6"
        adf = self._doc(self._para(self._text(f"use ghp_{secret} to clone")))
        assert secret not in source._adf_to_markdown(adf)

    def test_a_credential_in_an_attribute_is_redacted_inside_the_bounded_walk(self):
        """`_adf_attr_label` redacts before it escapes.

        Escaping would insert a backslash into `ghp_...` and hide it from the
        payload-level redactor that runs afterwards, and doing this inside the
        converter's depth-capped traversal is what avoids an unbounded pre-pass
        over a provider-controlled tree.
        """
        secret = "Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(
                {
                    "type": "media",
                    "attrs": {
                        "type": "external",
                        "url": "https://ex.com/a.png",
                        "alt": f"use ghp_{secret}",
                    },
                }
            )
        )
        assert secret not in source._adf_to_markdown(adf)

    def test_a_credential_split_across_marked_siblings_is_still_redacted(self):
        """A plain-text walk joins sibling text nodes seamlessly, so the payload
        redactor catches a credential spanning them. Marks would put delimiters
        between the halves and hide it, so an inline run whose own raw text
        carries a credential is emitted as one redacted string."""
        head, tail = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4", "Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(self._text(head), self._text(tail, [{"type": "strong"}]))
        )
        rendered = source._adf_to_markdown(source._redact_provider_data(adf))
        assert tail not in rendered
        assert head not in rendered

    def test_redacting_a_run_keeps_every_node_s_text(self):
        """The fallback emits the plain rendition of the WHOLE run, so a mention
        or card in the same paragraph keeps its text instead of disappearing."""
        secret = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(
                self._text(f"token {secret} for "),
                {"type": "mention", "attrs": {"text": "Alice"}},
                self._text(" see "),
                {"type": "inlineCard", "attrs": {"url": "https://example.com/doc"}},
            )
        )
        rendered = source._adf_to_markdown(adf)
        assert secret not in rendered
        assert "Alice" in rendered
        assert "https://example.com/doc" in rendered

    def test_a_credential_split_across_a_label_boundary_is_redacted(self):
        """An emoji label contributes text with no delimiter of its own, so a
        secret continued inside one is contiguous in the rendered output."""
        head, tail = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4", "Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(self._text(head), {"type": "emoji", "attrs": {"text": tail}})
        )
        rendered = source._adf_to_markdown(adf)
        assert head not in rendered
        assert tail not in rendered

    def test_a_credential_split_through_an_unknown_container_is_redacted(self):
        """An unrecognised inline container emits nothing of its own, so a
        plain-text walk joined the halves either side of it seamlessly. It has to
        stay inside the span the credential check reads."""
        head, tail = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4", "Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(
                self._text(head),
                {"type": "someFutureInline", "content": [self._text(tail)]},
            )
        )
        rendered = source._adf_to_markdown(adf)
        assert head not in rendered
        assert tail not in rendered

    def test_literal_math_syntax_is_escaped(self):
        """The same renderer runs remark-math, so a literal `$$x$$` would
        otherwise render as KaTeX instead of as the characters typed."""
        adf = self._doc(self._para(self._text("costs $$5 and $x$ too")))
        assert source._adf_to_markdown(adf) == r"costs \$\$5 and \$x\$ too"

    def test_a_credential_split_inside_an_unknown_container_is_redacted(self):
        """Both halves inside the container, the second one marked."""
        head, tail = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4", "Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(
                {
                    "type": "someFutureInline",
                    "content": [self._text(head), self._text(tail, [{"type": "strong"}])],
                }
            )
        )
        rendered = source._adf_to_markdown(adf)
        assert head not in rendered
        assert tail not in rendered

    def test_adjacent_identical_marks_inside_an_unknown_container_are_merged(self):
        adf = self._doc(
            self._para(
                {
                    "type": "someFutureInline",
                    "content": [
                        self._text("a", [{"type": "strong"}]),
                        self._text("b", [{"type": "strong"}]),
                    ],
                }
            )
        )
        assert source._adf_to_markdown(adf) == "**ab**"

    def test_a_literal_bang_cannot_splice_an_image_onto_a_link(self):
        """`!` before an emitted `[` would form image syntax, and an image
        auto-fetches the URL -- the exact beacon the media-as-link form avoids."""
        adf = self._doc(
            self._para(
                self._text("!"),
                {
                    "type": "media",
                    "attrs": {"type": "external", "url": "https://evil.example/beacon.png"},
                },
            )
        )
        rendered = source._adf_to_markdown(adf)
        assert rendered == r"\![https://evil.example/beacon.png](https://evil.example/beacon.png)"

    def test_a_wide_run_of_text_nodes_merges_in_one_pass(self):
        """Only traversal DEPTH is capped, so a provider can put hundreds of
        thousands of adjacent text nodes in one paragraph. The run's text is
        joined once rather than rebuilt per node."""
        n = 100000
        adf = self._doc(
            {"type": "paragraph", "content": [{"type": "text", "text": "a"} for _ in range(n)]}
        )
        assert source._adf_to_markdown(adf) == "a" * n

    def test_a_credential_split_deep_inside_nested_containers_is_redacted(self):
        """The scan runs once at the outermost run and inner containers reuse it,
        so the guarantee has to hold at depth, not just at the top level."""
        head, tail = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4", "Yz7Bc0Ef3Gh6"
        node = {
            "type": "someFutureInline",
            "content": [self._text(head), self._text(tail, [{"type": "strong"}])],
        }
        for _ in range(3):
            node = {"type": "someFutureInline", "content": [node]}
        rendered = source._adf_to_markdown(self._doc(self._para(node)))
        assert head not in rendered
        assert tail not in rendered

    def test_a_link_whose_href_fails_the_scan_emits_no_destination(self):
        """`_URL_RE` stops at `)`, so a paren in the path puts the whole query
        outside every exfiltration check. The href is scanned paren-encoded and
        the link is dropped rather than emitted partly redacted."""
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2"
        href = f"https://evil.example.com/a)b?data={blob}"
        adf = self._doc(
            self._para(self._text("click", [{"type": "link", "attrs": {"href": href}}]))
        )
        rendered = source._adf_to_markdown(adf)
        assert rendered == "click"
        assert blob not in rendered
        assert "evil.example.com" not in rendered

    def test_a_benign_url_with_parentheses_keeps_its_link(self):
        """The scan must not cost legitimate links: wiki and Confluence URLs
        carry parentheses routinely, and one is not evidence of anything."""
        for href in (
            "https://en.wikipedia.org/wiki/Salt_(chemistry)",
            "https://co.atlassian.net/wiki/spaces/X/pages/1/Plan_(v2)?focus=1",
        ):
            adf = self._doc(
                self._para(self._text("doc", [{"type": "link", "attrs": {"href": href}}]))
            )
            assert source._adf_to_markdown(adf) == f"[doc](<{href}>)"

    def test_a_layout_keeps_its_columns_as_blocks(self):
        """Markdown has no columns, so a layout flattens -- but its children are
        BLOCKS. Reaching the inline path concatenated them into `firstsecond`,
        losing both the separator and the heading's `##`."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "layoutSection",
                    "content": [
                        {
                            "type": "layoutColumn",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "first"}]}
                            ],
                        },
                        {
                            "type": "layoutColumn",
                            "content": [
                                {
                                    "type": "heading",
                                    "attrs": {"level": 2},
                                    "content": [{"type": "text", "text": "second"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        assert source._adf_to_markdown(adf) == "first\n\n## second"

    def test_a_bodied_extension_and_decision_list_keep_their_blocks(self):
        for container, item, expected in (
            ("bodiedExtension", "paragraph", "alpha\n\nbeta"),
            ("decisionList", "decisionItem", "alpha\n\nbeta"),
        ):
            adf = {
                "type": "doc",
                "content": [
                    {
                        "type": container,
                        "content": [
                            {"type": item, "content": [{"type": "text", "text": "alpha"}]},
                            {"type": item, "content": [{"type": "text", "text": "beta"}]},
                        ],
                    }
                ],
            }
            assert source._adf_to_markdown(adf) == expected

    def test_a_block_card_keeps_its_url(self):
        """A block-level card carries its URL in an attribute and has no content,
        so the inline fallthrough rendered it as the empty string -- the URL was
        lost outright, the same unrecoverable loss this change exists to fix."""
        for card in ("blockCard", "embedCard"):
            adf = {"type": "doc", "content": [{"type": card, "attrs": {"url": "https://e.com/p"}}]}
            assert source._adf_to_markdown(adf) == "[https://e.com/p](https://e.com/p)"

    def test_no_node_type_emits_an_attribute_without_passing_a_gate(self):
        """Redaction lives at two chokepoints -- `_adf_attr_label` for attribute
        text and `_adf_url_link` for a URL destination. That invariant is
        conventional unless something checks it, so the node types and attribute
        names are read back OUT of the module and every combination is tried:
        a type added later is covered without anyone remembering this test."""
        src = inspect.getsource(source)
        body = src[src.index("def _adf_block_to_markdown") : src.index("def _adf_plain_text")]
        attr_names = set(re.findall(r'attrs\.get\("([a-zA-Z]+)"\)', body))
        node_types = set(source._ADF_BLOCK_TYPES) | set(
            re.findall(r'node_type (?:==|in \()\s*"([a-zA-Z]+)"', body)
        )
        assert "url" in attr_names and len(node_types) > 10, (attr_names, node_types)

        secret = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4Yz7Bc0Ef3Gh6"
        leaked = []
        for node_type in sorted(node_types):
            node = {
                "type": node_type,
                "attrs": {name: secret for name in attr_names},
                "content": [{"type": "text", "text": "body"}],
            }
            rendered = source._adf_to_markdown({"type": "doc", "content": [node]})
            if secret in rendered.replace("\\", ""):
                leaked.append(node_type)
        assert not leaked, f"attribute reached the document unredacted for: {leaked}"

    def test_overlapping_adjacent_marks_stay_unambiguous(self):
        """Wrapping each node on its own emitted `**a*****b****c*`, which a
        CommonMark parser reads as strong(a), a LITERAL `***b***`, then em(c):
        the delimiters showed as text and the middle node lost both marks. Each
        expectation below was checked through a parser, not reasoned about.
        """

        def t(txt, *kinds):
            return self._text(txt, [{"type": k} for k in kinds])

        # `**a*b***_c_`   -> <strong>a<em>b</em></strong><em>c</em>
        # `_a**b**_**c**` -> <em>a<strong>b</strong></em><strong>c</strong>
        # `**a**_b_**c**` -> <strong>a</strong><em>b</em><strong>c</strong>
        for nodes, expected in (
            ((t("a", "strong"), t("b", "strong", "em"), t("c", "em")), "**a*b***_c_"),
            ((t("a", "em"), t("b", "em", "strong"), t("c", "strong")), "_a**b**_**c**"),
            ((t("a", "strong"), t("b", "em"), t("c", "strong")), "**a**_b_**c**"),
        ):
            rendered = source._adf_to_markdown(self._doc(self._para(*nodes)))
            assert rendered == expected
            # No delimiter run longer than the three of a nested strong+em.
            assert "****" not in rendered

    def test_an_unrenderable_emphasis_is_dropped_not_corrupted(self):
        """When a run needs `*` at one end and `_` at the other -- an em that
        both abuts an asterisk run and is followed by a word -- neither spelling
        works. The mark is dropped and the text kept, because emitting a
        delimiter anyway would show it as content AND lose the italic."""

        def t(txt, *kinds):
            return self._text(txt, [{"type": k} for k in kinds])

        rendered = source._adf_to_markdown(
            self._doc(
                self._para(
                    t("a", "strong"), t("b", "strong", "em"), t("c", "em"), t("d")
                )
            )
        )
        assert rendered == "**a*b***cd"
        assert "_" not in rendered

    def test_a_language_with_a_trailing_newline_is_rejected(self):
        """`$` also matches just before a trailing newline, so `re.match` accepted
        `"python\\n"` and the fence emitted a blank first line inside the block."""
        adf = self._doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python\n"},
                "content": [{"type": "text", "text": "body"}],
            }
        )
        assert source._adf_to_markdown(adf) == "```\nbody\n```"

    def test_a_wide_alternating_mark_run_stays_unambiguous(self):
        """The emitter looks at the character before each mark, so it must carry
        one character forward rather than the accumulated output -- passing the
        prefix made it quadratic (13.85s for 100k nodes against 0.72s)."""
        nodes = []
        for i in range(3000):
            kinds = (("strong",), ("strong", "em"), ("em",))[i % 3]
            nodes.append(self._text(f"w{i}", [{"type": k} for k in kinds]))
        rendered = source._adf_to_markdown(self._doc({"type": "paragraph", "content": nodes}))
        # Four in a row is the signature of two delimiter runs that have merged.
        assert "****" not in rendered
        assert "w2999" in rendered

    def test_a_code_body_round_trips_its_own_trailing_newlines(self):
        """The newline before the closing fence SEPARATES the body from it. Adding
        it unconditionally changed the content: a source ending in one newline came
        back with two, and an empty body became a block holding a blank line."""
        for body, expected in (
            ("x", "```\nx\n```"),
            ("x\n", "```\nx\n```"),
            ("", "```\n```"),
            ("x\n\n", "```\nx\n\n```"),
            ("a\nb", "```\na\nb\n```"),
        ):
            adf = self._doc(
                {
                    "type": "codeBlock",
                    "content": [{"type": "text", "text": body}] if body else [],
                }
            )
            assert source._adf_to_markdown(adf) == expected

    def test_an_explicit_zero_start_is_preserved(self):
        """ADF allows `order: 0` and CommonMark honours it as `<ol start="0">`.
        Coercing with `or 1` silently renumbered the list from 1."""
        adf = self._doc(
            {
                "type": "orderedList",
                "attrs": {"order": 0},
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": t}]}
                        ],
                    }
                    for t in ("a", "b")
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "0. a\n1. b"

    def test_an_out_of_range_list_start_falls_back(self):
        """A marker of more than nine digits is not a list start: CommonMark reads
        `1000000000. a` as a paragraph, so the whole list would render as literal
        text. The last item's marker is what has to fit."""
        for order, expected_first in ((10**9, "1."), (999999999, "1."), (999999998, "999999998.")):
            adf = self._doc(
                {
                    "type": "orderedList",
                    "attrs": {"order": order},
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": t}]}
                            ],
                        }
                        for t in ("a", "b")
                    ],
                }
            )
            assert source._adf_to_markdown(adf).startswith(expected_first)

    def test_a_non_integer_order_falls_back_to_one(self):
        for order in ("3", True, -1, None, 1.5):
            adf = self._doc(
                {
                    "type": "orderedList",
                    "attrs": {"order": order},
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "a"}]}
                            ],
                        }
                    ],
                }
            )
            assert source._adf_to_markdown(adf) == "1. a"

    def test_every_scan_terminating_character_is_covered(self):
        """`_URL_RE`'s path class is `[^\\s)\\"'>]*`, so a character from it ends
        the match and puts the rest of the query outside every exfiltration check.

        A link DESTINATION is scanned as one address, whitespace included, because
        that is what gets emitted: the angle-bracket form percent-encodes a space,
        so a space does not end the URL there. All ten characters are therefore
        sealed on this path -- each one measured as leaking before.
        """
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2"
        for ch in (")", "'", '"', ">", " ", "\t", "\n", "\r", "\v", "\f"):
            href = f"https://evil.example.com/a{ch}b?data={blob}"
            adf = self._doc(
                self._para(self._text("click", [{"type": "link", "attrs": {"href": href}}]))
            )
            rendered = source._adf_to_markdown(adf)
            assert blob not in rendered, f"leaked past {ch!r}"
            assert "evil.example.com" not in rendered

    def test_a_url_followed_by_prose_is_left_alone(self):
        """In PROSE a space really does end the URL -- verified against the real
        renderer, where `https://host/a?data= <blob>` yields an anchor whose href
        is `https://host/a?data=`, so following text cannot ride along in a
        fetchable address. Encoding whitespace here anyway treated a URL and the
        next word as one address: a URL followed by a 40-character commit SHA
        scanned as a query carrying the SHA, the entropy heuristic fired, and the
        whole paragraph became `see%20[REDACTED: suspicious URL ...]` with every
        word after the URL gone."""
        sha = "9f2c1ab4de5607893bcf24e01a7d6b3958e04c12"
        text = f"see https://example.com/pr?id=7 {sha} for detail"
        rendered = source._adf_to_markdown(self._doc(self._para(self._text(text))))
        assert rendered == text
        assert "%20" not in rendered and "REDACTED" not in rendered

    def test_a_benign_url_with_an_apostrophe_keeps_its_link(self):
        """The scan must not cost legitimate links: a page title with an
        apostrophe is ordinary, and encoding it is only for the scan."""
        href = "https://co.atlassian.net/wiki/spaces/X/pages/1/Bob's_Plan?focus=1"
        adf = self._doc(self._para(self._text("doc", [{"type": "link", "attrs": {"href": href}}])))
        assert source._adf_to_markdown(adf) == f"[doc]({href})"

    @staticmethod
    def _table_row(*cells):
        return {
            "type": "tableRow",
            "content": [
                {
                    "type": "tableCell",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": c}]}],
                }
                for c in cells
            ],
        }

    def test_a_row_wider_than_the_header_keeps_its_cells(self):
        """GFM fixes the table width at the HEADER and DROPS a longer row's excess
        -- the text is gone, not wrapped. Verified against a GFM parser: under a
        two-column header, `| c | d | e | f |` renders only c and d."""
        adf = self._doc(
            {
                "type": "table",
                "content": [self._table_row("a", "b"), self._table_row("c", "d", "e", "f")],
            }
        )
        rendered = source._adf_to_markdown(adf)
        assert rendered == "| a | b |  |  |\n| --- | --- | --- | --- |\n| c | d | e | f |"

    def test_widening_the_header_does_not_pad_every_row(self):
        """Only the header and separator grow. Padding every row to the widest is
        what made this quadratic before: one wide row among many narrow ones cost
        rows x width cells for the handful actually carried."""
        rows = [self._table_row(*[f"c{i}" for i in range(400)])]
        rows.extend(self._table_row("x") for _ in range(400))
        rendered = source._adf_to_markdown(self._doc({"type": "table", "content": rows}))
        # Header + separator carry 400 each; the 400 narrow rows carry one apiece.
        assert rendered.count("|") < 2500

    def test_the_escape_set_is_pinned_to_the_renderers_plugins(self):
        """This escape set is DERIVED from the plugins the panel's renderer runs:
        `$` is escaped because remark-math is in that stack, `!` because rehypeRaw
        admits an image that would auto-fetch, `~` because of GFM strikethrough.
        The coupling crosses a language boundary, so adding a remark plugin with
        new syntax would reopen a hole here with nothing going red.

        This pins the stack rather than the conclusion: if the list changes,
        re-derive the escape set and only then update this test. It is the shared
        contract Design Review asked for, in the one place that can fail.
        """
        renderer = (
            pathlib.Path(__file__).resolve().parents[1]
            / "website"
            / "src"
            / "components"
            / "MarkdownRenderer.tsx"
        )
        if not renderer.is_file():
            pytest.skip("frontend renderer is not part of this checkout")
        imported = set(re.findall(r"from '((?:remark|rehype)-[a-z0-9-]+)'", renderer.read_text()))
        assert imported == {
            "remark-parse",
            "remark-gfm",
            "remark-math",
            "remark-cjk-friendly",
            "remark-cjk-friendly-gfm-strikethrough",
            "rehype-raw",
            "rehype-katex",
        }, "renderer plugin stack changed -- re-derive the escape set in _MD_INLINE_ESCAPE"

    def test_the_shared_safety_fixture_matches_the_converter(self):
        """Half of a contract the FRONTEND test asserts the other half of.

        This side pins that the converter turns each fixture `adf` into exactly
        that `markdown`, so the fixture cannot drift from the converter. The
        frontend side renders the same `markdown` through the real
        MarkdownRenderer plugin stack and asserts the selectors, which is what
        checks the escape set against the renderer that actually runs instead of
        against a comment describing it.
        """
        import json

        fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "adf_markdown_safety.json"
        cases = json.loads(fixture.read_text())["cases"]
        assert len(cases) >= 6
        for case in cases:
            rendered = source._adf_to_markdown(case["adf"])
            assert rendered == case["markdown"], case["name"]
            if case.get("forbidText"):
                assert case["forbidText"] not in rendered.replace("\\", ""), case["name"]

    def test_no_emission_site_scans_a_truncated_url(self):
        """The truncation gap is per-CALL-SITE, not per-node-type. When only the
        link destination normalised the URL before scanning, an expand title, a
        mention label, an inline card and a media URL each still leaked a
        high-entropy query -- measured one by one. All of them now go through the
        one redaction primitive."""
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2"
        url = f"https://evil.example.com/a)b?data={blob}"
        sites = {
            "expand title": self._doc(
                {
                    "type": "expand",
                    "attrs": {"title": f"see {url}"},
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "x"}]}
                    ],
                }
            ),
            "mention label": self._doc(
                self._para({"type": "mention", "attrs": {"text": url}})
            ),
            "inline card": self._doc(self._para({"type": "inlineCard", "attrs": {"url": url}})),
            "media url": self._doc(
                self._para({"type": "media", "attrs": {"type": "external", "url": url}})
            ),
            "link destination": self._doc(
                self._para(self._text("c", [{"type": "link", "attrs": {"href": url}}]))
            ),
        }
        for name, adf in sites.items():
            rendered = source._adf_to_markdown(adf)
            assert blob not in rendered, name

    def test_redaction_leaves_ordinary_text_byte_identical(self):
        """The scan form is only a scan form: when nothing is found the original
        text is emitted, so no percent escape shows up in the common case."""
        adf = self._doc(
            self._para(self._text("see https://example.com/a(b)c?page=2 and 'x' > y"))
        )
        rendered = source._adf_to_markdown(adf)
        assert "%28" not in rendered and "%29" not in rendered and "%27" not in rendered
        assert "https://example.com/a(b)c?page=2" in rendered

    def test_a_bare_url_in_prose_is_scanned(self):
        """This is the case that actually linkifies. Verified against the real
        renderer: bare text `https://host/a)b?data=<blob>` becomes an anchor whose
        href carries the paren AND the whole query, so the truncated scan has to
        be corrected here or a fetchable address reaches the panel."""
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2"
        text = f"look at https://evil.example.com/a)b?data={blob} please"
        rendered = source._adf_to_markdown(self._doc(self._para(self._text(text))))
        assert blob not in rendered
        # The marker names the host on purpose, so the reader knows what went.
        assert "REDACTED: suspicious URL to evil.example.com" in rendered
        assert rendered.startswith("look at ") and rendered.endswith(" please")

    def test_a_code_block_url_is_not_a_link_so_it_is_not_url_scanned(self):
        """A code body deliberately does NOT get the URL-entropy scan.

        Verified against the real renderer: for a fenced block and for an inline
        code span the rendered output has zero anchors and zero images, so a URL
        in code is text and not a fetchable address -- the same reasoning that
        makes whitespace end a URL in prose. Running the entropy heuristic here
        would replace legitimate code samples (an API example with a long opaque
        token reads exactly like an exfiltration query) for no reachable gain.

        Credentials are still covered: the payload-level pass is token-shaped, so
        it catches `ghp_...` inside a code block regardless of any URL truncation.
        """
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2"
        url = f"https://api.example.com/v1)x?token={blob}"
        adf = self._doc(
            {"type": "codeBlock", "content": [{"type": "text", "text": f"curl {url}"}]}
        )
        assert source._adf_to_markdown(adf) == f"```\ncurl {url}\n```"

    def test_a_credential_split_across_a_media_alt_is_redacted(self):
        """This supersedes an earlier, narrower claim of mine.

        In round 16 I rebutted this by showing the emitted `[alt](url)` brackets
        the alt, so the halves cannot form one token. That was true of the code at
        the time. Round 17 then added a path where a URL failing the destination
        scan drops the link and emits the LABEL ALONE -- no brackets -- and the
        rebuttal quietly stopped holding. Measured on that path, the output was
        `ghp\\_Ab3Df6Hj9Kl2Np5Qr8TvWx4Yz7Bc0Ef3`: one recoverable credential, with
        the backslash from escaping `ghp_` defeating the payload-level pass.

        So the run gate now reads the media ALT rather than its URL. It sees the
        contiguity the output can actually have, and errs toward MORE contiguity
        than the output has when the brackets do survive, which is the safe way to
        be wrong.
        """
        head = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv"
        tail = "Wx4Yz7Bc0Ef3"
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2"
        for url in (
            "https://ex.com/a.png",
            # Fails the destination scan (whitespace is encoded there), so the
            # link is dropped and the alt would be emitted bare.
            f"https://evil.example.com/a b?data={blob}",
        ):
            adf = self._doc(
                self._para(
                    self._text(head),
                    {"type": "media", "attrs": {"type": "external", "url": url, "alt": tail}},
                )
            )
            rendered = source._adf_to_markdown(adf)
            assert (head + tail) not in rendered.replace("\\", ""), url
            assert "REDACTED: credential" in rendered, url

    def test_an_ordinary_media_alt_still_renders_as_a_link(self):
        """The gate reading the alt must not cost the ordinary case."""
        adf = self._doc(
            self._para(
                self._text("before "),
                {
                    "type": "media",
                    "attrs": {"type": "external", "url": "https://ex.com/a.png", "alt": "shot"},
                },
            )
        )
        assert source._adf_to_markdown(adf) == "before [shot](https://ex.com/a.png)"

    def test_line_leading_list_marker_in_text_is_escaped(self):
        adf = self._doc(self._para(self._text("- not a list")), self._para(self._text("1. nor this")))
        assert source._adf_to_markdown(adf) == "\\- not a list\n\n1\\. nor this"

    def test_a_setext_underline_in_text_cannot_promote_the_line_above(self):
        """A line of `=` or `-` under a paragraph line makes it a heading, so both
        underline characters have to be escaped, not just the list-marker one."""
        adf = self._doc(self._para(self._text("Title\n===")), self._para(self._text("Sub\n---")))
        assert source._adf_to_markdown(adf) == "Title\n\\===\n\nSub\n\\---"

    def test_pipe_in_a_table_cell_is_escaped(self):
        adf = self._doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableHeader",
                                "content": [self._para(self._text("a|b"))],
                            }
                        ],
                    }
                ],
            }
        )
        assert source._adf_to_markdown(adf).startswith("| a\\|b |")

    def test_line_expansion_guard_refuses_a_projected_overflow(self):
        """Per-line expansion is checked by projection, before any allocation."""
        many = "x\n" * 100000
        with pytest.raises(source.SourceProviderError):
            source._md_guard_line_expansion(many, 120)
        # The same text with a two-character indent projects well under the cap.
        source._md_guard_line_expansion(many, 2)

    def test_deeply_nested_quotes_over_the_ceiling_are_refused_not_rendered(self):
        """Newlines inside one text node cost ~3 payload bytes each while 60
        levels of nesting adds 120 characters to every one, so a small document
        can project past the payload ceiling. It must raise, not allocate."""
        inner = {"type": "paragraph", "content": [{"type": "text", "text": "x\n" * 100000}]}
        node = {"type": "blockquote", "content": [inner]}
        for _ in range(59):
            node = {"type": "blockquote", "content": [node]}
        with pytest.raises(source.SourceProviderError):
            source._adf_to_markdown(self._doc(node))

    def test_nested_blockquotes_get_one_marker_per_level(self):
        """A chain of single-child quotes is prefixed in one pass, so the marker
        count must still match the nesting depth."""
        node = {"type": "blockquote", "content": [self._para(self._text("deep"))]}
        for _ in range(2):
            node = {"type": "blockquote", "content": [node]}
        assert source._adf_to_markdown(self._doc(node)) == "> > > deep"

    def test_code_span_whitespace_survives_cell_flattening(self):
        """A cell is folded to one line by collapsing NEWLINES only -- a code
        span's repeated spaces are literal content."""
        adf = self._doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableHeader",
                                "content": [
                                    self._para(self._text("a  b", [{"type": "code"}]))
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        assert source._adf_to_markdown(adf).startswith("| `a  b` |")

    def test_pipe_inside_a_code_span_in_a_cell_is_escaped(self):
        """A code span is emitted literally, so its pipe would split the cell.
        GFM honours a backslash-escaped pipe inside a code span."""
        adf = self._doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableHeader",
                                "content": [
                                    self._para(self._text("a|b", [{"type": "code"}]))
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        assert source._adf_to_markdown(adf).startswith("| `a\\|b` |")

    def test_empty_and_non_dict_returns_empty(self):
        assert source._adf_to_markdown(None) == ""
        assert source._adf_to_markdown("just a string") == ""
        assert source._adf_to_markdown({}) == ""

    def test_unknown_node_type_still_contributes_its_text(self):
        adf = self._doc(
            {"type": "someFutureNode", "content": [self._text("kept")]},
        )
        assert source._adf_to_markdown(adf) == "kept"

    def test_traversal_is_depth_limited(self):
        def nest(levels):
            node = self._para(self._text("deep"))
            for _ in range(levels):
                node = {"type": "blockquote", "content": [node]}
            return self._doc(node)

        assert "deep" in source._adf_to_markdown(nest(3))
        assert "deep" not in source._adf_to_markdown(nest(200))

    @staticmethod
    def _nest(container, levels):
        """Wrap a 'deep' paragraph in *levels* nested *container* blocks."""
        node = {"type": "paragraph", "content": [{"type": "text", "text": "deep"}]}
        for _ in range(levels):
            if container == "blockquote":
                node = {"type": "blockquote", "content": [node]}
            elif container == "taskList":
                node = {"type": "taskList", "content": [{"type": "taskItem", "content": [node]}]}
            elif container == "table":
                node = {
                    "type": "table",
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [{"type": "tableCell", "content": [node]}],
                        }
                    ],
                }
            elif container == "someFutureInline":
                node = {"type": "paragraph", "content": [{"type": "someFutureInline", "content": [node]}]}
            elif container in ("layoutSection", "bodiedExtension", "decisionList"):
                node = {"type": container, "content": [node]}
            else:
                node = {"type": container, "content": [{"type": "listItem", "content": [node]}]}
        return {"type": "doc", "content": [node]}

    @pytest.mark.parametrize(
        "container",
        [
            "blockquote",
            "bulletList",
            "orderedList",
            "taskList",
            "table",
            "someFutureInline",
            "layoutSection",
            "bodiedExtension",
            "decisionList",
        ],
    )
    def test_every_nesting_container_respects_the_depth_limit(self, container):
        """No recursing container may reach its renderer past the guarded entry.

        The depth cap lives in `_adf_to_markdown`, so a container whose renderer
        recursed straight back into the block renderer would skip the cap and
        exhaust the stack on a deeply nested document.
        """
        assert "deep" in source._adf_to_markdown(self._nest(container, 3))
        assert "deep" not in source._adf_to_markdown(self._nest(container, 350))

    def test_realistic_description_round_trips_to_markdown(self):
        """One document exercising every structure a Jira description carries."""
        adf = self._doc(
            {"type": "heading", "attrs": {"level": 2}, "content": [self._text("Problem")]},
            self._para(
                self._text("The "),
                self._text("fetch_issue", [{"type": "code"}]),
                self._text(" helper drops "),
                self._text("every", [{"type": "strong"}]),
                self._text(" mark."),
            ),
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [self._para(self._text("headings"))]},
                    {
                        "type": "listItem",
                        "content": [
                            self._para(
                                self._text("links like "),
                                self._text(
                                    "the docs",
                                    [
                                        {
                                            "type": "link",
                                            "attrs": {"href": "https://example.com/docs"},
                                        }
                                    ],
                                ),
                            )
                        ],
                    },
                ],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [self._text("x = 1")],
            },
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [self._para(self._text("a"))]},
                            {"type": "tableHeader", "content": [self._para(self._text("b"))]},
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [self._para(self._text("1"))]},
                            {"type": "tableCell", "content": [self._para(self._text("2"))]},
                        ],
                    },
                ],
            },
            {"type": "rule"},
            self._para(self._text("See "), {"type": "mention", "attrs": {"text": "Alice"}}),
        )
        expected = "\n".join(
            [
                "## Problem",
                "",
                "The `fetch_issue` helper drops **every** mark.",
                "",
                "- headings",
                "- links like [the docs](https://example.com/docs)",
                "",
                "```python",
                "x = 1",
                "```",
                "",
                "| a | b |",
                "| --- | --- |",
                "| 1 | 2 |",
                "",
                "---",
                "",
                "See @Alice",
            ]
        )
        assert source._adf_to_markdown(adf) == expected


class TestGetJiraAuth:
    """Credential resolution for Jira hosts."""

    def test_returns_none_when_no_config(self, monkeypatch):
        """No entries → None."""

        class FakeDashboard:
            jira_auth = []

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        assert source._get_jira_auth("acme.atlassian.net") is None

    def test_matches_host_case_insensitively(self, monkeypatch):
        class FakeEntry:
            host = "Acme.atlassian.net"
            email = "user@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "secret123"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("user@acme.com", "secret123")

    def test_strips_port_443(self, monkeypatch):
        class FakeEntry:
            host = "jira.internal:443"
            email = ""

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "pat-token"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        result = source._get_jira_auth("jira.internal")
        assert result == ("", "pat-token")

    def test_raises_value_error_on_config_load_failure(self, monkeypatch):
        """Config errors propagate as ValueError, not silent None."""

        class BrokenConfig:
            @classmethod
            def load(cls):
                raise RuntimeError("corrupt config.json")

        monkeypatch.setattr(source, "KiroCrewConfig", BrokenConfig)
        with pytest.raises(ValueError, match="jira_config_error"):
            source._get_jira_auth("acme.atlassian.net")

    def test_per_host_token_takes_precedence(self, monkeypatch):
        """JIRA_TOKEN_<hex> is preferred over global JIRA_API_TOKEN."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                host_key = "acme.atlassian.net".encode().hex().upper()
                return {
                    "JIRA_API_TOKEN": "global-fallback",
                    f"JIRA_TOKEN_{host_key}": "per-host-secret",
                }

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "per-host-secret")

    def test_seeded_global_env_does_not_bypass_per_host_vault(self, monkeypatch):
        """A global JIRA_API_TOKEN that load_credentials merely SEEDED into
        os.environ (setdefault), not a real pre-existing operator override,
        must NOT be treated as a live override: a host with its own per-host
        vault token still gets that per-host token. The snapshot is captured
        BEFORE load_credentials runs, so a value that did not exist in the
        environment beforehand is not seen as an override."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        # No REAL operator override present before the call.
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                # Simulate load_credentials' setdefault seeding the .env global
                # into the process environment during the call. Use monkeypatch
                # so the seed is auto-reverted at test teardown and cannot leak
                # into later tests (a raw os.environ.setdefault would persist).
                monkeypatch.setenv("JIRA_API_TOKEN", "seeded-global")
                return {"JIRA_API_TOKEN": "seeded-global"}

        host_key = "acme.atlassian.net".encode().hex().upper()
        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "per-host-vault" if name == f"JIRA_TOKEN_{host_key}" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        # Per-host vault token wins; the seeded global is ignored.
        assert result == ("dev@acme.com", "per-host-vault")

    def test_vault_token_preferred_over_env(self, monkeypatch):
        """A vault secret wins over the legacy .env value for the same host."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "env-token"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        host_key = "acme.atlassian.net".encode().hex().upper()
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "vault-token" if name == f"JIRA_TOKEN_{host_key}" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "vault-token")

    def test_vault_miss_falls_back_to_env(self, monkeypatch):
        """When the vault has no entry, the .env / environ value is used."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "env-token"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(source, "_resolve_jira_token_from_vault", lambda name: "")
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "env-token")

    def test_vault_single_host_global_token(self, monkeypatch):
        """Single host with no per-host vault entry uses the global vault secret."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "vault-global" if name == "JIRA_API_TOKEN" else "",
        )
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "vault-global")

    def test_global_env_override_beats_stale_global_vault(self, monkeypatch):
        """A nonempty process-environment JIRA_API_TOKEN overrides even a stale
        global vault entry.

        `load_credentials` overlays `os.environ` over the .env for this key, so
        a live env var is the effective credential — and `secrets import` skips
        migrating the key while such an override is set. A vault entry left by an
        EARLIER migration must NOT shadow that override under vault-first
        resolution. Per-host keys are unaffected (not env-overlaid)."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                # env overlay would also place it here; the resolver reads the
                # override directly from os.environ before the global vault.
                return {"JIRA_API_TOKEN": "env-override"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        # Stale global vault entry that must NOT win.
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "stale-vault" if name == "JIRA_API_TOKEN" else "",
        )
        monkeypatch.setenv("JIRA_API_TOKEN", "env-override")
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "env-override")

    def test_migrated_secret_ref_in_env_resolves_from_vault_not_uri(self, monkeypatch):
        """After `secrets import --apply`, the .env line is
        `JIRA_API_TOKEN=secret://JIRA_API_TOKEN` and `load_credentials`
        propagates that ref into os.environ AND the creds dict. The resolver
        must NOT hand the `secret://` URI to Jira as the token — it must treat
        it as a vault reference and resolve the real secret from the vault."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                # load_credentials overlays the migrated secret:// ref here too.
                return {"JIRA_API_TOKEN": "secret://JIRA_API_TOKEN"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "real-vault-secret" if name == "JIRA_API_TOKEN" else "",
        )
        # The migrated ref is propagated into the environment by load_credentials.
        monkeypatch.setenv("JIRA_API_TOKEN", "secret://JIRA_API_TOKEN")
        result = source._get_jira_auth("acme.atlassian.net")
        # The vault secret is used — NOT the secret:// URI.
        assert result == ("dev@acme.com", "real-vault-secret")

    def test_returns_none_when_no_token_anywhere(self, monkeypatch):
        """Configured host but neither vault nor env holds a token → None."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(source, "_resolve_jira_token_from_vault", lambda name: "")
        assert source._get_jira_auth("acme.atlassian.net") is None

    def test_env_override_equal_to_env_file_is_not_genuine_override(self, monkeypatch):
        """When os.environ['JIRA_API_TOKEN'] equals the .env file value (i.e. it
        was seeded there by GatewayOrchestrator's startup load_credentials call),
        it must NOT beat a vault entry — the vault's rotated value should win.

        This is the Finding 2 fix: a value that merely came from .env via
        load_credentials' setdefault is NOT a genuine operator override.
        """

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        _ENV_FILE_TOKEN = "stale-env-token"
        _VAULT_TOKEN = "fresh-vault-token"

        monkeypatch.setenv("JIRA_API_TOKEN", _ENV_FILE_TOKEN)

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": _ENV_FILE_TOKEN}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        # Per-host vault returns nothing; global vault has the rotated token.
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: (
                _VAULT_TOKEN if name == "JIRA_API_TOKEN" else ""
            ),
        )
        # .env file contains the same value as os.environ (startup-seeded).
        monkeypatch.setattr(
            source,
            "read_env_file_credential",
            lambda key: _ENV_FILE_TOKEN if key == "JIRA_API_TOKEN" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        # Vault token must win; the .env-seeded env value must NOT override it.
        assert result == ("dev@acme.com", _VAULT_TOKEN), (
            "Vault token should win when env value equals .env file value "
            f"(startup-seeded); got {result}"
        )

    def test_env_override_differing_from_env_file_is_genuine_override(self, monkeypatch):
        """When os.environ['JIRA_API_TOKEN'] DIFFERS from the .env file value,
        the operator explicitly set it at runtime — it must beat the vault entry.
        """

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        _ENV_FILE_TOKEN = "stale-env-token"
        _OPERATOR_TOKEN = "operator-set-at-runtime"
        _VAULT_TOKEN = "vault-token"

        monkeypatch.setenv("JIRA_API_TOKEN", _OPERATOR_TOKEN)

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": _OPERATOR_TOKEN}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: _VAULT_TOKEN if name == "JIRA_API_TOKEN" else "",
        )
        # .env file contains a different (older) value — operator set a new one.
        monkeypatch.setattr(
            source,
            "read_env_file_credential",
            lambda key: _ENV_FILE_TOKEN if key == "JIRA_API_TOKEN" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        # The differing env value is a genuine override; it must win over vault.
        assert result == ("dev@acme.com", _OPERATOR_TOKEN), (
            "Operator runtime override should win over vault when it differs "
            f"from .env file value; got {result}"
        )


class TestJiraIsCloud:
    def test_cloud_host(self):
        assert source._jira_is_cloud("acme.atlassian.net") is True

    def test_server_host(self):
        assert source._jira_is_cloud("jira.internal.corp") is False

    def test_subdomain_required_for_cloud(self):
        # The URL parser already rejects bare .atlassian.net, but _jira_is_cloud
        # is about suffix matching, not URL parsing. Any valid Cloud host has a
        # non-empty org prefix.
        assert source._jira_is_cloud("x.atlassian.net") is True


@pytest.mark.asyncio
async def test_fetch_jira_issue_no_credentials_raises_value_error(monkeypatch):
    """When no credentials are configured, a ValueError with the expected prefix is raised."""
    monkeypatch.setattr(source, "_get_jira_auth", lambda host: None)
    ref = source.SourceRef(
        provider="jira",
        url="https://acme.atlassian.net/browse/PROJ-123",
        host="acme.atlassian.net",
        owner="",
        repo="PROJ",
        number=123,
        kind="issue",
    )
    with pytest.raises(ValueError, match="jira_no_credentials"):
        await source._fetch_jira_issue(ref)


def test_parse_jira_cloud_url() -> None:
    """Jira Cloud URLs parse correctly."""
    ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-42")
    assert ref.provider == "jira"
    assert ref.host == "acme.atlassian.net"
    assert ref.repo == "PROJ"
    assert ref.number == 42
    assert ref.kind == "issue"
    assert ref.url == "https://acme.atlassian.net/browse/PROJ-42"


def test_parse_jira_self_hosted_url(monkeypatch) -> None:
    """Self-hosted Jira URLs parse when host is in the allowlist."""
    monkeypatch.setattr(source, "_allowed_jira_hosts", lambda: frozenset({"jira.internal.corp"}))
    ref = source.parse_source_url("https://jira.internal.corp/browse/TEAM-99")
    assert ref.provider == "jira"
    assert ref.host == "jira.internal.corp"
    assert ref.repo == "TEAM"
    assert ref.number == 99


# --- Jira linked issues (issue #2584) ---


class TestJiraLinkedChanges:
    """Tests for _jira_linked_changes parsing."""

    def test_outward_link_parsed(self) -> None:
        """An outward issue link is parsed with the correct relation label."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    "outwardIssue": {
                        "key": "PROJ-456",
                        "fields": {
                            "summary": "Blocked task",
                            "status": {"statusCategory": {"key": "new"}},
                        },
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert len(result) == 1
        assert result[0]["provider"] == "jira"
        assert result[0]["url"] == "https://acme.atlassian.net/browse/PROJ-456"
        assert result[0]["number"] == 456
        assert result[0]["title"] == "Blocked task"
        assert result[0]["state"] == "open"
        assert result[0]["relation"] == "blocks"
        assert result[0]["issueKey"] == "PROJ-456"

    def test_inward_link_parsed(self) -> None:
        """An inward issue link is parsed with the inward relation label."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    "inwardIssue": {
                        "key": "TEAM-10",
                        "fields": {
                            "summary": "Upstream dep",
                            "status": {"statusCategory": {"key": "indeterminate"}},
                        },
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://jira.corp/jira")
        assert len(result) == 1
        assert result[0]["relation"] == "is blocked by"
        assert result[0]["url"] == "https://jira.corp/jira/browse/TEAM-10"
        assert result[0]["state"] == "open"
        assert result[0]["issueKey"] == "TEAM-10"

    def test_done_status_maps_to_closed(self) -> None:
        """A linked issue with statusCategory 'done' maps to state 'closed'."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
                    "outwardIssue": {
                        "key": "FIX-7",
                        "fields": {
                            "summary": "Done fix",
                            "status": {"statusCategory": {"key": "done"}},
                        },
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert result[0]["state"] == "closed"

    def test_duplicate_keys_deduped(self) -> None:
        """Duplicate issue keys are folded."""
        link = {
            "type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
            "outwardIssue": {
                "key": "DUP-1",
                "fields": {"summary": "Dup", "status": {"statusCategory": {"key": "new"}}},
            },
        }
        fields = {"issuelinks": [link, link]}
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert len(result) == 1

    def test_empty_issuelinks(self) -> None:
        """Empty or missing issuelinks returns an empty list."""
        assert source._jira_linked_changes({}, "https://x") == []
        assert source._jira_linked_changes({"issuelinks": []}, "https://x") == []

    def test_malformed_link_skipped(self) -> None:
        """A link with neither inwardIssue nor outwardIssue is skipped."""
        fields = {
            "issuelinks": [
                {"type": {"name": "Bad", "inward": "x", "outward": "y"}},
                "not a dict",
                None,
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert result == []

    def test_missing_summary_uses_key_as_title(self) -> None:
        """When summary is missing, the issue key is used as the title."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Rel", "inward": "r", "outward": "r"},
                    "outwardIssue": {
                        "key": "NO-SUM-1",
                        "fields": {"status": {"statusCategory": {"key": "new"}}},
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert result[0]["title"] == "NO-SUM-1"

    def test_multiple_links(self) -> None:
        """Multiple links are all returned in order."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    "outwardIssue": {
                        "key": "A-1",
                        "fields": {"summary": "First", "status": {"statusCategory": {"key": "new"}}},
                    },
                },
                {
                    "type": {"name": "Duplicates", "inward": "is duplicated by", "outward": "duplicates"},
                    "inwardIssue": {
                        "key": "B-2",
                        "fields": {"summary": "Second", "status": {"statusCategory": {"key": "done"}}},
                    },
                },
            ]
        }
        result = source._jira_linked_changes(fields, "https://x.atlassian.net")
        assert len(result) == 2
        assert result[0]["issueKey"] == "A-1"
        assert result[0]["relation"] == "blocks"
        assert result[1]["issueKey"] == "B-2"
        assert result[1]["relation"] == "is duplicated by"
        assert result[1]["state"] == "closed"


# --- Jira fix versions as the milestone equivalent (issue #2585) ---


class TestJiraFixVersionMilestone:
    """Tests for _jira_fix_versions and _jira_fix_version_milestone."""

    def test_unreleased_version_maps_to_open_milestone(self) -> None:
        """name -> title, releaseDate -> dueOn, unreleased -> open."""
        fields = {
            "fixVersions": [
                {"name": "2.4.0", "releaseDate": "2026-09-30", "released": False},
            ]
        }
        versions = source._jira_fix_versions(fields)
        assert len(versions) == 1
        assert source._jira_fix_version_milestone(versions[0]) == {
            "title": "2.4.0",
            "state": "open",
            "dueOn": "2026-09-30",
        }

    def test_released_version_maps_to_closed(self) -> None:
        """A shipped release is a closed milestone."""
        version = {"name": "2.3.0", "releaseDate": "2026-06-30", "released": True}
        assert source._jira_fix_version_milestone(version)["state"] == "closed"

    def test_archived_version_maps_to_closed(self) -> None:
        """An archived version no longer takes work, so it is not open."""
        version = {"name": "1.0.0", "released": False, "archived": True}
        assert source._jira_fix_version_milestone(version)["state"] == "closed"

    def test_missing_release_date_keeps_the_name(self) -> None:
        """A version with no release date still carries its target release."""
        version = {"name": "Next", "released": False}
        assert source._jira_fix_version_milestone(version) == {
            "title": "Next",
            "state": "open",
            "dueOn": "",
        }

    def test_locale_formatted_release_date_is_not_used(self) -> None:
        """userReleaseDate is locale-formatted, so only releaseDate feeds dueOn."""
        version = {"name": "3.0", "releaseDate": "2026-12-01", "userReleaseDate": "01/Dec/26"}
        assert source._jira_fix_version_milestone(version)["dueOn"] == "2026-12-01"

    def test_first_usable_version_wins_over_malformed_head(self) -> None:
        """A malformed or nameless leading entry does not hide a usable one."""
        fields = {
            "fixVersions": [
                "not a dict",
                None,
                {"name": "   "},
                {"name": "", "releaseDate": "2026-01-01"},
                {"name": "4.1", "releaseDate": "2026-11-05"},
            ]
        }
        versions = source._jira_fix_versions(fields)
        assert len(versions) == 1
        assert source._jira_fix_version_milestone(versions[0])["title"] == "4.1"

    def test_name_is_trimmed(self) -> None:
        """Surrounding whitespace in a version name is not rendered."""
        version = {"name": "  2.5.0  "}
        assert source._jira_fix_version_milestone(version)["title"] == "2.5.0"

    def test_no_fix_versions_yields_nothing_usable(self) -> None:
        """Missing, empty, and non-list fixVersions all yield no milestone."""
        assert source._jira_fix_versions({}) == []
        assert source._jira_fix_versions({"fixVersions": []}) == []
        assert source._jira_fix_versions({"fixVersions": None}) == []
        assert source._jira_fix_versions({"fixVersions": "1.0"}) == []

    def test_multiple_versions_are_all_returned(self) -> None:
        """The filter keeps every usable version so the caller can flag partial."""
        fields = {
            "fixVersions": [
                {"name": "2.4.0"},
                {"name": "2.5.0"},
            ]
        }
        assert [v["name"] for v in source._jira_fix_versions(fields)] == ["2.4.0", "2.5.0"]

    def test_milestone_keys_match_the_frontend_contract(self) -> None:
        """The mapped shape is exactly IssueMilestone: title, state, dueOn."""
        version = {"name": "2.4.0", "releaseDate": "2026-09-30"}
        assert set(source._jira_fix_version_milestone(version)) == {"title", "state", "dueOn"}


class TestJiraPickFixVersion:
    """Tests for _jira_pick_fix_version (issue #7595)."""

    def test_mixed_versions_prefer_the_unreleased_one(self) -> None:
        """A pending release wins over an already-shipped one ahead of it."""
        fix_versions = [
            {"name": "2.3.0", "released": True},
            {"name": "2.4.0", "released": False},
        ]
        chosen = source._jira_pick_fix_version(fix_versions)
        assert chosen is not None
        assert chosen["name"] == "2.4.0"

    def test_all_released_falls_back_to_the_first(self) -> None:
        """A ticket whose versions all shipped still shows one milestone."""
        fix_versions = [
            {"name": "2.2.0", "released": True},
            {"name": "2.3.0", "archived": True},
        ]
        chosen = source._jira_pick_fix_version(fix_versions)
        assert chosen is not None
        assert chosen["name"] == "2.2.0"

    def test_empty_list_yields_none(self) -> None:
        """No usable fix versions means no milestone."""
        assert source._jira_pick_fix_version([]) is None

    def test_archived_is_not_pending(self) -> None:
        """An archived-but-unreleased version does not count as pending."""
        fix_versions = [
            {"name": "1.0.0", "released": False, "archived": True},
            {"name": "1.1.0", "released": False},
        ]
        chosen = source._jira_pick_fix_version(fix_versions)
        assert chosen is not None
        assert chosen["name"] == "1.1.0"


class _JiraFakeContent:
    """Minimal ``StreamReader`` stand-in for the capped-response reader."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _n: int):
        if self._body:
            yield self._body


class _JiraFakeResponse:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.content = _JiraFakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _JiraRecordingSession:
    """Fake ``aiohttp.ClientSession`` that records the URL it was asked for."""

    def __init__(self, body: bytes, seen: list[str]) -> None:
        self._body = body
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def get(self, url, **_kwargs):
        self._seen.append(url)
        return _JiraFakeResponse(self._body)


def _jira_recording_session(monkeypatch, fields: dict) -> list[str]:
    """Arm a fake Jira endpoint serving *fields*; return the recorded URL list."""
    body = json.dumps({"fields": fields}).encode("utf-8")
    seen: list[str] = []
    monkeypatch.setattr(
        source.aiohttp, "ClientSession", lambda *a, **kw: _JiraRecordingSession(body, seen)
    )
    monkeypatch.setattr(source, "_get_jira_auth", lambda host: ("e@example.com", "tok"))
    return seen


async def _jira_fetch(monkeypatch, fields: dict) -> tuple[dict, list[str]]:
    """Drive ``_fetch_jira_issue`` over a canned payload; return it and the URLs."""
    seen = _jira_recording_session(monkeypatch, fields)
    ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-123")
    return await source._fetch_jira_issue(ref), seen


class TestJiraFixVersionInPayload:
    """End-to-end pins: the field is requested and reaches the payload."""

    @pytest.mark.asyncio
    async def test_fix_versions_is_requested_from_the_rest_api(self, monkeypatch) -> None:
        """An unrequested field is absent from the response, so it must be asked for."""
        _issue, seen = await _jira_fetch(monkeypatch, {"summary": "x"})
        assert len(seen) == 1
        assert "fixVersions" in seen[0]

    @pytest.mark.asyncio
    async def test_milestone_carries_the_fix_version(self, monkeypatch) -> None:
        """The Issues panel milestone chip is fed by the first fix version."""
        issue, _seen = await _jira_fetch(
            monkeypatch,
            {
                "summary": "Ship it",
                "fixVersions": [{"name": "2.4.0", "releaseDate": "2026-09-30"}],
            },
        )
        assert issue["milestone"] == {
            "title": "2.4.0",
            "state": "open",
            "dueOn": "2026-09-30",
        }
        assert "fix versions" not in issue["partialSections"]

    @pytest.mark.asyncio
    async def test_multiple_fix_versions_are_declared_partial(self, monkeypatch) -> None:
        """Dropping the tail of a multi-release ticket is disclosed, not silent."""
        issue, _seen = await _jira_fetch(
            monkeypatch,
            {
                "summary": "Ship it twice",
                "fixVersions": [{"name": "2.4.0"}, {"name": "2.5.0"}],
            },
        )
        assert issue["milestone"]["title"] == "2.4.0"
        assert "fix versions" in issue["partialSections"]

    @pytest.mark.asyncio
    async def test_pending_version_wins_over_a_shipped_one(self, monkeypatch) -> None:
        """A released version ahead of a pending one does not steal the chip (#7595)."""
        issue, _seen = await _jira_fetch(
            monkeypatch,
            {
                "summary": "Fixed in the next release",
                "fixVersions": [
                    {"name": "2.3.0", "releaseDate": "2026-06-30", "released": True},
                    {"name": "2.4.0", "releaseDate": "2026-09-30", "released": False},
                ],
            },
        )
        assert issue["milestone"] == {
            "title": "2.4.0",
            "state": "open",
            "dueOn": "2026-09-30",
        }
        assert "fix versions" in issue["partialSections"]

    @pytest.mark.asyncio
    async def test_no_fix_version_leaves_milestone_null(self, monkeypatch) -> None:
        """A ticket with no fix version keeps the panel's 'no milestone' state."""
        issue, _seen = await _jira_fetch(monkeypatch, {"summary": "Unscheduled"})
        assert issue["milestone"] is None
        assert "fix versions" not in issue["partialSections"]


class _ReapProbe:
    """A PIPE-stdio child double that records how it is reaped.

    A killed child blocked writing into a full pipe -- or a surviving
    descendant still holding the pipes open -- makes a bare ``await
    proc.wait()`` hang the caller forever (#6005). The bounded reap must
    therefore drain the pipes via ``communicate()`` and must never touch
    ``wait()``.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: "int | None" = None
        self.kill_calls = 0
        self.wait_calls = 0
        self.communicate_calls = 0

    async def communicate(self):
        self.communicate_calls += 1
        self.returncode = -9
        return b"", b""

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        return -9


@pytest.mark.asyncio
async def test_terminate_process_reaps_via_communicate_not_wait(monkeypatch):
    """``_terminate_process`` must route through the bounded, pipe-draining
    ``kill_and_reap`` -- a bare ``await proc.wait()`` here can hang the gateway
    task forever when the child is killed with a full pipe (#6005)."""
    from kiro_crew import platform_compat

    proc = _ReapProbe()
    tree_kills: "list[tuple[int, int]]" = []

    async def _fake_tree(pid, sig):
        tree_kills.append((pid, sig))
        return True

    # Fake pid + patched tree kill so no test can reach a real killpg. Both the
    # async helper (used by kill_and_reap) and the legacy sync entry point are
    # patched so the pin stays safe even when run against unmodified code.
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", _fake_tree)
    monkeypatch.setattr(
        platform_compat, "kill_process_tree", lambda *a, **k: tree_kills.append(a)
    )

    await source._terminate_process(proc)

    assert proc.communicate_calls == 1
    assert proc.wait_calls == 0
    assert tree_kills and tree_kills[0][0] == proc.pid


def test_parse_repo_url_accepts_github_repo() -> None:
    ref = source.parse_repo_url("https://github.com/acme/console")
    assert ref.provider == "github"
    assert ref.host == "github.com"
    assert ref.owner == "acme"
    assert ref.repo == "console"


def test_parse_repo_url_strips_git_suffix_and_trailing_slash() -> None:
    ref = source.parse_repo_url("https://github.com/acme/repo.git/")
    assert (ref.owner, ref.repo) == ("acme", "repo")


def test_parse_repo_url_rejects_pull_request_url() -> None:
    # A pull URL has extra path segments and is not a repository root.
    with pytest.raises(ValueError):
        source.parse_repo_url("https://github.com/acme/repo/pull/12")


def test_parse_repo_url_rejects_userinfo() -> None:
    with pytest.raises(ValueError):
        source.parse_repo_url("https://user:pass@github.com/acme/repo")


def test_parse_repo_url_rejects_non_https() -> None:
    with pytest.raises(ValueError):
        source.parse_repo_url("http://github.com/acme/repo")


def test_parse_repo_url_rejects_non_allowlisted_host(monkeypatch) -> None:
    # Cold allowlist snapshot: a self-managed host is not recognized (fails closed).
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    with pytest.raises(ValueError):
        source.parse_repo_url("https://git.internal.example.com/acme/repo")


def test_parse_repo_url_accepts_public_gitlab() -> None:
    ref = source.parse_repo_url("https://gitlab.com/group/subgroup/project")
    assert ref.provider == "gitlab"
    assert ref.owner == "group/subgroup"
    assert ref.repo == "project"


@pytest.mark.asyncio
async def test_fetch_app_contributors_uses_profile_name(monkeypatch) -> None:
    source._contributors_cache.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if "/contributors?" in command:
            return [
                {"login": "octocat", "avatar_url": "https://avatars.example/1"},
                {"login": "hubot", "avatar_url": "https://avatars.example/2"},
            ]
        if command.endswith("users/octocat"):
            return {"name": "The Octocat"}
        if command.endswith("users/hubot"):
            return {"name": None}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    result = await source.fetch_app_contributors("https://github.com/acme/repo")

    assert result == [
        {
            "login": "octocat",
            "name": "The Octocat",
            "avatarUrl": "https://avatars.example/1",
            "profileUrl": "https://github.com/octocat",
        },
        {
            "login": "hubot",
            "name": "hubot",  # null profile name falls back to the login
            "avatarUrl": "https://avatars.example/2",
            "profileUrl": "https://github.com/hubot",
        },
    ]


@pytest.mark.asyncio
async def test_fetch_app_contributors_non_github_returns_empty(monkeypatch) -> None:
    source._contributors_cache.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)

    assert await source.fetch_app_contributors("https://gitlab.com/group/project") == []
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_app_contributors_coalesces_concurrent_calls(monkeypatch) -> None:
    source._contributors_cache.clear()
    source._contributors_inflight.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    calls = {"n": 0}

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if "/contributors?" in command:
            calls["n"] += 1
            await asyncio.sleep(0.02)
            return [{"login": "octocat", "avatar_url": "a"}]
        if command.endswith("users/octocat"):
            return {"name": "Octo"}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    first, second = await asyncio.gather(
        source.fetch_app_contributors("https://github.com/acme/repo"),
        source.fetch_app_contributors("https://github.com/acme/repo"),
    )

    assert first == second
    assert calls["n"] == 1  # one shared provider fanout for both callers
    # A second identical read is served from cache without another provider call.
    await source.fetch_app_contributors("https://github.com/acme/repo")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fetch_app_contributors_skips_profile_for_unsafe_login(monkeypatch) -> None:
    source._contributors_cache.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    seen: list[str] = []

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        seen.append(command)
        if "/contributors?" in command:
            return [{"login": "../etc", "avatar_url": "a"}]
        raise AssertionError(f"unexpected profile lookup: {command}")

    monkeypatch.setattr(source, "_run_json", fake_run)
    result = await source.fetch_app_contributors("https://github.com/acme/repo")

    # A malformed login never reaches the users/<login> path; name falls back.
    assert result == [
        {
            "login": "../etc",
            "name": "../etc",
            "avatarUrl": "a",
            "profileUrl": "https://github.com/../etc",
        }
    ]
    assert not any("users/" in c for c in seen)


@pytest.mark.asyncio
async def test_app_contributors_endpoint_requires_owner_claim(monkeypatch) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(source, "fetch_app_contributors", fetch)

    async with TestClient(TestServer(_app(user="U_OTHER"))) as client:
        response = await client.post(
            "/api/source/contributors", json={"url": "https://github.com/acme/repo"}
        )
        assert response.status == 403
        assert await response.json() == {"error": "forbidden"}
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_app_contributors_endpoint_returns_list_for_owner(monkeypatch) -> None:
    payload = [
        {
            "login": "octocat",
            "name": "Octo",
            "avatarUrl": "https://a/1",
            "profileUrl": "https://github.com/octocat",
        }
    ]
    monkeypatch.setattr(source, "fetch_app_contributors", AsyncMock(return_value=payload))

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/contributors", json={"url": "https://github.com/acme/repo"}
        )
        assert response.status == 200
        assert await response.json() == {"contributors": payload}


@pytest.mark.asyncio
async def test_app_contributors_endpoint_maps_bad_url_to_400(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "fetch_app_contributors", AsyncMock(side_effect=ValueError("bad url"))
    )

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/contributors", json={"url": "nope"})
        assert response.status == 400
        assert (await response.json())["error"] == "bad url"
