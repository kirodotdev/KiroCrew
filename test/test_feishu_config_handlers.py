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
    if os.name != "nt":
        # POSIX-only: group/other must hold no bits on a file carrying secrets.
        # Windows locks the same file down with an ACL (icacls) instead, where
        # st_mode reports 0o666 no matter what the ACL says -- so asserting mode
        # bits there tests a mechanism the platform does not use.
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
        feishu_connect_error = "lark-oapi is not installed — run: pip install 'lark-oapi>=1.4,<2'"

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
    # And our config write was rolled back, so nothing durable remains from a save
    # that reported failure. The file itself SURVIVES: the rollback restores the
    # `feishu` section only, because unlinking the config to undo our own section
    # would take every other channel's settings with it.
    cfg_path = tmp_path / "config.json"
    if cfg_path.exists():
        assert "feishu" not in json.loads(cfg_path.read_text(encoding="utf-8"))


def test_a_malformed_stored_value_does_not_crash_the_save(tmp_path: Path, monkeypatch) -> None:
    """`null` stored where a list or an int belongs must still be repairable.

    `dict.get(key, default)` substitutes the default only for an ABSENT key, so a
    hand-edited `null` returns None and used to reach `list(None)` / `int(None)`
    — a 500 from the exact request that would have fixed the file.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "feishu": {
                    "allowed_open_ids": None,
                    "allowed_group_ids": None,
                    "soft_threshold_pct": None,
                    "session_folder": None,
                }
            }
        ),
        encoding="utf-8",
    )
    status_body, _env = _client_put(
        mod,
        monkeypatch,
        tmp_path,
        {
            "allowed_user_ids": [OPEN_ID],
            "allowed_group_ids": [CHAT_ID],
            "soft_threshold_pct": 75,
        },
    )
    status, _body = status_body
    assert status == 200
    out = json.loads(cfg.read_text(encoding="utf-8"))
    assert out["feishu"]["allowed_open_ids"] == [OPEN_ID]
    assert out["feishu"]["allowed_group_ids"] == [CHAT_ID]
    assert out["feishu"]["soft_threshold_pct"] == 75


def test_a_stored_bool_threshold_is_not_read_as_a_number(tmp_path: Path, monkeypatch) -> None:
    """`bool` is an `int` subclass, so a stored `true` must not compare as 1.

    Otherwise saving the default 80 over a stored `true` would look like a change
    from 1 and report restart_required for a value the user never had.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"feishu": {"soft_threshold_pct": True}}), encoding="utf-8")
    status_body, _env = _client_put(mod, monkeypatch, tmp_path, {"soft_threshold_pct": 80})
    status, _body = status_body
    assert status == 200
    # 80 is the default the guard falls back to, so this is a no-op write rather
    # than "changed from 1 to 80".
    out = json.loads(cfg.read_text(encoding="utf-8"))
    assert out["feishu"]["soft_threshold_pct"] is True


