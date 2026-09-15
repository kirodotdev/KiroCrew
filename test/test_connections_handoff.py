"""The warm handoff: an unclaimed premint stays invisible, and Connect ADOPTS it.

The warm table and the per-caller Connect flow shared a row store but no handoff, so the
moment the gallery gained a premint caller three defects became reachable at once -- each
in a different seam, each independently enough to make warming useless.

* **NO ADOPTION.** ``api_connections_mint`` reserved a fresh row unconditionally, and
  ``reserve_mint_row`` pops WHATEVER row is there -- shared warm rows included -- so
  ``start_oauth_mint`` disposed the very URL the premint had minted for that click. Every
  Connect paid a cold spawn and the warm table's only product was thrown away by its
  intended consumer.
* **MISCLASSIFICATION.** ``_classify`` read any ``minting``/``waiting`` row as
  ``awaiting_consent``, and the page's ``waitingSlugs`` memo folds those slugs into its
  waiting set -- so one premint flipped EVERY mintable card to the in-flight-consent
  rendering with no user action at all.
* **THE POLL DESTROYED WHAT IT WENT LOOKING FOR.** ``expire_dead_holder`` judges a row by
  its own ``client``, which a warm row never owns (its verifier lives in the shared
  process), so ``_mint_holder_alive`` answered False and the first mint-state poll on a
  warm slug withdrew a perfectly redeemable URL.

Two axes come out of the fix and the tests below keep them apart, because conflating them
is what produced the defects: ``shared`` is OWNERSHIP -- an unclaimed premint anyone may
adopt -- while ``generation``/``activation`` is PROVENANCE, saying redeemability is judged
by the shared process rather than by a row-local client. Adoption clears the first and
keeps the second.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

# Both autouse resets, imported rather than re-spelled: the warm engine is a module
# singleton, so a test that leaves a runtime, a digest or a loop-bound lock on it is read
# by whichever test shares its worker. ``_clean_warm_runtime`` also swaps in a FRESH
# asyncio.Lock, because a lock binds to the loop that first waits on it.
from test_connections_warm import (  # noqa: F401 -- autouse fixtures
    _clean_mint_table,
    _clean_warm_runtime,
)

from kiro_crew.connections import mint, status, warm
from kiro_crew.connections.mint import _mints
from kiro_crew.connections.registry import Provider
from kiro_crew.dashboard.handlers import connections

_URL = "https://consent.example/authorize?state=abc"


def _provider(slug: str = "notion") -> Provider:
    return {  # type: ignore[typeddict-item]
        "slug": slug,
        "mcp_url": f"https://mcp.{slug}.test/mcp",
        "l0_expectations": {"dcr": True},
    }


class _Runtime:
    """A stand-in for one kiro-cli process, with the liveness answer we choose."""

    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def _warm_process(monkeypatch: pytest.MonkeyPatch, *, generation: int = 1, alive: bool = True):
    """Make ``generation`` (and activation 1) read live, or dead, to ``_warm_row_alive``."""
    monkeypatch.setattr(warm._warm_mint, "_generation", generation)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(alive))
    monkeypatch.setattr(warm._warm_mint, "_sessions", {1: object()} if alive else {})


def _row(
    slug: str = "notion",
    *,
    state: str = "waiting",
    url: str | None = _URL,
    shared: bool = True,
    generation: int = 1,
    activation: int = 1,
    token: str = "premint-token",
    watcher: Any = None,
) -> dict[str, Any]:
    """Install one warm row and return it. ``shared=False`` is the ADOPTED shape."""
    row: dict[str, Any] = {"state": state, "token": token, "started": 0.0}
    if shared:
        row["shared"] = True
    if generation:
        row["generation"] = generation
    if activation:
        row["activation"] = activation
    if url:
        row["oauth_url"] = url
    if watcher is not None:
        row["watcher"] = watcher
    _mints[slug] = row  # type: ignore[assignment]
    return row


async def _idle_watcher(slug: str, mcp_url: str, token: str) -> None:
    """A grant watcher that does nothing and finishes at once.

    The real one polls ``grant_observed``, which stats kiro-cli's OAuth cache, and it
    holds for a full mint TTL -- so a test that let adoption arm the real watcher both
    reached the filesystem and left a pending task behind for the loop teardown to
    complain about.
    """
    return None


@pytest.fixture(autouse=True)
def _no_real_watcher(monkeypatch: pytest.MonkeyPatch):
    """Adoption ARMS a watcher, so every test here would otherwise start a real one."""
    monkeypatch.setattr(warm, "_mint_watcher", _idle_watcher)


# ── part 1: an unclaimed premint is not user consent ──


def test_the_card_view_reports_an_unclaimed_premint_as_shared():
    """The classifier cannot refuse a verdict it is never told about.

    ``pending_mint_for`` is the single card-facing view and it feeds BOTH the status
    classifier and the mint-state poll, so the flag is carried rather than the row hidden:
    hiding it would make the poll answer ``idle``, which its own contract defines as "no
    mint exists for the provider" -- a lie about a row that does exist, and the reading the
    card takes as "nothing pending" right when it has just adopted one.
    """
    _row()

    view = mint.pending_mint_for("notion")

    assert view is not None
    assert view["state"] == "waiting"
    assert view.get("shared") is True


def test_an_unclaimed_premint_never_serves_its_consent_url():
    """A consent URL completes a MACHINE-WIDE grant, so it is served only to the
    caller whose click owns the row. An unclaimed premint belongs to nobody: its
    view says a row exists (``shared``) and nothing more -- the URL surfaces only
    after adoption rotates the token and clears ``shared``. Without this, any
    authenticated GET of the mint state could read a grant URL nobody asked for."""
    _row()

    view = mint.pending_mint_for("notion")

    assert view is not None
    assert view.get("shared") is True
    assert "oauth_url" not in view


def test_a_caller_owned_row_is_never_reported_as_shared():
    _row(shared=False)

    view = mint.pending_mint_for("notion")

    assert view is not None
    assert view.get("shared") is None


@pytest.mark.parametrize("state", ["minting", "waiting"])
def test_an_unclaimed_premint_never_reads_as_user_consent(state: str):
    """The defect: one premint flipped every mintable card to waiting-for-approval.

    A shared row is a URL nobody asked for yet, so the honest verdict is the one the card
    would get with no row at all -- which is why this falls through to the grant branches
    instead of inventing a third status the frontend has no rendering for.
    """
    assert status._classify(False, state, shared=True) == (status.STATUS_NOT_CONNECTED, "no_grant")


def test_an_unclaimed_premint_does_not_mask_an_unreadable_grant():
    """Falling through must reach the RIGHT branch: ``None`` is "could not look"."""
    assert status._classify(None, "waiting", shared=True) == (
        status.STATUS_NOT_CONNECTED,
        "grant_unreadable",
    )


@pytest.mark.parametrize("state", ["minting", "waiting"])
def test_a_caller_owned_mint_still_reads_as_awaiting_consent(state: str):
    """The regression guard on the other side: a real Connect must still show as waiting."""
    assert status._classify(False, state) == (status.STATUS_AWAITING_CONSENT, "mint_in_flight")


def test_a_granted_provider_outranks_an_unclaimed_premint():
    assert status._classify(True, "waiting", shared=True) == (
        status.STATUS_CONNECTED,
        "grant_present",
    )


@pytest.mark.asyncio
async def test_the_status_feed_reports_a_premintted_provider_as_not_connected(
    monkeypatch: pytest.MonkeyPatch,
):
    """End to end over the real collector, which is where the frontend memo reads."""
    monkeypatch.setattr(status, "get_visible_providers", lambda: [dict(_provider())])
    monkeypatch.setattr(status, "_provider_grant_presence", lambda url: False)
    monkeypatch.setattr(status, "reconcile_connected_since", lambda statuses, now: {})
    _row()

    statuses = await status.collect_connection_statuses()

    assert [entry["status"] for entry in statuses] == [status.STATUS_NOT_CONNECTED]


# ── part 1b: the poll must not destroy the row it went looking for ──


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [True, False], ids=["unclaimed", "adopted"])
async def test_the_mint_state_poll_never_withdraws_a_live_warm_row(
    monkeypatch: pytest.MonkeyPatch, shared: bool
):
    """``_mint_holder_alive`` must ABSTAIN on a warm row, not vote it dead.

    A warm row owns no ``client`` because its verifier lives in the shared process, so the
    ``client is None`` reading is a verdict rather than an abstention -- and
    ``expire_dead_holder`` acts on it, so the very first mint-state poll withdrew a
    redeemable URL. Withdrawal for these rows belongs to ``warm.expire_dead_mints``, which
    asks the generation/activation pair.
    """
    _warm_process(monkeypatch)
    _row(shared=shared)

    await mint.expire_dead_holder("notion")

    assert _mints["notion"]["state"] == "waiting"
    assert _mints["notion"]["oauth_url"] == _URL


@pytest.mark.asyncio
async def test_a_cold_row_whose_process_died_is_still_withdrawn_by_the_poll():
    """The abstention is scoped to warm provenance; the cold verdict is untouched."""
    _row(shared=False, generation=0, activation=0)

    await mint.expire_dead_holder("notion")

    assert _mints["notion"]["state"] == "expired"
    assert _mints["notion"]["reason"] == "mint_process_gone"


# ── part 2: adoption ──


@pytest.mark.asyncio
async def test_adoption_hands_over_the_preminted_url_under_a_fresh_token(
    monkeypatch: pytest.MonkeyPatch,
):
    _warm_process(monkeypatch)
    _row()

    token = await warm.adopt_shared_mint("notion", "https://mcp.notion.test/mcp")

    assert token and token != "premint-token"
    row = _mints["notion"]
    # The point of the whole slice: the URL survives the click that came for it.
    assert row["oauth_url"] == _URL
    assert row["state"] == "waiting"
    assert row["token"] == token
    # Ownership transferred; provenance kept, because the verifier did not move.
    assert not row.get("shared")
    assert row["generation"] == 1
    assert row["activation"] == 1


@pytest.mark.asyncio
async def test_adoption_refuses_a_row_whose_holding_process_is_gone(
    monkeypatch: pytest.MonkeyPatch,
):
    """Same liveness the reaper uses: a dead holder's URL is not adoptable, it is garbage."""
    _warm_process(monkeypatch, alive=False)
    _row()

    assert await warm.adopt_shared_mint("notion", "https://mcp.notion.test/mcp") is None
    # Left exactly as found, for ``expire_dead_mints`` to withdraw on its own terms.
    assert _mints["notion"].get("shared") is True
    assert _mints["notion"]["token"] == "premint-token"


