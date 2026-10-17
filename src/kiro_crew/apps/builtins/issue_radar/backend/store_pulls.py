"""The pull-request caches of a connected repo.

The per-state PR list with its lock, snapshot and write-through patches, and
single-PR detail. Callers reach every name here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.store`.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write

from .store_issues import _read_list_snapshot
from .store_repos import repo_data_dir

# ── pull-request caches (mirror the issue list + detail caches) ──────────────
#
# Same cache-first philosophy as issues: the PR list is cached per state
# (open/closed) and each PR's detail (detail + normalized timeline + changed
# files) gets one file, so a PR view opens instantly (and offline) on re-visit.
# Both live under the repo's cache dir, so ``remove_connected_repo``'s rmtree
# cleans them up on disconnect. ``refresh=1`` on the route bypasses either.

# Bump when the shape of a cached PR row changes (i.e. when ``_PR_JQ`` in
# github_client gains/renames/drops a field), so an older-schema cache is a MISS
# on read and the route transparently refetches with the current field set.
#   v1: initial PR list shape
#   v2: added additions / deletions / checks_state (GraphQL list enrichment)
#   v3: added changed_files / checks_counts (per-bucket check tally on the card)
#   v4: checks_counts now collapses same-name runs, so v3 tallies are inflated
#   v5: unavailable enrichment is now null (unknown) instead of 0/empty, and rows
#       carry checks_truncated; v4 rows cannot express either
#   v6: rows carry head_sha. A bulk approve pins each verdict to the commit its row
#       was rendered at, and a v5 row has no such field — served as-is it would
#       silently disable bulk approve for every cached repo until the TTL expired,
#       which reads as a broken button rather than as a stale cache
#   v7: rows carry mergeable_state / mergeable. The bulk bar reads them to tell a PR
#       that is not ready yet (arming auto-merge is meaningful) from one that is ready
#       NOW (GitHub REFUSES to arm, "Pull request is in clean status"). A v6 row has
#       neither field, and an absent value is indistinguishable from "not ready" — so
#       serving one would keep offering the arm that fails, which is the whole defect
#       this version exists to fix
PULLS_CACHE_SCHEMA = 7


def pulls_cache_path(owner: str, repo: str, root: Path | None = None, state: str = "open") -> Path:
    fname = "pulls-cache.json" if state == "open" else f"pulls-{state}-cache.json"
    return repo_data_dir(owner, repo, root) / fname


@contextlib.contextmanager
def _pulls_cache_lock(owner: str, repo: str, root: Path | None, state: str):
    """Serialize writes to ONE pulls list cache across threads and processes.

    ``atomic_write`` prevents a torn file but not a lost update: a detail poll's
    read→patch→write (``apply_pr_checks_to_list_cache``) can overlap a full
    ``/pulls?refresh=1`` write and replace the whole refreshed document with its
    own stale copy. Both writers hold this lock, so the patch always reads what
    the refresh wrote. Same reasoning as :func:`_config_lock`.
    """
    path = pulls_cache_path(owner, repo, root, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".json.lock")
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            yield


def write_pulls_cache(
    owner: str,
    repo: str,
    pulls: list[dict],
    *,
    root: Path | None = None,
    state: str = "open",
    probe: dict | None = None,
) -> None:
    with _pulls_cache_lock(owner, repo, root, state):
        payload: dict = {
            "schema": PULLS_CACHE_SCHEMA,
            "owner": owner,
            "repo": repo,
            "state": state,
            "pulls": pulls,
            # Inside the payload on purpose — see write_issues_cache.
            "fetched_at": time.time(),
        }
        if probe is not None:
            payload["probe"] = probe
        atomic_write(pulls_cache_path(owner, repo, root, state), json.dumps(payload, indent=2))


def read_pulls_snapshot(
    owner: str, repo: str, root: Path | None = None, state: str = "open"
) -> dict | None:
    """``{"rows", "probe", "age_sec"}`` for the cached PRs in one read.
    Mirrors :func:`read_issues_snapshot`, including why it is a single read."""
    return _read_list_snapshot(
        pulls_cache_path(owner, repo, root, state), PULLS_CACHE_SCHEMA, "pulls"
    )


def read_pulls_cache(
    owner: str, repo: str, root: Path | None = None, state: str = "open"
) -> list[dict] | None:
    """Return cached pull requests for the given state, or None when there is no
    current-schema cache (a stale/absent schema stamp is treated as a miss so
    the route refetches with the current PR shape)."""
    path = pulls_cache_path(owner, repo, root, state)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("schema") != PULLS_CACHE_SCHEMA:
        return None
    pulls = data.get("pulls")
    return pulls if isinstance(pulls, list) else None


def apply_pr_checks_to_list_cache(
    owner: str, repo: str, number: int, summary: dict, *, root: Path | None = None
) -> None:
    """Write a PR's fresh check tally back into whichever list cache holds its row.

    Without this the two views drift apart the moment you open a PR: the detail
    pane re-reads the checks every couple of minutes, while the card keeps
    whatever the last LIST refresh computed — so a check that turned red in the
    sidebar stayed green on the card until the whole list was refetched. The
    detail fetch has the authoritative rows in hand, so it patches the row it
    just learned about (same write-through idea as apply_label_change_to_caches).

    ``summary`` is ``github_client.summarize_checks``'s output. Only the two
    check fields are touched; a cache whose schema is stale is left alone, since
    it will be refetched wholesale anyway.
    """
    for state in ("open", "closed"):
        path = pulls_cache_path(owner, repo, root, state)
        if not path.is_file():
            continue
        # The whole read→patch→write runs under the cache's lock so a concurrent
        # full refresh cannot be clobbered by this partial update (or vice versa).
        with _pulls_cache_lock(owner, repo, root, state):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, FileNotFoundError):
                continue
            if not isinstance(data, dict) or data.get("schema") != PULLS_CACHE_SCHEMA:
                continue
            changed = False
            for row in data.get("pulls") or []:
                if isinstance(row, dict) and row.get("number") == int(number):
                    # Also clear a stale truncation flag: this tally comes from the
                    # fully-paginated detail read, so it is complete even if the
                    # GraphQL enrichment had to give up on a >1-page PR.
                    patch = {
                        "checks_counts": summary.get("checks_counts"),
                        "checks_state": summary.get("checks_state"),
                        "checks_truncated": bool(summary.get("checks_truncated")),
                    }
                    # Only a real difference is worth a write. The detail poll
                    # calls this every 30s while a PR pane is open, and this file
                    # is multi-MB on a busy repo — rewriting an identical payload
                    # sixty times an hour is pure churn.
                    if any(row.get(k) != v for k, v in patch.items()):
                        row.update(patch)
                        changed = True
            if changed:
                atomic_write(path, json.dumps(data, indent=2))


def apply_pr_state_change_to_caches(
    owner: str, repo: str, number: int, state: str, *, root: Path | None = None
) -> None:
    """Make the caches agree after a PR was closed or reopened in-app.

    Two things have to happen, and doing only the first is the bug this exists to
    prevent: a closed PR must LEAVE the open list (otherwise the card sits there
    until the next full refresh, and clicking it reopens a pane that disagrees
    with it), and its DETAIL entry must be dropped so the pane's next read is a
    real fetch rather than the pre-change payload.

    The stale row is removed rather than patched in place, and the closed list is
    NOT synthesized: this function knows the PR's new state but not its
    ``merged_at``, ``updated_at`` or check tally, so inventing a row there would
    put a half-populated card in the closed list. Dropping it from the list it
    left is honest — the next fetch of the destination list builds a real row.
    """
    target = int(number)
    # A PR that just closed leaves the OPEN list; one that just reopened leaves
    # the CLOSED list. Only the list it departed is touched.
    leaving = "open" if state == "closed" else "closed"
    path = pulls_cache_path(owner, repo, root, leaving)
    if path.is_file():
        with _pulls_cache_lock(owner, repo, root, leaving):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, FileNotFoundError):
                data = None
            if isinstance(data, dict) and data.get("schema") == PULLS_CACHE_SCHEMA:
                rows = data.get("pulls") or []
                kept = [
                    row
                    for row in rows
                    if not (isinstance(row, dict) and row.get("number") == target)
                ]
                if len(kept) != len(rows):
                    data["pulls"] = kept
                    atomic_write(path, json.dumps(data, indent=2))
    # The detail cache now describes the pre-change PR (state, mergeability, and
    # the auto-merge arming a close silently clears), so it must not be served.
    with contextlib.suppress(OSError):
        pr_detail_cache_path(owner, repo, target, root).unlink(missing_ok=True)


def drop_pr_detail_cache(owner: str, repo: str, number: int, *, root: Path | None = None) -> None:
    """Drop ONE PR's detail cache entry.

    Used after any action that changes what a detail read would return without
    moving the PR between lists — a review, a comment, an auto-merge arm, a
    cancelled CI run. The pane refetches on its next poll and shows the real
    state, rather than serving the up-to-30s-old payload from before the action
    (which is exactly long enough for a user to click a button and watch nothing
    happen).
    """
    with contextlib.suppress(OSError):
        pr_detail_cache_path(owner, repo, int(number), root).unlink(missing_ok=True)


def drop_pulls_cache(
    owner: str, repo: str, state: str = "open", *, root: Path | None = None
) -> None:
    """Delete a PR list cache file.

    Used when a fresh fetch could not be fully enriched: skipping the WRITE alone
    would leave the previous (non-expiring) cache in place, so the very next plain
    request would serve those older rows instead of retrying the enrichment.
    Removing it makes the next read a real fetch.

    Holds the same lock as every other mutation of this file. Without it a
    concurrent write-through could read the old list, have this call unlink it, and
    then atomically write its stale copy back — leaving the cache we meant to
    invalidate in place.
    """
    path = pulls_cache_path(owner, repo, root, state)
    with _pulls_cache_lock(owner, repo, root, state):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def pr_detail_cache_path(owner: str, repo: str, number: int, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / f"pull-{int(number)}.json"


# Bump whenever the shape of a cached PR DETAIL entry changes (a new field on the
# detail JQ, or a new sibling payload like ``checks``). An entry written under an
# older schema — or with no stamp at all — is treated as a MISS on read, so the
# route transparently refetches with the current field set.
#
# Without this, a field added later is silently absent FOREVER on any PR the user
# had already opened: the cache hit short-circuits the fetch and the route serves
# the old payload with the new key defaulting to empty. That is exactly how the
# automated-check results came back empty on already-visited PRs. The issues list
# cache guards the same way (see ISSUES_CACHE_SCHEMA).
#
#   v2: replaced the changed-files payload with ``checks``
#   v3: mergeability is now resolved via a retry (see get_pr_detail), so caches
#       written earlier hold a permanent ``mergeable_state: "unknown"``
#   v4: checks are de-duplicated per (publisher, name) rather than by name alone,
#       so v3 entries can be missing a same-named check from another app
#   v5: the detail carries ``auto_merge``, which the actions bar reads to decide
#       whether it offers "enable auto-merge" or "cancel" — a v4 entry has no such
#       key, and defaulting it to absent would show "enable" on a PR that is
#       already armed
PR_DETAIL_CACHE_SCHEMA = 5

# How long a cached PR detail may be served to a plain (non-``refresh=1``) read.
# Freshness belongs to the cache, not to the caller: this is what lets the detail
# pane simply poll, and keeps the route honest for any other consumer.
PR_DETAIL_CACHE_TTL_SEC = 30.0


def write_pr_detail_cache(
    owner: str,
    repo: str,
    number: int,
    detail: dict,
    timeline: list[dict],
    checks: list[dict],
    *,
    root: Path | None = None,
) -> None:
    """Cache one PR's full detail + normalized timeline + automated-check results.

    One file per PR (``pull-{number}.json``) so a detail view opens instantly on
    re-visit; ``refresh=1`` on the route bypasses it.
    """
    atomic_write(
        pr_detail_cache_path(owner, repo, number, root),
        json.dumps(
            {
                "schema": PR_DETAIL_CACHE_SCHEMA,
                "owner": owner,
                "repo": repo,
                "number": int(number),
                "detail": detail,
                "timeline": timeline,
                "checks": checks,
            },
            indent=2,
        ),
    )


def read_pr_detail_cache(
    owner: str,
    repo: str,
    number: int,
    root: Path | None = None,
    *,
    max_age_sec: float | None = None,
) -> dict | None:
    """Return ``{"detail", "timeline", "checks"}`` for a cached PR, or None when
    there is no CURRENT-schema entry (a stale or unstamped file is a miss, so the
    route refetches — see PR_DETAIL_CACHE_SCHEMA).

    ``max_age_sec`` makes freshness a property of the CACHE rather than of the
    caller: an entry older than that reads as a miss. Without it, correctness
    would depend on every consumer of ``/pull`` knowing to pass ``refresh=1``
    after its first read — a plain GET from any second consumer (an MCP tool,
    another pane) would otherwise be served indefinitely-old data.
    """
    path = pr_detail_cache_path(owner, repo, number, root)
    if not path.is_file():
        return None
    if max_age_sec is not None:
        try:
            if (time.time() - path.stat().st_mtime) > max_age_sec:
                return None
        except OSError:
            return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("schema") != PR_DETAIL_CACHE_SCHEMA:
        return None
    return {
        "detail": data.get("detail"),
        "timeline": data.get("timeline", []),
        "checks": data.get("checks", []),
    }
