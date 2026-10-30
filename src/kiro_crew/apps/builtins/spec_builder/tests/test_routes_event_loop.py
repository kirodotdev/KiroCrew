"""Route tests: blocking work stays off the event loop.

Handlers hand filesystem, index, settings, recents, sandbox and path-validation
work to a thread, and no async function touches the filesystem inline; route
registration touches no filesystem at all.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes, routes_source

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _never_touch_the_real_state,
    _redirect_state,
)

# ── browse scan offloading and symlink skipping ──────────────────────────────


def test_browse_handler_does_not_scan_on_the_event_loop():
    """Source guard: the handler must delegate to the thread offload, not call
    os.scandir inline (which stalled chat streaming on large directories)."""

    src = inspect.getsource(routes._handle_browse)
    assert "asyncio.to_thread(_scan_subdirs" in src
    assert "os.scandir" not in src


# ── handlers offload filesystem work; delete tears down the slot ─────────────


# (1) polled handlers must not do filesystem work on the event loop


def test_detail_handler_offloads_all_filesystem_work():
    """Source guard. The detail endpoint is polled every 2.5s while a build runs
    and it stat-ed three files, read up to three 1 MiB documents and read
    .spec-state.json — all inline, freezing the gateway loop (chat streaming and
    heartbeats included) for the duration of every poll."""

    src = inspect.getsource(routes._handle_get)
    assert "asyncio.to_thread(" in src and "_collect_spec_documents" in src
    # No inline reader calls left in the handler body.
    for inline in ("_read_spec_files(", "_derive_phase(", "_read_spec_text("):
        assert inline not in src, f"{inline} still called inline in the detail handler"


def test_list_handler_offloads_folder_discovery():
    """The list endpoint walks every known project root's .kiro/specs; that walk
    must not run on the loop either. Both the walk AND the index read/write now
    ride one hop, so the write cannot clobber a concurrent create."""

    src = inspect.getsource(routes._handle_list)
    assert "asyncio.to_thread(_load_index_with_discovery)" in src
    for inline in ("_load_index(", "_save_index(", "_discover_folder_specs("):
        assert inline not in src, f"{inline} still called inline in the list handler"


# ── blocking sentinel write on the event loop ────────────────────────────────


def test_halt_execution_writes_the_sentinel_off_the_loop():
    """The reported stall: _write_stop_sentinel is realpath + is_sensitive_path +
    open + write + close + replace. Inline in an async handler, a spec dir on
    unresponsive network storage froze the whole gateway on a Stop click.

    Also pins that the write goes through the IDENTITY-PINNED wrapper: the plain
    primitive would land the STOP wherever the path now points, and the caller's
    own check cannot cover the thread hop in between."""

    src = inspect.getsource(routes._halt_execution)
    # Whitespace-insensitive: the call spans lines, and a literal anchor that a
    # reformat can break is a guard that silently stops guarding.
    assert re.search(
        r"asyncio\.to_thread\(\s*_write_stop_sentinel_for_spec", src
    ), "the STOP write no longer rides the identity-pinned wrapper off-loop"
    assert "\n    _write_stop_sentinel(" not in src, "sentinel still written on the event loop"


def test_handoff_does_no_filesystem_work_on_the_loop():
    """Sibling of the same class: the handoff endpoint stat-ed tasks.md, unlinked
    a stale sentinel and resolved a realpath inline. All three ride one hop."""

    src = inspect.getsource(routes._handle_handoff)
    # Whitespace-insensitive: the call spans lines, and a literal anchor that
    # a reformat can break is a guard that silently stops guarding.
    assert re.search(
        r"asyncio\.to_thread\(\s*_prepare_handoff", src
    ), "handoff no longer hands _prepare_handoff to a worker thread"
    for inline in ("_clear_stop_sentinel(", "os.path.realpath(", '/ "tasks.md"'):
        assert inline not in src, f"{inline} still runs on the event loop in handoff"


# ── index transactions serialize; phases derive off the loop ─────────────────


def test_list_handler_derives_phases_off_the_loop(tmp_path, monkeypatch):
    """The reported stall: the response loop called _derive_phase per spec, and
    each call stats up to three files — so a large index froze the gateway on
    every 15s poll. Phases now come back from the same thread hop."""

    # _save_index below writes to module-level STATE_DIR: without this redirect
    # the test scribbles a pytest tmp path into the USER's real index.
    _redirect_state(monkeypatch, tmp_path)
    src = inspect.getsource(routes._handle_list)
    assert "_derive_phase(" not in src, "_derive_phase still runs on the event loop"
    assert "phases.get(name" in src

    # Non-vacuous: the bundled loader really reports each spec's phase.
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "s1"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# r")
    (spec_dir / "design.md").write_text("# d")
    routes._save_index({"s1": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})
    _index, phases = routes._load_index_with_discovery()
    assert phases["s1"] == "design"


# ── path validation off the loop; the slot-scoping chokepoint ────────────────


def test_no_handler_validates_paths_on_the_event_loop():
    """The reported stall: _safe_dir expands, realpaths and stats a
    caller-supplied path (plus its nearest existing ancestor), so an unresponsive
    mount froze the gateway before the browse scan ever reached its own thread.
    Every handler-side path validation now rides a thread hop."""

    for handler in (routes._handle_browse, routes._handle_put_settings, routes._handle_create):
        src = inspect.getsource(handler)
        for line in src.splitlines():
            stripped = line.strip()
            if "_safe_dir" not in stripped or stripped.startswith("#"):
                continue
            assert "to_thread" in stripped, f"{handler.__name__}: {stripped}"
        # create's remaining filesystem work rides one bundled hop.
        if handler is routes._handle_create:
            assert "asyncio.to_thread(\n        _prepare_spec_dir" in src or (
                "to_thread(" in src and "_prepare_spec_dir" in src
            )
            for inline in ("_resolve_spec_dir(", "_contained(", "spec_dir.mkdir("):
                assert inline not in src, f"{inline} still runs on the event loop in create"


# ── index reads stay off the event loop ──────────────────────────────────────


def test_no_handler_reads_the_index_on_the_event_loop():
    """The reported stall, and the last member of this class: _load_index is a
    file read plus a JSON parse, and the detail endpoint is polled every 2.5s
    during a build — so on a stalled data home the gateway froze repeatedly.
    Handlers read through _aload_index; the sync primitive stays for the
    transaction helpers, which already run on a worker thread."""

    module_src = routes_source()
    tree = ast.parse(module_src)
    offenders: list[str] = []
    # Only the off-loop helpers may call the sync reader. Iterate TOP-LEVEL
    # definitions and skip an allowed one's whole subtree, so the nested
    # thread-body closures inside them are not counted as offenders.
    # _prepare_handoff joins the off-loop helpers: it re-checks the spec's
    # identity under the index lock before clearing the STOP sentinel, and it
    # only ever runs via asyncio.to_thread. _write_stop_sentinel_for_spec is its
    # counterpart for the opposite act (creating a STOP rather than removing one)
    # and earns the allowance the same way. The BLOCKING assertion below ties
    # both allowances to that contract, so the set cannot be widened to admit a
    # function that actually runs on the loop.
    allowed = {
        "_aload_index",
        "_aload_index_with_slot_identity",
        "_aload_index_with_decision_alias_status",
        "_mutate_index",
        "_load_index_with_discovery",
        "_recover_abandoned_reservations",
        "_load_index",
        "_prepare_handoff",
        "_write_stop_sentinel_for_spec",
        "_claim_decision_locked",
        "_forget_decisions_locked",
        "_alias_slots_locked",
    }
    for off_loop in (
        "_aload_index_with_slot_identity",
        "_aload_index_with_decision_alias_status",
        "_load_index_with_discovery",
        "_recover_abandoned_reservations",
        "_prepare_handoff",
        "_write_stop_sentinel_for_spec",
        "_claim_decision_locked",
        "_forget_decisions_locked",
        "_alias_slots_locked",
    ):
        doc = inspect.getdoc(getattr(routes, off_loop)) or ""
        assert "BLOCKING" in doc, (
            f"{off_loop} is allowed to read the index synchronously only because it "
            "runs on a worker thread; its docstring no longer says so"
        )
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in allowed:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_load_index"
            ):
                offenders.append(node.name)
    assert not offenders, f"synchronous _load_index called in: {sorted(set(offenders))}"

    for handler in (
        routes._handle_get,
        routes._handle_messages,
        routes._handle_message,
        routes._handle_handoff,
        routes._handle_stop_execution,
        routes._handle_delete,
        routes._handle_create,
    ):
        source = inspect.getsource(handler)
        assert (
            "await _aload_index()" in source or "await _aload_index_with_slot_identity(" in source
        ), f"{handler.__name__} does not read the index off the loop"


@pytest.mark.asyncio
async def test_aload_index_returns_the_persisted_index(tmp_path, monkeypatch):
    """Non-vacuous: the offloaded reader must return what was written."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"a": {"spec_dir": "/a/a", "status": "planning"}})
    assert await routes._aload_index() == {"a": {"spec_dir": "/a/a", "status": "planning"}}
    assert await routes._aload_index() == routes._load_index()


