"""What a new terminal session starts: its shell and working directory, and the environment
the shell child inherits.

The bash readiness hook that announces the first prompt stays in the facade beside the
WebSocket route that installs it; its marker is stripped from the output stream in
``terminal_transport``.

An owner of :mod:`kiro_crew.dashboard.handlers.terminal`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import os
import shutil

from kiro_crew import platform_compat
from kiro_crew.sandbox import without_python_env


def _resolve_cwd(cfg: dict, requested: str | None) -> str:
    """Resolve the PTY working directory.

    A client-requested dir (the chat's project dir, passed as ?cwd=) must exist.
    Without one, use the configured cwd, else the user's home directory. The
    home is resolved with ``os.path.expanduser("~")``, which honours
    ``USERPROFILE`` on Windows (where ``HOME`` is unset for cmd/PowerShell) and
    ``HOME`` on POSIX, so each platform resolves to its own home. Reading
    ``HOME`` directly would yield ``"/"`` on a Windows shell with ``HOME`` unset,
    and ``os.path.abspath`` resolves ``"/"`` to the current drive root. The
    ``"/"`` last resort applies only when no home resolves at all (``expanduser``
    then returns the literal ``"~"``). This is the user's own interactive shell
    (auth is enforced at the WS handshake), so there is no root restriction
    beyond isdir.
    """
    home = os.path.expanduser("~")
    if home == "~":
        home = ""
    default = cfg.get("cwd") or home or "/"
    if requested:
        candidate = os.path.abspath(os.path.expanduser(requested))
        if os.path.isdir(candidate):
            return candidate
        raise ValueError("Terminal working directory does not exist")
    return default


def _resolve_shell(cfg: dict) -> tuple[str, str | None]:
    """Resolve the shell program the terminal launches.

    Resolution order is the configured ``dashboard.terminal.shell``, else
    ``$SHELL`` (POSIX only), else the platform default (``/bin/bash`` /
    ``powershell.exe``). Each candidate
    must now resolve to an executable (``shutil.which`` handles both absolute
    paths and bare names on ``PATH``). A configured value that does not resolve
    falls back rather than failing the open: a typo'd setting must never leave
    the user without a terminal.

    The value returned is the ABSOLUTE path ``shutil.which`` resolved, not the
    candidate as written: the spawn runs with the session's cwd (the chat's
    project directory), so a bare name would be resolved a second time there,
    and a relative ``PATH`` entry would let a project-planted executable win a
    race the validation here already decided. Pinning the resolved path makes
    the program validated the program spawned.

    Blocking note: ``shutil.which`` stats every ``PATH`` entry, so callers on
    the event loop must run this via an executor (they do — see the three call
    sites), never inline.

    Returns ``(shell, rejected)`` where ``rejected`` is the configured value
    when it was set but skipped, so callers can surface the fallback (session
    response, log) instead of silently launching a different shell.

    When no candidate resolves at all, the platform default is returned
    unvalidated so the spawn's own error — not a silent substitution — is what
    the user sees on such a host.
    """
    configured = str(cfg.get("shell") or "").strip()
    if configured:
        resolved = shutil.which(configured)
        if resolved:
            return os.path.abspath(resolved), None
    rejected = configured or None
    if platform_compat.IS_WINDOWS:
        candidates = ["powershell.exe"]
    else:
        env_shell = os.environ.get("SHELL", "")
        candidates = ([env_shell] if env_shell else []) + ["/bin/bash"]
    for cand in candidates:
        resolved = shutil.which(cand)
        if resolved:
            # abspath (both branches): `which` joins the matching PATH entry
            # verbatim, so a RELATIVE entry (PATH=bin:…) yields a relative
            # result the spawn's project cwd would re-resolve — exactly the
            # substitution pinning exists to prevent. Anchoring here binds the
            # path to the gateway cwd the validation ran in.
            return os.path.abspath(resolved), rejected
    return candidates[-1], rejected


def _is_bash_shell(shell: str) -> bool:
    """Whether *shell* supports the injected post-profile readiness marker."""
    name = shell.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in {"bash", "bash.exe"}


def _pty_child_env(extra: dict[str, str]) -> dict[str, str]:
    """Build the interactive shell's environment: the gateway environment plus
    *extra*, minus Kiro Crew's own Python startup variables.

    ``PYTHONPATH``/``PYTHONHOME``/``PYTHONPYCACHEPREFIX`` are searched BEFORE a
    venv's own site-packages, so leaking the gateway's copies makes a user's
    Python 3.13 venv import Kiro Crew's 3.12 site-packages and its C extensions
    fail to load. The agent surface strips them via
    ``sandbox.scrub_agent_subprocess_env``; this brings the terminal into line.

    Only the Python prefixes are dropped. Unlike the agent spawn, this is the
    user's own unsandboxed shell (see the spawn comment in ``api_terminal_ws``), so
    ``SSH_AUTH_SOCK``, the AWS vars and the rest of the credential-bearing
    environment must survive or git-over-SSH and the AWS CLI break in it.

    One variable is ADDED: ``BASH_SILENCE_DEPRECATION_WARNING=1``. macOS ships
    Bash 3.2, which prints a three-line "the default interactive shell is now
    zsh" notice on every interactive start. In this panel that banner is pure
    noise: it lands at the top of the transcript of a terminal the user opened
    inside Kiro Crew, it says nothing about Kiro Crew, and it repeats on every
    session. It also precedes the readiness marker, so it is the first thing any
    consumer of the stream has to skip. A user who wants the notice keeps it by
    exporting the variable themselves with an empty value, which this respects:
    an existing value of any kind wins, so this only fills a gap. The variable
    means nothing to other shells and to Bash on other platforms.
    """
    env = without_python_env({**os.environ, **extra})
    env.setdefault("BASH_SILENCE_DEPRECATION_WARNING", "1")
    return env
