"""Windows ConPTY pseudo-terminal backend for the dashboard web terminal.

POSIX drives the terminal with ``pty``/``fork`` (see
``dashboard/handlers/terminal.py``). Windows has no such primitives; Windows 10
1809+ ships a real pseudo-console (ConPTY). Rather than hand-roll the fiddly
Win32 ``CreatePseudoConsole`` + ``STARTUPINFOEX`` dance via ctypes (which is
notoriously easy to get subtly wrong — the child silently fails to attach to
the pseudo-console), this wraps **pywinpty**, the maintained ConPTY binding.

pywinpty is a Windows-only dependency (declared under a ``platform_system ==
"Windows"`` marker in ``setup.cfg``, alongside ``tzdata``); it is never imported
or installed on macOS/Linux, so POSIX behavior is untouched.

``WindowsPty`` exposes exactly what the terminal handler needs from a PTY:
``read``/``write`` (blocking; the handler runs them in an executor), ``resize``,
``isalive``, ``terminate``, and ``pid`` — with byte-oriented read/write so the
handler's redaction/streaming code is backend-agnostic.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class WindowsPty:
    """A ConPTY-backed child process (PowerShell/cmd) via pywinpty."""

    def __init__(
        self,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        # Imported lazily so a POSIX import of this module (e.g. test collection)
        # never requires the Windows-only package.
        from winpty import PtyProcess  # type: ignore[import-not-found]

        self._p = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))

    @property
    def pid(self) -> int:
        return int(getattr(self._p, "pid", 0) or 0)

    def read(self, size: int = 4096) -> bytes:
        """Blocking read of up to *size* bytes; ``b""`` on EOF.

        pywinpty returns ``str`` and raises ``EOFError`` at end-of-stream; the
        handler works in bytes and treats ``b""`` as EOF, so translate here.
        """
        try:
            data = self._p.read(size)
        except EOFError:
            return b""
        except Exception:
            return b""
        if not data:
            return b""
        if isinstance(data, bytes):
            return data
        return data.encode("utf-8", errors="replace")

    def write(self, data: bytes) -> int:
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
        else:
            text = data
        self._p.write(text)
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        try:
            self._p.setwinsize(rows, cols)
        except Exception:
            logger.debug("ConPTY resize failed", exc_info=True)

    def isalive(self) -> bool:
        try:
            return bool(self._p.isalive())
        except Exception:
            return False

    def terminate(self, force: bool = True) -> None:
        try:
            self._p.terminate(force=force)
        except Exception:
            logger.debug("ConPTY terminate failed", exc_info=True)
