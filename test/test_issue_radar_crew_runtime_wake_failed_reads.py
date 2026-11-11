"""Issue Radar crews: a failed forge read in the new-work and mention wake sweep.

A sweep that cannot read the repository comments or the open issues knows nothing
new. It must wake nobody and leave ``crew-wake-checks.json`` exactly as it was, so
the cursor, the creation floor and the seen marks never move past work nobody read,
and the next sweep that can read finds the mention or the issue. The sweep is driven
the same way as in ``test_issue_radar_crew_runtime_wake.py``: a fake forge client, a
fake gateway state, a real crew store.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from issue_radar_crew_runtime_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    OWNER,
    REPO,
    _app,
    _crew,
    _FakeState,
    _isolated_crew_log,
    _private_sel_root_per_test,
)
from test_issue_radar_crew_runtime_wake import (  # noqa: F401 -- the autouse fixture registers by import
    _KEY,
    _LATER,
    _gitlab_cutoff_far_ahead,
    _WakeClient,
)

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.apps.builtins.issue_radar.backend import provider

#: A mention cursor and the floor of an unfinished earlier scan held behind it, both
#: before the ask the tests post at ``_LATER``.
_CURSOR = "2098-01-01T00:00:00Z"
_FLOOR = "2097-06-01T00:00:00Z"


def _rate_limited(*_args, **_kwargs):
    raise RuntimeError("API rate limit exceeded")


class TestFailedReadsInTheWakeSweep(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        cs.write_settings(
            OWNER,
            REPO,
            {"crew_answer_mentions": True, "crew_wake_on_new_work": True},
            self.root,
        )
        self.checks_file = cr.wake_checks_path(OWNER, REPO, self.root)

    async def _sweep(self, client):
        wake = mock.AsyncMock(return_value=True)
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(cr.provider, "client_for", return_value=client),
            mock.patch.object(cr, "wake_crew", new=wake),
        ):
            woken = await cr.sweep_repo(_app(_FakeState()), _KEY, self.root)
        return woken, wake

    @staticmethod
    def _reasons(wake) -> list[str]:
        return [call.args[4] for call in wake.await_args_list]

    async def test_a_failed_comment_read_moves_no_mention_state_and_wakes_nobody(self):
        crew = _crew(self.root)
        client = _WakeClient()
        client.list_thread_comments = mock.Mock(side_effect=AssertionError("GitLab path"))
        client.list_repo_comments_since = mock.Mock(return_value=[])
        await self._sweep(client)  # first sight: the cursor is seeded
        checks = cr.read_wake_checks(OWNER, REPO, self.root)
        checks["mentions"].update(cursor=_CURSOR, floor=_FLOOR, seen=["c0"], at_cursor=[])
        cr.write_wake_checks(OWNER, REPO, checks, self.root)
        before = self.checks_file.read_bytes()
        ask = {
            "id": "c1",
            "number": 5,
            "is_pr": False,
            "actor": "alice",
            "created_at": _LATER,
            "updated_at": _LATER,
            "body": "@crew-bot can you take this?",
        }

        client.list_repo_comments_since.side_effect = _rate_limited
        woken, wake = await self._sweep(client)

        wake.assert_not_awaited()
        self.assertEqual(woken, {})
        self.assertEqual(self.checks_file.read_bytes(), before)

        def page_of(o, r, since, page=1, **kw):
            return [dict(ask)] if page == 1 and ask["updated_at"][:19] > since[:19] else []

        client.list_repo_comments_since.side_effect = page_of
        woken, wake = await self._sweep(client)

        self.assertEqual(self._reasons(wake), ["#5 asked c1"])
        self.assertEqual(woken, {crew["id"]: [cr.SIG_ASKED]})

    async def test_a_failed_open_issues_read_moves_no_mark_and_wakes_nobody(self):
        scoped = _crew(self.root, labels=["bug"])
        client = _WakeClient()
        client.add_issue(4)
        client.add_issue(5, ["bug"])
        await self._sweep(client)  # first sight: the backlog is marked, nobody woken
        # A crew first seen while the read fails has no mark yet: a sweep that took
        # the failure for an empty page would seed it below the backlog.
        _crew(self.root, name="Bode")
        client.add_issue(6, ["bug"])
        before = self.checks_file.read_bytes()

        client.list_open_issues_first_page = mock.Mock(side_effect=_rate_limited)
        woken, wake = await self._sweep(client)

        wake.assert_not_awaited()
        self.assertEqual(woken, {})
        self.assertEqual(self.checks_file.read_bytes(), before)

        del client.list_open_issues_first_page  # the read works again
        woken, wake = await self._sweep(client)

        # The scoped crew finds the new issue; the new crew seeds at the backlog's top.
        self.assertEqual(self._reasons(wake), ["#6 scope-label"])
        self.assertEqual(woken, {scoped["id"]: [cr.SIG_SCOPE_LABEL]})
