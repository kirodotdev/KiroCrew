"""The Strands gate module's own behaviour, run against stand-in ``strands`` modules.

The gate module is loaded by the Strands CLI's Python worker, never by Kiro Crew, and
the Strands SDK is not a Kiro Crew dependency. So these tests load the module from its
packaged path with small stand-ins for exactly the ``strands`` names it imports, and
drive the one behaviour that lives in its own code: what happens to the next prompt
when a cancel left an approval interrupt open. On Strands CLI 0.2.3 that state is
saved with the session, and Strands refuses every later prompt and reload with "must
resume from interrupt"; the gate denies the open interrupts first, so the tool never
runs and the session keeps working. The same behaviour was checked on the real SDK
(``strands-agents`` 1.63.0) with a scripted model: red without the fix, green with it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

GATE = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "kiro_crew"
    / "agent_sdk"
    / "gate_extensions"
    / "strands"
    / "kiro_crew_strands_gate.py"
)


class _Interrupt:
    def __init__(self, response: Any = None) -> None:
        self.response = response


class _InterruptState:
    def __init__(self) -> None:
        self.activated = False
        self.interrupts: dict[str, _Interrupt] = {}
        self.deactivated = False

    def deactivate(self) -> None:
        self.activated = False
        self.interrupts = {}
        self.deactivated = True


class _Hooks:
    def add_hook(self, _hook: Any) -> None:
        pass


class _LocalSandbox:
    pass


class _MCPAgentTool:
    pass


def _stand_in_strands(monkeypatch: pytest.MonkeyPatch) -> type:
    """Install stand-ins for every ``strands`` name the gate imports; return ``Agent``."""

    class Agent:
        #: What the real ``Agent.stream_async`` does with interrupt state, reduced to
        #: the rule the fix answers: while an interrupt is open, only a list of
        #: interrupt responses is accepted.
        keep_interrupt_open = False

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.hooks = _Hooks()
            self.sandbox = _LocalSandbox()
            self._interrupt_state = _InterruptState()
            self.prompts: list[Any] = []

        async def stream_async(self, prompt: Any = None, **kwargs: Any):
            state = self._interrupt_state
            if state.activated:
                if not (isinstance(prompt, list) and all("interruptResponse" in p for p in prompt)):
                    raise TypeError("must resume from interrupt with list of interruptResponse's")
                for item in prompt:
                    answer = item["interruptResponse"]
                    state.interrupts[answer["interruptId"]].response = answer["response"]
                if not type(self).keep_interrupt_open:
                    state.activated = False
                    state.interrupts = {}
            self.prompts.append(prompt)
            yield {"result": "ok"}

    def module(name: str, **attrs: Any) -> types.ModuleType:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    local = module(
        "strands.sandbox.not_a_sandbox_local_environment",
        NotASandboxLocalEnvironment=_LocalSandbox,
        _stream_process=lambda *a, **k: None,
    )
    module("strands", Agent=Agent)
    module("strands.hooks", HookProvider=object, HookRegistry=object)
    module("strands.hooks.events", BeforeToolCallEvent=object)
    module("strands.sandbox", not_a_sandbox_local_environment=local)
    module("strands.sandbox.types", ExecutionResult=object)
    module("strands.session", SnapshotSessionManager=object)
    module("strands.storage", LocalFileStorage=object)
    module("strands.tools", mcp=None)
    module("strands.tools.mcp", MCPAgentTool=_MCPAgentTool)
    module("strands_harness", create_harness=lambda *a, **k: Agent())
    return Agent


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch) -> type:
    for name in (
        "KIROCREW_STRANDS_PROJECT",
        "KIROCREW_STRANDS_GATE_MARKER",
        "KIROCREW_STRANDS_GATE_SESSION",
        "KIROCREW_STRANDS_WITHHELD",
    ):
        monkeypatch.delenv(name, raising=False)
    agent_cls = _stand_in_strands(monkeypatch)
    spec = importlib.util.spec_from_file_location("kiro_crew_strands_gate_under_test", GATE)
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    agent_cls.gate = gate
    return agent_cls


def _run(agent: Any, prompt: Any) -> None:
    async def drive() -> None:
        async for _event in agent.stream_async(prompt):
            pass

    asyncio.run(drive())


def test_an_interrupt_a_cancel_left_open_is_denied_before_the_next_prompt(gated) -> None:
    agent = gated()
    agent._interrupt_state.activated = True
    agent._interrupt_state.interrupts = {"v1:before_tool_call:tooluse_1:x": _Interrupt()}

    _run(agent, "hello again")

    # The open interrupt was answered with a DENY first, then the prompt ran.
    assert agent.prompts == [
        [
            {
                "interruptResponse": {
                    "interruptId": "v1:before_tool_call:tooluse_1:x",
                    "response": False,
                }
            }
        ],
        "hello again",
    ]
    assert agent._interrupt_state.activated is False


def test_a_prompt_with_no_open_interrupt_runs_unchanged(gated) -> None:
    agent = gated()
    _run(agent, "hello")
    assert agent.prompts == ["hello"]


def test_the_harness_own_interrupt_answers_pass_straight_through(gated) -> None:
    """The CLI answering an approval is not a new prompt and is never pre-denied."""
    agent = gated()
    agent._interrupt_state.activated = True
    agent._interrupt_state.interrupts = {"i1": _Interrupt()}
    answer = [{"interruptResponse": {"interruptId": "i1", "response": True}}]

    _run(agent, answer)

    assert agent.prompts == [answer]


def test_a_deny_pass_that_stays_paused_never_blocks_the_prompt(gated) -> None:
    """Bounded: if denying keeps the agent paused, the state is cleared and the prompt runs."""
    gated.keep_interrupt_open = True
    agent = gated()
    agent._interrupt_state.activated = True
    agent._interrupt_state.interrupts = {"i1": _Interrupt()}

    _run(agent, "hello again")

    # One deny was sent; the stand-in kept the agent paused, so the gate cleared the
    # state rather than leave the session refusing every prompt.
    assert agent.prompts[0] == [{"interruptResponse": {"interruptId": "i1", "response": False}}]
    assert agent.prompts[-1] == "hello again"
    assert agent._interrupt_state.deactivated is True


def _tool_from(module: str) -> Any:
    def func() -> None:
        pass

    func.__module__ = module
    return types.SimpleNamespace(_tool_func=func)


def _event(name: str, tool: Any) -> Any:
    return types.SimpleNamespace(tool_use={"name": name}, selected_tool=tool)


@pytest.mark.parametrize(
    ("name", "module", "kind"),
    [
        ("read", "strands_harness.tools.file_tools", "read"),
        ("write", "strands_harness.tools.file_tools", "edit"),
        ("shell", "strands.vended_tools.shell.shell", "execute"),
        ("web_fetch", "strands_harness.tools.web_fetch", "fetch"),
    ],
)
def test_a_harness_builtin_keeps_its_kind(gated, name, module, kind) -> None:
    assert gated.gate._tool_identity(_event(name, _tool_from(module))) == (name, kind)


@pytest.mark.parametrize("name", ["read", "shell", "web_fetch"])
def test_a_project_tool_wearing_a_builtin_name_gets_no_builtin_kind(gated, name) -> None:
    """A ``read`` kind is auto-approved by the read-only tier, so a custom tool named
    ``read`` (one that marks mail as read, say) must not inherit it."""
    title, kind = gated.gate._tool_identity(_event(name, _tool_from("my_project.agent")))
    assert (title, kind) == (name, "other")


def test_a_tool_with_no_function_gets_no_builtin_kind(gated) -> None:
    assert gated.gate._tool_identity(_event("read", object())) == ("read", "other")


# ── The provider keys never sit in a launch environment ──

#: A child that installs stand-in ``strands`` modules, loads the gate module from its
#: path, says whether the key reached its ``os.environ``, then waits for stdin to close
#: so the test can read its ``/proc/<pid>/environ`` while it is alive.
_CHILD = r"""
import importlib.util, json, os, sys, types

