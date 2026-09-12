"""A2A (Agent-to-Agent) provider — a remote agent driven as a subagent.

An :class:`A2AProvider` speaks the A2A v1.0 protocol (JSON-RPC 2.0 over HTTP +
SSE) to a remote agent and presents Kiro Crew's provider-agnostic
:class:`~kiro_crew.providers.base.LLMProvider` contract, so the subagent manager
drives it exactly like a local ACP session.

Conversation model (see the Rodin-over-A2A design): the A2A ``contextId`` IS the
subagent conversation; each ``stream()`` call is one A2A task within that
context. ``spawn_continue`` reconstructs the provider with the retained
``contextId`` and sends a new task in that context (``referenceTaskIds`` is
attached only when the same provider instance sent an earlier task; the
persisted record carries the ``contextId`` alone) — a terminal A2A task is never
reopened.

Everything the ABC does not force is left at its (A2A-friendly) default:
``supports_steer`` stays False (so a ``mode=interrupt`` steer gets the typed
rejection for free), ``is_session_sharing_eligible`` stays False (a remote agent
cannot host multiplexed local sessions), ``runtime_info`` stays ``(None, None)``
(no local process to abort-push), ``billing_stats`` stays None (unmetered here).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, Callable
from urllib.parse import urlsplit

import aiohttp

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    PROVIDER_LABEL_A2A,
)
from kiro_crew.providers.base import CancelOutcome, LLMEvent, LLMProvider

logger = logging.getLogger(__name__)


class A2AStreamError(RuntimeError):
    """A turn's A2A stream failed or ended without a terminal task state.

    Raised (never yielded as a completion) so the subagent runner's generic
    error arm marks the run FAILED — the completion event's text/stop_reason
    are read only for billing, so an error reported there surfaces as a
    silent empty success. ``transient = False`` is the structural verdict
    ``acp_error_is_transient`` reads first: a remote agent that died mid-turn
    must not be blind-retried by the transient-backend ladder (the retry
    would re-send the whole task).
    """

    transient = False


#: Required by the A2A v1.0 wire contract — a request without it is treated as
#: the older 0.3 dialect and rejected with JSON-RPC error -32009.
A2A_VERSION_HEADER = {"A2A-Version": "1.0"}

#: A2A task terminal states (SCREAMING_SNAKE per the protocol enum encoding).
_TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
_TASK_STATE_FAILED = "TASK_STATE_FAILED"
#: The v1.0 proto enum (``a2a_pb2``, and what the reference SDK's TaskUpdater
#: emits) spells it CANCELED; the 0.3 compatibility layer spells it CANCELLED.
#: A cancel the client cannot recognise is indistinguishable from a lost
#: connection, so both are terminal here.
_TASK_STATE_CANCELED = "TASK_STATE_CANCELED"
_TASK_STATE_CANCELLED = "TASK_STATE_CANCELLED"
_CANCEL_STATES = frozenset({_TASK_STATE_CANCELED, _TASK_STATE_CANCELLED})
_TASK_STATE_REJECTED = "TASK_STATE_REJECTED"
_TERMINAL_STATES = frozenset(
    {_TASK_STATE_COMPLETED, _TASK_STATE_FAILED, _TASK_STATE_REJECTED} | _CANCEL_STATES
)
_FAILURE_STATES = frozenset({_TASK_STATE_FAILED, _TASK_STATE_REJECTED})
#: The remote agent asking THIS client to supply a credential mid-task (spec
#: 7.6). Never satisfied: a client that answers it is a credential-exfiltration
#: channel. Treated as a failed turn with the remote's request text preserved.
_TASK_STATE_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"

#: Bound on how long the SSE read may sit SILENT before the turn is treated as
#: hung. This is an idle bound (``sock_read``), not a total: a healthy remote turn
#: that streams for an hour is legitimate, and the subagent manager's wall-clock
#: timeout is the ceiling on total duration.
_IDLE_READ_TIMEOUT_SECS = 1800.0

#: Size caps on what a remote may make this process hold. A remote endpoint is
#: operator configuration, but the subagent memory guard cannot measure a remote
#: run (there is no pid), so an oversized Agent Card or an endless stream of
#: chunks would otherwise grow gateway memory without limit. An Agent Card is a
#: few KB; a turn's text is a transcript, not a payload.
_MAX_CARD_BYTES = 256 * 1024
_MAX_TURN_TEXT_CHARS = 4 * 1024 * 1024
_CAP_ERROR = (
    f"the remote agent's output exceeded {_MAX_TURN_TEXT_CHARS} characters in one turn; "
    "the turn was abandoned"
)


def _origin(url: str) -> str:
    """``scheme://host[:port]`` of *url*, lowercased; empty when unparsable or when the
    URL carries userinfo (``https://allowed.example:x@evil.example/``), which would
    otherwise let a netloc prefix pass an origin comparison while the request went
    to the host after the ``@``. Built from the parsed hostname and port."""
    try:
        u = urlsplit(str(url))
        host = u.hostname
        port = u.port
    except ValueError:
        return ""
    if not u.scheme or not host or u.username is not None or u.password is not None:
        return ""
    host = host.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{u.scheme.lower()}://{host}" + (f":{port}" if port is not None else "")


def _credentials_may_travel(url: str) -> bool:
    """True when a credential may be sent to *url*: ``https``, or ``http`` to loopback.

    Loopback (``localhost``, ``127.0.0.0/8``, ``::1``) is the one plaintext case
    that never leaves the host -- it is what the test fixture uses. Anything
    unparsable is refused.
    """
    try:
        u = urlsplit(str(url))
    except ValueError:
        return False
    scheme = (u.scheme or "").lower()
    if scheme == "https":
        return True
    if scheme != "http":
        return False
    host = (u.hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _context_mismatch(retained: str | None, received: object) -> str:
    """Error text for a frame whose ``contextId`` is not the retained conversation's.

    A continuation carries the retained ``contextId``; a server that answers with
    another one is not continuing this conversation, and recording its output
    under the original handle would corrupt the record. The turn fails instead.
    """
    return (
        f"a2a contextId mismatch: continuing {retained!r} but the remote answered "
        f"under {str(received)!r}; refusing to record another conversation's output"
    )


def _scheme_type(scheme: Any) -> str:
    """The Agent Card security scheme TYPE a scheme entry declares.

    v1.0 proto-JSON wraps the variant (``{"httpAuthSecurityScheme": {...}}``);
    0.3 used ``{"type": "http", "scheme": "bearer"}``. Both are folded to the
    small vocabulary the driver's scheme table speaks: ``bearer`` for HTTP
    bearer, OAuth2 and OIDC (one ``Authorization: Bearer`` header each),
    ``apiKey``, ``mutualTLS``, or the raw discriminator when unrecognised.
    """
    if not isinstance(scheme, dict):
        return "unknown"
    if "httpAuthSecurityScheme" in scheme or scheme.get("type") == "http":
        inner = scheme.get("httpAuthSecurityScheme") or scheme
        return "bearer" if str(inner.get("scheme", "")).lower() == "bearer" else "http"
    if "oauth2SecurityScheme" in scheme or scheme.get("type") == "oauth2":
        return "bearer"
    if "openIdConnectSecurityScheme" in scheme or scheme.get("type") == "openIdConnect":
        return "bearer"
    if "apiKeySecurityScheme" in scheme or scheme.get("type") == "apiKey":
        return "apiKey"
    if "mutualTlsSecurityScheme" in scheme or scheme.get("type") == "mutualTLS":
        return "mutualTLS"
    return str(scheme.get("type") or next(iter(scheme), "unknown"))


def _parts_text(parts: Any) -> str:
    """Concatenate the ``text`` of every text part in an A2A parts list."""
    if not isinstance(parts, list):
        return ""
    out = []
    for p in parts:
        if isinstance(p, dict):
            t = p.get("text")
            if isinstance(t, str) and t:
                out.append(t)
    return "".join(out)


class A2AProvider(LLMProvider):
    """LLMProvider backed by a remote A2A agent."""

    def __init__(
        self,
        *,
        name: str,
        agent_card_url: str,
        credentials: "Callable[[], dict[str, str] | None] | None" = None,
        supported_schemes: "frozenset[str]" = frozenset(),
        credential_origin: str = "",
        context_id: str | None = None,
    ) -> None:
        self._name = name
        self._agent_card_url = agent_card_url
        # ``credentials()`` returns the auth headers for ONE request (read fresh
        # each call so a rotated credential is picked up), or None for no
        # authentication. ``supported_schemes`` are the Agent Card security scheme
        # TYPES (``http``-bearer, ``oauth2``, ``openIdConnect``, ...) those headers
        # satisfy; the card's declared requirements are checked against them in
        # start(). ``credential_origin`` is the ONE ``scheme://host[:port]`` the
        # credential may be sent to, pinned by the operator outside config; with
        # credentials configured, start() refuses a card URL on any other origin.
        # The driver builds all three -- this class never reads config or env.
        self._credentials = credentials
        self._supported_schemes = frozenset(supported_schemes)
        self._credential_origin = str(credential_origin or "").lower()
        # Durable conversation handle: adopted from the first response when not
        # supplied, reused for every subsequent turn. This is the value the
        # continuation layer persists and hands back on resume.
        self._context_id: str | None = context_id
        # In-flight / last turn — the CancelTask target and the referenceTaskIds
        # anchor for the next turn.
        self._current_task_id: str | None = None
        self._message_endpoint: str = ""
        self._agent_card: dict[str, Any] | None = None
        self._session: Any = None  # aiohttp.ClientSession, created in start()
        self._started = False

    # Persisted so ``_provider_label_of`` can identify this provider WITHOUT
    # importing this module (avoids the providers->subagent cycle). The helper
    # duck-types on this attribute.
    provider_label = PROVIDER_LABEL_A2A

    # ── Public accessors used by the continuation layer ──

    @property
    def context_id(self) -> str | None:
        """Durable A2A conversation handle for this subagent (persist on keep)."""
        return self._context_id

    @property
    def session_id(self) -> str:
        """The A2A contextId, reused as the persisted session identifier.

        A2A has no on-disk session files, so ``cleanup_session`` stays a no-op
        (the ABC default). But the subagent run path captures ``session_id``
        into ``state.json`` and hands it back on resume — reusing the durable
        ``contextId`` here means the continuation layer persists and restores the
        A2A conversation handle through the existing machinery, with no new
        state field. Empty until the first response adopts a contextId.
        """
        return self._context_id or ""

    @property
    def current_task_id(self) -> str | None:
        return self._current_task_id

    # ── LLMProvider ABC ──

    async def start(self) -> None:
        """Open the HTTP client and fetch the remote Agent Card.

        A failed card fetch is fatal (raises): on the resume path the subagent
        manager's ``resume_failed`` guard then fires because ``_resumed`` was
        reported False, and on a fresh spawn the run tombstones cleanly rather
        than streaming against an unreachable endpoint. The client session is
        closed before re-raising: a provider that fails here is never stashed on
        the run record, so nothing else would release it.
        """
        # Everything this client sends -- the task text on every message POST and
        # any credential on the card fetch and those POSTs (all origin-pinned to
        # the card URL) -- travels only over TLS. Task text is session-derived
        # data, so this holds for an unauthenticated remote too; an ``http://``
        # card URL is refused before any request. Loopback is exempt: the test
        # fixture is plain HTTP on 127.0.0.1.
        if not _credentials_may_travel(self._agent_card_url):
            raise A2AStreamError(
                f"agent {self._name!r}: agent_card_url is not https://; task text and "
                "credentials are only sent over TLS (loopback excepted)"
            )
        # ...and only to the origin the operator pinned them to. The driver checks
        # this at construction; repeated here so a provider built any other way
        # cannot carry a credential to an unpinned host. No origin means refuse.
        if self._credentials is not None and (
            not self._credential_origin or _origin(self._agent_card_url) != self._credential_origin
        ):
            raise A2AStreamError(
                f"agent {self._name!r}: agent_card_url origin {_origin(self._agent_card_url) or '?'} "
                f"is not the origin its credential is pinned to "
                f"({self._credential_origin or 'none pinned'}); refused"
            )
        self._session = aiohttp.ClientSession()
        try:
            # No redirects: a redirect away from the operator-configured host is
            # the SSRF hop the origin pin in _resolve_message_endpoint exists to
            # stop, and aiohttp would follow it silently. raise_for_status
            # ignores 3xx, so it is refused explicitly.
            async with self._session.get(
                self._agent_card_url,
                headers=self._headers(A2A_VERSION_HEADER),
                allow_redirects=False,
            ) as resp:
                if 300 <= resp.status < 400:
                    raise A2AStreamError(
                        f"agent card fetch was redirected (HTTP {resp.status}); "
                        "redirects are refused"
                    )
                resp.raise_for_status()
                # Bound the body BEFORE parsing: read one byte past the cap and
                # refuse if it arrived, rather than trusting Content-Length.
                body = await resp.content.read(_MAX_CARD_BYTES + 1)
                if len(body) > _MAX_CARD_BYTES:
                    raise A2AStreamError(
                        f"agent card for {self._name!r} exceeds {_MAX_CARD_BYTES} bytes; refused"
                    )
                parsed = json.loads(body.decode("utf-8", "replace"))
                if not isinstance(parsed, dict):
                    raise A2AStreamError(f"agent card for {self._name!r} is not a JSON object")
                card: dict[str, Any] = parsed
            self._agent_card = card
            self._message_endpoint = self._resolve_message_endpoint(card)
            self._check_security_requirements(card)
        except BaseException:
            session, self._session = self._session, None
            try:
                await session.close()
            except Exception:  # pragma: no cover - best-effort release
                logger.debug("A2A client session close failed after start error", exc_info=True)
            raise
        self._started = True

    def _headers(self, base: dict[str, str]) -> dict[str, str]:
        """*base* plus this request's authentication headers, if any."""
        if self._credentials is None:
            return dict(base)
        extra = self._credentials()
        return {**base, **extra} if extra else dict(base)

    def _check_security_requirements(self, card: dict[str, Any]) -> None:
        """Refuse to start when the card requires a scheme this client cannot satisfy.

        Spec 7.3: the card's ``securityRequirements`` is a list of alternatives,
        each naming scheme keys in ``securitySchemes``. One alternative whose
        every scheme type is in ``supported_schemes`` (and for which credentials
        are configured) is enough. No requirements, or an empty alternative,
        means the server accepts anonymous access. Anything else is a fail-closed
        start: sending an unauthenticated request "to see" is exactly the silent
        misconfiguration an operator cannot detect.
        """
        requirements = card.get("securityRequirements") or card.get("security") or []
        if not isinstance(requirements, list) or not requirements:
            return
        schemes = card.get("securitySchemes") or {}
        if not isinstance(schemes, dict):
            schemes = {}
        for alternative in requirements:
            names: list[str] = []
            if isinstance(alternative, dict):
                # v1.0 proto-JSON shape {"schemes": {name: {...}}} and the 0.3
                # shape {name: [scopes]} both key on the scheme name.
                inner = alternative.get("schemes")
                names = list(inner.keys()) if isinstance(inner, dict) else list(alternative.keys())
            if not names:
                return  # an empty alternative: anonymous access is acceptable
            types = {_scheme_type(schemes.get(n)) for n in names}
            if types <= self._supported_schemes and self._credentials is not None:
                return
        raise A2AStreamError(
            f"agent card for {self._name!r} requires authentication this client is not "
            f"configured for (card schemes: "
            f"{sorted(_scheme_type(v) for v in schemes.values()) or 'unnamed'}; "
            f"configured: {sorted(self._supported_schemes) or 'none'}). "
            "Set the entry's auth scheme and credential rather than sending unauthenticated requests."
        )

    def _resolve_message_endpoint(self, card: dict[str, Any]) -> str:
        """Pick the JSON message endpoint from the Agent Card.

        The card's ``supportedInterfaces`` lists {url, protocolBinding}. Prefer
        an HTTP+JSON / JSON-RPC binding; fall back to the card's own origin
        (the shim serves message:send at the root path).

        **Origin pin.** The card is fetched from an operator-configured URL,
        but its contents are remote-controlled. An interface URL on a different
        scheme or host would make this gateway POST the task to wherever the
        card author chose -- an internal service, a metadata endpoint. Only
        URLs sharing the card URL's origin are accepted; anything else fails
        the start (fail closed), it is never silently downgraded to the fallback.
        """
        card_origin = _origin(self._agent_card_url)
        interfaces = card.get("supportedInterfaces")
        if isinstance(interfaces, list):
            candidates: list[str] = []
            for iface in interfaces:
                if not isinstance(iface, dict):
                    continue
                binding = str(iface.get("protocolBinding", "")).upper()
                url = iface.get("url")
                if url and ("JSON" in binding or "RPC" in binding or "HTTP" in binding):
                    candidates.append(str(url))
            for iface in interfaces:
                if isinstance(iface, dict) and iface.get("url"):
                    candidates.append(str(iface["url"]))
            for url in candidates:
                if _origin(url) != card_origin:
                    raise A2AStreamError(
                        f"agent card for {self._name!r} names a message endpoint on another "
                        f"origin ({_origin(url) or url!r}); only {card_origin} is accepted"
                    )
                return url
        # Fallback: derive the root from the well-known card URL.
        marker = "/.well-known/"
        if marker in self._agent_card_url:
            return self._agent_card_url.split(marker, 1)[0] + "/"
        return self._agent_card_url

    async def shutdown(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("A2AProvider %s: session close failed", self._name, exc_info=True)
            self._session = None
        self._started = False

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        """Send one A2A task and yield provider events for its lifecycle.

        Emits ``EVENT_TEXT_CHUNK`` per WORKING-status text delta and a single
        terminal ``EVENT_COMPLETE`` (carrying the final artifact text; on a
        FAILED/REJECTED terminal the error text is surfaced as the completion
        text with ``stop_reason`` in the ``error:`` family).
        """
        if not self._started:
            await self.start()

        msg_id = uuid.uuid4().hex
        a2a_message: dict[str, Any] = {
            "role": "ROLE_USER",
            "parts": [{"text": message}],
            "messageId": msg_id,
        }
        if self._context_id:
            a2a_message["contextId"] = self._context_id
        # Anchor this turn to the previous task in the same conversation.
        if self._current_task_id:
            a2a_message["referenceTaskIds"] = [self._current_task_id]

        payload = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "SendStreamingMessage",
            "params": {"message": a2a_message},
        }

        artifact_text = ""
        final_text = ""
        stop_reason = "end_turn"
        error_text = ""
        failed = False  # a protocol- or task-level failure was signalled
        # Terminal-state discipline (mirror of what we ask of the server): a
        # stream that ENDS without a terminal task state is a truncated turn —
        # connection lost, server died mid-stream — never a completed one.
        saw_terminal = False

        headers = {
            **A2A_VERSION_HEADER,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            async with self._session.post(
                self._message_endpoint,
                json=payload,
                headers=self._headers(headers),
                allow_redirects=False,
                timeout=self._make_timeout(),
            ) as resp:
                resp.raise_for_status()
                async for raw in _iter_sse_data(resp.content):
                    try:
                        frame = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    result = frame.get("result")
                    if not isinstance(result, dict):
                        # A JSON-RPC error frame terminates the turn as failed.
                        err = frame.get("error")
                        if isinstance(err, dict):
                            error_text = str(err.get("message") or err)
                            stop_reason = "error: a2a"
                            failed = True
                            saw_terminal = True  # protocol-level terminal outcome
                            break
                        continue

                    task = result.get("task")
                    if isinstance(task, dict):
                        self._current_task_id = task.get("id") or self._current_task_id
                        ctx = task.get("contextId")
                        if ctx and not self._context_id:
                            self._context_id = ctx
                        elif ctx and str(ctx) != str(self._context_id):
                            raise A2AStreamError(_context_mismatch(self._context_id, ctx))
                        continue

                    art = result.get("artifactUpdate")
                    if isinstance(art, dict):
                        artifact = art.get("artifact")
                        if isinstance(artifact, dict):
                            delta = _parts_text(artifact.get("parts"))
                            if delta:
                                artifact_text += delta
                                if len(artifact_text) + len(final_text) > _MAX_TURN_TEXT_CHARS:
                                    stop_reason = "error: a2a output cap"
                                    failed = True
                                    saw_terminal = True
                                    error_text = _CAP_ERROR
                                    break
                                # Artifacts ARE the result in A2A ("results go in
                                # Artifacts, not Messages"), and run.py assembles
                                # the run's result from EVENT_TEXT_CHUNK alone, so
                                # artifact deltas must stream as chunks — an
                                # artifact reported only on EVENT_COMPLETE
                                # (billing-only) surfaces as "_No response._".
                                # One exception: servers built on the reference
                                # SDK stream progress as status-message deltas
                                # and then send ONE artifact repeating the whole
                                # text. That recap has already been shown; do not
                                # show it twice. Only an EXACT full recap is
                                # skipped — a distinct artifact whose text merely
                                # occurs inside earlier status text is output.
                                if not (final_text and delta.strip() == final_text.strip()):
                                    yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=delta)
                        continue

                    su = result.get("statusUpdate")
                    if isinstance(su, dict):
                        tid = su.get("taskId")
                        if tid:
                            self._current_task_id = tid
                        ctx = su.get("contextId")
                        if ctx and not self._context_id:
                            self._context_id = ctx
                        elif ctx and str(ctx) != str(self._context_id):
                            raise A2AStreamError(_context_mismatch(self._context_id, ctx))
                        status = su.get("status") or {}
                        state = str(status.get("state") or "")
                        msg = status.get("message")
                        delta = _parts_text(msg.get("parts")) if isinstance(msg, dict) else ""
                        if delta:
                            final_text += delta
                            if len(artifact_text) + len(final_text) > _MAX_TURN_TEXT_CHARS:
                                stop_reason = "error: a2a output cap"
                                failed = True
                                saw_terminal = True
                                error_text = _CAP_ERROR
                                break
                            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=delta)
                        if state == _TASK_STATE_AUTH_REQUIRED:
                            stop_reason = "error: a2a task auth_required"
                            failed = True
                            saw_terminal = True
                            if not error_text:
                                error_text = (
                                    "the remote agent asked this client for a credential "
                                    "mid-task; Kiro Crew never supplies one"
                                )
                            break
                        if state in _TERMINAL_STATES:
                            saw_terminal = True
                            if state in _FAILURE_STATES:
                                stop_reason = "error: a2a task " + state.lower()
                                failed = True
                                # On failure the status message text (if any) IS
                                # the error surface.
                                if delta and not error_text:
                                    error_text = delta
                            elif state in _CANCEL_STATES:
                                # Cancelled work is not completed work: the runner
                                # calls record_success on EVENT_COMPLETE, so this
                                # must surface as a failed turn with the cause named.
                                stop_reason = "error: a2a task cancelled"
                                failed = True
                                if not error_text:
                                    error_text = delta or "the remote agent cancelled the task"
                            break
        except asyncio.CancelledError:
            raise
        except A2AStreamError:
            raise
        except Exception as exc:  # pragma: no cover - network/protocol errors
            logger.warning("A2AProvider %s: stream failed: %s", self._name, exc)
            # RAISE, do not complete: run.py builds info.result from
            # EVENT_TEXT_CHUNKs only and reads EVENT_COMPLETE just for billing,
            # so an error reported via a completion event surfaces as a
            # successful run with "_No response._" (observed live: shim killed
            # mid-turn -> TransferEncodingError -> ✅ empty success). A raised
            # exception propagates through the transient-retry wrapper
            # (connection loss to a dead server is non-transient) into the
            # run's generic error arm — error tombstone, run marked failed.
            raise A2AStreamError(
                f"a2a stream failed mid-turn: {exc}"
                + (
                    f" [partial output before failure: {len(artifact_text) + len(final_text)} chars]"
                    if artifact_text or final_text
                    else ""
                )
            ) from exc

        if not saw_terminal:
            # The SSE stream ended cleanly but without ANY terminal task state:
            # the server closed the connection mid-turn. Same contract as the
            # exception path above — fail loudly, never complete.
            raise A2AStreamError(
                "a2a stream ended without a terminal task state — connection "
                "to the remote agent was lost mid-turn"
                + (
                    f" [partial output before failure: {len(artifact_text) + len(final_text)} chars]"
                    if artifact_text or final_text
                    else ""
                )
            )

        if failed:
            # FAILED / REJECTED is a failed run, not a completed one. The runner
            # reads EVENT_COMPLETE only for billing and calls record_success on
            # it, so completing here would show ✅ for a task the remote agent
            # refused or could not do. The remote's own reason (its terminal
            # status message, already streamed as a chunk when it arrived) rides
            # in the exception so the tombstone says why; partial output stays in
            # the transcript because every delta was yielded live.
            raise A2AStreamError(
                "a2a task "
                + stop_reason.removeprefix("error: a2a task ").removeprefix("error: a2a ")
                + ": "
                + (error_text or "the remote agent reported no reason")
                + (
                    f" [partial output before failure: {len(artifact_text) + len(final_text)} chars]"
                    if artifact_text or final_text
                    else ""
                )
            )

        # Everything the remote produced has been streamed as EVENT_TEXT_CHUNKs
        # (artifact deltas and status-message deltas alike, recaps deduplicated
        # above); the completion event carries the assembled text for the record,
        # artifact text first because the protocol makes artifacts the result.
        complete_text = artifact_text or final_text
        yield LLMEvent(kind=EVENT_COMPLETE, text=complete_text, stop_reason=stop_reason)

    def _make_timeout(self) -> Any:
        # sock_read: the gap between two reads. total=None so an actively streaming
        # long turn is never cut off by this client; the manager's wall clock is.
        return aiohttp.ClientTimeout(total=None, sock_read=_IDLE_READ_TIMEOUT_SECS)

    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> None:
        """No-op — a remote A2A agent runs its own tools behind its own gate;
        Kiro Crew never receives A2A permission requests to approve."""
        return None

    async def reject_tool(self, request_id: str | int) -> None:
        """No-op — see :meth:`approve_tool`."""
        return None

    def context_usage_pct(self) -> float:
        """Remote agent does not report context usage over A2A."""
        return 0.0

    def billing_stats(self) -> object | None:
        """Unmetered on this side, by construction.

        A remote A2A agent runs on its operator's account and meters itself; the
        protocol carries no per-turn cost and Kiro Crew is not the payer. Declared
        explicitly rather than inherited so the accounting path reads "no per-turn
        stats" as a statement about this backend, not an oversight
        (``test_background_turn_accounting`` requires every concrete provider to
        say which it is).
        """
        return None

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        """CancelTask on the in-flight A2A task, if any."""
        if not self._current_task_id or self._session is None:
            return "no_turn"
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "CancelTask",
            "params": {"taskId": self._current_task_id},
        }
        try:
            async with self._session.post(
                self._message_endpoint,
                json=payload,
                headers=self._headers({**A2A_VERSION_HEADER, "Content-Type": "application/json"}),
                allow_redirects=False,
                timeout=self._make_timeout(),
            ) as resp:
                resp.raise_for_status()
            return "acked"
        except Exception:  # pragma: no cover - best-effort cancel
            logger.debug("A2AProvider %s: cancel failed", self._name, exc_info=True)
            return "error"


async def _iter_sse_data(content: Any) -> AsyncIterator[str]:
    """Yield the JSON payload of each ``data:`` line in an SSE byte stream.

    A2A SSE frames are single-line ``data: {json}`` records separated by blank
    lines; this yields the ``{json}`` string for each.
    """
    async for line_bytes in content:
        line = line_bytes.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("data:"):
            yield line[len("data:") :].strip()
