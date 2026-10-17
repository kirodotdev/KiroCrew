"""Model state on ``AcpRuntime`` and ``AcpSessionHandle``.

Resolved-model capture in ``store_session_config``, the entitlement probe and its
caches, ``refresh_available_models``, and read-path revalidation.
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import _make_runtime, _register

from kiro_crew.acp.runtime import AcpRuntimeError, AcpSessionHandle
from kiro_crew.acp.types import METHOD_SESSION_NEW, METHOD_SESSION_TERMINATE
from kiro_crew.testing.wait import await_until

# ── store_session_config: resolved-model capture ──


def test_store_session_config_adopts_sole_advertised_model_when_no_current_id():
    """An unpinned session whose ``session/new`` advertises exactly one model
    but omits ``currentModelId`` must still resolve that model, so ``served_model``
    is non-empty for the whole run (the panel model chip depends on it)."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config(
        {"models": {"availableModels": [{"modelId": "kiro-model-x", "name": "X"}]}}
    )
    assert handle._resolved_model_id == "kiro-model-x"
    assert handle.served_model == "kiro-model-x"


def test_store_session_config_current_model_id_wins_over_sole_advertised():
    """When the backend DOES echo ``currentModelId`` it is authoritative — the
    sole-advertised fallback must not override it."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config(
        {
            "models": {
                "currentModelId": "kiro-current",
                "availableModels": [{"modelId": "kiro-other", "name": "Other"}],
            }
        }
    )
    assert handle._resolved_model_id == "kiro-current"


def test_store_session_config_leaves_model_empty_when_ambiguous():
    """Two or more advertised models and no ``currentModelId`` is genuinely
    ambiguous — do not guess; ``served_model`` stays empty."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config(
        {
            "models": {
                "availableModels": [
                    {"modelId": "kiro-a", "name": "A"},
                    {"modelId": "kiro-b", "name": "B"},
                ]
            }
        }
    )
    assert handle._resolved_model_id == ""
    assert handle.served_model == ""


# ── probe_advertised_models (entitlement revalidation) ──


_PROBE_RESP = {
    "sessionId": "probe-1",
    "models": {
        "currentModelId": "claude-opus-5",
        "availableModels": [
            {"modelId": "auto", "name": "auto"},
            {"modelId": "claude-opus-5", "name": "claude-opus-5"},
        ],
    },
}


@pytest.mark.asyncio
async def test_probe_returns_fresh_set_and_terminates_probe_session():
    """The probe re-asks entitlement with a throwaway minimal session/new
    (mcpServers present-but-empty — kiro-cli treats a missing field as
    malformed), reads the advertised set, and evicts the probe session so it
    never accumulates in the shared process."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(side_effect=[_PROBE_RESP, {}])  # type: ignore[method-assign]
    fresh = await rt.probe_advertised_models()
    assert [m["modelId"] for m in fresh] == ["auto", "claude-opus-5"]
    calls = rt._send_and_await.call_args_list
    assert calls[0].args[0] == METHOD_SESSION_NEW
    assert calls[0].args[1]["mcpServers"] == []
    assert calls[1].args[0] == METHOD_SESSION_TERMINATE
    assert calls[1].args[1] == {"sessionId": "probe-1"}
    # Init scope closed — staged init notifications cannot leak into a later
    # real session.
    assert rt._session_inits_in_flight == 0


@pytest.mark.asyncio
async def test_probe_failure_returns_empty_and_closes_init_scope():
    """A failed probe is not evidence: it returns [] (caller keeps its prior
    snapshot) and must not leave the init-notification scope open."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=AcpRuntimeError("boom")
    )
    assert await rt.probe_advertised_models() == []
    assert rt._session_inits_in_flight == 0


@pytest.mark.asyncio
async def test_probe_advertising_nothing_returns_empty_but_still_terminates():
    """A session/new that omits models yields [] — and the probe session is
    still evicted. The empty outcome is TTL-recorded (O4), so a second call
    inside the TTL replays [] WITHOUT opening a fresh session/new; the cached
    RESULT stays empty (no evidence = fail open)."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{"sessionId": "probe-2"}, {}, {"sessionId": "probe-3"}, {}]
    )
    assert await rt.probe_advertised_models() == []
    assert rt._send_and_await.call_args_list[1].args[0] == METHOD_SESSION_TERMINATE
    # Second call inside the TTL: still [], but no NEW session/new was opened.
    assert await rt.probe_advertised_models() == []
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 1


@pytest.mark.asyncio
async def test_probe_failure_is_ttl_cached_so_a_burst_costs_one_session(monkeypatch):
    """O4: a FAILING probe records the attempt time too, so a burst of reads
    inside the TTL costs one session/new, not one per read. The failure replays
    as [] (no evidence = fail open) and the cached result stays empty."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=AcpRuntimeError("boom")
    )
    assert await rt.probe_advertised_models() == []
    assert await rt.probe_advertised_models() == []
    # The single-flight lock serialized both; only ONE session/new was attempted
    # because the second call hit the TTL guard on the recorded failure time.
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 1
    # Past the TTL, it probes again (still failing here).
    rt._entitlement_probe_attempt_at = time.monotonic() - 100.0
    assert await rt.probe_advertised_models() == []
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 2


