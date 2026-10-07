"""Canonical execution routing is independent of labels and parent lifetime."""

import logging
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from kiro_crew import execution_context as execution
from kiro_crew.config.sections import KiroCrewAgentConfig, MemoryStoreConfig
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.vector_memory import create_member_database


@pytest.fixture
def members(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    execution._LIVE_EXECUTIONS.clear()
    execution._VOUCHED_EXECUTIONS.clear()
    cfg = SimpleNamespace(agents={}, memory_stores={})
    for name in ("alice", "bob"):
        store = f"member-{name}"
        path = tmp_path / "memory_stores" / store / "memory.db"
        path.parent.mkdir(parents=True)
        create_member_database(path, member_id=f"id-{name}", store_id=store)
        cfg.agents[name] = KiroCrewAgentConfig(
            member_id=f"id-{name}", memory_store=store, kiro_agent="shared-template"
        )
        cfg.memory_stores[store] = MemoryStoreConfig(
            owner_member=name, owner_member_id=f"id-{name}", memory_version=2
        )
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    yield cfg
    execution._LIVE_EXECUTIONS.clear()
    execution._VOUCHED_EXECUTIONS.clear()


def test_database_identity_path_and_cross_member_routing(members):
    alice = execution.resolve_member_execution(members, "alice")
    bob = execution.resolve_member_execution(members, "bob")
    assert alice.template_id == bob.template_id
    assert alice.member_id != bob.member_id
    assert execution.validate_execution(alice) == alice
    assert execution.validate_execution(bob) == bob
    with pytest.raises(FrozenInstanceError):
        alice.member_id = bob.member_id


def test_rename_and_rebind_cannot_reinterpret_captured_work(members):
    admitted = execution.resolve_member_execution(members, "alice")
    members.agents["renamed"] = members.agents.pop("alice")
    members.agents["renamed"].memory_store = "member-bob"
    assert execution.derive_execution(admitted) is not None
    assert execution.validate_execution(admitted).store.store_id == "member-alice"
    assert execution.member_config_for_id(members, "id-alice")[0] == "renamed"


def test_explicit_target_selects_existing_member_without_broadening_privacy(members):
    parent = execution.resolve_member_execution(
        members, "alice", memory_mode="incognito", app="example-app"
    )
    inherited = execution.derive_execution(parent, requested_mode="persistent")
    targeted = execution.derive_execution(parent, target_member="bob", config=members)
    assert inherited == parent
    assert targeted.member_id == "id-bob"
    assert targeted.memory_mode == "incognito"
    assert targeted.app == "example-app"
    with pytest.raises(UnknownMemoryStore):
        execution.derive_execution(parent, target_member="missing", config=members)


def test_template_selection_keeps_the_store_and_flips_only_the_namespace(members):
    """`with_template` is the one rewrite for running a store under a template.

    A member with a persisted id can carry "this member, under that template":
    the store and id stay, the selection namespace becomes the template's. A
    member with NO persisted id is named by its selection alone, so the same
    rewrite leaves a record attributed to no member -- the plain template run the
    spawn gate mints for that caller; the `session_create` arm, which keeps such
    a member's selection, does not call this. A template record renames its
    selection.
    """
    alice = execution.resolve_member_execution(members, "alice", memory_mode="incognito")
    delegate = alice.with_template("worker-template", "kirocrew-worker")
    assert delegate.selection_kind == "template"
    assert delegate.selection_name == "kirocrew-worker"
    assert delegate.template_id == "worker-template"
    assert (delegate.member_id, delegate.store, delegate.memory_mode) == (
        alice.member_id,
        alice.store,
        "incognito",
    )

    legacy = execution.ExecutionContext(
        None, execution.MemoryStoreRef("legacy-v1"), "member", "shared-template", "incognito"
    )
    legacy = replace(legacy, selection_name="scribe")
    flipped = legacy.with_template("worker-template", "kirocrew-worker")
    assert flipped == replace(
        legacy,
        selection_kind="template",
        template_id="worker-template",
        selection_name="kirocrew-worker",
    )
    assert (flipped.store, flipped.memory_mode) == (legacy.store, "incognito")

    plain = execution.ExecutionContext(None, execution.MemoryStoreRef("default"), "template", "")
    renamed = plain.with_template("worker-template", "kirocrew-worker")
    assert renamed == replace(
        plain, template_id="worker-template", selection_name="kirocrew-worker"
    )


def test_corrupt_member_never_becomes_global(members, tmp_path):
    admitted = execution.resolve_member_execution(members, "alice")
    (tmp_path / "memory_stores" / "member-alice" / "memory.db").unlink()
    with pytest.raises((ValueError, OSError)):
        execution.validate_execution(admitted)
    assert admitted.store.store_id == "member-alice"


@pytest.mark.parametrize("admission", ["captured", "configured"])
def test_member_admission_normalizes_selected_sqlite_driver_errors(members, monkeypatch, admission):
    from kiro_crew import vector_memory
    from kiro_crew.memory_stores import require_memory_store

    admitted = execution.resolve_member_execution(members, "alice")

    class AlternateDriverError(Exception):
        """A driver error unrelated to stdlib sqlite3.Error, like pysqlite3's."""

    failure = AlternateDriverError("synthetic unreadable member database")

    opened = []

    def connect(database, *, uri):
        opened.append((database, uri))
        raise failure

    monkeypatch.setattr(
        vector_memory, "sqlite3", SimpleNamespace(Error=AlternateDriverError, connect=connect)
    )
    with pytest.raises(UnknownMemoryStore, match="unreadable") as refused:
        if admission == "captured":
            execution.validate_execution(admitted)
        else:
            require_memory_store("member-alice", config=members)
    assert refused.value.__cause__ is failure
    assert "Global was not used" in str(refused.value)
    from kiro_crew.memory_stores import _named_store_dir

    path = _named_store_dir("member-alice") / "memory.db"
    assert opened == [(path.resolve().as_uri() + "?mode=ro", True)]
    assert admitted.store.store_id == "member-alice"


def test_restricted_session_record_stays_live_and_monotonic(members, tmp_path):
    admitted = execution.resolve_member_execution(members, "alice", memory_mode="incognito")
    execution.bind_session_execution("dashboard_private", admitted)
    assert execution.read_session_execution("dashboard_private") == admitted
    execution.bind_session_execution(
        "dashboard_private", replace(admitted, memory_mode="persistent")
    )
    assert execution.read_session_execution("dashboard_private").memory_mode == "incognito"
    from kiro_crew.history import ConversationLog

    assert not ConversationLog()._path("dashboard_private").exists()


@pytest.mark.parametrize("line_mode", ["incognito", "Incognito"])
def test_persistent_bind_honors_restricted_record_without_execution_context(members, line_mode):
    from kiro_crew.history import ConversationLog

    key = "dashboard_restricted_recreate"
    log = ConversationLog()
    log.update_metadata(key, {"memory_mode": line_mode})
    persistent = execution.resolve_member_execution(members, "alice")

    execution.bind_session_execution(key, persistent)

    metadata = log.get_metadata(key)
    assert metadata["memory_mode"] == "incognito"
    assert "memory_store" not in metadata
    assert execution.EXECUTION_CONTEXT_KEY not in metadata
    live = execution.read_live_session_execution(key)
    assert live is not None
    assert live.memory_mode == "incognito"
    assert execution.read_session_execution(key) == live


@pytest.mark.parametrize("line_mode", ["incognito", "Incognito"])
def test_durable_record_reads_no_looser_than_its_tightened_line(members, line_mode):
    """A persistent record beside a restricted line answers with the line's mode.

    A member chat binds a persistent DURABLE record into its line. The line's own
    ``memory_mode`` can then be tightened without the record following it -- a
    hand-edited ``Incognito`` header, or a save that ratcheted the line as a
    restricted original's rows landed under it. Every carrier-first reader goes
    through ``read_session_execution``, so it folds the line in: the identity is
    the record's, the mode is the stricter of the two. The next binding then takes
    the restricted branch and heals the record itself, so the file stops
    disagreeing with itself.
    """
    from kiro_crew.history import ConversationLog

    key = "dashboard_member_line_tightened"
    log = ConversationLog()
    persistent = execution.resolve_member_execution(members, "alice")
    execution.bind_session_execution(key, persistent)
    before = log.get_metadata(key)
    assert before[execution.EXECUTION_CONTEXT_KEY]["memory_mode"] == "persistent"
    assert execution.read_session_execution(key) == persistent

    log.update_metadata(key, {"memory_mode": line_mode})

    read = execution.read_session_execution(key)
    assert read is not None
    assert read.memory_mode == "incognito", "the record's looser mode won over the line"
    assert replace(read, memory_mode="persistent") == persistent, "the identity moved"

    execution.bind_session_execution(key, persistent)

    metadata = log.get_metadata(key)
    assert metadata["memory_mode"] == "incognito"
    assert metadata[execution.EXECUTION_CONTEXT_KEY]["memory_mode"] == "incognito"
    assert metadata[execution.EXECUTION_CONTEXT_KEY]["member_id"] == persistent.member_id
    live = execution.read_live_session_execution(key)
    assert live is not None
    assert live.memory_mode == "incognito"


def test_session_publication_compares_captured_record(members):
    alice = execution.resolve_member_execution(members, "alice")
    bob = execution.resolve_member_execution(members, "bob")
    execution.bind_session_execution("dashboard_race", alice)
    with pytest.raises(UnknownMemoryStore):
        execution.bind_session_execution(
            "dashboard_race", bob, replace_existing=True, expected=None
        )
    assert execution.read_session_execution("dashboard_race") == alice


def test_missing_canonical_member_record_is_an_error(members):
    from kiro_crew.history import ConversationLog

    ConversationLog().update_metadata("dashboard_damaged", {"memory_store": "member-alice"})
    with pytest.raises(UnknownMemoryStore):
        execution.read_session_execution("dashboard_damaged")


def test_subagent_owns_context_after_parent_disappears(members):
    from kiro_crew.subagent_persistence import create_agent_folder, read_run_execution

    parent = execution.resolve_member_execution(members, "alice")
    create_agent_folder("test-child", task="synthetic", execution_context=parent)
    members.agents.clear()
    child = read_run_execution("test-child")
    assert child == parent
    assert execution.validate_execution(child) == parent


def test_restricted_subagent_does_not_write_task_or_result(members):
    from kiro_crew.subagent_persistence import (
        create_agent_folder,
        read_run_execution,
        write_result_chunk,
    )

    parent = execution.resolve_member_execution(members, "alice", memory_mode="temporary")
    directory = create_agent_folder(
        "test-restricted",
        task="synthetic private prompt",
        execution_context=parent,
        memory_mode="temporary",
    )
    write_result_chunk("test-restricted", "synthetic private result")
    assert read_run_execution("test-restricted") == parent
    assert not directory.exists()


def test_cron_record_keeps_member_after_parent_and_config_change(members):
    from kiro_crew.cron import CronJob, CronSchedule, bind_cron_memory, resolve_cron_memory

    parent = execution.resolve_member_execution(members, "alice")
    execution.bind_session_execution("dashboard_cron", parent)
    job = CronJob(
        id="job-a",
        name="synthetic",
        message="test",
        schedule=CronSchedule(kind="every", every_secs=60),
        session_key="dashboard_cron",
    )
    bind_cron_memory(job)
    members.agents.clear()
    assert resolve_cron_memory(job) == ("member-alice", "shared-template")


@pytest.mark.asyncio
async def test_workflow_carrier_survives_closed_parent_and_uses_single_record(members, tmp_path):
    from kiro_crew.workflow_memory import WorkflowScope

    parent = execution.resolve_member_execution(members, "alice")
    execution.bind_session_execution("dashboard_workflow", parent)
    context = SimpleNamespace(_session_memory_modes={})
    scope = await WorkflowScope.admit("wf_test", context, "dashboard_workflow")
    members.agents.clear()
    await scope.prepare(context, scope.worker_key("worker"))
    assert scope.execution_context == parent
    assert execution.read_session_execution(scope.worker_key("worker")) == parent
    assert not (tmp_path / "member-memory-bindings").exists()


def test_repeated_retention_tightening_survives_restart_without_new_body(members):
    from kiro_crew.history import ConversationLog

    admitted = execution.resolve_member_execution(members, "alice")
    key = "dashboard_restricted_restart"
    execution.bind_session_execution(key, admitted)
    log = ConversationLog()
    original_rows = log._path(key).read_text(encoding="utf-8").splitlines()[1:]
    execution.bind_session_execution(key, admitted.with_mode("incognito"))
    execution.bind_session_execution(key, admitted.with_mode("temporary"))
    execution._LIVE_EXECUTIONS.clear()
    execution._VOUCHED_EXECUTIONS.clear()
    restored = execution.read_session_execution(key, required=True)
    assert restored.memory_mode == "temporary"
    assert restored.store == admitted.store
    assert log._path(key).read_text(encoding="utf-8").splitlines()[1:] == original_rows


def test_cancelled_restricted_selection_preserves_strongest_retention(members):
    admitted = execution.resolve_member_execution(members, "alice", memory_mode="incognito")
    published = execution.resolve_member_execution(members, "bob", memory_mode="temporary")
    execution.bind_session_execution("dashboard_cancelled", admitted)
    execution.bind_session_execution("dashboard_cancelled", published, replace_existing=True)
    assert execution.restore_live_session_execution(
        "dashboard_cancelled", admitted.to_record(), published.to_record()
    )
    restored = execution.read_session_execution("dashboard_cancelled", required=True)
    assert restored.member_id == admitted.member_id
    assert restored.memory_mode == "temporary"


def test_cancelled_persistent_selection_withdraws_the_vouched_identity(members):
    # A persistent session has no live carrier, so the rollback reports False and
    # its caller undoes the durable record itself. The vouched entry is DROPPED
    # rather than rolled back to `prior`, and the reason is that `prior` is the
    # record the session itself writes. The publication already overwrote whatever
    # entry existed before it, so nothing reachable at the rollback distinguishes a
    # prior that WAS legitimately vouched from a forged one that never was --
    # restoring it would let publish-then-rollback hand a forged store the vouched
    # half of the agreement, which is the forgery the agreement exists to refuse
    # reached through a rollback rather than a fresh claim.
    #
    # `restore_agent_selection` routes every rollback through this one seam before
    # it touches the record, so the seam is where the withdrawal belongs. The cost
    # is that own-store dispatch waits for the owner to re-select the agent, which
    # binds afresh through the durable path -- the recovery the spec documents.
    admitted = execution.resolve_member_execution(members, "alice")
    published = execution.resolve_member_execution(members, "bob")
    key = "dashboard:cancelled-persistent"
    execution.bind_session_execution(key, admitted, vouch=True)
    execution.bind_session_execution(key, published, replace_existing=True, vouch=True)
    # Precondition: the switch really did vouch for bob, so a failure below means
    # the rollback did not withdraw rather than that nothing was published.
    assert execution.read_vouched_session_execution(key).member_id == published.member_id
    # False is the persistent path: no live carrier for the rollback to undo.
    assert not execution.restore_live_session_execution(
        key, admitted.to_record(), published.to_record()
    )
    assert execution.read_vouched_session_execution(key) is None


def test_every_binder_declares_whether_it_vouches():
    """Own-store authority is claimed only where it is spelled out.

    `bind_session_execution(vouch=...)` defaults to False, so a publication claims
    own-store authority ONLY by asking. That direction is deliberate: a caller that
    should have vouched and did not loses a capability loudly at a refused dispatch,
    while one that vouches a store rebuilt from the session's own record grants
    access to another member's private memory silently.

    This enumeration is the second half of that protection. A new binder reddens it
    until the site is listed with its disposition, so the provenance question is
    answered rather than inherited, and a site that flips from not-vouching to
    vouching cannot pass unnoticed.
    """
    import ast
    from pathlib import Path

    root = Path(execution.__file__).parent
    actual: dict[tuple[str, str], str] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else ""
            )
            if called != "bind_session_execution":
                continue
            enclosing, cursor = "<module>", node
            while cursor in parents:
                cursor = parents[cursor]
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    enclosing = cursor.name
                    break
            keywords = {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg}
            actual[(path.relative_to(root).as_posix(), enclosing)] = keywords.get("vouch", "ABSENT")

    # ABSENT means the default: publish the record, claim NO own-store authority.
    # The fork carries its store from the SOURCE session's record, and the subagent
    # binder runs on keys that never reach the vouch, so neither may vouch.
    #
    # The vouched population is CALLERS whose session key can reach `create_session`
    # -- a real dashboard slot key that `caller_slot_key` can resolve -- because a
    # vouched entry is only ever read for the CALLER slot of `create_session`'s
    # own-store admission. `hooks.bind_captured` runs on a `hook:` synthetic key in
    # an EPHEMERAL session `_run_hook_agent` destroys after the turn, which is never
    # a dashboard slot, so `caller_slot_key` cannot resolve it and it can never be
    # that caller. It therefore publishes with `vouch=False` and spends no slot in
    # the capped map.
    #
    # "True" is an establishing site whose key IS (or becomes) a real create-capable
    # slot: `_persist_birth` binds the freshly created child slot, and
    # `bind_private_session_store` establishes own-store dispatch authority for a
    # member's dashboard slot from a trusted store argument, not from a record the
    # session can write. `record_agent_selection` threads the decision because it
    # serves both shapes.
    #
    # `rebind_cron_session_template` rebinds a `cron:` key, which is never the
    # caller slot of an own-store admission, so it publishes with `vouch=False`.
    expected = {
        ("cron_service/identity.py", "rebind_cron_session_template"): "False",
        ("dashboard/chat_fork.py", "_bind_fork_execution"): "ABSENT",
        ("dashboard/chat_persistence.py", "_pin_private_agent_assignment"): "ABSENT",
        ("dashboard/handlers/hooks.py", "bind_captured"): "False",
        ("dashboard/session_control.py", "_persist_birth"): "True",
        ("member_memory_auth.py", "bind_private_session_store"): "True",
        ("session_agent_selection.py", "record_agent_selection"): "vouch",
        ("subagent_manager/run.py", "publish_execution"): "ABSENT",
        ("subagent_persistence.py", "bind_session_memory_mode"): "ABSENT",
    }
    assert actual == expected


