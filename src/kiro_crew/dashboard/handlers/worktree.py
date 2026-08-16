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
import contextlib
import logging
import os
import re
import shutil
import subprocess

from aiohttp import web

from kiro_crew.dashboard.chat_handlers import deny_non_dashboard_caller
from kiro_crew.sandbox import resource_limit_preexec, sandboxed_spawn_argv
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.validation import MAX_FOLLOWUP_BRANCH, is_valid_followup_branch

logger = logging.getLogger(__name__)

# Wall-clock ceiling for each git invocation. `worktree add` copies a working
# tree, so it is not instant on a large repo, but it is local-only — a run
# longer than this means something is wedged (a lock, a hook prompting for
# input) and the request should fail rather than hold a connection open.
_GIT_TIMEOUT = 120

# Characters kept when turning a branch name into a directory suffix.
_DIR_SLUG_STRIP_RE = re.compile(r"[^A-Za-z0-9._-]+")

# `core.hooksPath` sink. A NON-DIRECTORY, non-replaceable OS device: git finds no
# `post-checkout` under it and there is no directory anyone could drop one into.
#
# Two earlier shapes were both wrong. An in-repo sentinel
# (`.git/kirocrew-no-hooks`) is resolved relative to the repo, so whoever prepared
# the checkout could create it and put `post-checkout` inside — the suppression
# became the execution vector. A gateway-owned `mkdtemp` directory moved the path
# out of the repo but left a same-uid, process-lifetime directory that a
# compromised agent could chmod and populate between calls. `os.devnull` has
# no such window and needs no bookkeeping.
_HOOKS_SINK = os.devnull


def _git_no_repo_code() -> tuple[str, ...]:
    """Config overrides that stop the REPOSITORY supplying a program to run.

    ``-c`` beats every config file, so these hold even against a hostile
    ``.git/config``:

    * ``core.hooksPath`` -> :data:`_HOOKS_SINK`, so no ``post-checkout`` (the one
      hook ``worktree add`` fires) can be found or planted.
    * ``core.fsmonitor=false`` -> repo config can otherwise name an arbitrary
      filesystem-monitor command that git spawns on index reads.

    Together these remove the repo-controlled-code-execution vector, which is
    what would otherwise argue for OS-sandbox isolation on this spawn.
    """
    return ("-c", f"core.hooksPath={_HOOKS_SINK}", "-c", "core.fsmonitor=false")


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


_SANDBOX_REFUSAL = (
    "This host has no OS sandbox backend, so Kiro Crew will not run git for you. "
    "Create the worktree manually."
)

# Prefix every failure the sandbox launcher itself reports (see `sandbox.py`'s
# `sys.exit(f"sandbox: ...")` calls). Distinguishes "isolation could not be
# established" from a genuine git error.
_SANDBOX_LAUNCHER_PREFIX = "sandbox: "

# STRICT, not the "standard" default. `_checkout_filter` runs `git config
# --includes`, and `include.path` is repo-controlled: a hostile checkout can point
# it at `~/.aws/credentials` (or `~/.netrc`, `~/.git-credentials`) and have git
# READ that file as config. "standard" leaves those visible; "strict" bind-mounts
# them away, along with `~/.ssh`. Nothing here
# needs a credential: the base ref is resolved from local refs and no remote is
# contacted, so strict costs the operation nothing.
_SANDBOX_MODE = "strict"


class SandboxUnavailable(RuntimeError):
    """No OS sandbox backend, so the git spawn is refused rather than run bare."""


# One lock per repo root, so two same-repo creates in this gateway never
# interleave their check/create/cleanup sequences. Cross-request atomicity does
# not depend on this (the branch claim in `_claim_branch` is what git enforces),
# but it removes the same-destination window between the "does dest exist" probe
# and `worktree add`, which is what lets `_cleanup_partial` treat an unregistered
# leftover directory as its own.
_REPO_LOCKS: dict[str, asyncio.Lock] = {}
_MAX_REPO_LOCKS = 64