@pytest.mark.asyncio
async def test_probe_result_reused_within_ttl():
    """A fresh non-empty answer is served from cache inside the TTL, so a burst
    of rejections costs one round-trip."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(side_effect=[_PROBE_RESP, {}])  # type: ignore[method-assign]
    first = await rt.probe_advertised_models()
    second = await rt.probe_advertised_models()
    assert second == first
    # One session/new + one terminate total: the second call never hit the wire.
    assert rt._send_and_await.await_count == 2


@pytest.mark.asyncio
async def test_a_failure_never_revives_an_expired_success(monkeypatch):
    """P2: once a successful result's OWN TTL has expired, a subsequent FAILURE
    within its own (attempt) TTL must return [] — not replay the stale success.
    A failure buys a no-new-session window, never a fresh lease on old data."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=[_PROBE_RESP, {}, AcpRuntimeError("boom")]
    )
    first = await rt.probe_advertised_models()
    assert [m["modelId"] for m in first] == ["auto", "claude-opus-5"]
    # Expire BOTH clocks so the next call is a genuinely fresh probe (not an
    # attempt-window replay); it fails, stamping only the attempt clock.
    rt._entitlement_probe_result_at = time.monotonic() - 100.0
    rt._entitlement_probe_attempt_at = time.monotonic() - 100.0
    failed = await rt.probe_advertised_models()
    assert failed == []  # the expired success is NOT replayed
    # A burst right after the failure returns [] from the attempt-clock window,
    # still never the stale success, and opens no new session.
    assert await rt.probe_advertised_models() == []
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 2  # the original success + the one failure


@pytest.mark.asyncio
async def test_old_success_and_repeated_failures_past_ttl_reprobe(monkeypatch):
    """P2: an old success plus failures for longer than the TTL does not replay
    forever — once both clocks are stale the next call opens a fresh session."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=[_PROBE_RESP, {}, AcpRuntimeError("boom"), _PROBE_RESP, {}]
    )
    await rt.probe_advertised_models()  # success
    # Expire both clocks so the next call is a fresh probe (a failure).
    rt._entitlement_probe_result_at = time.monotonic() - 100.0
    rt._entitlement_probe_attempt_at = time.monotonic() - 100.0
    await rt.probe_advertised_models()  # failure, stamps attempt clock only
    # The success result is long expired and the fresh attempt clock is now
    # stale too: the next call must probe again, not replay the old success.
    rt._entitlement_probe_attempt_at = time.monotonic() - 100.0
    third = await rt.probe_advertised_models()
    assert [m["modelId"] for m in third] == ["auto", "claude-opus-5"]
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 3


@pytest.mark.asyncio
async def test_force_bypasses_the_failure_replay_but_not_a_recent_success(monkeypatch):
    """D1: a user action (force=True) skips the failed/empty attempt-clock replay
    so it earns a fresh probe, but still honours a recent NON-EMPTY success —
    re-probing gains nothing there. force=False keeps the burst cap."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=[AcpRuntimeError("boom"), _PROBE_RESP, {}]
    )
    # First attempt fails, stamping the attempt clock.
    assert await rt.probe_advertised_models() == []
    # force=False within the TTL replays [] with no new session (the read path).
    assert await rt.probe_advertised_models() == []
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 1
    # force=True within the same window opens a FRESH session (user action),
    # which here succeeds.
    forced = await rt.probe_advertised_models(force=True)
    assert [m["modelId"] for m in forced] == ["auto", "claude-opus-5"]
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 2
    # A recent SUCCESS is replayed even under force — no third session opened.
    again = await rt.probe_advertised_models(force=True)
    assert [m["modelId"] for m in again] == ["auto", "claude-opus-5"]
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 2


@pytest.mark.asyncio
async def test_probe_single_flight_concurrent_callers_share_one_probe():
    rt, _, _ = _make_runtime()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            started.set()
            await release.wait()
            return dict(_PROBE_RESP)
        return {}

    rt._send_and_await = AsyncMock(side_effect=slow_send)  # type: ignore[method-assign]
    t1 = asyncio.ensure_future(rt.probe_advertised_models())
    t2 = asyncio.ensure_future(rt.probe_advertised_models())
    await started.wait()
    release.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1 == r2 != []
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 1


@pytest.mark.asyncio
async def test_probe_on_dead_or_uninitialized_runtime_returns_empty():
    rt, _, _ = _make_runtime()
    rt._dead = True
    assert await rt.probe_advertised_models() == []
    rt2, _, _ = _make_runtime()
    rt2._initialized = False
    assert await rt2.probe_advertised_models() == []


# ── AcpSessionHandle.refresh_available_models ──


@pytest.mark.asyncio
async def test_refresh_replaces_snapshot_on_nonempty_probe():
    """One refresh heals every consumer of the handle's snapshot: the fresh
    probe answer replaces the session-init availableModels in place."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config(
        {
            "models": {
                "availableModels": [
                    {"modelId": "claude-sonnet-4"},
                    {"modelId": "claude-sonnet-4.5"},
                ]
            }
        }
    )
    fresh_set = [
        {"modelId": "auto", "name": "auto", "description": ""},
        {"modelId": "claude-opus-5", "name": "claude-opus-5", "description": ""},
    ]
    rt.probe_advertised_models = AsyncMock(return_value=fresh_set)  # type: ignore[method-assign]
    fresh = await handle.refresh_available_models()
    assert fresh == fresh_set
    assert [m["modelId"] for m in handle.available_models] == [
        "auto",
        "claude-opus-5",
    ]


@pytest.mark.asyncio
async def test_refresh_keeps_snapshot_on_empty_probe():
    """A failed/empty probe is not evidence — the prior snapshot survives so a
    flaky probe can never WIDEN or clear entitlement."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config({"models": {"availableModels": [{"modelId": "claude-sonnet-4"}]}})
    rt.probe_advertised_models = AsyncMock(return_value=[])  # type: ignore[method-assign]
    assert await handle.refresh_available_models() == []
    assert [m["modelId"] for m in handle.available_models] == ["claude-sonnet-4"]


