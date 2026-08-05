"""Tests for Issue Radar's cheap change probe on the OPEN issue / PR lists.

The open lists are fully paginated, so a speculative refetch costs tens of REST
requests plus a multi-MB cache rewrite on a large repo. The client's 60s poll
therefore sends ``poll=1`` and the backend decides: one search call reports the
open set's newest ``updated_at`` and total count, and the paginated fetch is only
paid when that moved since the reading recorded with the cached rows.

Coverage is deliberately at three levels, because the middle one is where a
regression hides best:
  * ``probe_open_list`` — the request shape and its failure modes;
  * ``_poll_can_serve_cache`` — the decision, including the staleness ceiling
    that bounds a probe which is WRONG rather than merely unavailable;
  * the ``/issues`` and ``/pulls`` HANDLERS — that the decision is actually wired
    into both routes. Without these, deleting the ``is_poll`` branch from either
    handler leaves every other test green while ``poll=1`` silently serves the
    TTL-less cache forever.

Every test patches ``_run_gh_api`` or the fetch helpers, so no ``gh`` subprocess
is spawned (and the POSIX-only ``_gh_bin`` guard is never reached — these run on
Windows too).
"""
import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import routes, store

PROBE_TARGET = "kiro_crew.apps.builtins.issue_radar.backend.github_client.probe_open_list"


def _key(owner: str, repo: str):
    """The GitHub repo key these tests exercise.

    The poll path is provider-dispatched now, so it takes a key rather than a
    loose owner/repo pair. GitHub is used throughout here so these tests keep
    asserting the ORIGINAL probe behaviour unchanged.
    """
    from kiro_crew.apps.builtins.issue_radar.backend import provider

    return provider.key_from_parts(owner, repo)


def _snapshot(probe=None, age_sec=0.0, rows=None):
    return {"rows": rows if rows is not None else [], "probe": probe, "age_sec": age_sec}


