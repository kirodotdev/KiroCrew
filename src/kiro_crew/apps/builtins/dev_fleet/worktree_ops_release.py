"""Materializing the release-channel worktree: the one mutation this lane has.

Resolution is a pure read and lives in ``release_channel_pin``. This is the writer,
and it lives beside the other worktree writers because it takes the same ``.git``
admin lock they take -- a second module reaching for that lock from outside this
family is how a lock-order inversion gets introduced. Reached through
``worktree_ops``, its facade.

Lock order, unchanged from the removal path::

    _wt_lock(name)  ->  _GIT_MUTATION_LOCK

The checkout itself is HERMETIC: the release's files are written by a ``git
read-tree -u --reset`` whose ``GIT_COMMON_DIR`` points at a throwaway directory
this process created, so the repository's own ``config`` and ``info/attributes``
are never opened by that spawn. See :func:`_populate_release_tree` for what that
replaces and why enumerating the config instead could not close the class.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from kiro_crew.apps.builtins.dev_fleet import fleet_state, release_channel_pin, repository, runtime
from kiro_crew.apps.builtins.dev_fleet.worktree_ops_pods import _wt_lock
from kiro_crew.apps.builtins.dev_fleet.worktree_ops_removal import _GIT_MUTATION_LOCK
from kiro_crew.executors import subprocess_executor
from kiro_crew.loop_lock import LoopBoundLock

#: Result ``code``s that mean "the current state does not allow this", as opposed to
#: "a git command this app runs failed unexpectedly".
#:
#: Read by ``http_api`` to pick the status: a refusal is a 4xx the caller can act on
#: (remove the tree, publish a release, clean the forged ref), an unexpected git
#: failure is a 5xx. Spelled ONCE, here, beside the code strings themselves -- the
#: alternative was a second literal list in the route, and the two would drift the
#: first time a refusal was added, silently turning it into a 500.
_RELEASE_CHANNEL_REFUSALS = frozenset(
    {
        "no_repo",
        "ambiguous_name",
        "exists",
        "busy",
        "path_exists",
        "fetch_failed",
        "unresolved",
        "unpublished",
        "provenance_unverifiable",
        "provenance_mismatch",
    }
)

#: Minimum config for a git common dir that must answer nothing but "not bare".
#:
#: Written rather than copied: a copy of the repository's own ``config`` would carry
#: the very declarations the hermetic spawn exists to put out of reach. ``bare =
#: false`` is required because ``read-tree -u`` refuses to touch a working tree
#: without it; nothing else is needed, and nothing else is supplied.
_HERMETIC_COMMON_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"

#: HEAD for that common dir. Unborn on purpose: the populate reads HEAD from the
#: staging worktree's OWN gitdir (``$GIT_DIR/HEAD``, which ``worktree add --detach``
#: wrote to the resolved oid), so this file is never the one consulted for the
#: checkout -- it exists because git requires the common dir to look like a
#: repository at all.
_HERMETIC_COMMON_HEAD = "ref: refs/heads/none\n"


#: The run id of the most recent create started through
#: :func:`_release_channel_create_start`, and the lock that makes "is one running?"
#: and "start one" a single step. Checked against the run registry rather than
#: cleared at the end: a run whose status is not ``running`` is simply not in flight.
_CREATE_RID: str | None = None
_CREATE_ADMISSION_LOCK = LoopBoundLock()

#: ``_RUNS`` label of the create, so a run listing names what it is.
_CREATE_RUN_LABEL = "create " + release_channel_pin.WORKTREE_NAME


async def _release_channel_preflight() -> tuple[dict | None, str, str, str]:
    """``(refusal, repo, name, path)`` -- the refusals that need no lock to decide.

    These are answered twice on purpose: once by the route, synchronously, so the
    caller hears ``exists`` or ``ambiguous_name`` as a 409 instead of a run that
    fails, and once more by the run itself right before it takes the lock, because
    the fleet it is about to write is the one at that moment and not the one the
    route looked at.
    """
    name = release_channel_pin.WORKTREE_NAME
    try:
        repo = repository._repo()
    except repository.RepoUnavailable as exc:
        return {"ok": False, "code": "no_repo", "error": str(exc)}, "", name, ""
    existing, find_err = await repository._find_worktree(name)
    if existing is None and find_err and find_err != f"worktree not found: {name}":
        # The fleet's own writer only ever creates the canonical sibling path, so
        # ambiguity means an out-of-band checkout; refusing is loud and
        # path-recoverable.
        refusal = {
            "ok": False,
            "code": "ambiguous_name",
            "error": f"refusing to create {name}: {find_err}",
        }
        return refusal, repo, name, ""
    if existing is not None:
        # Names the two controls that exist. This is the one collision an operator is
        # guaranteed to hit -- the placeholder's Create is gone once the row is
        # adopted, so anything reaching here is a stale tab or a second client -- and
        # the pin does not move in place, so "move it to the tip" would send them
        # looking for a control that was never shipped.
        refusal = {
            "ok": False,
            "code": "exists",
            "error": f"{name} already exists -- remove it first, then create it again",
        }
        return refusal, repo, name, ""
    if _wt_lock(name).locked():
        refusal = {
            "ok": False,
            "code": "busy",
            "error": f"another {name} operation is already running",
        }
        return refusal, repo, name, ""
    return None, repo, name, release_channel_pin.worktree_path(repo)


async def _release_channel_create_start(
    on_finish: Callable[[dict | None], None] | None = None,
) -> dict:
    """Start the create as a tracked run; ``{ok, run_id}`` or a refusal.

    The create is a fetch, a ``worktree add``, a populate and a ``worktree move``,
    each with a timeout in the minutes, and the app proxy a browser reaches this
    route through gives up on a request at 30 seconds -- so a route that awaited
    the create could never deliver its result to the one caller that asked for it.
    The route starts the run and answers with its id; the outcome is read from
    ``/api/run?id=`` as ``result`` once the run's status has left ``running``.

    Single-flight: a second start while one is running answers ``busy`` and NAMES
    the run, so a page that lost its own id (a reload mid-create) attaches to it
    instead of reporting a collision. Check and start are one step under
    :data:`_CREATE_ADMISSION_LOCK`; two queued starts could otherwise both see no
    run in flight.
    """
    global _CREATE_RID
    name = release_channel_pin.WORKTREE_NAME
    async with _CREATE_ADMISSION_LOCK:
        # The running create is answered FIRST, before the preflight: the run body
        # holds ``_wt_lock(name)`` from just after it starts until the move lands,
        # so the preflight's own ``busy`` -- which names no run -- would otherwise
        # be the answer for the whole of a create's life, and the page that asked
        # would show a failure instead of attaching to the work in flight.
        if _CREATE_RID is not None:
            async with runtime._RUNS_LOCK:
                running = runtime._RUNS.get(_CREATE_RID, {}).get("status") == "running"
            if running:
                return {
                    "ok": False,
                    "code": "busy",
                    "error": f"a {name} create is already running",
                    "run_id": _CREATE_RID,
                }
        # Awaited under the admission lock on purpose: two starts queued behind one
        # another must each see the run the other started, and the preflight is a
        # single ``git worktree list`` read.
        refusal, _repo, name, _path = await _release_channel_preflight()
        if refusal is not None:
            return refusal
        rid = await runtime._start_task_run(
            _CREATE_RUN_LABEL, _release_channel_create, on_finish=on_finish
        )
        _CREATE_RID = rid
    return {"ok": True, "run_id": rid, "name": name}


async def _release_channel_create() -> dict:
    """Materialize the release-channel worktree, detached at the channel tip.

    Detached on purpose. A branch would invite a ``git pull`` that drifts the tree
    off the release it is supposed to BE, and the fleet's behind-main count on a
    branch-bearing row would start measuring against a ref the operator never chose.
    Detached says what the tree is: a fixed point, which moves only by being removed
    and created again at a newer release.

    This is the body of the run :func:`_release_channel_create_start` tracks; it
    runs to completion and returns the result the run record carries.
    """
    refusal, repo, name, path = await _release_channel_preflight()
    if refusal is not None:
        return refusal
    async with _wt_lock(name):
        return await _release_channel_create_locked(repo, name, path)


async def _release_channel_create_locked(repo: str, name: str, path: str) -> dict:
    """The create, with ``_wt_lock(name)`` already held."""
    loop = asyncio.get_running_loop()
    # Refuse rather than adopt. A directory already at the target path is not ours;
    # ``git worktree add`` fails on a non-empty path anyway, and refusing here names
    # the path instead of surfacing git's message about a directory the operator did
    # not know was involved.
    if await loop.run_in_executor(subprocess_executor(), Path(path).exists):
        return {
            "ok": False,
            "code": "path_exists",
            "error": f"refusing: {path} already exists on disk",
        }
    fetch_err = await release_channel_pin.fetch_refs(repo)
    if fetch_err:
        return {
            "ok": False,
            "code": "fetch_failed",
            "error": f"cannot refresh release refs: {fetch_err}",
        }
    resolved = await release_channel_pin.resolve_tip(repo=repo)
    if not resolved.get("ok"):
        return {
            "ok": False,
            "code": "unpublished" if resolved.get("unpublished") else "unresolved",
            "error": resolved.get("error") or "the release channel could not be resolved",
        }
    oid = str(resolved["oid"])
    # The oid above came from the mirror, which the fetch's `--prune` only cleans of
    # refs absent when that subprocess EXITED -- so a ref planted afterwards is
    # unpruned, outranks every real release if its version is high enough, and its oid
    # is what would be checked out and later run by the lane's pod. Ask the remote what
    # it actually publishes and refuse anything else. This closes the class rather than
    # the instance: no timing wins, because a locally planted ref is not in the
    # advertised set at any moment.
    #
    # Fails CLOSED on a failed query, and treats an unadvertised tag as a refusal
    # rather than falling back to the real oid -- a silent correction would materialize
    # the right tree and hide that the mirror had been written to.
    tag = str(resolved["ref"]).rsplit("/", 1)[-1]
    published, adv_err = await release_channel_pin.advertised_release_commit(repo, tag)
    if adv_err:
        return {
            "ok": False,
            "code": "provenance_unverifiable",
            "error": f"cannot confirm the release with the remote: {adv_err}",
        }
    if published != oid:
        return {
            "ok": False,
            "code": "provenance_mismatch",
            "error": (
                f"refusing: {tag} in this checkout does not match what the remote "
                f"publishes -- the release ref store has been written to locally"
            ),
        }
    staging = _lane_staging_path(path)
    # Every exit from here to the adoption below has to leave the staging tree cleaned
    # up, and a RETURN is not the only exit. `_run_uninterruptible` shields each git
    # child but re-raises `CancelledError` once it returns, so an ordinary backend
    # shutdown unwinds this frame between a successful `worktree add` and the `worktree
    # move` that adopts it -- and the failure cleanups below are reached only by a
    # returning failure, never by an unwind. `BaseException` rather than `Exception`
    # because cancellation is the case this exists for. Best-effort and then re-raise:
    # the cleanup's own awaits can be cancelled in turn, so this narrows the window
    # rather than closing it. What is left when it loses is bounded -- a
    # `<lane>.staging.<pid>.<rand>` directory, which the operator's own `git worktree
    # prune` or `remove` reclaims (this module never prunes repository-wide, see
    # `_discard_failed_lane_creation`) and which cannot block a retry, because the
    # create guard tests the LANE path and every call stages under a fresh name.
    try:
        return await _stage_and_adopt(repo, name, path, staging, oid, resolved)
    except BaseException:
        await _discard_failed_lane_creation(repo, staging)
        raise


async def _stage_and_adopt(
    repo: str, name: str, path: str, staging: str, oid: str, resolved: dict
) -> dict:
    """Build the lane at *staging*, populate it hermetically, then adopt it at *path*."""
    loop = asyncio.get_running_loop()
    add_err = ""
    populate_err: str | None = None
    staged_head: str | None = None
    # Every failure below is reported AFTER this block, never inside it:
    # `_discard_failed_lane_creation` takes `_GIT_MUTATION_LOCK` itself, and
    # `LoopBoundLock` is not reentrant, so a cleanup reached while the lock is held
    # would wait on this frame forever.
    async with _GIT_MUTATION_LOCK:
        # Build the admin record and staging gitdir first, but deliberately leave the
        # worktree empty: the populate below supplies its own object-store view, so
        # there is nothing to gain from letting git write files under the repository's
        # own config first.
        #
        # Drained: a cancellation landing mid-`worktree add` would otherwise unwind
        # this frame -- releasing _GIT_MUTATION_LOCK -- while git is still registering
        # the admin record.
        #
        # Built at `staging`, not at `path`, so the destructive cleanup below can only
        # ever target a directory THIS call named. See `_lane_staging_path`.
        add_rc, _add_out, add_err = await runtime._run_uninterruptible(
            runtime._run_cmd(
                ["git", "-C", repo, "worktree", "add", "--no-checkout", "--detach", staging, oid],
                timeout=300,
                mode="strict",
            )
        )
        if add_rc == 0:
            populate_err = await _populate_release_tree(repo, staging, oid)
            if populate_err is None:
                # Read back through the staging worktree's REAL gitdir, which is a
                # plain file read of `$GIT_DIR/HEAD`: the populate never moves HEAD, so
                # this confirms the detached pin `worktree add` set is still the oid
                # provenance verified.
                staged_head = await repository._git(staging, "rev-parse", "HEAD", mode="strict")
    if add_rc != 0:
        # `worktree add` can fail AFTER registering the worktree and creating the
        # directory, and the path-exists guard in the caller then refuses every retry --
        # so the operator would be left with a lane that can neither be created nor
        # removed, recoverable only by hand.
        await _discard_failed_lane_creation(repo, staging)
        detail = runtime._error_detail(add_err)
        return {"ok": False, "code": "git_failed", "error": f"git worktree add failed: {detail}"}
    if populate_err is not None:
        await _discard_failed_lane_creation(repo, staging)
        return {
            "ok": False,
            "code": "git_failed",
            "error": f"could not write the release into the worktree: {populate_err}",
        }
    if staged_head != oid:
        await _discard_failed_lane_creation(repo, staging)
        return {
            "ok": False,
            "code": "git_failed",
            "error": "refusing: the staged worktree is not detached at the resolved release",
        }
    # Adopt the staged tree under the lane's real name. `worktree move` refuses a
    # destination that is a registered WORKTREE, and that is the half of the race it
    # decides: a second writer in this process or another gateway that adopted `path`
    # first wins, and we discard our own staging tree instead of deleting theirs.
    #
    # It does NOT refuse a plain DIRECTORY. Like mv(1), `worktree move <src> <dir>`
    # with an existing directory destination moves the source INTO it, exits 0, and
    # registers the worktree at `<path>/<staging basename>` -- nested inside whatever
    # appeared at `path` during the fetch, resolve and populate above (a `mkdir`, an
    # operator's own `worktree add`, a checkout another process is still building).
    # Measured on git 2.54. So the move is bracketed: the destination is re-checked
    # under the lock right before it, which closes the window for every writer in
    # this process, and the landing is verified right after it, which is the check no
    # lock can replace for a writer in another process. Verification asks git where
    # the worktree at `path` keeps its admin dir and compares it with the staging
    # worktree's own, captured before the move: equal means the move landed as
    # intended; anything else means `path` is someone else's tree and ours is nested
    # inside it, in which case the nested tree is removed -- it is ours by
    # construction, see `_lane_staging_path` -- and the collision is reported.
    collided = False
    staging_gitdir = await repository._git(
        staging, "rev-parse", "--absolute-git-dir", mode="strict"
    )
    async with _GIT_MUTATION_LOCK:
        if await loop.run_in_executor(subprocess_executor(), Path(path).exists):
            collided, mrc, merr = True, 0, ""
        else:
            mrc, _mout, merr = await runtime._run_uninterruptible(
                runtime._run_cmd(
                    ["git", "-C", repo, "worktree", "move", staging, path],
                    timeout=120,
                    mode="strict",
                )
            )
    if mrc == 0 and not collided:
        landed_gitdir = await repository._git(
            path, "rev-parse", "--absolute-git-dir", mode="strict"
        )
        if not staging_gitdir or landed_gitdir != staging_gitdir:
            collided = True
            # Where mv(1) semantics put it. Removing by that path and not by `path`
            # is the whole point: `path` is the other writer's tree.
            staging = os.path.join(path, os.path.basename(staging))
    if collided:
        await _discard_failed_lane_creation(repo, staging)
        return {
            "ok": False,
            "code": "path_exists",
            "error": f"refusing: {path} already exists on disk",
        }
    if mrc != 0:
        await _discard_failed_lane_creation(repo, staging)
        if await loop.run_in_executor(subprocess_executor(), Path(path).exists):
            return {
                "ok": False,
                "code": "path_exists",
                "error": f"refusing: {path} already exists on disk",
            }
        return {
            "ok": False,
            "code": "git_failed",
            "error": f"git worktree move failed: {runtime._error_detail(merr)}",
        }
    # The row exists now. Tell the fleet cache before anyone asks, so a rebuild that
    # started while this create ran cannot publish a snapshot without it -- the
    # `fresh=1` the page sends on success would otherwise coalesce onto that build
    # and show the placeholder it just replaced. `_fleet_forget` is the removal twin.
    fleet_state._fleet_adopted()
    return {
        "ok": True,
        # The channel's name rides along for the success toast ("Created stable
        # worktree at 0.5.3"); the page reads it from the reply rather than from a
        # row it may not have rendered yet.
        "lane": resolved["lane"],
        "name": name,
        "ref": resolved["ref"],
        "version": resolved["version"],
        "path": path,
    }


async def _populate_release_tree(repo: str, staging: str, oid: str) -> str | None:
    """Write *oid*'s tree into *staging* with this repository's config out of reach.

    ``None`` on success, else the error to report.

    WHY THE OBJECT STORE IS BORROWED RATHER THAN THE CONFIG READ. Checking a tag out
    runs whatever ``filter.<name>.process``/``.smudge`` driver applies to the files it
    writes, and that driver is an arbitrary command named by this repository's
    ``.git/config`` -- which is writable by anything with the agent's filesystem
    access. ``core.worktree`` from the same file redirects where those files land.
    Neither is reachable by ``_GIT_ENV_NEUTRALIZERS``: the filter key space is
    unbounded (``<name>`` is attacker-chosen) and git deliberately ignores a
    ``core.worktree`` supplied through ``-c``/``GIT_CONFIG_*``.

    Enumerating the config instead -- probe for a filter key, probe for a redirect,
    refuse -- was the previous shape, and it could not close the class. ``git``
    reads the LOCAL ``.git/config`` on every invocation and no flag suppresses that
    (``GIT_CONFIG_NOSYSTEM`` and ``GIT_CONFIG_GLOBAL`` reach only the other two
    scopes), so a probe and the checkout it guards are two spawns with a writable
    file between them; the probe also had to grow a scope, a boolean-spelling and an
    include-following rule per git feature it learned about.

    So the checkout is given a DIFFERENT repository to read config from: a throwaway
    common dir this process just created, holding the minimum config
    (:data:`_HERMETIC_COMMON_CONFIG`), an empty ``refs/``, and an
    ``objects/info/alternates`` naming the real store -- so every object resolves
    while ``config`` and ``info/attributes`` resolve to files no other writer has.
    ``GIT_DIR`` stays the staging worktree's own gitdir, so the index and HEAD are
    written where the worktree keeps them. ONE spawn, with no probe to race.

    A ``.gitattributes`` in the release's own tree can still NAME a filter, and that
    stays inert: a name with no driver defined in the config git is reading is a
    no-op, ``required`` included.

    What this does NOT promise: after the populate, the lane is an ordinary worktree
    again, so an operator-config ``required`` clean filter can make a later ``git
    status`` in it fail (rc 128). That is the operator's own config acting on an
    ordinary read, identical to every other fleet row, and the row builder already
    tolerates a failed status.
    """
    loop = asyncio.get_running_loop()
    gitdir, objects, probe_err = await _hermetic_paths(repo, staging)
    if probe_err is not None:
        return probe_err
    try:
        common = await loop.run_in_executor(
            subprocess_executor(),
            _make_hermetic_common_dir,
            str(Path(staging).parent),
            objects,
        )
    except OSError as exc:
        return runtime._error_detail(f"cannot stage a private git common dir: {exc}")
    try:
        # Built, not merged over `os.environ`: the hazard here is a variable that must
        # be ABSENT. An inherited `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
        # `GIT_ALTERNATE_OBJECT_DIRECTORIES` or `GIT_NAMESPACE` would survive an
        # update() and silently retarget this write, so every inherited `GIT_*` is
        # dropped and only the five below are supplied. `_run_cmd` re-applies
        # `_GIT_ENV_NEUTRALIZERS` on top, which is where the fixed-key exec vectors
        # (hooks, fsmonitor, credential helper, signature programs) are pinned.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(
            {
                "GIT_DIR": gitdir,
                "GIT_COMMON_DIR": common,
                "GIT_WORK_TREE": staging,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            }
        )
        # `read-tree -u --reset`, never `reset --hard`: this must update the index and
        # the working tree and leave HEAD exactly where `worktree add --detach` put it,
        # which is what the read-back above verifies.
        rc, _out, err = await runtime._run_uninterruptible(
            runtime._run_cmd(
                ["git", "read-tree", "-u", "--reset", oid],
                cwd=staging,
                env=env,
                timeout=300,
                mode="strict",
            )
        )
    finally:
        # Deleted whatever happened, including on the cancellation path: the directory
        # is this call's by construction (``mkdtemp``), holds no object of its own, and
        # a leftover would be a private git repository sitting beside the operator's
        # checkouts.
        await loop.run_in_executor(subprocess_executor(), _discard_hermetic_common_dir, common)
    if rc != 0:
        return runtime._error_detail(err) or "git read-tree failed"
    return None


async def _hermetic_paths(repo: str, staging: str) -> tuple[str, str, str | None]:
    """``(staging gitdir, repository object store, error)`` -- both absolute.

    Fails CLOSED: a path git will not state is an error, because a populate whose
    object store or index location had to be guessed must not run. Both reads take
    the ``strict`` tier -- they need no network, so they need no credentials.
    """
    rc, out, err = await runtime._run_cmd(
        ["git", "-C", staging, "rev-parse", "--absolute-git-dir"], timeout=20, mode="strict"
    )
    gitdir = (out or "").strip()
    if rc != 0 or not gitdir:
        detail = runtime._error_detail(err)
        return "", "", detail or "cannot locate the staged worktree's git dir"
    rc, out, err = await runtime._run_cmd(
        ["git", "-C", repo, "rev-parse", "--git-common-dir"], timeout=20, mode="strict"
    )
    stated = (out or "").strip()
    if rc != 0 or not stated:
        detail = runtime._error_detail(err)
        return "", "", detail or "cannot locate this checkout's git object store"
    # `--git-common-dir` answers relatively (`.git`) for a command run inside the
    # checkout, so it is joined against the repo rather than used as given; `join`
    # keeps an absolute answer unchanged.
    objects = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), lambda: os.path.realpath(os.path.join(repo, stated, "objects"))
    )
    return gitdir, objects, None


def _make_hermetic_common_dir(parent: str, objects: str) -> str:
    """Create the throwaway git common dir the populate reads config from.

    Runs on the executor: four file writes and a ``mkdtemp``. ``mkdtemp`` creates the
    directory 0700 and with a name no other process can predict, which is what makes
    the later delete this call's own.

    Placed beside the staging worktree rather than in the system temp dir so it shares
    the filesystem and the access rules of the trees this app already writes, and so an
    operator who finds a leftover finds it next to the tree it belonged to.
    """
    common = tempfile.mkdtemp(prefix=".release-channel-gitcommon.", dir=parent)
    os.makedirs(os.path.join(common, "objects", "info"), exist_ok=True)
    os.makedirs(os.path.join(common, "refs"), exist_ok=True)
    for relative, text in (
        ("config", _HERMETIC_COMMON_CONFIG),
        ("HEAD", _HERMETIC_COMMON_HEAD),
        (os.path.join("objects", "info", "alternates"), f"{objects}\n"),
    ):
        with open(os.path.join(common, relative), "w", encoding="utf-8") as handle:
            handle.write(text)
    return common


def _discard_hermetic_common_dir(common: str) -> str | None:
    """Delete the throwaway common dir. Best-effort; the populate's error is the news."""
    try:
        shutil.rmtree(common)
    except OSError as exc:  # pragma: no cover - defensive; nothing to recover
        runtime.logger.warning("release-channel: private git common dir left behind: %s", exc)
        return str(exc)
    return None