_BROAD_SET = [
    {"modelId": "auto", "name": "auto", "description": ""},
    {"modelId": "claude-opus-5", "name": "claude-opus-5", "description": ""},
]


def _seed_cached_result(rt, at: float) -> None:
    """Park a non-empty result on BOTH clocks at monotonic time ``at``."""
    rt._entitlement_probe_result = list(_BROAD_SET)
    rt._entitlement_probe_result_at = at
    rt._entitlement_probe_attempt_at = at


@pytest.mark.asyncio
async def test_a_cached_result_older_than_the_floor_is_not_replayed():
    """A replay never answers with a result older than the caller's snapshot.
    Neither clock predating the floor stands in for a probe: the unforced read
    opens a fresh session/new instead of replaying, and so does a forced one."""
    rt, _, _ = _make_runtime()
    # Seeded in the past: Windows' monotonic clock ticks coarsely, so a cache
    # stamped "now" can share a tick with the fresh result and defeat the
    # strict "newer" assertion below.
    t0 = time.monotonic() - 5.0
    _seed_cached_result(rt, t0)
    rt._send_and_await = AsyncMock(side_effect=[_PROBE_RESP, {}, _PROBE_RESP, {}])  # type: ignore[method-assign]

    fresh = await rt.probe_advertised_models(not_before=t0 + 1.0)
    assert [m["modelId"] for m in fresh] == ["auto", "claude-opus-5"]
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 1
    # The fresh result is newer than the cached one it superseded.
    assert rt._entitlement_probe_result_at > t0

    forced = await rt.probe_advertised_models(force=True, not_before=time.monotonic() + 1.0)
    assert [m["modelId"] for m in forced] == ["auto", "claude-opus-5"]
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 2


@pytest.mark.asyncio
async def test_a_failed_attempt_older_than_the_floor_does_not_suppress_the_probe():
    """A no-evidence attempt that predates the caller's snapshot is not evidence
    about that snapshot: the read probes. An attempt at or after the floor still
    replays [] within the window and opens no session/new."""
    rt, _, _ = _make_runtime()
    t0 = time.monotonic()
    rt._entitlement_probe_result = []
    rt._entitlement_probe_result_at = 0.0
    rt._entitlement_probe_attempt_at = t0
    rt._send_and_await = AsyncMock(side_effect=AssertionError("no probe"))  # type: ignore[method-assign]

    assert await rt.probe_advertised_models(not_before=t0) == []
    assert await rt.probe_advertised_models(not_before=t0 - 5.0) == []
    assert rt._send_and_await.await_count == 0

    rt._send_and_await = AsyncMock(side_effect=[_PROBE_RESP, {}])  # type: ignore[method-assign]
    fresh = await rt.probe_advertised_models(not_before=t0 + 1.0)
    assert [m["modelId"] for m in fresh] == ["auto", "claude-opus-5"]
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 1


@pytest.mark.asyncio
async def test_a_probe_is_stamped_when_its_answer_arrives_not_after_teardown(monkeypatch):
    """The result clock records the moment the probe's session/new answered,
    BEFORE the terminate round-trip. A real session/new that completes during
    that teardown holds a NEWER snapshot, and the floor must keep the probe's
    older answer from replaying over it."""
    rt, _, _ = _make_runtime()
    stamped_during_teardown: list[float] = []

    async def slow_terminate(session_id: str) -> None:
        # flake-ok: pre-existing 50 ms delay in the stubbed terminate that spaces two monotonic stamps, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed terminate's own delay, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.05)
        # A concurrent real session captures its snapshot mid-teardown.
        stamped_during_teardown.append(time.monotonic())

    monkeypatch.setattr(rt, "terminate_session", slow_terminate)
    rt._send_and_await = AsyncMock(side_effect=[_PROBE_RESP])  # type: ignore[method-assign]

    fresh = await rt.probe_advertised_models()
    assert [m["modelId"] for m in fresh] == ["auto", "claude-opus-5"]
    assert stamped_during_teardown, "teardown fake did not run"
    # The probe's answer predates the snapshot captured during its teardown...
    assert rt._entitlement_probe_result_at < stamped_during_teardown[0]
    # ...so a caller holding that newer snapshot is not served the older answer:
    # the floor falls through to a real probe attempt (here failing -> []).
    rt._send_and_await = AsyncMock(side_effect=RuntimeError("probe attempted"))  # type: ignore[method-assign]
    monkeypatch.setattr(rt, "terminate_session", AsyncMock())
    rt._entitlement_probe_attempt_at = 0.0
    assert await rt.probe_advertised_models(not_before=stamped_during_teardown[0]) == []
    assert rt._send_and_await.await_count == 1
    rt, _, _ = _make_runtime()
    t0 = time.monotonic()
    _seed_cached_result(rt, t0)
    rt._send_and_await = AsyncMock(side_effect=AssertionError("no probe"))  # type: ignore[method-assign]

    assert await rt.probe_advertised_models(not_before=t0) == _BROAD_SET
    assert await rt.probe_advertised_models(not_before=t0 - 5.0) == _BROAD_SET
    assert rt._send_and_await.await_count == 0


