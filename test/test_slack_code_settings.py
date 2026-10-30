"""Settings → Slack: the Slack Code switches and the install's drift status.

Covers the PUT validation of the registered switches, the GET read model with
each switch's capability, the SEL trail, and the manifest-drift probe against
recorded Slack payloads (no live workspace).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as mod
from kiro_crew import slack_manifest
from kiro_crew.dashboard import slack_code_settings as scs
from kiro_crew.slack import features, scope_probe

_AS = frozenset({slack_manifest.AGENT_SESSIONS})


class _State:
    def __init__(self) -> None:
        self.slack_socket_connected = False
        self.slack_connect_error = ""


class _SelRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.calls.append(kw)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    envf = tmp_path / ".env"
    envf.write_text("", encoding="utf-8")
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(loader, "env_path", lambda: envf)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    sel = _SelRecorder()
    monkeypatch.setattr(mod, "_sel", lambda: sel)
    return {"cfg": cfg, "sel": sel, "state": _State()}


def _call(env: dict[str, Any], method: str, body: dict | None = None) -> tuple[int, dict]:
    async def _run() -> tuple[int, dict]:
        app = web.Application()
        app["state"] = env["state"]
        app.router.add_put("/api/slack/config", mod.api_slack_config_save)
        app.router.add_get("/api/slack/config", mod.api_slack_config_get)
        async with TestClient(TestServer(app)) as client:
            if method == "PUT":
                resp = await client.put("/api/slack/config", json=body)
            else:
                resp = await client.get("/api/slack/config")
            return resp.status, await resp.json()

    return asyncio.run(_run())


def _stored(env: dict[str, Any]) -> dict:
    return json.loads(env["cfg"].read_text(encoding="utf-8"))["slack"]


class TestSwitches:
    def test_defaults_read_off(self, env: dict) -> None:
        status, body = _call(env, "GET")
        assert status == 200
        assert body["agent_sessions"] is False
        # Only registered switches are reported; nothing ships ahead of its reader.
        assert "code_channels" not in body and "code_channel_defaults" not in body

    @pytest.mark.parametrize("on", [True, False])
    def test_the_switch_saves_and_reads_back(self, env: dict, on: bool) -> None:
        status, res = _call(env, "PUT", {"agent_sessions": on})
        assert status == 200
        # Hot-applied: the runtime reads the toggle per event, behind the
        # capability guard, so no flip needs a restart.
        assert res["restart_required"] is False
        _, body = _call(env, "GET")
        assert body["agent_sessions"] is on

    def test_an_unregistered_switch_is_not_stored(self, env: dict) -> None:
        _call(env, "PUT", {"not_a_switch": True, "code_channels": True, "agent_sessions": True})
        stored = _stored(env)
        assert stored["agent_sessions"] is True
        assert "not_a_switch" not in stored and "code_channels" not in stored

    def test_unchanged_switch_is_not_staged(self, env: dict) -> None:
        _call(env, "PUT", {"agent_sessions": True})
        status, _ = _call(env, "PUT", {"agent_sessions": True})
        assert status == 200
        ok = [c for c in env["sel"].calls if c["outcome"] == "ok"]
        assert "agent_sessions" in ok[0]["resources"]
        assert "agent_sessions" not in ok[1]["resources"]

    def test_toggle_on_reports_the_capability_it_still_lacks(self, env: dict) -> None:
        features.reset_capabilities()
        _call(env, "PUT", {"agent_sessions": True})
        _, body = _call(env, "GET")
        assert body["agent_sessions"] is True
        assert body["agent_sessions_capability"] == {
            "available": False,
            "reason": "scopes_unknown",
            "scope": "assistant:write",
        }
        features.record_granted_scopes({"chat:write"})
        _, body = _call(env, "GET")
        assert body["agent_sessions_capability"]["reason"] == "missing_scope"
        features.record_granted_scopes({"chat:write", "assistant:write"})
        _, body = _call(env, "GET")
        assert body["agent_sessions_capability"] == {
            "available": True,
            "reason": "",
            "scope": "assistant:write",
        }
        features.reset_capabilities()

    @pytest.mark.parametrize("bad", ["true", 1, None])
    def test_switch_must_be_a_boolean(self, env: dict, bad: object) -> None:
        status, res = _call(env, "PUT", {"agent_sessions": bad})
        assert status == 400 and "boolean" in res["error"]
        assert env["sel"].calls[-1]["outcome"] == "denied"
        assert not env["cfg"].exists()


# ── Manifest drift probe, against recorded Slack payloads ──


class _Resp(dict):
    def __init__(self, data: dict, headers: dict | None = None) -> None:
        super().__init__(data)
        self.headers = headers or {}


class _SlackErr(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.response = {"ok": False, "error": error}


class _FakeClient:
    def __init__(self, scopes: str | None, *, auth_error: str = "") -> None:
        self.scopes = scopes
        self.auth_error = auth_error
        self.calls: list[str] = []

    async def auth_test(self) -> _Resp:
        self.calls.append("auth.test")
        if self.auth_error:
            raise _SlackErr(self.auth_error)
        headers = {} if self.scopes is None else {"X-OAuth-Scopes": self.scopes}
        return _Resp({"ok": True, "team_id": "T123", "bot_id": "B123"}, headers)

    async def bots_info(self, bot: str) -> _Resp:
        self.calls.append("bots.info")
        return _Resp({"ok": True, "bot": {"id": bot, "app_id": "A0123ABCD"}})

    async def api_call(self, method: str, **kw: Any) -> _Resp:
        self.calls.append(method)
        return _Resp({"ok": True})


def _probe(client: _FakeClient, features: frozenset[str]) -> scope_probe.ManifestStatus:
    req = slack_manifest.requirements(features)
    every = slack_manifest.requirements(frozenset(slack_manifest.FEATURES)).bot_scopes
    return asyncio.run(
        scope_probe.probe_manifest_status(client, req.bot_scopes, known_scopes=every)
    )


def _scopes(features: frozenset[str]) -> str:
    return ",".join(sorted(slack_manifest.requirements(features).bot_scopes))


@pytest.fixture(autouse=True)
def _clean_probe_state() -> Any:
    features.reset_capabilities()
    scs.reset_status_cache()
    yield
    features.reset_capabilities()
    scs.reset_status_cache()


class TestDrift:
    @pytest.mark.parametrize("features", slack_manifest.feature_sets())
    def test_current_install_is_up_to_date(self, features: frozenset[str]) -> None:
        got = _probe(_FakeClient(_scopes(features)), features)
        assert got.status == scope_probe.STATUS_UP_TO_DATE
        assert got.missing_scopes == () and got.app_id == "A0123ABCD"

    def test_old_install_after_enabling_agent_sessions_needs_update(self) -> None:
        client = _FakeClient(_scopes(frozenset()))
        got = _probe(client, _AS)
        assert got.status == scope_probe.STATUS_NEEDS_UPDATE
        assert got.missing_scopes == ("assistant:write",)
        # Agent sessions have no read-only feature probe.
        assert client.calls == ["auth.test", "bots.info"]

    def test_switch_turned_off_reports_extra_scopes_only(self) -> None:
        got = _probe(_FakeClient(_scopes(_AS) + ",emoji:read"), frozenset())
        assert got.status == scope_probe.STATUS_UP_TO_DATE
        # emoji:read is the user's own addition, not drift.
        assert got.extra_scopes == ("assistant:write",)

    def test_unreadable_scopes(self) -> None:
        got = _probe(_FakeClient(None), _AS)
        assert (got.status, got.reason) == (scope_probe.STATUS_UNKNOWN, "scopes_unreadable")

    def test_network_failure_is_unknown(self) -> None:
        class _Down(_FakeClient):
            async def auth_test(self) -> _Resp:
                raise ConnectionError("offline")

        got = _probe(_Down(""), _AS)
        assert (got.status, got.reason) == (scope_probe.STATUS_UNKNOWN, "ConnectionError")

    def test_rejected_token(self) -> None:
        got = _probe(_FakeClient("", auth_error="invalid_auth"), _AS)
        assert got.status == scope_probe.STATUS_TOKEN_REJECTED

    def test_runtime_refusal_is_reported_as_workspace_not_enabled(self) -> None:
        # Agent sessions have no read-only probe: the runtime's feature_disabled
        # latch is the only signal, and the page shows it.
        features.latch_unavailable("agent_sessions", "feature_disabled")
        got = _probe(_FakeClient(_scopes(_AS)), _AS)
        assert got.status == scope_probe.STATUS_FEATURE_DISABLED
        assert got.disabled_features == ("agent_sessions",)
        # Missing scopes still come first: the update is the next step.
        got = _probe(_FakeClient(_scopes(frozenset())), _AS)
        assert got.status == scope_probe.STATUS_NEEDS_UPDATE

    def test_status_endpoint_without_token(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        body = asyncio.run(scs.manifest_status(loader.KiroCrewConfig(), ""))
        assert body["install"]["status"] == scope_probe.STATUS_NOT_CONFIGURED
        assert body["features"] == [] and body["app_manifest_url"] is None
        assert set(body["feature_changes"]) == set(slack_manifest.FEATURES)

    def test_check_again_lifts_a_stale_workspace_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An admin enabled the feature after the runtime latched it: Check again
        # (refresh) must re-probe like a reconnect, not keep the old refusal.
        import slack_sdk.web.async_client as sdk

        monkeypatch.setattr(sdk, "AsyncWebClient", lambda **kw: _FakeClient(_scopes(_AS)))
        cfg = loader.KiroCrewConfig()
        cfg.slack.agent_sessions = True
        features.latch_unavailable("agent_sessions", "feature_disabled")
        first = asyncio.run(scs.manifest_status(cfg, "xoxb-test"))
        assert first["install"]["status"] == scope_probe.STATUS_FEATURE_DISABLED
        again = asyncio.run(scs.manifest_status(cfg, "xoxb-test", refresh=True))
        assert again["install"]["status"] == scope_probe.STATUS_UP_TO_DATE
        assert features.latched() == {}
        assert features.available("agent_sessions")

    def test_a_cached_read_keeps_the_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import slack_sdk.web.async_client as sdk

        monkeypatch.setattr(sdk, "AsyncWebClient", lambda **kw: _FakeClient(_scopes(_AS)))
        cfg = loader.KiroCrewConfig()
        cfg.slack.agent_sessions = True
        features.latch_unavailable("agent_sessions", "feature_disabled")
        got = asyncio.run(scs.manifest_status(cfg, "xoxb-test"))
        assert got["install"]["status"] == scope_probe.STATUS_FEATURE_DISABLED
        assert features.latched() == {"agent_sessions": "feature_disabled"}

    def test_status_is_cached_and_refresh_bypasses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clients: list[_FakeClient] = []

        def _make(**kw: Any) -> _FakeClient:
            c = _FakeClient(_scopes(frozenset()))
            clients.append(c)
            return c

        import slack_sdk.web.async_client as sdk

        monkeypatch.setattr(sdk, "AsyncWebClient", _make)
        cfg = loader.KiroCrewConfig()
        first = asyncio.run(scs.manifest_status(cfg, "xoxb-test"))
        asyncio.run(scs.manifest_status(cfg, "xoxb-test"))
        assert len(clients) == 1
        asyncio.run(scs.manifest_status(cfg, "xoxb-test", refresh=True))
        assert len(clients) == 2
        assert first["install"]["status"] == scope_probe.STATUS_UP_TO_DATE
        assert first["app_manifest_url"] == "https://api.slack.com/apps/A0123ABCD/app-manifest"
        assert "xoxb-test" not in json.dumps(first)


class TestStatusReadKeepsARemovedToken:
    def test_the_status_read_does_not_put_back_a_token_the_watcher_removed(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live watcher has applied a removal of the bot token (it is gone from
        the environment) while the status read still sees it in ``.env``. The read
        uses the token for its one Slack call, and leaves the removal standing."""
        import os

        loader.env_path().write_text("SLACK_BOT_TOKEN=xoxb-removed\n", encoding="utf-8")
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        seen: list[str] = []

        async def _status(cfg: Any, token: str, refresh: bool = False) -> dict:
            seen.append(token)
            return {"install": {"status": scope_probe.STATUS_UP_TO_DATE}}

        monkeypatch.setattr(scs, "manifest_status", _status)

        async def _run() -> int:
            app = web.Application()
            app.router.add_get("/api/slack/manifest/status", mod.api_slack_manifest_status)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/slack/manifest/status")
                return resp.status

        assert asyncio.run(_run()) == 200
        assert seen == ["xoxb-removed"]
        assert "SLACK_BOT_TOKEN" not in os.environ
