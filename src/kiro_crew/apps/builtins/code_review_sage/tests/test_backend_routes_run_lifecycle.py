"""Backend-route tests: a review run from preflight to retention.

The runtime is checked before anything spawns, and runs and reviewers execute one at a time.
Only a fully delivered auto-post lets the result records be cleaned up: a partial or failed
one keeps the records the retry needs, and a successful retry repairs the reviewed index. A
restart clears a stranded posting flag, the startup reap and layout pass stay off the event
loop, retention never evicts a live run, and the skipped reasons the failure mapping knows
render distinct, cause-naming sentences.
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

import pytest
from aiohttp import web
from sage_lib import review_driver as _rd
from sage_lib.review_driver import _all_delivered

from kiro_crew.apps.builtins.code_review_sage.tests.backend_routes_helpers import (
    _load_routes_module,
)


async def _noop_save() -> None:
    """Stand-in for ``routes._save_runs``, which is a coroutine.

    The registry write is offloaded to a worker thread because it is blocking
    file IO (see ``_write_runs``), so a plain ``lambda: None`` stub is
    not awaitable and the patched call site would raise instead of no-op.
    """


class TestRecordsSurviveAnIncompletePost:
    """Records are the only source of the redacted comment payload.

    ``auto_post`` is the INTENT to post, not evidence of delivery. When a post
    half-fails the records must stay, or the explicit "post comments" retry has
    nothing to send while the PR carries only some of the findings.
    """

    def _rec(self, **over):
        base = {
            "result_recorded": True,
            "cancelled": False,
            "post_ok": True,
            "posted_comments": 3,
            "posting_expected": 3,
        }
        base.update(over)
        return base

    def test_full_delivery_allows_the_cleanup(self):

        assert _all_delivered([self._rec(), self._rec()]) is True

    def test_a_failed_post_keeps_the_records(self):

        assert _all_delivered([self._rec(), self._rec(post_ok=False)]) is False

    def test_a_partial_post_keeps_the_records(self):

        # post_ok True but fewer comments landed than the run expected.
        assert _all_delivered([self._rec(posted_comments=1, posting_expected=3)]) is False

    def test_nothing_to_deliver_does_not_block_the_cleanup(self):

        # A cancelled change and one that recorded no result have nothing to post.
        assert (
            _all_delivered(
                [
                    self._rec(cancelled=True, post_ok=False, posted_comments=0),
                    self._rec(result_recorded=False, post_ok=False, posted_comments=0),
                ]
            )
            is True
        )

    def test_missing_counters_are_not_read_as_delivered(self):

        # A record that never reached the posting arm must not count as delivered.
        assert _all_delivered([{"result_recorded": True}]) is False


class TestRetentionKeepsActiveRuns(unittest.IsolatedAsyncioTestCase):
    """Retention is by position, so a still-RUNNING run can sit past the cap.

    Evicting it deleted the subtree under a live review: its endpoints 404'd and
    the report it was about to write was orphaned. Terminal runs are evictable;
    running ones are kept even if that holds the registry above the cap.
    """

    def setUp(self):
        self.mod = _load_routes_module()
        self.removed: list[str] = []
        self.mod._RUNS[:] = []

    async def _record_many(self, statuses):
        with (
            unittest.mock.patch.object(self.mod, "_save_runs", _noop_save),
            unittest.mock.patch.object(
                self.mod.store, "remove_run_dir", lambda rid, *a, **k: self.removed.append(rid)
            ),
        ):
            for i, st in enumerate(statuses):
                await self.mod._record({"run_id": f"run-{i}", "status": st})

    async def test_an_active_run_past_the_cap_is_not_evicted(self):
        cap = self.mod._RUNS_MAX
        # Oldest is recorded first and ends up last, i.e. past the cap.
        await self._record_many(["running"] + ["done"] * cap)

        self.assertNotIn("run-0", self.removed, "a running run's subtree was deleted")
        ids = [r["run_id"] for r in self.mod._RUNS]
        self.assertIn("run-0", ids, "a running run was dropped from the registry")

    async def test_a_posting_run_past_the_cap_is_not_evicted(self):
        # Posting happens AFTER the run reaches a terminal status, so status alone
        # says "done" while the poster is still delivering to the pull request.
        # Evicting it deletes the subtree mid-delivery and loses the record of what
        # landed. The delete handler already refused this; retention did not.
        cap = self.mod._RUNS_MAX
        with (
            unittest.mock.patch.object(self.mod, "_save_runs", _noop_save),
            unittest.mock.patch.object(
                self.mod.store, "remove_run_dir", lambda rid, *a, **k: self.removed.append(rid)
            ),
        ):
            await self.mod._record({"run_id": "run-0", "status": "done", "posting": True})
            for i in range(1, cap + 1):
                await self.mod._record({"run_id": f"run-{i}", "status": "done"})

        self.assertNotIn("run-0", self.removed, "a posting run's subtree was deleted mid-delivery")
        self.assertIn("run-0", [r["run_id"] for r in self.mod._RUNS])

    async def test_the_same_run_is_evictable_once_posting_finishes(self):
        # The guard must not pin the run forever: once posting clears, it is
        # terminal and reclaimable on the next _record.
        cap = self.mod._RUNS_MAX
        with (
            unittest.mock.patch.object(self.mod, "_save_runs", _noop_save),
            unittest.mock.patch.object(
                self.mod.store, "remove_run_dir", lambda rid, *a, **k: self.removed.append(rid)
            ),
        ):
            done = {"run_id": "run-0", "status": "done", "posting": True}
            await self.mod._record(done)
            for i in range(1, cap + 1):
                await self.mod._record({"run_id": f"run-{i}", "status": "done"})
            self.assertNotIn("run-0", self.removed)
            done["posting"] = False  # delivery completed
            await self.mod._record({"run_id": "run-next", "status": "done"})

        self.assertIn("run-0", self.removed, "retention stopped reclaiming a finished poster")

    async def test_a_terminal_run_past_the_cap_is_still_evicted(self):
        cap = self.mod._RUNS_MAX
        await self._record_many(["done"] + ["done"] * cap)

        self.assertIn("run-0", self.removed, "retention stopped reclaiming finished runs")
        self.assertLessEqual(len(self.mod._RUNS), cap)


class TestRestartClearsAStrandedPostingFlag(unittest.TestCase):
    """A restart mid-post must not strand the run.

    `posting` is persisted while delivery runs, but the deliverer is an asyncio
    task in the gateway process. Before this recovery, a restart reloaded the flag
    with nothing driving it: the post endpoint answered 409 `already_posting`,
    `_is_live` kept retention from evicting the run, and delete refused it. Nothing
    could ever clear it.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.routes = _load_routes_module()

    def _write_runs(self, runs):
        d = Path(self.tmp)
        (d / "runs.json").write_text(json.dumps(runs), encoding="utf-8")
        return d / "runs.json"

    def test_persisted_posting_flag_is_cleared_on_load(self):
        path = self._write_runs(
            [
                {
                    "run_id": "r1",
                    "status": "done",
                    "posting": True,
                    "posted_keys": {"c1": ["k1"]},
                    "posted_comments": 1,
                }
            ]
        )
        with unittest.mock.patch.object(self.routes, "_runs_file", lambda: path):
            self.routes._load_runs()
        run = self.routes._RUNS[0]
        self.assertFalse(run["posting"], "the stranded posting flag must be cleared")
        self.assertIn("restart", (run.get("post_error") or "").lower())
        # Delivery evidence survives, so re-posting sends only the remainder.
        self.assertEqual(run["posted_keys"], {"c1": ["k1"]})
        # And the run does not count as live, so retention can reclaim it.
        self.assertFalse(self.routes._is_live(run))

    def test_a_run_that_was_not_posting_is_untouched(self):
        path = self._write_runs([{"run_id": "r2", "status": "done"}])
        with unittest.mock.patch.object(self.routes, "_runs_file", lambda: path):
            self.routes._load_runs()
        run = self.routes._RUNS[0]
        self.assertIsNone(run.get("post_error"))
        self.assertFalse(run.get("posting"))

    def test_running_recovery_still_applies_alongside_posting(self):
        path = self._write_runs([{"run_id": "r3", "status": "running", "posting": True}])
        with unittest.mock.patch.object(self.routes, "_runs_file", lambda: path):
            self.routes._load_runs()
        run = self.routes._RUNS[0]
        self.assertEqual(run["status"], "interrupted")
        self.assertFalse(run["posting"])


