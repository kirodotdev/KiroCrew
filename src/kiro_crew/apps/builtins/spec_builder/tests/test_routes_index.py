"""Route tests: the spec index and the app's state files.

Mutations re-read the index under one lock instead of writing a caller's
snapshot; reservations left by a dead process are released; state files are
written atomically; persisted shapes, identity fields and names are validated on
load; every index write admits a name by the predicate the loader uses; and the
test harness redirects, and guards, every state path the app writes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
import time
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_builder.tests import routes_helpers
from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import (
    backend_namespace,
    routes,
    routes_source,
)

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _live_state_snapshot,
    _make_client,
    _never_touch_the_real_state,
    _redirect_state,
)

# ── stale-index writes (concurrent delete resurrects a spec) ─────────────────


@pytest.mark.asyncio
async def test_mutate_index_reads_a_fresh_index_not_the_callers_snapshot(tmp_path, monkeypatch):
    """The reported defect, at its root: a handler loads the index, awaits, then
    writes its stale snapshot back — restoring an entry a concurrent DELETE
    removed and dropping one a concurrent CREATE added, because the WHOLE file is
    overwritten. _mutate_index re-reads inside the same hop as the write."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"kept": {"spec_dir": "/a/kept", "status": "planning"}})

    # Stand in for the concurrent request that lands during the await: it edits
    # the file directly, exactly as another handler's own mutation would.
    routes._save_index(
        {
            "kept": {"spec_dir": "/a/kept", "status": "planning"},
            "added-meanwhile": {"spec_dir": "/a/added", "status": "planning"},
        }
    )

    assert await routes._mutate_index(lambda idx: idx.pop("kept", None) is not None) is True
    after = routes._load_index()
    assert "kept" not in after, "mutation did not apply"
    assert "added-meanwhile" in after, "the concurrent entry was clobbered by a stale write"


def test_load_index_releases_a_reservation_left_by_a_dead_process(tmp_path, monkeypatch):
    """R79: a delete reservation that outlived its process must not reserve a name
    forever.

    ``_mark_deleting`` writes the marker before the teardown and clears it after,
    so a crash in that window persists it with no request left to release it. The
    entry then stays hidden from the list AND its name stays reserved against a
    re-create -- permanently. Loading must release a reservation this process does
    not own."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index(
        {
            "orphaned": {
                "spec_dir": "/a/orphaned",
                "status": "planning",
                routes._DELETING: {"owner": "1234:deadbeef", "at": 1.0},
            }
        }
    )

    loaded = routes._load_index()

    assert "orphaned" in loaded, "the entry must survive -- the delete never completed"
    assert (
        routes._DELETING not in loaded["orphaned"]
    ), "a reservation from a process that is gone still hides the spec and reserves its name"


def test_load_index_releases_a_legacy_bare_timestamp_reservation(tmp_path, monkeypatch):
    """An index written by an older build stores the marker as a bare float, which
    carries no owner. It cannot have come from THIS process, so it is foreign and
    must be released -- otherwise upgrading strands every in-flight delete that was
    interrupted before the upgrade."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index(
        {"legacy": {"spec_dir": "/a/legacy", "status": "planning", routes._DELETING: 1699999999.0}}
    )

    loaded = routes._load_index()

    assert routes._DELETING not in loaded["legacy"], "a pre-upgrade reservation was not released"


@pytest.mark.asyncio
async def test_load_index_keeps_a_reservation_this_process_still_owns(tmp_path, monkeypatch):
    """The ordinary case, and the one a blanket "clear all reservations" would
    break: a delete in flight reads the index repeatedly (``_mutate_index`` loads
    on every hop), so clearing indiscriminately would cancel the reservation
    underneath the very request holding it and re-open the same-name window it
    exists to close."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"live": {"spec_dir": "/a/live", "slot_key": "", "status": "planning"}})

    assert (
        await routes._mark_deleting("live", expect_spec_dir="/a/live", expect_slot_key="") is True
    )

    loaded = routes._load_index()
    assert (
        routes._DELETING in loaded["live"]
    ), "this process's own in-flight reservation was released by its own read"


@pytest.mark.asyncio
async def test_released_reservation_is_persisted_by_the_next_mutation(tmp_path, monkeypatch):
    """The release needs no write of its own: ``_load_index`` is the read half of
    ``_mutate_index``, so the next mutation writes the cleaned entry back. Pins
    that the cleanup reaches disk rather than being re-derived on every read."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index(
        {
            "orphaned": {
                "spec_dir": "/a/orphaned",
                "status": "planning",
                routes._DELETING: {"owner": "1234:deadbeef", "at": 1.0},
            }
        }
    )

    assert await routes._touch_spec("orphaned", status="planning") is not None

    on_disk = json.loads((tmp_path / "spec-builder" / "index.json").read_text())
    assert (
        routes._DELETING not in on_disk["orphaned"]
    ), "the stale reservation is still on disk after a mutation"


@pytest.mark.asyncio
async def test_touch_spec_refuses_to_resurrect_a_deleted_spec(tmp_path, monkeypatch):
    """A stamp on a spec that is gone must FAIL, not recreate the entry — the
    caller has to abort rather than dispatch a worker for a deleted spec."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({})

    assert await routes._touch_spec("ghost", status="executing") is None
    assert routes._load_index() == {}, "a deleted spec was resurrected by a status stamp"


@pytest.mark.asyncio
async def test_touch_spec_returns_the_fresh_entry(tmp_path, monkeypatch):
    """Non-vacuous: on success it commits the fields AND hands back the entry as
    read from disk, so the caller stops reading its pre-await snapshot."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"live": {"spec_dir": "/w/spec", "working_dir": "/w", "status": "planning"}})

    fresh = await routes._touch_spec("live", status="executing")
    assert fresh is not None
    assert fresh["status"] == "executing" and fresh["working_dir"] == "/w"
    assert fresh["updated_at"] > 0
    assert routes._load_index()["live"]["status"] == "executing"


def test_no_handler_writes_the_index_from_a_stale_snapshot():
    """Source guard for the CLASS, not the reported instances. Every handler
    awaits somewhere, so none of them may call _save_index directly: the only
    sanctioned writers are the re-reading mutator, startup recovery, and the
    discovery loader."""

    src = routes_source()
    writers = [
        ln.strip()
        for ln in src.splitlines()
        if "_save_index(" in ln and not ln.strip().startswith(("#", "*", "def _save_index"))
    ]
    # Each sanctioned writer holds _INDEX_LOCK around its read-modify-write.
    assert len(writers) == 3, f"unexpected _save_index call sites: {writers}"
    for handler in (
        routes._handle_create,
        routes._handle_message,
        routes._handle_handoff,
        routes._handle_stop_execution,
        routes._handle_delete,
        routes._handle_list,
    ):
        assert "_save_index(" not in inspect.getsource(
            handler
        ), f"{handler.__name__} writes the index directly; it must go through _mutate_index"


# ── index transactions serialize; phases derive off the loop ─────────────────


@pytest.mark.asyncio
async def test_concurrent_index_transactions_do_not_lose_a_write(tmp_path, monkeypatch):
    """The reported defect: moving the read-modify-write onto a worker thread
    fixed the await window but not thread interleaving — two concurrent creates
    could read the SAME index and the second write would silently drop the first.
    _INDEX_LOCK serializes the whole transaction inside the hop."""

    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({})

    real_load = routes._load_index

    def _slow_load():
        # Widen the read→write window so an unserialized implementation loses a
        # write deterministically rather than by luck.
        # flake-ok: widens the stub's read-to-write window so a lost write shows; no assertion waits on it
        # nosemgrep: kirocrew.test-sleep-as-barrier -- widens the stub's read-to-write window on purpose; no assertion waits on it
        time.sleep(0.05)
        return real_load()

    monkeypatch.setattr(routes, "_load_index", _slow_load)

    def _insert(key):
        def _apply(index):
            index[key] = {"spec_dir": f"/a/{key}", "status": "planning"}
            return True

        return _apply

    await asyncio.gather(*(routes._mutate_index(_insert(f"spec-{i}")) for i in range(6)))
    after = real_load()
    assert sorted(after) == [f"spec-{i}" for i in range(6)], f"a write was lost: {sorted(after)}"


def test_index_transactions_are_serialized_by_a_shared_lock():
    """Source guard: both index writers must hold the same lock. An asyncio.Lock
    would NOT do — the transactions run on worker threads, which an asyncio lock
    does not exclude from each other."""

    assert isinstance(routes._INDEX_LOCK, type(threading.Lock()))
    for fn in (routes._mutate_index, routes._load_index_with_discovery):
        assert "_INDEX_LOCK" in inspect.getsource(fn), f"{fn.__name__} does not take the lock"


# ── atomic state writes and git auditing ─────────────────────────────────────


def test_state_files_are_written_atomically(tmp_path, monkeypatch):
    """The reported defect: a truncating write_text interrupted mid-flight (SIGTERM
    on a gateway restart, a full disk) leaves invalid JSON, and both loaders treat
    a JSONDecodeError as 'empty' — so settings silently reset and EVERY indexed
    spec disappears. atomic_write writes a temp file and renames."""

    for fn in (routes._save_index, routes._save_settings):
        src = inspect.getsource(fn)
        assert "atomic_write(" in src, f"{fn.__name__} does not write atomically"
        assert "write_text(" not in src, f"{fn.__name__} still truncates in place"

    # Non-vacuous: a failing write must leave the previous file intact rather
    # than a truncated one.
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"keep": {"spec_dir": "/a/keep", "status": "planning"}})

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(routes, "atomic_write", _boom)
    with pytest.raises(OSError):
        routes._save_index({"keep": {"spec_dir": "/a/keep", "status": "planning"}, "new": {}})
    assert routes._load_index() == {"keep": {"spec_dir": "/a/keep", "status": "planning"}}


# ── persisted shapes are validated; identity comes from the client ───────────


def test_persisted_shapes_are_validated(tmp_path, monkeypatch):
    """The reported crash: only the TOP-LEVEL shape was guarded. A settings file
    holding a list, or an index entry of null, reached .get() in the handlers and
    500ed the request."""
    _redirect_state(monkeypatch, tmp_path)

    # settings.json of the wrong shape -> treated as "no settings".
    for bad in ("[]", '"nope"', "null", "3"):
        routes._settings_path().parent.mkdir(parents=True, exist_ok=True)
        routes._settings_path().write_text(bad)
        assert routes._load_settings() == {"base_path": "", "model": ""}, bad
        assert routes._load_settings().get("base_path") == ""

    # A malformed ENTRY is dropped; well-formed siblings survive.
    routes._index_path().write_text(
        json.dumps({"good": {"spec_dir": "/s/good"}, "bad": None, "worse": ["x"]})
    )
    idx = routes._load_index()
    assert set(idx) == {"good"}, idx
    for meta in idx.values():
        assert isinstance(meta, dict)

    # A whole file of the wrong shape is still empty, not a crash.
    routes._index_path().write_text("[1, 2]")
    assert routes._load_index() == {}


