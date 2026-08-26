"""Tests for Slack config API helpers (secret masking + .env updates)."""

from __future__ import annotations

import asyncio
import json
import os
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
    # No stray temp files left behind in the dir. The persistent `.env.lock`
    # advisory-lock sibling (shared by all .env writers) is expected and is not
    # a temp leftover, so only the atomic-write temp pattern is checked.
    assert not any(
        p.name.startswith(".env.") and p.name.endswith(".tmp") for p in tmp_path.iterdir()
    )


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
    status_body, env = _client_put(mod, monkeypatch, tmp_path, {"bot_token": "xoxb-bad"})
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
    status_body, env = _client_put(mod, monkeypatch, tmp_path, {"bot_token": "xoxb-offline"})
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


def test_webex_held_env_lock_leaves_legacy_credential_intact(tmp_path, monkeypatch) -> None:
    """When the .env lock is held (concurrent import), _write_env_updates raises
    OSError.  The Webex save handler must NOT purge the legacy config.json
    ``webex.bot_token`` fallback before the .env write succeeds: a held lock
    must leave the pre-existing credential intact in both stores."""
    import asyncio
    import json

    import kiro_crew.config.loader as loader
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text("WEBEX_BOT_TOKEN=old-env-token\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"webex": {"bot_token": "legacy-plaintext", "enabled": True}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    # Simulate a held .env lock: _write_env_updates raises OSError.
    def _write_env_raises(_updates):
        raise OSError("env is locked by another process")

    monkeypatch.setattr(mod, "_write_env_updates", _write_env_raises)

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def _run():
        app = web.Application()
        app.router.add_put("/api/webex/config", mod.api_webex_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/webex/config",
                json={"bot_token": "new-token"},
            )
            return resp.status

    status = asyncio.run(_run())
    # The handler must propagate the OSError (unhandled = 500).
    assert status >= 400

    # CRITICAL: the legacy bot_token in config.json must NOT have been cleared —
    # the pre-existing credential must survive the failed write.
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert (
        saved["webex"]["bot_token"] == "legacy-plaintext"
    ), "legacy config.json credential was purged even though .env write failed"


def test_teams_held_env_lock_leaves_legacy_credential_intact(tmp_path, monkeypatch) -> None:
    """When the .env lock is held (concurrent import), _write_env_updates raises
    OSError.  The Teams save handler must NOT purge the legacy config.json
    ``teams.app_password`` before the .env write succeeds: a held lock must
    leave the pre-existing credential intact in both stores."""
    import asyncio
    import json

    import kiro_crew.config.loader as loader
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text("MICROSOFT_APP_PASSWORD=old-pass\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "teams": {
                    "app_password": "legacy-pass",
                    "app_id": "app-123",
                    "enabled": True,
                    "allowed_emails": ["admin@example.com"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    # Simulate a held .env lock: asyncio.to_thread(_write_env_updates, ...) raises.
    _orig_to_thread = asyncio.to_thread

    async def _to_thread_raise(fn, *args, **kwargs):
        if fn is mod._write_env_updates:
            raise OSError("env is locked by another process")
        return await _orig_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _to_thread_raise)

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def _run():
        app = web.Application()
        app.router.add_put("/api/teams/config", mod.api_teams_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/teams/config",
                json={"app_password": "new-pass"},
            )
            return resp.status

    status = asyncio.run(_run())
    # The handler must propagate the OSError (unhandled = 500).
    assert status >= 400

    # CRITICAL: the legacy app_password in config.json must NOT have been cleared.
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert (
        saved["teams"]["app_password"] == "legacy-pass"
    ), "legacy config.json credential was purged even though .env write failed"


def test_teams_config_json_write_failure_leaves_consistent_pair(tmp_path, monkeypatch) -> None:
    """Teams SET that rotates password+app_id: if _atomic_json_write raises,
    .env must be UNTOUCHED — the config-first ordering means a config-write
    failure leaves old-credential + old-metadata, always a consistent pair.

    Failure scenario (config-first ordering):
      1. config.json write attempted — raises (e.g. disk full).
      2. .env is never written (handler raised before reaching _commit_env).
      3. Both stores still hold their original values.
      -> Restart sees old-pass + old app_id/tenant: consistent, no auth failure.
    """
    import json

    import kiro_crew.agent as agent_mod
    import kiro_crew.config.loader as loader
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text("MICROSOFT_APP_PASSWORD=old-pass\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "teams": {
                    "app_id": "old-app-id",
                    "tenant_id": "old-tenant",
                    "enabled": True,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    # _atomic_json_write is imported locally inside the handler function via
    # `from kiro_crew.agent import _atomic_json_write`.  Patch the source module
    # so the local import picks up the stub.
    def _boom(path, data, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(agent_mod, "_atomic_json_write", _boom)

    # Track _write_env_updates calls: with config-first ordering, .env must
    # never be written when the config write fails.
    _write_env_calls: list[dict] = []
    _orig_write_env = mod._write_env_updates

    def _track_write_env(updates):
        _write_env_calls.append(dict(updates))
        _orig_write_env(updates)

    monkeypatch.setattr(mod, "_write_env_updates", _track_write_env)

    # Skip Azure credential verification (network call) — not the focus here.
    async def _mock_validate(*a, **kw):
        return None  # no error -> verified_triple is set

    monkeypatch.setattr(mod, "_validate_teams_app_credentials", _mock_validate)

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def _run():
        app = web.Application()
        app.router.add_put("/api/teams/config", mod.api_teams_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/teams/config",
                json={
                    "app_password": "new-pass",
                    "app_id": "new-app-id",
                },
            )
            return resp.status

    status = asyncio.run(_run())

    # Handler must propagate the OSError as an HTTP 500.
    assert status == 500, f"Expected 500, got {status}"

    # CRITICAL: .env must NOT have been written at all — config write failed
    # before the handler reached _commit_env().
    assert _write_env_calls == [], (
        "Config-first ordering: _write_env_updates must not be called when "
        f"_atomic_json_write raises; got calls: {_write_env_calls}"
    )

    # .env must still hold the original password, untouched.
    env_text = env.read_text(encoding="utf-8")
    assert "old-pass" in env_text, f"Expected .env to retain old-pass, got: {env_text!r}"
    assert (
        "new-pass" not in env_text
    ), f"new-pass must not appear in .env when config write failed; got: {env_text!r}"

    # config.json must be unchanged (the atomic write raised before persisting).
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert (
        saved["teams"].get("app_id") == "old-app-id"
    ), f"config.json must not have been mutated; got: {saved}"


def test_teams_config_write_failure_preserves_process_only_credential(
    tmp_path, monkeypatch
) -> None:
    """Teams SET with config-write failure must NOT clobber a credential that
    lives ONLY in os.environ (not in .env).

    Scenario:
      - os.environ["MICROSOFT_APP_PASSWORD"] = "env-only-pass"  (process-only)
      - .env file has no MICROSOFT_APP_PASSWORD entry at all
      - _atomic_json_write raises (disk full)

    With config-first ordering: config write fails before _commit_env() is
    called, so os.environ is never mutated by the handler.  The process-only
    credential is trivially preserved — no special rollback logic needed.
    """
    import json

    import kiro_crew.agent as agent_mod
    import kiro_crew.config.loader as loader
    import kiro_crew.dashboard.handlers.messaging as mod

    # .env has NO password entry — credential exists only in os.environ.
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "teams": {
                    "app_id": "old-app-id",
                    "tenant_id": "old-tenant",
                    "enabled": True,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    # Plant the credential ONLY in os.environ, not in .env.
    monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "env-only-pass")

    def _boom(path, data, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(agent_mod, "_atomic_json_write", _boom)

    async def _mock_validate(*a, **kw):
        return None

    monkeypatch.setattr(mod, "_validate_teams_app_credentials", _mock_validate)

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def _run():
        app = web.Application()
        app.router.add_put("/api/teams/config", mod.api_teams_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/teams/config",
                json={"app_password": "new-pass", "app_id": "new-app-id"},
            )
            return resp.status

    status = asyncio.run(_run())
    assert status == 500, f"Expected 500, got {status}"

    # CRITICAL: the process-only credential must survive intact.
    # With config-first ordering the handler never reaches _commit_env(), so
    # os.environ is untouched by the failed request.
    assert os.environ.get("MICROSOFT_APP_PASSWORD") == "env-only-pass", (
        "Config-first ordering: process-only credential must be untouched when "
        f"config write fails; got: {os.environ.get('MICROSOFT_APP_PASSWORD')!r}"
    )
    # .env must NOT hold the new credential value — .env was never written.
    assert "new-pass" not in env.read_text(
        encoding="utf-8"
    ), ".env must not be written when config write fails (config-first ordering)"


def test_teams_legacy_config_only_password_preserved_on_metadata_only_save(
    tmp_path, monkeypatch
) -> None:
    """Finding 1 regression: editing only app_id when the password lives ONLY in
    legacy config.json (not in .env or os.environ) must NOT cause the password to
    be treated as absent, skipping verification and allowing Phase-2 to purge it.

    Scenario:
      - config.json has teams.app_password = "legacy-only-pass" (no .env entry)
      - operator edits only app_id (no new password in the request body)

    Expected: verification is called with the legacy password (not empty), and
    after a successful save the password is NOT purged from config.json.
    """
    import asyncio
    import json

    import kiro_crew.config.loader as loader
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")  # no MICROSOFT_APP_PASSWORD in .env
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "teams": {
                    "app_password": "legacy-only-pass",
                    "app_id": "old-app-id",
                    "tenant_id": "t-1",
                    "enabled": True,
                    "allowed_emails": ["admin@example.com"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    # Capture what password is passed to Azure verification.
    verify_calls: list[tuple[str, str, str]] = []

    async def _mock_validate(app_id: str, app_password: str, tenant_id: str):
        verify_calls.append((app_id, app_password, tenant_id))
        return None  # no error — credentials accepted

    monkeypatch.setattr(mod, "_validate_teams_app_credentials", _mock_validate)

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def _run():
        app = web.Application()
        app.router.add_put("/api/teams/config", mod.api_teams_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/teams/config",
                json={"app_id": "new-app-id"},  # only app_id changed, no password
            )
            return resp.status

    status = asyncio.run(_run())
    assert status == 200, f"Expected 200, got {status}"

    # CRITICAL: verification must have been called with the legacy password, not "".
    assert len(verify_calls) == 1, f"Expected one verify call, got: {verify_calls}"
    _verified_app_id, _verified_pw, _verified_tenant = verify_calls[0]
    assert _verified_pw == "legacy-only-pass", (
        f"Verification was called with empty/wrong password '{_verified_pw}' "
        "instead of the legacy config.json password; password-only-in-config "
        "path is broken (Finding 1)"
    )

    # The password must NOT have been purged from config.json after the save.
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    # The handler purges app_password from config.json once the .env write
    # succeeds (moving it to .env), so it IS expected to be cleared/empty here
    # only when the password was newly written to .env.  In this test no new
    # password was supplied, so no .env write for the password occurred, and the
    # legacy value must survive.
    env_text = env.read_text(encoding="utf-8")
    # Either the password stays in config.json OR it was migrated to .env; the
    # important invariant is that it does NOT simply disappear (be set to "").
    legacy_still_in_cfg = saved["teams"].get("app_password") == "legacy-only-pass"
    migrated_to_env = "legacy-only-pass" in env_text
    assert legacy_still_in_cfg or migrated_to_env, (
        "Legacy config.json password was lost after a metadata-only save "
        f"(app_id edit). config: {saved['teams'].get('app_password')!r}, "
        f"env: {env_text!r}"
    )


def test_teams_cancellation_after_successful_env_write_does_not_rollback_config(
    tmp_path, monkeypatch
) -> None:
    """Finding 2 regression: if the request is cancelled AFTER the .env write has
    already committed, the config metadata must NOT be rolled back.

    Rolling config back after a successful .env write leaves new .env paired
    with old config — the exact mismatch the rollback is supposed to prevent.

    Scenario:
      - Teams save with a new password triggers _write_env_off_loop
      - The env write completes (actually writes to .env), then CancelledError
        is raised to the handler (post-commit cancellation)

    Expected: config.json retains the NEW app_id (written before .env); it is
    NOT rolled back to the original.
    """
    import asyncio
    import json

    import kiro_crew.config.loader as loader
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text("MICROSOFT_APP_PASSWORD=old-pass\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "teams": {
                    "app_id": "old-app-id",
                    "tenant_id": "t-1",
                    "enabled": True,
                    "allowed_emails": ["admin@example.com"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _mock_validate(*a, **kw):
        return None  # accepted

    monkeypatch.setattr(mod, "_validate_teams_app_credentials", _mock_validate)

    # Replace _write_env_off_loop with a version that SUCCESSFULLY writes the
    # .env and THEN raises CancelledError (simulating cancellation-after-commit).
    _orig_write_env_updates = mod._write_env_updates

    async def _write_env_succeeds_then_cancels(updates):
        # Actually write to .env so the credential is committed on disk.
        _orig_write_env_updates(updates)
        # Simulate cancellation arriving after the write completed.
        raise asyncio.CancelledError("simulated post-commit cancellation")

    monkeypatch.setattr(mod, "_write_env_off_loop", _write_env_succeeds_then_cancels)

    class _FakeRequest:
        app: dict = {"state": None}

        async def json(self) -> dict:
            return {"app_id": "new-app-id", "app_password": "new-pass"}

        def get(self, key, default=None):
            return default

    _got_cancelled = False

    async def _run():
        nonlocal _got_cancelled
        try:
            await mod.api_teams_config_save(_FakeRequest())
        except asyncio.CancelledError:
            _got_cancelled = True

    asyncio.run(_run())
    assert _got_cancelled, "Expected CancelledError to propagate from the handler"

    # CRITICAL: config.json must NOT have been rolled back.
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["teams"].get("app_id") == "new-app-id", (
        "Config was incorrectly rolled back after cancellation-after-successful-env-write. "
        f"Expected new-app-id, got: {saved['teams'].get('app_id')!r} (Finding 2)"
    )
    # .env must have the new password (the write actually committed).
    env_text = env.read_text(encoding="utf-8")
    assert (
        "new-pass" in env_text
    ), f"Expected .env to contain new-pass after successful write; got: {env_text!r}"
