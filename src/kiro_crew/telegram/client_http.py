"""One Telegram Bot API round trip.

Resolves the method and file-download endpoints (``TELEGRAM_API_BASE_URL``, https except
for a loopback host), picks the proxy and keeps the aiohttp session. ``_api`` (a JSON
body) and ``_api_multipart`` run through the one ``_api_request``, which holds the
single 429 ``retry_after`` back-off and records ``kirocrew.telegram.api.duration``
unless the caller passes ``record=False``, as the ``getUpdates`` long poll does.
``_call_raw`` (``getMe``) is a bare POST with no retry and no metric. Each function here
whose first parameter is ``self`` is bound as the ``TelegramClient`` attribute of the
same name in :mod:`kiro_crew.telegram.client`, which keeps the client's state, so
``self.<name>(...)`` calls and instance and class patches reach it as they reach a
method defined in the class body. They read ``_proxy``, ``_session_lock`` and
``_token``, and read and write ``_session``. New transport work goes here; the Bot API
methods themselves stay on :class:`~kiro_crew.telegram.client.TelegramClient`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
from typing import TYPE_CHECKING, Any, Callable, Sequence

import aiohttp

from kiro_crew.messaging.outbound_files import OutboundFile, upload_filename
from kiro_crew.metrics.provider import get_recorder

if TYPE_CHECKING:
    from kiro_crew.telegram.client import TelegramClient

logger = logging.getLogger("kiro_crew.telegram.client")


#: Tail of every Bot API method route. A file download is derived from a
#: configured template by dropping it, so a template that does not end in it
#: has no download route (see :func:`_file_base`).
_METHOD_TAIL = "bot{token}/{method}"

# Default Bot API method template: the public host when no proxy is set.
_DEFAULT_API_BASE = "https://api.telegram.org/" + _METHOD_TAIL

#: Hosts for which a plaintext ``http://`` scheme is accepted. Everywhere else
#: the bot token would travel in clear text to a remote host, so only ``https``
#: is allowed; loopback never leaves the machine.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "[::1]", "localhost"})


def _is_loopback_host(host: str) -> bool:
    """True when *host* (netloc without port) is a loopback address."""
    bare = host.rsplit(":", 1)[0] if host.rsplit(":", 1)[-1].isdigit() else host
    return bare.lower() in _LOOPBACK_HOSTS


def _url_is_loopback(url: str) -> bool:
    """True when *url*'s host is a loopback address.

    Lets the client bypass a configured forward proxy for a loopback
    destination: a loopback request must stay on the machine, so routing it
    through a remote ``HTTPS_PROXY``/``HTTP_PROXY`` would send the token-bearing
    URL off-box in plaintext — exactly what the loopback-``http`` allowance is
    meant to avoid.
    """
    try:
        return _is_loopback_host(urllib.parse.urlparse(url).netloc)
    except ValueError:
        return False


def _api_base() -> str:
    """The Bot API method template, read at call time (not import).

    Override with ``TELEGRAM_API_BASE_URL`` for proxy/reverse-proxy setups
    (e.g. a network that blocks api.telegram.org). Must be a full method
    template containing ``{token}`` and ``{method}``.

    Reading on every call (rather than freezing at import) is deliberate: the
    gateway loads ``~/.kiro/crew/.env`` into ``os.environ`` *after* this module
    is imported, so an override placed there only takes effect if read later.

    The value is validated because it carries the bot token: the scheme must be
    ``https``, except for a loopback host (``127.0.0.1``/``::1``/``localhost``),
    where plaintext never leaves the machine. A missing ``{token}``/``{method}``
    placeholder, an unparseable value, or a non-loopback ``http`` URL is refused
    with a warning and the public default is used instead, so a misconfigured
    var fails safe to the public host rather than leaking the token.
    """
    raw = os.environ.get("TELEGRAM_API_BASE_URL")
    if not raw:
        return _DEFAULT_API_BASE
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        parsed = None
    reason = ""
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        reason = "not an http(s) URL"
    elif parsed.scheme == "http" and not _is_loopback_host(parsed.netloc):
        reason = "plaintext http is only allowed for loopback hosts"
    elif "{token}" not in raw or "{method}" not in raw:
        reason = "missing {token}/{method} placeholder"
    if reason:
        logger.warning(
            "Ignoring TELEGRAM_API_BASE_URL (%s); using the public Bot API host.",
            reason,
        )
        return _DEFAULT_API_BASE
    return raw


def _file_base() -> str | None:
    """Origin + path prefix for Telegram file downloads, or ``None``.

    Derived from the same parse :func:`_api_base` validated, so a configured
    proxy also serves ``/file/bot<token>/<path>`` downloads and the scheme is
    the one ``_api_base`` accepted (``HTTPS://`` is ``https``). Everything up
    to the trailing ``bot{token}/{method}`` is kept, so a path-prefixed
    template such as ``https://proxy.example.com/tg/bot{token}/{method}``
    yields the download base ``https://proxy.example.com/tg`` and the file URL
    stays inside the proxied route.

    ``None`` when the template does not end in that tail, or carries a query
    after it: no download route can be derived from it, and the token-bearing
    URL is not requested from a guessed one. The public default always has it.
    """
    parts = urllib.parse.urlsplit(_api_base())
    if parts.query or not parts.path.endswith(_METHOD_TAIL):
        return None
    prefix = parts.path[: -len(_METHOD_TAIL)].rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{prefix}"


#: Histogram for Bot API call latency. Outbound sends/edits are awaited inline in
#: the token-streaming path, so a call's duration *is* the time the render loop
#: was blocked -- which is what tells us whether the perceived slowness is
#: dominated by the fixed edit throttle or by network round-trips.
_API_DURATION_METRIC = "kirocrew.telegram.api.duration"


def _record_api_duration(
    method: str,
    elapsed_ms: float,
    *,
    ok: bool,
    err_code: int | None,
    timed_out: bool = False,
) -> None:
    """Emit one Bot API call duration to the metrics recorder.

    Best-effort: a metrics failure must never break a Telegram send.
    """
    logger.debug("Telegram API %s took %.0fms (ok=%s)", method, elapsed_ms, ok)
    if timed_out:
        outcome = "timeout"
    elif err_code == 429:
        outcome = "rate_limited"
    elif ok:
        outcome = "ok"
    else:
        outcome = "error"
    try:
        get_recorder().histogram(
            _API_DURATION_METRIC,
            elapsed_ms,
            unit="ms",
            attrs={"method": method, "outcome": outcome},
        )
    except Exception:
        logger.debug("telegram api duration metric emit failed", exc_info=True)


def _proxy_for(self: TelegramClient, url: str) -> str | None:
    """The forward proxy to use for *url*, or ``None`` to bypass it.

    A loopback destination must never traverse a remote forward proxy: the
    request URL carries the bot token, so routing a ``http://127.0.0.1``
    call through ``HTTPS_PROXY`` would leak the token off-box in plaintext.
    For every non-loopback host the configured proxy is used unchanged.
    """
    if _url_is_loopback(url):
        return None
    return self._proxy


async def _call_raw(self: TelegramClient, method: str, params: dict, timeout: int = 15) -> Any:
    """POST a Bot API method and return the parsed JSON body.

    Unlike :meth:`_api`, transport errors PROPAGATE (aiohttp / timeout /
    OSError) instead of collapsing to ``None``, so callers can distinguish
    "Telegram said no" from "network down".
    """
    session = await self._ensure_session()
    url = _api_base().format(token=self._token, method=method)
    async with session.post(
        url,
        json=params,
        proxy=self._proxy_for(url),
        timeout=aiohttp.ClientTimeout(total=timeout),
        # The URL carries the bot token. A TLS-terminating proxy can answer
        # with a redirect to a plaintext http:// Location; following it would
        # send the token off-box in clear text. Refuse redirects outright,
        # as download_file already does.
        allow_redirects=False,
    ) as resp:
        return await resp.json(content_type=None)


async def _ensure_session(self: TelegramClient) -> aiohttp.ClientSession:
    """Return the shared ClientSession, creating it once on demand.

    ``_api`` runs concurrently — the polling loop calls it via
    ``_get_updates`` while each spawned ``_invoke_message`` /
    ``_invoke_callback`` handler task also calls it. Guard the lazy init
    with a lock (double-checked) so two coroutines can't each build a
    session and leak one unclosed.
    """
    if self._session is None or self._session.closed:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
    return self._session


async def _api(
    self: TelegramClient,
    method: str,
    params: dict,
    timeout: int = 30,
    *,
    record: bool = True,
    err_out: dict | None = None,
) -> Any:
    """Call a Bot API method with a JSON body. Returns 'result' or None.

    Thin wrapper over :meth:`_api_request` with a JSON body; see there for
    the 429 back-off and ``err_out`` semantics.
    """
    return await self._api_request(
        method,
        lambda: {"json": params},
        timeout=timeout,
        record=record,
        err_out=err_out,
    )


async def _api_multipart(
    self: TelegramClient,
    method: str,
    params: dict,
    files: "Sequence[OutboundFile]",
    *,
    field_names: "Sequence[str]",
    filenames: "Sequence[str] | None" = None,
    timeout: int = 120,
    err_out: dict | None = None,
) -> Any:
    """Call a Bot API method with a ``multipart/form-data`` body.

    Uploading by multipart is the only transport with the useful ceiling —
    by-URL caps photos at 5 MB, multipart at 10 MB — and it is the only one
    that works at all for a local file the Bot API has never seen.

    Scalar params are sent as form fields and non-scalars are JSON-encoded,
    which is what the Bot API asks for ("all queries must be made using
    UTF-8"; nested objects such as ``reply_markup`` and ``media`` travel as
    JSON strings). *field_names* is positional against *files*, so the
    caller decides whether a part is ``photo`` or ``document`` or an
    ``attach://`` name a ``media`` descriptor refers to.

    *filenames*, when supplied, is used verbatim in place of
    :func:`upload_filename`. That helper exists to sanitize and re-scan a name
    that came out of LLM-authored reply text; a file this process generated
    itself — a synthesized voice reply in a temp dir — has no such provenance,
    and putting it through a raster-oriented extension mapping would rename it
    to something the Bot API then rejects. Omit it for anything whose name the
    model influenced.

    The body is rebuilt per attempt because an aiohttp ``FormData`` is
    consumed as it is written — replaying one sends an empty body, which
    Telegram answers with a 400 that reads like a payload-shape bug.
    """

    def _body() -> dict:
        form = aiohttp.FormData()
        for key, value in params.items():
            if isinstance(value, bool):
                form.add_field(key, "true" if value else "false")
            elif isinstance(value, (int, float, str)):
                form.add_field(key, str(value))
            else:
                form.add_field(key, json.dumps(value))
        for index, (name, item) in enumerate(zip(field_names, files)):
            form.add_field(
                name,
                item.data,
                filename=(
                    filenames[index]
                    if filenames is not None and index < len(filenames)
                    else upload_filename(item, index)
                ),
                content_type=item.mime or "application/octet-stream",
            )
        return {"data": form}

    return await self._api_request(method, _body, timeout=timeout, err_out=err_out)


async def _api_request(
    self: TelegramClient,
    method: str,
    body: "Callable[[], dict]",
    timeout: int = 30,
    *,
    record: bool = True,
    err_out: dict | None = None,
) -> Any:
    """Call a Bot API method. Returns the 'result' field or None on error.

    Honors a single 429 ``retry_after`` back-off: a rate-limited edit that
    we simply dropped would freeze the streaming bubble until the next
    chunk, which reads as a stutter -- so we wait out the (usually short)
    cool-down once and retry instead.

    ``err_out``, when supplied, is populated with ``error_code`` and
    ``description`` on a Telegram-level failure. Callers use it to tell a
    PERMANENT failure (the method does not exist on this server) apart from
    a transient one (rate limit, network), so they can stop re-probing an
    unsupported method without disabling it on a blip.

    *body* is a FACTORY, not a body: it is called once per attempt so a
    streamed request body (a multipart form) is rebuilt for the retry
    instead of replayed empty.
    """
    session = await self._ensure_session()

    url = _api_base().format(token=self._token, method=method)
    # ONE timer for the whole call, not per attempt: the caller is blocked
    # for the entire span including a 429 ``retry_after`` sleep, and that
    # multi-second stall is exactly the user-visible latency the metric
    # exists to expose. Per-attempt timing dropped it (it fell between two
    # timers) and split one logical call into two misleadingly short samples.
    call_started = time.monotonic()

    def _elapsed_ms() -> float:
        return (time.monotonic() - call_started) * 1000.0

    for attempt in range(2):
        try:
            async with session.post(
                url,
                proxy=self._proxy_for(url),
                timeout=aiohttp.ClientTimeout(total=timeout),
                # The URL carries the bot token; refuse a proxy redirect to a
                # plaintext http:// Location rather than follow it off-box.
                allow_redirects=False,
                **body(),
            ) as resp:
                body_is_json = True
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    # A body that is not JSON (an HTML error page from the edge
                    # or a proxy) is a failed call like any other. Its HTTP
                    # status stands in for the error code the body did not
                    # carry, so the failure path below records and reports it.
                    # That status is not the Bot API's verdict, and err_out
                    # says so (``body_is_json``).
                    body_is_json = False
                    data = {
                        "ok": False,
                        "error_code": resp.status,
                        "description": "response body is not JSON",
                    }
                if data and data.get("ok"):
                    if record:
                        _record_api_duration(method, _elapsed_ms(), ok=True, err_code=None)
                    return data.get("result")
                # Log Telegram API errors.
                err_code = data.get("error_code") if data else None
                err_desc = data.get("description") if data else None
                # 400 "message is not modified" is benign during streaming.
                if err_code == 400 and "not modified" in (err_desc or "").lower():
                    if record:
                        _record_api_duration(method, _elapsed_ms(), ok=True, err_code=None)
                    return {}  # treat as success (no change needed)
                # 429: respect the server's retry_after once, then give up.
                # Deliberately NOT recorded here -- the retry continues the
                # same logical call, so the sample is emitted once at the
                # terminal outcome with the sleep included.
                if err_code == 429 and attempt == 0:
                    retry_after = 1.0
                    try:
                        retry_after = float((data.get("parameters") or {}).get("retry_after", 1.0))
                    except (TypeError, ValueError):
                        pass
                    await asyncio.sleep(min(max(retry_after, 0.5), 5.0))
                    continue
                if record:
                    _record_api_duration(method, _elapsed_ms(), ok=False, err_code=err_code)
                if err_out is not None:
                    err_out["error_code"] = err_code
                    err_out["description"] = err_desc
                    if not body_is_json:
                        err_out["body_is_json"] = False
                logger.warning(
                    "Telegram API %s failed: code=%s desc=%s",
                    method,
                    err_code,
                    err_desc,
                )
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # Record BEFORE returning: a timeout or connection failure is the
            # LONGEST a caller ever blocks, so dropping it here biased the
            # histogram towards calls that got a response and hid the worst
            # stalls (survivorship bias).
            if record:
                _record_api_duration(
                    method,
                    _elapsed_ms(),
                    ok=False,
                    err_code=None,
                    timed_out=isinstance(exc, asyncio.TimeoutError),
                )
            # Log only the exception type — its str() can embed the request
            # URL, which contains the bot token (a registered credential).
            logger.warning("Telegram API %s transport error: %s", method, type(exc).__name__)
            return None
    return None


def _resolve_proxy() -> str | None:
    """Resolve outbound proxy from environment."""
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    return None
