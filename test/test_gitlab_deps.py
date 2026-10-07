"""Tests for GitLab dependency-edge support in Issue Radar.

Covers:
* ``gitlab_queries.fetch_dependency_edges``: blocks/is_blocked_by/relates_to
  handling, mirrored-pair dedupe, closed blocker seeding, one issue's /links
  failing, empty links, call cap.
* ``/deps`` route for a GitLab key: edges served, cache-first path, GitHub path
  unchanged, Azure still empty.

No network, no real glab.  ``run_api`` is injected for all fetcher tests;
route tests patch ``gitlab_queries.fetch_dependency_edges`` directly.
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest import mock

from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import (
    gitlab_queries,
    provider,
    routes,
    store,
)
from kiro_crew.apps.builtins.issue_radar.backend.errors import (
    ProviderCliError,
    ProviderSetupError,
)
from kiro_crew.apps.builtins.issue_radar.backend.gitlab_queries import (
    _MAX_DEPS_EDGES,
    _MAX_DEPS_NODES,
    _MAX_LINKS_PER_ISSUE,
    _MAX_TITLE_CHARS,
)

OWNER, REPO, HOST = "gl-owner", "gl-repo", "gitlab.example.com"

GL_KEY = provider.RepoKey(provider="gitlab", host=HOST, owner=OWNER, repo=REPO)
GH_KEY = provider.RepoKey(provider="github", host="github.com", owner=OWNER, repo=REPO)
AZ_KEY = provider.RepoKey(provider="azure", host="dev.azure.com", owner=OWNER, repo=REPO)


# ── helpers ──────────────────────────────────────────────────────────────────


def _link(iid: int, link_type: str, state: str = "opened", title: str = "t") -> dict:
    """One ``/links`` row for an issue in the SAME project (``references.relative``
    is the bare ``#<iid>`` GitLab uses for that)."""
    return {
        "iid": iid,
        "link_type": link_type,
        "state": state,
        "title": title,
        "references": {"relative": f"#{iid}"},
    }


def _make_run_api(responses: dict[int, list[dict]]) -> object:
    """Fake ``/links`` reader that returns the responses map keyed by iid.

    Called as ``fn(path)`` with ``projects/.../issues/<iid>/links`` (see ``_edges``),
    so the iid is parsed from the tail. Raises ProviderCliError when the iid key is
    missing from the dict to simulate a per-issue failure.
    """

    def _run(path: str, **_kw) -> list:
        iid_str = path.rstrip("/").split("/")[-2]  # .../issues/<iid>/links
        iid = int(iid_str)
        if iid not in responses:
            raise ProviderCliError(f"simulated failure for iid {iid}")
        return list(responses[iid])

    return _run


def _edges(owner, repo, open_issues, *, host=HOST, run_api):
    """Run the fetcher with ``run_api`` standing in for the client's ``/links`` read.

    The production seam is ``gitlab_client.list_issue_links``; the fakes here are
    written against the REST path (``projects/<id>/issues/<iid>/links``) because that
    is what they assert on, so this adapter builds the path the client would and hands
    it over. ``host`` and ``timeout`` are passed through as keywords for fakes that
    want them.
    """
    from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client

    def fake_list_issue_links(o, r, iid, *, host, timeout):
        path = f"projects/{gitlab_client.project_path(o, r)}/issues/{int(iid)}/links"
        return run_api(path, host=host, timeout=timeout)

    with mock.patch.object(gitlab_client, "list_issue_links", side_effect=fake_list_issue_links):
        return gitlab_queries.fetch_dependency_edges(owner, repo, open_issues, host=host)


# ── fetcher unit tests ────────────────────────────────────────────────────────


class TestBlocksLinkType(unittest.TestCase):
    """``blocks`` on issue A → edge {blocked: B, blocker: A}."""

    def test_blocks_produces_correct_direction(self):
        # Issue 10 blocks issue 20.
        run_api = _make_run_api({10: [_link(20, "blocks", "opened")], 20: []})
        issues = [
            {"number": 10, "title": "a", "state": "open"},
            {"number": 20, "title": "b", "state": "open"},
        ]
        edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertIn({"blocked": 20, "blocker": 10, "source": "native"}, edges)
        self.assertNotIn({"blocked": 10, "blocker": 20, "source": "native"}, edges)


class TestIsBlockedByLinkType(unittest.TestCase):
    """``is_blocked_by`` on issue B → edge {blocked: B, blocker: A}."""

    def test_is_blocked_by_produces_correct_direction(self):
        # Issue 10 is_blocked_by issue 5.
        run_api = _make_run_api({10: [_link(5, "is_blocked_by", "opened")], 5: []})
        issues = [
            {"number": 10, "title": "dep", "state": "open"},
            {"number": 5, "title": "blk", "state": "open"},
        ]
        edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertIn({"blocked": 10, "blocker": 5, "source": "native"}, edges)
        self.assertNotIn({"blocked": 5, "blocker": 10, "source": "native"}, edges)


class TestRelatesToIsNotAnEdge(unittest.TestCase):
    """``relates_to`` must NOT become a dependency edge."""

    def test_relates_to_is_ignored(self):
        run_api = _make_run_api({10: [_link(5, "relates_to")], 5: []})
        issues = [
            {"number": 10, "title": "a", "state": "open"},
            {"number": 5, "title": "b", "state": "open"},
        ]
        edges, _ = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(edges, [])


class TestMirroredPairDedupe(unittest.TestCase):
    """A blocks/is_blocked_by mirrored pair must collapse to one edge after
    ``store._normalize_deps`` deduplication."""

    def test_mirrored_pair_dedupes_after_normalize(self):
        # Issue 10 blocks 20 AND issue 20 is_blocked_by 10 — same relationship
        # reported from both sides.  Both edges are {blocked:20, blocker:10}.
        run_api = _make_run_api(
            {
                10: [_link(20, "blocks", "opened")],
                20: [_link(10, "is_blocked_by", "opened")],
            }
        )
        issues = [
            {"number": 10, "title": "a", "state": "open"},
            {"number": 20, "title": "b", "state": "open"},
        ]
        edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        norm_edges, _ = store._normalize_deps(edges, nodes)
        self.assertEqual(
            len([e for e in norm_edges if e["blocked"] == 20 and e["blocker"] == 10]), 1
        )


class TestClosedBlockerState(unittest.TestCase):
    """A ``closed`` linked issue must produce ``state: "closed"`` in nodes."""

    def test_closed_blocker_node_state(self):
        run_api = _make_run_api({10: [_link(5, "is_blocked_by", "closed", "done")]})
        issues = [{"number": 10, "title": "dep", "state": "open"}]
        _, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(nodes["5"]["state"], "closed")


class TestFreshLinkPayloadWins(unittest.TestCase):
    """Fresh link fields update nodes seeded from a cached issue-list snapshot."""

    def test_fresh_closed_state_and_title_replace_seeded_values(self):
        run_api = _make_run_api({10: [_link(5, "is_blocked_by", "closed", "fresh title")], 5: []})
        issues = [
            {"number": 10, "title": "dependent", "state": "open"},
            {"number": 5, "title": "stale title", "state": "open"},
        ]

        _, nodes = _edges(OWNER, REPO, issues, run_api=run_api)

        self.assertEqual(nodes["5"]["state"], "closed")
        self.assertEqual(nodes["5"]["title"], "fresh title")

    def test_omitted_link_fields_leave_seeded_values_unchanged(self):
        link = _link(5, "is_blocked_by")
        link.pop("state")
        link.pop("title")
        run_api = _make_run_api({10: [link], 5: []})
        issues = [
            {"number": 10, "title": "dependent", "state": "open"},
            {"number": 5, "title": "seeded title", "state": "closed"},
        ]

        _, nodes = _edges(OWNER, REPO, issues, run_api=run_api)

        self.assertEqual(nodes["5"]["state"], "closed")
        self.assertEqual(nodes["5"]["title"], "seeded title")

    def test_node_cap_applies_only_when_inserting_a_new_node(self):
        issues = [
            {"number": n, "title": "seeded", "state": "open"} for n in range(1, _MAX_DEPS_NODES + 1)
        ]

        def existing_node_link(path: str, **_kw) -> list[dict]:
            iid = int(path.rstrip("/").split("/")[-2])
            return [_link(2, "blocks", "closed")] if iid == 1 else []

        _, nodes = _edges(OWNER, REPO, issues, run_api=existing_node_link)
        self.assertEqual(len(nodes), _MAX_DEPS_NODES)
        self.assertEqual(nodes["2"]["state"], "closed")

        def new_node_link(path: str, **_kw) -> list[dict]:
            iid = int(path.rstrip("/").split("/")[-2])
            return [_link(_MAX_DEPS_NODES + 1, "blocks", "closed")] if iid == 1 else []

        with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
            edges, nodes = _edges(OWNER, REPO, issues, run_api=new_node_link)
        # The edge to the unretained node is kept (the store tolerates an edge whose
        # endpoint has no node); only the node row past the cap is dropped.
        self.assertEqual(len(nodes), _MAX_DEPS_NODES)
        self.assertNotIn(str(_MAX_DEPS_NODES + 1), nodes)
        self.assertEqual(len(edges), 1)
        self.assertTrue(any("nodes_over_max=1" in line for line in captured.output))


class TestOpenedStateMapsToOpen(unittest.TestCase):
    """GitLab's ``"opened"`` state must normalize to ``"open"``."""

    def test_opened_maps_to_open(self):
        run_api = _make_run_api({10: [_link(5, "is_blocked_by", "opened")]})
        issues = [{"number": 10, "title": "dep", "state": "opened"}]
        _, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(nodes["5"]["state"], "open")
        # The issue node itself should also be "open" (from open_issues seeding).
        self.assertEqual(nodes["10"]["state"], "open")


class TestBoundedTitles(unittest.TestCase):
    """Every externally supplied title is bounded where graph nodes retain it."""

    def test_open_issue_seed_bounds_title_and_keeps_none_empty(self):
        oversized = "x" * (_MAX_TITLE_CHARS + 1)
        issues = [
            {"number": 1, "title": oversized, "state": "open"},
            {"number": 2, "title": None, "state": "open"},
        ]
        _, nodes = _edges(OWNER, REPO, issues, run_api=_make_run_api({1: [], 2: []}))

        self.assertEqual(nodes["1"]["title"], "x" * _MAX_TITLE_CHARS)
        self.assertEqual(len(nodes["1"]["title"]), _MAX_TITLE_CHARS)
        self.assertEqual(nodes["2"]["title"], "")

    def test_link_payload_seed_bounds_title_and_keeps_missing_empty(self):
        oversized = "y" * (_MAX_TITLE_CHARS + 1)
        missing_title = _link(3, "blocks")
        missing_title.pop("title")
        run_api = _make_run_api({1: [_link(2, "blocks", title=oversized), missing_title]})
        issues = [{"number": 1, "title": "source", "state": "open"}]
        _, nodes = _edges(OWNER, REPO, issues, run_api=run_api)

        self.assertEqual(nodes["2"]["title"], "y" * _MAX_TITLE_CHARS)
        self.assertEqual(len(nodes["2"]["title"]), _MAX_TITLE_CHARS)
        self.assertEqual(nodes["3"]["title"], "")


class TestRetentionBounds(unittest.TestCase):
    """Oversized GitLab dependency graphs are truncated at their bounds and served
    partial, never refused: a project past a cap must get the same graph on every load
    (including its first, when there is no cache to fall back to)."""

    def test_first_load_of_a_project_past_the_node_cap_gets_a_partial_graph(self):
        issues = [
            {"number": n, "title": "t", "state": "open"} for n in range(1, _MAX_DEPS_NODES + 2)
        ]

        def link_free(path: str, **_kw) -> list[dict]:
            return []

        with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
            edges, nodes = _edges(OWNER, REPO, issues, run_api=link_free)

        self.assertEqual(edges, [])
        self.assertEqual(len(nodes), _MAX_DEPS_NODES)
        self.assertNotIn(str(_MAX_DEPS_NODES + 1), nodes)
        truncated = [line for line in captured.output if "serving the partial graph" in line]
        self.assertEqual(len(truncated), 1)
        self.assertIn("open_issue_nodes_over_max=1", truncated[0])

    def test_one_issue_over_the_link_row_limit_keeps_the_first_rows(self):
        links = [_link(2, "blocks")] * (_MAX_LINKS_PER_ISSUE + 1)

        with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
            edges, _ = _edges(
                OWNER,
                REPO,
                [{"number": 1, "title": "t", "state": "open"}],
                run_api=_make_run_api({1: links}),
            )

        self.assertEqual(len(edges), _MAX_LINKS_PER_ISSUE)
        self.assertIn("link_rows_over_per_issue_max=1", captured.output[0])

    def test_worker_projects_and_bounds_rows_before_returning(self):
        oversized_title = "t" * (_MAX_TITLE_CHARS + 1)
        large_description = "d" * 100_000
        links = [
            dict(
                _link(2, "blocks", title=oversized_title),
                description=large_description,
            )
            for _ in range(_MAX_LINKS_PER_ISSUE + 1)
        ]

        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client

        with mock.patch.object(gitlab_client, "list_issue_links", return_value=links):
            result = gitlab_queries._fetch_issue_links(OWNER, REPO, 1, host=HOST, timeout=1.0)

        self.assertIsNotNone(result)
        projected, rows_over_max = result
        self.assertEqual(len(projected), _MAX_LINKS_PER_ISSUE)
        self.assertEqual(rows_over_max, 1)
        self.assertTrue(
            all(set(row) == {"link_type", "iid", "relative", "state", "title"} for row in projected)
        )
        self.assertTrue(all(len(row["title"]) == _MAX_TITLE_CHARS for row in projected))

    def test_total_edges_at_the_cap_succeed_and_one_more_is_dropped(self):
        def graph_with_edges(total: int) -> tuple[list[dict], object]:
            issues: list[dict] = []
            responses: dict[int, list[dict]] = {}
            remaining = total
            number = 1
            while remaining:
                batch = min(remaining, _MAX_LINKS_PER_ISSUE)
                issues.append({"number": number, "title": "t", "state": "open"})
                responses[number] = [_link(100_000, "blocks")] * batch
                remaining -= batch
                number += 1
            return issues, _make_run_api(responses)

        issues, run_api = graph_with_edges(_MAX_DEPS_EDGES)
        edges, _ = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(len(edges), _MAX_DEPS_EDGES)

        issues, run_api = graph_with_edges(_MAX_DEPS_EDGES + 1)
        with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
            edges, _ = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(len(edges), _MAX_DEPS_EDGES)
        self.assertIn("edges_over_max=1", captured.output[0])

    def test_warning_names_the_overflow_count(self):
        overflow = 3
        links = [_link(2, "blocks")] * (_MAX_LINKS_PER_ISSUE + overflow)

        with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
            _edges(
                OWNER,
                REPO,
                [{"number": 1, "title": "t", "state": "open"}],
                run_api=_make_run_api({1: links}),
            )

        self.assertEqual(len(captured.records), 1)
        self.assertIn(f"link_rows_over_per_issue_max={overflow}", captured.output[0])


class TestOneIssueLinksFail(unittest.TestCase):
    """A single issue's /links failure must degrade that issue (skip it) without
    aborting the build for the rest."""

    def test_failure_for_one_issue_skips_it(self):
        # _make_run_api raises ProviderCliError for iid 99 (missing key).
        run_api = _make_run_api({10: [_link(5, "is_blocked_by")]})
        # Issue 99 has no entry → ProviderCliError, should be skipped.
        issues = [
            {"number": 10, "title": "a", "state": "open"},
            {"number": 99, "title": "b", "state": "open"},
        ]
        edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        # Issue 10's edge still appears.
        self.assertIn({"blocked": 10, "blocker": 5, "source": "native"}, edges)
        # Issue 99 produced no edges (failure degraded silently).
        self.assertEqual([e for e in edges if e["blocked"] == 99 or e["blocker"] == 99], [])

    def test_all_failures_raise_instead_of_emptying_the_graph(self):
        # Every issue failing is a host-wide condition, not N per-issue skips. Returning
        # an empty graph here would let the route overwrite the previous good graph and
        # serve it as fresh for the TTL; the fetcher raises so the route keeps the cache.
        run_api = _make_run_api({})  # all issues will fail
        issues = [
            {"number": 1, "title": "a", "state": "open"},
            {"number": 2, "title": "b", "state": "open"},
        ]
        with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
            with self.assertRaises(ProviderCliError) as raised:
                _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertNotIsInstance(raised.exception, ProviderSetupError)
        self.assertIn("all 2 /links calls failed", str(raised.exception))
        self.assertIn("failed for every one of 2 open issues", captured.output[0])

    def test_majority_failures_raise_instead_of_shipping_the_survivors(self):
        # 199 of 200 calls failing (rate limiting that set in partway) is not 199
        # per-issue skips: the one survivor would make a complete-looking, nearly
        # empty graph that the route persists over the last good one. Past the
        # majority threshold the fetcher raises exactly as the all-failed case does.
        issues = [{"number": n, "title": "t", "state": "open"} for n in range(1, 11)]
        run_api = _make_run_api({10: [_link(1, "blocks")], 9: [], 8: []})  # 7 of 10 fail
        with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
            with self.assertRaises(ProviderCliError) as raised:
                _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertNotIsInstance(raised.exception, ProviderSetupError)
        self.assertIn("7 of 10 /links calls failed", str(raised.exception))
        self.assertIn("failed for 7 of 10 open issues", captured.output[0])

    def test_minority_failures_still_return_the_partial_graph(self):
        # At or below the threshold the per-issue policy holds: the survivors' edges
        # are the graph. 5 of 10 is exactly half, which is NOT "more than half".
        issues = [{"number": n, "title": "t", "state": "open"} for n in range(1, 11)]
        run_api = _make_run_api({n: [_link(n + 100, "blocks")] for n in range(1, 6)})
        edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(sorted(e["blocked"] for e in edges), [101, 102, 103, 104, 105])
        self.assertEqual(sorted(int(k) for k in nodes), list(range(1, 11)) + list(range(101, 106)))

    def test_no_open_issues_is_an_empty_graph_not_a_failure(self):
        edges, nodes = _edges(OWNER, REPO, [], run_api=_make_run_api({}))
        self.assertEqual((edges, nodes), ([], {}))


class TestSetupErrorIsHostWide(unittest.TestCase):
    """``ProviderSetupError`` (no glab, no session) is not one issue's failure: it
    propagates so the route answers 502 and keeps the previous cache, instead of
    persisting a complete-looking empty graph."""

    def test_setup_error_propagates_instead_of_emptying_the_graph(self):
        def unauthenticated(path: str, **_kw) -> list:
            raise ProviderSetupError(
                "glab is not authenticated for gitlab.example.com", reason="not_authenticated"
            )

        issues = [{"number": n, "title": "t", "state": "open"} for n in range(1, 4)]
        with self.assertRaises(ProviderSetupError):
            _edges(OWNER, REPO, issues, run_api=unauthenticated)

    def test_the_first_setup_error_stops_further_calls(self):
        # Deterministic by construction, not by timing. Exactly one call raises; every
        # other call that the pool has already started blocks until the fetcher's own
        # ``shutdown(cancel_futures=True)`` runs, which the recording executor turns
        # into an Event. So the submitting thread is guaranteed to observe the failure
        # while the rest of the work is still pending, and the assertions are about
        # what the pool can do at all: at most a worker-width of calls can be in flight
        # (plus the one thread that raised and picked up a second item before the
        # cancel landed), and every other future must have been cancelled unstarted.
        import threading
        from concurrent.futures import Future, ThreadPoolExecutor

        calls: list[int] = []
        lock = threading.Lock()
        cancelled = threading.Event()
        futures: list[Future] = []
        timed_out: list[str] = []

        class Recording(ThreadPoolExecutor):
            def submit(self, fn, /, *args, **kwargs):
                future = super().submit(fn, *args, **kwargs)
                futures.append(future)
                return future

            def shutdown(self, wait=True, *, cancel_futures=False):
                # Cancel the queue FIRST, release the blocked workers second. The
                # other order lets a woken worker take a queued item in the gap
                # before it is cancelled, and the call count stops being decided.
                super().shutdown(wait=wait, cancel_futures=cancel_futures)
                if cancel_futures:
                    cancelled.set()

        def gated(path: str, **_kw) -> list:
            with lock:
                calls.append(int(path.rstrip("/").split("/")[-2]))
                first = len(calls) == 1
            if first:
                raise ProviderSetupError(
                    "glab is not authenticated for gitlab.example.com", reason="not_authenticated"
                )
            # Bounded: a fetcher that never cancels fails this test, not the suite. The
            # verdict is recorded rather than asserted here because an exception in a
            # worker whose result nobody reads would be swallowed.
            if not cancelled.wait(timeout=10):
                timed_out.append(path)
            return []

        workers = gitlab_queries._LINKS_WORKERS
        total = 10 * workers
        issues = [{"number": n, "title": "t", "state": "open"} for n in range(1, total + 1)]
        with mock.patch.object(gitlab_queries, "ThreadPoolExecutor", Recording):
            with self.assertRaises(ProviderSetupError):
                _edges(OWNER, REPO, issues, run_api=gated)
        self.assertEqual(timed_out, [])
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(futures), total)
        unstarted = sum(1 for f in futures if f.cancelled())
        self.assertLessEqual(len(calls), workers + 1)
        self.assertEqual(len(calls) + unstarted, total)


