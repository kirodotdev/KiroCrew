"""The Claude adapter is a Node.js program: with no ``node`` to run it, say so.

claude-agent-acp ships as an npm bin whose first line is ``#!/usr/bin/env node``,
and the adapter ladder hands that bin back alone, so the shebang picks the Node at
exec. On a host with none the child dies before it answers ``initialize`` and the
chat can only report a lost connection. Both drivers' spawns check first
(``_refuse_claude_without_node``) and refuse with a message that names Node.js.

Driven against real files: a ``#!/usr/bin/env node`` entry script and fake ``node``
executables in temporary directories, with the PATH augmentation pinned to the PATH
the test sets and no contributed MCP dir unless a test adds one, so no Node on the
host can answer for the one under test.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import env as env_mod
from kiro_crew import platform_compat
from kiro_crew.acp.harness import claude as claude_mod
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.harness.pi_node import shebang_node_missing
from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.acp.transport_errors import AcpError

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX shebang launch; a Windows .cmd shim picks its own Node"
)


def _script(directory: Path, shebang: str = "#!/usr/bin/env node") -> str:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "claude-agent-acp"
    script.write_text(f"{shebang}\nconsole.log('adapter')\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _fake_node(directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    node = directory / "node"
    node.write_text("#!/bin/sh\necho v24.0.0\n", encoding="utf-8")
    node.chmod(0o755)
    return str(node)


@pytest.fixture
def child_path(monkeypatch, tmp_path):
    """Set the gateway's PATH; the augmentation hands it back and adds no MCP dir."""
    monkeypatch.setattr(env_mod, "augmented_path", lambda base, **_kw: base)
    monkeypatch.setattr(env_mod, "_extra_mcp_path_dirs", lambda: [])

    def _set(*dirs: Path) -> str:
        value = os.pathsep.join(str(d) for d in dirs)
        monkeypatch.setenv("PATH", value)
        return value

    return _set


def test_an_env_node_script_with_no_node_on_the_path_is_missing_its_node(tmp_path) -> None:
    script = _script(tmp_path / "bin")
    assert shebang_node_missing(script, str(tmp_path / "empty")) is True


def test_a_node_on_the_path_satisfies_the_shebang(tmp_path) -> None:
    script = _script(tmp_path / "bin")
    _fake_node(tmp_path / "node-bin")
    assert shebang_node_missing(script, str(tmp_path / "node-bin")) is False


@pytest.mark.parametrize("entry", ["bin", "", "."], ids=["relative", "empty", "dot"])
def test_a_relative_path_entry_is_left_to_the_launch(tmp_path, entry) -> None:
    """``env`` resolves such an entry against the child's working directory, unknown here."""
    script = _script(tmp_path / "bin")
    search_path = os.pathsep.join([str(tmp_path / "empty"), entry])
    assert shebang_node_missing(script, search_path) is False


def test_an_absolute_node_shebang_is_checked_by_its_own_path(tmp_path) -> None:
    node = _fake_node(tmp_path / "node-bin")
    assert shebang_node_missing(_script(tmp_path / "a", f"#!{node}"), "") is False
    gone = tmp_path / "gone" / "node"
    assert shebang_node_missing(_script(tmp_path / "b", f"#!{gone}"), "") is True


@pytest.mark.parametrize("shebang", ["#!/bin/sh", "#!/usr/bin/env node --flag", "\x7fELF"])
def test_a_program_that_is_not_a_plain_node_script_is_left_alone(tmp_path, shebang) -> None:
    assert shebang_node_missing(_script(tmp_path / "bin", shebang), "") is False


def test_an_unreadable_entry_is_left_to_the_launch(tmp_path) -> None:
    assert shebang_node_missing(str(tmp_path / "absent"), "") is False


