"""The restart idle check names the sessions and jobs a restart is waiting on.

Two integers cannot tell a person whose restart was refused a chat turn from a
script cron that fires every minute. Each item the census counts is a holder
with a kind, an identifier and, where a start was recorded, an age; the counts
are sums over the same holders. Every clock here is injected.
"""

from __future__ import annotations

import json
import unicodedata
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from slack_gateway_cron_exec_coverage_helpers import _cron_cb, _cron_service_double, _job
from slack_gateway_helpers import _make_orchestrator

from kiro_crew import cli_restart_idle as idle_cli
from kiro_crew import quiescence, restart_admission
from kiro_crew.dashboard import chat_utils, slot_retention, stale_wake
from kiro_crew.slack import gateway as gw
from kiro_crew.testing import synthetic_credentials


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Task:
    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _named(census) -> list[dict]:
    """The entries the host-only idle check answers with."""
    return census.named_holders()[0]


def _said(census) -> str:
    """The clause the CLI prints from those entries."""
    return quiescence.work_holders.describe(*census.named_holders())


def _refusal(census) -> str:
    """The refusal sentence every dashboard client and app is sent."""
    return restart_admission.RestartAdmission(False, restart_admission.REASON_BUSY, census).detail()


@pytest.fixture(autouse=True)
def _no_shutdown():
    gw.shutdown_event.clear()
    yield
    gw.shutdown_event.clear()


@pytest.fixture
def orch(monkeypatch):
    """A real orchestrator with nothing running and no session manager."""
    orchestrator = _make_orchestrator()
    orchestrator.dashboard_state = None
    orchestrator.sessions = None
    orchestrator.subagent_mgr = None
    orchestrator.task_runner = None
    orchestrator._session_tasks = {}
    monkeypatch.setattr(gw.inbound_spool, "pending_refusal_write_count", lambda: 0)
    monkeypatch.setattr("kiro_crew.messaging.approval.pending_count", lambda: 0)
    return orchestrator


class _Crons:
    """The two cron service reads the census makes: the job by id, and when its run began."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.started: dict[str, float] = {}

    def get_job(self, job_id: str) -> SimpleNamespace | None:
        name = self.names.get(job_id)
        return None if name is None else SimpleNamespace(name=name)

    def running_since(self, job_id: str) -> float | None:
        return self.started.get(job_id)


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    """The wall clock a cron run's age is read on."""
    manual = _Clock()
    monkeypatch.setattr(quiescence.work_holders, "_now", manual)
    return manual


def _script_running(orch, clock: _Clock, job_id: str, name: str) -> None:
    """A script cron that holds the overlap guard, claimed by the cron service now."""
    if not isinstance(orch.cron_svc, _Crons):
        orch.cron_svc = _Crons()
    orch.cron_svc.names[job_id] = name
    orch.cron_svc.started[job_id] = clock.now
    orch._running_script_ids.add(job_id)


def test_the_script_overlap_guard_is_a_plain_set():
    assert type(_make_orchestrator()._running_script_ids) is set


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,runner",
    [("command", "run_command_sandboxed"), ("script", "run_script_sandboxed")],
)
async def test_a_cron_job_is_named_for_as_long_as_it_runs(kind, runner):
    orchestrator = _make_orchestrator()
    job = _job(
        name="nightly probe",
        **{kind: "probes.py:check" if kind == "script" else "curl example.com"},
    )
    seen: list[Any] = []
    svc = _cron_service_double()
    svc.get_job = lambda job_id: job if job_id == job.id else None
    svc.running_since = lambda job_id: quiescence.work_holders._now() - 5.0

    def _payload(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        seen.extend(
            h
            for h in orchestrator._in_flight_work_census()
            if h.kind == quiescence.work_holders.SCRIPT
        )
        return {"status": "ok", "output": "done"}

    async with _cron_cb(orchestrator, svc=svc) as cb:
        with patch.object(gw, runner, _payload):
            await cb(job)

    assert [(h.name, h.count, h.since_secs is not None) for h in seen] == [
        ("nightly probe", 1, True)
    ]
    assert not [
        h for h in orchestrator._in_flight_work_census() if h.kind == quiescence.work_holders.SCRIPT
    ]


@pytest.mark.asyncio
async def test_a_refused_restart_names_the_script_cron_it_waits_on(orch, clock):
    _script_running(orch, clock, "job-7f3a", "nightly-sync")
    clock.now += 21

    admission = await restart_admission.admit_restart(orch)

    assert admission.reason == restart_admission.REASON_BUSY
    assert admission.census is not None
    assert (admission.census.turns, admission.census.background) == (0, 1)
    assert _named(admission.census) == [
        {"kind": "script", "name": "nightly-sync", "since_secs": 21, "count": 1}
    ]
    assert _said(admission.census) == "nightly-sync script (21 s)"
    # What is broadcast and logged stays the counts: no name leaves the host-only answer.
    assert "nightly-sync" not in admission.detail()
    assert "nightly-sync" not in json.dumps(admission.census.payload())


def test_a_script_that_ended_is_no_longer_named(orch, clock):
    _script_running(orch, clock, "job-7f3a", "nightly-sync")
    orch._running_script_ids.discard("job-7f3a")

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=clock.now)

    assert census.busy == 0
    assert _named(census) == []


@pytest.mark.parametrize(
    "crons",
    [
        None,  # no cron service
        _Crons(),  # the job was removed mid-run and its claim is gone
        SimpleNamespace(),  # a service that cannot answer
    ],
)
def test_a_running_script_the_cron_service_cannot_describe_still_counts(orch, crons):
    orch.cron_svc = crons
    orch._running_script_ids = {"job-7f3a", "job-9c1e"}

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=0.0)

    assert orch._in_flight_work_counts() == (0, 2)
    assert census.background == 2
    assert _named(census) == [{"kind": "script", "name": "", "since_secs": None, "count": 2}]
    assert _said(census) == "script x2"


def test_a_job_the_schedule_list_leaves_out_is_counted_unnamed(orch, clock):
    _script_running(orch, clock, "job-7f3a", "zebra-cron-name")
    orch.cron_svc.get_job = lambda _id: SimpleNamespace(
        name="zebra-cron-name", managed_by=stale_wake.MANAGED_KEY
    )

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=0.0)

    assert census.background == 1
    assert _said(census) == "script"


def test_a_run_whose_claim_was_taken_is_named_without_an_age(orch, clock):
    _script_running(orch, clock, "job-7f3a", "nightly-sync")
    del orch.cron_svc.started["job-7f3a"]

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=0.0)

    assert census.background == 1
    assert _said(census) == "nightly-sync script"


def _chat_state(provider, slot, *, queued: int = 0) -> SimpleNamespace:
    """A dashboard state whose allocation table files the slot's session as the gateway does."""
    session_key = chat_utils.effective_session_key(slot)
    table = {session_key: SimpleNamespace(provider=provider, queue=[object()] * queued)}
    sessions = SimpleNamespace(
        active_providers=lambda: [provider],
        inbound_callback_count=0,
        _allocation_boundary=lambda: SimpleNamespace(_sessions=table),
    )
    return SimpleNamespace(_slots={slot.key: slot}, sessions=sessions, workflow_service=None)


def test_a_running_chat_turn_is_one_entry_under_its_slot_key(orch, monkeypatch):
    monkeypatch.setattr(slot_retention, "has_pending_approval", lambda _state, _slot: False)
    provider = SimpleNamespace(has_active_turn=lambda: True)
    slot = SimpleNamespace(key="chat-95", task=_Task(), _task_started_at=100.0, _queue=[])
    # The provider is filed under the session key, the task under the slot key.
    assert chat_utils.effective_session_key(slot) == "dashboard:chat-95"
    orch.dashboard_state = _chat_state(provider, slot, queued=2)
    orch.sessions = orch.dashboard_state.sessions

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=0.0)

    # The census counts a chat turn at its provider and at its task, as before.
    assert census.turns == 2
    assert _named(census) == [
        {"kind": "turn", "name": "chat-95", "since_secs": None, "count": 2},
        {"kind": "queued", "name": "chat-95", "since_secs": None, "count": 2},
    ]
    assert _said(census) == "chat-95 turn, chat-95 queued"


def test_a_channel_born_slot_and_its_channel_turn_are_one_entry(orch, monkeypatch):
    monkeypatch.setattr(slot_retention, "has_pending_approval", lambda _state, _slot: False)
    provider = SimpleNamespace(has_active_turn=lambda: True)
    slot = SimpleNamespace(
        key="slack-1700", linked_session_key="slack:1700.5", task=None, _queue=[]
    )
    assert chat_utils.effective_session_key(slot) == "slack:1700.5"
    orch.dashboard_state = _chat_state(provider, slot)
    orch.sessions = orch.dashboard_state.sessions
    # The channel side runs the turn under the channel's own session key.
    orch._session_tasks = {"slack:1700.5": _Task()}

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=0.0)

    assert census.turns == 2
    assert _said(census) == "slack-1700 turn"


def test_a_channel_turn_with_no_slot_is_counted_unnamed(orch):
    provider = SimpleNamespace(has_active_turn=lambda: True)
    table = {"slack:1700.5": SimpleNamespace(provider=provider, queue=[])}
    orch.sessions = SimpleNamespace(
        active_providers=lambda: [provider],
        inbound_callback_count=0,
        _allocation_boundary=lambda: SimpleNamespace(_sessions=table),
    )
    orch.dashboard_state = SimpleNamespace(_slots={}, sessions=orch.sessions, workflow_service=None)
    orch._session_tasks = {"slack:1700.5": _Task()}

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=0.0)

    assert census.turns == 2
    # No list shows this caller a session with no slot, so it has no name here.
    assert _said(census) == "turn x2"


def test_work_of_different_kinds_stays_separate(orch):
    """A sub-agent is not its parent's turn, and a workflow is not the task runner."""
    orch._session_tasks = {"slack:1700.5": _Task()}
    orch.subagent_mgr = SimpleNamespace(pending_work_count=1)
    orch.task_runner = SimpleNamespace(running=True)
    orch.dashboard_state = SimpleNamespace(
        sessions=SimpleNamespace(active_providers=lambda: []),
        workflow_service=SimpleNamespace(
            list_runs=lambda: [{"run_id": "wf-12", "status": "running"}]
        ),
    )

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=0.0)

    assert (census.turns, census.background) == (1, 3)
    assert _said(census) == "workflow, turn, subagent, task runner"


def test_eight_holders_list_five_and_say_how_many_more(orch, monkeypatch):
    monkeypatch.setattr(slot_retention, "has_pending_approval", lambda _state, _slot: False)
    slots = {
        f"chat-{i}": SimpleNamespace(key=f"chat-{i}", task=_Task(), _queue=[]) for i in range(8)
    }
    orch.dashboard_state = SimpleNamespace(
        _slots=slots,
        sessions=SimpleNamespace(active_providers=lambda: []),
        workflow_service=None,
    )

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=0.0)
    entries, more = census.named_holders()

    assert census.turns == 8
    assert [h["name"] for h in entries] == [f"chat-{i}" for i in range(5)]
    assert more == 3
    assert _said(census).endswith("chat-4 turn, +3 more")


def test_holders_are_listed_longest_running_first_and_unnamed_work_is_one_count(orch, clock):
    _script_running(orch, clock, "b", "long-job")
    clock.now += 7100
    _script_running(orch, clock, "a", "short-job")
    clock.now += 100
    orch.subagent_mgr = SimpleNamespace(pending_work_count=3)

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=0.0)

    assert census.background == 5
    assert _said(census) == "long-job script (2 h), short-job script (1 min), subagent x3"


