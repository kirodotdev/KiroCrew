from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import source_providers as source


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


def test_safe_error_redacts_credentials_and_exfiltration_urls() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    payload = "x" * 80
    error = source._safe_error(
        f"failed with {secret} at https://attacker.example/c?data={payload}".encode()
    )
    assert secret not in error
    assert payload not in error
    assert "[REDACTED" in error


def test_provider_executable_ignores_workspace_path(monkeypatch, tmp_path) -> None:
    malicious = tmp_path / "gh"
    malicious.write_text("#!/bin/sh\nexit 99\n")
    malicious.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("KIROCREW_GH_BIN", raising=False)
    trusted = "/usr/bin/gh"
    seen: list[str] = []

    def validate(candidate: str) -> str:
        seen.append(candidate)
        return candidate

    monkeypatch.setattr(
        source,
        "_PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": (trusted,), "glab": ("/usr/bin/glab",)},
    )
    monkeypatch.setattr(source, "_validate_provider_executable", validate)

    assert source._resolve_provider_executable("gh") == trusted
    assert seen == [trusted]
    assert str(malicious) not in seen


def test_provider_executable_not_found_gives_setup_guidance(monkeypatch) -> None:
    monkeypatch.delenv("KIROCREW_GH_BIN", raising=False)
    monkeypatch.setattr(
        source,
        "_PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": ("/usr/local/libexec/kirocrew/gh",), "glab": ("/usr/local/libexec/kirocrew/glab",)},
    )

    def reject(_candidate: str) -> str:
        raise ValueError("path does not exist")

    monkeypatch.setattr(source, "_validate_provider_executable", reject)

    with pytest.raises(source.SourceProviderError) as excinfo:
        source._resolve_provider_executable("gh")

    message = str(excinfo.value)
    # Names the managed target dir and gives copy/paste sudo setup steps.
    assert "/usr/local/libexec/kirocrew" in message
    assert 'sudo cp "$(command -v gh)" /usr/local/libexec/kirocrew/gh' in message
    assert "sudo chown -R root /usr/local/libexec/kirocrew" in message
    # Points at the override and reassures auth carries over.
    assert "KIROCREW_GH_BIN" in message
    assert "gh auth login" in message


def test_provider_executable_rejects_relative_override(monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_GH_BIN", "workspace/bin/gh")

    with pytest.raises(source.SourceProviderError, match="path must be absolute"):
        source._resolve_provider_executable("gh")


def test_provider_executable_rejects_current_user_owned_override(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o500)
    monkeypatch.setenv("KIROCREW_GH_BIN", str(executable))

    with pytest.raises(source.SourceProviderError, match="executable is not root-owned"):
        source._resolve_provider_executable("gh")


def test_provider_executable_rejects_symlink(monkeypatch, tmp_path) -> None:
    target = tmp_path / "real-gh"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o500)
    link = tmp_path / "gh"
    link.symlink_to(target)
    monkeypatch.setenv("KIROCREW_GH_BIN", str(link))

    with pytest.raises(source.SourceProviderError, match="canonical.*no symlinks"):
        source._resolve_provider_executable("gh")


def test_provider_executable_accepts_root_owned_nonwritable_canonical_path(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o500)
    real_stat = executable.stat()
    root_stat = source.os.stat_result([*list(real_stat)[:4], 0, *list(real_stat)[5:]])
    monkeypatch.setattr(source, "_path_parents", lambda _path: [])
    monkeypatch.setattr(source.Path, "stat", lambda _path: root_stat)
    monkeypatch.setattr(
        source.os,
        "access",
        lambda _path, mode: mode == source.os.X_OK,
    )

    assert source._validate_provider_executable(str(executable)) == str(executable)


