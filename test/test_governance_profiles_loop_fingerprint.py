"""Event-loop freshness of the governance profile store.

Split out of ``test_governance_profiles.py`` (file-line-cap). Covers the on-loop
``_dir_fingerprint`` rate limit: the synchronous PreToolUse gate reaches
``ProfileStore._ensure_fresh`` on every gated tool call, so a warm store re-stats
the profiles directory at most once per ``_LOOP_RESTAT_MIN_INTERVAL_S`` on the
loop and serves the published snapshot in between. The ceiling token is never
rate-limited, the window is loop-only and warm-only, and a cold store always
loads (fail-closed).
"""

from __future__ import annotations

import json
import os

import pytest

from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.governance import resolve


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    gp.reset_store()
    yield d
    gp.reset_store()


def _write(d, name, body):
    (d / f"{name}.json").write_text(json.dumps(body))


class _FrozenClock:
    """A stand-in for the governance_profiles module's own ``time`` binding.

    Rebinding ``gp.time`` to this (``monkeypatch.setattr(gp, "time", ...)``)
    freezes only the clock ``_ensure_fresh`` reads — it never touches the stdlib
    ``time`` module, so the event loop's ``loop.time()`` (which is
    ``time.monotonic()``) and every other thread are unaffected (D2; the
    kirocrew.test-stdlib-clock-rebound rule). Only ``monotonic`` is used by the
    module under test. ``advance`` moves it, so a test drives the on-loop window
    deterministically instead of depending on wall-clock elapsing.
    """

    def __init__(self, value: float) -> None:
        self._value = value

    def monotonic(self) -> float:
        return self._value

    def advance(self, seconds: float) -> None:
        self._value += seconds


def test_warm_store_on_loop_rate_limits_the_fingerprint_walk(profiles_dir, monkeypatch):
    # On the EVENT LOOP a warm store must re-stat the profiles dir at most
    # once per _LOOP_RESTAT_MIN_INTERVAL_S — the synchronous PreToolUse gate reaches
    # _ensure_fresh on every gated tool call, and a _dir_fingerprint walk per call
    # stalls the loop under GIL contention. Pin it: many resolutions on the loop
    # inside one window do exactly ONE walk.
    #
    # The monotonic clock the store reads is PINNED to a fixed value for the whole
    # window so the assertion does not depend on how fast the runner executes 25
    # calls (writing-tests determinism class 2/5): a real clock could cross the
    # 1 s boundary mid-loop on a slow runner and the walk count would flake.
    import asyncio

    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    gp.reset_store()
    # Prime OFF the loop so the store is warm before we measure on-loop walks.
    assert gp.resolve_active_scope("cron:j:r") is not None

    real_fp = gp._dir_fingerprint
    calls = {"n": 0}

    def _counting_fp(directory):
        calls["n"] += 1
        return real_fp(directory)

    # Freeze the store's clock: every on-loop call inside the loop reads the same
    # instant, so the window never advances regardless of runner speed. Rebind the
    # module's OWN `time` name (not the stdlib time module), so the event loop's
    # clock is untouched.
    monkeypatch.setattr(gp, "time", _FrozenClock(1000.0))
    monkeypatch.setattr(gp, "_dir_fingerprint", _counting_fp)

    async def _resolve_many_on_the_loop():
        # Reset the window start so the priming walk above does not count against us.
        gp._STORE._last_loop_restat = 0.0
        for _ in range(25):
            gp.resolve_active_scope("cron:j:r")

    asyncio.run(_resolve_many_on_the_loop())

    assert calls["n"] == 1, (
        "a warm on-loop caller must walk the profiles dir at most once per window; "
        f"got {calls['n']} walks across 25 gated calls (the per-call stall)"
    )


def test_off_loop_caller_is_not_rate_limited(profiles_dir, monkeypatch):
    # Guardrail: the rate limit is LOOP-ONLY. An off-loop caller (the
    # dashboard watcher, the capability prime, the profile-projection watch) must
    # re-stat EVERY call so a profile edit is picked up immediately there — that
    # is what keeps the on-loop caching window bounded by an external refresh.
    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    gp.reset_store()
    assert gp.resolve_active_scope("cron:j:r") is not None  # prime (off loop)

    real_fp = gp._dir_fingerprint
    calls = {"n": 0}

    def _counting_fp(directory):
        calls["n"] += 1
        return real_fp(directory)

    monkeypatch.setattr(gp, "_dir_fingerprint", _counting_fp)
    for _ in range(5):
        gp.resolve_active_scope("cron:j:r")

    assert calls["n"] == 5, (
        "an off-loop caller must re-stat every call (no rate limit); "
        f"got {calls['n']} walks across 5 calls"
    )