@pytest.mark.asyncio
async def test_list_survives_a_malformed_index_entry(tmp_path, monkeypatch):
    """Non-vacuous end to end: the endpoint that iterates every entry must serve
    200 rather than 500 when the file holds a bad one."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "good"
    spec_dir.mkdir(parents=True)
    routes._index_path().parent.mkdir(parents=True, exist_ok=True)
    routes._index_path().write_text(
        json.dumps(
            {"good": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}, "bad": None}
        )
    )

    await client.start_server()
    try:
        resp = await client.get(f"{_BASE}/specs")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert [s["name"] for s in body["specs"]] == ["good"]


# ── settings shapes, slot-name grammar, and the browse skip list ─────────────


def test_settings_reader_normalizes_a_non_string_base_path(tmp_path, monkeypatch):
    """The reported crash: _load_settings validated the OUTER shape only, so
    ``{"base_path": []}`` passed as a dict and every reader then called .strip()
    on a list -- 500ing spec creation and the settings endpoint."""
    _redirect_state(monkeypatch, tmp_path)
    routes._settings_path().parent.mkdir(parents=True, exist_ok=True)

    for bad in (
        '{"base_path": []}',
        '{"base_path": 7}',
        '{"base_path": null}',
        '{"base_path": {}}',
    ):
        routes._settings_path().write_text(bad)
        assert routes._load_settings()["base_path"] == "", bad
        # The real crash was downstream: this must not raise.
        routes._resolve_spec_dir(str(tmp_path), "s1")

    # A legitimate string still survives untouched.
    routes._settings_path().write_text('{"base_path": "/tmp/base"}')
    assert routes._load_settings()["base_path"] == "/tmp/base"


# ── failures are reported, never swallowed ───────────────────────────────────


def test_index_entries_missing_identity_fields_are_dropped(tmp_path, monkeypatch):
    """The reported crash: the sanitizer accepted any dict, so ``{"demo": {}}``
    survived and handlers that dereference the required fields directly
    (``meta["spec_dir"]``) raised KeyError and 500ed the request."""
    _redirect_state(monkeypatch, tmp_path)
    routes._index_path().parent.mkdir(parents=True, exist_ok=True)
    good = {"spec_dir": str(tmp_path / "s"), "working_dir": str(tmp_path)}
    routes._index_path().write_text(
        json.dumps(
            {
                "demo": {},  # the reported shape
                "blank": {"spec_dir": "   "},
                "typed": {"spec_dir": []},
                "ok": good,
            }
        )
    )

    index = routes._load_index()

    assert list(index) == ["ok"], index
    # Handlers dereference this directly; the whole point is that it now exists.
    assert index["ok"]["spec_dir"]


# ── the state guard covers every path the app writes ─────────────────────────


def test_redirect_state_covers_every_path_the_app_writes():
    """The reported leak: DELETED_PATH was added to the app in the tombstone fix
    but not to _redirect_state, so the deletion tests rewrote the USER's live
    deleted.json and made their real deleted specs discoverable again.

    Enumerated from the module rather than a hand-kept list, so the next file this
    app learns to write fails here instead of in someone's home directory. A path
    still ``None`` counts too: these are bound lazily, and the autouse guard's own
    redirect is what makes them ``Path`` here, so filtering on ``Path`` alone let a
    path missing from the redirect stay ``None`` and drop out of the check.
    """
    written = {
        n
        for n, v in backend_namespace().items()
        if n.endswith("_PATH") and (v is None or isinstance(v, Path)) and n != "SPEC_STATE_PATH"
    }
    redirected = set(
        re.findall(r'setattr\(routes, "(\w+_PATH)"', inspect.getsource(_redirect_state))
    )
    assert written, "found no state paths in the backend -- the enumeration has gone blind"
    assert written <= redirected, f"not redirected in tests: {sorted(written - redirected)}"


def test_state_guard_watches_the_whole_directory():
    """The guard compares the whole directory listing: asserting one known filename
    lets a write to any other file through."""
    src = inspect.getsource(_never_touch_the_real_state)
    assert "_live_state_snapshot()" in src
    assert "_REAL_STATE_DIR" in inspect.getsource(_live_state_snapshot)


def test_state_guard_compares_the_real_dir_not_the_redirect():
    """The captured dir must survive the autouse redirect active right now.

    _REAL_STATE_DIR is captured on first use rather than at import (which the
    lazy-paths ratchet bans). The property that makes the guard work is that it holds the
    un-redirected dir even while routes._STATE_DIR points at a tmp dir -- the
    guard re-reads it AFTER its yield, with the redirect still applied. If the
    memoization were ever dropped so it re-resolved live, before == after would
    compare tmp-to-tmp and this app's tests could rewrite real user specs
    undetected, which has already happened twice.
    """
    assert routes_helpers._REAL_STATE_DIR is not None, "the first-use capture did not run"
    assert routes_helpers._REAL_STATE_DIR != routes._STATE_DIR, (
        "the live-state guard is pointed at the redirected tmp dir, so it can no "
        "longer detect a test writing to the user's real state"
    )


# ── unusable index keys are dropped; status is allowlisted ───────────────────


def test_index_keys_that_cannot_be_served_are_dropped_at_load(tmp_path, monkeypatch):
    """The reported egress path: a spec NAME is an index key, index.json is
    agent-writable, and `GET /specs` returns the key as `"name"` -- so a credential
    parked in the key reached the dashboard verbatim. Such an entry is dropped at load
    rather than scrubbed: a scrubbed name would not match the directory the
    entry points at."""
    _redirect_state(monkeypatch, tmp_path)
    # A real AWS-key shape satisfies the name grammar, which is why the grammar alone
    # was not enough -- _redact is what recognises it.
    credential = "glpat-IOSFODNN7EXAMPLE"
    assert routes._valid_name(credential), "precondition: the grammar accepts this"
    assert routes._redact(credential) != credential, "precondition: _redact rewrites it"

    routes._save_index(
        {
            "good": {"spec_dir": "/p/good"},
            credential: {"spec_dir": "/p/leak"},
            "../escape": {"spec_dir": "/p/escape"},
            "": {"spec_dir": "/p/empty"},
        }
    )
    loaded = routes._load_index()
    assert set(loaded) == {"good"}, f"an unusable key survived: {sorted(loaded)}"


def test_usable_name_requires_grammar_and_redaction_stability():
    """The predicate: a key must pass the same grammar create enforces AND survive
    _redact unchanged."""
    assert routes._usable_name("my-spec_2") is True
    assert routes._usable_name("glpat-IOSFODNN7EXAMPLE") is False
    assert routes._usable_name("../escape") is False
    assert routes._usable_name("with space") is False
    assert routes._usable_name("") is False


# ── index admission: the write side must use the read side's predicate ──


#: Satisfies _NAME_RE (letters, digits, '-', '_', <=64) but _redact rewrites it,
#: so _load_index would refuse to serve it. Assembled from parts so the literal
#: is not itself a credential-shaped constant in the source.
_CREDENTIAL_SHAPED_NAME = "ghp" + "_" + "0123456789abcdef0123456789abcdef0123"


def test_the_probe_name_is_the_shape_this_class_is_about():
    """Guards the fixture: grammar-valid, but not admissible to the index.

    If _redact stops rewriting this shape the other tests here would pass for the
    wrong reason, so pin both halves explicitly.
    """
    assert routes._valid_name(_CREDENTIAL_SHAPED_NAME), "probe must satisfy the grammar"
    assert not routes._usable_name(_CREDENTIAL_SHAPED_NAME), "probe must fail admission"


@pytest.mark.asyncio
async def test_create_refuses_a_name_the_loader_would_discard(tmp_path, monkeypatch):
    """Accepting on the grammar alone built a spec the next load dropped.

    The handler creates the directory, worktree and session BEFORE anything reads
    the index back, so the orphan outlived the request that made it.
    """
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.post(
            f"{_BASE}/specs",
            json={
                "name": _CREDENTIAL_SHAPED_NAME,
                "working_dir": str(tmp_path),
                "spec_type": "feature",
                "description": "d",
            },
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["code"] == "invalid_name"
        assert not (
            tmp_path / ".kiro" / "specs" / _CREDENTIAL_SHAPED_NAME
        ).exists(), "create left a spec directory for a name the index cannot hold"


def test_discovery_does_not_adopt_a_name_the_loader_would_discard(tmp_path, monkeypatch):
    """Discovery WRITES index[name]; admitting on the grammar alone made it
    re-add an entry the next load drops, rediscovering it on every call.

    Scan roots come from the index's own working_dir values, so one ordinary
    entry is what puts tmp_path in scope -- and it doubles as the control that
    proves discovery still works.
    """
    specs_base = tmp_path / ".kiro" / "specs"
    for spec_name in ("ordinary-spec", _CREDENTIAL_SHAPED_NAME, "second-ordinary"):
        d = specs_base / spec_name
        d.mkdir(parents=True)
        (d / "requirements.md").write_text("r")

    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(routes, "_load_deleted", lambda: set())

    index = {
        "ordinary-spec": {
            "working_dir": str(tmp_path),
            "spec_dir": str(specs_base / "ordinary-spec"),
        }
    }
    routes._discover_folder_specs(index)

    assert "second-ordinary" in index, "discovery stopped adopting ordinary directories"
    assert _CREDENTIAL_SHAPED_NAME not in index, (
        "discovery adopted a name _load_index will drop, so it would be "
        "rediscovered and re-saved on every list poll"
    )


def test_no_index_write_path_admits_on_the_grammar_alone():
    """Class guard. _load_index admits keys with _usable_name; a path that PUTS a
    name into the index (or resolves a slot for one) must use the same predicate,
    or it writes entries the next read discards.

    Asserts on the calls, not on a comment. _owns_slot_key is the one allowed
    _valid_name caller: its name always comes from an already-admitted index
    key, so the predicates agree there.
    """
    src = routes_source()
    allowed = {"_owns_slot_key", "_usable_name"}
    offenders = []
    for match in re.finditer(r"^(?:async )?def (\w+)\(", src, re.M):
        fname = match.group(1)
        start = match.end()
        nxt = re.search(r"^(?:async )?def \w+\(", src[start:], re.M)
        body = src[start : start + (nxt.start() if nxt else len(src) - start)]
        if "_valid_name(" in body and fname not in allowed:
            offenders.append(fname)
    assert not offenders, (
        "these gate on _valid_name, the grammar half only, and will admit names "
        f"_load_index discards: {offenders}"
    )