def test_windows_is_left_to_its_shim(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert shebang_node_missing(_script(tmp_path / "bin"), "") is False


def test_the_launch_refuses_naming_node_when_none_can_run_the_adapter(tmp_path, child_path) -> None:
    script = _script(tmp_path / "bin")
    child_path(tmp_path / "bin", tmp_path / "usr-bin")

    with pytest.raises(AcpError) as raised:
        asyncio.run(claude_mod._refuse_claude_without_node([script], {}))

    message = str(raised.value)
    assert message.startswith("Claude Code cannot start: it needs Node.js")
    # The one link in the message is where to get Node.js.
    assert re.findall(r"https://[^\s,]+", message) == ["https://nodejs.org"]
    assert script in message
    # The PATH the child would get, read at launch.
    assert str(tmp_path / "usr-bin") in message
    assert raised.value.transient is False
    assert raised.value.user_worded is True


def test_a_node_on_the_path_launches_the_adapter_unchanged(tmp_path, child_path) -> None:
    script = _script(tmp_path / "bin")
    child_path(tmp_path / "bin", Path(_fake_node(tmp_path / "node-bin")).parent)

    asyncio.run(claude_mod._refuse_claude_without_node([script], {}))


def test_a_node_on_the_spawns_own_path_is_found(tmp_path, child_path) -> None:
    """``extra_env`` lands over the gateway's environment, its ``PATH`` included."""
    script = _script(tmp_path / "bin")
    child_path(tmp_path / "empty")
    node_dir = Path(_fake_node(tmp_path / "node-bin")).parent

    asyncio.run(claude_mod._refuse_claude_without_node([script], {"PATH": str(node_dir)}))


def test_a_node_only_in_a_contributed_mcp_dir_is_found(tmp_path, monkeypatch, child_path) -> None:
    """The launch appends the contributed MCP dirs to the child's PATH, so they count."""
    script = _script(tmp_path / "bin")
    child_path(tmp_path / "empty")
    node_dir = Path(_fake_node(tmp_path / "contributed")).parent
    monkeypatch.setattr(env_mod, "_extra_mcp_path_dirs", lambda: [str(node_dir)])

    asyncio.run(claude_mod._refuse_claude_without_node([script], {}))


def test_an_adapter_launched_under_an_explicit_node_is_not_checked(tmp_path, child_path) -> None:
    """``[node, script]``: the ladder already chose the interpreter by path."""
    script = _script(tmp_path / "bin")
    argv = [str(tmp_path / "mise" / "bin" / "node"), script]
    child_path(tmp_path / "empty")

    asyncio.run(claude_mod._refuse_claude_without_node(argv, {}))


def _ctx(tmp_path: Path, **kwargs) -> SpawnContext:
    return SpawnContext(
        agent="",
        work_dir=str(tmp_path),
        model=None,
        environ=dict(os.environ),
        home=tmp_path,
        **kwargs,
    )


def test_the_per_session_launch_refuses_before_it_seeds_the_session(
    tmp_path, monkeypatch, child_path
) -> None:
    script = _script(tmp_path / "bin")
    monkeypatch.setattr(claude_mod, "_claude_acp_argv_cache", ([script], ""))
    child_path(tmp_path / "empty")

    async def seeded(*_a, **_k) -> None:
        raise AssertionError("a refused launch seeded its session")

    monkeypatch.setattr(claude_mod, "prepare_claude_session", seeded)
    session = SimpleNamespace(_model="auto", _extra_env={})

    with pytest.raises(AcpError, match="it needs Node.js"):
        asyncio.run(claude_mod.ClaudeLaunch().resolve_spawn(_ctx(tmp_path, session=session)))


def test_the_shared_runtime_refuses_with_the_same_words(tmp_path, monkeypatch, child_path) -> None:
    script = _script(tmp_path / "bin")
    monkeypatch.setattr(claude_mod, "_claude_acp_argv_cache", ([script], ""))
    child_path(tmp_path / "empty")

    with pytest.raises(AcpRuntimeError) as raised:
        asyncio.run(claude_mod.ClaudeHarness().resolve_spawn(_ctx(tmp_path, extra_env={})))

    assert str(raised.value).startswith("Claude Code cannot start: it needs Node.js")
