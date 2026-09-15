"""Tests for the connect dialog's contributed-repos picker backend.

Two deterministic, subprocess-free surfaces:
  * ``github_client.list_contributed_repos`` — the ``gh api`` path it builds,
    which event types count as a contribution, the trailing window, the
    per-repo "my last contribution" rollup, and newest-first ordering
    (monkeypatches ``_run_gh_api``, so no real ``gh`` runs);
  * ``routes._handle_recent_repos`` — login resolution, the ``connected`` flag
    it stamps by diffing against config.json, and the 400/502 error mapping.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import routes, store


def _iso_days_ago(days: float) -> str:
    """A GitHub-style UTC stamp ``days`` in the past."""
    return (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ev(repo: str, days_ago: float, kind: str = "PushEvent") -> dict:
    return {"type": kind, "repo": repo, "created_at": _iso_days_ago(days_ago)}


def _req(query: dict | None = None):
    """A real (mocked) aiohttp Request for the handler under test.

    aiohttp's own ``make_mocked_request`` rather than a hand-rolled stand-in:
    the handler is typed ``(web.Request) -> web.Response``, so a duck-typed
    stub fails the repo-wide ``mypy src/kiro_crew/`` gate.
    """
    path = "/api/apps/issue-radar/recent-repos"
    if query:
        path = f"{path}?{urlencode(query)}"
    return make_mocked_request("GET", path)


def _body(response):
    return json.loads(response.body.decode())


class TestListContributedRepos(unittest.TestCase):
    def _run(self, events, **kwargs):
        with mock.patch.object(gh, "_run_gh_api", return_value=events) as run:
            rows, _trunc = gh.list_contributed_repos(kwargs.pop("login", "octocat"), **kwargs)
        return rows, run

    def test_reads_the_users_event_feed(self):
        _, run = self._run([])
        path = run.call_args[0][0]
        self.assertIn("users/octocat/events", path)
        self.assertIn("per_page=100", path)
        # A picker, not an audit — must not walk the whole feed.
        self.assertFalse(run.call_args.kwargs["paginate"])

    def test_login_is_url_escaped(self):
        _, run = self._run([], login="weird/../name")
        self.assertNotIn("weird/../name", run.call_args[0][0])

    def test_rolls_up_last_contribution_per_repo(self):
        # The expected stamp is RETAINED, not recomputed: `_ev` derives its
        # timestamp from `now`, so calling it again in the assertion yields a
        # different second whenever the test crosses a second boundary.
        newest = _ev("o/alpha", 1)
        rows, _ = self._run([
            _ev("o/alpha", 5),
            newest,   # newer — should win
            _ev("o/beta", 3),
        ])
        by_name = {r["full_name"]: r for r in rows}
        self.assertEqual(by_name["o/alpha"]["contribution_count"], 2)
        # The 1-day-old stamp is the one surfaced.
        self.assertEqual(by_name["o/alpha"]["last_contributed_at"], newest["created_at"])
        self.assertEqual(by_name["o/beta"]["contribution_count"], 1)

    def test_newest_contribution_first(self):
        rows, _ = self._run([_ev("o/old", 20), _ev("o/new", 2), _ev("o/mid", 9)])
        self.assertEqual([r["full_name"] for r in rows], ["o/new", "o/mid", "o/old"])

    def test_owner_and_repo_are_split(self):
        rows, _ = self._run([_ev("acme/widget", 1)])
        self.assertEqual(rows[0]["owner"], "acme")
        self.assertEqual(rows[0]["repo"], "widget")

    def test_non_contribution_events_are_ignored(self):
        # Starring or forking a repo is not contributing to it.
        rows, _ = self._run([
            _ev("o/starred", 1, kind="WatchEvent"),
            _ev("o/forked", 1, kind="ForkEvent"),
            _ev("o/joined", 1, kind="MemberEvent"),
            _ev("o/real", 1, kind="PullRequestEvent"),
        ])
        self.assertEqual([r["full_name"] for r in rows], ["o/real"])

    def test_review_and_issue_events_count(self):
        rows, _ = self._run([
            _ev("o/reviewed", 2, kind="PullRequestReviewEvent"),
            _ev("o/triaged", 3, kind="IssuesEvent"),
            _ev("o/commented", 4, kind="IssueCommentEvent"),
        ])
        self.assertEqual(len(rows), 3)

    def test_window_excludes_older_contributions(self):
        rows, _ = self._run([_ev("o/recent", 3), _ev("o/stale", 45)])
        self.assertEqual([r["full_name"] for r in rows], ["o/recent"])

    def test_custom_and_disabled_window(self):
        events = [_ev("o/recent", 3), _ev("o/stale", 45)]
        rows, _ = self._run(events, within_days=7)
        self.assertEqual([r["full_name"] for r in rows], ["o/recent"])
        rows, _ = self._run(events, within_days=0)
        self.assertEqual(len(rows), 2)
        rows, _ = self._run(events, within_days=-5)  # negative == disabled
        self.assertEqual(len(rows), 2)

    def test_malformed_entries_are_skipped(self):
        rows, _ = self._run([
            {"type": "PushEvent", "repo": None, "created_at": _iso_days_ago(1)},
            {"type": "PushEvent", "repo": "no-slash", "created_at": _iso_days_ago(1)},
            {"type": "PushEvent", "repo": "o/r", "created_at": "not-a-date"},
            {"type": "PushEvent", "repo": "o/r", "created_at": None},
            _ev("o/good", 1),
        ])
        self.assertEqual([r["full_name"] for r in rows], ["o/good"])

    def test_internal_sort_key_is_not_leaked(self):
        rows, _ = self._run([_ev("o/r", 1)])
        self.assertNotIn("_when", rows[0])


class TestTruncationSignal(unittest.TestCase):
    """A full event page means older activity was never examined, so the caller
    must be told rather than presenting the list as exhaustive."""

    def _truncated(self, events) -> bool:
        with mock.patch.object(gh, "_run_gh_api", return_value=events):
            _rows, truncated = gh.list_contributed_repos("octocat")
        return truncated

    def test_full_page_reports_truncated(self):
        full = [_ev(f"o/r{i}", 1) for i in range(gh._EVENT_PAGE_SIZE)]
        self.assertTrue(self._truncated(full))

    def test_partial_page_is_not_truncated(self):
        self.assertFalse(self._truncated([_ev("o/r", 1)]))

    def test_empty_feed_is_not_truncated(self):
        self.assertFalse(self._truncated([]))

    def test_truncation_is_about_the_RAW_page_not_the_filtered_rows(self):
        # A full page of events that all fall outside the window yields zero
        # rows but is still truncated — older pages could hold in-window repos.
        stale_full = [_ev(f"o/r{i}", 90) for i in range(gh._EVENT_PAGE_SIZE)]
        with mock.patch.object(gh, "_run_gh_api", return_value=stale_full):
            rows, truncated = gh.list_contributed_repos("octocat", within_days=30)
        self.assertEqual(rows, [])
        self.assertTrue(truncated)


class TestGhSetupClassification(unittest.TestCase):
    """`gh` unusable because the HOST isn't set up is a distinct, actionable
    failure — the dialog turns it into install / login instructions."""

    def test_auth_markers_raise_setup_error(self):
        for tail in (
            "gh auth login to authenticate",
            "You are not logged in to any GitHub hosts",
            "HTTP 401: Bad credentials",
            "This endpoint requires authentication",
        ):
            with self.assertRaises(gh.GhSetupError) as ctx:
                gh._raise_if_auth_failure(tail)
            self.assertEqual(ctx.exception.reason, "not_authenticated")

    def test_unrelated_failures_are_not_setup_errors(self):
        for tail in ("HTTP 404: Not Found", "connection reset by peer", ""):
            gh._raise_if_auth_failure(tail)  # must not raise

    def test_setup_error_is_a_gh_cli_error(self):
        # Existing `except GhCliError` handlers must keep catching it.
        self.assertTrue(issubclass(gh.GhSetupError, gh.GhCliError))

    @unittest.skipIf(
        sys.platform == "win32",
        "Issue Radar is POSIX-only: _gh_bin() raises the platform guard before it "
        "ever reaches trust validation, which is what this test asserts.",
    )
    def test_rejected_override_binary_is_a_setup_error(self):
        # The common macOS case: gh IS installed, but the Homebrew path is a
        # symlink under a user-writable prefix, so trust validation rejects it.
        # That must reach the UI as actionable setup guidance, not a raw error.
        # Validation lives on the shared runner (github_runner), so it is
        # patched on its owning module, not on github_client.
        # The path is a neutral fixture — the point is that ANY rejected
        # override becomes a setup error, not that a specific prefix does.
        from kiro_crew import github_runner
        github_runner.reset_cache()
        try:
            with mock.patch.dict("os.environ", {"KIROCREW_ISSUE_RADAR_GH": "/fake/prefix/bin/gh"}), \
                 mock.patch(
                     "kiro_crew.github_runner.validate_provider_executable",
                     side_effect=ValueError("path must be canonical"),
                 ):
                with self.assertRaises(gh.GhSetupError) as ctx:
                    gh._gh_bin()
            self.assertEqual(ctx.exception.reason, "not_installed")
            self.assertIn("failed validation", str(ctx.exception))
        finally:
            github_runner.reset_cache()

    # The two tests below cover the CALL SITES, not the helper. Testing
    # `_raise_if_auth_failure` in isolation leaves the tree green even if a
    # refactor drops the call from `_run_gh_api` / `get_current_login` — an
    # unauthenticated `gh` would then surface as a generic 502 and the setup
    # guidance in the connect dialog would silently disappear.
    def _authfail(self):
        """A `gh` subprocess result that failed for lack of credentials."""
        return mock.Mock(
            returncode=1,
            stdout="",
            stderr="gh: To get started with GitHub CLI, please run: gh auth login\n",
        )

    def test_run_gh_api_classifies_auth_failure(self):
        with mock.patch.object(gh, "_gh_run", return_value=self._authfail()):
            with self.assertRaises(gh.GhSetupError) as ctx:
                gh._run_gh_api("repos/o/r/issues", ".[]")
        self.assertEqual(ctx.exception.reason, "not_authenticated")

    def test_get_current_login_classifies_auth_failure(self):
        with mock.patch.object(gh, "_gh_run", return_value=self._authfail()):
            with self.assertRaises(gh.GhSetupError) as ctx:
                gh.get_current_login()
        self.assertEqual(ctx.exception.reason, "not_authenticated")

    def test_non_auth_failures_stay_generic_gh_cli_errors(self):
        # The mirror assertion: a 404 must NOT be dressed up as a setup
        # problem, or the dialog would tell the user to log in when they
        # already are.
        notfound = mock.Mock(returncode=1, stdout="", stderr="HTTP 404: Not Found\n")
        for call in (
            lambda: gh._run_gh_api("repos/o/r/issues", ".[]"),
            gh.get_current_login,
        ):
            with mock.patch.object(gh, "_gh_run", return_value=notfound):
                with self.assertRaises(gh.GhCliError) as ctx:
                    call()
            self.assertNotIsInstance(ctx.exception, gh.GhSetupError)


class TestRecentReposRoute(unittest.IsolatedAsyncioTestCase):
    async def test_missing_gh_returns_setup_required_not_502(self):
        with mock.patch.object(
            routes.github_client, "get_current_login",
            side_effect=gh.GhSetupError("no gh found in <trusted dirs>", reason="not_installed"),
        ):
            resp = await routes._handle_recent_repos(_req())
        body = _body(resp)
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["setup_required"], "not_installed")
        self.assertEqual(body["repos"], [])
        # The diagnostic detail rides along for the collapsible "Details" block.
        self.assertIn("no gh found", body["error"])

    async def test_unauthenticated_gh_returns_setup_required(self):
        with mock.patch.object(routes.github_client, "get_current_login", return_value="octocat"), \
             mock.patch.object(
                 routes.github_client, "list_contributed_repos",
                 side_effect=gh.GhSetupError("not authenticated", reason="not_authenticated"),
             ):
            resp = await routes._handle_recent_repos(_req())
        body = _body(resp)
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["setup_required"], "not_authenticated")

    async def test_flags_already_connected_repos(self):
        live = [
            {"owner": "o", "repo": "already", "full_name": "o/already"},
            {"owner": "o", "repo": "fresh", "full_name": "o/fresh"},
        ]
        with mock.patch.object(routes.github_client, "get_current_login", return_value="octocat"), \
             mock.patch.object(routes.github_client, "list_contributed_repos", return_value=(live, False)), \
             mock.patch.object(routes.store, "list_connected_repos",
                               return_value=[{"owner": "o", "repo": "already"}]):
            resp = await routes._handle_recent_repos(_req())

        self.assertEqual(resp.status, 200)
        by_name = {r["full_name"]: r for r in _body(resp)["repos"]}
        self.assertTrue(by_name["o/already"]["connected"])
        self.assertFalse(by_name["o/fresh"]["connected"])

    async def test_connected_match_ignores_case(self):
        # GitHub names are case-preserving but not case-sensitive: the event
        # feed can spell a repo differently from config.json. A case-sensitive
        # compare would offer an already-connected repo as connectable and let
        # the user create a duplicate entry for the same repo.
        live = [{"owner": "Acme", "repo": "Widget", "full_name": "Acme/Widget"}]
        with mock.patch.object(routes.github_client, "get_current_login", return_value="octocat"), \
             mock.patch.object(routes.github_client, "list_contributed_repos", return_value=(live, False)), \
             mock.patch.object(routes.store, "list_connected_repos",
                               return_value=[{"owner": "acme", "repo": "widget"}]):
            resp = await routes._handle_recent_repos(_req())

        self.assertTrue(_body(resp)["repos"][0]["connected"])

    async def test_default_window_and_login_are_forwarded(self):
        with mock.patch.object(routes.github_client, "get_current_login", return_value="octocat"), \
             mock.patch.object(routes.github_client, "list_contributed_repos", return_value=([], False)) as lcr, \
             mock.patch.object(routes.store, "list_connected_repos", return_value=[]):
            await routes._handle_recent_repos(_req())
        lcr.assert_called_once_with("octocat", within_days=30)

    async def test_days_param_is_forwarded(self):
        with mock.patch.object(routes.github_client, "get_current_login", return_value="octocat"), \
             mock.patch.object(routes.github_client, "list_contributed_repos", return_value=([], False)) as lcr, \
             mock.patch.object(routes.store, "list_connected_repos", return_value=[]):
            await routes._handle_recent_repos(_req({"days": "7"}))
        lcr.assert_called_once_with("octocat", within_days=7)

    async def test_truncated_flag_is_surfaced(self):
        with mock.patch.object(routes.github_client, "get_current_login", return_value="octocat"), \
             mock.patch.object(routes.github_client, "list_contributed_repos",
                               return_value=([], True)), \
             mock.patch.object(routes.store, "list_connected_repos", return_value=[]):
            resp = await routes._handle_recent_repos(_req())
        self.assertTrue(_body(resp)["truncated"])

    async def test_non_integer_days_is_400(self):
        resp = await routes._handle_recent_repos(_req({"days": "soon"}))
        self.assertEqual(resp.status, 400)

    async def test_out_of_range_days_is_400_not_500(self):
        # An unbounded value reaches timedelta(days=...) and raises
        # OverflowError, which would surface as an internal server error.
        for bad in ("999999999999", "-1"):
            resp = await routes._handle_recent_repos(_req({"days": bad}))
            self.assertEqual(resp.status, 400, f"days={bad}")

    async def test_zero_days_is_allowed(self):
        # 0 is legal: it disables the window rather than being out of range.
        with mock.patch.object(routes.github_client, "get_current_login", return_value="octocat"), \
             mock.patch.object(routes.github_client, "list_contributed_repos", return_value=([], False)) as lcr, \
             mock.patch.object(routes.store, "list_connected_repos", return_value=[]):
            resp = await routes._handle_recent_repos(_req({"days": "0"}))
        self.assertEqual(resp.status, 200)
        lcr.assert_called_once_with("octocat", within_days=0)

    async def test_no_login_returns_empty_list_not_error(self):
        # The dialog stays usable (manual URL entry) when gh can't resolve a login.
        with mock.patch.object(routes.github_client, "get_current_login", return_value=None):
            resp = await routes._handle_recent_repos(_req())
        self.assertEqual(resp.status, 200)
        self.assertEqual(_body(resp)["repos"], [])

    async def test_gh_failure_is_502(self):
        with mock.patch.object(routes.github_client, "get_current_login", return_value="octocat"), \
             mock.patch.object(routes.github_client, "list_contributed_repos",
                               side_effect=gh.GhCliError("gh not authenticated")):
            resp = await routes._handle_recent_repos(_req())
        self.assertEqual(resp.status, 502)
        self.assertIn("gh not authenticated", _body(resp)["error"])

    async def test_login_lookup_failure_is_502(self):
        with mock.patch.object(routes.github_client, "get_current_login",
                               side_effect=gh.GhCliError("gh missing")):
            resp = await routes._handle_recent_repos(_req())
        self.assertEqual(resp.status, 502)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestConnectedRepoIdentity(unittest.TestCase):
    """GitHub names are case-preserving but not case-sensitive, so one repo must
    never be stored twice under two spellings — a duplicate entry carries its own
    independent caches and triage settings."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _repos(self):
        return store.read_config(self.root).get("repos", [])

    def test_reconnect_with_different_casing_does_not_duplicate(self):
        store.add_connected_repo("Acme", "Widget", root=self.root)
        store.add_connected_repo("acme", "widget", root=self.root)
        self.assertEqual(len(self._repos()), 1)
        # The FIRST spelling connected is the one kept — existing entries are
        # never rewritten out from under their caches.
        self.assertEqual((self._repos()[0]["owner"], self._repos()[0]["repo"]), ("Acme", "Widget"))

    def test_permissions_update_reaches_the_differently_cased_entry(self):
        store.add_connected_repo("Acme", "Widget", permissions={"push": False}, root=self.root)
        store.add_connected_repo("acme", "widget", permissions={"push": True}, root=self.root)
        self.assertEqual(len(self._repos()), 1)
        self.assertEqual(self._repos()[0]["permissions"], {"push": True})

    def test_set_repo_permissions_is_case_insensitive(self):
        store.add_connected_repo("Acme", "Widget", root=self.root)
        store.set_repo_permissions("ACME", "WIDGET", {"pull": True}, root=self.root)
        self.assertEqual(self._repos()[0]["permissions"], {"pull": True})

    def test_distinct_repos_are_still_distinct(self):
        store.add_connected_repo("o", "alpha", root=self.root)
        store.add_connected_repo("o", "beta", root=self.root)
        self.assertEqual(len(self._repos()), 2)
