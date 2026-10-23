"""Pins what ``create_session`` refuses and what the created child inherits.

Covers the caller-class refusals, the inherited workspace and agent, the workspace
project directory, the binding check when no agent is named, the sanitized agent
name and a template selection that survives member discovery. The private-member
cases pin a child bound to the creator's memory store, the caller recheck after
delegation, and a memory assignment written before birth history that a failed or
cancelled birth keeps; a private worker cannot adopt an old session.

The trust cases pin that ``_trust`` and ``_trust_reads`` follow the grant held at
allocation, that neither ``_trusted_patterns`` nor ``_trust_scope`` reaches the
child, and that the create audit records which grants the child inherited.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _agent_resolves,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.config import loader
from kiro_crew.dashboard import session_control as sc
from kiro_crew.testing.wait import await_until


def test_create_refuses_the_caller_classes_authorize_target_refuses(tmp_path):
    """`create_session` must apply the SAME caller refusals as the other verbs.

    An owned child is sendable by construction, so a caller class refused a
    target but allowed to CREATE one gets there anyway: create, then send to
    what it just made. Mutation guard: dropping either check below lets an
    app-scoped or incognito session manufacture a persistent peer it owns.
    """
    state = _make_state(tmp_path)

    app_caller = _slot(state, "chat-app")
    app_caller._app = "some-app"
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(app_caller)))
    assert exc.value.code == "app_scoped_caller"

    ghost = _slot(state, "chat-ghost")
    ghost.memory_mode = "incognito"
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(ghost)))
    assert exc.value.code == "ephemeral_caller"

    linked = _slot(state, "chat-linked")
    linked.linked_session_key = "channel:1786300000.000100"
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(linked)))
    assert exc.value.code == "linked_session_caller"


def test_created_session_inherits_the_callers_workspace(tmp_path, monkeypatch):
    """The child lands in the caller's workspace, not the `default` one.

    Two consequences ride on this, and the second is why a plausible-looking
    default is not harmless: workspace is the memory boundary, AND
    `authorize_target` refuses a cross-workspace target -- so a child left in
    `default` is both a boundary crossing and unsendable by its own creator,
    which would make `session_create` useless outside the default workspace.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.workspace = "research"
    # The caller's own agent is bound to its workspace by construction, and the
    # child inherits it -- so the binding check passes without naming an agent.
    caller.agent = "researcher"
    _agent_resolves(monkeypatch, "research")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])
    assert child is not None
    assert child.workspace == "research", "the child must not fall back to 'default'"
    assert child.agent == "researcher", "an unnamed agent inherits the caller's"

    # And the creator can actually address it -- the property the default breaks.
    assert (
        sc.authorize_target(
            state,
            caller_session_key=_key(caller),
            target=created["target"],
            operation="read",
        ).key
        == created["target"]
    )


@pytest.fixture
def private_dispatch(tmp_path, monkeypatch):

    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", triggers="write")
    store = provision_member_memory(cfg, "writer")
    cfg.save()
    state = _make_state(tmp_path)
    from kiro_crew.history import ConversationLog

    state.conversation_log = ConversationLog()
    caller = _slot(state, "chat-conductor")
    return state, caller, store


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_create_refuses_unreadable_delegation_identity(private_dispatch, monkeypatch, error_type):
    state, caller, _store = private_dispatch
    before = set(state._slots)

    def unavailable(*args):
        raise error_type("/private/identity/path")

    monkeypatch.setattr(sc, "read_session_execution", unavailable)
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller), agent="writer"))
    assert exc.value.code == "memory_unavailable"
    assert "/private/identity/path" not in str(exc.value)
    assert set(state._slots) == before


@pytest.mark.parametrize("explicit", [False, True])
def test_private_creator_keeps_its_memory(private_dispatch, explicit):
    from kiro_crew.member_memory_auth import bind_private_session_store, read_private_session_store

    state, caller, store = private_dispatch
    caller.agent = "writer"
    caller.memory_store = store
    bind_private_session_store(_key(caller), store)
    state.conversation_log.update_metadata(_key(caller), {"memory_store": store})
    created = asyncio.run(
        sc.create_session(
            state, caller_session_key=_key(caller), agent="writer" if explicit else ""
        )
    )
    child = state.get_slot(created["target"])
    assert child.memory_store == store
    assert read_private_session_store(_key(child)) == store


