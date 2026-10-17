"""Supervision of ``playwright-cli show``: loopback bind, health, lifecycle.

This module pins the relay's authorization and its cached listener proofs, the loopback
argv and the binding report, health, the free port, start, the relay and status. Port
ownership is pinned in ``test_browser_cli_view_port_owner.py`` (the owner lookup) and
``test_browser_cli_view_child_proof.py`` (what the spawned child itself proves); the shared
helpers live in ``browser_cli_view_helpers.py``.
"""

from __future__ import annotations

import contextlib
import http.server
import socket
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from browser_cli_view_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    FakeProc,
    _child_record,
    _FakeClock,
    _linux_shape,
    _recorded_child_port,
    _recorded_info,
    _recorded_proc,
    _stub_port_owner,
    reset_state_fixture,
)

from kiro_crew import platform_compat
from kiro_crew.browser_cli import view as mod
from kiro_crew.testing.wait import wait_until


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _record_relay_target(
    monkeypatch: pytest.MonkeyPatch,
    proc: FakeProc,
    port: int,
    token: str = "relay-capability",
) -> None:
    monkeypatch.setattr(mod, "_child", _child_record(proc, port))
    monkeypatch.setattr(mod, "_relay_token", token)


def test_relay_target_invalidates_token_when_child_is_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(alive=False)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind((mod.LOOPBACK_HOST, 0))
        squatter.listen()
        port = int(squatter.getsockname()[1])
        _record_relay_target(monkeypatch, proc, port)

        assert mod.relay_target() is None
        assert mod._relay_token is None
        assert _recorded_info() is None
        assert _recorded_child_port() is None
        # The same live listener cannot inherit the dead child's capability.
        assert mod.relay_target() is None


def test_relay_target_preserves_state_when_ownership_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc()
    port = 45613
    _record_relay_target(monkeypatch, proc, port)
    monkeypatch.setattr(mod, "_root_process_identity_matches", lambda child, phase: None)
    monkeypatch.setattr(
        mod,
        "_verify_child_listener",
        lambda *args, **kwargs: pytest.fail("inconclusive root identity must stop the proof"),
    )

    assert mod.relay_target() is None
    assert _recorded_proc() is proc
    assert _recorded_info() == mod.ShowInfo("http://127.0.0.1:45613", port)
    assert _recorded_child_port() == port
    assert mod._relay_token == "relay-capability"


def test_relay_target_returns_pair_when_current_ownership_is_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc()
    port = 45613
    _record_relay_target(monkeypatch, proc, port)
    monkeypatch.setattr(mod, "_root_process_identity_matches", lambda child, phase: True)
    monkeypatch.setattr(
        mod,
        "_verify_child_listener",
        lambda child, child_port, *, allow_report, proof_not_before=None: (True, False),
    )
    monkeypatch.setattr(
        mod,
        "_healthy",
        lambda child_port: pytest.fail("relay target lookup must not make an HTTP health probe"),
    )

    assert mod.relay_target() == (port, "relay-capability")


def test_relay_authorize_never_probes_for_a_mismatched_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pre-auth cost finding: the constant-time token comparison comes
    # FIRST, so an unauthenticated bad-token flood can never buy the OS-level
    # process/listener probes (which run under the supervisor lock and would
    # serialize gateway work).
    proc = FakeProc()
    port = 45613
    _record_relay_target(monkeypatch, proc, port)
    monkeypatch.setattr(
        mod,
        "_root_process_identity_matches",
        lambda child, phase: pytest.fail("ownership probe ran before token validation"),
    )
    monkeypatch.setattr(
        mod,
        "_verify_child_listener",
        lambda *args, **kwargs: pytest.fail("listener probe ran before token validation"),
    )

    assert mod.relay_authorize("WRONG-token") == ("token_mismatch", None)
    # A mismatch is not an ownership failure: recorded state survives intact.
    assert _recorded_proc() is proc
    assert mod._relay_token == "relay-capability"
    assert _recorded_child_port() == port


def test_relay_authorize_answers_view_down_without_probing_when_nothing_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "_child", None)
    monkeypatch.setattr(mod, "_relay_token", None)
    monkeypatch.setattr(
        mod,
        "_root_process_identity_matches",
        lambda child, phase: pytest.fail("ownership probe ran with nothing recorded"),
    )

    assert mod.relay_authorize("anything") == ("view_down", None)


def test_relay_authorize_refuses_as_busy_instead_of_parking_on_a_held_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The availability finding: ensure_running holds the supervisor lock
    # across its startup poll (up to 30s). Invalid candidates never reach the
    # lock at all (see the instant-refusal test below), so the bound protects
    # the callers who remain: even the RIGHT token must be refused within the
    # bound — never parked on the shared pool — while a start holds the lock,
    # because the instance that minted that token is being replaced.
    proc = FakeProc()
    port = 45613
    _record_relay_target(monkeypatch, proc, port)
    monkeypatch.setattr(mod, "_AUTHORIZE_ACQUIRE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        mod,
        "_root_process_identity_matches",
        lambda child, phase: pytest.fail("ownership probe ran for a busy refusal"),
    )
    monkeypatch.setattr(
        mod,
        "_verify_child_listener",
        lambda *args, **kwargs: pytest.fail("listener probe ran for a busy refusal"),
    )

    assert mod._lock.acquire()
    try:
        started = time.monotonic()
        # Even the RIGHT token is refused while the lock is held: a start in
        # progress is replacing the instance the token belongs to.
        outcome = mod.relay_authorize("relay-capability")
        elapsed = time.monotonic() - started
    finally:
        mod._lock.release()

    assert outcome == ("busy", None)
    assert elapsed < 1.0
    # Busy is a wait verdict, not a proof: recorded state survives intact.
    assert _recorded_proc() is proc
    assert mod._relay_token == "relay-capability"
    assert _recorded_child_port() == port


