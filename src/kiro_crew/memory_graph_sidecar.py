"""Serve and incrementally update one store's memory graph layout file.

:mod:`kiro_crew.memory_graph_layout` writes ``graph_layout.sqlite``; this module
reads it for the dashboard and keeps it current between full builds:

* :meth:`LayoutSidecar.clusters`, :meth:`~LayoutSidecar.nodes_in_box` (paged by
  weight), :meth:`~LayoutSidecar.ids_in_box` (box select over every node,
  loaded or not) and :meth:`~LayoutSidecar.positions` (search hits);
* :meth:`~LayoutSidecar.place` puts a new or changed row where a full build
  would: by its cluster's linear map when it has a vector, by its tokens when it
  has none, else on the band. No other row moves.

Every read returns plain ids, kinds, positions and weights, so any renderer can
draw them. Each call opens its own short connection under one lock, so
:meth:`~LayoutSidecar.adopt` can replace the file between calls on every
platform (Windows refuses to replace an open file).
"""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from kiro_crew import memory_graph_layout as gl
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.memory_graph_files import PAGE_CAP

#: Ids per ``IN (...)`` lookup, under SQLite's variable limit on every build.
_IN_CHUNK = 500

Box = tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass(frozen=True)
class PlaceItem:
    """One row to place: its vector blob (or None) and its tags as stored."""

    id: str
    kind: str
    embedding: bytes | None = None
    tags: object = None


@dataclass
class _Model:
    """What placement needs from the file, read once per file version."""

    stamp: tuple[int, int]
    proj: gl.Projection | None
    clusters: list[gl.Cluster]
    band: dict[str, gl.Cluster]
    band_radius: float