class TestGroupedPostAppliesKeysPerChange(unittest.TestCase):
    """A multi-change selection is one request, and each group keeps its own keys.

    `posting` is a per-run flag that only the poster clears, while the POST handler
    returns as soon as it dispatches the poster -- so one request per change gets
    every change after the first refused with `already_posting`. The grouped form
    is what makes the deliberate multi-select actually publish; the per-change key
    scoping has to survive inside it, or a selection made on one pull request would
    be applied to another.
    """

    def setUp(self):
        self.routes = _load_routes_module()

    def test_the_module_registers_both_new_background_tasks(self):
        """A collected task would leave `posting` / `_CONSOLIDATING` set forever."""
        src = Path(self.routes.__file__).read_text(encoding="utf-8")
        # Every create_task in this module must keep a strong ref: the set exists
        # precisely because a dropped task strands the flag it was going to clear.
        dispatches = [i for i in range(len(src)) if src.startswith("asyncio.create_task(", i)]
        self.assertGreaterEqual(len(dispatches), 4, "expected the known dispatch sites")
        for i in dispatches:
            window = src[i : i + 500]
            self.assertIn(
                "_TASKS.add(task)", window, f"create_task at offset {i} keeps no strong ref"
            )
            self.assertIn(
                "_TASKS.discard", window, f"create_task at offset {i} never drops its ref"
            )


