"""Tests for the preference store.

The load-bearing tests here are ``test_keyword_fallback_never_dedups`` and
``test_text_change_without_a_model_clears_the_stale_vector``. Both pin behaviour
that silently DESTROYS or MISDIRECTS a user's stored preference when the
embedding model is not serving — which is the store's normal state on a fresh
install, before the model has been downloaded. A regression in either one is
invisible in the UI: the entry count still looks right.
"""

import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins import BUILTIN_NAMES, personal_shopper
from kiro_crew.apps.builtins.personal_shopper.backend import store as store_mod
from kiro_crew.apps.builtins.personal_shopper.backend.store import PreferenceStore


class _FakeEmbedder:
    """Deterministic stand-in: each text maps to a fixed unit-ish vector."""

    def __init__(self, vectors: dict[str, list[float]] | None = None, ready: bool = True):
        self._vectors = vectors or {}
        self._ready = ready

    def is_ready(self) -> bool:
        return self._ready

    def embed(self, text: str) -> list[float] | None:
        return self._vectors.get(text)


def _with_embedder(embedder) -> mock._patch:
    return mock.patch.object(store_mod, "get_shared_embedder", lambda: embedder)


def _no_embedder() -> mock._patch:
    """Simulate a boot where the model has not landed: every embed returns None."""
    return mock.patch.object(store_mod, "get_shared_embedder", lambda: _FakeEmbedder({}))


class PreferenceStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.db = Path(self._tmp) / "preferences.db"
        # Registered FIRST so it runs LAST: unittest runs tearDown before the
        # addCleanup stack, and the stack itself is LIFO, so removing the tree
        # here (rather than in tearDown) guarantees every store.close() added by
        # _store() has already run. Deleting the directory while a sqlite handle
        # is still open fails on Windows and leaves temp directories behind.
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _store(self) -> PreferenceStore:
        s = PreferenceStore(db_path=self.db)
        self.addCleanup(s.close)
        return s


class TestKeywordFallbackSafety(PreferenceStoreTestCase):
    def test_keyword_fallback_never_dedups(self) -> None:
        """Two preferences that merely SHARE A WORD must both survive.

        Deduplication asks "is this the same claim?", which only an embedding can
        answer. A keyword score answers "do these share words?" — thresholding it
        merges "shoe size US 10" into "prefers running shoes" and the user's size
        is gone.
        """
        with _no_embedder():
            store = self._store()
            first = store.add("prefers running shoes")
            second = store.add("shoe size US 10")

            self.assertNotEqual(first, second, "the two preferences were merged")
            texts = sorted(p["text"] for p in store.list_all())
            self.assertEqual(texts, ["prefers running shoes", "shoe size US 10"])

    def test_keyword_results_are_flagged_non_semantic(self) -> None:
        """A keyword hit must not masquerade as a similarity score."""
        with _no_embedder():
            store = self._store()
            store.add("allergic to latex")

            results = store.search("latex")
            self.assertTrue(results, "keyword fallback returned nothing")
            self.assertFalse(
                results[0].semantic,
                "a keyword score was reported as a semantic similarity",
            )

    def test_dedup_rejects_a_non_semantic_hit_even_at_score_one(self) -> None:
        """The guard is the FLAG, not the number.

        bm25 on a tiny corpus can rank a single-term match arbitrarily close to
        zero, which maps to a score of ~1.0. If dedup thresholded the score it
        would merge on that; it must refuse purely because the hit is not
        semantic.
        """
        with _no_embedder():
            store = self._store()
            store.add("prefers running shoes")

            perfect_keyword_hit = [
                store_mod.SearchResult(
                    id="whatever",
                    text="prefers running shoes",
                    tags=[],
                    score=1.0,
                    semantic=False,
                )
            ]
            with mock.patch.object(store, "search", return_value=perfect_keyword_hit):
                self.assertIsNone(
                    store._find_similar("shoe size US 10"),
                    "dedup accepted a keyword hit scoring 1.0",
                )

    def test_fts_query_with_operator_characters_does_not_error(self) -> None:
        """Raw user text containing FTS5 syntax must still search."""
        with _no_embedder():
            store = self._store()
            store.add("avoids wool sweaters")

            # An unbalanced quote and a bare operator are a query-syntax error to
            # FTS5 if passed through unescaped.
            for probe in ['wool "', "wool OR", "wool*", '"']:
                with self.subTest(probe=probe):
                    store.search(probe)  # must not raise