@pytest.mark.asyncio
async def test_adoption_refuses_a_row_whose_session_no_longer_listens(
    monkeypatch: pytest.MonkeyPatch,
):
    """Redeemability takes TWO questions -- the process AND the loopback listener."""
    _warm_process(monkeypatch)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})
    _row()

    assert await warm.adopt_shared_mint("notion", "https://mcp.notion.test/mcp") is None


@pytest.mark.asyncio
async def test_adoption_refuses_a_row_no_premint_left_unclaimed(monkeypatch: pytest.MonkeyPatch):
    """A row a caller already owns is not up for grabs, however it was minted."""
    _warm_process(monkeypatch)
    _row(shared=False)

    assert await warm.adopt_shared_mint("notion", "https://mcp.notion.test/mcp") is None
    # Untouched: the owner's token still fences it.
    assert _mints["notion"]["token"] == "premint-token"


@pytest.mark.asyncio
async def test_adoption_refuses_a_claim_still_minting_even_with_a_url(
    monkeypatch: pytest.MonkeyPatch,
):
    """The state guard, pinned ALONE: every other conjunct is deliberately valid
    (URL present, holder alive), so this red-flags a dropped ``state == "waiting"``
    check specifically -- a row mid-claim is not handed over even if a URL is
    already visible on it."""
    _warm_process(monkeypatch)
    _row(state="minting")

    assert await warm.adopt_shared_mint("notion", "https://mcp.notion.test/mcp") is None
    # Left for its own watcher to finish; nothing rotated, nothing cleared.
    assert _mints["notion"]["token"] == "premint-token"
    assert _mints["notion"].get("shared") is True