def test_a_member_less_publication_is_never_vouched(members):
    # Asking is necessary but not sufficient. A Global-store session has no member_id
    # -- MemoryStoreRef refuses a member on Global, so the two go together -- and the
    # own-store admission identifies its caller BY member_id, refusing before the store
    # question when there is none. Such an entry could therefore never be admitted, and
    # keeping it would spend a slot in a capped map while inviting a later reader to
    # read "vouched" as meaning more than it can.
    globalish = execution.execution_for_store("", memory_mode="persistent")
    assert globalish.member_id is None

    execution.bind_session_execution("dashboard:global-vouch", globalish, vouch=True)
    assert execution.read_session_execution("dashboard:global-vouch") == globalish
    assert execution.read_vouched_session_execution("dashboard:global-vouch") is None


def test_a_hook_session_publishes_but_is_never_vouched(members):
    # A vouched entry is only ever read for the CALLER slot of `create_session`'s
    # own-store admission, so a key that can never be that caller must not be
    # vouched. A hook session's key is a `hook:` synthetic in an ephemeral session
    # `dashboard/handlers/hooks._run_hook_agent` destroys after the turn; it is never
    # a dashboard slot, so `caller_slot_key` cannot resolve it. `hooks.bind_captured`
    # therefore binds such a key with `vouch=False` (pinned by
    # `test_every_binder_declares_whether_it_vouches`). This is the OBSERVABLE effect
    # at the map: the record publishes so the hook turn runs, but no entry is
    # vouched, so the key spends no slot in the capped `_VOUCHED_EXECUTIONS` map.
    alice = execution.resolve_member_execution(members, "alice")
    hook_key = "hook:default:1700000000"

    # The same call `hooks.bind_captured` makes: replace_existing, no vouch.
    execution.bind_session_execution(hook_key, alice, replace_existing=True, vouch=False)

    assert execution.read_session_execution(hook_key) == alice, "the hook record must publish"
    assert (
        execution.read_vouched_session_execution(hook_key) is None
    ), "a hook session cannot reach create_session, so it must not be vouched"

    # A create-capable dashboard slot binding from independently established identity
    # still vouches, so own-store dispatch for a real caller keeps working.
    slot_key = "dashboard:create-capable"
    execution.bind_session_execution(slot_key, alice, vouch=True)
    assert execution.read_vouched_session_execution(slot_key).member_id == alice.member_id


