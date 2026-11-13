"""``kirocrew.session.started`` carries ``session_kind`` and ``trigger_source``.

``session_kind`` is a rollup of ``session_source`` and so adds no series.
``trigger_source`` says who caused the session to open: the key decides it for a
cron, sub-agent, workflow, heartbeat or background session; otherwise the caller
or the running turn states it, and with neither it is ``other``, never ``user``.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from unittest.mock import patch

import pytest

from kiro_crew.metrics import sessions as sess
from kiro_crew.metrics import usage_attrs as ua

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "kiro_crew"


class _Rec:
    enabled = False  # no crumb files

    def __init__(self) -> None:
        self.counters: list[dict] = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "attrs": dict(attrs or {})})

    def histogram(self, *a, **k) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean():
    yield
    with sess._live_lock:
        sess._live_starts.clear()
        sess._start_triggers.clear()


def _start(key, *, trigger=None, running=None):
    rec = _Rec()

    async def _go():
        if running is not None:
            ua.set_current_trigger(running)
        await sess.record_session_started(key, None, trigger=trigger)

    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        asyncio.run(_go())
    (c,) = [c for c in rec.counters if c["name"] == sess.SESSION_STARTED_COUNTER]
    return c["attrs"]


def test_existing_attributes_stay():
    attrs = _start("chat-1-2")
    assert attrs["session_source"] == "dashboard"
    assert attrs["harness"] == "unknown"


@pytest.mark.parametrize(
    "key,kind,trigger",
    [
        ("chat-1-2", "chat", "other"),
        ("cli_chat", "chat", "other"),
        ("telegram:42", "channel", "other"),
        ("cron:job1", "cron", "cron"),
        ("subagent:abc", "subagent", "subagent"),
        ("taskrunner:t1", "task_runner", "workflow"),
        ("wf:run:1", "workflow_step", "workflow"),
        ("_hb", "heartbeat", "heartbeat"),
        ("_consolidate", "background", "system"),
        ("weird-key", "other", "other"),
    ],
)
def test_kind_and_default_trigger(key, kind, trigger):
    attrs = _start(key)
    assert attrs["session_kind"] == kind
    assert attrs["trigger_source"] == trigger


def test_running_turn_trigger_names_a_chat_session_opener():
    assert _start("chat-1-2", running="user")["trigger_source"] == "user"
    assert _start("chat-1-3", running="agent")["trigger_source"] == "agent"


def test_key_fixed_kind_wins_over_the_running_turn():
    assert _start("subagent:x", running="user")["trigger_source"] == "subagent"


def test_explicit_trigger_wins_over_the_running_turn():
    assert _start("chat-1-2", trigger="agent", running="user")["trigger_source"] == "agent"


def test_an_open_trigger_value_is_closed():
    assert _start("chat-1-2", trigger="person")["trigger_source"] == "other"


def test_a_recycled_session_keeps_its_original_opener():
    _start("chat-9-9", running="user")
    assert sess.started_trigger_of("chat-9-9") == "user"
    # The compaction recycle re-records with the stored trigger, from a context
    # whose own running trigger is different.
    attrs = _start("chat-9-9", trigger=sess.started_trigger_of("chat-9-9"), running="monitor")
    assert attrs["trigger_source"] == "user"


def test_values_are_closed():
    for key in ("chat-1-2", "cron:a", "slack:T:C:1.2", "x"):
        attrs = _start(key, running="user")
        assert attrs["session_kind"] in ua.SESSION_KIND_VALUES
        assert attrs["trigger_source"] in ua.TRIGGER_SOURCE_VALUES


def test_the_compaction_recycle_passes_the_stored_trigger():
    tree = ast.parse((SRC / "session_compaction.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "record_session_started"
    ]
    assert len(calls) == 1
    (kw,) = [k for k in calls[0].keywords if k.arg == "trigger"]
    assert isinstance(kw.value, ast.Call) and kw.value.func.id == "started_trigger_of"