class _Hooks:
    def add_hook(self, hook): pass

class _Local: pass

class Agent:
    def __init__(self, *a, **k):
        self.hooks = _Hooks(); self.sandbox = _Local()
    async def stream_async(self, prompt=None, **k):
        yield {"result": "ok"}

def module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod

local = module("strands.sandbox.not_a_sandbox_local_environment",
               NotASandboxLocalEnvironment=_Local, _stream_process=lambda *a, **k: None)
module("strands", Agent=Agent)
module("strands.hooks", HookProvider=object, HookRegistry=object)
module("strands.hooks.events", BeforeToolCallEvent=object)
module("strands.sandbox", not_a_sandbox_local_environment=local)
module("strands.sandbox.types", ExecutionResult=object)
module("strands.session", SnapshotSessionManager=object)
module("strands.storage", LocalFileStorage=object)
module("strands.tools", mcp=None)
module("strands.tools.mcp", MCPAgentTool=object)
module("strands_harness", create_harness=lambda *a, **k: Agent())

spec = importlib.util.spec_from_file_location("gate_child", sys.argv[1])
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
proof = gate._keys_proof()
print(json.dumps({"runtime_has_key": os.environ.get("OPENAI_API_KEY") == "sk-test-value",
                  "proof": proof, "dumpable": gate._DUMPABLE, "pid": os.getpid()}),
      flush=True)
