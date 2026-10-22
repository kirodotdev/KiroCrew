"""Run an agent-written dashboard page once, offline, over a synthetic copy of its record.

An agent composes a page for a crewmate without being able to open it: it sees sample
data, the crewmate's record is what the page will really draw, and a page that works on
the first can throw on the second. The person then meets an error instead of a page.
:func:`verify_page` closes that gap while the agent is still composing. The gateway
renders the page the way the frame does, in a headless browser on this machine, and
reports every script error and where it was thrown, so the agent fixes the page before
anyone is asked to look at it.

What the page is given
----------------------
Not the record. The page is not yet approved by any person, and the dynamic dashboard RFC
says a page nobody approved never receives a value, so the real read is taken inside the
gateway only to be copied (:func:`kiro_crew.dashboard_synth.synthesize`): the copy has the
record's shape -- field names, list lengths, nulls, types, string lengths and character
kinds, number sizes, date and id formats, the words of the closed vocabularies, and the
keys the page names -- and invented values. The browser is handed the copy alone, and every error it reports
quotes invented values. :attr:`Verification.data` says so.

The one exception is a snapshot of the page IN FORCE (``real_record=True``, asked for by
``dashboard_snapshot`` and nothing else). That page has been applied and the tab already
serves it this record, after the same redaction pass, so the run hands it the same read
the tab gets and :attr:`Verification.data` says ``record``. A page that is only saved is
never run over the record.

What runs
---------
The document is :func:`kiro_crew.dashboard_frame.compose` over that copy, so the page sees
the same ``window.kirocrew``, the same bindings and the same Content-Security-Policy it
gets in the tab. It is served as ``Content-Security-Policy: sandbox allow-scripts``, the
flags the tab mounts the frame with: an opaque origin, no storage, no dialogs, no
navigation of the top window.

The page's text is the text the tab is served, not the text the agent wrote: the dashboard
route runs the gateway's secret filter over a page's markup and script before it composes
it, and that filter can rewrite a string in a script (a long run of letters and digits that
looks like a key) so that the page breaks on the gateway and nowhere else. The caller runs
the same filter first and hands the rewritten page in. :func:`rewrite_note` says, in the
result, how many places it rewrote and on which line the first is.

Only the first paint is checked. A refill that arrives later, another locale and a
different viewport are not exercised, and a library the page loads from the CDN
allowlist is not fetched (see below), so what this reports is a floor: an error here
that comes from a missing field, a null or a wrong type is a real one for the person,
and silence is not a promise. A value the copy invents can stand in a different relation
to another than the record's did (a count above its total), and an error that depends on
such a relation may come from the copy.

How little it costs the gateway
-------------------------------
* It runs only for ``dashboard_save`` of a page written as ``view.html``.
* One check at a time across the gateway: a save that finds one running is ``skipped`` as
  busy and does not wait, and neither does one on a machine short of memory.
* The copy is made in two linear walks over a read that is already bounded, on the thread
  that runs the check and never on the event loop.
* The run has one hard limit, :data:`RUN_TIMEOUT_SECONDS`, counted from the save. At it the
  browser's whole process group is killed and the result is ``timed_out``. The read of the
  record cannot be interrupted: a read that outlasts the limit starts no browser, and the
  caller's own wait is :data:`REPLY_SECONDS`, so a save never waits longer than that for
  the check.

Nothing reaches the network
---------------------------
The page holds only invented values, but its script runs on the gateway host, beside the
gateway's own loopback services, so the run closes every way out and does not rely on one
of them:

* the frame's own policy: ``connect-src 'none'``, ``default-src 'none'``, no ``img-src``
  beyond ``data:`` and ``blob:``, ``form-action 'none'``, ``frame-src 'none'``;
* a request filter in the browser (the DevTools ``Fetch`` domain): the page document is
  the only request answered, every other one the page makes is failed before it reaches
  the network. That includes the two CDN origins the policy allows, so a library the page
  loads from there is NOT fetched and a script that needs it fails with its name
  undefined. The filter sees the page's own requests, not a worker's; the policy refuses
  a worker from a ``blob:`` URL, the only kind the page could build, and a worker script
  from a CDN origin finds no network to load from (the two layers below);
* no name resolves (``--host-resolver-rules``) and every connection goes to a closed
  local port (``--proxy-server``, loopback not exempted);
* WebRTC is switched off by flag and its constructors are removed before the page runs,
  because a CSP does not govern it.

The DevTools connection is an inherited pipe, not a port: nothing listens, so no other
process on the host can attach to the browser. The browser runs with its own sandbox on,
a throwaway profile, a scrubbed environment and a hard time limit, and the whole process
group is killed at the end. A host whose browser cannot start its sandbox gets
``skipped``; the sandbox is never turned off. A browser that exits while starting is
remembered for :data:`_UNUSABLE_SECONDS`, so a host that cannot run it pays the wait once
rather than on every save.

A library the page loads
------------------------
Because nothing is fetched, a page that draws with Chart.js or D3 runs here without the
library, and its script stops at the library's name (``ReferenceError: Chart is not
defined``) with the rest of that script unrun. That is the run's doing and not the
page's, so it does not make the page ``failed``: when it is the only error the result is
``skipped`` and says so, and when the page has other errors those are reported and the
name is listed in a note (see :func:`_split_missing_library`).

Which browser
-------------
Only one an administrator installed (see :func:`find_browser`), started by a Python an
administrator installed (:func:`find_launcher_python`). The browser runs outside the
agent's OS sandbox and a save is the agent's to trigger, so a build the agent could
write, such as a Playwright cache in the user's home, would let the agent choose the
program the gateway runs. That rule is why the check runs on Linux only: ``/Applications``
on macOS is group-writable by every administrator account, so no browser installed there
can pass it.

A picture of the page
---------------------
With ``capture=True`` the run also takes a PNG of the first paint once the page has
settled (:mod:`kiro_crew.dashboard_page_image`), in the same browser and under the same
limit, and :attr:`Verification.image` holds it or the reason there is none. Only a run
that loaded the page and was judged ``passed`` or ``failed`` carries a picture: a skipped,
timed-out or crashed run drew nothing worth showing, and a page whose script stopped at a
library the run could not fetch would be shown without that library. The picture is of
whatever the run was handed: the record for a snapshot of the page in force.

Result
------
:class:`Verification` has a status of ``passed``, ``failed``, ``timed_out`` or
``skipped``. A skip is not a failure and says why in ``reason``: no browser, an
unsupported platform, a check already running, a machine short of memory, a browser that
would not start, or a page whose script stopped at a library the run could not fetch.
Nothing in here raises: the caller is a save that must not fail because a check could not
run.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from kiro_crew import (
    dashboard_frame,
    dashboard_page_image,
    dashboard_synth,
    github_runner,
    platform_compat,
)
from kiro_crew.browser_cli.launcher import _SANDBOX_MARKERS
from kiro_crew.constants import DEFAULT_SPAWN_MIN_MEMORY_GB

logger = logging.getLogger(__name__)

__all__ = [
    "DATA_RECORD",
    "DATA_SYNTHETIC",
    "REPLY_SECONDS",
    "RUN_TIMEOUT_SECONDS",
    "STATUS_FAILED",
    "STATUS_PASSED",
    "STATUS_SKIPPED",
    "STATUS_TIMED_OUT",
    "PageError",
    "Verification",
    "find_browser",
    "preflight",
    "rewrite_note",
    "timed_out_wire",
    "verify_page",
]

STATUS_PASSED: Final[str] = "passed"
STATUS_FAILED: Final[str] = "failed"
STATUS_SKIPPED: Final[str] = "skipped"
STATUS_TIMED_OUT: Final[str] = "timed_out"

#: What :attr:`Verification.data` says when the page ran over a synthetic copy.
DATA_SYNTHETIC: Final[str] = "synthetic"

#: What :attr:`Verification.data` says when the page ran over the record itself: only a
#: snapshot of the page in force, which the tab already serves that record.
DATA_RECORD: Final[str] = "record"

#: The hard limit on one run, counted from the save: the record is read and copied, and the
#: browser must start, be handed the page and finish loading it, inside this, or its whole
#: process group is killed and the result is ``timed_out``. A clean run takes about a second.
RUN_TIMEOUT_SECONDS: Final[float] = 8.0

#: How long after the load event the page may still raise errors from timers and
#: promises, never past the run's limit. Long enough for a ``setTimeout(0)`` chain and the
#: frame's own readiness beacon, short enough to be invisible next to the browser's start.
_SETTLE_SECONDS: Final[float] = 0.5

#: How long a browser that exited while starting is not tried again.
_UNUSABLE_SECONDS: Final[float] = 900.0

#: How long a browser that may have exited is given to show it, and how long the kill
#: gets to take effect. Both are only spent when something has already gone wrong.
_EXIT_WAIT_SECONDS: Final[float] = 0.5
_REAP_SECONDS: Final[float] = 2.0

#: The longest a caller waits for the check, from the moment it asked: the run's limit,
#: the wait for an exit and the kill, and a second of margin. The save route waits no
#: longer than this for a check, whatever the check is doing.
REPLY_SECONDS: Final[float] = RUN_TIMEOUT_SECONDS + _EXIT_WAIT_SECONDS + _REAP_SECONDS + 1.0

#: Available memory below which the check is not started, in MiB: the floor the gateway
#: already holds a new agent process to. The probe fails open, so a host that cannot be
#: read is not held back.
_MIN_AVAILABLE_MIB: Final[int] = int(DEFAULT_SPAWN_MIN_MEMORY_GB * 1024)

#: Errors kept per run. A page that throws in a loop would otherwise fill the reply.
MAX_ERRORS: Final[int] = 8

#: Distinct errors remembered, so that one that repeats is counted once. Past it a new
#: error is counted every time it arrives.
_MAX_SEEN: Final[int] = MAX_ERRORS * 8

#: Characters of one error message sent back.
MAX_MESSAGE_CHARS: Final[int] = 300

#: Characters kept of one refused URL, and how many URLs are kept. The page chooses
#: both, so both are bounded where they are stored.
_MAX_URL_CHARS: Final[int] = 300
_MAX_BLOCKED_URLS: Final[int] = 20

#: Largest single DevTools message kept. A page can make the browser send an arbitrarily
#: large one (a huge ``console.error`` argument); the rest of such a message is read and
#: thrown away, so the gateway holds at most this plus one read of it. A message dropped
#: this way was not inspected, and the run says so rather than passing the page.
_MAX_MESSAGE_BYTES: Final[int] = 4 * 1024 * 1024

#: Bytes taken from the pipe in one read.
_READ_CHUNK_BYTES: Final[int] = 64 * 1024

#: Largest composed document the run will carry through the pipe.
_MAX_DOCUMENT_BYTES: Final[int] = 16 * 1024 * 1024

#: The one URL the browser answers. Reserved TLD, so it can never resolve.
_PAGE_URL: Final[str] = "https://dashboard.invalid/page"

_VIEWPORT: Final[dict[str, Any]] = {
    "width": 1280,
    "height": 800,
    "deviceScaleFactor": 1,
    "mobile": False,
}

#: Served with the page so the document behaves as the tab's frame does: an opaque
#: origin, scripts allowed, nothing else. The page's own policy comes from its meta tag.
_RESPONSE_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("Content-Type", "text/html; charset=utf-8"),
    ("Content-Security-Policy", "sandbox allow-scripts"),
    ("Cache-Control", "no-store"),
)

#: Run before any page script. WebRTC is the one channel the page policy does not close.
_NO_EGRESS_JS: Final[str] = (
    "for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'RTCDataChannel']) {"
    " try { Object.defineProperty(window, name, { value: undefined, configurable: false }); }"
    " catch (err) {} }"
)

#: Switches shared by every browser build. ``--no-sandbox`` is never among them.
_BROWSER_ARGS: Final[tuple[str, ...]] = (
    "--remote-debugging-pipe",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-service-autorun",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-extensions",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-component-update",
    "--disable-background-networking",
    "--disable-breakpad",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--mute-audio",
    "--metrics-recording-only",
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--proxy-server=http://127.0.0.1:9",
    "--proxy-bypass-list=<-loopback>",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--js-flags=--max-old-space-size=512",
)

#: Runs in the child between fork and exec, as a fresh interpreter rather than a
#: ``preexec_fn``, which is not safe in a process with threads. DevTools over a pipe
#: reads commands on descriptor 3 and writes replies on 4; the parent's pipe ends are
#: copied there. Duplicated above 10 first so that an end that already sits on 3 or 4
#: is not overwritten before it is copied.
_LAUNCHER: Final[str] = (
    "import fcntl, os, sys\n"
    "r, w = int(sys.argv[1]), int(sys.argv[2])\n"
    "a = fcntl.fcntl(r, fcntl.F_DUPFD, 10)\n"
    "b = fcntl.fcntl(w, fcntl.F_DUPFD, 10)\n"
    "os.dup2(a, 3)\n"
    "os.dup2(b, 4)\n"
    "for fd in {r, w, a, b} - {3, 4}:\n"
    "    os.close(fd)\n"
    "os.execv(sys.argv[3], sys.argv[3:])\n"
)

#: The one slot for the whole gateway: a check holds it from before it reads the record to
#: after the browser is gone, and a save that finds it taken is ``skipped`` at once and
#: does not queue. The machine runs other work, and two Chromiums at once is the easiest
#: way to make a save slow.
_SLOT = threading.Lock()

#: ``browser path -> (when, why)`` for a browser that exited while starting. Read and
#: written only while the slot is held.
_UNUSABLE: dict[str, tuple[float, str]] = {}


@dataclasses.dataclass(frozen=True)
class PageError:
    """One error the page raised.

    ``kind`` is ``exception`` (uncaught), ``rejection`` (unhandled promise), ``console``
    (the page's own ``console.error`` or failed ``console.assert``), ``csp`` (the frame
    refused something the page tried), ``oversize`` (a message too large to inspect),
    ``timeout`` (the page document never finished loading) or ``crash``. ``line`` and ``column``
    are 1-based positions inside the page's own HTML, or ``None`` when the error did not
    come from the page's markup.
    """

    kind: str
    message: str
    line: int | None = None
    column: int | None = None

    def wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "line": self.line,
            "column": self.column,
        }


@dataclasses.dataclass(frozen=True)
class Verification:
    """The outcome of one run. See the module docstring for what each status means."""

    status: str
    errors: tuple[PageError, ...] = ()
    #: Why the run was skipped or timed out. Empty for the other two statuses.
    reason: str = ""
    #: Facts the reader needs to weigh the result, such as the requests that were refused.
    notes: tuple[str, ...] = ()
    #: Errors past :data:`MAX_ERRORS` that were counted and not kept.
    omitted: int = 0
    elapsed_ms: int = 0
    #: What the page was given: :data:`DATA_SYNTHETIC` when it ran over the synthetic
    #: copy, :data:`DATA_RECORD` when it ran over the record itself, empty when it was
    #: never given anything.
    data: str = ""
    #: The picture a ``capture=True`` run took, or why it has none; ``None`` when no
    #: picture was asked for or the run never got as far as one. Never on the wire.
    image: dashboard_page_image.PageImage | None = None

    def wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "data": self.data,
            "errors": [error.wire() for error in self.errors],
            "reason": self.reason,
            "notes": list(self.notes),
        }
        if self.omitted:
            out["omitted"] = self.omitted
        return out


def _skipped(reason: str, started: float) -> Verification:
    logger.info("dashboard verification skipped: %s", reason)
    return Verification(STATUS_SKIPPED, reason=reason, elapsed_ms=_ms_since(started))


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# --------------------------------------------------------------------------- #
# which browser
# --------------------------------------------------------------------------- #

#: Install locations of a system browser: the real executable, never a wrapper script or a
#: link to one, because the provenance check below refuses a link. Fixed paths, never a
#: ``PATH`` lookup.
_SYSTEM_BROWSERS: Final[tuple[str, ...]] = (
    "/opt/google/chrome/chrome",
    "/usr/lib/chromium/chromium",
    "/usr/lib/chromium-browser/chromium-browser",
    "/usr/lib64/chromium-browser/chromium-browser",
    "/opt/microsoft/msedge/msedge",
)

#: Where an administrator-installed Python lives. The launcher that places the DevTools
#: pipe on descriptors 3 and 4 runs under one, and under no other: the gateway's own
#: interpreter often sits in a virtualenv the agent can write, and what the launcher
#: execs is only as trustworthy as the program that execs it.
_SYSTEM_PYTHONS: Final[tuple[str, ...]] = ("/usr/bin/python3",)


def find_browser() -> str | None:
    """A browser this gateway may run, or ``None``.

    Only one that the agent could not have put there: root-owned, not a link, and not
    writable by this account through any parent
    (``validate_provider_executable(..., require_protected=True)``). The browser runs
    OUTSIDE the agent's OS sandbox, and a save is the agent's to trigger, so a user-owned
    build such as one in a Playwright cache the agent can write would let it run a program
    of its own choosing with the gateway's reach.
    That leaves browsers an administrator installed, which is why this is the whole list.
    """
    for candidate in _SYSTEM_BROWSERS:
        if not os.path.isfile(candidate):
            continue
        try:
            return github_runner.validate_provider_executable(candidate, require_protected=True)
        except Exception:
            logger.debug("browser candidate refused: %s", candidate, exc_info=True)
    return None


def find_launcher_python() -> str | None:
    """A Python this gateway may run to start the browser, or ``None``.

    Held to the same bar as the browser (:func:`find_browser`): root-owned, not a link,
    not writable by this account. A link such as ``/usr/bin/python3`` is resolved first,
    and it is the resolved file that is checked and run.
    """
    for candidate in _SYSTEM_PYTHONS:
        if not os.path.isfile(candidate):
            continue
        try:
            return github_runner.validate_provider_executable(
                os.path.realpath(candidate), require_protected=True
            )
        except Exception:
            logger.debug("launcher interpreter refused: %s", candidate, exc_info=True)
    return None


# --------------------------------------------------------------------------- #
# the page and where its lines land
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class _Document:
    """The composed document and where the page's own markup starts inside it."""

    html: str
    #: Zero-based line of the composed document on which the page's first line sits.
    first_line: int
    #: Lines the page occupies.
    page_lines: int

    def locate(self, line: int | None, column: int | None) -> tuple[int | None, int | None]:
        """A browser position (zero-based, in the composed document) as a position in the
        page's own HTML (one-based), or ``(None, None)`` when it is outside the page."""
        if line is None:
            return None, None
        relative = line - self.first_line
        if not 0 <= relative < self.page_lines:
            return None, None
        return relative + 1, (column + 1) if column is not None and column >= 0 else None


