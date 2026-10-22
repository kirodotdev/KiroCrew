"""Tests for ACP client: the stale-turn cutoff, its liveness-oracle gate and the tool-stall
watchdog.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import AcpClient, AcpProcessDied
from kiro_crew.acp.liveness import (
    EVIDENCE_SAMPLING,
    VERDICT_DEAD,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LivenessOracle,
)


class TestAcpClientStaleTurn:
    """The stale-turn check does not fire falsely on the thinking path.

    The staleness check in ``_prompt_loop`` must fold in ``_last_activity``
    (refreshed by the stderr drain when the agent streams ``thinking_tokens``)
    and not rely solely on the stdout clock. Otherwise a turn that streams its
    final text and then thinks silently on stdout — while still emitting
    thinking events on stderr — is falsely declared stale, burning
    ~_STALE_TURN_TIMEOUT seconds per turn and reaping long subagent runs.
    """

    def _client_with_silent_stdout(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        # stdout is silent: _read_message returns None (timeout/no data).
        client._read_message = AsyncMock(return_value=None)
        # process stays alive so the consecutive-empty death path is not taken.
        client._is_process_alive = MagicMock(return_value=True)
        # caller streamed text earlier this turn, so staleness is eligible.
        client._stale_eligible = True
        return client

    @pytest.mark.asyncio
    async def test_recent_stderr_activity_prevents_stale_turn(self, tmp_path, caplog):
        """thinking_tokens on stderr (recent _last_activity) keeps the turn alive.

        With a tiny stale timeout, a turn whose stdout is silent but whose
        _last_activity keeps refreshing (as the stderr drain does during
        thinking) must NOT be declared stale.
        """
        client = self._client_with_silent_stdout(tmp_path)

        # Emulate the stderr drain refreshing _last_activity on every poll, so
        # the turn is continuously "active" even though stdout yields nothing.
        # Sleep well under the stale timeout each poll so liveness never lapses
        # (mirrors thinking_tokens streaming faster than _STALE_TURN_TIMEOUT).
        async def read_and_touch(*args, **kwargs):
            # flake-ok: pre-existing 50 ms pacing in the stubbed read, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing poll pacing inside the stubbed read, moved verbatim; the comment above explains it
            await asyncio.sleep(0.05)
            client._last_activity = time.monotonic()
            return None

        client._read_message = AsyncMock(side_effect=read_and_touch)

        # The overall loop timeout (1.0s) must exceed _STALE_TURN_TIMEOUT (0.2s)
        # so the staleness check gets multiple chances to fire during the loop —
        # otherwise the loop exits on `remaining <= 0` before the check runs and
        # the test would pass trivially, even without the fix. Each 0.05s poll
        # refreshes _last_activity, staying well under the 0.2s stale window.
        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.2):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=1, timeout=1.0):
                    actions.append(action)

        assert actions == []  # stdout silent → nothing yielded
        assert "Stale turn detected" not in caplog.text  # fix: not falsely stale

    @pytest.mark.asyncio
    async def test_genuine_silence_still_triggers_stale_turn(self, tmp_path, caplog):
        """Silence on BOTH stdout and stderr still trips the stale-turn guard."""
        client = self._client_with_silent_stdout(tmp_path)
        # _last_activity never refreshes → genuinely silent on both clocks.
        client._last_activity = time.monotonic()

        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=7, timeout=5.0):
                    actions.append(action)

        assert actions == []  # returned via stale-turn early return
        assert "Stale turn detected" in caplog.text  # real protection preserved


class TestAcpClientStaleTurnOracleGate:
    """The 90s stale-turn cutoff is oracle-gated.

    A turn whose stdout AND stderr are silent but whose backend process
    subtree is provably WORKING (CPU/IO movement) must NOT be declared stale
    at ``_STALE_TURN_TIMEOUT`` — the kiro shared-runtime path already defers on
    a WORKING liveness verdict; the ``AcpClient`` capture path must converge
    onto the same contract instead of a blunt wall-clock. A non-WORKING verdict
    preserves today's behavior exactly (the turn ends).
    """

    def _silent_client(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = MagicMock(return_value=True)
        client._stale_eligible = True
        client._last_activity = time.monotonic()
        return client

    @pytest.mark.asyncio
    async def test_working_verdict_defers_stale_turn(self, tmp_path, caplog):
        """WORKING backend movement keeps a silent turn alive past the cutoff."""
        client = self._silent_client(tmp_path)
        client._consult_liveness_model_wait = AsyncMock(
            return_value=(VERDICT_WORKING, "backend activity (io)")
        )

        # Loop timeout must exceed the stale window so the check fires repeatedly;
        # without the fix the first stale hit returns and the turn ends.
        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.1):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=11, timeout=0.6):
                    actions.append(action)

        assert actions == []
        # The turn was NOT declared stale — it ran until the loop deadline.
        assert "Stale turn detected" not in caplog.text
        # Proves priming: the oracle is consulted on silent reads BEFORE the
        # cutoff too, not once at the 90s mark — a single consult would read
        # UNKNOWN (no movement baseline) and reap. Expect several consults over
        # the 0.6s loop at the ~_READ_TIMEOUT cadence (patched small in the loop).
        assert client._consult_liveness_model_wait.await_count >= 2

    @pytest.mark.asyncio
    async def test_real_oracle_movement_defers_via_fake_proc(self, tmp_path, caplog):
        """End-to-end with the REAL LivenessOracle over a fake /proc tree.

        Guards the priming bug the mock hides: the oracle needs a prior sample
        to compute a movement delta, so the loop must consult it repeatedly to
        build a baseline before the cutoff. Here the backend subtree's IO
        counter grows between reads → real WORKING verdict → deferral.

        Builds a minimal fake ``/proc`` inline (only the files the model-wait
        oracle reads: ``<pid>/stat``, ``<pid>/task/<tid>/children``,
        ``<pid>/io``) rather than importing a helper from another test module —
        a cross-``test``-package import fails in the pipeline runner where
        ``test`` is not an importable package (``ModuleNotFoundError``).
        """
        proc = tmp_path / "proc"

        def _write_pid(pid: int, *, children: list[int], io_bytes: int) -> None:
            d = proc / str(pid)
            (d / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
            (d / "task" / str(pid) / "children").write_text(" ".join(str(c) for c in children))
            # stat: pid (comm) state ... starttime(field 22); huge starttime →
            # negative age so the oracle's start-time guard accepts the pid.
            fields = ["0"] * 50
            fields[0] = "S"
            fields[19] = "10000000"
            (d / "stat").write_text(f"{pid} (fake) {' '.join(fields)}\n")
            (d / "io").write_text(f"rchar: {io_bytes}\nwchar: 0\n")

        def _set_io(pid: int, io_bytes: int) -> None:
            (proc / str(pid) / "io").write_text(f"rchar: {io_bytes}\nwchar: 0\n")

        _write_pid(4242, children=[4243], io_bytes=0)
        _write_pid(4243, children=[], io_bytes=1000)

        client = AcpClient(work_dir=tmp_path)
        client._is_process_alive = MagicMock(return_value=True)
        client._stale_eligible = True
        client._last_activity = time.monotonic()
        client._pid = 4242
        # Real oracle, tiny sample window so movement is detectable within the
        # fast test loop; point it at the fake /proc tree.
        client._liveness_oracle = LivenessOracle(str(proc), sample_min_secs=0.0)

        # Grow the subtree's IO on every silent read → movement between samples.
        _io = {"n": 1000}

        async def read_and_move(*args, **kwargs):
            # flake-ok: pre-existing 20 ms pacing in the stubbed read, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing pacing inside the stubbed read so IO moves between oracle samples, moved verbatim
            await asyncio.sleep(0.02)
            _io["n"] += 5000
            _set_io(4243, _io["n"])
            return None

        client._read_message = AsyncMock(side_effect=read_and_move)

        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with patch("kiro_crew.acp.client._READ_TIMEOUT", 0.02):
                with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                    actions = []
                    async for action, _msg in client._prompt_loop(req_id=42, timeout=0.5):
                        actions.append(action)

        assert actions == []
        assert "Stale turn detected" not in caplog.text  # real movement → deferred

    @pytest.mark.asyncio
    async def test_dead_verdict_still_ends_stale_turn(self, tmp_path, caplog):
        """A non-WORKING verdict preserves today's end-the-turn behavior."""
        client = self._silent_client(tmp_path)
        client._consult_liveness_model_wait = AsyncMock(
            return_value=(VERDICT_DEAD, "no established backend socket")
        )

        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=12, timeout=5.0):
                    actions.append(action)

        assert actions == []
        assert "Stale turn detected" in caplog.text

    @pytest.mark.asyncio
    async def test_unknown_verdict_still_ends_stale_turn(self, tmp_path, caplog):
        """UNKNOWN keeps the conservative 90s cutoff — the gate never weakens
        hang recovery; only a provably-WORKING turn is extended."""
        client = self._silent_client(tmp_path)
        client._consult_liveness_model_wait = AsyncMock(
            return_value=(VERDICT_UNKNOWN, "established-but-flat")
        )

        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=13, timeout=5.0):
                    actions.append(action)

        assert actions == []
        assert "Stale turn detected" in caplog.text

    @pytest.mark.asyncio
    async def test_consult_skips_while_prior_is_in_flight(self, tmp_path):
        """An unfinished consult prevents another executor job from starting."""
        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        client._liveness_oracle = MagicMock()
        client._consult_future = asyncio.get_running_loop().create_future()

        verdict = await client._consult_liveness_model_wait()

        assert verdict == (VERDICT_UNKNOWN, "prior consult still in flight")
        client._liveness_oracle.check_model_wait.assert_not_called()

        client._consult_future.set_result((VERDICT_WORKING, "done"))
        client._liveness_oracle.check_model_wait.return_value = (VERDICT_DEAD, "flat")

        assert await client._consult_liveness_model_wait() == (VERDICT_DEAD, "flat")
        client._liveness_oracle.check_model_wait.assert_called_once_with(4242)

    @pytest.mark.asyncio
    async def test_consult_consumes_a_failed_prior_consults_exception(self, tmp_path):
        """Reopening the guard must consume a failed prior consult's exception.

        ``wait_for`` cancels shield's outer future while the /proc walk is still
        running, and shield's outer-done callback then detaches the inner-done
        callback that would have retrieved the inner result — so a walk that
        raises after the timeout leaves its exception unretrieved.
        ``Future.__del__`` reports that through the loop exception handler, which
        the gateway records as an unhandled-asyncio crash for an ordinary probe
        failure.
        """
        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        client._liveness_oracle = MagicMock()
        client._liveness_oracle.check_model_wait.return_value = (VERDICT_DEAD, "flat")

        prior = asyncio.get_running_loop().create_future()
        prior.set_exception(OSError("wedged /proc read"))
        client._consult_future = prior

        assert await client._consult_liveness_model_wait() == (VERDICT_DEAD, "flat")

        # _log_traceback is the flag Future.__del__ consults to decide whether to
        # report an exception as never retrieved.
        assert prior._log_traceback is False

    @pytest.mark.asyncio
    async def test_consult_reports_unknown_when_the_submission_itself_fails(self, tmp_path):
        """A failed executor submission must degrade to UNKNOWN, not raise.

        The caller is a silent-read poll inside ``_prompt_loop``; an exception
        escaping here aborts the live turn. Submission can fail for ordinary
        reasons — a shut-down executor during teardown, or thread creation
        refused under load — so it stays inside the same guard that already
        converts probe failures to UNKNOWN.
        """
        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        client._liveness_oracle = MagicMock()

        with patch(
            "kiro_crew.acp.client.subprocess_executor",
            side_effect=RuntimeError("cannot schedule new futures after shutdown"),
        ):
            assert await client._consult_liveness_model_wait() == (
                VERDICT_UNKNOWN,
                "oracle offload error",
            )

        # Nothing was tracked, so the guard is not left latched shut by a
        # submission that never produced a future.
        assert client._consult_future is None

    @pytest.mark.asyncio
    async def test_reset_state_releases_a_consult_from_the_dead_generation(self, tmp_path):
        """A walk wedged on the dead PID must not gate the replacement process.

        ``_reset_state`` is the process-generation boundary. A /proc walk blocked
        on the old PID can never say anything about the new one, so retaining it
        answers every later poll with "prior consult still in flight" — and
        ``_prompt_loop``'s UNKNOWN cutoff then completes a healthy turn early,
        truncating its output.
        """
        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        client._consult_future = asyncio.get_running_loop().create_future()

        with (
            patch("kiro_crew.session_pid._pid_gone_or_unmanaged", return_value=True),
            patch("kiro_crew.session._untrack_pid"),
            patch("kiro_crew.session._untrack_session_pid"),
        ):
            client._reset_state()
        client._pid = 5353
        # Reset retires the oracle, so the stub belongs on the replacement.
        client._liveness_oracle = MagicMock()
        client._liveness_oracle.check_model_wait.return_value = (VERDICT_DEAD, "flat")

        assert client._consult_future is None
        assert await client._consult_liveness_model_wait() == (VERDICT_DEAD, "flat")
        client._liveness_oracle.check_model_wait.assert_called_once_with(5353)

    @pytest.mark.asyncio
    async def test_released_consult_exception_is_consumed_after_reset(self, tmp_path):
        """A released walk that fails afterwards must not read as a crash.

        Reset drops the client's last reference while the walk is still running,
        so an exception raised after that point reaches ``Future.__del__`` with
        nobody having retrieved it.
        """
        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        wedged = asyncio.get_running_loop().create_future()
        client._consult_future = wedged

        with (
            patch("kiro_crew.session_pid._pid_gone_or_unmanaged", return_value=True),
            patch("kiro_crew.session._untrack_pid"),
            patch("kiro_crew.session._untrack_session_pid"),
        ):
            client._reset_state()

        wedged.set_exception(OSError("wedged /proc read"))
        await asyncio.sleep(0)  # add_done_callback lands via call_soon

        assert wedged._log_traceback is False

    @pytest.mark.asyncio
    async def test_reset_state_retires_the_liveness_oracle(self, tmp_path):
        """A detached walk must not be able to pollute the next generation's baseline.

        The executor job captures ``self._liveness_oracle`` as a bound method and
        its /proc walk keeps running after the wait times out. Samples are keyed
        ``"io"``/``"cpu"`` with no PID in the key, so a late write lands on
        whatever the current generation reads — and any nonzero delta counts as
        movement, including the negative one from comparing a different process
        tree. That reads WORKING for a genuinely wedged turn and defers recovery
        to the 2h backstop. Retiring the instance confines a late writer to an
        object nobody reads.
        """
        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        retired = client._liveness_oracle
        # Seed the state a dead generation would leave behind. A replacement that
        # merely isolates writes (a deepcopy, say) would carry these over and the
        # first probe of the new process would delta against the dead one.
        retired._samples["io"] = (0.0, 12_345)
        retired._samples["cpu"] = (0.0, 678)
        retired._tracked_child = 9999
        retired._child_gone_ts = 1.0

        with (
            patch("kiro_crew.session_pid._pid_gone_or_unmanaged", return_value=True),
            patch("kiro_crew.session._untrack_pid"),
            patch("kiro_crew.session._untrack_session_pid"),
        ):
            client._reset_state()

        assert client._liveness_oracle is not retired
        assert client._liveness_oracle._samples == {}
        assert client._liveness_oracle._tracked_child is None
        assert client._liveness_oracle._child_gone_ts is None
        # A late write from the detached walk reaches the retired instance only.
        retired._samples["io"] = (0.0, 999_999)
        assert "io" not in client._liveness_oracle._samples

    @pytest.mark.asyncio
    async def test_every_prompt_path_retires_liveness_state(self, tmp_path):
        """Retirement must sit where all prompt paths funnel, not on one of them.

        ``send_message`` reaches ``_prompt_loop`` via ``_read_prompt_response``,
        and ``send_message_stream`` reaches it directly — neither goes through
        ``_dispatch_events``. Retiring only there leaves the worker-pool prompt
        API carrying the previous turn's wedged consult, so its next turn is
        answered "prior consult still in flight" and reaped at the 90s cutoff.
        ``_prompt_loop`` is the one place every consumer funnels through.
        """
        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        turn_a_oracle = client._liveness_oracle
        turn_a_oracle._samples["io"] = (0.0, 12_345)
        client._consult_future = asyncio.get_running_loop().create_future()

        observed: dict = {}

        async def _record_then_silence(*_args, **_kwargs):
            # Observed on the FIRST read, so retirement deferred to the loop's
            # finally (or to turn completion) would fail here.
            observed.setdefault("oracle", client._liveness_oracle)
            observed.setdefault("future", client._consult_future)
            return None

        client._read_message = _record_then_silence
        client._is_process_alive = lambda: True

        async for _action, _msg in client._prompt_loop(req_id=7, timeout=0.05):
            pass

        assert observed["oracle"] is not turn_a_oracle
        assert observed["future"] is None
        assert observed["oracle"]._samples == {}

    @pytest.mark.asyncio
    async def test_a_queued_turn_does_not_reopen_the_active_turns_gate(self, tmp_path):
        """Retirement must happen under the turn lock, not before it.

        A second queued turn that retires before blocking on ``_turn_lock`` clears
        the *active* turn's tracked consult. The active turn's next silent read
        then sees an open gate and submits a second walk while the first is still
        wedged — defeating the one-outstanding-walk bound this gate exists for.
        """
        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = lambda: True

        # Turn A owns the lock and has a walk in flight.
        await client._turn_lock.acquire()
        turn_a_walk = asyncio.get_running_loop().create_future()
        client._consult_future = turn_a_walk
        turn_a_oracle = client._liveness_oracle

        async def _turn_b():
            async for _action, _msg in client._prompt_loop(req_id=8, timeout=0.05):
                pass

        turn_b = asyncio.ensure_future(_turn_b())
        for _ in range(5):
            await asyncio.sleep(0)

        # Turn B is parked on the lock, so turn A's gate is untouched.
        assert client._consult_future is turn_a_walk
        assert client._liveness_oracle is turn_a_oracle

        client._turn_lock.release()
        await turn_b
        # Only once turn B actually owns the turn does it retire.
        assert client._consult_future is None
        assert client._liveness_oracle is not turn_a_oracle

    @pytest.mark.asyncio
    async def test_the_submitted_walk_is_bound_to_the_oracle_it_sampled(self, tmp_path):
        """The walk must capture its oracle, not resolve one when it finally runs.

        Retirement isolates a late writer only if the submitted callable holds the
        instance it was submitted with. Handing the executor something that
        resolves ``self._liveness_oracle`` at execution time would make a detached
        walk write into whatever oracle is live *then*, silently defeating every
        retirement in this change while leaving the other tests green. This guards
        behaviour that is already correct rather than fixing anything.
        """
        from concurrent.futures import Future as ThreadFuture

        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        submitted_against = client._liveness_oracle

        thread_future: ThreadFuture = ThreadFuture()
        thread_future.set_running_or_notify_cancel()
        pool = MagicMock()
        pool.submit.return_value = thread_future

        _real_wait_for = asyncio.wait_for

        async def _fast_timeout(awaitable, timeout=None):
            return await _real_wait_for(awaitable, timeout=0.01)

        with (
            patch("kiro_crew.acp.client.subprocess_executor", return_value=pool),
            patch("kiro_crew.acp.client.asyncio.wait_for", _fast_timeout),
        ):
            await client._consult_liveness_model_wait()

        walk = pool.submit.call_args[0][0]
        assert getattr(walk, "__self__", None) is submitted_against

        # After retirement the captured callable still targets the retired
        # instance, so a late write cannot reach the live baseline.
        client._retire_liveness_state()
        assert client._liveness_oracle is not submitted_against
        assert walk.__self__ is submitted_against

        thread_future.set_exception(OSError("wedged /proc read"))
        for _ in range(5):
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_a_real_submission_is_recorded_and_gates_the_next_poll(self, tmp_path):
        """The guard is only worth anything if a real submission is tracked.

        Injecting ``_consult_future`` by hand exercises the guard but proves
        nothing about the submission path: if the assignment were dropped, every
        timed-out walk would leave the field ``None`` and the next silent read
        would submit another executor job — the starvation defect this exists to
        stop.
        """
        from concurrent.futures import Future as ThreadFuture

        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        client._liveness_oracle = MagicMock()

        thread_future: ThreadFuture = ThreadFuture()
        thread_future.set_running_or_notify_cancel()
        pool = MagicMock()
        pool.submit.return_value = thread_future

        _real_wait_for = asyncio.wait_for

        async def _fast_timeout(awaitable, timeout=None):
            return await _real_wait_for(awaitable, timeout=0.01)

        with (
            patch("kiro_crew.acp.client.subprocess_executor", return_value=pool),
            patch("kiro_crew.acp.client.asyncio.wait_for", _fast_timeout),
        ):
            assert await client._consult_liveness_model_wait() == (
                VERDICT_UNKNOWN,
                "oracle offload error",
            )
            assert client._consult_future is not None
            assert pool.submit.call_count == 1

            # The recorded future is what closes the guard on the next poll.
            assert await client._consult_liveness_model_wait() == (
                VERDICT_UNKNOWN,
                "prior consult still in flight",
            )
            assert pool.submit.call_count == 1

        thread_future.set_exception(OSError("wedged /proc read"))
        for _ in range(5):
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_retired_oracle_keeps_its_configuration(self, tmp_path):
        """Retirement must not silently swap a caller's oracle config for defaults.

        A client may be handed an oracle pointed at a different ``/proc`` root or a
        different sampling interval. Replacing it with a default-constructed one at
        the generation boundary would silently change probe behaviour.
        """
        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        sentinel_clock = MagicMock(return_value=1.0)
        client._liveness_oracle = LivenessOracle(
            "/fake/proc", now=sentinel_clock, sample_min_secs=0.0
        )
        configured = client._liveness_oracle

        with (
            patch("kiro_crew.session_pid._pid_gone_or_unmanaged", return_value=True),
            patch("kiro_crew.session._untrack_pid"),
            patch("kiro_crew.session._untrack_session_pid"),
        ):
            client._reset_state()

        # Every constructor input is asserted: dropping any one of them from
        # fresh() must fail here. Asserting only the config would also hold if
        # retirement were removed entirely (the configured instance would simply
        # survive), and asserting only replacement would hold for a default-
        # constructed one that silently repoints all three.
        assert client._liveness_oracle is not configured
        assert client._liveness_oracle._proc == "/fake/proc"
        assert client._liveness_oracle._sample_min_secs == 0.0
        assert client._liveness_oracle._now is sentinel_clock

    @pytest.mark.asyncio
    async def test_pending_consult_exception_is_consumed_without_a_reset(self, tmp_path):
        """A stale turn can return with a consult still pending and never reset.

        The retrieval callback must be attached when the walk is SUBMITTED, not
        only when a later poll observes it or ``_reset_state`` releases it. A turn
        that reaches the 90s cutoff returns while the walk is still running; if the
        client then goes idle, a walk that raises afterwards reaches
        ``Future.__del__`` unretrieved and is recorded as an unhandled crash.
        """
        from concurrent.futures import Future as ThreadFuture

        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        client._liveness_oracle = MagicMock()

        thread_future: ThreadFuture = ThreadFuture()
        thread_future.set_running_or_notify_cancel()
        pool = MagicMock()
        pool.submit.return_value = thread_future

        async def _always_times_out(awaitable, timeout=None):
            # Delegate to the REAL wait_for with a tiny timeout: its cancellation
            # of shield's outer future is exactly what detaches the inner-done
            # callback, and a patched raise would leave that callback attached and
            # retrieve the exception for us — a vacuous pass.
            return await _real_wait_for(awaitable, timeout=0.01)

        _real_wait_for = asyncio.wait_for
        with (
            patch("kiro_crew.acp.client.subprocess_executor", return_value=pool),
            patch("kiro_crew.acp.client.asyncio.wait_for", _always_times_out),
        ):
            assert await client._consult_liveness_model_wait() == (
                VERDICT_UNKNOWN,
                "oracle offload error",
            )

        tracked = client._consult_future
        assert tracked is not None and not tracked.done()

        # The walk fails after the turn already returned, with no reset in between.
        thread_future.set_exception(OSError("wedged /proc read"))
        for _ in range(5):
            await asyncio.sleep(0)

        assert tracked.done()
        assert tracked._log_traceback is False

    @pytest.mark.asyncio
    async def test_cancelled_consult_still_consumes_a_later_failure(self, tmp_path):
        """Cancellation is what proves the callback is attached at SUBMISSION.

        Attaching it in the ``except Exception`` arm instead would cover the
        timeout path and look equivalent — but ``CancelledError`` is a
        ``BaseException``, so a turn cancelled while the walk is still running
        would skip it and the walk's later failure would reach ``Future.__del__``
        unretrieved.
        """
        from concurrent.futures import Future as ThreadFuture

        client = AcpClient(work_dir=tmp_path)
        client._pid = 4242
        client._liveness_oracle = MagicMock()

        thread_future: ThreadFuture = ThreadFuture()
        thread_future.set_running_or_notify_cancel()
        pool = MagicMock()
        pool.submit.return_value = thread_future

        with patch("kiro_crew.acp.client.subprocess_executor", return_value=pool):
            task = asyncio.ensure_future(client._consult_liveness_model_wait())
            while client._consult_future is None:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        tracked = client._consult_future
        assert tracked is not None and not tracked.done()

        thread_future.set_exception(OSError("wedged /proc read"))
        for _ in range(5):
            await asyncio.sleep(0)

        assert tracked.done()
        assert tracked._log_traceback is False


