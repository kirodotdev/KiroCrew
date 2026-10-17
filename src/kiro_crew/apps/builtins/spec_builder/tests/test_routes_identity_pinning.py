"""Route tests: a request acts on the spec creation it names.

The client sends ``spec_dir`` and ``slot_key``, and every handler that re-reads
the index pins both, so a spec deleted and recreated at the same name or path
mid-request is refused instead of acted on. Covers per-creation slot keys, their
grammar and ownership, and observed slot identities that survive an index rewrite.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import threading
import time
import types
from pathlib import Path

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.spec_builder.backend.orchestration.request_identity import _ClientClaim
from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes, routes_source

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _decision_spec,
    _make_client,
    _never_touch_the_real_state,
    _noop_await,
    _redirect_state,
    _slot_stub,
)
from kiro_crew.testing.wait import async_wait_until, default_timeout, until_parked

# ── spec_dir revalidation and identity pinning ───────────────────────────────


@pytest.mark.asyncio
async def test_detail_refuses_when_the_spec_is_recreated_mid_request(tmp_path, monkeypatch):
    """The detail handler reads the index, awaits the document collection, and must
    not then trust that PRE-AWAIT snapshot. A spec deleted and recreated elsewhere
    under the SAME NAME is a different spec, so
    continuing would pair documents read from the old directory with the new
    metadata and point the new worker at the old project. The request must refuse
    and let the client retry."""
    client = _make_client(monkeypatch, tmp_path)
    old_dir = tmp_path / "old" / ".kiro" / "specs" / "moved"
    new_dir = tmp_path / "new" / ".kiro" / "specs" / "moved"
    new_dir.mkdir(parents=True)
    routes._save_index({"moved": {"spec_dir": str(old_dir), "working_dir": str(tmp_path / "old")}})

    real_collect = routes._collect_spec_documents

    def _collect_then_recreate(spec_dir):
        # Stand in for the concurrent delete+recreate landing during the hop.
        routes._save_index(
            {"moved": {"spec_dir": str(new_dir), "working_dir": str(tmp_path / "new")}}
        )
        return real_collect(spec_dir)

    monkeypatch.setattr(routes, "_collect_spec_documents", _collect_then_recreate)

    scoped: list[str] = []

    class _Slot:
        key = "spec-builder-moved"
        _app = routes.APP_NAME
        project = ""
        _titled = False
        messages: list = []
        running = False

    slot = _Slot()

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            scoped.append(name)
            return slot

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.get(f"{_BASE}/specs/moved")
    finally:
        await client.close()

    assert resp.status == 409, "stale documents were served against fresh metadata"
    assert scoped == [], "a slot was scoped from a different spec's metadata"
    assert slot.project == "", f"worker was pointed at a project: {slot.project}"


@pytest.mark.asyncio
async def test_detail_serves_normally_when_nothing_changes(tmp_path, monkeypatch):
    """Non-vacuous: the identity check must not 409 the ordinary poll."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "steady"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# r")
    routes._save_index({"steady": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    class _Slot:
        key = "spec-builder-steady"
        _app = routes.APP_NAME
        project = ""
        _titled = False
        messages: list = []
        running = False

    slot = _Slot()

    class _State:
        def get_slot(self, key):
            return slot

        def get_or_create_slot(self, name, app=""):
            return slot

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.get(f"{_BASE}/specs/steady")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["phase"] == "requirements"
    assert slot.project == str(tmp_path / "wd")


@pytest.mark.asyncio
async def test_touch_spec_pins_identity_not_just_the_name(tmp_path, monkeypatch):
    """The shared mechanism: a name is not an identity. With expect_spec_dir the
    mutator refuses a same-name entry that now points somewhere else, which is
    what stops handoff dispatching a stale plan and stop/delete acting on the
    wrong spec."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"s": {"spec_dir": "/a/spec", "status": "planning"}})

    assert await routes._touch_spec("s", expect_spec_dir="/b/spec", status="executing") is None
    assert routes._load_index()["s"]["status"] == "planning", "a mismatched spec was mutated"

    ok = await routes._touch_spec("s", expect_spec_dir="/a/spec", status="executing")
    assert ok is not None and routes._load_index()["s"]["status"] == "executing"


def test_identity_pinned_sites_pass_expect_spec_dir():
    """Source guard: the three handlers that act on a captured spec_dir must pin
    identity, not merely existence."""

    for handler in (routes._handle_handoff, routes._handle_stop_execution):
        src = inspect.getsource(handler)
        assert "expect_spec_dir=" in src, f"{handler.__name__} checks existence only"
    delete_src = inspect.getsource(routes._handle_delete)
    assert "doomed_dir" in delete_src, "delete pops by name without checking identity"
    detail_src = inspect.getsource(routes._handle_get)
    assert 'spec_dir", "")) != str(spec_dir)' in detail_src


# ── handoff confirms identity before acquiring the slot ──────────────────────


@pytest.mark.asyncio
async def test_handoff_confirms_identity_before_acquiring_the_slot(tmp_path, monkeypatch):
    """The reported defect: _prepare_handoff awaits, so a delete+recreate can land
    first. The stale request then captured the REPLACEMENT's slot, and its own
    abort path -- correctly pinned to what it captured -- would close the NEW
    session. Checking identity before acquisition means the stale request never
    touches the replacement."""
    client = _make_client(monkeypatch, tmp_path)
    old_dir = tmp_path / "old" / ".kiro" / "specs" / "swap"
    new_dir = tmp_path / "new" / ".kiro" / "specs" / "swap"
    for d in (old_dir, new_dir):
        d.mkdir(parents=True)
        (d / "tasks.md").write_text("- [ ] task")
    routes._save_index({"swap": {"spec_dir": str(old_dir), "working_dir": str(tmp_path / "old")}})

    def _prepare_then_recreate(spec_dir, name="", expect_slot_key=""):
        # Mirrors the real signature: the handler now passes the name and the
        # identity it started with so the CLEAR itself can be refused.
        routes._forget_observed_slot_identity("swap", expect_slot_key)
        routes._save_index(
            {"swap": {"spec_dir": str(new_dir), "working_dir": str(tmp_path / "new")}}
        )
        return True, str(spec_dir / routes._STOP_FILE)

    monkeypatch.setattr(routes, "_prepare_handoff", _prepare_then_recreate)

    touched: list[str] = []
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            touched.append(name)
            raise AssertionError("slot acquired despite the spec being replaced")

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/swap/handoff")
    finally:
        await client.close()

    assert resp.status == 409, "stale handoff proceeded against a replaced spec"
    assert touched == [], "the replacement spec's slot was acquired"
    assert dispatched == []
    # The replacement is left in planning, untouched by the stale request.
    assert routes._load_index()["swap"]["spec_dir"] == str(new_dir)
    assert routes._load_index()["swap"].get("status") != "executing"


def test_handoff_checks_identity_before_slot_acquisition():
    """Source guard on the ORDERING, which is the whole fix."""

    src = inspect.getsource(routes._handle_handoff)
    check = src.index("!= str(spec_dir)")
    acquire = src.index("_ensure_worker_slot(")
    assert check < acquire, "handoff acquires the slot before confirming identity"


# ── name-only operations are identity-pinned ─────────────────────────────────


@pytest.mark.asyncio
async def test_message_refuses_a_recreated_spec(tmp_path, monkeypatch):
    """The reported defect for message: an old browser tab sends into a name that
    has since been deleted and recreated pointing elsewhere. Existence was enough,
    so the turn was dispatched into the REPLACEMENT spec's agent."""
    client = _make_client(monkeypatch, tmp_path)
    old_dir = tmp_path / "old" / ".kiro" / "specs" / "t"
    new_dir = tmp_path / "new" / ".kiro" / "specs" / "t"
    routes._save_index({"t": {"spec_dir": str(old_dir), "working_dir": str(tmp_path / "old")}})

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    real_read = routes._read_json

    async def _read_then_recreate(request):
        body = await real_read(request)
        routes._save_index({"t": {"spec_dir": str(new_dir), "working_dir": str(tmp_path / "new")}})
        return body

    monkeypatch.setattr(routes, "_read_json", _read_then_recreate)

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("slot acquired for a replaced spec")

    client.app["state"] = _State()
    await client.start_server()
    try:
        # The SPA sends the spec_dir it rendered -- that CLIENT-captured identity is
        # what makes the stale tab detectable.
        resp = await client.post(
            f"{_BASE}/specs/t/message", json={"text": "hi", "spec_dir": str(old_dir)}
        )
    finally:
        await client.close()

    assert resp.status == 409, "message dispatched into a replaced spec"
    assert dispatched == []


@pytest.mark.asyncio
async def test_pinned_halt_spares_a_replacement_loop_and_slot(tmp_path, monkeypatch):
    """stop/delete look the loop and slot up BY NAME. With captures pinned, a
    replacement that appears mid-teardown is left alone."""
    removed: list[str] = []

    class _Loop:
        id = "loop-new"

    class _Svc:
        def get_by_slot(self, key):
            return _Loop()

        async def remove(self, loop_id, *, stop_reason=""):
            removed.append(loop_id)

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    class _Slot:
        key = "spec-builder-p"
        _app = routes.APP_NAME
        running = True
        task = None

    replacement = _Slot()
    slots = {"spec-builder-p": replacement}

    class _State:
        _slots = slots
        sessions = None

        def get_slot(self, key):
            return slots.get(key)

    spec_dir = Path(os.path.realpath(tmp_path)) / "spec"
    spec_dir.mkdir()

    # Captured NOTHING (no loop/slot at request time) -> nothing may be touched.
    await routes._halt_execution(
        _State(), "p", spec_dir, reason="test", only_loop_id=None, only_slot=None
    )
    assert removed == [], "a loop was removed with no capture"
    assert slots["spec-builder-p"] is replacement

    # Captured a DIFFERENT loop/slot -> still refused.
    other = _Slot()
    await routes._halt_execution(
        _State(), "p", spec_dir, reason="test", only_loop_id="loop-ours", only_slot=other
    )
    assert removed == [], "the replacement's loop was removed"

    # Matching capture -> acts.
    await routes._halt_execution(
        _State(), "p", spec_dir, reason="test", only_loop_id="loop-new", only_slot=replacement
    )
    assert removed == ["loop-new"]


def test_name_only_operations_are_identity_pinned():
    """Source guard: message stamps with expect_spec_dir, and stop/delete capture
    the loop id and slot object BEFORE they await."""
    msg = inspect.getsource(routes._handle_message)
    assert "expect_spec_dir=" in msg, "message stamps by name only"

    for handler, cap, acts_on in (
        (routes._handle_stop_execution, "captured_loop_id", "await _halt_execution("),
        (routes._handle_delete, "doomed_loop_id", "await _remove_nudge_loop("),
    ):
        src = inspect.getsource(handler)
        assert f"{cap} = _exec_loop_id(" in src, f"{handler.__name__} does not capture the loop"
        assert (
            "only_loop_id=" in src and "only_slot=" in src
        ), f"{handler.__name__} does not pin its cleanups"
        # The capture must precede the teardown it pins (nothing may await in
        # between and let a recreate become the thing we act on).
        assert src.index(cap) < src.index(acts_on), f"{handler.__name__} captures too late"


# ── persisted shapes are validated; identity comes from the client ───────────


@pytest.mark.asyncio
async def test_message_identity_comes_from_the_client(tmp_path, monkeypatch):
    """The reported vacuity in my round-23 fix: the pin read spec_dir out of the
    SAME index it then compared against, so it always matched. The identity has to
    be the one the CLIENT captured."""
    src = inspect.getsource(routes._handle_message)
    assert 'body.get("spec_dir"' in src, "message still derives identity from the index"
    assert "expect_spec_dir=str(index[name]" not in src

    client = _make_client(monkeypatch, tmp_path)
    old_dir = tmp_path / "old" / ".kiro" / "specs" / "m"
    new_dir = tmp_path / "new" / ".kiro" / "specs" / "m"
    routes._save_index({"m": {"spec_dir": str(new_dir), "working_dir": str(tmp_path / "new")}})

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("slot acquired for a stale client")

    client.app["state"] = _State()
    await client.start_server()
    try:
        # A stale tab claims the OLD spec_dir; the index now points elsewhere.
        stale = await client.post(
            f"{_BASE}/specs/m/message", json={"text": "hi", "spec_dir": str(old_dir)}
        )
    finally:
        await client.close()

    assert stale.status == 409, "a stale tab drove the replacement spec"
    assert dispatched == []


# ── discovery validates each root; controls reject a stale identity ──────────


@pytest.mark.asyncio
async def test_controls_reject_a_stale_client_identity(tmp_path, monkeypatch):
    """The reported gap: execute / stop / delete sent only the NAME, so a control
    clicked in a tab whose spec had been deleted and recreated elsewhere drove the
    replacement -- executing the wrong project, or stopping/deleting its session.
    Each now carries the client's rendered spec_dir (query string for DELETE, which
    has no body) and refuses a mismatch before any side effect."""
    client = _make_client(monkeypatch, tmp_path)
    current = tmp_path / "now" / ".kiro" / "specs" / "c"
    current.mkdir(parents=True)
    (current / "tasks.md").write_text("- [ ] t")
    stale = str(tmp_path / "before" / ".kiro" / "specs" / "c")
    routes._save_index({"c": {"spec_dir": str(current), "working_dir": str(tmp_path / "now")}})

    halted: list[str] = []

    async def _spy_halt(*_a, **_k):
        halted.append("halt")

    monkeypatch.setattr(routes, "_halt_execution", _spy_halt)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: halted.append("dispatch"))
    monkeypatch.setattr(routes, "_teardown_worker_slot", lambda *a, **k: _noop())
    monkeypatch.setattr(routes, "_remove_nudge_loop", lambda *a, **k: _noop())

    client.app["state"] = None
    await client.start_server()
    try:
        ex = await client.post(f"{_BASE}/specs/c/execute", json={"spec_dir": stale})
        st = await client.post(f"{_BASE}/specs/c/stop", json={"spec_dir": stale})
        rm = await client.delete(f"{_BASE}/specs/c?spec_dir={stale}")
    finally:
        await client.close()

    assert (ex.status, st.status, rm.status) == (
        409,
        409,
        409,
    ), f"a stale control was honoured: {(ex.status, st.status, rm.status)}"
    assert halted == [], "a side effect ran for a stale client"
    assert "c" in routes._load_index(), "the replacement spec was deleted by a stale tab"


async def _noop(*_a, **_k):
    return None


def test_every_mutating_control_accepts_a_client_identity():
    """Source guard: the three name-only controls must consult the shared helper."""
    for handler in (
        routes._handle_handoff,
        routes._handle_stop_execution,
        routes._handle_delete,
    ):
        src = inspect.getsource(handler)
        assert "_client_claim(request)" in src, f"{handler.__name__} takes no client identity"
        assert "_client_identity_mismatch(" in src


# ── the body is parsed first; sentinel clear is directory-pinned ─────────────


@pytest.mark.asyncio
async def test_stop_reads_the_body_before_the_index(tmp_path, monkeypatch):
    """The reported window: reading the request body is an await, and it sat
    AFTER the index read -- so a delete+recreate landing while a slow body
    arrived left the identity check describing the OLD spec while the loop id and
    slot captured afterwards belonged to the REPLACEMENT."""
    _redirect_state(monkeypatch, tmp_path)
    src = inspect.getsource(routes._handle_stop_execution)
    body_at = src.index("_client_claim(request)")
    index_at = src.index("_aload_index()")
    capture_at = src.index("_exec_loop_id(name)")
    assert body_at < index_at < capture_at, "the body await is not first"
    # And no await may sit between the verified identity and the capture.
    between = src[src.rindex("_client_identity_mismatch(claimed", 0, capture_at) : capture_at]
    assert "await" not in between, "an await reopened the capture window"


def test_every_identity_checked_handler_parses_the_body_first():
    """Source guard for the class: stop, delete and handoff all had the body
    await after the index read whose result the identity check compares against.
    One fix, three call sites. (Handoff also reads the index at its top to find
    the spec at all; what matters is the RE-read that the check is paired with.)"""
    for handler in (
        routes._handle_stop_execution,
        routes._handle_delete,
        routes._handle_handoff,
    ):
        src = inspect.getsource(handler)
        assert "claimed = await _client_claim(request)" in src, handler.__name__
        deciding_read = max(
            src.rfind("_aload_index()"),
            src.rfind("_aload_index_with_slot_identity("),
        )
        assert deciding_read >= 0, f"{handler.__name__} has no indexed identity read"
        assert (
            src.index("claimed = await _client_claim(request)") < deciding_read
        ), f"{handler.__name__} reads the deciding index snapshot before the body"


# ── tombstones, and slot keys that are per-creation ──────────────────────────


def test_slot_key_is_per_creation_not_per_name(tmp_path, monkeypatch):
    """The reported merge: the slot key was derived from the NAME, so a spec
    recreated under a reused name wrote into the previous spec's history file --
    the fresh save preserved the archived rows and a restart rehydrated both
    conversations interleaved."""
    _redirect_state(monkeypatch, tmp_path)
    first, second = routes._new_slot_key("dup"), routes._new_slot_key("dup")
    assert first != second, "two creations of one name share a transcript"
    for key in (first, second):
        assert routes._SLOT_KEY_RE.match(key), key

    # The resolver prefers the persisted key...
    routes._save_index({"dup": {"spec_dir": "/a/dup", "slot_key": second}})
    routes._load_index()
    assert routes._slot_key("dup") == second

    # ...and falls back to the legacy form for entries written before it existed,
    # so specs that already have a transcript keep it.
    routes._save_index({"legacy": {"spec_dir": "/a/legacy"}})
    routes._load_index()
    assert routes._slot_key("legacy") == "spec-builder-legacy"


def test_persisted_slot_key_must_match_the_grammar(tmp_path, monkeypatch):
    """A slot key becomes a session FILENAME and reaches core's session-key
    parsing, so a tampered index must not be able to steer it."""
    _redirect_state(monkeypatch, tmp_path)
    for bad in ("../../etc/passwd", "spec-builder-" + "x" * 200, "other-app-key", "", 7):
        routes._save_index({"s": {"spec_dir": "/a/s", "slot_key": bad}})
        routes._load_index()
        assert routes._slot_key("s") == "spec-builder-s", bad


def test_slot_key_resolution_has_a_single_source():
    """The resolver map is rebuilt in one helper, used by both index chokepoints,
    so no handler threads the key through and none can read a stale one."""
    src = inspect.getsource(routes._refresh_slot_keys)
    # Ownership, not just grammar: the key must encode the entry's own name.
    assert "_owns_slot_key(" in src
    # Whole-dict replacement, because both chokepoints run on worker threads.
    assert "_SLOT_KEYS = {" in src


# ── a minted slot key survives the commit; git needs its audit ───────────────


def test_a_freshly_minted_slot_key_survives_the_commit(tmp_path, monkeypatch):
    """Create mints a unique key, then commits through _mutate_index -- whose internal
    RE-READ rebuilds the resolver map, and that rebuild must follow the WRITE. Rebuilt
    from the pre-insert snapshot it discards the minted key, and everything afterwards
    (seed turn, embedded chat, teardown) falls back to the legacy name-derived key
    while the index holds the unique one, splitting one spec across two slots."""
    _redirect_state(monkeypatch, tmp_path)
    minted = routes._new_slot_key("fresh")

    # The read chokepoint sees an index without our entry (the pre-insert state)...
    routes._save_index({})
    routes._load_index()
    # ...then the commit lands. The resolver must follow the WRITE, not just reads.
    routes._save_index({"fresh": {"spec_dir": str(tmp_path / "s"), "slot_key": minted}})

    assert routes._slot_key("fresh") == minted, "the minted key was discarded at commit"


def test_both_index_chokepoints_refresh_the_resolver():
    """Source guard: read-only refresh is what caused the split, so the writer
    must refresh too."""
    for fn in (routes._load_index_snapshot, routes._save_index):
        assert "_refresh_slot_keys" in inspect.getsource(fn), fn.__name__


def test_detail_reports_the_slot_key_it_scoped(tmp_path, monkeypatch):
    """The SPA must not derive the slot key from the name -- with per-creation keys
    a reused name would mount the embed on the previous spec's transcript. The
    detail payload therefore names the session the app itself scoped."""
    src = inspect.getsource(routes._handle_get)
    assert (
        '"slot_key": getattr(slot, "key", None) or _slot_key(name)' in src
    ), "detail no longer tells the client which slot to mount"


@pytest.mark.asyncio
async def test_teardown_uses_the_captured_slots_own_key(tmp_path, monkeypatch):
    """Recomputing the key from the name tore down a DIFFERENT slot once keys are
    per-creation. The pinned slot's own key wins."""
    _redirect_state(monkeypatch, tmp_path)
    unique = routes._new_slot_key("s")
    slot = types.SimpleNamespace(
        key=unique, _app=routes.APP_NAME, running=False, task=None, messages=[]
    )

    class _State:
        def __init__(self):
            self._slots = {unique: slot}
            self.asked: list[str] = []

        def get_slot(self, key):
            self.asked.append(key)
            return self._slots.get(key)

    state = _State()
    import kiro_crew.dashboard.chat_persistence as cp

    monkeypatch.setattr(cp, "save_slot_off_loop", lambda *a, **k: _noop_await([]))

    assert await routes._teardown_worker_slot(state, "s", only_slot=slot) is True
    assert state.asked == [unique], f"looked up {state.asked} instead of the pinned key"
    assert unique not in state._slots


# ── slot_key is the deciding identity; handoff refuses early ─────────────────


def test_slot_key_is_the_deciding_identity(tmp_path, monkeypatch):
    """The reported gap: a spec_dir does not identify a CREATION. Delete leaves the
    documents on disk by design, so re-importing under the same name and path
    produces a different spec with the same spec_dir -- and a stale tab's Pause
    would cancel the replacement's run. The per-creation slot key distinguishes
    them."""
    same_dir = "/p/.kiro/specs/s"
    old_key, new_key = "spec-builder-s-aaaa1111", "spec-builder-s-bbbb2222"

    # Same directory, different creation -> refused on the key alone.
    assert (
        routes._client_identity_mismatch(routes._ClientClaim(same_dir, old_key), same_dir, new_key)
        is True
    )
    # Same creation -> allowed.
    assert (
        routes._client_identity_mismatch(routes._ClientClaim(same_dir, new_key), same_dir, new_key)
        is False
    )
    # A directory mismatch still refuses on its own.
    assert (
        routes._client_identity_mismatch(
            routes._ClientClaim("/p/other", new_key), same_dir, new_key
        )
        is True
    )
    # Unpinned stays unpinned: an older tab sends neither field.
    assert routes._client_identity_mismatch(routes._ClientClaim("", ""), same_dir, new_key) is False
    # A server-side entry with no key yet cannot refuse on the key.
    assert (
        routes._client_identity_mismatch(routes._ClientClaim(same_dir, old_key), same_dir, "")
        is False
    )


@pytest.mark.asyncio
async def test_client_claim_reads_body_and_query(tmp_path, monkeypatch):
    """Both fields travel in the body for POSTs and the query string for DELETE."""
    client = _make_client(monkeypatch, tmp_path)
    seen: list[_ClientClaim] = []

    async def _probe(request):
        seen.append(await routes._client_claim(request))
        return web.json_response({"ok": True})

    # Routes must be registered BEFORE the app starts: the router freezes on start.
    client.app.router.add_post("/probe", _probe)
    client.app.router.add_delete("/probe", _probe)
    await client.start_server()
    try:
        await client.post("/probe", json={"spec_dir": "/d", "slot_key": "spec-builder-s-1"})
        await client.delete("/probe?spec_dir=/d&slot_key=spec-builder-s-1")
    finally:
        await client.close()

    assert seen == [routes._ClientClaim("/d", "spec-builder-s-1")] * 2, seen


# ── mutations pin the creation; failures revert armed state ──────────────────


@pytest.mark.asyncio
async def test_touch_spec_pins_the_creation_not_just_the_directory(tmp_path, monkeypatch):
    """The reported gap: the re-reading mutators compared spec_dir only. A delete +
    re-import at the same name AND path leaves spec_dir identical, so a stale
    request passed the check and stamped (or dropped) the replacement."""
    _redirect_state(monkeypatch, tmp_path)
    same_dir = str(tmp_path / "p" / ".kiro" / "specs" / "s")
    routes._save_index({"s": {"spec_dir": same_dir, "slot_key": "spec-builder-s-bbbb2222"}})

    # Stale claim: right directory, previous creation -> refused.
    assert (
        await routes._touch_spec(
            "s",
            expect_spec_dir=same_dir,
            expect_slot_key="spec-builder-s-aaaa1111",
            status="executing",
        )
        is None
    )
    assert routes._load_index()["s"].get("status") != "executing", "the stale stamp landed"

    # Current creation -> accepted.
    fresh = await routes._touch_spec(
        "s",
        expect_spec_dir=same_dir,
        expect_slot_key="spec-builder-s-bbbb2222",
        status="executing",
    )
    assert fresh is not None and fresh["status"] == "executing"

    # An entry predating slot keys carries none and cannot be pinned on it.
    routes._save_index({"old": {"spec_dir": same_dir}})
    assert (
        await routes._touch_spec(
            "old",
            expect_spec_dir=same_dir,
            expect_slot_key="spec-builder-old-1",
            status="planning",
        )
        is not None
    )


def test_every_reread_mutation_pins_the_creation():
    """Source guard: each handler that re-reads the index to mutate it must carry
    the slot key it verified, not just the directory."""
    for handler in (
        routes._handle_message,
        routes._handle_handoff,
        routes._handle_stop_execution,
        routes._handle_delete,
    ):
        src = inspect.getsource(handler)
        assert "slot_key" in src, f"{handler.__name__} mutates without pinning the creation"


# ── claims serialize; delete tombstones before it drops the entry ────────────


@pytest.mark.asyncio
async def test_claim_refuses_a_different_creation(tmp_path, monkeypatch):
    """The claim carries identity for the same reason every other mutation does: a
    delete + re-import at the same name AND path is a different spec."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "p" / ".kiro" / "specs" / "s")
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": "spec-builder-s-new"}})
    monkeypatch.setattr(routes, "_exec_loop_active", lambda _n: False)

    reason, _entry = await routes._claim_execution(
        "s", expect_spec_dir=spec_dir, expect_slot_key="spec-builder-s-old", live_running=False
    )
    assert reason == routes._CLAIM_GONE
    assert routes._load_index()["s"].get("status") != "executing"

    # Delete publishes its reservation before tearing down the slot. A handoff
    # queued behind that boundary must not claim the hidden entry while teardown
    # is in flight.
    deleting = routes._load_index()
    deleting["s"][routes._DELETING] = {"owner": routes._PROCESS_ID, "at": time.time()}
    routes._save_index(deleting)
    reason, _entry = await routes._claim_execution(
        "s", expect_spec_dir=spec_dir, expect_slot_key="spec-builder-s-new", live_running=False
    )
    assert reason == routes._CLAIM_GONE

    # A live turn on the slot counts as taken even when the index says planning.
    deleting["s"].pop(routes._DELETING)
    routes._save_index(deleting)
    reason, _entry = await routes._claim_execution(
        "s", expect_spec_dir=spec_dir, expect_slot_key="spec-builder-s-new", live_running=True
    )
    assert reason == routes._CLAIM_TAKEN


# ── slot-key ownership, and timestamps validated on egress ───────────────────


def test_a_spec_cannot_claim_another_specs_slot_key(tmp_path, monkeypatch):
    """The reported aliasing: index.json is agent-writable, and the filter accepted
    any key matching the generic grammar -- so an entry could carry ANOTHER spec's
    valid key, and `_ensure_worker_slot` would then adopt that spec's live session,
    delivering this spec's messages and approval cards into the other conversation.
    A key is only honoured for the entry whose name it encodes."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index(
        {
            "alpha": {"spec_dir": "/p/alpha", "slot_key": "spec-builder-alpha-1234abcd"},
            # Grammar-valid, but it names alpha -- the aliasing attempt.
            "beta": {"spec_dir": "/p/beta", "slot_key": "spec-builder-alpha-1234abcd"},
            # Legacy name-derived key stays honoured for its OWN spec.
            "gamma": {"spec_dir": "/p/gamma", "slot_key": "spec-builder-gamma"},
        }
    )
    routes._load_index()

    assert routes._slot_key("alpha") == "spec-builder-alpha-1234abcd"
    assert routes._slot_key("beta") == "spec-builder-beta", "beta aliased alpha's session"
    assert routes._slot_key("gamma") == "spec-builder-gamma"