def _repo_lock(root: str) -> asyncio.Lock:
    """Return (creating if needed) the serialization lock for ``root``."""
    lock = _REPO_LOCKS.get(root)
    if lock is None:
        if len(_REPO_LOCKS) >= _MAX_REPO_LOCKS:
            # Drop idle entries so a long-lived gateway that has seen many repos
            # does not accumulate locks forever. Held locks are kept.
            for key in [k for k, v in _REPO_LOCKS.items() if not v.locked()]:
                del _REPO_LOCKS[key]
        lock = _REPO_LOCKS[root] = asyncio.Lock()
    return lock


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    """Run git with an argv list (never a shell) inside ``cwd``, OS-sandboxed.

    Routed through the ``sandboxed_spawn_argv`` chokepoint, matching
    ``git_coord.py``'s treatment of agent-influenced git: the repository is
    agent-selected and the branch is LLM-authored, so this spawn takes the OS
    isolation layer plus the credential-scrubbed environment rather than relying
    on argument hygiene alone.

    :exc:`SandboxUnavailable` is raised when the host has no sandbox backend and
    ``agent.sandbox_allow_unsandboxed_exec`` is unset; the endpoint turns that
    into a 503 telling the user to create the worktree themselves. Fail CLOSED —
    a local convenience is not worth an unisolated spawn.

    The repo-supplied-code guards are still applied on top, because sandboxing
    bounds what a hook could reach but does not stop it running: the ``-c``
    overrides in :func:`_git_no_repo_code` remove ``core.hooksPath`` and
    ``core.fsmonitor``, and a repo declaring a checkout filter driver in either
    repository config scope is refused before this runs
    (:func:`_checkout_filter`).

    The remaining protections are all here: an argv list with no shell, the
    POSIX resource-limit ceiling (``resource_limit_preexec`` returns ``None`` on
    Windows, where ``preexec_fn`` must be ``None``), a wall-clock timeout, and
    ``GIT_TERMINAL_PROMPT=0`` so a credential helper cannot block on an
    interactive prompt.
    """
    try:
        argv, env, cleanup = sandboxed_spawn_argv(
            ["git", *_git_no_repo_code(), *args], mode=_SANDBOX_MODE
        )
    except RuntimeError as exc:  # no sandbox backend and no explicit opt-in
        raise SandboxUnavailable(str(exc)) from exc
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            preexec_fn=resource_limit_preexec(),
        )
    finally:
        if cleanup:
            with contextlib.suppress(OSError):
                os.unlink(cleanup)
    # The launcher can only discover SOME isolation failures in the child, after
    # `wrap_argv` has already returned: `unshare(NEWNS)` is permitted by the
    # backend probe but denied at exec time on hosts that restrict mount
    # namespaces (GitHub Actions runners are one — errno 1/EPERM). git never
    # runs, so without this the non-zero exit is misread downstream as "not a git
    # repository" or "cannot list worktrees". Surface it as the same refusal a
    # missing backend gets, so the user is told the truth.
    if proc.returncode != 0 and (proc.stderr or "").startswith(_SANDBOX_LAUNCHER_PREFIX):
        raise SandboxUnavailable((proc.stderr or "").strip())
    return proc


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


def _resolve_base_ref(root: str) -> str:
    """Pick the ref to branch from: the remote's default branch, else HEAD.

    ``origin/HEAD`` is the repo's own declaration of its default branch, which
    beats hardcoding "main" (repos whose default branch is named something else,
    or with a different primary remote layout, would otherwise fail). Falls back
    to ``HEAD`` so a repo with no remote — or no fetched ``origin/HEAD`` — still
    works, at the cost of branching from whatever is currently checked out.
    """
    probe = _run_git(["rev-parse", "--verify", "--quiet", "origin/HEAD"], root)
    if probe.returncode == 0 and probe.stdout.strip():
        return "origin/HEAD"
    return "HEAD"


def _git_toplevel(repo: str) -> str | None:
    """Return the work-tree root containing ``repo``, or None if not a repo."""
    probe = _run_git(["rev-parse", "--show-toplevel"], repo)
    if probe.returncode != 0:
        return None
    top = probe.stdout.strip()
    return os.path.realpath(top) if top else None


def _git_error(proc: subprocess.CompletedProcess[str]) -> str:
    """Condense git's stderr into a single-line message for the UI."""
    text = (proc.stderr or proc.stdout or "").strip()
    if not text:
        return "git failed"
    # Keep the first meaningful line; git prefixes most failures with "fatal:".
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:300]
    return "git failed"


def _norm_path(path: str) -> str:
    """Normalized, case-folded form used to compare paths against git's output."""
    return os.path.normcase(os.path.normpath(path))


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


def _allowed_repo_roots(state: object) -> list[str]:
    """Realpath'd project directories that some existing chat slot is scoped to.

    This is the allow-list ``repo`` must fall inside. The frontend only ever
    sends the active slot's own ``project``, so constraining to this set costs
    the feature nothing while removing the endpoint's arbitrary-path surface:
    without it, any authenticated dashboard caller could name *any* directory on
    the host and have git run against it (CodeQL: "uncontrolled data used in
    path expression"). Slot projects are set through
    ``/api/chat/slots/{slot}/project``, which already realpaths and
    sensitive-path-screens them.
    """
    roots: list[str] = []
    slots = getattr(state, "_slots", None) or {}
    for slot in list(getattr(slots, "values", list)()):
        project = str(getattr(slot, "project", "") or "").strip()
        if not project:
            continue
        resolved = os.path.realpath(project)
        if os.path.isdir(resolved) and resolved not in roots:
            roots.append(resolved)
    return roots


def _match_allowed_root(candidate: str, roots: list[str]) -> str | None:
    """Return the allow-listed root that ``candidate`` names or sits inside.

    Returns the value FROM ``roots`` (a server-held slot project), never the
    caller's string — every filesystem operation downstream then runs on a path
    the server chose, which is both the point of the barrier and why CodeQL's
    "uncontrolled data used in path expression" no longer applies: the request
    value is used for comparison only.

    ``candidate`` must be normalized by the caller. Comparison goes through
    ``os.path.normcase`` because Windows paths are case-insensitive and
    ``realpath`` does not reliably canonicalize case there — without it a
    differently-cased but identical path would be refused. The prefix test is
    ``os.sep``-terminated, so ``/repo-evil`` does not pass as inside ``/repo``.
    """
    probe = os.path.normcase(candidate)
    for root in roots:
        normalized = os.path.normcase(root)
        if probe == normalized or probe.startswith(normalized.rstrip(os.sep) + os.sep):
            return root
    return None


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
