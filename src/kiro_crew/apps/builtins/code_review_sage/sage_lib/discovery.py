#!/usr/bin/env python3
"""Repo discovery — so the user PICKS a pull request instead of pasting its URL.

Two jobs:

1. **Which repos does this user actually work on?** Answered from the GitHub
   event feed for the authenticated ``gh`` login (``list_contributed_repos``),
   newest contribution first. That is the same signal Issue Radar's connect
   dialog uses, deliberately reimplemented here rather than called across the
   app boundary: every Issue Radar route is gated on that app being *enabled*, so
   cross-calling would make Code Review Sage's PR picker break whenever a
   neighbouring builtin is toggled off. Sage owns its own discovery.

2. **Which repos has the user pinned here?** A tiny app-local list
   (``data/repos.json``) so the picker opens on the repos they care about instead
   of re-deriving from the feed on every visit.

``gh`` resolution/validation is shared with the dashboard's PR panel
(``source_providers.provider_executable_candidates`` +
``_validate_provider_executable``) so Sage accepts exactly the same ``gh``
installs as every other surface and refuses a binary owned by another user, a
world-writable one, or one inside the agent-writable project tree.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from sage_lib import store
from sage_lib.store import redact_text as pipeline_redact

# Guarded top-level import, matching pipeline.py: this module is also imported on
# the standalone path, where `kiro_crew` is not importable. A bare module-level
# import would turn that into an ImportError at import time.
try:
    from kiro_crew.apps.registry import minimal_env
except ImportError:  # pragma: no cover - standalone fallback
    minimal_env = None  # type: ignore

GH_TIMEOUT_SEC = 45.0
CONTRIB_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 365
# One page only: this feeds a picker, not an audit. GitHub's own feed is capped
# (roughly the last 90 days / 300 events) so pagination buys little and costs a
# lot of API budget.
_EVENT_PAGE_SIZE = 100
# Same reasoning for the repo list: the picker is searchable, so the newest 100
# by push date covers the realistic cases without paginating a 500-repo org.
_REPO_PAGE_SIZE = 100
_REPO_JQ = (
    ".[] | {owner: .owner.login, repo: .name, full_name: .full_name, "
    "pushed_at: .pushed_at, private: .private, archived: .archived, "
    "can_push: .permissions.push}"
)
_EVENT_JQ = ".[] | {type: .type, repo: .repo.name, created_at: .created_at}"
# Event types that mean "this person did work here", as opposed to merely
# watching or forking the repo.
_CONTRIB_EVENT_TYPES = {
    "PushEvent",
    "PullRequestEvent",
    "PullRequestReviewEvent",
    "PullRequestReviewCommentEvent",
    "IssuesEvent",
    "IssueCommentEvent",
    "CommitCommentEvent",
    "CreateEvent",
}

# Serializes the read-modify-write of repos.json so two concurrent adds can't
# clobber each other (the write itself is atomic; this guards read+merge).
_REPOS_LOCK = threading.Lock()


class GhError(RuntimeError):
    """A ``gh`` invocation failed (transient: not authed, network, 404)."""


class GhSetupError(GhError):
    """``gh`` is missing or unusable on this host — a setup problem the user must
    fix, distinct from a transient API failure, so the UI can offer instructions
    instead of an error toast."""


_gh_bin_cache: str | None = None

# Env vars ``gh`` legitimately needs: its own auth/host config plus proxy and TLS
# settings. Everything else in the gateway's environment (AWS, Slack, SSH agent
# sockets, …) is withheld — a `gh` subprocess has no business seeing it. Mirrors
# Issue Radar's passthrough list so the two apps behave identically.
_GH_ENV_PASSTHROUGH = (
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST", "GH_CONFIG_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)


def gh_env() -> dict[str, str]:
    """A minimal environment for ``gh``: the platform's safe-key base
    (PATH/HOME/XDG/…) plus gh's own auth + network/TLS vars when set — never the
    gateway's full environment."""
    if minimal_env is None:  # pragma: no cover - standalone fallback
        raise RuntimeError("gh_env requires the Kiro Crew runtime")
    return minimal_env(**{k: os.environ[k] for k in _GH_ENV_PASSTHROUGH if k in os.environ})


