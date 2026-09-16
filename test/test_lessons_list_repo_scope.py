"""``GET /api/lessons`` names each row's ``repo_scope`` delete selector.

A lesson's identity is the pair ``(rule, repo_scope)``. ``DELETE /api/lessons``
has accepted a ``repo_scope`` selector since the CLI/MCP fix, but the list the
dashboard renders from carried no scope at all -- so two same-rule rows in two
scopes were indistinguishable duplicates in the Memory tab, and the only delete
the UI could send (no selector) removed both (#10651).

The list now answers, per row, the selector that names exactly that row under
the delete route's present-vs-absent semantics:

* ``""``  -- an unscoped (global) row: the route's explicit-global selector.
* fragment -- a scoped row, in canonical form, so it folds back onto the row.
* ``None`` -- a row whose stored scope is present but unusable. Both stores
  keep such a row reachable only through the UNSELECTIVE delete, and the route
  refuses the raw value as a selector, so the client must send none.

Each case is proven by ROUND-TRIPPING the emitted selector into the store's own
delete: it must remove that row and leave the same-rule sibling standing.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers import cron
from kiro_crew.learn import LessonStore
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.asyncio

RULE = "run the gate before pushing"


def _store(tmp_path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    return store


async def _list(vector_store, state) -> list[dict]:
    request = MagicMock()
    request.app = {"state": state}
    request.headers = {"X-Session-Key": "dashboard:ui"}
    request.query = {}
    with (
        patch.object(cron, "_blocks_reads_session", return_value=False),
        patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=(None, None))),
        patch.object(cron, "_prepare_private_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=vector_store)),
        patch.object(cron, "_get_active_workspace", return_value="default"),
    ):
        resp = await cron.api_lessons(request)
    assert resp.status == 200
    return json.loads(resp.text)["lessons"]


def _selectors(rows: list[dict]) -> dict[str | None, str]:
    """``repo_scope`` -> rule, asserting the key is PRESENT on every row."""
    out: dict[str | None, str] = {}
    for row in rows:
        assert "repo_scope" in row, row
        out[row["repo_scope"]] = row["rule"]
    return out


async def test_vector_rows_carry_the_selector_that_names_exactly_that_row(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        # Distinct rule text per row so the writer's dedup cannot merge them, but
        # a SHARED substring so the delete below is the ambiguous case the fix
        # is for: a substring that matches every row, told apart only by scope.
        assert store.write_lesson(f"{RULE} everywhere", "knowledge")
        # Trailing slash: the stored form is canonical, and the emitted selector
        # must be the canonical form too so the two fold identically.
        assert store.write_lesson(f"{RULE} in this repo", "knowledge", repo_scope="src/pkg/")
        # An imported row that bypassed the write surface with a scope the gate
        # can never satisfy: scoped-but-broken, not global.
        assert (
            store.set_semantic(
                "lesson.broken",
                {"rule": f"{RULE} nowhere", "category": "knowledge", "repo_scope": "/"},
                1.0,
                "user_explicit",
            )
            is None
        )

        rows = await _list(store, MagicMock())
        by_scope = _selectors(rows)
        assert by_scope == {
            "": f"{RULE} everywhere",
            "src/pkg": f"{RULE} in this repo",
            None: f"{RULE} nowhere",
        }

        # Round trip: the scoped row's selector removes that row ONLY, even
        # though the substring matches all three.
        assert store.delete_lesson(RULE, "src/pkg") is True
        remaining = _selectors(await _list(store, MagicMock()))
        assert set(remaining) == {"", None}, remaining

        # The global row's selector ("") removes the global row and leaves the
        # broken row, which only the unselective path may claim.
        assert store.delete_lesson(RULE, "") is True
        remaining = _selectors(await _list(store, MagicMock()))
        assert set(remaining) == {None}, remaining
    finally:
        store.close()


async def test_jsonl_rows_carry_the_same_selector_contract(tmp_path) -> None:
    lines = [
        {"ts": "2026-09-15T00:00:00+00:00", "rule": f"{RULE} everywhere", "category": "knowledge"},
        {
            "ts": "2026-09-15T00:00:01+00:00",
            "rule": f"{RULE} in this repo",
            "category": "knowledge",
            "repo_scope": "src/pkg/",
        },
        {
            "ts": "2026-09-15T00:00:02+00:00",
            "rule": f"{RULE} nowhere",
            "category": "knowledge",
            "repo_scope": "/",
        },
    ]
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    state = MagicMock()
    state.lessons = LessonStore(base_dir=tmp_path)

    by_scope = _selectors(await _list(None, state))
    assert by_scope == {
        "": f"{RULE} everywhere",
        "src/pkg": f"{RULE} in this repo",
        None: f"{RULE} nowhere",
    }

    # Round trip through the JSONL store's own remove: the scoped selector takes
    # its row and leaves the global sibling and the broken row.
    assert state.lessons.remove(RULE, "src/pkg") is True
    remaining = _selectors(await _list(None, state))
    assert set(remaining) == {"", None}, remaining
