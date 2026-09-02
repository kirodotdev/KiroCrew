"""A cron run whose every tool call was security-blocked must record failure.

The model still returns plausible prose when the security gate blocks every tool
it tries, so the reply text cannot carry the verdict. A success resets
``consecutive_failures`` and clears ``auto_paused``, so recording one here would
keep the auto-pause guard permanently out of reach for a job that is
structurally incapable of succeeding. These tests pin the verdict AND the guard
it depends on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD, CronJob, CronSchedule

# (title, approved, security_blocked) triples a turn's tool gate reports.
GateScript = list[tuple[str, bool, bool]]

# The two refusal classes the verdict must treat differently.
BLOCKED = ("rm -rf /", False, True)
UNAPPROVED = ("Read README.md", False, False)
APPROVED = ("Read README.md", True, False)


def _run_cron_runs(
    gate: GateScript,
    runs: int = 1,
    approval_mode: str = "",
    agent_sequence: list[str] | None = None,
    per_agent: dict[str, GateScript] | None = None,
    job: CronJob | None = None,
    deliver_raises: bool = False,
) -> CronJob:
    """Drive the real ``_cron_callback`` *runs* times, replaying *gate* each turn.

    *per_agent* overrides *gate* for named agents, so a sequence can be scripted
    asymmetrically — replaying one script for every agent makes a per-agent tally
    and a run-scoped one reach the same verdict, so nothing distinguishes them.

    *job* reuses an existing job across calls, which is what makes a recovery
    assertion meaningful: a fresh job's counter reads 0 either way.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
    gw.conv_log = None
    gw.dashboard_state = None
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._no_crons = False
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.cancel_current = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="interactive_cb")
    if deliver_raises:
        # Fails the dashboard history read that runs AFTER the gate verdict and
        # is NOT wrapped in a local handler (the Slack post below it is), so the
        # enclosing handler sees an exception on a run the verdict already
        # counted. This is the reachable shape of the double count.
        ds = MagicMock()
        ds.has_slot = MagicMock(return_value=True)
        ds.conversation_log.read_messages = MagicMock(
            side_effect=RuntimeError("history read failed")
        )
        gw.dashboard_state = ds

    seq = list(agent_sequence or [])
    turn = {"n": 0}

    async def fake_stream(client, msg, **kwargs):
        report: Callable[[str, bool, bool], None] | None = kwargs.get("on_tool_gate")
        assert report is not None, "the cron path must observe its tool-gate decisions"
        script = gate
        if per_agent and seq:
            script = per_agent.get(seq[turn["n"] % len(seq)], gate)
        turn["n"] += 1
        for title, approved, blocked in script:
            report(title, approved, blocked)
        # A blocked turn still produces prose — that is the whole problem.
        return "I was unable to complete the requested changes."

    if job is None:
        job = CronJob(
            id="g1",
            name="selfheal",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=900),
            approval_mode=approval_mode,
            agent_sequence=seq,
        )

    captured_cb = None

    with patch("kiro_crew.slack.gateway.stream_and_collect", fake_stream), patch(
        "kiro_crew.slack.gateway.CronService"
    ) as mock_cron_cls:

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            for _ in range(runs):
                await captured_cb(job)

        asyncio.run(_init_and_run())

    return job


def test_a_fully_blocked_run_records_error() -> None:
    job = _run_cron_runs([BLOCKED, ("cat ~/.ssh/id_rsa", False, True)])

    assert job.last_status == "error", "a run that accomplished nothing recorded success"
    assert job.consecutive_failures == 1
    assert "blocked by the security gate" in job.last_error


def test_a_fully_blocked_run_names_the_refused_count() -> None:
    """The operator has to be able to tell WHY the job failed from the job list."""
    job = _run_cron_runs([("a", False, True), ("b", False, True), ("c", False, True)])

    assert "all 3 tool call(s) blocked" in job.last_error


def test_a_run_that_got_one_tool_through_is_a_success() -> None:
    """Blocked-then-adapted is the normal, healthy path — it must not be a failure."""
    job = _run_cron_runs([BLOCKED, APPROVED])

    assert job.last_status != "error"
    assert job.consecutive_failures == 0


def test_a_tool_free_run_is_still_a_success() -> None:
    """Plenty of crons only summarize or notify. No gate decisions must read as clean."""
    job = _run_cron_runs([])

    assert job.last_status != "error"
    assert job.consecutive_failures == 0


