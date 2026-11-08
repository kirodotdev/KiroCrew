"""Which ``mcp.json`` servers an agent spec's opt-in mounts beside its own.

kiro-cli, and KAS through the flag ``acp.kas_agents`` forwards, mount the servers of
the user's ``mcp.json`` files into an agent whose spec states ``includeMcpJson``.
Crew's own readers of "the servers this agent has" ask
:mod:`kiro_crew.agent_sdk.mcp_json_reach` for those names, so these tests pin what it
answers: the per-host reading of the flag, which files it reads, and that nothing in
them can make it raise.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from handlers_mcp_coverage_helpers import agents_dir_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _payload, _request

from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.testing.links import make_dir_link

# The reader is imported per call, so a tree without it fails each test that asks
# it something rather than stopping the whole run at collection.


def includes_mcp_json(spec: object, backend: str) -> bool:
    from kiro_crew.agent_sdk import mcp_json_reach

    return mcp_json_reach.includes_mcp_json(spec, backend)


def mcp_json_server_names(
    spec: object, backend: str, work_dir: Path | None = None
) -> tuple[str, ...]:
    from kiro_crew.agent_sdk import mcp_json_reach

    return mcp_json_reach.mcp_json_server_names(spec, backend, work_dir)


def _write(path: Path, servers: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


@pytest.fixture
def kiro_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A kiro home of the test's own, so the machine's ``mcp.json`` is never read."""
    import kiro_crew.agent as agent_mod

    home = tmp_path / "kiro-home"
    # The user-level file the session's settings reader reads.
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", home / "settings" / "mcp.json")
    return home


@pytest.mark.parametrize(
    ("spec", "on_kiro", "on_kas"),
    [
        ({"includeMcpJson": True}, True, True),
        ({"includeMcpJson": False}, False, False),
        # kiro-cli reads the older name too, and KAS is sent it as ``includeMcpJson``.
        ({"useLegacyMcpJson": True}, True, True),
        ({"useLegacyMcpJson": False}, False, False),
        # A stated ``includeMcpJson`` decides whatever the older name says.
        ({"includeMcpJson": False, "useLegacyMcpJson": True}, False, False),
        ({"includeMcpJson": True, "useLegacyMcpJson": False}, True, True),
        # A non-bool states nothing, so the next key (or the unstated reading) decides.
        ({"includeMcpJson": "yes"}, False, False),
        ({"includeMcpJson": None, "useLegacyMcpJson": True}, True, True),
        ({}, False, False),
    ],
)
def test_the_opt_in_is_a_stated_bool_read_per_host(spec, on_kiro, on_kas):
    from kiro_crew.acp.kas_settings_mcp import include_mcp_json as kas_wire_opt_in

    assert includes_mcp_json(spec, ACP_BACKEND_KIRO) is on_kiro
    assert includes_mcp_json(spec, ACP_BACKEND_KAS) is on_kas
    # KAS mounts the mcp.json servers exactly when the projection sends it a true
    # ``includeMcpJson`` (``kas_settings_mcp.excluded_whole``), so the two agree.
    assert includes_mcp_json(spec, ACP_BACKEND_KAS) is (kas_wire_opt_in(spec) is True)
    # A host reached only through the session/new array loads no kiro mcp.json.
    assert includes_mcp_json(spec, ACP_BACKEND_CODEX) is False


def test_a_spec_that_is_not_an_object_opts_in_to_nothing(kiro_home: Path):
    _write(kiro_home / "settings" / "mcp.json", {"g": {"command": "x"}})
    for spec in (None, [], "includeMcpJson"):
        assert includes_mcp_json(spec, ACP_BACKEND_KIRO) is False
        assert mcp_json_server_names(spec, ACP_BACKEND_KIRO) == ()


