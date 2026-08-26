"""Profiler artifacts → ONE frame tree the flame/sunburst views render.

Two native profiler shapes reach this app and neither is renderable as-is:

  - a Python ``cProfile`` dump (``.pstats``) — caller/callee statistics
    (``ncalls`` / ``tottime`` / ``cumtime``). This is a call *graph*, not a tree, so
    it has to be walked into one before it can be drawn;
  - a V8 ``.cpuprofile`` (Chromium DevTools, ``node --cpu-prof``) — a flat
    ``nodes[]`` array that already describes a tree, but whose per-frame time still
    has to be summed out of the ``samples[]`` / ``timeDeltas[]`` stream.

Both normalize to the SAME shape, and that shape is the contract the viewer
depends on — one set of components draws either profile::

    { "unit": "ms", "total": <float>, "kind": "cpu", "scenario": <str>,
      "root": { "name", "module", "self", "total", "calls", "children": [...],
                "hot": <bool> },
      "highlight": [<name>, ...] }

WHY the conversion is written at CAPTURE time (:func:`capture_profile` and the
two ``capture_*`` writers) rather than only on read: the ``hot`` flags are derived
from measured self-times, so writing the tree next to the raw artifact means the UI
serves real data with the highlight already populated. Normalizing lazily on first
read left ``hot`` empty until something forced a normalize, which showed up as a
flame graph with nothing highlighted. The read path (:func:`read_profile`) still
falls back to normalizing a raw artifact, so a capture taken before this existed
renders too — through the same normalizer, so the fallback flags ``hot`` as well.

The ``hot`` flag marks the DOMINANT self-time frames: the single hottest self-time
frame is always flagged, plus any frame holding at least :data:`HOT_SELF_FRACTION`
of the profile total. ``highlight`` carries those frame names so the viewer can
pre-focus the subtree that actually costs something instead of opening on the
scenario entry point.

Profile-agnostic on purpose: ``pstats`` is stdlib and ``.cpuprofile`` is the V8
standard, so nothing here knows which build tool, runtime, or repository produced
the artifact.
"""

from __future__ import annotations

import json
import logging
import pstats
from pathlib import Path
from typing import Any

from . import store

# A profile is addressed by the LEDGER's fingerprint, so the fingerprint-shape rule
# lives with the ledger and is imported rather than restated — two copies of a
# validator drift, and this one guards a path interpolation.
from .ledger_admin import validate_fingerprint

logger = logging.getLogger(__name__)

#: A frame whose self time is >= this fraction of the profile total is "hot" (a
#: dominant self-time frame). The single hottest frame is always hot regardless, so
#: a flat profile still highlights its peak.
HOT_SELF_FRACTION = 0.20


# ── pstats (a Python cProfile dump) → frame tree ──────────────────────────────


def normalize_pstats(path: Path) -> dict[str, Any] | None:
    """Build the normalized frame tree from a ``.pstats`` file.

    Walks caller→callee edges and weights each frame by cumulative time. Stdlib
    only (``pstats``). Returns ``None`` when the file cannot be parsed — the caller
    decides whether that is a 404 or a fall-through, and the raw artifact stays on
    disk either way.
    """
    try:
        st = pstats.Stats(str(Path(path)))
    except Exception:  # noqa: BLE001 - pstats raises several unrelated types
        return None
    # stats: {func: (cc, nc, tt, ct, callers)} where func = (file, line, name)
    stats = st.stats  # type: ignore[attr-defined]
    if not stats:
        return None

    def fname(func: tuple[Any, ...]) -> tuple[str, str]:
        return func[2], Path(func[0]).name

    # Invert the callers index into callees so the graph can be walked downward.
    callees: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    called: set[tuple[Any, ...]] = set()
    for func, (_cc, _nc, _tt, _ct, callers) in stats.items():
        for caller in callers:
            callees.setdefault(caller, []).append(func)
            called.add(func)
    # Roots are the frames nobody in the profile calls. A fully cyclic graph has no
    # such frame, so fall back to one arbitrary entry rather than returning nothing.
    roots = [f for f in stats if f not in called] or list(stats.keys())[:1]

    total = sum(stats[f][3] for f in roots) * 1000.0

    def build(func: tuple[Any, ...], seen: frozenset[tuple[Any, ...]]) -> dict[str, Any]:
        _cc, nc, tt, ct, _callers = stats[func]
        name, module = fname(func)
        node: dict[str, Any] = {
            "name": name,
            "module": module,
            "self": round(tt * 1000.0, 2),
            "total": round(ct * 1000.0, 2),
            "calls": nc,
            "children": [],
            "hot": False,
        }
        for child in callees.get(func, []):
            if child in seen:
                continue  # recursion: a cycle would otherwise never terminate
            node["children"].append(build(child, seen | {child}))
        return node

    # Order roots by cumulative time so the dominant subtree renders first, but keep
    # EVERY independent root — one tree covers the whole profile, not just its hottest
    # entry point. With more than one root they are wrapped under a synthetic aggregate
    # so the single-``root`` schema still holds.
    roots = sorted(roots, key=lambda f: stats[f][3], reverse=True)
    root_nodes = [build(f, frozenset({f})) for f in roots]
    if len(root_nodes) == 1:
        root_node = root_nodes[0]
    else:
        root_node = {
            "name": "(roots)",
            "module": "",
            "self": round(sum(n["self"] for n in root_nodes), 2),
            "total": round(sum(n["total"] for n in root_nodes), 2),
            "calls": sum(n["calls"] for n in root_nodes),
            "children": root_nodes,
            "hot": False,
        }
    tree: dict[str, Any] = {
        "unit": "ms",
        "total": round(total, 2),
        "kind": "cpu",
        "scenario": Path(path).stem,
        "root": root_node,
        "highlight": [],
    }
    _populate_hot(tree)
    return tree


