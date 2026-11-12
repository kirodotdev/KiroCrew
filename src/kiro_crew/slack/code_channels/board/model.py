"""The work board's data: what one worker row and the whole board PUBLISH.

Everything here is pure. :mod:`.source` fills it from the channel conductor's own
ledger and its own workers' live sessions; :mod:`.render` turns it into Slack
blocks; the workspace service caches the same snapshot for the channel's status.

THE PUBLISHED SET IS CLOSED. A code channel is a room other people may be in, and
the work ledger is kept out of channel audiences everywhere else
(``handlers/work_ledger.contained_channel_caller``). The conductor's dispatch
exemption admits exactly these fields into the channel, and nothing else may be
added without amending that rule:

* the conductor's ``goal``, first line, at most :data:`GOAL_CHARS` characters,
  through the same passes as a summary (the board header);
* the item's ``title``;
* the DERIVED state (:data:`STATES`), never the raw ``status`` / ``reason`` words;
* the first line of the worker's last report ``summary``, at most
  :data:`SUMMARY_CHARS` characters, through the outbound redaction and with
  channel-wide and user-group pings stripped;
* ``artifacts["branch"]``;
* the pull request number, URL and CI state;
* for a worker with a tool approval pending, WHAT it asks for as one phrase of
  the closed :data:`APPROVAL_WORDS` vocabulary, chosen by the approval's ACP
  tool-call kind;
* the ``step`` (the owner's design for the board row): for a working worker,
  the tool call in flight, read from the same transcript rows the dashboard's
  live line renders (a shell command, a file being read or edited, an MCP
  tool's short name); for a worker parked on an approval, the call that
  approval asks for (its permission row's title). Never as is: through
  :func:`safe_line` -- the outbound redaction over the WHOLE raw text first,
  then host paths and host names out, one line, cut to :data:`STEP_CHARS`.
  With no call in flight, the first line of a fresh progress report, through
  the same passes, or the fixed word :data:`STEP_THINKING`.

Never the acceptance bar, the conductor's decision, the event log, or any other
transcript text (no model output, no tool result, no arguments beyond the one
call line). Every :class:`WorkerRow` text field is one of the above.
"""

from __future__ import annotations

import bisect
import functools
import hashlib
import json
import os
import re
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field

#: Longest summary line the board shows.
SUMMARY_CHARS = 140
#: Longest title shown on a row.
TITLE_CHARS = 120
#: Longest goal line in the board header.
GOAL_CHARS = 200

#: What a pending approval asks for, by its ACP tool-call kind (``spawn`` is a
#: coordinator spawn approval). A CLOSED vocabulary: the board shows one of these
#: phrases or "a tool", never the approval's title, which is the transcript's
#: permission row and, for a shell call, its command line.
APPROVAL_WORDS: dict[str, str] = {
    "execute": "a command",
    "edit": "a file edit",
    "delete": "a file delete",
    "move": "a file move",
    "read": "a file read",
    "search": "a search",
    "fetch": "a web fetch",
    "spawn": "a sub-agent",
}


def approval_words(kind: str) -> str:
    """The fixed phrase for an approval of ACP *kind*; ``""`` (shown as "a tool") otherwise."""
    return APPROVAL_WORDS.get(str(kind or "").strip().lower(), "")


WORKING = "working"
IDLE = "idle"
WAITING_ON_YOU = "waiting_on_you"
BLOCKED = "blocked"
DONE = "done"
LOST = "lost"
CLOSED = "closed"

#: Every row state, most urgent first. Rows themselves stay in dispatch order, so
#: a pinned board never reshuffles under the reader's eye.
STATES: tuple[str, ...] = (WAITING_ON_YOU, BLOCKED, LOST, WORKING, IDLE, DONE, CLOSED)
#: Rows that are working or waiting: while one shows, the board is kept current
#: (the watcher's backstop and the publisher's stale-stamp edit).
ACTIVE_STATES = frozenset({WORKING, WAITING_ON_YOU})

STATE_LABELS = {
    WORKING: "working",
    IDLE: "idle",
    WAITING_ON_YOU: "waiting on you",
    BLOCKED: "blocked",
    DONE: "done",
    LOST: "lost",
    CLOSED: "closed",
}


def row_label(row: "WorkerRow") -> str:
    """The words for a row's state: a pending approval names its tool."""
    if row.waiting_on == "approval":
        return f"waiting on you: approve {row.approval_tool or 'a tool'}"
    return STATE_LABELS.get(row.state, row.state)


#: The board's overall state words.
OVERALL_DONE = "done"
OVERALL_NEEDS_YOU = "needs you"
OVERALL_WORKING = "working"
OVERALL_STUCK = "stuck"
OVERALL_IDLE = "idle"
OVERALL_EMPTY = "no helper agents yet"

#: Reasons that mean only a PERSON can move the item (``work_vocab``'s words).
_PERSON_REASONS = frozenset({"needs_human", "approval"})
_ASKING_STATUSES = frozenset({"blocked", "question"})

#: Liveness of a worker's session, joined server-side by :mod:`.source`.
LIVE_RUNNING = "running"
LIVE_QUEUED = "queued"
LIVE_IDLE = "idle"
LIVE_APPROVAL = "approval"
LIVE_GONE = "gone"
LIVE_UNBOUND = "unbound"

#: Channel-wide and user-group pings (the board's own copies: the wrap-up
#: defangs through the shared display sink, which keeps none of these).
_BROADCAST = re.compile(r"<!(channel|here|everyone)(?:\|[^>]*)?>")
_GROUP_PING = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")
_USER_PING = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_WS = re.compile(r"\s+")


def _redact(text: str) -> str:
    """The outbound redaction, scanned against what Slack will DISPLAY.

    ``redact_for_display`` also scans the text with emphasis, code and link
    markup rendered away, so a credential split by ``*...*`` cannot reassemble
    on screen; ``redact_via_context`` is the platform's egress redactor.
    """
    from kiro_crew.messaging.display_safety import redact_for_display
    from kiro_crew.platform.context import redact_via_context

    return redact_for_display(text, redact_via_context)[0]


def clean_text(text: str, limit: int) -> str:
    """Agent-authored text made safe for the channel: one line, no pings, redacted.

    The first line only; every Slack mention form defused (the wrap-up's own
    patterns for channel-wide and user-group pings, plus user mentions), so a
    worker's words never page anyone; whitespace folded; the outbound redaction;
    the cut to *limit* characters; and the redaction AGAIN, last, so a cut can
    never leave a credential the first pass would have caught whole.
    """
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    out = _BROADCAST.sub(r"@\1", first)
    out = _GROUP_PING.sub(lambda m: m.group(1) or "@group", out)
    out = _USER_PING.sub(r"@\1", out)
    out = _WS.sub(" ", out).strip()
    out = _redact(out)
    if len(out) > limit:
        at = limit - 1
        cut = out[:at]
        for start, end, unsure in _tokens(out):
            if start >= at:
                break
            if end > at:
                # The cut falls inside this token (classified WHOLE, from the full
                # text): one that holds a path or a quote is dropped, never shown
                # in part.
                token = out[start:end]
                if unsure or any(c in token for c in "/\\'\"") or _DRIVE_RELATIVE.search(token):
                    cut = out[:start]
                break
        out = cut.rstrip() + "…"
    return _redact(out)


@dataclass(frozen=True)
class Activity:
    """Raw facts about a worker's running turn (:func:`~.source.worker_activity`).

    Never published as is: :func:`step_text` reduces them to one safe line.
    """

    turn_started_at: str = ""
    project: str = ""
    tool_title: str = ""
    tool_kind: str = ""
    tool_input: str = ""
    tool_name: str = ""
    #: For a pending approval: the approval's own request id (never published;
    #: only hashed into the needs-you alert token, so each approval pages once).
    approval_id: str = ""


def approval_key(approval_id: str) -> str:
    """A short, stable digest of an approval's request id (``""`` when unknown)."""
    if not approval_id:
        return ""
    return hashlib.sha256(approval_id.encode("utf-8", "replace")).hexdigest()[:12]