@pytest.mark.parametrize("changed_field", ["agent", "memory_store", "linked_session_key"])
def test_create_rechecks_caller_identity_after_delegation(
    private_dispatch, monkeypatch, changed_field
):
    state, caller, _store = private_dispatch
    original = sc.read_session_execution
    before = set(state._slots)
    threads = []

    async def exercise():
        loop = asyncio.get_running_loop()
        changed = threading.Event()

        def check(key):
            threads.append(threading.get_ident())
            result = original(key)

            def mutate():
                setattr(caller, changed_field, "changed-selection")
                changed.set()

            loop.call_soon_threadsafe(mutate)
            assert changed.wait(5)
            return result

        monkeypatch.setattr(sc, "read_session_execution", check)
        creating = asyncio.ensure_future(
            sc.create_session(state, caller_session_key=_key(caller), agent="writer")
        )
        with pytest.raises(sc.SessionControlError) as exc:
            await await_until(creating, what="create_session refusing the changed caller")
        assert exc.value.code == "caller_memory_changed"

    asyncio.run(exercise())
    assert set(state._slots) == before
    assert threads and threading.get_ident() not in threads


@pytest.mark.parametrize("restored", [False, True])
def test_created_private_worker_can_start_after_birth_history(private_dispatch, tmp_path, restored):
    from kiro_crew.dashboard.chat_runner import _require_session_memory_assignment
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import read_private_session_store

    state, caller, store = private_dispatch
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller), agent="writer"))
    child = state.get_slot(created["target"])
    key = _key(child)
    log = state.conversation_log
    assert log.has_log(key), "the test must exercise the persisted-at-birth path"
    if restored:
        log = ConversationLog()
    # The first turn confirms the protected assignment written before birth
    # metadata. Reopening the log must not be what grants that assignment.
    _require_session_memory_assignment(key, store)
    assert read_private_session_store(key) == store
    assert log.get_metadata(key)["memory_store"] == store


def test_private_worker_assignment_precedes_birth_history(private_dispatch, monkeypatch):
    from kiro_crew.member_memory_auth import read_private_session_store

    state, caller, store = private_dispatch
    log = state.conversation_log
    real_update = log.update_metadata
    assignments = []
    loop_threads = []

    def persist(key, metadata):
        loop_threads.append(threading.get_ident())
        assignments.append(read_private_session_store(key))
        return real_update(key, metadata)

    monkeypatch.setattr(log, "update_metadata", persist)
    asyncio.run(sc.create_session(state, caller_session_key=_key(caller), agent="writer"))
    assert assignments == [store]
    assert loop_threads and threading.get_ident() not in loop_threads


@pytest.mark.parametrize("explicit", [False, True])
def test_created_template_keeps_namespace_after_member_discovery(tmp_path, monkeypatch, explicit):

    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, resolve_agent_bindings
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.memory_stores import provision_member_memory
    from kiro_crew.session_agent_selection import resolve_session_agent_bindings

    template = "dispatched-template"
    monkeypatch.setattr(
        loader,
        "_materialized_kiro_agent",
        lambda name, project_dir=None: template if name == template else "",
    )
    cfg = KiroCrewConfig.load()
    assert template not in cfg.agents
    cfg.save()
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-conductor", agent=template)
    created = asyncio.run(
        sc.create_session(
            state,
            caller_session_key=_key(caller),
            agent=template if explicit else "",
        )
    )
    child = state.get_slot(created["target"])
    key = _key(child)
    assert state.conversation_log.get_metadata(key)["agent"] == template

    # Discovery imports the same template as a private roster member before
    # this empty conversation's first turn. The earlier choice must survive.
    cfg = KiroCrewConfig.load()
    cfg.agents[template] = KiroCrewAgentConfig(kiro_agent=template)
    member_store = provision_member_memory(cfg, template)
    cfg.save()
    cfg = KiroCrewConfig.load()
    assert resolve_agent_bindings(cfg, template, child.project).memory_store_name == member_store
    chosen = resolve_session_agent_bindings(
        resolve_agent_bindings, cfg, key, template, child.project
    )
    assert chosen.memory_store_name in ("", "default")
    assert chosen.selection_kind == "template"
    assert read_private_session_store(key) is None


