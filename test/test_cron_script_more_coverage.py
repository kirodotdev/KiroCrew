"""Coverage for cron_script error/cleanup branches not exercised by CI.

``test/test_cron_script.py`` is deselected in CI, so the module's kill-guard,
MCP JSON-RPC bridge, ScriptContext delivery surface and sandboxed-run cleanup
paths read as uncovered there. Everything here is a pure unit test: no real
cron, no real network, no real subprocess, no writes outside ``tmp_path`` (the
module's ``tempfile`` root is redirected there too).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import cron_script
from kiro_crew.cron_script import (
    McpToolClient,
    ScriptContext,
    _kill_proc_group,
    _resolve_command_shell,
    _resolve_internal_secret,
    _resolve_mcp_server,
    _resolve_safe_pgid,
    _shell_is_posix_strict,
    kill_running_process,
    resolve_script_path,
    run_command_sandboxed,
    run_script_sandboxed,
)
from kiro_crew.sandbox import SandboxUnavailableError

# ── shared fakes ──


class _FakeStdin:
    """Captures the JSON-RPC lines the client writes."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.flushes = 0

    def write(self, text: str) -> int:
        self.lines.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushes += 1


class _FakeStdout:
    """readline() drains a list, then returns "" (EOF) forever."""

    def __init__(self, lines) -> None:
        self._lines = list(lines)

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


class _EndlessStdout:
    """readline() always returns the same line (never EOF)."""

    def __init__(self, line: str) -> None:
        self._line = line
        self.reads = 0

    def readline(self) -> str:
        self.reads += 1
        return self._line


