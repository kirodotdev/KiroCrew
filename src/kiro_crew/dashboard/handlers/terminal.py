"""WebSocket PTY handler for the built-in CLI panel."""

# This module is the terminal handler's facade: routes, the handlers package and tests
# import and patch every terminal name through it, as
# ``kiro_crew.dashboard.handlers.terminal.<name>``. A read of a name an owner holds is
# served from that owner, and a write -- ``monkeypatch.setattr``, ``mock.patch`` --
# reaches every module holding it (:mod:`kiro_crew._facade_mirror`). Owners never import
# this module. It keeps the WebSocket, completion and redaction routes and the title
# poller, with the helpers only they call (the bash readiness hook among them). New work goes to the owner of its responsibility:
#
#   ``terminal_session`` -- ``_TerminalSession``, the session registry, the terminal config gate
#   ``terminal_shell`` -- the shell, cwd and child environment a session starts with
#   ``terminal_transport`` -- which socket owns a session, and the PTY's bytes to and from it
#   ``terminal_lifecycle`` -- the shell child's liveness, exit, failure notice and teardown
#   ``terminal_placement`` -- which machine a chat's shell may run on, and retiring local ones
#   ``terminal_complete`` -- the vetting steps of path completion
#   ``terminal_routes`` -- the create, list and delete REST routes

from __future__ import annotations

import asyncio
import importlib as _importlib
import json
import logging
import os
import stat
import struct
import subprocess
import sys as _sys
import time
import uuid
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew import _facade_mirror, platform_compat
from kiro_crew.dashboard import terminal_commands
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.files import _PathProbeBusy, _run_path_probe
from kiro_crew.dashboard.handlers.terminal_complete import (
    _COMPLETE_MAX_ENTRIES,
    _COMPLETE_TOKEN_MAX,
    _log_complete,
    _resolve_vet_and_list,
    _split_path_token,
)
from kiro_crew.dashboard.handlers.terminal_lifecycle import (
    _TERMINAL_WS_CLOSE_SHELL_EXITED,
    _close_terminal_fd,
    _handle_pty_reader_end,
    _kill_session,
    _sess_alive,
    _sess_pid,
)
from kiro_crew.dashboard.handlers.terminal_placement import (
    _CHAT_NOT_LOADED,
    _PLACEMENT_UNAVAILABLE,
    _chat_gone,
    _local_terminal_refusal,
    _remote_pty_env,
    _remote_terminal_argv,
    _session_environments_on,
    retire_local_shells_of_chat,
)
from kiro_crew.dashboard.handlers.terminal_session import (
    _MAX_SESSION_ID_LEN,
    _MAX_SESSIONS,
    _completion_cfg,
    _completion_disabled,
    _get_config,
    _get_registry,
    _is_enabled,
    _sel,
    _TerminalSession,
)
from kiro_crew.dashboard.handlers.terminal_shell import (
    _is_bash_shell,
    _pty_child_env,
    _resolve_cwd,
    _resolve_shell,
)
from kiro_crew.dashboard.handlers.terminal_transport import (
    _close_terminal_ws_bounded,
    _record_and_forward_terminal_output,
    _release_terminal_ws_identity,
    _replace_terminal_ws,
    _resize_terminal,
    _send_owner_control_frame,
    _write_terminal_input,
)
from kiro_crew.dashboard.origin import check_origin, mark_audit_claimed
from kiro_crew.executors import discovery_executor, subprocess_executor
from kiro_crew.notifications.bus import NotificationBus
from kiro_crew.sandbox import RLIMIT_PROFILE_NONE, spawn_shim_argv
from kiro_crew.security import (  # noqa: F401 - is_sensitive_path: tests patch it here
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)

# PTY support is POSIX-only (openpty/fork/ioctl/termios). On Windows these
# modules do not exist; the web-terminal panel degrades to a clear error.
if platform_compat.IS_POSIX:
    import fcntl
    import pty as _pty
    import termios
else:  # pragma: no cover — Windows fallback
    fcntl = None  # type: ignore[assignment]
    _pty = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# A selected workspace can sit on an unresponsive mount. Stop waiting without
# holding terminal capacity or a dashboard-discovery worker indefinitely.
_TERMINAL_CWD_TIMEOUT_S = 5.0


def _proc_comm(pid: int) -> str | None:
    """Command name of a process (Linux /proc). None if unavailable.

    Read as bytes and decoded with ``replace``: a comm is whatever bytes the
    process named itself, and a multibyte name cut at the kernel's 15 bytes is
    not UTF-8. It is only ever shown as a title.
    """
    try:
        with open(f"/proc/{pid}/comm", "rb") as fh:
            return fh.read().decode("utf-8", "replace").strip() or None
    except OSError:
        return None


# Trusted absolute locations for the lsof binary used by the macOS/BSD cwd
# fallback. Resolving a bare "lsof" through inherited PATH would let anything
# that can prepend a PATH entry (e.g. an activated workspace virtualenv's bin/)
# hijack the spawn with gateway privileges, so we only ever execute these fixed
# system paths and fail closed (no cwd frame) when none exists.
_LSOF_PATHS = ("/usr/sbin/lsof", "/usr/bin/lsof")


