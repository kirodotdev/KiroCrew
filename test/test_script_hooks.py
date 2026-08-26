"""Tests for script hooks system (ScriptHookStore, run_script_hook, etc.)."""

from __future__ import annotations

import asyncio
import platform
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHook,
    ScriptHookStore,
    run_script_hook,
)

_IS_MACOS = platform.system() == "Darwin"
_IS_WINDOWS = platform.system() == "Windows"

# Reading an env var is the one hook-command shape that is inherently
# shell-specific: POSIX sh expands ``$VAR``, cmd.exe expands ``%VAR%`` and
# leaves ``$VAR`` as a literal.
_ECHO_HOOK_EVENT = "echo %KIROCREW_HOOK_EVENT%" if _IS_WINDOWS else "echo $KIROCREW_HOOK_EVENT"


def _script_command(script: Path, body: str) -> str:
    """Write *body* to *script* and return a hook command that runs it.

    A quoted interpreter plus a quoted script path is the one command shape both
    ``/bin/sh -c`` and ``cmd /c`` parse identically — an inline ``python -c
    '…'`` cannot be, because cmd.exe gives single quotes no grouping meaning.
    It is also the shape a real Windows hook takes (``sys.executable`` usually
    lives under a path containing a space), so the quotes must survive to the
    shell verbatim rather than being argv-escaped on the way.
    """
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


@pytest.fixture
def hook_store(tmp_path: Path) -> ScriptHookStore:
    """Create a temporary hook store."""
    return ScriptHookStore(tmp_path)


class TestScriptHook:
    """Test ScriptHook dataclass."""

    def test_to_dict(self):
        hook = ScriptHook(
            id="test-123",
            name="test-hook",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command="echo test",
            timeout=30,
            enabled=True,
        )
        d = hook.to_dict()
        assert d["id"] == "test-123"
        assert d["name"] == "test-hook"
        assert d["event"] == HOOK_EVENT_USER_PROMPT_SUBMIT
        assert d["command"] == "echo test"
        assert d["timeout"] == 30
        assert d["enabled"] is True

    def test_from_dict(self):
        d = {
            "id": "test-456",
            "name": "another-hook",
            "event": HOOK_EVENT_PRE_TOOL_USE,
            "command": "echo pre",
            "timeout": 10,
            "enabled": False,
            "matcher": "fs_*",
        }
        hook = ScriptHook.from_dict(d)
        assert hook.id == "test-456"
        assert hook.name == "another-hook"
        assert hook.event == HOOK_EVENT_PRE_TOOL_USE
        assert hook.command == "echo pre"
        assert hook.timeout == 10
        assert hook.enabled is False
        assert hook.matcher == "fs_*"


class TestScriptHookStore:
    """Test ScriptHookStore CRUD operations."""

    def test_create_hook(self, hook_store: ScriptHookStore):
        hook = hook_store.create(
            {
                "name": "test-create",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo hello",
                "timeout": 30,
            }
        )
        assert hook.name == "test-create"
        assert hook.event == HOOK_EVENT_USER_PROMPT_SUBMIT
        assert hook.enabled is True
        assert len(hook.id) > 0

    def test_get_hook(self, hook_store: ScriptHookStore):
        hook = hook_store.create(
            {
                "name": "test-get",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo test",
            }
        )
        retrieved = hook_store.get(hook.id)
        assert retrieved is not None
        assert retrieved.id == hook.id
        assert retrieved.name == "test-get"

    def test_get_nonexistent(self, hook_store: ScriptHookStore):
        assert hook_store.get("nonexistent-id") is None

    def test_list_hooks(self, hook_store: ScriptHookStore):
        hook_store.create(
            {"name": "hook1", "event": HOOK_EVENT_USER_PROMPT_SUBMIT, "command": "echo 1"}
        )
        hook_store.create({"name": "hook2", "event": HOOK_EVENT_PRE_TOOL_USE, "command": "echo 2"})
        hooks = hook_store.list_all()
        assert len(hooks) == 2
        assert {h.name for h in hooks} == {"hook1", "hook2"}

    def test_update_hook(self, hook_store: ScriptHookStore):
        hook = hook_store.create(
            {
                "name": "test-update",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo original",
            }
        )
        updated = hook_store.update(hook.id, {"name": "updated-name", "command": "echo updated"})
        assert updated is not None
        assert updated.name == "updated-name"
        assert updated.command == "echo updated"

    def test_update_nonexistent(self, hook_store: ScriptHookStore):
        result = hook_store.update("nonexistent", {"name": "foo"})
        assert result is None

    def test_delete_hook(self, hook_store: ScriptHookStore):
        hook = hook_store.create(
            {
                "name": "test-delete",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo delete",
            }
        )
        assert hook_store.delete(hook.id) is True
        assert hook_store.get(hook.id) is None

    def test_delete_nonexistent(self, hook_store: ScriptHookStore):
        assert hook_store.delete("nonexistent") is False

    def test_toggle_enabled(self, hook_store: ScriptHookStore):
        hook = hook_store.create(
            {
                "name": "test-toggle",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo toggle",
            }
        )
        assert hook.enabled is True

        toggled = hook_store.toggle(hook.id)
        assert toggled is not None
        assert toggled.enabled is False

        toggled_again = hook_store.toggle(hook.id)
        assert toggled_again is not None
        assert toggled_again.enabled is True

    def test_persistence(self, tmp_path: Path):
        """Test that hooks persist to disk."""
        store1 = ScriptHookStore(tmp_path)
        hook = store1.create(
            {
                "name": "persist-test",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo persist",
            }
        )

        # Load from same file
        store2 = ScriptHookStore(tmp_path)
        retrieved = store2.get(hook.id)
        assert retrieved is not None
        assert retrieved.name == "persist-test"


