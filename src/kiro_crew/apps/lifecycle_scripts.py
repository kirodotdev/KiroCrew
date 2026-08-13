"""Running an app's own lifecycle scripts (``onEnable`` / ``onDisable`` / ``onUpdate`` / ``onUninstall``).

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
import logging
import os
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.execution import app_execution_denied
from kiro_crew.apps.manager import apps_dir
from kiro_crew.apps.registry import minimal_env
from kiro_crew.sandbox import cgroup_scope_argv, create_subprocess_limited, wrap_argv

logger = logging.getLogger(__name__)


async def run_lifecycle_script(
    app_name: str,
    script: str,
    *,
    timeout: int = 30,
    extra_env: dict[str, str] | None = None,
    action: str = "lifecycle_script",
) -> dict[str, Any]:
    """Run a lifecycle script (onEnable/onDisable/onUpdate/onUninstall) in the app directory.

    Returns dict with ``output`` (str) and ``failed`` (bool).
    """
    app_root = apps_dir() / app_name
    denied = app_execution_denied(
        app_name,
        action=action,
        app_root=app_root,
        caller="dashboard",
    )
    if denied:
        logger.warning("Refusing app %s lifecycle action %s: %s", app_name, action, denied)
        return {"output": denied, "failed": True, "denied": True}

    if not app_root.is_dir():
        return {"output": f"app directory not found: {app_root}", "failed": True}

    safe_script = f"set -euo pipefail\n{script}"
    base_cmd = ["/bin/bash", "-c", safe_script]
    sandboxed_cmd, cleanup = wrap_argv(base_cmd, mode="standard")
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
        proc = await create_subprocess_limited(
            *sandboxed_cmd,
            cwd=str(app_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = (stdout or b"").decode(errors="replace").strip()
            lines = output.split("\n")
            output = "\n".join(lines[-20:])  # last 20 lines
        except asyncio.TimeoutError:
            try:
                # killpg on POSIX, taskkill /T on Windows — via platform_compat.
                # Async variant offloads the Windows taskkill spawn to
                # subprocess_executor so this lifecycle-script timeout path
                # never blocks the event loop on taskkill.exe.
                await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGTERM)
            except OSError:
                proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return {"output": f"script timed out after {timeout}s", "failed": True}
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass

    failed = bool(proc.returncode and proc.returncode != 0)
    return {"output": output, "failed": failed}