class _FakeProc:
    """A subprocess.Popen stand-in with scriptable communicate/wait behaviour."""

    def __init__(
        self,
        out_lines=(),
        pid: int = 4242,
        returncode: int = 0,
        comm_results=None,
        comm_raises_timeout: int = 0,
        wait_raises_timeout: bool = False,
        terminate_exc: BaseException | None = None,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout: object = _FakeStdout(out_lines)
        self.pid = pid
        self.returncode = returncode
        self.terminate_calls = 0
        self.kill_calls = 0
        self.waits: list[object] = []
        self.communicate_calls = 0
        self.spawn_argv: list[str] = []
        self.spawn_kwargs: dict = {}
        self._comm_results = list(comm_results or [("", "")])
        self._comm_raises_timeout = comm_raises_timeout
        self._wait_raises_timeout = wait_raises_timeout
        self._terminate_exc = terminate_exc

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self._terminate_exc is not None:
            raise self._terminate_exc

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self._wait_raises_timeout:
            self._wait_raises_timeout = False
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return self.returncode

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        if self._comm_raises_timeout > 0:
            self._comm_raises_timeout -= 1
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        if self._comm_results:
            return self._comm_results.pop(0)
        return ("", "")


@pytest.fixture(autouse=True)
def _module_state_is_restored(monkeypatch, tmp_path):
    """Keep module-level registries/caches and the tempfile root test-local."""
    monkeypatch.setattr(cron_script, "_RUNNING_PROCS", {})
    monkeypatch.setattr(cron_script, "_CANCELLED_PROC_JOBS", set())
    monkeypatch.setattr(cron_script, "_POSIX_STRICT_CACHE", {})
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    _resolve_mcp_server.cache_clear()
    yield
    _resolve_mcp_server.cache_clear()


@pytest.fixture
def posix_kill_stubs(monkeypatch):
    """Stub the POSIX process-group syscalls (absent on Windows) and force POSIX.

    ``os.getpgid`` / ``os.killpg`` do not exist on Windows, so they are
    installed with ``raising=False``; the kill-guard logic is then exercised
    identically on every platform CI runs.
    """
    monkeypatch.setattr(cron_script.platform_compat, "IS_POSIX", True)
    state = SimpleNamespace(pgids={0: 900}, killpg_calls=[], getpgid_exc=None, killpg_exc=None)

    def _getpgid(pid):
        if state.getpgid_exc is not None and pid != 0:
            raise state.getpgid_exc
        return state.pgids.get(pid, 555)

    def _killpg(pgid, sig):
        state.killpg_calls.append((pgid, sig))
        if state.killpg_exc is not None:
            raise state.killpg_exc

    monkeypatch.setattr(cron_script.os, "getpgid", _getpgid, raising=False)
    monkeypatch.setattr(cron_script.os, "killpg", _killpg, raising=False)
    return state


# ── _resolve_safe_pgid: the kill-broadcast guard ──


class TestResolveSafePgid:
    def test_windows_has_no_process_groups(self, monkeypatch):
        monkeypatch.setattr(cron_script.platform_compat, "IS_POSIX", False)
        assert _resolve_safe_pgid(_FakeProc(pid=4242)) is None

    def test_mock_pid_is_refused_because_index_coerces_to_one(self, posix_kill_stubs):
        # A MagicMock pid coerces to 1 via __index__, and killpg(1, sig) is a
        # signal broadcast to every process this uid can reach.
        proc = MagicMock()
        assert _resolve_safe_pgid(proc) is None

    @pytest.mark.parametrize("pid", [0, 1, -1])
    def test_reserved_pids_are_refused(self, posix_kill_stubs, pid):
        assert _resolve_safe_pgid(_FakeProc(pid=pid)) is None

    def test_missing_pid_attribute_is_refused(self, posix_kill_stubs):
        assert _resolve_safe_pgid(SimpleNamespace()) is None

    @pytest.mark.parametrize(
        "exc", [ProcessLookupError(), PermissionError(), OSError("boom")]
    )
    def test_getpgid_failure_falls_back_to_none(self, posix_kill_stubs, exc):
        posix_kill_stubs.getpgid_exc = exc
        assert _resolve_safe_pgid(_FakeProc(pid=4242)) is None

    def test_broadcast_pgid_is_refused(self, posix_kill_stubs):
        posix_kill_stubs.pgids[4242] = 1
        assert _resolve_safe_pgid(_FakeProc(pid=4242)) is None

    def test_own_process_group_is_refused(self, posix_kill_stubs):
        posix_kill_stubs.pgids[4242] = posix_kill_stubs.pgids[0]
        assert _resolve_safe_pgid(_FakeProc(pid=4242)) is None

    def test_distinct_group_is_returned(self, posix_kill_stubs):
        posix_kill_stubs.pgids[4242] = 777
        assert _resolve_safe_pgid(_FakeProc(pid=4242)) == 777


# ── kill_running_process / _kill_proc_group ──


class TestKillRunningProcess:
    def test_unknown_job_returns_false(self):
        assert kill_running_process("nope") is False

    def test_already_exited_process_returns_false(self):
        proc = _FakeProc(returncode=0)
        cron_script._RUNNING_PROCS["j1"] = proc
        assert kill_running_process("j1") is False
        assert "j1" not in cron_script._CANCELLED_PROC_JOBS

    def test_group_sigterm_marks_cancelled_and_arms_escalation(
        self, posix_kill_stubs, monkeypatch
    ):
        proc = _FakeProc(pid=4242, returncode=None)
        posix_kill_stubs.pgids[4242] = 777
        cron_script._RUNNING_PROCS["j2"] = proc
        threads: list[dict] = []
        monkeypatch.setattr(
            cron_script.threading,
            "Thread",
            lambda **kw: SimpleNamespace(start=lambda: threads.append(kw)),
        )

        assert kill_running_process("j2") is True

        assert posix_kill_stubs.killpg_calls == [(777, cron_script.signal.SIGTERM)]
        assert "j2" in cron_script._CANCELLED_PROC_JOBS
        assert len(threads) == 1 and threads[0]["name"] == "cron-cancel-j2"

    def test_escalation_thread_sigkills_a_survivor(self, posix_kill_stubs, monkeypatch):
        proc = _FakeProc(pid=4242, returncode=None)
        posix_kill_stubs.pgids[4242] = 777
        cron_script._RUNNING_PROCS["j3"] = proc
        captured: dict = {}
        monkeypatch.setattr(
            cron_script.threading,
            "Thread",
            lambda **kw: SimpleNamespace(start=lambda: captured.update(kw)),
        )
        monkeypatch.setattr(cron_script, "_KILL_ESCALATION_GRACE_SECS", 0.0)
        killed: list[object] = []
        monkeypatch.setattr(cron_script, "_kill_proc_group", killed.append)

        assert kill_running_process("j3") is True
        captured["target"]()  # run the escalation body inline

        assert killed == [proc]

    def test_escalation_thread_skips_an_exited_process(self, posix_kill_stubs, monkeypatch):
        proc = _FakeProc(pid=4242, returncode=None)
        posix_kill_stubs.pgids[4242] = 777
        cron_script._RUNNING_PROCS["j4"] = proc
        captured: dict = {}
        monkeypatch.setattr(
            cron_script.threading,
            "Thread",
            lambda **kw: SimpleNamespace(start=lambda: captured.update(kw)),
        )
        monkeypatch.setattr(cron_script, "_KILL_ESCALATION_GRACE_SECS", 0.0)
        killed: list[object] = []
        monkeypatch.setattr(cron_script, "_kill_proc_group", killed.append)

        assert kill_running_process("j4") is True
        proc.returncode = 0  # child reaped during the grace period
        captured["target"]()

        assert killed == []

    def test_killpg_failure_falls_back_to_terminate(self, posix_kill_stubs, monkeypatch):
        proc = _FakeProc(pid=4242, returncode=None)
        posix_kill_stubs.pgids[4242] = 777
        posix_kill_stubs.killpg_exc = ProcessLookupError()
        cron_script._RUNNING_PROCS["j5"] = proc
        monkeypatch.setattr(
            cron_script.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None)
        )

        assert kill_running_process("j5") is True
        assert proc.terminate_calls == 1

    def test_windows_reaps_the_tree_via_platform_compat(self, monkeypatch):
        monkeypatch.setattr(cron_script.platform_compat, "IS_POSIX", False)
        proc = _FakeProc(pid=4242, returncode=None)
        cron_script._RUNNING_PROCS["j6"] = proc
        tree: list[tuple] = []
        monkeypatch.setattr(
            cron_script.platform_compat,
            "kill_process_tree",
            lambda pid, sig: tree.append((pid, sig)),
        )
        monkeypatch.setattr(
            cron_script.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None)
        )

        assert kill_running_process("j6") is True
        assert tree == [(4242, cron_script.platform_compat.SIGTERM)]
        assert proc.terminate_calls == 0

    def test_windows_tree_kill_failure_falls_back_to_terminate(self, monkeypatch):
        monkeypatch.setattr(cron_script.platform_compat, "IS_POSIX", False)
        proc = _FakeProc(pid=4242, returncode=None)
        cron_script._RUNNING_PROCS["j7"] = proc

        def _boom(pid, sig):
            raise OSError("no taskkill")

        monkeypatch.setattr(cron_script.platform_compat, "kill_process_tree", _boom)
        monkeypatch.setattr(
            cron_script.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None)
        )

        assert kill_running_process("j7") is True
        assert proc.terminate_calls == 1

    def test_undeliverable_signal_clears_the_cancelled_flag(self, monkeypatch):
        # A natural completion must not be misreported as a user cancellation.
        monkeypatch.setattr(cron_script.platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(
            cron_script.platform_compat,
            "kill_process_tree",
            lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
        )
        proc = _FakeProc(pid=4242, returncode=None, terminate_exc=RuntimeError("gone"))
        cron_script._RUNNING_PROCS["j8"] = proc

        assert kill_running_process("j8") is False
        assert "j8" not in cron_script._CANCELLED_PROC_JOBS


class TestKillProcGroup:
    # `_kill_proc_group`'s process-group branch is POSIX-only by construction: it
    # calls os.killpg(pgid, signal.SIGKILL), and on Windows neither name exists
    # (`signal.SIGKILL` raises AttributeError) while `_resolve_safe_pgid` always
    # returns None, so the branch is unreachable there. These two tests force a
    # non-None pgid to reach it, which is meaningful only on POSIX. Windows keeps
    # its own coverage through test_windows_prefers_the_tree_kill below.
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="os.killpg/signal.SIGKILL are POSIX-only; the Windows path is covered separately",
    )
    def test_group_sigkill_short_circuits(self, posix_kill_stubs):
        proc = _FakeProc(pid=4242, returncode=None)
        posix_kill_stubs.pgids[4242] = 777

        _kill_proc_group(proc)

        assert posix_kill_stubs.killpg_calls == [(777, cron_script.signal.SIGKILL)]
        assert proc.kill_calls == 0

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="os.killpg/signal.SIGKILL are POSIX-only; the Windows path is covered separately",
    )
    def test_group_sigkill_failure_falls_through_to_kill(self, posix_kill_stubs):
        proc = _FakeProc(pid=4242, returncode=None)
        posix_kill_stubs.pgids[4242] = 777
        posix_kill_stubs.killpg_exc = PermissionError()

        _kill_proc_group(proc)

        assert proc.kill_calls == 1

    def test_windows_prefers_the_tree_kill(self, monkeypatch):
        monkeypatch.setattr(cron_script.platform_compat, "IS_POSIX", False)
        proc = _FakeProc(pid=4242, returncode=None)
        tree: list[tuple] = []
        monkeypatch.setattr(
            cron_script.platform_compat,
            "kill_process_tree",
            lambda pid, sig: tree.append((pid, sig)),
        )

        _kill_proc_group(proc)

        assert tree == [(4242, cron_script.platform_compat.SIGKILL)]
        assert proc.kill_calls == 0

    def test_kill_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(cron_script.platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(
            cron_script.platform_compat,
            "kill_process_tree",
            lambda pid, sig: (_ for _ in ()).throw(OSError()),
        )
        proc = _FakeProc(pid=4242, returncode=None)
        proc.kill = lambda: (_ for _ in ()).throw(RuntimeError("already reaped"))

        _kill_proc_group(proc)  # must not raise


# ── ScriptContext ──


def _ctx(job_id: str = "job-1", message: str = "hello") -> ScriptContext:
    return ScriptContext(job=SimpleNamespace(id=job_id, message=message))


class TestScriptContextInit:
    def test_secret_file_is_read_then_unlinked_and_env_popped(self, tmp_path, monkeypatch):
        secret = tmp_path / "secret.txt"
        secret.write_text("s3cret-value", newline="\n")
        monkeypatch.setenv("_KIROCREW_SECRET_FILE", str(secret))
        monkeypatch.setenv("KIROCREW_PORT", "7788")

        ctx = _ctx()

        assert ctx._secret == "s3cret-value"
        assert ctx._port == 7788
        assert not secret.exists()
        assert "_KIROCREW_SECRET_FILE" not in os.environ

    def test_unlink_failure_still_yields_the_secret(self, tmp_path, monkeypatch):
        secret = tmp_path / "secret.txt"
        secret.write_text("keep-me", newline="\n")
        monkeypatch.setenv("_KIROCREW_SECRET_FILE", str(secret))
        real_unlink = Path.unlink

        def _unlink(self, *a, **k):
            if os.path.realpath(str(self)) == os.path.realpath(str(secret)):
                raise OSError("read-only volume")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", _unlink)

        assert _ctx()._secret == "keep-me"

    def test_missing_secret_file_falls_back_to_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("_KIROCREW_SECRET_FILE", str(tmp_path / "absent"))
        monkeypatch.setenv("KIROCREW_INTERNAL_SECRET", "from-env")

        ctx = _ctx()

        assert ctx._secret == "from-env"
        assert "KIROCREW_INTERNAL_SECRET" not in os.environ

    def test_message_property_reads_the_job(self, monkeypatch):
        monkeypatch.delenv("_KIROCREW_SECRET_FILE", raising=False)
        assert _ctx(message="args here").message == "args here"

    def test_message_property_defaults_when_job_has_none(self, monkeypatch):
        monkeypatch.delenv("_KIROCREW_SECRET_FILE", raising=False)
        ctx = ScriptContext(job=SimpleNamespace(id="j"))
        assert ctx.message == ""


class TestScriptContextNotify:
    def test_error_response_raises(self, monkeypatch):
        ctx = _ctx()
        monkeypatch.setattr(ctx, "_post", lambda path, body: {"error": "403 Forbidden"})
        with pytest.raises(RuntimeError, match="notify\\(\\) failed: 403 Forbidden"):
            ctx.notify("hi")

    def test_caller_session_is_hard_assigned_not_spoofable(self, monkeypatch):
        ctx = _ctx(job_id="abc")
        seen: dict = {}

        def _post(path, body):
            seen["path"] = path
            seen["body"] = body
            return {"ok": True}

        monkeypatch.setattr(ctx, "_post", _post)

        assert ctx.notify("hi", caller_session="cron:someone-else") == {"ok": True}
        assert seen["path"] == "/api/send-message"
        assert seen["body"]["caller_session"] == "cron:abc"


class TestScriptContextPost:
    def test_request_url_headers_and_body(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_PORT", "7788")
        monkeypatch.setenv("KIROCREW_INTERNAL_SECRET", "tok")
        monkeypatch.delenv("_KIROCREW_SECRET_FILE", raising=False)
        ctx = _ctx(job_id="abc")
        captured: dict = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"delivered": true}'

        def _urlopen(req, timeout=None):
            captured["req"] = req
            captured["timeout"] = timeout
            return _Resp()

        monkeypatch.setattr(cron_script, "loopback_urlopen", _urlopen)

        assert ctx._post("/api/send-message", {"text": "hi"}) == {"delivered": True}

        req = captured["req"]
        # Full-value comparison: a prefix check would also match another host.
        assert req.full_url == "http://localhost:7788/api/send-message"
        assert req.get_method() == "POST"
        assert req.get_header("X-internal-secret") == "tok"
        assert req.get_header("X-session-key") == "cron:abc"
        assert json.loads(req.data) == {"text": "hi"}
        assert captured["timeout"] == 60

    def test_transport_failure_becomes_an_error_dict(self, monkeypatch):
        ctx = _ctx()
        monkeypatch.setattr(
            cron_script,
            "loopback_urlopen",
            lambda req, timeout=None: (_ for _ in ()).throw(OSError("connection refused")),
        )

        assert ctx._post("/api/send-message", {"text": "hi"}) == {
            "error": "connection refused"
        }


class TestScriptContextCallTool:
    def test_success_audits_ok_and_closes_the_client(self, monkeypatch):
        ctx = _ctx()
        client = MagicMock()
        client.call_tool.return_value = "tool output"
        monkeypatch.setattr(cron_script, "McpToolClient", lambda server: client)
        audits: list[tuple] = []
        monkeypatch.setattr(
            ctx, "_audit_tool_call", lambda *a, **k: audits.append((a, k))
        )

        assert ctx.call_tool("kirocrew-core", "browse_search", {"query": "x"}) == "tool output"

        client.call_tool.assert_called_once_with("browse_search", {"query": "x"})
        client.close.assert_called_once()
        assert audits == [(("kirocrew-core", "browse_search", "ok"), {})]

    def test_failure_audits_error_reraises_and_still_closes(self, monkeypatch):
        ctx = _ctx()
        client = MagicMock()
        client.call_tool.side_effect = RuntimeError("tool exploded")
        monkeypatch.setattr(cron_script, "McpToolClient", lambda server: client)
        audits: list[tuple] = []
        monkeypatch.setattr(ctx, "_audit_tool_call", lambda *a: audits.append(a))

        with pytest.raises(RuntimeError, match="tool exploded"):
            ctx.call_tool("srv", "t", {})

        assert audits == [("srv", "t", "error", "tool exploded")]
        client.close.assert_called_once()

    def test_construction_failure_needs_no_close(self, monkeypatch):
        ctx = _ctx()

        def _boom(server):
            raise RuntimeError("not found in agent config")

        monkeypatch.setattr(cron_script, "McpToolClient", _boom)
        monkeypatch.setattr(ctx, "_audit_tool_call", lambda *a: None)

        with pytest.raises(RuntimeError, match="not found in agent config"):
            ctx.call_tool("srv", "t", {})


class TestScriptContextAudit:
    def test_sel_invocation_is_recorded(self, monkeypatch):
        ctx = _ctx(job_id="abc")
        fake_sel = MagicMock()
        monkeypatch.setattr(cron_script, "sel", lambda: fake_sel)

        ctx._audit_tool_call("srv", "tool", "ok")

        kwargs = fake_sel.log_tool_invocation.call_args.kwargs
        assert kwargs["session_key"] == "cron:abc"
        assert kwargs["tool_name"] == "srv/tool"
        assert kwargs["tool_kind"] == "cron_script_tool"
        assert kwargs["outcome"] == "ok"

    def test_sel_failure_never_breaks_the_tool_call(self, monkeypatch):
        ctx = _ctx()
        monkeypatch.setattr(
            cron_script, "sel", lambda: (_ for _ in ()).throw(RuntimeError("sel down"))
        )

        ctx._audit_tool_call("srv", "tool", "error", "boom")  # must not raise


# ── McpToolClient ──


@pytest.fixture
def mcp_spawn(monkeypatch):
    """Patch the spawn chain so McpToolClient never starts a real process."""
    monkeypatch.setattr(cron_script, "_resolve_mcp_server", lambda name: ("srv-bin", "--stdio"))
    monkeypatch.setattr(cron_script, "wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr(cron_script, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(cron_script, "resource_limit_preexec", lambda: None)
    state = SimpleNamespace(proc=None, popen_exc=None, calls=[])

    def _popen(argv, **kw):
        state.calls.append((list(argv), kw))
        if state.popen_exc is not None:
            raise state.popen_exc
        return state.proc

    monkeypatch.setattr(cron_script.subprocess, "Popen", _popen)
    return state


_HANDSHAKE_OK = '{"jsonrpc": "2.0", "id": 1, "result": {}}\n'


class TestMcpToolClientSpawn:
    def test_unknown_server_raises_before_any_spawn(self, mcp_spawn, monkeypatch):
        monkeypatch.setattr(cron_script, "_resolve_mcp_server", lambda name: None)
        with pytest.raises(RuntimeError, match="not found in agent config"):
            McpToolClient("ghost")
        assert mcp_spawn.calls == []

    def test_handshake_sends_initialize_then_initialized(self, mcp_spawn):
        mcp_spawn.proc = _FakeProc(out_lines=["\n", _HANDSHAKE_OK])

        client = McpToolClient("kirocrew-core")

        sent = [json.loads(line) for line in mcp_spawn.proc.stdin.lines]
        assert sent[0]["method"] == "initialize"
        assert sent[0]["params"]["clientInfo"]["name"] == "kirocrew-cron-script"
        assert sent[1]["method"] == "notifications/initialized"
        assert "id" not in sent[1]
        assert mcp_spawn.proc.stdin.flushes == 2
        assert Path(client._stderr_file.name).exists()
        client.close()

    def test_spawn_failure_cleans_up_stderr_and_sandbox_files(self, mcp_spawn, tmp_path, monkeypatch):
        cleanup = tmp_path / "sandbox-profile.sb"
        cleanup.write_text("(deny default)", newline="\n")
        monkeypatch.setattr(
            cron_script, "wrap_argv", lambda argv, **k: (list(argv), str(cleanup))
        )
        mcp_spawn.popen_exc = FileNotFoundError("srv-bin missing")
        before = set(os.listdir(tmp_path))

        with pytest.raises(FileNotFoundError):
            McpToolClient("kirocrew-core")

        assert not cleanup.exists()
        # The stderr tempfile lives in tmp_path too and must not survive.
        assert set(os.listdir(tmp_path)) <= before

    def test_handshake_eof_reports_return_code_and_stderr_tail(self, mcp_spawn):
        mcp_spawn.proc = _FakeProc(out_lines=[], returncode=127)

        with pytest.raises(RuntimeError) as excinfo:
            McpToolClient("kirocrew-core")

        msg = str(excinfo.value)
        assert "disconnected during 'initialize'" in msg
        assert "rc=127" in msg
        assert "(empty)" in msg


class TestMcpToolClientRpc:
    def _client(self, mcp_spawn, out_lines=()):
        mcp_spawn.proc = _FakeProc(out_lines=[_HANDSHAKE_OK, *out_lines])
        return McpToolClient("kirocrew-core")

    def test_unrelated_ids_are_skipped_until_the_match(self, mcp_spawn):
        client = self._client(
            mcp_spawn,
            [
                '{"jsonrpc": "2.0", "method": "notifications/progress"}\n',
                '{"jsonrpc": "2.0", "id": 99, "result": {"stale": true}}\n',
                '{"jsonrpc": "2.0", "id": 2, "result": {"ok": true}}\n',
            ],
        )

        assert client._rpc("ping") == {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}
        client.close()

    def test_message_budget_is_bounded(self, mcp_spawn):
        client = self._client(mcp_spawn)
        client._proc.stdout = _EndlessStdout('{"jsonrpc": "2.0", "id": 99}\n')

        with pytest.raises(RuntimeError, match="within 1000 messages"):
            client._rpc("ping")

        assert client._proc.stdout.reads == 1000
        client.close()

    def test_params_default_to_an_empty_object(self, mcp_spawn):
        client = self._client(mcp_spawn, ['{"jsonrpc": "2.0", "id": 2}\n'])

        client._rpc("ping")

        assert json.loads(client._proc.stdin.lines[-1])["params"] == {}
        client.close()


class TestMcpToolClientCallTool:
    def _client(self, mcp_spawn):
        mcp_spawn.proc = _FakeProc(out_lines=[_HANDSHAKE_OK])
        return McpToolClient("kirocrew-core")

    def test_text_content_is_returned(self, mcp_spawn, monkeypatch):
        client = self._client(mcp_spawn)
        monkeypatch.setattr(
            client, "_rpc", lambda m, p: {"result": {"content": [{"text": "hi"}]}}
        )
        assert client.call_tool("t", {}) == "hi"
        client.close()

    def test_empty_content_is_an_empty_string(self, mcp_spawn, monkeypatch):
        client = self._client(mcp_spawn)
        monkeypatch.setattr(client, "_rpc", lambda m, p: {"result": {"content": []}})
        assert client.call_tool("t", {}) == ""
        client.close()

    def test_protocol_error_raises(self, mcp_spawn, monkeypatch):
        client = self._client(mcp_spawn)
        monkeypatch.setattr(client, "_rpc", lambda m, p: {"error": {"code": -32601}})
        with pytest.raises(RuntimeError, match="MCP tool error"):
            client.call_tool("t", {})
        client.close()

    def test_is_error_uses_the_content_text(self, mcp_spawn, monkeypatch):
        client = self._client(mcp_spawn)
        monkeypatch.setattr(
            client,
            "_rpc",
            lambda m, p: {"result": {"isError": True, "content": [{"text": "denied"}]}},
        )
        with pytest.raises(RuntimeError, match="MCP tool error: denied"):
            client.call_tool("t", {})
        client.close()

    def test_is_error_without_content_falls_back(self, mcp_spawn, monkeypatch):
        client = self._client(mcp_spawn)
        monkeypatch.setattr(client, "_rpc", lambda m, p: {"result": {"isError": True}})
        with pytest.raises(RuntimeError, match="MCP tool error: unknown error"):
            client.call_tool("t", {})
        client.close()


class TestMcpToolClientStderrTail:
    def _client(self, mcp_spawn):
        mcp_spawn.proc = _FakeProc(out_lines=[_HANDSHAKE_OK])
        return McpToolClient("kirocrew-core")

    def test_tail_is_capped_to_the_limit(self, mcp_spawn):
        client = self._client(mcp_spawn)
        Path(client._stderr_file.name).write_text("A" * 100 + "TAILMARKER", newline="\n")

        tail = client._stderr_tail(limit=10)

        assert tail == "TAILMARKER"
        client.close()

    def test_missing_stderr_handle_returns_empty(self, mcp_spawn):
        client = self._client(mcp_spawn)
        stderr_file = client._stderr_file
        del client._stderr_file

        assert client._stderr_tail() == ""

        stderr_file.close()
        Path(stderr_file.name).unlink(missing_ok=True)
        client._stderr_file = None
        client.close()

    def test_unreadable_stderr_returns_empty(self, mcp_spawn, tmp_path):
        client = self._client(mcp_spawn)
        client._stderr_file = SimpleNamespace(name=str(tmp_path / "absent" / "x.log"))

        assert client._stderr_tail() == ""
        client._stderr_file = None
        client.close()


class TestMcpToolClientClose:
    def _client(self, mcp_spawn, **proc_kwargs):
        mcp_spawn.proc = _FakeProc(out_lines=[_HANDSHAKE_OK], **proc_kwargs)
        return McpToolClient("kirocrew-core")

    def test_graceful_close_removes_the_stderr_file(self, mcp_spawn):
        client = self._client(mcp_spawn)
        stderr_path = Path(client._stderr_file.name)

        client.close()

        assert client._proc.terminate_calls == 1
        assert client._proc.waits == [5]
        assert not stderr_path.exists()

    def test_timeout_escalates_to_kill(self, mcp_spawn):
        client = self._client(mcp_spawn, wait_raises_timeout=True)

        client.close()

        assert client._proc.kill_calls == 1
        assert client._proc.waits == [5, None]

    def test_terminate_failure_is_swallowed(self, mcp_spawn):
        client = self._client(mcp_spawn, terminate_exc=RuntimeError("already reaped"))

        client.close()  # must not raise

        assert client._proc.kill_calls == 0

    def test_stderr_close_failure_still_unlinks_and_cleans_sandbox(
        self, mcp_spawn, tmp_path
    ):
        client = self._client(mcp_spawn)
        real_stderr = client._stderr_file
        stub = tmp_path / "stub-stderr.log"
        stub.write_text("x", newline="\n")
        cleanup = tmp_path / "profile.sb"
        cleanup.write_text("(deny default)", newline="\n")

        def _boom():
            raise OSError("already closed")

        client._stderr_file = SimpleNamespace(name=str(stub), close=_boom)
        client._sandbox_cleanup = str(cleanup)

        client.close()

        assert not stub.exists()
        assert not cleanup.exists()
        real_stderr.close()
        Path(real_stderr.name).unlink(missing_ok=True)


# ── _resolve_mcp_server ──


class TestResolveMcpServer:
    def test_missing_config_returns_none(self, tmp_path, monkeypatch):
        agents = tmp_path / "agents"
        agents.mkdir()
        monkeypatch.setattr(cron_script, "kiro_agents_dir", lambda: agents)
        assert _resolve_mcp_server("server-a") is None

    def test_glob_fallback_finds_an_aliased_spec(self, tmp_path, monkeypatch):
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "my-kirocrew-alias.json").write_text(
            json.dumps({"mcpServers": {"core": {"command": "node", "args": ["srv.js"]}}}),
            newline="\n",
        )
        monkeypatch.setattr(cron_script, "kiro_agents_dir", lambda: agents)

        assert _resolve_mcp_server("core") == ("node", "srv.js")

    def test_absent_server_entry_returns_none(self, tmp_path, monkeypatch):
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "kirocrew.json").write_text(
            json.dumps({"mcpServers": {"other": {"command": "node"}}}), newline="\n"
        )
        monkeypatch.setattr(cron_script, "kiro_agents_dir", lambda: agents)

        assert _resolve_mcp_server("server-b") is None

    def test_argless_spec_yields_a_single_element_tuple(self, tmp_path, monkeypatch):
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "kirocrew.json").write_text(
            json.dumps({"mcpServers": {"bare": {"command": "srv-bin"}}}), newline="\n"
        )
        monkeypatch.setattr(cron_script, "kiro_agents_dir", lambda: agents)

        assert _resolve_mcp_server("bare") == ("srv-bin",)


