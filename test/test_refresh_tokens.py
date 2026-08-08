"""Unit tests for the refresh-token module.

Covers TR-U-* test cases from docs/system-specs/features/dashboard-token-auth.md.

These tests exercise generate_refresh_token / validate_refresh_token /
RefreshStateManager directly. Handler integration tests are out of scope
here (they need an aiohttp test client and live in test_handlers_*.py
files that don't yet exist for this surface).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.dashboard.refresh_tokens import (
    MAX_REFRESH_TTL_SECS,
    REFRESH_GRACE_SECS,
    RefreshStateManager,
    cookie_jar_needs_pruning,
    foreign_port_cookies,
    generate_refresh_token,
    refresh_cookie_name,
    validate_refresh_token,
)

# -- Fixtures -----------------------------------------------------------------


@pytest.fixture()
def isolated_state(tmp_path: Path):
    """Patch the module singleton's state path to a temp dir for isolation.

    Each test gets a fresh RefreshStateManager backed by a fresh file so
    cross-test pollution is impossible.
    """
    state_file = tmp_path / "refresh_chains.json"
    mgr = RefreshStateManager(state_path=state_file)

    # Force the module-level singleton to point at our isolated manager
    with patch(
        "kiro_crew.dashboard.refresh_tokens._state_singleton",
        mgr,
    ):
        yield mgr


# -- generate_refresh_token (TR-U-01..04) -------------------------------------


def test_tr_u_01_generate_basic():
    """Returns dotted token, fresh chain_id (12 hex), fresh jti (24 hex)."""
    token, chain_id, jti, exp = generate_refresh_token("alice")
    assert "." in token
    assert len(chain_id) == 12
    assert len(jti) == 24
    # All hex characters
    int(chain_id, 16)
    int(jti, 16)
    # Exp is roughly 30 days out
    delta = exp - time.time()
    assert MAX_REFRESH_TTL_SECS - 5 < delta <= MAX_REFRESH_TTL_SECS


def test_tr_u_02_generate_with_chain_id():
    """Passing chain_id continues an existing chain."""
    token, chain_id, _jti, _exp = generate_refresh_token(
        "alice", chain_id="abc123def456"
    )
    assert chain_id == "abc123def456"
    # Token decodes back to the same chain_id
    valid, _user, _reason, decoded_chain, _decoded_jti, _exp = validate_refresh_token(
        token
    )
    assert valid is True
    assert decoded_chain == "abc123def456"


def test_tr_u_03_jti_chain_uniqueness():
    """Two back-to-back mints have different jti AND different chain_id."""
    _t1, c1, j1, _ = generate_refresh_token("alice")
    _t2, c2, j2, _ = generate_refresh_token("alice")
    assert j1 != j2
    assert c1 != c2


def test_tr_u_04_session_exp_within_max():
    """session_exp - iat is exactly MAX_REFRESH_TTL_SECS."""
    token, _chain, _jti, exp = generate_refresh_token("alice")
    parts = token.split(".")
    import base64

    payload_bytes = base64.urlsafe_b64decode(parts[0] + "=" * (4 - len(parts[0]) % 4))
    payload = json.loads(payload_bytes)
    diff = payload["session_exp"] - payload["iat"]
    assert abs(diff - MAX_REFRESH_TTL_SECS) < 1.0


# -- validate_refresh_token (TR-U-05..10) ------------------------------------


def test_tr_u_05_validate_happy_path():
    token, chain_id, jti, _exp = generate_refresh_token("alice")
    valid, user, reason, decoded_chain, decoded_jti, _exp = validate_refresh_token(
        token
    )
    assert valid is True
    assert user == "alice"
    assert reason == ""
    assert decoded_chain == chain_id
    assert decoded_jti == jti


def test_tr_u_06_validate_tampered_signature():
    token, _chain, _jti, _exp = generate_refresh_token("alice")
    # Tamper the signature
    parts = token.split(".")
    tampered = f"{parts[0]}.{'A' * len(parts[1])}"
    valid, _user, reason, _c, _j, _e = validate_refresh_token(tampered)
    assert valid is False
    assert reason == "bad signature"


def test_tr_u_07_validate_expired():
    """Expiry checks fire when session_exp is in the past."""
    # Mint with a 1-second TTL, sleep, validate
    token, _chain, _jti, _exp = generate_refresh_token("alice", ttl_seconds=1)
    time.sleep(1.1)
    valid, _user, reason, _c, _j, _e = validate_refresh_token(token)
    assert valid is False
    assert reason == "expired"


def test_tr_u_08_validate_wrong_kind():
    """Access tokens (kind != 'refresh') are rejected."""
    # generate_token (the access token) doesn't set kind=refresh
    from kiro_crew.dashboard.token_auth import generate_token

    access = generate_token("alice")
    valid, _user, reason, _c, _j, _e = validate_refresh_token(access)
    assert valid is False
    assert reason == "wrong token kind"


def test_tr_u_09_validate_malformed_no_dot():
    valid, _user, reason, _c, _j, _e = validate_refresh_token("notatokenatall")
    assert valid is False
    assert reason == "malformed token"


def test_tr_u_10_validate_empty():
    valid, _user, reason, _c, _j, _e = validate_refresh_token("")
    assert valid is False
    assert reason == "malformed token"


# -- RefreshStateManager (TR-U-11..18) ---------------------------------------


def test_tr_u_11_mark_and_check_consumed(isolated_state: RefreshStateManager):
    isolated_state.mark_consumed(
        "jti1", chain_id="c1", exp=time.time() + 30 * 86400, ip="1.2.3.4", replacement="{}"
    )
    assert isolated_state.is_consumed("jti1") is True
    assert isolated_state.is_consumed("jti2") is False


def test_tr_u_12_mark_consumed_idempotent(isolated_state: RefreshStateManager):
    isolated_state.mark_consumed(
        "jti1", chain_id="c1", exp=time.time() + 30 * 86400, ip="1.2.3.4", replacement="{}"
    )
    # Second call should not error
    isolated_state.mark_consumed(
        "jti1", chain_id="c1", exp=time.time() + 30 * 86400, ip="5.6.7.8", replacement="{}"
    )
    assert isolated_state.is_consumed("jti1") is True


def test_tr_u_13_revoke_chain(isolated_state: RefreshStateManager):
    isolated_state.revoke_chain("c1", time.time() + 30 * 86400)
    assert isolated_state.is_chain_revoked("c1") is True
    assert isolated_state.is_chain_revoked("c2") is False


def test_tr_u_14_validate_rejects_revoked_chain(isolated_state: RefreshStateManager):
    token, chain_id, _jti, _exp = generate_refresh_token("alice")
    isolated_state.revoke_chain(chain_id, time.time() + 30 * 86400)
    valid, _user, reason, _c, _j, _e = validate_refresh_token(token)
    assert valid is False
    assert reason == "chain revoked"


def test_tr_u_15_evict_expired(isolated_state: RefreshStateManager):
    isolated_state.mark_consumed(
        "jti_old", chain_id="c1", exp=time.time() - 10, ip="1.2.3.4", replacement="{}"
    )
    isolated_state.mark_consumed(
        "jti_new", chain_id="c1", exp=time.time() + 86400, ip="1.2.3.4", replacement="{}"
    )
    isolated_state.evict_expired(now=time.time())
    assert isolated_state.is_consumed("jti_old") is False
    assert isolated_state.is_consumed("jti_new") is True


def test_tr_u_15a_mark_consumed_auto_evicts_expired(tmp_path: Path):
    """mark_consumed must call evict_expired so the on-disk file cannot
    grow without bound (e.g. an attacker pumping rotations with a stolen
    refresh cookie before reuse-detection fires).
    """
    state_file = tmp_path / "rt.json"
    mgr = RefreshStateManager(state_path=state_file)
    # Seed an already-expired entry directly (bypassing mark_consumed)
    with mgr._lock:  # noqa: SLF001 — test-only direct access
        mgr._consumed_jtis["expired_jti"] = time.time() - 10
    assert mgr.is_consumed("expired_jti") is True
    # A fresh mark_consumed call must evict the expired one.
    mgr.mark_consumed(
        "fresh", chain_id="c1", exp=time.time() + 86400, ip="1.1.1.1", replacement="{}"
    )
    assert mgr.is_consumed("expired_jti") is False
    assert mgr.is_consumed("fresh") is True


def test_tr_u_15b_revoke_chain_auto_evicts_expired(tmp_path: Path):
    """revoke_chain must also auto-evict so revocation of an expired chain
    does not stash a permanent record.
    """
    state_file = tmp_path / "rt.json"
    mgr = RefreshStateManager(state_path=state_file)
    with mgr._lock:  # noqa: SLF001
        mgr._revoked_chains["c_expired"] = time.time() - 10
    assert mgr.is_chain_revoked("c_expired") is True
    mgr.revoke_chain("c_active", time.time() + 86400)
    assert mgr.is_chain_revoked("c_expired") is False
    assert mgr.is_chain_revoked("c_active") is True


def test_tr_u_15c_handler_rate_limiter_caps_per_ip():
    """_rate_limited returns True on the (max+1)-th call within the window.

    Defense-in-depth against an attacker pumping rotations.
    """
    from kiro_crew.dashboard.handlers import auth_refresh

    # Fresh bucket for this IP to avoid cross-test pollution
    auth_refresh._refresh_rate_buckets.pop("198.51.100.7", None)
    now = 1_000_000.0
    cap = auth_refresh._REFRESH_RATE_MAX_CALLS
    # First `cap` calls allowed
    for i in range(cap):
        assert auth_refresh._rate_limited("198.51.100.7", now=now + i * 0.1) is False
    # (cap+1)-th call is denied
    assert auth_refresh._rate_limited("198.51.100.7", now=now + cap * 0.1) is True
    # After the window slides, allowed again
    later = now + auth_refresh._REFRESH_RATE_WINDOW_SECS + 1
    assert auth_refresh._rate_limited("198.51.100.7", now=later) is False


def test_tr_u_15d_handler_rate_limiter_per_ip_isolation():
    """Rate-limit buckets must not cross-contaminate between source IPs."""
    from kiro_crew.dashboard.handlers import auth_refresh

    auth_refresh._refresh_rate_buckets.pop("203.0.113.1", None)
    auth_refresh._refresh_rate_buckets.pop("203.0.113.2", None)
    now = 2_000_000.0
    cap = auth_refresh._REFRESH_RATE_MAX_CALLS
    # Saturate IP A
    for i in range(cap):
        auth_refresh._rate_limited("203.0.113.1", now=now + i * 0.1)
    assert auth_refresh._rate_limited("203.0.113.1", now=now + cap * 0.1) is True
    # IP B starts fresh — saturating A must not affect B
    assert auth_refresh._rate_limited("203.0.113.2", now=now + cap * 0.1) is False


def test_tr_u_15e_handler_rate_limiter_empty_ip_fails_closed():
    """Empty client_ip MUST fail closed: an empty IP means we cannot
    rate-limit, so we deny outright. Defense-in-depth per review-bot
    security-controls (deny-by-default). Per security-review finding
    #2 on bucketing under a shared sentinel still allowed
    60/min, which contradicted the docstring claim of fail-closed.
    """
    from kiro_crew.dashboard.handlers import auth_refresh

    # First empty-IP call denied (no bucket lookup, immediate deny)
    assert auth_refresh._rate_limited("", now=1.0) is True
    # Second one too — never any allowance for unknown-IP requests
    assert auth_refresh._rate_limited("", now=2.0) is True
    # And they don't pollute any real bucket — a known IP works fine
    assert auth_refresh._rate_limited("198.51.100.99", now=3.0) is False


def test_tr_u_15f_rate_buckets_evict_stale_ips():
    """Regression: the per-IP rate-bucket map must not grow without bound.

    Every distinct source IP that ever hits /api/auth/refresh used to leave a
    permanent entry (an empty deque once its timestamps aged past the window),
    so a wide spread of one-shot client IPs (or a spoofed-XFF pump) slowly
    leaked memory. The periodic sweep must evict stale/empty buckets.
    """
    from kiro_crew.dashboard.handlers import auth_refresh as ar

    # Isolate: clear the map and force a sweep on the next call.
    with ar._refresh_rate_lock:
        ar._refresh_rate_buckets.clear()
        ar._refresh_rate_last_sweep = float("-inf")

    base = 5_000_000.0
    # Two one-shot IPs each record a single call at t=base.
    assert ar._rate_limited("192.0.2.10", now=base) is False
    assert ar._rate_limited("192.0.2.11", now=base) is False
    assert "192.0.2.10" in ar._refresh_rate_buckets
    assert "192.0.2.11" in ar._refresh_rate_buckets

    # Well past the window + sweep interval, a DIFFERENT IP calls in. The sweep
    # must evict the two now-stale buckets (their only timestamp aged out) while
    # keeping the fresh caller.
    later = (
        base
        + ar._REFRESH_RATE_WINDOW_SECS
        + ar._REFRESH_RATE_SWEEP_INTERVAL_SECS
        + 5
    )
    assert ar._rate_limited("192.0.2.99", now=later) is False
    assert "192.0.2.10" not in ar._refresh_rate_buckets
    assert "192.0.2.11" not in ar._refresh_rate_buckets
    assert "192.0.2.99" in ar._refresh_rate_buckets

    # Cleanup so we don't leak state into sibling tests.
    with ar._refresh_rate_lock:
        ar._refresh_rate_buckets.clear()
        ar._refresh_rate_last_sweep = float("-inf")


def test_tr_u_15g_rate_buckets_hard_capped():
    """Backstop bound: once the map is at _REFRESH_RATE_MAX_BUCKETS, a
    previously-unseen source IP is rate-limited (fail-closed) rather than
    admitted by evicting a live bucket. The map never grows past the cap and
    no live bucket is dropped to make room for a newcomer."""
    from kiro_crew.dashboard.handlers import auth_refresh as ar

    with ar._refresh_rate_lock:
        ar._refresh_rate_buckets.clear()
        ar._refresh_rate_last_sweep = float("-inf")

    base = 6_000_000.0
    import collections as _c

    # Fill exactly to the cap with live (recently-active) buckets, and freeze
    # the sweep so only the fail-closed cap check governs new IPs.
    with ar._refresh_rate_lock:
        for i in range(ar._REFRESH_RATE_MAX_BUCKETS):
            ar._refresh_rate_buckets[f"live-{i}"] = _c.deque([base])
        ar._refresh_rate_last_sweep = base + 10 * ar._REFRESH_RATE_SWEEP_INTERVAL_SECS

    # A brand-new IP at capacity is DENIED (fail-closed) and NOT inserted.
    assert ar._rate_limited("172.16.0.1", now=base + 1) is True
    assert "172.16.0.1" not in ar._refresh_rate_buckets
    assert len(ar._refresh_rate_buckets) == ar._REFRESH_RATE_MAX_BUCKETS
    # An already-known live bucket is unaffected — it was never evicted.
    assert "live-0" in ar._refresh_rate_buckets
    assert ar._rate_limited("live-0", now=base + 1) is False

    with ar._refresh_rate_lock:
        ar._refresh_rate_buckets.clear()
        ar._refresh_rate_last_sweep = float("-inf")


def test_tr_u_15h_rate_buckets_capped_per_insertion_between_sweeps():
    """Regression (GPT 5.6 MEDIUM): the hard cap must hold on EVERY insertion,
    not only at the throttled sweep. Fill the map to capacity, freeze the sweep
    (so only the per-insert fail-closed cap check can bound it), then feed a
    burst of brand-new source IPs. The map must never exceed
    _REFRESH_RATE_MAX_BUCKETS."""
    from kiro_crew.dashboard.handlers import auth_refresh as ar

    with ar._refresh_rate_lock:
        ar._refresh_rate_buckets.clear()
        # Pre-fill to exactly the cap with live (recently-active) buckets.
        import collections as _c

        base = 7_000_000.0
        for i in range(ar._REFRESH_RATE_MAX_BUCKETS):
            ar._refresh_rate_buckets[f"cap-{i}"] = _c.deque([base])
        # Freeze the sweep in the future so it CANNOT run during the burst —
        # only the per-insert cap check can keep the map bounded.
        ar._refresh_rate_last_sweep = base + 10 * ar._REFRESH_RATE_SWEEP_INTERVAL_SECS

    # 200 distinct new IPs arrive within the same window (sweep frozen off).
    for i in range(200):
        ar._rate_limited(f"newip-{i}", now=base + 1 + i * 0.001)
        # Invariant checked on every insert: never over the cap.
        assert len(ar._refresh_rate_buckets) <= ar._REFRESH_RATE_MAX_BUCKETS

    with ar._refresh_rate_lock:
        ar._refresh_rate_buckets.clear()
        ar._refresh_rate_last_sweep = float("-inf")


def test_tr_u_15i_saturated_client_cannot_reset_bucket_via_cap_flood():
    """Regression (Arbiter BLOCK / GPT 5.6 MEDIUM): a rate-limited client must
    NOT be able to reset its own bucket by flooding the map to capacity.

    Previously, once the map hit the cap a NEW IP evicted the
    least-recently-active bucket. A saturated attacker never appends a
    timestamp on denied calls, so their bucket froze at exhaustion time and
    became the eviction victim under an XFF / botnet pump — letting them drop
    their own exhausted bucket and re-create a fresh full allowance. The
    fix fails closed: unseen IPs are rejected at the cap, so the attacker's
    exhausted bucket survives and they stay limited until it ages out of the
    window on its own.
    """
    from kiro_crew.dashboard.handlers import auth_refresh as ar

    with ar._refresh_rate_lock:
        ar._refresh_rate_buckets.clear()
        ar._refresh_rate_last_sweep = float("-inf")

    base = 8_000_000.0
    attacker = "203.0.113.7"
    # Attacker exhausts its full allowance within the window.
    for i in range(ar._REFRESH_RATE_MAX_CALLS):
        assert ar._rate_limited(attacker, now=base + i * 0.001) is False
    # The next call is denied — the bucket is saturated.
    assert ar._rate_limited(attacker, now=base + 1.0) is True

    # Freeze the sweep so ageing-out cannot help the attacker, then pump a
    # large flood of distinct source IPs. Under the old eviction-to-admit
    # logic this would evict the attacker's frozen bucket; fail-closed rejects
    # the flood instead and leaves the attacker's bucket intact.
    with ar._refresh_rate_lock:
        ar._refresh_rate_last_sweep = base + 10 * ar._REFRESH_RATE_SWEEP_INTERVAL_SECS
    for i in range(ar._REFRESH_RATE_MAX_BUCKETS * 2):
        ar._rate_limited(f"flood-{i}", now=base + 2.0)

    # The attacker's bucket is still present and still saturated: no reset.
    assert attacker in ar._refresh_rate_buckets
    assert ar._rate_limited(attacker, now=base + 3.0) is True
    assert len(ar._refresh_rate_buckets) <= ar._REFRESH_RATE_MAX_BUCKETS

    with ar._refresh_rate_lock:
        ar._refresh_rate_buckets.clear()
        ar._refresh_rate_last_sweep = float("-inf")


def test_tr_u_15j_new_ip_admitted_at_cap_when_stale_buckets_reclaimable():
    """Regression (Arbiter BLOCK item 2): a legitimate new source IP must be
    ADMITTED — not denied — when the map is at capacity but full of reclaimable
    stale buckets.

    Previously the sweep was throttled to once per window even at capacity, so
    under a sustained flood / trusted-XFF pump (or organic IP churn) that kept
    the map pinned at _REFRESH_RATE_MAX_BUCKETS, a previously-unseen legitimate
    IP was denied /api/auth/refresh for up to a window even though most buckets
    were stale and reclaimable — an availability defect inside an auth control
    surfacing as unexplained forced logouts. The fix invokes the sweep
    UNCONDITIONALLY when an insertion is refused at the cap, reclaiming dead
    space exactly when it matters.
    """
    import collections as _c

    from kiro_crew.dashboard.handlers import auth_refresh as ar

    with ar._refresh_rate_lock:
        ar._refresh_rate_buckets.clear()
        ar._refresh_rate_last_sweep = float("-inf")

    base = 9_000_000.0
    # Fill the map to EXACTLY the cap with STALE buckets — every timestamp is
    # older than the window, so they are all reclaimable by a sweep.
    stale_ts = base - ar._REFRESH_RATE_WINDOW_SECS - 100
    with ar._refresh_rate_lock:
        for i in range(ar._REFRESH_RATE_MAX_BUCKETS):
            ar._refresh_rate_buckets[f"stale-{i}"] = _c.deque([stale_ts])
        # Freeze the interval throttle in the future so ONLY the unconditional
        # cap-refusal sweep can reclaim space — proving the fix, not the throttle.
        ar._refresh_rate_last_sweep = base + 10 * ar._REFRESH_RATE_SWEEP_INTERVAL_SECS

    # A legitimate new IP arrives at capacity. Even though the interval sweep is
    # frozen off, the cap-refusal forces a sweep, the stale buckets are
    # reclaimed, and the newcomer is ADMITTED (not rate-limited).
    assert ar._rate_limited("192.0.2.200", now=base) is False
    assert "192.0.2.200" in ar._refresh_rate_buckets
    # The stale buckets were reclaimed, so the map is no longer pinned at cap.
    assert len(ar._refresh_rate_buckets) < ar._REFRESH_RATE_MAX_BUCKETS

    with ar._refresh_rate_lock:
        ar._refresh_rate_buckets.clear()
        ar._refresh_rate_last_sweep = float("-inf")


def test_tr_u_16_persistence_roundtrip(tmp_path: Path):
    """Writing to disk, reloading into a new manager preserves state."""
    state_file = tmp_path / "rt.json"
    mgr1 = RefreshStateManager(state_path=state_file)
    mgr1.mark_consumed(
        "jti1", chain_id="c1", exp=time.time() + 86400, ip="1.2.3.4", replacement="{}"
    )
    mgr1.revoke_chain("c2", time.time() + 86400)
    assert state_file.exists()
    # Fresh manager reads same file
    mgr2 = RefreshStateManager(state_path=state_file)
    assert mgr2.is_consumed("jti1") is True
    assert mgr2.is_chain_revoked("c2") is True


def test_tr_u_17_persistence_file_mode_0600(tmp_path: Path):
    state_file = tmp_path / "rt.json"
    mgr = RefreshStateManager(state_path=state_file)
    mgr.mark_consumed(
        "jti1", chain_id="c1", exp=time.time() + 86400, ip="1.2.3.4", replacement="{}"
    )
    # Mode is 0o600 (owner read+write only)
    assert state_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(
    os.name == "nt",
    reason="injects the failure via platform_compat.os.chmod, which only "
    "restrict_to_owner's POSIX branch calls (Windows shells out to icacls)",
)
def test_a_failed_lockdown_still_persists_the_reuse_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A lockdown failure must not cost us the consumed-jti record.

    _persist passes restrict_on_error="warn" deliberately. Letting the OSError
    propagate would hit _persist's own outer OSError handler, which logs and
    returns WITHOUT writing -- so a read-only filesystem would silently drop the
    record and reuse detection would fail to fire for that jti after a restart
    (RFC-6819 5.2.2.3). A state file another local user can read is the lesser
    harm, so the write proceeds and warns.
    """
    state_file = tmp_path / "rt.json"
    mgr = RefreshStateManager(state_path=state_file)

    def _boom(*a, **kw):
        raise OSError("chmod denied (read-only filesystem)")

    monkeypatch.setattr("kiro_crew.platform_compat.os.chmod", _boom)
    mgr.mark_consumed(
        "jti1", chain_id="c1", exp=time.time() + 86400, ip="1.2.3.4", replacement="{}"
    )

    # Published despite the failed lockdown, and the record survives a reload.
    assert state_file.exists()
    assert RefreshStateManager(state_path=state_file).is_consumed("jti1") is True


