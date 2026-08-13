"""Off-loop DB discipline for the auto_research campaigns DB.

Every connection from ``_get_db()`` carries a 30s busy timeout, and the
watchdog writes the campaigns table every cycle while HTTP handlers read and
write the same rows — so a single ``_get_db()`` entered directly on the
asyncio event loop can freeze the whole gateway past the loop-stall watchdog
budget (25s) and hard-exit the process. These tests pin the discipline from
three directions:

1. the runtime chokepoint guard in ``_get_db()`` (strict raise / production
   warn / off-loop no-op),
2. a static AST ratchet: no ``async def`` in handlers.py calls a DB-touching
   function directly (the offload pattern is ``asyncio.to_thread`` /
   ``run_in_executor``, optionally via a nested sync helper),
3. an end-to-end contention proof: a held write lock on the campaigns DB
   stalls the affected handler, not the event loop's heartbeat.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.auto_research import handlers as h
from kiro_crew.apps.builtins.auto_research.handlers import (
    CampaignStatus,
    OnLoopDBError,
    _get_db,
    create_campaign,
    get_campaign,
    register_routes,
    update_campaign_status,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    """Isolate DB and research dir per test (same shape as test_auto_research)."""
    with (
        patch(
            "kiro_crew.apps.builtins.auto_research.handlers.DB_PATH",
            tmp_path / "test.db",
        ),
        patch(
            "kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR",
            tmp_path / "research",
        ),
    ):
        yield tmp_path


class TestOnLoopGuard:
    """The runtime chokepoint: ``_get_db()`` flags on-loop entry."""

    @pytest.mark.asyncio
    async def test_on_loop_get_db_raises_under_strict(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "1")
        with pytest.raises(OnLoopDBError):
            _get_db()

    def test_off_loop_get_db_allowed_under_strict(self, monkeypatch):
        """No running loop (worker thread / executor / CLI) is the sanctioned
        path — strict mode must not flag it."""
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "1")
        conn = _get_db()
        try:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_on_loop_get_db_warns_in_production_mode(self, monkeypatch, caplog):
        """Strict off (production): the on-loop entry proceeds but logs loudly,
        so a mis-wired call-site is never silent."""
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "0")
        monkeypatch.setattr(h, "_on_loop_db_warn_last", 0.0)  # reset throttle window
        with caplog.at_level("WARNING", logger=h.logger.name):
            conn = _get_db()
            conn.close()
        assert any("event loop" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_on_loop_warning_is_throttled(self, monkeypatch, caplog):
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "0")
        monkeypatch.setattr(h, "_on_loop_db_warn_last", 0.0)
        with caplog.at_level("WARNING", logger=h.logger.name):
            for _ in range(3):
                conn = _get_db()
                conn.close()
        assert sum("event loop" in r.message for r in caplog.records) == 1

    def test_get_db_enters_the_guard(self):
        """Mutation guard: removing the discipline check from ``_get_db``
        silently disarms every other protection here — pin the call."""
        tree = ast.parse(inspect.getsource(h._get_db))
        fn = tree.body[0]
        calls = [
            n.func.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        assert "_check_on_loop_db_discipline" in calls


# Functions that open (or transitively open) the campaigns DB. A direct call
# to any of these inside an ``async def`` in handlers.py runs the 30s busy
# timeout on the event loop. Kept honest by test_db_touching_set_is_current
# below, which recomputes the closure from the AST.
_DB_TOUCHING_FNS = frozenset(
    {
        "_get_db",
        "_guarded_txn",
        "update_campaign_status",
        "delete_campaign",
        "create_campaign",
        "get_campaign",
        "list_campaigns",
        "validate_campaign",
        "_campaign_execution_mode",
        "_campaign_run_has_status",
        "_campaign_run_is_current",
        "_persist_new_cycle_bookkeeping",
        "_should_finalize",
        "_ingest_emergent_questions",
        "_activate_emergent",
        "_advance_exploration",
    }
)


def _module_tree() -> ast.Module:
    src = Path(inspect.getsourcefile(h)).read_text(encoding="utf-8")
    return ast.parse(src)


class TestStaticRatchet:
    def test_db_touching_set_is_current(self):
        """Drift guard: ``_DB_TOUCHING_FNS`` must equal the transitive closure
        of top-level sync functions that reach ``_get_db``. A new sync DB
        helper added to handlers.py without extending the set would make the
        main ratchet below scan with a blind spot — fail loudly instead."""
        tree = _module_tree()
        calls: dict[str, set[str]] = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                calls[node.name] = {
                    n.func.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
        touching = {"_get_db"}
        changed = True
        while changed:
            changed = False
            for name, called in calls.items():
                if name not in touching and called & touching:
                    touching.add(name)
                    changed = True
        assert touching == set(_DB_TOUCHING_FNS), (
            "sync DB-helper closure drifted from _DB_TOUCHING_FNS — update the "
            f"set. computed-only: {sorted(touching - set(_DB_TOUCHING_FNS))}, "
            f"set-only: {sorted(set(_DB_TOUCHING_FNS) - touching)}"
        )

    def test_no_async_def_calls_db_functions_directly(self):
        """AST ratchet: every DB touch from async code must be offloaded.

        Enforced shape: inside an ``async def``, a DB-touching function may
        only appear as an ARGUMENT to ``asyncio.to_thread`` /
        ``run_in_executor`` — never called directly. A nested sync ``def``
        whose body touches the DB is the other offload pattern; it must
        itself be passed to ``to_thread``/``run_in_executor`` in the
        enclosing async def, and a direct in-line call to it (``row =
        _read_row()``) is flagged, since that re-runs the 30s busy wait on
        the loop while staying invisible to a name-based scan.
        """
        tree = _module_tree()
        violations: list[str] = []

        def _body_touches_db(fn: ast.FunctionDef) -> bool:
            for n in ast.walk(fn):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id in _DB_TOUCHING_FNS
                ):
                    return True
            return False

        def _is_offload_call(n: ast.Call) -> bool:
            return isinstance(n.func, ast.Attribute) and n.func.attr in (
                "to_thread",
                "run_in_executor",
            )

        def scan(node: ast.AsyncFunctionDef) -> None:
            db_closures = {
                child.name
                for child in ast.walk(node)
                if isinstance(child, ast.FunctionDef) and _body_touches_db(child)
            }
            offloaded: set[str] = set()
            flagged = _DB_TOUCHING_FNS | db_closures
            stack = list(ast.iter_child_nodes(node))
            while stack:
                n = stack.pop()
                if isinstance(n, ast.FunctionDef):
                    continue  # nested sync helper body runs off-loop when offloaded
                if isinstance(n, ast.Call):
                    if _is_offload_call(n):
                        offloaded.update(
                            a.id for a in n.args if isinstance(a, ast.Name) and a.id in flagged
                        )
                    elif isinstance(n.func, ast.Name) and n.func.id in flagged:
                        violations.append(f"{node.name}:{n.lineno} calls {n.func.id}() on the loop")
                stack.extend(ast.iter_child_nodes(n))
            for name in sorted(db_closures - offloaded):
                violations.append(
                    f"{node.name}: nested DB helper {name}() is defined but never "
                    "passed to asyncio.to_thread / run_in_executor"
                )

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                scan(node)
        assert not violations, (
            "direct campaigns-DB call(s) on the event loop (offload via "
            "asyncio.to_thread / run_in_executor):\n" + "\n".join(violations)
        )


class TestContention:
    @pytest.fixture
    def app(self, tmp_path: Path):
        @web.middleware
        async def _inject_user(request, handler):
            request["user"] = "test-user"
            return await handler(request)

        a = web.Application(middlewares=[_inject_user])
        register_routes(a)
        return a

    @pytest.mark.asyncio
    async def test_held_write_lock_stalls_handler_not_heartbeat(self, app, tmp_path: Path):
        """A write lock held on the campaigns DB must stall only the request
        that needs the lock — never the event loop. This is the production
        failure shape: the watchdog writes every cycle, a user clicks Pause
        mid-write, and before the fix the handler's 30s busy wait ran ON the
        loop, silencing the gateway heartbeat past the watchdog kill budget.
        """
        async with TestClient(TestServer(app)) as c:
            cr = await c.post(
                "/api/apps/auto-research/campaigns",
                json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
            )
            assert cr.status == 201
            cid = (await cr.json())["id"]
            # Pause is only legal from RUNNING.
            await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)

            # Hold the campaigns-DB write lock like a mid-write watchdog cycle.
            blocker = sqlite3.connect(str(tmp_path / "test.db"))
            blocker.execute("BEGIN IMMEDIATE")

            ticks = 0

            async def heartbeat() -> None:
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0.02)

            hb = asyncio.create_task(heartbeat())
            req = asyncio.create_task(
                c.patch(f"/api/apps/auto-research/campaigns/{cid}", json={"action": "pause"})
            )
            try:
                await asyncio.sleep(0.6)
                # The handler is genuinely blocked on the contended write...
                assert not req.done(), "handler finished despite a held write lock"
                # ...while the event loop stayed live (~30 ticks expected; >=5
                # is a generous slow-CI floor — an on-loop 30s busy wait yields
                # 0-1 because this sleep itself cannot run either).
                assert ticks >= 5, f"event loop starved during contended DB write (ticks={ticks})"
            finally:
                blocker.rollback()
                blocker.close()
                hb.cancel()
            resp = await req
            assert resp.status == 200
            assert (await resp.json())["status"] == CampaignStatus.PAUSED


class TestAddQuestionAtomicity:
    """The one read-modify-write among the offloaded sites: concurrent appends
    must serialize (write lock before read), never lose a question."""

    @pytest.fixture
    def app(self, tmp_path: Path):
        @web.middleware
        async def _inject_user(request, handler):
            request["user"] = "test-user"
            return await handler(request)

        a = web.Application(middlewares=[_inject_user])
        register_routes(a)
        return a

    @pytest.mark.asyncio
    async def test_concurrent_add_question_loses_nothing(self, app):
        async with TestClient(TestServer(app)) as c:
            cr = await c.post(
                "/api/apps/auto-research/campaigns",
                json={"question": "What drives adoption of edge computing?", "sources": ["web"]},
            )
            assert cr.status == 201
            cid = (await cr.json())["id"]
            n = 8
            resps = await asyncio.gather(
                *(
                    c.post(
                        f"/api/apps/auto-research/campaigns/{cid}/questions",
                        json={"text": f"q-{i}"},
                    )
                    for i in range(n)
                )
            )
            assert all(r.status == 200 for r in resps)

            def _read_subs() -> list:
                import json as _json

                db = _get_db()
                try:
                    row = db.execute(
                        "SELECT sub_questions FROM campaigns WHERE id = ?", (cid,)
                    ).fetchone()
                    return _json.loads(row["sub_questions"] or "[]")
                finally:
                    db.close()

            subs = await asyncio.to_thread(_read_subs)
            manual = {s["text"] for s in subs if s.get("origin") == "manual"}
            assert manual == {f"q-{i}" for i in range(n)}, (
                f"lost questions under concurrency: {sorted(manual)}"
            )


class TestGuardedTransition:
    """Background transitions must not overwrite a user action that committed
    during the thread hop (Stop wins over a stale watchdog/nudge observation)."""

    @pytest.mark.asyncio
    async def test_stale_observation_is_refused(self):
        cid = create_campaign({"question": "Does edge caching reduce latency?", "sources": ["web"]})["id"]
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)
        # User Stop commits between the observer's read and its write.
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.STOPPED)
        result = await h._guarded_transition(
            cid, CampaignStatus.NEEDS_INPUT, allowed_current=(CampaignStatus.RUNNING,)
        )
        assert result is None, "stale transition must be refused"
        row = await asyncio.to_thread(get_campaign, cid)
        assert row["status"] == CampaignStatus.STOPPED

    @pytest.mark.asyncio
    async def test_current_observation_proceeds(self):
        cid = create_campaign({"question": "Does edge caching reduce latency?", "sources": ["web"]})["id"]
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)
        result = await h._guarded_transition(
            cid, CampaignStatus.NEEDS_INPUT, allowed_current=(CampaignStatus.RUNNING,)
        )
        assert result is not None and result["status"] == CampaignStatus.NEEDS_INPUT

    @pytest.mark.asyncio
    async def test_nudge_does_not_resurrect_a_stopped_campaign(self, monkeypatch):
        """GPT scenario end-to-end: Stop committed, then a nudge whose question
        file still exists tries to restore RUNNING — the campaign stays STOPPED
        (a RUNNING row with no worker would be a zombie the watchdog re-adopts)."""
        cid = create_campaign({"question": "Does edge caching reduce latency?", "sources": ["web"]})["id"]
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.STOPPED)
        result = await h._guarded_transition(
            cid, CampaignStatus.RUNNING, allowed_current=(CampaignStatus.NEEDS_INPUT,)
        )
        assert result is None
        row = await asyncio.to_thread(get_campaign, cid)
        assert row["status"] == CampaignStatus.STOPPED

    @pytest.mark.asyncio
    async def test_refused_expiry_leaves_no_question_file(self, tmp_path):
        """GPT round-2 scenario: 24h expiry races a user Stop. When the guarded
        transition is refused, no synthetic question file may remain — it would
        drag a later Resume straight back into NEEDS_INPUT."""
        cid = create_campaign({"question": "Does edge caching reduce latency?", "sources": ["web"]})["id"]
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)
        row = await asyncio.to_thread(get_campaign, cid)
        observed = row["started_at"]
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.STOPPED)
        await h._expire_trust(cid, observed)
        qp = h._questions_path(cid)
        assert qp is None or not qp.exists(), "refused expiry left a stale question file"
        row = await asyncio.to_thread(get_campaign, cid)
        assert row["status"] == CampaignStatus.STOPPED

    @pytest.mark.asyncio
    async def test_successful_expiry_writes_question_and_parks(self, tmp_path):
        cid = create_campaign({"question": "Does edge caching reduce latency?", "sources": ["web"]})["id"]
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)
        row = await asyncio.to_thread(get_campaign, cid)
        await h._expire_trust(cid, row["started_at"])
        row = await asyncio.to_thread(get_campaign, cid)
        assert row["status"] == CampaignStatus.NEEDS_INPUT
        qp = h._questions_path(cid)
        assert qp is not None and qp.exists()
        assert "re-authorize" in qp.read_text()

    @pytest.mark.asyncio
    async def test_expiry_prompt_survives_a_directory_squatting_on_its_path(self, tmp_path):
        """GPT round-7: the agent controls the research dir and can leave a
        directory (or link) at questions.json. The expiry write must clear it
        and publish the prompt — and even if the write fails, the audit + SSE
        for the already-persisted NEEDS_INPUT must not be suppressed."""
        cid = create_campaign({"question": "Does edge caching reduce latency?", "sources": ["web"]})["id"]
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)
        row = await asyncio.to_thread(get_campaign, cid)
        qp = h._questions_path(cid)
        assert qp is not None
        qp.mkdir(parents=True)  # squat a directory on the prompt path
        await h._expire_trust(cid, row["started_at"])
        row = await asyncio.to_thread(get_campaign, cid)
        assert row["status"] == CampaignStatus.NEEDS_INPUT
        assert qp.is_file(), "expiry prompt was not published over the squatting dir"
        assert "re-authorize" in qp.read_text()

    @pytest.mark.asyncio
    async def test_commit_side_effects_survive_cancellation(self):
        """CI-repro (cycle-cap test flake): the awaiting frame can be CANCELLED
        at the ``to_thread`` suspension point AFTER the transition committed —
        a success-branch after the await is then silently skipped and the SSE
        for a persisted transition is lost. ``on_commit`` runs in the txn
        thread, so the side effect survives the cancel deterministically."""
        import threading

        cid = create_campaign({"question": "Does edge caching reduce latency?", "sources": ["web"]})["id"]
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)
        row = await asyncio.to_thread(get_campaign, cid)

        committed = threading.Event()
        release = threading.Event()
        emitted: list[dict] = []

        def _on_commit(_result: dict) -> None:
            emitted.append({"type": "complete", "campaign_id": cid})
            committed.set()
            release.wait(timeout=5)  # hold the txn thread so the cancel lands first

        task = asyncio.ensure_future(
            h._guarded_transition(
                cid,
                CampaignStatus.COMPLETE,
                allowed_current=(CampaignStatus.RUNNING,),
                expected_started_at=row["started_at"],
                on_commit=_on_commit,
            )
        )
        # Wait (off-loop) until the commit + side effect ran in the txn thread.
        assert await asyncio.to_thread(committed.wait, 5), "txn never committed"
        task.cancel()  # cancel the awaiting frame at its suspension point
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = await asyncio.to_thread(get_campaign, cid)
        assert row["status"] == CampaignStatus.COMPLETE, "commit was lost"
        assert emitted, "commit-time side effect was lost to the cancellation"

    @pytest.mark.asyncio
    async def test_stale_workflow_poll_writes_nothing_into_a_replacement_run(self):
        """GPT round-5 F1: a poll that read its snapshot against generation A
        must abort at lock entry when generation B replaced it — no cycle
        files, no bookkeeping, no terminal state may land in the new run."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        cid = create_campaign(
            {
                "question": "Does edge caching reduce latency?",
                "sources": ["web"],
                "execution_mode": "workflow",
            }
        )["id"]
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)
        row = await asyncio.to_thread(get_campaign, cid)
        old_generation = row["started_at"]
        h._write_workflow_run_id(cid, "run-1")
        # Pause → Resume replaces the run generation.
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.PAUSED)
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)
        snap = {
            "status": "finished",
            "result": {"report": "# stale report"},
            "events": [],
        }
        state = SimpleNamespace(
            workflow_service=SimpleNamespace(result=MagicMock(return_value=snap))
        )
        await h._poll_workflow_campaign(cid, state, old_generation)
        row = await asyncio.to_thread(get_campaign, cid)
        assert row["status"] == CampaignStatus.RUNNING, (
            "stale poll terminated the replacement run"
        )
        findings = h._campaign_dir(cid) / "FINDINGS.md"
        assert not findings.exists(), "stale poll wrote FINDINGS.md into the replacement run"

    @pytest.mark.asyncio
    async def test_stale_generation_is_refused_even_when_status_matches(self):
        """GPT round-4 ABA scenario: a Pause→Resume mints a NEW started_at, so
        the status is RUNNING again — but an old run's verdict carrying the OLD
        generation must not terminate the replacement run."""
        cid = create_campaign({"question": "Does edge caching reduce latency?", "sources": ["web"]})["id"]
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)
        row = await asyncio.to_thread(get_campaign, cid)
        old_generation = row["started_at"]
        # Pause → Resume: RUNNING again, but a NEW generation.
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.PAUSED)
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)
        row = await asyncio.to_thread(get_campaign, cid)
        assert row["started_at"] != old_generation, "resume must mint a new generation"
        result = await h._guarded_transition(
            cid,
            CampaignStatus.COMPLETE,
            allowed_current=(CampaignStatus.RUNNING,),
            expected_started_at=old_generation,
        )
        assert result is None, "stale-generation verdict terminated the replacement run"
        row = await asyncio.to_thread(get_campaign, cid)
        assert row["status"] == CampaignStatus.RUNNING

    @pytest.mark.asyncio
    async def test_current_generation_proceeds(self):
        cid = create_campaign({"question": "Does edge caching reduce latency?", "sources": ["web"]})["id"]
        await asyncio.to_thread(update_campaign_status, cid, CampaignStatus.RUNNING)
        row = await asyncio.to_thread(get_campaign, cid)
        result = await h._guarded_transition(
            cid,
            CampaignStatus.COMPLETE,
            allowed_current=(CampaignStatus.RUNNING,),
            expected_started_at=row["started_at"],
        )
        assert result is not None and result["status"] == CampaignStatus.COMPLETE

    def test_background_transitions_route_through_the_guard(self):
        """Ratchet: the watchdog / nudge / workflow-poll frames must not call
        ``update_campaign_status`` directly (even offloaded) — a raw offloaded
        write skips the lock + allowed_current re-check and reintroduces the
        lost-serialization race. ``_launch_workflow`` runs under
        ``_handle_action``'s transition lock, so its direct writes are exempt
        (the lock is not reentrant)."""
        tree = _module_tree()
        background = {
            "_watchdog_loop",
            "_handle_nudge",
            "_poll_workflow_campaign",
            "_record_new_cycle_from_watchdog",
        }
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in background:
                for n in ast.walk(node):
                    if (
                        isinstance(n, ast.Name)
                        and n.id == "update_campaign_status"
                        and isinstance(n.ctx, ast.Load)
                    ):
                        offenders.append(f"{node.name}:{n.lineno}")
        assert not offenders, (
            "background frame references update_campaign_status directly "
            f"(use _guarded_transition): {offenders}"
        )