def test_a_publication_does_not_vouch_unless_it_asks(members):
    # The fail-closed default itself, pinned. A binder that says nothing about
    # provenance publishes the record and claims NO own-store authority, so a caller
    # that carries its store out of a session's own record -- the dashboard fork
    # reads the SOURCE session's record, for instance -- cannot mint authority for
    # the child merely by not thinking about it. Asking is the only way in.
    alice = execution.resolve_member_execution(members, "alice")
    silent, asked = "dashboard:silent-bind", "dashboard:asked-bind"

    execution.bind_session_execution(silent, alice)
    assert execution.read_session_execution(silent) == alice
    assert execution.read_vouched_session_execution(silent) is None

    # The twin, so the refusal above cannot be a blanket break: the same publication
    # that asks does get vouched.
    execution.bind_session_execution(asked, alice, vouch=True)
    assert execution.read_vouched_session_execution(asked).member_id == alice.member_id


def test_old_close_cannot_clear_reused_session_identity(members):
    old = execution.resolve_member_execution(members, "alice", memory_mode="incognito")
    new = execution.resolve_member_execution(members, "bob", memory_mode="incognito")
    key = "dashboard:reused-key"
    execution.bind_session_execution(key, old)
    execution.bind_session_execution(key, new, replace_existing=True)
    execution.clear_session_execution(key, expected=old)
    assert execution.read_session_execution(key) == new
    execution.clear_session_execution(key, expected=new)
    assert execution.read_session_execution(key) is None


