"""Tests for Issue Radar's own-PR CI-failure alerts (backend/ci_watch.py).

Nothing here reaches the network: ``gh`` is replaced at the client's two
chokepoints (``_gh_run`` for the GraphQL search, ``_run_gh_api`` for the runs
read) or at the client functions, and the per-repo record lives in memory.

Covered:
  * with the setting off the watcher makes no CI call and sends no notification;
  * a failed run on the viewer's own PR raises exactly one notification, and a
    teammate's PR in the same repo never does;
  * a re-run attempt that fails notifies again, a repeated read does not;
  * success, in-flight runs and the first (seeding) read stay silent;
  * a ``gh`` error skips the repo and keeps its record;
  * the channel's mute and priority settings apply to the note;
  * the setting defaults off and round-trips through the store.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import (
    ci_watch,
    github_client,
    provider,
    store,
    watch,
)
from kiro_crew.notifications.bus import NotificationBus
from kiro_crew.notifications.settings import ChannelSettings

_KEY = provider.key_from_parts("o", "r")
_CHANNEL = "issue-radar.ci-failure"
_APP_JSON = Path(ci_watch.__file__).resolve().parents[1] / "app.json"


class _State:
    """A gateway-state stand-in with a REAL bus and REAL channel settings.

    The sink applies the channel settings exactly as ``DashboardState`` does, so
    a mute or a priority override set below reaches the delivered note.
    """

    def __init__(self, channel_settings: ChannelSettings) -> None:
        self.notes: list[dict[str, Any]] = []
        self.legacy: list[tuple[str, str]] = []
        self.notification_channel_settings = channel_settings
        self.notification_bus = NotificationBus(self._sink)
        # When set, each delivered note leaves a resolved persistence future with
        # this outcome, as the dashboard's notification coordinator does.
        self.persist_ok: bool | None = None
        self.last_notification_persist: asyncio.Future[bool] | None = None

    def _sink(self, note: dict[str, Any]) -> None:
        self.notification_channel_settings.apply(note)
        self.notes.append(note)
        self.last_notification_persist = None
        if self.persist_ok is not None:
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            fut.set_result(self.persist_ok)
            self.last_notification_persist = fut

    def notify(self, kind: str, title: str, body: str, *, meta: dict | None = None) -> None:
        self.legacy.append((kind, title))


def _app(state: _State) -> web.Application:
    return cast(web.Application, {"state": state})


def _graphql_stdout(viewer: str, nodes: list[Any]) -> str:
    return json.dumps({"viewer": {"login": viewer}, "search": {"nodes": nodes}})


def _pr_node(number: int, author: str, sha: str, repo: str = "o/r") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/{repo}/pull/{number}",
        "headRefOid": sha,
        "author": {"login": author},
        "repository": {"nameWithOwner": repo},
    }


def _run(run_id: int, conclusion: str | None, *, attempt: int = 1, status: str = "completed"):
    return {
        "id": run_id,
        "name": f"wf{run_id}",
        "status": status,
        "conclusion": conclusion,
        "url": f"https://github.com/o/r/actions/runs/{run_id}",
        "attempt": attempt,
    }


class _Base(unittest.IsolatedAsyncioTestCase):
    """Shared fixtures: temp notification settings, in-memory CI record, gh fakes."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch("kiro_crew.notifications.settings.config_dir", lambda: Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.channel_settings = ChannelSettings()
        self.state = _State(self.channel_settings)
        ci_watch._settled.clear()
        self.addCleanup(ci_watch._settled.clear)
        ci_watch._head_failed.clear()
        self.addCleanup(ci_watch._head_failed.clear)
        # The in-memory per-repo record, seeded unless a test says otherwise.
        self.record: dict[str, Any] = {"seeded": True, "notified": {}}
        self.writes: list[dict[str, int]] = []
        for name, fake in (
            ("read_ci_watch_record", self._read_record),
            ("write_ci_watch_state", self._write_record),
        ):
            p = mock.patch.object(ci_watch.store, name, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(
            ci_watch.notifications_push,
            "_resolve_app_channels",
            return_value={"ci-failure": "default"},
        )
        p.start()
        self.addCleanup(p.stop)
        # Search results and runs per head sha, consumed by the gh fakes.
        self.viewer = "me"
        self.nodes: list[dict[str, Any]] = []
        self.runs: dict[str, list[dict[str, Any]]] = {}
        self.runs_reads: list[str] = []

    def _read_record(self, owner: str, repo: str, root: Any = None) -> tuple[dict[str, Any], int]:
        record = {"seeded": self.record["seeded"], "notified": dict(self.record["notified"])}
        return record, ci_watch.store.ci_watch_generation(owner, repo, root)

    def _write_record(
        self, owner: str, repo: str, notified: dict[str, int], root: Any = None, **_kw: Any
    ) -> bool:
        self.record = {"seeded": True, "notified": dict(notified)}
        self.writes.append(dict(notified))
        return True

    def _gh_run(self, argv: list[str], *, timeout: float, input_text: str | None = None):
        assert argv[:3] == ["gh", "api", "graphql"], argv
        return subprocess.CompletedProcess(
            argv, 0, stdout=_graphql_stdout(self.viewer, self.nodes), stderr=""
        )

    def _run_gh_api(self, path: str, jq: str, **_kw: Any) -> list[dict]:
        sha = path.split("head_sha=")[1].split("&")[0]
        self.runs_reads.append(sha)
        return list(self.runs.get(sha, []))

    async def _cycle(self) -> None:
        with (
            mock.patch.object(github_client, "_gh_run", side_effect=self._gh_run),
            mock.patch.object(github_client, "_run_gh_api", side_effect=self._run_gh_api),
        ):
            await ci_watch.poll_ci_failures(_app(self.state), [_KEY])


class TestSettingOff(_Base):
    async def _sweep(self, settings: dict[str, Any]) -> mock.MagicMock:
        with (
            mock.patch.object(watch, "is_app_enabled", return_value=True),
            mock.patch.object(
                watch.store, "list_connected_repos", return_value=[{"owner": "o", "repo": "r"}]
            ),
            mock.patch.object(watch.store, "read_repo_settings", return_value=settings),
            mock.patch.object(watch.crew_runtime, "sweep_repo", new=mock.AsyncMock()),
            mock.patch.object(github_client, "_gh_run") as gh_run,
            mock.patch.object(github_client, "_run_gh_api") as gh_api,
        ):
            await watch._poll_once(_app(self.state))
        self.assertEqual(gh_api.call_count, 0)
        return gh_run

    async def test_default_settings_make_no_ci_call(self) -> None:
        gh_run = await self._sweep(dict(store.DEFAULT_REPO_SETTINGS))
        gh_run.assert_not_called()
        self.assertEqual(self.state.notes, [])
        self.assertEqual(self.writes, [])

    async def test_setting_off_makes_no_ci_call_with_new_issue_alerts_on(self) -> None:
        settings = {**store.DEFAULT_REPO_SETTINGS, "notify_on_new_issue": True}
        with mock.patch.object(
            provider.client_for(_KEY), "list_recent_open_issues", return_value=[]
        ) as recent:
            gh_run = await self._sweep(settings)
        recent.assert_called_once()  # the new-issue pass is untouched
        gh_run.assert_not_called()
        self.assertEqual(self.state.notes, [])

    async def test_setting_on_runs_the_ci_pass(self) -> None:
        settings = {**store.DEFAULT_REPO_SETTINGS, "notify_on_own_pr_ci_failure": True}
        with mock.patch.object(watch.ci_watch, "poll_ci_failures", new=mock.AsyncMock()) as poll:
            await self._sweep(settings)
        poll.assert_awaited_once()
        self.assertEqual(poll.await_args_list[0].args[1], [_KEY])

    async def test_non_github_repo_is_never_ci_polled(self) -> None:
        gl = provider.key_from_parts("g", "p", "gitlab", "gitlab.com")
        self.assertFalse(ci_watch.opted_in({"notify_on_own_pr_ci_failure": True}, gl))
        with mock.patch.object(github_client, "_gh_run") as gh_run:
            await ci_watch.poll_ci_failures(_app(self.state), [gl])
        gh_run.assert_not_called()


class TestFailureAlerts(_Base):
    async def test_failed_run_on_own_pr_notifies_once(self) -> None:
        self.nodes = [_pr_node(7, "me", "a" * 40)]
        self.runs = {"a" * 40: [_run(11, "failure"), _run(12, "success")]}
        await self._cycle()
        self.assertEqual(len(self.state.notes), 1)
        note = self.state.notes[0]
        self.assertEqual(note["channel"], _CHANNEL)
        self.assertEqual(note["source"], "app:issue-radar")
        self.assertEqual(note["priority"], "default")
        self.assertIn("o/r#7", note["title"])
        self.assertIn("wf11", note["body"])
        self.assertNotIn("wf12", note["body"])
        self.assertEqual(note["url"], "/issue-radar")
        self.assertEqual(note["run_url"], "https://github.com/o/r/actions/runs/11")
        self.assertEqual(self.state.legacy, [])
        # The same failure read again on the next cycle stays quiet.
        ci_watch._settled.clear()
        await self._cycle()
        self.assertEqual(len(self.state.notes), 1)

    async def test_teammates_pr_never_notifies(self) -> None:
        self.nodes = [_pr_node(8, "teammate", "b" * 40)]
        self.runs = {"b" * 40: [_run(21, "failure")]}
        await self._cycle()
        self.assertEqual(self.state.notes, [])
        self.assertEqual(self.runs_reads, [])  # its runs are never even read

    async def test_own_pr_alerts_beside_a_teammates_failing_pr(self) -> None:
        self.nodes = [_pr_node(8, "teammate", "b" * 40), _pr_node(9, "me", "c" * 40)]
        self.runs = {"b" * 40: [_run(21, "failure")], "c" * 40: [_run(31, "timed_out")]}
        await self._cycle()
        self.assertEqual(len(self.state.notes), 1)
        self.assertIn("o/r#9", self.state.notes[0]["title"])

    async def test_failed_rerun_attempt_notifies_again(self) -> None:
        sha = "d" * 40
        self.nodes = [_pr_node(5, "me", sha)]
        self.runs = {sha: [_run(41, "failure", attempt=1)]}
        await self._cycle()
        self.runs = {sha: [_run(41, "failure", attempt=2)]}
        ci_watch._settled.clear()
        await self._cycle()
        self.assertEqual(len(self.state.notes), 2)
        self.assertEqual(self.record["notified"], {"41": 2})

    async def test_success_and_in_flight_runs_stay_quiet(self) -> None:
        sha = "e" * 40
        self.nodes = [_pr_node(6, "me", sha)]
        self.runs = {
            sha: [_run(51, "success"), _run(52, None, status="in_progress"), _run(53, "cancelled")]
        }
        await self._cycle()
        self.assertEqual(self.state.notes, [])
        self.assertEqual(self.writes, [])

    async def test_first_read_seeds_without_notifying(self) -> None:
        self.record = {"seeded": False, "notified": {}}
        sha = "f" * 40
        self.nodes = [_pr_node(3, "me", sha)]
        self.runs = {sha: [_run(61, "failure")]}
        await self._cycle()
        self.assertEqual(self.state.notes, [])
        self.assertEqual(self.record, {"seeded": True, "notified": {"61": 1}})

    async def test_gh_error_on_runs_keeps_the_record(self) -> None:
        self.nodes = [_pr_node(4, "me", "1" * 40)]
        with (
            mock.patch.object(github_client, "_gh_run", side_effect=self._gh_run),
            mock.patch.object(
                github_client, "_run_gh_api", side_effect=github_client.GhCliError("net")
            ),
        ):
            await ci_watch.poll_ci_failures(_app(self.state), [_KEY])
        self.assertEqual(self.writes, [])
        self.assertEqual(self.state.notes, [])

    async def test_cut_short_unseeded_cycle_still_seeds_every_head(self) -> None:
        sha_a, sha_b = "a" * 40, "b" * 40
        self.record = {"seeded": False, "notified": {}}
        self.nodes = [_pr_node(1, "me", sha_a), _pr_node(2, "me", sha_b)]
        self.runs = {sha_a: [_run(21, "failure")], sha_b: [_run(22, "success")]}
        real = self._run_gh_api

        def flaky(path: str, jq: str, **kw: Any) -> list[dict]:
            if sha_b in path:
                raise github_client.GhCliError("net")
            return real(path, jq, **kw)

        with (
            mock.patch.object(github_client, "_gh_run", side_effect=self._gh_run),
            mock.patch.object(github_client, "_run_gh_api", side_effect=flaky),
        ):
            await ci_watch.poll_ci_failures(_app(self.state), [_KEY])
        self.assertEqual(self.writes, [])
        await self._cycle()  # head A settled last cycle, but the repo is not seeded
        self.assertEqual(self.record, {"seeded": True, "notified": {"21": 1}})
        ci_watch._settled.clear()
        await self._cycle()
        self.assertEqual(self.state.notes, [])

    async def test_channel_lookup_runs_off_the_event_loop(self) -> None:
        loop_thread = threading.get_ident()
        threads: list[int] = []

        def resolve(app_name: str) -> dict[str, str]:
            threads.append(threading.get_ident())
            return {"ci-failure": "default"}

        self.nodes = [_pr_node(7, "me", "a" * 40)]
        self.runs = {"a" * 40: [_run(11, "failure")]}
        with mock.patch.object(
            ci_watch.notifications_push, "_resolve_app_channels", side_effect=resolve
        ):
            await self._cycle()
        self.assertEqual(len(self.state.notes), 1)
        self.assertTrue(threads)
        self.assertNotIn(loop_thread, threads)

    async def test_reset_during_a_pass_stops_its_notes_and_writes(self) -> None:
        self.nodes = [_pr_node(7, "me", "a" * 40)]
        self.runs = {"a" * 40: [_run(11, "failure")]}
        generation = {"value": 0}
        real = self._run_gh_api

        def runs_then_reset(path: str, jq: str, **kw: Any) -> list[dict]:
            rows = real(path, jq, **kw)
            generation["value"] += 1  # the setting turned off and on mid-read
            return rows

        with (
            mock.patch.object(
                ci_watch.store, "ci_watch_generation", side_effect=lambda *a: generation["value"]
            ),
            mock.patch.object(github_client, "_gh_run", side_effect=self._gh_run),
            mock.patch.object(github_client, "_run_gh_api", side_effect=runs_then_reset),
        ):
            await ci_watch.poll_ci_failures(_app(self.state), [_KEY])
        self.assertEqual(self.state.notes, [])
        self.assertEqual(self.writes, [])

    async def test_truncated_search_skips_the_repo(self) -> None:
        self.record = {"seeded": False, "notified": {}}
        found = {"pulls": [], "truncated": [("o", "r")]}
        with (
            mock.patch.object(github_client, "search_viewer_open_pull_heads", return_value=found),
            mock.patch.object(github_client, "_run_gh_api", side_effect=self._run_gh_api),
            self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as logs,
        ):
            await ci_watch.poll_ci_failures(_app(self.state), [_KEY])
        self.assertEqual(self.runs_reads, [])
        self.assertEqual(self.writes, [])
        self.assertIn("o/r", "\n".join(logs.output))

    async def test_record_keeps_only_failures_on_open_heads(self) -> None:
        self.record = {"seeded": True, "notified": {"99": 1, "11": 1}}
        self.nodes = [_pr_node(7, "me", "a" * 40)]
        self.runs = {"a" * 40: [_run(11, "failure"), _run(12, "failure")]}
        await self._cycle()
        self.assertEqual(len(self.state.notes), 1)
        self.assertEqual(self.record["notified"], {"11": 1, "12": 1})

    async def test_more_live_failures_than_the_cap_skips_the_repo(self) -> None:
        self.record = {"seeded": False, "notified": {}}
        self.nodes = [_pr_node(7, "me", "a" * 40)]
        self.runs = {"a" * 40: [_run(i, "failure") for i in (1, 2, 3)]}
        with (
            mock.patch.object(ci_watch.store, "CI_WATCH_MAX_RUNS", 2),
            self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as logs,
        ):
            await self._cycle()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.state.notes, [])
        self.assertIn("exceed", "\n".join(logs.output))

    async def test_search_error_makes_no_runs_read(self) -> None:
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 502")
        with (
            mock.patch.object(github_client, "_gh_run", return_value=failed),
            mock.patch.object(github_client, "_run_gh_api") as gh_api,
        ):
            await ci_watch.poll_ci_failures(_app(self.state), [_KEY])
        gh_api.assert_not_called()
        self.assertEqual(self.writes, [])

    async def test_settled_head_is_not_reread_every_cycle(self) -> None:
        sha = "2" * 40
        self.nodes = [_pr_node(2, "me", sha)]
        self.runs = {sha: [_run(71, "success")]}
        await self._cycle()
        await self._cycle()
        self.assertEqual(self.runs_reads, [sha])


