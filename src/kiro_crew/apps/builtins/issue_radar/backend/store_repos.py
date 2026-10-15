"""Issue Radar's data root and its ``config.json`` repo registry.

The data dir, ``config.json`` and its lock, the provider-scoped repo directories,
the connected repos with their name matching, permissions and per-repo triage
settings, the background watchers' per-repo state, and the followed GitLab groups.
Callers reach every name here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.store`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger("kiro_crew.apps.builtins.issue_radar.backend.store")

APP_NAME = "issue-radar"


def data_dir(root: Path | None = None) -> Path:
    """Return the app's data dir, creating it if missing."""
    data = root if root is not None else app_data_dir(APP_NAME)
    data.mkdir(parents=True, exist_ok=True)
    return data


def config_path(root: Path | None = None) -> Path:
    return data_dir(root) / "config.json"


def read_config(root: Path | None = None) -> dict[str, Any]:
    """Read config.json, keeping malformed repo rows out of every caller."""
    path = config_path(root)
    if not path.is_file():
        return {"repos": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"repos": []}
    if not isinstance(data, dict):
        return {"repos": []}
    repos = data.get("repos", [])
    data["repos"] = (
        [row for row in repos if isinstance(row, dict)] if isinstance(repos, list) else []
    )
    return data


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
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            yield


# ── provider-scoped storage ─────────────────────────────────────────────────
#
# A GitHub repo's data lives at ``<data>/repos/{owner}/{repo}``. GitLab adds two
# dimensions to a repo's identity -- the provider and, for self-managed
# instances, the HOST -- and both must be part of the storage path, because
# ``group/project`` names an entirely different project on gitlab.com than on a
# private instance, and a GitLab group can share a name with a GitHub owner.
#
# Public GitHub keeps its ORIGINAL path, which keeps the layout migration-free:
# an install that has been triaging GitHub issues keeps every cache, setting, and
# investigation note exactly where it already is, and a bug in the GitLab layout
# cannot corrupt existing data because that layout is only ever entered by a
# non-GitHub key.
#
# Everything else is rooted under a reserved segment that a GitHub owner can
# never produce: ``parse_github_repo_url`` constrains owners to
# ``[A-Za-z0-9._-]+``, so no ``@``. Without that guarantee a repo literally named
# after the reserved segment could be made to collide with the provider subtree.
_PROVIDER_SUBTREE = "@providers"


def _host_slug(host: str) -> str:
    """Filesystem-safe form of a host[:port].

    A port makes the host ``gitlab.internal:8443``, and ``:`` is not a legal path
    character on Windows. The whole app is POSIX-only at the CLI layer, but store
    functions are exercised on Windows in CI, so the separator is folded to ``_``
    here rather than leaving a path that only fails on one platform.
    """
    return (host or "").lower().replace(":", "_")


def provider_root(
    owner: str = "",
    repo: str = "",
    root: Path | None = None,
    *,
    provider: str = "github",
    host: str = "github.com",
) -> Path:
    """The data root that scopes one provider+host's repositories.

    Pass the result as ``root=`` to any per-repo store function and its whole
    subtree (caches, settings, watch state, investigations) lands under this
    provider instead of GitHub's legacy tree. Threading the scope through the
    ``root`` parameter every store function ALREADY accepts is deliberate: it
    keeps the ~40 cache functions byte-identical, so their behaviour -- and their
    tests -- cannot regress while GitLab support is added.

    ``owner``/``repo`` are accepted and unused so callers can pass a repo key
    positionally without special-casing; the scope depends only on provider+host.
    """
    del owner, repo
    base = data_dir(root)
    if provider == "github" and host == "github.com":
        return base  # legacy layout — existing data stays exactly where it is
    return base / _PROVIDER_SUBTREE / provider / _host_slug(host)


def repo_data_dir(owner: str, repo: str, root: Path | None = None) -> Path:
    d = data_dir(root) / "repos" / owner / repo
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_connected_repos(root: Path | None = None) -> list[dict[str, Any]]:
    """Return the connected-repo list from config.json (``[]`` if none).

    Entries written before GitLab support carry no ``provider``/``host``, so both
    are filled in on read rather than being left absent. Doing it here -- the one
    function every reader goes through -- means no caller (route, watcher, or UI)
    has to know that a legacy entry means GitHub.
    """
    repos = read_config(root).get("repos", [])
    for entry in repos:
        if isinstance(entry, dict):
            entry.setdefault("provider", "github")
            entry.setdefault("host", "github.com")
    return repos


