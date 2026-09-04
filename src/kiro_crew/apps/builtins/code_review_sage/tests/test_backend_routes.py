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
import threading
import types
import typing
import unittest
import unittest.mock
from pathlib import Path

import pytest
from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.code_review_sage.tests.fixtures import SYMLINKS_OK

_APP_ROOT = Path(__file__).resolve().parent.parent
_ROUTES = _APP_ROOT / "backend" / "routes.py"
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from sage_lib import learning  # noqa: E402
from sage_lib import results  # noqa: E402
from sage_lib import store  # noqa: E402  (app root added to sys.path above)
from sage_lib import report as R  # noqa: E402
from sage_lib import review_driver as _rd  # noqa: E402
from sage_lib import review_pool as _rp  # noqa: E402
from sage_lib.review_driver import _all_delivered  # noqa: E402


async def _noop_save() -> None:
    """Stand-in for ``routes._save_runs``, which is a coroutine.

    The registry write is offloaded to a worker thread because it is blocking
    file IO (see ``_write_runs``), so a plain ``lambda: None`` stub is
    not awaitable and the patched call site would raise instead of no-op.
    """


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

        self.assertEqual(
            outsider.read_text(encoding="utf-8"),
            "do not touch",
            "the planted symlink was followed and its target rewritten",
        )
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

        self.assertEqual(
            squatter.read_text(encoding="utf-8"),
            "planted",
            "the write still targets the predictable <name>.tmp path",
        )
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
            seen["write"],
            seen["loop"],
            "_write_runs ran on the event-loop thread; the blocking file IO inside "
            "it would stall the gateway",
        )


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
            "summary": {
                "per_change": [
                    {
                        "change_id": cid,
                        "deep_reviewed": True,
                        "post_ok": True,
                        "posted_comments": posted,
                        "posting_expected": expected,
                    }
                ]
            },
        }

    def test_delivered_is_indexed_under_collision_free_key(self):
        captured = {}
        with unittest.mock.patch.object(
            self.mod.results,
            "mark_reviewed",
            side_effect=lambda entries, *a, **k: captured.update(entries),
        ):
            self.mod._record_reviewed(self._run(posted=2, expected=2))
        self.assertEqual(list(captured), ["github.com/acme/repo#1"])
        self.assertEqual(captured["github.com/acme/repo#1"]["head_sha"], "sha1")

    def test_failed_post_is_not_indexed(self):
        called = []
        with unittest.mock.patch.object(
            self.mod.results,
            "mark_reviewed",
            side_effect=lambda entries, *a, **k: called.append(entries),
        ):
            # post_ok True (turn ended) but nothing actually posted -> not reviewed.
            self.mod._record_reviewed(self._run(posted=0, expected=2))
        self.assertEqual(called, [])


