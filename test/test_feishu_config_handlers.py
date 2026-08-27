"""Tests for the Feishu config API (loopback gate, validation, persistence).

Mirrors ``test_wecom_config_handlers.py`` — Feishu has the same two-credential
shape — plus the two things Feishu adds: prefixed opaque ids (``ou_`` users,
``oc_`` groups) and the separate group-access axis.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from aiohttp.test_utils import make_mocked_request

import kiro_crew.config.loader as loader

APP_ID = "cli_a1b2c3d4e5f6g7h8"
APP_SECRET = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd"
OPEN_ID = "ou_c99cbd8a1b2c3d4e5f6a7b8c9d0e1f2a"
OPEN_ID_2 = "ou_0011aabbccdd22334455eeff66778899"
CHAT_ID = "oc_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def test_save_denies_non_loopback(monkeypatch) -> None:
    """Config writes are loopback-only: remote sessions are read-only."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
    req = make_mocked_request(
        "PUT",
        "/api/feishu/config",
        payload=b'{"bot_token": "planted-secret-value"}',
        headers={"Content-Type": "application/json"},
    )
    resp = asyncio.run(mod.api_feishu_config_save(req))
    assert resp.status == 403


def test_save_denies_forwarded_loopback_request() -> None:
    """A reverse-proxied request (loopback peer + XFF) cannot plant secrets."""
    import kiro_crew.dashboard.handlers.messaging as mod

    req = make_mocked_request(
        "PUT",
        "/api/feishu/config",
        payload=b'{"bot_token": "planted-secret-value"}',
        headers={"Content-Type": "application/json", "X-Forwarded-For": "203.0.113.7"},
    )
    resp = asyncio.run(mod.api_feishu_config_save(req))
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
        app.router.add_put("/api/feishu/config", mod.api_feishu_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/feishu/config", json=body)
            return resp.status, await resp.json()

    return asyncio.run(_run()), env


def test_save_persists_credentials_and_config(tmp_path: Path, monkeypatch) -> None:
    """Both secrets land in .env (0600), config in config.json, environ synced."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    status_body, env = _client_put(
        mod,
        monkeypatch,
        tmp_path,
        {
            "bot_id": APP_ID,
            "bot_token": APP_SECRET,
            "enabled": True,
            "allowed_user_ids": [OPEN_ID, OPEN_ID_2],
            "soft_threshold_pct": 75,
        },
    )
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is True
    env_text = env.read_text(encoding="utf-8")
    assert f"FEISHU_APP_ID={APP_ID}" in env_text
    assert f"FEISHU_APP_SECRET={APP_SECRET}" in env_text
    assert (env.stat().st_mode & 0o077) == 0
    assert os.environ["FEISHU_APP_ID"] == APP_ID
    assert os.environ["FEISHU_APP_SECRET"] == APP_SECRET
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["feishu"]["enabled"] is True
    # The wire name is the shared panel's `allowed_user_ids`; on disk it must be
    # `allowed_open_ids`, which is the key the transport actually reads.
    assert cfg["feishu"]["allowed_open_ids"] == [OPEN_ID, OPEN_ID_2]
    assert "allowed_user_ids" not in cfg["feishu"]
    assert cfg["feishu"]["soft_threshold_pct"] == 75


def test_save_rejects_whitespace_credentials(tmp_path: Path, monkeypatch) -> None:
    """A secret carrying inner whitespace fails before any write."""
    import kiro_crew.dashboard.handlers.messaging as mod

    status_body, env = _client_put(mod, monkeypatch, tmp_path, {"bot_token": "two words"})
    status, body = status_body
    assert status == 400
    assert "whitespace" in body["error"]
    assert "two" not in env.read_text(encoding="utf-8")


def test_save_rejects_open_id_without_prefix(tmp_path: Path, monkeypatch) -> None:
    """A user id missing the ou_ prefix fails closed, nothing persisted."""
    import kiro_crew.dashboard.handlers.messaging as mod

    status_body, _env = _client_put(
        mod, monkeypatch, tmp_path, {"allowed_user_ids": ["c99cbd8a1b2c3d4e"]}
    )
    status, body = status_body
    assert status == 400
    assert "invalid Feishu open_id" in body["error"]
    assert not (tmp_path / "config.json").exists()


def test_save_rejects_chat_id_in_the_user_list(tmp_path: Path, monkeypatch) -> None:
    """The two lists are not interchangeable.

    An ``oc_`` chat_id pasted into the DM allow-list is the likely mistake, and
    the transport reads the two lists for different decisions — accepting it
    would leave an entry that looks authoritative while authorising nobody.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    status_body, _env = _client_put(mod, monkeypatch, tmp_path, {"allowed_user_ids": [CHAT_ID]})
    status, body = status_body
    assert status == 400
    assert "invalid Feishu open_id" in body["error"]

    status_body, _env = _client_put(mod, monkeypatch, tmp_path, {"allowed_group_ids": [OPEN_ID]})
    status, body = status_body
    assert status == 400
    assert "invalid Feishu group chat_id" in body["error"]


def test_save_rejects_non_ascii_id_body(tmp_path: Path, monkeypatch) -> None:
    """Unicode digits/letters are rejected: str.isalnum() alone would admit
    them, but they can never match a real Feishu id — the entry would sit in the
    allow-list looking authoritative while granting nothing."""
    import kiro_crew.dashboard.handlers.messaging as mod

    for bad in ("ou_张三", "ou_１２３４５６", "ou_abc\u200bdef", "ou_abc def", "ou_"):
        status_body, _env = _client_put(mod, monkeypatch, tmp_path, {"allowed_user_ids": [bad]})
        status, body = status_body
        assert status == 400, bad
        assert "invalid Feishu open_id" in body["error"], bad


def test_save_dedupes_and_preserves_order(tmp_path: Path, monkeypatch) -> None:
    """Repeated ids collapse to the first occurrence, order otherwise kept."""
    import kiro_crew.dashboard.handlers.messaging as mod

    status_body, _env = _client_put(
        mod,
        monkeypatch,
        tmp_path,
        {"allowed_user_ids": [OPEN_ID_2, OPEN_ID, OPEN_ID_2, "  "]},
    )
    status, _body = status_body
    assert status == 200
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["feishu"]["allowed_open_ids"] == [OPEN_ID_2, OPEN_ID]


def test_group_access_is_a_separate_axis(tmp_path: Path, monkeypatch) -> None:
    """allow_group + allowed_group_ids persist independently of the DM list."""
    import kiro_crew.dashboard.handlers.messaging as mod

    status_body, _env = _client_put(
        mod,
        monkeypatch,
        tmp_path,
        {
            "allowed_user_ids": [OPEN_ID],
            "allow_group": True,
            "allowed_group_ids": [CHAT_ID],
        },
    )
    status, _body = status_body
    assert status == 200
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["feishu"]["allow_group"] is True
    assert cfg["feishu"]["allowed_group_ids"] == [CHAT_ID]
    assert cfg["feishu"]["allowed_open_ids"] == [OPEN_ID]


def test_save_rejects_non_bool_allow_group(tmp_path: Path, monkeypatch) -> None:
    """A truthy non-bool must not be coerced into widening group access."""
    import kiro_crew.dashboard.handlers.messaging as mod

    status_body, _env = _client_put(mod, monkeypatch, tmp_path, {"allow_group": "yes"})
    status, body = status_body
    assert status == 400
    assert "allow_group must be a boolean" in body["error"]
    assert not (tmp_path / "config.json").exists()


def test_clear_credentials(tmp_path: Path, monkeypatch) -> None:
    """bot_token_clear / bot_id_clear remove secrets from .env and environ."""
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text(f"FEISHU_APP_ID={APP_ID}\nFEISHU_APP_SECRET={APP_SECRET}\n", encoding="utf-8")
    monkeypatch.setenv("FEISHU_APP_ID", APP_ID)
    monkeypatch.setenv("FEISHU_APP_SECRET", APP_SECRET)
    status_body, env = _client_put(
        mod, monkeypatch, tmp_path, {"bot_token_clear": True, "bot_id_clear": True}
    )
    status, body = status_body
    assert status == 200
    assert body["restart_required"] is True
    env_text = env.read_text(encoding="utf-8")
    assert f"FEISHU_APP_ID={APP_ID}" not in env_text
    assert f"FEISHU_APP_SECRET={APP_SECRET}" not in env_text
    assert os.environ.get("FEISHU_APP_ID") is None
    assert os.environ.get("FEISHU_APP_SECRET") is None


def test_clear_wins_over_a_simultaneous_value(tmp_path: Path, monkeypatch) -> None:
    """A body carrying both a clear flag and a value clears, never sets."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    status_body, env = _client_put(
        mod,
        monkeypatch,
        tmp_path,
        {"bot_token_clear": True, "bot_token": "should-not-be-written"},
    )
    status, _body = status_body
    assert status == 200
    assert "should-not-be-written" not in env.read_text(encoding="utf-8")


def test_save_strips_an_accidental_env_line_paste(tmp_path: Path, monkeypatch) -> None:
    """`FEISHU_APP_SECRET=…` pasted whole stores only the value."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    status_body, env = _client_put(
        mod, monkeypatch, tmp_path, {"bot_token": f"FEISHU_APP_SECRET={APP_SECRET}"}
    )
    status, _body = status_body
    assert status == 200
    assert f"FEISHU_APP_SECRET={APP_SECRET}\n" in env.read_text(encoding="utf-8")
    assert "FEISHU_APP_SECRET=FEISHU_APP_SECRET" not in env.read_text(encoding="utf-8")


