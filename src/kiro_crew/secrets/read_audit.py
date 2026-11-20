"""One audit row per vault secret read in the gateway, opt-in.

When ``dashboard.audit_secret_reads`` is on, every :class:`SecretVault` read
that returns a stored secret writes an ``api_access`` row to the security event
log through :meth:`SecurityEventLog.log_api_access`, the writer the mediated
secret routes already use. The row names the secret, the module that read it
(``caller_identity``) and the function that asked (``purpose=`` in
``resources``). It never carries the value or any part of it: only the names
reach this module.

Reads repeat. The sign-in store reads its token on every check and a spawn
resolves each ``secret://`` reference, so one row per call would grow the log
with every turn. A ``(secret, caller, purpose)`` triple therefore writes at most
one row per :data:`WINDOW_SECS`; the triple's next row carries ``repeats=N``,
the reads its window absorbed. At most :data:`MAX_TRACKED` triples are
remembered; past that the oldest is forgotten, and one with absorbed reads
writes its count as a row first. A triple is remembered by the SHA-256 digest
of the secret's name, so the memory a triple holds does not grow with the name
and two different names never share a triple. A row shows a name longer than
:data:`MAX_NAME_CHARS` clipped, with a CRC-32 marker of the whole name. Reads absorbed after a triple's last row are
written with its next read, so a gateway that stops before that read leaves
them unwritten.

The caller is the first frame outside this package and the standard library's
thread-pool and task plumbing. A read handed to a worker thread as a bare bound
method (``asyncio.to_thread(vault.get, name)``) has no such frame on that
thread, so its row names ``unknown``; a call site that wants to be named hands
over a function of its own module instead.

The switch is read from the gateway's live config snapshot (a plain attribute
read). A process with no config watcher, such as the MCP gateway daemon that
resolves an MCP server's ``secret://`` references, loads ``config.json``
instead and reuses that answer for :data:`CONFIG_TTL_SECS`. The log writer only enqueues to its own thread (the file append happens there,
never on the caller's thread or the event loop), and every fault here is
swallowed: an audit that cannot be written never fails the read.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import threading
import time
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import FrameType
from typing import Any

logger = logging.getLogger(__name__)

#: Seconds one ``(secret, caller, purpose)`` triple stays quiet after a row.
WINDOW_SECS = 600
#: Triples remembered at once; the oldest is forgotten first.
MAX_TRACKED = 1024
#: Characters of a secret's name a row carries and a triple keeps.
MAX_NAME_CHARS = 128
#: The ``operation`` every row carries.
OPERATION = "secret_read"
#: Seconds a process with no config watcher reuses its disk read of the switch.
CONFIG_TTL_SECS = 30

#: Packages whose frames never name the caller: this one, and the standard
#: library's plumbing that runs a call on a worker thread or in a task.
_PLUMBING = (
    "kiro_crew.secrets",
    "asyncio",
    "concurrent.futures",
    "contextvars",
    "functools",
    "threading",
)


@dataclass(slots=True)
class _Window:
    """One triple's state: when its last row was written, reads since, its shown name."""

    last: float
    absorbed: int
    shown: str


_lock = threading.Lock()
#: (SHA-256 of the name, caller, purpose) -> its window
_seen: dict[tuple[bytes, str, str], _Window] = {}
_clock: Callable[[], float] = time.monotonic
#: (when, answer) of the last disk read of the switch, for a process with no watcher.
_loaded: tuple[float, bool] | None = None
#: Set on a thread while it loads config here, so a vault read that load makes is skipped.
_loading = threading.local()


def _switch(cfg: Any) -> bool:
    dashboard = getattr(cfg, "dashboard", None)
    return getattr(dashboard, "audit_secret_reads", False) is True


def _enabled() -> bool:
    """The switch, from the live snapshot, else from ``config.json``.

    With no snapshot the file is loaded at most once per :data:`CONFIG_TTL_SECS`
    and the answer reused, so turning the switch on or off reaches such a process
    within that many seconds. A load that fails reads as off and is reused too,
    so a broken file is not reloaded on every read.
    """
    global _loaded
    from kiro_crew.config import KiroCrewConfig, live

    cfg = live.snapshot()
    if cfg is not None:
        return _switch(cfg)
    if getattr(_loading, "active", False):
        return False
    now = _clock()
    with _lock:
        cached = _loaded
    if cached is not None and 0 <= now - cached[0] < CONFIG_TTL_SECS:
        return cached[1]
    _loading.active = True
    try:
        answer = _switch(KiroCrewConfig.load())
    except Exception:  # noqa: BLE001 - an unreadable config reads as off
        logger.debug("secret read audit switch unreadable", exc_info=True)
        answer = False
    finally:
        _loading.active = False
    with _lock:
        _loaded = (now, answer)
    return answer


def _sel() -> Any:
    from kiro_crew.sel import sel

    return sel()


def _plumbing(module: str) -> bool:
    for package in _PLUMBING:
        if module == package or module.startswith(package + "."):
            return True
    return False


def _caller() -> tuple[str, str]:
    """The first frame that is not plumbing (:data:`_PLUMBING`): its module and function."""
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if not _plumbing(module):
            code = frame.f_code
            return module or "unknown", getattr(code, "co_qualname", code.co_name)
        frame = frame.f_back
    return "unknown", "unknown"


def _shown(name: str, marker: str) -> str:
    """*name* as a row carries it: clipped past :data:`MAX_NAME_CHARS`, with its checksum."""
    if len(name) <= MAX_NAME_CHARS:
        return name
    return f"{name[:MAX_NAME_CHARS]}...#{marker}"


_Row = tuple[str, str, str, int]


def _due(name: str, caller: str, purpose: str, now: float) -> list[_Row]:
    """The rows to write for this read: ``(shown name, caller, purpose, repeats)``.

    Empty when the read is absorbed. A triple forgotten to keep the bound writes
    its absorbed reads as a row of its own first, so eviction never loses a count.
    """
    encoded = name.encode("utf-8", "surrogatepass")
    key = (hashlib.sha256(encoded).digest(), caller, purpose)
    with _lock:
        state = _seen.get(key)
        if state is not None and now - state.last < WINDOW_SECS:
            state.absorbed += 1
            return []
        if state is not None:
            shown = state.shown
        else:
            shown = _shown(name, format(zlib.crc32(encoded), "08x"))
        rows = [(shown, caller, purpose, state.absorbed if state is not None else 0)]
        _seen.pop(key, None)
        while len(_seen) >= MAX_TRACKED:
            oldest = next(iter(_seen))
            gone = _seen.pop(oldest)
            if gone.absorbed:
                *_, gone_caller, gone_purpose = oldest
                rows.insert(0, (gone.shown, gone_caller, gone_purpose, gone.absorbed))
        _seen[key] = _Window(now, 0, shown)
        return rows


def note_reads(names: Iterable[str]) -> None:
    """Audit a read of each stored secret in *names*. Never raises."""
    try:
        if not _enabled():
            return
        caller, purpose = _caller()
        now = _clock()
        for name in names:
            for secret, module, function, repeats in _due(name, caller, purpose, now):
                resources = f"secret={secret} purpose={function}"
                if repeats:
                    resources += f" repeats={repeats}"
                _sel().log_api_access(
                    caller=module,
                    operation=OPERATION,
                    outcome="ok",
                    source="gateway",
                    resources=resources,
                )
    except Exception:  # noqa: BLE001 - an audit fault never fails the read
        logger.debug("secret read audit skipped", exc_info=True)