#: Longest step line, cut with an ellipsis after the redaction.
STEP_CHARS = 70
#: The step when a turn runs with no tool call in flight and no fresh report.
STEP_THINKING = "thinking"
_INPUT_PATH_KEYS = ("path", "file_path", "filePath", "target_file", "file")
#: How much of a text is read for paths: a few lines' worth beyond its cut, so a
#: huge heredoc adds no token parsing or path rewriting. The account-name search
#: still reads past it (see :func:`_host_paths_out`). A path the bound cuts is
#: never shown in part.
_SCAN_FACTOR = 8
#: Home markers a published path may start with (either platform's).
_HOME_MARKERS = ("~", "%USERPROFILE%")
#: A segment of a relative path shown as written: nothing a shell, a URL or an
#: environment variable gives a meaning to (no ``$ : = % , ~``).
_PLAIN_SEGMENT = re.compile(r"[\w .@+-]+")
_SEPARATORS = re.compile(r"[\\/]+")
_DRIVE = re.compile(r"[A-Za-z]:")
_BARE_DRIVE = re.compile(r"[A-Za-z]:[\\/]*")
#: A drive-qualified name (``C:x``), at a token's start or after punctuation
#: (``--path=C:x``, ``!C:x``).
_DRIVE_RELATIVE = re.compile(r"(?:^|[^A-Za-z0-9])[A-Za-z]:[^\\/\s:]")
#: A backslash before white space: an escaped space or a line continuation.
_ESCAPED_SPACE = re.compile(r"\\(\r\n|\s)")
#: What ``str.splitlines`` breaks a line at, so a quote and a first line agree.
_LINE_BREAK = re.compile("[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
_HOST_RUN = re.compile(r"[\w.-]+")
_PUBLIC_HOST = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
#: Host names and addresses that say where the work runs: cloud-internal names
#: (``ip-10-0-0-1.ec2.internal``, ``db.internal.corp``), developer desktops
#: (``dev-dsk-…``) and private IPv4 addresses. Not every host name is
#: recognisable as one; public names (``github.com``) are left alone.
_INTERNAL_HOST = re.compile(
    # Anchored on a name START (not a word boundary, which sits at every ``a|-``
    # boundary and made a long ``a-a-a-…`` quadratic).
    r"(?<![\w.-])(?:ip-\d{1,3}(?:-\d{1,3}){3}(?:\.[\w-]+)*"
    r"|[\w-]+(?:\.[\w-]+)*\.(?:internal|local)(?:\.[\w-]+)*"
    r"|dev-dsk-[\w-]+(?:\.[\w-]+)*"
    r"|(?:10|127)(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2})(?![\w-])",
    re.IGNORECASE,  # a host name names the same machine in any case
)


def _input_field(raw: str, *keys: str) -> str:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _home_token(os_name: str | None = None) -> str:
    """How a home directory is shown: ``~`` on POSIX, ``%USERPROFILE%`` on Windows."""
    return "%USERPROFILE%" if (os_name or os.name) == "nt" else "~"


@dataclass(frozen=True)
class _Abs:
    """An absolute path, lexically normalised: no filesystem read, no expansion."""

    windows: bool
    anchor: str  # ``/``, ``//``, ``c:\``, ``\\server\share``, … (case-folded on Windows)
    segments: tuple[str, ...]  # as written (compare through :meth:`key`)
    names: tuple[str, ...]  # every named part, the UNC server and share included

    def key(self) -> tuple[str, tuple[str, ...]]:
        if self.windows:
            return self.anchor, tuple(s.casefold() for s in self.segments)
        return self.anchor, self.segments

    def under(self, root: _Abs) -> tuple[str, ...] | None:
        """The segments below *root*, or ``None`` when this path is not *root* or under it."""
        (anchor, segs), (root_anchor, root_segs) = self.key(), root.key()
        if self.windows != root.windows or anchor != root_anchor:
            return None
        if segs[: len(root_segs)] != root_segs:
            return None
        return self.segments[len(root_segs) :]


def _absolute(path: str) -> _Abs | None:
    """*path* as an absolute POSIX or Windows path (by its shape), else ``None``.

    Windows forms: a drive (``C:\\x``, ``C:/x``, drive-relative ``C:x``), UNC
    (``\\\\server\\share``), the device forms and a root on the current drive
    (``\\x``). ``posixpath`` / ``ntpath`` normalise lexically on any OS.
    """
    import ntpath
    import posixpath

    if path.startswith("/"):
        norm = posixpath.normpath(path)
        anchor = "//" if norm.startswith("//") and not norm.startswith("///") else "/"
        segs = tuple(s for s in norm.split(posixpath.sep) if s)
        return _Abs(False, anchor, segs, segs)
    if not (_DRIVE.match(path) or path.startswith("\\")):
        return None
    norm = ntpath.normpath(path)
    drive, rest = ntpath.splitdrive(norm)
    rooted = rest[:1] in ("\\", "/")
    anchor = (drive.replace("/", "\\") + ("\\" if rooted else "")).casefold()
    segs = tuple(s for s in _SEPARATORS.split(rest) if s)
    # The UNC server and share name a machine; a drive or a device prefix does not.
    lead = tuple(
        p for p in _SEPARATORS.split(drive) if p and p not in ("?", ".") and not _DRIVE.fullmatch(p)
    )
    return _Abs(True, anchor, segs, lead + segs)


@dataclass(frozen=True)
class _Scope:
    """What one scrub compares paths with: the homes, the project roots, host names."""

    homes: tuple[_Abs, ...]
    projects: tuple[_Abs, ...]
    accounts: frozenset[str]
    hosts: tuple[str, ...]
    #: Each word of each account name (``jane doe`` gives ``jane`` and ``doe``):
    #: one a space split off is never kept as a name either.
    account_words: frozenset[str] = frozenset()
    #: The whole account names, as text (any length, spaces and quotes included):
    #: one pattern per spelling.
    account_spans: tuple[re.Pattern[str], ...] = ()
    #: What a host name becomes: ``host``, unless that is itself a name kept out.
    host_mark: str = "host"


def _scope(project: str) -> _Scope:
    """The homes and project roots, lexically; ``realpath`` only for a native path."""

    def spellings(path: str) -> set[str]:
        out = {path} if path else set()
        if path and os.path.isabs(path):
            out.add(os.path.realpath(path))
        return out

    homes = tuple(
        p
        for p in (_absolute(h) for h in spellings(os.path.expanduser("~")))
        if p is not None and p.segments
    )
    # A project at or above home (``/home``) is never cut: the account under it
    # would be left behind as a relative path.
    projects = tuple(
        p
        for p in (_absolute(r) for r in spellings(project))
        if p is not None and p.segments and not any(h.under(p) is not None for h in homes)
    )
    accounts = frozenset(h.segments[-1].casefold() for h in homes)
    hosts = tuple(n.casefold() for n in _host_names())
    # Matched as written AND case-folded (``Straße`` / ``strasse``), any white
    # space between its words (the display folds it), with no word character
    # either side (``-name``, ``.name.`` are caught). One pattern per spelling,
    # so a shorter spelling never hides a longer one at the same start; a
    # spelling with no words (a blank home name) has nothing to match.
    spellings_of = {s for h in homes for s in (h.segments[-1], h.segments[-1].casefold())}
    spans = tuple(
        re.compile(
            r"(?<!\w)" + r"\s+".join(re.escape(w) for w in s.split()) + r"(?!\w)", re.IGNORECASE
        )
        for s in sorted(spellings_of)
        if s.split()
    )
    taken = any(h in "host" for h in hosts) or any(p.search("host") for p in spans)
    return _Scope(
        homes=homes,
        projects=projects,
        accounts=accounts,
        hosts=hosts,
        account_words=frozenset(w for a in accounts for w in a.split()),
        account_spans=spans,
        host_mark="…" if taken else "host",
    )


def _named(name: str, scope: _Scope) -> bool:
    """Whether *name* must not be shown as a path's name.

    An account (or a word of one), this machine's or a private host's name, or a
    drive-qualified name (``C:notes.txt``).
    """
    folded = name.casefold()
    bare = folded.strip(".-_~ ")
    return (
        bare in scope.accounts
        or bare in scope.account_words
        or any(p.search(name) for p in scope.account_spans)
        or any(h in folded for h in scope.hosts)
        or bool(_INTERNAL_HOST.search(name))
        or bool(_DRIVE_RELATIVE.search(name))
    )


def _last_name(names: tuple[str, ...] | list[str]) -> str:
    """The final name, split on BOTH separators (a POSIX name may hold ``\\``)."""
    for name in reversed(names):
        parts = [p for p in _SEPARATORS.split(name) if p]
        if parts:
            return parts[-1]
    return ""


def _marker_only(name: str) -> bool:
    """A name that is only dots or ellipses: never a safe name to show."""
    return not name.replace("…", "").replace(".", "")


def _shortened(names: tuple[str, ...] | list[str], scope: _Scope) -> str:
    """``…/<last>``, or ``…`` alone when there is no safe last name."""
    last = _last_name(names)
    if not last or _marker_only(last) or _named(last, scope):
        return "…"
    return "…/" + last


def _plain_and_unnamed(path: str, scope: _Scope) -> bool:
    """A plain relative path (:func:`_plain`) none of whose names is :func:`_named`."""
    return _plain(path) and not any(
        _named(p, scope) for p in re.split(r"[\\/]", path) if p and p != "."
    )


def _plain(path: str) -> bool:
    """A relative path shown as written: plain segments, no root, drive or ``..``.

    ``.`` is a plain segment; ``..`` and any other run of dots (``...``, a raw
    shortening marker) is not.
    """
    parts = re.split(r"[\\/]", path)
    if parts[-1] == "":
        parts = parts[:-1]  # ``src/``
    return bool(parts) and all(
        p and (p == "." or not _marker_only(p)) and _PLAIN_SEGMENT.fullmatch(p) for p in parts
    )


def _sep(path: str) -> str:
    found = re.findall(r"[\\/]", path)
    return found[-1] if found else "/"


def _below(marker: str, rest: tuple[str, ...], sep: str, scope: _Scope) -> str:
    """*rest* under a home marker: the marker alone, else ``marker/…/<last>``."""
    if not rest:
        return marker
    last = _last_name(rest)
    hidden = not last or _marker_only(last) or _named(last, scope)
    return marker + sep + "…" + ("" if hidden else sep + last)


def _public_url(url: str, scope: _Scope) -> bool:
    """An http(s) URL to a public host, with no user part and no Slack label: shown whole."""
    from urllib.parse import urlsplit

    if "|" in url:  # ``<url|label>``: the label is not part of the URL
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - a bad port raises ValueError
    except ValueError:
        return False
    return (
        parts.scheme.lower() in ("http", "https")
        and "@" not in parts.netloc
        and bool(_PUBLIC_HOST.fullmatch(host))
        and not _named(host, scope)
    )


def _scrub_path(path: str, scope: _Scope) -> str:
    """One complete path, made publishable (see :func:`_host_paths_out`)."""
    if re.match(r"https?://", path, re.IGNORECASE):
        return path if _public_url(path, scope) else _shortened(_SEPARATORS.split(path), scope)
    if path.startswith(("…", "...")):
        # A raw shortening marker: only its last name can be kept.
        return _shortened([s for s in _SEPARATORS.split(path.lstrip("….")) if s], scope)
    for marker in _HOME_MARKERS:
        head = path[: len(marker)]
        if head.casefold() == marker.casefold() and path[len(marker) : len(marker) + 1] in (
            "",
            "/",
            "\\",
        ):
            stack: list[str] = []
            for part in _SEPARATORS.split(path[len(marker) :]):
                if part in ("", "."):
                    continue
                if part != "..":
                    stack.append(part)
                elif stack:
                    stack.pop()
                else:
                    # Climbs out of home: where it lands is not home-relative.
                    return _shortened([p for p in _SEPARATORS.split(path) if p][-1:], scope)
            return _below(marker, tuple(stack), _sep(path), scope)
    found = _absolute(path)
    if found is None:
        return (
            path
            if _plain_and_unnamed(path, scope)
            else _shortened([s for s in _SEPARATORS.split(path) if s], scope)
        )
    if not found.segments and (not found.windows or _BARE_DRIVE.fullmatch(path)):
        return path  # ``/`` or a bare drive: no directory named
    sep = _sep(path)
    for root in scope.projects:
        rest = found.under(root)
        if rest is not None:
            relative = sep.join(rest) or "."
            keep = relative == "." or _plain_and_unnamed(relative, scope)
            return relative if keep else _shortened(rest, scope)
    for home in scope.homes:
        rest = found.under(home)
        if rest is not None:
            return _below(_home_token(), rest, sep, scope)
    return _shortened(found.names, scope)


def _tokens(text: str) -> Iterator[tuple[int, int, bool]]:
    """``(start, end, unsure)`` of each white-space-separated token.

    A quote opens wherever it does not follow a letter or digit (``"x``, ``("x``,
    ``=x``; not ``don't``) and runs to its match, holding spaces and the other
    quote. One with no match runs to the end of the text, and one whose match is
    on a later line spans those lines: both are *unsure*. A backslash before
    white space (``\\ ``, a line continuation) keeps the token going.
    """
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        start, unsure = i, False
        while i < n and not text[i].isspace():
            ch = text[i]
            if ch in "'\"" and (i == start or not text[i - 1].isalnum()):
                close = text.find(ch, i + 1)
                if close == -1:
                    i, unsure = n, True
                    break
                unsure = unsure or _LINE_BREAK.search(text, i + 1, close) is not None
                i = close + 1
            elif ch == "\\" and text[i + 1 : i + 2].isspace():
                i += 3 if text[i + 1 : i + 3] == "\r\n" else 2
            else:
                i += 1
        yield start, i, unsure


def _scrub_token(token: str, scope: _Scope) -> str:
    """A token holding a path: its wrapping quotes and edge punctuation kept."""
    lead = len(token) - len(token.lstrip("([{<"))
    core = token[lead:].rstrip(",;:!?)]}>")
    if core.endswith(".") and not core.endswith(("..", "/.", "\\.")) and len(core) > 1:
        core = core[:-1]
    tail = token[lead + len(core) :]
    if len(core) >= 2 and core[0] in "'\"" and core[-1] == core[0]:
        shown = core[0] + _scrub_path(core[1:-1], scope) + core[0]
    else:
        unescaped = _ESCAPED_SPACE.sub(r"\1", core)
        if "'" in unescaped or '"' in unescaped:
            names = [s for s in _SEPARATORS.split(re.sub(r"['\"]", "", unescaped)) if s]
            shown = _shortened(names, scope)
        else:
            shown = _scrub_path(unescaped, scope)
    return token[:lead] + shown + tail


def _hosts_out(text: str, scope: _Scope) -> str:
    """Every account name, and every name-shaped run naming a host kept out, replaced whole.

    An account is matched as written (any case, any length, spaces and quotes
    included) and becomes ``…``. A run holding this machine's name or a private
    host's becomes :attr:`_Scope.host_mark` WHOLE, so no name is rebuilt from what
    a partial replacement leaves (``test-test-host``, ``10.0.0.1.example``).
    """

    def one(match: re.Match[str]) -> str:
        run = match.group(0)
        folded = run.casefold()
        if any(h in folded for h in scope.hosts) or _INTERNAL_HOST.search(run):
            return scope.host_mark
        if folded.strip(".-_") in scope.accounts:
            return "…"
        return run

    # Host runs and account spans are both found on the SAME text, and overlapping
    # ones replaced as one range (``…``), so neither replacement splits the other
    # (``dev-u-box`` holding the account, an account ``Jane Doe`` holding a host).
    found: list[tuple[int, int, str]] = []
    for span in scope.account_spans:
        # Every match, overlapping ones too (``Jane Jane Jane`` for ``Jane Jane``,
        # ``Jane Doe Zee`` for ``Jane Doe`` and ``Doe Zee``): the next search starts
        # one character after the last match's START, not at its end, and never
        # past the end of the text.
        at = 0
        while at <= len(text) and (m := span.search(text, at)) is not None:
            if m.end() > m.start():
                found.append((m.start(), m.end(), "…"))
            at = m.start() + 1
    for m in _HOST_RUN.finditer(text):
        shown = one(m)
        if shown != m.group(0):
            found.append((m.start(), m.end(), shown))
    if not found:
        return text
    found.sort()
    merged: list[tuple[int, int, str]] = []
    for start, end, shown in found:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end), "…")
        else:
            merged.append((start, end, shown))
    out: list[str] = []
    pos = 0
    for start, end, shown in merged:
        out += [text[pos:start], shown]
        pos = end
    return "".join(out) + text[pos:]


def _host_paths_out(text: str, project: str, *, cut: bool = False, full: str | None = None) -> str:
    """Every path made publishable, whole token by whole token; host names out.

    A token holding ``/`` or ``\\``, or a drive-relative ``C:x``, is ONE path
    candidate (quoted, it may hold spaces). It is shown as one of: project-relative
    (lexically normalised, inside the project root, no root, drive or ``..``
    left); the home marker alone for home itself, ``~/…/<last>``
    (``%USERPROFILE%\\…\\<last>``) below it; a plain relative path or a public
    http(s) URL as written; else ``…/<last>``, or ``…`` when the last name is
    missing or names the account or this machine. A token the bound cut (*cut*:
    the text went on past it) or an unsure quote is ``…`` whole, and a plain
    relative path after a shortened one (the rest of a path
    with spaces) is shortened too. Nothing is read from the filesystem or
    expanded. Running it again on its own output changes nothing. *full* is the
    text before the bound cut it: an account name that starts in *text* and runs
    past the bound is found there, so none of it is left.
    """
    scope = _scope(project)
    out: list[str] = []
    pos = 0
    shortened_before = False  # a command's lines are folded into one: never reset
    tokens = list(_tokens(text))
    # An account name that spans several tokens (``Jane Doe Zee``) is found on the
    # ORIGINAL text, before any token is rewritten, and on the text past the scan
    # bound too (*full*): every token it touches goes, as one ``…``, so rewriting
    # or cutting one of them never leaves the others behind. Only a match that
    # STARTS in *text* is used, but each search may read on to the end of *full*
    # to find one, so its cost grows with the whole text, not only the bound.
    source = full if full is not None and full.startswith(text) else text
    starts = [s for s, _e, _u in tokens]
    hidden: dict[int, int] = {}  # first token index -> last token index
    for span in scope.account_spans:
        at = 0
        while at < len(text) and (m := span.search(source, at)) is not None:
            if m.start() >= len(text):
                break  # starts past the bound: none of it is in this text
            at = m.start() + 1
            crosses = m.end() > len(text)
            first = max(bisect.bisect_right(starts, m.start()) - 1, 0)
            last = bisect.bisect_left(starts, min(m.end(), len(text))) - 1
            if first <= last and (last > first or crosses) and tokens[first][1] > m.start():
                hidden[first] = max(hidden.get(first, first), last)
    skip_to = -1
    for index, (start, end, unsure) in enumerate(tokens):
        if index <= skip_to:
            pos = end
            continue
        if index in hidden:
            out.append(text[pos:start])
            out.append("…")
            skip_to = hidden[index]
            later = index + 1
            while later <= skip_to:  # a chain of overlapping names is one range
                skip_to = max(skip_to, hidden.get(later, later))
                later += 1
            pos = end
            shortened_before = True
            continue
        token = text[start:end]
        gap = text[pos:start]
        out.append(gap)
        if cut and end == len(text):
            # The bound cut this token: what it was (a path whose separator is
            # past the bound, say) cannot be told, so none of it is shown.
            token = "…"
        elif "/" in token or "\\" in token or _DRIVE_RELATIVE.search(token):
            if unsure:
                token = "…"
            else:
                shown = _scrub_token(token, scope)
                inner = token.strip("([{<)]}>,;:!?'\"")
                if shortened_before and shown == token and _plain(inner):
                    at = token.find(inner)
                    shortened = _shortened([s for s in _SEPARATORS.split(inner) if s], scope)
                    shown = token[:at] + shortened + token[at + len(inner) :]
                token = shown
            shortened_before = shortened_before or "…" in token
        out.append(token)
        pos = end
    out.append(text[pos:])
    return _hosts_out("".join(out), scope)