class TestOrphanReapDoesNotBlockStartup(unittest.IsolatedAsyncioTestCase):
    """The reap must run off the event loop.

    `register_routes` is a sync function, but `start_dashboard` -- a coroutine --
    calls it, so its body executes ON the loop. `_reap_orphan_run_dirs` walks every
    run dir and deletes the unreferenced ones, so its cost grows with accumulated
    residue: inline, it stalled gateway startup. It is now an `on_startup` hook that
    offloads to a worker thread.
    """

    def setUp(self):
        self.routes = _load_routes_module()

    def test_register_routes_does_not_reap_inline(self):
        app = web.Application()
        called = []

        def _reap() -> int:
            called.append("reaped")
            return 0

        with unittest.mock.patch.object(self.routes, "_reap_orphan_run_dirs", _reap):
            self.routes.register_routes(app)
        self.assertEqual(called, [], "the reap must not run during registration")
        # It is deferred, not dropped.
        self.assertTrue(app.on_startup, "no startup hook was registered")

    async def test_the_startup_hook_reaps_off_the_loop(self):
        app = web.Application()
        threads = []

        def _reap():
            threads.append(threading.current_thread().name)
            return 2

        with unittest.mock.patch.object(self.routes, "_reap_orphan_run_dirs", _reap):
            self.routes.register_routes(app)
            for hook in app.on_startup:
                await hook(app)

        self.assertEqual(len(threads), 1, "the reap ran once")
        self.assertNotEqual(
            threads[0],
            threading.current_thread().name,
            "the reap must run on a worker thread, not the loop thread",
        )

    async def test_a_failing_reap_never_breaks_startup(self):
        app = web.Application()

        def _boom():
            raise OSError("disk gone")

        with unittest.mock.patch.object(self.routes, "_reap_orphan_run_dirs", _boom):
            self.routes.register_routes(app)
            for hook in app.on_startup:
                await hook(app)  # must not raise


