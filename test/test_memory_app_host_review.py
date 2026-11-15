"""Dreaming's review queue and the member ``SOUL.md`` read behind the memory app host.

Accepting a Dreaming proposal re-reads every row it cites and refuses a stale one without
taking it off the list; the queue serves Dreaming's proposals only. ``SOUL.md`` is read
through the essentials builder's contained, no-follow, size-capped read and redacted.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from conftest import make_escaping_link, requires_symlinks
from kiro_crew import memory_dream, memory_edit
from kiro_crew import memory_kind_routing as kind_log
from kiro_crew.dashboard.handlers import memory_app_host as host
from kiro_crew.dashboard.handlers._shared import _redact_memory_field
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from kiro_crew.vector_memory import VectorMemoryStore

_STAMP = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).isoformat()


@pytest.fixture
def store(tmp_path):
    opened = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=4)
    opened.init()
    yield opened
    opened.close()


def _lesson(store: VectorMemoryStore, key: str, rule: str) -> None:
    with store._db_lock:
        store.db.execute(
            "INSERT INTO semantic_memory (key, value_json, confidence, source, created_at, "
            "updated_at, is_deleted) VALUES (?, ?, 0.5, 'consolidation', ?, ?, 0)",
            (key, json.dumps({"rule": rule, "applies": "on_topic"}), _STAMP, _STAMP),
        )
        store.db.commit()


def _forget_row(store: VectorMemoryStore, key: str) -> None:
    with store._db_lock:
        store.db.execute("UPDATE semantic_memory SET is_deleted = 1 WHERE key = ?", (key,))
        store.db.commit()


def _propose(store: VectorMemoryStore, entry: dict) -> str:
    with store._db_lock:
        kind_log._upsert(store.db, memory_dream.PROPOSALS_KEY, json.dumps([entry]), _STAMP)
        store.db.commit()
    return host._dream_id(entry)


def _listed(store: VectorMemoryStore) -> list[dict]:
    with store._db_lock:
        return memory_dream.read_proposals(store.db)


_MERGE = {
    "run": "r1",
    "kind": "near_duplicate",
    "keys": ["lesson.keep", "lesson.copy"],
    "changes": ["lesson.copy"],
    "gate": "pass",
    "seen": {"lesson.keep": _STAMP, "lesson.copy": _STAMP},
}


def _correct_row(store: VectorMemoryStore, key: str, rule: str) -> None:
    later = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc).isoformat()
    with store._db_lock:
        store.db.execute(
            "UPDATE semantic_memory SET value_json = ?, updated_at = ? WHERE key = ?",
            (json.dumps({"rule": rule, "applies": "on_topic"}), later, key),
        )
        store.db.commit()


@pytest.fixture
def edits(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        host, "_edit", lambda tier, s, k, ids, op, *_fence: seen.append((ids, op)) or 1
    )
    return seen


def test_accepting_a_merge_whose_keeper_is_gone_is_refused_and_stays_listed(store, edits):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)
    _forget_row(store, "lesson.keep")

    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", dream_id, "accept")

    assert refused.value.code == "review_item_stale" and refused.value.status == 409
    assert edits == []
    assert [host._dream_id(p) for p in _listed(store)] == [dream_id]


def test_accepting_a_merge_whose_folded_row_was_corrected_is_refused(store, edits):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)
    _correct_row(store, "lesson.copy", "Run the tests first, then lint.")

    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", dream_id, "accept")

    assert refused.value.code == "review_item_stale" and edits == []
    assert [host._dream_id(p) for p in _listed(store)] == [dream_id]


def test_a_proposal_without_its_proposal_time_rows_cannot_be_accepted(store, edits):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, {k: v for k, v in _MERGE.items() if k != "seen"})

    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", dream_id, "accept")

    assert refused.value.code == "review_item_stale" and edits == []


def test_rejecting_a_stale_proposal_takes_it_off_the_list(store, edits):
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)

    assert host._resolve(store, "", b"k", dream_id, "reject") == "rejected"
    assert edits == [] and _listed(store) == []


def test_accepting_a_live_merge_forgets_only_the_folded_row(store, edits):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)

    assert host._resolve(store, "", b"k", dream_id, "accept") == "accepted"
    assert edits == [([("directive", "lesson.copy")], {"type": "forget"})]
    assert _listed(store) == []


@pytest.mark.parametrize("kind", ["uncertain_merge", "legacy_tier"])
def test_accepting_a_string_lesson_rewrite_is_refused_and_stays_listed(store, edits, kind):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    with store._db_lock:
        store.db.execute(
            "UPDATE semantic_memory SET value_json = ? WHERE key = ?",
            (json.dumps("Run the tests."), "lesson.keep"),
        )
        store.db.commit()
        before = host._live_semantic(store.db, _MERGE["keys"])
    entry = dict(_MERGE, kind=kind, text="Run the tests first, then lint.")
    if kind == "legacy_tier":
        entry["changes"] = ["lesson.copy", "lesson.keep"]
    dream_id = _propose(store, entry)

    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", dream_id, "accept")

    assert refused.value.code == "review_item_unsupported"
    assert refused.value.status == 409
    assert edits == []
    with store._db_lock:
        assert host._live_semantic(store.db, _MERGE["keys"]) == before
    assert _listed(store) == [entry]
    assert [item["id"] for item in host._dream_items(store)] == [dream_id]
    assert host._resolve(store, "", b"k", dream_id, "reject") == "rejected"
    assert edits == [] and _listed(store) == []


def test_an_edit_that_fails_leaves_the_proposal_listed(store, monkeypatch):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)

    def refuse(*_args, **_kwargs):
        raise memory_edit.MemoryEditError("Stale preview.", "stale_preview", 409)

    monkeypatch.setattr(host, "_edit", refuse)
    with pytest.raises(memory_edit.MemoryEditError):
        host._resolve(store, "", b"k", dream_id, "accept")

    assert [host._dream_id(p) for p in _listed(store)] == [dream_id]


@pytest.mark.parametrize("late", ["corrected", "keeper_forgotten"])
def test_a_change_landing_after_the_check_is_never_applied_over(store, monkeypatch, late):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)
    real_refresh = memory_edit.refresh_records

    def change_then_refresh(tier, items, **kwargs):
        if late == "corrected":
            _correct_row(store, "lesson.copy", "Run the tests first, then lint.")
        else:
            _forget_row(store, "lesson.keep")
        return real_refresh(tier, items, **kwargs)

    monkeypatch.setattr(memory_edit, "refresh_records", change_then_refresh)
    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", dream_id, "accept")

    assert refused.value.code == "review_item_stale"
    with store._db_lock:
        assert set(host._live_semantic(store.db, ["lesson.copy"])) == {"lesson.copy"}
    assert [host._dream_id(p) for p in _listed(store)] == [dream_id]


@pytest.mark.parametrize("late", ["forgotten", "corrected"])
def test_a_keeper_another_process_changes_before_the_apply_stops_the_merge(
    store, monkeypatch, late
):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)
    real_preview = memory_edit.preview_edit

    def preview_then_forget(*args, **kwargs):
        # After the gateway's own check: a write from outside its lock (the CLI).
        preview = real_preview(*args, **kwargs)
        if late == "forgotten":
            _forget_row(store, "lesson.keep")
        else:
            _correct_row(store, "lesson.keep", "Run the tests, then deploy.")
        return preview

    monkeypatch.setattr(memory_edit, "preview_edit", preview_then_forget)
    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", dream_id, "accept")

    assert refused.value.code == "review_item_stale"
    with store._db_lock:
        assert set(host._live_semantic(store.db, ["lesson.copy"])) == {"lesson.copy"}
    assert [host._dream_id(p) for p in _listed(store)] == [dream_id]


@pytest.mark.parametrize("item_id", ["conflict:1", "replaced:ep1", "dream:missing"])
def test_only_a_listed_dreaming_proposal_resolves(store, edits, item_id):
    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", item_id, "accept")
    assert refused.value.status == 404 and edits == []


# ── SOUL.md ──────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A configured member workspace: the essentials read admits a ``SOUL.md`` in it."""
    from kiro_crew import member_essential_context as essentials

    real_table = essentials._managed_root_table

    def table(**kwargs):
        roots, workspaces, resolved = real_table(**kwargs)
        return roots, [*workspaces, tmp_path], {**resolved, tmp_path: tmp_path.resolve()}

    monkeypatch.setattr(essentials, "_managed_root_table", table)
    return tmp_path


