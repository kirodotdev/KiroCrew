"""The issue caches of a connected repo.

The per-state issue list with its locks, snapshot and age, single-issue detail,
reference summaries, and the write-through patches a triage write applies to them.
Callers reach every name here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.store`.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, read_json_or

from .store_ai import delete_issue_ai_cache
from .store_repos import logger, repo_data_dir


def issues_cache_path(owner: str, repo: str, root: Path | None = None, state: str = "open") -> Path:
    fname = "issues-cache.json" if state == "open" else f"issues-{state}-cache.json"
    return repo_data_dir(owner, repo, root) / fname


# Bump this whenever the shape of a cached issue changes — i.e. when
# ``_ISSUE_JQ`` in github_client gains/renames/drops a field. A cache written
# under an older schema is treated as a MISS on read (returns None) so the route
# transparently refetches with the current field set. Without this, a cache
# written before a field existed silently keeps missing it forever: that is
# exactly how ``author_association`` (which powers the derived member set) and
# ``reactions``/``thumbs_up`` went missing on repos cached by an earlier build,
# leaving Settings → Members empty even though the live detail pane — which
# fetches association per-issue — still showed member badges.
#
#   v2: added author_association, reactions, thumbs_up to _ISSUE_JQ
ISSUES_CACHE_SCHEMA = 2


@contextlib.contextmanager
def issues_cache_lock(owner: str, repo: str, root: Path | None = None, state: str = "open"):
    """Serialize writers of ONE issues list cache across threads AND processes.

    Two different writers touch this file: a full refresh (``write_issues_cache``)
    and the post-write patches that keep it coherent after a label or state change
    (``apply_label_change_to_caches`` / ``apply_state_change_to_caches``, which
    read-modify-write it). ``atomic_write`` prevents a torn file but not a lost
    update, so a patch that read before a refresh wrote would replace the whole
    refreshed document with its own stale copy — and with two dashboard tabs or a
    second API client that is a routine interleaving, not a rare race. Same
    discipline as :func:`_config_lock` and :func:`_pulls_cache_lock`.

    Public because the routes layer holds it across a read-then-write pair.
    """
    path = issues_cache_path(owner, repo, root, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".json.lock")
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            yield


@contextlib.contextmanager
def issue_write_lock(owner: str, repo: str, number: int, root: Path | None = None):
    """Serialize ONE issue's GitHub mutation together with its cache patch.

    Two concurrent applies to the same issue each get an authoritative label set
    back from GitHub, but nothing orders the two cache patches — so the SECOND
    response can be written before the first, leaving the cache showing a label
    set that is missing whatever the later mutation added. It self-heals on the
    next refresh, which is exactly why it is easy to miss.

    Per-issue, so applies to different issues still run concurrently. Held across
    the network call, which is the point: ordering the writes is what matters."""
    path = repo_data_dir(owner, repo, root) / f"issue-{int(number)}.write.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with open(path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            yield


def write_issues_cache(
    owner: str,
    repo: str,
    issues: list[dict],
    *,
    root: Path | None = None,
    state: str = "open",
    probe: dict | None = None,
) -> None:
    with issues_cache_lock(owner, repo, root, state):
        _write_issues_cache_unlocked(owner, repo, issues, root=root, state=state, probe=probe)


def _write_issues_cache_unlocked(
    owner: str,
    repo: str,
    issues: list[dict],
    *,
    root: Path | None = None,
    state: str = "open",
    probe: dict | None = None,
) -> None:
    payload: dict = {
        "schema": ISSUES_CACHE_SCHEMA,
        "owner": owner,
        "repo": repo,
        "state": state,
        "issues": issues,
        # See _read_list_snapshot: the age that bounds a poll's staleness has to
        # live INSIDE the payload, because partial write-through patches rewrite
        # the file and would otherwise reset its mtime.
        "fetched_at": time.time(),
    }
    if probe is not None:
        payload["probe"] = probe
    atomic_write(issues_cache_path(owner, repo, root, state), json.dumps(payload, indent=2))


def refresh_issues_cache(
    owner: str,
    repo: str,
    fetch: Callable[[], list[dict]],
    *,
    root: Path | None = None,
    state: str = "open",
    probe: dict | None = None,
) -> list[dict]:
    """Fetch a fresh issue list and store it, holding the cache lock ACROSS BOTH.

    Locking only the write leaves a window that loses data rather than merely
    ordering it: fetch returns a list from before a label was applied, an apply
    then patches the cache, and the refresh's write replaces that patch with the
    pre-write snapshot — so a label the user just applied silently disappears from
    the dashboard until the next refresh.

    The cost is that a concurrent patch waits for the network call. That is the
    right trade: a refresh is a deliberate user action on one repo, and the
    alternative is losing writes. ``fetch`` is called at most once.

    ``probe`` is the poll fingerprint read BEFORE the fetch (see
    ``_poll_can_serve_cache``); it is persisted in the same write so a poll can
    never pair fresh rows with a missing fingerprint.
    """
    with issues_cache_lock(owner, repo, root, state):
        issues = fetch()
        _write_issues_cache_unlocked(owner, repo, issues, root=root, state=state, probe=probe)
        return issues


def read_issues_snapshot(
    owner: str, repo: str, root: Path | None = None, state: str = "open"
) -> dict | None:
    """One read of the issues cache returning ``{"rows", "probe", "age_sec"}``.

    Rows and probe come from the SAME read on purpose. Reading them separately
    let a concurrent refresh land in between, pairing the old rows with the new
    probe — which the poll path would then treat as "verified unchanged" and
    serve as fresh. Returns None on a miss or a stale schema, exactly like
    :func:`read_issues_cache`.
    """
    return _read_list_snapshot(
        issues_cache_path(owner, repo, root, state), ISSUES_CACHE_SCHEMA, "issues"
    )


def _read_list_snapshot(path: Path, schema: object, rows_key: str) -> dict | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    data = read_json_or(path, None, logger=logger, what="issue-radar list cache")
    if not isinstance(data, dict) or data.get("schema") != schema:
        return None  # stale schema → treat as a miss so the route refetches
    rows = data.get(rows_key)
    if not isinstance(rows, list):
        return None
    probe = data.get("probe")
    return {
        "rows": rows,
        "probe": probe if isinstance(probe, dict) else None,
        "age_sec": _list_cache_age_sec(data, mtime),
    }


def _list_cache_age_sec(data: dict, mtime: float) -> float:
    """How long ago the cached ROWS were fetched from GitHub.

    Read from the payload's ``fetched_at``, NOT from the file's mtime: the
    write-through patches (``apply_pr_checks_to_list_cache``,
    ``apply_label_change_to_caches``) rewrite this file in place without
    refetching anything. With mtime, opening a single PR — whose detail poll
    patches its check tally back every 30s — kept resetting the age, so the
    poll staleness ceiling never fired for the open-PR list precisely while a
    PR pane was open, i.e. it was unreachable in the degenerate-probe case it
    exists to bound.

    Falls back to mtime for a cache written before this field existed, which is
    at worst the old (too-young) reading for one refresh cycle.
    """
    fetched_at = data.get("fetched_at")
    if isinstance(fetched_at, (int, float)) and not isinstance(fetched_at, bool):
        return max(0.0, time.time() - float(fetched_at))
    return max(0.0, time.time() - mtime)


def read_issues_cache(
    owner: str, repo: str, root: Path | None = None, state: str = "open"
) -> list[dict] | None:
    """Return cached issues for the given state, or None if there is no
    current-schema cache.

    A cache written under an older ``ISSUES_CACHE_SCHEMA`` (or one with no schema
    stamp at all, i.e. pre-versioning) is ignored — returns None — so the caller
    refetches with the current issue shape rather than serving data that is
    missing newer fields (``author_association`` etc.).
    """
    path = issues_cache_path(owner, repo, root, state)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("schema") != ISSUES_CACHE_SCHEMA:
        return None  # stale schema → treat as a miss so the route refetches
    issues = data.get("issues")
    return issues if isinstance(issues, list) else None


def issue_detail_cache_path(owner: str, repo: str, number: int, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / f"issue-{int(number)}.json"


def write_issue_detail_cache(
    owner: str,
    repo: str,
    number: int,
    detail: dict,
    timeline: list[dict],
    *,
    root: Path | None = None,
) -> None:
    """Cache one issue's full detail + normalized timeline.

    One file per issue (``issue-{number}.json``) so a detail view opens
    instantly (and offline) on re-visit; ``refresh=1`` on the route bypasses it.
    """
    atomic_write(
        issue_detail_cache_path(owner, repo, number, root),
        json.dumps(
            {
                "owner": owner,
                "repo": repo,
                "number": int(number),
                "detail": detail,
                "timeline": timeline,
            },
            indent=2,
        ),
    )


def read_issue_detail_cache(
    owner: str, repo: str, number: int, root: Path | None = None
) -> dict | None:
    """Return ``{"detail", "timeline"}`` for a cached issue, or None if absent."""
    path = issue_detail_cache_path(owner, repo, number, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {"detail": data.get("detail"), "timeline": data.get("timeline", [])}


# ── reference summary cache (hover preview + issue-vs-PR resolution) ─────────
#
# One tiny file per referenced number (``ref-{n}.json``). Short TTL because this
# backs a hover card: a stale state pill ("open" on a merged PR) is exactly the
# kind of wrong that makes a preview worse than no preview, and the underlying
# call is a single cheap request.
REF_SUMMARY_CACHE_TTL_SEC = 300.0


def ref_summary_cache_path(owner: str, repo: str, number: int, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / f"ref-{int(number)}.json"


def write_ref_summary_cache(
    owner: str, repo: str, number: int, summary: dict, *, root: Path | None = None
) -> None:
    """Cache one reference summary."""
    atomic_write(
        ref_summary_cache_path(owner, repo, number, root),
        json.dumps(
            {"owner": owner, "repo": repo, "number": int(number), "summary": summary},
            indent=2,
        ),
    )


def read_ref_summary_cache(
    owner: str,
    repo: str,
    number: int,
    root: Path | None = None,
    *,
    max_age_sec: float | None = None,
) -> dict | None:
    """Return the cached summary dict, or None when absent or older than
    ``max_age_sec`` (freshness is the cache's property, not the caller's — same
    rule as ``read_pr_detail_cache``)."""
    path = ref_summary_cache_path(owner, repo, number, root)
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
    if not isinstance(data, dict):
        return None
    summary = data.get("summary")
    return summary if isinstance(summary, dict) else None


# ── post-write cache coherence ───────────────────────────────────────────────
#
# After a label/state write, the served caches (issues-cache + issue-{n}.json)
# would otherwise be stale until the next refresh. These patch them in place so
# the change is durable across a reload / repo-switch without a slow full
# re-fetch, and so the frontend's optimistic update matches what the backend
# will serve next.


def _load_list_cache(
    owner: str, repo: str, root: Path | None, state: str
) -> tuple[dict | None, Path]:
    path = issues_cache_path(owner, repo, root, state)
    if not path.is_file():
        return None, path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, path
    return (data if isinstance(data, dict) else None), path


def apply_label_change_to_caches(
    owner: str, repo: str, number: int, label_objs: list[dict], *, root: Path | None = None
) -> None:
    """Patch an issue's labels in the detail cache + whichever list cache holds
    it, and drop its AI cache (the suggestion set is now stale).

    ``label_objs`` is the authoritative full label set (``[{name,color,...}]``)
    returned by the write; the list caches store only names."""
    names = [lab.get("name") for lab in label_objs if lab.get("name")]

    dpath = issue_detail_cache_path(owner, repo, number, root)
    if dpath.is_file():
        try:
            d = json.loads(dpath.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("detail"), dict):
                d["detail"]["labels"] = label_objs
                atomic_write(dpath, json.dumps(d, indent=2))
        except json.JSONDecodeError:
            pass

    for st in ("open", "closed"):
        # Hold the lock across read AND write: a concurrent full refresh would
        # otherwise land between them and be clobbered by this stale copy.
        with issues_cache_lock(owner, repo, root, st):
            data, path = _load_list_cache(owner, repo, root, st)
            if not data:
                continue
            changed = False
            for iss in data.get("issues", []):
                if iss.get("number") == int(number):
                    iss["labels"] = names
                    changed = True
            if changed:
                atomic_write(path, json.dumps(data, indent=2))

    delete_issue_ai_cache(owner, repo, number, root)


def apply_state_change_to_caches(
    owner: str,
    repo: str,
    number: int,
    state: str,
    state_reason: str | None,
    *,
    root: Path | None = None,
) -> None:
    """Patch an issue's state in the detail cache and drop it from the list
    cache it does not belong in (the open list on close, the closed list on
    reopen). The issue reappears in the correct list on the next refresh."""
    dpath = issue_detail_cache_path(owner, repo, number, root)
    if dpath.is_file():
        try:
            d = json.loads(dpath.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("detail"), dict):
                d["detail"]["state"] = state
                d["detail"]["state_reason"] = state_reason
                atomic_write(dpath, json.dumps(d, indent=2))
        except json.JSONDecodeError:
            pass

    drop_from = "open" if state == "closed" else "closed"
    with issues_cache_lock(owner, repo, root, drop_from):
        data, path = _load_list_cache(owner, repo, root, drop_from)
        if data:
            issues = data.get("issues", [])
            kept = [i for i in issues if i.get("number") != int(number)]
            if len(kept) != len(issues):
                data["issues"] = kept
                atomic_write(path, json.dumps(data, indent=2))


def apply_assignees_change_to_caches(
    owner: str, repo: str, number: int, assignees: list[str], *, root: Path | None = None
) -> None:
    """Patch an issue's assignees in the detail cache + whichever list cache
    holds it, so the sidebar and the row reflect the change without a refetch.

    ``assignees`` is the authoritative full set of logins returned by the write.
    Both the detail payload and the list row store assignees as a bare login
    list, so the same value writes to both."""
    logins = [a for a in assignees if a]

    dpath = issue_detail_cache_path(owner, repo, number, root)
    if dpath.is_file():
        try:
            d = json.loads(dpath.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("detail"), dict):
                d["detail"]["assignees"] = logins
                atomic_write(dpath, json.dumps(d, indent=2))
        except json.JSONDecodeError:
            pass

    for st in ("open", "closed"):
        # Hold the lock across read AND write, matching the label patch: a
        # concurrent full refresh would otherwise land between them and be
        # clobbered by this stale copy.
        with issues_cache_lock(owner, repo, root, st):
            data, path = _load_list_cache(owner, repo, root, st)
            if not data:
                continue
            changed = False
            for iss in data.get("issues", []):
                if iss.get("number") == int(number):
                    iss["assignees"] = logins
                    changed = True
            if changed:
                atomic_write(path, json.dumps(data, indent=2))
