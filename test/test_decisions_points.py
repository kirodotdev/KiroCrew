"""Skill selection: candidate eligibility, exact answers and bounded fallback.

Also the three things a sampled turn now carries beside the menu: the prior turns
inside their char budget, the rounds a menu too large for one request is split
into, and the outcome row that holds BOTH arms (word overlap and Jev) with the
agreement and the body characters the difference saves.

The context-assembly integration lives in test_decisions_integration.py.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew import credential_patterns as _cred
from kiro_crew import decisions as core
from kiro_crew.decisions.points import MAX_KEY_CHARS
from kiro_crew.decisions.points import skills_select as sel
from kiro_crew.decisions.types import Answer


class _FakeLoader:
    """The SkillsLoader members the selector reads, plus the two it measures with."""

    def __init__(self, rows, meta, *, cap=3, scoped=(), bodies=None):
        self._rows = list(rows)
        self._meta = {str(Path(key)): value for key, value in meta.items()}
        self._cap = cap
        self._scoped = set(scoped)
        self._bodies = dict(bodies or {})
        self.sized = []
        self.seen_project = "unset"

    def load_skill(self, name, project_dir=None):
        self.sized.append(name)
        return self._bodies.get(name)

    def strip_frontmatter(self, content):
        return content

    def _iter_visible(self, project_dir=None):
        self.seen_project = project_dir
        return [(name, Path(path), within) for name, path, within in self._rows]

    def _cached_frontmatter(self, path, mtime=None, *, within=None):
        return self._meta[str(path)]

    def _repo_scope_satisfied(self, scope, project_dir=None):
        return scope in self._scoped

    def _max_triggered_now(self):
        return self._cap


def _loader(*entries, cap=3, scoped=(), bodies=None):
    rows = []
    meta = {}
    for name, frontmatter in entries:
        path = f"/s/{name or 'blank'}"
        rows.append((name, path, None))
        meta[path] = frontmatter
    return _FakeLoader(rows, meta, cap=cap, scoped=scoped, bodies=bodies)


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
    assert spy.await_args.kwargs["session_key"] == "chat-1"
    extra = spy.await_args.kwargs["extra"]
    assert extra["candidates"] == 2
    assert extra["history_chars"] == 0 and extra["truncated"] == 0
    assert "round" not in extra and "batches" not in extra
    assert isinstance(extra["turn_id"], str) and extra["turn_id"]


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


# ---------------------------------------------------------------------------
# Prior turns: what the budget buys, and what it refuses to carry
# ---------------------------------------------------------------------------


def _menu(count, *, description="d" * 8):
    return [{"key": f"s{index}", "description": description} for index in range(count)]


def _turns(*pairs):
    """Transcript rows in ``ConversationLog.recent`` order: oldest first."""
    return [{"role": role, "content": content} for role, content in pairs]


def test_prior_turns_are_sent_newest_first():
    """Newest first because that is the order the budget spends in."""
    rows = sel.build_history(
        _turns(("user", "oldest"), ("assistant", "middle"), ("user", "newest")),
        "now",
        history_budget_chars=1000,
    )
    assert rows == [
        {"role": "user", "text": "newest"},
        {"role": "assistant", "text": "middle"},
        {"role": "user", "text": "oldest"},
    ]


def test_the_budget_stops_the_walk_and_clips_the_last_turn_admitted():
    trace = {}
    rows = sel.build_history(
        _turns(("user", "a" * 50), ("assistant", "b" * 50), ("user", "c" * 50)),
        "now",
        history_budget_chars=60,
        trace=trace,
    )
    assert [row["text"] for row in rows] == ["c" * 50, "b" * 10]
    assert trace == {"history_chars": 60, "truncated": 1}, "one clip, and the budget spent exactly"


def test_the_shipped_default_sends_no_prior_turns_at_all(monkeypatch):
    """Consent was given for the message and the menu, so that is what ships.

    An owner who wants the conversation sent raises
    ``decisions.history_budget_chars`` themselves; an upgrade does not raise it
    for them.
    """
    from kiro_crew.config.sections import DECISION_HISTORY_BUDGET_DEFAULT, DecisionsConfig

    assert DECISION_HISTORY_BUDGET_DEFAULT == 0
    assert DecisionsConfig().history_budget_chars == 0
    monkeypatch.setattr(core, "history_budget_chars", lambda *a, **k: 0)
    trace = {}
    assert sel.build_history(_turns(("user", "prior")), "now", trace=trace) == []
    assert trace == {"history_chars": 0, "truncated": 0}


def test_a_budget_of_zero_sends_the_message_alone():
    trace = {}
    assert (
        sel.build_history(_turns(("user", "prior")), "now", history_budget_chars=0, trace=trace)
        == []
    )
    assert trace == {"history_chars": 0, "truncated": 0}


def test_no_reachable_history_is_recorded_as_zero_chars_and_sent_as_nothing():
    """Reachability is answered by the ROW, and the wire carries no empty key.

    ``history_chars`` on the call row distinguishes "no prior turns were
    reachable" from "this build does not send them", so the request does not have
    to: at the shipped default its shape is the one that shipped before prior
    turns existed.
    """
    trace = {}
    assert sel.build_history(None, "now", history_budget_chars=2000, trace=trace) == []
    assert trace == {"history_chars": 0, "truncated": 0}
    state = sel.build_state("now", [{"key": "review", "description": "d"}], None)
    assert "history" not in state, "an empty history is omitted, not sent as []"
    assert set(state) == {"message", "candidates"}


@pytest.mark.parametrize("role", ["tool", "system", "tool_result", "", None])
def test_only_user_and_assistant_text_leaves_the_machine(role):
    """Tool output is the largest and least selective text in a transcript."""
    rows = sel.build_history(
        [{"role": role, "content": "cat /etc/passwd output"}], "now", history_budget_chars=1000
    )
    assert rows == []


def test_the_current_message_is_never_repeated_as_a_prior_turn():
    rows = sel.build_history(
        _turns(("user", "older"), ("user", "review this")), "review this", history_budget_chars=1000
    )
    assert [row["text"] for row in rows] == ["older"]


def test_an_unusable_history_row_is_skipped_not_fatal():
    rows = sel.build_history(
        [
            "not a mapping",
            {"role": "user"},
            {"role": "user", "content": 7},
            {"role": "user", "content": "ok"},
        ],
        "now",
        history_budget_chars=1000,
    )
    assert [row["text"] for row in rows] == ["ok"]


def test_the_history_reaches_the_state_the_oracle_is_sent():
    state = sel.build_state(
        "now",
        [{"key": "review", "description": "d"}],
        _turns(("user", "prior turn")),
        history_budget_chars=1000,
    )
    assert state == {
        "message": "now",
        "history": [{"role": "user", "text": "prior turn"}],
        "candidates": [{"key": "review", "description": "d"}],
    }


def test_a_history_source_that_raises_reads_as_no_history(bg_loop, enabled, monkeypatch):
    """A budget above 0 is patched in, or the source is never called and this
    proves nothing: the point skips the read entirely at the default."""
    calls = []

    def _boom():
        calls.append(1)
        raise OSError("transcript unreadable")

    monkeypatch.setattr(core, "history_budget_chars", lambda *a, **k: 2000)
    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    loader = _loader(("review", {"triggers": "code review"}))
    picked = sel.selected_skills(
        loader, "review this", None, session_key="s", loop=bg_loop, history_source=_boom
    )
    assert calls == [1], "the source really was asked"
    assert picked == ["review"], "prior turns make the question better, not answerable"
    assert "history" not in spy.await_args.args[1]


def test_the_history_source_is_not_called_at_the_shipped_budget(bg_loop, enabled, monkeypatch):
    """At a budget of 0 there is nothing a transcript read could contribute, so a
    sampled turn does not pay for one either."""
    calls = []
    monkeypatch.setattr(core, "history_budget_chars", lambda *a, **k: 0)
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers("review")))
    loader = _loader(("review", {"triggers": "code review"}))

    picked = sel.selected_skills(
        loader,
        "review this",
        None,
        session_key="s",
        loop=bg_loop,
        history_source=lambda: calls.append(1) or [],
    )

    assert picked == ["review"]
    assert calls == [], "the budget is read first, so the transcript is not"


def test_the_history_source_is_not_called_for_an_unsampled_turn(bg_loop, monkeypatch):
    """A transcript read is paid on the sampled turns and nowhere else."""
    calls = []
    monkeypatch.setattr(core, "is_enabled", lambda *args, **kwargs: False)
    loader = _loader(("review", {"triggers": "code review"}))
    assert (
        sel.selected_skills(
            loader,
            "review this",
            None,
            session_key="s",
            loop=bg_loop,
            history_source=lambda: calls.append(1) or [],
        )
        is None
    )
    assert calls == []


# The prior turns are new EGRESS, so the one property that matters about where
# they sit is that the scrub sees them. Samples are derived from the pattern
# source rather than written out: a contiguous key-shaped literal is refused by
# the repo's own secret scanners, correctly, since neither they nor Semgrep can
# tell a test vector from a real leak.
_AWS_KEY = _cred.AWS_KEY_ID_PREFIXES.split("|")[0] + "A2B3C4D5E6F7G8H9"
_EXFIL_URL = "https://collector.example.invalid/x?sess" + "ion=" + "b" * 40


def _state_with_prior_turn(text):
    return sel.build_state(
        "which skill applies?",
        [{"key": "review", "description": "reviews"}],
        [{"role": "assistant", "content": text}],
        history_budget_chars=2000,
    )


@pytest.mark.parametrize("secret", [_AWS_KEY, _EXFIL_URL], ids=["credential", "exfiltration-url"])
def test_a_secret_in_a_prior_turn_refuses_the_whole_request(secret):
    """Prior turns are inside ``state``, which is what the gate renders and scans.

    The placement is the claim: history carried beside the state -- a sibling
    argument, a header -- would be egress the scrub never sees, and this turn's
    own message being clean says nothing about the turn before it.
    """
    from kiro_crew.decisions import gate

    state = _state_with_prior_turn(f"here it is: {secret}")
    assert state["history"], "the turn under test must actually be in the state"
    assert gate.scrub_reason(state, []) in gate.SCRUB_ERRORS


def test_a_clean_prior_turn_is_sent():
    """The refusal above must be the secret, not prior turns being refused wholesale."""
    from kiro_crew.decisions import gate

    assert gate.scrub_reason(_state_with_prior_turn("we were talking about invoices"), []) is None


# ---------------------------------------------------------------------------
# One question, whatever the menu size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_whole_menu_is_one_question(monkeypatch):
    """The menu has a ceiling of its own, so there is nothing for a split to do.

    ``MAX_CANDIDATES`` x (``MAX_KEY_CHARS`` + ``MAX_DESCRIPTION_CHARS``) is 32,000
    characters; a request cannot grow past it however large the skill tree is.
    """
    spy = AsyncMock(return_value=_answers("s0"))
    monkeypatch.setattr(core, "decide", spy)
    picked = await sel.select_skills(
        "review this", _menu(sel.MAX_CANDIDATES), session_key="s", history_budget_chars=0
    )
    assert picked == ["s0"]
    assert spy.await_count == 1, "one menu, one question, one answer to reconcile"


def test_the_menu_ceiling_is_a_bound_in_its_own_right():
    """The number that makes a per-request budget unnecessary, pinned as arithmetic."""
    ceiling = sel.MAX_CANDIDATES * (MAX_KEY_CHARS + sel.MAX_DESCRIPTION_CHARS)
    assert ceiling == 32_000
    rows = sel.screen_candidates(
        [
            {"key": "k" * MAX_KEY_CHARS, "description": "d" * sel.MAX_DESCRIPTION_CHARS}
            for _ in range(sel.MAX_CANDIDATES * 3)
        ]
    )
    measured = sum(len(row["key"]) + len(row["description"]) for row in rows)
    assert measured == ceiling, "the screen enforces the ceiling the arithmetic claims"


def test_the_split_menu_vocabulary_is_gone():
    """A helper nothing can reach is not a feature; it is a thing to delete."""
    for name in ("plan_batches", "runoff_menu", "planned_rounds", "menu_chars", "_state_budget"):
        assert not hasattr(sel, name), f"{name} came back"
    assert not hasattr(core, "state_budget_chars")


# ---------------------------------------------------------------------------
# Both arms: what word overlap would have injected, beside what Jev picked
# ---------------------------------------------------------------------------


def test_the_baseline_arm_is_the_matchers_own_result(tmp_path):
    """Computed from the SAME walk, and equal to what the loader would inject."""
    from kiro_crew.skills import SkillsLoader

    root = tmp_path / "skills"
    _write_skill(root, "matcher", triggers="zebra, giraffe", description="animal work")
    _write_skill(root, "second", triggers="zebra", description="also animals")
    _write_skill(root, "unrelated", triggers="quarterly invoice", description="billing")
    loader = SkillsLoader(skills_path=root, install_builtins=False)
    loader._max_triggered = 3
    baseline = []
    rows = sel.candidates_from_loader(loader, "zebra please", baseline_out=baseline)
    assert baseline == loader.get_triggered_skills("zebra please")
    assert set(baseline) == {"matcher", "second"}
    assert "unrelated" in [row["key"] for row in rows], "the menu is wider than the baseline"


def test_the_baseline_arm_honours_the_same_cap(tmp_path):
    from kiro_crew.skills import SkillsLoader

    root = tmp_path / "skills"
    _write_skill(root, "one", triggers="zebra")
    _write_skill(root, "two", triggers="zebra")
    loader = SkillsLoader(skills_path=root, install_builtins=False)
    loader._max_triggered = 1
    baseline = []
    sel.candidates_from_loader(loader, "zebra", baseline_out=baseline)
    assert baseline == loader.get_triggered_skills("zebra")
    assert len(baseline) == 1


def test_a_skill_the_message_does_not_match_is_offered_but_not_in_the_baseline():
    loader = _loader(
        ("review", {"triggers": "code review", "description": "reviews"}),
        ("build", {"triggers": "compile the binary", "description": "builds"}),
    )
    baseline = []
    rows = sel.candidates_from_loader(loader, "please review this code", baseline_out=baseline)
    assert [row["key"] for row in rows] == ["review", "build"]
    assert baseline == ["review"]


def test_tokens_saved_measures_only_the_difference():
    loader = _loader(
        ("big", {"triggers": "zebra"}),
        ("small", {"triggers": "zebra"}),
        ("shared", {"triggers": "zebra"}),
        bodies={"big": "b" * 4000, "small": "s" * 400, "shared": "x" * 8000},
    )
    saved = sel.tokens_saved(loader, ["big", "shared"], ["small", "shared"], None)
    assert saved == (4000 - 400) // sel.CHARS_PER_TOKEN
    assert loader.sized == ["big", "small"], "a skill both arms chose is never read"


def test_tokens_saved_is_negative_when_the_pick_costs_more():
    loader = _loader(("big", {"triggers": "zebra"}), bodies={"big": "b" * 800})
    assert sel.tokens_saved(loader, [], ["big"], None) == -200


def test_an_unreadable_body_measures_as_nothing():
    class _NoBody(_FakeLoader):
        def load_skill(self, name, project_dir=None):
            raise OSError("gone")

    loader = _NoBody(rows=[], meta={})
    assert sel.tokens_saved(loader, ["a"], [], None) == 0


@pytest.mark.parametrize(
    "baseline,injected,agree",
    [
        (["review"], ["review"], True),
        (["review", "tests"], ["tests", "review"], True),
        (["review"], ["tests"], False),
        (["review"], [], False),
        ([], [], True),
    ],
)
def test_agree_is_set_equality(baseline, injected, agree):
    """Two identical sets in a different order are not a disagreement."""
    outcome = sel.build_outcome(
        _loader(), None, baseline=baseline, injected=injected, trace={"turn_id": "t"}
    )
    assert outcome["agree"] is agree


def test_the_outcome_row_carries_both_arms(bg_loop, enabled, log_home, monkeypatch):
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers("wide")))
    loader = _loader(
        ("narrow", {"triggers": "code review", "description": "the matched one"}),
        ("wide", {"triggers": "unrelated words", "description": "the offered one"}),
        bodies={"narrow": "n" * 2000, "wide": "w" * 400},
    )
    picked = sel.selected_skills(loader, "review this code", None, session_key="s", loop=bg_loop)
    assert picked == ["wide"]
    outcome = [row for row in _rows_in(log_home) if "baseline" in row]
    assert len(outcome) == 1, "one outcome row per turn"
    row = outcome[0]
    assert row["point"] == sel.POINT
    assert row["baseline"] == ["narrow"] and row["jev"] == ["wide"]
    assert row["agree"] is False
    assert row["p"] == 0.9
    assert row["tokens_saved"] == (2000 - 400) // sel.CHARS_PER_TOKEN
    assert row["candidates"] == 2
    assert row["history_chars"] == 0 and row["truncated"] == 0
    assert "batches" not in row and "rounds" not in row
    assert isinstance(row["turn_id"], str) and row["turn_id"]
    assert "message" not in json.dumps(row), "no conversation text in a row"


def test_agreement_is_recorded_as_a_turn_that_saved_nothing(
    bg_loop, enabled, log_home, monkeypatch
):
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers("review")))
    loader = _loader(
        ("review", {"triggers": "code review", "description": "reviews"}),
        bodies={"review": "r" * 4000},
    )
    assert sel.selected_skills(loader, "review this code", None, session_key="s", loop=bg_loop) == [
        "review"
    ]
    row = [row for row in _rows_in(log_home) if "baseline" in row][0]
    assert row["baseline"] == ["review"] and row["jev"] == ["review"]
    assert row["agree"] is True and row["tokens_saved"] == 0
    assert loader.sized == [], "agreement costs no body read at all"


def test_a_refused_turn_writes_no_outcome_row(bg_loop, enabled, log_home, monkeypatch):
    """An ``agree`` against an answer that never arrived compares one arm with nothing."""
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=None))
    loader = _loader(("review", {"triggers": "code review"}))
    assert sel.selected_skills(loader, "review this", None, session_key="s", loop=bg_loop) is None
    assert [row for row in _rows_in(log_home) if "baseline" in row] == []


# ---------------------------------------------------------------------------
# The outcome publish hook, which this module only ever reads
# ---------------------------------------------------------------------------


def test_the_outcome_is_published_when_the_module_is_importable(monkeypatch):
    import sys
    import types

    seen = []
    module = types.ModuleType(sel.OUTCOMES_MODULE)
    module.publish = lambda session_key, outcome: seen.append((session_key, outcome))
    monkeypatch.setitem(sys.modules, sel.OUTCOMES_MODULE, module)
    row = {"turn_id": "t", "baseline": ["a"], "jev": [], "agree": False}
    assert sel.publish_outcome("chat-1", row) is True
    assert seen == [("chat-1", row)], "the row exactly as it was written"


def test_a_build_without_the_outcomes_module_publishes_nothing(monkeypatch):
    """A ``None`` entry in ``sys.modules`` is what an unimportable module raises as.

    Patching ``builtins.__import__`` would NOT simulate this: ``import_module``
    resolves through ``sys.modules`` and the import machinery, not through that
    hook, so such a test passes only while the module happens not to exist and
    goes vacuous the moment it does. The ``pytest.raises`` below is the guard
    against that: the mechanism has to still produce an ``ImportError`` for the
    assertion under it to mean anything.
    """
    import importlib
    import sys

    monkeypatch.setitem(sys.modules, sel.OUTCOMES_MODULE, None)
    with pytest.raises(ImportError):
        importlib.import_module(sel.OUTCOMES_MODULE)
    assert sel.publish_outcome("chat-1", {"turn_id": "t"}) is False


def test_a_module_without_a_publisher_is_a_no_op(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, sel.OUTCOMES_MODULE, types.ModuleType(sel.OUTCOMES_MODULE))
    assert sel.publish_outcome("chat-1", {"turn_id": "t"}) is False


def test_a_raising_publisher_cannot_cost_the_turn_its_answer(
    bg_loop, enabled, log_home, monkeypatch
):
    import sys
    import types

    module = types.ModuleType(sel.OUTCOMES_MODULE)

    def _boom(session_key, outcome):
        raise RuntimeError("dashboard down")

    module.publish = _boom
    monkeypatch.setitem(sys.modules, sel.OUTCOMES_MODULE, module)
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers("review")))
    loader = _loader(("review", {"triggers": "code review"}))
    assert sel.selected_skills(loader, "review this", None, session_key="s", loop=bg_loop) == [
        "review"
    ]
    assert [row for row in _rows_in(log_home) if "baseline" in row], "the row still landed"


def test_a_refused_row_is_not_published(bg_loop, enabled, log_home, monkeypatch):
    """A strip whose durable row was refused describes a decision no verdict can
    be filed against, so it does not reach the reply."""
    import sys
    import types

    from kiro_crew.decisions import log as log_mod

    seen = []
    module = types.ModuleType(sel.OUTCOMES_MODULE)
    module.publish = lambda session_key, outcome: seen.append(outcome)
    monkeypatch.setitem(sys.modules, sel.OUTCOMES_MODULE, module)
    monkeypatch.setattr(log_mod, "append", lambda row: False)
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers("review")))
    loader = _loader(("review", {"triggers": "code review"}))

    picked = sel.selected_skills(loader, "review this", None, session_key="s", loop=bg_loop)

    assert picked == ["review"], "the pick is unaffected: the row is an observation"
    assert seen == [], "nothing published without its durable row"


def test_the_published_outcome_is_the_row_that_was_logged(bg_loop, enabled, log_home, monkeypatch):
    import sys
    import types

    seen = []
    module = types.ModuleType(sel.OUTCOMES_MODULE)
    module.publish = lambda session_key, outcome: seen.append((session_key, outcome))
    monkeypatch.setitem(sys.modules, sel.OUTCOMES_MODULE, module)
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers("review")))
    loader = _loader(("review", {"triggers": "code review"}))
    sel.selected_skills(loader, "review this", None, session_key="s", loop=bg_loop)
    logged = [row for row in _rows_in(log_home) if "baseline" in row]
    assert len(seen) == 1
    session_key, published = seen[0]
    assert session_key == "s"
    assert published == logged[0], "one description of one turn, not two"
    assert "ts" in published and "turn_id" in published