def test_a_save_refused_over_budget_never_puts_back_over_a_later_save(workspace, monkeypatch):
    path = workspace / "SOUL.md"
    path.write_text("old", encoding="utf-8")
    checking, release = threading.Event(), threading.Event()

    def refusal(_request, _member, _slug, _directory):
        if path.read_text(encoding="utf-8") != "too long":
            return ""
        checking.set()
        assert release.wait(FALLBACK_TIMEOUT_SECS)
        return "over"

    monkeypatch.setattr(host, "_essentials_refusal", refusal)
    results: dict[str, object] = {}

    def save(name: str, text: str) -> None:
        results[name] = host._save_soul(None, "Ada", "ada", workspace, path, text)

    first = threading.Thread(target=save, args=("first", "too long"))
    first.start()
    assert checking.wait(FALLBACK_TIMEOUT_SECS)
    second = threading.Thread(target=save, args=("second", "new"))
    second.start()
    release.set()
    first.join(FALLBACK_TIMEOUT_SECS)
    second.join(FALLBACK_TIMEOUT_SECS)

    assert results == {"first": ("over", "soul_over_budget", 413), "second": None}
    assert path.read_text(encoding="utf-8") == "new"


def test_a_missing_soul_reads_as_empty(workspace):
    assert host._read_soul(workspace / "SOUL.md", workspace) == ""