class TestChannelControls(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.nodes = [_pr_node(7, "me", "a" * 40)]
        self.runs = {"a" * 40: [_run(11, "failure")]}

    async def test_muted_channel_delivers_silenced_and_passive(self) -> None:
        self.channel_settings.update(_CHANNEL, muted=True)
        await self._cycle()
        self.assertEqual(len(self.state.notes), 1)
        self.assertTrue(self.state.notes[0].get("silenced"))
        self.assertEqual(self.state.notes[0]["priority"], "passive")

    async def test_priority_override_applies(self) -> None:
        self.channel_settings.update(_CHANNEL, priority="critical")
        await self._cycle()
        self.assertEqual(self.state.notes[0]["priority"], "critical")
        self.assertFalse(self.state.notes[0].get("silenced", False))

    async def test_rate_limiter_is_shared(self) -> None:
        limiter = mock.Mock()
        limiter.allow.return_value = False
        self.state.notification_rate_limiter = limiter  # type: ignore[attr-defined]
        await self._cycle()
        limiter.allow.assert_called_once_with("issue-radar")
        self.assertEqual(self.state.notes, [])

    async def test_rate_limited_note_is_retried_next_cycle(self) -> None:
        limiter = mock.Mock()
        limiter.allow.return_value = False
        self.state.notification_rate_limiter = limiter  # type: ignore[attr-defined]
        await self._cycle()
        self.assertEqual(self.state.notes, [])
        self.assertEqual(self.record["notified"], {})
        limiter.allow.return_value = True
        await self._cycle()
        self.assertEqual(len(self.state.notes), 1)
        self.assertEqual(self.record["notified"], {"11": 1})

    async def test_push_error_is_retried_next_cycle(self) -> None:
        with mock.patch.object(self.state.notification_bus, "push", side_effect=RuntimeError):
            await self._cycle()
        self.assertEqual(self.record["notified"], {})
        await self._cycle()
        self.assertEqual(len(self.state.notes), 1)

    async def test_unpersisted_note_is_retried_next_cycle(self) -> None:
        self.state.persist_ok = False
        await self._cycle()
        self.assertEqual(len(self.state.notes), 1)
        self.assertEqual(self.record["notified"], {})
        self.state.persist_ok = True
        await self._cycle()
        self.assertEqual(len(self.state.notes), 2)
        self.assertEqual(self.record["notified"], {"11": 1})

    async def test_unavailable_channel_is_recorded_not_retried(self) -> None:
        with mock.patch.object(
            ci_watch.notifications_push, "_resolve_app_channels", return_value=None
        ):
            await self._cycle()
        self.assertEqual(self.state.notes, [])
        self.assertEqual(self.record["notified"], {"11": 1})

    def test_manifest_declares_the_channel(self) -> None:
        manifest = json.loads(_APP_JSON.read_text(encoding="utf-8"))
        ids = [c["id"] for c in manifest["notifications"]["channels"]]
        self.assertIn(ci_watch.CHANNEL_CI_FAILURE, ids)


class TestSettingStore(unittest.TestCase):
    def test_defaults_off_and_round_trips(self) -> None:
        self.assertIs(store.DEFAULT_REPO_SETTINGS["notify_on_own_pr_ci_failure"], False)
        self.assertIs(store._normalize_settings({})["notify_on_own_pr_ci_failure"], False)
        for raw in ("false", "true", 1, "yes"):
            normalized = store._normalize_settings({"notify_on_own_pr_ci_failure": raw})
            self.assertIs(normalized["notify_on_own_pr_ci_failure"], False, raw)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.write_config({"repos": [{"owner": "o", "repo": "r"}]}, root)
            saved = store.write_repo_settings(
                "o", "r", {"notify_on_own_pr_ci_failure": True}, expected_revision=0, root=root
            )
            self.assertIs(saved["notify_on_own_pr_ci_failure"], True)
            self.assertIs(
                store.read_repo_settings("o", "r", root)["notify_on_own_pr_ci_failure"], True
            )
            with self.assertRaises(store.SettingsConflict):
                store.write_repo_settings(
                    "o",
                    "r",
                    {"notify_on_own_pr_ci_failure": False},
                    expected_revision=0,
                    root=root,
                )

    def test_turning_alerts_on_clears_the_ci_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.write_config({"repos": [{"owner": "o", "repo": "r"}]}, root)
            store.write_ci_watch_state("o", "r", {"5": 1}, root=root)
            store.write_repo_settings(
                "o", "r", {"notify_on_own_pr_ci_failure": True}, expected_revision=0, root=root
            )
            self.assertEqual(
                store.read_ci_watch_state("o", "r", root), {"seeded": False, "notified": {}}
            )
            # Staying on (any other settings save) keeps the record.
            store.write_ci_watch_state("o", "r", {"6": 1}, root=root)
            store.write_repo_settings(
                "o", "r", {"notify_on_own_pr_ci_failure": True}, expected_revision=1, root=root
            )
            self.assertEqual(store.read_ci_watch_state("o", "r", root)["notified"], {"6": 1})

    def test_a_write_from_before_a_reset_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = store.ci_watch_generation("o", "r", root)
            store.reset_ci_watch_state("o", "r", root)
            self.assertFalse(
                store.write_ci_watch_state("o", "r", {"7": 1}, root, generation=before)
            )
            self.assertEqual(
                store.read_ci_watch_state("o", "r", root), {"seeded": False, "notified": {}}
            )
            now = store.ci_watch_generation("o", "r", root)
            self.assertTrue(store.write_ci_watch_state("o", "r", {"7": 1}, root, generation=now))
            self.assertEqual(store.read_ci_watch_state("o", "r", root)["notified"], {"7": 1})

    def test_the_paired_read_waits_for_a_reset_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.write_ci_watch_state("o", "r", {"8": 1}, root)
            before = store.ci_watch_generation("o", "r", root)
            pairs: list[tuple[dict[str, Any], int]] = []
            with store._ci_watch_lock:  # a reset holding the lock mid-way
                reader = threading.Thread(
                    target=lambda: pairs.append(store.read_ci_watch_record("o", "r", root)),
                    daemon=True,
                )
                reader.start()
                key = (str(root), "o", "r")
                store._ci_watch_generations[key] = before + 1
                store.ci_watch_state_path("o", "r", root).unlink()
            reader.join(timeout=30)
            self.assertFalse(reader.is_alive(), "the paired read never returned")
            self.assertEqual(pairs, [({"seeded": False, "notified": {}}, before + 1)])

    def test_ci_record_round_trips_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                store.read_ci_watch_state("o", "r", root), {"seeded": False, "notified": {}}
            )
            many = {str(i): 1 for i in range(store.CI_WATCH_MAX_RUNS + 5)}
            store.write_ci_watch_state("o", "r", many, root)
            back = store.read_ci_watch_state("o", "r", root)
            self.assertTrue(back["seeded"])
            self.assertEqual(len(back["notified"]), store.CI_WATCH_MAX_RUNS)
            self.assertNotIn("0", back["notified"])  # the oldest are dropped
            store.ci_watch_state_path("o", "r", root).write_text("[1]", encoding="utf-8")
            self.assertEqual(
                store.read_ci_watch_state("o", "r", root), {"seeded": False, "notified": {}}
            )