@pytest.mark.asyncio
async def test_adoption_refuses_a_claim_that_holds_no_url_yet(monkeypatch: pytest.MonkeyPatch):
    """The URL guard, pinned ALONE: state is ``waiting`` and the holder is alive,
    so this red-flags a dropped ``oauth_url`` check specifically -- adopting a
    URL-less row would answer the click with nothing to redeem."""
    _warm_process(monkeypatch)
    _row(url=None)

    assert await warm.adopt_shared_mint("notion", "https://mcp.notion.test/mcp") is None


@pytest.mark.asyncio
async def test_adoption_refuses_a_provider_with_no_row_at_all(monkeypatch: pytest.MonkeyPatch):
    _warm_process(monkeypatch)

    assert await warm.adopt_shared_mint("notion", "https://mcp.notion.test/mcp") is None


@pytest.mark.asyncio
async def test_exactly_one_of_two_concurrent_adopters_wins(monkeypatch: pytest.MonkeyPatch):
    """Two tabs, one URL. The loser gets ``None`` and falls back to its own cold mint."""
    _warm_process(monkeypatch)
    _row()

    results = await asyncio.gather(
        warm.adopt_shared_mint("notion", "https://mcp.notion.test/mcp"),
        warm.adopt_shared_mint("notion", "https://mcp.notion.test/mcp"),
    )

    assert sorted(result is None for result in results) == [False, True]
    winner = next(result for result in results if result is not None)
    assert _mints["notion"]["token"] == winner


