"""Issue Radar — on-disk data layout.

Everything lives under ``~/.kirocrew/apps/issue-radar/data/`` (via
``kiro_crew.apps.manager.app_data_dir``, the platform-standard app-scoped data
dir). Nothing is stored on a KiroCrew-hosted backend and no GitHub App/PAT is
used — auth is entirely delegated to the user's own ``gh`` CLI session.

Layout::

    <data_dir>/config.json                          # connected repos, no secrets
    <data_dir>/repos/{owner}__{repo}/issues-cache.json  # last-fetched open issues
    <data_dir>/repos/{owner}__{repo}/members-cache.json # repo members (derived)

``root`` is accepted on every function (mirroring code_review_sage's
``store.py``) so tests can point at a tmp dir instead of the real app data dir.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write

APP_NAME = "issue-radar"


def data_dir(root: Path | None = None) -> Path:
    """Return the app's data dir, creating it if missing."""
    data = root if root is not None else app_data_dir(APP_NAME)
    data.mkdir(parents=True, exist_ok=True)
    return data


def config_path(root: Path | None = None) -> Path:
    return data_dir(root) / "config.json"


def read_config(root: Path | None = None) -> dict[str, Any]:
    """Read config.json. Returns {"repos": []} if it doesn't exist yet."""
    path = config_path(root)
    if not path.is_file():
        return {"repos": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"repos": []}


def write_config(config: dict[str, Any], root: Path | None = None) -> None:
    atomic_write(config_path(root), json.dumps(config, indent=2))


@contextlib.contextmanager
def _config_lock(root: Path | None = None):
    """Serialize the config.json read-modify-write critical section across
    threads AND processes. ``atomic_write`` prevents torn *files* but not lost
    *updates*: two overlapping read→mutate→write cycles each replace the whole
    document, so the later writer silently clobbers the earlier one. Concurrent
    connect / settings / permission-refresh / disconnect requests hit exactly
    that race, so every config RMW below holds this exclusive lock across the
    whole read→mutate→atomic-write."""
    lock_path = data_dir(root) / "config.json.lock"
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            yield


def repo_slug_dir_name(owner: str, repo: str) -> str:
    # Use nested directories (owner/repo) so repos whose names contain "__"
    # don't collide.  This helper remains for migration/test use; repo_data_dir
    # is the canonical path builder.
    return f"{owner}/{repo}"


def repo_data_dir(owner: str, repo: str, root: Path | None = None) -> Path:
    d = data_dir(root) / "repos" / owner / repo
    d.mkdir(parents=True, exist_ok=True)
    return d


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


def write_issues_cache(
    owner: str, repo: str, issues: list[dict], *, root: Path | None = None, state: str = "open"
) -> None:
    atomic_write(
        issues_cache_path(owner, repo, root, state),
        json.dumps(
            {"schema": ISSUES_CACHE_SCHEMA, "owner": owner, "repo": repo, "state": state, "issues": issues},
            indent=2,
        ),
    )


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
    if data.get("schema") != ISSUES_CACHE_SCHEMA:
        return None  # stale schema → treat as a miss so the route refetches
    issues = data.get("issues")
    return issues if isinstance(issues, list) else None


def labels_cache_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / "labels-cache.json"


def write_labels_cache(
    owner: str, repo: str, labels: list[dict], *, root: Path | None = None
) -> None:
    atomic_write(
        labels_cache_path(owner, repo, root),
        json.dumps({"owner": owner, "repo": repo, "labels": labels}, indent=2),
    )


def read_labels_cache(owner: str, repo: str, root: Path | None = None) -> list[dict] | None:
    """Return cached repo labels, or None if no cache exists yet."""
    path = labels_cache_path(owner, repo, root)
    if not path.is_file():
        return None
    try:
        labels = json.loads(path.read_text(encoding="utf-8")).get("labels")
    except json.JSONDecodeError:
        return None
    # A non-list (older/corrupt shape) is treated as a miss so the route
    # refetches with the current shape rather than serving a non-array.
    return labels if isinstance(labels, list) else None


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
    # Coerce ``members`` to a list: the members cache carries no schema stamp
    # (unlike issues), so a file written by an older build with a different
    # shape must never surface a non-array — the frontend would then crash on
    # ``.map`` behind its error boundary. A non-list degrades to empty here.
    members = data.get("members")
    return {"members": members if isinstance(members, list) else [], "source": data.get("source")}


