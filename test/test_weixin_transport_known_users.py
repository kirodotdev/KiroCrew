"""`WeixinTransport._known_users` is a bounded LRU, not an unbounded set.

Under ``dm_policy="open"`` the transport serves any DM, so the key space of
recently-seen peers is every user who messages the bot. ``_known_users`` caps
that at ``KNOWN_USERS_CAP`` so it cannot grow without bound for the life of the
gateway process. These tests drive the real ``receive`` entry point and pin the
bound, plus the behaviour that makes eviction safe: an evicted peer that writes
again is re-admitted, and proactive-send authorization (``may_send_to``) never
consults this set, so eviction revokes nothing.
"""

from __future__ import annotations

import asyncio

from kiro_crew.weixin import transport as _transport
from kiro_crew.weixin.client import ITEM_TEXT
from kiro_crew.weixin.transport import WeixinTransport

# Import the cap when the transport exposes it; fall back to its value otherwise
# so a transport without the named cap fails this test BEHAVIOURALLY (the set
# grows past the bound) rather than at import, i.e. red for the right reason.
KNOWN_USERS_CAP = getattr(_transport, "KNOWN_USERS_CAP", 512)


class _StubClient:
    """Stands in for WeixinClient: receive() touches no client method on this path."""


class _StubCtxStore:
    """Stands in for ContextTokenStore: .set is only called with a context_token,
    and this test sends none, so it stays untouched."""

    def set(self, *a, **k):  # pragma: no cover - never reached in this test
        raise AssertionError("ctx store should not be touched without a context_token")


def _envelope(user_id: str, *, msg_id: str | None = None) -> dict:
    return {
        "from_user_id": user_id,
        "msg_id": msg_id or f"m-{user_id}",
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
    }


def _open_transport() -> WeixinTransport:
    return WeixinTransport(
        _StubClient(),
        account_id="acct",
        ctx_store=_StubCtxStore(),
        allowed_user_ids=(),
        dm_policy="open",  # supported config: serve any DM
        dispatch=None,  # no downstream; we only exercise the state mutation
    )


def test_known_users_is_bounded_under_many_distinct_senders() -> None:
    transport = _open_transport()
    baseline = len(transport._known_users)

    n = 1000

    async def drive() -> None:
        for i in range(n):
            await transport.receive(_envelope(f"stranger-{i}"))

    asyncio.run(drive())

    assert len(transport._known_users) <= KNOWN_USERS_CAP, (
        f"_known_users grew to {len(transport._known_users)} after {n} distinct "
        f"senders (cap {KNOWN_USERS_CAP}, baseline {baseline}); it must be bounded"
    )
    # The most recent peers are the ones kept.
    assert f"stranger-{n - 1}" in transport._known_users
    assert "stranger-0" not in transport._known_users
    # The cap counts what it evicted, so the overflow can be disclosed.
    assert transport._known_users_evicted == n - KNOWN_USERS_CAP


def test_overflow_is_disclosed_once_per_snapshot_and_in_the_refusal() -> None:
    # Per a-bound-bounds-every-field-it-retains: a truncated tail must not read as
    # a population that never named those peers. The snapshot discloses the
    # overflow once, and an unresolved target past the cap names the truncation.
    transport = _open_transport()

    async def drive() -> None:
        for i in range(KNOWN_USERS_CAP + 5):
            await transport.receive(_envelope(f"stranger-{i}"))

    asyncio.run(drive())

    overflow = [t for t in transport.configured_targets() if not t.available]
    assert len(overflow) == 1, "overflow must be disclosed exactly once per snapshot"
    assert overflow[0].unavailable_reason == "known_users_truncated"
    # The sentinel is not a send target and cannot be read as an owner.
    assert not overflow[0].target_id.startswith("user:")

    # An unresolved target while the roster is truncated: the refusal must NOT put
    # the raw (possibly credential-shaped) target id into SEL's caller field,
    # which is not redacted on persistence; it goes in a fixed caller, and the
    # truncation is named in the redacted resources as a property of the roster.
    import kiro_crew.weixin.transport as _t

    captured: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            captured.append(kw)

    orig = _t.sel
    _t.sel = lambda: _Sel()  # type: ignore[assignment]
    # A credential-SHAPED id (not a real AWS key literal, which the content scan
    # flags; the point stands for any secret-shaped value): it must not reach
    # `caller`.
    shaped = "secret-" + "Z" * 32
    try:
        resolved = asyncio.run(transport.resolve_configured_target(f"user:{shaped}"))
    finally:
        _t.sel = orig  # type: ignore[assignment]
    assert resolved is None
    assert len(captured) == 1
    assert captured[0]["caller"] == "weixin", "raw target id must not land in caller"
    assert shaped not in str(captured[0].values())
    assert "known_users_truncated=true" in captured[0]["resources"]


