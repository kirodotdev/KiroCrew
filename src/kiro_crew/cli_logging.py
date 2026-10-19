"""Console and ``gateway.log`` logging for every ``kirocrew`` process.

``_setup_cli_logging`` configures the console echo and the persistent ``gateway.log``
handler for the running command (rotating for the ``gateway``, append-only for every other
process, behind a queue listener for the long-lived ones); ``_stop_log_queue_listener`` and
``drain_log_queue_before_hard_exit`` drain that queue on the normal and the hard-exit paths.
``_LOG_QUEUE_LISTENER`` lives here with both of the functions that rebind it.

Split out of ``kiro_crew.cli``, which stays the import path and the patch target: every name
here is reached as ``kiro_crew.cli.<name>``, and a patch through that facade reaches this
module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
"""

from __future__ import annotations

import asyncio
import atexit
import errno
import logging
import os
import queue
import stat
import sys
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

from kiro_crew.cli_log_files import _AppendOnlyLogFileHandler, _OwnerOnlyRotatingFileHandler
from kiro_crew.config import KiroCrewConfig, config_dir
from kiro_crew.log_redaction import install_log_redaction


def _fd_targets_file(fd: int, path: Path) -> bool:
    """True when *fd* is open on the same inode *path* names.

    Detects a detach-spawned gateway: ``_spawn_detached_gateway``
    redirects the child's stdout/stderr INTO ``gateway.log``, and from inside
    the child that redirect is only visible by comparing device/inode numbers.
    Any failure (fd closed, file missing, platform without usable inode
    numbers) answers ``False`` — callers then keep the foreground behavior,
    which is what a false negative degrades to today.
    """
    try:
        st_fd = os.fstat(fd)
        st_path = path.stat()
    except OSError:
        return False
    return (st_fd.st_dev, st_fd.st_ino) == (st_path.st_dev, st_path.st_ino)


def _fd_is_regular_file(fd: int) -> bool:
    """True when *fd* is open on a regular file (not a tty, pipe or socket).

    A gateway whose stderr is a plain file was started by something that
    keeps stderr on disk for it: a service manager's error file (launchd's
    ``StandardErrorPath``) or a launcher's log. Nothing rotates that file.
    Any failure answers ``False``.
    """
    try:
        return stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        return False


def _redirect_fds_to(path: Path, fds: tuple[int, ...] = (1, 2)) -> None:
    """Re-point raw *fds* at *path* (append mode) via ``dup2``.

    After the boot rotation renames ``gateway.log`` → ``gateway.log.prev``,
    a detach-spawned gateway's inherited stdout/stderr still reference the
    RENAMED inode, so raw writes that bypass the logging module (uncaught
    tracebacks, inherited child-process stderr) would land in the rotated
    file instead of the live one. ``dup2`` swaps the descriptors in place;
    ``sys.stdout``/``sys.stderr`` objects keep working because they address
    the fd *number*, not the open file description. Best-effort: on failure
    the fds keep pointing where they already were.
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except OSError:
        pass  # a broken std stream must not abort gateway boot
    try:
        # 0o600: this is gateway.log, which is owner-only like the rest of the data
        # home (kiro_crew.owner_only_files). Only applies when this creates it.
        raw_fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return
    try:
        for fd in fds:
            try:
                os.dup2(raw_fd, fd)
            except OSError:
                pass  # leave this fd as-is; the others may still succeed
    finally:
        os.close(raw_fd)


# errnos that will not clear by retrying: the log file cannot be reopened.
_LOG_FATAL_ERRNOS = frozenset({errno.ENOSYS, errno.EPERM, errno.EACCES, errno.EROFS})
# Consecutive OSErrors (any errno) after which file logging stops anyway.
_LOG_ERROR_STREAK_LIMIT = 3


class _FdTrackingRotatingFileHandler(_OwnerOnlyRotatingFileHandler):
    """RotatingFileHandler that re-points raw fds 1/2 after each rollover.

    In detached mode ``_redirect_fds_to`` aims the process's raw
    stdout/stderr at ``gateway.log`` so writes that bypass the logging module
    (uncaught tracebacks, child-process stderr) stay captured. But a
    size-based rollover RENAMES that file: the raw fds keep following the
    renamed inode through ``.1`` → ``.2`` → ``.3`` → unlink, after which raw
    stderr disappears from every retained log. Re-pointing the fds at the
    freshly created ``gateway.log`` inside ``doRollover`` keeps raw-write
    capture continuous across the file's whole retention lifecycle.

    If the reopen inside a rollover fails, the fds stay on the RENAMED file
    and the stdlib ``handleError`` would print a traceback there for every
    later record, escaping the size cap. So an OSError streak prints only its
    first traceback, and while no file is open a non-retryable errno (or a
    long streak) stops file logging: emit becomes a no-op and fds 1/2 move to
    ``os.devnull``.
    """

    _error_streak = 0
    _stopped = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._stopped:
            return
        streak = self._error_streak
        super().emit(record)
        if self._error_streak == streak:
            self._error_streak = 0  # this record was written: streak over

    def handleError(self, record: logging.LogRecord) -> None:
        exc = sys.exc_info()[1]
        if not isinstance(exc, OSError):
            super().handleError(record)
            return
        self._error_streak += 1
        if self._error_streak == 1:
            super().handleError(record)  # one traceback per streak, not per record
        # Stop only when no file is open (a rollover could not reopen it): a
        # write error on the live, still-rotated file may clear by itself.
        if self.stream is None and (
            exc.errno in _LOG_FATAL_ERRNOS or self._error_streak >= _LOG_ERROR_STREAK_LIMIT
        ):
            self._stop_file_logging(exc)

    def _stop_file_logging(self, exc: OSError) -> None:
        self._stopped = True
        line = f"file logging to {self.baseFilename} stopped ({exc!r}); records are dropped\n"
        try:
            sys.stderr.write(line)  # same stream the stdlib traceback went to
            sys.stderr.flush()
        except (AttributeError, OSError, ValueError):  # None, broken or closed
            pass  # nowhere left to report to; the devnull re-point still matters
        _redirect_fds_to(Path(os.devnull))

    def doRollover(self) -> None:
        super().doRollover()
        _redirect_fds_to(Path(self.baseFilename))


class _CliLogQueueHandler(QueueHandler):
    """Marker subclass so re-entrant setup (and tests) can find the CLI's own
    queue handler among whatever other handlers the process accumulated."""


# Commands that run an asyncio event loop for hours -- the ones the loop-stall
# watchdog guards. Only these need file-handler I/O moved off the calling
# thread, and none of them ever exec-over-self, so the atexit / bounded
# force-exit drains cover every exit path they have. Short-lived CLI verbs
# keep synchronous handlers: several replace the process via ``os.exec*``
# (``logs -f`` -> tail/journalctl, ``config edit`` -> $EDITOR, ``pod exec``),
# which skips atexit and would strand queued records -- and none of them runs
# an event loop, so the queue buys them nothing.
_LONG_LIVED_COMMANDS = {"serve", "gateway", "chat", None}

_LOG_QUEUE_LISTENER: QueueListener | None = None


def _stop_log_queue_listener(timeout: float | None = None) -> None:
    """Stop the log queue listener, draining every queued record to disk.

    Idempotent — registered via ``atexit`` so process shutdown flushes the
    queued tail. Also the deterministic drain point for tests: after this
    returns, every record logged before the call has been written by the
    file handler.

    ``timeout`` bounds the drain for force-exit paths (a second SIGINT/
    SIGTERM hard-exits via ``os._exit``, which skips atexit): the sentinel
    is enqueued and the listener thread joined for at most that long, so a
    wedged disk cannot hang the force exit. When the thread does not finish
    in time, flush/close are skipped — they would block on the same wedged
    handler.
    """
    global _LOG_QUEUE_LISTENER
    listener, _LOG_QUEUE_LISTENER = _LOG_QUEUE_LISTENER, None
    if listener is None:
        return
    try:
        if timeout is None:
            listener.stop()
        else:
            listener.enqueue_sentinel()
            # QueueListener has no bounded stop; join its (private but stable
            # across 3.10-3.14) thread handle against the deadline.
            thread = getattr(listener, "_thread", None)
            if thread is not None:
                thread.join(timeout)
                if thread.is_alive():
                    return  # wedged handler — do not block the force exit
    except Exception:
        return  # interpreter-teardown races must not raise
    for handler in listener.handlers:
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass  # a broken handler must not block shutdown


async def drain_log_queue_before_hard_exit(timeout: float = 2.0) -> None:
    """Flush the queued ``gateway.log`` tail from an ``async`` hard-exit path.

    ``os._exit`` runs no ``atexit`` handler, so the drain registered by
    :func:`_setup_cli_logging` never fires on a path that hard-exits -- every
    record still sitting in the queue is lost, including the ones logged
    immediately before the exit, which are the most diagnostic ones a
    post-mortem has.

    :func:`_stop_log_queue_listener` is a *synchronous* bounded drain (it
    joins the listener thread), so calling it inline from a coroutine would
    park the event loop for up to ``timeout``. Offload it to
    :func:`~kiro_crew.executors.subprocess_executor` -- the pool reserved for
    teardown work that can block on a wedged resource -- under an outer
    deadline, exactly as the restart path already does for the SEL flush. A
    wedged disk therefore delays neither the loop nor the exit.

    Never raises: a hard exit must not be blocked, or replaced, by logging.
    """
    from kiro_crew.executors import subprocess_executor

    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), _stop_log_queue_listener, timeout
            ),
            timeout=timeout + 1.0,
        )
    except Exception:
        pass  # includes TimeoutError: reaching the exit is what matters


def _setup_cli_logging(command: str | None, verbose: int) -> None:
    """Configure console echo + persistent ``gateway.log`` logging.

    Foreground invocations get the classic shape: ``basicConfig`` console
    handler on the root logger (stderr) plus a rotating file handler on the
    ``kiro_crew`` logger.

    A detach-spawned gateway (``_spawn_detached_gateway``) arrives with
    stdout/stderr already redirected INTO ``gateway.log``. In that mode the
    console handler would write a second, console-formatted copy (no [PID])
    of every ``kiro_crew`` record into the SAME file the file handler writes
    to — records propagate to root — doubling log volume and halving the
    rotation window. Detected via device/inode comparison, and then:

    - the console echo is skipped entirely;
    - the file handler attaches to the ROOT logger instead, so third-party
      WARNINGs reach the file formatted and PID-stamped rather than only as
      raw stderr;
    - after the boot rotation, fds 1/2 are re-pointed at the live log so raw
      writes (uncaught tracebacks, child stderr) do not land in ``.prev``.

    In BOTH modes, for LONG-LIVED commands (``_LONG_LIVED_COMMANDS``) the file
    handler is not attached to a logger directly: the logger gets a
    ``QueueHandler`` and the file handler lives on a ``QueueListener`` thread,
    so no file I/O (rollover included) ever runs on the event-loop thread.
    Short-lived commands attach the file handler synchronously — they run no
    event loop, and several exec-over-self (skipping atexit), where a queued
    tail would be lost. See the inline comment at the attach site.

    In every shape the ``kiro_crew`` logger is the SINGLE level gate: the file
    handler and the queue handler carry no level of their own, so a runtime
    ``agent.log_level`` change (``handlers/updates_streams.py::apply_log_level``) that
    moves the logger reaches ``gateway.log`` with nothing else to update.

    Only the ``gateway`` command rotates ``gateway.log``. Every other process
    writes through ``_AppendOnlyLogFileHandler``, which never rotates and holds
    no handle between records, so it cannot block the gateway's rename on Windows.
    """
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose >= 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    log_home = config_dir()
    log_file = log_home / "gateway.log"
    # Detect BEFORE the boot rotation below: rotation renames the file, and
    # the inode comparison must see the file stderr actually inherited.
    detached = _fd_targets_file(2, log_file)

    if detached:
        # Console echo would double-write into gateway.log — skip it. Set the
        # root level to what basicConfig would have set so third-party
        # loggers gate identically in both modes.
        logging.getLogger().setLevel(logging.WARNING)
    else:
        logging.basicConfig(
            level=logging.WARNING,  # third-party libs stay quiet
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        if command == "gateway" and _fd_is_regular_file(2):
            # stderr is a file nothing rotates (launchd's gateway.err, the
            # Desktop launch log). Echo only warnings and errors there: those
            # are what a start-failure dialog or a `tail` of it needs, and
            # every record at the configured level is already in gateway.log.
            for handler in logging.getLogger().handlers:
                if type(handler) is logging.StreamHandler:
                    handler.setLevel(logging.WARNING)
    # Kiro Crew loggers: --verbose CLI flag takes precedence, otherwise
    # fall back to the persistent log_level from config.
    if verbose == 0:
        try:
            _cfg = KiroCrewConfig.load()
            _persisted = _cfg.agent.log_level.upper()
            level = getattr(logging, _persisted, logging.WARNING)
        except Exception:
            pass  # config missing or corrupt — keep default WARNING
    logging.getLogger("kiro_crew").setLevel(level)

    # Persistent file log — respects the configured log_level.
    # On startup, rotate gateway.log → gateway.log.prev so a crash's final
    # lines are never lost.  Only for `gateway` subcommand
    # to avoid renaming the file while the gateway is actively writing.
    # encoding="utf-8" is REQUIRED on Windows: Kiro Crew logs non-ASCII glyphs and
    # the default file encoding there is cp1252, so a RotatingFileHandler without
    # it raises UnicodeEncodeError on the first non-ASCII log record (logging
    # swallows it, but it spams "--- Logging error ---" tracebacks and drops the
    # line). ensure_utf8_console() only fixes the console streams, not this file
    # handler.
    boot_rotation_error: OSError | None = None
    if command == "gateway":
        prev_log = log_file.with_suffix(".log.prev")
        if log_file.exists() and log_file.stat().st_size > 0:
            try:
                log_file.replace(prev_log)
            except OSError as exc:
                # Keep going on the old file; reported once the handler exists.
                boot_rotation_error = exc
        if detached:
            _redirect_fds_to(log_file)
    # Detached mode uses the fd-tracking subclass: a size-based rollover
    # renames gateway.log, and without re-pointing, the redirected raw fds
    # would follow the renamed inode through .1 → .2 → .3 → unlink, losing
    # later raw stderr from all retained logs.
    #
    # Only the gateway rotates gateway.log. Every other process appends one
    # record at a time and holds no handle in between, because on Windows a
    # handle held by any other process makes the gateway's rename fail.
    rotates = detached or command == "gateway"
    if detached:
        rotating_cls: type[RotatingFileHandler] = _FdTrackingRotatingFileHandler
    else:
        rotating_cls = _OwnerOnlyRotatingFileHandler
    # Seatbelt/sandbox children (e.g. ``kirocrew mcp-core`` under a sandboxed
    # agent profile) inherit a deny on ``gateway.log``. For those, opening the
    # file handler must not abort the process: the console handler
    # ``basicConfig`` installed above still carries every record, so the
    # warning lands somewhere a human reads and the MCP handshake proceeds.
    #
    # A DETACHED process is the opposite case and must still fail loudly: no
    # console handler was installed (the branch above skips ``basicConfig`` to
    # avoid double-writing into the log stderr already points at), so
    # soft-failing here would boot a long-lived gateway with no persistent log
    # AND no destination for the warning saying so. Let the OSError propagate.
    try:
        fh: logging.FileHandler
        if rotates:
            fh = rotating_cls(log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        else:
            fh = _AppendOnlyLogFileHandler(log_file, encoding="utf-8")
    except OSError as exc:
        if detached:
            raise
        logging.getLogger("kiro_crew").warning(
            "persistent log file %s unavailable (%s); continuing with console logging only",
            log_file,
            exc,
        )
        # Install redaction BEFORE returning: long-lived commands still emit
        # Bearer/JWT-bearing records to the console, and the normal
        # install_log_redaction call below is skipped by this early return.
        if command in _LONG_LIVED_COMMANDS:
            install_log_redaction([])
        return
    # No level on the handler: every record that can reach it is already gated
    # by a logger level -- kiro_crew records by the kiro_crew logger set above,
    # third-party records (root attach, detached mode) by the root logger's
    # WARNING. The logger level is the one level a runtime agent.log_level
    # change moves, so keeping the handler at NOTSET is what lets a raised
    # level reach gateway.log without a restart.
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [PID %(process)d]: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    if detached:
        # Root attach: kiro_crew records arrive once via propagation, and
        # third-party WARNINGs land formatted rather than as an unformatted
        # stderr echo.
        target_logger = logging.getLogger()
    else:
        target_logger = logging.getLogger("kiro_crew")

    # Never run file-handler I/O on the caller's thread of a LONG-LIVED
    # command: the gateway invokes this from the thread that will run the
    # asyncio event loop, and an inline emit contends for Handler.lock with
    # every worker thread — while a size-based rollover runs the WHOLE rename
    # chain plus the dup2 fd re-point on the loop. Blocking past the
    # loop-stall watchdog's ``exit_after`` hard-exits the gateway mid-turn
    # and orphans every in-flight subagent. A QueueHandler makes emit a
    # non-blocking put; the QueueListener's thread owns the file handler, so
    # ALL file I/O — ``doRollover()`` and ``_redirect_fds_to()`` included —
    # happens off the loop. Secret redaction is unaffected: it hooks the
    # log-record FACTORY (creation time, producer thread), upstream of any
    # handler.
    #
    # Short-lived commands keep the synchronous handler on purpose: none of
    # them runs an event loop (nothing to stall), and several replace the
    # process via ``os.exec*`` (``logs -f``, ``config edit``, ``pod exec``),
    # which skips atexit — a queue there would strand its tail records, while
    # a sync handler has already written them by the time exec runs. See
    # ``_LONG_LIVED_COMMANDS``.
    if command in _LONG_LIVED_COMMANDS:
        global _LOG_QUEUE_LISTENER
        _stop_log_queue_listener()  # re-entrant call (tests): retire the old thread
        for lgr in (logging.getLogger(), logging.getLogger("kiro_crew")):
            for stale in [h for h in lgr.handlers if isinstance(h, _CliLogQueueHandler)]:
                lgr.removeHandler(stale)  # re-entrant call: orphaned producer
        log_queue: "queue.SimpleQueue[logging.LogRecord]" = queue.SimpleQueue()
        # No level on the queue handler either, and no handler-level re-check at
        # dequeue: the loggers decide what enters the queue, and everything that
        # enters is written. A level here would be a second copy of `level` that
        # the runtime applier does not move; a re-check at dequeue would judge
        # records already queued by a level set after they were logged.
        queue_handler = _CliLogQueueHandler(log_queue)
        _LOG_QUEUE_LISTENER = QueueListener(log_queue, fh)
        _LOG_QUEUE_LISTENER.start()
        atexit.register(_stop_log_queue_listener)
        target_logger.addHandler(queue_handler)
    else:
        target_logger.addHandler(fh)

    if boot_rotation_error is not None:
        logging.getLogger("kiro_crew").warning(
            "could not move %s aside at boot (%s); appending to the previous run's log",
            log_file,
            boot_rotation_error,
        )
    # Install secret redaction filter — scrubs Bearer tokens from all kiro_crew
    # log output before it reaches any handler. The filter also accepts literal
    # secret values to redact, but none are passed here: wiring resolved vault
    # secret values into the filter is a follow-up PR. Bearer token redaction is
    # active immediately with zero vault I/O.
    if command in _LONG_LIVED_COMMANDS:
        install_log_redaction([])