def _lane_staging_path(path: str) -> str:
    """A sibling path only THIS call can name, for building a lane before adopting it.

    Creating the lane directly at its final path made the failure cleanup unsafe, and
    the reason is worth keeping: the cleanup inferred ownership from the earlier
    ``Path(path).exists()`` guard -- "it was not there before we ran, so whatever is
    there now is ours". That inference needs a single writer, and the locks these
    operations hold (:data:`_GIT_MUTATION_LOCK`, ``_wt_lock``) are per-event-loop, so
    they serialize one process and not two. A second gateway sharing the repo could
    create the path between our guard and our ``worktree add``; ours then failed and the
    cleanup deleted THEIR populated worktree, with no reflog to recover untracked files
    from.

    Staging removes the inference instead of adding a lock. The pid and a random suffix
    make the directory ours by construction, so ``remove --force`` can never name a path
    another writer owns, and ``git worktree move`` -- which refuses an existing
    destination -- becomes the single step that decides the race. The loser discards its
    own staging tree and reports the collision.

    Deliberately a sibling rather than a temp dir: ``worktree move`` across filesystems
    is not guaranteed, and the lane's parent is where it must land.
    """
    return f"{path}.staging.{os.getpid()}.{secrets.token_hex(4)}"


async def _discard_failed_lane_creation(repo: str, path: str) -> None:
    """Remove the residue of a ``worktree add`` that failed part-way.

    Only ever called on a STAGING path, which carries the creating process's pid and a
    random suffix, so the directory is this call's by construction. That is the whole
    reason a ``remove --force`` is allowed here: the previous shape aimed it at the
    lane's real path and inferred ownership from an earlier existence check, which a
    second process on the same repo could invalidate. See :func:`_lane_staging_path`.

    Best-effort by construction -- the caller is about to return git's real error, and a
    cleanup failure must not replace it, so every outcome here is swallowed and logged.

    ONE scoped command, no ``worktree prune``. ``remove --force <staging>`` deletes the
    staging worktree's administrative record itself, and does so whether or not the
    directory still exists (a staging dir that ``worktree add`` never got to create, or
    one already gone, is exit 0 with the record dropped). A repository-wide ``prune``
    after it therefore covered nothing this helper owns, and what it DID reach was every
    other worktree of the repository: an unrelated, unlocked checkout whose directory is
    temporarily absent -- unmounted removable or network media is the ordinary case -- is
    exactly what ``prune`` unregisters, and it has no undo. Cleanup after a failed lane
    create never gets to decide that for a worktree it did not create.
    """
    try:
        async with _GIT_MUTATION_LOCK:
            await runtime._run_uninterruptible(
                runtime._run_cmd(
                    ["git", "-C", repo, "worktree", "remove", "--force", path],
                    timeout=60,
                    mode="strict",
                )
            )
    except Exception:  # pragma: no cover - defensive; the real error is the caller's
        runtime.logger.warning("release-channel: cleanup after failed create raised", exc_info=True)


__all__ = (
    "_CREATE_RUN_LABEL",
    "_RELEASE_CHANNEL_REFUSALS",
    "_discard_failed_lane_creation",
    "_lane_staging_path",
    "_release_channel_create",
    "_release_channel_create_start",
    "_release_channel_preflight",
)