def test_relay_authorize_returns_port_and_probes_after_token_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc()
    port = 45613
    probed: list[str] = []
    _record_relay_target(monkeypatch, proc, port)

    def _identity(child: object, phase: str) -> bool:
        probed.append("identity")
        return True

    def _listener(
        child: object,
        child_port: int,
        *,
        allow_report: bool,
        proof_not_before: float | None = None,
    ) -> tuple[bool, bool]:
        probed.append("listener")
        return True, False

    monkeypatch.setattr(mod, "_root_process_identity_matches", _identity)
    monkeypatch.setattr(mod, "_verify_child_listener", _listener)
    monkeypatch.setattr(
        mod,
        "_healthy",
        lambda child_port: pytest.fail("relay authorization must not make an HTTP health probe"),
    )

    assert mod.relay_authorize("relay-capability") == ("ok", port)
    # The proof DID run — for the token holder, and only after the match.
    assert probed == ["identity", "listener"]


def test_relay_authorize_matched_token_dead_child_tears_down_and_invalidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same payoff as relay_target's teardown, reached through the authorize
    # path: a matched token whose child died definitively kills the recorded
    # state and the token itself before a squatter can inherit the port.
    proc = FakeProc(alive=False)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind((mod.LOOPBACK_HOST, 0))
        squatter.listen()
        port = int(squatter.getsockname()[1])
        _record_relay_target(monkeypatch, proc, port)

        assert mod.relay_authorize("relay-capability") == ("ownership_unproven", None)
        assert mod._relay_token is None
        assert _recorded_info() is None
        assert _recorded_child_port() is None
        # The invalidated capability now compares against nothing: the same
        # token answers view_down, and the squatter never becomes reachable.
        assert mod.relay_authorize("relay-capability") == ("view_down", None)


def test_relay_authorize_matched_token_inconclusive_preserves_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc()
    port = 45613
    _record_relay_target(monkeypatch, proc, port)
    monkeypatch.setattr(mod, "_root_process_identity_matches", lambda child, phase: None)
    monkeypatch.setattr(
        mod,
        "_verify_child_listener",
        lambda *args, **kwargs: pytest.fail("inconclusive root identity must stop the proof"),
    )

    assert mod.relay_authorize("relay-capability") == ("ownership_unproven", None)
    # Withheld, not destroyed: a later request may retry once the proof can
    # complete.
    assert _recorded_proc() is proc
    assert mod._relay_token == "relay-capability"
    assert _recorded_child_port() == port


def test_relay_authorize_refuses_invalid_candidates_instantly_while_lock_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pre-auth executor-exhaustion finding: the earlier design compared
    # the token UNDER the lock, so every bad-token request arriving during a
    # start window still parked on the bounded acquire — a flood multiplied
    # that bound into exhaustion of the shared thread pool. The lock-free
    # pre-check refuses an invalid candidate without touching the lock: held
    # or not, it answers immediately.
    proc = FakeProc()
    port = 45613
    _record_relay_target(monkeypatch, proc, port)
    # Deliberately NOT shortened: the refusal must not depend on the bound.
    assert mod._AUTHORIZE_ACQUIRE_TIMEOUT_S >= 1.0
    monkeypatch.setattr(
        mod,
        "_root_process_identity_matches",
        lambda child, phase: pytest.fail("ownership probe ran for an invalid candidate"),
    )

    assert mod._lock.acquire()
    try:
        started = time.monotonic()
        mismatch = mod.relay_authorize("WRONG-token")
        monkeypatch.setattr(mod, "_relay_token", None)
        down = mod.relay_authorize("anything")
        elapsed = time.monotonic() - started
    finally:
        mod._lock.release()

    assert mismatch == ("token_mismatch", None)
    # The first-start window (nothing recorded yet) is the likeliest flood
    # target, and it answers just as instantly.
    assert down == ("view_down", None)
    # No bounded wait was paid for either: the pool thread frees immediately.
    assert elapsed < 0.5


def test_relay_authorize_runs_ownership_probes_outside_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The serialization finding: the probes spawn ps/lsof and cost tens of
    # milliseconds, and running them under the supervisor lock queued every
    # concurrent asset fetch behind one request's probes (and behind the
    # status poll's own hold). They must run with the lock RELEASED, on a
    # snapshot taken under one hold.
    proc = FakeProc()
    port = 45613
    _record_relay_target(monkeypatch, proc, port)

    def _identity(child: object, phase: str) -> bool:
        if not mod._lock.acquire(blocking=False):
            pytest.fail("root-identity probe ran under the supervisor lock")
        mod._lock.release()
        return True

    def _listener(
        child: object,
        child_port: int,
        *,
        allow_report: bool,
        proof_not_before: float | None = None,
    ) -> tuple[bool, bool]:
        if not mod._lock.acquire(blocking=False):
            pytest.fail("listener probe ran under the supervisor lock")
        mod._lock.release()
        return True, False

    monkeypatch.setattr(mod, "_root_process_identity_matches", _identity)
    monkeypatch.setattr(mod, "_verify_child_listener", _listener)

    assert mod.relay_authorize("relay-capability") == ("ok", port)
    # relay_target shares the helper and the contract: same probes, same
    # lock-free execution, one consistent snapshot.
    assert mod.relay_target() == (port, "relay-capability")


