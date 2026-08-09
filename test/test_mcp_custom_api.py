"""Tests for the manual MCP server management handlers.

Covers the wire contract of ``POST /api/mcp/custom`` (add custom servers)
and ``PUT /api/mcp/custom/{name}`` (replace a spec): the validation
matrix, all-or-nothing batch writes, collision refusal, the consent
default (disabled unless ``enable: true``), enabled-state preservation
on edit, and SEL audit logging.  No network, no real home directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

_STDIO = {"command": "npx", "args": ["-y", "@acme/weather-mcp"], "env": {"KEY": ""}}
_REMOTE = {"url": "https://mcp.example.com/sse"}


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Pin $HOME to tmp_path so all config paths resolve into a sandbox."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def sandbox(fake_home, monkeypatch):
    """Sandbox the mcp.py module-level config paths + collaborator seams."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", fake_home / "kirocrew.mcp.json")
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", fake_home / "kiro.mcp.json")
    monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", fake_home / "kiro.mcp.lock")

    import kiro_crew.agent as agent_mod

    rebuild = MagicMock()
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)

    return SimpleNamespace(
        home=fake_home,
        kirocrew_json=fake_home / "kirocrew.mcp.json",
        rebuild=rebuild,
        mcp_mod=mcp_mod,
    )


@pytest.fixture
def fake_sel(monkeypatch):
    """Capture SEL calls made by the custom handlers."""
    from kiro_crew.dashboard.handlers import mcp_custom as mod

    instance = MagicMock()
    monkeypatch.setattr(mod, "sel", lambda: instance)
    return instance


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers import mcp_custom as mod

    app = web.Application()
    app["state"] = MagicMock()
    app.router.add_post("/api/mcp/custom", mod.api_mcp_custom_add)
    app.router.add_get("/api/mcp/custom/{name}", mod.api_mcp_custom_get)
    app.router.add_put("/api/mcp/custom/{name}", mod.api_mcp_custom_update)
    return app


async def _client() -> TestClient:
    client = TestClient(TestServer(_make_app()))
    await client.start_server()
    return client


def _written(sandbox) -> dict:
    """The mcpServers block currently on disk (empty when never written)."""
    if not sandbox.kirocrew_json.exists():
        return {}
    return json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8")).get("mcpServers", {})


