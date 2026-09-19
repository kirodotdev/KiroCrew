"""Skill selection: candidate eligibility, exact answers and bounded fallback.

The context-assembly integration lives in test_decisions_integration.py.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew import decisions as core
from kiro_crew.decisions.points import MAX_KEY_CHARS
from kiro_crew.decisions.points import skills_select as sel
from kiro_crew.decisions.types import Answer


class _FakeLoader:
    """The four SkillsLoader members the selector reads."""

    def __init__(self, rows, meta, *, cap=3, scoped=()):
        self._rows = list(rows)
        self._meta = {str(Path(key)): value for key, value in meta.items()}
        self._cap = cap
        self._scoped = set(scoped)
        self.seen_project = "unset"

    def _iter_visible(self, project_dir=None):
        self.seen_project = project_dir
        return [(name, Path(path), within) for name, path, within in self._rows]

    def _cached_frontmatter(self, path, mtime=None, *, within=None):
        return self._meta[str(path)]

    def _repo_scope_satisfied(self, scope, project_dir=None):
        return scope in self._scoped

    def _max_triggered_now(self):
        return self._cap


def _loader(*entries, cap=3, scoped=()):
    rows = []
    meta = {}
    for name, frontmatter in entries:
        path = f"/s/{name or 'blank'}"
        rows.append((name, path, None))
        meta[path] = frontmatter
    return _FakeLoader(rows, meta, cap=cap, scoped=scoped)


def _write_skill(root, name, *, triggers="zebra", always=None, description="d", repo_scope=None):
    directory = root / name
    directory.mkdir(parents=True)
    frontmatter = f"---\nname: {name}\ndescription: {description}\n"
    if triggers is not None:
        frontmatter += f"triggers: {triggers}\n"
    if always is not None:
        frontmatter += f"always: {always}\n"
    if repo_scope is not None:
        frontmatter += f"repo_scope: {repo_scope}\n"
    (directory / "SKILL.md").write_text(frontmatter + "---\nbody", encoding="utf-8")


def test_a_skill_below_the_overlap_threshold_is_still_offered():
    loader = _loader(
        ("build", {"triggers": "build, compile", "description": "build system"}),
        ("review", {"triggers": "code review", "description": "reviews"}),
    )
    rows = sel.candidates_from_loader(loader, "please review this code", "/proj")
    assert rows == [
        {"key": "review", "description": "reviews"},
        {"key": "build", "description": "build system"},
    ], "both offered, best-scoring first"
    assert loader.seen_project == "/proj", "enumeration must be project-aware"


def test_the_menu_keeps_every_restriction_the_baseline_applies():
    loader = _loader(
        ("matcher", {"triggers": "zebra", "description": "eligible"}),
        ("pinned", {"triggers": "zebra", "always": "true"}),
        ("manual", {"triggers": "   "}),
        ("nofield", {"description": "no triggers key at all"}),
        ("vetoed", {"triggers": "zebra, !giraffe"}),
        ("elsewhere", {"triggers": "zebra", "repo_scope": "src/other"}),
        ("", {"triggers": "zebra"}),
        ("k" * (MAX_KEY_CHARS + 1), {"triggers": "zebra"}),
    )
    rows = sel.candidates_from_loader(loader, "zebra and giraffe please", None)
    assert [row["key"] for row in rows] == ["matcher"]


def test_a_repo_scope_the_loader_accepts_is_offered():
    loader = _loader(
        ("scoped", {"triggers": "zebra", "repo_scope": "src/kiro_crew"}),
        scoped={"src/kiro_crew"},
    )
    assert [row["key"] for row in sel.candidates_from_loader(loader, "zebra", "/proj")] == [
        "scoped"
    ]


def test_a_failing_repo_scope_gate_reads_as_not_satisfied():
    class _BadGate(_FakeLoader):
        def _repo_scope_satisfied(self, scope, project_dir=None):
            raise RuntimeError("gate exploded")

    loader = _BadGate(
        rows=[("scoped", "/s/scoped", None)],
        meta={"/s/scoped": {"triggers": "zebra", "repo_scope": "src/x"}},
    )
    assert sel.candidates_from_loader(loader, "zebra", "/proj") == []


def test_an_over_long_key_is_dropped_rather_than_truncated():
    huge = "k" * (MAX_KEY_CHARS * 3)
    loader = _loader((huge, {"triggers": "zebra"}))
    assert sel.candidates_from_loader(loader, "zebra", None) == []
    state = sel.build_state("zebra", [{"key": huge, "description": "d"}])
    assert state["candidates"] == [], "the state builder drops it too, and never trims it"


def test_a_row_whose_metadata_cannot_be_read_is_dropped():
    class _BadMeta(_FakeLoader):
        def _cached_frontmatter(self, path, mtime=None, *, within=None):
            raise OSError("gone")

    loader = _BadMeta(rows=[("x", "/s/x", None)], meta={})
    assert sel.candidates_from_loader(loader, "zebra", None) == []


def _rows_in(directory):
    import json

    return [
        json.loads(line)
        for f in sorted(directory.glob("*.jsonl"))
        for line in f.read_text().splitlines()
    ]


@pytest.fixture
def log_home(tmp_path, monkeypatch):
    from kiro_crew.decisions import log as log_mod

    directory = tmp_path / "home" / "decisions"
    directory.parent.mkdir()
    monkeypatch.setattr(log_mod, "log_dir", lambda: directory)
    return directory


def test_a_failing_enumeration_offers_nothing_and_says_so(log_home):
    """A broken walk must not read as an unsampled session: one error row lands."""

    class _BadIter(_FakeLoader):
        def _iter_visible(self, project_dir=None):
            raise RuntimeError("tree walk failed")

    assert sel.candidates_from_loader(_BadIter([], {}), "zebra", None, session_key="s") == []
    rows = _rows_in(log_home)
    assert [(r["point"], r["error"], r["answers"]) for r in rows] == [
        (sel.POINT, sel.ERROR_CANDIDATES, None)
    ]


def test_every_entry_unreadable_offers_nothing_and_says_so(log_home):
    """The walk worked but the reader failed on every entry: also a broken menu."""

    class _BadMeta(_FakeLoader):
        def _cached_frontmatter(self, path, mtime=None, *, within=None):
            raise OSError("gone")

    loader = _BadMeta(rows=[("x", "/s/x", None), ("y", "/s/y", None)], meta={})
    assert sel.candidates_from_loader(loader, "zebra", None, session_key="s") == []
    assert [r["error"] for r in _rows_in(log_home)] == [sel.ERROR_CANDIDATES]


def test_an_empty_tree_offers_nothing_quietly(log_home):
    """No skills installed is a real state, not a failure: no row."""
    assert sel.candidates_from_loader(_FakeLoader([], {}), "zebra", None, session_key="s") == []
    assert _rows_in(log_home) == []


def test_the_menu_is_capped_and_ordered_deterministically():
    scoring = [f"m{index:02d}" for index in range(60)]
    quiet = [f"z{index:02d}" for index in range(60)]
    entries = []
    for match, other in zip(scoring, quiet):
        entries.append((match, {"triggers": "zebra"}))
        entries.append((other, {"triggers": "nothing here"}))
    rows = sel.candidates_from_loader(_loader(*entries), "zebra", None)
    expected = scoring + quiet[: sel.MAX_CANDIDATES - len(scoring)]
    assert [row["key"] for row in rows] == expected
    assert len(rows) == sel.MAX_CANDIDATES


def test_the_state_bounds_the_prose_it_sends():
    state = sel.build_state("x" * 5000, [{"key": "build", "description": "d" * 500}])
    assert len(state["message"]) == sel.MAX_MESSAGE_CHARS
    assert state["candidates"] == [{"key": "build", "description": "d" * sel.MAX_DESCRIPTION_CHARS}]


def test_the_menu_against_the_real_skills_loader(tmp_path):
    from kiro_crew.skills import SkillsLoader

    root = tmp_path / "skills"
    _write_skill(root, "matcher", triggers="zebra, giraffe", description="animal work")
    _write_skill(root, "unrelated", triggers="quarterly invoice", description="billing")
    _write_skill(root, "pinned", triggers="zebra", always="true")
    _write_skill(root, "manual", triggers=None, description="no triggers at all")
    loader = SkillsLoader(skills_path=root, install_builtins=False)
    loader._max_triggered = 3
    rows = sel.candidates_from_loader(loader, "zebra please")
    assert [row["key"] for row in rows] == ["matcher", "unrelated"]
    assert loader.get_triggered_skills("zebra please") == ["matcher"]


def _answers(value):
    return {"pick": Answer(id="pick", value=value, p=0.9, confidence=None)}


def test_the_none_option_can_never_be_a_skill_key(tmp_path):
    """A skill may be called anything a directory can be called -- so the sentinel
    must be a name no directory can have, or picking that skill would read as
    declining to pick one."""
    from kiro_crew.skills import SkillsLoader

    assert sel.NONE_OPTION.startswith("/")
    assert not SkillsLoader._safe_name(sel.NONE_OPTION)
    # And the old, merely unusual spelling IS a legal skill name.
    assert SkillsLoader._safe_name("(no skill applies)")
    loader = _loader(("(no skill applies)", {"triggers": "review"}))
    candidates = sel.candidates_from_loader(loader, "review", None)
    keys = [c["key"] for c in candidates]
    assert "(no skill applies)" in keys
    assert sel.read_answer(_answers("(no skill applies)"), keys) == ["(no skill applies)"]
    assert sel.read_answer(_answers(sel.NONE_OPTION), keys) == []


@pytest.mark.parametrize(
    "answers,expected",
    [
        (_answers("review"), ["review"]),
        (_answers(sel.NONE_OPTION), []),
        (_answers("rev"), None),
        (_answers("review "), None),
        (_answers("build"), None),
        (_answers(None), None),
        (_answers(1.0), None),
        ({}, None),
        (None, None),
        ({"pick": "review"}, None),
        ({"other": Answer(id="other", value="review", p=0.9)}, None),
    ],
)
def test_only_an_exact_offered_key_is_admitted(answers, expected):
    assert sel.read_answer(answers, ["review", "tests"]) == expected


@pytest.mark.asyncio
async def test_select_skills_asks_one_question_over_the_menu(monkeypatch):
    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    picked = await sel.select_skills(
        "review this",
        [{"key": "review", "description": "reviews"}, {"key": "tests", "description": "tests"}],
        session_key="chat-1",
    )
    assert picked == ["review"]
    point, state, questions = spy.await_args.args
    assert point == "skills.select"
    assert state["message"] == "review this"
    assert [question.id for question in questions] == ["pick"]
    assert questions[0].options == ["review", "tests", sel.NONE_OPTION]
    assert spy.await_args.kwargs == {"session_key": "chat-1"}


@pytest.mark.asyncio
async def test_select_skills_does_not_ask_about_an_empty_menu(monkeypatch):
    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    assert await sel.select_skills("hi", [], session_key="s") is None
    spy.assert_not_awaited()


@pytest.fixture
def bg_loop():
    """Run the target loop off the test thread, with a bounded startup wait."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    loop.call_soon(ready.set)
    thread = threading.Thread(target=loop.run_forever, name="skills-select-test-loop", daemon=True)
    thread.start()
    try:
        assert ready.wait(10), "background loop did not start"
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        assert not thread.is_alive(), "background loop did not stop"
        loop.close()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(core, "is_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(core, "timeout_secs", lambda *args, **kwargs: 0.0)
    monkeypatch.setattr(sel, "WAIT_MARGIN_SECS", 0.0)
    monkeypatch.setattr(sel, "MIN_WAIT_SECS", 0.5)


def test_a_pick_is_returned_to_the_caller(bg_loop, enabled, monkeypatch):
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers("review")))
    loader = _loader(("review", {"triggers": "code review"}))
    picked = sel.selected_skills(loader, "review this code", None, session_key="s", loop=bg_loop)
    assert picked == ["review"]