def _proc_cwd(pid: int) -> str | None:
    """Current working directory of a process, or None if it cannot be read.

    Prefers the subprocess-free sources (``/proc`` on Linux, ``libproc`` on
    macOS) and only falls back to ``lsof -d cwd`` — whose ``-Fn`` output carries
    the path on an ``n``-prefixed line — on a host where neither answers. That
    fallback forks, so callers must still run this off the event loop.
    """
    cwd = platform_compat.process_cwd(pid)
    if cwd is not None:
        return cwd
    lsof = next((p for p in _LSOF_PATHS if os.path.isfile(p)), None)
    if not lsof:
        return None  # fail closed rather than resolve via PATH
    try:
        out = subprocess.run(
            [lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        for line in out.splitlines():
            if line.startswith("n") and len(line) > 1:
                return line[1:]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# How long a cwd probe may be reused. Short enough that `cd foo` followed
# immediately by a completion sees the new directory, long enough that one poll
# tick — which needs the cwd for both the title and the cwd frame — probes the
# shell once rather than twice, and that holding a key down does not re-probe
# per keystroke.
_CWD_PROBE_TTL_S = 0.4


def _session_cwd(sess: "_TerminalSession") -> str | None:
    """Full current working directory of the session's shell, or None.

    Memoized on the session for :data:`_CWD_PROBE_TTL_S`, because the answer has
    two consumers a fraction of a millisecond apart (the title label and the cwd
    frame) and on a host where ``_proc_cwd`` has to fork ``lsof`` the probe, not
    the polling, is the cost that matters.
    """
    if not platform_compat.IS_POSIX or sess.proc is None:
        return None
    now = time.monotonic()
    probe = sess.cwd_probe
    if probe is not None and now - probe[0] < _CWD_PROBE_TTL_S:
        return probe[1]
    cwd = _proc_cwd(sess.proc.pid)
    sess.cwd_probe = (now, cwd)
    return cwd


async def _session_cwd_cached(sess: "_TerminalSession") -> str | None:
    """``_session_cwd`` probed off the event loop, skipping the executor hop
    entirely on a memo hit.

    The title poller's ``sess.last_cwd`` is deliberately NOT reused here: it is
    refreshed on a 1 s cadence, so a completion issued right after a ``cd``
    would resolve against the previous directory. The TTL is not the only
    guard — the WebSocket write path drops the memo whenever the client submits
    a line, so a ``cd`` invalidates it immediately rather than after the TTL."""
    now = time.monotonic()
    probe = sess.cwd_probe
    if probe is not None and now - probe[0] < _CWD_PROBE_TTL_S:
        return probe[1]
    loop = asyncio.get_running_loop()
    cwd = await loop.run_in_executor(subprocess_executor(), _session_cwd, sess)
    # _session_cwd memoizes its own probes; this covers the branches where it
    # returns without probing (no shell, non-POSIX host) so those do not re-hop
    # to the executor on every keystroke.
    sess.cwd_probe = (now, cwd)
    return cwd


def _session_title(sess: "_TerminalSession") -> str | None:
    """Best-effort "what is this terminal doing" label: the foreground command
    name while one runs, else the shell's cwd basename. Returns None when it
    can't tell (client keeps its current title, so a host that can resolve
    neither source simply stays at the tab's cwd default).

    The cwd goes through :func:`_session_cwd` rather than :func:`_proc_cwd` so
    that deriving this label shares one probe with the poller's cwd frame."""
    if not platform_compat.IS_POSIX or sess.master_fd < 0 or sess.proc is None:  # wokeignore:rule=master
        return None
    try:
        fg = os.tcgetpgrp(sess.master_fd)  # wokeignore:rule=master
    except OSError:
        return None
    # setsid() makes the shell its own process-group leader (pgid == pid); a
    # foreground pgid different from that means a command is running.
    if fg > 0 and fg != sess.proc.pid:
        name = _proc_comm(fg)
        if name:
            return name
    cwd = _session_cwd(sess)
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or cwd
    return None


# The shells a code fence can name. FIXED here, never derived from anything a
# client, a fence or an agent supplies: the client selects among the entries
# this list produced, so no caller-supplied string is ever resolved or run.
_FENCE_SHELL_NAMES = ("bash", "sh", "zsh", "fish")


def _agent_can_rewrite(path: str, uid: int) -> bool:
    """Whether *path* or any ancestor could be rewritten by uid.

    Current mode bits are not the question: the OWNER of a directory can chmod it
    writable whenever it likes, so a user-owned ``0555`` directory is mutable in
    one syscall. Ownership is the durable property, and it has to hold for every
    ancestor too -- being able to rename a parent is enough to substitute
    everything under it.

    A world- or group-writable directory counts as rewritable UNLESS it carries
    the sticky bit, which is what stops a non-owner removing or renaming someone
    else's entry (``/tmp`` is the ordinary case).

    Two tests are needed and they catch different things. Ownership is the durable
    property: an owner can chmod at will, so current permission proves nothing
    about it. Mode bits are the cheap one. Note what this does NOT see: a POSIX
    ACL grant on an ancestor is invisible to ``st_mode``, and probing it with
    ``os.access`` here would read the REAL uid rather than this check's subject.
    Such an ACL can only be placed by root or by the directory's owner, and either
    of those can equally re-point the shell ``_resolve_shell`` itself resolves, so
    the exposure it would add over the base is nil. The candidate FILE is probed
    with ``os.access`` in the caller, where the subject is unambiguous.

    Fails closed: a path that cannot be stat'ed is treated as rewritable.
    """
    current = os.path.abspath(path)
    while True:
        try:
            st = os.lstat(current)
        except OSError:
            return True
        if st.st_uid == uid:
            return True
        loose = st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if loose and not st.st_mode & stat.S_ISVTX:
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _resolve_fence_shells(launched: str) -> dict[str, str]:
    """Fence-nameable shells that sit ALONGSIDE the shell *launched* came from.

    Reported to the client so a snippet handed to a different shell names an
    absolute path rather than a bare name -- the same reasoning as
    ``_resolve_shell``'s pinning: the terminal runs with the chat's project
    directory as its cwd, so a bare name would be resolved there, and a relative
    ``PATH`` entry would let a project-planted executable win.

    Discovery adds NO new trusted location and consults no ``PATH`` at all. Each
    name is probed directly inside the directory the session's own shell
    canonically came from, so the set reported here is exactly as trustworthy as
    the shell ``_resolve_shell`` already chose. A planted binary therefore cannot
    become a reported shell unless the attacker already controls that directory,
    in which case the spawn itself is compromised first and a fence tag adds
    nothing. Probing the directory rather than resolving and filtering also means
    an unrelated ``PATH`` entry earlier in the search order (a venv, an asdf shim)
    cannot shadow a co-located shell out of the map.

    COVERAGE, deliberately narrow: a shell is offered only when it is co-located
    with the session's own shell AND neither it nor any ancestor of that directory
    is OWNED by the gateway's user (an owner can chmod at will, so mode bits alone
    prove nothing). Nothing this process could rewrite is ever offered, which is
    what closes the swap-after-discovery window: a path is reported now and invoked
    later, when the user confirms. That leaves the ordinary distro layout
    (root-owned ``/usr/bin``) and excludes a user-owned prefix -- Homebrew's, a
    workspace, a project-local ``bin`` -- where the snippet then behaves exactly as
    it does today. Under a root gateway every path is owned by the caller, so
    nothing is offered at all; that is the safe direction.

    Blocking note: a bounded handful of stats, run in the same off-loop hop as
    ``_resolve_shell`` rather than inline.
    """
    if not launched:
        return {}
    trusted_dir = os.path.dirname(os.path.realpath(launched))
    if not trusted_dir:
        return {}
    if platform_compat.IS_WINDOWS:
        # POSIX-only by construction: there is no bash/sh/zsh/fish host here to
        # hand a snippet to, and the ownership test below has no Windows
        # equivalent -- report nothing rather than approximate it.
        return {}
    uid = os.geteuid()
    if _agent_can_rewrite(trusted_dir, uid):
        return {}
    found: dict[str, str] = {}
    for name in _FENCE_SHELL_NAMES:
        candidate = os.path.join(trusted_dir, name)
        if os.path.islink(candidate):
            continue  # the link target is outside what was vetted above
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        try:
            st = os.lstat(candidate)
        except OSError:
            continue
        if st.st_uid == uid or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            continue  # rewritable in place
        if os.access(candidate, os.W_OK):
            continue  # rewritable in place via an ACL the mode bits do not show
        found[name] = candidate
    return found


def _resolve_shell_with_fence_shells(cfg: dict) -> tuple[str, str | None, dict[str, str]]:
    """``_resolve_shell`` plus the fence-shell map, in one off-loop hop."""
    shell, rejected = _resolve_shell(cfg)
    return shell, rejected, _resolve_fence_shells(shell)


# Names the readiness hook reads. When the hook runs, both are consumed and unset
# at the first prompt, so nothing the user runs afterwards sees them. A profile
# that ASSIGNS PROMPT_COMMAND prevents the hook from running at all, and both
# then stay exported for that session -- inert, since the only thing that reads
# them is the hook that was replaced.
_READY_TOKEN_VAR = "KIROCREW_TERMINAL_READY_TOKEN"
_READY_HOOK_VAR = "KIROCREW_TERMINAL_READY_HOOK"
# Carries an inherited PROMPT_COMMAND across the hook's lifetime so the
# withdrawal can restore it instead of unsetting the variable. Absent when the
# gateway's own environment exported no PROMPT_COMMAND, which is the norm.
_READY_PREV_VAR = "KIROCREW_TERMINAL_READY_PREV"


def _bash_ready_env(token: str) -> dict[str, str]:
    """Environment that makes a real login Bash report readiness at its prompt.

    Bash reads an ``--init-file`` only when it is *not* a login shell, so the
    marker cannot ride an injected rc file without giving up ``-l`` — and giving
    up ``-l`` breaks the profile chain: ``shopt -q login_shell`` is then false, so
    every profile stanza guarded on login-ness silently no-ops and the user's
    environment never loads. Sourcing the same files from an rc file cannot
    substitute, because that option is read-only and stays off.

    A login shell does honour a ``PROMPT_COMMAND`` inherited from its
    environment, and runs it after the profile chain returns and before the first
    prompt — exactly where the readiness marker has to fire.

    The snippet is single-shot and self-removing: it emits only while the token
    variable is still set, unsets that token so neither a later prompt nor a
    child shell repeats the sequence, and withdraws itself from
    ``PROMPT_COMMAND`` only when that variable is still exactly the scalar that
    was exported. Both halves of that last test matter: a profile that APPENDED
    (``PROMPT_COMMAND="$PROMPT_COMMAND; history -a"``) no longer matches, and one
    that appended as an ARRAY (``PROMPT_COMMAND+=("history -a")``, Bash 5.1+)
    leaves the exported text as element zero — which the scalar expansion alone
    would match, so unsetting there would take the user's own element with it.
    ``${PROMPT_COMMAND[1]+x}`` distinguishes the two without Bash 5.1 syntax the
    ``/bin/bash`` on macOS (3.2) cannot parse.

    A profile that ASSIGNS ``PROMPT_COMMAND`` outright drops the hook, and with
    it the marker: that session's barrier never opens, and the client's own
    bounded timeout reports the failure without writing. That is the same
    fail-closed outcome the barrier already has for a profile that never returns,
    and it is deliberate — releasing on inferred progress instead risks handing a
    queued command to a profile that is still reading input, which corrupts it.

    One case is NOT fail-closed and is worth naming: a profile that installs its
    own hook only when the variable looks unset — ``[ -z "$PROMPT_COMMAND" ] &&
    PROMPT_COMMAND=…``, which is how the RHEL-family ``/etc/bashrc`` installs its
    terminal-title updater — sees the exported hook, skips its own install, and
    then this snippet withdraws itself, so that session ends with no prompt
    command at all. It cannot be fixed by choosing a better moment or a cleverer
    guard: every carrier a login shell inherits is visible to the profile chain,
    and the carriers that are invisible to it (``BASH_ENV``, non-interactive only;
    ``ENV``, POSIX mode only; ``INPUTRC``, cannot run commands) do not run at the
    post-profile point a readiness marker needs.
    An operator who EXPORTED ``PROMPT_COMMAND`` into the gateway's own
    environment keeps it: the exported value is the readiness hook followed by
    the inherited command, and the withdrawal restores the inherited command
    rather than unsetting the variable. Replacing it outright would be data loss,
    not just a lost nicety — ``PROMPT_COMMAND='history -a'`` is the standard way
    to make concurrent shells append to ``HISTFILE``, and without it a shell
    exiting OVERWRITES that file with its own in-memory list, dropping the
    commands every sibling shell had appended.
    """
    hook = (
        f'if [[ -n "${{{_READY_TOKEN_VAR}-}}" ]]; then '
        f"builtin printf '\\033]697;KiroCrewReady;%s\\007' "
        f'"${{{_READY_TOKEN_VAR}}}"; '
        f"builtin unset {_READY_TOKEN_VAR}; "
        f'if [[ "${{PROMPT_COMMAND-}}" == "${{{_READY_HOOK_VAR}-}}" '
        f'&& -z "${{PROMPT_COMMAND[1]+x}}" ]]; then '
        f'if [[ -n "${{{_READY_PREV_VAR}+x}}" ]]; then '
        f'PROMPT_COMMAND="${{{_READY_PREV_VAR}}}"; '
        f"else builtin unset PROMPT_COMMAND; fi; fi; "
        f"builtin unset {_READY_HOOK_VAR} {_READY_PREV_VAR}; fi"
    )
    inherited = os.environ.get("PROMPT_COMMAND") or ""
    # Blankness is tested on a stripped copy, but the value CARRIED is the raw
    # one: trailing whitespace can be escaped (`printf x\ `), and stripping that
    # turns the escape into a line continuation, which changes what the command
    # does rather than merely tidying it.
    has_inherited = bool(inherited.strip())
    # The exported value is executed as shell code, so the inherited command is
    # carried verbatim rather than quoted into an argument. It runs AFTER the
    # marker, which is the order the readiness signal needs: the marker must be
    # the first thing this prompt writes.
    exported = f"{hook}; {inherited}" if has_inherited else hook
    # _READY_HOOK_VAR mirrors the exported value (the WHOLE value, hook plus any
    # inherited tail) so the hook can tell "still mine" from "a profile has taken
    # this over" without pattern-matching its own text. Whenever the hook RUNS it
    # unsets the mirror on both branches, withdrawing or not; the third path is a
    # profile that replaced PROMPT_COMMAND, where the hook never runs and these
    # names stay exported for that session (see the docstring).
    env = {
        _READY_TOKEN_VAR: token,
        _READY_HOOK_VAR: exported,
        "PROMPT_COMMAND": exported,
    }
    if has_inherited:
        env[_READY_PREV_VAR] = inherited
    return env


async def api_terminal_ws(request: web.Request) -> web.WebSocketResponse | web.Response:
    """WebSocket PTY for the built-in CLI panel.

    Protocol:
      - Binary frames: raw terminal I/O (both directions)
      - Text frames (JSON): control messages
        - Client→Server: {"type":"resize","cols":N,"rows":N}
        - Client→Server: {"type":"ping"}
        - Server→Client: {"type":"pong"}
        - Server→Client: {"type":"exit","status":N|null} -- the shell exited on
          its own; the socket closes with 4001 and the session is reaped, so
          the client closes the tab instead of redialing. ``status`` is null
          when the exit code is unavailable.
    """
    # A WebSocket upgrade is a GET, and `csrf_middleware` validates the origin
    # only for unsafe methods, so the handshake would otherwise arrive
    # unchecked. The session cookie is attached automatically and SameSite=Lax
    # does not distinguish ports, so any other loopback origin could open a PTY
    # under the operator's own session. The other two WebSocket routes check in
    # their own handlers for the same reason (`ws.py`, `stt_stream.py`).
    if not check_origin(request, require=True):
        _sel().log_api_access(
            caller=request.get("user") or "unknown",
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=f"origin_not_allowed={request.headers.get('Origin', '')[:80]!r}",
        )
        # This record is the specific one; claim the request so the deny-audit
        # boundary does not add a second, generic entry for the same refusal.
        mark_audit_claimed(request)
        raise web.HTTPForbidden(text="WebSocket origin not allowed")
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(request, "terminal.ws.open")
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources="feature_disabled",
        )
        return web.Response(status=403, text="Terminal panel disabled")

    session_id = request.match_info.get("session_id", "")
    if not session_id or len(session_id) > _MAX_SESSION_ID_LEN:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=f"invalid_session_id={session_id!r}",
        )
        return web.Response(status=400, text="Invalid session_id")

    registry = _get_registry(request)
    cfg = _get_config(request)
    max_sessions = cfg.get("max_sessions", _MAX_SESSIONS)
    # Shell discovery scans PATH and can block on filesystem access, so run it
    # off-loop before reserving terminal capacity. Reconnects keep the shell
    # and fence-shell map saved with their existing session.
    shell, rejected_shell, fence_shells = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _resolve_shell_with_fence_shells, cfg,
    )

    # A terminal for a chat on a REMOTE session environment is a shell in that
    # workspace, resolved before the reservation below (it awaits). Never a local
    # fallback: it would send the user's commands to the wrong machine.
    remote_argv: list[str] | None = None
    remote_slot = request.query.get("remote_slot")
    chat_slot = request.query.get("slot")
    remote_error = ""
    error_code = ""
    refused = "remote_unavailable=1"
    reattach_only: _TerminalSession | None = None
    if isinstance(remote_slot, str) and remote_slot:
        remote_argv, remote_error, error_code = await _remote_terminal_argv(request, remote_slot)
    elif isinstance(chat_slot, str) and chat_slot:
        live = registry.get(session_id)
        remote_error, error_code = await _local_terminal_refusal(request, chat_slot, reattach=live)
        if (
            not remote_error
            and live is not None
            and _chat_gone(request, chat_slot)
            and _session_environments_on()
        ):
            # Allowed only to take over THAT shell: no new one opens for a gone chat.
            reattach_only = live
        refused = "local_refused=1"
    # A local shell remembers its chat, so binding that chat retires it.
    local_chat_slot = chat_slot if remote_argv is None and isinstance(chat_slot, str) else ""
    if remote_error:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=f"session={session_id},{refused},code={error_code}",
        )
        # A browser cannot read a refused handshake's body, so accept it and
        # send a coded error: wrong-machine parks the tab, placement-unavailable
        # lets it redial. No PTY and no reservation exist yet.
        refusal_ws = web.WebSocketResponse(heartbeat=30, timeout=300)
        await refusal_ws.prepare(request)
        await _close_terminal_ws_bounded(
            refusal_ws, error_message=remote_error, error_code=error_code
        )
        return refusal_ws

    # A None entry reserves this session id and capacity throughout opening,
    # including asynchronous setup and any refusal response. Reject duplicate
    # opens until the entry becomes a session or its opening handler removes
    # the reservation; treating it as absent could spawn duplicate PTYs.
    if session_id in registry and registry[session_id] is None:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=f"session={session_id},reservation_in_flight=1",
        )
        return web.Response(
            status=409, text="Terminal session is already being opened"
        )
    existing = registry.get(session_id)
    if existing and not _sess_alive(existing):
        # Process died — clean up stale entry. The reader is cancelled first:
        # the kill's awaits would otherwise let it reach its exit handling and
        # reap this entry as a shell exit under the teardown.
        if existing.reader_task is not None:
            existing.reader_task.cancel()
        await _kill_session(existing)
        # DELETE or another opener can replace the entry while teardown waits.
        if registry.get(session_id) is not existing:
            _sel().log_api_access(
                caller=caller,
                operation="terminal.ws.open",
                outcome="denied",
                source="dashboard",
                resources=f"session={session_id},session_changed=1",
            )
            return web.Response(status=409, text="Terminal session changed while opening")
        del registry[session_id]
        existing = None

    if reattach_only is not None and existing is not reattach_only:
        # The gone chat's shell exited or was replaced while this dial awaited:
        # its placement is unknown again, so no new local shell opens.
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=f"session={session_id},local_refused=1,code={_PLACEMENT_UNAVAILABLE}",
        )
        refusal_ws = web.WebSocketResponse(heartbeat=30, timeout=300)
        await refusal_ws.prepare(request)
        await _close_terminal_ws_bounded(
            refusal_ws, error_message=_CHAT_NOT_LOADED, error_code=_PLACEMENT_UNAVAILABLE
        )
        return refusal_ws

    # Reserve slot synchronously before any await to prevent race condition
    if not existing and len(registry) >= max_sessions:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.open",
            outcome="denied",
            source="dashboard",
            resources=f"max_sessions={max_sessions}",
        )
        return web.Response(status=429, text=f"Max {max_sessions} terminal sessions")

    # Reserve a placeholder so concurrent requests see the slot as taken
    placeholder = not existing
    if placeholder:
        registry[session_id] = None

    try:
        ws = web.WebSocketResponse(heartbeat=30, timeout=300)
        cwd = ""
        if placeholder:
            try:
                requested_cwd = request.query.get("cwd")
                if requested_cwd:
                    cwd = await asyncio.wait_for(
                        _run_path_probe(
                            _resolve_cwd, cfg, requested_cwd, stall_path=requested_cwd
                        ),
                        timeout=_TERMINAL_CWD_TIMEOUT_S,
                    )
                else:
                    # Selecting the configured/HOME/root default performs no
                    # path stat and remains available when probes are exhausted.
                    cwd = _resolve_cwd(cfg, None)
            except (OSError, ValueError, asyncio.TimeoutError, _PathProbeBusy):
                registry.pop(session_id, None)
                placeholder = False
                _sel().log_api_access(
                    caller=caller,
                    operation="terminal.ws.open",
                    outcome="denied",
                    source="dashboard",
                    resources=(
                        f"session={session_id},invalid_cwd=1," f"cwd={request.query.get('cwd')!r}"
                    ),
                )
                # A browser WebSocket cannot read a rejected handshake's JSON
                # body. No shell can be created after refusal, so retire the
                # reservation before even a slow handshake can retain it.
                await ws.prepare(request)
                await _close_terminal_ws_bounded(
                    ws,
                    error_message="Terminal working directory is unavailable",
                    error_code="terminal_invalid_cwd",
                )
                return ws
        await ws.prepare(request)

        if existing:
            # Reconnect to existing PTY.
            replaced = await _replace_terminal_ws(existing, ws)
            if not replaced:
                _sel().log_api_access(
                    caller=caller,
                    operation="terminal.ws.reconnect",
                    outcome="error",
                    source="dashboard",
                    resources=f"session={session_id},takeover_failed=1",
                )
                await _close_terminal_ws_bounded(
                    ws,
                    error_message="Terminal reconnect failed",
                )
                return ws
            sess = existing
            if registry.get(session_id) is not sess:
                # A shell exit reaped this session while the reconnect queued on
                # replace_lock, so the socket was published on a dead entry. The
                # 4001 close tells this window what the exit already told the last
                # one: the shell is gone, close the tab rather than redial.
                _sel().log_api_access(
                    caller=caller,
                    operation="terminal.ws.reconnect",
                    outcome="error",
                    source="dashboard",
                    resources=f"session={session_id},reaped_during_reconnect=1",
                )
                await _close_terminal_ws_bounded(
                    ws, close_code=_TERMINAL_WS_CLOSE_SHELL_EXITED
                )
                return ws
            _sel().log_api_access(
                caller=caller,
                operation="terminal.ws.reconnect",
                outcome="ok",
                source="dashboard",
                resources=f"session={session_id},pid={_sess_pid(sess)}",
            )
        elif platform_compat.IS_WINDOWS:
            # Windows: spawn a ConPTY-backed shell (PowerShell by default). There is
            # no POSIX pty/fork; kiro_crew.conpty drives the Win32 pseudo-console via
            # ctypes (stdlib, no extra dependency).
            from kiro_crew.conpty import WindowsPty

            if rejected_shell:
                logger.warning(
                    "terminal: configured shell %r not executable; falling back to %r",
                    rejected_shell, shell,
                )
            if not request.query.get("cwd") and not os.path.isdir(cwd):
                cwd = os.path.expanduser("~")
            env = _pty_child_env({"KIROCREW_TERMINAL": "1"})
            argv = [shell, "-NoLogo"] if "powershell" in shell.lower() else [shell]
            if remote_argv is not None:
                argv, cwd = remote_argv, os.path.expanduser("~")
                env = _remote_pty_env({"KIROCREW_TERMINAL": "1"})
            try:
                wp = WindowsPty(argv, cwd=cwd, env=env, cols=80, rows=24)
            except Exception as exc:
                registry.pop(session_id, None)
                placeholder = False
                _sel().log_api_access(
                    caller=caller, operation="terminal.ws.open",
                    outcome="error", source="dashboard",
                    resources=f"conpty_spawn_failed={exc}",
                )
                if not ws.closed:
                    await ws.send_str(json.dumps(
                        {"type": "error", "message": f"Failed to start terminal: {exc}"}
                    ))
                    await ws.close()
                return ws
            sess = _TerminalSession(
                session_id=session_id, master_fd=-1, proc=None, winpty=wp, ws=ws, shell=shell,  # wokeignore:rule=master
                fence_shells=fence_shells, remote=remote_argv is not None,
                chat_slot=local_chat_slot,
            )
            registry[session_id] = sess
            placeholder = False
            _sel().log_api_access(
                caller=caller, operation="terminal.ws.open",
                outcome="ok", source="dashboard",
                resources=f"session={session_id},pid={wp.pid},shell={shell}",
            )
        else:
            if rejected_shell:
                logger.warning(
                    "terminal: configured shell %r not executable; falling back to %r",
                    rejected_shell, shell,
                )
            # Spawn new PTY. Bash is a real login shell (`-l`) so the user's own
            # profile chain runs with `shopt -q login_shell` true, and it inherits a
            # PROMPT_COMMAND that emits a definitive readiness marker once the
            # profiles return. Foreground process ownership alone is insufficient: a
            # profile's builtin `read` runs in the shell process and would consume an
            # early command batch.
            master_fd, worker_fd = _pty.openpty()  # wokeignore:rule=master
            sess = _TerminalSession(
                session_id=session_id,
                master_fd=master_fd,  # wokeignore:rule=master
                proc=None,
                ws=ws,
                shell=shell,
                fence_shells=fence_shells,
                remote=remote_argv is not None,
                chat_slot=local_chat_slot,
            )
            ready_marker: bytes | None = None
            close_cancelled: asyncio.CancelledError | None = None
            spawn_error: BaseException | None = None
            try:
                try:
                    fcntl.ioctl(
                        worker_fd,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", 24, 80, 0, 0),
                    )
                    env = _pty_child_env({
                        "TERM": "xterm-256color",
                        "KIROCREW_TERMINAL": "1",
                        # Export the shell actually being spawned (already resolved to
                        # an absolute path). Without this, a configured shell that
                        # differs from the login shell leaves the inherited $SHELL
                        # pointing at the login shell, so programs that consult it
                        # (vim's :sh, tmux default-shell) open the wrong one. POSIX
                        # branch only: PowerShell does not consult $SHELL.
                        "SHELL": shell,
                    })
                    # Security: intentionally unsandboxed — this is the user's own
                    # interactive terminal (like SSH), not agent-executed code.
                    # Auth is enforced at WS handshake via token_auth_middleware.
                    # See CLI_PANEL_DESIGN.md §8 "Security Considerations".
                    # The PTY has to become the child's CONTROLLING terminal or Ctrl+C
                    # reaches nothing: the kernel needs a foreground process group to
                    # deliver SIGINT to, and inheriting an already-open terminal
                    # descriptor does not confer one. It has to be claimed, after
                    # setsid(), by the session leader itself.
                    #
                    # The claim is made AFTER exec, by the post-exec shim, against fd 0
                    # (the PTY, below). Asking for it with preexec_fn instead is what
                    # makes CPython fork this whole multi-GB, ~120-thread gateway and run
                    # Python in the clone before exec, and the ioctl is not what costs:
                    # the page-table copy blocks the event loop for ~107ms per terminal
                    # open at 3GB resident, and a clone that cannot reach exec blocks it
                    # without bound, because the parent waits inside an un-awaitable
                    # os.read on the loop thread that no timeout can reach. The shim runs
                    # the same ioctl single-threaded in the exec'd child, where none of
                    # that applies -- see the module docstring of _spawn_exec_shim.py.
                    argv = [shell, "-l"]
                    if remote_argv is not None:
                        # The provider CLI opens the remote login shell; the local cwd
                        # and the local bash ready-marker do not apply to it.
                        argv, cwd = remote_argv, os.path.expanduser("~")
                        env = _remote_pty_env({"TERM": "xterm-256color", "KIROCREW_TERMINAL": "1"})
                    elif _is_bash_shell(shell):
                        token = uuid.uuid4().hex
                        ready_marker = (
                            f"\x1b]697;KiroCrewReady;{token}\x07".encode("ascii")
                        )
                        env.update(_bash_ready_env(token))

                    # RLIMIT_PROFILE_NONE: the user's own interactive shell carries no
                    # rlimits and no OOM bias, and never did. The controlling terminal is
                    # the only thing this spawn asks the shim for.
                    ctty_shim = spawn_shim_argv(RLIMIT_PROFILE_NONE, ctty_fd=0)
                    if not ctty_shim:
                        # Deliberately NOT falling back to preexec_fn: reintroducing the
                        # fork is the whole defect this spawn is avoiding, and a terminal
                        # whose Ctrl+C is dead is a smaller harm than a gateway the
                        # watchdog kills. Reachable only on a truncated install, where
                        # the shim source could not be captured at import.
                        logger.warning(
                            "terminal: post-exec shim unavailable; opening the shell with no "
                            "controlling terminal, so Ctrl+C will not reach it"
                        )

                    proc = await asyncio.create_subprocess_exec(
                        *ctty_shim,
                        *argv,
                        stdin=worker_fd,
                        stdout=worker_fd,
                        stderr=worker_fd,
                        start_new_session=True,
                        cwd=cwd,
                        env=env,
                    )
                    # Own the returned child before closing the parent's worker
                    # descriptor: cancellation at that await must reap it too.
                    sess.proc = proc
                    sess.ready_marker = ready_marker
                except asyncio.CancelledError as cancellation:
                    close_cancelled = cancellation
                    raise
                except BaseException as error:
                    spawn_error = error
                    raise
                finally:
                    try:
                        await _close_terminal_fd(worker_fd)
                    except (OSError, RuntimeError, asyncio.TimeoutError):
                        # A failed setup reports its own cause: the close that
                        # follows it must not replace that error for the owner.
                        if spawn_error is None:
                            raise
                        logger.warning(
                            "terminal: worker descriptor close failed after a setup error",
                            exc_info=True,
                        )
                    finally:
                        if close_cancelled is not None:
                            raise close_cancelled
            except BaseException as exc:
                # Keep both the reservation and teardown owned until disposal
                # settles, even if another cancellation arrives while queued.
                cleanup = asyncio.create_task(_kill_session(sess))
                try:
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError as cancellation:
                            exc = cancellation
                    cleanup.result()
                finally:
                    if not isinstance(exc, Exception):
                        raise exc
                registry.pop(session_id, None)
                placeholder = False
                # WS already prepared — send error over WS then close. A timed-out
                # descriptor close carries no text of its own.
                _sel().log_api_access(
                    caller=caller,
                    operation="terminal.ws.open",
                    outcome="error",
                    source="dashboard",
                    resources=f"pty_spawn_failed={exc}",
                )
                if not ws.closed:
                    message = str(exc) or f"Failed to start terminal: {type(exc).__name__}"
                    await ws.send_str(json.dumps({"type": "error", "message": message}))
                    await ws.close()
                return ws

            registry[session_id] = sess
            placeholder = False
            _sel().log_api_access(
                caller=caller,
                operation="terminal.ws.open",
                outcome="ok",
                source="dashboard",
                resources=f"session={session_id},pid={proc.pid},shell={shell}",
            )
            if ready_marker is None:
                # The reliable injection above intentionally targets Bash, the
                # reported shell. Configured shells whose startup protocol we cannot
                # control fall back to transport-ready.
                sess.shell_ready = True
                try:
                    async with sess.send_lock:
                        if sess.ws is ws and not ws.closed:
                            await ws.send_str(json.dumps({
                                "type": "ready",
                                "shell": sess.shell,
                                "fence_shells": sess.fence_shells,
                            }))
                except (ConnectionResetError, RuntimeError, OSError):
                    pass

    finally:
        # Only this opener can remove its reservation. Publication or an early
        # release retires that ownership before another handler can reserve it.
        if placeholder:
            registry.pop(session_id, None)

    # The placement check above ran before this shell was published, and a bind
    # in between skipped the then-unpublished reservation: read it again now
    # that the shell is visible, so either this read or the bind's retire sees it.
    if sess.chat_slot and registry.get(session_id) is sess:
        why, code = await _local_terminal_refusal(
            request, sess.chat_slot, reattach=sess if existing else None
        )
        if code:
            await retire_local_shells_of_chat(
                registry, sess.chat_slot, error_message=why, error_code=code
            )
            return ws

    # --- Read loop: PTY → WebSocket ---
    notification_bus: NotificationBus | None = getattr(
        request.app.get("state"), "notification_bus", None
    )

    async def read_pty():
        reader_ended = False
        try:
            loop = asyncio.get_running_loop()
            if sess.winpty is not None:
                reader = lambda: sess.winpty.read(4096)  # noqa: E731
            else:
                reader = lambda: os.read(sess.master_fd, 4096)  # noqa: E731  # wokeignore:rule=master
            while True:
                data = await loop.run_in_executor(None, reader)
                if not data:
                    reader_ended = True
                    break
                await _record_and_forward_terminal_output(sess, data)
        except OSError:
            # EIO: the child exited, or a teardown closed the fd under us.
            # _handle_pty_reader_end tells those apart.
            reader_ended = True
        if reader_ended:
            await _handle_pty_reader_end(registry, sess, bus=notification_bus)

    if sess.reader_task is None or sess.reader_task.done():
        sess.reader_task = asyncio.ensure_future(read_pty())

    # --- Write loop: WebSocket → PTY ---
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                try:
                    accepted = await _write_terminal_input(sess, ws, msg.data)
                except OSError:
                    break
                if not accepted:
                    _sel().log_api_access(
                        caller=caller,
                        operation="terminal.ws.input",
                        outcome="denied",
                        source="dashboard",
                        resources=f"session={session_id},stale_owner=1",
                    )
                    # The displaced handler ends here: later stale frames are
                    # never read, so one denial audit is the ceiling per socket.
                    break
            elif msg.type == web.WSMsgType.TEXT:
                try:
                    ctrl = json.loads(msg.data)
                except ValueError:
                    continue
                if ctrl.get("type") == "resize":
                    try:
                        cols = min(max(int(ctrl.get("cols", 80)), 1), 500)
                        rows = min(max(int(ctrl.get("rows", 24)), 1), 200)
                    except (ValueError, TypeError):
                        continue
                    if not await _resize_terminal(sess, ws, cols, rows):
                        _sel().log_api_access(
                            caller=caller,
                            operation="terminal.ws.resize",
                            outcome="denied",
                            source="dashboard",
                            resources=f"session={session_id},stale_owner=1",
                        )
                        break
                elif ctrl.get("type") == "ping":
                    await _send_owner_control_frame(sess, ws, {"type": "pong"})
            elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                break
    finally:
        # WS disconnected — mark for orphan reaper, but keep PTY alive.
        # Identity-guarded: a reconnect (e.g. the terminal panel popping out to
        # its own window) REPLACES sess.ws while this displaced handler is
        # still draining; unconditionally clearing it here would silence PTY
        # output to the freshly attached socket.
        _release_terminal_ws_identity(sess, ws)
        _sel().log_api_access(
            caller=caller,
            operation="terminal.ws.disconnect",
            outcome="ok",
            source="dashboard",
            resources=f"session={session_id}",
        )

    return ws