def test_slot_key_ownership_rules():
    """The predicate itself: per-creation form, legacy form, and the shapes that a
    hand-edited index could otherwise smuggle past the grammar."""
    assert routes._owns_slot_key("s", "spec-builder-s-0123abcd") is True
    assert routes._owns_slot_key("s", "spec-builder-s") is True
    # Another spec's key, a prefix collision, a non-hex suffix, wrong suffix width.
    assert routes._owns_slot_key("s", "spec-builder-other-0123abcd") is False
    assert routes._owns_slot_key("s", "spec-builder-s2-0123abcd") is False
    assert routes._owns_slot_key("s", "spec-builder-s-ZZZZZZZZ") is False
    assert routes._owns_slot_key("s", "spec-builder-s-0123abc") is False
    assert routes._owns_slot_key("s", "spec-builder-s-0123abcd-extra") is False
    # A name that is not a valid spec name cannot own anything.
    assert routes._owns_slot_key("../evil", "spec-builder-../evil") is False
    # And every key this app MINTS satisfies its own rule.
    minted = routes._new_slot_key("demo")
    assert routes._owns_slot_key("demo", minted) is True, minted


# ── the pre-dispatch re-pin uses the captured entry ──────────────────────────


@pytest.mark.asyncio
async def test_predispatch_repin_uses_captured_key_when_client_sends_none(tmp_path, monkeypatch):
    """slot_key is OPTIONAL on the wire, so a client that sends none must not leave the
    pre-dispatch check with no creation pin. A delete plus a same-path recreate passes a
    spec_dir-only check -- spec_dir still matches -- and the stale slot then writes into
    the REPLACEMENT's files.

    Simulates the window by swapping the index for a same-name, same-path spec with a
    NEW slot_key while _ensure_worker_slot is awaiting, then asserts the turn is
    refused rather than dispatched."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "s").mkdir(parents=True)
    sd = str(base / "s")
    original_key = routes._new_slot_key("s")
    routes._save_index(
        {
            "s": {
                "spec_dir": sd,
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": original_key,
            }
        }
    )

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a))

    real_ensure = routes._ensure_worker_slot

    async def _ensure_then_recreate(state, name, meta, **kw):
        slot = await real_ensure(state, name, meta, **kw)
        # The delete+recreate lands here: same name, same path, NEW creation.
        idx = await routes._aload_index()
        idx["s"]["slot_key"] = routes._new_slot_key("s")
        routes._save_index(idx)
        return slot

    monkeypatch.setattr(routes, "_ensure_worker_slot", _ensure_then_recreate)

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        # NO slot_key in the body -- the older-client shape the pin must not trust.
        resp = await client.post(f"{_BASE}/specs/s/message", json={"text": "edit files"})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, body
    assert body.get("code") == "stale_client", body
    assert dispatched == [], "dispatched into a spec that was recreated mid-request"


def test_predispatch_repin_pins_both_halves_from_the_captured_entry():
    """Source guard: BOTH pins on the final check must come from `fresh` (server-side,
    already verified) and NEITHER from the client body. Reusing the claimed slot_key is
    what reopened the recreate window. Asserts on the ARGUMENTS, not on a comment."""
    src = inspect.getsource(routes._handle_message)
    dispatch = src.index("_dispatch_turn(")
    # The last _touch_spec before the dispatch is the re-pin.
    repin_start = src.rindex("_touch_spec(", 0, dispatch)
    repin = src[repin_start:dispatch]
    assert 'fresh.get("spec_dir")' in repin, "the re-pin stopped pinning the captured spec_dir"
    assert 'fresh.get("slot_key"' in repin, (
        "the re-pin does not pin the CAPTURED slot_key: a client that omits slot_key "
        "would have no creation pin, so a same-path recreate slips through"
    )
    assert "claimed_key" not in repin, (
        "the re-pin reads the client-claimed slot_key again -- that value is optional "
        "on the wire, so it cannot carry the creation identity"
    )


# ── Identity pins must be (spec_dir AND slot_key), never directory-only ──────
# The rule this file already states in _unwind_create: a delete + re-import at
# the same name AND path leaves spec_dir identical, so the directory alone cannot
# distinguish our spec from the replacement's.


@pytest.mark.asyncio
async def test_status_reconcile_pins_on_slot_key_not_just_the_directory(monkeypatch):
    """_effective_status stamps status=planning when it decides an execution has
    finished. Pinned on spec_dir alone, that stamp landed on a REPLACEMENT spec
    created at the same name and path while the caller held a stale snapshot."""
    calls: list = []

    async def _spy_touch(name, **kw):
        calls.append(kw)
        return None

    monkeypatch.setattr(routes, "_touch_spec", _spy_touch)
    monkeypatch.setattr(routes, "_exec_loop_active", lambda name: False)

    meta = {
        "status": "executing",
        "spec_dir": "/w/.kiro/specs/s",
        "slot_key": "spec-builder-s-aaaa1111",
    }
    out = await routes._effective_status("s", meta, types.SimpleNamespace(running=False))
    assert out == "planning"
    assert calls, "the reconcile did not stamp at all"
    kw = calls[0]
    assert kw.get("expect_spec_dir") == "/w/.kiro/specs/s"
    assert kw.get("expect_slot_key") == "spec-builder-s-aaaa1111", (
        "the reconciling stamp is pinned on the directory only -- a same-name, "
        "same-path replacement would be reset to planning"
    )


@pytest.mark.asyncio
async def test_reconcile_stamp_survives_the_arming_window(monkeypatch):
    """The hole the three early guards do NOT close, and the reason the slot_key
    pin is required rather than merely tidy.

    A replacement mid-ARMING has written status=executing but has not armed its
    nudge loop, so `_exec_loop_active` is False and no turn is running. The arming
    grace cannot rescue it either: `exec_arming_at` is read from the CALLER'S
    STALE meta, not from the replacement's fresh entry. All three guards fall
    through and the stamp is attempted -- so the pin is what refuses it.
    """
    calls: list = []

    async def _spy_touch(name, **kw):
        calls.append(kw)
        return None

    monkeypatch.setattr(routes, "_touch_spec", _spy_touch)
    monkeypatch.setattr(routes, "_exec_loop_active", lambda name: False)

    # Stale snapshot: no arming stamp of its own, so no grace period applies.
    stale = {
        "status": "executing",
        "spec_dir": "/w/.kiro/specs/s",
        "slot_key": "spec-builder-s-OLD",
        "exec_arming_at": 0.0,
    }
    out = await routes._effective_status("s", stale, types.SimpleNamespace(running=False))
    assert out == "planning"
    assert calls and calls[0].get("expect_slot_key") == "spec-builder-s-OLD", (
        "the stamp carried no creation pin, so _touch_spec could not tell it "
        "apart from the replacement's entry"
    )


def test_handoff_captures_its_identity_before_the_await_and_pins_on_both():
    """Source guard on the ORDER and the ARGUMENTS.

    The capture must precede the _prepare_handoff await, and the reread must
    compare BOTH spec_dir and the captured slot_key. A slot_key check validates only
    the CLIENT's claim, so on its own it leaves a request carrying no claim with no
    identity check at all.
    """
    src = inspect.getsource(routes._handle_handoff)
    capture = src.index('started_slot_key = str(meta.get("slot_key", ""))')
    await_match = re.search(r"await asyncio\.to_thread\(\s*_prepare_handoff", src)
    assert await_match, "handoff no longer hands _prepare_handoff to a worker thread"
    await_at = await_match.start()
    guard = src.index("spec_changed_during_start")
    assert capture < await_at, "the identity is captured after the await it must survive"
    # The claim check must precede the await too: _prepare_handoff CLEARS the STOP
    # sentinel, so a stale execute that reaches it disarms a replacement's Pause
    # before any comparison has run.
    claim_at = src.index("_client_identity_mismatch(claimed, spec_dir, started_slot_key)")
    assert (
        claim_at < await_at
    ), "the client-claim check moved after the sentinel clear it is meant to gate"
    assert await_at < guard, "the reread guard does not follow the await"
    window = src[await_at:guard]
    assert "!= started_slot_key" in window, (
        "the reread is pinned on spec_dir only -- a same-name, same-path " "replacement passes it"
    )
    assert (
        'str(meta.get("spec_dir", "")) != str(spec_dir)' in window
    ), "the directory pin was dropped"


def test_no_index_mutation_is_pinned_on_the_directory_alone():
    """Class guard. Every _touch_spec call that pins spec_dir must also pin
    slot_key -- the two are one identity, and rounds 62 and 68 were both a
    caller passing only half of it."""
    src = routes_source()
    for match in re.finditer(r"_touch_spec\(", src):
        depth, i = 0, match.end() - 1
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call = src[match.start() : i + 1]
        if "expect_spec_dir" in call:
            assert "expect_slot_key" in call, (
                "a _touch_spec call pins the directory without the creation key, "
                f"so a same-path replacement passes it:\n{call}"
            )


@pytest.mark.asyncio
async def test_index_and_legacy_runtime_identity_are_captured_under_one_lock(tmp_path, monkeypatch):
    """A replacement writer cannot refresh the resolver between the two values."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "wd" / ".kiro" / "specs" / "legacy")
    routes._save_index({"legacy": {"spec_dir": spec_dir}})
    snapshot_read = threading.Event()
    release_snapshot = threading.Event()
    real_load = routes._load_index
    load_count = 0

    def _load_and_pause_once():
        nonlocal load_count
        index = real_load()
        load_count += 1
        if load_count == 1:
            snapshot_read.set()
            assert release_snapshot.wait(
                timeout=default_timeout()
            ), "the test never released the snapshot read"
        return index

    monkeypatch.setattr(routes, "_load_index", _load_and_pause_once)
    capture = asyncio.create_task(routes._aload_index_with_slot_identity("legacy"))
    await async_wait_until(
        snapshot_read.is_set, describe=lambda: "the index snapshot was never read"
    )
    replacement_key = routes._new_slot_key("legacy")

    def _replace(index):
        index["legacy"]["slot_key"] = replacement_key
        return True

    replacement = asyncio.create_task(routes._mutate_index(_replace))
    await asyncio.sleep(0)
    release_snapshot.set()

    index, runtime_key, observed_key = await capture
    assert await replacement
    assert "slot_key" not in index["legacy"]
    assert runtime_key == "spec-builder-legacy"
    assert observed_key == ""
    assert routes._load_index()["legacy"]["slot_key"] == replacement_key


