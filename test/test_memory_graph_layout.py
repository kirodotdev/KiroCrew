"""Behaviour of the server-side memory graph layout and its sidecar file."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import stat
import sys
from pathlib import Path

import numpy as np
import pytest

from kiro_crew import memory_graph_layout as gl
from kiro_crew.memory_graph_sidecar import PAGE_CAP, LayoutSidecar, PlaceItem
from kiro_crew.testing import synthetic_credentials

DIM = 32
EVERYWHERE: tuple[float, float, float, float] = (-1e9, -1e9, 1e9, 1e9)


def _unit(v: np.ndarray) -> np.ndarray:
    return (v / np.linalg.norm(v, axis=-1, keepdims=True)).astype(np.float32)


def _topic(rng: np.random.Generator, axis: int, n: int) -> np.ndarray:
    centre = np.zeros(DIM, np.float32)
    centre[axis] = 1.0
    return _unit(centre + 0.05 * rng.standard_normal((n, DIM)).astype(np.float32))


def _make_store(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE episodic_memories (
            id TEXT PRIMARY KEY, embedding BLOB, tags TEXT DEFAULT '[]', is_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE semantic_memory (key TEXT PRIMARY KEY, embedding BLOB, is_deleted INTEGER DEFAULT 0);
        """)
    return db


def _episode(db: sqlite3.Connection, rid: str, vec: np.ndarray | None, tags=(), deleted=0) -> None:
    db.execute(
        "INSERT INTO episodic_memories VALUES (?, ?, ?, ?)",
        (rid, None if vec is None else vec.tobytes(), json.dumps(list(tags)), deleted),
    )


def _fact(db: sqlite3.Connection, key: str, vec: np.ndarray | None, deleted=0) -> None:
    db.execute(
        "INSERT INTO semantic_memory VALUES (?, ?, ?)",
        (key, None if vec is None else vec.tobytes(), deleted),
    )


@pytest.fixture
def two_topics(tmp_path: Path):
    """60 experiences about topic A, 60 about B. Topic A rows carry tags
    ``alpha0``..``alpha11`` (5 rows each, rare enough to place by), B ``beta*``."""
    rng = np.random.default_rng(3)
    store = tmp_path / "memory.db"
    db = _make_store(store)
    for i, v in enumerate(_topic(rng, 0, 60)):
        _episode(db, f"a{i}", v, tags=[f"alpha{i % 12}"])
    for i, v in enumerate(_topic(rng, 1, 60)):
        _episode(db, f"b{i}", v, tags=[f"beta{i % 12}"])
    db.commit()
    db.close()
    side = LayoutSidecar(gl.sidecar_path(store))
    gl.build_sidecar(store, side.path)
    return side, rng, store


def _centre_of(side: LayoutSidecar, prefix: str) -> np.ndarray:
    ids = [i for i in side.ids_in_box(EVERYWHERE) if i.startswith(prefix) and i[1:].isdigit()]
    return np.array(list(side.positions(ids).values())).mean(0)


def _placed_by(side: LayoutSidecar, rid: str) -> str:
    db = sqlite3.connect(side.path)
    try:
        return db.execute("SELECT placed_by FROM nodes WHERE id = ?", (rid,)).fetchone()[0]
    finally:
        db.close()


def _nearer(xy, a, b) -> bool:
    xy = np.asarray(xy)
    return bool(np.linalg.norm(xy - a) < np.linalg.norm(xy - b))


# --- full build -----------------------------------------------------------