def list_connected_repos(root: Path | None = None) -> list[dict[str, Any]]:
    """Return the connected-repo list from config.json (``[]`` if none)."""
    return read_config(root).get("repos", [])


def is_repo_connected(owner: str, repo: str, root: Path | None = None) -> bool:
    config = read_config(root)
    return any(r["owner"] == owner and r["repo"] == repo for r in config.get("repos", []))


def add_connected_repo(owner: str, repo: str, *, permissions: dict | None = None, root: Path | None = None) -> None:
    """Add (owner, repo) to config.json's repo list. Idempotent.

    Stores the repo's GitHub ``permissions`` object (admin/maintain/push/pull/
    triage) so the UI can badge Read/Write access without a live call; updates
    it on reconnect if a fresh value is supplied.
    """
    with _config_lock(root):
        config = read_config(root)
        repos = config.setdefault("repos", [])
        existing = next((r for r in repos if r["owner"] == owner and r["repo"] == repo), None)
        if existing is None:
            repos.append({"owner": owner, "repo": repo, "enabled": True, "permissions": permissions})
        elif permissions is not None:
            existing["permissions"] = permissions
        write_config(config, root)


def set_repo_permissions(owner: str, repo: str, permissions: dict | None, *, root: Path | None = None) -> None:
    """Persist a repo's permissions object into its config entry (self-heal path
    for repos connected before permissions were tracked)."""
    with _config_lock(root):
        config = read_config(root)
        for r in config.get("repos", []):
            if r["owner"] == owner and r["repo"] == repo:
                r["permissions"] = permissions
        write_config(config, root)


# ── per-repo triage settings ────────────────────────────────────────────────
#
# Each connected repo carries a small ``settings`` object (stored inline in its
# config.json entry, so there is one source of truth and no extra file to keep
# in sync). These are *local* triage preferences — never written back to
# GitHub — that teach Issue Radar how THIS repo labels its work:
#
#   triage_labels            names of labels that mean "still needs triage"
#   unlabeled_is_untriaged   also treat issues with no labels as needing triage
#   good_first_issue_labels  names of labels that mark newcomer-friendly issues
#   notify_on_new_issue      push a KiroCrew notification when a new issue opens
#
# Different repos use different conventions (``needs-triage`` vs ``status: triage``
# vs just "no label"; ``good first issue`` vs ``help wanted`` vs ``beginner``),
# so every field is per-repo and defaults to a safe, backwards-compatible value
# (empty label sets + "unlabeled == untriaged", which is exactly the heuristic
# the dashboards used before settings existed). ``notify_on_new_issue`` is
# opt-in (default off): the background watcher only polls repos that turn it on.

DEFAULT_REPO_SETTINGS: dict[str, Any] = {
    "triage_labels": [],
    "unlabeled_is_untriaged": True,
    "good_first_issue_labels": [],
    "notify_on_new_issue": False,
}


