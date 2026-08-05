"""Tests for the durable job-status store in backend/routes.py.

Job status (the run registry) is the ONE thing the app persists so the page can
reflect current/last review status across navigation and gateway restarts. These
tests lock in: atomic save/load round-trip, 0600 perms, and the restart-recovery
rule that an orphaned ``running`` run is re-marked ``interrupted`` (its in-process
driver thread cannot survive a restart)."""
import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

from kiro_crew import platform_compat

_APP_ROOT = Path(__file__).resolve().parent.parent
_ROUTES = _APP_ROOT / "backend" / "routes.py"
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from sage_lib import store  # noqa: E402  (app root added to sys.path above)


def _load_routes_module():
    spec = importlib.util.spec_from_file_location("sage_backend_routes_under_test", str(_ROUTES))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRunsPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        self.mod._RUNS = []

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_then_load_roundtrip(self):
        self.mod._RUNS = [{"run_id": "a1", "status": "done", "changes": ["CR-1"]}]
        self.mod._save_runs()
        self.assertTrue(self.mod._runs_file().is_file())
        self.mod._RUNS = []  # simulate a fresh process
        self.mod._load_runs()
        self.assertEqual(len(self.mod._RUNS), 1)
        self.assertEqual(self.mod._RUNS[0]["run_id"], "a1")

    def test_orphaned_running_becomes_interrupted_on_load(self):
        self.mod._RUNS = [{"run_id": "b2", "status": "running", "changes": ["CR-9"]}]
        self.mod._save_runs()
        self.mod._RUNS = []
        self.mod._load_runs()  # simulates a gateway restart
        self.assertEqual(self.mod._RUNS[0]["status"], "interrupted")
        self.assertIn("restart", self.mod._RUNS[0]["error"].lower())
        self.assertIn("finished_at", self.mod._RUNS[0])

    @unittest.skipUnless(
        platform_compat.IS_POSIX,
        "POSIX mode bits are unobservable on Windows: the owner-only lockdown there is an "
        "ACL (platform_compat.restrict_to_owner), and st_mode always reports 0o666.",
    )
    def test_runs_file_is_0600(self):
        self.mod._RUNS = [{"run_id": "c3", "status": "done"}]
        self.mod._save_runs()
        self.assertEqual(oct(self.mod._runs_file().stat().st_mode)[-3:], "600")


class TestRecordReviewedDelivery(unittest.TestCase):
    """Regression for the reviewed-index write path:
      * a PR is indexed as reviewed ONLY when the poster
        actually delivered (posted_comments >= posting_expected), not merely when
        the poster turn completed (post_ok). A failed gh post must not strand it.
      * The entry is keyed by the collision-free reviewed key (github.com/o/r#n),
        NOT the lossy change-id."""

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

    def _run(self, *, posted, expected):
        url = "https://github.com/acme/repo/pull/1"
        cid = self.mod.review_driver.change_id_for(url)
        return {
            "run_id": "R1",
            "changes": [url],
            "head_shas": {self.mod.review_driver.reviewed_key_for(url): "sha1"},
            "summary": {"per_change": [{
                "change_id": cid, "deep_reviewed": True, "post_ok": True,
                "posted_comments": posted, "posting_expected": expected,
            }]},
        }

    def test_delivered_is_indexed_under_collision_free_key(self):
        captured = {}
        with unittest.mock.patch.object(
                self.mod.results, "mark_reviewed",
                side_effect=lambda entries, *a, **k: captured.update(entries)):
            self.mod._record_reviewed(self._run(posted=2, expected=2))
        self.assertEqual(list(captured), ["github.com/acme/repo#1"])
        self.assertEqual(captured["github.com/acme/repo#1"]["head_sha"], "sha1")

    def test_failed_post_is_not_indexed(self):
        called = []
        with unittest.mock.patch.object(
                self.mod.results, "mark_reviewed",
                side_effect=lambda entries, *a, **k: called.append(entries)):
            # post_ok True (turn ended) but nothing actually posted -> not reviewed.
            self.mod._record_reviewed(self._run(posted=0, expected=2))
        self.assertEqual(called, [])


class TestUnderLockRededup(unittest.TestCase):
    """Regression for a TOCTOU race: a repo-review re-checks the reviewed
    index under _RUN_LOCK, so a PR a concurrent run just recorded is not re-reviewed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        store.ensure_layout()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, force=False):
        url = "https://github.com/acme/repo/pull/1"
        rkey = self.mod.review_driver.reviewed_key_for(url)
        run = {
            "changes": [url], "force": force,
            "head_shas": {rkey: "sha1"},
            "change_ids": [self.mod.review_driver.change_id_for(url)],
        }
        return url, rkey, run

    def test_drops_pr_reviewed_by_concurrent_run(self):
        url, rkey, run = self._run()
        self.mod.results.mark_reviewed({rkey: {"head_sha": "sha1"}})  # concurrent run
        kept = self.mod._dedup_changes_under_lock(run, [url])
        self.assertEqual(kept, [])
        self.assertEqual(run["changes"], [])
        self.assertEqual(run["head_shas"], {})

    def test_keeps_when_head_differs(self):
        url, rkey, run = self._run()
        self.mod.results.mark_reviewed({rkey: {"head_sha": "OLDSHA"}})  # head moved on
        self.assertEqual(self.mod._dedup_changes_under_lock(run, [url]), [url])

    def test_force_bypasses_dedup(self):
        url, rkey, run = self._run(force=True)
        self.mod.results.mark_reviewed({rkey: {"head_sha": "sha1"}})
        self.assertEqual(self.mod._dedup_changes_under_lock(run, [url]), [url])

    def test_load_missing_file_is_noop(self):
        self.mod._RUNS = [{"run_id": "keep", "status": "done"}]
        self.mod._load_runs()  # no file on disk yet — must not clobber/raise
        self.assertEqual(self.mod._RUNS[0]["run_id"], "keep")


class TestProgressCallback(unittest.TestCase):
    """The driver-facing progress callback updates a run's per-change map
    copy-on-write so the /runs reader never sees a half-mutated dict."""

    def setUp(self):
        self.mod = _load_routes_module()

    def test_copy_on_write_updates(self):
        run: dict = {"progress": {}}
        cb = self.mod._make_progress(run)
        cb("CR-1", "gating")
        first = run["progress"]
        cb("CR-1", "done", {"posted": 2, "expected": 3})
        # Phase advanced, extras merged, and the dict object was REPLACED (CoW).
        self.assertEqual(run["progress"]["CR-1"],
                         {"phase": "done", "posted": 2, "expected": 3})
        self.assertIsNot(run["progress"], first)

    def test_independent_changes_coexist(self):
        run: dict = {"progress": {}}
        cb = self.mod._make_progress(run)
        cb("CR-1", "deep")
        cb("CR-2", "blocked")
        self.assertEqual(run["progress"]["CR-1"]["phase"], "deep")
        self.assertEqual(run["progress"]["CR-2"]["phase"], "blocked")


class TestHandlers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        self.mod._RUNS = []

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_runs_includes_pool_stats(self):
        self.mod._RUNS = [{"run_id": "r1", "status": "done"}]
        resp = await self.mod._handle_runs(None)
        data = json.loads(resp.body)
        self.assertEqual(data["runs"][0]["run_id"], "r1")
        self.assertIn("pool", data)
        self.assertGreaterEqual(data["pool"]["max"], 1)        # live occupancy present
        self.assertIn("starting_max", data["pool"])

    async def test_runs_includes_reviewer_model_and_effort(self):
        self.mod._RUNS = []
        resp = await self.mod._handle_runs(None)
        data = json.loads(resp.body)
        self.assertIn("reviewer", data)
        rv = data["reviewer"]
        self.assertTrue(rv and rv.get("agent"))
        self.assertTrue(rv.get("model"))                       # resolved (tracks default)
        # effort is surfaced for the UI; with no user override it is the
        # documented default "" (inherit the model/provider default), otherwise
        # one of the concrete levels. Assert the contract, not a fixed level.
        self.assertIn("effort", rv)
        from sage_lib import review_pool as _rp
        self.assertTrue(rv["effort"] == "" or rv["effort"] in _rp.VALID_EFFORTS)

    async def test_review_rejects_empty_input(self):
        class _Req:
            async def json(self):
                return {}
        resp = await self.mod._handle_review(_Req())
        self.assertEqual(resp.status, 400)

    async def test_review_starts_run_and_inits_progress(self):
        async def _noop(run, changes):
            return None
        self.mod._run_review_bg = _noop      # don't run the real driver

        _url = "https://github.com/kirodotdev/KiroCrew/pull/20"

        class _Req:
            async def json(self):
                return {"links": _url}
        resp = await self.mod._handle_review(_Req())
        data = json.loads(resp.body)
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["changes"], [_url])
        self.assertTrue(data["run_id"])
        run = self.mod._RUNS[0]
        # run recorded with an initialized progress map
        self.assertEqual(run["progress"], {})
        # change_ids are the SAME keys the driver writes progress under, so the
        # dashboard aligns each row with its phase instead of showing "queued"
        # forever (regression guard for the raw-link-vs-change-id mismatch).
        from sage_lib import review_driver as _rd
        self.assertEqual(run["change_ids"], [_rd.change_id_for(_url)])
        self.assertEqual(run["change_ids"], ["GH-kirodotdev-KiroCrew-20"])
        await asyncio.sleep(0)               # let the no-op bg task drain


class TestNoBareLibNamespacePollution(unittest.TestCase):
    """Regression guard for the bare-``lib`` shadowing hazard.

    The app's code dir was renamed ``lib`` -> ``sage_lib`` precisely so loading
    the backend into the long-lived gateway process never registers a top-level
    ``lib`` module that could shadow (or be shadowed by) another component's own
    ``lib``. This injects a FOREIGN top-level ``lib`` and asserts that loading the
    backend leaves it intact and imports under the namespaced ``sage_lib``. If
    anyone re-introduces ``from lib import ...`` this turns red.
    """

    def test_loading_backend_does_not_touch_bare_lib(self):
        foreign = types.ModuleType("lib")
        foreign.MARKER = "FOREIGN"          # type: ignore[attr-defined]
        saved = sys.modules.get("lib")
        sys.modules["lib"] = foreign
        try:
            _load_routes_module()           # executes `from sage_lib import ...`
            self.assertIs(sys.modules.get("lib"), foreign,
                          "backend import shadowed a foreign top-level `lib`")
            self.assertEqual(sys.modules["lib"].MARKER, "FOREIGN")
            self.assertIn("sage_lib", sys.modules,
                          "backend must import its code under the namespaced `sage_lib`")
        finally:
            if saved is not None:
                sys.modules["lib"] = saved
            else:
                sys.modules.pop("lib", None)


class TestSettingsModelValidation(unittest.TestCase):
    """The review model written to config.json (and later into the worker
    cli.json overlay) must be validated against the known-model allowlist so raw
    request input never reaches the subprocess config (security-controls)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        store.ensure_layout()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_known_model_accepted(self):
        known = self.mod._KNOWN_MODELS[0]
        review = self.mod._write_review_section({"model": known})
        self.assertEqual(review["model"], known)

    def test_unknown_model_rejected(self):
        with self.assertRaises(ValueError):
            self.mod._write_review_section({"model": "../../etc/passwd"})
        with self.assertRaises(ValueError):
            self.mod._write_review_section({"model": "evil-model-9000"})

    def test_empty_model_clears_override(self):
        self.mod._write_review_section({"model": self.mod._KNOWN_MODELS[0]})
        review = self.mod._write_review_section({"model": None})
        self.assertIsNone(review["model"])