@pytest.mark.asyncio
async def test_adoption_rearms_the_grant_watcher_under_the_new_token(
    monkeypatch: pytest.MonkeyPatch,
):
    """Rotating the token without rotating the watcher would leave the row unwatchable.

    Every write in ``_mint_watcher`` is fenced on the token it was started with, so the
    premint's watcher goes inert the moment the token changes: nothing would ever flip the
    row to ``granted`` or expire it, and it would keep the shared process resident forever.
    """
    _warm_process(monkeypatch)
    armed: list[tuple[str, str, str]] = []

    async def _recording_watcher(slug: str, mcp_url: str, token: str) -> None:
        armed.append((slug, mcp_url, token))

    monkeypatch.setattr(warm, "_mint_watcher", _recording_watcher)
    stale = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    _row(watcher=stale)

    token = await warm.adopt_shared_mint("notion", "https://mcp.notion.test/mcp")

    await asyncio.sleep(0)
    assert stale.cancelled() or stale.cancelling()
    assert armed == [("notion", "https://mcp.notion.test/mcp", token)]
    assert _mints["notion"]["watcher"] is not stale


# ── part 2b: an adopted row still belongs to the warm table's lifecycle ──


def test_an_adopted_row_still_keeps_its_generation_alive():
    """Killing the process the user is mid-consent on is the worst regression available.

    Every warm-side predicate keyed on ``shared``, so clearing it at adoption -- which
    ownership demands -- silently took the adopted row out of every count that keeps its
    process parked.
    """
    _row(shared=False, generation=3)

    assert warm._generation_holds_live_rows(3) is True


def test_an_adopted_row_still_holds_the_session_answering_its_redirect():
    _row(shared=False, generation=3, activation=7)

    assert warm._activations_in_use() == {7}


def test_an_adopted_row_keeps_the_shared_process_from_being_retired():
    _row(shared=False, generation=3)

    assert warm._shared_mints_pending() is True


@pytest.mark.asyncio
async def test_an_adopted_row_is_withdrawn_when_its_holder_dies(monkeypatch: pytest.MonkeyPatch):
    """The other half of the abstention above: someone still has to say no."""
    _warm_process(monkeypatch, alive=False)
    _row(shared=False)

    assert await warm.expire_dead_mints() == ["notion"]
    assert _mints["notion"]["state"] == "expired"


@pytest.mark.asyncio
async def test_an_adopted_row_is_expired_when_its_generation_is_retired():
    _row(shared=False, generation=3)

    assert await warm._expire_shared_mints("mint_process_gone", generation=3) == ["notion"]
    assert _mints["notion"]["state"] == "expired"


@pytest.mark.asyncio
async def test_a_premint_never_displaces_a_row_a_caller_already_adopted():
    """``_mint_is_cold_held`` cannot see an adopted row: it owns no ``client`` either."""
    _row(shared=False)

    claimed, displaced = await warm._claim_shared_mints(["notion"])

    assert claimed == {} and displaced == []
    assert _mints["notion"]["token"] == "premint-token"
    assert _mints["notion"]["oauth_url"] == _URL


@pytest.mark.asyncio
async def test_a_premint_still_replaces_an_unclaimed_row_of_its_own():
    """The guard is about CALLER ownership, not about warm rows being untouchable."""
    _row()

    claimed, displaced = await warm._claim_shared_mints(["notion"])

    assert list(claimed) == ["notion"]
    assert [row["token"] for row in displaced] == ["premint-token"]


# ── part 3: the HTTP surface ──


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_post("/api/connections/mint", connections.api_connections_mint)
    app.router.add_get("/api/connections/mint", connections.api_connections_mint_state)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
