"""Cron execution history store.

Persists run records as JSONL files per job, with a global index for
cross-job queries.  Uses fcntl advisory locking for cross-process safety.

Storage layout:
    ~/.kiro/crew/cron-history/
        {job_id}.jsonl      — full records (including trace) for one job
        _index.jsonl        — lightweight index (no trace) for list queries

History is BEST-EFFORT.  When the directory cannot be created or written the
store constructs anyway with ``enabled`` False: reads return empty, writes are
dropped, and nothing raises.  A runtime failure on a store that DID construct
degrades the same way (``_degrade``) rather than propagating, so the invariant
holds at the store itself instead of relying on every caller to wrap it.
Losing run records must never take scheduling down with it — see
``_prepare_dir`` for how usability is decided, and ``prepare()`` for why a
loop-bound caller must resolve that off the event loop.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import append_line, replace_with_retry
from kiro_crew.config import live
from kiro_crew.config.paths import config_dir
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.owner_only_files import owner_only_opener

logger = logging.getLogger(__name__)

#: Default summary cap. ``truncate_summary`` spends it on whole links first,
#: newest first, then on the outcome line. To keep a run's narration or every
#: link, raise ``cron_history.cron_summary_cap``.
_SUMMARY_CAP = 200
_TRACE_CAP = 50 * 1024  # 50KB
_MAX_RECORDS_PER_JOB = 100
_MAX_INDEX_RECORDS = 2000

#: Errnos that mean "this store is refused", not "this attempt failed". Only
#: these disable history for the process's lifetime — see ``_degrade``.
_DENIAL_ERRNOS = frozenset({errno.EPERM, errno.EACCES, errno.EROFS})


def _sandbox_confined() -> bool:
    """True when this process shows evidence of agent-shell confinement.

    ``cli.main`` pops ``KIROCREW_SANDBOX_ACTIVE`` before any subcommand runs, so
    the launch mark it noted first is asked as well as the live probe. Used
    only to pick a log level, never to grant anything.
    """
    from kiro_crew import sandbox_launch_mark
    from kiro_crew.sandbox_unavailable import agent_confinement_evidence

    if sandbox_launch_mark.launched_in_sandbox():
        return True
    try:
        return agent_confinement_evidence() is not None
    except Exception:  # a failed probe keeps the WARNING
        return False


#: A URL run, stopped only at whitespace and at the characters that cannot
#: appear in one. Which trailing characters actually belong to the address is
#: decided by ``_trim_url``, because a bracket can be either. The scheme is
#: case-insensitive per RFC 3986, so ``HTTPS://…`` is still a link, not prose.
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)

#: Trailing characters that close a sentence rather than an address. Only these
#: two: real addresses can end in ``!`` (``…/wiki/Yahoo!``), ``?``, ``:`` or
#: ``;``, and trimming those turns a live link into a dead one.
_SENTENCE_ENDERS = ".,"

#: Closing bracket -> its opener, for deciding whether the bracket is the URL's
#: own or the prose's.
_CLOSERS = {")": "(", "]": "["}

#: Marks the removed middle. One marker, however much was removed.
_CUT_MARKER = "..."


def truncate_summary(text: str, cap: int) -> str:
    """Cut ``text`` to ``cap`` characters without dropping what makes it findable.

    The summary is the only short, indexed record of a cron run (the trace is
    not in the index), so it is what a later session searches. A blind
    ``text[:cap]`` drops the end, which is where a run states its outcome and
    the URL of what it produced.

    What survives, in this order:

    1. as much of the head as still fits, cut BEFORE a URL rather than through
       one;
    2. one ``...`` marker standing for everything removed, dropped only when
       charging it would leave the result with no URL at all;
    3. as many URLs as the remaining budget holds, newest first and each one
       whole — half a link is not findable, so a URL is kept entire or not at
       all, with only the prose around it trimmed off;
    4. the end of the final non-blank line, where a run states how it finished.

    A URL is written once: the head stops before any copy the kept URLs or the
    outcome fragment already carry. Input at or under the cap is returned
    unchanged and the result never exceeds the cap, so the function is a fixed
    point on its own output.
    """
    if cap <= len(_CUT_MARKER):
        # Nothing structured fits in a budget this small; the cap still holds.
        return text[: max(cap, 0)]
    if len(text) <= cap:
        return text

    # URLs claim room before the outcome fragment: sized the other way, prose
    # can take half the cap and drop a link that would have fitted.
    candidates = _newest_first(text)
    links, used = _fit_links(candidates, cap - len(_CUT_MARKER) - 1)

    # The marker is charged first, except when it would leave no link at all:
    # a URL that fits the cap on its own is worth more than the marker.
    marked = True
    if not links:
        wider, wider_used = _fit_links(candidates, cap)
        if wider:
            links, used, marked = wider, wider_used, False

    # The end of the final non-blank line gets what the links left, and never
    # more than half the cap, so a single-line summary cannot spend the whole
    # budget on its own tail.
    spent = used + (len(_CUT_MARKER) + 1 if marked else 0)
    tail = _outcome_tail(text, min(cap // 2, cap - spent - (1 if links else 0)))

    # A link the fragment already carries needs no line of its own. Compare
    # URLs, not substrings: ``…/pull/28`` is a prefix of ``…/pull/2836``.
    if tail:
        tail_urls = set(_unique_urls(tail))
        links = [u for u in links if u not in tail_urls]

    body = "\n".join([*links, tail] if tail else links)
    # One newline for the marker's own line, one for the body's first line.
    marker_cost = len(_CUT_MARKER) + (1 if body else 0) if marked else 0
    head_room = cap - len(body) - marker_cost - 1
    head = _head_before(text, head_room).rstrip() if head_room > 0 else ""

    # The body holds every URL, so the head stops at the first one it reaches.
    # A URL whole inside the head is always in the body too: the head budget is
    # the link budget minus one.
    if head:
        head_urls = _url_spans(head)
        if head_urls:
            head = head[: head_urls[0][0]].rstrip()

    # The marker gets its own line so it never fuses onto the end of a kept
    # URL — "<url>..." reads as a longer, dead link.
    parts = ([head] if head else []) + ([_CUT_MARKER] if marked else []) + (
        [body] if body else []
    )
    return "\n".join(parts)


def _fit_links(candidates: list[str], budget: int) -> tuple[list[str], int]:
    """As many whole URLs as ``budget`` holds, in the order given, plus the spend.

    A separator is charged only when something already precedes the link, so a
    URL that exactly fills the budget is kept.
    """
    kept: list[str] = []
    used = 0
    for url in candidates:
        need = len(url) + (1 if kept else 0)
        if used + need <= budget:
            kept.insert(0, url)
            used += need
    return kept, used


def _url_spans(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, url)`` for every URL in ``text``, prose trimmed off.

    The positions are what lets a caller compare URLS rather than TEXT: a link
    whose whole address is a prefix of another's is a distinct URL, and only a
    span tells them apart.
    """
    spans: list[tuple[int, int, str]] = []
    for match in _URL_RE.finditer(text):
        # Never empty: the pattern needs a scheme plus one character, and
        # _trim_url stops at the scheme, so the shortest survivor is "http://a".
        url = _trim_url(match.group(0))
        spans.append((match.start(), match.start() + len(url), url))
    return spans


