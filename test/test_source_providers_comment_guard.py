"""The pull-request comment entry point must refuse an issue ref."""
import unittest
from unittest import mock

from kiro_crew.dashboard.handlers import source_providers as sp


class TestCommentOnPullRequestRefusesAnIssueUrl(unittest.IsolatedAsyncioTestCase):
    """The PR comment endpoint posts to /issues/{number}/comments.

    On GitHub, issues and pull requests share one number counter, so an issue URL
    reaching here publishes a comment on an unrelated issue that happens to carry
    the pull request's number. The sibling thread mutations already refuse an
    issue ref; this entry point did not.
    """

    async def test_an_issue_url_is_refused_before_anything_is_posted(self):
        calls: list[tuple] = []

        async def _spy(*args, **kwargs):
            calls.append(args)
            return {}

        with mock.patch.object(sp, "_run_json", _spy), \
                mock.patch.object(sp, "ensure_gitlab_hosts_loaded",
                                  new=mock.AsyncMock(return_value=None)):
            with self.assertRaises(ValueError):
                await sp.comment_on_pull_request(
                    "https://github.com/o/r/issues/58", "hello")

        self.assertEqual(calls, [], "a comment was posted for an issue URL")

    async def test_a_pull_request_url_still_posts(self):
        calls: list[tuple] = []

        async def _spy(*args, **kwargs):
            calls.append(args)
            return {}

        with mock.patch.object(sp, "_run_json", _spy), \
                mock.patch.object(sp, "ensure_gitlab_hosts_loaded",
                                  new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(sp, "_invalidate_pull_request_cache",
                                  new=mock.AsyncMock(return_value=None)):
            await sp.comment_on_pull_request(
                "https://github.com/o/r/pull/58", "hello")

        self.assertTrue(calls, "the pull-request path stopped posting")
        self.assertIn("repos/o/r/issues/58/comments", " ".join(calls[0]))
