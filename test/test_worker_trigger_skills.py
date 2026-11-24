"""``session_create(skills=...)``: a conductor chooses which skills trigger in a worker.

Pinned here: the list turns per-message trigger matching on for a worker and
narrows it to the listed keys, before the ``skills.max_triggered`` cap and within
the session's own scope; a session with no list matches exactly as before; a
conductor or any other agent never takes one; the create route refuses an unknown
key, naming it, before anything is allocated; the list rides the birth metadata
line and comes back on restore; ``$key`` keeps the worker's whole scope; and the
MCP tools forward it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew import agent_discovery, context, worker_trigger_skills
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.worker_trigger_skills import (
    TriggerSkillsRefused,
    listed_for,
    parse_trigger_skills,
    restored_trigger_skills,
)

WORKER = "kirocrew-worker"
MATE_WORKER = "kirocrew-mate-worker"


def _skills_tree(root, **triggers: str) -> SkillsLoader:
    """A loader over one skill per keyword, each with the given ``triggers:`` line."""
    for name, trigger in triggers.items():
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\ntriggers: {trigger}\n---\n\n{name} body\n",
            encoding="utf-8",
        )
    return SkillsLoader(skills_path=root, install_builtins=False)


# ── the list itself ──────────────────────────────────────────────────


def test_a_list_parses_in_order_without_repeats():
    assert parse_trigger_skills(["b", "a", "b"]) == ("b", "a")
    assert parse_trigger_skills(None) is None


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ([], "invalid_skills"),
        ("a", "invalid_field_type"),
        (["a", 1], "invalid_field_type"),
        ([f"k{i}" for i in range(worker_trigger_skills.MAX_TRIGGER_SKILLS + 1)], "invalid_skills"),
        (["x" * (worker_trigger_skills.MAX_TRIGGER_SKILL_KEY_LEN + 1)], "invalid_skills"),
    ],
    ids=["empty", "not-a-list", "not-strings", "too-many", "too-long"],
)
def test_a_malformed_list_is_refused_with_its_code(raw, code):
    with pytest.raises(TriggerSkillsRefused) as exc:
        parse_trigger_skills(raw)
    assert exc.value.code == code


def test_a_malformed_line_reads_as_no_list():
    assert restored_trigger_skills(["a"]) == ("a",)
    assert restored_trigger_skills("a") is None
    assert restored_trigger_skills([]) is None


@pytest.mark.parametrize(
    ("agent", "applies"),
    [
        (WORKER, True),
        (MATE_WORKER, True),
        ("kirocrew-conductor", False),
        ("kirocrew-mate-conductor", False),
        ("kirocrew", False),
        ("code-reviewer", False),
        (None, False),
    ],
)
def test_only_the_two_workers_take_a_list(agent, applies):
    assert (listed_for(agent, ("a",)) == ("a",)) is applies
    assert listed_for(agent, None) is None


# ── the turn ─────────────────────────────────────────────────────────


def _turn(tmp_path, monkeypatch, *, agent, trigger_skills, scope=None, invoke_select=False):
    """Run one turn whose text matches ``alpha`` and ``beta``.

    Returns the loader (to see what matching was asked) and the delivered keys, or
    ``None`` when nothing was delivered. *scope* is what the session's own scope
    reader answers: ``None`` is the whole catalog, an exception refuses.
    """

    def fake_scope(session_key, agent, *, project_dir=None, provider_type=None):
        if isinstance(scope, Exception):
            raise scope
        return scope

    monkeypatch.setattr(context, "pinned_skill_globs", lambda *_a, **_k: None)
    monkeypatch.setattr(context, "agent_skill_globs", lambda *_a, **_k: [])
    monkeypatch.setattr(agent_discovery, "session_skill_globs", fake_scope)

    loader = MagicMock()

    def triggered(*_args, only=None, select=None, **_kwargs):
        if invoke_select and select is not None:
            select()
        return [key for key in ("alpha", "beta") if only is None or key in only]

    loader.get_triggered_skills.side_effect = triggered
    loader.scoped_skills.side_effect = lambda *, project_dir=None, only=None: [
        {"key": key} for key in only or ()
    ]
    loader.split_triggered.side_effect = lambda triggered, _project: (list(triggered), [])
    loader.load_skill.return_value = "body"
    loader.strip_frontmatter.return_value = "body"
    loader.trigger_hint.return_value = ""
    builder = ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=loader)

    builder.build_message(
        "trigger",
        is_new_session=False,
        session_key="dashboard:w",
        agent=agent,
        trigger_skills=trigger_skills,
    )
    delivered = loader.split_triggered.call_args.args[0] if loader.split_triggered.called else None
    return loader, delivered


def test_a_list_turns_matching_on_for_the_conductor_mode_worker(tmp_path, monkeypatch):
    loader, delivered = _turn(tmp_path, monkeypatch, agent=WORKER, trigger_skills=("beta",))

    assert loader.get_triggered_skills.call_args.kwargs["only"] == ("beta",)
    assert delivered == ["beta"]


def test_without_a_list_the_conductor_mode_worker_matches_nothing(tmp_path, monkeypatch):
    loader, delivered = _turn(tmp_path, monkeypatch, agent=WORKER, trigger_skills=None)

    loader.get_triggered_skills.assert_not_called()
    assert delivered is None


def test_without_a_list_a_bots_worker_matches_as_before(tmp_path, monkeypatch):
    loader, delivered = _turn(tmp_path, monkeypatch, agent=MATE_WORKER, trigger_skills=None)

    assert loader.get_triggered_skills.call_args.kwargs["only"] is None
    assert delivered == ["alpha", "beta"]


def test_a_list_narrows_a_bots_worker(tmp_path, monkeypatch):
    _loader, delivered = _turn(tmp_path, monkeypatch, agent=MATE_WORKER, trigger_skills=("alpha",))

    assert delivered == ["alpha"]


def test_a_list_narrows_the_oracle_menu_before_selection(tmp_path, monkeypatch):
    from kiro_crew.decisions.points import skills_select

    seen = []

    def select(*_args, only=None, **_kwargs):
        seen.append(only)
        return None

    monkeypatch.setattr(skills_select, "selected_skills", select)

    _turn(
        tmp_path,
        monkeypatch,
        agent=MATE_WORKER,
        trigger_skills=("beta",),
        invoke_select=True,
    )

    assert seen == [("beta",)]


def test_a_conductor_carrying_a_list_still_matches_nothing(tmp_path, monkeypatch):
    loader, delivered = _turn(
        tmp_path, monkeypatch, agent="kirocrew-conductor", trigger_skills=("alpha",)
    )

    loader.get_triggered_skills.assert_not_called()
    assert delivered is None


def test_the_list_is_held_to_the_sessions_own_scope(tmp_path, monkeypatch):
    """A key added to the line by hand cannot reach past what skill_search reaches."""
    loader, delivered = _turn(
        tmp_path, monkeypatch, agent=WORKER, trigger_skills=("alpha", "beta"), scope=["alpha"]
    )

    assert loader.get_triggered_skills.call_args.kwargs["only"] == (
        "alpha",
    ), "cut before the match, so the trigger audit row names only what is injected"
    assert delivered == ["alpha"]


def test_an_unreadable_scope_triggers_nothing(tmp_path, monkeypatch):
    _loader, delivered = _turn(
        tmp_path,
        monkeypatch,
        agent=WORKER,
        trigger_skills=("alpha",),
        scope=agent_discovery.SkillScopeResolutionError("unreadable"),
    )

    assert delivered is None


def test_the_list_narrows_before_the_cap(tmp_path, monkeypatch):
    """With room for one match, a listed skill is not crowded out by an unlisted one."""
    loader = _skills_tree(tmp_path / "skills", alpha="upload fix now", beta="upload fix")
    monkeypatch.setattr(loader, "_max_triggered_now", lambda: 1)

    assert loader.get_triggered_skills("upload fix now") == ["alpha"]
    assert loader.get_triggered_skills("upload fix now", only=("beta",)) == ["beta"]


# ── the create route ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _worker_template(_floor_monkeypatch):
    """Resolve the worker name as the shared template, as an installed gateway does."""
    from pathlib import Path

    from kiro_crew.config.sections import ResolvedBindings

    real = sc.resolve_agent_bindings

    def resolve(cfg, requested, *args, **kwargs):
        if requested != WORKER:
            return real(cfg, requested, *args, **kwargs)
        return ResolvedBindings(
            workspace_dir=Path("workspace"),
            memory_store_name="default",
            effective_memory_config={},
            kiro_agent=WORKER,
            selection_kind="template",
            resolved_alias=WORKER,
        )

    _floor_monkeypatch.setattr(sc, "resolve_agent_bindings", resolve)


def _state(tmp_path):
    state = _make_state(tmp_path)
    state.context_builder = SimpleNamespace(
        skills=_skills_tree(tmp_path / "skills", alpha="alpha words", beta="beta words")
    )
    return state


def _create(state, caller, **kwargs):
    return asyncio.run(
        sc.create_session(
            state, caller_session_key=_key(caller), agent=WORKER, agent_kind="template", **kwargs
        )
    )


def test_create_keeps_the_list_on_the_slot_and_the_birth_line(tmp_path):
    state = _state(tmp_path)
    caller = _slot(state, "chat-1")

    created = _create(state, caller, trigger_skills=["beta"])

    child = state.get_slot(created["target"])
    assert child is not None
    assert child.trigger_skills == ("beta",)
    assert created["skills"] == ["beta"]
    line = state.conversation_log.get_metadata(slot_history_key(child))
    assert line.get("trigger_skills") == ["beta"]


def test_create_without_a_list_writes_none(tmp_path):
    state = _state(tmp_path)
    caller = _slot(state, "chat-1")

    created = _create(state, caller)

    child = state.get_slot(created["target"])
    assert child is not None and child.trigger_skills is None
    assert "skills" not in created
    assert "trigger_skills" not in state.conversation_log.get_metadata(slot_history_key(child))


@pytest.mark.parametrize("dry_run", [False, True], ids=["create", "dry-run"])
def test_an_unknown_key_is_refused_by_name_and_nothing_is_made(tmp_path, dry_run):
    state = _state(tmp_path)
    caller = _slot(state, "chat-1")
    before = state.live_slot_count()

    with pytest.raises(sc.SessionControlError) as exc:
        _create(state, caller, trigger_skills=["beta", "no-such-skill"], dry_run=dry_run)

    assert exc.value.code == "unknown_skills"
    assert "'no-such-skill'" in str(exc.value) and "'beta'" not in str(exc.value)
    assert state.live_slot_count() == before


@pytest.mark.parametrize("dry_run", [False, True], ids=["create", "dry-run"])
def test_a_building_catalog_is_retryable_and_nothing_is_made(tmp_path, monkeypatch, dry_run):
    state = _state(tmp_path)
    caller = _slot(state, "chat-1")
    loader = state.context_builder.skills
    monkeypatch.setattr(loader, "scoped_skills", lambda **_kwargs: [])
    monkeypatch.setattr(loader, "catalog_status", lambda _project_dir=None: "building")
    before = state.live_slot_count()

    with pytest.raises(sc.SessionControlError) as exc:
        _create(state, caller, trigger_skills=["beta"], dry_run=dry_run)

    assert exc.value.code == "skills_catalog_building"
    assert exc.value.status == 503
    assert "still loading" in str(exc.value) and "Retry shortly" in str(exc.value)
    assert "Nothing was created" in str(exc.value)
    assert state.live_slot_count() == before


def test_unknown_keys_does_not_report_a_key_unknown_while_catalog_builds(tmp_path, monkeypatch):
    loader = _skills_tree(tmp_path / "skills", alpha="alpha words")
    monkeypatch.setattr(loader, "scoped_skills", lambda **_kwargs: [])
    monkeypatch.setattr(loader, "catalog_status", lambda _project_dir=None: "building")

    with pytest.raises(TriggerSkillsRefused) as exc:
        worker_trigger_skills.unknown_keys(
            ["alpha"],
            loader,
            template=WORKER,
            member_id=None,
            project_dir=None,
            provider_type=None,
        )

    assert exc.value.code == "skills_catalog_building"


def test_a_list_for_any_other_agent_is_refused(tmp_path):
    state = _state(tmp_path)
    caller = _slot(state, "chat-1")

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(
                state, caller_session_key=_key(caller), agent="kirocrew", trigger_skills=["beta"]
            )
        )

    assert exc.value.code == "skills_not_for_agent"


def test_the_list_survives_a_restart(tmp_path):
    """Restore reads the birth line back onto a fresh slot."""
    state = _state(tmp_path)
    caller = _slot(state, "chat-1")
    created = _create(state, caller, trigger_skills=["alpha", "beta"])
    line = state.conversation_log.get_metadata(slot_history_key(state.get_slot(created["target"])))

    fresh = _make_state(tmp_path).get_or_create_slot("restored")
    codec._read_trigger_skills(SimpleNamespace(meta=line, slot=fresh))

    assert fresh.trigger_skills == ("alpha", "beta")


def test_a_bots_worker_is_checked_against_its_own_kit(tmp_path):
    """A Bot's worker has no session yet at create, so its scope is read by member id."""
    import json

    from kiro_crew.config import paths

    loader = _skills_tree(tmp_path / "skills", alpha="alpha words", beta="beta words")
    mates = paths.mate_agents_dir()
    mates.mkdir(parents=True, exist_ok=True)
    mapping = f"skill://{tmp_path / 'skills' / 'alpha' / 'SKILL.md'}"
    (mates / f"{MATE_WORKER}.json").write_text(
        json.dumps({"name": MATE_WORKER, "prompt": "", "resources": [mapping]}), encoding="utf-8"
    )

    unknown = worker_trigger_skills.unknown_keys(
        ["alpha", "beta"],
        loader,
        template=MATE_WORKER,
        member_id="bot-1",
        project_dir=None,
        provider_type=None,
    )

    assert unknown == ["beta"]


