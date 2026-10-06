"""Branch listing and switching for the dashboard Git panel.

Two endpoints back the panel's branch switcher:

* ``GET /api/project/git/branches?path=<dir>`` lists the repository's local
  branches (most recently committed first) and the remote-tracking branches that
  have no local counterpart, plus which one is checked out.
* ``POST /api/project/git/switch`` with ``{path, branch, create?, track?}``
  checks out an existing local branch, creates a new branch at ``HEAD``
  (``create: true``), or creates a local branch tracking a remote one
  (``track: "<remote>/<name>"``).

Trust model
-----------
This is the same boundary ``POST /api/worktree/create`` draws for the one other
dashboard route that changes a repository, and it reuses that module's
machinery rather than restating it:

* ``path`` must name, or sit inside, a directory an existing chat slot is scoped
  to, and so must the git toplevel it resolves to (a subdirectory grant cannot
  reach the parent repository). Only the server-held root is used as a path.
* git runs through :func:`kiro_crew.dashboard.handlers.worktree._run_git`: the
  ``sandboxed_spawn_argv`` chokepoint in strict mode, an argv list with no shell,
  resource limits, a timeout, ``core.hooksPath`` pointed at ``os.devnull`` (no
  ``post-checkout`` hook runs) and ``core.fsmonitor`` off.
* A checkout runs ``filter.<name>.smudge``/``.process`` drivers, which ``-c``
  cannot disable by name, so a repository whose own config declares one is
  refused before the switch (:func:`~kiro_crew.dashboard.handlers.worktree._checkout_filter`).
  The listing still answers there, carrying ``switchBlocked: "filter"`` so the
  panel can say why switching is off instead of failing on click.
* The switch rewrites files in the working tree, so it takes the same OWNER gate
  as ``/api/file-write``; listing takes the dashboard-caller gate the worktree
  route uses.
* Branch names come back through ``redact`` like every other repository string
  the dashboard renders. A name the redactor alters cannot be switched to by
  echoing it back, so it is marked ``switchable: false`` rather than offered as a
  row that fails on click.

``git switch`` itself refuses a switch that would overwrite uncommitted or
untracked changes, so the working tree is never discarded; that refusal is
reported as ``409 git_switch_dirty``. Non-conflicting local changes carry over
to the new branch, as they do on the command line.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess

from aiohttp import web

from kiro_crew.dashboard.chat_handlers import deny_non_dashboard_caller
from kiro_crew.dashboard.handlers.files import _is_not_a_repo_verdict, _run_git_bounded
from kiro_crew.dashboard.handlers._shared import read_bounded_json, require_owner_dashboard_request
from kiro_crew.dashboard.handlers.worktree import (
    SandboxUnavailable,
    _allowed_repo_roots,
    _checkout_filter,
    _git_no_repo_code,
    _match_allowed_root,
    _repo_lock,
    _resolve_commit,
    _run_git,
)
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.validation import is_valid_followup_branch

logger = logging.getLogger(__name__)

#: Rows per section. A repository can carry thousands of branches; the panel is
#: a picker, not an inventory, and the list is most-recent-first, so the cap
#: drops the stalest rows and the response says so with ``truncated``.
MAX_BRANCH_ROWS = 200

#: Longest branch name the switch route accepts. Git's own limit is the
#: filesystem's (a loose ref is a file), so this only bounds the request.
MAX_BRANCH_NAME = 200

#: Longest commit subject / author name a row keeps. Applied after redaction,
#: so a cut never leaves part of a secret readable.
MAX_ROW_TEXT = 300

#: Bytes of ``for-each-ref`` output one listing may capture. 200 rows of
#: ordinary refs fit with room to spare; a repository whose refs point at
#: commits with enormous subject lines overflows it, and the listing answers
#: 503 rather than buffering them.
MAX_LIST_OUTPUT = 2 * 1024 * 1024

#: git's diagnostics are matched as English text (``_SWITCH_FAILURES``, the
#: not-a-repository verdict), so every git call here runs in the C locale.
_C_LOCALE = {"LC_ALL": "C", "LANGUAGE": "C"}

_SEP = "\x1f"
_FORMAT = _SEP.join(
    (
        "%(refname)",
        "%(objectname:short)",
        "%(committerdate:iso-strict)",
        "%(authorname)",
        "%(contents:subject)",
        "%(upstream:short)",
        "%(upstream:track,nobracket)",
        "%(HEAD)",
    )
)
# for-each-ref interpolates %xx hex escapes, so the separator is passed as one.
_FORMAT_ARG = _FORMAT.replace(_SEP, "%1f")

_TRACK_RE = re.compile(r"(ahead|behind) (\d+)")

_SANDBOX_REFUSAL = (
    "This host has no OS sandbox backend, so Kiro Crew will not run git for you. "
    "Switch branches from a terminal."
)


class _Refusal(Exception):
    """A request that resolves to a JSON error body instead of a repository."""

    def __init__(self, body: dict, status: int) -> None:
        super().__init__(body.get("error", ""))
        self.body = body
        self.status = status


def _error(message: str, code: str, status: int) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


async def _resolve_repo_root(request: web.Request, raw: object, operation: str) -> str:
    """Map the submitted project path onto a server-held git toplevel.

    Raises :class:`_Refusal` for every outcome other than a usable root. Mirrors
    ``api_worktree_create``'s order: allow-list on the normalized string first,
    then the filesystem checks on the server-held value, then the toplevel
    re-check so resolving upward cannot leave the granted tree.
    """
    caller = str(request.get("user") or "dashboard")
    if not isinstance(raw, str) or not raw.strip():
        raise _Refusal({"error": "path required", "code": "path_required"}, 400)
    roots = await asyncio.to_thread(_allowed_repo_roots, request.app.get("state"))
    submitted = os.path.normpath(os.path.expanduser(raw.strip()))
    project = _match_allowed_root(submitted, roots)
    if project is None:
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="denied",
            resources=f"path={submitted[:300]}",
            error="outside slot project directories",
        )
        raise _Refusal({"error": "Unknown project directory", "code": "unknown_project_dir"}, 403)
    if await asyncio.to_thread(is_sensitive_path, project):
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="denied",
            resources=f"path={project}",
            error="sensitive path",
        )
        raise _Refusal({"error": "Access denied", "code": "access_denied"}, 403)
    if not await asyncio.to_thread(os.path.isdir, project):
        raise _Refusal({"error": "Not a git repository", "code": "not_a_repository"}, 404)
    try:
        root = await asyncio.to_thread(_toplevel, project)
    except SandboxUnavailable as exc:
        logger.warning("%s: sandbox unavailable: %s", operation, exc)
        raise _Refusal({"error": _SANDBOX_REFUSAL, "code": "git_sandbox_unavailable"}, 503) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("%s: git toplevel probe failed: %s", operation, exc)
        raise _Refusal({"error": "git is unavailable", "code": "git_unavailable"}, 503) from exc
    if root is None:
        raise _Refusal({"error": "Not a git repository", "code": "not_a_repository"}, 404)
    if not root:
        # git ran and refused for another reason (dubious ownership, a corrupt
        # .git): an outage, not proof the directory holds no repository.
        raise _Refusal(
            {
                "error": "Couldn't read the repository's branches.",
                "code": "git_branches_unavailable",
            },
            503,
        )
    if _match_allowed_root(root, roots) is None or await asyncio.to_thread(is_sensitive_path, root):
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="denied",
            resources=f"root={root}",
            error="git toplevel outside slot project directories",
        )
        raise _Refusal(
            {
                "error": (
                    "The repository root is outside this session's project directory, "
                    "so branches can't be switched from here."
                ),
                "code": "repo_root_outside_project",
            },
            403,
        )
    return root


def _toplevel(project: str) -> str | None:
    """Work-tree root of ``project``; None when git says it is no repository.

    Returns ``""`` for any other failed probe, which the caller reports as an
    outage rather than an absence.
    """
    probe = _run_git(["rev-parse", "--show-toplevel"], project, env_overrides=_C_LOCALE)
    if probe.returncode != 0:
        return None if _is_not_a_repo_verdict(probe.stderr or "") else ""
    top = probe.stdout.strip()
    return os.path.realpath(top) if top else ""


def _parse_track(track: str) -> tuple[int, int]:
    ahead = behind = 0
    for kind, count in _TRACK_RE.findall(track):
        if kind == "ahead":
            ahead = int(count)
        else:
            behind = int(count)
    return ahead, behind


def _list_refs(root: str, namespace: str) -> tuple[list[dict], bool] | None:
    """Rows for ``refs/heads`` or ``refs/remotes``, newest first; None on failure."""
    limit = MAX_BRANCH_ROWS + 1
    rc, stdout, overflow = _run_git_bounded(
        [
            "git",
            *_git_no_repo_code(),
            "for-each-ref",
            "--sort=-committerdate",
            f"--count={limit}",
            f"--format={_FORMAT_ARG}",
            namespace,
        ],
        cwd=root,
        env={**os.environ, **_C_LOCALE},
        timeout=10,
        cap=MAX_LIST_OUTPUT,
    )
    if rc != 0 or overflow:
        return None
    prefix = namespace.rstrip("/") + "/"
    rows: list[dict] = []
    lines = stdout.splitlines()
    for line in lines:
        parts = line.split(_SEP)
        if len(parts) != 8:
            continue
        refname, sha, date, author, subject, upstream, track, head = parts
        if not refname.startswith(prefix):
            continue
        name = refname[len(prefix) :]
        # ``refs/remotes/<remote>/HEAD`` is a symref naming the remote's default
        # branch, not a branch of its own.
        if namespace == "refs/remotes" and name.endswith("/HEAD"):
            continue
        row: dict = {
            "name": name,
            "sha": sha,
            "date": date,
            "author": author,
            "subject": subject,
        }
        if namespace == "refs/heads":
            row["current"] = head == "*"
            if upstream:
                row["upstream"] = upstream
                ahead, behind = _parse_track(track)
                if ahead:
                    row["ahead"] = ahead
                if behind:
                    row["behind"] = behind
                if "gone" in track:
                    row["upstreamGone"] = True
        rows.append(row)
    # git stops at ``limit`` lines and a skipped ``<remote>/HEAD`` symref can use
    # one of them, so a capped answer may hide a row even at <= 200 rows.
    truncated = len(rows) > MAX_BRANCH_ROWS or len(lines) >= limit
    return rows[:MAX_BRANCH_ROWS], truncated


def _branches_sync(root: str) -> tuple[dict, int]:
    """Blocking half of the listing route. Returns ``(json_body, http_status)``."""
    current_proc = _run_git(["branch", "--show-current"], root, env_overrides=_C_LOCALE)
    if current_proc.returncode != 0:
        return (
            {
                "error": "Couldn't read the repository's branches.",
                "code": "git_branches_unavailable",
            },
            503,
        )
    current = (current_proc.stdout or "").strip()
    local = _list_refs(root, "refs/heads")
    remote = _list_refs(root, "refs/remotes")
    if local is None or remote is None:
        return (
            {
                "error": "Couldn't read the repository's branches.",
                "code": "git_branches_unavailable",
            },
            503,
        )
    local_rows, local_truncated = local
    remote_rows, remote_truncated = remote
    local_names = {r["name"] for r in local_rows}
    # A remote branch that already has a local branch of the same short name is
    # reached through that local row; listing both offers two rows that switch
    # to the same place.
    remote_rows = [r for r in remote_rows if r["name"].split("/", 1)[-1] not in local_names]
    body: dict = {
        "repo": True,
        "current": current or None,
        "local": local_rows,
        "remote": remote_rows,
    }
    if not current:
        head = _run_git(["rev-parse", "--short", "HEAD"], root, env_overrides=_C_LOCALE)
        if head.returncode == 0 and head.stdout.strip():
            body["detached"] = True
            body["head"] = head.stdout.strip()
    if local_truncated or remote_truncated:
        body["truncated"] = True
    if _checkout_filter(root):
        body["switchBlocked"] = "filter"
    return body, 200


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _redact_rows(rows: list[dict]) -> None:
    """Redact every display field, then bound it; redacting first means a cut
    never leaves part of a secret readable."""
    for row in rows:
        original = row["name"]
        row["name"] = _clip(redact(original), MAX_BRANCH_NAME)
        # A redacted or clipped name cannot be echoed back to /switch, so the
        # row is shown but not offered as a target.
        row["switchable"] = row["name"] == original
        row["author"] = _clip(redact(row["author"]), MAX_ROW_TEXT)
        row["subject"] = _clip(redact(row["subject"]), MAX_ROW_TEXT)
        if "upstream" in row:
            row["upstream"] = _clip(redact(row["upstream"]), MAX_BRANCH_NAME)


async def api_project_git_branches(request: web.Request) -> web.Response:
    """GET ``/api/project/git/branches?path=<dir>``.

    Response: ``{repo, current, detached?, head?, local: [...], remote: [...],
    truncated?, switchBlocked?}``. Each local row carries ``name, sha, date,
    author, subject, current, switchable`` and, when it tracks one,
    ``upstream, ahead?, behind?, upstreamGone?``; remote rows carry the same
    commit fields with a ``<remote>/<branch>`` name.
    """
    denied = deny_non_dashboard_caller(request, "project_git_branches")
    if denied is not None:
        return denied
    try:
        root = await _resolve_repo_root(
            request, request.query.get("path", ""), "project_git_branches"
        )
    except _Refusal as refusal:
        if refusal.body.get("code") == "not_a_repository":
            return web.json_response({"repo": False, "local": [], "remote": []})
        return _error(refusal.body["error"], refusal.body["code"], refusal.status)
    sel().log_api_access(
        caller=str(request.get("user") or "dashboard"),
        operation="project_git_branches",
        outcome="allowed",
        resources=root,
    )
    try:
        body, status = await asyncio.to_thread(_branches_sync, root)
    except SandboxUnavailable as exc:
        logger.warning("project_git_branches: sandbox unavailable: %s", exc)
        return _error(_SANDBOX_REFUSAL, "git_sandbox_unavailable", 503)
    except subprocess.TimeoutExpired:
        return _error("git timed out", "git_timeout", 504)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("project_git_branches failed: %s", exc)
        return _error("Couldn't read the repository's branches.", "git_branches_unavailable", 503)
    if status != 200:
        return _error(body["error"], body["code"], status)
    _redact_rows(body["local"])
    _redact_rows(body["remote"])
    if body.get("current"):
        body["current"] = redact(body["current"])
    if body.get("head"):
        body["head"] = redact(body["head"])
    return web.json_response(body)


def _is_plain_ref_name(name: str) -> bool:
    """Whether ``name`` can only be read by git as a ref name, never an option or shorthand.

    Excludes a leading ``-`` (an option, and ``-`` alone is "previous branch"),
    ``@{`` (reflog shorthand such as ``@{-1}``), and any whitespace or control
    character. Existence and git's own ref grammar are checked separately.
    """
    if not name or len(name) > MAX_BRANCH_NAME or name.startswith("-") or "@{" in name:
        return False
    return not any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name)


# (stderr fragment, code, http status, user-facing message). Matched in order
# against git's English diagnostics; anything else falls through to the generic
# failure with git's own first line as ``detail``.
_SWITCH_FAILURES: tuple[tuple[str, str, int, str], ...] = (
    (
        "would be overwritten by checkout",
        "git_switch_dirty",
        409,
        "Your uncommitted changes would be overwritten by this switch. Commit or stash them first.",
    ),
    (
        "untracked working tree files would be overwritten",
        "git_switch_dirty",
        409,
        "Untracked files would be overwritten by this switch. Move or commit them first.",
    ),
    (
        "already used by worktree",
        "git_branch_in_worktree",
        409,
        "That branch is already checked out in another worktree.",
    ),
    (
        "is already checked out at",
        "git_branch_in_worktree",
        409,
        "That branch is already checked out in another worktree.",
    ),
    ("already exists", "git_branch_exists", 409, "A branch with that name already exists."),
    (
        "you need to resolve your current index first",
        "git_operation_in_progress",
        409,
        "Finish or abort the merge, rebase or cherry-pick in progress first.",
    ),
    (
        "in the middle of",
        "git_operation_in_progress",
        409,
        "Finish or abort the merge, rebase or cherry-pick in progress first.",
    ),
    (
        "cannot switch branch while",
        "git_operation_in_progress",
        409,
        "Finish or abort the merge, rebase or cherry-pick in progress first.",
    ),
)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:300]
    return ""


def _switch_sync(root: str, branch: str, create: bool, track: str) -> tuple[dict, int]:
    """Blocking half of the switch route. Returns ``(json_body, http_status)``."""
    if _checkout_filter(root):
        return (
            {
                "error": (
                    "This repository's Git config declares a content filter that git "
                    "would run on checkout, so switching is off here. Switch from a terminal."
                ),
                "code": "git_switch_filter_refused",
            },
            409,
        )
    fmt = _run_git(["check-ref-format", f"refs/heads/{branch}"], root, env_overrides=_C_LOCALE)
    if fmt.returncode != 0:
        return ({"error": "Invalid branch name", "code": "invalid_branch"}, 400)

    previous_proc = _run_git(["branch", "--show-current"], root, env_overrides=_C_LOCALE)
    previous = (previous_proc.stdout or "").strip() if previous_proc.returncode == 0 else ""

    if track:
        if not _resolve_commit(root, f"refs/remotes/{track}"):
            return ({"error": "Remote branch not found", "code": "git_branch_not_found"}, 404)
        args = ["switch", "--no-overwrite-ignore", "-c", branch, "--track", f"refs/remotes/{track}"]
    elif create:
        if _resolve_commit(root, f"refs/heads/{branch}"):
            return (
                {"error": "A branch with that name already exists.", "code": "git_branch_exists"},
                409,
            )
        args = ["switch", "--no-overwrite-ignore", "--no-track", "-c", branch]
    else:
        if not _resolve_commit(root, f"refs/heads/{branch}"):
            return ({"error": "Branch not found", "code": "git_branch_not_found"}, 404)
        if branch == previous:
            return ({"ok": True, "branch": branch, "previous": previous}, 200)
        # git's default lets a switch silently replace an ignored local file
        # that the target branch tracks; refuse that like any other overwrite.
        args = ["switch", "--no-overwrite-ignore", "--no-guess", "--", branch]

    proc = _run_git(args, root, env_overrides=_C_LOCALE)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").lower()
        for fragment, code, status, message in _SWITCH_FAILURES:
            if fragment in stderr:
                return ({"error": message, "code": code}, status)
        return (
            {
                "error": "git couldn't switch branches.",
                "code": "git_switch_failed",
                "detail": _first_line(proc.stderr or proc.stdout or ""),
            },
            400,
        )
    return ({"ok": True, "branch": branch, "previous": previous or None}, 200)


async def api_project_git_switch(request: web.Request) -> web.Response:
    """POST ``/api/project/git/switch`` with ``{path, branch, create?, track?}``.

    ``create: true`` makes a new branch at ``HEAD``; ``track: "<remote>/<name>"``
    makes ``branch`` as a local branch tracking that remote branch; neither
    switches to an existing local branch. Returns ``{ok, branch, previous}``.
    """
    caller = str(request.get("user") or "dashboard")
    owner_denied = await require_owner_dashboard_request(request, "project_git_switch")
    if owner_denied is not None:
        return owner_denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None

    branch = body.get("branch")
    create = body.get("create", False)
    track = body.get("track", "")
    if not isinstance(branch, str) or not isinstance(create, bool) or not isinstance(track, str):
        return _error("invalid input", "invalid_input", 400)
    branch, track = branch.strip(), track.strip()
    if create and track:
        return _error("create and track are mutually exclusive", "invalid_input", 400)
    # A new name must satisfy the stricter grammar the follow-up card's branch
    # field uses; an existing one only has to be unambiguous as a ref name,
    # since it already exists and git accepted it once.
    name_ok = (
        is_valid_followup_branch(branch) and len(branch) <= MAX_BRANCH_NAME
        if (create or track)
        else _is_plain_ref_name(branch)
    )
    if not name_ok or (track and not _is_plain_ref_name(track)):
        sel().log_api_access(
            caller=caller,
            operation="project_git_switch",
            outcome="denied",
            resources=f"branch={branch[:120]}",
            error="invalid branch name",
        )
        return _error("Invalid branch name", "invalid_branch", 400)

    try:
        root = await _resolve_repo_root(request, body.get("path"), "project_git_switch")
    except _Refusal as refusal:
        return _error(refusal.body["error"], refusal.body["code"], refusal.status)

    resources = f"root={root} branch={branch}" + (f" track={track}" if track else "")
    try:
        async with _repo_lock(root):
            payload, status = await asyncio.to_thread(_switch_sync, root, branch, create, track)
    except subprocess.TimeoutExpired:
        sel().log_api_access(
            caller=caller,
            operation="project_git_switch",
            outcome="error",
            resources=resources,
            error="git timeout",
        )
        return _error("git timed out", "git_timeout", 504)
    except SandboxUnavailable as exc:
        logger.warning("project_git_switch: sandbox unavailable: %s", exc)
        sel().log_api_access(
            caller=caller,
            operation="project_git_switch",
            outcome="denied",
            resources=resources,
            error="sandbox backend unavailable",
        )
        return _error(_SANDBOX_REFUSAL, "git_sandbox_unavailable", 503)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("project_git_switch failed: %s", exc)
        return _error("git couldn't switch branches.", "git_switch_failed", 500)

    sel().log_api_access(
        caller=caller,
        operation="project_git_switch",
        outcome="allowed" if status == 200 else "error",
        resources=resources,
        error="" if status == 200 else str(payload.get("code", "")),
    )
    if status != 200:
        if payload.get("detail"):
            return web.json_response(
                {
                    "error": payload["error"],
                    "code": payload["code"],
                    "detail": redact(payload["detail"]),
                },
                status=status,
            )
        return _error(payload["error"], payload["code"], status)
    payload["branch"] = redact(payload["branch"])
    if payload.get("previous"):
        payload["previous"] = redact(payload["previous"])
    return web.json_response(payload)