class TestUnderLockRededup(unittest.TestCase):
    """Regression for the TOCTOU + double-review guards: a run re-checks the
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
            "changes": [url],
            "force": force,
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
        self.assertNotEqual(
            self.mod.review_driver.reviewed_key_for(a), self.mod.review_driver.reviewed_key_for(b)
        )
        self.assertEqual(
            self.mod.results.safe_change_id(self.mod.review_driver.change_id_for(a)),
            self.mod.results.safe_change_id(self.mod.review_driver.change_id_for(b)),
        )

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
        self.assertEqual(run["change_ids"], [self.mod.review_driver.change_id_for(url)])
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
        if self.mod.review_driver.reviewed_key_for(a) != self.mod.review_driver.reviewed_key_for(b):
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
        self.assertEqual(run["progress"]["CR-1"], {"phase": "done", "posted": 2, "expected": 3})
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
        self.assertGreaterEqual(data["pool"]["max"], 1)  # live occupancy present
        self.assertIn("starting_max", data["pool"])

    async def test_runs_includes_reviewer_model_and_effort(self):
        self.mod._RUNS = []
        resp = await self.mod._handle_runs(None)
        data = json.loads(resp.body)
        self.assertIn("reviewer", data)
        rv = data["reviewer"]
        self.assertTrue(rv and rv.get("agent"))
        self.assertTrue(rv.get("model"))  # resolved (tracks default)
        # effort is surfaced for the UI; with no user override it is the
        # documented default "" (inherit the model/provider default), otherwise
        # one of the concrete levels. Assert the contract, not a fixed level.
        self.assertIn("effort", rv)
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

        self.mod._run_review_bg = _noop  # don't run the real driver

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
        self.assertEqual(run["change_ids"], [_rd.change_id_for(_url)])
        self.assertEqual(run["change_ids"], ["GH-kirodotdev-KiroCrew-20"])
        await asyncio.sleep(0)  # let the no-op bg task drain


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
        foreign.MARKER = "FOREIGN"  # type: ignore[attr-defined]
        saved = sys.modules.get("lib")
        sys.modules["lib"] = foreign
        try:
            _load_routes_module()  # executes `from sage_lib import ...`
            self.assertIs(
                sys.modules.get("lib"), foreign, "backend import shadowed a foreign top-level `lib`"
            )
            self.assertEqual(sys.modules["lib"].MARKER, "FOREIGN")
            self.assertIn(
                "sage_lib",
                sys.modules,
                "backend must import its code under the namespaced `sage_lib`",
            )
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
            "Reject requests whose auth token is absent before touching state.\n"
        )
        self.learning.stage_learning(
            {"title": "Bound list sizes", "guidance": "Cap unbounded growth.", "impact": "medium"},
            source="human_comment",
        )

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


# ---------------------------------------------------------------------------
# Round 11: worker-authored merge content reached learned-patterns.md unredacted,
# and a half-failed auto-post still deleted the records the retry needs.
# ---------------------------------------------------------------------------


class TestConsolidateRedactsMergedContent:
    """``consolidate_apply`` is the persistence chokepoint for merge output.

    The merged markdown comes from the merge WORKER, which has shell and file
    tools and has read the reviewed diffs. learned-patterns.md is rendered in the
    dashboard and injected into every later review prompt, so a credential that
    survives this write is exposed twice over. The caller hardens the read PATH;
    only this guard covers the CONTENT.
    """

    def test_credential_in_merged_markdown_is_scrubbed_before_the_write(self, tmp_path):

        secret = "ghp_" + "A" * 36
        # Real pattern markdown (a "### " heading), so the pattern-shape
        # guard admits it and redaction is what this test exercises.
        merged = "### Never hardcode a credential\n" f"The reviewed diff hardcoded one: {secret}\n"
        out = learning.consolidate_apply(merged, tmp_path, None)
        assert out["ok"], out
        written = Path(out["path"]).read_text(encoding="utf-8")
        assert secret not in written, "worker-authored credential persisted verbatim"
        assert (
            "Never hardcode a credential" in written
        ), "redaction must not eat the legitimate content"


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


class TestConsolidateRejectsPatternlessOutput:
    """Non-empty is not the same as usable.

    The merge worker is an LLM, so it can reply in prose instead of the pattern
    format. The emptiness check passes that through, and the write would then
    replace EVERY pattern with commentary and clear the candidate file in the same
    call — losing the staged learnings with nothing to show for them.
    """

    def _seeded(self, tmp_path):
        first = learning.consolidate_apply(
            "### Keep the guard\nReset it on every exit path.\n", tmp_path, None
        )
        assert first["ok"], first
        return learning, Path(first["path"])

    def test_prose_is_refused_and_the_ruleset_survives(self, tmp_path):
        learning, path = self._seeded(tmp_path)
        before = path.read_text(encoding="utf-8")

        out = learning.consolidate_apply(
            "I reviewed the candidates and found nothing worth merging.\n", tmp_path, None
        )

        assert out["ok"] is False
        assert "no recognizable patterns" in out["error"]
        assert path.read_text(encoding="utf-8") == before, "the ruleset was overwritten"

    def test_a_real_pattern_is_still_accepted(self, tmp_path):
        # The guard must not refuse legitimate merges.
        learning, path = self._seeded(tmp_path)
        out = learning.consolidate_apply(
            "### Authorize by confirming the owner\nDo not reject known-bad only.\n", tmp_path, None
        )
        assert out["ok"], out
        assert "Authorize by confirming the owner" in path.read_text(encoding="utf-8")


class TestAdoptionRefusesAPlantedLink:
    """The reviewer worker owns the shared dir and has file tools.

    ``is_file()`` follows symlinks, and ``os.replace`` moves the LINK, so a link
    planted where a record belongs used to land in the run dir intact — and
    ``read_result`` dereferences it with a plain read. Adoption must never carry a
    link across, and must not leave one behind to retry.
    """

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_symlink_is_refused_and_removed(self, tmp_path):

        store.ensure_layout(tmp_path)
        secret = tmp_path / "outside-secret.txt"
        secret.write_text("SENSITIVE", encoding="utf-8")

        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        planted = shared / f"{results.safe_change_id('CR-1')}.json"
        planted.symlink_to(secret)

        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False
        # Nothing was adopted...
        assert results.read_result("CR-1", tmp_path, "run-1") is None
        # ...the link is gone, so the same plant cannot be retried...
        assert not planted.exists() and not planted.is_symlink()
        # ...and the target itself was left alone.
        assert secret.read_text(encoding="utf-8") == "SENSITIVE"

    def test_a_real_record_is_still_adopted_as_a_regular_file(self, tmp_path):

        store.ensure_layout(tmp_path)
        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        (shared / f"{results.safe_change_id('CR-2')}.json").write_text(
            json.dumps(
                {
                    "schema": "code-review-sage-result",
                    "version": 1,
                    "change_id": "CR-2",
                    "platform": "github",
                    "repo_identity": "github.com/o/r",
                    "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
                    "counts": {"red": 0, "yellow": 1},
                }
            ),
            encoding="utf-8",
        )

        assert results.adopt_from_shared("CR-2", tmp_path, "run-1") is True
        got = results.read_result("CR-2", tmp_path, "run-1")
        assert got and got["change_id"] == "CR-2"
        dst = results.result_path("CR-2", tmp_path, "run-1")
        assert dst.is_file() and not dst.is_symlink()
        # The shared copy is consumed, as before.
        assert not (shared / f"{results.safe_change_id('CR-2')}.json").exists()


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


class TestAdoptionValidatesBeforeItWrites:
    """Adoption must be all-or-nothing.

    Round 13 traded ``os.replace`` for an ``O_TRUNC`` write to close a symlink
    hole, and that gave up atomicity: a malformed payload truncated whatever valid
    record was already filed, and ``read_result`` then raised on the wreckage, so
    no retry could recover it. Validate first, write via rename.
    """

    def _stage(self, tmp_path, change_id, body):
        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        (shared / f"{results.safe_change_id(change_id)}.json").write_text(body, encoding="utf-8")

    def _good(self, cid="CR-1", yellow=1):
        """A record that satisfies the real contract (REQUIRED_TOP/PHASE1).

        The earlier minimal fixture passed the envelope checks but was never a
        valid record, so it could not exercise adoption once the schema gate went
        in — the contract is what write_result has always enforced.
        """
        import json as _json

        return _json.dumps(
            {
                "schema": "code-review-sage-result",
                "version": 1,
                "change_id": cid,
                "platform": "github",
                "repo_identity": "github.com/o/r",
                "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
                "counts": {"red": 0, "yellow": yellow},
            }
        )

    def test_malformed_output_leaves_the_existing_record_intact(self, tmp_path):
        store.ensure_layout(tmp_path)

        # A valid record is adopted first.
        self._stage(tmp_path, "CR-1", self._good(yellow=3))
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is True
        before = results.read_result("CR-1", tmp_path, "run-1")
        assert before and before["counts"]["yellow"] == 3

        # Then the poster leaves malformed JSON for the same change.
        self._stage(tmp_path, "CR-1", "{not json at all")
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False

        # The valid record survived and is still readable — the retry path works.
        after = results.read_result("CR-1", tmp_path, "run-1")
        assert after == before, "a malformed payload destroyed the valid record"

    def test_a_record_violating_the_schema_is_refused(self, tmp_path):
        store.ensure_layout(tmp_path)
        # `phase1: []` is dict-shaped at the top level but carries a list where an
        # object belongs. The report reads it as rec.get("phase1", {}).get(...),
        # which raises on a list — the present-but-wrong-type case a default
        # cannot rescue, and the whole run fails.
        bad = json.dumps(
            {
                "schema": "code-review-sage-result",
                "version": 1,
                "change_id": "CR-1",
                "platform": "github",
                "repo_identity": "github.com/o/r",
                "phase1": [],
            }
        )
        self._stage(tmp_path, "CR-1", bad)
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False
        assert results.read_result("CR-1", tmp_path, "run-1") is None

    def test_an_unknown_gate_verdict_is_refused(self, tmp_path):
        store.ensure_layout(tmp_path)
        bad = json.dumps(
            {
                "schema": "code-review-sage-result",
                "version": 1,
                "change_id": "CR-1",
                "platform": "github",
                "repo_identity": "github.com/o/r",
                "phase1": {
                    "gate_verdict": "LOOKS_FINE",
                    "design_risk": "low",
                    "criticality": "low",
                },
            }
        )
        self._stage(tmp_path, "CR-1", bad)
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False

    def test_a_record_naming_another_change_is_refused(self, tmp_path):
        store.ensure_layout(tmp_path)
        # Filed under CR-1 but claiming to be CR-9: accepting it would attribute
        # CR-9's findings to CR-1 in the report.
        self._stage(tmp_path, "CR-1", self._good("CR-9"))
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False
        assert results.read_result("CR-1", tmp_path, "run-1") is None

    def test_a_json_array_is_refused(self, tmp_path):
        store.ensure_layout(tmp_path)
        self._stage(tmp_path, "CR-1", '[{"change_id": "CR-1"}]')
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False

    def test_no_temp_files_are_left_behind(self, tmp_path):
        store.ensure_layout(tmp_path)
        self._stage(tmp_path, "CR-1", self._good())
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is True
        rd = results.results_dir(tmp_path, "run-1")
        assert not list(rd.glob(".adopt-*")), "adoption left a temp file behind"


class TestPublishRefusesAPlantedDestinationLink:
    """The shared dir is worker-writable, so its paths are untrusted targets.

    Publishing wrote with `dst.write_bytes(...)`, which follows a symlink at the
    destination. The worker can plant one there, so the write landed on whatever it
    pointed at. Renaming over the name instead destroys the plant.
    """

    def _record(self, cid="CR-1"):
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": cid,
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 0, "yellow": 1},
        }

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_planted_link_is_replaced_not_followed(self, tmp_path):

        store.ensure_layout(tmp_path)
        results.write_result(self._record(), tmp_path, "run-1")

        outside = tmp_path / "outside-secret.txt"
        outside.write_text("SENSITIVE", encoding="utf-8")

        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        planted = shared / f"{results.safe_change_id('CR-1')}.json"
        planted.symlink_to(outside)

        assert results.publish_to_shared("CR-1", tmp_path, "run-1") is True

        # The target is untouched — the write did not follow the link.
        assert outside.read_text(encoding="utf-8") == "SENSITIVE"
        # And the shared path is now a real file holding the record.
        assert planted.is_file() and not planted.is_symlink()
        assert json.loads(planted.read_text(encoding="utf-8"))["change_id"] == "CR-1"

    def test_publishing_still_works_with_no_link_present(self, tmp_path):

        store.ensure_layout(tmp_path)
        results.write_result(self._record("CR-2"), tmp_path, "run-1")
        assert results.publish_to_shared("CR-2", tmp_path, "run-1") is True
        shared = results.results_dir(tmp_path, None)
        got = json.loads(
            (shared / f"{results.safe_change_id('CR-2')}.json").read_text(encoding="utf-8")
        )
        assert got["change_id"] == "CR-2"

    def test_no_temp_files_are_left_behind(self, tmp_path):

        store.ensure_layout(tmp_path)
        results.write_result(self._record("CR-3"), tmp_path, "run-1")
        assert results.publish_to_shared("CR-3", tmp_path, "run-1") is True
        shared = results.results_dir(tmp_path, None)
        assert not list(shared.glob(".publish-*")), "publish left a temp file behind"


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
        # And the run is no longer considered live, so retention can reclaim it.
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
    returns as soon as it dispatches the poster -- so one request per change had
    every change after the first refused with `already_posting`. The grouped form
    is what makes the deliberate multi-select actually publish; the per-change key
    scoping (round 8) has to survive inside it, or a selection made on one pull
    request would be applied to another.
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


class TestPublishRefusesAPlantedSourceLink:
    """The RUN results dir is worker-writable too, so the source is untrusted.

    Its sibling above covers the write: a link planted at the DESTINATION is
    replaced rather than followed. This covers the read. A worker that swaps its
    own finished record for a link to something outside the sandbox would
    otherwise have the linked bytes copied into the SHARED staging dir, which
    every worker can read. Same function, same trust boundary, opposite
    direction.
    """

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_symlinked_record_is_not_published(self, tmp_path):

        store.ensure_layout(tmp_path)
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("SENSITIVE", encoding="utf-8")

        # The worker plants a link where its own record belongs.
        src = results.result_path("CR-9", tmp_path, "run-9")
        src.parent.mkdir(parents=True, exist_ok=True)
        src.symlink_to(outside)

        assert results.publish_to_shared("CR-9", tmp_path, "run-9") is False

        shared = results.results_dir(tmp_path, None)
        landed = shared / f"{results.safe_change_id('CR-9')}.json"
        if landed.exists():
            assert "SENSITIVE" not in landed.read_text(encoding="utf-8")
        # A refused publish leaves no staging residue behind either.
        assert list(shared.glob(".publish-*")) == []

    def test_a_hardlinked_record_is_not_published(self, tmp_path):
        """The nolink guard also rejects a shared inode, not just a symlink."""

        store.ensure_layout(tmp_path)
        outside = tmp_path / "outside-hard.txt"
        outside.write_text("SENSITIVE", encoding="utf-8")

        src = results.result_path("CR-10", tmp_path, "run-10")
        src.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(outside, src)
        except OSError:  # pragma: no cover - platform without links
            pytest.skip("hardlinks unavailable here")

        assert results.publish_to_shared("CR-10", tmp_path, "run-10") is False


class TestReportWritesRefusePlantedLinks:
    """Report output must not be written through a worker-planted symlink.

    The reports dir is reachable by the review worker, so every output name in
    `generate()` -- focus-report.html, rows.json, report.json, index.json -- is an
    untrusted path. `write_text` followed a link planted at any of them and
    overwrote the linked host file, and the `os.chmod` after the write followed it
    too. All four now go through an atomic temp+rename, which swaps the name
    without following a link.
    """

    def _report(self):
        return {
            "rows": [
                {"band": "red", "title": "t", "change_id": "CR-1", "findings": []},
                {"band": "green", "title": "g", "change_id": "CR-2", "findings": []},
            ],
            "bands": {"red": 1, "yellow": 0, "green": 1},
            "generated_at": "2026-01-01T00:00:00Z",
        }

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    @pytest.mark.parametrize(
        "name",
        [
            "focus-report.html",
            "rows.json",
            "report.json",
            "index.json",
        ],
    )
    def test_a_planted_link_is_replaced_not_followed(self, tmp_path, name):

        store.ensure_layout(tmp_path)
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("SENSITIVE", encoding="utf-8")

        rd = R.reports_dir(tmp_path, "run-r1")
        rd.mkdir(parents=True, exist_ok=True)
        planted = rd / name
        planted.symlink_to(outside)

        R.write_outputs(self._report(), "<html>body</html>", tmp_path, None, "run-r1")

        # The link target is untouched -- the write did not follow it.
        assert outside.read_text(encoding="utf-8") == "SENSITIVE"
        # And the output is now a real file, not a link.
        assert planted.is_file() and not planted.is_symlink()

    def test_outputs_are_private_and_leave_no_temp_behind(self, tmp_path):

        store.ensure_layout(tmp_path)
        R.write_outputs(self._report(), "<html>body</html>", tmp_path, None, "run-r2")

        rd = R.reports_dir(tmp_path, "run-r2")
        for name in ("focus-report.html", "rows.json", "report.json", "index.json"):
            p = rd / name
            assert p.is_file(), f"{name} was not written"
            # The temp file is chmod'ed before it takes the real name, so the
            # mode must hold on the final path with no separate chmod step.
            # Windows expresses the same owner-only lockdown as a DACL, which
            # st_mode never reflects (it always reports 0o666), so the mode
            # bits are only observable on POSIX.
            if platform_compat.IS_POSIX:
                assert oct(p.stat().st_mode)[-3:] == "600", f"{name} is not 0600"
        assert list(rd.glob("*.tmp")) == [], "a staging temp file survived"


class TestAdoptionRejectsMalformedNestedShapes:
    """Validation must reject nested values the render path dereferences.

    `validate_result` checked that `phase1` was an object and `findings` a list,
    but not that `counts` / `blast_radius` were objects or that each finding entry
    was one. A worker writing `"counts": [1]` therefore passed validation, was
    adopted, and then aborted the run with an AttributeError when `counts.get()`
    ran at render time -- the review was already paid for and produced no report.
    """

    def _record(self, **over):
        rec = {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-1",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 0, "yellow": 0},
            "blast_radius": {"rating": "SMALL", "signals": {}},
            "findings": [],
        }
        rec.update(over)
        return rec

    def test_a_valid_record_still_passes(self):
        assert results.validate_result(self._record()) == []

    @pytest.mark.parametrize("bad", [[1], "red=0", 3, True])
    def test_non_object_counts_is_rejected(self, bad):
        errs = results.validate_result(self._record(counts=bad))
        assert any("counts must be an object" in e for e in errs), errs

    @pytest.mark.parametrize("bad", [["SMALL"], "SMALL", 0])
    def test_non_object_blast_radius_is_rejected(self, bad):
        errs = results.validate_result(self._record(blast_radius=bad))
        assert any("blast_radius must be an object" in e for e in errs), errs

    def test_a_non_object_finding_entry_is_rejected(self):
        errs = results.validate_result(
            self._record(findings=[{"severity": "red"}, "not-an-object"])
        )
        assert any("findings[1] must be an object" in e for e in errs), errs

    def test_absent_optional_shapes_are_still_valid(self):
        """counts / blast_radius are optional -- absent is not malformed."""
        rec = self._record()
        del rec["counts"]
        del rec["blast_radius"]
        assert results.validate_result(rec) == []

    def test_write_result_refuses_a_malformed_record(self, tmp_path):
        """The same validator gates the write path, so the bad shape never lands."""

        store.ensure_layout(tmp_path)
        with pytest.raises(ValueError, match="counts must be an object"):
            results.write_result(self._record(counts=[1]), tmp_path, "run-v1")


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


class TestAdoptionRequiresAnExactChangeIdentity:
    """Adoption must compare change ids EXACTLY, not through `safe_change_id`.

    That sanitizer is lossy by design — it produces a filename stem — so
    comparing the sanitized forms accepted a record naming a genuinely different
    change. `GH-acme-service/api-1` and `GH-acme-service_api-1` both reduce to
    `GH-acme-service_api-1`, so a worker record for the first was filed as the
    second and the report attributed its findings to the wrong pull request.
    """

    def _record(self, cid):
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": cid,
            "platform": "github",
            "repo_identity": "github.com/acme/service_api",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 0, "yellow": 0},
            "findings": [],
        }

    def test_a_record_naming_a_different_change_is_refused(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        want = "GH-acme-service_api-1"
        other = "GH-acme-service/api-1"  # different change, same stem
        assert results.safe_change_id(other) == results.safe_change_id(want)
        assert other != want

        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        (shared / f"{results.safe_change_id(want)}.json").write_text(
            json.dumps(self._record(other)), encoding="utf-8"
        )

        assert results.adopt_from_shared(want, tmp_path, "run-x1") is False

    def test_an_exact_match_is_still_adopted(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        want = "GH-acme-service_api-1"
        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        (shared / f"{results.safe_change_id(want)}.json").write_text(
            json.dumps(self._record(want)), encoding="utf-8"
        )

        assert results.adopt_from_shared(want, tmp_path, "run-x2") is True


class TestCountValuesMustBeNumeric:
    """`counts` values are used in arithmetic, so a string count aborts the run.

    `report.py` scores with `counts.get("red", 0) * 15 + counts.get("yellow", 0)
    * 5`. A worker writing `{"red": "1"}` satisfied the object check added for
    the non-object case, was adopted, and then raised TypeError at scoring time —
    the same failure one level deeper, after the review was already paid for.
    """

    def _record(self, counts):
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-1",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": counts,
            "findings": [],
        }

    @pytest.mark.parametrize("bad", ["1", None, [1], {"n": 1}, True, False])
    def test_a_non_numeric_count_is_rejected(self, bad):
        from sage_lib import results

        errs = results.validate_result(self._record({"red": bad, "yellow": 0}))
        assert any("counts.red must be a number" in e for e in errs), errs

    @pytest.mark.parametrize("good", [0, 3, 3.0])
    def test_numeric_counts_pass(self, good):
        from sage_lib import results

        assert results.validate_result(self._record({"red": good, "yellow": 0})) == []

    def test_every_band_is_checked_not_just_red_and_yellow(self):
        """A new band must not reintroduce the gap."""
        from sage_lib import results

        errs = results.validate_result(self._record({"red": 0, "yellow": 0, "green": "many"}))
        assert any("counts.green must be a number" in e for e in errs), errs

    def test_the_scoring_arithmetic_really_does_raise(self):
        """The premise: this is why the value type matters."""
        # Typed loosely on purpose — the point is the RUNTIME failure a worker's
        # string count causes in report scoring, which a static check would
        # otherwise refuse to let us demonstrate.
        counts: dict[str, typing.Any] = {"red": "1", "yellow": 0}
        with pytest.raises(TypeError):
            counts.get("red", 0) * 15 + counts.get("yellow", 0) * 5

    def test_write_result_refuses_a_string_count(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        with pytest.raises(ValueError, match="counts.red must be a number"):
            results.write_result(self._record({"red": "1", "yellow": 0}), tmp_path, "run-c1")


class TestReportsDirReadsDoNotFollowAPlant:
    """Round 21 made the reports-dir WRITES not follow a plant; the READS did.

    The dir is reachable by the review worker, so a symlink at `index.json` or
    `focus-report.html` was followed on read and its contents flowed onward —
    into a rendered report, or into a shareable dashboard artifact.
    """

    def _plant(self, tmp_path, name, body):
        from sage_lib import report

        rd = report.reports_dir(tmp_path, "run-r1")
        rd.mkdir(parents=True, exist_ok=True)
        secret = tmp_path / "outside-secret.txt"
        secret.write_text(body, encoding="utf-8")
        (rd / name).symlink_to(secret)
        return rd / name

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_planted_html_link_is_not_read(self, tmp_path):
        from sage_lib import report

        link = self._plant(tmp_path, "focus-report.html", "TOP-SECRET-BODY")
        assert link.is_file()  # the link resolves — it just must not be read
        assert report.read_within_reports(link, tmp_path, "run-r1") is None

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_planted_index_link_is_not_read(self, tmp_path):
        from sage_lib import report

        link = self._plant(tmp_path, "index.json", json.dumps({"report_slug": "stolen"}))
        assert report.read_within_reports(link, tmp_path, "run-r1") is None

    def test_a_real_file_still_reads(self, tmp_path):
        from sage_lib import report

        rd = report.reports_dir(tmp_path, "run-r2")
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "index.json").write_text('{"report_slug": "ok"}', encoding="utf-8")
        got = report.read_within_reports(rd / "index.json", tmp_path, "run-r2")
        assert json.loads(got or "{}")["report_slug"] == "ok"

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_read_report_refuses_a_planted_report_json(self, tmp_path):
        """The consumer, not just the helper: a plant renders as no report."""
        from sage_lib import report

        self._plant(tmp_path, "report.json", json.dumps({"rows": ["leak"]}))
        assert report.read_report(tmp_path, "run-r1") is None

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_set_report_slug_does_not_merge_a_planted_index(self, tmp_path):
        from sage_lib import report

        self._plant(tmp_path, "index.json", json.dumps({"secret": "leak"}))
        idx = report.set_report_slug("s1", tmp_path, "run-r1")
        assert idx == {"report_slug": "s1"}, idx
        rd = report.reports_dir(tmp_path, "run-r1")
        assert not (rd / "index.json").is_symlink()  # the plant was replaced
        assert "leak" not in (rd / "index.json").read_text(encoding="utf-8")


class TestRedactionReachesNestedValues:
    """Redacting top-level strings covered every KEY, not every VALUE.

    A worker that wrote a finding field as `{"evidence": {"k": "<secret>"}}` or
    `["<secret>"]` slipped past `isinstance(v, str)` and the nested string reached
    report.json and the dashboard unscrubbed. Both redactors now walk the whole
    structure, so no shape passes content through untouched.
    """

    # Assembled at runtime, never written as one literal: the redactor only fires
    # on credential-SHAPED input (a plain sentinel passes through untouched, so the
    # test would prove nothing), but a real key shape sitting in the source trips
    # `scripts/scrub-lint.sh`'s credential scan. Splitting it satisfies both — the
    # value is key-shaped when the redactor sees it, and no line here matches.
    SECRET = "AKIA" + "1234567890EXAMPLE"

    def _leaks(self, obj) -> bool:
        return self.SECRET in repr(obj)

    @pytest.mark.parametrize(
        "shape",
        [
            {"evidence": {"k": SECRET}},  # nested dict
            {"refs": [SECRET]},  # list
            {"pairs": [{"k": SECRET}]},  # list of dicts
            {"deep": {"a": {"b": {"c": SECRET}}}},  # several levels down
            {"observation": SECRET},  # the flat case, still works
        ],
    )
    def test_no_finding_shape_carries_a_secret_through(self, shape):
        from sage_lib import report

        f = {"file": "a.py", "line": 3, **shape}
        assert not self._leaks(report._redact_finding(f)), shape

    def test_a_row_value_written_as_a_container_is_scrubbed(self):
        """`url`/`platform` come straight off the worker's record."""
        from sage_lib import report

        row = report._redact_row(
            {"band": "red", "url": {"k": self.SECRET}, "platform": [self.SECRET]}
        )
        assert not self._leaks(row["url"])
        assert not self._leaks(row["platform"])

    def test_skipped_and_non_string_fields_are_untouched(self):
        """The scrub must not corrupt the values it is supposed to leave alone."""
        from sage_lib import report

        f = report._redact_finding({"file": "a.py", "line": 7, "severity": "red"})
        assert f["line"] == 7
        row = report._redact_row({"band": "yellow", "red": 2, "score": 41, "deep_reviewed": True})
        assert row == {"band": "yellow", "red": 2, "score": 41, "deep_reviewed": True}

    def test_a_deep_payload_does_not_exhaust_the_stack(self):
        """Depth is worker-chosen, so the walk is bounded and still scrubs."""
        from sage_lib import report

        nested: dict = {"k": self.SECRET}
        for _ in range(200):
            nested = {"n": nested}
        out = report._redact_finding({"file": "a.py", "blob": nested})
        assert not self._leaks(out)


