"""Tests for the WeCom config API (loopback gate, validation, persistence)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from aiohttp.test_utils import make_mocked_request

import kiro_crew.config.loader as loader

BOT_ID = "wxb-1234567890abcdef"
SECRET = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd"


def test_save_denies_non_loopback(monkeypatch) -> None:
    """Config writes are loopback-only: remote sessions are read-only."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
    req = make_mocked_request(
        "PUT",
        "/api/wecom/config",
        payload=b'{"bot_token": "planted-secret-value"}',
        headers={"Content-Type": "application/json"},
    )
    resp = asyncio.run(mod.api_wecom_config_save(req))
    assert resp.status == 403


def test_save_denies_forwarded_loopback_request() -> None:
    """A reverse-proxied request (loopback peer + XFF) cannot plant secrets."""
    import kiro_crew.dashboard.handlers.messaging as mod

    req = make_mocked_request(
        "PUT",
        "/api/wecom/config",
        payload=b'{"bot_token": "planted-secret-value"}',
        headers={"Content-Type": "application/json", "X-Forwarded-For": "203.0.113.7"},
    )
    resp = asyncio.run(mod.api_wecom_config_save(req))
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
        app.router.add_put("/api/wecom/config", mod.api_wecom_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/wecom/config", json=body)
            return resp.status, await resp.json()

    return asyncio.run(_run()), env


def test_save_persists_credentials_and_config(tmp_path: Path, monkeypatch) -> None:
    """Both secrets land in .env (0600), config in config.json, environ synced."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    monkeypatch.delenv("WECOM_SECRET", raising=False)
    (status_body, env) = _client_put(
        mod,
        monkeypatch,
        tmp_path,
        {
            "bot_id": BOT_ID,
            "bot_token": SECRET,
            "enabled": True,
            "allowed_user_ids": ["zhangsan", "li.si-01@corp"],
            "soft_threshold_pct": 75,
        },
    )
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is True
    env_text = env.read_text(encoding="utf-8")
    assert f"WECOM_BOT_ID={BOT_ID}" in env_text
    assert f"WECOM_SECRET={SECRET}" in env_text
    assert (env.stat().st_mode & 0o077) == 0
    assert os.environ["WECOM_BOT_ID"] == BOT_ID
    assert os.environ["WECOM_SECRET"] == SECRET
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["wecom"]["enabled"] is True
    assert cfg["wecom"]["allowed_users"] == [
        {"userid": "zhangsan", "name": ""},
        {"userid": "li.si-01@corp", "name": ""},
    ]
    assert cfg["wecom"]["soft_threshold_pct"] == 75


def test_save_rejects_whitespace_credentials(tmp_path: Path, monkeypatch) -> None:
    """A secret carrying inner whitespace fails before any write."""
    import kiro_crew.dashboard.handlers.messaging as mod

    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token": "two words"})
    status, body = status_body
    assert status == 400
    assert "whitespace" in body["error"]
    assert "two" not in env.read_text(encoding="utf-8")


def test_save_rejects_invalid_userid(tmp_path: Path, monkeypatch) -> None:
    """Userids outside the WeCom charset fail closed, nothing persisted."""
    import kiro_crew.dashboard.handlers.messaging as mod

    (status_body, _env) = _client_put(
        mod, monkeypatch, tmp_path, {"allowed_user_ids": ["zhang san"]}
    )
    status, body = status_body
    assert status == 400
    assert "invalid WeCom userid" in body["error"]
    assert not (tmp_path / "config.json").exists()


def test_save_rejects_non_ascii_userid(tmp_path: Path, monkeypatch) -> None:
    """Unicode letters/digits are rejected: str.isalnum() alone would admit
    them, but they can never match a real WeCom userid — the entry would sit
    in the allow-list looking authoritative while granting nothing."""
    import kiro_crew.dashboard.handlers.messaging as mod

    for bad in ("张三", "ｚｈａｎｇｓａｎ", "user\u200bname"):
        (status_body, _env) = _client_put(
            mod, monkeypatch, tmp_path, {"allowed_user_ids": [bad]}
        )
        status, body = status_body
        assert status == 400, bad
        assert "invalid WeCom userid" in body["error"]


def test_allowlist_preserves_display_names(tmp_path: Path, monkeypatch) -> None:
    """Surviving entries keep their stored names; removed entries drop."""
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "wecom": {
                    "allowed_users": [
                        {"userid": "zhangsan", "name": "Zhang San"},
                        {"userid": "lisi", "name": "Li Si"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (status_body, _env) = _client_put(
        mod, monkeypatch, tmp_path, {"allowed_user_ids": ["zhangsan", "wangwu"]}
    )
    status, _body = status_body
    assert status == 200
    out = json.loads(cfg.read_text(encoding="utf-8"))
    assert out["wecom"]["allowed_users"] == [
        {"userid": "zhangsan", "name": "Zhang San"},
        {"userid": "wangwu", "name": ""},
    ]


def test_clear_credentials(tmp_path: Path, monkeypatch) -> None:
    """bot_token_clear / bot_id_clear remove secrets from .env and environ."""
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text(f"WECOM_BOT_ID={BOT_ID}\nWECOM_SECRET={SECRET}\n", encoding="utf-8")
    monkeypatch.setenv("WECOM_BOT_ID", BOT_ID)
    monkeypatch.setenv("WECOM_SECRET", SECRET)
    (status_body, env) = _client_put(
        mod, monkeypatch, tmp_path, {"bot_token_clear": True, "bot_id_clear": True}
    )
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is True
    env_text = env.read_text(encoding="utf-8")
    assert "WECOM_BOT_ID" not in env_text or f"WECOM_BOT_ID={BOT_ID}" not in env_text
    assert f"WECOM_SECRET={SECRET}" not in env_text
    assert "WECOM_BOT_ID" not in os.environ
    assert "WECOM_SECRET" not in os.environ


def test_clear_flag_must_be_strict_boolean(tmp_path: Path, monkeypatch) -> None:
    """Truthy non-boolean clear flags are rejected (no coercion surprises)."""
    import kiro_crew.dashboard.handlers.messaging as mod

    (status_body, _env) = _client_put(mod, monkeypatch, tmp_path, {"bot_id_clear": "yes"})
    status, body = status_body
    assert status == 400
    assert "boolean" in body["error"]


def test_env_line_paste_is_stripped(tmp_path: Path, monkeypatch) -> None:
    """Pasting a full env line (KEY=value) stores just the value."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.delenv("WECOM_SECRET", raising=False)
    (status_body, env) = _client_put(
        mod, monkeypatch, tmp_path, {"bot_token": f"WECOM_SECRET={SECRET}"}
    )
    status, _body = status_body
    assert status == 200
    assert f"WECOM_SECRET={SECRET}\n" in env.read_text(encoding="utf-8")
    assert f"WECOM_SECRET=WECOM_SECRET={SECRET}" not in env.read_text(encoding="utf-8")


def test_allow_all_users_save_and_strict_boolean(tmp_path: Path, monkeypatch) -> None:
    """allow_all_users persists as a strict boolean; truthy strings rejected."""
    import kiro_crew.dashboard.handlers.messaging as mod

    (status_body, _env) = _client_put(mod, monkeypatch, tmp_path, {"allow_all_users": True})
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is True
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["wecom"]["allow_all_users"] is True

    (status_body, _env) = _client_put(mod, monkeypatch, tmp_path, {"allow_all_users": "yes"})
    status, body = status_body
    assert status == 400
    assert "boolean" in body["error"]


def test_get_allow_all_counts_as_configured(tmp_path: Path, monkeypatch) -> None:
    """With allow-all on, an empty allow-list still reports configured."""
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text(f"WECOM_BOT_ID={BOT_ID}\nWECOM_SECRET={SECRET}\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"wecom": {"enabled": True, "allow_all_users": True}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    monkeypatch.delenv("WECOM_SECRET", raising=False)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    class _State:
        wecom_connected = False
        wecom_connect_error = ""

    req = make_mocked_request("GET", "/api/wecom/config", app={"state": _State()})
    resp = asyncio.run(mod.api_wecom_config_get(req))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["allow_all_users"] is True
    assert body["allowed_user_ids"] == []
    assert body["configured"] is True  # allow-all substitutes for the list


def test_restart_required_only_on_actual_change(tmp_path: Path, monkeypatch) -> None:
    """A no-op save (same values) reports restart_required False."""
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "wecom": {
                    "enabled": True,
                    "allowed_users": [{"userid": "zhangsan", "name": ""}],
                    "soft_threshold_pct": 80,
                }
            }
        ),
        encoding="utf-8",
    )
    (status_body, _env) = _client_put(
        mod,
        monkeypatch,
        tmp_path,
        {"enabled": True, "allowed_user_ids": ["zhangsan"], "soft_threshold_pct": 80},
    )
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is False


