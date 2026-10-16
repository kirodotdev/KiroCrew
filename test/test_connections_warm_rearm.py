"""Warm-mint tests: row liveness, parked generations, the reaper, and re-arming a premint.

A row is redeemable only while both its generation and its session live and is withdrawn on
the fact that its holder is gone, a retry's expiry pass stays scoped to its own generation, a
parked generation is drained once the current process dies and keeps its rows while it is
parked (its number can still name the current generation), a dead shared process is re-armed
once and never into a restart loop, and an invalidated grant re-arms only its own provider's
premint without blocking the caller or stacking duplicates.
"""

from __future__ import annotations

import asyncio
from typing import Any

import connections_warm_helpers
import pytest
from connections_warm_helpers import (
    _parked_session,
    _provider,
    _rearm_registry,
    _result,
    _returns,
    _Runtime,
)

from kiro_crew.connections import warm
from kiro_crew.connections.mint import _mints
from kiro_crew.connections.registry import Provider
from kiro_crew.testing.wait import async_wait_until, await_until, finished, until_parked

# The shared fixtures, bound here by name so pytest registers them for this module.
_clean_mint_table = connections_warm_helpers._clean_mint_table
_clean_warm_runtime = connections_warm_helpers._clean_warm_runtime
_stub_activation = connections_warm_helpers._stub_activation


# ── redeemability takes TWO questions, and they die independently ──


def test_a_row_stamped_with_no_holder_at_all_is_never_alive():
    assert warm._warm_mint.generation_is_live(0) is False
    assert warm._warm_mint.activation_is_live(0) is False


def test_the_current_generation_is_live_exactly_while_its_process_is(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    assert warm._warm_mint.generation_is_live(4) is True
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    assert warm._warm_mint.generation_is_live(4) is False


def test_a_parked_generation_stays_live_while_its_own_process_can_still_redeem(
    monkeypatch: pytest.MonkeyPatch,
):
    """A parked process still holds its peers' verifiers, so answering False for it would
    withdraw a URL the user can still redeem."""
    monkeypatch.setattr(warm._warm_mint, "_generation", 9)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(3, _Runtime(True)), (4, _Runtime(False))])
    assert warm._warm_mint.generation_is_live(3) is True
    assert warm._warm_mint.generation_is_live(4) is False
    assert warm._warm_mint.generation_is_live(5) is False


def test_a_liveness_probe_that_raises_reads_as_dead_rather_than_failing_the_scan():
    """``expire_dead_mints`` runs on every status request, so a raising probe must not
    take the request down with it."""
    assert warm._runtime_alive(_Runtime(OSError("no such process"))) is False
    assert warm._runtime_alive(None) is False


def test_a_live_process_with_a_dead_session_does_not_keep_a_row_alive(
    monkeypatch: pytest.MonkeyPatch,
):
    """Process liveness alone passed a terminated-session row -- the observed failure that
    put the session question into the predicate at all."""
    monkeypatch.setattr(warm._warm_mint, "_generation", 2)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    row = {"state": "waiting", "shared": True, "generation": 2, "activation": 6}
    assert warm._warm_row_alive(row) is False  # type: ignore[arg-type]
    monkeypatch.setattr(warm._warm_mint, "_sessions", {6: object()})
    assert warm._warm_row_alive(row) is True  # type: ignore[arg-type]


# ── withdrawal is keyed on the FACT that the holder is gone ──


@pytest.mark.asyncio
async def test_a_row_whose_generation_is_gone_is_withdrawn():
    _mints["linear"] = {
        "state": "waiting",
        "oauth_url": "https://l/consent",
        "shared": True,
        "generation": 99,
        "activation": 1,
    }
    assert await warm.expire_dead_mints() == ["linear"]
    assert _mints["linear"]["state"] == "expired"
    assert _mints["linear"]["reason"] == "mint_process_gone"


@pytest.mark.asyncio
async def test_a_cold_row_is_left_to_the_cold_engine():
    """The two chokepoints partition the table, and each must leave the other's rows alone.

    A cold row owns a ``client`` and carries no warm mark at all, so ``_warm_table_row``
    excludes it and its verdict stays ``_mint_holder_alive``'s. The converse -- that a
    warm-held row's verdict is THIS chokepoint's, because the cold judge abstains on one
    -- is pinned in ``test_connections_handoff.py``.
    """
    _mints["linear"] = {"state": "waiting", "oauth_url": "https://cold", "client": object()}
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "waiting"


