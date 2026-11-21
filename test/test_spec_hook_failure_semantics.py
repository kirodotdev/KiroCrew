"""End-to-end tests for an agent spec's own hooks on the KAS backend.

On KAS, Crew runs a spec's ``hooks`` in the harness's place. Two reports from 0.11
users drive these tests, each through the real loader, the real hook spawn and
the real PreToolUse gate:

* A user-level PreToolUse telemetry hook whose command matched a shell deny
  rule was refused, reported as exit 2, and so blocked every tool call. The hook
  is operator-written into the read-only user-level agents tree; it must run as
  kiro-cli runs it, and the tool must go ahead.
* A PreToolUse guard hook killed by endpoint security (exit -9) blocked every
  tool call. kiro-cli warns on any exit but 2 and lets the tool run; so must Crew.

A delivered exit 2 still blocks, and a Hooks-page hook keeps failing closed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook, ScriptHookStore

_AGENT = "a1"


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture(autouse=True)
def _passthrough_sandbox(_floor_monkeypatch):
    # A plain shell spawn, so each assertion turns on the deny gate and the exit
    # code, not on the host's sandbox backend.
    _floor_monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
    _floor_monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


def _witness(tmp_path: Path, name: str) -> tuple[str, Path]:
    """A command that creates a file when it runs, and that file."""
    sentinel = tmp_path / f"{name}.ran"
    script = tmp_path / f"{name}.py"
    script.write_text(f"open({str(sentinel)!r}, 'w').close()\n", encoding="utf-8")
    return f'"{sys.executable}" "{script}"', sentinel


def _deny_listed(tmp_path: Path, name: str) -> tuple[str, Path]:
    """A harmless command that a default shell deny rule (``chmod 777``) refuses."""
    witness, sentinel = _witness(tmp_path, name)
    probe = tmp_path / "probe"
    probe.write_text("", encoding="utf-8")
    return f"{witness} && chmod 777 {probe}", sentinel


def _write_spec(agents_dir: Path, command: str) -> None:
    spec = {"name": _AGENT, "prompt": "p", "hooks": {"preToolUse": [{"command": command}]}}
    (agents_dir / f"{_AGENT}.json").write_text(json.dumps(spec), encoding="utf-8")


def _gate(tmp_path: Path, store: ScriptHookStore) -> str | None:
    """Load the agent's spec hooks as a KAS turn does, then run the PreToolUse gate."""
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd=str(tmp_path))
    turn = asyncio.run(spec_hooks.turn_spec_hooks(provider, _AGENT))
    assert not turn.unreadable and turn.gated and turn.hooks
    return asyncio.run(
        hooks_mod.permission_pre_tool_block(store, turn.hooks, turn.cwd, "Running: ReadFile")
    )


def test_a_deny_listed_user_spec_hook_runs_and_the_tool_goes_ahead(tmp_path, agents_dir):
    from kiro_crew.security import is_denied

    command, sentinel = _deny_listed(tmp_path, "spec")
    assert is_denied(command), "precondition: the command matches a default deny rule"
    _write_spec(agents_dir, command)
    assert _gate(tmp_path, ScriptHookStore(config_dir=tmp_path)) is None
    assert sentinel.exists(), "the user-level spec hook must run, as on kiro-cli"


def test_a_governance_pinned_rule_still_refuses_a_user_spec_hook(tmp_path, agents_dir, monkeypatch):
    # The exemption covers the operator's own deny rules, not an administrator's
    # pin: a pinned rule refuses the user-level spec hook, so its command never
    # runs. A refused spec hook blocks the tool, with the refusal as the reason
    # (P0-12): the guard never gave a verdict.
    monkeypatch.setattr(
        "kiro_crew.security.pinned_builtin_command_ids",
        lambda: {"local-destructive-chmod-777"},
    )
    command, sentinel = _deny_listed(tmp_path, "pinned")
    _write_spec(agents_dir, command)
    reason = _gate(tmp_path, ScriptHookStore(config_dir=tmp_path))
    assert reason is not None and "security policy" in reason.lower()
    assert not sentinel.exists(), "a governance-pinned command must not run"


def test_the_same_command_as_a_hooks_page_hook_is_still_refused(tmp_path, agents_dir):
    # hooks.json is writable in-sandbox, so a stored hook stays deny-list vetted
    # and fail-closed: it does not run, and the tool is blocked with the reason.
    _write_spec(agents_dir, _witness(tmp_path, "benign")[0])
    command, sentinel = _deny_listed(tmp_path, "page")
    store = ScriptHookStore(config_dir=tmp_path)
    store._hooks = {
        "page": ScriptHook(id="page", name="page", event=HOOK_EVENT_PRE_TOOL_USE, command=command)
    }
    reason = _gate(tmp_path, store)
    assert reason is not None and "security policy" in reason.lower()
    assert not sentinel.exists()


def test_a_hooks_page_hook_cannot_pass_as_a_spec_hook_by_its_id(tmp_path, agents_dir):
    # The spec-hook rules key on an in-memory origin, never on the id a
    # hand-written hooks.json controls.
    _write_spec(agents_dir, _witness(tmp_path, "benign")[0])
    command, sentinel = _deny_listed(tmp_path, "forged")
    store = ScriptHookStore(config_dir=tmp_path)
    forged = ScriptHook.from_dict(
        {
            "id": f"spec:{_AGENT}:PreToolUse:0",
            "event": HOOK_EVENT_PRE_TOOL_USE,
            "command": command,
            "spec_origin": "user",
        }
    )
    store._hooks = {forged.id: forged}
    assert _gate(tmp_path, store) is not None
    assert not sentinel.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal")
def test_a_killed_spec_hook_does_not_block_the_tool(tmp_path, agents_dir):
    # Endpoint security killing the hook process leaves exit -9.
    _write_spec(agents_dir, "kill -9 $$")
    assert _gate(tmp_path, ScriptHookStore(config_dir=tmp_path)) is None


def test_a_crashing_spec_hook_does_not_block_the_tool(tmp_path, agents_dir):
    _write_spec(agents_dir, "exit 1")
    assert _gate(tmp_path, ScriptHookStore(config_dir=tmp_path)) is None


def test_a_spec_hook_that_exits_2_still_blocks(tmp_path, agents_dir):
    # ``&&`` chains in both ``/bin/sh -c`` and Windows ``cmd /c``; ``;`` is sh-only.
    _write_spec(agents_dir, "echo no-reads 1>&2 && exit 2")
    reason = _gate(tmp_path, ScriptHookStore(config_dir=tmp_path))
    assert reason is not None and "no-reads" in reason


def test_a_spec_linked_in_from_outside_the_agents_dir_stays_vetted(tmp_path, monkeypatch):
    from kiro_crew.agent_sdk.spec_hooks import _user_dir_origin
    from kiro_crew.config import paths

    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: agents)
    mates = tmp_path / "mates"
    mates.mkdir()
    (mates / "conductor.json").write_text("{}")
    assert _user_dir_origin(mates) == "project"  # a sibling tree is not sealed
    (agents / "plain.json").write_text("{}")
    assert _user_dir_origin(agents) == "user"

    # Any symlinked spec stays vetted, even one resolving inside the tree: a link
    # it passes through could be retargeted after the spec is read.
    (agents / "inner.json").symlink_to(agents / "plain.json")
    assert _user_dir_origin(agents) == "project"
    (agents / "inner.json").unlink()

    outside = tmp_path / "checkout" / "linked.json"
    outside.parent.mkdir()
    outside.write_text("{}")
    (agents / "linked.json").symlink_to(outside)
    assert _user_dir_origin(agents) == "project"


