"""A GitLab crew item the deps graph never read must count as unknown blockers.

The GitLab fetcher (``gitlab_queries.fetch_dependency_edges``) leaves an open
issue out of ``nodes`` when it never read that issue's ``/links``: past
``_MAX_LINKS_CALLS`` or ``_MAX_DEPS_NODES`` with no fetched issue linking to it,
or a failed call no edge touched. Such an item also has no edges, which
``_open_blocker_count`` would otherwise read as zero open blockers, and a 1 → 0
reading fires ``SIG_DEP_UNBLOCKED`` on a still-blocked issue. The GitHub fetcher
seeds every open issue, so its "no node, no edges is zero" reading is pinned
unchanged here too.

The graphs below come from the real GitLab fetcher with only the ``/links`` read
faked, and are round-tripped through the deps-cache store the sweep reads.
"""

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client
from kiro_crew.apps.builtins.issue_radar.backend import gitlab_queries as glq
from kiro_crew.apps.builtins.issue_radar.backend import provider, store
from kiro_crew.apps.builtins.issue_radar.backend.errors import ProviderCliError

OWNER, REPO, HOST = "o", "r", "gitlab.example.com"
GITLAB_KEY = provider.RepoKey(owner=OWNER, repo=REPO, provider="gitlab", host=HOST)
GITHUB_KEY = provider.key_from_parts(OWNER, REPO)


def _gitlab_graph(open_issues, links_by_iid, *, max_calls):
    """Run the real GitLab fetcher with a fake ``/links`` reader and a lowered cap.

    An iid missing from ``links_by_iid`` fails its call, as a host error would.
    """

    def fake_links(owner, repo, iid, *, host, timeout):
        if iid not in links_by_iid:
            raise ProviderCliError(f"simulated failure for iid {iid}")
        return list(links_by_iid[iid])

    with (
        mock.patch.object(glq, "_MAX_LINKS_CALLS", max_calls),
        mock.patch.object(gitlab_client, "list_issue_links", side_effect=fake_links),
    ):
        return glq.fetch_dependency_edges(OWNER, REPO, open_issues, host=HOST)


def _open(*numbers):
    return [{"number": n, "title": "t", "state": "open"} for n in numbers]


class OpenBlockerCountAbsentNodeTest(unittest.TestCase):
    """``_open_blocker_count`` with and without ``absent_is_unknown``."""

    GRAPH = {"edges": [], "nodes": {"5": {"kind": "issue", "state": "open", "title": ""}}}

    def test_an_absent_item_is_unknown_when_absence_means_unread(self):
        self.assertIsNone(cr._open_blocker_count(self.GRAPH, 1, absent_is_unknown=True))

    def test_a_present_item_is_still_counted_under_the_flag(self):
        self.assertEqual(cr._open_blocker_count(self.GRAPH, 5, absent_is_unknown=True), 0)

    def test_an_absent_item_is_zero_by_default(self):
        self.assertEqual(cr._open_blocker_count(self.GRAPH, 1), 0)


class FingerprintAbsentNodeTest(unittest.TestCase):
    """``fingerprint_item`` picks the absent-node reading by the item's provider."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def _fp(self, key, number, deps):
        client = mock.Mock()
        client.get_issue_detail.return_value = {"comments": 0, "state": "open"}
        with (
            mock.patch.object(cr.provider, "client_for", return_value=client),
            mock.patch.object(cr.provider, "call_kwargs", return_value={}),
        ):
            return cr.fingerprint_item(
                key, {"number": number, "phase": "awaiting-reply"}, {}, None, deps
            )

    def _through_the_store(self, edges, nodes):
        store.write_deps_cache(OWNER, REPO, edges, nodes, root=self.root, fetched_at=time.time())
        return store.read_deps_cache(OWNER, REPO, self.root)

    def test_a_github_item_absent_from_nodes_still_records_zero(self):
        fp = self._fp(GITHUB_KEY, 10, OpenBlockerCountAbsentNodeTest.GRAPH)
        self.assertEqual(fp["open_blockers"], 0)

    def test_a_gitlab_item_absent_from_nodes_records_unknown(self):
        fp = self._fp(GITLAB_KEY, 10, OpenBlockerCountAbsentNodeTest.GRAPH)
        self.assertIsNone(fp["open_blockers"])

    def test_a_gitlab_item_past_the_links_cap_records_unknown_and_never_unblocks(self):
        # Issue 2 is past _MAX_LINKS_CALLS and nothing fetched links to it, so it
        # gets no node and no edges. Last cycle it read 1 open blocker; this
        # reading must be unknown, so no 1 → 0 unlock fires.
        edges, nodes = _gitlab_graph(_open(1, 2), {1: []}, max_calls=1)
        self.assertNotIn("2", nodes)
        fp = self._fp(GITLAB_KEY, 2, self._through_the_store(edges, nodes))
        self.assertIsNone(fp["open_blockers"])
        self.assertNotIn(cr.SIG_DEP_UNBLOCKED, cr.detect_unblocks({"open_blockers": 1}, fp))

    def test_a_gitlab_item_whose_links_call_failed_records_unknown(self):
        # Issue 3's call fails and no other issue links to it, so the fetcher
        # drops its node (one failure of three stays under the refusal ratio).
        edges, nodes = _gitlab_graph(_open(1, 2, 3), {1: [], 2: []}, max_calls=10)
        self.assertNotIn("3", nodes)
        fp = self._fp(GITLAB_KEY, 3, self._through_the_store(edges, nodes))
        self.assertIsNone(fp["open_blockers"])

    def test_a_walked_gitlab_item_with_no_blockers_still_records_zero(self):
        # The guard must not blind the signal: an issue whose /links came back
        # empty has a node and a real zero.
        edges, nodes = _gitlab_graph(_open(1, 2), {1: [], 2: []}, max_calls=10)
        fp = self._fp(GITLAB_KEY, 1, self._through_the_store(edges, nodes))
        self.assertEqual(fp["open_blockers"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
