"""A skill that two heartbeat tasks of one cycle match reaches the session once.

Every task of a heartbeat cycle runs on the one ``HEARTBEAT_KEY`` session, and
the cycle-end recycle starts the next cycle on a fresh one. ``_heartbeat_task``
builds each prompt without a session key, so it names the session in
``skill_bodies_session``: the second match in a cycle sends the pointer line,
the next cycle sends the body again, and a task whose prompt never landed rolls
its record back so the next task still gets the body. The settle runs before
the session is released, because the next task builds on the same session. A
compaction the backend reports during a task arms the session's one-shot
reinjection flag, so the next task sends the body and the session-start context
again.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.slack.gateway as gw_mod
from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.session import HEARTBEAT_KEY
from kiro_crew.skills import SkillsLoader

BODY_SENTINEL = "STEP ONE: pour the concrete before the rebar."
HINT_HEADER = "[Relevant skills for this message]"
REINJECTED_HEADER = "[REINJECTED AFTER COMPACTION"
TASK = "zebra quokka: check the enclosure"


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Every test builds a ``SkillsLoader``: close it so its ``skill-catalog-refresh`` thread does not leak across teardown (``test/conftest.py``)."""


def _builder(tmp_path: Path) -> ContextBuilder:
    skill_dir = tmp_path / "skills" / "foundation"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: foundation\ndescription: Lay a foundation\n"
        f"triggers: zebra quokka\n---\n{BODY_SENTINEL}",
        encoding="utf-8",
    )
    loader = SkillsLoader(
        skills_path=tmp_path / "skills",
        install_builtins=False,
        config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
    )
    return ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=loader)


class _ReinjectionFlag:
    """The one-shot flag ``SessionManager`` keeps on the live heartbeat session."""

    def __init__(self) -> None:
        self.armed = False

    def mark(self, _key: str) -> None:
        self.armed = True

    def consume(self, _key: str) -> bool:
        armed, self.armed = self.armed, False
        return armed


def _orchestrator(builder: ContextBuilder, fresh_sessions: list[bool]) -> tuple[Any, MagicMock]:
    """A gateway whose heartbeat session is fresh or warm per task, in order."""
    orch = gw_mod.GatewayOrchestrator.__new__(gw_mod.GatewayOrchestrator)
    sessions = MagicMock()
    client = MagicMock()
    sessions.get_or_create = AsyncMock(
        side_effect=[(client, fresh, False) for fresh in fresh_sessions]
    )
    sessions.reset = AsyncMock()
    sessions.release = MagicMock()
    flag = _ReinjectionFlag()
    sessions.mark_needs_reinjection = MagicMock(side_effect=flag.mark)
    sessions.consume_needs_reinjection = MagicMock(side_effect=flag.consume)
    orch.sessions = sessions
    orch.ctx_builder = builder
    orch.consolidator = None
    orch._heartbeat_approval = AsyncMock(return_value=True)
    orch._deliver_result = AsyncMock()
    return orch, sessions


def _stream_recording(
    sent: list[str],
    stop_reasons: list[str | None],
    compactions: list[str | None] | None = None,
) -> Any:
    """Record each streamed prompt and complete with the next stop reason.

    ``None`` ends the stream with no completion, which never counts as landed.
    A *compactions* entry is the compaction status the backend reports during
    that prompt's turn, or ``None`` for no compaction.
    """
    pending = list(stop_reasons)
    statuses = list(compactions) if compactions is not None else [None] * len(stop_reasons)

    async def _stream(
        _client: Any,
        message: str,
        *,
        on_complete: Any = None,
        on_compaction: Any = None,
        **_kw: Any,
    ) -> str:
        sent.append(message)
        status = statuses.pop(0)
        if status is not None and on_compaction is not None:
            on_compaction(SimpleNamespace(text=status))
        stop_reason = pending.pop(0)
        if stop_reason is not None and on_complete is not None:
            on_complete(SimpleNamespace(stop_reason=stop_reason))
        return "done"

    return _stream