# ---------------------------------------------------------------------------
# POST /api/mcp/custom — add
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCustomAdd:
    async def test_stdio_add_lands_disabled_by_default(self, sandbox, fake_sel):
        client = await _client()
        try:
            resp = await client.post("/api/mcp/custom", json={"servers": {"weather": _STDIO}})
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True, "added": ["weather"], "enabled": False}
            entry = _written(sandbox)["weather"]
            assert entry["command"] == "npx"
            assert entry["args"] == ["-y", "@acme/weather-mcp"]
            assert entry["env"] == {"KEY": ""}
            # Consent default: never enabled without the explicit flag.
            assert entry["disabled"] is True
            assert sandbox.rebuild.called
        finally:
            await client.close()

    async def test_enable_flag_is_honored(self, sandbox, fake_sel):
        client = await _client()
        try:
            resp = await client.post(
                "/api/mcp/custom", json={"servers": {"weather": _STDIO}, "enable": True}
            )
            assert resp.status == 200
            assert (await resp.json())["enabled"] is True
            assert "disabled" not in _written(sandbox)["weather"]
        finally:
            await client.close()

    async def test_remote_url_spec(self, sandbox, fake_sel):
        client = await _client()
        try:
            resp = await client.post("/api/mcp/custom", json={"servers": {"remote": _REMOTE}})
            assert resp.status == 200
            entry = _written(sandbox)["remote"]
            assert entry["url"] == _REMOTE["url"]
            assert entry["disabled"] is True
        finally:
            await client.close()

    async def test_remote_spec_keeps_scopes_and_client_id(self, sandbox, fake_sel):
        """A Connect writes the card's promised access; the entry must carry it."""
        spec = {
            "url": "https://api.githubcopilot.com/mcp/",
            "scopes": ["read:user", "read:org"],
            "clientId": "public-client-id",
        }
        client = await _client()
        try:
            resp = await client.post("/api/mcp/custom", json={"servers": {"github": spec}})
            assert resp.status == 200
            entry = _written(sandbox)["github"]
            assert entry["scopes"] == ["read:user", "read:org"]
            assert entry["clientId"] == "public-client-id"
        finally:
            await client.close()

    async def test_remote_spec_drops_empty_oauth_hints(self, sandbox, fake_sel):
        """Empty optionals are dropped, matching args/env — no noise keys on disk."""
        spec = {"url": "https://mcp.example.com/sse", "scopes": [], "clientId": ""}
        client = await _client()
        try:
            resp = await client.post("/api/mcp/custom", json={"servers": {"remote": spec}})
            assert resp.status == 400, "an empty clientId is malformed, not an omission"
            spec.pop("clientId")
            resp = await client.post("/api/mcp/custom", json={"servers": {"remote": spec}})
            assert resp.status == 200
            entry = _written(sandbox)["remote"]
            assert "scopes" not in entry
            assert "clientId" not in entry
        finally:
            await client.close()

    async def test_multi_add_writes_all(self, sandbox, fake_sel):
        client = await _client()
        try:
            resp = await client.post(
                "/api/mcp/custom", json={"servers": {"a": _STDIO, "b": _REMOTE}}
            )
            assert resp.status == 200
            assert (await resp.json())["added"] == ["a", "b"]
            assert set(_written(sandbox)) == {"a", "b"}
        finally:
            await client.close()

    async def test_one_bad_entry_fails_whole_batch(self, sandbox, fake_sel):
        client = await _client()
        try:
            resp = await client.post(
                "/api/mcp/custom",
                json={"servers": {"good": _STDIO, "bad": {"command": ""}}},
            )
            assert resp.status == 400
            assert "bad" in (await resp.json())["error"]
            # Nothing written — all-or-nothing.
            assert _written(sandbox) == {}
        finally:
            await client.close()

    async def test_collision_409_lists_conflicts_and_writes_nothing(self, sandbox, fake_sel):
        sandbox.kirocrew_json.write_text(json.dumps({"mcpServers": {"weather": dict(_STDIO)}}))
        client = await _client()
        try:
            resp = await client.post(
                "/api/mcp/custom", json={"servers": {"weather": _REMOTE, "fresh": _REMOTE}}
            )
            assert resp.status == 409
            body = await resp.json()
            assert body["conflicts"] == ["weather"]
            assert "fresh" not in _written(sandbox)
            outcome = fake_sel.log_api_access.call_args.kwargs["outcome"]
            assert outcome == "denied"
        finally:
            await client.close()

    async def test_sel_logged_on_success(self, sandbox, fake_sel):
        client = await _client()
        try:
            await client.post("/api/mcp/custom", json={"servers": {"weather": _STDIO}})
            kwargs = fake_sel.log_api_access.call_args.kwargs
            assert kwargs["operation"] == "mcp_custom_add"
            assert kwargs["outcome"] == "ok"
            assert "weather" in kwargs["resources"]
        finally:
            await client.close()

    @pytest.mark.parametrize(
        "spec,fragment",
        [
            ({"command": "npx", "url": "https://x.example"}, "both"),
            ({}, "needs"),
            ({"command": "npx", "cwd": "/tmp"}, "unknown spec key 'cwd'"),
            ({"command": 42}, "non-empty string"),
            ({"command": "npx", "args": ["ok", 7]}, "list of strings"),
            ({"command": "npx", "env": {"K": 1}}, "string values"),
            ({"url": "ftp://mcp.example.com"}, "http(s)"),
            ({"url": "https://x.example", "env": {"K": "v"}}, "not valid on a remote"),
            ({"url": "https://x.example", "scopes": "read"}, "list of non-empty strings"),
            ({"url": "https://x.example", "scopes": ["read", 7]}, "list of non-empty strings"),
            ({"url": "https://x.example", "scopes": ["read", ""]}, "list of non-empty strings"),
            ({"url": "https://x.example", "clientId": ""}, "'clientId' must be a non-empty"),
            ({"url": "https://x.example", "clientId": "   "}, "'clientId' must be a non-empty"),
            ({"url": "https://x.example", "clientId": 42}, "'clientId' must be a non-empty"),
            ({"command": "npx", "scopes": ["read"]}, "not valid on a stdio"),
            ({"command": "npx", "clientId": "public-id"}, "not valid on a stdio"),
            ("npx -y thing", "must be an object"),
        ],
    )
    async def test_invalid_spec_matrix_400(self, sandbox, fake_sel, spec, fragment):
        client = await _client()
        try:
            resp = await client.post("/api/mcp/custom", json={"servers": {"s": spec}})
            assert resp.status == 400
            assert fragment in (await resp.json())["error"]
            assert _written(sandbox) == {}
        finally:
            await client.close()

    @pytest.mark.parametrize(
        "body,fragment",
        [
            ({"servers": {}}, "non-empty"),
            ({"servers": []}, "non-empty"),
            ({}, "non-empty"),
            ({"servers": {"ok name": _STDIO}}, "invalid server name"),
            ({"servers": {"../evil": _STDIO}}, "invalid server name"),
            ({"servers": {"s": _STDIO}, "enable": "yes"}, "boolean"),
        ],
    )
    async def test_invalid_body_matrix_400(self, sandbox, fake_sel, body, fragment):
        client = await _client()
        try:
            resp = await client.post("/api/mcp/custom", json=body)
            assert resp.status == 400
            assert fragment in (await resp.json())["error"]
        finally:
            await client.close()

    async def test_non_json_body_400(self, sandbox, fake_sel):
        client = await _client()
        try:
            resp = await client.post("/api/mcp/custom", data=b"not json")
            assert resp.status == 400
        finally:
            await client.close()

    async def test_too_many_servers_400(self, sandbox, fake_sel):
        client = await _client()
        try:
            servers = {f"s{i}": dict(_REMOTE) for i in range(21)}
            resp = await client.post("/api/mcp/custom", json={"servers": servers})
            assert resp.status == 400
            assert "too many" in (await resp.json())["error"]
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# PUT /api/mcp/custom/{name} — edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCustomUpdate:
    async def test_replaces_spec_and_preserves_disabled_state(self, sandbox, fake_sel):
        entry = dict(_STDIO, disabled=True)
        sandbox.kirocrew_json.write_text(json.dumps({"mcpServers": {"weather": entry}}))
        client = await _client()
        try:
            resp = await client.put("/api/mcp/custom/weather", json={"spec": _REMOTE})
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "name": "weather"}
            written = _written(sandbox)["weather"]
            assert written["url"] == _REMOTE["url"]
            assert "command" not in written  # old stdio keys fully replaced
            assert written["disabled"] is True  # edit is not consent to run
            assert sandbox.rebuild.called
        finally:
            await client.close()

    async def test_enabled_server_stays_enabled(self, sandbox, fake_sel):
        sandbox.kirocrew_json.write_text(json.dumps({"mcpServers": {"weather": dict(_STDIO)}}))
        client = await _client()
        try:
            new_spec = dict(_STDIO, args=["-y", "@acme/weather-mcp@2"])
            resp = await client.put("/api/mcp/custom/weather", json={"spec": new_spec})
            assert resp.status == 200
            written = _written(sandbox)["weather"]
            assert written["args"] == ["-y", "@acme/weather-mcp@2"]
            assert "disabled" not in written
        finally:
            await client.close()

    async def test_spec_cannot_smuggle_disabled_false(self, sandbox, fake_sel):
        """A pasted spec carrying ``disabled: false`` must not enable the server."""
        entry = dict(_STDIO, disabled=True)
        sandbox.kirocrew_json.write_text(json.dumps({"mcpServers": {"weather": entry}}))
        client = await _client()
        try:
            resp = await client.put(
                "/api/mcp/custom/weather", json={"spec": dict(_REMOTE, disabled=False)}
            )
            assert resp.status == 200
            assert _written(sandbox)["weather"]["disabled"] is True
        finally:
            await client.close()

    async def test_unknown_server_404(self, sandbox, fake_sel):
        client = await _client()
        try:
            resp = await client.put("/api/mcp/custom/ghost", json={"spec": _REMOTE})
            assert resp.status == 404
            assert _written(sandbox) == {}
        finally:
            await client.close()

    async def test_invalid_name_400(self, sandbox, fake_sel):
        client = await _client()
        try:
            resp = await client.put("/api/mcp/custom/..evil", json={"spec": _REMOTE})
            assert resp.status == 400
        finally:
            await client.close()

    async def test_invalid_spec_400_and_nothing_written(self, sandbox, fake_sel):
        sandbox.kirocrew_json.write_text(json.dumps({"mcpServers": {"weather": dict(_STDIO)}}))
        client = await _client()
        try:
            resp = await client.put("/api/mcp/custom/weather", json={"spec": {"command": ""}})
            assert resp.status == 400
            assert _written(sandbox)["weather"]["command"] == "npx"
        finally:
            await client.close()

    async def test_sel_logged_on_update(self, sandbox, fake_sel):
        sandbox.kirocrew_json.write_text(json.dumps({"mcpServers": {"weather": dict(_STDIO)}}))
        client = await _client()
        try:
            await client.put("/api/mcp/custom/weather", json={"spec": _REMOTE})
            kwargs = fake_sel.log_api_access.call_args.kwargs
            assert kwargs["operation"] == "mcp_custom_update"
            assert kwargs["outcome"] == "ok"
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# GET /api/mcp/custom/{name} — raw spec for the edit modal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCustomGet:
    async def test_returns_full_spec_including_env(self, sandbox, fake_sel):
        entry = dict(_STDIO, disabled=True)
        sandbox.kirocrew_json.write_text(json.dumps({"mcpServers": {"weather": entry}}))
        client = await _client()
        try:
            resp = await client.get("/api/mcp/custom/weather")
            assert resp.status == 200
            body = await resp.json()
            assert body["name"] == "weather"
            assert body["enabled"] is False
            assert body["spec"]["env"] == {"KEY": ""}  # env preserved for prefill
            assert "disabled" not in body["spec"]
        finally:
            await client.close()

    async def test_oauth_hints_round_trip_without_echoing_authorization(
        self, sandbox, fake_sel
    ):
        """scopes/clientId survive GET→PUT unchanged, alongside a header entry."""
        entry = {
            "url": "https://api.githubcopilot.com/mcp/",
            "headers": {"Authorization": "Bearer custom-secret"},
            "scopes": ["read:user"],
            "clientId": "public-client-id",
        }
        sandbox.kirocrew_json.write_text(json.dumps({"mcpServers": {"github": entry}}))
        client = await _client()
        try:
            resp = await client.get("/api/mcp/custom/github")
            assert resp.status == 200
            body = await resp.json()
            assert body["spec"]["scopes"] == ["read:user"]
            assert body["spec"]["clientId"] == "public-client-id"

            resp = await client.put("/api/mcp/custom/github", json={"spec": body["spec"]})
            assert resp.status == 200
            written = _written(sandbox)["github"]
            assert written["scopes"] == ["read:user"]
            assert written["clientId"] == "public-client-id"
            assert written["headers"] == {"Authorization": "Bearer custom-secret"}
        finally:
            await client.close()

    async def test_unknown_404_and_bad_name_400(self, sandbox, fake_sel):
        client = await _client()
        try:
            assert (await client.get("/api/mcp/custom/ghost")).status == 404
            assert (await client.get("/api/mcp/custom/..evil")).status == 400
        finally:
            await client.close()


@pytest.mark.asyncio
class TestMalformedConfigNeverClobbered:
    """A malformed existing mcp.json must fail the add — never be coerced
    to {} and atomically replaced, destroying every configured server
    while reporting success (GPT 5.6 HIGH on the lenient loader)."""

    async def test_add_refuses_to_write_over_malformed_file(self, sandbox, fake_sel):
        sandbox.kirocrew_json.write_text("{not json")
        client = await _client()
        try:
            resp = await client.post(
                "/api/mcp/custom", json={"servers": {"weather": dict(_STDIO)}}
            )
            assert resp.status == 500
            assert "malformed" in (await resp.json())["error"]
            # The broken file is untouched — nothing was clobbered.
            assert sandbox.kirocrew_json.read_text(encoding="utf-8") == "{not json"
        finally:
            await client.close()

    async def test_add_refuses_non_object_mcpservers(self, sandbox, fake_sel):
        sandbox.kirocrew_json.write_text(json.dumps({"mcpServers": ["broken"]}))
        client = await _client()
        try:
            resp = await client.post(
                "/api/mcp/custom", json={"servers": {"weather": dict(_STDIO)}}
            )
            assert resp.status == 500
            assert json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8"))["mcpServers"] == ["broken"]
        finally:
            await client.close()

    async def test_add_preserves_existing_entries(self, sandbox, fake_sel):
        sandbox.kirocrew_json.write_text(
            json.dumps({"mcpServers": {"existing": {"command": "keepme"}}})
        )
        client = await _client()
        try:
            resp = await client.post(
                "/api/mcp/custom", json={"servers": {"weather": dict(_STDIO)}}
            )
            assert resp.status == 200
            servers = json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8"))["mcpServers"]
            assert servers["existing"] == {"command": "keepme"}
            assert servers["weather"]["disabled"] is True
        finally:
            await client.close()


