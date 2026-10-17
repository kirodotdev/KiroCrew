"""Route tests: creating a spec.

Create refuses existing documents and an opt-in flag that is not a real boolean,
dispatches a self-contained, type-aware seed prompt, and cleans up orphaned
execution loops and turns before any filesystem side effect. A create that fails
unwinds only what it made: an abort spares a replacement spec, its worktree and
its slot; a failed archive restores the original name; and a name reserved by an
in-flight delete holds.
"""

from __future__ import annotations

import contextlib
import inspect
import os
import types
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes, routes_source

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _make_client,
    _never_touch_the_real_state,
    _redirect_state,
)

# ── symlink containment, state normalization, create pre-checks ──────────────
#
# One test group per finding, so a regression names the finding it re-opens.


# (4) create must not silently adopt/overwrite an existing spec


@pytest.mark.asyncio
async def test_create_refuses_existing_spec_files(tmp_path, monkeypatch):
    """An existing .kiro/specs/<name> with Kiro markdown returns 409."""
    project = tmp_path / "proj"
    existing = project / ".kiro" / "specs" / "already"
    existing.mkdir(parents=True)
    (existing / "requirements.md").write_text("# pre-existing")

    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "already", "working_dir": str(project), "spec_type": "feature"},
        )
        assert resp.status == 409
        assert "import_existing" in (await resp.json())["error"]
        # The pre-existing file is untouched.
        assert (existing / "requirements.md").read_text() == "# pre-existing"


# ── foreign-slot refusal; the seed prompt is self-contained ──────────────────


def test_seed_prompt_is_self_contained_and_type_aware():
    """The reported gap: the seed told the agent to "follow the `spec-workflow`
    skill exactly", but builtin apps are not run through bridges.register_app
    (that path symlinks from ~/.kiro/crew/apps/<name>/, which a wheel-shipped
    builtin has no copy of), so the skill is not on the agent's skill path. It
    also listed all three documents for every spec type, contradicting `quick`."""
    spec_dir = Path("/w/.kiro/specs/thing")

    quick = routes._seed_prompt("quick", "thing", spec_dir, "/w", "make it fast")
    assert "spec-workflow" not in quick, "seed still points at an unavailable skill"
    assert (
        "design.md" not in quick.split("Do NOT write design.md")[0]
    ), "quick spec is still told to write design.md"
    assert "Do NOT write design.md" in quick
    assert str(spec_dir / "requirements.md") in quick
    assert str(spec_dir / "tasks.md") in quick

    feature = routes._seed_prompt("feature", "thing", spec_dir, "/w", "")
    for f in ("requirements.md", "design.md", "tasks.md"):
        assert str(spec_dir / f) in feature, f"feature spec omits {f}"

    bug = routes._seed_prompt("bug", "thing", spec_dir, "/w", "")
    assert "root cause" in bug.lower()

    # The state-file contract must be stated inline, not deferred to the skill's
    # 'Structured state' section.
    for token in ('"decisions"', '"blocking"', '"context"', ".spec-state.json"):
        assert token in quick, f"seed no longer states {token}"
    # ...and stay plumbing, never a chat topic or a deliverable.
    assert "never mention it in chat" in quick

    # An unknown type must not lose the deliverables entirely.
    assert str(spec_dir / "requirements.md") in routes._seed_prompt(
        "weird", "thing", spec_dir, "/w", ""
    )


# ── create refuses, and unwinds, when the spec is replaced ───────────────────