def test_a_kas_folder_agent_hook_keeps_the_full_deny_list_vet(tmp_path, agents_dir, monkeypatch):
    # A folder agent's hooks run on KAS once the folder is turned on, but they come
    # from a checkout an agent can write, so the user-level exemption above must not
    # reach them: the same deny-listed command is refused and never runs.
    import kiro_crew.project_hooks_consent as consent

    store_file = tmp_path / "project_hooks_consent.json"
    monkeypatch.setattr(consent, "project_hooks_consent_path", lambda: store_file)
    mates = tmp_path / "mates"
    mates.mkdir()
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    checkout = tmp_path / "checkout"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    command, sentinel = _deny_listed(tmp_path, "folder")
    _write_spec(checkout / ".kiro" / "agents", command)
    consent.set_choice(checkout, True)
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd=str(checkout))
    turn = asyncio.run(spec_hooks.turn_spec_hooks(provider, _AGENT))
    assert [h.spec_origin for h in turn.hooks] == ["project"]
    reason = asyncio.run(
        hooks_mod.permission_pre_tool_block(
            ScriptHookStore(config_dir=tmp_path), turn.hooks, turn.cwd, "Running: ReadFile"
        )
    )
    assert reason is not None and "security policy" in reason.lower()
    assert not sentinel.exists(), "a folder agent's hook gets no user-level exemption"


# --- A spec hook over cmd.exe's line limit blocks ------------------------
#
# A crash warns, as on kiro-cli. A hook whose line is over cmd.exe's limit never
# runs and gives no verdict, so letting the tool go ahead would skip the guard
# unseen: it blocks, as a policy refusal does. A policy refusal is checked first
# and keeps its own reason. Every other non-zero exit, a missing command
# included, is the command's own and still warns.


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh exit codes")
def test_a_posix_spec_hook_that_ran_then_exited_127_does_not_block(tmp_path, agents_dir):
    # The guard ran; its last step hit a missing tool (sh exit 127).
    _write_spec(agents_dir, f"echo checked && {tmp_path / 'no-such-tool'}")
    assert _gate(tmp_path, ScriptHookStore(config_dir=tmp_path)) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_on_posix_a_command_past_cmd_exes_limit_still_runs(tmp_path, agents_dir):
    # 9,000 characters is under the 20,480 POSIX bound: kept, run, and its exit 2 blocks.
    head, tail = "echo ", " >/dev/null && echo guard-said-no 1>&2 && exit 2"
    _write_spec(agents_dir, head + "x" * (9000 - len(head) - len(tail)) + tail)
    reason = _gate(tmp_path, ScriptHookStore(config_dir=tmp_path))
    assert reason is not None and "guard-said-no" in reason


class _WindowsExit:
    """A real process whose exit code reads as Windows', which is not cut to 8 bits."""

    def __init__(self, proc, code: int):
        self._proc, self._code = proc, code

    def __getattr__(self, name):
        return getattr(self._proc, name)

    @property
    def returncode(self):
        return None if self._proc.returncode is None else self._code


def _as_windows_cmd(monkeypatch, script: str, code: int) -> list[str]:
    """Run the hook as Windows does, with cmd.exe's answer played by ``sh``.

    The gate takes the Windows spawn (``create_subprocess_shell``); the stand-in
    ignores the command and gives cmd.exe's stderr and exit code instead. The
    returned list collects every command handed to the spawn.
    """
    from kiro_crew import platform_compat

    real_exec = asyncio.create_subprocess_exec
    spawned: list[str] = []

    async def _cmd(command, **kwargs):
        spawned.append(command)
        kwargs.pop("creationflags", None)
        return _WindowsExit(await real_exec("/bin/sh", "-c", script, **kwargs), code)

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _cmd)
    return spawned


def _gate_windows(
    tmp_path: Path, monkeypatch, script: str, code: int
) -> tuple[str | None, list[str]]:
    # Loaded as POSIX, so only the spawn and its answer are Windows'.
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd=str(tmp_path))
    turn = asyncio.run(spec_hooks.turn_spec_hooks(provider, _AGENT))
    assert turn.hooks
    spawned = _as_windows_cmd(monkeypatch, script, code)
    reason = asyncio.run(
        hooks_mod.permission_pre_tool_block(
            ScriptHookStore(config_dir=tmp_path), turn.hooks, turn.cwd, "Running: ReadFile"
        )
    )
    return reason, spawned


