"""Route tests: execute, handoff, pause and stop.

Handoff authorizes and arms the build loop before it dispatches, refuses a second
execute while one is building, and unwinds every armed effect when authorization,
the index commit or a concurrent delete fails. Pause and stop halt this app's own
turn only; execute, handoff and stop are owner-gated, before any side effect; and
the app never grants worker trust.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import time
import types
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes, routes_source

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _decision_spec,
    _make_client,
    _never_touch_the_real_state,
    _NoopState,
    _RecordingSlot,
    _redirect_state,
    _slot_stub,
)

# ── descriptor-pinned spec reads; this app grants no worker trust ────────────


# (2) the app must not grant auto-approval at all


def test_app_never_grants_worker_trust():
    """The load-bearing invariant: this app never stamps worker trust.

    The embed renders working Approve/Trust/Reject controls, so the permission
    prompt is visible where the user is, and a backend grant cannot be bounded
    honestly: a wall-clock TTL enforced on the UI's status poll stops enforcing the
    moment the page closes, while the grant survives. The decision belongs to the
    user, via core's own trust mechanism, where it is auditable as their choice.
    """

    src = routes_source()
    assert "slot._trust = True" not in src
    # And no revive-by-another-name.
    assert "_trust = True" not in src


def test_no_poll_dependent_ttl_machinery_remains():
    """The TTL is gone rather than left half-wired: a guard that only runs while
    a browser tab is open is not enforcement, and keeping it would imply a bound
    that does not hold."""

    src = routes_source()
    for gone in ("_enforce_trust_ttl", "_mark_trust_granted", "_TRUST_TTL_SECS"):
        assert gone not in src, f"{gone} still referenced"


@pytest.mark.asyncio
async def test_halt_execution_leaves_user_trust_alone(tmp_path):
    """Stop must sentinel the loop but NOT clear trust — if the user granted it
    from the approval card, that is their decision to reverse."""

    class _Slot:
        _trust = True

    slot = _Slot()

    class _State:
        def get_slot(self, key):
            return slot

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    await routes._halt_execution(_State(), "s", spec_dir, reason="user stop")
    assert (spec_dir / routes._STOP_FILE).is_file()  # loop is sentinelled
    assert slot._trust is True  # user's choice preserved


# ── stale-index writes (concurrent delete resurrects a spec) ─────────────────


def test_handoff_commits_before_dispatch_and_unwinds_on_deletion():
    """Source guard for the ordering that closes the reported race: the index
    commit must precede _dispatch_turn, and the abort path must undo the loop and
    the slot it just created."""

    src = inspect.getsource(routes._handle_handoff)
    # Delimit the release helper by its own last statement: its body contains a
    # _touch_spec call (the state revert), so slicing to a commit cut it short.
    release = src.index("async def _release(")
    release_end = src.index('_audit("spec_handoff_aborted"', release)
    helper = src[release:release_end]
    assert (
        "_remove_nudge_loop_for_slot(" in helper
    ), "the release leaves the armed nudge loop running"
    assert "_teardown_worker_slot(" in helper, "the release leaves the worker slot behind"
    assert 'status="planning"' in helper, "the release leaves the spec marked executing"

    claim = src.index("await _claim_execution(")
    arm = src.index("await authorize_and_add_nudge(")
    dispatch = src.index("_dispatch_turn(")
    # The claim is a single atomic compare-and-set that both refuses a duplicate
    # and records the state. It must precede the slot, the arm and the dispatch:
    # everything after it is a side effect that the claim is what authorizes.
    assert claim < src.index("_ensure_worker_slot("), "the slot is created before the claim"
    assert claim < arm, "the loop is armed before the execution state is recorded"
    assert claim < dispatch, "handoff dispatches before claiming the run"
    assert src[claim:dispatch].count("_release(") == 11, (
        "an abort arm does not release the loop, the recorded state and the slot"
        " — every early return between the claim and the dispatch must release,"
        " including both same-slot busy checks and the final alias check"
    )


# ── blocking sentinel write on the event loop ────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   \n\n\t\n",
        "# Tasks\n\nProse with no checkbox at all.\n",
        "- [x] done\n- [X] also done\n",
        "- [ x] not an empty box\n",
        "-[ ] no space after the marker\n",
    ],
)
def test_open_task_predicate_rejects_a_plan_with_nothing_to_do(body):
    """A tasks.md the autonomous prompt cannot act on: the prompt tells the agent
    to work through each UNCHECKED task, so an empty file, prose, or an
    all-checked list leaves the run with no work."""
    assert routes._has_open_task(body) is False


@pytest.mark.parametrize(
    "body",
    [
        "- [ ] task",
        "* [ ] star marker",
        "+ [ ] plus marker",
        "1. [ ] ordered marker",
        "2) [ ] paren-ordered marker",
        "- [] bare brackets",
        "  - [ ] indented under a heading",
        "\t- [ ] tab-indented",
        "- [x] done first\n- [ ] then this one is open\n",
    ],
)
def test_open_task_predicate_accepts_the_marker_styles_a_model_writes(body):
    """The list is model-written and its marker style varies between runs, so the
    gate accepts every Markdown checkbox spelling rather than only ``- [ ]``."""
    assert routes._has_open_task(body) is True


def test_prepare_handoff_refuses_a_tasks_file_with_no_open_task(tmp_path):
    """The reported gap: the gate stat-ed tasks.md and accepted it on existence
    alone, so a zero-byte or all-checked plan armed an autonomous loop that had
    nothing to act on while the spec still read as a finished Tasks phase."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    tasks = spec_dir / "tasks.md"

    tasks.write_text("")
    assert routes._prepare_handoff(spec_dir)[0] is False, "empty tasks.md armed a run"

    tasks.write_text("- [x] everything already done\n")
    assert routes._prepare_handoff(spec_dir)[0] is False, "all-checked tasks.md armed a run"

    tasks.write_text("- [x] done\n- [ ] still open\n")
    assert routes._prepare_handoff(spec_dir)[0] is True


