"""The CI runs dashboard on GitLab: the recent-pipelines read and the route.

The read must hand the dashboard exactly the row GitHub's read does, in GitHub's
status vocabulary, because one component renders both forges. The route lists
GitHub and GitLab repos, never Azure DevOps, and keeps one repo's failure in that
repo's row.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, cast
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import (
    azure_client,
    github_client,
    gitlab_client,
)
from kiro_crew.apps.builtins.issue_radar.backend.errors import ProviderCliError, ProviderSetupError
from kiro_crew.apps.builtins.issue_radar.backend.http_routes import ci_runs

_HOST = "gitlab.example.com"


def _pipeline(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 501,
        "iid": 42,
        "project_id": 7,
        "sha": "a" * 40,
        "ref": "main",
        "status": "success",
        "source": "push",
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T00:03:00.000Z",
        "web_url": "https://gitlab.example.com/acme/widgets/-/pipelines/501",
    }
    row.update(over)
    return row


def _gitlab_rows(rows: list[dict[str, Any]]) -> tuple[list[dict], mock.MagicMock]:
    with mock.patch.object(gitlab_client, "_glab_api", return_value=rows) as api:
        out = gitlab_client.list_recent_workflow_runs("acme", "widgets", host=_HOST)
    return out, api


class TestRecentPipelines(unittest.TestCase):
    def test_row_has_exactly_the_github_row_keys(self) -> None:
        github_raw = [
            {
                "id": 1,
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "url": "https://github.com/o/r/actions/runs/1",
                "branch": "main",
                "pull_requests": [3],
                "created_at": "2026-01-01T00:00:00Z",
                "started_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:01:00Z",
            }
        ]
        with mock.patch.object(github_client, "_run_gh_api", return_value=github_raw):
            (gh_row,) = github_client.list_recent_workflow_runs("o", "r")
        (gl_row,) = _gitlab_rows([_pipeline()])[0]
        self.assertEqual(set(gl_row), set(gh_row))

    def test_reads_one_newest_first_page_of_the_project(self) -> None:
        _, api = _gitlab_rows([])
        self.assertEqual(
            api.call_args.args[0],
            "projects/acme%2Fwidgets/pipelines?per_page=20&order_by=id&sort=desc",
        )
        self.assertEqual(api.call_args.kwargs["host"], _HOST)
        self.assertIs(api.call_args.kwargs["paginate"], False)

    def test_status_and_conclusion_use_githubs_vocabulary(self) -> None:
        cases = {
            "failed": ("completed", "failure"),
            "canceled": ("completed", "cancelled"),
            "success": ("completed", "success"),
            "skipped": ("completed", "skipped"),
            "running": ("in_progress", None),
            "canceling": ("in_progress", None),
            "pending": ("queued", None),
            "manual": ("queued", None),
        }
        rows = [_pipeline(id=i + 1, status=s) for i, s in enumerate(cases)]
        out, _ = _gitlab_rows(rows)
        self.assertEqual({s: (r["status"], r["conclusion"]) for s, r in zip(cases, out)}, cases)

    def test_a_merge_request_pipeline_names_its_mr(self) -> None:
        out, _ = _gitlab_rows(
            [_pipeline(id=1, ref="refs/merge-requests/17/head"), _pipeline(id=2, ref="feature")]
        )
        self.assertEqual((out[0]["branch"], out[0]["pull_requests"]), (None, [17]))
        self.assertEqual((out[1]["branch"], out[1]["pull_requests"]), ("feature", []))

    def test_name_url_and_duration(self) -> None:
        out, _ = _gitlab_rows(
            [_pipeline(id=1), _pipeline(id=2, name="nightly", duration=95), _pipeline(id=3)]
        )
        self.assertEqual(out[0]["name"], "pipeline #42")
        self.assertEqual(out[0]["url"], "https://gitlab.example.com/acme/widgets/-/pipelines/501")
        self.assertEqual(out[0]["duration_sec"], 180)
        self.assertEqual((out[1]["name"], out[1]["duration_sec"]), ("nightly", 95))

    def test_fields_are_bounded_and_bad_rows_dropped(self) -> None:
        out, _ = _gitlab_rows(
            [
                _pipeline(name="n" * 5000, web_url="u" * 5000, ref="b" * 5000, extra="y"),
                {"id": True, "status": "failed"},
                {"id": -3, "status": "failed"},
                {"status": "failed"},
            ]
        )
        (row,) = out
        self.assertNotIn("extra", row)
        self.assertLessEqual(len(row["name"]), 300)
        self.assertLessEqual(len(row["url"]), 500)
        self.assertLessEqual(len(row["branch"]), 300)

    def test_an_unlisted_host_is_refused_before_glab_runs(self) -> None:
        with (
            mock.patch.object(gitlab_client, "allowed_hosts", return_value=frozenset()),
            mock.patch.object(gitlab_client.subprocess, "run") as run,
        ):
            with self.assertRaises(ProviderCliError):
                gitlab_client.list_recent_workflow_runs(
                    "acme", "widgets", host="gitlab.other.example"
                )
            with self.assertRaises(ProviderCliError):
                gitlab_client.list_recent_workflow_runs("acme", "widgets", host="")
        run.assert_not_called()

    def test_azure_refuses_rather_than_reporting_no_builds(self) -> None:
        with self.assertRaises(ProviderCliError):
            azure_client.list_recent_workflow_runs("org/proj", "repo", host="dev.azure.com")


class TestCiRunsRouteByForge(unittest.IsolatedAsyncioTestCase):
    async def _call(
        self, repos: list[dict[str, Any]], gh_runs: Any, gl_runs: Any
    ) -> tuple[dict[str, Any], mock.MagicMock, mock.MagicMock, web.Response]:
        with (
            mock.patch.object(ci_runs.store, "list_connected_repos", return_value=repos),
            mock.patch.object(ci_runs.store, "read_repo_settings", return_value={}),
            mock.patch.object(
                github_client, "list_recent_workflow_runs", side_effect=gh_runs
            ) as gh,
            mock.patch.object(
                gitlab_client, "list_recent_workflow_runs", side_effect=gl_runs
            ) as gl,
            mock.patch.object(azure_client, "list_recent_workflow_runs") as az,
        ):
            resp = await ci_runs._handle_ci_runs(cast(web.Request, mock.Mock()))
        az.assert_not_called()
        return json.loads(cast(bytes, resp.body)), gh, gl, resp

    async def test_lists_github_and_gitlab_repos_and_never_azure(self) -> None:
        repos = [
            {"owner": "o", "repo": "r"},
            {"owner": "acme", "repo": "widgets", "provider": "gitlab", "host": _HOST},
            {"owner": "org/proj", "repo": "az", "provider": "azure", "host": "dev.azure.com"},
        ]
        body, gh, gl, _ = await self._call(
            repos,
            lambda owner, repo: [{"id": 1}],
            lambda owner, repo, *, host: [{"id": 2}],
        )
        rows = {r["repo"]: r for r in body["repos"]}
        self.assertEqual(set(rows), {"r", "widgets"})
        self.assertEqual(rows["widgets"]["provider"], "gitlab")
        self.assertEqual(rows["widgets"]["host"], _HOST)
        self.assertEqual(rows["widgets"]["runs"], [{"id": 2}])
        self.assertEqual(gl.call_args.kwargs, {"host": _HOST})
        self.assertEqual(gh.call_args.kwargs, {})

    async def test_a_gitlab_failure_stays_in_its_own_row(self) -> None:
        repos = [
            {"owner": "o", "repo": "r"},
            {"owner": "acme", "repo": "widgets", "provider": "gitlab", "host": _HOST},
            {"owner": "acme", "repo": "signed-out", "provider": "gitlab", "host": _HOST},
        ]

        def gl_runs(owner: str, repo: str, *, host: str) -> list[dict]:
            if repo == "widgets":
                raise ProviderCliError("glab: HTTP 404 /home/someone/secret")
            raise ProviderSetupError("glab is not signed in", reason="not_authenticated")

        body, _, _, resp = await self._call(repos, lambda owner, repo: [{"id": 1}], gl_runs)
        rows = {r["repo"]: r for r in body["repos"]}
        self.assertEqual(rows["r"]["runs"], [{"id": 1}])
        self.assertIsNone(rows["r"]["error"])
        self.assertEqual(
            (rows["widgets"]["error"], rows["widgets"]["runs"]), ("provider_error", [])
        )
        self.assertEqual(rows["signed-out"]["error"], "glab_setup")
        self.assertNotIn("secret", resp.text or "")


if __name__ == "__main__":
    unittest.main()