def test_an_unapproved_run_is_not_a_failure() -> None:
    """An unattended cron's approval request deny-fasts on a timeout, arriving
    unapproved but not security-blocked. Counting it would fail — and after
    five runs durably auto-pause — a job whose only problem is that no approver
    was present, and the fire-time governance denials in the same function
    deliberately keep policy state out of the failure counter for this reason.
    """
    job = _run_cron_runs([UNAPPROVED], runs=_AUTO_PAUSE_THRESHOLD)

    assert job.last_status != "error"
    assert job.consecutive_failures == 0
    assert job.auto_paused is False
    assert job.enabled is True


def test_a_mixed_refusal_run_is_not_a_failure() -> None:
    """A security block ALONGSIDE an unresolved refusal proves nothing.

    The unresolved tool might have run under a looser policy or with an approver
    present, so the run does not evidence a job that cannot work — and counting
    it would auto-pause a healthy one.
    """
    job = _run_cron_runs([BLOCKED, UNAPPROVED], runs=_AUTO_PAUSE_THRESHOLD)

    assert job.last_status != "error"
    assert job.consecutive_failures == 0
    assert job.auto_paused is False
    assert job.enabled is True


def test_a_blocked_run_counts_once_even_when_delivery_fails() -> None:
    """One run must move the failure counter by exactly one.

    The gate verdict and the enclosing exception handler are two writers to the
    same counter. A blocked turn whose dashboard history read then fails reaches
    both — that read sits after the verdict and, unlike the Slack post below it,
    carries no local handler. Counting twice would trip the auto-pause threshold
    in three runs instead of five, pausing a job on arithmetic rather than on
    five distinct failures.

    The handler re-raises after counting (that is how ``CronScheduler._execute``
    learns the run failed), so the job is created here to survive the raise.
    """
    job = CronJob(
        id="g1",
        name="selfheal",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=900),
    )

    with pytest.raises(RuntimeError, match="history read failed"):
        _run_cron_runs([("Read /etc/shadow", False, True)], job=job, deliver_raises=True)

    assert (
        job.consecutive_failures == 1
    ), f"a single blocked run was counted {job.consecutive_failures} times"


def test_clearing_an_auto_pause_re_enables_the_job() -> None:
    """``enabled`` is not independent state, so it must move with the pause.

    ``_job_enabled`` reconstructs it on load as ``not user_paused and not
    auto_paused``. A job left disabled-but-not-auto-paused is paused in memory
    and enabled on disk, so it stays stopped until a restart silently resumes
    it — the surprise a manual run on an auto-paused job used to create.
    """
    job = CronJob(
        id="g1",
        name="selfheal",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=900),
    )
    job.auto_paused = True
    job.enabled = False

    job.record_success()

    assert job.auto_paused is False
    assert job.enabled is True, "a cleared auto-pause left the job stopped until a restart"


def test_clearing_an_auto_pause_respects_a_user_pause() -> None:
    """Re-enabling a job the user paused is the user's action, not a run's."""
    job = CronJob(
        id="g1",
        name="selfheal",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=900),
    )
    job.auto_paused = True
    job.enabled = False
    job.user_paused = True

    job.record_success()

    assert job.auto_paused is False
    assert job.enabled is False, "a success overrode the user's own pause"


def test_a_success_leaves_the_persisted_enabled_invariant_intact() -> None:
    """The in-memory flag must agree with what a reload would compute."""
    job = CronJob(
        id="g1",
        name="selfheal",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=900),
    )
    job.auto_paused = True
    job.enabled = False

    job.record_success()

    assert job.enabled == (not job.user_paused and not job.auto_paused)


def test_repeated_fully_blocked_runs_auto_pause_the_job() -> None:
    """A job that cannot succeed stops re-firing once it crosses the threshold."""
    job = _run_cron_runs([BLOCKED], runs=_AUTO_PAUSE_THRESHOLD)

    assert job.consecutive_failures == _AUTO_PAUSE_THRESHOLD
    assert job.auto_paused is True
    assert job.enabled is False


def test_one_run_short_of_the_threshold_stays_enabled() -> None:
    """The guard must not fire early — that would pause healthy-but-flaky jobs."""
    job = _run_cron_runs([BLOCKED], runs=_AUTO_PAUSE_THRESHOLD - 1)

    assert job.auto_paused is False
    assert job.enabled is True


def test_a_success_after_blocked_runs_clears_the_counter() -> None:
    """Recovery works: a job is not permanently marked once it succeeds again.

    The clean run reuses the SAME job — a fresh one reads 0 either way, so it
    would assert nothing about the success arm.
    """
    job = _run_cron_runs([BLOCKED], runs=2)
    assert job.consecutive_failures == 2

    _run_cron_runs([APPROVED], job=job)
    assert job.consecutive_failures == 0