def _compose(page_html: str, read: Mapping[str, Any]) -> _Document:
    document = dashboard_frame.compose(page_html, read)
    # The page is placed between two host scripts on lines of its own, so its first line
    # is the one after the `</script>` that precedes it.
    anchor = f"</script>\n{page_html}\n<script>"
    index = document.find(anchor)
    if index < 0:
        return _Document(document, -1, 0)
    start = index + len("</script>\n")
    return _Document(document, document.count("\n", 0, start), page_html.count("\n") + 1)


#: What the gateway's secret filter writes where it takes something out of a page:
#: ``[REDACTED: credential]`` and the others of its family. Counted to say how many places
#: it rewrote, and never shown.
_REWRITE_MARK: Final[re.Pattern[str]] = re.compile(r"\[REDACTED[^\]\n]{0,80}\]")

_COMPARE_CHUNK: Final[int] = 4096


def _first_difference(before: str, after: str) -> int:
    """The offset at which two texts first differ, or the shorter one's length when it
    begins the other. Whole chunks are compared first, so the long stretch the two share
    costs a C-speed comparison rather than a character at a time."""
    limit = min(len(before), len(after))
    start = 0
    while (
        start < limit
        and before[start : start + _COMPARE_CHUNK] == after[start : start + _COMPARE_CHUNK]
    ):
        start += _COMPARE_CHUNK
    index = start
    stop = min(start + _COMPARE_CHUNK, limit)
    while index < stop and before[index] == after[index]:
        index += 1
    return min(index, limit)