def test_get_masks_credentials_and_reports_state(tmp_path: Path, monkeypatch) -> None:
    """GET never returns a raw secret, and reports receiver liveness verbatim."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text(f"FEISHU_APP_ID={APP_ID}\nFEISHU_APP_SECRET={APP_SECRET}\n", encoding="utf-8")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "feishu": {
                    "enabled": True,
                    "allowed_open_ids": [OPEN_ID],
                    "allow_group": True,
                    "allowed_group_ids": [CHAT_ID],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    class _State:
        feishu_connected = False
        feishu_connect_error = 'lark-oapi is not installed — run: pip install "kirocrew[feishu]"'

    async def _run():
        app = web.Application()
        app["state"] = _State()
        app.router.add_get("/api/feishu/config", mod.api_feishu_config_get)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/feishu/config")
            return resp.status, await resp.json()

    status, body = asyncio.run(_run())
    assert status == 200
    assert body["bot_id_set"] is True and body["bot_token_set"] is True
    assert APP_SECRET not in json.dumps(body)
    assert APP_ID not in json.dumps(body)
    # Credentials + enabled + a non-empty DM allow-list is what makes it usable.
    assert body["configured"] is True
    assert body["connected"] is False
    assert "lark-oapi is not installed" in body["connect_error"]
    assert body["allowed_user_ids"] == [OPEN_ID]
    assert body["allow_group"] is True
    assert body["allowed_group_ids"] == [CHAT_ID]


def test_get_reports_unconfigured_while_the_allowlist_is_empty(tmp_path: Path, monkeypatch) -> None:
    """Credentialed + enabled but no open_id is NOT configured: the transport
    fails closed and rejects every DM, so the badge must not claim readiness."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text(f"FEISHU_APP_ID={APP_ID}\nFEISHU_APP_SECRET={APP_SECRET}\n", encoding="utf-8")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"feishu": {"enabled": True, "allowed_open_ids": []}}), encoding="utf-8"
    )
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    async def _run():
        app = web.Application()
        app["state"] = type("S", (), {})()
        app.router.add_get("/api/feishu/config", mod.api_feishu_config_get)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/feishu/config")
            return await resp.json()

    body = asyncio.run(_run())
    assert body["configured"] is False


