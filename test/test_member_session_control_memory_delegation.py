"""A member's private store, and the template it shadows, under ``create_session``.

A crew member's private V2 store is reachable only on authority the caller holds. Naming a
member's agent is a caller-supplied string, so the two admissions are a same-store worker
whose protected execution record names that store, and the owner's own dashboard session; a
cron slot, a member DM slot naming a peer, anything either created, a forged execution
record and a provider template switch that carries a store over are refused
``memory_delegation_denied``. ``TestPrivateStoreCallerIsolation`` pins both directions,
including the vouch a restarted child earns again at its next gate-verified admission and
the durable vouch's sandbox mask. ``TestShadowedTemplateDispatch`` pins
``agent_kind="template"``: a caller dispatches the template a same-named crew member
shadows onto its own store, through ``create_session``, the HTTP route and the real
resolver, while the bare name stays refused and the operator's cause line names the
shared-store condition.

The member DM key, the ``_fresh_create_budget`` fixture and the member-tab builder come from
``member_session_control_helpers.py``; the gate bypass, the ownership boundary, the dispatch
ceiling and the created child's execution identity are pinned in
``test_member_session_control.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state
from member_session_control_helpers import (  # noqa: F401 -- the fixture is requested by name
    _MEMBER,
    _fresh_create_budget_fixture,
    _member_tab,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import SlotOrigin


class TestPrivateStoreCallerIsolation:
    """A member's private store is reachable only on authority the caller holds.

    Naming a member's agent is a caller-supplied string, so it cannot be the
    authority for binding a child onto that member's private V2 store. Two
    admissions, and nothing else: the store is the caller's OWN (read from its
    protected execution record -- the same-store worker), or the caller is the
    owner's own dashboard session. Every population the ownership fence already
    treats as untrusted -- a cron slot, a member DM slot naming a PEER, and
    anything either created -- is refused.

    Both directions are pinned. The refusals are the fix; the admissions are the
    shipped capability the fix must not remove, and a fix that refused them would
    need an RFC.
    """

    def _prepare(self, tmp_path, monkeypatch):
        from pathlib import Path

        from member_memory_helpers import forget_declared_stores, write_member_home

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.sections import ResolvedBindings
        from kiro_crew.execution_context import resolve_member_execution
        from kiro_crew.history import ConversationLog

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        write_member_home(tmp_path, "radar", "peer")
        forget_declared_stores(monkeypatch)
        state = _make_state(tmp_path)
        state.conversation_log = ConversationLog()
        cfg = KiroCrewConfig.load()

        def resolve(_cfg, name, *_args, **_kwargs):
            # A blank name resolves as a TEMPLATE binding on the global store, so
            # an ordinary create stays ordinary: the private authorization must
            # fire on the member selection and on nothing else.
            if not name:
                return ResolvedBindings(
                    workspace_dir=Path("workspace"),
                    memory_store_name="default",
                    effective_memory_config={},
                    kiro_agent="default",
                    selection_kind="template",
                    resolved_alias="",
                )
            selected = resolve_member_execution(_cfg, name)
            return ResolvedBindings(
                workspace_dir=Path("workspace"),
                memory_store_name=selected.store.legacy_name,
                effective_memory_config={},
                kiro_agent=selected.template_id,
                selection_kind="member",
                resolved_alias=name,
                execution_context=selected,
            )

        monkeypatch.setattr(sc, "resolve_agent_bindings", resolve)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda *_: "default")
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        return state, cfg

    def _cron_caller(self, state):
        """A cron job's own tab, with its owning job registered."""
        jobs = list(state.crons.list_jobs.return_value or [])
        jobs.append(SimpleNamespace(id="nightly", created_by="owner"))
        state.crons.list_jobs.return_value = jobs
        return state.get_or_create_slot(
            "cron-nightly", linked_session_key="cron:nightly", origin=SlotOrigin.CRON
        )

    def _member_caller(self, state, cfg):
        """A crew member's DM slot, bound to its own private store."""
        from kiro_crew.execution_context import bind_session_execution, resolve_member_execution

        caller = _member_tab(state)
        execution = resolve_member_execution(cfg, "radar")
        caller.agent = "radar"
        caller.memory_store = execution.store.legacy_name
        bind_session_execution(slot_history_key(caller), execution, vouch=True)
        return caller, execution

    # ── refused: every population the ownership fence distrusts ──────────────

    def test_a_cron_slot_cannot_select_a_members_agent(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        state, _cfg = self._prepare(tmp_path, monkeypatch)
        caller = self._cron_caller(state)

        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(
                sc.create_session(state, caller_session_key=slot_history_key(caller), agent="radar")
            )
        assert error.value.code == "memory_delegation_denied"
        assert error.value.status == 403
        # The refusal must name neither the store nor the member: a caller that
        # guessed an agent name must not have the guess confirmed for it.
        assert "radar" not in error.value.message
        assert "member-" not in error.value.message
        # Refused BEFORE the allocation, so nothing is published or attributed.
        assert state.creator_slot_count(caller.key) == 0

    def test_a_cron_slot_cannot_reach_a_member_through_its_slot_agent(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The SECOND door on the same store: with no `agent` argument the child
        # inherits `caller_slot.agent`, which is editable slot metadata. A check
        # placed on the explicit-selection branch alone would leave this open.
        state, _cfg = self._prepare(tmp_path, monkeypatch)
        caller = self._cron_caller(state)
        caller.agent = "radar"

        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        assert error.value.code == "memory_delegation_denied"
        assert state.creator_slot_count(caller.key) == 0

    def test_an_agent_created_child_cannot_select_a_members_agent(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The deputy case. A created child has a plain `chat-` key, so a
        # prefix-only test reads it as the owner's own tab; `_created_by` is what
        # makes it fenced, and the authorization has to follow that and not the
        # spelling -- otherwise a fenced caller buys the store one hop away.
        state, _cfg = self._prepare(tmp_path, monkeypatch)
        caller = state.get_or_create_slot("chat-9-1789000000")
        caller._created_by = "cron-nightly"

        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(
                sc.create_session(state, caller_session_key=slot_history_key(caller), agent="radar")
            )
        assert error.value.code == "memory_delegation_denied"
        assert state.creator_slot_count(caller.key) == 0

    def test_a_member_cannot_select_a_peers_agent(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # A private caller creates SAME-STORE workers. Its own store is an
        # admission; a peer's is not, so having a private record is not authority
        # over private memory in general.
        state, cfg = self._prepare(tmp_path, monkeypatch)
        caller, _execution = self._member_caller(state, cfg)

        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(
                sc.create_session(state, caller_session_key=slot_history_key(caller), agent="peer")
            )
        assert error.value.code == "memory_delegation_denied"
        assert state.creator_slot_count(caller.key) == 0

    def test_a_carried_fence_verdict_refuses_on_its_own(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The verdict the HTTP gate settled on the caller's VERIFIED scope decides
        # it, without the inline config read. Pinned with a caller the inline
        # predicate would call UNFENCED, so only the carried value can produce the
        # refusal -- which is what keeps a config write landing after admission
        # from widening the surface.
        state, _cfg = self._prepare(tmp_path, monkeypatch)
        caller = state.get_or_create_slot("chat-11-1789000000")
        monkeypatch.setattr(
            sc, "_caller_is_ownership_fenced", lambda *_: pytest.fail("read the config record")
        )

        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(
                sc.create_session(
                    state,
                    caller_session_key=slot_history_key(caller),
                    agent="radar",
                    caller_fenced=True,
                )
            )
        assert error.value.code == "memory_delegation_denied"
        assert state.creator_slot_count(caller.key) == 0

    def test_a_forged_execution_record_does_not_grant_the_own_store_admission(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The own-store admission's AUTHORITY, which is the weight this whole
        # narrowing rests on. A caller's execution record is metadata on its own
        # transcript, so a fenced worker can name a peer's store there and
        # `read_session_execution` will hand that claim back when this process
        # holds no word of its own. Nothing vouches for it, so it is refused.
        #
        # Paired with the test below, which differs ONLY in who wrote the
        # identity: there `bind_session_execution` commits it, so this process
        # vouches and the same create is admitted. That pair is what isolates the
        # vouched term from the record term.
        from kiro_crew.execution_context import resolve_member_execution

        state, cfg = self._prepare(tmp_path, monkeypatch)
        peer_execution = resolve_member_execution(cfg, "radar")
        caller = state.get_or_create_slot("chat-31-1789000000")
        caller._created_by = _MEMBER
        caller.agent = "radar"
        caller.memory_store = peer_execution.store.legacy_name
        monkeypatch.setattr(sc, "read_session_execution", lambda *_a, **_k: peer_execution)

        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(
                sc.create_session(state, caller_session_key=slot_history_key(caller), agent="radar")
            )
        assert error.value.code == "memory_delegation_denied"
        assert state.creator_slot_count(caller.key) == 0

    def test_a_provider_template_switch_does_not_vouch_a_carried_over_store(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The third member of the pair above, and the one the pair did not cover.
        # A provider template event republishes the session's execution by carrying
        # the owner over from `prior` -- and `prior` is the record the session itself
        # writes. So publishing it must not vouch for it: if it did, a fenced caller
        # could forge its record to name a peer's store, trigger a template switch,
        # and have this process vouch for the very claim the record term was supposed
        # to be checked against, collapsing the admission's two sources into one.
        from pathlib import Path

        from kiro_crew import execution_context
        from kiro_crew import session_agent_selection as sas
        from kiro_crew.config.sections import ResolvedBindings
        from kiro_crew.execution_context import (
            bind_session_execution,
            read_session_execution,
            resolve_member_execution,
        )

        state, cfg = self._prepare(tmp_path, monkeypatch)
        peer_execution = resolve_member_execution(cfg, "radar")
        caller = state.get_or_create_slot("chat-33-1789000000")
        caller._created_by = _MEMBER
        caller.agent = "radar"
        caller.memory_store = peer_execution.store.legacy_name
        key = slot_history_key(caller)
        # The forgery itself: the record names the peer's store and NOTHING vouches
        # for it, which is all a session that can rewrite its own metadata achieves.
        bind_session_execution(key, peer_execution, vouch=False)
        execution_context._VOUCHED_EXECUTIONS.clear()
        # The switch resolves the NEW template through config, on the global store.
        # The point of the test is that this resolved store is discarded in favour of
        # the one carried from `prior`, so the resolved value must not be the peer's.
        monkeypatch.setattr(
            sas,
            "resolve_agent_bindings",
            lambda _cfg, name, *_a, **_k: ResolvedBindings(
                workspace_dir=Path("workspace"),
                memory_store_name="default",
                effective_memory_config={},
                kiro_agent=name,
                selection_kind="template",
                resolved_alias=name,
            ),
        )

        sas.record_provider_agent_switch(cfg, key, "radar", "scout", str(tmp_path))

        # The switch published, so the record still names the peer's store.
        assert read_session_execution(key).store.legacy_name == peer_execution.store.legacy_name
        # What it must NOT have done is vouch for it.
        assert execution_context._VOUCHED_EXECUTIONS == {}
        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(sc.create_session(state, caller_session_key=key, agent="radar"))
        assert error.value.code == "memory_delegation_denied"
        assert state.creator_slot_count(caller.key) == 0

    def test_a_vouched_identity_admits_the_create_the_forgery_cannot(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The allow direction of the pair above. Same caller, same store, same
        # agent; the one difference is that the identity is committed through
        # `bind_session_execution`, so this process vouches for it and the record
        # agrees. Without this twin the refusal above could be a blanket break
        # rather than a conditional one.
        from kiro_crew.execution_context import bind_session_execution, resolve_member_execution

        state, cfg = self._prepare(tmp_path, monkeypatch)
        peer_execution = resolve_member_execution(cfg, "radar")
        caller = state.get_or_create_slot("chat-32-1789000000")
        caller._created_by = _MEMBER
        caller.agent = "radar"
        caller.memory_store = peer_execution.store.legacy_name
        bind_session_execution(slot_history_key(caller), peer_execution, vouch=True)

        result = asyncio.run(
            sc.create_session(state, caller_session_key=slot_history_key(caller), agent="radar")
        )
        assert state.get_slot(result["target"]) is not None

    def test_an_owner_tab_pinned_to_a_member_dispatches_its_own_store_worker(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The production binder, not a hand-written vouch. An ordinary `chat-` tab
        # the owner pointed at a member is case (b) of `_member_caller`, so it is
        # fenced and reaches only the own-store admission. That admission needs
        # this process's vouch, and the only thing that ever binds such a tab is
        # `_pin_private_agent_assignment`; if it publishes without vouching, every
        # conductor tab the owner opens on a member is refused for its whole life.
        from kiro_crew.dashboard.chat_persistence import _pin_private_agent_assignment
        from kiro_crew.execution_context import read_vouched_session_execution

        state, cfg = self._prepare(tmp_path, monkeypatch)
        caller = state.get_or_create_slot("chat-34-1789000000")
        key = slot_history_key(caller)
        store = _pin_private_agent_assignment(
            key, "radar", cfg, conversation_log=state.conversation_log
        )
        caller.agent = "radar"
        caller.memory_store = store
        vouched = read_vouched_session_execution(key)
        assert vouched is not None and vouched.store.store_id == store

        result = asyncio.run(sc.create_session(state, caller_session_key=key, agent="radar"))
        assert state.get_slot(result["target"]) is not None

    def test_a_restart_self_heals_own_store_dispatch_at_the_next_gate_admission(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # A restart empties the vouched map in process memory while the durable
        # record survives, so a rehydrated member session's own-store dispatch
        # would otherwise stay refused until its OWNER re-selected the agent. The
        # trust source that closes that gap: a vouch registered at the session's
        # next GATE-VERIFIED admission, keyed on the verified session key, not on
        # anything the record asserts. The member DM caller's key IS
        # `member-<slug>`, the key the HTTP gate authenticated, and its slug is the
        # member id -- a value the session cannot rewrite about itself. So this
        # create re-establishes the vouch and is admitted, with NO owner
        # re-selection.
        from kiro_crew import execution_context
        from kiro_crew.execution_context import read_vouched_session_execution

        state, cfg = self._prepare(tmp_path, monkeypatch)
        caller, execution = self._member_caller(state, cfg)
        # Simulate the restart: the durable record (and the caller's slot store)
        # survive; only this process's vouched word is gone.
        execution_context._VOUCHED_EXECUTIONS.clear()
        assert read_vouched_session_execution(slot_history_key(caller)) is None

        result = asyncio.run(
            sc.create_session(state, caller_session_key=slot_history_key(caller), agent="radar")
        )
        assert state.get_slot(result["target"]) is not None
        # The self-heal re-committed this process's word, keyed on the verified key.
        revouched = read_vouched_session_execution(slot_history_key(caller))
        assert revouched is not None
        assert revouched.store == execution.store

    def test_a_rehydrated_forger_cannot_revouch_a_peers_store(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The self-heal's trust source, SHOWN not asserted.
        # A session that rewrites its own durable record to name a PEER's store
        # must not be re-vouched by the gate-verified path, even after a restart
        # emptied the map. Three forgery shapes, all refused:
        #
        #  (a) an ordinary `chat-` caller whose writable record names the peer
        #      store. Its verified key is not a member DM key, so the re-vouch
        #      never fires -- the record alone is no authority.
        #  (b) a member DM caller (`member-radar`) whose record is forged to a
        #      PEER member's store. The verified key's slug is `radar`; the record
        #      claims `peer`; they disagree, so the re-vouch refuses. The vouch is
        #      keyed on the key the gate verified, never on the member the record
        #      asserts.
        #  (c) a member DM caller (`member-radar`) whose record keeps BOTH
        #      member-id fields as its own slug `radar` but points `store.store_id`
        #      at the PEER's store -- the shape `ExecutionContext.__post_init__`
        #      (which checks only `store.member_id == member_id`) and
        #      `MemoryStoreRef` (which shape-checks the store NAME) both admit. The
        #      re-vouch resolves `radar`'s own store from CONFIG and refuses,
        #      because the record's `store_id` is not the one config says `radar`
        #      owns.
        from kiro_crew import execution_context
        from kiro_crew.execution_context import (
            read_vouched_session_execution,
            resolve_member_execution,
            revouch_at_verified_admission,
        )

        state, cfg = self._prepare(tmp_path, monkeypatch)
        peer_execution = resolve_member_execution(cfg, "peer")

        # (a) ordinary caller, forged record -> the create is refused AND nothing
        # is vouched for it.
        forger = state.get_or_create_slot("chat-51-1789000000")
        forger._created_by = _MEMBER
        forger.agent = "peer"
        forger.memory_store = peer_execution.store.legacy_name
        execution_context._VOUCHED_EXECUTIONS.clear()
        monkeypatch.setattr(sc, "read_session_execution", lambda *_a, **_k: peer_execution)
        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(
                sc.create_session(state, caller_session_key=slot_history_key(forger), agent="peer")
            )
        assert error.value.code == "memory_delegation_denied"
        assert state.creator_slot_count(forger.key) == 0
        assert read_vouched_session_execution(slot_history_key(forger)) is None

        # (b) member DM key, record forged to the PEER's store. The direct call
        # shows the trust source itself: the verified key names `radar`, the
        # forged record names `peer`, so the re-vouch refuses and commits nothing.
        member_key = _MEMBER  # member-radar
        execution_context._VOUCHED_EXECUTIONS.clear()
        assert revouch_at_verified_admission(member_key, peer_execution, cfg) is False
        assert read_vouched_session_execution(member_key) is None

        # (c) member DM key `member-radar`, record with BOTH member-id fields left
        # as its own slug `radar` but `store.store_id` pointed at the PEER's store.
        # This is the shape the member-id agreement check alone would pass; the
        # config store-identity comparison is what refuses it.
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        radar_execution = resolve_member_execution(cfg, "radar")
        forged_store_id = ExecutionContext(
            member_id="radar",
            store=MemoryStoreRef(store_id=peer_execution.store.store_id, member_id="radar"),
            selection_kind="member",
            template_id=radar_execution.template_id,
            memory_mode="persistent",
        )
        # The forged record is a valid ExecutionContext: post-init checks only that
        # store.member_id == member_id, which both hold as "radar".
        assert forged_store_id.member_id == "radar"
        assert forged_store_id.store.member_id == "radar"
        assert forged_store_id.store.store_id == peer_execution.store.store_id
        assert forged_store_id.store.store_id != radar_execution.store.store_id
        execution_context._VOUCHED_EXECUTIONS.clear()
        assert revouch_at_verified_admission(member_key, forged_store_id, cfg) is False
        assert read_vouched_session_execution(member_key) is None
        # The honest record for the same key IS re-vouched, so the refusal above
        # is the store-identity check firing, not a blanket break.
        execution_context._VOUCHED_EXECUTIONS.clear()
        assert revouch_at_verified_admission(member_key, radar_execution, cfg) is True
        assert read_vouched_session_execution(member_key) is not None

    def test_a_fresh_bind_restores_the_own_store_admission_after_a_restart(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # A member re-selecting the agent binds through the durable path, which
        # vouches again, and the same dispatch is admitted. Kept alongside the
        # gate-verified self-heal above: both restore the capability after a
        # restart, one automatically at the next admission and one on an explicit
        # owner re-select, and neither removes it outright.
        from kiro_crew import execution_context
        from kiro_crew.execution_context import bind_session_execution

        state, cfg = self._prepare(tmp_path, monkeypatch)
        caller, execution = self._member_caller(state, cfg)
        execution_context._VOUCHED_EXECUTIONS.clear()
        bind_session_execution(
            slot_history_key(caller), execution, replace_existing=True, vouch=True
        )

        result = asyncio.run(
            sc.create_session(state, caller_session_key=slot_history_key(caller), agent="radar")
        )
        assert state.get_slot(result["target"]) is not None

    # ── admitted: the capability the narrowing must not remove ───────────────

    def test_an_owners_dashboard_slot_still_selects_a_members_agent(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The shipped capability: an owner dispatching a member worker. Removing
        # this would need an RFC, so the narrowing keeps it.
        from kiro_crew.execution_context import read_session_execution

        state, _cfg = self._prepare(tmp_path, monkeypatch)
        caller = state.get_or_create_slot("chat-owner")

        result = asyncio.run(
            sc.create_session(state, caller_session_key=slot_history_key(caller), agent="radar")
        )
        child = state.get_slot(result["target"])
        assert read_session_execution(slot_history_key(child)).member_id == "radar"

    def test_a_member_still_creates_a_same_store_template_child(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The explicit-TEMPLATE selection reaches the same admission as the
        # inherited and the explicit-member routes: the child is on the caller's
        # OWN store with the caller's member id, so the own-store agreement admits
        # a fenced member caller exactly as it does for its same-store worker. Only
        # the selection namespace differs.
        from pathlib import Path

        from kiro_crew.config.sections import ResolvedBindings
        from kiro_crew.execution_context import read_session_execution

        state, cfg = self._prepare(tmp_path, monkeypatch)
        caller, execution = self._member_caller(state, cfg)
        member_resolve = sc.resolve_agent_bindings

        def resolve(_cfg, name, *args, **kwargs):
            if name != "kirocrew-worker":
                return member_resolve(_cfg, name, *args, **kwargs)
            return ResolvedBindings(
                workspace_dir=Path("workspace"),
                memory_store_name="default",
                effective_memory_config={},
                kiro_agent="worker-template",
                selection_kind="template",
                resolved_alias=name,
            )

        monkeypatch.setattr(sc, "resolve_agent_bindings", resolve)
        result = asyncio.run(
            sc.create_session(
                state, caller_session_key=slot_history_key(caller), agent="kirocrew-worker"
            )
        )
        child = state.get_slot(result["target"])
        actual = read_session_execution(slot_history_key(child))
        assert actual.store == execution.store
        assert actual.member_id == execution.member_id
        assert actual.selection_kind == "template"

    def test_a_member_still_creates_a_same_store_worker(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        from kiro_crew.execution_context import read_session_execution

        state, cfg = self._prepare(tmp_path, monkeypatch)
        caller, execution = self._member_caller(state, cfg)

        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        child = state.get_slot(result["target"])
        assert read_session_execution(slot_history_key(child)).store == execution.store

    def test_a_member_may_name_its_own_agent_explicitly(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # Same-store, reached through the EXPLICIT selection branch rather than by
        # inheritance: the admission is the store, not which branch resolved it.
        from kiro_crew.execution_context import read_session_execution

        state, cfg = self._prepare(tmp_path, monkeypatch)
        caller, execution = self._member_caller(state, cfg)

        result = asyncio.run(
            sc.create_session(state, caller_session_key=slot_history_key(caller), agent="radar")
        )
        child = state.get_slot(result["target"])
        assert read_session_execution(slot_history_key(child)).store == execution.store

    def test_a_fenced_caller_still_creates_an_ordinary_worker(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The authorization is about PRIVATE stores only. A cron creating an
        # ordinary global-store worker is the cron operating model, and refusing
        # it would break the surface rather than narrow it.
        from kiro_crew.execution_context import read_session_execution

        state, _cfg = self._prepare(tmp_path, monkeypatch)
        caller = self._cron_caller(state)

        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        child = state.get_slot(result["target"])
        assert child is not None
        assert read_session_execution(slot_history_key(child)).member_id is None

    def test_a_fenced_worker_still_dispatches_its_own_same_store_child(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The nested-conductor design, and the precise intersection the two
        # authorities have to get right: the caller is FENCED (`_created_by` set by
        # its own birth) AND the child is private. The own-store authority admits
        # it, so a grandchild on the same member store is still reachable -- a fix
        # that keyed only on the fence would kill recursive dispatch outright.
        from kiro_crew.execution_context import bind_session_execution, read_session_execution

        state, cfg = self._prepare(tmp_path, monkeypatch)
        _member, execution = self._member_caller(state, cfg)
        worker = state.get_or_create_slot("chat-20-1789000000")
        worker._created_by = _MEMBER
        worker.agent = "radar"
        worker.memory_store = execution.store.legacy_name
        bind_session_execution(slot_history_key(worker), execution, vouch=True)

        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(worker)))
        child = state.get_slot(result["target"])
        assert read_session_execution(slot_history_key(child)).store == execution.store

    def test_a_restart_self_heals_a_member_born_child_at_its_next_gate_admission(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The nested-conductor case the member-DM self-heal does not reach. A
        # member creates a child (a `chat-` key, vouched at birth), the gateway
        # restarts, and the child's own-store dispatch must still be admitted.
        # The child's key is not a member DM key, so its trust source is the
        # durable vouch record the birth wrote in the sandbox-masked vouch directory.
        from kiro_crew import execution_context
        from kiro_crew.execution_context import (
            read_session_execution,
            read_vouched_session_execution,
        )

        state, cfg = self._prepare(tmp_path, monkeypatch)
        member, execution = self._member_caller(state, cfg)
        born = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(member)))
        child = state.get_slot(born["target"])
        child_key = slot_history_key(child)
        assert read_vouched_session_execution(child_key) is not None

        # The restart: only this process's word is gone; records survive.
        execution_context._VOUCHED_EXECUTIONS.clear()
        assert read_vouched_session_execution(child_key) is None

        result = asyncio.run(sc.create_session(state, caller_session_key=child_key))
        grandchild = state.get_slot(result["target"])
        assert read_session_execution(slot_history_key(grandchild)).store == execution.store
        assert read_vouched_session_execution(child_key).store == execution.store

    def test_a_restarted_child_whose_record_moved_to_a_peer_is_not_revouched(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The forgery the durable record must not open. A member-born child that
        # rewrites its own record to name a PEER's store after a restart still
        # disagrees with what the gateway recorded at its birth, so nothing is
        # re-vouched and the create is refused.
        from kiro_crew import execution_context
        from kiro_crew.execution_context import (
            read_vouched_session_execution,
            resolve_member_execution,
        )

        state, cfg = self._prepare(tmp_path, monkeypatch)
        member, _execution = self._member_caller(state, cfg)
        born = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(member)))
        child = state.get_slot(born["target"])
        child_key = slot_history_key(child)
        peer_execution = resolve_member_execution(cfg, "peer")
        child.memory_store = peer_execution.store.legacy_name
        execution_context._VOUCHED_EXECUTIONS.clear()
        monkeypatch.setattr(sc, "read_session_execution", lambda *_a, **_k: peer_execution)

        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(sc.create_session(state, caller_session_key=child_key, agent="peer"))
        assert error.value.code == "memory_delegation_denied"
        assert read_vouched_session_execution(child_key) is None

    def test_the_durable_vouch_is_sandbox_masked_and_a_tightening_withdraws_it(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The durable copy mirrors the in-memory vouch: written where the gateway
        # vouches, in a directory every sandbox masks and no file tool opens,
        # and withdrawn where a deliberate withdrawal happens. A child that turns
        # incognito keeps no restart-surviving authority behind.
        from kiro_crew._durable_vouch import durable_vouch_path, read_durable_vouch
        from kiro_crew.config.paths import data_home
        from kiro_crew.execution_context import bind_session_execution, read_session_execution

        state, cfg = self._prepare(tmp_path, monkeypatch)
        member, execution = self._member_caller(state, cfg)
        born = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(member)))
        child_key = slot_history_key(state.get_slot(born["target"]))

        path = durable_vouch_path(child_key)
        from kiro_crew import sandbox
        from kiro_crew.security import sensitive_home_dirs

        leaf = path.parent.name
        assert path.parent == data_home() / leaf
        # NOT under trust/, which sandboxes keep read-write for the SEL log.
        assert (data_home() / "trust") not in path.parents
        assert leaf in sandbox._CREW_HIDDEN_LEAVES
        assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert any(d.endswith(leaf) for d in sensitive_home_dirs())
        assert read_durable_vouch(child_key)["store"] == execution.to_record()["store"]

        current = read_session_execution(child_key)
        bind_session_execution(child_key, current.with_mode("incognito"), replace_existing=True)
        assert read_durable_vouch(child_key) is None
        assert not path.exists()

    def test_a_cleared_or_deleted_session_leaves_no_durable_vouch(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # Every other withdrawal takes the durable copy with it too: an explicit
        # clear (the hook path's per-request withdrawal, a failed fork) and a
        # deleted transcript. Otherwise each such key leaves a vouch file for good.
        from kiro_crew._durable_vouch import read_durable_vouch
        from kiro_crew.execution_context import clear_session_execution

        state, cfg = self._prepare(tmp_path, monkeypatch)
        member, _execution = self._member_caller(state, cfg)
        member_key = slot_history_key(member)
        first = asyncio.run(sc.create_session(state, caller_session_key=member_key))
        first_key = slot_history_key(state.get_slot(first["target"]))
        second = asyncio.run(sc.create_session(state, caller_session_key=member_key))
        second_key = slot_history_key(state.get_slot(second["target"]))
        assert read_durable_vouch(first_key) is not None
        assert read_durable_vouch(second_key) is not None

        clear_session_execution(first_key)
        assert read_durable_vouch(first_key) is None

        state.conversation_log.append(second_key, "user", "hello")
        assert state.conversation_log.delete_session(second_key)
        assert read_durable_vouch(second_key) is None


class TestShadowedTemplateDispatch:
    """``agent_kind`` lets a caller dispatch the TEMPLATE a same-named crew member shadows.

    The shape is the stock conductor on a legacy install: an older agent sync
    enrolled ``kirocrew-worker`` as a crew member with its own private store,
    while the conductor's own DM thread stayed on the shared store. The conductor's
    charter dispatches ``kirocrew-worker`` for every leaf item; a bare name selects
    the MEMBER, and a shared-store member thread may not bind that member's store,
    so the create is refused ``memory_delegation_denied`` -- for a worker that
    needs no memory of its own. ``agent_kind="template"`` is the way to say
    "the template, on my own memory", exactly as the chat create route's explicit
    template pick never pins member memory. These pin:

    * the bare name is still refused (the fence is untouched);
    * the server-side cause names the shared-store condition, not a lost vouch;
    * the template kind creates the child on the caller's own store with the
      template persona, and records the namespace on the slot and at birth;
    * a stated kind is validated (the closed set is ``template`` alone -- a bare
      name already selects the member, so no ``member`` kind exists -- requires
      ``agent``, never falls back to whoever answers by default);
    * the REAL resolver, not the stand-in the other tests swap in, resolves a
      member-shadowed name as the template under the stated kind.
    """

    _WORKER_TEMPLATE = "worker-template"

    def _prepare(self, tmp_path, monkeypatch, *, fake_resolver=True):
        from pathlib import Path

        from member_memory_helpers import forget_declared_stores, write_member_home

        from kiro_crew.config.sections import ResolvedBindings
        from kiro_crew.execution_context import (
            ExecutionContext,
            MemoryStoreRef,
            bind_session_execution,
            resolve_member_execution,
        )
        from kiro_crew.history import ConversationLog

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        # `worker` is the crew member the old sync enrolled with a private store.
        write_member_home(tmp_path, "worker")
        forget_declared_stores(monkeypatch)
        state = _make_state(tmp_path)
        state.conversation_log = ConversationLog()

        # The conductor: a crew member's DM thread whose record is on the SHARED
        # store -- the id-less member shape a Global-V1 crewmate carries.
        caller = _member_tab(state)
        caller.agent = "conductor"
        caller.memory_store = "default"
        conductor = ExecutionContext(
            None,
            MemoryStoreRef("default"),
            "member",
            "conductor-template",
            selection_name="conductor",
        )
        bind_session_execution(slot_history_key(caller), conductor)

        template_name = self._WORKER_TEMPLATE
        calls: list[str] = []

        def resolve(_cfg, name, *_args, selection_kind="", **_kwargs):
            calls.append(selection_kind)
            if name == "worker" and selection_kind != "template":
                selected = resolve_member_execution(_cfg, "worker")
                return ResolvedBindings(
                    workspace_dir=Path("workspace"),
                    memory_store_name=selected.store.legacy_name,
                    effective_memory_config={},
                    kiro_agent=selected.template_id,
                    selection_kind="member",
                    resolved_alias="worker",
                    execution_context=selected,
                )
            if name == "worker":
                # The same name as a TEMPLATE: what `_resolve_agent_selection`
                # answers for `selection_kind="template"` -- the installed spec,
                # on the default store, with the alias hit skipped.
                return ResolvedBindings(
                    workspace_dir=Path("workspace"),
                    memory_store_name="default",
                    effective_memory_config={},
                    kiro_agent=template_name,
                    selection_kind="template",
                    resolved_alias="",
                    requested_resolved=True,
                )
            # Any other name under a stated kind does not resolve; a bare name
            # falls to the default template as the real resolver does.
            return ResolvedBindings(
                workspace_dir=Path("workspace"),
                memory_store_name="default",
                effective_memory_config={},
                kiro_agent="default",
                selection_kind="template",
                resolved_alias="",
                requested_resolved=not bool(selection_kind),
            )

        if fake_resolver:
            monkeypatch.setattr(sc, "resolve_agent_bindings", resolve)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda *_: "default")
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        return state, caller, conductor, calls

    def test_the_bare_name_is_still_refused_and_the_cause_names_the_shared_store(
        self, tmp_path, monkeypatch, _fresh_create_budget, caplog
    ):
        # The fence is untouched: a shared-store member thread naming a member
        # with private memory is a peer-member dispatch and stays refused. What
        # changes is the operator's log line -- this caller is never vouched, so
        # "no vouched identity" pointed at a re-bind that cannot help.
        import logging

        state, caller, _conductor, _calls = self._prepare(tmp_path, monkeypatch)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.session_control"):
            with pytest.raises(sc.SessionControlError) as error:
                asyncio.run(
                    sc.create_session(
                        state, caller_session_key=slot_history_key(caller), agent="worker"
                    )
                )
        assert error.value.code == "memory_delegation_denied"
        assert error.value.status == 403
        # The wire refusal still names neither the store nor the member.
        assert "worker" not in error.value.message
        assert "member-" not in error.value.message
        assert state.creator_slot_count(caller.key) == 0
        causes = [
            r.getMessage() for r in caplog.records if "memory delegation refused" in r.getMessage()
        ]
        assert len(causes) == 1
        assert "shared store" in causes[0]
        assert "template" in causes[0]
        assert "no vouched identity" not in causes[0]
        # The log is not a second disclosure channel either.
        assert "member-worker" not in causes[0]

    def test_a_v1_member_on_its_own_store_is_not_logged_as_a_shared_store_caller(
        self, tmp_path, monkeypatch, _fresh_create_budget, caplog
    ):
        # A missing id alone is not the shared-store condition. A memory_version-1
        # crew member bound to its own NAMED store resolves with ``member_id=None``
        # too (no persisted identity), while its store is the private one it
        # declares, so the cause line must not describe that caller as running on
        # the shared store. The bare-name dispatch of a V2 member stays refused.
        import json
        import logging

        from member_memory_helpers import forget_declared_stores

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.execution_context import bind_session_execution, resolve_member_execution

        state, caller, _conductor, _calls = self._prepare(tmp_path, monkeypatch)
        # Declare the conductor as a V1 crew member on its own named store, beside
        # the V2 ``worker`` member `_prepare` enrolled. A V1 declaration names no
        # owner and its row persists no id; `require_memory_store` refuses either.
        config_path = tmp_path / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["memory_stores"]["conductor-notes"] = {"memory_version": 1}
        config["agents"]["conductor"] = {"memory_store": "conductor-notes"}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        forget_declared_stores(monkeypatch)
        legacy = resolve_member_execution(KiroCrewConfig.load(), "conductor")
        # The exact shape the cause must not misname: id-less, on a private store.
        assert legacy.member_id is None
        assert legacy.store.store_id == "conductor-notes"
        caller.memory_store = legacy.store.legacy_name
        bind_session_execution(slot_history_key(caller), legacy, replace_existing=True)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.session_control"):
            with pytest.raises(sc.SessionControlError) as error:
                asyncio.run(
                    sc.create_session(
                        state, caller_session_key=slot_history_key(caller), agent="worker"
                    )
                )
        assert error.value.code == "memory_delegation_denied"
        assert error.value.status == 403
        assert "worker" not in error.value.message
        assert "conductor-notes" not in error.value.message
        assert state.creator_slot_count(caller.key) == 0
        causes = [
            r.getMessage() for r in caplog.records if "memory delegation refused" in r.getMessage()
        ]
        assert len(causes) == 1
        assert "shared store" not in causes[0]
        # This caller is never vouched (`bind_session_execution` vouches only a
        # truthy member id), so the branch order falls to the vouch cause for it.
        assert "no vouched identity" in causes[0]
        # The log is not a second disclosure channel either.
        assert "member-worker" not in causes[0]
        assert "conductor-notes" not in causes[0]

    def test_a_bare_stock_worker_pick_takes_the_template_on_the_callers_store(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The legacy-install shape with the conductor omitting `agent_kind`: the
        # stock worker's name is shadowed by a private-memory crew member. The
        # bare name must still dispatch the TEMPLATE on the caller's own store
        # instead of being refused `memory_delegation_denied`.
        from kiro_crew.dashboard.session_control_parts import session_create
        from kiro_crew.execution_context import MemoryStoreRef, read_session_execution

        assert session_create._STOCK_WORKER_AGENT == "kirocrew-worker"
        state, caller, _conductor, calls = self._prepare(tmp_path, monkeypatch)
        # `_prepare` enrolls the shadowing member as `worker`; name it the stock worker.
        monkeypatch.setattr(session_create, "_STOCK_WORKER_AGENT", "worker")

        result = asyncio.run(
            sc.create_session(state, caller_session_key=slot_history_key(caller), agent="worker")
        )
        assert calls == ["template"]
        child = state.get_slot(result["target"])
        assert child.agent_kind == "template"
        actual = read_session_execution(slot_history_key(child), required=True)
        assert actual.store == MemoryStoreRef("default")
        assert actual.member_id is None
        assert actual.template_id == self._WORKER_TEMPLATE

    def test_the_stock_worker_default_leaves_other_member_names_name_first(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The default is for the stock worker's name ONLY. Any other private
        # member named bare still resolves as the member and stays refused, so
        # member-to-member dispatch keeps its name-first resolution.
        state, caller, _conductor, calls = self._prepare(tmp_path, monkeypatch)

        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(
                sc.create_session(
                    state, caller_session_key=slot_history_key(caller), agent="worker"
                )
            )
        assert error.value.code == "memory_delegation_denied"
        assert calls == [""]

    def test_the_template_kind_dispatches_the_worker_on_the_callers_own_store(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        from kiro_crew.execution_context import MemoryStoreRef, read_session_execution

        state, caller, conductor, calls = self._prepare(tmp_path, monkeypatch)

        result = asyncio.run(
            sc.create_session(
                state,
                caller_session_key=slot_history_key(caller),
                agent="worker",
                agent_kind="template",
            )
        )
        # The resolver was asked in the stated namespace.
        assert "template" in calls
        child = state.get_slot(result["target"])
        assert child is not None
        assert child.agent == "worker"
        # The namespace the pick resolved in, on the slot and in the birth record.
        assert child.agent_kind == "template"
        metadata = state.conversation_log.get_metadata(slot_history_key(child))
        assert metadata["agent"] == "worker"
        assert metadata["agent_kind"] == "template"
        # The global store, in both spellings the record keeps for it.
        assert metadata.get("memory_store", "") == ""
        # The child runs the worker TEMPLATE on the conductor's own (shared)
        # store; the member's private store is never touched. The id-less member
        # caller keeps its selection and takes only the template, as the
        # unshadowed template arm already does for that caller shape.
        actual = read_session_execution(slot_history_key(child), required=True)
        assert actual.store == MemoryStoreRef("default")
        assert actual.member_id is None
        assert actual.template_id == self._WORKER_TEMPLATE
        assert actual.selection_kind == conductor.selection_kind
        assert actual.selection_name == conductor.selection_name
        assert child.memory_store in ("", "default")
        assert state.creator_slot_count(caller.key) == 1

    def test_the_template_kind_rides_the_http_route(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        import json

        from member_memory_helpers import make_request

        from kiro_crew.dashboard.handlers.session_control import api_session_control_create

        state, caller, _conductor, _calls = self._prepare(tmp_path, monkeypatch)

        async def create(body):
            return await api_session_control_create(
                make_request(
                    state,
                    "/api/session-control/create",
                    body=body,
                    internal=True,
                    session=slot_history_key(caller),
                )
            )

        refused = asyncio.run(create({"agent": "worker"}))
        assert refused.status == 403, refused.text
        assert json.loads(refused.text)["code"] == "memory_delegation_denied"

        response = asyncio.run(create({"agent": "worker", "agent_kind": "template"}))
        assert response.status == 200, response.text
        child = state.get_slot(json.loads(response.text)["target"])
        assert child.agent_kind == "template"

        bad_type = asyncio.run(create({"agent": "worker", "agent_kind": 1}))
        assert bad_type.status == 400
        assert json.loads(bad_type.text)["code"] == "invalid_field_type"

    def test_a_stated_kind_is_validated_and_never_falls_back(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        state, caller, _conductor, calls = self._prepare(tmp_path, monkeypatch)
        key = slot_history_key(caller)

        # The closed set is "template" alone. "member" is not a kind this route
        # takes -- a bare name already selects the member, so the value would only
        # restate that resolution -- and nothing else is either. Refused before the
        # resolver is consulted.
        for kind in ("member", "crew"):
            with pytest.raises(sc.SessionControlError) as error:
                asyncio.run(
                    sc.create_session(
                        state, caller_session_key=key, agent="worker", agent_kind=kind
                    )
                )
            assert error.value.code == "invalid_agent_kind", kind
            assert '"template"' in error.value.message
        assert calls == []

        # A kind qualifies an explicit selection; the inherited caller agent is
        # not one, so the request is refused rather than silently applied.
        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(sc.create_session(state, caller_session_key=key, agent_kind="template"))
        assert error.value.code == "agent_kind_requires_agent"

        # A name that does not resolve in the stated namespace is refused, not
        # answered by whoever resolves by default -- and the refusal names the
        # namespace so the caller can tell "no such template" from "no such agent".
        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(
                sc.create_session(
                    state, caller_session_key=key, agent="nobody", agent_kind="template"
                )
            )
        assert error.value.code == "agent_unresolved"
        assert "template" in error.value.message
        assert state.creator_slot_count(caller.key) == 0

    def test_the_real_resolver_resolves_a_member_shadowed_name_as_the_template(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        """The REAL ``resolve_agent_bindings``, against the shape the class describes.

        Every other test here swaps the resolver for a stand-in, so none of them
        shows that the real one answers ``selection_kind="template"`` for a name
        a crew member shadows. This one does: the config ``_prepare`` writes
        carries ``worker`` as a crew member bound to its own private V2 store
        (``member-worker``), and the installed-template catalog -- the one
        external boundary, seeded the way the loader's own tests seed it --
        declares a template of the same name. The bare name must still select
        the member and be refused; the stated kind must create the child on the
        caller's shared store, never the private one.
        """
        from kiro_crew.config import loader
        from kiro_crew.execution_context import MemoryStoreRef, read_session_execution

        state, caller, conductor, _calls = self._prepare(tmp_path, monkeypatch, fake_resolver=False)
        assert sc.resolve_agent_bindings is loader.resolve_agent_bindings
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"worker"}))
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
        key = slot_history_key(caller)

        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(sc.create_session(state, caller_session_key=key, agent="worker"))
        assert error.value.code == "memory_delegation_denied"
        assert state.creator_slot_count(caller.key) == 0

        result = asyncio.run(
            sc.create_session(state, caller_session_key=key, agent="worker", agent_kind="template")
        )
        child = state.get_slot(result["target"])
        assert child is not None
        assert child.agent == "worker"
        assert child.agent_kind == "template"
        assert child.memory_store in ("", "default")
        actual = read_session_execution(slot_history_key(child), required=True)
        assert actual.store == MemoryStoreRef("default")
        assert actual.store == conductor.store
        assert actual.store.legacy_name != "member-worker"
        assert actual.member_id is None
        # The real passthrough: the template IS the name, grafted onto the
        # caller's own store, as an unshadowed template child already is.
        assert actual.template_id == "worker"
        assert state.creator_slot_count(caller.key) == 1