class TestBriefTransactionality:
    """brief.md publication invariant (two failure modes, both pinned):

    1. Never publish BEFORE commit — a rollback/crash after the file write
       would leave a brief describing phantom (uncommitted) state.
    2. Publish order must match commit order — a stale snapshot must not
       overwrite a newer brief. The per-campaign ``_brief_publish_lock`` spans
       commit→publish in every producer, giving both properties at once.
    """

    def _producer_source(self, fn) -> str:
        import textwrap

        return textwrap.dedent(inspect.getsource(fn))

    def test_launch_publishes_after_commit_under_the_lock(self):
        src = self._producer_source(h._launch_loop)
        assert "_brief_publish_lock" in src, "launch producer lost the publish lock"
        closure = src[src.index("_brief_publish_lock") :]
        assert closure.index("db.commit()") < closure.index("_write_brief("), (
            "launch publishes the brief before its transaction commits"
        )

    def test_append_question_publishes_after_commit_under_the_lock(self):
        src = self._producer_source(h._handle_add_question)
        assert "_brief_publish_lock" in src, "append producer lost the publish lock"
        assert src.index("db.commit()") < src.index("_write_brief("), (
            "_handle_add_question publishes the brief before commit"
        )

    def test_activate_emergent_publishes_after_commit_under_the_lock(self):
        src = self._producer_source(h._activate_emergent)
        assert "_brief_publish_lock" in src, "emergent producer lost the publish lock"
        assert src.index("db.commit()") < src.index("_write_brief("), (
            "_activate_emergent publishes the brief before commit"
        )

    def test_emergent_ledger_persists_before_the_brief_publish(self):
        """GPT round-6: the dedup ledger (mark_analyzed + save_queue) must be
        persisted BEFORE the brief write — a failing brief write must not lose
        the activation record, or the same items are re-activated (duplicated)
        next cycle. Pinned structurally: source order within _activate_emergent
        is commit → mark_analyzed → save_queue → _write_brief."""
        src = inspect.getsource(h._activate_emergent)
        i_commit = src.index("db.commit()")
        i_mark = src.index("_sq.mark_analyzed(")
        i_save = src.index("_sq.save_queue(")
        i_brief = src.index("_write_brief(")
        assert i_commit < i_mark < i_save < i_brief, (
            "emergent activation order must be commit -> ledger -> brief publish"
        )

    def test_every_write_brief_producer_holds_the_publish_lock(self):
        """Drift guard: any function that calls _write_brief must also acquire
        _brief_publish_lock (rendering helpers and the lock factory exempt)."""
        tree = _module_tree()
        offenders = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in ("_write_brief", "_brief_publish_lock"):
                    continue
                names = {
                    n.func.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
                if "_write_brief" in names and "_brief_publish_lock" not in names:
                    offenders.append(node.name)
        assert not offenders, f"_write_brief called without the publish lock in: {offenders}"


class TestWarnThrottleClock:
    def test_throttle_uses_monotonic_clock(self):
        """The warn throttle must use time.monotonic (wall-clock jumps must not
        re-open or permanently close the throttle window)."""
        src = inspect.getsource(h._check_on_loop_db_discipline)
        assert "time.monotonic()" in src
        assert time.monotonic  # imported and real
