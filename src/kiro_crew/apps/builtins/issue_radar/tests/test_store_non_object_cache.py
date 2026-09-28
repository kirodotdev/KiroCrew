"""A cache file whose JSON root is not an object must read as a MISS.

``read_pr_ai_cache`` already states the contract this module pins, in a comment
on its own guard: a syntactically VALID but non-object root (``[]``, a bare
string) "would blow up on ``.get()`` and keep failing every request until the
file is deleted by hand. Treat it as a miss and let the route rewrite it."

The readers here are caches -- every one of them is rebuilt by its route on a
miss -- so degrading to a miss costs a refetch and heals the file on the next
write. Each is reached from an HTTP route through ``routes._st``, so an
unguarded ``.get()`` on a non-object root is an ``AttributeError`` that surfaces
as a 500 on every request for that repo or issue until someone deletes the file
by hand.

``read_investigation`` is deliberately NOT in this module. Its record is the
only copy of a user's findings (``patch_investigation`` says so), so "treat a
malformed root as absent" would let the next write replace it -- silent loss
rather than a free refetch. What that reader should do is a product decision,
not this contract.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from kiro_crew.apps.builtins.issue_radar.backend import store

OWNER = "o"
REPO = "r"
NUMBER = 7

# Valid JSON, non-object root. A list is the shape a hand-edit or a restore from
# a differently-shaped backup most plausibly leaves behind; ``json.loads``
# accepts it, so the JSONDecodeError arm these readers already have never runs.
_NON_OBJECT = "[]"


class TestCacheReadersTreatANonObjectRootAsAMiss(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_issues_cache_reads_as_a_miss(self):
        store.write_issues_cache(OWNER, REPO, [{"number": 1}], root=self.tmp)
        path = store.issues_cache_path(OWNER, REPO, self.tmp)
        path.write_text(_NON_OBJECT, encoding="utf-8")
        self.assertIsNone(store.read_issues_cache(OWNER, REPO, self.tmp))

    def test_members_cache_reads_as_a_miss(self):
        store.write_members_cache(OWNER, REPO, [{"login": "a"}], source="org", root=self.tmp)
        path = store.members_cache_path(OWNER, REPO, self.tmp)
        path.write_text(_NON_OBJECT, encoding="utf-8")
        self.assertIsNone(store.read_members_cache(OWNER, REPO, self.tmp))

    def test_issue_detail_cache_reads_as_a_miss(self):
        store.write_issue_detail_cache(OWNER, REPO, NUMBER, {"title": "t"}, [], root=self.tmp)
        path = store.issue_detail_cache_path(OWNER, REPO, NUMBER, self.tmp)
        path.write_text(_NON_OBJECT, encoding="utf-8")
        self.assertIsNone(store.read_issue_detail_cache(OWNER, REPO, NUMBER, self.tmp))

    def test_issue_ai_cache_reads_as_a_miss(self):
        store.write_issue_ai_cache(OWNER, REPO, NUMBER, {"summary": "s"}, root=self.tmp)
        path = store.issue_ai_cache_path(OWNER, REPO, NUMBER, self.tmp)
        path.write_text(_NON_OBJECT, encoding="utf-8")
        self.assertIsNone(store.read_issue_ai_cache(OWNER, REPO, NUMBER, self.tmp))

    def test_recommendations_cache_reads_as_a_miss(self):
        store.write_recommendations_cache(OWNER, REPO, {"recommendations": []}, root=self.tmp)
        path = store.recommendations_cache_path(OWNER, REPO, self.tmp)
        path.write_text(_NON_OBJECT, encoding="utf-8")
        self.assertIsNone(store.read_recommendations_cache(OWNER, REPO, self.tmp))

    def test_a_healthy_cache_is_still_served(self):
        """The guard must not turn every read into a miss.

        Without this, a reader that unconditionally returned None would pass
        every assertion above while destroying the cache.
        """
        store.write_issue_detail_cache(
            OWNER, REPO, NUMBER, {"title": "t"}, [{"event": "labeled"}], root=self.tmp
        )
        self.assertEqual(
            store.read_issue_detail_cache(OWNER, REPO, NUMBER, self.tmp),
            {"detail": {"title": "t"}, "timeline": [{"event": "labeled"}]},
        )

    def test_the_corruption_is_valid_json(self):
        """Guard the guard: if ``_NON_OBJECT`` stopped parsing, the tests above
        would pass through the JSONDecodeError arm that already exists and prove
        nothing about the root-shape guard."""
        self.assertEqual(json.loads(_NON_OBJECT), [])