# ── .cpuprofile (a V8 CPU profile) → frame tree ───────────────────────────────


def normalize_cpuprofile(path: Path) -> dict[str, Any] | None:
    """Convert a V8 ``.cpuprofile`` file into the normalized frame tree.

    Self time per node is the sum of the sample intervals attributed to it; total
    time is self plus children. Returns ``None`` when the file is unparseable or
    carries no nodes.
    """
    try:
        prof = json.loads(Path(path).read_text(encoding="utf-8"))
        return _normalize_cpuprofile_obj(prof, scenario=Path(path).stem)
    except Exception:  # noqa: BLE001 - a torn/foreign artifact is not an error here
        return None


def _normalize_cpuprofile_obj(prof: dict[str, Any], *, scenario: str) -> dict[str, Any] | None:
    """Normalize an ALREADY-PARSED ``.cpuprofile`` object.

    Separate from :func:`normalize_cpuprofile` because a capture harness that just
    produced the profile in memory should not have to round-trip it through a file
    to get a tree back.
    """
    raw_nodes = prof.get("nodes") or []
    if not raw_nodes:
        return None
    # Index by id, skipping any node that has none. Upstream keyed on ``n["id"]``
    # directly, which raised a KeyError here on a truncated capture — BEFORE the
    # ``root_id is None`` check below, making that guard unreachable. Skipping instead
    # lets the guard do its job; a well-formed profile is unaffected.
    nodes = {n["id"]: n for n in raw_nodes if isinstance(n, dict) and n.get("id") is not None}
    deltas = prof.get("timeDeltas") or []
    samples = prof.get("samples") or []
    self_us: dict[int, float] = {}
    # V8 convention: ``timeDeltas[i]`` is the interval ENDING at ``samples[i]``, and
    # ``timeDeltas[0]`` is the offset from ``startTime`` to the first sample, which
    # belongs to no sampled frame. So ``samples[i]``'s own time is the interval until
    # the NEXT sample, ``deltas[i + 1]``: the startup offset is discarded and the final
    # sample has no trailing interval. Attributing ``deltas[i]`` instead shifts every
    # frame's cost onto whatever ran before it.
    for i, nid in enumerate(samples):
        self_us[nid] = self_us.get(nid, 0.0) + (deltas[i + 1] if i + 1 < len(deltas) else 0.0)

    def total_us(nid: int, seen: frozenset[int]) -> float:
        n = nodes.get(nid)
        if not n:  # pragma: no cover - callers already filter on ``c in nodes``
            return 0.0
        s = self_us.get(nid, 0.0)
        for c in n.get("children", []):
            if c in nodes and c not in seen:
                s += total_us(c, seen | {c})
        return s

    def build(nid: int, seen: frozenset[int]) -> dict[str, Any]:
        n = nodes[nid]
        cf = n.get("callFrame", {})
        return {
            "name": cf.get("functionName") or "(anonymous)",
            # A frame's ``url`` is a full script URL; the basename is what fits in a
            # flame-graph label. Fall back to the raw url when it has no path part.
            "module": Path(cf.get("url", "")).name or cf.get("url", ""),
            "self": round(self_us.get(nid, 0.0) / 1000.0, 2),
            "total": round(total_us(nid, seen) / 1000.0, 2),
            "calls": n.get("hitCount", 0),
            "children": [
                build(c, seen | {c}) for c in n.get("children", []) if c in nodes and c not in seen
            ],
            "hot": False,
        }

    root_id = raw_nodes[0].get("id")
    if root_id is None:
        return None
    tree: dict[str, Any] = {
        "unit": "ms",
        "total": round(total_us(root_id, frozenset({root_id})) / 1000.0, 2),
        "kind": "cpu",
        "scenario": scenario,
        "root": build(root_id, frozenset({root_id})),
        "highlight": [],
    }
    _populate_hot(tree)
    return tree