@pytest.mark.asyncio
async def test_refresh_never_replaces_a_newer_narrower_snapshot_with_an_older_cache():
    """A broad answer cached on the shared runtime BEFORE this session captured
    a narrower list at session/new is not replayed over it, and neither does the
    stale attempt clock stand in for a probe: the handle earns a fresh
    session/new, and only that fresh evidence replaces the snapshot."""
    rt, _, _ = _make_runtime()
    t0 = time.monotonic() - 5.0
    _seed_cached_result(rt, t0)
    rt._send_and_await = AsyncMock(side_effect=[_PROBE_RESP, {}])  # type: ignore[method-assign]
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config({"models": {"availableModels": [{"modelId": "auto"}]}})
    captured_at = handle._available_models_captured_at
    assert captured_at > t0

    fresh = await handle.refresh_available_models()
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 1
    assert [m["modelId"] for m in fresh] == ["auto", "claude-opus-5"]
    # The snapshot was replaced by the fresh probe, never by the stale cache
    # (stamped 5s in the past, so this holds on a coarse monotonic clock too).
    assert rt._entitlement_probe_result_at > t0
    assert handle._available_models_probe_confirmed is True


@pytest.mark.asyncio
async def test_refresh_replays_a_cache_newer_than_the_snapshot():
    rt, _, _ = _make_runtime()
    t0 = time.monotonic()
    _seed_cached_result(rt, t0)
    rt._send_and_await = AsyncMock(side_effect=AssertionError("no probe"))  # type: ignore[method-assign]
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config({"models": {"availableModels": [{"modelId": "auto"}]}})
    handle._available_models_captured_at = t0 - 1.0

    assert await handle.refresh_available_models() == _BROAD_SET
    assert [m["modelId"] for m in handle.available_models] == ["auto", "claude-opus-5"]
    assert handle._available_models_probe_confirmed is True