# ── handoff authorization, pause, and stop ───────────────────────────────────


@pytest.mark.asyncio
async def test_handoff_refuses_when_authorization_is_unavailable(tmp_path, monkeypatch):
    """The reported fail-open: the arm was wrapped in a broad except that logged
    'running single turn' and fell through to _dispatch_turn — starting an
    autonomous run with NO authorization, no message limit, no sentinel refusal
    and no SEL audit, exactly when the enforcing machinery was unavailable."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "s"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index(
        {
            "s": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": "spec-builder-s",
            }
        }
    )

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)

    class _State:
        def get_or_create_slot(self, name, app=""):
            class _S:
                key = name
                _app = app

            return _S()

        def get_slot(self, key):
            return None

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/s/handoff")
    finally:
        await client.close()

    assert resp.status == 503, "handoff started an unauthorized run"
    assert dispatched == [], "an autonomous turn was dispatched without authorization"
    assert routes._load_index()["s"].get("status") != "executing"


@pytest.mark.asyncio
async def test_handoff_refuses_when_authorization_raises(tmp_path, monkeypatch):
    """Same contract for an authorization helper that raises rather than
    returning an error string."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "s"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index(
        {
            "s": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": "spec-builder-s",
            }
        }
    )

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    async def _boom(**_kw):
        raise RuntimeError("authz backend down")

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _boom)

    class _State:
        def get_or_create_slot(self, name, app=""):
            class _S:
                key = name
                _app = app

            return _S()

        def get_slot(self, key):
            return None

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/s/handoff")
    finally:
        await client.close()

    assert resp.status == 503
    assert dispatched == []


@pytest.mark.asyncio
async def test_pause_stops_the_in_flight_turn(tmp_path):
    """The reported defect: Pause wrote the STOP sentinel and removed the nudge
    loop — both of which only prevent FUTURE nudges — then reported 'planning'
    while the in-flight _run_chat kept editing the user's files."""
    stopped: list[str] = []
    cancelled = {"v": False}

    class _Task:
        def cancel(self):
            cancelled["v"] = True

        def done(self):
            return False

        def __await__(self):
            async def _done():
                return None

            return _done().__await__()

    class _Slot:
        key = "spec-builder-live"
        _app = "spec-builder"
        running = True
        task = _Task()

    class _Sessions:
        async def stop_turn(self, key, force=False):
            stopped.append(key)

    class _State:
        sessions = _Sessions()

        def get_slot(self, key):
            return _Slot() if key == "spec-builder-live" else None

    assert await routes._halt_active_turn(_State(), "live") is True
    assert stopped, "cooperative stop_turn was never called"
    assert cancelled["v"] is True, "the in-flight turn was not cancelled"