class TestCappedScriptHookOutput:
    @pytest.mark.asyncio
    async def test_reader_keeps_only_the_cap_but_drains_to_eof(self):
        from kiro_crew.hooks import _read_capped_stream

        reader = asyncio.StreamReader()
        reader.feed_data(b"abcdefgh")
        reader.feed_eof()

        retained, truncated = await _read_capped_stream(reader, 5)

        assert retained == b"abcde"
        assert truncated is True
        assert await reader.read() == b""

    def test_decode_marks_a_multibyte_boundary(self):
        from kiro_crew.hooks import _HOOK_TRUNCATION_MARKER, _decode_capped

        raw = ("€" * 2).encode("utf-8")[:4]
        decoded = _decode_capped(raw, truncated=True)

        assert decoded.startswith("€")
        assert "\ufffd" in decoded
        assert decoded.endswith(_HOOK_TRUNCATION_MARKER)

    @pytest.mark.asyncio
    async def test_cancellation_observes_both_reader_tasks(self):
        from kiro_crew.hooks import _communicate_capped

        class BlockingReader:
            def __init__(self):
                self.entered = asyncio.Event()
                self.cancelled = asyncio.Event()

            async def read(self, _size):
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise

        stdout = BlockingReader()
        stderr = BlockingReader()
        proc = SimpleNamespace(
            stdin=None,
            stdout=stdout,
            stderr=stderr,
            wait=AsyncMock(),
        )
        task = asyncio.create_task(_communicate_capped(proc, b"", 32))
        await asyncio.gather(stdout.entered.wait(), stderr.entered.wait())

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert stdout.cancelled.is_set()
        assert stderr.cancelled.is_set()
        proc.wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_real_hook_caps_and_marks_both_streams(self, tmp_path, monkeypatch):
        from kiro_crew.hooks import _HOOK_STREAM_CAP_BYTES, _HOOK_TRUNCATION_MARKER

        # This test exercises real pipe draining, not host sandbox discovery.
        # Keep the subprocess real while making its isolation wrappers stable
        # across Linux, namespace-sandbox, and Windows CI environments.
        monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))

        command = _script_command(
            tmp_path / "large_output.py",
            "import sys\n" "sys.stdout.write('o' * 70000)\n" "sys.stderr.write('e' * 70000)\n",
        )
        hook = ScriptHook(
            id="large-output",
            name="large-output",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command=command,
            timeout=30,
        )

        result = await run_script_hook(hook, "ctx")

        assert result.exit_code == 0
        assert result.stdout.endswith(_HOOK_TRUNCATION_MARKER)
        assert result.stderr.endswith(_HOOK_TRUNCATION_MARKER)
        assert len(result.stdout.encode("utf-8")) <= (
            _HOOK_STREAM_CAP_BYTES + len(_HOOK_TRUNCATION_MARKER.encode("utf-8"))
        )
        assert len(result.stderr.encode("utf-8")) <= (
            _HOOK_STREAM_CAP_BYTES + len(_HOOK_TRUNCATION_MARKER.encode("utf-8"))
        )