def test_relay_authorize_definitive_failure_spares_a_replaced_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The proofs run outside the lock on a snapshot, so by the time one
    # fails definitively a stop/start cycle may have recorded a NEW child.
    # Tearing down blindly would kill the new instance's target on the
    # strength of the old one's corpse: the teardown re-checks under the
    # lock that the state still describes the instance that failed.
    old = FakeProc()
    port = 45613
    _record_relay_target(monkeypatch, old, port)
    replacement = FakeProc()

    def _identity(child: object, phase: str) -> bool:
        # A restart completes while the old snapshot's proof is in flight.
        mod._child = _child_record(replacement, port + 1)
        mod._relay_token = "fresh-capability"
        return False

    monkeypatch.setattr(mod, "_root_process_identity_matches", _identity)

    assert mod.relay_authorize("relay-capability") == ("ownership_unproven", None)
    # The replacement instance's recorded target survives intact.
    assert _recorded_proc() is replacement
    assert mod._relay_token == "fresh-capability"
    assert _recorded_child_port() == port + 1


def test_concurrent_provers_are_serialized_and_share_one_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The per-proc identity-proof slot protocol (record→take, cleared on
    # entry) tolerates exactly one prover at a time. Two provers must
    # serialize on the gate, and the one that waited must consume the
    # leader's cached verdict instead of proving again.
    proc = FakeProc()
    port = 45613
    entered = threading.Event()
    release = threading.Event()
    runs: list[str] = []
    inside = threading.Lock()

    def _gated(child: object, child_port: int, *, allow_report: bool) -> tuple[bool | None, bool]:
        assert inside.acquire(blocking=False), "two provers ran concurrently"
        try:
            runs.append("proof")
            entered.set()
            assert release.wait(timeout=5)
            return True, False
        finally:
            inside.release()

    monkeypatch.setattr(mod, "_verify_child_listener_gated", _gated)
    results: list[tuple[bool | None, bool]] = []

    def _call() -> None:
        results.append(mod._verify_child_listener(proc, port, allow_report=False))

    leader = threading.Thread(target=_call)
    waiter = threading.Thread(target=_call)
    leader.start()
    assert entered.wait(timeout=5)
    waiter.start()
    # The waiter must park at the gate while the leader is mid-proof.
    waiter.join(timeout=0.2)
    assert waiter.is_alive(), "second prover was not serialized behind the gate"
    assert runs == ["proof"]
    release.set()
    leader.join(timeout=5)
    waiter.join(timeout=5)
    assert not leader.is_alive() and not waiter.is_alive()
    # Single-flight: the waiter consumed the leader's verdict from the cache.
    assert results == [(True, False), (True, False)]
    assert runs == ["proof"]


def test_concurrent_relay_authorize_never_clobbers_the_proof_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The regression Opus named: parallel relay requests through the REAL
    # record→take slot protocol. Un-serialized, one prover's clearing take
    # lands inside another's record→take window — a live child reads as a
    # definitive FALSE and the view is torn down mid-load. Serialized and
    # cached, every concurrent request authorizes and state survives.
    proc = FakeProc()
    port = 45613
    _record_relay_target(monkeypatch, proc, port)
    _stub_port_owner(monkeypatch, listener_pids=(proc.pid,))

    results: list[tuple[str, int | None]] = []
    lock = threading.Lock()

    def _call() -> None:
        outcome = mod.relay_authorize("relay-capability")
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=_call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert results == [("ok", port)] * 8
    # No clobber-induced teardown: the recorded target and token survive.
    assert _recorded_proc() is proc
    assert mod._relay_token == "relay-capability"
    assert _recorded_child_port() == port


def test_listener_verdicts_are_cached_within_ttl_then_reproved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc()
    port = 45613
    clock = _FakeClock()
    monkeypatch.setattr(mod, "time", clock)
    runs: list[float] = []

    def _gated(child: object, child_port: int, *, allow_report: bool) -> tuple[bool | None, bool]:
        runs.append(clock.now)
        return True, False

    monkeypatch.setattr(mod, "_verify_child_listener_gated", _gated)

    assert mod._verify_child_listener(proc, port, allow_report=False) == (True, False)
    assert mod._verify_child_listener(proc, port, allow_report=False) == (True, False)
    assert len(runs) == 1  # second call consumed the cached verdict
    clock.now += mod._PROOF_CACHE_TTL_S + 0.1
    assert mod._verify_child_listener(proc, port, allow_report=False) == (True, False)
    assert len(runs) == 2  # TTL expired — proved afresh


def test_proof_not_before_fence_refuses_older_cached_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The post-connect re-proof's bracketing invariant: a proof started
    # before the connect can never vouch for it, however fresh the TTL says
    # it is. Only a proof started strictly after the fence is consumable.
    proc = FakeProc()
    port = 45613
    clock = _FakeClock()
    monkeypatch.setattr(mod, "time", clock)
    runs: list[float] = []

    def _gated(child: object, child_port: int, *, allow_report: bool) -> tuple[bool | None, bool]:
        runs.append(clock.now)
        return True, False

    monkeypatch.setattr(mod, "_verify_child_listener_gated", _gated)

    assert mod._verify_child_listener(proc, port, allow_report=False) == (True, False)
    assert runs == [0.0]  # cache warmed at t=0
    clock.now = 0.1
    verdict = mod._verify_child_listener(proc, port, allow_report=False, proof_not_before=0.05)
    assert verdict == (True, False)
    assert runs == [0.0, 0.1]  # t=0 proof predates the fence — proved afresh
    verdict = mod._verify_child_listener(proc, port, allow_report=False, proof_not_before=0.05)
    assert verdict == (True, False)
    assert runs == [0.0, 0.1]  # t=0.1 proof started after the fence — consumed


def test_fence_refuses_a_proof_started_on_the_fence_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The coarse-clock case, exactly as Windows produces it: monotonic ticks
    # at ~15.6ms (GetTickCount64 under Python 3.12), so a proof's start and a
    # later caller's fence land on the SAME clock reading even though the
    # fence is physically later. Equality must refuse — the proof's evidence
    # may predate the fence within the tick — and the caller re-proves.
    # Deterministic distillation of the status-poll re-probe tests that fail
    # on Windows when equality serves (shard-5:
    # test_two_status_calls_run_one_self_test_after_the_target_probe,
    # test_status_does_not_report_a_squatter_as_running).
    proc = FakeProc()
    port = 45613
    clock = _FakeClock()
    clock.now = 1.0
    monkeypatch.setattr(mod, "time", clock)
    runs: list[float] = []

    def _gated(child: object, child_port: int, *, allow_report: bool) -> tuple[bool | None, bool]:
        runs.append(clock.now)
        return True, False

    monkeypatch.setattr(mod, "_verify_child_listener_gated", _gated)

    assert mod._verify_child_listener(proc, port, allow_report=False) == (True, False)
    assert runs == [1.0]  # cache warmed: start stamp == 1.0
    # Fence captured on the same tick as the cached proof's start.
    verdict = mod._verify_child_listener(proc, port, allow_report=False, proof_not_before=1.0)
    assert verdict == (True, False)
    assert runs == [1.0, 1.0]  # equality refused the cache — proved afresh


def test_fence_refuses_a_proof_that_started_before_it_but_completed_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cache stamp is the proof's START, captured before any evidence
    # gathering: a proof whose ps/lsof evidence predates the fence must not
    # satisfy the fence merely by COMPLETING after it. Here the proof runs
    # from t=1.0 to t=2.0 and the fence is captured mid-proof at t=1.5: the
    # cached verdict carries stamp 1.0, is refused, and one fresh proof runs
    # — refusal never loops, because the fresh proof satisfies the caller's
    # fence by program order (the fence is captured before the call begins).
    proc = FakeProc()
    port = 45613
    clock = _FakeClock()
    clock.now = 1.0
    monkeypatch.setattr(mod, "time", clock)
    runs: list[float] = []

    def _gated(child: object, child_port: int, *, allow_report: bool) -> tuple[bool | None, bool]:
        runs.append(clock.now)
        clock.now += 1.0  # evidence gathering spans a full unit of time
        return True, False

    monkeypatch.setattr(mod, "_verify_child_listener_gated", _gated)

    assert mod._verify_child_listener(proc, port, allow_report=False) == (True, False)
    assert runs == [1.0]  # proof ran t=1.0→2.0; stamp must be the START
    # A connect completed at t=1.5, mid-proof; its re-proof fence is 1.5.
    verdict = mod._verify_child_listener(proc, port, allow_report=False, proof_not_before=1.5)
    assert verdict == (True, False)
    # The t=1.0-started proof is refused despite completing at 2.0 > 1.5;
    # exactly one fresh proof runs (starting t=2.0, satisfying the fence).
    assert runs == [1.0, 2.0]


def test_report_eligible_proofs_bypass_the_verdict_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # allow_report=True is the startup-adoption path: a stdout-report pass is
    # startup-only evidence, so those proofs neither write the cache nor
    # consume another proof's verdict.
    proc = FakeProc()
    port = 45613
    runs: list[bool] = []

    def _gated(child: object, child_port: int, *, allow_report: bool) -> tuple[bool | None, bool]:
        runs.append(allow_report)
        return True, allow_report

    monkeypatch.setattr(mod, "_verify_child_listener_gated", _gated)

    assert mod._verify_child_listener(proc, port, allow_report=True) == (True, True)
    assert runs == [True]
    # Had the report-eligible run written the cache, this would consume it.
    assert mod._verify_child_listener(proc, port, allow_report=False) == (True, False)
    assert runs == [True, False]
    # And a report-eligible run never consumes the non-report verdict.
    assert mod._verify_child_listener(proc, port, allow_report=True) == (True, True)
    assert runs == [True, False, True]


def test_relay_authorize_forwards_the_proof_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc()
    port = 45613
    _record_relay_target(monkeypatch, proc, port)
    fences: list[float | None] = []

    def _proof(child: object, child_port: int, *, proof_not_before: float | None = None) -> bool:
        fences.append(proof_not_before)
        return True

    monkeypatch.setattr(mod, "_relay_ownership_proof_for", _proof)

    assert mod.relay_authorize("relay-capability", proof_not_before=123.4) == ("ok", port)
    assert fences == [123.4]


def test_status_then_relay_target_share_one_proof_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Opus finding's second half: the 5s status payload ran the proof
    # TWICE per poll — once under status()'s lock, once in relay_target().
    # The verdict cache collapses the pair into one proof run.
    proc = FakeProc()
    port = 45613
    _record_relay_target(monkeypatch, proc, port)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda child_port: True)
    runs: list[str] = []

    def _gated(child: object, child_port: int, *, allow_report: bool) -> tuple[bool | None, bool]:
        runs.append("proof")
        return True, False

    monkeypatch.setattr(mod, "_verify_child_listener_gated", _gated)

    assert mod.status()["status"] == "running"
    assert mod.relay_target() == (port, "relay-capability")
    assert runs == ["proof"]


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Answers ``/`` with 302, exactly as the real dashboard does."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        self.send_response(302)
        self.send_header("Location", "/dashboard")
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@pytest.fixture
def redirecting_server() -> Iterator[int]:
    """A concurrent loopback HTTP server whose root answers 302.

    Relay tests hold more than one connection open at a time.  A serial
    ``HTTPServer`` leaves one upstream leg queued behind another, so closing
    that queued connection cannot finish both relay pumps until the unrelated
    active connection also closes.  Which relay worker reaches the server
    first is scheduler-dependent.  Match the real dashboard's concurrent
    connection handling so each test connection owns an independent handler.
    """
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    # Make server_close() join every request thread after the clients are torn
    # down; no daemon handler may leak into the next test.
    srv.daemon_threads = False
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_show_argv_binds_explicit_loopback_host() -> None:
    """The default listener is IPv6-only, so ``--host 127.0.0.1`` must be passed."""
    argv = mod._show_argv(["/n/playwright-cli"], 45613)

    assert "--host" in argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "45613"
    assert argv[:2] == ["/n/playwright-cli", "show"]


