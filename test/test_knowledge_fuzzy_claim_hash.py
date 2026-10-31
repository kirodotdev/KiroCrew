"""A dedup collapse records the hash of the copy it moved a claim onto.

A fuzzy collapse merges two documents whose text differs. The loser's row keeps its
own hash, which names no item once the loser's copy is gone, while its source stays
a location of the winner's items. Every release and adoption resolves the claim by
hash, so the collapse writes the winner's hash to ``claimed_hash`` and they use it.
A row without one (never collapsed, or written before the column) behaves as before.
"""

import asyncio
import hashlib
import json
import os

import pytest

from kiro_crew.knowledge import agent_source, artifact_ingest
from kiro_crew.knowledge.dedup import DocRef, _collapse_doc, dedup_sweep
from kiro_crew.knowledge.embedder import floats_to_bytes
from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.store import KnowledgeStore


class _Pipeline:
    """Pipeline stand-in that stores each file's text as one item, hashed as the
    real pipeline hashes it."""

    def __init__(self, store, source_id):
        self.store = store
        self.source_id = source_id

    async def ingest_file(self, path, *, on_committed=None, **_kwargs):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        item_id = self.store.add_item(
            os.path.basename(path),
            text,
            "doc",
            source_id=self.source_id,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        )
        if on_committed is not None:
            on_committed([item_id])
        return "job"


def _folder(store, path):
    path.mkdir(parents=True, exist_ok=True)
    sid = store.add_source(path.name, "local_folder", str(path))
    return {
        "id": sid,
        "uri": str(path),
        "source_type": "local_folder",
        "properties": json.dumps({}),
    }


def _scan(store, source):
    asyncio.run(FolderWatcher(store, _Pipeline(store, source["id"])).scan_source(source))


def _untracked(store, source_id):
    """Items the source owns that none of its state rows names."""
    named = set()
    for table in ("folder_file_state", "artifact_item_state", "agent_item_state"):
        for r in store.db.execute(
            f"SELECT item_ids FROM {table} WHERE source_id = ?", (source_id,)  # noqa: S608
        ):
            named.update(json.loads(r["item_ids"] or "[]"))
    owned = {
        r["id"] for r in store.db.execute("SELECT id FROM items WHERE source_id = ?", (source_id,))
    }
    return owned - named


@pytest.fixture
def fuzzy_pair(tmp_path):
    """Two folder files with the same name and embedding but different text,
    fuzzy-collapsed: ``loser`` (older) now claims ``winner``'s item."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    loser = _folder(store, tmp_path / "a")
    winner = _folder(store, tmp_path / "b")
    loser_file = tmp_path / "a" / "notes.md"
    loser_file.write_text("weekly notes\nfirst draft\n", encoding="utf-8")
    (tmp_path / "b" / "notes.md").write_text("weekly notes\nfinal draft\n", encoding="utf-8")
    os.utime(loser_file, (100, 100))
    os.utime(tmp_path / "b" / "notes.md", (300, 300))
    _scan(store, loser)
    _scan(store, winner)
    store.db.execute(
        "UPDATE items SET embedding = ?, embedding_sig = 'sig'", (floats_to_bytes([1.0, 0.0, 0.0]),)
    )
    store.db.commit()
    (winner_item,) = [
        r["id"]
        for r in store.db.execute("SELECT id FROM items WHERE source_id = ?", (winner["id"],))
    ]
    winner_hash = store.get_item(winner_item)["content_hash"]
    loser_hash = store.db.execute(
        "SELECT content_hash FROM items WHERE source_id = ?", (loser["id"],)
    ).fetchone()[0]
    assert loser_hash != winner_hash, "precondition: a FUZZY pair, not an exact one"

    actions = dedup_sweep(store, apply=True)

    assert [a["loser_source_id"] for a in actions] == [loser["id"]]
    assert loser["id"] in store.sources_holding_item(winner_item)
    try:
        yield store, loser, winner, loser_file, winner_item, winner_hash
    finally:
        store.close()


def _loser_row(store, loser):
    return store.db.execute(
        "SELECT status, item_ids, claimed_hash, merged_into_source_id "
        "FROM folder_file_state WHERE source_id = ?",
        (loser["id"],),
    ).fetchone()


def test_collapse_records_the_winner_hash_on_the_loser_row(fuzzy_pair):
    store, loser, winner, _file, _item, winner_hash = fuzzy_pair
    row = _loser_row(store, loser)
    assert row["status"] == "deduped" and row["merged_into_source_id"] == winner["id"]
    assert row["claimed_hash"] == winner_hash


def test_deleting_the_loser_file_then_the_winner_leaves_nothing_untracked(fuzzy_pair):
    """The reporter's case."""
    store, loser, winner, loser_file, winner_item, _h = fuzzy_pair
    loser_file.unlink()
    _scan(store, loser)
    assert loser["id"] not in store.sources_holding_item(winner_item)

    store.delete_source_cascade(winner["id"])

    assert store.get_item(winner_item) is None, "no holder is left, so the text goes"
    assert _untracked(store, loser["id"]) == set()


