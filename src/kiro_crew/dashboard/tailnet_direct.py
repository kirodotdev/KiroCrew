"""The direct tailnet listener: this machine's own tailnet addresses, gated by daemon ``whois``.

When ``dashboard.shared_crew.tailnet_direct`` is on (it is off by default) and Tailscale
is running, the gateway also listens on this machine's tailnet addresses
(``100.64.0.0/10`` and ``fd7a:115c:a1e0::/48``) on the dashboard port. Nothing
else is bound: never a wildcard, never a LAN address. Loopback is unchanged.

A request is on the direct listener when the LOCAL end of its TCP connection is
one of those addresses (:meth:`DirectGate.owns`). Every such request is
authenticated by the daemon, never by a header:

1. the socket peer (``ip:port`` of the TCP connection) must sit inside the
   tailnet ranges and carry no forwarding header;
2. the daemon's ``whois`` of that ``ip:port`` must name a node and a user
   (:func:`parse_whois`); any failure rejects the request;
3. the user must be admitted by :func:`admission_refusal`: the Crew owner
   (the login of this machine's own node), or a login
   :func:`tailnet.login_allowed` admits, which covers
   ``dashboard.tailscale.allowed_logins`` and logins an in-Crew invite granted
   or revoked since startup. With no login listed or granted, only the owner
   gets in. An ACL-tagged node is admitted only when the list names one of
   its tags.

An admitted request needs no dashboard token. Its principal is keyed by the
stable Tailscale user id, so every node of one user, including a re-created
node and a node shared in from another tailnet, maps to one person.

``whois`` runs once per connection and is cached per ``ip:port`` for
:data:`WHOIS_TTL_SECS`; the admission rule is applied again on every request; a cache refresh that finds a different node key on the
same connection rejects it. A WebSocket upgrade always asks again, and an open
WebSocket is re-verified every :data:`WEBSOCKET_REVERIFY_SECS`.

``whois`` names the user who owns the Tailscale node, not the operating-system
user at that machine: several people sharing one node are one principal, and
need the token path to be told apart.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from kiro_crew.dashboard import tailnet_localapi
from kiro_crew.dashboard.tailnet import (
    _TAILNET_RANGES,
    TAGGED_DEVICES_LOGIN,
    _valid_identity,
    _valid_magicdns_name,
    login_allowed,
)
from kiro_crew.executors import subprocess_executor

if TYPE_CHECKING:
    from aiohttp import web

logger = logging.getLogger(__name__)

#: Request key holding the verified :class:`DirectPeer` of a direct-listener request.
DIRECT_PEER_REQUEST_KEY = "tailnet_direct_peer"

WHOIS_TTL_SECS = 30.0
#: A failed ``whois`` is remembered briefly so a burst from one peer costs one call.
WHOIS_FAILURE_TTL_SECS = 2.0
WHOIS_CACHE_MAX_ENTRIES = 512
WEBSOCKET_REVERIFY_SECS = 60.0

STATE_ACTIVE = "active"
STATE_OFF = "off"
STATE_UNAVAILABLE = "unavailable"

REASON_DISABLED = "disabled"
REASON_GOVERNANCE_PINNED = "governance_pinned_off"
REASON_TAILSCALE_NOT_RUNNING = "tailscale_not_running"
REASON_NO_TAILNET_ADDRESS = "no_tailnet_address"
REASON_OWNER_UNKNOWN = "owner_login_unknown"
REASON_BIND_FAILED = "bind_failed"

REFUSE_NOT_TAILNET_PEER = "not_tailnet_peer"
REFUSE_FORWARDED = "forwarded_header"
REFUSE_WHOIS_FAILED = "whois_failed"
REFUSE_SELF_CONNECTION = "self_connection"
REFUSE_NODE_CHANGED = "node_changed"
REFUSE_ALLOWLIST_UNREADABLE = "allowlist_unreadable"
REFUSE_TAGGED = "tagged_node_not_allowed"
REFUSE_NOT_ALLOWED = "login_not_allowed"
#: A verified person the Crew has not let in, now listed for the owner to approve.
REFUSE_AWAITING_APPROVAL = "awaiting_approval"

_FORWARD_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP")
_NODE_KEY_RE = re.compile(r"\A(?:nodekey:)?[0-9a-f]{64}\Z")
_TAG_RE = re.compile(r"\Atag:[A-Za-z0-9-]{1,100}\Z")
_DISPLAY_MAX = 128


@dataclass(frozen=True)
class DirectPeer:
    """A peer the daemon named for one TCP connection."""

    user_id: str
    login: str
    display_name: str
    node: str
    node_key: str
    tags: tuple[str, ...] = ()
    #: ``UserProfile.ProfilePicURL`` when it is a plain https URL; display only.
    profile_pic_url: str = ""

    @property
    def tagged(self) -> bool:
        return self.login == TAGGED_DEVICES_LOGIN or bool(self.tags)


@dataclass(frozen=True)
class SelfNode:
    """This machine's own tailnet node: its addresses and owner."""

    addresses: tuple[str, ...]
    login: str
    user_id: str
    dns_name: str = ""


