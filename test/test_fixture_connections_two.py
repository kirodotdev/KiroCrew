"""Consumer coverage for the ``connections-two`` seeded-home fixture."""

from __future__ import annotations

import shutil

import pytest

from kiro_crew import mcp_discovery
from kiro_crew.testing.fixtures import seeded_home


@pytest.mark.asyncio
async def test_connections_two_drives_real_mcp_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discover both data-home entries and enforce the disabled spawn gate."""
    spawned: list[tuple[object, ...]] = []

    async def _refuse_spawn(*args: object, **_kwargs: object) -> None:
        spawned.append(args)
        raise AssertionError("a disabled fixture server must never be spawned")

    with seeded_home("connections-two") as home:
        kirocrew_source = mcp_discovery._mcp_sources()[0]
        assert kirocrew_source[0].resolve() == (home / "mcp.json").resolve()
        assert kirocrew_source[1] == mcp_discovery.SCOPE_KIROCREW

        # A fixture can seed only its data-home scope. Keep discovery on that
        # production-resolved source so this test never reads a machine-global
        # MCP config or an edition-contributed provider scope.
        monkeypatch.setattr(mcp_discovery, "_MCP_SOURCES", (kirocrew_source,))
        monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", None)
        monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", lambda: [])
        monkeypatch.setattr(
            mcp_discovery,
            "kiro_agents_dir",
            lambda: home / "isolated-kiro-agents",
        )

        scoped = mcp_discovery._load_mcp_json_by_source()
        assert set(scoped[mcp_discovery.SCOPE_KIROCREW]) == {
            "fixture-healthy",
            "fixture-broken",
        }

        rows = {row.name: row for row in mcp_discovery.list_servers()}
        healthy = rows["fixture-healthy"]
        broken = rows["fixture-broken"]

        assert healthy.source == "mcp.json"
        assert healthy.presence[mcp_discovery.SCOPE_KIROCREW] is True
        assert healthy.disabled is False
        assert healthy.env == {}
        assert isinstance(healthy.args, list) and healthy.args
        assert shutil.which(healthy.command), "the nominal stdio command must resolve"
        assert healthy.args[-1] == "import sys; sys.stdin.buffer.read()"

        assert broken.source == "mcp.json"
        assert broken.presence[mcp_discovery.SCOPE_KIROCREW] is False
        assert broken.disabled is True
        assert shutil.which(broken.command) is None
        assert broken.env == {
            "FIXTURE_API_TOKEN": "fixture-placeholder-not-secret",
        }

        monkeypatch.setattr(
            mcp_discovery,
            "create_subprocess_limited",
            _refuse_spawn,
        )
        result = await mcp_discovery.probe_server(broken)

    assert result.status == "disabled"
    assert spawned == []