@pytest.mark.asyncio
async def test_pause_leaves_a_foreign_slot_alone():
    """Anti-collision: a slot this app does not own must not be stopped because
    its key happens to match."""

    class _Slot:
        key = "spec-builder-x"
        _app = "some-other-app"
        running = True
        task = None

    class _State:
        def get_slot(self, key):
            return _Slot()

    assert await routes._halt_active_turn(_State(), "x") is False


def test_stop_handler_halts_the_running_turn():
    """Source guard so the halt cannot regress to sentinel-only."""

    assert "_halt_active_turn(" in inspect.getsource(routes._halt_execution)


# ── abort cleanup spares replacements; status is served reconciled ───────────


@pytest.mark.asyncio
async def test_abort_cleanup_spares_a_replacement_slot():
    """Both cleanups look the slot up BY NAME, so unwinding a refused handoff must
    not destroy the slot of a same-name spec that has replaced ours."""

    class _Slot:
        def __init__(self, tag):
            self.tag = tag
            self.key = "spec-builder-x"
            self._app = routes.APP_NAME
            self.running = False
            self.task = None

    ours = _Slot("ours")
    replacement = _Slot("replacement")
    slots = {"spec-builder-x": replacement}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

    # Pinned to OUR captured slot: the registry now holds a different object.
    await routes._teardown_worker_slot(_State(), "x", only_slot=ours)
    assert "spec-builder-x" in slots, "the replacement spec's slot was destroyed"

    # Unpinned (delete's own path) still tears the live slot down.
    await routes._teardown_worker_slot(_State(), "x")
    assert "spec-builder-x" not in slots


@pytest.mark.asyncio
async def test_abort_cleanup_spares_a_replacement_nudge_loop(monkeypatch):
    """Same for the autonudge loop: get_by_slot keys off the name, so an unpinned
    removal cancelled the replacement spec's execution loop."""
    removed: list[str] = []

    class _Loop:
        id = "loop-new"

    class _Svc:
        def get_by_slot(self, key):
            return _Loop()

        async def remove(self, loop_id, *, stop_reason=""):
            removed.append(loop_id)

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    await routes._remove_nudge_loop("x", only_loop_id="loop-ours")
    assert removed == [], "the replacement spec's loop was cancelled"

    await routes._remove_nudge_loop("x", only_loop_id="loop-new")
    assert removed == ["loop-new"]


def test_handoff_abort_passes_both_captures():
    """Source guard: the abort path must pin both cleanups."""

    src = inspect.getsource(routes._handle_handoff)
    assert "only_loop_id=" in src and "only_slot=slot" in src


# ── handoff unwinds when the index commit raises ─────────────────────────────


