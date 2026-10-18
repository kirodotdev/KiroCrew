"""The fake ACP client, fixtures and helpers the ``test_connections_mint*.py`` suites share.

``_isolated_mint`` is autouse: every test gets an empty mint table, a scratch agents
dir and grant cache, and ``_FakeClient`` in place of a real spawn. Each suite binds it,
and any fixture here it requests, by name so pytest registers them for that module.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import mcp_grant
from kiro_crew.connections import mint

_URL = "https://mcp.example.com/mcp"
_AUTHORIZE = (
    "https://auth.example.com/authorize?client_id=abc"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A43123%2Foauth%2Fcallback&state=opaque"
)


class _FakeClient:
    """Stand-in for AcpClient: records lifecycle, yields one OAuth challenge."""

    instances: list["_FakeClient"] = []
    next_pid = 424242
    #: Scripted (`/mcp`, `/tools`) results for the grant-validation path, class-wide
    #: so a test can arm it before the mint spawns any instance. Defaults to a
    #: usable Notion server -- most tests never touch validation and must not have
    #: to script it just to reach the mint spawn loop.
    command_results: dict[str, dict[str, Any]] = {
        "/mcp": {
            "data": {
                "servers": [
                    {"name": "notion", "status": "running", "toolCount": 1, "authenticating": False}
                ]
            }
        },
        "/tools": {
            "data": {"tools": [{"name": "search", "source": "mcp:notion", "status": "allowed"}]}
        },
    }

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.ready = False
        self.shutdowns = 0
        self.alive = True
        _FakeClient.next_pid += 1
        self._pid = _FakeClient.next_pid
        self.requests: list[dict[str, str]] = [
            {"serverName": "notion", "oauthUrl": _AUTHORIZE},
        ]
        self.commands: list[str] = []
        _FakeClient.instances.append(self)

    async def ensure_ready(self) -> None:
        self.ready = True

    async def command_result(self, command: str, args: dict | None = None) -> dict[str, Any]:
        self.commands.append(command)
        outcome = _FakeClient.command_results.get(command, {})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        out = list(self.requests)
        self.requests.clear()
        return out

    def is_process_alive(self) -> bool:
        return self.alive

    async def shutdown(self) -> None:
        self.shutdowns += 1


def _state_only(view: dict | None) -> dict:
    """The card-facing view minus the row token.

    The token is a correlation id, not part of the state contract, so assertions
    about what a state MEANS compare without it. Its presence has its own tests.
    """
    return {k: v for k, v in (view or {}).items() if k != "token"}


def _grant_reads_recorded(seen: list[int]) -> Any:
    """The real grant predicate, plus the id of the thread each read ran on."""
    real = mcp_grant.grant_presence

    def recorded(url: str, **kw: Any) -> bool:
        seen.append(threading.get_ident())
        return real(url, **kw)

    return recorded


def _write_paired_grant_artifacts(mcp_url: str) -> None:
    """Land a grant kiro-cli would recognize in the scratch cache dir."""
    cache_dir = mcp_grant.kiro_oauth_cache_dir()
    key = mcp_grant.grant_key(mcp_url)
    (cache_dir / f"{key}.token.json").write_text("{}", encoding="utf-8")
    (cache_dir / f"{key}.registration.json").write_text("{}", encoding="utf-8")


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse fixture of test_connections_mint.py and the suites split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _isolated_mint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Empty mint table, a scratch agents dir and grant cache, no real spawn."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "kirocrew.json").write_text(
        json.dumps({"mcpServers": {"notion": {"url": _URL}}}), encoding="utf-8"
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: agents_dir)
    monkeypatch.setattr(mcp_grant, "kiro_oauth_cache_dir", lambda **kw: cache_dir)
    # The manifest is real gateway state; tests must never write the live one.
    monkeypatch.setattr(mint, "_mint_manifest_path", lambda: tmp_path / "mint-specs.json")
    monkeypatch.setattr(mint, "_mints", {})
    monkeypatch.setattr(mint, "_mints_lock", asyncio.Lock())
    _FakeClient.instances.clear()
    _FakeClient.command_results = {
        "/mcp": {
            "data": {
                "servers": [
                    {"name": "notion", "status": "running", "toolCount": 1, "authenticating": False}
                ]
            }
        },
        "/tools": {
            "data": {"tools": [{"name": "search", "source": "mcp:notion", "status": "allowed"}]}
        },
    }
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _FakeClient)
    return agents_dir


@pytest.fixture()
def protected_pids(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """The sweep-protection set as the mint suites drive it."""
    live: set[int] = set()
    monkeypatch.setattr(mint, "register_protected_pid", live.add)
    monkeypatch.setattr(mint, "unregister_protected_pid", live.discard)
    return live
