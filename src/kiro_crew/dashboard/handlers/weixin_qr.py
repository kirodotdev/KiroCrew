"""Dashboard endpoints for the Weixin (iLink) QR-login setup flow.

Two routes back the Settings > Channels > WeChat "Connect via QR" flow:

  POST /api/channels/weixin/qr/start   -> start an iLink QR session, return the
                                          QR image + a server-side session id.
  GET  /api/channels/weixin/qr/status  -> poll scan status; on ``confirmed``,
                                          persist the bot token to the cred
                                          store (~/.kiro/crew/.env) and the
                                          account_id/base_url to config.json,
                                          then enable the channel.

The secret (bot credential) is written server-side and never returned to the
client (only ``connected: true`` + the non-secret account_id), mirroring how every
other channel's credentials are handled.

Both the ``.env`` and ``config.json`` writes are minimal atomic writers keyed off
``env_path()`` / ``config_path()``, and every write here runs under the
repository-wide ``_get_config_lock()`` — the same lock the sibling channel savers
take — so a QR confirmation can never interleave with another config save.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import CRED_WEIXIN_TOKEN, KiroCrewConfig, config_path, env_path
from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.dashboard.handlers.messaging import is_direct_local_request
from kiro_crew.platform_compat import restrict_to_owner
from kiro_crew.weixin.client import ILINK_BASE_URL, WeixinClient

logger = logging.getLogger(__name__)

# In-memory QR sessions: session_id -> {client, qrcode, created_at}. Short-lived
# (a QR expires in minutes); pruned opportunistically on each start.
_SESSIONS: Dict[str, Dict[str, Any]] = {}
_SESSION_TTL_SECONDS = 600


def _render_qr_data_uri(scan_data: str) -> str:
    """Encode ``scan_data`` (the iLink login URL) into a PNG data URI.

    Runs in a worker thread: PNG encoding is CPU-bound and must not stall the
    gateway event loop. Import is function-local deliberately — qrcode pulls
    Pillow, and this is the only feature needing it at handler-module import.
    """
    import qrcode as _qrcode  # noqa: PLC0415 — heavy optional import, QR path only

    qr = _qrcode.QRCode(border=2, box_size=8)
    qr.add_data(scan_data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _prune_sessions() -> None:
    now = time.time()
    for sid, sess in list(_SESSIONS.items()):
        if now - sess.get("created_at", 0) >= _SESSION_TTL_SECONDS:
            client = sess.get("client")
            if client is not None:
                # best-effort close; the event loop owns the coroutine
                try:
                    asyncio.create_task(client.close())
                except Exception:
                    pass
            _SESSIONS.pop(sid, None)


def _atomic_write(path: Path, text: str, *, secret: bool = False) -> None:
    """Atomically replace ``path`` with ``text``.

    Delegates to :func:`kiro_crew.atomic_write.atomic_write`, which every
    atomic-write site in the repo is required to use — it allocates the temp file
    with ``mkstemp`` so concurrent writers to the same target cannot collide on a
    deterministic ``.tmp`` name (an ENOENT race).

    Permissions are never weakened. ``secret=True`` forces 0600 and then applies
    :func:`restrict_to_owner`, which locks the file down on Windows too (POSIX
    mode bits are meaningless against NTFS ACLs). For a non-secret file the
    EXISTING mode is carried over, because the replacement would otherwise adopt
    the umask default and silently downgrade an already-restricted file —
    ``config.json`` can hold inline fallback credentials, so a 0600 → 0644
    transition there would expose them to other local users.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if secret:
        atomic_write(path, text, mode=0o600)
        try:
            restrict_to_owner(path)
        except OSError:
            logger.warning("weixin: could not restrict credential permissions", exc_info=True)
        return
    preserved: Optional[int] = None
    try:
        preserved = path.stat().st_mode & 0o777
    except OSError:
        preserved = None  # new file: fall through to the repo-default umask mode
    atomic_write(path, text, mode=preserved)


