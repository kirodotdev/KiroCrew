"""Apply an available update unattended.

A git checkout is fetched, reset, rebuilt and its dependencies synced; a managed-venv
install gets the new wheel built beside the live venv. Each child process is spawned
in its own session and killed and reaped on timeout or cancellation.

Orchestrator fields read: ``_REQUIRED_DEPS``, ``_wheel_update_noticed``,
``dashboard_state``.
Orchestrator fields written: ``_pending_update_mandatory``,
``_pending_update_mandatory_key``, ``_pending_update_respawn``,
``_pending_update_version``, ``_update_apply_deferred``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import contextlib
    import importlib
    import importlib.util
    import os
    import sys

    from kiro_crew.slack.gateway import (
        GatewayOrchestrator,
        Path,
        _pinned_kiro_cli,
        build_frontend_async,
        commits_ahead,
        dep_sync,
        fetch_and_pin_upstream,
        git_command_env,
        hidden_worktree_edits,
        is_bundled_kiro_cli,
        is_primary_branch,
        kiro_crew,
        loggable_path,
        logger,
        platform_compat,
        redact_credentials,
        redact_exfiltration_urls,
        redact_log_via_context,
        repo_exec_config_reason,
        resolve_remote_url,
        restart_command_hint,
        subprocess_executor,
        tracks_upstream,
        update_blocked_reason,
        update_ownership,
    )


async def _auto_apply_update(self: GatewayOrchestrator, *, mandatory: bool = False) -> None:
    """Auto-apply: fetch, reset to remote, rebuild frontend, pip install, restart.

    Uses ``git fetch`` + ``git reset --hard`` instead of ``git pull``
    so local tracked-file edits never cause merge conflicts.
    Untracked files (task specs, notes) are untouched by reset.

    ``mandatory`` says a policy ``min_version`` floor drove this apply rather
    than the voluntary auto-update path. It changes nothing about the git
    work; it only makes a no-op outcome VISIBLE to the operator. A voluntary
    apply that finds no diff can settle silently, but a floor-mandated apply
    that finds nothing to do means the host is below the floor AND cannot move
    — the operator needs to see that, not a cleared progress bar.

    The public OSS flow is the same one used by ``kirocrew update`` and the
    dashboard update endpoint: git reset to origin → build + stage the
    in-tree ``website/`` frontend → ``pip install -e .`` → ``os.execv``
    restart. The optional ``kiro-cli`` backend is updated only when present.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj:
        return
    # Timeout/cancel discipline for every asyncio spawn below: the helper owns
    # the kill-the-tree + bounded-reap contract for an asyncio child. (The
    # managed-venv engine's build children are synchronous ``Popen`` children
    # and use ``platform_compat.kill_popen_tree`` instead.)
    from kiro_crew.platform.update_governance import unattended_apply_allowed
    from kiro_crew.platform.update_provider import _kill_and_reap

    # Loaded before the reinstall below for the same reason as the provider
    # path: `pip install -e .` rewrites the package this process imports
    # from, so the resolver must already be in memory when the restart
    # needs it. Called only after the install succeeded (see the exec below).
    from kiro_crew.platform.wheel_engine import respawn_executable

    owned = contextlib.ExitStack()
    # Set once the reset may have moved the tree, cleared once its
    # dependencies are synced: in between, a relaunch could die at import.
    tree_moved = False
    try:
        # Every git call below reads a tree an agent can write, and several of
        # them (`status`, `diff`, `reset`) will EXEC a program the repository
        # names in its own config. Bound once here, ahead of the first spawn,
        # so the whole sequence is covered and a later-added command cannot
        # quietly opt out of it.
        #
        # A redirected work tree is handled separately, by the
        # `repo_exec_config_reason` refusal below: git ignores a
        # `core.worktree` supplied through the environment, so it cannot be
        # pinned here.
        #
        # `git_command_env` BUILDS the environment rather than merging over
        # `os.environ`, because an inherited `GIT_DIR` has to be ABSENT and a
        # merge can only add keys. Left in place it would point every call
        # below at unrelated metadata while `cwd` still says `proj`.
        #
        # Built off the loop: it reads the operator's `safe.directory` values
        # with a short `git config` child (see `_protected_safe_directories`).
        _git_env = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), git_command_env
        )

        # `git` itself is resolved OFF `PATH`. A gateway's `PATH` can lead
        # with an agent-writable directory (a worktree venv's `bin`,
        # `~/.local/bin`), so a bare `"git"` lets a planted shim run — and on
        # THIS path what git reports decides which code is installed and
        # re-executed, so the shim would not merely lie, it would choose the
        # payload. `AGENTS.md` already requires this for system tools;
        # `cli_doctor` already did it for git.
        #
        # Resolved ONCE here rather than per call, so every step below runs
        # the same binary: re-resolving per spawn would leave a window for the
        # answer to change mid-sequence.
        _git = platform_compat.trusted_git_bin()
        if _git is None:
            logger.warning(
                "Auto-update: skipping — no trustworthy `git` outside PATH. "
                "Run `kirocrew update` to apply this manually."
            )
            if self.dashboard_state:
                self.dashboard_state.clear_update_progress()
                self.dashboard_state.push_refresh("update_available")
            return
        # Narrowed once for the nested pre-check, where the None test above
        # does not carry over.
        git_bin: str = _git

        branch_proc = await asyncio.create_subprocess_exec(
            _git,
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
            cwd=proj,
            env=_git_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # Own process group (POSIX; no-op on Windows) so a timeout or
            # cancellation kill reaches the whole tree, not just the direct
            # child. Every spawn in this method carries the same discipline:
            # on TimeoutError/CancelledError, kill the tree
            # and reap under a bound via the shared `_kill_and_reap`, then
            # re-raise so the outer handler keeps its current behaviour —
            # without this the child is ABANDONED on timeout, not stopped.
            start_new_session=platform_compat.IS_POSIX,
        )
        try:
            branch_out, _ = await asyncio.wait_for(branch_proc.communicate(), timeout=10)
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            await _kill_and_reap(branch_proc)
            raise
        if branch_proc.returncode != 0:
            logger.error("Auto-update: could not determine current branch")
            return
        branch = branch_out.strip().decode() if branch_out else ""

        # Only a PRIMARY branch is auto-updated: a feature or beta branch
        # needs a deliberate `kirocrew update`, and a detached HEAD has no
        # branch to fast-forward at all.
        #
        # This gate read `branch != "mainline"` — inherited verbatim from the
        # internal repo whose primary line is named that — so on this repo,
        # whose primary line is `main`, it matched nothing and returned at
        # `logger.debug`. Every git checkout (the documented `install.sh`
        # path) therefore never auto-updated, and said so nowhere.
        #
        # `is_primary_branch` reads a reviewed allowlist and nothing else, so
        # no local git ref can steer or veto this decision. See its docstring
        # — this is also the path a mandatory `min_version` floor drives.
        if not is_primary_branch(branch):
            logger.info(
                "Auto-update: skipping — %s is not a primary branch",
                branch or "detached HEAD",
            )
            return

        # A content filter or textconv driver is named BY THE REPOSITORY, so
        # there is no fixed key to pin and `_git_env` cannot reach it. Refuse
        # the unattended run rather than execute it; the operator still has
        # `kirocrew update`, where a human is deciding.
        exec_config = await asyncio.get_running_loop().run_in_executor(
            None, lambda: repo_exec_config_reason(proj)
        )
        if exec_config:
            logger.warning(
                "Auto-update refused: %s, which git would run during the update",
                exec_config,
            )
            if self.dashboard_state:
                self.dashboard_state.push_refresh("update_available")
            return

        # The availability check compares HEAD against `@{u}` (the TRACKED
        # upstream) while this applies `origin/<branch>`. On a fork checkout
        # whose branch tracks `upstream` and whose `origin` is a stale fork,
        # those are different refs: the check sees the canonical remote move
        # ahead and the reset below would discard commits. Only reset when the
        # branch tracks the remote this actually fetches and pins.
        if not await asyncio.get_running_loop().run_in_executor(
            None, lambda: tracks_upstream(proj, branch)
        ):
            logger.info(
                "Auto-update: skipping — %s does not track origin, and the "
                "update check measures against its tracked upstream",
                branch,
            )
            if self.dashboard_state:
                self.dashboard_state.push_refresh("update_available")
            return

        # Source pin, checked before the fetch. This is the most privileged
        # update path in the product — no auth, no click, `git reset --hard`
        # + pip + execv on boot — so a blocked host must not touch its tree.
        blocked = await asyncio.get_running_loop().run_in_executor(
            # `_git_env` is the env the fetch below runs with, so the pin judges
            # the URL that isolated fetch will connect to.
            None,
            lambda: update_blocked_reason(
                resolve_remote_url(proj, fetch_env=_git_env, remote="origin")
            ),
        )
        if blocked:
            logger.warning("Auto-update refused: %s", blocked)
            if self.dashboard_state:
                self.dashboard_state.clear_update_progress()
            return

        if self.dashboard_state:
            self.dashboard_state.push_update_progress("pulling", "Fetching latest changes…")

        # The fetch's ref write is the one git operation here a repo-defined
        # hook can fire on (`reference-transaction` -> `hook.<name>.command`,
        # which no `-c` pin or fetch flag closes), so it runs through the shared
        # isolated helper: the fetch happens in a git dir the gateway owns and no
        # repository hook exists there, while objects land in this checkout.
        #
        # `target` is the OID the helper fetched, used for the comparison AND the
        # reset below. It comes straight from the fetch, so it cannot be moved by
        # a concurrent fetch between decision and reset, and the old
        # `refs/remotes/origin/<branch>` tag-vs-ref ambiguity does not arise: no
        # ref name is resolved to get it. The helper writes no ref in the
        # checkout, so its remote-tracking ref stays as the last plain fetch left
        # it.
        fetched = await fetch_and_pin_upstream(
            proj, "origin", branch, git=_git, env=_git_env, timeout=60
        )
        if not fetched:
            # Refused source URL, a failed or timed-out fetch, or an unreadable
            # tip -- the helper logs which. Stop without touching the tree.
            logger.warning("Auto-update: skipping — could not fetch origin/%s", branch)
            if self.dashboard_state:
                self.dashboard_state.clear_update_progress()
            return
        # Bound as a plain ``str`` (not ``str | None``) so the closures and
        # spawns below that capture it type-check: mypy does not carry the
        # ``if not fetched`` narrowing into a lambda.
        target: str = fetched

        # Check if there are actually new commits
        diff_proc = await asyncio.create_subprocess_exec(
            _git,
            "diff",
            "HEAD",
            target,
            "--quiet",
            cwd=proj,
            env=_git_env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=platform_compat.IS_POSIX,
        )
        try:
            await asyncio.wait_for(diff_proc.wait(), timeout=10)
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            await _kill_and_reap(diff_proc)
            raise
        if diff_proc.returncode == 0:
            # No diff — the working tree already matches the fetched tip. A
            # MANDATED apply reaches this only once the gate upstream has
            # confirmed a newer ``__version__`` is available, so an identical
            # tree means the code is already in place and this process is
            # still running the build from before it; a restart is what
            # remains. The ``restarting`` step is reserved for the moment the
            # gateway is about to exec itself: it arms the SPA's reload latch
            # (``bundleReload.ts``), which the next reconnect consumes to
            # reload the tab. No exec happens here, so pushing it would arm a
            # latch that reloads over an unrelated later reconnect and leave
            # the progress bar stuck mid-"restarting". Clear the bar and
            # refresh the update badge instead — the badge is the same signal
            # the no-newer-build notify path raises, and it surfaces the
            # pending mandatory update without faking a restart.
            if self.dashboard_state:
                self.dashboard_state.clear_update_progress()
                if mandatory:
                    self.dashboard_state.push_refresh("update_available")
            if mandatory:
                logger.warning(
                    "Version compliance: a mandatory update found the fetched tip "
                    "already checked out — a restart is pending to run it"
                )
            return

        async def _refused_before_reset() -> bool:
            """Every refusal ahead of the reset; True when one refused and reported.

            A checkout is a live tree, and `reset --hard` is not recoverable
            from, so these are re-read rather than trusted from the earlier
            availability pass and config probe. Called twice, around the
            memory copy: see there.
            """
            # 1. Local commits. `git status --porcelain` below reports
            #    working-tree edits, NOT commits, and the `git diff` above is
            #    satisfied by any difference in either direction — so a checkout
            #    that is ahead of origin passes both and then loses those commits.
            # Counted against `target` — the OID the reset will use — not against
            # `origin/<branch>`. A ref is re-resolved per command, so a concurrent
            # fetch could advance it, make this read zero against the new tip, and
            # leave the reset discarding commits relative to the old one.
            ahead = await asyncio.get_running_loop().run_in_executor(
                None, lambda: commits_ahead(proj, target)
            )
            if ahead != 0:
                logger.warning(
                    "Auto-update: skipping — %s is ahead of origin/%s by %s commit(s); "
                    "a reset would discard them. Run `kirocrew update` to decide.",
                    branch,
                    branch,
                    "an unknown number of" if ahead is None else ahead,
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return True

            # 2. The work tree, and the repo-named exec drivers, re-read after the
            #    fetch. The earlier probe is a check-then-use otherwise: config
            #    rewritten in between would redirect this reset, or hand the
            #    checkout's own driver to the command that performs it.
            exec_config_now = await asyncio.get_running_loop().run_in_executor(
                None, lambda: repo_exec_config_reason(proj)
            )
            if exec_config_now:
                logger.warning("Auto-update refused before reset: %s", exec_config_now)
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return True

            # 3. Uncommitted tracked edits. REFUSE, like the two checks above —
            #    logging a warning and resetting anyway would make an unattended
            #    boot-time update the one code path that could silently destroy a
            #    developer's uncommitted work. `reset --hard`
            #    is not recoverable and nothing here has the standing to make
            #    that trade on the developer's behalf: the count check one screen
            #    up already refuses for COMMITTED work and defers to `kirocrew
            #    update`, and uncommitted work is the strictly more fragile case
            #    (a discarded commit is at least recoverable from the reflog; an
            #    uncommitted edit is gone). The manual path keeps the destructive
            #    semantics, because there a human chose them.
            #
            #    Untracked files are excluded: `reset --hard` preserves them, so
            #    task specs and notes are not a reason to refuse.
            status_proc = await asyncio.create_subprocess_exec(
                git_bin,
                "status",
                "--porcelain",
                cwd=proj,
                env=_git_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=platform_compat.IS_POSIX,
            )
            try:
                status_out, _ = await asyncio.wait_for(status_proc.communicate(), timeout=10)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                await _kill_and_reap(status_proc)
                raise
            if status_proc.returncode != 0:
                # Cannot prove the tree is clean, and the next step is
                # irreversible — treat an unreadable status as dirty.
                logger.warning(
                    "Auto-update: skipping — could not read the work-tree status of %s; "
                    "a reset could discard uncommitted changes. Run `kirocrew update`.",
                    loggable_path(proj),
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return True
            tracked = [
                ln
                for ln in (status_out or b"").decode(errors="replace").splitlines()
                if ln.strip() and not ln.startswith("??")
            ]
            if tracked:
                logger.warning(
                    "Auto-update: skipping — %s has %s uncommitted tracked change(s); "
                    "a reset would discard them. Run `kirocrew update` to decide.",
                    loggable_path(proj),
                    len(tracked),
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return True

            # 3b. Tracked edits git was TOLD not to look at. `status --porcelain`
            #     above honours `assume-unchanged` / `skip-worktree` and reports a
            #     clean tree for an edited file, while `reset --hard` still
            #     overwrites it -- so check 3 alone cannot see this loss.
            hidden = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), lambda: hidden_worktree_edits(proj)
            )
            if hidden is None or hidden:
                logger.warning(
                    "Auto-update: skipping — %s has %s tracked change(s) hidden by "
                    "assume-unchanged/skip-worktree (e.g. %s); a reset would discard "
                    "them. Run `kirocrew update` to decide.",
                    loggable_path(proj),
                    "an unknown number of" if hidden is None else len(hidden),
                    loggable_path(hidden[0]) if hidden else "unknown",
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return True

            # 4. Untracked files that the TARGET would create. `reset --hard`
            #    leaves untracked files alone ONLY while they do not collide with
            #    a path the target adds -- where it does, the local file is
            #    overwritten. `git status --porcelain` reports such a file as
            #    `??`, which check 3 deliberately skips, so this is the one
            #    data-loss case that survives a "clean" tracked tree. Verified:
            #    upstream adds `newfile.txt`, a local untracked `newfile.txt` is
            #    replaced by the upstream content.
            #
            #    Detected rather than prevented by switching to `merge --ff-only`:
            #    the reset semantics are deliberate (documented as discarding
            #    tracked edits) and this path keeps them. A collision is a refusal
            #    for the same reason as the three above -- it is unrecoverable and
            #    unattended.
            added_proc = await asyncio.create_subprocess_exec(
                git_bin,
                "diff",
                "--name-only",
                "--diff-filter=A",
                # Rename detection is ON by default for porcelain diffs, and it
                # DEFEATS this guard: a pure `git mv` upstream is reported as a
                # single `R` entry, which `--diff-filter=A` excludes, so the
                # destination path never appears as added. Verified — upstream
                # renaming `a.txt` to `b.txt` yields `R100` and an EMPTY added
                # list, while an untracked local `b.txt` is still overwritten by
                # the reset. `--no-renames` decomposes the rename into a delete
                # plus an add, which is what this check needs to see.
                "--no-renames",
                "-z",
                "HEAD",
                target,
                cwd=proj,
                env=_git_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=platform_compat.IS_POSIX,
            )
            try:
                added_out, _ = await asyncio.wait_for(added_proc.communicate(), timeout=10)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                await _kill_and_reap(added_proc)
                raise
            if added_proc.returncode != 0:
                logger.warning(
                    "Auto-update: skipping — could not list the paths %s would add; "
                    "a reset could overwrite untracked files. Run `kirocrew update`.",
                    branch,
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return True
            # `os.fsdecode`, NOT `.decode(errors="replace")`: a path byte that is
            # not valid UTF-8 becomes U+FFFD under `replace`, and the resulting
            # name does not exist on disk — so the check answers "no collision"
            # for a file it is looking straight at. Verified: a `bad\xffname.txt`
            # decodes to `bad\ufffdname.txt` (lexists False) under `replace` and
            # to `bad\udcffname.txt` (lexists True) under `fsdecode`, and the
            # reset overwrote it. `fsdecode` round-trips through surrogateescape,
            # which is what the os functions below need.
            added_names = [os.fsdecode(raw) for raw in (added_out or b"").split(b"\0") if raw]

            def _obstructions(name: str) -> bool:
                """Whether *name* collides with something already on disk.

                Two shapes, both unrecoverable and both invisible to check 3:

                * the path ITSELF exists untracked, and the reset overwrites it;
                * an ANCESTOR exists as a non-directory. When the target adds
                  `pkg/mod.py` and `pkg` is locally an untracked FILE, git must
                  replace that file with a directory — `lexists("pkg/mod.py")` is
                  False, so checking only the full path misses it. Verified: the
                  untracked `pkg` was destroyed while the full-path check passed.
                """
                full = os.path.join(proj, name)
                if os.path.lexists(full):
                    return True
                parent = os.path.dirname(name)
                while parent:
                    candidate = os.path.join(proj, parent)
                    # Link check FIRST: `isdir` follows the link, so an untracked
                    # symlink-to-directory reported "directory, not an
                    # obstruction" -- and the reset then replaced the developer's
                    # symlink with a real directory. Verified against real git.
                    #
                    # `is_link_or_junction`, not `os.path.islink`: `islink` returns
                    # False for a Windows JUNCTION, so a junction ancestor would
                    # read as a plain directory and the reset would write through
                    # it, outside the checkout. AGENTS.md names this helper as the
                    # required form for exactly this reason, and its own docstring
                    # describes this failure -- using the bare `islink` here was a
                    # rule violation, not a judgement call.
                    if platform_compat.is_link_or_junction(candidate):
                        return True
                    if os.path.lexists(candidate) and not os.path.isdir(candidate):
                        return True
                    parent = os.path.dirname(parent)
                return False

            # Offloaded: `_obstructions` walks each added path's ancestors with
            # synchronous `os.path` probes, so a large update would run an
            # unbounded stat walk ON THE EVENT LOOP and stall every chat and the
            # heartbeat (`no-blocking-call-on-event-loop`).
            collisions = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                lambda: [name for name in added_names if _obstructions(name)],
            )
            if collisions:
                logger.warning(
                    "Auto-update: skipping — %s would add %s path(s) that already "
                    "exist untracked here (e.g. %s); a reset would overwrite them. "
                    "Run `kirocrew update` to decide.",
                    branch,
                    len(collisions),
                    # `loggable_path`, not the raw name: this is the one log line
                    # that carries a filename straight from git output, and a
                    # non-UTF-8 byte in it would make logging DROP the record --
                    # silently losing the evidence that the update refused here.
                    loggable_path(collisions[0]),
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return True

            # 5. Interpreter floor of the TARGET revision, read from the fetched
            #    commit itself. pip would refuse the reinstall below on the same
            #    floor, but only after the reset has moved the tree to code this
            #    venv cannot import -- a state every boot then re-enters, because
            #    the next check sees the commit already applied and never retries.
            #    Refuse before the reset, with the remedy, and leave the tree.
            try:
                floor_breach = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    lambda: dep_sync.incoming_python_floor_breach(
                        Path(proj), target, Path(sys.executable), git_bin=git_bin, env=_git_env
                    ),
                )
            except dep_sync.IncomingFloorUnreadable as exc:
                # A floor git could not read is not a floor that is absent:
                # refusing here is what keeps a git hiccup from re-admitting
                # the stranded state this whole gate exists to prevent.
                floor_breach = (
                    "could not read the incoming revision's interpreter "
                    f"requirement ({exc}); leaving the checkout where it is"
                )
            if floor_breach:
                # Redact first, cap last -- the same treatment every sibling push
                # gives text it did not author. The refusal quotes the remote's
                # `requires-python` verbatim and paths from the local install.
                floor_breach, _ = redact_exfiltration_urls(floor_breach)
                floor_breach, _ = redact_credentials(floor_breach)
                floor_breach = floor_breach[:500]
                logger.warning("Auto-update refused: %s", floor_breach)
                if self.dashboard_state:
                    self.dashboard_state.push_update_progress(
                        "failed", f"Update refused: {floor_breach}"
                    )
                    self.dashboard_state.push_refresh("update_available")
                return True

            # 6. A policy that turned unattended installs off since the effect
            #    was derived. Read LAST, after every await above, so on the
            #    post-copy pass nothing awaits between it and the reset. A
            #    floor's mandatory update does not ask.
            if not mandatory and not unattended_apply_allowed():
                logger.info(
                    "Auto-update not applied: the security policy turned off "
                    "unattended installs while this update waited"
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return True

            return False

        # Every refusal runs before the copy, so a checkout they keep refusing
        # spends no copy (each one rotates a backup out of `memory.backup_keep`),
        # and again after it, so a change made while the copy ran is caught
        # before the reset.
        if await _refused_before_reset():
            return

        # A fresh copy of every memory store, the same copy-or-refuse step
        # `kirocrew update` takes. Nobody is watching this path, so a copy that
        # did not land skips the update and says so; it never resets blind and
        # never exits the gateway. Retried on the next check.
        from kiro_crew.platform.wheel_apply import (
            snapshot_memory_before_update,
            snapshot_refusal,
        )

        copied, snapshot_failure = await asyncio.to_thread(snapshot_memory_before_update)
        if snapshot_failure:
            snapshot_failure, _ = redact_exfiltration_urls(snapshot_failure)
            snapshot_failure, _ = redact_credentials(snapshot_failure)
            snapshot_failure = snapshot_failure[:500]
            logger.warning(
                "Auto-update skipped: pre-update memory snapshot failed: %s", snapshot_failure
            )
            message = snapshot_refusal(snapshot_failure)
            self._notice_wheel_update_once(
                f"snapshot_failed:{target}", "Kiro Crew update skipped", message
            )
            if self.dashboard_state:
                self.dashboard_state.push_update_progress("failed", message)
                self.dashboard_state.push_refresh("update_available")
            return
        logger.info("Auto-update: pre-update memory snapshot, %d store(s) copied", copied)

        # LAST-MOMENT REVALIDATION: the same refusals again, now the last thing
        # ahead of the reset, which is the only destructive step.
        if await _refused_before_reset():
            return

        # From the reset on, this step rewrites the install it serves from,
        # so it owns a missing bundle until it returns (update_ownership).
        owned.enter_context(update_ownership.step(update_ownership.Step.GIT_AUTO_UPDATE))

        # Hard reset to remote. Reached only with a clean tracked tree and no
        # untracked collisions, so it overwrites nothing the developer owns.
        reset = await asyncio.create_subprocess_exec(
            _git,
            "reset",
            "--hard",
            target,
            cwd=proj,
            env=_git_env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=platform_compat.IS_POSIX,
        )
        # Only a reset that started can have moved the tree: a spawn that
        # raised wrote nothing.
        tree_moved = True
        try:
            await asyncio.wait_for(reset.wait(), timeout=10)
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            # This child is a MUTATION, not a query: abandoned, it is a
            # hard reset still running against the operator's checkout.
            await _kill_and_reap(reset)
            raise
        if reset.returncode != 0:
            logger.error("Auto-update: git reset --hard failed (rc=%d)", reset.returncode)
            if self.dashboard_state:
                self.dashboard_state.clear_update_progress()
            return
        logger.info("Auto-update: reset to origin/%s, rebuilding", branch)

        # Update the optional kiro-cli backend, by the pinned absolute path
        # `_pinned_kiro_cli` returns — never a bare argv0 this unattended
        # path would let `PATH` answer. `None` means do not spawn it,
        # skipped like any absent backend, which this step already treats as
        # non-fatal.
        # On the KAS bundle route kiro-cli is not part of what runs, so its
        # self-update is skipped with the reason in the log (the only place an
        # unattended apply reports). The engine is Crew's own install, laid
        # down separately by `kirocrew kas install` for the pin the installed
        # release carries; this apply does not install it, so the log names
        # that command for a changed pin. Local import: this body runs on the
        # gateway facade's globals, and the config read goes off the loop like
        # every other blocking read in this apply.
        from kiro_crew.agent_sdk import kas_bundle

        if await asyncio.to_thread(kas_bundle.kas_bundle_route):
            logger.info(
                "Auto-update: kiro-cli update skipped; KAS runs Kiro Crew's own engine "
                "bundle, not kiro-cli. A changed engine pin needs `kirocrew kas install` "
                "after the restart."
            )
            kiro_cli_bin = None
        else:
            kiro_cli_bin = await _pinned_kiro_cli("the optional kiro-cli backend update")
        # The desktop app's bundled copy is skipped too: it sits inside the
        # signed app bundle, where an in-place self-update would break the
        # codesign seal, and the app update is what replaces it. Checked
        # against the live environment because that is where the Electron
        # shell publishes the bundled directory.
        if kiro_cli_bin is not None and is_bundled_kiro_cli(kiro_cli_bin, os.environ):
            logger.debug("Auto-update: kiro-cli is the app's bundled copy, not updated in place")
            kiro_cli_bin = None
        if kiro_cli_bin is not None:
            kiro_update: asyncio.subprocess.Process | None = None
            try:
                kiro_update = await asyncio.create_subprocess_exec(
                    kiro_cli_bin,
                    "update",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    # Own process group (POSIX; no-op on Windows) so the
                    # kill in the arms below reaches the whole tree.
                    start_new_session=platform_compat.IS_POSIX,
                )
                await asyncio.wait_for(kiro_update.wait(), timeout=120)
            except (TimeoutError, asyncio.TimeoutError):
                # Kill the tree BEFORE falling through: this step is
                # non-fatal, but the code below rebuilds the frontend and
                # reinstalls the Python deps, and an abandoned
                # `kiro-cli update` would keep mutating the installation
                # concurrently — the same half-replaced-install race the
                # wheel path's CancelledError branch exists to prevent.
                if kiro_update is not None:
                    await _kill_and_reap(kiro_update)
                logger.debug("Auto-update: kiro-cli update timed out (non-fatal)")
            except asyncio.CancelledError:
                # Shutdown cancels the update task; without this the child
                # keeps mutating the installation unsupervised.
                if kiro_update is not None:
                    await _kill_and_reap(kiro_update)
                raise
            except Exception:
                logger.debug("Auto-update: kiro-cli update failed (non-fatal)")

        # Build + stage the in-tree website/ frontend so the dashboard
        # serves the latest bundle. Graceful no-op if no website/ or npm.
        if self.dashboard_state:
            self.dashboard_state.push_update_progress("building", "Building frontend…")
        await build_frontend_async(
            proj,
            push_progress=(
                self.dashboard_state.push_update_progress if self.dashboard_state else None
            ),
        )

        if self.dashboard_state:
            self.dashboard_state.push_update_progress("building", "Rebuilding package…")
        # Install the reset revision's Python deps / entry points. The gateway
        # is normally started through the console script pip would have to
        # rewrite, which Windows locks, so dep_sync picks the reinstall only
        # where it can actually run and substitutes a dependency-only sync
        # where it cannot — a reinstall that dies on the locked script has
        # already deleted the editable .pth.
        pip_messages: list[tuple[str, bool]] = []
        # The bound lives on the pip subprocess (dep_sync's `timeout`), not on
        # an `asyncio.wait_for` around the executor. Cancelling a wait_for does
        # NOT cancel the thread it is waiting on: expiry would report failure
        # while a live pip kept writing to the venv and permanently occupied a
        # subprocess_executor thread. Passing the deadline down means the child
        # is actually killed and the thread is released.
        #
        # Supervised: a shutdown that cancels this task also cancels the install,
        # so one still queued, or cancelled before its last check ahead of pip,
        # never starts pip. A pip already running is left to finish (see
        # dep_sync.sync_or_reinstall_supervised for the exact window).
        pip_rc = await dep_sync.sync_or_reinstall_supervised(
            subprocess_executor(),
            Path(proj),
            Path(sys.executable),
            lambda message, error: pip_messages.append((message, error)),
            timeout=600,
        )
        if pip_rc != 0:
            # The tree moved but its dependencies did not, and this step
            # stays up rather than restart (below): a relaunch would die at
            # import, so the stale-asset watchdog must not take one either.
            update_ownership.refuse_restart(
                "the dependency sync after the git auto-update did not complete"
            )
            # Same reasoning as the dep-repair path: redact first, cap last.
            err_text = "; ".join(m for m, _ in pip_messages)
            err_text, _ = redact_exfiltration_urls(err_text)
            err_text, _ = redact_credentials(err_text)
            err_text = err_text[:500]
            logger.error(
                "Auto-update: dependency install failed (rc=%d): %s",
                pip_rc,
                err_text,
            )
            if pip_rc == dep_sync.REFUSED:
                # REFUSED means the sync stopped BEFORE touching the venv --
                # most importantly when that venv serves a different
                # checkout. The core-dep repair below would then install into
                # exactly the venv the guard just protected, so a refusal ends
                # the auto-update here without even repairing: the messages
                # above name the remedy, and nothing was changed.
                if self.dashboard_state:
                    self.dashboard_state.push_update_progress("error", err_text)
                return
            if self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "building",
                    "Dependency install hit an error — repairing core deps…",
                )
            # The source tree is already on the new version (git reset ran
            # first), so booting without the core deps crashes every
            # command (e.g. cc_agent's `import yaml`). Install the core
            # public deps directly so the gateway still boots and can
            # self-heal — this can't fully fail the way `pip install -e .`
            # can, because these resolve from public PyPI with no
            # private-index dependency.
            core_deps = [pip for _mod, pip in self._REQUIRED_DEPS]
            fallback = await asyncio.create_subprocess_exec(
                *platform_compat.isolated_python_argv(
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    *core_deps,
                ),
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own process group (POSIX; no-op on Windows) so the kill
                # below reaches pip's build-backend grandchildren too.
                start_new_session=platform_compat.IS_POSIX,
            )
            try:
                _fb_out, fb_err = await asyncio.wait_for(fallback.communicate(), timeout=300)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                await _kill_and_reap(fallback)
                raise
            if fallback.returncode == 0:
                logger.info(
                    "Auto-update: core deps repaired after pip failure (%s)",
                    ", ".join(core_deps),
                )
            else:
                logger.error(
                    "Auto-update: core dep repair also failed (rc=%d): %s",
                    fallback.returncode,
                    # Redact the whole stream (pip can echo an index URL
                    # with credentials), then keep the tail where pip
                    # prints its error.
                    redact_log_via_context(fb_err.decode(errors="replace"))[-300:],
                )
            # Repair or not, do NOT restart after a sync that did not come back
            # clean. The tree is already on the new revision (the reset ran
            # first), and every nonzero result names something the restart
            # cannot fix by itself:
            #
            #   - dependencies still unsatisfied  -> the process this restart
            #     brings up dies at import and takes the running gateway with
            #     it, and the repair only covers the CORE deps, not whatever
            #     the revision actually added.
            #   - console script repointed or removed by the revision -> the
            #     wrapper on disk still dispatches to the old target, and no
            #     dependency install rewrites it. This restart uses
            #     `-m kiro_crew` so it would survive, but the next restart
            #     through the service manager runs `kirocrew` and does not.
            #
            # Staying up on already-imported modules is strictly better than
            # either: the operator keeps a working gateway to finish the
            # install from, and is told so now rather than at the next restart.
            if self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "error",
                    "Update stopped before restart: the dependency sync did "
                    "not complete cleanly. The gateway is still running on the "
                    "previously loaded code — finish the install from a "
                    "terminal, then restart.",
                )
            return

        # The tree and its dependencies agree again: a refusal an earlier
        # attempt recorded does not describe the install.
        tree_moved = False
        update_ownership.clear_restart_refusal()
        logger.info("Auto-update: rebuild complete, preparing safe restart")
        # Re-read version from rebuilt package for the operator-facing log;
        # restart ownership itself is centralized below.
        importlib.reload(kiro_crew)
        new_ver = kiro_crew.__version__
        print(f"👻 New version {new_ver} available — auto-updating and restarting…")
        # Hand the gap to the restart: this step's ownership ends here and
        # the restart's begins as the await starts, with no yield between.
        owned.close()
        await self._restart_after_update(respawn_executable)
    except Exception:
        logger.warning("Auto-update failed", exc_info=True)
        if tree_moved:
            update_ownership.refuse_restart(
                "the git auto-update failed after the tree moved, before its "
                "dependencies were synced"
            )
        if self.dashboard_state:
            # Surface the platform-correct manual restart command so a failed
            # auto-restart doesn't leave the user guessing. Resolved OFF the
            # loop thread: the hint stats the two unit-file locations, and
            # the per-user one is under the account's home, which can be a
            # network mount — a stat against a disconnected mount blocks for
            # as long as the mount does, and on this thread that freezes
            # chat and the liveness heartbeat together with nothing in-band
            # to clear it (the watchdog's kill is the only exit). A worker
            # thread waits in its place; the loop keeps serving.
            hint = await asyncio.to_thread(restart_command_hint)
            self.dashboard_state.push_update_progress("failed", f"Restart failed — run: {hint}")
    finally:
        owned.close()


async def _auto_apply_wheel_update(
    self: GatewayOrchestrator,
    channel: str,
    version: str,
    *,
    mandatory: bool = False,
    mandatory_key: str = "",
) -> None:
    """Apply a managed-venv update unattended, building it BESIDE the live venv.

    Runs :func:`kiro_crew.platform.wheel_apply.run_wheel_apply`, the path
    ``POST /api/update/approve`` runs too (and the engine ``kirocrew update``
    drives): the signed manifest and wheel digest are verified,
    ``crew-venv-<version>`` is built as a sibling tree, proven to serve the
    version, memory is copied, and ``crew-venv-current`` is flipped with
    ``os.replace``. Nothing this process loads from is moved, rewritten or
    deleted, so a stop at any point before the flip leaves the running
    install whole. That holds because this process runs from a versioned
    tree, not through the link: a restart execs the link's RESOLVED tree, and
    a gateway an earlier version restarted through the link itself
    (:func:`~kiro_crew.platform.wheel_apply.relaunch_before_apply`) restarts
    onto its resolved tree, at the same version, before anything is built.

    Admission stays OPEN while the tree builds. Only the restart into the
    promoted tree pauses it, through :meth:`_retry_pending_update_restart`,
    which a busy gateway retries on the short cadence; a policy floor's
    restart keeps the mandatory grace across those retries.

    Preconditions (checked by the caller): ``auto_update`` is on or a policy
    floor mandates the update, ``auto_update_effect`` chose the wheel route (a
    managed venv on POSIX, not a git checkout, not externally managed), and
    the check reported *version* newer on *channel*. The source pin and the
    CDN shape are checked again here, by the shared preflight.
    """
    # Loaded before the apply, as every update path here does, so the restart
    # never depends on an import after the install changed.
    from kiro_crew.platform import wheel_apply
    from kiro_crew.platform.wheel_engine import (
        WheelUpdateError,
        check_release_version,
        respawn_executable,
    )

    state = self.dashboard_state
    try:
        check_release_version(version)
        feed_base, artifact_base = wheel_apply.preflight_bases()
    except (WheelUpdateError, wheel_apply.WheelApplyRefused) as exc:
        # A version no tree can be named for, or a preflight refusal.
        logger.warning("Auto-update (wheel) refused: %s", exc)
        if state:
            state.push_refresh("update_available")
        return
    # Promotion would move the launcher a userns AppArmor profile applies to,
    # and only the operator can re-attach it, so this waits for them, a policy
    # floor included: the floor is then retried on the short cadence, and the
    # notice says exactly what to run.
    if await asyncio.to_thread(wheel_apply.userns_reattach_needed, version):
        logger.warning(
            "Auto-update (wheel) to %s waits for the operator: it would detach "
            "the sandbox's AppArmor profile",
            version,
        )
        self._notice_wheel_update_once(
            f"reattach:{version}",
            f"Kiro Crew {version} needs you to apply it",
            wheel_apply.userns_reattach_remedy(version),
        )
        if mandatory:
            self._update_apply_deferred = True
        if state:
            state.push_refresh("update_available")
        return

    if await asyncio.to_thread(wheel_apply.relaunch_before_apply):
        # The flip would swap this process's own modules under it while a busy
        # restart waits. The pending restart retries like an update's; the
        # successor runs from the resolved tree and builds on its next cycle.
        logger.info(
            "Auto-update (wheel): this gateway loads its code through the stable "
            "link; restarting onto its resolved tree before building %s",
            version,
        )
        self._pending_update_respawn = respawn_executable
        self._pending_update_mandatory = mandatory
        self._pending_update_mandatory_key = mandatory_key
        self._pending_update_version = "" if mandatory else version
        await self._retry_pending_update_restart()
        return

    logger.info("Auto-update (wheel): building %s beside the running install", version)
    outcome = await wheel_apply.run_wheel_apply(
        channel=channel,
        version=version,
        feed_base=feed_base,
        artifact_base=artifact_base,
        state=state,
        refuse_if_unattended_off=not mandatory,
    )
    if outcome.status == "promoted":
        if not await asyncio.to_thread(wheel_apply.restart_reaches, version):
            # Restarting would exec the running version again, and its next
            # cycle would promote and restart again, for ever.
            logger.error(
                "Auto-update (wheel): %s promoted, but the restart would not reach "
                "it; not restarting",
                version,
            )
            self._notice_wheel_update_once(
                f"unreachable:{version}",
                f"Kiro Crew {version} needs the installer to finish",
                wheel_apply.restart_unreachable_remedy(version, channel),
            )
            if state:
                state.push_refresh("update_available")
            return
        logger.info("Auto-update (wheel): %s promoted, preparing safe restart", version)
        self._pending_update_respawn = respawn_executable
        self._pending_update_mandatory = mandatory
        self._pending_update_mandatory_key = mandatory_key
        self._pending_update_version = "" if mandatory else version
        await self._retry_pending_update_restart()
    elif outcome.status in wheel_apply.RETRY_SOON:
        # Another apply holds the lock, memory is still preparing, or the
        # apply was stopped: retry on the short cadence, pushing nothing onto
        # another apply's progress feed.
        self._update_apply_deferred = True
    elif outcome.status == "incompatible":
        self._notice_wheel_update_once(
            f"incompatible:{version}",
            f"Kiro Crew {version} cannot be applied automatically",
            f"{outcome.message}. {wheel_apply.incompatible_remedy(channel)}",
        )
        if state:
            state.push_refresh("update_available")
    elif state:
        # failed, timed_out, snapshot_failed: an ordinary failure, retried on
        # the normal cadence. A refused memory copy names its own repair.
        suffix = "" if outcome.status == "snapshot_failed" else " — run manually: kirocrew update"
        state.push_update_progress("failed", f"{outcome.message}{suffix}")


def _notice_wheel_update_once(self: GatewayOrchestrator, key: str, title: str, body: str) -> None:
    """Send one dashboard notice per *key* for the life of this process."""
    if key in self._wheel_update_noticed:
        return
    self._wheel_update_noticed.add(key)
    if self.dashboard_state:
        self.dashboard_state.notify("update", title, body)