class TestLayoutPassDoesNotBlockStartup(unittest.IsolatedAsyncioTestCase):
    """The layout self-heal must run off the event loop.

    Same shape as the reap above, and the same reason: `register_routes` is sync
    and `start_dashboard` is a coroutine, so its body executes ON the loop.
    `ensure_layout` walks each of nine directories' ancestor chain to refuse a
    planted link before creating it, so on a network-homed or stalled data
    directory that is a synchronous filesystem walk holding the loop.

    The ordering the UI depends on survives because aiohttp runs `on_startup`
    before the site accepts a connection, so no request can observe a missing
    `resolved_paths`.
    """

    def setUp(self):
        self.routes = _load_routes_module()

    def test_register_routes_does_not_build_the_layout_inline(self):
        app = web.Application()
        called = []

        def _ensure() -> None:
            called.append("built")

        with unittest.mock.patch.object(self.routes.store, "ensure_layout", _ensure):
            self.routes.register_routes(app)
        self.assertEqual(called, [], "the layout pass must not run during registration")
        # Deferred, not dropped.
        self.assertIn(
            "_ensure_layout_on_startup",
            {getattr(h, "__name__", "") for h in app.on_startup},
            "no layout startup hook was registered",
        )

    async def test_the_startup_hook_builds_the_layout_off_the_loop(self):
        app = web.Application()
        threads = []

        def _ensure() -> None:
            threads.append(threading.current_thread().name)

        with unittest.mock.patch.object(self.routes.store, "ensure_layout", _ensure):
            self.routes.register_routes(app)
            for hook in app.on_startup:
                await hook(app)

        self.assertEqual(len(threads), 1, "the layout pass ran once")
        self.assertNotEqual(
            threads[0],
            threading.current_thread().name,
            "the layout pass must run on a worker thread, not the loop thread",
        )

    async def test_a_failing_layout_never_breaks_startup(self):
        app = web.Application()

        def _boom() -> None:
            raise OSError("read-only filesystem")

        with unittest.mock.patch.object(self.routes.store, "ensure_layout", _boom):
            self.routes.register_routes(app)
            for hook in app.on_startup:
                await hook(app)  # must not raise

        self.assertTrue(
            [r for r in app.router.routes() if r.resource is not None],
            "the routes are registered even when the layout pass fails",
        )