def test_tr_i_17_corrupted_state_file_starts_empty(tmp_path: Path):
    """TR-I-17: corrupted state file is treated as empty rather than crashing.

    Renamed from test_TR_U_18 to match the spec naming — TR-I (integration)
    rather than TR-U (unit). Per review-bot finding (post 34).
    """
    state_file = tmp_path / "rt.json"
    state_file.write_text("not valid json {[")
    mgr = RefreshStateManager(state_path=state_file)
    # Should not raise; should treat as empty
    assert mgr.is_consumed("anything") is False


def test_tr_i_17a_malformed_exp_entry_is_skipped_not_fatal(tmp_path: Path):
    """A single entry with a bad `exp` (valid JSON, non-numeric) must not brick
    the store. float(exp) raises in the constructor's _load, so an unguarded
    coercion made _get_state() — and every /api/auth/refresh call — 500 until
    the file was hand-repaired. The bad entry is dropped; good ones survive."""
    import json

    state_file = tmp_path / "rt.json"
    good_exp = time.time() + 86400
    state_file.write_text(
        json.dumps(
            {
                "consumed_jtis": [
                    {"jti": "bad", "exp": "not-a-number"},
                    {"jti": "alsobad", "exp": None},
                    {"jti": "good", "exp": good_exp},
                ],
                "revoked_chains": [
                    {"chain_id": "badchain", "exp": "nope"},
                    {"chain_id": "goodchain", "exp": good_exp},
                ],
            }
        )
    )
    mgr = RefreshStateManager(state_path=state_file)  # must not raise
    assert mgr.is_consumed("good") is True
    assert mgr.is_consumed("bad") is False
    assert mgr.is_consumed("alsobad") is False
    assert mgr.is_chain_revoked("goodchain") is True
    assert mgr.is_chain_revoked("badchain") is False


