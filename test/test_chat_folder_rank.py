"""Fractional rank keys for sidebar folder order.

Two layers: the pure helpers in ``kiro_crew.dashboard.folder_rank`` (key
generation, the comparator, the placement plan), and the PATCH endpoint that
positions a folder by naming a sibling (``before``/``after``). The endpoint
tests drive the real handler through the real ``FolderRepository`` transaction,
so what they assert is what lands in the store.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import random
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import folder_rank
from kiro_crew.dashboard.chat_folders import (
    _MAX_FOLDER_UPDATE_BODY_BYTES,
    api_chat_folder_create,
    api_chat_folder_delete,
    api_chat_folder_reorder_retired,
    api_chat_folder_update,
)
from kiro_crew.dashboard.folder_rank import (
    MAX_RANK_LEN,
    append_rank,
    custom_sort_key,
    plan_position,
    rank_between,
    section_siblings,
    spread_ranks,
    valid_rank,
)
from kiro_crew.dashboard.folder_repository import FolderRepository
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.loop_lock import LoopBoundLock

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "chat_folder_rank.json"


class TestRankBetween:
    def test_ends_and_empty_section(self) -> None:
        assert rank_between(None, None) == "V"
        assert rank_between(None, "V") < "V"
        assert rank_between("V", None) > "V"

    def test_adjacent_digits_descend_a_level(self) -> None:
        key = rank_between("1", "2")
        assert key is not None and "1" < key < "2"
        assert key.startswith("1")

    def test_below_a_key_that_starts_with_zeros(self) -> None:
        key = rank_between(None, "01")
        assert key is not None and key < "01"
        assert valid_rank(key) == key

    def test_refuses_a_bad_interval(self) -> None:
        assert rank_between("b", "a") is None
        assert rank_between("a", "a") is None
        assert rank_between("a0", None) is None  # trailing zero: not a valid rank
        assert rank_between("a!", None) is None

    def test_random_inserts_stay_strictly_between_and_valid(self) -> None:
        """Property check: any sequence of inserts keeps a strict total order."""
        rng = random.Random(20260929)
        for _ in range(300):
            keys = sorted(spread_ranks(rng.randint(0, 6)))
            for _step in range(40):
                i = rng.randint(0, len(keys))
                lo = keys[i - 1] if i > 0 else None
                hi = keys[i] if i < len(keys) else None
                key = rank_between(lo, hi)
                if key is None:
                    # Only a gap too deep for the length cap may refuse.
                    assert lo is not None and hi is not None
                    break
                assert valid_rank(key) == key
                assert (lo is None or lo < key) and (hi is None or key < hi)
                keys.insert(i, key)
            assert keys == sorted(keys)
            assert len(set(keys)) == len(keys)

    def test_the_same_gap_refuses_before_the_cap(self) -> None:
        """Repeated inserts into one gap end in a refusal, never an overlong key."""
        lo, hi = "V", "W"
        for _ in range(10_000):
            key = rank_between(lo, hi)
            if key is None:
                break
            assert len(key) <= MAX_RANK_LEN
            lo = key
        else:
            pytest.fail("the gap never filled")

    def test_deterministic(self) -> None:
        assert rank_between("3", "k") == rank_between("3", "k")


class TestSpreadRanks:
    @pytest.mark.parametrize("count", [0, 1, 2, 7, 61, 62, 500])
    def test_ascending_distinct_valid(self, count: int) -> None:
        keys = spread_ranks(count)
        assert len(keys) == count
        assert keys == sorted(keys)
        assert len(set(keys)) == count
        assert all(valid_rank(k) == k for k in keys)

    def test_every_gap_has_room(self) -> None:
        keys = spread_ranks(500)
        for lo, hi in zip([None, *keys], [*keys, None], strict=True):
            assert rank_between(lo, hi) is not None


class TestValidRank:
    @pytest.mark.parametrize(
        "value",
        [None, "", 5, 1.5, True, ["a"], {"a": 1}, "a0", "a b", "é", "x" * (MAX_RANK_LEN + 1)],
    )
    def test_rejects(self, value: object) -> None:
        assert valid_rank(value) is None

    def test_accepts(self) -> None:
        assert valid_rank("0V") == "0V"
        assert valid_rank("z" * MAX_RANK_LEN) == "z" * MAX_RANK_LEN


class TestCustomSortKey:
    def test_ranked_before_unranked_and_ties_break_by_id(self) -> None:
        rows = [
            {"id": "u1", "name": "a", "order": 0},
            {"id": "r2", "name": "z", "rank": "V"},
            {"id": "r1", "name": "y", "rank": "V"},
            {"id": "r0", "name": "x", "rank": "F"},
            {"id": "bad", "name": "b", "rank": 7, "order": -5},
        ]
        got = [r["id"] for r in sorted(rows, key=custom_sort_key)]
        # ``bad`` carries a corrupt rank, so it sorts with the unranked rows by order.
        assert got == ["r0", "r1", "r2", "bad", "u1"]

    def test_equal_ranks_break_by_utf16_id_order(self) -> None:
        ids = ["\uffff", "\U0001f600"]
        rows = [{"id": fid, "rank": "V"} for fid in ids]
        got = [r["id"] for r in sorted(rows, key=custom_sort_key)]
        expected = sorted(ids, key=lambda fid: fid.encode("utf-16-be", "surrogatepass"))
        assert got == expected
        assert got == ["\U0001f600", "\uffff"]

    def test_the_shared_fixture_agrees(self) -> None:
        """Same rows the sidebar's vitest suite sorts; see chat_folder_rank.json."""
        spec = json.loads(_FIXTURE.read_text())
        for case in spec["sort_cases"]:
            got = [r["id"] for r in sorted(case["rows"], key=custom_sort_key)]
            assert got == case["expected"], case["name"]

    def test_the_shared_fixture_keys_agree(self) -> None:
        spec = json.loads(_FIXTURE.read_text())
        for case in spec["between_cases"]:
            assert rank_between(case["lo"], case["hi"]) == case["expected"], case
        for case in spec["spread_cases"]:
            assert spread_ranks(case["count"]) == case["expected"], case


class TestPlanPosition:
    def test_fully_ranked_is_one_write(self) -> None:
        sibs = [{"id": "a", "rank": "F"}, {"id": "b", "rank": "V"}, {"id": "c", "rank": "k"}]
        rank, respread = plan_position(sibs, 1)
        assert respread == {}
        assert "F" < rank < "V"

    def test_an_unranked_section_is_spread_in_its_current_order(self) -> None:
        sibs = [
            {"id": "a", "order": 0, "name": "a"},
            {"id": "b", "order": 1, "name": "b"},
            {"id": "c", "order": 2, "name": "c"},
        ]
        rank, respread = plan_position(sibs, 1)
        assert set(respread) == {"a", "b", "c"}
        assert respread["a"] < rank < respread["b"] < respread["c"]

    def test_a_tie_at_the_gap_forces_a_spread(self) -> None:
        sibs = [{"id": "a", "rank": "V"}, {"id": "b", "rank": "V"}]
        rank, respread = plan_position(sibs, 1)
        assert respread["a"] < rank < respread["b"]

    def test_the_end_of_a_mixed_section_forces_a_spread(self) -> None:
        sibs = [{"id": "a", "rank": "V"}, {"id": "b", "order": 3}]
        rank, respread = plan_position(sibs, 2)
        assert "b" in respread and respread["b"] < rank

    def test_a_duplicate_pair_elsewhere_forces_a_spread(self) -> None:
        sibs = [
            {"id": "a", "rank": "F"},
            {"id": "b", "rank": "F"},
            {"id": "c", "rank": "V"},
        ]
        rank, respread = plan_position(sibs, 3)
        assert respread["a"] < respread["b"] < respread["c"] < rank

    def test_an_unranked_row_at_the_end_forces_a_front_spread(self) -> None:
        sibs = [{"id": "a", "rank": "F"}, {"id": "b", "rank": "V"}, {"id": "c"}]
        rank, respread = plan_position(sibs, 0)
        assert rank < respread["a"] < respread["b"] < respread["c"]

    def test_a_ranked_prefix_of_a_mixed_section_is_spread(self) -> None:
        sibs = [{"id": "a", "rank": "F"}, {"id": "b", "rank": "V"}, {"id": "c", "order": 3}]
        rank, respread = plan_position(sibs, 1)
        assert respread["a"] < rank < respread["b"] < respread["c"]

    def test_empty_section(self) -> None:
        assert plan_position([], 0) == ("V", {})


