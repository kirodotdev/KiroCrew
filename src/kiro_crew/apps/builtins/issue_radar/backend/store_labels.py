"""The cached label set and member roster of a connected repo.

Callers reach every name here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.store`.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write

from .store_repos import repo_data_dir


def labels_cache_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / "labels-cache.json"


# How long a cached label set is served before the route refetches it.
#
# This cache had NO expiry, and unlike the issue/PR lists nothing polls the
# `/labels` query, so the first fetch of a repo was served forever. A label
# created on GitHub afterwards was invisible in the rail, the filter palette and
# every picker until the user happened to press Refresh, which reads as a
# TRUNCATED label list rather than as a stale one: nothing on screen says the set
# is incomplete, and the missing labels are silently unfilterable.
#
# 10 minutes because the cost side is negligible and bounded: labels are ONE
# `per_page=100` request (fully paginated, but a repo with thousands of labels
# does not exist), against the 5,000/hr core budget rather than the 30/min search
# quota the list probes share, so at worst 6 requests an hour per open repo, with
# no probe needed to justify them. The label set is also edited far less often
# than issues change, which is why this is a plain TTL and not a probe-gated poll
# like the lists: a probe here would cost as much as the refetch it guards.
LABELS_CACHE_TTL_SEC = 600.0


@contextlib.contextmanager
def labels_cache_lock(owner: str, repo: str, root: Path | None = None):
    """Serialize writers of ONE repo's label cache across threads and processes.

    Two writers touch it: a full refresh, and ``add_label_to_cache``'s
    read-modify-write after creating a label. Client-side serialization cannot
    help here — separate tabs are separate processes — so a newly created label
    could be dropped and stay invisible until a manual refresh."""
    path = labels_cache_path(owner, repo, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".json.lock")
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            yield


def write_labels_cache(
    owner: str, repo: str, labels: list[dict], *, root: Path | None = None
) -> None:
    with labels_cache_lock(owner, repo, root):
        _write_labels_cache_unlocked(owner, repo, labels, root=root)


def _write_labels_cache_unlocked(
    owner: str,
    repo: str,
    labels: list[dict],
    *,
    root: Path | None = None,
    fetched_at: float | None = None,
) -> None:
    """Write the label cache, stamping when the rows were FETCHED.

    ``fetched_at`` carries the previous stamp through for a write-through patch
    (``add_label_to_cache``) that appends a locally-created label without
    refetching. Re-stamping there would reset the age on every label create, so
    a repo whose labels are edited regularly would never age its cache out,
    exactly the staleness :data:`LABELS_CACHE_TTL_SEC` exists to bound. Same
    reasoning as ``_list_cache_age_sec``'s payload-vs-mtime note.
    """
    atomic_write(
        labels_cache_path(owner, repo, root),
        json.dumps(
            {
                "owner": owner,
                "repo": repo,
                "labels": labels,
                "fetched_at": time.time() if fetched_at is None else fetched_at,
            },
            indent=2,
        ),
    )


def refresh_labels_cache(
    owner: str, repo: str, fetch: Callable[[], list[dict]], *, root: Path | None = None
) -> list[dict]:
    """Fetch the repo's labels and store them, holding the cache lock ACROSS BOTH.

    Locking only the write leaves a window that loses data: the fetch returns a
    list from before a label was created, ``add_label_to_cache`` appends it, and
    this write replaces the cache with the pre-create snapshot — so a label that
    exists on GitHub is invisible in every picker until the next refresh.
    ``fetch`` is called at most once."""
    with labels_cache_lock(owner, repo, root):
        labels = fetch()
        _write_labels_cache_unlocked(owner, repo, labels, root=root)
        return labels


def read_labels_cache(
    owner: str,
    repo: str,
    root: Path | None = None,
    *,
    max_age_sec: float | None = LABELS_CACHE_TTL_SEC,
) -> list[dict] | None:
    """Return cached repo labels, or None if absent, malformed, or too OLD.

    Freshness is a property of the CACHE, not of the caller (same rule as
    ``read_pr_detail_cache``), so the TTL is the default rather than something
    every route has to remember to pass. ``max_age_sec=None`` opts out, which is
    what ``add_label_to_cache``'s read-modify-write needs: it is patching whatever
    is on disk, and treating an expired file as absent there would silently drop
    the append.
    """
    path = labels_cache_path(owner, repo, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if max_age_sec is not None and _labels_cache_age_sec(data, path) > max_age_sec:
        return None
    labels = data.get("labels")
    # A non-list (older/corrupt shape) is treated as a miss so the route
    # refetches with the current shape rather than serving a non-array.
    return labels if isinstance(labels, list) else None


def _labels_cache_age_sec(data: dict, path: Path) -> float:
    """How long ago the cached labels were fetched from the provider.

    From the payload's ``fetched_at`` rather than the file's mtime, because
    ``add_label_to_cache`` rewrites the file without refetching; see
    ``_write_labels_cache_unlocked``. A cache written before the field existed has
    no stamp; it falls back to mtime, which is the correct reading for it (that
    file was only ever written by a real fetch).
    """
    fetched_at = data.get("fetched_at")
    if isinstance(fetched_at, (int, float)) and not isinstance(fetched_at, bool):
        return max(0.0, time.time() - float(fetched_at))
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return 0.0


def members_cache_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / "members-cache.json"


def write_members_cache(
    owner: str, repo: str, members: list[dict], *, source: str, root: Path | None = None
) -> None:
    """Cache the repo's member roster (``[{login, role}]``) plus its ``source``
    (``"collaborators"`` for the authoritative roster, ``"derived"`` for the
    read-only fallback inferred from issue authors).

    Repo-level metadata (like the labels cache): the detail badge and the
    "created by member" filter read it instantly instead of waiting on a live
    fetch.
    """
    atomic_write(
        members_cache_path(owner, repo, root),
        json.dumps({"owner": owner, "repo": repo, "source": source, "members": members}, indent=2),
    )


def read_members_cache(owner: str, repo: str, root: Path | None = None) -> dict | None:
    """Return ``{"members": [...], "source": str|None}`` for the cached roster,
    or None if no cache exists yet."""
    path = members_cache_path(owner, repo, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # Coerce ``members`` to a list: the members cache carries no schema stamp
    # (unlike issues), so a file written by an older build with a different
    # shape must never surface a non-array — the frontend would then crash on
    # ``.map`` behind its error boundary. A non-list degrades to empty here.
    members = data.get("members")
    return {"members": members if isinstance(members, list) else [], "source": data.get("source")}


def add_label_to_cache(owner: str, repo: str, label: dict, *, root: Path | None = None) -> None:
    """Append a newly-created label to the labels cache so the pickers show it
    immediately. No-op when the cache doesn't exist yet (a later refresh fetches
    the full set) or the label is already present."""
    # Read and write under ONE lock: a concurrent refresh (or another tab's
    # create) would otherwise land between them and drop this label, leaving it
    # invisible until someone hits refresh.
    with labels_cache_lock(owner, repo, root):
        # `max_age_sec=None`: this patches whatever is on disk. Honouring the TTL here
        # would read an expired file as absent and silently drop the append, and the
        # freshly created label would then be missing from every picker until the next
        # refetch, the very staleness this function exists to avoid.
        labels = read_labels_cache(owner, repo, root, max_age_sec=None)
        if labels is None:
            return
        if any(isinstance(lab, dict) and lab.get("name") == label.get("name") for lab in labels):
            return
        labels.append(
            {
                "name": label.get("name"),
                "color": label.get("color") or "888888",
                "description": label.get("description") or "",
            }
        )
        # Carries the ORIGINAL fetch stamp through, so appending a label does not
        # reset the age and defer the refetch that picks up everyone else's labels.
        _write_labels_cache_unlocked(
            owner,
            repo,
            labels,
            root=root,
            fetched_at=_read_labels_fetched_at(owner, repo, root),
        )


def _read_labels_fetched_at(owner: str, repo: str, root: Path | None = None) -> float | None:
    """When the label cache's rows were fetched, for a write-through patch to carry over.

    Falls back to the file's MTIME when the payload carries no ``fetched_at``, and only
    returns None when neither is readable. A pre-stamp file was written solely by a real
    fetch, so its mtime IS its fetch time; returning None there would mean "stamp it now",
    and since the append is about to replace the file that silently resets the TTL clock.
    A cache nine minutes into its ten-minute life would then get a fresh ten, and one label
    creation per interval could defer the refetch indefinitely, which is the staleness
    :data:`LABELS_CACHE_TTL_SEC` exists to bound.

    Read INSIDE the caller's cache lock, so the value cannot be from a different write than
    the one being patched.
    """
    path = labels_cache_path(owner, repo, root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fetched_at = data.get("fetched_at") if isinstance(data, dict) else None
    if isinstance(fetched_at, (int, float)) and not isinstance(fetched_at, bool):
        return float(fetched_at)
    try:
        return path.stat().st_mtime
    except OSError:
        return None
