"""Asking the skill catalog for a chat's scope before that chat's first message.

The dashboard's ``slot_focused`` frame (composer focus, a key press) makes the gateway
ask the loader for the slot's skill scope on a worker thread, so a re-walk that is due
starts while the person types and not beside the first prompt build. Nothing is kept:
every case here that changes a source after the read sees the change in the next build.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from context_composition_contract_helpers import (  # noqa: F401 -- the fixture is requested by name
    _Rig,
    rig_fixture,
)
from skills_helpers import _settled

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, await_until

pytestmark = [pytest.mark.usefixtures("ample_host_resources", "close_skills_loaders")]


@pytest.fixture(name="mod")
def mod_fixture(monkeypatch: pytest.MonkeyPatch) -> Any:
    from kiro_crew.dashboard import skill_read_ahead

    monkeypatch.setattr(skill_read_ahead, "_started", {})
    monkeypatch.setattr(skill_read_ahead, "_in_flight", set())
    return skill_read_ahead


class _Loader:
    """The loader's one read, recorded; ``hold`` keeps a read on its worker thread."""

    def __init__(self, *, hold: bool = False, fail: bool = False) -> None:
        self.calls: list[tuple[Any, Any, int]] = []
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self._hold = hold
        self._fail = fail
        self._loop = asyncio.get_running_loop()

    def scoped_skills(self, *, project_dir: Any, only: Any) -> list[Any]:
        self.calls.append((project_dir, only, threading.get_ident()))
        self._loop.call_soon_threadsafe(self.entered.set)
        if self._hold:
            assert self.release.wait(FALLBACK_TIMEOUT_SECS), "the test never released the read"
        if self._fail:
            raise RuntimeError("the skills tree could not be read")
        return []


class _OnlySkills:
    """A context builder that fails the test if anything but its loader is touched."""

    def __init__(self, skills: _Loader) -> None:
        self.skills = skills

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the read ahead touched context_builder.{name}")


def _state(
    loader: _Loader | None,
    *,
    project: str = "",
    running: bool = False,
    live: bool = False,
    fresh: bool = False,
) -> SimpleNamespace:
    slot = SimpleNamespace(key="s1", running=running, project=project, linked_session_key="")
    sessions = SimpleNamespace(
        has_session=lambda key: live, fresh_start_unclaimed=lambda key: fresh
    )
    return SimpleNamespace(
        get_slot=lambda key: slot if key == "s1" else None,
        context_builder=None if loader is None else _OnlySkills(loader),
        sessions=sessions,
        _background_tasks=set(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("project", "asked"), [("", None), ("/repo", "/repo")])
async def test_a_frame_reads_the_slots_scope_on_a_worker_thread(
    mod: Any, project: str, asked: str | None
) -> None:
    loader = _Loader()
    state = _state(loader, project=project)

    task = mod.read_ahead(state, "s1")
    assert task in state._background_tasks
    await await_until(task, what="the read ahead did not end")
    await async_wait_until(lambda: not state._background_tasks)

    assert [(p, o) for p, o, _ in loader.calls] == [(asked, None)]
    assert loader.calls[0][2] != threading.get_ident()
    assert mod._in_flight == set()


@pytest.mark.asyncio
async def test_a_frame_during_a_read_starts_no_second_read(mod: Any) -> None:
    loader = _Loader(hold=True)
    state = _state(loader)

    task = mod.read_ahead(state, "s1")
    await await_until(loader.entered, what="the read never reached the loader")
    assert mod.read_ahead(state, "s1") is None

    loader.release.set()
    await await_until(task, what="the read ahead did not end")
    assert len(loader.calls) == 1


@pytest.mark.asyncio
async def test_a_scope_is_read_again_only_after_the_floor(mod: Any) -> None:
    loader = _Loader()
    state = _state(loader)

    await await_until(mod.read_ahead(state, "s1"), what="the first read did not end")
    assert mod.read_ahead(state, "s1") is None

    mod._started[""] -= mod.MIN_INTERVAL_SECS
    await await_until(mod.read_ahead(state, "s1"), what="the second read did not end")
    assert len(loader.calls) == 2


@pytest.mark.asyncio
async def test_a_started_session_no_turn_has_claimed_is_read_for(mod: Any) -> None:
    loader = _Loader()

    await await_until(
        mod.read_ahead(_state(loader, live=True, fresh=True), "s1"),
        what="the read ahead did not end",
    )
    assert len(loader.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "key"),
    [
        ({"live": True, "fresh": False}, "s1"),
        ({"running": True}, "s1"),
        ({}, "unknown"),
        ({}, None),
        ({}, 7),
    ],
)
async def test_a_slot_with_no_session_start_ahead_reads_nothing(
    mod: Any, kwargs: dict[str, bool], key: object
) -> None:
    loader = _Loader()
    state = _state(loader, **kwargs)

    assert mod.read_ahead(state, key) is None
    assert loader.calls == []
    assert state._background_tasks == set()