class TestAppendRank:
    def test_ranked_section_gets_a_trailing_rank(self) -> None:
        rank = append_rank([{"id": "a", "rank": "F"}, {"id": "b", "rank": "V"}])
        assert rank is not None and rank > "V"

    def test_mixed_section_stays_unranked(self) -> None:
        assert append_rank([{"id": "a", "rank": "F"}, {"id": "b"}]) is None

    def test_empty_section_is_ranked(self) -> None:
        assert append_rank([]) == "V"


class TestSectionSiblings:
    def test_orphans_count_as_top_level(self) -> None:
        rows = [
            {"id": "p", "parent_id": ""},
            {"id": "c", "parent_id": "p"},
            {"id": "o", "parent_id": "gone"},
        ]
        assert {f["id"] for f in section_siblings(rows, "")} == {"p", "o"}
        assert [f["id"] for f in section_siblings(rows, "p")] == ["c"]
        assert section_siblings(rows, "", exclude_id="p")[0]["id"] == "o"


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

A, B, C, D = "fldr0000000a", "fldr0000000b", "fldr0000000c", "fldr0000000d"
APP = "issue-radar"


def _legacy_rows() -> list[dict[str, Any]]:
    """Four top-level folders from before ranks: integer order only."""
    return [
        {"id": A, "name": "Alpha", "parent_id": "", "order": 0},
        {"id": B, "name": "Bravo", "parent_id": "", "order": 1, "owner_app": APP},
        {"id": C, "name": "Charlie", "parent_id": "", "order": 2},
        {"id": D, "name": "Delta", "parent_id": "", "order": 3, "owner_app": APP},
    ]


