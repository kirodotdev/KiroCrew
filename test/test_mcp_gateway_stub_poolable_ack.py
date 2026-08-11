"""Mixed-version safety: a private backend must never be silently pooled.

``poolable`` is a register-payload field, so a daemon predating it ignores the
flag and routes every register through the shared index. That daemon is
reachable in production: ``GatewayManager`` adopts any process answering
``pong`` with no version handshake, so one that outlived a package upgrade
serves brand-new stubs. A stub cannot detect the outcome after the fact — by the
time it would notice, a stateful server the operator never allowlisted is
already co-tenanted.

So the stub negotiates: no ``poolable_ack`` attestation and no sharing asked for
means abandon the gateway and exec the real server directly, which IS the
private topology it wanted.
"""

from __future__ import annotations

from kiro_crew.mcp_gateway.gatewayd import REGISTERED_CAPABILITIES
from kiro_crew.mcp_gateway.stub import must_degrade_unshareable


def test_current_daemon_advertises_the_attestation() -> None:
    """The two sides must not drift: the stub's gate is only reachable because
    a current daemon says the word."""
    assert "poolable_ack" in REGISTERED_CAPABILITIES


def test_private_stub_against_an_old_daemon_degrades() -> None:
    """The hazard: flag sent, daemon ignores it, register lands in the shared
    index. Degrading is the only outcome that preserves what was asked for."""
    assert must_degrade_unshareable(poolable=False, capabilities=["ensure_backend"]) is True
    assert must_degrade_unshareable(poolable=False, capabilities=[]) is True


def test_private_stub_against_a_current_daemon_proceeds() -> None:
    """Over-degrading would cost every private server the gateway — no stub, no
    MCP Apps — which is the coupling this whole change removes."""
    assert (
        must_degrade_unshareable(
            poolable=False, capabilities=list(REGISTERED_CAPABILITIES)
        )
        is False
    )


def test_shareable_stub_needs_no_attestation() -> None:
    """A stub that asked to share is not harmed by an old daemon: it pools the
    register, which is exactly the request. Degrading here would throw away
    pooling for no safety gain."""
    assert must_degrade_unshareable(poolable=True, capabilities=[]) is False
    assert (
        must_degrade_unshareable(
            poolable=True, capabilities=list(REGISTERED_CAPABILITIES)
        )
        is False
    )
