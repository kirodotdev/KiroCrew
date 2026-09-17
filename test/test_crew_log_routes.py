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
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_log as lg
from kiro_crew.dashboard.handlers import crew_log as routes
from kiro_crew.crew_log import Ledger, Ref
from kiro_crew.crew_log import projection as crew_log

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


def _log(unit_id: str = SESSION) -> Ledger:
    return Ledger.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew")


def _opened(handle: Ledger) -> None:
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


def _turn(handle: Ledger, turn: int) -> None:
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
    with patch.object(Ledger, "resolve", autospec=True, side_effect=Ledger.resolve) as spy:
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
        Ledger.create(lg.KIND_SESSION, f"s-kid{index}", owner="raymond", agent="kirocrew")
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


def _plant_unknown_required_type(handle: Ledger) -> None:
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
    with patch.object(Ledger, "iter_from", autospec=True, side_effect=Ledger.iter_from) as spy:
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
async def test_installing_the_publisher_registers_exactly_one_growth_listener():
    from kiro_crew.crew_log import emit as crew_log_emit

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
        [os.path.abspath(sys.executable), "-c", probe],
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PATH": os.environ.get("PATH", ""),
            "KIROCREW_HOME": os.environ.get("KIROCREW_HOME", ""),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []
