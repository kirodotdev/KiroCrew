"""A sub-agent run with no label is named from its task, and the name is shown.

``spawn_run``'s ``label`` names a run on every surface that lists it. When the
caller gives none, the dashboard's ``subagent_spawn`` handler asks the titler
that names chat sessions for a name, sets it on the run while the run still has
none, and announces it with a ``subagent_label`` frame. The parent agent reads
the same name from ``spawn_list``, ``spawn_status`` and the completion event, so
the person and the agent can refer to a run by it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from config_loader_helpers import _load_from_dict
from test_gateway_subagent_wiring import _LOST_RUN_SECS, _Done, harness, make_info

from kiro_crew import mcp_core
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config import live as config_live
from kiro_crew.dashboard import chat_title
from kiro_crew.slack import gateway as gw_mod
from kiro_crew.spawn_label import SPAWN_LABEL_MAX_CHARS
from kiro_crew.subagent import SubagentInfo

#: Opens every task the way an orchestrator's assignments do, and is longer than
#: the titler's per-line budget, so a one-line prompt would carry nothing else.
_BOILERPLATE = "DO NOT SPAWN SUBAGENTS. Do this work yourself directly with tool calls. " * 4
_TASK = f"{_BOILERPLATE}\n\nReview the Refresh context hint fix in the side chat."


@pytest.fixture
def done(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Done]:
    yield from harness(monkeypatch)


def _running(agent_id: str = "a1", *, task: str = _TASK, parent: str = "dashboard:main") -> Any:
    return SubagentInfo(id=agent_id, task=task, parent_session_key=parent)


async def _spawn(done: _Done, info: SubagentInfo) -> None:
    """Deliver *info*'s ``subagent_spawn`` and wait for the naming it starts."""
    before = len(done.state._background_tasks.added)
    await done.manager_kwargs["on_event"]("subagent_spawn", info, {"task": info.task})
    await asyncio.wait_for(
        asyncio.gather(*done.state._background_tasks.added[before:]), _LOST_RUN_SECS
    )