class TestStaleTurnOpenToolCall:
    """An OPEN tool call defers the stale-turn cutoff.

    Only kiro-cli streams ``tool_call_update`` progress frames while a tool
    runs; a third-party backend that runs a tool to completion and only then
    reports the result is silent for the whole tool. Text streamed before the
    dispatch leaves ``_stale_eligible`` set, and off Linux the liveness oracle
    can only answer UNKNOWN, so the cutoff declared that turn complete and tore
    it down MID-TOOL. An open tool call is positive evidence the turn is alive:
    the silence is handed to the tool-stall watchdog, which owns a much longer
    budget and RECOVERS the slot instead of reporting a truncated turn as done.
    """

    def _silent_client(self, tmp_path, *, tool_dispatched: bool):
        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = MagicMock(return_value=True)
        client._stale_eligible = True
        client._tool_dispatched = tool_dispatched
        client._last_activity = time.monotonic()
        # The verdict every /proc-less host returns — the reporter's own log line.
        client._consult_liveness_model_wait = AsyncMock(
            return_value=(VERDICT_UNKNOWN, "no readable counters")
        )
        return client

    @pytest.mark.asyncio
    async def test_open_tool_call_defers_the_cutoff(self, tmp_path, caplog, monkeypatch):
        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 30.0)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)
        client = self._silent_client(tmp_path, tool_dispatched=True)

        t0 = time.monotonic()
        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=85, timeout=0.4):
                    actions.append(action)
        elapsed = time.monotonic() - t0

        assert actions == []
        assert "Stale turn detected" not in caplog.text
        assert elapsed >= 0.3, f"loop exited at {elapsed:.2f}s — the cutoff reaped an open tool"

    @pytest.mark.asyncio
    async def test_a_resolved_tool_re_arms_the_cutoff(self, tmp_path, caplog, monkeypatch):
        """Deferral is not a disarm: once the tool resolves, the cutoff fires."""
        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 30.0)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)
        client = self._silent_client(tmp_path, tool_dispatched=True)

        async def _resolve_then_stay_silent(*_args, **_kwargs):
            # flake-ok: pre-existing 20 ms pacing in the stubbed read, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing pacing inside the stubbed read before the tool resolves, moved verbatim
            await asyncio.sleep(0.02)
            # What _dispatch_events does when the tool's result frame arrives.
            client._tool_dispatched = False
            return None

        client._read_message = AsyncMock(side_effect=_resolve_then_stay_silent)

        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=86, timeout=5.0):
                    actions.append(action)

        assert actions == []
        assert "Stale turn detected" in caplog.text

    @pytest.mark.asyncio
    async def test_a_genuinely_stalled_open_tool_is_still_killed(self, tmp_path, monkeypatch):
        """The deferral must not swallow the tool-stall watchdog.

        Past ITS budget the wedged child is killed and AcpProcessDied raised, so
        the slot cold-starts on the next prompt — the recovery the bare stale
        ``return`` never performed.
        """
        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 0.1)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)
        client = self._silent_client(tmp_path, tool_dispatched=True)
        client._kill_process = AsyncMock()

        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with pytest.raises(AcpProcessDied, match="tool stalled"):
                async for _action, _msg in client._prompt_loop(req_id=87, timeout=5.0):
                    pass

        client._kill_process.assert_awaited_once()


