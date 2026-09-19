"""``skills.select`` end to end: a real ContextBuilder, from a real executor.

``test_decisions_points.py`` pins the point in isolation. This file is the only
place the REAL production shape runs: a ``ContextBuilder`` constructed on the
event loop, ``build_message`` called on an executor thread (the only way
production reaches it), a real ``SkillsLoader`` over a real skill tree, and the
answer landing — or not landing — in the assembled message.

What it exists to catch
-----------------------
* An answer that is CONSUMED. The point returns a list and ``build_message`` must
  use it above ``split_triggered``, so a pick reaches the prompt through the same
  body/pointer path as a trigger match. A test that awaits the point directly
  cannot see that.
* The three readings of a return value: a list REPLACES the baseline, ``[]`` is a
  real "no skill applies" that empties it, and ``None`` keeps it.
* The refusals that must preserve the baseline exactly: off, cap 0, a custom
  agent, a minimal context, an answer outside the menu, a raising transport, an
  expired budget, and a builder with no loop to submit to.
* That nothing is left running. The budget expiry cancels its future and the loop
  is left with no task of ours.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time

import pytest

from kiro_crew import decisions as core
from kiro_crew.decisions.points import skills_select as sel
from kiro_crew.decisions.types import Answer

BASELINE = "matcher"
WIDENED = "unrelated"


def _answers(value):
    return {"pick": Answer(id="pick", value=value, p=0.9, confidence=None)}


def _write_skill(root, name, *, triggers, description="d"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\ntriggers: {triggers}\n---\n"
        f"body of {name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def enabled(monkeypatch):
    """The point on, with a budget small enough for a test to outlive."""
    monkeypatch.setattr(core, "is_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(core, "timeout_secs", lambda *args, **kwargs: 0.0)
    monkeypatch.setattr(sel, "WAIT_MARGIN_SECS", 0.0)
    monkeypatch.setattr(sel, "MIN_WAIT_SECS", 0.5)


@pytest.fixture
def tree(tmp_path):
    """A skill tree where the baseline wins one skill and one is only offered."""
    root = tmp_path / "skills"
    _write_skill(root, BASELINE, triggers="zebra, giraffe", description="animal work")
    _write_skill(root, WIDENED, triggers="quarterly invoice", description="billing")
    return root


def _builder(tmp_path, skills_root, *, cap=3):
    """A real ContextBuilder. MUST be called on the loop, as production does."""
    from kiro_crew.context import ContextBuilder
    from kiro_crew.learn import LessonStore
    from kiro_crew.memory import MemoryStore
    from kiro_crew.skills import SkillsLoader

    loader = SkillsLoader(skills_path=skills_root, install_builtins=False)
    loader._max_triggered = cap
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=loader,
        lessons=LessonStore(base_dir=tmp_path),
    )


async def _message(builder, text="zebra please", **kwargs):
    """``build_message`` where production runs it: an executor thread."""
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        message, _hook = await loop.run_in_executor(
            pool, lambda: builder.build_message(text, False, "s-exec", **kwargs)
        )
    return message


def _bodies(message):
    """Which skill bodies the assembled message actually carries."""
    return [name for name in (BASELINE, WIDENED) if f"[Skill: {name}]" in message]


class _Recorder:
    """A ``decide`` stand-in that answers *value* and keeps what it was asked."""

    def __init__(self, value):
        self.value = value
        self.calls: list[tuple] = []

    async def __call__(self, point, state, questions, **kwargs):
        self.calls.append((point, state, questions, kwargs))
        return _answers(self.value) if self.value is not None else None

    @property
    def keys(self):
        return list(self.calls[0][2][0].options)


@pytest.mark.asyncio
async def test_a_pick_replaces_the_trigger_matched_selection(tmp_path, tree, enabled, monkeypatch):
    """The whole feature: an offered skill the baseline would never have picked."""
    decide = _Recorder(WIDENED)
    monkeypatch.setattr(core, "decide", decide)
    builder = _builder(tmp_path, tree)

    message = await _message(builder)

    assert _bodies(message) == [WIDENED]
    assert f"body of {WIDENED}" in message
    assert decide.calls[0][0] == "skills.select"
    assert decide.keys == [BASELINE, WIDENED, sel.NONE_OPTION], (
        "the menu is every eligible skill, not the baseline's winners: "
        f"{WIDENED} scores nothing on this message"
    )


@pytest.mark.asyncio
async def test_an_empty_answer_injects_no_skill_at_all(tmp_path, tree, enabled, monkeypatch):
    """``[]`` is a real answer, not a refusal: the baseline's skill is dropped."""
    monkeypatch.setattr(core, "decide", _Recorder(sel.NONE_OPTION))
    builder = _builder(tmp_path, tree)

    assert _bodies(await _message(builder)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "injected"),
    [(WIDENED, [WIDENED]), (sel.NONE_OPTION, []), ("no-such-skill", [BASELINE])],
    ids=["replaced", "emptied", "kept"],
)
async def test_the_audit_row_records_the_selection_that_was_injected(
    tmp_path, tree, enabled, monkeypatch, value, injected
):
    """ONE ``skill_trigger`` row, naming what the prompt carries -- never the
    superseded lexical match, and written even when the pick emptied it."""
    from unittest.mock import MagicMock

    fake_sel = MagicMock()
    monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)
    monkeypatch.setattr(core, "decide", _Recorder(value))
    builder = _builder(tmp_path, tree)

    assert _bodies(await _message(builder)) == injected

    assert fake_sel.log_tool_invocation.call_count == 1
    kwargs = fake_sel.log_tool_invocation.call_args.kwargs
    assert kwargs["tool_name"] == "skill_trigger"
    assert kwargs["metadata"]["skills"] == ",".join(injected)
    assert kwargs["metadata"]["bodies"] == ",".join(injected)
    assert ("selected" in kwargs["metadata"]) is (injected != [BASELINE])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [None, "no-such-skill", BASELINE[:3]],
    ids=["no-answers", "unoffered-key", "prefix-of-a-key"],
)
async def test_an_unusable_answer_keeps_the_baseline(tmp_path, tree, enabled, monkeypatch, value):
    monkeypatch.setattr(core, "decide", _Recorder(value))
    builder = _builder(tmp_path, tree)

    assert _bodies(await _message(builder)) == [BASELINE]


@pytest.mark.asyncio
async def test_a_raising_transport_keeps_the_baseline(tmp_path, tree, enabled, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("transport died")

    monkeypatch.setattr(core, "decide", _boom)
    builder = _builder(tmp_path, tree)

    assert _bodies(await _message(builder)) == [BASELINE]


@pytest.mark.asyncio
async def test_a_disabled_point_keeps_the_baseline_and_asks_nothing(tmp_path, tree, monkeypatch):
    decide = _Recorder(WIDENED)
    monkeypatch.setattr(core, "decide", decide)
    monkeypatch.setattr(core, "is_enabled", lambda *args, **kwargs: False)
    builder = _builder(tmp_path, tree)

    assert _bodies(await _message(builder)) == [BASELINE]
    assert decide.calls == []


@pytest.mark.asyncio
async def test_a_cap_of_zero_injects_nothing_and_asks_nothing(tmp_path, tree, enabled, monkeypatch):
    """The shipped default: ``skills.max_triggered`` is 0, so nothing is selected."""
    decide = _Recorder(WIDENED)
    monkeypatch.setattr(core, "decide", decide)
    builder = _builder(tmp_path, tree, cap=0)

    assert _bodies(await _message(builder)) == []
    assert decide.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs", [{"agent": "custom-agent"}, {"minimal_context": True}], ids=["custom", "minimal"]
)
async def test_a_custom_agent_and_a_minimal_context_never_reach_the_point(
    tmp_path, tree, enabled, monkeypatch, kwargs
):
    """Both already skip skill injection entirely; the point sits inside that."""
    decide = _Recorder(WIDENED)
    monkeypatch.setattr(core, "decide", decide)
    builder = _builder(tmp_path, tree)

    assert _bodies(await _message(builder, **kwargs)) == []
    assert decide.calls == []


@pytest.mark.asyncio
async def test_a_negative_trigger_keeps_a_skill_off_the_menu(tmp_path, enabled, monkeypatch):
    root = tmp_path / "skills"
    _write_skill(root, BASELINE, triggers="zebra")
    _write_skill(root, "vetoed", triggers="zebra, !giraffe")
    decide = _Recorder("vetoed")
    monkeypatch.setattr(core, "decide", decide)
    builder = _builder(tmp_path, root)

    message = await _message(builder, text="zebra and giraffe please")

    assert decide.keys == [BASELINE, sel.NONE_OPTION]
    assert "[Skill: vetoed]" not in message, (
        "a negatively triggered skill is not offered, so naming it is an "
        "unoffered key and the baseline stands"
    )
    assert f"[Skill: {BASELINE}]" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted", [False, True], ids=["untrusted", "trusted"])
async def test_a_project_skill_is_offered_only_once_the_project_is_trusted(
    tmp_path, tree, enabled, monkeypatch, trusted
):
    """Enumeration runs through the loader's own visibility walk, trust included.

    Both halves are asserted because the untrusted half alone would also pass if
    the project layout below were simply wrong.
    """
    from kiro_crew import skill_trust

    if trusted and not skill_trust.project_skill_traversal_supported():
        pytest.skip("project skills require no-follow directory-descriptor traversal")

    project = tmp_path / "project"
    _write_skill(project / ".kiro" / "skills", "projectskill", triggers="zebra")
    if trusted:
        skill_trust.grant_project_trust(project)
    decide = _Recorder("projectskill")
    monkeypatch.setattr(core, "decide", decide)
    builder = _builder(tmp_path, tree)

    message = await _message(builder, project=str(project))

    assert ("projectskill" in decide.keys) is trusted
    assert ("[Skill: projectskill]" in message) is trusted


@pytest.mark.asyncio
async def test_an_expired_budget_keeps_the_baseline_and_leaves_no_task(
    tmp_path, tree, enabled, monkeypatch
):
    """The wait is finite, the abandoned call is cancelled, and nothing lingers."""
    observed: dict[str, bool] = {}

    async def _slow(*args, **kwargs):
        observed["started"] = True
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            observed["cancelled"] = True
            raise
        return _answers(WIDENED)  # pragma: no cover - the sleep is cancelled

    monkeypatch.setattr(core, "decide", _slow)
    builder = _builder(tmp_path, tree)

    started = time.monotonic()
    message = await _message(builder)
    waited = time.monotonic() - started

    assert _bodies(message) == [BASELINE]
    assert waited < 10, "the message must not wait past the point's own budget"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not observed.get("cancelled"):
        await asyncio.sleep(0.02)
    assert observed == {"started": True, "cancelled": True}
    ours = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    assert ours == [], "the abandoned call must not outlive the message"


def test_a_builder_with_no_loop_keeps_the_baseline(tmp_path, tree, enabled, monkeypatch):
    """A sync caller (a script, a bare test) has no loop to submit to.

    Deliberately NOT an async test: this is the one path that must work with no
    loop anywhere, and it must refuse rather than build one.
    """
    decide = _Recorder(WIDENED)
    monkeypatch.setattr(core, "decide", decide)
    builder = _builder(tmp_path, tree)

    assert builder._decisions_loop is None
    message, _hook = builder.build_message("zebra please", False, "s-sync")

    assert _bodies(message) == [BASELINE]
    assert decide.calls == []
