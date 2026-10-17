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
