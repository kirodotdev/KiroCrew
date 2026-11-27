"""Placement skips a crew that already holds a worker for the item (M1a).

A create whose reply was lost leaves the peer holding a session for the item; a
retry there is refused 409 ``duplicate_item`` before the peer makes anything. The
real placement and bound create run against stub crews: that crew is skipped and
named, and with no other crew the reply is ``crew_none_available``. Nearby peer
answers (another 409 code, ``duplicate_item`` on a non-409 status, a 409 with no
code, a 5xx) still stop the placement.
"""

from __future__ import annotations

from typing import Any

import pytest
import work_ledger_projection_helpers as helpers
from test_remote_dispatch_placement import Crews, _refused, _run, _settings
from work_ledger_projection_helpers import CONDUCTOR

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes
from kiro_crew.dashboard.handlers import remote_ingest
from kiro_crew.dashboard.handlers import remote_placement as placement

recorded = helpers.recorded
_isolated = helpers._isolated

_DUPLICATE = (
    409,
    {
        "error": "item 'it_x' already has a worker on this crew",
        "code": work_ledger.CODE_DUPLICATE_ITEM,
        "field": "item_id",
    },
)


class _Crews(Crews):
    """Crews whose create may answer a fixed ``(status, payload)``."""

    def _answer(self, iid: str, method: str, path: str, body: Any) -> tuple[int, Any]:
        answer = self.crews[iid].get("answer")
        if path == "api/chat/slots" and answer is not None:
            self.creates.append(iid)
            return answer
        return super()._answer(iid, method, path, body)


@pytest.fixture
def crews(monkeypatch) -> _Crews:
    monkeypatch.setattr(placement, "remote_dispatch_settings", _settings)
    return _Crews()


def test_a_peer_409_duplicate_item_reaches_placement_as_the_typed_error(
    recorded, crews, monkeypatch
):
    crews.add("a", answer=_DUPLICATE)
    seen: list[BaseException] = []
    real = remote_ingest.create_bound

    async def spy(*args: Any, **kwargs: Any):
        try:
            return await real(*args, **kwargs)
        except BaseException as exc:
            seen.append(exc)
            raise

    monkeypatch.setattr(remote_ingest, "create_bound", spy)

    async def body(state, items):
        await _refused(state, items[0])

    _run(crews, 1, body)
    assert len(seen) == 1 and type(seen[0]) is placement.DuplicateItemError
    assert (seen[0].status, seen[0].code) == (409, work_ledger.CODE_DUPLICATE_ITEM)
    # A crew_peer_refused-shaped error is not it, whatever its text says.
    assert not isinstance(
        routes.CrewCallError(409, "duplicate_item", "x"), placement.DuplicateItemError
    )


def test_a_duplicate_item_crew_is_skipped_and_named(recorded, crews):
    crews.add("a", mem=40, answer=_DUPLICATE)
    crews.add("b", mem=10)

    async def body(state, items):
        result, name = await placement.place(
            state, state.instances_manager, CONDUCTOR, {"work_item": items[0]}, board=CONDUCTOR
        )
        assert name == "b" and result["crew"] == "b"
        assert result["skipped"] == [{"crew": "a", "crew_name": "a", "code": "duplicate_item"}]
        stored = work_ledger.read_work_item(CONDUCTOR, items[0])
        assert stored is not None and stored.worker_instance_id == "b"

    _run(crews, 1, body)
    # a has more headroom, so it was asked first.
    assert crews.creates == ["a", "b"]


def test_a_placement_with_no_skip_carries_no_skipped(recorded, crews):
    crews.add("b")

    async def body(state, items):
        result, _ = await placement.place(
            state, state.instances_manager, CONDUCTOR, {"work_item": items[0]}, board=CONDUCTOR
        )
        assert "skipped" not in result

    _run(crews, 1, body)


def test_only_a_duplicate_item_crew_answers_none_available(recorded, crews):
    crews.add("a", answer=_DUPLICATE)

    async def body(state, items):
        exc = await _refused(state, items[0])
        assert (exc.status, exc.code) == (503, placement.CREW_NONE_AVAILABLE)
        assert "a answered duplicate_item" in exc.message
        stored = work_ledger.read_work_item(CONDUCTOR, items[0])
        assert stored is not None and not stored.is_terminal
        assert not stored.worker_session_key and not stored.worker_instance_id

    _run(crews, 1, body)
    assert crews.creates == ["a"]


def test_busy_and_duplicate_crews_are_both_named(recorded, crews):
    crews.add("a", mem=40, answer=_DUPLICATE)
    crews.add("b", mem=10, create="busy")

    async def body(state, items):
        exc = await _refused(state, items[0])
        assert exc.code == placement.CREW_NONE_AVAILABLE
        assert "b answered crew_busy" in exc.message
        assert "a answered duplicate_item" in exc.message

    _run(crews, 1, body)


@pytest.mark.parametrize(
    "answer",
    [
        (409, {"error": "no", "code": "crew_dispatch_limit"}),  # another 409 code
        (409, {"error": "no"}),  # a 409 with no code
        (409, {"error": "x (duplicate_item)"}),  # the code only in the prose
        (500, {"error": "boom", "code": "duplicate_item"}),  # the code on a 5xx
        (400, {"error": "no", "code": "duplicate_item"}),  # the code on a 400
        (502, {"error": "bad gateway"}),
    ],
)
def test_nearby_refusals_still_stop_the_placement(recorded, crews, answer):
    crews.add("a", mem=40, answer=answer)
    crews.add("b", mem=10)

    async def body(state, items):
        assert (await _refused(state, items[0])).code == "crew_peer_refused"

    _run(crews, 1, body)
    assert crews.creates == ["a"]


def test_the_code_matches_the_store():
    assert placement.CODE_DUPLICATE_ITEM == work_ledger.CODE_DUPLICATE_ITEM


@pytest.mark.parametrize(
    "skipped, shown",
    [
        ([{"crew": "a-1", "crew_name": "lab", "code": "duplicate_item"}], "Skipped `lab`"),
        ([{"crew": "a-1", "code": "duplicate_item"}], "Skipped `a-1`"),
        (None, None),
        ("lab", None),  # not a list
        ([{"code": "duplicate_item"}, 3], None),  # no name to show
    ],
)
def test_the_tool_names_a_skipped_crew(skipped, shown):
    from unittest.mock import MagicMock

    from kiro_crew.mcp_session_crew import run_crew_create

    resp: dict[str, Any] = {"target": "c", "crew": "b-2", "crew_name": "b", "placed": True}
    if skipped is not None:
        resp["skipped"] = skipped
    ctx = MagicMock()
    ctx.client.post.return_value = resp
    out = run_crew_create({"work_item": "it_0000abcd"}, ctx)
    assert "on crew `b`" in out
    if shown is None:
        assert "Skipped" not in out
    else:
        assert f"{shown}: it already holds a worker for this item" in out