def test_a_refusal_to_pick_is_a_real_empty_selection(bg_loop, enabled, monkeypatch):
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers(sel.NONE_OPTION)))
    loader = _loader(("review", {"triggers": "code review"}))
    assert sel.selected_skills(loader, "review this", None, session_key="s", loop=bg_loop) == []


def test_the_pick_is_capped_by_max_triggered(bg_loop, enabled, monkeypatch):
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers("review")))
    loader = _loader(("review", {"triggers": "code review"}), cap=1)
    assert sel.selected_skills(loader, "review this", None, session_key="s", loop=bg_loop) == [
        "review"
    ]


def test_a_cap_of_zero_means_no_selection_and_no_call(bg_loop, enabled, monkeypatch):
    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    loader = _loader(("review", {"triggers": "code review"}), cap=0)
    assert sel.selected_skills(loader, "review this", None, session_key="s", loop=bg_loop) is None
    spy.assert_not_awaited()
    assert loader.seen_project == "unset", "the skill tree must not be walked"


def test_an_unreadable_cap_reads_as_zero(bg_loop, enabled, monkeypatch):
    class _BadCap(_FakeLoader):
        def _max_triggered_now(self):
            raise RuntimeError("snapshot exploded")

    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    loader = _BadCap(
        rows=[("review", "/s/review", None)], meta={"/s/review": {"triggers": "review"}}
    )
    assert sel.selected_skills(loader, "review", None, session_key="s", loop=bg_loop) is None
    spy.assert_not_awaited()