@pytest.mark.parametrize("target", ["outside", pytest.param("/dev/zero", marks=requires_symlinks)])
def test_a_soul_link_leaving_the_workspace_is_refused(workspace, tmp_path_factory, target):
    if target == "outside":
        outside = tmp_path_factory.mktemp("elsewhere")
        (outside / "secret.py").write_text("not for the app", encoding="utf-8")
        path = workspace / make_escaping_link(workspace, outside)
    else:
        path = workspace / "SOUL.md"
        path.symlink_to(target)

    with pytest.raises(ValueError):
        host._read_soul(path, workspace)


def test_a_soul_over_the_cap_is_refused(workspace):
    (workspace / "SOUL.md").write_text("x" * (host.SOUL_MAX_CHARS + 1), encoding="utf-8")
    with pytest.raises(host._SoulTooLarge):
        host._read_soul(workspace / "SOUL.md", workspace)


def test_a_member_on_the_unmapped_default_workspace_has_its_soul_folder(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.loader_locations import workspace_dir_from_entry

    def config(workspace):
        agent = SimpleNamespace(member_id="ada", workspace=workspace)
        return SimpleNamespace(agents={"Ada": agent}, workspaces={}, default_workspace="default")

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: config("default")))
    assert host._member_workspace_dir("ada") == ("Ada", workspace_dir_from_entry(None))
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: config("elsewhere")))
    assert host._member_workspace_dir("ada") is None