def test_vouched_identities_are_capped_and_an_evicted_session_can_rebind(members, monkeypatch):
    # The population this map retains is not the set of live sessions: several
    # `bind_session_execution` callers mint a fresh key per REQUEST, so uptime
    # alone grows it. The cap is the backstop for the producers that have no
    # teardown of their own.
    #
    # Patched small rather than filling 4096 entries: the property is that the
    # OLDEST entry goes and the newest survive, which does not depend on the
    # number, and a loop of thousands of real admissions would cost the shared
    # host for nothing.
    monkeypatch.setattr(execution, "_MAX_VOUCHED_EXECUTIONS", 2)
    alice = execution.resolve_member_execution(members, "alice")
    keys = ["hook:default:1", "hook:default:2", "hook:default:3"]
    for key in keys:
        execution.bind_session_execution(key, alice, vouch=True)
    # Precondition: all three really were admitted, so a None below is the cap
    # rather than a bind that never happened.
    assert execution.read_session_execution(keys[0]).member_id == alice.member_id
    # Evicted, so this process does not vouch for it -- which makes the own-store
    # question unanswerable and the caller must refuse. Fail-closed.
    assert execution.read_vouched_session_execution(keys[0]) is None
    assert execution.read_vouched_session_execution(keys[1]) is not None
    assert execution.read_vouched_session_execution(keys[2]) is not None
    # Nothing durable was destroyed: the cap withdraws this process's WORD, not
    # the session's record, so re-binding restores the vouch with no repair.
    execution.bind_session_execution(keys[0], alice, replace_existing=True, vouch=True)
    assert execution.read_vouched_session_execution(keys[0]).member_id == alice.member_id


def test_nothing_is_vouched_when_the_durable_write_fails(members):
    # This ordering is what bounds the retained KEY, so it is pinned rather than
    # duplicated as a second length cap here: the vouch runs strictly after the
    # write that names a file after the session key, so a key the record cannot
    # carry never reaches the map. It is also the property that keeps this process
    # from ever vouching for an identity the record does not hold.
    alice = execution.resolve_member_execution(members, "alice")
    overlong = "hook:" + "k" * 300
    with pytest.raises(OSError):
        execution.bind_session_execution(overlong, alice, vouch=True)
    assert execution.read_vouched_session_execution(overlong) is None
    # A key the record CAN carry is vouched, so the refusal above is the failed
    # write and not a blanket break.
    within = "hook:short-enough"
    execution.bind_session_execution(within, alice, vouch=True)
    assert execution.read_vouched_session_execution(within).member_id == alice.member_id


def test_overflow_is_counted_so_an_evicted_entry_is_not_silent(members, monkeypatch):
    # `read_vouched_session_execution` answers None for an evicted key exactly as
    # it does for one never bound, so without a tally the cap is indistinguishable
    # from a session nobody ever vouched for. Counted, the two can be told apart.
    monkeypatch.setattr(execution, "_MAX_VOUCHED_EXECUTIONS", 1)
    alice = execution.resolve_member_execution(members, "alice")
    # A delta, not an absolute: the tally is cumulative for the process and other
    # tests in this file share it.
    before = execution._vouched_overflow_count
    execution.bind_session_execution("hook:default:a", alice, vouch=True)
    assert execution._vouched_overflow_count == before
    execution.bind_session_execution("hook:default:b", alice, vouch=True)
    assert execution._vouched_overflow_count == before + 1
    # The throttle is armed, and a clear that does NOT drop the population below
    # the cap must leave it armed. This key was already evicted, so popping it is a
    # no-op and the map still sits at the cap -- re-arming here would emit a line
    # per evicting bind, which is the storm the throttle exists to prevent. This is
    # also what makes the guard's length term able to be FALSE: `_vouch` trims to
    # the cap on every insert, so a non-strict comparison would always hold.
    assert execution._vouched_overflow_reported
    execution.clear_session_execution("hook:default:a")
    assert execution._vouched_overflow_reported
    # Clearing a LIVE entry does drop it below the cap, so a later episode is
    # heard rather than swallowed by the first one.
    execution.clear_session_execution("hook:default:b")
    assert not execution._vouched_overflow_reported


def test_a_used_entry_outlives_an_idle_one_at_the_cap(members, monkeypatch):
    # Eviction drops the OLDEST and `_vouch` orders by last BIND, so without a
    # refresh a member session that binds once and then dispatches all day is the
    # FIRST entry dropped once a teardown-less producer churns keys in behind it.
    # That is backwards: it is the one still in use.
    monkeypatch.setattr(execution, "_MAX_VOUCHED_EXECUTIONS", 2)
    alice = execution.resolve_member_execution(members, "alice")
    execution.bind_session_execution("hook:dispatcher", alice, vouch=True)
    execution.bind_session_execution("hook:idle", alice, vouch=True)
    # Precondition: both are held, so a None below is the cap and not a bind that
    # never happened.
    assert execution.read_vouched_session_execution("hook:dispatcher") is not None
    assert execution.read_vouched_session_execution("hook:idle") is not None
    # The dispatcher is USED, which moves it to the young end and leaves the idle
    # key as the oldest.
    execution.refresh_vouched_session_execution("hook:dispatcher")
    execution.bind_session_execution("hook:churn", alice, vouch=True)
    assert execution.read_vouched_session_execution("hook:dispatcher") is not None
    assert execution.read_vouched_session_execution("hook:idle") is None


def test_refreshing_a_key_this_process_never_vouched_grants_nothing(members):
    # The refresh reorders; it must never be a second way to CREATE authority.
    # Without this, a caller reaching the refresh before the bind would mint an
    # entry the durable write never backed.
    assert execution.read_vouched_session_execution("hook:never-bound") is None
    execution.refresh_vouched_session_execution("hook:never-bound")
    assert execution.read_vouched_session_execution("hook:never-bound") is None


def test_a_later_overflow_episode_reports_the_all_time_tally(members, monkeypatch, caplog):
    # `_rearm_vouched_overflow` resets only the reported FLAG, so the count runs on
    # across episodes. A line calling that number this episode's would understate
    # every episode after the first.
    monkeypatch.setattr(execution, "_MAX_VOUCHED_EXECUTIONS", 1)
    alice = execution.resolve_member_execution(members, "alice")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.execution_context"):
        execution.bind_session_execution("hook:ep:1", alice, vouch=True)
        execution.bind_session_execution("hook:ep:2", alice, vouch=True)
        # Drop the population under the cap so a second episode is heard.
        execution.clear_session_execution("hook:ep:2")
        assert not execution._vouched_overflow_reported
        execution.bind_session_execution("hook:ep:3", alice, vouch=True)
        execution.bind_session_execution("hook:ep:4", alice, vouch=True)
    episodes = [r for r in caplog.records if "vouched execution overflow" in r.getMessage()]
    # Two episodes, so the SECOND line is the one that can be mislabelled.
    assert len(episodes) == 2, [r.getMessage() for r in episodes]
    assert episodes[1].args[0] > episodes[0].args[0]
    for record in episodes:
        assert "dropped in total" in record.getMessage()
        assert "this episode" not in record.getMessage()


