"""Shared state pins, fakes and request builders for the Spec Builder route coverage tests.

``test_spec_builder_routes_coverage.py`` and its ``test_spec_builder_routes_coverage_*.py``
siblings import from here: the three autouse fixtures (``_pin_state_dir`` points
the app's state files at ``tmp_path`` and clears its process globals,
``_quiet_sel`` stubs the SEL, ``_no_autonudge`` removes the autonudge service),
which every test module binds; the index builders; the ``_FakeProc``, ``_Slot``
and ``_State`` fakes; the spec-tree builder; and the mocked-request helpers
(``_mk``, ``_body``) with the ``_no_dispatch`` and ``_armed`` fixtures and the
handoff builders.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes as r

# A name that satisfies _NAME_RE yet does NOT survive _redact -- the exact shape
# _usable_name exists to reject (a description can slugify into one).
CRED_NAME = "ghp_" + "a" * 36


@pytest.fixture(autouse=True)
def _pin_state_dir(tmp_path: Path):
    """Point the app's three state files at tmp_path, and clear module globals.

    ``_SLOT_KEYS`` is process-global and rebuilt from every index read, so a key
    left behind by one test would make ``_slot_key`` resolve another test's spec.
    """
    state = tmp_path / "app-state"
    saved = dict(r._SLOT_KEYS)
    with (
        mock.patch.object(r, "_STATE_DIR", state),
        mock.patch.object(r, "_INDEX_PATH", state / "index.json"),
        mock.patch.object(r, "_DELETED_PATH", state / "deleted.json"),
        mock.patch.object(r, "_SETTINGS_PATH", state / "settings.json"),
        mock.patch.object(r, "_EXECUTION_CLAIMS", {}),
        mock.patch.object(r, "_EXECUTION_STOPS", {}),
        mock.patch.object(r, "_PENDING_DISPATCH_CLAIMS", {}),
        mock.patch.object(r, "_OBSERVED_SLOT_KEYS", {}),
        mock.patch.object(r, "_OBSERVED_SPEC_DIRS", {}),
    ):
        r._SLOT_KEYS.clear()
        yield state
    r._SLOT_KEYS.clear()
    r._SLOT_KEYS.update(saved)


@pytest.fixture(autouse=True)
def _quiet_sel():
    """A stub SEL so audits are observable and ``_audit_tool`` reports success.

    Left as the real object, ``_audit_tool(critical=True)`` would do a synchronous
    HMAC log write on every ``_git`` call; set to ``None`` it would return False
    and make ``_git`` fail closed in every test. A stub keeps both honest.
    """
    log = mock.MagicMock()
    with mock.patch.object(r, "sel", lambda: log):
        yield log


def _write_index(entries: dict) -> None:
    r._index_path().parent.mkdir(parents=True, exist_ok=True)
    r._index_path().write_text(json.dumps(entries))


def _entry(spec_dir: Path, **over) -> dict:
    base = {
        "working_dir": str(spec_dir.parent),
        "spec_dir": str(spec_dir),
        "spec_type": "feature",
        "status": "planning",
        "slot_key": "spec-builder-demo-0123abcd",
        "created_at": 100.0,
        "updated_at": 200.0,
    }
    base.update(over)
    return base


class _FakeProc:
    """The slice of ``asyncio.subprocess.Process`` that ``_git`` touches."""

    def __init__(self, rc: int = 0, out: bytes = b"", err: bytes = b""):
        self.returncode: int | None = None
        self._rc, self._out, self._err = rc, out, err
        self.killed = False

    async def communicate(self):
        self.returncode = self._rc
        return self._out, self._err

    def kill(self):
        self.killed = True

    async def wait(self):
        self.returncode = -9
        return self.returncode


class _Slot:
    """The slice of the gateway's ``_ChatSlot`` this module touches."""

    def __init__(self, key: str, *, app: str | None = r.APP_NAME, running: bool = False):
        self.key = key
        self._app = app
        self.running = running
        self.project: str | None = None
        self.title = ""
        self._titled = False
        self.messages: list[dict] = []
        self.task = None
        self._queue: list[str] = []
        self._pending_steers: list[str] = []
        self._pending_synthesis = False
        self.queued: list[str] = []

    def queue_append(self, message: str, *, meta=None, directive_user_origin: bool) -> None:
        assert directive_user_origin is False
        # The relay stamps the admission-time containment snapshot; an
        # app slot records app=True so its own queued turns keep draining.
        assert isinstance(meta, dict)
        self._queue.append(message)

    def append(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content, "ts": ""})