class TestRunScriptHook:
    """Test run_script_hook execution."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        # run_script_hook uses a lazy `from kiro_crew.sandbox import wrap_argv`
        # inside the function. Patch the source module so macOS 26 doesn't raise.
        monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        hook = ScriptHook(
            id="test-1",
            name="success",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command="echo success",
            timeout=30,
            enabled=True,
        )
        result = await run_script_hook(hook, "test-context")
        assert result.hook_id == "test-1"
        assert result.exit_code == 0
        assert "success" in result.stdout
        assert result.error == ""
        # ``>= 0``, not ``> 0``: ``duration_ms`` is ``int((monotonic() - start) *
        # 1000)``, so a command that finishes in under a millisecond truncates to
        # 0 legitimately — and ``echo`` on Windows' coarser clock does exactly
        # that, which made this a platform-dependent flake. The guarantee worth
        # asserting here is that the field is MEASURED and never negative; that it
        # tracks real elapsed time is pinned by ``test_timeout`` below, where the
        # hook runs long enough for the value to be meaningful (>= 1000).
        assert isinstance(result.duration_ms, int)
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_non_zero_exit(self):
        hook = ScriptHook(
            id="test-2",
            name="fail",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command="exit 1",
            timeout=30,
            enabled=True,
        )
        result = await run_script_hook(hook, "test-context")
        assert result.exit_code == 1
        assert result.error == ""  # exit code is not an error, just non-zero

    @pytest.mark.asyncio
    async def test_timeout(self, tmp_path: Path):
        hook = ScriptHook(
            id="test-3",
            name="timeout",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command=_script_command(tmp_path / "timeout.py", "import time\ntime.sleep(10)\n"),
            timeout=1,
            enabled=True,
        )
        result = await run_script_hook(hook, "test-context")
        assert "Timed out" in result.error
        assert result.duration_ms >= 1000  # at least 1 second

    @pytest.mark.asyncio
    async def test_exit_code_2_blocks(self):
        """Exit code 2 means hook blocks the operation."""
        hook = ScriptHook(
            id="test-4",
            name="block",
            event=HOOK_EVENT_PRE_TOOL_USE,
            command="exit 2",
            timeout=30,
            enabled=True,
        )
        result = await run_script_hook(hook, "test-context")
        assert result.exit_code == 2

    @pytest.mark.skipif(_IS_MACOS, reason="Flaky stdin piping through macOS sandbox")
    @pytest.mark.asyncio
    async def test_stdin_json(self, tmp_path: Path):
        """Hook receives JSON via stdin."""
        command = _script_command(
            tmp_path / "stdin_hook.py",
            'import sys, json; print(json.load(sys.stdin)["hook_event_name"])\n',
        )
        hook = ScriptHook(
            id="test-5",
            name="stdin",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command=command,
            timeout=30,
            enabled=True,
        )
        result = await run_script_hook(hook, "test-context")
        assert result.exit_code == 0, result.stderr
        assert HOOK_EVENT_USER_PROMPT_SUBMIT in result.stdout

    @pytest.mark.asyncio
    async def test_env_vars(self):
        """Hook receives context via environment variables."""
        hook = ScriptHook(
            id="test-6",
            name="env",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command=_ECHO_HOOK_EVENT,
            timeout=30,
            enabled=True,
        )
        result = await run_script_hook(hook, "test-context")
        assert HOOK_EVENT_USER_PROMPT_SUBMIT in result.stdout

    @pytest.mark.asyncio
    async def test_hook_updates_metadata(self):
        """Hook execution updates last_run, last_status, run_count."""
        hook = ScriptHook(
            id="test-7",
            name="metadata",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command="echo test",
            timeout=30,
            enabled=True,
            last_run=0,
            last_status="",
            run_count=0,
        )
        await run_script_hook(hook, "test-context")
        assert hook.last_run > 0
        assert hook.last_status == "ok"
        assert hook.run_count == 1


class TestScriptHookStoreFire:
    """Test ScriptHookStore.fire() method."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))

    @pytest.mark.asyncio
    async def test_fire_enabled_hooks(self, hook_store: ScriptHookStore):
        hook1 = hook_store.create(
            {
                "name": "enabled",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo enabled",
                "timeout": 30,
                "enabled": True,
            }
        )
        hook_store.create(
            {
                "name": "disabled",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo disabled",
                "timeout": 30,
                "enabled": False,
            }
        )
        results = await hook_store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "test-context")
        assert len(results) == 1
        assert results[0].hook_id == hook1.id

    @pytest.mark.asyncio
    async def test_fire_correct_event(self, hook_store: ScriptHookStore):
        hook_store.create(
            {
                "name": "prompt-hook",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo prompt",
                "timeout": 30,
            }
        )
        hook_store.create(
            {
                "name": "tool-hook",
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "command": "echo tool",
                "timeout": 30,
            }
        )
        results = await hook_store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "test-context")
        assert len(results) == 1
        assert "prompt" in results[0].stdout

    @pytest.mark.asyncio
    async def test_fire_with_matcher(self, hook_store: ScriptHookStore):
        hook_store.create(
            {
                "name": "fs-hook",
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "command": "echo matched",
                "timeout": 30,
                "matcher": "fs_*",
            }
        )
        # Should match
        results = await hook_store.fire(HOOK_EVENT_PRE_TOOL_USE, "test", tool_name="fs_write")
        assert len(results) == 1
        assert "matched" in results[0].stdout

        # Should not match
        results = await hook_store.fire(HOOK_EVENT_PRE_TOOL_USE, "test", tool_name="git_commit")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_fire_blocking_hook(self, hook_store: ScriptHookStore):
        """Exit code 2 means blocked."""
        hook_store.create(
            {
                "name": "blocker",
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "command": "exit 2",
                "timeout": 30,
            }
        )
        results = await hook_store.fire(HOOK_EVENT_PRE_TOOL_USE, "test")
        assert len(results) == 1
        assert results[0].exit_code == 2

    @pytest.mark.asyncio
    async def test_fire_multiple_hooks(self, hook_store: ScriptHookStore):
        """Multiple hooks for same event fire in order."""
        hook_store.create(
            {
                "name": "first",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo first",
                "timeout": 30,
            }
        )
        hook_store.create(
            {
                "name": "second",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo second",
                "timeout": 30,
            }
        )
        results = await hook_store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "test")
        assert len(results) == 2
        # Results maintain insertion order
        assert "first" in results[0].stdout
        assert "second" in results[1].stdout

    @pytest.mark.skipif(_IS_MACOS, reason="Flaky stdin piping through macOS sandbox")
    @pytest.mark.asyncio
    async def test_fire_with_tool_input(self, hook_store: ScriptHookStore, tmp_path: Path):
        """Tool input passed to hook via stdin."""
        command = _script_command(
            tmp_path / "tool_input_hook.py",
            'import sys, json; print(json.load(sys.stdin).get("tool_input", {}).get("test_key"))\n',
        )
        hook_store.create(
            {
                "name": "input-hook",
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "command": command,
                "timeout": 30,
            }
        )
        results = await hook_store.fire(
            HOOK_EVENT_PRE_TOOL_USE,
            "test",
            tool_name="test_tool",
            tool_input={"test_key": "test_value"},
        )
        assert len(results) == 1
        assert "test_value" in results[0].stdout, results[0].stderr

    @pytest.mark.asyncio
    async def test_fire_no_hooks(self, hook_store: ScriptHookStore):
        """Fire with no matching hooks returns empty list."""
        results = await hook_store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "test")
        assert results == []

    @pytest.mark.asyncio
    async def test_fire_agent_spawn_returns_stdout(self, hook_store: ScriptHookStore):
        """AgentSpawn hook stdout should be available for context injection."""
        hook_store.create(
            {
                "name": "startup-prefs",
                "event": HOOK_EVENT_AGENT_SPAWN,
                "command": "echo 'Enable caveman mode'",
                "timeout": 30,
            }
        )
        results = await hook_store.fire(HOOK_EVENT_AGENT_SPAWN, "session-key")
        assert len(results) == 1
        assert results[0].succeeded
        assert "caveman" in results[0].stdout


