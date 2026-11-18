"""The app backend's client for the gateway's ``_sel`` route (``apps/sel_routes.py``).

A spawned backend holds only its per-app secret (``KIROCREW_PROXY_SECRET``), never
the SEL MAC key. It trades the secret for the app-scoped token at
``POST /api/apps/{app}/token`` and posts each audit row to
``POST /api/apps/{app}/_sel``; the gateway signs and appends the row.

``caller``, ``source`` and ``forwarded`` are accepted so the call sites written for
the gateway's own writer keep working, and are dropped: the gateway sets the actor
from the token, so a backend cannot name one.

Failure: ``critical=True`` is synchronous and raises :class:`SelClientError`, so the
caller refuses the action it could not audit. Anything else is queued and sent from
one daemon thread, retried twice, then dropped with a warning; the backend never falls
back to loading the key.

Three writers exist, as only ``log_api_access`` is on the route's allowlist:
``log_api_access``, ``log_tool_invocation`` (an ``api_access`` row with the
operation ``tool.<name>``, never the gateway's ``tool_invocation`` kind) and ``log``
(a whole event as an ``api_access`` row, operation ``security.<event_type>``). Every
operation goes through :func:`normalize_operation` and the row is kept. Any other ``log_*``
method (``log_sel_rotation`` ...) warns once and writes nothing. ``log_governance_decision``
and ``log_governance_degraded`` are translated into app-attributed ``api_access`` rows
(operation ``governance.decision`` / ``governance.degraded``; no new event kind).
At exit a bounded drain sends what is queued and warns with the count it could not.
``outcome``, ``resources`` and ``error`` go through the writer's own ``_redact_and_clip``
(redact, then clip to 500 characters) before a row is queued or sent, so no field can
push the body past the route's 16 KiB cap and cost the row.
Requests go through ``loopback_urlopen``: no system proxy, no redirect.
shortcut: stdlib ``urllib`` and one thread; upgrade if a backend writes more than a
few rows a second.
"""

from __future__ import annotations

import atexit
import http.client
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.sel import SecurityEvent, _redact_and_clip

logger = logging.getLogger(__name__)

#: An app token's link window is 300 s (``token_auth_tokens.py`` ``LINK_WINDOW_SECS``);
#: renew before that.
_TOKEN_MAX_AGE = 240.0
_RETRY_DELAYS = (0.2, 1.0)
#: How long the exit drain waits for the queue before it gives up and counts the rest.
_EXIT_DRAIN_SECS = 3.0
_QUEUE_MAX = 1000
_TOOL_NAME_UNSAFE = re.compile(r"[^a-z0-9_.-]")
_OPERATION_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
#: An operation before the ``<app>.`` prefix (an app name is at most 64 characters too, so
#: the stored value stays under the shared 500-character clip).
_OPERATION_MAX = 64


def normalize_operation(operation: object) -> str:
    """The operation as stored: credentials are redacted first, then every character
    outside ``[A-Za-z0-9_.-]`` becomes ``_``, clipped to 64 characters, ``unnamed`` when
    nothing is left.

    Redaction runs before normalization because the redactor reads delimiters
    (``password=``, ``Bearer <token>``, ``user:pass@host``) that normalization removes.
    The client and the ``_sel`` route both call this, so no credential, newline or
    control character reaches a text view of the log. Neither logs the raw value.
    """
    redacted = _redact_and_clip(str(operation))
    return _OPERATION_UNSAFE.sub("_", redacted)[:_OPERATION_MAX] or "unnamed"


class SelClientError(RuntimeError):
    """The gateway did not take the row."""


_clients: dict[tuple[str, str, str], SelClient] = {}
_clients_lock = threading.Lock()


def from_env() -> SelClient | None:
    """The client for this process, or None when it is not a spawned app backend.

    A spawned backend carries the spawn marker, its app name, the gateway origin and
    its secret (``apps/backend.py``). Without all four it is not one, and the caller
    keeps the real writer. One client per process, so the token and the queue are shared.
    """
    env = os.environ
    key = (
        env.get("KIROCREW_APP_NAME", "").strip(),
        env.get("KIROCREW_GATEWAY_ORIGIN", "").strip(),
        env.get("KIROCREW_PROXY_SECRET", ""),
    )
    if env.get(KIROCREW_SPAWNED_ENV) != KIROCREW_SPAWNED_VALUE or not all(key):
        return None
    with _clients_lock:
        if key not in _clients:
            _clients[key] = SelClient(*key)
        return _clients[key]