@functools.lru_cache(maxsize=1)
def _host_names() -> tuple[str, ...]:
    """This machine's own names, longest first (read once: ``getfqdn`` may hit DNS)."""
    names = set()
    for read in (socket.gethostname, socket.getfqdn):
        try:
            names.add(read())
        except OSError:
            continue
    return tuple(
        sorted((n for n in names if len(n) > 2 and n != "localhost"), key=len, reverse=True)
    )


def tool_step(activity: Activity) -> str:
    """The call in flight, worded the way the dashboard's live line words it (raw)."""
    title = re.sub(r"^[^\w@]+", "", activity.tool_title or "").strip()
    kind = (activity.tool_kind or "").lower()
    if kind == "execute":
        command = _input_field(activity.tool_input, "command", "cmd") or re.sub(
            r"^Running:?\s*", "", title
        )
        return f"Running {command}" if command else ""
    if kind == "edit":
        path = _input_field(activity.tool_input, *_INPUT_PATH_KEYS)
        return f"Editing {path}" if path else title
    if kind == "read":
        path = _input_field(activity.tool_input, *_INPUT_PATH_KEYS)
        return f"Reading {path}" if path else title
    if activity.tool_name:
        return activity.tool_name
    if title.startswith("@") and "/" in title:
        return title.rsplit("/", 1)[-1]
    return title


