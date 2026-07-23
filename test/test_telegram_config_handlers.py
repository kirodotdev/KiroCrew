"""Tests for the Telegram config API (loopback gate, validation, persistence)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from aiohttp.test_utils import make_mocked_request

import kiro_crew.config.loader as loader


def test_save_denies_non_loopback(monkeypatch) -> None:
    """Config writes are loopback-only: remote sessions are read-only."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
    req = make_mocked_request(
        "PUT",
        "/api/telegram/config",
        payload=b'{"bot_token": "12345:planted-token-value"}',
        headers={"Content-Type": "application/json"},
    )
    resp = asyncio.run(mod.api_telegram_config_save(req))
    assert resp.status == 403


def test_save_denies_forwarded_loopback_request() -> None:
    """A reverse-proxied request (loopback peer + XFF) cannot plant tokens."""
    import kiro_crew.dashboard.handlers.messaging as mod

    req = make_mocked_request(
        "PUT",
        "/api/telegram/config",
        payload=b'{"bot_token": "12345:planted-token-value"}',
        headers={"Content-Type": "application/json", "X-Forwarded-For": "203.0.113.7"},
    )
    resp = asyncio.run(mod.api_telegram_config_save(req))
    assert resp.status == 403


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
        app.router.add_put("/api/telegram/config", mod.api_telegram_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/telegram/config", json=body)
            return resp.status, await resp.json()

    return asyncio.run(_run()), env


VALID_TOKEN = "110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def _accept_token(monkeypatch, mod) -> None:
    async def _accept(token):
        return None

    monkeypatch.setattr(mod, "_validate_telegram_token", _accept)


def test_save_persists_token_and_config(tmp_path: Path, monkeypatch) -> None:
    """Token lands in .env (0600), config in config.json, environ synced."""
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    (status_body, env) = _client_put(
        mod,
        monkeypatch,
        tmp_path,
        {
            "bot_token": VALID_TOKEN,
            "enabled": True,
            "allowed_user_ids": ["123456789", "987654321"],
            "soft_threshold_pct": 75,
        },
    )
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is True
    assert f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}" in env.read_text(encoding="utf-8")
    assert (env.stat().st_mode & 0o077) == 0
    assert os.environ["TELEGRAM_BOT_TOKEN"] == VALID_TOKEN
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["telegram"]["enabled"] is True
    assert cfg["telegram"]["allowed_user_ids"] == [123456789, 987654321]
    assert cfg["telegram"]["soft_threshold_pct"] == 75


def test_save_rejects_malformed_token(tmp_path: Path, monkeypatch) -> None:
    """A token that doesn't match <bot_id>:<secret> fails before any write."""
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token": "not-a-token"})
    status, body = status_body
    assert status == 400
    assert "BotFather" in body["error"]
    assert "not-a-token" not in env.read_text(encoding="utf-8")


def test_save_rejects_token_telegram_refuses(tmp_path: Path, monkeypatch) -> None:
    """A token Telegram rejects (Unauthorized) fails the save; nothing written."""
    import kiro_crew.dashboard.handlers.messaging as mod

    async def _reject(token):
        return "Unauthorized"

    monkeypatch.setattr(mod, "_validate_telegram_token", _reject)
    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token": VALID_TOKEN})
    status, body = status_body
    assert status == 400
    assert "Unauthorized" in body["error"]
    assert VALID_TOKEN not in env.read_text(encoding="utf-8")


def test_save_proceeds_with_warning_when_telegram_unreachable(tmp_path: Path, monkeypatch) -> None:
    """Being offline must not block a save — token stored, warning returned."""
    import kiro_crew.dashboard.handlers.messaging as mod

    async def _unreachable(token):
        raise ConnectionError("no route to api.telegram.org")

    monkeypatch.setattr(mod, "_validate_telegram_token", _unreachable)
    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token": VALID_TOKEN})
    status, body = status_body
    assert status == 200
    assert body["verify_warning"]
    assert f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}" in env.read_text(encoding="utf-8")


def test_save_rejects_non_numeric_user_ids(tmp_path: Path, monkeypatch) -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    (status_body, _) = _client_put(mod, monkeypatch, tmp_path, {"allowed_user_ids": ["@username"]})
    status, body = status_body
    assert status == 400
    assert "numeric" in body["error"]


