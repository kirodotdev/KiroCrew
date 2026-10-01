"""Client affinity for Slack deliveries that outlive a Reconnect.

``POST /api/slack/reconnect`` replaces the gateway's live Web API client
(``GatewayOrchestrator.slack``) while work started under the previous client
may still be in flight: a turn awaiting its agent, an approval card waiting
for the owner's click, a modal about to be updated. Every destination that
work holds -- channel id, thread ``ts``, message ``ts`` -- was minted by the
workspace the OLD client reached. Sent through the new client after a switch
to another workspace it is lost (``channel_not_found``) or, on a colliding
id, misrouted.

The receiving side already binds the routing of a message to the client that
received it (``_route_message``'s ``received_by``, the queue entry's client).
This module closes the rest: the client an envelope arrived through is bound
to the TASK CONTEXT of that envelope, and ``GatewayOrchestrator.slack``
resolves to the bound client whenever one is set. ``asyncio.create_task``
copies the context, so every task spawned while handling the envelope --
the interactive dispatch, the slash command, the agent turn and its final
post -- keeps answering through the client that received it, however many
reconnects happen meanwhile. Code that runs outside any envelope (the HTTP
routes, boot, cron) sees the live client as before.

Deliberately strict: a bound delivery never falls back to the live client,
even for a same-workspace token rotation, because nothing here can tell a
rotation from a switch without a network round trip, and the failure of a
post through a revoked token is visible (the Web API says so) where a post
into the wrong workspace is not.

The AUTHORIZATION SUBJECT travels the same way. ``slack.handler`` keeps the
owner id (the only authorized Slack user) as a module global that a Reconnect
rebinds from the credential store, and a turn reads it many awaits after its
envelope arrived (``is_allowed_user`` after a Web API call, after queueing).
An envelope received under workspace A's owner and resumed after a Reconnect
to workspace B would otherwise be authorized against B's owner -- and a
sender id that happens to equal B's owner's (Slack ids are per workspace;
nothing prevents the collision, and the W/U prefix cross-match widens it)
would hold owner privileges in A's thread. So the owner the socket was
built with is bound to each envelope's context beside its client
(``owner_scope``), and ``is_owner`` resolves the bound owner whenever one is
set; a queued turn carries it in its queue entry like the client.

What is bound is a REVOCABLE authority (``SocketAuthority``), one per socket,
not the owner string: a Reconnect whose close of the former socket fails or
times out keeps that socket referenced and de-authorizes through the module
globals, and a binding that outranked those globals would let the surviving
listener keep authorizing the former owner on every envelope it still
delivers. Revoking the socket's authority reaches every envelope bound to it,
queued turns included, in one step.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:  # pragma: no cover
    from kiro_crew.slack.client import RealSlackClient


class _Unbound:
    """Sentinel type: no client is bound to the current context."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNBOUND"


UNBOUND: Any = _Unbound()

_bound_client: ContextVar[Any] = ContextVar("kiro_crew.slack.bound_client", default=UNBOUND)


def bound_client() -> Any:
    """The client bound to the current context, or ``UNBOUND``.

    ``None`` is a legitimate binding (an envelope received while the gateway
    held no Web API client), which is why absence is a sentinel, not ``None``.
    """
    return _bound_client.get()


class SocketAuthority:
    """The authorization subject one Socket Mode socket was built with.

    ``owner_id`` is the owner every envelope the socket receives is authorized
    against, until :meth:`revoke`: from then on it is ``""`` -- nobody -- for
    every envelope still bound to this socket, in flight or queued. The gateway
    revokes it when it retires a socket it could not close (the listener may
    still be delivering) and when it tears down a socket whose workspace was
    never admitted; a socket closed cleanly delivers nothing more, and the
    turns it received finish under the owner they were received from.
    """

    __slots__ = ("_owner_id", "_revoked", "_self_bot_id")

    def __init__(self, owner_id: str, self_bot_id: str = "") -> None:
        self._owner_id = owner_id
        self._revoked = False
        self._self_bot_id = self_bot_id

    @property
    def owner_id(self) -> str:
        return "" if self._revoked else self._owner_id

    @property
    def self_bot_id(self) -> str:
        """The gateway's OWN ``bot_id`` in the workspace THIS socket was admitted
        to (``auth.test``), bound per socket for the same reason the owner is:
        the trusted-bot admission excludes the gateway's own id so its replies
        never re-enter as input, and a Reconnect that switches workspace
        replaces the module-level validated id with the new workspace's --
        an event of the former workspace still in its ack suspension would
        then be judged against the wrong self id. Empty until the socket's
        workspace is validated, which fails admission closed; "" once revoked."""
        return "" if self._revoked else self._self_bot_id

    def bind_self_bot_id(self, self_bot_id: str) -> None:
        """Record the validated self id once ``auth.test`` named it."""
        self._self_bot_id = self_bot_id

    @property
    def revoked(self) -> bool:
        return self._revoked

    def revoke(self) -> None:
        self._revoked = True

    def __repr__(self) -> str:
        state = "revoked" if self._revoked else "live"
        return f"SocketAuthority({self._owner_id!r}, {state})"


_bound_owner: ContextVar[Any] = ContextVar("kiro_crew.slack.bound_owner", default=UNBOUND)


def bound_owner() -> Any:
    """The :class:`SocketAuthority` bound to the current context, or ``UNBOUND``."""
    return _bound_owner.get()


@contextmanager
def owner_scope(authority: SocketAuthority) -> Iterator[None]:
    """Bind *authority* as the authorization subject for the block (and tasks
    it spawns). Nesting and restoration as for :func:`client_scope`."""
    token = _bound_owner.set(authority)
    try:
        yield
    finally:
        _bound_owner.reset(token)


@contextmanager
def client_scope(client: "RealSlackClient | Any | None") -> Iterator[None]:
    """Bind *client* for the dynamic extent of the block (and tasks it spawns).

    Nesting is honoured: the inner binding wins inside, the outer one is
    restored on exit. Re-binding the same client is a no-op in effect.
    """
    token = _bound_client.set(client)
    try:
        yield
    finally:
        _bound_client.reset(token)
