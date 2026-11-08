"""Change cards: the reader helpers behind an MCP, crewmate or schedule card's rows.

What a card shows and checks comes from the gateway's own reads of the current state.
These pin the small readers the card routes otherwise reach only through stubs: the
global MCP entry a server row reads (and its fallback to the discovered servers), a
scope file that cannot be decoded reading as unreadable, the registry facts an MCP
install card displays, the server names an MCP undo plan removes, a crewmate's display
label, a schedule's editable fields, and the preview of a cron expression that never
runs.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import change_card_catalog as catalog
from kiro_crew import mcp_discovery
from kiro_crew.config import loader
from kiro_crew.cron_service import schedule
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.handlers import mcp as mcp_handlers
from kiro_crew.dashboard.handlers import mcp_discover


def _discovered(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    servers = [SimpleNamespace(name=name) for name in names]
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda: servers)


def _global_mcp_json(monkeypatch: pytest.MonkeyPatch, path: Any) -> None:
    monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", path)


# ── the global MCP entry a server row reads ──


def test_a_server_in_the_global_mcp_json_is_read_from_there(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    entry = {"command": "uvx", "args": ["alpha-mcp"]}
    path.write_text(json.dumps({"mcpServers": {"alpha": entry}}), encoding="utf-8")
    _global_mcp_json(monkeypatch, path)
    _discovered(monkeypatch)

    assert cards._mcp_global_entry("alpha") == (True, entry)


def test_a_server_missing_from_the_file_exists_only_when_discovery_lists_it(tmp_path, monkeypatch):
    _global_mcp_json(monkeypatch, tmp_path / "absent.json")
    _discovered(monkeypatch, "beta")

    assert cards._mcp_global_entry("beta") == (True, None)
    assert cards._mcp_global_entry("gamma") == (False, None)


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"mcpServers": ["alpha"]}'])
def test_an_unreadable_global_file_falls_back_to_the_discovered_servers(
    tmp_path, monkeypatch, text
):
    path = tmp_path / "mcp.json"
    path.write_text(text, encoding="utf-8")
    _global_mcp_json(monkeypatch, path)
    _discovered(monkeypatch, "alpha")

    assert cards._mcp_global_entry("alpha") == (True, None)


def test_an_ambiguous_server_key_falls_back_to_the_discovered_servers(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"alpha": {"command": "x"}}}), encoding="utf-8")
    _global_mcp_json(monkeypatch, path)
    _discovered(monkeypatch)

    def ambiguous(servers: Any, name: str) -> str:
        raise ValueError(f"{name} names two keys")

    monkeypatch.setattr(mcp_handlers, "_config_key_for", ambiguous)

    assert cards._mcp_global_entry("alpha") == (False, None)


def test_a_server_exists_when_any_scope_holds_its_spec(monkeypatch):
    specs = {"alpha": {"command": "x"}}
    monkeypatch.setattr(mcp_handlers, "_find_server_spec_anywhere", specs.get)

    assert cards._mcp_exists("alpha") is True
    assert cards._mcp_exists("beta") is False


def test_a_scope_file_that_is_not_utf8_is_unreadable_not_an_error(tmp_path, monkeypatch):
    # A hand-edited mcp.json saved in a legacy code page: the 0xe9 is a
    # latin-1 "é" that UTF-8 cannot decode.
    legacy, readable = tmp_path / "legacy.json", tmp_path / "readable.json"
    legacy.write_bytes(b'{"mcpServers": {"srv": {"command": "caf\xe9"}}}')
    readable.write_text(json.dumps({"mcpServers": {"srv": {"command": "x"}}}), encoding="utf-8")
    monkeypatch.setattr(
        mcp_handlers,
        "_uninstall_scope_files",
        lambda: [("kiroGlobal", legacy), ("kirocrew", readable)],
    )
    monkeypatch.setattr(cards, "_mcp_value_key", lambda: b"k" * 32)

    try:
        scopes = cards._mcp_scopes("srv")
    except UnicodeDecodeError as exc:
        pytest.fail(f"an undecodable scope raised instead of reading as unreadable: {exc}")

    assert scopes["kiroGlobal"] == "unreadable"
    assert scopes["kirocrew"] not in ("", "unreadable")


# ── the server names an MCP undo plan removes ──


def test_the_undo_names_are_every_named_change_in_the_undo_steps():
    rec = {
        "plan": {
            "undo": [
                {"body": {"changes": [{"name": "alpha"}, {"name": 7}, "beta"]}},
                {"body": {}},
                {},
                {"body": {"changes": [{"name": "gamma"}]}},
            ]
        }
    }

    assert cards._mcp_undo_names(rec) == ["alpha", "gamma"]
    assert cards._mcp_undo_names({}) == []
    assert cards._mcp_undo_names({"plan": None}) == []


# ── the registry facts an MCP install card displays ──


class _Registry:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def get(self, name: str) -> Any:
        return self._provider if name == "hub" else None


class _Provider:
    def __init__(self, detail: Any, *, available: bool = True) -> None:
        self._detail = detail
        self._available = available

    def is_available(self) -> bool:
        return self._available

    async def fetch_detail(self, server_id: str) -> Any:
        return self._detail


def _registry(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    monkeypatch.setattr(mcp_discover, "_get_registry", lambda: _Registry(provider))


def _detail(spec: Any, **facts: Any) -> SimpleNamespace:
    return SimpleNamespace(install_plan=SimpleNamespace(spec=spec), **facts)


def test_an_install_detail_shows_the_command_and_its_arguments(monkeypatch):
    detail = _detail(
        {"command": "npx", "args": ["-y", "@scope/server", 3]},
        version="1.2.3",
        repo_url="https://example.com/" + "r" * 300,
    )
    _registry(monkeypatch, _Provider(detail))

    out = asyncio.run(cards._mcp_detail({"provider": "hub", "id": "scope/server"}))

    assert out["command"] == "npx -y @scope/server 3"
    assert out["version"] == "1.2.3"
    assert out["repo_url"] == ("https://example.com/" + "r" * 300)[:200]


def test_an_install_detail_shows_a_remote_server_by_its_url(monkeypatch):
    detail = _detail({"url": "https://mcp.example.com/sse", "command": "ignored"}, version="")
    _registry(monkeypatch, _Provider(detail))

    out = asyncio.run(cards._mcp_detail({"provider": "hub", "id": "remote"}))

    assert out == {"command": "https://mcp.example.com/sse"}


def test_an_install_detail_without_a_spec_shows_only_its_facts(monkeypatch):
    detail = SimpleNamespace(install_plan=None, version="2.0", repo_url=None)
    _registry(monkeypatch, _Provider(detail))

    assert asyncio.run(cards._mcp_detail({"provider": "hub", "id": "plain"})) == {"version": "2.0"}


@pytest.mark.parametrize("provider_name", ["hub", "other"])
def test_an_unreachable_registry_shows_nothing(monkeypatch, provider_name):
    _registry(monkeypatch, _Provider(_detail({"command": "x"}), available=False))

    out = asyncio.run(cards._mcp_detail({"provider": provider_name, "id": "x"}))

    assert out == {}


def test_a_registry_that_fails_to_load_shows_nothing(monkeypatch):
    def broken() -> Any:
        raise OSError("registry offline")

    monkeypatch.setattr(mcp_discover, "_get_registry", broken)

    assert asyncio.run(cards._mcp_detail({"provider": "hub", "id": "x"})) == {}


def test_a_server_the_registry_does_not_know_is_refused(monkeypatch):
    _registry(monkeypatch, _Provider(None))

    with pytest.raises(catalog.CardCatalogError) as caught:
        asyncio.run(cards._mcp_detail({"provider": "hub", "id": "missing"}))

    assert caught.value.code == "not_found"
    assert "missing" in caught.value.message


# ── a crewmate's label and a schedule's fields ──


def test_a_crewmate_is_labelled_by_its_display_name_else_its_key(monkeypatch):
    agents = {
        "ada": SimpleNamespace(display_name="  Ada Lovelace  "),
        "bob": SimpleNamespace(display_name="   "),
        "cy": SimpleNamespace(display_name=None),
    }
    monkeypatch.setattr(loader.KiroCrewConfig, "load", lambda: SimpleNamespace(agents=agents))

    assert cards._crewmate_label("ada") == "Ada Lovelace"
    assert cards._crewmate_label("bob") == "bob"
    assert cards._crewmate_label("cy") == "cy"
    assert cards._crewmate_label("zed") == "zed"


def test_a_schedule_card_reads_the_fields_a_person_edits():
    job = SimpleNamespace(
        name="nightly",
        message="summarize",
        schedule=SimpleNamespace(cron_expr="0 3 * * *"),
        timezone=None,
    )

    assert cards._cron_job_fields(job) == {
        "name": "nightly",
        "message": "summarize",
        "cron_expr": "0 3 * * *",
        "timezone": "",
    }
    assert cards._cron_job_fields(SimpleNamespace()) == {
        "name": None,
        "message": None,
        "cron_expr": None,
        "timezone": "",
    }


def test_a_cron_expression_whose_next_run_cannot_be_computed_previews_none(monkeypatch):
    def never(job: Any) -> float:
        raise ValueError(f"{job.schedule.cron_expr} never runs")

    monkeypatch.setattr(schedule, "compute_next_run_ts", never)

    assert cards.next_run_at("0 0 31 2 *", "UTC") is None