def _state(folders: list[dict[str, Any]], *slots: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = folders
    state._slots = {s.key: s for s in (_ChatSlot("chat-1-100"), *slots)}
    state.push_slots_update = MagicMock()
    state.conversation_log = None
    repo = FolderRepository(lambda: MagicMock())
    lock = LoopBoundLock()
    state.writes = 0

    def _writer(_path: Any, _snapshot: list[dict[str, Any]]) -> None:
        state.writes += 1

    async def _mutate(fn: Any, on_committed: Any = None) -> Any:
        return await repo.mutate(
            lambda: state._folders,
            lock,
            fn,
            lambda: MagicMock(name="folders.json"),
            _writer,
            on_committed,
        )

    state.mutate_folders = _mutate
    return state


def _app_slot(key: str, app: str) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._app = app
    return slot


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_post("/api/chat/folders/reorder", api_chat_folder_reorder_retired)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    app.router.add_delete("/api/chat/folders/{id}", api_chat_folder_delete)
    return app


def _drawn(state: DashboardState, parent: str = "") -> list[str]:
    return [f["id"] for f in section_siblings(state._folders, parent)]


def _ranks(state: DashboardState) -> dict[str, Any]:
    return {f["id"]: f.get("rank") for f in state._folders}


PERSON_KEY = {"X-Session-Key": "dashboard:chat-1-100"}
APP_KEY = {"X-Session-Key": "dashboard:chat-1-200"}


async def _patch(client: TestClient, fid: str, body: dict, headers: dict = PERSON_KEY) -> Any:
    return await client.patch(f"/api/chat/folders/{fid}", json=body, headers=headers)


class TestPositioningEndpoint:
    @pytest.mark.asyncio
    async def test_oversized_patch_is_413_without_a_write(self) -> None:
        state = _state(_legacy_rows())
        payload = b'{"name":"' + (b"x" * _MAX_FOLDER_UPDATE_BODY_BYTES) + b'"}'
        headers = {**PERSON_KEY, "Content-Type": "application/json"}
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(f"/api/chat/folders/{A}", data=payload, headers=headers)
        assert resp.status == 413, await resp.text()
        assert state.writes == 0
        assert state._folders == _legacy_rows()

    @pytest.mark.asyncio
    async def test_first_move_spreads_then_later_moves_write_one_row(self) -> None:
        state = _state(_legacy_rows())
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"after": A})
            assert resp.status == 200, await resp.text()
            assert _drawn(state) == [A, D, B, C]
            assert all(valid_rank(r) for r in _ranks(state).values())
            before = _ranks(state)
            resp = await _patch(client, A, {"before": C})
            assert resp.status == 200
        assert _drawn(state) == [D, B, A, C]
        after = _ranks(state)
        assert {k for k in after if after[k] != before[k]} == {A}
        # Legacy ``order`` is left as it was; it only decides unranked rows.
        assert [f["order"] for f in state._folders] == [0, 1, 2, 3]

    @pytest.mark.asyncio
    async def test_a_failed_spread_write_restores_live_and_persisted_state(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _legacy_rows()
        state = _state(rows)
        store = tmp_path / "folders.json"
        store.write_text(json.dumps(rows), encoding="utf-8")
        repo = FolderRepository(lambda: MagicMock())
        lock = LoopBoundLock()
        attempted: list[list[dict[str, Any]]] = []

        def _write_json(path: pathlib.Path, value: Any) -> None:
            path.write_text(json.dumps(value), encoding="utf-8")

        def _fail_confirmed_write(
            path: pathlib.Path, snapshot: list[dict[str, Any]], write_json: Any
        ) -> None:
            attempted.append(snapshot)
            raise OSError("persist failed")

        async def _mutate(fn: Any, on_committed: Any = None) -> Any:
            return await repo.mutate(
                lambda: state._folders,
                lock,
                fn,
                lambda: store,
                lambda path, snapshot: repo.write_confirmed(path, snapshot, _write_json),
                on_committed,
            )

        state.mutate_folders = _mutate
        before = {folder["id"]: (folder.get("rank"), folder.get("order")) for folder in rows}
        persisted_before = json.loads(store.read_text(encoding="utf-8"))
        monkeypatch.setattr(
            FolderRepository, "write_confirmed", staticmethod(_fail_confirmed_write)
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"after": A})

        assert resp.status == 500
        assert attempted and all(valid_rank(folder.get("rank")) for folder in attempted[0])
        assert {
            folder["id"]: (folder.get("rank"), folder.get("order")) for folder in state._folders
        } == before
        assert json.loads(store.read_text(encoding="utf-8")) == persisted_before

    @pytest.mark.asyncio
    async def test_before_the_first_and_after_the_last(self) -> None:
        state = _state(_legacy_rows())
        async with TestClient(TestServer(_make_app(state))) as client:
            assert (await _patch(client, C, {"before": A})).status == 200
            assert (await _patch(client, A, {"after": D})).status == 200
        assert _drawn(state) == [C, B, D, A]

    @pytest.mark.asyncio
    async def test_a_corrupt_rank_falls_back_to_the_spread(self) -> None:
        rows = _legacy_rows()
        for f, r in zip(rows, ["F", {"x": 1}, "V", "k"], strict=True):
            f["rank"] = r
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, A, {"after": D})
        assert resp.status == 200
        # B's corrupt rank sorted it after the ranked rows, so the gap after D
        # touches an unranked row: the spread keeps that drawn order around A.
        assert _drawn(state) == [C, D, A, B]
        assert all(valid_rank(r) for r in _ranks(state).values())

    @pytest.mark.asyncio
    async def test_an_app_cannot_position_in_the_persons_legacy_section(self) -> None:
        state = _state(_legacy_rows(), _app_slot("chat-1-200", APP))
        before = [dict(f) for f in state._folders]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"before": A}, APP_KEY)
            assert resp.status == 409, await resp.text()
            body = await resp.json()
        assert body["code"] == "folder_section_unranked"
        assert state._folders == before
        assert _ranks(state) == {A: None, B: None, C: None, D: None}
        assert state.writes == 0

    @pytest.mark.asyncio
    async def test_an_app_positions_after_the_person_arranges_the_section(self) -> None:
        state = _state(_legacy_rows(), _app_slot("chat-1-200", APP))
        async with TestClient(TestServer(_make_app(state))) as client:
            arranged = await _patch(client, C, {"before": A})
            assert arranged.status == 200, await arranged.text()
            before = _ranks(state)
            resp = await _patch(client, D, {"before": A}, APP_KEY)
        assert resp.status == 200, await resp.text()
        after = _ranks(state)
        assert {fid for fid in after if after[fid] != before[fid]} == {D}
        assert _drawn(state) == [C, D, A, B]

    @pytest.mark.asyncio
    async def test_an_app_may_spread_only_its_own_rows(self) -> None:
        rows = [
            {"id": A, "name": "Alpha", "parent_id": "", "order": 0},
            {"id": B, "name": "Bravo", "parent_id": A, "order": 0, "owner_app": APP},
            {"id": D, "name": "Delta", "parent_id": A, "order": 1, "owner_app": APP},
        ]
        state = _state(rows, _app_slot("chat-1-200", APP))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"before": B}, APP_KEY)
        assert resp.status == 200, await resp.text()
        assert _drawn(state, A) == [D, B]
        assert all(valid_rank(_ranks(state)[fid]) for fid in (B, D))

    @pytest.mark.asyncio
    async def test_an_app_reparents_to_a_legacy_section_without_spreading_it(self) -> None:
        rows = _legacy_rows()
        rows[3]["parent_id"] = B
        state = _state(rows, _app_slot("chat-1-200", APP))
        before = {f["id"]: dict(f) for f in state._folders}
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"parent_id": ""}, APP_KEY)
        assert resp.status == 200, await resp.text()
        after = {f["id"]: f for f in state._folders}
        assert all(after[fid] == before[fid] for fid in (A, B, C))
        assert after[D]["parent_id"] == ""
        assert after[D]["order"] == before[D]["order"]
        assert after[D].get("rank") is None
        assert state.writes == 1

    @pytest.mark.asyncio
    async def test_an_app_cannot_position_the_persons_folder(self) -> None:
        state = _state(_legacy_rows(), _app_slot("chat-1-200", APP))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, A, {"after": D}, APP_KEY)
        assert resp.status == 403
        assert _drawn(state) == [A, B, C, D]
        assert all(r is None for r in _ranks(state).values())
        assert state.writes == 0

    @pytest.mark.asyncio
    async def test_an_app_cannot_position_a_folder_holding_the_persons(self) -> None:
        rows = _legacy_rows()
        rows.append({"id": "fldr0000000k", "name": "Kid", "parent_id": D, "order": 0})
        state = _state(rows, _app_slot("chat-1-200", APP))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"before": A}, APP_KEY)
        assert resp.status == 403
        assert state.writes == 0

    @pytest.mark.asyncio
    async def test_adjacent_anchor_pair_places_between_siblings(self) -> None:
        state = _state(_legacy_rows())
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"after": A, "before": B})
        assert resp.status == 200, await resp.text()
        assert _drawn(state) == [A, D, B, C]
        assert state.writes == 1

    @pytest.mark.asyncio
    async def test_non_adjacent_anchor_pair_is_a_conflict_without_a_write(self) -> None:
        state = _state(_legacy_rows())
        before = [dict(f) for f in state._folders]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"after": A, "before": C})
            assert resp.status == 409
            assert (await resp.json())["code"] == "folder_anchor_not_sibling"
        assert state._folders == before
        assert state.writes == 0

    @pytest.mark.asyncio
    async def test_anchor_pair_crossing_sections_is_a_conflict_without_a_write(self) -> None:
        rows = _legacy_rows()
        rows[2]["parent_id"] = A
        state = _state(rows)
        before = [dict(f) for f in state._folders]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"after": A, "before": C})
            assert resp.status == 409
            assert (await resp.json())["code"] == "folder_anchor_not_sibling"
        assert state._folders == before
        assert state.writes == 0

    @pytest.mark.asyncio
    async def test_an_anchor_in_another_section_is_a_conflict(self) -> None:
        rows = _legacy_rows()
        rows[2]["parent_id"] = A  # Charlie now sits inside Alpha
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"after": C})
            missing = await _patch(client, D, {"after": "fldr0000zzzz"})
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "folder_anchor_not_sibling"
            assert body["error"] == (
                "The folder this move was placed next to has moved or was deleted. "
                "Try the move again."
            )
            assert missing.status == 409
        assert state.writes == 0

    @pytest.mark.asyncio
    async def test_reparent_and_position_in_one_request(self) -> None:
        rows = _legacy_rows()
        rows[1]["parent_id"] = A
        rows[2]["parent_id"] = A
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"parent_id": A, "before": C})
        assert resp.status == 200
        assert _drawn(state, A) == [B, D, C]
        assert state.writes == 1

    @pytest.mark.asyncio
    async def test_a_reparent_without_anchor_lands_at_the_end_of_a_ranked_section(self) -> None:
        rows = [
            {"id": A, "name": "Alpha", "parent_id": "", "order": 0},
            {"id": B, "name": "Bravo", "parent_id": A, "order": 9, "rank": "F"},
            {"id": C, "name": "Charlie", "parent_id": A, "order": 8, "rank": "V"},
            {"id": D, "name": "Delta", "parent_id": "", "order": 1, "rank": "0V"},
        ]
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, {"parent_id": A})
        assert resp.status == 200
        assert _drawn(state, A) == [B, C, D]
        assert _ranks(state)[D] > "V"

    @pytest.mark.asyncio
    async def test_a_reparent_without_anchor_lands_at_the_end_of_a_legacy_section(self) -> None:
        rows = _legacy_rows()
        rows[1]["parent_id"] = D  # Bravo (order 1) and Charlie (order 2) sit in Delta
        rows[2]["parent_id"] = D
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            # Alpha's order 0 would sort it FIRST among Delta's unranked children.
            resp = await _patch(client, A, {"parent_id": D})
        assert resp.status == 200
        assert _drawn(state, D) == [B, C, A]
        ranks = _ranks(state)
        assert all(valid_rank(ranks[fid]) for fid in (A, B, C))
        assert ranks[B] < ranks[C] < ranks[A]

    @pytest.mark.asyncio
    async def test_a_reparent_without_anchor_handles_a_clamped_legacy_order(self) -> None:
        rows = _legacy_rows()
        rows[1]["parent_id"] = D
        rows[1]["order"] = 2**53 - 1
        rows[2]["parent_id"] = D
        rows[2]["order"] = 2**53 - 1
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, A, {"parent_id": D})
        assert resp.status == 200
        assert _drawn(state, D) == [B, C, A]
        ranks = _ranks(state)
        assert all(valid_rank(ranks[fid]) for fid in (A, B, C))
        assert ranks[B] < ranks[C] < ranks[A]

    @pytest.mark.asyncio
    async def test_a_same_parent_patch_does_not_move_the_folder(self) -> None:
        rows = [
            {"id": A, "name": "Alpha", "parent_id": "", "order": 0, "rank": "F"},
            {"id": B, "name": "Bravo", "parent_id": "", "order": 1, "rank": "V"},
        ]
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, A, {"parent_id": "", "name": "Alpha2"})
        assert resp.status == 200
        assert _ranks(state)[A] == "F"

    @pytest.mark.asyncio
    async def test_concurrent_moves_both_land(self) -> None:
        """Two tabs moving different folders at once: both land, one consistent order."""
        state = _state(_legacy_rows())
        async with TestClient(TestServer(_make_app(state))) as client:
            one, two = await asyncio.gather(
                _patch(client, D, {"before": A}), _patch(client, C, {"before": A})
            )
        assert (one.status, two.status) == (200, 200)
        drawn = _drawn(state)
        assert drawn.index(D) < drawn.index(A) and drawn.index(C) < drawn.index(A)
        assert drawn[-1] == B
        assert len({f["rank"] for f in state._folders}) == 4

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"order": 3}, "order_retired"),
            ({"before": A, "after": ""}, "anchor_invalid"),
            ({"before": ""}, "anchor_invalid"),
            ({"after": 7}, "anchor_invalid"),
            ({"after": D}, "anchor_invalid"),
        ],
    )
    async def test_malformed_position_requests(self, body: dict, code: str) -> None:
        state = _state(_legacy_rows())
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await _patch(client, D, body)
            assert resp.status == 400
            assert (await resp.json())["code"] == code
        assert state.writes == 0