class TestOwnPullSearch(unittest.TestCase):
    def test_query_is_scoped_to_the_viewer_and_the_repos(self) -> None:
        seen: list[list[str]] = []

        def fake(argv: list[str], *, timeout: float, input_text: str | None = None):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=_graphql_stdout("me", []), stderr="")

        repos = [(f"o{i}", "r") for i in range(7)] + [("bad owner", "r")]
        with mock.patch.object(github_client, "_gh_run", side_effect=fake):
            out = github_client.search_viewer_open_pull_heads(repos)
        self.assertEqual(out, {"pulls": [], "truncated": []})
        self.assertEqual(len(seen), 2)  # 7 valid repos, five per query
        query = next(a for a in seen[0] if a.startswith("q="))
        self.assertIn("author:@me", query)
        self.assertIn("is:open", query)
        self.assertIn("repo:o0/r", query)
        self.assertNotIn("bad owner", " ".join(" ".join(a) for a in seen))

    def test_teammate_and_malformed_nodes_are_dropped(self) -> None:
        nodes = [
            _pr_node(1, "Me", "a" * 40),
            _pr_node(2, "teammate", "b" * 40),
            {**_pr_node(3, "me", "c" * 40), "headRefOid": "not-a-sha"},
            None,
        ]
        proc = subprocess.CompletedProcess([], 0, stdout=_graphql_stdout("me", nodes), stderr="")
        with mock.patch.object(github_client, "_gh_run", return_value=proc):
            out = github_client.search_viewer_open_pull_heads([("o", "r")])
        self.assertEqual([p["number"] for p in out["pulls"]], [1])

    def test_search_follows_pages_and_reports_a_capped_one(self) -> None:
        calls: list[list[str]] = []

        def page(number: int, sha: str, repo: str, more: bool, cursor: str | None) -> str:
            return json.dumps(
                {
                    "viewer": {"login": "me"},
                    "search": {
                        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                        "nodes": [_pr_node(number, "me", sha, repo=repo)],
                    },
                }
            )

        def paged(argv: list[str], *, timeout: float, input_text: str | None = None):
            calls.append(argv)
            q = next(a for a in argv if a.startswith("q="))
            after = next((a for a in argv if a.startswith("after=")), None)
            if "repo:o/many" in q:  # never ends: always one more page
                out = page(len(calls), "d" * 40, "o/many", True, f"c{len(calls)}")
            elif after is None:
                out = page(1, "a" * 40, "o/r", True, "p2")
            else:
                self.assertEqual(after, "after=p2")
                out = page(2, "b" * 40, "o/r", False, None)
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        with mock.patch.object(github_client, "_gh_run", side_effect=paged):
            two = github_client.search_viewer_open_pull_heads([("o", "r")])
        self.assertEqual([p["number"] for p in two["pulls"]], [1, 2])
        self.assertEqual(two["truncated"], [])
        calls.clear()
        with mock.patch.object(github_client, "_gh_run", side_effect=paged):
            capped = github_client.search_viewer_open_pull_heads([("o", "many")])
        self.assertEqual(capped, {"pulls": [], "truncated": [("o", "many")]})
        self.assertEqual(len(calls), github_client.CI_SEARCH_MAX_PAGES)

    def test_recent_run_fields_are_bounded(self) -> None:
        raw = [
            {
                "id": 9,
                "name": "n" * 5000,
                "status": "completed",
                "conclusion": "failure",
                "url": "u" * 5000,
                "branch": "b" * 5000,
                "pull_requests": [*range(1, 500), "x", True],
                "created_at": "2026-01-01T00:00:00Z",
                "extra": "y" * 5000,
            }
        ]
        with mock.patch.object(github_client, "_run_gh_api", return_value=raw):
            (row,) = github_client.list_recent_workflow_runs("o", "r")
        self.assertNotIn("extra", row)
        self.assertLessEqual(len(row["name"]), 300)
        self.assertLessEqual(len(row["url"]), 500)
        self.assertLessEqual(len(row["branch"]), 300)
        self.assertEqual(row["pull_requests"], list(range(1, 11)))

    def test_recent_runs_carry_duration(self) -> None:
        raw = [
            {
                "id": 1,
                "status": "completed",
                "conclusion": "failure",
                "started_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:02:30Z",
            },
            {"id": 2, "status": "queued", "created_at": "2026-01-01T00:00:00Z"},
            {"no_id": True},
        ]
        with mock.patch.object(github_client, "_run_gh_api", return_value=raw) as api:
            rows = github_client.list_recent_workflow_runs("o", "r")
        self.assertIn("per_page=20", api.call_args.args[0])
        self.assertEqual([r["duration_sec"] for r in rows], [150, None])


