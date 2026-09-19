"""One sampled turn, all the way across the seam: decide, show, judge.

Two halves of this feature were built against a contract rather than against each
other: the point publishes an outcome under the session key it was handed, and the
message finalizer claims one under the session key its slot resolves to. Each side
is covered on its own -- ``test_decisions_points.py`` and
``test_decisions_integration.py`` for the decision,
``test_decisions_strip_rides_message.py`` and ``test_decisions_feedback_route.py``
for the ride-along and the verdict. Neither can see the JOIN, and the join is the
one thing a contract cannot prove: a key that does not match yields no strip and
no error, so the whole feature would go quiet without a single test turning red.

So this drives the real chain once, with nothing stubbed between the links: a real
``ContextBuilder.build_message`` on an executor thread (the only way production
reaches it) publishes through the real registry; the real ``_flush_segment``
claims it onto a real slot's assistant row; the real feedback handler files a
verdict against the ``turn_id`` that row carries. Only the provider is a stand-in,
because the one thing this must not do is send anything.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_decisions_strip_rides_message import _slot, _state

from kiro_crew import decisions as core
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers.decisions import api_decisions_feedback
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions import outcomes
from kiro_crew.decisions.points import skills_select as sel
from kiro_crew.decisions.types import Answer

BASELINE = "matcher"
WIDENED = "unrelated"


@pytest.fixture(autouse=True)
def clean_registry():
    outcomes.reset()
    yield
    outcomes.reset()


@pytest.fixture(autouse=True)
def _generous_append_deadline(monkeypatch):
    """Take the production append deadline off the critical path of these tests.

    Assertions below read rows back off the day-file, so they depend on the real
    writer beating ``platform_log_append._APPEND_TIMEOUT_SECONDS`` -- 0.5s, there to
    stop an observation occupying a caller on lock contention. Worth keeping in
    production and worth not racing in a parallel suite, where losing it surfaces as
    an unexplained assertion about the READER.

    Raised, not removed: a genuinely stuck lock still fails the test.
    """
    from kiro_crew import platform_log_append

    monkeypatch.setattr(platform_log_append, "_APPEND_TIMEOUT_SECONDS", 10.0)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """One decision log for the whole chain: the outcome row and the verdict row.

    Created here, as every sibling test of this log does. The append helper pins
    the directory it writes into, and whether it will CREATE an absent one is a
    platform question -- so a test that leaves it absent is testing the platform,
    and on Windows this one was: the verdict append refused and the route honestly
    answered 503 where the test expected 200.
    """
    directory = tmp_path / "decisions"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(log_mod, "log_dir", lambda: directory)
    return directory


@pytest.fixture
def sampled(monkeypatch):
    """The point on, with a budget a test outlives and a menu that stays one round."""
    monkeypatch.setattr(core, "is_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(core, "timeout_secs", lambda *args, **kwargs: 0.0)
    monkeypatch.setattr(core, "history_budget_chars", lambda *args, **kwargs: 2000)
    monkeypatch.setattr(sel, "WAIT_MARGIN_SECS", 0.0)
    monkeypatch.setattr(sel, "MIN_WAIT_SECS", 0.5)
    monkeypatch.setattr(
        core,
        "decide",
        AsyncMock(return_value={"pick": Answer(id="pick", value=WIDENED, p=0.91)}),
    )


def _write_skill(root, name, *, triggers, description="d"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\ntriggers: {triggers}\n---\n"
        f"body of {name}\n",
        encoding="utf-8",
    )


def _builder(tmp_path):
    """A real ContextBuilder. MUST be constructed on the loop, as production does."""
    from kiro_crew.context import ContextBuilder
    from kiro_crew.learn import LessonStore
    from kiro_crew.memory import MemoryStore
    from kiro_crew.skills import SkillsLoader

    root = tmp_path / "skills"
    _write_skill(root, BASELINE, triggers="zebra, giraffe", description="animal work")
    _write_skill(root, WIDENED, triggers="quarterly invoice", description="billing")
    loader = SkillsLoader(skills_path=root, install_builtins=False)
    loader._max_triggered = 3
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=loader,
        lessons=LessonStore(base_dir=tmp_path),
    )


def _owner_request(body):
    """A request shaped like a real dashboard OWNER call to the verdict route."""
    request = MagicMock()
    request.path = "/api/decisions/feedback"
    store = {"app": "", "user": "owner-1"}
    request.get = lambda key, default=None: store.get(key, default)
    request.__contains__ = lambda _self, key: key in store
    request.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = "owner-1"
    request.app = {"state": state}
    request.query = {}
    request.json = AsyncMock(return_value=body)
    return request


def _rows(home):
    path = log_mod.log_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.mark.asyncio
async def test_a_sampled_turn_shows_its_decision_and_takes_a_verdict(
    tmp_path, home, sampled, monkeypatch
):
    """decide -> publish -> the reply carries it -> a verdict is filed against it."""
    import kiro_crew.dashboard.handlers as handlers_pkg

    monkeypatch.setattr(handlers_pkg, "sel", lambda: MagicMock())
    builder = _builder(tmp_path)
    state, slot = _state(tmp_path), _slot("chat-strip-e2e")

    # The JOIN under test: the point is handed the key the slot resolves to, which
    # is the key the finalizer will claim under. A different string here is exactly
    # the silent failure this test exists for.
    session_key = effective_session_key(slot)

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        message, _hook = await loop.run_in_executor(
            pool, lambda: builder.build_message("zebra please", False, session_key)
        )

    # The decision happened and it was Jev's, not word overlap's.
    assert f"[Skill: {WIDENED}]" in message
    assert f"[Skill: {BASELINE}]" not in message
    assert outcomes.pending_count() == 1, "the outcome is waiting for the reply it influenced"

    chat_runner._flush_segment(state, slot, "here is the answer")

    rows = [row for row in slot.messages if row.get("role") == "assistant"]
    assert len(rows) == 1
    strip = (rows[0].get("meta") or {}).get("decisions_strip")
    assert strip is not None, "the reply must carry the decision that shaped it"
    assert strip["baseline"] == [BASELINE], "what word overlap would have injected"
    assert strip["jev"] == [WIDENED], "what was injected"
    assert strip["agree"] is False
    assert strip["p"] == 0.91
    assert outcomes.pending_count() == 0, "claimed once, so the next reply carries nothing"

    # The strip's turn_id is the one the log already wrote, which is what makes a
    # verdict about the row and not about a number only the message knows.
    logged = [row for row in _rows(home) if row.get("baseline") is not None]
    assert len(logged) == 1
    assert logged[0]["turn_id"] == strip["turn_id"]

    response = await api_decisions_feedback(
        _owner_request({"turn_id": strip["turn_id"], "verdict": "wrong", "side": "baseline"})
    )

    assert response.status == 200
    verdicts = [row for row in _rows(home) if row.get("kind") == "feedback"]
    assert len(verdicts) == 1
    assert verdicts[0]["turn_id"] == strip["turn_id"]
    assert (verdicts[0]["verdict"], verdicts[0]["side"]) == ("wrong", "baseline")
    # Appended, never rewritten: the decision row this judges is still there.
    assert [row for row in _rows(home) if row.get("baseline") is not None] == logged