def test_tr_u_18_concurrent_writers(tmp_path: Path):
    """TR-U-18: 10 threads each marking a unique jti — no data loss.

    Verifies the file-locked write path in RefreshStateManager.mark_consumed
    serializes correctly under concurrent access. Per spec (and review-bot
    finding post 34).
    """
    state_file = tmp_path / "rt.json"
    mgr = RefreshStateManager(state_path=state_file)

    def mark(i: int) -> None:
        mgr.mark_consumed(
            f"jti_{i}",
            chain_id="c1",
            exp=time.time() + 86400,
            ip="1.2.3.4",
            replacement="{}",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(mark, range(10)))

    # All 10 writes must be persisted — no lost updates.
    for i in range(10):
        assert mgr.is_consumed(f"jti_{i}") is True


# -- Multi-tab grace window (TR-U-20..22) ------------------------------------


def test_tr_u_20_grace_same_ip_returns_cached(isolated_state: RefreshStateManager):
    replacement = json.dumps({"refreshed_at": 123, "_access_token": "X"})
    isolated_state.mark_consumed(
        "jti1",
        chain_id="c1",
        exp=time.time() + 30 * 86400,
        ip="1.2.3.4",
        replacement=replacement,
    )
    cached = isolated_state.grace_replacement("c1", "jti1", "1.2.3.4")
    assert cached == replacement