def _age_cache_payload(path: Path, seconds: float) -> None:
    """Backdate a list cache's recorded fetch time.

    The age comes from the payload rather than the file's mtime (a write-through
    patch rewrites the file without refetching), so ``os.utime`` cannot age a
    cache any more.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    data["fetched_at"] = time.time() - seconds
    path.write_text(json.dumps(data), encoding="utf-8")


def _clear_probe_state() -> None:
    routes._probe_memo.clear()
    routes._probe_inflight.clear()


class TestProbeOpenList(unittest.TestCase):
    def test_parses_total_count_and_top_timestamp(self):
        with mock.patch.object(
            gh, "_run_gh_api",
            return_value=[{"total_count": 41, "top_updated_at": "2026-07-26T00:00:00Z"}],
        ) as run:
            probe = gh.probe_open_list("o", "r", "issue")
        self.assertEqual(probe, {"total_count": 41, "top_updated_at": "2026-07-26T00:00:00Z"})
        # One request, one item — the probe must never walk the list it guards.
        self.assertIn("per_page=1", run.call_args[0][0])
        self.assertIs(run.call_args[1]["paginate"], False)

    def test_query_scopes_to_the_repo_and_excludes_the_other_kind(self):
        # `is:issue` matters: the REST issues endpoint mixes PRs in, so a
        # one-item peek there could report a PR's timestamp and make the issue
        # list refetch on unrelated PR activity.
        with mock.patch.object(
            gh, "_run_gh_api", return_value=[{"total_count": 0, "top_updated_at": None}],
        ) as run:
            gh.probe_open_list("myorg", "myrepo", "issue")
        issue_path = run.call_args[0][0]
        self.assertIn("repo%3Amyorg%2Fmyrepo", issue_path)
        self.assertIn("is%3Aissue", issue_path)
        self.assertIn("state%3Aopen", issue_path)

        with mock.patch.object(
            gh, "_run_gh_api", return_value=[{"total_count": 0, "top_updated_at": None}],
        ) as run:
            gh.probe_open_list("myorg", "myrepo", "pr")
        self.assertIn("is%3Apr", run.call_args[0][0])

    def test_empty_open_set_probes_cleanly(self):
        with mock.patch.object(
            gh, "_run_gh_api", return_value=[{"total_count": 0, "top_updated_at": None}],
        ):
            self.assertEqual(
                gh.probe_open_list("o", "r", "pr"),
                {"total_count": 0, "top_updated_at": None},
            )

    def test_unknown_kind_rejected(self):
        with self.assertRaises(gh.GhCliError):
            gh.probe_open_list("o", "r", "comment")

    def test_missing_envelope_or_count_raises(self):
        # A probe that cannot be trusted must RAISE, so the caller falls back to
        # its fail-safe instead of comparing against a half-parsed reading.
        with mock.patch.object(gh, "_run_gh_api", return_value=[]):
            with self.assertRaises(gh.GhCliError):
                gh.probe_open_list("o", "r", "issue")
        with mock.patch.object(gh, "_run_gh_api", return_value=[{"top_updated_at": "x"}]):
            with self.assertRaises(gh.GhCliError):
                gh.probe_open_list("o", "r", "issue")


class TestListSnapshot(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_rows_probe_and_age_come_from_one_read(self):
        probe = {"total_count": 7, "top_updated_at": "2026-07-26T10:00:00Z"}
        store.write_issues_cache("o", "r", [{"number": 1}], root=self.root, probe=probe)
        snap = store.read_issues_snapshot("o", "r", self.root)
        assert snap is not None
        self.assertEqual(snap["rows"], [{"number": 1}])
        self.assertEqual(snap["probe"], probe)
        self.assertLess(snap["age_sec"], 60)
        # The plain rows reader still works and is unaffected by the probe key.
        self.assertEqual(store.read_issues_cache("o", "r", self.root), [{"number": 1}])

    def test_absent_probe_reads_as_unknown_not_as_a_match(self):
        # A cache written without a probe (an older build, or a path that had no
        # reading to record) must NOT be served as verified — None here makes the
        # poll refetch once and record a probe for next time.
        store.write_issues_cache("o", "r", [{"number": 1}], root=self.root)
        snap = store.read_issues_snapshot("o", "r", self.root)
        assert snap is not None
        self.assertIsNone(snap["probe"])

    def test_pulls_snapshot_round_trips(self):
        probe = {"total_count": 2, "top_updated_at": "2026-07-26T11:00:00Z"}
        store.write_pulls_cache("o", "r", [{"number": 9}], root=self.root, probe=probe)
        snap = store.read_pulls_snapshot("o", "r", self.root)
        assert snap is not None
        self.assertEqual(snap["rows"], [{"number": 9}])
        self.assertEqual(snap["probe"], probe)

    def test_probe_is_per_state(self):
        store.write_issues_cache(
            "o", "r", [], root=self.root, state="open",
            probe={"total_count": 1, "top_updated_at": "a"},
        )
        self.assertIsNone(store.read_issues_snapshot("o", "r", self.root, state="closed"))

    def test_age_comes_from_the_payload_not_the_mtime(self):
        store.write_issues_cache("o", "r", [], root=self.root)
        path = store.issues_cache_path("o", "r", self.root, "open")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["fetched_at"] = time.time() - 4_000
        path.write_text(json.dumps(data), encoding="utf-8")
        snap = store.read_issues_snapshot("o", "r", self.root)
        assert snap is not None
        self.assertGreater(snap["age_sec"], 3_000)

    def test_mtime_is_the_fallback_for_a_cache_without_a_fetch_stamp(self):
        # Written by a build that predates the field: age is still bounded, just
        # from the file's mtime, for one refresh cycle.
        store.write_issues_cache("o", "r", [], root=self.root)
        path = store.issues_cache_path("o", "r", self.root, "open")
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["fetched_at"]
        path.write_text(json.dumps(data), encoding="utf-8")
        old = time.time() - 4_000
        os.utime(path, (old, old))
        snap = store.read_issues_snapshot("o", "r", self.root)
        assert snap is not None
        self.assertGreater(snap["age_sec"], 3_000)

    def test_a_check_write_through_does_not_reset_the_age(self):
        # The regression this field exists for: the PR detail poll patches its
        # check tally into the list cache every 30s, which rewrites the file. With
        # the age read from the mtime, the poll staleness ceiling could NEVER fire
        # for the open-PR list while a PR pane was open — exactly the case it was
        # written to bound.
        store.write_pulls_cache("o", "r", [{"number": 4, "checks_state": "pending"}],
                                root=self.root)
        path = store.pulls_cache_path("o", "r", self.root, "open")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["fetched_at"] = time.time() - 4_000
        path.write_text(json.dumps(data), encoding="utf-8")
        store.apply_pr_checks_to_list_cache(
            "o", "r", 4, {"checks_counts": {"failing": 1}, "checks_state": "failing"},
            root=self.root,
        )
        snap = store.read_pulls_snapshot("o", "r", self.root)
        assert snap is not None
        self.assertGreater(snap["age_sec"], 3_000)  # patched, not refetched
        self.assertEqual(snap["rows"][0]["checks_state"], "failing")

    def test_an_identical_check_write_through_does_not_rewrite_the_file(self):
        # This file is multi-MB on a busy repo and the detail poll calls the patch
        # every 30s; an unchanged tally is not worth the write.
        summary = {"checks_counts": {"passing": 2}, "checks_state": "passing"}
        store.write_pulls_cache("o", "r", [{"number": 4}], root=self.root)
        store.apply_pr_checks_to_list_cache("o", "r", 4, summary, root=self.root)
        path = store.pulls_cache_path("o", "r", self.root, "open")
        before = path.stat().st_mtime_ns
        store.apply_pr_checks_to_list_cache("o", "r", 4, summary, root=self.root)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_a_label_write_through_keeps_the_probe_and_the_age(self):
        # The Tagging dashboard patches labels into this same file (store.
        # apply_label_change_to_caches) without refetching. That patch is a
        # read-modify-write of the WHOLE payload, so it has to carry `probe` and
        # `fetched_at` through untouched: dropping the probe would make the next
        # poll see "no recorded reading" and fall back to a full paginated
        # refetch, silently undoing the probe gate for anyone who applies labels.
        probe = {"top_updated_at": "2026-07-01T00:00:00Z", "total_count": 3}
        store.write_issues_cache(
            "o", "r", [{"number": 7, "labels": ["old"]}], root=self.root, probe=probe,
        )
        path = store.issues_cache_path("o", "r", self.root, "open")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["fetched_at"] = time.time() - 4_000
        path.write_text(json.dumps(data), encoding="utf-8")

        store.apply_label_change_to_caches(
            "o", "r", 7, [{"name": "bug", "color": "d73a4a"}], root=self.root,
        )

        snap = store.read_issues_snapshot("o", "r", self.root)
        assert snap is not None
        self.assertEqual(snap["rows"][0]["labels"], ["bug"])
        self.assertEqual(snap["probe"], probe)
        self.assertGreater(snap["age_sec"], 3_000)  # patched, not refetched

    def test_missing_and_stale_schema_read_as_a_miss(self):
        self.assertIsNone(store.read_issues_snapshot("o", "r", self.root))
        path = store.issues_cache_path("o", "r", self.root, "open")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": "ancient", "issues": []}), encoding="utf-8")
        self.assertIsNone(store.read_issues_snapshot("o", "r", self.root))


class TestPollCanServeCache(unittest.TestCase):
    """The decision itself: when may a poll be answered from the cache?"""

    def setUp(self):
        _clear_probe_state()
        self.addCleanup(_clear_probe_state)

    @staticmethod
    def _decide(kind, state, snapshot, **probe_kwargs):
        with mock.patch(PROBE_TARGET, **probe_kwargs) as probe:
            out = asyncio.run(routes._poll_can_serve_cache(_key("o", "r"), kind, state, snapshot))
        return out, probe

    def test_unchanged_probe_serves_the_cache(self):
        reading = {"total_count": 5, "top_updated_at": "2026-07-26T00:00:00Z"}
        (serve, probe_val), _ = self._decide(
            "issue", "open", _snapshot(dict(reading)), return_value=dict(reading),
        )
        self.assertTrue(serve)
        self.assertEqual(probe_val, reading)

    def test_new_timestamp_forces_a_refetch(self):
        recorded = {"total_count": 5, "top_updated_at": "2026-07-26T00:00:00Z"}
        fresh = {"total_count": 5, "top_updated_at": "2026-07-26T01:00:00Z"}
        (serve, probe_val), _ = self._decide(
            "issue", "open", _snapshot(recorded), return_value=fresh,
        )
        self.assertFalse(serve)
        # The FRESH reading comes back so the caller records it with the new rows.
        self.assertEqual(probe_val, fresh)

    def test_count_drop_forces_a_refetch(self):
        # Closing an issue removes it from the open set without bumping any
        # remaining item's timestamp — the count is the only signal.
        recorded = {"total_count": 5, "top_updated_at": "2026-07-26T00:00:00Z"}
        fresh = {"total_count": 4, "top_updated_at": "2026-07-26T00:00:00Z"}
        (serve, _), _ = self._decide("issue", "open", _snapshot(recorded), return_value=fresh)
        self.assertFalse(serve)

    def test_no_recorded_probe_forces_a_refetch_but_still_reports_one(self):
        fresh = {"total_count": 5, "top_updated_at": "2026-07-26T00:00:00Z"}
        (serve, probe_val), _ = self._decide(
            "issue", "open", _snapshot(None), return_value=fresh,
        )
        self.assertFalse(serve)
        self.assertEqual(probe_val, fresh)

    def test_probe_failure_keeps_serving_the_cache(self):
        # Refetching on every failed probe would turn a sustained probe outage
        # into the paginated-fetch-per-minute drain this path exists to avoid.
        # Staleness stays bounded by the ceiling, not by this branch.
        recorded = {"total_count": 5, "top_updated_at": "2026-07-26T00:00:00Z"}
        (serve, probe_val), _ = self._decide(
            "issue", "open", _snapshot(recorded), side_effect=gh.GhCliError("boom"),
        )
        self.assertTrue(serve)
        # Nothing is recorded from a failed probe, so the next fetch re-probes
        # rather than pinning a reading that was never taken.
        self.assertIsNone(probe_val)

    def test_staleness_ceiling_refetches_without_probing(self):
        # Bounds every way the probe can be WRONG rather than unavailable: a
        # consistently wrong reading matches its own prior recording forever, so
        # no amount of error handling catches it. Notably, a PR check run turning
        # red changes neither updated_at nor the open count.
        recorded = {"total_count": 5, "top_updated_at": "2026-07-26T00:00:00Z"}
        snap = _snapshot(dict(recorded), age_sec=routes.LIST_POLL_MAX_STALENESS_SEC + 1)
        (serve, probe_val), probe = self._decide(
            "issue", "open", snap, return_value=dict(recorded),
        )
        self.assertFalse(serve)
        self.assertIsNone(probe_val)
        probe.assert_not_called()

    def test_just_inside_the_ceiling_still_probes(self):
        recorded = {"total_count": 5, "top_updated_at": "2026-07-26T00:00:00Z"}
        snap = _snapshot(dict(recorded), age_sec=routes.LIST_POLL_MAX_STALENESS_SEC - 1)
        (serve, _), probe = self._decide("issue", "open", snap, return_value=dict(recorded))
        self.assertTrue(serve)
        probe.assert_called_once()

    def test_ceiling_is_a_multiple_of_the_client_poll(self):
        # ~6 full fetches an hour in the worst case, still an order of magnitude
        # below the unprobed cost. Pinned so it cannot drift to "never".
        self.assertEqual(routes.LIST_POLL_MAX_STALENESS_SEC, 600.0)

    def test_closed_lists_are_not_probed(self):
        # The closed lists are one bounded page, so refetching is already one
        # request — probing would just add a second.
        recorded = {"total_count": 5, "top_updated_at": "2026-07-26T00:00:00Z"}
        (serve, probe_val), probe = self._decide(
            "issue", "closed", _snapshot(dict(recorded)), return_value=dict(recorded),
        )
        self.assertFalse(serve)
        self.assertIsNone(probe_val)
        probe.assert_not_called()


class TestProbeCoalescing(unittest.TestCase):
    def setUp(self):
        _clear_probe_state()
        self.addCleanup(_clear_probe_state)

    def test_repeated_polls_share_one_reading(self):
        # Otherwise every visible tab probes on its own cadence and the search
        # quota (30/min, shared with the user's own searches) scales with tabs.
        reading = {"total_count": 3, "top_updated_at": "2026-07-26T00:00:00Z"}
        with mock.patch(PROBE_TARGET, return_value=reading) as probe:
            async def three_polls():
                snap = _snapshot(dict(reading))
                return [
                    await routes._poll_can_serve_cache(_key("o", "r"), "issue", "open", snap)
                    for _ in range(3)
                ]
            results = asyncio.run(three_polls())
        self.assertTrue(all(serve for serve, _ in results))
        probe.assert_called_once()

    def test_different_kinds_and_repos_do_not_share(self):
        reading = {"total_count": 3, "top_updated_at": "2026-07-26T00:00:00Z"}
        with mock.patch(PROBE_TARGET, return_value=reading) as probe:
            async def mixed():
                snap = _snapshot(dict(reading))
                await routes._poll_can_serve_cache(_key("o", "r"), "issue", "open", snap)
                await routes._poll_can_serve_cache(_key("o", "r"), "pr", "open", snap)
                await routes._poll_can_serve_cache(_key("o", "other"), "issue", "open", snap)
            asyncio.run(mixed())
        self.assertEqual(probe.call_count, 3)

    def test_window_expires(self):
        reading = {"total_count": 3, "top_updated_at": "2026-07-26T00:00:00Z"}
        with mock.patch(PROBE_TARGET, return_value=reading) as probe:
            async def two_polls_apart():
                snap = _snapshot(dict(reading))
                await routes._poll_can_serve_cache(_key("o", "r"), "issue", "open", snap)
                # Age the memo entry past the window instead of sleeping.
                key, (taken_at, val) = next(iter(routes._probe_memo.items()))
                routes._probe_memo[key] = (taken_at - routes._PROBE_COALESCE_SEC - 1, val)
                await routes._poll_can_serve_cache(_key("o", "r"), "issue", "open", snap)
            asyncio.run(two_polls_apart())
        self.assertEqual(probe.call_count, 2)

    def test_concurrent_polls_for_one_key_share_a_single_in_flight_probe(self):
        # The memo only helps a poll that arrives AFTER a reading landed. Two
        # polls in flight at once (two tabs on the same repo) must join one call
        # rather than each spending from the 30/min search quota.
        reading = {"total_count": 3, "top_updated_at": "2026-07-26T00:00:00Z"}
        started = threading.Event()
        release = threading.Event()

        def slow_probe(owner, repo, kind):
            started.set()
            release.wait(5)
            return dict(reading)

        with mock.patch(PROBE_TARGET, side_effect=slow_probe) as probe:
            async def two_at_once():
                snap = _snapshot(dict(reading))
                first = asyncio.create_task(
                    routes._poll_can_serve_cache(_key("o", "r"), "issue", "open", snap)
                )
                await asyncio.to_thread(started.wait, 5)
                second = asyncio.create_task(
                    routes._poll_can_serve_cache(_key("o", "r"), "issue", "open", snap)
                )
                await asyncio.sleep(0)  # let the second reach the shared future
                release.set()
                return await asyncio.gather(first, second)
            results = asyncio.run(two_at_once())
        self.assertTrue(all(serve for serve, _ in results))
        probe.assert_called_once()

    def test_a_slow_probe_does_not_block_another_key(self):
        # The regression guard for the lock scope: holding the map lock across the
        # probe made one repo's 20s `gh` timeout stall every other repo's and
        # kind's poll response. Here the SECOND key must finish while the first
        # is still blocked.
        reading = {"total_count": 3, "top_updated_at": "2026-07-26T00:00:00Z"}
        blocked = threading.Event()
        release = threading.Event()

        def probe_impl(owner, repo, kind):
            if repo == "slow":
                blocked.set()
                release.wait(5)
            return dict(reading)

        with mock.patch(PROBE_TARGET, side_effect=probe_impl):
            async def overlap():
                snap = _snapshot(dict(reading))
                slow = asyncio.create_task(
                    routes._poll_can_serve_cache(_key("o", "slow"), "issue", "open", snap)
                )
                await asyncio.to_thread(blocked.wait, 5)
                # No timeout escape hatch on purpose: if this serializes behind
                # the blocked probe it deadlocks until `release`, and asserting on
                # a wall-clock margin would just make the test flaky on CI.
                fast = await asyncio.wait_for(
                    routes._poll_can_serve_cache(_key("o", "fast"), "issue", "open", snap), 5,
                )
                release.set()
                return fast, await slow
            fast, slow = asyncio.run(overlap())
        self.assertTrue(fast[0])
        self.assertTrue(slow[0])

    def test_a_cancelled_caller_still_publishes_its_reading(self):
        # The reading is recorded by a done-callback, not by the awaiting request,
        # so a closed tab does not waste the call it already paid for.
        reading = {"total_count": 3, "top_updated_at": "2026-07-26T00:00:00Z"}
        started = threading.Event()
        release = threading.Event()

        def slow_probe(owner, repo, kind):
            started.set()
            release.wait(5)
            return dict(reading)

        with mock.patch(PROBE_TARGET, side_effect=slow_probe) as probe:
            async def cancel_then_poll():
                snap = _snapshot(dict(reading))
                doomed = asyncio.create_task(
                    routes._poll_can_serve_cache(_key("o", "r"), "issue", "open", snap)
                )
                await asyncio.to_thread(started.wait, 5)
                doomed.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await doomed
                release.set()
                # The shielded probe survives the cancellation; give it the loop.
                for _ in range(50):
                    if routes._probe_memo:
                        break
                    await asyncio.sleep(0.01)
                return await routes._poll_can_serve_cache(_key("o", "r"), "issue", "open", snap)
            serve, _ = asyncio.run(cancel_then_poll())
        self.assertTrue(serve)
        probe.assert_called_once()  # the second poll reused the cancelled one's reading


class _HandlerCase(unittest.TestCase):
    """Shared scaffolding for the route-level tests."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        _clear_probe_state()
        self.addCleanup(_clear_probe_state)
        # Every handler gates on the repo being connected; the data root is
        # redirected at the store so nothing touches the real app data dir.
        self._patches = [
            mock.patch.object(store, "repo_data_dir", lambda o, r, root=None: self._repo_dir(o, r)),
            mock.patch.object(
                store, "is_repo_connected",
                lambda o, r, root=None, **kw: True,
            ),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _repo_dir(self, owner, repo):
        d = self.root / "repos" / owner / repo
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _get(path):
        return make_mocked_request("GET", path)

    @staticmethod
    def _body(response):
        return json.loads(response.body.decode("utf-8"))


class TestIssuesHandlerPolling(_HandlerCase):
    READING = {"total_count": 1, "top_updated_at": "2026-07-26T00:00:00Z"}

    def _seed(self, probe=None):
        store.write_issues_cache("o", "r", [{"number": 1, "title": "cached"}], probe=probe)

    def test_poll_with_matching_probe_serves_cache_without_fetching(self):
        self._seed(probe=dict(self.READING))
        with mock.patch(PROBE_TARGET, return_value=dict(self.READING)), \
                mock.patch.object(gh, "list_open_issues") as fetch:
            resp = asyncio.run(routes._handle_issues(self._get("/issues?owner=o&repo=r&poll=1")))
        body = self._body(resp)
        self.assertTrue(body["from_cache"])
        self.assertEqual(body["issues"][0]["title"], "cached")
        fetch.assert_not_called()

    def test_poll_with_changed_probe_refetches_and_records_the_new_reading(self):
        self._seed(probe=dict(self.READING))
        moved = {"total_count": 2, "top_updated_at": "2026-07-26T02:00:00Z"}
        with mock.patch(PROBE_TARGET, return_value=moved), \
                mock.patch.object(gh, "list_open_issues", return_value=[{"number": 2, "title": "fresh"}]) as fetch:
            resp = asyncio.run(routes._handle_issues(self._get("/issues?owner=o&repo=r&poll=1")))
        body = self._body(resp)
        self.assertFalse(body["from_cache"])
        self.assertEqual(body["issues"][0]["title"], "fresh")
        fetch.assert_called_once()
        # The reading is persisted, so the NEXT poll can be served from cache.
        snap = store.read_issues_snapshot("o", "r")
        assert snap is not None
        self.assertEqual(snap["probe"], moved)

    def test_plain_request_serves_cache_without_probing(self):
        # The first fetch for a query key sends no flag and must paint instantly.
        self._seed(probe=dict(self.READING))
        with mock.patch(PROBE_TARGET) as probe, mock.patch.object(gh, "list_open_issues") as fetch:
            resp = asyncio.run(routes._handle_issues(self._get("/issues?owner=o&repo=r")))
        self.assertTrue(self._body(resp)["from_cache"])
        probe.assert_not_called()
        fetch.assert_not_called()

    def test_refresh_bypasses_the_probe_entirely(self):
        # The manual Refresh button must always reach GitHub.
        self._seed(probe=dict(self.READING))
        with mock.patch(PROBE_TARGET) as probe, \
                mock.patch.object(gh, "list_open_issues", return_value=[]) as fetch:
            resp = asyncio.run(routes._handle_issues(self._get("/issues?owner=o&repo=r&refresh=1")))
        self.assertFalse(self._body(resp)["from_cache"])
        probe.assert_not_called()
        fetch.assert_called_once()

    def test_poll_on_a_cold_cache_fetches(self):
        with mock.patch(PROBE_TARGET) as probe, \
                mock.patch.object(gh, "list_open_issues", return_value=[]) as fetch:
            resp = asyncio.run(routes._handle_issues(self._get("/issues?owner=o&repo=r&poll=1")))
        self.assertFalse(self._body(resp)["from_cache"])
        probe.assert_not_called()  # nothing to compare against yet
        fetch.assert_called_once()

    def test_closed_state_poll_refetches_the_bounded_page(self):
        store.write_issues_cache("o", "r", [{"number": 1}], state="closed",
                                 probe=dict(self.READING))
        with mock.patch(PROBE_TARGET) as probe, \
                mock.patch.object(gh, "list_closed_issues", return_value=[]) as fetch:
            resp = asyncio.run(
                routes._handle_issues(self._get("/issues?owner=o&repo=r&state=closed&poll=1"))
            )
        self.assertFalse(self._body(resp)["from_cache"])
        probe.assert_not_called()
        fetch.assert_called_once()


class TestPullsHandlerPolling(_HandlerCase):
    READING = {"total_count": 1, "top_updated_at": "2026-07-26T00:00:00Z"}

    def _seed(self, probe=None):
        store.write_pulls_cache("o", "r", [{"number": 1, "title": "cached"}], probe=probe)

    def test_poll_with_matching_probe_serves_cache_without_fetching(self):
        self._seed(probe=dict(self.READING))
        with mock.patch(PROBE_TARGET, return_value=dict(self.READING)), \
                mock.patch.object(gh, "list_open_pulls") as fetch:
            resp = asyncio.run(routes._handle_pulls(self._get("/pulls?owner=o&repo=r&poll=1")))
        body = self._body(resp)
        self.assertTrue(body["from_cache"])
        self.assertEqual(body["pulls"][0]["title"], "cached")
        fetch.assert_not_called()

    def test_poll_with_changed_probe_refetches_and_records_the_new_reading(self):
        self._seed(probe=dict(self.READING))
        moved = {"total_count": 2, "top_updated_at": "2026-07-26T02:00:00Z"}
        fresh = [{"number": 2, "title": "fresh"}]
        with mock.patch(PROBE_TARGET, return_value=moved), \
                mock.patch.object(gh, "list_open_pulls", return_value=fresh) as fetch, \
                mock.patch.object(gh, "enrich_pulls", side_effect=lambda o, r, p, s, **kw: p), \
                mock.patch.object(gh, "enrichment_complete", return_value=True):
            resp = asyncio.run(routes._handle_pulls(self._get("/pulls?owner=o&repo=r&poll=1")))
        body = self._body(resp)
        self.assertFalse(body["from_cache"])
        self.assertEqual(body["pulls"][0]["title"], "fresh")
        fetch.assert_called_once()
        snap = store.read_pulls_snapshot("o", "r")
        assert snap is not None
        self.assertEqual(snap["probe"], moved)

    def test_plain_request_serves_cache_without_probing(self):
        self._seed(probe=dict(self.READING))
        with mock.patch(PROBE_TARGET) as probe, mock.patch.object(gh, "list_open_pulls") as fetch:
            resp = asyncio.run(routes._handle_pulls(self._get("/pulls?owner=o&repo=r")))
        self.assertTrue(self._body(resp)["from_cache"])
        probe.assert_not_called()
        fetch.assert_not_called()

    def test_refresh_bypasses_the_probe_entirely(self):
        self._seed(probe=dict(self.READING))
        with mock.patch(PROBE_TARGET) as probe, \
                mock.patch.object(gh, "list_open_pulls", return_value=[]) as fetch, \
                mock.patch.object(gh, "enrich_pulls", side_effect=lambda o, r, p, s, **kw: p), \
                mock.patch.object(gh, "enrichment_complete", return_value=True):
            resp = asyncio.run(routes._handle_pulls(self._get("/pulls?owner=o&repo=r&refresh=1")))
        self.assertFalse(self._body(resp)["from_cache"])
        probe.assert_not_called()
        fetch.assert_called_once()

    def test_stale_cache_refetches_even_when_the_probe_would_match(self):
        # End-to-end proof of the ceiling: a probe that agrees with its own prior
        # recording must not keep the list frozen past the bound.
        self._seed(probe=dict(self.READING))
        _age_cache_payload(
            store.pulls_cache_path("o", "r", None, "open"),
            routes.LIST_POLL_MAX_STALENESS_SEC + 60,
        )
        with mock.patch(PROBE_TARGET, return_value=dict(self.READING)) as probe, \
                mock.patch.object(gh, "list_open_pulls", return_value=[]) as fetch, \
                mock.patch.object(gh, "enrich_pulls", side_effect=lambda o, r, p, s, **kw: p), \
                mock.patch.object(gh, "enrichment_complete", return_value=True):
            resp = asyncio.run(routes._handle_pulls(self._get("/pulls?owner=o&repo=r&poll=1")))
        self.assertFalse(self._body(resp)["from_cache"])
        probe.assert_not_called()
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