def _user_id(raw: object) -> str:
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, int) and raw > 0:
        return str(raw)
    if isinstance(raw, str) and raw.isdigit() and raw != "0":
        return raw
    return ""


def _display(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    text = "".join(ch for ch in raw.strip() if ch.isprintable())
    return text[:_DISPLAY_MAX]


_PICTURE_MAX = 512


def _picture(raw: object) -> str:
    """A profile picture URL a browser may load: ``https``, no credentials, bounded."""
    if not isinstance(raw, str) or len(raw) > _PICTURE_MAX:
        return ""
    url = raw.strip()
    if not url.startswith("https://") or any(ch.isspace() or not ch.isprintable() for ch in url):
        return ""
    host = url[len("https://") :].split("/", 1)[0]
    if not host or "@" in host:
        return ""
    return url


def _tailnet_ip(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0].strip())
    except ValueError:
        return ""
    return str(ip) if any(ip in net for net in _TAILNET_RANGES) else ""


def parse_whois(data: object) -> DirectPeer | None:
    """The :class:`DirectPeer` in a daemon ``whois`` answer, or ``None``.

    The user is ``UserProfile`` (the node's owner); ``Node.Sharer`` only marks
    a node shared in from another tailnet and never names the principal. A
    human needs a positive stable user id; a tagged node is keyed by its node.
    """
    if not isinstance(data, dict):
        return None
    profile = data.get("UserProfile")
    node = data.get("Node")
    if not isinstance(profile, dict) or not isinstance(node, dict):
        return None
    login = _valid_identity(profile.get("LoginName"))
    name = _valid_identity(node.get("ComputedName") or node.get("Name"))
    raw_key = node.get("Key")
    node_key = raw_key.strip().lower() if isinstance(raw_key, str) else ""
    if login is None or name is None or not _NODE_KEY_RE.match(node_key):
        return None
    raw_tags = node.get("Tags")
    tags = tuple(
        sorted({t for t in raw_tags if isinstance(t, str) and _TAG_RE.match(t)})
        if isinstance(raw_tags, list)
        else ()
    )
    user_id = _user_id(profile.get("ID"))
    tagged = login == TAGGED_DEVICES_LOGIN or bool(tags)
    if not tagged and not user_id:
        return None
    return DirectPeer(
        user_id="" if tagged else user_id,
        login=login,
        display_name=_display(profile.get("DisplayName")) or login,
        node=name.rstrip(".").lower(),
        node_key=node_key,
        tags=tags,
        profile_pic_url=_picture(profile.get("ProfilePicURL")),
    )