class TestRunScriptHookSpawnForm:
    """The spawn form per platform, and the guard that keeps isolation ahead of it.

    ``run_script_hook`` hands the command to the platform's shell two different
    ways, and each way carries an invariant these tests pin. Both run on every
    platform: the assertion is about which spawn the code CHOOSES, not about
    running a shell, so a POSIX CI still catches a regression in the Windows
    branch (and vice versa).
    """

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))

    @staticmethod
    def _hook(command: str) -> ScriptHook:
        return ScriptHook(
            id="spawn-form",
            name="spawn-form",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command=command,
            timeout=30,
            enabled=True,
        )

    @pytest.mark.asyncio
    async def test_command_reaches_the_shell_verbatim(self, monkeypatch):
        """The operator's quotes must survive to the shell unescaped.

        On Windows an argv spawn of ``["cmd", "/c", command]`` would route the
        line through ``subprocess.list2cmdline``, which backslash-escapes every
        quote — so a quoted interpreter path (unavoidable when it contains a
        space) would reach cmd.exe as a backslash-escaped ``\\"C:\\...\\"`` and
        fail. Whichever spawn this platform picks, the command string itself must
        be passed through untouched.
        """
        command = r'"C:\Program Files\Py\python.exe" -c "print(1)"'
        seen: dict[str, object] = {}

        fake_proc = MagicMock()
        fake_proc.communicate = AsyncMock(return_value=(b"", b""))
        fake_proc.returncode = 0

        async def fake_shell(cmd, **kwargs):
            seen["shell_cmd"] = cmd
            return fake_proc

        async def fake_exec(*argv, **kwargs):
            seen["argv"] = list(argv)
            return fake_proc

        # THREE layers can prepend to the argv before it reaches a real spawn:
        # wrap_argv (OS sandbox), cgroup_scope_argv (cgroup v2), and
        # create_subprocess_limited's own RLIMIT shim. Capturing at
        # create_subprocess_limited — the boundary hooks.py actually calls — sees
        # the argv hooks.py BUILT, independent of which of the three a host
        # offers. Patching asyncio.create_subprocess_exec instead made the
        # assertion host-dependent: green where no backend exists (Windows, this
        # box) and red on the namespace-sandbox job, which has all three.
        monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))
        monkeypatch.setattr("kiro_crew.sandbox.create_subprocess_limited", fake_exec)
        monkeypatch.setattr("asyncio.create_subprocess_shell", fake_shell)
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

        await run_script_hook(self._hook(command), "ctx")

        if _IS_WINDOWS:
            # No argv, hence no list2cmdline, hence no quote mangling.
            assert seen.get("shell_cmd") == command
            assert "argv" not in seen
        else:
            assert seen["argv"] == ["/bin/sh", "-c", command]

    @pytest.mark.asyncio
    async def test_a_wrapping_sandbox_wins_over_the_shell_spawn(self, monkeypatch):
        """A wrapper that prepends argv must own the spawn, quoting notwithstanding.

        The Windows shell spawn is deliberately guarded on ``wrap_argv`` +
        ``cgroup_scope_argv`` having been no-ops. Should an isolation backend ever
        prepend anything on Windows, the shell form would silently drop that
        wrapper — so the code must fall back to the argv path instead.
        """
        monkeypatch.setattr(
            "kiro_crew.sandbox.wrap_argv", lambda argv, **k: (["sandbox-exec", *argv], None)
        )
        # cgroup_scope_argv runs AFTER wrap_argv and prepends its own launcher on
        # a cgroup-v2 host, which would displace "sandbox-exec" from argv[0]. Pin
        # it to a no-op so the assertion names the wrapper this test installed.
        monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))
        seen: dict[str, object] = {}

        fake_proc = MagicMock()
        fake_proc.communicate = AsyncMock(return_value=(b"", b""))
        fake_proc.returncode = 0

        async def fake_shell(cmd, **kwargs):
            seen["shell_cmd"] = cmd
            return fake_proc

        async def fake_exec(*argv, **kwargs):
            seen["argv"] = list(argv)
            return fake_proc

        # Capture at create_subprocess_limited so its RLIMIT shim cannot displace
        # "sandbox-exec" from argv[0] (see the sibling test above).
        monkeypatch.setattr("kiro_crew.sandbox.create_subprocess_limited", fake_exec)
        monkeypatch.setattr("asyncio.create_subprocess_shell", fake_shell)
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

        await run_script_hook(self._hook("echo hi"), "ctx")

        assert "shell_cmd" not in seen, "a wrapped argv must not be discarded for a shell spawn"
        assert seen["argv"][0] == "sandbox-exec"