@pytest.mark.asyncio
async def test_a_shared_row_not_yet_serving_a_url_is_left_alone():
    """Only a row actually SERVING a URL can be serving a dead one; a claim still minting
    is the activation's to fill or release."""
    _mints["linear"] = {"state": "minting", "shared": True, "generation": 99}
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "minting"


@pytest.mark.asyncio
async def test_a_row_whose_process_and_session_both_live_keeps_its_url(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    monkeypatch.setattr(warm._warm_mint, "_sessions", {2: object()})
    _mints["linear"] = {
        "state": "waiting",
        "oauth_url": "https://l/consent",
        "shared": True,
        "generation": 5,
        "activation": 2,
    }
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "waiting"


# ── defect: the retry's expiry pass is unscoped ──


@pytest.mark.asyncio
async def test_a_retry_does_not_withdraw_a_parked_generations_live_url(
    monkeypatch: pytest.MonkeyPatch,
):
    """A parked generation's process still holds its verifier and its session still answers
    the redirect, so its URL is redeemable. An expiry pass not scoped to the DEAD generation
    withdraws a working link."""
    _mints["parked"] = {
        "state": "waiting",
        "shared": True,
        "oauth_url": "https://p/consent",
        "generation": 3,
        "activation": 1,
        "token": "tok-parked",
    }

    attempts: list[str] = []

    async def _dies_then_declines(*, slug: str = ""):
        attempts.append(slug)
        if len(attempts) == 1:
            raise warm._WarmMintDied("ProcessGone")
        return None

    monkeypatch.setattr(warm._warm_mint, "mint_for", _dies_then_declines)

    assert await warm._warm_activate() is None
    assert len(attempts) == 2, "the death is retried once"
    assert _mints["parked"]["state"] == "waiting"
    assert _mints["parked"]["oauth_url"] == "https://p/consent"


@pytest.mark.asyncio
async def test_a_dead_generations_rows_are_expired_by_the_kill_itself(
    monkeypatch: pytest.MonkeyPatch,
):
    """Why the retry needs no expiry pass of its own: the stand-down that precedes
    ``_WarmMintDied`` already withdrew exactly the rows whose verifier died, scoped to that
    generation and leaving every other generation alone."""
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    _mints["dead"] = {"state": "waiting", "shared": True, "generation": 4, "token": "t4"}
    _mints["parked"] = {"state": "waiting", "shared": True, "generation": 3, "token": "t3"}

    await warm._warm_mint._park_or_kill_locked()

    assert _mints["dead"]["state"] == "expired"
    assert _mints["dead"]["reason"] == "mint_process_gone"
    assert _mints["parked"]["state"] == "waiting"


# ── defect: a parked generation is stranded when the current process dies ──


@pytest.mark.asyncio
async def test_the_reaper_drains_parked_generations_after_the_current_process_dies(
    monkeypatch: pytest.MonkeyPatch,
):
    """Nothing else on any path reaches a parked generation once the current process is gone:
    a new mint would sweep it, and the leak is exactly the case where none arrives. So the
    process and its sessions stay resident forever after its last card completes."""
    parked_runtime = _Runtime(True)
    killed: list[Any] = []

    async def _record_kill(runtime):
        killed.append(runtime)
        return True

    monkeypatch.setattr(warm, "_kill_quietly", _record_kill)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 6)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(5, parked_runtime)])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {9: _parked_session(5)})
    # Generation 5 is parked because this card still points at it.
    _mints["parked"] = {
        "state": "waiting",
        "shared": True,
        "generation": 5,
        "activation": 9,
        "token": "t5",
    }

    sweeps = 0
    real_sweep = warm._warm_mint.sweep_retiring

    async def _counting_sweep():
        nonlocal sweeps
        sweeps += 1
        if sweeps == 2:
            # The card completes, so nothing needs generation 5 any more.
            _mints.pop("parked", None)
        await real_sweep()

    monkeypatch.setattr(warm._warm_mint, "sweep_retiring", _counting_sweep)

    await finished(warm._warm_mint_reaper(6), "warm mint reaper")

    assert killed == [parked_runtime], "the parked generation's process was never retired"
    assert warm._warm_mint._retiring == []
    assert warm._warm_mint._sessions == {}, "its sessions own the loopback servers"


# ── a dead shared process is re-armed once during the page-owned lifecycle ──


