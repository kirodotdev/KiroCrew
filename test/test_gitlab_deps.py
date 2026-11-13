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
        # Nodes are seeded ONLY for the walked (capped) issues; a NEW endpoint a
        # walked issue links to is admitted only while under the node cap. Patch the
        # caps small: 2 walked issues seed 2 nodes (== cap), so a fresh link to an
        # already-seeded node still overwrites its state, but a link to a NEW node is
        # refused and its edge dropped with it (no edge may name a descriptor-less
        # number — the frontend would draw it as an open, blocking issue).
        with (
            mock.patch.object(gitlab_queries, "_MAX_LINKS_CALLS", 2),
            mock.patch.object(gitlab_queries, "_MAX_DEPS_NODES", 2),
        ):
            issues = [
                {"number": 1, "title": "seeded", "state": "open"},
                {"number": 2, "title": "seeded", "state": "open"},
            ]

            # Issue 1 links to node 2, which is already seeded (in the capped set):
            # the fresh /links state wins over the seed, and no new node is added.
            existing = _make_run_api({1: [_link(2, "blocks", "closed")], 2: []})
            edges, nodes = _edges(OWNER, REPO, issues, run_api=existing)
            self.assertEqual(set(nodes), {"1", "2"})
            self.assertEqual(nodes["2"]["state"], "closed")

            # Issue 1 links to a NEW node (99) while the cap is full (2 seeded):
            # the node is refused and its edge dropped with it.
            new_node = _make_run_api({1: [_link(99, "blocks", "closed")], 2: []})
            with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
                edges, nodes = _edges(OWNER, REPO, issues, run_api=new_node)
            self.assertEqual(set(nodes), {"1", "2"})
            self.assertNotIn("99", nodes)
            self.assertEqual(edges, [])
            # 1 BLOCKS the refused node: the dropped edge took nothing from 1's own
            # blocker set, so neither retained node is flagged.
            self.assertNotIn("blockers_unknown", nodes["1"])
            self.assertNotIn("blockers_unknown", nodes["2"])

            # Issue 1 is BLOCKED BY a new node the cap refuses: its blocker is now
            # missing from the graph, so 1 must not read as ready.
            refused_blocker = _make_run_api({1: [_link(99, "is_blocked_by", "open")], 2: []})
            with self.assertLogs("kirocrew.app.issue-radar", level="WARNING"):
                edges, nodes = _edges(OWNER, REPO, issues, run_api=refused_blocker)
            self.assertEqual(edges, [])
            self.assertTrue(nodes["1"].get("blockers_unknown"))
            self.assertNotIn("blockers_unknown", nodes["2"])
        self.assertTrue(any("nodes_over_max=1" in line for line in captured.output))

    def test_every_retained_edge_endpoint_has_a_node(self):
        # Every edge the fetcher keeps must name two numbers both in ``nodes``;
        # otherwise the frontend invents the missing endpoint as open and the
        # dependent reads ``hold`` (or, for a closed blocker, falsely ``ready``).
        # Seeded nodes are only the walked (capped) issues, so to exercise the node
        # cap the walked issues must themselves name enough distinct new blockers to
        # fill it. Patch caps small: 3 walked issues seed {1,2,3}, cap is 4, and each
        # names a distinct closed blocker past the walked set — one blocker fits, the
        # rest are refused together with their edges.
        with (
            mock.patch.object(gitlab_queries, "_MAX_LINKS_CALLS", 3),
            mock.patch.object(gitlab_queries, "_MAX_DEPS_NODES", 4),
        ):
            issues = [{"number": n, "title": "t", "state": "open"} for n in range(1, 4)]
            run_api = _make_run_api(
                {n: [_link(100 + n, "is_blocked_by", "closed")] for n in range(1, 4)}
            )
            with self.assertLogs("kirocrew.app.issue-radar", level="WARNING"):
                edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        # The three walked issues are seeded; exactly one blocker fit the last node
        # slot. Every retained edge names two numbers both present as nodes.
        self.assertEqual(len(nodes), 4)
        self.assertTrue({"1", "2", "3"}.issubset(set(nodes)))
        for e in edges:
            self.assertIn(str(e["blocker"]), nodes)
            self.assertIn(str(e["blocked"]), nodes)
        # Only one node slot remained, so only one blocker (and its edge) was kept;
        # the other two were refused with their edges rather than reaching the UI.
        self.assertEqual(len(edges), 1)
        self.assertTrue(all(nodes[str(e["blocker"])]["state"] == "closed" for e in edges))


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
        # Two bounds govern seeding: the open-issue list is capped at _MAX_DEPS_NODES
        # (rows past it are neither seeded nor walked, counted as
        # open_issue_nodes_over_max), and only the first _MAX_LINKS_CALLS of what
        # remains is seeded AND walked. A project past both is served partial, never
        # refused. Patch caps small: _MAX_DEPS_NODES=3 keeps the first 3 of 5 rows,
        # then _MAX_LINKS_CALLS=2 seeds/walks the first 2 of those.
        with (
            mock.patch.object(gitlab_queries, "_MAX_DEPS_NODES", 3),
            mock.patch.object(gitlab_queries, "_MAX_LINKS_CALLS", 2),
        ):
            issues = [{"number": n, "title": "t", "state": "open"} for n in range(1, 6)]

            def link_free(path: str, **_kw) -> list[dict]:
                return []

            with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
                edges, nodes = _edges(OWNER, REPO, issues, run_api=link_free)

        self.assertEqual(edges, [])
        # Only the first _MAX_LINKS_CALLS (2) issues are seeded — a seeded node must
        # have had its /links fetched, so none can read falsely "ready".
        self.assertEqual(set(nodes), {"1", "2"})
        # The two rows past _MAX_DEPS_NODES (3) were dropped from the list entirely.
        truncated = [line for line in captured.output if "serving the partial graph" in line]
        self.assertEqual(len(truncated), 1)
        self.assertIn("open_issue_nodes_over_max=2", truncated[0])

    def test_one_issue_over_the_link_row_limit_keeps_the_first_rows(self):
        links = [_link(2, "blocks")] * (_MAX_LINKS_PER_ISSUE + 1)

        with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
            edges, nodes = _edges(
                OWNER,
                REPO,
                [
                    {"number": 1, "title": "t", "state": "open"},
                    {"number": 3, "title": "t", "state": "open"},
                ],
                run_api=_make_run_api({1: links, 3: []}),
            )

        self.assertEqual(len(edges), _MAX_LINKS_PER_ISSUE)
        self.assertIn("link_rows_over_per_issue_max=1", captured.output[0])
        # The dropped row could have been one of issue 1's own blockers, so 1 is
        # flagged: its incoming edges are incomplete. Issue 3, whose rows all fit,
        # is not.
        self.assertTrue(nodes["1"].get("blockers_unknown"))
        self.assertNotIn("blockers_unknown", nodes["3"])

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
            edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(len(edges), _MAX_DEPS_EDGES)
        self.assertIn("edges_over_max=1", captured.output[0])

    def test_an_edge_dropped_by_the_edge_cap_flags_its_blocked_node(self):
        # Cap at one edge. Issue 1 blocks 2 and issue 2 is blocked by 3: two edges
        # into node 2, of which the cap keeps one (the pool decides which). Either
        # way walked issue 2 has lost a blocker it really has, so it must carry
        # blockers_unknown; 1 and 3 lost nothing of their own.
        with mock.patch.object(gitlab_queries, "_MAX_DEPS_EDGES", 1):
            issues = [{"number": n, "title": "t", "state": "open"} for n in (1, 2, 3)]
            run_api = _make_run_api(
                {1: [_link(2, "blocks", "open")], 2: [_link(3, "is_blocked_by", "open")], 3: []}
            )
            with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
                edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["blocked"], 2)
        self.assertIn(edges[0]["blocker"], (1, 3))
        self.assertIn("edges_over_max=1", captured.output[0])
        self.assertTrue(nodes["2"].get("blockers_unknown"))
        self.assertNotIn("blockers_unknown", nodes["1"])
        self.assertNotIn("blockers_unknown", nodes["3"])

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

    def test_an_unmapped_exception_for_one_issue_also_skips_only_that_issue(self):
        # Not every failure arrives as ProviderCliError: json.loads raises a bare
        # ValueError on a 4300+ digit integer, subprocess.run can raise an unmapped
        # OSError. The worker's contract is per-issue degrade, so these count as that
        # issue's failure (logged at WARNING with the type) rather than escaping the
        # pool as a 500 that first drains every remaining call.
        def one_bad_payload(path: str, **_kw) -> list:
            iid = int(path.rstrip("/").split("/")[-2])
            if iid == 99:
                raise ValueError("Exceeds the limit (4300 digits) for integer string conversion")
            return [_link(5, "is_blocked_by")]

        issues = [
            {"number": 10, "title": "a", "state": "open"},
            {"number": 99, "title": "b", "state": "open"},
        ]
        with self.assertLogs(gitlab_queries.logger, level="WARNING") as captured:
            edges, _nodes = _edges(OWNER, REPO, issues, run_api=one_bad_payload)
        self.assertIn({"blocked": 10, "blocker": 5, "source": "native"}, edges)
        self.assertEqual([e for e in edges if 99 in (e["blocked"], e["blocker"])], [])
        self.assertTrue(any("ValueError" in line and "#99" in line for line in captured.output))

    def test_unmapped_exceptions_on_most_issues_still_refuse_the_graph(self):
        # The majority-failure rule sees these as failures too, so a host returning
        # garbage for most issues cannot ship a nearly empty graph as fresh.
        def mostly_garbage(path: str, **_kw) -> list:
            iid = int(path.rstrip("/").split("/")[-2])
            if iid <= 7:
                raise TypeError("garbage")
            return []

        issues = [{"number": n, "title": "t", "state": "open"} for n in range(1, 11)]
        with (
            self.assertRaises(ProviderCliError),
            self.assertLogs(gitlab_queries.logger, level="WARNING"),
        ):
            _edges(OWNER, REPO, issues, run_api=mostly_garbage)

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
        # Issues 6-10 failed their own /links call and no survivor links to them, so
        # their seeded nodes are DROPPED (absent beats a false-ready node) rather
        # than left edgeless, which buildNodes would draw as "ready".
        issues = [{"number": n, "title": "t", "state": "open"} for n in range(1, 11)]
        run_api = _make_run_api({n: [_link(n + 100, "blocks")] for n in range(1, 6)})
        with self.assertLogs("kirocrew.app.issue-radar", level="WARNING") as captured:
            edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(sorted(e["blocked"] for e in edges), [101, 102, 103, 104, 105])
        # Survivors 1-5 and their blockers 101-105 remain; the 5 failed-and-unlinked
        # issues 6-10 are omitted.
        self.assertEqual(sorted(int(k) for k in nodes), [1, 2, 3, 4, 5, 101, 102, 103, 104, 105])
        self.assertTrue(
            any("omitted 5 issue(s) whose /links call failed" in o for o in captured.output)
        )

    def test_a_failed_issue_that_a_survivor_links_to_keeps_its_node(self):
        # The prune only drops a failed issue with NO edge touching it. Issue 2's own
        # call fails, but issue 1's successful call says "1 is_blocked_by 2", so 2 is
        # a real blocker endpoint — its node stays (that edge carries its state) and
        # the edge is kept.
        issues = [
            {"number": 1, "title": "t", "state": "open"},
            {"number": 2, "title": "t", "state": "open"},
        ]
        run_api = _make_run_api(
            {1: [_link(2, "is_blocked_by", "closed")]}
        )  # 2 omitted from dict -> its call fails
        edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(edges, [{"blocked": 1, "blocker": 2, "source": "native"}])
        self.assertEqual(sorted(int(k) for k in nodes), [1, 2])
        self.assertEqual(nodes["2"]["state"], "closed")
        # Its own /links never came back, so its blockers are unknown: flagged, so
        # the frontend cannot read "no incoming edge" as ready.
        self.assertTrue(nodes["2"].get("blockers_unknown"))
        self.assertNotIn("blockers_unknown", nodes["1"])

    def test_a_link_admitted_node_past_the_cap_is_flagged_blockers_unknown(self):
        # Issue 3 is past _MAX_LINKS_CALLS, so its /links are never read. Issue 1
        # says "1 is_blocked_by 3", which admits 3 as a node. 3 may itself be blocked
        # by something nobody fetched, so it must carry blockers_unknown; the walked
        # issues (whose incoming edges are authoritative) must not.
        with mock.patch.object(gitlab_queries, "_MAX_LINKS_CALLS", 2):
            issues = [{"number": n, "title": "t", "state": "open"} for n in (1, 2, 3)]
            run_api = _make_run_api({1: [_link(3, "is_blocked_by", "open")], 2: []})
            with self.assertLogs("kirocrew.app.issue-radar", level="WARNING"):
                edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(edges, [{"blocked": 1, "blocker": 3, "source": "native"}])
        self.assertEqual(sorted(int(k) for k in nodes), [1, 2, 3])
        self.assertTrue(nodes["3"].get("blockers_unknown"))
        self.assertNotIn("blockers_unknown", nodes["1"])
        self.assertNotIn("blockers_unknown", nodes["2"])

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

    def test_a_worker_bug_also_cancels_the_rest_before_propagating(self):
        # The worker degrades every per-call failure itself, so the only exception
        # that can escape it is a bug in the worker. The pool loop must still cancel
        # the queue on that path: without it ``with`` runs ``shutdown(wait=True)``
        # and finishes every queued call before the caller sees the error.
        import threading
        from concurrent.futures import Future, ThreadPoolExecutor

        calls: list[int] = []
        lock = threading.Lock()
        cancelled = threading.Event()
        futures: list[Future] = []
        timed_out: list[int] = []

        class Recording(ThreadPoolExecutor):
            def submit(self, fn, /, *args, **kwargs):
                future = super().submit(fn, *args, **kwargs)
                futures.append(future)
                return future

            def shutdown(self, wait=True, *, cancel_futures=False):
                super().shutdown(wait=wait, cancel_futures=cancel_futures)
                if cancel_futures:
                    cancelled.set()

        def buggy_worker(owner, repo, iid, *, host, timeout):
            with lock:
                calls.append(iid)
                first = len(calls) == 1
            if first:
                raise RuntimeError("worker bug")
            if not cancelled.wait(timeout=10):
                timed_out.append(iid)
            return [], 0

        workers = gitlab_queries._LINKS_WORKERS
        total = 10 * workers
        issues = [{"number": n, "title": "t", "state": "open"} for n in range(1, total + 1)]
        with (
            mock.patch.object(gitlab_queries, "ThreadPoolExecutor", Recording),
            mock.patch.object(gitlab_queries, "_fetch_issue_links", buggy_worker),
        ):
            with self.assertRaises(RuntimeError):
                gitlab_queries.fetch_dependency_edges(OWNER, REPO, issues, host=HOST)
        self.assertEqual(timed_out, [])
        self.assertTrue(cancelled.is_set())
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
        # A foreign row this issue BLOCKS takes nothing away from its own
        # blockers, so the issue is not flagged.
        self.assertNotIn("blockers_unknown", nodes["1"])

    def test_dropped_open_foreign_blocker_flags_the_local_issue(self):
        # Issue 1 is blocked by a closed local issue and an OPEN cross-project
        # one. The foreign row stays out of the graph, but with it gone the local
        # issue's blockers look all closed; the flag stops buildNodes drawing it
        # ready while it is in fact still blocked.
        foreign_open = dict(_link(7, "is_blocked_by"), references={"relative": "other/proj#7"})
        local_closed = _link(9, "is_blocked_by", state="closed")
        run_api = _make_run_api({1: [foreign_open, local_closed], 9: []})
        issues = [
            {"number": 1, "title": "a", "state": "open"},
            {"number": 9, "title": "b", "state": "closed"},
        ]
        edges, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertEqual(edges, [{"blocked": 1, "blocker": 9, "source": "native"}])
        self.assertNotIn("7", nodes)
        self.assertTrue(nodes["1"].get("blockers_unknown"))

    def test_dropped_closed_foreign_blocker_does_not_flag(self):
        # A closed foreign blocker holds nothing back: dropping it leaves the
        # local issue's blocked state fully known.
        foreign_closed = dict(
            _link(7, "is_blocked_by", state="closed"), references={"relative": "other/proj#7"}
        )
        run_api = _make_run_api({1: [foreign_closed]})
        issues = [{"number": 1, "title": "a", "state": "open"}]
        _, nodes = _edges(OWNER, REPO, issues, run_api=run_api)
        self.assertNotIn("blockers_unknown", nodes["1"])


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

    def test_list_issue_links_is_one_request_not_a_page_walk(self):
        # GitLab's /issues/:iid/links ignores page/per_page and returns the whole list.
        # Walking pages never sees a short page for an issue with >= 100 links, so it
        # would spawn _MAX_PAGES identical glab calls per issue; one request is the
        # whole answer. The int() on iid is what keeps a path segment out of the URL.
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client

        seen: list[tuple[str, dict]] = []

        def fake_api(path, **kw):
            seen.append((path, kw))
            return [_link(2, "blocks")]

        with mock.patch.object(gitlab_client, "_glab_api", side_effect=fake_api):
            rows = gitlab_client.list_issue_links("grp/sub", "proj", 7, host="gitlab.example.com")
        self.assertEqual(len(seen), 1)
        path, kw = seen[0]
        self.assertEqual(path, "projects/grp%2Fsub%2Fproj/issues/7/links")
        self.assertIs(kw["paginate"], False)
        self.assertEqual(kw["host"], "gitlab.example.com")
        self.assertEqual([r["iid"] for r in rows], [2])

    def test_list_issue_links_with_an_empty_body_is_an_empty_list(self):
        # Non-paginated transport returns {} for an empty body; _rows turns that into [].
        from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client

        with mock.patch.object(gitlab_client, "_glab_api", return_value={}):
            self.assertEqual(gitlab_client.list_issue_links("o", "r", 1, host="gitlab.com"), [])


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

    def test_a_gitlab_fetch_failure_is_a_502_that_keeps_the_cache(self):
        # The 502 depends on ProviderSetupError / ProviderCliError being caught by the
        # route. Pinned for the GitLab branch so a later narrowing of those excepts cannot
        # turn a failure into a 500, and so nothing is written to the cache on the way out.
        # The two causes get DISTINCT codes: a missing glab session is the user's to fix
        # (deps_auth_required -> "sign in"), a majority-failed walk is an outage the user
        # cannot fix by re-authenticating (deps_fetch_failed -> "wait and retry"). The UX
        # review blocked the earlier revision that showed the sign-in blurb for both.
        for exc, code in (
            (
                ProviderSetupError(
                    "glab is not authenticated for gitlab.com", reason="not_authenticated"
                ),
                "deps_auth_required",
            ),
            (ProviderCliError("7 of 10 /links calls failed"), "deps_fetch_failed"),
        ):
            with self.subTest(exc=type(exc).__name__):
                with (
                    mock.patch.object(store, "is_repo_connected", return_value=True),
                    mock.patch.object(store, "read_deps_cache", return_value=None),
                    mock.patch.object(routes, "_load_open_issues_for_reco", return_value=[]),
                    mock.patch.object(store, "write_deps_cache") as write,
                    mock.patch.object(gitlab_queries, "fetch_dependency_edges", side_effect=exc),
                ):
                    res = asyncio.run(_call(GL_QUERY))
                self.assertEqual(res.status, 502)
                self.assertEqual(_body(res)["code"], code)
                write.assert_not_called()

    def test_cold_issue_scope_with_no_glab_session_is_auth_required_not_scope(self):
        # When the issue-scope load ITSELF raises ProviderSetupError (cold issues
        # cache AND no glab session), the route must answer deps_auth_required
        # ("sign in"), not deps_issue_scope_unavailable ("open the Issues tab").
        # ProviderSetupError is a GhCliError subclass, so without the dedicated
        # re-raise it would be wrapped as _DepsScopeUnavailable and mislead the user.
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", return_value=None),
            mock.patch.object(
                routes,
                "_load_open_issues_for_reco",
                side_effect=ProviderSetupError(
                    "glab is not authenticated for gitlab.com", reason="not_authenticated"
                ),
            ),
            mock.patch.object(store, "write_deps_cache") as write,
        ):
            res = asyncio.run(_call(GL_QUERY))
        self.assertEqual(res.status, 502)
        body = _body(res)
        self.assertEqual(body["code"], "deps_auth_required")
        # The remedy command comes from the connected key's own host, never
        # parsed out of the CLI's message (which here names a different host).
        self.assertEqual(body["loginCommand"], f"glab auth login --hostname {HOST}")
        write.assert_not_called()

    def test_not_installed_setup_error_is_cli_missing_with_no_login_command(self):
        # A missing binary is not fixed by signing in: its own code, no command.
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", return_value=None),
            mock.patch.object(
                routes,
                "_load_open_issues_for_reco",
                side_effect=ProviderSetupError("glab is not installed", reason="not_installed"),
            ),
            mock.patch.object(store, "write_deps_cache"),
        ):
            res = asyncio.run(_call(GL_QUERY))
        self.assertEqual(res.status, 502)
        body = _body(res)
        self.assertEqual(body["code"], "deps_cli_missing")
        self.assertNotIn("loginCommand", body)

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

    def test_github_setup_error_keeps_the_codes_it_had_before_gitlab_support(self):
        # The setup codes (deps_auth_required / deps_cli_missing) are read by the
        # Graph tab on GitLab only. On GitHub a ProviderSetupError is a GhCliError
        # and keeps main's mapping: fetch_failed from the edge fetch, scope
        # unavailable from the issue-scope load, and never a gh login command.
        cases = (
            ("edges", "not_authenticated", "deps_fetch_failed"),
            ("edges", "not_installed", "deps_fetch_failed"),
            ("scope", "not_authenticated", "deps_issue_scope_unavailable"),
            ("scope", "not_installed", "deps_issue_scope_unavailable"),
        )
        for where, reason, code in cases:
            exc = ProviderSetupError("gh is not set up", reason=reason)
            with self.subTest(where=where, reason=reason):
                with (
                    mock.patch.object(store, "is_repo_connected", return_value=True),
                    mock.patch.object(store, "read_deps_cache", return_value=None),
                    mock.patch.object(
                        routes,
                        "_load_open_issues_for_reco",
                        **({"side_effect": exc} if where == "scope" else {"return_value": []}),
                    ),
                    mock.patch.object(store, "read_pulls_cache", return_value=[]),
                    mock.patch.object(store, "write_deps_cache") as write,
                    mock.patch.object(
                        gh,
                        "fetch_dependency_edges",
                        side_effect=exc if where == "edges" else None,
                        return_value=([], {}),
                    ),
                ):
                    res = asyncio.run(_call(GH_QUERY))
                self.assertEqual(res.status, 502)
                body = _body(res)
                self.assertEqual(body["code"], code)
                self.assertNotIn("loginCommand", body)
                write.assert_not_called()


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
