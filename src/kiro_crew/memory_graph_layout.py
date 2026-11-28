"""Server-side 2D layout of every memory in one store, kept in a sidecar file.

This module lays out ALL memories of a store once, on the server, so the
dashboard's memory graph never samples or lays out in the browser. The result
lives in ``graph_layout.sqlite`` next to the store's database, and
:mod:`kiro_crew.memory_graph_sidecar` serves it.

:func:`build_sidecar` runs these steps (the method comparison behind them is in
the memory-graph design doc, section 11):

1. Read the store read-only, twice, in :data:`READ_CHUNK` blocks: the first
   pass sums the covariance of the unit-length embeddings, the second projects
   each row onto the top :data:`PROJ_DIM` principal components. The full-width
   matrix is never in memory.
2. Group the projected rows with k-means, place the cluster centres with
   classical MDS, push overlapping clusters apart, and spread each cluster's
   rows over a disc with that cluster's own 2-axis PCA. The centre, the two
   axes and the scale are kept per cluster, so a new row is placed by the same
   linear map (:func:`project_row`, :func:`local_position`).
3. Rows with no usable embedding are placed by their tokens: an experience's
   tags and a fact's key namespace. Each token's position is the mean of the
   rows already placed that carry it, and a row goes to the mean of its
   tokens' positions weighted by rarity. Up to :data:`TOKEN_ROUNDS` rounds let
   rows placed by tokens place others.
4. What is still unplaced goes into a band outside the layout, one arc per
   group (a fact's key namespace, else its kind).

The sidecar is derived data: it holds positions, cluster maps, token positions
and the projection basis, never memory text. A missing, corrupt or older-version
file is rebuilt, never trusted.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

# The store's own SQLite library: a second copy in the gateway keeps its own lock
# table, so its close drops the store connections' POSIX locks on memory.db.
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.memory_graph_files import GRAPH_LAYOUT_FILE as SIDECAR_NAME
from kiro_crew.memory_graph_names import (
    cluster_terms,
    is_weak,
    known_names,
    memory_title,
    mentions,
    name_terms,
)
from kiro_crew.security import redact

#: Bumped when the file's tables or the meaning of a column change; an older
#: file is rebuilt rather than read.
LAYOUT_VERSION = 13
#: Principal components each embedding is projected onto.
PROJ_DIM = 64
#: Rows read from the store per fetch; keeps a 100k-row build under 150 MB.
READ_CHUNK = 1024
#: Rows per distance block in k-means, so no n-by-k matrix is held at once.
DIST_CHUNK = 8192
KMEANS_ITERS = 8
#: k-means makes ``CLUSTER_SCALE * sqrt(n / 2)`` clusters. 2 gave the most
#: similar 2D neighbours on real store copies (design doc section 11); fewer
#: clusters put several topics on one disc.
CLUSTER_SCALE = 2.0
#: Rounds of token placement after the first, in which rows placed by tokens
#: may place others.
TOKEN_ROUNDS = 3
#: A token on more than this share of rows says nothing about place
#: (``kirocrew`` and ``project`` on a busy bot's store)...
TOKEN_MAX_SHARE = 0.05
#: ...but in a small store a real topic is a large share, so the cap never
#: drops below this many rows.
TOKEN_MIN_CAP = 50
#: Share of rows placed incrementally since the last full build past which a
#: full rebuild is due.
REBUILD_DRIFT = 0.2

PLACED_VECTOR = "vector"
PLACED_TOKENS = "tokens"
PLACED_BAND = "band"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE projection (dim INTEGER NOT NULL, mean BLOB NOT NULL, basis BLOB NOT NULL);
CREATE TABLE clusters (
    id INTEGER PRIMARY KEY, x REAL NOT NULL, y REAL NOT NULL, r REAL NOT NULL,
    count INTEGER NOT NULL, label TEXT,
    centre BLOB, axes BLOB, scale REAL
);
CREATE TABLE tokens (token TEXT PRIMARY KEY, n INTEGER NOT NULL, sx REAL NOT NULL, sy REAL NOT NULL);
CREATE TABLE nodes (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
    cluster INTEGER NOT NULL, weight REAL NOT NULL DEFAULT 1.0,
    placed_by TEXT NOT NULL, placed_incr INTEGER NOT NULL DEFAULT 0,
    tokens TEXT NOT NULL DEFAULT '[]', fp TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_nodes_x ON nodes(x);
CREATE INDEX idx_nodes_weight ON nodes(weight DESC);
CREATE TABLE links (a TEXT NOT NULL, b TEXT NOT NULL, sim REAL NOT NULL, PRIMARY KEY (a, b));
CREATE INDEX idx_links_b ON links(b);
"""

#: ``(rows query, table kind)``: each query yields ``(id, embedding, tags)``.
#: A bot store answers both through views over ``memory_items``.
_TAG_TABLE_QUERIES = (
    ("SELECT id, embedding, tags FROM episodic_memories WHERE is_deleted = 0", None),
    ("SELECT key, embedding, NULL FROM semantic_memory WHERE is_deleted = 0", "semantic"),
)


def sidecar_path(db_path: Path) -> Path:
    """The layout file that belongs to the store database at *db_path*."""
    return Path(db_path).parent / SIDECAR_NAME


def row_kind(row_id: str, table_kind: str | None) -> str:
    """``episode`` for an experience, ``lesson`` or ``fact`` for a semantic row."""
    if table_kind is None:
        return "episode"
    return "lesson" if row_id.startswith("lesson.") else "fact"


