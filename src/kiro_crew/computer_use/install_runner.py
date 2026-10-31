"""How the agent-desktop installer runs a child, and what of its output may be shown.

:mod:`kiro_crew.computer_use.agent_desktop_install` keeps the install steps; this module
holds what they run on: the child's environment (:func:`cli_env`); the
:class:`InstallScope` through which the gateway stops one install job's children; the one
spawn every step and probe goes through (:func:`_run`); and :func:`redact_install_output`,
the redactor every surface that shows installer output must use.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Callable
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.env import augmented_path, node_augmented_path
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def cli_env() -> dict[str, str]:
    """Environment for an npm/CLI child, with the Node bin directories on PATH.

    The gateway's own PATH is not enough: a GUI- or daemon-launched gateway inherits a
    minimal PATH, and the mise-managed ``npm`` wrapper needs ``mise`` itself on PATH.
    :func:`augmented_path` adds the broad non-login dirs; :func:`node_augmented_path`
    puts the Node bins outermost so they win. ``NODE_USE_SYSTEM_CA=1`` lets npm trust
    the OS certificate store behind a TLS-intercepting proxy; a value the user set is
    kept.
    """
    env = dict(os.environ)
    env["PATH"] = node_augmented_path(augmented_path(env.get("PATH", "")))
    env.setdefault("NODE_USE_SYSTEM_CA", "1")
    return env


#: Return code of a step refused or cut short because its :class:`InstallScope` was
#: terminated (the shell's "ended by interrupt" code).
INTERRUPTED_RC = 130
_INTERRUPTED_REASON = "interrupted: the gateway stopped this installer"
#: Return code :func:`_run` reports when the child outlives its timeout.
TIMEOUT_RC = 124

#: How long a killed child gets to close its pipes before :func:`_run` stops waiting.
_REAP_TIMEOUT_S = 5.0


class InstallScope:
    """The installer children one install job owns, so the gateway can stop them.

    ``asyncio.to_thread`` cannot cancel its worker, so the job's worker runs inside a
    scope (:func:`run_in_scope`); every :func:`_run` call on that thread registers its
    child here, and :meth:`terminate` kills each child's whole process tree. After
    termination the scope refuses to spawn.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._children: set[subprocess.Popen[str]] = set()
        self._terminated = False

    @property
    def terminated(self) -> bool:
        with self._lock:
            return self._terminated

    def _adopt(self, proc: subprocess.Popen[str]) -> bool:
        """Track *proc*; ``False`` when the scope was terminated first."""
        with self._lock:
            if self._terminated:
                return False
            self._children.add(proc)
            return True

    def _release(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._children.discard(proc)

    def terminate(self) -> int:
        """Kill every live child and refuse new ones. Returns how many were signalled."""
        with self._lock:
            self._terminated = True
            children = list(self._children)
        for proc in children:
            platform_compat.kill_popen_tree(proc)
        return len(children)


_scope_local = threading.local()


def _current_scope() -> InstallScope | None:
    scope = getattr(_scope_local, "scope", None)
    return scope if isinstance(scope, InstallScope) else None


def run_in_scope(scope: InstallScope, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Call *fn* with every :func:`_run` child on this thread owned by *scope*."""
    previous = getattr(_scope_local, "scope", None)
    _scope_local.scope = scope
    try:
        return fn(*args, **kwargs)
    finally:
        _scope_local.scope = previous


def _collect_after_kill(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Output a killed child printed, bounded by :data:`_REAP_TIMEOUT_S`."""
    try:
        out, err = proc.communicate(timeout=_REAP_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return "", ""
    return out or "", err or ""


def _run(argv: list[str], timeout: float, *, cwd: str | None = None) -> tuple[int, str, str]:
    """Run *argv*, returning ``(returncode, stdout, stderr)``.

    A timeout or a missing executable is a non-zero code with the reason on stderr.
    The child gets its own process group, and a timeout kills the whole group, so an
    expired ``npm install`` cannot leave grandchildren running. Inside an
    :class:`InstallScope` the child is registered for gateway shutdown, and a
    terminated scope answers :data:`INTERRUPTED_RC` without spawning.
    """
    scope = _current_scope()
    if scope is not None and scope.terminated:
        return INTERRUPTED_RC, "", _INTERRUPTED_REASON
    try:
        # npm and the agent-desktop CLI write in the host's locale.
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,  # subprocess-encoding: locale
            env=cli_env(),
            cwd=cwd,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
    except OSError as exc:
        return 127, "", f"{exc}"
    if scope is not None and not scope._adopt(proc):
        platform_compat.kill_popen_tree(proc)
        _collect_after_kill(proc)
        return INTERRUPTED_RC, "", _INTERRUPTED_REASON
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            platform_compat.kill_popen_tree(proc)
            _collect_after_kill(proc)
            return TIMEOUT_RC, "", f"timed out after {timeout:.0f}s: {' '.join(argv)}"
    finally:
        if scope is not None:
            scope._release(proc)
    if scope is not None and scope.terminated:
        return INTERRUPTED_RC, out or "", _INTERRUPTED_REASON
    return proc.returncode, out or "", err or ""


# npm-specific credential shapes the shared `redact_credentials` leaves intact: the
# registry query (`?_authToken=`), the .npmrc line (`//host/:_authToken=`), `*_TOKEN=`
# env echo, and scheme://user:secret@host. Scoped here: this is the surface that emits
# npm output. The `{0,40}` prefix is bounded on purpose: an unbounded run before a
# required keyword backtracks catastrophically on a large npm log.
_NPM_SECRET_RES = (
    re.compile(r"(_authToken\s*=\s*)[^\s&]+", re.I),
    re.compile(r"(_password\s*=\s*)[^\s&]+", re.I),
    re.compile(r"([A-Z0-9_]{0,40}(?:TOKEN|SECRET|PASSWORD|APIKEY|API_KEY)\s*=\s*)[^\s&]+", re.I),
    re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"),
)


def redact_install_output(text: str) -> str:
    """Redact credential-shaped content before it reaches a log or the dashboard.

    Runs the shared two-pass used on every external surface, then the npm shapes that
    pass leaves untouched (:data:`_NPM_SECRET_RES`). Any surface that renders installer
    output must use THIS redactor rather than the shared pair alone.
    """
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    for pattern in _NPM_SECRET_RES:
        text = pattern.sub(
            lambda m: m.group(1) + "[REDACTED]" + (m.group(m.re.groups) if m.re.groups > 1 else ""),
            text,
        )
    return text
