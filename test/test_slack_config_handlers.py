"""Tests for Slack config API helpers (secret masking + .env updates)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp.test_utils import make_mocked_request

import kiro_crew.config.loader as loader
from kiro_crew.dashboard.handlers.messaging import _mask_secret, _write_env_updates


def test_mask_secret_keeps_prefix_and_tail() -> None:
    assert _mask_secret("xoxb-1234-abcdWXYZ") == "xoxb-••••WXYZ"
    assert _mask_secret("xapp-1-A0-9-secretkey") == "xapp-••••tkey"


def test_mask_secret_edge_cases() -> None:
    assert _mask_secret("") == ""  # unset → empty
    assert _mask_secret("abc") == "••••"  # too short for a tail, no dash prefix
    assert _mask_secret("nodash") == "••••dash"  # no prefix, last 4 shown


def test_write_env_updates_adds_and_preserves(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("# creds\nOTHER=keepme\nSLACK_BOT_TOKEN=old\n", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)

    _write_env_updates({"SLACK_BOT_TOKEN": "xoxb-new", "SLACK_APP_TOKEN": "xapp-new"})

    lines = env.read_text(encoding="utf-8").splitlines()
    assert "# creds" in lines  # comment preserved
    assert "OTHER=keepme" in lines  # unrelated key untouched
    assert "SLACK_BOT_TOKEN=xoxb-new" in lines  # updated in place
    assert "SLACK_APP_TOKEN=xapp-new" in lines  # new key appended
    # Credential file hardened to owner-only perms.
    assert (env.stat().st_mode & 0o077) == 0


def test_write_env_updates_deletes_on_none(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("SLACK_BOT_TOKEN=old\nSLACK_APP_TOKEN=keep\n", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)

    _write_env_updates({"SLACK_BOT_TOKEN": None})

    text = env.read_text(encoding="utf-8")
    assert "SLACK_BOT_TOKEN" not in text  # removed
    assert "SLACK_APP_TOKEN=keep" in text  # sibling untouched


def test_write_env_updates_handles_missing_file(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / "sub" / ".env"  # parent dir does not exist yet
    monkeypatch.setattr(loader, "env_path", lambda: env)

    _write_env_updates({"KIROCREW_OWNER_ID": "U0123ABC456"})

    assert env.read_text(encoding="utf-8").strip() == "KIROCREW_OWNER_ID=U0123ABC456"


def test_save_denies_non_loopback(monkeypatch) -> None:
    """Config writes are loopback-only: remote sessions are read-only."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
    req = make_mocked_request(
        "PUT",
        "/api/slack/config",
        payload=b'{"command": "evil", "bot_token": "xoxb-planted"}',
        headers={"Content-Type": "application/json"},
    )
    resp = asyncio.run(mod.api_slack_config_save(req))
    assert resp.status == 403


def test_write_env_updates_is_atomic_and_owner_only(tmp_path: Path, monkeypatch) -> None:
    """The .env write lands with 0600 perms and preserves unrelated keys."""
    env = tmp_path / ".env"
    env.write_text("SLACK_APP_TOKEN=keep\nWECOM_SECRET=other\n", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)

    _write_env_updates({"SLACK_BOT_TOKEN": "xoxb-new"})

    text = env.read_text(encoding="utf-8")
    assert "SLACK_BOT_TOKEN=xoxb-new" in text
    assert "WECOM_SECRET=other" in text  # unrelated credential preserved
    assert (env.stat().st_mode & 0o077) == 0  # owner-only
    # No stray temp files left behind in the dir.
    assert not any(p.name.startswith(".env.") for p in tmp_path.iterdir())


class _StubRequest:
    """Minimal request double: is_direct_local_request reads only .remote and
    .headers. make_mocked_request cannot set a loopback peer in this aiohttp
    version, so tests use this to exercise the loopback branch for real."""

    def __init__(self, remote: str, headers: dict | None = None) -> None:
        self.remote = remote
        self.headers = headers or {}


def test_direct_local_requires_loopback_and_no_forward_headers() -> None:
    from kiro_crew.dashboard.origin import is_direct_local_request

    # Genuine local: loopback peer, no proxy headers.
    assert is_direct_local_request(_StubRequest("127.0.0.1"))
    assert is_direct_local_request(_StubRequest("::1"))
    # Non-loopback peer: always remote.
    assert not is_direct_local_request(_StubRequest("203.0.113.7"))


def test_forwarded_loopback_request_is_not_direct_local() -> None:
    """A proxied/tunneled request arrives FROM a real loopback peer but must
    be treated as remote: any standard forwarding header flips the gate."""
    from kiro_crew.dashboard.origin import is_direct_local_request

    headers = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP")
    for header in headers:
        req = _StubRequest("127.0.0.1", {header: "203.0.113.7"})
        assert not is_direct_local_request(req), f"{header} should mark request remote"


def test_save_denies_forwarded_loopback_request() -> None:
    """End-to-end: a reverse-proxied request (loopback peer + XFF) cannot
    write config or plant tokens — 403 before any parsing."""
    import kiro_crew.dashboard.handlers.messaging as mod

    req = make_mocked_request(
        "PUT",
        "/api/slack/config",
        payload=b'{"bot_token": "xoxb-planted"}',
        headers={"Content-Type": "application/json", "X-Forwarded-For": "203.0.113.7"},
    )
    resp = asyncio.run(mod.api_slack_config_save(req))
    assert resp.status == 403


def test_save_syncs_process_environ(tmp_path: Path, monkeypatch) -> None:
    """After a save, os.environ reflects the new .env state for managed keys,
    so GET (which lets env win) reports the replaced/cleared token truthfully.

    Uses a real TestServer: make_mocked_request(payload=...) does not feed
    request.json() in this aiohttp version, so body-carrying tests must go
    over a live client.
    """
    import os

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text("SLACK_BOT_TOKEN=xoxb-OLD\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _accept(key, token):
        return None

    monkeypatch.setattr(mod, "_validate_slack_token", _accept)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-OLD")
    monkeypatch.setenv("KIROCREW_OWNER_ID", "U0123ABC456")

    async def _run() -> int:
        app = web.Application()
        app.router.add_put("/api/slack/config", mod.api_slack_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/slack/config", json={"bot_token": "xoxb-NEW", "owner_id": ""}
            )
            return resp.status

    assert asyncio.run(_run()) == 200
    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-NEW"  # replaced in-process
    assert "KIROCREW_OWNER_ID" not in os.environ  # cleared key removed
    assert "SLACK_BOT_TOKEN=xoxb-NEW" in env.read_text(encoding="utf-8")


def _client_put(mod, monkeypatch, tmp_path, body):
    """Run a save over a real TestClient with paths isolated to tmp_path."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    env = tmp_path / ".env"
    if not env.exists():
        env.write_text("", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _run():
        app = web.Application()
        app.router.add_put("/api/slack/config", mod.api_slack_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/slack/config", json=body)
            return resp.status, await resp.json()

    return asyncio.run(_run()), env


def test_save_rejects_token_slack_refuses(tmp_path, monkeypatch) -> None:
    """A token Slack rejects (invalid_auth) fails the save; nothing written."""
    import kiro_crew.dashboard.handlers.messaging as mod

    async def _reject(key, token):
        return "invalid_auth"

    monkeypatch.setattr(mod, "_validate_slack_token", _reject)
    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token": "xoxb-bad"})
    status, body = status_body
    assert status == 400
    assert "invalid_auth" in body["error"]
    assert "xoxb-bad" not in env.read_text(encoding="utf-8")


def test_save_proceeds_with_warning_when_slack_unreachable(tmp_path, monkeypatch) -> None:
    """Being offline must not block a save — token stored, warning returned."""
    import kiro_crew.dashboard.handlers.messaging as mod

    async def _unreachable(key, token):
        raise ConnectionError("no route to slack.com")

    monkeypatch.setattr(mod, "_validate_slack_token", _unreachable)
    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token": "xoxb-offline"})
    status, body = status_body
    assert status == 200
    assert body["verify_warning"]
    assert "SLACK_BOT_TOKEN=xoxb-offline" in env.read_text(encoding="utf-8")


def test_manifest_endpoint_renders_alias_and_url(monkeypatch) -> None:
    """Manifest endpoint uses a non-identifying default alias (never $USER)
    and builds Slack's deep link; explicit ?alias= is honored."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setenv("USER", "hostaccount")
    req = make_mocked_request("GET", "/api/slack/manifest")
    resp = asyncio.run(mod.api_slack_manifest(req))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["alias"] == "kirocrew"  # $USER must NOT leak as the default
    assert "hostaccount" not in body["manifest"]
    assert body["create_url"].startswith("https://api.slack.com/apps?new_app=1&manifest_yaml=")

    req = make_mocked_request("GET", "/api/slack/manifest?alias=myteam")
    body = json.loads(asyncio.run(mod.api_slack_manifest(req)).text)
    assert body["alias"] == "myteam"
    assert "KiroCrew-myteam" in body["manifest"]


def test_manifest_endpoint_rejects_bad_alias() -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    req = make_mocked_request("GET", "/api/slack/manifest?alias=../evil")
    resp = asyncio.run(mod.api_slack_manifest(req))
    assert resp.status == 400


def test_clear_flags_must_be_strict_booleans(tmp_path, monkeypatch) -> None:
    """Truthy non-bool clear flags (e.g. "false", 1) must not delete tokens."""
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text("SLACK_BOT_TOKEN=xoxb-KEEP\n", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def _run(payload):
        app = web.Application()
        app.router.add_put("/api/slack/config", mod.api_slack_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/slack/config", json=payload)
            return resp.status

    assert asyncio.run(_run({"bot_token_clear": "false"})) == 400  # string rejected
    assert asyncio.run(_run({"bot_token_clear": 1})) == 400  # int rejected
    assert "xoxb-KEEP" in env.read_text(encoding="utf-8")  # token untouched
    assert asyncio.run(_run({"bot_token_clear": True})) == 200  # real bool works
    assert "SLACK_BOT_TOKEN" not in env.read_text(encoding="utf-8")


def test_restart_required_only_on_actual_change(tmp_path, monkeypatch) -> None:
    """The UI sends every field on save; unchanged boot-read fields and an
    unchanged owner must NOT flag restart_required (it was always-True)."""
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text('{"slack": {"command": "kirocrew"}}', encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.delenv("KIROCREW_OWNER_ID", raising=False)

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def _run(payload):
        app = web.Application()
        app.router.add_put("/api/slack/config", mod.api_slack_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/slack/config", json=payload)
            return await resp.json()

    # Unchanged command + empty owner + live-applied toggle: no restart.
    body = asyncio.run(_run({"command": "kirocrew", "owner_id": "", "reactions_enabled": True}))
    assert body["restart_required"] is False
    # Changed command (boot-read): restart.
    body = asyncio.run(_run({"command": "myclaw"}))
    assert body["restart_required"] is True