@pytest.mark.asyncio
class TestCarriedKeyRoundTrip:
    """GET→PUT round-trip of entries carrying non-allowlisted keys.

    Entries legitimately carry keys other flows write (``disabledTools``,
    ``autoApprove``, ``headers``).  A save of the unmodified GET payload
    must succeed and those keys must survive verbatim — dropping
    ``disabledTools`` on save would silently widen the agent's tool
    surface (Arbiter blocking item on the v1 strict allowlist)."""

    _ENTRY = {
        "command": "npx",
        "args": ["-y", "@acme/weather-mcp"],
        "disabledTools": ["dangerous_tool"],
        "disabled": True,
    }

    def _seed(self, sandbox) -> None:
        sandbox.kirocrew_json.write_text(json.dumps({"mcpServers": {"weather": self._ENTRY}}))

    async def test_unmodified_get_payload_saves_and_preserves_keys(self, sandbox, fake_sel):
        self._seed(sandbox)
        client = await _client()
        try:
            got = await (await client.get("/api/mcp/custom/weather")).json()
            resp = await client.put("/api/mcp/custom/weather", json={"spec": got["spec"]})
            assert resp.status == 200
            data = json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8"))
            entry = data["mcpServers"]["weather"]
            assert entry["disabledTools"] == ["dangerous_tool"]
            assert entry["disabled"] is True  # enabled state also preserved
        finally:
            await client.close()

    async def test_the_authorship_marker_is_the_one_carried_key_not_preserved(
        self, sandbox, fake_sel
    ):
        """A hand-added marker in the store cannot ride a save back out.

        Every other non-allowlisted key round-trips, because dropping one would
        silently change behaviour the editor does not own. The marker is the
        exception: it records that Kiro Crew wrote an entry into a file it does
        NOT own, so it is meaningless in the store, and preserving it here would
        let a hand-edit volunteer the entry for management on a shared surface.
        """
        from kiro_crew.mcp_provenance import MARKER_KEY

        sandbox.kirocrew_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "weather": {**self._ENTRY, MARKER_KEY: {"managed": True}},
                    }
                }
            )
        )
        client = await _client()
        try:
            got = await (await client.get("/api/mcp/custom/weather")).json()
            resp = await client.put("/api/mcp/custom/weather", json={"spec": got["spec"]})
            assert resp.status == 200
            entry = json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8"))["mcpServers"][
                "weather"
            ]
            assert MARKER_KEY not in entry
            assert entry["disabledTools"] == ["dangerous_tool"], "real carried keys still survive"

            # A FRESH add tolerates no such key, so a pasted block cannot claim
            # management for a server the dashboard has never written.
            resp = await client.post(
                "/api/mcp/custom",
                json={"servers": {"pasted": {"command": "npx", MARKER_KEY: {"managed": True}}}},
            )
            assert resp.status == 400
            assert MARKER_KEY in (await resp.json())["error"]
        finally:
            await client.close()

    async def test_removing_carried_key_still_preserves_it(self, sandbox, fake_sel):
        self._seed(sandbox)
        client = await _client()
        try:
            resp = await client.put(
                "/api/mcp/custom/weather",
                json={"spec": {"command": "npx", "args": ["-y", "@acme/weather-mcp@2"]}},
            )
            assert resp.status == 200
            entry = json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8"))["mcpServers"]["weather"]
            assert entry["disabledTools"] == ["dangerous_tool"], "must never be dropped by edit"
            assert entry["args"] == ["-y", "@acme/weather-mcp@2"]
        finally:
            await client.close()

    async def test_modifying_carried_key_is_rejected(self, sandbox, fake_sel):
        self._seed(sandbox)
        client = await _client()
        try:
            resp = await client.put(
                "/api/mcp/custom/weather",
                json={"spec": {"command": "npx", "disabledTools": []}},
            )
            assert resp.status == 400
            assert "managed by other flows" in (await resp.json())["error"]
            entry = json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8"))["mcpServers"]["weather"]
            assert entry["disabledTools"] == ["dangerous_tool"]
        finally:
            await client.close()

    async def test_clearing_oauth_hints_defeats_a_carried_wire_sibling(self, sandbox, fake_sel):
        """An explicit clear must actually stop the grant being requested.

        The scope-toggle preservation rule copies a global server's spec into the
        store verbatim, so the entry can hold the WIRE spelling. Those keys sit
        outside the editable set, so carried-key restoration puts them back
        untouched -- and with the internal key absent from disk the reader falls
        through to the sibling, so the cleared grant keeps being requested while
        the API answers 200 and looks like it worked.
        """
        from kiro_crew.mcp_utils import kiro_entry_client_id, kiro_entry_scopes

        sandbox.kirocrew_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "weather": {
                            "url": "https://mcp.acme.com/mcp",
                            "oauthScopes": ["acme:read"],
                            "oauth": {"clientId": "acme-client", "issuer": "https://iss"},
                        }
                    }
                }
            )
        )
        client = await _client()
        try:
            resp = await client.put(
                "/api/mcp/custom/weather",
                json={"spec": {"url": "https://mcp.acme.com/mcp", "scopes": []}},
            )
            assert resp.status == 200
            entry = _written(sandbox)["weather"]
            assert kiro_entry_scopes(entry) == [], "the cleared scopes must not come back"
            assert kiro_entry_client_id(entry) == "", "nor the cleared client id"
            assert entry.get("oauth", {}).get("issuer") == "https://iss", (
                "a sibling sub-key we never owned still survives"
            )
        finally:
            await client.close()

    async def test_clearing_oauth_hints_also_drops_the_nested_wire_sibling(
        self, sandbox, fake_sel
    ):
        """The reader falls through to ``oauth.oauthScopes`` too, so a clear must reach it."""
        from kiro_crew.mcp_utils import kiro_entry_scopes

        sandbox.kirocrew_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "weather": {
                            "url": "https://mcp.acme.com/mcp",
                            "oauth": {
                                "oauthScopes": ["acme:read"],
                                "issuer": "https://iss",
                            },
                        }
                    }
                }
            )
        )
        client = await _client()
        try:
            resp = await client.put(
                "/api/mcp/custom/weather",
                json={"spec": {"url": "https://mcp.acme.com/mcp"}},
            )
            assert resp.status == 200
            entry = _written(sandbox)["weather"]
            assert kiro_entry_scopes(entry) == [], "a nested wire sibling must not survive"
            assert entry.get("oauth", {}).get("issuer") == "https://iss", (
                "an unrelated oauth sub-key still survives"
            )
        finally:
            await client.close()

    async def test_malformed_wire_oauth_fields_are_rejected_like_internal_ones(
        self, sandbox, fake_sel
    ):
        """Both spellings of a hint answer to one shape rule.

        The wire spellings are editable on this path, so tolerating them by name
        (so an unmodified round trip still saves) must not also skip their shape
        check -- otherwise the same field is accepted or rejected depending only
        on which spelling the caller used, and an invalid value reaches disk
        under a 200.
        """
        url = "https://mcp.acme.com/mcp"
        for bad in (
            {"oauthScopes": [123]},
            {"oauthScopes": "not-a-list"},
            {"oauthScopes": [""]},
            {"oauth": {"clientId": {}}},
            {"oauth": {"clientId": ""}},
            {"oauth": "not-a-dict"},
        ):
            sandbox.kirocrew_json.write_text(
                json.dumps({"mcpServers": {"weather": {"url": url}}})
            )
            client = await _client()
            try:
                resp = await client.put(
                    "/api/mcp/custom/weather", json={"spec": {"url": url, **bad}}
                )
                assert resp.status == 400, f"{bad} must be rejected, got {resp.status}"
                assert _written(sandbox)["weather"] == {"url": url}, "disk must be untouched"
            finally:
                await client.close()

    async def test_valid_wire_oauth_fields_still_round_trip(self, sandbox, fake_sel):
        """The shape check must not break the preservation path it guards."""
        url = "https://mcp.acme.com/mcp"
        sandbox.kirocrew_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "weather": {
                            "url": url,
                            "oauthScopes": ["acme:read"],
                            "oauth": {"clientId": "acme-client", "issuer": "https://iss"},
                        }
                    }
                }
            )
        )
        client = await _client()
        try:
            got = await (await client.get("/api/mcp/custom/weather")).json()
            resp = await client.put("/api/mcp/custom/weather", json={"spec": got["spec"]})
            assert resp.status == 200
            entry = _written(sandbox)["weather"]
            assert entry["oauthScopes"] == ["acme:read"]
            assert entry["oauth"]["clientId"] == "acme-client"
            assert entry["oauth"]["issuer"] == "https://iss"
        finally:
            await client.close()

    async def test_editing_a_wire_sibling_value_persists(self, sandbox, fake_sel):
        """Stated-is-authoritative covers a VALUE change, not just presence.

        A preserved entry can hold the wire spelling, so the editor's only way to
        narrow that grant is to change the wire value. Honouring absence but
        dropping an edit would be the same silent 200 as a dropped clear.
        """
        url = "https://mcp.acme.com/mcp"
        for before, submit, expect_scopes in (
            # nested wire sibling edited in place
            (
                {"oauth": {"oauthScopes": ["a"], "issuer": "https://iss"}},
                {"oauth": {"oauthScopes": ["b"], "issuer": "https://iss"}},
                ["b"],
            ),
            # top-level wire spelling edited in place
            ({"oauthScopes": ["a"]}, {"oauthScopes": ["b"]}, ["b"]),
        ):
            sandbox.kirocrew_json.write_text(
                json.dumps({"mcpServers": {"weather": {"url": url, **before}}})
            )
            client = await _client()
            try:
                resp = await client.put(
                    "/api/mcp/custom/weather", json={"spec": {"url": url, **submit}}
                )
                assert resp.status == 200, f"{submit} rejected: {await resp.text()}"
                entry = _written(sandbox)["weather"]
                from kiro_crew.mcp_utils import kiro_entry_scopes

                assert kiro_entry_scopes(entry) == expect_scopes, (
                    f"{submit} must persist, got {entry}"
                )
            finally:
                await client.close()

    async def test_editing_a_nested_client_id_persists(self, sandbox, fake_sel):
        """Same rule for the other hint, and unrelated sub-keys survive."""
        from kiro_crew.mcp_utils import kiro_entry_client_id

        url = "https://mcp.acme.com/mcp"
        sandbox.kirocrew_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "weather": {
                            "url": url,
                            "oauth": {"clientId": "old-client", "issuer": "https://iss"},
                        }
                    }
                }
            )
        )
        client = await _client()
        try:
            resp = await client.put(
                "/api/mcp/custom/weather",
                json={
                    "spec": {
                        "url": url,
                        "oauth": {"clientId": "new-client", "issuer": "https://iss"},
                    }
                },
            )
            assert resp.status == 200
            entry = _written(sandbox)["weather"]
            assert kiro_entry_client_id(entry) == "new-client"
            assert entry["oauth"]["issuer"] == "https://iss"
        finally:
            await client.close()

    async def test_editing_a_non_hint_oauth_subkey_is_rejected_not_silently_reverted(
        self, sandbox, fake_sel
    ):
        """A sub-key we do not own follows the carried-key rule: refuse, don't revert.

        ``issuer`` is written by other flows, so it is not editable here -- but the
        answer must say so. Silently restoring the on-disk value would report
        success for a change that never happened.
        """
        url = "https://mcp.acme.com/mcp"
        sandbox.kirocrew_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "weather": {
                            "url": url,
                            "oauth": {"clientId": "c", "issuer": "https://old"},
                        }
                    }
                }
            )
        )
        client = await _client()
        try:
            resp = await client.put(
                "/api/mcp/custom/weather",
                json={"spec": {"url": url, "oauth": {"clientId": "c", "issuer": "https://new"}}},
            )
            assert resp.status == 400, f"expected refusal, got {resp.status}"
            entry = _written(sandbox)["weather"]
            assert entry["oauth"]["issuer"] == "https://old", "disk must be untouched"
        finally:
            await client.close()

    async def test_adding_a_non_hint_oauth_subkey_is_refused_not_dropped(self, sandbox, fake_sel):
        """A stated sub-key we cannot store must be refused, not accepted and dropped.

        The editor is a raw JSON field, so a sub-key with no prior value on disk is
        reachable. Base answered 400 for an unknown key; accepting it and writing
        nothing turns that explicit refusal into a silent no-op.
        """
        url = "https://mcp.acme.com/mcp"
        sandbox.kirocrew_json.write_text(json.dumps({"mcpServers": {"weather": {"url": url}}}))
        client = await _client()
        try:
            resp = await client.put(
                "/api/mcp/custom/weather",
                json={"spec": {"url": url, "oauth": {"issuer": "https://new"}}},
            )
            assert resp.status == 400, f"expected refusal, got {resp.status}"
            assert "oauth" not in _written(sandbox)["weather"]
        finally:
            await client.close()

    async def test_an_internal_clear_outranks_a_round_tripped_wire_sibling(self, sandbox, fake_sel):
        """Stating the internal spelling settles BOTH wire spellings.

        The editor prefills the whole spec, so a user clearing scopes submits
        ``scopes: []`` next to the nested wire hint the GET handed them. Copying
        that sibling back would let the reader fall through to it and resurrect the
        grant the submission just cleared.
        """
        from kiro_crew.mcp_utils import kiro_entry_scopes

        url = "https://mcp.acme.com/mcp"
        sandbox.kirocrew_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "weather": {
                            "url": url,
                            "oauth": {"oauthScopes": ["stale:read"], "issuer": "https://iss"},
                        }
                    }
                }
            )
        )
        client = await _client()
        try:
            resp = await client.put(
                "/api/mcp/custom/weather",
                json={
                    "spec": {
                        "url": url,
                        "scopes": [],
                        "oauth": {"oauthScopes": ["stale:read"], "issuer": "https://iss"},
                    }
                },
            )
            assert resp.status == 200, await resp.text()
            entry = _written(sandbox)["weather"]
            assert kiro_entry_scopes(entry) == [], f"clear did not take effect: {entry}"
            assert entry["oauth"]["issuer"] == "https://iss", "unowned sub-key must survive"
        finally:
            await client.close()

    async def test_fresh_add_still_rejects_unknown_keys(self, sandbox, fake_sel):
        client = await _client()
        try:
            resp = await client.post(
                "/api/mcp/custom",
                json={"servers": {"new-srv": {"command": "npx", "disabledTools": []}}},
            )
            assert resp.status == 400  # POST has no existing entry to carry from
        finally:
            await client.close()