# Selection hand-off size cap. Generous for terminal selections (xterm buffers
# are bounded anyway) while preventing a multi-megabyte POST from tying up the
# redactors on the event loop's executor.
_REDACT_MAX_BYTES = 256 * 1024


async def api_terminal_redact(request: web.Request) -> web.Response:
    """POST /api/terminal/redact — scan a COMPLETE terminal selection before it
    is inserted into chat. This is the whole credential boundary for the web
    terminal: the live PTY stream is forwarded to the browser unscanned, and the
    scrollback ring buffer is replayed only there, so the selection hand-off is
    the one path by which terminal output reaches a model. It therefore runs
    unconditionally, and callers MUST fail closed: no chat insertion unless this
    returns 200 with redacted text."""
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.selection.redact",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(
        request, "terminal.selection.redact"
    )
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _sel().log_api_access(
            caller=caller,
            operation="terminal.selection.redact",
            outcome="denied",
            source="dashboard",
            resources="feature_disabled",
        )
        return web.Response(status=403, text="Terminal panel disabled")
    try:
        body = await request.json()
        text = body["text"]
        if not isinstance(text, str):
            raise TypeError
    except Exception:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.selection.redact",
            outcome="denied",
            source="dashboard",
            resources="invalid_body",
        )
        return web.json_response(
            {"error": "expected JSON body {text: string}", "code": "terminal_invalid_body"},
            status=400,
        )
    if len(text.encode("utf-8", errors="replace")) > _REDACT_MAX_BYTES:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.selection.redact",
            outcome="denied",
            source="dashboard",
            resources="selection_too_large",
        )
        return web.json_response(
            {"error": "selection too large", "code": "terminal_selection_too_large"},
            status=413,
        )
    # This is the ONLY credential scan on the path from PTY output to a model,
    # so it runs unconditionally and there is no configuration that skips it.
    # A contiguous selection is also the only input the redactors can be
    # accurate on: they are regex scans, and a secret split across two reads is
    # invisible to a per-chunk scan by construction.
    # Run off-loop: the redactors scale with input size.
    loop = asyncio.get_running_loop()

    def _scan(t: str) -> str:
        t, _ = redact_exfiltration_urls(t)
        t, _ = redact_credentials(t)
        return t

    try:
        redacted = await loop.run_in_executor(subprocess_executor(), _scan, text)
    except Exception:
        # Fail closed: the caller gets no text to insert.
        logger.exception("terminal: selection redaction failed")
        _sel().log_api_access(
            caller=caller,
            operation="terminal.selection.redact",
            outcome="error",
            source="dashboard",
            resources="redaction_failed",
        )
        return web.json_response(
            {"error": "redaction failed", "code": "terminal_redaction_failed"},
            status=500,
        )
    _sel().log_api_access(
        caller=caller,
        operation="terminal.selection.redact",
        outcome="ok",
        source="dashboard",
        resources="ok",
    )
    return web.json_response({"text": redacted})


