"""Multi-group preferences must survive an edit.

The Preferences tab edits a preference's group through a SINGLE select, but a
preference can carry several tags. Two properties have to hold or editing
silently un-groups it:

* ``update(text=...)`` with ``tags`` omitted must leave the tag column alone —
  this is what lets a text-only edit avoid rewriting tags at all;
* a caller that DOES change the group must be able to keep the remaining tags,
  because the store treats the array it is handed as authoritative (a short
  array is a deletion, not a partial update).

Reuses ``PreferenceStoreTestCase`` for its Windows-safe cleanup ordering rather
than opening a store by hand.
"""

from .test_store import PreferenceStoreTestCase


class TestUpdatePreservesTags(PreferenceStoreTestCase):
    def _tags_of(self, store, entry_id: str) -> list[str]:
        for row in store.list_all():
            if row["id"] == entry_id:
                return list(row["tags"])
        raise AssertionError("entry vanished")

    def test_text_only_update_leaves_every_tag_intact(self) -> None:
        """Omitting `tags` must not clear a multi-group preference."""
        store = self._store()
        entry = store.add("shoe size US 10", tags=["g1", "g2", "g3"])
        store.update(entry, text="shoe size US 10.5")
        self.assertEqual(self._tags_of(store, entry), ["g1", "g2", "g3"])

    def test_regrouping_can_keep_the_remaining_tags(self) -> None:
        """Changing the first tag while preserving the tail is representable.

        This is the exact payload the edit form now sends: ``[newFirst, *rest]``.
        """
        store = self._store()
        entry = store.add("prefers wool", tags=["g1", "g2"])
        store.update(entry, text="prefers wool", tags=["gNEW", "g2"])
        self.assertEqual(self._tags_of(store, entry), ["gNEW", "g2"])

    def test_a_short_array_is_a_deletion_not_a_merge(self) -> None:
        """Documents WHY the form must send the tail explicitly.

        If this ever became a merge, the frontend's tail-preserving payload
        would be redundant rather than load-bearing — so the assertion records
        the contract the fix depends on.
        """
        store = self._store()
        entry = store.add("size 42", tags=["g1", "g2"])
        store.update(entry, text="size 42", tags=["g1"])
        self.assertEqual(self._tags_of(store, entry), ["g1"])