class TestSemanticDedup(PreferenceStoreTestCase):
    def test_high_cosine_merges_into_the_existing_entry(self) -> None:
        vectors = {
            "shoe size US 10": [1.0, 0.0, 0.0],
            "shoe size is US 10": [0.999, 0.01, 0.0],
        }
        with _with_embedder(_FakeEmbedder(vectors)):
            store = self._store()
            first = store.add("shoe size US 10")
            second = store.add("shoe size is US 10")

            self.assertEqual(first, second, "a restatement created a second entry")
            self.assertEqual(len(store.list_all()), 1)

    def test_low_cosine_keeps_both(self) -> None:
        vectors = {
            "shoe size US 10": [1.0, 0.0, 0.0],
            "allergic to latex": [0.0, 1.0, 0.0],
        }
        with _with_embedder(_FakeEmbedder(vectors)):
            store = self._store()
            store.add("shoe size US 10")
            store.add("allergic to latex")

            self.assertEqual(len(store.list_all()), 2)


class TestVectorStaleness(PreferenceStoreTestCase):
    def test_text_change_without_a_model_clears_the_stale_vector(self) -> None:
        """A vector describing replaced text must not survive the edit.

        Keeping it means the entry keeps matching queries about wording the user
        has deleted — a silent recall corruption that no UI surface reveals.
        """
        vectors = {"budget under $100": [1.0, 0.0, 0.0]}
        with _with_embedder(_FakeEmbedder(vectors)):
            store = self._store()
            entry_id = store.add("budget under $100")

        # The model goes away, then the user edits the text.
        with _no_embedder():
            store.update(entry_id, text="budget under $400")

        with sqlite3.connect(str(self.db)) as conn:
            text, blob = conn.execute(
                "SELECT text, embedding FROM preferences WHERE id = ?", (entry_id,)
            ).fetchone()
        self.assertEqual(text, "budget under $400")
        self.assertIsNone(blob, "the vector for the OLD text was kept")

    def test_reembed_all_refills_cleared_vectors(self) -> None:
        with _no_embedder():
            store = self._store()
            store.add("prefers minimalist style")

        with _with_embedder(_FakeEmbedder({"prefers minimalist style": [0.0, 1.0]})):
            self.assertEqual(store.reembed_all(), 1)

        with sqlite3.connect(str(self.db)) as conn:
            (blob,) = conn.execute("SELECT embedding FROM preferences").fetchone()
        self.assertIsNotNone(blob)

    def test_dimension_mismatch_scores_zero_rather_than_truncating(self) -> None:
        """Vectors from two different models are incomparable, not 'similar'.

        Truncating to the shorter length would invent a similarity from a prefix.
        """
        self.assertEqual(store_mod._cosine([1.0, 0.0], [1.0, 0.0, 0.0]), 0.0)


class TestFtsIndexSync(PreferenceStoreTestCase):
    def test_update_replaces_the_indexed_text(self) -> None:
        """After an edit the OLD wording must no longer be findable."""
        with _no_embedder():
            store = self._store()
            entry_id = store.add("prefers wool coats")
            store.update(entry_id, text="prefers down jackets")

            self.assertEqual([r.text for r in store.search("wool")], [])
            self.assertEqual(
                [r.text for r in store.search("down")], ["prefers down jackets"]
            )

    def test_delete_removes_from_the_index(self) -> None:
        with _no_embedder():
            store = self._store()
            entry_id = store.add("avoids leather")
            store.delete(entry_id)

            self.assertEqual(store.search("leather"), [])
            self.assertEqual(store.list_all(), [])


