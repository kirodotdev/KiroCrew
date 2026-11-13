"""The wire client for a memory provider an App Kit app's backend serves.

An app declares the provider in ``contributes.memoryProviders``
(``apps/manifest_memory_providers.py``); its supervised backend answers
``POST /api/<route>/v1/<op>`` on loopback. Only the gateway calls it: every call
carries the per-app proxy signature (v1 and the replay-proof v2, as the app proxy
sends), and the backend's health watch gates it -- a backend the watch does not
report healthy is unavailable without a call.

The body and the answer are JSON::

    {"ok": true, "result": {...}}
    {"ok": false, "error": {"code": "...", "message": "...", "retry_after_secs": 5}}

Every answer is validated against :mod:`kiro_crew.memory_provider.contract`; one that
does not fit is :class:`ProviderInvalidResponse`, treated as unavailable. Answers are
capped at 200 items and 512 KB. The last 200 calls' latency and outcome are kept per
provider for the Health view (``latency_summary``).
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from kiro_crew.memory_provider.contract import (
    CONTEXT_TIMEOUT_DEFAULT,
    CONTEXT_TIMEOUT_MAX,
    MAX_ANSWER_BYTES,
    OP_DEADLINES,
    BlockingProvider,
    Capabilities,
    Health,
    IngestReceipt,
    MemoryItem,
    NewItem,
    Page,
    ProviderError,
    ProviderInvalidResponse,
    ProviderReadOnly,
    ProviderScope,
    ProviderUnavailable,
    TranscriptSpan,
    WriteResult,
    capabilities_from_wire,
    error_from_wire,
    health_from_wire,
    ingest_receipt_from_wire,
    item_from_wire,
    items_from_wire,
    page_from_wire,
    write_result_from_wire,
)

logger = logging.getLogger("kiro_crew.memory_provider")

ID_PREFIX = "app:"
#: How long a provider's ``describe`` answer is reused before it is asked again.
_DESCRIBE_TTL_SECS = 60.0
_LATENCY_WINDOW = 200


@dataclass(frozen=True)
class DeclaredProvider:
    """A provider an installed app declares, as the picker lists it."""

    provider_id: str
    app: str
    app_version: str
    display_name: str
    description: str
    route: str
    settings_page: str
    enabled: bool


def split_id(provider_id: str) -> tuple[str, str] | None:
    """``app:<app>/<provider>`` as ``(app, provider)``, or ``None`` for any other shape."""
    if not provider_id.startswith(ID_PREFIX):
        return None
    app, sep, provider = provider_id[len(ID_PREFIX) :].partition("/")
    if not sep or not app or not provider or "/" in provider:
        return None
    return app, provider


def make_id(app: str, provider: str) -> str:
    return f"{ID_PREFIX}{app}/{provider}"


def _manifest(app: str) -> Any:
    from kiro_crew.apps.manager import get_app_manifest

    return get_app_manifest(app)


def _enabled(app: str) -> bool:
    from kiro_crew.apps.manager import is_app_enabled

    return bool(is_app_enabled(app))


def declared_providers() -> list[DeclaredProvider]:
    """Every provider an installed app declares, enabled or not."""
    from kiro_crew.apps.manager import list_apps

    out: list[DeclaredProvider] = []
    for row in list_apps():
        name = str(row.get("name") or "")
        if not name:
            continue
        manifest = _manifest(name)
        if manifest is None:
            continue
        for decl in manifest.contributes.memoryProviders.usable():
            out.append(
                DeclaredProvider(
                    provider_id=make_id(name, decl.id),
                    app=name,
                    app_version=str(manifest.version),
                    display_name=decl.display_name,
                    description=decl.description,
                    route=decl.route,
                    settings_page=decl.settings_page,
                    enabled=_enabled(name),
                )
            )
    return out


def provider_app_version(provider_id: str) -> str:
    parts = split_id(provider_id)
    if parts is None:
        return ""
    manifest = _manifest(parts[0])
    return str(manifest.version) if manifest is not None else ""


_providers_lock = threading.Lock()
_providers: dict[tuple[str, str], AppMemoryProvider] = {}


def provider_for_id(provider_id: str) -> AppMemoryProvider | None:
    """The client for *provider_id*, or ``None`` when no enabled app offers it.

    One client per app version, so a new version's ``describe`` is asked afresh and
    its latency history starts empty.
    """
    parts = split_id(provider_id)
    if parts is None:
        return None
    app, pid = parts
    manifest = _manifest(app)
    if manifest is None or not _enabled(app):
        return None
    decl = next((d for d in manifest.contributes.memoryProviders.usable() if d.id == pid), None)
    if decl is None:
        return None
    key = (provider_id, str(manifest.version))
    with _providers_lock:
        client = _providers.get(key)
        if client is None or client.route != decl.route:
            client = AppMemoryProvider(
                app=app,
                provider=pid,
                route=decl.route,
                display_name=decl.display_name,
                app_version=str(manifest.version),
            )
            _providers[key] = client
    return client


def reset_clients() -> None:
    with _providers_lock:
        _providers.clear()


def _backend_port(app: str) -> int | None:
    from kiro_crew.apps.backend import get_app_backend_port

    return get_app_backend_port(app)


def _app_secret(app: str) -> str:
    from kiro_crew.apps.routes import _get_app_secret

    return _get_app_secret(app)


def _signed_headers(secret: str, method: str, target: str, body: bytes) -> dict[str, str]:
    from kiro_crew.apps.proxy_auth import (
        PROXY_NONCE_HEADER,
        PROXY_V2_HEADER,
        new_proxy_nonce,
        sign_proxy_request_v2,
    )

    ts = int(time.time())
    digest = hashlib.sha256(body).hexdigest()
    v1 = hmac.new(
        secret.encode(), f"{ts}:{method}:{target}:{digest}".encode(), hashlib.sha256
    ).hexdigest()
    nonce = new_proxy_nonce()
    return {
        "Content-Type": "application/json",
        "X-KiroCrew-Proxy": f"{ts}:{v1}",
        PROXY_NONCE_HEADER: nonce,
        PROXY_V2_HEADER: sign_proxy_request_v2(
            secret=secret, ts=ts, nonce=nonce, method=method, target=target, body=body
        ),
    }


class _DeadlinePassed(Exception):
    """A provider call ran past its whole deadline."""


class AppMemoryProvider(BlockingProvider):
    """A provider an app's backend serves. Gateway-only caller."""

    def __init__(
        self, *, app: str, provider: str, route: str, display_name: str, app_version: str
    ) -> None:
        self.app = app
        self.provider = provider
        self.route = route
        self.provider_id = make_id(app, provider)
        self.display_name = display_name
        self.app_version = app_version
        self._lock = threading.Lock()
        self._caps: Capabilities | None = None
        self._caps_at = 0.0
        self._calls: deque[tuple[str, float, str]] = deque(maxlen=_LATENCY_WINDOW)
        self.context_timeout = CONTEXT_TIMEOUT_DEFAULT

    # -- transport ---------------------------------------------------------

    def _record(self, op: str, started: float, outcome: str) -> None:
        with self._lock:
            self._calls.append((op, (time.monotonic() - started) * 1000.0, outcome))

    def _call(self, op: str, payload: dict[str, Any], timeout: float) -> Any:
        started = time.monotonic()
        try:
            result = self._exchange(op, payload, timeout)
        except ProviderError as exc:
            self._record(op, started, exc.code)
            raise
        self._record(op, started, "ok")
        return result

    @staticmethod
    def _until(connection: http.client.HTTPConnection, deadline: float) -> None:
        """Give the next socket read only what is left of *deadline*, or stop."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlinePassed
        if connection.sock is not None:
            connection.sock.settimeout(remaining)

    def _exchange(self, op: str, payload: dict[str, Any], timeout: float) -> Any:
        port = _backend_port(self.app)
        if not port:
            raise ProviderUnavailable("the provider's app backend is not healthy")
        secret = _app_secret(self.app)
        if not secret:
            raise ProviderUnavailable("the provider's app has no proxy secret")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        target = f"/api/{self.route}/v1/{op}"
        headers = _signed_headers(secret, "POST", target, body)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        deadline = time.monotonic() + timeout
        try:
            connection.request("POST", target, body=body, headers=headers)
            self._until(connection, deadline)
            response = connection.getresponse()
            chunks: list[bytes] = []
            size = 0
            # The socket timeout bounds one read; the deadline bounds the whole
            # answer, so a provider that trickles bytes cannot stretch a turn.
            while size <= MAX_ANSWER_BYTES:
                self._until(connection, deadline)
                chunk = response.read1(min(65_536, MAX_ANSWER_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
        except _DeadlinePassed:
            raise ProviderUnavailable(f"{op} timed out after {timeout:g}s") from None
        except (OSError, http.client.HTTPException) as exc:
            if time.monotonic() >= deadline:
                raise ProviderUnavailable(f"{op} timed out after {timeout:g}s") from None
            raise ProviderUnavailable(f"{op} failed: {type(exc).__name__}") from None
        finally:
            connection.close()
        if len(raw) > MAX_ANSWER_BYTES:
            raise ProviderInvalidResponse("answer is larger than 512 KB")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProviderInvalidResponse(f"{op} answer is not JSON (HTTP {response.status})")
        if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
            raise ProviderInvalidResponse(f"{op} answer has no ok flag")
        if not envelope["ok"]:
            raise error_from_wire(envelope.get("error"))
        return envelope.get("result")

    # -- contract ----------------------------------------------------------

    def describe(self) -> Capabilities:
        with self._lock:
            if self._caps is not None and time.monotonic() - self._caps_at < _DESCRIBE_TTL_SECS:
                return self._caps
        caps = capabilities_from_wire(self._call("describe", {}, OP_DEADLINES["describe"]))
        with self._lock:
            self._caps, self._caps_at = caps, time.monotonic()
        return caps

    def cached_capabilities(self) -> Capabilities | None:
        with self._lock:
            return self._caps

    def blocking_health(self) -> Health:
        started = time.monotonic()
        health = health_from_wire(self._call("health", {}, OP_DEADLINES["health"]))
        return Health(health.ok, health.detail, (time.monotonic() - started) * 1000.0)

    def blocking_context(
        self, scope: ProviderScope, query: str, budget_chars: int
    ) -> list[MemoryItem]:
        timeout = min(CONTEXT_TIMEOUT_MAX, max(0.5, self.context_timeout))
        result = self._call(
            "context",
            {"scope": scope.to_wire(), "query": query, "budget_chars": budget_chars},
            timeout,
        )
        return items_from_wire(result)

    def blocking_search(
        self, scope: ProviderScope, query: str, limit: int, kinds: frozenset[str] | None
    ) -> list[MemoryItem]:
        payload: dict[str, Any] = {"scope": scope.to_wire(), "query": query, "limit": limit}
        if kinds:
            payload["kinds"] = sorted(kinds)
        return items_from_wire(self._call("search", payload, OP_DEADLINES["search"]), limit=limit)

    def blocking_get(self, scope: ProviderScope, item_id: str) -> MemoryItem | None:
        result = self._call("get", {"scope": scope.to_wire(), "id": item_id}, OP_DEADLINES["get"])
        if not isinstance(result, dict):
            raise ProviderInvalidResponse("get result must be an object")
        item = result.get("item")
        return None if item is None else item_from_wire(item)

    def blocking_list(
        self,
        scope: ProviderScope,
        cursor: str | None,
        kinds: frozenset[str] | None,
        status: str | None,
    ) -> Page:
        payload: dict[str, Any] = {"scope": scope.to_wire(), "cursor": cursor}
        if kinds:
            payload["kinds"] = sorted(kinds)
        if status:
            payload["status"] = status
        return page_from_wire(self._call("list", payload, OP_DEADLINES["list"]))

    def _writable(self) -> None:
        if self.describe().read_only:
            raise ProviderReadOnly("this provider only reads")

    def blocking_put(
        self,
        scope: ProviderScope,
        item: NewItem,
        idempotency_key: str,
        item_id: str | None = None,
    ) -> WriteResult:
        self._writable()
        payload: dict[str, Any] = {
            "scope": scope.to_wire(),
            "item": item.to_wire(),
            "idempotency_key": idempotency_key,
        }
        if item_id is not None:
            payload["id"] = item_id
        return write_result_from_wire(self._call("put", payload, OP_DEADLINES["put"]))

    def blocking_delete(self, scope: ProviderScope, item_id: str) -> WriteResult:
        self._writable()
        result = self._call(
            "delete", {"scope": scope.to_wire(), "id": item_id}, OP_DEADLINES["delete"]
        )
        return write_result_from_wire(result)

    def blocking_ingest(
        self, scope: ProviderScope, span: TranscriptSpan, receipt_id: str
    ) -> IngestReceipt:
        self._writable()
        payload = {"scope": scope.to_wire(), "receipt_id": receipt_id, **span.to_wire()}
        return ingest_receipt_from_wire(self._call("ingest", payload, OP_DEADLINES["ingest"]))

    # -- health view -------------------------------------------------------

    def latency_summary(self) -> dict[str, Any]:
        """p50/p95 per operation and errors by code over the last 200 calls."""
        with self._lock:
            calls = list(self._calls)
        by_op: dict[str, list[float]] = {}
        errors: dict[str, int] = {}
        for op, ms, outcome in calls:
            by_op.setdefault(op, []).append(ms)
            if outcome != "ok":
                errors[outcome] = errors.get(outcome, 0) + 1

        def pct(values: list[float], q: float) -> float:
            ordered = sorted(values)
            return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)

        return {
            "calls": len(calls),
            "ops": {
                op: {"p50_ms": pct(v, 0.5), "p95_ms": pct(v, 0.95), "count": len(v)}
                for op, v in by_op.items()
            },
            "errors": errors,
            "last_error": next((o for _, _, o in reversed(calls) if o != "ok"), None),
        }


def item_list_from_wire(raw: object) -> list[MemoryItem]:
    """A bare list of wire items (the import reads JSONL lines through this)."""
    if not isinstance(raw, list):
        raise ProviderInvalidResponse("items must be a list")
    return [item_from_wire(entry) for entry in raw]