@pytest.mark.asyncio
async def test_handoff_unwinds_when_the_index_commit_raises(tmp_path, monkeypatch):
    """The original hole was a raising index write AFTER the loop was armed: the
    user was told the run failed while the persisted timer dispatched execution
    anyway. The commit now runs FIRST, so the same failure must leave nothing armed
    at all -- and still no dispatch, no slot and no "executing" state."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "boom"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index({"boom": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    removed: list[str] = []
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    class _Loop:
        id = "loop-armed"

    class _Svc:
        """Faithful stub: see the sibling unwind test -- the loop only exists once
        the handoff arms it, so the already-executing pre-check sees none."""

        def __init__(self):
            self.armed = False

        def get_by_slot(self, key):
            return _Loop() if self.armed else None

        async def add(self, **_kw):
            self.armed = True
            return _Loop()

        async def remove(self, loop_id, *, stop_reason=""):
            self.armed = False
            removed.append(loop_id)

    svc = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: svc)

    async def _authz(**_kw):
        # The handoff arms through authorize_and_add_nudge, not svc.add -- reflect
        # that here so the unwind has a loop of ours to remove.
        svc.armed = True
        return _Loop(), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authz)

    async def _boom(*_a, **_kw):
        raise OSError("index write failed")

    # The state write that can fail is now the atomic claim, and it runs BEFORE
    # anything is armed or created.
    monkeypatch.setattr(routes, "_claim_execution", _boom)

    class _Slot:
        key = "spec-builder-boom"
        _app = routes.APP_NAME
        project = ""
        _titled = False
        running = False
        task = None

    slot = _Slot()
    slots: dict = {}  # the slot does NOT pre-exist: this request creates it

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

        def get_or_create_slot(self, name, app=""):
            slots["spec-builder-boom"] = slot
            return slot

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/boom/handoff")
    finally:
        await client.close()

    assert resp.status == 500
    assert dispatched == [], "a turn was dispatched despite the failed commit"
    assert removed == [], f"a loop was armed before the state was recorded: {removed}"
    assert svc.armed is False, "the loop was armed despite the failed commit"
    assert "spec-builder-boom" not in slots, "the worker slot was left behind"
    # And the spec is not left claiming to be executing.
    assert routes._load_index()["boom"].get("status") != "executing"


# ── delete ordering and handoff-unwind gating ────────────────────────────────


@pytest.mark.asyncio
async def test_failed_handoff_keeps_a_pre_existing_conversation(tmp_path, monkeypatch):
    """The reported defect: the unwind closed the worker slot unconditionally. When
    the slot ALREADY existed it carries the user's conversation (and possibly a
    running turn), so destroying it because a later index write failed lost work the
    handoff never owned. Only a slot this request created may be torn down."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "chatty"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index(
        {
            "chatty": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": "spec-builder-chatty",
            }
        }
    )

    class _Loop:
        id = "loop-armed"

    class _Svc:
        """Faithful stub: no loop exists until the handoff arms one, which is what
        the already-executing pre-check reads."""

        def __init__(self):
            self.armed = False

        def get_by_slot(self, key):
            return _Loop() if self.armed else None

        async def add(self, **_kw):
            self.armed = True
            return _Loop()

        async def remove(self, loop_id, *, stop_reason=""):
            self.armed = False

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    async def _authz(**_kw):
        return _Loop(), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authz)

    async def _boom(*_a, **_kw):
        raise OSError("index write failed")

    monkeypatch.setattr(routes, "_touch_spec", _boom)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: None)

    class _Slot:
        key = "spec-builder-chatty"
        _app = routes.APP_NAME
        project = ""
        _titled = True
        running = False
        task = None

    existing = _Slot()
    slots = {"spec-builder-chatty": existing}  # PRE-EXISTING conversation

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

        def get_or_create_slot(self, name, app=""):
            return existing

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/chatty/handoff")
    finally:
        await client.close()

    assert resp.status == 500
    assert (
        slots.get("spec-builder-chatty") is existing
    ), "a pre-existing conversation was destroyed by the failed handoff"


def test_handoff_unwind_is_gated_on_having_created_the_slot():
    """Source guard: the unwind must consult the pre-existence flag."""
    src = inspect.getsource(routes._handle_handoff)
    assert "slot_pre_existed" in src
    start = src.index("async def _release(")
    # Delimit by the unwind's own last statement rather than the next "try:" —
    # the body contains one now (the best-effort loop removal), and slicing to it
    # cut the assertion's search space to nothing.
    release = src[start : src.index('_audit("spec_handoff_aborted"', start)]
    assert (
        "if owned and not slot_pre_existed:" in release
    ), "unwind tears down a slot it may not have created"


# ── slot_key is the deciding identity; handoff refuses early ─────────────────


@pytest.mark.asyncio
async def test_second_handoff_is_refused_while_executing(tmp_path, monkeypatch):
    """The reported duplicate: a second execute queued another prompt on the same
    slot. Pause cancels the running turn, then the queued duplicate drains
    immediately -- so the run the user stopped carried on. The second handoff is
    now refused BEFORE any side effect."""
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "busy"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("turn"))

    class _Loop:
        id = "loop-live"

    class _Svc:
        def get_by_slot(self, key):
            return None

        async def remove(self, loop_id, *, stop_reason=""):
            pass

    # The autonudge service must be available: it is checked before the claim (it
    # writes nothing), so an unavailable service would answer 503 first and the
    # duplicate refusal would never be reached.
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    async def _authz(**_kw):
        return _Loop(), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authz)

    for label, index, slot_running in (
        (
            "indexed status",
            {
                "busy": {
                    "spec_dir": str(spec_dir),
                    "working_dir": str(tmp_path / "wd"),
                    "status": "executing",
                }
            },
            False,
        ),
        (
            "live slot",
            {"busy": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}},
            True,
        ),
    ):
        # The client redirects state, so the index must be written AFTER it exists
        # (the autouse fixture points elsewhere until then).
        client = _make_client(monkeypatch, tmp_path)
        routes._save_index(index)
        slot = types.SimpleNamespace(
            key=routes._slot_key("busy"),
            _app=routes.APP_NAME,
            running=slot_running,
            project="",
            messages=[],
            _titled=True,
        )

        class _State:
            def get_slot(self, key):
                return slot

            def get_or_create_slot(self, name, app=""):
                return slot

        client.app["state"] = _State()
        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/busy/execute", json={})
            # Read the body BEFORE closing: the stream dies with the client.
            status, body = resp.status, await resp.json()
        finally:
            await client.close()

        assert status == 409, f"{label}: {status}"
        assert "already building" in body["error"], label
        assert dispatched == [], f"{label}: a duplicate turn was dispatched"


def test_handoff_refuses_before_any_side_effect():
    """Source guard: the already-executing check must precede the slot acquisition
    and the dispatch, or the refusal comes too late to matter."""
    src = inspect.getsource(routes._handle_handoff)
    guard = src.index("already building")
    assert guard < src.index("_ensure_worker_slot("), "the slot is acquired before the refusal"
    assert guard < src.index("_dispatch_turn("), "the turn is dispatched before the refusal"


# ── mutations pin the creation; failures revert armed state ──────────────────


@pytest.mark.asyncio
async def test_authorization_failure_reverts_the_recorded_execution_state(tmp_path, monkeypatch):
    """The reported ordering defect: the loop was armed BEFORE the execution state
    was recorded, so a shutdown between the two persisted a shielded timer with no
    execution state -- and the restored timer ran something Pause could not stop,
    because Pause keys off that state. Recording first inverts the failure, which
    means a refused authorization must now revert what was recorded."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "nope"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index(
        {
            "nope": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": "spec-builder-nope-1234abcd",
            }
        }
    )
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    class _Svc:
        def get_by_slot(self, key):
            return None

        async def remove(self, loop_id, *, stop_reason=""):
            pass

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    async def _refuse(**_kw):
        return None, "slot is owned by another app", 403

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _refuse)

    slot = types.SimpleNamespace(
        key=routes._slot_key("nope"),
        _app=routes.APP_NAME,
        running=False,
        project="",
        messages=[],
        _titled=True,
        task=None,
    )
    slots: dict = {}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

        def get_or_create_slot(self, name, app=""):
            slots[slot.key] = slot
            return slot

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/nope/execute", json={})
        status = resp.status
    finally:
        await client.close()

    assert status == 403
    assert dispatched == [], "a turn was dispatched despite refused authorization"
    entry = routes._load_index()["nope"]
    assert entry.get("status") != "executing", "the spec is left claiming to be executing"
    assert slot.key not in slots, "the worker slot was left behind"


@pytest.mark.asyncio
async def test_deletion_during_authorization_removes_the_armed_loop(tmp_path, monkeypatch):
    """Recording before arming moved one window: a DELETE landing during the arm
    tears down the loops it can see BY NAME, and ours arrives after. The post-arm
    re-verification catches that and removes it."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "gone"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    captured_slot_key = "spec-builder-gone-1234abcd"
    routes._save_index(
        {
            "gone": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": captured_slot_key,
            }
        }
    )
    removed: list[str] = []
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    class _Loop:
        id = "loop-armed"

    class _Svc:
        def __init__(self):
            self.armed = False

        def get_by_slot(self, key):
            return _Loop() if self.armed and key == captured_slot_key else None

        async def remove(self, loop_id, *, stop_reason=""):
            self.armed = False
            removed.append(loop_id)

    svc = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: svc)

    async def _authz(**_kw):
        # The spec is deleted while authorization is in flight.
        routes._save_index({})
        svc.armed = True
        return _Loop(), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authz)

    slot = types.SimpleNamespace(
        key=routes._slot_key("gone"),
        _app=routes.APP_NAME,
        running=False,
        project="",
        messages=[],
        _titled=True,
        task=None,
    )
    slots: dict = {}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

        def get_or_create_slot(self, name, app=""):
            slots[slot.key] = slot
            return slot

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/gone/execute", json={})
        status = resp.status
    finally:
        await client.close()

    assert status == 409
    assert dispatched == [], "a turn was dispatched for a deleted spec"
    assert removed == ["loop-armed"], f"the armed loop was left nudging: {removed}"
    assert slot.key not in slots, "the worker slot was left behind"


