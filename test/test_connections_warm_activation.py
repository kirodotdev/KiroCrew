"""Warm-mint tests: one activation fills the row table, and what it may serve.

Servability of a shrunk or reordered candidate set, which providers are activation candidates,
one activation stamping every claimed row, row identity across activations at one slug, the
credential screen before a URL is stored, a cancel anywhere between claim and activation
releasing every claimed row, reuse never mounting an excluded provider, and an unreadable grant
cache read as indeterminate rather than absent.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
from pathlib import Path
from typing import Any

import connections_warm_helpers
import pytest
from connections_warm_helpers import _claim, _provider, _result, _returns, _Runtime, _SessionHandle
from oauth_url_corpus import OPERATOR_EXTENSION_OAUTH_URLS

from kiro_crew import security
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.connections import mint, warm
from kiro_crew.connections.mint import _mints

# The shared fixtures, bound here by name so pytest registers them for this module.
_agents_dir = connections_warm_helpers._agents_dir
_clean_mint_table = connections_warm_helpers._clean_mint_table
_clean_warm_runtime = connections_warm_helpers._clean_warm_runtime
_stub_activation = connections_warm_helpers._stub_activation


# ── servability: a set that SHRANK is still servable ──


def _plan(entries: dict[str, dict[str, Any]]) -> warm._WarmSpecPlan:
    return warm._WarmSpecPlan(
        all_agent="all" if entries else "",
        specs={},
        entries=entries,
        digest=repr(sorted(entries.items())),
    )


def test_a_shrunk_candidate_set_is_served_by_the_running_process():
    resident = _plan({"linear": {"url": "https://l"}, "vercel": {"url": "https://v"}})
    assert warm._plan_is_servable(resident, _plan({"linear": {"url": "https://l"}})) is True


def test_a_changed_authorization_ask_is_not_servable():
    resident = _plan({"linear": {"url": "https://l"}})
    assert warm._plan_is_servable(resident, _plan({"linear": {"url": "https://other"}})) is False
    assert warm._plan_is_servable(resident, _plan({"notion": {"url": "https://n"}})) is False


def test_a_process_that_enumerated_nothing_serves_nothing():
    assert warm._plan_is_servable(_plan({}), _plan({"linear": {"url": "https://l"}})) is False


# ── candidates: a granted provider is warmed into the spec but asked for no URL ──


def test_a_granted_provider_is_not_an_activation_candidate(monkeypatch: pytest.MonkeyPatch):
    universe = [_provider("granted"), _provider("fresh")]
    monkeypatch.setattr(warm, "grant_present", lambda url: "granted" in url)
    assert [p["slug"] for p in warm._warm_activation_candidates(universe)] == ["fresh"]


def test_an_unreadable_inventory_warms_nothing_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        warm, "warm_spec_providers", lambda: (_ for _ in ()).throw(OSError("config unreadable"))
    )
    assert warm._warm_candidate_scan() == ([], [])


def test_a_warm_session_never_injects_mcp_servers():
    """A remote server through ``session/new`` kills the process and every verifier."""
    assert warm._warm_session_mcp_servers() == []


# ── one activation fills the whole table ──


@pytest.mark.asyncio
async def test_one_activation_stamps_every_row_with_its_generation_and_activation(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    providers = [_provider("linear"), _provider("vercel")]
    monkeypatch.setattr(
        warm,
        "_warm_activate",
        _returns(
            _result(
                providers,
                [
                    {"serverName": "linear", "oauthUrl": "https://l/consent"},
                    {"serverName": "vercel", "oauthUrl": "https://v/consent"},
                ],
            )
        ),
    )

    minted = await warm.warm_mint_all(providers)

    assert sorted(minted) == ["linear", "vercel"]
    for slug in ("linear", "vercel"):
        row = _mints[slug]
        assert row["state"] == "waiting"
        assert row["generation"] == 7 and row["activation"] == 3
        assert row["shared"] is True
        # Every row carries the cold engine's row identity, which is what the grant
        # watcher re-checks before it writes a verdict.
        assert row["token"]
    assert len({_mints["linear"]["token"], _mints["vercel"]["token"]}) == 2


@pytest.mark.asyncio
async def test_a_claim_the_activation_did_not_cover_is_released_not_left_minting(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    claimed = [_provider("linear"), _provider("stranded")]
    monkeypatch.setattr(
        warm,
        "_warm_activate",
        _returns(
            _result([claimed[0]], [{"serverName": "linear", "oauthUrl": "https://l/consent"}])
        ),
    )

    assert await warm.warm_mint_all(claimed) == ["linear"]
    assert "stranded" not in _mints, "an uncovered claim must not sit in the table forever"


@pytest.mark.asyncio
async def test_a_failed_activation_releases_every_claim(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    monkeypatch.setattr(warm, "_warm_activate", _returns(None))
    assert await warm.warm_mint_all([_provider("linear")]) == []
    assert _mints == {}


@pytest.mark.asyncio
async def test_a_cold_held_row_is_never_replaced_by_a_warm_claim():
    _mints["linear"] = {"state": "waiting", "client": object(), "oauth_url": "https://cold"}
    assert await warm._claim_shared_mints(["linear"]) == ({}, [])
    assert _mints["linear"]["oauth_url"] == "https://cold"


@pytest.mark.asyncio
async def test_expiry_is_narrowed_to_the_one_generation_whose_verifier_died():
    """Withdrawal follows the verifier, so it follows the generation. A row on any OTHER
    generation is redeemable on a process this expiry is not about."""
    _mints["a"] = {"state": "waiting", "shared": True, "generation": 1, "token": "tok-a"}
    _mints["b"] = {"state": "waiting", "shared": True, "generation": 2, "token": "tok-b"}
    # A claim not yet stamped with a generation belongs to an activation still in flight.
    _mints["c"] = {"state": "minting", "shared": True, "token": "tok-c"}
    assert await warm._expire_shared_mints("mint_process_gone", generation=1) == ["a"]
    assert _mints["b"]["state"] == "waiting"
    assert _mints["c"]["state"] == "minting"


# ── defect: a batch timestamp is not a row identity ──
#
# The fence separating one activation's rows from the next at the SAME slug was
# ``entry["started"] == started``, a ``time.monotonic()`` reading taken once per
# ``warm_mint_all`` call. ``_new_mint_token``'s own docstring records why that is not a row
# identity: the clock has ~15.6ms granularity on Windows, so two Connects for one provider
# inside a single tick read as the same row and every guard built on it fails OPEN.


@pytest.mark.asyncio
async def test_a_late_absorb_does_not_write_its_url_over_a_newer_claim(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """Two overlapping activations at one slug. The first one's absorb lands last, and it
    must not replace the second's claim -- the URL it carries belongs to a session the
    second Connect is not waiting on."""
    # A coarse clock, as Windows has: both claims are taken inside one tick, so a fence
    # reading ``started`` cannot tell the two rows apart.
    monkeypatch.setattr(warm.time, "monotonic", lambda: 100.0)
    provider = _provider("linear")

    first = await _claim("linear")
    second, displaced = await warm._claim_shared_mints(["linear"])
    assert first and second
    assert first["linear"] != second["linear"], "each claim needs its own row identity"
    assert [row["token"] for row in displaced] == [
        first["linear"]
    ], "the row the second claim replaced comes back for the caller to dispose"

    stale = _result(
        [provider], [{"serverName": "linear", "oauthUrl": "https://stale/consent"}], activation=1
    )
    assert await warm._absorb_warm_requests(stale, first) == []
    row = _mints["linear"]
    assert row.get("oauth_url") != "https://stale/consent"
    assert row["state"] == "minting", "the newer claim is still the activation's to fill"
    assert row["token"] == second["linear"]


@pytest.mark.asyncio
async def test_a_release_leaves_a_newer_claim_at_the_same_slug_alone(
    monkeypatch: pytest.MonkeyPatch,
):
    """The release path needs the same identity: dropping the row a newer activation is
    filling would strand that Connect on a card with no claim and no URL."""
    monkeypatch.setattr(warm.time, "monotonic", lambda: 100.0)
    first = await _claim("linear")
    second, _ = await warm._claim_shared_mints(["linear"])

    await warm._release_shared_claims(first)

    assert _mints["linear"]["token"] == second["linear"]


# ── defect: a credential-bearing URL must be refused before it is stored ──


@pytest.mark.asyncio
async def test_a_url_carrying_a_credential_is_refused_rather_than_stored(_stub_activation):
    """The same gate the cold mint and the chat consent banner apply. A refused URL is
    never written to the row, so nothing can serve it to a card."""
    provider = _provider("linear")
    claims = await _claim("linear")
    assert claims

    bearing = _result(
        [provider],
        [
            {
                "serverName": "linear",
                "oauthUrl": "https://linear.example/authorize?state=AKIA" + "IOSFODNN7EXAMPLE1",
            }
        ],
    )
    assert await warm._absorb_warm_requests(bearing, claims) == []
    assert "linear" not in _mints, "the claim is released so the card asks for a fresh mint"


@pytest.mark.asyncio
async def test_a_refused_premint_stays_slug_only_and_hands_naming_to_the_cold_mint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, _stub_activation
):
    """The warm gate is one half of a two-step surface. It must NOT name the endpoint
    itself -- the warning and the audit line are logger sinks, and URL-derived text there
    is what the credential-disclosure scanners flag -- but the URL it refuses must be one
    the cold mint CAN name, or releasing the claim would send the card to a fresh mint that
    ends just as opaquely. Both halves are pinned here: the release leaves no row (so the
    card's poll reads ``idle`` and asks for a cold mint), the warm-side text carries the
    slug and nothing from the URL, and the display contract the cold mint applies to the
    same URL yields the copy-ready endpoint."""
    # The operator-extension corpus: rejected for a long opaque state at an
    # endpoint outside the allowlist, clean once the endpoint is added -- the
    # one shape whose cold-mint card must name the endpoint.
    _, url, (host, path) = OPERATOR_EXTENSION_OAUTH_URLS[0]
    audited: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        warm, "_log_warm_event", lambda op, res, outcome="ok": audited.append((op, res, outcome))
    )
    provider = _provider("linear")
    claims = await _claim("linear")

    with caplog.at_level(logging.WARNING, logger=warm.logger.name):
        absorbed = await warm._absorb_warm_requests(
            _result([provider], [{"serverName": "linear", "oauthUrl": url}]), claims
        )

    assert absorbed == []
    assert "linear" not in _mints
    assert mint.pending_mint_for("linear") is None
    # Slug-only on every warm-side channel.
    assert audited == [("connections_warm_mint_url", "provider:linear", "refused")]
    assert "linear" in caplog.text
    for never in (host, path, "a1B2c3D4", url):
        assert never not in caplog.text
        assert never not in json.dumps(audited)
    # The hand-off is coherent: the cold mint the card now starts re-hits this URL,
    # and its card view names exactly this endpoint (host+path, nothing from the query)
    # -- because this is a rejection the allowlist entry would clear.
    assert security.oauth_url_contains_credential(url)
    assert security.oauth_rejection_is_endpoint_exemptible(url)
    assert security.sanitized_oauth_endpoint_display(url) == f"{host}{path}"


@pytest.mark.asyncio
async def test_the_screen_is_the_shared_security_gate_not_a_local_copy(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """A second implementation of the predicate would drift from the one the rest of the
    product enforces, so the module must call through to security's."""
    assert warm.oauth_url_contains_credential is security.oauth_url_contains_credential

    monkeypatch.setattr(warm, "oauth_url_contains_credential", lambda url: True)
    claims = await _claim("linear")
    plain = _result(
        [_provider("linear")], [{"serverName": "linear", "oauthUrl": "https://l/consent"}]
    )
    assert await warm._absorb_warm_requests(plain, claims) == []


@pytest.mark.asyncio
async def test_a_clean_url_still_lands_on_the_row(_stub_activation):
    """The screen must not refuse the ordinary case it exists to let through."""
    claims = await _claim("linear")
    clean = _result(
        [_provider("linear")],
        [{"serverName": "linear", "oauthUrl": "https://linear.example/oauth/authorize?state=xyz"}],
    )
    assert await warm._absorb_warm_requests(clean, claims) == ["linear"]
    assert _mints["linear"]["state"] == "waiting"


# ── defect: a cancel between claim and activation strands every claimed row ──


@pytest.mark.asyncio
async def test_a_cancel_between_claim_and_activation_releases_every_row(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """``minting`` rows are invisible to ``expire_dead_mints`` (it judges ``waiting`` only)
    and they keep ``_shared_mints_pending`` true, so a row left behind here is never
    withdrawn AND the process is never retired."""

    async def _cancelled(*, slug: str = ""):
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_warm_activate", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await warm.warm_mint_all([_provider("linear"), _provider("vercel")])

    assert _mints == {}, "a cancelled activation must not leave a claim minting with no watcher"
    assert warm._shared_mints_pending() is False


@pytest.mark.asyncio
async def test_a_real_task_cancel_mid_activation_still_completes_the_release(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """The stronger form: not a raised ``CancelledError`` but a real ``Task.cancel()`` while
    the coroutine is suspended. The cleanup awaits a lock and a dispose, so this is what
    proves those awaits run rather than being interrupted a second time."""
    entered = asyncio.Event()

    async def _hangs(*, slug: str = ""):
        entered.set()
        await asyncio.Event().wait()  # never completes; the cancel lands here

    monkeypatch.setattr(warm, "_warm_activate", _hangs)

    task = asyncio.get_running_loop().create_task(warm.warm_mint_all([_provider("linear")]))
    await entered.wait()
    assert _mints["linear"]["state"] == "minting", "the claim is taken before the activation"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _mints == {}
    assert warm._shared_mints_pending() is False


@pytest.mark.asyncio
async def test_a_cancel_after_the_activation_lands_releases_the_rows_too(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """The window does not close when the activation returns: an absorb interrupted
    mid-flight leaves the same orphaned claims."""

    async def _cancelling_absorb(result, claims):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        warm,
        "_warm_activate",
        _returns(
            _result(
                [_provider("linear")], [{"serverName": "linear", "oauthUrl": "https://l/consent"}]
            )
        ),
    )
    monkeypatch.setattr(warm, "_absorb_warm_requests", _cancelling_absorb)

    with pytest.raises(asyncio.CancelledError):
        await warm.warm_mint_all([_provider("linear")])

    assert _mints == {}


# ── defect: the claim loop's own await is a cancel window OUTSIDE the protected region ──
#
# ``warm_mint_all`` takes its claims BEFORE entering the try whose ``except BaseException``
# rolls them back, so any await inside the claim loop is unprotected. Replacing an existing
# row awaits ``_dispose_mint``, which suspends on a client teardown and again on the
# shielded spec removal in its ``finally`` -- so a cancellation there unwinds with earlier
# slugs already installed as ``minting`` and no caller holding their tokens.


@pytest.mark.asyncio
async def test_a_cancel_while_replacing_a_row_does_not_strand_the_earlier_claims(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """``vercel`` is claimed first and needs no dispose; ``linear`` already holds a row, so
    replacing it is the await the cancellation lands in. ``vercel`` must not survive it."""
    real_dispose = warm._dispose_mint

    async def _cancel_on_the_displaced_row(entry):
        # Only the row being REPLACED carries a URL; our own fresh claims do not, so the
        # rollback's own disposes still run for real.
        if entry.get("oauth_url"):
            raise asyncio.CancelledError()
        await real_dispose(entry)

    monkeypatch.setattr(warm, "_dispose_mint", _cancel_on_the_displaced_row)
    _mints["linear"] = {
        "state": "waiting",
        "shared": True,
        "oauth_url": "https://l/consent",
        "generation": 5,
        "activation": 1,
        "token": "older-linear-row",
    }

    with pytest.raises(asyncio.CancelledError):
        await warm.warm_mint_all([_provider("vercel"), _provider("linear")])

    assert _mints == {}, "an interrupted claim must not leave a row minting with no owner"
    # `minting` rows are invisible to expire_dead_mints and keep the pending check true, so
    # a survivor here is never withdrawn AND stops the process from ever being retired.
    assert warm._shared_mints_pending() is False


@pytest.mark.asyncio
async def test_the_claim_loop_itself_contains_no_await():
    """The structural invariant behind the test above: with no await between the first row
    being installed and the loop ending, there is no window to be cancelled in."""
    body = ast.parse(inspect.getsource(warm._claim_shared_mints)).body[0]
    loops = [node for node in ast.walk(body) if isinstance(node, ast.For)]
    assert loops, "guard is reading the wrong tree"
    offenders = [
        type(node).__name__
        for loop in loops
        for node in ast.walk(loop)
        if isinstance(node, ast.Await)
    ]
    assert not offenders, f"awaits inside the claim loop: {offenders} -- move them out"


# ── defect: reuse activated the RESIDENT plan, which still names excluded providers ──
#
# Servability deliberately reuses a process whose spec set is a SUPERSET of what the current
# scan wants, so a shrink does not strand a peer's listeners. But the bulk activation names
# the resident ALL-AGENT mode, and a mode's mounted server set is fixed by the spec it
# carried at spawn -- specs are enumerated ONCE, and the wanted subset cannot travel through
# `session/new` because injected servers kill the process. So `set_mode` initializes the
# excluded provider's MCP server and an authorization request goes out for exactly the
# provider `_warm_mintable_entry` vetoed. Filtering the activation's RESULT leaves that
# request made, which is why these tests assert what the process MOUNTS, not what the mint
# returns.


def _divergent_config(agents: Path, alias: str) -> None:
    """The user's own agent spec, asking for a DIFFERENT endpoint than the registry does."""
    (agents / AGENT_FILENAME).write_text(
        json.dumps({"mcpServers": {alias: {"url": "https://elsewhere.example/mcp"}}}),
        encoding="utf-8",
    )


class _SpecEnumeratingRuntime:
    """A process that mounts what kiro-cli mounts: the specs on disk AT SPAWN, by name.

    kiro-cli enumerates the agents directory once at spawn and ``set_mode`` then activates one
    of THOSE modes, so the servers an activation initializes are the ones the named spec listed
    when the process started -- not whatever the file says later. Modelling that is the whole
    point: a fake that re-read the spec at ``create_session`` time would make the defect these
    tests exist for invisible.
    """

    def __init__(self, agents_dir: Path, mounted: list[tuple[str, frozenset[str]]], **kwargs: Any):
        self._agents_dir = agents_dir
        self._mounted = mounted
        self._modes: dict[str, frozenset[str]] = {}

    async def spawn(self, start_priority=None) -> None:
        for path in self._agents_dir.glob("*.json"):
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            self._modes[path.stem] = frozenset((body.get("mcpServers") or {}))

    def is_alive(self) -> bool:
        return True

    async def create_session(self, *, agent: str, mcp_servers: list[Any]) -> Any:
        assert mcp_servers == [], "a warm session must inject no servers"
        # What `set_mode` would initialize: the roster this mode carried at spawn.
        self._mounted.append((agent, self._modes.get(agent, frozenset())))
        return _SessionHandle([])


@pytest.fixture
def _mount_spy(monkeypatch: pytest.MonkeyPatch, _agents_dir: Path):
    """Every activation's ``(mode, mounted servers)``, observed at the spawn/set_mode seam."""
    mounted: list[tuple[str, frozenset[str]]] = []

    def _factory():
        def _build(**kwargs: Any) -> _SpecEnumeratingRuntime:
            work_dir = Path(kwargs["work_dir"])
            return _SpecEnumeratingRuntime(
                warm._warm_generation_agents_dir(work_dir),
                mounted,
                **kwargs,
            )

        return _build

    monkeypatch.setattr(warm, "_acp_runtime_factory", _factory)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 3600)
    monkeypatch.setattr(warm, "_WARM_OAUTH_SETTLE_ROUNDS", 1)
    return mounted


async def _spawned_on(agents_dir: Path, plan: warm._WarmSpecPlan, mounted: list[Any]) -> Any:
    """A live process that enumerated ``plan``'s specs, the way a resident one did."""
    warm._write_warm_mint_specs(plan, agents_dir)
    runtime = _SpecEnumeratingRuntime(agents_dir, mounted)
    await runtime.spawn()
    return runtime


@pytest.mark.asyncio
async def test_an_excluded_provider_is_never_mounted_by_a_bulk_activation(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path, _mount_spy: list[Any]
):
    """THE test: assert what the activation INITIALIZES, not what the mint returns.

    RED before the fix -- the resident all-agent mode was reused, so `notion`'s MCP server
    was still mounted and challenged for even though its URL was discarded."""
    linear, notion = _provider("linear"), _provider("notion")
    resident = warm._warm_spec_plan([linear, notion])
    assert set(resident.entries) == {"linear", "notion"}
    stale = await _spawned_on(_agents_dir, resident, _mount_spy)
    _mount_spy.clear()

    # notion's configured entry now diverges, which vetoes it in every fresh plan.
    _divergent_config(_agents_dir, "notion")

    async def _kills(runtime: Any) -> bool:
        return True

    monkeypatch.setattr(warm, "_kill_quietly", _kills)
    monkeypatch.setattr(warm, "_warm_candidate_scan", lambda: ([linear, notion], [linear, notion]))
    monkeypatch.setattr(warm._warm_mint, "_runtime", stale)
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_plan", resident)
    monkeypatch.setattr(warm._warm_mint, "_digest", resident.digest)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    result = await warm._warm_mint.mint_for()

    assert result is not None
    assert len(_mount_spy) == 1, "exactly one activation"
    mode, servers = _mount_spy[0]
    assert mode == warm._WARM_ALL_AGENT
    assert servers == frozenset({"linear"}), "the vetoed provider's server was initialized"
    assert [provider["slug"] for provider in result.providers] == ["linear"]


@pytest.mark.asyncio
async def test_a_shrink_parks_a_process_a_card_still_needs_rather_than_killing_it(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path, _mount_spy: list[Any]
):
    """The other half of the constraint: re-mounting an exact roster must not strand a peer.

    A process holding a redeemable code is PARKED, keeps its generation live, and the drain
    retires it once its rows are gone -- so the excluded provider stops being mounted without
    any card losing its URL."""
    linear, notion = _provider("linear"), _provider("notion")
    resident = warm._warm_spec_plan([linear, notion])
    stale = await _spawned_on(_agents_dir, resident, _mount_spy)
    _mount_spy.clear()
    _divergent_config(_agents_dir, "notion")

    killed: list[Any] = []

    async def _record_kill(runtime: Any) -> bool:
        killed.append(runtime)
        return True

    # A peer is mid-consent on the resident generation, so killing it would strand its URL.
    _mints["vercel"] = {"state": "waiting", "shared": True, "generation": 4, "token": "t"}

    monkeypatch.setattr(warm, "_kill_quietly", _record_kill)
    monkeypatch.setattr(warm, "_warm_candidate_scan", lambda: ([linear, notion], [linear, notion]))
    monkeypatch.setattr(warm._warm_mint, "_runtime", stale)
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_plan", resident)
    monkeypatch.setattr(warm._warm_mint, "_digest", resident.digest)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    assert await warm._warm_mint.mint_for() is not None

    assert killed == [], "a process a card is still mid-consent on must be parked, not killed"
    assert [generation for generation, _ in warm._warm_mint._retiring] == [4]
    assert warm._warm_mint.generation_is_live(4) is True, "the parked URL must stay redeemable"
    assert _mount_spy[0][1] == frozenset({"linear"})


@pytest.mark.asyncio
async def test_a_roster_that_only_changed_ORDER_reuses_the_process_instead_of_parking_it(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """The reuse branch's surviving case, now that the slug-scoped modes are gone.

    The digest is order-sensitive (a spec's ``tools`` is a LIST built in scan order), so a
    registry that hands the same providers back in a different order produces a different
    digest for an IDENTICAL roster. Only the two predicates catch that: servability says the
    resident process can answer for everything wanted, and
    ``_resident_roster_is_asked_for`` says it mounts nothing extra. Without them a bare
    reorder would park a live process and respawn for no gain.

    This is also the guard on the tempting simplification: collapsing the pair into the
    digest comparison alone.
    """
    linear, notion = _provider("linear"), _provider("notion")
    resident = warm._warm_spec_plan([linear, notion])
    reordered = warm._warm_spec_plan([notion, linear])
    assert resident.entries == reordered.entries, "same roster"
    assert resident.digest != reordered.digest, "a reorder must not be caught by the digest"

    def _never_respawn():
        raise AssertionError("an identical roster in a new order must reuse the process")

    monkeypatch.setattr(warm, "_acp_runtime_factory", _never_respawn)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    monkeypatch.setattr(warm._warm_mint, "_plan", resident)
    monkeypatch.setattr(warm._warm_mint, "_digest", resident.digest)

    served = await warm._warm_mint._ensure_locked([notion, linear])

    assert served is not None
    assert set(served.entries) == {"linear", "notion"}
    # The enumerated superset is what the next scan is judged against, so it must survive
    # a reuse untouched.
    assert warm._warm_mint._plan is resident


# ── an unreadable grant cache is not an absent grant ──
#
# `grant_present` is tri-state and `None` means the cache could not be read at all, so
# `not grant_present(...)` selected an indeterminate provider for a warm mint: consent was
# initiated on an absence nobody confirmed, and the card was flipped to waiting behind an
# approval URL for a provider that may already be connected. L1 collapses the same third
# answer on purpose because it never initiates consent; this path does.


def test_an_indeterminate_grant_is_not_read_as_an_absent_one(monkeypatch: pytest.MonkeyPatch):
    """RED before the fix: `not None` is True, so `unreadable` was warmed."""
    universe = [_provider("fresh"), _provider("unreadable"), _provider("granted")]

    def _presence(url: str) -> bool | None:
        if "unreadable" in url:
            return None
        return "granted" in url

    monkeypatch.setattr(warm, "grant_present", _presence)

    assert [p["slug"] for p in warm._warm_activation_candidates(universe)] == ["fresh"]


def test_an_indeterminate_provider_is_named_and_read_exactly_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """Skipped is not dropped: the provider is named, so its absence from the batch is
    accountable. Read ONCE -- a re-stat to classify the skip is the two-pass race
    `grant_presence` refuses, where a failure clearing between passes reads as absence."""
    reads: list[str] = []

    def _unreadable(url: str) -> bool | None:
        reads.append(url)
        return None

    monkeypatch.setattr(warm, "grant_present", _unreadable)

    with caplog.at_level(logging.WARNING, logger=warm.logger.name):
        assert warm._warm_activation_candidates([_provider("notion")]) == []

    assert len(reads) == 1, "presence was read more than once per provider"
    assert "notion" in caplog.text
