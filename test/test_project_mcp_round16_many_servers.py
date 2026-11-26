"""A project file with more held servers than a chat's card limit stops at the limit."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.change_card_project_mcp import server_snapshot
from kiro_crew.dashboard.change_cards import MAX_LIVE_CARDS_PER_SLOT, CardError
from kiro_crew.dashboard.project_mcp_cards import offer_project_mcp_cards
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE


@pytest.fixture
def project(tmp_path, monkeypatch):
    settled = threading.Event()
    settled.set()
    monkeypatch.setattr(approvals, "_MIGRATION_SETTLED", settled)
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


async def _offer(project, monkeypatch, count):
    from kiro_crew.agent_sdk import capabilities
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
    from kiro_crew.dashboard import state as dashboard_state
    from kiro_crew.dashboard.handlers import change_cards_propose

    servers = {f"s{n:04d}": {"command": f"server-{n}"} for n in range(count)}
    (project / CLAUDE_PROJECT_MCP_FILE).write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )
    scans = []
    real_check = approvals.check_folder

    def check_folder(*a, **kw):
        scans.append(1)
        return real_check(*a, **kw)

    proposed: list[str] = []
    rows: list[tuple[str, str]] = []

    async def propose(*_a, params, **_kw):
        # Like the real route: the card is built (one file scan) before the store
        # refuses it past the slot's live-card limit.
        server_snapshot(params)
        if len(proposed) >= MAX_LIVE_CARDS_PER_SLOT:
            raise CardError(429, "too_many_cards", "this conversation has too many open cards")
        proposed.append(params["server"])
        return {}

    monkeypatch.setattr(approvals, "check_folder", check_folder)
    monkeypatch.setattr(change_cards_propose, "propose_in_slot", propose)
    monkeypatch.setattr(
        dashboard_state,
        "append_and_surface",
        lambda state, slot, role, content, *_a: rows.append((role, content)),
    )
    monkeypatch.setattr(
        capabilities, "capabilities_of", lambda _: SimpleNamespace(backend=ACP_BACKEND_CLAUDE)
    )
    slot = SimpleNamespace(key="chat-1", project=str(project))
    await offer_project_mcp_cards(object(), slot, SimpleNamespace(cwd=str(project)), "sk")
    return proposed, rows, len(scans)


@pytest.mark.asyncio
async def test_the_offer_stops_at_the_card_limit_with_one_notice(project, monkeypatch):
    extra = 24
    proposed, rows, scans = await _offer(project, monkeypatch, MAX_LIVE_CARDS_PER_SLOT + extra)
    assert len(proposed) == MAX_LIVE_CARDS_PER_SLOT
    path = approvals.pending_servers(project)[0].path
    assert rows == [
        (
            "notice",
            f"⚠ {extra} more servers in {path} are held. Allow or dismiss the cards "
            "above, then start a new chat to see the rest.",
        )
    ]
    # The cards built plus the one refused; no scan per server past the limit.
    assert scans == MAX_LIVE_CARDS_PER_SLOT + 1


@pytest.mark.asyncio
async def test_the_scan_count_does_not_grow_past_the_limit(project, monkeypatch):
    _, _, small = await _offer(project, monkeypatch, MAX_LIVE_CARDS_PER_SLOT + 4)
    _, _, large = await _offer(project, monkeypatch, MAX_LIVE_CARDS_PER_SLOT * 5)
    assert small == large