def test_tr_u_21_grace_outside_window_returns_none(isolated_state: RefreshStateManager):
    isolated_state.mark_consumed(
        "jti1", chain_id="c1", exp=time.time() + 30 * 86400, ip="1.2.3.4", replacement="{}"
    )
    # Simulate request well after grace window
    future = time.time() + REFRESH_GRACE_SECS + 10
    cached = isolated_state.grace_replacement("c1", "jti1", "1.2.3.4", now=future)
    assert cached is None


def test_tr_u_22_grace_different_ip_returns_none(isolated_state: RefreshStateManager):
    isolated_state.mark_consumed(
        "jti1", chain_id="c1", exp=time.time() + 30 * 86400, ip="1.2.3.4", replacement="{}"
    )
    # Different source IP — could be theft, no grace
    cached = isolated_state.grace_replacement("c1", "jti1", "5.6.7.8")
    assert cached is None


def test_tr_u_22a_grace_accepts_only_chain_head(
    isolated_state: RefreshStateManager,
):
    """Chain-head-only grace: after rapid rotations jti1->jti2->jti3 on one
    chain, ONLY the most-recently-rotated jti (the chain head, jti3) may
    authenticate a same-IP in-window replay and be re-served its live
    replacement pair. Any OLDER rotated jti (jti1, jti2) is treated as token
    reuse and returns None so the caller revokes the chain — the undiluted
    RFC 6819 §5.2.2.3 theft signal chosen over a wider multi-jti grace history.
    """
    now = time.time()
    r1 = json.dumps({"refreshed_at": 1, "_access_token": "A1", "_refresh_token": "R1"})
    r2 = json.dumps({"refreshed_at": 2, "_access_token": "A2", "_refresh_token": "R2"})
    r3 = json.dumps({"refreshed_at": 3, "_access_token": "A3", "_refresh_token": "R3"})
    isolated_state.mark_consumed(
        "jti1", chain_id="c1", exp=now + 30 * 86400, ip="1.2.3.4", replacement=r1
    )
    isolated_state.mark_consumed(
        "jti2", chain_id="c1", exp=now + 30 * 86400, ip="1.2.3.4", replacement=r2
    )
    isolated_state.mark_consumed(
        "jti3", chain_id="c1", exp=now + 30 * 86400, ip="1.2.3.4", replacement=r3
    )

    # (a) The chain head (newest consumed jti) replays successfully, served its
    # own live replacement pair.
    assert isolated_state.grace_replacement("c1", "jti3", "1.2.3.4") == r3

    # (b) Older rotated jtis are NO LONGER accepted -> None -> caller revokes
    # the chain (undiluted reuse signal).
    assert isolated_state.grace_replacement("c1", "jti1", "1.2.3.4") is None
    assert isolated_state.grace_replacement("c1", "jti2", "1.2.3.4") is None

    # IP mismatch on the head is still refused (theft posture unchanged).
    assert isolated_state.grace_replacement("c1", "jti3", "9.9.9.9") is None

    # Beyond the grace window even the head stops replaying.
    future = now + REFRESH_GRACE_SECS + 5
    assert isolated_state.grace_replacement("c1", "jti3", "1.2.3.4", now=future) is None


def test_tr_u_22a2_older_rotated_jti_triggers_reuse_not_replay(
    isolated_state: RefreshStateManager,
):
    """Reuse-signal regression for chain-head-only grace.

    Model the real handler contract: consuming jtiN records a replacement
    carrying the NEXT minted jti (the token the client presents next). After
    jti1->jti2->jti3->jti4 (jti4 == current live head, not yet consumed), ONLY
    the head jti (jti3, the last consumed) may replay, and it is served the
    live jti4 pair. Every OLDER consumed jti (jti1, jti2) returns None so the
    handler revokes the chain — an attacker replaying a stale captured jti can
    no longer resolve to a live session inside the window, and the served pair
    is always the live head (never an already-consumed token).
    """
    now = time.time()
    for cur, nxt in (("jti1", "jti2"), ("jti2", "jti3"), ("jti3", "jti4")):
        isolated_state.mark_consumed(
            cur,
            chain_id="c1",
            exp=now + 30 * 86400,
            ip="1.2.3.4",
            replacement=json.dumps({"_refresh_token": nxt}),
        )

    # Only the head (jti3, last consumed) replays; it is served the live jti4.
    served = isolated_state.grace_replacement("c1", "jti3", "1.2.3.4")
    assert served is not None
    assert json.loads(served)["_refresh_token"] == "jti4"

    # Older consumed jtis are rejected -> reuse signal (chain revocation).
    assert isolated_state.grace_replacement("c1", "jti1", "1.2.3.4") is None
    assert isolated_state.grace_replacement("c1", "jti2", "1.2.3.4") is None


def test_tr_u_22b_single_refresh_race_still_absorbed(
    isolated_state: RefreshStateManager,
):
    """The original false-revocation bug fix still holds under chain-head-only.

    A single tab whose refresh POST is duplicated (network retry / double
    fire) presents the SAME just-consumed head jti twice. The duplicate must
    be recognised as a benign race and re-served the same replacement pair —
    NOT revoked. Only the head is retained, so this single-refresh race (the
    primary bug this mechanism targets) is unaffected by the tightening, and
    re-serving stays idempotent.
    """
    now = time.time()
    head = json.dumps({"refreshed_at": 1, "_access_token": "A", "_refresh_token": "R"})
    isolated_state.mark_consumed(
        "jtiH", chain_id="c1", exp=now + 30 * 86400, ip="1.2.3.4", replacement=head
    )

    # First replay of the just-consumed head: served the replacement (no revoke).
    assert isolated_state.grace_replacement("c1", "jtiH", "1.2.3.4") == head
    # Idempotent: a second duplicate within the window is served the same pair.
    assert isolated_state.grace_replacement("c1", "jtiH", "1.2.3.4") == head


