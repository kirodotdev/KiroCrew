"""A sub-agent at any depth inherits the root chat's trust and routes to its tab.

A chat the operator trusts runs its DIRECT sub-agents under that trust already. These
tests cover the deeper levels: a sub-agent spawned by a sub-agent (depth 2+) must read
the trust of the chat (or cron/channel) at the ROOT of its spawn tree, and its prompts
and cards must surface on that root tab.

The spawn gate resolves the root ONCE at admission by following ``parent_session_key``
through sub-agent records until a non-``subagent:`` key, stamps it on the run as
``trust_root_key``, and reads that stamp for the trust decision; the run loop reads the
same stamp for the tool-approval policy, and the card router reads it to pick the tab.
Trust never exceeds the root's: an untrusted root stays untrusted at every depth, and a
broken or unprovable chain fails closed to untrusted.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from subagent_helpers import _mock_ctx_builder, _mock_sessions

from kiro_crew.execution_context import execution_for_store
from kiro_crew.subagent import SubagentInfo, SubagentManager

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_ROOT = "dashboard:root"


def _sessions(policies: dict[str, str]) -> MagicMock:
    """A session double whose approval policy is keyed per session key.

    ``get_approval_policy`` returns ``""`` (untrusted) for an unknown key — which
    is what a ``subagent:<id>`` parent key always is, and the whole point of the
    bug: reading trust off that key can never see the root's ``"auto"``.
    """
    sessions = _mock_sessions()
    sessions.get_approval_policy = MagicMock(side_effect=lambda k: policies.get(k, ""))
    sessions.has_session = MagicMock(
        side_effect=lambda k: k in policies or k.startswith("subagent:")
    )
    return sessions


def _manager(policies: dict[str, str]):
    """A manager whose only auto-approval source is parent/root trust.

    ``_mock_ctx_builder`` leaves ``auto_approve_subagent_spawn`` False and there is
    no ``_on_spawn_approval`` callback, so a spawn starts a task (``error == ""``)
    ONLY when the root trust gate admits it; otherwise it ends "spawn rejected".
    """
    return SubagentManager(sessions=_sessions(policies), ctx_builder=_mock_ctx_builder())


def _level1(manager: SubagentManager, *, root: str, trust_root: str | None = None) -> SubagentInfo:
    """A live depth-1 sub-agent of *root*, stamped as admission would stamp it."""
    rec = SubagentInfo(
        execution_context=execution_for_store(""),
        id="lvl1aaaa",
        task="level one",
        parent_session_key=root,
        depth=1,
        trust_root_key=root if trust_root is None else trust_root,
    )
    manager._agents[rec.id] = rec
    return rec


async def _spawn(manager: SubagentManager, *, parent_session_key: str) -> SubagentInfo:
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("child work", parent_session_key=parent_session_key)
        assert info is not None
        if info.id in manager._tasks:
            await manager._tasks[info.id]
    return info


class TestNestedSpawnInheritsRootTrust:
    @pytest.mark.asyncio
    async def test_1_trusted_root_makes_a_depth2_run_trusted_and_root_stamped(self) -> None:
        """A depth-2 child of a TRUSTED root is admitted (not rejected) and its
        resolved root is the chat key, so its prompt routes to the root tab."""
        manager = _manager({_ROOT: "auto"})
        lvl1 = _level1(manager, root=_ROOT)
        info = await _spawn(manager, parent_session_key=f"subagent:{lvl1.id}")
        assert info.depth == 2
        assert info.error == ""  # trusted — admitted without an approval prompt
        assert info.trust_root_key == _ROOT

    @pytest.mark.asyncio
    async def test_a_direct_child_of_a_trusted_root_is_unchanged(self) -> None:
        """First-level behaviour: a direct child's root IS its parent key."""
        manager = _manager({_ROOT: "auto"})
        info = await _spawn(manager, parent_session_key=_ROOT)
        assert info.depth == 1
        assert info.error == ""
        assert info.trust_root_key == _ROOT

    @pytest.mark.asyncio
    async def test_2_untrusted_root_keeps_a_depth2_run_untrusted(self) -> None:
        """An UNTRUSTED root never grants trust at any depth: the nested run is
        rejected (no approval mechanism) exactly as a direct child would be."""
        manager = _manager({})  # root carries no "auto" policy
        lvl1 = _level1(manager, root=_ROOT)
        info = await _spawn(manager, parent_session_key=f"subagent:{lvl1.id}")
        assert info.depth == 2
        assert info.trust_root_key == _ROOT
        assert "spawn rejected" in info.error  # untrusted → not auto-approved

    @pytest.mark.asyncio
    async def test_3_broken_parent_chain_fails_closed_to_untrusted(self) -> None:
        """A ``subagent:`` parent whose run record is absent (evicted, lost to a
        restart) cannot prove a root: the stamp is empty and the run stays
        untrusted even though the root chat is trusted."""
        manager = _manager({_ROOT: "auto"})
        # No level-1 record registered → the chain dead-ends at subagent:gone.
        info = await _spawn(manager, parent_session_key="subagent:gone0001")
        assert info.depth == 2
        assert info.trust_root_key == ""  # fail closed
        assert "spawn rejected" in info.error

    @pytest.mark.asyncio
    async def test_a_level1_with_an_empty_stamp_does_not_leak_trust_to_its_child(
        self,
    ) -> None:
        """A level-1 whose OWN root was unprovable (empty stamp) cannot hand a
        root to its child: the child stays untrusted even under a trusted root."""
        manager = _manager({_ROOT: "auto"})
        _level1(manager, root=_ROOT, trust_root="")  # its own chain was broken
        info = await _spawn(manager, parent_session_key="subagent:lvl1aaaa")
        assert info.trust_root_key == ""
        assert "spawn rejected" in info.error

    @pytest.mark.asyncio
    async def test_a_depth3_run_reads_the_root_through_two_levels(self) -> None:
        """The stamp is transitive: each level fixes its root from the level
        above, so a depth-3 run reads the same root as depth 1."""
        manager = _manager({_ROOT: "auto"})
        lvl1 = _level1(manager, root=_ROOT)
        lvl2 = SubagentInfo(
            execution_context=execution_for_store(""),
            id="lvl2bbbb",
            task="level two",
            parent_session_key=f"subagent:{lvl1.id}",
            depth=2,
            trust_root_key=_ROOT,  # stamped when lvl2 was admitted
        )
        manager._agents[lvl2.id] = lvl2
        info = await _spawn(manager, parent_session_key=f"subagent:{lvl2.id}")
        assert info.depth == 3
        assert info.trust_root_key == _ROOT
        assert info.error == ""