class TestRetryRepairsTheReviewedIndex(unittest.TestCase):
    """A retry that succeeds after a failed post must leave the PR indexed.

    `_record_reviewed` reads ONLY `summary.per_change`. A retry writing just the
    run-level counters leaves a record still showing the original failure, which
    keeps the PR out of the dedup index -- and the next repo review reviews and
    posts it a second time. Both the first attempt and the retry write those fields
    through `review_driver.apply_post_outcome`.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_after_failed_post(self):
        """A run whose initial auto-post failed: nothing delivered, not indexed."""
        url = "https://github.com/acme/repo/pull/1"
        cid = self.mod.review_driver.change_id_for(url)
        return (
            url,
            cid,
            {
                "run_id": "R1",
                "changes": [url],
                "head_shas": {self.mod.review_driver.reviewed_key_for(url): "sha1"},
                "summary": {
                    "per_change": [
                        {
                            "change_id": cid,
                            "deep_reviewed": True,
                            "result_recorded": True,
                            "post_ok": False,
                            "posted_comments": 0,
                            "posting_expected": 3,
                        }
                    ]
                },
            },
        )

    def test_failed_post_is_not_indexed_before_the_retry(self):
        _url, _cid, run = self._run_after_failed_post()
        called = []
        with unittest.mock.patch.object(
            self.mod.results,
            "mark_reviewed",
            side_effect=lambda entries, *a, **k: called.append(entries),
        ):
            self.mod._record_reviewed(run)
        self.assertEqual(called, [], "a failed post must not be indexed")

    def test_run_collects_auto_posted_keys_from_per_change(self):
        """An auto-posted draft must be publishable in the app.

        The publish action reads `run["posted_keys"][change_id]`; without the
        collection step a draft `review.auto_post` genuinely delivered looks
        undelivered and the verdict buttons stay withheld.
        """
        run: dict = {}
        summary: dict = {
            "ok": True,
            "per_change": [
                {
                    "change_id": "GH-acme-repo-1",
                    "post_ok": True,
                    "posted_keys": ["design", "sec-1"],
                },
                {"change_id": "GH-acme-repo-2", "post_ok": True, "posted_keys": []},
            ],
        }
        self.mod._collect_delivered(run, summary)
        # The change that delivered is recorded; the one that delivered nothing is
        # absent rather than present-and-empty, which reads as not-delivered.
        self.assertEqual(run["posted_keys"], {"GH-acme-repo-1": ["design", "sec-1"]})
        self.assertNotIn("GH-acme-repo-2", run["posted_keys"])

    def test_apply_post_outcome_records_which_findings_landed(self):
        """`posted_keys` is delivery evidence, not decoration.

        The four counters say HOW MUCH was delivered; only `posted_keys` says WHICH
        change it belonged to, and that is what the publish gate reads.
        """
        rec: dict = {}
        self.mod.review_driver.apply_post_outcome(
            rec,
            {
                "post_ok": True,
                "posted_comments": 2,
                "expected_units": 2,
                "design_comment_posted": False,
                "posted_keys": ["design", "sec-1"],
            },
        )
        self.assertEqual(rec["posted_keys"], ["design", "sec-1"])

    def test_apply_post_outcome_absent_keys_read_as_nothing_delivered(self):
        """A post result with no keys must not fabricate evidence."""
        rec: dict = {}
        self.mod.review_driver.apply_post_outcome(
            rec, {"post_ok": False, "posted_comments": 0, "expected_units": 3}
        )
        self.assertEqual(rec["posted_keys"], [])

    def test_successful_retry_repairs_the_record_and_indexes(self):
        _url, cid, run = self._run_after_failed_post()
        # What post_recorded returns for a retry that delivered every unit.
        out = {
            "change_id": cid,
            "post_ok": True,
            "posted_comments": 3,
            "expected_units": 3,
            "design_comment_posted": True,
            "posted_keys": ["k1", "k2"],
        }
        rec = run["summary"]["per_change"][0]
        self.mod.review_driver.apply_post_outcome(rec, out)
        self.assertTrue(rec["post_ok"])
        self.assertEqual(rec["posted_comments"], 3)
        self.assertEqual(rec["posting_expected"], 3)

        captured = {}
        with unittest.mock.patch.object(
            self.mod.results,
            "mark_reviewed",
            side_effect=lambda entries, *a, **k: captured.update(entries),
        ):
            self.mod._record_reviewed(run)
        self.assertEqual(
            list(captured),
            ["github.com/acme/repo#1"],
            "a delivered retry must be indexed, or the PR is re-posted",
        )

    def test_retry_records_a_still_failing_post_as_undelivered(self):
        """A retry that fails again must NOT flip the record to delivered."""
        _url, cid, run = self._run_after_failed_post()
        rec = run["summary"]["per_change"][0]
        self.mod.review_driver.apply_post_outcome(
            rec, {"change_id": cid, "post_ok": False, "posted_comments": 1, "expected_units": 3}
        )
        called = []
        with unittest.mock.patch.object(
            self.mod.results,
            "mark_reviewed",
            side_effect=lambda entries, *a, **k: called.append(entries),
        ):
            self.mod._record_reviewed(run)
        self.assertEqual(called, [])

    def test_the_retry_path_calls_record_reviewed(self):
        """Structural: the delivery handler must reach the index, not just save."""
        with open(self.mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        task = src.split("async def _post_comments_bg", 1)[1]
        task = task.split("\nasync def ", 1)[0]
        self.assertIn(
            "apply_post_outcome", task, "the retry must repair per_change delivery evidence"
        )
        self.assertIn(
            "_record_reviewed", task, "the retry must index the PR or it gets re-reviewed"
        )


class TestWholeRunsSerialize:
    """A second run's body must not start while the first is still reviewing."""

    @pytest.mark.asyncio
    async def test_run_bodies_do_not_overlap(self, monkeypatch, tmp_path):
        import asyncio

        mod = _load_routes_module()

        overlapped = []
        inside = 0

        def fake_run_review(changes, **kw):
            nonlocal inside
            inside += 1
            if inside > 1:
                overlapped.append(True)
            # Yield the GIL the way real work does, so an unserialized second body
            # would get in here.
            import time

            # flake-ok: pre-existing 50 ms window that widens an overlap if run bodies are not serialized; no assertion waits on it, moved verbatim
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms window that widens an overlap if run bodies are not serialized; no assertion waits on it, moved verbatim
            time.sleep(0.05)
            inside -= 1
            return {"ok": True, "changes": len(changes), "per_change": []}

        monkeypatch.setattr(mod.review_driver, "run_review", fake_run_review)
        monkeypatch.setattr(mod, "_claim_changes_under_lock", lambda run, changes: list(changes))
        monkeypatch.setattr(mod, "_record_reviewed", lambda run: None)
        monkeypatch.setattr(mod, "_make_progress", lambda run: None)

        class _Pool:
            async def begin_batch(self):
                return None

            async def end_batch(self):
                return None

        monkeypatch.setattr(mod.review_pool, "get_pool", lambda: _Pool())
        monkeypatch.setattr(
            mod.review_pool, "make_sync_dispatch", lambda loop, pool: (lambda *a, **k: {"ok": True})
        )

        runs = [{"run_id": f"run-{i}"} for i in range(3)]
        await asyncio.gather(
            *(mod._run_review_bg(r, [f"https://x/pull/{i}"]) for i, r in enumerate(runs))
        )

        assert not overlapped, "two run bodies were inside run_review at once"