def test_the_names_are_the_enabled_global_servers_the_spec_does_not_define(kiro_home: Path):
    _write(
        kiro_home / "settings" / "mcp.json",
        {
            "slow": {"command": "npx"},
            "off": {"disabled": True},
            "odd": {"disabled": "yes"},
            "own": {"command": "x"},
        },
    )
    spec = {"includeMcpJson": True, "mcpServers": {"own": {"command": "y"}}}

    # A muted entry (the launch's own rule) starts nothing, and a name the spec
    # defines mounts from the spec's entry, so neither is an mcp.json server.
    assert mcp_json_server_names(spec, ACP_BACKEND_KIRO) == ("slow",)
    assert mcp_json_server_names(spec, ACP_BACKEND_KAS) == ("slow",)


def test_without_the_opt_in_the_global_file_mounts_nothing(kiro_home: Path):
    _write(kiro_home / "settings" / "mcp.json", {"slow": {"command": "npx"}})

    for spec in ({}, {"includeMcpJson": False}, {"useLegacyMcpJson": False}):
        assert mcp_json_server_names(spec, ACP_BACKEND_KIRO) == ()
        assert mcp_json_server_names(spec, ACP_BACKEND_KAS) == ()
    assert mcp_json_server_names({"includeMcpJson": "true"}, ACP_BACKEND_KIRO) == ()
    assert mcp_json_server_names({"includeMcpJson": True}, ACP_BACKEND_CODEX) == ()
    # The older spelling opts in on both hosts.
    assert mcp_json_server_names({"useLegacyMcpJson": True}, ACP_BACKEND_KAS) == ("slow",)


def test_the_checkout_file_adds_its_servers_and_a_mute_in_either_file_wins(
    kiro_home: Path, tmp_path: Path
):
    _write(kiro_home / "settings" / "mcp.json", {"g": {"command": "a"}, "both": {"command": "a"}})
    checkout = tmp_path / "checkout"
    _write(
        checkout / ".kiro" / "settings" / "mcp.json",
        {"w": {"command": "b"}, "both": {"disabled": True}},
    )
    spec = {"includeMcpJson": True}

    assert mcp_json_server_names(spec, ACP_BACKEND_KIRO, checkout) == ("g", "w")
    # With no checkout, only the user-level file is read.
    assert mcp_json_server_names(spec, ACP_BACKEND_KIRO) == ("g", "both")


@pytest.mark.parametrize(
    "text",
    [
        "{oops",
        "[]",
        '{"mcpServers": ["a"]}',
        '{"mcpServers": {"a": "not an object", "": {"command": "x"}}}',
    ],
)
def test_a_malformed_file_contributes_nothing_and_never_raises(kiro_home: Path, text: str):
    path = kiro_home / "settings" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")

    assert mcp_json_server_names({"includeMcpJson": True}, ACP_BACKEND_KIRO) == ()


def test_a_missing_file_or_a_directory_in_its_place_contributes_nothing(
    kiro_home: Path, tmp_path: Path
):
    checkout = tmp_path / "checkout"
    (checkout / ".kiro" / "settings" / "mcp.json").mkdir(parents=True)

    assert mcp_json_server_names({"includeMcpJson": True}, ACP_BACKEND_KIRO, checkout) == ()