def parse_status(data: object) -> SelfNode | None:
    """This node's tailnet addresses and owning login from a daemon ``status``."""
    if not isinstance(data, dict):
        return None
    if data.get("BackendState") not in (None, "Running"):
        return None
    self_node = data.get("Self")
    if not isinstance(self_node, dict):
        return None
    raw_ips = self_node.get("TailscaleIPs")
    addresses = tuple(
        dict.fromkeys(ip for ip in (_tailnet_ip(r) for r in raw_ips or []) if ip)
        if isinstance(raw_ips, list)
        else ()
    )
    user_id = _user_id(self_node.get("UserID"))
    users = data.get("User")
    profile: Any = None
    if isinstance(users, dict) and user_id:
        profile = users.get(user_id)
        if profile is None:
            profile = users.get(int(user_id))  # type: ignore[call-overload]
    login = _valid_identity(profile.get("LoginName")) if isinstance(profile, dict) else None
    tailnet = data.get("CurrentTailnet")
    suffix = tailnet.get("MagicDNSSuffix") if isinstance(tailnet, dict) else None
    dns_name = _valid_magicdns_name(self_node.get("DNSName"), suffix) or ""
    return SelfNode(
        addresses=addresses,
        login=login or "",
        user_id=user_id if login else "",
        dns_name=dns_name,
    )


def _split_allowlist(allowed: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str]]:
    """Allowlist entries as lower-cased logins and ``tag:<name>`` tags."""
    logins: set[str] = set()
    tags: set[str] = set()
    for entry in allowed:
        value = entry.strip().lower() if isinstance(entry, str) else ""
        if not value:
            continue
        (tags if value.startswith("tag:") else logins).add(value)
    return frozenset(logins), frozenset(tags)


def admission_refusal(
    peer: DirectPeer,
    *,
    allowed_logins: tuple[str, ...],
    owner: SelfNode,
    identity_unknown: bool = False,
) -> str | None:
    """Why *peer* may not use the direct listener, or ``None`` when it may.

    Matching is by login through :func:`tailnet.login_allowed`, so a login an
    in-Crew invite granted or revoked since startup is followed here too; the
    owner is matched by stable user id. ``allowed_logins`` entries of the form
    ``tag:<name>`` admit an ACL-tagged node carrying that tag and are never a
    login. With no login listed or granted, only the owner is admitted. An
    unreadable allowlist admits nobody. While the derived set trusts the
    network (``local_authorization`` off), every verified person is admitted.
    """
    from kiro_crew.dashboard import tailnet_live_allowlist

    derived = tailnet_live_allowlist.snapshot()
    if derived is not None and derived.network_trust and not peer.tagged:
        return None
    if identity_unknown:
        return REFUSE_ALLOWLIST_UNREADABLE
    if derived is not None:
        return _derived_refusal(peer, owner, derived)
    logins, tags = _split_allowlist(allowed_logins)
    if peer.tagged:
        if tags and any(t.lower() in tags for t in peer.tags):
            return None
        return REFUSE_TAGGED
    if is_owner(peer, owner):
        return None
    if login_allowed(peer.login, tuple(sorted(logins))):
        return None
    return REFUSE_NOT_ALLOWED


def is_owner(peer: DirectPeer, owner: SelfNode) -> bool:
    """Whether the daemon named this machine's own Tailscale user: id and login both match."""
    return bool(
        owner.user_id
        and not peer.tagged
        and peer.user_id == owner.user_id
        and peer.login.lower() == owner.login.lower()
    )


def _derived_refusal(peer: DirectPeer, owner: SelfNode, derived: Any) -> str | None:
    """The admission answer while Shared Crew's derived access set is installed.

    The owner, a person who accepted a Crew invite (by login), and a person the
    owner approved (by stable user id) are admitted. A same-tailnet person the
    Tailscale ACL lets reach this machine is not: they are refused and may be
    listed as waiting for the owner. ``allowed_logins`` is not read at all.
    """
    from kiro_crew.dashboard import tailnet_live_allowlist

    if peer.tagged:
        if (
            derived.tags
            and not derived.unreadable
            and any(t.lower() in derived.tags for t in peer.tags)
        ):
            return None
        return REFUSE_TAGGED
    if is_owner(peer, owner):
        return None
    if derived.unreadable:
        return REFUSE_ALLOWLIST_UNREADABLE
    if tailnet_live_allowlist.user_id_revoked(peer.user_id):
        return REFUSE_NOT_ALLOWED
    if tailnet_live_allowlist.decision(peer.login) is False and not (
        peer.user_id in derived.approved_user_ids
    ):
        return REFUSE_NOT_ALLOWED
    if derived.admits_person(peer.user_id, peer.login):
        return None
    if tailnet_live_allowlist.granted(peer.login):
        # Granted by an invite accept the set has not been rebuilt for yet.
        return None
    return REFUSE_NOT_ALLOWED