def test_show_argv_never_binds_a_routable_address() -> None:
    """A non-loopback bind would publish remote browser input to the network."""
    argv = mod._show_argv(["/n/playwright-cli"], 45613)

    assert "0.0.0.0" not in argv
    assert "::" not in argv
    assert argv[argv.index("--host") + 1] == mod.LOOPBACK_HOST


def test_show_argv_preserves_the_direct_node_and_javascript_prefix() -> None:
    command = ["/sealed/gateway-node", "/sealed/playwright-cli.js"]

    argv = mod._show_argv(command, 45613)

    assert argv[:3] == [*command, "show"]
    assert argv[argv.index("--host") + 1] == mod.LOOPBACK_HOST


@pytest.mark.parametrize(
    ("line", "requested_port", "reported_port"),
    [
        pytest.param(
            b"Listening on http://127.0.0.1:45613\n",
            45613,
            45613,
            id="captured-fixed-port",
        ),
        pytest.param(
            b"Listening on http://127.0.0.1:42963\n",
            0,
            42963,
            id="captured-port-zero",
        ),
        pytest.param(
            b"[playwright] Listening at: http://127.0.0.1:45613/dashboard\n",
            45613,
            45613,
            id="compatible-prefix-and-path",
        ),
        pytest.param(
            b"Browser view is listening on http://127.0.0.1:45613/\n",
            45613,
            45613,
            id="compatible-wording",
        ),
    ],
)
def test_binding_report_extracts_the_spawned_loopback_address(
    line: bytes,
    requested_port: int,
    reported_port: int,
) -> None:
    assert mod._binding_reported_port_on_line(line, requested_port) == reported_port


