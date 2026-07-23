"""Tests for the Webex config API handlers (GET/PUT /api/webex/config)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp.test_utils import make_mocked_request

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as mod


def test_save_denies_non_loopback(monkeypatch) -> None:
    """Config writes are loopback-only: remote sessions are read-only."""
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
    req = make_mocked_request(
        "PUT",
        "/api/webex/config",
        payload=b'{"enabled": true, "bot_token": "planted"}',
        headers={"Content-Type": "application/json"},
    )
    resp = asyncio.run(mod.api_webex_config_save(req))
    assert resp.status == 403


class _StubRequest:
    """Request double for the save handler: real ``json()``, ``get()``."""

    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body

    def get(self, key: str, default=None):
        return default


def _save(monkeypatch, tmp_path: Path, body: dict, *, verify=None):
    """Drive api_webex_config_save against isolated .env + config.json."""
    env = tmp_path / ".env"
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _fake_verify(token: str):
        if verify is None:
            return None
        if isinstance(verify, Exception):
            raise verify
        return verify

    monkeypatch.setattr(mod, "_validate_webex_token", _fake_verify)
    resp = asyncio.run(mod.api_webex_config_save(_StubRequest(body)))
    return resp, env, cfg_path


class TestSave:
    def test_saves_token_and_config(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {
                "bot_token": "webex-tok-1234",
                "enabled": True,
                "allowed_emails": ["kyle@example.com"],
            },
        )
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["ok"] is True
        assert payload["restart_required"] is True
        assert "WEBEX_BOT_TOKEN=webex-tok-1234" in env.read_text(encoding="utf-8")
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["webex"] == {"enabled": True, "allowed_emails": ["kyle@example.com"]}
        # Live process env kept in sync so GET reflects the new token pre-restart.
        import os

        assert os.environ.get("WEBEX_BOT_TOKEN") == "webex-tok-1234"
        monkeypatch.delenv("WEBEX_BOT_TOKEN", raising=False)

    def test_rejected_token_blocks_save(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {"bot_token": "bad-token", "enabled": True},
            verify="invalid_token (http 401)",
        )
        assert resp.status == 400
        assert not env.exists()  # nothing persisted
        assert not cfg_path.exists()

    def test_unreachable_webex_saves_with_warning(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, _ = _save(
            monkeypatch,
            tmp_path,
            {"bot_token": "webex-tok-5678"},
            verify=RuntimeError("network down"),
        )
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["verify_warning"]  # saved, but flagged unverified
        assert "WEBEX_BOT_TOKEN=webex-tok-5678" in env.read_text(encoding="utf-8")
        monkeypatch.delenv("WEBEX_BOT_TOKEN", raising=False)

    def test_token_clear_removes_env_key(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("WEBEX_BOT_TOKEN=old\n", encoding="utf-8")
        resp, env, _ = _save(monkeypatch, tmp_path, {"bot_token_clear": True})
        assert resp.status == 200
        assert "WEBEX_BOT_TOKEN" not in env.read_text(encoding="utf-8")

    def test_invalid_email_rejected(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, cfg_path = _save(monkeypatch, tmp_path, {"allowed_emails": ["not-an-email"]})
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_token_with_whitespace_rejected(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, _ = _save(monkeypatch, tmp_path, {"bot_token": "has space"})
        assert resp.status == 400
        assert not env.exists()

    def test_enabled_must_be_boolean(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, cfg_path = _save(monkeypatch, tmp_path, {"enabled": "yes"})
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_pasted_env_line_is_stripped(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, _ = _save(monkeypatch, tmp_path, {"bot_token": "WEBEX_BOT_TOKEN=webex-tok-9"})
        assert resp.status == 200
        assert "WEBEX_BOT_TOKEN=webex-tok-9" in env.read_text(encoding="utf-8")
        monkeypatch.delenv("WEBEX_BOT_TOKEN", raising=False)

    def test_noop_save_requires_no_restart(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = _save(monkeypatch, tmp_path, {"enabled": False, "allowed_emails": []})
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["restart_required"] is False

    def test_token_set_purges_legacy_config_token(self, monkeypatch, tmp_path: Path) -> None:
        """A stale plaintext webex.bot_token in config.json is purged when the
        credential moves to .env, so it can never shadow the .env value."""
        (tmp_path / "config.json").write_text(
            json.dumps({"webex": {"enabled": True, "bot_token": "legacy-plaintext"}}),
            encoding="utf-8",
        )
        resp, env, cfg_path = _save(monkeypatch, tmp_path, {"bot_token": "webex-tok-new"})
        assert resp.status == 200
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["webex"]["bot_token"] == ""  # legacy copy gone
        assert "WEBEX_BOT_TOKEN=webex-tok-new" in env.read_text(encoding="utf-8")
        monkeypatch.delenv("WEBEX_BOT_TOKEN", raising=False)

    def test_token_clear_purges_legacy_config_token(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"webex": {"bot_token": "legacy-plaintext"}}), encoding="utf-8"
        )
        (tmp_path / ".env").write_text("WEBEX_BOT_TOKEN=old\n", encoding="utf-8")
        resp, env, cfg_path = _save(monkeypatch, tmp_path, {"bot_token_clear": True})
        assert resp.status == 200
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["webex"]["bot_token"] == ""  # cleared everywhere
        assert "WEBEX_BOT_TOKEN" not in env.read_text(encoding="utf-8")
