"""Listing and removal for git worktrees of a session's own repository.

Two endpoints over :mod:`kiro_crew.worktree.service`:

* ``GET  /api/worktree/list?repo=<path>`` — every worktree registered against the
  repo, with branch, dirty state, commit counts and a recyclability verdict.
* ``POST /api/worktree/remove`` — remove one, refusing a dirty tree unless the
  caller passes ``force`` (which is what the UI's confirmation encodes).

Both apply the same barrier as worktree creation: ``repo`` must resolve inside a
directory some existing chat slot is scoped to, and the git toplevel it resolves
to is re-checked against that same allow-list, so resolving upward out of a
granted subdirectory is refused. Every path operation downstream uses the
server-held value, never the request string.

Removal is destructive and irreversible, so it fails closed at every uncertainty:
an unregistered path, the main worktree, and an undeterminable dirty state are all
refusals rather than best-effort deletions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_handlers import deny_non_dashboard_caller
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.worktree.access import allowed_repo_roots, match_allowed_root
from kiro_crew.worktree.git_exec import (
    SANDBOX_REFUSAL,
    SandboxUnavailable,
    git_toplevel,
    repo_lock,
)
from kiro_crew.worktree.service import (
    CACHE_TTL_S,
    invalidate_cache,
    list_worktrees_cached,
    remove_worktree,
)

logger = logging.getLogger(__name__)


class _Refused(Exception):
    """Carries the response the barrier decided on, so callers can just return it."""

    def __init__(self, response: web.Response) -> None:
        super().__init__("refused")
        self.response = response


# Read live from config, so flipping the switch in Settings takes effect on the
# next request without a gateway restart.
WORKTREES_OFF = (
    "Worktree sessions are turned off. Enable them in Settings → Chat "
    "(Worktree Sessions), or use `git worktree` from a terminal."
)


def worktrees_enabled() -> bool:
    """Whether the beta worktree surface is switched on for this instance."""
    try:
        return bool(KiroCrewConfig.load().dashboard.worktrees_enabled)
    except Exception:  # pragma: no cover — a config read failure must not 500
        logger.warning("worktree flag read failed; treating as off", exc_info=True)
        return False


def deny_when_disabled(operation: str) -> web.Response | None:
    """403 when the feature is off, so the switch is a real off switch.

    Hiding the control would leave the endpoints reachable by anything that
    already knows the URL, which is not what a user turning a beta feature off
    is asking for.
    """
    if worktrees_enabled():
        return None
    logger.debug("%s refused: worktree sessions disabled", operation)
    return web.json_response(
        {"error": WORKTREES_OFF, "code": "worktrees_disabled"}, status=403
    )


async def _resolve_repo_root(request: web.Request, repo: str, *, operation: str) -> str:
    """Resolve ``repo`` to an allow-listed git toplevel, or raise :exc:`_Refused`.

    The allow-list comparison happens on strings BEFORE the submitted value
    touches the filesystem, and what comes back is the server-held slot project.
    ``allowed_repo_roots`` realpaths and stats every slot project, so a project on
    stalled network storage would block the event loop — and with it every session
    — for as long as the filesystem takes to answer. All of it runs on a worker
    thread, per the repo's no-blocking-calls-on-the-loop rule.
    """
    caller = str(request.get("user") or "dashboard")

    def _log_denial(error: str, resources: str) -> None:
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="denied",
            resources=resources,
            error=error,
        )

    roots = await asyncio.to_thread(allowed_repo_roots, request.app.get("state"))
    submitted = os.path.normpath(os.path.expanduser(repo))
    repo_root = match_allowed_root(submitted, roots)
    if repo_root is None:
        _log_denial("outside slot project directories", f"repo={submitted[:300]}")
        raise _Refused(
            web.json_response(
                {"error": "repo must be a project directory of an existing session.",
                 "code": "repo_not_allowed"},
                status=403,
            )
        )
    if not await asyncio.to_thread(os.path.isdir, repo_root):
        raise _Refused(
            web.json_response(
                {"error": "repo is not a directory", "code": "repo_not_a_directory"},
                status=400,
            )
        )
    if await asyncio.to_thread(is_sensitive_path, repo_root):
        _log_denial("sensitive path", f"repo={repo_root}")
        raise _Refused(
            web.json_response(
                {"error": "Access denied", "code": "repo_sensitive_path"},
                status=403,
            )
        )

    try:
        root = await asyncio.to_thread(git_toplevel, repo_root)
    except SandboxUnavailable as exc:
        # Fail CLOSED: no OS isolation available, so the spawn does not happen.
        logger.warning("%s: sandbox unavailable: %s", operation, exc)
        _log_denial("sandbox backend unavailable", f"repo={repo_root}")
        raise _Refused(
            web.json_response(
                {"error": SANDBOX_REFUSAL, "code": "sandbox_unavailable"},
                status=503,
            )
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("%s: git toplevel probe failed: %s", operation, exc)
        raise _Refused(
            web.json_response(
                {"error": "git is unavailable", "code": "git_unavailable"}, status=503
            )
        ) from exc
    if not root:
        raise _Refused(
            web.json_response(
                {"error": "Not a git repository", "code": "not_a_git_repository"}, status=400
            )
        )
    # Re-check the toplevel: resolving upward from an allowed subdirectory can
    # land on a repo root ABOVE every allowed root, which the match above never
    # saw. Without this, granting a nested directory would let git operate on an
    # ancestor the caller was never granted.
    if match_allowed_root(root, roots) is None:
        _log_denial("git toplevel outside slot project directories", f"root={root}")
        raise _Refused(
            web.json_response(
                {"error": "The repository root is outside this session's project directory.",
                 "code": "repo_root_not_allowed"},
                status=403,
            )
        )
    if await asyncio.to_thread(is_sensitive_path, root):
        _log_denial("sensitive path", f"root={root}")
        raise _Refused(
            web.json_response(
                {"error": "Access denied", "code": "root_sensitive_path"},
                status=403,
            )
        )
    return root


def _body_repo(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    repo = body.get("repo")
    return repo.strip() if isinstance(repo, str) and repo.strip() else None


async def api_worktree_list(request: web.Request) -> web.Response:
    """GET ``/api/worktree/list?repo=<path>[&size=1]``.

    ``size=1`` walks each tree to total its bytes, which is O(files) — opt-in so
    the common poll stays cheap.
    """
    caller = str(request.get("user") or "dashboard")
    denied = deny_non_dashboard_caller(request, "worktree_list")
    if denied is not None:
        return denied
    off = deny_when_disabled("worktree_list")
    if off is not None:
        return off
    repo = (request.query.get("repo") or "").strip()
    if not repo:
        return web.json_response(
            {"error": "repo is required", "code": "repo_required"}, status=400
        )
    try:
        root = await _resolve_repo_root(request, repo, operation="worktree_list")
    except _Refused as refused:
        return refused.response

    with_size = request.query.get("size") == "1"
    fresh = request.query.get("fresh") == "1"
    try:
        result = await list_worktrees_cached(
            root, with_size=with_size, ttl=0.0 if fresh else CACHE_TTL_S
        )
        # Totalled from the same walk the rows used; a second pass over every tree
        # would double the cost of the one expensive option this endpoint has.
        disk = (
            sum(w.size_bytes for w in result.worktrees if not w.is_main)
            if with_size
            else 0
        )
    except SandboxUnavailable as exc:
        logger.warning("worktree_list: sandbox unavailable: %s", exc)
        return web.json_response(
            {"error": SANDBOX_REFUSAL, "code": "sandbox_unavailable"}, status=503
        )
    except subprocess.TimeoutExpired:
        return web.json_response(
            {"error": "git timed out", "code": "git_timeout"}, status=504
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("worktree_list failed: %s", exc)
        return web.json_response(
            {"error": "could not list worktrees", "code": "list_failed"}, status=500
        )

    payload = result.to_dict()
    payload["disk_bytes"] = disk
    # Every refusal above is audited; the allowed path has to be too, or the log
    # records only the requests that were stopped and a reviewer cannot tell an
    # untouched repository from one this caller enumerated.
    sel().log_api_access(
        caller=caller,
        operation="worktree_list",
        outcome="allowed",
        resources=f"root={root} trees={len(result.worktrees)}",
    )
    return web.json_response(payload)


async def api_worktree_remove(request: web.Request) -> web.Response:
    """POST ``/api/worktree/remove`` with ``{repo, path, force?}``.

    A dirty tree is refused with 409 unless ``force`` is true. That is the whole
    safety model: uncommitted work exists only inside that directory, so the
    decision to destroy it belongs to the user, and the flag is the record that
    they made it.

    Only a JSON ``true`` counts. Truthiness would read ``"false"`` — a plausible
    thing for a client to send — as consent to delete uncommitted work, so the
    check is identity against ``True`` and every other value refuses.
    """
    caller = str(request.get("user") or "dashboard")
    denied = deny_non_dashboard_caller(request, "worktree_remove")
    if denied is not None:
        return denied
    off = deny_when_disabled("worktree_remove")
    if off is not None:
        return off
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    repo = _body_repo(body)
    path = body.get("path") if isinstance(body, dict) else None
    if not repo or not isinstance(path, str) or not path.strip():
        return web.json_response(
            {"error": "repo and path are required", "code": "repo_and_path_required"}, status=400
        )
    force = isinstance(body, dict) and body.get("force") is True

    try:
        root = await _resolve_repo_root(request, repo, operation="worktree_remove")
    except _Refused as refused:
        return refused.response

    # The worktree path itself is NOT taken on trust either: `remove_worktree`
    # only acts on a path git lists against this repository, and the allow-list
    # already bounds the repository.
    try:
        async with repo_lock(root):
            payload, status = await asyncio.to_thread(
                remove_worktree, root, path.strip(), force=force
            )
    except SandboxUnavailable as exc:
        logger.warning("worktree_remove: sandbox unavailable: %s", exc)
        return web.json_response(
            {"error": SANDBOX_REFUSAL, "code": "sandbox_unavailable"}, status=503
        )
    except subprocess.TimeoutExpired:
        return web.json_response(
            {"error": "git timed out", "code": "git_timeout"}, status=504
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("worktree_remove failed: %s", exc)
        return web.json_response(
            {"error": "worktree removal failed", "code": "remove_failed"}, status=500
        )

    # The cached listing predates this mutation, so the next read must re-measure.
    invalidate_cache(root)
    sel().log_api_access(
        caller=caller,
        operation="worktree_remove",
        outcome="allowed" if status == 200 else "error",
        resources=f"root={root} path={path.strip()[:300]} force={force}",
        error="" if status == 200 else str(payload.get("error", "")),
    )
    if status == 200:
        logger.info("Removed worktree %s from %s", payload.get("path"), root)
        return web.json_response(payload, status=200)
    # Error responses from the service always carry "error" and "code"; route by
    # status so the AST-level error-code contract detector sees literal statuses.
    error_msg = payload.get("error", "")
    error_code = payload.get("code", "unknown")
    if status == 404:
        return web.json_response({"error": error_msg, "code": error_code}, status=404)
    if status == 409:
        return web.json_response(
            {"error": error_msg, "code": error_code,
             "dirty": payload.get("dirty"), "verdict": payload.get("verdict")},
            status=409,
        )
    return web.json_response({"error": error_msg, "code": error_code}, status=400)
