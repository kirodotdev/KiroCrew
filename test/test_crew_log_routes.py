"""The crew-log read routes and the ``session_projection`` push.

Three things are pinned here that the fold tests cannot see: the owner gate on
both reads, the deliberate difference in posture between a PAGE read and a FOLD
read over the same bytes, and that the push sends a frame only for a projection
whose seq actually moved.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, Ref
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard.handlers import crew_log as routes

SESSION = "s-route"
GATEWAY = "gateway"


@pytest.fixture(autouse=True)
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


def _log(unit_id: str = SESSION) -> CrewLog:
    return CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew")


def _opened(handle: CrewLog) -> None:
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": "dashboard:1",
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
        },
        src=GATEWAY,
    )


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


def _page_request(session_id: str = SESSION, query: str = "") -> object:
    url = f"/api/sessions/{session_id}/crew-log" + (f"?{query}" if query else "")
    request = make_mocked_request("GET", url)
    request.match_info["id"] = session_id
    return request


def _projection_request(name: str, session_id: str = SESSION) -> object:
    request = make_mocked_request("GET", f"/api/sessions/{session_id}/crew-log/projection/{name}")
    request.match_info["id"] = session_id
    request.match_info["name"] = name
    return request


def _body(response) -> dict:
    return json.loads(response.body)


# --- the owner gate -------------------------------------------------------


@pytest.mark.asyncio
async def test_both_reads_refuse_a_caller_that_is_not_the_owner():
    """A crew log holds the session's message bodies, so a reader must be the owner."""
    _opened(_log())
    with (
        patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ),
        patch("kiro_crew.dashboard.handlers._shared.logger"),
    ):
        page = await routes.api_session_crew_log(_page_request())
        fold = await routes.api_session_crew_log_projection(_projection_request("status"))
    assert page.status in {401, 403}
    assert fold.status in {401, 403}


@pytest.mark.asyncio
async def test_the_module_stands_behind_the_private_member_guard():
    """Every ``api_`` route here is wrapped, so one added later is refused by default."""
    for handler in (routes.api_session_crew_log, routes.api_session_crew_log_projection):
        assert getattr(handler, "__wrapped__", None) is not None


# --- the page read --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_page_returns_the_requested_range_oldest_first():
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    _turn(handle, 2)
    body = _body(await routes.api_session_crew_log(_page_request(query="from=2&to=3")))
    assert [row["seq"] for row in body["entries"]] == [2, 3]
    assert body["from"] == 2
    assert body["to"] == 3
    assert body["last_seq"] == handle.last_seq
    assert body["exists"] is True


@pytest.mark.asyncio
async def test_a_page_hands_back_the_cursor_for_the_next_one():
    handle = _log()
    _opened(handle)
    for turn in range(1, 4):
        _turn(handle, turn)
    body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=2")))
    assert body["next_from"] == 3
    tail = _body(await routes.api_session_crew_log(_page_request(query="from=3&to=999")))
    assert tail["next_from"] is None


@pytest.mark.asyncio
async def test_a_span_wider_than_one_page_is_clamped_rather_than_refused():
    handle = _log()
    _opened(handle)
    body = _body(
        await routes.api_session_crew_log(
            _page_request(query=f"from=1&to={lg.MAX_PAGE_LIMIT + 500}")
        )
    )
    assert body["to"] == lg.MAX_PAGE_LIMIT
    assert handle.last_seq == 1


@pytest.mark.asyncio
async def test_from_defaults_to_the_start_and_to_defaults_to_one_page():
    _opened(_log())
    body = _body(await routes.api_session_crew_log(_page_request()))
    assert body["from"] == 1
    assert body["to"] == lg.DEFAULT_PAGE_LIMIT


@pytest.mark.asyncio
async def test_a_malformed_range_is_refused():
    _opened(_log())
    for query in ("from=0", "from=abc", "from=5&to=2", "to=-1"):
        response = await routes.api_session_crew_log(_page_request(query=query))
        assert response.status == 400, query
        assert _body(response)["code"] == "bad_range"


@pytest.mark.asyncio
async def test_a_session_with_no_crew_log_reads_as_an_empty_page():
    body = _body(await routes.api_session_crew_log(_page_request("s-none")))
    assert body["exists"] is False
    assert body["entries"] == []
    assert body["last_seq"] == 0


# --- refs on a page ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_page_resolves_a_ref_to_a_verdict_and_a_span():
    child = _log("s-child")
    _opened(child)
    _turn(child, 1)
    parent = _log()
    _opened(parent)
    parent.append(
        "subagent/spawned",
        {"turn": 1, "agent_id": "sub-1", "agent": "worker", "model": "opus"},
        src=GATEWAY,
        ref=Ref(unit=lg.KIND_SESSION, id="s-child", from_seq=1, to_seq=2),
    )
    body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=9")))
    cited = [row for row in body["entries"] if "ref_resolution" in row]
    assert len(cited) == 1
    assert cited[0]["ref_resolution"]["status"] == lg.STATUS_OK
    assert cited[0]["ref_resolution"]["entries"] == 2
    # The verdict and the span, never the cited bytes.
    assert "entries" not in cited[0]["ref"]


@pytest.mark.asyncio
async def test_a_ref_into_a_session_that_has_no_log_resolves_as_gone():
    parent = _log()
    _opened(parent)
    parent.append(
        "subagent/spawned",
        {"turn": 1, "agent_id": "sub-1", "agent": "worker", "model": "opus"},
        src=GATEWAY,
        ref=Ref(unit=lg.KIND_SESSION, id="s-vanished", from_seq=1),
    )
    body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=9")))
    cited = [row for row in body["entries"] if "ref_resolution" in row]
    assert cited[0]["ref_resolution"]["status"] == lg.STATUS_GONE


