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
import importlib.machinery
import importlib.util
import os
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
    slots = (
        "shell",
        "read",
        "write",
        "edit",
        "web_fetch",
        "web_search",
        "programmatic_tool_caller",
        "subagent",
    )

    def builtin_tools(*_a: Any, **_k: Any) -> dict:
        return {slot: types.SimpleNamespace(tool_name=slot) for slot in slots}

    module("strands_harness.agent", _builtin_tools=builtin_tools)
    module("strands_harness", create_harness=lambda *a, **k: Agent(), BUILTIN_TOOL_NAMES=slots)
    return Agent


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch) -> type:
    for name in (
        "KIROCREW_STRANDS_PROJECT",
        "KIROCREW_STRANDS_PROJECT_SHA256",
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


def _event(name: str, tool: Any) -> Any:
    return types.SimpleNamespace(tool_use={"name": name}, selected_tool=tool)


def _harness_builtins(gate: Any) -> dict:
    """The built-ins as the harness's own factory builds them, through the gate's hook."""
    return sys.modules["strands_harness.agent"]._builtin_tools({})


@pytest.mark.parametrize("name", ["shell", "read", "write", "edit", "web_fetch"])
def test_a_harness_builtin_is_owned_by_the_harness(gated, name) -> None:
    tool = _harness_builtins(gated.gate)[name]
    assert gated.gate._tool_identity(_event(name, tool)) == (name, "builtin")


@pytest.mark.parametrize("name", ["read", "shell", "web_fetch"])
def test_a_project_tool_wearing_a_builtin_name_is_custom(gated, name) -> None:
    """A ``read`` built-in is auto-approved by the read-only tier, so a project tool
    named ``read`` (one that marks mail as read, say) is ``custom``, not the built-in."""
    _harness_builtins(gated.gate)
    own = types.SimpleNamespace(tool_name=name)
    assert gated.gate._tool_identity(_event(name, own)) == (name, "custom")


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
module("strands_harness.agent", _builtin_tools=lambda *a, **k: {})
module("strands_harness", create_harness=lambda *a, **k: Agent(), BUILTIN_TOOL_NAMES=())

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


def test_a_tool_that_is_not_an_object_the_harness_built_is_custom(gated) -> None:
    assert gated.gate._tool_identity(_event("read", object())) == ("read", "custom")
    assert gated.gate._tool_identity(_event("read", None)) == ("read", "custom")


def test_a_factory_result_of_another_shape_records_no_builtin(gated) -> None:
    """A renamed or missing slot breaks the one-per-slot shape: nothing is recorded."""
    factory = sys.modules["strands_harness.agent"]
    renamed = {"shell": types.SimpleNamespace(tool_name="read")}
    original = gated.gate._original_builtin_tools
    try:
        gated.gate._original_builtin_tools = lambda *a, **k: renamed
        tools = factory._builtin_tools({})
    finally:
        gated.gate._original_builtin_tools = original
    assert gated.gate._tool_identity(_event("read", tools["shell"])) == ("read", "custom")


def _sdk_shell(name: str = "shell") -> Any:
    """A tool built by the Strands SDK's ``make_shell``, outside the harness factory."""

    def shell_tool() -> None:
        pass

    shell_tool.__module__ = "strands.vended_tools.shell.shell"
    return types.SimpleNamespace(tool_name=name, _tool_func=shell_tool)


def test_the_sdk_shell_outside_the_harness_factory_is_still_the_builtin_shell(gated) -> None:
    """``Agent(tools=[shell])`` must reach Crew's command tiers as a shell, not ``custom``."""
    assert gated.gate._tool_identity(_event("shell", _sdk_shell())) == ("shell", "builtin")


@pytest.mark.parametrize("name", ["bash", "read", "web_fetch", "web_search"])
def test_a_renamed_sdk_shell_never_wears_a_builtin_owner(gated, name) -> None:
    """``make_shell(name="read")`` must not be judged as the file reader."""
    assert gated.gate._tool_identity(_event(name, _sdk_shell(name))) == (name, "renamed-sdk-shell")


# ── The operator's project is the confirmed version, checked again right before import ──


def _user_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.py").write_text(
        "import helper\nfrom strands import Agent\nagent = Agent()\nMARK = helper.MARK\n",
        encoding="utf-8",
    )
    (root / "helper.py").write_text("MARK = 'confirmed'\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "notes.txt").write_bytes(b"\x00\xffbytes")
    return root / "agent.py"


@pytest.fixture
def isolated_import(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The project load changes interpreter-wide import state; give it back afterwards."""
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    machinery = importlib.machinery
    for cls, name in (
        (machinery.SourceFileLoader, "get_code"),
        (machinery.SourcelessFileLoader, "get_code"),
        (machinery.ExtensionFileLoader, "create_module"),
    ):
        monkeypatch.setattr(cls, name, getattr(cls, name))
    import runpy
    import zipimport

    monkeypatch.setattr(runpy, "_get_code_from_file", runpy._get_code_from_file)
    monkeypatch.setattr(zipimport.zipimporter, "__init__", zipimport.zipimporter.__init__)
    for name in ("helper", "later", "fresh"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "kirocrew_strands_user_project", raising=False)


def test_the_gate_fingerprint_equals_the_gateway_one(gated, tmp_path) -> None:
    from kiro_crew.acp.harness import strands_project_pin

    entry = _user_project(tmp_path / "proj")
    (entry.parent / "__pycache__").mkdir()
    (entry.parent / "__pycache__" / "helper.cpython-312.pyc").write_bytes(b"stale")
    expected, count = strands_project_pin.project_fingerprint(entry)
    assert gated.gate._project_fingerprint(entry)[0] == expected
    assert len(gated.gate._project_fingerprint(entry)[1]) == count
    assert gated.gate._PIN_MAX_FILES == strands_project_pin.MAX_FILES
    assert gated.gate._PIN_MAX_BYTES == strands_project_pin.MAX_BYTES


def test_a_confirmed_project_is_imported(gated, tmp_path, monkeypatch, isolated_import) -> None:
    entry = _user_project(tmp_path / "proj")
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    module, _captured = gated.gate._load_user_project(str(entry.parent))
    assert module.MARK == "confirmed"


def test_planted_bytecode_never_runs_instead_of_the_confirmed_source(
    gated, tmp_path, monkeypatch, isolated_import
) -> None:
    """An unchecked-hash .pyc is trusted by Python without its source; the project's
    imports never read bytecode at all."""
    import py_compile

    entry = _user_project(tmp_path / "proj")
    helper = entry.parent / "helper.py"
    evil = tmp_path / "evil.py"
    evil.write_text("MARK = 'planted'\n", encoding="utf-8")
    # Where Python looks: the in-tree cache, and a moved cache the agent could write.
    for prefix in (None, str(tmp_path / "moved-cache")):
        monkeypatch.setattr(sys, "pycache_prefix", prefix)
        py_compile.compile(
            str(evil),
            cfile=importlib.util.cache_from_source(str(helper)),
            doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    module, _captured = gated.gate._load_user_project(str(entry))
    assert module.MARK == "confirmed"


def test_a_project_file_changed_mid_session_is_not_imported_later(
    gated, tmp_path, monkeypatch, isolated_import
) -> None:
    entry = _user_project(tmp_path / "proj")
    (entry.parent / "later.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    gated.gate._load_user_project(str(entry))
    # After the load, the agent edits a module the project imports lazily, and adds one.
    (entry.parent / "later.py").write_text("VALUE = 'edited'\n", encoding="utf-8")
    (entry.parent / "fresh.py").write_text("VALUE = 'new'\n", encoding="utf-8")
    importlib.invalidate_caches()  # the folder listing Python caches, as a later turn sees it
    with pytest.raises(ImportError, match="not part of the Strands project version"):
        importlib.import_module("later")
    with pytest.raises(ImportError, match="not part of the Strands project version"):
        importlib.import_module("fresh")


def test_a_file_swapped_after_the_launch_check_is_not_imported(
    gated, tmp_path, monkeypatch, isolated_import
) -> None:
    entry = _user_project(tmp_path / "proj")
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    (entry.parent / "helper.py").write_text("raise SystemExit('ran')\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="not the version confirmed"):
        gated.gate._load_user_project(str(entry))
    assert "helper" not in sys.modules


def test_a_project_with_no_pin_is_not_imported(
    gated, tmp_path, monkeypatch, isolated_import
) -> None:
    entry = _user_project(tmp_path / "proj")
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", "")
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    with pytest.raises(PermissionError, match="not the version confirmed"):
        gated.gate._load_user_project(str(entry))


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
def test_a_linked_spelling_of_the_folder_is_still_checked(
    gated, tmp_path, monkeypatch, isolated_import
) -> None:
    entry = _user_project(tmp_path / "proj")
    (entry.parent / "later.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    gated.gate._load_user_project(str(entry))
    alias = tmp_path / "alias"
    alias.symlink_to(entry.parent, target_is_directory=True)
    sys.path.insert(0, str(alias))
    (entry.parent / "later.py").write_text("VALUE = 'edited'\n", encoding="utf-8")
    with pytest.raises(ImportError, match="not part of the Strands project version"):
        importlib.import_module("later")


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
def test_a_file_linked_out_of_the_folder_mid_session_is_not_imported(
    gated, tmp_path, monkeypatch, isolated_import
) -> None:
    entry = _user_project(tmp_path / "proj")
    (entry.parent / "later.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    gated.gate._load_user_project(str(entry))
    outside = tmp_path / "elsewhere.py"
    outside.write_text("VALUE = 'unconfirmed'\n", encoding="utf-8")
    (entry.parent / "later.py").unlink()
    (entry.parent / "later.py").symlink_to(outside)
    importlib.invalidate_caches()
    with pytest.raises(ImportError, match="not part of the Strands project version"):
        importlib.import_module("later")


def test_a_confirmed_package_still_reads_its_own_resources(
    gated, tmp_path, monkeypatch, isolated_import
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "agent.py").write_text(
        "import importlib.resources, pkgutil\n"
        "from strands import Agent\n"
        "agent = Agent()\n"
        "TEXT = importlib.resources.files('kc_pkg').joinpath('prompt.txt').read_text()\n"
        "DATA = pkgutil.get_data('kc_pkg', 'prompt.txt')\n",
        encoding="utf-8",
    )
    (root / "kc_pkg").mkdir()
    (root / "kc_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "kc_pkg" / "prompt.txt").write_text("hi", encoding="utf-8")
    monkeypatch.delitem(sys.modules, "kc_pkg", raising=False)
    entry = root / "agent.py"
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    module, _captured = gated.gate._load_user_project(str(entry))
    assert module.TEXT == "hi"
    assert module.DATA == b"hi"


def _plant_unchecked_pyc(source: Path, evil_text: str, tmp_path: Path) -> None:
    import py_compile

    evil = tmp_path / "evil_source.py"
    evil.write_text(evil_text, encoding="utf-8")
    py_compile.compile(
        str(evil),
        cfile=importlib.util.cache_from_source(str(source)),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )


def _load_by_path(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_file_the_project_loads_by_path_never_runs_planted_bytecode(
    gated, tmp_path, monkeypatch, isolated_import
) -> None:
    entry = _user_project(tmp_path / "proj")
    helper = entry.parent / "helper.py"
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    gated.gate._load_user_project(str(entry))
    _plant_unchecked_pyc(helper, "MARK = 'planted'\n", tmp_path)
    assert _load_by_path("by_path_helper", helper).MARK == "confirmed"
    # An edit made mid-session is refused on this path too.
    helper.write_text("MARK = 'edited'\n", encoding="utf-8")
    with pytest.raises(ImportError, match="not part of the Strands project version"):
        _load_by_path("by_path_helper_2", helper)
    # Outside the project folder the stock loader is unchanged.
    other = tmp_path / "outside_mod.py"
    other.write_text("MARK = 'outside'\n", encoding="utf-8")
    assert _load_by_path("outside_mod", other).MARK == "outside"


def test_bytecode_in_the_project_folder_is_never_loaded_by_path(
    gated, tmp_path, monkeypatch, isolated_import
) -> None:
    entry = _user_project(tmp_path / "proj")
    helper = entry.parent / "helper.py"
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    gated.gate._load_user_project(str(entry))
    import py_compile

    # Inside the project folder, wherever Python's own cache setting points.
    pyc = str(entry.parent / "__pycache__" / "helper.cpython-312.pyc")
    py_compile.compile(str(helper), cfile=pyc, doraise=True)
    loader = importlib.machinery.SourcelessFileLoader("planted_pyc", pyc)
    spec = importlib.util.spec_from_loader("planted_pyc", loader)
    assert spec is not None
    with pytest.raises(ImportError, match="not Python source"):
        loader.exec_module(importlib.util.module_from_spec(spec))


def test_a_file_run_by_path_is_checked_too(gated, tmp_path, monkeypatch, isolated_import) -> None:
    import runpy

    entry = _user_project(tmp_path / "proj")
    helper = entry.parent / "helper.py"
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    gated.gate._load_user_project(str(entry))
    assert runpy.run_path(str(helper))["MARK"] == "confirmed"
    # A bytes path reaches the same check (older runpy passes it through unchanged).
    run_file = runpy._get_code_from_file
    raw = os.fsencode(str(helper))
    try:
        code = run_file(raw)
    except TypeError:  # the older (run_name, fname) shape
        code = run_file("__main__", raw)
    code = code[0] if isinstance(code, tuple) else code
    # Compiled from the confirmed source: the constant comes from the checked bytes.
    assert code.co_filename == str(helper)
    assert "confirmed" in code.co_consts
    helper.write_text("MARK = 'edited'\n", encoding="utf-8")
    with pytest.raises(ImportError, match="not part of the Strands project version"):
        runpy.run_path(str(helper))
    other = tmp_path / "outside_run.py"
    other.write_text("MARK = 'outside'\n", encoding="utf-8")
    assert runpy.run_path(str(other))["MARK"] == "outside"


def test_a_zip_archive_in_the_project_folder_is_never_imported_from(
    gated, tmp_path, monkeypatch, isolated_import
) -> None:
    import zipfile
    import zipimport

    entry = _user_project(tmp_path / "proj")
    archive = entry.parent / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("zipped_mod.py", "MARK = 'zipped'\n")
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    gated.gate._load_user_project(str(entry))
    with pytest.raises(zipimport.ZipImportError, match="zip archive in the Strands project"):
        zipimport.zipimporter(str(archive))


def test_another_spelling_of_the_folder_is_still_the_folder(
    gated, tmp_path, monkeypatch, isolated_import
) -> None:
    """A case-insensitive volume (macOS by default) or a short name gives the folder a
    spelling that neither ``normcase`` nor ``realpath`` maps back. The folder is then
    known by device and inode. Simulated here with a second folder that answers to
    the project folder's identity and a hard link to the confirmed file."""
    entry = _user_project(tmp_path / "Project")
    helper = entry.parent / "helper.py"
    monkeypatch.setattr(gated.gate, "PROJECT_SHA256", gated.gate._project_fingerprint(entry)[0])
    monkeypatch.setattr(gated.gate, "STATE_DIR", str(tmp_path / "state"))
    gated.gate._load_user_project(str(entry))
    alias = tmp_path / "project_alias"
    alias.mkdir()
    os.link(helper, alias / "helper.py")
    real_identity = gated.gate._identity
    root_id = real_identity(str(entry.parent), follow=True)

    def identity(path: str, *, follow: bool) -> Any:
        if os.path.abspath(path) == str(alias):
            return root_id
        return real_identity(path, follow=follow)

    monkeypatch.setattr(gated.gate, "_identity", identity)
    assert _load_by_path("alias_helper", alias / "helper.py").MARK == "confirmed"
    (alias / "helper.py").write_text("MARK = 'edited'\n", encoding="utf-8")
    with pytest.raises(ImportError, match="not part of the Strands project version"):
        _load_by_path("alias_helper_2", alias / "helper.py")