def gh_bin() -> str:
    """Absolute path to an acceptable ``gh``, resolved once and cached.

    Set ``KIROCREW_SAGE_GH`` to an absolute path to override (still validated).
    Raises :class:`GhSetupError` when no acceptable executable is found."""
    global _gh_bin_cache
    if _gh_bin_cache:
        return _gh_bin_cache
    if sys.platform == "win32":
        raise GhSetupError(
            "Code Review Sage requires a POSIX platform (macOS/Linux); "
            "run the Kiro Crew gateway under WSL on Windows"
        )
    # Imported lazily: the owning module pulls in dashboard state, so a top-level
    # import here would be circular (same reason Issue Radar defers it).
    from kiro_crew.dashboard.handlers.source_providers import (
        _validate_provider_executable,
        provider_executable_candidates,
    )

    override = os.environ.get("KIROCREW_SAGE_GH")
    if override:
        try:
            validated = _validate_provider_executable(override)
        except (ValueError, OSError) as exc:
            raise GhSetupError(
                f"KIROCREW_SAGE_GH={override!r} failed validation: {exc}"
            ) from exc
        _gh_bin_cache = validated
        return validated

    last_err: Exception | None = None
    for candidate in provider_executable_candidates("gh"):
        try:
            validated = _validate_provider_executable(candidate)
        except (ValueError, OSError) as exc:
            last_err = exc
            continue
        _gh_bin_cache = validated
        return validated
    raise GhSetupError(
        "no usable `gh` CLI found — install GitHub CLI and run `gh auth login`"
        + (f" (last candidate rejected: {last_err})" if last_err else "")
    )


def run_gh_json(path: str, jq: str | None = None, *,
                timeout: float = GH_TIMEOUT_SEC,
                paginate: bool = False) -> list[dict]:
    """Run ``gh api <path>`` and parse the result into a list of dicts.

    ``path`` is an API path, never a shell string, and the argv is a LIST (no
    ``shell=True``). With ``jq`` the output is JSONL (one object per line); each
    unparseable line is skipped, but output that is entirely unparseable raises
    rather than masquerading as an empty result."""
    argv = [gh_bin(), "api", path]
    if paginate:
        argv.append("--paginate")
    if jq:
        argv += ["--jq", jq]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False, env=gh_env())
    except FileNotFoundError as exc:
        raise GhSetupError("the `gh` CLI is not installed on this host") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"`gh api {path}` timed out") from exc
    if proc.returncode != 0:
        tail = " ".join((proc.stderr or "").strip().splitlines()[-3:])
        if "auth login" in tail or "not logged" in tail.lower():
            raise GhSetupError(f"`gh` is not authenticated: {tail}")
        raise GhError(f"`gh api {path}` failed (exit {proc.returncode}): {tail}")
    text = (proc.stdout or "").strip()
    if not text:
        return []
    if not jq:
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GhError(f"could not parse `gh api {path}` output") from exc
        if isinstance(loaded, list):
            return [r for r in loaded if isinstance(r, dict)]
        return [loaded] if isinstance(loaded, dict) else []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    if not out:
        raise GhError(
            f"could not parse `gh api {path}` output "
            "(expected one JSON object per line)")
    return out