@pytest.mark.asyncio
async def test_the_soul_read_response_is_redacted(workspace, monkeypatch):
    secret = "sk-ant-api03-" + "a" * 90
    assert _redact_memory_field(secret) != secret  # the precondition this test needs
    (workspace / "SOUL.md").write_text(f"Be kind.\n{secret}\n", encoding="utf-8")

    async def owner(request, operation):
        return None

    monkeypatch.setattr(host, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(host, "_member_workspace_dir", lambda slug: ("Ada", workspace))
    app = web.Application()
    app["state"] = SimpleNamespace()
    request = make_mocked_request(
        "GET", "/api/members/ada/soul", app=app, match_info={"slug": "ada"}
    )

    response = await host.api_member_soul(request)

    assert isinstance(response, web.Response) and response.text is not None
    body = json.loads(response.text)
    assert response.status == 200 and body["member"] == "Ada"
    assert secret not in body["text"] and body["text"].startswith("Be kind.")


@pytest.mark.asyncio
async def test_a_soul_holding_a_secret_is_not_replaced_by_its_masked_copy(workspace, monkeypatch):
    secret = "sk-ant-api03-" + "b" * 90
    original = f"Be kind.\n{secret}\n"
    (workspace / "SOUL.md").write_text(original, encoding="utf-8")

    async def owner(request, operation):
        return None

    async def body(request, max_bytes):
        return {"text": _redact_memory_field(original)}, None

    monkeypatch.setattr(host, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(host, "read_bounded_json", body)
    monkeypatch.setattr(host, "_member_workspace_dir", lambda slug: ("Ada", workspace))
    app = web.Application()
    app["state"] = SimpleNamespace()
    request = make_mocked_request(
        "PUT", "/api/members/ada/soul", app=app, match_info={"slug": "ada"}
    )

    response = await host.api_member_soul(request)

    assert response.status == 409
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_a_soul_no_editor_could_show_is_not_overwritten(workspace, monkeypatch):
    # Over the editor's cap, so its GET answers 413: a save must not count it as empty.
    original = "x" * (host.SOUL_MAX_CHARS + 1)
    (workspace / "SOUL.md").write_text(original, encoding="utf-8")

    async def owner(request, operation):
        return None

    async def body(request, max_bytes):
        return {"text": "Be kind."}, None

    monkeypatch.setattr(host, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(host, "read_bounded_json", body)
    monkeypatch.setattr(host, "_member_workspace_dir", lambda slug: ("Ada", workspace))
    app = web.Application()
    app["state"] = SimpleNamespace()
    request = make_mocked_request(
        "PUT", "/api/members/ada/soul", app=app, match_info={"slug": "ada"}
    )

    response = await host.api_member_soul(request)

    assert isinstance(response, web.Response) and response.status == 409
    assert json.loads(response.text or "{}")["code"] == "document_unchecked"
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == original


# ── accepting with the real bulk edit ───────────────────────────────────────


def _value(store: VectorMemoryStore, key: str) -> dict | None:
    with store._db_lock:
        row = store.db.execute(
            "SELECT value_json FROM semantic_memory WHERE key = ? AND is_deleted = 0", (key,)
        ).fetchone()
    return json.loads(row["value_json"]) if row else None


def _written(store: VectorMemoryStore, key: str, rule: str, applies: str) -> str:
    """Write a lesson the way Crew does (with its record metadata); its ``updated_at``."""
    refused = store.set_semantic(
        key, {"rule": rule, "applies": applies}, 0.9, "consolidation", defer_embedding=True
    )
    assert refused is None
    with store._db_lock:
        row = store.db.execute(
            "SELECT updated_at FROM semantic_memory WHERE key = ?", (key,)
        ).fetchone()
    return str(row["updated_at"])


def test_accepting_a_tier_move_and_a_merged_sentence_edits_the_rows(store):
    seen = {
        "lesson.keep": _written(store, "lesson.keep", "Run the tests.", "on_topic"),
        "lesson.copy": _written(store, "lesson.copy", "Run the tests first.", "on_topic"),
        "lesson.wide": _written(store, "lesson.wide", "Always lint.", "always"),
    }
    tier_move = {
        "run": "r2",
        "kind": "legacy_tier",
        "keys": ["lesson.wide"],
        "changes": ["lesson.wide"],
        "seen": {"lesson.wide": seen["lesson.wide"]},
    }
    merge = dict(
        _MERGE,
        kind="uncertain_merge",
        text="  Run the tests first, every time.  ",
        seen={k: seen[k] for k in ("lesson.keep", "lesson.copy")},
    )
    with store._db_lock:
        entries = json.dumps([tier_move, merge])
        kind_log._upsert(store.db, memory_dream.PROPOSALS_KEY, entries, _STAMP)
        store.db.commit()

    assert host._resolve(store, "", b"k", host._dream_id(tier_move), "accept") == "accepted"
    assert (_value(store, "lesson.wide") or {})["applies"] == "on_topic"
    assert host._resolve(store, "", b"k", host._dream_id(merge), "accept") == "accepted"
    assert (_value(store, "lesson.keep") or {})["rule"] == "Run the tests first, every time."
    assert _value(store, "lesson.copy") is None and _listed(store) == []
    # A selection that names no live row changes nothing.
    assert host._edit(store, "", b"k", [("directive", "lesson.copy")], {"type": "forget"}) == 0


def test_accepting_a_rewrite_of_a_lesson_that_is_not_json_is_refused(store, edits):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    with store._db_lock:
        store.db.execute(
            "UPDATE semantic_memory SET value_json = '{broken' WHERE key = 'lesson.keep'"
        )
        store.db.commit()
    dream_id = _propose(store, dict(_MERGE, kind="uncertain_merge", text="Run them."))

    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", dream_id, "accept")

    assert refused.value.code == "review_item_unsupported" and edits == []
    assert [host._dream_id(p) for p in _listed(store)] == [dream_id]


def test_the_queue_shows_what_each_kind_of_proposal_would_change(store):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    with store._db_lock:
        store.db.execute(
            "INSERT INTO episodic_memories (id, text, created_at) VALUES ('ep1', 'met Ada', ?)",
            (_STAMP,),
        )
        store.db.commit()
    entries = [
        {"run": "r1", "kind": "work_state", "keys": ["lesson.copy"], "changes": ["lesson.copy"]},
        dict(_MERGE, kind="uncertain_merge", text="Run the tests first, every time."),
        {"run": "r1", "kind": "numbered_family", "keys": ["lesson.keep", 7]},
    ]
    with store._db_lock:
        kind_log._upsert(store.db, memory_dream.PROPOSALS_KEY, json.dumps(entries), _STAMP)
        store.db.commit()

    expire, merge, family = host._dream_items(store)

    assert (expire["op"], expire["record_id"], expire["after"]) == ("expire", "lesson.copy", None)
    assert expire["before"] == "Run the tests first." and expire["tier"] == "on_topic"
    assert (merge["op"], merge["after"]) == ("merge", "Run the tests first, every time.")
    assert merge["related"] == ["lesson.copy"] and merge["kind"] == "directive"
    assert (family["op"], family["related"]) == ("group", [])
    assert host._queue_counts(store) == {"dreams": 3, "never_recalled": 1}


@pytest.mark.parametrize(
    ("kind", "snapshot", "text"),
    [
        ("fact", None, ""),
        ("episode", {"text": "met Ada"}, "met Ada"),
        ("fact", {"value_json": "{broken"}, "{broken"),
        ("fact", {"value_json": json.dumps({"rule": "lint"})}, "lint"),
        ("fact", {"value_json": json.dumps({"value": "Mandarin"})}, "Mandarin"),
        ("fact", {"value_json": json.dumps({"n": 1})}, '{"n": 1}'),
        ("fact", {"value_json": json.dumps("plain")}, "plain"),
        ("fact", {"value_json": json.dumps([1, 2])}, "[1, 2]"),
    ],
)
def test_a_stored_row_reads_as_its_rule_text_or_json(kind, snapshot, text):
    assert host._record_text(kind, snapshot) == text


def test_a_lesson_tier_and_a_stamp_read_leniently():
    assert host._lesson_tier("{broken") is None and host._lesson_tier('"x"') is None
    assert host._lesson_tier({"applies": "always"}) == "always"
    assert host._lesson_tier({"applies": "sometimes"}) == "unstated"
    assert host._iso_to_epoch(None) is None and host._iso_to_epoch("not a date") is None
    naive = datetime(2026, 10, 1, 12, 0)
    assert host._iso_to_epoch(naive.isoformat()) == naive.replace(tzinfo=timezone.utc).timestamp()
    assert host._iso_to_epoch("2026-10-01T12:00:00Z") == datetime.fromisoformat(_STAMP).timestamp()


# ── the review routes ───────────────────────────────────────────────────────


@pytest.fixture
def routes(monkeypatch, store):
    """The review routes with the owner gate open and *store* as the addressed tier."""
    from kiro_crew.dashboard.handlers import _shared
    from kiro_crew.dashboard.handlers import memory as memory_handlers

    gates: dict = {"owner": None, "store": None, "write": None, "tier": store}
    audited: list[tuple] = []

    async def owner(request, operation):
        return gates["owner"]

    async def admin_store(request, operation):
        return "", gates["store"]

    async def tier_for(state, name):
        return gates["tier"]

    async def write_gate(state, request, operation, *, store=None):
        return gates["write"]

    async def audit(request, operation, outcome, detail):
        audited.append((operation, outcome, detail))

    monkeypatch.setattr(host, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(host, "_admin_store", admin_store)
    monkeypatch.setattr(host, "vector_memory_for_store", tier_for)
    monkeypatch.setattr(memory_handlers, "_memory_write_gate", write_gate)
    monkeypatch.setattr(_shared, "_audit", audit)
    return SimpleNamespace(gates=gates, audited=audited)


class _Stream:
    """A request payload that yields *data* once, as aiohttp's reader does."""

    def __init__(self, data: bytes) -> None:
        self._chunks = [data] if data else []

    async def readany(self) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def at_eof(self) -> bool:
        return not self._chunks

    async def iter_chunked(self, size: int):
        while self._chunks:
            yield self._chunks.pop(0)


def _route_request(method: str, path: str, match_info: dict | None = None, body=None):
    app = web.Application()
    app["state"] = SimpleNamespace()
    payload = json.dumps(body).encode() if body is not None else b""
    return make_mocked_request(
        method,
        path,
        app=app,
        match_info=match_info or {},
        headers={"Content-Type": "application/json"} if body is not None else {},
        payload=_Stream(payload),
    )


def _code(response: web.Response) -> str:
    return json.loads(response.text or "{}").get("code", "")


@pytest.mark.asyncio
async def test_the_review_list_serves_dreaming_proposals_redacted(routes, store):
    secret = "sk-ant-api03-" + "c" * 90
    _lesson(store, "lesson.keep", f"Use {secret}.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    _propose(store, _MERGE)

    listed = await host.api_memory_review(_route_request("GET", "/api/memory/review"))
    assert listed.status == 200 and secret not in (listed.text or "")
    body = json.loads(listed.text or "{}")
    assert body["store"] == "default" and [i["queue"] for i in body["items"]] == ["dreams"]

    other = await host.api_memory_review(_route_request("GET", "/api/memory/review?queue=x"))
    assert other.status == 400 and _code(other) == "invalid_review_queue"


@pytest.mark.asyncio
async def test_the_review_list_answers_each_refusal_as_is(routes, monkeypatch):
    def request():
        return _route_request("GET", "/api/memory/review")

    routes.gates["owner"] = web.json_response({"code": "owner_only"}, status=403)
    assert (await host.api_memory_review(request())).status == 403
    routes.gates["owner"], routes.gates["store"] = None, web.json_response({}, status=404)
    assert (await host.api_memory_review(request())).status == 404
    routes.gates["store"], routes.gates["tier"] = None, None
    assert _code(await host.api_memory_review(request())) == "store_unavailable"
    routes.gates["tier"] = SimpleNamespace()

    def broken(tier):
        raise OSError("disk gone")

    monkeypatch.setattr(host, "_dream_items", broken)
    unavailable = await host.api_memory_review(request())
    assert unavailable.status == 503 and _code(unavailable) == "store_unavailable"


@pytest.mark.asyncio
async def test_resolving_a_proposal_applies_the_decision_and_audits_it(routes, store):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)
    path = f"/api/memory/review/{dream_id}"

    rejected = await host.api_memory_review_resolve(
        _route_request("POST", path, {"id": dream_id}, {"decision": "reject"})
    )

    assert rejected.status == 200 and json.loads(rejected.text or "{}") == {"outcome": "rejected"}
    assert _listed(store) == []
    assert routes.audited == [("memory.review.resolve", "success", f"default:{dream_id}:reject")]
    gone = await host.api_memory_review_resolve(
        _route_request("POST", path, {"id": dream_id}, {"decision": "accept"})
    )
    assert gone.status == 404 and _code(gone) == "review_item_gone"
    assert _value(store, "lesson.copy") is not None


@pytest.mark.asyncio
async def test_resolving_answers_each_refusal_before_touching_the_queue(routes, store, monkeypatch):
    def post(body):
        return host.api_memory_review_resolve(
            _route_request("POST", "/api/memory/review/dream:x", {"id": "dream:x"}, body)
        )

    routes.gates["owner"] = web.json_response({}, status=403)
    assert (await post({"decision": "accept"})).status == 403
    routes.gates["owner"] = None
    assert (await post(None)).status == 400  # no body at all
    routes.gates["store"] = web.json_response({}, status=404)
    assert (await post({"decision": "accept"})).status == 404
    routes.gates["store"], routes.gates["write"] = None, web.json_response({}, status=403)
    assert (await post({"decision": "accept"})).status == 403
    routes.gates["write"] = None
    invalid = await post({"decision": "maybe"})
    assert invalid.status == 400 and _code(invalid) == "invalid_review_decision"
    routes.gates["tier"] = None
    assert _code(await post({"decision": "accept"})) == "store_unavailable"
    routes.gates["tier"] = store
    for raised, status, code in (
        (ValueError("bad id"), 404, "review_item_unknown"),
        (OSError("disk gone"), 503, "store_unavailable"),
    ):

        def fail(*_args, _raised=raised):
            raise _raised

        monkeypatch.setattr(host, "_resolve", fail)
        answer = await post({"decision": "accept"})
        assert (answer.status, _code(answer)) == (status, code)
    assert routes.audited == []


# ── the SOUL.md route ────────────────────────────────────────────────────────


@pytest.fixture
def soul_route(workspace, monkeypatch):
    """``api_member_soul`` for member ``ada`` in *workspace*, the owner gate open."""
    from kiro_crew.dashboard.handlers import _shared

    audited: list[tuple] = []

    async def owner(request, operation):
        return None

    async def audit(request, operation, outcome, detail):
        audited.append((operation, outcome, detail))

    monkeypatch.setattr(host, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(_shared, "_audit", audit)
    monkeypatch.setattr(
        host, "_member_workspace_dir", lambda slug: ("Ada", workspace) if slug == "ada" else None
    )

    async def call(method: str, slug: str = "ada", body=None) -> web.Response:
        request = _route_request(method, f"/api/members/{slug}/soul", {"slug": slug}, body)
        return await host.api_member_soul(request)

    return SimpleNamespace(call=call, audited=audited, path=workspace / "SOUL.md")


@pytest.mark.asyncio
async def test_saving_a_soul_writes_the_file_and_audits_it(soul_route):
    saved = await soul_route.call("PUT", body={"text": "Be kind."})

    assert saved.status == 200
    assert json.loads(saved.text or "{}") == {"slug": "ada", "member": "Ada", "chars": 8}
    assert soul_route.path.read_text(encoding="utf-8") == "Be kind."
    assert soul_route.audited == [("members.soul.write", "success", "ada:8")]
    read = await soul_route.call("GET")
    assert json.loads(read.text or "{}")["text"] == "Be kind."


@pytest.mark.asyncio
@pytest.mark.parametrize("had_file", [True, False])
async def test_a_soul_that_overflows_the_bot_context_is_refused_and_undone(
    soul_route, monkeypatch, had_file
):
    from kiro_crew.config import loader
    from kiro_crew.member_essential_context import MemberEssentialContextError

    if had_file:
        soul_route.path.write_text("Be kind.", encoding="utf-8")
    seen: list[str] = []

    class Builder:
        def _build_v2_essentials(self, store, *, member, project, profile_overrides):
            seen.append(soul_route.path.read_text(encoding="utf-8"))
            raise MemberEssentialContextError("over the 64000-character budget")

    monkeypatch.setattr(
        loader, "resolve_agent_bindings", lambda cfg, name: SimpleNamespace(memory_store_name="ada")
    )
    request = _route_request("PUT", "/api/members/ada/soul", {"slug": "ada"}, {"text": "Be bold."})
    request.app["state"].context_builder = Builder()
    refused = await host.api_member_soul(request)

    assert refused.status == 413 and _code(refused) == "soul_over_budget"
    assert seen == ["Be bold."]
    if had_file:
        assert soul_route.path.read_text(encoding="utf-8") == "Be kind."
    else:
        assert not soul_route.path.exists()
    assert soul_route.audited == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"text": 7}, "invalid_soul"),
        ({"text": "x" * (host.SOUL_MAX_CHARS + 1)}, "invalid_soul"),
        ({"text": "token sk-ant-api03-" + "d" * 90}, "soul_secret"),
    ],
)
async def test_a_soul_save_that_is_not_plain_capped_text_is_refused(soul_route, body, code):
    refused = await soul_route.call("PUT", body=body)
    assert refused.status == 400 and _code(refused) == code
    assert not soul_route.path.exists() and soul_route.audited == []


@pytest.mark.asyncio
async def test_a_soul_for_an_unknown_bot_or_a_bad_file_is_refused(
    soul_route, tmp_path_factory, monkeypatch
):
    from kiro_crew import member_essential_context as essentials

    unknown = await soul_route.call("GET", slug="nobody")
    assert unknown.status == 404 and _code(unknown) == "member_unknown"
    soul_route.path.write_text("x" * (essentials._MAX_SOURCE_BYTES + 1), encoding="utf-8")
    oversized = await soul_route.call("GET")
    assert oversized.status == 413 and _code(oversized) == "soul_too_large"

    def unreadable(path, directory):
        raise essentials._UnreadableEssentialSourceError("refused")

    monkeypatch.setattr(essentials, "_read", unreadable)
    broken = await soul_route.call("GET")
    assert broken.status == 503 and _code(broken) == "soul_unreadable"


def test_a_slug_no_bot_carries_has_no_workspace(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig

    config = SimpleNamespace(
        agents={"Ada": SimpleNamespace(member_id="ada")}, workspaces={}, default_workspace="d"
    )
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: config))
    assert host._member_workspace_dir("grace") is None