@pytest.mark.asyncio
async def test_the_reaper_rearms_a_dead_generation_once(monkeypatch: pytest.MonkeyPatch):
    """The page already paid for warming, so a mid-visit death is replaced without a click."""
    rearmed: list[int] = []

    async def _record_rearm(generation: int) -> list[str]:
        rearmed.append(generation)
        return ["linear"]

    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    monkeypatch.setattr(warm, "_rearm_dead_warm_mint", _record_rearm)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 6)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})
    warm._warm_mint.arm_supervision()

    await finished(warm._warm_mint_reaper(6), "warm mint reaper")

    assert rearmed == [6]
    assert warm._warm_mint._rearms_remaining == 0


@pytest.mark.asyncio
async def test_reaper_audits_the_grant_scan_before_replacement_warm(
    monkeypatch: pytest.MonkeyPatch,
):
    """A page-owned re-arm acts on the same credential observation as premint."""
    from kiro_crew import hooks

    assert warm._GRANT_PRESENCE_READ_ID in hooks._AUDIT_ONLY_READ_IDS
    provider = _provider("linear")
    events: list[tuple[Any, ...]] = []

    def _audit(read_id: str, outcome: str) -> bool:
        events.append(("audit", read_id, outcome))
        return True

    async def _record_warm(
        providers: list[Provider] | None = None, *, arm_supervision: bool = True
    ) -> list[str]:
        events.append(("warm", [item["slug"] for item in providers or []], arm_supervision))
        warm._warm_mint._runtime = _Runtime(True)
        return ["linear"]

    monkeypatch.setattr(hooks, "emit_internal_read_audit", _audit)
    monkeypatch.setattr(warm, "mintable_providers", lambda: [provider])
    monkeypatch.setattr(warm, "warm_mint_all", _record_warm)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 6)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})
    warm._warm_mint.arm_supervision()

    await finished(warm._warm_mint_reaper(6), "warm mint reaper")

    assert events == [
        ("audit", "connections_premint.oauth_grant_presence", "success"),
        ("warm", ["linear"], False),
    ]


@pytest.mark.asyncio
async def test_rearm_is_single_flight_and_cannot_form_a_restart_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    """Concurrent observers share one replacement, and its death spends no hidden retry."""
    gate = asyncio.Event()
    calls: list[int] = []

    async def _gated_rearm(generation: int) -> list[str]:
        calls.append(generation)
        await gate.wait()
        return []

    monkeypatch.setattr(warm, "_rearm_dead_warm_mint", _gated_rearm)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    warm._warm_mint.arm_supervision()

    first = warm._warm_mint.start_rearm(3)
    second = warm._warm_mint.start_rearm(3)

    assert first is not None and second is first
    # The one started rearm has recorded its call and is parked on the gate.
    await until_parked(gate, describe=lambda: f"calls={calls}")
    assert calls == [3]
    gate.set()
    await await_until(first, what="the rearm")
    # The finished rearm has released its single-flight slot, so the refusal
    # below is the spent budget, not a slot still held.
    await async_wait_until(lambda: warm._warm_mint._rearm_task is None)

    assert warm._warm_mint.start_rearm(3) is None
    assert calls == [3], "one automatic replacement must not recursively earn another"


@pytest.mark.asyncio
async def test_rearm_skips_connected_and_indeterminate_providers(
    monkeypatch: pytest.MonkeyPatch, _stub_activation: None
):
    """Recovery initiates consent only for a grant whose absence was confirmed."""
    universe = [_provider("fresh"), _provider("granted"), _provider("unreadable")]
    reads: list[str] = []

    def _presence(url: str) -> bool | None:
        reads.append(url)
        if "unreadable" in url:
            return None
        return "granted" in url

    monkeypatch.setattr(warm, "warm_spec_providers", lambda: universe)
    monkeypatch.setattr(warm, "grant_present", _presence)
    monkeypatch.setattr(
        warm,
        "_warm_activate",
        _returns(
            _result(
                [universe[0]],
                [{"serverName": "fresh", "oauthUrl": "https://fresh.example/consent"}],
                generation=5,
                activation=7,
            )
        ),
    )
    monkeypatch.setattr(warm._warm_mint, "_supervision_armed", True)
    monkeypatch.setattr(warm._warm_mint, "_rearms_remaining", 0)

    assert await warm._rearm_dead_warm_mint(4) == ["fresh"]

    assert list(_mints) == ["fresh"]
    assert _mints["fresh"]["state"] == "waiting"
    assert _mints["fresh"]["generation"] == 5
    assert warm._warm_mint._rearms_remaining == 0
    assert sorted(reads) == sorted(provider["mcp_url"] for provider in universe)