def test_provider_executable_rejects_user_managed_homebrew_location(monkeypatch) -> None:
    candidate = "/opt/homebrew/bin/gh"
    validate = MagicMock(side_effect=ValueError("path contains a symlink"))
    monkeypatch.delenv("KIROCREW_GH_BIN", raising=False)
    monkeypatch.setattr(
        source,
        "_PROVIDER_EXECUTABLE_CANDIDATES",
        {"gh": (candidate,), "glab": ("/usr/bin/glab",)},
    )
    monkeypatch.setattr(source, "_validate_provider_executable", validate)

    with pytest.raises(source.SourceProviderError) as exc_info:
        source._resolve_provider_executable("gh")

    message = str(exc_info.value)
    assert "GitHub CLI (gh)" in message
    assert "user-owned copy is intentionally refused" in message
    assert "sudo mkdir -p /opt/homebrew/bin" in message
    assert 'sudo cp "$(command -v gh)" /opt/homebrew/bin/gh' in message
    assert "sudo chown -R root /opt/homebrew/bin" in message
    assert "`gh auth login` credentials are reused automatically" in message
    assert "KIROCREW_GH_BIN" in message
    assert "{executable}" not in message
    validate.assert_called_once_with(candidate)


def test_provider_executable_rejects_effectively_writable_root_path(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "glab"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o500)
    real_stat = executable.stat()
    root_stat = source.os.stat_result([*list(real_stat)[:4], 0, *list(real_stat)[5:]])
    monkeypatch.setattr(source, "_path_parents", lambda _path: [])
    monkeypatch.setattr(source.Path, "stat", lambda _path: root_stat)
    monkeypatch.setattr(source.os, "access", lambda _path, _mode: True)

    with pytest.raises(ValueError, match="writable by the gateway user"):
        source._validate_provider_executable(str(executable))


@pytest.mark.parametrize(
    ("parent_uid", "parent_mode", "parent_writable", "reason"),
    [
        (501, 0o755, False, "executable parent is not root-owned"),
        (0, 0o775, False, "executable parent is writable by the gateway user"),
        (0, 0o755, True, "executable parent is writable by the gateway user"),
    ],
)
def test_provider_executable_rejects_untrusted_ancestor(
    monkeypatch,
    tmp_path,
    parent_uid,
    parent_mode,
    parent_writable,
    reason,
) -> None:
    parent = tmp_path / "provider-bin"
    parent.mkdir()
    executable = parent / "gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o500)
    executable_stat = executable.stat()
    parent_stat = parent.stat()
    root_executable_stat = source.os.stat_result(
        [*list(executable_stat)[:4], 0, *list(executable_stat)[5:]]
    )
    tested_parent_stat = source.os.stat_result(
        [
            parent_stat.st_mode & ~0o777 | parent_mode,
            *list(parent_stat)[1:4],
            parent_uid,
            *list(parent_stat)[5:],
        ]
    )
    real_stat = source.Path.stat

    def fake_stat(path):
        if path == executable:
            return root_executable_stat
        if path == parent:
            return tested_parent_stat
        return real_stat(path)

    def fake_access(path, mode):
        if mode == source.os.X_OK:
            return path == executable
        if mode == source.os.W_OK:
            return path == parent and parent_writable
        return False

    monkeypatch.setattr(source, "_path_parents", lambda _path: [parent])
    monkeypatch.setattr(source.Path, "stat", fake_stat)
    monkeypatch.setattr(source.os, "access", fake_access)

    with pytest.raises(ValueError, match=reason):
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

    sandbox = MagicMock(return_value=(["sandbox", "/usr/bin/gh", "api"], {"SAFE": "1"}, None))
    spawn = AsyncMock(return_value=FakeProcess())
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "a" * 36)
    monkeypatch.setenv("PATH", "/workspace/attacker-bin")
    preexec = object()
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(source, "resource_limit_preexec", MagicMock(return_value=preexec))
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
    assert spawn.call_args.kwargs["preexec_fn"] is preexec


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
    assert checks[0]["name"] == "Pipeline"
    assert checks[0]["bucket"] == "passed"


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
    app.router.add_post("/api/source/pull-request/resolve", source.api_pull_request_resolve)
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
