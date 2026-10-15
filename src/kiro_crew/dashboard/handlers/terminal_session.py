"""One terminal session's server-side state, the registry that holds the sessions,
and the ``dashboard.terminal`` config every terminal route gates on.

An owner of :mod:`kiro_crew.dashboard.handlers.terminal`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from kiro_crew.config.loader import config_path, read_config_text

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.dashboard.state import DashboardState


# Global ceiling across ALL chats' terminal tabs. Each chat's activity bar caps
# its own terminals (frontend MAX_TERMINALS_PER_CHAT); this is the server-side
# backstop. Override via config.json dashboard.terminal.max_sessions.
_MAX_SESSIONS = 12


# Bound on a ``{session_id}`` URL path param, applied by BOTH routes that read
# one. Named rather than repeated because the two call sites drifted while the
# bound was a bare literal: WS-open enforced it and DELETE did not.
_MAX_SESSION_ID_LEN = 64


def _sel():
    import kiro_crew.dashboard.handlers as _pkg  # circular import: __init__ imports terminal

    return _pkg.sel()


class _ConptyBackend(Protocol):
    """Structural type for the Windows ConPTY backend (:class:`kiro_crew.conpty.WindowsPty`).

    Declared as a Protocol rather than importing WindowsPty so this module stays
    importable on POSIX, where ``conpty`` pulls in Windows-only bindings. Typing
    the field as bare ``object`` made every ``sess.winpty.<method>()`` call an
    ``attr-defined`` error and pushed callers toward scattered ``type: ignore``
    comments; the Protocol keeps the call sites checked instead.
    """

    @property
    def pid(self) -> int: ...

    def read(self, size: int = 4096) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def resize(self, cols: int, rows: int) -> None: ...

    def isalive(self) -> bool: ...

    def exitstatus(self) -> int | None: ...

    def terminate(self, force: bool = True) -> None: ...


@dataclass
class _TerminalSession:
    """Server-side state for one PTY session."""

    session_id: str
    master_fd: int  # wokeignore:rule=master
    proc: "asyncio.subprocess.Process | None" = None
    winpty: _ConptyBackend | None = None  # WindowsPty (ConPTY) backend on Windows
    cols: int = 80
    rows: int = 24
    created_at: float = field(default_factory=time.monotonic)
    last_ws_disconnect: float | None = None  # set when WS drops, cleared on reconnect
    ws: web.WebSocketResponse | None = None
    reader_task: asyncio.Task | None = None
    scrollback: bytearray = field(default_factory=bytearray)
    last_title: str | None = None  # last title pushed to the client (dedup)
    last_cwd: str | None = None  # last cwd pushed to the client (dedup)
    # Absolute path of the shell this PTY actually launched, as _resolve_shell
    # pinned it. Reported to the client in the `ready` frame: the client mints
    # session ids and opens the socket without ever asking what got spawned, so
    # a caller that needs to know which shell will interpret the bytes it is
    # about to write (Run-in-terminal, which honors a code fence's language)
    # has no other way to find out. Reporting is deliberately one-way -- the
    # client is never given a say in WHICH shell is spawned.
    shell: str = ""
    # name -> absolute path for the shells a code fence can name, as resolved on
    # this host. Reported alongside `shell` so a caller handing a snippet to a
    # different shell can name an absolute path instead of a bare name a
    # project-local PATH entry could hijack.
    fence_shells: dict[str, str] = field(default_factory=dict)
    # (monotonic_ts, cwd) memo for the path-completion route. The title poller's
    # ``last_cwd`` is up to a second stale, which is long enough for a user to
    # `cd` and immediately request completions against the OLD directory — so
    # completion probes the shell itself and memoizes here instead (see
    # _session_cwd_cached). Cleared as soon as the client submits a line, since
    # that line may be the `cd` the memo would otherwise hide.
    cwd_probe: tuple[float, str | None] | None = None
    # Whether the title/cwd poller still has anything to recompute. A shell
    # cannot change directory, or start or finish a command, without writing to
    # the PTY — so a session that has produced no output since the last probe
    # cannot have gone stale, and probing it again would spend a process
    # introspection call (a forked `lsof` where nothing cheaper answers) to
    # re-derive a label it already has. Set on PTY output, on a submitted line,
    # and on (re)connect, where the dedup markers are cleared and both frames
    # must be pushed again.
    frames_dirty: bool = True
    # A WebSocket upgrade completes before startup has yielded the terminal back
    # to a freshly spawned login shell. Run-in-terminal callers must not release
    # queued commands until this barrier has been crossed.
    shell_ready: bool = False
    # Bash inherits a PROMPT_COMMAND that emits this randomized OSC marker once,
    # after its login profiles return. ``ready_probe`` retains the small suffix
    # needed when the marker straddles two PTY reads; output itself is still
    # forwarded byte-for-byte.
    ready_marker: bytes | None = None
    ready_probe: bytearray = field(default_factory=bytearray)
    # Serializes concurrent WS writes (reader loop + title poller + pong);
    # aiohttp's WebSocket writer is not safe for concurrent sends.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The sole PTY reader races its transport child against this event. Takeover
    # signals it at publication, releasing the reader even when the displaced
    # socket is blocked on its old send lock or inside flow control.
    output_send_cancel: asyncio.Event | None = None
    # Counts PTY output bytes over the session lifetime. Reconnect uses it to
    # catch output produced while replay awaits the candidate socket.
    output_bytes: int = 0
    # Linearizes output recording and owner publication. Socket sends never hold
    # this lock except the bounded final ready frame, so a slow client cannot
    # normally stop the PTY reader from recording bytes for reconnect replay.
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes reconnect candidates without coupling takeover to the current
    # owner's socket writes or an in-flight PTY write.
    replace_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes client-to-PTY input and resize across handlers. A displaced
    # handler may keep draining its socket after reconnect, so each operation
    # must confirm ownership while holding this lock. It also keeps a frame
    # contiguous when ``_write_all`` needs several ``os.write`` calls.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _get_registry(request: web.Request) -> dict[str, _TerminalSession | None]:
    state: DashboardState = request.app["state"]
    return state._terminal_sessions


def _get_config(request: web.Request) -> dict:
    """The ``dashboard.terminal`` object, or ``{}`` for anything malformed.

    Every level is type-checked rather than chained, and the read fails CLOSED to
    the default. config.json is hand-edited, so a non-object at any level --
    ``"dashboard": false``, a number, a string, a list, or a document that is not
    an object at all -- would make a chained ``.get`` raise ``AttributeError``,
    which is NOT in the caught set below. That surfaced as an HTTP 500 on every
    terminal route, including the per-keystroke completion one, from a single typo.

    Returning the empty default (rather than propagating) is the convention the
    nested reads already follow: a malformed value means "nothing configured",
    which is also what an absent key means.
    """
    try:
        data = json.loads(read_config_text(config_path()))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    dashboard = data.get("dashboard")
    if not isinstance(dashboard, dict):
        return {}
    cfg = dashboard.get("terminal")
    return cfg if isinstance(cfg, dict) else {}


def _is_enabled(request: web.Request) -> bool:
    """Terminal panel is enabled by default. Disable via config.json:
    {"dashboard": {"terminal": {"enabled": false}}}
    Cached for 30s to avoid disk I/O per request.
    """
    now = time.monotonic()
    if now - _enabled_cache[1] < 30:
        return _enabled_cache[0]
    result = bool(_get_config(request).get("enabled", True))
    _enabled_cache[0] = result
    _enabled_cache[1] = now
    return result


_enabled_cache: list = [True, 0.0]  # [value, timestamp]


def _completion_cfg(request: web.Request) -> dict:
    """The ``dashboard.terminal.completion`` object, or ``{}``.

    Type-checked at BOTH levels because config.json is hand-edited: `"terminal":
    false` would make `.get("completion")` raise on a boolean and `"completion":
    false` would make the next `.get` raise — each an HTTP 500 from a typo on a
    per-keystroke route. A non-object at either level means "nothing configured",
    which is also the default.

    Deliberately NOT memoised in ``_enabled_cache``: that slot belongs to
    ``_is_enabled``, and a second flag sharing it would cross-contaminate the two.
    Callers pay the executor-offloaded read the command tier already paid.
    """
    cfg = _get_config(request)
    if not isinstance(cfg, dict):
        return {}
    inner = cfg.get("completion")
    return inner if isinstance(inner, dict) else {}


def _completion_disabled(completion_cfg: dict) -> bool:
    """True only for a literal ``enabled: false``.

    Any other value — a JSON string, a number, null, or an absent key — degrades
    to the default (enabled). ``bool("false") is True``, so coercing would turn a
    hand-edited string into the opposite of what it reads like.
    """
    return completion_cfg.get("enabled", True) is False