@pytest.mark.asyncio
async def test_create_refuses_when_the_spec_is_replaced_during_slot_setup(tmp_path, monkeypatch):
    """The working-dir chokepoint is async, so slot setup AWAITS and a delete-and-recreate
    can land between the index insert and the dispatch. The seed prompt names OUR
    spec_dir, so dispatching would drive the replacement spec's agent with our plan."""
    client = _make_client(monkeypatch, tmp_path)
    wd = Path(os.path.realpath(tmp_path)) / "wd"
    wd.mkdir()
    other = Path(os.path.realpath(tmp_path)) / "other" / ".kiro" / "specs" / "racy"
    other.mkdir(parents=True)

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    real_ensure = routes._ensure_worker_slot

    class _Slot:
        key = "spec-builder-racy"
        _app = routes.APP_NAME
        project = ""
        _titled = False

    slot = _Slot()

    async def _ensure_then_replace(state, name, meta, **kwargs):
        out = await real_ensure(state, name, meta, **kwargs)
        # The concurrent delete+recreate, landing inside the await window.
        routes._forget_observed_slot_identity(name, str(meta.get("slot_key", "")))
        routes._save_index({"racy": {"spec_dir": str(other), "working_dir": str(other.parent)}})
        return out

    monkeypatch.setattr(routes, "_ensure_worker_slot", _ensure_then_replace)

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return slot

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs", json={"name": "racy", "working_dir": str(wd), "spec_type": "quick"}
        )
    finally:
        await client.close()

    assert resp.status == 409, "create dispatched against a replaced spec"
    assert dispatched == []
    # The REPLACEMENT's index entry survives -- the unwind is identity-pinned.
    assert routes._load_index()["racy"]["spec_dir"] == str(
        other
    ), "the unwind deleted the replacement spec's index entry"


def test_create_unwind_is_identity_pinned():
    """Source guard: the create unwind must not pop by name alone, and the identity
    recheck must precede the dispatch."""
    src = inspect.getsource(routes._handle_create)
    assert "idx.pop(name, None)" not in src, "create unwinds by name alone"
    assert "_pop_if_ours" in src
    recheck = src.index("!= str(spec_dir)")
    assert recheck < src.index("_dispatch_turn("), "create dispatches before rechecking identity"


# ── settings shapes, slot-name grammar, and the browse skip list ─────────────


def test_opt_in_flags_require_a_real_boolean():
    """The reported bypass: bool("false") is True, so a request that said NOT to
    create a worktree (or NOT to adopt existing documents) did both. These two
    flags cause side effects a retry cannot undo, so the check is exact."""
    rejected: tuple[object, ...] = ("false", "0", "no", "", 0, [], None, 1, "true")
    for truthy_but_not_true in rejected:
        assert (
            routes._opted_in({"use_worktree": truthy_but_not_true}, "use_worktree") is False
        ), f"{truthy_but_not_true!r} opted in"
    assert routes._opted_in({"use_worktree": True}, "use_worktree") is True
    assert routes._opted_in({}, "import_existing") is False

    # Source guard: no handler may go back to coercing these flags with bool().
    src = routes_source()
    for field in ("use_worktree", "import_existing"):
        assert f'bool(body.get("{field}"))' not in src, f"{field} is coerced with bool() again"


# ── a create abort spares a replacement spec ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_abort_does_not_drop_a_replacement_spec(tmp_path, monkeypatch):
    """The reported gap: the create path's two identity checks compared spec_dir
    only. A delete plus a re-import at the same name AND path during slot setup
    leaves spec_dir identical, so a stale create either removed the replacement's
    index entry on its abort path or drove the replacement's agent with its own seed
    prompt. The per-creation slot key is what separates them."""
    client = _make_client(monkeypatch, tmp_path)
    working = tmp_path / "wd"
    spec_dir = working / ".kiro" / "specs" / "reused"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# adopted")
    seeded: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: seeded.append("seed"))

    # A REPLACEMENT spec occupies the name at the same path, with its own creation.
    replacement = {
        "working_dir": str(working),
        "spec_dir": str(spec_dir),
        "spec_type": "feature",
        "status": "planning",
        "slot_key": "spec-builder-reused-99999999",
    }

    slot = types.SimpleNamespace(
        key="spec-builder-reused-99999999",
        _app=routes.APP_NAME,
        running=False,
        project="",
        messages=[],
        _titled=True,
        task=None,
    )

    class _State:
        def get_slot(self, key):
            return slot

        def get_or_create_slot(self, name, app=""):
            return slot

    real_ensure = routes._ensure_worker_slot

    async def _ensure_then_replace(state, name, meta, **kw):
        got = await real_ensure(state, name, meta, **kw)
        # The delete + re-import lands while the slot is being set up.
        routes._forget_observed_slot_identity(name, str(meta.get("slot_key", "")))
        routes._save_index({"reused": dict(replacement)})
        return got

    monkeypatch.setattr(routes, "_ensure_worker_slot", _ensure_then_replace)

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs",
            json={
                "name": "reused",
                "working_dir": str(working),
                "spec_type": "feature",
                "import_existing": True,
            },
        )
        status, body = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 409, f"{status}: {body}"
    assert body["code"] == "spec_changed_during_create"
    assert seeded == [], "the stale create seeded the replacement spec's agent"
    # And the replacement's entry survived the abort.
    entry = routes._load_index().get("reused")
    assert entry is not None, "the abort deleted the replacement spec's index entry"
    assert entry["slot_key"] == "spec-builder-reused-99999999"


