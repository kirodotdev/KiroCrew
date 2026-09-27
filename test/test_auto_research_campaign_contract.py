"""Characterization contract for the Research Lab campaign engine.

These tests pin what callers of ``kiro_crew.apps.builtins.auto_research.handlers``
observe -- the names the module exposes, the patch seams tests and tools rely
on, the route table, the SQLite schema and payload shapes, and the concurrency
rules that keep one campaign's transitions ordered -- independently of which
module implements each piece, so a restructuring that has to edit them has
changed behaviour.

Covered here, beyond the per-feature suites next door:

* the historic ``handlers`` namespace still resolves every name it bound, and
  the imported collaborators keep their identity;
* a patch applied to ``handlers`` still reaches the code that consumes it;
* route table, schema, pragmas and the create/get/update/delete shapes;
* single-flight user transitions, refusal of a stale run generation, delete
  staying final over late callbacks, and the residue of a failed cycle write;
* both execution modes' launch ordering;
* log records keep the historic logger name;
* no bounded slice feeds a redactor anywhere in the app package.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from test_core_path_redact_before_bound import _find_slice_inside_redact_call

from kiro_crew.apps.builtins.auto_research import handlers as h

BASE = "/api/apps/auto-research"
_HANDLERS_LOGGER = "kiro_crew.apps.builtins.auto_research.handlers"

# Every name ``handlers`` exposes to importers and patches, minus the stdlib
# modules and typing helpers imported only for its own use.
_HISTORIC_NAMES = """
asyncio web _sq AUTO_RESEARCH_APP is_campaign_id is_research_slot_key research_slot_key
RESEARCH_WORKFLOW_SOURCE build_workflow_args is_app_enabled AUTONUDGE_STOP_REASON
_autonudge_instance data_home slot_history_key ImportChunkBudgetError LLMPool
_extract_json_of_type OnLoopDBGuard is_link_or_junction unlink_link_or_junction
ArtifactNotFoundError ArtifactStore _HAS_ARTIFACTS redact_credentials
redact_exfiltration_urls _HAS_SECURITY sel logger _UNTRUSTED_DATA_NOTICE
_fence_untrusted RESEARCH_DIR DB_PATH research_dir db_path _DB_INIT_LOCK
_INITIALIZED_DBS MAX_CYCLES_HARD_CAP VALID_EXECUTION_MODES DEFAULT_EXECUTION_MODE
DEFAULT_MAX_SUBQUESTIONS_PER_ROUND DEFAULT_DEPTH_DECAY DEFAULT_RESERVE_FRACTION
POLL_INTERVAL _TERMINAL_LOOP_REMOVAL_ATTEMPTS _MAX_PARALLEL_WORKERS DEFAULT_IDLE_SECS
_FIRST_CYCLE_GRACE_SECS _TRUST_TTL_SECS _MAX_MODEL_LEN _unresponsive_deadline
CampaignStatus _TERMINAL_STATUSES _RESEARCH_AGENT _RESEARCH_NUDGE _validate_campaign_id
_safe_campaign_dir _ON_LOOP_DB_GUARD _get_db _ensure_schema _redact_finding
_redact_tree_node _audit _campaign_model validate_campaign _CYCLE_FILE_RE _cycle_index
_cycle_finding_files check_stagnation _campaign_dir _read_text_or_missing
_read_json_or_missing _write_text _write_new_cycle_files _copy_parent_findings
_unlink_if_present _questions_path _pending_question write_status write_guidance
get_findings _list_cycle_files _read_finding_file _FORK_NAME_PREFIX _fork_name
create_campaign update_campaign_status _redact_campaign get_campaign list_campaigns
delete_campaign _watchdog_task _SSE_QUEUE_MAXSIZE _sse_queues _campaign_transition_locks
_campaign_transition_lock _settle_before_cancellation _guarded_txn _sse_from_thread
_guarded_transition _expire_trust _emit_sse _should_pause_for_question
_suspend_research_loops_while_disabled _WORKER_DONE_FILENAME _WORKER_DONE_MAX_BYTES
_read_worker_done _clear_worker_done_marker _stalled_campaign_verdict
_persist_new_cycle_bookkeeping _record_new_cycle_from_watchdog _campaign_run_has_status
_campaign_run_is_current _settle_campaign_from_watchdog _watchdog_loop _require_auth
_prepare_loop_launch _launch_loop _brief_publish_locks _brief_publish_locks_guard
_brief_publish_lock _write_brief _EMERGENT_FILENAME _FINALIZE_FLAG _reserve_cycles
_in_reserve_zone _ingest_emergent_questions _activate_emergent _should_finalize
_enter_finalize _advance_exploration _stop_loop _WORKFLOW_RUN_FILE
_campaign_execution_mode _write_workflow_run_id _read_workflow_cycle_offset
_read_workflow_run_id _launch_workflow _stop_workflow _poll_workflow_campaign
_read_json_body _handle_validate _MAX_GRILL_DEPTH _GRILL_CHILD_CAP _new_node_id
_node_depth _GRILL_EXPAND_PROMPT _compact_tree _grill_node_shaped _parse_grill_nodes
_grill_expand_children _handle_grill_expand _handle_create _handle_list _handle_get
_read_report _handle_report _handle_action _handle_delete _handle_nudge _REPORT_TIMEOUT
_build_report_prompt _handle_report_status _handle_to_artifact _render_findings_html
_handle_knowledge_status _handle_to_knowledge _handle_add_question _handle_stream
_handle_grill_tree register_routes
""".split()

# Collaborators ``handlers`` imported: (defining module, attribute) whose object
# ``handlers.<name>`` must still be, so a patch of either spelling is one patch.
_IMPORTED_IDENTITIES = {
    "_sq": ("kiro_crew.apps.builtins.auto_research", "subquestion_queue"),
    "AUTO_RESEARCH_APP": ("kiro_crew.apps.builtins.auto_research.session_keys", None),
    "is_campaign_id": ("kiro_crew.apps.builtins.auto_research.session_keys", None),
    "is_research_slot_key": ("kiro_crew.apps.builtins.auto_research.session_keys", None),
    "research_slot_key": ("kiro_crew.apps.builtins.auto_research.session_keys", None),
    "RESEARCH_WORKFLOW_SOURCE": ("kiro_crew.apps.builtins.auto_research.workflow_template", None),
    "build_workflow_args": ("kiro_crew.apps.builtins.auto_research.workflow_template", None),
    "is_app_enabled": ("kiro_crew.apps.manager", None),
    "AUTONUDGE_STOP_REASON": ("kiro_crew.autonudge", None),
    "_autonudge_instance": ("kiro_crew.autonudge", "get_instance"),
    "data_home": ("kiro_crew.config.paths", None),
    "slot_history_key": ("kiro_crew.dashboard.chat_utils", None),
    "ImportChunkBudgetError": ("kiro_crew.knowledge.ingestion", None),
    "LLMPool": ("kiro_crew.knowledge.llm_pool", None),
    "_extract_json_of_type": ("kiro_crew.llm_helpers", None),
    "OnLoopDBGuard": ("kiro_crew.on_loop_db", None),
    "is_link_or_junction": ("kiro_crew.platform_compat", None),
    "unlink_link_or_junction": ("kiro_crew.platform_compat", None),
    "ArtifactNotFoundError": ("kiro_crew.artifacts", None),
    "ArtifactStore": ("kiro_crew.artifacts", None),
    "redact_credentials": ("kiro_crew.security", None),
    "redact_exfiltration_urls": ("kiro_crew.security", None),
    "sel": ("kiro_crew.sel", None),
    "web": ("aiohttp", "web"),
    "asyncio": ("asyncio", ""),
}

_ROUTES = [
    ("POST", f"{BASE}/validate", "_handle_validate"),
    ("POST", f"{BASE}/grill/expand", "_handle_grill_expand"),
    ("POST", f"{BASE}/campaigns", "_handle_create"),
    ("HEAD", f"{BASE}/campaigns", "_handle_list"),
    ("GET", f"{BASE}/campaigns", "_handle_list"),
    ("HEAD", f"{BASE}/campaigns/{{id}}", "_handle_get"),
    ("GET", f"{BASE}/campaigns/{{id}}", "_handle_get"),
    ("HEAD", f"{BASE}/campaigns/{{id}}/report", "_handle_report"),
    ("GET", f"{BASE}/campaigns/{{id}}/report", "_handle_report"),
    ("HEAD", f"{BASE}/campaigns/{{id}}/grill-tree", "_handle_grill_tree"),
    ("GET", f"{BASE}/campaigns/{{id}}/grill-tree", "_handle_grill_tree"),
    ("PATCH", f"{BASE}/campaigns/{{id}}", "_handle_action"),
    ("DELETE", f"{BASE}/campaigns/{{id}}", "_handle_delete"),
    ("POST", f"{BASE}/campaigns/{{id}}/nudge", "_handle_nudge"),
    ("POST", f"{BASE}/campaigns/{{id}}/questions", "_handle_add_question"),
    ("POST", f"{BASE}/campaigns/{{id}}/to-knowledge", "_handle_to_knowledge"),
    ("HEAD", f"{BASE}/campaigns/{{id}}/knowledge-status", "_handle_knowledge_status"),
    ("GET", f"{BASE}/campaigns/{{id}}/knowledge-status", "_handle_knowledge_status"),
    ("POST", f"{BASE}/campaigns/{{id}}/to-artifact", "_handle_to_artifact"),
    ("HEAD", f"{BASE}/campaigns/{{id}}/report-status", "_handle_report_status"),
    ("GET", f"{BASE}/campaigns/{{id}}/report-status", "_handle_report_status"),
    ("HEAD", f"{BASE}/campaigns/{{id}}/stream", "_handle_stream"),
    ("GET", f"{BASE}/campaigns/{{id}}/stream", "_handle_stream"),
]

# PRAGMA table_info(campaigns) on a freshly created DB:
# (cid, name, type, notnull, dflt_value, pk).
_SCHEMA = [
    (0, "id", "TEXT", 0, None, 1),
    (1, "name", "TEXT", 1, None, 0),
    (2, "question", "TEXT", 1, None, 0),
    (3, "sub_questions", "TEXT", 1, "'[]'", 0),
    (4, "sources", "TEXT", 1, "'[]'", 0),
    (5, "max_cycles", "INTEGER", 1, "30", 0),
    (6, "idle_secs", "INTEGER", 1, "120", 0),
    (7, "status", "TEXT", 1, "'ready'", 0),
    (8, "created_at", "REAL", 1, None, 0),
    (9, "started_at", "REAL", 0, None, 0),
    (10, "completed_at", "REAL", 0, None, 0),
    (11, "total_cycles", "INTEGER", 1, "0", 0),
    (12, "error_message", "TEXT", 0, None, 0),
    (13, "success_criteria", "TEXT", 0, None, 0),
    (14, "auto_approve", "INTEGER", 1, "0", 0),
    (15, "parent_id", "TEXT", 0, None, 0),
    (16, "scope_constraints", "TEXT", 0, None, 0),
    (17, "parallel_workers", "INTEGER", 1, "1", 0),
    (18, "report_artifact_slug", "TEXT", 0, None, 0),
    (19, "execution_mode", "TEXT", 1, "'agent'", 0),
    (20, "max_subquestions_per_round", "INTEGER", 1, "3", 0),
    (21, "depth_decay", "REAL", 1, "0.5", 0),
    (22, "reserve_fraction", "REAL", 1, "0.15", 0),
    (23, "model", "TEXT", 1, "''", 0),
]

# The table as the first release created it; every later column is migrated in.
_ORIGINAL_DDL = """CREATE TABLE campaigns (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, question TEXT NOT NULL,
    sub_questions TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '[]',
    max_cycles INTEGER NOT NULL DEFAULT 30, idle_secs INTEGER NOT NULL DEFAULT 120,
    status TEXT NOT NULL DEFAULT 'ready',
    created_at REAL NOT NULL, started_at REAL, completed_at REAL,
    total_cycles INTEGER NOT NULL DEFAULT 0, error_message TEXT)"""


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(h, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(h, "RESEARCH_DIR", tmp_path / "r")
    return tmp_path


@pytest.fixture
def _no_autonudge(monkeypatch: pytest.MonkeyPatch) -> None:
    """No live loop registry; classes that drive loop code opt in."""
    monkeypatch.setattr(h, "_autonudge_instance", lambda: None)


@pytest.fixture(autouse=True)
def _no_stray_sse_queues():
    before = list(h._sse_queues)
    yield
    assert h._sse_queues == before, "test leaked an SSE queue"


class _Sink:
    """Records ``_emit_sse`` payloads in delivery order."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [str(e.get("type")) for e in self.events]