def cold_mint(monkeypatch: pytest.MonkeyPatch):
    """Record every cold-path call the handler makes, and make none of them real."""
    calls: dict[str, int] = {"reserved": 0, "spawned": 0, "disposed": 0}

    async def _reserve(slug: str):
        calls["reserved"] += 1
        prior = _mints.pop(slug, None)
        _mints[slug] = {"state": "minting", "started": 0.0, "token": "cold-token"}
        return "cold-token", prior

    async def _start(slug, mcp_url, token=None, prior=None):
        calls["spawned"] += 1
        if prior is not None:
            calls["disposed"] += 1

    async def _dispose(entry):
        calls["disposed"] += 1

    monkeypatch.setattr(mint, "reserve_mint_row", _reserve)
    monkeypatch.setattr(mint, "start_oauth_mint", _start)
    monkeypatch.setattr(mint, "_dispose_mint", _dispose)
    monkeypatch.setattr(connections, "get_provider", lambda slug: _provider(slug))
    return calls


@pytest.mark.asyncio
async def test_connect_adopts_the_preminted_url_instead_of_spawning_a_cold_mint(
    monkeypatch: pytest.MonkeyPatch, cold_mint: dict[str, int]
):
    """THE defect: the click the premint existed for was the click that threw it away."""
    _warm_process(monkeypatch)
    _row()
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"slug": "notion"})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["state"] == "waiting"
    assert body["token"] == _mints["notion"]["token"]
    # No cold spawn, and nothing displaced the row: the URL is still there to serve.
    assert cold_mint == {"reserved": 0, "spawned": 0, "disposed": 0}
    assert _mints["notion"]["oauth_url"] == _URL


@pytest.mark.asyncio
async def test_connect_falls_back_to_a_cold_mint_when_nothing_is_adoptable(
    monkeypatch: pytest.MonkeyPatch, cold_mint: dict[str, int]
):
    _warm_process(monkeypatch, alive=False)
    _row()
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"slug": "notion"})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["state"] == "minting"
    assert body["token"] == "cold-token"
    assert cold_mint["reserved"] == 1 and cold_mint["spawned"] == 1


@pytest.mark.asyncio
async def test_connect_does_not_redrain_a_stale_authorization_shape(
    monkeypatch: pytest.MonkeyPatch, cold_mint: dict[str, int]
):
    class _StaleHandle:
        def __init__(self) -> None:
            self.pops = 0

        def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
            self.pops += 1
            return [{"serverName": "notion", "oauthUrl": _URL}]

    current = _provider()
    current["recommended_scopes"] = ["write"]
    activated = _provider()
    activated["recommended_scopes"] = ["read"]
    monkeypatch.setattr(connections, "get_provider", lambda slug: current)
    monkeypatch.setattr(warm, "get_visible_providers", lambda: [current])
    monkeypatch.setattr(warm, "list_servers", lambda: [])
    monkeypatch.setattr(warm, "grant_present", lambda url: False)
    monkeypatch.setattr(warm, "_read_agent_spec", lambda *args, **kwargs: {})
    _warm_process(monkeypatch)
    handle = _StaleHandle()
    session = warm._WarmSession(
        generation=1,
        handle=handle,
        expires_at=float("inf"),
        settled=True,
        providers=(activated,),
    )
    monkeypatch.setattr(warm._warm_mint, "_sessions", {1: session})
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"slug": "notion"})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["state"] == "minting"
    assert body["token"] == "cold-token"
    assert handle.pops == 0, "a stale session must not be read"
    assert cold_mint["reserved"] == 1 and cold_mint["spawned"] == 1


