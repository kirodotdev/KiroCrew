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
    # and member-auth binders either run on keys that never reach the vouch or take
    # their store from a caller argument, so none of them may vouch.
    #
    # "True" is an establishing site: the store came from an authorization, not from
    # a record the session can write. `record_agent_selection` threads the decision
    # because it serves both shapes.
    expected = {
        ("dashboard/chat_fork.py", "_bind_fork_execution"): "ABSENT",
        ("dashboard/chat_persistence.py", "_pin_private_agent_assignment"): "ABSENT",
        ("dashboard/handlers/hooks.py", "bind_captured"): "True",
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