def is_repo_connected(
    owner: str,
    repo: str,
    root: Path | None = None,
    *,
    provider: str = "github",
    host: str = "github.com",
) -> bool:
    """Whether this exact provider+host+owner+repo is connected.

    This is the app's authorization gate, not a convenience lookup: routes accept
    ``owner``/``repo`` straight from the query string WITHOUT charset validation
    and rely on this returning False for anything that was not deliberately
    connected from a parsed URL. Including provider+host in the match is therefore
    load-bearing -- without it, a request naming an allowlisted self-managed
    project could be served against gitlab.com (or against GitHub) at the same
    path.
    """
    return any(
        _same_repo(r, owner, repo, provider=provider, host=host) for r in list_connected_repos(root)
    )


def find_connected_repo(
    owner: str,
    repo: str,
    root: Path | None = None,
    *,
    provider: str = "github",
    host: str = "github.com",
) -> dict[str, Any] | None:
    """The config entry for this exact provider+host+owner+repo, or ``None``.

    The read-only counterpart to :func:`is_repo_connected`, for callers that need
    the entry's payload (notably its cached ``permissions``) and not just whether
    it exists. Public so callers do not reach into :func:`_same_repo`.
    """
    for entry in list_connected_repos(root):
        if _same_repo(entry, owner, repo, provider=provider, host=host):
            return entry
    return None


def add_connected_repo(
    owner: str,
    repo: str,
    *,
    permissions: dict | None = None,
    provider: str = "github",
    host: str = "github.com",
    root: Path | None = None,
) -> None:
    """Add a repo to config.json's repo list. Idempotent.

    Stores the repo's ``permissions`` object (admin/maintain/push/pull/triage) so
    the UI can badge Read/Write access without a live call; updates it on
    reconnect if a fresh value is supplied.

    Identity follows the provider's own case semantics (see :func:`_same_repo`).
    GitHub names are case-preserving but not case-sensitive, so ``acme/widget``
    and ``Acme/Widget`` are one repo -- a case-sensitive match would append a
    second entry with its own independent caches and triage settings; the first
    spelling connected stays the stored one, so existing entries are never
    rewritten.

    GitLab project paths ARE case-sensitive, so ``group/Project`` and
    ``group/project`` are distinct projects and are stored as distinct entries --
    matching how the provider API and cache paths resolve them. Folding case for
    GitLab would let the authorization gate admit a case-variant of a connected
    project that the data-plane then resolves to a different project.
    """
    with _config_lock(root):
        config = read_config(root)
        repos = config.setdefault("repos", [])
        existing = next(
            (r for r in repos if _same_repo(r, owner, repo, provider=provider, host=host)), None
        )
        if existing is None:
            repos.append(
                {
                    "owner": owner,
                    "repo": repo,
                    "provider": provider,
                    "host": host,
                    "enabled": True,
                    "permissions": permissions,
                }
            )
        elif permissions is not None:
            existing["permissions"] = permissions
        write_config(config, root)


# Providers whose owner/repo names are case-INSENSITIVE at the source. GitHub
# names are case-preserving but not case-sensitive, so ``acme/widget`` and
# ``Acme/Widget`` address the same repository. GitLab project paths ARE
# case-sensitive: ``group/Project`` and ``group/project`` are different projects
# on the server. Any provider not listed here is matched case-SENSITIVELY -- the
# fail-safe default for an authorization gate, so an unknown/self-managed
# provider never silently widens the allowlist to case-variants.
#
# Azure DevOps is deliberately ABSENT. Its documented uniqueness rule for both
# project names and Git repository names is "must not be identical", which does
# not state a case-folding rule; the naming-restrictions page states one
# explicitly only for Artifacts FEED names ("can't differ from another feed name
# only by capitalization"), and that explicit contrast is evidence against
# assuming the same for projects and repos. Listing it on an unverified
# assumption is the one direction that fails unsafely: it would merge two
# distinct projects' caches and admit a case-variant through the gate. It can be
# added once the behaviour is confirmed against a real organization.
_CASE_INSENSITIVE_NAME_PROVIDERS = frozenset({"github"})


def name_compare_key(name: str, provider: str) -> str:
    """``name`` reduced to the form ``provider``'s case semantics compare on.

    The ONE definition of "same name" for a provider. A caller that needs a dict
    or set key rather than a pairwise comparison -- a probe memo, a
    connected-repo membership test -- goes through this instead of hand-rolling a
    ``.casefold()``, so it cannot drift from :func:`_name_matches` and start
    disagreeing with the authorization gate about which names are the same.
    """
    if provider.lower() in _CASE_INSENSITIVE_NAME_PROVIDERS:
        return name.casefold()
    return name


def _name_matches(a: str, b: str, provider: str) -> bool:
    """Compare two owner/repo name segments using ``provider``'s case semantics.

    Load-bearing for authorization: the gate MUST interpret the name exactly as
    the data-plane does. The data-plane (``repo_data_dir`` and the provider API
    call -- e.g. ``gitlab_client.project_path`` which addresses the raw-case
    slug) is case-sensitive for GitLab, so casefolding here would let a
    case-variant of a connected GitLab project pass the gate and then resolve to
    a DIFFERENT project under the owner's credentials.
    """
    return name_compare_key(a, provider) == name_compare_key(b, provider)


def _same_repo(
    entry: dict, owner: str, repo: str, *, provider: str = "github", host: str = "github.com"
) -> bool:
    """Provider-, host-, and provider-aware-case identity for a config entry.

    Name case sensitivity follows the provider (see :func:`_name_matches`):
    case-insensitive for GitHub, case-sensitive for GitLab and any other
    provider. This keeps the authorization gate's interpretation of owner/repo
    identical to the data-plane's, so a case-variant of a connected GitLab
    project is NOT authorized against a different project.

    An entry missing ``provider``/``host`` predates GitLab support and therefore
    means public GitHub; it is treated as such rather than as a non-match, so
    already-connected repos keep working after an upgrade.
    """
    entry_provider = str(entry.get("provider") or "github").lower()
    entry_host = str(entry.get("host") or "github.com").lower()
    return (
        entry_provider == provider.lower()
        and entry_host == host.lower()
        and _name_matches(str(entry.get("owner", "")), owner, entry_provider)
        and _name_matches(str(entry.get("repo", "")), repo, entry_provider)
    )


def set_repo_permissions(
    owner: str,
    repo: str,
    permissions: dict | None,
    *,
    provider: str = "github",
    host: str = "github.com",
    root: Path | None = None,
) -> None:
    """Persist a repo's permissions object into its config entry (self-heal path
    for repos connected before permissions were tracked)."""
    with _config_lock(root):
        config = read_config(root)
        for r in config.get("repos", []):
            if _same_repo(r, owner, repo, provider=provider, host=host):
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
#   notify_on_new_issue      push a Kiro Crew notification when a new issue opens
#   notify_on_own_pr_ci_failure
#                            push a Kiro Crew notification when a GitHub Actions
#                            run fails on an open pull request the signed-in
#                            ``gh`` user authored
#
# Different repos use different conventions (``needs-triage`` vs ``status: triage``
# vs just "no label"; ``good first issue`` vs ``help wanted`` vs ``beginner``),
# so every field is per-repo and defaults to a safe, backwards-compatible value
# (empty label sets + "unlabeled == untriaged", which is exactly the heuristic
# the dashboards used before settings existed). Both ``notify_on_*`` toggles are
# opt-in (default off): the background watcher makes a toggle's provider calls
# only for the repos that turn it on.

