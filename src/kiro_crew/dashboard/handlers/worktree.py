"""Git worktree creation for the follow-up card's "Start in new worktree" action.

One endpoint, ``POST /api/worktree/create``, which creates a sibling worktree of
an existing local git repository on a new branch. The follow-up card calls it
before opening a new chat session scoped to the resulting directory.

Threat model. Both inputs are attacker-influenceable in the sense that matters
here: ``branch`` originates from an LLM (``suggest_followup``) and ``repo`` from
whatever the calling session's project happens to be. So:

* git is invoked with an **argv list and no shell** — there is no command string
  for a metacharacter to escape from — with a credential-scrubbed environment,
  the POSIX resource-limit ceiling, and a wall-clock timeout.
* ``repo`` must resolve inside a directory some existing chat slot is already
  scoped to (:func:`_allowed_repo_roots`). Without that barrier any
  authenticated dashboard caller could name an arbitrary host directory.
  Both the submitted path AND the git toplevel it resolves to are checked, so
  resolving upward out of an allowed subdirectory is refused.

Why the git spawn is sandbox-routed
-----------------------------------
:func:`_run_git` goes through the ``sandboxed_spawn_argv`` chokepoint (OS
isolation + credential-scrubbed env), matching ``git_coord.py``'s treatment of
agent-influenced git. A host with no sandbox backend and no explicit
``agent.sandbox_allow_unsandboxed_exec`` opt-in gets a 503 rather than an
unisolated spawn.

The repo-supplied-code guards sit ON TOP of that, because isolation bounds what
a hook can reach but does not stop it running:

* ``git worktree add`` would otherwise run the repo's ``post-checkout`` hook or
  an ``core.fsmonitor`` command; both are removed by the ``-c`` overrides in
  :func:`_git_no_repo_code`, which beat every config file. ``core.hooksPath``
  points at :data:`_HOOKS_SINK` (``os.devnull``) — a non-directory OS device, so
  there is no ``post-checkout`` to find and no directory anyone could plant one
  in.
* A ``filter.<name>.process``/``.smudge`` driver cannot be disabled generically
  (driver names are arbitrary), so a repo declaring one in EITHER repository
  config scope is refused instead (:func:`_checkout_filter`).

Concurrency
-----------
The branch is claimed atomically with ``update-ref <ref> <base> ""`` (empty old
value = "must not exist") BEFORE anything is created, so two concurrent requests
for the same branch are decided by git's ref lock and only the winner proceeds.
Cleanup removes only what a request can prove it created — the branch only if it
won the claim, the destination only if git registers it against that same branch
(or against nothing, under the per-repo lock). Same-repo requests are also
serialized in-process by :func:`_repo_lock`.

Other input protections
-----------------------
* ``branch`` must satisfy :func:`~kiro_crew.validation.is_valid_followup_branch`,
  which excludes a leading ``-`` (git would read it as a flag), ``..``, ``~``,
  ``^``, ``:``, ``?``, ``*``, ``[``, ``\\`` and whitespace.
* ``repo`` is realpath'd, must be a directory, must not be a sensitive path, and
  must resolve to a git work tree whose **toplevel** is used thereafter — a path
  pointing anywhere inside a repo cannot be used to make git operate on a parent
  it does not control.
* The destination is *derived* by this module (never supplied by the caller) and
  must not already exist, so the endpoint cannot be aimed at an existing tree.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess

from aiohttp import web

from kiro_crew.dashboard.chat_handlers import deny_non_dashboard_caller
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.validation import MAX_FOLLOWUP_BRANCH, is_valid_followup_branch
from kiro_crew.worktree.access import allowed_repo_roots as _allowed_repo_roots
from kiro_crew.worktree.access import match_allowed_root as _match_allowed_root
from kiro_crew.worktree.git_exec import SANDBOX_REFUSAL as _SANDBOX_REFUSAL
from kiro_crew.worktree.git_exec import SandboxUnavailable
from kiro_crew.worktree.git_exec import git_error as _git_error
from kiro_crew.worktree.git_exec import git_toplevel as _git_toplevel
from kiro_crew.worktree.git_exec import norm_path as _norm_path
from kiro_crew.worktree.git_exec import repo_lock as _repo_lock
from kiro_crew.worktree.git_exec import resolve_base_ref as _resolve_base_ref
from kiro_crew.worktree.git_exec import run_git as _run_git

logger = logging.getLogger(__name__)

# Characters kept when turning a branch name into a directory suffix.
_DIR_SLUG_STRIP_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Bound the derived directory suffix so a long branch name cannot push the
# resulting absolute path past the filesystem's component limit (255 on ext4).
_MAX_DIR_SLUG = 60

# Repo-local config keys that would hand git a program to run during checkout.
# `-c` cannot generically disable these (the driver name is arbitrary), so a repo
# declaring one is refused outright — see `_checkout_filter`.
_FILTER_KEY_RE = re.compile(r"^filter\.(?P<name>.+)\.(process|smudge|clean)$", re.IGNORECASE)

# Returned instead of a key name when a config scope could not be read at all.
# Treated as "refuse": an unreadable scope cannot be proven filter-free.
_FILTER_PROBE_FAILED = "unreadable git config"


def _dir_slug(branch: str) -> str:
    """Derive a filesystem-safe directory suffix from a branch name.

    Uses the last path segment ("feat/upload-limit" -> "upload-limit") so the
    sibling directory does not contain a slash, and strips anything outside
    ``[A-Za-z0-9._-]``. The branch has already been regex-gated by the caller;
    this is about path shape, not safety.
    """
    tail = branch.rstrip("/").split("/")[-1]
    slug = _DIR_SLUG_STRIP_RE.sub("-", tail).strip("-.") or "followup"
    return slug[:_MAX_DIR_SLUG]


def _worktree_branches(root: str) -> dict[str, str] | None:
    """Map ``normalized worktree path -> branch name`` for every registered tree.

    Returns ``None`` when the git query itself FAILS, which is deliberately
    distinct from an empty mapping: "git could not tell us" must never be read as
    "nothing is registered", because cleanup keys destructive decisions off this
    answer.

    Parsed from ``worktree list --porcelain``, whose per-tree block carries a
    ``branch refs/heads/<name>`` line (absent when detached, which maps to "").
    The path alone is not enough to identify a worktree for reuse: ``_dir_slug``
    keeps only a branch's LAST segment, so ``feat/foo`` and ``fix/foo`` derive the
    same destination. Matching on path only would hand back the wrong branch's
    worktree as "reused".
    """
    # `-z` because a worktree path may itself contain a newline: with the
    # line-oriented form such a path splits across records, never matches its
    # registered entry, and a retry 409s instead of reporting `reused`.
    # In `-z` mode git NUL-terminates every
    # attribute and emits an extra NUL between entries, so empty fields are
    # simply skipped.
    listing = _run_git(["worktree", "list", "--porcelain", "-z"], root)
    if listing.returncode != 0:
        return None
    trees: dict[str, str] = {}
    current = ""
    for field in listing.stdout.split("\0"):
        if not field:
            continue
        if field.startswith("worktree "):
            current = _norm_path(field[len("worktree ") :])
            trees[current] = ""
        elif field.startswith("branch ") and current:
            ref = field[len("branch ") :]
            trees[current] = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
    return trees


def _worktree_config_active(root: str) -> bool:
    """True when this repo has a *worktree-scoped* config file git will read.

    ``extensions.worktreeConfig=true`` makes git load ``$GIT_DIR/config.worktree``
    in addition to ``.git/config``. ``$GIT_DIR`` is **per worktree**: the common
    dir for the main worktree, but ``$GIT_COMMON_DIR/worktrees/<id>`` for a linked
    one — so ``--git-common-dir`` misses a linked worktree's own file entirely
    (verified: a filter declared there executed during checkout while the common
    dir had no ``config.worktree`` at all). ``--absolute-git-dir``
    resolves the right directory in both cases.

    Both conditions matter: without the extension git ignores the file, and with
    the extension but no file ``git config --worktree --list`` exits 128 ("unable
    to read config file") — so probing unconditionally would refuse every repo
    that merely enables the extension.
    """
    ext = _run_git(["config", "--bool", "--get", "extensions.worktreeConfig"], root)
    if ext.returncode != 0 or ext.stdout.strip() != "true":
        return False
    gitdir = _run_git(["rev-parse", "--absolute-git-dir"], root)
    path = gitdir.stdout.strip() if gitdir.returncode == 0 else ""
    if not path:
        # Cannot locate GIT_DIR: assume the scope is live, so the probe below
        # runs and any failure there fails closed.
        return True
    if not os.path.isabs(path):
        path = os.path.join(root, path)
    return os.path.isfile(os.path.join(path, "config.worktree"))


def _checkout_filter(root: str) -> str:
    """Name of a repo-supplied content filter git would run on checkout, else "".

    Defense in depth for the same class the ``-c`` overrides close. A
    ``.gitattributes`` entry can name a filter (``foo``) whose driver is defined
    in config as ``filter.foo.process`` / ``.smudge``; ``git worktree add``
    checks files out, so that driver would run. Filter DRIVERS can only come from
    a config file (never from ``.gitattributes``, and never from a remote — clone
    does not transfer config), so the repository-scoped sources are the two
    config scopes git reads from inside the repo: ``--local`` (``.git/config``)
    and, when :func:`_worktree_config_active`, ``--worktree``
    (``$GIT_DIR/config.worktree``, per-worktree). Probing only ``--local`` was a real
    hole: ``git config --local --name-only --list`` does NOT report
    worktree-scoped keys, so a repo with ``extensions.worktreeConfig=true`` and
    ``filter.evil.smudge`` in ``config.worktree`` passed the check and the driver
    executed during checkout (verified empirically).

    ``--includes`` is mandatory on both probes. For a *specific* scope query
    (``--local``/``--worktree``) git defaults include-following OFF, so a driver
    reached through ``include.path = hostile.cfg`` was invisible to the probe yet
    still resolved — and executed — during checkout (verified empirically).

    Rather than try to neutralize an unbounded set of ``filter.<name>.*`` keys
    with ``-c``, refuse the operation and tell the user to create the worktree
    themselves. A probe that fails (git error on a scope that is live) also
    refuses: we cannot prove the repo is filter-free, so we do not proceed.

    Global/system config is deliberately NOT probed: that is the user's own
    machine configuration (``git lfs install`` writes there), not something the
    repository supplies.
    """
    scopes = ["--local"]
    if _worktree_config_active(root):
        scopes.append("--worktree")
    for scope in scopes:
        proc = _run_git(["config", scope, "--includes", "--name-only", "--list"], root)
        if proc.returncode != 0:
            return _FILTER_PROBE_FAILED
        for key in proc.stdout.splitlines():
            key = key.strip()
            if _FILTER_KEY_RE.match(key):
                return key[:120]
    return ""


def _resolve_commit(root: str, ref: str) -> str:
    """Commit sha for ``ref``, or "" when it does not resolve."""
    proc = _run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], root)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _claim_branch(root: str, branch: str, base_sha: str) -> bool:
    """Atomically create ``refs/heads/<branch>`` at ``base_sha``; False if taken.

    The empty old-value argument means "the ref must not exist", so git's ref
    lock decides the winner: exactly one of N concurrent requests for the same
    branch gets a zero exit. That replaces the earlier check-then-create
    (``_branch_head`` followed by ``worktree add -b``), where two requests could
    both observe the branch as absent and the loser's cleanup would then delete
    the winner's branch and working tree.

    A True return is also this request's PROOF OF CREATION: only the claimant may
    later delete the branch.
    """
    proc = _run_git(
        ["update-ref", "--create-reflog", f"refs/heads/{branch}", base_sha, ""],
        root,
    )
    return proc.returncode == 0


def _delete_ref_if_unchanged(root: str, branch: str, base_sha: str) -> bool:
    """Delete ``refs/heads/<branch>`` only while it still points at ``base_sha``.

    ``update-ref -d <ref> <old>`` is git's compare-and-delete: it refuses when the
    ref has moved, which is what keeps cleanup from discarding commits a
    concurrent process added after our claim. With no ``base_sha`` recorded (older
    call sites) there is nothing to compare against, so nothing is deleted —
    leaving a claimed branch behind is recoverable, deleting someone's commits is
    not.
    """
    if not base_sha:
        logger.warning(
            "worktree cleanup has no claimed sha for %s in %s; leaving the branch in place",
            branch,
            root,
        )
        return False
    proc = _run_git(["update-ref", "-d", f"refs/heads/{branch}", base_sha], root)
    return proc.returncode == 0


def _cleanup_partial(
    root: str,
    dest: str,
    branch: str,
    *,
    claimed: bool,
    created: bool,
    base_sha: str = "",
) -> None:
    """Best-effort unwind of a half-created worktree/branch pair.

    ``git worktree add`` can register the worktree and create the branch before
    failing later in the same command (or time out mid-way), leaving artifacts
    that make every retry 409 on "already exists".

    Removes ONLY what this request can PROVE it created:

    * the destination, only when ``created`` — i.e. this request's own
      :func:`os.mkdir` created that directory, which is an atomic claim that fails
      with ``EEXIST`` if anyone else got there first. An additional guard skips it
      if git reports the path registered to a DIFFERENT branch, and if the
      listing could not be read at all (``None``) the directory is still ours by
      the mkdir claim, so only the deregistration is best-effort.
    * the branch, only when ``claimed`` — :func:`_claim_branch` returned True, so
      the ref did not exist beforehand.

    Never inferring ownership from "git lists nothing here" is the point: that
    answer is also what a transient listing failure looks like, and a wrong read
    would recursively delete a directory belonging to something else.
    """
    if created:
        registered = _worktree_branches(root)
        foreign = registered is not None and registered.get(_norm_path(dest), branch) != branch
        if not foreign:
            _run_git(["worktree", "remove", "--force", dest], root)
            if os.path.isdir(dest):
                # `worktree remove` refused (e.g. never registered) — drop the
                # directory we created ourselves so the retry path is clear.
                shutil.rmtree(dest, ignore_errors=True)
    # Prune BEFORE deleting the branch: when `worktree remove` failed and the tree
    # was dropped with rmtree, git still lists the worktree as checked out on this
    # branch and refuses `branch -D` ("used by worktree"). Pruning afterwards left
    # the claimed branch behind, so the retry the docstring promises hit "branch
    # already exists". Delete is retried once after a
    # second prune, and a branch that survives both is logged rather than ignored.
    _run_git(["worktree", "prune"], root)
    if claimed:
        # A concurrent `git worktree add` (or a plain checkout) can ADOPT the
        # branch we claimed while this request was failing. `update-ref -d` has
        # none of `branch -D`'s "used by worktree" protection, so deleting here
        # would leave that worktree sitting on a dangling ref. Re-list AFTER the
        # prune — the prune is what removes our own stale registration — and keep
        # the branch when any surviving worktree other than our own destination
        # holds it. An unreadable listing cannot PROVE nobody adopted it, so it
        # keeps the branch too: a retry reporting "already exists" is recoverable,
        # breaking someone else's worktree is not.
        registered = _worktree_branches(root)
        if registered is None or any(
            held == branch and path != _norm_path(dest) for path, held in registered.items()
        ):
            logger.warning(
                "worktree cleanup left claimed branch %s in %s: another worktree "
                "holds it (or the worktree list could not be read)",
                branch,
                root,
            )
        # COMPARE-AND-DELETE, never `branch -D`: a concurrent git process can
        # advance the ref between our claim and this cleanup (a commit, a push
        # into it), and a force delete would leave those commits unreferenced.
        # `update-ref -d <ref> <old>` deletes ONLY while the ref still points at
        # the value we claimed, so an advanced branch is left alone.
        elif _delete_ref_if_unchanged(root, branch, base_sha) is False:
            _run_git(["worktree", "prune"], root)
            if _delete_ref_if_unchanged(root, branch, base_sha) is False:
                logger.warning(
                    "worktree cleanup could not delete claimed branch %s in %s; "
                    "a retry will report it as already existing",
                    branch,
                    root,
                )


def _create_worktree_sync(root: str, branch: str) -> tuple[dict, int]:
    """Blocking half of the endpoint. Returns ``(json_body, http_status)``."""
    parent = os.path.dirname(root)
    dest = os.path.join(parent, f"{os.path.basename(root)}-wt-{_dir_slug(branch)}")
    if is_sensitive_path(dest):
        return ({"error": "Access denied"}, 403)

    offending = _checkout_filter(root)
    if offending:
        return (
            {
                "error": (
                    f"This repository configures a content filter ({offending}) that git "
                    "would run on checkout. Create the worktree manually."
                )
            },
            409,
        )

    registered = _worktree_branches(root)
    if registered is None:
        return ({"error": "git could not list this repository's worktrees"}, 503)

    # Idempotent re-entry: if the destination is ALREADY the registered worktree
    # for this repo ON THIS BRANCH, this is a retry of a request whose second
    # half (opening the session) failed. Report success with the existing pair
    # instead of 409-ing, so the card's retry can complete. Anything else at that
    # path is someone else's — including a worktree for a DIFFERENT branch that
    # happens to derive the same directory name — and is refused.
    if os.path.exists(dest):
        if registered.get(_norm_path(dest)) == branch:
            return (
                {"ok": True, "path": dest, "branch": branch, "base": "", "reused": True},
                200,
            )
        return ({"error": f"Directory already exists: {dest}"}, 409)

    base = _resolve_base_ref(root)
    base_sha = _resolve_commit(root, base)
    if not base_sha:
        return ({"error": f"Cannot resolve a commit to branch from ({base})"}, 400)
    # Claim the branch BEFORE creating anything, so concurrent requests for the
    # same branch are decided by git's ref lock rather than by a check that both
    # can pass. `worktree add` then checks out the ref we own instead of creating
    # it with `-b`.
    if not _claim_branch(root, branch, base_sha):
        return ({"error": f"Branch already exists: {branch}"}, 409)

    # Claim the DESTINATION the same way, with an atomic mkdir: EEXIST means
    # something else owns that path, and a successful mkdir is this request's
    # proof of creation — the only thing that later authorizes deleting it.
    # `git worktree add` accepts an existing EMPTY directory, so pre-creating it
    # costs nothing (its "already exists" refusal applies to non-empty paths).
    try:
        os.mkdir(dest)
    except FileExistsError:
        _cleanup_partial(root, dest, branch, claimed=True, created=False, base_sha=base_sha)
        return ({"error": f"Directory already exists: {dest}"}, 409)
    except OSError as exc:
        _cleanup_partial(root, dest, branch, claimed=True, created=False, base_sha=base_sha)
        return ({"error": f"Cannot create {dest}: {exc.strerror or exc}"}, 500)

    try:
        proc = _run_git(["worktree", "add", dest, branch], root)
    except subprocess.TimeoutExpired:
        # A timeout can still leave a registered worktree behind.
        _cleanup_partial(root, dest, branch, claimed=True, created=True, base_sha=base_sha)
        raise
    if proc.returncode != 0:
        _cleanup_partial(root, dest, branch, claimed=True, created=True, base_sha=base_sha)
        return ({"error": _git_error(proc)}, 400)
    if not os.path.isdir(dest):
        # Defensive: git reported success but the tree is not there.
        _cleanup_partial(root, dest, branch, claimed=True, created=True, base_sha=base_sha)
        return ({"error": "worktree add reported success but no directory was created"}, 500)
    return ({"ok": True, "path": dest, "branch": branch, "base": base, "reused": False}, 200)


async def api_worktree_create(request: web.Request) -> web.Response:
    """POST ``/api/worktree/create`` with ``{repo, branch}``.

    Creates ``<parent>/<repo>-wt-<slug>`` on a new ``branch`` off the repo's
    default branch. See the module docstring for the input trust model.
    """
    caller = str(request.get("user") or "dashboard")
    # Dashboard users only. The allow-list below is built from EVERY slot's
    # project, so an app caller reaching here could create a worktree inside a
    # repository belonging to another app's session.
    denied = deny_non_dashboard_caller(request, "worktree_create")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON"}, status=400)

    repo = body.get("repo")
    branch = body.get("branch")
    if not isinstance(repo, str) or not isinstance(branch, str):
        return web.json_response({"error": "repo and branch must be strings"}, status=400)
    repo, branch = repo.strip(), branch.strip()
    if not repo or not branch:
        return web.json_response({"error": "repo and branch are required"}, status=400)
    if len(branch) > MAX_FOLLOWUP_BRANCH or not is_valid_followup_branch(branch):
        sel().log_api_access(
            caller=caller,
            operation="worktree_create",
            outcome="denied",
            resources=f"branch={branch[:120]}",
            error="invalid branch name",
        )
        return web.json_response({"error": "Invalid branch name"}, status=400)

    # Allow-list barrier FIRST, before the submitted value touches the
    # filesystem: it is normalized and compared as a string, and what comes back
    # is the server-held slot project. Every path operation from here down uses
    # `repo_root` (server-chosen), never the request value.
    # `_allowed_repo_roots` realpaths and stats every slot project, and the
    # checks below stat again. A project on stalled network storage would block
    # the event loop — and with it every session — for as long as the filesystem
    # takes to answer, so all of it runs on a worker thread, per the repo's
    # no-blocking-calls-on-the-loop rule.
    roots = await asyncio.to_thread(_allowed_repo_roots, request.app.get("state"))
    submitted = os.path.normpath(os.path.expanduser(repo))
    repo_root = _match_allowed_root(submitted, roots)
    if repo_root is None:
        sel().log_api_access(
            caller=caller,
            operation="worktree_create",
            outcome="denied",
            resources=f"repo={submitted[:300]}",
            error="outside slot project directories",
        )
        return web.json_response(
            {
                "error": (
                    "repo must be a project directory of an existing session. "
                    "Set the session's project first."
                )
            },
            status=403,
        )

    if not await asyncio.to_thread(os.path.isdir, repo_root):
        return web.json_response({"error": "repo is not a directory"}, status=400)
    if await asyncio.to_thread(is_sensitive_path, repo_root):
        sel().log_api_access(
            caller=caller,
            operation="worktree_create",
            outcome="denied",
            resources=f"repo={repo_root}",
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied"}, status=403)

    try:
        root = await asyncio.to_thread(_git_toplevel, repo_root)
    except SandboxUnavailable as exc:
        # Fail CLOSED: no OS isolation available, so the spawn does not happen.
        logger.warning("worktree_create: sandbox unavailable: %s", exc)
        sel().log_api_access(
            caller=caller,
            operation="worktree_create",
            outcome="denied",
            resources=f"repo={repo_root}",
            error="sandbox backend unavailable",
        )
        return web.json_response({"error": _SANDBOX_REFUSAL}, status=503)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("worktree_create: git toplevel probe failed: %s", exc)
        return web.json_response({"error": "git is unavailable"}, status=503)
    if not root:
        return web.json_response({"error": "Not a git repository"}, status=400)
    # Re-check the toplevel: resolving upward from an allowed subdirectory can
    # land on a repo root ABOVE every allowed root, which the match above never
    # saw. Without this, granting a nested directory would let git operate on an
    # ancestor the caller was never granted.
    if _match_allowed_root(root, roots) is None:
        sel().log_api_access(
            caller=caller,
            operation="worktree_create",
            outcome="denied",
            resources=f"root={root}",
            error="git toplevel outside slot project directories",
        )
        return web.json_response(
            {"error": "The repository root is outside this session's project directory."},
            status=403,
        )
    if await asyncio.to_thread(is_sensitive_path, root):
        sel().log_api_access(
            caller=caller,
            operation="worktree_create",
            outcome="denied",
            resources=f"root={root}",
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied"}, status=403)

    try:
        async with _repo_lock(root):
            payload, status = await asyncio.to_thread(_create_worktree_sync, root, branch)
    except subprocess.TimeoutExpired:
        sel().log_api_access(
            caller=caller,
            operation="worktree_create",
            outcome="error",
            resources=f"root={root} branch={branch}",
            error="git timeout",
        )
        return web.json_response({"error": "git timed out"}, status=504)
    except SandboxUnavailable as exc:
        logger.warning("worktree_create: sandbox unavailable: %s", exc)
        sel().log_api_access(
            caller=caller,
            operation="worktree_create",
            outcome="denied",
            resources=f"root={root} branch={branch}",
            error="sandbox backend unavailable",
        )
        return web.json_response({"error": _SANDBOX_REFUSAL}, status=503)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("worktree_create failed: %s", exc)
        return web.json_response({"error": "worktree creation failed"}, status=500)

    sel().log_api_access(
        caller=caller,
        operation="worktree_create",
        outcome="allowed" if status == 200 else "error",
        resources=f"root={root} branch={branch} path={payload.get('path', '')}",
        error="" if status == 200 else str(payload.get("error", "")),
    )
    if status == 200:
        logger.info("Created worktree %s (branch %s) from %s", payload.get("path"), branch, root)
    return web.json_response(payload, status=status)
