"""Keep each store's memory graph layout built and current, for the dashboard.

One :class:`StoreGraph` per store database, from :func:`graph_for`:

* :meth:`StoreGraph.refresh` is called (in a worker thread) before each graph
  read. It starts a full build in a child process when the layout file is
  missing, stale or drifted, adopts a finished build, and otherwise brings the
  file up to date with the store: rows written since the last look are placed,
  rows forgotten are removed, and a row that gained a vector (the embedding
  backfill) moves to its place. No memory write path calls into the graph, so
  a graph failure can never fail a write.
* The look is cheap when nothing changed: it compares the store files' stat
  (database and WAL) with the last one it reconciled against.
* Each row's weight is recall's own weight for it
  (:func:`kiro_crew.vector_memory_runtime.episodic_search.recall_weight`),
  refreshed on a new day or when the tier's decay rates change. Facts and lessons do not fade,
  and neither does any row of a V2 store (:func:`kiro_crew.memory_decay.applies`).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew import memory_graph_layout as gl
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.memory_graph_files import (
    BUILDING_SUFFIX,
    GRAPH_LAYOUT_FILE,
    STATUS_BUILDING,
    STATUS_READY,
    building_name,
)
from kiro_crew.memory_graph_sidecar import LayoutSidecar, PlaceItem

logger = logging.getLogger(__name__)

#: Seconds before a failed build is tried again.
BUILD_RETRY_SECS = 60.0
#: Rows read per fetch while reconciling.
_CHUNK = 1024

#: Live rows: ``(layout id, kind, vector fingerprint)``, where the fingerprint
#: is :func:`kiro_crew.memory_graph_layout.vector_fingerprint` computed in SQL.
#: Experiences are keyed by id, facts and lessons by key, as the build keys them.
_LIVE_SQL = (
    "SELECT id, 'episode', COALESCE(hex(substr(embedding, 1, 16)), '') "
    "FROM episodic_memories WHERE is_deleted = 0 UNION ALL "
    "SELECT key, CASE WHEN key LIKE 'lesson.%' THEN 'lesson' ELSE 'fact' END, "
    "COALESCE(hex(substr(embedding, 1, 16)), '') FROM semantic_memory WHERE is_deleted = 0"
)


def _files_stamp(db_path: Path) -> tuple:
    out: list[tuple[int, int, int] | None] = []
    for name in (db_path, Path(f"{db_path}-wal")):
        try:
            st = os.stat(name)
            out.append((st.st_ino, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _drop_unadopted_builds(store_dir: Path) -> None:
    """Remove every unadopted build file (and its SQLite journals) in *store_dir*.

    Called only as a new build starts, while this process holds the store's
    lock, so no build of ours still needs one. A file an old build child still
    holds open is left for the next start (Windows refuses the unlink)."""
    for p in store_dir.glob(f".{GRAPH_LAYOUT_FILE}.*{BUILDING_SUFFIX}*"):
        try:
            p.unlink()
        except OSError:
            logger.debug("memory graph: could not remove the old build file %s", p.name)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


@dataclass
class _Build:
    proc: Any
    out: Path


class StoreGraph:
    """The layout of one store database and the work that keeps it current."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.sidecar = LayoutSidecar(gl.sidecar_path(self.db_path))
        self._lock = threading.Lock()
        self._build: _Build | None = None
        self._failed_at = 0.0
        self._stamp: tuple | None = None
        self._weights_key: tuple | None = None
        self._width: tuple[tuple, int | None] | None = None

    # --- the one entry point ---------------------------------------------

    def refresh(self, tier: Any, now: datetime | None = None) -> str:
        """Bring the layout up to date; return :data:`STATUS_READY` or
        :data:`STATUS_BUILDING`. Blocking: call it from a worker thread."""
        now = now or datetime.now(tz=timezone.utc)
        with self._lock:
            # While a build runs, a usable older file keeps serving and
            # reconciling; the finished build is reconciled again on adoption.
            building = self._build is not None and not self._finish_build(tier, now)
            stamp = _files_stamp(self.db_path)
            if not building and self.sidecar.needs_rebuild(self._width_at(stamp)):
                if not (self._failed_at and time.monotonic() - self._failed_at < BUILD_RETRY_SECS):
                    self._start_build()
            if self.sidecar.meta() is None:
                return STATUS_BUILDING
            if stamp != self._stamp:
                self._reconcile(tier, now)
                self._stamp = stamp
            elif self._weights_key != self._weight_key(tier, now):
                self._refresh_weights(tier, now)
            return STATUS_READY

    # --- full builds -------------------------------------------------------

    def _start_build(self) -> None:
        out = self.sidecar.path.with_name(building_name(os.getpid()))
        # A build an earlier gateway started and never adopted (a restart or a
        # crash mid-build) left its file, named after that gateway's pid: drop
        # it, and our own leftover, before writing a new one.
        _drop_unadopted_builds(self.sidecar.path.parent)
        self._build = _Build(gl.spawn_rebuild(self.db_path, out), out)

    def _finish_build(self, tier: Any, now: datetime) -> bool:
        """True once no build is running (adopting a finished one first)."""
        assert self._build is not None
        rc = self._build.proc.poll()
        if rc is None:
            return False
        build, self._build = self._build, None
        if rc == 0 and build.out.exists():
            self.sidecar.adopt(build.out)
            self._failed_at = 0.0
            # Stamp first: a write that lands during the reconcile changes the
            # files after this stamp, so the next read reconciles it.
            stamp = _files_stamp(self.db_path)
            self._reconcile(tier, now)
            self._stamp = stamp
        else:
            logger.warning("memory graph build for %s exited %s", self.db_path, rc)
            self._failed_at = time.monotonic()
            try:
                build.out.unlink()
            except FileNotFoundError:
                pass
        return True

    def wait_for_build(self, timeout: float) -> None:
        """Block until a running build exits (tests and the CLI)."""
        build = self._build
        if build is not None:
            build.proc.wait(timeout=timeout)

    def stop(self) -> None:
        """End a running build and remove its half-written file."""
        with self._lock:
            build, self._build = self._build, None
        if build is None:
            return
        if build.proc.poll() is None:
            build.proc.kill()
            build.proc.wait(timeout=10)
        try:
            build.out.unlink()
        except FileNotFoundError:
            pass

    def _width_at(self, stamp: tuple) -> int | None:
        """The store's vector width, read again only when its files changed: the
        query scans the store, and every graph request asks."""
        if self._width is None or self._width[0] != stamp:
            self._width = (stamp, self._store_width())
        return self._width[1]

    def _store_width(self) -> int | None:
        try:
            db = _connect_ro(self.db_path)
        except sqlite3.Error:
            return None
        try:
            return gl._store_width(db) or None
        finally:
            db.close()

    # --- incremental upkeep ------------------------------------------------

    def _reconcile(self, tier: Any, now: datetime) -> None:
        """Place rows the file lacks or that gained a vector; drop forgotten rows."""
        if self.sidecar.meta() is None:
            return
        db = _connect_ro(self.db_path)
        try:
            live = {str(rid): (kind, fp) for rid, kind, fp in db.execute(_LIVE_SQL)}
        finally:
            db.close()
        filed = self.sidecar.fingerprints()
        gone = [rid for rid in filed if rid not in live]
        # New rows, and rows whose vector appeared or changed. A row whose vector
        # went away keeps its place: where it sat is still its best place.
        todo = [
            (rid, kind)
            for rid, (kind, fp) in live.items()
            if rid not in filed or (fp and fp != filed[rid])
        ]
        if gone:
            self.sidecar.remove(gone)
        if todo:
            self.sidecar.place(self._items(todo))
        self._refresh_weights(tier, now)

    def _items(self, rows: list[tuple[str, str]]) -> list[PlaceItem]:
        """The vector and tags of each ``(id, kind)``, read from the store."""
        epi = [rid for rid, kind in rows if kind == "episode"]
        sem = [rid for rid, kind in rows if kind != "episode"]
        found: dict[str, tuple[bytes | None, object]] = {}
        db = _connect_ro(self.db_path)
        try:
            for ids, sql in (
                (
                    epi,
                    "SELECT id, embedding, tags FROM episodic_memories WHERE is_deleted = 0 AND id IN",
                ),
                (
                    sem,
                    "SELECT key, embedding, NULL FROM semantic_memory WHERE is_deleted = 0 AND key IN",
                ),
            ):
                for s in range(0, len(ids), 500):
                    part = ids[s : s + 500]
                    for rid, blob, tags in db.execute(f"{sql} ({','.join('?' * len(part))})", part):
                        found[str(rid)] = (blob, tags)
        finally:
            db.close()
        return [
            PlaceItem(rid, kind, found[rid][0], found[rid][1]) for rid, kind in rows if rid in found
        ]

    @staticmethod
    def _weight_key(tier: Any, now: datetime) -> tuple:
        return (
            now.date().isoformat(),
            getattr(tier, "_decay_default", None),
            tuple(sorted(getattr(tier, "_decay_by_tag", {}).items())),
        )

    def _refresh_weights(self, tier: Any, now: datetime) -> None:
        from kiro_crew import memory_decay
        from kiro_crew.vector_memory_runtime.episodic_search import recall_weight

        # A V2 store does not fade experiences, so none of them fades here either.
        fades = memory_decay.applies(tier)
        weights: dict[str, float] = {}
        db = _connect_ro(self.db_path)
        db.row_factory = sqlite3.Row
        try:
            cur = db.execute(
                "SELECT id, importance, tags, created_at FROM episodic_memories WHERE is_deleted = 0"
            )
            while rows := cur.fetchmany(_CHUNK):
                for row in rows:
                    weights[str(row["id"])] = recall_weight(tier, row, now) if fades else 1.0
        finally:
            db.close()
        self.sidecar.set_weights(weights)
        self._weights_key = self._weight_key(tier, now)


