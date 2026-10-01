"""Same-origin capability relay for a Remote Crew's dashboard pane.

``/instance-pane/{capability}/{tail}`` on the hub's own port relays HTTP and
WebSocket traffic to a connected peer crew's dashboard, over the SSH forward the
:class:`~kiro_crew.instances.ssh_tunnel_manager.SshTunnelManager` already holds.
The switcher frames this path instead of ``http://<published-host>:<loopback>/``,
so a remote pane rides the one published HTTPS origin that delivers the hub
itself — no wildcard host, no second port, no mixed content.

This is the backend half of the capability relay in
``.audit/pane-load-kc-46d84a/architecture-synthesis.md`` (candidate B base); the
frontend half (the relocatable dashboard runtime that resolves its URLs under
the capability prefix) is ``website/src/lib/dashboardRuntime.ts``. It is a close
sibling of :mod:`kiro_crew.dashboard.handlers.browser_view_relay`, and shares
that module's posture deliberately — a reader who knows one knows both.

Why a relay, and why opaque
---------------------------
The embedded SPA derives every request target from ``window.location`` and its
bundle is not rewritten, so a pane can only route its root-relative ``/api`` and
WebSocket traffic to a specific remote if the pane document lives under a path
that the hub can map back to that remote. The capability segment IS that map.
The frame is sandboxed WITHOUT ``allow-same-origin`` (an opaque origin), so
remote bytes served on the hub's network origin cannot read the hub DOM,
cookies, storage, or authenticated responses. The relay therefore cannot use the
hub's owner cookie (an opaque frame sends none) and must not want to: it
authenticates each request with the capability in the path.

Security posture (mirrors browser_view_relay, adapted to the tunnel)
--------------------------------------------------------------------
* **No SSRF surface.** The upstream is always the loopback end of the manager's
  already-open forward. Neither the host, the port, nor a redirect can be named
  by the request — the manager resolves the target from the instance id alone,
  and never follows an upstream 30x.
* **Capability-authenticated, authentication first.** A missing, malformed,
  expired, unknown, or stale capability answers one uniform ``404`` before the
  manager, the peer, the request body, or any error detail is touched. Only a
  holder of a live capability reaches the forward.
* **Generation-bound.** A lease is pinned to the forward generation live when it
  was issued. Disconnect, rebuild, removal, manager close, or tunnel replacement
  each advance that generation, so the lease is dead on the next request with no
  explicit revoke needing to run — the check is fail-closed by construction.
  Expiry is a monotonic deadline, immune to wall-clock changes.
* **No credential or ambient identity crosses the tunnel.** The browser's
  cookies, ``Authorization``, ``Origin``, ``Referer``, and every forwarding
  header are stripped; only an allow-list of safe transfer request headers
  (``Range``, the conditionals, ``Accept``) plus the narrow application allow-list
  (``X-Session-Key`` — the SPA's session-slot identity, not a credential) is
  forwarded, and the manager adds its own port-scoped session cookie. The peer's
  ``Set-Cookie`` and ``Clear-Site-Data`` never reach the hub origin.
* **Isolated even off-panel.** Every relayed response except script types is
  stamped ``Content-Security-Policy: sandbox …`` (opaque origin) plus
  ``X-Content-Type-Options: nosniff``, so a copied relay URL opened as a full tab
  is still opaque, whatever content type a compromised peer answers with.
* **Uniform refusal, audited; capability kept out of logs.** The wire answer is
  uniform; a SEL audit record carries only a fixed reason (never the capability,
  channel, request path, query, or body — see :func:`_audit`). The capability
  also travels in the request PATH, which a SEPARATE sink — aiohttp's access log
  — records by default, so :func:`install_access_log_redaction` masks it there at
  the server boundary. Evidence, not an absolute: the relay's own audit omits it
  by construction, and the access-log filter redacts ``/instance-pane/<cap>`` to
  ``/instance-pane/<redacted>``. A log configured outside these two boundaries is
  outside this guarantee.
* **Bounded.** Request bodies are size-capped before buffering; passthrough
  responses stream in chunks and are never fully held; the ONE buffered response
  — the ``text/html`` entry document, which must be read whole to rewrite — is
  itself size-capped before buffering and answers a fixed error past the cap;
  WebSocket messages are size-capped both legs; connect/idle timeouts are the
  manager's; and live leases are bounded per instance and globally.

What it deliberately does NOT do
--------------------------------
It does not rewrite the built JavaScript bundle. The remote build is relocatable
(Vite emits relative chunk/asset references; the runtime resolves its own base
from the capability path), so the only HTML mutation is re-rooting the entry
document's root-absolute ``src``/``href`` markers under the prefix plus injecting
the pre-module storage bootstrap. It inserts NO ``<base>`` element — that would
retarget in-document SVG ``url(#id)`` fragments and ``#hash`` anchors, which the
build contract forbids (see :func:`_rewrite_entry_html`). It does not proxy
anything but a live, generation-current lease. It does not accept an arbitrary
host, port, or URL from the request.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
import time
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: The pane-relay wire protocol the hub speaks. A remote build advertises the
#: same integer from ``/api/status`` (``pane_relay_protocol``); the issuer fails
#: closed with ``remote_upgrade_required`` when a published hub needs the relay
#: and the remote does not advertise this exact version. One authoritative source
#: for the number, imported by both the issuer and the status emitter.
PANE_RELAY_PROTOCOL: Literal[1] = 1

#: The dashboard-origin path the switcher frames, with the capability appended:
#: ``/instance-pane/<capability>/``. No trailing slash.
ROUTE_PREFIX = "/instance-pane"

#: Default lease lifetime. Long enough to cover a pane's load and settle; the
#: frontend re-issues before this elapses. Bounded so an abandoned capability
#: cannot outlive its usefulness even if every explicit revoke path is missed.
DEFAULT_LEASE_TTL_SECONDS = 900

#: Live-lease bounds. Per-instance keeps a reconnect storm from accreting leases
#: for one crew; global keeps the whole table bounded regardless of crew count.
_MAX_LEASES_PER_INSTANCE = 8
_MAX_LEASES_GLOBAL = 256

#: Upper bound on a buffered relayed request body. The hub must not hold
#: unbounded bytes for either side; larger uploads are refused, not streamed
#: unbounded. Mirrors the chat proxy's inbound cap.
_REQUEST_BODY_MAX_BYTES = 32 * 1024 * 1024

#: Upper bound for one proxied WebSocket message, both legs.
_WS_MAX_MSG_BYTES = 32 * 1024 * 1024

#: Upper bound on the entry document the relay BUFFERS to rewrite. Unlike every
#: other response — which streams through :meth:`_relay_stream` in bounded chunks
#: and is never fully held — the ``text/html`` entry document must be read whole
#: to re-root its markers and inject the pre-module bootstrap. A hostile or broken
#: peer answering ``text/html`` with an unbounded body would otherwise let the hub
#: buffer without limit, so the read stops at this cap and the relay answers a
#: fixed error instead of holding the bytes. A real dashboard index is tens of KiB;
#: 8 MiB is generous headroom while still bounded.
_ENTRY_HTML_MAX_BYTES = 8 * 1024 * 1024

#: Streaming chunk size for passthrough response bodies.
_CHUNK_BYTES = 64 * 1024

#: Methods the relay forwards. A valid capability holder using any other method
#: gets a ``405`` (it has already authenticated; the uniform 404 is only for the
#: capability gate). ``HEAD``/``OPTIONS`` ride through so conditional GETs and
#: CORS preflights behave.
_ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

#: Transfer/representation request headers forwarded upstream. Allow-list, not
#: block-list: everything the browser sends that is not here (or in
#: :data:`_FORWARD_APPLICATION_HEADERS`) is dropped, so ``Cookie``,
#: ``Authorization``, ``Origin``, ``Referer``, ``Host``, and every
#: ``Forwarded``/``X-Forwarded-*`` header can never cross the tunnel. The manager
#: adds its own session cookie; nothing of the browser's ambient identity does.
_FORWARD_REQUEST_HEADERS = (
    "Accept",
    "Accept-Language",
    "Range",
    "If-None-Match",
    "If-Modified-Since",
    "If-Range",
    "If-Match",
    "If-Unmodified-Since",
)

#: Application (not transfer) request headers forwarded upstream, kept as a
#: SEPARATE, deliberately narrow allow-list so a new safe transfer header and a
#: new application header are never added by the same edit. ``X-Session-Key`` is
#: the dashboard SPA's own session-identity header (``website/src/api/client.ts``
#: attaches ``dashboard:ui`` or the active slot key to every request): the peer
#: gateway selects a slot, enforces temporary/incognito restrictions, attributes
#: artifact activity, and REFUSES a request that carries no session identity from
#: it, so a direct-loopback pane sends it and the relay must too or session-scoped
#: behavior and its fail-closed restrictions silently diverge. It is NOT a
#: credential — it names which of the owner's own slots to act in, never grants
#: access (the capability in the path is the only access gate) — so forwarding it
#: leaks no ambient identity: ``Cookie``, ``Authorization``, ``Origin``,
#: ``Referer``, and the forwarding headers stay stripped, and the manager's
#: :data:`~kiro_crew.instances.ssh_tunnel_manager._RELAY_FORWARD_HEADER_DENY`
#: floor still refuses any of those even if a future caller regresses.
_FORWARD_APPLICATION_HEADERS = ("X-Session-Key",)

#: Response headers copied back downstream on STREAMED bodies. Forwarding
#: ``Range`` upstream while dropping ``Content-Range`` would relay a 206 whose
#: partial body the browser must discard; dropping the validators makes the
#: forwarded conditionals dead weight. ``Set-Cookie``, ``Clear-Site-Data``,
#: ``Content-Encoding`` (bodies arrive already decompressed), ``Content-Length``
#: (recomputed by the stream), and every hop-by-hop header are NOT here and so
#: never reach the hub origin.
#:
#: ``Cache-Control`` is DELIBERATELY NOT forwarded: a peer answering with a long
#: private lifetime (e.g. an authenticated artifact's ``private, max-age=31536000,
#: immutable``) would otherwise stay fresh in the browser cache long after the
#: 15-minute capability expires or its tunnel generation is revoked, and the
#: network gate never sees a cache hit. :func:`_stamp_relay_headers` overrides
#: every relayed response with ``no-store`` instead, so the browser cache can
#: never outlive the lease. The peer's validators (``ETag``/``Last-Modified``)
#: still ride through for conditional requests, which re-enter the relay and are
#: re-gated each time.
_FORWARD_RESPONSE_HEADERS = (
    "Content-Range",
    "Accept-Ranges",
    "ETag",
    "Last-Modified",
    "Content-Disposition",
    "Vary",
)

#: Stamped on every relayed response except script types (see
#: :func:`_stamp_relay_headers`). The CSP ``sandbox`` directive forces an opaque
#: origin at the SERVER, so the isolation holds even when the relay URL is opened
#: as a full tab rather than inside the panel's sandboxed iframe. The keyword set
#: mirrors the pane iframe's ``sandbox`` attribute minus ``allow-same-origin``.
_DOCUMENT_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"

#: Content types whose bodies are rewritten (the entry document) rather than
#: streamed. Only ``text/html`` — never JavaScript, which is not rewritten.
_REWRITE_HTML = "text/html"

#: Script content types EXEMPT from the CSP ``sandbox`` stamp: a worker created
#: from a relayed script URL is governed by the CSP on the SCRIPT response, and a
#: script type never renders as a document on navigation, so the exemption
#: reopens nothing while letting the pane's own workers run.
_SCRIPT_TYPES = frozenset(
    (
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "text/ecmascript",
    )
)


class PaneRelayPeer(Protocol):
    """The narrow slice of :class:`SshTunnelManager` the relay depends on.

    Declared as a Protocol so the relay is testable against a fake that forwards
    to a real loopback stub, WITHOUT the relay ever reaching around the manager
    to name a host or port. The real manager satisfies it; nothing test-only is
    exported into production to make the relay work.
    """

    def peer_forward_snapshot(self, instance_id: str) -> tuple[int, int]: ...

    def peer_forward_current(self, instance_id: str, snapshot: tuple[int, int]) -> bool: ...

    def proxy_request(
        self,
        instance_id: str,
        method: str,
        path: str,
        *,
        data: bytes | None = ...,
        content_type: str = ...,
        extra_headers: "Mapping[str, str] | None" = ...,
        expected_forward: "tuple[int, int] | None" = ...,
    ) -> "AbstractAsyncContextManager[aiohttp.ClientResponse]": ...

    def proxy_websocket(
        self,
        instance_id: str,
        path: str,
        *,
        subprotocols: tuple[str, ...] = ...,
        max_msg_size: int = ...,
        expected_forward: "tuple[int, int] | None" = ...,
    ) -> "AbstractAsyncContextManager[aiohttp.ClientWebSocketResponse]": ...


@dataclass(frozen=True, slots=True)
class PaneGrant:
    """The wire response the owner-authenticated issuer returns to the browser.

    Carries no port and no remote token — only the capability path, the frame
    channel (the frontend's postMessage attribution value), the protocol, and a
    wall-clock expiry for the UI. The capability appears exactly once, here.
    """

    kind: Literal["same-origin-relay"]
    document_path: str
    channel: str
    protocol: Literal[1]
    lease_expires_at_epoch_ms: int


@dataclass(frozen=True, slots=True)
class _PaneLease:
    """The private, memory-only lease. Never serialized, never logged.

    Stores DIGESTS of the capability and channel, not the secrets themselves, so
    a memory disclosure yields no usable credential. ``forward_snapshot`` is the
    ``(port, generation)`` the lease was issued against; a request is served only
    while it still names the live forward. ``expires_at_monotonic`` is a
    monotonic deadline, so a wall-clock change cannot extend a lease.
    """

    capability_digest: bytes
    instance_id: str
    forward_snapshot: tuple[int, int]
    channel_digest: bytes
    expires_at_monotonic: float


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


#: Matches a capability segment immediately after the relay prefix anywhere in a
#: formatted log line, capturing the prefix so only the secret is masked and the
#: trailing separator (``/``, ``?``, whitespace, end) is preserved. The
#: capability alphabet is ``secrets.token_urlsafe`` (``A-Za-z0-9_-``).
_ACCESS_LOG_CAPABILITY_RE = re.compile(rf"({re.escape(ROUTE_PREFIX)}/)[A-Za-z0-9_-]+")


def redact_capability_in_log(message: str) -> str:
    """Mask any relay capability in a log line as ``/instance-pane/<redacted>``.

    The capability is a bearer secret carried in the request PATH, so any log
    that records a request line — notably aiohttp's default access log, whose
    ``%r`` atom is ``"<method> <path+query> HTTP/x.y"`` — would otherwise persist
    a live capability an attacker with log read could replay. This masks it
    wherever it appears while leaving the rest of the line (method, status,
    timing) intact. Pure, so it is unit-testable without a logger.
    """
    return _ACCESS_LOG_CAPABILITY_RE.sub(r"\1<redacted>", message)


class CapabilityRedactingLogFilter(logging.Filter):
    """Redacts relay capabilities from every record on the logger it is attached
    to. Installed on aiohttp's access logger at the server boundary (see
    :func:`install_access_log_redaction`) so the request path cannot persist a
    live capability even when access logging is enabled by the production runner.

    It filters CONTENT, not records: it mutates the record in place and always
    returns ``True``. A logger-level filter runs before propagation, so every
    downstream handler (including the root's) sees the already-redacted message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = redact_capability_in_log(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:  # pragma: no cover - redaction must never break logging
            pass
        return True


def install_access_log_redaction(logger: logging.Logger | None = None) -> None:
    """Install the capability redactor on the aiohttp access logger (idempotent).

    Called at the dashboard server boundary. If *logger* is omitted the target is
    ``aiohttp.log.access_logger`` — the logger aiohttp's ``AccessLogger`` writes
    to. Idempotent: a second call does not stack a duplicate filter.
    """
    import aiohttp.log

    target = logger if logger is not None else aiohttp.log.access_logger
    if not any(isinstance(f, CapabilityRedactingLogFilter) for f in target.filters):
        target.addFilter(CapabilityRedactingLogFilter())


def _audit(outcome: str, reason: str) -> None:
    """SEL-audit one relay decision. Never the capability, channel, path, or query.

    Mirrors the sibling capability handlers (``browser_view_relay``,
    ``sandbox_doc``). ``reason`` is a fixed enum plus at most an instance id —
    never a credential and never the request line, both of which embed the
    capability here.
    """
    try:
        sel().log_api_access(
            caller="dashboard:instance-pane-relay",
            operation="instance_pane_relay.serve",
            outcome=outcome,
            source="dashboard",
            resources=reason[:256],
        )
    except Exception:  # pragma: no cover - auditing must never break serving
        logger.debug("instance-pane-relay: audit failed", exc_info=True)


def _split_capability(request: web.Request) -> tuple[str | None, str]:
    """``(capability, upstream path+query)`` from the raw request path.

    Derived from ``raw_path`` (never the decoded ``match_info``) so what the
    browser encoded stays encoded on the wire. The first segment after the
    prefix is the capability (``token_urlsafe`` alphabet, never percent-encoded);
    everything after it is passed upstream verbatim. A suffix can never steer the
    HOST: the manager joins it onto a fixed ``http://127.0.0.1:<port>`` authority
    where a leading ``//`` is still just a path.
    """
    raw = request.raw_path
    rest = raw[len(ROUTE_PREFIX) :] if raw.startswith(ROUTE_PREFIX) else raw
    if not rest.startswith("/"):
        return None, "/"  # bare `/instance-pane` (or `?query`): no capability
    rest = rest[1:]
    capability, sep, tail = rest, "", ""
    for index, char in enumerate(rest):
        if char in "/?":
            capability, sep, tail = rest[:index], char, rest[index + 1 :]
            break
    if sep == "?":
        suffix = f"/?{tail}"
    elif sep == "/":
        suffix = "/" + tail
    else:
        suffix = "/"
    return (capability or None), suffix


def _rewrite_location(location: str, prefix: str) -> str | None:
    """Re-root a same-peer redirect under *prefix*, or ``None`` to fail closed.

    Only a path-only ``Location`` is honored: the upstream is the peer's own
    dashboard and only ever redirects within itself, so a ``Location`` carrying a
    scheme or an authority — including the protocol-relative ``//host`` form,
    which ``urlsplit`` reads as a netloc — is refused rather than sent off-origin.
    """
    parts = urlsplit(location)
    if parts.scheme or parts.netloc:
        return None
    path = parts.path if parts.path.startswith("/") else "/" + parts.path
    rewritten = f"{prefix}{path}"
    return f"{rewritten}?{parts.query}" if parts.query else rewritten


#: The pre-module relay-pane bootstrap, injected inline into the entry document
#: (see :func:`_rewrite_entry_html`). It MUST run before the deferred app module
#: graph evaluates, because a relay pane is a sandboxed, opaque-origin iframe
#: where ``window.localStorage``/``window.sessionStorage`` throw ``SecurityError``
#: on *access*, and the SPA reads storage during module evaluation (i18n, ui
#: prefs). A classic (non-module) inline ``<script>`` runs during parse, before
#: any ``<script type="module">`` (which is always deferred), so the shims are in
#: place before the first imported module touches Storage.
#:
#: This is a hand-mirrored, self-contained port of the authoritative TypeScript
#: bootstrap in ``website/src/lib/relayPaneBootstrap.ts`` +
#: ``website/src/lib/relayStorage.ts`` — the app bundle's own copy runs too late
#: (it is a deferred module), so it cannot be reused here. The two must agree on
#: the envelope tag/version, ``PANE_CHANNEL_FIELD`` (``mcPaneChannel``), the
#: ``mc-relay-storage``/``mc-relay-storage-update`` message types, and the caps;
#: ``test_instance_pane_relay`` asserts those shared tokens are present so a
#: divergence fails a test rather than silently drifting.
#:
#: Inert in a direct (non-relay) load: this script is only ever injected by the
#: relay path (the direct dashboard index bytes are never rewritten), so its mere
#: presence means "this is a relay document" — a subsequent-document load where
#: ``window.name`` is empty is a RELAY navigation, not direct mode, and is handled
#: by the parent handshake below rather than returned from.
#:
#: One protocol, one gate, document-bound authority. The entry app module is
#: served INERT — its ``type="module"`` is rewritten to
#: :data:`_RELAY_GATED_SCRIPT_TYPE` by :func:`_rewrite_entry_html` so the browser
#: never auto-runs it — and this bootstrap RELEASES it (re-inserts a real
#: ``type="module"`` element in document order) only once an AUTHENTICATED port
#: handshake has delivered the channel + storage bank AND the document has parsed.
#: Holding a deferred module in HTML this way lets an async step finish before the
#: module graph evaluates: a not-yet-parsed module tag cannot be neutralised
#: synchronously, and a deferred module otherwise runs before any JS hook after
#: parsing.
#:
#: Every document — the first load and every subsequent navigation — follows the
#: same steps:
#:   1. Install storage shims. On a FIRST load ``window.name`` may carry a
#:      storage snapshot for a warm first paint; it is consumed as a pre-seed and
#:      ``window.name`` is cleared immediately, so nothing lingers in
#:      browser-visible state. It is only a storage accelerator — never the
#:      channel authority.
#:   2. Derive the capability prefix from ``location.pathname`` and hand the
#:      parent one port of a fresh ``MessageChannel`` in
#:      ``mc-relay-bootstrap-request`` (with the documentPath + a per-document
#:      nonce). The parent validates the exact frame + the documentPath it issued
#:      (never the opaque origin — see :func:`resolvePaneBootstrapRequest` in
#:      ``paneChannel.ts``), adopts the transferred port, and replies OVER it with
#:      the channel + the authoritative bank snapshot.
#:   3. On the port reply, adopt the channel/origin, reseed the shims from the
#:      bank, publish ``__kcRelayPaneContext``, bridge the port's downward
#:      messages into this document's window listeners, and release the module —
#:      exactly once.
#:
#: The port is entangled with THIS document and is neutered the instant the
#: document is replaced, so a successor document in the same iframe — which never
#: authenticated and holds no port — receives neither the channel nor any downward
#: state. A handshake that never completes leaves the module gated (fail closed);
#: the parent's readiness watchdog surfaces its Retry panel rather than the app
#: booting with an empty channel and the storage reload loop that would follow.
#: An off-capability or malformed pathname likewise never releases.
#:
#: Mirrors ``website/src/lib/relayPaneBootstrap.ts`` + ``relayStorage.ts``; the two
#: must agree on the envelope tag/version, ``mcPaneChannel``, the ``mc-relay-*``
#: message types (storage + bootstrap), the ``MessageChannel`` port handshake, the
#: caps, and the sentinel gate type — ``test_instance_pane_relay`` asserts those
#: shared tokens are present.
_RELAY_GATED_SCRIPT_TYPE = "application/kc-relay-gated"
_RELAY_PANE_BOOTSTRAP_SCRIPT = (
    "<script>/* relay pane pre-module bootstrap; mirrors "
    "website/src/lib/relayPaneBootstrap.ts */(function(w,d){"
    # Gate release: re-insert every inert app module in document order once the
    # authenticated port handshake has SUCCEEDED and the document has parsed.
    # Idempotent — release strips the sentinel type from each script, and the
    # ``released`` latch makes a second call a no-op. There is NO empty-shim
    # release: the module runs only after ``finalize`` installs the real channel,
    # parent origin, and storage bank. A handshake that never completes leaves the
    # module gated, and the parent's readiness watchdog surfaces its Retry panel —
    # a blank-but-recoverable pane, never one that looks initialized with no
    # channel, no persistence, and the reload loop that follows from empty storage.
    "var shimsReady=false,domReady=(d.readyState!=='loading'),released=false;"
    "function releaseNow(){if(released)return;released=true;var list=d.querySelectorAll("
    "'script[type=\"application/kc-relay-gated\"]');"
    "for(var i=0;i<list.length;i++){var old=list[i];var s=d.createElement('script');"
    "for(var a=0;a<old.attributes.length;a++){var at=old.attributes[a];"
    "if(at.name==='type')continue;s.setAttribute(at.name,at.value);}"
    "s.type='module';s.async=false;"
    "if(!old.getAttribute('src')&&old.textContent)s.textContent=old.textContent;"
    "if(old.parentNode)old.parentNode.replaceChild(s,old);}}"
    "function tryRelease(){if(shimsReady&&domReady)releaseNow();}"
    "if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',"
    "function(){domReady=true;tryRelease();});"
    # The authoritative channel + parent origin the shims report under and the
    # window downstream-listener validates by. Empty until the port reply fills
    # it in ``finalize``; no app module runs before then, so the sink and the
    # channel check are never exercised while it is empty.
    "var CTX={channel:'',parentOrigin:'',protocol:1};"
    "try{"
    # Bounded storage shim factory (mirrors relayStorage.ts). ``reseed`` replaces
    # the whole map from an authoritative parent snapshot without echoing up —
    # the port reply's bank supersedes any pre-seed.
    "var CAPS={maxKeys:200,maxKeyBytes:512,maxValueBytes:262144,maxTotalBytes:2097152};"
    "var enc=new TextEncoder();function b(s){return enc.encode(s).length;}"
    "function over(map,key,value){if(b(key)>CAPS.maxKeyBytes)return true;"
    "if(b(value)>CAPS.maxValueBytes)return true;"
    "if(!map.has(key)&&map.size>=CAPS.maxKeys)return true;var total=0;"
    "map.forEach(function(v,k){if(k!==key)total+=b(k)+b(v);});"
    "return total+b(key)+b(value)>CAPS.maxTotalBytes;}"
    'function seed(snap){var map=new Map();if(snap&&typeof snap==="object"){'
    "Object.keys(snap).forEach(function(k){var v=String(snap[k]);"
    "if(!over(map,k,v))map.set(k,v);});}return map;}"
    "function store(area){var map=seed(null);"
    "function report(m){try{w.parent.postMessage("
    '{type:"mc-relay-storage",area:area,mutation:m,mcPaneChannel:CTX.channel},CTX.parentOrigin);}catch(e){}}'
    "function setLocal(key,value,doReport){var v=String(value);"
    "if(over(map,key,v)){if(doReport)throw new DOMException("
    '"relay storage quota exceeded","QuotaExceededError");return;}'
    'map.set(key,v);if(doReport)report({op:"set",key:key,value:v});}'
    "return{get length(){return map.size;},"
    'key:function(i){if(typeof i!=="number"||i<0||(i|0)!==i)return null;'
    "var n=0,out=null;map.forEach(function(_v,k){if(n===i)out=k;n++;});return out;},"
    "getItem:function(k){return map.has(k)?map.get(k):null;},"
    "setItem:function(k,v){setLocal(String(k),String(v),true);},"
    'removeItem:function(k){var kk=String(k);if(map.delete(kk))report({op:"remove",key:kk});},'
    'clear:function(){map.clear();report({op:"clear"});},'
    "reseed:function(snap){map=seed(snap);},"
    'applyDownstream:function(m){if(m.op==="set")setLocal(m.key,m.value,false);'
    'else if(m.op==="remove")map.delete(m.key);else if(m.op==="clear")map.clear();}};}'
    "function def(key,value){try{Object.defineProperty(w,key,"
    "{configurable:true,enumerable:true,value:value});}catch(e){}}"
    "var stores=null;"
    # Install the storage shims + the downstream (parent->child) storage listener.
    # The listener reads CTX at message time and is fed by the port bridge below
    # (a parent that never wildcard-posts to this frame), so a document replacing
    # this one in the same iframe — which holds no port and gets no CTX — receives
    # nothing here.
    "function installShims(){stores={local:store('local'),session:store('session')};"
    'def("localStorage",stores.local);def("sessionStorage",stores.session);'
    'w.addEventListener("message",function(ev){if(ev.source!==w.parent)return;'
    'var m=ev.data;if(!m||typeof m!=="object"||m.type!=="mc-relay-storage-update")return;'
    "if(m.mcPaneChannel!==CTX.channel)return;var s=stores[m.area];"
    'if(s&&m.mutation&&typeof m.mutation==="object")s.applyDownstream(m.mutation);});}'
    # Complete the handshake exactly once: adopt the authoritative channel/origin,
    # reseed the shims from the parent bank, publish the pane context the app's
    # upward-messaging layer reads, and bridge the AUTHENTICATED port's downward
    # messages into this document's own window listeners (source=w.parent) so the
    # existing host-model/ack/cursor handlers work unchanged — then release the
    # gated module. The port is bound to THIS document and neutered when it is
    # replaced, so nothing downward can reach a successor document.
    "function finalize(channel,parentOrigin,protocol,storage,port){if(released)return;"
    "CTX.channel=channel;CTX.parentOrigin=parentOrigin;CTX.protocol=protocol;"
    "if(!stores)installShims();"
    "if(storage&&typeof storage==='object'){stores.local.reseed(storage.local);"
    "stores.session.reseed(storage.session);}"
    "w.__kcRelayPaneContext={channel:channel,parentOrigin:parentOrigin,protocol:protocol};"
    "port.onmessage=function(ev){try{w.dispatchEvent(new MessageEvent('message',"
    "{data:ev.data,source:w.parent,origin:parentOrigin}));}catch(e){}};"
    "try{port.start();}catch(e){}"
    "shimsReady=true;tryRelease();}"
    # One protocol for every document (first load and every subsequent
    # navigation): install shims, optionally PRE-SEED storage synchronously from a
    # first-load window.name envelope (for a warm first paint), clear window.name
    # immediately so the seed never lingers, then run the port handshake. The
    # channel and the authoritative storage bank always arrive over the port, so
    # window.name is only ever a storage accelerator, never the channel authority.
    "installShims();"
    "var preSnap=null,nm=w.name;"
    "if(nm){try{var p=JSON.parse(nm);if(p&&typeof p==='object'&&p.__kcRelayPane===1"
    "&&p.v===1&&p.storage&&typeof p.storage==='object')preSnap=p.storage;}catch(e){}}"
    'try{w.name="";}catch(e){}'
    "if(preSnap){stores.local.reseed(preSnap.local);stores.session.reseed(preSnap.session);}"
    "var mm=/^(\\/instance-pane\\/[^/?#]+\\/)/.exec(w.location.pathname||'');"
    # Off-capability or malformed pathname: fail CLOSED. Never release the app
    # with an empty channel — an unreachable/foreign document must not look
    # initialized. The parent's readiness watchdog owns recovery (its Retry panel).
    "if(!mm){return;}"
    "var docPath=mm[1];var settled=false,elapsed=0,timer=null;"
    "function mkNonce(){var s;try{var arr=new Uint8Array(16);"
    "(w.crypto||w.msCrypto).getRandomValues(arr);"
    "s='';for(var i=0;i<arr.length;i++)s+=('0'+arr[i].toString(16)).slice(-2);}"
    "catch(e){s=String(Math.random())+'.'+String(Date.now());}return s;}"
    # ONE MessageChannel is retained for the document's life. The child keeps
    # port1 (its downstream inbox) and TRANSFERS port2 to the parent with the
    # capability documentPath + a per-document nonce. The parent validates the
    # exact frame + issued documentPath, adopts the port, and replies OVER it —
    # and holds it for every later downward message. Only this document holds the
    # entangled peer, so the reply and all downward state are inaccessible to a
    # replacement document in the same iframe.
    "var mc=new MessageChannel();var nonce=mkNonce();"
    "mc.port1.onmessage=function(ev){if(settled)return;var r=ev.data;"
    "if(!r||typeof r!=='object'||r.type!=='mc-relay-bootstrap-reply')return;"
    "if(r.nonce!==nonce)return;if(typeof r.channel!=='string'||!r.channel)return;"
    "if(typeof r.parentOrigin!=='string'||!r.parentOrigin)return;"
    "if(typeof r.protocol!=='number')return;settled=true;if(timer)clearTimeout(timer);"
    "finalize(r.channel,r.parentOrigin,r.protocol,"
    "(r.storage&&typeof r.storage==='object')?r.storage:null,mc.port1);};"
    "try{mc.port1.start();}catch(e){}"
    # The FIRST ask transfers the port. Bounded retries (~15s) re-ask WITHOUT a
    # port — it cannot be transferred twice — so a parent slow to reply re-replies
    # on the port it already holds (no port thrash, and the reply always lands on
    # the one inbox the child is listening on). On terminal failure the module
    # stays gated (fail closed): no empty release, no reload loop — the parent's
    # readiness watchdog owns recovery.
    "try{w.parent.postMessage({type:'mc-relay-bootstrap-request',documentPath:docPath,"
    "nonce:nonce,v:1},'*',[mc.port2]);}catch(e){}"
    "(function tick(){timer=setTimeout(function(){if(settled)return;elapsed+=2000;"
    "try{w.parent.postMessage({type:'mc-relay-bootstrap-request',documentPath:docPath,"
    "nonce:nonce,v:1},'*');}catch(e){}if(elapsed<15000)tick();},2000);})();"
    "}catch(e){/* fail closed: never release the app on a bootstrap error */}"
    "})(window,document);</script>"
)


def _rewrite_entry_html(text: str, prefix: str) -> str:
    """Make the entry document relocatable under *prefix* and boot the relay pane.

    Re-root the entry document's root-absolute ``src``/``href`` markers under the
    capability path and inject the pre-module :data:`_RELAY_PANE_BOOTSTRAP_SCRIPT`
    so the opaque-origin pane's Storage shims install before any deferred app
    module reads storage. The built JavaScript is NOT touched — only the HTML
    markers and this one injected classic ``<script>``.

    NO ``<base>`` element is inserted, deliberately. The build contract
    (``website/scripts/check-build-output.mjs`` and its
    ``buildOutputRelocatable.test.ts``) forbids a ``<base>`` in the emitted
    ``index.html`` precisely because it retargets in-document SVG ``url(#id)``
    fragment references and ``#hash`` anchors against the base URL instead of the
    current document URL — silently breaking gradients/masks/clip-paths and
    same-page links after client-side navigation. The relocatable build emits its
    entry ``<script type="module">`` and ``modulepreload`` links as root-absolute
    ``/assets/…`` markers (rewritten here to ``{prefix}/assets/…``) and every
    chunk-to-chunk import, dynamic import, worker ``new URL`` and CSS ``url()`` as
    a RELATIVE reference that self-locates against the document/asset URL. So the
    prefix rewrite of the entry markers is sufficient on its own; a ``<base>``
    would be both redundant and harmful.
    """
    # Re-root the entry document's root-absolute references under the prefix. The
    # relocatable build emits only the entry-script/modulepreload markers as
    # root-absolute ``/assets/…`` (chunk-to-chunk refs are relative), so this is
    # the single transform that carries the entry graph under the capability
    # path. The injected bootstrap carries no ``src``/``href`` and is unaffected.
    for attr in ("src", "href"):
        text = text.replace(f'{attr}="/', f'{attr}="{prefix}/')
    # Inert the entry app module so the browser does NOT auto-run it: the injected
    # bootstrap re-inserts a real ``type="module"`` element only once the
    # authenticated port handshake has delivered the channel + storage bank. This
    # HTML-level gate is what holds a deferred module until that async step
    # completes — see :data:`_RELAY_PANE_BOOTSTRAP_SCRIPT`. The relocatable build emits exactly one
    # ``type="module"`` (the entry); ``type="importmap"`` is a different token and
    # is left untouched so it still registers during parse, before the re-inserted
    # module resolves its bare specifiers against it.
    text = text.replace('type="module"', f'type="{_RELAY_GATED_SCRIPT_TYPE}"')
    # A classic (non-module) script that runs during parse, before the gated app
    # module could ever be released. Placed at the very top of <head> so it can
    # find and release every gated module in the document once shims are ready.
    injected = _RELAY_PANE_BOOTSTRAP_SCRIPT
    if "<head>" in text:
        return text.replace("<head>", f"<head>{injected}", 1)
    return injected + text


def _not_found() -> web.Response:
    """The uniform refusal: missing, malformed, expired, unknown, and stale match."""
    response = web.json_response({"error": "not found", "code": "not_found"}, status=404)
    # A refusal must never be cached: an expired/revoked capability answers 404,
    # and a cached 404 (or a cached earlier success replaced by one) would let the
    # browser skip the network gate. Uncacheable at the relay boundary.
    response.headers["Cache-Control"] = _RELAY_NO_STORE
    return response


#: The relay's uncacheable posture, stamped on every relayed response so a peer
#: cache lifetime can never outlive the capability lease or its tunnel
#: generation. ``no-store`` forbids the browser from writing ANY cache entry, so
#: there is no stored response to serve after expiry or generation revocation.
_RELAY_NO_STORE = "no-store"


def _stamp_relay_headers(response: web.StreamResponse, base_type: str) -> None:
    """The relay's own response headers: opaque, uncacheable, non-referring.

    ``Access-Control-Allow-Origin: null`` matches the opaque frame's ``Origin:
    null`` and is deliberately WITHOUT ``Access-Control-Allow-Credentials`` — the
    capability in the path gates access; CORS only gates cross-origin
    readability, and a capability holder already has access. The CSP ``sandbox``
    stamp goes on every response except script types (see :data:`_SCRIPT_TYPES`).

    ``Cache-Control: no-store`` is set UNCONDITIONALLY here — after any peer
    forward-header copy (see :func:`_relay_stream`) and set (not ``setdefault``)
    so it wins over both the peer's value and the outer middleware default. This
    is the enforcement point for the lease's freshness contract: no relayed
    response, authenticated or not, may be cached past its capability lease or
    tunnel generation. The peer's ``Cache-Control`` is additionally not forwarded
    at all (see :data:`_FORWARD_RESPONSE_HEADERS`), so this is belt-and-suspenders
    rather than a race against copy order.
    """
    response.headers["Access-Control-Allow-Origin"] = "null"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = _RELAY_NO_STORE
    if base_type not in _SCRIPT_TYPES:
        response.headers["Content-Security-Policy"] = _DOCUMENT_CSP


async def api_instance_pane_relay(request: web.Request) -> web.StreamResponse:
    """Route entry for ``* /instance-pane/{tail}`` — serve the live relay.

    Resolves the per-gateway :class:`InstancePaneRelay` from dashboard state at
    request time (it is created when the instances manager starts). When the
    relay is absent — instances disabled, manager not running — the answer is the
    SAME uniform 404 a bad capability gets, so a prober cannot tell "feature off"
    from "wrong capability" from "no such pane".
    """
    relay = getattr(request.app["state"], "instance_pane_relay", None)
    if relay is None:
        _audit("denied", "relay_unavailable")
        return _not_found()
    return await relay.serve(request)


class InstancePaneRelay:
    """Issue pane capabilities and serve the capability route.

    Two public operations callers need: :meth:`issue` (the owner-authenticated
    issuer calls it) and :meth:`serve` (the capability route binds it). Everything
    else — lease storage, expiry, generation binding, request filtering, redirect
    handling, HTML rewriting, CORS, CSP, streaming, WebSocket pumping, and audit
    redaction — is hidden behind them. This is a deep module, not a bag of route
    options.
    """

    def __init__(
        self,
        peer: PaneRelayPeer,
        *,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        max_leases_per_instance: int = _MAX_LEASES_PER_INSTANCE,
        max_leases_global: int = _MAX_LEASES_GLOBAL,
    ) -> None:
        self._peer = peer
        self._ttl_seconds = ttl_seconds
        self._max_per_instance = max_leases_per_instance
        self._max_global = max_leases_global
        #: capability_digest -> lease. Keyed by digest so the raw capability is
        #: never stored; a wrong capability simply misses.
        self._leases: dict[bytes, _PaneLease] = {}

    # ── issue ────────────────────────────────────────────────────────────────

    def issue(self, instance_id: str) -> PaneGrant | None:
        """Mint one capability bound to the live forward, or ``None`` if down.

        Captures the forward generation now, mints a 256-bit capability and an
        independent frame channel, stores only their digests, bounds the live
        set per-instance and globally (pruning expired first, then evicting the
        oldest), and returns the raw values ONCE. Two calls create two
        independent leases — issuing is retry-safe, not idempotent, so a lost
        response simply yields a fresh capability rather than resurrecting a
        stale one.
        """
        snapshot = self._peer.peer_forward_snapshot(instance_id)
        if snapshot[0] <= 0:  # not connected -> no forward to bind to
            return None

        now = time.monotonic()
        self._prune_expired(now)

        capability = secrets.token_urlsafe(32)  # 256 bits
        channel = secrets.token_urlsafe(16)
        lease = _PaneLease(
            capability_digest=_digest(capability),
            instance_id=instance_id,
            forward_snapshot=snapshot,
            channel_digest=_digest(channel),
            expires_at_monotonic=now + self._ttl_seconds,
        )
        self._enforce_bounds(instance_id)
        self._leases[lease.capability_digest] = lease

        return PaneGrant(
            kind="same-origin-relay",
            document_path=f"{ROUTE_PREFIX}/{capability}/",
            channel=channel,
            protocol=PANE_RELAY_PROTOCOL,
            lease_expires_at_epoch_ms=int((time.time() + self._ttl_seconds) * 1000),
        )

    def _prune_expired(self, now: float) -> None:
        dead = [d for d, lease in self._leases.items() if lease.expires_at_monotonic <= now]
        for digest in dead:
            self._leases.pop(digest, None)

    def _enforce_bounds(self, instance_id: str) -> None:
        """Evict the oldest lease(s) so per-instance and global caps hold.

        Insertion order in the dict is issue order, so the FIRST matching lease
        is the oldest. Eviction is silent: an evicted capability's next request
        simply misses and gets the uniform 404.
        """
        same = [d for d, lease in self._leases.items() if lease.instance_id == instance_id]
        while len(same) >= self._max_per_instance:
            self._leases.pop(same.pop(0), None)
        while len(self._leases) >= self._max_global:
            oldest = next(iter(self._leases))
            self._leases.pop(oldest, None)

    def revoke_instance(self, instance_id: str) -> None:
        """Drop every lease for *instance_id* (removal, explicit disconnect).

        Best-effort hygiene only: the serve-time generation check already makes a
        lease dead the moment its forward moves, so security does not depend on
        this running. It keeps the table from holding leases for a crew that is
        gone.
        """
        for digest in [d for d, lease in self._leases.items() if lease.instance_id == instance_id]:
            self._leases.pop(digest, None)

    async def close(self) -> None:
        """Drop all leases (shutdown hook). No sessions are owned here — every
        upstream session is created and closed inside the manager's
        ``proxy_request``/``proxy_websocket`` context."""
        self._leases.clear()

    # ── serve ──────────────────────────────────────────────────────────────

    def _resolve(self, request: web.Request) -> tuple[_PaneLease | None, str]:
        """Validate the presented capability. ``(lease, suffix)`` or ``(None, _)``.

        Every failure — missing, malformed, unknown, expired, or stale
        generation — returns ``None`` here, BEFORE the manager, the peer, the
        request body, or any error detail is touched, so all of them share the
        one uniform 404.
        """
        capability, suffix = _split_capability(request)
        if capability is None:
            _audit("denied", "no_capability")
            return None, suffix
        lease = self._leases.get(_digest(capability))
        if lease is None:
            _audit("denied", "unknown_capability")
            return None, suffix
        if lease.expires_at_monotonic <= time.monotonic():
            self._leases.pop(lease.capability_digest, None)
            _audit("denied", "expired_capability")
            return None, suffix
        if not self._peer.peer_forward_current(lease.instance_id, lease.forward_snapshot):
            # The forward moved since issue (disconnect/rebuild/removal/close/
            # tunnel replacement). Dead lease; drop it and refuse uniformly.
            self._leases.pop(lease.capability_digest, None)
            _audit("denied", "stale_generation")
            return None, suffix
        return lease, suffix

    async def serve(self, request: web.Request) -> web.StreamResponse:
        """Serve one ``/instance-pane/{capability}/{tail}`` request.

        Authentication first: :meth:`_resolve` answers the uniform 404 for any
        bad capability before the forward is consulted. A live-lease holder is
        forwarded — WebSocket if it is an upgrade, otherwise HTTP.
        """
        lease, suffix = self._resolve(request)
        if lease is None:
            return _not_found()

        if request.method not in _ALLOWED_METHODS:
            # A valid holder using an unsupported method: it has authenticated,
            # so a 405 (not the uniform 404) is safe and correct.
            return web.json_response(
                {"error": "method not allowed", "code": "pane_method_not_allowed"}, status=405
            )

        # CORS preflight, answered by the relay itself — never forwarded. The
        # pane is a SANDBOXED, opaque-origin document (no allow-same-origin), so
        # its own ``Origin`` is ``null`` and EVERY request it makes back to the
        # capability path is cross-origin (null -> hub). A non-simple request —
        # the SPA sends ``X-Session-Key`` and JSON bodies — triggers a preflight
        # ``OPTIONS`` that the peer dashboard does not answer with CORS, so
        # forwarding it would fail the preflight and block the real request.
        # A live-capability holder has already authenticated here (``_resolve``
        # ran), so answering the preflight leaks nothing the path did not already
        # authorize. Mirrors ``_stamp_relay_headers``' opaque-origin posture:
        # ``Access-Control-Allow-Origin: null`` and NO ``…-Allow-Credentials``
        # (the capability in the path gates access; the pane sends no ambient
        # credentials, and the relay adds the peer's own upstream).
        if request.method == "OPTIONS" and request.headers.get("Access-Control-Request-Method"):
            _audit("allowed", "cors_preflight")
            return self._cors_preflight(request)

        capability, _ = _split_capability(request)
        prefix = f"{ROUTE_PREFIX}/{capability}"

        # The lease's forward snapshot is carried into the manager attempt so
        # the generation is re-checked INSIDE the operation that selects the URL
        # and credential — not just here, where a rebuild between this resolve
        # and the manager's own capture could let an old capability ride the
        # replacement forward. See :meth:`SshTunnelManager.proxy_request`.
        if (
            request.method == "GET"
            and request.headers.get("Upgrade", "").lower() == "websocket"
            and "upgrade" in request.headers.get("Connection", "").lower()
        ):
            return await self._relay_ws(request, lease.instance_id, suffix, lease.forward_snapshot)
        return await self._relay_http(
            request, lease.instance_id, suffix, prefix, lease.forward_snapshot
        )

    @staticmethod
    def _cors_preflight(request: web.Request) -> web.Response:
        """A CORS preflight answer for the opaque pane: allow the real request.

        Echoes the requested method/headers within the relay's method set, keeps
        the opaque ``Access-Control-Allow-Origin: null`` (no credentials), and is
        cached briefly so the SPA's steady-state traffic is not preflighted on
        every call. No body, so no CSP ``sandbox`` stamp is needed.
        """
        requested_headers = request.headers.get("Access-Control-Request-Headers", "")
        response = web.Response(status=204)
        response.headers["Access-Control-Allow-Origin"] = "null"
        response.headers["Access-Control-Allow-Methods"] = ", ".join(sorted(_ALLOWED_METHODS))
        # Reflect exactly what the browser asked to send; the capability, not
        # CORS, is the access gate, so the header allow-list is not a control here.
        response.headers["Access-Control-Allow-Headers"] = requested_headers or "*"
        response.headers["Access-Control-Max-Age"] = "600"
        response.headers["Vary"] = "Origin, Access-Control-Request-Headers"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # The preflight DECISION is cached by ``Access-Control-Max-Age`` (a
        # separate CORS mechanism); ``no-store`` only forbids HTTP-caching the 204
        # itself, so it cannot linger past the lease as a stored response.
        response.headers["Cache-Control"] = _RELAY_NO_STORE
        return response

    # ── HTTP leg ─────────────────────────────────────────────────────────────

    async def _relay_http(
        self,
        request: web.Request,
        instance_id: str,
        suffix: str,
        prefix: str,
        expected_forward: tuple[int, int],
    ) -> web.StreamResponse:
        body = await self._read_bounded_body(request)
        if body is None:
            return web.json_response(
                {"error": "request body too large", "code": "pane_body_too_large"}, status=413
            )
        forward_headers = {
            name: request.headers[name]
            for name in (*_FORWARD_REQUEST_HEADERS, *_FORWARD_APPLICATION_HEADERS)
            if name in request.headers
        }
        try:
            async with self._peer.proxy_request(
                instance_id,
                request.method,
                suffix,
                data=body or None,
                content_type=request.headers.get("Content-Type", ""),
                extra_headers=forward_headers,
                expected_forward=expected_forward,
            ) as upstream:
                content_type = upstream.headers.get("Content-Type", "")
                base_type = content_type.split(";", 1)[0].strip().lower()

                if upstream.status in (301, 302, 303, 307, 308):
                    return self._relay_redirect(upstream, prefix)

                if upstream.status == 200 and base_type == _REWRITE_HTML:
                    return await self._relay_entry_document(upstream, prefix)

                return await self._relay_stream(request, upstream, content_type, base_type)
        except ProxyRequestError as e:
            _audit("failure", f"{instance_id}:{e.code}")
            status = 503 if e.http_status == 503 else 502
            return web.json_response({"error": e.message, "code": e.code}, status=status)

    async def _read_bounded_body(self, request: web.Request) -> bytes | None:
        """Buffer the request body under the cap, or ``None`` if it overflows."""
        if not request.body_exists:
            return b""
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.content.iter_chunked(_CHUNK_BYTES):
            received += len(chunk)
            if received > _REQUEST_BODY_MAX_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def _relay_redirect(self, upstream, prefix: str) -> web.Response:
        location = upstream.headers.get("Location", "/")
        rewritten = _rewrite_location(location, prefix)
        if rewritten is None:
            # Cross-authority or protocol-relative: fail closed, never off-origin.
            _audit("denied", "off_origin_redirect")
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)
        redirect = web.Response(status=upstream.status, headers={"Location": rewritten})
        _stamp_relay_headers(redirect, "")
        _audit("allowed", "redirect")
        return redirect

    async def _relay_entry_document(self, upstream, prefix: str) -> web.Response:
        raw = await self._read_bounded_entry_html(upstream)
        if raw is None:
            _audit("denied", "document_too_large")
            return web.json_response(
                {"error": "entry document too large", "code": "pane_document_too_large"},
                status=502,
            )
        # Decode with the charset the peer declared in Content-Type, falling back
        # to UTF-8. Read from the header (not ``get_encoding()``, which requires
        # the body to have been buffered by ``read()`` first and raises otherwise)
        # because the body was streamed under the cap rather than read whole.
        encoding = upstream.charset or "utf-8"
        text = raw.decode(encoding, errors="replace")
        document = web.Response(
            status=upstream.status,
            text=_rewrite_entry_html(text, prefix),
            content_type=_REWRITE_HTML,
            charset="utf-8",
        )
        # ``Cache-Control: no-store`` is set by ``_stamp_relay_headers`` below,
        # the single boundary override for every relayed response.
        _stamp_relay_headers(document, _REWRITE_HTML)
        _audit("allowed", "document")
        return document

    @staticmethod
    async def _read_bounded_entry_html(upstream) -> bytes | None:
        """Buffer the entry document under :data:`_ENTRY_HTML_MAX_BYTES`.

        Reads only up to the cap and returns ``None`` the moment the body would
        exceed it, so a peer answering ``text/html`` with an unbounded stream can
        never make the hub hold more than the cap. The bytes are read in chunks
        and never more than one chunk past the boundary is examined.
        """
        chunks: list[bytes] = []
        received = 0
        async for chunk in upstream.content.iter_chunked(_CHUNK_BYTES):
            received += len(chunk)
            if received > _ENTRY_HTML_MAX_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    async def _relay_stream(
        self, request: web.Request, upstream, content_type: str, base_type: str
    ) -> web.StreamResponse:
        response = web.StreamResponse(status=upstream.status)
        if content_type:
            response.headers["Content-Type"] = content_type
        for name in _FORWARD_RESPONSE_HEADERS:
            if name in upstream.headers:
                response.headers[name] = upstream.headers[name]
        _stamp_relay_headers(response, base_type)
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
        except ConnectionResetError:
            _audit("partial", "client_disconnected")
            return response
        await response.write_eof()
        _audit("allowed", "stream")
        return response

    # ── WebSocket leg ─────────────────────────────────────────────────────────

    async def _relay_ws(
        self, request: web.Request, instance_id: str, suffix: str, expected_forward: tuple[int, int]
    ) -> web.WebSocketResponse:
        """Pump one WebSocket bidirectionally between the browser and the peer.

        Both legs cap message size; ping/pong is autopiloted per leg (aiohttp's
        autoping), so only data and close frames cross. Either side closing tears
        the other down. The lease's ``expected_forward`` is carried into the
        manager handshake so the generation is re-checked INSIDE the attempt that
        selects the URL and credential — a rebuild between resolve and handshake
        refuses before any frame reaches the replacement peer.
        """
        requested = request.headers.get("Sec-WebSocket-Protocol", "")
        subprotocols = tuple(p.strip() for p in requested.split(",") if p.strip())
        downstream = web.WebSocketResponse(max_msg_size=_WS_MAX_MSG_BYTES, protocols=subprotocols)
        await downstream.prepare(request)
        try:
            async with self._peer.proxy_websocket(
                instance_id,
                suffix,
                subprotocols=subprotocols,
                max_msg_size=_WS_MAX_MSG_BYTES,
                expected_forward=expected_forward,
            ) as upstream:
                _audit("allowed", "websocket")
                await self._pump_ws(downstream, upstream)
        except ProxyRequestError as e:
            _audit("failure", f"{instance_id}:{e.code}")
            with contextlib.suppress(Exception):
                await downstream.close(code=aiohttp.WSCloseCode.TRY_AGAIN_LATER)
        return downstream

    @staticmethod
    async def _pump_ws(downstream: web.WebSocketResponse, upstream) -> None:
        async def _down() -> None:
            async for msg in upstream:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await downstream.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await downstream.send_bytes(msg.data)
                else:
                    break

        async def _up() -> None:
            async for msg in downstream:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await upstream.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await upstream.send_bytes(msg.data)
                else:
                    break

        down = asyncio.create_task(_down())
        up = asyncio.create_task(_up())
        try:
            _done, pending = await asyncio.wait({down, up}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in _done:
                failure = task.exception()
                if failure is not None and not isinstance(failure, asyncio.CancelledError):
                    raise failure
        finally:
            with contextlib.suppress(Exception):
                await downstream.close()