def safe_line(
    text: str, project: str = "", *, limit: int = STEP_CHARS, first_line: bool = False
) -> str:
    """Worker text made publishable as ONE line of at most *limit* characters.

    In this order: the outbound redaction over the WHOLE raw text (before anything
    rewrites it, so a pattern such as a URL's ``user:pw@`` still sees its ``://``),
    then the host paths and host names out (relative to the worker's worktree, ``~``,
    ``…/name``, ``host``), then ONE line -- the first only when *first_line* (a
    report's summary, a title, a goal), every line folded into it otherwise (a
    command) -- then :func:`clean_text`: pings defused, redacted again, cut,
    redacted once more, so a credential straddling the cut cannot leave half of
    itself behind. Mrkdwn is escaped where it is rendered.
    """
    if not text or not text.strip():
        return ""
    # The redaction sees the WHOLE text; only then is it bounded (a few lines'
    # worth beyond the cut), so a huge heredoc adds no token rewriting. The
    # account-name search still reads the whole redacted text (*full*).
    redacted = _redact(text)
    bound = limit * _SCAN_FACTOR
    scrubbed = _host_paths_out(redacted[:bound], project, cut=len(redacted) > bound, full=redacted)
    if first_line:
        scrubbed = scrubbed.strip().splitlines()[0] if scrubbed.strip() else ""
    return clean_text(" ".join(scrubbed.split()), limit)


def _path_step(activity: Activity) -> str:
    """A read or edit call's line, its path scrubbed as ONE whole value first.

    The raw path is redacted whole, then bounded: a path longer than the bound is
    shown as ``…``, never in part. Only then is the verb put in front.
    """
    kind = (activity.tool_kind or "").lower()
    path = _input_field(activity.tool_input, *_INPUT_PATH_KEYS) if kind in ("read", "edit") else ""
    if not path:
        return ""
    redacted = _redact(path).strip()
    if len(redacted) > STEP_CHARS * _SCAN_FACTOR or "\n" in redacted:
        shown = "…"
    else:
        scope = _scope(activity.project)
        shown = _hosts_out(_scrub_path(redacted, scope), scope)
    return f"{'Editing' if kind == 'edit' else 'Reading'} {shown}"


def step_text(activity: Activity, *, report: str = "") -> str:
    """The working row's step: the call in flight, else a fresh report, else thinking."""
    raw = _path_step(activity) or tool_step(activity)
    if raw:
        return safe_line(raw, activity.project) or STEP_THINKING
    if report:
        return safe_line(report, activity.project, first_line=True)
    return STEP_THINKING


@dataclass(frozen=True)
class WorkerRow:
    """One work item on the board, already reduced to the published set."""

    #: The ledger's server-minted item id (``it_<hex>``): a stable row key.
    item_id: str
    title: str
    state: str
    summary: str = ""
    branch: str = ""
    #: The worker's dashboard slot key, for the deep link (never shown as text).
    session_key: str = ""
    #: ``last_report_at`` of the report that put the row in its state.
    report_stamp: str = ""
    round: int = 0
    #: What a ``waiting_on_you`` row waits on: ``approval`` (a tool approval on
    #: the worker's session) or ``report`` (a report naming a person).
    waiting_on: str = ""
    #: For an ``approval`` wait, what it asks for: one :data:`APPROVAL_WORDS`
    #: phrase (``""`` when the kind is unknown), never the approval's own text.
    approval_tool: str = ""
    #: For a ``closed`` row, how the conductor closed it: ``rejected`` / ``abandoned``.
    closed_as: str = ""
    #: For a ``working`` row: what the turn is doing now (:func:`step_text`, safe).
    step: str = ""
    #: For a ``working`` row: when its turn started (ISO), for an elapsed time.
    turn_started_at: str = ""
    #: For an ``approval`` wait: a short digest of the pending approval's request
    #: id (never shown), so a second approval in the same report pages again.
    approval_key: str = ""

    @property
    def needs_owner(self) -> bool:
        return self.state == WAITING_ON_YOU

    @property
    def settled(self) -> bool:
        return self.state in {DONE, CLOSED}


#: Slack's limit on tasks in one plan block.
MAX_TASKS = 50
#: Rows the board message shows at most (Slack caps a message at 50 blocks). Totals always count every item.
MAX_SHOWN = 20
#: Settled items (accepted, rejected, abandoned) a snapshot keeps as rows: the
#: newest ones. Older settled items are only counted (``BoardSnapshot.dropped_*``),
#: so a long-lived conductor's history never grows the snapshot.
MAX_SETTLED = 2 * MAX_TASKS


@dataclass(frozen=True)
class BoardSnapshot:
    """The whole board for one channel: EVERY item, in dispatch order."""

    goal: str = ""
    round: int = 0
    rows: tuple[WorkerRow, ...] = field(default_factory=tuple)
    #: Settled items beyond :data:`MAX_SETTLED`, counted but not kept as rows.
    dropped_done: int = 0
    dropped_closed: int = 0

    @property
    def dropped(self) -> int:
        return self.dropped_done + self.dropped_closed

    @property
    def shown(self) -> tuple[WorkerRow, ...]:
        """The rows on display: every open row first (dispatch order), then the
        most recently dispatched settled ones, at most :data:`MAX_SHOWN`."""
        open_rows = [r for r in self.rows if not r.settled]
        settled = [r for r in self.rows if r.settled]
        room = max(0, MAX_SHOWN - len(open_rows))
        keep = set(id(r) for r in open_rows[:MAX_SHOWN])
        keep.update(id(r) for r in (settled[-room:] if room else []))
        return tuple(r for r in self.rows if id(r) in keep)

    @property
    def omitted(self) -> int:
        return self.total - len(self.shown)

    @property
    def plan_rows(self) -> tuple[WorkerRow, ...]:
        """The rows the plan block shows: all of them up to :data:`MAX_TASKS`, else the
        :data:`MAX_TASKS` - 1 most urgent (open first, then by state), in dispatch order."""
        if len(self.rows) <= MAX_TASKS:
            return self.rows
        # Open rows first, by state; settled ones newest first (the board keeps
        # the newest settled rows only, so the oldest are never the ones shown).
        urgent = sorted(
            enumerate(self.rows),
            key=lambda p: ((1, 0, -p[0]) if p[1].settled else (0, STATES.index(p[1].state), p[0])),
        )
        keep = {id(r) for _i, r in urgent[: MAX_TASKS - 1]}
        return tuple(r for r in self.rows if id(r) in keep)

    @property
    def total(self) -> int:
        return len(self.rows) + self.dropped

    @property
    def done(self) -> int:
        return self.counts[DONE]

    @property
    def counts(self) -> dict[str, int]:
        """Every item's state, the dropped settled ones included."""
        return _counts(self.rows, self.dropped_done, self.dropped_closed)

    @property
    def overall(self) -> str:
        return overall_state(self.rows, dropped=self.dropped)

    @property
    def aggregate(self) -> str:
        return aggregate_line(
            self.rows, dropped_done=self.dropped_done, dropped_closed=self.dropped_closed
        )


