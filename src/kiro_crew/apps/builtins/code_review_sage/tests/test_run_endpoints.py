"""Tests for the per-run + repo-discovery HTTP handlers in backend/routes.py.

These cover the endpoints added for the "one review = one thread" rework:
  * GET    /runs/{id}          — one run's descriptor (+ 404)
  * GET    /runs/{id}/report   — the run's Focus Report as data (ready:false
                                 while it has none, ready:true once written)
  * POST   /runs/{id}/cancel   — cooperative cancel (409 non-running, 404 unknown)
  * DELETE /runs/{id}          — dismiss + delete the on-disk run dir (409 live)
  * POST   /runs/{id}/archive  — re-archive path (existing-slug shortcut, 502 no html)
  * GET    /recent-repos       — gh-derived repo picker (happy / setup / 502 / validation)
  * GET|POST|DELETE /repos     — pinned-repo round-trip

Plus the registry-maintenance helpers ``_record`` (bounded eviction deletes the
evicted run's dir) and ``_reap_orphan_run_dirs`` (deletes dirs with no registry
entry), and the path-param sanitizer that keeps a traversal-shaped run id inside
the runs dir.

Handlers are exercised directly with lightweight fake request objects — the same
style as ``test_backend_routes.py`` — rather than through a live aiohttp client,
because the handlers only touch ``request.match_info`` / ``request.query`` /
``request.json()`` / ``request.method``.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from aiohttp import web

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


class _Req:
    """Minimal stand-in for an aiohttp request.

    ``run_id`` -> ``match_info``; ``query`` -> query string params; ``method`` +
    ``body`` back ``request.json()``. A ``None`` body raises from ``json()`` like
    a real request with no/invalid JSON body, exercising the handlers'
    ``except`` fallbacks."""

    def __init__(self, *, run_id=None, query=None, method="GET", body=None):
        self.match_info = {} if run_id is None else {"run_id": run_id}
        self.query = query or {}
        self.method = method
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class _RunEndpointBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()
        self.mod._RUNS = []
        self.mod._CANCELLED.clear()
        store.ensure_layout()

    def tearDown(self):
        self.mod._CANCELLED.clear()
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRunDetail(_RunEndpointBase):
    async def test_detail_200(self):
        self.mod._RUNS = [{"run_id": "r1", "status": "done", "changes": ["CR-1"]}]
        resp = await self.mod._handle_run_detail(_Req(run_id="r1"))
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["run"]["run_id"], "r1")

    async def test_detail_404_for_unknown(self):
        resp = await self.mod._handle_run_detail(_Req(run_id="nope"))
        self.assertEqual(resp.status, 404)


class TestRunReport(_RunEndpointBase):
    async def test_report_not_ready_for_run_without_report(self):
        self.mod._RUNS = [{"run_id": "live", "status": "running"}]
        resp = await self.mod._handle_run_report(_Req(run_id="live"))
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body)
        self.assertFalse(data["ready"])
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["bands"], {"red": 0, "yellow": 0, "green": 0})
        self.assertEqual(data["status"], "running")

    async def test_report_ready_once_written(self):
        self.mod._RUNS = [{"run_id": "rr1", "status": "done"}]
        report_dict = {
            "bands": {"red": 1, "yellow": 0, "green": 0},
            "rows": [{"change_id": "GH-a-b-1", "url": "https://github.com/a/b/pull/1",
                      "band": "red", "why": "1x red", "score": 75}],
            "generated_at": "2026-01-01T00:00:00Z",
        }
        # write_outputs persists report.json + index.json under the run dir; the
        # report endpoint reads those (never the artifact store).
        self.mod.report.write_outputs(report_dict, "<html>focus</html>", run_id="rr1")
        resp = await self.mod._handle_run_report(_Req(run_id="rr1"))
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body)
        self.assertTrue(data["ready"])
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["change_id"], "GH-a-b-1")
        self.assertEqual(data["bands"]["red"], 1)

    async def test_report_404_for_unknown_run(self):
        resp = await self.mod._handle_run_report(_Req(run_id="ghost"))
        self.assertEqual(resp.status, 404)


class TestRunCancel(_RunEndpointBase):
    async def test_cancel_running_marks_cancelled(self):
        self.mod._RUNS = [{"run_id": "c1", "status": "running"}]
        resp = await self.mod._handle_run_cancel(_Req(run_id="c1", method="POST"))
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["status"], "cancelling")
        self.assertIn("c1", self.mod._CANCELLED)

    async def test_cancel_non_running_409(self):
        self.mod._RUNS = [{"run_id": "c2", "status": "done"}]
        resp = await self.mod._handle_run_cancel(_Req(run_id="c2", method="POST"))
        self.assertEqual(resp.status, 409)
        self.assertNotIn("c2", self.mod._CANCELLED)

    async def test_cancel_unknown_404(self):
        resp = await self.mod._handle_run_cancel(_Req(run_id="c3", method="POST"))
        self.assertEqual(resp.status, 404)


class TestRunDelete(_RunEndpointBase):
    async def test_delete_removes_entry_and_dir(self):
        self.mod._RUNS = [{"run_id": "d1", "status": "done"}]
        store.ensure_run_layout("d1")
        self.assertTrue(store.run_dir("d1").exists())
        resp = await self.mod._handle_run_delete(_Req(run_id="d1", method="DELETE"))
        self.assertEqual(resp.status, 200)
        self.assertIsNone(self.mod._find_run("d1"))
        self.assertFalse(store.run_dir("d1").exists())

    async def test_delete_while_posting_409(self):
        """Posting happens on a TERMINAL run, so the running check misses it.

        Deleting mid-post removes the run while the poster may still be delivering
        to the pull request: the record of what landed is lost, and the poster can
        recreate an orphan run dir after the delete.
        """
        self.mod._RUNS = [{"run_id": "dp", "status": "done", "posting": True}]
        store.ensure_run_layout("dp")
        resp = await self.mod._handle_run_delete(_Req(run_id="dp", method="DELETE"))
        self.assertEqual(resp.status, 409)
        self.assertIsNotNone(self.mod._find_run("dp"))
        self.assertTrue(store.run_dir("dp").exists())

    async def test_delete_running_409(self):
        self.mod._RUNS = [{"run_id": "d2", "status": "running"}]
        store.ensure_run_layout("d2")
        resp = await self.mod._handle_run_delete(_Req(run_id="d2", method="DELETE"))
        self.assertEqual(resp.status, 409)
        # dir must survive — deleting it under the live driver would corrupt the run
        self.assertTrue(store.run_dir("d2").exists())
        self.assertIsNotNone(self.mod._find_run("d2"))

    async def test_delete_unknown_404(self):
        resp = await self.mod._handle_run_delete(_Req(run_id="d3", method="DELETE"))
        self.assertEqual(resp.status, 404)


class TestRunArchive(_RunEndpointBase):
    async def test_archive_returns_existing_slug_without_rearchiving(self):
        self.mod._RUNS = [{"run_id": "a1", "status": "done", "report_slug": "sage-report-xyz"}]
        with unittest.mock.patch.object(
                self.mod.review_driver, "archive_report",
                side_effect=AssertionError("must not re-archive when a slug exists")):
            resp = await self.mod._handle_run_archive(_Req(run_id="a1", method="POST"))
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["report_slug"], "sage-report-xyz")
        self.assertFalse(data["created"])

    async def test_archive_502_when_no_report_html_on_disk(self):
        self.mod._RUNS = [{"run_id": "a2", "status": "done"}]
        # archive_report should never be reached — the html read fails first — but
        # patch it so a regression that DID call it can't hit the real artifact API.
        with unittest.mock.patch.object(
                self.mod.review_driver, "archive_report", return_value="unexpected"):
            resp = await self.mod._handle_run_archive(_Req(run_id="a2", method="POST"))
        self.assertEqual(resp.status, 502)

    async def test_archive_unknown_404(self):
        resp = await self.mod._handle_run_archive(_Req(run_id="a3", method="POST"))
        self.assertEqual(resp.status, 404)


class TestRunIdSanitization(_RunEndpointBase):
    """Containment, at both layers that can enforce it.

    The endpoint refuses a traversal-shaped id outright; `store.run_dir` still has
    to contain one, because ids also reach it from callers that never saw a URL."""

    async def test_store_contains_a_traversal_id_that_never_came_from_a_url(self):
        safe = store.safe_run_id("../../etc/passwd")
        # Every path separator is collapsed, so a separator-bearing id becomes a
        # single path segment ("..","/" -> "_") that cannot climb out of runs/.
        self.assertNotIn("/", safe)
        self.assertNotIn(os.sep, safe)
        # …and the run dir built from it stays directly under the runs root.
        rd = store.run_dir("../../etc/passwd")
        self.assertEqual(rd.resolve().parent, store.runs_root().resolve())

    async def test_traversal_delete_cannot_escape(self):
        # A DELETE with a traversal-shaped id is refused at the boundary: aiohttp
        # renders the raised HTTPNotFound as the same 404 a missing run gets, and
        # no filesystem path is derived from the id at all.
        with self.assertRaises(web.HTTPNotFound) as caught:
            await self.mod._handle_run_delete(
                _Req(run_id="../../../etc", method="DELETE"))
        self.assertEqual(caught.exception.status, 404)


class TestRegistryMaintenance(_RunEndpointBase):
    async def test_record_evicts_oldest_and_deletes_its_dir(self):
        self.mod._RUNS_MAX = 2  # shrink the bound for the test (module reloaded per setUp)
        store.ensure_run_layout("oldrun")
        self.assertTrue(store.run_dir("oldrun").exists())
        self.mod._RUNS = [{"run_id": "r2", "status": "done"},
                          {"run_id": "oldrun", "status": "done"}]
        await self.mod._record({"run_id": "new", "status": "running"})
        ids = [r["run_id"] for r in self.mod._RUNS]
        self.assertEqual(ids, ["new", "r2"])          # newest first, oldest evicted
        self.assertFalse(store.run_dir("oldrun").exists())  # evicted dir deleted

    def test_reap_orphan_run_dirs(self):
        store.ensure_run_layout("known1")
        store.ensure_run_layout("orphan1")
        self.mod._RUNS = [{"run_id": "known1", "status": "done"}]
        removed = self.mod._reap_orphan_run_dirs()
        self.assertEqual(removed, 1)
        self.assertTrue(store.run_dir("known1").exists())   # registered dir kept
        self.assertFalse(store.run_dir("orphan1").exists())  # orphan reaped


class TestRecentRepos(_RunEndpointBase):
    async def test_happy_path_annotates_pinned(self):
        with unittest.mock.patch.object(
                self.mod.discovery, "read_repos",
                return_value=[{"owner": "acme", "repo": "widget",
                               "full_name": "acme/widget"}]), \
             unittest.mock.patch.object(
                self.mod.discovery, "current_login", return_value="octocat"), \
             unittest.mock.patch.object(
                self.mod.discovery, "list_contributed_repos",
                return_value=([{"owner": "acme", "repo": "widget",
                                "full_name": "acme/widget"},
                               {"owner": "other", "repo": "svc",
                                "full_name": "other/svc"}], False)):
            resp = await self.mod._handle_recent_repos(_Req(method="GET"))
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["login"], "octocat")
        self.assertFalse(data["truncated"])
        by_name = {r["full_name"]: r for r in data["repos"]}
        self.assertTrue(by_name["acme/widget"]["pinned"])       # already pinned
        self.assertFalse(by_name["other/svc"]["pinned"])        # not pinned

    async def test_setup_required_returns_200(self):
        with unittest.mock.patch.object(
                self.mod.discovery, "read_repos", return_value=[]), \
             unittest.mock.patch.object(
                self.mod.discovery, "current_login",
                side_effect=self.mod.discovery.GhSetupError("run gh auth login")):
            resp = await self.mod._handle_recent_repos(_Req(method="GET"))
        # "you need to set up gh" is a normal first-run state, not an error status.
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body)
        self.assertTrue(data["setup_required"])

    async def test_gh_error_returns_502(self):
        with unittest.mock.patch.object(
                self.mod.discovery, "read_repos", return_value=[]), \
             unittest.mock.patch.object(
                self.mod.discovery, "current_login", return_value="octocat"), \
             unittest.mock.patch.object(
                self.mod.discovery, "list_contributed_repos",
                side_effect=self.mod.discovery.GhError("gh api blew up")):
            resp = await self.mod._handle_recent_repos(_Req(method="GET"))
        self.assertEqual(resp.status, 502)

    async def test_days_must_be_integer(self):
        resp = await self.mod._handle_recent_repos(
            _Req(method="GET", query={"days": "notanumber"}))
        self.assertEqual(resp.status, 400)

    async def test_days_out_of_range(self):
        too_big = str(self.mod.discovery.MAX_WINDOW_DAYS + 1)
        resp = await self.mod._handle_recent_repos(
            _Req(method="GET", query={"days": too_big}))
        self.assertEqual(resp.status, 400)
        resp2 = await self.mod._handle_recent_repos(
            _Req(method="GET", query={"days": "-1"}))
        self.assertEqual(resp2.status, 400)


class TestReposCrud(_RunEndpointBase):
    async def test_get_post_delete_roundtrip_owner_repo_pair(self):
        # starts empty
        resp = await self.mod._handle_repos(_Req(method="GET"))
        self.assertEqual(json.loads(resp.body)["repos"], [])
        # POST an owner+repo pair
        resp = await self.mod._handle_repos(
            _Req(method="POST", body={"owner": "acme", "repo": "widget"}))
        self.assertEqual(resp.status, 200)
        repos = json.loads(resp.body)["repos"]
        self.assertEqual(repos[0]["full_name"], "acme/widget")
        # GET reflects it
        resp = await self.mod._handle_repos(_Req(method="GET"))
        self.assertEqual(len(json.loads(resp.body)["repos"]), 1)
        # DELETE removes it
        resp = await self.mod._handle_repos(
            _Req(method="DELETE", body={"owner": "acme", "repo": "widget"}))
        self.assertEqual(json.loads(resp.body)["repos"], [])

    async def test_post_accepts_repo_url_form(self):
        resp = await self.mod._handle_repos(
            _Req(method="POST", body={"repo": "https://github.com/acme/widget"}))
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body)["repos"][0]["full_name"], "acme/widget")

    async def test_invalid_repo_400(self):
        resp = await self.mod._handle_repos(
            _Req(method="POST", body={"repo": "https://evil.example/a/b"}))
        self.assertEqual(resp.status, 400)

    async def test_missing_fields_400(self):
        resp = await self.mod._handle_repos(_Req(method="POST", body={}))
        self.assertEqual(resp.status, 400)


class TestRunIdParamRejectsMalformed(_RunEndpointBase):
    """A ``{run_id}`` that is not already its own safe form must 404 rather than
    be repaired into a DIFFERENT, real run's id.

    ``store.safe_run_id`` collapses unsafe characters to ``_`` and strips them
    from the ends, so ``<valid-id>!`` sanitizes to ``<valid-id>``. Repairing the
    param would let two distinct URLs address the same run, and a mangled id
    would quietly act on a real run's report instead of failing."""

    def test_clean_id_passes_through(self):
        self.assertEqual(self.mod._run_id_param(_Req(run_id="a1b2c3d4e5f6")), "a1b2c3d4e5f6")

    def test_trailing_unsafe_char_does_not_alias_to_the_real_run(self):
        self.assertEqual(store.safe_run_id("a1b2c3d4e5f6!"), "a1b2c3d4e5f6")
        with self.assertRaises(web.HTTPNotFound):
            self.mod._run_id_param(_Req(run_id="a1b2c3d4e5f6!"))

    def test_traversal_attempt_is_rejected_not_collapsed(self):
        with self.assertRaises(web.HTTPNotFound):
            self.mod._run_id_param(_Req(run_id="../../etc/passwd"))

    def test_empty_id_is_rejected(self):
        with self.assertRaises(web.HTTPNotFound):
            self.mod._run_id_param(_Req(run_id=""))


if __name__ == "__main__":
    unittest.main()
