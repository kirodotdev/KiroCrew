"""Conductor prompts steer every direct message to an existing worker.

A plain ``session_send`` to a busy target queues behind its running turn, so a
ruling or correction sent that way may arrive after the worker has spent a whole
turn without it. The initial dispatch seed is the only plain send because the
new session has no turn to steer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import agent, conductor_patrol, crewmate_prompts
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

GOAL_SKILL = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "SKILL.md"
PIPELINE_SKILL = Path(_BUILTIN_SKILLS_DIR) / "pipeline-conductor" / "SKILL.md"
WORKER_DEBATE = GOAL_SKILL.parent / "references" / "worker-debate.md"


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _flat_text(text: str) -> str:
    return " ".join(text.split())


def _section(body: str, heading: str, next_heading: str) -> str:
    start = body.index(heading)
    end = body.index(next_heading, start + len(heading))
    return body[start:end]


def test_shared_conductor_prompt_steers_every_direct_worker_message():
    rule = _flat_text(agent._LEDGER_CONDUCTOR_RULES["worker_communication"])
    assert "Every direct message to an existing worker is a steer" in rule
    assert "answers, rulings, corrections, nudges and follow-ups" in rule
    assert "The initial seed stays plain" in rule
    assert rule in _flat_text(agent._CONDUCTOR_SYSTEM_PROMPT)


def test_goal_conductor_steers_every_direct_worker_follow_up():
    section = _section(
        _flat(GOAL_SKILL), "### Goal changes mid-flight", "## When a conductor dispatched you"
    )
    assert "`session_send` the correction straight into it with `steer: true`" in section
    assert "**Steer every direct follow-up; never queue it.**" in section
    assert "Follow your prompt's direct-worker communication rule after the seed" in section
    assert "Leave `steer` off" not in section
    assert "If `session_read_message` reports a non-zero `queue_depth`" in section


def test_goal_conductor_steers_the_next_item_goal_to_a_reused_worker():
    section = _section(_flat(GOAL_SKILL), "### Patrol", "### Close the round")
    sentence = (
        "then `session_send` it the new item's goal with `steer: true`; its `work_brief` "
        "and `work_report` resolve to the new item."
    )
    assert section.count(sentence) == 1
    assert "`session_send` it the new item's goal;" not in section


def test_worker_report_guidance_steers_the_next_step_to_the_same_worker():
    guidance = _flat_text(crewmate_prompts.text("worker_report.guidance"))
    sentence = (
        "When a worker needs the next step of its own task, give it with session_send "
        "and steer: true, and end this turn;"
    )
    assert guidance.count(sentence) == 1
    assert "give it with session_send and end this turn" not in guidance


def test_pipeline_conductor_steers_every_direct_worker_message():
    prompt = _flat_text(agent._PIPELINE_CONDUCTOR_SYSTEM_PROMPT)
    assert "Every direct message to an existing worker is a steer" in prompt
    assert "pass `steer: true` on every `session_send`" in prompt
    section = _section(_flat(PIPELINE_SKILL), "## Live steering", "## Merge, cleanup, reconcile")
    assert (
        "Every direct message to an existing worker uses `session_send` with `steer: true`"
        in section
    )
    assert "including mode changes, rulings and nudges" in section
    assert "The initial seed stays plain" in section
    assert "Keep `steer` off" not in section
    assert "A steer does not withdraw messages already queued" in section


def test_worker_debate_steers_every_relay_and_direct_follow_up():
    body = _flat(WORKER_DEBATE)
    section = _section(body, "## A round", "## Checkpoint")
    assert (
        "Send every relay, ruling, changed role card, nudge and finishing instruction "
        "with `session_send` and `steer: true`." in section
    )
    assert "use `session_broadcast` with `mode: steer`" in section
    assert "The initial `work_dispatch` seed stays plain (`steer: false`)." in section
    ruling = _section(body, "## The human's ruling", "## Finishing")
    assert "`session_send` with `steer: true`" in ruling


def test_worker_debate_distinguishes_steered_started_and_queued_receipts():
    section = _section(_flat(WORKER_DEBATE), "## A round", "## Checkpoint")
    assert (
        "Check each delivery result in order: `steered: true` means it entered the "
        "running turn; otherwise `started: true` means a new turn started." in section
    )
    assert "Only `steered: false` AND `started: false` means queued." in section
    assert "Never resend a successful steer merely because `started` is false." in section
    assert "`started: false` means the message is queued" not in section
    assert "resend with `session_send` and `steer: true`" in section


def test_default_patrol_steers_next_steps_and_question_or_blocked_answers():
    message = conductor_patrol.PATROL_MESSAGE
    assert "send it the next step with session_send with steer: true." in message
    assert "Answer question and blocked items with session_send with steer: true." in message
    assert message.count("session_send") == message.count("session_send with steer: true") == 2


@pytest.mark.parametrize(
    "steered,started,expected,absent",
    [
        pytest.param(True, False, "Steered", "Queued", id="running-turn-steered"),
        pytest.param(False, True, "started a turn", "Queued", id="idle-turn-started"),
        pytest.param(False, False, "Queued", "Steered", id="steer-fell-back-to-queue"),
        pytest.param(True, True, "Steered", "started a turn", id="steered-takes-precedence"),
    ],
)
def test_debate_receipt_guidance_matches_session_send_tool(steered, started, expected, absent):
    client = InMemoryDashboardClient(
        {
            "POST /api/session-control/send": {
                "ok": True,
                "target": "chat-debate-worker",
                "steered": steered,
                "started": started,
            }
        }
    )
    result = TABLE.call(
        "session_send",
        {"target": "chat-debate-worker", "message": "read the next round", "steer": True},
        ToolContext(client, Caller.strict("dashboard:chat-debate-conductor")),
    )
    assert expected in result
    assert absent not in result
    (request,) = client.requests
    assert request.body["steer"] is True
    if not steered and not started:
        assert "Your steer could not go into the running turn" in result