# ── hot-frame population ──────────────────────────────────────────────────────


def _populate_hot(tree: dict[str, Any]) -> None:
    """Flag the dominant self-time frames and fill ``tree["highlight"]``.

    Always flags the single hottest self-time frame — so even a flat profile
    highlights its peak — plus any frame independently holding
    :data:`HOT_SELF_FRACTION` of the total. Mutates the tree in place.

    ROOT EXCLUSION, the part that is easy to get wrong: the root frame is the
    scenario entry point and its own self time is usually near zero, but not always.
    When a root's self time merely edges out the real hot leaf, picking "the hottest
    frame overall" pre-focuses the entry point instead of the work — the viewer opens
    on a frame that explains nothing. So the always-hot pick is made among NON-root
    frames whenever any of them has self time; it falls back to all frames only when
    no non-root frame has any, which is the one case where the root genuinely is the
    only frame that spent time. The threshold path is unaffected: a root that
    independently clears the fraction is still co-highlighted next to the real hot
    frame. When no frame has self time at all there is no hot frame — an all-zero
    profile must not flag its root by winning the zero tie.
    """
    frames: list[dict[str, Any]] = []

    def walk(n: dict[str, Any]) -> None:
        frames.append(n)
        for c in n.get("children", []):
            walk(c)

    walk(tree["root"])
    if not frames:  # pragma: no cover - the walk always appends the root itself
        return
    total = float(tree.get("total") or 0.0)
    threshold = total * HOT_SELF_FRACTION if total > 0 else 0.0

    non_root = frames[1:]
    non_root_has_self = any(float(f.get("self") or 0.0) > 0 for f in non_root)
    hot_pool = non_root if non_root_has_self else frames
    hottest = max(hot_pool, key=lambda f: float(f.get("self") or 0.0))
    hottest_self = float(hottest.get("self") or 0.0)
    highlight: list[str] = []
    for f in frames:
        self_t = float(f.get("self") or 0.0)
        is_hot = (f is hottest and hottest_self > 0) or (self_t > 0 and self_t >= threshold)
        f["hot"] = bool(is_hot)
        if is_hot and f.get("name") and f["name"] not in highlight:
            highlight.append(f["name"])
    tree["highlight"] = highlight


# ── capture-time writers ──────────────────────────────────────────────────────


def capture_pstats(
    pstats_path: Path, out_json: Path, *, scenario: str | None = None
) -> dict[str, Any] | None:
    """Normalize a ``.pstats`` artifact and WRITE the frame tree to ``out_json``.

    Called right after the harness dumps the raw profile, so the UI reads a tree
    whose ``hot`` highlight is already populated. Returns the tree, or ``None`` when
    normalization failed — the raw ``.pstats`` is still on disk for the read-time
    fallback, so a failure here degrades the view, it does not lose the capture.

    ``scenario`` overrides the default (the file stem, which is the fingerprint). The
    harness knows the hot path a profile exercised and that name is what the viewer
    labels the tree with; without the override every profile is titled with a hash.
    """
    tree = normalize_pstats(Path(pstats_path))
    if tree is None:
        return None
    if scenario:
        tree["scenario"] = scenario
    store.write_json_atomic(Path(out_json), tree)
    return tree