@pytest.mark.parametrize(
    "line",
    [
        b"Listening on http://127.0.0.1:45614\n",
        b"Listening on http://localhost:45613\n",
        b"Listening on ftp://127.0.0.1:45613\n",
        b"Dashboard link http://127.0.0.1:45613\n",
    ],
)
def test_binding_report_rejects_the_wrong_endpoint_or_non_listener_line(line: bytes) -> None:
    assert mod._binding_reported_port_on_line(line, 45613) is None


def test_health_accepts_a_302(redirecting_server: int) -> None:
    """``/`` answers 302; a health check that demanded 200 would report dead."""
    assert mod._healthy(redirecting_server) is True


def test_health_false_when_nothing_is_listening() -> None:
    assert mod._healthy(_free_port()) is False


def test_free_port_is_bindable_loopback() -> None:
    port = mod._free_port()

    assert 1 <= port <= 65535
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_free_port_is_not_hardcoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed port would collide with whatever else the operator runs.

    So every number must be the kernel's: each call binds port 0 on loopback and
    returns the port the kernel answered for that bind. That is asserted on the binds
    and their answers, over two calls so a cached first answer fails too. The two
    numbers are never compared: once the first socket is closed the kernel may legally
    hand back the same port.
    """
    binds: list[object] = []
    answers: list[int] = []

    class _RecordingSocket(socket.socket):
        def bind(self, address: object) -> None:
            binds.append(address)
            super().bind(address)

        def getsockname(self) -> object:
            name = super().getsockname()
            answers.append(name[1])
            return name

    # The module's own ``socket`` binding, so the stdlib module stays untouched.
    monkeypatch.setattr(
        mod,
        "socket",
        SimpleNamespace(
            socket=_RecordingSocket, AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM
        ),
    )

    first, second = mod._free_port(), mod._free_port()

    assert binds == [(mod.LOOPBACK_HOST, 0)] * 2, "a call did not leave the port to the kernel"
    assert answers == [first, second], "a returned port is not the kernel's answer to that call"


def test_ensure_running_returns_none_without_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: None)
    spawned: list[list[str]] = []
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawned.append([cli]) or FakeProc())

    assert mod.ensure_running() is None
    assert spawned == []


def test_ensure_running_spawns_with_loopback_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real argv reaching the child carries the explicit loopback bind."""
    recorded: list[list[str]] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)

    def fake_spawn(command: list[str], port: int) -> FakeProc:
        recorded.append(mod._show_argv(command, port))
        return FakeProc()

    monkeypatch.setattr(mod, "_spawn", fake_spawn)

    info = mod.ensure_running()

    assert info is not None
    assert len(recorded) == 1
    argv = recorded[0]
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert info.url == f"http://127.0.0.1:{info.port}"
    assert argv[argv.index("--port") + 1] == str(info.port)


def test_ensure_running_spawns_the_direct_node_and_javascript_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct = ["/sealed/gateway-node", "/sealed/playwright-cli.js"]
    recorded: list[list[str]] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/sealed/playwright-cli")
    monkeypatch.setattr(mod, "cli_command", lambda cli=None: direct)
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(
        mod,
        "_spawn",
        lambda command, port: recorded.append(list(command)) or FakeProc(),
    )

    info = mod.ensure_running()

    assert info is not None
    assert recorded == [direct]