def row_tokens(row_id: str, kind: str, tags: object) -> list[str]:
    """The tokens that place a row with no vector: its tags, and for a fact the
    namespace parts of its key (``project.acme_bug.root_cause`` gives
    ``project`` and ``acme_bug``; the leaf names one fact, not a topic)."""
    out: set[str] = set()
    raw = tags
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            raw = []
    # Scrub each tag or namespace before case folding removes credential syntax.
    if isinstance(raw, list):
        out |= {redact(t).strip().lower() for t in raw if isinstance(t, str) and t.strip()}
    if kind == "fact":
        out |= {redact(p).lower() for p in row_id.split(".")[:-1] if len(p) > 2}
    return sorted(out)


def vector_fingerprint(blob: bytes | None) -> str:
    """A cheap mark of a row's vector: its first 16 bytes in upper-case hex,
    which SQLite computes as ``hex(substr(embedding, 1, 16))`` without reading
    the rest. A re-embedded row changes it."""
    return blob[:16].hex().upper() if blob else ""


def band_group(row_id: str, kind: str) -> str:
    """The band arc a row with nothing to place it by goes on."""
    if kind == "fact" and "." in row_id:
        return row_id.rsplit(".", 1)[0]
    return kind


def jitter(row_id: str, radius: float) -> tuple[float, float]:
    """A small fixed offset per id, so rows with the same tokens do not stack."""
    h = hashlib.blake2b(row_id.encode(), digest_size=8).digest()
    a = int.from_bytes(h[:4], "little") / 2**32 * 2 * math.pi
    r = radius * math.sqrt(int.from_bytes(h[4:], "little") / 2**32)
    return r * math.cos(a), r * math.sin(a)


@dataclass
class Projection:
    """Unit-normalise, subtract the mean, project onto the basis."""

    dim: int
    mean: np.ndarray  # (dim,) float32
    basis: np.ndarray  # (dim, PROJ_DIM) float32

    def project(self, raw: np.ndarray) -> np.ndarray:
        """Rows of width :attr:`dim` to :data:`PROJ_DIM` columns.

        ``einsum`` rather than ``@``: placement runs in the gateway, whose BLAS
        pool is not capped, and a product through it ran 4x slower on a loaded
        host.
        """
        a = raw.astype(np.float32, copy=False)
        norms = np.linalg.norm(a, axis=1, keepdims=True)
        a = a / np.where(norms > 0, norms, 1) - self.mean
        return np.einsum("ij,jk->ik", a, self.basis)


@dataclass
class StoreRows:
    """What :func:`read_store` read."""

    proj: Projection | None
    #: Rows with a usable vector, in the order of :attr:`vectors`.
    ids: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    vectors: np.ndarray = field(default_factory=lambda: np.empty((0, PROJ_DIM), np.float32))
    #: ``(id, kind)`` of rows with no vector, or one of another width.
    unembedded: list[tuple[str, str]] = field(default_factory=list)
    #: Tokens of every row, embedded or not.
    tokens: dict[str, list[str]] = field(default_factory=dict)
    #: :func:`vector_fingerprint` of each row with a vector.
    fps: dict[str, str] = field(default_factory=dict)
    #: Each vector row's unit embedding at full width, in the order of
    #: :attr:`ids`, as float16: what the links between memories are scored on.
    unit: np.ndarray = field(default_factory=lambda: np.empty((0, 0), np.float16))
    #: Each row's text (an experience's, a lesson's rule, a fact's value), read
    #: only to name the topics (:mod:`memory_graph_names`); never written out.
    texts: dict[str, str] = field(default_factory=dict)
    #: Names that read as a person, not a subject: the bots that keep stores
    #: beside this one and the ``@handles`` in its text.
    names: set[str] = field(default_factory=set)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)


def _iter_rows(db: sqlite3.Connection) -> Iterable[tuple[str, str, bytes | None, object]]:
    for sql, table_kind in _TAG_TABLE_QUERIES:
        try:
            cur = db.execute(sql)
        except sqlite3.OperationalError:
            continue  # a store without this table has no rows of its kind
        while rows := cur.fetchmany(READ_CHUNK):
            for rid, blob, tags in rows:
                rid = str(rid)
                yield rid, row_kind(rid, table_kind), blob, tags


