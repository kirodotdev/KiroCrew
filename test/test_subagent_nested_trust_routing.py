"""A nested subagent's prompt, cards and frames belong to the ROOT chat's tab.

The gateway's ``_interactive_approval`` and event plumbing resolve a nested run's
``subagent:<id>`` parent -- a key no tab shows -- to the chat at the root of its
spawn tree: the spawn prompt lands in that tab (and on the global feed, with no
trust control, when the conversation is contested), file cards and per-run frames
follow the run there, a retry keeps the failed run's root, and recovered rows
with no stamp fail closed.
Stand-ins come from ``subagent_nested_trust_helpers.py``; the spawn gate and run
loop inheritance itself is pinned in ``test_subagent_nested_trust.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_nested_trust_helpers import (
    CONTESTED_A,
    ROOT,
    _continue,
    _ctx_builder,
    _founder_record,
    _gateway,
    _live,
    _manager,
    _sessions,
)

from kiro_crew.execution_context import execution_for_store
from kiro_crew.subagent import SubagentInfo, SubagentManager, contested_root, is_contested_root
from kiro_crew.testing.wait import async_wait_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = [
    pytest.mark.usefixtures("healthy_host_memory"),
    pytest.mark.usefixtures("close_subagent_managers"),
]


class TestNestedPromptRoutesToRootSlot:
    """Requirement 2.3 -- a nested subagent's prompt is the root chat's prompt."""

    @pytest.mark.asyncio
    async def test_run_loop_hands_the_root_key_to_the_interactive_approver(self) -> None:
        """An untrusted root still prompts, and the prompt is addressed to the root."""
        from kiro_crew.llm_helpers import LLMEvent

        sessions = _sessions(set())
        seen: list[str] = []

        async def approver(event: LLMEvent, parent_key: str = "") -> bool:
            seen.append(parent_key)
            return True

        manager = SubagentManager(
            sessions=sessions, ctx_builder=_ctx_builder(), on_tool_approval=approver
        )
        manager._global_approval_mode = ""
        _live(manager, "A", ROOT)
        info = SubagentInfo(
            execution_context=execution_for_store("", template_id=""),
            id="B",
            task="grandchild",
            parent_session_key="subagent:A",
        )
        info.root_session_key = ROOT
        manager._agents["B"] = info
        manager._agents.pop("A")  # the ancestor is gone before the first prompt

        async def stream(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
            yield LLMEvent(kind="permission_request", request_id="req-1", title="shell: ls")

        provider = sessions.get_or_create.return_value[0]
        provider.stream = MagicMock(side_effect=lambda *a, **kw: stream())
        provider.respond_permission = AsyncMock()
        manager._log_spawned(info)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            await manager._run(info)

        assert seen == [ROOT]

    @pytest.mark.asyncio
    async def test_a_contested_runs_tool_prompt_says_why_it_has_no_chat(self) -> None:
        """The run loop labels a contested run's tool prompt as the spawn gate labels its own.

        The prompt reaches only the global feed, where a bare ``shell(...)`` with
        no chat provenance reads as a routing bug. The words go in the purpose
        (the card's body); the title stays the tool. A chat-rooted run's prompt
        carries no label.
        """
        from kiro_crew.llm_helpers import LLMEvent
        from kiro_crew.subagent import (
            CONTESTED_PROMPT_REMEDY,
            CONTESTED_PROMPT_STATE,
            CONTESTED_PROMPT_WHY,
        )

        async def _prompt_for(root: str) -> LLMEvent:
            sessions = _sessions(set())
            seen: list[LLMEvent] = []

            async def approver(event: LLMEvent, parent_key: str = "") -> bool:
                seen.append(event)
                return True

            manager = SubagentManager(
                sessions=sessions, ctx_builder=_ctx_builder(), on_tool_approval=approver
            )
            manager._global_approval_mode = ""
            info = SubagentInfo(
                execution_context=execution_for_store("", template_id=""),
                id="B",
                task="grandchild",
                parent_session_key="subagent:A",
            )
            info.root_session_key = root
            manager._agents["B"] = info

            async def stream(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
                yield LLMEvent(
                    kind="permission_request",
                    request_id="req-1",
                    title="shell: psql --dry-run",
                    tool_purpose="Dry-run the pinned migration",
                )

            provider = sessions.get_or_create.return_value[0]
            provider.stream = MagicMock(side_effect=lambda *a, **kw: stream())
            provider.respond_permission = AsyncMock()
            manager._log_spawned(info)
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                await manager._run(info)
            assert len(seen) == 1
            return seen[0]

        contested = await _prompt_for(contested_root("subagent:A"))
        assert contested.title == "shell: psql --dry-run"
        # State first, then what the tool is for (the card's two lines), then
        # the why and the remedy LAST -- whole in the Review panel, so a user
        # who only ever meets tool prompts still finds the way out where they
        # decide, and the card is not filled with boilerplate on every prompt.
        # Three paragraphs: the system's state, the RUN's own claim about the
        # tool -- set as a quote, so on the one surface that exists because the
        # run is not trusted its claim never reads with the system's authority,
        # while the card's plain excerpt reads it as prose after the state --
        # then the system's why and remedy.
        # The card's two lines carry the state and the purpose; the why joins
        # the remedy in Review, where a tool-prompt-only user decides.
        assert contested.tool_purpose == (
            f"{CONTESTED_PROMPT_STATE}.\n\n> The run says: Dry-run the pinned migration."
            f"\n\nKiro Crew could not confirm that the chat continuing this task is the one that started it. "
            f"{CONTESTED_PROMPT_REMEDY}."
        )
        # The state, verbatim: one sentence true for every case the marker covers;
        # the why states only what every case established (the starting chat was
        # not confirmed as the continuing one) -- true for two chats, for an
        # unreadable or pre-stamp founder, and for a chat continuing a cron's or
        # the CLI's run, without asserting a comparison the unmeasured cases never made.
        assert CONTESTED_PROMPT_STATE == "This run has no single owning chat"
        assert (
            CONTESTED_PROMPT_WHY
            == "Kiro Crew could not confirm that the chat continuing this task is the one that started it"
        )

        plain = await _prompt_for(ROOT)
        assert plain.tool_purpose == "Dry-run the pinned migration"

    @pytest.mark.asyncio
    async def test_low_fidelity_child_fallback_also_receives_the_root_key(self) -> None:
        """The child-origin (low-fidelity) interactive downgrade is addressed to the root too."""
        from kiro_crew.hooks import ToolHookResult
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

        sessions = _sessions(set())
        seen: list[str] = []

        async def approver(event: LLMEvent, parent_key: str = "") -> bool:
            seen.append(parent_key)
            return True

        ctx = _ctx_builder()
        ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult.allow())
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, on_tool_approval=approver)
        manager._global_approval_mode = ""
        info = SubagentInfo(
            execution_context=execution_for_store("", template_id=""),
            id="B",
            task="grandchild",
            parent_session_key="subagent:A",  # ancestor never live at prompt time
        )
        info.root_session_key = ROOT
        manager._agents["B"] = info
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="@example-server/get-item",
            request_id=7001,
            sub_session_id="child-a",
            shell_classified=True,
            is_shell=False,
            mcp_server_name="example-server",
            tool_name="get-item",
        )
        assert event.child_low_fidelity

        async def stream(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
            yield event

        provider = sessions.get_or_create.return_value[0]
        provider.stream = MagicMock(side_effect=lambda *a, **kw: stream())
        provider.approve_tool = AsyncMock()
        provider.reject_tool = AsyncMock()
        manager._log_spawned(info)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            await manager._run(info)

        assert seen == [ROOT]

    def test_spawn_approval_slot_is_the_root_chat_tab(self) -> None:
        """A nested spawn's approval card is addressed to the root chat's tab."""
        manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
        _live(manager, "A", ROOT)
        b = _live(manager, "B", "subagent:A")
        b.root_session_key = ROOT
        manager._agents.pop("A")  # ancestor gone; the stamp still routes
        gateway = _gateway(manager)

        assert gateway._spawn_approval_slot("spawn:B") == "chat-1"  # type: ignore[attr-defined]
        assert gateway._spawn_approval_slot("spawn:ghost") == ""  # type: ignore[attr-defined]
        gateway.subagent_mgr = None  # type: ignore[attr-defined]
        assert gateway._spawn_approval_slot("spawn:B") == ""  # type: ignore[attr-defined]

    def test_a_workflow_step_root_resolves_to_the_run_origin_tab(self) -> None:
        """A spawn tree rooted in a workflow step answers to the chat that started the run.

        The step's own ``wf:<run_id>:...`` session is shown by no tab, so the
        root alone names nothing; the registry's recorded origin does -- at
        every depth, not only for the step's direct spawn.
        """
        from types import SimpleNamespace

        from kiro_crew.workflows.registry import RunHandle, RunRegistry

        step = "wf:run1:0"
        manager = _manager(_sessions({ROOT, step}), AsyncMock(return_value=True))
        _live(manager, "A", step)
        b = _live(manager, "B", "subagent:A")
        assert b.root_session_key == step
        registry = RunRegistry(store=None)
        registry.register(RunHandle(run_id="run1", name="w", session_key=ROOT), persist=False)
        gateway = _gateway(manager)
        gateway.dashboard_state.workflow_service = SimpleNamespace(registry=registry)  # type: ignore[attr-defined]

        assert gateway._spawn_approval_slot("spawn:A") == "chat-1"  # type: ignore[attr-defined]
        assert gateway._spawn_approval_slot("spawn:B") == "chat-1"  # type: ignore[attr-defined]
        # An unknown run still prompts rather than guessing a tab.
        gateway.dashboard_state.workflow_service = SimpleNamespace(registry=RunRegistry(store=None))  # type: ignore[attr-defined]
        assert gateway._spawn_approval_slot("spawn:B") == ""  # type: ignore[attr-defined]

    def test_a_contested_root_names_no_slot(self) -> None:
        """A contested marker is no session key; it must not become a phantom slot.

        ``subagent_event_slot`` falls back to the raw key for a key no tab shows,
        which would broadcast the approval under ``contested:subagent:A`` -- a
        slot nothing renders, with a misleading audit reason -- instead of the
        documented global-feed-only prompt.
        """
        manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
        original = _live(manager, "A", "dashboard:chat-2")
        original.done = True
        _continue(manager, "A2", ROOT, "subagent:A")
        b = _live(manager, "B", "subagent:A")
        assert b.root_session_key == CONTESTED_A
        gateway = _gateway(manager)

        assert gateway._spawn_approval_slot("spawn:B") == ""  # type: ignore[attr-defined]
        # The continuation's OWN prompt too: its card lives in the continuing
        # chat's tab, but that tab's Trust must not answer it, so the resolver
        # follows the trust root (the marker) and names no slot.
        a2 = manager.get("A2")
        assert a2 is not None and manager.root_session_key_for(a2) == ROOT
        assert gateway._spawn_approval_slot("spawn:A2") == ""  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_lifecycle_frames_follow_the_root_tab(self) -> None:
        """A nested run's spawn/done frames carry the root chat's slot.

        The spawn prompt planted a pending card in that tab; frames tagged with
        the literal ``subagent:<id>`` parent would name no tab and leave the
        card "Starting" forever.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.slack.gateway import GatewayOrchestrator

        manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
        _live(manager, "A", ROOT)
        b = _live(manager, "B", "subagent:A")
        assert b.root_session_key == ROOT

        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U000"}):
            orch = GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
        orch.sessions = MagicMock()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = MagicMock()
        with (
            patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False),
            patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm,
        ):
            mock_sm.return_value = MagicMock(start_reaper=MagicMock())
            orch._init_subagents()
            on_event = mock_sm.call_args.kwargs["on_event"]
        orch.subagent_mgr = manager  # the real manager resolves the stamp

        await on_event("subagent_spawn", b, {"task": "t", "agent": ""})
        etype, payload = orch.dashboard_state.broadcast_ws.call_args[0]
        assert etype == "subagent_spawn"
        assert payload["slot"] == "chat-1"  # the root tab, not subagent:A

    def test_rooted_list_is_the_tree_and_the_parent_list_is_the_wave(self) -> None:
        """The per-slot list a tab shows is the tree rooted there; the wave stays parent-keyed."""
        manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
        _live(manager, "A", ROOT)
        _live(manager, "B", "subagent:A")
        _live(manager, "C", "dashboard:chat-2")
        rooted = {a["id"] for a in manager.running_agents_rooted_at(ROOT)}
        assert rooted == {"A", "B"}
        assert {a["id"] for a in manager.running_agents_for(ROOT)} == {"A"}
        assert {a["id"] for a in manager.running_agents_for("subagent:A")} == {"B"}
        # The once-per-push form the sidebar badge reads: every live run's tab
        # root, so a nested run keeps its root tab's badge lit and a finished
        # run lights nothing.
        assert manager.live_root_session_keys() == {ROOT, "dashboard:chat-2"}
        manager._agents["A"].done = True
        assert manager.live_root_session_keys() == {ROOT, "dashboard:chat-2"}  # B still runs
        manager._agents["B"].done = True
        assert manager.live_root_session_keys() == {"dashboard:chat-2"}

    def test_a_run_below_a_contest_is_slotted_to_its_ancestors_tab_not_the_marker(self) -> None:
        """A contested marker is a trust stamp; no tab reads it.

        A run spawned by a contested continuation is stamped with the marker so
        its refusal outlives the records that established it -- but slotted to
        the marker, its frames and the rooted listing would put its card in no
        tab and the sidebar's count would go dark while it runs. The tab is the
        one its parent conversation's LIVE card sits in -- the newest active
        record under the conversation, not the founder that
        ``_conversation_records`` lists first: the founder's tab answering
        until its record is evicted would paint the card in a chat the user
        is not driving, then move it mid-run and leave the first tab a stale
        running card no ``running == 0`` frame ever clears. Trust still reads
        the marker.
        """
        from kiro_crew.dashboard.ws import subagent_replay_slot
        from kiro_crew.slack.gateway import subagent_event_slot

        manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
        founder = _live(manager, "A", ROOT)
        founder.done = True
        founder.started = 1.0
        cont = _continue(manager, "A2", "dashboard:chat-2", "subagent:A")  # contests A
        cont.started = 2.0
        child = _live(manager, "C", "subagent:A")
        assert child.root_session_key == CONTESTED_A  # the trust stamp
        assert manager.trust_root_for(child) == CONTESTED_A

        tab_root = manager.root_session_key_for(child)
        assert tab_root == "dashboard:chat-2"  # the live continuation, not the founder
        assert not is_contested_root(tab_root)
        assert subagent_event_slot(tab_root) == "chat-2"
        # The continuation's own card sits there too; the child joins it.
        assert [a["id"] for a in manager.running_agents_rooted_at("dashboard:chat-2")] == [
            "A2",
            "C",
        ]
        assert manager.running_agents_rooted_at(ROOT) == []
        assert manager.running_agents_rooted_at(CONTESTED_A) == []
        # The replay names a tab too, from the same rule; without a manager the
        # marker resolves to no slot rather than tagging the card with it.
        assert subagent_replay_slot(child, manager) == "chat-2"
        assert subagent_replay_slot(child) == ""

        # Evicting the done founder changes nothing: the card does not move.
        del manager._agents["A"]
        assert manager.root_session_key_for(child) == "dashboard:chat-2"
        # A roster of only-finished records still answers, and with the same
        # tab -- the most recently started -- so a finished tree reads the same
        # before and after the founder is pruned.
        manager._agents["A"] = founder
        cont.done = True
        assert manager.root_session_key_for(child) == "dashboard:chat-2"
        del manager._agents["A"]
        assert manager.root_session_key_for(child) == "dashboard:chat-2"
        # A live founder outranks a finished continuation: active before done.
        manager._agents["A"] = founder
        founder.done = False
        assert manager.root_session_key_for(child) == ROOT
        founder.done = True
        # With no ancestor retained, there is no tab -- the feed, where the
        # marker resolved anyway -- and still no marker in a slot.
        del manager._agents["A"]
        del manager._agents["A2"]
        assert manager.root_session_key_for(child) == ""
        assert manager.trust_root_for(child) == CONTESTED_A

    def test_a_continuation_issued_from_under_a_contest_routes_its_children_to_the_continuing_chat(
        self,
    ) -> None:
        """Only the live record at each hop is asked; a contested one is climbed through.

        Chat 2 continues A (contesting it), and from that contested turn
        continues A's finished child B. B2's own root stamp is the marker --
        it was spawned from under the contest -- so an answer that fell back
        to the next record under ``subagent:B`` would pick B's founder stamp,
        Chat 1's tab, and a grandchild's file card or done frame would land in
        the chat that is not driving it. The climb follows B2's parent instead,
        up to the contested continuation A2 and its chat.
        """
        manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
        a = _live(manager, "A", ROOT)
        a.done = True
        a.started = 1.0
        b = _live(manager, "B", "subagent:A")  # founded under Chat 1
        b.done = True
        b.started = 2.0
        a2 = _continue(manager, "A2", "dashboard:chat-2", "subagent:A")  # contests A
        a2.started = 3.0
        b2 = _continue(manager, "B2", "subagent:A", "subagent:B")  # issued from under the contest
        b2.started = 4.0
        assert b2.root_session_key == CONTESTED_A
        d = _live(manager, "D", "subagent:B")
        assert is_contested_root(d.root_session_key)

        assert manager.root_session_key_for(d) == "dashboard:chat-2"
        assert manager.root_session_key_for(b2) == "dashboard:chat-2"
        assert [x["id"] for x in manager.running_agents_rooted_at("dashboard:chat-2")] == [
            "A2",
            "B2",
            "D",
        ]
        assert manager.running_agents_rooted_at(ROOT) == []
        # Trust still reads a marker on every run below the contest: B2 brought
        # Chat 2's turn into Chat 1's conversation B, so B is contested in its
        # own name and its children refuse under that marker.
        assert manager.trust_root_for(d) == contested_root("subagent:B")
        assert manager.trust_root_for(b2) == contested_root("subagent:B")

    @pytest.mark.asyncio
    async def test_status_frame_for_a_nested_run_refreshes_the_root_tabs_list(self) -> None:
        """``subagent_status`` names the root tab and lists the tree rooted there.

        The reducer REPLACES a slot's list with ``agents`` and evicts the slot's
        cards when ``running`` is 0: a parent-keyed frame for the depth-one run's
        done would wipe the live nested card from the root tab, and a frame keyed
        on the nested run's ``subagent:<id>`` parent would reach no tab at all.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.slack.gateway import GatewayOrchestrator

        manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
        a = _live(manager, "A", ROOT)
        b = _live(manager, "B", "subagent:A")

        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U000"}):
            orch = GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
        orch.sessions = MagicMock()
        orch.sessions.get_pid = MagicMock(return_value=None)
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = MagicMock()
        orch.dashboard_state.get_slot = MagicMock(return_value=None)
        with (
            patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False),
            patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm,
        ):
            mock_sm.return_value = MagicMock(start_reaper=MagicMock())
            orch._init_subagents()
            on_done = mock_sm.call_args.kwargs["on_done"]
        orch.subagent_mgr = manager

        # The coordinator finishes first while its nested run is still live.
        a.done = True
        await on_done(a)
        status = [
            c.args[1]
            for c in orch.dashboard_state.broadcast_ws.call_args_list
            if c.args[0] == "subagent_status"
        ]
        assert status, "the done path broadcasts a status frame"
        assert status[-1]["slot"] == "chat-1"
        assert status[-1]["running"] == 1  # B is still live in this tab
        assert [x["id"] for x in status[-1]["agents"]] == ["B"]

        # Then the nested run finishes: its frame reaches the root tab too.
        orch.dashboard_state.broadcast_ws.reset_mock()
        b.done = True
        await on_done(b)
        status = [
            c.args[1]
            for c in orch.dashboard_state.broadcast_ws.call_args_list
            if c.args[0] == "subagent_status"
        ]
        assert status and status[-1]["slot"] == "chat-1"
        assert status[-1]["running"] == 0 and status[-1]["agents"] == []

    def test_reconnect_replay_slots_a_nested_run_to_the_root_tab(self) -> None:
        """The replayed snapshot names the tab the live frames did, or the card vanishes on reconnect."""
        from kiro_crew.dashboard.ws import build_subagent_snapshot, subagent_replay_slot

        manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
        _live(manager, "A", ROOT)
        b = _live(manager, "B", "subagent:A")
        assert subagent_replay_slot(b) == "chat-1"
        assert build_subagent_snapshot(b)["slot"] == "chat-1"
        # A record without a stamp keeps the parent mapping it always had.
        b.root_session_key = ""
        assert subagent_replay_slot(b) == "subagent:A"

    @pytest.mark.asyncio
    async def test_root_slot_trust_approves_a_nested_prompt(self) -> None:
        """Slot trust on the root chat auto-approves a prompt addressed to it."""
        from kiro_crew.llm_helpers import LLMEvent

        manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
        gateway = _gateway(manager)
        root_slot = MagicMock()
        root_slot._trust = True
        gateway.dashboard_state._slots = {"chat-1": root_slot}  # type: ignore[attr-defined]

        with (
            patch("kiro_crew.slack.gateway.safety_override") as override,
            patch("kiro_crew.slack.gateway.sel"),
        ):
            override.return_value.is_active.return_value = False
            approve = gateway._interactive_approval("subagent")  # type: ignore[attr-defined]
            event = LLMEvent(kind="permission_request", request_id="req-1", title="shell: ls")
            approved = await approve(event, ROOT)

        assert approved is True
        gateway.dashboard_state.request_approval.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_the_prompt_record_says_contested_only_for_a_contested_root(self) -> None:
        """The dashboard record carries an explicit ``contested`` flag.

        The feed card leads with the purpose only for a contested prompt, where
        the purpose is the system's account of why there is no tab. A reader
        must not infer that from the slot: a prompt with no slot is not thereby
        contested -- a run rooted in a cron, the CLI or a channel has no tab
        either, and its purpose is the run's own words -- so the gateway sets the
        flag from the trust root it was handed, and only then.
        """
        from kiro_crew.llm_helpers import LLMEvent

        manager = _manager(_sessions(set()), AsyncMock(return_value=True))
        gateway = _gateway(manager)

        async def _record_for(parent_key: str) -> dict:
            gateway.dashboard_state.request_approval.reset_mock()  # type: ignore[attr-defined]
            with (
                patch("kiro_crew.slack.gateway.safety_override") as override,
                patch("kiro_crew.slack.gateway.sel"),
            ):
                override.return_value.is_active.return_value = False
                approve = gateway._interactive_approval("subagent")  # type: ignore[attr-defined]
                event = LLMEvent(
                    kind="permission_request",
                    request_id="req-1",
                    title="shell: psql --dry-run",
                    tool_purpose="Dry-run the migration",
                )
                await approve(event, parent_key)
            gateway.dashboard_state.request_approval.assert_awaited_once()  # type: ignore[attr-defined]
            return gateway.dashboard_state.request_approval.await_args.kwargs  # type: ignore[attr-defined]

        contested = await _record_for(CONTESTED_A)
        assert contested["slot"] == "" and contested["contested"] is True
        # Slotless, but rooted in a cron: NOT contested.
        cron_rooted = await _record_for("cron:job-1")
        assert cron_rooted["slot"] == "" and cron_rooted["contested"] is False

    @pytest.mark.asyncio
    async def test_spawn_prompt_first_line_is_the_title_and_the_rest_its_purpose(self) -> None:
        """The gateway's spawn approver hands a two-line description over as title + purpose.

        The dashboard renders a prompt's title truncated to a few words (the feed
        card, the detail panel header) and its purpose in full as the body, so
        the contested label's operative first line is what the feed shows and its
        explanation is not lost to the truncation. A one-line description is
        unchanged: the whole of it is the title, and the purpose stays empty.
        """
        gateway = _gateway(_manager(_sessions({ROOT}), AsyncMock(return_value=True)))
        gateway.ctx_builder = MagicMock()  # type: ignore[attr-defined]
        gateway.ctx_builder.hooks = MagicMock()  # type: ignore[attr-defined]
        gateway.conv_log = None  # type: ignore[attr-defined]
        recorder = AsyncMock(return_value=True)
        gateway._interactive_approval = MagicMock(return_value=recorder)  # type: ignore[attr-defined,method-assign]
        captured: dict = {}

        def _capture(**kw: object) -> MagicMock:
            captured["on_spawn_approval"] = kw["on_spawn_approval"]
            mgr = MagicMock()
            mgr.running = []
            mgr.queued_count_for = MagicMock(return_value=0)
            mgr.queued_count_for_async = AsyncMock(return_value=0)
            return mgr

        with (
            patch("kiro_crew.slack.gateway.SubagentManager", side_effect=_capture),
            patch("kiro_crew.slack.gateway.deliver_spawn_approval", AsyncMock(return_value=None)),
        ):
            gateway._init_subagents()  # type: ignore[attr-defined]
            spawn_approve = captured["on_spawn_approval"]
            two_lines = "spawn_run(t)\nThis run has no single owning chat. Start this task again from a single chat, or approve this request (each request will ask again)."
            assert await spawn_approve("req-2", two_lines, ROOT) is True
            event, key = recorder.await_args.args
            assert key == ROOT
            assert event.title == "spawn_run(t)"
            assert (
                event.tool_purpose
                == "This run has no single owning chat. Start this task again from a single chat, or approve this request (each request will ask again)."
            )

            assert await spawn_approve("req-3", "spawn_run(t)", ROOT) is True
            event, _key = recorder.await_args.args
            assert (event.title, event.tool_purpose) == ("spawn_run(t)", "")


class TestNestedFileCardRoutesToRoot:
    """A nested run's file card lands in the root chat's tab instead of being suppressed."""

    def test_parent_lookup_prefers_the_stamped_root(self) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers.files import _subagent_parent_session_key

        b = SimpleNamespace(
            id="B",
            conversation_key="",
            parent_session_key="subagent:A",
            root_session_key=ROOT,
            done=False,
            started=2.0,
        )
        state = SimpleNamespace(subagents=SimpleNamespace(all_agents=[b]))

        assert _subagent_parent_session_key(state, "subagent:B") == ROOT

    def test_parent_lookup_without_a_stamp_is_unchanged(self) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers.files import _subagent_parent_session_key

        a = SimpleNamespace(
            id="A",
            conversation_key="",
            parent_session_key=ROOT,
            root_session_key="",
            done=False,
            started=1.0,
        )
        state = SimpleNamespace(subagents=SimpleNamespace(all_agents=[a]))

        assert _subagent_parent_session_key(state, "subagent:A") == ROOT

    def test_a_contested_descendants_file_card_reaches_its_ancestors_tab(self) -> None:
        """The card routes through the manager's tab accessor, never the raw marker.

        A run below a contested conversation is stamped with the ``contested:``
        marker; read raw, the marker has no dashboard slot, so the card would be
        dropped with a log line while ``file_send`` still reported success. The
        accessor turns it into the ancestor's tab, as every other frame of the
        run is.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers.files import _subagent_parent_session_key

        manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
        founder = _live(manager, "A", ROOT)
        founder.done = True
        founder.started = 1.0
        cont = _continue(manager, "A2", "dashboard:chat-2", "subagent:A")  # contests A
        cont.started = 2.0
        child = _live(manager, "C", "subagent:A")
        assert child.root_session_key == CONTESTED_A
        state = SimpleNamespace(subagents=manager)

        # The live continuation's tab -- where the run's other frames land.
        assert _subagent_parent_session_key(state, "subagent:C") == "dashboard:chat-2"
        del manager._agents["A"]
        assert _subagent_parent_session_key(state, "subagent:C") == "dashboard:chat-2"
        # Every ancestor evicted: no tab, so the card is suppressed -- and still
        # no marker is handed to the slot lookup.
        del manager._agents["A2"]
        assert _subagent_parent_session_key(state, "subagent:C") == ""

    @pytest.mark.asyncio
    async def test_a_later_continuation_from_another_chat_does_not_move_a_contested_childs_tab(
        self,
    ) -> None:
        """The tab a run below a contest belongs to is fixed when it is admitted.

        Chat 2 continues A (contesting it) and that turn spawns C; the
        continuation then finishes while C's record stays. Chat 3 continues A
        next. Resolved afresh, C's tab would follow the newest live record
        under ``subagent:A`` -- chat 3's -- so C's file card and frames would
        land in a chat that never asked for it.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers.files import _subagent_parent_session_key

        manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
        founder = _live(manager, "A", ROOT)
        founder.done = True
        founder.started = 1.0
        cont = _continue(manager, "A2", "dashboard:chat-2", "subagent:A")  # contests A
        cont.started = 2.0
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn("child task", parent_session_key="subagent:A")
            assert child is not None
            await manager._tasks[child.id]
        assert child.root_session_key == CONTESTED_A
        assert manager.root_session_key_for(child) == "dashboard:chat-2"

        cont.done = True
        later = _continue(manager, "A3", "dashboard:chat-3", "subagent:A")
        later.started = 3.0
        state = SimpleNamespace(subagents=manager)
        assert manager.root_session_key_for(child) == "dashboard:chat-2"
        assert _subagent_parent_session_key(state, f"subagent:{child.id}") == "dashboard:chat-2"
        assert manager.trust_root_for(child) == CONTESTED_A


class TestRetryKeepsTheFailedRunsRoot:
    """A retried run is governed by the trust its original actually ran under.

    Between the failure and the retry, the parent conversation can be evicted and
    continued from a trusted chat; a gate that re-walked the parent link would
    then hand the retry -- and every tool call under it -- that chat's trust.
    The retry therefore passes the original's admission stamp, exactly as a
    drained queue member re-enters with the root it was queued under.
    """

    @pytest.mark.asyncio
    async def test_retry_passes_the_admission_stamped_root(self) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers.messaging import api_spawn_retry
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        untrusted = "dashboard:chat-2"
        old = SimpleNamespace(
            id="a1",
            task="t",
            _raw_task="t",
            parent_session_key="subagent:A",
            root_session_key=untrusted,  # stamped at admission, before A's eviction
            conversation_root_session_key=untrusted,
            agent="",
            max_turns=0,
            cwd="",
            model="",
            reasoning_effort="",
            approval_mode="",
            silent=False,
            delegation={},
            include_memory=True,
            include_lessons=True,
            include_project=True,
            memory_store="",
            crew="",
            done=True,
            outcome="failed",
            execution_context=ExecutionContext(
                None, MemoryStoreRef("default"), "template", "kirocrew"
            ),
        )
        mgr = MagicMock()
        mgr.get.return_value = old
        mgr.spawn.return_value = SimpleNamespace(id="a2", done=False, error="")
        request = MagicMock()
        request.app = {"state": SimpleNamespace(subagents=mgr)}
        request.headers = {}
        request.match_info = {"agent_id": "a1"}

        await api_spawn_retry(request)

        kwargs = mgr.spawn.call_args.kwargs
        assert kwargs["parent_session_key"] == "subagent:A"
        assert kwargs["_root_session_key"] == untrusted
        # An uncontested stamp is re-derived by the gate, as for a fresh spawn.
        assert kwargs["_conversation_root_session_key"] == ""

        # A failed CONTESTED continuation: its routing root is the chat that
        # continued it, but its trust root is the marker, and the retry must
        # carry the marker rather than found a fresh conversation at the chat.
        old.parent_session_key = ROOT
        old.root_session_key = ROOT
        old.conversation_root_session_key = CONTESTED_A
        await api_spawn_retry(request)

        kwargs = mgr.spawn.call_args.kwargs
        assert kwargs["_root_session_key"] == ROOT
        assert kwargs["_conversation_root_session_key"] == CONTESTED_A

    @pytest.mark.asyncio
    async def test_gate_honours_the_carried_root_over_a_re_walk(self) -> None:
        """The stamp the retry carries wins even when a re-walk would now find a trusted chat."""
        untrusted = "dashboard:chat-2"
        sessions = _sessions({ROOT})
        approval = AsyncMock(return_value=True)
        manager = _manager(sessions, approval)
        # The failed run's parent conversation was evicted; a fresh walk of
        # ``subagent:A`` now yields ROOT (arranged through the founder's durable
        # record so a trusted continuation is retained), which is exactly the
        # answer the carried stamp must beat.
        with _founder_record(ROOT):
            _continue(manager, "A2", ROOT, "subagent:A")
        assert manager.root_session_key("subagent:A") == ROOT

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn(
                "retried task", parent_session_key="subagent:A", _root_session_key=untrusted
            )
            assert info is not None
            await manager._tasks[info.id]

        assert info.root_session_key == untrusted
        approval.assert_awaited_once()  # interactive: the original's chat was not trusted

    @pytest.mark.asyncio
    async def test_a_retried_contested_continuation_keeps_the_marker(self) -> None:
        """A retry whose routing root is a trusted chat still runs under the contested marker.

        The failed run was a continuation the trusted chat made of another chat's
        conversation: its ``root_session_key`` is that chat (its card's tab) and
        its trust root the marker. The retry founds no fresh conversation at the
        chat -- the gate takes the carried marker as the conversation root, so
        the spawn is interactive and its run's trust root is the marker.
        """
        sessions = _sessions({ROOT})
        approval = AsyncMock(return_value=True)
        manager = _manager(sessions, approval)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn(
                "retried follow-up",
                parent_session_key=ROOT,
                _root_session_key=ROOT,
                _conversation_root_session_key=CONTESTED_A,
            )
            assert info is not None
            await manager._tasks[info.id]

        assert info.root_session_key == ROOT  # the card still belongs to the chat's tab
        assert info.conversation_root_session_key == CONTESTED_A
        assert manager.trust_root_for(info) == CONTESTED_A
        approval.assert_awaited_once()  # interactive, though the routing root is trusted
        assert approval.await_args.args[2] == CONTESTED_A


class TestRecoveredRowsFailClosed:
    """A store row written before roots were stamped cannot inherit trust at re-entry.

    A queued row outlives a restart, and ``_agents`` is not rehydrated on boot, so
    a pre-stamp row's ``subagent:`` caller has no record to walk: a continuation
    of its conversation from another chat founds the conversation in that chat's
    name, and a re-walk would hand the recovered run that chat's trust. The store's
    read side hands the row through as written; the gate's resumed-admission rule
    (every window entry re-enters with ``_from_queue=True``) fails it closed.
    """

    @staticmethod
    def _entry(session_key: str, params: dict) -> dict:
        from kiro_crew.subagent_manager.admission.taskq_bridge import _TaskqBridgeMixin
        from kiro_crew.taskq import model

        rec = model.TaskRecord(
            id="r1", kind=model.KIND_SUBAGENT, session_key=session_key, params=params
        )
        return _TaskqBridgeMixin._window_entry(rec)

    def test_the_read_side_hands_a_legacy_row_through_unstamped(self) -> None:
        """No second copy of the rule: the entry carries what the row carries."""
        entry = self._entry("subagent:A", {"task": "t"})
        assert "_root_session_key" not in entry
        assert entry["_preassigned_id"] == "r1"
        stamped = self._entry("subagent:A", {"task": "t", "_root_session_key": ROOT})
        assert stamped["_root_session_key"] == ROOT

    def test_the_read_side_strips_the_conversation_root_a_row_was_written_under(self) -> None:
        """The root stamp is re-resolved at re-entry, never replayed from the row.

        The trust-root stamp stays (a re-walk could only widen it); the
        conversation-root stamp goes (a replay could only widen it), the same
        way as the two process-local grants beside it.
        """
        entry = self._entry(
            "subagent:A",
            {
                "task": "t",
                "conversation_key": "subagent:A",
                "_root_session_key": ROOT,
                "_conversation_root_session_key": ROOT,
                "_agent_prevalidated": True,
                "approval_mode": "auto",
            },
        )
        assert entry["_root_session_key"] == ROOT
        assert "_conversation_root_session_key" not in entry
        assert "_agent_prevalidated" not in entry and "approval_mode" not in entry
        # A CONTESTED stamp is kept: a refusal, not a grant, and a row that
        # carries one may name no conversation to re-resolve it from.
        contested = self._entry(
            "subagent:A",
            {"task": "t", "_root_session_key": ROOT, "_conversation_root_session_key": CONTESTED_A},
        )
        assert contested["_conversation_root_session_key"] == CONTESTED_A

    @pytest.mark.asyncio
    async def test_a_refilled_retry_of_a_contested_continuation_stays_contested(self) -> None:
        """The dashboard retry of a failed contested continuation, refilled after a restart.

        The retry founds a fresh conversation (no ``conversation_key``) at the
        continuing chat's root and carries the marker as its only tie to the
        contest (``api_spawn_retry``). Its row outlives a restart while queued;
        a read side that stripped the marker would leave a ``dashboard:`` caller
        the gate's fail-closed rule does not cover, resolving to the continuing
        chat's own trust and auto-approving the turn the marker denies.
        """
        sessions = _sessions({ROOT})
        approval = AsyncMock(return_value=True)
        manager = _manager(sessions, approval)
        entry = self._entry(
            ROOT,
            {
                "task": "retry of a contested continuation",
                "parent_session_key": ROOT,
                "_root_session_key": ROOT,
                "_conversation_root_session_key": CONTESTED_A,
            },
        )
        entry.pop("_lane")
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn(**entry, _from_queue=True)
            assert info is not None and not info.queued
            await manager._tasks[info.id]

        assert info.root_session_key == ROOT  # its card stays in the continuing chat's tab
        assert info.conversation_root_session_key == CONTESTED_A
        assert manager.trust_root_for(info) == CONTESTED_A
        approval.assert_awaited_once()  # interactive, never the continuing chat's auto
        _rid, _description, session_key = approval.await_args.args
        assert session_key == CONTESTED_A

    @pytest.mark.asyncio
    async def test_a_refilled_continuation_reads_a_contest_written_while_it_waited(
        self, monkeypatch
    ) -> None:
        """A store-only row does not hold its conversation busy, so a continuation
        from another chat can be admitted while it waits and write a contest onto
        the founder's durable record. The row's own stamp predates that contest:
        replayed, it would run the refilled continuation -- and every tool call in
        it -- on the founding chat's auto-approval. The pump re-resolves instead,
        reading the founder's durable record off-loop, and the in-memory founder
        (still retained with its founding root; the rival that contested it is
        gone) does not outvote the record.
        """
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission.taskq_bridge import _TaskqBridgeMixin

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        sessions = _sessions({ROOT})
        manager = _manager(sessions, AsyncMock(return_value=True))
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        admission = manager._admission
        store = admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"
        founder = _live(manager, "A", ROOT)  # retained, still stamped with its founding root
        founder.done = True  # finished, so a continuation may start; its record stays
        assert founder.conversation_root_session_key == ROOT

        # The row as admission wrote it, before the contest: stamped ROOT.
        rec = admission.taskq_build_record(
            "r1",
            {
                "task": "continue A",
                "parent_session_key": ROOT,
                "conversation_key": "subagent:A",
                "_root_session_key": ROOT,
                "_conversation_root_session_key": ROOT,
                "_execution_context": execution_for_store("", template_id="").to_record(),
            },
            parent_session_key=ROOT,
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert rec.params["_conversation_root_session_key"] == ROOT  # the row carries it

        # Only the FOUNDER's durable record is faked -- carrying the contest a
        # rival wrote while the row waited; every other read is real.
        from kiro_crew import subagent as _subagent

        real_read_state = _subagent.read_state

        def _read_state(agent_id: str, *a: object, **kw: object) -> object:
            if agent_id == "A":
                return {"id": "A", "conversation_root": CONTESTED_A}
            return real_read_state(agent_id, *a, **kw)

        async def _park(info: SubagentInfo) -> None:  # the admitted run stays LIVE
            await asyncio.Event().wait()

        try:
            assert await store.run(admission.taskq_accept_record, rec) is None
            entry = _TaskqBridgeMixin._window_entry(rec)  # the refill's read side
            entry.pop("_lane")
            with (
                patch("kiro_crew.subagent.read_state", side_effect=_read_state),
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
            ):
                info = await manager._dispatch_async(entry)
            assert info is not None and not info.queued and not info.error, info.error
            assert info.id == "r1" and info.id in manager._tasks
            assert info.root_session_key == ROOT  # the card still belongs to the chat's tab
            assert info.conversation_root_session_key == CONTESTED_A
            assert manager.trust_root_for(info) == CONTESTED_A
            assert sessions.get_approval_policy(manager.trust_root_for(info)) == ""
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_queue_dispatch_holds_the_conversation_while_its_cached_root_is_in_flight(
        self, monkeypatch
    ) -> None:
        """The founder's durable root is read once and carried across the
        validation, memory and claim awaits. A rival continuation admitted in
        that window could contest the conversation on disk and be dismissed
        before the commit-point check (which reads retained records) could
        adopt it, and the cached root would run the refilled continuation on
        the founding chat's auto-approval. So the dispatch holds the
        conversation (``_continuation_starts``) from before the read until the
        start settles: the gate answers a rival ``conversation_busy`` naming
        this row meanwhile, and the registered run holds the conversation after.
        """
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission.taskq_bridge import _TaskqBridgeMixin

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        sessions = _sessions({ROOT})
        manager = _manager(sessions, AsyncMock(return_value=True))
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        admission = manager._admission
        store = admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"
        founder = _live(manager, "A", ROOT)
        founder.done = True
        rec = admission.taskq_build_record(
            "r1",
            {
                "task": "continue A",
                "parent_session_key": ROOT,
                "conversation_key": "subagent:A",
                "_root_session_key": ROOT,
                "_conversation_root_session_key": ROOT,
                "_execution_context": execution_for_store("", template_id="").to_record(),
            },
            parent_session_key=ROOT,
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )

        real_check = manager._check_agent_off_loop
        seen_during_validation: dict[str, object] = {}

        async def _check(*args: object, **kwargs: object) -> object:
            # The window GPT named: the durable root is already cached, the
            # gate has not run. A rival's gate entry asks this question.
            busy = manager._conversation_busy("subagent:A")
            seen_during_validation["held"] = manager._continuation_starts.get("subagent:A")
            seen_during_validation["busy"] = busy.id if busy is not None else None
            return await real_check(*args, **kwargs)

        async def _park(info: SubagentInfo) -> None:
            await asyncio.Event().wait()

        try:
            assert await store.run(admission.taskq_accept_record, rec) is None
            entry = _TaskqBridgeMixin._window_entry(rec)
            entry.pop("_lane")
            with (
                _founder_record(ROOT),
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_check_agent_off_loop", _check),
                patch.object(manager, "_run", _park),
            ):
                info = await manager._dispatch_async(entry)
            assert seen_during_validation == {"held": "r1", "busy": "r1"}
            assert info is not None and not info.queued and not info.error, info.error
            assert info.id == "r1" and info.id in manager._tasks
            # Registered: the run itself holds the conversation; the claim is gone.
            assert "subagent:A" not in manager._continuation_starts
            busy = manager._conversation_busy("subagent:A")
            assert busy is not None and busy.id == "r1"
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_stop_during_the_dispatch_reads_releases_the_conversation(
        self, monkeypatch
    ) -> None:
        """A user stopping a queued continuation while it is being dispatched is
        an ordinary action. The dispatch holds the conversation from before its
        reads; the stopped-row exit (an undurable row the stop took from
        ``_undurable_in_dispatch`` while the reads ran) must release it like
        every other exit, or ``_conversation_busy`` refuses every later
        continuation of that conversation until a restart.
        """
        from kiro_crew.subagent_manager import admission as admission_mod

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        sessions = _sessions({ROOT})
        manager = _manager(sessions, AsyncMock(return_value=True))
        await manager.wait_taskq_ready()
        founder = _live(manager, "A", ROOT)
        founder.done = True
        entry = {
            "task": "continue A",
            "parent_session_key": ROOT,
            "conversation_key": "subagent:A",
            "_preassigned_id": "r1",
            "_memory_mode": "incognito",  # no durable row: held in _undurable_in_dispatch
            "_root_session_key": ROOT,
            "_conversation_root_session_key": ROOT,
        }
        real_check = manager._check_agent_off_loop
        held_during_reads: dict[str, object] = {}

        async def _check(*args: object, **kwargs: object) -> object:
            held_during_reads["held"] = manager._continuation_starts.get("subagent:A")
            # The stop lands here: it takes the row out from under the dispatch.
            manager._undurable_in_dispatch.pop("r1", None)
            return await real_check(*args, **kwargs)

        try:
            with (
                _founder_record(ROOT),
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_check_agent_off_loop", _check),
            ):
                info = await manager._dispatch_async(entry)
            assert held_during_reads == {"held": "r1"}
            assert info is None  # the stop already reported the row
            assert "subagent:A" not in manager._continuation_starts
            assert manager._conversation_busy("subagent:A") is None
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_dispatch_under_a_rivals_start_claim_waits_instead_of_caching_the_root(
        self, monkeypatch
    ) -> None:
        """When another start already owns the conversation
        (``_continuation_starts``), its root read is in flight. A second row
        that read and cached the founder's root now would carry it across its
        awaits while that start may contest the conversation and be dismissed;
        the commit-point check would then find no rival and register the row
        on the outdated root. So the row reads nothing and waits for the next
        pass, leaving the rival's claim untouched.
        """
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission.fairness import MEMORY_WAIT_UNTIL_KEY

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        sessions = _sessions({ROOT})
        manager = _manager(sessions, AsyncMock(return_value=True))
        await manager.wait_taskq_ready()
        founder = _live(manager, "A", ROOT)
        founder.done = True
        manager._continuation_starts["subagent:A"] = "r0"  # a rival's start, in flight
        entry = {
            "task": "continue A",
            "parent_session_key": ROOT,
            "conversation_key": "subagent:A",
            "_preassigned_id": "r1",
            "_memory_mode": "incognito",
            "_root_session_key": ROOT,
            "_conversation_root_session_key": ROOT,
        }
        root_read = MagicMock(side_effect=AssertionError("the root must not be read"))
        check = AsyncMock(side_effect=AssertionError("the agent must not be validated"))
        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_durable_conversation_root", root_read),
                patch.object(manager, "_check_agent_off_loop", check),
            ):
                info = await manager._dispatch_async(entry)
            assert info is None
            root_read.assert_not_called()
            check.assert_not_called()
            # The rival still owns the conversation; this row is back in the
            # window, not eligible until the admit wait passes.
            assert manager._continuation_starts == {"subagent:A": "r0"}
            assert [p["_preassigned_id"] for p in manager._queue] == ["r1"]
            assert manager._queue[0][MEMORY_WAIT_UNTIL_KEY] > 0
            assert "r1" not in manager._undurable_in_dispatch
        finally:
            manager._continuation_starts.clear()
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_the_inline_pump_hands_a_refilled_continuation_its_founders_durable_root(
        self,
    ) -> None:
        """The inline pump (no event loop, or the suite's default) reads the
        founder's durable record for a refilled continuation the way the coroutine
        pump does off-loop. Handed nothing, the gate fails closed, so the founding
        chat's OWN continuation would run contested -- prompting for every tool --
        whenever it had waited in the window. A contested record still wins.
        """
        for durable_root, expected in ((ROOT, ROOT), (CONTESTED_A, CONTESTED_A)):
            sessions = _sessions({ROOT})
            manager = _manager(sessions, AsyncMock(return_value=True))
            manager._spawn_stagger_secs = 0.0
            # The refill's own entry: the row's conversation-root stamp is
            # stripped on the read side, so only the durable read can restore it.
            entry = self._entry(
                "subagent:A",
                {
                    "task": "continue A",
                    "parent_session_key": ROOT,
                    "conversation_key": "subagent:A",
                    "_root_session_key": ROOT,
                    "_conversation_root_session_key": ROOT,
                    "_execution_context": execution_for_store("", template_id="").to_record(),
                },
            )
            assert "_conversation_root_session_key" not in entry
            manager._queue.append(entry)

            async def _park(info: SubagentInfo) -> None:  # the admitted run stays LIVE
                await asyncio.Event().wait()

            try:
                with (
                    _founder_record(durable_root),
                    patch("kiro_crew.subagent.Stats"),
                    patch("kiro_crew.subagent.sel"),
                    patch.object(manager, "_run", _park),
                ):
                    manager._drain_queue()
                    await async_wait_until(
                        lambda: "r1" in manager._tasks,
                        describe=lambda: f"tasks={sorted(manager._tasks)!r}",
                    )
                info = manager._agents.get("r1")
                assert info is not None and not info.queued and not info.error, info
                assert info.root_session_key == ROOT
                assert info.conversation_root_session_key == expected, durable_root
            finally:
                await manager.cancel_all()

    @pytest.mark.asyncio
    async def test_recovered_row_is_interactive_even_when_a_re_walk_finds_a_trusted_chat(
        self,
    ) -> None:
        """The exploit path, closed: restart, trusted cross-chat continuation, refill."""
        sessions = _sessions({ROOT})
        approval = AsyncMock(return_value=True)
        manager = _manager(sessions, approval)
        # After the restart the original ``A`` has no record in memory; the
        # trusted chat continued its conversation (its durable record naming ROOT
        # lets that continuation be retained), so a fresh walk of ``subagent:A``
        # yields ROOT -- the answer the recovered row must not be handed.
        with _founder_record(ROOT):
            _continue(manager, "A2", ROOT, "subagent:A")
        assert manager.root_session_key("subagent:A") == ROOT

        # The refill's own entry, re-entering the way the drains do.
        entry = self._entry(
            "subagent:A", {"task": "recovered task", "parent_session_key": "subagent:A"}
        )
        entry.pop("_lane")
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn(**entry, _from_queue=True)
            assert info is not None and not info.queued
            await manager._tasks[info.id]

        assert info.root_session_key == contested_root("subagent:A")
        approval.assert_awaited_once()  # interactive, never the continuing chat's trust
        _rid, description, session_key = approval.await_args.args
        assert session_key == contested_root("subagent:A")
        assert (
            "\nThis run has no single owning chat. Start this task again from a single chat, or approve this request (each request will ask again). Kiro Crew could not confirm that the chat continuing this task is the one that started it."
            in description
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resume_flag", ["_from_queue", "_store_accepted"])
    async def test_a_resumed_admission_without_its_stamp_fails_closed_in_the_gate(
        self, resume_flag: str
    ) -> None:
        """The rule is structural: a re-entry path that forgets the parameter cannot re-walk.

        ``_from_queue`` (the drains) and ``_store_accepted`` (the ``spawn_async``
        re-entry) share one branch and mark a resumed admission; each arm is
        driven on its own so neither can be dropped unnoticed. Without the stamp
        a nested caller resolves to a contested root inside the gate, whatever
        the walk would now find; with the stamp, and for a parentless run,
        nothing changes.
        """

        async def _resumed(**kw: object) -> SubagentInfo:
            # One manager per case: a second live run would meet the fixture's
            # capacity cap and park as a queued placeholder, which carries no stamp.
            manager = _manager(_sessions({ROOT}), AsyncMock(return_value=True))
            with _founder_record(ROOT):
                _continue(manager, "A2", ROOT, "subagent:A")
            assert manager.root_session_key("subagent:A") == ROOT  # the walk would say trusted
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                info = manager.spawn("t", **{resume_flag: True}, **kw)  # type: ignore[arg-type]
                assert info is not None and not info.queued
                await manager._tasks[info.id]
            return info

        resumed = await _resumed(parent_session_key="subagent:A")
        stamped = await _resumed(parent_session_key="subagent:A", _root_session_key=ROOT)
        parentless = await _resumed(parent_session_key="")

        assert resumed.root_session_key == contested_root("subagent:A")
        assert stamped.root_session_key == ROOT
        assert parentless.root_session_key == ""
