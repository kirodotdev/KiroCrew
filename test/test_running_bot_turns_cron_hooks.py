"""A scheduled run and a webhook turn note the Bot they run as (``running_bot_turns``).

The scheduled run is driven through the gateway's own cron callback, on the harness
of ``test_gateway_cron_turn.py``. The webhook turn runs the real ``_run_hook_agent``
and ``_run_hook_inner`` up to the session claim. Both note the execution the turn is
bound to, after it is bound, and take the note out first thing in their ``finally``.
Only a turn that runs as the Bot itself is noted. A Bot's own scheduled run is, also
where the Bot runs it on its worker; a job made from a worker session, an agent
sequence and a webhook request that names another agent are not.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import test_gateway_cron_turn as cron_kit
from chat_test_helpers import _make_state
from cron_gateway_integration_helpers import _make_llm_job

from kiro_crew import context, execution_context, mate_worker
from kiro_crew.dashboard import running_bot_turns
from kiro_crew.dashboard.handlers import hooks
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.testing.wait import finished

MEMBER = "writer-1"
SESSION_KEY = "hook:default:1791000001"


def _as_bot(member_id: str = MEMBER) -> ExecutionContext:
    """A Bot's own execution: selected as a member, on its own agent."""
    return ExecutionContext(
        member_id, MemoryStoreRef(f"member-{member_id}", member_id), "member", "kirocrew"
    )


def _as_template() -> ExecutionContext:
    return ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")


