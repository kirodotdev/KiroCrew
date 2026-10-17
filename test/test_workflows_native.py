"""GATES D1–D5 — KiroCrew-native ctx primitives wired through the runner.

D1 ctx.cron, D2 ctx.nudge, D3 ctx.memory/ctx.learn, D4 ctx.approve,
D5 ctx.send_slack/ctx.send_message. Each delegates to an injected port (the real
CronSDK/AutoNudgeService/MemoryStore/LessonStore/approval/messaging wiring is the
gateway's job; here we assert the ctx surface calls the port with the right args).
A primitive with no port wired raises a clear RuntimeError (not NotImplementedError).

All against stubs — no real service, no network, no kiro-cli.
See ``docs/system-specs/modules/workflows.md`` and GATES.md (D1–D5).
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.runner import WorkflowRunner

pytestmark = pytest.mark.asyncio

NOW = "2026-06-18T00:00:00Z"


async def _echo(prompt: str, opts: dict):
    return f"echo:{prompt}"


def _runner(ports: dict) -> WorkflowRunner:
    return WorkflowRunner(agent_fn=_echo, ports=ports, audit=lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# D1 — ctx.cron
# --------------------------------------------------------------------------- #


async def test_d1_cron_port_invoked() -> None:
    class CronStub:
        def __init__(self):
            self.calls = []

        def ensure(self, name, *, cron_expr, workflow, **kw):
            self.calls.append((name, cron_expr, workflow))

    cron = CronStub()
    script = (
        'META = {"name": "c"}\n'
        "async def workflow(ctx):\n"
        "    ctx.cron.ensure('daily', cron_expr='0 9 * * *', workflow='ticket-triage')\n"
        "    return 'ok'\n"
    )
    res = await _runner({"cron": cron}).run(script, run_id="wf_cron", now=NOW)
    assert res.ok, res.error
    assert cron.calls == [("daily", "0 9 * * *", "ticket-triage")]


# --------------------------------------------------------------------------- #
# D2 — ctx.nudge
# --------------------------------------------------------------------------- #


async def test_d2_nudge_port_invoked() -> None:
    seen = {}

    def nudge_fn(*, session_key, idle_secs, message, max_cycles, notify=None):
        seen.update(
            session_key=session_key,
            idle_secs=idle_secs,
            message=message,
            max_cycles=max_cycles,
            has_notify=notify is not None,
        )

    script = (
        'META = {"name": "n"}\n'
        "async def workflow(ctx):\n"
        "    ctx.nudge(idle_secs=120, message='still there?')\n"
        "    return 'ok'\n"
    )
    res = await _runner({"nudge": nudge_fn}).run(
        script, run_id="wf_n", now=NOW, session_key="dashboard:chat-1-9"
    )
    assert res.ok, res.error
    # The run's originating session_key is threaded through to the port, along
    # with a notify emitter so outcomes surface in the run event stream.
    assert seen == {
        "session_key": "dashboard:chat-1-9",
        "idle_secs": 120,
        "message": "still there?",
        "max_cycles": 0,
        "has_notify": True,
    }


# --------------------------------------------------------------------------- #
# D3 — ctx.memory / ctx.learn (cross-run state)
# --------------------------------------------------------------------------- #


async def test_d3_memory_get_set_and_learn() -> None:
    class MemStub:
        def __init__(self):
            self.store = {"seen": [1, 2]}

        def get(self, key, default=None):
            return self.store.get(key, default)

        def set(self, key, value):
            self.store[key] = value

    class LearnStub:
        def __init__(self):
            self.added = []

        def add(self, rule, scope="workspace"):
            self.added.append((rule, scope))

    mem, learn = MemStub(), LearnStub()
    script = (
        'META = {"name": "m"}\n'
        "async def workflow(ctx):\n"
        "    prev = ctx.memory.get('seen', default=[])\n"
        "    ctx.memory.set('seen', prev + [3])\n"
        "    ctx.learn.add('downweight noisy finder X')\n"
        "    return prev\n"
    )
    res = await _runner({"memory": mem, "learn": learn}).run(script, run_id="wf_m", now=NOW)
    assert res.ok, res.error
    assert res.result == [1, 2]
    assert mem.store["seen"] == [1, 2, 3]  # persisted across the run
    assert learn.added == [("downweight noisy finder X", "workspace")]


# --------------------------------------------------------------------------- #
# D4 — ctx.approve (human-in-the-loop gate)
# --------------------------------------------------------------------------- #


async def test_d4_approve_resolves_decision() -> None:
    async def approve_fn(prompt: str) -> bool:
        return "ship" in prompt

    script = (
        'META = {"name": "a"}\n'
        "async def workflow(ctx):\n"
        "    yes = await ctx.approve('ship it?')\n"
        "    no = await ctx.approve('delete everything?')\n"
        "    return [yes, no]\n"
    )
    res = await _runner({"approve": approve_fn}).run(script, run_id="wf_a", now=NOW)
    assert res.ok, res.error
    assert res.result == [True, False]


# --------------------------------------------------------------------------- #
# D5 — ctx.send_slack / ctx.send_message
# --------------------------------------------------------------------------- #


async def test_d5_send_slack_and_message() -> None:
    sent = []

    async def slack_fn(target, text):
        sent.append(("slack", target, text))

    async def msg_fn(channel, text):
        sent.append(("msg", channel, text))

    script = (
        'META = {"name": "s"}\n'
        "async def workflow(ctx):\n"
        "    await ctx.send_slack(ctx.owner_dm, 'digest')\n"
        "    await ctx.send_message('C123', 'hi')\n"
        "    return 'ok'\n"
    )
    res = await _runner({"send_slack": slack_fn, "send_message": msg_fn}).run(
        script, run_id="wf_s", now=NOW, owner_dm="U_OWNER"
    )
    assert res.ok, res.error
    assert sent == [("slack", "U_OWNER", "digest"), ("msg", "C123", "hi")]


# --------------------------------------------------------------------------- #
# Unwired ports → clear RuntimeError (run_failed, not a crash)
# --------------------------------------------------------------------------- #


async def test_unwired_primitive_fails_cleanly() -> None:
    script = (
        'META = {"name": "x"}\n'
        "async def workflow(ctx):\n"
        "    await ctx.approve('go?')\n"
        "    return 1\n"
    )
    res = await _runner({}).run(script, run_id="wf_x", now=NOW)
    assert res.ok is False
    assert res.events[-1].type == "run_failed"
    assert "approve" in res.error