def test_tr_u_22c_grace_isolated_per_chain(isolated_state: RefreshStateManager):
    """A jti from one chain must never resolve under a different chain_id."""
    now = time.time()
    isolated_state.mark_consumed(
        "jtiA", chain_id="cA", exp=now + 30 * 86400, ip="1.2.3.4", replacement="A"
    )
    isolated_state.mark_consumed(
        "jtiB", chain_id="cB", exp=now + 30 * 86400, ip="1.2.3.4", replacement="B"
    )
    assert isolated_state.grace_replacement("cA", "jtiA", "1.2.3.4") == "A"
    assert isolated_state.grace_replacement("cB", "jtiA", "1.2.3.4") is None


# -- Cookie name helper -------------------------------------------------------


def test_refresh_cookie_name_per_port():
    """Mirrors the existing access cookie's per-port pattern."""
    assert refresh_cookie_name(7777) == "mc_refresh_7777"
    assert refresh_cookie_name("5555") == "mc_refresh_5555"


# -- Foreign-port cookie pruning (cookie-jar overflow, issue #610) ------------


def test_foreign_port_cookies_selects_other_ports_with_matching_paths():
    """Other-port access/refresh cookies are returned with the path each was
    set with (access="/", refresh="/api/auth") so a max_age=0 Set-Cookie
    actually deletes them (cookie deletion is path-sensitive)."""
    jar = [
        "mc_token_7777",
        "mc_refresh_7777",
        "mc_token_5599",
        "mc_refresh_5599",
        "mc_token_6821",
    ]
    stale = foreign_port_cookies(jar, 7777)
    assert set(stale) == {
        ("mc_token_5599", "/"),
        ("mc_refresh_5599", "/api/auth"),
        ("mc_token_6821", "/"),
    }


def test_foreign_port_cookies_preserves_current_port():
    """The current port's own pair must never be expired."""
    stale = foreign_port_cookies(["mc_token_7777", "mc_refresh_7777"], 7777)
    assert stale == []
    # Current port passed as int or str resolves identically.
    assert foreign_port_cookies(["mc_token_7777"], "7777") == []


def test_foreign_port_cookies_ignores_non_port_names():
    """Legacy 'mc_token' (no suffix), non-digit suffixes, and unrelated
    cookies must be left untouched — only digit-suffixed per-port names
    are pruned."""
    jar = [
        "mc_token",  # legacy, pre-per-port
        "mc_token_abc",  # non-digit suffix
        "session",  # unrelated
        "mc_refresh_",  # empty suffix
        "mc_token_5599",  # genuine other port -> only this one
    ]
    stale = foreign_port_cookies(jar, 7777)
    assert stale == [("mc_token_5599", "/")]


def test_cookie_jar_needs_pruning_threshold():
    """The size gate is False for a small jar (live gateways coexist) and True
    once the approximate Cookie header size crosses the threshold."""
    from kiro_crew.dashboard.refresh_tokens import COOKIE_JAR_PRUNE_THRESHOLD_BYTES

    small = {"mc_token_7777": "abc", "mc_refresh_7777": "def"}
    assert cookie_jar_needs_pruning(small) is False

    # One oversized value pushes the jar past the threshold.
    big = {"mc_token_7777": "x" * (COOKIE_JAR_PRUNE_THRESHOLD_BYTES + 1)}
    assert cookie_jar_needs_pruning(big) is True

    assert cookie_jar_needs_pruning({}) is False


# -- Atomic write under crash (TR-U-19) — best-effort smoke test --------------


def test_tr_u_19_atomic_write_no_partial_state(tmp_path: Path):
    """Concurrent writes don't produce a partial file readable as truncated state."""
    state_file = tmp_path / "rt.json"
    mgr = RefreshStateManager(state_path=state_file)
    # Write a big payload
    for i in range(100):
        mgr.mark_consumed(
            f"jti{i}",
            chain_id="c1",
            exp=time.time() + 86400,
            ip="1.2.3.4",
            replacement="{}",
        )
    # File should always parse cleanly (atomic-rename pattern)
    raw = state_file.read_text(encoding="utf-8")
    data = json.loads(raw)  # no JSONDecodeError
    assert len(data["consumed_jtis"]) == 100


# -- Logout endpoint (TR-U-23..24) -------------------------------------------


def test_tr_u_23_logout_revokes_chain_and_clears_cookies(
    isolated_state: RefreshStateManager,
):
    """POST /api/auth/logout must revoke the refresh chain AND clear both
    cookies, so a stolen refresh cookie cannot survive logout. Per
    security-review finding #3.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import auth_refresh as ar

    # Mint a real refresh token so validate_refresh_token accepts it
    token, chain_id, _jti, _exp = generate_refresh_token("alice")
    cookie_name = refresh_cookie_name(7777)

    request = MagicMock(spec=web.Request)
    request.app = {"port": 7777, "allowed_origins": set()}
    request.cookies = {cookie_name: token}
    request.headers = {"Origin": "http://localhost:7777", "Host": "localhost:7777"}
    request.scheme = "http"
    request.host = "localhost:7777"
    request.remote = "127.0.0.1"

    # check_origin must accept loopback — patch it tight to True
    with patch("kiro_crew.dashboard.handlers.auth_refresh.check_origin", return_value=True):
        import asyncio
        resp = asyncio.run(ar.api_auth_logout(request))

    # Chain revoked
    assert isolated_state.is_chain_revoked(chain_id) is True

    # Both cookies cleared on the response (max_age=0, stored as string by SimpleCookie)
    assert "mc_refresh_7777" in resp.cookies
    assert "mc_token_7777" in resp.cookies
    assert int(resp.cookies["mc_refresh_7777"]["max-age"]) == 0
    assert int(resp.cookies["mc_token_7777"]["max-age"]) == 0


def test_tr_u_24_logout_without_cookie_still_clears(
    isolated_state: RefreshStateManager,
):
    """POST /api/auth/logout with no refresh cookie must still clear cookies
    (user intent is 'log me out') and respond 200, not 401. The lack of a
    refresh cookie just means there's no chain to revoke.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import auth_refresh as ar

    request = MagicMock(spec=web.Request)
    request.app = {"port": 7777, "allowed_origins": set()}
    request.cookies = {}  # no refresh cookie
    request.headers = {"Origin": "http://localhost:7777", "Host": "localhost:7777"}
    request.scheme = "http"
    request.host = "localhost:7777"
    request.remote = "127.0.0.1"

    with patch("kiro_crew.dashboard.handlers.auth_refresh.check_origin", return_value=True):
        import asyncio
        resp = asyncio.run(ar.api_auth_logout(request))

    assert resp.status == 200
    # Both cookies still cleared
    assert "mc_refresh_7777" in resp.cookies
    assert "mc_token_7777" in resp.cookies
    assert int(resp.cookies["mc_refresh_7777"]["max-age"]) == 0
    assert int(resp.cookies["mc_token_7777"]["max-age"]) == 0