def _raises(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("the table is broken")


class _BrokenTable(dict):
    get = _raises
    values = _raises
    __setitem__ = _raises


# ── a scheduled run ──────────────────────────────────────────────────────────


@pytest.fixture
def fire(monkeypatch: pytest.MonkeyPatch) -> cron_kit._Fire:
    harness = cron_kit._Fire(monkeypatch)
    # A Bot with no file of its own: its scheduled turn runs as the Bot.
    monkeypatch.setattr(mate_worker, "cron_worker_execution", lambda *_a, **_k: None)
    yield harness
    cron_kit.runtime_death._reset_for_tests()


def _seen_in_the_stream(fire: cron_kit._Fire) -> list[frozenset[str]]:
    seen: list[frozenset[str]] = []
    fire.stream_hook = lambda _kw: seen.append(running_bot_turns.running_member_ids())
    return seen


def _bot_job(**fields: Any) -> Any:
    return _make_llm_job(execution_context=_as_bot().to_record(), **fields)


@pytest.mark.asyncio
async def test_a_scheduled_run_as_a_bot_is_running_until_the_run_ends(fire) -> None:
    seen = _seen_in_the_stream(fire)

    assert await fire.fire(_bot_job()) == "Agent response here"

    assert seen == [frozenset({MEMBER})]
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
async def test_a_scheduled_run_on_a_template_notes_no_bot(fire) -> None:
    seen = _seen_in_the_stream(fire)

    await fire.fire(_make_llm_job())
    await fire.fire(_make_llm_job(execution_context=_as_template().to_record()))

    assert seen == [frozenset(), frozenset()]
    assert not running_bot_turns._RUNNING


def _on_its_worker(execution: ExecutionContext) -> ExecutionContext:
    """The record a Bot that owns its agent file runs work on: its worker, member kept."""
    return execution.with_template(mate_worker.MATE_WORKER_AGENT, mate_worker.MATE_WORKER_AGENT)


@pytest.mark.asyncio
async def test_a_bots_scheduled_run_on_its_worker_is_the_bots_own(fire, monkeypatch) -> None:
    """A Bot that owns its file runs its scheduled turn on its worker: a template
    selection that keeps the member id. The job is the Bot's, so the run is noted
    under the member id the fire captured before that swap."""
    monkeypatch.setattr(
        mate_worker, "cron_worker_execution", lambda execution, *_a, **_k: _on_its_worker(execution)
    )
    bound: list[Any] = []
    monkeypatch.setattr(
        execution_context, "bind_session_execution", lambda *args, **_kw: bound.append(args[1])
    )
    seen = _seen_in_the_stream(fire)

    await fire.fire(_bot_job())

    # The run was bound to the worker record, and still reads as the Bot's.
    assert bound and mate_worker.is_own_worker_execution(bound[0])
    assert seen == [frozenset({MEMBER})]
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
async def test_a_job_whose_own_record_is_a_worker_is_not_noted(fire) -> None:
    """A job made from a worker session captured that worker: a template selection.
    Nothing selected the Bot as a member, so the run is a helper's."""
    seen = _seen_in_the_stream(fire)

    await fire.fire(_make_llm_job(execution_context=_on_its_worker(_as_bot()).to_record()))

    assert seen == [frozenset()]
    assert not running_bot_turns._RUNNING


def test_a_worker_session_outside_a_schedule_is_still_not_noted() -> None:
    """The scheduled run alone reads a worker as the Bot: the general note does not."""
    turn = SimpleNamespace()
    running_bot_turns.begin(turn, _on_its_worker(_as_bot()))
    assert running_bot_turns.running_member_ids() == frozenset()
    running_bot_turns.begin_scheduled(turn, MEMBER)
    assert running_bot_turns.running_member_ids() == {MEMBER}
    running_bot_turns.end(turn)
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
async def test_no_step_of_an_agent_sequence_is_noted(fire) -> None:
    job = cron_kit._sequence_job()
    job.execution_context = _as_bot().to_record()
    seen = _seen_in_the_stream(fire)

    await fire.fire(job)

    assert seen and set(seen) == {frozenset()}
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
async def test_a_run_that_never_gets_its_session_is_not_noted(fire, monkeypatch) -> None:
    noted: list[Any] = []
    real_begin = running_bot_turns.begin_scheduled
    monkeypatch.setattr(
        running_bot_turns,
        "begin_scheduled",
        lambda *args: (noted.append(args), real_begin(*args))[1],
    )
    fire.acquire_errors = [RuntimeError("session store unreadable")]

    with pytest.raises(RuntimeError):
        await fire.fire(_bot_job())

    assert noted == []
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("stream broke"), asyncio.CancelledError()], ids=["fails", "cancelled"]
)
async def test_a_run_that_ends_in_its_stream_leaves_no_entry(fire, error) -> None:
    seen: list[frozenset[str]] = []

    def stream_hook(_kw: Any) -> None:
        seen.append(running_bot_turns.running_member_ids())
        raise error

    fire.stream_hook = stream_hook

    with pytest.raises(type(error)):
        await fire.fire(_bot_job())

    assert seen == [frozenset({MEMBER})]
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
async def test_a_retried_run_leaves_no_entry(fire) -> None:
    fire.acquire_errors = [cron_kit.AcpProcessDied("ACP process pipe broken: Connection lost")]
    seen = _seen_in_the_stream(fire)

    assert await fire.fire(_bot_job()) == "Agent response here"

    assert fire.names().count("acquire") == 2
    assert seen == [frozenset({MEMBER})]
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["_home", "_RUNNING"])
async def test_a_fault_in_the_table_does_not_reach_the_scheduled_run(
    fire, monkeypatch, broken
) -> None:
    monkeypatch.setattr(running_bot_turns, broken, _raises if broken == "_home" else _BrokenTable())

    assert await fire.fire(_bot_job()) == "Agent response here"
    assert fire.names()[-4:] == ["release", "pending?", "reset", "clear"]


# ── a webhook turn ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _pin_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    _floor_monkeypatch.setattr("kiro_crew.webhooks.config_dir", lambda: tmp_path)


