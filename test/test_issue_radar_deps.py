"""Tests for Issue Radar's dependency edges + auto-unlock (issue #5187, M1).

Four levels, matching how the six existing signals and the other caches are
tested:

  * the ``deps-cache.json`` store — a stale schema reads as a MISS (so the route
    refetches with the current edge shape rather than serving a graph missing a
    field), and the native-wins dedup that ``_normalize_deps`` owns;
  * ``github_client.fetch_dependency_edges`` — that native and inferred edges are
    both emitted (the store collapses a duplicate pair, native winning), and that
    same-repo scoping drops a cross-repo cross-reference;
  * the ``/deps`` HANDLER — validation, the connected gate, cache-first with the
    TTL, the empty-repo answer, and the GitHub-only guard that gives a non-GitHub
    key an empty graph instead of an error;
  * the SEVENTH sweep signal ``SIG_DEP_UNBLOCKED`` — that it fires exactly ONCE
    when the last blocker closes, stays silent while a blocker remains open, and
    never fires without a real >0 → 0 transition (fingerprint stability).

Every test patches the ``gh`` layer or scopes the store to ``tmp_path``; no
subprocess is spawned and the real data home is never touched.
"""

import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import provider, routes, store

OWNER, REPO = "o", "r"


# ── store: schema guard + native-wins dedup ──────────────────────────────────


class DepsCacheStoreTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_round_trips(self):
        edges = [{"blocked": 10, "blocker": 5, "source": "native"}]
        nodes = {"5": {"kind": "issue", "state": "open", "title": "blocker"}}
        store.write_deps_cache(OWNER, REPO, edges, nodes, root=self.root)
        out = store.read_deps_cache(OWNER, REPO, self.root)
        self.assertIsNotNone(out)
        self.assertEqual(out["edges"], edges)
        self.assertEqual(out["nodes"]["5"]["title"], "blocker")
        self.assertGreater(out["fetched_at"], 0.0)

    def test_absent_cache_is_a_miss(self):
        self.assertIsNone(store.read_deps_cache(OWNER, REPO, self.root))

    def test_schema_mismatch_is_a_cache_miss(self):
        # A cache written under an older DEPS_CACHE_SCHEMA reads as None so the
        # route refetches with the current edge shape — same discipline as the
        # issue/pull caches.
        path = store.deps_cache_path(OWNER, REPO, self.root)
        path.write_text(
            json.dumps({"schema": store.DEPS_CACHE_SCHEMA + 1, "edges": [], "nodes": {}}),
            encoding="utf-8",
        )
        self.assertIsNone(store.read_deps_cache(OWNER, REPO, self.root))

    def test_unstamped_and_corrupt_files_are_misses(self):
        path = store.deps_cache_path(OWNER, REPO, self.root)
        path.write_text('{"edges": [], "nodes": {}}', encoding="utf-8")  # no schema
        self.assertIsNone(store.read_deps_cache(OWNER, REPO, self.root))
        path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(store.read_deps_cache(OWNER, REPO, self.root))

    def test_native_wins_over_an_inferred_duplicate(self):
        # Same (blocked, blocker) pair from both sources: the native edge must be
        # the one kept, regardless of the order they were appended.
        edges = [
            {"blocked": 10, "blocker": 5, "source": "inferred"},
            {"blocked": 10, "blocker": 5, "source": "native"},
        ]
        store.write_deps_cache(OWNER, REPO, edges, {}, root=self.root)
        out = store.read_deps_cache(OWNER, REPO, self.root)
        self.assertEqual(out["edges"], [{"blocked": 10, "blocker": 5, "source": "native"}])

        # And the reverse append order collapses to the same native edge.
        store.write_deps_cache(OWNER, REPO, list(reversed(edges)), {}, root=self.root)
        out = store.read_deps_cache(OWNER, REPO, self.root)
        self.assertEqual(out["edges"], [{"blocked": 10, "blocker": 5, "source": "native"}])

    def test_self_and_malformed_edges_are_dropped(self):
        edges = [
            {"blocked": 7, "blocker": 7, "source": "native"},  # self-edge
            {"blocked": 0, "blocker": 5, "source": "native"},  # non-positive
            {"blocked": 8, "source": "native"},  # missing blocker
            {"blocked": 8, "blocker": 9, "source": "native"},  # the only valid one
        ]
        store.write_deps_cache(OWNER, REPO, edges, {}, root=self.root)
        out = store.read_deps_cache(OWNER, REPO, self.root)
        self.assertEqual(out["edges"], [{"blocked": 8, "blocker": 9, "source": "native"}])

    def test_unknown_source_defaults_to_inferred(self):
        store.write_deps_cache(
            OWNER, REPO, [{"blocked": 2, "blocker": 1, "source": "bogus"}], {}, root=self.root
        )
        out = store.read_deps_cache(OWNER, REPO, self.root)
        self.assertEqual(out["edges"][0]["source"], "inferred")


# ── github_client.fetch_dependency_edges ─────────────────────────────────────


_NO_BATCH = mock.patch.object(gh, "_batch_dependency_graph", return_value=None)


class FetchDependencyEdgesTest(unittest.TestCase):
    """native + inferred merge, and same-repo scoping. The gh layer is stubbed;
    the batched GraphQL prefetch is stubbed to None so these exercise the
    per-issue fallback path (the batch has its own tests below)."""

    def setUp(self):
        self._nb = _NO_BATCH
        self._nb.start()
        self.addCleanup(self._nb.stop)

    def test_native_and_inferred_edges_are_both_emitted(self):
        open_issues = [{"number": 10, "title": "dependent", "state": "open"}]
        hints = {10: {"kind": "issue", "state": "open", "title": "dependent"}}
        native = [{"number": 5, "title": "native blocker", "state": "closed", "is_pr": False}]
        timeline = [
            {
                "kind": "cross-referenced",
                "source": {
                    "number": 6,
                    "title": "ref blocker",
                    "state": "open",
                    "url": f"https://github.com/{OWNER}/{REPO}/pull/6",
                    "is_pr": True,
                },
            }
        ]
        with (
            mock.patch.object(gh, "list_issue_blocked_by", return_value=native),
            mock.patch.object(gh, "list_issue_timeline", return_value=timeline),
        ):
            edges, nodes = gh.fetch_dependency_edges(OWNER, REPO, open_issues, hints)

        self.assertIn({"blocked": 10, "blocker": 5, "source": "native"}, edges)
        self.assertIn({"blocked": 10, "blocker": 6, "source": "inferred"}, edges)
        # Nodes were seeded from the returned rows without any extra ref call.
        self.assertEqual(nodes["5"]["state"], "closed")
        self.assertEqual(nodes["6"]["kind"], "pull")

    def test_native_and_inferred_duplicate_collapses_to_native_after_store(self):
        # The fetcher may emit BOTH for the same pair; the store's normalize is the
        # single dedup point and native wins.
        open_issues = [{"number": 10, "title": "d", "state": "open"}]
        native = [{"number": 5, "title": "b", "state": "open", "is_pr": False}]
        timeline = [
            {
                "kind": "cross-referenced",
                "source": {
                    "number": 5,
                    "title": "b",
                    "state": "open",
                    "url": f"https://github.com/{OWNER}/{REPO}/issues/5",
                    "is_pr": False,
                },
            }
        ]
        with (
            mock.patch.object(gh, "list_issue_blocked_by", return_value=native),
            mock.patch.object(gh, "list_issue_timeline", return_value=timeline),
        ):
            edges, nodes = gh.fetch_dependency_edges(OWNER, REPO, open_issues, {})
        deduped, _ = store._normalize_deps(edges, nodes)
        self.assertEqual(deduped, [{"blocked": 10, "blocker": 5, "source": "native"}])

    def test_cross_repo_cross_reference_is_dropped(self):
        open_issues = [{"number": 10, "title": "d", "state": "open"}]
        timeline = [
            {
                "kind": "cross-referenced",
                "source": {
                    "number": 99,
                    "title": "other repo",
                    "state": "open",
                    "url": "https://github.com/other/elsewhere/issues/99",
                    "is_pr": False,
                },
            }
        ]
        with (
            mock.patch.object(gh, "list_issue_blocked_by", return_value=[]),
            mock.patch.object(gh, "list_issue_timeline", return_value=timeline),
        ):
            edges, _ = gh.fetch_dependency_edges(OWNER, REPO, open_issues, {})
        self.assertEqual(edges, [])

    def test_absent_dependencies_endpoint_yields_zero_native_edges(self):
        # A 404/410 from the young dependencies API is tolerated as "no native
        # edges" so the inferred graph still builds.
        proc = mock.Mock(returncode=1, stdout="", stderr="HTTP 404: Not Found")
        with mock.patch.object(gh, "_gh_run", return_value=proc):
            self.assertEqual(gh.list_issue_blocked_by(OWNER, REPO, 10), [])

    def test_a_missing_node_falls_back_to_one_ref_summary(self):
        open_issues = [{"number": 10, "title": "d", "state": "open"}]
        # A blocker seeded from a row that already carries state/title needs NO ref
        # call; a number referenced by an edge but never seeded triggers exactly
        # one get_ref_summary.
        native = [{"number": 5, "title": "seeded", "state": "closed", "is_pr": False}]
        with (
            mock.patch.object(gh, "list_issue_blocked_by", return_value=native),
            mock.patch.object(gh, "list_issue_timeline", return_value=[]),
            mock.patch.object(gh, "get_ref_summary") as ref,
        ):
            edges, nodes = gh.fetch_dependency_edges(OWNER, REPO, open_issues, {})
        self.assertIn({"blocked": 10, "blocker": 5, "source": "native"}, edges)
        self.assertEqual(ref.call_count, 0)  # #5 was seeded from the native row
        self.assertEqual(nodes["5"]["state"], "closed")


# ── github_client._batch_dependency_graph ────────────────────────────────────


def _gql_page(nodes, has_next=False, cursor=None):
    payload = {
        "data": {
            "repository": {
                "issues": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": nodes,
                }
            }
        }
    }
    return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")


class BatchDependencyGraphTest(unittest.TestCase):
    """The batched GraphQL walk: pagination, shape mapping, and the None
    fallback contract that routes fetch_dependency_edges to per-issue reads."""

    def test_two_pages_are_walked_and_mapped(self):
        page1 = _gql_page(
            [
                {
                    "number": 10,
                    "title": "dependent",
                    "state": "OPEN",
                    "blockedBy": {"nodes": [{"number": 5, "title": "blk", "state": "CLOSED"}]},
                    "timelineItems": {
                        "nodes": [
                            {
                                "source": {
                                    "number": 6,
                                    "title": "ref",
                                    "state": "MERGED",
                                    "merged": True,
                                    "url": f"https://github.com/{OWNER}/{REPO}/pull/6",
                                    "repository": {"nameWithOwner": f"{OWNER}/{REPO}"},
                                }
                            }
                        ]
                    },
                }
            ],
            has_next=True,
            cursor="C1",
        )
        page2 = _gql_page(
            [
                {
                    "number": 11,
                    "title": "loner",
                    "state": "OPEN",
                    "blockedBy": {"nodes": []},
                    "timelineItems": {"nodes": []},
                }
            ]
        )
        with mock.patch.object(gh, "_gh_run", side_effect=[page1, page2]) as run:
            out = gh._batch_dependency_graph(OWNER, REPO)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(sorted(out), [10, 11])
        self.assertEqual(out[10]["native"][0]["number"], 5)
        self.assertEqual(out[10]["native"][0]["state"], "closed")
        # A merged PR source carries the merged sentinel so _dep_node_state
        # resolves it to "merged", and is_pr is derived from the PR fragment.
        src = out[10]["refs"][0]["source"]
        self.assertTrue(src["is_pr"])
        self.assertTrue(src["merged_at"])

    def test_cross_repo_source_is_dropped_at_batch_time(self):
        page = _gql_page(
            [
                {
                    "number": 10,
                    "title": "d",
                    "state": "OPEN",
                    "blockedBy": {"nodes": []},
                    "timelineItems": {
                        "nodes": [
                            {
                                "source": {
                                    "number": 99,
                                    "title": "other",
                                    "state": "OPEN",
                                    "url": "https://github.com/other/elsewhere/issues/99",
                                    "repository": {"nameWithOwner": "other/elsewhere"},
                                }
                            }
                        ]
                    },
                }
            ]
        )
        with mock.patch.object(gh, "_gh_run", return_value=page):
            out = gh._batch_dependency_graph(OWNER, REPO)
        self.assertEqual(out[10]["refs"], [])

    def test_a_failed_graphql_call_returns_none(self):
        proc = mock.Mock(returncode=1, stdout="", stderr="GraphQL: not available")
        with mock.patch.object(gh, "_gh_run", return_value=proc):
            self.assertIsNone(gh._batch_dependency_graph(OWNER, REPO))

    def test_fetch_consumes_the_batch_without_per_issue_reads(self):
        open_issues = [{"number": 10, "title": "dependent", "state": "open"}]
        batch = {
            10: {
                "row": {
                    "number": 10,
                    "title": "dependent",
                    "state": "open",
                    "is_pr": False,
                    "merged_at": None,
                },
                "native": [
                    {
                        "number": 5,
                        "title": "blk",
                        "state": "closed",
                        "is_pr": False,
                        "merged_at": None,
                    }
                ],
                "refs": [
                    {
                        "kind": "cross-referenced",
                        "source": {
                            "number": 6,
                            "title": "ref",
                            "state": "open",
                            "is_pr": True,
                            "merged_at": None,
                            "url": f"https://github.com/{OWNER}/{REPO}/pull/6",
                        },
                    }
                ],
            }
        }
        boom = mock.Mock(side_effect=AssertionError("per-issue path must not run"))
        with (
            mock.patch.object(gh, "_batch_dependency_graph", return_value=batch),
            mock.patch.object(gh, "list_issue_blocked_by", boom),
            mock.patch.object(gh, "list_issue_timeline", boom),
        ):
            edges, nodes = gh.fetch_dependency_edges(OWNER, REPO, open_issues, {})
        self.assertIn({"blocked": 10, "blocker": 5, "source": "native"}, edges)
        self.assertIn({"blocked": 10, "blocker": 6, "source": "inferred"}, edges)
        self.assertEqual(nodes["6"]["kind"], "pull")


# ── /deps route ──────────────────────────────────────────────────────────────


def _get(query: str):
    return make_mocked_request("GET", f"/api/apps/issue-radar/deps?{query}")


async def _call(query: str):
    return await routes._handle_deps(_get(query))


def _body(response):
    return json.loads(response.body.decode("utf-8"))


class DepsHandlerTest(unittest.TestCase):
    def test_missing_params_are_rejected(self):
        for query in ("", "owner=o", "repo=r"):
            res = asyncio.run(_call(query))
            self.assertEqual(res.status, 400, query)

    def test_an_unconnected_repo_is_refused(self):
        with (
            mock.patch.object(store, "is_repo_connected", return_value=False),
            mock.patch.object(gh, "fetch_dependency_edges") as fetch,
        ):
            res = asyncio.run(_call("owner=o&repo=r"))
        self.assertEqual(res.status, 404)
        fetch.assert_not_called()

    def test_serves_a_fresh_cache_without_calling_gh(self):
        cached = {
            "edges": [{"blocked": 10, "blocker": 5, "source": "native"}],
            "nodes": {"5": {"kind": "issue", "state": "open", "title": "b"}},
            "fetched_at": time.time(),
        }
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", return_value=cached),
            mock.patch.object(gh, "fetch_dependency_edges") as fetch,
        ):
            res = asyncio.run(_call("owner=o&repo=r"))
        self.assertEqual(res.status, 200)
        body = _body(res)
        self.assertTrue(body["from_cache"])
        self.assertEqual(body["edges"], cached["edges"])
        fetch.assert_not_called()

    def test_a_stale_cache_is_served_immediately_and_revalidated_behind(self):
        # Serve-stale-revalidate-behind (issue #5612): a stale cache is returned
        # RIGHT NOW with stale=true and the ~11s rebuild is moved OFF the request
        # path, so the handler must NOT await fetch_dependency_edges inline.
        stale = {"edges": [], "nodes": {}, "fetched_at": time.time() - 100000}
        req = _get("owner=o&repo=r")
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", return_value=stale),
            mock.patch.object(routes, "_schedule_deps_refresh") as sched,
            mock.patch.object(gh, "fetch_dependency_edges") as fetch,
        ):
            res = asyncio.run(routes._handle_deps(req))
        self.assertEqual(res.status, 200)
        body = _body(res)
        self.assertTrue(body["from_cache"])
        # Serve-stale does NOT announce itself: the response shape is unchanged,
        # so nothing here reports whether the graph was aged. Pinned so re-adding
        # such a field is a deliberate act with a named consumer.
        self.assertNotIn("stale", body)
        self.assertEqual(body["edges"], stale["edges"])
        # The request returned without paying for the rebuild.
        fetch.assert_not_called()
        # A background revalidation was scheduled for this repo.
        sched.assert_called_once()

    def test_a_fresh_cache_is_not_stale_and_schedules_no_refresh(self):
        cached = {
            "edges": [{"blocked": 10, "blocker": 5, "source": "native"}],
            "nodes": {},
            "fetched_at": time.time(),
        }
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", return_value=cached),
            mock.patch.object(routes, "_schedule_deps_refresh") as sched,
            mock.patch.object(gh, "fetch_dependency_edges") as fetch,
        ):
            res = asyncio.run(_call("owner=o&repo=r"))
        self.assertEqual(res.status, 200)
        self.assertTrue(_body(res)["from_cache"])
        sched.assert_not_called()
        fetch.assert_not_called()

    def test_empty_repo_returns_an_empty_graph(self):
        # An issues-cache MISS is unknown, not empty: the handler now resolves it
        # through the cache-first loader before building. A repo with a KNOWN
        # empty open-issue list still yields (and persists) an empty graph.
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", side_effect=[None, None]),
            mock.patch.object(routes, "_load_open_issues_for_reco", return_value=[]),
            mock.patch.object(store, "read_pulls_cache", return_value=None),
            mock.patch.object(store, "write_deps_cache"),
            mock.patch.object(gh, "fetch_dependency_edges", return_value=([], {})) as fetch,
        ):
            res = asyncio.run(_call("owner=o&repo=r"))
        self.assertEqual(res.status, 200)
        body = _body(res)
        self.assertEqual(body["edges"], [])
        self.assertEqual(body["nodes"], {})
        # An empty repo still fetches (with an empty open-issue list) rather than
        # erroring.
        fetch.assert_called_once()

    def test_refresh_bypasses_the_cache(self):
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(
                store,
                "read_deps_cache",
                side_effect=[{"edges": [], "nodes": {}, "fetched_at": time.time()}],
            ),
            mock.patch.object(store, "read_issues_cache", return_value=[]),
            mock.patch.object(store, "read_pulls_cache", return_value=[]),
            mock.patch.object(store, "write_deps_cache"),
            mock.patch.object(gh, "fetch_dependency_edges", return_value=([], {})) as fetch,
        ):
            res = asyncio.run(_call("owner=o&repo=r&refresh=1"))
        self.assertEqual(res.status, 200)
        # refresh=1 skipped the freshness read entirely and went straight to fetch.
        fetch.assert_called_once()

    def test_a_gh_failure_maps_to_502(self):
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", return_value=None),
            mock.patch.object(store, "read_issues_cache", return_value=[]),
            mock.patch.object(store, "read_pulls_cache", return_value=[]),
            mock.patch.object(gh, "fetch_dependency_edges", side_effect=gh.GhCliError("boom")),
        ):
            res = asyncio.run(_call("owner=o&repo=r"))
        self.assertEqual(res.status, 502)

    def test_a_non_github_key_gets_an_empty_graph_without_a_fetch(self):
        # M1 is GitHub-native. A GitLab key returns an empty graph rather than an
        # error, so the frontend can call /deps uniformly.
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(gh, "fetch_dependency_edges") as fetch,
        ):
            res = asyncio.run(_call("owner=o&repo=r&provider=gitlab&host=gitlab.com"))
        self.assertEqual(res.status, 200)
        body = _body(res)
        self.assertEqual(body["provider"], "gitlab")
        self.assertEqual(body["edges"], [])
        fetch.assert_not_called()


