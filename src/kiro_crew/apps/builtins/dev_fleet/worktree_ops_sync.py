"""Pull + Build sync of the main checkout, for Dev Fleet.

``_sync`` pulls the resolved base branch into the main checkout and rebuilds it (venv,
dependency sync, frontend dist) as one single-flight run, pinned to a per-process base
ref. Reached through ``worktree_ops``, its facade; holds ``_SYNC_RID``, the id of the
sync run in flight, which ``_sync_start_locked`` rebinds.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from kiro_crew import dep_sync, frontend, platform_compat
from kiro_crew.apps.builtins.dev_fleet import fleet_state, npm_preflight, repository, runtime
from kiro_crew.executors import subprocess_executor
from kiro_crew.git_config_hooks import ConfigHookScanError
from kiro_crew.sandbox import sandboxed_spawn_argv, shielded_prepare_off_loop


def _sync_base_ref() -> str:
    """The ref this process pins the revision it is about to merge to.

    ``<remote>/<base branch>`` cannot serve for this: it is a MUTABLE name, and
    the status refresher re-fetches it every ``_NET_REFRESH_S`` seconds in this
    same process. Resolving that name once in the probe and again in the merge --
    with a real install in between -- lets the two land on different commits, so
    the probe would certify a revision the merge does not install. Fetching the
    tip into a ref of our own closes the window instead of narrowing it: the
    refresher's fetch writes only the remote-tracking refs, so nothing can move
    this one for the life of the run.

    The PID is part of the name because ``_SYNC_LOCK`` is a lock in ONE process,
    which makes syncs single-flight only within a single gateway. Two gateways
    configured against the same checkout would otherwise share this ref, and the
    second one's fetch would move it between the first one's probe and merge --
    reopening exactly the window the ref exists to close. That configuration is
    already destructive for a bigger reason (two syncs running ``git merge`` and
    ``npm ci`` against one working tree), so this does not make concurrent syncs
    safe; it only stops one gateway from invalidating another's probe.

    Safe to force and safe to reuse: the fetch step rewrites it before any step
    reads it, so a value left by an earlier run -- or by an earlier process that
    happened to hold this PID -- can never be consumed.
    """
    return f"refs/kirocrew/sync-base-{os.getpid()}"


async def _prune_dead_sync_base_refs(repo: str) -> None:
    """Delete pinned base refs left behind by gateway processes that are gone.

    The PID in the ref name is what stops two gateways on one checkout from
    moving each other's pin, but nothing deletes the ref on the way out -- an
    ordinary restart strands it, and so does a killed run. Without this every
    gateway generation would leave one behind in the OPERATOR's own checkout,
    bounded only by ``pid_max`` and visible in ``git for-each-ref`` forever.

    A ref whose PID is still alive is LEFT ALONE. It may be a second gateway's
    live pin, and deleting that would reopen exactly the window the PID suffix
    exists to close -- so the prune is bounded by the number of live gateways,
    which is the real floor. Liveness goes through
    ``platform_compat.pid_exists``: a raw ``os.kill(pid, 0)`` is a liveness probe
    only on POSIX, and on Windows it TERMINATES the process it is asked about --
    which here would mean killing the very live gateway this is meant to spare.
    ``pid_exists`` also collapses "exists but we cannot signal it" into alive,
    which is the answer we want: unsure means do not touch.

    Best-effort throughout: this is housekeeping, and a sync must not fail
    because a stale ref could not be removed.
    """
    listed = await repository._git(repo, "for-each-ref", "--format=%(refname)", "refs/kirocrew/")
    if not listed:
        return
    mine = _sync_base_ref()
    prefix = "refs/kirocrew/sync-base-"
    for ref in listed.splitlines():
        ref = ref.strip()
        if not ref.startswith(prefix) or ref == mine:
            continue
        try:
            pid = int(ref[len(prefix) :])
        except ValueError:
            continue  # not one of ours to reason about
        if pid <= 0:
            # 0 and negatives are not PIDs: on POSIX they address the caller's
            # process group and every process respectively, so they must never
            # reach a liveness probe even a harmless one.
            continue
        if platform_compat.pid_exists(pid):
            continue  # owner still running -- may be another gateway's live pin
        await repository._git(repo, "update-ref", "-d", ref)


# --- sync (pull + build) ---
_SYNC_RID: str | None = None


async def _sync() -> dict:
    """Pull upstream main + rebuild. Single-flight via _SYNC_LOCK."""
    async with runtime._SYNC_LOCK:
        if _SYNC_RID is not None:
            async with runtime._RUNS_LOCK:
                run = runtime._RUNS.get(_SYNC_RID)
            if run and run["status"] == "running":
                # Guard against a stale "running" status: the worker task may
                # have exited (process reaped) but the status update has not yet
                # landed because the event loop has not yielded back to the
                # worker's finally block.  The correct liveness signal is the
                # subprocess handle: _ACTIVE_RUNS[rid] is (task, proc), and
                # proc.returncode is not None means the process has exited even
                # if the task's finally (cleanup_paths unlinking, status write)
                # has not completed.  task.done() is strictly LATER than the
                # status write, so checking it would miss the exact window.
                active = runtime._ACTIVE_RUNS.get(_SYNC_RID)
                if active is not None:
                    _task, proc = active
                    if proc is None or proc.returncode is None:
                        # Process still running (or not yet spawned) — genuine.
                        return {"ok": False, "error": "sync already running", "run_id": _SYNC_RID}
                    # Process exited but worker hasn't written status yet.
                    # Wait briefly for the task's finally block to land the
                    # status write + cleanup, rather than starting a new sync
                    # while the old worker is still unlinking temp files in the
                    # same worktree.
                    try:
                        await asyncio.wait_for(asyncio.shield(_task), timeout=2.0)
                    except asyncio.TimeoutError:
                        # Worker still cleaning up after 2s — refuse the new
                        # sync to avoid concurrent worktree writes.
                        return {"ok": False, "error": "sync already running", "run_id": _SYNC_RID}
                    except Exception:
                        pass  # task raised during cleanup; safe to proceed
                # Re-read status after giving the worker a chance to land it.
                async with runtime._RUNS_LOCK:
                    run = runtime._RUNS.get(_SYNC_RID)
                if run and run["status"] == "running":
                    # Still stale after the wait — reconcile defensively.
                    run["status"] = "done"
                    run["exit_code"] = run.get("exit_code") or -1
        return await _sync_start_locked()


def _venv_python(repo: str) -> Path | None:
    """Resolve the repo's own venv interpreter cross-platform (POSIX bin/,
    Windows Scripts/). Returns None when the venv is not provisioned."""
    for rel in ("bin/python", "Scripts/python.exe"):
        cand = Path(repo) / ".venv" / rel
        if cand.is_file():
            return cand
    return None


def _frontend_build_steps(
    *,
    npm_bin: str,
    git_bin: str,
    repo: str,
) -> list[tuple[list[str], str, dict, str]]:
    """The ``npm ci`` and ``npm build + stage`` steps, in order.

    Returns both steps unconditionally. Whether they RUN is decided at run time
    by the sync runner, which suppresses a step whose label is in its
    frontend-skip set when the trusted preflight step exits the frontend-skip
    verdict. That decision is made by the preflight -- the one point that runs
    after ``fetch`` pinned the incoming ref and before ``merge`` -- so it is made
    in the only window where "does the incoming ref touch the frontend?" has a
    correct answer. Deciding it here at assembly time would read the per-PID sync
    ref before fetch wrote it, so on a long-lived gateway's second sync it would
    compare against the PRIOR tip and skip a rebuild an incoming frontend change
    genuinely needed.

    The two steps suppress together because the runner keys on their labels off
    one preflight verdict, and they must: ``npm ci`` reifies the incoming
    lockfile and ``npm build + stage`` compiles the source, sharing the one
    precondition the verdict encodes.

    ``git_bin`` is the sync's trusted-bin absolute git path; it is threaded into
    ``build_and_stage`` so the read-only build-source fingerprint's git calls use
    a trusted binary rather than a PATH search.
    """
    return [
        ([npm_bin, "ci", "--prefix", "website"], "strict", runtime._build_env(), "npm ci"),
        # Build and stage as ONE step, holding the staging lock across both.
        # `npm run build` swaps a new tree into website/dist
        # (website/scripts/publish-dist.mjs), so a peer flow (the dashboard's own
        # update, pod provisioning) copying concurrently could copy half of each
        # — and a bundle's lazy chunks are not reachable from index.html, so no
        # post-hoc inspection detects that reliably.
        # Covering only the copy is not enough; the holder has to span the build.
        #
        # Run with THIS backend's interpreter, not the target checkout's: the
        # logic is revision-independent, while resolving it from the target would
        # make the step's very EXISTENCE contingent on the pulled revision
        # carrying build_and_stage, turning an older target into an ImportError
        # that fails the whole Pull+Build. The repo to build, npm's resolved
        # trusted path, and git's trusted path are passed in rather than
        # re-resolved.
        (
            [
                sys.executable,
                "-c",
                "import sys;from kiro_crew.frontend import build_and_stage;"
                "sys.exit(0 if build_and_stage(sys.argv[1], npm=sys.argv[2], "
                "git=sys.argv[3]) else 1)",
                repo,
                npm_bin,
                git_bin,
            ],
            "strict",
            runtime._build_env(),
            "npm build + stage",
        ),
    ]


#: Trusted loader for the sync-runner snapshot. A FIXED literal — nothing is
#: ever interpolated into it — so unlike the pre-extraction inline script it
#: carries no program logic beyond verify-and-exec. It exists because a file
#: on disk is mutable between staging and exec while argv is not: a sandboxed
#: same-UID process could rewrite the staged snapshot in that window and have
#: its code run UNSANDBOXED as the runner. The bootstrap travels in argv
#: (immutable once this process exists) together with the snapshot's expected
#: sha256, reads the file ONCE, hashes and compiles the SAME buffer (no
#: re-read between check and use), and refuses on mismatch. The runner's own
#: argv follows and is re-exposed as ``sys.argv``.
_SYNC_RUNNER_BOOTSTRAP = (
    "import hashlib, sys\n"
    "path, digest = sys.argv[1], sys.argv[2]\n"
    "with open(path, 'rb') as fh:\n"
    "    raw = fh.read()\n"
    "if hashlib.sha256(raw).hexdigest() != digest:\n"
    "    print('sync runner: snapshot does not match the staged source '\n"
    "          '(refusing to execute)', flush=True)\n"
    "    sys.exit(1)\n"
    "code = compile(raw, path, 'exec')\n"
    "sys.argv = [path] + sys.argv[3:]\n"
    "exec(code, {'__name__': '__main__', '__file__': path})\n"
)


async def _sync_start_locked() -> dict:
    """Start the sync run. Caller holds _SYNC_LOCK."""
    global _SYNC_RID  # noqa: F824 (assigned below after await)

    try:
        repo = repository._repo()
    except repository.RepoUnavailable as exc:
        # Sync answers a UI action, so the unresolved state degrades to the
        # same {"ok": False} shape every other refusal here uses.
        return {"ok": False, "error": str(exc)}
    await runtime._warm_build_path()
    head = await repository._git(repo, "symbolic-ref", "--short", "HEAD")
    if head is None:
        return {"ok": False, "error": "cannot determine checked-out branch (git failed)"}
    if head.strip() != repository.BASE_BRANCH:
        return {
            "ok": False,
            "error": (
                f"refusing to sync: primary checkout is on {head.strip()!r}, not {repository.BASE_BRANCH!r}"
            ),
        }

    remote = await repository._upstream_remote()

    # CRITICAL: pip must run with the TARGET repo's own venv interpreter.
    # ``sys.executable`` here is the app backend's venv (a feature worktree's)
    # — `pip install -e .` with it would re-point that venv's editable install
    # at MAIN_REPO, hijacking the running gateway's code identity on its next
    # restart (observed live: gateway silently became the main repo's code).
    target_py = _venv_python(repo)
    if target_py is None:
        return {
            "ok": False,
            "error": (
                "main checkout has no .venv — provision it first "
                f"(expected under {Path(repo) / '.venv'})"
            ),
        }
    # Both binary lookups stat the filesystem (`_trusted_bin` walks the trusted
    # dirs; `_toolchain_bin` adds a `shutil.which` over the node bin dirs, which
    # may be NFS-backed). The console-script probe opens files in the target
    # venv, and the origin probe RUNS that interpreter. Resolve them together on
    # the executor so /api/sync cannot stall the gateway's requests and liveness
    # behind a directory scan or a subprocess.
    loop = asyncio.get_running_loop()
    git_bin, npm_bin, locked_scripts, venv_origin = await loop.run_in_executor(
        subprocess_executor(),
        lambda: (
            runtime._trusted_bin("git"),
            runtime._toolchain_bin("npm"),
            dep_sync.locked_console_scripts(target_py),
            dep_sync.installed_package_origin(target_py),
        ),
    )
    if git_bin is None:
        # Same failure class as the discovery path: name the remedy in the
        # response; the log records the event. The searched dirs are the
        # static _TRUSTED_BIN_DIRS constant (and the discovery-path warning
        # already prints them when it fires) — repeating them here trips
        # CodeQL's name-based secret heuristic on _TRUSTED_PATH for no
        # diagnostic gain.
        runtime.logger.warning(
            "dev-fleet: %s'git' — no trusted-dir candidate passed vetting "
            "and no override is set",
            runtime._UNRESOLVED_TOOL_PREFIX,
        )
        return {"ok": False, "error": runtime._unresolved_tool_message("git")}
    if npm_bin is None:
        # Drop the memoized resolution so the remedy this message advertises
        # actually works. `node_bin_dirs()` is lru_cached and `_BUILD_PATH_CACHE`
        # is set once per process, so in a long-lived gateway a user who ran
        # ensure-node.sh and hit Pull+build again would get this SAME error --
        # the marker file is never re-read. Invalidating on the failure path
        # makes the retry fresh. Only on failure: a successful resolution is
        # worth keeping cached, and this path is user-initiated, not a loop.
        runtime._invalidate_toolchain_cache()
        return {
            "ok": False,
            "error": (
                "npm not found. Kiro Crew looks for a Node toolchain in "
                "<data-home>/node-bin-dir (written by ensure-node.sh), then in "
                "mise / asdf / nvm / fnm / volta install dirs, then in "
                f"{runtime._TRUSTED_PATH}. Fix: run `bash ensure-node.sh` in the main "
                "checkout and press Pull + Build again — no restart needed. To point "
                "at a toolchain by hand instead, set "
                "KIROCREW_NODE_BIN_DIR=/abs/path/to/node/bin in the gateway's "
                "service environment; that one does need a restart, because a "
                "running process cannot see a new environment variable."
            ),
        }
    # A locked console script does not have to mean the sync cannot happen.
    #
    # pip cannot replace a running executable on Windows, and its uninstall is
    # not atomic: it renames the dist-info aside and deletes the editable `.pth`
    # before it reaches the locked script, rolling back neither. So
    # `pip install -e .` must not run against a venv serving this gateway.
    #
    # It does not have to run at all, though. An editable install needs no
    # reinstall for a source change -- `src` is already on `sys.path`, so merged
    # code is live the moment the merge lands. The only thing the reinstall was
    # still buying is a dependency a new revision added, and installing a
    # dependency never touches the project's own console script.
    #
    # The substitute keeps the SAME shape as the reinstall it stands in for:
    # `fetch -> merge -> install the project's requirements`, in that order, with
    # the project itself left out. Deviating from that shape -- installing before
    # the merge so a failure cannot strand a merged checkout -- was tried and
    # abandoned: it requires proving in advance that the merge cannot fail, which
    # needs a growing set of preconditions that still cannot be complete. A failed
    # install after a landed merge is what every other platform already does when
    # its reinstall step fails.
    #
    # The substitute step runs with THIS backend's interpreter for the same
    # reason the build+stage step does: the logic is revision-independent, so
    # resolving it from the target would make the step's very EXISTENCE
    # contingent on the pulled revision carrying dep_sync.
    #
    # It runs a pre-merge SNAPSHOT of that module, by path, and neither half of
    # that is incidental. `-m kiro_crew...dep_sync` would import the module from
    # the working tree AFTER the merge has landed, dragging in the whole package
    # __init__ chain with it -- so a revision that raises the `requires-python`
    # floor and uses newer syntax anywhere in that chain would die with a
    # SyntaxError while being parsed, and the floor refusal that exists for
    # exactly that revision could never run. Copying the module first and
    # executing the copy keeps the only parsed file one this interpreter has
    # already imported, which makes the refusal reachable in the case it is
    # written for. Running it by path rather than by module name is what keeps
    # the package chain out of it; the module imports nothing but the standard
    # library, so it needs no package context.
    #
    # This mirrors pip rather than deviating from it: the INSTALLED pip reads the
    # merged project's metadata, and an old pip refusing a too-new project is the
    # behaviour being stood in for here.
    # The fetch pins the tip it brought into this process's own base ref as well
    # as updating the remote-tracking ref, so the probe below and the merge here
    # consume ONE commit even though the refresher keeps fetching underneath
    # them. Resolved once here so every step of this run names the same ref, and
    # refs stranded by gateway processes that are gone are cleared first so they
    # do not accumulate in the operator's checkout.
    sync_base_ref = _sync_base_ref()
    await _prune_dead_sync_base_refs(str(repo))
    fetch_step = (
        [
            git_bin,
            "fetch",
            remote,
            repository.BASE_BRANCH,
            f"+refs/heads/{repository.BASE_BRANCH}:{sync_base_ref}",
        ],
        "standard",
        runtime._build_env(with_credentials=True),
        "Pull",
    )
    # Labelled distinctly from the fetch: with the preflight between them, two
    # steps both called "Pull" would render as Pull -> Preflight -> Pull and read
    # like the run had restarted.
    merge_step = (
        [git_bin, "merge", "--ff-only", sync_base_ref],
        "strict",
        runtime._build_env(),
        "Merge",
    )
    # The venv must be an install OF this checkout before either install step
    # runs, and that is true of the reinstall just as much as the substitute.
    #
    # `<repo>/.venv` is only where the interpreter was FOUND; it says nothing
    # about what it is an install of. When it serves a different checkout,
    # `pip install -e .` silently repoints its editable install at this repo and
    # the other checkout's gateway becomes this code on its next restart -- the
    # same hijack the target_py resolution above exists to prevent, arriving by
    # the other direction. The substitute has always refused this (dep_sync's own
    # first check); the reinstall branch did not, so the safer path was the only
    # guarded one. Checking here covers both, on every platform.
    foreign = dep_sync.venv_not_mapped_to(venv_origin, Path(repo))
    if foreign:
        return {
            "ok": False,
            "error": (
                f"refusing to sync: {foreign}. Give this checkout its own editable "
                "install, or run the sync from the checkout that venv serves."
            ),
        }
    dep_sync_snapshot: Path | None = None
    # Whether the frontend half runs at all is decided BEFORE the step list is
    # assembled, because the preflight belongs between fetch and merge and there
    # is nothing to preflight when the frontend half is skipped (see the edition
    # rationale where the build steps are appended).
    frontend_half = not frontend.edition_configured()
    # The preflight goes AFTER fetch and BEFORE merge, and that position is the
    # whole point rather than a detail.
    #
    # `npm ci` deletes website/node_modules before installing, so a registry
    # that refuses one package does not merely fail the sync — it empties the
    # tree and leaves the checkout with new source, a new lockfile and no
    # frontend dependencies. The lockfile that will be installed is knowable as
    # soon as fetch lands (it is in the fetched ref), and fetch moves only
    # remote refs, so between the two is the one moment where the question can
    # be asked while a refusal still costs nothing.
    #
    # The runner is fail-fast, so no extra refusal plumbing is needed: a failing
    # preflight step stops the run before the merge step is reached.
    #
    # Only ONE preflight is built, and it runs in the credential-free build tier
    # like every other npm invocation here — `_run_cmd` would have been the
    # obvious host for it but it overwrites PATH with _TRUSTED_PATH
    # unconditionally, and npm's wrapper needs `node` to resolve BY NAME, which
    # only _build_env()'s node-augmented PATH provides.
    preflight_steps: list[tuple[list[str], str, dict, str]] = []
    preflight_cleanup: list[str] = []
    if frontend_half:
        # Executed as a SNAPSHOT by path, never imported from the checkout.
        #
        # `-I` alone was not enough. It drops the cwd from sys.path, which closes
        # the untracked-`kiro_crew/`-at-the-checkout-root shadow -- but the
        # install is EDITABLE, so `import kiro_crew...` still resolves into the
        # checkout's own `src/` tree. This step is trusted to assert a failure
        # cause precisely BECAUSE its binary is ours, so importing a tree that is
        # itself the thing being synced put the boundary's own key under the mat.
        #
        # Copying the module out and running that copy is the same move this
        # function already makes for dep_sync a few lines below, and it works
        # here for a reason worth stating: npm_preflight imports nothing but the
        # stdlib, so a by-path snapshot needs no package context at all.
        #
        # mkdtemp rather than a fixed path, for the reason the dep_sync snapshot
        # gives: executing a script by path would put its DIRECTORY on sys.path,
        # so a predictable one would let anything dropped beside the snapshot
        # shadow a stdlib module it imports. mkdtemp is unguessable and 0o700 --
        # and `-I` removes that directory from sys.path as well, so neither the
        # checkout nor the snapshot's own neighbours can reach the interpreter.
        if runtime._PREFLIGHT_SOURCE is None:
            return {
                "ok": False,
                "error": (
                    "the dependency preflight's own source could not be read at "
                    "startup, so there is nothing trustworthy to run it from — "
                    "reinstall the gateway from a source checkout"
                ),
            }
        snap_dir: Path | None = None
        try:
            snap_dir = Path(tempfile.mkdtemp(prefix="kirocrew-npm-preflight-"))
            snap = snap_dir / "npm_preflight.py"
            # The BYTES captured at import, not a copy of the file as it is now:
            # copying at sync time would execute whatever had been written to the
            # checkout since this gateway started.
            snap.write_bytes(runtime._PREFLIGHT_SOURCE)
        except OSError as exc:
            # mkdtemp can succeed and the COPY still fail, and nothing has
            # registered the directory for the run's cleanup yet at this point --
            # so remove it here or a failed sync leaks one temp directory every
            # time, which is the same litter the pinned base refs were just
            # taught to avoid.
            if snap_dir is not None:
                shutil.rmtree(snap_dir, ignore_errors=True)
            # A full or unwritable TMPDIR must not escape as a 500: the sync
            # answers a UI action, so it degrades to the same {"ok": False} shape
            # every other refusal here uses. Refusing is also the SAFE outcome --
            # without a probe there is nothing to trust, and the alternative
            # (proceed unprobed) is exactly the destructive path this change
            # exists to prevent.
            return {
                "ok": False,
                "error": (
                    "could not stage the dependency preflight: "
                    f"{exc.strerror or exc} — check BOTH the free bytes (df -h) "
                    "and the free file count (df -i) on the temporary directory, "
                    "because either budget can be exhausted while the other still "
                    "looks healthy — then free room and press Pull + Build again"
                ),
            }
        # File before directory: the run's cleanup unlinks each entry and falls
        # back to rmdir, which only succeeds on an empty one.
        preflight_cleanup = [str(snap), str(snap_dir)]
        preflight_steps = [
            (
                [
                    sys.executable,
                    "-I",
                    "-X",
                    "utf8",
                    str(snap),
                    "--git",
                    git_bin,
                    "--npm",
                    npm_bin,
                    "--repo",
                    str(repo),
                    "--ref",
                    sync_base_ref,
                    # Asks this step to exit EXIT_FRONTEND_SKIP (a reserved code
                    # trusted only from this step) when the incoming ref proves
                    # the frontend install/build is already present, so the
                    # runner suppresses the later npm ci and build+stage steps.
                    # A flag, not a value -- the verdict travels as this step's
                    # exit code and lives in runner state, never a file another
                    # same-UID step could forge.
                    "--emit-frontend-skip",
                ],
                "strict",
                runtime._build_env(),
                runtime._PREFLIGHT_LABEL,
            )
        ]
    if locked_scripts:
        runtime.logger.info(
            "dev-fleet: %s locked by a running process; substituting a "
            "dependency-only sync for the editable reinstall",
            ", ".join(locked_scripts),
        )
        # mkdtemp, not a fixed path under the system temp dir: executing a script
        # by path puts its DIRECTORY on sys.path, so a shared or predictable
        # directory would let anything dropped beside the snapshot shadow a
        # stdlib module the snapshot imports. mkdtemp is unguessable and 0o700.
        snapshot_dir = Path(tempfile.mkdtemp(prefix="kirocrew-dep-sync-"))
        dep_sync_snapshot = snapshot_dir / "dep_sync.py"
        shutil.copyfile(dep_sync.__file__, dep_sync_snapshot)
        steps = [
            fetch_step,
            *preflight_steps,
            merge_step,
            (
                [sys.executable, str(dep_sync_snapshot), str(repo), str(target_py)],
                "strict",
                runtime._build_env(),
                "pip install",
            ),
        ]
    else:
        steps = [
            fetch_step,
            *preflight_steps,
            merge_step,
            (
                [str(target_py), "-m", "pip", "install", "-e", "."],
                "strict",
                runtime._build_env(),
                "pip install",
            ),
        ]
    raw_steps: list[tuple[list[str], str, dict, str]] = steps
    # The whole FRONTEND half of the sync is skipped on an edition checkout.
    #
    # The build runs under _build_env(), whose allowlist (_SAFE_ENV_KEYS) drops
    # KIROCREW_EDITION_DIR and KIROCREW_ALLOW_EDITION, so on an edition
    # composition root `npm run build` can only compile the STOCK SPA -- and it
    # publishes that into website/dist. An edition checkout serves a private
    # copy, but one whose static/dist links to website/dist (no private edition
    # copy, or one that could not be made) would have the build alone replace the served edition dashboard
    # with upstream's, with or without a staging step. Skipping the
    # build is therefore the only way to make this safe, and it costs an edition
    # nothing: the only artifact this path could produce for it is a stock SPA it
    # must never serve. It is the same call frontend's own
    # edition_sources_missing() guard already makes -- leave the shipped bundle
    # alone rather than degrade it.
    #
    # This backend is a SEPARATE process started with apps.registry.minimal_env(),
    # which strips KIROCREW_EDITION_DIR, so the guard would read "stock" on every
    # install and never fire. apps/backend.py therefore propagates that one var
    # explicitly, the same way it already propagates KIROCREW_PROJECT_DIR.
    if not frontend_half:
        runtime.logger.info(
            "dev-fleet: skipping the frontend build and dist staging -- this is "
            "an edition checkout and the sync build cannot recompose the "
            "edition; the shipped bundle is left in place"
        )
    else:
        # Append the reinstall AND the rebuild. Whether they RUN is decided at
        # run time by the sync runner: it suppresses these two labels when the
        # preflight step (which runs after fetch pinned the incoming ref and
        # before merge) exits the frontend-skip verdict -- the incoming ref
        # changed nothing under website/ and node_modules is populated. A
        # backend-only sync -- the common case, since most syncs move only
        # Python -- otherwise pays a full `npm ci` (which DELETES node_modules
        # before reinstalling from an unchanged lockfile) and a full vite build
        # that reproduces a byte-identical bundle. The decision is made
        # post-fetch, carried by the preflight's trusted exit code rather than
        # in-process here where the ref is not yet fetched.
        raw_steps += _frontend_build_steps(npm_bin=npm_bin, git_bin=git_bin, repo=str(repo))
    cleanups: list[str] = []
    # The preflight's snapshot is removed with the run's other temporaries. It is
    # registered here rather than left behind: a leaked mkdtemp per sync is how
    # the dependency-only path already accumulates one, and repeating that would
    # litter the operator's temp directory on every Pull + Build.
    cleanups.extend(preflight_cleanup)
    if dep_sync_snapshot is not None:
        # File first, then its directory: the cleanup loop removes entries in
        # order, and a directory cannot go until it is empty.
        cleanups += [str(dep_sync_snapshot), str(dep_sync_snapshot.parent)]
    wrapped_steps: list[dict] = []
    for argv, mode, base_env, label in raw_steps:
        try:
            argv = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), runtime._with_config_hooks_off, argv, str(repo), base_env
            )
        except ConfigHookScanError as exc:
            for _p in cleanups:
                try:
                    os.unlink(_p)
                except (IsADirectoryError, PermissionError):
                    try:
                        os.rmdir(_p)
                    except OSError:
                        pass
                except OSError:
                    pass
            return {"ok": False, "error": f"git hook config refused: {exc}"}
        w_argv, w_env, cleanup = await shielded_prepare_off_loop(
            functools.partial(sandboxed_spawn_argv, argv, mode, env=base_env),
            executor=subprocess_executor(),
        )
        if cleanup:
            cleanups.append(cleanup)
        wrapped_steps.append({"argv": w_argv, "env": w_env, "label": label})
    for step in wrapped_steps:
        if step["label"] == "npm ci":
            # `npm ci` deletes node_modules BEFORE it installs, and a tree it
            # emptied is the one artifact of a failed sync that cannot be
            # rebuilt without the registry — which is exactly what is unavailable
            # when this step fails. So the runner moves it aside first and puts it
            # back if the step does not succeed, making the failure a no-op
            # instead of damage. A separate "restore" step could not do this: the
            # runner is fail-fast, so anything after a failed step never runs.
            step["stash"] = str(Path(repo) / "website" / "node_modules")
    # The runner is a SNAPSHOT of :mod:`sync_runner`, staged into a private
    # mkdtemp and run BY PATH with `-I`. Running `-m kiro_crew...sync_runner`
    # would import it from the working tree AFTER the merge has landed (and drag
    # the package __init__ chain in with it), and interpolating source into a
    # `-c` string is what this module replaced. mkdtemp is unguessable and
    # 0o700; the steps JSON lives in the SAME dir and is passed as a PATH
    # argument, never interpolated into source.
    if runtime._SYNC_RUNNER_SOURCE is None:
        return {
            "ok": False,
            "error": (
                "the sync runner's own source could not be read at startup, so "
                "there is nothing trustworthy to run it from — reinstall the "
                "gateway from a source checkout"
            ),
        }
    runner_dir: Path | None = None
    try:
        runner_dir = Path(tempfile.mkdtemp(prefix="kirocrew-sync-runner-"))
        runner_snap = runner_dir / "sync_runner.py"
        # The BYTES captured at import, not a copy of the file as it is now.
        runner_snap.write_bytes(runtime._SYNC_RUNNER_SOURCE)
        # Pinned in argv below (same contract as the steps digest): the staged
        # file is mutable between now and exec, argv is not, so the bootstrap
        # can refuse a snapshot rewritten in that window.
        runner_sha256 = hashlib.sha256(runtime._SYNC_RUNNER_SOURCE).hexdigest()
        steps_json_path = runner_dir / "steps.json"
        # The step list travels as a FILE, not embedded in argv or source: a
        # non-ASCII checkout path in a step's argv or env then cannot break the
        # program that reads it, and nothing worktree-controlled is interpolated
        # into code.
        steps_payload = json.dumps(wrapped_steps).encode("utf-8")
        steps_json_path.write_bytes(steps_payload)
        # Pinned in argv below: argv is immutable once the runner process
        # exists, so the runner can verify the file it reads is the manifest
        # THIS gateway staged -- a rewrite of steps.json between staging and
        # startup fails the digest check and nothing runs.
        steps_sha256 = hashlib.sha256(steps_payload).hexdigest()
    except OSError as exc:
        # A full or unwritable TMPDIR must not escape as a 500, and nothing has
        # registered this dir for the run's cleanup yet -- remove it here or a
        # failed sync leaks one temp dir every time. Refusing is also the SAFE
        # outcome: without a runner there is nothing to drive the steps.
        if runner_dir is not None:
            shutil.rmtree(runner_dir, ignore_errors=True)
        return {
            "ok": False,
            "error": (
                "could not stage the sync runner: "
                f"{exc.strerror or exc} — check BOTH the free bytes (df -h) and the "
                "free file count (df -i) on the temporary directory, because either "
                "budget can be exhausted while the other still looks healthy — then "
                "free room and press Pull + Build again"
            ),
        }
    # Files before their directory: the run's cleanup unlinks each entry and
    # falls back to rmdir, which only succeeds on an empty one.
    cleanups += [str(runner_snap), str(steps_json_path), str(runner_dir)]
    # `-I` isolates the interpreter (drops cwd from sys.path and ignores env
    # vars that alter it), the same hardening the preflight snapshot uses; the
    # snapshot dir itself is removed from sys.path by `-I` too. The runner is
    # executed THROUGH the fixed bootstrap above, which verifies the staged
    # snapshot's bytes against the argv-pinned digest before compiling them —
    # closing the staging→exec mutation window. The reserved set and the one
    # trusted label are passed IN so the runner imports nothing from
    # kiro_crew -- what it does cannot change with the revision merged under it.
    cmd = [
        sys.executable,
        "-I",
        "-c",
        _SYNC_RUNNER_BOOTSTRAP,
        str(runner_snap),
        runner_sha256,
        str(steps_json_path),
        str(repo),
        "--reserved",
        ",".join(str(c) for c in sorted(npm_preflight.RESERVED_EXIT_CODES)),
        "--preflight-label",
        runtime._PREFLIGHT_LABEL,
        "--exit-tree-ambiguous",
        str(npm_preflight.EXIT_TREE_AMBIGUOUS),
        "--exit-restore-failed",
        str(npm_preflight.EXIT_RESTORE_FAILED),
        "--exit-frontend-skip",
        str(npm_preflight.EXIT_FRONTEND_SKIP),
        # The two labels the runner suppresses when the preflight asserts the
        # frontend-skip verdict. They MATCH the labels _frontend_build_steps
        # gives those steps; kept literal here rather than derived so the runner
        # (which imports nothing from kiro_crew) is told exactly what to skip.
        "--frontend-labels",
        "npm ci,npm build + stage",
        "--steps-sha256",
        steps_sha256,
    ]
    rid = await runtime._start_run(
        runtime._SYNC_RUN_LABEL,
        cmd,
        env=runtime._build_env(),
        cleanup_paths=cleanups,
        # A dependency sync writes pip installs into the repo .venv and
        # `npm ci` into website/node_modules — both inside trees `du -sm`
        # measures — so it invalidates the disk cache the same way a
        # provision run does.
        on_finish=fleet_state._disk_invalidate,
    )
    _SYNC_RID = rid
    return {"ok": True, "run_id": rid}