def test_the_detail_read_scopes_the_slot_from_a_fresh_index_read():
    """Source guard: nothing may await between the detail handler's FRESH index read
    and the ``_ensure_worker_slot`` call that consumes its ``meta``.

    A delete-and-re-import in that window hands the replacement's slot the stale
    entry, and the agent's next turn then runs in the OLD project directory. Reading
    this backend's decision ledger separately opened exactly that window; the read
    belongs in the one filesystem hop above instead."""
    src = inspect.getsource(routes._handle_get)
    fresh_call = "await _aload_index_with_decision_alias_status("
    fresh = src.rindex(fresh_call)
    # The CALL, not the comment that names the helper a few lines above it.
    scope = src.index("await _ensure_worker_slot(")
    assert fresh < scope, "the slot is scoped before the index is re-read"
    between = src[fresh + len(fresh_call) : scope]
    awaits = [
        ln.strip()
        for ln in between.splitlines()
        if "await " in ln and not ln.strip().startswith("#")
    ]
    assert (
        not awaits
    ), f"an await sits between the fresh index read and the slot scoping: {awaits!r}"


@pytest.mark.asyncio
async def test_a_same_path_reimport_during_the_read_is_refused(tmp_path, monkeypatch):
    """A delete leaves the documents on disk, so a re-import at the same name AND path
    is a DIFFERENT creation. A spec_dir-only freshness check passed it, pairing the
    replacement's metadata with documents and a decision record read for the spec that
    is gone -- serving the deleted spec's locked answers on the new one."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    real_collect = routes._collect_spec_documents

    def _collect_then_reimport(sd):
        # Same name, same path, NEW creation -- exactly what a delete + re-import
        # leaves behind.
        idx = routes._load_index()
        routes._forget_observed_slot_identity("s", str(idx["s"].get("slot_key", "")))
        idx["s"]["slot_key"] = routes._new_slot_key("s")
        routes._save_index(idx)
        return real_collect(sd)

    monkeypatch.setattr(routes, "_collect_spec_documents", _collect_then_reimport)

    client.app["state"] = _slot_stub()[0]
    await client.start_server()
    try:
        resp = await client.get(f"{_BASE}/specs/s")
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_changed_during_read", payload


@pytest.mark.asyncio
async def test_index_keeps_resolving_an_observed_per_creation_slot_key(tmp_path, monkeypatch):
    """An agent cannot detach a live turn by replacing its authenticated slot key."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    original = "spec-builder-s-1234abcd"
    replacement = "spec-builder-s-deadbeef"
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": original}})
    assert routes._load_index()["s"]["slot_key"] == original

    routes._index_path().write_text(
        json.dumps({"s": {"spec_dir": spec_dir, "slot_key": replacement}})
    )

    changed = routes._load_index()["s"]
    assert changed["slot_key"] == replacement
    assert routes._slot_key("s") == original
    assert routes._OBSERVED_SLOT_KEYS["s"] == original
    assert await routes._pin_legacy_slot_identity("s", changed) is None


