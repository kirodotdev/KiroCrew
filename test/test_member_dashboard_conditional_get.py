"""Per-user conditional-GET safety for the crewmate Dashboard page.

Same route as ``test_member_dashboard_routes.py``, split out for the file-line cap; the
autouse ``_env`` fixture and ``_client`` / ``_q`` are imported from there.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Imported for their side effects on this module: ``_env`` is autouse, so importing
# its name binds it here and pytest runs it for every test below; ``_client`` / ``_q``
# are the request helpers the tests call; ``SLUG`` is the fixture crewmate's slug.
from test_member_dashboard_routes import (  # noqa: F401
    SLUG,
    _client,
    _env,
    _q,
)

from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates import instance

pytestmark = pytest.mark.asyncio


async def test_a_composed_read_carries_an_etag_and_revalidates_304():
    """The conditional GET on the Dashboard page: a composed page carries a strong ETag, and
    a re-read echoing it back gets a bodyless 304 so the ~180 KB download is skipped for
    a provably-unchanged page."""
    async with _client() as client:
        instance.adopt(SLUG, "fixture-board")
        first = await client.get(_q(""))
        assert first.status == 200
        etag = first.headers.get("ETag")
        assert etag, "a composed read must carry an ETag"
        assert "rendered_html" in await first.json()
        # The browser's own revalidation: echo the ETag back, get a 304 with no body.
        again = await client.get(_q(""), headers={"If-None-Match": etag})
        assert again.status == 304
        assert again.headers.get("ETag") == etag
        assert (await again.read()) == b""


async def test_a_stale_if_none_match_still_gets_the_full_body():
    """An If-None-Match that does NOT match recomposes and sends the full 200 body with
    the current ETag -- the cache only ever short-circuits a proven match."""
    async with _client() as client:
        instance.adopt(SLUG, "fixture-board")
        resp = await client.get(_q(""), headers={"If-None-Match": '"not-the-current-one"'})
        assert resp.status == 200
        assert resp.headers.get("ETag")
        assert "rendered_html" in await resp.json()


async def test_the_empty_state_carries_no_etag():
    """Only a composed page is cacheable: the empty/no-html answer carries no ETag, so
    it is never short-circuited and always sends its (small) body."""
    async with _client() as client:
        resp = await client.get(_q(""))
        assert resp.status == 200
        assert "ETag" not in resp.headers


async def test_a_new_version_is_never_served_as_a_304_on_the_real_route():
    """A new instance version moves the body (`instance_version`), so a re-read echoing
    the earlier ETag must be recomposed (200) with a new ETag, never short-circuited to
    a 304 with the old page. This drives the REAL route: it reads once, writes a new
    version through the store the handler reads, and re-reads with the old validator --
    so it proves the property on the handler, not on a local digest of a hand-built
    dict."""
    async with _client() as client:
        instance.adopt(SLUG, "fixture-board")
        first = await client.get(_q(""))
        assert first.status == 200
        etag_before = first.headers.get("ETag")
        assert etag_before, "a composed read must carry an ETag"
        # The same read, unchanged, still revalidates to a 304 -- the perf win holds
        # right up until the page moves.
        unchanged = await client.get(_q(""), headers={"If-None-Match": etag_before})
        assert unchanged.status == 304
        assert instance.adopt(SLUG, "fixture-board").instance_version == 2
        after = await client.get(_q(""), headers={"If-None-Match": etag_before})
        assert (
            after.status == 200
        ), "a page with a new version was served a 304 against the earlier ETag"
        etag_after = after.headers.get("ETag")
        assert etag_after and etag_after != etag_before
        assert (await after.json())["instance_version"] == 2


@asynccontextmanager
async def _client_as(reader: str, *, owner_gate=None, state=None):
    """A started client whose requests carry *reader* as ``request["user"]``.

    A front middleware stands in for token auth, so one route can be driven as two
    identities. *owner_gate* replaces ``routes._owner_only`` to test its order against
    the 304 shortcut (the autouse fixture otherwise opens the gate).
    """
    seen_order: list[str] = []

    @web.middleware
    async def _mark_reader(request, handler):
        request["user"] = reader
        seen_order.append("auth")
        return await handler(request)

    app = web.Application(middlewares=[_mark_reader])
    if state is not None:
        app["state"] = state
    saved = routes._owner_only
    if owner_gate is not None:

        async def _gate(request, operation):
            seen_order.append("owner_gate")
            return await owner_gate(request, operation)

        routes._owner_only = _gate
    routes.register_member_dashboard_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c, seen_order
    finally:
        await c.close()
        routes._owner_only = saved


async def test_the_conditional_get_is_safe_on_a_per_user_authenticated_page():
    """One reader's ``If-None-Match`` never gets a 304 for another reader's page.

    The ETag is keyed to the authenticated reader, the owner gate runs before any 304,
    and the response is ``Cache-Control: private``.
    """
    import kiro_crew.dashboard.handlers.member_dashboard as mod

    # (3) + a reader-A baseline. The gate is open here (the sibling boundary); what we
    # read off A is its ETag and its Cache-Control.
    async with _client_as("owner-A") as (client, _order):
        instance.adopt(SLUG, "fixture-board")
        a_first = await client.get(_q(""))
        assert a_first.status == 200
        cache_control = a_first.headers.get("Cache-Control", "")
        # (3) a shared/intermediary cache must not store an owner-only page.
        assert "private" in cache_control or "no-store" in cache_control, (
            f"the dashboard page must be Cache-Control private/no-store, got " f"{cache_control!r}"
        )
        etag_a = a_first.headers.get("ETag")
        assert etag_a, "a composed read must carry an ETag"
        # Reader A revalidating its OWN tag is correctly 304'd (the perf win is intact).
        a_again = await client.get(_q(""), headers={"If-None-Match": etag_a})
        assert a_again.status == 304

    # (1) The SAME page, read by a different authenticated identity, mints a DIFFERENT
    # tag, and reader B echoing reader A's tag is recomposed (200), never handed A's
    # cached page via a 304.
    async with _client_as("owner-B") as (client_b, _order_b):
        b_first = await client_b.get(_q(""))
        assert b_first.status == 200
        etag_b = b_first.headers.get("ETag")
        assert etag_b, "a composed read must carry an ETag"
        assert etag_b != etag_a, (
            "two authenticated identities were served the SAME ETag for the same page "
            "-- a cross-reader 304 would then leak one reader's cached page to another"
        )
        cross = await client_b.get(_q(""), headers={"If-None-Match": etag_a})
        assert cross.status == 200, (
            "reader B echoing reader A's ETag was 304'd -- the validator is not keyed "
            "to the reader, so a cross-user cache hit is possible"
        )

    # (2) AUTH BEFORE 304. With the owner gate denying, a request that echoes a known
    # ETag is refused by the gate (403) and never reaches the conditional-GET branch,
    # so an unauthenticated / wrong-identity caller cannot ride a 304 shortcut past
    # auth. The recorded order shows the gate ran before the handler would answer.
    async def _deny(request, operation):
        return web.json_response({"error": "owner authorization required"}, status=403)

    async with _client_as("stranger", owner_gate=_deny) as (client_x, order):
        denied = await client_x.get(_q(""), headers={"If-None-Match": etag_a})
        assert denied.status == 403, (
            f"a non-owner echoing a valid ETag was answered {denied.status}, not 403 "
            "-- auth must gate before any 304 shortcut"
        )
        assert (
            "ETag" not in denied.headers and (await denied.read()) != b""
        ), "the denial must be the owner gate's 403 body, not a bodyless 304"
        # The owner gate ran before the handler reached its conditional-GET branch.
        assert order and order[0] == "auth" and "owner_gate" in order, order

    assert mod  # the route under test is this module's handler