def capture_cpuprofile(
    cpuprofile: dict[str, Any] | Path, out_json: Path, *, scenario: str | None = None
) -> dict[str, Any] | None:
    """Normalize a V8 ``.cpuprofile`` and WRITE the frame tree to ``out_json``.

    Accepts either a path or the parsed profile object, because a harness running in
    the browser/Node hands the JSON straight over and should not have to write a file
    the normalizer would immediately re-read. Returns ``None`` on failure; the raw
    artifact remains for the read-time fallback.
    """
    if isinstance(cpuprofile, (str, Path)):
        path = Path(cpuprofile)
        tree = normalize_cpuprofile(path)
        if tree is not None and scenario:
            tree["scenario"] = scenario
    else:
        try:
            tree = _normalize_cpuprofile_obj(cpuprofile, scenario=scenario or "profile")
        except Exception:  # noqa: BLE001 - a malformed object must not kill the capture
            return None
    if tree is None:
        return None
    store.write_json_atomic(Path(out_json), tree)
    return tree


def capture_profile(
    fp: str, raw_artifact: Path | dict[str, Any], *, scenario: str | None = None
) -> dict[str, Any] | None:
    """The single capture-time entry point, binding a fingerprint to its tree file.

    Resolves the output to ``profiles/<fp>.json`` (the location :func:`read_profile`
    serves), dispatches on artifact shape, and writes the tree with ``hot`` populated.
    Returns ``None`` when the artifact could not be normalized or is not a profiler
    shape we understand; the raw artifact stays on disk either way.
    """
    try:
        safe_fp = validate_fingerprint(fp)
    except ValueError:
        logger.warning("profiles: refusing to capture under an unsafe fingerprint")
        return None
    out_json = store.profiles_dir() / f"{safe_fp}.json"
    if isinstance(raw_artifact, dict):  # a parsed V8 .cpuprofile object
        return capture_cpuprofile(raw_artifact, out_json, scenario=scenario or safe_fp)
    art = Path(raw_artifact)
    if art.suffix == ".pstats":
        return capture_pstats(art, out_json, scenario=scenario)
    if art.suffix == ".cpuprofile":
        return capture_cpuprofile(art, out_json, scenario=scenario)
    return None


# ── reads (what the profile views call) ───────────────────────────────────────


def read_profile(fp: str) -> dict[str, Any] | None:
    """The normalized frame tree for one fingerprint, or ``None`` when there is none.

    Serves the capture-time ``profiles/<fp>.json`` when present. When only a raw
    artifact exists — an older capture, or one whose normalize failed — it is
    normalized here with the SAME normalizer, so an old profile still renders and the
    fallback also carries ``hot``.

    Every failure returns ``None`` rather than raising: this is a read of one panel,
    and a torn or foreign artifact must degrade that panel instead of failing the
    request that asked for it.
    """
    try:
        safe_fp = validate_fingerprint(fp)
    except ValueError:
        logger.warning("profiles: rejected a read for an unsafe fingerprint")
        return None
    profiles = store.profiles_dir()
    tree = store.read_json(profiles / f"{safe_fp}.json")
    if tree is not None:
        # A hand-edited or truncated tree file can parse as JSON and still not be a
        # tree; serving it would break the viewer downstream of the read.
        return tree if isinstance(tree, dict) else None
    try:
        pstats_path = profiles / f"{safe_fp}.pstats"
        if pstats_path.exists():
            return normalize_pstats(pstats_path)
        cpuprofile = profiles / f"{safe_fp}.cpuprofile"
        if cpuprofile.exists():
            return normalize_cpuprofile(cpuprofile)
    except Exception:  # noqa: BLE001 - a deep/degenerate artifact must not 500 the read
        logger.info("profiles: read-time normalize failed for %s", safe_fp, exc_info=True)
        return None
    return None


def list_profiles() -> list[dict[str, Any]]:
    """Every captured profile, by scenario, with the fingerprint it belongs to.

    Reads each tree only for its ``scenario``/``kind`` labels; a tree that will not
    parse still lists under its fingerprint, because a profile the UI cannot label is
    better than a profile the UI cannot see.
    """
    out: list[dict[str, Any]] = []
    for path in sorted(store.profiles_dir().glob("*.json")):
        fp = path.stem
        tree = store.read_json(path)
        if isinstance(tree, dict):
            out.append(
                {
                    "fp": fp,
                    "scenario": tree.get("scenario", fp),
                    "kind": tree.get("kind", "cpu"),
                }
            )
        else:
            out.append({"fp": fp, "scenario": fp, "kind": "cpu"})
    return out