def test_create_identity_checks_pin_the_creation():
    """Source guard: both create-path checks compare the slot key, not just the
    directory."""
    src = inspect.getsource(routes._handle_create)
    pop = src.index("def _pop_if_ours(")
    pop_body = src[pop : src.index("del idx[name]", pop)]
    assert "slot_key" in pop_body, "the unwind pops on the directory alone"
    post = src.index("live = current.get(name) or {}")
    assert (
        "slot_key" in src[post : src.index("_unwind_create()", post)]
    ), "the post-slot-setup check compares the directory alone"


# ── a failed archive restores the name; a reserved name holds ────────────────


@pytest.mark.asyncio
async def test_failed_archive_restores_the_original_name_and_key(tmp_path, monkeypatch):
    """A teardown that pops the entry and restores it as `<name>-2` when the name was
    taken mid-archival severs the conversation: slot keys are bound to their entry's own
    name, so a renamed entry cannot own its per-creation key -- `_slot_key` falls back
    to the name-derived form and the original conversation is unreachable. The name is
    RESERVED for the whole teardown, so a failure puts the spec back exactly as it
    was."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "keeper"
    spec_dir.mkdir(parents=True)
    key = "spec-builder-keeper-abcd1234"
    routes._save_index(
        {
            "keeper": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": key,
            }
        }
    )

    async def _archive_fails(*_a, **_kw):
        return False

    monkeypatch.setattr(routes, "_teardown_worker_slot", _archive_fails)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/keeper")
        status, body = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 503
    assert body["code"] == "archive_failed"
    assert "nothing was deleted" in body["error"]
    # No rename: the spec is back under its own name, with its own key, and the
    # conversation therefore still resolves.
    index = routes._load_index()
    assert set(index) == {"keeper"}, f"the spec was renamed or lost: {list(index)}"
    assert index["keeper"]["slot_key"] == key
    assert routes._DELETING not in index["keeper"], "the reservation was left behind"
    routes._load_index()
    assert routes._slot_key("keeper") == key, "the original conversation is unreachable"
    assert str(spec_dir) not in routes._load_deleted(), "a live spec is tombstoned"


@pytest.mark.asyncio
async def test_a_reserved_name_cannot_be_taken_mid_delete(tmp_path, monkeypatch):
    """The reservation is what makes the rename unnecessary: while the delete runs the
    entry is still present, so create refuses the name instead of racing into it."""
    client = _make_client(monkeypatch, tmp_path)
    working = tmp_path / "wd"
    spec_dir = working / ".kiro" / "specs" / "busy"
    spec_dir.mkdir(parents=True)
    routes._save_index(
        {
            "busy": {
                "spec_dir": str(spec_dir),
                "working_dir": str(working),
                "slot_key": "spec-builder-busy-11112222",
            }
        }
    )
    # Mark the entry as a delete in flight, then try to create the same name.
    assert await routes._mark_deleting(
        "busy", expect_spec_dir=str(spec_dir), expect_slot_key="spec-builder-busy-11112222"
    )

    client.app["state"] = None
    await client.start_server()
    try:
        listed = await (await client.get(f"{_BASE}/specs")).json()
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "busy", "working_dir": str(working), "spec_type": "feature"},
        )
        status = resp.status
    finally:
        await client.close()

    assert [s["name"] for s in listed["specs"]] == [], "a delete in flight is still listed"
    assert status == 409, f"the reserved name was taken: {status}"


# ── A stale create-unwind must not delete a replacement's worktree ───────────


def _removal_probe(monkeypatch):
    """Record what _remove_worktree would destroy, without touching git."""
    removed: list = []

    async def _fake_remove(repo_root, worktree_path, branch=""):
        removed.append((worktree_path, branch))

    monkeypatch.setattr(routes, "_remove_worktree", _fake_remove)
    return removed


@pytest.mark.asyncio
async def test_rollback_removes_the_worktree_when_the_name_is_still_ours(monkeypatch):
    """The control. Without this, every failed create orphans a worktree+branch."""
    removed = _removal_probe(monkeypatch)
    did = await routes._rollback_worktree_if_ours(
        "s",
        was_ours=True,
        repo_root="/repo",
        created_worktree="/repo-wt-s",
        worktree_branch="spec/s",
    )
    assert did is True
    assert removed == [("/repo-wt-s", "spec/s")]


@pytest.mark.asyncio
async def test_rollback_spares_a_replacements_worktree(monkeypatch):
    """The reported hazard. A concurrent delete + same-name recreate makes the
    identity-pinned pop return False; the worktree path is derived from the NAME,
    so it now belongs to the replacement. Removing it would force-delete that
    spec's uncommitted work and hard-delete its branch."""
    removed = _removal_probe(monkeypatch)
    did = await routes._rollback_worktree_if_ours(
        "s",
        was_ours=False,
        repo_root="/repo",
        created_worktree="/repo-wt-s",
        worktree_branch="spec/s",
    )
    assert did is False
    assert removed == [], "a stale unwind destroyed the replacement spec's worktree"