@pytest.mark.asyncio
async def test_deleted_creation_releases_its_observed_slot_identity(tmp_path, monkeypatch):
    """A trusted delete permits a later creation to mint a fresh identity."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    original = "spec-builder-s-1234abcd"
    replacement = "spec-builder-s-deadbeef"
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": original}})

    routes._index_path().write_text(
        json.dumps({"s": {"spec_dir": spec_dir, "slot_key": replacement}})
    )
    routes._load_index()
    assert routes._slot_key("s") == original

    def _delete(index):
        del index["s"]
        return True

    assert await routes._mutate_index(
        _delete,
        on_commit=lambda: routes._forget_observed_slot_identity("s", replacement, original),
    )
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": replacement}})

    assert routes._load_index()["s"]["slot_key"] == replacement


@pytest.mark.asyncio
async def test_legacy_message_pins_its_name_derived_slot_before_alias_scan(tmp_path, monkeypatch):
    """A genuine pre-key spec remains usable without weakening missing-key aliases."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "s"
    spec_dir.mkdir(parents=True)
    routes._save_index(
        {
            "s": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
            }
        }
    )
    state, _slot = _slot_stub()
    dispatched: list[str] = []
    monkeypatch.setattr(
        routes,
        "_dispatch_turn",
        lambda *_args, **_kwargs: dispatched.append("sent"),
    )

    client.app["state"] = state
    await client.start_server()
    try:
        response = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": str(spec_dir), "text": "continue"},
        )
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 200, payload
    assert routes._load_index()["s"]["slot_key"] == "spec-builder-s"
    assert dispatched == ["sent"]


