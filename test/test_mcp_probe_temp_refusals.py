"""A declared-temp refusal reaches the probe payload, not only the journal.

The classifier is stubbed, so these run on every OS: they pin the plumbing from
``probe_server`` into ``McpServerInfo.temp_refusals``, ``to_dict``, the probe
cache and the ``list_servers`` merge, not the sealing decision itself.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import mcp_discovery
from kiro_crew.mcp_discovery import (
    McpServerInfo,
    _cache_probe,
    _probe_cache,
    list_servers,
    probe_metadata,
    probe_server,
)

# A credential-shaped path segment: the payload must carry the redacted text.
_SECRET = "AKIAIOSFODNN7EXAMPLE"
_DECLARED = f"/data/run/{_SECRET}/tmp"
_REDACTED = "/data/run/[REDACTED: credential]/tmp"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No real spawn, no managed-temp allocation, a clean cache."""
    from kiro_crew.mcp_gateway import backend_tmp as bt

    _probe_cache.clear()
    monkeypatch.setattr(bt, "allocate_probe_tmp", MagicMock(side_effect=OSError("no alloc")))

    def _wrap(argv, *a, env=None, **k):
        return list(argv), dict(env if env is not None else os.environ), None

    monkeypatch.setattr(mcp_discovery, "sandboxed_spawn_argv", _wrap)
    monkeypatch.setattr(
        mcp_discovery,
        "create_subprocess_limited",
        AsyncMock(side_effect=OSError("stop after env capture")),
    )
    yield
    _probe_cache.clear()


def _server(env: dict[str, str]) -> McpServerInfo:
    return McpServerInfo(name="temp-refusal", command=sys.executable, args=["-c", "pass"], env=env)


@pytest.mark.asyncio
async def test_sealed_refusal_is_in_the_payload_with_the_warning_facts(monkeypatch, caplog) -> None:
    monkeypatch.setattr(mcp_discovery, "classify_declared_temp_path", lambda path: "sealed")
    server = _server({"TMPDIR": _DECLARED})

    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_discovery"):
        await probe_server(server)

    expected = [{"key": "TMPDIR", "path": _REDACTED, "cause": "sealed"}]
    assert server.to_dict()["tempRefusals"] == expected
    warning = next(
        r.getMessage() for r in caplog.records if "ignoring spec-declared" in r.getMessage()
    )
    # The journal line names the same key and redacted path.
    assert f"TMPDIR={_REDACTED!r}" in warning
    assert _SECRET not in warning
    assert _SECRET not in json.dumps(server.to_dict())


@pytest.mark.asyncio
async def test_check_failed_refusal_is_reported_with_its_cause(monkeypatch) -> None:
    def _boom(path: str) -> None:
        raise OSError(f"cannot stat {path}")

    monkeypatch.setattr(mcp_discovery, "classify_declared_temp_path", _boom)
    server = _server({"TMP": _DECLARED})

    await probe_server(server)

    assert server.to_dict()["tempRefusals"] == [
        {"key": "TMP", "path": _REDACTED, "cause": "check-failed"}
    ]


@pytest.mark.asyncio
async def test_a_probe_that_never_spawns_drops_a_rehydrated_refusal() -> None:
    # Disabled: probe_server returns before the spawn path, so no handshake
    # stands behind the cached refusal and the row must not repeat it.
    server = _server({"TMPDIR": _DECLARED})
    server.temp_refusals = [{"key": "TMPDIR", "path": "/old", "cause": "sealed"}]
    _cache_probe(server)
    server.disabled = True

    await probe_server(server)

    assert server.status == "disabled"
    assert "tempRefusals" not in server.to_dict()
    # The cache is cleared too, so the next list_servers read cannot rehydrate it.
    cached = probe_metadata(server.name)
    assert cached is not None and cached.temp_refusals == []


@pytest.mark.asyncio
async def test_no_refusal_omits_the_key_and_clears_a_rehydrated_list(monkeypatch) -> None:
    monkeypatch.setattr(mcp_discovery, "classify_declared_temp_path", lambda path: None)
    server = _server({"TMPDIR": "/chosen/tmp"})
    # As if ``list_servers`` rehydrated an earlier probe's refusal onto the row.
    server.temp_refusals = [{"key": "TMPDIR", "path": "/old", "cause": "sealed"}]

    await probe_server(server)

    assert server.temp_refusals == []
    assert "tempRefusals" not in server.to_dict()


def test_refusal_survives_the_cache_and_the_list_merge(tmp_path, monkeypatch) -> None:
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    cfg = {"mcpServers": {"srv": {"command": "srv", "env": {"TMPDIR": "/x/run/t"}}}}
    (agent_dir / "defaults.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (tmp_path / "nope.json",))
    monkeypatch.setattr(mcp_discovery.Path, "home", lambda: tmp_path)

    (row,) = [s for s in list_servers() if s.name == "srv"]
    row.status = "ok"
    row.temp_refusals = [{"key": "TMPDIR", "path": "/x/run/t", "cause": "sealed"}]
    _cache_probe(row)

    cached = probe_metadata("srv")
    assert cached is not None and cached.temp_refusals == row.temp_refusals
    (merged,) = [s for s in list_servers() if s.name == "srv"]
    assert merged.status == "ok"
    assert merged.to_dict()["tempRefusals"] == row.temp_refusals