@pytest.mark.asyncio
async def test_the_handle_that_filled_the_cache_gets_the_replay_on_a_repeat_pick(monkeypatch):
    """The refreshed snapshot is dated from before the probe was awaited, so the
    answer that filled the cache is at or above this handle's floor: a second
    forced pick within the TTL replays it rather than opening another
    throwaway session/new."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config({"models": {"availableModels": [{"modelId": "auto"}]}})

    async def slow_terminate(session_id: str) -> None:
        # flake-ok: pre-existing 20 ms delay in the stubbed terminate before the probe answers, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed terminate's own delay, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.02)

    monkeypatch.setattr(rt, "terminate_session", slow_terminate)
    rt._send_and_await = AsyncMock(side_effect=[_PROBE_RESP])  # type: ignore[method-assign]

    first = await handle.refresh_available_models(force=True)
    assert [m["modelId"] for m in first] == ["auto", "claude-opus-5"]
    assert handle._available_models_captured_at == rt._entitlement_probe_result_at

    rt._send_and_await = AsyncMock(side_effect=AssertionError("second session/new"))  # type: ignore[method-assign]
    second = await handle.refresh_available_models(force=True)
    assert [m["modelId"] for m in second] == ["auto", "claude-opus-5"]
    assert rt._send_and_await.await_count == 0


@pytest.mark.asyncio
async def test_a_replayed_answer_dates_the_snapshot_by_its_own_clock():
    """A refresh served from the runtime's replay stores the snapshot dated by
    the replayed answer's arrival, not by the call: the handle's floor stays at
    the data it holds, so a further refresh within the TTL replays again (no
    session/new), and a replay does not re-date the snapshot out of the spawn
    race window it was captured in."""
    rt, _, _ = _make_runtime()
    t0 = time.monotonic() - 5.0
    _seed_cached_result(rt, t0)
    rt._send_and_await = AsyncMock(side_effect=AssertionError("no probe"))  # type: ignore[method-assign]
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config({"models": {"availableModels": [{"modelId": "auto"}]}})
    handle._available_models_captured_at = t0 - 1.0

    assert await handle.refresh_available_models() == _BROAD_SET
    assert handle._available_models_captured_at == t0
    assert handle._available_models_captured_at < time.monotonic() - 4.0

    assert await handle.refresh_available_models(force=True) == _BROAD_SET
    assert handle._available_models_captured_at == t0
    assert rt._send_and_await.await_count == 0


class TestParseAdvertisedModels:
    """Both response shapes normalize identically, so a probe answer and a
    session-init snapshot are directly comparable."""

    def test_models_object_shape(self):
        from kiro_crew.acp.session_handle import parse_advertised_models

        out = parse_advertised_models(_PROBE_RESP)
        assert [m["modelId"] for m in out] == ["auto", "claude-opus-5"]
        assert all(set(m) == {"modelId", "name", "description"} for m in out)

    def test_bare_list_shape(self):
        from kiro_crew.acp.session_handle import parse_advertised_models

        out = parse_advertised_models({"availableModels": [{"modelId": "claude-sonnet-4"}]})
        assert [m["modelId"] for m in out] == ["claude-sonnet-4"]

    def test_absent_or_malformed_yields_empty(self):
        from kiro_crew.acp.session_handle import parse_advertised_models

        assert parse_advertised_models({}) == []
        assert parse_advertised_models({"models": {"availableModels": "nope"}}) == []
        assert parse_advertised_models({"models": 7}) == []


class TestStoreSessionConfigParseConsolidation:
    """Drift-pin: ``store_session_config`` sources its model list from
    ``parse_advertised_models``, so the session-init snapshot can never drift
    from what a pooled-runtime probe would parse out of the same payload."""

    def _handle(self):
        rt, _, _ = _make_runtime()
        q = _register(rt, "sA")
        return AcpSessionHandle("sA", q["sA"], rt)

    def test_models_object_shape_matches_canonical_parser(self):
        # Mixed modelId/value spellings + a missing description exercise every
        # normalization branch; hard-coded expectation so a regression inside
        # parse_advertised_models fails this pin too.
        resp = {
            "models": {
                "currentModelId": "kiro-model-x",
                "availableModels": [
                    {"modelId": "kiro-model-x", "name": "X", "description": "d"},
                    {"value": "kiro-model-y"},
                    {"name": "no id — skipped"},
                    "not-a-dict",
                ],
            }
        }
        handle = self._handle()
        handle.store_session_config(resp)
        assert handle.available_models == [
            {"modelId": "kiro-model-x", "name": "X", "description": "d"},
            {"modelId": "kiro-model-y", "name": "kiro-model-y", "description": ""},
        ]

    def test_bare_list_shape_matches_canonical_parser(self):
        resp = {"availableModels": [{"modelId": "kiro-model-x"}, {"value": "kiro-model-y"}]}
        handle = self._handle()
        handle.store_session_config(resp)
        assert handle.available_models == [
            {"modelId": "kiro-model-x", "name": "kiro-model-x", "description": ""},
            {"modelId": "kiro-model-y", "name": "kiro-model-y", "description": ""},
        ]

    def test_bare_list_under_models_key_matches_canonical_parser(self):
        """The bare-list branch is the only site that RE-KEYS the payload
        (``models`` list → ``{"availableModels": models}`` envelope) — reach
        it via the ``models`` key so the re-key itself is pinned."""
        handle = self._handle()
        handle.store_session_config(
            {"models": [{"modelId": "kiro-model-x"}, {"value": "kiro-model-y"}]}
        )
        assert handle.available_models == [
            {"modelId": "kiro-model-x", "name": "kiro-model-x", "description": ""},
            {"modelId": "kiro-model-y", "name": "kiro-model-y", "description": ""},
        ]

    def test_dict_branch_delegates_to_canonical_parser(self, monkeypatch):
        """Anti-re-fork pin: the dict branch must SOURCE its list from
        ``parse_advertised_models`` AND call it with the checked-binding
        envelope — a restored inline walk, a whole-response re-resolution, or
        a wrong envelope all fail this pin."""
        import kiro_crew.acp.session_handle as sh

        sentinel = [
            {"modelId": "sentinel-a", "name": "A", "description": ""},
            {"modelId": "sentinel-b", "name": "B", "description": ""},
        ]
        calls: list = []

        def _fake(resp):
            calls.append(resp)
            return list(sentinel)

        monkeypatch.setattr(sh, "parse_advertised_models", _fake)
        handle = self._handle()
        handle.store_session_config({"models": {"availableModels": [{"modelId": "real"}]}})
        assert handle.available_models == sentinel
        assert calls == [{"models": {"availableModels": [{"modelId": "real"}]}}]

    def test_bare_list_branch_delegates_to_canonical_parser(self, monkeypatch):
        import kiro_crew.acp.session_handle as sh

        sentinel = [
            {"modelId": "sentinel-a", "name": "A", "description": ""},
            {"modelId": "sentinel-b", "name": "B", "description": ""},
        ]
        calls: list = []

        def _fake(resp):
            calls.append(resp)
            return list(sentinel)

        monkeypatch.setattr(sh, "parse_advertised_models", _fake)
        handle = self._handle()
        handle.store_session_config({"availableModels": [{"modelId": "real"}]})
        assert handle.available_models == sentinel
        assert calls == [{"availableModels": [{"modelId": "real"}]}]

    def test_well_formed_empty_list_still_overwrites_prior_snapshot(self):
        """Pre-existing call-site policy pinned through the consolidation: a
        WELL-FORMED empty ``availableModels`` list DOES clear a prior snapshot
        here (unlike ``AcpClient._capture_available_models``'s non-empty
        guard). Switching this site to a client-style guard would be a
        behavior change this test exists to catch."""
        handle = self._handle()
        handle.store_session_config({"models": {"availableModels": [{"modelId": "kiro-model-x"}]}})
        assert [m["modelId"] for m in handle.available_models] == ["kiro-model-x"]
        handle.store_session_config({"models": {"availableModels": []}})
        assert handle.available_models == []

    def test_malformed_inner_shape_does_not_clobber_prior_snapshot(self):
        """The assignment guard survives the consolidation: a later response
        whose ``availableModels`` is malformed must not clear an
        already-captured list (the canonical parser returns ``[]`` for it, but
        assignment policy is the call site's, not the parser's)."""
        handle = self._handle()
        handle.store_session_config({"models": {"availableModels": [{"modelId": "kiro-model-x"}]}})
        assert [m["modelId"] for m in handle.available_models] == ["kiro-model-x"]
        handle.store_session_config({"models": {"availableModels": "nope"}})
        assert [m["modelId"] for m in handle.available_models] == ["kiro-model-x"]


# ── Read-path entitlement revalidation (maybe_refresh_available_models) ──
#
# The dashboard picker narrows the model catalog through the newest live
# session's advertised-model snapshot. When that snapshot is the startup-race
# default it hides models the account has, and no explicit pick is ever refused
# to trigger the refresh-before-refuse heal. maybe_refresh_available_models is
# the read-path counterpart: it decides WHETHER a narrowing snapshot is stale
# enough to re-probe, reusing refresh_available_models for the probe itself. It
# never decides entitlement (that stays with catalog_row_would_drop, the
# endpoint's own per-row verdict); these pin the staleness heuristic and the
# fail-open contract.

_KIRO_CATALOG_IDS = ["auto", "claude-opus-5", "claude-sonnet-5"]


def _entitlement_handle(rt):
    return AcpSessionHandle("sE", _register(rt, "sE")["sE"], rt)


@pytest.mark.asyncio
async def test_auto_only_snapshot_revalidates_and_replaces_from_probe():
    """(a) The strongest staleness signal — an unconfirmed auto-only snapshot
    against a richer catalog — re-probes, and a disagreeing probe replaces it."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic() - 3600.0  # long past the spawn race band
    handle = _entitlement_handle(rt)
    handle._available_models = [{"modelId": "auto", "name": "auto", "description": ""}]
    handle._mark_available_models_captured()  # unconfirmed
    full = [
        {"modelId": "auto", "name": "auto", "description": ""},
        {"modelId": "claude-sonnet-5", "name": "Sonnet 5", "description": ""},
        {"modelId": "claude-opus-5", "name": "Opus 5", "description": ""},
    ]
    rt.probe_advertised_models = AsyncMock(return_value=full)  # type: ignore[method-assign]

    result = await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)

    rt.probe_advertised_models.assert_awaited_once()
    assert [m["modelId"] for m in result] == [m["modelId"] for m in full]
    assert [m["modelId"] for m in handle.available_models] == [m["modelId"] for m in full]
    assert handle._available_models_probe_confirmed is True