@pytest.mark.asyncio
async def test_an_explicit_page_warm_arms_one_replacement(
    monkeypatch: pytest.MonkeyPatch, _stub_activation: None
):
    monkeypatch.setattr(warm, "_warm_activate", _returns(None))

    assert await warm.warm_mint_all([_provider("linear")]) == []
    assert warm._warm_mint._supervision_armed is True
    assert warm._warm_mint._rearms_remaining == warm._WARM_REARM_ATTEMPTS


@pytest.mark.asyncio
async def test_shutdown_cancels_an_inflight_rearm_and_disarms_recovery(
    monkeypatch: pytest.MonkeyPatch,
):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _blocked_rearm(generation: int) -> list[str]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _kill(runtime: Any) -> bool:
        return True

    monkeypatch.setattr(warm, "_rearm_dead_warm_mint", _blocked_rearm)
    monkeypatch.setattr(warm, "_kill_quietly", _kill)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 8)
    warm._warm_mint.arm_supervision()
    task = warm._warm_mint.start_rearm(8)
    assert task is not None
    await started.wait()

    await warm._warm_mint.shutdown()

    assert cancelled.is_set()
    assert task.cancelled()
    assert warm._warm_mint._rearm_task is None
    assert warm._warm_mint._supervision_armed is False
    assert warm._warm_mint._rearms_remaining == 0
    assert warm._warm_mint.start_rearm(8) is None


# ── an invalidated grant re-arms that provider's premint ──


def _recording_warm(warmed: list[tuple[list[str], bool]]):
    async def _warm_mint_all(
        providers: list[Provider] | None = None, *, arm_supervision: bool = True
    ) -> list[str]:
        slugs = [str(item["slug"]) for item in providers or []]
        warmed.append((slugs, arm_supervision))
        return slugs

    return _warm_mint_all


@pytest.mark.asyncio
async def test_invalidation_rearms_only_the_invalidated_provider(
    monkeypatch: pytest.MonkeyPatch,
):
    """Provider-scoped, not a whole-pool warm: the other candidates are left alone."""
    warmed: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(
        warm, "mintable_providers", lambda: [_provider("linear"), _provider("vercel")]
    )
    monkeypatch.setattr(warm, "warm_mint_all", _recording_warm(warmed))

    assert await warm._rearm_invalidated_provider("vercel") == ["vercel"]

    assert warmed == [(["vercel"], False)]