class TestLearningsEndpoint(unittest.IsolatedAsyncioTestCase):
    """GET /learnings surfaces a namespace's consolidated patterns AND the pending
    candidate (staged-but-not-yet-consolidated) learnings, so the dashboard can
    render the self-learning state. Read-only: it must never mutate on-disk files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        store.ensure_layout()
        from sage_lib import learning
        self.learning = learning

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _req(self, namespace=None):
        class _Req:
            query = {"namespace": namespace} if namespace else {}
        return _Req()

    async def test_returns_patterns_and_candidate(self):
        # A consolidated pattern (what reviews load) + a pending candidate.
        self.learning.consolidate_apply(
            "### Guard null tokens <!-- scope:common --> <!-- impact:high -->\n"
            "Reject requests whose auth token is absent before touching state.\n")
        self.learning.stage_learning(
            {"title": "Bound list sizes", "guidance": "Cap unbounded growth.",
             "impact": "medium"}, source="human_comment")

        resp = await self.mod._handle_learnings(self._req())
        data = json.loads(resp.body)
        self.assertEqual(data["namespace"], "default")
        titles = [p["title"] for p in data["patterns"]]
        self.assertIn("Guard null tokens", titles)
        cand_titles = [c["title"] for c in data["candidate"]]
        self.assertIn("Bound list sizes", cand_titles)

    async def test_empty_namespace_is_empty_lists(self):
        resp = await self.mod._handle_learnings(self._req())
        data = json.loads(resp.body)
        self.assertEqual(data["patterns"], [])
        self.assertEqual(data["candidate"], [])


if __name__ == "__main__":
    unittest.main()