def test_clear_also_removes_legacy_config_token(tmp_path: Path, monkeypatch) -> None:
    """Clearing the token must also drop config.json's legacy telegram.bot_token.

    The gateway falls back to that field when .env is empty, so leaving it
    behind would resurrect the removed credential on the next restart.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(
        '{"telegram": {"enabled": true, "bot_token": "999999:legacy-config-token-x"}}',
        encoding="utf-8",
    )

    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token_clear": True})
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is True
    assert "TELEGRAM_BOT_TOKEN" not in env.read_text(encoding="utf-8")
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert "bot_token" not in saved["telegram"]  # legacy fallback purged
    assert saved["telegram"]["enabled"] is True  # sibling config untouched


def test_replace_also_removes_legacy_config_token(tmp_path: Path, monkeypatch) -> None:
    """Setting a new .env token must purge the legacy config.json token so the
    old credential cannot shadow-survive a later .env clear."""
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    cfg = tmp_path / "config.json"
    cfg.write_text('{"telegram": {"bot_token": "999999:legacy-config-token-x"}}', encoding="utf-8")

    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token": VALID_TOKEN})
    status, _ = status_body
    assert status == 200
    assert f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}" in env.read_text(encoding="utf-8")
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert "bot_token" not in saved["telegram"]


def test_legacy_purge_is_persisted_before_env_write(tmp_path: Path, monkeypatch) -> None:
    """Crash-safety ordering: the config.json write (carrying the legacy
    bot_token removal) must land BEFORE the .env update. A crash between the
    two must never leave .env cleared while the legacy fallback survives to
    resurrect the revoked credential on restart."""
    import kiro_crew.agent as agent_mod
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    order: list[str] = []
    real_json_write = agent_mod._atomic_json_write
    real_env_write = mod._write_env_updates

    def _spy_json_write(path, data):
        order.append("config")
        return real_json_write(path, data)

    def _spy_env_write(updates):
        order.append("env")
        return real_env_write(updates)

    # The handler imports _atomic_json_write from kiro_crew.agent at call time.
    monkeypatch.setattr(agent_mod, "_atomic_json_write", _spy_json_write)
    monkeypatch.setattr(mod, "_write_env_updates", _spy_env_write)

    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text('{"telegram": {"bot_token": "999999:legacy-config-token-x"}}', encoding="utf-8")

    (status_body, _) = _client_put(mod, monkeypatch, tmp_path, {"bot_token_clear": True})
    assert status_body[0] == 200
    assert order == ["config", "env"]  # legacy purge persisted first


def test_clear_flag_must_be_strict_boolean(tmp_path: Path, monkeypatch) -> None:
    """Truthy non-bool clear flags (e.g. "false", 1) must not delete the token."""
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}\n", encoding="utf-8")

    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token_clear": "false"})
    assert status_body[0] == 400
    assert VALID_TOKEN in env.read_text(encoding="utf-8")

    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token_clear": True})
    assert status_body[0] == 200
    assert "TELEGRAM_BOT_TOKEN" not in env.read_text(encoding="utf-8")


def test_restart_required_only_on_actual_change(tmp_path: Path, monkeypatch) -> None:
    """Unchanged fields must NOT flag restart_required."""
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    cfg = tmp_path / "config.json"
    cfg.write_text(
        '{"telegram": {"enabled": true, "allowed_user_ids": [111], "soft_threshold_pct": 80}}',
        encoding="utf-8",
    )
    (status_body, _) = _client_put(
        mod,
        monkeypatch,
        tmp_path,
        {"enabled": True, "allowed_user_ids": ["111"], "soft_threshold_pct": 80},
    )
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is False

    (status_body, _) = _client_put(mod, monkeypatch, tmp_path, {"enabled": False})
    assert status_body[1]["restart_required"] is True


def test_soft_threshold_bounds(tmp_path: Path, monkeypatch) -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    for bad in (0, 101, "80", True):
        (status_body, _) = _client_put(mod, monkeypatch, tmp_path, {"soft_threshold_pct": bad})
        assert status_body[0] == 400, f"soft_threshold_pct={bad!r} should be rejected"


def test_get_masks_token_and_reports_state(tmp_path: Path, monkeypatch) -> None:
    """GET returns presence + masked preview, never the raw token."""
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text('{"telegram": {"enabled": true, "allowed_user_ids": [42]}}', encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    class _State:
        telegram_connected = False
        telegram_connect_error = ""

    req = make_mocked_request("GET", "/api/telegram/config", app={"state": _State()})
    resp = asyncio.run(mod.api_telegram_config_get(req))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["bot_token_set"] is True
    assert VALID_TOKEN not in resp.text  # raw token never returned
    assert body["bot_token_preview"].endswith(VALID_TOKEN[-4:])
    assert body["configured"] is True  # token + enabled + allowlist
    assert body["connected"] is False
    assert body["enabled"] is True
    assert body["allowed_user_ids"] == ["42"]