def _store_width(db: sqlite3.Connection) -> int:
    """The embedding width of the store: that of its most common width."""
    widths: dict[int, int] = defaultdict(int)
    for sql in (
        "SELECT length(embedding), COUNT(*) FROM episodic_memories "
        "WHERE is_deleted = 0 AND embedding IS NOT NULL GROUP BY 1",
        "SELECT length(embedding), COUNT(*) FROM semantic_memory "
        "WHERE is_deleted = 0 AND embedding IS NOT NULL GROUP BY 1",
    ):
        try:
            for nbytes, count in db.execute(sql):
                if nbytes and nbytes % 4 == 0:
                    widths[nbytes // 4] += count
        except sqlite3.OperationalError:
            continue
    return max(widths, key=lambda w: widths[w]) if widths else 0


def read_store(db_path: Path) -> StoreRows:
    """Read every live experience, fact and lesson from the store, read-only.

    Two streaming passes (covariance, then projection), so peak memory is the
    projected matrix plus one :data:`READ_CHUNK` block.
    """
    db = _connect_ro(db_path)
    try:
        # One read transaction, so the width query and both passes see one
        # snapshot even while memory writes commit (the store runs in WAL mode).
        db.execute("BEGIN")
        dim = _store_width(db)
        cov = np.zeros((dim, dim))
        total = np.zeros(dim)
        count = 0
        out = StoreRows(proj=None)
        batch: list[np.ndarray] = []

        def flush_cov() -> None:
            nonlocal cov, total, count
            if not batch:
                return
            a = np.stack(batch).astype(np.float64)
            norms = np.linalg.norm(a, axis=1, keepdims=True)
            a /= np.where(norms > 0, norms, 1)
            cov += a.T @ a
            total += a.sum(0)
            count += len(a)
            batch.clear()

        for rid, kind, blob, tags in _iter_rows(db):
            out.tokens[rid] = row_tokens(rid, kind, tags)
            if dim and blob and len(blob) == dim * 4:
                out.ids.append(rid)
                out.kinds.append(kind)
                out.fps[rid] = vector_fingerprint(blob)
                batch.append(np.frombuffer(blob, np.float32))
                if len(batch) >= READ_CHUNK:
                    flush_cov()
            else:
                if blob:
                    # A vector of another width cannot be projected, but its
                    # fingerprint is filed so the reconcile does not place it again.
                    out.fps[rid] = vector_fingerprint(blob)
                out.unembedded.append((rid, kind))
        flush_cov()
        if not count:
            return out
        mean = total / count
        _, vecs = np.linalg.eigh(cov / count - np.outer(mean, mean))
        k = min(PROJ_DIM, dim)
        basis = np.zeros((dim, PROJ_DIM), np.float32)
        basis[:, :k] = vecs[:, ::-1][:, :k]
        out.proj = Projection(dim=dim, mean=mean.astype(np.float32), basis=basis)

        index = {rid: i for i, rid in enumerate(out.ids)}
        out.vectors = np.empty((len(out.ids), PROJ_DIM), np.float32)
        # shortcut: holds every vector at full width (float16, 47 MB for 23k rows
        # of 1024); score links per cluster from the store if stores grow past ~200k rows.
        out.unit = np.zeros((len(out.ids), dim), np.float16)
        pending: list[tuple[int, bytes]] = []

        def flush_proj() -> None:
            if not pending or out.proj is None:
                return
            raw = np.frombuffer(b"".join(b for _, b in pending), np.float32).reshape(-1, dim)
            at = [i for i, _ in pending]
            out.vectors[at] = out.proj.project(raw)
            norms = np.linalg.norm(raw, axis=1, keepdims=True)
            out.unit[at] = (raw / np.where(norms > 0, norms, 1)).astype(np.float16)
            pending.clear()

        for rid, _, blob, _ in _iter_rows(db):
            i = index.get(rid)
            if i is not None and blob and len(blob) == dim * 4:
                pending.append((i, blob))
                if len(pending) >= READ_CHUNK:
                    flush_proj()
        flush_proj()
        out.texts = _read_texts(db)
        out.names = known_names(Path(db_path)) | mentions(out.texts.values())
        return out
    finally:
        db.close()


def _read_texts(db: sqlite3.Connection) -> dict[str, str]:
    """Each live row's text, for naming topics: an experience's text, a fact's
    or lesson's value (a lesson's rule when its value is a record)."""
    out: dict[str, str] = {}
    for sql in (
        "SELECT id, text FROM episodic_memories WHERE is_deleted = 0",
        "SELECT key, value_json FROM semantic_memory WHERE is_deleted = 0",
    ):
        try:
            cur = db.execute(sql)
        except sqlite3.OperationalError:
            continue
        while rows := cur.fetchmany(READ_CHUNK):
            for rid, raw in rows:
                text = raw if isinstance(raw, str) else ""
                if sql.startswith("SELECT key"):
                    try:
                        value = json.loads(text) if text else ""
                    except ValueError:
                        value = text
                    if isinstance(value, dict):
                        value = value.get("rule") or value.get("text") or ""
                    text = value if isinstance(value, str) else ""
                # Both term extraction and fallback titles consume this scrubbed text.
                out[str(rid)] = redact(text)
    return out


def _nearest(x: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Index of the nearest centre for each row, in :data:`DIST_CHUNK` blocks."""
    lab = np.empty(len(x), dtype=np.int32)
    cc = (c * c).sum(1)
    for s in range(0, len(x), DIST_CHUNK):
        lab[s : s + DIST_CHUNK] = (cc[None, :] - 2 * x[s : s + DIST_CHUNK] @ c.T).argmin(1)
    return lab


def _kmeans(x: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(1)
    c = x[rng.choice(len(x), k, replace=False)].copy()
    for _ in range(KMEANS_ITERS):
        lab = _nearest(x, c)
        sums = np.zeros_like(c)
        np.add.at(sums, lab, x)
        cnt = np.bincount(lab, minlength=k)
        keep = cnt > 0
        c[keep] = sums[keep] / cnt[keep, None]
    return c, _nearest(x, c)


@dataclass
class Cluster:
    id: int
    x: float
    y: float
    r: float
    count: int
    label: str | None = None
    #: The cluster's linear map for a vector row; None on a band arc.
    centre: np.ndarray | None = None  # (PROJ_DIM,)
    axes: np.ndarray | None = None  # (2, PROJ_DIM)
    scale: float = 0.0


def spread_disc(p: np.ndarray, scale: float, radius: float) -> np.ndarray:
    """Offsets for rows at 2-axis positions *p* (n x 2) in a disc of *radius*.

    Each row keeps its direction from the centre. Its distance goes through the
    radial distribution of a 2-D normal whose median is ``1 / scale``, then to
    the radius that holds that share of the disc's area, so a cluster's rows
    cover the disc at an even density instead of piling up at the middle. Every
    row stays inside the disc.
    """
    d = np.hypot(p[:, 0], p[:, 1])
    share = 1.0 - np.exp(-math.log(2) * (d * scale) ** 2)
    r = radius * np.sqrt(share)
    with np.errstate(invalid="ignore", divide="ignore"):
        unit = np.where(d[:, None] > 0, p / d[:, None], 0.0)
    return unit * r[:, None]


def local_position(cluster: Cluster, vec: np.ndarray) -> tuple[float, float]:
    """Where the cluster's own map puts the projected row *vec*.

    The same map the build used for the cluster's members (its 2-axis
    projection, then :func:`spread_disc`), so a new row lands where a full
    build would have put it, give or take the cluster's drift.
    """
    assert cluster.centre is not None and cluster.axes is not None
    raw = np.einsum("ij,j->i", cluster.axes, vec - cluster.centre)
    off = spread_disc(raw[None, :], cluster.scale, cluster.r)[0]
    return cluster.x + float(off[0]), cluster.y + float(off[1])


def nearest_cluster(clusters: Sequence[Cluster], vec: np.ndarray) -> Cluster | None:
    """The vector cluster whose centre is nearest *vec*; None when there is none."""
    best: Cluster | None = None
    best_d = math.inf
    for c in clusters:
        if c.centre is None:
            continue
        diff = c.centre - vec
        d = float(np.einsum("i,i->", diff, diff))
        if d < best_d:
            best, best_d = c, d
    return best


@dataclass
class Placed:
    kind: str
    x: float
    y: float
    cluster: int
    placed_by: str
    tokens: list[str]
    fp: str = ""


@dataclass
class Layout:
    nodes: dict[str, Placed]
    clusters: list[Cluster]
    proj: Projection | None
    token_sums: dict[str, tuple[int, float, float]]
    #: ``(a, b, cosine)`` with ``a < b``: see :func:`memory_links`.
    links: list[tuple[str, str, float]] = field(default_factory=list)


#: The golden ratio: the client's zoom levels step by it (graphView.ts PHI),
#: so the layout's spiral and the view's levels share one rhythm.
PHI = (1.0 + math.sqrt(5.0)) / 2.0
#: The golden angle, 2*pi / PHI**2 (137.508 degrees): consecutive slots at this
#: turn never line up, so a sunflower spiral fills a disc evenly whatever the count.
GOLDEN_ANGLE = 2.0 * math.pi / PHI**2
#: A cluster's disc radius is ``DISC_UNIT * sqrt(count)``: about 20 square
#: units a row, whatever the cluster's size.
DISC_UNIT = 2.5
#: The mean distance between neighbours on such a disc's sunflower: one row's
#: share of the area, ``sqrt(pi) * DISC_UNIT`` (about 4.4 units).
SLOT_SPACING = math.sqrt(math.pi) * DISC_UNIT


def _mds2(c: np.ndarray) -> np.ndarray:
    """Classical MDS of the centres onto 2 axes, each scaled to span [-1, 1]."""
    k = len(c)
    d2 = ((c[:, None, :] - c[None, :, :]) ** 2).sum(-1).astype(np.float64)
    j = np.eye(k) - 1.0 / k
    w, v = np.linalg.eigh(-0.5 * j @ d2 @ j)
    cp = v[:, -2:] * np.sqrt(np.maximum(w[-2:], 1e-9))
    # Each axis to the same span, so the map fills a square view instead of a strip.
    return cp / (np.abs(cp).max(0) + 1e-9)


def _separate(cp: np.ndarray, rad: np.ndarray, gap: float, rounds: int = 60) -> np.ndarray:
    """Push discs apart until none overlaps, with *gap* between neighbours."""
    for _ in range(rounds):
        diff = cp[:, None, :] - cp[None, :, :]
        dist = np.sqrt((diff**2).sum(-1)) + 1e-9
        push = np.clip(rad[:, None] + rad[None, :] + gap - dist, 0, None)
        np.fill_diagonal(push, 0)
        if not push.any():
            break
        cp = cp + ((diff / dist[..., None]) * push[..., None]).sum(1) * 0.5
    return cp


def _assign(want: np.ndarray, slots: np.ndarray, order: np.ndarray) -> np.ndarray:
    """Give each of *want* (a cluster, or a row of one), in *order*, the free
    slot nearest the place it wants; returns each one's slot index."""
    free = np.ones(len(slots), bool)
    got = np.empty(len(want), np.int64)
    for i in order:
        d = ((slots - want[i]) ** 2).sum(1)
        d[~free] = np.inf
        got[i] = int(d.argmin())
        free[got[i]] = False
    return got


def _spiral_centres(c: np.ndarray, rad: np.ndarray) -> np.ndarray:
    """Cluster centres on a sunflower spiral: slot *i* at angle
    ``i * GOLDEN_ANGLE`` and radius ``sqrt(i)``. Each cluster, biggest first,
    takes the free slot nearest its place in a classical MDS of the centres, so
    topics that sit together by similarity sit together on the spiral; then the
    discs are pushed apart until none overlaps."""
    k = len(c)
    gap = float(np.median(rad)) * 0.35
    i = np.arange(k, dtype=np.float64)
    slots = np.stack(
        [np.sqrt(i + 0.5) * np.cos(i * GOLDEN_ANGLE), np.sqrt(i + 0.5) * np.sin(i * GOLDEN_ANGLE)],
        1,
    )
    want = _mds2(c) * math.sqrt(k)
    got = _assign(want, slots, np.argsort(-rad, kind="stable"))
    # Slot i encloses area pi * i * step**2, so one slot holds about one mean
    # disc with its gap: the spiral starts packed and _separate fixes the rest.
    step = (2.0 * float(rad.mean()) + gap) / math.sqrt(math.pi)
    return _separate(slots[got] * step, rad, gap)


def sunflower_disc(count: int, radius: float) -> np.ndarray:
    """*count* offsets on a sunflower disc of *radius*: slot 0 at the middle,
    later slots further out, at an even density."""
    i = np.arange(count, dtype=np.float64)
    r = radius * np.sqrt((i + 0.5) / max(count, 1))
    a = i * GOLDEN_ANGLE
    return np.stack([r * np.cos(a), r * np.sin(a)], 1)


def sunflower_fit(offsets: np.ndarray, radius: float) -> np.ndarray:
    """Move each of *offsets* (a cluster's rows, from :func:`spread_disc`) onto
    the free sunflower slot nearest it, outermost rows first, so related rows
    stay side by side, the most typical stay in the middle, and the disc fills
    at an even density with no two dots on top of each other."""
    slots = sunflower_disc(len(offsets), radius)
    order = np.argsort(-np.hypot(offsets[:, 0], offsets[:, 1]), kind="stable")
    return slots[_assign(offsets, slots, order)]


def _vector_layout(rows: StoreRows) -> tuple[dict[str, Placed], list[Cluster]]:
    x = rows.vectors
    n = len(x)
    if not n:
        return {}, []
    k = max(1, min(n, int(round(CLUSTER_SCALE * math.sqrt(n / 2)))))
    centres, lab = _kmeans(x, k)
    counts = np.bincount(lab, minlength=k)
    # Disc area grows with the row count, about 20 square units a row.
    rad = np.sqrt(np.maximum(counts, 1)) * DISC_UNIT
    cp = _spiral_centres(centres, rad) if k > 1 else np.zeros((1, 2))
    nodes: dict[str, Placed] = {}
    clusters: list[Cluster] = []
    for jj in range(k):
        m = np.flatnonzero(lab == jj)
        if not len(m):
            continue
        centre = x[m].mean(0)
        loc = x[m] - centre
        if len(m) > 2:
            _, _, vt = np.linalg.svd(loc[:2000], full_matrices=False)
            axes = vt[:2] if len(vt) >= 2 else np.vstack([vt, np.zeros_like(vt)])
        else:
            axes = np.zeros((2, PROJ_DIM), np.float32)
            axes[0, 0] = axes[1, 1] = 1.0
        p = loc @ axes.T
        # 1 / the median distance from the centre: spread_disc maps that median
        # to the radius holding half the disc, so rows sit evenly, not in a clump.
        scale = 1.0 / (float(np.median(np.hypot(p[:, 0], p[:, 1]))) + 1e-9)
        cl = Cluster(
            id=jj,
            x=float(cp[jj, 0]),
            y=float(cp[jj, 1]),
            r=float(rad[jj]),
            count=len(m),
            centre=centre.astype(np.float32),
            axes=axes.astype(np.float32),
            scale=scale,
        )
        clusters.append(cl)
        offsets = sunflower_fit(spread_disc(p, scale, float(rad[jj])), float(rad[jj]))
        for i, (px, py) in zip(m, offsets):
            rid = rows.ids[i]
            nodes[rid] = Placed(
                rows.kinds[i],
                cl.x + float(px),
                cl.y + float(py),
                jj,
                PLACED_VECTOR,
                rows.tokens.get(rid, []),
                rows.fps.get(rid, ""),
            )
    return nodes, clusters


def token_cap(n_rows: int) -> float:
    """Most rows a token may carry and still place a row."""
    return max(float(TOKEN_MIN_CAP), TOKEN_MAX_SHARE * n_rows)


def usable_tokens(tokens: dict[str, list[str]]) -> dict[str, float]:
    """Rarity weight (IDF) of each token worth placing by: on two rows or more,
    on at most :func:`token_cap` of them, and not on every row."""
    n = len(tokens)
    df: dict[str, int] = defaultdict(int)
    for toks in tokens.values():
        for t in toks:
            df[t] += 1
    cap = token_cap(n)
    return {t: math.log(n / c) for t, c in df.items() if 2 <= c <= cap and c < n}


def token_position(
    toks: Iterable[str],
    sums: dict[str, tuple[int, float, float]],
    idf: dict[str, float],
) -> tuple[float, float] | None:
    """IDF-weighted mean of the positions of *toks*, or None when none is known."""
    ax = ay = wsum = 0.0
    for t in toks:
        w = idf.get(t)
        s = sums.get(t)
        if not w or not s or not s[0]:
            continue
        ax += w * s[1] / s[0]
        ay += w * s[2] / s[0]
        wsum += w
    return (ax / wsum, ay / wsum) if wsum else None


def _add_to_sums(
    sums: dict[str, tuple[int, float, float]], toks: Iterable[str], x: float, y: float
) -> None:
    for t in toks:
        n, sx, sy = sums.get(t, (0, 0.0, 0.0))
        sums[t] = (n + 1, sx + x, sy + y)


def _closest_cluster_2d(clusters: Sequence[Cluster], x: float, y: float) -> int:
    best = min(clusters, key=lambda c: (c.x - x) ** 2 + (c.y - y) ** 2)
    return best.id


def compute_layout(rows: StoreRows) -> Layout:
    """Lay out *rows*: vector rows by cluster map, then tokens, then the band."""
    nodes, clusters = _vector_layout(rows)
    idf = usable_tokens(rows.tokens)
    sums: dict[str, tuple[int, float, float]] = {}
    for p in nodes.values():
        _add_to_sums(sums, (t for t in p.tokens if t in idf), p.x, p.y)

    left = list(rows.unembedded)
    for _ in range(1 + TOKEN_ROUNDS):
        if not left:
            break
        placed_now: list[tuple[str, str, float, float]] = []
        still: list[tuple[str, str]] = []
        for rid, kind in left:
            pos = token_position(rows.tokens.get(rid, []), sums, idf)
            if pos is None:
                still.append((rid, kind))
                continue
            jx, jy = jitter(rid, 2.0)
            placed_now.append((rid, kind, pos[0] + jx, pos[1] + jy))
        if not placed_now:
            break
        for rid, kind, x, y in placed_now:
            toks = rows.tokens.get(rid, [])
            cid = _closest_cluster_2d(clusters, x, y) if clusters else 0
            nodes[rid] = Placed(kind, x, y, cid, PLACED_TOKENS, toks, rows.fps.get(rid, ""))
            _add_to_sums(sums, (t for t in toks if t in idf), x, y)
        left = still

    if not clusters and nodes:
        # Every placed row came from tokens: one cluster holds them all.
        xs = np.array([[p.x, p.y] for p in nodes.values()])
        c = xs.mean(0)
        clusters.append(Cluster(0, float(c[0]), float(c[1]), float(np.abs(xs - c).max() + 1), 0))
    by_id = {c.id: c for c in clusters}
    for p in nodes.values():
        if p.placed_by == PLACED_TOKENS:
            by_id[p.cluster].count += 1

    extent = max((math.hypot(p.x, p.y) for p in nodes.values()), default=0.0)
    band_nodes, band_clusters = _band(left, extent, rows.fps)
    nodes.update(band_nodes)
    _label_clusters(clusters, nodes, idf, rows.texts, rows.names)
    return Layout(
        nodes=nodes,
        clusters=clusters + band_clusters,
        proj=rows.proj,
        token_sums=sums,
        links=memory_links(rows, nodes, clusters),
    )


#: Nearest neighbours each memory links to.
LINK_K = 3
#: Cosine a link needs: recall's own admission floor
#: (``episodic_search._EPISODIC_RELEVANCE_THRESHOLD``), so a drawn link joins two
#: memories recall would call related.
LINK_FLOOR = 0.55
#: Clusters searched for a row's neighbours: its own and the nearest others.
LINK_CLUSTERS = 3


def memory_links(
    rows: StoreRows, nodes: dict[str, Placed], clusters: Sequence[Cluster]
) -> list[tuple[str, str, float]]:
    """Each vector row's :data:`LINK_K` nearest rows by embedding cosine, kept
    at :data:`LINK_FLOOR` or above, as undirected pairs ``(a, b, cosine)`` with
    ``a < b`` and no pair twice. A row's candidates are the rows of its own
    cluster and of the :data:`LINK_CLUSTERS` - 1 clusters whose centres sit
    nearest, so the cost stays near linear in the row count. A row with no
    vector gets none."""
    n = len(rows.ids)
    if n < 2 or rows.unit.shape[0] != n:
        return []
    lab = np.array([nodes[rid].cluster for rid in rows.ids])
    vec = [(c.id, c.centre) for c in clusters if c.centre is not None]
    if not vec:
        return []
    ids = np.array([cid for cid, _ in vec])
    cen = np.stack([centre for _, centre in vec]).astype(np.float32)
    d2 = ((cen[:, None, :] - cen[None, :, :]) ** 2).sum(-1)
    near = np.argsort(d2, axis=1)[:, :LINK_CLUSTERS]
    members = {int(cid): np.flatnonzero(lab == cid) for cid in ids}
    best: dict[tuple[int, int], float] = {}
    for ci, cid in enumerate(ids):
        m = members[int(cid)]
        if not len(m):
            continue
        cand = np.concatenate([members[int(ids[j])] for j in near[ci]])
        if len(cand) < 2:
            continue
        sim = rows.unit[m].astype(np.float32) @ rows.unit[cand].astype(np.float32).T
        sim[m[:, None] == cand[None, :]] = -1.0
        k = min(LINK_K, len(cand) - 1)
        top = np.argpartition(-sim, k - 1, axis=1)[:, :k]
        for r, i in enumerate(m):
            for t in top[r]:
                s = float(sim[r, t])
                if s < LINK_FLOOR:
                    continue
                j = int(cand[t])
                # Keyed by the unordered pair, so i->j and j->i are one link
                # (cosine is symmetric, so both carry the same score).
                best[(min(int(i), j), max(int(i), j))] = s
    out = []
    for (p, q), s in best.items():
        a, b = sorted((rows.ids[p], rows.ids[q]))
        out.append((a, b, round(s, 4)))
    out.sort()
    return out


#: Tokens in a cluster's topic label.


#: Rows nearest a weak cluster's centre tried for a title, in order.
TITLE_TRIES = 20


def _label_clusters(
    clusters: list[Cluster],
    nodes: dict[str, Placed],
    idf: dict[str, float],
    texts: dict[str, str] | None = None,
    names: set[str] | None = None,
) -> None:
    """Name each cluster from the terms its rows share (:mod:`memory_graph_names`:
    c-TF-IDF over tags, key namespaces and the words and phrases of their text,
    handles and ids pushed down). When even the best term is weak, the cluster
    takes the short title of its most typical row: the one nearest its centre,
    where the spread puts the most typical. No model call."""
    members: dict[int, list[str]] = defaultdict(list)
    for rid, p in nodes.items():
        members[p.cluster].append(rid)
    texts = texts or {}
    scored, row_terms = cluster_terms(
        members,
        {rid: p.tokens for rid, p in nodes.items()},
        texts,
        names or set(),
        lambda t: t in idf and not _id_like(t),
    )
    used: set[str] = set()
    # Biggest first, so a name two topics would share goes to the bigger one.
    for c in sorted(clusters, key=lambda c: (-c.count, c.id)):
        terms = scored.get(c.id) or []
        rows = members.get(c.id) or []
        candidates: list[str] = []
        if not is_weak(terms):
            candidates.append(topic_name(name_terms(terms, rows, row_terms)))
            # The next terms alone, for when the first name is already taken.
            candidates += [topic_name([t]) for t, score in terms[1:4] if not is_weak([(t, score)])]
        # Nearest the centre first: the rows whose text gives a title.
        near = sorted(rows, key=lambda r: (nodes[r].x - c.x) ** 2 + (nodes[r].y - c.y) ** 2)
        candidates += [
            topic_name([t])
            for t in (memory_title(texts.get(r, ""), names or set()) for r in near[:TITLE_TRIES])
            if t
        ]
        name = next(
            (n for n in candidates if n and n not in used), candidates[0] if candidates else ""
        )
        if name:
            used.add(name)
            c.label = name


#: Words in a topic's name, not counting the "&" between two tags.
TOPIC_WORDS = 3
#: Most characters in a two-tag name; a longer one keeps its first tag. The
#: graph wraps a name to two lines of about 18 characters.
TOPIC_CHARS = 30
#: Words a reader knows as letters: written in capitals in a topic's name.
_ACRONYMS = frozenset(
    "acp api aws cd ci cli cpu cr css db e2e ec2 gh gpt gpu html iam id js json jsonl kas llm mcp os"
    " oss pr qa rfc s3 sdk sso ssh ts ui url ux".split()
)
#: Words with a spelling of their own.
_SPELLED = {"i18n": "i18n", "github": "GitHub", "webkit": "WebKit", "kirocrew": "Kiro Crew"}
_WORD_SPLIT = re.compile(r"[-_./:\s]+")


#: Small words kept lower case inside a name ("Drive to Green").
_SMALL = frozenset("a an and as at by for in of on or the to via vs with".split())


def _title(word: str, inner: bool = False) -> str:
    low = word.lower()
    if inner and low in _SMALL:
        return low
    if low in _SPELLED:
        return _SPELLED[low]
    return low.upper() if low in _ACRONYMS else low[:1].upper() + low[1:]


def topic_name(tokens: Sequence[str]) -> str:
    """A short name for a topic from its most distinctive *tokens* (tags or key
    namespaces, best first): one to :data:`TOPIC_WORDS` title-case words, never
    cut. The best token that reads as a phrase (``pr-review``: "PR Review")
    names the topic alone; a one-word best token takes the next token after an
    "&" ("Git & Rebase") when the words and :data:`TOPIC_CHARS` fit. A word another token already gave
    (``review`` after ``pr-review``) is not repeated."""
    seen: set[str] = set()
    parts: list[list[str]] = []
    for t in tokens:
        words = [w for w in _WORD_SPLIT.split(t) if w and w.lower().rstrip("s") not in seen]
        if not words:
            continue
        seen.update(w.lower().rstrip("s") for w in words)
        parts.append([_title(w, inner=j > 0) for j, w in enumerate(words[:TOPIC_WORDS])])
    if not parts:
        return ""
    first = parts[0]
    if len(first) == 1 and len(parts) > 1 and 1 + len(parts[1]) <= TOPIC_WORDS:
        both = f"{first[0]} & {' '.join(parts[1])}"
        if len(both) <= TOPIC_CHARS:
            return both
    return " ".join(first)


def _token_key(token: str) -> str:
    return token.lower().replace("-", "").replace("_", "").replace(".", "")


#: An issue, PR or ticket reference: ``issue-15267``, ``pr16076``, ``#123``.
_ID_PREFIX = re.compile(r"^(?:issue|pr|#)[-_]?\d", re.IGNORECASE)
_LONG_NUMBER = re.compile(r"\d{4,}")


def _id_like(token: str) -> bool:
    """True for a token that is an id, not a word: an issue/PR/``#`` reference,
    a run of 4+ digits anywhere, or a bare number."""
    return bool(_ID_PREFIX.match(token) or _LONG_NUMBER.search(token)) or token.isdigit()


def _band(
    rows: Sequence[tuple[str, str]], extent: float, fps: dict[str, str]
) -> tuple[dict[str, Placed], list[Cluster]]:
    """One arc per :func:`band_group`, on a circle just outside the layout."""
    if not rows:
        return {}, []
    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for rid, kind in rows:
        groups[band_group(rid, kind)].append((rid, kind))
    names = sorted(groups)
    radius = extent + 20.0 + math.sqrt(len(rows))
    circumference = 2 * math.pi * radius
    # Each arc's share of the circle follows its size, with a gap after it.
    weights = [len(groups[g]) + 2 for g in names]
    total = float(sum(weights))
    nodes: dict[str, Placed] = {}
    clusters: list[Cluster] = []
    angle = 0.0
    for gi, name in enumerate(names):
        span = 2 * math.pi * weights[gi] / total
        members = groups[name]
        cid = -1 - gi
        for mi, (rid, kind) in enumerate(members):
            a = angle + (mi + 1) / (len(members) + 2) * span
            nodes[rid] = Placed(
                kind,
                radius * math.cos(a),
                radius * math.sin(a),
                cid,
                PLACED_BAND,
                [],
                fps.get(rid, ""),
            )
        mid = angle + span / 2
        clusters.append(
            Cluster(
                cid,
                radius * math.cos(mid),
                radius * math.sin(mid),
                circumference * span / (4 * math.pi),
                len(members),
                label=name,
            )
        )
        angle += span
    return nodes, clusters


def write_layout(layout: Layout, path: Path) -> None:
    """Write *layout* to a fresh file at *path*."""
    path = Path(path)
    if path.exists():
        path.unlink()
    # Owner-only, like the store beside it: SQLite keeps an existing file's mode
    # and gives its journal the database's mode.
    os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
    db = sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
        if layout.proj is not None:
            db.execute(
                "INSERT INTO projection VALUES (?, ?, ?)",
                (layout.proj.dim, layout.proj.mean.tobytes(), layout.proj.basis.tobytes()),
            )
        db.executemany(
            "INSERT INTO clusters VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    c.id,
                    c.x,
                    c.y,
                    c.r,
                    c.count,
                    c.label,
                    None if c.centre is None else c.centre.tobytes(),
                    None if c.axes is None else c.axes.tobytes(),
                    c.scale,
                )
                for c in layout.clusters
            ),
        )
        db.executemany(
            "INSERT INTO tokens VALUES (?, ?, ?, ?)",
            ((t, n, sx, sy) for t, (n, sx, sy) in layout.token_sums.items()),
        )
        db.executemany("INSERT INTO links VALUES (?, ?, ?)", layout.links)
        db.executemany(
            "INSERT INTO nodes (id, kind, x, y, cluster, placed_by, tokens, fp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (rid, p.kind, p.x, p.y, p.cluster, p.placed_by, json.dumps(p.tokens), p.fp)
                for rid, p in layout.nodes.items()
            ),
        )
        meta = {
            "version": LAYOUT_VERSION,
            "built_at": time.time(),
            "positions": 0,
            "n": len(layout.nodes),
            "embed_dim": layout.proj.dim if layout.proj else 0,
            "incr_count": 0,
        }
        db.executemany(
            "INSERT INTO meta VALUES (?, ?)", ((k, json.dumps(v)) for k, v in meta.items())
        )
        db.commit()
    finally:
        db.close()


