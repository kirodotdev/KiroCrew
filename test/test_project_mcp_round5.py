"""Project MCP cards show complete launches and boot reads stay off the event loop."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.change_card_catalog import CardCatalogError
from kiro_crew.change_card_project_mcp import build_preview, server_snapshot
from kiro_crew.dashboard.project_mcp_cards import _reason, offer_project_mcp_cards
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    settled = threading.Event()
    settled.set()
    monkeypatch.setattr(approvals, "_MIGRATION_SETTLED", settled)
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def _server(folder, entry):
    (folder / CLAUDE_PROJECT_MCP_FILE).write_text(
        json.dumps({"mcpServers": {"weather": entry}}), encoding="utf-8"
    )
    [server] = approvals.pending_servers(folder)
    return server


def _params(server):
    return {
        "folder": server.folder,
        "file": server.file,
        "server": server.name,
        "fingerprint": server.fingerprint,
    }


@pytest.mark.parametrize("key", ["command", "args", "url"])
def test_the_record_keeps_a_full_launch_digest(project, key):
    value = "a" * (approvals._MAX_LAUNCH_CHARS + 1)
    entry = {key: [value] if key == "args" else value}
    other = {key: [value + "b"] if key == "args" else value + "b"}
    saved = approvals.summary(entry)
    assert len(saved["url" if key == "url" else "launch"]) <= approvals._MAX_LAUNCH_CHARS
    assert saved["launch_digest"] != approvals.summary(other)["launch_digest"]
    assert (
        saved["launch_digest"] == approvals.summary({**entry, "env": {"A": "b"}})["launch_digest"]
    )


@pytest.mark.parametrize("digest", [None, "0" * 64])
def test_command_rows_and_reasons_use_the_record_digest(project, digest):
    entry = {"command": "weather-server"}
    server = _server(project, entry)
    saved = approvals.summary(entry)
    if digest is None:
        saved.pop("launch_digest")
    else:
        saved["launch_digest"] = digest
    changed = approvals.ProjectServer(
        server.folder, server.file, server.name, entry, server.fingerprint, "changed", saved
    )
    out: dict[str, Any] = {}
    build_preview(
        _params(changed),
        {
            "pending": True,
            "fingerprint": changed.fingerprint,
            "status": "changed",
            "entry": entry,
            "approved": saved,
        },
        out,
    )
    command = next(row for row in out["changes"] if row["label"] == "Runs")
    assert command["after"] == "weather-server"
    assert _reason(changed) == "Held back: its command changed since you allowed it here."


@pytest.mark.parametrize("key", ["command", "url"])
def test_an_unshowable_launch_has_no_apply_step(project, key):
    server = _server(project, {key: "a" * (approvals._MAX_LAUNCH_CHARS + 1)})
    params = _params(server)
    out: dict[str, Any] = {}
    with pytest.raises(CardCatalogError) as caught:
        build_preview(params, server_snapshot(params), out)
    assert caught.value.code == "card_too_large"
    assert "apply" not in out
    assert approvals.pending_servers(project) == [server]


@pytest.mark.asyncio
async def test_an_unshowable_card_gets_one_notice_and_stays_held(project, monkeypatch):
    from kiro_crew.agent_sdk import capabilities
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
    from kiro_crew.dashboard import state as dashboard_state
    from kiro_crew.dashboard.handlers import change_cards_propose

    server = _server(project, {"command": "a" * (approvals._MAX_LAUNCH_CHARS + 1)})
    rows = []
    cards = []

    async def propose(*_a, params, **_kw):
        out: dict[str, Any] = {}
        build_preview(params, server_snapshot(params), out)
        cards.append(out)
        return out

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
    assert cards == []
    assert rows == [
        (
            "notice",
            f"⚠ weather in {server.path} is too large to show as a card. "
            "Shorten its command, URL or settings in that file, then start a new chat.",
        )
    ]
    assert approvals.pending_servers(project) == [server]


def test_boot_skips_an_existing_record_without_reading_it(project, monkeypatch):
    path = approvals.store_path()
    path.parent.mkdir(parents=True)
    path.write_text("not JSON", encoding="utf-8")

    def unexpected(*_a):
        pytest.fail("an existing record needs no boot reads")

    monkeypatch.setattr(approvals, "_read_store", unexpected)
    monkeypatch.setattr(approvals, "_known_folders", unexpected)
    assert approvals.begin_boot_migration() is None
    assert approvals._MIGRATION_SETTLED.is_set()


@pytest.mark.asyncio
async def test_boot_reads_off_loop_and_hands_off_an_unsettled_scan(project, monkeypatch):
    from kiro_crew.slack.gateway import GatewayOrchestrator

    _init_services = GatewayOrchestrator._init_services

    loop_thread = threading.get_ident()
    release = threading.Event()
    scanned = []

    def folders():
        assert threading.get_ident() != loop_thread
        return [str(project)]

    def migrate(path, known):
        scanned.append((known, approvals._MIGRATION_SETTLED.is_set()))
        assert release.wait(30), "the boot checkpoint did not release the scan"

    class Checkpoint(Exception):
        pass

    async def checkpoint():
        assert not approvals._MIGRATION_SETTLED.is_set()
        raise Checkpoint

    monkeypatch.setattr(approvals, "_known_folders", folders)
    monkeypatch.setattr(approvals, "_migrate", migrate)
    gateway: Any = SimpleNamespace(
        _slack_enabled=False, _warn_if_kiro_cli_outdated=checkpoint, _background_tasks=set()
    )
    try:
        with pytest.raises(Checkpoint):
            await asyncio.wait_for(_init_services(gateway), timeout=30)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*gateway._background_tasks), timeout=30)
    assert scanned == [([str(project)], False)]
    assert approvals._MIGRATION_SETTLED.is_set()