class TestResolveParentPolicyReadsRoot:
    """The run loop's tool-approval inheritance reads the same stamped root."""

    def _coordinator(self, policies: dict[str, str]):
        manager = _manager(policies)
        coord = SimpleNamespace(_manager=manager)
        return coord

    def test_nested_run_inherits_the_root_policy_not_the_subagent_parent(self) -> None:
        from kiro_crew.subagent_manager.turn_session import resolve_parent_policy

        coord = self._coordinator({_ROOT: "auto"})
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="runaaaa1",
            task="nested",
            parent_session_key="subagent:lvl1aaaa",  # carries no policy
            depth=2,
            trust_root_key=_ROOT,
        )
        with patch("kiro_crew.subagent.sel"):
            assert resolve_parent_policy(coord, info) == "auto"

    def test_broken_chain_run_gets_no_policy_from_the_root(self) -> None:
        from kiro_crew.subagent_manager.turn_session import resolve_parent_policy

        coord = self._coordinator({_ROOT: "auto"})
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="runaaaa2",
            task="orphan",
            parent_session_key="subagent:gone",
            depth=2,
            trust_root_key="",  # unprovable → fail closed
        )
        # yolo / global-config / auto-approve fallbacks all off on this manager.
        coord._manager._is_yolo = lambda: False
        coord._manager._global_approval_mode = ""
        with patch("kiro_crew.subagent.sel"):
            assert resolve_parent_policy(coord, info) == ""


class TestCardRoutesToRoot:
    """A nested run's file card addresses the root tab, not its subagent parent."""

    def _state_with(self, *infos: SimpleNamespace):
        manager = SimpleNamespace(all_agents=list(infos))
        return SimpleNamespace(subagents=manager)

    def test_nested_card_routes_to_the_root(self) -> None:
        from kiro_crew.dashboard.handlers.files import _subagent_parent_session_key

        nested = SimpleNamespace(
            id="nest0001",
            conversation_key="",
            parent_session_key="subagent:lvl1aaaa",
            trust_root_key=_ROOT,
            done=False,
            started=10.0,
        )
        state = self._state_with(nested)
        assert _subagent_parent_session_key(state, "subagent:nest0001") == _ROOT

    def test_direct_child_card_still_routes_to_its_parent(self) -> None:
        from kiro_crew.dashboard.handlers.files import _subagent_parent_session_key

        direct = SimpleNamespace(
            id="dir00001",
            conversation_key="",
            parent_session_key=_ROOT,
            trust_root_key=_ROOT,
            done=False,
            started=10.0,
        )
        state = self._state_with(direct)
        assert _subagent_parent_session_key(state, "subagent:dir00001") == _ROOT

    def test_record_without_a_stamp_falls_back_to_the_parent(self) -> None:
        from kiro_crew.dashboard.handlers.files import _subagent_parent_session_key

        unstamped = SimpleNamespace(
            id="old00001",
            conversation_key="",
            parent_session_key=_ROOT,
            trust_root_key="",  # no stamp recorded
            done=False,
            started=10.0,
        )
        state = self._state_with(unstamped)
        assert _subagent_parent_session_key(state, "subagent:old00001") == _ROOT