@pytest.mark.asyncio
async def test_identical_refs_on_one_page_are_resolved_once():
    child = _log("s-child")
    _opened(child)
    parent = _log()
    _opened(parent)
    pointer = Ref(unit=lg.KIND_SESSION, id="s-child", from_seq=1)
    for index in range(3):
        parent.append(
            "subagent/spawned",
            {"turn": 1, "agent_id": f"sub-{index}", "agent": "worker", "model": "opus"},
            src=GATEWAY,
            ref=pointer,
        )
    with patch.object(CrewLog, "resolve", autospec=True, side_effect=CrewLog.resolve) as spy:
        body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=9")))
    assert spy.call_count == 1
    cited = [row for row in body["entries"] if "ref_resolution" in row]
    assert len(cited) == 3


@pytest.mark.asyncio
async def test_a_page_past_its_ref_budget_says_how_many_it_left():
    child = _log("s-child")
    _opened(child)
    parent = _log()
    _opened(parent)
    extra = 2
    for index in range(routes.MAX_PAGE_REFS + extra):
        # A DISTINCT ref each time, so the budget rather than the dedupe is what
        # bounds the work.
        CrewLog.create(lg.KIND_SESSION, f"s-kid{index}", owner="raymond", agent="kirocrew")
        parent.append(
            "subagent/spawned",
            {"turn": 1, "agent_id": f"sub-{index}", "agent": "worker", "model": "opus"},
            src=GATEWAY,
            ref=Ref(unit=lg.KIND_SESSION, id=f"s-kid{index}", from_seq=1),
        )
    body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=200")))
    resolved = [row for row in body["entries"] if "ref_resolution" in row]
    assert len(resolved) == routes.MAX_PAGE_REFS
    assert body["refs_unresolved"] == extra


# --- the projection read -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", crew_log.PROJECTION_NAMES)
async def test_each_projection_is_served_with_the_seq_it_folded_through(name):
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    body = _body(await routes.api_session_crew_log_projection(_projection_request(name)))
    assert body["name"] == name
    assert body["seq"] == handle.last_seq
    assert body["session_id"] == SESSION
    assert body["value"] == crew_log.read_projection(SESSION, name).value


@pytest.mark.asyncio
async def test_an_unknown_projection_name_is_refused():
    _opened(_log())
    response = await routes.api_session_crew_log_projection(_projection_request("board"))
    assert response.status == 400
    assert _body(response)["code"] == "unknown_projection"


@pytest.mark.asyncio
async def test_a_projection_for_a_session_with_no_log_is_the_empty_one():
    body = _body(await routes.api_session_crew_log_projection(_projection_request("usage", "s-x")))
    assert body["seq"] == 0
    assert body["value"]["turns"]["completed"] == 0


# --- the posture difference ---------------------------------------------


def _plant_unknown_required_type(handle: CrewLog) -> None:
    line = json.dumps(
        {
            "type": "turn/teleported",
            "seq": handle.last_seq + 1,
            "time": 1789000000000,
            "src": GATEWAY,
            "data": {"turn": 1},
        }
    )
    with handle.path.open("a", encoding="utf-8") as sink:
        sink.write(line + "\n")


@pytest.mark.asyncio
async def test_a_page_shows_a_line_it_cannot_interpret():
    """Paging renders history for a person: an unfamiliar line is a detail, not a fault."""
    handle = _log()
    _opened(handle)
    _plant_unknown_required_type(handle)
    body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=9")))
    assert [row["type"] for row in body["entries"]] == ["session/opened", "turn/teleported"]


@pytest.mark.asyncio
async def test_a_fold_refuses_a_line_it_cannot_interpret():
    """A fold would answer with a total the unknown line may have changed."""
    handle = _log()
    _opened(handle)
    _plant_unknown_required_type(handle)
    response = await routes.api_session_crew_log_projection(_projection_request("usage"))
    assert response.status == 409
    assert _body(response)["code"] == lg.CODE_UNKNOWN_ENTRY_TYPE


# --- the push -----------------------------------------------------------


class _Sockets:
    """A dashboard state stub that records what a push would send."""

    def __init__(self, watchers: int = 1) -> None:
        self.frames: list[tuple[str, dict]] = []
        self._watchers = watchers

    def dashboard_user_ws_count(self) -> int:
        return self._watchers

    def broadcast_ws_owners(self, frame: str, data: dict) -> None:
        self.frames.append((frame, data))


@pytest.mark.asyncio
async def test_a_growth_pushes_one_frame_per_projection():
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    await publisher._publish(SESSION)
    assert {frame for frame, _ in state.frames} == {routes.FRAME}
    assert {data["name"] for _, data in state.frames} == set(crew_log.PROJECTION_NAMES)
    for _, data in state.frames:
        assert data["session_id"] == SESSION
        assert data["seq"] == handle.last_seq
        assert "value" in data


@pytest.mark.asyncio
async def test_a_second_pass_with_no_growth_pushes_nothing():
    handle = _log()
    _opened(handle)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    await publisher._publish(SESSION)
    state.frames.clear()
    await publisher._publish(SESSION)
    assert state.frames == []


@pytest.mark.asyncio
async def test_a_growth_only_reads_the_entries_that_arrived():
    handle = _log()
    _opened(handle)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    await publisher._publish(SESSION)
    first_seq = handle.last_seq
    _turn(handle, 1)
    with patch.object(CrewLog, "iter_from", autospec=True, side_effect=CrewLog.iter_from) as spy:
        await publisher._publish(SESSION)
    assert spy.call_args.args[1] == first_seq + 1


@pytest.mark.asyncio
async def test_no_watcher_means_no_fold_and_no_frame():
    handle = _log()
    _opened(handle)
    state = _Sockets(watchers=0)
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    publisher._dirty.add(SESSION)
    await publisher._flush()
    assert state.frames == []
    assert handle.last_seq == 1


@pytest.mark.asyncio
async def test_the_publisher_caches_a_bounded_number_of_sessions():
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    for index in range(routes.MAX_CACHED_SESSIONS + 3):
        unit = f"s-many{index}"
        _opened(_log(unit))
        await publisher._publish(unit)
    assert len(publisher._bundles) == routes.MAX_CACHED_SESSIONS