# -- Conditional Secure flag (TR-U-25) ---------------------------------------


def test_tr_u_25_secure_flag_only_on_https():
    """Cookies must set Secure=True only when the request is HTTPS. Localhost
    HTTP must not set it (browser would refuse to send it back). Per the security reviewer
    finding #5 on. Forward-compatible for KiroCrew OSS behind
    a real HTTPS reverse proxy.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import auth_refresh as ar

    # HTTP request: Secure should NOT be set
    http_resp = web.Response()
    http_req = MagicMock(spec=web.Request)
    http_req.app = {"port": 7777}
    http_req.scheme = "http"
    http_req.host = "localhost:7777"
    http_req.headers = {}
    http_req.remote = "127.0.0.1"
    ar._set_access_cookie(http_resp, http_req, "tok", time.time() + 3600)
    ar._set_refresh_cookie(http_resp, http_req, "rt", time.time() + 86400)
    # SimpleCookie morsel: 'secure' attr is "" (empty string) when unset, truthy when set
    assert not http_resp.cookies["mc_token_7777"]["secure"]
    assert not http_resp.cookies["mc_refresh_7777"]["secure"]

    # HTTPS request: Secure MUST be set on both cookies
    https_resp = web.Response()
    https_req = MagicMock(spec=web.Request)
    https_req.app = {"port": 443}
    https_req.scheme = "https"
    https_req.host = "kirocrew.example.com"
    https_req.headers = {}
    https_req.remote = "127.0.0.1"
    ar._set_access_cookie(https_resp, https_req, "tok", time.time() + 3600)
    ar._set_refresh_cookie(https_resp, https_req, "rt", time.time() + 86400)
    assert https_resp.cookies["mc_token_443"]["secure"]
    assert https_resp.cookies["mc_refresh_443"]["secure"]


def test_tr_u_25b_secure_flag_via_forwarded_proto_over_tunnel():
    """Behind a TLS-terminating tunnel/proxy the gateway sees plain HTTP on
    loopback but the browser connection is HTTPS. X-Forwarded-Proto=https from
    a loopback peer MUST cause Secure=True — otherwise the wss:// dashboard
    WebSocket is denied the cookie and the dashboard flaps online/offline. A
    spoofed header from a NON-loopback peer must NOT flip Secure on.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import auth_refresh as ar

    # Tunnel: scheme=http on loopback, XFP=https -> Secure MUST be set
    tun_resp = web.Response()
    tun_req = MagicMock(spec=web.Request)
    tun_req.app = {"port": 7777}
    tun_req.scheme = "http"
    tun_req.host = "kirocrew.example.com"
    tun_req.headers = {"X-Forwarded-Proto": "https"}
    tun_req.remote = "127.0.0.1"
    ar._set_access_cookie(tun_resp, tun_req, "tok", time.time() + 3600)
    ar._set_refresh_cookie(tun_resp, tun_req, "rt", time.time() + 86400)
    assert tun_resp.cookies["mc_token_7777"]["secure"]
    assert tun_resp.cookies["mc_refresh_7777"]["secure"]

    # Spoofed XFP from a non-loopback peer -> Secure MUST NOT be set
    spoof_resp = web.Response()
    spoof_req = MagicMock(spec=web.Request)
    spoof_req.app = {"port": 7777}
    spoof_req.scheme = "http"
    spoof_req.host = "localhost:7777"
    spoof_req.headers = {"X-Forwarded-Proto": "https"}
    spoof_req.remote = "10.0.0.5"
    ar._set_access_cookie(spoof_resp, spoof_req, "tok", time.time() + 3600)
    assert not spoof_resp.cookies["mc_token_7777"]["secure"]


# -- Cookie path scope (TR-U-26) ---------------------------------------------


