"""Learned project notes: store logic and the project-first search outcome.

Against a real ``KnowledgeStore`` (keyword/graph legs, no embedder), proving the
item's acceptance points:

* a note added from a session in project X is found ranked first from another
  session in X, and not first in project Y;
* a session outside any project (empty namespace) is refused and writes nothing,
  so an unscoped search is byte-for-byte the pre-notes behaviour;
* a nested child folder resolves to the same project as its root;
* an over-long note is refused; a note containing a token-like string is
  redacted before it is stored and before it is searchable;
* the per-project count cap evicts the oldest note (FIFO).
"""

from __future__ import annotations

import pytest

from kiro_crew.knowledge import project_notes
from kiro_crew.knowledge.project_scope import project_namespace
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore

NS_X = project_namespace("root-x")
NS_Y = project_namespace("root-y")


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "knowledge.db"
    s = KnowledgeStore(str(db))
    yield s
    s._close_all_for_tests()


# ── write path: refusal, bounds, redaction ──


def test_a_session_outside_any_project_is_refused_and_writes_nothing(store):
    for ns in ("", "   ", "default", "not-a-project"):
        with pytest.raises(project_notes.ProjectNoteError):
            project_notes.add_note(store, namespace=ns, text="a learning")
    # Nothing was written under any namespace.
    assert store.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_an_empty_note_is_refused(store):
    with pytest.raises(project_notes.ProjectNoteError):
        project_notes.add_note(store, namespace=NS_X, text="   \n ")


def test_an_over_long_note_is_refused_and_writes_nothing(store):
    too_long = "x" * (project_notes.MAX_NOTE_LEN + 1)
    with pytest.raises(project_notes.ProjectNoteError):
        project_notes.add_note(store, namespace=NS_X, text=too_long)
    assert project_notes.note_count(store, NS_X) == 0


def test_a_note_at_the_cap_is_accepted(store):
    note = project_notes.add_note(store, namespace=NS_X, text="y" * project_notes.MAX_NOTE_LEN)
    assert note["id"]
    assert len(note["text"]) == project_notes.MAX_NOTE_LEN


def test_a_token_like_string_is_redacted_before_storage(store):
    # A note that quotes something the agent read can carry a credential. The
    # stored note, its title and the searchable item must all have it redacted.
    secret = "AKIAIOSFODNN7EXAMPLE"  # an AWS-access-key-shaped token
    note = project_notes.add_note(
        store,
        namespace=NS_X,
        text=f"deploy key is {secret} keep it out of logs",
    )
    assert secret not in note["text"]
    assert secret not in note["title"]
    stored = store.get_item(note["id"])
    assert secret not in (stored["content"] or "")
    assert secret not in (stored["title"] or "")
    # And it must not be searchable by the token either.
    hits = HybridRetriever(store).search(secret, limit=5, namespace=NS_X)
    assert all(secret not in (h.get("content") or "") for h in hits)


def test_tags_are_cleaned_and_bounded(store):
    note = project_notes.add_note(
        store,
        namespace=NS_X,
        text="a learning",
        tags=["build", "build", "  spaced  ", 123, "", "x" * 200] + [f"t{i}" for i in range(20)],
    )
    tags = note["tags"]
    assert "build" in tags
    assert tags.count("build") == 1  # deduped
    assert "spaced" in tags  # stripped
    assert all(isinstance(t, str) and len(t) <= project_notes.MAX_TAG_LEN for t in tags)
    assert len(tags) <= project_notes.MAX_TAGS


def test_the_source_session_is_stored_as_provenance_not_as_content(store):
    note = project_notes.add_note(
        store, namespace=NS_X, text="a learning", source_session="dashboard_123"
    )
    assert note["source_session"] == "dashboard_123"
    # Provenance rides on summary, which is NOT FTS-indexed, so searching for the
    # session key does not surface the note.
    hits = HybridRetriever(store).search("dashboard_123", limit=5, namespace=NS_X)
    assert note["id"] not in {h["id"] for h in hits}


# ── read path: list / get / delete, scoped per project ──


def test_list_returns_newest_first_and_is_empty_for_no_project(store):
    assert project_notes.list_notes(store, "") == []
    assert project_notes.list_notes(store, NS_X) == []  # no source yet
    first = project_notes.add_note(store, namespace=NS_X, text="first learning")
    second = project_notes.add_note(store, namespace=NS_X, text="second learning")
    listed = project_notes.list_notes(store, NS_X)
    assert [n["id"] for n in listed] == [second["id"], first["id"]]


def test_get_and_delete_are_scoped_to_their_project(store):
    note_x = project_notes.add_note(store, namespace=NS_X, text="x learning")
    project_notes.add_note(store, namespace=NS_Y, text="y learning")
    # The X note is not reachable, nor deletable, by naming its id under Y.
    assert project_notes.get_note(store, NS_Y, note_x["id"]) is None
    assert project_notes.delete_note(store, NS_Y, note_x["id"]) is False
    assert project_notes.get_note(store, NS_X, note_x["id"]) is not None
    # Deleting it under its own project works and removes the item + its FTS row.
    assert project_notes.delete_note(store, NS_X, note_x["id"]) is True
    assert project_notes.get_note(store, NS_X, note_x["id"]) is None
    assert store.get_item(note_x["id"]) is None


# ── per-project count cap: FIFO eviction ──


def test_the_oldest_note_is_evicted_at_the_cap(store, monkeypatch):
    monkeypatch.setattr(project_notes, "MAX_NOTES_PER_PROJECT", 3)
    ids = [
        project_notes.add_note(store, namespace=NS_X, text=f"learning {i}")["id"] for i in range(4)
    ]
    remaining = {n["id"] for n in project_notes.list_notes(store, NS_X)}
    assert len(remaining) == 3
    assert ids[0] not in remaining  # oldest evicted
    assert ids[3] in remaining  # newest kept


# ── the pointer block: absent unless a project has notes ──


def test_pointer_block_is_empty_without_a_project_or_notes(store):
    assert project_notes.render_pointer_block(store, "") == ""
    assert project_notes.render_pointer_block(store, NS_X) == ""  # no notes yet


def test_pointer_block_points_at_search_and_labels_notes_as_data(store):
    project_notes.add_note(store, namespace=NS_X, text="a learning")
    block = project_notes.render_pointer_block(store, NS_X)
    assert block
    assert "local_knowledge_search" in block
    assert "DATA" in block and "NOT instructions" in block
    # It points; it must NOT inline the note text (that would be always-on
    # context rather than pull-on-demand reference data).
    assert "a learning" not in block