@pytest.mark.asyncio
async def test_rollback_is_a_noop_when_this_create_made_no_worktree(monkeypatch):
    removed = _removal_probe(monkeypatch)
    assert (
        await routes._rollback_worktree_if_ours(
            "s", was_ours=True, repo_root="/repo", created_worktree="", worktree_branch=""
        )
        is False
    )
    assert removed == []


def test_unwind_gates_the_rollback_on_the_pinned_pop():
    """Source guard on the WIRING: the unwind must pass the pop's own result
    through, not re-derive ownership or hardcode it."""
    src = inspect.getsource(routes._handle_create)
    unwind = src[src.index("async def _unwind_create") :]
    pop = unwind.index("was_ours = await _mutate_index(")
    call = unwind.index("_rollback_worktree_if_ours(")
    assert pop < call, "ownership must be established before the rollback"
    args = unwind[call : unwind.index(")", unwind.index("worktree_branch=worktree_branch", call))]
    assert "was_ours=was_ours" in args, (
        "the rollback is not gated on the pinned pop -- a stale unwind would "
        "force-delete a replacement spec's worktree and branch"
    )
    # The raw destructive call must NOT survive alongside the gated one.
    assert (
        "_remove_worktree(" not in unwind
    ), "the unwind still calls _remove_worktree directly, bypassing the gate"


def test_remove_worktree_is_destructive_enough_to_need_the_gate():
    """Pins WHY the gate matters. If _remove_worktree ever stops being a
    force-remove + branch delete, the gate can be re-argued rather than assumed."""
    src = inspect.getsource(routes._remove_worktree)
    assert '"worktree", "remove", "--force"' in src
    assert '"branch", "-D"' in src


def test_only_the_post_insert_unwind_needs_the_gate():
    """Scope guard: the six early rollbacks are unconditional on purpose.

    They run before the index insert, so no other request can hold the name --
    and _create_worktree would itself have failed had a concurrent create already
    made `<repo>-wt-<name>`. Gating those would orphan a worktree on every
    legitimate 400/409. Only the post-insert unwind spans an await (the slot
    acquisition) during which a delete + recreate can land.

    The ledger refusals are reached after awaits of their own, but both precede the
    insert, so this create has claimed nothing and ``created_worktree`` is still only
    ever the one it made itself -- the same reasoning that leaves the others ungated.
    """
    src = inspect.getsource(routes._handle_create)
    early = src[: src.index("async def _unwind_create")]
    assert early.count("_remove_worktree(") == 6, (
        "the early-rollback count changed -- re-audit whether the new one spans "
        "an await after the index insert (if so it needs the ownership gate too)"
    )
    assert "was_ours" not in early, "an early rollback should not need the gate"


def _maintenance_loader(service):
    """Build the AutoNudge class seam around one authoritative test service."""

    class _Loader:
        @classmethod
        @contextlib.asynccontextmanager
        async def maintenance_service(cls, base_dir=None):
            yield service

    return _Loader