class TestLastError:
    """Test that last_error is populated on failure and cleared on success."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))

    @pytest.mark.asyncio
    async def test_last_error_cleared_on_success(self):
        hook = ScriptHook(
            id="err-1",
            name="last-error-clear",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command="echo ok",
            timeout=30,
            enabled=True,
            last_error="old error",
        )
        await run_script_hook(hook, "ctx")
        assert hook.last_status == "ok"
        assert hook.last_error == ""

    @pytest.mark.asyncio
    async def test_last_error_populated_on_non_zero_exit(self, tmp_path: Path):
        hook = ScriptHook(
            id="err-2",
            name="last-error-exit",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command=_script_command(
                tmp_path / "exit_one.py",
                'import sys\nsys.stderr.write("oops\\n")\nsys.exit(1)\n',
            ),
            timeout=30,
            enabled=True,
        )
        await run_script_hook(hook, "ctx")
        assert hook.last_status == "error"
        assert "oops" in hook.last_error

    @pytest.mark.asyncio
    async def test_last_error_on_timeout(self, tmp_path: Path):
        hook = ScriptHook(
            id="err-3",
            name="last-error-timeout",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command=_script_command(
                tmp_path / "last_error_timeout.py", "import time\ntime.sleep(10)\n"
            ),
            timeout=1,
            enabled=True,
        )
        await run_script_hook(hook, "ctx")
        assert hook.last_status == "timeout"
        assert "Timed out after 1s" in hook.last_error

    @pytest.mark.asyncio
    async def test_last_error_on_exit_2_blocked(self, tmp_path: Path):
        hook = ScriptHook(
            id="err-4",
            name="last-error-blocked",
            event=HOOK_EVENT_PRE_TOOL_USE,
            command=_script_command(
                tmp_path / "exit_two.py",
                'import sys\nsys.stderr.write("block-reason\\n")\nsys.exit(2)\n',
            ),
            timeout=30,
            enabled=True,
        )
        await run_script_hook(hook, "ctx")
        assert hook.last_status == "blocked"
        assert "block-reason" in hook.last_error

    @pytest.mark.asyncio
    async def test_last_error_fallback_when_no_stderr(self):
        hook = ScriptHook(
            id="err-5",
            name="last-error-no-stderr",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command="exit 42",
            timeout=30,
            enabled=True,
        )
        await run_script_hook(hook, "ctx")
        assert hook.last_status == "error"
        assert "42" in hook.last_error

    def test_last_error_serialization_roundtrip(self):
        hook = ScriptHook(
            id="err-6",
            name="serial",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command="echo hi",
            last_error="something went wrong",
        )
        data = hook.to_dict()
        assert data["last_error"] == "something went wrong"
        restored = ScriptHook.from_dict(data)
        assert restored.last_error == "something went wrong"

    def test_last_error_defaults_empty_on_missing_key(self):
        hook = ScriptHook.from_dict({"id": "old", "name": "legacy"})
        assert hook.last_error == ""

    def test_last_error_redacted_on_load_from_persisted_data(self):
        # hooks.json is operator-writable; a persisted last_error can carry a
        # credential. from_dict() must scrub it before it reaches /api/hooks and
        # the dashboard InfoTip — not only the runtime write path.
        secret = "AKIAIOSFODNN7EXAMPLE"
        hook = ScriptHook.from_dict(
            {"id": "sek", "name": "leaky", "last_error": f"auth failed: {secret}"}
        )
        assert secret not in hook.last_error

    def test_last_error_non_string_persisted_defaults_empty(self):
        # A non-string last_error in persisted data must not crash the redactor
        # and must default to "".
        hook = ScriptHook.from_dict({"id": "bad", "name": "x", "last_error": 12345})
        assert hook.last_error == ""
