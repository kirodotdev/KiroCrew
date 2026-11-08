"""Repair the install at boot.

Missing pip dependencies, a missing console script and an outdated kiro-cli, each
repaired or reported by a bounded child process the gateway kills and reaps itself,
and the pinned kiro-cli path an unattended spawn execs.

Orchestrator fields read: ``_DEP_INSTALL_TIMEOUT_SECS``,
``_KIRO_CLI_VERSION_TIMEOUT_SECS``, ``_REQUIRED_DEPS``, ``_STARTUP_CHILD_REAP_SECS``,
``_background_tasks``.
Orchestrator fields written: ``_console_script_repair_task``.

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
    import signal

    from kiro_crew.slack.gateway import (
        PATH_ONLY_INSTALL_NOTE,
        GatewayOrchestrator,
        Path,
        SandboxUnavailableError,
        create_subprocess_limited,
        dep_sync,
        logger,
        parse_kiro_cli_version,
        pin_kiro_cli,
        platform_compat,
        redact_credentials,
        redact_exfiltration_urls,
        sandboxed_spawn_argv,
        sandboxed_spawn_argv_async,
    )


#: Budget for pinning kiro-cli's path before an unattended spawn. The lookup is
#: a handful of `stat` calls, but they are under the home directory and
#: `_warn_if_kiro_cli_outdated` awaits them BEFORE `_init_dashboard` binds its
#: socket — so on an unresponsive network-mounted home an unbounded lookup would
#: keep the gateway from ever coming up. Overrunning the budget refuses the
#: spawn, exactly as an absent binary does.
_KIRO_CLI_RESOLVE_TIMEOUT_SECS = 5.0


async def _pinned_kiro_cli(purpose: str) -> str | None:
    """kiro-cli's absolute path for an unattended spawn, or ``None`` to refuse.

    Neither unattended spawn may exec a bare argv0: the gateway's inherited
    ``PATH`` can lead with an agent-writable directory (a worktree venv's
    ``bin``), and whatever that names would decide the payload. So the candidate
    set is the fixed known install directories plus the operator's own
    ``KIROCREW_KIRO_BIN``, with the inherited ``PATH`` excluded.

    That set does not cover every install: a system-wide one outside
    ``known_kiro_cli_dirs`` — a root-owned ``/usr/local/bin`` on Linux — is
    refused here while sessions keep launching it off ``PATH``. Refusing is the
    right default for a spawn with no operator present, but being SILENT about
    it is not: the resulting host never auto-updates and never warns it is
    outdated, with nothing in the log to say why. Hence the warning naming the
    override, and hence its condition — an install the pin declined is worth a
    line, a backend that simply is not installed is not.

    The sync half is :func:`kiro_crew.kiro_cli.pin_kiro_cli`, shared with the
    CLI's update command and the diagnostics bundle; this wrapper adds only
    what an unattended path on the event loop needs. Off the loop and bounded:
    see :data:`_KIRO_CLI_RESOLVE_TIMEOUT_SECS`.
    """

    try:
        pinned, unpinned_exists = await asyncio.wait_for(
            asyncio.to_thread(pin_kiro_cli),
            timeout=_KIRO_CLI_RESOLVE_TIMEOUT_SECS,
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "kiro-cli: path lookup exceeded %.0fs (unresponsive home?), skipping %s",
            _KIRO_CLI_RESOLVE_TIMEOUT_SECS,
            purpose,
        )
        return None
    if pinned is None and unpinned_exists:
        logger.warning("%s is skipped: %s.", purpose, PATH_ONLY_INSTALL_NOTE)
    return pinned


# Budgets for the two startup subprocesses below. Class attributes (not
# literals) so tests can shrink them to exercise the timeout/kill paths
# without waiting out the real budgets.
_DEP_INSTALL_TIMEOUT_SECS: float = 300.0


_KIRO_CLI_VERSION_TIMEOUT_SECS: float = 5.0


# Bound on the post-kill reap: a build-backend grandchild that survived
# the kill can hold the stdout/stderr pipes open, making an unbounded
# ``communicate()`` wait forever and hang boot.
_STARTUP_CHILD_REAP_SECS: float = 5.0


async def _kill_startup_child(proc: "asyncio.subprocess.Process") -> None:
    """Best-effort kill of a startup child and its descendants.

    ``proc.kill()`` signals only the child's own PID; pip's build-backend
    grandchildren survive it, keep writing into site-packages, and hold
    the pipe write ends open. The tree kill covers them: process-group on
    POSIX (the child is spawned with ``start_new_session``) and
    ``taskkill /T`` on Windows. Async on purpose — the Windows branch
    spawns ``taskkill`` (up to 5s), and ``kill_process_tree_async``
    offloads it so the kill itself cannot stall the loop this fix exists
    to protect (POSIX dispatches inline; ``killpg`` is non-blocking).
    Falls back to a plain kill when the tree kill is refused
    (already-dead child, non-int mocked PID in tests).
    """
    sig = getattr(signal, "SIGKILL", signal.SIGTERM)  # no SIGKILL on Windows
    try:
        await platform_compat.kill_process_tree_async(proc.pid, sig)
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()


async def _reap_startup_child(
    cls: type[GatewayOrchestrator], proc: "asyncio.subprocess.Process"
) -> None:
    """Bounded reap after a kill — never lets a wedged pipe hang boot.

    Best-effort by design: past the bound, boot proceeds and the OS reaps
    the zombie eventually. ``suppress(Exception)`` deliberately does not
    swallow ``CancelledError`` (a ``BaseException``).
    """
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.communicate(), timeout=cls._STARTUP_CHILD_REAP_SECS)


async def _check_missing_deps(self: GatewayOrchestrator) -> None:
    """Auto-repair missing pip deps for venv installs.

    After auto-update, old code may have pulled new source via git reset
    but skipped ``pip install``. This catches the gap on next startup.

    Async on purpose: the install can legitimately take minutes, and a
    synchronous ``subprocess.run`` here would block the event loop for the
    whole budget (see the module invariant — the loop runs callbacks one at
    a time, so nothing else, including the loop-stall heartbeat once it is
    armed, runs while a callback blocks). The child runs via
    ``asyncio.create_subprocess_exec`` — the same pattern the auto-update
    path in ``gateway_runtime/update_apply.py`` already uses — so the loop
    keeps servicing callbacks while pip works.
    """
    missing = [pip for mod, pip in self._REQUIRED_DEPS if importlib.util.find_spec(mod) is None]
    if not missing:
        return

    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj or self._is_brazil_install(proj):
        return

    logger.warning("Missing deps %s — installing directly", missing)
    print(f"👻 Installing missing dependencies: {', '.join(missing)}")
    proc = await asyncio.create_subprocess_exec(
        *platform_compat.isolated_python_argv(
            "-m",
            "pip",
            "install",
            "--quiet",
            *missing,
        ),
        cwd=proj,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Own process group (POSIX; no-op on Windows) so a timeout kill
        # reaches pip's build-backend grandchildren, not just pip itself.
        start_new_session=platform_compat.IS_POSIX,
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self._DEP_INSTALL_TIMEOUT_SECS
        )
    except (TimeoutError, asyncio.TimeoutError):
        await self._kill_startup_child(proc)
        await self._reap_startup_child(proc)
        print("❌ pip install timed out — run manually: kirocrew update")
        logger.error("Dep repair timed out after %.0fs", self._DEP_INSTALL_TIMEOUT_SECS)
        return
    except asyncio.CancelledError:
        # Gateway shutdown / Ctrl-C mid-install: leaving pip running would
        # race the NEXT boot's install of the same distributions — the
        # half-installed state this repair path exists to fix.
        await self._kill_startup_child(proc)
        raise
    if proc.returncode == 0:
        # Invalidate import caches so the new packages are found
        importlib.invalidate_caches()
        print("✅ Dependencies installed")
    else:
        print("❌ pip install failed — run manually: kirocrew update")
        # pip stderr can echo an index URL with embedded credentials
        # (https://user:token@private-index/...), so redact before the
        # volume cap: truncating first can bisect a token, and half a token
        # does not match the redactors' patterns.
        dep_err = (stderr or b"").decode(errors="replace")
        dep_err, _ = redact_exfiltration_urls(dep_err)
        dep_err, _ = redact_credentials(dep_err)
        logger.error("Dep repair failed: %s", dep_err[-500:])


async def _check_console_script(self: GatewayOrchestrator) -> None:
    """Repair a venv whose ``kirocrew`` console script went missing.

    ``_check_missing_deps`` catches a git-reset-without-pip-install that left
    an import missing, but not the failure mode where an interrupted venv
    rebuild (e.g. a Python-version bump that reran ``python -m venv`` + a
    killed ``pip install -e``) leaves a venv with a working interpreter but
    no ``kirocrew`` entry point — the gateway then dies later with an
    exit-127 "binary not found". This closes that gap at startup: if the
    recorded pip install has no executable console script, run the same
    in-place editable reinstall ``dep_sync`` uses, which is the one operation
    that rewrites the entry point.

    Scoped to pip installs of a real project dir: a Brazil install owns its
    own entry point, and an empty ``KIROCREW_PROJECT_DIR`` means there is no
    checkout to reinstall from.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj:
        return
    dep_sync_file = dep_sync.__file__

    def _probe() -> tuple[Path, Path, str | None] | None:
        # Every stat and read here is against the checkout, which can sit on a
        # network mount, and this runs at gateway startup -- so the whole probe
        # runs in a thread rather than on the loop that serves every session.
        if self._is_brazil_install(proj):
            return None
        method_file = Path(proj) / ".install-method"
        if not (
            method_file.is_file()
            and method_file.read_text(encoding="utf-8", errors="replace").strip() == "pip"
        ):
            return None
        # The venv interpreter under the project, platform-aware (Scripts on
        # Windows, bin on POSIX), shared with dep_sync's ownership exception.
        venv_py = dep_sync.project_venv_python(Path(proj))
        script = dep_sync.console_script_path(venv_py)
        if script.exists() and os.access(script, os.X_OK):
            return None
        resolved = str(Path(dep_sync_file).resolve()) if dep_sync_file is not None else None
        return venv_py, script, resolved

    probed = await asyncio.to_thread(_probe)
    if probed is None:
        return
    venv_py, script, dep_sync_path = probed

    logger.warning("kirocrew console script missing/not executable at %s — reinstalling", script)
    print("👻 Repairing kirocrew install (console script missing)…")
    # Run the stdlib-only module by absolute file path: the target venv may
    # not currently contain an importable kiro_crew package. A dedicated
    # child session lets cancellation own pip and its build descendants.
    if dep_sync_path is None:
        raise RuntimeError("dep_sync module has no source path")
    # Route through the sandbox chokepoint: this child EXECUTES
    # ``venv_py`` (dep_sync probes the target interpreter via
    # ``installed_package_origin``), and that interpreter lives in the
    # project checkout, so its bytes are not ours to trust. The sandbox
    # gives the whole repair subtree filesystem isolation plus a
    # credential-scrubbed env, and ``create_subprocess_limited`` adds the
    # kernel resource ceiling. ``mode="strict"`` hides the credential dirs
    # AND ``.ssh`` while the untrusted interpreter runs -- the tightest
    # tier, chosen because the child executes bytes we do not trust. It
    # still leaves the project and its venv writable (pip must rewrite the
    # entry point) and the network open (pip must reach the index);
    # ``scrub_env`` is mode-independent, so ``PIP_INDEX_URL`` / proxy / SSL
    # vars survive. ``strip_python_env`` stops an inherited PYTHONPATH from
    # satisfying the child's import probe from outside the venv. No
    # ``extra_writable_dirs``: the project is already writable, and a
    # carve-out outside the sealed runtime parent would be refused anyway.
    try:
        argv, env, cleanup = await sandboxed_spawn_argv_async(
            platform_compat.isolated_python_argv(
                dep_sync_path,
                "--repair-missing-package",
                str(proj),
                str(venv_py),
                force_isolation=True,
            ),
            mode="strict",
            env=os.environ.copy(),
            strip_python_env=True,
            _prepare=sandboxed_spawn_argv,
        )
    except SandboxUnavailableError as exc:
        # Fail CLOSED. Running an untrusted interpreter unsandboxed is the
        # exposure this routing exists to remove, so a host with no sandbox
        # backend does not get the repair -- it gets a named reason instead
        # of a silent no-op, leaving the pre-existing manual path. The typed
        # ``kind``/``detail`` are logged rather than an inferred English
        # guess: this PR exists to make this failure class diagnosable.
        print("❌ kirocrew reinstall skipped (no sandbox available) — run: kirocrew update")
        logger.error(
            "Console-script repair skipped: sandbox unavailable (kind=%s): %s — "
            "refusing to run the project venv interpreter unsandboxed",
            exc.kind,
            exc.detail,
        )
        return
    try:
        proc = await create_subprocess_limited(
            *argv,
            cwd=proj,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=platform_compat.IS_POSIX,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._DEP_INSTALL_TIMEOUT_SECS
            )
        except (TimeoutError, asyncio.TimeoutError):
            await self._kill_startup_child(proc)
            await self._reap_startup_child(proc)
            print("❌ kirocrew reinstall timed out — run manually: kirocrew update")
            logger.error(
                "Console-script reinstall timed out after %.0fs",
                self._DEP_INSTALL_TIMEOUT_SECS,
            )
            return
        except asyncio.CancelledError:
            await self._kill_startup_child(proc)
            await self._reap_startup_child(proc)
            raise
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)

    if proc.returncode != 0:
        detail = b"\n".join(part for part in (stdout, stderr) if part).decode(
            "utf-8", errors="replace"
        )
        detail, _ = redact_exfiltration_urls(detail)
        detail, _ = redact_credentials(detail)
        print("❌ kirocrew reinstall failed — run manually: kirocrew update")
        logger.error("Console-script reinstall failed: %s", detail[:500])
    else:
        print("✅ kirocrew console script restored")