class _State:
    """A stand-in dashboard state: a slot registry plus the hooks this app calls."""

    def __init__(self, **slots: _Slot):
        self._slots: dict[str, _Slot] = dict(slots)
        self.titles: list[tuple[str, str]] = []
        self.pushes = 0
        self._background_tasks: set = set()
        self.sessions = SimpleNamespace(stop_turn=mock.AsyncMock())
        self.conversation_log = None

    def get_slot(self, key: str):
        return self._slots.get(key)

    def get_or_create_slot(self, *, name: str, app: str | None = None):
        slot = self._slots.get(name)
        if slot is None:
            slot = _Slot(name, app=app)
            self._slots[name] = slot
        return slot

    def push_slot_title(self, key: str, title: str) -> None:
        self.titles.append((key, title))

    def push_slots_update(self) -> None:
        self.pushes += 1


def _no_rehydrate():
    return mock.patch.object(
        r, "rehydrate_slot_from_history_async", mock.AsyncMock(return_value=None)
    )


def _spec_tree(root: Path, name: str, *files: str) -> Path:
    spec = root / ".kiro" / "specs" / name
    spec.mkdir(parents=True)
    for fname in files:
        (spec / fname).write_text("x")
    return spec


BASE = f"/api/apps/{r.APP_NAME}"


def _readable_payload():
    """A payload stub whose ``at_eof()`` is False, so ``can_read_body`` is True.

    ``make_mocked_request`` defaults to an empty stream, which reads as "no body"
    -- and ``_client_claim`` only consults the body when it can be read.
    """
    payload = mock.Mock()
    payload.at_eof.return_value = False
    return payload


#: The local bootstrap subject the dashboard owner gate accepts when no owner is
#: configured; execute, handoff and stop are owner-gated.
_OWNER_USER = "local-app"


def _mk(
    method: str,
    path: str,
    *,
    state=None,
    match: dict | None = None,
    query: str = "",
    body=...,
    authed: bool = True,
):
    """A mocked aiohttp request for a handler under test."""
    full = f"{BASE}/{path}" + (f"?{query}" if query else "")
    app = {"state": state} if state is not None else {}
    kwargs = {"app": app, "match_info": match or {}}
    if body is not ...:
        kwargs["payload"] = _readable_payload()
    req = make_mocked_request(method, full, **kwargs)  # type: ignore[arg-type]
    if authed:
        req["user"] = _OWNER_USER
        req["app"] = ""
    if body is not ...:
        if body is None:
            req.json = mock.AsyncMock(side_effect=ValueError("bad json"))  # type: ignore
        else:
            req.json = mock.AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response) -> dict:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


@pytest.fixture(autouse=True)
def _no_autonudge():
    """Default to "no autonudge service" so nothing reaches a live loop registry."""
    with mock.patch.object(r, "_autonudge_instance", lambda: None):
        yield


@pytest.fixture
def _no_dispatch():
    """Replace the turn relay: creating a real task needs a live chat runner."""
    with mock.patch.object(r, "_dispatch_turn") as dispatch:
        yield dispatch


@pytest.fixture
def _armed():
    """A stub autonudge service plus a successful authorization."""
    svc = mock.MagicMock()
    svc.get_by_slot.return_value = None
    svc.remove = mock.AsyncMock()
    authz = mock.AsyncMock(return_value=(SimpleNamespace(id="loop-1"), None, 0))
    with (
        mock.patch.object(r, "_autonudge_instance", lambda: svc),
        mock.patch.object(r, "authorize_and_add_nudge", authz),
        mock.patch.object(r, "_exec_loop_active", return_value=False),
    ):
        yield SimpleNamespace(svc=svc, authz=authz)


def _ready_handoff(sentinel: str = "/spec/STOP"):
    return mock.patch.object(r, "_prepare_handoff", return_value=(True, sentinel))


def _handoff_request(tmp_path, state, **body):
    _write_index({"demo": _entry(tmp_path / "demo", working_dir=str(tmp_path))})
    return _mk(
        "POST",
        "specs/demo/handoff",
        state=state,
        match={"name": "demo"},
        body=body or {},
    )
