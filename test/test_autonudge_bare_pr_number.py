"""A gated loop naming its pull request as a bare ``PR <number>``.

Such a loop got no monitor at all when only a full URL could select a subject, so it
fired on its plain timer forever and no check state or comment body ever reached the
wake judge. The repository now comes from the loop's OWN session log -- a URL or
``owner/name#<number>`` with that number -- and when the log names none, or two,
nothing is guessed and the loop keeps its timer.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from kiro_crew import autonudge_judge as judge
from kiro_crew import history
from kiro_crew.autonudge_service import subject
from kiro_crew.probes import GH_PR

_URL = "https://github.com/kirodotdev/KiroCrew/pull/14361"
_MESSAGE = "Babysit PR 14361 until it is green and approved"


@pytest.fixture
def fake_log(monkeypatch):
    """The guarded read ``subject`` makes on the conversation log, recorded."""
    state = SimpleNamespace(
        rows=[{"role": "user", "content": f"please drive {_URL} to green"}],
        reads=[],
        threads=[],
        withhold=False,
    )

    def _derive(self, key):
        state.reads.append(key)
        state.threads.append(threading.current_thread())
        if state.withhold:
            raise history.TranscriptWithheld("restricted")
        return list(state.rows)

    monkeypatch.setattr(history.ConversationLog, "derive_messages", _derive)
    return state


def _arm(message=_MESSAGE, slot_key="chat-7-1"):
    """What the arming surfaces do: read the log first, then infer from it."""
    texts = subject.read_session_texts(message, slot_key)
    return subject.infer_monitor(message, 100.0, slot_key=slot_key, session_texts=texts)


def _stored(monitor, message=_MESSAGE, slot_key="chat-7-1"):
    return SimpleNamespace(message=message, judge={}, monitor=monitor, slot_key=slot_key)


def test_a_bare_number_loop_is_armed_with_a_monitor_from_its_session_log(fake_log):
    monitor = _arm(slot_key="chat-7-1700000000")
    assert monitor is not None
    assert monitor.kind == GH_PR
    assert monitor.target == "kirodotdev/KiroCrew#14361"
    # A dashboard loop binds on the BARE slot key; its transcript is ``dashboard:``.
    assert fake_log.reads == ["dashboard:chat-7-1700000000"]


def test_a_channel_loop_reads_its_own_session_key(fake_log):
    key = "slack:1700000000.000100"
    texts = subject.read_session_texts(_MESSAGE, key)
    assert subject.infer_subject(_MESSAGE, slot_key=key, session_texts=texts) is not None
    assert fake_log.reads == ["slack:1700000000.000100"]


def test_no_matching_repository_in_the_log_leaves_the_loop_ungated(fake_log):
    fake_log.rows = [{"role": "user", "content": "https://github.com/o/r/pull/99"}]
    assert _arm() is None


def test_two_repositories_in_the_log_leave_the_loop_ungated(fake_log):
    fake_log.rows.append({"role": "tool", "content": "other/thing#14361 is unrelated"})
    assert _arm() is None


def test_a_withheld_transcript_leaves_the_loop_ungated(fake_log):
    fake_log.withhold = True
    assert _arm() is None
    assert fake_log.reads == ["dashboard:chat-7-1"]


def test_a_loop_with_no_slot_never_reads_a_log(fake_log):
    assert subject.read_session_texts(_MESSAGE, "") is None
    assert subject.infer_monitor(_MESSAGE, 100.0) is None
    assert fake_log.reads == []


def test_inference_itself_never_reads_a_transcript(fake_log):
    # Inference runs on the gateway's event loop, so the whole-transcript read lives
    # only in ``read_session_texts``; without its result a bare number resolves nothing.
    assert subject.infer_monitor(_MESSAGE, 100.0, slot_key="chat-7-1") is None
    assert subject.infer_subject(_MESSAGE, slot_key="chat-7-1") is None
    assert fake_log.reads == []


def test_a_work_ledger_watch_never_reads_a_log(fake_log):
    assert subject.read_session_texts(_MESSAGE, "chat-7-1", "work-ledger") is None
    assert fake_log.reads == []


@pytest.mark.asyncio
async def test_the_service_arm_reads_the_log_off_the_event_loop(fake_log, tmp_path):
    from kiro_crew.autonudge import AutoNudgeService

    svc = AutoNudgeService(base_dir=tmp_path)
    loop = await svc.add(slot_key="chat-7-1", message=_MESSAGE, idle_secs=60, gate=True)
    assert loop.monitor is not None and loop.monitor.target == "kirodotdev/KiroCrew#14361"
    assert fake_log.reads == ["dashboard:chat-7-1"]
    assert fake_log.threads and threading.main_thread() not in fake_log.threads


@pytest.mark.asyncio
async def test_the_service_retarget_reads_the_log_off_the_event_loop(fake_log, tmp_path):
    from kiro_crew.autonudge import AutoNudgeService

    svc = AutoNudgeService(base_dir=tmp_path)
    loop = await svc.add(slot_key="chat-7-1", message="Babysit PR 1", idle_secs=60, gate=True)
    assert loop.monitor is None
    fake_log.reads.clear()
    fake_log.threads.clear()
    updated = await svc.update(loop.id, message=_MESSAGE)
    assert updated is not None
    assert updated.monitor is not None and updated.monitor.target == "kirodotdev/KiroCrew#14361"
    assert fake_log.reads == ["dashboard:chat-7-1"]
    assert threading.main_thread() not in fake_log.threads


def test_a_url_loop_never_reads_its_log(fake_log):
    monitor = _arm(f"Babysit {_URL}")
    assert monitor is not None and monitor.target == "kirodotdev/KiroCrew#14361"
    assert fake_log.reads == []


def test_a_stored_loop_resolves_from_its_monitor_without_reading_the_log(fake_log):
    # The gate fires instead of observing whenever ``loop_subject`` and the stored
    # monitor disagree, so the tick must give the arm's answer -- and it must not
    # read a transcript on the event loop every tick to do it.
    monitor = _arm()
    armed = subject.infer_subject(
        _MESSAGE,
        slot_key="chat-7-1",
        session_texts=subject.read_session_texts(_MESSAGE, "chat-7-1"),
    )
    fake_log.reads.clear()
    # The log changing after the arm cannot move a stored binding.
    fake_log.rows.append({"role": "user", "content": "other/thing#14361"})
    target = subject.loop_subject(_stored(monitor))
    assert target is not None
    assert (target.kind, target.subject) == (monitor.kind, monitor.target)
    assert target == armed
    assert fake_log.reads == []


def test_a_stored_monitor_for_another_number_resolves_nothing(fake_log):
    monitor = _arm()
    fake_log.reads.clear()
    assert subject.loop_subject(_stored(monitor, message="Babysit PR 555")) is None
    assert fake_log.reads == []


def test_the_judge_collects_the_monitor_subject_for_a_bare_number_loop(fake_log):
    monitor = _arm()
    watched = judge.watched_pr_subject(_stored(monitor))
    assert watched == "kirodotdev/KiroCrew#14361"
    # Without ``watched`` the collector has no target and the judge tick fires.
    assert judge.parse_targets({}, _MESSAGE) == []
    assert judge.parse_targets({}, _MESSAGE, watched=watched) == [watched]
    # The reading the collector gets back is about the watched subject.
    assert judge.pr_observation_is_about(
        watched, monitor_kind=monitor.kind, monitor_target=monitor.target
    )


def test_the_judge_never_adds_a_monitor_the_instruction_did_not_name():
    watched = "kirodotdev/KiroCrew#14361"
    assert judge.parse_targets({}, "Babysit PR 7", watched=watched) == []
    assert judge.parse_targets({}, "tidy the docs", watched=watched) == []
    # A brief that NARROWED the watch keeps its own list.
    assert judge.parse_targets({"targets": []}, _MESSAGE, watched=watched) == []
    # A URL instruction is collected as before, once.
    assert judge.parse_targets({}, f"Babysit {_URL}", watched=watched) == [f"Babysit {_URL}"]


def test_watched_pr_subject_ignores_a_non_pull_request_monitor():
    loop = SimpleNamespace(monitor=SimpleNamespace(kind="work-ledger", target="chat-1-2"))
    assert judge.watched_pr_subject(loop) == ""
    assert judge.watched_pr_subject(SimpleNamespace(monitor=None)) == ""


def test_the_real_conversation_log_is_read_under_the_dashboard_key(tmp_path, monkeypatch):
    # The key mapping against the real store, not the fake: a wrong prefix reads an
    # empty log and silently leaves every dashboard loop ungated.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    history.ConversationLog().append("dashboard:chat-9-1700000001", "user", f"drive {_URL}")
    monitor = _arm(slot_key="chat-9-1700000001")
    assert monitor is not None and monitor.target == "kirodotdev/KiroCrew#14361"


@pytest.fixture
def tool_session(monkeypatch):
    """A session the monitor tools accept, with no retained stop on its binding."""
    from kiro_crew import mcp_core

    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
    monkeypatch.setattr(mcp_core, "_autonudge_binding_key", lambda sk: "chat-7-1")
    monkeypatch.setattr(mcp_core, "_structured_monitor_binding_key", lambda sk: "chat-7-1")
    monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: {"enabled": True, "monitor": None})
    return "dashboard:chat-7-1"


def test_the_tool_run_reads_the_log_to_shape_its_ack(fake_log, tool_session, monkeypatch):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import control

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda *a, **k: tool_session)
    out = control.monitor_start("monitor_start", {"message": _MESSAGE, "interval_secs": 317})
    assert fake_log.reads, out
    assert "kirodotdev/KiroCrew#14361" in out or "14361" in out, out


def test_the_gateway_replay_never_reads_the_log_on_its_event_loop(fake_log, tool_session):
    from kiro_crew import mcp_core

    derived = mcp_core.derive_directive(
        "monitor_start", {"message": _MESSAGE, "interval_secs": 317}, tool_session
    )
    assert derived is not None and derived[0] == "monitor_start"
    assert derived[1]["message"] == _MESSAGE
    assert fake_log.reads == []