# ── the context stack ───────────────────────────────────────────────────────

_PROMPT = (
    "[CRITICAL RULES]\nBe safe.\n"
    "[V2 ESSENTIAL CONTEXT]\n"
    "[MEMBER IDENTITY]\nYou are Ada.\n"
    "[Essential source: /w/SOUL.md]\nBe kind.\n"
    "[Essential source: /w/settings#context-settings]\nterse\n"
    "[Essential source: /w/notes.txt]\nnotes\n"
    "[END V2 ESSENTIAL CONTEXT]\n"
    "[Learned corrections]\n- Run the tests.\n"
    "[CURRENT DATE] today\n"
)


@pytest.fixture
def stack(monkeypatch, store, tmp_path):
    """The context-stack route for store ``crew``, owned by Bot Ada, on a stub builder."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import steering, usage

    member = SimpleNamespace(member_id="ada", kiro_agent="", workspace="", acp_backend="kas")
    config = SimpleNamespace(
        memory_stores={"crew": SimpleNamespace(owner_member="Ada", memory_version=2)},
        agents={"Ada": member},
        agent=SimpleNamespace(acp_backend="kas", member_acp_backend="kas"),
    )
    built: list[dict] = []

    def build_session_context(_message, **kwargs):
        built.append(kwargs)
        return _PROMPT

    files = [
        {"key": "user/a.md", "name": "a", "inclusion": "always", "readable": True},
        {"key": "user/b.md", "name": "b", "inclusion": "manual", "readable": False, "size": 9},
        {"key": "user/c.md", "name": "c", "inclusion": "always", "readable": True},
        {"key": "user/d.md", "name": "d", "inclusion": "never"},
    ]
    texts = {"user/a.md": ("Prefer tabs.", "", None), "user/c.md": ("", "", "denied")}
    traces: list[str] = []

    def trace(slot, days):
        traces.append(slot)
        return {"turns": [{"first_start": True, "blocks": {"critical_rules": 25}}]}

    async def owner(request, operation):
        return None

    async def admin_store(request, operation):
        return "crew", None

    async def tier_for(state, name):
        return store

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: config))
    monkeypatch.setattr(host, "_member_workspace_dir", lambda slug: ("Ada", tmp_path))
    monkeypatch.setattr(steering, "list_steering_blocking", lambda project: {"files": files})
    monkeypatch.setattr(steering, "_resolve_and_read_blocking", lambda key, project: texts[key])
    monkeypatch.setattr(usage, "context_trace", trace)
    monkeypatch.setattr(host, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(host, "_admin_store", admin_store)
    monkeypatch.setattr(host, "vector_memory_for_store", tier_for)
    slots = {
        "chat-old": SimpleNamespace(memory_store="crew", created_at="1"),
        "chat-new.memory-crew": SimpleNamespace(memory_store="", created_at="2"),
        "chat-other": SimpleNamespace(memory_store="else", created_at="3"),
    }
    state = SimpleNamespace(
        _slots=slots, context_builder=SimpleNamespace(build_session_context=build_session_context)
    )

    async def call(query: str = "") -> web.Response:
        request = _route_request("GET", f"/api/memory/context-stack?store=crew{query}")
        request.app["state"] = state
        return await host.api_memory_context_stack(request)

    return SimpleNamespace(call=call, built=built, traces=traces, state=state, root=tmp_path)


@pytest.mark.asyncio
async def test_the_context_stack_names_each_block_its_owner_and_last_turn(stack, store):
    _lesson(store, "lesson.keep", "Run the tests.")

    response = await stack.call()

    assert response.status == 200
    body = json.loads(response.text or "{}")
    labels = [b["label"] for b in body["blocks"]]
    for label in ("steering", "critical_rules", "member_identity", "soul_md", "lessons"):
        assert label in labels
    assert {"context_settings", "essential_source"} <= set(labels)
    assert body["member"] == {"slug": "ada", "name": "Ada", "version": 2}
    assert body["total_chars"] == len(_PROMPT) and body["store"] == "crew"
    assert stack.built[0]["memory_store"] == "crew" and stack.built[0]["member"] == "Ada"
    assert stack.built[0]["project"] == str(stack.root)
    # The newest chat on the store is the one its trace is read for.
    assert stack.traces == ["chat-new.memory-crew"]
    assert body["last_turn"] == {"chat": "chat-new.memory-crew", "at": None}
    by_label = {b["label"]: b for b in body["blocks"]}
    assert by_label["critical_rules"]["last_turn"]["trace_chars"] == 25
    assert by_label["soul_md"]["group"] == "essential" and by_label["soul_md"]["editable"]
    # A section inside a traced block went with it: SOUL.md rode the critical rules block.
    assert by_label["soul_md"]["last_turn"] == {
        "sent": True,
        "trace_label": "critical_rules",
        "trace_chars": 25,
    }
    assert by_label["lessons"]["last_turn"]["sent"] is False
    assert by_label["lessons"]["cap"] and by_label["lessons"]["owner"] == "learned"
    assert body["groups"][0]["id"] == "essential" and body["groups"][0]["chars"] > 0
    steering_files = by_label["steering"]["files"]
    assert [f["key"] for f in steering_files] == ["user/a.md", "user/b.md", "user/c.md"]
    assert [f["chars"] for f in steering_files] == [12, 9, 0]
    assert by_label["steering"]["chars"] == len("Prefer tabs.")
    assert body["recall"] == {"rules": 0, "findings": 1, "facts": 0, "experiences": 0}
    assert body["queues"] == {"dreams": 0, "never_recalled": 0}
    assert [r["id"] for r in body["recent"]] == ["lesson.keep"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["kas", "claude"])
async def test_the_context_stack_is_built_for_the_harness_the_turn_runs_on(stack, backend):
    from kiro_crew.agent_sdk.drivers.acp import provider_label_for_backend
    from kiro_crew.config.loader import KiroCrewConfig

    KiroCrewConfig.load().agents["Ada"].acp_backend = backend
    await stack.call()
    assert stack.built[0]["provider_type"] == provider_label_for_backend(backend)


@pytest.mark.asyncio
async def test_the_context_stack_reads_the_named_chat_and_refuses_when_unbuilt(stack):
    named = await stack.call("&chat=chat-old")
    assert json.loads(named.text or "{}")["last_turn"]["chat"] == "chat-old"
    assert stack.traces == ["chat-old"]

    stack.state.context_builder = None
    unbuilt = await stack.call()
    assert unbuilt.status == 503 and _code(unbuilt) == "store_unavailable"


def test_a_session_start_with_no_trace_has_no_last_turn(monkeypatch):
    from kiro_crew.dashboard.handlers import usage

    monkeypatch.setattr(usage, "context_trace", lambda slot, days: {"turns": [{"blocks": {}}]})
    assert host._last_turn("chat-1") is None
    assert host._latest_slot(SimpleNamespace(_slots={}), "crew") == ""


def test_the_newest_rows_come_first_across_kinds(store):
    _lesson(store, "lesson.keep", "Run the tests.")
    with store._db_lock:
        store.db.execute(
            "INSERT INTO episodic_memories (id, text, created_at) VALUES ('ep1', 'met Ada', ?)",
            ("2026-10-03T08:00:00+00:00",),
        )
        store.db.commit()

    rows = host._recent(store)

    assert [(r["kind"], r["id"]) for r in rows] == [
        ("episode", "ep1"),
        ("directive", "lesson.keep"),
    ]
    assert rows[1]["tier"] == "on_topic" and rows[0]["text"] == "met Ada"


def test_a_member_marker_mid_line_does_not_split_a_block():
    prompt = "intro [MEMBER IDENTITY] inline\n[MEMBER IDENTITY]\nAda"
    assert host._split_member("x", 0, len(prompt), prompt) == [
        ("x", 0, prompt.index("\n[MEMBER") + 1),
        ("member_identity", prompt.index("\n[MEMBER") + 1, len(prompt)),
    ]
    assert host._split_member("y", 0, 5, "plain") == [("y", 0, 5)]