@pytest.mark.parametrize("edit", ["content", "touch"])
def test_rereading_the_loser_file_releases_the_moved_claim(fuzzy_pair, edit):
    """The edit path: the row is re-ingested and rewritten, so its claim must go
    first -- whether the bytes changed or only the mtime did."""
    store, loser, winner, loser_file, winner_item, _h = fuzzy_pair
    if edit == "content":
        loser_file.write_text("something else entirely\n", encoding="utf-8")
    os.utime(loser_file, (500, 500))
    _scan(store, loser)
    assert loser["id"] not in store.sources_holding_item(winner_item)

    store.delete_source_cascade(winner["id"])

    assert store.get_item(winner_item) is None
    assert _untracked(store, loser["id"]) == set()


def test_deleting_the_winner_rebuilds_the_loser_from_its_own_bytes(fuzzy_pair):
    """A loser never takes the winner's text: it is different content. Deleting the
    winner destroys that text, sends the loser back to 'pending', and the next scan
    rebuilds it from its own file -- as main does."""
    store, loser, winner, _file, winner_item, _h = fuzzy_pair
    store.delete_source_cascade(winner["id"])

    assert store.get_item(winner_item) is None
    row = _loser_row(store, loser)
    assert (row["status"], row["merged_into_source_id"], row["claimed_hash"]) == (
        "pending",
        None,
        None,
    )
    assert _untracked(store, loser["id"]) == set()
    _scan(store, loser)
    texts = [
        r["content"]
        for r in store.db.execute("SELECT content FROM items WHERE source_id = ?", (loser["id"],))
    ]
    assert texts == ["weekly notes\nfirst draft\n"]


def test_deleting_the_winner_file_rebuilds_the_loser(fuzzy_pair):
    """The item-level path: the winner's FILE goes, not its source."""
    store, loser, winner, _file, winner_item, _h = fuzzy_pair
    os.remove(os.path.join(winner["uri"], "notes.md"))
    _scan(store, winner)

    assert store.get_item(winner_item) is None
    assert _loser_row(store, loser)["status"] == "pending"
    assert _untracked(store, loser["id"]) == set()