@pytest.mark.asyncio
async def test_connect_redrains_the_live_warm_session_before_spawning_cold(
    monkeypatch: pytest.MonkeyPatch, cold_mint: dict[str, int]
):
    class _LateHandle:
        def __init__(self) -> None:
            self.drains = 0
            self._pending: list[dict[str, str]] = []

        def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
            pending, self._pending = self._pending, []
            return pending

        async def drain_init(self, **kwargs: Any) -> None:
            self.drains += 1
            self._pending.append({"serverName": "notion", "oauthUrl": _URL})

    provider = _provider()
    monkeypatch.setattr(warm, "get_visible_providers", lambda: [provider])
    monkeypatch.setattr(warm, "list_servers", lambda: [])
    monkeypatch.setattr(warm, "grant_present", lambda url: False)
    monkeypatch.setattr(warm, "_read_agent_spec", lambda *args, **kwargs: {})
    _warm_process(monkeypatch)
    handle = _LateHandle()
    session = warm._WarmSession(
        generation=1,
        handle=handle,
        expires_at=float("inf"),
        settled=True,
        providers=(provider,),
    )
    monkeypatch.setattr(warm._warm_mint, "_sessions", {1: session})
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"slug": "notion"})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["state"] == "waiting"
    assert body["token"] == _mints["notion"]["token"]
    assert handle.drains == 1
    assert cold_mint == {"reserved": 0, "spawned": 0, "disposed": 0}
    assert _mints["notion"]["oauth_url"] == _URL


@pytest.mark.asyncio
async def test_connect_redrain_does_not_publish_a_sibling_challenge(
    monkeypatch: pytest.MonkeyPatch, cold_mint: dict[str, int]
):
    class _SiblingHandle:
        def __init__(self) -> None:
            self._pending: list[dict[str, str]] = []

        def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
            pending, self._pending = self._pending, []
            return pending

        async def drain_init(self, **kwargs: Any) -> None:
            self._pending.extend(
                [
                    {"serverName": "notion", "oauthUrl": _URL},
                    {"serverName": "linear", "oauthUrl": "https://linear.test/authorize"},
                ]
            )

    notion, linear = _provider("notion"), _provider("linear")
    monkeypatch.setattr(warm, "get_visible_providers", lambda: [notion])
    monkeypatch.setattr(warm, "list_servers", lambda: [])
    monkeypatch.setattr(warm, "grant_present", lambda url: False)
    monkeypatch.setattr(warm, "_read_agent_spec", lambda *args, **kwargs: {})
    _warm_process(monkeypatch)
    session = warm._WarmSession(
        generation=1,
        handle=_SiblingHandle(),
        expires_at=float("inf"),
        settled=True,
        providers=(notion, linear),
    )
    monkeypatch.setattr(warm._warm_mint, "_sessions", {1: session})
    client = await _client()
    try:
        response = await client.post("/api/connections/mint", json={"slug": "notion"})
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert body["state"] == "waiting"
    assert _mints["notion"]["oauth_url"] == _URL
    assert "linear" not in _mints, "a sibling challenge was not validated for this Connect"
    assert cold_mint == {"reserved": 0, "spawned": 0, "disposed": 0}


@pytest.mark.asyncio
async def test_the_premint_then_connect_round_trip_serves_the_warm_url(
    monkeypatch: pytest.MonkeyPatch, cold_mint: dict[str, int]
):
    """premint -> not_connected -> Connect -> the warm URL, over the real endpoints.

    The whole slice in one pass, in the order the page performs it. Each step is the one a
    separate defect broke: the status read must not flip the card, the POST must adopt, and
    the poll must both survive and serve.
    """
    _warm_process(monkeypatch)
    monkeypatch.setattr(status, "get_visible_providers", lambda: [dict(_provider())])
    monkeypatch.setattr(status, "_provider_grant_presence", lambda url: False)
    monkeypatch.setattr(status, "reconcile_connected_since", lambda statuses, now: {})
    # What ``warm_mint_all`` leaves behind once an activation has absorbed its challenges.
    _row()

    # 1. The gallery renders. The premint is held, and no card claims to be mid-consent.
    statuses = await status.collect_connection_statuses()
    assert [entry["status"] for entry in statuses] == [status.STATUS_NOT_CONNECTED]

    client = await _client()
    try:
        # 2. The user clicks Connect, which adopts rather than re-mints.
        started = await (await client.post("/api/connections/mint", json={"slug": "notion"})).json()
        # 3. The card's ordinary mint-state poll finds the warm URL immediately.
        polled = await (await client.get("/api/connections/mint?slug=notion")).json()
    finally:
        await client.close()

    assert cold_mint["spawned"] == 0
    assert polled["state"] == "waiting"
    assert polled["oauth_url"] == _URL
    assert polled["token"] == started["token"]
    # Ownership actually moved in the TABLE, which is what stops the next premint sweep
    # reclaiming the row and stops the status feed reporting it as unclaimed. The wire
    # payload deliberately does not carry the flag: no card consumes it, and the
    # authorization verdict is the status feed's job.
    assert not _mints["notion"].get("shared")
