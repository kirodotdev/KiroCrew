"""Running an app's own lifecycle scripts (``onEnable`` / ``onDisable`` / ``onUninstall``).

``setup.onInstall`` is run by the install transaction in ``registry_pipeline/install.py``,
not here, and ``setup.onUpdate`` is declared but dispatched by nothing (see the
declared-not-wired paragraph in ``docs/system-specs/modules/app-kit-platform.md``).

Extracted out of ``apps/routes.py`` so that ``apps/teardown.py`` can run
``setup.onDisable`` as part of the ONE shared teardown. ``routes.py`` imports
``teardown.py``, so ``teardown`` importing the runner back from ``routes`` would be a
cycle — and the alternative, a function-local import inside ``teardown``, both hides
the dependency from ``patch`` and breaks the repo's top-level-imports rule. This
module sits below both: it imports only ``execution`` / ``manager`` / ``registry`` /
``sandbox`` / ``platform_compat``, none of which import ``teardown``.

Why ``teardown`` needs it at all: ``onDisable`` is the ONLY thing that can stop
whatever the app's ``onEnable`` started out-of-band (a detached helper the gateway
never tracked). Leaving it in the disable handler alone meant **revoking trust was
weaker than merely disabling** — the exact inversion the shared teardown exists to
prevent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.execution import app_execution_denied

# sanitize_script_output lives in manager (the lower layer — importing the
# reverse here would cycle) and is re-exported for the CLI's enable surface.
from kiro_crew.apps.manager import apps_dir, sanitize_script_output
from kiro_crew.apps.registry import minimal_env
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)

logger = logging.getLogger(__name__)

#: Seconds a timed-out lifecycle script gets between the graceful SIGTERM and
#: the SIGKILL escalation, so an install/uninstall hook's cleanup trap can run
#: without the timeout path being able to hang on a trap that never exits.
#: Mirrors the app-build grace in ``registry._KILL_GRACE_PERIOD``.
_TERM_GRACE_SECS = 5

__all__ = ["run_lifecycle_script", "sanitize_script_output"]


async def run_lifecycle_script(
    app_name: str,
    script: str,
    *,
    timeout: int = 30,
    extra_env: dict[str, str] | None = None,
    action: str = "lifecycle_script",
    app_root: Path | None = None,
    caller: str = "dashboard",
    repository: str | None = None,
    reap_surviving_group: bool = False,
) -> dict[str, Any]:
    """Run a lifecycle script (onEnable/onDisable/onUninstall) in the app directory.

    Returns dict with ``output`` (str) and ``failed`` (bool).
    """
    root = app_root if app_root is not None else apps_dir() / app_name
    denied = app_execution_denied(
        app_name,
        action=action,
        app_root=root,
        caller=caller,
        repository=repository,
    )
    if denied:
        logger.warning("Refusing app %s lifecycle action %s: %s", app_name, action, denied)
        return {"output": denied, "failed": True, "denied": True}

    if not root.is_dir():
        return {"output": f"app directory not found: {root}", "failed": True}

    safe_script = f"set -euo pipefail\n{script}"
    base_cmd = ["/bin/bash", "-c", safe_script]
    # wrap_argv_async fail-closes (RuntimeError) on a host with no sandbox
    # backend and unsandboxed exec not allowed, and the spawn itself can raise
    # OSError (e.g. no bash on Windows). Both are SCRIPT failures, not gateway
    # errors: callers gate rollbacks on the ``failed`` flag, so a raise here
    # would either crash the CLI after ``enable_app`` already flipped the flag
    # or surface as a 500 from the enable route instead of its 400 rollback.
    cleanup: str | None = None
    try:
        sandboxed_cmd, cleanup = await wrap_argv_async(
            base_cmd, mode="standard", _prepare=wrap_argv
        )
    except RuntimeError as exc:
        logger.warning("Sandbox unavailable for app %s lifecycle script: %s", app_name, exc)
        return {"output": f"sandbox unavailable: {exc}", "failed": True}
    sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
    env = minimal_env(NONINTERACTIVE="1")
    if extra_env:
        env.update(extra_env)
    try:
        # Process-group isolation for timeout tree-kill. Pass both flags explicitly
        # (NOT **dict unpack — breaks mypy's Popen overload resolution on the build
        # fleet): start_new_session=True is a no-op on Windows, creationflags is 0
        # (no-op) on POSIX. (App lifecycle scripts are bash; on Windows without bash
        # they fail gracefully rather than crash here.)
        try:
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
        except OSError as exc:
            # No interpreter for the script on this host (e.g. Windows without
            # bash). Same script-failure contract as the sandbox refusal above.
            logger.warning("Could not launch app %s lifecycle script: %s", app_name, exc)
            return {"output": f"failed to launch lifecycle script: {exc}", "failed": True}
        # Capture kill authority BEFORE communicate/wait reaps the leader. A
        # backgrounded descendant can outlive that leader while holding the
        # group and the output pipe alive; an after-the-fact PID lookup would
        # then raise and silently leave the straggler running.
        leader_pgid: int | None = None
        leader_start: str | None = None
        if platform_compat.IS_POSIX:
            with contextlib.suppress(OSError):
                pgid = platform_compat.posix_getpgid(proc.pid)
                if pgid == platform_compat.posix_getpgid(0):
                    # setsid may not have landed yet: the spawned leader is
                    # still in our group. With start_new_session its eventual
                    # group id is its own pid, never ours.
                    pgid = proc.pid
                leader_pgid = pgid
        else:
            leader_start = platform_compat.process_start_time(proc.pid)

        async def _kill_captured_group(sig: int) -> None:
            if platform_compat.IS_POSIX:
                if leader_pgid is not None:
                    await asyncio.to_thread(platform_compat.kill_pgid, leader_pgid, sig)
                    return
            elif leader_start is not None and await asyncio.to_thread(
                platform_compat.kill_process_tree_pinned,
                proc.pid,
                leader_start,
                sig,
            ):
                return
            await platform_compat.kill_process_tree_async(proc.pid, sig)

        def _kill_captured_group_now(sig: int) -> None:
            """Unawaited BaseException cleanup: never trust a recycled PID."""
            if platform_compat.IS_POSIX:
                if leader_pgid is not None:
                    platform_compat.kill_pgid(leader_pgid, sig)
                return
            if leader_start is not None:
                platform_compat.kill_process_tree_pinned(proc.pid, leader_start, sig)

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # start_new_session means SIGINT reached this task, not the child.
            # A cancelled/interrupted script must not survive its rollback.
            with contextlib.suppress(OSError, ProcessLookupError, ValueError):
                _kill_captured_group_now(platform_compat.SIGKILL)
            raise
        except asyncio.TimeoutError:
            try:
                # SIGTERM first so a script's cleanup trap can run.
                await _kill_captured_group(platform_compat.SIGTERM)
            except OSError:
                pass
            try:
                # Bounded grace: drain pipes while the trap runs. communicate()
                # returns once the child exits; a script that ignores the TERM
                # outlives the grace.
                with contextlib.suppress(Exception, asyncio.TimeoutError):
                    await asyncio.wait_for(proc.communicate(), timeout=_TERM_GRACE_SECS)
            except BaseException:
                # Cancellation does not excuse a surviving group: kill before
                # propagating; the initial communicate is already inactive.
                with contextlib.suppress(OSError, ProcessLookupError, ValueError):
                    await _kill_captured_group(platform_compat.SIGKILL)
                raise
            # This is unconditional because a TERM-exited leader does not prove
            # its group is empty: a trap-armed descendant can still hold the
            # pipes and keep writing. An empty captured group tolerates SIGKILL.
            with contextlib.suppress(OSError, ProcessLookupError, ValueError):
                await _kill_captured_group(platform_compat.SIGKILL)
            if proc.returncode is None:
                await platform_compat.kill_and_reap(proc)
            return {"output": f"script timed out after {timeout}s", "failed": True}
        if reap_surviving_group:
            # Install scripts may not leave detached writers behind: one could
            # rewrite the copied manifest after post-script re-admission. The
            # leader has exited, so every group member still alive is an
            # unregistered straggler.
            with contextlib.suppress(OSError, ProcessLookupError, ValueError):
                await _kill_captured_group(platform_compat.SIGKILL)
        output = (stdout or b"").decode(errors="replace").strip()
        lines = output.split("\n")
        output = "\n".join(lines[-20:])  # last 20 lines
        failed = bool(proc.returncode and proc.returncode != 0)
        if failed and not output:
            output = f"exit code {proc.returncode}"
        return {
            "output": output,
            "failed": failed,
        }
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass
