"""Shared fixtures and builders for the crew-log route suites.

``test_crew_log_routes.py`` and its ``_dispatch_fence``, ``_unit_door_audit`` and
``_listing_workspace`` siblings import these by name. Three are autouse, so each of
those modules imports all three: ``_isolated_home`` (a per-test ``KIROCREW_HOME``),
``_is_owner`` (every call is the dashboard owner's unless a test says otherwise) and
``_fresh_dispatch_scan`` (the read side's per-unit caches cleared around each test).
The rest are the session and unit names the suites share, builders for a unit's log
and its entries, the internal-transport request with its slot and state doubles, and
the conductor's four-unit dispatch tree.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.dashboard.handlers import crew_log as routes

SESSION = "s-route"
GATEWAY = "gateway"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_crew_log_routes*.py modules calls monkeypatch.undo()
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture(autouse=True)
def _is_owner():
    """Every test here calls as the dashboard owner unless it says otherwise."""
    with patch(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        return_value=True,
    ):
        yield


def _log(unit_id: str = SESSION, slot: str = "dashboard:1") -> CrewLog:
    # The HEADER's slot, which is what the session tree keys its fold by. Separate
    # from the opening entry's own ``slot`` field, and the two are kept equal here
    # because the emitter writes them from the same value.
    return CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)


def _opened(
    handle: CrewLog,
    *,
    parent_sid: str = "",
    parent_slot: str = "",
    slot: str = "dashboard:1",
    memory: str = "persistent",
    app: str = "",
    channel: bool = False,
    workspace: str = "default",
    record_class: bool = True,
) -> None:
    data = {
        "agent": "kirocrew",
        "slot": slot,
        "model": "opus",
        "cwd": "/w",
        "owner": "raymond",
        "resumed": False,
    }
    if parent_slot:
        # The same shape ``crew_log.emit`` writes: ``slot`` names the creator's tab,
        # which is what a scope test reads because a slot outlives its ACP session,
        # and ``sid`` names the creator's crew log unit as an audit citation.
        data["parent"] = {"slot": parent_slot}
        if parent_sid:
            data["parent"]["sid"] = parent_sid
    if record_class:
        # ``record_class=False`` is a log written before the class was recorded, which
        # a cross-session read must refuse rather than read as "nothing applies".
        session_class: dict[str, object] = {"memory": memory}
        if app:
            session_class["app"] = app
        if channel:
            session_class["channel"] = True
        if workspace:
            session_class["workspace"] = workspace
        data["class"] = session_class
    handle.append("session/opened", data, src=GATEWAY)


def _class_moved(
    unit_id: str,
    *,
    memory: str = "persistent",
    app: str = "",
    channel: bool = False,
    workspace: str = "default",
) -> None:
    """Append the ``session/class`` the emitter writes when a class moves mid-life.

    Same payload as the opening entry's ``class`` object, which is why both are
    declared from one field tuple: a reader folds the second over the first. The log
    is OPENED rather than created, because a transition by definition arrives after
    the opener and ``create`` refuses a log that already exists.
    """
    data: dict[str, object] = {"memory": memory}
    if app:
        data["app"] = app
    if channel:
        data["channel"] = True
    if workspace:
        data["workspace"] = workspace
    CrewLog.open(lg.KIND_SESSION, unit_id).append("session/class", data, src=GATEWAY)


def _turn(handle: CrewLog, turn: int) -> None:
    handle.append("turn/started", {"turn": turn, "actor": "user", "depth": 0}, src=GATEWAY)
    handle.append(
        "turn/completed",
        {
            "turn": turn,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 10,
            "model": "opus",
            "provider": "kiro",
            "credits": 0.1,
            "tokens": {"input": 5, "output": 1, "cache_read": 0, "cache_write": 0},
        },
        src=GATEWAY,
    )


class _Provider:
    """The one attribute ``session_id_of`` reads off a live provider."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _Sessions:
    """A SessionManager stand-in: an exact key lookup, the way the resolver uses it.

    Shared by ``test_crew_log_routes.py``'s session-keyed reads and by the unit-keyed
    door's ``_State``: both address a unit through one slot key, so one stand-in serves
    both.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def get_provider(self, key: str) -> object | None:
        found = self._mapping.get(key)
        return _Provider(found) if found else None


class _Slot:
    """The little of a dashboard slot these routes read."""

    def __init__(
        self,
        *,
        app: str = "",
        restricted: bool = False,
        key: str = "",
        memory_mode: str = "persistent",
        linked_session_key: str = "",
        workspace: str = "default",
    ) -> None:
        self._app = app
        self.workspace = workspace
        self.is_restricted = restricted
        self.linked_session_key = linked_session_key
        # ``key`` is how a slot is matched back to the unit it is writing, and
        # ``memory_mode`` is the field the mirrored incognito test reads --
        # ``is_restricted`` is the owner test's own signal and is not the same field.
        self.key = key
        self.memory_mode = memory_mode


class _State:
    def __init__(self, slots: dict[str, _Slot], sessions: dict[str, str]) -> None:
        self._slots = slots
        self.sessions = _Sessions(sessions)
        self.crons = None
        self.subagents = None


OWNER_KEY = "dashboard:chat-owner"


def _internal_request(
    path: str,
    *,
    caller: str = "kirocrew-crew-log",
    session_key: str = OWNER_KEY,
    secret: bool = True,
    slots: dict[str, _Slot] | None = None,
    sessions: dict[str, str] | None = None,
    match: dict[str, str] | None = None,
) -> object:
    """A request as the MCP proxy makes it: internal secret + caller + session key.

    ``secret=False`` is the shape a COOKIE-authenticated caller arrives in. The
    middleware admits one on loopback (it falls through to cookie auth when the
    header is absent, strict bucket or not), so the handler sees it and has to
    refuse it itself.
    """
    headers = {"X-Internal-Secret": "s3cret"} if secret else {}
    if caller:
        headers["X-Internal-Caller"] = caller
    if session_key:
        headers["X-Session-Key"] = session_key
    # A REAL application, not ``make_mocked_request``'s MagicMock default: these
    # handlers read ``request.app.get("state")``, and on the mock that answers a
    # fresh MagicMock -- every attribute of which is truthy, so the app-ownership
    # check would "find" an owning app for the person and the whole gate would be
    # tested against a fiction.
    app = web.Application()
    app["state"] = _State(
        {"chat-owner": _Slot()} if slots is None else slots,
        {OWNER_KEY: SESSION} if sessions is None else sessions,
    )
    request = make_mocked_request("GET", path, headers=headers, app=app)
    for key, value in (match or {}).items():
        request.match_info[key] = value
    return request


def _flag_on(monkeypatch) -> None:
    monkeypatch.setenv(routes.CREW_LOG_ENV, "1")


#: A conductor, its child and its grandchild, plus a session in another tree. The
#: keys are deliberately NOT ``dashboard:`` so the owner arm cannot answer for them
#: and the lineage arm is what is under test. The SLOT half of the conductor's key is
#: what the fence matches, because the recorded edge names a slot.
CONDUCTOR_KEY = "subagent:conductor"
CONDUCTOR_SLOT = "conductor"
CONDUCTOR_UNIT = "s-conductor"
CHILD_SLOT = "chat-child-1789000000"
CHILD_UNIT = "s-child"
GRANDCHILD_SLOT = "chat-grandchild-1789000001"
GRANDCHILD_UNIT = "s-grandchild"
STRANGER_SLOT = "chat-stranger-1789000002"
STRANGER_UNIT = "s-stranger"


@pytest.fixture(autouse=True)
def _fresh_dispatch_scan():
    """Drop the read side's per-unit caches between tests.

    Two of them, both module-level so they survive a request, which is what keeps a
    scope test one ``stat`` per untouched unit and one fold per grown log. Each is
    validated against the log's own identity, so a new data home would invalidate it
    anyway -- clearing them outright means these tests do not depend on that
    validation being right, which is a property with its own tests.
    """
    from kiro_crew.crew_log import read as crew_log_read

    crew_log_read._TREE._heads.clear()
    crew_log_read.reset_class_bundles()
    yield
    crew_log_read._TREE._heads.clear()
    crew_log_read.reset_class_bundles()


def _dispatch_tree(**child_class) -> None:
    """Four units: conductor -> child -> grandchild, and a stranger under nobody.

    The edges are recorded by SLOT, which is what the fence walks; each entry also
    carries the creator's unit as ``sid``, the way the emitter writes it, so a test
    that keys on the slot cannot pass merely because the sid happens to agree.
    ``child_class`` sets the child's own recorded class.
    """
    _opened(_log(CONDUCTOR_UNIT, slot=CONDUCTOR_SLOT), slot=CONDUCTOR_SLOT)
    _opened(
        _log(CHILD_UNIT, slot=CHILD_SLOT),
        slot=CHILD_SLOT,
        parent_slot=CONDUCTOR_SLOT,
        parent_sid=CONDUCTOR_UNIT,
        **child_class,
    )
    _opened(
        _log(GRANDCHILD_UNIT, slot=GRANDCHILD_SLOT),
        slot=GRANDCHILD_SLOT,
        parent_slot=CHILD_SLOT,
        parent_sid=CHILD_UNIT,
    )
    _opened(
        _log(STRANGER_UNIT, slot=STRANGER_SLOT),
        slot=STRANGER_SLOT,
        parent_slot="somebody-else",
        parent_sid="s-somebody-else",
    )


def _as_conductor(path: str, **kwargs) -> object:
    """A request from the conductor session, which is not the owner's own tab."""
    kwargs.setdefault("session_key", CONDUCTOR_KEY)
    kwargs.setdefault("slots", {"conductor": _Slot()})
    kwargs.setdefault("sessions", {CONDUCTOR_KEY: CONDUCTOR_UNIT})
    return _internal_request(path, **kwargs)