def _write_env_secret(key: str, value: str) -> None:
    """Upsert ``KEY=VALUE`` in ~/.kiro/crew/.env (0600), preserving other lines."""
    ep = env_path()
    lines: list[str] = []
    found = False
    if ep.exists():
        for line in ep.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k == key:
                    lines.append(f"{key}={value}")
                    found = True
                    continue
            lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    _atomic_write(ep, "\n".join(lines) + "\n", secret=True)


def _read_env_value(key: str) -> Optional[str]:
    """Return the current value of ``key`` in ~/.kiro/crew/.env, or None if absent.

    Used to snapshot the previous credential so a failed two-file commit can be
    rolled back instead of destroying a working credential.
    """
    ep = env_path()
    if not ep.exists():
        return None
    for line in ep.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, value = stripped.partition("=")
            if k.strip() == key:
                return value
    return None


def _delete_env_key(key: str) -> None:
    """Remove ``key`` from ~/.kiro/crew/.env, preserving every other line."""
    ep = env_path()
    if not ep.exists():
        return
    kept: list[str] = []
    for line in ep.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if stripped.split("=", 1)[0].strip() == key:
                continue
        kept.append(line)
    _atomic_write(ep, "\n".join(kept) + "\n", secret=True)


def _commit_credential_and_config(cp: Path, serialized: str, token: str) -> None:
    """Write the credential then the config, rolling the credential back on failure.

    These are two separate files, so the pair cannot be replaced atomically.
    Snapshot the old credential and restore it if the second write fails (ENOSPC,
    EACCES, a crash between the two): otherwise the new credential would be left
    paired with stale account metadata — the channel would authenticate against
    the wrong account — and the previously working credential would be gone, with
    no way to recover it from the dashboard.
    """
    prior = _read_env_value(CRED_WEIXIN_TOKEN)
    _write_env_secret(CRED_WEIXIN_TOKEN, token)
    try:
        _atomic_write(cp, serialized)
    except BaseException:
        try:
            if prior is None:
                _delete_env_key(CRED_WEIXIN_TOKEN)
            else:
                _write_env_secret(CRED_WEIXIN_TOKEN, prior)
        except Exception:
            # Surfacing the rollback failure would mask the original cause, so
            # log it and let the real error propagate.
            logger.error(
                "weixin: config commit failed AND credential rollback failed; "
                "the stored credential may not match config.json",
                exc_info=True,
            )
        raise


