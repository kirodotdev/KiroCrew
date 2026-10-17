"""Request doubles and fixtures shared by the ``test_handlers_mcp_coverage*.py`` modules.

The aiohttp request double, the background-task registry stand-in, the ``sandbox``
fixture that points every ``handlers/mcp.py`` filesystem and audit seam at
``tmp_path``, and the tmp agents directory.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from body_stream_helpers import BodyStreamPayload
from dashboard_owner_helpers import owner_claims

from kiro_crew.dashboard.handlers import mcp as mcp_mod

# ── harness ─────────────────────────────────────────────────────────────


def _request(
    body: Any = None,
    *,
    state: Any = None,
    query: dict[str, str] | None = None,
    match_info: dict[str, str] | None = None,
    method: str = "POST",
) -> web.Request:
    """Minimal aiohttp request double, matching the suite's existing style."""
    req = MagicMock(spec=web.Request)
    if isinstance(body, Exception):
        # Malformed body: real undecodable bytes for the streaming (capped)
        # path, a raising mock for the ``request.json()`` (uncapped) path.
        raw = b"{not json"
        req.json = AsyncMock(side_effect=body)
    else:
        if (
            isinstance(body, dict)
            and body.get("stub") is True
            and isinstance(body.get("name"), str)
            and "expected_launch" not in body
        ):
            from kiro_crew.mcp_gateway import launch_approval

            env_hash = launch_approval.env_fingerprint({})
            expected = launch_approval.launch_pair(
                launch_approval.hash_command(body["name"], []), env_hash
            )
            body = {**body, "expected_launch": expected}
        raw = json.dumps(body).encode() if body is not None else b""
        # Kept alive: ``api_mcp_server_detail`` reads uncapped
        # (``max_bytes=None``) and consumes ``request.json()``.
        req.json = AsyncMock(return_value=body)
    req.content = BodyStreamPayload(raw)
    req.content_length = len(raw) if raw else None
    req.charset = None
    req.can_read_body = bool(raw)
    req.app = {"state": state if state is not None else _State()}
    req.query = query or {}
    req.match_info = match_info or {}
    req.method = method
    # Every mutating handler in handlers/mcp.py is owner-gated
    # (``handlers._shared.require_owner_dashboard_request``), so the double has to
    # carry the claims the token-auth middleware publishes or each test lands on
    # the gate instead of its own subject.
    return owner_claims(req)


class _State:
    """Stand-in for DashboardState's background-task registry.

    ``owner_id`` is ``""`` -- the standalone-local shape, where the owner gate on
    these routes accepts the signed local bootstrap subject ``owner_claims`` sets.
    """

    owner_id = ""

    def __init__(self) -> None:
        self._background_tasks: set[asyncio.Task] = set()
        self.sessions = SimpleNamespace(final_drain_started=False)


def _effective_stubs(section: dict[str, Any]) -> list[str]:
    """The stub set IN EFFECT for a saved ``mcp_gateway`` section.

    The toggle handler does not rewrite ``stub_servers``: that key is the roster
    a distribution ships and keeps growing, and a click is recorded as a decision
    in ``stub_overrides`` over it. What the operator sees stubbed is the two
    resolved together, so that -- not either key alone -- is what a test about
    toggle behaviour should assert. Reads the production resolver so these tests
    cannot drift from the runtime's own answer.
    """
    from kiro_crew.config.loader import _resolve_stub_servers

    return _resolve_stub_servers(section)


def _payload(resp: web.Response) -> Any:
    body = resp.body
    assert isinstance(body, (bytes, bytearray))
    return json.loads(body)


@pytest.fixture(name="sandbox")
def sandbox_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Point every mcp.py filesystem + audit seam at ``tmp_path``.

    The agent-config sync (``kirocrew.json`` read-modify-write) belongs to
    ``handlers.agents`` and has its own suite — record the calls instead of
    re-testing it here.
    """
    global_json = tmp_path / "kiro" / "settings" / "mcp.json"
    global_json.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", global_json)
    monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", global_json.with_suffix(".lock"))
    monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "crew" / "mcp.json")
    monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", list)
    sel = MagicMock()
    monkeypatch.setattr(mcp_mod, "sel", lambda: sel)

    synced: list[tuple[str, bool, bool]] = []
    batched: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(
        mcp_mod,
        "_sync_mcp_to_agent",
        lambda name, enabled, remove=False: synced.append((name, enabled, remove)),
    )
    monkeypatch.setattr(
        mcp_mod,
        "_sync_mcp_to_agent_batch",
        lambda names, enabled: batched.append((list(names), enabled)),
    )
    return SimpleNamespace(
        global_json=global_json,
        synced=synced,
        batched=batched,
        sel=sel,
    )


def _write_global(sandbox: SimpleNamespace, servers: dict[str, Any]) -> None:
    sandbox.global_json.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _read_global(sandbox: SimpleNamespace) -> dict[str, Any]:
    data = json.loads(sandbox.global_json.read_text(encoding="utf-8"))
    servers = data.get("mcpServers", {})
    assert isinstance(servers, dict)
    return servers


@pytest.fixture(name="agents_dir")
def agents_dir_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``kiro_agents_dir_path()`` at a tmp agents directory."""
    import kiro_crew.agent as agent_mod

    d = tmp_path / "agents"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    return d