def test_no_entry_carries_message_text_a_title_or_a_path(orch, monkeypatch):
    monkeypatch.setattr(slot_retention, "has_pending_approval", lambda _state, _slot: True)
    provider = SimpleNamespace(has_active_turn=lambda: True)
    slot = SimpleNamespace(
        key="chat-95",
        task=_Task(),
        _task_started_at=None,
        _queue=["what is the launch date"],
        title="Quarterly numbers",
        workspace="/home/someone/private-project",
    )
    orch.dashboard_state = _chat_state(provider, slot)
    orch.sessions = orch.dashboard_state.sessions
    orch.dashboard_state.workflow_service = SimpleNamespace(
        list_runs=lambda: [{"run_id": "wf-12", "name": "Draft the board memo", "status": "running"}]
    )

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=0.0)
    said = json.dumps(_named(census)) + _said(census)

    assert {(h["kind"], h["name"]) for h in _named(census)} == {
        ("turn", "chat-95"),
        ("workflow", ""),
        ("queued", "chat-95"),
        ("approval", "chat-95"),
    }
    for private in ("launch date", "Quarterly", "private-project", "board memo", "wf-12"):
        assert private not in said


def test_a_source_that_cannot_be_read_is_named_by_its_kind_only(orch):
    def _boom():
        raise RuntimeError("secret detail")

    orch.subagent_mgr = SimpleNamespace()  # no pending_work_count
    orch.dashboard_state = SimpleNamespace(
        sessions=SimpleNamespace(active_providers=_boom), workflow_service=None
    )

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=0.0)

    assert census.background == 2
    assert _said(census) == "providers unreadable, subagents unreadable"


def test_the_counts_are_the_sum_of_the_holders(orch, clock):
    _script_running(orch, clock, "a", "one")
    orch._session_tasks = {"s": _Task(), "done": _Task(done=True)}
    orch.subagent_mgr = SimpleNamespace(pending_work_count=4)
    orch.task_runner = SimpleNamespace(running=True)

    held = orch._in_flight_work_census()

    assert quiescence.work_holders.counts(held) == orch._in_flight_work_counts() == (1, 6)


def test_an_orchestrator_with_no_named_census_still_answers_with_its_counts():
    stub = SimpleNamespace(
        _in_flight_work_counts=lambda: (2, 1), sessions=None, dashboard_state=None
    )

    census = quiescence.assess(stub, quiet_window_secs=0.0, now=0.0)

    assert (census.turns, census.background, census.busy) == (2, 1, 3)
    assert _named(census) == []
    assert _said(census) == ""


def test_describe_skips_what_is_not_an_entry():
    assert quiescence.work_holders.describe(None) == ""
    assert quiescence.work_holders.describe(
        [{"name": "x"}, "turn", {"kind": "turn", "name": "a\nb"}], 2
    ) == ("a b turn, +2 more")


_HOSTILE = "nightly\x1b]52;c;aGVsbG8=\x07 \x1b[2K\x1b[1A\x9b31m sync\nfake: restarted\r\u202egnp.exe\u2066\x00"


def _live_controls(text: str) -> list[str]:
    return [c for c in text if unicodedata.category(c) in ("Cc", "Cf", "Cs")]


@pytest.fixture
def at_a_terminal(monkeypatch):
    """The CLI's stdout is a person's terminal, the one place it prints names."""
    monkeypatch.setattr(idle_cli, "_stdout_is_terminal", lambda: True)


def _cli_line(entries: object, more: object = 0) -> str:
    """What the CLI says for a busy answer carrying *entries*."""
    waiting_on = quiescence.work_holders.describe(entries, more)
    return idle_cli._describe(idle_cli._Verdict("busy", 1, 1, waiting_on=waiting_on))


def test_a_hostile_job_name_is_harmless_in_the_answer_and_on_the_terminal(
    orch, clock, at_a_terminal
):
    _script_running(orch, clock, "job-7f3a", _HOSTILE)
    orch._session_tasks = {"slack:\x1b[31m1700\x07": _Task()}

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=clock.now)
    answered = json.loads(json.dumps(census.named_holders()))[0]
    line = _cli_line(answered)

    names = [h["name"] for h in answered]
    assert names == ["nightly 31m sync fake: restarted gnp.exe", ""]
    assert "nightly 31m sync fake: restarted gnp.exe script" in line
    for text in (line, *names):
        assert _live_controls(text) == []
        assert "52;c" not in text and "\n" not in text


