"""A folder's own MCP servers wait for the person's approval before a session starts them.

The record the gateway keeps (``project_mcp_approvals``), the hold a session puts on
an unapproved server (``agent_sdk.project_mcp_holds``), the card that asks
(``change_card_project_mcp``) and the route its Apply calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp.web import Request

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.agent_sdk import project_mcp_holds as holds
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.change_card_catalog import CardCatalogError
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE

_PLANTED = {"command": "node", "args": ["planted.js"], "env": {"TOKEN": "abc"}}
_EXISTING = {"command": "npx", "args": ["-y", "weather-mcp"]}


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    crew = tmp_path / "crew-home"
    crew.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    return crew


def _write(folder: Path, rel: str, servers: dict[str, Any]) -> None:
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _folder(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    return folder


def _names(servers) -> list[str]:
    return sorted(s.name for s in servers)


def _migrate() -> None:
    scan = approvals.begin_boot_migration()
    if scan is not None:
        scan()


@pytest.mark.parametrize("start_known", [False, True])
def test_boot_approves_what_known_folders_already_hold(home, tmp_path, start_known):
    known = _folder(tmp_path, "known")
    _write(known, CLAUDE_PROJECT_MCP_FILE, {"weather": _EXISTING})
    (home / "recent_projects.json").write_text(json.dumps([str(known)]), encoding="utf-8")
    other = _folder(tmp_path, "other")

    _migrate()
    assert approvals.pending_servers(known if start_known else other) == []
    assert approvals.store_path().is_file()
    assert approvals.pending_servers(known) == []

    # A file that appears in another folder afterwards is not approved.
    _write(other, KIRO_PROJECT_MCP_FILE, {"planted": _PLANTED})
    pending = approvals.pending_servers(other)
    assert _names(pending) == ["planted"]
    assert pending[0].status == approvals.STATUS_NEW
    assert pending[0].file == KIRO_PROJECT_MCP_FILE


def test_approve_records_the_shown_launch_and_a_change_asks_again(home, tmp_path):
    approvals.pending_servers(_folder(tmp_path, "first"))  # the one-time record
    folder = _folder(tmp_path, "repo")
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"planted": _PLANTED})
    [server] = approvals.pending_servers(folder)

    approvals.approve(str(folder), CLAUDE_PROJECT_MCP_FILE, "planted", server.fingerprint)
    assert approvals.pending_servers(folder) == []
    record = approvals.store_path().read_text(encoding="utf-8")
    assert "abc" not in record  # an env value is never copied into the record

    # Only the launch fields count: a tool switch is no new launch.
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"planted": {**_PLANTED, "disabledTools": ["x"]}})
    assert approvals.pending_servers(folder) == []

    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"planted": {**_PLANTED, "args": ["evil.js"]}})
    [changed] = approvals.pending_servers(folder)
    assert changed.status == approvals.STATUS_CHANGED
    assert changed.approved["launch"] == "node planted.js"

    with pytest.raises(approvals.ApprovalStale):
        approvals.approve(str(folder), CLAUDE_PROJECT_MCP_FILE, "planted", server.fingerprint)


def test_a_disabled_server_and_a_folder_without_files_hold_nothing(home, tmp_path):
    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, KIRO_PROJECT_MCP_FILE, {"off": {**_PLANTED, "disabled": True}})
    assert approvals.pending_servers(folder) == []
    assert approvals.pending_servers(None) == []
    assert approvals.pending_servers("relative") == []


def test_an_unreadable_record_approves_nothing(home, tmp_path):
    folder = _folder(tmp_path, "repo")
    _write(folder, KIRO_PROJECT_MCP_FILE, {"planted": _PLANTED})
    path = approvals.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert _names(approvals.pending_servers(folder)) == ["planted"]


def _spec(monkeypatch, spec: dict[str, Any] | None) -> None:
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.harness import kas

    monkeypatch.setattr(session_mcp, "agent_spec_snapshot", lambda agent, work_dir=None: spec)
    monkeypatch.setattr(kas, "resolve_projected_spec", lambda directory, *_a: (spec, directory))


def test_kiro_holds_only_for_a_spec_that_reads_the_project_file(home, tmp_path, monkeypatch):
    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, KIRO_PROJECT_MCP_FILE, {"planted": _PLANTED, "mine": _EXISTING})
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"claude-only": _PLANTED})

    _spec(monkeypatch, {"name": "a", "includeMcpJson": False})
    assert holds.held_project_servers(ACP_BACKEND_KIRO, "a", folder) == []

    # A server the spec defines itself is the spec's entry, not the file's.
    _spec(monkeypatch, {"name": "a", "includeMcpJson": True, "mcpServers": {"mine": {}}})
    assert _names(holds.held_project_servers(ACP_BACKEND_KIRO, "a", folder)) == ["planted"]
    assert _names(holds.held_project_servers(ACP_BACKEND_KAS, "a", folder)) == ["planted"]


def test_claude_holds_the_mcp_json_servers_only(home, tmp_path):
    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, KIRO_PROJECT_MCP_FILE, {"kiro-only": _PLANTED})
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"planted": _PLANTED})
    assert _names(holds.held_project_servers("claude", "a", folder)) == ["planted"]
    assert holds.held_project_servers("codex", "a", folder) == []


def test_a_held_name_gets_a_server_that_starts_nothing(home, tmp_path):
    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, KIRO_PROJECT_MCP_FILE, {"planted": _PLANTED, "kirocrew-core": _PLANTED})
    held = approvals.pending_servers(folder)
    crew_own = {"name": "kirocrew-core", "command": "crew", "args": [], "env": []}

    out = holds.with_held_servers([crew_own], held, "a", folder)
    assert out[0] is crew_own  # Kiro Crew's own element already outranks the file
    [added] = [e for e in out if e["name"] == "planted"]
    from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, decode_target_args

    [packed] = [a for a in added["args"] if a.startswith(STUB_FLAGS_FLAG + "=")]
    flags = decode_target_args(packed.split("=", 1)[1])
    assert "--refuse" in flags and flags[flags.index("--server") + 1] == "planted"
    assert "planted.js" not in json.dumps(added)
    assert len(out) == 2


def _card_params(server: approvals.ProjectServer) -> dict[str, str]:
    return {
        "folder": server.folder,
        "file": server.file,
        "server": server.name,
        "fingerprint": server.fingerprint,
    }


def test_the_card_shows_the_file_and_the_launch_and_applies_through_the_route(home, tmp_path):
    from kiro_crew.change_card_project_mcp import build_preview, clean_params, server_snapshot

    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"planted": _PLANTED})
    [server] = approvals.pending_servers(folder)
    params = clean_params(_card_params(server))

    out: dict[str, Any] = {}
    build_preview(params, server_snapshot(params), out)
    assert out["title"] == "Start planted from this folder's .mcp.json?"
    labels = {row["label"]: row for row in out["changes"]}
    assert labels["File"]["after"] == str(folder.resolve() / CLAUDE_PROJECT_MCP_FILE)
    assert labels["Runs"]["after"] == "node planted.js"
    assert labels["Environment"]["add"]
    assert out["changes"][-1] == {
        "label": "Open chats",
        "after": "Allowing restarts every open chat so planted loads; history is kept.",
    }
    assert [s["path"] for s in out["apply"]] == ["/api/mcp/project-servers/approve"]

    approvals.approve(server.folder, server.file, server.name, server.fingerprint)
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"planted": {**_PLANTED, "args": ["evil.js"]}})
    [changed] = approvals.pending_servers(folder)
    params = clean_params(_card_params(changed))
    out = {}
    build_preview(params, server_snapshot(params), out)
    assert out["title"] == "planted changed in this folder's .mcp.json. Start it?"
    command = {row["label"]: row for row in out["changes"]}["Runs"]
    assert (command["before"], command["after"]) == ("node planted.js", "node evil.js")
    # An unchanged env says so; an entry with no headers shows no Headers row.
    rows = {row["label"]: row for row in out["changes"]}
    assert rows["Environment"] == {"label": "Environment", "after": "unchanged"}
    assert "Headers" not in rows

    # A card for a launch other than the one the file holds does not apply.
    with pytest.raises(CardCatalogError):
        build_preview(clean_params(_card_params(server)), server_snapshot(params), {})


@pytest.mark.parametrize(
    "bad",
    [
        {"folder": "relative"},
        {"file": "mcp.json"},
        {"server": ""},
        {"fingerprint": "XYZ"},
        {"extra": "x"},
    ],
)
def test_the_card_refuses_bad_params(bad):
    from kiro_crew.change_card_project_mcp import clean_params

    good = {"folder": "/abs", "file": ".mcp.json", "server": "s", "fingerprint": "a" * 64}
    with pytest.raises(CardCatalogError):
        clean_params({**good, **bad})


def test_an_agent_cannot_raise_the_card():
    from kiro_crew import change_card_catalog as catalog

    assert catalog.KIND_PROJECT_MCP_SERVER in catalog.GATEWAY_RESOLVED_KINDS
    assert catalog.offers_no_undo(catalog.KIND_PROJECT_MCP_SERVER, {})


def _request(body: Any) -> Request:
    from unittest.mock import AsyncMock, Mock

    from aiohttp import web

    return Mock(spec=web.Request, json=AsyncMock(return_value=body))


async def _owner(request, operation):
    return None


def _count_resets(monkeypatch) -> list[Any]:
    from kiro_crew.dashboard.handlers import sessions

    resets: list[Any] = []

    async def _reset(request):
        resets.append(request)
        return 1

    monkeypatch.setattr(sessions, "_reset_all_sessions", _reset)
    return resets


@pytest.mark.asyncio
async def test_the_route_records_only_the_launch_the_card_showed(home, tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import _shared
    from kiro_crew.dashboard.handlers.project_mcp_approve import api_mcp_project_server_approve

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", _owner)
    resets = _count_resets(monkeypatch)
    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, KIRO_PROJECT_MCP_FILE, {"planted": _PLANTED})
    [server] = approvals.pending_servers(folder)
    body = _card_params(server)

    bad = await api_mcp_project_server_approve(_request({**body, "file": "../x"}))
    assert bad.status == 400
    stale = await api_mcp_project_server_approve(_request({**body, "fingerprint": "0" * 64}))
    assert stale.status == 409
    assert _names(approvals.pending_servers(folder)) == ["planted"]
    assert resets == []

    done = await api_mcp_project_server_approve(_request(body))
    assert done.status == 200
    assert approvals.pending_servers(folder) == []
    assert len(resets) == 1  # open chats restart only once the record is written


@pytest.mark.asyncio
async def test_the_route_is_the_owners_only(home, monkeypatch):
    from aiohttp import web

    from kiro_crew.dashboard.handlers import _shared
    from kiro_crew.dashboard.handlers.project_mcp_approve import api_mcp_project_server_approve

    async def _deny(request, operation):
        return web.json_response({"error": "denied"}, status=403)

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", _deny)
    resp = await api_mcp_project_server_approve(_request({}))
    assert resp.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded", [False, True])
async def test_claude_names_held_servers_only_in_session_settings(home, tmp_path, excluded):
    from kiro_crew.acp.client import ClaudeAcpClient
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    client = ClaudeAcpClient(work_dir=folder, acp_backend=ACP_BACKEND_CLAUDE)
    client._claude_local_settings_excluded = excluded
    client._claude_inline_settings = json.loads(client._render_claude_settings_payload())
    assert "disabledMcpjsonServers" not in client._claude_inline_settings

    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"planted": _PLANTED, "weather": _EXISTING})
    assert "disabledMcpjsonServers" not in json.loads(client._render_claude_settings_payload())
    await client._prepare_claude_held_asks()
    options = client._harness_session_meta([])["claudeCode"]["options"]
    assert options["settings"]["disabledMcpjsonServers"] == ["planted", "weather"]
    assert ("settingSources" in options) is excluded

    [weather] = [s for s in approvals.pending_servers(folder) if s.name == "weather"]
    approvals.approve(weather.folder, weather.file, weather.name, weather.fingerprint)
    assert "disabledMcpjsonServers" not in json.loads(client._render_claude_settings_payload())
    await client._prepare_claude_held_asks()
    options = client._harness_session_meta([])["claudeCode"]["options"]
    assert options["settings"]["disabledMcpjsonServers"] == ["planted"]
    assert "disabledMcpjsonServers" not in client._claude_inline_settings


@pytest.mark.asyncio
async def test_claude_seed_stays_identical_after_a_held_server_is_approved(home, tmp_path):
    from kiro_crew.acp.client import ClaudeAcpClient
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

    folder = _folder(tmp_path, "repo")
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"weather": _EXISTING})
    owner = ClaudeAcpClient(work_dir=folder, acp_backend=ACP_BACKEND_CLAUDE)
    held_seed = owner._render_claude_settings_payload()
    assert "disabledMcpjsonServers" not in json.loads(held_seed)
    await owner._prepare_claude_held_asks()
    assert owner._harness_session_meta([])["claudeCode"]["options"]["settings"][
        "disabledMcpjsonServers"
    ] == ["weather"]

    [server] = approvals.pending_servers(folder)
    approvals.approve(server.folder, server.file, server.name, server.fingerprint)
    sibling = ClaudeAcpClient(work_dir=folder, acp_backend=ACP_BACKEND_CLAUDE)
    approved_seed = sibling._render_claude_settings_payload()
    assert "disabledMcpjsonServers" not in json.loads(approved_seed)
    assert approved_seed == held_seed
    await sibling._prepare_claude_held_asks()
    options = sibling._harness_session_meta([])["claudeCode"]["options"]
    assert "disabledMcpjsonServers" not in options.get("settings", {})


@pytest.mark.asyncio
async def test_a_new_chat_raises_one_card_per_held_server(home, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
    from kiro_crew.dashboard.change_cards import CardError
    from kiro_crew.dashboard.handlers import change_cards_propose
    from kiro_crew.dashboard.project_mcp_cards import offer_project_mcp_cards
    from kiro_crew.providers.acp import AcpProvider

    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"planted": _PLANTED, "dup": _EXISTING})
    raised: list[dict[str, Any]] = []

    async def _propose(state, slot_key, sk, *, kind, params, reason, **_kw):
        if params["server"] == "dup":
            raise CardError(409, "duplicate_card", "already waiting")
        raised.append({"slot": slot_key, "kind": kind, "reason": reason, **params})
        return {}

    monkeypatch.setattr(change_cards_propose, "propose_in_slot", _propose)
    client = AcpProvider(work_dir=str(folder), acp_backend=ACP_BACKEND_CLAUDE)
    slot = SimpleNamespace(key="chat-1", agent="a", project=str(folder))
    await offer_project_mcp_cards(object(), slot, client, "sk")

    [card] = raised
    assert (card["slot"], card["kind"], card["server"]) == (
        "chat-1",
        "mcp.project_server",
        "planted",
    )
    assert card["file"] == CLAUDE_PROJECT_MCP_FILE and len(card["fingerprint"]) == 64
    assert card["reason"] == "Held back: you haven't allowed this server in this folder yet."

    raised.clear()
    await offer_project_mcp_cards(object(), slot, SimpleNamespace(cwd=str(folder)), "sk")
    assert raised == []  # a client that names no backend raises nothing


@pytest.mark.parametrize("file", [KIRO_PROJECT_MCP_FILE, CLAUDE_PROJECT_MCP_FILE])
def test_the_first_unknown_folder_waits_for_approval(home, tmp_path, file):
    folder = _folder(tmp_path, "new-project")
    _write(folder, file, {"weather": _EXISTING})

    assert _names(approvals.pending_servers(folder)) == ["weather"]
    assert not approvals.store_path().exists()
    assert _names(approvals.pending_servers(folder)) == ["weather"]


@pytest.mark.parametrize("file", [KIRO_PROJECT_MCP_FILE, CLAUDE_PROJECT_MCP_FILE])
@pytest.mark.parametrize("names", [[f"server-{i}" for i in range(257)], ["s" * 300]])
def test_every_enabled_server_waits_for_approval(home, tmp_path, file, names):
    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, file, dict.fromkeys(names, _EXISTING))

    assert _names(approvals.pending_servers(folder, files=(file,))) == sorted(names)


@pytest.mark.parametrize("file", [KIRO_PROJECT_MCP_FILE, CLAUDE_PROJECT_MCP_FILE])
def test_surrogate_text_keeps_all_enabled_servers_pending(home, tmp_path, file):
    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, file, {"weather": _EXISTING, "unicode": {"command": "\ud800"}})

    pending = approvals.pending_servers(folder, files=(file,))
    assert _names(pending) == ["unicode", "weather"]
    assert all(len(server.fingerprint) == 64 for server in pending)


@pytest.mark.parametrize("file", [KIRO_PROJECT_MCP_FILE, CLAUDE_PROJECT_MCP_FILE])
def test_only_requested_files_are_fingerprinted(home, tmp_path, monkeypatch, file):
    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    other = CLAUDE_PROJECT_MCP_FILE if file == KIRO_PROJECT_MCP_FILE else KIRO_PROJECT_MCP_FILE
    _write(folder, file, {"weather": _EXISTING})
    _write(folder, other, {"other": {"command": "\ud800"}})
    real = approvals.fingerprint
    checked = []

    def fingerprint(entry):
        checked.append(entry)
        return real(entry)

    monkeypatch.setattr(approvals, "fingerprint", fingerprint)
    assert _names(approvals.pending_servers(folder, files=iter([file]))) == ["weather"]
    assert checked == [_EXISTING]


def test_an_unfingerprintable_entry_stays_held(home, tmp_path, monkeypatch):
    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"weather": _EXISTING, "unreadable": _PLANTED})
    real = approvals.fingerprint

    def fingerprint(entry):
        if entry == _PLANTED:
            raise ValueError("cannot fingerprint this entry")
        return real(entry)

    monkeypatch.setattr(approvals, "fingerprint", fingerprint)
    pending = approvals.pending_servers(folder)
    assert _names(pending) == ["unreadable", "weather"]
    [unreadable] = [server for server in pending if server.name == "unreadable"]
    with pytest.raises(approvals.ApprovalStale):
        approvals.approve(
            unreadable.folder, unreadable.file, unreadable.name, unreadable.fingerprint
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("decline", ["mode", "hooks", "path"])
async def test_a_declined_claude_seed_carries_project_holds_inline(
    home, tmp_path, monkeypatch, decline
):
    from kiro_crew.acp import client as acp_client
    from kiro_crew.acp.client import ClaudeAcpClient
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"weather": _EXISTING})
    local = folder / ".claude" / "settings.local.json"
    local.parent.mkdir()
    content = json.dumps({"hooks": {"PreToolUse": []}} if decline == "hooks" else {})
    local.write_text(content, encoding="utf-8")
    client = ClaudeAcpClient(
        work_dir=folder,
        acp_backend=ACP_BACKEND_CLAUDE,
        permission_mode="acceptEdits" if decline == "mode" else "default",
    )
    client._claude_adapter_disk_version = "0.94.0"
    if decline == "path":
        monkeypatch.setattr(acp_client, "_claude_settings_usable", lambda path: False)
    client._write_claude_local_settings()
    await client._prepare_claude_held_asks()

    assert not client._claude_settings_authored
    assert not client._claude_local_settings_excluded
    options = client._claude_session_meta()["claudeCode"]["options"]
    assert "settingSources" not in options
    assert options["settings"]["disabledMcpjsonServers"] == ["weather"]
    assert client._claude_session_needs_options()
    from kiro_crew.acp.transport_errors import AcpToolGateUnroutable

    with pytest.raises(AcpToolGateUnroutable):
        client._refuse_claude_session_below_floor_before_spawn("0.83.0")
    assert local.read_text(encoding="utf-8") == content

    [server] = approvals.pending_servers(folder)
    approvals.approve(server.folder, server.file, server.name, server.fingerprint)
    await client._prepare_claude_held_asks()
    options = client._claude_session_meta()["claudeCode"]["options"]
    assert "disabledMcpjsonServers" not in options.get("settings", {})


@pytest.mark.parametrize(
    "edit, reason",
    [
        ({"args": ["evil.js"]}, "Held back: its command changed since you allowed it here."),
        (
            {"env": {"TOKEN": "other"}},
            "Held back: its settings changed since you allowed it here.",
        ),
    ],
)
def test_a_changed_card_says_whether_the_command_changed(home, tmp_path, edit, reason):
    from kiro_crew.dashboard.project_mcp_cards import _reason

    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"planted": _PLANTED})
    [server] = approvals.pending_servers(folder)
    approvals.approve(server.folder, server.file, server.name, server.fingerprint)
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"planted": {**_PLANTED, **edit}})

    [changed] = approvals.pending_servers(folder)
    assert changed.status == approvals.STATUS_CHANGED
    assert _reason(changed) == reason


def test_a_changed_card_with_headers_lists_unchanged_headers(home, tmp_path):
    from kiro_crew.change_card_project_mcp import build_preview, clean_params, server_snapshot

    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    remote = {"url": "https://mcp.example.test/a", "headers": {"X-Team": "shop"}}
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"remote": remote})
    [server] = approvals.pending_servers(folder)
    approvals.approve(server.folder, server.file, server.name, server.fingerprint)
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"remote": {**remote, "url": "https://evil.test/"}})

    [changed] = approvals.pending_servers(folder)
    params = clean_params(_card_params(changed))
    out: dict[str, Any] = {}
    build_preview(params, server_snapshot(params), out)
    rows = {row["label"]: row for row in out["changes"]}
    assert rows["URL"]["after"] == "https://evil.test/"
    assert rows["Environment"]["after"] == "unchanged"
    assert rows["Headers"]["after"] == "unchanged"


def test_the_record_bounds_the_env_keys_it_stores_but_not_the_fingerprint(
    home, tmp_path, caplog, monkeypatch
):
    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    env = {f"K{i}": "v" for i in range(approvals._MAX_PAIR_KEYS + 5)}
    env["L" * (approvals._MAX_PAIR_KEY_CHARS + 1)] = "v"
    _write(folder, KIRO_PROJECT_MCP_FILE, {"big": {"command": "node", "env": env}})
    [server] = approvals.pending_servers(folder)
    with caplog.at_level("WARNING", logger=approvals.__name__):
        approvals.approve(server.folder, server.file, server.name, server.fingerprint)
        approvals.summary(server.entry)
    assert sum("exceeds" in r.getMessage() for r in caplog.records) == 2

    stored = json.loads(approvals.store_path().read_text(encoding="utf-8"))
    [kept] = stored["folders"].values()
    record = kept[KIRO_PROJECT_MCP_FILE]["big"]
    assert len(record["env"]) == approvals._MAX_PAIR_KEYS
    assert all(len(k) <= approvals._MAX_PAIR_KEY_CHARS for k in record["env"])
    assert approvals.pending_servers(folder) == []

    # A change to a key the record left out still changes the fingerprint.
    env["L" * (approvals._MAX_PAIR_KEY_CHARS + 1)] = "other"
    _write(folder, KIRO_PROJECT_MCP_FILE, {"big": {"command": "node", "env": env}})
    assert _names(approvals.pending_servers(folder)) == ["big"]


def test_applying_the_card_through_the_middleware_marks_it_applied(home, tmp_path, monkeypatch):
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from change_cards_helpers import SK, SLOT, FakeState, _card_headers, _fake_auth

    from kiro_crew.dashboard.change_cards import STATUS_APPLIED, CardStore
    from kiro_crew.dashboard.handlers import change_cards as routes
    from kiro_crew.dashboard.handlers.change_cards_propose import propose_in_slot
    from kiro_crew.dashboard.handlers.project_mcp_approve import api_mcp_project_server_approve

    resets = _count_resets(monkeypatch)
    monkeypatch.setattr(routes, "sel", lambda: SimpleNamespaceSel())
    approvals.pending_servers(_folder(tmp_path, "first"))
    folder = _folder(tmp_path, "repo")
    _write(folder, CLAUDE_PROJECT_MCP_FILE, {"planted": _PLANTED})
    [server] = approvals.pending_servers(folder)
    store = CardStore(tmp_path / "cards.json")
    state = FakeState(store)

    app = web.Application(middlewares=[_fake_auth, routes.change_card_middleware])
    app["state"] = state
    routes.register_change_card_routes(app)
    app.router.add_post("/api/mcp/project-servers/approve", api_mcp_project_server_approve)

    async def main():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            card = await propose_in_slot(
                state,
                SLOT,
                SK,
                kind="mcp.project_server",
                params=_card_params(server),
                reason="held",
            )
            [only] = card["plan"]["apply"]
            r = await client.request(
                only["method"], only["path"], json=only["body"], headers=_card_headers(card)
            )
            assert r.status == 200, await r.text()
            if routes._BACKGROUND:
                await asyncio.gather(*list(routes._BACKGROUND))
            return card
        finally:
            await client.close()

    card = asyncio.run(main())
    assert store.get(card["id"])["status"] == STATUS_APPLIED
    assert approvals.pending_servers(folder) == []
    assert len(resets) == 1


class SimpleNamespaceSel:
    """A security-event log that records nothing."""

    def __getattr__(self, _name: str) -> Any:
        return lambda *a, **k: None