def rewrite_note(before: str, after: str) -> str:
    """What the dashboard's secret filter did to a page, in a sentence; ``""`` if nothing.

    *before* is the page as the agent wrote it and *after* the same page once the filter
    the dashboard route runs over a page's text (``member_dashboard._template_text_safe``)
    has been over it. The page the tab runs is *after*, so that is what the check runs, and
    a script that depends on text the filter replaced fails here exactly as it does there.

    It says how many places were rewritten and on which line of *before* the first one is,
    and never what was replaced. The count is the filter's own marks added to the page, at
    least one when the text changed without one; the line comes from the first byte at
    which the two texts differ, so one pass over the page is all it costs.
    """
    if before == after:
        return ""
    line = before.count("\n", 0, _first_difference(before, after)) + 1
    places = max(1, len(_REWRITE_MARK.findall(after)) - len(_REWRITE_MARK.findall(before)))
    where = f"on line {line}" if places == 1 else f"the first on line {line}"
    note = (
        f"Before this page is shown, the gateway's secret filter rewrote {places} "
        f"{'place' if places == 1 else 'places'} in it, {where} (what it replaced is not "
        "shown). The check works on the rewritten page, as the live page does. The filter "
        "replaces text that looks like a key or a token, and a long run of letters and "
        "digits can look like one."
    )
    if before.count("\n") != after.count("\n"):
        note += " Error positions count the lines of the rewritten page."
    return note