def node_labels(db_path: Path, nodes: list[dict]) -> dict[str, str]:
    """The full label text per node, read from the store: an experience's text,
    a lesson's rule, a fact's ``key = value``. Unredacted and uncut: the route
    redacts the whole text first and then cuts it to :data:`~kiro_crew.memory_graph_files.LABEL_CHARS`, so a
    secret that straddles the cut is never left half-redacted."""
    import json

    from kiro_crew.vector_memory_runtime.lessons import _lesson_display_text

    epi = [n["id"] for n in nodes if n["kind"] == "episode"]
    sem = [n["id"] for n in nodes if n["kind"] != "episode"]
    out: dict[str, str] = {}
    db = _connect_ro(Path(db_path))
    try:
        for s in range(0, len(epi), 500):
            part = epi[s : s + 500]
            q = f"SELECT id, text FROM episodic_memories WHERE id IN ({','.join('?' * len(part))})"
            for rid, text in db.execute(q, part):
                out[str(rid)] = str(text or "")
        for s in range(0, len(sem), 500):
            part = sem[s : s + 500]
            q = f"SELECT key, value_json FROM semantic_memory WHERE key IN ({','.join('?' * len(part))})"
            for key, raw in db.execute(q, part):
                try:
                    value = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    value = raw
                if str(key).startswith("lesson."):
                    text = _lesson_display_text(value) or str(value)
                else:
                    text = f"{key} = {value if isinstance(value, str) else json.dumps(value)}"
                out[str(key)] = text
    finally:
        db.close()
    return out


_GRAPHS: dict[str, StoreGraph] = {}
_GRAPHS_LOCK = threading.Lock()


def graph_for(db_path: Path) -> StoreGraph:
    """The one :class:`StoreGraph` for the store database at *db_path*."""
    key = os.path.realpath(db_path)
    with _GRAPHS_LOCK:
        graph = _GRAPHS.get(key)
        if graph is None:
            graph = _GRAPHS[key] = StoreGraph(Path(db_path))
        return graph


def forget_graphs() -> None:
    """Drop every cached :class:`StoreGraph`, ending its running build (tests; a
    store's removal)."""
    with _GRAPHS_LOCK:
        graphs = list(_GRAPHS.values())
        _GRAPHS.clear()
    for graph in graphs:
        graph.stop()