@pytest.mark.asyncio
async def test_a_fold_refusal_does_not_stop_the_other_sessions():
    good = _log("s-good")
    _opened(good)
    broken = _log("s-broken")
    _opened(broken)
    _plant_unknown_required_type(broken)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    publisher._dirty.update({"s-good", "s-broken"})
    await publisher._flush()
    assert {data["session_id"] for _, data in state.frames} == {"s-good"}


def test_notify_from_a_writer_thread_does_no_work_of_its_own():
    """The writer hands the id to the loop; the reading happens there."""
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    loop = MagicMock()
    publisher.bind(loop)
    publisher.notify(SESSION)
    loop.call_soon_threadsafe.assert_called_once()
    assert state.frames == []


def test_notify_before_the_publisher_is_bound_is_a_no_op():
    publisher = routes.CrewLogPublisher(_Sockets())
    publisher.notify(SESSION)
    publisher.notify("")


@pytest.mark.asyncio
async def test_installing_the_publisher_registers_exactly_one_growth_listener(monkeypatch):
    from kiro_crew.crew_log import emit as crew_log_emit

    monkeypatch.setenv(routes.CREW_LOG_ENV, "1")
    with (
        patch.object(routes, "_publisher", None),
        patch.object(crew_log_emit, "_growth_listeners", []),
    ):
        first = routes.install_crew_log_publisher(_Sockets())
        with patch.object(routes, "_publisher", first):
            again = routes.install_crew_log_publisher(_Sockets())
        assert again is first
        assert len(crew_log_emit._growth_listeners) == 1


def test_the_frame_keeps_the_name_the_rfc_gives_it():
    assert routes.FRAME == "session_projection"