class TestRetiredReorderTombstone:
    """``POST /api/chat/folders/reorder`` is gone, but not a bare 404.

    A tab still running the pre-rank bundle drags folders through this POST.
    It gets a 410 whose ``error`` is the reload sentence that tab renders in
    its sidebar error line, and the folder store is left exactly as it was.
    """

    RELOAD = "This page is out of date; reload it to move folders."

    @pytest.mark.asyncio
    async def test_old_batch_reorder_is_410_and_writes_nothing(self) -> None:
        state = _state(_legacy_rows())
        before = [dict(f) for f in state._folders]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={"orders": [{"id": D, "order": 0}, {"id": A, "order": 3}]},
                headers=PERSON_KEY,
            )
            assert resp.status == 410
            body = await resp.json()
        assert body == {"error": self.RELOAD, "code": "reorder_retired"}
        assert state._folders == before
        assert state.writes == 0

    @pytest.mark.asyncio
    async def test_tombstone_never_reads_the_body(self) -> None:
        """A body that is not JSON (or not even there) still gets the 410."""
        state = _state(_legacy_rows())
        before = [dict(f) for f in state._folders]
        async with TestClient(TestServer(_make_app(state))) as client:
            garbage = await client.post(
                "/api/chat/folders/reorder", data=b"not json", headers=PERSON_KEY
            )
            empty = await client.post("/api/chat/folders/reorder", headers=PERSON_KEY)
            assert (garbage.status, empty.status) == (410, 410)
            assert (await garbage.json())["code"] == "reorder_retired"
            assert (await empty.json())["code"] == "reorder_retired"
        assert state._folders == before
        assert state.writes == 0

    @pytest.mark.asyncio
    async def test_patch_order_and_the_tombstone_say_the_same_sentence(self) -> None:
        """Both stale-client writes hand back one reload message."""
        state = _state(_legacy_rows())
        async with TestClient(TestServer(_make_app(state))) as client:
            patched = await _patch(client, D, {"order": 3})
            posted = await client.post(
                "/api/chat/folders/reorder", json={"orders": []}, headers=PERSON_KEY
            )
            assert (patched.status, posted.status) == (400, 410)
            assert (await patched.json())["error"] == (await posted.json())["error"]
        assert state.writes == 0

    @pytest.mark.asyncio
    async def test_tombstone_keeps_the_folder_write_caller_guard(self) -> None:
        """A caller naming a dashboard slot that is gone is refused like every
        other folder write, so the tombstone is not a guard-free route."""
        state = _state(_legacy_rows())
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={"orders": []},
                headers={"X-Session-Key": "dashboard:chat-9-999"},
            )
            assert resp.status == 403
        assert state.writes == 0