def current_login(*, timeout: float = GH_TIMEOUT_SEC) -> str | None:
    """The authenticated ``gh`` login, or None when it can't be determined.

    Runs ``gh api user --jq .login`` directly rather than through
    ``run_gh_json``: ``--jq .login`` emits a BARE STRING, which the JSONL dict
    parser cannot represent. Raises :class:`GhSetupError` when ``gh`` itself is
    unusable, because "no login" and "no gh" need different UI treatment."""
    argv = [gh_bin(), "api", "user", "--jq", ".login"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False, env=gh_env())
    except FileNotFoundError as exc:
        raise GhSetupError("the `gh` CLI is not installed on this host") from exc
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        tail = " ".join((proc.stderr or "").strip().splitlines()[-3:])
        if "auth login" in tail or "not logged" in tail.lower():
            raise GhSetupError(f"`gh` is not authenticated: {tail}")
        return None
    login = (proc.stdout or "").strip()
    return login or None


def list_user_repos(*, limit: int = _REPO_PAGE_SIZE,
                    timeout: float = GH_TIMEOUT_SEC) -> tuple[list[dict], bool]:
    """Repos the authenticated user can actually reach, newest push first.

    Complements :func:`list_contributed_repos`: the event feed only knows what
    you touched in the last ~90 days, so a repo you own but have not pushed to
    recently is invisible there. This asks GitHub directly for everything you own
    or collaborate on.

    Returns ``(rows, truncated)`` where a row is ``{owner, repo, full_name,
    pushed_at, private, archived, can_push}``. ``truncated`` is True when the page
    came back full, meaning repos beyond the newest ``limit`` were not listed —
    the picker must say so rather than implying the list is exhaustive, and a
    manual-entry field must always remain available for what it omits."""
    n = max(1, min(int(limit), _REPO_PAGE_SIZE))
    path = (
        f"user/repos?per_page={n}&sort=pushed&direction=desc"
        "&affiliation=owner,collaborator,organization_member"
    )
    rows = run_gh_json(path, _REPO_JQ, timeout=timeout, paginate=False)
    truncated = len(rows) >= n
    out: list[dict] = []
    for r in rows:
        owner = r.get("owner")
        repo = r.get("repo")
        if not isinstance(owner, str) or not isinstance(repo, str) or not owner or not repo:
            continue
        out.append({
            "owner": owner,
            "repo": repo,
            "full_name": r.get("full_name") or f"{owner}/{repo}",
            "pushed_at": r.get("pushed_at") or "",
            "private": bool(r.get("private")),
            "archived": bool(r.get("archived")),
            "can_push": bool(r.get("can_push")),
        })
    return out, truncated


