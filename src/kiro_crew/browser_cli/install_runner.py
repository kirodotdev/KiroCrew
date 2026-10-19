"""How the ``@playwright/cli`` installer runs a child, and what of its output may be shown.

:mod:`kiro_crew.browser_cli.install` keeps the install steps; this module holds what they
run on: the time and size budgets of the steps and probes; the child's environment
(:func:`cli_env`); the :class:`InstallScope` through which the gateway stops one install
job's children; the one spawn every step and probe goes through (:func:`_run`); and
:func:`redact_install_output`, the redactor every surface that shows installer output must
use. It imports nothing else from ``browser_cli``, so every other part of the installer
can build on it.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import threading
from collections.abc import Callable
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.env import augmented_path, node_augmented_path
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# A version probe answers immediately or something is wrong; an install talks to
# the npm registry and then downloads a browser, so its budget is minutes.
_PROBE_TIMEOUT_S = 20.0
_NPM_INSTALL_TIMEOUT_S = 900.0
_BROWSER_INSTALL_TIMEOUT_S = 1800.0
_SKILLS_INSTALL_TIMEOUT_S = 180.0
_CLI_PACKAGE_JSON_MAX_BYTES = 1024 * 1024


def cli_env() -> dict[str, str]:
    """Environment for a CLI/npm child, with the Node bin directories on PATH.

    The gateway's own PATH is not sufficient: a global ``npm install -g`` lands
    in a version-manager-owned bin directory that the gateway process may never
    have had on PATH, so a child that inherits it unchanged cannot find the
    binary that was just installed.

    Two layers, node bins outermost so they win:

    1. :func:`node_augmented_path` prepends the Node bin dirs, so ``npm``/``node``
       resolve to the managed toolchain.
    2. :func:`augmented_path` (the inner layer) contributes the broad non-login
       PATH -- ``~/.local/bin``, ``/opt/homebrew/bin``, the mise shims -- because
       the mise-managed per-version ``npm`` is itself a wrapper script that runs
       ``mise reshim`` after a global install. That hook needs the ``mise``
       binary itself on PATH, and mise installs to ``~/.local/bin`` (or
       Homebrew's bin), NOT to any Node bin dir. A GUI- or daemon-launched
       gateway inherits a minimal PATH lacking those dirs, so without this layer
       the wrapper dies ``mise: command not found`` and, under its
       ``set -euo pipefail``, fails the whole ``npm install -g`` with rc 127.

    This PATH is execution support only. :func:`cli_path` does not consume it:
    gateway execution resolves the sandbox-sealed managed entrypoint or a fixed,
    non-writable system candidate by absolute path.

    ``NODE_USE_SYSTEM_CA=1`` makes these Node children trust the OS certificate
    store (macOS Keychain, Windows Certificate Store, OpenSSL defaults on Linux)
    in addition to Node's bundled roots. Behind a TLS-intercepting corporate
    proxy the browser download otherwise fails ``UNABLE_TO_GET_ISSUER_CERT_LOCALLY``
    even though the OS trusts the proxy's CA, and a GUI-launched gateway has no
    shell profile through which the user could set the variable. The gateway's
    own Python side already verifies through the OS store (``_ssl_compat``).
    The variable is used instead of ``--use-system-ca`` in ``NODE_OPTIONS``
    because a Node that predates the feature ignores an unknown variable but
    refuses to start on an unknown flag. A value the user already set is kept.
    """
    env = dict(os.environ)
    env["PATH"] = node_augmented_path(augmented_path(env.get("PATH", "")))
    env.setdefault("NODE_USE_SYSTEM_CA", "1")
    return env


#: Return code of a step refused or cut short because its :class:`InstallScope`
#: was terminated. 130 is the shell's "ended by interrupt" code, distinct from the
#: 124 timeout and 127 missing-executable codes :func:`_run` already reports.
INTERRUPTED_RC = 130
_INTERRUPTED_REASON = "interrupted: the gateway stopped this installer"
#: Return code :func:`_run` reports when the child outlives its timeout. Shared
#: with :func:`_step` so a timed-out step is not dressed as an ordinary failure.
TIMEOUT_RC = 124

#: How long a killed child gets to close its pipes before :func:`_run` stops
#: waiting for its output. The kill has already been sent; this bounds only the
#: collection of whatever it printed.
_REAP_TIMEOUT_S = 5.0


class InstallScope:
    """The installer children one install job owns, so the gateway can stop them.

    ``asyncio.to_thread`` cannot cancel its worker, and cancelling the awaiting
    task leaves the worker's subprocess running. The job's worker therefore runs
    inside a scope (:func:`run_in_scope`); every :func:`_run` call made on that
    thread registers its child here, and :meth:`terminate` kills each child's
    whole process group or tree. After termination the scope refuses to spawn,
    so the worker's remaining steps end at once instead of starting the next
    download.
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
        """Kill every live child and refuse new ones. Returns how many were signalled.

        Safe from any thread and idempotent. The kill is synchronous and does not
        wait for exit: the worker thread's own ``communicate`` reaps the child.
        """
        with self._lock:
            self._terminated = True
            children = list(self._children)
        for proc in children:
            platform_compat.kill_popen_tree(proc)
        return len(children)


# Named by the path callers import it from: an instance's ``repr`` and pickle render
# the class as ``module.qualname``, so the owner module must not leak into it.
InstallScope.__module__ = "kiro_crew.browser_cli.install"


_scope_local = threading.local()


def _current_scope() -> InstallScope | None:
    scope = getattr(_scope_local, "scope", None)
    return scope if isinstance(scope, InstallScope) else None


def run_in_scope(scope: InstallScope, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Call *fn* with every :func:`_run` child on this thread owned by *scope*.

    Meant as the target of ``asyncio.to_thread``: the scope is thread-local, so
    concurrent status probes on other executor threads are never adopted.
    """
    previous = getattr(_scope_local, "scope", None)
    _scope_local.scope = scope
    try:
        return fn(*args, **kwargs)
    finally:
        _scope_local.scope = previous


def kill_cli_process_tree(pid: int) -> None:
    """Signal *pid* and every descendant; never raises.

    The one pid-addressed tree kill ``browser_cli`` issues, for
    :mod:`kiro_crew.browser_cli.view`'s reaper, which holds a pid rather than a
    ``Popen``. This installer's timeout and cancel path holds the ``Popen`` and
    uses :func:`platform_compat.kill_popen_tree` instead. So the package adds a
    single site to the kill-attribution ratchet
    (``test_kill_chokepoint_ratchet.py``) rather than one per caller.
    """
    with contextlib.suppress(Exception):
        platform_compat.kill_process_tree(pid)


def _collect_after_kill(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Output a killed child printed, bounded by :data:`_REAP_TIMEOUT_S`."""
    try:
        out, err = proc.communicate(timeout=_REAP_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return "", ""
    return out or "", err or ""


def _run(argv: list[str], timeout: float, *, cwd: str | None = None) -> tuple[int, str, str]:
    """Run *argv*, returning ``(returncode, stdout, stderr)``.

    A timeout or a missing executable is reported as a non-zero return code with
    the reason on stderr, so callers branch on one shape instead of catching
    three exception types at every call site. *cwd* is the child's working
    directory; ``None`` inherits the gateway's, which is right for a PATH probe
    and wrong for the staged-copy smoke run (see :func:`_staged_node_runs`).

    The child gets its own process group, and a timeout kills that whole group
    rather than only the direct child, so an expired ``npm install`` cannot leave
    its download running: ``npm`` and the browser installer each start
    grandchildren. POSIX uses a new session (``setsid`` in the C fork path);
    Windows uses a new process group that
    :func:`platform_compat.kill_process_tree` walks. Inside an
    :class:`InstallScope` the child is also registered for gateway shutdown,
    and a terminated scope answers :data:`INTERRUPTED_RC` without spawning.
    """
    scope = _current_scope()
    if scope is not None and scope.terminated:
        return INTERRUPTED_RC, "", _INTERRUPTED_REASON
    try:
        # npm and playwright-cli write in the host's locale.
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


# A failing npm run can emit a very large log. The step keeps the redacted TAIL
# of stderr, capped at this many characters: Playwright prints the list of
# missing OS libraries last, so the tail is what the operator needs, not
# megabytes in a log line and a dashboard card. The remedy hint is carried
# separately and appended after the capped text.
_STDERR_CAP = 2000


# npm-specific credential shapes. The shared `redact_credentials` matches
# header-style secrets and (since the fetch-scheme widening) inline-credential
# http(s)/ftp(s) URLs, but still leaves the npm-specific forms intact -- the
# registry query (`?_authToken=`), the .npmrc line (`//host/:_authToken=`), and
# `*_TOKEN=` env echo. The URL pattern below stays as a backstop for schemes
# the shared alternation does not name.
# Scoped here rather than added to the shared helper: this is the one surface that
# emits npm output, and widening a security primitive every caller depends on is a
# change that deserves its own review.
_NPM_SECRET_RES = (
    re.compile(r"(_authToken\s*=\s*)[^\s&]+", re.I),
    re.compile(r"(_password\s*=\s*)[^\s&]+", re.I),
    # Bounded prefix ({0,40}), not `*`: an unbounded run before a required
    # keyword backtracks catastrophically on a large log -- MEASURED as a 120s
    # timeout on 50 KB of stderr, which would have hung the install task on a
    # real npm failure, not merely slowed a test.
    re.compile(r"([A-Z0-9_]{0,40}(?:TOKEN|SECRET|PASSWORD|APIKEY|API_KEY)\s*=\s*)[^\s&]+", re.I),
    # scheme://user:secret@host -- keep the user, drop the secret.
    re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"),
)


def redact_install_output(text: str) -> str:
    """Redact credential-shaped content before it reaches a log or the dashboard.

    Runs the shared two-pass used on every external surface, then the npm shapes
    that pass leaves untouched (see :data:`_NPM_SECRET_RES`). Public: any surface
    that renders installer output (step ``stderr``, the ``error`` fallback, or an
    exception message quoting an npm line) must use THIS redactor rather than the
    shared pair alone, or a bare ``_authToken=<value>`` assignment survives.
    """
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    for pattern in _NPM_SECRET_RES:
        # The last pattern has a trailing group (the `@`); the rest have one.
        text = pattern.sub(
            lambda m: m.group(1) + "[REDACTED]" + (m.group(m.re.groups) if m.re.groups > 1 else ""),
            text,
        )
    return text
