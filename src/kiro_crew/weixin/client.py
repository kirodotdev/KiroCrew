"""Low-level iLink Bot API client for the Weixin channel.

Ported from Nous Research's Hermes Agent ``gateway/platforms/weixin.py``
(MIT License). Kept dependency-light: only ``aiohttp`` (already a KiroCrew dep),
imported lazily inside :meth:`WeixinClient.connect` so a missing extra never
breaks boot.

This module owns ONLY protocol I/O + credential persistence + the small
context-token / typing-ticket caches. Turn handling, authorization, rendering
and chunking live in the sibling transport / dispatch / renderer modules.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import secrets
import struct
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import aiohttp

from kiro_crew.platform_compat import restrict_to_owner

logger = logging.getLogger(__name__)

# --- Endpoints & protocol constants -------------------------------------------
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
DEFAULT_BOT_TYPE = 3

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2
MESSAGE_DEDUP_TTL_SECONDS = 300

# item_list types
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

TYPING_START = 1
TYPING_STOP = 2


# --- Small helpers ------------------------------------------------------------
def _safe_id(value: Optional[str], keep: int = 8) -> str:
    if not value:
        return "?"
    return value[:keep] + ("…" if len(value) > keep else "")


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@functools.lru_cache(maxsize=1)
def _wechat_uin() -> str:
    """One random UIN per process, generated on first use and then reused.

    iLink binds a bot session to the ``X-WECHAT-UIN`` it saw at authorization.
    Generating a fresh value per request made the first ``getupdates``
    long-poll after a QR login return ``-14`` (:data:`SESSION_EXPIRED_ERRCODE`),
    which parked the poll loop for 10 minutes and made the channel look like it
    had never connected.
    """
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> Dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# NOTE: media (image/voice/file/video) is NOT supported yet — only the text item
# of an inbound message is read, and replies are text-only. The iLink media path
# needs an AES-128-ECB envelope over the WeChat CDN (the cipher mode is dictated
# by the remote protocol, not chosen by us); those helpers land with the media
# feature rather than sitting here unreachable.


# --- Credential + ephemeral state persistence ---------------------------------
def _account_dir(home: str) -> Path:
    path = Path(home) / "weixin" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_weixin_account(home: str, *, account_id: str, token: str, base_url: str, user_id: str = "") -> None:
    """Persist account credentials owner-only.

    The file holds the bot credential, so it is locked down with
    :func:`platform_compat.restrict_to_owner` — a bare ``chmod(0o600)`` is a no-op
    against Windows ACLs.
    """
    path = _account_dir(home) / f"{account_id}.json"
    _atomic_json_write(path, {
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    try:
        restrict_to_owner(str(path))
    except OSError:
        logger.warning("weixin: could not restrict account file permissions", exc_info=True)


def load_weixin_account(home: str, account_id: str) -> Optional[Dict[str, Any]]:
    path = _account_dir(home) / f"{account_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


class ContextTokenStore:
    """Disk-backed ``context_token`` cache keyed by account + peer.

    iLink requires echoing the peer's latest ``context_token`` on every
    outbound message; persistence gives reply continuity across restarts.
    """

    def __init__(self, home: str) -> None:
        self._root = _account_dir(home)
        self._cache: Dict[str, str] = {}

    def _path(self, account_id: str) -> Path:
        return self._root / f"{account_id}.context-tokens.json"

    def _key(self, account_id: str, user_id: str) -> str:
        return f"{account_id}:{user_id}"

    def restore(self, account_id: str) -> None:
        path = self._path(account_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("weixin: failed to restore reply context for %s: %s", _safe_id(account_id), exc)
            return
        for user_id, tok in data.items():
            if isinstance(tok, str) and tok:
                self._cache[self._key(account_id, user_id)] = tok

    def get(self, account_id: str, user_id: str) -> Optional[str]:
        return self._cache.get(self._key(account_id, user_id))

    def set(self, account_id: str, user_id: str, token: str) -> None:
        self._cache[self._key(account_id, user_id)] = token
        prefix = f"{account_id}:"
        payload = {k[len(prefix):]: v for k, v in self._cache.items() if k.startswith(prefix)}
        try:
            _atomic_json_write(self._path(account_id), payload)
        except Exception as exc:
            logger.warning("weixin: failed to persist reply context for %s: %s", _safe_id(account_id), exc)


class TypingTicketCache:
    """Short-lived typing-ticket cache from ``getconfig`` (default 10 min TTL)."""

    def __init__(self, ttl_seconds: float = 600.0) -> None:
        self._ttl = ttl_seconds
        self._cache: Dict[str, Tuple[str, float]] = {}

    def get(self, user_id: str) -> Optional[str]:
        entry = self._cache.get(user_id)
        if not entry:
            return None
        if time.time() - entry[1] >= self._ttl:
            self._cache.pop(user_id, None)
            return None
        return entry[0]

    def set(self, user_id: str, ticket: str) -> None:
        self._cache[user_id] = (ticket, time.time())


class WeixinSendError(RuntimeError):
    """iLink accepted the HTTP request but rejected the message.

    Raised when ``sendmessage`` returns a nonzero ``errcode``/``ret`` (rate limit,
    expired session, bad context token). The HTTP call succeeded, so without this
    the renderer would treat an undelivered reply as delivered and the dispatcher
    would persist it as a successful turn.
    """

    def __init__(self, endpoint: str, code: Any, payload: Dict[str, Any]) -> None:
        super().__init__(f"iLink {endpoint} rejected the message: code={code}")
        self.endpoint = endpoint
        self.code = code
        self.payload = payload


def protocol_error_code(resp: Dict[str, Any]) -> Any:
    """Return the nonzero iLink error code in ``resp``, else None.

    iLink reports failure as ``errcode`` on some endpoints and ``ret`` on others;
    both use 0 for success. Absent keys mean success.
    """
    for key in ("errcode", "ret"):
        value = resp.get(key)
        if value is not None and value != 0:
            return value
    return None


class WeixinClient:
    """Thin async wrapper over the iLink Bot HTTP API.

    Owns an ``aiohttp.ClientSession`` bound to ``base_url`` + bot ``token``.
    All methods return the raw decoded JSON so callers can inspect protocol
    error codes (``errcode``/``ret``) — e.g. ``-14`` (session expired) or
    ``-2`` (rate limited).
    """

    def __init__(self, *, token: str, base_url: str = ILINK_BASE_URL, account_id: str = "") -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id
        self._session: Any = None  # aiohttp.ClientSession, created in connect()

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # -- core request helpers --------------------------------------------------
    async def _post(self, endpoint: str, payload: Dict[str, Any], *, timeout_ms: int) -> Dict[str, Any]:
        body = _json_dumps({**payload, "base_info": _base_info()})
        url = f"{self.base_url}/{endpoint}"

        async def _do() -> Dict[str, Any]:
            async with self._session.post(url, data=body, headers=_headers(self.token, body)) as resp:
                raw = await resp.text()
                if not resp.ok:
                    raise RuntimeError(f"iLink POST {endpoint} HTTP {resp.status}: {raw[:200]}")
                return json.loads(raw)

        return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)

    async def _get(self, endpoint: str, *, timeout_ms: int) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        headers = {
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }

        async def _do() -> Dict[str, Any]:
            async with self._session.get(url, headers=headers) as resp:
                raw = await resp.text()
                if not resp.ok:
                    raise RuntimeError(f"iLink GET {endpoint} HTTP {resp.status}: {raw[:200]}")
                return json.loads(raw)

        return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)

    # -- receive / send --------------------------------------------------------
    async def get_updates(self, sync_buf: str) -> Dict[str, Any]:
        """Long-poll for new messages; returns {ret, msgs, get_updates_buf}.

        A client-side deadline expiry is returned as an empty result with
        ``_timed_out`` set instead of being silently equated with a healthy
        empty poll: the server normally answers well inside the deadline, so
        hitting it at all means zero server contact — the poll loop counts
        consecutive occurrences to make a black-holed network visible.
        """
        try:
            return await self._post(
                EP_GET_UPDATES, {"get_updates_buf": sync_buf}, timeout_ms=LONG_POLL_TIMEOUT_MS
            )
        except asyncio.TimeoutError:
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf, "_timed_out": True}

    async def send_message(self, *, to: str, text: str, context_token: Optional[str], client_id: str) -> Dict[str, Any]:
        """Send one text message. Raises :class:`WeixinSendError` if iLink rejects it.

        A 200 with a nonzero ``errcode``/``ret`` means the message was NOT
        delivered, so it must not be reported as success — the renderer relies on
        this to fail the turn instead of persisting a reply the user never saw.
        """
        if not text or not text.strip():
            raise ValueError("send_message: text must not be empty")
        message: Dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        resp = await self._post(EP_SEND_MESSAGE, {"msg": message}, timeout_ms=API_TIMEOUT_MS)
        code = protocol_error_code(resp)
        if code is not None:
            raise WeixinSendError(EP_SEND_MESSAGE, code, resp)
        return resp

    async def get_config(self, *, user_id: str, context_token: Optional[str]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"ilink_user_id": user_id}
        if context_token:
            payload["context_token"] = context_token
        return await self._post(EP_GET_CONFIG, payload, timeout_ms=CONFIG_TIMEOUT_MS)

    async def send_typing(self, *, to_user_id: str, typing_ticket: str, status: int) -> None:
        await self._post(
            EP_SEND_TYPING,
            {"ilink_user_id": to_user_id, "typing_ticket": typing_ticket, "status": status},
            timeout_ms=CONFIG_TIMEOUT_MS,
        )

    # -- QR login (dashboard setup flow) --------------------------------------
    async def get_bot_qrcode(self, bot_type: int = DEFAULT_BOT_TYPE) -> Dict[str, Any]:
        """Start a QR-login session. Returns {qrcode, qrcode_img_content}."""
        return await self._get(f"{EP_GET_BOT_QR}?bot_type={bot_type}", timeout_ms=API_TIMEOUT_MS)

    async def get_qrcode_status(self, qrcode: str) -> Dict[str, Any]:
        """Long-poll the scan status. On ``status == 'confirmed'`` the payload
        carries ``bot_token`` + ``baseurl`` + ``ilink_user_id``."""
        return await self._get(f"{EP_GET_QR_STATUS}?qrcode={quote(qrcode, safe='')}", timeout_ms=QR_TIMEOUT_MS)