# ── the arming window survives polling ───────────────────────────────────────


@pytest.mark.asyncio
async def test_polling_does_not_reconcile_away_the_arming_window(tmp_path, monkeypatch):
    """The reported consequence of recording state before arming: between those two
    steps there is legitimately no loop and no running turn, so a detail/list poll
    landing in that window reconciled "executing" back to "planning" -- which hid
    Pause for the whole run that followed. The arming marker exempts exactly that
    window, and only while it is fresh."""
    _redirect_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)

    class _Idle:
        running = False

    spec_dir = str(tmp_path / "p" / ".kiro" / "specs" / "arming")

    # Arming right now: no loop yet, but the state must stand.
    meta = {"spec_dir": spec_dir, "status": "executing", "exec_arming_at": time.time()}
    routes._save_index({"arming": dict(meta)})
    assert await routes._effective_status("arming", meta, _Idle()) == "executing"
    assert routes._load_index()["arming"]["status"] == "executing", "the poll cleared it"

    # A stale marker must NOT mask the reconciliation forever: a process that died
    # mid-arm would otherwise leave the spec building with nothing running.
    stale = {
        "spec_dir": spec_dir,
        "status": "executing",
        "exec_arming_at": time.time() - (routes._ARMING_GRACE_SECS + 5),
    }
    routes._save_index({"arming": dict(stale)})
    assert await routes._effective_status("arming", stale, _Idle()) == "planning"

    # A non-numeric marker is ignored rather than raising.
    junk = {"spec_dir": spec_dir, "status": "executing", "exec_arming_at": "soon"}
    routes._save_index({"arming": dict(junk)})
    assert await routes._effective_status("arming", junk, _Idle()) == "planning"


def test_handoff_stamps_and_clears_the_arming_marker():
    """Source guard: the marker is set by the pre-arm commit and cleared once the
    loop exists, so the exemption lasts for the arming window and not past it."""
    # The stamp is part of the atomic claim; the clear is in the handler, after the
    # loop exists.
    assert 'meta["exec_arming_at"] = now' in inspect.getsource(
        routes._claim_execution
    ), "the claim no longer marks the pre-arm window"
    src = inspect.getsource(routes._handle_handoff)
    claim = src.index("await _claim_execution(")
    arm = src.index("await authorize_and_add_nudge(")
    clear = src.index("exec_arming_at=0.0", arm)
    assert claim < arm < clear, "the marker does not bracket the arm"


# ── claims serialize; delete tombstones before it drops the entry ────────────


@pytest.mark.asyncio
async def test_concurrent_execute_claims_are_serialized(tmp_path, monkeypatch):
    """The reported hole: reading the status and committing it in a separate step is
    not a guard. Two concurrent execute requests both read "planning", both pass,
    and both dispatch -- Pause then cancels one prompt while the other drains and
    keeps editing the user's files. Exactly one caller may win the claim."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "p" / ".kiro" / "specs" / "race")
    routes._save_index({"race": {"spec_dir": spec_dir, "slot_key": "spec-builder-race-1"}})
    monkeypatch.setattr(routes, "_exec_loop_active", lambda _n: False)

    results = await asyncio.gather(
        *[
            routes._claim_execution(
                "race",
                expect_spec_dir=spec_dir,
                expect_slot_key="spec-builder-race-1",
                live_running=False,
            )
            for _ in range(8)
        ]
    )
    winners = [r for r, _entry in results if r == routes._CLAIM_OK]
    assert len(winners) == 1, f"{len(winners)} callers claimed the same run"
    assert all(r == routes._CLAIM_TAKEN for r, _e in results if r != routes._CLAIM_OK)
    assert routes._load_index()["race"]["status"] == "executing"


def test_queued_dispatch_preserves_structural_directive_provenance():
    slot = _RecordingSlot()

    routes._dispatch_turn(_NoopState(), slot, "automated seed")
    routes._dispatch_turn(
        _NoopState(),
        slot,
        "human answer",
        directive_user_origin=True,
    )

    assert slot.queued_origins == [False, True]


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (routes._handle_create, False),
        (routes._handle_handoff, False),
        (routes._handle_message, True),
        (routes._deliver_pending_decision, True),
    ],
)
def test_spec_dispatch_callers_assign_directive_provenance(handler, expected):
    """Only direct human input may authorize session-retargeting directives."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_dispatch_turn"
    ]
    assert len(calls) == 1
    keyword = next(
        (kw for kw in calls[0].keywords if kw.arg == "directive_user_origin"),
        None,
    )
    if keyword is None:
        actual = False
    else:
        assert isinstance(keyword.value, ast.Constant)
        actual = keyword.value.value
    assert actual is expected