@pytest.mark.asyncio
async def test_create_cleanup_removes_a_fully_orphaned_execution_loop(tmp_path, monkeypatch):
    """An empty index has an app-owned recovery path for a durable orphan."""
    _redirect_state(monkeypatch, tmp_path)
    orphan_slot = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="orphan-loop",
        active=True,
        slot_key=orphan_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(tmp_path / "old-spec" / "STOP"),
    )
    removed: list[str] = []
    torn_down: list[object] = []
    orphan_slot_state = types.SimpleNamespace(
        key=orphan_slot,
        _app=routes.APP_NAME,
        running=True,
    )

    class _State:
        def __init__(self):
            self.slot: object | None = orphan_slot_state

        def get_slot(self, key):
            return self.slot if key == orphan_slot else None

    state = _State()

    class _Svc:
        def list_all(self):
            return [] if removed else [loop]

        def get_by_slot(self, key):
            return loop if key == orphan_slot and not removed else None

        async def deactivate_and_wait(self, loop_id, *, stopped_reason=None):
            assert loop_id == loop.id
            stop_reasons.append(stopped_reason)
            loop.active = False
            return True

        async def remove(self, loop_id, *, stop_reason=""):
            removed.append(loop_id)
            loop.active = False

    async def _teardown(_state, _name, *, only_slot, require_archive):
        assert require_archive is True
        torn_down.append(only_slot)
        state.slot = None
        return True

    stop_reasons: list[str | None] = []
    service = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: service)
    monkeypatch.setattr(routes, "_AutoNudgeService", _maintenance_loader(service))
    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown)
    routes._save_index({})
    assert routes._reserve_pending_dispatch("/new/spec", "spec-builder-n-deadbeef", "n") == ""

    assert await routes._remove_orphaned_executions(state) == {orphan_slot}

    assert removed == [loop.id]
    assert stop_reasons == ["orphaned_worker"]
    assert torn_down == [orphan_slot_state]
    token = routes._reserve_pending_dispatch("/new/spec", "spec-builder-n-deadbeef", "n")
    assert token
    routes._drop_pending_dispatch(token)


@pytest.mark.asyncio
async def test_create_cleanup_rechecks_a_slot_materialized_while_its_loop_quiesces(
    tmp_path, monkeypatch
):
    """A firing timer cannot publish a worker after cleanup's slot snapshot."""
    _redirect_state(monkeypatch, tmp_path)
    orphan_slot = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="orphan-loop",
        active=True,
        slot_key=orphan_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(tmp_path / "old-spec" / "STOP"),
    )
    published = types.SimpleNamespace(
        key=orphan_slot,
        _app=routes.APP_NAME,
        running=True,
    )
    torn_down: list[object] = []

    class _State:
        slot = None

        def get_slot(self, key):
            return self.slot if key == orphan_slot else None

    state = _State()

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == orphan_slot else None

        async def deactivate_and_wait(self, loop_id, *, stopped_reason=None):
            assert loop_id == loop.id
            loop.active = False
            state.slot = published
            return True

        async def remove(self, loop_id, *, stop_reason=""):
            assert loop_id == loop.id

    async def _teardown(_state, _name, *, only_slot, require_archive):
        assert require_archive is True
        torn_down.append(only_slot)
        state.slot = None
        return True

    service = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: service)
    monkeypatch.setattr(routes, "_AutoNudgeService", _maintenance_loader(service))
    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown)
    routes._save_index({})

    assert await routes._remove_orphaned_executions(state) == {orphan_slot}
    assert torn_down == [published]


@pytest.mark.asyncio
async def test_create_cleanup_keeps_a_quiesced_loop_until_worker_archive_succeeds(
    tmp_path, monkeypatch
):
    """A retry or restart retains the identity when cancellation times out."""
    _redirect_state(monkeypatch, tmp_path)
    orphan_slot = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="orphan-loop",
        active=True,
        slot_key=orphan_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(tmp_path / "old-spec" / "STOP"),
    )
    slot = types.SimpleNamespace(key=orphan_slot, _app=routes.APP_NAME, running=True)
    removed: list[str] = []

    class _State:
        def get_slot(self, key):
            return slot if key == orphan_slot else None

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == orphan_slot else None

        async def deactivate_and_wait(self, loop_id, *, stopped_reason=None):
            assert loop_id == loop.id
            loop.active = False
            return True

        async def remove(self, loop_id, *, stop_reason=""):
            removed.append(loop_id)

    async def _failed_archive(*_args, **_kwargs):
        return False

    service = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: service)
    monkeypatch.setattr(routes, "_AutoNudgeService", _maintenance_loader(service))
    monkeypatch.setattr(routes, "_teardown_worker_slot", _failed_archive)
    routes._save_index({})

    with pytest.raises(RuntimeError, match="could not be archived"):
        await routes._remove_orphaned_executions(_State())

    assert loop.active is False
    assert removed == []
    assert routes._matching_execution_loops("", "", set(), include_orphans=True) == {
        orphan_slot: loop.id
    }


