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

The same daemon reachability produces a mirror hazard for the opposite request.
A server that separates the co-tenants the gateway cannot NAME by the
per-connection nonce is safe to pool only while a nonce keeps arriving, and an
adopted pre-nonce daemon mints none — so ``tenant_nonce`` is attested the same
way and ``must_degrade_nonce_blind`` execs a per-session backend when the word is
missing. Nothing downstream can catch that case: at the backend an absent tenant
block is equally what a 1:1 topology with no gateway looks like, and the two need
opposite answers.
"""

from __future__ import annotations

from kiro_crew.mcp_caller import POOLING_REQUIRES_TENANT_NONCE
from kiro_crew.mcp_gateway.gatewayd import REGISTERED_CAPABILITIES
from kiro_crew.mcp_gateway.stub import (
    must_degrade_nonce_blind,
    must_degrade_unshareable,
)

#: A server whose unnamed co-tenants are separated by the nonce, and one that is
#: not. Read from the constant rather than spelled, so the pair cannot name a
#: server the predicate has stopped scoping itself to.
_NONCE_SERVER = "kirocrew-computer"
_PLAIN_SERVER = "kirocrew-core"


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


def test_the_scoping_constant_names_the_server_under_test() -> None:
    """The pair below is only meaningful while the constant agrees with it.

    ``_NONCE_SERVER`` must be in scope and ``_PLAIN_SERVER`` out of it, or the
    two-sided tests are both asserting the same branch and neither would notice
    the predicate losing its scope.
    """
    assert _NONCE_SERVER in POOLING_REQUIRES_TENANT_NONCE
    assert _PLAIN_SERVER not in POOLING_REQUIRES_TENANT_NONCE


def test_current_daemon_advertises_the_nonce_attestation() -> None:
    """The mirror of the drift check above: the stub's nonce gate is only
    reachable because a current daemon says this word too."""
    assert "tenant_nonce" in REGISTERED_CAPABILITIES


def test_pooled_stub_against_a_nonce_blind_daemon_degrades() -> None:
    """The hazard: pooling asked for and granted, and every unnamed co-tenant of
    the one backend lands in a single namespace because no nonce separates them.
    An exclusive backend is where the per-process fallback is correct, so the
    exec restores the separation rather than approximating it."""
    assert (
        must_degrade_nonce_blind(_NONCE_SERVER, poolable=True, capabilities=["ensure_backend"])
        is True
    )
    assert must_degrade_nonce_blind(_NONCE_SERVER, poolable=True, capabilities=[]) is True


def test_pooled_stub_against_a_current_daemon_proceeds() -> None:
    """Over-degrading would spend one process per session on the pooled path this
    server is otherwise cleared for."""
    assert (
        must_degrade_nonce_blind(
            _NONCE_SERVER, poolable=True, capabilities=list(REGISTERED_CAPABILITIES)
        )
        is False
    )


def test_private_stub_needs_no_nonce_attestation() -> None:
    """A stub that asked for a PRIVATE backend gets one backend per session
    whatever the daemon's generation, so there are no co-tenants to separate.
    ``must_degrade_unshareable`` already guards that request."""
    assert must_degrade_nonce_blind(_NONCE_SERVER, poolable=False, capabilities=[]) is False
    assert (
        must_degrade_nonce_blind(
            _NONCE_SERVER, poolable=False, capabilities=list(REGISTERED_CAPABILITIES)
        )
        is False
    )


def test_an_unaffected_server_keeps_pooling_on_a_nonce_blind_daemon() -> None:
    """Scoping is the point, not an optimisation. A server that does not key
    per-tenant state on the nonce loses nothing without it, and degrading every
    pooled stub would fork one process per session across the fleet on any host
    whose daemon outlived a package upgrade."""
    assert must_degrade_nonce_blind(_PLAIN_SERVER, poolable=True, capabilities=[]) is False
    assert must_degrade_nonce_blind("slack-mcp", poolable=True, capabilities=[]) is False