# --------------------------------------------------------------------------- #
# DevTools over a pipe
# --------------------------------------------------------------------------- #


class _TimedOut(Exception):
    """The run's deadline passed while the browser was still being waited on."""


class _BrowserGone(Exception):
    """The browser closed the pipe or exited.

    ``failed_to_start`` is set when it exited before answering its first command, which
    is a property of the host and not of the page.
    """

    def __init__(self, message: str, *, failed_to_start: bool = False) -> None:
        super().__init__(message)
        self.failed_to_start = failed_to_start


class _CdpError(Exception):
    """The browser answered a command with an error."""


class _Pipe:
    """DevTools messages over two inherited pipes: JSON objects separated by NUL bytes."""

    def __init__(self, to_browser: int, from_browser: int) -> None:
        self._out = to_browser
        self._in = from_browser
        os.set_blocking(self._out, False)
        os.set_blocking(self._in, False)
        self._selector = selectors.DefaultSelector()
        self._buffer = b""
        #: The largest message kept. Raised only while the page's picture is read back.
        self.max_message_bytes = _MAX_MESSAGE_BYTES
        #: True while the tail of an oversize message is being read and dropped.
        self._discarding = False
        #: How many oversize messages were dropped.
        self.dropped = 0
        self._next_id = 0
        self.on_event: Callable[[dict[str, Any]], None] = lambda _message: None

    def close(self) -> None:
        """Release the selector. The descriptors belong to the caller, which closes each
        one exactly once."""
        self._selector.close()

    def _wait(self, fd: int, event: int, deadline: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        self._selector.register(fd, event)
        try:
            return bool(self._selector.select(remaining))
        finally:
            self._selector.unregister(fd)

    def send(
        self, method: str, params: Mapping[str, Any] | None, session: str | None, deadline: float
    ) -> int:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        data = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\0"
        view = memoryview(data)
        while view:
            try:
                written = os.write(self._out, view)
            except BlockingIOError:
                if not self._wait(self._out, selectors.EVENT_WRITE, deadline):
                    raise _TimedOut("the browser stopped reading commands") from None
                continue
            except OSError as exc:
                raise _BrowserGone(f"the browser closed its command pipe ({exc.strerror})") from exc
            view = view[written:]
        return self._next_id

    def receive(self, deadline: float) -> dict[str, Any] | None:
        """The next message, or ``None`` when *deadline* passes first."""
        while True:
            end = self._buffer.find(b"\0")
            if end >= 0:
                raw, self._buffer = self._buffer[:end], self._buffer[end + 1 :]
                if self._discarding:
                    self._discarding = False
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    logger.debug("dropped an unreadable DevTools message")
                    continue
                if isinstance(message, dict):
                    return message
                continue
            if self._discarding or len(self._buffer) > self.max_message_bytes:
                if not self._discarding:
                    self.dropped += 1
                self._discarding = True
                self._buffer = b""
            if not self._wait(self._in, selectors.EVENT_READ, deadline):
                return None
            try:
                chunk = os.read(self._in, _READ_CHUNK_BYTES)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise _BrowserGone(f"the browser closed its reply pipe ({exc.strerror})") from exc
            if not chunk:
                raise _BrowserGone("the browser exited")
            self._buffer += chunk

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session: str | None = None,
        deadline: float,
    ) -> dict[str, Any]:
        """Send one command and wait for its reply. Events that arrive meanwhile are
        handed to :attr:`on_event`, so a request the page makes while this waits is
        still answered."""
        wanted = self.send(method, params, session, deadline)
        while True:
            message = self.receive(deadline)
            if message is None:
                raise _TimedOut(f"the browser did not answer {method} in time")
            if message.get("id") == wanted:
                error = message.get("error")
                if error:
                    raise _CdpError(f"{method}: {error.get('message', error)}")
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            if "id" not in message:
                self.on_event(message)


# --------------------------------------------------------------------------- #
# what the page said
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class _Raw:
    kind: str
    message: str
    line: int | None = None
    column: int | None = None


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 1] + "\u2026"


def _without_queries(text: str) -> str:
    """*text* with the query and fragment cut off every URL in it. A page that builds a
    URL out of its data would otherwise have that data echoed back in an error.

    Quadratic in the length of *text* when it holds many ``http://`` and no query, so a
    caller hands it a bounded text."""
    return re.sub(r"(https?://[^\s'\"?#]+)[?#][^\s'\"]*", r"\1", text)


#: How much of a page's message is scrubbed for URL queries. The page chooses the message
#: and its length, and what the agent is shown is clipped to :data:`MAX_MESSAGE_CHARS`
#: anyway, so the scrub looks at a bounded stretch and the work stays small.
_SCRUB_CHARS: Final[int] = 2048


def _message(text: str) -> str:
    """Page-controlled *text* as the agent reads it: no URL query, one line, clipped."""
    return _clip(_without_queries(" ".join(text.split())[:_SCRUB_CHARS]))


def _first_frame(stack: Any) -> tuple[int | None, int | None, str]:
    frames = stack.get("callFrames") if isinstance(stack, Mapping) else None
    if isinstance(frames, list) and frames and isinstance(frames[0], Mapping):
        top = frames[0]
        return top.get("lineNumber"), top.get("columnNumber"), str(top.get("url") or "")
    return None, None, ""


#: Hosts the frame's policy lets a page load scripts from.
_SCRIPT_HOSTS: Final[tuple[str, ...]] = tuple(
    origin.split("://", 1)[1] for origin in dashboard_frame.RESOURCE_ORIGINS
)

#: What the browser says when a script names a global that nothing defined, as the first
#: thing in an uncaught error or a ``console.error``.
_UNDEFINED_NAME: Final[re.Pattern[str]] = re.compile(
    r"^(?:console\.error: )?ReferenceError: ([\w$]+) is not defined\b"
)


#: Globals a library defines that its URL does not spell.
_LIBRARY_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "$": ("jquery",),
    "_": ("lodash", "underscore"),
    "L": ("leaflet",),
    "hljs": ("highlight",),
}

