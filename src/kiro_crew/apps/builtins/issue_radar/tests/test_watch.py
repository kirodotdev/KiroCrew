"""Tests for the Issue Radar in-process new-issue watcher (backend/watch.py).

Locks in the watcher's contract without touching the network or the real app
data dir: the ``gh`` fetch, the per-repo settings, and the watch-state
read/write are all patched, and a fake dashboard state records notifications.

Covered:
  * first observation seeds the high-water mark WITHOUT notifying (no backlog
    announcement);
  * an issue whose number exceeds the mark notifies and advances the mark;
  * nothing new → no write, no notify;
  * single- vs multi-issue notification body;
  * the sweep is silent when the app is disabled and skips repos that have not
    opted in.
"""
import unittest
from typing import cast
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import provider, store, watch

# The watcher is provider-dispatched, so every poll needs an explicit repo
# key. GitHub is used here so these tests keep asserting the ORIGINAL
# behaviour (legacy storage layout, github.com links) unchanged.
_KEY = provider.key_from_parts("o", "r")


class _FakeState:
    """Minimal stand-in for DashboardState — records notify() calls."""

    def __init__(self):
        self.notes: list[dict] = []

    def notify(self, kind: str, title: str, body: str, *, meta: dict | None = None) -> None:
        self.notes.append({"kind": kind, "title": title, "body": body, "meta": meta})


def _fake_app(state: _FakeState) -> web.Application:
    """A dict is a sufficient stand-in for the app here (the watcher only reads
    ``app['state']``); cast so mypy accepts it where an Application is expected."""
    return cast(web.Application, {"state": state})


class TestPollRepo(unittest.IsolatedAsyncioTestCase):
    async def test_seed_first_observation_no_notify(self):
        state = _FakeState()
        recent = [
            {"number": 5, "title": "e", "url": "u5"},
            {"number": 3, "title": "c", "url": "u3"},
        ]
        with mock.patch.object(provider.client_for(_KEY), "list_recent_open_issues", return_value=recent), \
             mock.patch.object(watch.store, "read_watch_state", return_value={}), \
             mock.patch.object(watch.store, "write_watch_state") as wws:
            await watch._poll_repo(_fake_app(state), _KEY)
        wws.assert_called_once_with(
                "o", "r", 5, root=store.provider_root(root=None)
            )  # seeded to current max
        self.assertEqual(state.notes, [])          # but nothing announced

    async def test_notify_on_new_issues(self):
        state = _FakeState()
        recent = [
            {"number": 102, "title": "new B", "url": "u102"},
            {"number": 101, "title": "new A", "url": "u101"},
            {"number": 100, "title": "old", "url": "u100"},
        ]
        with mock.patch.object(provider.client_for(_KEY), "list_recent_open_issues", return_value=recent), \
             mock.patch.object(watch.store, "read_watch_state", return_value={"last_seen_number": 100}), \
             mock.patch.object(watch.store, "write_watch_state") as wws:
            await watch._poll_repo(_fake_app(state), _KEY)
        wws.assert_called_once_with(
                "o", "r", 102, root=store.provider_root(root=None)
            )  # mark advanced before notify
        self.assertEqual(len(state.notes), 1)
        note = state.notes[0]
        self.assertEqual(note["meta"]["count"], 2)
        self.assertIn("2 new issues", note["body"])
        self.assertIn("#101", note["body"])
        self.assertIn("#102", note["body"])

    async def test_no_notify_when_nothing_new(self):
        state = _FakeState()
        recent = [{"number": 100, "title": "x", "url": "u"}]
        with mock.patch.object(provider.client_for(_KEY), "list_recent_open_issues", return_value=recent), \
             mock.patch.object(watch.store, "read_watch_state", return_value={"last_seen_number": 100}), \
             mock.patch.object(watch.store, "write_watch_state") as wws:
            await watch._poll_repo(_fake_app(state), _KEY)
        wws.assert_not_called()
        self.assertEqual(state.notes, [])

    async def test_single_new_issue_body(self):
        state = _FakeState()
        recent = [{"number": 7, "title": "Solo", "url": "u7"}]
        with mock.patch.object(provider.client_for(_KEY), "list_recent_open_issues", return_value=recent), \
             mock.patch.object(watch.store, "read_watch_state", return_value={"last_seen_number": 6}), \
             mock.patch.object(watch.store, "write_watch_state"):
            await watch._poll_repo(_fake_app(state), _KEY)
        self.assertEqual(len(state.notes), 1)
        self.assertIn("New issue #7", state.notes[0]["body"])
        self.assertEqual(state.notes[0]["meta"]["url"], "u7")

    async def test_empty_fetch_is_noop(self):
        state = _FakeState()
        with mock.patch.object(provider.client_for(_KEY), "list_recent_open_issues", return_value=[]), \
             mock.patch.object(watch.store, "read_watch_state", return_value={}) as rws, \
             mock.patch.object(watch.store, "write_watch_state") as wws:
            await watch._poll_repo(_fake_app(state), _KEY)
        rws.assert_not_called()
        wws.assert_not_called()
        self.assertEqual(state.notes, [])


class TestPollSweep(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_app_is_silent(self):
        with mock.patch.object(watch, "is_app_enabled", return_value=False), \
             mock.patch.object(watch.store, "list_connected_repos") as lcr:
            await watch._poll_once(_fake_app(_FakeState()))
        lcr.assert_not_called()

    async def test_optout_repo_not_polled(self):
        with mock.patch.object(watch, "is_app_enabled", return_value=True), \
             mock.patch.object(watch.store, "list_connected_repos", return_value=[{"owner": "o", "repo": "r"}]), \
             mock.patch.object(watch.store, "read_repo_settings", return_value={"notify_on_new_issue": False}), \
             mock.patch.object(provider.client_for(_KEY), "list_recent_open_issues") as lri:
            await watch._poll_once(_fake_app(_FakeState()))
        lri.assert_not_called()

    async def test_optin_repo_is_polled(self):
        with mock.patch.object(watch, "is_app_enabled", return_value=True), \
             mock.patch.object(watch.store, "list_connected_repos", return_value=[{"owner": "o", "repo": "r"}]), \
             mock.patch.object(watch.store, "read_repo_settings", return_value={"notify_on_new_issue": True}), \
             mock.patch.object(provider.client_for(_KEY), "list_recent_open_issues", return_value=[]) as lri:
            await watch._poll_once(_fake_app(_FakeState()))
        lri.assert_called_once()


if __name__ == "__main__":
    unittest.main()
