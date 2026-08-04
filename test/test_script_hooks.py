"""Tests for script hooks system (ScriptHookStore, run_script_hook, etc.)."""

from __future__ import annotations

import platform
import sys
from pathlib import Path
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
        assert result.duration_ms > 0

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
    async def test_timeout(self):
        hook = ScriptHook(
            id="test-3",
            name="timeout",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command="sleep 10",
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