class TestStaleTurnProbeAccounting:
    """The stale cutoff must measure BACKEND silence, not our own probe.

    Two ways the gate can end a live turn on no evidence at all:

    * the idle clock was read AFTER awaiting the liveness consult, so a cold
      walk's latency (executor warm-up plus the subtree read) was charged to the
      backend's silence budget;
    * the oracle's baseline-priming answer (``EVIDENCE_SAMPLING``) counted as a
      negative verdict, although it is structurally incapable of reading WORKING
      -- it means "ask me again", not "idle".

    Together they reaped a turn on its FIRST silent read, which is the
    truncation the per-silent-read consulting exists to avoid.
    """

    def _silent_client(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = MagicMock(return_value=True)
        client._stale_eligible = True
        client._last_activity = time.monotonic()
        return client

    @pytest.mark.asyncio
    async def test_priming_verdict_defers_then_real_evidence_reaps(
        self, tmp_path, caplog, monkeypatch
    ):
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)
        client = self._silent_client(tmp_path)
        client._consult_liveness_model_wait = AsyncMock(
            side_effect=[
                (VERDICT_UNKNOWN, EVIDENCE_SAMPLING),  # no baseline yet
                (VERDICT_UNKNOWN, "flat counters"),  # a real answer
            ]
        )

        # Cutoff 0 so every silent read is past it: the only thing that can keep
        # the turn alive on the first read is the priming deferral.
        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.0):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                async for _action, _msg in client._prompt_loop(req_id=88, timeout=5.0):
                    pass

        assert "Stale turn detected" in caplog.text  # recovery still fires
        assert (
            client._consult_liveness_model_wait.await_count == 2
        ), "the turn was ended on the priming answer instead of on real evidence"

    @pytest.mark.asyncio
    async def test_consult_latency_is_not_charged_to_the_silence_budget(
        self, tmp_path, caplog, monkeypatch
    ):
        """A consult slower than the cutoff must not itself end the turn."""
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.01)
        client = self._silent_client(tmp_path)
        consults = {"n": 0}

        async def _slow_consult():
            consults["n"] += 1
            # flake-ok: the stubbed consult outlasts the patched cutoff on purpose; the assertion is about that latency
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed consult is slower than the cutoff on purpose; the test measures that latency
            await asyncio.sleep(0.15)  # 3x the cutoff patched below
            return VERDICT_UNKNOWN, "flat counters"

        client._consult_liveness_model_wait = _slow_consult

        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                async for _action, _msg in client._prompt_loop(req_id=89, timeout=5.0):
                    pass

        assert "Stale turn detected" in caplog.text
        assert (
            consults["n"] >= 2
        ), "the first consult's own 0.15s runtime was counted as backend silence"