def test_only_the_withdraw_helper_may_shrink_the_vouched_map():
    """Pin the structure, because a per-site re-arm is a debt any site can omit.

    `_withdraw_vouched` holds the pop and the re-arm together, so a withdrawal
    that routes through it is correct by construction and no bare pop exists to
    copy. This test keeps that property: it fails on any pop of the vouched map
    written outside the helper, which is the shape the defect takes. A behavioural
    test can only cover the withdrawal sites that exist; this one fails on the
    next one written outside the helper.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(execution.__file__).read_text(encoding="utf-8"))
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def _enclosing_function(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"

    def _is_vouched_pop(node):
        # `.popitem` is the eviction loop, which reports in the same breath and so
        # arms the throttle rather than owing it a re-arm. Only `.pop` is a
        # withdrawal, and every withdrawal belongs to the helper.
        func = getattr(node, "func", None)
        return (
            isinstance(node, ast.Call)
            and isinstance(func, ast.Attribute)
            and func.attr == "pop"
            and isinstance(func.value, ast.Name)
            and func.value.id == "_VOUCHED_EXECUTIONS"
        )

    pops = [(_enclosing_function(n), n.lineno) for n in ast.walk(tree) if _is_vouched_pop(n)]
    offenders = [site for site in pops if site[0] != "_withdraw_vouched"]
    assert not offenders, f"the vouched map was popped outside _withdraw_vouched at {offenders}"
    # Positive control on the helper: a refactor that deletes the pop entirely must
    # not leave this test passing vacuously.
    assert len(pops) == 1, f"expected exactly one pop, inside the helper; found {pops}"

    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_withdraw_vouched"
    )
    assert any(
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Name)
        and stmt.value.func.id == "_rearm_overflow_if_below_cap"
        for stmt in helper.body
    ), "the helper popped without re-arming, which is the debt it exists to pay"
    # Positive control on the call sites: the four withdrawals must still route
    # through the helper, or the invariant holds over code nobody calls.
    calls = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_withdraw_vouched"
    ]
    assert len(calls) >= 4, f"expected the four withdrawal sites to call the helper; found {calls}"


def test_an_oversized_rebind_at_the_cap_leaves_a_later_eviction_audible(
    members, monkeypatch, caplog
):
    # `_vouch`'s oversized branch POPS, which shrinks the map -- so it owes the same
    # below-cap re-arm the withdrawal path owes. Without it the throttle stayed
    # armed after the population dropped, and the next genuine eviction episode
    # emitted no line at all: the map silently shed identities with nothing in the
    # log, which is the one failure the counted-and-said-out-loud rule forbids.
    monkeypatch.setattr(execution, "_MAX_VOUCHED_EXECUTIONS", 1)
    alice = execution.resolve_member_execution(members, "alice")
    from kiro_crew.validation import MAX_SHORT_STRING

    with caplog.at_level(logging.WARNING, logger="kiro_crew.execution_context"):
        execution.bind_session_execution("hook:cap:x", alice, vouch=True)
        # Episode 1: evicts x and arms the throttle.
        execution.bind_session_execution("hook:cap:y", alice, vouch=True)
        assert execution._vouched_overflow_reported
        # An oversized REBIND of the key that is actually held, so the pop removes
        # something and the population really does fall below the cap. An absent
        # key would pop nothing and must NOT re-arm.
        huge = replace(alice, template_id="t" * (MAX_SHORT_STRING + 1))
        execution.bind_session_execution("hook:cap:y", huge, replace_existing=True, vouch=True)
        assert execution.read_vouched_session_execution("hook:cap:y") is None
        assert not execution._vouched_overflow_reported
        # Episode 2 must be HEARD. This is the assertion the missing re-arm broke.
        execution.bind_session_execution("hook:cap:z", alice, vouch=True)
        execution.bind_session_execution("hook:cap:w", alice, vouch=True)
    records = [r for r in caplog.records if "vouched execution overflow" in r.getMessage()]
    episodes = [r.getMessage() for r in records]
    assert len(episodes) == 2, episodes
    assert "hit the 1 cap" in episodes[1]


def test_an_execution_with_an_oversized_retained_field_is_not_vouched(members):
    # A cap on the COUNT bounds memory only if each retained item is bounded too,
    # and these strings are not all config-derived: the provider-switch path builds
    # an execution from the session's OWN record with `dataclass_replace(prior,
    # ...)`, so a session that writes an oversized field into its transcript
    # reaches the retention point.
    from kiro_crew.validation import MAX_SHORT_STRING

    alice = execution.resolve_member_execution(members, "alice")
    key = "dashboard:oversized-template"
    huge = replace(alice, template_id="t" * (MAX_SHORT_STRING + 1))
    execution.bind_session_execution(key, huge, vouch=True)
    # The record is written as always, so the session still works -- it is simply
    # not vouched, which refuses the own-store admission. Fail-closed, and the
    # identity is DROPPED rather than truncated: a truncated one would compare
    # equal to the honest session owning the shortened form and vouch for it.
    assert execution.read_session_execution(key).template_id == huge.template_id
    assert execution.read_vouched_session_execution(key) is None
    # A second field, so the check is a sweep over the retained strings and not
    # one special case.
    app_key = "dashboard:oversized-app"
    execution.bind_session_execution(
        app_key, replace(alice, app="a" * (MAX_SHORT_STRING + 1)), vouch=True
    )
    assert execution.read_vouched_session_execution(app_key) is None
    # The honest execution IS vouched, so the refusals above are the bound and not
    # a blanket break. Config-derived values sit far under it.
    within = "dashboard:within-bounds"
    execution.bind_session_execution(within, alice, vouch=True)
    assert execution.read_vouched_session_execution(within).member_id == alice.member_id
    assert len(alice.template_id) <= MAX_SHORT_STRING


def test_a_deeply_nested_durable_vouch_reads_as_not_recorded(members):
    # The reader is total: a file too deep for `json.loads` (RecursionError) must
    # read as "not recorded", the refusing answer, not raise out of the admission.
    from kiro_crew._durable_vouch import durable_vouch_path, read_durable_vouch

    key = "dashboard:deep-vouch"
    path = durable_vouch_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    assert read_durable_vouch(key) is None


def test_a_conditional_forget_racing_a_newer_vouch_keeps_the_newer_record(members, monkeypatch):
    # `forget_durable_vouch(only_if=...)` compares then unlinks. Bind writes from
    # worker threads while the withdrawal runs on the loop, so a write can land
    # between the two: unlocked, the unlink then deletes the NEWER vouch and a
    # restart loses valid authority. Make that interleaving deterministic: the
    # compare's read starts the writer and gives it time to finish, which it must
    # NOT be able to do until the forget has released the lock.
    import threading

    from kiro_crew import _durable_vouch

    key = "dashboard:racing-vouch"
    old_record = {"member_id": "alice", "generation": 1}
    new_record = {"member_id": "alice", "generation": 2}
    _durable_vouch.record_durable_vouch(key, old_record)
    assert _durable_vouch.read_durable_vouch(key) == old_record

    real_read = _durable_vouch.read_durable_vouch
    writer = threading.Thread(
        target=_durable_vouch.record_durable_vouch, args=(key, new_record), daemon=True
    )
    started = []

    def racing_read(session_key):
        result = real_read(session_key)
        if not started:
            started.append(True)
            writer.start()
            # Long enough for an unblocked write to land before the unlink; with
            # the lock held the writer cannot, so this join simply times out.
            writer.join(timeout=1.0)
        return result

    monkeypatch.setattr(_durable_vouch, "read_durable_vouch", racing_read)
    _durable_vouch.forget_durable_vouch(key, only_if=old_record)
    writer.join(timeout=5.0)
    assert not writer.is_alive()
    assert real_read(key) == new_record


def _legacy_member_schedule(member_id, store="member-alice"):
    """The shape 0.7.0-insider.1 to .5 stored: alias selector, store, no capture."""
    from kiro_crew.cron import CronJob, CronSchedule

    return CronJob(
        id="legacy-job",
        name="synthetic",
        message="test",
        schedule=CronSchedule(kind="every", every_secs=60),
        member_id=member_id,
        memory_store=store,
    )


@pytest.mark.parametrize("selector", ["alice", "id-alice"])
def test_legacy_member_schedule_is_attributed_to_its_stores_owner(members, selector):
    from kiro_crew.cron_service.identity import legacy_member_cron_execution

    job = _legacy_member_schedule(selector)
    attributed = legacy_member_cron_execution(job)
    assert attributed == execution.resolve_member_execution(members, "alice")
    assert attributed.member_id == "id-alice"
    assert attributed.template_id == "shared-template"
    assert job.execution_context is None


@pytest.mark.parametrize("selector", ["alice", "id-alice"])
def test_an_uncaptured_legacy_member_schedule_never_dispatches(members, selector):
    """Only the start-of-process capture binds it; dispatch never re-derives one."""
    from kiro_crew.cron import resolve_cron_memory
    from kiro_crew.cron_service.identity import LegacyScheduleRefused

    with pytest.raises(LegacyScheduleRefused, match="restart the gateway"):
        resolve_cron_memory(_legacy_member_schedule(selector))


def test_legacy_member_schedule_naming_another_member_is_refused(members):
    from kiro_crew.cron_service.identity import LegacyScheduleRefused, legacy_member_cron_execution

    with pytest.raises(LegacyScheduleRefused, match="recreate it from the member's chat"):
        legacy_member_cron_execution(_legacy_member_schedule("bob"))


def test_legacy_member_schedule_on_an_unattributed_store_names_the_repair(members):
    from kiro_crew.cron_service.identity import LegacyScheduleRefused, legacy_member_cron_execution
    from kiro_crew.memory_stores import LEGACY_MEMBER_STORE_REMEDY

    members.memory_stores["member-alice"].owner_member_id = ""
    with pytest.raises(LegacyScheduleRefused) as refused:
        legacy_member_cron_execution(_legacy_member_schedule("alice"))
    assert refused.value.args[0].startswith("memory_unavailable: ")
    assert LEGACY_MEMBER_STORE_REMEDY in refused.value.args[0]


def test_legacy_member_schedule_of_a_deleted_member_says_to_delete_it(members):
    """The store and its reserved id outlive the member, so recreating is impossible."""
    from kiro_crew.cron_service.identity import LegacyScheduleRefused, legacy_member_cron_execution

    del members.agents["alice"]
    with pytest.raises(LegacyScheduleRefused) as refused:
        legacy_member_cron_execution(_legacy_member_schedule("alice"))
    assert "member was deleted" in refused.value.args[0]
    assert "delete this schedule" in refused.value.args[0]
    assert "recreate" not in refused.value.args[0]


def test_a_lost_store_declaration_names_the_restart_its_repair_needs(members):
    """Restoring the entry reloads live, but only a restart captures the schedule."""
    from kiro_crew.cron_service.identity import LegacyScheduleRefused, legacy_member_cron_execution

    del members.memory_stores["member-alice"]
    with pytest.raises(LegacyScheduleRefused) as refused:
        legacy_member_cron_execution(_legacy_member_schedule("alice"))
    assert "declaration is unavailable" in refused.value.args[0]
    assert "then restart the gateway" in refused.value.args[0]


@pytest.mark.parametrize("damage", ["missing-store", "string-version", "bad-slug"])
def test_every_legacy_attribution_failure_is_the_typed_refusal(members, damage):
    from kiro_crew.cron_service.identity import LegacyScheduleRefused, legacy_member_cron_execution

    selector = "alice"
    if damage == "missing-store":
        del members.memory_stores["member-alice"]
    elif damage == "string-version":
        members.memory_stores["member-alice"].memory_version = "2"
    else:
        members.agents["alice"].member_id = "ID_Alice"
        members.memory_stores["member-alice"].owner_member_id = "ID_Alice"
    with pytest.raises(LegacyScheduleRefused):
        legacy_member_cron_execution(_legacy_member_schedule(selector))


@pytest.mark.parametrize("store", ["legacy-v1", "default"])
def test_a_member_schedule_on_a_v1_store_is_not_a_legacy_member_schedule(members, store):
    from kiro_crew.cron_service.identity import legacy_member_cron_execution

    members.memory_stores["legacy-v1"] = MemoryStoreConfig(memory_version=1)
    assert legacy_member_cron_execution(_legacy_member_schedule("alice", store)) is None


def test_the_schedule_capture_and_the_chat_backfill_share_one_attribution(members, monkeypatch):
    """Both callers run the five attribution steps through ``attribute_legacy_member``."""
    from kiro_crew.cron_service.identity import legacy_member_cron_execution

    calls = []
    real = execution.attribute_legacy_member

    def spy(config, store, named, **kwargs):
        calls.append((store, named, kwargs))
        return real(config, store, named, **kwargs)

    monkeypatch.setattr(execution, "attribute_legacy_member", spy)
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr(ConversationLog, "update_metadata_if", lambda *a, **k: True)
    expected = execution.resolve_member_execution(members, "alice")

    assert legacy_member_cron_execution(_legacy_member_schedule("id-alice")) == expected
    record = {"agent": "alice", "memory_store": "member-alice", "app": "notes"}
    backfilled = execution._backfill_legacy_member_record(
        "dashboard:legacy", record, "member-alice"
    )
    assert backfilled == replace(expected, app="notes")
    assert calls == [
        ("member-alice", "id-alice", {}),
        ("member-alice", "alice", {"memory_mode": "persistent", "app": "notes"}),
    ]


@pytest.mark.parametrize(
    ("damage", "refusal"),
    [
        ("no-owner", "LegacyStoreHasNoOwner"),
        ("deleted", "LegacyStoreOwnerDeleted"),
        ("not-named", "LegacyOwnerNotNamed"),
        ("rebound", "LegacyOwnerBoundElsewhere"),
    ],
)
def test_each_attribution_step_refuses_with_its_own_type(members, damage, refusal):
    named = "alice"
    if damage == "no-owner":
        members.memory_stores["member-alice"].owner_member_id = ""
    elif damage == "deleted":
        del members.agents["alice"]
    elif damage == "not-named":
        named = "bob"
    else:
        # A second store claiming the same owner: the member resolves, but elsewhere.
        members.memory_stores["member-alice-2"] = MemoryStoreConfig(
            owner_member="alice", owner_member_id="id-alice", memory_version=2
        )
        members.agents["alice"].memory_store = "member-alice-2"
    with pytest.raises(getattr(execution, refusal)):
        execution.attribute_legacy_member(members, "member-alice", named)


def _write_crons(path, records):
    import json

    path.write_text(json.dumps({"version": 2, "jobs": records}, indent=2), encoding="utf-8")


def _insider_record(job_id, member_id, store="member-alice", **extra):
    """A crons.json record exactly as 0.7.0-insider.1 to .5 wrote it: no execution_context key."""
    record = {
        "id": job_id,
        "name": job_id,
        "message": "digest",
        "schedule": {"kind": "every", "every_secs": 3600},
        "agent_id": "",
        "member_id": member_id,
        "memory_store": store,
    }
    record.update(extra)
    return record


@pytest.mark.parametrize("agent_id", ["", "named-template"])
def test_pre_identity_member_schedules_are_captured_once(members, tmp_path, caplog, agent_id):
    import json
    from types import SimpleNamespace

    from kiro_crew.cron import resolve_cron_memory
    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules
    from kiro_crew.cron_service.store import _job_from_record
    from kiro_crew.dashboard.handlers._shared import _cron_execution_from_registry

    store_dir = tmp_path / "cron-store"
    store_dir.mkdir()
    captured = _insider_record("captured", "id-alice")
    captured["execution_context"] = execution.resolve_member_execution(members, "alice").to_record()
    untouched = [
        captured,
        _insider_record("other-member", "bob"),
        _insider_record("ordinary", "", store=""),
    ]
    records = [_insider_record("legacy", "alice", agent_id=agent_id), *untouched]
    _write_crons(store_dir / "crons.json", json.loads(json.dumps(records)))

    assert migrate_legacy_member_schedules(store_dir) == ["legacy"]

    saved = json.loads((store_dir / "crons.json").read_text(encoding="utf-8"))["jobs"]
    alice = execution.resolve_member_execution(members, "alice")
    expected = replace(alice, template_id=agent_id) if agent_id else alice
    assert saved[0]["execution_context"] == expected.to_record()
    assert saved[0]["member_id"] == "id-alice"
    assert saved[0]["agent_id"] == agent_id
    assert saved[0]["execution_context"]["template_id"] == (agent_id or "shared-template")
    assert saved[1:] == json.loads(json.dumps(untouched))
    assert "other-member" in caplog.text and "recreate it from the member's chat" in caplog.text

    job = _job_from_record(saved[0])
    assert resolve_cron_memory(job) == ("member-alice", expected.template_id)
    state = SimpleNamespace(crons=SimpleNamespace(_jobs=[job]))
    assert _cron_execution_from_registry(state, "cron:legacy") == (True, expected)

    before = (store_dir / "crons.json").read_bytes()
    assert migrate_legacy_member_schedules(store_dir) == []
    assert (store_dir / "crons.json").read_bytes() == before


def test_the_schedule_capture_never_raises(members, tmp_path):
    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules

    assert migrate_legacy_member_schedules(tmp_path / "absent") == []
    (tmp_path / "crons.json").write_text("{", encoding="utf-8")
    assert migrate_legacy_member_schedules(tmp_path) == []
    assert (tmp_path / "crons.json").read_text(encoding="utf-8") == "{"


def _insider_cron_session(job_id, agent, store="member-alice", **extra):
    """The session record 0.7.0-insider.1 to .5 wrote under ``cron:<id>`` on the first fire.

    ``slack/gateway.py`` there ran ``log.update_metadata(key, {"memory_store":
    cron_memory_store, "agent": job.member_id})`` before ``get_or_create``, so the
    record's ``agent`` is the schedule's member selector, never the agent it named.
    """
    from kiro_crew.history import ConversationLog

    key = f"cron:{job_id}"
    ConversationLog().update_metadata(key, {"memory_store": store, "agent": agent, **extra})
    return key


def _dispatch_bind(store_dir, job_id="legacy"):
    """The single-agent fire's publication, exactly as the gateway performs it.

    ``build_cron_session_context`` mints the key and ``bind_session_execution`` is
    called positionally -- ``replace_existing`` False -- with the record's capture.
    """
    import json

    from kiro_crew.cron_service.identity import build_cron_session_context
    from kiro_crew.cron_service.store import _job_from_record

    records = json.loads((store_dir / "crons.json").read_text(encoding="utf-8"))["jobs"]
    job = _job_from_record(next(record for record in records if record["id"] == job_id))
    key, _ = build_cron_session_context(job)
    cron_execution = execution.execution_from_record({"execution_context": job.execution_context})
    execution.bind_session_execution(key, cron_execution)
    return key, cron_execution


@pytest.mark.parametrize("legacy_agent", ["alice", "id-alice", "named-template"])
def test_without_the_reconciliation_the_first_fire_is_refused(members, tmp_path, legacy_agent):
    """The clash the reconciliation exists for, on the same inputs the next test repairs.

    A record naming the member is backfilled into the member's OWN template on
    first read, so the capture's named agent is "another execution"; one naming
    anything else is refused as identity-less. Either way every fire failed until
    the schedule auto-paused.
    """
    from kiro_crew.cron_service import identity
    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules

    store_dir = tmp_path / "cron-store"
    store_dir.mkdir()
    _write_crons(
        store_dir / "crons.json", [_insider_record("legacy", "alice", agent_id="named-template")]
    )
    _insider_cron_session("legacy", legacy_agent)
    with pytest.MonkeyPatch.context() as untreated:
        untreated.setattr(identity, "_reconcile_legacy_cron_session", lambda job, execution: None)
        assert migrate_legacy_member_schedules(store_dir) == ["legacy"]
    with pytest.raises(ValueError, match="another execution|no canonical member identity"):
        _dispatch_bind(store_dir)


@pytest.mark.parametrize("legacy_agent", ["alice", "id-alice", "named-template"])
def test_a_captured_schedule_naming_an_agent_still_binds_its_old_session(
    members, tmp_path, legacy_agent
):
    """The record the old build left under ``cron:<id>`` agrees with the capture."""
    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules

    store_dir = tmp_path / "cron-store"
    store_dir.mkdir()
    _write_crons(
        store_dir / "crons.json", [_insider_record("legacy", "alice", agent_id="named-template")]
    )
    key = _insider_cron_session("legacy", legacy_agent)
    assert migrate_legacy_member_schedules(store_dir) == ["legacy"]

    bound_key, bound = _dispatch_bind(store_dir)
    assert bound_key == key == "cron:legacy"
    alice = execution.resolve_member_execution(members, "alice")
    assert bound == replace(alice, template_id="named-template")
    recorded = execution.read_session_execution(key, required=True)
    assert recorded.template_id == "named-template"
    assert recorded.member_id == "id-alice"
    assert recorded.store.store_id == "member-alice"
    assert execution.read_vouched_session_execution(key) is None
    # A second fire re-binds the same key and must agree with itself.
    _dispatch_bind(store_dir)


@pytest.mark.parametrize("backfilled", [False, True])
def test_session_write_failure_leaves_the_schedule_uncaptured_for_retry(
    members, tmp_path, caplog, backfilled
):
    import json
    from unittest.mock import patch

    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules
    from kiro_crew.history import ConversationLog

    path = tmp_path / "crons.json"
    legacy = _insider_record("legacy", "alice", agent_id="named-template")
    _write_crons(path, [legacy, _insider_record("never-fired", "alice")])
    key = _insider_cron_session("legacy", "alice")
    if backfilled:
        assert execution.read_session_execution(key).template_id == "shared-template"
    before = ConversationLog().get_metadata_status(key)[0]
    with patch.object(
        ConversationLog, "_update_metadata_locked", side_effect=OSError("session write failed")
    ) as write:
        assert migrate_legacy_member_schedules(tmp_path) == ["never-fired"]
    write.assert_called_once()
    assert ConversationLog().get_metadata_status(key)[0] == before
    saved = json.loads(path.read_text(encoding="utf-8"))["jobs"]
    assert saved[0] == legacy
    assert "execution_context" not in saved[0]
    assert any(
        record.name == "kiro_crew.cron" and "left uncaptured" in record.getMessage()
        for record in caplog.records
    )

    assert migrate_legacy_member_schedules(tmp_path) == ["legacy"]
    bound_key, bound = _dispatch_bind(tmp_path)
    assert bound_key == key
    assert bound.template_id == "named-template"
    assert execution.read_session_execution(key, required=True) == bound


def test_session_read_failure_leaves_the_schedule_uncaptured_for_retry(members, tmp_path):
    import json
    from unittest.mock import patch

    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules
    from kiro_crew.history import ConversationLog

    path = tmp_path / "crons.json"
    legacy = _insider_record("legacy", "alice", agent_id="named-template")
    _write_crons(path, [legacy])
    key = _insider_cron_session("legacy", "alice")
    with patch.object(ConversationLog, "get_metadata_status", return_value=(None, False)) as read:
        assert migrate_legacy_member_schedules(tmp_path) == []
    read.assert_called_once_with(key)
    saved = json.loads(path.read_text(encoding="utf-8"))["jobs"]
    assert saved == [legacy]
    assert "execution_context" not in saved[0]

    assert migrate_legacy_member_schedules(tmp_path) == ["legacy"]
    bound_key, bound = _dispatch_bind(tmp_path)
    assert bound_key == key
    assert bound.template_id == "named-template"
    assert execution.read_session_execution(key, required=True) == bound


def test_schedule_write_failure_retries_an_already_reconciled_session(members, tmp_path):
    from unittest.mock import patch

    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules
    from kiro_crew.history import ConversationLog

    path = tmp_path / "crons.json"
    _write_crons(path, [_insider_record("legacy", "alice", agent_id="named-template")])
    before = path.read_bytes()
    key = _insider_cron_session("legacy", "alice")
    with patch("kiro_crew.atomic_write.atomic_write", side_effect=OSError("schedule write failed")):
        assert migrate_legacy_member_schedules(tmp_path) == []
    assert path.read_bytes() == before
    named = replace(
        execution.resolve_member_execution(members, "alice"), template_id="named-template"
    )
    log = ConversationLog()
    assert log.get_metadata_status(key)[0]["execution_context"] == named.to_record()
    reconciled = log._path(key).read_bytes()

    assert migrate_legacy_member_schedules(tmp_path) == ["legacy"]
    assert log._path(key).read_bytes() == reconciled
    assert _dispatch_bind(tmp_path) == (key, named)


def test_a_session_already_backfilled_is_rebound_to_the_named_agent(members, tmp_path):
    """A read before the capture already turned the record into the member's own template."""
    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules

    store_dir = tmp_path / "cron-store"
    store_dir.mkdir()
    _write_crons(
        store_dir / "crons.json", [_insider_record("legacy", "alice", agent_id="named-template")]
    )
    key = _insider_cron_session("legacy", "alice")
    alice = execution.resolve_member_execution(members, "alice")
    assert execution.read_session_execution(key) == alice

    assert migrate_legacy_member_schedules(store_dir) == ["legacy"]
    _, bound = _dispatch_bind(store_dir)
    assert bound == replace(alice, template_id="named-template")
    assert execution.read_session_execution(key, required=True) == bound
    assert execution.read_vouched_session_execution(key) is None