# ── recents and settings IO off the loop; missing git degrades ───────────────


def test_recents_and_settings_io_stay_off_the_event_loop():
    """The reported stall: the browse handler read (and JSON-parsed) the
    dashboard's recent-projects file plus an is_dir() per candidate inline, and
    the settings handlers read/wrote their file inline. On stalled home storage
    the picker's very first request froze the whole gateway."""

    browse = inspect.getsource(routes._handle_browse)
    assert "asyncio.to_thread(_read_recent_projects)" in browse
    assert "read_text()" not in browse, "recents still read on the event loop"

    for handler, fn in (
        (routes._handle_get_settings, "_load_settings"),
        (routes._handle_put_settings, "_save_settings"),
    ):
        src = inspect.getsource(handler)
        assert f"asyncio.to_thread({fn}" in src, f"{handler.__name__}: {fn} runs on the loop"


def test_read_recent_projects_filters_to_existing_dirs(tmp_path, monkeypatch):
    """Non-vacuous: the extracted helper keeps the filtering behaviour."""
    home = tmp_path / "cfg"
    home.mkdir()
    real = tmp_path / "real-project"
    real.mkdir()
    (home / "recent_projects.json").write_text(json.dumps([str(real), str(tmp_path / "gone"), 42]))
    monkeypatch.setattr(routes, "config_dir", lambda: home)

    assert routes._read_recent_projects() == [str(real)]

    (home / "recent_projects.json").write_text("not json")
    assert routes._read_recent_projects() == []


# ── repo info and handoff validate through the chokepoint ────────────────────


def test_repo_info_validates_through_the_chokepoint_off_loop():
    """The reported stall: a hand-rolled is_absolute()/is_dir() pair stat-ed a
    caller-supplied path on the event loop (an unavailable network path froze the
    gateway) AND skipped the sensitive-path denial _safe_dir applies."""

    src = inspect.getsource(routes._handle_repo_info)
    assert "asyncio.to_thread(_safe_dir" in src
    code = [ln for ln in src.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for ln in code:
        assert "is_dir()" not in ln, f"repo-info still stats on the event loop: {ln.strip()}"


# ── persisted transcripts are read off the loop and redacted ─────────────────


@pytest.mark.asyncio
async def test_persisted_transcript_is_read_off_the_event_loop():
    """The reported stall: the messages endpoint fell back to the PERSISTED
    transcript (a whole JSONL file) and read it inline. That fallback is the case
    that matters — a rehydrated session with no in-memory messages, i.e. right
    after a gateway restart, which is exactly when the user reopens the spec."""

    src = inspect.getsource(routes._serialize_messages)
    assert src.lstrip().startswith("async def"), "_serialize_messages is still sync"
    assert "asyncio.to_thread(" in src and "read_messages" in src
    for ln in src.splitlines():
        stripped = ln.strip()
        if stripped.startswith("#") or "logger" in stripped:
            continue
        if "conversation_log.read_messages" not in stripped:
            continue
        assert stripped.startswith(
            "state.conversation_log.read_messages,"
        ), f"read_messages still called on the loop: {stripped}"
    # And the caller must await it, not schedule a coroutine into the payload.
    assert "await _serialize_messages(" in inspect.getsource(routes._handle_messages)


# ── no async function touches the filesystem inline ──────────────────────────


def test_no_async_function_touches_the_filesystem_inline():
    """The reported stall (`wt_path.exists()` in _create_worktree) was the LAST
    filesystem call in this module still running on the event loop. This guard
    closes the class rather than the line: an async def may not call a stat/read/
    write directly — every one must sit in a helper marked BLOCKING and be
    invoked through asyncio.to_thread."""

    tree = ast.parse(routes_source())
    fs_attrs = {
        "exists",
        "is_dir",
        "is_file",
        "is_symlink",
        "read_text",
        "write_text",
        "unlink",
        "mkdir",
        "iterdir",
        "stat",
    }
    offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            # A bare `x.exists()` call. `to_thread(x.exists)` is an attribute
            # REFERENCE, not a call, so it is correctly not flagged.
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr in fs_attrs
            ):
                offenders.append(f"{node.name}: .{inner.func.attr}()")
    assert not offenders, f"filesystem calls on the event loop: {offenders}"


