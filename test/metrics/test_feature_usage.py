"""Feature-use counters: closed value lists, the record functions, and their call sites.

Each list is pinned exactly, because adding a value is a reviewed change to what
an exported series can carry. Each call site is checked to be wired where the
module docstring says, after the action it measures.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from unittest.mock import patch

import pytest

from kiro_crew.constants import CHANNEL_SESSION_NAMESPACES
from kiro_crew.metrics import feature_usage as fu

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "kiro_crew"


class _Rec:
    enabled = True

    def __init__(self) -> None:
        self.counters: list[dict] = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "value": value, "attrs": dict(attrs or {})})

    def histogram(self, *a, **k) -> None:
        pass


@pytest.fixture
def rec():
    r = _Rec()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


def _named(rec, name):
    return [c for c in rec.counters if c["name"] == name]


# -- closed lists -------------------------------------------------------------


def test_value_lists_are_pinned():
    assert fu.MEMORY_PATH_VALUES == ("tool", "context")
    assert fu.LESSON_RECEIPT_KINDS == ("lessons", "turn_lessons")
    assert fu.APP_TASK_OUTCOME_VALUES == ("ok", "error", "other")
    assert fu.CHANNEL_VALUES == tuple(CHANNEL_SESSION_NAMESPACES) + ("other",)
    assert len(fu.CHANNEL_VALUES) == 12
    assert fu.UI_CONTROL_VALUES == (
        "option_chip",
        "question_card_answer",
        "question_card_freetext",
        "followup_start_worktree",
        "followup_start_chat",
        "followup_add_session",
        "followup_skip",
        "other",
    )


def test_memory_blocks_are_real_context_block_labels():
    from kiro_crew.context_blocks import _MARKERS

    labels = {label for label, _ in _MARKERS}
    assert set(fu.MEMORY_BLOCKS) <= labels
    assert "lessons" in labels


def test_lesson_receipt_kinds_are_real_receipt_kinds():
    from kiro_crew import injection_receipts

    assert set(fu.LESSON_RECEIPT_KINDS) <= set(injection_receipts.KINDS)


def test_shipped_skill_ids_are_the_shipped_skill_dirs():
    root = SRC / "builtin_skills"
    builtin = {
        p.parent.relative_to(root).as_posix() for p in root.rglob("SKILL.md") if p.parent != root
    }
    repo_dir = ROOT / "skills"
    assert builtin, "no builtin skills found"
    assert builtin == set(fu.BUILTIN_SKILL_IDS), (
        "a shipped skill was added or removed: update BUILTIN_SKILL_IDS in "
        "metrics/feature_usage.py to match builtin_skills/"
    )
    if repo_dir.is_dir():
        repo = {p.name for p in repo_dir.iterdir() if (p / "SKILL.md").is_file()}
        assert set(fu.REPO_SKILL_IDS) == repo
    assert fu.SHIPPED_SKILL_IDS == builtin | set(fu.REPO_SKILL_IDS)


def test_a_nested_shipped_skill_is_sent_by_its_nested_id():
    nested = sorted(i for i in fu.SHIPPED_SKILL_IDS if "/" in i)
    assert nested, "expected the nested kirocrew-dev skills to be shipped"
    for skill_id in nested:
        assert fu.skill_label(skill_id) == skill_id


def test_labelling_reads_no_directory():
    """The id lists are spelled out, so labelling a skill or an app on the
    gateway loop does no filesystem work."""
    with (
        patch("pathlib.Path.iterdir") as iterdir,
        patch("pathlib.Path.rglob") as rglob,
        patch("pathlib.Path.is_file") as is_file,
    ):
        fu.skill_label("kirocrew-dev/writing-tests")
        fu.skill_label("my-own-skill")
        fu.app_label("papyrus")
    iterdir.assert_not_called()
    rglob.assert_not_called()
    is_file.assert_not_called()


def test_official_app_ids_are_builtin_apps_that_report():
    root = SRC / "apps" / "builtins"
    dirs = {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))}
    assert fu.OFFICIAL_APP_IDS <= dirs
    assert "private" not in fu.OFFICIAL_APP_IDS
    reported = set()
    for path in SRC.rglob("*.py"):
        if path.name == "feature_usage.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "record_app_task(" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", "")) == "record_app_task"
            ):
                first = node.args[0]
                assert isinstance(first, ast.Constant), f"{path}:{node.lineno}"
                reported.add(first.value)
    assert reported == set(
        fu.OFFICIAL_APP_IDS
    ), "every reporting app is listed, and every listed app reports"


def test_memory_recalled(rec):
    fu.record_memory_recalled("tool", hit=True)
    fu.record_memory_recalled("weird", hit=False)
    assert [c["attrs"] for c in _named(rec, fu.MEMORY_RECALLED)] == [
        {"path": "tool", "hit": "true"},
        {"path": "other", "hit": "false"},
    ]


@pytest.mark.parametrize(
    "payload,hit",
    [
        ({"retrieval": {"facts": [{"id": "x"}], "episodes": []}}, True),
        ({"retrieval": {"facts": [], "episodes": [{"id": "e"}]}}, True),
        ({"retrieval": {"facts": [], "episodes": []}}, False),
        ({"context": "  "}, False),
        ({"context": "some memory"}, True),
        ({}, False),
        (None, False),
    ],
)
def test_recall_payload_hit(payload, hit):
    assert fu.recall_payload_hit(payload) is hit


def test_context_composition_with_memory_and_lessons(rec):
    fu.record_context_composition(
        {"memory": 120, "lessons": 300}, {"lessons": {"ids": ["lesson.a"], "omitted": 0}}
    )
    assert [c["attrs"] for c in _named(rec, fu.MEMORY_RECALLED)] == [
        {"path": "context", "hit": "true"}
    ]
    assert [c["attrs"] for c in _named(rec, fu.LESSON_INJECTED)] == [{"hit": "true"}]


def test_context_composition_without_a_lesson_tier_records_no_lesson_point(rec):
    fu.record_context_composition({"date": 10}, {})
    assert [c["attrs"] for c in _named(rec, fu.MEMORY_RECALLED)] == [
        {"path": "context", "hit": "false"}
    ]
    assert _named(rec, fu.LESSON_INJECTED) == []


def test_an_empty_lesson_tier_is_a_miss(rec):
    fu.record_context_composition({}, {"turn_lessons": {"ids": [], "omitted": 0}})
    assert [c["attrs"] for c in _named(rec, fu.LESSON_INJECTED)] == [{"hit": "false"}]


def test_unnamed_lesson_rows_still_count_as_a_hit(rec):
    """A store that keeps no lesson key records no ids, but the block is there."""
    fu.record_context_composition({"lessons": 80}, {"lessons": {"ids": [], "omitted": 0}})
    assert [c["attrs"] for c in _named(rec, fu.LESSON_INJECTED)] == [{"hit": "true"}]


def test_findings_alone_are_not_a_lesson_tier(rec):
    fu.record_context_composition({}, {"findings": {"ids": ["f"], "omitted": 0}})
    assert _named(rec, fu.LESSON_INJECTED) == []


def test_composition_counters_never_raise():
    with patch("kiro_crew.metrics.provider.get_recorder", side_effect=RuntimeError("x")):
        fu.record_context_composition({"memory": 1}, {"lessons": {"ids": ["a"]}})
        fu.record_context_composition(object(), object())


@pytest.mark.parametrize(
    "name,label",
    [
        ("papyrus-writing", "papyrus-writing"),
        ("goal-loop", "goal-loop"),
        ("my-secret-project-skill", "custom"),
        ("team/nested", "custom"),
        ("../escape", "other"),
        ("", "other"),
        (None, "other"),
        ("bad name with spaces", "other"),
    ],
)
def test_skill_label(name, label):
    assert fu.skill_label(name) == label


def test_the_lazy_load_counter_carries_the_skill_label(rec):
    from kiro_crew import skills_body_reads

    # Patched through the skills facade, which every owner in the family reads.
    with patch("kiro_crew.skills.get_recorder", return_value=rec):
        skills_body_reads._emit_lazy_load_metric(0.0, hit=True, skill="widgets")
        skills_body_reads._emit_lazy_load_metric(0.0, hit=False, skill="acme-internal")
    assert [c["attrs"] for c in _named(rec, fu.SKILL_LAZY_LOAD_COUNT)] == [
        {"hit": True, "skill": "widgets"},
        {"hit": False, "skill": "custom"},
    ]
    # One counter counts skill body loads; no second name is emitted.
    assert {c["name"] for c in rec.counters} == {fu.SKILL_LAZY_LOAD_COUNT}


def test_every_lazy_load_emit_names_its_skill():
    tree = ast.parse((SRC / "skills_body_reads.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_emit_lazy_load_metric"
    ]
    assert len(calls) == 4
    for call in calls:
        assert "skill" in {k.arg for k in call.keywords}, call.lineno


def test_app_task(rec):
    fu.record_app_task("papyrus", "ok")
    fu.record_app_task("someone-elses-app", "error")
    fu.record_app_task("papyrus", "exploded")
    assert [c["attrs"] for c in _named(rec, fu.APP_TASK_COMPLETED)] == [
        {"app": "papyrus", "outcome": "ok"},
        {"app": "private", "outcome": "error"},
        {"app": "papyrus", "outcome": "other"},
    ]


@pytest.mark.parametrize(
    "key,label",
    [
        ("telegram:42", "telegram"),
        ("slack:T:C:1.2", "slack"),
        ("chat-1-2", "other"),
        (None, "other"),
    ],
)
def test_channel_label(key, label):
    assert fu.channel_label(key) == label


def test_ui_control(rec):
    fu.record_ui_control_clicked("option_chip")
    fu.record_ui_control_clicked("<script>")
    assert [c["attrs"] for c in _named(rec, fu.UI_CONTROL_CLICKED)] == [
        {"control": "option_chip"},
        {"control": "other"},
    ]


# -- channel admission --------------------------------------------------------


def _run_channel(monkeypatch, *, admit: bool, automation: bool):
    from kiro_crew.metrics import usage_attrs
    from kiro_crew.shared_crew import turn_gate

    seen: dict = {}

    async def fake_run_admitted(coro, *, refused, **kwargs):
        if admit:
            return await coro
        coro.close()
        refused("capacity")
        return None

    async def run():
        seen["trigger"] = usage_attrs.current_trigger()
        return "ran"

    async def refused(reason):
        return "refused"

    monkeypatch.setattr(turn_gate, "run_admitted", fake_run_admitted)
    result = asyncio.run(
        turn_gate.run_channel_turn(
            session_key="telegram:42",
            text="hi",
            automation=automation,
            run=run,
            refused=refused,
        )
    )
    return result, seen


def test_an_admitted_channel_message_is_counted_and_declares_its_trigger(monkeypatch, rec):
    result, seen = _run_channel(monkeypatch, admit=True, automation=False)
    assert result == "ran"
    assert seen["trigger"] == "user"
    assert [c["attrs"] for c in _named(rec, fu.CHANNEL_MESSAGE_RECEIVED)] == [
        {"channel": "telegram", "admitted": "true", "automation": "false"}
    ]


def test_a_monitor_wake_on_a_channel_is_automation(monkeypatch, rec):
    _, seen = _run_channel(monkeypatch, admit=True, automation=True)
    assert seen["trigger"] == "monitor"
    (call,) = _named(rec, fu.CHANNEL_MESSAGE_RECEIVED)
    assert call["attrs"]["automation"] == "true"


def test_a_refused_channel_message_is_counted_as_not_admitted(monkeypatch, rec):
    result, _ = _run_channel(monkeypatch, admit=False, automation=False)
    assert result == "refused"
    assert [c["attrs"] for c in _named(rec, fu.CHANNEL_MESSAGE_RECEIVED)] == [
        {"channel": "telegram", "admitted": "false", "automation": "false"}
    ]


# -- call sites ---------------------------------------------------------------


def _function(path: pathlib.Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _call_lines(fn: ast.AST, name: str) -> list[int]:
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if called == name:
                out.append(n.lineno)
    return out


def test_tool_recall_is_recorded_after_the_receipt_commit():
    fn = _function(SRC / "dashboard/handlers/memory_member.py", "api_memory_recall")
    (rec_line,) = _call_lines(fn, "record_memory_recalled")
    # commit_receipt runs through asyncio.to_thread, so find where it is named.
    (commit_line,) = [
        n.lineno for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "commit_receipt"
    ]
    assert rec_line > commit_line


# -- app tasks ----------------------------------------------------------------


def test_an_app_not_in_the_list_is_private(rec):
    fu.record_app_task("meetings", "ok")
    assert [c["attrs"] for c in _named(rec, fu.APP_TASK_COMPLETED)] == [
        {"app": "private", "outcome": "ok"}
    ]


def test_papyrus_counts_each_finished_compile():
    fn = _function(SRC / "apps/builtins/papyrus/backend/routes.py", "_handle_compile")
    (rec_line,) = _call_lines(fn, "record_app_task")
    (compile_line,) = _call_lines(fn, "compile_project")
    assert rec_line > compile_line
