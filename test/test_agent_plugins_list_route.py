"""The owner-only, read-only ``GET /api/agent-plugins`` list.

Every installed plugin and Kiro Power is listed with its state and reason, and
none is loaded: a ``POWER.md`` Power is ``not-a-plugin`` with ``legacy_power``
set, a valid plugin is ``off``, and every record says ``loaded: false``. The
route has no write half, reading it changes nothing on disk, and anyone but the
dashboard owner is refused, with the refusal written to the security event log.
"""

from __future__ import annotations

import asyncio
import builtins
import errno
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agent_plugins_helpers import install, mcp, skill

from kiro_crew.agent_plugins import manifest as manifest_module
from kiro_crew.agent_plugins import mcp_config as mcp_config_module
from kiro_crew.dashboard.handlers import agent_plugins_list as handler

OWNER = "owner-1"


@pytest.fixture
def powers(tmp_path, monkeypatch):
    home = tmp_path / "powers"
    crew = tmp_path / "crew"
    crew.mkdir()
    monkeypatch.setenv("KIRO_POWERS_HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    return home


@pytest.fixture
def audit(monkeypatch):
    """Capture the SEL rows the handler writes, so a refusal test never writes the real log."""
    import kiro_crew.dashboard.handlers as handlers_pkg

    rows: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: rows.append(kw)
    monkeypatch.setattr(handlers_pkg, "sel", lambda: fake)
    return rows


def _req(*, app: str = "", user: str = OWNER):
    req = MagicMock()
    req.path = "/api/agent-plugins"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = OWNER
    req.app = {"state": state}
    return req


def _list(req) -> tuple[int, dict]:
    resp = asyncio.run(handler.api_agent_plugins_get(req))
    return resp.status, json.loads(resp.text)


def _refuse_open_in(monkeypatch, module, folder: str) -> None:
    """Make *module*'s ``open`` fail inside ``installed/<folder>/`` as a mode-000 file does.

    Patched where the file is read rather than made with chmod, because root and
    Windows read a mode-000 file anyway and the test would pass there for nothing.
    """

    def refusing(file, *args, **kwargs):
        path = os.fspath(file)
        if os.path.basename(os.path.dirname(path)) == folder:
            raise PermissionError(errno.EACCES, "Permission denied", path)
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(module, "open", refusing, raising=False)


def _tree(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


@pytest.mark.parametrize(
    "who",
    [
        pytest.param({"app": "notes"}, id="app-token"),
        pytest.param({"user": "slack-guest"}, id="non-owner-messaging-user"),
        pytest.param({"user": ""}, id="unauthenticated"),
    ],
)
def test_anyone_but_the_owner_is_refused_and_audited(powers, audit, who):
    install(powers, "demo")
    status, body = _list(_req(**who))
    assert status == 403
    assert body["code"] == "dashboard_owner_required"
    assert "plugins" not in body
    (row,) = audit
    assert (row["operation"], row["outcome"], row["source"]) == (
        "agent_plugins.list",
        "denied",
        "dashboard",
    )
    assert row["caller"] == (who.get("user", OWNER) or "anonymous")
    assert row["resources"] == "/api/agent-plugins"


def test_a_failed_audit_still_refuses(powers, monkeypatch):
    import kiro_crew.dashboard.handlers as handlers_pkg

    def broken():
        raise OSError("log unavailable")

    monkeypatch.setattr(handlers_pkg, "sel", broken)
    status, body = _list(_req(user="slack-guest"))
    assert (status, body["code"]) == (403, "dashboard_owner_required")


def test_the_owners_read_writes_no_refusal(powers, audit):
    install(powers, "demo")
    status, _body = _list(_req())
    assert status == 200
    assert audit == []


def test_a_power_md_power_is_listed_as_not_loaded_with_its_reason(powers):
    install(powers, "legacy", manifest="", **{"POWER.md": "---\nname: legacy\n---\nBody.\n"})
    status, body = _list(_req())
    assert status == 200
    (rec,) = body["plugins"]
    assert rec["name"] == "legacy"
    assert rec["state"] == "not-a-plugin"
    assert rec["legacy_power"] is True
    assert rec["loaded"] is False
    assert "POWER.md" in rec["reason"]
    assert rec["skills"] == [] and rec["servers"] == []


def test_every_state_is_listed_and_nothing_is_loaded(powers):
    install(
        powers,
        "good",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "good",
            "version": "1.2.0",
            "description": "A valid plugin.",
        },
        **{
            "skills__good-skill__SKILL.md": skill("good-skill", "Helps."),
            "mcp.json": mcp(
                {
                    "local": {"type": "stdio", "command": "node", "args": ["server.js"]},
                    "remote": {"type": "streamable-http", "url": "https://example.com/mcp"},
                }
            ),
        },
    )
    install(powers, "broken", manifest="{not json")
    install(powers, "bare", manifest="", **{"README.md": "nothing here"})
    install(powers, "old", manifest="", **{"POWER.md": "---\nname: old\n---\n"})

    status, body = _list(_req())
    assert status == 200
    by_name = {p["name"]: p for p in body["plugins"]}
    assert [p["name"] for p in body["plugins"]] == ["bare", "broken", "good", "old"]
    assert {p["name"]: p["loaded"] for p in body["plugins"]} == dict.fromkeys(by_name, False)

    good = by_name["good"]
    assert (good["state"], good["reason"], good["legacy_power"]) == ("off", None, False)
    assert (good["version"], good["description"]) == ("1.2.0", "A valid plugin.")
    assert good["skills"] == [{"name": "good-skill", "description": "Helps."}]
    assert sorted(good["servers"], key=lambda s: s["name"]) == [
        {"name": "local", "type": "stdio", "command": "node"},
        {"name": "remote", "type": "streamable-http", "url": "https://example.com/mcp"},
    ]

    assert by_name["broken"]["state"] == "invalid" and by_name["broken"]["reason"]
    assert by_name["broken"]["legacy_power"] is False
    assert (by_name["bare"]["state"], by_name["bare"]["legacy_power"]) == ("not-a-plugin", False)
    assert by_name["old"]["legacy_power"] is True
    assert {p["name"]: p["failed"] for p in body["plugins"]} == dict.fromkeys(by_name, False)
    assert body["index_error"] is None


def test_a_failed_read_is_marked_and_a_broken_rule_is_not(powers, monkeypatch):
    install(powers, "locked-manifest")
    install(powers, "broken", manifest="{not json")
    install(powers, "locked-mcp", **{"mcp.json": mcp({"v": {"type": "stdio", "command": "node"}})})
    install(powers, "bad-mcp", **{"mcp.json": "{"})
    _refuse_open_in(monkeypatch, manifest_module, "locked-manifest")
    _refuse_open_in(monkeypatch, mcp_config_module, "locked-mcp")

    status, body = _list(_req())
    assert status == 200
    by_name = {p["name"]: p for p in body["plugins"]}
    locked = by_name["locked-manifest"]
    assert (locked["state"], locked["failed"]) == ("invalid", True)
    assert locked["reason"] == "plugin.json could not be read: PermissionError"
    assert locked["issues"] == [
        {"component": "plugin", "entry": None, "message": locked["reason"], "failed": True}
    ]
    assert (by_name["broken"]["state"], by_name["broken"]["failed"]) == ("invalid", False)
    assert [i["failed"] for i in by_name["broken"]["issues"]] == [False]

    assert (by_name["locked-mcp"]["state"], by_name["locked-mcp"]["failed"]) == ("off", False)
    assert by_name["locked-mcp"]["issues"] == [
        {
            "component": "mcp",
            "entry": None,
            "message": "mcp.json could not be read; MCP is off",
            "failed": True,
        }
    ]
    assert [i["failed"] for i in by_name["bad-mcp"]["issues"]] == [False]


def test_no_installed_index_lists_nothing(powers):
    status, body = _list(_req())
    assert (status, body) == (200, {"plugins": [], "index_error": None})


def test_an_unreadable_index_is_reported_not_listed_as_nothing_installed(powers, monkeypatch):
    install(powers, "demo")
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.name == "installed.json":
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    status, body = _list(_req())
    assert (status, body) == (
        200,
        {"plugins": [], "index_error": "installed.json could not be read: PermissionError"},
    )


def test_listing_writes_nothing(powers, tmp_path):
    install(powers, "good", **{"skills__s__SKILL.md": skill("s")})
    install(powers, "old", manifest="", **{"POWER.md": "x"})
    before_powers, before_crew = _tree(powers), _tree(tmp_path / "crew")
    _list(_req())
    assert _tree(powers) == before_powers
    assert _tree(tmp_path / "crew") == before_crew
    assert not (powers / "data").exists()


def test_the_route_is_read_only_on_the_dashboard():
    from aiohttp import web

    from kiro_crew.dashboard.routes import system

    app = web.Application()
    system.register(app)
    methods = {
        r.method
        for r in app.router.routes()
        if r.resource is not None and r.resource.canonical == "/api/agent-plugins"
    }
    assert methods == {"GET", "HEAD"}