def test_a_disabled_point_walks_nothing_and_asks_nothing(bg_loop, monkeypatch):
    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    monkeypatch.setattr(core, "is_enabled", lambda *args, **kwargs: False)
    loader = _loader(("review", {"triggers": "code review"}))
    assert sel.selected_skills(loader, "review this", None, session_key="s", loop=bg_loop) is None
    spy.assert_not_awaited()
    assert loader.seen_project == "unset"


def test_an_empty_menu_asks_nothing(bg_loop, enabled, monkeypatch):
    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    loader = _loader(("pinned", {"triggers": "zebra", "always": "true"}))
    assert sel.selected_skills(loader, "zebra", None, session_key="s", loop=bg_loop) is None
    spy.assert_not_awaited()


def test_a_raising_transport_keeps_the_baseline(bg_loop, enabled, monkeypatch):
    monkeypatch.setattr(core, "decide", AsyncMock(side_effect=RuntimeError("transport died")))
    loader = _loader(("review", {"triggers": "code review"}))
    assert sel.selected_skills(loader, "review this", None, session_key="s", loop=bg_loop) is None


def test_an_expired_budget_cancels_the_call_and_keeps_the_baseline(bg_loop, enabled, monkeypatch):
    """The bounded wait discards late answers and propagates cancellation."""
    observed: dict[str, bool] = {}

    async def _slow(*args, **kwargs):
        observed["started"] = True
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            observed["cancelled"] = True
            raise
        return _answers("review")  # pragma: no cover - the sleep is cancelled

    monkeypatch.setattr(core, "decide", _slow)
    loader = _loader(("review", {"triggers": "code review"}))
    started = time.monotonic()
    picked = sel.selected_skills(loader, "review this", None, session_key="s", loop=bg_loop)
    waited = time.monotonic() - started
    assert picked is None
    assert waited < 10, "the caller must not wait past its own budget"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not observed.get("cancelled"):
        time.sleep(0.02)
    assert observed == {"started": True, "cancelled": True}, "no detached call may survive"