def test_ensure_running_pins_the_configured_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pin is served by a held relay listener; the child stays ephemeral.

    The pinned port must never appear in the child argv — handing it to the
    child would reopen the probe-to-bind window a local squatter can win. The
    module claims the pin itself and the child binds its own ephemeral port.
    """
    recorded: list[list[str]] = []
    opened: list[tuple[object, int]] = []
    claimed: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_free_port", lambda: 51515)

    sentinel_listener = object()
    monkeypatch.setattr(
        mod, "_claim_listener", lambda port: claimed.append(port) or sentinel_listener
    )

    class FakeRelay:
        def __init__(self) -> None:
            self.closed = False

        @classmethod
        def from_listener(cls, listener: object, target_port: int) -> "FakeRelay":
            opened.append((listener, target_port))
            return cls()

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(mod, "_Relay", FakeRelay)

    def fake_spawn(command: list[str], port: int) -> FakeProc:
        recorded.append(mod._show_argv(command, port))
        return FakeProc()

    monkeypatch.setattr(mod, "_spawn", fake_spawn)

    info = mod.ensure_running(port=45613)

    assert info is not None
    assert info.port == 45613
    assert info.url == "http://127.0.0.1:45613"
    assert claimed == [45613]
    assert opened == [(sentinel_listener, 51515)]
    argv = recorded[0]
    # The child binds its own ephemeral port; the pin never reaches its argv.
    assert argv[argv.index("--port") + 1] == "51515"
    # Pinning the port must not loosen the loopback bind.
    assert argv[argv.index("--host") + 1] == "127.0.0.1"


def test_pinned_start_claims_the_pin_before_choosing_the_child_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin is bound before _free_port() runs, so an ephemeral-range pin can
    never be handed back as the child's port (bind collision by construction)."""
    order: list[str] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc())

    pin = _free_port()
    real_claim = mod._claim_listener

    def tracking_claim(port: int) -> object:
        order.append("claim")
        return real_claim(port)

    real_free = mod._free_port

    def tracking_free() -> int:
        order.append("free_port")
        got = real_free()
        assert got != pin, "kernel handed out a port that is supposed to be bound"
        return got

    monkeypatch.setattr(mod, "_claim_listener", tracking_claim)
    monkeypatch.setattr(mod, "_free_port", tracking_free)

    try:
        info = mod.ensure_running(port=pin)
        assert info is not None and info.port == pin
        assert order == ["claim", "free_port"]
    finally:
        mod.stop()


def test_relay_forwards_http_to_the_target_port(redirecting_server: int) -> None:
    """The held listener relays real HTTP byte-for-byte to the child's port."""
    public = _free_port()
    relay = mod._Relay.open(public, redirecting_server)
    assert relay is not None
    try:
        assert mod._healthy(public)  # the 302 travels through the relay
    finally:
        relay.close()


def test_relay_close_joins_the_accept_thread(redirecting_server: int) -> None:
    """close() must not leave the accept thread running (test-visible side
    effect otherwise: a daemon thread outliving the test that spawned it)."""
    relay = mod._Relay.open(_free_port(), redirecting_server)
    assert relay is not None
    try:
        assert relay._thread.is_alive()
    finally:
        relay.close()
    assert not relay._thread.is_alive()


def test_relay_caps_concurrent_connections(
    monkeypatch: pytest.MonkeyPatch, redirecting_server: int
) -> None:
    """Connections beyond the cap are refused; finished ones free their slot."""
    monkeypatch.setattr(mod, "_RELAY_MAX_CONNS", 2)
    public = _free_port()
    relay = mod._Relay.open(public, redirecting_server)
    assert relay is not None
    held: list[socket.socket] = []
    try:
        held = [socket.create_connection(("127.0.0.1", public), timeout=2) for _ in range(2)]
        # Give the accept loop a moment to register both.
        wait_until(lambda: len(relay._conns) >= 2, timeout=5)
        assert len(relay._conns) == 2
        # The third is accepted at the OS level then closed by the cap: reads EOF.
        extra = socket.create_connection(("127.0.0.1", public), timeout=2)
        try:
            extra.settimeout(5)
            assert extra.recv(1) == b""
        finally:
            extra.close()
        # Closing a held connection frees its slot.
        held[0].close()
        wait_until(lambda: len(relay._conns) <= 1, timeout=5)
        assert len(relay._conns) == 1
    finally:
        for sock in held:
            with contextlib.suppress(OSError):
                sock.close()
        relay.close()


def test_relay_close_tears_down_live_connections(redirecting_server: int) -> None:
    """close() closes tracked sockets instead of leaving pumps to linger."""
    public = _free_port()
    relay = mod._Relay.open(public, redirecting_server)
    assert relay is not None
    client: socket.socket | None = None
    try:
        client = socket.create_connection(("127.0.0.1", public), timeout=2)
        wait_until(lambda: bool(relay._conns), timeout=5)
        assert relay._conns
    finally:
        relay.close()
    assert not relay._conns
    try:
        client.settimeout(5)
        assert client.recv(1) == b""  # our side was closed
    finally:
        client.close()


def test_relay_slot_held_until_both_pumps_finish() -> None:
    """A half-closed connection keeps its cap slot: one pump exiting while the
    other stays parked must not free the accounting bound."""
    # A silent upstream that accepts and then neither sends nor closes, so the
    # upstream->client pump stays parked after the client half-closes. (The
    # redirecting HTTP fixture would close on EOF and end BOTH pumps.)
    silent = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    silent.bind(("127.0.0.1", 0))
    silent.listen(4)
    accepted: list[socket.socket] = []

    def _hold() -> None:
        with contextlib.suppress(OSError):
            conn, _ = silent.accept()
            accepted.append(conn)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()

    public = _free_port()
    relay = mod._Relay.open(public, int(silent.getsockname()[1]))
    assert relay is not None
    client: socket.socket | None = None
    try:
        client = socket.create_connection(("127.0.0.1", public), timeout=2)
        wait_until(lambda: bool(relay._conns), timeout=5)
        assert relay._conns
        # Half-close: our write side closes, so the client->upstream pump sees
        # EOF and exits, while the upstream->client pump stays parked on the
        # silent server.
        client.shutdown(socket.SHUT_WR)
        time.sleep(0.5)  # give the exited pump time to run on_done
        assert relay._conns, "slot was released while one pump was still live"
    finally:
        relay.close()
        for sock in [client, silent, *accepted]:
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        holder.join(timeout=5)
    assert not relay._conns


