"""Shared HTTP helper for REST-based providers.

Uses ``urllib.request`` on a worker thread rather than adding an HTTP client
dependency — Kiro Crew's convention is to prefer stdlib, and these adapters make a
handful of small JSON calls on a 2-minute cadence, which does not justify a new
third-party dep.

Two properties every caller depends on:

**Errors never carry the credential.** The auth header is built by the caller and
the raised message is scrubbed here, so a 401 body echoing back a token cannot
land in a log, a transcript, or a Slack message.

**Bodies are size-capped.** A provider returning a huge payload must not be able
to blow out memory or a model's context.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.providers import HTTP_TIMEOUT_SECS
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import redact_tokens

logger = logging.getLogger(__name__)

#: Hard cap on a provider response body. Generous for a JSON list of incidents,
#: small enough to bound memory and downstream context cost.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Only https is accepted. Provider tokens travel in these headers; permitting
#: http would let a mistyped or attacker-supplied config exfiltrate them in
#: cleartext.
_REQUIRED_SCHEME = "https"


#: Statuses that mean "back off", as distinct from "this request was wrong".
#: 429 is the explicit one; 503 and 504 are the ones a provider returns while shedding
#: load, where re-polling at full rate on the next heartbeat makes it worse.
RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 503, 504})

#: Cap on an honoured ``Retry-After``. A provider (or a proxy in front of one) asking
#: for a day would otherwise switch a source off with no operator action and no way
#: back short of a restart.
MAX_RETRY_AFTER_SECS = 15 * 60


def _parse_retry_after(raw: str | None) -> int:
    """Seconds from a ``Retry-After`` header, clamped. 0 when absent or unparseable.

    Only the delta-seconds form is honoured. The HTTP-date form is legal but rare from
    JSON APIs, and parsing dates here would mean trusting a provider's clock against
    ours — a wrong answer there silently disables a source, so absent is the safer read.
    """
    if not raw:
        return 0
    try:
        secs = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return max(0, min(secs, MAX_RETRY_AFTER_SECS))


class HttpError(RuntimeError):
    """A provider HTTP call failed. The message is always token-scrubbed."""

    def __init__(self, status: int, message: str, retry_after: int = 0) -> None:
        super().__init__(redact_tokens(message))
        self.status = status
        #: Seconds the provider asked us to wait, already clamped. 0 when it did not
        #: say. Read by the registry's backoff gate — before this existed, ``status``
        #: was assigned here and read nowhere, so a 429 and a 404 were handled
        #: identically and a rate-limited provider was re-polled every cycle.
        self.retry_after = retry_after

    @property
    def is_retryable(self) -> bool:
        """Whether re-polling soon is pointless or harmful, rather than just failing."""
        return self.status in RETRYABLE_STATUSES


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT_SECS,
) -> Any:
    """Perform a JSON HTTP request synchronously. Call via ``asyncio.to_thread``.

    Raises ``HttpError`` on a non-2xx response or a transport failure, with the
    message scrubbed of anything token-shaped.
    """
    if params:
        # doseq so list-valued params (PagerDuty's ``statuses[]``) serialize right.
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params, doseq=True)}"

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != _REQUIRED_SCHEME:
        raise HttpError(0, f"refusing non-https provider URL: {parsed.scheme or 'none'}")

    payload = json.dumps(body).encode("utf-8") if body is not None else None
    all_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        all_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=payload, headers=all_headers, method=method)
    try:
        # The rule's concern is a dynamic URL reaching urllib, because urllib honours
        # `file://` and would read arbitrary files. Unreachable here: the scheme is
        # checked against `_REQUIRED_SCHEME` above and anything but https raises before
        # this line, so no non-https scheme — file, ftp, data — can arrive.
        with urllib.request.urlopen(  # nosemgrep: dynamic-urllib-use-detected
            req, timeout=timeout
        ) as response:  # noqa: S310
            raw = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            detail = ""
        # ``Retry-After`` is read here because this is the only place the response
        # headers exist; by the time a caller sees the HttpError they are gone.
        retry_after = 0
        try:
            retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
        except Exception:  # noqa: BLE001 — a malformed header must not mask the error
            retry_after = 0
        raise HttpError(exc.code, f"HTTP {exc.code}: {detail[:400]}", retry_after) from None
    except urllib.error.URLError as exc:
        raise HttpError(0, f"transport error: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise HttpError(0, f"transport error: {exc}") from None

    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HttpError(0, f"malformed JSON response: {exc}") from None