def test_no_loop_means_no_selection(enabled, monkeypatch):
    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    loader = _loader(("review", {"triggers": "code review"}))
    assert sel.selected_skills(loader, "review", None, session_key="s", loop=None) is None
    spy.assert_not_awaited()


def test_a_closed_loop_means_no_selection(enabled, monkeypatch):
    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    loop = asyncio.new_event_loop()
    loop.close()
    loader = _loader(("review", {"triggers": "code review"}))
    assert sel.selected_skills(loader, "review", None, session_key="s", loop=loop) is None
    spy.assert_not_awaited()


def test_a_loop_that_is_not_running_means_no_selection(enabled, monkeypatch):
    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    loop = asyncio.new_event_loop()
    loader = _loader(("review", {"triggers": "code review"}))
    try:
        assert sel.selected_skills(loader, "review", None, session_key="s", loop=loop) is None
    finally:
        loop.close()
    spy.assert_not_awaited()


def test_a_scheduling_failure_closes_the_coroutine(bg_loop, enabled, monkeypatch):
    import inspect

    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers("review")))
    created = []

    def _refuse(coro, loop):
        created.append(coro)
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CREATED
        raise RuntimeError("event loop is closed")

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _refuse)
    loader = _loader(("review", {"triggers": "code review"}))
    try:
        assert sel.selected_skills(loader, "review", None, session_key="s", loop=bg_loop) is None
        assert len(created) == 1
        assert inspect.getcoroutinestate(created[0]) == inspect.CORO_CLOSED
    finally:
        for coro in created:
            coro.close()


@pytest.mark.asyncio
async def test_a_caller_on_the_loop_thread_keeps_the_baseline(enabled, monkeypatch):
    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    loader = _loader(("review", {"triggers": "code review"}))
    picked = sel.selected_skills(
        loader, "review", None, session_key="s", loop=asyncio.get_running_loop()
    )
    assert picked is None
    spy.assert_not_awaited()


@pytest.mark.parametrize(
    "provider_secs,expected",
    [
        (0.0, sel.WAIT_MARGIN_SECS),
        (1.0, 1.0 + sel.WAIT_MARGIN_SECS),
        (3600.0, sel.MAX_WAIT_SECS),
        (float("inf"), sel.MIN_WAIT_SECS),
        (float("nan"), sel.MIN_WAIT_SECS),
        (-5.0, sel.MIN_WAIT_SECS),
    ],
)
def test_the_wait_budget_is_clamped(monkeypatch, provider_secs, expected):
    monkeypatch.setattr(core, "timeout_secs", lambda *args, **kwargs: provider_secs)
    assert sel._wait_budget() == expected


def test_an_unreadable_provider_budget_falls_back_to_the_floor(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("no config")

    monkeypatch.setattr(core, "timeout_secs", _boom)
    assert sel._wait_budget() == sel.MIN_WAIT_SECS