def test_claim_listener_sets_the_platform_exclusivity_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX gets SO_REUSEADDR (TIME_WAIT rebind); Windows gets
    SO_EXCLUSIVEADDRUSE (without it another local process can rebind our held
    port by setting SO_REUSEADDR on ITS socket, defeating the ownership proof)."""
    calls: list[tuple[int, int, int]] = []
    real_setsockopt = socket.socket.setsockopt

    def spy(self: socket.socket, level: int, opt: int, value: int) -> None:
        calls.append((level, opt, value))
        real_setsockopt(self, level, opt, value)

    monkeypatch.setattr(socket.socket, "setsockopt", spy)

    listener = mod._claim_listener(_free_port())
    assert listener is not None
    listener.close()

    if platform_compat.IS_POSIX:
        assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in calls
    elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        assert (socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1) in calls


def test_relay_open_returns_none_when_port_is_taken(redirecting_server: int) -> None:
    """bind() is the atomic ownership proof: an occupied pin is refused."""
    assert mod._Relay.open(redirecting_server, 51515) is None


def test_ensure_running_falls_back_to_ephemeral_when_unpinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` and ``0`` both mean "unset": today's OS-assigned behavior."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_free_port", lambda: 51515)
    spawns: list[int] = []
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc())

    for unset in (None, 0):
        mod._child = None
        info = mod.ensure_running(port=unset)
        assert info is not None and info.port == 51515, unset

    assert spawns == [51515, 51515]


def test_ensure_running_refuses_an_occupied_pinned_port(
    monkeypatch: pytest.MonkeyPatch, redirecting_server: int
) -> None:
    """An occupied pin must fail BEFORE spawning: the doomed child would lose
    the bind and exit while ``_healthy`` accepts the unrelated occupant's
    response, recording a corpse as running and iframing a stranger."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    spawns: list[int] = []
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc())

    info = mod.ensure_running(port=redirecting_server)

    assert info is None
    assert spawns == []
    st = mod.status()
    assert st["status"] == "stopped"
    assert st["reason"] is not None and str(redirecting_server) in st["reason"]


def test_status_reason_cleared_after_a_successful_start(
    monkeypatch: pytest.MonkeyPatch, redirecting_server: int
) -> None:
    """A stale failure reason must not outlive a later successful start."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc())
    # First attempt fails on the occupied pin and records a reason.
    assert mod.ensure_running(port=redirecting_server) is None
    assert mod.status()["reason"] is not None

    # Second attempt (unpinned, healthy) succeeds; then stop() clears state.
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_free_port", lambda: 51515)
    assert mod.ensure_running() is not None
    assert mod.status()["reason"] is None
    mod.stop()
    assert mod.status() == {
        "status": "stopped",
        "url": None,
        "port": None,
        "reason": None,
    }


def test_ensure_running_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy server is reused, not duplicated by a second panel mount."""
    spawns: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc())

    first = mod.ensure_running()
    second = mod.ensure_running()

    assert first == second
    assert len(spawns) == 1