# ── /deps serve-stale background revalidation (issue #5612) ───────────────────


def _gh_key():
    return provider.RepoKey(provider="github", host="github.com", owner=OWNER, repo=REPO)


class DepsBackgroundRefreshTest(unittest.IsolatedAsyncioTestCase):
    """The serve-stale-revalidate-behind machinery: coalescing to one in-flight
    refresh per repo, a failing refresh leaving the prior cache intact, refresh=1
    still rebuilding synchronously, and clean cancellation on shutdown."""

    async def _drain(self, app):
        """Await every registered background refresh task to completion."""
        tasks = list(app.get(routes._DEPS_REFRESH_TASKS_APP_KEY, {}).values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_scope_and_fetch_failures_keep_distinct_error_codes(self):
        # Both failures surface as GhCliError, and before ``_rebuild_deps`` owned
        # the whole build they were told apart by WHICH try block caught them.
        # Now the scope failure carries its own type, so the two 502 codes stay
        # distinguishable -- pinned here because a message-pattern classifier
        # would silently collapse them (a scope error reading "gh api ... failed"
        # mentions neither "issue" nor "scope").
        req = make_mocked_request("GET", "/api/apps/issue-radar/deps?owner=o&repo=r")

        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", return_value=None),
            mock.patch.object(
                routes,
                "_load_open_issues_for_reco",
                side_effect=gh.GhCliError("gh api graphql failed (exit 1)"),
            ),
        ):
            res = await routes._handle_deps(req)
        self.assertEqual(res.status, 502)
        self.assertEqual(
            json.loads(res.body.decode("utf-8"))["code"], "deps_issue_scope_unavailable"
        )

        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", return_value=None),
            mock.patch.object(routes, "_load_open_issues_for_reco", return_value=[]),
            mock.patch.object(store, "read_pulls_cache", return_value=[]),
            mock.patch.object(
                gh, "fetch_dependency_edges", side_effect=gh.GhCliError("edges failed")
            ),
        ):
            res = await routes._handle_deps(req)
        self.assertEqual(res.status, 502)
        self.assertEqual(json.loads(res.body.decode("utf-8"))["code"], "deps_fetch_failed")

    async def test_background_refresh_is_coalesced_to_one_per_repo(self):
        app = web.Application()
        key = _gh_key()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def _slow_rebuild(_app, _key):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"edges": [], "nodes": {}}

        with mock.patch.object(routes, "_rebuild_deps", side_effect=_slow_rebuild):
            routes._schedule_deps_refresh(app, key)
            await started.wait()
            # Second and third stale callers land while the first is in flight:
            # they must NOT spawn another rebuild.
            routes._schedule_deps_refresh(app, key)
            routes._schedule_deps_refresh(app, key)
            self.assertEqual(len(app[routes._DEPS_REFRESH_TASKS_APP_KEY]), 1)
            release.set()
            await self._drain(app)

        self.assertEqual(calls, 1)
        # The slot is freed once the task finishes, so a later visit can refresh.
        self.assertEqual(len(app[routes._DEPS_REFRESH_TASKS_APP_KEY]), 0)

    async def test_a_slow_rebuild_cannot_overwrite_a_newer_one(self):
        # GPT round 1: a stale GET starts background rebuild A; an edge changes;
        # refresh=1 starts synchronous rebuild B. B writes the fresh graph, then
        # the slower A lands on top with its OLDER edges -- and because
        # write_deps_cache stamps fetched_at at WRITE time, those older edges are
        # then treated as fresh for a full TTL. The per-repo rebuild mutex makes
        # the LAST write the LAST fetch, which is what the freshness stamp claims.
        app = web.Application()
        key = _gh_key()
        old = [{"blocked": 10, "blocker": 5, "source": "native"}]
        new = [{"blocked": 10, "blocker": 6, "source": "native"}]
        fetches = 0
        writes: list = []

        def _fetch(*_a, **_k):
            nonlocal fetches
            n = fetches
            fetches += 1
            if n == 0:
                time.sleep(0.30)  # the slow background rebuild, holding the lock
                return (old, {})
            return (new, {})

        def _write(*a, **_k):
            writes.append(a[2])

        with (
            mock.patch.object(routes, "_load_open_issues_for_reco", return_value=[]),
            mock.patch.object(store, "read_pulls_cache", return_value=[]),
            mock.patch.object(store, "write_deps_cache", side_effect=_write),
            mock.patch.object(store, "read_deps_cache", return_value=None),
            mock.patch.object(gh, "fetch_dependency_edges", side_effect=_fetch),
        ):
            routes._schedule_deps_refresh(app, key)  # A (background)
            await asyncio.sleep(0.05)  # let A take the lock and enter its fetch
            await routes._rebuild_deps(app, key)  # B (the refresh=1 path)
            await self._drain(app)

        # Both rebuilds ran, and the surviving graph is the one fetched LAST.
        self.assertEqual(fetches, 2)
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[-1], new)

    async def test_a_failing_background_refresh_keeps_the_prior_cache(self):
        app = web.Application()
        key = _gh_key()
        with (
            mock.patch.object(
                routes, "_rebuild_deps", side_effect=gh.GhCliError("boom")
            ) as rebuild,
            mock.patch.object(store, "write_deps_cache") as write,
        ):
            routes._schedule_deps_refresh(app, key)
            await self._drain(app)
        # The rebuild ran and raised, but nothing crashed and the failure never
        # reached write_deps_cache — the previous good cache is untouched.
        rebuild.assert_called_once()
        write.assert_not_called()
        self.assertEqual(len(app[routes._DEPS_REFRESH_TASKS_APP_KEY]), 0)

    async def test_refresh_query_still_rebuilds_synchronously(self):
        # refresh=1 must return FRESH data inline (not serve-stale), even when a
        # fresh cache exists — a user-initiated refresh pays for the rebuild.
        fresh_edges = [{"blocked": 10, "blocker": 5, "source": "native"}]
        req = make_mocked_request("GET", "/api/apps/issue-radar/deps?owner=o&repo=r&refresh=1")
        stored = {"edges": fresh_edges, "nodes": {}, "fetched_at": time.time()}
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(routes, "_load_open_issues_for_reco", return_value=[]),
            mock.patch.object(store, "read_pulls_cache", return_value=[]),
            mock.patch.object(store, "write_deps_cache") as write,
            mock.patch.object(store, "read_deps_cache", return_value=stored),
            mock.patch.object(
                gh, "fetch_dependency_edges", return_value=(fresh_edges, {})
            ) as fetch,
            mock.patch.object(routes, "_schedule_deps_refresh") as sched,
        ):
            res = await routes._handle_deps(req)
        self.assertEqual(res.status, 200)
        body = json.loads(res.body.decode("utf-8"))
        self.assertFalse(body["from_cache"])
        self.assertEqual(body["edges"], fresh_edges)
        fetch.assert_called_once()
        write.assert_called_once()
        # A forced sync rebuild does not also queue a background one.
        sched.assert_not_called()

    async def test_an_absent_cache_still_blocks_on_a_synchronous_build(self):
        req = make_mocked_request("GET", "/api/apps/issue-radar/deps?owner=o&repo=r")
        stored = {"edges": [], "nodes": {}, "fetched_at": time.time()}
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", side_effect=[None, stored]),
            mock.patch.object(routes, "_load_open_issues_for_reco", return_value=[]),
            mock.patch.object(store, "read_pulls_cache", return_value=[]),
            mock.patch.object(store, "write_deps_cache") as write,
            mock.patch.object(gh, "fetch_dependency_edges", return_value=([], {})) as fetch,
            mock.patch.object(routes, "_schedule_deps_refresh") as sched,
        ):
            res = await routes._handle_deps(req)
        self.assertEqual(res.status, 200)
        body = json.loads(res.body.decode("utf-8"))
        self.assertFalse(body["from_cache"])
        # A never-synced repo builds inline (blocks) and does not serve-stale.
        fetch.assert_called_once()
        write.assert_called_once()
        sched.assert_not_called()

    async def test_shutdown_cancels_outstanding_refreshes(self):
        app = web.Application()
        key = _gh_key()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def _hang(_app, _key):
            started.set()
            try:
                await asyncio.Event().wait()  # never completes on its own
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with mock.patch.object(routes, "_rebuild_deps", side_effect=_hang):
            routes._schedule_deps_refresh(app, key)
            await started.wait()
            self.assertEqual(len(app[routes._DEPS_REFRESH_TASKS_APP_KEY]), 1)
            await routes._stop_deps_refreshes(app)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(app[routes._DEPS_REFRESH_TASKS_APP_KEY]), 0)