def _normalize_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce an arbitrary (possibly client-supplied) settings blob into the
    known schema: string label lists (de-duplicated, order-preserving) and a
    boolean toggle. Unknown keys are dropped."""
    raw = raw or {}

    def _labels(key: str) -> list[str]:
        val = raw.get(key, [])
        if not isinstance(val, list):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for item in val:
            if isinstance(item, str):
                name = item.strip()
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)
        return out

    return {
        "triage_labels": _labels("triage_labels"),
        "unlabeled_is_untriaged": bool(raw.get("unlabeled_is_untriaged", True)),
        "good_first_issue_labels": _labels("good_first_issue_labels"),
        "notify_on_new_issue": bool(raw.get("notify_on_new_issue", False)),
    }


def read_repo_settings(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    """Return the normalized triage settings for a repo (defaults if unset)."""
    for r in read_config(root).get("repos", []):
        if r["owner"] == owner and r["repo"] == repo:
            return _normalize_settings(r.get("settings"))
    return dict(DEFAULT_REPO_SETTINGS)


def write_repo_settings(
    owner: str, repo: str, settings: dict[str, Any], *, root: Path | None = None
) -> dict[str, Any]:
    """Persist (after normalizing) a repo's triage settings into its config
    entry. Raises ``KeyError`` if the repo is not connected. Returns the
    normalized object that was stored."""
    normalized = _normalize_settings(settings)
    with _config_lock(root):
        config = read_config(root)
        found = False
        for r in config.get("repos", []):
            if r["owner"] == owner and r["repo"] == repo:
                r["settings"] = normalized
                found = True
        if not found:
            raise KeyError(f"{owner}/{repo} is not connected")
        write_config(config, root)
    return normalized


# ── new-issue watch state (background watcher high-water mark) ────────────────
#
# The in-process watcher (backend/watch.py) records, per repo, the highest issue
# number it has seen. GitHub issue/PR numbers are globally monotonic, so any
# open issue whose number exceeds this mark was created since the last check.
# One tiny file per repo under the repo's cache dir, so ``remove_connected_repo``'s
# rmtree cleans it up on disconnect. An absent file means "never observed" — the
# watcher then seeds the mark WITHOUT notifying, so it never announces the whole
# existing backlog on the first poll after a repo opts in.


def watch_state_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / "watch-state.json"


def read_watch_state(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    """Return the watcher's per-repo state (``{}`` if never observed)."""
    path = watch_state_path(owner, repo, root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def write_watch_state(
    owner: str, repo: str, last_seen_number: int, root: Path | None = None
) -> None:
    """Persist the highest issue number seen for a repo (the watcher's
    high-water mark)."""
    atomic_write(
        watch_state_path(owner, repo, root),
        json.dumps(
            {"owner": owner, "repo": repo, "last_seen_number": int(last_seen_number)},
            indent=2,
        ),
    )


def remove_connected_repo(owner: str, repo: str, *, root: Path | None = None) -> bool:
    """Disconnect a repo: drop it from config.json and delete its local cache
    dir. Local-only — nothing on GitHub is touched. Returns True if a repo was
    removed, False if it was not connected."""
    with _config_lock(root):
        config = read_config(root)
        repos = config.get("repos", [])
        kept = [r for r in repos if not (r["owner"] == owner and r["repo"] == repo)]
        if len(kept) == len(repos):
            return False
        config["repos"] = kept
        write_config(config, root)
    cache_dir = repo_data_dir(owner, repo, root)
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
    # Clean up the now-empty owner dir if this was the last repo for that owner.
    owner_dir = cache_dir.parent
    if owner_dir.exists() and not any(owner_dir.iterdir()):
        owner_dir.rmdir()
    return True


def issue_detail_cache_path(owner: str, repo: str, number: int, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / f"issue-{int(number)}.json"


def write_issue_detail_cache(
    owner: str, repo: str, number: int, detail: dict, timeline: list[dict], *, root: Path | None = None
) -> None:
    """Cache one issue's full detail + normalized timeline.

    One file per issue (``issue-{number}.json``) so a detail view opens
    instantly (and offline) on re-visit; ``refresh=1`` on the route bypasses it.
    """
    atomic_write(
        issue_detail_cache_path(owner, repo, number, root),
        json.dumps(
            {"owner": owner, "repo": repo, "number": int(number), "detail": detail, "timeline": timeline},
            indent=2,
        ),
    )


def read_issue_detail_cache(owner: str, repo: str, number: int, root: Path | None = None) -> dict | None:
    """Return ``{"detail", "timeline"}`` for a cached issue, or None if absent."""
    path = issue_detail_cache_path(owner, repo, number, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return {"detail": data.get("detail"), "timeline": data.get("timeline", [])}


# ── AI triage cache (summary + suggested labels) ─────────────────────────────
#
# The AI summary + suggested-labels for one issue are computed by a single LLM
# call and cached per issue (``issue-{n}-ai.json``), mirroring the cache-first
# philosophy of the issue/detail/labels caches: the (relatively expensive) model
# call is paid once per issue and served instantly on re-open; ``refresh=1`` on
# the route bypasses it, and a label edit drops it (the applied label changes
# what counts as "already on the issue", so suggestions must be recomputed).


def issue_ai_cache_path(owner: str, repo: str, number: int, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / f"issue-{int(number)}-ai.json"


def _cache_generated_at(data: dict, path: Path) -> str | None:
    """The stamped ``generated_at``, falling back to the file's mtime.

    Caches written before the field existed carry no stamp, and the UI would then
    show no age at all until the user manually regenerated. The mtime is when the
    cache was written, which IS when the summary was generated — so it is the
    right answer, not a guess.
    """
    stamped = data.get("generated_at")
    if isinstance(stamped, str) and stamped:
        return stamped
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def write_issue_ai_cache(
    owner: str, repo: str, number: int, payload: dict, *, root: Path | None = None
) -> None:
    """Cache one issue's AI triage result (``{summary, suggested_labels}``).

    Stamped with ``generated_at`` so the UI can show how old the summary is —
    without it a cached card gives no hint whether it was written minutes or
    months ago."""
    atomic_write(
        issue_ai_cache_path(owner, repo, number, root),
        json.dumps(
            {
                "owner": owner, "repo": repo, "number": int(number),
                "summary": payload.get("summary", ""),
                "suggested_labels": payload.get("suggested_labels", []),
                "generated_at": _now_iso(),
            },
            indent=2,
        ),
    )


def read_issue_ai_cache(owner: str, repo: str, number: int, root: Path | None = None) -> dict | None:
    """Return ``{"summary", "suggested_labels", "generated_at"}`` for a cached
    issue, or None. Caches written before the stamp existed fall back to the
    file's mtime (see _cache_generated_at)."""
    path = issue_ai_cache_path(owner, repo, number, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return {
        "summary": data.get("summary", ""),
        "suggested_labels": data.get("suggested_labels", []),
        "generated_at": _cache_generated_at(data, path),
    }


def delete_issue_ai_cache(owner: str, repo: str, number: int, root: Path | None = None) -> None:
    """Drop a cached AI result (called after a label edit so it recomputes)."""
    issue_ai_cache_path(owner, repo, number, root).unlink(missing_ok=True)


# ── PR AI summary cache ──────────────────────────────────────────────────────
#
# Unlike an issue's triage result, a PR summary goes stale on its own: it reads
# the description, EVERY comment/review, and the check state, all of which move
# while the PR is open. So the cache is keyed by a FINGERPRINT of those inputs
# (see routes._pr_ai_fingerprint) and a mismatch reads as a miss — a new comment
# or a flipped check silently earns a fresh summary, with no user action and no
# repeated model call while nothing has changed.


def pr_ai_cache_path(owner: str, repo: str, number: int, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / f"pull-{int(number)}-ai.json"


def write_pr_ai_cache(
    owner: str, repo: str, number: int, payload: dict, *, root: Path | None = None
) -> None:
    """Cache one PR's AI summary together with the fingerprint it was built from."""
    atomic_write(
        pr_ai_cache_path(owner, repo, number, root),
        json.dumps(
            {
                "owner": owner, "repo": repo, "number": int(number),
                "summary": payload.get("summary", ""),
                "fingerprint": payload.get("fingerprint", ""),
                "generated_at": _now_iso(),
            },
            indent=2,
        ),
    )


def read_pr_ai_cache(
    owner: str, repo: str, number: int, root: Path | None = None, *, fingerprint: str | None = None
) -> dict | None:
    """Return ``{"summary", "generated_at"}`` for a cached PR summary, or None.

    A stored fingerprint that does not match ``fingerprint`` is a MISS: the PR has
    moved (new comment, new push, check flipped) since the summary was written.
    """
    path = pr_ai_cache_path(owner, repo, number, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    # A syntactically VALID but non-object root (``[]``, a bare string) would blow
    # up on .get() and keep failing every request until the file is deleted by
    # hand. Treat it as a miss and let the route rewrite it.
    if not isinstance(data, dict):
        return None
    if fingerprint is not None and data.get("fingerprint") != fingerprint:
        return None
    return {"summary": data.get("summary", ""), "generated_at": _cache_generated_at(data, path)}


# ── AI label recommendations (per-repo taxonomy proposal) ────────────────────
#
# One cache per repo (NOT per issue): the "what NEW labels should this repo add"
# result computed over the repo's existing labels + a sample of open issues.
# Generated on explicit user action (the settings "Recommend labels" button),
# so it is cached until the user regenerates.

def recommendations_cache_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / "recommendations-cache.json"


def write_recommendations_cache(
    owner: str, repo: str, payload: dict, *, root: Path | None = None
) -> None:
    """Cache a repo's AI label recommendations (``{recommendations, generated_at}``)."""
    atomic_write(
        recommendations_cache_path(owner, repo, root),
        json.dumps(
            {
                "owner": owner, "repo": repo,
                "recommendations": payload.get("recommendations", []),
                "generated_at": payload.get("generated_at", ""),
            },
            indent=2,
        ),
    )


def read_recommendations_cache(owner: str, repo: str, root: Path | None = None) -> dict | None:
    """Return ``{"recommendations", "generated_at"}`` for a repo, or None."""
    path = recommendations_cache_path(owner, repo, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return {
        "recommendations": data.get("recommendations", []),
        "generated_at": data.get("generated_at", ""),
    }


def add_label_to_cache(owner: str, repo: str, label: dict, *, root: Path | None = None) -> None:
    """Append a newly-created label to the labels cache so the pickers show it
    immediately. No-op when the cache doesn't exist yet (a later refresh fetches
    the full set) or the label is already present."""
    labels = read_labels_cache(owner, repo, root)
    if labels is None:
        return
    if any(isinstance(lab, dict) and lab.get("name") == label.get("name") for lab in labels):
        return
    labels.append({
        "name": label.get("name"),
        "color": label.get("color") or "888888",
        "description": label.get("description") or "",
    })
    write_labels_cache(owner, repo, labels, root=root)


# ── post-write cache coherence ───────────────────────────────────────────────
#
# After a label/state write, the served caches (issues-cache + issue-{n}.json)
# would otherwise be stale until the next refresh. These patch them in place so
# the change is durable across a reload / repo-switch without a slow full
# re-fetch, and so the frontend's optimistic update matches what the backend
# will serve next.


def _load_list_cache(owner: str, repo: str, root: Path | None, state: str) -> tuple[dict | None, Path]:
    path = issues_cache_path(owner, repo, root, state)
    if not path.is_file():
        return None, path
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except json.JSONDecodeError:
        return None, path


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
            if isinstance(d.get("detail"), dict):
                d["detail"]["labels"] = label_objs
                atomic_write(dpath, json.dumps(d, indent=2))
        except json.JSONDecodeError:
            pass

    for st in ("open", "closed"):
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
    owner: str, repo: str, number: int, state: str, state_reason: str | None,
    *, root: Path | None = None,
) -> None:
    """Patch an issue's state in the detail cache and drop it from the list
    cache it no longer belongs to (the open list on close, the closed list on
    reopen). The issue reappears in the correct list on the next refresh."""
    dpath = issue_detail_cache_path(owner, repo, number, root)
    if dpath.is_file():
        try:
            d = json.loads(dpath.read_text(encoding="utf-8"))
            if isinstance(d.get("detail"), dict):
                d["detail"]["state"] = state
                d["detail"]["state_reason"] = state_reason
                atomic_write(dpath, json.dumps(d, indent=2))
        except json.JSONDecodeError:
            pass

    drop_from = "open" if state == "closed" else "closed"
    data, path = _load_list_cache(owner, repo, root, drop_from)
    if data:
        issues = data.get("issues", [])
        kept = [i for i in issues if i.get("number") != int(number)]
        if len(kept) != len(issues):
            data["issues"] = kept
            atomic_write(path, json.dumps(data, indent=2))


# ── investigation records (the "Investigate" button) ─────────────────────────
#
# Clicking "Investigate" on an issue opens a KiroCrew chat session, seeds it
# with an investigation prompt, and files it into a per-repo "Issue Radar -
# <repo>" chat folder. There is NO shared, git-backed,
# CLI-driven ledger; instead each investigated issue gets ONE small local
# record, keyed by number like the detail/AI caches, so Issue Radar can:
#   * RESUME the same session on a repeat click (via ``slot_key``) instead of
#     spawning a duplicate;
#   * badge the issue's investigation ``status``;
#   * retain ``findings`` the investigating agent (or the user) writes back.
# The record lives under the repo's cache dir, so ``remove_connected_repo``'s
# ``rmtree`` cleans it up on disconnect — nothing on GitHub is ever touched.

_INVESTIGATION_STATUSES = ("investigating", "resolved", "archived")


def _now_iso() -> str:
    """UTC timestamp, microsecond precision, ``Z`` suffix — stable, sortable, and
    fine-grained enough that rapid successive writes order deterministically
    when investigation records are sorted on ``last_opened_at``."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# Public alias: the routes layer stamps freshly-computed AI results with the same
# clock the caches use, so a "generated N minutes ago" label reads identically
# whether the response came from cache or was just computed.
now_iso = _now_iso


def investigation_path(owner: str, repo: str, number: int, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / f"investigation-{int(number)}.json"


def read_investigation(owner: str, repo: str, number: int, root: Path | None = None) -> dict | None:
    """Return an issue's investigation record, or None if never investigated."""
    path = investigation_path(owner, repo, number, root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _normalize_findings(raw: Any) -> dict[str, Any] | None:
    """Coerce a (client- or agent-supplied) findings blob into the known schema,
    or None. Strings are trimmed; ``suggested_labels`` is a de-duplicated string
    list; unknown keys are dropped. An all-empty result collapses to None so
    "no findings yet" stays null rather than a hollow object."""
    if not isinstance(raw, dict):
        return None

    def _s(key: str) -> str | None:
        v = raw.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    labels: list[str] = []
    labels_raw = raw.get("suggested_labels")
    if isinstance(labels_raw, list):
        seen: set[str] = set()
        for item in labels_raw:
            if isinstance(item, str) and item.strip() and item.strip() not in seen:
                seen.add(item.strip())
                labels.append(item.strip())

    findings = {
        "verdict": _s("verdict"),
        "root_cause": _s("root_cause"),
        "suggested_labels": labels,
        "next_action": _s("next_action"),
        "summary": _s("summary"),
    }
    if not any(findings.values()):
        return None
    return findings


def write_investigation(
    owner: str, repo: str, number: int, patch: dict[str, Any], *, root: Path | None = None
) -> dict[str, Any]:
    """Upsert an issue's investigation record, MERGING ``patch`` into any
    existing record (last-writer-wins per field). ``started_at`` is stamped once
    on first create; ``last_opened_at`` is refreshed on every write. Only known,
    validated fields are applied — ``slot_key``/``folder_id`` (strings; ``""``
    clears to None), ``status`` (one of ``_INVESTIGATION_STATUSES``), and
    ``findings`` (normalized). A partial patch (even ``{}``, which just bumps the
    open stamp) is valid. Returns the stored record."""
    number = int(number)
    now = _now_iso()
    lock_path = investigation_path(owner, repo, number, root).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            existing = read_investigation(owner, repo, number, root) or {}

            record: dict[str, Any] = {
                "owner": owner,
                "repo": repo,
                "number": number,
                "slot_key": existing.get("slot_key"),
                "folder_id": existing.get("folder_id"),
                "status": existing.get("status") if existing.get("status") in _INVESTIGATION_STATUSES else "investigating",
                "started_at": existing.get("started_at") or now,
                "last_opened_at": now,
                "findings": existing.get("findings"),
            }

            if "slot_key" in patch and isinstance(patch["slot_key"], str):
                record["slot_key"] = patch["slot_key"].strip() or None
            if "folder_id" in patch and isinstance(patch["folder_id"], str):
                record["folder_id"] = patch["folder_id"].strip() or None
            if "status" in patch:
                st = str(patch.get("status") or "").strip().lower()
                if st in _INVESTIGATION_STATUSES:
                    record["status"] = st
            if "findings" in patch:
                record["findings"] = _normalize_findings(patch.get("findings"))

            atomic_write(investigation_path(owner, repo, number, root), json.dumps(record, indent=2))
    return record


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
PULLS_CACHE_SCHEMA = 5


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
    with open(path.with_suffix(".json.lock"), "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            yield


def write_pulls_cache(
    owner: str, repo: str, pulls: list[dict], *, root: Path | None = None, state: str = "open"
) -> None:
    with _pulls_cache_lock(owner, repo, root, state):
        atomic_write(
            pulls_cache_path(owner, repo, root, state),
            json.dumps(
                {"schema": PULLS_CACHE_SCHEMA, "owner": owner, "repo": repo,
                 "state": state, "pulls": pulls},
                indent=2,
            ),
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
                    row["checks_counts"] = summary.get("checks_counts")
                    row["checks_state"] = summary.get("checks_state")
                    # Also clear a stale truncation flag: this tally comes from the
                    # fully-paginated detail read, so it is complete even if the
                    # GraphQL enrichment had to give up on a >1-page PR.
                    row["checks_truncated"] = bool(summary.get("checks_truncated"))
                    changed = True
            if changed:
                atomic_write(path, json.dumps(data, indent=2))


def drop_pulls_cache(owner: str, repo: str, state: str = "open", *, root: Path | None = None) -> None:
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
PR_DETAIL_CACHE_SCHEMA = 4

# How long a cached PR detail may be served to a plain (non-``refresh=1``) read.
# Freshness belongs to the cache, not to the caller: this is what lets the detail
# pane simply poll, and keeps the route honest for any other consumer.
PR_DETAIL_CACHE_TTL_SEC = 30.0


def write_pr_detail_cache(
    owner: str, repo: str, number: int, detail: dict, timeline: list[dict], checks: list[dict],
    *, root: Path | None = None,
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
                "owner": owner, "repo": repo, "number": int(number),
                "detail": detail, "timeline": timeline, "checks": checks,
            },
            indent=2,
        ),
    )


def read_pr_detail_cache(
    owner: str, repo: str, number: int, root: Path | None = None,
    *, max_age_sec: float | None = None,
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