# ── sandbox setup and loop removal stay off the event loop ───────────────────


def test_sandbox_setup_is_offloaded():
    """The reported stall: sandboxed_spawn_argv looks like a string operation but
    probes the sandbox backend (which can shell out on first use on a host) and
    writes the scrubbed-env temp file, so building the argv froze the loop on the
    first browse."""
    body = inspect.getsource(routes._git).split('"""')[-1]  # drop docstring prose
    assert "asyncio.to_thread(" in body and "_prepare_git_spawn" in body
    for inline in ("sandboxed_spawn_argv(",):
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert inline not in stripped, f"spawn setup still runs on the event loop: {stripped}"
    prep = inspect.getsource(routes._prepare_git_spawn)
    assert "sandboxed_spawn_argv(" in prep
    # Limits are applied AFTER exec by the shim (see test_spawn_preexec_guard), so no
    # preexec callable is built here at all.
    assert "preexec" not in prep


@pytest.mark.asyncio
async def test_nudge_loop_removal_keeps_the_fsync_off_the_loop(tmp_path, monkeypatch):
    """The reported stall: AutoNudgeService.remove() called remove_sync(), whose
    _save() fsyncs -- so a Pause click or a spec delete blocked chat and heartbeats
    on disk. The service already documents the fix (remove_sync(persist=False) plus
    a snapshot-and-offload write, as update() does); remove() just did not use it."""
    from kiro_crew import autonudge as an

    # Assert the PROPERTY, not one implementation of it. This app needs
    # remove() to keep the fsync off the event loop (a Pause click or a spec
    # delete must not block chat on a wedged disk). Follow the complete call
    # path through the transaction-owned persistence helper.
    remove_src = inspect.getsource(an.AutoNudgeService.remove) + inspect.getsource(
        an.AutoNudgeService._remove_unserialized
    )
    persist_src = inspect.getsource(an.AutoNudgeService._start_persistence)
    assert "persist=False" in remove_src, "remove() still fsyncs inline"
    assert "_start_persistence" in remove_src, "remove() bypasses shared persistence"
    assert "run_in_executor" in persist_src, "removal persistence is not offloaded"
    assert "CancelledError" in remove_src, "remove() does not drain the write on cancel"
    # The drain must not be time-bounded: giving up after N seconds releases the
    # lock while the worker is still fsyncing, so a later mutation can write
    # first and the abandoned older payload lands last.
    assert "wait_for" not in remove_src, "the drain is time-bounded and can still be overtaken"

    # Behavioural: the loop really is gone and the state file really was written.
    svc = an.AutoNudgeService(base_dir=tmp_path)
    loop = await svc.add(slot_key="dashboard:x", message="go", idle_secs=60, max_cycles=1)
    assert svc.get_by_slot("dashboard:x") is not None

    writes: list[str] = []
    real_write = svc._write_state

    def _spy_write(payload):
        writes.append("w")
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", _spy_write)

    await svc.remove(loop.id)
    assert svc.get_by_slot("dashboard:x") is None, "loop was not removed"
    assert writes, "removal was not persisted"

    # Removing an unknown id must not write at all.
    writes.clear()
    await svc.remove("does-not-exist")
    assert writes == []


# ── transcript ownership; registration touches no filesystem ─────────────────


def test_route_registration_touches_no_filesystem():
    """The reported startup stall: register_routes ran STATE_DIR.mkdir on the event
    loop during start_dashboard, so a KIROCREW_HOME on stalled network storage
    froze gateway startup -- on a directory the app may never need. Registration
    must create nothing; the off-loop writers mkdir themselves."""
    src = inspect.getsource(routes.register_routes)
    body = src.split('"""')[-1]
    for inline in ("mkdir(", "_state_dir()"):
        assert inline not in body, f"registration touches the filesystem: {inline}"
    # ...and the writers still do create it.
    for writer in (routes._save_index, routes._save_settings):
        assert "mkdir(" in inspect.getsource(writer), f"{writer.__name__} lost its mkdir"


def test_registration_works_with_an_uncreatable_state_dir(tmp_path, monkeypatch):
    """Non-vacuous: registration must succeed even when STATE_DIR cannot be made,
    which is what proves it does not touch it."""
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "nope" / "deeper")

    def _explode(*_a, **_k):
        raise OSError("stalled storage")

    monkeypatch.setattr(Path, "mkdir", _explode)
    app = web.Application()
    routes.register_routes(app)  # must not raise
    assert any("/api/apps/spec-builder" in str(r.resource) for r in app.router.routes())
