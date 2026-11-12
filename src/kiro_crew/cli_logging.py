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
import time
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

from kiro_crew.cli_log_files import (
    BOOT_LOG_GENERATIONS,
    BOOT_LOG_GENERATIONS_MAX_BYTES,
    _AppendOnlyLogFileHandler,
    _OwnerOnlyRotatingFileHandler,
    boot_log_dir,
    boot_log_dir_is_linked,
    older_boot_logs,
)
from kiro_crew.config import KiroCrewConfig, config_dir
from kiro_crew.log_redaction import install_log_redaction


def _rotate_boot_logs(log_file: Path, live_size: int) -> tuple[OSError | None, list[OSError]]:
    """Move *log_file* to ``gateway.log.prev`` at boot, keeping older boots behind it.

    Called only by ``_start_gateway_log_after_lock``, so only by the process that
    holds the gateway lock: no other gateway is running, and a start the lock
    refuses never reaches here, so it never moves or deletes a running gateway's
    log. Two renames, each into a name nothing else holds, and no step ever
    undone:

    1. The old ``gateway.log.prev`` moves to ``logs/gateway-boots/gateway.log.<n>``,
       *n* one more than the largest kept (``cli_log_files.older_boot_logs``), so
       the name is free and order never depends on the clock. If it cannot move
       (a Windows reader holds it), stop: every file stays where it is and boot
       appends to the live log.
    2. ``gateway.log`` moves to the now free ``gateway.log.prev``. If it cannot
       move, stop the same way: the old ``.prev`` is already kept, so nothing is
       lost, and a ``gateway.log`` another process recreated meanwhile is never
       written over.

    Only after both succeed are older boots past ``BOOT_LOG_GENERATIONS``, or past
    ``BOOT_LOG_GENERATIONS_MAX_BYTES`` in total counted newest first from
    *live_size* (the size of the new ``.prev``, read by the caller), deleted.
    ``gateway.log.prev`` is never deleted, so a failed boot never costs a kept log.
    Boot lists only ``logs/gateway-boots``, which holds at most a few files.

    Returns the error that stopped the rotation (the caller keeps appending to
    the live log) and every prune error.
    """
    prev = log_file.with_name(log_file.name + ".prev")
    if boot_log_dir_is_linked(log_file):
        # Never follow a link off the data home: keep only one previous boot and say why.
        try:
            log_file.replace(prev)
        except OSError as exc:
            return exc, []
        return None, [OSError(f"{boot_log_dir(log_file)} is a link; older boots not kept")]
    try:
        kept = older_boot_logs(log_file, strict=True)
    except OSError as exc:
        # The archive cannot be listed: move nothing rather than guess a number.
        return exc, []
    if os.path.lexists(prev):
        archive_dir = boot_log_dir(log_file)
        archived = archive_dir / f"{log_file.name}.{kept[0][0] + 1 if kept else 1}"
        from kiro_crew.owner_only_files import mkdirs_owner_only

        try:
            mkdirs_owner_only(archive_dir)
            prev.rename(archived)
        except OSError as exc:
            return exc, []
        kept.insert(0, (0, archived))
    try:
        log_file.rename(prev)
    except OSError as exc:
        return exc, []
    return None, _prune_older_boot_logs([path for _, path in kept], live_size)


def _prune_older_boot_logs(kept: list[Path], prev_size: int) -> list[OSError]:
    """Delete older boots (*kept*, newest first) past the count or the byte budget.

    *kept* lists only files in ``logs/gateway-boots``, never ``gateway.log`` or
    ``gateway.log.prev``, and this runs only under the gateway lock, so it never
    deletes a log a running gateway still writes.
    """
    errors: list[OSError] = []
    total = prev_size
    for index, path in enumerate(kept):
        if index < BOOT_LOG_GENERATIONS:
            try:
                total += path.lstat().st_size
            except OSError as exc:
                errors.append(exc)
                continue
            if total <= BOOT_LOG_GENERATIONS_MAX_BYTES:
                continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(exc)
    return errors


#: Size rollover of the live ``gateway.log`` (three ``.1`` .. ``.3`` backups).
_GATEWAY_LOG_MAX_BYTES = 2 * 1024 * 1024
#: The gateway's file handler, set by ``_setup_cli_logging("gateway")`` until
#: ``_start_gateway_log_after_lock`` has run once.
_GATEWAY_LOG_HANDLER: RotatingFileHandler | None = None
#: Whether that gateway's fds 1/2 point at ``gateway.log`` (a detached start).
_GATEWAY_LOG_DETACHED = False


def _start_gateway_log_after_lock() -> None:
    """Rotate ``gateway.log`` and turn on its size rollover, once the gateway lock is held.

    Call it right after ``GatewayLock.acquire`` succeeds. Until then the gateway
    appends to the live log and never renames it, so a start the lock refuses
    leaves a running gateway's log, and every kept boot, as they were.

    The records this start wrote before the lock stay at the end of the file that
    becomes ``gateway.log.prev``; the restart relay
    (``cli_server_restart._gateway_log_lines_since``) already reads them there.

    The file handler's lock is held across the rotation, so its listener thread
    writes nothing meanwhile, and its stream is closed first: Windows cannot rename
    a file this process holds open. The next record reopens a fresh ``gateway.log``.
    A failed rotation is logged and the gateway keeps appending to the live log.
    """
    global _GATEWAY_LOG_HANDLER
    fh = _GATEWAY_LOG_HANDLER
    if fh is None:
        return
    _GATEWAY_LOG_HANDLER = None
    log_file = Path(fh.baseFilename)
    rotation_error: OSError | None = None
    shift_errors: list[OSError] = []
    rotated = False
    fh.acquire()
    try:
        stream = fh.stream
        try:
            if stream is not None:
                stream.flush()
            live_size = log_file.stat().st_size
        except FileNotFoundError:
            live_size = 0
        except OSError as exc:
            live_size, rotation_error = 0, exc
        if live_size > 0:
            if stream is not None:
                fh.stream = None  # type: ignore[assignment]
                try:
                    stream.close()
                except OSError:
                    pass  # flushed above; the next record opens a fresh file
            rotation_error, shift_errors = _rotate_boot_logs(log_file, live_size)
            rotated = rotation_error is None
        fh.maxBytes = _GATEWAY_LOG_MAX_BYTES
    finally:
        fh.release()
    if rotated and _GATEWAY_LOG_DETACHED:
        _redirect_fds_to(log_file)
    for shift_error in shift_errors:
        logging.getLogger("kiro_crew").warning(
            "could not keep an older boot's log at boot (%s)", shift_error
        )
    if rotation_error is not None:
        logging.getLogger("kiro_crew").warning(
            "could not move %s aside at boot (%s); appending to the previous run's log",
            log_file,
            rotation_error,
        )


def _fd_targets_file(fd: int, path: Path) -> bool:
    """True when *fd* is open on the same inode *path* names.

    Detects a detach-spawned gateway: ``_spawn_detached_gateway``
    redirects the child's stdout/stderr INTO ``gateway.log``, and from inside
    the child that redirect is only visible by comparing device/inode numbers.
    Any failure (fd closed, file missing, platform without usable inode
    numbers) answers ``False``, so callers keep the foreground behavior.
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

    After a rename of ``gateway.log``, a detach-spawned gateway's inherited
    stdout/stderr still reference the renamed inode, so raw writes (uncaught
    tracebacks, child-process stderr) would land in the rotated file.
    ``sys.stdout``/``sys.stderr`` keep working because they address the fd
    *number*. Best-effort: on failure the fds keep pointing where they were.
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

    In detached mode raw stdout/stderr point at ``gateway.log``. A rollover
    renames that file, so without the re-point the raw fds would follow the old
    inode through ``.1`` .. ``.3`` to unlink and raw stderr would be lost.

    If the reopen inside a rollover fails, the fds stay on the renamed file and
    a traceback per record would escape the size cap. So an OSError streak prints
    only its first traceback, and while no file is open a non-retryable errno (or
    a long streak) stops file logging: emit becomes a no-op and fds 1/2 move to
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
# (``logs -f`` -> tail/journalctl, ``pod exec``),
# which skips atexit and would strand queued records -- and none of them runs
# an event loop, so the queue buys them nothing.
_LONG_LIVED_COMMANDS = {"serve", "gateway", "chat", None}

_LOG_QUEUE_LISTENER: QueueListener | None = None

# The servers the loop-stall watchdog guards. A foreground one echoes to a
# terminal or pipe, and a write there blocks for as long as the reader stops
# reading (output paused with Ctrl-S, a stalled terminal app, an undrained
# pipe). So their console echo also runs on a listener thread, behind a
# bounded queue that drops records once full: ``gateway.log`` has every one.
# ``chat`` keeps its synchronous echo: its terminal is the conversation, so
# a log line must land in order with the chat's own output.
_CONSOLE_OFF_LOOP_COMMANDS = {"serve", "gateway"}
_CONSOLE_QUEUE_MAX = 10_000
_CONSOLE_DRAIN_TIMEOUT = 2.0

_CONSOLE_QUEUE_LISTENER: "_ConsoleQueueListener | None" = None


class _ConsoleQueueHandler(_CliLogQueueHandler):
    """Never blocks and never grows past its queue: a full queue drops the record."""

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            # Not handleError: its traceback goes to the very stderr that stopped reading.
            pass


class _ConsoleQueueListener(QueueListener):
    """The console echo's listener, with a stop that gives up at a deadline."""

    def __init__(
        self, bounded: "queue.Queue[logging.LogRecord | None]", *handlers: logging.Handler
    ) -> None:
        # respect_handler_level: a service's stderr file echoes WARNING and up only.
        super().__init__(bounded, *handlers, respect_handler_level=True)
        self.bounded = bounded

    def stop_within(self, deadline: float) -> None:
        end = time.monotonic() + deadline  # one budget for the put and the join
        try:
            self.bounded.put(None, timeout=deadline)  # None is QueueListener's stop sentinel
        except queue.Full:
            return  # the reader is stuck; nothing queued after this would land
        thread = self._thread
        if thread is not None:
            thread.join(max(0.0, end - time.monotonic()))
            if not thread.is_alive():
                self._thread = None  # what QueueListener.stop leaves behind


