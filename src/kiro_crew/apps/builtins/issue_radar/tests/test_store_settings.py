"""Tests for issue_radar per-repo triage settings + disconnect (store.py).

Locks in the settings feature's storage contract: settings live inline in each
repo's config.json entry, are normalized on write (unknown keys dropped, label
lists coerced to de-duplicated strings), default to the backwards-compatible
"unlabeled == untriaged" heuristic, and survive permission/reconnect writes.
Disconnect is local-only: it removes the config entry AND the cache dir but
never touches GitHub.
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from kiro_crew.apps.builtins.issue_radar.backend import store


class TestRepoSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_when_unset(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        s = store.read_repo_settings("o", "r", self.tmp)
        self.assertEqual(s, store.DEFAULT_REPO_SETTINGS)
        self.assertTrue(s["unlabeled_is_untriaged"])
        self.assertEqual(s["triage_labels"], [])
        self.assertEqual(s["good_first_issue_labels"], [])

    def test_defaults_for_unconnected_repo(self):
        s = store.read_repo_settings("no", "pe", self.tmp)
        self.assertEqual(s, store.DEFAULT_REPO_SETTINGS)

    def test_write_then_read_roundtrip(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        saved = store.write_repo_settings(
            "o", "r",
            {
                "triage_labels": ["needs-triage", "triage"],
                "unlabeled_is_untriaged": False,
                "good_first_issue_labels": ["good first issue"],
            },
            root=self.tmp,
        )
        self.assertEqual(saved["triage_labels"], ["needs-triage", "triage"])
        self.assertFalse(saved["unlabeled_is_untriaged"])
        self.assertEqual(saved["good_first_issue_labels"], ["good first issue"])
        self.assertEqual(store.read_repo_settings("o", "r", self.tmp), saved)

    def test_normalize_dedups_trims_and_drops_unknown(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        saved = store.write_repo_settings(
            "o", "r",
            {
                "triage_labels": ["a", "a", " b ", "", 5, None],
                "good_first_issue_labels": "notalist",
                "junk": "dropped",
            },
            root=self.tmp,
        )
        self.assertEqual(saved["triage_labels"], ["a", "b"])
        self.assertEqual(saved["good_first_issue_labels"], [])
        self.assertNotIn("junk", saved)
        # missing toggle falls back to the safe default
        self.assertTrue(saved["unlabeled_is_untriaged"])

    def test_write_unconnected_raises(self):
        with self.assertRaises(KeyError):
            store.write_repo_settings("no", "pe", {"triage_labels": ["x"]}, root=self.tmp)

    def test_settings_survive_permissions_update(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_repo_settings("o", "r", {"triage_labels": ["x"]}, root=self.tmp)
        store.set_repo_permissions("o", "r", {"push": True}, root=self.tmp)
        self.assertEqual(store.read_repo_settings("o", "r", self.tmp)["triage_labels"], ["x"])

    def test_reconnect_preserves_settings(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_repo_settings("o", "r", {"triage_labels": ["x"]}, root=self.tmp)
        # An idempotent reconnect only refreshes permissions; it must not wipe settings.
        store.add_connected_repo("o", "r", permissions={"push": True}, root=self.tmp)
        self.assertEqual(store.read_repo_settings("o", "r", self.tmp)["triage_labels"], ["x"])

    def test_notify_on_new_issue_defaults_false(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        self.assertFalse(store.read_repo_settings("o", "r", self.tmp)["notify_on_new_issue"])

    def test_notify_on_new_issue_roundtrip(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        saved = store.write_repo_settings("o", "r", {"notify_on_new_issue": True}, root=self.tmp)
        self.assertTrue(saved["notify_on_new_issue"])
        self.assertTrue(store.read_repo_settings("o", "r", self.tmp)["notify_on_new_issue"])


class TestWatchState(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_absent_returns_empty(self):
        # Never-observed repo → {} so the watcher seeds without notifying.
        self.assertEqual(store.read_watch_state("o", "r", self.tmp), {})

    def test_roundtrip(self):
        store.write_watch_state("o", "r", 42, root=self.tmp)
        self.assertEqual(store.read_watch_state("o", "r", self.tmp)["last_seen_number"], 42)

    def test_removed_with_repo_cache(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_watch_state("o", "r", 5, root=self.tmp)
        self.assertTrue(store.remove_connected_repo("o", "r", root=self.tmp))
        self.assertEqual(store.read_watch_state("o", "r", self.tmp), {})


class TestDisconnect(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_remove_drops_repo_and_cache(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_issues_cache("o", "r", [{"number": 1}], root=self.tmp)
        cache_dir = store.repo_data_dir("o", "r", self.tmp)
        self.assertTrue(cache_dir.exists())
        self.assertTrue(store.remove_connected_repo("o", "r", root=self.tmp))
        self.assertFalse(store.is_repo_connected("o", "r", self.tmp))
        self.assertFalse(cache_dir.exists())

    def test_remove_unconnected_returns_false(self):
        self.assertFalse(store.remove_connected_repo("no", "pe", root=self.tmp))

    def test_remove_leaves_other_repos_intact(self):
        store.add_connected_repo("o", "r1", root=self.tmp)
        store.add_connected_repo("o", "r2", root=self.tmp)
        store.remove_connected_repo("o", "r1", root=self.tmp)
        self.assertFalse(store.is_repo_connected("o", "r1", self.tmp))
        self.assertTrue(store.is_repo_connected("o", "r2", self.tmp))


class TestRecommendationsAndLabelCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recommendations_roundtrip(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        self.assertIsNone(store.read_recommendations_cache("o", "r", self.tmp))
        payload = {
            "recommendations": [
                {"name": "priority: high", "category": "priority", "color": "d73a4a",
                 "description": "", "rationale": "", "examples": [1, 2]}
            ],
            "generated_at": "2026-07-23T00:00:00Z",
        }
        store.write_recommendations_cache("o", "r", payload, root=self.tmp)
        got = store.read_recommendations_cache("o", "r", self.tmp)
        assert got is not None
        self.assertEqual(got["recommendations"][0]["name"], "priority: high")
        self.assertEqual(got["generated_at"], "2026-07-23T00:00:00Z")

    def test_two_languages_do_not_evict_each_other(self):
        """The partition is what makes a per-browser output language safe.

        `rationale` prose is written in one language, so a set is only meaningful
        for that language. With ONE shared slot, two browsers resolving different
        languages would each read the other's set as absent and each regenerate
        would overwrite it -- paid model output discarded back and forth, with the
        panel just showing "none generated yet". Separate partitions remove it.
        """
        store.add_connected_repo("o", "r", root=self.tmp)
        de = {"recommendations": [{"name": "bereich: auth"}], "generated_at": "t1"}
        pt = {"recommendations": [{"name": "area: auth"}], "generated_at": "t2"}
        store.write_recommendations_cache("o", "r", de, root=self.tmp, ui_language="de")
        store.write_recommendations_cache("o", "r", pt, root=self.tmp, ui_language="pt")

        got_de = store.read_recommendations_cache("o", "r", self.tmp, ui_language="de")
        got_pt = store.read_recommendations_cache("o", "r", self.tmp, ui_language="pt")
        assert got_de is not None and got_pt is not None
        # The pt write did not destroy the de set.
        self.assertEqual(got_de["recommendations"][0]["name"], "bereich: auth")
        self.assertEqual(got_pt["recommendations"][0]["name"], "area: auth")
        # A language with nothing generated reads as absent, not as another's set.
        self.assertIsNone(store.read_recommendations_cache("o", "r", self.tmp, ui_language="ja"))

    def test_an_unconfigured_install_keeps_the_legacy_path(self):
        # "" is the no-language-known case and must land on the ORIGINAL filename,
        # so a cache written before partitioning is still found with no migration.
        store.add_connected_repo("o", "r", root=self.tmp)
        legacy = store.recommendations_cache_path("o", "r", self.tmp)
        self.assertEqual(legacy.name, "recommendations-cache.json")
        self.assertEqual(
            store.recommendations_cache_path("o", "r", self.tmp, ui_language="").name,
            legacy.name,
        )
        self.assertEqual(
            store.recommendations_cache_path("o", "r", self.tmp, ui_language="zh-CN").name,
            "recommendations-cache-zh-CN.json",
        )

    def test_add_label_to_cache_appends_when_cache_exists(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_labels_cache("o", "r", [{"name": "bug", "color": "d73a4a", "description": ""}], root=self.tmp)
        store.add_label_to_cache("o", "r", {"name": "priority: high", "color": "d93f0b", "description": "x"}, root=self.tmp)
        cached = store.read_labels_cache("o", "r", self.tmp)
        assert cached is not None
        names = [lab["name"] for lab in cached]
        self.assertIn("priority: high", names)
        self.assertIn("bug", names)

    def test_add_label_to_cache_dedups(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_labels_cache("o", "r", [{"name": "bug", "color": "d73a4a", "description": ""}], root=self.tmp)
        store.add_label_to_cache("o", "r", {"name": "bug", "color": "000000", "description": "dup"}, root=self.tmp)
        cached = store.read_labels_cache("o", "r", self.tmp)
        assert cached is not None
        self.assertEqual(len(cached), 1)

    def test_add_label_to_cache_noop_when_no_cache(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        # No labels cache exists — must NOT create a partial 1-label cache that
        # would mask the real set until a refresh.
        store.add_label_to_cache("o", "r", {"name": "x", "color": "d93f0b", "description": ""}, root=self.tmp)
        self.assertIsNone(store.read_labels_cache("o", "r", self.tmp))

    def test_the_label_ttl_is_pinned_to_its_documented_value(self):
        # Every other test here derives its aging FROM this constant, so they stay green
        # for any value at all: raising it to a year would leave the cache effectively
        # unbounded (the defect the TTL exists to fix) with no test objecting. The spec
        # states 600s, so the number itself is pinned.
        self.assertEqual(store.LABELS_CACHE_TTL_SEC, 600.0)

    def test_an_expired_labels_cache_reads_as_a_MISS(self):
        # The cache had no expiry at all, and nothing polls the /labels query, so the
        # first fetch of a repo was served forever and a label created on GitHub
        # afterwards never appeared. That reads as a TRUNCATED label list, not a stale
        # one: nothing on screen says the set is incomplete and the missing labels are
        # silently unfilterable.
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_labels_cache(
            "o", "r", [{"name": "bug", "color": "d73a4a", "description": ""}], root=self.tmp,
        )
        self.assertIsNotNone(store.read_labels_cache("o", "r", self.tmp))
        path = store.labels_cache_path("o", "r", self.tmp)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["fetched_at"] = time.time() - (store.LABELS_CACHE_TTL_SEC + 1)
        path.write_text(json.dumps(data), encoding="utf-8")
        # A miss, so the route refetches and picks up labels created since.
        self.assertIsNone(store.read_labels_cache("o", "r", self.tmp))
        # Still readable by the write-through patch, which must not drop an append
        # just because the file it is patching is due a refetch.
        self.assertIsNotNone(store.read_labels_cache("o", "r", self.tmp, max_age_sec=None))

    def test_appending_a_label_does_not_RESET_the_cache_age(self):
        # `add_label_to_cache` rewrites the file without refetching, so re-stamping it
        # would mean a repo whose labels are edited regularly never ages its cache out,
        # exactly the staleness the TTL exists to bound. Same payload-vs-mtime reasoning
        # as the list caches' `_list_cache_age_sec`.
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_labels_cache(
            "o", "r", [{"name": "bug", "color": "d73a4a", "description": ""}], root=self.tmp,
        )
        path = store.labels_cache_path("o", "r", self.tmp)
        data = json.loads(path.read_text(encoding="utf-8"))
        aged = time.time() - (store.LABELS_CACHE_TTL_SEC - 5)
        data["fetched_at"] = aged
        path.write_text(json.dumps(data), encoding="utf-8")
        store.add_label_to_cache(
            "o", "r", {"name": "new", "color": "d93f0b", "description": ""}, root=self.tmp,
        )
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertAlmostEqual(after["fetched_at"], aged, places=3)
        # The append still landed.
        self.assertIn("new", [lab["name"] for lab in after["labels"]])

    def test_appending_to_a_PRESTAMP_cache_does_not_reset_its_TTL_clock(self):
        # A pre-stamp file was written only by a real fetch, so its mtime IS its fetch
        # time. Treating the missing stamp as "stamp it now" reset the clock on every
        # append: a cache nine minutes into its ten-minute life got a fresh ten, and one
        # label creation per interval could defer the refetch indefinitely.
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_labels_cache(
            "o", "r", [{"name": "bug", "color": "d73a4a", "description": ""}], root=self.tmp,
        )
        path = store.labels_cache_path("o", "r", self.tmp)
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["fetched_at"]
        path.write_text(json.dumps(data), encoding="utf-8")
        aged = time.time() - (store.LABELS_CACHE_TTL_SEC - 60)
        os.utime(path, (aged, aged))
        store.add_label_to_cache(
            "o", "r", {"name": "new", "color": "d93f0b", "description": ""}, root=self.tmp,
        )
        # The carried-over stamp reflects the ORIGINAL fetch, not the append.
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertAlmostEqual(after["fetched_at"], aged, delta=2)
        self.assertIn("new", [lab["name"] for lab in after["labels"]])
        # And it still expires on the original schedule rather than a renewed one.
        data = json.loads(path.read_text(encoding="utf-8"))
        data["fetched_at"] = time.time() - (store.LABELS_CACHE_TTL_SEC + 1)
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(store.read_labels_cache("o", "r", self.tmp))

    def test_a_prestamp_labels_cache_falls_back_to_mtime(self):
        # A cache written before `fetched_at` existed was only ever written by a real
        # fetch, so its mtime IS its fetch time; it must not be treated as ageless.
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_labels_cache(
            "o", "r", [{"name": "bug", "color": "d73a4a", "description": ""}], root=self.tmp,
        )
        path = store.labels_cache_path("o", "r", self.tmp)
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["fetched_at"]
        path.write_text(json.dumps(data), encoding="utf-8")
        old = time.time() - (store.LABELS_CACHE_TTL_SEC + 60)
        os.utime(path, (old, old))
        self.assertIsNone(store.read_labels_cache("o", "r", self.tmp))


if __name__ == "__main__":
    unittest.main()