#: Path words the two CDNs put in front of every library, which name none.
_URL_FILLER: Final[frozenset[str]] = frozenset(
    {"npm", "gh", "ajax", "libs", "lib", "dist", "build", "umd", "min", "js", "css", "files"}
)


def _is_library_url(url: str) -> bool:
    try:
        return urlsplit(url).hostname in _SCRIPT_HOSTS
    except ValueError:
        return False


def _defined_by(name: str, url: str) -> bool:
    """Whether the library at *url* plausibly defines the global *name*.

    Read from the URL alone, because nothing was fetched: ``Chart`` for ``.../chart.js@4``,
    ``d3`` for ``.../d3/7.8.5/d3.min.js``, ``ReactDOM`` for ``.../react-dom``. A name the
    URL does not suggest is the page's own, so a typo on a page that loads a library is
    still reported.
    """
    words = [
        word
        for word in re.sub(r"[^a-z0-9]+", " ", urlsplit(url).path.lower()).split()
        if word not in _URL_FILLER
    ]
    words += [a + b for a, b in zip(words, words[1:])]
    return any(
        word == wanted or (len(wanted) >= 4 and word.startswith(wanted))
        for wanted in _LIBRARY_ALIASES.get(name) or (name.lower(),)
        for word in words
    )


class _Collector:
    """Turns DevTools events into the errors a page author can act on."""

    def __init__(self) -> None:
        self.errors: list[_Raw] = []
        self.omitted = 0
        self.blocked: list[str] = []
        #: Distinct-or-not requests refused past :data:`_MAX_BLOCKED_URLS`, counted so the
        #: note can say they exist.
        self.blocked_overflow = 0
        #: A script request to one of the CDN origins the frame allows was refused, so a
        #: library the page uses was never loaded; and which ones, up to
        #: :data:`_MAX_BLOCKED_URLS`.
        self.library_refused = False
        self.library_urls: list[str] = []
        #: The page document itself has been navigated to. Its load event is the one that
        #: counts: the blank page the tab starts on can finish loading first.
        self.committed = False
        self.loaded = False
        self.crashed = False
        #: The picture of the settled page, when one was asked for and the page loaded.
        self.image: dashboard_page_image.PageImage | None = None
        self._seen: set[tuple[str, str, int | None]] = set()

    def refuse(self, url: str, resource_type: str) -> None:
        """Note a request that was refused. The page chooses the URL, so how many are kept
        and how long each is are bounded here."""
        kept = url[:_MAX_URL_CHARS]
        if kept not in self.blocked:
            if len(self.blocked) < _MAX_BLOCKED_URLS:
                self.blocked.append(kept)
            else:
                self.blocked_overflow += 1
        if resource_type == "Script" and _is_library_url(url):
            self.library_refused = True
            if kept not in self.library_urls and len(self.library_urls) < _MAX_BLOCKED_URLS:
                self.library_urls.append(kept)

    def add(self, raw: _Raw) -> None:
        key = (raw.kind, raw.message, raw.line)
        if key in self._seen:
            return
        if len(self._seen) < _MAX_SEEN:
            self._seen.add(key)
        if len(self.errors) >= MAX_ERRORS:
            # Counted, not kept: a page that throws without end must not grow this.
            self.omitted += 1
            return
        self.errors.append(raw)

    def on_event(self, method: str, params: Mapping[str, Any]) -> None:
        if method == "Runtime.exceptionThrown":
            self._exception(params.get("exceptionDetails") or {})
        elif method == "Runtime.consoleAPICalled":
            self._console(params)
        elif method == "Log.entryAdded":
            self._log(params.get("entry") or {})
        elif method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if frame.get("url") == _PAGE_URL and not frame.get("parentId"):
                self.committed = True
        elif method == "Page.loadEventFired":
            if self.committed:
                self.loaded = True
        elif method in ("Inspector.targetCrashed", "Target.targetCrashed"):
            self.crashed = True

    def _exception(self, details: Mapping[str, Any]) -> None:
        exception = details.get("exception") or {}
        description = exception.get("description")
        text = str(details.get("text") or "Uncaught")
        if isinstance(description, str) and description:
            message = description.split("\n", 1)[0]
        elif "value" in exception:
            message = f"{text} {exception['value']}"
        else:
            message = text
        line, column, url = _first_frame(details.get("stackTrace"))
        if line is None:
            line, column, url = (
                details.get("lineNumber"),
                details.get("columnNumber"),
                str(details.get("url") or ""),
            )
        if url and url != _PAGE_URL:
            line = column = None
        kind = "rejection" if "in promise" in text else "exception"
        self.add(_Raw(kind, _message(message), line, column))

    def _console(self, params: Mapping[str, Any]) -> None:
        if params.get("type") not in ("error", "assert"):
            return
        parts = []
        for arg in (params.get("args") or [])[:5]:
            if not isinstance(arg, Mapping):
                continue
            value = arg.get("value", arg.get("description", ""))
            parts.append(value if isinstance(value, str) else json.dumps(value, default=str))
        line, column, url = _first_frame(params.get("stackTrace"))
        if url and url != _PAGE_URL:
            line = column = None
        label = "console.assert failed" if params.get("type") == "assert" else "console.error"
        self.add(
            _Raw("console", _message(f"{label}: {' '.join(parts)}".rstrip(": ")), line, column)
        )

    def _log(self, entry: Mapping[str, Any]) -> None:
        # Only what the frame's policy refused. A script exception is also logged here,
        # but it is reported once, from the runtime event; a failed request is this run's
        # own doing and is listed as a note.
        if entry.get("source") != "security" or entry.get("level") != "error":
            return
        self.add(_Raw("csp", _message(str(entry.get("text") or "")), None, None))


def _short_url(url: str) -> str:
    url = _without_queries(url)
    return url if len(url) <= 120 else url[:119] + "\u2026"


# --------------------------------------------------------------------------- #
# the browser process
# --------------------------------------------------------------------------- #