def test_auto_mode_applies_the_verdict_too() -> None:
    """approval_mode="auto" takes the AUTO_APPROVE branch, which still passes
    on_tool_gate and still applies the verdict. The always-enforced deny checks
    that make a refusal reachable there live in _resolve_permission, which this
    harness replaces — test_llm_helpers_tool_gate covers that half."""
    job = _run_cron_runs([BLOCKED], approval_mode="auto")

    assert job.last_status == "error"
    assert job.consecutive_failures == 1


class TestMultiAgentSequence:
    """The sequential (agent_sequence) path carries the same verdict as the
    single-agent one, so a multi-agent job's failure counter moves in both
    directions and auto-pause is both reachable and clearable there."""

    _AGENTS = ["researcher", "coder"]

    def test_a_fully_blocked_sequence_records_error(self) -> None:
        job = _run_cron_runs([BLOCKED], agent_sequence=self._AGENTS)

        assert job.last_status == "error"
        assert job.consecutive_failures == 1
        assert "blocked by the security gate" in job.last_error

    def test_the_tally_spans_the_whole_sequence(self) -> None:
        """Scripted asymmetrically on purpose: agent 1 gets a tool through and
        agent 2 is blocked outright. A per-agent tally would fail agent 2 and
        record an error; a run-scoped one sees the run did work."""
        job = _run_cron_runs(
            [],
            agent_sequence=self._AGENTS,
            per_agent={"researcher": [APPROVED], "coder": [BLOCKED]},
        )

        assert job.last_status != "error", "the tally is per-agent, not per-run"
        assert job.consecutive_failures == 0

    def test_repeated_blocked_sequences_auto_pause(self) -> None:
        job = _run_cron_runs(
            [BLOCKED],
            runs=_AUTO_PAUSE_THRESHOLD,
            agent_sequence=self._AGENTS,
        )

        assert job.auto_paused is True
        assert job.enabled is False

    def test_a_clean_sequence_clears_a_prior_auto_pause(self) -> None:
        """The arm that makes the failure arm safe: without a success arm on this
        path, auto_paused set here could never be lifted."""
        job = _run_cron_runs(
            [BLOCKED],
            runs=_AUTO_PAUSE_THRESHOLD,
            agent_sequence=self._AGENTS,
        )
        assert job.auto_paused is True

        job.enabled = True
        _run_cron_runs([APPROVED], agent_sequence=self._AGENTS, job=job)

        assert job.auto_paused is False
        assert job.consecutive_failures == 0


class TestSchedulerContract:
    """The cron callback signals failure by MUTATING last_status rather than
    raising, so these pin the two scheduler behaviors that verdict rides on.
    Neither is covered elsewhere, and either one changing would silently restore
    a success verdict for a run that did nothing.
    """

    @pytest.mark.asyncio
    async def test_execute_preserves_an_explicit_error_from_the_callback(self, tmp_path) -> None:
        """``_execute`` sets "ok" only when the callback did not claim an error.

        Without this guard the LLM path's mutation would be overwritten to "ok"
        on the way out and the run would report success again.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)

        async def blocked_cb(job):
            job.last_status = "error"
            job.last_error = "all 1 tool call(s) blocked by the security gate: rm -rf /"
            return "prose the model produced anyway"

        svc._on_job = blocked_cb
        job = svc.add_job("blocked", "go", every_secs=900)
        await svc._execute(job)

        assert job.last_status == "error", "the callback's verdict was overwritten"
        assert "blocked by the security gate" in job.last_error

    @pytest.mark.asyncio
    async def test_an_error_status_becomes_a_failure_history_record(self, tmp_path) -> None:
        """The history status is derived from last_status; pin the mapping so the
        operator-visible run log keeps reflecting a blocked run as a failure."""
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        svc._history.append = AsyncMock()

        async def blocked_cb(job):
            job.last_status = "error"
            job.last_error = "all 2 tool call(s) blocked by the security gate: a, b"
            return "prose"

        svc._on_job = blocked_cb
        job = svc.add_job("blocked", "go", every_secs=900)
        await svc._run_job_isolated(job)

        assert svc._history.append.await_count == 1
        record = svc._history.append.await_args.args[0]
        assert record.status == "failure"

    @pytest.mark.asyncio
    async def test_a_clean_run_still_becomes_a_success_history_record(self, tmp_path) -> None:
        """The control for the test above — healthy runs must stay "success"."""
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        svc._history.append = AsyncMock()

        async def clean_cb(job):
            return "all good"

        svc._on_job = clean_cb
        job = svc.add_job("okjob", "go", every_secs=900)
        await svc._run_job_isolated(job)

        assert svc._history.append.await_count == 1
        record = svc._history.append.await_args.args[0]
        assert record.status == "success"
