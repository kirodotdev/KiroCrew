"""Owner-only log file handlers shared by the CLI and the slim ``mcp-*`` entry.

Lives outside :mod:`kiro_crew.cli` so :mod:`kiro_crew.mcp_entry` can attach the
same ``gateway.log`` handler without importing the whole CLI. ``cli`` re-exports
every name here, so existing references keep working.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from kiro_crew.owner_only_files import owner_only_opener


def _open_owner_only(handler: logging.FileHandler):  # type: ignore[no-untyped-def]
    """Open *handler*'s log file the way ``FileHandler._open`` does, but ``0600``.

    The stdlib opens the log with the builtin ``open``, so ``gateway.log`` -- and
    the fresh file every rollover creates -- would come out at the umask default
    (``0644``), readable by any local account that can reach the data home. The
    log carries prompts, tool calls and paths, so it follows the data home's
    owner-only policy. Only the creation mode changes; an existing file keeps its
    mode (the startup sweep tightens one an older version created).
    """
    # The stdlib's own handle on ``open`` (it survives interpreter teardown),
    # read through getattr because typeshed does not declare it.
    open_func = getattr(handler, "_builtin_open", open)
    return open_func(
        handler.baseFilename,
        handler.mode,
        encoding=handler.encoding,
        errors=handler.errors,
        opener=owner_only_opener,
    )


class _OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler whose log files are created ``0600`` (see
    :func:`_open_owner_only`)."""

    def _open(self):  # type: ignore[no-untyped-def]
        return _open_owner_only(self)


class _AppendOnlyLogFileHandler(logging.FileHandler):
    """Appends each record to ``gateway.log`` and closes the file again.

    Every process that is not the gateway (the ``mcp-*`` stdio servers,
    ``kirocrew chat``, short CLI verbs) writes through this handler. It never
    rotates and holds no open handle between records. Windows refuses to rename
    a file while any other process has it open, so a child that kept its own
    handle on ``gateway.log`` for its whole lifetime made every one of the
    gateway's size rollovers fail, and the gateway's file log then dropped
    every later record until restart. The gateway is the only rotator, and the
    only window in which a child can block a rename is the write of one record.

    The constructor opens the file once and closes it, so a log path the
    process may not write (a sandbox deny) still raises at setup, where
    ``_setup_cli_logging`` falls back to console-only logging.
    """

    def __init__(self, filename: Path, encoding: str = "utf-8") -> None:
        super().__init__(filename, mode="a", encoding=encoding)
        self._close_stream()

    def _open(self):  # type: ignore[no-untyped-def]
        return _open_owner_only(self)

    def _close_stream(self) -> None:
        stream, self.stream = self.stream, None  # type: ignore[assignment]
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass  # the record was already written or reported

    def emit(self, record: logging.LogRecord) -> None:
        # ``Handler.handle`` holds the handler lock around emit, so the
        # open/write/close below is atomic with respect to this handler.
        # ``FileHandler.emit`` opens the file outside its own error handling,
        # so a failed open is routed to ``handleError`` here instead of
        # raising into the code that logged.
        try:
            super().emit(record)
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)
        finally:
            self._close_stream()