class SelClient:
    """Same keyword names as the gateway's ``log_api_access`` writer, minus the actor."""

    def __init__(self, app: str, origin: str, secret: str, *, timeout: float = 5.0) -> None:
        if urllib.parse.urlsplit(origin).scheme not in ("http", "https") or not secret:
            raise ValueError("a gateway origin and the app secret are required")
        self._app, self._secret, self._timeout = app, secret, timeout
        self._origin = origin.rstrip("/")
        self._token = ""
        self._minted = 0.0
        self._lock = threading.Lock()
        self._queue: queue.Queue[dict[str, object]] = queue.Queue(_QUEUE_MAX)
        self._worker: threading.Thread | None = None
        self._unsupported_seen: set[str] = set()

    def log_api_access(
        self,
        *,
        operation: str,
        outcome: str,
        resources: str = "",
        error: str = "",
        critical: bool = False,
        caller: str = "",
        source: str = "",
        forwarded: bool | None = None,
    ) -> None:
        # The route adds ``<app>.`` itself, and normalizes the operation again.
        operation = normalize_operation(str(operation).removeprefix(f"{self._app}."))
        self._submit(
            "log_api_access",
            {"operation": operation, "outcome": outcome},
            resources,
            error,
            critical,
        )

    def log_tool_invocation(
        self,
        *,
        tool_name: str,
        outcome: str,
        resources: str = "",
        error: str = "",
        critical: bool = False,
        **_gateway_only: Any,
    ) -> None:
        """A tool use as an app row: operation ``tool.<name>``, filed as ``<app>.tool.<name>``.

        ``session_key``, ``agent``, ``source``, ``tool_kind``, ``request_id``,
        ``downstream_service`` and ``metadata`` are what the gateway's own writer takes;
        a backend cannot set them, so they are accepted and dropped. The name is a
        call site's own label (never user text), redacted first (a credential-shaped name
        must be redacted before it is lower-cased or clipped), then folded to the route's
        charset and length so its wording never costs the row.
        """
        redacted = _redact_and_clip(str(tool_name))
        name = _TOOL_NAME_UNSAFE.sub("_", redacted.lower())[:59] or "unnamed"
        self.log_api_access(
            operation=f"tool.{name}",
            outcome=outcome,
            resources=resources,
            error=error,
            critical=critical,
        )

    def log(self, event: SecurityEvent, *, critical: bool = False, **_gateway_only: Any) -> None:
        """A whole security event as an app row: operation ``security.<event_type>``.

        Still an ``api_access`` row, no new event kind. ``resources`` and the
        ``metadata`` items (as ``key=value``) go through the writer's own redact-then-clip,
        and the event's ``operation`` (the command or tool the event is about) leads the
        row's resources so a grant row names what it granted; the actor, ``agent``,
        ``source`` and ids are what the gateway sets and are dropped.
        A ``deny_exception`` is always critical: the grant waits for the write and the
        caller refuses it when the row is not taken.
        """
        parts = [f"operation={_redact_and_clip(str(event.operation))}"] if event.operation else []
        if event.resources:
            parts.append(str(event.resources))
        parts += [
            f"{key}={_redact_and_clip(str(value))}" for key, value in (event.metadata or {}).items()
        ]
        self.log_api_access(
            operation=f"security.{event.event_type}",
            outcome=event.outcome,
            resources=" ".join(parts),
            error=event.error,
            critical=critical or event.event_type == "deny_exception",
        )

    def log_governance_decision(
        self,
        *,
        tool_name: str,
        outcome: str,
        scope: str = "",
        item: str = "",
        rule: str = "",
        layer: str = "",
        reason: str = "",
        critical: bool = False,
        **_gateway_only: Any,
    ) -> None:
        """A governance decision as an app row: operation ``governance.decision``.

        Still an ``api_access`` row, no new event kind. The details go in ``resources``
        (redacted then clipped like every field); ``session_key``, ``agent`` and
        ``redact`` are what the gateway's own writer takes and are dropped.
        """
        self._governance(
            "decision", outcome, critical, tool=tool_name, scope=scope, item=item,
            rule=rule, layer=layer, reason=reason,
        )  # fmt: skip

    def log_governance_degraded(
        self,
        *,
        chokepoint: str,
        scope: str = "",
        app: str = "",
        reason: str = "",
        failed_closed: bool = False,
        **_gateway_only: Any,
    ) -> None:
        """A chokepoint that degraded, as an app row: operation ``governance.degraded``."""
        self._governance(
            "degraded", "failed_closed" if failed_closed else "failed_open", False,
            chokepoint=chokepoint, scope=scope, app=app, reason=reason,
        )  # fmt: skip

    def _governance(self, kind: str, outcome: str, critical: bool, **details: str) -> None:
        resources = " ".join(f"{k}={v}" for k, v in details.items() if v)
        self.log_api_access(
            operation=f"governance.{kind}", outcome=outcome, resources=resources, critical=critical
        )

    def __getattr__(self, name: str) -> Any:
        """A gateway-only writer (``log_sel_rotation`` ...) warns once, never raises.

        Shared code that runs inside a backend may call one; the route has no such kind,
        so the row is not written and the operator can see why. Anything else that is
        missing is a plain ``AttributeError``.
        """
        if not name.startswith("log_"):
            raise AttributeError(name)

        def _unsupported(*_a: Any, **_kw: Any) -> None:
            with self._lock:
                first = name not in self._unsupported_seen
                self._unsupported_seen.add(name)
            if first:
                logger.warning(
                    "app %s: %s is gateway-only; the row was not written", self._app, name
                )

        return _unsupported

    def _submit(
        self,
        method: str,
        kwargs: dict[str, object],
        resources: str,
        error: str,
        critical: bool,
    ) -> None:
        # The writer's own redact-then-clip (500 characters), applied before the row is
        # queued or sent: an unclipped field can pass the route's 16 KiB body cap, and a 413
        # is a hard failure that drops the row. Redact first, so a key straddling the clip
        # point is replaced whole rather than left as a prefix.
        kwargs["outcome"] = _redact_and_clip(str(kwargs["outcome"]))
        if resources:
            kwargs["resources"] = _redact_and_clip(str(resources))
        if error:
            kwargs["error"] = _redact_and_clip(str(error))
        if critical:
            kwargs["critical"] = True
            # Rows queued before this one land first (bounded, like the exit drain).
            self._wait_for_queue(_EXIT_DRAIN_SECS)
            self._send({"method": method, "kwargs": kwargs})
            return
        self._enqueue({"method": method, "kwargs": kwargs})

    def _enqueue(self, body: dict[str, object]) -> None:
        try:
            self._queue.put_nowait(body)
        except queue.Full:
            logger.warning("app %s: audit row dropped (queue full)", self._app)
            return
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._drain, name="sel-client", daemon=True)
                self._worker.start()
                # A daemon sender dies with the process: drain what is queued, bounded.
                atexit.register(self.flush_at_exit)

    def _wait_for_queue(self, budget: float) -> None:
        deadline = time.monotonic() + budget
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)

    def flush_at_exit(self, budget: float = _EXIT_DRAIN_SECS) -> None:
        """Wait up to *budget* seconds for the queue to empty, then say what was lost."""
        self._wait_for_queue(budget)
        if left := self._queue.unfinished_tasks:
            logger.warning("app %s: %d audit row(s) not sent at exit", self._app, left)

    def _drain(self) -> None:
        while True:
            body = self._queue.get()
            try:
                self._send_with_retries(body)
            except Exception:  # noqa: BLE001 - the sender must never die: one bad row, one warning
                logger.warning("app %s: audit row dropped (unexpected error)", self._app)
                logger.debug("sel-client sender error", exc_info=True)
            finally:
                self._queue.task_done()

    def _send_with_retries(self, body: dict[str, object]) -> None:
        for delay in (*_RETRY_DELAYS, None):
            try:
                self._send(body)
                return
            except SelClientError:
                if delay is None:
                    logger.warning("app %s: audit row dropped", self._app)
                else:
                    time.sleep(delay)

    def _send(self, body: dict[str, object]) -> None:
        for attempt in (0, 1):
            status = self._post(f"/_sel?token={urllib.parse.quote(self._get_token())}", body, {})
            if status == 204:
                return
            if status not in (401, 403) or attempt:
                break
            self._token = ""  # refused: the token may have expired or the app was restarted
        raise SelClientError(f"gateway answered {status}")

    def _get_token(self) -> str:
        if self._token and time.monotonic() - self._minted < _TOKEN_MAX_AGE:
            return self._token
        raw = self._post("/token", {}, {"X-App-Secret": self._secret}, want_body=True)
        token = raw.get("token") if isinstance(raw, dict) else None
        if not isinstance(token, str) or not token:
            raise SelClientError("token exchange refused")
        self._token, self._minted = token, time.monotonic()
        return token

    def _post(
        self,
        path: str,
        body: dict[str, object],
        headers: dict[str, str],
        *,
        want_body: bool = False,
    ) -> Any:
        # UTF-8, not ``\uXXXX`` escapes: an astral character is 12 escaped bytes but 4 in
        # UTF-8, so three 500-character fields stay under the route's 16 KiB cap. A lone
        # surrogate (a surrogateescape path) becomes ``?`` instead of raising here.
        data = json.dumps(body, ensure_ascii=False).encode("utf-8", "replace")
        req = urllib.request.Request(  # noqa: S310 - scheme checked in __init__
            f"{self._origin}/api/apps/{urllib.parse.quote(self._app)}{path}",
            data=data,
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            # The gateway's own origin: no system proxy, no redirect (the secret goes only there).
            with loopback_urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read() or b"{}") if want_body else resp.status
        except urllib.error.HTTPError as exc:
            if want_body:
                raise SelClientError(f"token exchange answered {exc.code}") from None
            return exc.code
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # HTTPException (``IncompleteRead``, ``RemoteDisconnected`` ...) is not an OSError.
            raise SelClientError(f"gateway unreachable: {type(exc).__name__}") from None