def _stop_console_queue_listener(timeout: float | None = None) -> None:
    """Drain the console echo for at most ``timeout`` seconds, never longer.

    The console may be the thing that stopped reading, so exit must not wait
    on it: the listener thread is a daemon and is left behind when the
    deadline passes.
    """
    global _CONSOLE_QUEUE_LISTENER
    listener, _CONSOLE_QUEUE_LISTENER = _CONSOLE_QUEUE_LISTENER, None
    if listener is None:
        return
    try:
        listener.stop_within(_CONSOLE_DRAIN_TIMEOUT if timeout is None else timeout)
    except Exception:
        return  # interpreter-teardown races must not raise


def _move_console_echo_off_loop() -> None:
    """Put the root logger's console handlers behind a bounded queue listener."""
    global _CONSOLE_QUEUE_LISTENER
    root = logging.getLogger()
    console = [h for h in root.handlers if type(h) is logging.StreamHandler]
    if not console:
        return
    for handler in console:
        root.removeHandler(handler)
    bounded: "queue.Queue[logging.LogRecord | None]" = queue.Queue(maxsize=_CONSOLE_QUEUE_MAX)
    _CONSOLE_QUEUE_LISTENER = _ConsoleQueueListener(bounded, *console)
    _CONSOLE_QUEUE_LISTENER.start()
    root.addHandler(_ConsoleQueueHandler(bounded))


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

    The ``gateway.log`` tail drains first; the console echo gets only what is
    left of ``timeout``, so a terminal that stopped reading never costs the
    file the records a post-mortem needs most.
    """
    started = time.monotonic()
    try:
        _stop_file_log_listener(timeout)
    finally:
        if timeout is None:
            _stop_console_queue_listener()
        else:
            _stop_console_queue_listener(max(0.0, timeout - (time.monotonic() - started)))


def _stop_file_log_listener(timeout: float | None) -> None:
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

    ``os._exit`` runs no ``atexit`` handler, so without this every queued
    record is lost, including the last ones before the exit, which a
    post-mortem needs most.

    :func:`_stop_log_queue_listener` joins the listener thread, so it runs on
    :func:`~kiro_crew.executors.subprocess_executor` (the pool for teardown work
    that can block on a wedged resource) under an outer deadline: a wedged disk
    delays neither the event loop nor the exit.

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


def _chat_console_filter(record: logging.LogRecord) -> bool:
    """Keep an internal ``kiro_crew`` record below ERROR off the ``kirocrew chat`` terminal.

    That record is already in ``gateway.log``. Still echoed: chat's own warnings,
    which explain a refusal its on-screen notice tells the user to fix; a
    ``SECURITY:`` warning, which must stay loud (SEC-009); and a third-party record,
    which reaches no file.
    """
    name = record.name
    if record.levelno >= logging.ERROR or name == "kiro_crew.cli_chat":
        return True
    internal = name == "kiro_crew" or name.startswith("kiro_crew.")
    return not internal or str(record.msg).startswith("SECURITY")


def _setup_cli_logging(command: str | None, verbose: int) -> None:
    """Configure console echo + persistent ``gateway.log`` logging.

    Foreground invocations get a ``basicConfig`` console handler on the root
    logger (stderr) plus a file handler on the ``kiro_crew`` logger.

    A detach-spawned gateway (``_spawn_detached_gateway``) arrives with
    stdout/stderr already redirected INTO ``gateway.log``, where a console
    handler would write a second copy of every record. Detected via
    device/inode comparison, and then:

    - the console echo is skipped entirely;
    - the file handler attaches to the ROOT logger instead, so third-party
      WARNINGs reach the file formatted and PID-stamped rather than only as
      raw stderr;
    - after the boot rotation (``_start_gateway_log_after_lock``), fds 1/2 are
      re-pointed at the live log so raw writes (uncaught tracebacks, child
      stderr) do not land in ``.prev``.

    In BOTH modes, for LONG-LIVED commands (``_LONG_LIVED_COMMANDS``) the file
    handler is not attached to a logger directly: the logger gets a
    ``QueueHandler`` and the file handler lives on a ``QueueListener`` thread,
    so no file I/O (rollover included) ever runs on the event-loop thread.
    Short-lived commands attach the file handler synchronously — they run no
    event loop, and several exec-over-self (skipping atexit), where a queued
    tail would be lost.

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

    # encoding="utf-8" is REQUIRED on Windows: the default there is cp1252, and
    # the first non-ASCII record would raise UnicodeEncodeError and drop the line.
    # ensure_utf8_console() fixes only the console streams, not this file handler.
    #
    # The gateway does NOT rotate here: this runs before the gateway lock, and a
    # start the lock refuses must leave a running gateway's log where it is. Until
    # ``_start_gateway_log_after_lock`` runs, the gateway appends to the live log
    # with size rollover off, so a refused start renames nothing.
    global _GATEWAY_LOG_HANDLER, _GATEWAY_LOG_DETACHED
    _GATEWAY_LOG_HANDLER = None
    _GATEWAY_LOG_DETACHED = detached
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
            # maxBytes 0 turns the size rollover off until the gateway lock is held.
            max_bytes = 0 if command == "gateway" else _GATEWAY_LOG_MAX_BYTES
            fh = rotating_cls(log_file, maxBytes=max_bytes, backupCount=3, encoding="utf-8")
            if command == "gateway":
                _GATEWAY_LOG_HANDLER = fh
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
    # No level on the handler: the loggers already gate every record, and a
    # runtime agent.log_level change moves only the logger level.
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

    # A LONG-LIVED command calls this from the thread that will run the event
    # loop. An inline emit (or a rollover's rename chain and fd re-point) could
    # block past the loop-stall watchdog's ``exit_after``, which hard-exits the
    # gateway mid-turn. So the queue handler makes emit a non-blocking put and
    # the listener thread does all file I/O. Secret redaction is unaffected: it
    # hooks the log-record FACTORY, upstream of any handler.
    if command in _LONG_LIVED_COMMANDS:
        global _LOG_QUEUE_LISTENER
        # Re-entrant call: basicConfig was a no-op (root still held the console
        # queue handler), so the console handlers come back from the old listener.
        prior_console = list(_CONSOLE_QUEUE_LISTENER.handlers) if _CONSOLE_QUEUE_LISTENER else []
        _stop_log_queue_listener()  # re-entrant call (tests): retire the old thread
        for lgr in (logging.getLogger(), logging.getLogger("kiro_crew")):
            for stale in [h for h in lgr.handlers if isinstance(h, _CliLogQueueHandler)]:
                lgr.removeHandler(stale)  # re-entrant call: orphaned producer
        for handler in prior_console:
            logging.getLogger().addHandler(handler)
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
        if command in _CONSOLE_OFF_LOOP_COMMANDS and not detached:
            _move_console_echo_off_loop()
    else:
        target_logger.addHandler(fh)

    # The chat terminal is the conversation: without -v it shows our errors only.
    # After the file handler, so a log file that cannot open keeps the full echo.
    if command == "chat" and verbose == 0:
        for handler in logging.getLogger().handlers:
            if type(handler) is logging.StreamHandler:
                handler.addFilter(_chat_console_filter)

    # Install secret redaction filter — scrubs Bearer tokens from all kiro_crew
    # log output before it reaches any handler. The filter also accepts literal
    # secret values to redact, but none are passed here: wiring resolved vault
    # secret values into the filter is a follow-up PR. Bearer token redaction is
    # active immediately with zero vault I/O.
    if command in _LONG_LIVED_COMMANDS:
        install_log_redaction([])