@dataclass
class _CacheEntry:
    expires: float
    peer: DirectPeer | None
    reason: str


@dataclass
class DirectGate:
    """Decides every request that arrives on the direct tailnet listener."""

    owner: SelfNode
    port: int
    allowed_logins: tuple[str, ...] = ()
    identity_unknown: bool = False
    whois_fn: Callable[[str], tailnet_localapi.Answer] = tailnet_localapi.whois
    clock: Callable[[], float] = time.monotonic
    _cache: OrderedDict[tuple[str, int], _CacheEntry] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def addresses(self) -> frozenset[str]:
        return frozenset(self.owner.addresses)

    def owns(self, request: web.Request) -> bool:
        """Whether *request* arrived on one of this machine's tailnet addresses."""
        local = _sockaddr(request, "sockname")
        return local is not None and local[0] in self.addresses

    def _lookup(self, address: str, port: int, *, fresh: bool) -> tuple[DirectPeer | None, str]:
        key = (address, port)
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit.expires > now and not fresh:
                self._cache.move_to_end(key)
                return hit.peer, hit.reason or self._refusal(hit.peer)
            previous = hit.peer if hit is not None else None
        answer = self.whois_fn(f"{_host_for_whois(address)}:{port}")
        peer = parse_whois(answer.data)
        reason = "" if peer is not None else REFUSE_WHOIS_FAILED
        if peer is not None and not peer.tagged:
            from kiro_crew.dashboard import tailnet_live_allowlist

            tailnet_live_allowlist.note_login(peer.user_id, peer.login)
        if peer is not None and previous is not None and previous.node_key != peer.node_key:
            peer, reason = None, REFUSE_NODE_CHANGED
        ttl = WHOIS_TTL_SECS if answer.data is not None else WHOIS_FAILURE_TTL_SECS
        with self._lock:
            self._cache[key] = _CacheEntry(self.clock() + ttl, peer, reason)
            self._cache.move_to_end(key)
            while len(self._cache) > WHOIS_CACHE_MAX_ENTRIES:
                self._cache.popitem(last=False)
        return peer, reason or self._refusal(peer)

    def _refusal(self, peer: DirectPeer | None) -> str:
        """The admission answer for a cached ``whois``, decided again on every request.

        Only the daemon's answer is cached, so a login an invite revokes is
        refused on its next request rather than when the cache entry expires.
        """
        if peer is None:
            return REFUSE_WHOIS_FAILED
        from kiro_crew.dashboard import tailnet_live_allowlist

        reason = (
            admission_refusal(
                peer,
                allowed_logins=self.allowed_logins,
                owner=self.owner,
                identity_unknown=self.identity_unknown,
            )
            or ""
        )
        if not reason:
            return ""
        if (
            reason == REFUSE_NOT_ALLOWED
            and peer.user_id
            and not peer.tagged
            and tailnet_live_allowlist.note_waiting(peer)
        ):
            return REFUSE_AWAITING_APPROVAL
        return reason

    def is_owner(self, peer: DirectPeer) -> bool:
        return is_owner(peer, self.owner)

    async def verify(
        self, request: web.Request, *, fresh: bool = False
    ) -> tuple[DirectPeer | None, str]:
        """``(peer, "")`` for an admitted request, ``(peer or None, reason)`` otherwise."""
        remote = _sockaddr(request, "peername")
        if remote is None:
            return None, REFUSE_NOT_TAILNET_PEER
        address, port = remote
        if not _tailnet_ip(address):
            return None, REFUSE_NOT_TAILNET_PEER
        if address in self.addresses:
            # A process on this machine dialling its own tailnet address: whois
            # names the owner's node, but the caller is local, not a person on
            # the tailnet. Local callers use the loopback token path.
            return None, REFUSE_SELF_CONNECTION
        if any(h in request.headers for h in _FORWARD_HEADERS):
            return None, REFUSE_FORWARDED
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                subprocess_executor(), lambda: self._lookup(address, port, fresh=fresh)
            )
        except Exception:  # noqa: BLE001 - a lookup that cannot run verifies nobody
            logger.debug("tailnet direct whois failed to run", exc_info=True)
            return None, REFUSE_WHOIS_FAILED