def _parse_ts(value: object) -> datetime | None:
    """Parse a GitHub ISO-8601 UTC stamp to an aware datetime, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def list_contributed_repos(login: str, *, within_days: int = CONTRIB_WINDOW_DAYS,
                           timeout: float = GH_TIMEOUT_SEC) -> tuple[list[dict], bool]:
    """Repos ``login`` personally contributed to within ``within_days``.

    Returns ``(rows, truncated)`` where a row is ``{owner, repo, full_name,
    last_contributed_at, contribution_count}``, newest contribution first.
    ``truncated`` is True when the event page came back full — meaning older
    activity was not examined and the list may be MISSING repos. The UI must not
    present a truncated list as exhaustive; a picker that looks complete leads the
    user to conclude they never worked on a repo. ``within_days=0`` disables the
    window."""
    path = f"users/{quote(login, safe='')}/events?per_page={_EVENT_PAGE_SIZE}"
    events = run_gh_json(path, _EVENT_JQ, timeout=timeout, paginate=False)
    truncated = len(events) >= _EVENT_PAGE_SIZE

    days = max(0, min(int(within_days), MAX_WINDOW_DAYS))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None

    by_repo: dict[str, dict] = {}
    for ev in events:
        if ev.get("type") not in _CONTRIB_EVENT_TYPES:
            continue
        full_name = ev.get("repo")
        if not isinstance(full_name, str) or full_name.count("/") != 1:
            continue
        when = _parse_ts(ev.get("created_at"))
        if when is None or (cutoff is not None and when < cutoff):
            continue
        row = by_repo.get(full_name)
        if row is None:
            owner, _, repo = full_name.partition("/")
            by_repo[full_name] = {
                "owner": owner, "repo": repo, "full_name": full_name,
                "last_contributed_at": ev.get("created_at") or "",
                "contribution_count": 1, "_when": when,
            }
        else:
            row["contribution_count"] += 1
            # The feed is newest-first, but don't rely on it — keep the max.
            if when > row["_when"]:
                row["_when"] = when
                row["last_contributed_at"] = ev.get("created_at") or ""

    rows = sorted(by_repo.values(), key=lambda r: r["_when"], reverse=True)
    for r in rows:
        del r["_when"]
    return rows, truncated


# --- Pinned repos ------------------------------------------------------------

def repos_path(root: Path | None = None) -> Path:
    return store.data_dir(root) / "repos.json"


def read_repos(root: Path | None = None) -> list[dict]:
    """The user's pinned repos, newest-added first. ``[]`` when unset/unreadable.

    Read through the no-link guard and scrubbed on the way out, because this file
    sits in a directory a review worker can reach and `GET /repos` renders the
    result in the sidebar. Two distinct exposures, so two defences: a prompt-injected
    worker can plant a symlink at the path (the guard refuses to dereference it), and
    it can write a credential into a field of a legitimate file (redaction removes
    it). Values are read for display only, so scrubbing them changes nothing a caller
    depends on -- `owner`/`repo` are re-validated by the write path, and a real
    owner/repo never matches a credential shape.
    """
    within = store.data_dir(root)
    data = store.read_json_nolink(repos_path(root), within)
    if data is None:
        return []
    repos = data.get("repos")
    if not isinstance(repos, list):
        return []
    # `owner`/`repo` must be non-empty STRINGS, not merely truthy: a worker owns
    # this file, and a planted dict or list is truthy, survives redaction (which
    # walks strings), and reaches the client as an object React cannot render --
    # taking the page down rather than showing one bad row.
    return [_redact_repo(r) for r in repos
            if isinstance(r, dict)
            and isinstance(r.get("owner"), str) and r["owner"]
            and isinstance(r.get("repo"), str) and r["repo"]]


def _redact_repo(row: dict) -> dict:
    """Scrub every string in a pinned-repo row -- keys as well as values.

    A worker writing this file controls key names too, so a credential can ride in
    either half; the report path learned the same lesson one module over. Non-string
    scalars pass through, and nested containers are walked so a value that is a dict
    or list cannot smuggle a string past an `isinstance(v, str)` test.
    """
    return {_redact_any(k): _redact_any(v) for k, v in row.items()}


def _redact_any(value: object) -> object:
    if isinstance(value, str):
        return pipeline_redact(value) if value else value
    if isinstance(value, dict):
        return {_redact_any(k): _redact_any(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_any(v) for v in value]
    return value


def _write_repos(repos: list[dict], root: Path | None = None) -> Path:
    store.ensure_layout(root)
    path = repos_path(root)
    payload = json.dumps({"repos": repos}, indent=2).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def _same(a: dict, b_owner: str, b_repo: str) -> bool:
    """Repo identity is case-insensitive on GitHub, so compare that way."""
    return (str(a.get("owner", "")).lower() == b_owner.lower()
            and str(a.get("repo", "")).lower() == b_repo.lower())


def add_repo(owner: str, repo: str, root: Path | None = None) -> list[dict]:
    """Pin a repo (idempotent, most-recent first). Returns the new list."""
    with _REPOS_LOCK:
        repos = [r for r in read_repos(root) if not _same(r, owner, repo)]
        repos.insert(0, {
            "owner": owner, "repo": repo, "full_name": f"{owner}/{repo}",
            "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        _write_repos(repos, root)
        return repos


def remove_repo(owner: str, repo: str, root: Path | None = None) -> list[dict]:
    """Unpin a repo. Returns the new list."""
    with _REPOS_LOCK:
        repos = [r for r in read_repos(root) if not _same(r, owner, repo)]
        _write_repos(repos, root)
        return repos
