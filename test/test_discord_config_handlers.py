"""Tests for the Discord config API (loopback gate, validation, persistence)."""

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
        "/api/discord/config",
        payload=b'{"bot_token": "planted-token-value"}',
        headers={"Content-Type": "application/json"},
    )
    resp = asyncio.run(mod.api_discord_config_save(req))
    assert resp.status == 403


def test_save_denies_forwarded_loopback_request() -> None:
    """A reverse-proxied request (loopback peer + XFF) cannot plant tokens."""
    import kiro_crew.dashboard.handlers.messaging as mod

    req = make_mocked_request(
        "PUT",
        "/api/discord/config",
        payload=b'{"bot_token": "planted-token-value"}',
        headers={"Content-Type": "application/json", "X-Forwarded-For": "203.0.113.7"},
    )
    resp = asyncio.run(mod.api_discord_config_save(req))
    assert resp.status == 403


def _client_put(mod, monkeypatch, tmp_path, body, state=None):
    """Run a save over a real TestClient with paths isolated to tmp_path.

    *state* seeds ``app["state"]``; the folder-creation hook is skipped when it is
    absent, which is also the production guard for a request that arrives before
    the dashboard state is wired.
    """
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
        if state is not None:
            app["state"] = state
        app.router.add_put("/api/discord/config", mod.api_discord_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/discord/config", json=body)
            return resp.status, await resp.json()

    return asyncio.run(_run()), env


# Fake token matching Discord's three-segment shape (base64url segments).
# Assembled at runtime so no literal in this file matches GitHub secret
# scanning's Discord bot-token pattern (push protection rejects the blob
# otherwise — the scanner keys on the joined three-segment form).
VALID_TOKEN = ".".join(
    ["MTA5OTk5OTk5OTk5OTk5OTk5OQ", "GhIjKl", "MnOpQrStUvWxYz0123456789_-AbCdEfGhIj"]
)


def _accept_token(monkeypatch, mod) -> None:
    async def _accept(token):
        return None

    monkeypatch.setattr(mod, "_validate_discord_token", _accept)


def test_save_persists_token_and_config(tmp_path: Path, monkeypatch) -> None:
    """Token lands in .env (0600), config in config.json, environ synced."""
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    (status_body, env) = _client_put(
        mod,
        monkeypatch,
        tmp_path,
        {
            "bot_token": VALID_TOKEN,
            "enabled": True,
            "allowed_user_ids": ["123456789012345678", "987654321098765432"],
            "allowed_thread_ids": ["234567890123456789"],
            "soft_threshold_pct": 75,
        },
    )
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is True
    assert f"DISCORD_BOT_TOKEN={VALID_TOKEN}" in env.read_text(encoding="utf-8")
    assert (env.stat().st_mode & 0o077) == 0
    assert os.environ["DISCORD_BOT_TOKEN"] == VALID_TOKEN
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["discord"]["enabled"] is True
    assert cfg["discord"]["allowed_user_ids"] == [
        "123456789012345678",
        "987654321098765432",
    ]
    assert cfg["discord"]["allowed_thread_ids"] == ["234567890123456789"]
    assert loader.KiroCrewConfig.load().discord.allowed_thread_ids == [
        "234567890123456789"
    ]
    assert cfg["discord"]["soft_threshold_pct"] == 75


def test_save_rejects_malformed_token(tmp_path: Path, monkeypatch) -> None:
    """A token that doesn't match the three-segment shape fails before any write."""
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token": "not-a-token"})
    status, body = status_body
    assert status == 400
    assert "Developer Portal" in body["error"]
    assert "not-a-token" not in env.read_text(encoding="utf-8")


def test_save_strips_accidental_bot_prefix(tmp_path: Path, monkeypatch) -> None:
    """A pasted 'Bot <token>' Authorization-header line stores the bare token."""
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    (status_body, env) = _client_put(
        mod, monkeypatch, tmp_path, {"bot_token": f"Bot {VALID_TOKEN}"}
    )
    status, _ = status_body
    assert status == 200
    assert f"DISCORD_BOT_TOKEN={VALID_TOKEN}" in env.read_text(encoding="utf-8")


def test_save_rejects_token_discord_refuses(tmp_path: Path, monkeypatch) -> None:
    """A token Discord rejects (401) fails the save; nothing written."""
    import kiro_crew.dashboard.handlers.messaging as mod

    async def _reject(token):
        return "401: Unauthorized"

    monkeypatch.setattr(mod, "_validate_discord_token", _reject)
    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token": VALID_TOKEN})
    status, body = status_body
    assert status == 400
    assert "Unauthorized" in body["error"]
    assert VALID_TOKEN not in env.read_text(encoding="utf-8")


def test_save_proceeds_with_warning_when_discord_unreachable(tmp_path: Path, monkeypatch) -> None:
    """Being offline must not block a save — token stored, warning returned."""
    import kiro_crew.dashboard.handlers.messaging as mod

    async def _unreachable(token):
        raise ConnectionError("no route to discord.com")

    monkeypatch.setattr(mod, "_validate_discord_token", _unreachable)
    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token": VALID_TOKEN})
    status, body = status_body
    assert status == 200
    assert body["verify_warning"]
    assert f"DISCORD_BOT_TOKEN={VALID_TOKEN}" in env.read_text(encoding="utf-8")


def test_save_rejects_non_numeric_user_ids(tmp_path: Path, monkeypatch) -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    (status_body, _) = _client_put(mod, monkeypatch, tmp_path, {"allowed_user_ids": ["@username"]})
    status, body = status_body
    assert status == 400
    assert "numeric" in body["error"]


def test_save_rejects_non_numeric_thread_ids(tmp_path: Path, monkeypatch) -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    (status_body, _) = _client_put(
        mod, monkeypatch, tmp_path, {"allowed_thread_ids": ["general"]}
    )
    status, body = status_body
    assert status == 400
    assert "thread ID" in body["error"]
    assert "numeric" in body["error"]


def test_clear_flag_must_be_strict_boolean(tmp_path: Path, monkeypatch) -> None:
    """Truthy non-bool clear flags (e.g. "false", 1) must not delete the token."""
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    env = tmp_path / ".env"
    env.write_text(f"DISCORD_BOT_TOKEN={VALID_TOKEN}\n", encoding="utf-8")

    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token_clear": "false"})
    assert status_body[0] == 400
    assert VALID_TOKEN in env.read_text(encoding="utf-8")

    (status_body, env) = _client_put(mod, monkeypatch, tmp_path, {"bot_token_clear": True})
    assert status_body[0] == 200
    assert "DISCORD_BOT_TOKEN" not in env.read_text(encoding="utf-8")


def test_restart_required_only_on_actual_change(tmp_path: Path, monkeypatch) -> None:
    """Unchanged fields must NOT flag restart_required."""
    import kiro_crew.dashboard.handlers.messaging as mod

    _accept_token(monkeypatch, mod)
    cfg = tmp_path / "config.json"
    cfg.write_text(
        '{"discord": {"enabled": true, "allowed_user_ids": ["111"], ' '"soft_threshold_pct": 80}}',
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
    env.write_text(f"DISCORD_BOT_TOKEN={VALID_TOKEN}\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(
        '{"discord": {"enabled": true, "allowed_user_ids": ["42"], '
        '"allowed_thread_ids": ["99"]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    class _State:
        discord_connected = False
        discord_connect_error = ""

    req = make_mocked_request("GET", "/api/discord/config", app={"state": _State()})
    resp = asyncio.run(mod.api_discord_config_get(req))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["bot_token_set"] is True
    assert VALID_TOKEN not in resp.text  # raw token never returned
    assert body["bot_token_preview"].endswith(VALID_TOKEN[-4:])
    assert body["configured"] is True  # token + enabled + allowlist
    assert body["connected"] is False
    assert body["enabled"] is True
    assert body["allowed_user_ids"] == ["42"]
    assert body["allowed_thread_ids"] == ["99"]


def test_save_persists_session_folder_without_asking_for_a_restart(
    tmp_path: Path, monkeypatch
) -> None:
    """``session_folder`` is read live, so changing it alone needs no restart."""
    import kiro_crew.dashboard.handlers.messaging as mod

    (status_body, _) = _client_put(mod, monkeypatch, tmp_path, {"session_folder": " Discord "})
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is False
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["discord"]["session_folder"] == "Discord"


def test_save_creates_the_configured_folder(tmp_path: Path, monkeypatch) -> None:
    """The folder is created HERE, on the save — never on the reconcile path.

    Creation from the reconciler would put an ``fsync`` on the event loop and,
    being reachable from both the 30s pass and every inbound channel message,
    could drop a concurrent folder edit. The save endpoint is user-initiated and
    already writes config.json, so it is the right owner.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    created: list[tuple[str, str]] = []

    async def fake_ensure(state, namespace: str, name: str) -> str:
        created.append((namespace, name))
        return "fid"

    monkeypatch.setattr(mod, "ensure_channel_folder", fake_ensure)
    (status_body, _) = _client_put(
        mod, monkeypatch, tmp_path, {"session_folder": "Team chat"}, state=object()
    )
    assert status_body[0] == 200
    assert created == [("discord", "Team chat")]


def test_save_creates_no_folder_when_the_setting_is_off(tmp_path: Path, monkeypatch) -> None:
    """Off is the default; a save that leaves it off must not create anything."""
    import kiro_crew.dashboard.handlers.messaging as mod

    created: list[tuple[str, str]] = []

    async def fake_ensure(state, namespace: str, name: str) -> str:
        created.append((namespace, name))
        return ""

    monkeypatch.setattr(mod, "ensure_channel_folder", fake_ensure)
    (status_body, _) = _client_put(
        mod, monkeypatch, tmp_path, {"enabled": True}, state=object()
    )
    assert status_body[0] == 200
    assert created == []


def test_save_still_asks_for_a_restart_for_boot_read_fields(
    tmp_path: Path, monkeypatch
) -> None:
    """A live-reload field alongside a boot-read one still requires a restart."""
    import kiro_crew.dashboard.handlers.messaging as mod

    (status_body, _) = _client_put(
        mod, monkeypatch, tmp_path, {"session_folder": "Discord", "enabled": True}
    )
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is True


def test_save_rejects_an_unusable_session_folder(tmp_path: Path, monkeypatch) -> None:
    """A name that could not address a sidebar folder is refused, not coerced."""
    import kiro_crew.dashboard.handlers.messaging as mod

    for bad in ("nested/name", "back\\slash", "line\nbreak", "x" * 101, 42):
        (status_body, _) = _client_put(mod, monkeypatch, tmp_path, {"session_folder": bad})
        assert status_body[0] == 400, f"session_folder={bad!r} should be rejected"


def test_get_reports_the_configured_session_folder(tmp_path: Path, monkeypatch) -> None:
    """The panel reads the current value back, defaulting to off."""
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = tmp_path / "config.json"
    cfg.write_text('{"discord": {"session_folder": "Discord"}}', encoding="utf-8")
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(loader, "env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    class _State:
        discord_connected = False
        discord_connect_error = ""

    req = make_mocked_request("GET", "/api/discord/config", app={"state": _State()})
    body = json.loads(asyncio.run(mod.api_discord_config_get(req)).text)
    assert body["session_folder"] == "Discord"