@pytest.mark.asyncio
async def test_confirmed_recently_probed_snapshot_does_not_reprobe():
    """(b) A narrowing snapshot that was probe-confirmed AND probed within the
    per-session interval is trusted: no probe, snapshot untouched."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic() - 3600.0
    handle = _entitlement_handle(rt)
    handle._available_models = [
        {"modelId": "auto", "name": "auto", "description": ""},
        {"modelId": "claude-sonnet-5", "name": "Sonnet 5", "description": ""},
    ]
    handle._mark_available_models_captured()
    handle._available_models_probe_confirmed = True
    handle._available_models_read_probe_at = time.monotonic()  # just probed
    rt.probe_advertised_models = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)

    rt.probe_advertised_models.assert_not_awaited()
    assert [m["modelId"] for m in result] == ["auto", "claude-sonnet-5"]


@pytest.mark.asyncio
async def test_probe_failure_keeps_the_current_snapshot_fail_open():
    """(c) A probe that fails must never worsen the picker: the current snapshot
    is returned unchanged, exactly as before the read-path revalidation."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic()  # inside the spawn race band -> suspect
    handle = _entitlement_handle(rt)
    handle._available_models = [
        {"modelId": "auto", "name": "auto", "description": ""},
        {"modelId": "claude-sonnet-5", "name": "Sonnet 5", "description": ""},
    ]
    handle._mark_available_models_captured()
    rt.probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom")
    )

    result = await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)

    rt.probe_advertised_models.assert_awaited_once()
    assert [m["modelId"] for m in result] == ["auto", "claude-sonnet-5"]
    assert [m["modelId"] for m in handle.available_models] == ["auto", "claude-sonnet-5"]


@pytest.mark.asyncio
async def test_unconfirmed_narrow_snapshot_reprobes_and_probe_agrees():
    """(d) A legitimately narrow but never-probe-confirmed snapshot IS suspect,
    so it re-probes; a probe that agrees marks it confirmed and it stays narrow."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic() - 3600.0
    handle = _entitlement_handle(rt)
    narrow = [
        {"modelId": "auto", "name": "auto", "description": ""},
        {"modelId": "claude-sonnet-5", "name": "Sonnet 5", "description": ""},
    ]
    handle._available_models = list(narrow)
    handle._mark_available_models_captured()  # confirmed=False
    assert handle._available_models_probe_confirmed is False
    rt.probe_advertised_models = AsyncMock(return_value=narrow)  # type: ignore[method-assign]

    result = await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)

    rt.probe_advertised_models.assert_awaited_once()
    assert [m["modelId"] for m in result] == ["auto", "claude-sonnet-5"]
    assert handle._available_models_probe_confirmed is True


@pytest.mark.asyncio
async def test_snapshot_covering_the_catalog_never_probes():
    """The cheap-path gate: a snapshot that would not narrow the catalog cannot
    hide anything, so no probe is spent even when it was never confirmed."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic()  # would be "suspect" if it narrowed
    handle = _entitlement_handle(rt)
    handle._available_models = [
        {"modelId": mid, "name": mid, "description": ""} for mid in _KIRO_CATALOG_IDS
    ]
    handle._mark_available_models_captured()  # confirmed=False
    rt.probe_advertised_models = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)

    rt.probe_advertised_models.assert_not_awaited()
    assert [m["modelId"] for m in result] == _KIRO_CATALOG_IDS


@pytest.mark.asyncio
async def test_unadvertised_auto_sentinel_alone_does_not_trigger_a_probe():
    """The endpoint keeps ``auto`` whatever the snapshot advertises, so a
    snapshot that omits only ``auto`` narrows nothing and spends no probe."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic()  # would be "suspect" if it narrowed
    handle = _entitlement_handle(rt)
    handle._available_models = [{"modelId": "m1", "name": "m1", "description": ""}]
    handle._mark_available_models_captured()  # confirmed=False
    rt.probe_advertised_models = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await handle.maybe_refresh_available_models(["auto", "m1"])

    rt.probe_advertised_models.assert_not_awaited()
    assert handle._read_refresh_task is None
    assert [m["modelId"] for m in result] == ["m1"]


@pytest.mark.asyncio
async def test_unadvertised_real_model_beside_auto_still_probes():
    """Excluding the ``auto`` sentinel is narrow: a real catalog model the
    snapshot omits still makes the snapshot narrowing, so it probes."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic()
    handle = _entitlement_handle(rt)
    snapshot = [{"modelId": "m1", "name": "m1", "description": ""}]
    handle._available_models = list(snapshot)
    handle._mark_available_models_captured()  # confirmed=False
    rt.probe_advertised_models = AsyncMock(return_value=snapshot)  # type: ignore[method-assign]

    result = await handle.maybe_refresh_available_models(["auto", "m1", "m2"])

    rt.probe_advertised_models.assert_awaited_once()
    assert [m["modelId"] for m in result] == ["m1"]