def _host_for_whois(address: str) -> str:
    return f"[{address}]" if ":" in address else address


def _sockaddr(request: web.Request, name: str) -> tuple[str, int] | None:
    transport = getattr(request, "transport", None)
    if transport is None:
        return None
    try:
        info = transport.get_extra_info(name)
    except Exception:  # noqa: BLE001 - an unreadable socket address owns nothing
        return None
    if not isinstance(info, (tuple, list)) or len(info) < 2:
        return None
    host, port = info[0], info[1]
    if not isinstance(host, str) or not isinstance(port, int):
        return None
    try:
        return str(ipaddress.ip_address(host.split("%", 1)[0])), port
    except ValueError:
        return None


_REFUSAL_TEXT = {
    REFUSE_NOT_ALLOWED: "Only people the Crew owner invited or approved can open this Crew. Ask the owner for an invite.",
    REFUSE_AWAITING_APPROVAL: "You're waiting for the Crew owner to approve you. They can see your request in People. Reload this page once they approve.",
    REFUSE_TAGGED: "This is a tagged Tailscale device. Ask the Crew owner to allow its tag.",
    REFUSE_ALLOWLIST_UNREADABLE: "This Crew cannot read its Tailscale allowlist right now, so nobody is let in.",
    REFUSE_NODE_CHANGED: "Your Tailscale device changed during this connection. Reload the page.",
    REFUSE_SELF_CONNECTION: "This machine reached its own Tailscale address. Open the dashboard on localhost instead.",
}
_REFUSAL_DEFAULT = "Tailscale could not confirm who you are, so this Crew did not let you in."


def refusal_response(request: web.Request, reason: str) -> web.Response:
    """A 403 that says why, as a page for a browser and JSON for everything else."""
    from aiohttp import web

    message = _REFUSAL_TEXT.get(reason, _REFUSAL_DEFAULT)
    heading = (
        "Waiting for the owner to approve you"
        if reason == REFUSE_AWAITING_APPROVAL
        else "You can't open this Crew"
    )
    accept = request.headers.get("Accept", "")
    if request.method in ("GET", "HEAD") and "text/html" in accept:
        body = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>Kiro Crew: access refused</title></head>"
            '<body style="font-family: system-ui, sans-serif; max-width: 36rem; margin: 4rem auto; padding: 0 1rem">'
            f'<h1 style="font-size: 1.25rem">{html.escape(heading)}</h1>'
            f"<p>{html.escape(message)}</p>"
            f'<p style="color: #666; font-size: 0.875rem">Reason: <code>{html.escape(reason)}</code></p>'
            "</body></html>"
        )
        return web.Response(status=403, text=body, content_type="text/html")
    return web.json_response(
        {"error": message, "code": "tailnet_direct_refused", "reason": reason}, status=403
    )


async def run_with_reverify(
    request: web.Request,
    gate: DirectGate,
    peer: DirectPeer,
    handler: Callable[[web.Request], Awaitable[Any]],
    *,
    interval: float = WEBSOCKET_REVERIFY_SECS,
) -> Any:
    """Run *handler*, closing the connection when *peer* stops verifying.

    For a WebSocket or an event stream this ends a removed person's live view;
    a short request finishes before the first check and costs one idle task.
    """

    async def _watch() -> None:
        while True:
            await asyncio.sleep(interval)
            again, reason = await gate.verify(request, fresh=True)
            if (
                reason
                or again is None
                or again.user_id != peer.user_id
                or again.node_key != peer.node_key
            ):
                logger.info(
                    "tailnet direct peer no longer verifies (%s); closing its connection",
                    reason or "changed",
                )
                transport = getattr(request, "transport", None)
                if transport is not None:
                    transport.close()
                return

    watcher = asyncio.create_task(_watch())
    try:
        return await handler(request)
    finally:
        watcher.cancel()


