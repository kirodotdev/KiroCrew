"""``GET /api/lessons`` says how big the population is and pages through it.

The route returns one bounded window (``LESSON_LIST_LIMIT`` rows by default),
and it was the only lesson surface that omitted rows without saying so: the
body was ``{"lessons": [...]}`` and nothing else, ``learn_list`` rendered it
verbatim under a description promising "all", and ``learn_add``'s ``deduped``
outcome sent the model to that list to find a stored lesson that -- being an
older dedup winner whose ``updated_at`` the dedup never bumps -- sat exactly
outside the newest window. The body now carries ``total``, ``truncated`` and
the effective ``limit`` / ``offset``, both tiers honour the window the same
way (``offset`` counts back from the newest row), and ``learn_list`` reports
"showing N of M" with the offset that reaches the next page.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers import cron
from kiro_crew.learn import LessonStore
from kiro_crew.mcp_tools import learn
from kiro_crew.validation import (
    LEARN_LIST_SCHEMA,
    LESSON_LIST_LIMIT,
    LESSON_LIST_LIMIT_MAX,
    LESSON_LIST_OFFSET_MAX,
    MAX_RESPONSE_LEN,
    MCP_CORE_SCHEMAS,
    ValidationError,
    validate_tool_args,
)
from kiro_crew.vector_memory import VectorMemoryStore, create_member_database, open_member_database


def _store(tmp_path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    return store


_WORDS = (
    "alpha bravo charlie",
    "delta echo foxtrot",
    "golf hotel india",
    "juliet kilo lima",
    "mike november oscar",
)


def _seed_vector(store: VectorMemoryStore, n: int) -> list[str]:
    """``n`` lessons oldest to newest, with disjoint vocabularies so the writer's
    substring / topic-overlap dedup keeps every one, and distinct ``updated_at``
    stamps so the recency order is the seeding order and not the clock's
    resolution."""
    rules = [f"prefer {_WORDS[i]}" for i in range(n)]
    for i, rule in enumerate(rules):
        assert store.write_lesson(rule, "knowledge")
        with store._db_lock, store.db:
            store.db.execute(
                "UPDATE semantic_memory SET updated_at = ? WHERE key LIKE 'lesson.%' "
                "AND value_json LIKE ?",
                (f"2026-01-01T00:00:{i:02d}+00:00", f"%{rule}%"),
            )
    return rules


async def _call(
    vector_store,
    state,
    query: dict[str, str] | None = None,
    *,
    blocked: bool = False,
    store_name: str | None = None,
):
    request = MagicMock()
    request.app = {"state": state}
    request.headers = {"X-Session-Key": "dashboard:ui"}
    request.query = dict(query or {})
    with (
        patch.object(cron, "_blocks_reads_session", return_value=blocked),
        patch.object(
            cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=(store_name, None))
        ),
        patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=vector_store)),
        patch.object(
            cron.ContextBuilder, "get_memory_for", return_value=MagicMock(vector_store=vector_store)
        ),
        patch.object(cron, "_get_active_workspace", return_value="default"),
        patch.object(cron, "_sel", return_value=MagicMock()),
    ):
        resp = await cron.api_lessons(request)
    return resp.status, json.loads(resp.text)


def _rules(body: dict) -> list[str]:
    return [row["rule"] for row in body["lessons"]]


def _window(body: dict) -> dict:
    return {k: body[k] for k in ("total", "truncated", "limit", "offset")}


# ── the body carries the population and the window ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("population", ("empty", "undecodable", "past_end"))
async def test_member_sqlite_remains_authority_when_the_page_has_no_rows(
    tmp_path, monkeypatch, population
) -> None:
    from kiro_crew import memory_stores

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "memory_stores")
    path = tmp_path / "memory_stores" / "member-store" / "memory.db"
    create_member_database(path, member_id="member-id", store_id="member-store")
    store = open_member_database(path, member_id="member-id", store_id="member-store")
    try:
        total = {"empty": 0, "undecodable": 1, "past_end": 2}[population]
        rules = [f"prefer {_WORDS[i]}" for i in range(total)]
        for i, rule in enumerate(rules):
            assert store.write_lesson(rule, "knowledge")
            with store._db_lock, store.db:
                store.db.execute(
                    "UPDATE memory_items SET updated_at = ? WHERE key LIKE 'lesson.%' "
                    "AND value_json LIKE ?",
                    (f"2026-01-01T00:00:{i:02d}+00:00", f"%{rule}%"),
                )
        if population == "undecodable":
            with store._db_lock, store.db:
                store.db.execute(
                    "UPDATE memory_items SET value_json = 'not json' WHERE key LIKE 'lesson.%'"
                )
        offset = 10 if population == "past_end" else 0
        with patch.object(
            cron, "_lesson_jsonl_store", side_effect=AssertionError("V2 must not read JSONL")
        ):
            if population == "past_end":
                _, first = await _call(
                    store, MagicMock(), {"limit": "1"}, store_name="member-store"
                )
                assert _rules(first) == rules[-1:]
                assert _window(first) == {"total": 2, "truncated": True, "limit": 1, "offset": 0}
            status, body = await _call(
                store, MagicMock(), {"offset": str(offset)}, store_name="member-store"
            )
        assert status == 200
        assert body["lessons"] == []
        assert _window(body) == {
            "total": total,
            "truncated": total > 0,
            "limit": LESSON_LIST_LIMIT,
            "offset": offset,
        }
        assert not (path.parent / "lessons.jsonl").exists()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_default_window_names_total_and_says_when_nothing_is_missing(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        rules = _seed_vector(store, 3)
        status, body = await _call(store, MagicMock())
        assert status == 200
        # Oldest-first within the window, as before.
        assert _rules(body) == rules
        assert _window(body) == {
            "total": 3,
            "truncated": False,
            "limit": LESSON_LIST_LIMIT,
            "offset": 0,
        }
    finally:
        store.close()


@pytest.mark.asyncio
async def test_vector_tier_pages_back_from_the_newest_row(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        rules = _seed_vector(store, 5)
        # First page: the two NEWEST, oldest-first within the page. The old
        # ``[-50:]`` idiom selected the oldest rows here; the window must not.
        status, body = await _call(store, MagicMock(), {"limit": "2"})
        assert status == 200
        assert _rules(body) == rules[3:5]
        assert _window(body) == {"total": 5, "truncated": True, "limit": 2, "offset": 0}
        # Second page skips those two.
        _, body = await _call(store, MagicMock(), {"limit": "2", "offset": "2"})
        assert _rules(body) == rules[1:3]
        assert _window(body) == {"total": 5, "truncated": True, "limit": 2, "offset": 2}
        # Last page is short and still truncated: the body is not the population.
        _, body = await _call(store, MagicMock(), {"limit": "2", "offset": "4"})
        assert _rules(body) == rules[0:1]
        assert _window(body) == {"total": 5, "truncated": True, "limit": 2, "offset": 4}
        # The three pages together are the whole population, each row once.
        seen: list[str] = []
        for offset in (0, 2, 4):
            _, page = await _call(store, MagicMock(), {"limit": "2", "offset": str(offset)})
            seen.extend(_rules(page))
        assert sorted(seen) == sorted(rules)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_page_past_the_end_keeps_the_vector_total_and_never_reads_jsonl(tmp_path) -> None:
    """The tier fallback is keyed on the vector POPULATION, not on the page.

    An empty page past the end of a populated vector store must answer from
    the vector tier with its true total: falling through to the JSONL file
    would list rows of a superseded tier and report their count as the total.
    """
    store = _store(tmp_path)
    try:
        _seed_vector(store, 2)
        (tmp_path / "lessons.jsonl").write_text(
            json.dumps(
                {"ts": "2026-01-01T00:00:00+00:00", "rule": "a jsonl row", "category": "tool"}
            )
            + "\n",
            encoding="utf-8",
        )
        state = MagicMock()
        state.lessons = LessonStore(base_dir=tmp_path)
        status, body = await _call(store, state, {"offset": "10"})
        assert status == 200
        assert body["lessons"] == []
        assert _window(body) == {
            "total": 2,
            "truncated": True,
            "limit": LESSON_LIST_LIMIT,
            "offset": 10,
        }
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_vector_store_of_only_undecodable_rows_does_not_silence_jsonl(tmp_path) -> None:
    """Tier authority is whether any vector row DECODES, not the raw row count.

    An import or legacy migration can leave ``lesson.*`` rows whose stored JSON
    does not decode. This list drops exactly those rows (and keeps every row
    that decodes, legacy shapes included), so a store holding only such rows
    has answered nothing; keying the fallback on ``count_lessons()`` would
    still pick the vector tier and hide the valid corrections in the JSONL file.
    """
    store = _store(tmp_path)
    try:
        _seed_vector(store, 1)
        with store._db_lock, store.db:
            store.db.execute(
                "UPDATE semantic_memory SET value_json = 'not json' WHERE key LIKE 'lesson.%'"
            )
        assert store.count_lessons() == 1 and not store.has_any_decodable_lesson()
        (tmp_path / "lessons.jsonl").write_text(
            json.dumps(
                {"ts": "2026-01-01T00:00:00+00:00", "rule": "a jsonl row", "category": "tool"}
            )
            + "\n",
            encoding="utf-8",
        )
        state = MagicMock()
        state.lessons = LessonStore(base_dir=tmp_path)
        status, body = await _call(store, state)
        assert status == 200
        assert _rules(body) == ["a jsonl row"]
        assert _window(body) == {
            "total": 1,
            "truncated": False,
            "limit": LESSON_LIST_LIMIT,
            "offset": 0,
        }
    finally:
        store.close()


@pytest.mark.asyncio
async def test_jsonl_tier_pages_from_its_tail_under_the_same_contract(tmp_path) -> None:
    lines = [
        {"ts": f"2026-01-01T00:00:{i:02d}+00:00", "rule": f"jsonl rule {i}", "category": "tool"}
        for i in range(4)
    ]
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    state = MagicMock()
    state.lessons = LessonStore(base_dir=tmp_path)

    _, body = await _call(None, state)
    assert _rules(body) == [f"jsonl rule {i}" for i in range(4)]
    assert _window(body) == {
        "total": 4,
        "truncated": False,
        "limit": LESSON_LIST_LIMIT,
        "offset": 0,
    }
    # ``load_all()`` is append order, so the newest rows are the TAIL and the
    # window ends ``offset`` rows before it -- the same direction the vector
    # tier pages in.
    _, body = await _call(None, state, {"limit": "3"})
    assert _rules(body) == ["jsonl rule 1", "jsonl rule 2", "jsonl rule 3"]
    assert _window(body) == {"total": 4, "truncated": True, "limit": 3, "offset": 0}
    _, body = await _call(None, state, {"limit": "3", "offset": "3"})
    assert _rules(body) == ["jsonl rule 0"]
    assert _window(body) == {"total": 4, "truncated": True, "limit": 3, "offset": 3}
    _, body = await _call(None, state, {"offset": "9"})
    assert body["lessons"] == []
    assert body["total"] == 4 and body["truncated"] is True


@pytest.mark.asyncio
async def test_the_window_is_clamped_and_echoed_and_a_non_integer_is_refused(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        _seed_vector(store, 1)
        # Clamped, not refused: the body names the window actually served.
        _, body = await _call(store, MagicMock(), {"limit": "999999", "offset": "-4"})
        assert body["limit"] == LESSON_LIST_LIMIT_MAX and body["offset"] == 0
        _, body = await _call(store, MagicMock(), {"limit": "0"})
        assert body["limit"] == 1
        # An offset past SQLite's 64-bit range never reaches the bound
        # parameter (where it would raise OverflowError, a 500): it is clamped
        # to the shared ceiling and served as the empty page it means.
        for huge in ("9223372036854775808", str(2**80)):
            status, body = await _call(store, MagicMock(), {"offset": huge})
            assert status == 200, huge
            assert body["lessons"] == []
            assert body["offset"] == LESSON_LIST_OFFSET_MAX
            assert body["total"] == 1
        # A page the caller cannot tell apart from the one it asked for is not
        # served: refuse instead of defaulting.
        for query in ({"limit": "ten"}, {"offset": "1.5"}, {"limit": ""}):
            status, body = await _call(store, MagicMock(), query)
            assert status == 400, query
            assert body["code"] == "invalid_pagination"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_blocked_session_gets_the_same_shape(tmp_path) -> None:
    """A temporary session still reads an empty list, in the paged shape, so a
    client parsing the body never meets a key that is sometimes absent."""
    status, body = await _call(None, MagicMock(), {"limit": "7"}, blocked=True)
    assert status == 200
    assert body == {"lessons": [], "total": 0, "truncated": False, "limit": 7, "offset": 0}


# ── the store's bounded read honours the offset ──────────────────────────────


def test_store_offset_skips_the_newest_rows_and_the_unbounded_read_ignores_it(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        rules = _seed_vector(store, 4)

        def rule_of(row: dict) -> str:
            return json.loads(row["value_json"])["rule"]

        assert [rule_of(r) for r in store.get_lessons(2)] == [rules[3], rules[2]]
        assert [rule_of(r) for r in store.get_lessons(2, 2)] == [rules[1], rules[0]]
        assert store.get_lessons(2, 4) == []
        # The unbounded read is the whole population whatever offset says: its
        # callers score over everything, and an offset would silently drop rows
        # from a dedup or contradiction scan.
        assert len(store.get_lessons(None, 3)) == 4
        assert len(store.get_lessons(0, 3)) == 4
        assert store.count_lessons() == 4
    finally:
        store.close()


# ── learn_list tells the model what it is not seeing ─────────────────────────


def _rows(n: int, start: int = 0) -> list[dict]:
    return [
        {"rule": f"rule {i}", "category": "tool", "repo_scope": ""} for i in range(start, start + n)
    ]


def test_learn_list_forwards_only_a_named_window() -> None:
    with patch.object(learn.mcp_core, "_get", return_value={"lessons": []}) as get:
        learn.learn_list("learn_list", {})
        learn.learn_list("learn_list", {"limit": 20})
        learn.learn_list("learn_list", {"offset": 50, "limit": 25})
        # A bool is not a window even though it is an int to Python.
        learn.learn_list("learn_list", {"limit": True})
    assert [c.args[0] for c in get.call_args_list] == [
        "/api/lessons",
        "/api/lessons?limit=20",
        "/api/lessons?limit=25&offset=50",
        "/api/lessons",
    ]


def test_learn_list_reports_showing_n_of_m_with_the_next_offset() -> None:
    body = {"lessons": _rows(2), "total": 7, "truncated": True, "limit": 2, "offset": 0}
    with patch.object(learn.mcp_core, "_get", return_value=body):
        text = learn.learn_list("learn_list", {"limit": 2})
    assert text.splitlines() == [
        "Showing 2 of 7 lessons; 5 older not shown -- pass offset=2 to list them.",
        "[tool] rule 0",
        "[tool] rule 1",
    ]


def test_learn_list_advances_a_short_page_by_the_window_the_store_consumed() -> None:
    # The route drops a row whose stored JSON does not decode, so the store
    # skipped 3 rows for this page while only 2 came back. Both the older count
    # and the next offset advance by limit, not by shown: 9 - 3 - 3 = 3 older,
    # next offset 6. Counting by 2 would claim 4 older and re-read this page's
    # tail instead of reaching them.
    body = {"lessons": _rows(2), "total": 9, "truncated": True, "limit": 3, "offset": 3}
    with patch.object(learn.mcp_core, "_get", return_value=body):
        text = learn.learn_list("learn_list", {"limit": 3, "offset": 3})
    assert text.splitlines()[0] == (
        "Showing 2 of 9 lessons; 3 older not shown -- pass offset=6 to list them."
    )
    # A body with no usable limit (an older gateway) falls back to the rows shown.
    body = {"lessons": _rows(2), "total": 9, "offset": 3}
    with patch.object(learn.mcp_core, "_get", return_value=body):
        text = learn.learn_list("learn_list", {"offset": 3})
    assert text.splitlines()[0] == (
        "Showing 2 of 9 lessons; 4 older not shown -- pass offset=5 to list them."
    )


def test_learn_list_on_the_last_page_states_the_count_without_a_next_offset() -> None:
    # offset 5 + 2 shown == total: nothing older remains, and the rows not shown
    # are the newer ones the caller skipped on purpose.
    body = {"lessons": _rows(2, 5), "total": 7, "truncated": True, "limit": 2, "offset": 5}
    with patch.object(learn.mcp_core, "_get", return_value=body):
        text = learn.learn_list("learn_list", {"limit": 2, "offset": 5})
    assert text.splitlines()[0] == "Showing 2 of 7 lessons."
    assert "offset=" not in text


def test_learn_list_past_the_end_does_not_claim_the_store_is_empty() -> None:
    body = {"lessons": [], "total": 7, "truncated": True, "limit": 50, "offset": 50}
    with patch.object(learn.mcp_core, "_get", return_value=body):
        assert learn.learn_list("learn_list", {"offset": 50}) == "Showing 0 of 7 lessons."


def test_learn_list_refuses_a_page_the_response_cap_would_cut() -> None:
    # sanitize_response drops the TAIL past MAX_RESPONSE_LEN and the header is
    # the head, so a page that renders past the cap would claim "Showing N"
    # and then lose rows the model never sees. The page is refused whole and
    # a limit that fits is named; the reply itself stays under the cap.
    rows = [{"rule": "x" * 500, "category": "tool", "repo_scope": ""} for _ in range(300)]
    body = {"lessons": rows, "total": 300, "truncated": False, "limit": 300, "offset": 0}
    with patch.object(learn.mcp_core, "_get", return_value=body):
        text = learn.learn_list("learn_list", {"limit": 300})
    assert text.startswith("This page of 300 lessons renders to ")
    assert f"past the {MAX_RESPONSE_LEN}-character tool response cap" in text
    assert "so none of it is shown. Pass limit=" in text
    fits = int(text.split("Pass limit=")[1].split()[0])
    assert 1 <= fits < 300
    assert "xxxx" not in text and "Showing" not in text
    assert len(text) < MAX_RESPONSE_LEN
    # The same rows in a page that fits render whole.
    body = {"lessons": rows[:fits], "total": 300, "truncated": True, "limit": fits, "offset": 0}
    with patch.object(learn.mcp_core, "_get", return_value=body):
        text = learn.learn_list("learn_list", {"limit": fits})
    assert text.count("[tool] " + "x" * 500) == fits
    assert len(text) <= MAX_RESPONSE_LEN


def test_learn_list_is_silent_about_the_window_when_it_holds_everything() -> None:
    body = {"lessons": _rows(3), "total": 3, "truncated": False, "limit": 50, "offset": 0}
    with patch.object(learn.mcp_core, "_get", return_value=body):
        text = learn.learn_list("learn_list", {})
    assert text.splitlines() == ["[tool] rule 0", "[tool] rule 1", "[tool] rule 2"]
    # An older gateway that sends no total has nothing truthful to add.
    with patch.object(learn.mcp_core, "_get", return_value={"lessons": _rows(2)}):
        assert learn.learn_list("learn_list", {}).splitlines() == ["[tool] rule 0", "[tool] rule 1"]
    with patch.object(learn.mcp_core, "_get", return_value={"lessons": []}):
        assert learn.learn_list("learn_list", {}) == "No lessons saved."


def test_learn_list_advertises_the_window_and_no_longer_promises_all() -> None:
    (tool,) = [s for s in learn.schemas() if s["name"] == "learn_list"]
    assert "all" not in tool["description"].lower().split()
    props = tool["inputSchema"]["properties"]
    assert props["limit"]["maximum"] == LESSON_LIST_LIMIT_MAX
    assert props["limit"]["minimum"] == 1
    assert props["offset"]["minimum"] == 0
    assert props["offset"]["maximum"] == LESSON_LIST_OFFSET_MAX
    assert str(LESSON_LIST_LIMIT) in props["limit"]["description"]


def test_learn_list_arguments_are_validated_against_the_routes_own_bounds() -> None:
    assert MCP_CORE_SCHEMAS["learn_list"] is LEARN_LIST_SCHEMA
    assert validate_tool_args({}, LEARN_LIST_SCHEMA) == {}
    assert validate_tool_args({"limit": 10, "offset": 20}, LEARN_LIST_SCHEMA) == {
        "limit": 10,
        "offset": 20,
    }
    for bad in (
        {"limit": 0},
        {"limit": LESSON_LIST_LIMIT_MAX + 1},
        {"offset": -1},
        {"offset": LESSON_LIST_OFFSET_MAX + 1},
        {"offset": 2**63},
        {"limit": True},
        {"limit": "10"},
        {"page": 2},
    ):
        with pytest.raises(ValidationError):
            validate_tool_args(bad, LEARN_LIST_SCHEMA)


def test_deduped_outcome_tells_the_model_the_list_pages() -> None:
    """The one instructed workflow that routes through the list: an older
    dedup winner sits outside the newest window, so the instruction must say
    where else to look."""
    body = {"ok": False, "outcome": "deduped", "reason": "substring", "superseded": []}
    with (
        patch.object(learn.mcp_core, "_vet_memory_writes_governance", return_value=None),
        patch.object(learn.mcp_core, "_post", return_value=body),
    ):
        text = learn.learn_add("learn_add", {"rule": "always run the gate", "category": "tool"})
    assert "run learn_list" in text
    assert "offset" in text and "not shown" in text
