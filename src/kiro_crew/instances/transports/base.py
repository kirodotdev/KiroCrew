"""The ``PeerTransport`` seam: one object per way of reaching a remote crew.

Every per-method decision the gateway makes about an instance — how to validate
its addressing, what child to spawn, whether there is a dashboard token to mint,
which diagnosis ladder to run, whether a remote restart exists — is answered by
the transport resolved from ``Instance.connection_method``, not by a conditional
at each call site. The manager keeps the lifecycle (locks, epochs, ports,
self-heal tiers); the transport answers only "how do I reach this peer".

Why a protocol and not a base class: two of the members a transport must answer
are *capabilities* rather than behaviour (``mints_token``, the timeout defaults),
and a protocol lets a future transport be a plain object that states them. The
one piece of genuinely shared behaviour — a transport whose reach is a supervised
child process forwarding a loopback port — lives in :class:`ChildForwardTransport`,
which all three shipped transports use.

The seam exists so a fourth transport is an addition rather than a fourth arm on
eighteen conditionals. RFC ``docs/request-for-change/rfc-outbound-instance-transport.md``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from kiro_crew.instances.registry import Instance
from kiro_crew.instances.transports.params import TransportParams


class TransportRefusal(RuntimeError):
    """A transport was asked for something it structurally cannot do.

    Raised rather than returned so a caller cannot forget to check: the message
    is owner-facing and is surfaced verbatim (``restart_remote`` turns it into
    its ``{ok: False, message}`` answer). Distinct from a validation error — the
    record is fine, the *operation* does not exist for this transport.
    """


@dataclass(frozen=True)
class TransportSeams:
    """The callables through which a transport reaches the outside world.

    Built per call by the manager from its OWN module globals, which keeps
    ``kiro_crew.instances.ssh_tunnel_manager`` the single substitution point the
    test suite already patches: moving a call into a transport must not move
    where it can be faked. ``mint_ssh`` is additionally the manager's
    constructor-injected mint seam.

    A transport uses the seams it needs and ignores the rest — nothing here is a
    branch, and no transport inspects another's fields. The split exists so the
    transports hold only policy (what to call, with which validated arguments)
    while the manager holds the wiring.
    """

    mint_ssh: Callable[..., Awaitable[str]]
    mint_ssm: Callable[..., Awaitable[str]]
    restart_ssh: Callable[..., Awaitable[tuple[int, str | None]]]
    restart_ssm: Callable[..., Awaitable[tuple[int, str | None]]]
    diagnose_ssh: Callable[..., Awaitable[Any]]
    diagnose_ssm: Callable[..., Awaitable[Any]]
    diagnose_fargate: Callable[..., Awaitable[Any]]


@runtime_checkable
class PeerTransport(Protocol):
    """How the gateway reaches one remote crew.

    Implementations are stateless and shared: resolve once, call many times.
    """

    #: The ``connection_method`` string this transport owns.
    method: str

    #: Whether the peer serves a dashboard whose token can be minted. False for a
    #: transport that forwards to something other than a Kiro Crew gateway, which
    #: is what the connect path and self-heal tier 2 branch on instead of the
    #: method name.
    mints_token: bool

    #: Readiness timeout default for this transport, in seconds. An explicit
    #: caller override still wins on the manager.
    connect_timeout_default: float

    #: Token-mint timeout default for this transport, in seconds.
    mint_timeout_default: float

    def validate(self, inst: Instance) -> TransportParams:
        """Validate *inst*'s addressing and return its resolved params.

        Runs immediately before a command line is built, so a record stored
        before a rule existed is refused here rather than failing later with a
        generic error. Raises ``SshValidationError`` / ``SsmValidationError``.
        """
        ...

    def open(self, params: TransportParams) -> dict[str, Any]:
        """Kwargs describing the reach this transport opens for *params*."""
        ...

    def describe_target(self, params: TransportParams) -> str:
        """The addressing value that identifies the peer, for messages."""
        ...

    async def mint(
        self,
        inst: Instance,
        params: TransportParams,
        *,
        parent_port: int | None,
        timeout_secs: float,
        seams: TransportSeams,
    ) -> str:
        """Mint a dashboard token for *inst* over this transport.

        Raises ``TokenMintError`` — including for a transport with no token to
        mint. Never logs the token.
        """
        ...

    async def diagnose(
        self,
        inst: Instance,
        *,
        local_port: int,
        connect_timeout_secs: float,
        seams: TransportSeams,
    ) -> Any:
        """Run this transport's read-only failure-diagnosis ladder."""
        ...

    async def restart(
        self,
        inst: Instance,
        params: TransportParams,
        *,
        timeout_secs: float,
        seams: TransportSeams,
    ) -> tuple[int, str | None]:
        """Restart the peer's Kiro Crew gateway; return ``(rc, error)``.

        Raises :class:`TransportRefusal` when this transport reaches something
        that runs no gateway to restart.
        """
        ...


class ChildForwardTransport:
    """Shared behaviour for a transport whose reach is a supervised child process.

    All three shipped transports spawn a local child that forwards a loopback
    port to the peer, so ``open`` is the child's argv dimensions — taken from
    :meth:`TransportParams.tunnel_kwargs` rather than restated here, so the
    transport and the spawn site cannot disagree.

    A transport that needs no child (the RFC's outbound transport opens an
    in-process listener over an already-dialled channel) does not inherit this.
    """

    def open(self, params: TransportParams) -> dict[str, Any]:
        """The forwarder child's transport dimensions for *params*."""
        return params.tunnel_kwargs()