def test_a_legacy_row_without_a_claimed_hash_keeps_todays_release(tmp_path):
    """NULL claimed_hash: the claim is released by the row's own hash, as before."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        holder = store.add_source("h", "local_folder", str(tmp_path / "h"))
        loser = _folder(store, tmp_path / "l")
        h = "7" * 64
        iid = store.add_item("dup.md", "body", "doc", source_id=holder, content_hash=h)
        store.add_source_location(iid, holder)
        store.add_source_location(iid, loser["id"])
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, text_hash, "
            "mtime, item_ids, last_seen, status, merged_into_source_id) "
            "VALUES (?, ?, ?, ?, 1.0, '[]', '2024-01-01', 'deduped', ?)",
            (loser["id"], str(tmp_path / "l" / "dup.md"), h, h, holder),
        )
        store.db.commit()

        _scan(store, loser)  # the file is not on disk: handled as deleted

        assert loser["id"] not in store.sources_holding_item(iid)
    finally:
        store.close()


def test_a_legacy_fuzzy_row_still_releases_nothing(tmp_path):
    """Residual, pinned: a row a fuzzy collapse wrote before the column existed has
    no record of the winner's hash, so its claim survives the file's deletion as it
    does today. Nothing is guessed for it; a rescan of that file writes a fresh row."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        holder = store.add_source("h", "local_folder", str(tmp_path / "h"))
        loser = _folder(store, tmp_path / "l")
        own, winners = "8" * 64, "9" * 64
        iid = store.add_item("dup.md", "body", "doc", source_id=holder, content_hash=winners)
        store.add_source_location(iid, holder)
        store.add_source_location(iid, loser["id"])
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, text_hash, "
            "mtime, item_ids, last_seen, status, merged_into_source_id) "
            "VALUES (?, ?, ?, ?, 1.0, '[]', '2024-01-01', 'deduped', ?)",
            (loser["id"], str(tmp_path / "l" / "dup.md"), own, own, holder),
        )
        store.db.commit()

        _scan(store, loser)

        assert loser["id"] in store.sources_holding_item(iid)
    finally:
        store.close()


def test_a_claim_two_rows_share_survives_one_deletion(tmp_path):
    """Two files of one source both collapsed onto the same winner share one claim.
    The claimant count reads the claim hash, so deleting one file keeps the claim
    the other still needs."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        holder = store.add_source("h", "local_folder", str(tmp_path / "h"))
        loser = _folder(store, tmp_path / "l")
        hw = "5" * 64
        wid = store.add_item("notes.md", "final", "doc", source_id=holder, content_hash=hw)
        store.add_source_location(wid, holder)
        store.add_source_location(wid, loser["id"])
        kept = tmp_path / "l" / "kept.md"
        kept.write_text("x", encoding="utf-8")
        os.utime(kept, (100, 100))
        for name, own in (("gone.md", "1" * 64), ("kept.md", "2" * 64)):
            store.db.execute(
                "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
                "text_hash, claimed_hash, mtime, item_ids, last_seen, status, "
                "merged_into_source_id) VALUES (?, ?, ?, ?, ?, 100.0, '[]', "
                "'2024-01-01', 'deduped', ?)",
                (loser["id"], str(tmp_path / "l" / name), own, own, hw, holder),
            )
        store.db.commit()

        _scan(store, loser)  # gone.md is not on disk; kept.md is unchanged

        assert loser["id"] in store.sources_holding_item(wid)
    finally:
        store.close()


def test_a_revived_row_drops_the_claim_hash_it_no_longer_holds(tmp_path):
    """Two rows in one source share a fuzzy claim. Deleting the winner revives both
    to 'pending' with no claim hash, and the winner's text does not stay behind."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        winner = store.add_source("w", "local_folder", str(tmp_path / "w"))
        loser = store.add_source("l", "local_folder", str(tmp_path / "l"))
        hw = "6" * 64
        wid = store.add_item("notes.md", "final", "doc", source_id=winner, content_hash=hw)
        store.add_source_location(wid, winner)
        store.add_source_location(wid, loser)
        for name, own in (("a.md", "1" * 64), ("b.md", "2" * 64)):
            store.db.execute(
                "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
                "text_hash, claimed_hash, mtime, item_ids, last_seen, status, "
                "merged_into_source_id) VALUES (?, ?, ?, ?, ?, 1.0, '[]', "
                "'2024-01-01', 'deduped', ?)",
                (loser, str(tmp_path / "l" / name), own, own, hw, winner),
            )
        store.db.commit()

        store.delete_source_cascade(winner)

        assert store.get_item(wid) is None
        rows = store.db.execute(
            "SELECT status, claimed_hash FROM folder_file_state WHERE source_id = ?", (loser,)
        ).fetchall()
        assert [(r["status"], r["claimed_hash"]) for r in rows] == [("pending", None)] * 2
    finally:
        store.close()


def test_every_chunk_of_a_multi_chunk_winner_leaves_its_loser(tmp_path):
    """Each chunk is handled by its own call and all carry the same hash, so the
    first must not stop the rest from matching the loser's claim."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        winner = store.add_source("w", "local_folder", str(tmp_path / "w"))
        loser = store.add_source("l", "local_folder", str(tmp_path / "l"))
        own, hw = "3" * 64, "4" * 64
        chunks = [
            store.add_item(f"notes.md#{n}", f"part {n}", "doc", source_id=winner, content_hash=hw)
            for n in range(3)
        ]
        for iid in chunks:
            store.add_source_location(iid, winner)
            store.add_source_location(iid, loser)
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
            "text_hash, claimed_hash, mtime, item_ids, last_seen, status, "
            "merged_into_source_id) VALUES (?, ?, ?, ?, ?, 1.0, '[]', "
            "'2024-01-01', 'deduped', ?)",
            (loser, str(tmp_path / "l" / "notes.md"), own, own, hw, winner),
        )
        store.db.commit()

        store.delete_source_cascade(winner)

        assert [store.get_item(c) for c in chunks] == [None] * 3
        assert _untracked(store, loser) == set()
    finally:
        store.close()


@pytest.mark.parametrize("exact", [False, True])
def test_a_chained_deferral_follows_the_claim_to_the_new_winner(tmp_path, exact):
    """A row that deferred to the loser's source defers to the winner's once its claim
    moves there, so deleting the loser's source does not revive it while it still
    holds the winner's items -- on an exact collapse too."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        first = _folder(store, tmp_path / "a")
        mid = store.add_source("b", "local_folder", str(tmp_path / "b"))
        last = store.add_source("c", "local_folder", str(tmp_path / "c"))
        hb = "a" * 64
        hc = hb if exact else "c" * 64
        bid = store.add_item("notes.md", "draft", "doc", source_id=mid, content_hash=hb)
        cid = store.add_item("notes.md", "final", "doc", source_id=last, content_hash=hc)
        for iid, sid in ((bid, mid), (bid, first["id"]), (cid, last)):
            store.add_source_location(iid, sid)
        bpath = str(tmp_path / "b" / "notes.md")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
            "text_hash, mtime, item_ids, last_seen, status) "
            "VALUES (?, ?, ?, ?, 1.0, ?, '2024-01-01', 'done')",
            (mid, bpath, hb, hb, json.dumps([bid])),
        )
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
            "text_hash, claimed_hash, mtime, item_ids, last_seen, status, "
            "merged_into_source_id) VALUES (?, ?, ?, ?, ?, 1.0, '[]', "
            "'2024-01-01', 'deduped', ?)",
            (first["id"], str(tmp_path / "a" / "notes.md"), "1" * 64, "1" * 64, hb, mid),
        )
        store.db.commit()

        _collapse_doc(store, _doc(mid, bid, hb, file_path=bpath), _doc(last, cid, hc))
        row = store.db.execute(
            "SELECT claimed_hash, merged_into_source_id FROM folder_file_state "
            "WHERE source_id = ?",
            (first["id"],),
        ).fetchone()
        assert (row["claimed_hash"], row["merged_into_source_id"]) == (hc, last)

        store.delete_source_cascade(mid)
        _scan(store, first)  # its file is not on disk: handled as deleted
        assert first["id"] not in store.sources_holding_item(cid)
        store.delete_source_cascade(last)
        assert store.get_item(cid) is None
        assert _untracked(store, first["id"]) == set()
    finally:
        store.close()


def test_no_fuzzy_loser_inherits_the_winner(tmp_path):
    """Two sources fuzzy-lost to one winner. Neither holds that text, so deleting
    the winner destroys it and revives both rather than handing it to one."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        winner = store.add_source("w", "local_folder", str(tmp_path / "w"))
        losers = [store.add_source(n, "local_folder", str(tmp_path / n)) for n in ("x", "l")]
        hw = "4" * 64
        wid = store.add_item("notes.md", "final", "doc", source_id=winner, content_hash=hw)
        store.add_source_location(wid, winner)
        for n, sid in enumerate(losers):
            store.add_source_location(wid, sid)
            store.db.execute(
                "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
                "text_hash, claimed_hash, mtime, item_ids, last_seen, status, "
                "merged_into_source_id) VALUES (?, ?, ?, ?, ?, 1.0, '[]', "
                "'2024-01-01', 'deduped', ?)",
                (sid, str(tmp_path / f"f{n}" / "notes.md"), f"{n}" * 64, f"{n}" * 64, hw, winner),
            )
        store.db.commit()

        store.delete_source_cascade(winner)

        assert store.get_item(wid) is None
        for sid in losers:
            assert _untracked(store, sid) == set()
            status = store.db.execute(
                "SELECT status FROM folder_file_state WHERE source_id = ?", (sid,)
            ).fetchone()[0]
            assert status == "pending"
    finally:
        store.close()


def test_an_exact_claim_survives_the_heir_being_another_holder(tmp_path, monkeypatch):
    """An agent document and a folder file both exact-lost to one winner. The
    exact collapse records a claim hash equal to the row's own, so when deleting
    the winner hands the item to the folder, the agent's claim is still on the
    same content and must stay -- nothing re-ingests a push-driven document."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        winner = store.add_source("w", "local_folder", str(tmp_path / "w"))
        folder = store.add_source("b", "local_folder", str(tmp_path / "b"))
        agent = store.add_source("a", "agent", "agent://a")
        h = "5" * 64
        wid = store.add_item("notes.md", "same", "doc", source_id=winner, content_hash=h)
        store.add_source_location(wid, winner)
        fid = store.add_item("notes.md", "same", "doc", source_id=folder, content_hash=h)
        store.add_source_location(fid, folder)
        aid = store.add_item("notes.md", "same", "doc", source_id=agent, content_hash=h)
        store.add_source_location(aid, agent)
        fpath = str(tmp_path / "b" / "notes.md")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, text_hash, "
            "mtime, item_ids, last_seen, status) VALUES (?, ?, ?, ?, 1.0, ?, "
            "'2024-01-01', 'ingested')",
            (folder, fpath, h, h, json.dumps([fid])),
        )
        store.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status) VALUES (?, 'doc-a', ?, ?, '2024-01-01', "
            "'notes.md', 'active')",
            (agent, h, json.dumps([aid])),
        )
        store.db.commit()

        _collapse_doc(store, _doc(folder, fid, h, file_path=fpath), _doc(winner, wid, h))
        _collapse_doc(store, _doc(agent, aid, h, source_type="agent"), _doc(winner, wid, h))
        assert set(store.sources_holding_item(wid)) == {winner, folder, agent}

        # The heir is whichever holder the location query returns first, which
        # SQLite does not order; pin it to the folder, the case that strands.
        holders = store.sources_holding_item
        monkeypatch.setattr(
            store,
            "sources_holding_item",
            lambda item_id, **kw: sorted(holders(item_id, **kw), key=lambda s: s != folder),
        )
        store.delete_source_cascade(winner)
        assert store.get_item(wid)["source_id"] == folder, "precondition: the folder inherits"
        assert agent in store.sources_holding_item(wid)

        store.delete_source_cascade(folder)
        assert store.get_item(wid) is not None, "the agent still holds the content"
        assert store.get_item(wid)["source_id"] == agent
    finally:
        store.close()


def _doc(source_id, item_id, h, *, file_path=None, source_type="local_folder"):
    return DocRef(
        source_id=source_id,
        source_type=source_type,
        filename="notes.md",
        item_ids=[item_id],
        content_hash=h,
        embedding_sig="sig",
        recency=1.0,
        resident_since=1.0,
        file_path=file_path,
    )


def test_a_chained_holder_claim_moves_with_the_collapse(tmp_path):
    """A third source that lost to the loser earlier claims the loser's copy under
    the loser's hash. Collapsing the loser moves that claim onto the winner's items,
    so its recorded hash moves too and deleting its file still releases it."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        third = _folder(store, tmp_path / "t")
        loser = store.add_source("l", "local_folder", str(tmp_path / "l"))
        winner = store.add_source("w", "local_folder", str(tmp_path / "w"))
        hl, hw = "a" * 64, "b" * 64
        lid = store.add_item("notes.md", "first", "doc", source_id=loser, content_hash=hl)
        wid = store.add_item("notes.md", "final", "doc", source_id=winner, content_hash=hw)
        for iid, sid in ((lid, loser), (lid, third["id"]), (wid, winner)):
            store.add_source_location(iid, sid)
        lpath = str(tmp_path / "l" / "notes.md")
        for sid, path, ids, status in (
            (loser, lpath, [lid], "done"),
            (third["id"], str(tmp_path / "t" / "notes.md"), [], "deduped"),
        ):
            store.db.execute(
                "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
                "text_hash, mtime, item_ids, last_seen, status) "
                "VALUES (?, ?, ?, ?, 1.0, ?, '2024-01-01', ?)",
                (sid, path, hl, hl, json.dumps(ids), status),
            )
        store.db.commit()

        _collapse_doc(store, _doc(loser, lid, hl, file_path=lpath), _doc(winner, wid, hw))

        assert (
            store.db.execute(
                "SELECT claimed_hash FROM folder_file_state WHERE source_id = ?", (third["id"],)
            ).fetchone()[0]
            == hw
        )
        assert third["id"] in store.sources_holding_item(wid)
        _scan(store, third)  # its file is not on disk: handled as deleted
        assert third["id"] not in store.sources_holding_item(wid)
    finally:
        store.close()


@pytest.mark.parametrize(
    "table,source_type,remove",
    [
        ("agent_item_state", "agent", agent_source.remove_document),
        ("artifact_item_state", "artifact", artifact_ingest.remove_artifact),
    ],
)
def test_removing_a_fuzzy_merged_aggregate_document_releases_its_claim(
    tmp_path, table, source_type, remove
):
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        agg = store.add_source("agg", source_type, f"{source_type}://agg")
        winner = store.add_source("w", "local_folder", str(tmp_path / "w"))
        hl, hw = "c" * 64, "d" * 64
        lid = store.add_item("notes.md", "first", "doc", source_id=agg, content_hash=hl)
        wid = store.add_item("notes.md", "final", "doc", source_id=winner, content_hash=hw)
        store.add_source_location(lid, agg)
        store.add_source_location(wid, winner)
        store.db.execute(
            f"INSERT INTO {table} (source_id, slug, content_hash, item_ids, "  # noqa: S608
            "updated_at, name, status) VALUES (?, 'doc-a', ?, ?, '2024-01-01', "
            "'notes.md', 'active')",
            (agg, hl, json.dumps([lid])),
        )
        store.db.commit()

        _collapse_doc(store, _doc(agg, lid, hl, source_type=source_type), _doc(winner, wid, hw))
        assert agg in store.sources_holding_item(wid)

        remove(store, agg, "doc-a")
        assert agg not in store.sources_holding_item(wid)
        store.delete_source_cascade(winner)
        assert store.get_item(wid) is None
        assert _untracked(store, agg) == set()
    finally:
        store.close()


@pytest.mark.parametrize("table", ["agent_item_state", "artifact_item_state"])
def test_an_aggregate_reingest_releases_the_moved_claim(tmp_path, table):
    """The aggregate edit path passes the row's claimed hash on."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        agg = store.add_source("agg", "agent", "agent://agg")
        winner = store.add_source("w", "local_folder", str(tmp_path / "w"))
        hl, hw = "e" * 64, "f" * 64
        wid = store.add_item("notes.md", "final", "doc", source_id=winner, content_hash=hw)
        store.add_source_location(wid, winner)
        store.add_source_location(wid, agg)
        store.db.execute(
            f"INSERT INTO {table} (source_id, slug, content_hash, item_ids, "  # noqa: S608
            "updated_at, name, status, merged_into_source_id, claimed_hash) "
            "VALUES (?, 'doc-a', ?, '[]', '2024-01-01', 'notes.md', 'deduped', ?, ?)",
            (agg, hl, winner, hw),
        )
        store.db.commit()
        release = agent_source if table == "agent_item_state" else artifact_ingest
        release._release_stale_claim(store, agg, "doc-a", hl, "0" * 64, [])
        assert agg not in store.sources_holding_item(wid)
    finally:
        store.close()


