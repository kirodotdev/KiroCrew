"""The owner sign-in record and the ``ownerSignIn`` flag it stamps on MCP rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.connections import owner_servers

_URL = "https://docs.example.com/mcp"


def test_only_a_recorded_name_at_its_recorded_url_is_an_owner_server():
    cfg = {"connections": {"owner_mcp_servers": {"docs": {"url": _URL}}}}

    assert owner_servers.is_owner_server(cfg, "docs", {"url": _URL})
    assert not owner_servers.is_owner_server(cfg, "docs", {"url": "https://other.example.com/mcp"})
    assert not owner_servers.is_owner_server(cfg, "agentServer", {"url": _URL})
    assert not owner_servers.is_owner_server(cfg, "docs", {"command": "docs-bin"})
    assert not owner_servers.is_owner_server({}, "docs", {"url": _URL})
    assert not owner_servers.is_owner_server(None, "docs", {"url": _URL})


def test_record_writes_remote_entries_and_drops_one_that_turned_local():
    cfg: dict = {"connections": {"owner_mcp_servers": {"docs": {"url": _URL}}}}

    out = owner_servers.record(
        cfg, {"docs": {"command": "docs-bin"}, "wiki": {"url": "https://wiki.example.com/mcp"}}
    )

    assert out is not None
    assert out["connections"]["owner_mcp_servers"] == {
        "wiki": {"url": "https://wiki.example.com/mcp"}
    }


def test_record_and_forget_write_nothing_when_nothing_changes():
    cfg: dict = {"connections": {"owner_mcp_servers": {"docs": {"url": _URL}}}}

    assert owner_servers.record(cfg, {"docs": {"url": _URL}}) is None
    assert owner_servers.forget(cfg, ["absent"]) is None
    assert owner_servers.forget({}, ["docs"]) is None
    assert owner_servers.forget(cfg, ["docs"]) is not None
    assert cfg["connections"]["owner_mcp_servers"] == {}


def test_record_keeps_other_connections_settings():
    cfg: dict = {"connections": {"oauth_clients": {"github": {"client_id": "x"}}}}

    owner_servers.record(cfg, {"docs": {"url": _URL}})

    assert cfg["connections"]["oauth_clients"] == {"github": {"client_id": "x"}}


def test_the_row_flag_is_true_only_for_the_owner_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"connections": {"owner_mcp_servers": {"docs": {"url": _URL}}}}),
        encoding="utf-8",
    )
    rows = [
        {"name": "docs", "url": _URL},
        {"name": "agentServer", "url": "https://agent.example.com/mcp"},
        {"name": "docs-moved", "url": _URL},
        # A cached row that once read True is rewritten, never left stale.
        {"name": "wiki", "url": "https://wiki.example.com/mcp", "ownerSignIn": True},
    ]

    mcp_mod._annotate_owner_sign_in(rows)

    assert [row["ownerSignIn"] for row in rows] == [True, False, False, False]


def test_the_row_flag_is_false_for_a_name_the_mint_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # A recorded server whose name is a registry slug, or one the agent spec
    # rewrites, is refused by the mint, so its row is never offered a sign-in.
    from kiro_crew.connections.registry import get_all_providers
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    slug = get_all_providers()[0]["slug"]
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps(
            {"connections": {"owner_mcp_servers": {slug: {"url": _URL}, "a/b": {"url": _URL}}}}
        ),
        encoding="utf-8",
    )
    rows = [{"name": slug, "url": _URL}, {"name": "a/b", "url": _URL}]

    mcp_mod._annotate_owner_sign_in(rows)

    assert [row["ownerSignIn"] for row in rows] == [False, False]


@pytest.mark.parametrize("url", ["https://[", "https://docs.example.com/" + "a" * 4096, 42, ""])
def test_a_malformed_or_oversized_url_is_never_a_remote_url(url):
    assert owner_servers.remote_url({"url": url}) is None
    cfg: dict = {}
    assert owner_servers.record(cfg, {"docs": {"url": url}}) is None


def test_record_stops_at_the_cap_and_keeps_updating_existing_names(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(owner_servers, "MAX_RECORDS", 1)
    cfg: dict = {}
    owner_servers.record(cfg, {"docs": {"url": _URL}})

    assert owner_servers.record(cfg, {"wiki": {"url": "https://wiki.example.com/mcp"}}) is None
    owner_servers.record(cfg, {"docs": {"url": "https://docs.example.com/v2"}})

    assert cfg["connections"]["owner_mcp_servers"] == {
        "docs": {"url": "https://docs.example.com/v2"}
    }


def test_an_unreadable_record_clears_a_cached_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    rows = [{"name": "docs", "url": _URL, "ownerSignIn": True}]

    mcp_mod._annotate_owner_sign_in(rows)

    assert rows[0]["ownerSignIn"] is False


def test_a_malformed_url_row_does_not_break_the_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"connections": {"owner_mcp_servers": {"docs": {"url": _URL}}}}),
        encoding="utf-8",
    )
    rows = [{"name": "broken", "url": "https://["}, {"name": "docs", "url": _URL}]

    mcp_mod._annotate_owner_sign_in(rows)

    assert [row["ownerSignIn"] for row in rows] == [False, True]