@dataclass(frozen=True)
class DirectResolution:
    """What the gateway found at start: a gate to install, or why there is none."""

    state: str
    reason: str = ""
    gate: DirectGate | None = None
    method: str = tailnet_localapi.METHOD_NONE


@dataclass
class GateRef:
    """The direct gate the middleware chain reads, filled in after the bind.

    The chain is built before the bind, but the gate needs a daemon read, which
    runs only once the dashboard answers. ``None`` until then, and whenever the
    listener is off: no direct site is bound without a gate, so nothing waits.
    """

    gate: DirectGate | None = None


def resolve(
    *,
    port: int,
    allowed_logins: tuple[str, ...],
    identity_unknown: bool,
    governance_pinned: bool,
    status_fn: Callable[[], tailnet_localapi.Answer] = tailnet_localapi.status,
) -> DirectResolution:
    """Read the daemon once and decide whether the direct listener runs. Blocking."""
    if governance_pinned:
        return DirectResolution(STATE_UNAVAILABLE, REASON_GOVERNANCE_PINNED)
    answer = status_fn()
    owner = parse_status(answer.data)
    if owner is None:
        return DirectResolution(
            STATE_UNAVAILABLE, REASON_TAILSCALE_NOT_RUNNING, method=answer.method
        )
    if not owner.addresses:
        return DirectResolution(STATE_UNAVAILABLE, REASON_NO_TAILNET_ADDRESS, method=answer.method)
    if not owner.login or not owner.user_id:
        return DirectResolution(STATE_UNAVAILABLE, REASON_OWNER_UNKNOWN, method=answer.method)
    gate = DirectGate(
        owner=owner,
        port=port,
        allowed_logins=allowed_logins,
        identity_unknown=identity_unknown,
    )
    return DirectResolution(STATE_ACTIVE, gate=gate, method=answer.method)


def direct_origins(gate: DirectGate, port: int) -> set[str]:
    """The browser origins the direct listener serves, for the CSRF and ``Host`` allowlists."""
    origins = {f"http://{_host_for_whois(a)}:{port}" for a in gate.owner.addresses}
    if gate.owner.dns_name:
        origins.add(f"http://{gate.owner.dns_name}:{port}")
    return origins


def direct_urls(gate: DirectGate, port: int) -> list[str]:
    """The plain share links, the MagicDNS name first and then each address."""
    urls: list[str] = []
    if gate.owner.dns_name:
        urls.append(f"http://{gate.owner.dns_name}:{port}/")
    urls.extend(f"http://{_host_for_whois(a)}:{port}/" for a in gate.owner.addresses)
    return urls


async def start_sites(runner: web.AppRunner, port: int, gate: DirectGate) -> list[web.BaseSite]:
    """Bind one site per tailnet address of this machine on *port*. Nothing else is bound.

    An address that will not bind is skipped and logged; the caller reports the
    capability from what actually started.
    """
    from aiohttp import web

    sites: list[web.BaseSite] = []
    for address in gate.owner.addresses:
        if not _tailnet_ip(address):
            continue
        # Bound and listening in one call, as the main listener binds: on POSIX
        # SO_REUSEADDR lets a restart rebind through the last run's TIME_WAIT
        # connections; Windows keeps it off, where it would let another process
        # bind over a live listener.
        site = web.TCPSite(runner, address, port, reuse_address=os.name == "posix")
        try:
            await site.start()
        except OSError as exc:
            logger.warning("tailnet direct listener could not bind %s:%d: %s", address, port, exc)
            continue
        sites.append(site)
        logger.info("tailnet direct listener on %s:%d (whois-verified, token-free)", address, port)
    return sites


__all__ = [
    "DIRECT_PEER_REQUEST_KEY",
    "DirectGate",
    "DirectPeer",
    "DirectResolution",
    "GateRef",
    "SelfNode",
    "admission_refusal",
    "direct_origins",
    "is_owner",
    "direct_urls",
    "parse_status",
    "parse_whois",
    "refusal_response",
    "resolve",
    "run_with_reverify",
    "start_sites",
]