class TestCrossProjectLinksAreDropped(unittest.TestCase):
    """A link row naming an issue in another project must not become an edge in
    this project's number space; a row with no references is dropped too."""

    def test_foreign_and_unreferenced_rows_produce_no_edge(self):
        foreign = dict(_link(7, "blocks"), references={"relative": "other/proj#7"})
        unreferenced = {"iid": 8, "link_type": "blocks", "state": "opened", "title": "t"}
        local = _link(9, "blocks")
        run_api = _make_run_api({1: [foreign, unreferenced, local]})
        issues = [{"number": 1, "title": "a", "state": "open"}]
        edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(edges, [{"blocked": 9, "blocker": 1, "source": "native"}])
        self.assertNotIn("7", nodes)
        self.assertNotIn("8", nodes)


class TestEmptyLinks(unittest.TestCase):
    """An issue with no links must produce no edges and the issue still appears
    in nodes (seeded from open_issues)."""

    def test_no_links_means_no_edges(self):
        run_api = _make_run_api({10: []})
        issues = [{"number": 10, "title": "a", "state": "open"}]
        edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(edges, [])
        self.assertIn("10", nodes)


class TestProductionSeam(unittest.TestCase):
    """Without ``run_api`` the fetcher goes through the client's PUBLIC read."""

    def test_production_path_uses_list_issue_links(self):
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client

        calls: list[tuple] = []

        def fake_links(owner, repo, iid, *, host, timeout):
            calls.append((owner, repo, iid, host, timeout))
            return [_link(2, "blocks", title="two")]

        issues = [{"number": 1, "title": "one", "state": "open"}]
        with mock.patch.object(gitlab_client, "list_issue_links", side_effect=fake_links):
            edges, nodes = gitlab_queries.fetch_dependency_edges(
                OWNER, REPO, issues, host="gitlab.example.com", timeout=3.0
            )
        self.assertEqual(calls, [(OWNER, REPO, 1, "gitlab.example.com", 3.0)])
        self.assertEqual(edges, [{"blocked": 2, "blocker": 1, "source": "native"}])
        self.assertIn("2", nodes)


class TestCallCap(unittest.TestCase):
    """Exactly the first ``_MAX_LINKS_CALLS`` issues are fetched, and the overflow
    is counted out loud so a truncated graph never reads as a complete one."""

    def test_exactly_the_first_cap_issues_are_fetched_and_the_rest_counted(self):
        cap = gitlab_queries._MAX_LINKS_CALLS
        calls: list[int] = []

        def counting_api(path: str, **_kw) -> list:
            iid_str = path.rstrip("/").split("/")[-2]
            calls.append(int(iid_str))
            return []

        numbers = list(range(1, cap + 50))
        issues = [{"number": n, "title": "t", "state": "open"} for n in numbers]
        with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
            _edges(OWNER, REPO, issues, run_api=counting_api)
        self.assertEqual(len(calls), cap)
        self.assertEqual(sorted(calls), numbers[:cap])
        self.assertEqual(len(captured.records), 1)
        self.assertIn("49 past _MAX_LINKS_CALLS", captured.output[0])

    def test_under_the_cap_nothing_is_said(self):
        issues = [{"number": n, "title": "t", "state": "open"} for n in range(1, 4)]
        with self.assertNoLogs("kirocrew.app.issue-radar", level="WARNING"):
            _edges(OWNER, REPO, issues, run_api=lambda path, **_kw: [])