DEFAULT_REPO_SETTINGS: dict[str, Any] = {
    "triage_labels": [],
    "unlabeled_is_untriaged": True,
    "good_first_issue_labels": [],
    "notify_on_new_issue": False,
    "notify_on_own_pr_ci_failure": False,
    # Local absolute path to this repo's working copy. Empty string means "use
    # the gateway's default cwd" (the pre-workspace behavior). Local-only, like
    # every other field here -- never written back to the source host. The
    # Investigate action opens its chat session with this as the working
    # directory so the agent sees the repo's real source.
    "workspace_path": "",
    # Bumped by every write; a full-document PUT must echo what it read so a
    # stale snapshot cannot overwrite a newer change. See SettingsConflict.
    "revision": 0,
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

    try:
        revision = int(raw.get("revision", 0))
    except (TypeError, ValueError):
        revision = 0

    # A local filesystem path is stored verbatim (only stripped): it is NOT
    # validated against the filesystem here. The gateway may run on a different
    # host than the one the operator has in mind, and a path that does not exist
    # yet is a legitimate not-yet-checked-out state -- an empty string, not an
    # error. A non-string (or missing) value degrades to "" (default cwd).
    ws = raw.get("workspace_path", "")
    workspace_path = ws.strip() if isinstance(ws, str) else ""

    return {
        "triage_labels": _labels("triage_labels"),
        "unlabeled_is_untriaged": bool(raw.get("unlabeled_is_untriaged", True)),
        "good_first_issue_labels": _labels("good_first_issue_labels"),
        "notify_on_new_issue": bool(raw.get("notify_on_new_issue", False)),
        "notify_on_own_pr_ci_failure": raw.get("notify_on_own_pr_ci_failure") is True,
        "workspace_path": workspace_path,
        # Monotonic per-repo counter, bumped by every write. A full-document PUT
        # carries the revision it read, so a write built on a snapshot that has
        # since moved is REFUSED instead of silently discarding the newer change
        # (see SettingsConflict). Absent in pre-revision configs -> 0.
        "revision": max(0, revision),
    }


def read_repo_settings(
    owner: str,
    repo: str,
    root: Path | None = None,
    *,
    provider: str = "github",
    host: str = "github.com",
) -> dict[str, Any]:
    """Return the normalized triage settings for a repo (defaults if unset)."""
    for r in read_config(root).get("repos", []):
        if _same_repo(r, owner, repo, provider=provider, host=host):
            return _normalize_settings(r.get("settings"))
    return dict(DEFAULT_REPO_SETTINGS)


class SettingsConflict(Exception):
    """A full-document settings write was built on a stale snapshot.

    The PUT replaces every field, so a client that read revision N and writes
    while the stored revision is N+1 would silently discard whatever produced
    N+1 — typically a label appended by ``add_setting_label`` from another tab.
    Carries the current settings so the caller can re-read and retry."""

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("settings changed since they were read")
        self.current = current


# Named by the path callers import it from: a traceback renders a raised class as
# ``module.qualname``, and the store facade, not this owner, is that path.
SettingsConflict.__module__ = "kiro_crew.apps.builtins.issue_radar.backend.store"


def write_repo_settings(
    owner: str,
    repo: str,
    settings: dict[str, Any],
    *,
    expected_revision: int | None = None,
    root: Path | None = None,
    provider: str = "github",
    host: str = "github.com",
) -> dict[str, Any]:
    """Persist (after normalizing) a repo's triage settings into its config entry.

    Raises ``KeyError`` if the repo is not connected. When ``expected_revision``
    is given and does not match what is stored, raises :class:`SettingsConflict`
    rather than overwriting — this is what stops a stale tab from erasing a label
    appended meanwhile. Turning ``notify_on_own_pr_ci_failure`` on clears the
    repo's CI-watch record, so the next read seeds it again and the red runs
    left from while it was off are not announced. Returns the normalized object
    that was stored, with its revision bumped."""
    normalized = _normalize_settings(settings)
    with _config_lock(root):
        config = read_config(root)
        for r in config.get("repos", []):
            if _same_repo(r, owner, repo, provider=provider, host=host):
                current = _normalize_settings(r.get("settings"))
                if expected_revision is not None and expected_revision != current["revision"]:
                    raise SettingsConflict(current)
                normalized["revision"] = current["revision"] + 1
                r["settings"] = normalized
                write_config(config, root)
                key = "notify_on_own_pr_ci_failure"
                if normalized[key] and not current[key]:
                    scope = provider_root(root=root, provider=provider, host=host)
                    try:
                        reset_ci_watch_state(owner, repo, scope)
                    except OSError:
                        logger.warning("could not reset the CI-watch record for %s/%s", owner, repo)
                return normalized
        raise KeyError(f"{owner}/{repo} is not connected")


_SETTINGS_LABEL_ROLES = ("triage_labels", "good_first_issue_labels")


def add_setting_label(
    owner: str,
    repo: str,
    role: str,
    label: str,
    *,
    root: Path | None = None,
    provider: str = "github",
    host: str = "github.com",
) -> dict[str, Any]:
    """Append ONE label to a repo's triage-label role, under the config lock.

    This exists because the settings PUT replaces the whole document, so a client
    doing read-modify-write can only serialize itself. Two dashboard tabs — or a
    tab and an API client — each read the same settings and issue competing full
    replacements, and the later write permanently drops the other's label. Doing
    the append here makes the read and the write one critical section for every
    caller, which no amount of client-side chaining can achieve.

    Idempotent: appending a label the role already carries is a no-op. Raises
    ``KeyError`` if the repo is not connected, ``ValueError`` on an unknown role.
    Returns the repo's full normalized settings after the append.
    """
    if role not in _SETTINGS_LABEL_ROLES:
        raise ValueError(f"unknown settings role: {role}")
    with _config_lock(root):
        config = read_config(root)
        for r in config.get("repos", []):
            if _same_repo(r, owner, repo, provider=provider, host=host):
                current = _normalize_settings(r.get("settings"))
                if label not in current[role]:
                    current[role] = [*current[role], label]
                    # Bump so a PUT built on the pre-append snapshot is refused
                    # rather than silently dropping this label.
                    current["revision"] = current["revision"] + 1
                r["settings"] = current
                write_config(config, root)
                return current
        raise KeyError(f"{owner}/{repo} is not connected")


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


# ── CI-failure watch state (own-PR workflow-run alerts) ────────────────────────
#
# For a repo with ``notify_on_own_pr_ci_failure`` on, the watcher records which
# workflow-run attempts it has already reported, as ``{run_id: attempt}``. A run
# notifies when its failed attempt is higher than the recorded one, so a re-run
# that fails again notifies again and a repeated read of the same failure does
# not. ``seeded`` is false until the first successful read, which records the
# failures already present WITHOUT notifying, so turning the setting on does not
# announce every red run already sitting on the user's open PRs.
#
# A separate file from ``watch-state.json``: that file is rewritten whole by the
# new-issue pass, and the two passes are independent opt-ins.

#: Most run ids remembered per repo. The watcher keeps only the failed runs on
#: the user's open PR heads, and skips a repo whose live failures exceed this
#: rather than forgetting some of them; the slice below is a last-resort bound.
CI_WATCH_MAX_RUNS = 500


def ci_watch_state_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / "ci-watch-state.json"


def read_ci_watch_state(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    """Return ``{"seeded": bool, "notified": {run_id: attempt}}`` for a repo.

    An absent, unreadable or malformed file reads as unseeded with nothing
    notified, so the next successful read seeds instead of announcing.
    """
    path = ci_watch_state_path(owner, repo, root)
    data: Any = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    raw = data.get("notified")
    notified: dict[str, int] = {}
    if isinstance(raw, dict):
        for run_id, attempt in raw.items():
            if (
                isinstance(run_id, str)
                and isinstance(attempt, int)
                and not isinstance(attempt, bool)
            ):
                notified[run_id] = attempt
    return {"seeded": data.get("seeded") is True, "notified": notified}


def write_ci_watch_state(
    owner: str,
    repo: str,
    notified: dict[str, int],
    root: Path | None = None,
    *,
    generation: int | None = None,
) -> bool:
    """Persist the reported run attempts for a repo and mark it seeded.

    Keeps only the newest :data:`CI_WATCH_MAX_RUNS` entries (insertion order).
    With ``generation``, writes only while the repo's opt-in generation still
    equals it (see :func:`reset_ci_watch_state`) and returns whether it wrote.
    """
    items = list(notified.items())[-CI_WATCH_MAX_RUNS:]
    with _ci_watch_lock:
        if generation is not None and generation != ci_watch_generation(owner, repo, root):
            return False
        atomic_write(
            ci_watch_state_path(owner, repo, root),
            json.dumps(
                {"owner": owner, "repo": repo, "seeded": True, "notified": dict(items)},
                indent=2,
            ),
        )
    return True


# In-process opt-in generation per repo record. Turning the setting on bumps it
# and deletes the record under ``_ci_watch_lock``; a watcher pass reads it with
# the record under the same lock, and neither notifies nor writes once it has
# moved, so a pass that started before the reset cannot restore the old record.
_ci_watch_lock = threading.Lock()
_ci_watch_generations: dict[tuple[str, str, str], int] = {}


def ci_watch_generation(owner: str, repo: str, root: Path | None = None) -> int:
    """The repo's current opt-in generation. Pure in-memory read, no I/O."""
    return _ci_watch_generations.get((str(root), owner.lower(), repo.lower()), 0)


def reset_ci_watch_state(owner: str, repo: str, root: Path | None = None) -> None:
    """Forget a repo's reported attempts, so the next read seeds it again."""
    with _ci_watch_lock:
        key = (str(root), owner.lower(), repo.lower())
        _ci_watch_generations[key] = _ci_watch_generations.get(key, 0) + 1
        ci_watch_state_path(owner, repo, root).unlink(missing_ok=True)


def read_ci_watch_record(
    owner: str, repo: str, root: Path | None = None
) -> tuple[dict[str, Any], int]:
    """The repo's record and the opt-in generation it belongs to, read together.

    Taken under the same lock as :func:`reset_ci_watch_state`, so a generation
    is never paired with the record a reset was about to delete.
    """
    with _ci_watch_lock:
        return read_ci_watch_state(owner, repo, root), ci_watch_generation(owner, repo, root)


def remove_connected_repo(
    owner: str,
    repo: str,
    *,
    root: Path | None = None,
    provider: str = "github",
    host: str = "github.com",
) -> bool:
    """Disconnect a repo: drop it from config.json and delete its local cache
    dir. Local-only — nothing on the provider is touched. Returns True if a repo
    was removed, False if it was not connected.

    The cache dir is resolved through :func:`provider_root`, so disconnecting a
    GitLab project deletes ITS subtree and can never rmtree a same-named GitHub
    repo's data.
    """
    with _config_lock(root):
        config = read_config(root)
        repos = config.get("repos", [])
        kept = [r for r in repos if not _same_repo(r, owner, repo, provider=provider, host=host)]
        if len(kept) == len(repos):
            return False
        config["repos"] = kept
        write_config(config, root)
    scope = provider_root(root=root, provider=provider, host=host)
    cache_dir = repo_data_dir(owner, repo, scope)
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
    # Clean up the now-empty owner dir if this was the last repo for that owner.
    owner_dir = cache_dir.parent
    if owner_dir.exists() and not any(owner_dir.iterdir()):
        owner_dir.rmdir()
    return True


# ── GitLab groups ───────────────────────────────────────────────────────────
#
# A GitLab group the user follows for review requests. Stored in config.json
# under ``gitlab_groups`` as ``{"host", "path"}`` and nothing else: no project
# list, no token, no per-group settings. The key is absent until the first group
# is added, so an install that never adds one keeps its config byte-for-byte.
# Paths match case-sensitively, the same fail-safe rule GitLab projects follow.

GITLAB_GROUPS_KEY = "gitlab_groups"


def list_gitlab_groups(root: Path | None = None) -> list[dict[str, str]]:
    """The added GitLab groups as ``[{"host", "path"}]`` (``[]`` if none)."""
    raw = read_config(root).get(GITLAB_GROUPS_KEY)
    if not isinstance(raw, list):
        return []
    groups: list[dict[str, str]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        host, path = row.get("host"), row.get("path")
        if isinstance(host, str) and host and isinstance(path, str) and path:
            groups.append({"host": host, "path": path})
    return groups


def is_gitlab_group_added(host: str, path: str, root: Path | None = None) -> bool:
    """Whether exactly this ``host`` + ``path`` group was added.

    The authorization gate for the group routes: they accept ``host`` and
    ``path`` from the query string and serve only a group the owner added.
    """
    return {"host": host, "path": path} in list_gitlab_groups(root)


def add_gitlab_group(host: str, path: str, root: Path | None = None) -> bool:
    """Add a group. Returns ``False`` when it was already added (idempotent)."""
    with _config_lock(root):
        config = read_config(root)
        groups = list_gitlab_groups(root)
        entry = {"host": host, "path": path}
        if entry in groups:
            return False
        config[GITLAB_GROUPS_KEY] = [*groups, entry]
        write_config(config, root)
    return True


def remove_gitlab_group(host: str, path: str, root: Path | None = None) -> bool:
    """Remove a group. Returns ``False`` when it was not added.

    Removing the last group drops the ``gitlab_groups`` key, returning the config
    to exactly what it was before any group was added.
    """
    with _config_lock(root):
        config = read_config(root)
        groups = list_gitlab_groups(root)
        kept = [g for g in groups if g != {"host": host, "path": path}]
        if len(kept) == len(groups):
            return False
        if kept:
            config[GITLAB_GROUPS_KEY] = kept
        else:
            config.pop(GITLAB_GROUPS_KEY, None)
        write_config(config, root)
    return True