class TestReviewersSerialize:
    """Reviewers inside one run are serialized.

    Workers share the staging directory and each has file tools, so two live at
    once lets one write another change's record between that slot being cleared
    and its own worker writing -- findings attributed to the wrong pull request.
    """

    @pytest.mark.asyncio
    async def test_one_reviewer_at_a_time(self, monkeypatch, tmp_path):
        import asyncio

        mod = _load_routes_module()

        seen: dict = {}

        def fake_run_review(changes, **kw):
            seen.update(kw)
            return {"ok": True, "changes": len(changes), "per_change": []}

        monkeypatch.setattr(mod.review_driver, "run_review", fake_run_review)
        monkeypatch.setattr(mod, "_claim_changes_under_lock", lambda run, changes: list(changes))
        monkeypatch.setattr(mod, "_record_reviewed", lambda run: None)
        monkeypatch.setattr(mod, "_make_progress", lambda run: None)

        class _Pool:
            async def begin_batch(self):
                return None

            async def end_batch(self):
                return None

        monkeypatch.setattr(mod.review_pool, "get_pool", lambda: _Pool())
        monkeypatch.setattr(
            mod.review_pool, "make_sync_dispatch", lambda loop, pool: (lambda *a, **k: {"ok": True})
        )

        runs = [{"run_id": f"run-{i}"} for i in range(3)]
        await asyncio.gather(
            *(mod._run_review_bg(r, [f"https://x/pull/{i}"]) for i, r in enumerate(runs))
        )

        assert seen.get("concurrency") == 1, (
            "the backend must ask for one reviewer at a time; got " f"{seen.get('concurrency')!r}"
        )