class TestContinuationCarriesRootOnlyWhenParentUnchanged:
    """A continuation keeps the run's root only when it keeps the run's parent.

    The dashboard owner may continue a resumable run under a DIFFERENT parent
    session. Carrying the original root there would run the new turn under the
    previous chat's trust and route its cards to the previous chat, so the
    continuation prelude passes the stamp to ``spawn`` only when the parent is
    unchanged; a changed parent passes ``None`` and lets admission re-resolve
    the root from the new parent.
    """

    def _mgr_with_run(self, *, parent: str, root: str) -> SubagentManager:
        sessions = _sessions({_ROOT: "auto", "dashboard:other": "auto"})
        mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder())
        mgr._agents["conv1"] = SubagentInfo(
            execution_context=execution_for_store(""),
            id="conv1",
            task="t",
            parent_session_key=parent,
            depth=1,
            trust_root_key=root,
        )
        return mgr

    def _captured_continue(self, mgr: SubagentManager, *, new_parent: str) -> dict:
        import kiro_crew.subagent as _sa

        captured: dict = {}
        with (
            patch.object(mgr, "_conversation_busy", lambda _k: None),
            patch.object(mgr._sessions, "resumable_sid", lambda _k: "sid-1", create=True),
            patch.object(mgr, "_promote_conversation", lambda *_a: None),
            patch.object(mgr, "spawn", lambda *_a, **kw: captured.update(kw)),
            patch.object(_sa, "read_state", lambda _id: {}, create=True),
        ):
            mgr.continue_conversation("conv1", "follow up", parent_session_key=new_parent)
        return captured

    def test_same_parent_continuation_carries_the_root(self) -> None:
        mgr = self._mgr_with_run(parent=_ROOT, root=_ROOT)
        captured = self._captured_continue(mgr, new_parent=_ROOT)
        assert captured.get("_trust_root_key") == _ROOT

    def test_changed_parent_continuation_does_not_carry_the_old_root(self) -> None:
        mgr = self._mgr_with_run(parent=_ROOT, root=_ROOT)
        captured = self._captured_continue(mgr, new_parent="dashboard:other")
        # The old root must NOT ride along; admission re-resolves from the new
        # parent (passing None here).
        assert captured.get("_trust_root_key") is None