def row_state(
    *,
    item_state: str,
    status: str | None,
    reason: str | None,
    live: str,
    verdict: str | None = None,
    handed_back: bool = False,
) -> str:
    """The derived row state, from the item and its worker's live session.

    The order is the rule, most decisive first:

    1. a closed item: ``accepted`` is done, ``rejected`` / ``abandoned`` closed;
    2. a tool approval waiting on the worker's session: waiting on you;
    3. a turn in flight or queued: working (a fresh turn outranks an old report);
    4. a ``blocked`` / ``question`` report the conductor already answered (a
       ``decide`` at or after it) or a ``done`` the conductor's evaluator failed:
       the move is the worker's again, so the report's own word does not apply
       (idle, or lost when gone);
    5. a ``blocked`` / ``question`` report a person must move: waiting on you;
    6. any other ``blocked`` / ``question``: blocked (a service, or the conductor);
    7. a session that is gone: done when it last reported done, else lost;
    8. a ``done`` report: done (awaiting the conductor's acceptance);
    9. otherwise idle (open, quiet, or not dispatched yet).
    """
    if item_state == "accepted":
        return DONE
    if item_state in {"rejected", "abandoned"}:
        return CLOSED
    if live == LIVE_APPROVAL:
        return WAITING_ON_YOU
    if live in {LIVE_RUNNING, LIVE_QUEUED}:
        return WORKING
    if handed_back or (status == "done" and verdict == "fail"):
        return LOST if live == LIVE_GONE else IDLE
    if status in _ASKING_STATUSES:
        return WAITING_ON_YOU if reason in _PERSON_REASONS else BLOCKED
    if live == LIVE_GONE:
        return DONE if status == "done" else LOST
    if status == "done":
        return DONE
    return IDLE


def _counts(
    rows: tuple[WorkerRow, ...] | list[WorkerRow], dropped_done: int = 0, dropped_closed: int = 0
) -> dict[str, int]:
    out = dict.fromkeys(STATES, 0)
    for row in rows:
        out[row.state] = out.get(row.state, 0) + 1
    out[DONE] += dropped_done
    out[CLOSED] += dropped_closed
    return out


def overall_state(rows: tuple[WorkerRow, ...] | list[WorkerRow], *, dropped: int = 0) -> str:
    """One word for the whole board (*dropped*: settled items not kept as rows)."""
    if not rows and not dropped:
        return OVERALL_EMPTY
    states = {r.state for r in rows} | ({DONE} if dropped else set())
    if states <= {DONE, CLOSED}:
        return OVERALL_DONE
    if WAITING_ON_YOU in states:
        return OVERALL_NEEDS_YOU
    if WORKING in states:
        return OVERALL_WORKING
    if states & {BLOCKED, LOST}:
        return OVERALL_STUCK
    return OVERALL_IDLE


def aggregate_line(
    rows: tuple[WorkerRow, ...] | list[WorkerRow],
    *,
    dropped_done: int = 0,
    dropped_closed: int = 0,
) -> str:
    """``"2 working · 1 waiting on you · 1 of 4 done"``: the one-line status.

    Only non-zero counts are named, in urgency order after "working"; the done
    count always follows. Settled items not kept as rows count too. An empty
    board says so.
    """
    total = len(rows) + dropped_done + dropped_closed
    if not total:
        return "No helper agents yet"
    counts = _counts(rows, dropped_done, dropped_closed)
    parts: list[str] = []
    if counts[WORKING]:
        parts.append(f"{counts[WORKING]} working")
    for state in (WAITING_ON_YOU, BLOCKED, LOST, IDLE):
        if counts[state]:
            parts.append(f"{counts[state]} {STATE_LABELS[state]}")
    if counts[CLOSED]:
        parts.append(f"{counts[CLOSED]} closed")
    parts.append(f"{counts[DONE]} of {total} done")
    return " · ".join(parts)


#: ``work_aggregate`` states, worst first: the first that applies wins.
AGGREGATE_STATES: tuple[str, ...] = ("waiting", "stuck", "working", "done", "idle")


def work_aggregate(snapshot: BoardSnapshot | None) -> dict | None:
    """The conductor slot's ``work_aggregate`` field, or ``None`` with no workers.

    THE FIELD CONTRACT (read by the dashboard sidebar to collapse a conductor's
    workers under its row)::

        {"state": "waiting" | "stuck" | "working" | "done" | "idle",
         "label": "<the board's summary line>",
         "counts": {"working": n, "waiting": n, "stuck": n, "done": n, "total": n}}

    ``state`` is worst first (:data:`AGGREGATE_STATES`): ``waiting`` when any
    worker waits on you, ``stuck`` when any is blocked or lost, ``working`` when any
    runs, ``done`` when every item is settled (done or closed), else ``idle``.
    ``label`` is the board's own summary line, made only of counts and fixed words
    and passed through the board's redaction all the same.
    """
    if snapshot is None or not snapshot.total:
        return None
    rows = snapshot.rows
    every = snapshot.counts
    counts = {
        "working": every[WORKING],
        "waiting": every[WAITING_ON_YOU],
        "stuck": every[BLOCKED] + every[LOST],
        "done": every[DONE],
        "total": snapshot.total,
    }
    if counts["waiting"]:
        state = "waiting"
    elif counts["stuck"]:
        state = "stuck"
    elif counts["working"]:
        state = "working"
    elif all(r.settled for r in rows):
        state = "done"
    else:
        state = "idle"
    return {"state": state, "label": clean_text(snapshot.aggregate, 200), "counts": counts}