class TestCiRunsRoute(unittest.IsolatedAsyncioTestCase):
    async def test_lists_github_repos_with_a_per_repo_error_row(self) -> None:
        from kiro_crew.apps.builtins.issue_radar.backend.http_routes import ci_runs

        repos = [
            {"owner": "o", "repo": "r"},
            {"owner": "g", "repo": "p", "provider": "gitlab", "host": "gitlab.com"},
            {"owner": "o", "repo": "down"},
        ]

        def fake_runs(owner: str, repo: str) -> list[dict]:
            if repo == "down":
                raise github_client.GhCliError("gh: HTTP 404 /private/path")
            return [{"id": 1, "status": "completed", "conclusion": "failure"}]

        def fake_settings(owner: str, repo: str, **_kw: Any) -> dict[str, Any]:
            return {"notify_on_own_pr_ci_failure": repo == "r"}

        with (
            mock.patch.object(ci_runs.store, "list_connected_repos", return_value=repos),
            mock.patch.object(ci_runs.store, "read_repo_settings", side_effect=fake_settings),
            mock.patch.object(
                ci_runs.github_client, "list_recent_workflow_runs", side_effect=fake_runs
            ),
        ):
            resp = await ci_runs._handle_ci_runs(cast(web.Request, mock.Mock()))
        body = json.loads(cast(bytes, resp.body))
        self.assertEqual(body["truncated"], False)
        rows = {r["repo"]: r for r in body["repos"]}
        self.assertEqual(set(rows), {"r", "down"})  # the GitLab repo is not listed
        self.assertTrue(rows["r"]["alerts"])
        self.assertEqual(rows["r"]["runs"][0]["id"], 1)
        self.assertIsNone(rows["r"]["error"])
        self.assertEqual(rows["down"]["error"], "provider_error")
        self.assertEqual(rows["down"]["runs"], [])
        self.assertNotIn("private", resp.text or "")

    async def test_repo_count_is_bounded(self) -> None:
        from kiro_crew.apps.builtins.issue_radar.backend.http_routes import ci_runs

        repos = [{"owner": "o", "repo": f"r{i}"} for i in range(ci_runs.CI_RUNS_MAX_REPOS + 3)]
        with (
            mock.patch.object(ci_runs.store, "list_connected_repos", return_value=repos),
            mock.patch.object(ci_runs.store, "read_repo_settings", return_value={}),
            mock.patch.object(
                ci_runs.github_client, "list_recent_workflow_runs", return_value=[]
            ) as runs,
        ):
            resp = await ci_runs._handle_ci_runs(cast(web.Request, mock.Mock()))
        body = json.loads(cast(bytes, resp.body))
        self.assertEqual(len(body["repos"]), ci_runs.CI_RUNS_MAX_REPOS)
        self.assertTrue(body["truncated"])
        self.assertEqual(runs.call_count, ci_runs.CI_RUNS_MAX_REPOS)


if __name__ == "__main__":
    unittest.main()