def test_no_overflow_disclosure_before_the_cap_is_reached() -> None:
    # The snapshot carries no overflow sentinel while nothing has been evicted.
    transport = _open_transport()

    async def drive() -> None:
        await transport.receive(_envelope("only-peer"))

    asyncio.run(drive())
    assert all(t.available for t in transport.configured_targets())
    assert transport._known_users_evicted == 0


def test_an_oversized_user_id_is_served_but_not_retained() -> None:
    # The byte size of a retained id must be bounded too, but without dropping
    # the message: an over-long id is still dispatched, it is just not remembered
    # as a recent peer. A normal-length id is retained as usual.
    from kiro_crew.weixin.transport import MAX_RETAINED_USER_ID_LEN

    dispatched: list[str] = []

    async def _dispatch(msg) -> None:
        dispatched.append(msg.user_id)

    transport = WeixinTransport(
        _StubClient(),
        account_id="acct",
        ctx_store=_StubCtxStore(),
        allowed_user_ids=(),
        dm_policy="open",
        dispatch=_dispatch,
    )
    huge = "x" * (MAX_RETAINED_USER_ID_LEN + 1)
    at_cap = "y" * MAX_RETAINED_USER_ID_LEN

    async def drive() -> None:
        await transport.receive(_envelope(huge))
        await transport.receive(_envelope(at_cap))

    asyncio.run(drive())

    # The over-long id was delivered to the agent, not silently dropped.
    assert huge in dispatched
    # ...but it is not remembered, so it never bloats the LRU key space.
    assert huge not in transport._known_users
    # An id exactly at the retention ceiling is kept (boundary inclusive).
    assert at_cap in transport._known_users


def test_an_evicted_peer_is_re_admitted_when_it_writes_again() -> None:
    transport = _open_transport()

    async def drive() -> None:
        # stranger-0 speaks first, then enough others to push it past the cap.
        await transport.receive(_envelope("stranger-0"))
        for i in range(1, KNOWN_USERS_CAP + 1):
            await transport.receive(_envelope(f"stranger-{i}"))
        assert "stranger-0" not in transport._known_users  # evicted
        # It writes again (a new message id, so dedup does not drop it) and comes
        # straight back as the newest entry.
        await transport.receive(_envelope("stranger-0", msg_id="m-stranger-0-again"))

    asyncio.run(drive())

    assert "stranger-0" in transport._known_users
    assert len(transport._known_users) <= KNOWN_USERS_CAP


def test_eviction_does_not_affect_proactive_send_authorization() -> None:
    # may_send_to consults _allowed alone, so churning _known_users past the cap
    # changes nothing about who may receive a proactive message.
    allowed = "wxid_allowed"
    transport = WeixinTransport(
        _StubClient(),
        account_id="acct",
        ctx_store=_StubCtxStore(),
        allowed_user_ids=(allowed,),
        dm_policy="allowlist",
        dispatch=None,
    )

    async def drive() -> None:
        for i in range(KNOWN_USERS_CAP + 100):
            await transport.receive(_envelope(f"stranger-{i}"))

    asyncio.run(drive())

    # An allow-listed peer may always be sent to, whether or not it is in the LRU.
    assert transport.may_send_to(allowed) is True
    # A peer learned only from inbound traffic is never a proactive-send target,
    # evicted or not.
    assert transport.may_send_to("stranger-0") is False
    assert transport.may_send_to(f"stranger-{KNOWN_USERS_CAP + 99}") is False