#: cmd.exe's 8,191-character line less the ``%ComSpec% /c ""`` wrapper with
#: ComSpec unset (``C:\Windows\System32\cmd.exe``).
_DEFAULT_WINDOWS_LIMIT = 8158


def _windows_limit(monkeypatch) -> int:
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    limit = hooks_mod._windows_command_limit()
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    return limit


def test_the_windows_limit_is_cmd_exes_line_less_the_wrapper(monkeypatch):
    from kiro_crew import platform_compat

    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")
    assert _windows_limit(monkeypatch) == 8191 - len(r'C:\Windows\system32\cmd.exe /c ""')
    monkeypatch.delenv("COMSPEC")
    assert _windows_limit(monkeypatch) == _DEFAULT_WINDOWS_LIMIT
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    assert hooks_mod._windows_command_limit() is None


@pytest.mark.skipif(sys.platform == "win32", reason="cmd.exe played by /bin/sh")
@pytest.mark.parametrize("over", [1, 34, 842], ids=["one-over", "8192", "9000-repro"])
def test_on_windows_a_command_over_cmd_exes_limit_blocks_unrun(
    tmp_path, agents_dir, monkeypatch, over
):
    monkeypatch.delenv("COMSPEC", raising=False)
    length = _DEFAULT_WINDOWS_LIMIT + over
    _write_spec(agents_dir, "echo " + "x" * (length - 5))
    reason, spawned = _gate_windows(tmp_path, monkeypatch, "exit 0", 0)
    assert spawned == [], "a line cmd.exe would refuse is not spawned"
    assert reason is not None and "did not start" in reason
    assert f"{length:,} characters" in reason and "cmd.exe's 8,158-character" in reason
    assert "script" in reason


@pytest.mark.skipif(sys.platform == "win32", reason="cmd.exe played by /bin/sh")
def test_on_windows_a_command_at_cmd_exes_limit_runs(tmp_path, agents_dir, monkeypatch):
    monkeypatch.delenv("COMSPEC", raising=False)
    _write_spec(agents_dir, "echo " + "x" * (_DEFAULT_WINDOWS_LIMIT - 5))
    reason, spawned = _gate_windows(tmp_path, monkeypatch, "echo guard-said-no >&2; exit 2", 2)
    assert len(spawned) == 1
    assert reason is not None and "guard-said-no" in reason


@pytest.mark.skipif(sys.platform == "win32", reason="cmd.exe played by /bin/sh")
def test_on_windows_a_governance_refusal_of_a_long_hook_still_blocks(
    tmp_path, agents_dir, monkeypatch
):
    # Policy comes first: the refusal blocks with its own reason, as on POSIX.
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": "hooks off")
    _write_spec(agents_dir, "echo " + "x" * 9000)
    reason, spawned = _gate_windows(tmp_path, monkeypatch, "exit 0", 0)
    assert spawned == []
    assert reason is not None and "governance" in reason.lower()
    assert "did not start" not in reason


@pytest.mark.skipif(sys.platform == "win32", reason="cmd.exe played by /bin/sh")
def test_on_windows_a_deny_listed_long_hook_still_blocks(tmp_path, agents_dir, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.security.pinned_builtin_command_ids",
        lambda: {"local-destructive-chmod-777"},
    )
    command, sentinel = _deny_listed(tmp_path, "long")
    _write_spec(agents_dir, command + " && echo " + "x" * 9000)
    reason, spawned = _gate_windows(tmp_path, monkeypatch, "exit 0", 0)
    assert spawned == [] and not sentinel.exists()
    assert reason is not None and "security policy" in reason.lower()


@pytest.mark.skipif(sys.platform == "win32", reason="cmd.exe played by /bin/sh")
@pytest.mark.parametrize(
    "message",
    ["The command line is too long.", "The input line is too long."],
    ids=["command-line", "input-line"],
)
def test_on_windows_a_line_cmd_exe_refuses_as_too_long_blocks(
    tmp_path, agents_dir, monkeypatch, message
):
    # A short literal whose %VAR% expands past the limit at run time.
    _write_spec(agents_dir, "guard.cmd %BIG%")
    reason, _ = _gate_windows(tmp_path, monkeypatch, f"echo '{message}' >&2", 1)
    assert reason is not None and "did not start (exit 1)" in reason


