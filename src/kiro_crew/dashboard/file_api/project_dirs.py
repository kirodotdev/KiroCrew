"""Known project directories and the checked-out branch label: ``GET /api/project/git``."""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _GIT_ROOT_WALK_LIMIT,
        _HEAD_READ_LIMIT,
        _MACOS_TEMP_PROJECT_PREFIX_RE,
        DashboardState,
        _sel,
        config_dir,
        is_sensitive_path,
        platform_compat,
        redact,
        safe_read_prefix,
    )


def _read_git_meta_prefix(path: str) -> str | None:
    """Read a bounded prefix of a git metadata file through the hooks gate.

    ``.git`` and ``.git/HEAD`` are ordinary filesystem paths inside a directory
    the caller chose, so either can be a symlink pointing at something the
    gateway must never read — a secret whose first line happens to look like a
    ref, or a 40-64 char hex blob that would match the detached-HEAD shape.
    ``hooks.safe_read_prefix`` canonicalises via realpath, refuses sensitive
    resolved targets, and opens with ``O_NOFOLLOW`` as TOCTOU defence against a
    final-component swap. A refused or unreadable path returns ``None`` and the
    caller degrades to "no branch".
    """
    data = safe_read_prefix(path, _HEAD_READ_LIMIT)
    if data is None:
        return None
    return data.decode("utf-8", errors="replace").strip()


def _git_head_path(root: str) -> str | None:
    """Resolve the HEAD file for the repo at *root*.

    A linked worktree's ``.git`` is a FILE containing ``gitdir: <path>``, and that
    directory holds the worktree's own HEAD — so the pointer has to be followed
    rather than assuming ``<root>/.git`` is a directory.
    """
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return os.path.join(dot, "HEAD")
    pointer = _read_git_meta_prefix(dot)
    if pointer is None or not pointer.startswith("gitdir:"):
        return None
    gitdir = pointer.split(":", 1)[1].strip()
    if not gitdir:
        return None
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(root, gitdir)
    return os.path.join(gitdir, "HEAD")


def _slot_project_snapshot(state: DashboardState) -> list[str]:
    """Copy every live slot's project dir. MUST run on the event loop.

    Slots are created and deleted by other coroutines on the loop, so the copy
    has to happen where those mutations are serialised against it. Doing it in a
    worker thread would iterate a dict that the loop can mutate underneath.
    Pure in-memory, no I/O — safe to call inline.
    """
    dirs: list[str] = []
    for slot in list(getattr(state, "_slots", {}).values()):
        proj = getattr(slot, "project", "") or ""
        if proj:
            dirs.append(proj)
    return dirs


def _known_project_dirs(slot_projects: list[str]) -> list[str]:
    """Server-held project directories a branch lookup may be asked about.

    The caller's slot snapshot plus the recorded recent-projects list —
    directories the gateway itself set or the user already picked through the
    project picker. Nothing in the returned list comes from the current request.
    Reads a file, so this belongs in a worker thread.
    """
    dirs: list[str] = list(slot_projects)
    fp = config_dir() / "recent_projects.json"
    try:
        recent = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
    except (OSError, ValueError):
        recent = []
    if isinstance(recent, list):
        dirs.extend(d for d in recent if isinstance(d, str) and d)
    return dirs


def _match_known_project(raw: str, known: list[str]) -> str | None:
    """Map a request-supplied path onto the matching known project directory.

    Returns the SERVER-HELD string, never the caller's, so request data is only
    ever a comparison operand and never reaches a filesystem call. Matching is
    pure string normalisation (expanduser + normpath) with no filesystem access
    on the untrusted value — deliberately not realpath, which would stat a
    caller-controlled path and reintroduce the probe this guard removes.
    """
    want = os.path.normpath(os.path.expanduser(raw))
    for cand in known:
        if os.path.normpath(os.path.expanduser(cand)) == want:
            return cand
    return None


