"""Kiro's Chromium's stderr: keep both ends of it, and quote the cause when it dies.

The supervisor launches Kiro's Chromium with its stderr on a pipe of the gateway's
own and drains it for the browser's whole life (:func:`keep_ends`), so a chatty
browser never blocks on a full pipe. Two bounded buffers are kept in memory: the
oldest bytes and the newest. When the browser exits before the start completes,
:func:`exit_detail` quotes its last lines, the way a missing shared library is
named by the loader's own report, and ahead of them the ``FATAL:`` (else
``ERROR:``) lines from the oldest bytes when the tail buffer had to drop them,
because Chromium writes its cause FIRST (``No usable sandbox!``) and then a
register and stack dump longer than the tail buffer. The newest bytes alone then
hold only registers.

Every kept line, both buffers whole, is judged by the stderr publication policy
(:func:`agent_sdk.published_stderr_tail`) before any is quoted, so a credential
anywhere in them, wrapped across lines or not, withholds the whole excerpt instead
of being published in pieces. POSIX only: the Windows launcher does not capture
stderr, and both buffers stay empty there.
"""

from __future__ import annotations

import asyncio
import os

#: How much of Kiro's Chromium's stderr is kept, newest bytes only, and how many of
#: its lines a start failure quotes: room for the dynamic loader's report of a
#: missing shared library (one line from ld.so, three from dyld).
STDERR_TAIL_BYTES = 4096
STDERR_TAIL_LINES = 3

#: How much of the START of that stderr is kept as well, and how many of its lines
#: a start failure quotes before the tail.
STDERR_HEAD_BYTES = 4096
STDERR_HEAD_LINES = 2
#: The severity tokens Chromium's log prefix carries (``[pid:tid:date:FATAL:file]``);
#: a head line is quoted only when it carries one, FATAL lines before ERROR lines,
#: since every headless start writes a few ``ERROR:bus.cc`` lines that explain nothing.
_HEAD_SEVERITIES = ("FATAL:", "ERROR:")
#: Marks the bytes left out between the quoted head and the quoted tail.
GAP = "..."
#: Joins the quoted lines.
_SEP = " | "

#: What Chromium's zygote writes when it gets no sandbox, and the remedy the detail
#: then names for the common cause, a kernel that restricts unprivileged user
#: namespaces (Ubuntu 23.10+ with ``kernel.apparmor_restrict_unprivileged_userns=1``):
#: an AppArmor profile the operator attaches to the one executable path, which keeps
#: Chromium's own sandbox. Stated as a condition: user namespaces off by sysctl or by
#: a container's seccomp give the same line, and a profile does not help there.
NO_SANDBOX_MARKER = "No usable sandbox"
NO_SANDBOX_HINT = (
    "If this host restricts unprivileged user namespaces (Ubuntu 23.10+ with AppArmor), "
    "attach an AppArmor profile granting userns to Kiro's Chromium executable path, as "
    "Chromium documents at https://chromium.googlesource.com/chromium/src/+/main/docs/"
    "security/apparmor-userns-restrictions.md, rather than running it with --no-sandbox."
)


def keep_ends(fd: int) -> tuple[bytearray, bytearray]:
    """Drain *fd* on the running loop until EOF; return the buffers of its oldest
    and its newest bytes.

    Read for as long as anything holds the write end, so a chatty browser never
    blocks on a full pipe. The head keeps the first :data:`STDERR_HEAD_BYTES` and
    then stops growing; the tail keeps the newest :data:`STDERR_TAIL_BYTES`. One
    read takes a whole pipe buffer, so what the browser wrote before it exited is
    in the buffers by the time its exit is seen. *fd* is closed at EOF. POSIX only;
    the pipe launcher is its one caller.
    """
    loop = asyncio.get_running_loop()
    head = bytearray()
    tail = bytearray()
    os.set_blocking(fd, False)

    def _read() -> None:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            return
        if not chunk:
            loop.remove_reader(fd)
            os.close(fd)
            return
        head.extend(chunk[: STDERR_HEAD_BYTES - len(head)])
        tail.extend(chunk)
        del tail[:-STDERR_TAIL_BYTES]

    loop.add_reader(fd, _read)
    return head, tail


def exit_detail(code: int, head: bytearray, tail: bytearray) -> str:
    """``Kiro's Chromium exited with code <code>`` and the redacted excerpt of its
    stderr: the head's cause, a :data:`GAP`, and the last lines, or the last lines
    alone while the tail still holds the whole stream; a quoted
    :data:`NO_SANDBOX_MARKER` carries :data:`NO_SANDBOX_HINT`."""
    from kiro_crew.agent_sdk import published_stderr_tail
    from kiro_crew.platform.context import redact_via_context

    text = tail.decode("utf-8", "replace")
    overflowed = len(tail) >= STDERR_TAIL_BYTES
    if overflowed:
        # The byte cap cut the oldest line, maybe mid-secret: drop that piece.
        text = text.partition("\n")[2]
    kept = _lines(text)
    tail_lines = kept[-STDERR_TAIL_LINES:]
    # The tail holds the whole stderr until it overflows; only then can the cause
    # have scrolled out of it, and only then is the head worth quoting.
    head_lines = _head_lines(head) if overflowed else []
    quoted_head = _head_excerpt(head_lines, tail_lines)
    # Every kept line is judged, whole head and whole tail buffer, not just the
    # lines quoted: a secret wrapped from a quoted line into the next, or held by
    # the tail outside its last lines, withholds the excerpt all the same.
    judged = published_stderr_tail(
        head_lines + kept, _SEP, len(head_lines) + len(kept), redact=redact_via_context
    )
    detail = f"Kiro's Chromium exited with code {code}"
    if not judged:
        return detail
    if judged != _SEP.join(head_lines + kept):
        return f"{detail}: {judged}"
    excerpt = _SEP.join(quoted_head + ([GAP] if quoted_head else []) + tail_lines)
    if NO_SANDBOX_MARKER in excerpt:
        return f"{detail}: {excerpt}. {NO_SANDBOX_HINT}"
    return f"{detail}: {excerpt}"


def _lines(text: str) -> list[str]:
    """*text*'s non-blank lines, stripped."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _head_lines(head: bytearray) -> list[str]:
    """The whole lines in the first bytes of the stream.

    A head that filled its buffer ends mid-line, maybe mid-secret: that piece is
    dropped before any line is judged, as the tail drops the piece its cap cut.
    """
    text = head.decode("utf-8", "replace")
    if len(head) >= STDERR_HEAD_BYTES:
        text = text.rpartition("\n")[0]
    return _lines(text)


def _head_excerpt(head_lines: list[str], tail_lines: list[str]) -> list[str]:
    """The lines worth quoting among *head_lines*: the first :data:`STDERR_HEAD_LINES`
    carrying ``FATAL:``, or failing that ``ERROR:`` (:data:`_HEAD_SEVERITIES`, in
    that order), minus any line the tail already quotes."""
    lines = [line for line in head_lines if line not in tail_lines]
    for severity in _HEAD_SEVERITIES:
        found = [line for line in lines if severity in line]
        if found:
            return found[:STDERR_HEAD_LINES]
    return []