@pytest.mark.asyncio
async def test_legacy_slot_read_cannot_erase_observed_per_creation_identity(tmp_path, monkeypatch):
    """A temporary legacy key cannot make later key removal look like migration."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    per_creation = "spec-builder-s-1234abcd"
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": per_creation}})
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": "spec-builder-s"}})
    routes._save_index({"s": {"spec_dir": spec_dir}})

    assert routes._OBSERVED_SLOT_KEYS["s"] == per_creation
    assert routes._slot_key("s") == per_creation
    assert await routes._pin_legacy_slot_identity("s", {"spec_dir": spec_dir}) is None


@pytest.mark.asyncio
async def test_stop_refuses_a_spec_recreated_at_the_same_path(tmp_path, monkeypatch):
    """The directory cannot detect a delete + recreate; the slot key can.

    Rechecking the canonical directory after waiting for the lock is not enough: a
    recreate at the SAME path resolves to the same directory -- so the recheck
    passes and Stop cancels the replacement's run. Slot keys are minted per
    creation, so the replacement necessarily carries a different one.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dir_key = routes._decision_key(spec_dir)

    halted: list = []

    async def _halt_spy(*_a, **_kw):
        halted.append("halt")

    monkeypatch.setattr(routes, "_halt_execution", _halt_spy)

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        held = routes._turn_lock(dir_key)
        async with held:
            stop = asyncio.ensure_future(client.post(f"{_BASE}/specs/s/stop", json={}))
            await until_parked(held, describe=lambda: "Stop never reached the held turn lock")
            # The spec is deleted and recreated at the SAME path while Stop waits:
            # same directory, new creation, new slot key.
            index = routes._load_index()
            routes._forget_observed_slot_identity("s", slot_key)
            index["s"]["slot_key"] = "spec-builder-s-99999999"
            routes._save_index(index)
        resp = await stop
        status, payload = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 409, f"{status}: {payload}"
    assert payload["code"] == "stale_client", payload
    assert halted == [], "Stop cancelled the run of a spec created after it started waiting"
    assert slot_key != "spec-builder-s-99999999"