def _redact_project_path(path: str) -> str:
    """Redact a project path without treating Darwin's temp root as a secret.

    The generic bare-secret detector includes ``/`` because credentials may
    contain base64 characters.  A macOS per-user temp prefix can therefore look
    like one long high-entropy token even though its two variable components are
    OS-owned and fixed-width.  Withhold those OS-owned components from the scan;
    everything else still goes through the canonical redactor.

    BOUNDARY CONTRACT -- WHERE THE SPLIT GOES, AND WHY IT STOPS THERE.
    ``_contains_bare_secret`` slides an exactly-40-character window across a
    complete base64-alphabet run precisely so that adjacent bytes cannot make a
    credential invisible, and an AWS secret key may itself contain ``/``.  So a
    window is a real credential candidate whenever every one of its bytes is
    either fixed or user-controlled -- ``/T/`` followed by 37 user-controlled
    characters is a well-formed 40-byte key, not a window that merely "borrows
    OS bytes".  Two earlier splits were wrong for that reason: giving the scan
    only ``path[match.end():]`` dropped every window crossing the boundary, and
    giving it ``T`` + suffix still dropped the one starting at the ``/`` before
    it.  Each produced a class where ``redact(path) != path`` while this helper
    returned ``path`` unchanged -- the helper weakening the canonical output
    policy rather than narrowing a false positive.

    THE RESIDUAL OF THAT CLASS, MEASURED.  One such class survives this split, and
    it is not a weakening: a 40-char key carrying MORE than
    ``_SECRET_MAX_SLASHES`` separators.  Standalone, such a key is masked -- a
    40-char run is the token somebody wrote, so the separator ceiling is
    deliberately not applied to it.  Prepending ``/T`` makes the scanned run
    longer than one whole key, which is exactly the condition that switches the
    ceiling on, and every window is then declined.  Canonical on the whole path
    still masks it, but NOT by recognising the key: the id supplies a slash-free
    stretch that lets an id-straddling window clear the ceiling, i.e. it masks it
    by the very false positive this exemption exists to remove.  The control is
    that the same key also survives canonical on an ordinary deep path with no
    exemption anywhere near it (``/srv/<key>``, ``/tmp/<key>``, ``/<key>``), so
    the separator ceiling loses slash-dense keys everywhere in the product and
    this helper is no weaker than the treatment the same key already gets
    elsewhere.  Pinned by
    ``test_a_slash_dense_key_is_treated_as_on_any_other_path``.

    The split therefore sits at the END of the variable region, not one byte
    inside it.  The prefix regex ends in the literal ``/T``, and everything to
    the left of that literal is the OS-generated ``[a-z0-9]{2}/[a-z0-9_]{30}``
    id.  Scanning ``/T`` + suffix covers EVERY window composed entirely of fixed
    or user-controlled bytes, so there is no next byte to concede: a window
    reaching further left necessarily contains id bytes, which the OS generates
    and no caller can choose.  That is the terminating argument, and it is why
    this is not "one more byte" a third time.

    Withholding the id is the whole point of the exemption -- it is high-entropy
    and self-flagging, and letting it into the scan is the false positive this
    exemption exists to remove.
    Measured over ordinary project names, pytest temp-dir names, truncated
    sha-256 digests and uuid hex (300 samples each, under both a self-flagged and
    a non-self-flagged prefix): the two-byte boundary costs ZERO additional
    redactions, the same as the one-byte split it replaces.  The only names it
    newly redacts are uniformly-random base64 runs of 37-38 characters, which the
    canonical redactor already redacts on this path, so the boundary stays
    strictly narrower than canonical rather than becoming a second policy.
    DARWIN ONLY.  The withheld region is safe to withhold only because the OS
    generates it: off Darwin ``/private/var/folders/<id>/T`` names nothing the OS
    owns, so a caller who can choose a project directory can choose those bytes
    outright and place a credential inside the one region this helper never
    scans.  Everywhere but macOS the canonical redactor therefore decides alone,
    which also keeps the exemption exactly as wide as the false positive it was
    measured against.
    Regression-guarded by ``TestMacosPrefixBoundary``.
    """
    if not platform_compat.IS_MACOS:
        return redact(path)
    match = _MACOS_TEMP_PROJECT_PREFIX_RE.match(path)
    if match is None:
        return redact(path)
    prefix = match.group(0)
    # Split at the end of the OS-generated id: the trailing ``/T`` is fixed, so
    # it belongs to the scanned text. Preserving it instead would hide the
    # 40-char window that starts on that ``/``.
    return prefix[:-2] + redact(prefix[-2:] + path[match.end() :])