@pytest.mark.asyncio
async def test_create_cleanup_loads_durable_loops_when_autonudge_is_disabled(tmp_path, monkeypatch):
    """Disabling timers cannot hide an old loop from Create recovery."""
    _redirect_state(monkeypatch, tmp_path)
    orphan_slot = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="orphan-loop",
        active=True,
        slot_key=orphan_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(tmp_path / "old-spec" / "STOP"),
    )
    removed: list[str] = []

    class _OfflineSvc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == orphan_slot else None

        async def deactivate_and_wait(self, loop_id, *, stopped_reason=None):
            assert loop_id == loop.id
            loop.active = False
            return True

        async def remove(self, loop_id, *, stop_reason=""):
            removed.append(loop_id)

    offline = _OfflineSvc()

    class _Loader:
        @classmethod
        @contextlib.asynccontextmanager
        async def maintenance_service(cls, base_dir=None):
            yield offline

    class _State:
        def get_slot(self, _key):
            return None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)
    monkeypatch.setattr(routes, "_AutoNudgeService", _Loader)
    routes._save_index({})

    assert await routes._remove_orphaned_executions(_State()) == {orphan_slot}
    assert removed == [loop.id]


def test_create_removes_orphaned_loops_before_filesystem_side_effects():
    """Recovery precedes worktree and spec-directory creation."""
    src = inspect.getsource(routes._handle_create)
    cleanup_at = src.index("await _remove_orphaned_executions(")
    worktree_at = src.index("await _create_worktree(")
    spec_dir_at = src.index("_prepare_spec_dir")

    assert cleanup_at < worktree_at < spec_dir_at


@pytest.mark.asyncio
async def test_create_refuses_an_unreadable_index_before_orphan_cleanup(tmp_path, monkeypatch):
    """Invalid index bytes cannot make durable workers look orphaned."""
    client = _make_client(monkeypatch, tmp_path)
    index_path = routes._index_path()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("not-json")
    cleaned: list[bool] = []
    prepared: list[bool] = []

    async def _cleanup(_state):
        cleaned.append(True)
        return set()

    def _prepare(*_args, **_kwargs):
        prepared.append(True)
        return tmp_path / "spec", ""

    monkeypatch.setattr(routes, "_remove_orphaned_executions", _cleanup)
    monkeypatch.setattr(routes, "_prepare_spec_dir", _prepare)
    await client.start_server()
    try:
        response = await client.post(
            f"{_BASE}/specs",
            json={"name": "new", "working_dir": str(tmp_path), "spec_type": "quick"},
        )
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 503, payload
    assert payload.get("code") == "spec_index_unavailable"
    assert cleaned == []
    assert prepared == []
    assert index_path.read_text() == "not-json"


@pytest.mark.asyncio
async def test_create_refuses_before_side_effects_when_orphan_cleanup_fails(tmp_path, monkeypatch):
    """An unremovable orphan cannot overlap a partially-created replacement."""
    client = _make_client(monkeypatch, tmp_path)
    prepared: list[str] = []

    async def _fail_cleanup(_state):
        raise OSError("autonudge store unavailable")

    def _prepare(*_args, **_kwargs):
        prepared.append("prepared")
        return tmp_path / "spec", None

    monkeypatch.setattr(routes, "_remove_orphaned_executions", _fail_cleanup)
    monkeypatch.setattr(routes, "_prepare_spec_dir", _prepare)
    await client.start_server()
    try:
        response = await client.post(
            f"{_BASE}/specs",
            json={"name": "new", "working_dir": str(tmp_path), "spec_type": "quick"},
        )
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 503, payload
    assert payload.get("code") == "orphaned_execution_cleanup_failed"
    assert prepared == []