class TestNonScalarFindingFieldsAreRefused:
    """The boundary rejects what the redactor would otherwise have to sanitize.

    `validate_result` checked that each finding was an object but said nothing
    about its field values — the same gap `counts` had, one level deeper. Both
    entrances (`write_result` and adoption) share this validator.
    """

    def _record(self, finding):
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-1",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 1, "yellow": 0},
            "findings": [finding],
        }

    @pytest.mark.parametrize("bad", [{"k": "v"}, ["v"], (1, 2)])
    def test_a_container_field_is_rejected(self, bad):
        from sage_lib import results

        errs = results.validate_result(self._record({"file": "a.py", "x": bad}))
        assert any("findings[0].x must be a string" in e for e in errs), errs

    @pytest.mark.parametrize("good", ["s", None])
    def test_prose_fields_pass(self, good):
        from sage_lib import results

        assert results.validate_result(self._record({"file": "a.py", "x": good})) == []

    @pytest.mark.parametrize("bad", [3, 3.5, True])
    def test_a_number_in_a_prose_field_is_rejected(self, bad):
        """`line` is the only numeric field. Every other one is rendered through
        `html.escape()`, which raises on a non-string -- so a numeric `snippet` used
        to be adopted and then crash report generation, leaving a COMPLETED run with
        no report."""
        from sage_lib import results

        errs = results.validate_result(self._record({"file": "a.py", "snippet": bad}))
        assert any("findings[0].snippet must be a string" in e for e in errs), errs

    @pytest.mark.parametrize("good", [3, 3.5, None])
    def test_line_still_takes_a_number(self, good):
        from sage_lib import results

        assert results.validate_result(self._record({"file": "a.py", "line": good})) == []

    def test_write_result_refuses_the_record(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        with pytest.raises(ValueError, match="must be a string"):
            results.write_result(
                self._record({"file": "a.py", "evidence": {"k": "s"}}), tmp_path, "run-n1"
            )


class TestResultReadsDoNotFollowAPlantedLink:
    """Runs are concurrent, so another live worker can reach a run's results dir.

    A prompt-injected reviewer replaces another run's record with a symlink; a
    plain read dereferences it and the victim's report and posted payload are built
    from attacker-chosen JSON filed under the victim's change id. `publish_to_shared`
    and `adopt_from_shared` already guarded their copies — the readers did not.
    """

    def _plant(self, tmp_path, run_id, change_id, payload):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        rd = results.results_dir(tmp_path, run_id)
        rd.mkdir(parents=True, exist_ok=True)
        target = tmp_path / "attacker.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        (rd / f"{results.safe_change_id(change_id)}.json").symlink_to(target)
        return rd

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_read_result_refuses_a_planted_record(self, tmp_path):
        from sage_lib import results

        self._plant(tmp_path, "victim", "CR-1", {"change_id": "ATTACKER"})
        assert results.read_result("CR-1", tmp_path, "victim") is None

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_list_results_skips_a_planted_record(self, tmp_path):
        from sage_lib import results

        self._plant(tmp_path, "victim", "CR-1", {"change_id": "ATTACKER"})
        assert results.list_results(tmp_path, "victim") == []

    def test_a_real_record_still_reads(self, tmp_path):
        """The guard must not break the normal path."""
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        rec = {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-2",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 0, "yellow": 0},
            "findings": [],
        }
        results.write_result(rec, tmp_path, "ok-run")
        got = results.read_result("CR-2", tmp_path, "ok-run")
        assert got is not None and got["change_id"] == "CR-2"
        assert [r["change_id"] for r in results.list_results(tmp_path, "ok-run")] == ["CR-2"]

    def test_a_non_object_record_is_refused(self, tmp_path):
        """Consumers index with .get(); a list or scalar must not reach them."""
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        rd = results.results_dir(tmp_path, "odd")
        rd.mkdir(parents=True, exist_ok=True)
        (rd / f"{results.safe_change_id('CR-3')}.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert results.read_result("CR-3", tmp_path, "odd") is None

    def test_a_missing_record_is_still_none(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        assert results.read_result("CR-NONE", tmp_path, "empty") is None

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_the_reviewed_index_is_guarded_too(self, tmp_path):
        """It decides which PRs count as reviewed, so a swap suppresses reviews."""
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        target = tmp_path / "fake-index.json"
        target.write_text(json.dumps({"GH-o-r-1": {"head_sha": "x"}}), encoding="utf-8")
        p = results.reviewed_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.unlink()
        p.symlink_to(target)
        assert results.read_reviewed(tmp_path) == {}

    def test_a_real_reviewed_index_still_loads(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        results.write_reviewed({"GH-o-r-9": {"head_sha": "abc"}}, tmp_path)
        assert results.read_reviewed(tmp_path) == {"GH-o-r-9": {"head_sha": "abc"}}


class TestNoFindingFieldIsExemptFromRedaction:
    """`line` was exempt from redaction on an assumption nothing enforced.

    The redactor skipped `line` "because it is numeric", but `validate_result`
    accepted any scalar — so a reviewer writing a credential into `line` as a
    string rode the exemption straight to the dashboard. The premise is now
    enforced at the boundary and the exemption is gone, which is why a number is
    still left alone: `_redact_deep` only touches strings.
    """

    SECRET = "AKIA" + "1234567890EXAMPLE"  # split: see the sentinel note above

    def test_a_credential_in_line_is_redacted(self):
        from sage_lib import report

        out = report._redact_finding({"file": "a.py", "line": self.SECRET})
        assert self.SECRET not in repr(out)

    @pytest.mark.parametrize("good", [7, 0, 7.5])
    def test_a_numeric_line_is_untouched(self, good):
        from sage_lib import report

        assert report._redact_finding({"file": "a.py", "line": good})["line"] == good

    def test_the_redactor_has_no_skip_set_for_findings(self):
        """Guard the shape, not just the instance: no field may be exempt."""
        import inspect

        from sage_lib import report

        src = inspect.getsource(report._redact_finding)
        assert "frozenset" not in src, (
            "a skip set reappeared in _redact_finding; an exemption is only safe "
            "if something enforces its premise"
        )


class TestFindingLineMustBeANumber:
    """The boundary enforces what the redactor used to assume."""

    def _record(self, line):
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-1",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 1, "yellow": 0},
            "findings": [{"file": "a.py", "line": line}],
        }

    @pytest.mark.parametrize("bad", ["12", "AKIA-shaped", True, False])
    def test_a_non_numeric_line_is_rejected(self, bad):
        from sage_lib import results

        errs = results.validate_result(self._record(bad))
        assert any("findings[0].line must be a number" in e for e in errs), errs

    @pytest.mark.parametrize("good", [7, 0, 7.5, None])
    def test_a_numeric_or_absent_line_passes(self, good):
        from sage_lib import results

        assert results.validate_result(self._record(good)) == []

    def test_write_result_refuses_a_string_line(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        with pytest.raises(ValueError, match="line must be a number"):
            results.write_result(self._record("12"), tmp_path, "run-l1")


class TestNestedStringFieldsMustBeScalars:
    """Third time this class appeared: an object check that ignored its values.

    `counts` needed its values widened to numbers, finding fields needed theirs
    widened to scalars, and `phase1` / `blast_radius.rating` were still open. Their
    values are consumed as dict KEYS (`_RISK_W.get(design_risk)`,
    `_BLAST_W.get(rating)`) or with string operations (`band_override_reason
    .strip()`), so a list is unhashable / has no `.strip()` and killed report
    generation after the review had already been paid for.

    `phase1` is checked by SHAPE — because naming the fields is exactly what let
    this recur twice. `blast_radius` cannot take that rule: `signals` is
    legitimately a nested object in the schema.

    The phase1 rule was later tightened from "any scalar" to "a string" (see
    TestPhase1ValuesMustBeStrings): every field in the worker's contract is text,
    and a number passed validation and then crashed the code that renders it.
    """

    def _record(self, **over):
        r = {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-1",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "blast_radius": {"rating": "SMALL", "signals": {"sensitive_hits": [], "loc_added": 0}},
            "counts": {"red": 0, "yellow": 0},
            "findings": [],
        }
        r.update(over)
        return r

    @pytest.mark.parametrize(
        "field",
        [
            "design_risk",
            "gate_verdict",
            "criticality",
            "band_override_reason",
            "problem",
            "solution_assessment",
        ],
    )
    @pytest.mark.parametrize("bad", [[], {}, ["x"]])
    def test_a_non_scalar_phase1_value_is_rejected(self, field, bad):
        from sage_lib import results

        p1 = {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"}
        p1[field] = bad
        errs = results.validate_result(self._record(phase1=p1))
        assert any(f"phase1.{field} must be a string" in e for e in errs), errs

    def test_a_non_string_rating_is_rejected(self):
        from sage_lib import results

        errs = results.validate_result(self._record(blast_radius={"rating": [], "signals": {}}))
        assert any("blast_radius.rating must be a string" in e for e in errs), errs

    def test_blast_radius_signals_may_stay_nested(self):
        """The schema nests `signals`; the guard must not reject a valid record."""
        from sage_lib import results

        assert results.validate_result(self._record()) == []

    def test_scalar_phase1_values_pass(self):
        from sage_lib import results

        assert (
            results.validate_result(
                self._record(
                    phase1={
                        "gate_verdict": "PASS",
                        "design_risk": "low",
                        "criticality": "low",
                        "design_headline": None,
                    }
                )
            )
            == []
        )

    def test_the_validator_itself_does_not_crash_on_a_bad_verdict(self):
        """It REPORTS a malformed record; it must never raise on one.

        `gate_verdict not in VALID_VERDICTS` tests membership of a SET, so an
        unhashable value raised TypeError inside validate_result — the function
        whose whole job is refusing malformed records died on one. The shape check
        now runs first, so every later check sees a scalar.
        """
        from sage_lib import results

        bad_verdicts: list[typing.Any] = [[], {}, ["PASS"]]
        for bad in bad_verdicts:
            errs = results.validate_result(
                self._record(
                    phase1={"gate_verdict": bad, "design_risk": "low", "criticality": "low"}
                )
            )
            assert any("phase1.gate_verdict must be a string" in e for e in errs), errs

    def test_the_scoring_lookup_really_does_raise(self):
        """The premise: an unhashable design_risk kills focus_score."""
        from sage_lib import report

        with pytest.raises(TypeError):
            report.focus_score(
                self._record(
                    phase1={"gate_verdict": "PASS", "design_risk": [], "criticality": "low"}
                )
            )

    def test_write_result_refuses_the_record(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        with pytest.raises(ValueError, match="phase1.design_risk must be a string"):
            results.write_result(
                self._record(
                    phase1={"gate_verdict": "PASS", "design_risk": [], "criticality": "low"}
                ),
                tmp_path,
                "run-p35",
            )


class TestRetryRepairsTheReviewedIndex(unittest.TestCase):
    """A retry that succeeds after a failed post must leave the PR indexed.

    `_record_reviewed` reads ONLY `summary.per_change`. The explicit-retry path
    used to write just the run-level counters, so a record still showing the
    original failure kept the PR out of the dedup index -- and the next repo
    review reviewed and posted it a second time. Both the first attempt and the
    retry now write those fields through `review_driver.apply_post_outcome`.
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


class TestConsolidationCannotResurrectADeletedNamespace(unittest.IsolatedAsyncioTestCase):
    """Deleting a namespace mid-consolidation must not bring it back.

    A consolidation captures its namespace BY NAME before dispatch, so the delete
    handler's active-list prune does nothing for it -- that prune only stops
    in-flight *reviews*, which write to whatever is currently active. Every
    learning writer mkdirs its parents, so a merge applying after the delete
    recreated the directory and ruleset after the caller was told the delete
    succeeded.

    The fix is the `_CONSOLIDATING` claim: it is added in the handler before its
    first await and released only in the worker's `finally`, so it brackets the
    worker's whole lifetime, and DELETE is refused for that entire span. These
    tests pin the hazard, the refusal, and the claim-lifetime property the refusal
    depends on -- because the guard is only sound while that property holds.
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

    _MD = (
        "### A learned rule <!-- scope:common --> <!-- impact:high -->"
        " <!-- added:2026-01-01T00:00:00Z -->\nguidance here\n"
    )

    def test_a_late_apply_would_resurrect_a_deleted_namespace(self):
        """The hazard, so the claim is not mistaken for ceremony.

        If applying to a deleted namespace ever stops recreating it, this fails and
        the guard should be re-justified rather than kept out of habit.
        """
        L = self.mod.learning
        L.create_namespace("doomed")
        L.delete_namespace("doomed")
        self.assertNotIn("doomed", L.list_namespaces())
        L.consolidate_apply(self._MD, None, "doomed", [])
        self.assertIn("doomed", L.list_namespaces())

    async def test_refused_delete_does_not_deactivate_the_namespace(self):
        """A refused delete must not leave the namespace pruned from the active list:
        reviews would silently stop using one the caller was told still exists."""
        L, mod = self.mod.learning, self.mod
        L.create_namespace("keepme")
        mod._write_review_section({"active_namespaces": ["default", "keepme"]})

        # Refuse at the learning layer, the way a missing directory or an out-of-tree
        # path does -- none of those refusals depend on the active list, so pruning
        # first would deactivate a namespace that then fails to delete.
        with unittest.mock.patch.object(
            L, "delete_namespace", return_value={"ok": False, "error": "does not exist"}
        ):
            resp = await self._delete("keepme")

        self.assertEqual(resp.status, 400)
        self.assertIn(
            "keepme",
            mod._load_review_section().get("active_namespaces") or [],
            "a refused delete must not prune the active list",
        )

    async def test_delete_is_refused_while_a_consolidation_is_claimed(self):
        """Refuse with 409, and leave no partial side effect behind."""
        L, mod = self.mod.learning, self.mod
        L.create_namespace("busy")
        before = sorted(L.list_namespaces())
        active_before = list(mod._load_review_section().get("active_namespaces") or [])

        mod._CONSOLIDATING.add("busy")
        try:
            resp = await self._delete("busy")
        finally:
            mod._CONSOLIDATING.discard("busy")

        self.assertEqual(resp.status, 409)
        self.assertIn("consolidation_in_progress", resp.text)
        self.assertEqual(
            sorted(L.list_namespaces()), before, "a refused delete must not remove the namespace"
        )
        # And it must not have pruned the active list on its way out.
        self.assertEqual(
            list(mod._load_review_section().get("active_namespaces") or []), active_before
        )

    async def test_delete_still_works_when_nothing_is_consolidating(self):
        """The guard must not wedge ordinary deletes shut."""
        L = self.mod.learning
        L.create_namespace("spare")
        resp = await self._delete("spare")
        self.assertEqual(resp.status, 200)
        self.assertNotIn("spare", L.list_namespaces())

    def test_the_claim_brackets_the_whole_worker_lifetime(self):
        """Structural: the property the 409 relies on.

        The claim must be taken in the handler BEFORE the task is created, and
        given back in the worker's `finally`. If a later edit moves the add after
        `create_task`, or releases it in the handler instead, the refusal above
        stops covering the window it exists to cover.
        """
        with open(self.mod.__file__, encoding="utf-8") as fh:
            src = fh.read()

        handler = src.split("async def _handle_consolidate", 1)
        if len(handler) == 1:  # tolerate a rename of the endpoint
            handler = src.split("_CONSOLIDATING.add(", 1)
        tail = handler[1]
        add_at = tail.find("_CONSOLIDATING.add(")
        task_at = tail.find("create_task(_consolidate_bg(")
        self.assertNotEqual(add_at, -1)
        self.assertNotEqual(task_at, -1)
        self.assertLess(add_at, task_at, "the claim must be held before the worker is dispatched")

        worker = src.split("async def _consolidate_bg", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("finally:", worker)
        self.assertIn(
            "_CONSOLIDATING.discard(",
            worker.split("finally:", 1)[1],
            "the worker must release the claim on every terminal path",
        )

    def test_a_refused_delete_cannot_have_already_pruned(self):
        """The defect class: rejection must precede the side effect.

        An earlier shape of this guard checked the claim a SECOND time AFTER the
        active-list prune. That closed the race window but introduced a worse
        outcome -- a 409 returned with the namespace already pruned from
        `active_namespaces`, so the request refused to delete the namespace while
        silently deactivating it. The prune and the rmtree now happen in one
        offloaded helper under `_NS_OPS_LOCK`, so no 409 can be returned after a
        prune has been applied.
        """
        with open(self.mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        handler = src.split("async def _handle_namespaces", 1)[1]
        body = handler.split("\nasync def ", 1)[0]
        delete_arm = body.split('if request.method == "DELETE":', 1)[1]

        # Both sides must serialise on the same lock.
        self.assertIn("_NS_OPS_LOCK", delete_arm)
        consolidate = src.split("async def _handle_consolidate", 1)[1]
        self.assertIn("_NS_OPS_LOCK", consolidate.split("\nasync def ", 1)[0])

        # No 409 may appear after the active-list write in the DELETE arm.
        write_at = delete_arm.find("_write_review_section")
        self.assertNotEqual(write_at, -1)
        self.assertNotIn(
            "409",
            delete_arm[write_at:],
            "a delete must not be refused after pruning the active list",
        )

    async def _delete(self, ns):
        req = unittest.mock.MagicMock()
        req.method = "DELETE"
        req.json = unittest.mock.AsyncMock(return_value={"name": ns})
        req.query = {}
        req.match_info = {}
        return await self.mod._handle_namespaces(req)


class TestPhase1ValuesMustBeStrings(unittest.TestCase):
    """Phase-one values are text by contract, and every reader consumes them as text.

    Accepting "any scalar" let a number through validation and then crash the code
    that renders it: `classify()` calls `.strip()` on `band_override_reason`, and the
    HTML renderer calls `html.escape()` on `gate_verdict`. Every field in the
    worker's phase1 contract is text — gate_verdict, design_risk, criticality,
    design_headline, problem, why_it_matters, solution_assessment — so a numeric
    value is malformed, and refusing the record is the fail-closed direction: it is
    re-reviewed rather than rendered half-broken.
    """

    def _record(self, **phase1):
        p1 = {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"}
        p1.update(phase1)
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "change_id": "CR-1",
            "blast_radius": {"rating": "SMALL", "signals": {}},
            "counts": {"red": 0, "yellow": 0},
            "findings": [],
            "phase1": p1,
        }

    def test_a_string_phase1_record_is_accepted(self):
        from sage_lib import results

        self.assertEqual(results.validate_result(self._record()), [])

    def test_numeric_phase1_value_is_refused(self):
        from sage_lib import results

        errs = results.validate_result(self._record(band_override_reason=42))
        self.assertTrue(any("must be a string" in e for e in errs), errs)

    def test_bool_phase1_value_is_refused(self):
        """bool is an int subclass, so it must not slip through as a number."""
        from sage_lib import results

        errs = results.validate_result(self._record(criticality=True))
        self.assertTrue(any("must be a string" in e for e in errs), errs)

    def test_numeric_gate_verdict_is_refused(self):
        """This used to validate cleanly.

        The vocabulary check sat behind an isinstance() guard, so a numeric
        gate_verdict was neither rejected as a shape nor checked against
        VALID_VERDICTS — it reached `html.escape()` in the renderer, which raises.
        """
        from sage_lib import results

        errs = results.validate_result(self._record(gate_verdict=5))
        self.assertTrue(errs, "a numeric gate_verdict must not validate")

    def test_none_stays_allowed(self):
        """An optional field present as null is still a valid record."""
        from sage_lib import results

        self.assertEqual(results.validate_result(self._record(problem=None)), [])


class TestPersistedReportIsRedactedOnRead(unittest.TestCase):
    """A worker-planted report.json must not reach the dashboard unredacted.

    `read_within_reports` refuses symlinks and paths escaping the reports dir, but
    the dir itself is writable by the review worker, so a REAL file planted there is
    legitimate as far as the reader is concerned. `build_report` redacts every row on
    the way in; the read path did not, so planted rows reached the UI verbatim.
    Redacting on read is idempotent, so a report this module built is unchanged.
    """

    # Assembled at runtime: scrub-lint scans source text, while the redactor only
    # fires on credential-shaped input.
    _SENTINEL = "AKIA" + "IOSFODNN7EXAMPLE"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, payload, run_id="run-a"):
        from sage_lib import report

        rd = report.reports_dir(None, run_id)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "report.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_planted_row_text_is_redacted(self):
        from sage_lib import report

        self._plant(
            {
                "bands": {"red": 1, "yellow": 0, "green": 0},
                "rows": [
                    {
                        "change_id": "CR-1",
                        "band": "red",
                        "why": f"leaked {self._SENTINEL} here",
                        "design_headline": f"credential {self._SENTINEL}",
                    }
                ],
            }
        )
        got = report.read_report(None, "run-a")
        self.assertNotIn(self._SENTINEL, json.dumps(got))
        self.assertIn("REDACTED", got["rows"][0]["why"])

    def test_a_real_report_survives_the_read_unchanged(self):
        """Idempotence: redacting twice must not alter a legitimately built report."""
        from sage_lib import report

        built = report.build_report(
            [
                {
                    "change_id": "CR-2",
                    "revision": "b" * 40,
                    "phase1": {
                        "gate_verdict": "PASS",
                        "design_risk": "low",
                        "criticality": "low",
                        "design_headline": "a clean headline",
                    },
                    "counts": {"red": 0, "yellow": 0},
                    "findings": [],
                    "blast_radius": {"rating": "SMALL"},
                }
            ]
        )
        self._plant(built, run_id="run-b")
        got = report.read_report(None, "run-b")
        self.assertEqual(got["rows"], built.get("rows"))

    def test_planted_tallies_are_coerced_to_counts(self):
        """Bands and total are arithmetic inputs the UI trusts."""
        from sage_lib import report

        self._plant(
            {"bands": {"red": "lots", "yellow": -3, "green": True}, "rows": []}, run_id="run-c"
        )
        got = report.read_report(None, "run-c")
        self.assertEqual(got["bands"], {"red": 0, "yellow": 0, "green": 0})
        self.assertIsInstance(got["total"], int)

    def test_non_dict_rows_are_dropped(self):
        """A row is a mapping by contract; anything else cannot be redacted."""
        from sage_lib import report

        self._plant(
            {
                "bands": {"red": 0, "yellow": 0, "green": 0},
                "rows": ["not-a-row", 7, {"change_id": "CR-3", "band": "green"}],
            },
            run_id="run-d",
        )
        got = report.read_report(None, "run-d")
        self.assertEqual([r["change_id"] for r in got["rows"]], ["CR-3"])


class TestPlantedReportMetadataCannotBreakTheEndpoint(unittest.TestCase):
    """The remaining worker-writable fields in the read_report payload.

    Round 40 redacted the rows and coerced the tallies but left two gaps in its
    own hardening: `bands` was screened for truthiness rather than for being a
    MAPPING, and `report_slug` was passed through untouched.

    `[] or {}` yields `{}`, so an empty list looked handled -- but a truthy
    non-dict (a non-empty list, a string, a number) reached `.get` and raised
    AttributeError, turning a planted file into an HTTP 500 on the report
    endpoint. The slug names an artifact the dashboard turns into a share link, so
    it is screened against the artifact store's own grammar rather than redacted:
    a value that is not a slug cannot reference a real artifact.
    """

    # Assembled at runtime so scrub-lint sees no credential-shaped literal.
    _SENTINEL = "AKIA" + "IOSFODNN7EXAMPLE"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, run_id, payload, index=None):
        from sage_lib import report

        rd = report.reports_dir(None, run_id)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "report.json").write_text(json.dumps(payload), encoding="utf-8")
        if index is not None:
            (rd / "index.json").write_text(json.dumps(index), encoding="utf-8")

    def test_a_truthy_non_dict_bands_does_not_raise(self):
        """The shape that actually crashed: truthy, so `or {}` never fired."""
        from sage_lib import report

        for i, bad in enumerate([[1, 2], "red", 7, [{"red": 1}]]):
            with self.subTest(bands=bad):
                rid = f"bands-{i}"
                self._plant(rid, {"bands": bad, "rows": []})
                got = report.read_report(None, rid)
                self.assertEqual(got["bands"], {"red": 0, "yellow": 0, "green": 0})

    def test_an_empty_list_bands_still_works(self):
        """Kept explicit: this case was already safe and must stay safe."""
        from sage_lib import report

        self._plant("bands-empty", {"bands": [], "rows": []})
        self.assertEqual(
            report.read_report(None, "bands-empty")["bands"], {"red": 0, "yellow": 0, "green": 0}
        )

    def test_a_planted_slug_is_dropped(self):
        from sage_lib import report

        self._plant(
            "slug-bad", {"bands": {}, "rows": []}, index={"report_slug": f"leaked {self._SENTINEL}"}
        )
        got = report.read_report(None, "slug-bad")
        self.assertIsNone(got["report_slug"])
        self.assertNotIn(self._SENTINEL, json.dumps(got))

    def test_a_traversal_shaped_slug_is_dropped(self):
        from sage_lib import report

        # Assembled so the path never appears as a literal in this file.
        traversal = "..%s..%setc%sshadow" % ("/", "/", "/")
        self._plant("slug-trav", {"bands": {}, "rows": []}, index={"report_slug": traversal})
        self.assertIsNone(report.read_report(None, "slug-trav")["report_slug"])

    def test_a_non_string_slug_is_dropped(self):
        from sage_lib import report

        self._plant("slug-num", {"bands": {}, "rows": []}, index={"report_slug": 5})
        self.assertIsNone(report.read_report(None, "slug-num")["report_slug"])

    def test_a_real_artifact_slug_survives(self):
        """The constraint must not break the feature it guards.

        Screened against `artifacts._SLUG_RE`, the same grammar the artifact store
        itself enforces, so anything the store could have issued is accepted.
        """
        from sage_lib import report

        from kiro_crew.artifacts import _SLUG_RE

        slug = "focus-report-cr-1-abc123"
        self.assertTrue(_SLUG_RE.match(slug), "fixture must be a valid slug")
        self._plant("slug-ok", {"bands": {}, "rows": []}, index={"report_slug": slug})
        self.assertEqual(report.read_report(None, "slug-ok")["report_slug"], slug)


class TestNoRowFieldIsExemptFromRedaction(unittest.TestCase):
    """`band` was the last redaction exemption in a report row, and it leaked.

    The exemption existed because `bands[row["band"]]` and `BAND_DOT[row.band]`
    index on the exact value, so scrubbing it looked like it would break grouping.
    The concern was real; the protection was not. Redaction is shape-based, so the
    three real bands come back byte-identical -- while the exemption made `band`
    the ONE field in a row that reached the dashboard verbatim, so a planted
    "red <credential>" leaked where the prose beside it was scrubbed.

    Keying is now protected by a vocabulary on the untrusted read path instead: a
    row whose band is not one of the three cannot be grouped, so it is dropped.
    """

    # Assembled at runtime: scrub-lint scans source text, the redactor only fires
    # on credential-shaped input.
    _SENTINEL = "AKIA" + "IOSFODNN7EXAMPLE"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, run_id, rows):
        from sage_lib import report

        rd = report.reports_dir(None, run_id)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "report.json").write_text(
            json.dumps({"bands": {"red": 1, "yellow": 0, "green": 0}, "rows": rows}),
            encoding="utf-8",
        )

    def test_a_credential_in_band_is_scrubbed(self):
        """The exemption's actual consequence, at the redactor itself."""
        from sage_lib import report

        out = report._redact_row(
            {"band": f"red {self._SENTINEL}", "why": f"prose {self._SENTINEL}"}
        )
        self.assertNotIn(self._SENTINEL, out["band"])
        self.assertNotIn(self._SENTINEL, out["why"])

    def test_a_legitimate_band_survives_redaction_unchanged(self):
        """Why removing the exemption is free: the real redactor is a no-op here.

        This is the property the UI needs -- `bands[]`/`BAND_DOT[]` keep keying on
        the exact value -- and it is what the exemption was wrongly protecting.
        """
        from sage_lib import report

        for band in ("red", "yellow", "green"):
            with self.subTest(band=band):
                self.assertEqual(report._redact_row({"band": band})["band"], band)

    def test_a_planted_band_cannot_reach_the_dashboard(self):
        from sage_lib import report

        self._plant("band-leak", [{"change_id": "CR-1", "band": f"red {self._SENTINEL}"}])
        got = report.read_report(None, "band-leak")
        self.assertNotIn(self._SENTINEL, json.dumps(got))

    def test_a_row_with_an_unknown_band_is_dropped(self):
        """An ungroupable row cannot render, so it is not passed through."""
        from sage_lib import report

        self._plant(
            "band-vocab",
            [
                {"change_id": "CR-1", "band": "purple"},
                {"change_id": "CR-2", "band": None},
                {"change_id": "CR-3", "band": "green"},
            ],
        )
        got = report.read_report(None, "band-vocab")
        self.assertEqual([r["change_id"] for r in got["rows"]], ["CR-3"])

    def test_the_redactor_takes_no_skip_set(self):
        """Structural: no row field may be re-exempted without this failing.

        Three rounds of findings in this family were "one more field was exempt",
        so the absence of a skip set is asserted directly rather than left to a
        future reader's judgement.
        """
        with open(self.mod_report_path(), encoding="utf-8") as fh:
            src = fh.read()
        body = src.split("def _redact_row", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_redact_deep_map(row)", body, "_redact_row must pass no skip set")
        self.assertNotIn("_STRUCTURAL_ROW_FIELDS", src, "the row skip set must stay gone")

    def mod_report_path(self):
        from sage_lib import report

        return report.__file__


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

            time.sleep(0.05)
            inside -= 1
            return {"ok": True, "changes": len(changes), "per_change": []}

        monkeypatch.setattr(mod.review_driver, "run_review", fake_run_review)
        monkeypatch.setattr(mod, "_claim_changes_under_lock", lambda run, changes: list(changes))
        monkeypatch.setattr(mod, "_record_reviewed", lambda run: None)
        monkeypatch.setattr(mod, "_make_progress", lambda run: None)

        class _Pool:
            async def begin_batch(self, model=None):
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
            async def begin_batch(self, model=None):
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


class TestSelectedModelGoesToThePoolOnly:
    """A run's selected model is bound by ``begin_batch``, not by the driver call.

    ``review_driver.run_review`` takes no ``model`` parameter — the model is fixed
    when ``begin_batch`` spawns the one shared runtime — so forwarding one there
    raises TypeError inside ``run_review`` and every review started with a model
    selected fails before dispatching a single change.
    """

    @pytest.mark.asyncio
    async def test_review_start_with_a_model_reaches_run_review(self, monkeypatch):
        mod = _load_routes_module()

        batch_models: list[str | None] = []

        def fake_run_review(
            changes,
            *,
            dispatch=None,
            progress=None,
            run_id=None,
            cancelled=None,
            preflight=None,
            concurrency=0,
        ):
            # No **kw on purpose: the stub carries the real signature, so an extra
            # ``model=`` kwarg raises here exactly as it does in the driver.
            return {
                "ok": True,
                "changes": len(changes),
                "per_change": [],
                "result_records": 1,
                "deep_reviewed": 1,
            }

        class _Pool:
            async def begin_batch(self, model=None):
                batch_models.append(model)

            async def end_batch(self):
                return None

        monkeypatch.setattr(mod.review_driver, "run_review", fake_run_review)
        monkeypatch.setattr(mod, "_claim_changes_under_lock", lambda run, changes: list(changes))
        monkeypatch.setattr(mod, "_record_reviewed", lambda run: None)
        monkeypatch.setattr(mod, "_make_progress", lambda run: None)
        monkeypatch.setattr(mod.review_pool, "get_pool", lambda: _Pool())
        monkeypatch.setattr(
            mod.review_pool, "make_sync_dispatch", lambda loop, pool: (lambda *a, **k: {"ok": True})
        )
        # The runtime looks usable on every host: on a runner without kiro-cli
        # the preflight would short-circuit before begin_batch, and the model
        # would never reach the pool at all.
        monkeypatch.setattr(mod.review_pool, "runtime_preflight", lambda: "")

        # The id is a sentinel, not a real model: the stub pool validates
        # nothing, and this test pins that the run's selected model reaches the
        # pool as the ``begin_batch(model=...)`` kwarg -- a hardcoded real id
        # would trip the model-selection rule (AGENTS.md -> Model selection).
        run = {"run_id": "run-model", "model": "model-under-test"}
        await mod._run_review_bg(run, ["https://x/pull/1"])

        assert batch_models == [
            "model-under-test"
        ], "the pool must spawn its runtime with the run's selected model"
        # An "error" here means the driver call raised -- the TypeError dropping the
        # ``model=`` kwarg prevents.
        assert run["status"] == "done", f"review start failed: {run.get('error')!r}"


class _FakeSessions:
    """The two SessionManager methods the follow-up route uses."""

    def __init__(self, mapped=None):
        self.map: dict[str, tuple] = dict(mapped or {})
        self.seeds: list[tuple] = []
        # When set, a seeded entry is dropped on read-back — what the real
        # session map does to an entry whose session files are gone.
        self.drop_seeded = False

    def resumable_sid(self, key):
        entry = self.map.get(key)
        return entry[0] if entry else None

    def seed_conversation(self, key, sid, *, provider="", cwd=""):
        self.seeds.append((key, sid, provider, cwd))
        if not self.drop_seeded:
            self.map[key] = (sid, provider, cwd)


class _FakeState:
    def __init__(self, sessions=None, folders=None, slots=None):
        self.sessions = sessions
        self._folders = list(folders or [])
        self._slots = dict(slots or {})

    async def mutate_folders(self, mutate):
        _changed, value = mutate(self._folders)
        return value


class _Req:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


class TestFollowupRoutes(unittest.IsolatedAsyncioTestCase):
    """Opening a follow-up must never hand back a session that resumed nothing.

    The dashboard's fallback for a failed resume is to replay Kiro Crew's own
    conversation log, and a follow-up slot has none — so a slot created without a
    live mapping answers confidently with no idea what was reviewed. Every branch
    here is about refusing that outcome rather than degrading into it.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        self.mod._RUNS = [{"run_id": "run1", "status": "done"}]
        self.mod.is_app_enabled = lambda name: True
        self.sessions_dir = Path(self.tmp) / "kiro-sessions"
        self.sessions_dir.mkdir(parents=True)
        self.mod.followup.kiro_sessions_dir = lambda: self.sessions_dir
        store.ensure_layout()
        store.ensure_run_layout("run1")

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, sid="sid-1", change="GH-o-r-42"):
        (self.sessions_dir / f"{sid}.json").write_text("{}", encoding="utf-8")
        self.assertTrue(
            self.mod.followup.write_descriptor(
                "run1", change, sid=sid, agent="sage-reviewer", cwd="/work"
            )
        )

    async def test_state_reports_not_resumable_with_a_reason(self):
        resp = await self.mod._handle_chat_get(
            _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        data = json.loads(resp.body)
        self.assertFalse(data["resumable"])
        self.assertEqual(data["reason"], self.mod.followup.ERR_NO_DESCRIPTOR)
        self.assertTrue(data["slot_key"])

    async def test_state_reports_resumable_once_recorded(self):
        self._record()
        resp = await self.mod._handle_chat_get(
            _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        data = json.loads(resp.body)
        self.assertTrue(data["resumable"])
        self.assertEqual(data["reason"], "")

    async def test_state_requires_both_ids(self):
        resp = await self.mod._handle_chat_get(_Req(query={"run_id": "run1"}))
        self.assertEqual(resp.status, 400)

    async def test_start_refuses_a_deleted_run(self):
        self._record()
        self.mod._RUNS = []
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 409)
        self.assertEqual(json.loads(resp.body)["code"], self.mod.followup.ERR_RUN_GONE)

    async def test_start_refuses_when_the_transcript_is_gone(self):
        self._record()
        (self.sessions_dir / "sid-1.json").unlink()
        self.mod._APP_STATE["state"] = _FakeState(_FakeSessions())
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 409)
        self.assertEqual(json.loads(resp.body)["code"], self.mod.followup.ERR_TRANSCRIPT_GONE)

    async def test_start_seeds_the_resume_and_returns_the_slot(self):
        self._record()
        sessions = _FakeSessions()
        self.mod._APP_STATE["state"] = _FakeState(sessions)
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body)
        expected_key = self.mod.followup.slot_key("run1", "GH-o-r-42")
        self.assertEqual(data["slot_key"], expected_key)
        self.assertEqual(data["agent"], "sage-reviewer")
        self.assertTrue(data["folder_id"])
        self.assertTrue(data["title"].startswith("followup-pr#42"))
        # Seeded under the session key a dashboard slot resolves its resume
        # from, carrying the provider and cwd the review actually ran with: a
        # provider mismatch makes the dashboard DISCARD the session id, and the
        # cwd is what the reviewer's relative paths were written against.
        self.assertEqual(sessions.seeds, [(f"dashboard:{expected_key}", "sid-1", "acp", "/work")])

    async def test_start_does_not_reseed_an_existing_conversation(self):
        """Re-seeding would point a follow-up conversation back at the review's
        own starting transcript and discard everything discussed since."""
        self._record()
        key = self.mod.followup.slot_key("run1", "GH-o-r-42")
        sessions = _FakeSessions({f"dashboard:{key}": ("later-sid", "acp", "/w")})
        self.mod._APP_STATE["state"] = _FakeState(sessions)
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(sessions.seeds, [])

    async def test_start_refuses_when_the_seed_does_not_stick(self):
        """The session map self-prunes an entry whose files are gone, so the
        read-back is the last check that there is anything to resume."""
        self._record()
        sessions = _FakeSessions()
        sessions.drop_seeded = True
        self.mod._APP_STATE["state"] = _FakeState(sessions)
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 409)
        self.assertEqual(json.loads(resp.body)["code"], self.mod.followup.ERR_TRANSCRIPT_GONE)

    async def test_start_reports_unavailable_sessions_rather_than_pretending(self):
        self._record()
        self.mod._APP_STATE["state"] = _FakeState(None)
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 503)

    async def test_the_folder_is_adopted_not_duplicated(self):
        self._record()
        self._record(sid="sid-2", change="GH-o-r-43")
        state = _FakeState(_FakeSessions())
        self.mod._APP_STATE["state"] = state
        first = json.loads(
            (
                await self.mod._handle_followup_start(
                    _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        second = json.loads(
            (
                await self.mod._handle_followup_start(
                    _Req({"run_id": "run1", "change_id": "GH-o-r-43"})
                )
            ).body
        )
        self.assertEqual(first["folder_id"], second["folder_id"])
        self.assertEqual([f["name"] for f in state._folders], [self.mod.followup.FOLDER_NAME])

    async def test_a_disabled_app_answers_nothing(self):
        """Disabling an app withdraws its runtime, not just its UI — a request
        already in flight is the one no teardown hook can reach."""
        self._record()
        self.mod.is_app_enabled = lambda name: False
        self.mod._APP_STATE["state"] = _FakeState(_FakeSessions())
        for resp in (
            await self.mod._handle_chat_get(
                _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
            ),
            await self.mod._handle_followup_start(
                _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
            ),
        ):
            self.assertEqual(resp.status, 403)
            self.assertEqual(json.loads(resp.body)["code"], "app_disabled")


class TestFollowupRunLiveAndReentry(unittest.IsolatedAsyncioTestCase):
    """A live run must not be offerable, and a returning user must see re-entry.

    The mid-run window is the reachable one: a first pass writes a descriptor, the
    panel offers it, and a successful second coverage pass then retires that
    descriptor -- leaving an already-open conversation pointing at findings the run
    replaced, with nothing saying so.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        self.mod.is_app_enabled = lambda name: True
        self.sessions_dir = Path(self.tmp) / "kiro-sessions"
        self.sessions_dir.mkdir(parents=True)
        self.mod.followup.kiro_sessions_dir = lambda: self.sessions_dir
        store.ensure_layout()
        store.ensure_run_layout("run1")
        (self.sessions_dir / "sid-1.json").write_text("{}", encoding="utf-8")
        self.mod.followup.write_descriptor(
            "run1", "GH-o-r-42", sid="sid-1", agent="sage-reviewer", cwd="/work"
        )
        self.sessions = _FakeSessions()
        self.state = _FakeState(self.sessions)
        self.mod._APP_STATE["state"] = self.state

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, **over):
        run = {"run_id": "run1", "status": "done"}
        run.update(over)
        self.mod._RUNS = [run]

    async def test_a_running_run_is_not_offerable(self):
        self._run(status="running")
        data = json.loads(
            (
                await self.mod._handle_chat_get(
                    _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        self.assertFalse(data["resumable"])
        self.assertEqual(data["reason"], self.mod.followup.ERR_RUN_LIVE)

    async def test_a_posting_run_is_not_offerable_either(self):
        """Posting happens AFTER a terminal status, so the status check alone
        would let this through."""
        self._run(status="done", posting=True)
        data = json.loads(
            (
                await self.mod._handle_chat_get(
                    _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        self.assertFalse(data["resumable"])
        self.assertEqual(data["reason"], self.mod.followup.ERR_RUN_LIVE)

    async def test_start_refuses_while_the_run_is_live(self):
        self._run(status="running")
        resp = await self.mod._handle_followup_start(
            _Req({"run_id": "run1", "change_id": "GH-o-r-42"})
        )
        self.assertEqual(resp.status, 409)
        self.assertEqual(json.loads(resp.body)["code"], self.mod.followup.ERR_RUN_LIVE)
        self.assertEqual(self.sessions.seeds, [])

    async def test_a_finished_run_is_offerable(self):
        self._run(status="done")
        data = json.loads(
            (
                await self.mod._handle_chat_get(
                    _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        self.assertTrue(data["resumable"])
        self.assertEqual(data["reason"], "")

    async def _open_flag(self):
        data = json.loads(
            (
                await self.mod._handle_chat_get(
                    _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        return data["followup_open"]

    async def test_reentry_is_reported_once_a_session_exists(self):
        """Without this the panel invites a returning user to "open" a
        conversation they already had, with no trace of it -- which reads as the
        review having lost it."""
        self._run(status="done")
        key = self.mod.followup.slot_key("run1", "GH-o-r-42")
        self.assertFalse(await self._open_flag())
        self.state._slots[key] = object()
        self.assertTrue(await self._open_flag())

    async def test_a_surviving_mapping_is_not_an_existing_session(self):
        """Closing a tab pops the slot but KEEPS its mapping, so a resume can
        reload it later. Reading existence off the map therefore reported a closed
        session as open, and the recreate that followed omitted the title and
        folder -- leaving the session unfiled under the placeholder title.
        """
        self._run(status="done")
        key = self.mod.followup.slot_key("run1", "GH-o-r-42")
        self.sessions.map[f"dashboard:{key}"] = ("sid-1", "acp", "/work")
        self.assertFalse(await self._open_flag())

    async def test_reentry_is_false_when_state_is_unavailable(self):
        self._run(status="done")
        self.mod._APP_STATE["state"] = _FakeState(None)
        self.assertFalse(await self._open_flag())
        data = json.loads(
            (
                await self.mod._handle_chat_get(
                    _Req(query={"run_id": "run1", "change_id": "GH-o-r-42"})
                )
            ).body
        )
        self.assertTrue(data["resumable"])


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


class TestLocalFixOneRunPerRepository(unittest.IsolatedAsyncioTestCase):
    """The direct-fix path edits the USER'S checkout (no candidate worktree).

    Its staleness check is check-then-act, so two concurrent requests could
    both pass it and spawn two fix agents racing on the same tree. The handler
    now registers the repository under _LOCAL_LOCK BEFORE create_task; the
    second request gets 409 fix_in_progress, and the registry entry is
    released when the run finishes (success or failure).
    """

    def setUp(self):
        self.mod = _load_routes_module()
        self.mod._ACTIVE_FIX_REPOS.clear()

    def tearDown(self):
        self.mod._ACTIVE_FIX_REPOS.clear()

    class _Req:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    async def test_a_second_fix_for_the_same_repo_is_rejected_409(self):
        session = {
            "id": "sess-1",
            "repository": "/repo",
            "mode": "all-working-tree",
            "status": "completed",
            "revision": "rev-1",
            "findings": [{"id": "f1", "status": "open"}],
            "fix_runs": [],
        }
        calls = {"save": 0, "diff": 0}

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "")
            if name == "save_session":
                calls["save"] += 1
                return None
            if name == "validate_repository":
                return Path("/repo")
            if name == "is_app_enabled":
                return True
            if name == "working_tree_diff":
                calls["diff"] += 1

                class _Diff:
                    revision = "rev-1"

                return _Diff()
            return fn(*args, **kwargs)

        created = []

        class FakeTask:
            def add_done_callback(self, cb):
                pass

        def fake_create_task(coro):
            created.append(coro)
            return FakeTask()

        with (
            unittest.mock.patch.object(self.mod.asyncio, "to_thread", side_effect=fake_to_thread),
            unittest.mock.patch.object(self.mod, "is_app_enabled", lambda app: True),
            unittest.mock.patch.object(self.mod.local_review, "load_session", lambda sid: session),
            unittest.mock.patch.object(
                self.mod.asyncio, "create_task", side_effect=fake_create_task
            ),
        ):
            first = await self.mod._handle_local_fix(
                _Req(body={"session_id": "sess-1", "finding_ids": ["f1"], "instruction": "fix"})
            )
            self.assertEqual(first.status, 202)
            self.assertEqual(self.mod._ACTIVE_FIX_REPOS, {"/repo"})

            second = await self.mod._handle_local_fix(
                _Req(body={"session_id": "sess-1", "finding_ids": ["f1"], "instruction": "fix"})
            )
            self.assertEqual(second.status, 409)
            self.assertEqual(json.loads(second.body)["code"], "fix_in_progress")

        # Cancel the never-run background coroutine so the loop shuts cleanly.
        for coro in created:
            coro.close()
        # Both requests reached the staleness re-check (check-then-act); the
        # registry is what rejected the second, not the diff itself.
        self.assertEqual(calls["diff"], 2)

    async def test_the_registry_slot_is_released_after_the_run(self):
        mod = self.mod
        mod._ACTIVE_FIX_REPOS.add("/repo")

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "")
            if name == "save_session":
                return None
            if name == "_run_git":
                return "M changed.txt"
            return fn(*args, **kwargs)

        class FakePool:
            async def begin_batch(self):
                pass

            async def send(self, prompt, timeout=0.0):
                return "done"

            async def end_batch(self):
                pass

            async def shutdown(self):
                pass

        with (
            unittest.mock.patch.object(mod.asyncio, "to_thread", side_effect=fake_to_thread),
            unittest.mock.patch.object(mod.review_pool, "ReviewPool", lambda **kw: FakePool()),
        ):
            await mod._local_fix_bg(
                {"id": "s", "repository": "/repo", "findings": [], "fix_runs": []},
                [],
                "fixid",
                "instruction",
            )

        self.assertEqual(mod._ACTIVE_FIX_REPOS, set(), "slot must be released")

    async def test_the_registry_slot_is_released_after_a_failure(self):
        mod = self.mod
        mod._ACTIVE_FIX_REPOS.add("/repo")

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "")
            if name == "save_session":
                return None
            return fn(*args, **kwargs)

        def boom(**kw):
            raise RuntimeError("agent spawn failed")

        with (
            unittest.mock.patch.object(mod.asyncio, "to_thread", side_effect=fake_to_thread),
            unittest.mock.patch.object(mod.review_pool, "ReviewPool", boom),
        ):
            session = {"id": "s", "repository": "/repo", "findings": [], "fix_runs": []}
            await mod._local_fix_bg(session, [], "fixid", "instruction")

        self.assertEqual(mod._ACTIVE_FIX_REPOS, set(), "slot must be released")


class TestLocalFixHeadRevisionCas(unittest.IsolatedAsyncioTestCase):
    """The fix turn writes the USER'S checkout directly, so the revision that
    the request-time staleness check approved must be re-verified around the
    agent turn: just before spawning (edits landing in that window would be
    silently overwritten) and just after (edits during the turn must not be
    folded into a success report). No polling — a single check each side.
    """

    def setUp(self):
        self.mod = _load_routes_module()
        self.mod._ACTIVE_FIX_REPOS.clear()

    def tearDown(self):
        self.mod._ACTIVE_FIX_REPOS.clear()

    def _session(self):
        session = {
            "id": "sess-cas",
            "repository": "/repo",
            "mode": "all-working-tree",
            "status": "completed",
            "revision": "rev-1",
            "findings": [{"id": "f1", "status": "open"}],
            "fix_runs": [],
        }
        return session

    def _patches(self, revisions):
        """Stub to_thread so working_tree_diff yields the given revisions in
        order; the status sweep (a lambda over _run_git) reports a fixed file."""
        queue = list(revisions)

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "")
            if name == "working_tree_diff":

                class _Diff:
                    revision = queue.pop(0) if queue else "rev-1"

                return _Diff()
            if name == "save_session":
                return None
            if name == "<lambda>":  # the git status sweep in _local_fix_bg
                return ["M fixed.txt"]
            return fn(*args, **kwargs)

        return fake_to_thread

    class _RecordingPool:
        """Records the prompt to prove whether the agent turn happened."""

        def __init__(self):
            self.sent: list[str] = []

        async def begin_batch(self):
            pass

        async def send(self, prompt, timeout=0.0):
            self.sent.append(prompt)
            return "done"

        async def end_batch(self):
            pass

        async def shutdown(self):
            pass

    def _run_bg(self, session, pool, baseline, revisions):
        fake_to_thread = self._patches(revisions)
        return (
            unittest.mock.patch.object(self.mod.asyncio, "to_thread", side_effect=fake_to_thread),
            unittest.mock.patch.object(self.mod.review_pool, "ReviewPool", lambda **kw: pool),
            baseline,
            session,
        )

    async def test_local_fix_fails_when_tree_changes_before_the_fix_turn(self):
        session = self._session()
        pool = self._RecordingPool()
        # Sequence: pre-turn re-check sees "rev-2" != baseline "rev-1".
        to_thread_patch, pool_patch, baseline, _session = self._run_bg(
            session, pool, "rev-1", ["rev-2"]
        )
        with to_thread_patch, pool_patch:
            await self.mod._local_fix_bg(session, [], "fix-1", "instruction", baseline)

        self.assertEqual(pool.sent, [], "the agent must not spawn on a moved tree")
        fix = session["fix_runs"][0]
        self.assertEqual(fix["status"], "failed")
        self.assertIn("revision mismatch", fix["error"])
        self.assertIn("re-review and retry", fix["error"])
        # The slot is released on this path too.
        self.assertEqual(self.mod._ACTIVE_FIX_REPOS, set())

    async def test_a_revision_mismatch_persists_the_terminal_state(self):
        # The mismatch branch returns from INSIDE the try, so the session save
        # must run in the finally: what the user reads back is the persisted
        # session, and a save that only the happy path reached would leave the
        # fix "running" with its findings stuck in "fixing" forever. The save
        # is a read-modify-write against the on-disk session, so the merge
        # source is a simulated disk copy — never the in-memory object.
        session = self._session()
        pool = self._RecordingPool()
        saves: list[dict] = []
        queue = ["rev-2"]  # pre-turn re-check sees a moved tree

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "")
            if name == "working_tree_diff":

                class _Diff:
                    revision = queue.pop(0) if queue else "rev-1"

                return _Diff()
            return fn(*args, **kwargs)

        def fake_load(session_id):
            # A fresh copy per read: mutations must flow through the merge,
            # not leak by object identity.
            return json.loads(json.dumps(session))

        def fake_save(disk):
            saves.append(json.loads(json.dumps(disk)))

        with (
            unittest.mock.patch.object(self.mod.asyncio, "to_thread", side_effect=fake_to_thread),
            unittest.mock.patch.object(
                self.mod.local_review, "load_session", side_effect=fake_load
            ),
            unittest.mock.patch.object(
                self.mod.local_review, "save_session", side_effect=fake_save
            ),
            unittest.mock.patch.object(self.mod.review_pool, "ReviewPool", lambda **kw: pool),
        ):
            await self.mod._local_fix_bg(
                session, session["findings"], "fix-1", "instruction", "rev-1"
            )

        # One write marking the turn started, then the terminal write.
        self.assertEqual(len(saves), 2)
        terminal = saves[-1]
        self.assertEqual(terminal["fix_runs"][0]["status"], "failed")
        self.assertTrue(terminal["fix_runs"][0].get("completed_at"))
        self.assertEqual([f["status"] for f in terminal["findings"]], ["open"])
        self.assertEqual([f["status"] for f in session["findings"]], ["open"])
        self.assertEqual(self.mod._ACTIVE_FIX_REPOS, set())

    async def test_local_fix_reports_conflicts_when_tree_changes_during_the_fix_turn(self):
        session = self._session()
        pool = self._RecordingPool()
        # Sequence: pre-turn re-check passes ("rev-1"), the turn runs, the
        # post-turn re-check sees "rev-2" != baseline "rev-1".
        to_thread_patch, pool_patch, baseline, _session = self._run_bg(
            session, pool, "rev-1", ["rev-1", "rev-2"]
        )
        with to_thread_patch, pool_patch:
            await self.mod._local_fix_bg(session, [], "fix-1", "instruction", baseline)

        self.assertEqual(len(pool.sent), 1, "the fix turn itself must run")
        fix = session["fix_runs"][0]
        self.assertEqual(fix["status"], "completed_with_conflicts")
        self.assertNotEqual(fix["status"], "completed")
        # The agent's files are still listed for triage.
        self.assertEqual(fix["changed_files"], ["M fixed.txt"])

    async def test_local_fix_happy_path_records_changed_files(self):
        session = self._session()
        pool = self._RecordingPool()
        # Sequence: pre- and post-turn re-checks both match the baseline.
        to_thread_patch, pool_patch, baseline, _session = self._run_bg(
            session, pool, "rev-1", ["rev-1", "rev-1"]
        )
        with to_thread_patch, pool_patch:
            await self.mod._local_fix_bg(session, [], "fix-1", "instruction", baseline)

        self.assertEqual(len(pool.sent), 1)
        fix = session["fix_runs"][0]
        self.assertEqual(fix["status"], "completed")
        self.assertEqual(fix["changed_files"], ["M fixed.txt"])

    async def test_the_handler_pins_the_baseline_at_acceptance(self):
        # The baseline handed to the background run must be the revision the
        # staleness check approved (captured under _LOCAL_LOCK), not a
        # re-read that could race with later edits.
        session = self._session()
        seen_baselines: list = []
        created: list = []

        class FakeTask:
            def add_done_callback(self, cb):
                pass

        def fake_create_task(coro):
            created.append(coro)
            return FakeTask()

        captured_calls: list = []

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "")
            if name == "working_tree_diff":
                captured_calls.append(name)

                class _Diff:
                    revision = "rev-1"

                return _Diff()
            if name == "save_session":
                return None
            return fn(*args, **kwargs)

        # Record the baseline AT CALL TIME: create_task is faked, so the spy
        # coroutine never runs its body.
        def spy_bg(_session, findings, fix_id, instruction, baseline_revision=None):
            seen_baselines.append(baseline_revision)
            return asyncio.sleep(0)

        with (
            unittest.mock.patch.object(self.mod.asyncio, "to_thread", side_effect=fake_to_thread),
            unittest.mock.patch.object(self.mod, "is_app_enabled", lambda app: True),
            unittest.mock.patch.object(self.mod.local_review, "load_session", lambda sid: session),
            unittest.mock.patch.object(
                self.mod.asyncio, "create_task", side_effect=fake_create_task
            ),
            unittest.mock.patch.object(self.mod, "_local_fix_bg", spy_bg),
        ):
            response = await self.mod._handle_local_fix(
                self._Req(
                    body={"session_id": "sess-cas", "finding_ids": ["f1"], "instruction": "fix"}
                )
            )

        self.assertEqual(response.status, 202)
        self.assertEqual(seen_baselines, ["rev-1"])
        for coro in created:
            coro.close()

    class _Req:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body