@pytest.mark.asyncio
async def test_namespace_qualified_row_the_endpoint_folds_does_not_probe():
    """A ``ns::id`` catalog row whose bare id the snapshot advertises is KEPT by
    the endpoint (rewritten to the bare id), so it hides nothing and spends no
    probe."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic()  # would be "suspect" if it narrowed
    handle = _entitlement_handle(rt)
    handle._available_models = [
        {"modelId": "auto", "name": "auto", "description": ""},
        {"modelId": "m1", "name": "m1", "description": ""},
    ]
    handle._mark_available_models_captured()  # confirmed=False
    rt.probe_advertised_models = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await handle.maybe_refresh_available_models(["auto", "ns::m1"])

    rt.probe_advertised_models.assert_not_awaited()
    assert handle._read_refresh_task is None
    assert [m["modelId"] for m in result] == ["auto", "m1"]


@pytest.mark.asyncio
async def test_empty_catalog_id_does_not_trigger_a_probe():
    """An empty catalog id drops against every snapshot, so a fresher one cannot
    restore it and it spends no probe."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic()  # would be "suspect" if it narrowed
    handle = _entitlement_handle(rt)
    handle._available_models = [{"modelId": "m1", "name": "m1", "description": ""}]
    handle._mark_available_models_captured()  # confirmed=False
    rt.probe_advertised_models = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await handle.maybe_refresh_available_models(["", "m1"])

    rt.probe_advertised_models.assert_not_awaited()
    assert handle._read_refresh_task is None
    assert [m["modelId"] for m in result] == ["m1"]


@pytest.mark.asyncio
async def test_disjoint_snapshot_the_endpoint_fails_open_on_does_not_probe():
    """A snapshot that advertises neither ``auto`` nor any catalog row makes the
    endpoint fail open to the full catalog, so it narrows nothing and spends no
    probe."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic()  # would be "suspect" if it narrowed
    handle = _entitlement_handle(rt)
    handle._available_models = [{"modelId": "x9", "name": "x9", "description": ""}]
    handle._mark_available_models_captured()  # confirmed=False
    rt.probe_advertised_models = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await handle.maybe_refresh_available_models(["auto", "m1", "m2"])

    rt.probe_advertised_models.assert_not_awaited()
    assert handle._read_refresh_task is None
    assert [m["modelId"] for m in result] == ["x9"]


@pytest.mark.asyncio
async def test_probe_deadline_timeout_raises_revalidating_but_probe_completes():
    """O1/F2: when the probe exceeds the read deadline the read RAISES
    EntitlementRevalidating (so the endpoint returns 503 and the frontend
    re-polls rather than caching the un-revalidated snapshot as live), and the
    probe is NOT cancelled — it keeps running to completion and replaces the
    snapshot, so the next read serves the corrected list."""
    from kiro_crew.acp.session_handle import EntitlementRevalidating

    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic() - 3600.0
    handle = _entitlement_handle(rt)
    handle._available_models = [{"modelId": "auto", "name": "auto", "description": ""}]
    handle._mark_available_models_captured()

    release = asyncio.Event()
    probe_finished = asyncio.Event()

    async def _slow(*_a, **_kw) -> list:
        await release.wait()
        probe_finished.set()
        return [
            {"modelId": "auto", "name": "auto", "description": ""},
            {"modelId": "claude-opus-5", "name": "Opus 5", "description": ""},
        ]

    rt.probe_advertised_models = AsyncMock(side_effect=_slow)  # type: ignore[method-assign]
    from kiro_crew.acp import session_handle as _sh

    with patch.object(_sh, "_READ_PATH_PROBE_DEADLINE_SECS", 0.01):
        with pytest.raises(EntitlementRevalidating):
            await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)

    # Timed out -> signalled revalidating, probe still in flight (not cancelled).
    assert handle._read_refresh_task is not None and not handle._read_refresh_task.done()

    # Let the probe finish; it was NOT cancelled, so it completes and replaces
    # the snapshot in place (what the next read will serve).
    release.set()
    await await_until(probe_finished, what="the model probe finishing")
    await await_until(handle._read_refresh_task, what="the model probe landing")
    assert [m["modelId"] for m in handle.available_models] == ["auto", "claude-opus-5"]
    assert handle._available_models_probe_confirmed is True


@pytest.mark.asyncio
async def test_in_flight_probe_is_awaited_not_bypassed_on_the_next_poll():
    """P1: the frontend re-polls every 8s while degraded. A second read that
    lands WHILE the same shielded probe is still in flight must NOT bypass it via
    the interval gate and return the un-revalidated snapshot (which the endpoint
    would serve as a live 200 the frontend caches). It re-awaits the same task,
    so it raises EntitlementRevalidating again; only once the probe lands does a
    read return the corrected list."""
    from kiro_crew.acp.session_handle import EntitlementRevalidating

    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic() - 3600.0
    handle = _entitlement_handle(rt)
    handle._available_models = [{"modelId": "auto", "name": "auto", "description": ""}]
    handle._mark_available_models_captured()

    release = asyncio.Event()

    async def _slow(*_a, **_kw) -> list:
        await release.wait()
        return [
            {"modelId": "auto", "name": "auto", "description": ""},
            {"modelId": "claude-opus-5", "name": "Opus 5", "description": ""},
        ]

    rt.probe_advertised_models = AsyncMock(side_effect=_slow)  # type: ignore[method-assign]
    from kiro_crew.acp import session_handle as _sh

    with patch.object(_sh, "_READ_PATH_PROBE_DEADLINE_SECS", 0.01):
        # First read: starts the probe, times out, raises.
        with pytest.raises(EntitlementRevalidating):
            await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)
        # Second read INSIDE the interval while the same probe still runs: it
        # must re-await that task and raise again, NOT return the stale snapshot.
        with pytest.raises(EntitlementRevalidating):
            await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)

    # Exactly one probe was ever started (the second read reused the task).
    assert rt.probe_advertised_models.await_count == 1

    # Let the probe land; a read now returns the corrected list.
    release.set()
    await await_until(handle._read_refresh_task, what="the model probe landing")
    third = await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)
    assert [m["modelId"] for m in third] == ["auto", "claude-opus-5"]
    assert rt.probe_advertised_models.await_count == 1


@pytest.mark.asyncio
async def test_confirmed_auto_only_snapshot_honours_the_reprobe_interval():
    """F3: a genuine free-tier account is legitimately auto-only. Once a probe
    has CONFIRMED an auto-only snapshot, an immediate second read must NOT probe
    again — auto-only overrides the confirmed flag, not the re-probe interval."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic() - 3600.0
    handle = _entitlement_handle(rt)
    handle._available_models = [{"modelId": "auto", "name": "auto", "description": ""}]
    handle._mark_available_models_captured()  # unconfirmed
    # The probe agrees: the account really is auto-only.
    rt.probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"modelId": "auto", "name": "auto", "description": ""}]
    )

    first = await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)
    assert [m["modelId"] for m in first] == ["auto"]
    assert rt.probe_advertised_models.await_count == 1
    assert handle._available_models_probe_confirmed is True

    # Immediate second read: still auto-only and still narrowing, but confirmed
    # and inside the interval -> no second probe (no forever re-probe).
    second = await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)
    assert [m["modelId"] for m in second] == ["auto"]
    assert rt.probe_advertised_models.await_count == 1


