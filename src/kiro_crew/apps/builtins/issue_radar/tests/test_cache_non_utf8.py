"""Issue Radar's cache readers must read a non-UTF-8 cache file as a miss.

Every reader here decodes its cache with ``.decode("utf-8")`` and already treats
a file that does not parse as a miss, so the route refetches. ``UnicodeDecodeError``
is a ``ValueError``, not a ``json.JSONDecodeError`` or an ``OSError``, so a cache
whose bytes are not UTF-8 (a hand edit, a restore from another tool) escaped those
guards and failed the route instead. It now reads as the same miss, and the
cache-patching helpers skip it the same way they skip bad JSON, leaving it on
disk for the next refresh to replace. The investigation record is not a cache, so
it is left to raise rather than be replaced.
"""

import shutil
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from kiro_crew.apps.builtins.issue_radar.backend import (
    store_ai,
    store_deps,
    store_issues,
    store_labels,
    store_pulls,
)

OWNER = "o"
REPO = "r"
NUMBER = 7

# Latin-1 bytes that are not valid UTF-8 on their own (0x80 is a continuation byte
# with no leading byte before it), so ``.decode("utf-8")`` raises deterministically.
_NON_UTF8_BYTES = b'{"schema": 1, "note": "caf\x80"}'


class TestIssueRadarCachesReadNonUtf8AsAMiss(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _plant(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_NON_UTF8_BYTES)
        return path

    def _no_raise(self, fn: Callable[[], object]) -> object:
        try:
            return fn()
        except UnicodeDecodeError as exc:  # pragma: no cover - the failure being pinned
            self.fail(f"a cache that is not UTF-8 raised {exc!r}")

    def test_ai_caches_read_as_a_miss(self):
        root = self.tmp
        self._plant(store_ai.issue_ai_cache_path(OWNER, REPO, NUMBER, root))
        self._plant(store_ai.pr_ai_cache_path(OWNER, REPO, NUMBER, root))
        self._plant(store_ai.recommendations_cache_path(OWNER, REPO, root))
        self._plant(store_ai.tagging_cache_path(OWNER, REPO, root))

        self.assertIsNone(
            self._no_raise(lambda: store_ai.read_issue_ai_cache(OWNER, REPO, NUMBER, root))
        )
        self.assertIsNone(
            self._no_raise(lambda: store_ai.read_pr_ai_cache(OWNER, REPO, NUMBER, root))
        )
        self.assertIsNone(
            self._no_raise(lambda: store_ai.read_recommendations_cache(OWNER, REPO, root))
        )
        self.assertIsNone(self._no_raise(lambda: store_ai.read_tagging_cache(OWNER, REPO, root)))

    def test_an_investigation_record_that_is_not_utf8_is_never_overwritten(self):
        # Not a cache: the write merges into the stored record and rewrites it
        # whole, so reading it as absent would replace it. It raises instead.
        root = self.tmp
        record = self._plant(store_ai.investigation_path(OWNER, REPO, NUMBER, root))

        with self.assertRaises(UnicodeDecodeError):
            store_ai.write_investigation(OWNER, REPO, NUMBER, {"slot_key": "s"}, root=root)

        self.assertEqual(record.read_bytes(), _NON_UTF8_BYTES)

    def test_issue_caches_read_as_a_miss(self):
        root = self.tmp
        self._plant(store_issues.issues_cache_path(OWNER, REPO, root, "open"))
        self._plant(store_issues.issue_detail_cache_path(OWNER, REPO, NUMBER, root))
        self._plant(store_issues.ref_summary_cache_path(OWNER, REPO, NUMBER, root))

        self.assertIsNone(
            self._no_raise(lambda: store_issues.read_issues_cache(OWNER, REPO, root, "open"))
        )
        self.assertIsNone(
            self._no_raise(lambda: store_issues.read_issue_detail_cache(OWNER, REPO, NUMBER, root))
        )
        self.assertIsNone(
            self._no_raise(lambda: store_issues.read_ref_summary_cache(OWNER, REPO, NUMBER, root))
        )

    def test_issue_cache_patches_skip_the_file_and_leave_it_alone(self):
        root = self.tmp
        detail = self._plant(store_issues.issue_detail_cache_path(OWNER, REPO, NUMBER, root))
        for state in ("open", "closed"):
            self._plant(store_issues.issues_cache_path(OWNER, REPO, root, state))

        self._no_raise(
            lambda: store_issues.apply_label_change_to_caches(
                OWNER, REPO, NUMBER, [{"name": "bug"}], root=root
            )
        )
        self._no_raise(
            lambda: store_issues.apply_state_change_to_caches(
                OWNER, REPO, NUMBER, "closed", None, root=root
            )
        )
        self._no_raise(
            lambda: store_issues.apply_assignees_change_to_caches(
                OWNER, REPO, NUMBER, ["a"], root=root
            )
        )

        self.assertEqual(detail.read_bytes(), _NON_UTF8_BYTES)

    def test_label_and_member_caches_read_as_a_miss(self):
        root = self.tmp
        self._plant(store_labels.labels_cache_path(OWNER, REPO, root))
        self._plant(store_labels.members_cache_path(OWNER, REPO, root))

        self.assertIsNone(self._no_raise(lambda: store_labels.read_labels_cache(OWNER, REPO, root)))
        self.assertIsNone(
            self._no_raise(lambda: store_labels.read_members_cache(OWNER, REPO, root))
        )
        self.assertIsNone(
            self._no_raise(lambda: store_labels._read_labels_fetched_at(OWNER, REPO, root))
        )

    def test_pull_caches_read_as_a_miss_and_patches_skip(self):
        root = self.tmp
        listing = self._plant(store_pulls.pulls_cache_path(OWNER, REPO, root, "open"))
        self._plant(store_pulls.pulls_cache_path(OWNER, REPO, root, "closed"))
        self._plant(store_pulls.pr_detail_cache_path(OWNER, REPO, NUMBER, root))

        self.assertIsNone(
            self._no_raise(lambda: store_pulls.read_pulls_cache(OWNER, REPO, root, "open"))
        )
        self.assertIsNone(
            self._no_raise(lambda: store_pulls.read_pr_detail_cache(OWNER, REPO, NUMBER, root))
        )
        self._no_raise(
            lambda: store_pulls.apply_pr_checks_to_list_cache(OWNER, REPO, NUMBER, {}, root=root)
        )
        self._no_raise(
            lambda: store_pulls.apply_pr_state_change_to_caches(
                OWNER, REPO, NUMBER, "closed", root=root
            )
        )

        self.assertEqual(listing.read_bytes(), _NON_UTF8_BYTES)

    def test_deps_cache_reads_as_a_miss(self):
        root = self.tmp
        self._plant(store_deps.deps_cache_path(OWNER, REPO, root))

        self.assertIsNone(self._no_raise(lambda: store_deps.read_deps_cache(OWNER, REPO, root)))
        self.assertIsNone(
            self._no_raise(lambda: store_deps._read_deps_fetched_at_raw(OWNER, REPO, root))
        )


if __name__ == "__main__":
    unittest.main()