@pytest.fixture
def sse(monkeypatch: pytest.MonkeyPatch) -> _Sink:
    sink = _Sink()
    monkeypatch.setattr(h, "_emit_sse", sink)
    return sink


# --- helpers ----------------------------------------------------------------


def _app(**keys: Any) -> web.Application:
    app = web.Application()
    for key, value in keys.items():
        app[key] = value
    return app


def _req(
    method: str,
    path: str,
    *,
    app: web.Application | None = None,
    match: dict | None = None,
    body: Any = ...,
) -> web.Request:
    req = make_mocked_request(method, f"{BASE}/{path}", app=app, match_info=match or {})
    req["user"] = "test-user"
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.StreamResponse) -> dict:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _campaign(**config: Any) -> str:
    cfg: dict[str, Any] = {"question": "How do teams handle API rate limiting today?"}
    cfg.update(config)
    return h.create_campaign(cfg)["id"]


def _set(cid: str, **cols: Any) -> None:
    db = h._get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in cols)
        db.execute(f"UPDATE campaigns SET {sets} WHERE id = ?", (*cols.values(), cid))
        db.commit()
    finally:
        db.close()


def _running(cid: str, *, started_at: float | None = None) -> float:
    h.update_campaign_status(cid, h.CampaignStatus.RUNNING)
    if started_at is not None:
        _set(cid, started_at=started_at)
    campaign = h.get_campaign(cid)
    assert campaign is not None
    return float(campaign["started_at"])