def test_on_loop_edit_is_picked_up_after_the_window(profiles_dir, monkeypatch):
    # Fail-closed pin ("a changed profile is picked up"):
    # the on-loop caching window must be a DELAY, never a miss. A profile tightened
    # on disk is still enforced on the loop once the window has elapsed. Simulate
    # the window expiry by rewinding the store's last-restat stamp rather than
    # sleeping, so the test is deterministic (writing-tests class 2).
    import asyncio

    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read", "code"]},
        },
    )
    gp.reset_store()

    # Freeze the store's clock (its OWN time binding, not the stdlib module) and
    # drive the window by advancing it, so both the within-window and after-window
    # assertions are deterministic and never depend on wall-clock elapsing.
    clock = _FrozenClock(5000.0)
    monkeypatch.setattr(gp, "time", clock)

    async def _drive():
        # First on-loop resolution: loads, 'code' is allowed.
        prof = gp.resolve_active_scope("cron:j:r")
        assert prof is not None and resolve(None, prof, "tools", "code").permitted

        # Prime the warm on-loop restat window: this call re-stats (the cold load
        # above did not touch the stamp) and records the window start at the frozen
        # instant. The loop is now inside an active caching window.
        gp.resolve_active_scope("cron:j:r")
        assert gp._STORE._last_loop_restat == clock.monotonic()

        # Tighten the profile on disk (drop 'code') with a changed fingerprint.
        _write(
            profiles_dir,
            "cron",
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            },
        )
        p = profiles_dir / "cron.json"
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + 5))

        # Still inside the window (clock has not advanced): the loop serves the
        # prior (still-looser) snapshot without re-statting.
        clock.advance(gp._LOOP_RESTAT_MIN_INTERVAL_S / 2)
        prof_cached = gp.resolve_active_scope("cron:j:r")
        assert prof_cached is not None and resolve(None, prof_cached, "tools", "code").permitted

        # Advance past the window → the next on-loop call re-stats and enforces the
        # tightening.
        clock.advance(gp._LOOP_RESTAT_MIN_INTERVAL_S)
        prof_fresh = gp.resolve_active_scope("cron:j:r")
        assert prof_fresh is not None
        assert not resolve(
            None, prof_fresh, "tools", "code"
        ).permitted, "a tightened profile must be enforced on the loop after the window"

    asyncio.run(_drive())


def test_on_loop_cold_store_is_never_rate_limited(profiles_dir):
    # Fail-closed pin: the rate limit guards ONLY the warm path
    # (self._snap.loaded). A never-loaded store on the loop must still LOAD — an
    # empty snapshot read as "no profiles configured" would fail OPEN. The very
    # first on-loop resolution of a bound surface must see the profile.
    import asyncio

    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    gp.reset_store()  # COLD: nothing primed it.

    async def _first_touch_on_loop():
        return gp.resolve_active_scope("cron:j:r")

    prof = asyncio.run(_first_touch_on_loop())
    assert prof is not None, "a cold on-loop first touch must load, never serve an empty snapshot"
    assert resolve(None, prof, "tools", "read").permitted


def test_ceiling_swap_is_picked_up_on_the_loop_inside_the_window(profiles_dir, monkeypatch):
    # The on-loop window rate-limits ONLY the _dir_fingerprint walk. The ceiling
    # token (two locked integer reads, no I/O) is re-read on every on-loop call, so
    # a mid-session ceiling swap — policy_distribution.apply_ceiling bumps
    # governance_generation() — must bust the window and take the reload path even
    # when the profiles directory is untouched and the clock has not advanced. A
    # window that skipped the ceiling comparison would keep serving profiles
    # composed against the retired ceiling for up to a second on the loop.
    import asyncio

    import kiro_crew.platform.governance_profiles as gpm

    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    gp.reset_store()

    token = {"v": (False, 1)}
    monkeypatch.setattr(gpm, "_ceiling_token", lambda: token["v"])
    # Freeze the clock so the window is unambiguously ACTIVE for every call below —
    # any reload is forced by the ceiling token, never by the window expiring.
    # Rebind the module's OWN `time` name, not the stdlib time module.
    monkeypatch.setattr(gpm, "time", _FrozenClock(2000.0))

    real_reload = gp._STORE._reload
    reloads = {"n": 0}

    def _counting_reload(directory):
        reloads["n"] += 1
        return real_reload(directory)

    monkeypatch.setattr(gp._STORE, "_reload", _counting_reload)

    async def _drive():
        # Prime on the loop: first resolve loads + sets the window start.
        assert gp.resolve_active_scope("cron:j:r") is not None
        reloads["n"] = 0

        # Same ceiling, same dir, inside the window → NO reload (window skips the walk).
        gp.resolve_active_scope("cron:j:r")
        assert reloads["n"] == 0, "an unchanged ceiling inside the window must not reload"

        # Swap the ceiling (bump the generation). Directory untouched, clock frozen
        # — only the ceiling token changed. The next on-loop call must reload.
        token["v"] = (False, 2)
        gp.resolve_active_scope("cron:j:r")
        assert reloads["n"] == 1, (
            "a ceiling swap inside the window must bust the window and reload; "
            f"got {reloads['n']} reloads"
        )

    asyncio.run(_drive())