def _schedule_console_script_repair(self: GatewayOrchestrator) -> asyncio.Task[None]:
    """Run the console-script repair after the HTTP socket has bound.

    The healthy path is a few filesystem probes, but repair can spend the
    full pip timeout in its owned child process. Keep a strong reference so
    the task is observable and shutdown cancellation reaches that child.
    """
    existing = self._console_script_repair_task
    if existing is not None and not existing.done():
        return existing

    async def _repair() -> None:
        try:
            await self._check_console_script()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Console-script check failed", exc_info=True)

    task = asyncio.create_task(_repair())
    self._console_script_repair_task = task
    self._background_tasks.add(task)

    def _clear(done: asyncio.Task[None]) -> None:
        self._background_tasks.discard(done)
        if self._console_script_repair_task is done:
            self._console_script_repair_task = None

    task.add_done_callback(_clear)
    return task


async def _warn_if_kiro_cli_outdated(self: GatewayOrchestrator) -> None:
    """Warn when kiro-cli is too old for ``--agent`` (requires >= 1.26).

    Never raises: an absent, hung, or unparseable kiro-cli must not break
    boot. Off the loop via an async subprocess so a slow binary cannot
    stall every other callback for the 5s budget, and a timeout is logged
    (not silently swallowed) so a wedged kiro-cli that costs 5s on every
    boot is diagnosable from gateway.log.

    Pinned the same way the auto-update pins it, via `_pinned_kiro_cli`:
    this probe runs unattended at boot, so a shim planted on `PATH` would
    execute here regardless of the `--version` argument. A binary the pin
    refuses has no version worth warning about — and the pin logs why.

    Skipped on the KAS bundle route: the selected harness runs Crew's own
    engine, so a kiro-cli that happens to be installed decides nothing about
    this gateway's sessions, and the warning's remedy (update it, or switch
    backend) would send the operator to fix a program nothing here spawns.
    """
    # Local import: this body runs on the gateway facade's globals.
    from kiro_crew.agent_sdk import kas_bundle

    if await asyncio.to_thread(kas_bundle.kas_bundle_route):
        return
    kiro_cli_bin = await _pinned_kiro_cli("the kiro-cli version check")
    if kiro_cli_bin is None:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            kiro_cli_bin,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group (POSIX; no-op on Windows) so the tree kill
            # in `_kill_startup_child` below reaches any descendants, not
            # just the direct child: without a session of its own the group
            # signal has nothing to address beyond the child's PID, so the
            # kill+reap arms below could not reach a descendant.
            start_new_session=platform_compat.IS_POSIX,
        )
    except Exception:
        return  # binary missing/unspawnable — stay silent
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(), timeout=self._KIRO_CLI_VERSION_TIMEOUT_SECS
        )
    except (TimeoutError, asyncio.TimeoutError):
        await self._kill_startup_child(proc)
        await self._reap_startup_child(proc)
        logger.warning(
            "kiro-cli --version timed out after %.0fs",
            self._KIRO_CLI_VERSION_TIMEOUT_SECS,
        )
        return
    except asyncio.CancelledError:
        await self._kill_startup_child(proc)
        raise
    except Exception:
        # Transport/pipe errors must not abort boot (this helper's
        # contract is "never raises", so a blanket except swallows them).
        # Kill+reap best-effort and continue.
        await self._kill_startup_child(proc)
        await self._reap_startup_child(proc)
        logger.debug("kiro-cli --version probe failed", exc_info=True)
        return
    try:
        if proc.returncode == 0:
            version = parse_kiro_cli_version(out.decode(errors="replace"))
            if version is not None and version[:2] < (1, 26):
                major, minor = version[0], version[1]
                print(
                    f"⚠️  kiro-cli {major}.{minor} is outdated (1.26+ required). "
                    "Update kiro-cli, or use the default claude-agent-acp backend."
                )
    except Exception:
        pass  # unparseable version output — stay silent