def _unique_urls(text: str) -> list[str]:
    """URLs in order of appearance, first occurrence only, prose trimmed off."""
    seen: set[str] = set()
    out: list[str] = []
    for _start, _end, url in _url_spans(text):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _newest_first(text: str) -> list[str]:
    """Unique URLs, the one appearing LATEST first.

    A URL's freshness is where it appears LAST: a link named in the opening
    sentence and again in the closing line is the run's newest fact, and ranking
    it by the opening mention gives its room away.
    """
    last_seen: dict[str, int] = {}
    for start, _end, url in _url_spans(text):
        last_seen[url] = start
    return sorted(last_seen, key=lambda url: last_seen[url], reverse=True)


def _trim_url(url: str) -> str:
    """Drop the trailing characters that close the prose, not the address.

    A link written mid-sentence collects the full stop after it, and a link in
    parentheses collects the closing one. A bracket is only prose when it is
    UNMATCHED inside the run: ``.../Foo_(bar)`` keeps its bracket, while
    ``(https://host/p)`` loses it.

    Only a trailing run is considered, so ``…/a.b,c:d/`` survives whole, and
    only ``.`` and ``,`` count as prose (see ``_SENTENCE_ENDERS``). The scan
    stops at the scheme, so a bare ``http://a`` is returned as it stands.
    """
    while len(url) > len("https://"):
        last = url[-1]
        if last in _SENTENCE_ENDERS:
            url = url[:-1]
            continue
        opener = _CLOSERS.get(last)
        if opener is not None and url.count(opener) < url.count(last):
            url = url[:-1]
            continue
        break
    return url


def _outcome_tail(text: str, limit: int) -> str:
    """End of the final non-blank line, at most ``limit`` characters.

    Clipped from the LEFT, so the line's conclusion survives, and never clipped
    into the middle of a URL: a partial link is noise, and the whole one is
    restated by ``truncate_summary`` anyway.
    """
    if limit <= 0:
        return ""
    line = ""
    for candidate in reversed(text.splitlines()):
        if candidate.strip():
            line = candidate.strip()
            break
    if len(line) <= limit:
        return line
    start = len(line) - limit
    for match in _URL_RE.finditer(line):
        if match.start() < start < match.end():
            start = match.end()
            break
    return line[start:]


def _head_before(text: str, limit: int) -> str:
    """Opening of ``text``, at most ``limit`` characters, never splitting a URL.

    The spans are measured on the WHOLE text, not on the slice: a slice can end
    inside ``https://`` itself, where the pattern has nothing to match yet, and
    a scan of the slice would report no URL and leave a bare ``a http`` behind.
    """
    end = limit
    for match in _URL_RE.finditer(text):
        if match.start() >= end:
            break
        if match.start() < end < match.end():
            end = match.start()
            break
    return text[:end]


def _trim_file(path: Path, max_records: int) -> None:
    """Keep only the newest *max_records* lines of the JSONL at *path*.

    Writes a temp file then replaces atomically, so a lock-free reader sees the
    whole old file or the whole new one. A no-op at or below the cap.

    :func:`replace_with_retry` rides out the Windows sharing violation
    (``EACCES``) a reader holding the file open causes; otherwise ``_degrade``
    would disable history over a transient overlap.

    The caller MUST hold the store lock, or an append between the read and the
    replace is lost.
    """
    if max_records <= 0 or not path.exists():
        return
    lines = _raw_lines(path)
    if len(lines) <= max_records:
        return
    tmp = path.with_suffix(".tmp")
    wfd = os.open(str(tmp), _REWRITE_FLAGS, 0o600)
    with os.fdopen(wfd, "wb") as f:
        f.write(_NEWLINE.join(lines[-max_records:]) + _NEWLINE)
    replace_with_retry(tmp, path)


#: The line ending a rewrite uses: what a text-mode write of ``"\n"`` produced,
#: and what ``append_line`` writes, so a rewritten file keeps one ending.
_NEWLINE = os.linesep.encode("ascii")

#: ``O_BINARY`` as ``append_line`` opens: without it a Windows fd is text mode,
#: and the CRT would turn ``_NEWLINE``'s ``\n`` into a second ``\r\n``.
_REWRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)


def _raw_lines(path: Path) -> list[bytes]:
    """*path*'s JSONL lines as bytes, never decoded as a whole.

    A byte that is not UTF-8 (a torn or foreign write) must cost its own line,
    not the file: decoding the whole file raises ``UnicodeDecodeError``, which is
    not an ``OSError``, so it escapes every reader and the append that trims.
    Rewrites carry the kept lines byte for byte. ``json.dumps`` escapes every
    character ``str.splitlines`` would split on beyond ``\\n``, so splitting the
    bytes yields the same records.
    """
    return path.read_bytes().strip().splitlines()