def test_the_cli_cleans_an_answer_from_a_gateway_that_did_not(at_a_terminal):
    """The entries are another process's text: the sink cleans kind and name itself."""
    entries = [{"kind": "script\x1b[2J", "name": _HOSTILE, "since_secs": 21, "count": 1}]

    assert quiescence.work_holders.describe(entries, 0) == (
        "nightly 31m sync fake: restarted gnp.exe script (21 s)"
    )
    raw = idle_cli._Verdict("busy", 0, 1, waiting_on="x\x1b]0;t\x07y\nz")
    assert _live_controls(idle_cli._describe(raw)) == []


def test_a_credential_in_a_job_name_is_masked_in_the_answer_and_on_the_terminal(
    orch, clock, at_a_terminal
):
    token = synthetic_credentials.github_classic()
    secret = synthetic_credentials.aws_secret()
    _script_running(orch, clock, "a", f"sync TOKEN={token}")
    _script_running(orch, clock, "b", f"post https://example.com/hook?token={token}&x=1")
    _script_running(orch, clock, "c", f"aws_secret_access_key={secret}")

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=clock.now)
    answered = json.loads(json.dumps(census.named_holders()))[0]
    line = _cli_line(answered)

    assert sorted(h["name"] for h in answered) == [
        "aws_secret_access_key=[REDACTED: credential]",
        "post https://example.com/hook?token=[REDACTED: credential]&x=1",
        "sync TOKEN=[REDACTED: credential]",
    ]
    for text in (json.dumps(answered), line):
        assert token not in text and secret not in text


def test_the_cli_masks_a_credential_a_gateway_did_not(at_a_terminal):
    token = synthetic_credentials.github_classic()
    entries = [{"kind": "script", "name": f"sync TOKEN={token}", "count": 1}]

    line = _cli_line(entries)

    assert token not in line
    assert "sync TOKEN=[REDACTED: credential] script" in line


def test_a_credential_longer_than_the_name_cap_is_not_shown_in_part():
    token = synthetic_credentials.github_classic()

    name = quiescence.Holder("script", "x" * 50 + " TOKEN=" + token).name

    assert token[:8] not in name


def test_a_captured_cli_run_prints_the_counts_alone(monkeypatch):
    """Remote Crew relays this output to another machine's dashboard."""
    verdict = idle_cli._Verdict("busy", 0, 1, waiting_on="nightly-sync script (21 s)")
    counts = "0 chat turn(s) and 1 background task(s)"

    monkeypatch.setattr(idle_cli, "_stdout_is_terminal", lambda: False)
    assert idle_cli._describe(verdict) == counts
    monkeypatch.setattr(idle_cli, "_stdout_is_terminal", lambda: True)
    assert idle_cli._describe(verdict) == f"{counts} (nightly-sync script (21 s))"


def test_what_every_client_is_sent_carries_no_name(orch, clock):
    """``payload()`` and ``detail()`` reach every dashboard client and app: counts only."""
    _script_running(orch, clock, "job-7f3a", "zebra-cron-name")
    orch._session_tasks = {"slack:quokka.123": _Task()}

    census = quiescence.assess(orch, quiet_window_secs=0.0, now=clock.now)

    assert {h["name"] for h in _named(census)} == {"zebra-cron-name", ""}
    assert set(census.payload()) == {
        "turns",
        "background",
        "queued_messages",
        "pending_approvals",
        "busy",
        "quiet_remaining_secs",
        "settle_remaining_secs",
    }
    assert _refusal(census) == (
        "Restart postponed so running work is not interrupted "
        "(turns 1, background 1, queued messages 0, pending approvals 0). "
        "Try again when everything is idle."
    )


def test_a_long_name_is_cut_to_one_short_line():
    name = quiescence.Holder("script", "j" * 200).name

    assert len(name) == quiescence.work_holders.NAME_MAX_CHARS and name.endswith("...")