@pytest.mark.asyncio
async def test_handoff_refuses_when_its_own_slot_starts_during_authorization(tmp_path, monkeypatch):
    """Authorization awaits while channel traffic can start the same slot.

    The pre-arm snapshot is not authoritative after that await. Dispatching the
    build would queue it behind the new turn, and Pause deliberately clears that queue,
    despite this endpoint reporting that execution started.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    state, slot = _slot_stub()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    async def _authorized(**_kw):
        slot.running = True
        return types.SimpleNamespace(id="loop-raced"), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    released: list[str] = []

    async def _remove(_slot_key, *, only_loop_id=None, stop_reason=""):
        released.append(only_loop_id)

    monkeypatch.setattr(routes, "_remove_nudge_loop_for_slot", _remove)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/s/handoff", json={"spec_dir": spec_dir})
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_agent_busy", payload
    assert dispatched == []
    assert released == ["loop-raced"], "the armed timer survived the busy refusal"
    assert routes._load_index()["s"].get("status") == "planning"


@pytest.mark.asyncio
async def test_handoff_refuses_when_its_own_slot_starts_during_the_final_repin(
    tmp_path, monkeypatch
):
    """The final freshness write awaits, so channel traffic can start there too."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    state, slot = _slot_stub()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    async def _authorized(**_kw):
        return types.SimpleNamespace(id="loop-repin-raced"), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)
    real_touch = routes._touch_spec

    async def _touch_then_start(*args, **kwargs):
        result = await real_touch(*args, **kwargs)
        if kwargs.get("exec_arming_at") == 0.0 and kwargs.get("status") is None:
            slot.running = True
        return result

    monkeypatch.setattr(routes, "_touch_spec", _touch_then_start)
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    released: list[str] = []

    async def _remove(_slot_key, *, only_loop_id=None, stop_reason=""):
        released.append(only_loop_id)

    monkeypatch.setattr(routes, "_remove_nudge_loop_for_slot", _remove)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/s/handoff", json={"spec_dir": spec_dir})
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_agent_busy", payload
    assert dispatched == []
    assert released == ["loop-repin-raced"], "the armed timer survived the busy refusal"
    assert routes._load_index()["s"].get("status") == "planning"


def test_the_busy_refusal_cannot_leak_an_armed_loop():
    """Source guard: the busy check must precede the arm.

    Arming after the check removes the leak outright, where releasing the timer inside
    the refusal only patches it. Ordering is the property worth pinning -- an edit that
    moves arming above the check reintroduces a hazard that only shows up as a build
    firing minutes after a 409.
    """
    src = inspect.getsource(routes._handle_handoff)
    lock = src.index("async with _turn_lock(handoff_dir_key):")
    busy = src.index("_busy_alias(", lock)
    arm = src.index("await authorize_and_add_nudge(", lock)
    assert busy < arm, (
        "the handoff arms its nudge loop before checking for a busy alias, so a "
        "refusal leaves a timer that dispatches the build it just denied"
    )
    assert lock < arm, (
        "the loop is armed outside the directory turn lock, so its idle timer can "
        "dispatch while this handler is still waiting for the lock"
    )


# ── owner gate on execute / handoff / stop ───────────────────────────────────


_GATE_OWNER = "owner-subject"


_GATE_NON_OWNER = "allowlisted-messaging-user"


class _ConfiguredOwnerState:
    """A state with a configured owner, so a different subject is a non-owner."""

    owner_id = _GATE_OWNER

    def get_or_create_slot(self, name, app=""):
        raise AssertionError("an owner-gated route acquired a worker slot")

    def get_slot(self, key):
        return None


def _claims_mw(user: str, app_claim: str):
    @web.middleware
    async def _mw(request, handler):
        request["user"] = user
        request["app"] = app_claim
        return await handler(request)

    return _mw