def test_a_checkout_file_linked_into_a_protected_directory_is_never_read(
    kiro_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A link in the checkout, committed or made by the agent, must not make the
    gateway open the credential store it points into, whatever that file holds."""
    # is_sensitive_path is HOME-relative and resolved per call, so pointing HOME at
    # the fixture makes this credential store genuinely protected.
    home = tmp_path / "home"
    _write(home / ".aws" / "mcp.json", {"leaked": {"command": "x"}})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    checkout = tmp_path / "checkout"
    settings = checkout / ".kiro" / "settings"
    settings.parent.mkdir(parents=True)
    # A directory link: a junction on Windows, which needs no privilege.
    make_dir_link(settings, home / ".aws")
    spec = {"includeMcpJson": True}

    assert mcp_json_server_names(spec, ACP_BACKEND_KIRO, checkout) == ()

    # The refusal is the protected target's, not the link's: a link to an ordinary
    # directory is still read.
    ordinary = tmp_path / "shared-settings"
    _write(ordinary / "mcp.json", {"shared": {"command": "x"}})
    other = tmp_path / "other-checkout"
    (other / ".kiro").mkdir(parents=True)
    make_dir_link(other / ".kiro" / "settings", ordinary)
    assert mcp_json_server_names(spec, ACP_BACKEND_KIRO, other) == ("shared",)


def test_the_names_kept_are_bounded_in_count_and_length(kiro_home: Path):
    from kiro_crew.agent_sdk.mcp_json_reach import MAX_MCP_JSON_NAME_LEN, MAX_MCP_JSON_NAMES

    servers: dict = {"x" * (MAX_MCP_JSON_NAME_LEN + 1): {"command": "a"}}
    servers.update({f"s{i}": {"command": "a"} for i in range(MAX_MCP_JSON_NAMES + 10)})
    _write(kiro_home / "settings" / "mcp.json", servers)

    # A name too long is left out whole, and the count stops at the bound.
    assert mcp_json_server_names({"includeMcpJson": True}, ACP_BACKEND_KIRO) == tuple(
        f"s{i}" for i in range(MAX_MCP_JSON_NAMES)
    )


def test_a_server_the_spec_refs_past_the_bound_is_kept_and_never_unresolved(kiro_home: Path):
    from kiro_crew.agent_sdk.mcp_json_reach import MAX_MCP_JSON_NAMES
    from kiro_crew.agent_sdk.mcp_refs import unresolved_server_refs

    _write(
        kiro_home / "settings" / "mcp.json",
        {f"s{i}": {"command": "a"} for i in range(MAX_MCP_JSON_NAMES + 1)},
    )
    last = f"s{MAX_MCP_JSON_NAMES}"
    spec = {"includeMcpJson": True, "tools": [f"@{last}"]}

    names = mcp_json_server_names(spec, ACP_BACKEND_KIRO)

    # The cap leaves out unreferenced servers only; a referenced one past it is
    # still a configured server, so the ref guard does not call it unconfigured.
    assert len(names) == MAX_MCP_JSON_NAMES + 1 and names[-1] == last
    assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_KIRO, mcp_json=names) == []
    # An unreferenced server past the cap is still left out.
    assert len(mcp_json_server_names({"includeMcpJson": True}, ACP_BACKEND_KIRO)) == (
        MAX_MCP_JSON_NAMES
    )


def test_a_referenced_name_past_the_length_bound_is_kept(kiro_home: Path):
    from kiro_crew.agent_sdk.mcp_json_reach import MAX_MCP_JSON_NAME_LEN

    long_name = "x" * (MAX_MCP_JSON_NAME_LEN + 1)
    _write(kiro_home / "settings" / "mcp.json", {long_name: {"command": "a"}})

    assert mcp_json_server_names({"includeMcpJson": True}, ACP_BACKEND_KIRO) == ()
    spec = {"includeMcpJson": True, "tools": [f"@{long_name}"]}
    assert mcp_json_server_names(spec, ACP_BACKEND_KIRO) == (long_name,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("opt_in", "expected"), [(True, ["g", "own"]), (False, ["own"]), (None, ["own"])]
)
async def test_the_dashboard_lists_the_global_servers_an_agent_opts_in_to(
    kiro_home: Path,
    agents_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    opt_in: bool | None,
    expected: list[str],
):
    """``GET /api/mcp/active?agent=<name>`` names what kiro-cli mounts for that agent."""
    import kiro_crew.config.loader as loader

    # Bind the name to a same-named kiro agent, so the per-agent branch answers.
    monkeypatch.setattr(
        loader, "resolve_agent_bindings", lambda cfg, name: SimpleNamespace(kiro_agent=name)
    )
    _write(
        kiro_home / "settings" / "mcp.json", {"g": {"command": "x"}, "muted": {"disabled": True}}
    )
    spec: dict = {"name": "other", "mcpServers": {"own": {}}}
    if opt_in is not None:
        spec["includeMcpJson"] = opt_in
    (agents_dir / "other.json").write_text(json.dumps(spec), encoding="utf-8")

    resp = await mcp_mod.api_mcp_active(_request(query={"agent": "other"}))

    assert resp.status == 200
    assert _payload(resp) == [{"name": n, "enabled": True} for n in expected]