def _stage_weixin_config(*, account_id: str, base_url: str) -> tuple[Path, str]:
    """Parse config.json and return ``(path, serialized)`` with the weixin patch.

    Parsing/serializing is separated from writing so the caller can validate the
    config BEFORE overwriting the credential: a corrupt ``config.json`` must not
    leave a replaced token behind with no matching config (which would lose the
    previously working credential).

    A corrupt file PROPAGATES rather than being reset to ``{}`` — resetting would
    silently replace every other setting with just the weixin section. Mirrors
    :func:`weixin_config_save`, which returns 500 on a parse failure.
    """
    cp = config_path()
    data: Dict[str, Any] = {}
    if cp.exists():
        data = json.loads(cp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config.json is not a JSON object")
    weixin = data.get("weixin")
    if not isinstance(weixin, dict):
        weixin = {}
    weixin["enabled"] = True
    weixin["account_id"] = account_id
    if base_url:
        weixin["base_url"] = base_url
    data["weixin"] = weixin
    return cp, json.dumps(data, ensure_ascii=False, indent=2)


async def weixin_qr_start(request: web.Request) -> web.Response:
    """Begin an iLink QR-login session; return the QR image + session id.

    Loopback-only: a QR confirmation writes the bot credential into the shared
    credential store and enables the channel, so a remote (or tunneled) session
    must never be able to start one — it could scan the returned code with its own
    WeChat account and take over the channel.
    """
    if not is_direct_local_request(request):
        return web.json_response(
            {"error": "read-only from remote sessions (local machine only)"}, status=403
        )
    _prune_sessions()
    client = WeixinClient(token="", base_url=ILINK_BASE_URL)
    try:
        await client.connect()
        resp = await client.get_bot_qrcode()
    except Exception as exc:  # network / iLink error
        await client.close()
        logger.warning("weixin QR start failed: %s", exc)
        return web.json_response({"error": "qr_start_failed", "detail": str(exc)[:200]}, status=502)

    qrcode_token = resp.get("qrcode", "")
    scan_url = resp.get("qrcode_img_content", "")
    if not qrcode_token:
        await client.close()
        return web.json_response({"error": "invalid_qr_response"}, status=502)

    # Despite its name, iLink's `qrcode_img_content` is NOT image bytes — it is
    # the full scannable login URL, while `qrcode` is just the hex token (the
    # reference implementation encodes the URL itself, falling back to the
    # token). Render the QR PNG here, off the event loop, and hand the panel a
    # data URI so <img src> is actually loadable.
    scan_data = scan_url or qrcode_token
    try:
        img_data_uri = await asyncio.to_thread(_render_qr_data_uri, scan_data)
    except Exception:
        await client.close()
        logger.exception("weixin: QR image rendering failed")
        return web.json_response({"error": "qr_render_failed"}, status=500)

    session_id = uuid.uuid4().hex
    _SESSIONS[session_id] = {"client": client, "qrcode": qrcode_token, "created_at": time.time()}
    return web.json_response({"session_id": session_id, "qrcode_img_content": img_data_uri})


async def weixin_qr_status(request: web.Request) -> web.Response:
    """Poll scan status; on confirmation, persist creds + enable the channel.

    Loopback-only for the same reason as :func:`weixin_qr_start` — this is the
    handler that actually writes the credential.
    """
    if not is_direct_local_request(request):
        return web.json_response(
            {"error": "read-only from remote sessions (local machine only)"}, status=403
        )
    session_id = request.query.get("session_id", "")
    sess = _SESSIONS.get(session_id)
    if not sess:
        return web.json_response({"error": "unknown_session"}, status=404)

    client: WeixinClient = sess["client"]
    try:
        resp = await client.get_qrcode_status(sess["qrcode"])
    except Exception as exc:
        logger.warning("weixin QR status poll failed: %s", exc)
        return web.json_response({"status": "pending"})

    status = resp.get("status", "pending")
    if status != "confirmed":
        # "scaned" / "pending" / "expired" — surface as-is for the UI.
        if status == "expired":
            await client.close()
            _SESSIONS.pop(session_id, None)
        return web.json_response({"status": status})

    token = resp.get("bot_token", "")
    base_url = resp.get("baseurl") or ILINK_BASE_URL
    account_id = resp.get("ilink_bot_id") or resp.get("account_id") or resp.get("ilink_user_id") or ""
    if not token or not account_id:
        await client.close()
        _SESSIONS.pop(session_id, None)
        return web.json_response({"error": "confirmed_without_credentials"}, status=502)

    def _persist() -> None:
        # Parse + stage the config FIRST: if config.json is corrupt we must fail
        # before touching the credential, otherwise a re-login would replace a
        # working token and then abort, losing it.
        cp, serialized = _stage_weixin_config(account_id=account_id, base_url=base_url)
        _commit_credential_and_config(cp, serialized, token)

    try:
        # Both writes touch shared stores (.env + config.json) that other config
        # savers read-modify-write, so hold the repository-wide config lock across
        # BOTH — otherwise a concurrent channel save can interleave and silently
        # drop one side's write.
        #
        # The whole block runs OFF the loop: besides the file I/O,
        # restrict_to_owner() shells out to whoami/icacls on Windows, which would
        # freeze the gateway for seconds on the first confirmation.
        async with _get_config_lock():
            await asyncio.to_thread(_persist)
    except Exception as exc:
        # A corrupt config.json or an un-restrictable credential file must NOT be
        # reported as a successful sign-in.
        logger.exception("weixin: failed to persist QR sign-in")
        return web.json_response(
            {"error": "persist_failed", "detail": str(exc)[:200]}, status=500
        )
    finally:
        await client.close()
        _SESSIONS.pop(session_id, None)

    logger.info("weixin: QR login confirmed; account_id=%s persisted. Restart to connect.", account_id[:8])
    return web.json_response({"status": "confirmed", "connected": True, "account_id": account_id})


def setup_weixin_routes(app: web.Application) -> None:
    """Register the Weixin QR-login routes."""
    app.router.add_post("/api/channels/weixin/qr/start", weixin_qr_start)
    app.router.add_get("/api/channels/weixin/qr/status", weixin_qr_status)
    app.router.add_get("/api/weixin/config", weixin_config_get)
    app.router.add_put("/api/weixin/config", weixin_config_save)


async def weixin_config_get(request: web.Request) -> web.Response:
    """GET /api/weixin/config — status + policy. Never returns the credential."""

    def _load() -> tuple[Any, bool]:
        cfg = KiroCrewConfig.load()
        creds = cfg.load_credentials()
        return cfg.weixin, bool(creds.get(CRED_WEIXIN_TOKEN, "") or cfg.weixin.token)

    # Off-loop: both config.json and the .env credential store are read from disk.
    wx, has_cred = await asyncio.to_thread(_load)
    state = request.app.get("state")
    return web.json_response(
        {
            # True only when the long-poll transport actually started this session.
            "connected": bool(getattr(state, "weixin_connected", False)),
            "connect_error": str(getattr(state, "weixin_connect_error", ""))[:120],
            "configured": bool(has_cred and wx.account_id and wx.enabled),
            "read_only": not is_direct_local_request(request),
            # Presence only — the credential itself is never serialized.
            "credential_set": has_cred,
            "enabled": bool(wx.enabled),
            "account_id": wx.account_id,
            "dm_policy": wx.dm_policy,
            "allowed_user_ids": [str(u) for u in wx.allowed_user_ids],
        }
    )


async def weixin_config_save(request: web.Request) -> web.Response:
    """PUT /api/weixin/config — persist policy fields to config.json.

    Serialized with every other config.json writer via the repository-wide
    ``_get_config_lock()``; loopback-only like the sibling channel savers.
    """
    if not is_direct_local_request(request):
        return web.json_response(
            {"error": "read-only from remote sessions (local machine only)"}, status=403
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)

    if "enabled" in body and not isinstance(body["enabled"], bool):
        return web.json_response({"error": "enabled must be a boolean"}, status=400)
    if "dm_policy" in body and body["dm_policy"] not in ("open", "allowlist", "disabled"):
        return web.json_response({"error": "invalid dm_policy"}, status=400)
    if "allowed_user_ids" in body and not isinstance(body["allowed_user_ids"], list):
        return web.json_response({"error": "allowed_user_ids must be a list"}, status=400)

    async with _get_config_lock():
        cp = config_path()

        def _read_config() -> Dict[str, Any]:
            return json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}

        # Off-loop read: a large or slow config.json must not stall the gateway
        # event loop. Reading under the lock keeps the snapshot current relative
        # to every other config writer (mirrors the Telegram saver).
        try:
            data: Dict[str, Any] = await asyncio.to_thread(_read_config)
        except Exception:
            return web.json_response({"error": "config.json is corrupt"}, status=500)
        if not isinstance(data, dict):
            return web.json_response({"error": "config.json is corrupt"}, status=500)
        if not isinstance(data.get("weixin"), dict):
            data["weixin"] = {}
        wx = data["weixin"]
        if "enabled" in body:
            wx["enabled"] = bool(body["enabled"])
        if "dm_policy" in body:
            wx["dm_policy"] = str(body["dm_policy"])
        if "allowed_user_ids" in body:
            wx["allowed_user_ids"] = [
                str(u).strip() for u in body["allowed_user_ids"] if str(u).strip()
            ]
        serialized = json.dumps(data, ensure_ascii=False, indent=2)
        await asyncio.to_thread(_atomic_write, cp, serialized)

    # Every weixin field is read once in the orchestrator's constructor.
    return web.json_response({"ok": True, "restart_required": True})