class TestToolStallWatchdog:
    """A tool dispatched that never returns must abort the turn via the
    _TOOL_STALL_TIMEOUT watchdog, not hang to the full prompt timeout."""

    @pytest.mark.asyncio
    async def test_dispatched_tool_with_no_data_exits(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as acp_client

        # Shrink the windows so the watchdog trips quickly under test.
        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        # Simulate "a tool was dispatched this turn".
        client._tool_dispatched = True
        client._stale_eligible = False  # the stale check must NOT be what saves us
        client._last_activity = time.monotonic() - 10.0  # stderr/keepalive also silent
        # Process is alive but silent: _read_message always returns None.
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = lambda: True
        # Recovery kills the wedged child; stub it so the test doesn't touch a
        # real process (there is none — AcpClient was not spawned).
        client._kill_process = AsyncMock()

        actions = []
        # Generous outer timeout; the watchdog must end the loop well before it.
        t0 = time.monotonic()
        with pytest.raises(AcpProcessDied, match="tool stalled"):
            async for action, _ in client._prompt_loop(req_id=1, timeout=30.0):
                actions.append(action)
        elapsed = time.monotonic() - t0

        # No spurious actions, the wedged child was killed, and the turn-done
        # waiter was released via the finally so no cooperative-stop hangs.
        assert actions == []
        client._kill_process.assert_awaited_once()
        assert client._turn_done.is_set()
        # Prove it was the watchdog (stall window 0.2s) that ended the loop,
        # not the outer 30s deadline — guards against the watchdog branch being
        # removed and the test still passing on the outer timeout.
        assert elapsed < 5.0, f"loop ran too long ({elapsed:.2f}s) — watchdog may not have fired"

    @pytest.mark.asyncio
    async def test_no_dispatch_does_not_trip_watchdog(self, tmp_path, monkeypatch):
        # Without a dispatched tool, the stall watchdog must NOT fire; the loop
        # simply runs to its own deadline (proving the guard is gated on the flag).
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._tool_dispatched = False
        client._stale_eligible = False
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = lambda: True

        actions = []
        # Short outer timeout: the loop should exhaust it (no early watchdog exit).
        t0 = time.monotonic()
        async for action, _ in client._prompt_loop(req_id=1, timeout=0.5):
            actions.append(action)
        elapsed = time.monotonic() - t0

        assert actions == []
        assert client._turn_done.is_set()
        # The guard is gated on _tool_dispatched, so with the flag clear the
        # watchdog (0.2s stall window) must NOT fire — the loop runs to its own
        # 0.5s deadline. If the `self._tool_dispatched and ...` guard were
        # removed, the loop would exit ~0.2s early and this assertion would fail.
        assert (
            elapsed >= 0.4
        ), f"loop exited too early ({elapsed:.2f}s) — watchdog gate may be broken"

    @pytest.mark.asyncio
    async def test_progress_frame_then_stall_still_trips(self, tmp_path, monkeypatch):
        # Regression: a dispatched tool that emits ONE inbound frame and then
        # goes silent must still trip the watchdog.  _prompt_loop must not clear
        # _tool_dispatched on inbound frames — only an actual tool result or
        # turn completion (handled in _dispatch_events) clears it.  The
        # last_data_ts reset alone prevents false positives for tools that keep
        # streaming.
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._tool_dispatched = True  # a tool was dispatched this turn
        client._stale_eligible = False
        client._last_activity = time.monotonic() - 10.0  # stderr/keepalive silent
        client._kill_process = AsyncMock()

        # One progress frame, then silence forever.
        frames = [JsonRpcMessage(method="session/update", params={})]

        async def fake_read(*_args, **_kwargs):
            return frames.pop(0) if frames else None

        client._read_message = fake_read  # type: ignore[assignment]
        client._is_process_alive = lambda: True

        actions = []
        # The single frame is yielded as an action; then the watchdog must end
        # the loop well before this generous outer timeout.
        with pytest.raises(AcpProcessDied, match="tool stalled"):
            async for action, _ in client._prompt_loop(req_id=1, timeout=30.0):
                actions.append(action)

        # The flag was NOT cleared by the inbound frame (that's _dispatch_events'
        # job), so the watchdog still fired, killed the child, and raised.
        assert client._tool_dispatched is True
        client._kill_process.assert_awaited_once()
        assert client._turn_done.is_set()

    @pytest.mark.asyncio
    async def test_complete_clears_tool_dispatched(self, monkeypatch):
        # Covers the EVENT_COMPLETE clear path in _dispatch_events: a turn that
        # ends while a tool is still marked dispatched must disarm the flag so a
        # stale True can't leak into the next turn's watchdog.
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        # No tool results to flush on the complete path.
        monkeypatch.setattr(client, "_read_new_tool_results_sync", lambda: [])
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop  # type: ignore[assignment]
        client._tool_dispatched = True  # a tool was in flight when the turn ended

        async for _ in client._dispatch_events(req_id=1, timeout=5.0):
            pass

        assert client._tool_dispatched is False

    @pytest.mark.asyncio
    async def test_tool_result_clears_tool_dispatched(self, monkeypatch):
        # Covers the tool_call_update clear path in _dispatch_events: when the
        # dispatched tool produces a result, the watchdog is disarmed.
        from kiro_crew.acp.types import (
            EVENT_TOOL_CALL_UPDATE,
            METHOD_SESSION_UPDATE,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        monkeypatch.setattr(client, "_read_new_tool_results_sync", lambda: [])
        # An update frame that carries a tool result (no text chunk, no new
        # tool_call) — drive only the tool_call_update branch.
        monkeypatch.setattr(client, "_extract_text_chunk", lambda msg: ("", False))
        monkeypatch.setattr(client, "_extract_tool_event", lambda msg: None)
        monkeypatch.setattr(
            client,
            "_extract_tool_call_update",
            lambda msg: AcpEvent(kind=EVENT_TOOL_CALL_UPDATE, tool_call_id="t1"),
        )
        monkeypatch.setattr(client, "_extract_tool_call_refinement", lambda msg: None)
        update_msg = JsonRpcMessage(method=METHOD_SESSION_UPDATE, params={})
        complete_msg = JsonRpcMessage(id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", update_msg
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop  # type: ignore[assignment]
        client._tool_dispatched = True

        saw_result = False
        async for event in client._dispatch_events(req_id=1, timeout=5.0):
            if event.kind == EVENT_TOOL_CALL_UPDATE:
                # Cleared the instant the result is dispatched, before complete.
                assert client._tool_dispatched is False
                saw_result = True

        assert saw_result
        assert client._tool_dispatched is False


class TestToolStallKeepalive:
    """Keepalive pings (touch_activity) must prevent false tool-stall timeouts
    for MCP tools that legitimately block (wait, spawn_sub_agents)."""

    @pytest.mark.asyncio
    async def test_keepalive_prevents_tool_stall(self, tmp_path, monkeypatch):
        """When _last_activity is refreshed by keepalive pings, the tool stall
        watchdog must NOT fire even if no JSON-RPC stdout data arrives."""
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 0.5)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.05)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._tool_dispatched = True
        client._stale_eligible = False
        client._is_process_alive = lambda: True

        # _read_message must actually yield to the event loop (simulating the
        # real readline timeout) so the keepalive pinger gets scheduled.
        async def slow_read(*_a, **_kw):
            # flake-ok: pre-existing 50 ms pacing in the stubbed read, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing pacing inside the stubbed read so the keepalive pinger runs, moved verbatim
            await asyncio.sleep(0.05)
            return None

        client._read_message = slow_read  # type: ignore[assignment]

        # Simulate keepalive pings refreshing _last_activity every 0.1s
        # while no stdout data arrives.
        stop = asyncio.Event()

        async def keepalive_pinger():
            while not stop.is_set():
                client.touch_activity()
                # Pings well inside the stall window are what keep the watchdog quiet.
                await asyncio.sleep(
                    0.1
                )  # flake-ok: the keepalive cadence is the stimulus under test

        pinger = asyncio.create_task(keepalive_pinger())

        actions: list[object] = []
        # Outer timeout 1.2s > stall window 0.5s.  Without the fix, the
        # watchdog would fire at ~0.5s.  With the fix, keepalive keeps it
        # alive and the loop runs to the outer deadline.
        t0 = time.monotonic()
        async for action, _ in client._prompt_loop(req_id=1, timeout=1.2):
            actions.append(action)
        elapsed = time.monotonic() - t0

        stop.set()
        await pinger

        # Loop ran to outer timeout (1.2s), NOT cut short by stall watchdog (0.5s).
        assert elapsed >= 1.0, f"loop exited at {elapsed:.2f}s — watchdog fired despite keepalive"
        assert actions == []

    @pytest.mark.asyncio
    async def test_genuine_stall_still_fires_without_keepalive(self, tmp_path, monkeypatch):
        """Without keepalive pings, the tool stall watchdog must still fire
        when a dispatched tool goes completely silent."""
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._tool_dispatched = True
        client._stale_eligible = False
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = lambda: True
        client._kill_process = AsyncMock()
        # No keepalive pings — _last_activity stays at construction time.

        actions: list[object] = []
        t0 = time.monotonic()
        with pytest.raises(AcpProcessDied, match="tool stalled"):
            async for action, _ in client._prompt_loop(req_id=1, timeout=30.0):
                actions.append(action)
        elapsed = time.monotonic() - t0

        # Watchdog fired quickly (at ~0.2s stall window), NOT the outer 30s,
        # killed the wedged child, and raised.
        assert elapsed < 5.0, f"watchdog didn't fire ({elapsed:.2f}s)"
        assert actions == []
        client._kill_process.assert_awaited_once()
        assert client._turn_done.is_set()