# ── SIG_DEP_UNBLOCKED (detect + count + fingerprint stability) ────────────────


class DepUnblockDetectTest(unittest.TestCase):
    """The transition rule in ``detect_unblocks`` for the seventh signal."""

    BASE = {
        "issue_comments": 0,
        "checks": None,
        "check_counts": None,
        "review_decision": "",
        "conflicted": False,
        "merged": False,
        "pr_comments": 0,
        "open_blockers": 1,
    }

    def test_fires_once_on_the_last_blocker_closing(self):
        prev = {**self.BASE, "open_blockers": 1}
        cur = {**self.BASE, "open_blockers": 0}
        self.assertEqual(cr.detect_unblocks(prev, cur), [cr.SIG_DEP_UNBLOCKED])

    def test_no_signal_while_a_blocker_remains(self):
        prev = {**self.BASE, "open_blockers": 2}
        cur = {**self.BASE, "open_blockers": 1}
        self.assertEqual(cr.detect_unblocks(prev, cur), [])

    def test_no_signal_without_a_transition(self):
        # Already unblocked in both readings (an item with no blockers) → nothing.
        prev = {**self.BASE, "open_blockers": 0}
        cur = {**self.BASE, "open_blockers": 0}
        self.assertEqual(cr.detect_unblocks(prev, cur), [])

    def test_unknown_count_is_not_a_transition(self):
        # None means the deps cache could not be read; None-vs-known must not read
        # as unblocked (same guard as unknown CI).
        self.assertEqual(
            cr.detect_unblocks(
                {**self.BASE, "open_blockers": None}, {**self.BASE, "open_blockers": 0}
            ),
            [],
        )
        self.assertEqual(
            cr.detect_unblocks(
                {**self.BASE, "open_blockers": 1}, {**self.BASE, "open_blockers": None}
            ),
            [],
        )

    def test_first_observation_reports_nothing(self):
        self.assertEqual(cr.detect_unblocks(None, {**self.BASE, "open_blockers": 0}), [])

    def test_the_signal_is_in_the_table(self):
        self.assertIn(cr.SIG_DEP_UNBLOCKED, cr.UNBLOCK_SIGNALS)