def test_a_schedule_naming_no_agent_leaves_its_session_to_the_backfill(members, tmp_path):
    """The reconciliation and the read-time backfill agree, so the record is written once."""
    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules
    from kiro_crew.history import ConversationLog

    store_dir = tmp_path / "cron-store"
    store_dir.mkdir()
    _write_crons(store_dir / "crons.json", [_insider_record("legacy", "alice")])
    key = _insider_cron_session("legacy", "alice")
    assert migrate_legacy_member_schedules(store_dir) == ["legacy"]
    alice = execution.resolve_member_execution(members, "alice")
    assert ConversationLog().get_metadata_status(key)[0]["execution_context"] == alice.to_record()
    _, bound = _dispatch_bind(store_dir)
    assert bound == alice


@pytest.mark.parametrize(
    "record",
    [
        {"memory_store": "member-alice", "agent": "bob"},
        {"memory_store": "member-bob", "agent": "alice"},
        {"memory_store": "member-alice", "agent": "alice", "memory_mode": "incognito"},
        {"memory_store": "member-alice", "agent": "alice", "app": "some-app"},
    ],
    ids=["another-member", "another-store", "restricted", "app-owned"],
)
def test_a_session_the_capture_cannot_vouch_for_is_left_as_it_is(members, tmp_path, caplog, record):
    """Nothing is guessed: a record that does not plainly belong to the schedule is logged."""
    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules
    from kiro_crew.history import ConversationLog

    store_dir = tmp_path / "cron-store"
    store_dir.mkdir()
    _write_crons(
        store_dir / "crons.json", [_insider_record("legacy", "alice", agent_id="named-template")]
    )
    key = _insider_cron_session("legacy", **record)
    before = ConversationLog().get_metadata_status(key)[0]
    with caplog.at_level(logging.WARNING, logger="kiro_crew.cron"):
        assert migrate_legacy_member_schedules(store_dir) == ["legacy"]
    after = ConversationLog().get_metadata_status(key)[0]
    named = replace(
        execution.resolve_member_execution(members, "alice"), template_id="named-template"
    )
    assert after.get("execution_context") != named.to_record()
    assert {k: after[k] for k in before} == before
    assert "left as it is" in caplog.text