class TestTagsAreOrganisationOnly(PreferenceStoreTestCase):
    def test_deleting_a_group_keeps_its_preferences(self) -> None:
        """A group is the user's filing, not the knowledge itself."""
        with _no_embedder():
            store = self._store()
            # The id is opaque (a slug collapses "Work Shoes" and "work-shoes"
            # onto one key), so the group's own id is the only handle.
            group_id = store.add_group("Body")
            entry_id = store.add("shoe size US 10", tags=[group_id])

            store.delete_group(group_id)

            remaining = store.list_all()
            self.assertEqual(len(remaining), 1, "deleting a folder deleted the fact")
            self.assertEqual(remaining[0]["id"], entry_id)
            self.assertEqual(remaining[0]["tags"], [], "the stale tag was left behind")
            self.assertEqual(store.list_groups(), [])

    def test_search_ignores_tags_by_default(self) -> None:
        """An untagged preference must still be retrievable."""
        with _no_embedder():
            store = self._store()
            store.add("allergic to latex")  # no tags at all

            self.assertEqual(
                [r.text for r in store.search("latex")], ["allergic to latex"]
            )


class TestDataHomeIsResolvedLazily(unittest.TestCase):
    def test_default_path_follows_the_active_data_home(self) -> None:
        """The store must land in the ACTIVE home, not the one set at import.

        A module-level constant would bind the importing process's home, sending
        a pod's or a test's writes into the real user's data.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Patch the name the module calls. The import is module-level, so the
            # FUNCTION is bound at import while the DIRECTORY is only resolved
            # when it is called -- which is the property under test: if the path
            # were computed into a constant at import, replacing the function
            # here would not change the result.
            with mock.patch.object(
                store_mod, "app_data_dir", return_value=Path(tmp)
            ):
                self.assertEqual(
                    store_mod._default_db_path(), Path(tmp) / "preferences.db"
                )


class TestConcurrentWrites(PreferenceStoreTestCase):
    def test_parallel_adds_all_land(self) -> None:
        """Route handlers run on several worker threads over one connection.

        Without serialisation the interleaved multi-statement FTS sync races and
        sqlite raises 'database is locked'.
        """
        with _no_embedder():
            store = self._store()
            errors: list[BaseException] = []

            def add(i: int) -> None:
                try:
                    store.add(f"distinct preference number {i}")
                except BaseException as exc:  # noqa: BLE001 - recorded and re-raised
                    errors.append(exc)

            threads = [threading.Thread(target=add, args=(i,)) for i in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], f"concurrent writes failed: {errors}")
            self.assertEqual(len(store.list_all()), 12)


class TestHistory(PreferenceStoreTestCase):
    def test_history_round_trips_with_feedback(self) -> None:
        with _no_embedder():
            store = self._store()
            hid = store.add_history(
                "half marathon with bad knees",
                advice="high-cushion shoe, not a barefoot model",
                products=[{"name": "Pegasus 41", "price": 129}],
            )
            store.update_feedback(hid, "Pegasus 41", "purchased")

            (session,) = store.list_history()
            self.assertEqual(session["problem"], "half marathon with bad knees")
            self.assertEqual(session["products"][0]["name"], "Pegasus 41")
            self.assertEqual(session["feedback"], {"Pegasus 41": "purchased"})

    def test_advice_only_session_has_no_products(self) -> None:
        """A session that solved the problem WITHOUT a purchase is a valid one."""
        with _no_embedder():
            store = self._store()
            store.add_history("neck ache at my desk", advice="raise the monitor")

            (session,) = store.list_history()
            self.assertEqual(session["products"], [])
            self.assertTrue(session["advice"])


class TestManifestWiring(unittest.TestCase):
    """The app is invisible unless it is registered and its routes are reachable."""

    def test_app_is_in_builtin_names(self) -> None:
        self.assertIn("personal_shopper", BUILTIN_NAMES)

    def test_package_re_exports_register_routes(self) -> None:
        """server.py checks ``hasattr(pkg, 'register_routes')`` on the PACKAGE."""
        self.assertTrue(hasattr(personal_shopper, "register_routes"))

    def test_manifest_declares_its_agent_and_skill(self) -> None:
        """Without these fields the advisor agent and the skill never install."""
        manifest_path = (
            Path(__file__).resolve().parents[1] / "app.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["agents"], ["agents/advisor.json"])
        self.assertEqual(manifest["skills"], ["skills/personal-shopper"])

        app_root = manifest_path.parent
        for rel in manifest["agents"]:
            self.assertTrue((app_root / rel).is_file(), f"missing agent file {rel}")
        for rel in manifest["skills"]:
            self.assertTrue(
                (app_root / rel / "SKILL.md").is_file(), f"missing SKILL.md in {rel}"
            )

    def test_declared_assets_exist(self) -> None:
        """A manifest that names an icon it does not ship renders a blank card."""
        manifest_path = Path(__file__).resolve().parents[1] / "app.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # tests/ -> personal_shopper -> builtins -> apps -> kiro_crew -> src -> repo
        repo_root = Path(__file__).resolve().parents[6]
        public = repo_root / "website" / "public"
        for field in ("iconUrl", "heroImage", "heroImageDark"):
            rel = manifest[field].lstrip("/")
            self.assertTrue((public / rel).is_file(), f"{field} -> {rel} is missing")


class TestGroupAssignmentSurvives(PreferenceStoreTestCase):
    """A group is a tag holding the group's id, so tag handling IS group handling."""

    def test_dedup_does_not_strip_an_entry_out_of_its_group(self) -> None:
        """Re-adding an equivalent preference must not un-file the original.

        ``add`` merges into a semantically equivalent entry. Passing the incoming
        tags straight through replaced the stored ones, so adding "shoe size US 10"
        again WITHOUT picking a group silently removed the existing entry from the
        group it was filed under. The entry survives, so the count looks right --
        it just disappears from its section, which reads as data loss.
        """
        vectors = {"shoe size US 10": [1.0, 0.0], "shoe size US10": [1.0, 0.0]}
        with _with_embedder(_FakeEmbedder(vectors)):
            store = self._store()
            group_id = store.add_group("Body")
            entry_id = store.add("shoe size US 10", tags=[group_id])

            # Same claim, no group picked.
            again = store.add("shoe size US10", tags=[])

            self.assertEqual(again, entry_id, "expected a dedup merge")
            stored = {e["id"]: e for e in store.list_all()}
            self.assertIn(group_id, stored[entry_id]["tags"],
                          "dedup erased the group assignment")

    def test_dedup_unions_rather_than_discarding_new_tags(self) -> None:
        vectors = {"budget is $100": [0.0, 1.0], "budget about $100": [0.0, 1.0]}
        with _with_embedder(_FakeEmbedder(vectors)):
            store = self._store()
            first = store.add_group("Money")
            second = store.add_group("Gifts")
            entry_id = store.add("budget is $100", tags=[first])

            store.add("budget about $100", tags=[second])

            stored = {e["id"]: e for e in store.list_all()}
            self.assertEqual(sorted(stored[entry_id]["tags"]), sorted([first, second]))