def _project_git_branch(base: str) -> dict:
    """Resolve the checked-out branch for ``base``.

    Returns ``{"repo": False}`` when ``base`` is not inside a git repository.
    For a repository, returns the repo root plus either a ``branch`` name or,
    on a detached HEAD, ``detached: True`` with the short commit in ``head``.
    """
    root: str | None = None
    cur = base
    for _ in range(_GIT_ROOT_WALK_LIMIT):
        # A worktree's .git is a FILE (a gitdir pointer), not a directory, so
        # probe for existence rather than is_dir() — otherwise every Kiro Crew
        # worktree reports as not-a-repo.
        if os.path.exists(os.path.join(cur, ".git")):
            root = cur
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if root is None:
        return {"repo": False}
    # ``root`` is derived from an allow-listed project directory, but a directory
    # NAME is itself agent-influenceable via set_project and this value is echoed
    # to the dashboard, so it goes through egress redaction. It is a path rather
    # than a label, so it uses the path-aware wrapper: a normal path is unchanged,
    # including a macOS temp root the bare detector would read as one secret.
    out: dict = {"repo": True, "repoRoot": _redact_project_path(root)}
    head_path = _git_head_path(root)
    if head_path is None:
        return out
    raw = _read_git_meta_prefix(head_path)
    if raw is None:
        # Unreadable, absent, or refused by the sensitive-path gate: still a
        # repo, just no label.
        return out
    if raw.startswith("ref:"):
        ref = raw[len("ref:") :].strip()
        prefix = "refs/heads/"
        if ref.startswith(prefix) and len(ref) > len(prefix):
            # Branch names are attacker/agent-controllable content that this route
            # renders in the dashboard AND makes copyable, so it goes through the
            # canonical egress redaction like any other echoed string. Ordinary
            # branch names are unchanged; one that embeds something matching a
            # credential pattern is masked rather than displayed.
            out["branch"] = redact(ref[len(prefix) :])
        return out
    # A bare object id in HEAD means detached (mid-rebase, bisect, explicit
    # --detach). Surface a short form so the caller shows something truthful
    # instead of an empty label. This is a fixed 7-char prefix rather than git's
    # dynamic uniqueness-based abbreviation — for a decorative label that is an
    # acceptable difference, and it needs no repository query.
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", raw):
        out["detached"] = True
        out["head"] = redact(raw[:7])
    return out


def _match_known_project_for(slot_projects: list[str], raw: str) -> str | None:
    """Build the allow-list and match *raw* against it. Worker-thread only.

    Takes an already-taken slot snapshot rather than the live state, so nothing
    here touches structures the event loop mutates. Both remaining halves must
    stay off the loop: reading the recent-projects file does I/O, and
    ``expanduser`` on a ``~user`` form does a passwd lookup, which can block on
    NSS/LDAP for an authenticated caller passing ``?path=~x/y``.
    """
    return _match_known_project(raw, _known_project_dirs(slot_projects))


def _resolve_project_git(project: str) -> tuple[str, str, dict]:
    """Vet *project* and read its branch. Runs entirely in a worker thread.

    Every filesystem touch for the request lives here: ``realpath``,
    the directory check, and ``is_sensitive_path`` all stat, so a project on a
    stalled network mount would block the event loop for the whole probe if any
    of them ran inline.

    Returns ``(status, base, info)`` with status ``"ok"``, ``"not_a_dir"``, or
    ``"sensitive"``; ``info`` is populated only for ``"ok"``.
    """
    base = os.path.realpath(os.path.expanduser(project))
    if not os.path.isdir(base):
        return "not_a_dir", base, {}
    if is_sensitive_path(base):
        return "sensitive", base, {}
    return "ok", base, _project_git_branch(base)


async def api_project_git(request: web.Request) -> web.Response:
    """GET /api/project/git?path=... — checked-out branch for a project dir.

    ``path`` is matched against the gateway's own set of known project
    directories and the matched server-held value is what gets stat'd, so a
    caller cannot make this route probe arbitrary filesystem paths for existence
    or git metadata. An unrecognised directory is refused outright.
    """
    state: DashboardState = request.app["state"]
    caller = request.get("user", "dashboard")
    raw = request.query.get("path", "").strip()
    if not raw:
        return web.json_response({"error": "path required"}, status=400)
    project = await asyncio.to_thread(_match_known_project_for, _slot_project_snapshot(state), raw)
    if project is None:
        _sel().log_api_access(
            caller=caller,
            operation="project_git",
            outcome="denied",
            resources=raw,
            error="not a known project directory",
        )
        return web.json_response({"error": "Unknown project directory"}, status=403)
    status, base, info = await asyncio.to_thread(_resolve_project_git, project)
    if status == "not_a_dir":
        # Redacted like every other echoed path: this arm is reachable whenever a
        # known project directory is deleted or replaced between the allow-list
        # match and the stat, so it is a live egress surface, not a dead branch.
        return web.json_response(
            {"error": "Not a directory", "path": _redact_project_path(base)}, status=400
        )
    if status == "sensitive":
        _sel().log_api_access(
            caller=caller,
            operation="project_git",
            outcome="denied",
            resources=base,
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied"}, status=403)
    _sel().log_api_access(caller=caller, operation="project_git", outcome="allowed", resources=base)
    # The SEL audit above records the real path; the response body is an egress
    # surface the dashboard renders, so the echoed path is redacted like the rest.
    return web.json_response({"path": _redact_project_path(base), **info})