sys.stdin.read()
"""


_CLI_STAND_IN = "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/<pid>/environ")
def test_the_key_reaches_the_worker_but_no_launch_environment(tmp_path, monkeypatch) -> None:
    """Red before the fix: Crew put the key in the launch env, so ``/proc`` showed it.

    Green after: the child's ``os.environ`` holds the key, its ``/proc/<pid>/environ``
    does not, and the one-time file is gone once the gate has loaded.
    """
    import json
    import os
    import subprocess

    from kiro_crew.acp.harness import strands as strands_mod
    from kiro_crew.acp.harness import strands_keys

    monkeypatch.setattr(
        strands_mod, "_strands_vault_env", lambda: {"OPENAI_API_KEY": "sk-test-value"}
    )
    launch = strands_mod.StrandsLaunch()
    launch._session = types.SimpleNamespace(_scratch_dir=tmp_path, _shared_scratch=None)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KIROCREW_STRANDS_")}

    async def to_thread(fn, *args):
        return fn(*args)

    asyncio.run(launch.prepare_spawn_env(env, to_thread=to_thread))
    key_file = env.get(strands_keys.KEYS_FILE_ENV, "")

    # The worker's parent stands in for the Strands CLI: launched with the prepared
    # environment, so the parent-launch proof reads that env and not pytest's own
    # (which may carry a developer's real credential).
    child = subprocess.Popen(
        [sys.executable, "-c", _CLI_STAND_IN, sys.executable, "-c", _CHILD, str(GATE)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert child.stdout is not None
        report = json.loads(child.stdout.readline())
        try:
            with open(f"/proc/{report['pid']}/environ", "rb") as handle:
                launch_env = handle.read()
        except PermissionError:
            # Non-dumpable: the kernel refuses a same-account read outright, which is
            # the strongest form of "does not see it".
            launch_env = b""
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=30)

    # What the Node CLI would be launched with: the key is not in it.
    assert "sk-test-value" not in env.values() and "OPENAI_API_KEY" not in env
    assert b"sk-test-value" not in launch_env
    assert b"OPENAI_API_KEY=" not in launch_env
    assert report["runtime_has_key"] is True
    # The worker's own reading of its launch block (taken before it went
    # non-dumpable) and of its parent's: neither carries the key.
    assert report["proof"]["loaded"] == ["OPENAI_API_KEY"]
    assert report["proof"]["launch_env"] == []
    assert report["proof"]["parent_launch_env"] == []
    assert report["proof"]["file_left"] is False
    assert key_file and not os.path.lexists(key_file)
    assert report["dumpable"] == "linux-non-dumpable"


def test_a_key_file_another_account_could_read_refuses_the_start(
    tmp_path, monkeypatch, gated
) -> None:
    """Fail closed: a file readable by others is refused (and still removed)."""
    if sys.platform == "win32":
        pytest.skip("POSIX mode bits")
    path = tmp_path / "keys.json"
    path.write_text('{"OPENAI_API_KEY": "sk"}', encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(gated.gate.ProviderKeysRefused, match="readable by other"):
        gated.gate._load_provider_keys(str(path))
    assert not path.exists()


@pytest.mark.parametrize("own_uid", [None, "other"], ids=["no-getuid", "another-account"])
def test_a_key_file_not_proven_to_be_ours_refuses_the_start(
    tmp_path, monkeypatch, gated, own_uid
) -> None:
    """Fail closed on POSIX: an owner that cannot be read, or is someone else."""
    if sys.platform == "win32":
        pytest.skip("POSIX ownership")
    path = tmp_path / "keys.json"
    path.write_text('{"OPENAI_API_KEY": "sk"}', encoding="utf-8")
    path.chmod(0o600)
    if own_uid is None:
        monkeypatch.delattr(gated.gate.os, "getuid")
    else:
        mine = path.stat().st_uid
        monkeypatch.setattr(gated.gate.os, "getuid", lambda: mine + 1)
    with pytest.raises(gated.gate.ProviderKeysRefused, match="another account"):
        gated.gate._load_provider_keys(str(path))
    assert not path.exists()


def test_keys_read_from_a_file_that_cannot_be_removed_refuse_the_start(
    tmp_path, monkeypatch, gated
) -> None:
    """Fail closed: parsed keys are not handed over while their file is still readable."""
    path = tmp_path / "keys.json"
    path.write_text('{"OPENAI_API_KEY": "sk"}', encoding="utf-8")
    path.chmod(0o600)

    real_unlink = gated.gate.os.unlink

    def unlink(target, *args, **kwargs):
        # gate.os IS the os module, so only the key file is made to fail.
        if str(target) == str(path):
            raise PermissionError("read-only directory")
        return real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(gated.gate.os, "unlink", unlink)
    with pytest.raises(gated.gate.ProviderKeysRefused, match="could not be removed"):
        gated.gate._load_provider_keys(str(path))


@pytest.mark.parametrize("error", [PermissionError, OSError], ids=["eacces", "eio"])
def test_a_removal_that_cannot_be_confirmed_refuses_the_start(
    tmp_path, monkeypatch, gated, error
) -> None:
    """lexists() would read an lstat error as "gone": only FileNotFoundError clears."""
    path = tmp_path / "keys.json"
    path.write_text('{"OPENAI_API_KEY": "sk"}', encoding="utf-8")
    path.chmod(0o600)
    real_unlink = gated.gate.os.unlink
    real_lstat = gated.gate.os.lstat
    state = {"unlinked": False}

    def unlink(target, *args, **kwargs):
        if str(target) == str(path):
            state["unlinked"] = True
            raise PermissionError("read-only directory")
        return real_unlink(target, *args, **kwargs)

    def lstat(target, *args, **kwargs):
        if str(target) == str(path) and state["unlinked"]:
            raise error("cannot inspect")
        return real_lstat(target, *args, **kwargs)

    monkeypatch.setattr(gated.gate.os, "unlink", unlink)
    monkeypatch.setattr(gated.gate.os, "lstat", lstat)
    with pytest.raises(gated.gate.ProviderKeysRefused, match="cannot be confirmed"):
        gated.gate._load_provider_keys(str(path))


def test_an_unlink_that_reports_success_but_leaves_the_file_refuses_the_start(
    tmp_path, monkeypatch, gated
) -> None:
    path = tmp_path / "keys.json"
    path.write_text('{"OPENAI_API_KEY": "sk"}', encoding="utf-8")
    path.chmod(0o600)
    real_unlink = gated.gate.os.unlink

    def unlink(target, *args, **kwargs):
        if str(target) == str(path):
            return None
        return real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(gated.gate.os, "unlink", unlink)
    with pytest.raises(gated.gate.ProviderKeysRefused, match="could not be removed"):
        gated.gate._load_provider_keys(str(path))


def test_the_proof_reports_a_key_file_it_cannot_inspect_as_left(
    tmp_path, monkeypatch, gated
) -> None:
    """The marker's file_left fails closed: an lstat error is not "gone"."""
    path = str(tmp_path / "keys.json")
    real_lstat = gated.gate.os.lstat

    def lstat(target, *args, **kwargs):
        if str(target) == path:
            raise PermissionError("cannot inspect")
        return real_lstat(target, *args, **kwargs)

    monkeypatch.setattr(gated.gate, "_KEYS_FILE", path)
    assert gated.gate._keys_proof()["file_left"] is False
    monkeypatch.setattr(gated.gate.os, "lstat", lstat)
    assert gated.gate._keys_proof()["file_left"] is True