class TestBoundedConcurrency(unittest.TestCase):
    """Exactly ``_LINKS_WORKERS`` calls run at once: no fewer, and no more."""

    def test_at_least_workers_calls_run_concurrently(self):
        import threading

        workers = gitlab_queries._LINKS_WORKERS
        # The barrier admits a wave only once ``workers`` calls are inside it at the
        # same time, so a pool narrower than that times out (BrokenBarrierError, a
        # bounded failure, not a hang). No sleep: the barrier IS the synchronisation.
        barrier = threading.Barrier(workers, timeout=10)

        def gated_api(path: str, **_kw) -> list:
            barrier.wait()
            return []

        issues = [{"number": n, "title": "t", "state": "open"} for n in range(1, 2 * workers + 1)]
        _edges(OWNER, REPO, issues, run_api=gated_api)
        self.assertFalse(barrier.broken)

    def test_the_pool_is_never_wider_than_workers(self):
        # Whether a wider pool's extra threads increment a counter before the first
        # wave drains is a thread-start race, so the upper bound is pinned where it is
        # decided: the executor's width.
        from concurrent.futures import ThreadPoolExecutor

        widths: list[int] = []

        class Recording(ThreadPoolExecutor):
            def __init__(self, max_workers=None, **kw):
                widths.append(max_workers)
                super().__init__(max_workers=max_workers, **kw)

        issues = [{"number": 1, "title": "t", "state": "open"}]
        with mock.patch.object(gitlab_queries, "ThreadPoolExecutor", Recording):
            _edges(OWNER, REPO, issues, run_api=lambda path, **_kw: [])
        self.assertEqual(widths, [gitlab_queries._LINKS_WORKERS])