async def _heartbeat_task(orch: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run ``_init_heartbeat`` with the service start stubbed; return its task callback."""
    started: dict[str, Any] = {}

    async def _fake_start(self: Any) -> None:
        started["on_task"] = self._on_task

    monkeypatch.setattr(gw_mod.HeartbeatService, "start", _fake_start)
    monkeypatch.setattr(gw_mod, "_persist_turn_row", AsyncMock())
    await orch._init_heartbeat()
    return started["on_task"]


class TestOneCycle:
    @pytest.mark.asyncio()
    async def test_a_skill_two_tasks_match_reaches_the_session_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch, _sessions = _orchestrator(_builder(tmp_path), fresh_sessions=[True, False])
        sent: list[str] = []
        monkeypatch.setattr(
            gw_mod, "stream_and_collect", _stream_recording(sent, ["end_turn", "end_turn"])
        )
        on_task = await _heartbeat_task(orch, monkeypatch)

        await on_task(TASK, "")
        await on_task(TASK, "")

        assert BODY_SENTINEL in sent[0]
        assert BODY_SENTINEL not in sent[1]
        assert HINT_HEADER in sent[1]

    @pytest.mark.asyncio()
    async def test_the_next_cycle_sends_the_body_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The third task opens the next cycle's fresh session.
        orch, _sessions = _orchestrator(_builder(tmp_path), fresh_sessions=[True, False, True])
        sent: list[str] = []
        monkeypatch.setattr(gw_mod, "stream_and_collect", _stream_recording(sent, ["end_turn"] * 3))
        on_task = await _heartbeat_task(orch, monkeypatch)

        for _ in range(3):
            await on_task(TASK, "")

        assert BODY_SENTINEL not in sent[1]
        assert BODY_SENTINEL in sent[2]

    @pytest.mark.asyncio()
    async def test_a_cycle_that_opens_on_an_unmatched_task_still_starts_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cycle 1 sends the body. Cycle 2 opens its fresh session on a task that
        # matches no skill, then a task that does: that session never held it.
        orch, _sessions = _orchestrator(_builder(tmp_path), fresh_sessions=[True, True, False])
        sent: list[str] = []
        monkeypatch.setattr(gw_mod, "stream_and_collect", _stream_recording(sent, ["end_turn"] * 3))
        on_task = await _heartbeat_task(orch, monkeypatch)

        await on_task(TASK, "")
        await on_task("check the water level", "")
        await on_task(TASK, "")

        assert BODY_SENTINEL not in sent[1]
        assert BODY_SENTINEL in sent[2]

    @pytest.mark.asyncio()
    async def test_a_task_that_never_landed_leaves_the_body_for_the_next(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch, _sessions = _orchestrator(_builder(tmp_path), fresh_sessions=[True, False])
        sent: list[str] = []
        monkeypatch.setattr(
            gw_mod, "stream_and_collect", _stream_recording(sent, [None, "end_turn"])
        )
        on_task = await _heartbeat_task(orch, monkeypatch)

        await on_task(TASK, "")
        await on_task(TASK, "")

        assert BODY_SENTINEL in sent[1]


class TestSettleOrder:
    @pytest.mark.asyncio()
    async def test_the_record_is_settled_before_the_session_is_released(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        builder = _builder(tmp_path)
        orch, sessions = _orchestrator(builder, fresh_sessions=[True])
        events: list[tuple[str, str | None]] = []
        commit = builder.commit_skill_bodies

        def _recording_commit(session_key: str | None) -> None:
            events.append(("commit", session_key))
            commit(session_key)

        monkeypatch.setattr(builder, "commit_skill_bodies", _recording_commit)
        sessions.release = MagicMock(side_effect=lambda key: events.append(("release", key)))
        monkeypatch.setattr(gw_mod, "stream_and_collect", _stream_recording([], ["end_turn"]))
        on_task = await _heartbeat_task(orch, monkeypatch)

        await on_task(TASK, "")

        assert events == [("commit", HEARTBEAT_KEY), ("release", HEARTBEAT_KEY)]

    @pytest.mark.asyncio()
    async def test_a_compaction_arms_the_flag_before_the_session_is_released(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch, sessions = _orchestrator(_builder(tmp_path), fresh_sessions=[True])
        events: list[tuple[str, str]] = []
        sessions.mark_needs_reinjection = MagicMock(
            side_effect=lambda key: events.append(("arm", key))
        )
        sessions.release = MagicMock(side_effect=lambda key: events.append(("release", key)))
        monkeypatch.setattr(
            gw_mod,
            "stream_and_collect",
            _stream_recording([], ["end_turn"], compactions=["completed"]),
        )
        on_task = await _heartbeat_task(orch, monkeypatch)

        await on_task(TASK, "")

        assert events == [("arm", HEARTBEAT_KEY), ("release", HEARTBEAT_KEY)]


class TestCompactionMidCycle:
    @pytest.mark.asyncio()
    async def test_the_task_after_a_compaction_sends_the_body_again_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch, _sessions = _orchestrator(_builder(tmp_path), fresh_sessions=[True, False, False])
        sent: list[str] = []
        monkeypatch.setattr(
            gw_mod,
            "stream_and_collect",
            _stream_recording(sent, ["end_turn"] * 3, compactions=["completed", None, None]),
        )
        on_task = await _heartbeat_task(orch, monkeypatch)

        for _ in range(3):
            await on_task(TASK, "")

        assert BODY_SENTINEL in sent[1]
        assert REINJECTED_HEADER in sent[1]
        assert BODY_SENTINEL not in sent[2]
        assert REINJECTED_HEADER not in sent[2]

    @pytest.mark.asyncio()
    async def test_a_failed_compaction_leaves_the_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch, _sessions = _orchestrator(_builder(tmp_path), fresh_sessions=[True, False])
        sent: list[str] = []
        monkeypatch.setattr(
            gw_mod,
            "stream_and_collect",
            _stream_recording(sent, ["end_turn"] * 2, compactions=["failed", None]),
        )
        on_task = await _heartbeat_task(orch, monkeypatch)

        await on_task(TASK, "")
        await on_task(TASK, "")

        assert BODY_SENTINEL not in sent[1]
        assert REINJECTED_HEADER not in sent[1]

    @pytest.mark.asyncio()
    async def test_a_reinjection_that_never_landed_passes_to_the_next_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch, _sessions = _orchestrator(_builder(tmp_path), fresh_sessions=[True, False, False])
        sent: list[str] = []
        monkeypatch.setattr(
            gw_mod,
            "stream_and_collect",
            _stream_recording(
                sent, ["end_turn", None, "end_turn"], compactions=["completed", None, None]
            ),
        )
        on_task = await _heartbeat_task(orch, monkeypatch)

        for _ in range(3):
            await on_task(TASK, "")

        assert REINJECTED_HEADER in sent[1]
        assert REINJECTED_HEADER in sent[2]
