"""Tests for the durable job-status store in backend/routes.py.

Job status (the run registry) is the ONE thing the app persists so the page can
reflect current/last review status across navigation and gateway restarts. These
tests lock in: atomic save/load round-trip, 0600 perms, and the restart-recovery
rule that an orphaned ``running`` run is re-marked ``interrupted`` (its in-process
driver thread cannot survive a restart)."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
import unittest.mock
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.code_review_sage.tests.backend_routes_helpers import (
    _load_routes_module,
)
from kiro_crew.apps.builtins.code_review_sage.tests.fixtures import OwnerRequest

_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from sage_lib import learning  # noqa: E402
from sage_lib import store  # noqa: E402  (app root added to sys.path above)
from sage_lib import review_driver as _rd  # noqa: E402
from sage_lib import review_pool as _rp  # noqa: E402


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
        asyncio.run(self.mod._save_runs())
        self.assertTrue(self.mod._runs_file().is_file())
        self.mod._RUNS = []  # simulate a fresh process
        self.mod._load_runs()
        self.assertEqual(len(self.mod._RUNS), 1)
        self.assertEqual(self.mod._RUNS[0]["run_id"], "a1")

    def test_orphaned_running_becomes_interrupted_on_load(self):
        self.mod._RUNS = [{"run_id": "b2", "status": "running", "changes": ["CR-9"]}]
        asyncio.run(self.mod._save_runs())
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
        asyncio.run(self.mod._save_runs())
        self.assertEqual(oct(self.mod._runs_file().stat().st_mode)[-3:], "600")

    def test_a_planted_tmp_symlink_is_not_followed(self):
        """The review worker writes into this tree and is prompt-injectable, so it
        must not be able to steer the registry write at an arbitrary file.

        A predictable ``runs.json.tmp`` was guessable: planting a symlink there
        made the gateway's own ``touch`` / lockdown / write land on the target,
        permission-changing and overwriting a user-owned file outside the sandbox.
        The randomly-named O_EXCL temp cannot open a path that already exists.
        """
        outsider = Path(self.tmp) / "precious.txt"
        outsider.write_text("do not touch", encoding="utf-8")
        runs = self.mod._runs_file()
        runs.parent.mkdir(parents=True, exist_ok=True)
        planted = runs.with_name(runs.name + ".tmp")
        try:
            os.symlink(outsider, planted)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover
            self.skipTest(f"symlink creation unavailable on this host: {exc}")

        self.mod._RUNS = [{"run_id": "d4", "status": "done"}]
        asyncio.run(self.mod._save_runs())

        self.assertEqual(outsider.read_text(encoding="utf-8"), "do not touch",
                         "the planted symlink was followed and its target rewritten")
        self.assertIn("d4", runs.read_text(encoding="utf-8"))

    def test_the_predictable_tmp_name_is_never_used(self):
        """Symlink-free twin of the test above, so the invariant is also covered on
        Windows -- where creating a symlink needs a privilege CI does not grant, yet
        which is the platform this whole change targets.

        Anything the worker can pre-place at the guessable path must survive: if the
        write still used ``<name>.tmp`` this file would be silently clobbered.
        """
        runs = self.mod._runs_file()
        runs.parent.mkdir(parents=True, exist_ok=True)
        squatter = runs.with_name(runs.name + ".tmp")
        squatter.write_text("planted", encoding="utf-8")

        self.mod._RUNS = [{"run_id": "e5", "status": "done"}]
        asyncio.run(self.mod._save_runs())

        self.assertEqual(squatter.read_text(encoding="utf-8"), "planted",
                         "the write still targets the predictable <name>.tmp path")
        self.assertIn("e5", runs.read_text(encoding="utf-8"))

    def test_the_lockdown_never_runs_on_the_event_loop(self):
        """Persisting the registry is blocking file IO (the owner-only lockdown
        itself is now in-process — ``platform_compat``), and it must not run on
        the single gateway loop -- a stall there delays every chat turn and the
        liveness heartbeat, not just this write.

        Asserted structurally rather than by timing, so it holds regardless of how
        fast the syscall happens to be on this host. The thread is RECORDED and
        compared afterwards rather than asserted inside the patch: ``_save_runs``
        deliberately swallows every exception, so an ``AssertionError`` raised in
        there would be logged and the test would pass on a real regression.
        """
        seen: dict[str, int] = {}

        def _record_thread(payload: str) -> None:
            seen["write"] = threading.get_ident()

        async def _drive() -> None:
            seen["loop"] = threading.get_ident()
            with unittest.mock.patch.object(self.mod, "_write_runs", _record_thread):
                await self.mod._save_runs()

        self.mod._RUNS = [{"run_id": "d4", "status": "done"}]
        asyncio.run(_drive())
        self.assertIn("write", seen, "_write_runs was never reached")
        self.assertNotEqual(
            seen["write"], seen["loop"],
            "_write_runs ran on the event-loop thread; the blocking file IO inside "
            "it would stall the gateway")


class TestRecordReviewedDelivery(unittest.TestCase):
    """The reviewed-index write path:
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
    """The TOCTOU + double-review guards: a run re-checks the
    reviewed index AND the in-flight claim registry before it owns a change, so a
    PR another run just recorded (or is reviewing right now) is not re-reviewed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        self.mod._INFLIGHT.clear()
        store.ensure_layout()

    def tearDown(self):
        self.mod._INFLIGHT.clear()
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, force=False, run_id="r1"):
        url = "https://github.com/acme/repo/pull/1"
        rkey = self.mod.review_driver.reviewed_key_for(url)
        run = {
            "run_id": run_id,
            "changes": [url], "force": force,
            "head_shas": {rkey: "sha1"},
            "change_ids": [self.mod.review_driver.change_id_for(url)],
        }
        return url, rkey, run

    def test_drops_pr_reviewed_by_concurrent_run(self):
        url, rkey, run = self._run()
        self.mod.results.mark_reviewed({rkey: {"head_sha": "sha1"}})  # concurrent run
        kept = self.mod._claim_changes_under_lock(run, [url])
        self.assertEqual(kept, [])
        self.assertEqual(run["changes"], [])
        self.assertEqual(run["head_shas"], {})

    def test_keeps_when_head_differs(self):
        url, rkey, run = self._run()
        self.mod.results.mark_reviewed({rkey: {"head_sha": "OLDSHA"}})  # head moved on
        self.assertEqual(self.mod._claim_changes_under_lock(run, [url]), [url])

    def test_force_bypasses_dedup(self):
        url, rkey, run = self._run(force=True)
        self.mod.results.mark_reviewed({rkey: {"head_sha": "sha1"}})
        self.assertEqual(self.mod._claim_changes_under_lock(run, [url]), [url])

    def test_claims_block_a_second_live_run(self):
        """Runs execute concurrently now, so the claim registry — not a whole-run
        lock — is what stops two live runs reviewing and posting to the same PR."""
        url, _rkey, first = self._run(run_id="first")
        self.assertEqual(self.mod._claim_changes_under_lock(first, [url]), [url])
        _url2, _rkey2, second = self._run(run_id="second")
        self.assertEqual(self.mod._claim_changes_under_lock(second, [url]), [])
        self.assertEqual(second["skipped_inflight"], 1)

    def test_changes_sharing_a_staging_path_are_serialized(self):
        """Two DIFFERENT pull requests can collapse to one shared staging file.

        A change id is also an on-disk filename, so it is lossily sanitized
        (``-`` -> ``_``): ``acme/service-api#1`` and ``acme/service_api#1`` are
        different repositories that stage as the same ``GH-acme-service_api-1.json``.
        ``reviewed_key_for`` exists precisely because of that collision, so the two
        claim distinct reviewed keys — and claiming on those alone let both run at
        once and let one worker's record be adopted into the other run, which is
        the silent empty-report failure this module exists to prevent. The staging
        identity is claimed too.
        """
        first = {"run_id": "first", "head_shas": {}, "force": True}
        second = {"run_id": "second", "head_shas": {}, "force": True}
        a = "https://github.com/acme/service-api/pull/1"
        b = "https://github.com/acme/service_api/pull/1"
        # Distinct reviewed keys, one staging stem: exactly the gap being closed.
        self.assertNotEqual(self.mod.review_driver.reviewed_key_for(a),
                            self.mod.review_driver.reviewed_key_for(b))
        self.assertEqual(
            self.mod.results.safe_change_id(self.mod.review_driver.change_id_for(a)),
            self.mod.results.safe_change_id(self.mod.review_driver.change_id_for(b)))

        self.assertEqual(self.mod._claim_changes_under_lock(first, [a]), [a])
        self.assertEqual(self.mod._claim_changes_under_lock(second, [b]), [])
        self.assertEqual(second["skipped_inflight"], 1)

    def test_two_colliding_changes_in_ONE_run_do_not_both_claim(self):
        """The same-run exemption must be per CHANGE, not per run.

        Exempting a run's own claims keeps re-claiming idempotent, but it also let
        a SECOND change in the same pasted batch through when its lossy change id
        collapsed onto the first one's staging file: both workers then wrote one
        path and a review was adopted under the wrong pull request.
        """
        a = "https://github.com/acme/service-api/pull/1"
        b = "https://github.com/acme/service_api/pull/1"
        run = {"run_id": "one", "head_shas": {}, "force": True}

        kept = self.mod._claim_changes_under_lock(run, [a, b])

        # Distinct pull requests, one staging path: only the first is kept.
        self.assertEqual(kept, [a])
        self.assertEqual(run["skipped_inflight"], 1)

    def test_a_posting_claim_blocks_a_review_of_that_change(self):
        """The POSTING path claims only `_INFLIGHT[skey]`, never `_STAGE_OWNER`.

        It holds a change id, not a reviewed key, so a claim check that consulted
        `_STAGE_OWNER` alone could not see it: a pasted-link review would claim a
        change whose staging file a live post was mid-round-trip through, and the
        two would swap result records.
        """
        url = "https://github.com/o/r/pull/5"
        cid = self.mod.review_driver.change_id_for(url)
        # What `_handle_run_post` records before dispatching the poster.
        self.mod._INFLIGHT[self.mod._stage_key(cid)] = "posting-run"

        run = {"run_id": "review-run", "head_shas": {}, "force": True}
        kept = self.mod._claim_changes_under_lock(run, [url])

        self.assertEqual(kept, [])
        self.assertEqual(run["skipped_inflight"], 1)

    def test_a_repeated_url_is_kept_once(self):
        """A duplicate in the pasted batch must not be dispatched twice.

        The claim checks all exempt the same run re-claiming its own key, so the
        second occurrence passed every gate. Both workers then staged through one
        path and one result was lost, while the run still reported success.
        """
        url = "https://github.com/o/r/pull/5"
        run = {"run_id": "one", "head_shas": {}, "force": True}

        kept = self.mod._claim_changes_under_lock(run, [url, url, url])

        self.assertEqual(kept, [url])
        self.assertEqual(run["change_ids"],
                         [self.mod.review_driver.change_id_for(url)])
        # A caller's duplicate is not contention with another run, so it must not
        # be reported as one.
        self.assertNotIn("skipped_inflight", run)

    def test_two_spellings_of_one_change_are_kept_once(self):
        """Dedup keys on the reviewed key, not the raw string.

        Two URLs that differ only in a form the reviewed key normalizes away are
        the same pull request, so keeping both would dispatch one change twice.
        """
        a = "https://github.com/o/r/pull/5"
        b = "https://github.com/o/r/pull/5/"
        run = {"run_id": "one", "head_shas": {}, "force": True}
        if (self.mod.review_driver.reviewed_key_for(a)
                != self.mod.review_driver.reviewed_key_for(b)):
            self.skipTest("reviewed_key_for does not normalize a trailing slash")

        kept = self.mod._claim_changes_under_lock(run, [a, b])

        self.assertEqual(len(kept), 1)

    def test_claim_is_reentrant_for_its_own_run(self):
        """A run re-claiming its own change keeps it (idempotent), so a retry of
        the claim step cannot starve the run that already owns the change."""
        url, _rkey, run = self._run(run_id="same")
        self.assertEqual(self.mod._claim_changes_under_lock(run, [url]), [url])
        self.assertEqual(self.mod._claim_changes_under_lock(run, [url]), [url])

    def test_release_frees_the_claim(self):
        """Every terminal path releases claims; otherwise the PR would be
        permanently unreviewable until the gateway restarted."""
        url, rkey, run = self._run(run_id="done-run")
        self.mod._claim_changes_under_lock(run, [url])
        self.assertIn(rkey, self.mod._INFLIGHT)
        self.mod._release_claims(run)
        self.assertNotIn(rkey, self.mod._INFLIGHT)
        _u, _k, later = self._run(run_id="later")
        self.assertEqual(self.mod._claim_changes_under_lock(later, [url]), [url])

    def test_pasted_link_run_also_claims(self):
        """A pasted-link run carries no head_shas, so the old dedup skipped it
        entirely — meaning it could collide with a repo run on the same PR. It
        must still take a claim."""
        url = "https://github.com/acme/repo/pull/9"
        rkey = self.mod.review_driver.reviewed_key_for(url)
        run = {"run_id": "pasted", "changes": [url]}
        self.assertEqual(self.mod._claim_changes_under_lock(run, [url]), [url])
        self.assertEqual(self.mod._INFLIGHT.get(rkey), "pasted")

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
        self.assertTrue(rv["effort"] == "" or rv["effort"] in _rp.VALID_EFFORTS)

    async def test_review_rejects_empty_input(self):
        class _Req(OwnerRequest):
            async def json(self):
                return {}
        resp = await self.mod._handle_review(_Req())
        self.assertEqual(resp.status, 400)

    async def test_review_starts_run_and_inits_progress(self):
        async def _noop(run, changes):
            return None
        self.mod._run_review_bg = _noop      # don't run the real driver

        _url = "https://github.com/kirodotdev/KiroCrew/pull/20"

        class _Req(OwnerRequest):
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