# ── /deps route tests ─────────────────────────────────────────────────────────


def _req(query: str):
    return make_mocked_request("GET", f"/api/apps/issue-radar/deps?{query}")


async def _call(query: str):
    return await routes._handle_deps(_req(query))


def _body(response):
    return json.loads(response.body.decode("utf-8"))


GL_QUERY = f"owner={OWNER}&repo={REPO}&provider=gitlab&host={HOST}"
GH_QUERY = f"owner={OWNER}&repo={REPO}"
AZ_QUERY = f"owner={OWNER}&repo={REPO}&provider=azure&host=dev.azure.com"


class TestGitlabDepsRoute(unittest.TestCase):
    """The /deps route for a GitLab key goes through the real build path."""

    def test_gitlab_key_serves_real_edges(self):
        edges = [{"blocked": 10, "blocker": 5, "source": "native"}]
        nodes = {"5": {"kind": "issue", "state": "open", "title": "b"}}
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", side_effect=[None, None]),
            mock.patch.object(routes, "_load_open_issues_for_reco", return_value=[]),
            mock.patch.object(store, "write_deps_cache"),
            mock.patch.object(
                gitlab_queries,
                "fetch_dependency_edges",
                return_value=(edges, nodes),
            ) as fetch,
        ):
            res = asyncio.run(_call(GL_QUERY))
        self.assertEqual(res.status, 200)
        body = _body(res)
        self.assertEqual(body["provider"], "gitlab")
        self.assertEqual(body["edges"], edges)
        self.assertFalse(body["from_cache"])
        fetch.assert_called_once()

    def test_gitlab_cache_first_path(self):
        """A fresh GitLab cache is served without calling fetch_dependency_edges."""
        cached = {
            "edges": [{"blocked": 2, "blocker": 1, "source": "native"}],
            "nodes": {"1": {"kind": "issue", "state": "open", "title": "b"}},
            "fetched_at": time.time(),
        }
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", return_value=cached),
            mock.patch.object(gitlab_queries, "fetch_dependency_edges") as fetch,
        ):
            res = asyncio.run(_call(GL_QUERY))
        self.assertEqual(res.status, 200)
        body = _body(res)
        self.assertTrue(body["from_cache"])
        self.assertEqual(body["edges"], cached["edges"])
        fetch.assert_not_called()

    def test_gitlab_cache_is_scoped_to_provider_root(self):
        """write_deps_cache must be called with the GitLab provider-scoped root,
        not the GitHub legacy tree."""
        seen_root: list = []

        def _write(owner, repo, edges, nodes, *, root, fetched_at):
            seen_root.append(root)

        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", side_effect=[None, None]),
            mock.patch.object(routes, "_load_open_issues_for_reco", return_value=[]),
            mock.patch.object(store, "write_deps_cache", side_effect=_write),
            mock.patch.object(gitlab_queries, "fetch_dependency_edges", return_value=([], {})),
        ):
            asyncio.run(_call(GL_QUERY))

        self.assertEqual(len(seen_root), 1)
        root_path = str(seen_root[0])
        # Provider-scoped root must contain the provider name; must NOT be the
        # GitHub legacy root (which is the plain data_dir with no provider subtree).
        self.assertIn("gitlab", root_path)

    def test_gitlab_and_github_roots_are_distinct(self):
        """GitLab must use a different on-disk scope from GitHub."""
        gl_root = store.provider_root(provider="gitlab", host=HOST)
        gh_root = store.provider_root(provider="github", host="github.com")
        self.assertNotEqual(gl_root, gh_root)


class TestGithubPathUnchanged(unittest.TestCase):
    """The GitHub path is byte-identical after the dispatch change."""

    def test_github_key_calls_github_client(self):
        edges = [{"blocked": 10, "blocker": 5, "source": "inferred"}]
        nodes = {"5": {"kind": "issue", "state": "open", "title": "b"}}
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", side_effect=[None, None]),
            mock.patch.object(routes, "_load_open_issues_for_reco", return_value=[]),
            mock.patch.object(store, "read_pulls_cache", return_value=[]),
            mock.patch.object(store, "write_deps_cache"),
            mock.patch.object(
                gh, "fetch_dependency_edges", return_value=(edges, nodes)
            ) as gh_fetch,
            mock.patch.object(gitlab_queries, "fetch_dependency_edges") as gl_fetch,
        ):
            res = asyncio.run(_call(GH_QUERY))
        self.assertEqual(res.status, 200)
        gh_fetch.assert_called_once()
        gl_fetch.assert_not_called()
        body = _body(res)
        self.assertEqual(body["provider"], "github")
        self.assertEqual(body["edges"], edges)


class TestAzureStillEmpty(unittest.TestCase):
    """Azure DevOps must return an empty graph (no fetch, no error)."""

    def test_azure_key_returns_empty(self):
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", side_effect=[None, None]),
            mock.patch.object(store, "write_deps_cache"),
            mock.patch.object(gh, "fetch_dependency_edges") as gh_fetch,
            mock.patch.object(gitlab_queries, "fetch_dependency_edges") as gl_fetch,
        ):
            res = asyncio.run(_call(AZ_QUERY))
        self.assertEqual(res.status, 200)
        body = _body(res)
        self.assertEqual(body["provider"], "azure")
        self.assertEqual(body["edges"], [])
        self.assertEqual(body["nodes"], {})
        gh_fetch.assert_not_called()
        gl_fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