@pytest.mark.asyncio
async def test_the_invalidation_call_does_not_block_on_minting(monkeypatch: pytest.MonkeyPatch):
    """The scheduler hands back before the activation it scheduled has even begun.

    A Disconnect calls this inside its own request, and an activation spawns a helper process
    and negotiates OAuth. Pinned on the task's own state rather than on wall-clock timing:
    ``create_task`` cannot run its coroutine until the caller yields, so an unstarted task at
    the moment the call returns is the property itself.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked_rearm(slug: str) -> list[str]:
        started.set()
        await release.wait()
        return [slug]

    monkeypatch.setattr(warm, "_rearm_invalidated_provider", _blocked_rearm)

    async with _rearm_registry() as registry:
        warm.rearm_invalidated_provider("linear")

        assert not started.is_set(), "the caller returned before the re-arm ran at all"
        task = registry["linear"]
        await await_until(started)
        assert not task.done()
        release.set()

        assert await await_until(task) == ["linear"]
        assert "linear" not in registry


@pytest.mark.asyncio
async def test_repeated_invalidation_does_not_stack_duplicate_premints(
    monkeypatch: pytest.MonkeyPatch,
):
    """Two more Disconnects while one re-arm is in flight add no second activation."""
    release = asyncio.Event()
    calls: list[str] = []

    async def _blocked_rearm(slug: str) -> list[str]:
        calls.append(slug)
        await release.wait()
        return []

    monkeypatch.setattr(warm, "_rearm_invalidated_provider", _blocked_rearm)

    async with _rearm_registry() as registry:
        warm.rearm_invalidated_provider("linear")
        first = registry["linear"]
        warm.rearm_invalidated_provider("linear")
        warm.rearm_invalidated_provider("linear")

        assert registry["linear"] is first
        release.set()
        await await_until(first)

        assert calls == ["linear"]
        assert "linear" not in registry


@pytest.mark.asyncio
async def test_a_provider_without_arming_preconditions_is_not_rearmed(
    monkeypatch: pytest.MonkeyPatch,
):
    """The ordinary candidate scan is the authority, so its vetoes all apply here.

    Every reason not to warm reaches this function as the same fact -- the scan did not return
    the provider -- whether it is a disabled entry, no usable auth configuration (no DCR and no
    pre-registered client id), a configured entry asking for something other than the registry,
    a grant still PRESENT because the revoke was refused, or a grant cache that could not be
    read at all. Re-arming any of them would only queue a mint that fails cold.
    """
    warmed: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(warm, "mintable_providers", lambda: [_provider("linear")])
    monkeypatch.setattr(warm, "warm_mint_all", _recording_warm(warmed))

    assert await warm._rearm_invalidated_provider("github") == []

    assert warmed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["minting", "waiting"])
async def test_a_live_row_is_not_displaced_by_an_invalidation_rearm(
    monkeypatch: pytest.MonkeyPatch, state: str
):
    """A card mid-consent keeps its URL: re-arming would replace one it may be redeeming."""
    warmed: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(warm, "mintable_providers", lambda: [_provider("linear")])
    monkeypatch.setattr(warm, "warm_mint_all", _recording_warm(warmed))
    _mints["linear"] = {"state": state, "shared": True, "token": "tok"}

    assert await warm._rearm_invalidated_provider("linear") == []

    assert warmed == []
    assert _mints["linear"]["token"] == "tok"


@pytest.mark.asyncio
async def test_the_invalidation_rearm_audits_the_grant_scan_it_acts_on(
    monkeypatch: pytest.MonkeyPatch,
):
    """It acts on a credential-store observation, so it owes the same SEL record as premint."""
    from kiro_crew import hooks

    assert warm._GRANT_PRESENCE_READ_ID in hooks._AUDIT_ONLY_READ_IDS
    events: list[tuple[Any, ...]] = []

    def _audit(read_id: str, outcome: str) -> bool:
        events.append(("audit", read_id, outcome))
        return True

    async def _record_warm(
        providers: list[Provider] | None = None, *, arm_supervision: bool = True
    ) -> list[str]:
        events.append(("warm", [str(item["slug"]) for item in providers or []], arm_supervision))
        return ["linear"]

    monkeypatch.setattr(hooks, "emit_internal_read_audit", _audit)
    monkeypatch.setattr(warm, "mintable_providers", lambda: [_provider("linear")])
    monkeypatch.setattr(warm, "warm_mint_all", _record_warm)

    assert await warm._rearm_invalidated_provider("linear") == ["linear"]

    assert events == [
        ("audit", "connections_premint.oauth_grant_presence", "success"),
        ("warm", ["linear"], False),
    ]


@pytest.mark.asyncio
async def test_an_invalidation_rearm_reaches_a_real_activation(
    monkeypatch: pytest.MonkeyPatch, _stub_activation: None
):
    """End to end through the real ``warm_mint_all``: the card ends up holding a URL."""
    provider = _provider("linear")
    monkeypatch.setattr(warm, "warm_spec_providers", lambda: [provider])
    monkeypatch.setattr(warm, "grant_present", lambda url: False)
    monkeypatch.setattr(
        warm,
        "_warm_activate",
        _returns(
            _result(
                [provider],
                [{"serverName": "linear", "oauthUrl": "https://linear.example/consent"}],
                generation=9,
                activation=4,
            )
        ),
    )

    assert await warm._rearm_invalidated_provider("linear") == ["linear"]

    assert _mints["linear"]["state"] == "waiting"
    assert _mints["linear"]["generation"] == 9
    assert warm._warm_mint._supervision_armed is False


def test_the_scheduler_is_a_noop_without_a_running_loop():
    """A synchronous caller off the loop has nothing to schedule onto; the next scan covers it.

    Deliberately not wrapped in ``_rearm_registry``: that helper is async and this test must
    run with NO event loop, which is the property under test. Nothing needs settling either --
    the assertion is that no task was ever created.
    """
    warm._invalidation_rearms.clear()

    warm.rearm_invalidated_provider("linear")

    assert warm._invalidation_rearms == {}


@pytest.mark.asyncio
async def test_shutdown_settles_an_inflight_invalidation_rearm(monkeypatch: pytest.MonkeyPatch):
    """A Disconnect moments before teardown must not leave its re-arm unsettled at loop close.

    Nothing awaits these tasks in the ordinary course, so teardown is the only place that can
    settle them. An unsettled one is reported as ``Task was destroyed but it is pending`` and
    its activation is abandoned part-way through minting.
    """
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _blocked_rearm(slug: str) -> list[str]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return []

    async def _kill(runtime: Any) -> bool:
        return True

    monkeypatch.setattr(warm, "_rearm_invalidated_provider", _blocked_rearm)
    monkeypatch.setattr(warm, "_kill_quietly", _kill)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    async with _rearm_registry() as registry:
        warm.rearm_invalidated_provider("linear")
        task = registry["linear"]
        await await_until(started)

        await finished(warm.shutdown_warm_mint(), "shutdown warm mint")

        assert cancelled.is_set(), "teardown did not cancel the in-flight re-arm"
        assert task.cancelled()
        assert registry == {}


@pytest.mark.asyncio
async def test_shutdown_leaves_a_completed_rearms_bookkeeping_alone(
    monkeypatch: pytest.MonkeyPatch,
):
    """Settlement only touches tasks still in flight; a finished re-arm keeps its result."""

    async def _quick_rearm(slug: str) -> list[str]:
        return [slug]

    async def _kill(runtime: Any) -> bool:
        return True

    monkeypatch.setattr(warm, "_rearm_invalidated_provider", _quick_rearm)
    monkeypatch.setattr(warm, "_kill_quietly", _kill)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    async with _rearm_registry() as registry:
        warm.rearm_invalidated_provider("linear")
        task = registry["linear"]
        assert await await_until(task) == ["linear"]

        await finished(warm.shutdown_warm_mint(), "shutdown warm mint")

        assert not task.cancelled()
        assert task.result() == ["linear"]


@pytest.mark.asyncio
async def test_a_failed_respawn_still_drains_the_generation_it_parked(
    monkeypatch: pytest.MonkeyPatch,
):
    """The same leak by the other route: the stand-down cancels the old reaper, so a spawn
    that then fails leaves a parked generation with no reaper at all."""
    parked_runtime = _Runtime(True)
    killed: list[Any] = []

    async def _record_kill(runtime):
        killed.append(runtime)
        return True

    def _explode(*a, **k):
        raise OSError("kiro-cli is not installed")

    monkeypatch.setattr(warm, "_kill_quietly", _record_kill)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm, "_write_warm_mint_specs", lambda plan, agents_dir: None)
    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _explode)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    monkeypatch.setattr(warm._warm_mint, "_runtime", None)
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(5, parked_runtime)])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    assert await warm._warm_mint._ensure_locked([_provider("linear")]) is None

    drain = warm._warm_mint._reaper
    assert drain is not None, "a parked generation with no process needs a drain task"
    await await_until(drain)
    assert killed == [parked_runtime]
    assert warm._warm_mint._retiring == []


# ── defect: the CURRENT generation number can name a PARKED process ──
#
# Only a successful spawn bumps `_generation`, so a stand-down leaves the live process in
# `_retiring` under the number that is still `self._generation`, with `self._runtime`
# cleared. The equality branch answered `is_alive()` for that number and so reported the
# parked process dead -- withdrawing redeemable URLs, and then letting the next sweep kill
# the very process the park existed to preserve.


@pytest.mark.asyncio
async def test_a_generation_parked_mid_respawn_keeps_its_rows(monkeypatch: pytest.MonkeyPatch):
    """A status scan runs without the runtime lock, so it sees the window between the
    stand-down and its replacement spawn."""
    live = _Runtime(True)
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    # Exactly the mid-respawn state: stood down, replacement not yet spawned, so the
    # parked entry still carries the CURRENT number.
    monkeypatch.setattr(warm._warm_mint, "_runtime", None)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(4, live)])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {2: _parked_session(4)})
    _mints["linear"] = {
        "state": "waiting",
        "shared": True,
        "oauth_url": "https://l/consent",
        "generation": 4,
        "activation": 2,
        "token": "t4",
    }

    assert warm._warm_mint.generation_is_live(4) is True
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "waiting"


def test_a_dead_current_runtime_is_still_dead_when_nothing_is_parked(
    monkeypatch: pytest.MonkeyPatch,
):
    """The fall-through must not turn a genuinely dead generation live."""
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    assert warm._warm_mint.generation_is_live(4) is False
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(4, _Runtime(False))])
    assert warm._warm_mint.generation_is_live(4) is False