# ── path + secret resolution ──


class TestPathAndSecretResolution:
    def test_sensitive_path_is_blocked_before_the_allowed_dir_check(
        self, tmp_path, monkeypatch
    ):
        crons = tmp_path / "crons"
        crons.mkdir()
        script = crons / "job.py"
        script.write_text("def run(ctx): pass\n", newline="\n")
        monkeypatch.setattr(cron_script, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(cron_script, "is_sensitive_path", lambda p: True)

        with pytest.raises(PermissionError, match="blocked by security policy"):
            resolve_script_path(f"{script}:run")

    def test_allowed_script_resolves(self, tmp_path, monkeypatch):
        crons = tmp_path / "crons"
        crons.mkdir()
        script = crons / "job.py"
        script.write_text("def run(ctx): pass\n", newline="\n")
        monkeypatch.setattr(cron_script, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(cron_script, "is_sensitive_path", lambda p: False)

        resolved, func = resolve_script_path(f"{script}:run")

        assert os.path.realpath(resolved) == os.path.realpath(str(script))
        assert func == "run"

    def test_internal_secret_prefers_the_environment(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_INTERNAL_SECRET", "env-secret")
        monkeypatch.setattr(cron_script, "read_local_secret", lambda: "file-secret")
        assert _resolve_internal_secret() == "env-secret"

    def test_internal_secret_falls_back_to_the_local_secret_file(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_INTERNAL_SECRET", raising=False)
        monkeypatch.setattr(cron_script, "read_local_secret", lambda: "file-secret")
        assert _resolve_internal_secret() == "file-secret"


# ── run_script_sandboxed ──


@pytest.fixture
def script_run(monkeypatch, tmp_path):
    """Patch run_script_sandboxed's spawn chain; expose the recorded Popen call."""
    script = tmp_path / "job.py"
    script.write_text("def run(ctx): pass\n", newline="\n")
    monkeypatch.setattr(
        cron_script, "resolve_script_path", lambda spec: (str(script), "run")
    )
    monkeypatch.setattr(cron_script, "wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr(cron_script, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(cron_script, "resource_limit_preexec", lambda: None)
    monkeypatch.setattr(cron_script, "_resolve_internal_secret", lambda: "unit-secret")
    restricted: list[str] = []
    monkeypatch.setattr(
        cron_script.platform_compat, "restrict_to_owner", restricted.append
    )
    state = SimpleNamespace(
        script=script, proc=None, argv=[], env={}, launcher_src="", restricted=restricted
    )

    def _popen(argv, **kw):
        state.argv = list(argv)
        state.env = dict(kw.get("env") or {})
        state.launcher_src = Path(argv[1]).read_text()
        state.secret_seen = Path(state.env["_KIROCREW_SECRET_FILE"]).read_text()
        return state.proc

    monkeypatch.setattr(cron_script.subprocess, "Popen", _popen)
    return state


class TestRunScriptSandboxed:
    def test_ok_result_and_temp_file_cleanup(self, script_run):
        script_run.proc = _FakeProc(comm_results=[('{"status": "ok"}\n', "")])

        assert run_script_sandboxed("spec:run", "job-9", "the message") == {"status": "ok"}

        assert script_run.secret_seen == "unit-secret"
        assert script_run.restricted == [script_run.env["_KIROCREW_SECRET_FILE"]]
        assert "job-9" in script_run.launcher_src
        assert "the message" in script_run.launcher_src
        # Secrets are handed over by file, never inherited through the env.
        assert "KIROCREW_INTERNAL_SECRET" not in script_run.env
        assert not Path(script_run.argv[1]).exists()
        assert not Path(script_run.env["_KIROCREW_SECRET_FILE"]).exists()
        assert cron_script._RUNNING_PROCS == {}

    def test_only_the_last_stdout_line_is_parsed(self, script_run):
        script_run.proc = _FakeProc(
            comm_results=[('warming up\n{"status": "done", "message": "bye"}\n', "")]
        )

        assert run_script_sandboxed("spec:run", "job-9") == {
            "status": "done",
            "message": "bye",
        }

    def test_cancellation_wins_over_the_exit_status(self, script_run):
        script_run.proc = _FakeProc(comm_results=[("", "")], returncode=-15)
        cron_script._CANCELLED_PROC_JOBS.add("job-c")

        assert run_script_sandboxed("spec:run", "job-c") == {
            "status": "cancelled",
            "error": "Cancelled by user",
        }
        assert "job-c" not in cron_script._CANCELLED_PROC_JOBS

    def test_nonzero_exit_with_no_stdout_surfaces_stderr(self, script_run):
        script_run.proc = _FakeProc(comm_results=[("  ", "Traceback: boom")], returncode=1)

        result = run_script_sandboxed("spec:run", "job-9")

        assert result == {"status": "error", "error": "Traceback: boom"}

    def test_nonzero_exit_with_no_output_at_all_reports_the_code(self, script_run):
        script_run.proc = _FakeProc(comm_results=[("", "")], returncode=3)

        assert run_script_sandboxed("spec:run", "job-9") == {
            "status": "error",
            "error": "exit 3",
        }

    def test_unparseable_output_is_reported_as_bad_output(self, script_run):
        script_run.proc = _FakeProc(comm_results=[("not json at all", "")])

        result = run_script_sandboxed("spec:run", "job-9")

        assert result["status"] == "error"
        assert result["error"].startswith("Bad output:")
        assert "not json at all" in result["error"]

    def test_timeout_kills_the_group_and_reaps_before_reporting(self, script_run, monkeypatch):
        script_run.proc = _FakeProc(comm_raises_timeout=1)
        killed: list[object] = []
        monkeypatch.setattr(cron_script, "_kill_proc_group", killed.append)

        assert run_script_sandboxed("spec:run", "job-9", timeout=7) == {
            "status": "error",
            "error": "Script timed out after 7s",
        }

        assert killed == [script_run.proc]
        assert script_run.proc.communicate_calls == 2
        assert cron_script._RUNNING_PROCS == {}

    def test_sandbox_cleanup_file_is_removed(self, script_run, monkeypatch, tmp_path):
        cleanup = tmp_path / "profile.sb"
        cleanup.write_text("(deny default)", newline="\n")
        monkeypatch.setattr(
            cron_script, "wrap_argv", lambda argv, **k: (list(argv), str(cleanup))
        )
        script_run.proc = _FakeProc(comm_results=[('{"status": "ok"}', "")])

        assert run_script_sandboxed("spec:run", "job-9")["status"] == "ok"
        assert not cleanup.exists()

    def test_sandbox_unavailable_returns_the_remedy(self, script_run, monkeypatch):
        def _refuse(argv, **k):
            raise SandboxUnavailableError(
                "Set agent.sandbox_allow_unsandboxed_exec=true to allow it.",
                kind="no_backend",
                detail="not Linux",
            )

        monkeypatch.setattr(cron_script, "wrap_argv", _refuse)

        result = run_script_sandboxed("spec:run", "job-9")

        assert result["status"] == "error"
        assert result["error"].startswith(cron_script._SANDBOX_UNAVAILABLE_PREFIX)
        assert "allow_unsandboxed_exec" in result["error"]


# ── shell resolution ──


class TestResolveCommandShell:
    def test_windows_has_no_posix_shell(self, monkeypatch):
        monkeypatch.setattr(cron_script.platform_compat, "IS_WINDOWS", True)
        assert _resolve_command_shell() is None

    def test_first_strict_trusted_path_wins(self, monkeypatch):
        monkeypatch.setattr(cron_script.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(cron_script.os.path, "isfile", lambda p: True)
        monkeypatch.setattr(cron_script, "_shell_is_posix_strict", lambda p: True)
        assert _resolve_command_shell() == "/bin/sh"

    def test_a_brace_expanding_shell_is_skipped(self, monkeypatch):
        monkeypatch.setattr(cron_script.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(cron_script.os.path, "isfile", lambda p: True)
        monkeypatch.setattr(
            cron_script, "_shell_is_posix_strict", lambda p: p == "/usr/bin/sh"
        )
        assert _resolve_command_shell() == "/usr/bin/sh"

    def test_no_candidate_present_returns_none(self, monkeypatch):
        monkeypatch.setattr(cron_script.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(cron_script.os.path, "isfile", lambda p: False)
        assert _resolve_command_shell() is None


class TestShellIsPosixStrict:
    @pytest.fixture(autouse=True)
    def _no_real_sandbox(self, monkeypatch):
        monkeypatch.setattr(cron_script, "wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr(cron_script, "cgroup_scope_argv", lambda argv: list(argv))
        monkeypatch.setattr(cron_script, "resource_limit_preexec", lambda: None)

    def test_literal_output_is_accepted_and_memoized(self, monkeypatch):
        calls: list[list[str]] = []

        def _run(argv, **kw):
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="x.{a,a}\n", stderr="")

        monkeypatch.setattr(cron_script.subprocess, "run", _run)

        assert _shell_is_posix_strict("/bin/sh") is True
        assert _shell_is_posix_strict("/bin/sh") is True  # cache hit, no second spawn

        assert calls == [["/bin/sh", "-c", "echo x.{a,a}"]]

    def test_expanding_shell_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            cron_script.subprocess,
            "run",
            lambda argv, **kw: SimpleNamespace(returncode=0, stdout="x.a x.a\n", stderr=""),
        )
        assert _shell_is_posix_strict("/bin/sh") is False

    def test_nonzero_probe_exit_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            cron_script.subprocess,
            "run",
            lambda argv, **kw: SimpleNamespace(returncode=1, stdout="x.{a,a}", stderr=""),
        )
        assert _shell_is_posix_strict("/bin/sh") is False

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("exec format error"),
            subprocess.TimeoutExpired(cmd="sh", timeout=5),
            SandboxUnavailableError("no backend", kind="no_backend", detail="none"),
        ],
    )
    def test_probe_failures_are_rejected_not_raised(self, monkeypatch, exc):
        def _run(argv, **kw):
            raise exc

        monkeypatch.setattr(cron_script.subprocess, "run", _run)
        assert _shell_is_posix_strict("/bin/sh") is False

    def test_sandbox_profile_is_unlinked_even_when_gone(self, monkeypatch, tmp_path):
        cleanup = tmp_path / "profile.sb"
        cleanup.write_text("(deny default)", newline="\n")
        monkeypatch.setattr(
            cron_script, "wrap_argv", lambda argv, **k: (list(argv), str(cleanup))
        )
        monkeypatch.setattr(
            cron_script.subprocess,
            "run",
            lambda argv, **kw: SimpleNamespace(returncode=0, stdout="x.{a,a}", stderr=""),
        )

        assert _shell_is_posix_strict("/bin/sh") is True
        assert not cleanup.exists()

        # A second, already-removed profile must not raise out of the finally.
        cron_script._POSIX_STRICT_CACHE.clear()
        assert _shell_is_posix_strict("/bin/sh") is True


# ── run_command_sandboxed ──


@pytest.fixture
def command_run(monkeypatch):
    monkeypatch.setattr(cron_script, "_resolve_command_shell", lambda: "/bin/sh")
    monkeypatch.setattr(cron_script, "wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr(cron_script, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(cron_script, "resource_limit_preexec", lambda: None)
    state = SimpleNamespace(proc=None, argv=[], env={})

    def _popen(argv, **kw):
        state.argv = list(argv)
        state.env = dict(kw.get("env") or {})
        return state.proc

    monkeypatch.setattr(cron_script.subprocess, "Popen", _popen)
    return state


class TestRunCommandSandboxed:
    def test_no_posix_shell_refuses_with_guidance(self, monkeypatch):
        monkeypatch.setattr(cron_script, "_resolve_command_shell", lambda: None)

        result = run_command_sandboxed("echo hi")

        assert result["status"] == "error"
        assert result["exit_code"] == -1
        assert "No POSIX shell available" in result["output"]
        assert "script cron" in result["output"]

    def test_success_passes_the_command_to_the_shell(self, command_run):
        command_run.proc = _FakeProc(comm_results=[("hello\n", "")])

        result = run_command_sandboxed("echo hello")

        assert result == {"status": "ok", "output": "hello\n", "exit_code": 0}
        assert command_run.argv == ["/bin/sh", "-c", "echo hello"]
        assert "KIROCREW_INTERNAL_SECRET" not in command_run.env

    def test_nonzero_exit_annotates_output_and_appends_stderr(self, command_run):
        command_run.proc = _FakeProc(comm_results=[("partial\n", "boom")], returncode=42)

        result = run_command_sandboxed("false")

        assert result["status"] == "error"
        assert result["exit_code"] == 42
        assert "Exit code 42" in result["output"]
        assert "stderr:\nboom" in result["output"]

    def test_oversized_output_is_truncated(self, command_run):
        payload = "x" * (cron_script._MAX_COMMAND_OUTPUT + 500)
        command_run.proc = _FakeProc(comm_results=[(payload, "")])

        result = run_command_sandboxed("cat big")

        assert result["status"] == "ok"
        assert "truncated" in result["output"]
        assert result["output"].startswith("x" * 100)
        assert len(result["output"]) < len(payload)

    def test_timeout_kills_the_group(self, command_run, monkeypatch):
        command_run.proc = _FakeProc(comm_raises_timeout=1)
        killed: list[object] = []
        monkeypatch.setattr(cron_script, "_kill_proc_group", killed.append)

        result = run_command_sandboxed("sleep 100", timeout=3, job_id="job-t")

        assert result == {
            "status": "error",
            "output": "❌ Command timed out after 3s",
            "exit_code": -1,
        }
        assert killed == [command_run.proc]
        assert cron_script._RUNNING_PROCS == {}

    def test_cancellation_is_reported_over_the_output(self, command_run):
        command_run.proc = _FakeProc(comm_results=[("ignored", "")], returncode=-15)
        cron_script._CANCELLED_PROC_JOBS.add("job-c")

        result = run_command_sandboxed("sleep 100", job_id="job-c")

        assert result == {
            "status": "cancelled",
            "output": "Cancelled by user",
            "exit_code": -15,
        }

    def test_registry_only_tracks_identified_jobs(self, command_run):
        command_run.proc = _FakeProc(comm_results=[("", "")])

        assert run_command_sandboxed("true")["status"] == "ok"
        assert cron_script._RUNNING_PROCS == {}

    def test_unexpected_spawn_failure_is_structured(self, command_run, monkeypatch):
        def _popen(argv, **kw):
            raise OSError("fork failed")

        monkeypatch.setattr(cron_script.subprocess, "Popen", _popen)

        result = run_command_sandboxed("echo hi")

        assert result["status"] == "error"
        assert result["exit_code"] == -1
        assert "Command failed: fork failed" in result["output"]

    def test_sandbox_unavailable_returns_the_remedy(self, command_run, monkeypatch):
        def _refuse(argv, **k):
            raise SandboxUnavailableError(
                "Set agent.sandbox_allow_unsandboxed_exec=true to allow it.",
                kind="no_backend",
                detail="not Linux",
            )

        monkeypatch.setattr(cron_script, "wrap_argv", _refuse)

        result = run_command_sandboxed("echo hi")

        assert result["status"] == "error"
        assert result["exit_code"] == -1
        assert result["output"].startswith(cron_script._SANDBOX_UNAVAILABLE_PREFIX)

    def test_sandbox_profile_is_cleaned_up(self, command_run, monkeypatch, tmp_path):
        cleanup = tmp_path / "profile.sb"
        cleanup.write_text("(deny default)", newline="\n")
        monkeypatch.setattr(
            cron_script, "wrap_argv", lambda argv, **k: (list(argv), str(cleanup))
        )
        command_run.proc = _FakeProc(comm_results=[("ok\n", "")])

        assert run_command_sandboxed("echo ok")["status"] == "ok"
        assert not cleanup.exists()