async def api_terminal_complete(request: web.Request) -> web.Response:
    """POST /api/terminal/complete — completions for the word under a terminal cursor.

    Two mutually exclusive tiers, chosen by the CLIENT because only the client can
    see the screen row:

    * **path** (no ``argv`` in the body) — the default tier. Body
      ``{session_id, token, folders_only?}`` where ``token`` is the DEQUOTED
      literal path the cursor sits in (``"../Kiro"``, ``"src/"``, ``""``); the
      client decodes backslash escapes before asking, so an on-screen ``my\\ dir/``
      arrives here as ``my dir/``.
    * **command** (``argv`` present) — subcommands and flags for an allowlisted
      CLI, e.g. ``argv: ["gh", "pr"]`` with ``token: "cre"``. See
      ``dashboard/terminal_commands.py`` for the protocols and the authority
      argument.

    The tiers do not fall back into one another. The client sends ``argv`` only for
    a word that cannot be a path (no separator, not ``~``-rooted) under a command
    that is not a known path command, so the two never both apply — and keeping
    them disjoint means the path tier's response shape is untouched by this
    addition.

    Authority note (path tier): this lists a directory on behalf of an
    authenticated caller who already owns a LIVE PTY in this gateway — i.e. an
    interactive shell with the gateway user's full filesystem access. Requiring an
    existing session id is what keeps it from being a general filesystem-
    enumeration endpoint; it grants nothing the session's own `ls` does not. Paths
    are therefore resolved without a root restriction, exactly like the shell
    would — with one carve-out: the governance trust-root and credential dirs
    (``is_sensitive_path``) are never enumerated, and no individual ENTRY inside an
    allowed directory is returned if it (or its symlink target) is itself
    protected, so the panel cannot be used to harvest protected metadata names."""
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.complete",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(request, "terminal.complete")
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _log_complete(caller, "denied", "feature_disabled")
        return web.Response(status=403, text="Terminal panel disabled")
    try:
        body = await request.json()
        session_id = body["session_id"]
        token = body.get("token", "")
        folders_only = body.get("folders_only", False)
        # folders_only is type-checked like session_id/token instead of being
        # coerced: bool("false") is True, so a client sending the JSON STRING
        # would silently get files dropped from every listing.
        if (
            not isinstance(session_id, str)
            or not isinstance(token, str)
            or not isinstance(folders_only, bool)
        ):
            raise TypeError
    except Exception:
        _log_complete(caller, "denied", "invalid_body")
        return web.json_response(
            {
                "error": "expected JSON body "
                         "{session_id: string, token?: string, folders_only?: boolean, "
                         "argv?: string[]}",
                "code": "terminal_invalid_body",
            },
            status=400,
        )
    if len(token) > _COMPLETE_TOKEN_MAX:
        _log_complete(caller, "denied", "token_too_long")
        return web.json_response(
            {"error": "token too long", "code": "terminal_token_too_long"}, status=413
        )

    sess = _get_registry(request).get(session_id)
    if sess is None:
        _log_complete(caller, "denied", "unknown_session")
        return web.json_response(
            {"error": "Unknown terminal session", "code": "terminal_unknown_session"},
            status=404,
        )

    # Split BEFORE any filesystem work: `prefix` is a pure function of the token
    # and the completion gate below needs it for the empty answer, so the cwd
    # probe must not run ahead of a request that is going to be suppressed.
    dir_part, prefix = _split_path_token(token)

    # `argv` is validated HERE, above the completion gate, because it is a
    # BODY-SHAPE check like the session_id/token/folders_only ones further up: a
    # malformed request must keep its 400 and its `invalid_argv` denial audit
    # whether completion is on or off. Gating first would turn garbage argv into a
    # silent 200 and drop the refusal from the SEL trail — the client would read a
    # contract violation as "no suggestions". Parsing is pure (no filesystem, no
    # subprocess), so doing it before the gate keeps the gate ahead of all real
    # work; `argv is None` below therefore means "no argv in the body", since an
    # unparsable one has already returned 400.
    raw_argv = body.get("argv")
    argv = None
    if raw_argv is not None:
        argv = terminal_commands.parse_argv(raw_argv)
        if argv is None:
            _log_complete(caller, "denied", "invalid_argv")
            return web.json_response(
                {
                    "error": "argv must be a non-empty list of plain words whose first "
                             "entry is a bare command name",
                    "code": "terminal_invalid_argv",
                },
                status=400,
            )

    # `completion` is read ONCE per request, above the tier split, and reused for
    # both the gate here and the engine's operator command map below. Read off the
    # event loop (`_get_config` does a synchronous `read_text()` of config.json and
    # this route fires per keystroke, so on a slow home filesystem an inline read
    # would stall every gateway task) and type-checked at both nesting levels.
    completion_cfg = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _completion_cfg, request,
    )
    if _completion_disabled(completion_cfg):
        # Gated ABOVE the tier split, so `completion.enabled: false` silences the
        # `cd ` path popup as well as subcommand/flag suggestions — gating only the
        # command tier would leave the path popup alive.
        #
        # The empty listing, NOT a 403: 403 is `_is_enabled`'s whole-panel signal
        # and the client treats it differently. This is the same shape the
        # unknown-cwd branch returns, so a configured silence needs no frontend
        # change. Audited as `ok`: nothing was refused, and naming the state lets
        # an operator tell a configured silence from a broken route.
        _log_complete(caller, "ok", "completion_disabled")
        return web.json_response(
            {"dir": None, "prefix": prefix, "entries": [], "truncated": False}
        )

    if sess.remote:
        # A workspace shell: its directory and files are on the remote machine, so
        # a listing here would offer this gateway's paths for the remote command
        # line. The same empty shape as a configured silence.
        _log_complete(caller, "ok", "remote_session")
        return web.json_response(
            {"dir": None, "prefix": prefix, "entries": [], "truncated": False}
        )

    cwd = await _session_cwd_cached(sess)

    # ── Command tier ──
    # Ordered before the unknown-cwd branch: a subcommand list does not depend on
    # the working directory (a cobra probe answers without one), so a session whose
    # cwd cannot be read still gets `gh pr` completions even though it can get no
    # path ones.
    if argv is not None:
        # Already parsed and validated above the completion gate, so this branch
        # reuses it rather than parsing twice; a non-None value is by construction
        # a well-formed argv.
        # `completion_cfg` was read once above the tier split — off the event loop
        # and type-checked at both nesting levels — so this branch reuses it
        # rather than paying a second per-keystroke read of config.json.
        cmd_entries, reason = await terminal_commands.complete(
            argv, token, cwd, completion_cfg.get("commands"),
        )
        _log_complete(caller, "denied" if reason == "sensitive_path" else "ok", reason)
        # `dir: null` — the same "nothing was resolved on the filesystem" signal
        # the path tier uses for an unknown cwd, so the client needs no new
        # top-level field to tell a command answer from a path one; the per-entry
        # `kind` carries that.
        return web.json_response(
            {
                "dir": None,
                "prefix": prefix,
                "entries": [e.to_json() for e in cmd_entries],
                "truncated": False,
            }
        )

    if not cwd:
        # cwd is unknowable (Windows, or the probe failed) — no completions
        # rather than an error the frontend would have to special-case. A null
        # ``dir`` is the signal that nothing was resolved.
        _log_complete(caller, "ok", "no_cwd")
        return web.json_response(
            {"dir": None, "prefix": prefix, "entries": [], "truncated": False}
        )

    loop = asyncio.get_running_loop()
    # discovery_executor, not subprocess_executor: this is a read-only
    # filesystem scan, and subprocess_executor's workers are shared with PTY
    # teardown (an os.close that can wedge in the kernel) — a slow directory
    # here must not be able to occupy a thread that session cleanup needs.
    # Resolution and vetting ride the SAME hop as the listing: all three touch
    # the filesystem (or the name service, via ``~user`` expansion), so none of
    # them may run inline in this coroutine, and one hop keeps a keystroke's
    # latency to a single thread round-trip.
    directory, allowed, entries, truncated = await loop.run_in_executor(
        discovery_executor(),
        _resolve_vet_and_list,
        cwd,
        dir_part,
        prefix,
        folders_only,
        _COMPLETE_MAX_ENTRIES,
    )
    if not allowed:
        # Protected tree (or a symlink resolving into one). Answer with the SAME
        # empty-listing shape as the unknown-cwd branch so the client needs no
        # special case — and so the response does not disclose whether the path
        # exists.
        _log_complete(caller, "denied", "sensitive_path")
        return web.json_response(
            {"dir": None, "prefix": prefix, "entries": [], "truncated": False}
        )
    _log_complete(caller, "ok", "listed")
    return web.json_response(
        {
            # The LEXICAL path, not the canonicalized one used for the gate: this
            # is displayed back to the user, who typed it, and /tmp reading as
            # /private/tmp would be confusing.
            "dir": directory,
            "prefix": prefix,
            "entries": entries,
            "truncated": truncated,
        }
    )