@pytest.fixture
def hook(tmp_path, monkeypatch) -> SimpleNamespace:
    """A hook state whose session claim records the table and then fails.

    The turn runs the real runner and the real inner turn as far as the claim; the
    session's record is what ``prior`` holds, and the bind is recorded, not written.
    """
    permits_before = hooks._hook_semaphore._value
    state = _make_state(tmp_path)
    state.sessions.record_failure = AsyncMock()
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.notify = MagicMock()
    state.slack_client = None
    seen = SimpleNamespace(
        state=state,
        at_claim=[],
        bound=[],
        prior=None,
        error=RuntimeError("the session could not be claimed"),
    )

    async def claim(*_args: Any, **_kwargs: Any) -> Any:
        seen.at_claim.append(running_bot_turns.running_member_ids())
        raise seen.error

    state.sessions.get_or_create = AsyncMock(side_effect=claim)
    monkeypatch.setattr(execution_context, "read_session_execution", lambda _key: seen.prior)
    monkeypatch.setattr(
        execution_context, "bind_session_execution", lambda *args, **_kw: seen.bound.append(args)
    )
    monkeypatch.setattr(context, "session_store_for_turn", AsyncMock(return_value=None))
    yield seen
    assert hooks._hook_semaphore._value == permits_before
    assert SESSION_KEY not in hooks._hook_inflight_sessions


async def _run_hook(
    seen: SimpleNamespace,
    execution: ExecutionContext,
    *,
    agent: str | None = None,
) -> tuple[str, str]:
    """Run the hook the way the route handler hands it to the runner."""
    hooks._hook_inflight_sessions.add(SESSION_KEY)
    await hooks._hook_semaphore.acquire()
    return await finished(
        hooks._run_hook_agent(
            seen.state,
            SESSION_KEY,
            "deploy finished",
            "ci",
            agent,
            True,
            300,
            execution_context=execution,
        ),
        "hook agent",
    )


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_webhook_turn_as_a_bot_is_running_until_the_turn_ends(hook) -> None:
    outcome, _text = await _run_hook(hook, _as_bot())

    assert outcome == "error"  # the claim failed, after the note
    assert len(hook.bound) == 1
    assert hook.at_claim == [frozenset({MEMBER})]
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_webhook_turn_that_times_out_leaves_no_entry(hook) -> None:
    # What the runner's own time limit raises into the turn.
    hook.error = asyncio.TimeoutError()

    outcome, _text = await _run_hook(hook, _as_bot())

    assert outcome == "timeout"
    assert hook.at_claim == [frozenset({MEMBER})]
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_webhook_request_that_names_an_agent_is_not_noted(hook) -> None:
    """The memory is the Bot's and the agent is the caller's pick: not the Bot's own turn."""
    await _run_hook(hook, _as_bot(), agent="some-other-agent")

    assert len(hook.bound) == 1
    assert hook.at_claim == [frozenset()]
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "execution",
    [_as_template(), _as_bot().with_template("kirocrew-mate-worker", "kirocrew-mate-worker")],
    ids=["a template", "a Bot's worker"],
)
async def test_a_webhook_registered_by_no_bot_itself_is_not_noted(hook, execution) -> None:
    await _run_hook(hook, execution)

    assert hook.at_claim == [frozenset()]
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_webhook_whose_session_is_another_bots_is_refused_and_not_noted(hook) -> None:
    hook.prior = _as_bot("reviewer-1")

    outcome, _text = await _run_hook(hook, _as_bot())

    assert outcome == "error"
    assert hook.bound == [] and hook.at_claim == []
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize("broken", ["_home", "_RUNNING"])
async def test_a_fault_in_the_table_does_not_reach_the_webhook_turn(
    hook, monkeypatch, broken
) -> None:
    monkeypatch.setattr(running_bot_turns, broken, _raises if broken == "_home" else _BrokenTable())

    await _run_hook(hook, _as_bot())

    # The turn went on to its claim, and its teardown ran.
    assert len(hook.at_claim) == 1
    hook.state.sessions.release.assert_called_once_with(SESSION_KEY)