def test_a_key_file_already_gone_after_reading_is_not_a_refusal(
    tmp_path, monkeypatch, gated
) -> None:
    path = tmp_path / "keys.json"
    path.write_text('{"OPENAI_API_KEY": "sk"}', encoding="utf-8")
    path.chmod(0o600)
    real_unlink = gated.gate.os.unlink

    def unlink(target, *args, **kwargs):
        real_unlink(target, *args, **kwargs)
        if str(target) == str(path):
            raise FileNotFoundError(target)

    monkeypatch.setattr(gated.gate.os, "unlink", unlink)
    assert gated.gate._load_provider_keys(str(path)) == {"OPENAI_API_KEY": "sk"}


def test_a_missing_key_file_refuses_the_start(tmp_path, gated) -> None:
    with pytest.raises(gated.gate.ProviderKeysRefused, match="unreadable"):
        gated.gate._load_provider_keys(str(tmp_path / "absent.json"))


def test_a_linked_key_file_refuses_the_start(tmp_path, gated) -> None:
    if sys.platform == "win32":
        pytest.skip("symlinks")
    target = tmp_path / "real.json"
    target.write_text('{"OPENAI_API_KEY": "sk"}', encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "keys.json"
    link.symlink_to(target)
    with pytest.raises(gated.gate.ProviderKeysRefused, match="not a regular file"):
        gated.gate._load_provider_keys(str(link))