def _spawn(
    python: str, browser: str, profile: str, commands: int, replies: int
) -> subprocess.Popen[bytes]:
    """Start the browser with DevTools reading commands on *commands* and writing
    replies on *replies*, which the launcher moves to descriptors 3 and 4.

    Fixed argv: nothing the agent wrote reaches it. The page is data delivered over the
    pipe, never a flag or a path. It starts in the throwaway profile directory, so nothing
    it reads or writes by a relative name touches the gateway's own working directory.
    """
    headless = "--headless" if "headless" in os.path.basename(browser) else "--headless=new"
    env = {"PATH": "/usr/bin:/bin", "HOME": profile, "TMPDIR": profile, "LANG": "C.UTF-8"}
    argv = [
        python,
        "-I",
        "-S",
        "-c",
        _LAUNCHER,
        str(commands),
        str(replies),
        browser,
        headless,
        f"--user-data-dir={profile}",
        *_BROWSER_ARGS,
    ]
    with open(os.path.join(profile, "stderr.log"), "wb") as stderr:
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            env=env,
            cwd=profile,
            pass_fds=(commands, replies),
            close_fds=True,
            start_new_session=True,
        )


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Kill the browser and everything it started, then reap it.

    The group is killed while the browser is still unreaped, the one time its pid cannot
    name another process's group; this is why nothing before it reaps (:func:`_exited`).
    """
    platform_compat.kill_popen_tree(proc)
    try:
        proc.wait(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning("the verification browser did not exit after SIGKILL")


def _exited(proc: subprocess.Popen[bytes], seconds: float) -> bool:
    """Whether *proc* has exited within *seconds*, without reaping it.

    ``wait`` would reap the browser, and the group of a browser that died mid-run could
    then not be killed, leaving its child processes running after the check is over.
    """
    end = time.monotonic() + seconds
    while True:
        try:
            if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return True
        except ChildProcessError:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def _path_free(text: str) -> str:
    """*text* with every absolute path cut down to its last component."""
    return re.sub(r"/(?:[^\s:'\"]+/)+([^\s:'\"/]+)", r"\1", text)


def _exit_reason(proc: subprocess.Popen[bytes], profile: str) -> str:
    """Why the browser is gone, from the last line it wrote, or ``""`` if it is alive."""
    if not _exited(proc, _EXIT_WAIT_SECONDS):
        return ""
    try:
        data = Path(profile, "stderr.log").read_bytes()[-2000:]
    except OSError:
        data = b""
    text = data.decode("utf-8", "replace")
    if any(marker in text for marker in _SANDBOX_MARKERS):
        return (
            "the browser cannot start its sandbox on this machine (it needs unprivileged "
            "user namespaces), and Kiro Crew does not turn the sandbox off"
        )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "the browser exited before it ran the page"
    return _clip(_path_free(re.sub(r"^\[[^\]]*\]\s*", "", lines[-1])))


class _Run:
    """One page load: the request filter, the event stream and the settle wait."""

    def __init__(
        self, pipe: _Pipe, document: _Document, deadline: float, *, capture: bool = False
    ) -> None:
        self._pipe = pipe
        self._document = document
        self._deadline = deadline
        self._capture = capture
        self._session = ""
        self._served = False
        #: Set once the browser has answered a command.
        self.started = False
        self.collector = _Collector()
        pipe.on_event = self._handle

    def _handle(self, message: Mapping[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        if method == "Fetch.requestPaused":
            self._answer(params)
        elif method == "Page.javascriptDialogOpening":
            self._pipe.send(
                "Page.handleJavaScriptDialog", {"accept": False}, self._session, self._deadline
            )
        else:
            self.collector.on_event(method, params)

    def _answer(self, params: Mapping[str, Any]) -> None:
        """Fulfil the page, wave through inline schemes, fail everything else."""
        request_id = params.get("requestId")
        url = str((params.get("request") or {}).get("url") or "")
        if url == _PAGE_URL and not self._served:
            self._served = True
            body = base64.b64encode(self._document.html.encode("utf-8")).decode("ascii")
            self._reply(
                "Fetch.fulfillRequest",
                {
                    "requestId": request_id,
                    "responseCode": 200,
                    "responseHeaders": [{"name": n, "value": v} for n, v in _RESPONSE_HEADERS],
                    "body": body,
                },
            )
        elif url.startswith(("data:", "blob:", "about:")):
            self._reply("Fetch.continueRequest", {"requestId": request_id})
        else:
            self.collector.refuse(url, str(params.get("resourceType") or ""))
            self._reply(
                "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
            )

    def _reply(self, method: str, params: Mapping[str, Any]) -> None:
        self._pipe.send(method, params, self._session, self._deadline)

    def _pump(self, until: float, stop: Callable[[], bool]) -> None:
        while not stop():
            message = self._pipe.receive(until)
            if message is None:
                return
            if "id" not in message:
                self._handle(message)

    def execute(self) -> _Collector:
        pipe, deadline = self._pipe, self._deadline
        target = pipe.call("Target.createTarget", {"url": "about:blank"}, deadline=deadline)
        self.started = True
        attached = pipe.call(
            "Target.attachToTarget",
            {"targetId": target["targetId"], "flatten": True},
            deadline=deadline,
        )
        self._session = str(attached["sessionId"])
        for method, params in (
            ("Page.enable", {}),
            ("Runtime.enable", {}),
            ("Log.enable", {}),
            ("Emulation.setDeviceMetricsOverride", _VIEWPORT),
            ("Page.addScriptToEvaluateOnNewDocument", {"source": _NO_EGRESS_JS}),
            ("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}),
        ):
            pipe.call(method, params, session=self._session, deadline=deadline)
        navigated = pipe.call(
            "Page.navigate", {"url": _PAGE_URL}, session=self._session, deadline=deadline
        )
        if navigated.get("errorText"):
            raise _BrowserGone(f"the browser refused to load the page ({navigated['errorText']})")
        collector = self.collector
        # One limit covers the start and the page: what the page does not use, the
        # settle wait may, and nothing runs past it.
        self._pump(deadline, lambda: collector.loaded or collector.crashed)
        if collector.loaded:
            self._pump(min(deadline, time.monotonic() + _SETTLE_SECONDS), lambda: collector.crashed)
        if pipe.dropped:
            collector.add(
                _Raw(
                    "oversize",
                    f"the page made the browser send {pipe.dropped} message(s) over "
                    f"{_MAX_MESSAGE_BYTES // 1024} KiB, which were not inspected "
                    "(a very large console.error or thrown value)",
                )
            )
        if self._capture and collector.loaded and not collector.crashed:
            # After the oversize count above, so the picture's own large reply is never
            # mistaken for a message the page made the browser send.
            collector.image = self._picture()
        return collector

    def _picture(self) -> dashboard_page_image.PageImage:
        """A PNG of the settled page, or why there is none. Never raises."""
        pipe = self._pipe

        def call(params: Mapping[str, Any]) -> Mapping[str, Any]:
            return pipe.call(
                "Page.captureScreenshot", params, session=self._session, deadline=self._deadline
            )

        pipe.max_message_bytes = dashboard_page_image.MAX_REPLY_BYTES
        try:
            return dashboard_page_image.capture(call, (_VIEWPORT["width"], _VIEWPORT["height"]))
        except _TimedOut:
            reason = "the check reached its time limit before the picture was taken"
        except _BrowserGone:
            reason = "the browser closed before the picture was taken"
        except _CdpError:
            reason = "the browser could not take the picture"
        finally:
            pipe.max_message_bytes = _MAX_MESSAGE_BYTES
        return dashboard_page_image.PageImage(reason=reason)


def _drive(
    python: str, browser: str, document: _Document, deadline: float, capture: bool = False
) -> _Collector:
    """Load the document in *browser* and collect what the page raises, and with
    *capture* a picture of it.

    Each descriptor is closed exactly once, by this function: a number closed twice can
    by then belong to another thread's file.
    """
    profile = tempfile.mkdtemp(prefix="kirocrew-verify-")
    owned: set[int] = set()
    proc: subprocess.Popen[bytes] | None = None
    pipe: _Pipe | None = None
    run: _Run | None = None

    def release(fd: int) -> None:
        if fd in owned:
            owned.discard(fd)
            try:
                os.close(fd)
            except OSError:
                pass

    try:
        commands_read, commands_write = os.pipe()
        owned.update((commands_read, commands_write))
        replies_read, replies_write = os.pipe()
        owned.update((replies_read, replies_write))
        proc = _spawn(python, browser, profile, commands_read, replies_write)
        # The child holds its own copies. Ours would keep the reply pipe open after it
        # exits and hide the exit from the reader.
        release(commands_read)
        release(replies_write)
        pipe = _Pipe(commands_write, replies_read)
        run = _Run(pipe, document, deadline, capture=capture)
        return run.execute()
    except _BrowserGone as exc:
        reason = _exit_reason(proc, profile) if proc is not None else ""
        starting = run is None or not run.started
        raise _BrowserGone(reason or str(exc), failed_to_start=bool(reason) and starting) from None
    finally:
        if proc is not None:
            _stop(proc)
        if pipe is not None:
            pipe.close()
        for fd in list(owned):
            release(fd)
        shutil.rmtree(profile, ignore_errors=True)


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #


def _notes(collector: _Collector) -> tuple[str, ...]:
    if not collector.blocked:
        return ()
    listed = ", ".join(_short_url(url) for url in collector.blocked[:3])
    more = len(collector.blocked) - 3 + collector.blocked_overflow
    if more > 0:
        listed += f" and {more} more"
    note = f"This check runs offline, so these requests were refused: {listed}."
    if collector.library_refused:
        note += (
            " One was a script from a CDN origin, so code that depends on it did not "
            "run and was not checked."
        )
    return (note,)


def _split_missing_library(collector: _Collector) -> tuple[list[_Raw], list[str]]:
    """The errors the page raised, and the names an unfetched library left undefined.

    A page that draws with Chart.js or D3 loads it from a CDN origin the frame allows, and
    this run fetches nothing. The script that uses the library stops at its name, and the
    rest of that script does not run. That error is the run's doing, not the page's, so it
    is taken out of the page's errors. The test is narrow on purpose: a script request to
    one of those origins was refused, the error is an undefined name raised as the first
    thing in an uncaught error or a ``console.error``, and a refused URL suggests that
    name (:func:`_defined_by`). A typo beside a library is still reported, and so is any
    other error the missing library causes. With errors omitted there are too many
    distinct ones to attribute any of them.
    """
    if not collector.library_urls or collector.omitted:
        return list(collector.errors), []
    kept: list[_Raw] = []
    missing: list[str] = []
    for raw in collector.errors:
        found = _UNDEFINED_NAME.match(raw.message)
        if found and any(_defined_by(found.group(1), url) for url in collector.library_urls):
            missing.append(found.group(1))
        else:
            kept.append(raw)
    return kept, list(dict.fromkeys(missing))


def _undefined(names: list[str]) -> str:
    shown = ", ".join(f"'{name} is not defined'" for name in names[:3])
    return shown + (f" and {len(names) - 3} more" if len(names) > 3 else "")


#: Said under a ``failed`` result, because the values the page ran over are invented.
_INVENTED_NOTE: Final[str] = (
    "The values this page ran over are invented. An error that comes from a missing field, a "
    "null or a wrong type is a real one. One that depends on how two values compare, such as "
    "a count above its total, may come from the copy."
)

_BUSY: Final[str] = (
    "busy: another page check is running, so this one was not started; save again in a moment"
)


def _verdict(
    collector: _Collector, document: _Document, started: float, data: str = DATA_SYNTHETIC
) -> Verification:
    raws, missing = _split_missing_library(collector)
    errors = [
        PageError(raw.kind, raw.message, *document.locate(raw.line, raw.column)) for raw in raws
    ]
    notes = _notes(collector)
    if collector.crashed:
        errors.append(PageError("crash", "the page crashed the browser tab"))
    elif not collector.loaded:
        # The limit passed before the page document finished loading. If the browser had
        # not got as far as the document, the machine was slow; if it had, a script that
        # never returns is the likely reason and the agent is told so.
        if collector.committed:
            errors.append(
                PageError(
                    "timeout",
                    "the page did not finish loading before the time limit; "
                    "a script that never returns would also freeze the tab",
                )
            )
        return Verification(
            STATUS_TIMED_OUT,
            tuple(errors),
            reason=_limit_reason(),
            notes=notes,
            omitted=collector.omitted,
            elapsed_ms=_ms_since(started),
            data=data,
        )
    if missing:
        if not errors:
            # Everything the page raised came from a library this run cannot fetch, so
            # what follows that call in the same script was never run. That is not a pass,
            # and the page is not at fault, so it is not a failure either.
            return Verification(
                STATUS_SKIPPED,
                reason=(
                    "a script the page loads from a CDN origin was not fetched by this "
                    f"offline check, and the page's own script then stopped at {_undefined(missing)}"
                ),
                notes=notes,
                elapsed_ms=_ms_since(started),
                data=data,
            )
        notes += (
            f"Not listed above: {_undefined(missing)}, most likely because a script the page "
            "loads from a CDN origin was not fetched by this offline check.",
        )
    status = STATUS_FAILED if errors else STATUS_PASSED
    if errors and data == DATA_SYNTHETIC:
        notes += (_INVENTED_NOTE,)
    return Verification(
        status,
        tuple(errors),
        notes=notes,
        omitted=collector.omitted,
        elapsed_ms=_ms_since(started),
        data=data,
        image=_image_to_keep(collector, missing),
    )


def _image_to_keep(
    collector: _Collector, missing: list[str]
) -> dashboard_page_image.PageImage | None:
    """The picture the run may hand back: none for a crashed page, and none for a page
    that loads a library the run could not fetch, which says so instead.

    Any refused library script counts, not only one whose name an error then named: a page
    that guards its drawing with ``if (window.Chart)`` raises nothing and still draws
    without it, which is not the page the tab shows."""
    if collector.crashed or collector.image is None:
        return None
    if missing or collector.library_refused:
        which = (
            _undefined(missing)
            if missing
            else ", ".join(_short_url(url) for url in collector.library_urls[:3])
        )
        return dashboard_page_image.PageImage(
            reason=(
                "the page loads a library from a CDN origin that this offline run does not "
                f"fetch ({which}), so a picture would leave that part out"
            )
        )
    return collector.image


def _limit_reason() -> str:
    return f"the check reached its {RUN_TIMEOUT_SECONDS:g} second limit and the browser was stopped"


def timed_out_wire(reason: str = "") -> dict[str, Any]:
    """The wire form of a check whose answer was not waited for.

    For a caller that stops waiting at :data:`REPLY_SECONDS` while the check, which holds
    the slot until it ends, is still winding down. It says nothing about what the page ran
    over, because the check may not have got as far as a copy.
    """
    return Verification(
        STATUS_TIMED_OUT,
        reason=reason
        or (
            f"the check had not finished after {REPLY_SECONDS:g} seconds, "
            "so the save stopped waiting for it"
        ),
    ).wire()


def _short_of_memory() -> str:
    free = platform_compat.host_available_mib()
    # ``0`` means the host could not be read, and an unread host is not held back.
    if 0 < free < _MIN_AVAILABLE_MIB:
        return (
            f"the machine is short of memory ({free} MiB available, {_MIN_AVAILABLE_MIB} MiB "
            "needed to start a browser), so the check was not started"
        )
    return ""


def preflight() -> str:
    """Why a check cannot start right now, or ``""`` when it can.

    The cheap tests, made before the record is read so that a save which will be skipped
    anyway costs the gateway nothing more than these: not Linux, the slot taken, a machine
    short of memory. :func:`verify_page` makes them again, and the slot's own acquire is
    the one that decides.
    """
    if not platform_compat.IS_LINUX:
        return "this check runs on Linux only"
    if _SLOT.locked():
        return _BUSY
    return _short_of_memory()


class _ReadFailed(Exception):
    """The record could not be read."""


def _copy_of_record(
    read: Mapping[str, Any] | Callable[[], Mapping[str, Any]],
    model: Mapping[str, Any] | None,
    page: str,
    real_record: bool = False,
) -> dict[str, Any]:
    """Read the record and copy it. This is the one place the real record is held: what
    comes back is the copy, and the record is released when this returns.

    With *real_record* the record itself comes back, uncopied: a snapshot of the page
    in force, which the tab already serves this record (see :func:`verify_page`)."""
    try:
        record = read() if callable(read) else read
    except Exception as exc:
        logger.warning("dashboard: the record for a page check could not be read", exc_info=True)
        raise _ReadFailed from exc
    if real_record:
        return dict(record)
    return dashboard_synth.synthesize(record, model=model, page=page)


def _run_in_slot(
    page_html: str,
    read: Mapping[str, Any] | Callable[[], Mapping[str, Any]],
    model: Mapping[str, Any] | None,
    browser: str,
    python: str,
    started: float,
    end: float,
    capture: bool = False,
    real_record: bool = False,
) -> Verification:
    """The check proper, while this thread holds the slot."""
    data = DATA_RECORD if real_record else DATA_SYNTHETIC
    if time.monotonic() >= end:
        return Verification(
            STATUS_TIMED_OUT,
            reason="the check had no time left when it got its turn",
            elapsed_ms=_ms_since(started),
        )
    known = _UNUSABLE.get(browser)
    if known is not None and time.monotonic() - known[0] < _UNUSABLE_SECONDS:
        return _skipped(f"the browser could not start: {known[1]}", started)
    try:
        copy = _copy_of_record(read, model, page_html, real_record)
    except _ReadFailed:
        return _skipped(
            "this crewmate's current data could not be read, so the page was not run", started
        )
    except dashboard_synth.SynthesisRefused as exc:
        return _skipped(f"the record could not be copied for the check ({exc})", started)
    if time.monotonic() >= end:
        # The read of the record cannot be interrupted, so a read that outlasts the limit is
        # told here, before a browser is started for a check that has no time left.
        return Verification(
            STATUS_TIMED_OUT,
            reason="the check ran out of time reading the record, so no browser was started",
            elapsed_ms=_ms_since(started),
            data=data,
        )
    document = _compose(page_html, copy)
    if len(document.html.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
        return _skipped("the page and its data are too large to run here", started)
    try:
        collector = _drive(python, browser, document, end, capture)
    except _TimedOut:
        return Verification(
            STATUS_TIMED_OUT,
            reason=_limit_reason() + " before the page was loaded",
            elapsed_ms=_ms_since(started),
            data=data,
        )
    except _BrowserGone as exc:
        if exc.failed_to_start:
            _UNUSABLE[browser] = (time.monotonic(), str(exc))
        raise
    return _verdict(collector, document, started, data)


def verify_page(
    page_html: str,
    read: Mapping[str, Any] | Callable[[], Mapping[str, Any]],
    *,
    model: Mapping[str, Any] | None = None,
    timeout: float = RUN_TIMEOUT_SECONDS,
    deadline: float | None = None,
    capture: bool = False,
    real_record: bool = False,
) -> Verification:
    """Run *page_html* once over a synthetic copy of *read* and report what it raised.

    *read* is the record the page would receive as ``window.kirocrew``, built by
    :func:`kiro_crew.dashboard_frame.read_payload`, or a function that returns it. The
    function is called only once the check holds the slot and has passed its cheap tests,
    so a check that is skipped never reads the record. *model* is the package's
    ``model``, which tells the copy which words to keep and which maps are keyed by an id
    (:func:`kiro_crew.dashboard_synth.synthesize`). *page_html* must already have passed
    :func:`kiro_crew.dashboard_frame.validate_page`, and must be the text the tab is served:
    the caller puts the agent's page through the route's own text filter first (see
    :func:`rewrite_note`).

    The record is read and copied here and goes no further: the browser is given the copy.

    *deadline* is a ``time.monotonic()`` instant past which nothing runs; left out, it is
    *timeout* from now. It lets a caller that queued the check count the wait against the
    same limit.

    *capture* also takes a picture of the settled page (:attr:`Verification.image`; see
    "A picture of the page" above). *real_record* hands the page *read* itself instead of
    a copy; only a snapshot of the page in force asks for that.

    Blocking: it reads, copies, starts a process and waits on it, so a caller on the event
    loop offloads it to a thread. Never raises.
    """
    started = time.monotonic()
    end = deadline if deadline is not None else started + timeout
    try:
        reason = preflight()
        if reason:
            return _skipped(reason, started)
        browser = find_browser()
        if browser is None:
            return _skipped(
                "no system-installed Chromium or Chrome was found on this machine (the gateway "
                "runs only a browser that this account cannot modify)",
                started,
            )
        python = find_launcher_python()
        if python is None:
            return _skipped(
                "no system-installed Python was found to start the browser (the gateway runs "
                "only a Python that this account cannot modify)",
                started,
            )
        if not _SLOT.acquire(blocking=False):
            return _skipped(_BUSY, started)
        try:
            verdict = _run_in_slot(
                page_html, read, model, browser, python, started, end, capture, real_record
            )
        finally:
            _SLOT.release()
        logger.info(
            "dashboard verification %s in %d ms (%d error(s))",
            verdict.status,
            verdict.elapsed_ms,
            len(verdict.errors),
        )
        return verdict
    except _BrowserGone as exc:
        what = "start" if exc.failed_to_start else "run the page"
        return _skipped(f"the browser could not {what}: {exc}", started)
    except Exception:
        logger.warning("dashboard verification failed to run", exc_info=True)
        return _skipped("the check could not run on this machine", started)