def test_soft_threshold_bounds(tmp_path: Path, monkeypatch) -> None:
    """soft_threshold_pct outside [1, 100] is rejected."""
    import kiro_crew.dashboard.handlers.messaging as mod

    for bad in (0, 101, True, "80"):
        (status_body, _env) = _client_put(
            mod, monkeypatch, tmp_path, {"soft_threshold_pct": bad}
        )
        status, body = status_body
        assert status == 400
        # A machine-readable code, not prose only: the dashboard renders `error`
        # verbatim into a localized UI, so prose alone is untranslatable.
        assert body["code"] == "soft_threshold_pct_invalid"


def test_get_masks_secrets_and_reports_state(tmp_path: Path, monkeypatch) -> None:
    """GET returns presence + masked previews, never the raw credentials."""
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text(f"WECOM_BOT_ID={BOT_ID}\nWECOM_SECRET={SECRET}\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "wecom": {
                    "enabled": True,
                    "allowed_users": [{"userid": "zhangsan", "name": "Zhang San"}],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    monkeypatch.delenv("WECOM_SECRET", raising=False)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    class _State:
        wecom_connected = False
        wecom_connect_error = ""

    req = make_mocked_request("GET", "/api/wecom/config", app={"state": _State()})
    resp = asyncio.run(mod.api_wecom_config_get(req))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["bot_token_set"] is True
    assert body["bot_id_set"] is True
    assert SECRET not in resp.text  # raw secret never returned
    assert body["bot_token_preview"].endswith(SECRET[-4:])
    assert body["configured"] is True  # both creds + enabled + allowlist
    assert body["connected"] is False
    assert body["enabled"] is True
    assert body["allowed_user_ids"] == ["zhangsan"]


def test_get_unconfigured_reports_needs_setup(tmp_path: Path, monkeypatch) -> None:
    """GET on a pristine home reports nothing set and not configured."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setattr(loader, "env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    monkeypatch.delenv("WECOM_SECRET", raising=False)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    class _State:
        wecom_connected = False
        wecom_connect_error = ""

    req = make_mocked_request("GET", "/api/wecom/config", app={"state": _State()})
    resp = asyncio.run(mod.api_wecom_config_get(req))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["bot_token_set"] is False
    assert body["bot_id_set"] is False
    assert body["configured"] is False
    assert body["enabled"] is False
    assert body["allowed_user_ids"] == []


def test_clear_config_write_failure_does_not_leave_env_cleared(
    tmp_path: Path, monkeypatch
) -> None:
    """On a CLEAR-only path (no config changes), the env write runs via
    _write_env_off_loop.  If that write raises, .env must remain untouched —
    the credential stays fully present, never half-cleared."""
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    cfg_path = tmp_path / "config.json"
    env.write_text(f"WECOM_SECRET={SECRET}\n", encoding="utf-8")
    cfg_path.write_text(json.dumps({"wecom": {"enabled": True}}), encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.delenv("WECOM_SECRET", raising=False)

    async def _boom(_updates):
        raise OSError("disk full writing .env")

    monkeypatch.setattr(mod, "_write_env_off_loop", _boom)
    try:
        _client_put(mod, monkeypatch, tmp_path, {"bot_token_clear": True})
    except Exception:
        pass
    # .env must still hold the credential — the env write failed before any
    # content was cleared.
    assert f"WECOM_SECRET={SECRET}" in env.read_text(encoding="utf-8")
    monkeypatch.delenv("WECOM_SECRET", raising=False)


def test_set_config_write_failure_leaves_consistent_pair(
    tmp_path: Path, monkeypatch
) -> None:
    """On a SET, config.json is written FIRST.  If it fails, .env is never
    touched — old credentials + old metadata, always a consistent pair.
    This mirrors the Teams and Webex config-first SET pattern.

    The config write only fires on the SET path when `staged` is non-empty
    (i.e. at least one config field differs from stored).  This test includes
    ``enabled: True`` in the body to ensure `staged` has an entry and the
    config-write failure path is exercised."""
    import kiro_crew.agent as _agent
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    cfg_path = tmp_path / "config.json"
    env.write_text("WECOM_BOT_ID=old-bot-id\nWECOM_SECRET=old-secret\n", encoding="utf-8")
    # enabled=False initially so sending enabled=True creates a staged change,
    # giving the handler a non-empty `staged` and thus a config write to fail.
    cfg_path.write_text(json.dumps({"wecom": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    monkeypatch.delenv("WECOM_SECRET", raising=False)

    def _boom(*_a, **_k):
        raise OSError("disk full during config write")

    monkeypatch.setattr(_agent, "_atomic_json_write", _boom)
    try:
        _client_put(
            mod,
            monkeypatch,
            tmp_path,
            {"bot_id": BOT_ID, "secret": SECRET, "enabled": True},
        )
    except Exception:
        pass

    # CRITICAL: .env must be untouched — config write failed before the
    # handler reached _commit_env_wecom(), so both stores still hold originals.
    env_text = env.read_text(encoding="utf-8")
    assert "old-bot-id" in env_text, (
        "Config-first: .env must be untouched when config write fails (WECOM_BOT_ID)"
    )
    assert "old-secret" in env_text, (
        "Config-first: .env must be untouched when config write fails (WECOM_SECRET)"
    )
    assert BOT_ID not in env_text, ".env must not hold new bot_id when config write failed"
    assert SECRET not in env_text, ".env must not hold new secret when config write failed"
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    monkeypatch.delenv("WECOM_SECRET", raising=False)


def test_set_config_write_failure_preserves_process_only_credential(
    tmp_path: Path, monkeypatch
) -> None:
    """WeCom SET with config-write failure must NOT clobber credentials that
    live ONLY in os.environ (not in .env).

    Scenario:
      - os.environ["WECOM_BOT_ID"]  = "env-only-bot-id"  (process-only)
      - os.environ["WECOM_SECRET"]  = "env-only-secret"   (process-only)
      - .env file has no WECOM_* entries at all
      - _atomic_json_write raises (disk full)

    With config-first ordering: the config write fails before
    _commit_env_wecom() is called, so os.environ is never mutated by the
    handler.  Both process-only credentials are trivially preserved.
    """
    import kiro_crew.agent as _agent
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    cfg_path = tmp_path / "config.json"
    # .env has NO WECOM entries — credentials are process-only.
    env.write_text("", encoding="utf-8")
    # enabled=False so sending enabled=True creates a staged change and
    # forces the config write (and its failure).
    cfg_path.write_text(json.dumps({"wecom": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    # Plant both credentials ONLY in os.environ, not in .env.
    monkeypatch.setenv("WECOM_BOT_ID", "env-only-bot-id")
    monkeypatch.setenv("WECOM_SECRET", "env-only-secret")

    def _boom(*_a, **_k):
        raise OSError("disk full during config write")

    monkeypatch.setattr(_agent, "_atomic_json_write", _boom)
    try:
        _client_put(
            mod,
            monkeypatch,
            tmp_path,
            {"bot_id": BOT_ID, "secret": SECRET, "enabled": True},
        )
    except Exception:
        pass

    # CRITICAL: both process-only credentials must survive intact.
    # Config-first ordering: _commit_env_wecom() is never called when config
    # write fails, so os.environ is untouched by the failed request.
    assert os.environ.get("WECOM_BOT_ID") == "env-only-bot-id", (
        "Config-first ordering: process-only WECOM_BOT_ID must be untouched "
        f"when config write fails; got: {os.environ.get('WECOM_BOT_ID')!r}"
    )
    assert os.environ.get("WECOM_SECRET") == "env-only-secret", (
        "Config-first ordering: process-only WECOM_SECRET must be untouched "
        f"when config write fails; got: {os.environ.get('WECOM_SECRET')!r}"
    )
    # .env must NOT hold the new credential values — .env was never written.
    env_text = env.read_text(encoding="utf-8")
    assert BOT_ID not in env_text, ".env must not be written when config write fails (WECOM_BOT_ID)"
    assert SECRET not in env_text, ".env must not be written when config write fails (WECOM_SECRET)"
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    monkeypatch.delenv("WECOM_SECRET", raising=False)