@pytest.mark.parametrize("preexisting", [False, True])
@pytest.mark.parametrize("started_turn", [False, True])
def test_failed_private_birth_retains_its_permanent_assignment(
    private_dispatch, monkeypatch, preexisting, started_turn
):
    from kiro_crew.dashboard.chat_runner import _require_session_memory_assignment
    from kiro_crew.member_memory_auth import bind_private_session_store, read_private_session_store
    from kiro_crew.memory_stores import UnknownMemoryStore
    from kiro_crew.session_agent_selection import session_agent_selection_kind

    state, caller, store = private_dispatch
    before = set(state._slots)
    child_name = "chat-failed-private-birth"
    key = f"dashboard:{child_name}"
    monkeypatch.setattr("kiro_crew.dashboard.state._mint_slot_key", lambda *args: child_name)
    if preexisting:
        bind_private_session_store(key, store)

    def fail_history(requested_key, metadata):
        assert requested_key == key
        assert read_private_session_store(key) == store
        if started_turn:
            _require_session_memory_assignment(key, store)
            state._slots[child_name].append("user", "work already started", "msg msg-u")
        raise OSError("birth metadata could not be written")

    monkeypatch.setattr(state.conversation_log, "update_metadata", fail_history)
    with pytest.raises(OSError, match="birth metadata"):
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller), agent="writer"))
    assert set(state._slots) == (before | {child_name} if started_turn else before)
    # The birth was authorized and its slot was already addressable. History
    # failure cannot prove no turn consumed this identity, so the lifetime pin
    # must remain; neither Global nor another member may adopt the key later.
    assert read_private_session_store(key) == store
    assert session_agent_selection_kind(key, "writer") == "member"
    _require_session_memory_assignment(key, store)
    for other in ("default", "another-member-store"):
        with pytest.raises(UnknownMemoryStore, match="memory assignment"):
            _require_session_memory_assignment(key, other)


def test_cancelled_private_birth_drains_before_returning(private_dispatch, monkeypatch):
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.session_agent_selection import session_agent_selection_kind

    state, caller, store = private_dispatch
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    keys = []
    real_update = state.conversation_log.update_metadata

    def blocked_history(key, metadata):
        keys.append(key)
        entered.set()
        assert release.wait(5), "the test must release the birth writer"
        try:
            return real_update(key, metadata)
        finally:
            finished.set()

    monkeypatch.setattr(state.conversation_log, "update_metadata", blocked_history)

    async def exercise():
        task = asyncio.create_task(
            sc.create_session(state, caller_session_key=_key(caller), agent="writer")
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            # flake-ok: pre-existing yields that deliver each cancel before the not-done check; moved verbatim and tracked for a de-flake pass
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "cancellation must not abandon the history writer"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await await_until(task, what="the cancelled history writer finishing")
            assert finished.is_set()
        finally:
            release.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert len(keys) == 1
    key = keys[0]
    child = next(slot for slot in state._slots.values() if _key(slot) == key)
    assert child._created_by == caller.key
    assert state.conversation_log.get_metadata(key)["memory_store"] == store
    assert read_private_session_store(key) == store
    assert session_agent_selection_kind(key, "writer") == "member"


@pytest.mark.parametrize("prior_context", ["history", "native"])
def test_private_worker_creation_cannot_adopt_an_old_session(
    private_dispatch, monkeypatch, prior_context
):
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.memory_stores import UnknownMemoryStore

    state, caller, _store = private_dispatch
    before = set(state._slots)
    child_name = "chat-private-worker-collision"
    key = f"dashboard:{child_name}"
    monkeypatch.setattr("kiro_crew.dashboard.state._mint_slot_key", lambda *args: child_name)
    if prior_context == "history":
        state.conversation_log.append(key, "user", "Existing conversation", agent="default")
    else:
        state.sessions.resumable_sid.side_effect = lambda requested: (
            "old-native-session" if requested == key else None
        )
    with pytest.raises(UnknownMemoryStore, match="new conversation"):
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller), agent="writer"))
    assert read_private_session_store(key) is None
    assert set(state._slots) == before


def test_create_checks_the_workspace_binding_even_when_no_agent_is_named(tmp_path, monkeypatch):
    """An omitted agent is not an unchecked agent.

    `resolve_agent_bindings` falls through to `config.default_agent` when no name
    is given, so the agent that ANSWERS may be bound to another workspace even
    though the caller named nothing. Authorization reads `slot.workspace` while
    execution follows that binding, so the same check has to cover this branch --
    which is exactly the branch the first version of this guard skipped.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.workspace = "research"

    # The effective default resolves to a DIFFERENT workspace than the caller's.
    monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: "default")

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert exc.value.code == "agent_workspace_mismatch"
    assert "default agent" in str(exc.value), "the message should name what would answer"


def test_created_session_gets_its_workspace_project_dir(tmp_path):
    """cwd follows the workspace, or project-scoped resolution uses the wrong tree."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])
    assert child is not None
    assert child.project == loader.default_project_dir(child.workspace)


# ── session_create: the creator's trust grant ───────────────────────────────