@pytest.mark.asyncio
class TestServersListSurfacesCustomAdds:
    """End-to-end within the handler layer: a consent-disabled custom add
    must appear in ``GET /api/mcp`` as a disabled, KiroCrew-managed row.

    Pins the live bug where freshly added (disabled) servers were invisible
    in the table — making the enable/consent action unreachable."""

    async def test_disabled_custom_add_appears_in_servers_list(
        self, sandbox, fake_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        # Route discovery at the sandboxed KiroCrew scope file only.
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_JSON_PATHS", (sandbox.kirocrew_json,)
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._extra_scope_sources", lambda: [])

        app = web.Application()
        state = MagicMock()
        state._background_tasks = set()
        app["state"] = state
        from kiro_crew.dashboard.handlers import mcp_custom as custom_mod

        app.router.add_post("/api/mcp/custom", custom_mod.api_mcp_custom_add)
        app.router.add_get("/api/mcp", mcp_mod.api_mcp_servers)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/mcp/custom", json={"servers": {"weather": dict(_STDIO)}}
            )
            assert resp.status == 200

            resp = await client.get("/api/mcp")
            assert resp.status == 200
            rows = await resp.json()
            row = next((r for r in rows if r["name"] == "weather"), None)
            assert row is not None, "consent-disabled add must get a table row"
            assert row["enabled"] is False
            assert row["status"] == "disabled"
            assert row["kirocrewManaged"] is True
        finally:
            await client.close()