class OpenBlockerCountTest(unittest.TestCase):
    """``_open_blocker_count`` reads the deps-cache graph for one item."""

    GRAPH = {
        "edges": [
            {"blocked": 10, "blocker": 5, "source": "native"},
            {"blocked": 10, "blocker": 6, "source": "native"},
            {"blocked": 20, "blocker": 7, "source": "inferred"},
        ],
        "nodes": {
            "5": {"kind": "issue", "state": "closed", "title": ""},
            "6": {"kind": "pull", "state": "merged", "title": ""},
            "7": {"kind": "issue", "state": "open", "title": ""},
        },
    }

    def test_all_blockers_closed_or_merged_is_zero(self):
        self.assertEqual(cr._open_blocker_count(self.GRAPH, 10), 0)

    def test_an_open_blocker_counts(self):
        self.assertEqual(cr._open_blocker_count(self.GRAPH, 20), 1)

    def test_no_blockers_is_zero_not_unknown(self):
        self.assertEqual(cr._open_blocker_count(self.GRAPH, 999), 0)

    def test_no_graph_is_unknown(self):
        self.assertIsNone(cr._open_blocker_count(None, 10))

    def test_a_blocker_with_no_node_is_treated_as_open(self):
        graph = {"edges": [{"blocked": 1, "blocker": 2, "source": "native"}], "nodes": {}}
        self.assertEqual(cr._open_blocker_count(graph, 1), 1)


class FingerprintOpenBlockersTest(unittest.TestCase):
    """``fingerprint_item`` records the blocker count from the deps graph."""

    def setUp(self):
        self.key = provider.key_from_parts(OWNER, REPO)

    def _fp(self, item, deps):
        client = mock.Mock()
        client.get_issue_detail.return_value = {"comments": 0, "state": "open"}
        with (
            mock.patch.object(cr.provider, "client_for", return_value=client),
            mock.patch.object(cr.provider, "call_kwargs", return_value={}),
        ):
            return cr.fingerprint_item(self.key, item, {}, None, deps)

    def test_records_open_blocker_count(self):
        graph = {
            "edges": [{"blocked": 10, "blocker": 5, "source": "native"}],
            "nodes": {"5": {"kind": "issue", "state": "open", "title": ""}},
        }
        fp = self._fp({"number": 10, "phase": "awaiting-reply"}, graph)
        self.assertEqual(fp["open_blockers"], 1)

    def test_no_deps_cache_records_unknown(self):
        fp = self._fp({"number": 10, "phase": "awaiting-reply"}, None)
        self.assertIsNone(fp["open_blockers"])


class _FakeSlot:
    """Stand-in for a chat slot; records prompts, never runs a real turn."""

    def __init__(self, key="crew-x", agent="", model="", workspace=""):
        self.key = key
        self.title = ""
        self._titled = False
        self._trust = False
        self._trust_scope = ""
        self.agent = agent
        self.model = model
        self.workspace = workspace
        self.messages: list = []
        self.running = False

    def append(self, role, content, cls="", **kw):
        self.messages.append({"role": role, "content": content, "cls": cls})

    def enqueue_or_run_prompt(self, prompt, run_chat_coro, state):
        return True


class _FakeState:
    """Minimal DashboardState — just the calls the sweep/watchdog reach."""

    def __init__(self):
        self.slots: dict[str, _FakeSlot] = {}
        self.created: list = []
        self.pushes = 0
        self.capped: list = []

    def get_slot(self, key):
        return self.slots.get(key)

    async def run_background_turn(self, slot, coro):
        self.capped.append(str(getattr(slot, "key", "")))
        return await coro

    def get_or_create_slot(self, name="", agent="", workspace="default", model="", app="", **kw):
        self.created.append(name)
        slot = self.slots.get(name)
        if slot is None:
            slot = _FakeSlot(name, agent=agent, model=model, workspace=workspace)
            self.slots[name] = slot
        return slot

    def push_slots_update(self):
        self.pushes += 1

    def push_slot_title(self, key, title):
        self.pushes += 1