@pytest.mark.asyncio
async def test_create_cleanup_archives_an_orphaned_direct_turn_without_a_loop(
    tmp_path, monkeypatch
):
    """Removing the last index row cannot detach an already-running worker."""
    _redirect_state(monkeypatch, tmp_path)
    old_slot = "spec-builder-s-1234abcd"
    old_dir = str(tmp_path / "old-spec")
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": old_slot}})
    routes._save_index({})
    slot = types.SimpleNamespace(key=old_slot, _app=routes.APP_NAME, running=True)
    torn_down: list[object] = []

    class _Svc:
        def list_all(self):
            return []

    class _State:
        def __init__(self):
            self.slot: object | None = slot

        def get_slot(self, key):
            return self.slot if key == old_slot else None

    state = _State()

    async def _teardown(_state, _name, *, only_slot, require_archive):
        assert require_archive is True
        torn_down.append(only_slot)
        state.slot = None
        return True

    service = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: service)
    monkeypatch.setattr(routes, "_AutoNudgeService", _maintenance_loader(service))
    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown)

    assert await routes._remove_orphaned_executions(state) == {old_slot}
    assert torn_down == [slot]
    assert old_slot not in routes._OBSERVED_SLOT_KEYS.values()
    assert old_slot not in routes._OBSERVED_SPEC_DIRS


@pytest.mark.asyncio
async def test_create_cleanup_keeps_the_witness_when_orphan_archive_fails(tmp_path, monkeypatch):
    """A failed transcript archive remains retryable and blocks replacement."""
    _redirect_state(monkeypatch, tmp_path)
    old_slot = "spec-builder-s-1234abcd"
    old_dir = str(tmp_path / "old-spec")
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": old_slot}})
    routes._save_index({})
    slot = types.SimpleNamespace(key=old_slot, _app=routes.APP_NAME, running=True)

    class _Svc:
        def list_all(self):
            return []

    class _State:
        def get_slot(self, key):
            return slot if key == old_slot else None

    async def _failed_archive(*_args, **_kwargs):
        return False

    service = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: service)
    monkeypatch.setattr(routes, "_AutoNudgeService", _maintenance_loader(service))
    monkeypatch.setattr(routes, "_teardown_worker_slot", _failed_archive)

    with pytest.raises(RuntimeError, match="could not be archived"):
        await routes._remove_orphaned_executions(_State())

    assert routes._OBSERVED_SLOT_KEYS["s"] == old_slot
    assert routes._OBSERVED_SPEC_DIRS[old_slot] == routes._decision_key(old_dir)


def test_global_orphan_scan_ignores_an_unrelated_monitor_without_a_sentinel(tmp_path, monkeypatch):
    """An empty target cannot turn two empty directory keys into a direct match."""
    _redirect_state(monkeypatch, tmp_path)
    loop = types.SimpleNamespace(
        id="ordinary-loop",
        active=True,
        slot_key="spec-builder-ordinary-1234abcd",
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}ordinary'. Continue.",
        stop_sentinel_path="",
    )

    class _Svc:
        def list_all(self):
            return [loop]

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    routes._save_index({})

    assert routes._matching_execution_loops("", "", set(), include_orphans=True) == {}


@pytest.mark.parametrize("raw_slot", [None, "not-owned"])
def test_restart_keeps_a_bad_slot_row_from_orphaning_its_durable_loop(
    tmp_path, monkeypatch, raw_slot
):
    """Cold-start name and directory witnesses do not depend on a valid raw key."""
    _redirect_state(monkeypatch, tmp_path)
    old_dir = str(tmp_path / "old-spec")
    old_slot = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="loop-s",
        active=True,
        slot_key=old_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(Path(old_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": raw_slot}})

    assert routes._OBSERVED_SLOT_KEYS == {}
    assert routes._SLOT_KEYS == {}
    assert routes._matching_execution_loops("", "", set(), include_orphans=True) == {}


def test_the_refusal_unwinds_a_worktree_it_created():
    """Source guard: the 503 must not leak the worktree create just made.

    Every other refusal in this handler removes it; this one is reached after the
    worktree exists, so omitting it would leave an orphaned checkout behind.
    """
    src = inspect.getsource(routes._handle_create)
    at = src.index("decision_record_not_cleared")
    # Look back from the refusal payload to the branch it belongs to.
    branch = src[src.index("if not cleared", 0) : at]
    assert (
        "_remove_worktree(" in branch
    ), "the stale-record refusal returns without removing a worktree it created"