@pytest.mark.asyncio
async def test_a_gateway_with_no_context_builder_reads_nothing(mod: Any) -> None:
    assert mod.read_ahead(_state(None), "s1") is None


@pytest.mark.asyncio
async def test_a_failed_read_is_dropped_and_the_scope_can_be_read_again(mod: Any) -> None:
    loader = _Loader(fail=True)
    state = _state(loader)

    await await_until(mod.read_ahead(state, "s1"), what="the failed read did not end")
    assert mod._in_flight == set()

    mod._started[""] -= mod.MIN_INTERVAL_SECS
    await await_until(mod.read_ahead(state, "s1"), what="the second read did not end")
    assert len(loader.calls) == 2


@pytest.mark.asyncio
async def test_the_scopes_remembered_are_bounded(mod: Any) -> None:
    loader = _Loader()
    tasks = [
        mod.read_ahead(_state(loader, project=f"/repo-{i}"), "s1")
        for i in range(mod._MAX_SCOPES + 3)
    ]

    await await_until(asyncio.gather(*tasks), what="the reads did not end")
    assert len(mod._started) == mod._MAX_SCOPES
    assert "/repo-0" not in mod._started


def test_only_the_owners_focus_frame_starts_a_read() -> None:
    from kiro_crew.dashboard import ws

    lines = inspect.getsource(ws._serve_ws).splitlines()
    calls = [i for i, line in enumerate(lines) if "read_skills_ahead(" in line]
    assert len(calls) == 1
    indent = len(lines[calls[0]]) - len(lines[calls[0]].lstrip())
    guard = next(
        line.strip()
        for line in reversed(lines[: calls[0]])
        if line.strip() and len(line) - len(line.lstrip()) < indent
    )
    assert guard == "if owner_request:"


def _build(rig: _Rig, key: str, **kwargs: Any) -> str:
    message, _hook = rig.builder.build_message(
        "Fix today's task", True, session_key=f"dashboard:{key}", **kwargs
    )
    return message


def _add_a_skill(rig: _Rig, tmp: Path) -> dict[str, Any]:
    assert rig.skills.create_skill("later", "---\nname: later\ndescription: LATER-SKILL\n---\nb\n")
    _settled(rig.skills)
    return {}


def _write_memory(rig: _Rig, tmp: Path) -> dict[str, Any]:
    rig.seed_memory()
    return {}


def _change_config(rig: _Rig, tmp: Path) -> dict[str, Any]:
    rig.cfg.dashboard.verbosity = "concise"
    return {}


def _edit_the_agent_prompt(rig: _Rig, tmp: Path) -> dict[str, Any]:
    (tmp / "prompt.md").write_text("A CHANGED-CONTRACT.", encoding="utf-8", newline="\n")
    return {}


def _edit_a_steering_file(rig: _Rig, tmp: Path) -> dict[str, Any]:
    (tmp / "standards" / "coding.md").write_text("# Standards\nRULE-TWO\n", encoding="utf-8")
    return {"steering_dirs": (str(tmp / "standards"),)}


@pytest.mark.parametrize(
    ("change", "marker"),
    [
        (_add_a_skill, "LATER-SKILL"),
        (_write_memory, "Always keep safeguards"),
        (_change_config, "[RESPONSE PREFERENCES"),
        (_edit_the_agent_prompt, "CHANGED-CONTRACT"),
        (_edit_a_steering_file, "RULE-TWO"),
    ],
    ids=["skill", "memory", "config", "agent-prompt", "steering"],
)
def test_a_change_made_after_the_read_reaches_the_next_build(
    rig: _Rig, mod: Any, change: Callable[[_Rig, Path], dict[str, Any]], marker: str
) -> None:
    (rig.tmp / "standards").mkdir()
    (rig.tmp / "standards" / "coding.md").write_text("# Standards\nRULE-ONE\n", encoding="utf-8")
    steering = {"steering_dirs": (str(rig.tmp / "standards"),)}
    _settled(rig.skills)

    mod.read_scope(rig.skills, "")
    assert marker not in _build(rig, "before", **steering)

    change(rig, rig.tmp)
    assert marker in _build(rig, "after", **steering)