def test_get_reads_the_config_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    """The panel polls this every 15s, so its filesystem reads must not run on the
    gateway's event loop thread, where they would stall every other task."""
    import threading

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    env.write_text(f"FEISHU_APP_ID={APP_ID}\n", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    read_threads: list[int] = []
    real_load = loader.KiroCrewConfig.load

    def _spy_load(*a, **kw):
        read_threads.append(threading.get_ident())
        return real_load(*a, **kw)

    monkeypatch.setattr(loader.KiroCrewConfig, "load", _spy_load)

    loop_thread: list[int] = []

    async def _run():
        loop_thread.append(threading.get_ident())
        app = web.Application()
        app["state"] = type("S", (), {})()
        app.router.add_get("/api/feishu/config", mod.api_feishu_config_get)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/feishu/config")
            return resp.status

    status = asyncio.run(_run())
    assert status == 200
    assert read_threads, "the handler never read the config at all"
    assert loop_thread, "could not identify the loop thread"
    assert all(
        t != loop_thread[0] for t in read_threads
    ), f"config read ran on the event loop thread ({read_threads} vs {loop_thread})"


def test_save_waits_on_the_cross_process_config_lock(tmp_path: Path, monkeypatch) -> None:
    """A writer in another PROCESS must not be able to interleave with the save.

    The in-process asyncio lock the caller holds cannot see another process, so the
    save goes through ``update_config_locked``, which takes an advisory lock on the
    sidecar ``<config>.lock``. Holding that lockfile here — the same way a second
    process would — must block the save until it is released, which is the property
    the in-process lock alone cannot provide.
    """
    import threading

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import kiro_crew.dashboard.handlers.messaging as mod
    from kiro_crew.platform_compat import file_lock

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"feishu": {"enabled": False}}), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    holding = threading.Event()
    release = threading.Event()
    observed_while_held: list[bool] = []

    def _hold_the_sidecar() -> None:
        lock_path = cfg_path.parent / (cfg_path.name + ".lock")
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with file_lock(fd):
                holding.set()
                release.wait(timeout=10)
                # Still unwritten at this point iff the save is genuinely waiting.
                observed_while_held.append(
                    json.loads(cfg_path.read_text(encoding="utf-8"))["feishu"]["enabled"]
                )
        finally:
            os.close(fd)

    async def _run():
        app = web.Application()
        app["state"] = type("S", (), {})()
        app.router.add_put("/api/feishu/config", mod.api_feishu_config_save)
        async with TestClient(TestServer(app)) as client:
            worker = threading.Thread(target=_hold_the_sidecar, daemon=True)
            worker.start()
            assert holding.wait(timeout=10), "could not take the sidecar lock"

            save = asyncio.ensure_future(client.put("/api/feishu/config", json={"enabled": True}))
            # Give the save a real chance to (wrongly) win the race.
            await asyncio.sleep(0.4)
            assert not save.done(), "the save did not wait for the sidecar lock"

            release.set()
            resp = await save
            worker.join(timeout=10)
            return resp.status

    status = asyncio.run(_run())
    assert status == 200
    # The lock holder saw the OLD value, so the write really landed after release.
    assert observed_while_held == [False], observed_while_held
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["feishu"]["enabled"] is True


def test_rollback_keeps_a_concurrent_edit_it_does_not_own(tmp_path: Path, monkeypatch) -> None:
    """A failed save must undo only what it wrote, not a concurrent writer's edit.

    Sequence: we write `enabled` and `allow_group`, another process then changes
    `allow_group`, and our .env write fails. Rolling the whole section back would
    discard their change; the rollback compares per key, so `enabled` reverts and
    `allow_group` keeps THEIR value.

    The injection is keyed to observed behavior, not call order: the save's apply
    is the only write that leaves `allow_group` as True in THIS test's config, so
    the foreign edit lands right after that call returns, strictly before the
    handler proceeds to the failing .env write and the rollback. `loader` is
    patched module-wide, so a leaked background writer from a sibling test in the
    same worker process can also reach `_interleave`; a call-count heuristic let
    such a caller suppress the injection entirely (issue #7944), and an unlocked
    read-modify-write could lose it. Hence the once-gate under a lock and the
    injection going through the real locked primitive.
    """
    import threading

    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"feishu": {"enabled": False, "allow_group": False}}), encoding="utf-8"
    )

    real_update = loader.update_config_locked
    inject_gate = threading.Lock()
    injected: list[bool] = []

    def _set_foreign(data: dict) -> dict:
        # Stand in for `kirocrew config set feishu.allow_group true` landing
        # after our write and before our rollback.
        data.setdefault("feishu", {})["allow_group"] = "set-by-someone-else"
        return data

    def _interleave(*a, **kw):
        result = real_update(*a, **kw)
        target = a[0] if a else kw.get("path")
        section = result.get("feishu") if isinstance(result, dict) else None
        with inject_gate:
            if (
                not injected
                and target == cfg
                and isinstance(section, dict)
                and section.get("allow_group") is True
            ):
                injected.append(True)
                real_update(cfg, mutate=_set_foreign)
        return result

    async def _boom(updates):
        raise OSError("read-only file system")

    monkeypatch.setattr(mod, "update_config_locked", _interleave, raising=False)
    monkeypatch.setattr(loader, "update_config_locked", _interleave)
    monkeypatch.setattr(mod, "_write_env_off_loop", _boom)

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _run():
        app = web.Application()
        app["state"] = type("S", (), {})()
        app.router.add_put("/api/feishu/config", mod.api_feishu_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/feishu/config",
                json={"enabled": True, "allow_group": True, "bot_token": APP_SECRET},
            )
            return resp.status

    status = asyncio.run(_run())
    assert status >= 500, status
    assert injected, "the foreign concurrent edit was never injected"

    out = json.loads(cfg.read_text(encoding="utf-8"))["feishu"]
    # Ours: reverted. Theirs: untouched.
    assert out["enabled"] is False
    assert out["allow_group"] == "set-by-someone-else"


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