class TestResolveTrustRootIsUnambiguous:
    """``_resolve_trust_root`` picks one agreed root or fails closed.

    One ``subagent:<id>`` key can match more than one record: the original run
    and a continuation of it from a DIFFERENT chat, each carrying its own root.
    A live match wins over a finished one; two matching roots that disagree are
    ambiguous and resolve to ``""`` (untrusted) rather than guessing, because a
    wrong guess auto-approves under a chat the run does not belong to — in
    Shared Crew, another person's chat.
    """

    def _mgr(self) -> SubagentManager:
        return SubagentManager(sessions=_sessions({}), ctx_builder=_mock_ctx_builder())

    def _record(self, mgr, *, id, root, parent, done, conv="subagent:A"):
        mgr._agents[id] = SubagentInfo(
            execution_context=execution_for_store(""),
            id=id,
            task="t",
            parent_session_key=parent,
            conversation_key=conv,
            depth=1,
            trust_root_key=root,
            done=done,
        )

    def test_single_match_resolves_its_root(self) -> None:
        mgr = self._mgr()
        self._record(mgr, id="A", root=_ROOT, parent=_ROOT, done=False)
        assert mgr._admission._resolve_trust_root("subagent:A") == _ROOT

    def test_two_finished_records_with_different_roots_fail_closed(self) -> None:
        """The exact retry edge: original (root P1) and a cross-chat continuation
        (root P2) both done and both matching subagent:A -> ambiguous -> ''."""
        mgr = self._mgr()
        self._record(mgr, id="A", root="dashboard:P1", parent="dashboard:P1", done=True)
        self._record(mgr, id="B", root="dashboard:P2", parent="dashboard:P2", done=True)
        assert mgr._admission._resolve_trust_root("subagent:A") == ""

    def test_a_live_match_wins_over_a_finished_one(self) -> None:
        mgr = self._mgr()
        self._record(mgr, id="A", root="dashboard:P1", parent="dashboard:P1", done=True)
        self._record(mgr, id="B", root="dashboard:P2", parent="dashboard:P2", done=False)
        assert mgr._admission._resolve_trust_root("subagent:A") == "dashboard:P2"

    def test_two_live_records_with_different_roots_fail_closed(self) -> None:
        mgr = self._mgr()
        self._record(mgr, id="A", root="dashboard:P1", parent="dashboard:P1", done=False)
        self._record(mgr, id="B", root="dashboard:P2", parent="dashboard:P2", done=False)
        assert mgr._admission._resolve_trust_root("subagent:A") == ""

    def test_a_live_trusted_root_beside_a_live_empty_stamp_fails_closed(self) -> None:
        """A live match whose own chain was unprovable (empty stamp) sitting
        beside a live match with a root makes the key ambiguous: trusting the
        root would grant trust through the record that cannot trace it, so the
        deciding tier fails closed."""
        mgr = self._mgr()
        self._record(mgr, id="A", root="dashboard:P1", parent="dashboard:P1", done=False)
        self._record(mgr, id="B", root="", parent="subagent:gone", done=False)
        assert mgr._admission._resolve_trust_root("subagent:A") == ""

    def test_a_done_empty_stamp_does_not_block_a_live_root(self) -> None:
        """An empty stamp in the NON-deciding (finished) tier is ignored: a live
        match with a root still resolves, because live records override finished
        ones outright."""
        mgr = self._mgr()
        self._record(mgr, id="A", root="dashboard:P1", parent="dashboard:P1", done=False)
        self._record(mgr, id="B", root="", parent="subagent:gone", done=True)
        assert mgr._admission._resolve_trust_root("subagent:A") == "dashboard:P1"

    def test_a_done_root_beside_a_done_empty_stamp_fails_closed(self) -> None:
        """When finished records are the deciding tier, an empty stamp among them
        fails closed just as a live empty stamp does."""
        mgr = self._mgr()
        self._record(mgr, id="A", root="dashboard:P1", parent="dashboard:P1", done=True)
        self._record(mgr, id="B", root="", parent="subagent:gone", done=True)
        assert mgr._admission._resolve_trust_root("subagent:A") == ""

    def test_matches_agreeing_on_one_root_resolve_it(self) -> None:
        mgr = self._mgr()
        self._record(mgr, id="A", root=_ROOT, parent=_ROOT, done=True)
        self._record(mgr, id="B", root=_ROOT, parent=_ROOT, done=False)
        assert mgr._admission._resolve_trust_root("subagent:A") == _ROOT


class TestRetryCarriesTheResolvedRoot:
    """A retry of a failed run forwards the run's resolved root to ``spawn``,
    carried like its depth, so re-resolution off an ambiguous parent key cannot
    change which chat the retried run trusts.

    ``_retry_failed_run`` runs on ``handlers.messaging``'s globals, so the spawn
    entry it reaches is ``handlers.messaging._spawn_on_loop``. Patching it to
    capture and then stop (by returning a sentinel the caller's post-spawn
    settle treats as "nothing started") isolates the one thing under test: the
    kwargs the retry hands to spawn.
    """

    @pytest.mark.asyncio
    async def test_retry_passes_the_old_runs_trust_root(self) -> None:
        from kiro_crew.dashboard.handlers import messaging as _messaging
        from kiro_crew.dashboard.messaging_api import run_control

        captured: dict = {}

        async def _fake_spawn_on_loop(_state, task, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id="new1", error="", done=False)

        old = SimpleNamespace(
            id="x1",
            task="t",
            _raw_task="t",
            parent_session_key="subagent:A",
            agent="",  # empty -> skips warm_project_agents_for_spawn
            max_turns=0,
            cwd="",
            model="",
            reasoning_effort="",
            backend=None,
            approval_mode="",
            silent=False,
            delegation={},
            include_memory=True,
            include_lessons=True,
            include_project=True,
            memory_store="",
            crew="",
            depth=2,
            trust_root_key="dashboard:P2",
            execution_context=SimpleNamespace(
                app="", memory_mode="persistent", to_record=lambda: {}
            ),
        )
        # The post-spawn settle (settle_retry / completion bookkeeping) is out
        # of scope; give it enough of a double not to raise, so the one thing
        # under test is the kwargs captured above.
        state = SimpleNamespace(subagents=MagicMock())
        with patch.object(_messaging, "_spawn_on_loop", _fake_spawn_on_loop, create=True):
            with contextlib.suppress(Exception):
                await run_control._retry_failed_run(state, "x1", old, admission=None)
        assert captured.get("_trust_root_key") == "dashboard:P2"
        assert captured.get("_depth") == 2