def _row(cid: str) -> dict | None:
    return h.get_campaign(cid)


def _write_finding(cid: str, cycle: int, **fields: Any) -> Path:
    d = h._campaign_dir(cid)
    payload: dict[str, Any] = {"cycle": cycle, "summary": "s", "new_findings_count": 1}
    payload.update(fields)
    path = d / "findings" / ("cycle_%03d.json" % cycle)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


async def _await_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _investigation_snapshot(*labels: str, status: str = "running") -> dict:
    events: list[dict] = []
    for i, label in enumerate(labels):
        agent_id = f"a{i}"
        events.append({"type": "agent_started", "data": {"agent_id": agent_id, "label": label}})
        events.append(
            {
                "type": "agent_finished",
                "data": {"agent_id": agent_id, "ok": True, "result_summary": f"sum {i}"},
            }
        )
    return {"status": status, "events": events, "result": {"report": "final report"}}


# --- the historic namespace --------------------------------------------------


class TestHistoricNamespace:
    def test_every_historic_name_still_resolves_through_handlers(self):
        missing = [name for name in _HISTORIC_NAMES if not hasattr(h, name)]
        assert missing == []

    def test_star_import_and_dir_still_list_the_route_entry_point(self):
        assert "register_routes" in dir(h)
        package = importlib.import_module("kiro_crew.apps.builtins.auto_research")
        assert package.register_routes is h.register_routes

    @pytest.mark.parametrize("name", sorted(_IMPORTED_IDENTITIES))
    def test_imported_collaborators_keep_their_identity(self, name: str):
        module_name, attr = _IMPORTED_IDENTITIES[name]
        module = importlib.import_module(module_name)
        if attr == "":
            assert getattr(h, name) is module
            return
        assert getattr(h, name) is getattr(module, attr or name)

    def test_status_values_are_the_persisted_strings(self):
        assert [s.value for s in h.CampaignStatus] == [
            "ready",
            "running",
            "paused",
            "stagnant",
            "needs_input",
            "complete",
            "failed",
            "stopped",
        ]
        assert h._TERMINAL_STATUSES == (h.CampaignStatus.COMPLETE, h.CampaignStatus.STOPPED)

    def test_tunables_keep_their_values(self):
        assert (h.MAX_CYCLES_HARD_CAP, h.POLL_INTERVAL, h.DEFAULT_IDLE_SECS) == (100, 5, 120)
        assert (h._FIRST_CYCLE_GRACE_SECS, h._TRUST_TTL_SECS) == (600, 24 * 3600)
        assert (h._MAX_PARALLEL_WORKERS, h._MAX_MODEL_LEN) == (5, 128)
        assert h._TERMINAL_LOOP_REMOVAL_ATTEMPTS == 3
        assert h._SSE_QUEUE_MAXSIZE == 256
        assert h._WORKER_DONE_MAX_BYTES == 64 * 1024
        assert (h._MAX_GRILL_DEPTH, h._GRILL_CHILD_CAP, h._REPORT_TIMEOUT) == (4, 5, 90.0)
        assert h.VALID_EXECUTION_MODES == ("agent", "workflow")
        assert (h.DEFAULT_EXECUTION_MODE, h.DEFAULT_MAX_SUBQUESTIONS_PER_ROUND) == ("agent", 3)
        assert (h.DEFAULT_DEPTH_DECAY, h.DEFAULT_RESERVE_FRACTION) == (0.5, 0.15)
        assert (h._WORKER_DONE_FILENAME, h._WORKFLOW_RUN_FILE) == (
            "worker_done.json",
            "workflow_run.json",
        )
        assert (h._EMERGENT_FILENAME, h._FINALIZE_FLAG) == (
            "emergent_questions.json",
            "finalize.flag",
        )
        assert h._RESEARCH_AGENT == "kirocrew-research"


# --- route table and registration --------------------------------------------