def test_this_module_does_not_load_the_storage_package_at_import():
    """The crew log is optional, and this module sits on the dashboard's boot path.

    A clean interpreter is the only place it is observable: this suite has already
    imported the storage package, so an in-process check would read its own
    imports rather than the boot path's.
    """
    probe = (
        "import importlib, json, sys;"
        "importlib.import_module('kiro_crew.dashboard.handlers.crew_log');"
        "print(json.dumps(sorted(k for k in sys.modules if k.startswith('kiro_crew.crew_log'))))"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [os.path.abspath(sys.executable), "-B", "-c", probe],
        # Inherit the full environment (Windows needs SYSTEMROOT and friends to
        # start the interpreter at all) and layer the probe's own values on top.
        # ``-B`` already stops the child writing bytecode into the checkout, so no
        # env var is relied on for that.
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "KIROCREW_HOME": os.environ.get("KIROCREW_HOME", ""),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []


def test_run_does_not_start_an_overlapping_flush_while_one_is_in_flight():
    """A second scheduled pass must not run concurrently with a slow flush.

    Two overlapping ``_publish`` for one session would share the same ``before``
    bundle and race the cache write, so an older seq could be broadcast last.
    """
    publisher = routes.CrewLogPublisher(_Sockets())
    loop = MagicMock()
    publisher.bind(loop)
    publisher._flushing = True
    publisher._scheduled = True
    publisher._run()
    loop.create_task.assert_not_called()


def test_finished_reschedules_when_work_arrived_mid_flush():
    """A growth marked during a flush is picked up once the pass finishes."""
    publisher = routes.CrewLogPublisher(_Sockets())
    loop = MagicMock()
    publisher.bind(loop)
    publisher._flushing = True
    publisher._dirty.add(SESSION)
    publisher._scheduled = False
    done = MagicMock()
    done.cancelled.return_value = False
    done.exception.return_value = None
    publisher._finished(done)
    assert publisher._flushing is False
    assert publisher._scheduled is True
    loop.call_later.assert_called_once()


def test_a_page_reports_the_tail_it_observed_not_a_stale_cached_one():
    """A page must not tell a client the history ends where its handle thinks.

    ``CrewLog.last_seq`` is the handle's own cached figure and its docstring says it
    is authoritative only for that handle's own appends. A reader never appends, so
    a writer growing the file after the handle opened is invisible to it. The pass
    over the file is live and walks the whole tail, so the real end is observable;
    deriving the metadata from the cached figure instead would return rows up to
    ``to`` and still report that nothing follows, and a client that believes it
    stops paging with entries left unread.
    """
    handle = _log()
    _opened(handle)
    for turn in (1, 2):
        _turn(handle, turn)

    # A handle opened NOW, then a writer that grows the file behind its back.
    stale = crew_log.open_session_log(SESSION)
    assert stale is not None
    cached = stale.last_seq
    writer = CrewLog.open(lg.KIND_SESSION, SESSION)
    for turn in (3, 4, 5):
        _turn(writer, turn)
    assert writer.last_seq > cached
    assert stale.last_seq == cached  # the reader handle never learned

    with patch.object(crew_log, "open_session_log", return_value=stale):
        page = routes._read_page(SESSION, 1, cached)

    # The page stops at the range it was asked for, but it does NOT claim the log
    # ends there: next_from points at the entries the writer added.
    assert page["last_seq"] == writer.last_seq
    assert page["next_from"] == cached + 1
    assert max(row["seq"] for row in page["entries"]) == cached


@pytest.mark.asyncio
async def test_a_recreated_log_at_the_same_seq_still_pushes_its_new_values():
    """A seq is only comparable within one file.

    ``fold_session`` refuses a bundle whose origin does not match the file and
    rebuilds from the start, so a log removed and recreated can come back at the
    same terminal seq carrying entirely different values. Comparing seqs alone
    reads that as nothing having moved and suppresses every frame, leaving each
    client holding the retired file's projection with no later growth able to
    dislodge it.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    await publisher._publish(SESSION)
    before = publisher._bundles[SESSION]
    state.frames.clear()

    # Same session id, a DIFFERENT file, folded to the same terminal seq. Handing
    # the publisher that cached bundle is the recreated-log shape without touching
    # the filesystem, so it behaves the same on every platform.
    other = _log("s-recreated-src")
    _opened(other)
    _turn(other, 1)
    fresh = crew_log.fold_session("s-recreated-src", crew_log.PROJECTION_NAMES)
    assert fresh.last_seq == before.last_seq  # seq-only check would suppress
    assert fresh.origin != before.origin
    publisher._bundles[SESSION] = crew_log.SessionProjections(
        session_id=SESSION,
        last_seq=fresh.last_seq,
        checkpoints=fresh.checkpoints,
        origin="a-retired-file",
    )

    await publisher._publish(SESSION)
    assert state.frames, "a rebuilt bundle must push, not be read as unchanged"


@pytest.mark.asyncio
async def test_rebinding_the_publisher_repoints_it_at_the_state_now_serving():
    """A restart inside one process must not keep broadcasting to the retired hub.

    The publisher is a per-process singleton, so a second install returns the same
    object. Rebinding only the loop would leave it counting the old hub's sockets
    and sending every frame to a room nobody is in, which looks exactly like a
    session that quietly stopped updating.
    """
    handle = _log()
    _opened(handle)
    retired = _Sockets()
    publisher = routes.CrewLogPublisher(retired)
    publisher.bind(asyncio.get_running_loop())

    serving = _Sockets()
    publisher.bind(asyncio.get_running_loop(), serving)
    await publisher._publish(SESSION)

    assert serving.frames, "frames must reach the state now serving"
    assert retired.frames == [], "and none must reach the retired one"


@pytest.mark.asyncio
async def test_rebinding_clears_scheduling_flags_left_on_the_retired_loop():
    """A timer armed on a closed loop never fires and a flush there never ends.

    Left set, ``_scheduled`` makes a growth believe a pass is already coming and
    ``_flushing`` makes the runner yield to a pass that does not exist, so the
    publisher would go quiet permanently after a restart. The dirty set is kept:
    those sessions did grow and the next pass folds them forward.
    """
    publisher = routes.CrewLogPublisher(_Sockets())
    publisher.bind(asyncio.get_running_loop())
    publisher._scheduled = True
    publisher._flushing = True
    publisher._dirty.add(SESSION)

    publisher.bind(asyncio.get_running_loop(), _Sockets())

    assert publisher._scheduled is False
    assert publisher._flushing is False
    assert publisher._dirty == {SESSION}


def test_the_flag_name_matches_the_emitters_own_constant():
    """The boot path spells the variable itself, so a test keeps the two in step.

    Importing the emitter to ask whether the crew log is wanted is the cost the
    flag exists to avoid, so the name is spelled in the handler. That is only safe
    while something proves the spelling still matches.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    assert routes.CREW_LOG_ENV == crew_log_emit.CREW_LOG_ENV


@pytest.mark.asyncio
async def test_installing_with_the_flag_off_builds_nothing(monkeypatch):
    """A launch without the flag must not pay for the subsystem it will not use.

    The installer runs on the gateway's boot path. With the crew log off it returns
    without importing the emitter and without constructing a publisher, so a
    disabled launch does no optional work and registers no listener.
    """
    monkeypatch.delenv(routes.CREW_LOG_ENV, raising=False)
    monkeypatch.setattr(routes, "_publisher", None)
    from kiro_crew.crew_log import emit as crew_log_emit

    with patch.object(crew_log_emit, "_growth_listeners", []):
        assert routes.install_crew_log_publisher(_Sockets()) is None
        assert crew_log_emit._growth_listeners == []
    assert routes._publisher is None


# --------------------------------------------------------------------------- #
# The agent's door: the unit-keyed routes and who may walk through them
# --------------------------------------------------------------------------- #


class _Slot:
    """The little of a dashboard slot these routes read."""

    def __init__(self, *, app: str = "", restricted: bool = False) -> None:
        self._app = app
        self.is_restricted = restricted
        self.linked_session_key = ""


class _Sessions:
    """A SessionManager stand-in: one slot key maps to one live ACP session id."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def get_provider(self, key: str) -> object | None:
        found = self._mapping.get(key)
        return _Provider(found) if found else None


class _Provider:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


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


def test_the_caller_name_is_pinned_to_the_mcp_servers_own(monkeypatch):
    """Two modules name one component; a rename on one side must fail here."""
    from kiro_crew.mcp_crew_log import SERVER_NAME

    assert routes.CREW_LOG_MCP_CALLER == SERVER_NAME


def test_the_enable_hint_names_the_real_flag():
    assert routes.CREW_LOG_ENV in routes.CREW_LOG_ENABLE_HINT


def test_the_enable_hint_names_the_live_data_home_not_the_legacy_one():
    """An agent is told to edit this file, so naming the wrong one wastes the turn.

    The live credentials file is ``~/.kiro/crew/.env`` (``config/loader.py``'s own
    header, and ``config_dir()`` under the default home). ``~/.kirocrew/.env`` is a
    legacy location that ``sandbox.py`` keeps only to fence a leftover copy;
    nothing reads configuration from it. A hint naming it sends the reader to an
    inert file, and the flag appears not to work.

    Not compared against ``config_dir()``: the suite's isolation fixture overrides
    the home, so that call answers a ``tmp_path`` here and would pass on either
    string.
    """
    assert ".kiro/crew/.env" in routes.CREW_LOG_ENABLE_HINT
    assert ".kirocrew/" not in routes.CREW_LOG_ENABLE_HINT


def test_the_page_reader_is_the_one_shared_implementation():
    """One implementation, so the browser door and the agent door cannot differ."""
    from kiro_crew.crew_log import read as shared

    assert routes.MAX_PAGE_REFS == shared.MAX_PAGE_REFS
    assert routes._read_page.__module__ == routes.__name__
    with patch.object(shared, "read_page", return_value={"exists": False}) as called:
        routes._read_page(SESSION, 1, 2)
    called.assert_called_once_with(SESSION, 1, 2)


def test_a_read_with_the_flag_off_says_how_to_switch_it_on(monkeypatch):
    """MUTATION-SENSITIVE: the agent learns the flag state from THIS refusal."""
    monkeypatch.delenv(routes.CREW_LOG_ENV, raising=False)
    request = _internal_request("/api/crew-log/sessions")
    response = asyncio.run(routes.api_crew_log_sessions(request))
    assert response.status == 422
    body = json.loads(response.text)
    assert body["code"] == "crew_log_disabled"
    assert routes.CREW_LOG_ENV in body["error"]


class TestWhoMayReadAnotherUnit:
    """The owner at a dashboard tab, and nobody else. Each refusal names its class."""

    @pytest.mark.parametrize(
        "session_key",
        [
            "cron:nightly",
            "subagent:abc123",
            "taskrunner:run-1",
            "slack:1712793600.123",
            "discord:99",
            "some-future-surface:1",
        ],
        ids=["cron", "subagent", "taskrunner", "slack", "discord", "unnamed-surface"],
    )
    def test_a_headless_or_channel_caller_is_refused(self, monkeypatch, session_key):
        """The rule is the ``dashboard:`` namespace, not a list of what a caller is not.

        The last case is a namespace no release has shipped: a new surface is
        refused the wider read the day it is added, without anyone remembering to
        name it here.
        """
        _flag_on(monkeypatch)
        request = _internal_request("/api/crew-log/sessions", session_key=session_key)
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        body = json.loads(response.text)
        assert body["code"] == "forbidden"
        assert session_key.split(":", 1)[0] in body["error"]

    def test_a_caller_with_no_session_key_is_refused(self, monkeypatch):
        _flag_on(monkeypatch)
        request = _internal_request("/api/crew-log/sessions", session_key="")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        assert json.loads(response.text)["code"] == "forbidden"

    def test_a_key_naming_no_live_slot_is_refused(self, monkeypatch):
        """A popped tab cannot say whose it was, so it is not the person."""
        _flag_on(monkeypatch)
        request = _internal_request("/api/crew-log/sessions", slots={})
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        assert "no live dashboard slot" in json.loads(response.text)["error"]

    def test_an_app_owned_session_is_refused(self, monkeypatch):
        """An app agent arrives on the same transport and must not inherit the person."""
        _flag_on(monkeypatch)
        request = _internal_request(
            "/api/crew-log/sessions", slots={"chat-owner": _Slot(app="travel-desk")}
        )
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        assert "app-owned" in json.loads(response.text)["error"]

    def test_an_incognito_session_is_refused(self, monkeypatch):
        """The session asked to leave nothing behind; handing it every other
        session's history is the opposite of that."""
        _flag_on(monkeypatch)
        request = _internal_request(
            "/api/crew-log/sessions", slots={"chat-owner": _Slot(restricted=True)}
        )
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        assert "incognito or temporary" in json.loads(response.text)["error"]

    def test_a_request_naming_another_component_is_refused(self, monkeypatch):
        """The route serves ONE internal caller; the header is validated, not trusted."""
        _flag_on(monkeypatch)
        request = _internal_request("/api/crew-log/sessions", caller="kirocrew-dashboard")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        assert routes.CREW_LOG_MCP_CALLER in json.loads(response.text)["error"]

    def test_the_owner_at_a_dashboard_tab_is_admitted(self, monkeypatch):
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        request = _internal_request("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        assert json.loads(response.text)["kind"] == "session"


class TestOnlyTheInternalTransportReachesThese:
    """A cookie-authenticated caller is refused here, and told where its door is.

    Strict membership is NOT what refuses it. ``token_auth_middleware`` falls
    through to ordinary cookie auth when a LOOPBACK request carries no
    ``X-Internal-Secret``, on a strict path as much as a mixed one, and calls the
    handler once the cookie validates; strict decides only the NON-loopback caller,
    and ``local_only=False`` reclassifies strict as mixed anyway. So a same-machine
    tab reaches these handlers, and the refusal below is the only thing stopping it.
    """

    @pytest.mark.parametrize(
        "handler,path,match",
        [
            ("api_crew_log_sessions", "/api/crew-log/sessions", None),
            ("api_crew_log_resolve", f"/api/crew-log/resolve?key={OWNER_KEY}", None),
            (
                "api_crew_log_unit_page",
                f"/api/crew-log/units/{SESSION}/page",
                {"unit": SESSION},
            ),
            (
                "api_crew_log_unit_projection",
                f"/api/crew-log/units/{SESSION}/projection/status",
                {"unit": SESSION, "name": "status"},
            ),
        ],
        ids=["sessions", "resolve", "page", "projection"],
    )
    def test_a_caller_with_no_internal_secret_is_refused(self, monkeypatch, handler, path, match):
        """All four, because one unguarded route is the whole hole."""
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(path, secret=False, match=match)
        response = asyncio.run(getattr(routes, handler)(request))
        assert response.status == 403
        body = json.loads(response.text)
        assert body["code"] == "forbidden"
        assert "/api/sessions/" in body["error"]

    def test_a_cookie_caller_never_reaches_the_owner_test(self, monkeypatch):
        """The owner test must not become a second way in from an authenticated page.

        If a cookie caller fell through to it, any page the person has open would
        reach ANOTHER session's recorded history -- the owner test would pass,
        because the person IS the owner. The refusal has to land before it.
        """
        _flag_on(monkeypatch)
        request = _internal_request("/api/crew-log/sessions", secret=False)
        with patch.object(routes, "require_owner_dashboard_request") as owner_gate:
            response = asyncio.run(routes.api_crew_log_sessions(request))
        owner_gate.assert_not_called()
        assert response.status == 403

    def test_the_prefix_is_on_the_strict_transport(self):
        """Strict, not mixed: a forwarded browser is hard-denied rather than
        offered the cookie fall-through, because what is behind it is another live
        session's history. The browser's own pair must stay OUT of the prefix, or
        the panel could not load its own log at all."""
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        assert "/api/crew-log" in _STRICT_INTERNAL_API_PATHS
        assert "/api/crew-log" not in _MIXED_INTERNAL_API_PATHS
        assert "/api/sessions" not in _STRICT_INTERNAL_API_PATHS


class TestSelfScope:
    """Reading YOUR OWN unit needs only a strict identity -- no dashboard tab."""

    def test_a_subagent_may_read_its_own_unit(self, monkeypatch):
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        _turn(handle, 1)
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/page",
            session_key="subagent:abc",
            slots={},
            sessions={"subagent:abc": SESSION},
            match={"unit": SESSION},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["entries"]

    def test_a_subagent_may_not_read_a_different_unit(self, monkeypatch):
        """The self scope is derived server-side from the forwarded key, so naming
        another unit falls to the owner rule rather than being waved through."""
        _flag_on(monkeypatch)
        other = _log("s-someone-else")
        _opened(other)
        request = _internal_request(
            "/api/crew-log/units/s-someone-else/page",
            session_key="subagent:abc",
            slots={},
            sessions={"subagent:abc": SESSION},
            match={"unit": "s-someone-else"},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert json.loads(response.text)["code"] == "forbidden"

    def test_an_incognito_session_may_still_read_its_own_unit(self, monkeypatch):
        """Restriction is about what LEAVES the session, not about reading itself."""
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/projection/status",
            slots={"chat-owner": _Slot(restricted=True)},
            match={"unit": SESSION, "name": "status"},
        )
        response = asyncio.run(routes.api_crew_log_unit_projection(request))
        assert response.status == 200
        assert json.loads(response.text)["name"] == "status"


class TestTheNewRoutes:
    def test_the_listing_returns_one_row_per_unit(self, monkeypatch):
        _flag_on(monkeypatch)
        first = _log()
        _opened(first)
        _turn(first, 1)
        second = _log("s-second")
        _opened(second)
        request = _internal_request("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        body = json.loads(response.text)
        units = {row["unit"]: row for row in body["units"]}
        assert set(units) == {SESSION, "s-second"}
        assert units[SESSION]["slot"] == "dashboard:1"
        assert units[SESSION]["agent"] == "kirocrew"
        assert units[SESSION]["model"] == "opus"
        assert units[SESSION]["open"] is True
        assert units[SESSION]["last_seq"] >= 3
        assert body["scanned"] == 2
        assert body["truncated"] is False

    def test_type_counts_are_the_histogram_read_by_hand_today(self, monkeypatch):
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        _turn(handle, 1)
        _turn(handle, 2)
        request = _internal_request("/api/crew-log/sessions?with_type_counts=1")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        body = json.loads(response.text)
        assert body["type_counts"]["turn/started"] == 2
        assert body["type_counts"]["turn/completed"] == 2
        assert body["units"][0]["type_counts"]["session/opened"] == 1

    def test_a_slot_filter_keeps_only_matching_units(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        other = _log("s-other-slot")
        other.append(
            "session/opened",
            {
                "agent": "kirocrew",
                "slot": "dashboard:99",
                "model": "opus",
                "cwd": "/w",
                "owner": "raymond",
                "resumed": False,
            },
            src=GATEWAY,
        )
        request = _internal_request("/api/crew-log/sessions?slot_contains=dashboard%3A99")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        body = json.loads(response.text)
        assert [row["unit"] for row in body["units"]] == ["s-other-slot"]

    def test_an_unrecognized_query_does_not_change_what_is_listed(self, monkeypatch):
        """``kind`` is not part of this surface, so it is an unknown query.

        The listing holds sessions, because nothing writes a unit of another kind.
        A ``kind`` argument would therefore have one legal value equal to its own
        default, and refusing the illegal values would keep a decision alive on the
        surface that the storage layer does not offer. These routes ignore an
        unknown query parameter the way every other route does. What must NOT
        happen is a refusal, which reads to a caller as "this listing holds other
        kinds, and you named one wrong".
        """
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request("/api/crew-log/sessions?kind=members")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        body = json.loads(response.text)
        assert body["kind"] == "session"
        assert [row["unit"] for row in body["units"]] == [SESSION]

    def test_resolve_answers_the_unit_a_key_is_landing_in(self, monkeypatch):
        _flag_on(monkeypatch)
        request = _internal_request(f"/api/crew-log/resolve?key={OWNER_KEY}")
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == 200
        assert json.loads(response.text) == {"key": OWNER_KEY, "unit": SESSION}

    def test_resolve_refuses_a_key_with_no_live_acp_session(self, monkeypatch):
        _flag_on(monkeypatch)
        request = _internal_request("/api/crew-log/resolve?key=dashboard:chat-gone")
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == 404
        assert json.loads(response.text)["code"] == "unresolvable_key"

    def test_resolve_needs_a_key(self, monkeypatch):
        _flag_on(monkeypatch)
        request = _internal_request("/api/crew-log/resolve")
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == 400
        assert json.loads(response.text)["code"] == "unresolvable_key"

    def test_a_unit_with_no_log_is_unknown_rather_than_an_empty_page(self, monkeypatch):
        """``exists: false`` is the browser route's shape; an agent needs a code."""
        _flag_on(monkeypatch)
        request = _internal_request(
            "/api/crew-log/units/s-nothing/page", match={"unit": "s-nothing"}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 404
        assert json.loads(response.text)["code"] == "unknown_unit"

    def test_an_unknown_projection_is_named_as_such(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/projection/nope",
            match={"unit": SESSION, "name": "nope"},
        )
        response = asyncio.run(routes.api_crew_log_unit_projection(request))
        assert response.status == 400
        assert json.loads(response.text)["code"] == "unknown_projection"

    def test_a_reversed_range_is_a_bad_range(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/page?from=9&to=2", match={"unit": SESSION}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 400
        assert json.loads(response.text)["code"] == "bad_range"


class TestAudit:
    """Every read is SEL-audited under the SAME action names the browser routes use."""

    @pytest.mark.parametrize(
        "handler,operation,match",
        [
            ("api_crew_log_sessions", "session_crew_log.list", {}),
            ("api_crew_log_unit_page", "session_crew_log.read", {"unit": SESSION}),
            (
                "api_crew_log_unit_projection",
                "session_crew_log.projection",
                {"unit": SESSION, "name": "status"},
            ),
        ],
        ids=["list", "read", "projection"],
    )
    def test_a_granted_read_is_audited_under_its_action(
        self, monkeypatch, handler, operation, match
    ):
        _flag_on(monkeypatch)
        _opened(_log())
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        path = "/api/crew-log/x"
        request = _internal_request(path, match=match)
        with patch("kiro_crew.sel.sel", return_value=sel):
            asyncio.run(getattr(routes, handler)(request))
        rows = [row for row in recorded if row["operation"] == operation]
        assert rows, recorded
        assert rows[0]["caller"] == routes.CREW_LOG_MCP_CALLER
        assert rows[0]["source"] == "mcp"
        assert rows[0]["outcome"] == "granted"

    def test_a_denied_read_is_audited_with_its_reason(self, monkeypatch):
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", session_key="cron:nightly")
        with patch("kiro_crew.sel.sel", return_value=sel):
            asyncio.run(routes.api_crew_log_sessions(request))
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows and rows[0]["outcome"] == "denied"
        assert "cron" in rows[0]["error"]

    def test_a_secretless_caller_is_audited_as_the_dashboard(self, monkeypatch):
        """The denial an operator most wants: something reached an MCP-only route
        with a browser-shaped credential. It is refused BEFORE the caller identity
        is settled, so the record has to come from ``request_origin``'s no-secret
        answer -- ``("dashboard", "dashboard")`` -- rather than from a claimed
        component name, which an unauthenticated caller could set to anything.
        """
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", secret=False)
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows, recorded
        assert rows[0]["outcome"] == "denied"
        assert rows[0]["caller"] == "dashboard"
        assert rows[0]["source"] == "dashboard"
        assert "/api/sessions/" in rows[0]["error"]

    def test_a_request_naming_another_component_is_audited(self, monkeypatch):
        """The other boundary-crossing shape: an authenticated internal caller that
        is not this server. It audits under the name ``request_origin`` resolved,
        which is a KNOWN component or the clamped ``unknown-internal`` -- never the
        raw header value.
        """
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", caller="kirocrew-dashboard")
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows, recorded
        assert rows[0]["outcome"] == "denied"
        assert rows[0]["caller"] == "kirocrew-dashboard"
        assert rows[0]["source"] == "mcp"
        assert routes.CREW_LOG_MCP_CALLER in rows[0]["error"]

    def test_an_unrecognized_component_is_audited_as_unknown_internal(self, monkeypatch):
        """A name no release ships is clamped, so the audit log cannot be seeded
        with an arbitrary string by whoever set the header."""
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", caller="not-a-real-server")
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows and rows[0]["caller"] == "unknown-internal"

    @pytest.mark.parametrize(
        "handler,operation,match",
        [
            ("api_crew_log_sessions", "session_crew_log.list", {}),
            ("api_crew_log_resolve", "session_crew_log.resolve", {}),
            ("api_crew_log_unit_page", "session_crew_log.read", {"unit": SESSION}),
            (
                "api_crew_log_unit_projection",
                "session_crew_log.projection",
                {"unit": SESSION, "name": "status"},
            ),
        ],
        ids=["list", "resolve", "read", "projection"],
    )
    def test_no_route_refuses_without_leaving_a_record(
        self, monkeypatch, handler, operation, match
    ):
        """One unaudited denial path is the whole gap, so all four are pinned."""
        _flag_on(monkeypatch)
        _opened(_log())
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/x", secret=False, match=match)
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(getattr(routes, handler)(request))
        assert response.status == 403
        assert [row for row in recorded if row["outcome"] == "denied"], recorded

    def test_a_failing_audit_never_changes_the_outcome(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request("/api/crew-log/sessions")
        with patch("kiro_crew.sel.sel", side_effect=RuntimeError("sel is down")):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200

    def test_the_proxys_own_call_lands_in_the_callers_crew_log(self, monkeypatch):
        """The read is recorded twice on purpose: SEL for the operator, and a
        ``tool/called`` entry in the calling session's OWN log for the record the
        session itself carries. The second is automatic; this pins that it happens.
        """
        monkeypatch.setenv(routes.CREW_LOG_ENV, "1")
        from kiro_crew.crew_log import emit

        handle = _log()
        _opened(handle)
        handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src=GATEWAY)
        emit.on_tool_called(
            SESSION,
            1,
            name="crew_log_read",
            server="kirocrew-crew-log",
            kind="read",
            call_id="c-1",
            args='{"unit": "self"}',
        )
        emit.drain_for_shutdown()
        types = [entry.type for entry in CrewLog.open(lg.KIND_SESSION, SESSION).iter_from(1)]
        assert "tool/called" in types
        called = [
            entry
            for entry in CrewLog.open(lg.KIND_SESSION, SESSION).iter_from(1)
            if entry.type == "tool/called"
        ]
        assert called[0].data["server"] == "kirocrew-crew-log"
        assert called[0].data["name"] == "crew_log_read"


class TestTheBrowserRoutesAreUnchanged:
    def test_the_browser_page_route_still_refuses_the_internal_caller(self, monkeypatch):
        """The session-keyed pair is cookie-only: the agent's door is the unit-keyed
        prefix, and admitting the internal caller here would widen two routes."""
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(f"/api/sessions/{SESSION}/crew-log", match={"id": SESSION})
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            response = asyncio.run(routes.api_session_crew_log(request))
        assert response.status == 403


class TestTheListingBoundsWhatItRetains:
    """The count bound is applied while walking, not by the scan cap downstream.

    A host that has run many sessions is the ordinary case, not the adversarial
    one. If the walk retained every directory before the cap, the cost of one
    listing would scale with how many sessions the host had ever run, which is
    what the bound exists to prevent.
    """

    @staticmethod
    def _dirs(count: int, *, newest_first_names: bool = True) -> list[str]:
        """*count* session unit directories, each with a distinct write time.

        The stamp goes on the LOG, not the directory, because that is what the
        reader orders by -- a real unit is a directory holding ``log.jsonl``, and a
        directory with no log is not a unit at all.
        """
        from kiro_crew.crew_log import read as reader

        root = lg.crew_log_root(lg.KIND_SESSION)
        root.mkdir(parents=True, exist_ok=True)
        names = []
        for index in range(count):
            unit = root / f"s-unit-{index:04d}"
            unit.mkdir()
            log = unit / reader.LOG_FILE
            log.write_text("", encoding="utf-8")
            # Ascending write time, so the LAST created is the newest.
            os.utime(log, (1_700_000_000 + index, 1_700_000_000 + index))
            names.append(unit.name)
        assert reader  # the module under test is importable from here
        return names if newest_first_names else list(reversed(names))

    def test_only_the_newest_candidates_are_retained(self, monkeypatch):
        from kiro_crew.crew_log import read as reader

        names = self._dirs(7)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 3)
        kept, cut = reader._candidate_dirs(lg.KIND_SESSION)
        assert [path.name for path in kept] == names[-3:][::-1]
        assert cut is True

    def test_a_root_within_the_bound_is_not_reported_cut(self, monkeypatch):
        from kiro_crew.crew_log import read as reader

        self._dirs(3)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 3)
        kept, cut = reader._candidate_dirs(lg.KIND_SESSION)
        assert len(kept) == 3
        assert cut is False

    def test_a_cut_candidate_set_makes_the_listing_report_truncated(self, monkeypatch):
        """A caller must not read a bounded walk as the whole tree."""
        from kiro_crew.crew_log import read as reader

        self._dirs(5)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 2)
        payload = reader.list_session_units(limit=50)
        assert payload["truncated"] is True

    def test_the_walk_never_holds_more_than_the_bound(self, monkeypatch):
        """The bound is on RETENTION: the heap is capped during iteration, so the
        peak held size cannot grow with the directory count."""
        from kiro_crew.crew_log import read as reader

        self._dirs(9)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 2)
        peaks: list[int] = []
        real_push = reader.heapq.heappush

        def watched(heap, item):
            real_push(heap, item)
            peaks.append(len(heap))

        monkeypatch.setattr(reader.heapq, "heappush", watched)
        reader._candidate_dirs(lg.KIND_SESSION)
        assert peaks and max(peaks) <= 2

    def test_a_root_that_was_never_created_is_empty_and_not_cut(self):
        from kiro_crew.crew_log import read as reader

        assert not lg.crew_log_root(lg.KIND_SESSION).exists()
        assert reader._candidate_dirs(lg.KIND_SESSION) == ([], False)


class TestRecencyComesFromTheLogNotItsDirectory:
    """A long-lived session is the case that breaks reading the directory's mtime.

    A directory's mtime moves when its entry SET changes -- a file created, renamed
    or removed -- not when a file inside it is written. A unit directory gets its
    ``log.jsonl`` once and is appended to for the rest of the session, so its own
    mtime freezes at creation. Order by it and "newest first" silently means "most
    recently STARTED first", and a recency window drops the session that has been
    open and busy for a day while keeping one created a minute ago and idle since.
    That is backwards for both callers: the listing exists to find active sessions.
    """

    @staticmethod
    def _unit(unit_id: str, *, dir_at: float, log_at: float) -> None:
        """One real unit whose directory and log carry DIFFERENT times.

        Through ``crew_log_dir``, never ``root / unit_id``: a unit directory is named
        with a readable-plus-digest fold of the id, not the id itself.
        """
        from kiro_crew.crew_log import read as reader

        _opened(_log(unit_id))
        directory = lg.crew_log_dir(lg.KIND_SESSION, unit_id)
        os.utime(directory / reader.LOG_FILE, (log_at, log_at))
        # The directory LAST, so creating the log cannot move it afterwards.
        os.utime(directory, (dir_at, dir_at))

    def test_the_busy_old_session_sorts_newer_than_the_idle_new_one(self):
        from kiro_crew.crew_log import read as reader

        now = time.time()
        self._unit("s-open-all-day", dir_at=now - 86_400, log_at=now)
        self._unit("s-started-just-now", dir_at=now, log_at=now - 86_400)
        kept, _cut = reader._candidate_dirs(lg.KIND_SESSION)
        assert [path.name for path in kept] == [
            lg.crew_log_dir(lg.KIND_SESSION, "s-open-all-day").name,
            lg.crew_log_dir(lg.KIND_SESSION, "s-started-just-now").name,
        ]

    def test_a_recency_window_keeps_the_busy_session_and_drops_the_idle_one(self):
        from kiro_crew.crew_log import read as reader

        now = time.time()
        self._unit("s-open-all-day", dir_at=now - 86_400, log_at=now)
        self._unit("s-started-just-now", dir_at=now, log_at=now - 86_400)
        payload = reader.list_session_units(limit=50, active_within_ms=3_600_000)
        assert [row["unit"] for row in payload["units"]] == ["s-open-all-day"]

    def test_a_unit_with_no_readable_log_sorts_oldest_rather_than_raising(self):
        """An unreadable member of the root must not fail the whole listing.

        It is refused a moment later for having no unit id, so the only question
        here is whether one bad directory can take the listing down with it.
        """
        from kiro_crew.crew_log import read as reader

        now = time.time()
        self._unit("s-real", dir_at=now, log_at=now)
        (lg.crew_log_root(lg.KIND_SESSION) / "s-no-log").mkdir()
        assert reader._written_at(lg.crew_log_root(lg.KIND_SESSION) / "s-no-log") == 0.0
        payload = reader.list_session_units(limit=50)
        assert [row["unit"] for row in payload["units"]] == ["s-real"]
