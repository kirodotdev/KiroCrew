"""A stdio MCP server's ``${HOME}``/``${USER}`` args reach the child as real values.

kiro-cli expands ``${VAR}`` in a declared ``env`` map but never in ``args``, so a
launch path written as ``${HOME}/.local/share/server/index.js`` reaches the child
literally unless Kiro Crew resolves it: the probe fails, the server is
quarantined, and the session cannot start it either. These tests pin both launch
inputs -- the emitted agent spec and the probe's argv -- and that only home and
login-name variables are ever substituted, so no other variable's value lands on
argv.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from kiro_crew import mcp_discovery
from kiro_crew.agent_materialization import mcp_sources
from kiro_crew.env import expand_spec_args
from kiro_crew.mcp_discovery import McpServerInfo, probe_server

HOME = "/home/alice"


@pytest.fixture
def home_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", HOME)
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("SOME_SECRET", "s3cr3t")
    monkeypatch.delenv("LOGNAME", raising=False)


@pytest.mark.usefixtures("home_env")
def test_home_and_user_references_are_substituted() -> None:
    args = ["${HOME}/srv/index.js", "--user=${USER}", "${env:HOME}/x", "plain", 3]
    assert expand_spec_args(args) == [
        f"{HOME}/srv/index.js",
        "--user=alice",
        f"{HOME}/x",
        "plain",
        3,
    ]


@pytest.mark.usefixtures("home_env")
@pytest.mark.parametrize(
    "arg",
    [
        "--token=${SOME_SECRET}",  # not a home/login variable: never dereferenced
        "${LOGNAME}/x",  # allowed name, but unset: stays literal, not "/x"
        "$HOME/x",  # not kiro-cli's placeholder grammar
        "$(id -u)",  # text only: nothing is evaluated by a shell
    ],
)
def test_anything_else_is_left_exactly_as_written(arg: str) -> None:
    assert expand_spec_args([arg]) == [arg]


@pytest.mark.usefixtures("home_env")
def test_substitution_is_plain_text_around_shell_syntax() -> None:
    assert expand_spec_args(["$(echo ${HOME})"]) == [f"$(echo {HOME})"]


def test_non_list_args_and_the_source_list_are_untouched() -> None:
    source = ["${HOME}/x"]
    expand_spec_args(source)
    assert source == ["${HOME}/x"]
    assert expand_spec_args("--flag") == "--flag"
    assert expand_spec_args(None) is None


@pytest.mark.usefixtures("home_env")
def test_the_emitted_agent_spec_carries_the_expanded_launch_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = {"command": "node", "args": ["${HOME}/.local/share/bb/build/index.js"]}
    config: dict[str, Any] = {"mcpServers": {"bitbucket": dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={}, kiro_global={"bitbucket": spec}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", frozenset)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)

    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", ""))

    assert config["mcpServers"]["bitbucket"]["args"] == [f"{HOME}/.local/share/bb/build/index.js"]
    # The scope's own declaration keeps its template for the next rebuild.
    assert spec["args"] == ["${HOME}/.local/share/bb/build/index.js"]


class _ProbeStop(Exception):
    """Raised by the fake sandbox wrap once it has seen the argv."""


@pytest.mark.asyncio
@pytest.mark.usefixtures("home_env")
async def test_the_probe_spawns_the_expanded_launch_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str]] = {}

    async def _wrap(argv: list[str], **_kwargs: Any) -> Any:
        seen["argv"] = list(argv)
        raise _ProbeStop

    monkeypatch.setattr(mcp_discovery, "sandboxed_spawn_argv_async", _wrap)
    monkeypatch.setattr(mcp_discovery.shutil, "which", lambda *_a, **_k: "/opt/bin/node")

    server = McpServerInfo(
        name="bitbucket", command="node", args=["${HOME}/srv/index.js", "${SOME_SECRET}"]
    )
    await probe_server(server)

    assert seen["argv"] == ["/opt/bin/node", f"{HOME}/srv/index.js", "${SOME_SECRET}"]
    # The declaration the dashboard shows and the probe identity hashes is unchanged.
    assert server.args == ["${HOME}/srv/index.js", "${SOME_SECRET}"]


@pytest.mark.usefixtures("home_env")
def test_the_emitted_agent_spec_expands_home_in_command_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = {
        "command": "${HOME}/.local/bin/terraform-mcp-server",
        "env": {"NODE_EXTRA_CA_CERTS": "${HOME}/ca.pem", "EXTRA": "${SOME_SECRET}"},
    }
    config: dict[str, Any] = {"mcpServers": {"tf": dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={}, kiro_global={"tf": spec}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", frozenset)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    asked: list[tuple[str, Any]] = []

    def _resolve(cmd: str, env: Any) -> tuple[str, str]:
        asked.append((cmd, dict(env or {})))
        return cmd, ""

    mcp_sources.resolve_mcp_servers(config, sources, _resolve)

    # The resolver sees what the server will be given: expanded command and env.
    assert [cmd for cmd, _ in asked] == [f"{HOME}/.local/bin/terraform-mcp-server"]
    assert asked[0][1]["NODE_EXTRA_CA_CERTS"] == f"{HOME}/ca.pem"
    emitted = config["mcpServers"]["tf"]
    assert emitted["command"] == f"{HOME}/.local/bin/terraform-mcp-server"
    assert emitted["env"]["NODE_EXTRA_CA_CERTS"] == f"{HOME}/ca.pem"
    # Not a home/login name: left for the harness, never dereferenced here.
    assert emitted["env"]["EXTRA"] == "${SOME_SECRET}"
    # The scope's own declaration keeps its template for the next rebuild.
    assert spec["command"] == "${HOME}/.local/bin/terraform-mcp-server"
    assert spec["env"]["NODE_EXTRA_CA_CERTS"] == "${HOME}/ca.pem"


@pytest.mark.asyncio
@pytest.mark.usefixtures("home_env")
async def test_the_probe_expands_home_in_command_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _wrap(argv: list[str], **kwargs: Any) -> Any:
        seen["argv"] = list(argv)
        seen["env"] = dict(kwargs.get("env") or {})
        raise _ProbeStop

    def _which(cmd: str, **_k: Any) -> str:
        seen["which"] = cmd
        return cmd

    monkeypatch.setattr(mcp_discovery, "sandboxed_spawn_argv_async", _wrap)
    monkeypatch.setattr(mcp_discovery.shutil, "which", _which)

    server = McpServerInfo(
        name="tf",
        command="${HOME}/.local/bin/terraform-mcp-server",
        env={"NODE_EXTRA_CA_CERTS": "${HOME}/ca.pem", "EXTRA": "${SOME_SECRET}"},
    )
    await probe_server(server)

    assert seen["which"] == f"{HOME}/.local/bin/terraform-mcp-server"
    assert seen["argv"] == [f"{HOME}/.local/bin/terraform-mcp-server"]
    assert seen["env"]["NODE_EXTRA_CA_CERTS"] == f"{HOME}/ca.pem"
    assert seen["env"]["EXTRA"] == "${SOME_SECRET}"
    # The declaration the dashboard shows and the probe identity hashes is unchanged.
    assert server.command == "${HOME}/.local/bin/terraform-mcp-server"
    assert server.env["NODE_EXTRA_CA_CERTS"] == "${HOME}/ca.pem"


@pytest.mark.asyncio
@pytest.mark.usefixtures("home_env")
async def test_the_probe_classifies_and_searches_the_expanded_temp_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _classify(declared: Any, **_k: Any) -> Any:
        seen["temp"] = dict(declared)
        return {}, {}, ""

    async def _wrap(argv: list[str], **_kwargs: Any) -> Any:
        raise _ProbeStop

    def _which(cmd: str, *, path: str = "", **_k: Any) -> str:
        seen["path"] = path
        return f"/opt/bin/{cmd}"

    monkeypatch.setattr(mcp_discovery, "classify_declared_temp_env", _classify)
    monkeypatch.setattr(mcp_discovery, "sandboxed_spawn_argv_async", _wrap)
    monkeypatch.setattr(mcp_discovery.shutil, "which", _which)

    server = McpServerInfo(
        name="tf",
        command="node",
        env={"TMPDIR": "${HOME}/tmp", "PATH": "${HOME}/bin"},
    )
    await probe_server(server)

    assert seen["temp"]["TMPDIR"] == f"{HOME}/tmp"
    assert "${HOME}" not in seen["path"]
    if os.name != "nt":  # "/home/alice/bin" is not absolute on Windows, so it is dropped
        assert seen["path"].split(os.pathsep)[0] == f"{HOME}/bin"
    assert server.env == {"TMPDIR": "${HOME}/tmp", "PATH": "${HOME}/bin"}


@pytest.mark.usefixtures("home_env")
def test_an_expanded_entry_agrees_with_its_home_placeholder_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The merge (``_merge_command_unit``) and the dashboard's sync check compare
    # the source as written with the entry this rebuild emitted. Both must read
    # ``${HOME}/...`` as the expanded path, or an args/env edit is never adopted
    # and the server is offered for sync on every poll.
    monkeypatch.setattr(mcp_discovery, "_pinned_command_missing", lambda _cmd: False)
    assert not mcp_discovery._commands_diverged("${HOME}/bin/srv", f"{HOME}/bin/srv")
    assert mcp_discovery._commands_diverged("${HOME}/bin/srv", "/opt/other/tool")
    assert mcp_discovery._envs_agree({"CA": f"{HOME}/ca.pem"}, {"CA": "${HOME}/ca.pem"})
    assert not mcp_discovery._envs_agree({"CA": "/elsewhere"}, {"CA": "${HOME}/ca.pem"})


@pytest.mark.usefixtures("home_env")
def test_a_source_env_edit_is_adopted_onto_an_expanded_entry() -> None:
    existing = {"command": f"{HOME}/bin/srv", "env": {"CA": f"{HOME}/old.pem"}}
    source = {"command": "${HOME}/bin/srv", "env": {"CA": "${HOME}/new.pem"}}
    mcp_sources._merge_command_unit(existing, source)
    assert existing["env"] == {"CA": "${HOME}/new.pem"}