@pytest.mark.skipif(sys.platform == "win32", reason="cmd.exe played by /bin/sh")
def test_on_windows_a_9009_exit_still_does_not_block(tmp_path, agents_dir, monkeypatch):
    # cmd.exe 9009 (not recognized) is warn-and-allow.
    _write_spec(agents_dir, "no-such-guard.exe")
    reason, _ = _gate_windows(
        tmp_path,
        monkeypatch,
        "echo \"'no-such-guard.exe' is not recognized as an internal or external command\" >&2",
        9009,
    )
    assert reason is None


@pytest.mark.skipif(sys.platform == "win32", reason="cmd.exe played by /bin/sh")
def test_on_windows_a_crashing_spec_hook_still_does_not_block(tmp_path, agents_dir, monkeypatch):
    _write_spec(agents_dir, "guard.cmd")
    assert _gate_windows(tmp_path, monkeypatch, "echo boom >&2", 1)[0] is None


# --- The same, on a real cmd.exe (Windows CI only) --------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="needs a real cmd.exe")
def test_on_real_windows_a_hook_at_the_limit_runs_and_blocks(tmp_path, agents_dir):
    # A literal command exactly at the limit is spawned, and cmd.exe runs it:
    # its exit 2 blocks, so the limit leaves room for the ``%ComSpec% /c ""`` wrapper.
    limit = hooks_mod._windows_command_limit()
    head, tail = "echo ", " 1>&2 && exit 2"
    _write_spec(agents_dir, head + "x" * (limit - len(head) - len(tail)) + tail)
    reason = _gate(tmp_path, ScriptHookStore(config_dir=tmp_path))
    assert reason is not None and "did not start" not in reason


@pytest.mark.skipif(sys.platform != "win32", reason="needs a real cmd.exe")
def test_on_real_windows_a_9000_character_hook_blocks(tmp_path, agents_dir):
    # The issue's repro: a guard cmd.exe cannot run blocks the tool, naming the fix.
    head, tail = "echo ", " 1>&2 && exit 0"
    _write_spec(agents_dir, head + "x" * (9000 - len(head) - len(tail)) + tail)
    reason = _gate(tmp_path, ScriptHookStore(config_dir=tmp_path))
    assert reason is not None and "did not start" in reason and "script" in reason


@pytest.mark.skipif(sys.platform != "win32", reason="needs a real cmd.exe")
def test_on_real_windows_a_variable_expanded_past_the_limit_still_guards(tmp_path, agents_dir):
    # A short literal whose %PATH% copies expand past cmd.exe's line. Either cmd.exe
    # runs it, so the guard's exit 2 blocks, or it refuses the line, which blocks as
    # not started. The tool never goes ahead unguarded.
    copies = 8191 // max(1, len(os.environ.get("PATH", ""))) + 2
    if copies > 100:
        pytest.skip("PATH too short to expand past cmd.exe's line")
    _write_spec(agents_dir, "echo " + "%PATH%" * copies + " > NUL && exit 2")
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd=str(tmp_path))
    turn = asyncio.run(spec_hooks.turn_spec_hooks(provider, _AGENT))
    result = asyncio.run(hooks_mod.run_script_hook(turn.hooks[0], cwd=turn.cwd))
    result.from_spec = True
    assert result.pre_tool_blocks, (result.exit_code, result.stderr[:200], result.stdout[:200])


@pytest.mark.skipif(sys.platform != "win32", reason="needs a real cmd.exe")
def test_on_real_windows_a_missing_command_still_does_not_block(tmp_path, agents_dir):
    # cmd.exe 9009 (not recognized) warns and lets the tool run.
    _write_spec(agents_dir, "no-such-guard-38514")
    assert _gate(tmp_path, ScriptHookStore(config_dir=tmp_path)) is None
