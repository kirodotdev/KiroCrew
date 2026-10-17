"""The per-repo dependency-edge cache: the blocked-by / blocking graph.

Callers reach every name here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.store`.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write

from .store_repos import logger, repo_data_dir

# ── dependency-edge cache (blocked-by / blocking graph) ──────────────────────
#
# One document per repo holding the dependency DAG for its OPEN work items: the
# GitHub-native blocked-by edges plus the ones we infer from timeline
# cross-references. Schema-versioned exactly like ISSUES_CACHE_SCHEMA — a stale
# or missing stamp reads as a MISS (returns None) so the /deps route refetches
# with the current edge shape rather than serving a graph missing a field.
#
#   v1: {schema, fetched_at, edges:[{blocked, blocker, source}], nodes:{...}}
DEPS_CACHE_SCHEMA = 1

# How long a synced dependency graph is served before it is considered stale.
#
# This constant has TWO consumers with DIFFERENT needs, which is why it stays at
# ten minutes even though the /deps route alone would be happy with hours:
#
#   * the /deps route, which does not block a request on an expired cache (it
#     serves the stale graph and revalidates behind, refreshing in the
#     background), so for the route this TTL governs how often a BACKGROUND
#     rebuild fires and a long value would be harmless;
#   * crew_runtime._read_or_refresh_deps, the sweep that feeds SIG_DEP_UNBLOCKED.
#     For the sweep this TTL IS the freshness horizon on which a crew waiting for
#     its blocker to merge gets woken, so raising it directly delays that wake.
#
# Serve-stale already removes the ~11s stall a user could hit here, so there is
# nothing left to buy by stretching it -- and the cost would land on the sweep,
# not on the route that wanted it. Decoupling the two horizons (a long refresh
# interval for the route, a short one for the sweep) is a separate change.
DEPS_CACHE_TTL_SEC = 600.0

# Edge provenance. A native edge is one GitHub itself records via the
# dependencies API; an inferred edge is backfilled from a timeline
# cross-reference so the graph is useful in repos where nobody sets native
# dependencies. A native edge WINS over an inferred duplicate (see
# _normalize_deps).
DEP_SOURCE_NATIVE = "native"
DEP_SOURCE_INFERRED = "inferred"
_DEP_SOURCES = (DEP_SOURCE_NATIVE, DEP_SOURCE_INFERRED)


def deps_cache_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / "deps-cache.json"


@contextlib.contextmanager
def _deps_cache_lock(owner: str, repo: str, root: Path | None = None):
    """Serialize writers of ONE repo's deps cache across threads AND processes.

    Two independent producers rebuild this graph — the ``/deps`` route and the
    crew sweep's ``_read_or_refresh_deps``. The sweep's write runs on a worker
    thread (``asyncio.to_thread``) and never takes the route's per-repo asyncio
    mutex, so that mutex cannot order the two. ``atomic_write`` prevents a
    torn file but not a lost update: the compare-and-set in
    :func:`write_deps_cache` must read the stored stamp and write under one lock,
    or two writers could both read the pre-existing stamp, both decide they are
    newer, and the LOSER would still land last. Same discipline as
    :func:`issues_cache_lock` / :func:`labels_cache_lock` / ``_tagging_cache_lock``.
    """
    path = deps_cache_path(owner, repo, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".json.lock")
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            yield


def _normalize_deps(edges: Any, nodes: Any) -> tuple[list[dict], dict[str, dict]]:
    """Coerce a raw deps payload to ``(edges, nodes)`` in the stored shape.

    Deliberately tolerant: a partially-written or hand-edited document should
    degrade to a smaller graph, never break the /deps route. Edges are
    deduplicated on the ``(blocked, blocker)`` pair with a NATIVE edge winning
    over an inferred duplicate (a self-edge, or one with a non-positive number,
    is dropped). ``nodes`` keeps only well-formed entries keyed by the number as
    a string, matching the on-the-wire JSON object-key type.
    """
    by_pair: dict[tuple[int, int], dict] = {}
    if isinstance(edges, list):
        for raw in edges:
            if not isinstance(raw, dict):
                continue
            raw_blocked = raw.get("blocked")
            raw_blocker = raw.get("blocker")
            if raw_blocked is None or raw_blocker is None:
                continue
            try:
                blocked = int(raw_blocked)
                blocker = int(raw_blocker)
            except (TypeError, ValueError):
                continue
            if blocked <= 0 or blocker <= 0 or blocked == blocker:
                continue
            source = raw.get("source")
            if source not in _DEP_SOURCES:
                source = DEP_SOURCE_INFERRED
            pair = (blocked, blocker)
            existing = by_pair.get(pair)
            # Native wins: only overwrite an existing entry when the newcomer is
            # native and the incumbent is not, so a native edge is never demoted
            # to inferred by a later duplicate.
            if existing is None or (
                source == DEP_SOURCE_NATIVE and existing.get("source") != DEP_SOURCE_NATIVE
            ):
                by_pair[pair] = {"blocked": blocked, "blocker": blocker, "source": source}
    out_edges = [by_pair[p] for p in sorted(by_pair)]

    out_nodes: dict[str, dict] = {}
    if isinstance(nodes, dict):
        for key, val in nodes.items():
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
            if number <= 0 or not isinstance(val, dict):
                continue
            out_nodes[str(number)] = {
                "kind": str(val.get("kind") or "issue"),
                "state": str(val.get("state") or "open"),
                "title": str(val.get("title") or ""),
            }
    return out_edges, out_nodes


def read_deps_cache(owner: str, repo: str, root: Path | None = None) -> dict | None:
    """Return ``{"edges", "nodes", "fetched_at"}`` for a repo, or None when the
    graph has never been synced.

    An unreadable file, or one written under an older ``DEPS_CACHE_SCHEMA`` (or
    with no stamp at all), is treated as a MISS — same guard as every other
    cache — so the route refetches with the current edge shape.
    """
    path = deps_cache_path(owner, repo, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != DEPS_CACHE_SCHEMA:
        return None  # stale schema → treat as a miss so the route refetches
    edges, nodes = _normalize_deps(data.get("edges"), data.get("nodes"))
    # 0.0 (epoch) on an undeterminable stamp, so the entry reads as maximally
    # stale and the TTL refresh repairs it — see _coerce_stamp for why
    # coercion must not raise.
    stamp = _coerce_stamp(data.get("fetched_at")) or 0.0
    now = time.time()
    if stamp > now + _DEPS_STAMP_FUTURE_SLACK_SEC:
        # A stored stamp beyond the future slack can only come from a clock
        # retreat (write_deps_cache persists the RAW stamp — its compare-and-set
        # needs the stamp's epoch information, see there). Clamping it to "now"
        # here would pin the age at ~0 on EVERY read for the whole retreat
        # window, so the automatic TTL refresh would never fire until wall time
        # passed the stamp. Return 0.0 (maximally stale) instead: the refresh
        # fires immediately and its current-epoch write lands through the CAS's
        # stored-future fail-open branch, repairing the stamp — self-healing
        # rather than self-sustaining.
        stamp = 0.0
    return {
        "edges": edges,
        "nodes": nodes,
        # min(): sub-slack skew is normal clock granularity, not a retreat —
        # the clamp keeps the returned age from reading negative.
        "fetched_at": min(stamp, now),
    }


# A stored fetched_at more than this far ahead of the current wall clock cannot
# come from a healthy writer: stamps are always captured BEFORE the write they
# ride in on, so on one machine a stored stamp is at most a clock tick ahead of
# "now". Beyond this slack the only real-world cause is a backwards clock
# adjustment (NTP step, VM suspend/resume), which makes comparisons against the
# stored stamp unordered — see write_deps_cache.
_DEPS_STAMP_FUTURE_SLACK_SEC = 5.0


def _read_deps_fetched_at_raw(owner: str, repo: str, root: Path | None = None) -> float | None:
    """The stored stamp for :func:`write_deps_cache`'s compare-and-set, read from
    the RAW payload.

    Deliberately NOT :func:`read_deps_cache`: that returns None for an unreadable
    file AND for one written under an older ``DEPS_CACHE_SCHEMA``, and coerces a
    missing stamp to ``0.0`` after normalizing the whole graph. For the CAS,
    "cannot determine an existing stamp" must fail OPEN (return None, so the
    write proceeds): a schema-stale or corrupt file has to be replaceable,
    otherwise a schema bump would wedge the cache permanently — the old file's
    stamp would refuse every new-schema write while the reader keeps missing.
    """
    path = deps_cache_path(owner, repo, root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != DEPS_CACHE_SCHEMA:
        return None
    return _coerce_stamp(data.get("fetched_at"))


def _coerce_stamp(value: Any) -> float | None:
    """Coerce a stored ``fetched_at`` payload value to a float stamp, or None
    when it is not a usable number.

    ``float()`` on a valid-JSON but absurd integer (hundreds of digits) raises
    ``OverflowError``; that is corrupt data, not a stamp, and it must read as
    "undeterminable" — raising would escape as a 500 out of every consumer
    (the route's read as well as the CAS) and leave the corruption in place.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value)
    except OverflowError:
        return None


def write_deps_cache(
    owner: str,
    repo: str,
    edges: list[dict],
    nodes: dict[str, dict],
    *,
    root: Path | None = None,
    fetched_at: float,
) -> None:
    """Store the dependency graph for a repo, stamping the current schema and the
    fetch time. Edges/nodes are normalized on the way in (native-wins dedup,
    self-edges dropped) so a caller cannot persist a malformed graph.

    ``fetched_at`` is when the graph was READ from the forge — callers capture
    ``time.time()`` before the work that produces their graph and pass it here
    (the convention ``_write_labels_cache_unlocked`` uses). It is required, not
    defaulted: stamping at write time re-dates the data — a slow rebuild that
    started earlier but finishes later would land its older graph on top of a
    newer one and mark it fresh, deferring the refetch a full
    ``DEPS_CACHE_TTL_SEC`` and blinding the sweep's unblock-transition signal.

    The write is a compare-and-set under :func:`_deps_cache_lock`: it is SKIPPED
    (silently — a skipped write is a normal outcome, not an error) when the
    stored stamp is ``>=`` the incoming one. ``>=`` rather than ``>``: an equal
    stamp means the stored graph is at least as new, and two writes in the same
    clock tick must not flip the graph back and forth. Both callers re-read the
    cache after writing, so the caller that lost serves the newer stored graph
    rather than the one it just built. Stamps are wall-clock (``time.time()``),
    matching every other cache in this module — they must be comparable to
    ``time.time()`` in the TTL checks, so a monotonic clock is not a drop-in.
    A backwards clock adjustment is handled epoch-aware: raw stamps from the
    SAME epoch (both normal, or both from the pre-retreat future) stay mutually
    comparable and the CAS applies to them unchanged; only when the stored
    stamp is more than ``_DEPS_STAMP_FUTURE_SLACK_SEC`` ahead of the wall clock
    while the incoming one is current-epoch does the write fail open — the
    incoming rebuild ran after the retreat and is newer in real time, and
    honouring the raw comparison would wedge every write (including a
    user-forced ``?refresh=1``) until wall time passed the stored stamp. The
    persisted stamp is the RAW capture — it is the CAS's ordering token, and
    clamping it to "now" at persistence would erase its epoch, letting a
    slower pre-retreat rebuild compare greater than the clamped newer write
    and overwrite it. :func:`read_deps_cache` repairs the value it RETURNS
    instead: a beyond-slack stored stamp reads as 0.0 (maximally stale, so
    the TTL refresh fires and its current-epoch write self-heals the stamp
    through the fail-open branch above), and sub-slack skew is clamped to
    "now" so the age never reads negative. The mirror quadrant — an incoming
    write whose stamp is ahead of the wall clock AT ALL against a current-epoch
    stored stamp — is ambiguous: the stored write may have run after the
    retreat (newer in real time) or may be a genuinely old cache whose expiry
    triggered the incoming rebuild, and the stamps cannot distinguish the two.
    The slack is a tolerance for a STORED stamp, not for an incoming one:
    applying it to the incoming side classified a retreat SMALLER than the
    slack as current-epoch, so the raw comparison let the older pre-retreat
    graph win and read fresh for a whole TTL. The write
    is therefore PERSISTED WITH A MAXIMALLY STALE STAMP (0.0): no completed
    refresh is ever discarded, and the stale stamp makes the very next TTL
    check refetch with a current-epoch stamp, bounding the harm in either
    reading to a single freshness-check interval. Every quadrant
    of the epoch matrix is thus decided by real-time order — or, where the
    stamps cannot decide it, forced to self-heal on the next check — and the
    residual harm in any retreat race is bounded by one TTL.
    """
    norm_edges, norm_nodes = _normalize_deps(edges, nodes)
    stamp = float(fetched_at)
    with _deps_cache_lock(owner, repo, root):
        now = time.time()
        existing = _read_deps_fetched_at_raw(owner, repo, root)
        if existing is not None:
            # Epoch-aware comparison, on RAW stamps. Clamping or discarding
            # before the comparison would erase the epoch information: an older
            # pre-retreat rebuild (clamped to "now") could then overwrite a
            # newer pre-retreat one (discarded as unordered), resurrecting the
            # very defect this CAS exists to stop.
            stored_is_future = existing > now + _DEPS_STAMP_FUTURE_SLACK_SEC
            # The slack is a tolerance for the STORED stamp ONLY. Extending it to
            # the incoming side classified a stamp up to the slack ahead of the
            # clock as current-epoch, which broke BOTH branches below: it let a
            # sub-slack retreat skip the ambiguous branch, and — worse — it let
            # an OLDER pre-retreat write pass the fail-open branch against a
            # NEWER pre-retreat one (retreat beyond the slack, the two captures
            # less than the slack apart, so the older stamp lands under
            # ``now + slack`` while the newer one lands above it). ``stamp`` is
            # captured before ``now`` in this very function, so an honest write
            # always satisfies ``stamp <= now`` and needs no tolerance here.
            incoming_is_current_epoch = stamp <= now
            if stored_is_future and incoming_is_current_epoch:
                # The stored stamp predates a clock retreat; the incoming one is
                # from the current epoch, so its rebuild ran AFTER the retreat —
                # newer in real time. Honouring the raw comparison here would
                # wedge every post-retreat write (including a user-forced
                # ?refresh=1) until wall time passed the stored stamp. Fail open.
                logger.debug(
                    "issue-radar: deps-cache stamp for %s/%s is pre-retreat future (%s); "
                    "accepting the current-epoch write",
                    owner,
                    repo,
                    existing,
                )
            elif not stored_is_future and not incoming_is_current_epoch:
                # Last quadrant of the epoch matrix, and the one the stamps
                # genuinely cannot decide: the INCOMING stamp is ahead of the
                # wall clock while the stored one is current-epoch.
                # Two real-time orders produce exactly this signature. (a) The
                # stored write ran AFTER the retreat — it is newer, and landing
                # the older pre-retreat graph on top of it re-dated fresh is
                # the lost-update defect inside the retreat window. (b) The
                # stored cache is
                # genuinely OLD — its expiry is what triggered this very
                # rebuild, the rebuild started pre-retreat and finished after
                # it, and the retreat has shrunk the old cache's apparent age
                # back under the TTL; rejecting here discards a completed
                # refresh and serves the stale graph until the age re-passes
                # the TTL.
                # Neither outright accept (unbounded harm in (a)) nor reject
                # (retreat-magnitude harm in (b)) is safe, so do neither:
                # persist the incoming graph with a MAXIMALLY STALE stamp.
                # No data is ever discarded, and the 0.0 stamp makes the very
                # next TTL check refetch with a current-epoch stamp that
                # lands by the normal comparison — bounding the harm in both
                # cases to a single freshness-check interval.
                logger.debug(
                    "issue-radar: deps-cache write for %s/%s carries a stamp ahead of the wall "
                    "clock (%s) against a current-epoch stored stamp (%s) — persisting the "
                    "graph maximally stale so the next TTL check self-heals",
                    owner,
                    repo,
                    stamp,
                    existing,
                )
                stamp = 0.0
            elif existing >= stamp:
                # Same epoch — both normal, or both from the pre-retreat future —
                # so the raw stamps are mutually comparable: the stored graph is
                # at least as new. Skip on >=, not >: two writes in the same
                # clock tick must not flip the graph back and forth. A skipped
                # write is a normal outcome; both callers re-read the cache and
                # serve the newer stored graph.
                logger.debug(
                    "issue-radar: skipping deps-cache write for %s/%s — stored graph is newer "
                    "(stored fetched_at=%s >= incoming %s)",
                    owner,
                    repo,
                    existing,
                    stamp,
                )
                return
        atomic_write(
            deps_cache_path(owner, repo, root),
            json.dumps(
                {
                    "schema": DEPS_CACHE_SCHEMA,
                    "owner": owner,
                    "repo": repo,
                    # The RAW stamp, deliberately unclamped: this field is the
                    # compare-and-set's ordering token, and clamping a
                    # pre-retreat stamp to "now" at persistence would erase its
                    # epoch — a slower pre-retreat rebuild would then compare
                    # greater than the clamped newer write and overwrite it —
                    # the lost update inside the retreat window. One
                    # exception: the ambiguous quadrant above rewrites the
                    # stamp to 0.0 (maximally stale) — the opposite direction
                    # from a clamp, which no straggler can compare greater
                    # than fresh. Readers
                    # never see this stamp raw: read_deps_cache repairs the
                    # value it RETURNS (beyond-slack → 0.0 so the TTL refresh
                    # self-heals it; sub-slack clamped to "now").
                    "fetched_at": stamp,
                    "edges": norm_edges,
                    "nodes": norm_nodes,
                },
                indent=2,
            ),
        )