# ── Missing-SDK install advice (GET) ──
# lark-oapi ships as the optional [feishu] extra, so a fully credentialed channel
# still cannot start without it. The badge cannot report that in the case that
# matters: maybe_start_feishu returns at its first line when the channel is
# disabled, and the ImportError branch that records the missing SDK sits AFTER
# that return -- so a user who has not yet flipped the enable toggle gets no hint
# at all, and one who has must restart the gateway first. These cover the config
# endpoint answering it directly instead.


def _get_config(monkeypatch, *, connected: bool = False) -> dict:
    """Run the Feishu config GET and return its JSON body."""
    import kiro_crew.dashboard.handlers.messaging as mod

    class _State:
        feishu_connected = connected
        feishu_connect_error = ""

    req = make_mocked_request("GET", "/api/feishu/config")
    req.app["state"] = _State()
    resp = asyncio.run(mod.api_feishu_config_get(req))
    return json.loads(resp.body.decode())


def test_get_reports_the_sdk_as_present_when_importable(monkeypatch) -> None:
    """An importable SDK needs no advice, so the command is empty."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setattr(mod.importlib.util, "find_spec", lambda name: object())
    body = _get_config(monkeypatch)
    assert body["sdk_installed"] is True
    assert body["sdk_install_command"] == ""


def test_get_names_this_interpreter_when_the_sdk_is_missing(monkeypatch) -> None:
    """The command must name the gateway's OWN python, not a bare ``pip``.

    Installing into a different environment is the actual failure mode: the
    gateway keeps skipping the channel and nothing says the install landed
    elsewhere.
    """
    import sys

    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setattr(mod.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(mod, "_pip_install_channel_available", lambda: True)
    body = _get_config(monkeypatch)
    assert body["sdk_installed"] is False
    assert body["sdk_install_supported"] is True
    assert sys.executable in body["sdk_install_command"]
    # The dependency itself, because `pip install kirocrew[feishu]` cannot
    # resolve -- this project is published on no index.
    assert "lark-oapi" in body["sdk_install_command"]
    assert "kirocrew[" not in body["sdk_install_command"]
    assert "-m pip install" in body["sdk_install_command"]


def test_get_withholds_a_command_that_cannot_work(monkeypatch) -> None:
    """No install channel -> no command.

    On the bundled desktop interpreter a pip install writes into the code-signed
    bundle and is discarded on the next app update, so naming the command there
    is wrong advice rather than merely unhelpful.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setattr(mod.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(mod, "_pip_install_channel_available", lambda: False)
    body = _get_config(monkeypatch)
    assert body["sdk_installed"] is False
    assert body["sdk_install_supported"] is False
    assert body["sdk_install_command"] == ""


def test_sdk_probe_reports_nothing_missing_for_a_channel_without_an_extra() -> None:
    """A channel with no optional extra renders no card: nothing can be missing."""
    from kiro_crew.dashboard.handlers.messaging import _channel_sdk_status

    assert _channel_sdk_status("wecom") == (True, False, "")