def test_ensure_running_replaces_a_dead_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reusing a corpse would leave the panel permanently blank."""
    spawns: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc())

    mod.ensure_running()
    mod._child = _child_record(FakeProc(alive=False), None)

    assert mod.ensure_running() is not None
    assert len(spawns) == 2


def test_ensure_running_respawns_when_process_stops_answering(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """Alive but unresponsive is still unusable, and the stale child is reaped."""
    spawns: list[int] = []
    probes: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    healthy = {"value": True}

    def probe(port: int) -> bool:
        probes.append(port)
        return healthy["value"]

    monkeypatch.setattr(mod, "_healthy", probe)
    monkeypatch.setattr(
        mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc(pid=len(spawns))
    )
    # This test deliberately never satisfies the startup gate, so it would
    # otherwise wait out the real 30s budget one 0.25s tick at a time. The fake
    # clock only advances when the gate sleeps, which costs no wall time and
    # makes the tick count exact rather than timing-dependent.
    monkeypatch.setattr(mod, "time", _FakeClock())

    mod.ensure_running()
    healthy["value"] = False
    probes.clear()
    # Startup gate cannot pass while unhealthy, so this reports failure...
    assert mod.ensure_running() is None
    # ...and BOTH children were signalled rather than left holding a port: the
    # incumbent it declined to reuse (pid 1) and the replacement that never
    # answered (pid 2). Only checking pid 1 would leave the give-up path's reap
    # untested, since the incumbent is reaped before the replacement is spawned.
    assert reset_state == [1, 2]
    # The gate polled for its whole documented budget before giving up, one probe
    # per tick, plus the single probe of the incumbent it declined to reuse. A
    # budget that expires before its first poll would report the same failure
    # while proving nothing about an unhealthy child.
    assert len(probes) == 1 + int(mod._STARTUP_TIMEOUT_S / mod._POLL_INTERVAL_S)


@pytest.mark.parametrize("failure", ["health", "ownership"])
def test_definitive_live_child_failure_is_reaped_and_respawned(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
    failure: str,
) -> None:
    _linux_shape(monkeypatch)
    children = [FakeProc(pid=4242), FakeProc(pid=4343)]
    spawned: list[FakeProc] = []
    ports = iter((45613, 45614))
    failed_health_ports: set[int] = set()
    ownership = {4242: True, 4343: True}
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_free_port", lambda: next(ports))
    monkeypatch.setattr(mod, "_healthy", lambda port: port not in failed_health_ports)
    monkeypatch.setattr(
        mod, "_spawn", lambda cli, port: spawned.append(children.pop(0)) or spawned[-1]
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: ownership[pid],
    )
    _stub_port_owner(monkeypatch, listener_pids=(), tool=False)

    first = mod.ensure_running()
    assert first is not None
    if failure == "health":
        failed_health_ports.add(first.port)
    else:
        ownership[4242] = False

    second = mod.ensure_running()

    assert second is not None
    assert second.port == 45614
    assert [child.pid for child in spawned] == [4242, 4343]
    assert 4242 in reset_state


def test_ensure_running_gives_up_when_child_exits_during_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: False)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(alive=False))

    assert mod.ensure_running() is None
    assert mod.status()["status"] == "stopped"


def test_ensure_running_returns_none_when_spawn_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: None)

    assert mod.ensure_running() is None


def test_stop_reaps_child_without_a_global_kill(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """``stop()`` reaps only the child it spawned.

    A global ``show --kill`` would stop the daemon, but it stops EVERY session
    with it, including one the operator launched independently — and unsaved work
    goes with it. The child is spawned into its own session, so reaping its
    process group covers the server and the browser it started.
    """
    runs: list[list[str]] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(pid=777))
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda argv, **kw: runs.append(list(argv)) or None,
    )

    mod.ensure_running()
    mod.stop()

    assert runs == [], runs
    assert 777 in reset_state
    assert mod.status()["status"] == "stopped"


def test_stop_reaps_child_even_when_kill_command_fails(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """A failed daemon kill must not leave the child holding the port."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(pid=888))

    def boom(argv: list[str], **kw: object) -> None:
        raise OSError("no such binary")

    monkeypatch.setattr(mod.subprocess, "run", boom)

    mod.ensure_running()
    mod.stop()

    assert 888 in reset_state
    assert _recorded_proc() is None


def test_status_unavailable_without_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable is distinct from stopped: starting the server cannot fix it."""
    monkeypatch.setattr(mod, "cli_path", lambda: None)

    st = mod.status()

    assert st["status"] == "unavailable"
    assert st["url"] is None
    assert st["port"] is None
    assert st["reason"]
    assert "cli_version" not in st


def test_status_unavailable_without_a_launcher_says_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing on disk or PATH: the plain not-installed reason, unchanged."""
    monkeypatch.setattr(mod, "cli_path", lambda: None)
    monkeypatch.setattr(mod, "legacy_launcher_refused", lambda: False)

    st = mod.status()

    assert st["status"] == "unavailable"
    assert st["reason"] == mod.CLI_NOT_INSTALLED_REASON


def test_status_unavailable_with_a_refused_legacy_launcher_explains_the_reinstall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launcher is present but declined: explain the one-time managed reinstall.

    A ``~/.local/bin`` or version-manager install that sits outside the managed
    prefix is not run, so the surface must say a reinstall is needed rather than
    repeat "not installed" as if nothing exists.
    """
    monkeypatch.setattr(mod, "cli_path", lambda: None)
    monkeypatch.setattr(mod, "legacy_launcher_refused", lambda: True)

    st = mod.status()

    assert st["status"] == "unavailable"
    assert st["reason"] == mod.CLI_LEGACY_UNTRUSTED_REASON
    assert st["reason"] != mod.CLI_NOT_INSTALLED_REASON
    # The reason the dashboard renders must name the remedy (reinstall).
    assert "reinstall" in st["reason"].lower()


def test_status_running_reports_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc())

    info = mod.ensure_running()
    st = mod.status()

    assert info is not None
    assert st["status"] == "running"
    assert st["url"] == info.url
    assert st["port"] == info.port
    assert "cli_version" not in st


def test_status_does_not_start_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    spawns: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc())
    monkeypatch.setattr(
        mod,
        "installed_cli_version",
        lambda command=None: pytest.fail("status must not read the CLI version"),
    )

    result = mod.status()

    assert result["status"] == "stopped"
    assert "cli_version" not in result
    assert spawns == []


def test_structurally_blind_status_without_start_allowance_names_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    mod._child = _child_record(proc, 45613)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(platform_compat, "listening_pid_tool", lambda: "lsof")
    monkeypatch.setattr(
        mod,
        "_verify_child_listener",
        lambda child, port, allow_report, proof_not_before=None: (None, False),
    )
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)

    reasons: list[str | None] = []
    for is_windows in (False, True):
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", is_windows)
        reasons.append(mod.status()["reason"])

    assert reasons == [
        "listener ownership cannot be re-proved on this host: "
        "lsof is absent or cannot attribute processes",
        "listener ownership cannot be re-proved on this host: "
        "GetExtendedTcpTable is absent or cannot attribute processes",
    ]
    assert _recorded_proc() is proc


def test_capable_host_rechecks_owner_on_every_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    mod._child = _child_record(proc, 45613)
    checks: list[tuple[FakeProc, int, bool]] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: False)
    monkeypatch.setattr(
        mod,
        "_verify_child_listener",
        lambda child, port, allow_report, proof_not_before=None: (
            checks.append((child.proc, port, allow_report)) or False,
            False,
        ),
    )
    assert mod.status() == {
        "status": "stopped",
        "url": None,
        "port": None,
        "reason": "Couldn't confirm the browser view still owns its port",
    }
    assert checks == [(proc, 45613, False)]