class LayoutSidecar:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._model: _Model | None = None

    def _connect(self) -> sqlite3.Connection:
        # ``mode=rw``: only a build creates the file, never a read or a placement.
        return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=rw", uri=True)

    def _read(self, sql: str, args: Sequence = ()) -> list[tuple]:
        """Rows of *sql*; none when there is no usable layout file yet."""
        if self.meta() is None:
            return []
        with self._lock:
            db = self._connect()
            try:
                return db.execute(sql, args).fetchall()
            finally:
                db.close()

    # --- file state -----------------------------------------------------

    def meta(self) -> dict | None:
        """The file's meta row, or None when it is missing, corrupt or stale."""
        if not self.path.exists():
            return None
        try:
            with self._lock:
                db = self._connect()
                try:
                    rows = db.execute("SELECT key, value FROM meta").fetchall()
                finally:
                    db.close()
        except sqlite3.DatabaseError:
            return None
        meta = {k: json.loads(v) for k, v in rows}
        return meta if meta.get("version") == gl.LAYOUT_VERSION else None

    def revision(self) -> str:
        """Build time and placement counter, or "" with no usable file.

        Placing new or moved rows changes which memories a drawn box holds.
        """
        meta = self.meta()
        if meta is None:
            return ""
        return f"{meta['built_at']}.{meta.get('positions', 0)}"

    def drift(self) -> float:
        meta = self.meta() or {}
        n = int(meta.get("n") or 0)
        # A layout built while the store was empty counts each later row as drift.
        return int(meta.get("incr_count") or 0) / max(n, 1)

    def needs_rebuild(self, embed_dim: int | None = None) -> bool:
        """True when the file is unusable, was built for another embedding
        width, or more than :data:`~kiro_crew.memory_graph_layout.REBUILD_DRIFT`
        of its rows were placed incrementally."""
        meta = self.meta()
        if meta is None:
            return True
        if embed_dim and meta.get("embed_dim") != embed_dim:
            return True
        return self.drift() > gl.REBUILD_DRIFT

    def adopt(self, built: Path) -> None:
        """Replace the served file with the finished build at *built*."""
        with self._lock:
            os.replace(built, self.path)
            self._model = None

    # --- reads ----------------------------------------------------------

    def summary(self) -> dict:
        """Node counts per kind and the box that holds every node."""
        totals = dict(self._read("SELECT kind, COUNT(*) FROM nodes GROUP BY kind"))
        row = self._read("SELECT MIN(x), MIN(y), MAX(x), MAX(y) FROM nodes")
        bounds = list(row[0]) if row and row[0][0] is not None else [0.0, 0.0, 0.0, 0.0]
        return {"totals": totals, "bounds": bounds}

    def clusters(self) -> list[dict]:
        rows = self._read("SELECT id, x, y, r, count, label FROM clusters ORDER BY id")
        return [
            {"id": i, "x": x, "y": y, "r": r, "count": c, "label": label}
            for i, x, y, r, c, label in rows
        ]

    @staticmethod
    def _box_where(box: Box, kinds: Iterable[str] | None) -> tuple[str, list]:
        x0, y0, x1, y1 = box
        sql = "x BETWEEN ? AND ? AND y BETWEEN ? AND ?"
        args: list = [min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)]
        kinds = list(kinds or [])
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        return sql, args

    def nodes_in_box(
        self,
        box: Box,
        kinds: Iterable[str] | None = None,
        limit: int = PAGE_CAP,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """Nodes inside *box*, heaviest first, at most *limit* (capped at
        :data:`PAGE_CAP`). The second value is the cursor for the next page, or
        None on the last page. A malformed cursor raises ``ValueError``."""
        limit = max(1, min(int(limit), PAGE_CAP))
        where, args = self._box_where(box, kinds)
        if cursor:
            w_text, rowid_text = cursor.split(":", 1)
            w, rowid = float(w_text), int(rowid_text)
            where += " AND (weight < ? OR (weight = ? AND rowid > ?))"
            args += [w, w, rowid]
        rows = self._read(
            "SELECT rowid, id, kind, x, y, cluster, weight FROM nodes WHERE "
            f"{where} ORDER BY weight DESC, rowid LIMIT ?",
            [*args, limit + 1],
        )
        more = len(rows) > limit
        rows = rows[:limit]
        nodes = [
            {"id": i, "kind": k, "x": x, "y": y, "cluster": c, "weight": w}
            for _, i, k, x, y, c, w in rows
        ]
        nxt = f"{rows[-1][6]!r}:{rows[-1][0]}" if more and rows else None
        return nodes, nxt

    def ids_in_box(self, box: Box, kinds: Iterable[str] | None = None) -> list[str]:
        """Every id inside *box*, loaded on the client or not (box select)."""
        where, args = self._box_where(box, kinds)
        return [r[0] for r in self._read(f"SELECT id FROM nodes WHERE {where}", args)]

    def positions(
        self, ids: Sequence[str], kinds: Iterable[str] | None = None
    ) -> dict[str, tuple[float, float]]:
        """``id -> (x, y)`` for each of *ids* the file holds (search hits),
        only of *kinds* when given."""
        kinds = list(kinds or [])
        kind_sql = f" AND kind IN ({','.join('?' * len(kinds))})" if kinds else ""
        out: dict[str, tuple[float, float]] = {}
        for s in range(0, len(ids), _IN_CHUNK):
            part = list(ids[s : s + _IN_CHUNK])
            q = f"SELECT id, x, y FROM nodes WHERE id IN ({','.join('?' * len(part))}){kind_sql}"
            for i, x, y in self._read(q, [*part, *kinds]):
                out[i] = (x, y)
        return out

    def links(self, ids: Sequence[str]) -> dict[str, list[tuple[str, float]]]:
        """``id -> [(other id, cosine), ...]`` for each of *ids*: its links to
        other memories, in both directions (see ``memory_graph_layout.memory_links``)."""
        out: dict[str, list[tuple[str, float]]] = {}
        seen: set[tuple[str, str]] = set()
        for s in range(0, len(ids), _IN_CHUNK):
            part = list(ids[s : s + _IN_CHUNK])
            marks = ",".join("?" * len(part))
            q = f"SELECT a, b, sim FROM links WHERE a IN ({marks}) UNION SELECT a, b, sim FROM links WHERE b IN ({marks})"
            for a, b, sim in self._read(q, [*part, *part]):
                pair = (min(a, b), max(a, b))
                if pair in seen:
                    continue
                seen.add(pair)
                out.setdefault(a, []).append((b, sim))
                out.setdefault(b, []).append((a, sim))
        wanted = set(ids)
        return {i: sorted(v) for i, v in out.items() if i in wanted}

    def fingerprints(self) -> dict[str, str]:
        """``id -> vector fingerprint`` of every row (empty when it was not
        placed by its vector): what reconciling compares with the store."""
        return dict(self._read("SELECT id, fp FROM nodes"))

    def missing(self, ids: Iterable[str]) -> list[str]:
        """Those of *ids* the file has no row for (written during a rebuild)."""
        want = list(ids)
        return [i for i in want if i not in self.positions(want)]

    # --- writes ---------------------------------------------------------

    def set_weights(self, weights: dict[str, float]) -> None:
        """Store each row's recall weight. The caller computes it with recall's
        own scoring helper, so no formula lives here."""
        if self.meta() is None:
            return
        with self._lock:
            db = self._connect()
            try:
                db.executemany(
                    "UPDATE nodes SET weight = ? WHERE id = ?",
                    ((float(w), i) for i, w in weights.items()),
                )
                db.commit()
            finally:
                db.close()

    def remove(self, ids: Sequence[str]) -> None:
        if self.meta() is None:
            return
        with self._lock:
            db = self._connect()
            try:
                for rid in ids:
                    row = db.execute("SELECT cluster FROM nodes WHERE id = ?", (rid,)).fetchone()
                    if row is None:
                        continue
                    db.execute("DELETE FROM nodes WHERE id = ?", (rid,))
                    db.execute("DELETE FROM links WHERE a = ? OR b = ?", (rid, rid))
                    db.execute("UPDATE clusters SET count = MAX(count - 1, 0) WHERE id = ?", row)
                db.commit()
            finally:
                db.close()

    def _load_model(self, db: sqlite3.Connection) -> _Model:
        st = self.path.stat()
        stamp = (st.st_mtime_ns, st.st_ino)
        if self._model is not None and self._model.stamp == stamp:
            return self._model
        proj = None
        row = db.execute("SELECT dim, mean, basis FROM projection").fetchone()
        if row:
            dim, mean, basis = row
            proj = gl.Projection(
                dim=dim,
                mean=np.frombuffer(mean, np.float32),
                basis=np.frombuffer(basis, np.float32).reshape(dim, gl.PROJ_DIM),
            )
        clusters: list[gl.Cluster] = []
        band: dict[str, gl.Cluster] = {}
        band_radius = 0.0
        for cid, x, y, r, count, label, centre, axes, scale in db.execute(
            "SELECT id, x, y, r, count, label, centre, axes, scale FROM clusters"
        ):
            c = gl.Cluster(cid, x, y, r, count, label)
            if centre is not None and axes is not None:
                c.centre = np.frombuffer(centre, np.float32)
                c.axes = np.frombuffer(axes, np.float32).reshape(2, gl.PROJ_DIM)
                c.scale = float(scale or 0.0)
            if cid < 0 and label:
                band[label] = c
                band_radius = max(band_radius, math.hypot(x, y))
            clusters.append(c)
        if not band_radius:
            ext = db.execute("SELECT MAX(x*x + y*y) FROM nodes").fetchone()[0] or 0.0
            band_radius = math.sqrt(ext) + 20.0
        self._model = _Model(stamp, proj, clusters, band, band_radius)
        return self._model

    def place(self, items: Sequence[PlaceItem]) -> dict[str, tuple[float, float]]:
        """Place new or changed rows without moving any other row.

        A row with a vector of the file's width goes where its nearest
        cluster's map puts it. A row without one goes to the rarity-weighted
        mean of its tokens' positions, else onto its band arc. A changed row is
        moved, never duplicated.
        """
        meta = self.meta()
        if meta is None or not items:
            return {}
        placed: dict[str, tuple[float, float]] = {}
        with self._lock:
            db = self._connect()
            try:
                model = self._load_model(db)
                n_rows = max(int(meta.get("n") or 0), 1)
                token_rows = dict(
                    (t, (n, sx, sy))
                    for t, n, sx, sy in db.execute("SELECT token, n, sx, sy FROM tokens")
                )
                for item in items:
                    old = db.execute(
                        "SELECT cluster FROM nodes WHERE id = ?", (item.id,)
                    ).fetchone()
                    if old is not None:
                        db.execute("DELETE FROM nodes WHERE id = ?", (item.id,))
                        db.execute("DELETE FROM links WHERE a = ? OR b = ?", (item.id, item.id))
                        db.execute(
                            "UPDATE clusters SET count = MAX(count - 1, 0) WHERE id = ?", old
                        )
                    toks = gl.row_tokens(item.id, item.kind, item.tags)
                    x, y, cid, how = self._where(item, toks, model, token_rows, n_rows)
                    # Any vector, placed by it or not: the reconcile compares it with the live one.
                    fp = gl.vector_fingerprint(item.embedding) if item.embedding else ""
                    db.execute(
                        "INSERT INTO nodes (id, kind, x, y, cluster, placed_by, placed_incr, tokens, fp) "
                        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                        (item.id, item.kind, x, y, cid, how, json.dumps(toks), fp),
                    )
                    db.execute("UPDATE clusters SET count = count + 1 WHERE id = ?", (cid,))
                    for t in toks:
                        n, sx, sy = token_rows.get(t, (0, 0.0, 0.0))
                        token_rows[t] = (n + 1, sx + x, sy + y)
                        db.execute(
                            "INSERT INTO tokens VALUES (?, ?, ?, ?) ON CONFLICT(token) DO UPDATE "
                            "SET n = excluded.n, sx = excluded.sx, sy = excluded.sy",
                            (t, *token_rows[t]),
                        )
                    placed[item.id] = (x, y)
                incr = int(meta.get("incr_count") or 0) + len(placed)
                db.execute(
                    "UPDATE meta SET value = ? WHERE key = 'incr_count'", (json.dumps(incr),)
                )
                db.execute(
                    "INSERT INTO meta (key, value) VALUES ('positions', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
                )
                db.commit()
            finally:
                db.close()
            # This process's own write changed the file's stamp; the cached
            # maps are still those the file holds.
            if self._model is not None:
                st = self.path.stat()
                self._model.stamp = (st.st_mtime_ns, st.st_ino)
        return placed

    @staticmethod
    def _where(
        item: PlaceItem,
        toks: list[str],
        model: _Model,
        token_rows: dict[str, tuple[int, float, float]],
        n_rows: int,
    ) -> tuple[float, float, int, str]:
        proj = model.proj
        if proj is not None and item.embedding and len(item.embedding) == proj.dim * 4:
            vec = proj.project(np.frombuffer(item.embedding, np.float32).reshape(1, -1))[0]
            cluster = gl.nearest_cluster(model.clusters, vec)
            if cluster is not None:
                x, y = gl.local_position(cluster, vec)
                return x, y, cluster.id, gl.PLACED_VECTOR
        cap = gl.token_cap(n_rows)
        idf = {
            t: math.log(n_rows / token_rows[t][0])
            for t in toks
            if t in token_rows and token_rows[t][0] <= cap and token_rows[t][0] < n_rows
        }
        pos = gl.token_position(toks, token_rows, idf)
        if pos is not None:
            jx, jy = gl.jitter(item.id, 2.0)
            x, y = pos[0] + jx, pos[1] + jy
            vector_clusters = [c for c in model.clusters if c.id >= 0]
            cid = min(
                vector_clusters or model.clusters, key=lambda c: (c.x - x) ** 2 + (c.y - y) ** 2
            ).id
            return x, y, cid, gl.PLACED_TOKENS
        arc = model.band.get(gl.band_group(item.id, item.kind))
        if arc is not None:
            jx, jy = gl.jitter(item.id, max(arc.r * 0.2, 1.0))
            return arc.x + jx, arc.y + jy, arc.id, gl.PLACED_BAND
        h = gl.jitter(item.id, 1.0)
        angle = math.atan2(h[1], h[0])
        r = model.band_radius
        fallback = model.clusters[0].id if model.clusters else 0
        return r * math.cos(angle), r * math.sin(angle), fallback, gl.PLACED_BAND