def build_sidecar(db_path: Path, out_path: Path) -> dict:
    """Read the store at *db_path*, lay it out and write *out_path*."""
    t0 = time.perf_counter()
    rows = read_store(db_path)
    layout = compute_layout(rows)
    write_layout(layout, out_path)
    by: dict[str, int] = defaultdict(int)
    for p in layout.nodes.values():
        by[p.placed_by] += 1
    return {
        "n": len(layout.nodes),
        "placed_by": dict(by),
        "clusters": len(layout.clusters),
        "seconds": round(time.perf_counter() - t0, 3),
        "bytes": Path(out_path).stat().st_size,
    }


#: Environment that pins every BLAS backend numpy may use to one thread. On a
#: loaded 32-core host the default thread pool made a 23k build 300x slower.
ONE_THREAD_ENV = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}


def spawn_rebuild(db_path: Path, out_path: Path) -> subprocess.Popen:
    """Start a full build of *db_path* into *out_path* in a child process.

    Returns at once; the caller polls the process and, once it exits 0, hands
    *out_path* to :meth:`kiro_crew.memory_graph_sidecar.LayoutSidecar.adopt`.
    """
    from kiro_crew.embeddings_child import _child_creationflags
    from kiro_crew.platform_compat import isolated_python_argv

    env = {**os.environ, **ONE_THREAD_ENV}
    return subprocess.Popen(
        isolated_python_argv(
            "-P", "-m", "kiro_crew.memory_graph_layout", "build", str(db_path), str(out_path)
        ),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # No console window on Windows (a gateway with no console would open one),
        # and below-normal priority, as the other background children do.
        creationflags=_child_creationflags(),
    )


def main(argv: Sequence[str]) -> int:
    if len(argv) == 3 and argv[0] == "build":
        build_sidecar(Path(argv[1]), Path(argv[2]))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


__all__ = [
    "Cluster",
    "Layout",
    "Placed",
    "Projection",
    "StoreRows",
    "band_group",
    "vector_fingerprint",
    "jitter",
    "build_sidecar",
    "compute_layout",
    "local_position",
    "nearest_cluster",
    "read_store",
    "row_tokens",
    "sidecar_path",
    "spawn_rebuild",
    "token_cap",
    "token_position",
    "usable_tokens",
    "write_layout",
]