def _titler(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> AsyncMock:
    fake = AsyncMock(**kwargs)
    monkeypatch.setattr(chat_title, "title_for_text", fake)
    return fake


# ── the titling call ─────────────────────────────────────────────────────────


class TestTitleForText:
    @pytest.fixture(autouse=True)
    def _no_language(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(chat_title, "_ui_language", lambda: "")

    @pytest.mark.asyncio
    async def test_each_paragraph_is_its_own_line_so_the_tail_reaches_the_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prompts: list[str] = []

        async def _oneliner(_sessions: Any, prompt: str, **_kw: Any) -> str:
            prompts.append(prompt)
            return "Refresh hint fix review"

        monkeypatch.setattr(chat_title, "run_bg_oneliner", _oneliner)

        name = await chat_title.title_for_text(SimpleNamespace(sessions=None), _TASK)

        assert name == "Refresh hint fix review"
        assert "user: Review the Refresh context hint fix in the side chat." in prompts[0]

    def test_the_whole_task_as_one_line_would_hide_the_tail(self) -> None:
        prompt = chat_title._build_title_prompt([{"role": "user", "content": _TASK}])

        assert prompt is not None
        assert "Refresh context hint" not in prompt

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reply", ["SKIP", "I cannot name this task. It is unclear."])
    async def test_a_skip_or_a_sentence_names_nothing(
        self, monkeypatch: pytest.MonkeyPatch, reply: str
    ) -> None:
        monkeypatch.setattr(chat_title, "run_bg_oneliner", AsyncMock(return_value=reply))

        assert await chat_title.title_for_text(SimpleNamespace(sessions=None), _TASK) == ""

    @pytest.mark.asyncio
    async def test_blank_text_makes_no_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        oneliner = AsyncMock(return_value="unused")
        monkeypatch.setattr(chat_title, "run_bg_oneliner", oneliner)

        assert await chat_title.title_for_text(SimpleNamespace(sessions=None), " \n\n ") == ""
        oneliner.assert_not_awaited()


# ── naming a run ─────────────────────────────────────────────────────────────


class TestNaming:
    @pytest.mark.asyncio
    async def test_an_unlabelled_run_is_named_and_the_name_is_announced(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        titler = _titler(monkeypatch, return_value="Refresh hint review")
        info = _running()
        done.mgr.all_agents = [info]

        await _spawn(done, info)

        assert info.label == "Refresh hint review"
        assert done.frames("subagent_label") == [
            {"id": "a1", "slot": "evt:dashboard:main", "label": "Refresh hint review"}
        ]
        titler.assert_awaited_once_with(done.state, _TASK, session_key="dashboard:main")

    @pytest.mark.asyncio
    async def test_the_spawn_frame_goes_out_before_the_name(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _titler(monkeypatch, return_value="Refresh hint review")
        info = _running()
        done.mgr.all_agents = [info]

        await _spawn(done, info)

        kinds = [c.args[0] for c in done.state.broadcast_ws.call_args_list]
        assert kinds.index("subagent_spawn") < kinds.index("subagent_label")

    @pytest.mark.asyncio
    async def test_a_wave_member_carries_its_batch(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _titler(monkeypatch, return_value="Refresh hint review")
        info = _running()
        info.batch_id = "w1"
        done.mgr.all_agents = [info]

        await _spawn(done, info)

        assert done.frames("subagent_label") == [
            {
                "id": "a1",
                "slot": "evt:dashboard:main",
                "label": "Refresh hint review",
                "batch_id": "w1",
            }
        ]

    @pytest.mark.asyncio
    async def test_a_callers_label_is_kept_and_no_name_is_asked_for(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        titler = _titler(monkeypatch, return_value="unused")
        info = SubagentInfo(id="a1", task=_TASK, parent_session_key="dashboard:main", label="Mine")

        await _spawn(done, info)

        assert info.label == "Mine"
        titler.assert_not_awaited()
        assert done.frames("subagent_label") == []

    @pytest.mark.asyncio
    async def test_a_label_set_while_the_name_is_asked_for_is_kept(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        info = _running()
        done.mgr.all_agents = [info]

        async def _late_label(*_a: Any, **_kw: Any) -> str:
            info.label = "Set meanwhile"
            return "Generated"

        _titler(monkeypatch, side_effect=_late_label)

        await _spawn(done, info)

        assert info.label == "Set meanwhile"
        assert done.frames("subagent_label") == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", [RuntimeError("backend down"), ""])
    async def test_a_failed_or_empty_answer_leaves_the_run_unnamed(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch, answer: Any
    ) -> None:
        if isinstance(answer, BaseException):
            _titler(monkeypatch, side_effect=answer)
        else:
            _titler(monkeypatch, return_value=answer)
        info = _running()
        done.mgr.all_agents = [info]

        await _spawn(done, info)

        assert info.label == ""
        assert done.frames("subagent_label") == []

    @pytest.mark.asyncio
    async def test_a_name_a_sibling_already_has_is_numbered(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _titler(monkeypatch, return_value="Refresh hint review")
        info = _running("a2")
        sibling = SubagentInfo(
            id="a1", task="x", parent_session_key="dashboard:main", label="refresh hint REVIEW"
        )
        numbered = SubagentInfo(
            id="a0", task="x", parent_session_key="dashboard:main", label="Refresh hint review 2"
        )
        elsewhere = SubagentInfo(
            id="b1", task="x", parent_session_key="dashboard:other", label="Refresh hint review 3"
        )
        done.mgr.all_agents = [sibling, numbered, elsewhere, info]

        await _spawn(done, info)

        assert info.label == "Refresh hint review 3"

    @pytest.mark.asyncio
    async def test_a_numbered_name_stays_within_the_label_limit(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        long_name = "n" * SPAWN_LABEL_MAX_CHARS
        _titler(monkeypatch, return_value=long_name)
        info = _running("a2")
        sibling = SubagentInfo(
            id="a1", task="x", parent_session_key="dashboard:main", label=long_name
        )
        done.mgr.all_agents = [sibling, info]

        await _spawn(done, info)

        assert info.label == "n" * (SPAWN_LABEL_MAX_CHARS - 2) + " 2"

    @pytest.mark.asyncio
    async def test_at_most_two_names_are_asked_for_at_once(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = asyncio.Event()
        active: list[str] = []
        peak = 0

        async def _held(_state: Any, task: str, **_kw: Any) -> str:
            nonlocal peak
            active.append(task)
            peak = max(peak, len(active))
            await release.wait()
            active.remove(task)
            return f"Name for {task}"

        _titler(monkeypatch, side_effect=_held)
        infos = [_running(f"a{i}", task=f"task {i}") for i in range(3)]
        done.mgr.all_agents = infos
        on_event = done.manager_kwargs["on_event"]

        for info in infos:
            await on_event("subagent_spawn", info, {"task": info.task})
        for _ in range(50):
            await asyncio.sleep(0)

        assert len(active) == 2
        release.set()
        await asyncio.wait_for(asyncio.gather(*done.state._background_tasks.added), _LOST_RUN_SECS)
        assert peak == 2
        assert sorted(i.label for i in infos) == [
            "Name for task 0",
            "Name for task 1",
            "Name for task 2",
        ]


# ── turning it off ───────────────────────────────────────────────────────────


def _config(*, auto_names: bool) -> KiroCrewConfig:
    config = KiroCrewConfig()
    config.agent.subagent_auto_names = auto_names
    return config


class TestTheSetting:
    def test_it_is_on_unless_the_config_says_false(self) -> None:
        assert KiroCrewConfig().agent.subagent_auto_names is True
        assert _load_from_dict({}).agent.subagent_auto_names is True
        off = _load_from_dict({"agent": {"subagent_auto_names": False}})
        assert off.agent.subagent_auto_names is False
        assert _load_from_dict(off.to_dict()).agent.subagent_auto_names is False

    @pytest.mark.asyncio
    async def test_off_in_the_live_config_makes_no_call_and_names_nothing(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        titler = _titler(monkeypatch, return_value="Refresh hint review")
        monkeypatch.setattr(config_live, "snapshot", lambda: _config(auto_names=False))
        info = _running()
        done.mgr.all_agents = [info]

        await _spawn(done, info)

        titler.assert_not_awaited()
        assert info.label == ""
        assert done.frames("subagent_label") == []
        assert done.frames("subagent_spawn")

    @pytest.mark.asyncio
    async def test_the_boot_config_decides_when_no_live_config_is_armed(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        titler = _titler(monkeypatch, return_value="Refresh hint review")
        monkeypatch.setattr(config_live, "snapshot", lambda: None)
        done.orch._cfg.agent.subagent_auto_names = False
        info = _running()
        done.mgr.all_agents = [info]

        await _spawn(done, info)

        titler.assert_not_awaited()
        assert info.label == ""

    @pytest.mark.asyncio
    async def test_a_live_edit_turning_it_back_on_names_the_next_run(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _titler(monkeypatch, return_value="Refresh hint review")
        monkeypatch.setattr(config_live, "snapshot", lambda: _config(auto_names=True))
        done.orch._cfg.agent.subagent_auto_names = False
        info = _running()
        done.mgr.all_agents = [info]

        await _spawn(done, info)

        assert info.label == "Refresh hint review"


# ── the name, as the parent agent reads it ───────────────────────────────────


def _call(tool: str, args: dict[str, Any], payload: dict[str, Any]) -> str:
    with (
        patch.object(mcp_core, "_get", return_value=payload),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "list_agents", return_value=[]),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        return mcp_core._call_tool(tool, args)


class TestTools:
    def test_spawn_list_names_each_run_and_queued_spawn_by_its_label(self) -> None:
        payload = {
            "agents": [
                {"id": "a1", "task": _TASK, "label": "Refresh hint review", "done": False},
                {"id": "a2", "task": "Bare task text", "done": False},
            ],
            "queued": [
                {"id": "q1", "task": _TASK, "label": "Flake repro", "reason": "capacity"},
            ],
        }

        out = _call("spawn_list", {}, payload)

        assert "a1  [running] (0s)  Refresh hint review" in out
        assert "a2  [running] (0s)  Bare task text" in out
        assert "  Flake repro" in out
        assert "DO NOT SPAWN" not in out

    @pytest.mark.parametrize("done_run", [False, True])
    def test_spawn_status_opens_with_the_runs_name(self, done_run: bool) -> None:
        payload = {"id": "a1", "task": _TASK, "label": "Refresh hint review", "done": done_run}
        payload["result"] = "the result" if done_run else ""

        out = _call("spawn_status", {"agent_id": "a1"}, payload)

        assert out.startswith("Name: Refresh hint review\n")

    def test_spawn_status_without_a_name_is_unchanged(self) -> None:
        payload = {"id": "a1", "task": _TASK, "done": True, "result": "the result"}

        out = _call("spawn_status", {"agent_id": "a1"}, payload)

        assert "Name:" not in out


class TestDeliveryFailure:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("close_subagent_managers")
    @pytest.mark.parametrize("label", ["Refresh hint review", ""])
    async def test_the_queued_notice_keeps_the_runs_name(self, label: str) -> None:
        from subagent_coverage_helpers import _manager

        delivered = asyncio.get_running_loop().create_future()

        async def _capture(_etype: str, _info: SubagentInfo, extra: dict[str, Any]) -> None:
            delivered.set_result(extra)

        manager = _manager(on_event=_capture)
        info = make_info(task="do the thing")
        info.label = label
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value="main"):
            manager.notify_injection_failed(info)
        extra = await asyncio.wait_for(delivered, _LOST_RUN_SECS)
        text = extra["failure_msg"]

        if label:
            assert f"\nTask: do the thing\nName: {label}\nUsage: " in text
        else:
            assert "\nTask: do the thing\nUsage: " in text
            assert "Name:" not in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", ["Refresh SECRET EXFIL", ""])
    async def test_the_visible_notice_keeps_the_redacted_run_name(
        self, done: _Done, label: str
    ) -> None:
        slot = MagicMock(name="loaded tab")
        slot.window_pending = False
        slot._pending_subagent_failures = []
        done.state.get_slot = MagicMock(return_value=slot)
        info = make_info(task="do the thing")
        info.label = label

        await done.manager_kwargs["on_event"](
            "subagent_injection_failed", info, {"error": "boom", "failure_msg": ""}
        )

        slot.append.assert_called_once()
        text = slot.append.call_args.args[1]
        card = slot.append.call_args.kwargs["meta"][gw_mod.SUBAGENT_COMPLETION_META_KEY]
        if label:
            assert "\nTask: do the thing\nName: Refresh [R] [U]\n\nUsage: " in text
            assert card["label"] == "Refresh [R] [U]"
        else:
            assert "\nTask: do the thing\n\nUsage: " in text
            assert "Name:" not in text
            assert "label" not in card


class TestCompletion:
    def test_the_completion_names_the_run_inside_its_header(self) -> None:
        info = make_info(task="do the thing")
        info.label = "Refresh hint review"

        report = gw_mod._compose_subagent_completion(info, _flush_only=False)

        assert "\nTask: do the thing\nName: Refresh hint review\n\nUsage: " in report.announce
        assert report.sub_meta["label"] == "Refresh hint review"

    def test_a_run_without_a_name_reads_as_before(self) -> None:
        report = gw_mod._compose_subagent_completion(
            make_info(task="do the thing"), _flush_only=False
        )

        assert "\nTask: do the thing\n\nUsage: " in report.announce
        assert "Name:" not in report.announce
        assert "label" not in report.sub_meta

    @pytest.mark.asyncio
    async def test_completion_waits_for_an_in_flight_name(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Longer than any stall between the checks below, so the wait cannot run
        # out before the test releases the titler.
        monkeypatch.setattr(gw_mod, "_SUBAGENT_NAMING_COMPLETION_WAIT_SECS", _LOST_RUN_SECS)
        asked = asyncio.Event()
        release = asyncio.Event()

        async def _held(*_args: Any, **_kwargs: Any) -> str:
            asked.set()
            await asyncio.wait_for(release.wait(), _LOST_RUN_SECS)
            return "Refresh hint review"

        _titler(monkeypatch, side_effect=_held)
        completion_started = asyncio.Event()

        def _observe_frame(kind: str, payload: dict[str, Any]) -> None:
            if kind == "subagent_status" and payload.get("event") == "done":
                completion_started.set()

        done.state.broadcast_ws.side_effect = _observe_frame
        slot = done.tab("main")
        slot.running = True
        info = make_info(task=_TASK)
        done.mgr.all_agents = [info]

        await done.manager_kwargs["on_event"]("subagent_spawn", info, {"task": info.task})
        await asyncio.wait_for(asked.wait(), _LOST_RUN_SECS)
        completion = asyncio.create_task(done.done(info))
        await asyncio.wait_for(completion_started.wait(), _LOST_RUN_SECS)
        assert not completion.done()

        release.set()
        await asyncio.wait_for(completion, _LOST_RUN_SECS)

        ((text, _kind, meta),) = slot.queued
        assert "Name: Refresh hint review" in text
        card = meta[gw_mod.SUBAGENT_COMPLETION_META_KEY]
        assert card["label"] == "Refresh hint review"

    @pytest.mark.asyncio
    async def test_completion_timeout_does_not_cancel_naming(
        self, done: _Done, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The titler below cannot answer before ``release`` is set, so a real
        # wait this short always times out: the outcome does not depend on timing.
        monkeypatch.setattr(gw_mod, "_SUBAGENT_NAMING_COMPLETION_WAIT_SECS", 0.001)
        asked = asyncio.Event()
        release = asyncio.Event()

        async def _held(*_args: Any, **_kwargs: Any) -> str:
            asked.set()
            await asyncio.wait_for(release.wait(), _LOST_RUN_SECS)
            return "Refresh hint review"

        _titler(monkeypatch, side_effect=_held)
        slot = done.tab("main")
        slot.running = True
        info = make_info(task=_TASK)
        done.mgr.all_agents = [info]

        await done.manager_kwargs["on_event"]("subagent_spawn", info, {"task": info.task})
        await asyncio.wait_for(asked.wait(), _LOST_RUN_SECS)
        naming = done.state._background_tasks.added[-1]

        await done.done(info)

        ((text, _kind, meta),) = slot.queued
        assert "Name:" not in text
        assert "label" not in meta[gw_mod.SUBAGENT_COMPLETION_META_KEY]
        assert not naming.cancelled()
        assert not naming.done()

        release.set()
        await asyncio.wait_for(naming, _LOST_RUN_SECS)

        assert done.frames("subagent_label") == [
            {"id": "a1", "slot": "evt:dashboard:main", "label": "Refresh hint review"}
        ]

    @pytest.mark.asyncio
    async def test_a_wave_line_names_its_member(self, done: _Done) -> None:
        slot = done.tab("main")
        slot.running = True
        first = make_info("w0", batch_id="w9", batch_total=2)
        first.label = "Refresh hint review"
        second = make_info("w1", batch_id="w9", batch_total=2)

        await done.done(first)
        await done.done(second)

        ((text, _kind, _meta),) = slot.queued
        assert "— `w0` ✅ Refresh hint review · " in text
        assert "— `w1` ✅ do the thing · " in text