def test_save_rejects_a_config_whose_top_level_is_not_an_object(
    tmp_path: Path, monkeypatch
) -> None:
    """A hand-edited `[]` config answers "corrupt", not a 500 with a stack trace.

    The read path already treats an unparseable config that way; a parseable one
    of the wrong SHAPE is the same class of problem to the person who has to fix
    it, and letting `data.get` raise AttributeError instead tells them nothing.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    (tmp_path / "config.json").write_text("[]", encoding="utf-8")
    status_body, _env = _client_put(mod, monkeypatch, tmp_path, {"enabled": True})
    status, body = status_body
    assert status == 500
    assert body["code"] == "config_corrupt"
    # Untouched: a corrupt config is not silently replaced with a fresh one, which
    # would discard every other channel's settings.
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == "[]"


def test_a_failed_credential_write_leaves_no_folder_behind(tmp_path: Path, monkeypatch) -> None:
    """The session folder is reconciled only after the .env write commits.

    The config write is rolled back when the credential write fails, but a folder
    that has already been created or renamed is NOT — so reconciling before the
    write would leave a durable change behind from a save that reported failure.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    calls: list[tuple] = []

    async def _record(state, channel, name, relabel=False):
        calls.append((channel, name, relabel))

    async def _boom(updates):
        raise OSError("read-only file system")

    monkeypatch.setattr(mod, "ensure_channel_folder", _record)
    monkeypatch.setattr(mod, "_write_env_off_loop", _boom)

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _run():
        app = web.Application()
        app["state"] = type("S", (), {})()
        app.router.add_put("/api/feishu/config", mod.api_feishu_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/feishu/config",
                json={"bot_token": APP_SECRET, "session_folder": "Feishu"},
            )
            return resp.status

    status = asyncio.run(_run())
    assert status >= 500, status
    # The whole point: the folder was never touched.
    assert calls == []
    # And the config write was rolled back with it, so nothing durable remains.
    assert not (tmp_path / "config.json").exists()


def test_id_shape_validator() -> None:
    """The prefix + ASCII-alphanumeric body rule, without a length equality.

    No fixed length on purpose: the id body length is not contractual, and a
    stricter rule would reject ids a future tenant issues.
    """
    from kiro_crew.dashboard.handlers.messaging import _is_valid_feishu_id

    assert _is_valid_feishu_id(OPEN_ID, "ou_")
    assert _is_valid_feishu_id(CHAT_ID, "oc_")
    assert _is_valid_feishu_id("ou_a", "ou_")
    assert not _is_valid_feishu_id(OPEN_ID, "oc_")
    assert not _is_valid_feishu_id("ou_", "ou_")
    assert not _is_valid_feishu_id("", "ou_")
    assert not _is_valid_feishu_id("ou_ab-cd", "ou_")
    assert not _is_valid_feishu_id("ou_" + "a" * 200, "ou_")