@pytest.mark.parametrize("table", ["agent_item_state", "artifact_item_state"])
def test_an_unchanged_reingest_keeps_an_exact_claim(tmp_path, table):
    """An exact collapse records no claim hash, so re-adding unchanged bytes keeps
    the claim until the ingest actually rewrites the row -- an ingest refused on
    budget rewrites nothing and must lose nothing."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        stype = "agent" if table == "agent_item_state" else "artifact"
        agg = store.add_source("agg", stype, f"{stype}://agg")
        winner = store.add_source("w", "local_folder", str(tmp_path / "w"))
        h = "6" * 64
        aid = store.add_item("notes.md", "same", "doc", source_id=agg, content_hash=h)
        store.add_source_location(aid, agg)
        wid = store.add_item("notes.md", "same", "doc", source_id=winner, content_hash=h)
        store.add_source_location(wid, winner)
        store.db.execute(
            f"INSERT INTO {table} (source_id, slug, content_hash, item_ids, "  # noqa: S608
            "updated_at, name, status) VALUES (?, 'doc-a', ?, ?, '2024-01-01', "
            "'notes.md', 'active')",
            (agg, h, json.dumps([aid])),
        )
        store.db.commit()
        _collapse_doc(store, _doc(agg, aid, h, source_type=stype), _doc(winner, wid, h))
        assert store.claimed_hash_of(table, agg, "doc-a") is None

        release = agent_source if table == "agent_item_state" else artifact_ingest
        assert release._release_stale_claim(store, agg, "doc-a", h, h, []) == 0
        assert agg in store.sources_holding_item(wid)
    finally:
        store.close()


def test_an_exact_html_artifact_claim_survives_revive(tmp_path, monkeypatch):
    """An artifact row hashes its submitted text (HTML) while its items hash the
    extracted prose, so the two never match. An exact collapse must still leave
    nothing that reads as a fuzzy claim: when the winner's deletion hands the item
    to another folder, the artifact keeps its location, and deleting that folder
    then hands the document to the artifact rather than destroying it."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        art = store.add_source("art", "artifact", "artifact://art")
        winner = store.add_source("w", "local_folder", str(tmp_path / "w"))
        folder = store.add_source("b", "local_folder", str(tmp_path / "b"))
        raw, prose = "9" * 64, "0" * 64  # hash of the HTML vs hash of its prose
        aid = store.add_item("page", "prose", "doc", source_id=art, content_hash=prose)
        store.add_source_location(aid, art)
        wid = store.add_item("page", "prose", "doc", source_id=winner, content_hash=prose)
        store.add_source_location(wid, winner)
        fid = store.add_item("page", "prose", "doc", source_id=folder, content_hash=prose)
        store.add_source_location(fid, folder)
        store.db.execute(
            "INSERT INTO artifact_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status) VALUES (?, 'page', ?, ?, '2024-01-01', 'page', "
            "'active')",
            (art, raw, json.dumps([aid])),
        )
        fpath = str(tmp_path / "b" / "page.md")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, text_hash, "
            "mtime, item_ids, last_seen, status) VALUES (?, ?, ?, ?, 1.0, ?, "
            "'2024-01-01', 'ingested')",
            (folder, fpath, prose, prose, json.dumps([fid])),
        )
        store.db.commit()

        _collapse_doc(
            store, _doc(art, aid, prose, source_type="artifact"), _doc(winner, wid, prose)
        )
        _collapse_doc(store, _doc(folder, fid, prose, file_path=fpath), _doc(winner, wid, prose))
        assert store.claimed_hash_of("artifact_item_state", art, "page") is None

        holders = store.sources_holding_item
        monkeypatch.setattr(
            store,
            "sources_holding_item",
            lambda item_id, **kw: sorted(holders(item_id, **kw), key=lambda s: s != folder),
        )
        store.delete_source_cascade(winner)
        assert store.get_item(wid)["source_id"] == folder, "precondition: the folder inherits"
        assert art in store.sources_holding_item(wid)

        store.delete_source_cascade(folder)
        assert store.get_item(wid)["source_id"] == art
    finally:
        store.close()