class TestFailureStringMapping(unittest.TestCase):
    """Each skipped_reason renders a DISTINCT, cause-naming sentence.

    "The reviewer found nothing" and "the reviewer never ran" collapsing into one
    message is the ambiguity that made these failures untriageable — a reader
    must be able to tell the causes apart from the run-level error alone.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_home = os.environ.get("KIROCREW_HOME")
        self.addCleanup(self._restore_home)
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home

    def _mapped(self, reason: str) -> str:
        return self.mod._first_change_error({"per_change": [{"skipped_reason": reason}]})

    def test_every_reason_maps_to_its_own_sentence(self):
        reasons = (
            "no_review_recorded",
            "review_record_incomplete",
            "runtime_unavailable",
            "review_failed",
        )
        rendered = {reason: self._mapped(reason) for reason in reasons}
        for reason, text in rendered.items():
            self.assertNotEqual(text, reason, f"{reason} passed through unmapped")
            self.assertTrue(text, f"{reason} rendered empty")
        self.assertEqual(
            len(set(rendered.values())), len(reasons), f"reasons share a sentence: {rendered}"
        )

    def test_never_ran_and_found_nothing_read_apart(self):
        never_ran = self._mapped("runtime_unavailable")
        found_nothing = self._mapped("no_review_recorded")
        self.assertIn("never ran", never_ran)
        self.assertNotIn("never ran", found_nothing)

    def test_specific_error_text_outranks_the_reason_mapping(self):
        # A record carrying the preflight's own message (which names the missing
        # runtime) surfaces that message verbatim rather than the generic map.
        out = self.mod._first_change_error(
            {
                "per_change": [
                    {
                        "deep_error": "the reviewer cannot run: no kiro-cli executable was "
                        "found on this host",
                        "skipped_reason": "runtime_unavailable",
                    }
                ]
            }
        )
        self.assertIn("kiro-cli", out)


class TestRuntimePreflightWiring(unittest.IsolatedAsyncioTestCase):
    """The review path checks the runtime BEFORE spawning anything.

    On a host that cannot spawn a reviewer, the run must fail fast with an error
    naming the missing runtime — the batch is never opened, no session is
    dispatched, and every change's progress carries the discriminated reason —
    instead of "completing" and reporting an untriageable "no result record".
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_home = os.environ.get("KIROCREW_HOME")
        self.addCleanup(self._restore_home)
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        self.mod._RUNS = []

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home

    async def test_failing_preflight_fails_the_run_without_spawning(self):
        batch_calls: list[str] = []

        class _FakePool:
            async def begin_batch(self):
                batch_calls.append("begin")

            async def end_batch(self):
                batch_calls.append("end")

        def _refuse_dispatch(loop, pool, **kw):
            def dispatch(task, timeout=0, **kwargs):
                raise AssertionError(
                    "a session was dispatched despite a " "failed runtime preflight"
                )

            return dispatch

        async def _noop_async(*a, **k):
            return None

        url = "https://github.com/kirodotdev/KiroCrew/pull/33"
        run: dict = {
            "run_id": "rp1",
            "status": "running",
            "changes": [url],
            "change_ids": [_rd.change_id_for(url)],
            "progress": {},
        }
        self.mod._RUNS = [run]
        with (
            unittest.mock.patch.object(
                self.mod.review_pool,
                "runtime_preflight",
                lambda: "the reviewer cannot run: no kiro-cli executable was " "found on this host",
            ),
            unittest.mock.patch.object(self.mod.review_pool, "get_pool", lambda: _FakePool()),
            unittest.mock.patch.object(
                self.mod.review_pool, "make_sync_dispatch", _refuse_dispatch
            ),
            unittest.mock.patch.object(self.mod, "_save_runs", _noop_async),
            unittest.mock.patch.object(self.mod, "_notify_finished", _noop_async),
        ):
            await self.mod._run_review_bg(run, [url])

        self.assertEqual(batch_calls, [])  # runtime never spawned
        self.assertEqual(run["status"], "error")
        self.assertIn("kiro-cli", run["error"])
        entry = run["progress"][_rd.change_id_for(url)]
        self.assertEqual(entry["phase"], "failed")
        self.assertIn("kiro-cli", entry["error"])
        recs = run["summary"]["per_change"]
        self.assertEqual(recs[0]["skipped_reason"], "runtime_unavailable")