def test_tr_u_26_refresh_cookie_path_covers_logout():
    """The refresh cookie's Path attribute MUST cover /api/auth/logout.

    Live test on 2026-06-18 caught this: cookie was scoped Path=/api/auth/refresh,
    so browsers/curl don't send it to /api/auth/logout (path prefix doesn't match).
    Logout silently no-opped: server saw 'no_cookie', returned 200 logged_out:true,
    but never called revoke_chain. A subsequent refresh on the same cookie still
    succeeded because the chain was alive.

    This regression test asserts the cookie's Path is "/api/auth" (covers BOTH
    /refresh AND /logout) and verifies a concrete RFC 6265 path-match for /logout.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard import refresh_tokens
    from kiro_crew.dashboard.handlers import auth_refresh as ar

    # The constant itself must scope to /api/auth (one segment broader than
    # /api/auth/refresh) so /api/auth/logout is included.
    assert refresh_tokens.REFRESH_COOKIE_PATH == "/api/auth", (
        f"Refresh cookie path must be '/api/auth' to cover both refresh and "
        f"logout endpoints. Got: {refresh_tokens.REFRESH_COOKIE_PATH!r}"
    )

    # Concrete check: when we set the cookie, the Path morsel must match
    # /api/auth/logout per RFC 6265 §5.1.4 (request path starts with cookie path
    # AND next char is "/" or end-of-path).
    resp = web.Response()
    req = MagicMock(spec=web.Request)
    req.app = {"port": 7777}
    req.scheme = "http"
    req.host = "localhost:7777"
    ar._set_refresh_cookie(resp, req, "rt", time.time() + 86400)
    cookie_path = resp.cookies["mc_refresh_7777"]["path"]
    assert cookie_path == "/api/auth", (
        f"Cookie Path attribute must be '/api/auth'. Got: {cookie_path!r}"
    )

    # RFC 6265 path-match: cookie path "/api/auth" matches BOTH
    # request-paths "/api/auth/refresh" AND "/api/auth/logout".
    for request_path in ("/api/auth/refresh", "/api/auth/logout", "/api/auth/me"):
        assert request_path.startswith(cookie_path), (
            f"path-match failed: cookie path {cookie_path!r} does not cover "
            f"{request_path!r}"
        )
        # Next char after the cookie path prefix must be "/" or end-of-string
        rest = request_path[len(cookie_path):]
        assert rest == "" or rest.startswith("/"), (
            f"RFC 6265 §5.1.4 path-match: cookie path {cookie_path!r} prefix "
            f"of {request_path!r} but not a path boundary"
        )

    # Negative: cookie must NOT leak to unrelated paths
    for request_path in ("/api/chat", "/dashboard", "/"):
        is_match = (
            request_path.startswith(cookie_path)
            and (request_path[len(cookie_path):] == ""
                 or request_path[len(cookie_path):].startswith("/"))
        )
        assert not is_match, (
            f"Cookie path {cookie_path!r} unexpectedly leaks to {request_path!r}"
        )


# -- Per-session access-cookie revocation on logout (CWE-613) ----------------


def test_tr_u_27_logout_revokes_access_cookie(tmp_path, monkeypatch):
    """Reproduces the pentest finding: after POST /api/auth/logout, replaying
    the saved access cookie must be REJECTED (was 200 = still valid before the
    fix). Closes CWE-613 for the self-contained access token.
    """
    import asyncio
    from unittest.mock import MagicMock

    from aiohttp import web

    import kiro_crew.dashboard.token_auth as ta
    from kiro_crew.dashboard.handlers import auth_refresh as ar
    from kiro_crew.dashboard.token_auth import generate_token, validate_token

    # Isolate BOTH the refresh store and the token_auth revoked-nonce store to
    # tmp dirs so nothing touches the real ~/.kirocrew.
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(ta, "_REVOCATION_GEN", 0)
    monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    refresh_state = RefreshStateManager(state_path=tmp_path / "refresh_chains.json")

    # Establish a session: a real access cookie + a real refresh cookie.
    access_token = generate_token("alice", ttl_seconds=MAX_REFRESH_TTL_SECS)
    refresh_token, chain_id, _jti, _exp = generate_refresh_token("alice")

    # Cookie is valid BEFORE logout (the pentest "confirm valid" step).
    assert validate_token(access_token, use_session_exp=True)[0] is True

    request = MagicMock(spec=web.Request)
    request.app = {"port": 7777, "allowed_origins": set()}
    request.cookies = {
        "mc_token_7777": access_token,
        refresh_cookie_name(7777): refresh_token,
    }
    request.headers = {"Origin": "http://localhost:7777", "Host": "localhost:7777"}
    request.scheme = "http"
    request.host = "localhost:7777"
    request.remote = "127.0.0.1"

    with patch(
        "kiro_crew.dashboard.refresh_tokens._state_singleton", refresh_state
    ), patch(
        "kiro_crew.dashboard.handlers.auth_refresh.check_origin", return_value=True
    ):
        resp = asyncio.run(ar.api_auth_logout(request))

    assert resp.status == 200
    # Refresh chain revoked (pre-existing behaviour) ...
    assert refresh_state.is_chain_revoked(chain_id) is True
    # ... AND the access cookie is now rejected on replay (the fix).
    ok, _uid, reason = validate_token(access_token, use_session_exp=True)
    assert ok is False
    assert reason == "session revoked"


# -- Refresh endpoint trims the shared cookie jar (issue #610) ----------------


def test_refresh_expires_foreign_port_cookies_keeps_current(
    isolated_state: RefreshStateManager,
):
    """POST /api/auth/refresh must expire other-port mc_token_*/mc_refresh_*
    cookies (max_age=0 with the matching path) so the shared 127.0.0.1 jar
    self-trims, while re-setting the CURRENT port's pair with a live TTL.
    Without this the per-port cookies accumulate until the Cookie header
    exceeds aiohttp's max_field_size and every request 400s (LineTooLong).
    """
    import asyncio
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import auth_refresh as ar

    token, _chain_id, _jti, _exp = generate_refresh_token("alice")

    request = MagicMock(spec=web.Request)
    request.app = {"port": 7777, "allowed_origins": set()}
    request.cookies = {
        refresh_cookie_name(7777): token,
        # Stale pairs left by gateways on other ports, sized so the whole jar
        # exceeds COOKIE_JAR_PRUNE_THRESHOLD_BYTES and the size gate fires.
        "mc_token_5599": "x" * 2500,
        "mc_refresh_5599": "y" * 2500,
        "mc_token_6821": "z" * 2500,
    }
    request.headers = {"Origin": "http://localhost:7777", "Host": "localhost:7777"}
    request.scheme = "http"
    request.host = "localhost:7777"
    request.remote = "127.0.0.1"

    with patch(
        "kiro_crew.dashboard.handlers.auth_refresh.check_origin", return_value=True
    ), patch(
        "kiro_crew.dashboard.handlers.auth_refresh._rate_limited", return_value=False
    ):
        resp = asyncio.run(ar.api_auth_refresh(request))

    assert resp.status == 200
    # Current port's pair re-issued with a live TTL (not expired).
    assert int(resp.cookies["mc_token_7777"]["max-age"]) > 0
    assert int(resp.cookies["mc_refresh_7777"]["max-age"]) > 0
    # Foreign-port cookies expired with the path each was set with.
    assert int(resp.cookies["mc_token_5599"]["max-age"]) == 0
    assert resp.cookies["mc_token_5599"]["path"] == "/"
    assert int(resp.cookies["mc_refresh_5599"]["max-age"]) == 0
    assert resp.cookies["mc_refresh_5599"]["path"] == "/api/auth"
    assert int(resp.cookies["mc_token_6821"]["max-age"]) == 0
    assert resp.cookies["mc_token_6821"]["path"] == "/"


def test_refresh_leaves_small_jar_untouched(
    isolated_state: RefreshStateManager,
):
    """With a small cookie jar (e.g. two live gateways sharing a browser), the
    refresh endpoint must NOT expire the other port's cookies — pruning only
    fires once the jar approaches the header limit."""
    import asyncio
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import auth_refresh as ar

    token, _chain_id, _jti, _exp = generate_refresh_token("alice")

    request = MagicMock(spec=web.Request)
    request.app = {"port": 7777, "allowed_origins": set()}
    request.cookies = {
        refresh_cookie_name(7777): token,
        # A second LIVE gateway's cookies — small jar, must be preserved.
        "mc_token_6821": "small",
        "mc_refresh_6821": "small",
    }
    request.headers = {"Origin": "http://localhost:7777", "Host": "localhost:7777"}
    request.scheme = "http"
    request.host = "localhost:7777"
    request.remote = "127.0.0.1"

    with patch(
        "kiro_crew.dashboard.handlers.auth_refresh.check_origin", return_value=True
    ), patch(
        "kiro_crew.dashboard.handlers.auth_refresh._rate_limited", return_value=False
    ):
        resp = asyncio.run(ar.api_auth_refresh(request))

    assert resp.status == 200
    # The other live gateway's cookies were not touched (no expiry Set-Cookie).
    assert "mc_token_6821" not in resp.cookies
    assert "mc_refresh_6821" not in resp.cookies