async def poll_terminal_titles(app: web.Application) -> None:
    """Background task: push a per-session title (foreground command name while
    one runs, else the shell's cwd basename) and the shell's full cwd to each
    connected terminal, on change only. Fast commands that finish within the
    poll interval never flip the title, so there's no flicker at the prompt.

    The cadence is an upper bound, not a rate: a session is only probed when it
    has written to the PTY since its last probe, so a terminal idling at a
    prompt costs nothing at all."""
    try:
        while True:
            await asyncio.sleep(1.0)
            state = app.get("state")
            if not state or not hasattr(state, "_terminal_sessions"):
                continue
            registry: dict[str, _TerminalSession] = state._terminal_sessions
            loop = asyncio.get_running_loop()
            for sess in list(registry.values()):
                if sess is None or sess.ws is None or sess.ws.closed:
                    continue
                if not sess.frames_dirty:
                    continue
                # Cleared BEFORE probing, never after: output landing while a
                # probe is in flight must re-dirty the session so the next tick
                # picks up the change instead of the flag swallowing it.
                sess.frames_dirty = False
                if sess.remote:
                    # A workspace shell runs on the remote machine: the probes
                    # below would read the local provider CLI's process, whose
                    # cwd and title are this gateway's, not the workspace's. The
                    # tab keeps the title the client gave it.
                    continue
                # _session_title / _session_cwd do blocking syscalls (tcgetpgrp
                # ioctl, /proc reads, lsof where nothing cheaper answers) that
                # can wedge on a D-state process or a stuck fs; run them off the
                # loop on the subprocess pool (same rationale as the os.close
                # offload in _kill_session) so one stuck read can never freeze
                # the gateway event loop. Both probes share one cwd lookup via
                # the session's memo.
                # The WS can detach (sess.ws = None) or be displaced by a
                # reconnect while an executor probe is in flight — capture +
                # revalidate the socket after EACH hop, and let the bounded
                # owner-checked sender re-confirm identity under the transport
                # lock so a displaced socket never receives a frame and a
                # blocked transport can never park the singleton poller.
                title = await loop.run_in_executor(subprocess_executor(), _session_title, sess)
                ws = sess.ws
                if ws is None or ws.closed:
                    continue
                if title and title != sess.last_title:
                    # Advance the dedup marker only after the frame was handed
                    # to the owner's transport: a timed-out or failed send must
                    # not suppress the retry, or chat handoff would label output
                    # with a stale title/cwd until the value changes again.
                    if await _send_owner_control_frame(
                        sess, ws, {"type": "title", "text": title}
                    ):
                        sess.last_title = title
                # Live cwd (full path) rides the same poll: the frontend uses it
                # to attribute terminal output handed off to chat. Pushed only
                # on change, like the title.
                cwd = await loop.run_in_executor(subprocess_executor(), _session_cwd, sess)
                ws = sess.ws
                if ws is None or ws.closed:
                    continue
                if cwd and cwd != sess.last_cwd:
                    if await _send_owner_control_frame(
                        sess, ws, {"type": "cwd", "path": cwd}
                    ):
                        sess.last_cwd = cwd
    except asyncio.CancelledError:
        pass


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.terminal_session",
    "kiro_crew.dashboard.handlers.terminal_shell",
    "kiro_crew.dashboard.handlers.terminal_transport",
    "kiro_crew.dashboard.handlers.terminal_lifecycle",
    "kiro_crew.dashboard.handlers.terminal_placement",
    "kiro_crew.dashboard.handlers.terminal_complete",
    "kiro_crew.dashboard.handlers.terminal_routes",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.terminal_complete import (  # noqa: F401
        _COMPLETE_MAX_SCAN,
        _UNSAFE_NAME_RE,
        _entry_is_sensitive,
        _entry_sort_key,
        _list_completions,
        _list_vetted_completions,
        _open_vetted_dir,
        _resolve_completion_dir,
        _vetted_completion_dir,
    )
    from kiro_crew.dashboard.handlers.terminal_lifecycle import (  # noqa: F401
        _CHILD_REAP_TIMEOUT_S,
        _CONPTY_STATUS_POLL_S,
        _ORPHAN_TIMEOUT_S,
        _TERMINAL_FD_CLOSE_TIMEOUT_S,
        _child_exit_status,
        _notify_shell_failed,
        _shell_failure_note,
        reap_orphaned_terminals,
    )
    from kiro_crew.dashboard.handlers.terminal_placement import (  # noqa: F401
        _WRONG_MACHINE,
    )
    from kiro_crew.dashboard.handlers.terminal_routes import (  # noqa: F401
        api_terminal_create,
        api_terminal_delete,
        api_terminal_list,
    )
    from kiro_crew.dashboard.handlers.terminal_session import (  # noqa: F401
        _ConptyBackend,
        _enabled_cache,
    )
    from kiro_crew.dashboard.handlers.terminal_transport import (  # noqa: F401
        _OWNER_CONTROL_SEND_TIMEOUT_S,
        _SCROLLBACK_MAX,
        _STALE_OWNER_ERROR_CODE,
        _STALE_OWNER_ERROR_MESSAGE,
        _TAKEOVER_CATCH_UP_ROUNDS,
        _TAKEOVER_READY_TIMEOUT_S,
        _TAKEOVER_REPLAY_SEND_TIMEOUT_S,
        _TERMINAL_WS_CLEANUP_TIMEOUT_S,
        _consume_ready_marker,
        _write_all,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew.config.loader import config_path, read_config_text  # noqa: F401
    from kiro_crew.hooks import validate_file_path  # noqa: F401
    from kiro_crew.notifications.bus import NotificationPayload  # noqa: F401
    from kiro_crew.sandbox import without_python_env  # noqa: F401
