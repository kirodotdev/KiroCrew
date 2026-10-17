"""The order the supervisor starts and drains its children in.

Two orderings here are correctness rules, not preferences, and neither is visible in the
output of the thing it protects:

* The authority files are restored to COMPLETION before the backend starts. The backend
  flushes the slot table from its own memory, so a backend that starts first persists an
  empty one over the restored files. The conversation list then comes up blank while the
  transcripts are still in the bucket, and nothing reports a fault.
* The sidecar is drained LAST. Its final cycle uploads what the backend's own drain
  flushed, so draining it earlier loses every turn taken since the previous interval on
  an orderly replacement, which is the common case because a deploy is one.
* That final cycle's verdict reaches the EXIT CODE. It is the only copy of the turns in
  the backend's flush, so a task that is asked to stop, fails the cycle and still exits
  0 reports a lossless replacement for a lossy one.

Both are pinned on the recorded SEQUENCE of calls. A test that only checked each step
happened would pass on either ordering, which is the failure being guarded against.

The orphan sweep is stubbed wherever the real ``_teardown`` is called. It discovers and
kills process groups on the host it runs on, and a test has no business doing that.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from container.supervisor import __main__ as sup

from ._settings_helper import make_settings


class _FakeGroup:
    """Stands in for a process group: a pid, a record of its drain, and its status."""

    _next_pid = 9000

    def __init__(self, name: str, log: list[str], status: int | None = 0) -> None:
        self.name = name
        _FakeGroup._next_pid += 1
        self.pid = _FakeGroup._next_pid
        self._log = log
        self._status = status

    def terminate(self, timeout: float) -> int | None:
        self._log.append(f"drain {self.name} {timeout:g}")
        return self._status


@pytest.fixture
def order(monkeypatch, tmp_path):
    """Neutralise every step of ``run`` except the ordering, and record the sequence."""
    calls: list[str] = []
    settings = make_settings(tmp_path, bucket="bkt", crew="crew-5", prefix="crews")

    def _record(label, result=None):
        def _fn(*_args, **_kwargs):
            calls.append(label)
            return result

        return _fn

    monkeypatch.setattr(sup, "verify_layout", _record("verify_layout"))
    monkeypatch.setattr(sup, "verify_sandbox", _record("verify_sandbox"))
    monkeypatch.setattr(sup.backend_mod, "build_backend_env", lambda s: {"E": "1"})
    monkeypatch.setattr(sup.backend_mod, "seed_model_identity", lambda s, source=None: True)
    monkeypatch.setattr(sup.backend_mod, "require_model_identity", lambda s: None)
    monkeypatch.setattr(sup.kiro_login_mod, "seed_kiro_cli_login", _record("kiro login"))
    monkeypatch.setattr(sup.bundle_mod, "install_bundle", _record("bundle"))
    monkeypatch.setattr(sup.backend_mod, "write_backend_config", _record("write_config"))
    monkeypatch.setattr(sup, "_claim_writer_ownership", _record("claim"))
    monkeypatch.setattr(sup, "restore_authority", _record("restore"))
    monkeypatch.setattr(
        sup.backend_mod, "start_backend", _record("start backend", _FakeGroup("backend", calls))
    )
    monkeypatch.setattr(sup.backend_mod, "wait_until_ready", _record("backend ready"))
    monkeypatch.setattr(sup, "_start_front", _record("start front", _FakeGroup("front", calls)))
    monkeypatch.setattr(
        sup, "_start_sidecar", _record("start sidecar", _FakeGroup("sidecar", calls))
    )
    monkeypatch.setattr(sup, "_teardown", _record("teardown"))
    return calls, settings


def _run(settings):
    return sup.run(settings, wait_for_shutdown=lambda children: "signal")


def test_the_authority_files_are_restored_before_the_backend_starts(order):
    calls, settings = order

    _run(settings)

    assert calls.index("restore") < calls.index("start backend")


def test_writer_ownership_is_claimed_before_the_restore(order):
    """The handoff is one step: ownership is claimed in shared storage BEFORE the pair is read.

    This is the F2 ordering rule. Restoration reads the authority pair, and a predecessor that
    adds a slot after that read but before this task's sidecar mints an incarnation would be
    captured into a stale pair and committed under a newer incarnation, deleting the
    predecessor's slot. Claiming ownership (bumping the committed incarnation) BEFORE the
    restore fences the predecessor from that instant, so the claim must precede the restore.
    """
    calls, settings = order

    _run(settings)

    assert calls.index("claim") < calls.index("restore")


def test_the_backend_is_ready_before_the_front_starts(order):
    calls, settings = order

    _run(settings)

    assert calls.index("backend ready") < calls.index("start front")


def test_the_sidecar_starts_after_the_front(order):
    calls, settings = order

    _run(settings)

    assert calls.index("start front") < calls.index("start sidecar")


def test_the_crew_bundle_is_installed_before_the_restore(order):
    """The restore writes into the data home the bundle install lays out."""
    calls, settings = order

    _run(settings)

    assert calls.index("bundle") < calls.index("restore")


def test_a_failed_restore_stops_the_task_before_the_backend_starts(order, monkeypatch):
    """Booting without the slot table is the loss the restore exists to prevent."""
    calls, settings = order

    def _fail(_settings):
        calls.append("restore")
        raise RuntimeError("the bucket could not be read")

    monkeypatch.setattr(sup, "restore_authority", _fail)

    with pytest.raises(RuntimeError):
        _run(settings)

    assert "start backend" not in calls


def test_a_failed_restore_releases_the_ownership_claim(order, monkeypatch):
    """GPT F2: a claim followed by a restore failure releases the claim, not fences forever.

    The claim bumps the committed incarnation BEFORE restoration. restore_authority and
    start_backend run inside the ownership-release scope, so a restore timeout or spawn
    failure after a successful claim releases the bump -- otherwise the aborting task leaves
    the committed incarnation raised and permanently fences a still-healthy predecessor.

    It fails if restore/spawn run outside the release scope (the predecessor-fenced bug).
    """
    calls, settings = order
    sentinel = sup.generation_mod.ClaimedOwnership(token="t", prior_body=b"{}", prior_etag='"e"')
    monkeypatch.setattr(
        sup, "_claim_writer_ownership", lambda _s: (calls.append("claim"), sentinel)[1]
    )

    def _fail(_settings):
        calls.append("restore")
        raise RuntimeError("authority GET timed out")

    monkeypatch.setattr(sup, "restore_authority", _fail)

    released: list = []

    def _release(_settings, _store, claim):
        released.append(claim)
        return True

    monkeypatch.setattr(sup.generation_mod, "release_ownership", _release)

    with pytest.raises(RuntimeError):
        _run(settings)

    # The restore failure released the claim and never started the backend.
    assert released == [sentinel]
    assert "start backend" not in calls


def test_a_stop_signal_during_startup_releases_the_ownership_claim(order, monkeypatch):
    """GPT F1: a SIGTERM/SIGINT mid-startup releases the claim rather than fencing forever.

    The claim bumps the committed incarnation before the supervise-phase signal handlers are
    installed, so without a guard a stop during restore or readiness takes the process down
    (SIGTERM) or escapes as KeyboardInterrupt (SIGINT) WITHOUT releasing the claim -- leaving a
    still-live predecessor fenced into silent loss of its later turns. The startup signal guard
    raises _StartupInterrupted for such a stop, which the abort path catches and releases the
    claim.

    Modelled by restore_authority raising _StartupInterrupted, exactly what the guard's handler
    raises when a signal arrives there. It fails if the abort path catches only Exception (the
    BaseException escapes with the claim still raised).
    """
    calls, settings = order
    sentinel = sup.generation_mod.ClaimedOwnership(token="t", prior_body=b"{}", prior_etag='"e"')
    monkeypatch.setattr(
        sup, "_claim_writer_ownership", lambda _s: (calls.append("claim"), sentinel)[1]
    )

    def _interrupted(_settings):
        calls.append("restore")
        raise sup._StartupInterrupted(15)  # SIGTERM, as the guard's handler raises it

    monkeypatch.setattr(sup, "restore_authority", _interrupted)

    released: list = []

    def _release(_settings, _store, claim):
        released.append(claim)
        return True

    monkeypatch.setattr(sup.generation_mod, "release_ownership", _release)

    with pytest.raises(sup._StartupInterrupted):
        _run(settings)

    # The stop released the claim and never started the backend -- the predecessor is not left
    # fenced.
    assert released == [sentinel]
    assert "start backend" not in calls


def test_a_stop_during_the_supervise_handoff_releases_the_claim_and_tears_down(order, monkeypatch):
    """GPT F1: a stop in the window between the startup guard and wait_for_shutdown is covered.

    The startup guard is still active across the `watched` list build and the supervise `try`
    (they live INSIDE the guard scope), so a SIGTERM/SIGINT that lands there -- after every
    child is up but before `wait_for_shutdown` has installed its own handlers -- is raised as
    `_StartupInterrupted` rather than taking the process down by its default disposition. The
    handoff abort path catches it, releases the ownership claim (so the incarnation bump does
    not outlive this aborting task and fence a live predecessor), and the `finally` tears the
    children down.

    Modelled by injecting a `wait_for_shutdown` that raises `_StartupInterrupted`, exactly what
    the still-active guard raises for a stop in that span. It fails if the supervise region ran
    outside the guard (the stop would be an un-caught BaseException with the claim still raised)
    or if the abort path released nothing.
    """
    calls, settings = order
    sentinel = sup.generation_mod.ClaimedOwnership(token="t", prior_body=b"{}", prior_etag='"e"')
    monkeypatch.setattr(
        sup, "_claim_writer_ownership", lambda _s: (calls.append("claim"), sentinel)[1]
    )

    released: list = []

    def _release(_settings, _store, claim):
        released.append(claim)
        return True

    monkeypatch.setattr(sup.generation_mod, "release_ownership", _release)

    def _interrupted_wait(_children):
        raise sup._StartupInterrupted(15)  # SIGTERM landing in the handoff window

    with pytest.raises(sup._StartupInterrupted):
        sup.run(settings, wait_for_shutdown=_interrupted_wait)

    # Every child started (the stop lands AFTER the spawns), the claim was released, and the
    # children were torn down -- not left running with the claim raised.
    assert "start sidecar" in calls
    assert released == [sentinel]
    assert "teardown" in calls


def test_the_startup_signal_guard_raises_and_restores_the_prior_handlers():
    """The guard converts a real SIGTERM/SIGINT to _StartupInterrupted, then restores handlers.

    Inside the guard a stop signal must raise (so the abort path runs); on exit the prior
    dispositions must be restored, so the supervise phase installs its own handlers over a
    clean baseline rather than over the guard's.
    """
    import os as _os
    import signal as _signal

    before_term = _signal.getsignal(_signal.SIGTERM)
    before_int = _signal.getsignal(_signal.SIGINT)
    try:
        with pytest.raises(sup._StartupInterrupted):
            with sup._startup_signal_guard():
                _os.kill(_os.getpid(), _signal.SIGTERM)
        # The guard restored the prior handlers on exit.
        assert _signal.getsignal(_signal.SIGTERM) == before_term
        assert _signal.getsignal(_signal.SIGINT) == before_int
    finally:
        _signal.signal(_signal.SIGTERM, before_term)
        _signal.signal(_signal.SIGINT, before_int)


def test_the_guard_defers_a_stop_during_the_claim_then_raises_it_afterwards():
    """GPT F2: a stop DURING claim acquisition is held, not raised mid-claim.

    A SIGTERM/SIGINT between the claim's committed PUT and the moment the returned claim is
    bound must not escape there -- the release scope has no claim object to release yet, so the
    claim would be stranded (the predecessor stays fenced while this task is gone). Inside
    `deferring()` the stop is recorded; `raise_pending()` raises it only once the claim is
    recorded inside the release scope.

    It fails if `deferring()` lets the signal raise in place (the stranded-claim bug).
    """
    import os as _os
    import signal as _signal

    before_term = _signal.getsignal(_signal.SIGTERM)
    before_int = _signal.getsignal(_signal.SIGINT)
    try:
        with sup._startup_signal_guard() as guard:
            # A stop lands DURING the (modelled) claim: deferring holds it rather than raising.
            with guard.deferring():
                _os.kill(_os.getpid(), _signal.SIGTERM)
                # Control reaches here only because the stop was deferred, not raised.
                claim_bound = True
            assert claim_bound
            # Now that the claim is bound, the pending stop is raised inside the release scope.
            with pytest.raises(sup._StartupInterrupted):
                guard.raise_pending()
            # A second call is a no-op: the pending stop is consumed once.
            guard.raise_pending()
    finally:
        _signal.signal(_signal.SIGTERM, before_term)
        _signal.signal(_signal.SIGINT, before_int)


def test_a_stop_during_the_claim_releases_it_rather_than_stranding_it(order, monkeypatch):
    """GPT F2: a stop mid-claim is deferred, the claim is bound, then it is released.

    `run` acquires the claim inside `deferring()`, so a stop there is held; the claim is bound,
    the release scope becomes active, and `raise_pending()` raises _StartupInterrupted inside it
    so the ordinary release path runs. The claim is released, not stranded.

    Modelled by _claim_writer_ownership sending itself SIGTERM mid-acquisition (deferred by the
    guard) before returning the claim. It fails if the claim call does not defer the stop (it
    escapes before the claim is bound and nothing releases it).
    """
    import os as _os
    import signal as _signal

    calls, settings = order
    sentinel = sup.generation_mod.ClaimedOwnership(token="t", prior_body=b"{}", prior_etag='"e"')

    def _claim_with_stop(_s):
        calls.append("claim")
        # A SIGTERM lands between the (modelled) committed PUT and returning the claim. The
        # guard's deferring scope holds it, so the claim is still returned and bound.
        _os.kill(_os.getpid(), _signal.SIGTERM)
        return sentinel

    monkeypatch.setattr(sup, "_claim_writer_ownership", _claim_with_stop)

    released: list = []

    def _release(_settings, _store, claim):
        released.append(claim)
        return True

    monkeypatch.setattr(sup.generation_mod, "release_ownership", _release)

    before_term = _signal.getsignal(_signal.SIGTERM)
    before_int = _signal.getsignal(_signal.SIGINT)
    try:
        with pytest.raises(sup._StartupInterrupted):
            _run(settings)
    finally:
        _signal.signal(_signal.SIGTERM, before_term)
        _signal.signal(_signal.SIGINT, before_int)

    # The deferred stop was raised inside the release scope: the claim was released, not
    # stranded, and the backend never started.
    assert released == [sentinel]
    assert "start backend" not in calls


def test_all_three_children_are_watched(order):
    calls, settings = order
    watched: list = []

    def record_and_signal(children) -> str:
        watched.append(children)
        return "signal"

    sup.run(settings, wait_for_shutdown=record_and_signal)

    assert [child.name for child in watched[0]] == ["backend", "front", "sidecar"]


def test_no_bucket_starts_no_sidecar(monkeypatch):
    """A writer with no destination looks exactly like a working backup, so there is none."""
    started: list = []
    monkeypatch.setattr(sup, "spawn_process_group", lambda *a, **k: started.append(a))

    assert sup._start_sidecar(SimpleNamespace(backup_bucket="")) is None
    assert started == []


def test_no_bucket_restores_nothing(monkeypatch):
    """With nothing in a bucket there is nothing to bring back, and that is not a fault.

    The wrapper reports whether it restored authority from a remote snapshot, so the
    no-bucket no-op reports ``False``.
    """
    monkeypatch.setattr(
        sup.restore_mod,
        "restore_authority",
        lambda *a, **k: pytest.fail("the restore must not run without a bucket"),
    )

    assert sup.restore_authority(SimpleNamespace(backup_bucket="")) is False


def _run_capturing_backend_env(monkeypatch, tmp_path, *, restored: bool) -> dict:
    """Drive ``run`` far enough to capture the env handed to ``start_backend``.

    Returns that env dict. ``restored`` is what the supervisor
    ``restore_authority`` wrapper reports — whether the authority pair came back
    from a remote snapshot — which is the only input that decides the flag.
    """
    settings = make_settings(tmp_path, bucket="bkt", crew="crew-5", prefix="crews")
    captured: dict = {}

    def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(sup, "verify_layout", _noop)
    monkeypatch.setattr(sup, "verify_sandbox", _noop)
    monkeypatch.setattr(sup.backend_mod, "build_backend_env", lambda s: {"E": "1"})
    monkeypatch.setattr(sup.backend_mod, "seed_model_identity", lambda s, source=None: True)
    monkeypatch.setattr(sup.backend_mod, "require_model_identity", lambda s: None)
    monkeypatch.setattr(sup.kiro_login_mod, "seed_kiro_cli_login", _noop)
    monkeypatch.setattr(sup.bundle_mod, "install_bundle", _noop)
    monkeypatch.setattr(sup.backend_mod, "write_backend_config", _noop)
    monkeypatch.setattr(sup, "_claim_writer_ownership", lambda s: SimpleNamespace(token=""))
    monkeypatch.setattr(sup, "restore_authority", lambda s: restored)

    def _start_backend(settings, env=None):
        captured.update(env or {})
        return _FakeGroup("backend", [])

    monkeypatch.setattr(sup.backend_mod, "start_backend", _start_backend)
    monkeypatch.setattr(sup.backend_mod, "wait_until_ready", _noop)
    monkeypatch.setattr(sup, "_start_front", lambda *a, **k: _FakeGroup("front", []))
    monkeypatch.setattr(sup, "_start_sidecar", lambda *a, **k: _FakeGroup("sidecar", []))
    monkeypatch.setattr(sup, "_teardown", _noop)

    sup.run(settings, wait_for_shutdown=lambda children: "signal")
    return captured


def test_a_remote_authority_restore_tells_the_backend_transcripts_are_remote(monkeypatch, tmp_path):
    """The backend learns absent-local transcripts may be remote-only.

    GPT 6.1 F1: when the authority pair was restored from a remote snapshot
    without the transcripts, the backend must keep a listed slot whose transcript
    is not yet local as a reopen seed. The signal rides to the backend as
    ``ENV_AUTHORITY_RESTORED`` on its environment.
    """
    env = _run_capturing_backend_env(monkeypatch, tmp_path, restored=True)
    assert env.get(sup.backend_mod.ENV_AUTHORITY_RESTORED) == "1"


def test_no_remote_restore_leaves_the_backend_flag_unset(monkeypatch, tmp_path):
    """A boot that restored nothing from a remote snapshot sets no flag.

    An ordinary boot must keep pruning a listed slot with no transcript as a dead
    tab, so the flag that flips that behaviour must not appear when there was no
    remote restore.
    """
    env = _run_capturing_backend_env(monkeypatch, tmp_path, restored=False)
    assert sup.backend_mod.ENV_AUTHORITY_RESTORED not in env


def test_the_sidecar_is_drained_last_so_its_final_cycle_has_something_to_upload(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sup, "_sweep_orphans_the_backend_cannot_reap", lambda known: None)

    sup._teardown(
        _FakeGroup("front", calls), _FakeGroup("backend", calls), _FakeGroup("sidecar", calls)
    )

    assert [line.split()[1] for line in calls] == ["front", "backend", "sidecar"]


def test_teardown_without_a_sidecar_drains_the_other_two(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sup, "_sweep_orphans_the_backend_cannot_reap", lambda known: None)

    sup._teardown(_FakeGroup("front", calls), _FakeGroup("backend", calls))

    assert [line.split()[1] for line in calls] == ["front", "backend"]


# --- the drain hands the final cycle's verdict back -------------------------------


def test_teardown_hands_back_the_sidecars_status_and_not_the_others(monkeypatch):
    """The other two drain on our own signal, so a status there is the signal.

    The sidecar's status is different: it is the only evidence that the post-shutdown
    cycle committed, which is why it is the one the drain returns.
    """
    calls: list[str] = []
    monkeypatch.setattr(sup, "_sweep_orphans_the_backend_cannot_reap", lambda known: None)

    status = sup._teardown(
        _FakeGroup("front", calls, status=-15),
        _FakeGroup("backend", calls, status=-15),
        _FakeGroup("sidecar", calls, status=3),
    )

    assert status == 3


def test_teardown_without_a_sidecar_reports_no_status(monkeypatch):
    """No writer, no final cycle: there is nothing for the exit code to weigh."""
    calls: list[str] = []
    monkeypatch.setattr(sup, "_sweep_orphans_the_backend_cannot_reap", lambda known: None)

    assert sup._teardown(_FakeGroup("front", calls), _FakeGroup("backend", calls)) is None


# --- a spawn that fails partway tears down the children already started -----------


def test_a_sidecar_spawn_failure_tears_down_the_backend_and_front(order, monkeypatch):
    """``_start_sidecar`` raising after the front is up must not leak the started pair.

    The children are started INSIDE the cleanup scope, so a fork/PID failure between the
    two spawns drains the front and the backend rather than returning from ``run`` with
    them orphaned under a PID 1 that has not yet exited. Without the scope the backend's
    turns never drained and its accepted state could be lost.
    """
    calls, settings = order
    monkeypatch.setattr(
        sup, "_start_front", lambda s: (calls.append("start front"), _FakeGroup("front", calls))[1]
    )

    def _boom(_s):
        calls.append("start sidecar")
        raise OSError("cannot fork: pid table exhausted")

    monkeypatch.setattr(sup, "_start_sidecar", _boom)
    torn: list[tuple[_FakeGroup | None, _FakeGroup | None, _FakeGroup | None]] = []

    def _teardown(front, backend, sidecar=None):
        torn.append((front, backend, sidecar))
        return None

    monkeypatch.setattr(sup, "_teardown", _teardown)

    with pytest.raises(OSError):
        sup.run(settings, wait_for_shutdown=lambda children: "signal")

    # The front spawn ran before the sidecar spawn raised, so teardown saw a real front
    # and the running backend; the sidecar never started, so it is None.
    assert len(torn) == 1
    front, backend, sidecar = torn[0]
    assert front is not None and front.name == "front"
    assert backend is not None and backend.name == "backend"
    assert sidecar is None


def test_a_spawn_failure_after_readiness_releases_the_ownership_claim(order, monkeypatch):
    """GPT F1: a front/sidecar spawn failure after readiness releases the claim.

    The claim bumps the committed incarnation before restoration. Readiness succeeds, so the
    claim is held; then a child spawn raises (a fork/PID failure). That abort path must release
    the claim as the restore/readiness abort path does -- otherwise the bump outlives the
    aborting task and permanently fences a still-healthy predecessor, whose later turns are
    then lost on replacement.

    It fails if the spawn-failure teardown re-raises without releasing the claim.
    """
    calls, settings = order
    sentinel = sup.generation_mod.ClaimedOwnership(token="t", prior_body=b"{}", prior_etag='"e"')
    monkeypatch.setattr(
        sup, "_claim_writer_ownership", lambda _s: (calls.append("claim"), sentinel)[1]
    )
    monkeypatch.setattr(
        sup, "_start_front", lambda s: (calls.append("start front"), _FakeGroup("front", calls))[1]
    )

    def _boom(_s):
        raise OSError("cannot fork: pid table exhausted")

    monkeypatch.setattr(sup, "_start_sidecar", _boom)
    monkeypatch.setattr(sup, "_teardown", lambda *a, **k: None)

    released: list = []

    def _release(_settings, _store, claim):
        released.append(claim)
        return True

    monkeypatch.setattr(sup.generation_mod, "release_ownership", _release)

    with pytest.raises(OSError):
        sup.run(settings, wait_for_shutdown=lambda children: "signal")

    # The spawn failure after readiness released the same claim the abort path holds.
    assert released == [sentinel]


def test_teardown_drains_the_backend_alone_when_the_front_never_started(monkeypatch):
    """A spawn that fails at the FRONT leaves only the backend to drain.

    ``_teardown`` must accept ``front=None`` so the cleanup scope can tear down the one
    child that did start -- the running backend -- without tripping over an absent front.
    """
    calls: list[str] = []
    monkeypatch.setattr(sup, "_sweep_orphans_the_backend_cannot_reap", lambda known: None)

    status = sup._teardown(None, _FakeGroup("backend", calls))

    assert [line.split()[1] for line in calls] == ["backend"]
    assert status is None