@pytest.mark.asyncio
async def test_unconfirmed_auto_only_always_earns_one_probe_even_if_recent():
    """F3 boundary: auto-only still overrides the CONFIRMED flag. An unconfirmed
    auto-only snapshot probes even when the read-probe clock was recently set,
    because it has never been confirmed."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic() - 3600.0
    handle = _entitlement_handle(rt)
    handle._available_models = [{"modelId": "auto", "name": "auto", "description": ""}]
    handle._mark_available_models_captured()  # unconfirmed
    # A recent read-probe stamp would gate a confirmed snapshot; unconfirmed
    # auto-only is suspect regardless, and the stamp is only consulted after the
    # suspect gate — but confirmed is False here so it must still probe.
    rt.probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {"modelId": "auto", "name": "auto", "description": ""},
            {"modelId": "claude-opus-5", "name": "Opus 5", "description": ""},
        ]
    )
    result = await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)
    assert rt.probe_advertised_models.await_count == 1
    assert [m["modelId"] for m in result] == ["auto", "claude-opus-5"]


@pytest.mark.asyncio
async def test_unconfirmed_auto_only_reprobes_after_a_failed_probe_within_the_interval():
    """G2: a FAILED probe leaves an auto-only snapshot unconfirmed, and the
    interval must not then suppress it for the whole window — an unconfirmed
    auto-only snapshot always gets to probe. The first read probes and the probe
    raises; the immediate second read probes AGAIN rather than sitting stale."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic() - 3600.0
    handle = _entitlement_handle(rt)
    handle._available_models = [{"modelId": "auto", "name": "auto", "description": ""}]
    handle._mark_available_models_captured()  # unconfirmed
    rt.probe_advertised_models = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom")
    )

    first = await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)
    assert [m["modelId"] for m in first] == ["auto"]
    assert rt.probe_advertised_models.await_count == 1
    # Probe failed -> still unconfirmed. Immediately (inside the interval) probe
    # again rather than suppressing an unconfirmed auto-only snapshot.
    assert handle._available_models_probe_confirmed is False
    # Drain the failed task (it raised) so single-flight starts a fresh probe.
    with pytest.raises(RuntimeError):
        await await_until(handle._read_refresh_task, what="the model probe landing")
    second = await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)
    assert [m["modelId"] for m in second] == ["auto"]
    assert rt.probe_advertised_models.await_count == 2


@pytest.mark.asyncio
async def test_read_path_revalidation_reuses_the_shared_probe_not_a_second_one():
    """Mutation probe / no-second-spelling guard: the read path heals through
    refresh_available_models (which calls the runtime's single-flight probe),
    NOT a private re-implementation. If maybe_refresh_available_models stopped
    delegating to refresh_available_models, the snapshot would never be replaced
    and this disagreeing probe would be ignored — the exact regression this pins.
    """
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic() - 3600.0
    handle = _entitlement_handle(rt)
    handle._available_models = [{"modelId": "auto", "name": "auto", "description": ""}]
    handle._mark_available_models_captured()
    full = [
        {"modelId": "auto", "name": "auto", "description": ""},
        {"modelId": "claude-opus-5", "name": "Opus 5", "description": ""},
    ]
    calls = {"n": 0}

    async def _probe(*_a, **_kw) -> list:
        calls["n"] += 1
        return full

    rt.probe_advertised_models = AsyncMock(side_effect=_probe)  # type: ignore[method-assign]

    await handle.maybe_refresh_available_models(_KIRO_CATALOG_IDS)

    # Exactly one shared probe, and the snapshot was replaced through the shared
    # refresh path — proof the read path did not grow a second parser/probe.
    assert calls["n"] == 1
    assert [m["modelId"] for m in handle.available_models] == ["auto", "claude-opus-5"]