def test_created_session_inherits_the_callers_trust(tmp_path):
    """A trusted creator's worker starts trusted, or dispatch stalls on a prompt.

    The whole point of dispatching a session is that the work proceeds without the
    operator sitting on it, and `spawn_run` subagents already start auto-approved
    off the parent's policy (`parent_trusted`). A child born interactive blocks on
    the first tool call with nobody watching -- the same failure, one layer up.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = True

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child is not None
    assert child._trust is True, "the creator's posture must follow the work"


def test_command_grants_never_transfer_even_under_full_trust(tmp_path):
    """`_trusted_patterns` are per-command grants, not a posture, so they stay put.

    A pattern is judged against the session the operator was LOOKING at, while a
    dispatched worker runs model-authored work they have not seen -- so the same
    glob can admit a command the grant was never asked about. Inheriting them also
    buys nothing where it would be safe: with `_trust` set the child already
    auto-approves through `_slot_is_trusted`, so the list is dead weight here and
    changes the outcome only in the case that must keep asking (see the sibling
    test below).
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = True
    caller._trusted_patterns = {"npm test", "git status"}

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trusted_patterns == set(), "command grants must not be delegated"
    # And nothing was aliased on the way past: the caller keeps its own grants.
    assert caller._trusted_patterns == {"npm test", "git status"}


def test_a_pattern_only_creator_produces_a_child_that_still_asks(tmp_path):
    """The case the exclusion exists for: grants without session trust.

    An operator who approved single commands and deliberately did NOT click trust
    has said "ask me". `chat_runner` matches `_trusted_patterns` independently of
    `_trust` (so an inherited pattern would auto-approve on its own), which is why
    a child of such a caller must be born with nothing.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = False
    caller._trusted_patterns = {"npm test"}

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trust is False
    assert child._trusted_patterns == set(), "a withheld posture must not leak as a grant"


def test_created_session_inherits_trust_reads(tmp_path):
    """`trust_reads` is a grant too, and it is the narrower one.

    Inheriting only the full grant would leave the read-only mode -- the setting a
    cautious operator picks -- as the one that still stalls its own workers.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = False
    caller._trust_reads = True

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trust_reads is True
    assert child._trust is False, "the narrow grant must not widen on the way down"


def test_an_untrusted_creator_makes_an_untrusted_child(tmp_path):
    """Negative control: nothing is granted that the creator did not hold.

    Without this the inheritance could be a constant `True` and every test above
    would still pass.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trust is False
    assert child._trust_reads is False
    assert child._trusted_patterns == set()


def test_the_scoped_safety_override_grant_is_never_inherited(tmp_path):
    """`_trust_scope` is a revocable credential, not a setting to copy.

    Its entire value is being re-checked on every approval against a TTL-bounded,
    SEL-audited `SafetyOverride` scope. Forking the key hands a second session a
    grant whose revocation the create path cannot observe -- and the child would
    keep auto-approving after the scope that justified it is gone.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust_scope = "app:worker:abc123"

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trust_scope == "", "a scoped grant must not fork onto the child"
    assert child._trust is False, "nor may it be laundered into the durable flag"


def test_trust_revoked_mid_create_is_not_inherited(tmp_path, monkeypatch):
    """The grant that transfers is the one held at ALLOCATION, not at entry.

    `create_session` suspends several times before the slot exists (project dir,
    config load, folder confirmation), and the operator can pick `normal` in any
    of those windows. Reading the entry-time slot would hand the child a grant
    that has been revoked. Simulated by revoking inside the project-dir
    resolution, the same interleaving the folder-delete test uses.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = True

    def _revoke_then_resolve(_workspace):
        caller._trust = False
        caller._trust_reads = False
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _revoke_then_resolve)

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trust is False, "a revoked grant must not be resurrected by a create"
    assert child._trust_reads is False


def test_the_create_audit_records_what_the_child_was_born_with(tmp_path):
    """An auto-approval in the child must be traceable to the creator's grant.

    Without it, the SEL shows a session whose tools approve themselves and no
    record of where that authority came from. Recorded on both outcomes, so
    "false" is positive evidence the grant did not transfer.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = True

    seen: dict[str, object] = {}

    def _capture(**kwargs):
        seen.update(kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sc, "_audit", _capture)
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    detail = seen["detail"]
    assert detail["inherited_trust"] == "true"
    assert detail["inherited_trust_reads"] == "false"


def test_the_created_agent_name_is_sanitized_before_storage(tmp_path, monkeypatch):
    """The caller-supplied agent goes through the same outbound guard as the title.

    It arrives from the calling model, is persisted verbatim to the metadata line
    and pushed to every dashboard client, so the schema's length cap is not
    enough -- a credential-shaped string would otherwise be stored and displayed.

    Mutation guard: dropping the `sanitize_outbound` call leaves the raw value.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    monkeypatch.setattr(sc, "sanitize_outbound", lambda s: f"SANITIZED:{s}")
    # Keep the binding check satisfied so the test reaches the storage under test.
    _agent_resolves(monkeypatch, caller.workspace)

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), agent="secret-looking")
    )
    child = state.get_slot(created["target"])
    assert child is not None
    assert child.agent.startswith(
        "SANITIZED:"
    ), "the agent value must pass through the outbound guard before storage"
