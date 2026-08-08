from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

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
        source,
        "_PROVIDER_EXECUTABLE_CANDIDATES",
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
        source,
        "_PROVIDER_EXECUTABLE_CANDIDATES",
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
        source,
        "_PROVIDER_EXECUTABLE_CANDIDATES",
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


def test_provider_executable_accepts_user_owned_install(monkeypatch, tmp_path) -> None:
    """The default policy accepts the user's own gh — the Homebrew case that
    previously forced a `sudo cp` into a root-owned directory."""
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(source, "_agent_writable_roots", lambda: ())
    monkeypatch.setenv("KIROCREW_GH_BIN", str(executable))

    assert source._resolve_provider_executable("gh") == str(executable.resolve())


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
    monkeypatch.setattr(source, "_agent_writable_roots", lambda: ())
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
    monkeypatch.setattr(source, "_agent_writable_roots", lambda: ())
    monkeypatch.setattr(source.os, "geteuid", lambda: 0)

    with pytest.raises(ValueError, match="disabled for a root gateway"):
        source._validate_provider_executable(str(executable))


def test_provider_executable_rejects_binary_owned_by_another_user(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    real_stat = executable.stat()
    foreign_stat = source.os.stat_result([*list(real_stat)[:4], 4242, *list(real_stat)[5:]])
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(source, "_agent_writable_roots", lambda: ())
    monkeypatch.setattr(source, "_path_parents", lambda _path: [])
    monkeypatch.setattr(source.Path, "stat", lambda _path: foreign_stat)

    with pytest.raises(ValueError, match="owned by another user"):
        source._validate_provider_executable(str(executable))


def test_provider_executable_rejects_world_writable_binary(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o777)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(source, "_agent_writable_roots", lambda: ())

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
    monkeypatch.setattr(source, "_agent_writable_roots", lambda: ())
    monkeypatch.setattr(source, "_path_parents", lambda _path: [parent])

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
    monkeypatch.setattr(source, "_agent_writable_roots", lambda: ())
    monkeypatch.setattr(source, "_path_parents", lambda _path: [parent])

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
    root_executable_stat = source.os.stat_result(
        [*list(executable_stat)[:4], 0, *list(executable_stat)[5:]]
    )
    real_stat = source.Path.stat

    def fake_stat(path):
        if path == executable:
            return root_executable_stat
        return real_stat(path)

    monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
    monkeypatch.setattr(source, "_path_parents", lambda _path: [parent])
    monkeypatch.setattr(source.Path, "stat", fake_stat)
    monkeypatch.setattr(source.os, "access", lambda _path, mode: mode == source.os.X_OK)

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

    proc = FakeProcess()
    spawn_kwargs = {}

    async def fake_create(*_args, **kwargs):
        spawn_kwargs.update(kwargs)
        return proc

    def kill_tree(pid, sig):
        assert pid == proc.pid
        assert sig == source.platform_compat.SIGKILL
        proc.returncode = -sig
        proc.done.set()
        return True

    tree_kill = MagicMock(side_effect=kill_tree)
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        source,
        "sandboxed_spawn_argv",
        lambda argv, **kwargs: (argv, kwargs["env"], None),
    )
    monkeypatch.setattr(source.platform_compat, "kill_process_tree", tree_kill)
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(source.SourceProviderError, match="response was too large"):
        await source._run_json("gh", "api", "repos/acme/repo", max_output_bytes=4)
    tree_kill.assert_called_once_with(proc.pid, source.platform_compat.SIGKILL)
    assert proc.killed is False
    assert spawn_kwargs["env"]["GH_HOST"] == "github.com"
    assert spawn_kwargs["start_new_session"] is source.platform_compat.IS_POSIX
    assert spawn_kwargs["creationflags"] == source.platform_compat.CREATE_NEW_PROCESS_GROUP


@pytest.mark.asyncio
async def test_run_json_refuses_provider_cli_on_windows(monkeypatch) -> None:
    resolver = MagicMock()
    sandbox = MagicMock()
    spawn = AsyncMock()
    monkeypatch.setattr(source.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(source, "_resolve_provider_executable", resolver)
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(source.asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(source.SourceProviderError, match="not supported on Windows"):
        await source._run_json("gh", "api", "repos/acme/repo")

    resolver.assert_not_called()
    sandbox.assert_not_called()
    spawn.assert_not_awaited()


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
        order.append("audit-started")
        audit_started.set()
        await release_audit.wait()
        func(*args, **kwargs)
        order.append("audit-completed")

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
        assert "mergeable,mergeStateStatus" in " ".join(argv)
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
        source._publish_gitlab_hosts(frozenset({"gitlab.acme.internal"}))
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
        "statusCheckRollup",
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

    def fake_load() -> frozenset[str]:
        calls.append("load")
        return frozenset({"gitlab.acme.internal"})

    monkeypatch.setattr(source, "_load_gitlab_hosts", fake_load)
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

    def slow_stale_load() -> frozenset[str]:
        loads["n"] += 1
        # asyncio.Event is not thread-safe: this runs in a worker thread, so the
        # set() must be marshalled back onto the loop.
        loop.call_soon_threadsafe(started.set)
        # Block inside the worker thread so a second waiter queues on the lock.
        source.asyncio.run_coroutine_threadsafe(_noop(), loop).result(timeout=5)
        return frozenset({"gitlab.acme.internal"})

    async def _noop() -> None:
        await release.wait()

    loop = source.asyncio.get_running_loop()
    monkeypatch.setattr(source, "_load_gitlab_hosts", slow_stale_load)
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

    source._publish_gitlab_hosts(frozenset({"gitlab.acme.internal"}))
    first = source.gitlab_hosts_generation()
    assert first == 1

    source._publish_gitlab_hosts(frozenset({"gitlab.acme.internal"}))
    assert source.gitlab_hosts_generation() == first

    source._publish_gitlab_hosts(frozenset())
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
    stays owner-only, so a local-app token with no owner still fails closed."""
    resolve = AsyncMock()
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolve)

    async with TestClient(TestServer(_app(owner_id="", user="local-app", app_name=""))) as client:
        response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/1", "threadId": "PRRT_1"},
        )
        assert response.status == 403
        assert (await response.json()) == {"error": "forbidden"}

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
    owner-only, so a local-app token with no owner still fails closed."""
    action = AsyncMock()
    monkeypatch.setattr(source, action_name, action)

    async with TestClient(TestServer(_app(owner_id="", user="local-app", app_name=""))) as client:
        response = await client.post(path, json={"url": "https://github.com/acme/repo/pull/1"})
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
        assert await response.json() == {"error": "An issue URL is required."}


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
        assert await response.json() == {"error": "gh timed out"}


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
        source._publish_gitlab_hosts(frozenset({"gitlab.acme.internal"}))
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