@pytest.mark.usefixtures("_no_autonudge")
class TestRouteRegistration:
    def test_route_table_is_unchanged(self):
        app = web.Application()
        h.register_routes(app)
        routes = [(r.method, r.resource.canonical, r.handler.__name__) for r in app.router.routes()]
        assert routes == _ROUTES
        for _method, _path, name in _ROUTES:
            handler = next(r.handler for r in app.router.routes() if r.handler.__name__ == name)
            assert handler is getattr(h, name)

    def test_registration_seeds_the_task_registry_and_lifecycle_hooks(self):
        app = web.Application()
        h.register_routes(app)
        assert app["_bg_tasks"] == set()
        assert app.on_startup[-1].__name__ == "_start_watchdog"
        assert [hook.__name__ for hook in app.on_shutdown] == ["_stop_watchdog"]

    @pytest.mark.asyncio
    async def test_startup_builds_the_pool_and_task_and_shutdown_reaps_both(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        pools: list[Any] = []

        class _Pool:
            def __init__(self, pool_size: int) -> None:
                self.pool_size = pool_size
                self.shutdown = AsyncMock()
                pools.append(self)

        monkeypatch.setattr(h, "LLMPool", _Pool)
        monkeypatch.setattr(h, "is_app_enabled", lambda _name: False)
        app = web.Application()
        h.register_routes(app)
        await app.on_startup[-1](app)
        task = h._watchdog_task
        assert task is not None and not task.done()
        assert app["auto_research_llm_pool"] is pools[0]
        assert pools[0].pool_size == 1
        await app.on_shutdown[-1](app)
        assert task.done()
        pools[0].shutdown.assert_awaited_once_with()


# --- schema and payload shapes -----------------------------------------------


@pytest.mark.usefixtures("_no_autonudge")
class TestSchemaAndPayloads:
    def test_fresh_schema_and_pragmas(self):
        conn = h._get_db()
        try:
            assert [tuple(r) for r in conn.execute("PRAGMA table_info(campaigns)")] == _SCHEMA
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
            assert conn.isolation_level is None
        finally:
            conn.close()

    def test_original_schema_is_migrated_with_defaults(self, _isolate: Path):
        db = sqlite3.connect(str(_isolate / "t.db"))
        db.execute(_ORIGINAL_DDL)
        db.execute(
            "INSERT INTO campaigns (id, name, question, created_at) VALUES (?, ?, ?, ?)",
            ("0123abcd", "old", "an old question that predates migrations", 1.0),
        )
        db.commit()
        db.close()
        campaign = h.get_campaign("0123abcd")
        assert campaign is not None
        assert {
            key: campaign[key]
            for key in (
                "success_criteria",
                "auto_approve",
                "parent_id",
                "scope_constraints",
                "parallel_workers",
                "report_artifact_slug",
                "execution_mode",
                "max_subquestions_per_round",
                "depth_decay",
                "reserve_fraction",
                "model",
            )
        } == {
            "success_criteria": None,
            "auto_approve": 0,
            "parent_id": None,
            "scope_constraints": None,
            "parallel_workers": 1,
            "report_artifact_slug": None,
            "execution_mode": "agent",
            "max_subquestions_per_round": 3,
            "depth_decay": 0.5,
            "reserve_fraction": 0.15,
            "model": "",
        }

    def test_create_get_update_delete_shapes(self, _isolate: Path):
        created = h.create_campaign({"question": "How do teams handle API rate limiting today?"})
        assert list(created) == ["id", "name", "status"]
        assert created["status"] is h.CampaignStatus.READY
        cid = created["id"]
        campaign = h.get_campaign(cid)
        assert campaign is not None
        assert list(campaign) == [row[1] for row in _SCHEMA] + ["findings", "pending_question"]
        status_file = json.loads((_isolate / "r" / cid / "status.json").read_text("utf-8"))
        assert list(status_file) == ["status", "campaign_id", "ts"]
        assert h.update_campaign_status(cid, "running") == {"id": cid, "status": "running"}
        status_file = json.loads((_isolate / "r" / cid / "status.json").read_text("utf-8"))
        assert list(status_file) == ["status", "campaign_id", "ts", "error_message"]
        assert h.update_campaign_status("zzzzzzzz", "running") == {"error": "invalid campaign_id"}
        assert h.update_campaign_status("fedcba98", "running") == {"error": "campaign not found"}
        assert h.delete_campaign(cid) == {"id": cid, "deleted": True, "residual": False}
        assert h.delete_campaign(cid) == {"error": "campaign not found"}
        assert h.delete_campaign("../x") == {"error": "invalid campaign_id"}

    def test_terminal_transition_refusal_message(self):
        cid = _campaign()
        h.update_campaign_status(cid, h.CampaignStatus.STOPPED)
        assert h.update_campaign_status(cid, h.CampaignStatus.PAUSED) == {
            "error": "invalid transition: stopped -> CampaignStatus.PAUSED"
        }
        assert h.update_campaign_status(cid, h.CampaignStatus.RUNNING)["status"] == "running"


# --- a patch on handlers reaches its consumer ---------------------------------


@pytest.mark.usefixtures("_no_autonudge")
class TestPatchReach:
    def test_write_status_patch_reaches_status_updates(self, monkeypatch: pytest.MonkeyPatch):
        cid = _campaign()
        calls: list[tuple] = []
        monkeypatch.setattr(h, "write_status", lambda *a, **kw: calls.append((a, kw)))
        h.update_campaign_status(cid, h.CampaignStatus.RUNNING)
        assert calls == [((cid, h.CampaignStatus.RUNNING), {"error_message": None})]

    def test_sel_patch_reaches_lifecycle_audit(self, monkeypatch: pytest.MonkeyPatch):
        log = MagicMock()
        monkeypatch.setattr(h, "sel", lambda: log)
        cid = _campaign()
        log.log_api_access.assert_called_once_with(
            caller="auto_research",
            operation="campaign_created",
            outcome="success",
            resources=cid,
        )

    def test_get_db_patch_reaches_every_store_reader(self, monkeypatch: pytest.MonkeyPatch):
        cid = _campaign()
        real = h._get_db
        opened: list[int] = []

        def _counting() -> sqlite3.Connection:
            opened.append(1)
            return real()

        monkeypatch.setattr(h, "_get_db", _counting)
        assert h.get_campaign(cid) is not None
        assert h._campaign_execution_mode(cid) == "agent"
        assert h._should_finalize(cid) is False
        assert h._guarded_txn(cid, "paused", ("running",), None) is None
        assert h._campaign_run_is_current(cid, 1.0) is False
        assert len(opened) == 5

    def test_update_status_patch_reaches_the_guarded_write(self, monkeypatch: pytest.MonkeyPatch):
        cid = _campaign()
        started = _running(cid)
        seen: list[tuple] = []
        monkeypatch.setattr(
            h, "update_campaign_status", lambda *a, **kw: seen.append((a, kw)) or {"id": a[0]}
        )
        result = h._guarded_txn(cid, "paused", ("running",), started, error_message="x")
        assert result == {"id": cid}
        assert seen == [((cid, "paused"), {"error_message": "x"})]

    @pytest.mark.asyncio
    async def test_emit_patch_reaches_thread_and_loop_emitters(self, sse: _Sink):
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(h._sse_from_thread, loop, {"type": "t1", "campaign_id": "c"})
        assert await _await_until(lambda: sse.types() == ["t1"])
        cid = await asyncio.to_thread(_campaign)
        response = await h._handle_add_question(
            _req("POST", f"campaigns/{cid}/questions", match={"id": cid}, body={"text": "Why?"})
        )
        assert response.status == 200
        assert sse.types() == ["t1", "question_added"]

    @pytest.mark.asyncio
    async def test_emit_patch_reaches_workflow_launch_failures(self, sse: _Sink):
        cid = await asyncio.to_thread(_campaign, execution_mode="workflow")
        await h._launch_workflow(_req("PATCH", f"campaigns/{cid}", app=_app()), cid)
        assert sse.events == [{"type": "failed", "campaign_id": cid}]

    @pytest.mark.asyncio
    async def test_autonudge_patch_reaches_loop_control(self, monkeypatch: pytest.MonkeyPatch):
        loop = SimpleNamespace(id="L1", active=True, slot_key="research-0123abcd")
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop),
            list_all=MagicMock(return_value=[loop]),
            remove=AsyncMock(),
            update=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._stop_loop("0123abcd", remove=True)
        svc.remove.assert_awaited_once_with("L1", stop_reason="")
        await h._stop_loop("0123abcd", remove=False)
        svc.update.assert_awaited_once_with("L1", active=False)
        await h._suspend_research_loops_while_disabled(None)
        assert svc.update.await_count == 2

    def test_advance_patch_reaches_cycle_bookkeeping(self, monkeypatch: pytest.MonkeyPatch):
        cid = _campaign()
        path = _write_finding(cid, 1)
        advanced: list[str] = []
        monkeypatch.setattr(h, "_advance_exploration", advanced.append)
        latest = h._persist_new_cycle_bookkeeping(cid, [path])
        assert latest["cycle"] == 1
        assert advanced == [cid]
        assert (h.get_campaign(cid) or {})["total_cycles"] == 1

    def test_should_finalize_patch_reaches_the_exploration_step(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign()
        monkeypatch.setattr(h, "_should_finalize", lambda _cid: True)
        h._advance_exploration(cid)
        assert (h._campaign_dir(cid) / "finalize.flag").exists()

    def test_worker_done_patch_reaches_the_stall_verdict(self, monkeypatch: pytest.MonkeyPatch):
        cid = _campaign()
        path = _write_finding(cid, 1)
        monkeypatch.setattr(h, "_read_worker_done", lambda _cid: {"reason": "done"})
        status, message = h._stalled_campaign_verdict(cid, [path])
        assert status is h.CampaignStatus.STOPPED
        assert message == "Worker ended the research loop — findings are preserved."

    @pytest.mark.asyncio
    async def test_marker_patch_reaches_launch_preparation(self, monkeypatch: pytest.MonkeyPatch):
        cleared: list[str] = []
        monkeypatch.setattr(h, "_clear_worker_done_marker", cleared.append)
        await h._prepare_loop_launch("0123abcd")
        assert cleared == ["0123abcd"]

    def test_cycle_listing_patch_reaches_the_workflow_run_file(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign(execution_mode="workflow")
        monkeypatch.setattr(h, "_list_cycle_files", lambda _cid: [Path("a"), Path("b")])
        h._write_workflow_run_id(cid, "run-1")
        assert h._read_workflow_cycle_offset(cid) == 2
        assert h._read_workflow_run_id(cid) == "run-1"

    @pytest.mark.asyncio
    async def test_run_identity_patch_reaches_the_workflow_poll(
        self, sse: _Sink, monkeypatch: pytest.MonkeyPatch
    ):
        cid = await asyncio.to_thread(_campaign, execution_mode="workflow")
        started = await asyncio.to_thread(_running, cid)
        await asyncio.to_thread(h._write_workflow_run_id, cid, "run-1")
        monkeypatch.setattr(h, "_campaign_run_is_current", lambda _cid, _started: False)
        snap = _investigation_snapshot("investigate: q", status="finished")
        state = SimpleNamespace(workflow_service=SimpleNamespace(result=lambda _rid: snap))
        await h._poll_workflow_campaign(cid, state, started)
        assert sse.events == []
        assert not (h._campaign_dir(cid) / "findings" / "cycle_001.json").exists()

    @pytest.mark.asyncio
    async def test_verdict_patch_reaches_terminal_settlement(
        self, sse: _Sink, monkeypatch: pytest.MonkeyPatch
    ):
        cid = await asyncio.to_thread(_campaign)
        started = await asyncio.to_thread(_running, cid)
        monkeypatch.setattr(
            h,
            "_stalled_campaign_verdict",
            lambda *_a, **_kw: (h.CampaignStatus.COMPLETE, None),
        )
        await h._settle_campaign_from_watchdog(cid, [], {}, {}, observed_started_at=started)
        assert (await asyncio.to_thread(_row, cid) or {})["status"] == "complete"
        assert sse.events == [{"type": "complete", "campaign_id": cid}]

    def test_security_flag_patch_reaches_redaction(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(h, "_HAS_SECURITY", False)
        assert h._redact_finding({"a": "text", "n": 1}) == {"a": "[REDACTED]", "n": 1}
        assert h._redact_tree_node("x") == "[REDACTED]"

    @pytest.mark.asyncio
    async def test_security_flag_patch_reaches_the_workflow_report(
        self, sse: _Sink, monkeypatch: pytest.MonkeyPatch
    ):
        cid = await asyncio.to_thread(_campaign, execution_mode="workflow")
        started = await asyncio.to_thread(_running, cid)
        await asyncio.to_thread(h._write_workflow_run_id, cid, "run-1")
        monkeypatch.setattr(h, "_HAS_SECURITY", False)
        snap = {"status": "finished", "events": [], "result": {"report": "raw report"}}
        state = SimpleNamespace(workflow_service=SimpleNamespace(result=lambda _rid: snap))
        await h._poll_workflow_campaign(cid, state, started)
        report = (h._campaign_dir(cid) / "FINDINGS.md").read_text(encoding="utf-8")
        assert report == "[REDACTED]"
        assert await _await_until(lambda: sse.types() == ["complete"])

    @pytest.mark.asyncio
    async def test_artifact_patches_reach_report_status(self, monkeypatch: pytest.MonkeyPatch):
        cid = await asyncio.to_thread(_campaign)
        await asyncio.to_thread(_set, cid, report_artifact_slug="slug-1")
        probed: list[str] = []

        class _Store:
            def get(self, slug: str) -> object:
                probed.append(slug)
                return object()

        monkeypatch.setattr(h, "ArtifactStore", _Store)
        req = _req("GET", f"campaigns/{cid}/report-status", match={"id": cid})
        assert _body(await h._handle_report_status(req)) == {"slug": "slug-1"}
        assert probed == ["slug-1"]
        monkeypatch.setattr(h, "_HAS_ARTIFACTS", False)
        assert _body(await h._handle_report_status(req)) == {"slug": None}
        assert probed == ["slug-1"]

    @pytest.mark.asyncio
    async def test_grill_patch_reaches_the_expand_endpoint(self, monkeypatch: pytest.MonkeyPatch):
        async def _children(_pool, _question, _tree, _node_id):
            return [{"kind": "clarifier", "text": "Which region?", "recommended": "EU"}]

        monkeypatch.setattr(h, "_grill_expand_children", _children)
        body = {"question": "How do teams handle API rate limiting today?", "tree": []}
        response = await h._handle_grill_expand(_req("POST", "grill/expand", body=body))
        nodes = _body(response)["nodes"]
        assert [(n["kind"], n["text"], n["recommended"], n["origin"]) for n in nodes] == [
            ("clarifier", "Which region?", "EU", "")
        ]

    @pytest.mark.asyncio
    async def test_launch_patches_reach_the_action_route_in_agent_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        real_update = h.update_campaign_status

        async def _prepare(cid: str) -> None:
            order.append("prepare")

        async def _launch(_request, cid: str, *, prepared: bool = False) -> None:
            order.append(f"launch prepared={prepared}")

        def _update(cid: str, status: str, **kw: Any) -> dict:
            order.append(f"status {getattr(status, 'value', status)}")
            return real_update(cid, status, **kw)

        monkeypatch.setattr(h, "_prepare_loop_launch", _prepare)
        monkeypatch.setattr(h, "_launch_loop", _launch)
        monkeypatch.setattr(h, "update_campaign_status", _update)
        cid = await asyncio.to_thread(_campaign)
        req = _req("PATCH", f"campaigns/{cid}", match={"id": cid}, body={"action": "start"})
        assert (await h._handle_action(req)).status == 200
        assert order == ["prepare", "status running", "launch prepared=True"]

    @pytest.mark.asyncio
    async def test_launch_patches_reach_the_action_route_in_workflow_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        prepare = AsyncMock()
        launch = AsyncMock()
        stop = AsyncMock()
        monkeypatch.setattr(h, "_prepare_loop_launch", prepare)
        monkeypatch.setattr(h, "_launch_workflow", launch)
        monkeypatch.setattr(h, "_stop_workflow", stop)
        cid = await asyncio.to_thread(_campaign, execution_mode="workflow")
        for action in ("start", "pause"):
            req = _req("PATCH", f"campaigns/{cid}", match={"id": cid}, body={"action": action})
            assert (await h._handle_action(req)).status == 200
        prepare.assert_not_awaited()
        assert launch.await_args.args[1] == cid
        assert stop.await_args.args[1] == cid

    @pytest.mark.asyncio
    async def test_stop_patch_reaches_the_delete_route(self, monkeypatch: pytest.MonkeyPatch):
        stop = AsyncMock()
        monkeypatch.setattr(h, "_stop_loop", stop)
        cid = await asyncio.to_thread(_campaign)
        response = await h._handle_delete(_req("DELETE", f"campaigns/{cid}", match={"id": cid}))
        assert _body(response) == {"id": cid, "deleted": True, "residual": False}
        stop.assert_awaited_once_with(cid, remove=True, stop_reason="campaign_deleted")

    @pytest.mark.asyncio
    async def test_watchdog_patches_reach_the_watchdog_loop(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(h, "POLL_INTERVAL", 0.01)
        enabled = {"value": False}
        monkeypatch.setattr(h, "is_app_enabled", lambda _name: enabled["value"])
        suspend = AsyncMock()
        monkeypatch.setattr(h, "_suspend_research_loops_while_disabled", suspend)
        poll = AsyncMock()
        monkeypatch.setattr(h, "_poll_workflow_campaign", poll)
        cid = await asyncio.to_thread(_campaign, execution_mode="workflow")
        await asyncio.to_thread(_running, cid)
        task = asyncio.ensure_future(h._watchdog_loop({"state": None}))
        try:
            assert await _await_until(lambda: suspend.await_count > 0)
            enabled["value"] = True
            assert await _await_until(lambda: poll.await_count > 0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert poll.await_args.args[0] == cid

    @pytest.mark.asyncio
    async def test_deadline_patch_reaches_the_watchdog_loop(
        self, sse: _Sink, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(h, "POLL_INTERVAL", 0.01)
        monkeypatch.setattr(h, "is_app_enabled", lambda _name: True)
        monkeypatch.setattr(h, "_unresponsive_deadline", lambda _idle: 0)
        cid = await asyncio.to_thread(_campaign, auto_approve=True)
        await asyncio.to_thread(_running, cid)
        task = asyncio.ensure_future(h._watchdog_loop({"state": None}))
        try:
            assert await _await_until(lambda: "failed" in sse.types())
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert (await asyncio.to_thread(_row, cid) or {})["status"] == "failed"


# --- one campaign's transitions stay ordered ---------------------------------


@pytest.mark.usefixtures("_no_autonudge")
class TestTransitionSerialization:
    @pytest.mark.asyncio
    async def test_concurrent_starts_are_single_flight(self, monkeypatch: pytest.MonkeyPatch):
        """The first start is held BEFORE it persists RUNNING, so only the
        transition lock can make the second one see its outcome."""
        entered = asyncio.Event()
        release = asyncio.Event()
        prepared: list[str] = []
        launches: list[str] = []

        async def _prepare(cid: str) -> None:
            prepared.append(cid)
            entered.set()
            await release.wait()

        async def _launch(_request, cid: str, *, prepared: bool = False) -> None:
            launches.append(cid)

        monkeypatch.setattr(h, "_prepare_loop_launch", _prepare)
        monkeypatch.setattr(h, "_launch_loop", _launch)
        cid = await asyncio.to_thread(_campaign)

        def _start() -> web.Request:
            return _req("PATCH", f"campaigns/{cid}", match={"id": cid}, body={"action": "start"})

        first = asyncio.ensure_future(h._handle_action(_start()))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.ensure_future(h._handle_action(_start()))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not second.done(), "the second start must wait for the first transition"
        release.set()
        first_response = await asyncio.wait_for(first, timeout=5)
        second_response = await asyncio.wait_for(second, timeout=5)
        assert first_response.status == 200
        assert second_response.status == 409
        assert _body(second_response) == {"error": "Cannot start a campaign in 'running' state"}
        assert (prepared, launches) == ([cid], [cid])

    @pytest.mark.asyncio
    async def test_expiry_for_a_replaced_run_is_refused(self, sse: _Sink):
        cid = await asyncio.to_thread(_campaign)
        started = await asyncio.to_thread(_running, cid, started_at=2000.0)
        await h._expire_trust(cid, 1000.0)
        assert (await asyncio.to_thread(_row, cid) or {})["status"] == "running"
        assert not (h._campaign_dir(cid) / "questions.json").exists()
        await h._expire_trust(cid, started)
        assert (await asyncio.to_thread(_row, cid) or {})["status"] == "needs_input"
        assert (h._campaign_dir(cid) / "questions.json").exists()
        assert await _await_until(lambda: sse.types() == ["needs_input"])

    @pytest.mark.asyncio
    async def test_settlement_for_a_replaced_run_touches_nothing(
        self, sse: _Sink, monkeypatch: pytest.MonkeyPatch
    ):
        loop = SimpleNamespace(id="L1", active=True)
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop), remove=AsyncMock(), update=AsyncMock()
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        cid = await asyncio.to_thread(_campaign)
        await asyncio.to_thread(_running, cid, started_at=2000.0)
        counts = {cid: 1}
        stamps = {cid: 5.0}
        await h._settle_campaign_from_watchdog(cid, [], counts, stamps, observed_started_at=1.0)
        assert (await asyncio.to_thread(_row, cid) or {})["status"] == "running"
        assert (counts, stamps) == ({cid: 1}, {cid: 5.0})
        svc.remove.assert_not_awaited()
        svc.update.assert_not_awaited()
        assert sse.events == []

    @pytest.mark.asyncio
    async def test_refused_guarded_transition_skips_its_commit_hook(self):
        cid = await asyncio.to_thread(_campaign)
        hooks: list[dict] = []
        result = await h._guarded_transition(
            cid,
            h.CampaignStatus.COMPLETE,
            allowed_current=(h.CampaignStatus.RUNNING,),
            on_commit=hooks.append,
        )
        assert result is None
        assert hooks == []

    @pytest.mark.asyncio
    async def test_commit_hook_runs_in_the_transaction_thread(self):
        cid = await asyncio.to_thread(_campaign)
        await asyncio.to_thread(_running, cid)
        threads: list[str] = []
        result = await h._guarded_transition(
            cid,
            h.CampaignStatus.PAUSED,
            allowed_current=(h.CampaignStatus.RUNNING,),
            on_commit=lambda _r: threads.append(threading.current_thread().name),
        )
        assert result == {"id": cid, "status": h.CampaignStatus.PAUSED}
        assert threads and threads[0] != threading.main_thread().name

    @pytest.mark.asyncio
    async def test_transition_locks_are_per_campaign_and_per_loop(self):
        a = h._campaign_transition_lock("0123abcd")
        assert h._campaign_transition_lock("0123abcd") is a
        assert h._campaign_transition_lock("89abcdef") is not a

        async def _lock_here() -> asyncio.Lock:
            return h._campaign_transition_lock("0123abcd")

        other = await asyncio.to_thread(asyncio.run, _lock_here())
        assert other is not a


# --- delete stays final --------------------------------------------------------


@pytest.mark.usefixtures("_no_autonudge")
class TestDeleteIsFinal:
    @pytest.mark.asyncio
    async def test_a_workflow_poll_in_flight_cannot_resurrect_a_deleted_campaign(self, sse: _Sink):
        cid = await asyncio.to_thread(_campaign, execution_mode="workflow")
        started = await asyncio.to_thread(_running, cid)
        await asyncio.to_thread(h._write_workflow_run_id, cid, "run-1")
        entered = threading.Event()
        release = threading.Event()
        snap = _investigation_snapshot("investigate: q", status="finished")

        def _result(_run_id: str) -> dict:
            entered.set()
            assert release.wait(5)
            return snap

        state = SimpleNamespace(
            workflow_service=SimpleNamespace(result=_result, cancel=AsyncMock())
        )
        poll = asyncio.ensure_future(h._poll_workflow_campaign(cid, state, started))
        assert await asyncio.to_thread(entered.wait, 5)
        response = await h._handle_delete(
            _req("DELETE", f"campaigns/{cid}", app=_app(state=state), match={"id": cid})
        )
        assert _body(response)["deleted"] is True
        release.set()
        await asyncio.wait_for(poll, timeout=5)
        assert not (h.research_dir() / cid).exists()
        assert await asyncio.to_thread(_row, cid) is None
        assert sse.events == []
        state.workflow_service.cancel.assert_awaited_once_with("run-1")

    @pytest.mark.asyncio
    async def test_late_watchdog_callbacks_do_not_recreate_state(self):
        cid = await asyncio.to_thread(_campaign)
        started = await asyncio.to_thread(_running, cid)
        finding = await asyncio.to_thread(_write_finding, cid, 1)
        assert (await asyncio.to_thread(h.delete_campaign, cid))["deleted"] is True
        await h._record_new_cycle_from_watchdog(cid, [finding], {}, {})
        await h._expire_trust(cid, started)
        hooks: list[dict] = []
        assert (
            await h._guarded_transition(
                cid,
                h.CampaignStatus.COMPLETE,
                allowed_current=(h.CampaignStatus.RUNNING,),
                expected_started_at=started,
                on_commit=hooks.append,
            )
            is None
        )
        assert await asyncio.to_thread(h.update_campaign_status, cid, "failed") == {
            "error": "campaign not found"
        }
        assert hooks == []
        assert not (h.research_dir() / cid).exists()
        assert await asyncio.to_thread(_row, cid) is None


# --- cycle-write failure residue ----------------------------------------------


@pytest.mark.usefixtures("_no_autonudge")
class TestCycleWriteFailure:
    @pytest.mark.asyncio
    async def test_workflow_batch_failure_keeps_residue_and_retries_the_rest(
        self, sse: _Sink, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        cid = await asyncio.to_thread(_campaign, execution_mode="workflow")
        started = await asyncio.to_thread(_running, cid)
        await asyncio.to_thread(h._write_workflow_run_id, cid, "run-1")
        snap = _investigation_snapshot("investigate: one", "investigate: two")
        state = SimpleNamespace(workflow_service=SimpleNamespace(result=lambda _rid: snap))
        findings = h._campaign_dir(cid) / "findings"
        real_write = Path.write_text

        def _failing_write(self: Path, *args: Any, **kwargs: Any) -> int:
            if self.name == "cycle_002.json":
                raise OSError("disk full")
            return real_write(self, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(Path, "write_text", _failing_write)
            with caplog.at_level(logging.ERROR, logger=_HANDLERS_LOGGER):
                await h._poll_workflow_campaign(cid, state, started)
        assert sorted(p.name for p in findings.iterdir()) == ["cycle_001.json"]
        first = (findings / "cycle_001.json").read_text(encoding="utf-8")
        assert (await asyncio.to_thread(_row, cid) or {})["total_cycles"] == 0
        assert sse.events == []
        assert any("workflow poll failed" in r.getMessage() for r in caplog.records)

        await h._poll_workflow_campaign(cid, state, started)
        assert sorted(p.name for p in findings.iterdir()) == ["cycle_001.json", "cycle_002.json"]
        assert (findings / "cycle_001.json").read_text(encoding="utf-8") == first
        campaign = await asyncio.to_thread(_row, cid)
        assert campaign is not None
        assert (campaign["total_cycles"], campaign["status"]) == (2, "running")
        assert sse.types() == ["new_finding"]
        assert sse.events[0]["finding"]["cycle"] == 2
        assert sse.events[0]["finding"]["key_insight"] == "two"

    @pytest.mark.asyncio
    async def test_agent_bookkeeping_failure_leaves_the_observation_unadvanced(
        self, sse: _Sink, monkeypatch: pytest.MonkeyPatch
    ):
        cid = await asyncio.to_thread(_campaign)
        finding = await asyncio.to_thread(_write_finding, cid, 1)

        def _locked() -> sqlite3.Connection:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(h, "_get_db", _locked)
        counts = {cid: 0}
        stamps = {cid: 1.0}
        with pytest.raises(sqlite3.OperationalError):
            await h._record_new_cycle_from_watchdog(cid, [finding], counts, stamps)
        assert (counts, stamps) == ({cid: 0}, {cid: 1.0})
        assert sse.events == []


# --- ordering of observable effects ------------------------------------------


@pytest.mark.usefixtures("_no_autonudge")
class TestEffectOrdering:
    @pytest.mark.asyncio
    async def test_finished_workflow_run_publishes_findings_before_completion(self, sse: _Sink):
        cid = await asyncio.to_thread(_campaign, execution_mode="workflow")
        started = await asyncio.to_thread(_running, cid)
        await asyncio.to_thread(h._write_workflow_run_id, cid, "run-1")
        snap = _investigation_snapshot("investigate: q", status="finished")
        state = SimpleNamespace(workflow_service=SimpleNamespace(result=lambda _rid: snap))
        await h._poll_workflow_campaign(cid, state, started)
        assert await _await_until(lambda: len(sse.events) == 2)
        assert sse.types() == ["new_finding", "complete"]
        report = (h._campaign_dir(cid) / "FINDINGS.md").read_text(encoding="utf-8")
        assert report == "final report"
        campaign = await asyncio.to_thread(_row, cid)
        assert campaign is not None
        assert (campaign["status"], campaign["total_cycles"]) == ("complete", 1)

    @pytest.mark.asyncio
    async def test_stall_settlement_persists_before_it_notifies(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        cid = await asyncio.to_thread(_campaign)
        started = await asyncio.to_thread(_running, cid)
        seen: list[tuple[str, str]] = []

        def _sink(event: dict) -> None:
            status_file = h._campaign_dir(cid) / "status.json"
            seen.append((event["type"], json.loads(status_file.read_text("utf-8"))["status"]))

        monkeypatch.setattr(h, "_emit_sse", _sink)
        await h._settle_campaign_from_watchdog(cid, [], {}, {}, observed_started_at=started)
        assert seen == [("failed", "failed")]


# --- logging -------------------------------------------------------------------


@pytest.mark.usefixtures("_no_autonudge")
class TestLogIdentity:
    @pytest.mark.asyncio
    async def test_engine_records_keep_the_historic_logger_name(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ):
        cid = await asyncio.to_thread(_campaign, execution_mode="workflow")
        started = await asyncio.to_thread(_running, cid)
        await asyncio.to_thread(h._write_workflow_run_id, cid, "run-1")

        def _boom(_run_id: str) -> dict:
            raise RuntimeError("engine down")

        state = SimpleNamespace(workflow_service=SimpleNamespace(result=_boom))
        monkeypatch.setattr(h, "_should_finalize", MagicMock(side_effect=RuntimeError("x")))
        with caplog.at_level(logging.WARNING):
            await h._poll_workflow_campaign(cid, state, started)
            await asyncio.to_thread(h._advance_exploration, cid)
            await h._launch_loop(_req("PATCH", f"campaigns/{cid}", app=_app()), cid)
        messages = {
            "workflow poll failed": None,
            "emergent exploration failed": None,
            "cannot launch loop": None,
        }
        for record in caplog.records:
            for fragment in messages:
                if fragment in record.getMessage():
                    messages[fragment] = record.name
        assert messages == dict.fromkeys(messages, _HANDLERS_LOGGER)


# --- redaction precedes bounding everywhere in the package -------------------


class TestRedactBeforeBoundAcrossThePackage:
    def test_no_bounded_slice_feeds_a_redactor_in_any_app_module(self):
        package = Path(h.__file__).resolve().parent
        modules = sorted(
            p
            for p in package.rglob("*.py")
            if "tests" not in p.relative_to(package).parts and "__pycache__" not in p.parts
        )
        assert Path(h.__file__).resolve() in modules
        redacting = [p for p in modules if "redact" in p.read_text(encoding="utf-8")]
        assert Path(h.__file__).resolve() in redacting
        offenders = [hit for p in modules for hit in _find_slice_inside_redact_call(p)]
        assert offenders == []