class TestGroupIdsAreOpaque(PreferenceStoreTestCase):
    def test_two_names_that_slug_alike_stay_distinct(self) -> None:
        """``Work Shoes`` and ``work-shoes`` both slug to ``work-shoes``.

        With a slug id and ``INSERT OR REPLACE`` the second add destroyed the
        first group's metadata AND reassigned every preference tagged with that
        id into the second group.
        """
        with _no_embedder():
            store = self._store()
            a = store.add_group("Work Shoes")
            b = store.add_group("work-shoes")

            self.assertNotEqual(a, b, "distinct names collapsed onto one id")
            self.assertEqual(len(store.list_groups()), 2)

    def test_adding_the_same_name_twice_returns_the_same_group(self) -> None:
        """Opaque ids must not cost the idempotency the slug provided."""
        with _no_embedder():
            store = self._store()
            first = store.add_group("Body")
            second = store.add_group("  body  ")

            self.assertEqual(first, second)
            self.assertEqual(len(store.list_groups()), 1)

    def test_a_group_id_is_not_derived_from_its_name(self) -> None:
        with _no_embedder():
            store = self._store()
            group_id = store.add_group("Work Shoes")
            self.assertNotEqual(group_id, "work-shoes")


if __name__ == "__main__":
    unittest.main()