def test_a_session_bound_to_another_execution_is_not_rebound(members, tmp_path, caplog):
    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules

    store_dir = tmp_path / "cron-store"
    store_dir.mkdir()
    _write_crons(
        store_dir / "crons.json", [_insider_record("legacy", "alice", agent_id="named-template")]
    )
    bob = execution.resolve_member_execution(members, "bob")
    execution.bind_session_execution("cron:legacy", bob)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.cron"):
        assert migrate_legacy_member_schedules(store_dir) == ["legacy"]
    assert execution.read_session_execution("cron:legacy") == bob
    assert "belongs to another execution" in caplog.text


def test_only_the_stable_single_agent_key_is_reconciled(members, tmp_path):
    """A per-run key is fresh on every fire; the sequential path replaces its own record."""
    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules
    from kiro_crew.history import ConversationLog

    store_dir = tmp_path / "cron-store"
    store_dir.mkdir()
    _write_crons(
        store_dir / "crons.json",
        [
            _insider_record("ephemeral", "alice", agent_id="named", persistent_session=False),
            _insider_record("sequence", "alice", agent_sequence=["one", "two"]),
            _insider_record("never-fired", "alice", agent_id="named"),
        ],
    )
    for job_id in ("ephemeral", "sequence"):
        _insider_cron_session(job_id, "alice")
    assert sorted(migrate_legacy_member_schedules(store_dir)) == [
        "ephemeral",
        "never-fired",
        "sequence",
    ]
    log = ConversationLog()
    for job_id in ("ephemeral", "sequence"):
        meta = log.get_metadata_status(f"cron:{job_id}")[0]
        assert "execution_context" not in meta
        assert (meta["memory_store"], meta["agent"]) == ("member-alice", "alice")
    assert log.get_metadata_status("cron:never-fired") == ({}, True)
    assert not log._path("cron:never-fired").exists()