def test_build_lays_out_every_live_row(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    store = tmp_path / "memory.db"
    db = _make_store(store)
    vecs = _unit(rng.standard_normal((40, DIM)).astype(np.float32))
    for i, v in enumerate(vecs):
        _episode(db, f"e{i}", v)
    _fact(db, "lesson.x", vecs[0])
    _fact(db, "project.acme.owner", None)
    _fact(db, "fact.gone", vecs[1], deleted=1)
    _episode(db, "old_model", np.zeros(DIM * 2, np.float32))
    db.commit()
    db.close()
    side = LayoutSidecar(gl.sidecar_path(store))
    stats = gl.build_sidecar(store, side.path)

    ids = set(side.ids_in_box(EVERYWHERE))
    assert "fact.gone" not in ids
    assert {"lesson.x", "project.acme.owner", "old_model"} <= ids
    assert len(ids) == stats["n"] == 43
    assert stats["placed_by"][gl.PLACED_VECTOR] == 41
    assert sum(c["count"] for c in side.clusters()) == 43


@pytest.mark.parametrize("source", ["experience", "fact", "lesson", "tags", "key"])
@pytest.mark.parametrize("credential", ["aws", "github"])
def test_topic_names_redact_before_transforming_content(
    tmp_path: Path, source: str, credential: str
) -> None:
    secret, content = {
        "aws": (
            synthetic_credentials.aws_secret(),
            f"aws_secret_access_key = {synthetic_credentials.aws_secret()}",
        ),
        "github": (synthetic_credentials.github_classic(), synthetic_credentials.github_classic()),
    }[credential]
    store = tmp_path / "memory.db"
    db = _make_store(store)
    try:
        vec = np.eye(1, DIM, dtype=np.float32)[0]
        if source in {"experience", "tags"}:
            _episode(db, "episode", vec, tags=[content] if source == "tags" else [])
        else:
            key = f"project.{content}.detail" if source == "key" else f"{source}.detail"
            _fact(db, key, vec)
        db.execute("ALTER TABLE episodic_memories ADD COLUMN text TEXT DEFAULT ''")
        db.execute("ALTER TABLE semantic_memory ADD COLUMN value_json TEXT DEFAULT ''")
        if source == "experience":
            db.execute("UPDATE episodic_memories SET text = ?", (content,))
        elif source in {"fact", "lesson"}:
            value = {"rule": content} if source == "lesson" else content
            db.execute("UPDATE semantic_memory SET value_json = ?", (json.dumps(value),))
        db.commit()
    finally:
        db.close()

    rows = gl.read_store(store)
    assert len(rows.ids) == 1
    scored, row_terms = gl.cluster_terms(
        {0: rows.ids}, rows.tokens, rows.texts, set(), lambda t: True
    )
    # Check extracted terms as well as the winning name: a safe winner alone can mask a leak.
    derived = [t for terms in row_terms.values() for t in terms]
    derived += [t for terms in scored.values() for t, _ in terms]
    derived += [gl.topic_name([gl.memory_title(text)]) for text in rows.texts.values()]
    side = LayoutSidecar(gl.sidecar_path(store))
    gl.build_sidecar(store, side.path)
    labels = [c["label"] for c in side.clusters()]
    assert len(labels) == 1
    derived += [label or "" for label in labels]
    # Phrase extraction can expose only a letter run from a mixed-case token.
    fragments = [part.lower() for part in re.findall(r"[A-Za-z]{4,}", secret)]
    assert fragments
    for text in derived:
        assert secret.lower() not in text.lower()
        assert not any(part in text.lower() for part in fragments), text


def test_similar_rows_are_laid_out_together(two_topics) -> None:
    side, *_ = two_topics
    a, b = _centre_of(side, "a"), _centre_of(side, "b")
    pos = side.positions(side.ids_in_box(EVERYWHERE))
    a_rows = [pos[f"a{i}"] for i in range(60)]
    assert sum(_nearer(xy, a, b) for xy in a_rows) >= 57


def test_a_cluster_spreads_its_rows_evenly_over_its_disc(two_topics) -> None:
    side, *_ = two_topics
    db = sqlite3.connect(side.path)
    try:
        discs = {cid: (x, y, r) for cid, x, y, r in db.execute("SELECT id, x, y, r FROM clusters")}
        rows = db.execute("SELECT cluster, x, y FROM nodes WHERE placed_by = 'vector'").fetchall()
    finally:
        db.close()
    inner = [
        math.hypot(x - discs[c][0], y - discs[c][1]) < discs[c][2] / math.sqrt(2)
        for c, x, y in rows
    ]
    # Even density: about half the rows lie in the inner half of the disc's area.
    assert 0.3 < sum(inner) / len(inner) < 0.7


def test_cluster_discs_keep_a_gap_between_them(two_topics) -> None:
    side, *_ = two_topics
    discs = [c for c in side.clusters() if c["id"] >= 0]
    for i, a in enumerate(discs):
        for b in discs[i + 1 :]:
            gap = math.hypot(a["x"] - b["x"], a["y"] - b["y"]) - a["r"] - b["r"]
            assert gap >= 0.1 * (a["r"] + b["r"])


def test_rows_without_a_vector_are_placed_by_their_tags(tmp_path: Path) -> None:
    rng = np.random.default_rng(4)
    store = tmp_path / "memory.db"
    db = _make_store(store)
    for i, v in enumerate(_topic(rng, 0, 40)):
        _episode(db, f"a{i}", v, tags=[f"alpha{i % 20}"])
    for i, v in enumerate(_topic(rng, 1, 40)):
        _episode(db, f"b{i}", v, tags=[f"beta{i % 20}"])
    _episode(db, "tagged-a", None, tags=["alpha3"])
    _episode(db, "tagged-b", None, tags=["beta3"])
    # Only link-holder shares a tag with a vector row; via-tagged-a shares one
    # with link-holder alone, so a second round places it.
    _episode(db, "link-holder", None, tags=["alpha5", "rare-link"])
    _episode(db, "via-tagged-a", None, tags=["rare-link"])
    db.commit()
    db.close()
    side = LayoutSidecar(gl.sidecar_path(store))
    stats = gl.build_sidecar(store, side.path)

    a, b = _centre_of(side, "a"), _centre_of(side, "b")
    pos = side.positions(["tagged-a", "tagged-b", "via-tagged-a"])
    assert _nearer(pos["tagged-a"], a, b)
    assert _nearer(pos["tagged-b"], b, a)
    assert _nearer(pos["via-tagged-a"], a, b)
    assert stats["placed_by"][gl.PLACED_TOKENS] == 4


def test_facts_are_placed_by_their_key_namespace(tmp_path: Path) -> None:
    rng = np.random.default_rng(6)
    store = tmp_path / "memory.db"
    db = _make_store(store)
    for i, v in enumerate(_topic(rng, 0, 30)):
        _fact(db, f"project.acme.f{i}", v)
    for i, v in enumerate(_topic(rng, 1, 30)):
        _fact(db, f"project.zeta.f{i}", v)
    _fact(db, "project.acme.no_vector", None)
    db.commit()
    db.close()
    side = LayoutSidecar(gl.sidecar_path(store))
    gl.build_sidecar(store, side.path)

    pos = side.positions(side.ids_in_box(EVERYWHERE))
    acme = np.mean([pos[f"project.acme.f{i}"] for i in range(30)], axis=0)
    zeta = np.mean([pos[f"project.zeta.f{i}"] for i in range(30)], axis=0)
    assert _nearer(pos["project.acme.no_vector"], acme, zeta)
    assert _placed_by(side, "project.acme.no_vector") == gl.PLACED_TOKENS


def test_rows_with_nothing_to_place_them_by_go_on_the_band(tmp_path: Path) -> None:
    rng = np.random.default_rng(7)
    store = tmp_path / "memory.db"
    db = _make_store(store)
    for i, v in enumerate(_topic(rng, 0, 30)):
        _episode(db, f"a{i}", v)
    for i in range(3):
        _fact(db, f"orphan_ns.k{i}", None)
    _fact(db, "lesson.deadbeef", None)
    db.commit()
    db.close()
    side = LayoutSidecar(gl.sidecar_path(store))
    stats = gl.build_sidecar(store, side.path)
    assert stats["placed_by"][gl.PLACED_BAND] == 4

    pos = side.positions(side.ids_in_box(EVERYWHERE))
    inner = max(np.hypot(*pos[f"a{i}"]) for i in range(30))
    for rid in ("orphan_ns.k0", "orphan_ns.k1", "orphan_ns.k2", "lesson.deadbeef"):
        assert np.hypot(*pos[rid]) > inner
    # One arc per fact namespace: its three facts sit closer to each other than
    # to the lesson's arc.
    ns = np.array([pos[f"orphan_ns.k{i}"] for i in range(3)])
    spread = max(np.linalg.norm(p - ns.mean(0)) for p in ns)
    assert spread < np.linalg.norm(np.array(pos["lesson.deadbeef"]) - ns.mean(0))


def test_each_cluster_is_named_by_its_most_distinctive_tag(tmp_path: Path) -> None:
    rng = np.random.default_rng(9)
    store = tmp_path / "memory.db"
    db = _make_store(store)
    for i, v in enumerate(_topic(rng, 0, 40)):
        _episode(db, f"a{i}", v, tags=["kirocrew", "otters"])
    for i, v in enumerate(_topic(rng, 1, 40)):
        _episode(db, f"b{i}", v, tags=["kirocrew", "zebras"])
    # Rows that make "kirocrew" too common to name anything.
    for i in range(900):
        _fact(db, f"kirocrew.filler.f{i}", None)
    db.commit()
    db.close()
    side = LayoutSidecar(gl.sidecar_path(store))
    gl.build_sidecar(store, side.path)

    pos = side.positions([f"a{i}" for i in range(40)] + [f"b{i}" for i in range(40)])
    sdb = sqlite3.connect(side.path)
    labels = dict(sdb.execute("SELECT id, label FROM clusters WHERE id >= 0"))
    node_cluster = dict(sdb.execute("SELECT id, cluster FROM nodes"))
    sdb.close()
    a_labels = {labels.get(node_cluster[f"a{i}"]) for i in range(40)}
    b_labels = {labels.get(node_cluster[f"b{i}"]) for i in range(40)}
    assert a_labels == {"Otters"}
    assert b_labels == {"Zebras"}
    assert len(pos) == 80


def test_bot_store_views_are_read(tmp_path: Path) -> None:
    rng = np.random.default_rng(8)
    store = tmp_path / "memory.db"
    db = sqlite3.connect(store)
    db.executescript("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY, kind TEXT, key TEXT, embedding BLOB,
            tags TEXT DEFAULT '[]', is_deleted INTEGER DEFAULT 0
        );
        CREATE VIEW episodic_memories AS
            SELECT id, embedding, tags, is_deleted FROM memory_items WHERE kind = 'episode';
        CREATE VIEW semantic_memory AS
            SELECT key, embedding, is_deleted FROM memory_items WHERE kind IN ('fact', 'directive');
        """)
    for i, v in enumerate(_topic(rng, 0, 20)):
        db.execute(
            "INSERT INTO memory_items VALUES (?, 'episode', NULL, ?, '[]', 0)",
            (f"e{i}", v.tobytes()),
        )
    db.execute("INSERT INTO memory_items VALUES ('x', 'directive', 'lesson.1', NULL, '[]', 0)")
    db.commit()
    db.close()
    side = LayoutSidecar(gl.sidecar_path(store))
    stats = gl.build_sidecar(store, side.path)
    assert stats["n"] == 21
    assert "lesson.1" in side.ids_in_box(EVERYWHERE, kinds=["lesson"])


# --- incremental placement --------------------------------------------------


def test_new_row_with_a_vector_lands_with_its_topic_and_nothing_else_moves(two_topics) -> None:
    side, rng, _ = two_topics
    before = side.positions(side.ids_in_box(EVERYWHERE))
    vec = _topic(rng, 0, 1)[0]
    placed = side.place([PlaceItem("new-a", "episode", vec.tobytes())])

    assert _nearer(placed["new-a"], _centre_of(side, "a"), _centre_of(side, "b"))
    assert side.positions(list(before)) == before
    assert side.meta()["incr_count"] == 1


def test_new_row_lands_where_a_full_build_puts_it(two_topics) -> None:
    """The cluster map reproduces the build: a row placed again with its own
    vector lands within a few dots of where the build put it. The build moves
    each row from its map position onto the nearest free slot of the disc's
    sunflower (:func:`gl.sunflower_fit`), which a single new row cannot take,
    so "next to" is measured in slots: the mean distance between neighbours."""
    side, _, store = two_topics
    centres = {c["id"]: np.array([c["x"], c["y"]]) for c in side.clusters()}
    sdb = sqlite3.connect(side.path)
    rows = sdb.execute("SELECT id, x, y, cluster FROM nodes WHERE id LIKE 'a%'").fetchall()
    sdb.close()
    # The row farthest from its cluster's centre: a map that ignored the row's
    # own offset would put it at the centre instead.
    rid, x, y, cid = max(rows, key=lambda r: np.linalg.norm(np.array(r[1:3]) - centres[r[3]]))
    built = np.array([x, y])
    offset = np.linalg.norm(built - centres[cid])
    assert offset > 0

    db = sqlite3.connect(store)
    blob = db.execute("SELECT embedding FROM episodic_memories WHERE id = ?", (rid,)).fetchone()[0]
    db.close()
    placed = np.array(side.place([PlaceItem(rid, "episode", blob)])[rid])
    assert np.linalg.norm(placed - built) < 3 * gl.SLOT_SPACING


def test_new_row_without_a_vector_is_placed_by_its_tags(two_topics) -> None:
    side, *_ = two_topics
    placed = side.place([PlaceItem("tag-only", "episode", None, tags=["beta4"])])
    assert _nearer(placed["tag-only"], _centre_of(side, "b"), _centre_of(side, "a"))
    assert _placed_by(side, "tag-only") == gl.PLACED_TOKENS


def test_backfilled_vector_moves_a_tag_placed_row_to_its_topic(two_topics) -> None:
    side, rng, _ = two_topics
    side.place([PlaceItem("late", "episode", None, tags=["beta4"])])
    assert _placed_by(side, "late") == gl.PLACED_TOKENS
    moved = side.place(
        [PlaceItem("late", "episode", _topic(rng, 0, 1)[0].tobytes(), tags=["beta4"])]
    )
    assert side.ids_in_box(EVERYWHERE).count("late") == 1
    assert _nearer(moved["late"], _centre_of(side, "a"), _centre_of(side, "b"))
    assert _placed_by(side, "late") == gl.PLACED_VECTOR


def test_row_with_nothing_to_place_it_by_goes_outside_the_layout(two_topics) -> None:
    side, *_ = two_topics
    pos = side.positions(side.ids_in_box(EVERYWHERE))
    inner = max(np.hypot(*xy) for xy in pos.values())
    placed = side.place([PlaceItem("lesson.abc", "lesson", None)])
    assert np.hypot(*placed["lesson.abc"]) > inner
    assert _placed_by(side, "lesson.abc") == gl.PLACED_BAND


def test_row_of_another_width_falls_back_to_its_tags(two_topics) -> None:
    side, *_ = two_topics
    wide = np.zeros(DIM * 2, np.float32).tobytes()
    placed = side.place([PlaceItem("wide", "episode", wide, tags=["alpha2"])])
    assert _nearer(placed["wide"], _centre_of(side, "a"), _centre_of(side, "b"))
    assert _placed_by(side, "wide") == gl.PLACED_TOKENS


def test_replacing_a_linked_node_drops_only_its_incident_links(two_topics) -> None:
    side, rng, _ = two_topics
    ids = side.ids_in_box(EVERYWHERE)
    before = side.links(ids)
    rid = next(
        i
        for i, links in sorted(before.items())
        if any(other < i for other, _ in links) and any(other > i for other, _ in links)
    )
    expected = {
        i: [(other, sim) for other, sim in links if other != rid]
        for i, links in before.items()
        if i != rid and any(other != rid for other, _ in links)
    }
    assert expected

    side.place([PlaceItem(rid, "episode", _topic(rng, 1, 1)[0].tobytes())])

    assert rid in side.positions([rid])
    assert side.links(ids) == expected


def test_links_across_query_chunks_are_returned_once(two_topics, monkeypatch) -> None:
    side, *_ = two_topics
    ids = side.ids_in_box(EVERYWHERE)
    expected = side.links(ids)
    assert expected
    assert all(len(links) == len(set(links)) for links in expected.values())
    monkeypatch.setattr("kiro_crew.memory_graph_sidecar._IN_CHUNK", 1)

    assert side.links(ids) == expected


def test_removed_rows_leave_the_layout_and_their_cluster_count(two_topics) -> None:
    side, *_ = two_topics
    total = sum(c["count"] for c in side.clusters())
    side.remove(["a1", "b2", "not-there"])
    assert not {"a1", "b2"} & set(side.ids_in_box(EVERYWHERE))
    assert sum(c["count"] for c in side.clusters()) == total - 2


# --- box queries ------------------------------------------------------------


def test_box_pages_cover_every_node_once_heaviest_first(two_topics) -> None:
    side, *_ = two_topics
    ids = side.ids_in_box(EVERYWHERE)
    side.set_weights({i: (n % 7) / 7 for n, i in enumerate(ids)})

    seen: list[str] = []
    weights: list[float] = []
    cursor = None
    for _ in range(len(ids)):  # one page per node at most, so a stuck cursor fails
        page, cursor = side.nodes_in_box(EVERYWHERE, limit=13, cursor=cursor)
        assert len(page) <= 13
        seen += [n["id"] for n in page]
        weights += [n["weight"] for n in page]
        if cursor is None:
            break
    assert cursor is None
    assert sorted(seen) == sorted(ids)
    assert weights == sorted(weights, reverse=True)


def test_box_page_is_capped(two_topics) -> None:
    side, *_ = two_topics
    page, _ = side.nodes_in_box(EVERYWHERE, limit=10**9)
    assert len(page) == min(PAGE_CAP, len(side.ids_in_box(EVERYWHERE)))


def test_box_select_returns_nodes_a_client_never_loaded(two_topics) -> None:
    side, *_ = two_topics
    first_page, _ = side.nodes_in_box(EVERYWHERE, limit=5)
    every = side.ids_in_box(EVERYWHERE)
    assert len(every) == 120 > len(first_page)

    pos = side.positions(every)
    a_xy = np.array([pos[f"a{i}"] for i in range(60)])
    box = (*a_xy.min(0), *a_xy.max(0))
    assert {f"a{i}" for i in range(60)} <= set(side.ids_in_box(box))


def test_kind_filter_applies_to_box_queries(two_topics) -> None:
    side, rng, _ = two_topics
    side.place([PlaceItem("lesson.n", "lesson", _topic(rng, 0, 1)[0].tobytes())])
    assert side.ids_in_box(EVERYWHERE, kinds=["lesson"]) == ["lesson.n"]
    page, _ = side.nodes_in_box(EVERYWHERE, kinds=["lesson"])
    assert [n["id"] for n in page] == ["lesson.n"]


def test_rows_missing_from_the_file_are_reported(two_topics) -> None:
    side, *_ = two_topics
    assert side.missing(["a0", "written-during-rebuild"]) == ["written-during-rebuild"]


# --- rebuild triggers and file trust ----------------------------------------


def test_drift_past_threshold_asks_for_a_rebuild(two_topics) -> None:
    side, rng, _ = two_topics
    assert not side.needs_rebuild(DIM)
    batch = int(120 * gl.REBUILD_DRIFT) + 1
    vecs = _topic(rng, 0, batch)
    side.place([PlaceItem(f"n{i}", "episode", v.tobytes()) for i, v in enumerate(vecs)])
    assert side.needs_rebuild(DIM)


def test_width_change_asks_for_a_rebuild(two_topics) -> None:
    side, *_ = two_topics
    assert side.needs_rebuild(DIM * 2)


def test_a_layout_built_on_an_empty_store_rebuilds_once_memories_arrive(tmp_path: Path) -> None:
    store = tmp_path / "memory.db"
    _make_store(store).close()
    side = LayoutSidecar(gl.sidecar_path(store))
    gl.build_sidecar(store, side.path)
    assert not side.needs_rebuild(None)

    vec = _topic(np.random.default_rng(5), 0, 1)[0]
    assert side.needs_rebuild(DIM)
    side.place([PlaceItem("late", "episode", vec.tobytes())])
    assert side.needs_rebuild(None)


def test_missing_corrupt_or_old_file_is_not_trusted(tmp_path: Path, two_topics) -> None:
    assert LayoutSidecar(tmp_path / "absent.sqlite").needs_rebuild()
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database")
    assert LayoutSidecar(bad).meta() is None

    side, *_ = two_topics
    db = sqlite3.connect(side.path)
    db.execute("UPDATE meta SET value = '1' WHERE key = 'version'")
    db.commit()
    db.close()
    assert side.needs_rebuild()


def test_adopt_swaps_in_a_finished_build(tmp_path: Path, two_topics) -> None:
    side, rng, store = two_topics
    side.place([PlaceItem("new", "episode", _topic(rng, 0, 1)[0].tobytes())])
    assert side.meta()["incr_count"] == 1
    assert side.meta()["positions"] == 1

    built = tmp_path / "rebuild.sqlite"
    gl.build_sidecar(store, built)
    side.adopt(built)
    assert side.meta()["incr_count"] == 0
    assert side.meta()["positions"] == 0
    assert side.revision() == f"{side.meta()['built_at']}.0"
    assert side.place([]) == {}
    assert side.meta()["positions"] == 0
    assert not built.exists()
    # Placement after the swap uses the new file's maps.
    assert side.place([PlaceItem("after", "episode", _topic(rng, 1, 1)[0].tobytes())])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_layout_file_is_owner_only(two_topics) -> None:
    side, *_ = two_topics
    assert stat.S_IMODE(side.path.stat().st_mode) == 0o600


def test_a_vector_of_another_width_keeps_its_fingerprint(tmp_path: Path) -> None:
    store = tmp_path / "memory.db"
    db = _make_store(store)
    rng = np.random.default_rng(9)
    for i, v in enumerate(_topic(rng, 0, 20)):
        _episode(db, f"a{i}", v)
    odd = _unit(rng.standard_normal(DIM * 2).astype(np.float32))
    _episode(db, "odd", odd, tags=["t"])
    db.commit()
    db.close()
    side = LayoutSidecar(gl.sidecar_path(store))
    gl.build_sidecar(store, side.path)
    assert side.fingerprints()["odd"] == gl.vector_fingerprint(odd.tobytes())

    late = _unit(rng.standard_normal(DIM * 2).astype(np.float32))
    side.place([PlaceItem("late", "episode", late.tobytes(), json.dumps(["t"]))])
    assert side.fingerprints()["late"] == gl.vector_fingerprint(late.tobytes())


def test_both_build_passes_read_one_snapshot(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "memory.db"
    db = _make_store(store)
    db.execute("PRAGMA journal_mode=WAL")
    for i, v in enumerate(_topic(np.random.default_rng(4), 0, 30)):
        _episode(db, f"a{i}", v)
    db.commit()
    db.close()
    clean = gl.read_store(store)

    real, passes = gl._iter_rows, []

    def rows_then_write(conn):
        yield from real(conn)
        passes.append(1)
        if len(passes) == 1:
            # A memory write that commits between the covariance and projection passes.
            writer = sqlite3.connect(store)
            writer.execute("UPDATE episodic_memories SET embedding = NULL WHERE id = 'a3'")
            writer.commit()
            writer.close()

    monkeypatch.setattr(gl, "_iter_rows", rows_then_write)
    got = gl.read_store(store)

    assert len(passes) == 2
    assert got.ids == clean.ids
    np.testing.assert_allclose(got.vectors, clean.vectors, atol=1e-5)


def test_the_build_child_opens_no_console_window(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.embeddings_child import _child_creationflags

    seen = {}
    monkeypatch.setattr(gl.subprocess, "Popen", lambda argv, **kw: seen.update(kw) or "child")
    assert gl.spawn_rebuild(tmp_path / "memory.db", tmp_path / "out.sqlite") == "child"
    assert seen["creationflags"] == _child_creationflags()


def test_build_in_a_child_process_writes_the_file(tmp_path: Path, two_topics) -> None:
    _, _, store = two_topics
    out = tmp_path / "child.sqlite"
    proc = gl.spawn_rebuild(store, out)
    try:
        # Half the per-test timeout, so a stalled child fails here, not by pytest-timeout.
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    meta = LayoutSidecar(out).meta()
    assert meta is not None
    assert meta["n"] == 120


def test_a_topic_is_named_by_words_not_numbers_and_never_twice() -> None:
    cluster = gl.Cluster(0, 0.0, 0.0, 1.0, 6)
    tokens = ["issue-12", "pr-7", "notes-2026-q4", "42", "beehive-hub", "beehive_hub", "release"]
    nodes = {f"r{i}": gl.Placed("fact", 0.0, 0.0, 0, "vector", tokens) for i in range(6)}
    gl._label_clusters([cluster], nodes, {t: 1.0 for t in tokens})
    # A phrase names the topic alone, so "release" is not added after it.
    assert cluster.label == "Beehive Hub"


def _link_rows(vectors: list[list[float]]) -> tuple[gl.StoreRows, dict, list]:
    unit = np.array(vectors, np.float32)
    unit /= np.linalg.norm(unit, axis=1, keepdims=True)
    ids = [f"r{i}" for i in range(len(vectors))]
    rows = gl.StoreRows(proj=None, ids=ids, kinds=["fact"] * len(ids), unit=unit.astype(np.float16))
    nodes = {rid: gl.Placed("fact", 0.0, 0.0, 0, "vector", []) for rid in ids}
    cluster = gl.Cluster(0, 0.0, 0.0, 1.0, len(ids), centre=np.zeros(gl.PROJ_DIM, np.float32))
    return rows, nodes, [cluster]


def test_memory_links_are_undirected_nearest_neighbours_above_recall_floor() -> None:
    # r0..r3 point the same way give or take; r4 is orthogonal to them all.
    rows, nodes, clusters = _link_rows(
        [[1, 0.05, 0], [1, 0, 0.05], [1, 0.1, 0.1], [1, -0.05, 0], [0, 0, 1]]
    )
    links = gl.memory_links(rows, nodes, clusters)
    pairs = [(a, b) for a, b, _ in links]
    assert all(a < b for a, b in pairs)
    assert len(pairs) == len(set(pairs))
    assert all(sim >= gl.LINK_FLOOR for _, _, sim in links)
    assert not any("r4" in p for p in pairs)
    degree: dict[str, int] = {}
    for a, b in pairs:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    # Each of the four related rows reaches its LINK_K = 3 others, and no row links itself.
    assert set(degree) == {"r0", "r1", "r2", "r3"}
    assert len(pairs) == 6


def test_memory_links_floor_matches_recall_admission() -> None:
    from kiro_crew.vector_memory_runtime import episodic_search

    assert gl.LINK_FLOOR == episodic_search._EPISODIC_RELEVANCE_THRESHOLD


def test_rows_without_a_vector_get_no_links(tmp_path: Path) -> None:
    rows, nodes, clusters = _link_rows([[1, 0, 0], [1, 0.01, 0]])
    rows.unit = np.empty((0, 0), np.float16)
    assert gl.memory_links(rows, nodes, clusters) == []


def test_a_cluster_fills_its_sunflower_disc_with_no_two_dots_on_top_of_each_other() -> None:
    """Rows move onto the disc's sunflower slots (golden angle 137.508 degrees
    apart), one row a slot: the disc fills evenly and dots never pile up, which
    the raw map did (a fifth of the rows of a 23k store within 1 unit of another)."""
    assert math.degrees(gl.GOLDEN_ANGLE) == pytest.approx(137.508, abs=1e-3)
    rng = np.random.default_rng(5)
    n = 200
    p = rng.normal(size=(n, 2)) * [1.0, 0.2]
    radius = gl.DISC_UNIT * math.sqrt(n)
    raw = gl.spread_disc(p, 1 / float(np.median(np.hypot(p[:, 0], p[:, 1]))), radius)
    fitted = gl.sunflower_fit(raw, radius)
    slots = gl.sunflower_disc(n, radius)
    assert sorted(map(tuple, fitted.round(9))) == sorted(map(tuple, slots.round(9)))
    gaps = np.hypot(*(fitted[:, None, :] - fitted[None, :, :]).transpose(2, 0, 1))
    np.fill_diagonal(gaps, np.inf)
    assert gaps.min() > 0.3 * gl.SLOT_SPACING
    # The outermost row of the map stays at the rim, on its own side.
    far = int(np.argmax(np.hypot(raw[:, 0], raw[:, 1])))
    assert np.linalg.norm(fitted[far] - raw[far]) < gl.SLOT_SPACING


@pytest.mark.parametrize(
    ("tokens", "name"),
    [
        # A phrase names the topic alone, acronyms in capitals.
        (["pr-review", "security"], "PR Review"),
        # One word takes the next tag after an "&", never repeating a word.
        (["git", "rebase"], "Git & Rebase"),
        (["review", "pr-review"], "Review & PR"),
        (["security", "pr-review"], "Security & PR Review"),
        # Too long for two short lines: the first tag alone, never cut.
        (["userscriptkit", "kirocrewteamcapabilities"], "Userscriptkit"),
        (["i18n", "github_issues"], "i18n & GitHub Issues"),
        ([], ""),
    ],
)
def test_a_topic_is_named_in_a_few_whole_words(tokens: list[str], name: str) -> None:
    assert gl.topic_name(tokens) == name
    assert "…" not in gl.topic_name(tokens)