class SweepDepUnblockTest(unittest.IsolatedAsyncioTestCase):
    """End-to-end through ``sweep_repo``: the last blocker closing wakes the crew
    exactly once, and a still-open blocker does not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.key = provider.key_from_parts(OWNER, REPO)

    def _client(self):
        client = mock.Mock()
        client.get_issue_detail.return_value = {"comments": 0, "state": "open"}
        client.enrich_pulls_by_number.return_value = []
        return client

    async def _sweep(self, client):
        app = {"state": _FakeState()}
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(cr.provider, "client_for", return_value=client),
            mock.patch.object(cr, "wake_crew", new=mock.AsyncMock(return_value=True)) as wake,
        ):
            woken = await cr.sweep_repo(app, self.key, self.root)
        return woken, wake

    def _write_graph(self, blocker_state):
        store.write_deps_cache(
            OWNER,
            REPO,
            [{"blocked": 2201, "blocker": 5, "source": "native"}],
            {"5": {"kind": "issue", "state": blocker_state, "title": "blocker"}},
            root=self.root,
        )

    async def test_fires_once_when_the_last_blocker_closes(self):
        crew = cs.create_crew(OWNER, REPO, {"name": "Andromeda", "unattended": True}, self.root)
        cs.upsert_work_item(OWNER, REPO, crew["id"], 2201, {"phase": "awaiting-reply"}, self.root)

        # Seed: the blocker is still open.
        self._write_graph("open")
        client = self._client()
        woken, wake = await self._sweep(client)
        self.assertEqual(woken, {})  # first observation seeds, no wake
        wake.assert_not_awaited()

        # Make the item due again, then close the blocker.
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)
        self._write_graph("closed")

        woken, wake = await self._sweep(client)
        self.assertEqual(woken, {crew["id"]: [cr.SIG_DEP_UNBLOCKED]})
        wake.assert_awaited_once()

        # A THIRD sweep with the blocker still closed must NOT re-fire.
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)
        woken, wake2 = await self._sweep(client)
        self.assertEqual(woken, {})
        wake2.assert_not_awaited()

    async def test_does_not_fire_while_a_blocker_remains_open(self):
        crew = cs.create_crew(OWNER, REPO, {"name": "Andromeda", "unattended": True}, self.root)
        cs.upsert_work_item(OWNER, REPO, crew["id"], 2201, {"phase": "awaiting-reply"}, self.root)
        # Two blockers; only one closes.
        store.write_deps_cache(
            OWNER,
            REPO,
            [
                {"blocked": 2201, "blocker": 5, "source": "native"},
                {"blocked": 2201, "blocker": 6, "source": "native"},
            ],
            {
                "5": {"kind": "issue", "state": "open", "title": ""},
                "6": {"kind": "issue", "state": "open", "title": ""},
            },
            root=self.root,
        )
        client = self._client()
        await self._sweep(client)
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)

        store.write_deps_cache(
            OWNER,
            REPO,
            [
                {"blocked": 2201, "blocker": 5, "source": "native"},
                {"blocked": 2201, "blocker": 6, "source": "native"},
            ],
            {
                "5": {"kind": "issue", "state": "closed", "title": ""},
                "6": {"kind": "issue", "state": "open", "title": ""},
            },
            root=self.root,
        )
        woken, wake = await self._sweep(client)
        self.assertEqual(woken, {})
        wake.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


# ── /deps cold-cache + sweep deps refresh ────────────────────────────────────


class DepsColdCacheTest(unittest.TestCase):
    """An issues-cache MISS resolves through the authoritative loader instead of
    persisting a wrong-empty graph (the unknown-vs-empty distinction)."""

    def test_a_cold_issues_cache_uses_the_authoritative_loader(self):
        rows = [{"number": 10, "title": "d", "state": "open"}]
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", side_effect=[None, None]),
            mock.patch.object(routes, "_load_open_issues_for_reco", return_value=rows) as load,
            mock.patch.object(store, "read_pulls_cache", return_value=None),
            mock.patch.object(store, "write_deps_cache") as write,
            mock.patch.object(gh, "fetch_dependency_edges", return_value=([], {})) as fetch,
        ):
            res = asyncio.run(_call("owner=o&repo=r"))
        self.assertEqual(res.status, 200)
        load.assert_called_once()
        self.assertEqual(fetch.call_args.args[2], rows)  # graph scoped to real rows
        write.assert_called_once()

    def test_a_failed_authoritative_load_is_a_502_and_persists_nothing(self):
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", return_value=None),
            mock.patch.object(
                routes,
                "_load_open_issues_for_reco",
                side_effect=gh.GhCliError("gh api failed"),
            ),
            mock.patch.object(store, "write_deps_cache") as write,
        ):
            res = asyncio.run(_call("owner=o&repo=r"))
        self.assertEqual(res.status, 502)
        write.assert_not_called()


class SweepDepsRefreshTest(unittest.TestCase):
    """_read_or_refresh_deps: fresh cache served as-is; stale GitHub cache
    refreshed in the sweep; a refresh failure keeps the stale graph."""

    def _key(self, prov="github"):
        return provider.RepoKey(owner="o", repo="r", provider=prov)

    def test_a_fresh_cache_is_served_without_a_fetch(self):
        fresh = {"edges": [], "nodes": {}, "fetched_at": time.time()}
        with (
            mock.patch.object(cr.store, "read_deps_cache", return_value=fresh),
            mock.patch.object(gh, "fetch_dependency_edges") as fetch,
        ):
            out = cr._read_or_refresh_deps(self._key(), None)
        self.assertIs(out, fresh)
        fetch.assert_not_called()

    def test_a_stale_github_cache_is_refreshed_in_the_sweep(self):
        stale = {"edges": [], "nodes": {}, "fetched_at": time.time() - 99999}
        stored = {"edges": [{"blocked": 2, "blocker": 1, "source": "native"}], "nodes": {}}
        with (
            mock.patch.object(cr.store, "read_deps_cache", side_effect=[stale, stored]),
            mock.patch.object(cr.store, "read_issues_cache", return_value=[{"number": 2}]),
            mock.patch.object(cr.store, "write_deps_cache") as write,
            mock.patch.object(
                gh,
                "fetch_dependency_edges",
                return_value=([{"blocked": 2, "blocker": 1, "source": "native"}], {}),
            ),
        ):
            out = cr._read_or_refresh_deps(self._key(), None)
        write.assert_called_once()
        self.assertIs(out, stored)

    def test_a_non_github_provider_stays_a_plain_cache_read(self):
        with mock.patch.object(cr.store, "read_deps_cache", return_value=None):
            self.assertIsNone(cr._read_or_refresh_deps(self._key("gitlab"), None))

    def test_a_refresh_failure_keeps_the_stale_graph(self):
        stale = {"edges": [], "nodes": {}, "fetched_at": time.time() - 99999}
        with (
            mock.patch.object(cr.store, "read_deps_cache", return_value=stale),
            mock.patch.object(cr.store, "read_issues_cache", return_value=[]),
            mock.patch.object(gh, "fetch_dependency_edges", side_effect=RuntimeError("boom")),
        ):
            out = cr._read_or_refresh_deps(self._key(), None)
        self.assertIs(out, stale)


class SweepUnknownScopeTest(unittest.TestCase):
    def test_an_absent_issues_cache_keeps_the_cached_graph(self):
        # Unknown scope must never build (and overwrite) a wrong-empty graph.
        stale = {"edges": [{"blocked": 2, "blocker": 1, "source": "native"}], "nodes": {}}
        stale["fetched_at"] = time.time() - 99999
        with (
            mock.patch.object(cr.store, "read_deps_cache", return_value=stale),
            mock.patch.object(cr.store, "read_issues_cache", return_value=None),
            mock.patch.object(cr.store, "write_deps_cache") as write,
            mock.patch.object(gh, "fetch_dependency_edges") as fetch,
        ):
            out = cr._read_or_refresh_deps(
                provider.RepoKey(owner="o", repo="r", provider="github"), None
            )
        self.assertIs(out, stale)
        fetch.assert_not_called()
        write.assert_not_called()


class SeedFreshnessTest(unittest.TestCase):
    def test_a_fresh_dependency_row_overwrites_a_stale_open_seed(self):
        # The issues cache still says #5 is open; the fetch's own dependency row
        # says closed. The persisted node must be closed or auto-unlock never fires.
        open_issues = [
            {"number": 10, "title": "d", "state": "open"},
            {"number": 5, "title": "stale title", "state": "open"},
        ]
        native = [{"number": 5, "title": "blk", "state": "closed", "is_pr": False}]
        with (
            mock.patch.object(gh, "_batch_dependency_graph", return_value=None),
            mock.patch.object(
                gh,
                "list_issue_blocked_by",
                side_effect=lambda o, r, n, timeout: native if n == 10 else [],
            ),
            mock.patch.object(gh, "list_issue_timeline", return_value=[]),
        ):
            _, nodes = gh.fetch_dependency_edges(OWNER, REPO, open_issues, {})
        self.assertEqual(nodes["5"]["state"], "closed")

    def test_a_truncated_batch_entry_falls_back_to_per_issue_reads(self):
        batch = {
            10: {
                "row": {
                    "number": 10,
                    "title": "mega",
                    "state": "open",
                    "is_pr": False,
                    "merged_at": None,
                },
                "native": [],  # truncated: incomplete by construction
                "refs": [],
                "truncated": True,
            }
        }
        native = [{"number": 5, "title": "blk", "state": "open", "is_pr": False}]
        with (
            mock.patch.object(gh, "_batch_dependency_graph", return_value=batch),
            mock.patch.object(gh, "list_issue_blocked_by", return_value=native) as per_issue,
            mock.patch.object(gh, "list_issue_timeline", return_value=[]),
        ):
            edges, _ = gh.fetch_dependency_edges(
                OWNER, REPO, [{"number": 10, "title": "mega", "state": "open"}], {}
            )
        per_issue.assert_called_once()
        self.assertIn({"blocked": 10, "blocker": 5, "source": "native"}, edges)

    def test_batch_marks_truncated_connections(self):
        page = _gql_page(
            [
                {
                    "number": 10,
                    "title": "mega",
                    "state": "OPEN",
                    "blockedBy": {"pageInfo": {"hasNextPage": True}, "nodes": []},
                    "timelineItems": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                }
            ]
        )
        with mock.patch.object(gh, "_gh_run", return_value=page):
            out = gh._batch_dependency_graph(OWNER, REPO)
        self.assertTrue(out[10]["truncated"])


class ClosedItemBlockerCountTest(unittest.TestCase):
    def test_a_closed_item_reads_unknown_not_zero(self):
        # The graph is scoped to open issues: a closed tracked item has no edges,
        # and zero would fire a false prev>0 -> 0 unlock the moment it closes.
        deps = {"edges": [{"blocked": 10, "blocker": 5, "source": "native"}], "nodes": {}}
        fake_client = mock.Mock()
        fake_client.get_issue_detail.return_value = {"state": "closed", "comments": 0}
        with (
            mock.patch.object(cr.provider, "client_for", return_value=fake_client),
            mock.patch.object(cr.provider, "call_kwargs", return_value={}),
        ):
            fp = cr.fingerprint_item(
                provider.RepoKey(owner="o", repo="r", provider="github"),
                {"number": 10, "phase": "implementing"},
                {},
                None,
                deps=deps,
            )
        self.assertIsNone(fp["open_blockers"])

    def test_an_open_item_still_counts_from_the_graph(self):
        deps = {"edges": [{"blocked": 10, "blocker": 5, "source": "native"}], "nodes": {}}
        fake_client = mock.Mock()
        fake_client.get_issue_detail.return_value = {"state": "open", "comments": 0}
        with (
            mock.patch.object(cr.provider, "client_for", return_value=fake_client),
            mock.patch.object(cr.provider, "call_kwargs", return_value={}),
        ):
            fp = cr.fingerprint_item(
                provider.RepoKey(owner="o", repo="r", provider="github"),
                {"number": 10, "phase": "implementing"},
                {},
                None,
                deps=deps,
            )
        self.assertEqual(fp["open_blockers"], 1)


class DepsAuthFailureTest(unittest.TestCase):
    def test_a_403_propagates_instead_of_reading_as_empty(self):
        # Revoked access must never become "no dependencies": an empty result
        # would overwrite the cached graph and falsely unlock every dependent.
        proc = mock.Mock(returncode=1, stdout="", stderr="HTTP 403: Forbidden")
        with mock.patch.object(gh, "_gh_run", return_value=proc):
            with self.assertRaises(gh.GhCliError):
                gh.list_issue_blocked_by(OWNER, REPO, 10)

    def test_a_404_still_reads_as_feature_absent(self):
        proc = mock.Mock(returncode=1, stdout="", stderr="HTTP 404: Not Found")
        with mock.patch.object(gh, "_gh_run", return_value=proc):
            self.assertEqual(gh.list_issue_blocked_by(OWNER, REPO, 10), [])

    def test_timeline_403_propagates_too(self):
        with mock.patch.object(
            gh, "list_issue_timeline", side_effect=gh.GhCliError("HTTP 403: Forbidden")
        ):
            with self.assertRaises(gh.GhCliError):
                gh._inferred_blockers_from_timeline(OWNER, REPO, 10, timeout=5)