# The text fields the dashboard redacts with string scanners before serving a run.
_SERVED_TEXT_FIELDS = ("summary", "error", "trace")


def _served_row(line: bytes) -> dict[str, Any] | None:
    """*line* as a run the history routes can serve, or ``None`` to skip it.

    Read paths only. A row whose ``summary``, ``error`` or ``trace`` is present
    but not a string (a hand edit, a foreign writer) is skipped like a line
    that is not JSON: the routes redact those fields with string scanners, so
    one such row turned the whole history page into a 500. The rewrite paths
    keep parsing with :func:`parse_json_object_line`, so no such row is dropped
    from disk.
    """
    row = parse_json_object_line(line)
    if row is None:
        return None
    for name in _SERVED_TEXT_FIELDS:
        value = row.get(name)
        if value is not None and not isinstance(value, str):
            return None
    return row


@dataclass
class CronRunRecord:
    """Single cron execution record."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    job_id: str = ""
    trigger: str = "scheduled"  # "scheduled" | "manual"
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: int = 0
    status: str = "success"  # "success" | "failure" | "timeout" | "cancelled"
    summary: str = ""
    trace: str = ""
    error: str = ""

    def to_dict(self, include_trace: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not include_trace:
            d.pop("trace", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CronRunRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class CronHistoryStore:
    """JSONL-per-job execution history with global index."""

    def __init__(
        self,
        base_dir: Path | None = None,
        cron_summary_cap: int = _SUMMARY_CAP,
        cron_trace_cap_kb: int = _TRACE_CAP // 1024,
        cron_max_records_per_job: int = _MAX_RECORDS_PER_JOB,
        cron_max_index_records: int = _MAX_INDEX_RECORDS,
        *,
        _defer_prepare: bool = False,
    ):
        self._dir = (base_dir or config_dir()) / "cron-history"
        self._index_path = self._dir / "_index.jsonl"
        self._summary_cap = cron_summary_cap
        self._trace_cap = cron_trace_cap_kb * 1024
        self._max_records_per_job = cron_max_records_per_job
        self._max_index_records = cron_max_index_records
        # Directory setup does synchronous filesystem I/O, so a caller on an
        # event loop MUST defer it and run prepare() in a worker thread — see
        # prepare() and CronService.create(). Deferred starts DISABLED so a read
        # racing the prepare degrades rather than touching an unprepared store.
        self._prepared = False
        self._enabled = False
        if not _defer_prepare:
            self.prepare()
        # The caps above are copies of cron_history.*, so a config write reaches
        # them only through reconfigure(). Held on self because the watcher holds
        # the owner weakly.
        self._config_sub = live.watch_object(self, "cron_history", name="CronHistoryStore")

    def reconfigure(self, cfg: object) -> None:
        """Adopt new ``cron_history.*`` caps.

        The caps only bound what the NEXT record write stores and what the next
        trim keeps, so there is nothing to migrate: records already on disk keep
        the shape they were written with, and the next trim applies the new
        retention. ``cron_trace_cap_kb`` is re-multiplied here rather than copied,
        because the attribute is in bytes.
        """
        cron_history_cfg = getattr(cfg, "cron_history")
        self._summary_cap = int(getattr(cron_history_cfg, "cron_summary_cap"))
        self._trace_cap = int(getattr(cron_history_cfg, "cron_trace_cap_kb")) * 1024
        self._max_records_per_job = int(getattr(cron_history_cfg, "cron_max_records_per_job"))
        self._max_index_records = int(getattr(cron_history_cfg, "cron_max_index_records"))

    @property
    def enabled(self) -> bool:
        """False when the history directory is unusable, or not yet prepared.

        Every read returns empty and every write is dropped, so a caller never
        has to branch on it: history degrades, scheduling does not.
        """
        return self._enabled

    def prepare(self) -> None:
        """Resolve whether history is usable. Blocking; idempotent.

        Off the event loop only. ``CronService.create()`` runs this via
        ``asyncio.to_thread``, mirroring how it defers ``_load()``.
        """
        if self._prepared:
            return
        self._prepared = True
        self._enabled = self._prepare_dir()

    def _prepare_dir(self) -> bool:
        """Ensure the history directory is usable. Never raises.

        Returns True when records can be persisted, False to disable history.
        """
        try:
            from kiro_crew.owner_only_files import ensure_directory

            ensure_directory(self._dir)  # run records: 0700 in the data home
            return True
        except OSError as exc:
            # A denial scoped to this one leaf answers EPERM to BOTH os.mkdir
            # and the os.stat behind Path.is_dir(), and pathlib consults
            # is_dir() to decide whether exist_ok applies — so mkdir raises
            # even though the directory is already there. A failed mkdir
            # therefore does not by itself mean the store is unusable.
            if self._probe_usable():
                logger.debug(
                    "cron history: mkdir refused for existing %s (%s); the "
                    "store's own syscalls work, continuing",
                    self._dir,
                    exc,
                )
                return True
            if self._dir_masked():
                logger.debug(
                    "cron history not available in this sandbox: %s is masked "
                    "(%s); the gateway records runs",
                    self._dir,
                    exc,
                )
                return False
            logger.warning(
                "cron history disabled: %s is unusable (%s). Scheduling is "
                "unaffected; run records will not be persisted.",
                self._dir,
                exc,
            )
            return False

    def _dir_masked(self) -> bool:
        """True when the directory exists but this process may not look at it.

        That is the signature of an agent sandbox masking the leaf: this process
        shows evidence of agent confinement, the parent lists the name, and stat
        on the leaf answers a denial. The gateway runs unsandboxed and records
        runs, so a disabled store here is expected, not a fault. The mkdir errno
        is not used: it is EEXIST or EPERM depending on which mkdir route ran
        and on the Python version.

        Every other shape keeps the WARNING: an unconfined process such as the
        gateway (whatever the denial), a regular file at the path (stat works),
        a refused parent (the listing fails), a missing directory (the name is
        not listed). A check that cannot run answers False, which is the old,
        loud behaviour. The answer only picks a log level; it grants nothing.
        """
        if not _sandbox_confined():
            return False
        try:
            os.stat(self._dir)
        except OSError as stat_exc:
            if stat_exc.errno not in _DENIAL_ERRNOS:
                return False
        else:
            return False
        try:
            return self._dir.name in os.listdir(self._dir.parent)
        except OSError:
            return False

    def _probe_usable(self) -> bool:
        """True when the syscalls this store's own paths depend on all work.

        Opening the lock file alone is not enough: the read and rotate paths
        call ``Path.exists()``, and pathlib re-raises EPERM from it rather than
        returning False, which would raise out of ``CronService.start()``. So
        stat the directory and open the lock file, both O(1). A directory scan
        is left out: its cost grows with the job count, and ``_degrade()``
        already handles a scan failure at runtime.
        """
        try:
            os.stat(self._dir)
            fd = platform_compat.open_create_or_existing(str(self._lock_path()), os.O_WRONLY, 0o600)
        except OSError:
            return False
        os.close(fd)
        return True

    def _degrade(self, operation: str, exc: OSError) -> None:
        """Handle a runtime failure. Never raises.

        Only a DENIAL disables the store: it holds for the process's whole life,
        so retrying only logs the same error per run. Every other ``OSError``
        (full disk, fd exhaustion, transient I/O) costs ONE record and the store
        stays enabled, because those clear on their own.
        """
        if exc.errno in _DENIAL_ERRNOS:
            if self._enabled:
                self._enabled = False
                logger.warning(
                    "cron history disabled: %s was denied on %s (%s). Scheduling "
                    "is unaffected; run records will no longer be persisted.",
                    operation,
                    self._dir,
                    exc,
                )
            return
        logger.warning(
            "cron history: %s failed on %s (%s); dropping this record and "
            "staying enabled. Scheduling is unaffected.",
            operation,
            self._dir,
            exc,
        )

    def _job_path(self, job_id: str) -> Path:
        path = (self._dir / f"{job_id}.jsonl").resolve()
        if path.parent != self._dir.resolve():
            raise ValueError(f"Path traversal blocked: {job_id!r}")
        return path

    def _lock_path(self) -> Path:
        return self._dir / ".history.lock"

    def _lock(self) -> int:
        """Acquire advisory lock, return fd.

        The caller owns the returned fd and closes it via ``_unlock``. When the
        acquire itself raises (an ``OSError`` on lock timeout), no fd has been
        handed back for the caller to close, so close it here before re-raising
        — otherwise the open handle lingers and on Windows blocks deleting the
        lock file or the history folder.
        """
        fd = platform_compat.open_create_or_existing(str(self._lock_path()), os.O_WRONLY, 0o600)
        try:
            platform_compat.acquire_lock(fd, exclusive=True)
        except OSError:
            os.close(fd)
            raise
        return fd

    def _unlock(self, fd: int) -> None:
        platform_compat.release_lock(fd)
        os.close(fd)

    async def append(self, record: CronRunRecord) -> None:
        """Write record to job file and index."""
        # The ONE truncation site for a summary: callers hand the text over
        # uncut, so the configured cap and the keep-the-links rule apply here.
        # A command cron's failure summary comes from ``job.last_error``, which
        # slack/gateway_runtime/cron_exec.py already caps at 200 for crons.json.
        record.summary = truncate_summary(record.summary, self._summary_cap)
        if len(record.trace) > self._trace_cap:
            record.trace = record.trace[:self._trace_cap] + "\n...[truncated]"

        if not self._enabled:
            return
        try:
            await asyncio.to_thread(self._append_sync, record)
        except OSError as exc:
            self._degrade("append", exc)

    def _append_sync(self, record: CronRunRecord) -> None:
        fd = self._lock()
        try:
            job_path = self._job_path(record.job_id)
            append_line(
                job_path, json.dumps(record.to_dict(include_trace=True)), opener=owner_only_opener
            )
            append_line(
                self._index_path,
                json.dumps(record.to_dict(include_trace=False)),
                opener=owner_only_opener,
            )
            # Trim on every write: ``rotate_all`` runs once at start, so it never
            # bounds what a long-lived gateway writes, and every read loads its
            # whole file.
            #
            # A job id of ``_index`` resolves onto ``_index.jsonl``; skip the
            # per-job trim there so the smaller cap never truncates the index.
            if job_path != self._index_path:
                _trim_file(job_path, self._max_records_per_job)
            _trim_file(self._index_path, self._max_index_records)
        finally:
            self._unlock(fd)

    async def get_job_history(
        self, job_id: str, offset: int = 0, limit: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (records_without_trace, total_count) for a job, newest first."""
        if not self._enabled:
            return [], 0
        try:
            return await asyncio.to_thread(self._get_job_history_sync, job_id, offset, limit)
        except OSError as exc:
            self._degrade("get_job_history", exc)
            return [], 0

    # Reads are lock-free (eventually-consistent): a concurrent append may
    # produce a partial final line, silently skipped by the JSONDecodeError handler.
    def _get_job_history_sync(
        self, job_id: str, offset: int, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        job_path = self._job_path(job_id)
        if not job_path.exists():
            return [], 0
        try:
            lines = _raw_lines(job_path)
        except FileNotFoundError:
            return [], 0
        records = [row for line in lines if (row := _served_row(line)) is not None]
        total = len(records)
        records.reverse()
        results = records[offset : offset + limit]
        for row in results:
            row.pop("trace", None)
        return results, total

    async def get_all_history(
        self, offset: int = 0, limit: int = 20, job_id: str | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """Return records from global index, newest first, optionally filtered."""
        if not self._enabled:
            return [], 0
        try:
            return await asyncio.to_thread(self._get_all_history_sync, offset, limit, job_id)
        except OSError as exc:
            self._degrade("get_all_history", exc)
            return [], 0

    # Reads are lock-free (eventually-consistent): a concurrent append may
    # produce a partial final line, silently skipped by the JSONDecodeError handler.
    def _get_all_history_sync(
        self, offset: int, limit: int, job_id: str | None
    ) -> tuple[list[dict[str, Any]], int]:
        if not self._index_path.exists():
            return [], 0
        try:
            lines = _raw_lines(self._index_path)
        except FileNotFoundError:
            return [], 0
        records = [row for line in lines if (row := _served_row(line)) is not None]
        if job_id:
            records = [row for row in records if row.get("job_id") == job_id]
        total = len(records)
        records.reverse()
        return records[offset : offset + limit], total

    async def get_run_detail(self, job_id: str, run_id: str) -> dict[str, Any] | None:
        """Return full record (with trace) for a specific run."""
        if not self._enabled:
            return None
        try:
            return await asyncio.to_thread(self._get_run_detail_sync, job_id, run_id)
        except OSError as exc:
            self._degrade("get_run_detail", exc)
            return None

    def _get_run_detail_sync(self, job_id: str, run_id: str) -> dict[str, Any] | None:
        job_path = self._job_path(job_id)
        if not job_path.exists():
            return None
        try:
            lines = _raw_lines(job_path)
        except FileNotFoundError:
            return None
        for line in lines:
            row = _served_row(line)
            if row is not None and row.get("run_id") == run_id:
                return row
        return None

    async def rotate(self, job_id: str) -> None:
        """Trim job file to last _MAX_RECORDS_PER_JOB records."""
        if not self._enabled:
            return
        try:
            await asyncio.to_thread(self._rotate_sync, job_id)
        except OSError as exc:
            self._degrade("rotate", exc)

    def _rotate_sync(self, job_id: str) -> None:
        job_path = self._job_path(job_id)
        if not job_path.exists():
            return
        fd = self._lock()
        try:
            _trim_file(job_path, self._max_records_per_job)
        finally:
            self._unlock(fd)

    async def rotate_all(self) -> None:
        """Rotate all job files and trim the global index."""
        if not self._enabled:
            return
        try:
            await asyncio.to_thread(self._rotate_all_sync)
        except OSError as exc:
            self._degrade("rotate_all", exc)

    def _rotate_all_sync(self) -> None:
        fd = self._lock()
        try:
            for p in self._dir.glob("*.jsonl"):
                if p.name == "_index.jsonl":
                    continue
                _trim_file(p, self._max_records_per_job)
            _trim_file(self._index_path, self._max_index_records)
        finally:
            self._unlock(fd)

    async def delete_job_history(self, job_id: str) -> bool:
        """Remove all history for a job."""
        if not self._enabled:
            return False
        try:
            return await asyncio.to_thread(self._delete_job_history_sync, job_id)
        except OSError as exc:
            self._degrade("delete_job_history", exc)
            return False

    def delete_job_history_sync(self, job_id: str) -> bool:
        """Synchronous :meth:`delete_job_history`, for a loop-less caller.

        Blocking filesystem I/O under the history lock, so it never runs on an
        event loop; there, await :meth:`delete_job_history`.
        """
        if not self._enabled:
            return False
        try:
            return self._delete_job_history_sync(job_id)
        except OSError as exc:
            self._degrade("delete_job_history", exc)
            return False

    def _delete_job_history_sync(self, job_id: str) -> bool:
        fd = self._lock()
        try:
            job_path = self._job_path(job_id)
            removed = job_path.exists()
            if removed:
                job_path.unlink()
            if self._index_path.exists():
                lines = _raw_lines(self._index_path)
                filtered = []
                for line in lines:
                    row = parse_json_object_line(line)
                    if row is not None and row.get("job_id") != job_id:
                        filtered.append(line)
                if len(filtered) != len(lines):
                    tmp = self._index_path.with_suffix(".tmp")
                    content = _NEWLINE.join(filtered) + _NEWLINE if filtered else b""
                    wfd = os.open(str(tmp), _REWRITE_FLAGS, 0o600)
                    with os.fdopen(wfd, "wb") as f:
                        f.write(content)
                    replace_with_retry(tmp, self._index_path)
            return removed
        finally:
            self._unlock(fd)
