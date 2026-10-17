"""Shared harness for the Spec Builder route tests (``test_routes*.py``).

State redirection and the autouse live-state guard, the HTTP client builders,
and the fakes and seeds that tests in two or more parts use. A helper only one
part uses stays in that part. The guard is active in a part only because the
part binds ``_never_touch_the_real_state`` in its own namespace.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes

_BASE = "/api/apps/spec-builder"


_REAL_DECISIONS_PATH: Path | None = None


_DEFAULT_DECISION_STATE = {
    "decisions": [
        {
            "id": "transport",
            "title": "Inbound transport",
            "options": [],
        }
    ]
}


# ── HTTP harness ─────────────────────────────────────────────────────────────


def _redirect_state(monkeypatch, tmp_path):
    """Point the module's state files at a tmp dir and force-enable the app."""
    state_dir = tmp_path / "spec-builder"
    monkeypatch.setattr(routes, "_STATE_DIR", state_dir)
    monkeypatch.setattr(routes, "_SETTINGS_PATH", state_dir / "settings.json")
    monkeypatch.setattr(routes, "_INDEX_PATH", state_dir / "index.json")
    # Every module-level path the app writes to, not just the ones a test happens
    # to read: the tombstone file was added later and missed here, so the
    # deletion tests wrote into the USER's live state and made their real deleted
    # specs discoverable again.
    monkeypatch.setattr(routes, "_DELETED_PATH", state_dir / "deleted.json")
    # The decision ledger lives under the security keystone's trust root, OUTSIDE
    # this dir, so it needs its own redirect -- and the autouse guard below watches
    # the real one, which is what catches a test that forgets.
    monkeypatch.setattr(routes, "_DECISIONS_PATH", state_dir / "decisions.json")
    # Process-owned execution claims are scoped to the same temporary app state.
    # A failed test must not leave a pre-dispatch claim that affects the next one.
    monkeypatch.setattr(routes, "_EXECUTION_CLAIMS", {})
    monkeypatch.setattr(routes, "_EXECUTION_STOPS", {})
    monkeypatch.setattr(routes, "_PENDING_DISPATCH_CLAIMS", {})
    monkeypatch.setattr(routes, "_REVOKED_EXECUTION_CLAIMS", set())
    monkeypatch.setattr(routes, "_REVOKED_PENDING_DISPATCH_CLAIMS", set())
    monkeypatch.setattr(routes, "_STOP_ROLLBACK_TASKS", set())
    monkeypatch.setattr(routes, "_OBSERVED_SLOT_KEYS", {})
    monkeypatch.setattr(routes, "_OBSERVED_SPEC_DIRS", {})
    monkeypatch.setattr(routes, "_INDEXED_SPEC_IDENTITIES", set())
    monkeypatch.setattr(routes, "_INDEXED_SPEC_NAMES", set())
    monkeypatch.setattr(routes, "_INDEXED_SPEC_DIRS", set())
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)
    return state_dir


def _live_state_snapshot() -> dict[str, int]:
    """Names + mtimes of the guarded live state dir, or {} when there is none.

    The guarded dir is captured on FIRST call and memoized: the capture must
    happen before any test's ``_redirect_state`` patches the ``routes``
    attributes (the autouse guard calls this before redirecting, so first use
    is always un-redirected), but it must NOT happen at import time -- a
    module-level ``routes._state_dir()`` freezes whichever ``KIROCREW_HOME``
    is active at collection, defeating pod isolation and the per-test home
    isolation (the class of leak the lazy-paths ratchet enforces against).
    """
    global _REAL_STATE_DIR
    if _REAL_STATE_DIR is None:
        _REAL_STATE_DIR = routes._state_dir()
    try:
        return {p.name: p.stat().st_mtime_ns for p in _REAL_STATE_DIR.iterdir()}
    except OSError:
        return {}


@pytest.fixture(autouse=True)
def _never_touch_the_real_state(_floor_monkeypatch, tmp_path):
    """Safety net: a test that forgets _redirect_state must still not write to
    the USER's live ~/.kiro/crew/workspace/spec-builder/.

    Two leaks got through before this was tight enough: one test called
    _save_index without redirecting at all, and later the tombstone file was
    added to the app without being added to _redirect_state -- so the deletion
    tests rewrote the real deleted.json. The guard therefore compares the WHOLE
    directory (names + mtimes) rather than one known filename, so the next file
    this app learns to write is covered without anyone remembering to list it.

    The decision ledger needs its own watch: it lives under the security keystone's
    ``trust/`` root, OUTSIDE this app's state dir, so the directory comparison above
    cannot see it -- and it holds the user's real recorded answers.

    The redirect goes through ``_floor_monkeypatch``, not the test's shared
    ``monkeypatch``: a test calling ``monkeypatch.undo()`` reverts every record on
    that instance, which would put the real paths back for the rest of the test.
    """
    before = _live_state_snapshot()
    before_ledger = _live_decisions_snapshot()
    _redirect_state(_floor_monkeypatch, tmp_path / "_autouse_state")
    yield
    assert (
        _live_state_snapshot() == before
    ), f"a test wrote to the live state dir: {_REAL_STATE_DIR}"
    assert _live_decisions_snapshot() == before_ledger, "a test wrote to the live decision ledger"


#: The un-redirected state dir the autouse guard watches. ``None`` until
#: :func:`_live_state_snapshot`'s first call fills it (see its docstring for
#: why capture is first-use, not import-time).
_REAL_STATE_DIR: Path | None = None


#: The local bootstrap subject the dashboard owner gate accepts when no owner is
#: configured. Execute, handoff and stop are owner-gated, so the default caller
#: is the owner; a test that wants a non-owner builds its own middleware.
_OWNER_USER = "local-app"


@web.middleware
async def _auth_mw(request, handler):
    """Inject the owner's claims (mirrors the gateway's auth middleware)."""
    request["user"] = _OWNER_USER
    request["app"] = ""
    return await handler(request)


def _make_client(monkeypatch, tmp_path):
    """An unstarted client over a freshly wired app.

    Set ``client.app["state"]`` (and any other app state) BEFORE
    ``await client.start_server()``: starting the server freezes the
    application, and aiohttp deprecates -- and will eventually refuse --
    every ``app[...] =`` write after that point.
    """
    _redirect_state(monkeypatch, tmp_path)
    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    return TestClient(TestServer(app))


async def _noop_await(sink: list) -> None:
    sink.append("called")


def _slot_stub():
    """A slot + state pair shaped like the ones _ensure_worker_slot expects."""

    class _Slot:
        key = "spec-builder-s"
        running = False
        messages: list = []
        _app = ""
        project = ""
        _titled = False

    slot = _Slot()

    class _State:
        def __init__(self):
            self._background_tasks: set[asyncio.Task] = set()

        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return slot

    return _State(), slot


class _RecordingSlot:
    """Minimal slot: records what _dispatch_turn appends, and reports running."""

    def __init__(self):
        self.running = True
        self.appended: list[tuple[str, str]] = []
        self.queued: list[str] = []
        self.queued_origins: list[bool] = []

    def queue_append(self, message, **kwargs):
        self.queued.append(message)
        self.queued_origins.append(kwargs["directive_user_origin"])

    def append(self, role, content, cls="", ts="", *, broadcast=True, meta=None):
        self.appended.append((role, content))


class _NoopState:
    def push_slots_update(self):
        pass


def _seed_spec(tmp_path, name="live", *, files=None, extra=None):
    """Seed one spec on disk and in the index. Returns (spec_dir, working_dir)."""
    working_dir = tmp_path / "wd"
    spec_dir = working_dir / ".kiro" / "specs" / name
    spec_dir.mkdir(parents=True)
    for fname, text in (files or {}).items():
        (spec_dir / fname).write_text(text)
    entry = {
        "spec_dir": str(spec_dir),
        "working_dir": str(working_dir),
        "spec_type": "feature",
        "status": "planning",
        "slot_key": routes._slot_key(name),
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    entry.update(extra or {})
    routes._save_index({name: entry})
    return spec_dir, working_dir


def _spec_identity(name="live"):
    """Return the creation identity a detail response gives the client."""
    entry = routes._load_index()[name]
    return {"spec_dir": entry["spec_dir"], "slot_key": entry["slot_key"]}


class _IdleSlot:
    """A slot of OUR app that is not mid-turn."""

    running = False
    messages: list = []
    _app = routes.APP_NAME
    project = ""
    _titled = False
    title = ""

    def __init__(self, key):
        self.key = key
        self.dispatched: list[str] = []


def _state_for(*names):
    """A fake gateway state serving an idle slot per spec name."""
    slots = {routes._slot_key(n): _IdleSlot(routes._slot_key(n)) for n in names}

    class _State:
        def get_slot(self, key):
            return slots.get(key)

        def get_or_create_slot(self, name, app=""):
            key = routes._slot_key(name)
            slots.setdefault(key, _IdleSlot(key))
            return slots[key]

    return _State(), slots


def _live_decisions_snapshot() -> tuple[bool, int]:
    """(exists, size) of the REAL decision ledger, captured before redirection."""
    global _REAL_DECISIONS_PATH
    if _REAL_DECISIONS_PATH is None:
        _REAL_DECISIONS_PATH = routes._decisions_path()
    try:
        return True, _REAL_DECISIONS_PATH.stat().st_size
    except OSError:
        return False, 0


def _final_answer(decision_id: str, option: str) -> dict[str, str]:
    """Build one final durable entry for tests that seed the protected store."""
    return {
        "decision_id": decision_id,
        "option": option,
        "fingerprint": "",
        "status": "final",
        "message": "",
        "delivery_id": "",
    }


def _decision_record(store: dict, spec_dir: str) -> dict[str, str]:
    """Test projection of durable entries as decision id to option."""
    return {
        entry["decision_id"]: entry["option"]
        for entry in routes._decision_entries(store, spec_dir).values()
    }


async def _recorded_answers(spec_dir: str) -> dict[str, str]:
    """Read the test projection without blocking the event loop."""

    def _read() -> dict[str, str]:
        with routes._DECISIONS_LOCK:
            store, _usable = routes._read_decisions()
            return _decision_record(store, spec_dir)

    return await asyncio.to_thread(_read)


def _decision_spec(tmp_path, *, state: dict | None = _DEFAULT_DECISION_STATE) -> tuple[str, str]:
    """Index one spec named ``s`` and return ``(spec_dir, slot_key)``."""
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "s"
    spec_dir.mkdir(parents=True, exist_ok=True)
    if state is not None:
        (spec_dir / ".spec-state.json").write_text(json.dumps(state))
    slot_key = routes._new_slot_key("s")
    routes._save_index(
        {
            "s": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": slot_key,
            }
        }
    )
    return str(spec_dir), slot_key


def _macos_case_aliases(monkeypatch) -> None:
    """Make path identity behave like a case-insensitive Darwin volume."""
    monkeypatch.setattr(routes, "_CASE_FOLD_TURN_KEYS", True)
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path)
    monkeypatch.setattr(
        routes.os.path,
        "samefile",
        lambda left, right: os.path.normpath(left).casefold() == os.path.normpath(right).casefold(),
    )


class _AsyncReturn:
    """An awaitable stand-in that ignores its arguments and returns ``value``."""

    def __init__(self, value):
        self.value = value

    async def __call__(self, *_a, **_kw):
        return self.value


def _create_client(monkeypatch, tmp_path):
    """A client whose create tail is stubbed out, plus its state.

    ``_make_client`` performs the state redirect, so it must run BEFORE anything
    seeds the ledger -- seeding first writes into the pre-redirect location, and a
    test asserting "no answers recorded" would then pass while proving nothing.

    The clear under test runs long before the worker slot is touched, so the slot
    setup and the seed dispatch are stubbed: neither is what these tests measure.
    """
    client = _make_client(monkeypatch, tmp_path)
    state, slot = _slot_stub()
    monkeypatch.setattr(routes, "_ensure_worker_slot", _AsyncReturn(slot))
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_a, **_kw: None)
    return client, state