class TestCreateInARankedSection:
    @pytest.mark.asyncio
    async def test_a_new_folder_takes_a_trailing_rank(self) -> None:
        rows = [
            {"id": A, "name": "Alpha", "parent_id": "", "order": 0, "rank": "F"},
            {"id": B, "name": "Bravo", "parent_id": "", "order": 1, "rank": "V"},
        ]
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Charlie", "icon": "📁"},
                headers=PERSON_KEY,
            )
        assert resp.status in (200, 201), await resp.text()
        new = next(f for f in state._folders if f["name"] == "Charlie")
        assert new["rank"] > "V"
        assert _drawn(state)[-1] == new["id"]

    @pytest.mark.asyncio
    async def test_a_new_folder_in_a_legacy_section_stays_unranked(self) -> None:
        state = _state(_legacy_rows())
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Echo", "icon": "📁"},
                headers=PERSON_KEY,
            )
        assert resp.status in (200, 201), await resp.text()
        new = next(f for f in state._folders if f["name"] == "Echo")
        assert "rank" not in new
        assert _drawn(state)[-1] == new["id"]


class TestDeletePromotesChildrenIntoTheRootSection:
    """A folder's children move to the top level when it is deleted. Their
    ranks were generated for the OLD section, so a child keeping its rank can
    tie with a root folder (the first child of any folder and the first root
    folder are both "V"), and the next positioned move would re-spread the
    whole root. The delete places each child the way an anchor-less reparent
    does: after the root's last ranked sibling, or unranked in a legacy root."""

    E = "fldr0000000e"

    async def _delete(self, client: TestClient, fid: str) -> Any:
        return await client.delete(f"/api/chat/folders/{fid}", headers=PERSON_KEY)

    @pytest.mark.asyncio
    async def test_a_promoted_child_does_not_tie_with_a_root_folder(self) -> None:
        # A and B at the root, C inside B: C's rank is "V", the same key A got
        # as the first root folder.
        rows = [
            {"id": A, "name": "Alpha", "parent_id": "", "order": 0, "rank": "V"},
            {"id": B, "name": "Bravo", "parent_id": "", "order": 1, "rank": "k"},
            {"id": C, "name": "Charlie", "parent_id": B, "order": 2, "rank": "V"},
        ]
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await self._delete(client, B)
            assert resp.status == 200, await resp.text()
            ranks = _ranks(state)
            assert set(ranks) == {A, C}
            assert all(valid_rank(r) for r in ranks.values())
            assert len(set(ranks.values())) == 2
            assert _drawn(state) == [A, C]
            # The root is still rankable: a positioned move writes ONE row.
            before = _ranks(state)
            resp = await _patch(client, C, {"before": A})
            assert resp.status == 200, await resp.text()
        after = _ranks(state)
        assert {fid for fid in after if after[fid] != before[fid]} == {C}
        assert _drawn(state) == [C, A]

    @pytest.mark.asyncio
    async def test_promoted_children_keep_their_relative_order(self) -> None:
        # Inside D, E sorts before C by rank; both land after the root's last
        # sibling, E then C.
        rows = [
            {"id": A, "name": "Alpha", "parent_id": "", "order": 0, "rank": "F"},
            {"id": B, "name": "Bravo", "parent_id": "", "order": 1, "rank": "V"},
            {"id": D, "name": "Delta", "parent_id": "", "order": 2, "rank": "k"},
            {"id": C, "name": "Charlie", "parent_id": D, "order": 3, "rank": "k"},
            {"id": self.E, "name": "Echo", "parent_id": D, "order": 4, "rank": "F"},
        ]
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await self._delete(client, D)
        assert resp.status == 200, await resp.text()
        assert _drawn(state) == [A, B, self.E, C]
        ranks = _ranks(state)
        assert all(valid_rank(r) for r in ranks.values())
        assert len(set(ranks.values())) == 4
        assert ranks[self.E] > "V" and ranks[C] > ranks[self.E]

    @pytest.mark.asyncio
    async def test_promoted_children_follow_legacy_root_in_their_child_order(self) -> None:
        rows = [
            {"id": A, "name": "Alpha", "parent_id": "", "order": 0},
            {"id": C, "name": "Charlie", "parent_id": "", "order": 1},
            {"id": B, "name": "Bravo", "parent_id": "", "order": 2},
            {"id": D, "name": "Delta", "parent_id": B, "order": 5, "rank": "F"},
            {"id": self.E, "name": "Echo", "parent_id": B, "order": 0, "rank": "V"},
        ]
        state = _state(rows)
        root_before = {fid: dict(row) for fid, row in ((A, rows[0]), (C, rows[1]))}
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await self._delete(client, B)
        assert resp.status == 200, await resp.text()
        assert _drawn(state) == [A, C, D, self.E]
        after = {folder["id"]: folder for folder in state._folders}
        assert all("rank" not in after[fid] for fid in (D, self.E))
        assert (after[D]["order"], after[self.E]["order"]) == (2, 3)
        assert all(after[fid] == root_before[fid] for fid in (A, C))

    @pytest.mark.asyncio
    async def test_promotion_respreads_a_legacy_root_at_the_order_limit(self) -> None:
        rows = [
            {"id": A, "name": "Alpha", "parent_id": "", "order": 0},
            {"id": C, "name": "Charlie", "parent_id": "", "order": 2**53 - 1},
            {"id": B, "name": "Bravo", "parent_id": "", "order": 1},
            {"id": D, "name": "Delta", "parent_id": B, "order": 1, "rank": "V"},
            {"id": self.E, "name": "Echo", "parent_id": B, "order": 0, "rank": "F"},
        ]
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await self._delete(client, B)
        assert resp.status == 200, await resp.text()
        assert _drawn(state) == [A, C, self.E, D]
        ranks = _ranks(state)
        assert set(ranks) == {A, C, self.E, D}
        assert all(valid_rank(rank) for rank in ranks.values())
        assert len(set(ranks.values())) == 4

    @pytest.mark.asyncio
    async def test_a_child_promoted_into_a_legacy_root_stays_unranked(self) -> None:
        rows = _legacy_rows()
        rows.append({"id": self.E, "name": "Echo", "parent_id": B, "order": 4, "rank": "V"})
        state = _state(rows)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await self._delete(client, B)
        assert resp.status == 200, await resp.text()
        echo = next(f for f in state._folders if f["id"] == self.E)
        assert echo["parent_id"] == ""
        assert "rank" not in echo
        assert _drawn(state) == [A, C, D, self.E]


def test_the_module_is_what_the_gateway_uses() -> None:
    """The MCP tree listing and the endpoint sort with one comparator."""
    from kiro_crew import mcp_dashboard

    assert mcp_dashboard._chat_folder_sort_key("custom") is folder_rank.custom_sort_key