# ── what the list does not narrow ────────────────────────────────────


def test_dollar_key_keeps_the_workers_whole_scope(tmp_path, monkeypatch):
    """``$beta`` loads in a worker whose trigger list names only alpha."""
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_turn import prompt_assembly

    state = _state(tmp_path)
    slot = state.get_or_create_slot("w")
    slot.agent = WORKER
    slot.trigger_skills = ("alpha",)
    scope_calls = []

    def whole_catalog(*args, **kwargs):
        scope_calls.append(args)
        return None

    monkeypatch.setattr(chat_runner, "session_skill_globs", whole_catalog)

    expanded, count = prompt_assembly._expand_dollar_skills(
        "use $beta", state, slot, "dashboard:w", ""
    )

    assert count == 1 and "beta body" in expanded
    assert scope_calls, "the $key path reads the session scope, never the trigger list"


# ── the MCP tools ────────────────────────────────────────────────────


def _call(tool: str, args: dict, reply: dict):
    from kiro_crew.mcp_dashboard import TABLE
    from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
    from kiro_crew.mcp_tools.table import Caller, ToolContext

    dash = InMemoryDashboardClient(
        {
            "GET /api/chat/slots": [{"key": "chat-1-100", "title": "Caller"}],
            "GET /api/chat/folders": [],
            "POST /api/session-control/create": lambda req: reply,
            "POST /api/work-ledger/record": lambda req: {"item": {"item_id": "it_1"}},
            "POST /api/session-control/send": lambda req: {"ok": True},
        }
    )
    out = TABLE.call(tool, args, ToolContext(dash, Caller.strict("dashboard:chat-1-100")))
    creates = [r.body for r in dash.requests if r.path == "/api/session-control/create"]
    return out, creates


def test_session_create_forwards_the_list_and_names_it():
    out, creates = _call(
        "session_create",
        {"title": "w", "agent": WORKER, "skills": ["beta"]},
        {"ok": True, "target": "chat-9", "title": "w", "skills": ["beta"]},
    )

    assert creates[-1]["skills"] == ["beta"]
    assert "triggering only `beta`" in out


def test_work_dispatch_forwards_the_list():
    _out, creates = _call(
        "work_dispatch",
        {
            "title": "item",
            "acceptance": {"kind": "human_approval"},
            "agent": WORKER,
            "seed": "go",
            "skills": ["beta"],
        },
        {"ok": True, "target": "chat-9", "title": "item"},
    )

    assert creates and creates[-1]["skills"] == ["beta"]