def test_a_winner_source_row_keeps_deferring_to_the_loser(tmp_path):
    """Folder W's file R lost to L earlier; now W's newer file beats L. R must not
    end up deferring to its own source, or deleting L never revives it."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        loser = store.add_source("l", "local_folder", str(tmp_path / "l"))
        wsrc = store.add_source("w", "local_folder", str(tmp_path / "w"))
        hl, hw = "7" * 64, "8" * 64
        lid = store.add_item("notes.md", "first", "doc", source_id=loser, content_hash=hl)
        store.add_source_location(lid, loser)
        store.add_source_location(lid, wsrc)
        wid = store.add_item("notes.md", "final", "doc", source_id=wsrc, content_hash=hw)
        store.add_source_location(wid, wsrc)
        lpath = str(tmp_path / "l" / "notes.md")
        rows = (
            (loser, lpath, hl, json.dumps([lid]), "ingested", None),
            (wsrc, str(tmp_path / "w" / "r.md"), hl, "[]", "deduped", loser),
            (wsrc, str(tmp_path / "w" / "notes.md"), hw, json.dumps([wid]), "ingested", None),
        )
        for sid, path, h, ids, status, merged in rows:
            store.db.execute(
                "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
                "text_hash, mtime, item_ids, last_seen, status, merged_into_source_id) "
                "VALUES (?, ?, ?, ?, 1.0, ?, '2024-01-01', ?, ?)",
                (sid, path, h, h, ids, status, merged),
            )
        store.db.commit()

        _collapse_doc(store, _doc(loser, lid, hl, file_path=lpath), _doc(wsrc, wid, hw))

        def r_row():
            return store.db.execute(
                "SELECT status, merged_into_source_id FROM folder_file_state "
                "WHERE file_path = ?",
                (str(tmp_path / "w" / "r.md"),),
            ).fetchone()

        assert r_row()["merged_into_source_id"] == loser
        store.delete_source_cascade(loser)
        assert tuple(r_row()) == ("pending", None)
    finally:
        store.close()


def test_an_exact_recollapse_keeps_an_earlier_fuzzy_claim(tmp_path):
    """An exact collapse supplies no claim hash and must not write NULL over one an
    earlier fuzzy collapse recorded. The row here is built by hand: with adoption
    gone the code no longer produces it, so this pins the write itself."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        loser = store.add_source("l", "local_folder", str(tmp_path / "l"))
        winner = store.add_source("w", "local_folder", str(tmp_path / "w"))
        own, h, earlier = "a1" * 32, "b2" * 32, "c3" * 32
        lid = store.add_item("notes.md", "same", "doc", source_id=loser, content_hash=h)
        store.add_source_location(lid, loser)
        wid = store.add_item("notes.md", "same", "doc", source_id=winner, content_hash=h)
        store.add_source_location(wid, winner)
        lpath = str(tmp_path / "l" / "notes.md")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, text_hash, "
            "claimed_hash, mtime, item_ids, last_seen, status) VALUES (?, ?, ?, ?, ?, 1.0, "
            "?, '2024-01-01', 'done')",
            (loser, lpath, own, own, earlier, json.dumps([lid])),
        )
        store.db.commit()

        _collapse_doc(store, _doc(loser, lid, h, file_path=lpath), _doc(winner, wid, h))

        assert store.claimed_hash_of("folder_file_state", loser, lpath) == earlier
    finally:
        store.close()