def _gated_client(monkeypatch, tmp_path, user: str, app_claim: str) -> TestClient:
    _redirect_state(monkeypatch, tmp_path)
    app = web.Application(middlewares=[_claims_mw(user, app_claim)])
    routes.register_routes(app)
    app["state"] = _ConfiguredOwnerState()
    return TestClient(TestServer(app))


def _gated_spec(tmp_path) -> Path:
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "s"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index(
        {
            "s": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": "spec-builder-s",
            }
        }
    )
    return spec_dir


def _record_side_effects(monkeypatch) -> list[str]:
    effects: list[str] = []

    def _sync(label):
        def _spy(*_a, **_k):
            effects.append(label)
            return (False, "")

        return _spy

    def _async(label):
        async def _spy(*_a, **_k):
            effects.append(label)
            return None, "recorded by test", 403

        return _spy

    monkeypatch.setattr(routes, "_prepare_handoff", _sync("prepare_handoff"))
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())
    monkeypatch.setattr(routes, "authorize_and_add_nudge", _async("arm"))
    monkeypatch.setattr(routes, "_dispatch_turn", _sync("dispatch"))
    monkeypatch.setattr(routes, "_halt_execution", _async("halt"))
    monkeypatch.setattr(routes, "_remove_nudge_loop_for_slot", _async("remove_loop"))
    monkeypatch.setattr(routes, "_halt_active_turn", _async("halt_turn"))
    return effects


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["execute", "handoff"])
async def test_non_owner_dashboard_subject_cannot_execute_or_handoff(verb, tmp_path, monkeypatch):
    """An allow-listed non-owner holds a dashboard credential with an empty app
    claim. Arming the run's loop makes its message the owner session's next turn,
    so it gets the owner gate's 403 before anything -- including the STOP-sentinel
    clear in _prepare_handoff -- runs."""
    client = _gated_client(monkeypatch, tmp_path, _GATE_NON_OWNER, "")
    spec_dir = _gated_spec(tmp_path)
    effects = _record_side_effects(monkeypatch)

    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/{verb}",
            json={"spec_dir": str(spec_dir), "slot_key": "spec-builder-s"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert (resp.status, body.get("code")) == (403, "owner_only"), (resp.status, body)
    assert effects == [], f"a non-owner {verb} reached {effects}"


@pytest.mark.asyncio
async def test_non_owner_dashboard_subject_cannot_stop_spec_execution(tmp_path, monkeypatch):
    """Stop removes the owner session's loop, the partner of arming it, so a
    non-owner is refused before any halt, loop removal or STOP write."""
    client = _gated_client(monkeypatch, tmp_path, _GATE_NON_OWNER, "")
    spec_dir = _gated_spec(tmp_path)
    effects = _record_side_effects(monkeypatch)

    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/stop",
            json={"spec_dir": str(spec_dir), "slot_key": "spec-builder-s"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert (resp.status, body.get("code")) == (403, "owner_only"), (resp.status, body)
    assert effects == [], f"a non-owner stop reached {effects}"


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["execute", "handoff", "stop"])
async def test_spec_app_token_cannot_execute_handoff_or_stop(verb, tmp_path, monkeypatch):
    """The same refusal POST /api/autonudge gives an app token: even this app's
    own token carries the owner's subject but is not the owner's dashboard."""
    client = _gated_client(monkeypatch, tmp_path, _GATE_OWNER, "spec-builder")
    spec_dir = _gated_spec(tmp_path)
    effects = _record_side_effects(monkeypatch)

    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/{verb}",
            json={"spec_dir": str(spec_dir), "slot_key": "spec-builder-s"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert (resp.status, body.get("code")) == (403, "owner_only"), (resp.status, body)
    assert effects == [], f"an app token {verb} reached {effects}"


@pytest.mark.asyncio
async def test_non_owner_keeps_the_ungated_spec_routes(tmp_path, monkeypatch):
    """Only execute, handoff and stop take the owner gate; listing specs keeps
    its current callers."""
    client = _gated_client(monkeypatch, tmp_path, _GATE_NON_OWNER, "")
    _gated_spec(tmp_path)

    await client.start_server()
    try:
        resp = await client.get(f"{_BASE}/specs")
    finally:
        await client.close()

    assert resp.status == 200, resp.status
