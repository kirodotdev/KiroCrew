#!/usr/bin/env python3
"""Consolidation: the step that makes staged learnings actually reach reviews.

Reviews load ``learned-patterns.md`` only, so a candidate that is never merged has
no effect at all. Before this endpoint existed the app described consolidation it
could not perform, and staged learnings sat inert indefinitely.

The merge is a judgment call and runs as one worker turn; the APPLY is
deterministic. These tests pin that split — a chatty, truncated, or failed turn
must leave the ruleset exactly as it was.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from backend import routes
from sage_lib import learning, store


def _pattern(title: str) -> dict:
    return {"title": title, "scope": "common", "impact": "high",
            "guidance": f"Guidance for {title}."}


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        store.ensure_layout()
        routes._CONSOLIDATING.clear()
        routes._CONSOLIDATE_STATE.clear()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)


class _FakeRequest:
    """Minimal stand-in: the handler reads only the JSON body."""

    def __init__(self, body: dict | None = None, bad: bool = False):
        self._body = body or {}
        self._bad = bad

    async def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


def _text(resp: web.Response) -> str:
    return resp.text or ""


class TestConsolidateEndpoint(_Base):
    async def test_refuses_when_nothing_is_staged(self):
        resp = await routes._handle_consolidate(
            _FakeRequest({"namespace": "default"}))  # type: ignore[arg-type]
        self.assertEqual(resp.status, 409)
        self.assertIn("nothing to consolidate", _text(resp))

    async def test_refuses_an_invalid_namespace(self):
        # The namespace names a directory, so a traversal attempt must not reach
        # the filesystem helpers.
        resp = await routes._handle_consolidate(
            _FakeRequest({"namespace": "../../etc"}))  # type: ignore[arg-type]
        self.assertEqual(resp.status, 400)

    async def test_refuses_a_second_concurrent_merge(self):
        learning.stage_learning(_pattern("A"), "fix_introduce")
        routes._CONSOLIDATING.add("default")
        resp = await routes._handle_consolidate(
            _FakeRequest({"namespace": "default"}))  # type: ignore[arg-type]
        # Two merges could interleave writes and lose patterns.
        self.assertEqual(resp.status, 409)
        self.assertIn("already running", _text(resp))

    async def test_two_concurrent_requests_start_only_one_merge(self):
        """The guard has to claim the namespace BEFORE its first await.

        Checking membership and then awaiting the staged count left a window
        where both requests passed the guard, then both dispatched a merge
        against the same scratch path — the second overwriting or unlinking the
        first's output.
        """
        learning.stage_learning(_pattern("Pending"), "fix_introduce")
        started: list = []

        def capture_task(coro):
            """Record the dispatch and close the coroutine so it never runs.

            Returns a stand-in that supports ``add_done_callback``, because the
            dispatch keeps a strong ref to the task and registers a discard
            callback -- a real ``create_task`` never returns None.
            """
            started.append(coro)
            coro.close()
            return unittest.mock.Mock()

        with patch.object(routes.asyncio, "create_task",
                          side_effect=capture_task):
            first, second = await asyncio.gather(
                routes._handle_consolidate(_FakeRequest({})),
                routes._handle_consolidate(_FakeRequest({})),
            )

        codes = sorted([first.status, second.status])
        self.assertEqual(codes, [200, 409])
        self.assertEqual(len(started), 1)

    async def test_starts_a_merge_when_candidates_exist(self):
        learning.stage_learning(_pattern("A"), "fix_introduce")
        with patch.object(routes.asyncio, "create_task") as spawn:
            resp = await routes._handle_consolidate(
                _FakeRequest({"namespace": "default"}))  # type: ignore[arg-type]
            self.assertEqual(resp.status, 200)
            spawn.assert_called_once()
        self.assertIn("default", routes._CONSOLIDATING)

    async def test_a_missing_body_defaults_to_the_default_namespace(self):
        learning.stage_learning(_pattern("A"), "fix_introduce")
        with patch.object(routes.asyncio, "create_task"):
            resp = await routes._handle_consolidate(
                _FakeRequest(bad=True))  # type: ignore[arg-type]
        self.assertEqual(resp.status, 200)


class TestConsolidateMerge(_Base):
    """The background half: what the worker writes, and what is done with it."""

    def _pool(self, writes: str | None, ok: bool = True, error: str = ""):
        """Fake the pool so the 'worker' writes (or fails to write) a merge file."""
        ns_dir = learning._namespace_dir("default")
        out = Path(ns_dir) / "learned-patterns.merge.md"

        def dispatch(task, timeout=0, on_activity=None):
            if writes is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(writes, encoding="utf-8")
            return {"ok": ok, "output": "done", "error": error}

        pool = AsyncMock()
        pool.begin_batch = AsyncMock()
        pool.end_batch = AsyncMock()
        return dispatch, pool, out

    async def _run(self, writes: str | None, ok: bool = True, error: str = ""):
        dispatch, pool, out = self._pool(writes, ok, error)
        with patch.object(routes.review_pool, "get_pool", return_value=pool), \
                patch.object(routes.review_pool, "make_sync_dispatch",
                             return_value=dispatch):
            await routes._consolidate_bg("default")
        return out

    async def test_applies_the_merge_the_worker_wrote(self):
        learning.stage_learning(_pattern("Old lesson"), "fix_introduce")
        merged = (
            "# Common learned patterns\n\n"
            "### Sharpened lesson <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-07-29T00:00:00Z -->\nGuidance that survived.\n"
        )
        await self._run(merged)
        titles = [p["title"] for p in learning.list_patterns_for_review()]
        self.assertEqual(titles, ["Sharpened lesson"])
        # The candidate is consumed, so the next merge does not redo this one.
        self.assertEqual(learning.candidate_count(), 0)
        self.assertFalse(routes._CONSOLIDATE_STATE["default"]["running"])
        self.assertIsNone(routes._CONSOLIDATE_STATE["default"]["error"])

    async def test_a_worker_that_wrote_nothing_leaves_the_ruleset_alone(self):
        # The reviewer's memory must survive a failed merge: this is the case that
        # would otherwise wipe every learned pattern.
        learning.consolidate_apply(
            "# Common\n\n### Kept <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nStill here.\n")
        learning.stage_learning(_pattern("Pending"), "fix_introduce")
        await self._run(None, ok=False, error="turn failed")
        self.assertEqual([p["title"] for p in learning.list_patterns_for_review()],
                         ["Kept"])
        # And the candidate is NOT cleared, so nothing staged is lost either.
        self.assertEqual(learning.candidate_count(), 1)
        self.assertIn("turn failed", routes._CONSOLIDATE_STATE["default"]["error"])

    async def test_a_failed_worker_that_wrote_a_partial_ruleset_is_refused(self):
        # The dangerous shape: the turn FAILED, but it had already emitted one
        # valid pattern. Non-empty and parseable, so only spawn["ok"] can catch it.
        # Applying it would replace the full ruleset with this fragment and clear
        # the candidates, losing the omitted rules from both copies.
        learning.consolidate_apply(
            "# Common\n\n"
            "### Kept one <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nStill here.\n"
            "### Kept two <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nAlso still here.\n")
        learning.stage_learning(_pattern("Pending"), "fix_introduce")

        partial = (
            "# Common learned patterns\n\n"
            "### Kept one <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nStill here.\n"
        )
        await self._run(partial, ok=False, error="timed out mid-write")

        # Both rules survive, not just the one the partial file happened to carry.
        self.assertEqual(
            sorted(p["title"] for p in learning.list_patterns_for_review()),
            ["Kept one", "Kept two"])
        # And the staged candidate is still staged, so nothing pending is lost.
        self.assertEqual(learning.candidate_count(), 1)
        self.assertIn("timed out mid-write",
                      routes._CONSOLIDATE_STATE["default"]["error"])

    async def test_a_successful_worker_still_applies_its_merge(self):
        # The guard must not refuse legitimate merges: ok=True still applies.
        learning.stage_learning(_pattern("Pending"), "fix_introduce")
        merged = (
            "# Common learned patterns\n\n"
            "### Fresh rule <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-07-29T00:00:00Z -->\nApplied.\n"
        )
        await self._run(merged, ok=True)
        self.assertEqual([p["title"] for p in learning.list_patterns_for_review()],
                         ["Fresh rule"])
        self.assertIsNone(routes._CONSOLIDATE_STATE["default"]["error"])

    async def test_a_learning_staged_during_the_merge_survives(self):
        # Staged before the merge: this one IS represented in the merged ruleset.
        learning.stage_learning(_pattern("Known at dispatch"), "fix_introduce")

        ns_dir = learning._namespace_dir("default")
        out = Path(ns_dir) / "learned-patterns.merge.md"
        merged = (
            "# Common learned patterns\n\n"
            "### Known at dispatch <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-07-29T00:00:00Z -->\nFolded in.\n"
        )

        def dispatch(task, timeout=0, on_activity=None):
            # A concurrent review stages a NEW learning while the merge runs. The
            # worker has already read the candidate file, so this cannot appear in
            # `merged` — and must therefore not be cleared by this merge.
            learning.stage_learning(_pattern("Staged mid-merge"), "fix_introduce")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(merged, encoding="utf-8")
            return {"ok": True, "output": "done", "error": ""}

        pool = AsyncMock()
        pool.begin_batch = AsyncMock()
        pool.end_batch = AsyncMock()
        with patch.object(routes.review_pool, "get_pool", return_value=pool), \
                patch.object(routes.review_pool, "make_sync_dispatch",
                             return_value=dispatch):
            await routes._consolidate_bg("default")

        # The merge landed.
        self.assertEqual([p["title"] for p in learning.list_patterns_for_review()],
                         ["Known at dispatch"])
        # And the concurrently-staged learning is STILL staged, not silently gone.
        self.assertEqual([p["title"] for p in learning.list_candidate()],
                         ["Staged mid-merge"])
        self.assertIsNone(routes._CONSOLIDATE_STATE["default"]["error"])

    async def test_consolidating_everything_still_empties_the_candidate(self):
        # The selective clear must not leave consumed candidates behind forever.
        learning.stage_learning(_pattern("Only one"), "fix_introduce")
        merged = (
            "# Common learned patterns\n\n"
            "### Only one <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-07-29T00:00:00Z -->\nFolded in.\n"
        )
        await self._run(merged, ok=True)
        self.assertEqual(learning.candidate_count(), 0)

    async def test_an_empty_merge_file_is_refused(self):
        learning.stage_learning(_pattern("Pending"), "fix_introduce")
        await self._run("   \n")
        self.assertEqual(learning.candidate_count(), 1)
        self.assertTrue(routes._CONSOLIDATE_STATE["default"]["error"])

    async def test_a_symlinked_merge_file_is_refused(self):
        """The worker writes the merge file, so it can plant a symlink there.

        Following it would copy an arbitrary file into learned-patterns.md, which
        is rendered in the UI and injected into every later review prompt. The
        read goes through the hooks chokepoint, which opens O_NOFOLLOW.
        """
        secret = Path(self.tmp) / "outside-secret.txt"
        secret.write_text("### Stolen <!-- scope:common --> <!-- impact:high -->"
                          " <!-- added:2026-01-01T00:00:00Z -->\nleaked.\n",
                          encoding="utf-8")
        learning.consolidate_apply(
            "# Common\n\n### Kept <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nStill here.\n")
        learning.stage_learning(_pattern("Pending"), "fix_introduce")

        ns_dir = learning._namespace_dir("default")
        out = Path(ns_dir) / "learned-patterns.merge.md"

        def dispatch(task, timeout=0, on_activity=None):
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.exists() or out.is_symlink():
                out.unlink()
            out.symlink_to(secret)
            return {"ok": True, "output": "done", "error": ""}

        pool = AsyncMock()
        pool.begin_batch = AsyncMock()
        pool.end_batch = AsyncMock()
        with patch.object(routes.review_pool, "get_pool", return_value=pool), \
                patch.object(routes.review_pool, "make_sync_dispatch",
                             return_value=dispatch):
            await routes._consolidate_bg("default")

        # The ruleset is untouched and nothing from outside leaked into it.
        titles = [p["title"] for p in learning.list_patterns_for_review()]
        self.assertEqual(titles, ["Kept"])
        self.assertNotIn("Stolen", learning.common_file().read_text(encoding="utf-8"))
        self.assertEqual(learning.candidate_count(), 1)

    async def test_stale_merge_residue_is_not_applied(self):
        """A crash between the worker's write and the apply leaves the scratch file.

        The next consolidation whose worker produces nothing would otherwise read
        that stale output, apply it over the live ruleset, and clear a candidate
        that was never actually merged.
        """
        learning.consolidate_apply(
            "# Common\n\n### Kept <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nStill here.\n")
        learning.stage_learning(_pattern("Pending"), "fix_introduce")

        ns_dir = learning._namespace_dir("default")
        stale = Path(ns_dir) / "learned-patterns.merge.md"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(
            "### Stale <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nfrom a crashed run.\n",
            encoding="utf-8")

        def writes_nothing(task, timeout=0, on_activity=None):
            return {"ok": True, "output": "done", "error": ""}

        pool = AsyncMock()
        pool.begin_batch = AsyncMock()
        pool.end_batch = AsyncMock()
        with patch.object(routes.review_pool, "get_pool", return_value=pool), \
                patch.object(routes.review_pool, "make_sync_dispatch",
                             return_value=writes_nothing):
            await routes._consolidate_bg("default")

        titles = [p["title"] for p in learning.list_patterns_for_review()]
        self.assertEqual(titles, ["Kept"])
        self.assertNotIn("Stale", learning.common_file().read_text(encoding="utf-8"))
        # The candidate is still staged, because nothing was merged.
        self.assertEqual(learning.candidate_count(), 1)

    async def test_the_scratch_file_is_removed(self):
        learning.stage_learning(_pattern("A"), "fix_introduce")
        out = await self._run(
            "# C\n\n### T <!-- scope:common --> <!-- impact:low -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nG.\n")
        # Left behind, the next run would read a stale merge as this run's output.
        self.assertFalse(out.exists())

    async def test_the_claim_is_released_even_when_the_merge_fails(self):
        learning.stage_learning(_pattern("A"), "fix_introduce")
        routes._CONSOLIDATING.add("default")
        with patch.object(routes.review_pool, "get_pool",
                          side_effect=RuntimeError("no pool")):
            await routes._consolidate_bg("default")
        # Otherwise the namespace could never be consolidated again this process.
        self.assertNotIn("default", routes._CONSOLIDATING)
        self.assertIn("no pool", routes._CONSOLIDATE_STATE["default"]["error"])


class TestConsolidationPrompt(unittest.TestCase):
    def test_the_prompt_names_both_inputs_and_the_output(self):
        from sage_lib import review_driver as D
        task = D.build_consolidation_task(
            "default", "/live.md", "/cand.md", "/out.md")
        for token in ("/live.md", "/cand.md", "/out.md", "default"):
            self.assertIn(token, task)

    def test_the_prompt_forbids_dropping_rules_casually(self):
        from sage_lib import review_driver as D
        task = D.build_consolidation_task("ns", "a", "b", "c")
        # The ruleset IS the reviewer's memory; a merge that silently drops rules
        # loses lessons permanently.
        self.assertIn("Keep every current pattern", task)
        self.assertIn("code-agnostic", task)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestConcurrentStagingKeepsEveryLearning(unittest.TestCase):
    """Two reviews staging at once must not overwrite each other.

    ``stage_learning`` reads the candidate file, appends one pattern, and rewrites
    the whole file. The write is atomic; the read-modify-write is not. The lock
    around it is what makes concurrent staging additive.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "app"
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pat(self, title):
        return {"title": title, "scope": "common", "impact": "high",
                "dimension": "correctness",
                "guidance": f"Guidance for {title}."}

    def test_concurrent_stagers_do_not_drop_a_learning(self):
        # Widen the read->write window so the race is deterministic, not luck.
        real_write = learning._atomic_write

        def slow_write(path, body):
            time.sleep(0.02)
            return real_write(path, body)

        titles = [f"Lesson {i}" for i in range(8)]
        errors: list[BaseException] = []

        def stage(t):
            try:
                learning.stage_learning(self._pat(t), "fix_introduce", self.root)
            except BaseException as e:       # pragma: no cover - surfaced below
                errors.append(e)

        with unittest.mock.patch.object(learning, "_atomic_write", slow_write):
            threads = [threading.Thread(target=stage, args=(t,)) for t in titles]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

        self.assertEqual(errors, [])
        staged = sorted(p["title"] for p in learning.list_candidate(self.root))
        self.assertEqual(staged, sorted(titles),
                         "a concurrently staged learning was overwritten")
