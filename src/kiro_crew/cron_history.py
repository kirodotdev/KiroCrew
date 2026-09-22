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
from kiro_crew.config import live
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: Default summary cap. What it decides is ROOM: ``truncate_summary`` spends that
#: room on links before prose, newest first, so the default holds a summary's
#: outcome line plus as many whole links as the budget allows — one link always,
#: several only while the budget lasts. An operator who wants a run's NARRATION
#: kept, or every link of a run that produced many, raises
#: ``cron_history.cron_summary_cap`` — see the cap tests for what that buys.
_SUMMARY_CAP = 200
_TRACE_CAP = 50 * 1024  # 50KB
_MAX_RECORDS_PER_JOB = 100
_MAX_INDEX_RECORDS = 2000

#: Errnos that mean "this store is refused", not "this attempt failed". Only
#: these disable history for the process's lifetime — see ``_degrade``.
_DENIAL_ERRNOS = frozenset({errno.EPERM, errno.EACCES, errno.EROFS})


#: A URL run, stopped only at whitespace and at the characters that cannot
#: appear in one. Which trailing characters actually belong to the address is
#: decided by ``_trim_url``, because a bracket can be either. The scheme is
#: matched case-insensitively: a scheme is case-insensitive per RFC 3986, and a
#: summary that shouts ``HTTPS://…`` otherwise holds a link this treats as prose
#: and is free to cut in half.
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)

#: Trailing characters that close a sentence rather than an address. Only these
#: two: real addresses end in ``!`` (``…/wiki/Yahoo!``), in ``?`` (an empty
#: query), and in ``:`` or ``;`` inside a path segment, so trimming those
#: rewrites a live link into a dead one — the opposite of the point.
_SENTENCE_ENDERS = ".,"

#: Closing bracket -> its opener, for deciding whether the bracket is the URL's
#: own or the prose's.
_CLOSERS = {")": "(", "]": "["}

#: Marks the removed middle. One marker, however much was removed.
_CUT_MARKER = "..."


def truncate_summary(text: str, cap: int) -> str:
    """Cut ``text`` to ``cap`` characters without dropping what makes it findable.

    The summary is the only SHORT, INDEXED record of a cron run — the trace is
    capped separately (50 KB) and is not in the index — so the summary is what a
    later session searches. A blind ``text[:cap]`` keeps a summary's opening and
    throws away its conclusion, which is exactly where the outcome and the URL of
    whatever the run produced both sit: a run that opens a pull request records
    the link at the end, so a cut that drops the end leaves nothing findable and
    the next session re-derives the same pull request.

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
    outcome fragment already carry, so one fact never spends the budget twice.
    Input at or under the cap is returned unchanged, and the result never
    exceeds the cap, which makes the function a fixed point: applying it to its
    own output returns that output. When the URLs alone cannot fit, the ones
    that do are kept newest first, since the link a run has just produced is
    the one a later run needs.
    """
    if cap <= len(_CUT_MARKER):
        # Nothing structured fits in a budget this small; the cap still holds.
        return text[: max(cap, 0)]
    if len(text) <= cap:
        return text

    # URLs claim room FIRST, newest first, whole or not at all. Sizing the
    # outcome fragment before them lets half the cap go to prose and drop a link
    # that would have fitted, and a link is the fact the fragment cannot replace.
    #
    # "Newest" is a URL's LAST appearance, not its first: a link mentioned early
    # and again in the closing line is the run's freshest fact, and ranking it by
    # the early mention hands its room to a link the run cared about less.
    #
    # A separator is charged only when something already precedes the link, so a
    # URL that exactly fills the remaining room is kept rather than dropped for a
    # newline it does not need.
    candidates = _newest_first(text)
    links, used = _fit_links(candidates, cap - len(_CUT_MARKER) - 1)

    # The marker is charged first, EXCEPT when charging it would leave the record
    # with no link at all: a URL that fits the cap on its own must reach the
    # index, and "something was removed" is worth less than the address it would
    # evict. Only that case drops the marker — whenever a link already fits
    # beside it, the cut stays marked.
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

    # A link the surviving fragment already carries needs no line of its own.
    # Membership is decided against the URLs the fragment HOLDS, never by
    # substring: one link's whole address is often a prefix of another's
    # (``…/pull/28`` inside ``…/pull/2836``), and a substring test drops the
    # shorter one as already present when it is nowhere in the result. Dropping
    # a link here only shortens the result, so the cap still holds.
    if tail:
        tail_urls = set(_unique_urls(tail))
        links = [u for u in links if u not in tail_urls]

    body = "\n".join([*links, tail] if tail else links)
    # One newline for the marker's own line, one for the body's first line.
    marker_cost = len(_CUT_MARKER) + (1 if body else 0) if marked else 0
    head_room = cap - len(body) - marker_cost - 1
    head = _head_before(text, head_room).rstrip() if head_room > 0 else ""

    # One rule for where a URL lands: the body holds it, and the head stops at
    # the first one it reaches. A second copy buys nothing and spends the budget
    # twice on one fact. There is no case to distinguish: a URL whole inside the
    # head is always one the body carries too, because the head budget is the
    # link budget minus one, so a URL the links step had no room for cannot fit
    # in the head either. Trimming only shortens the head, so the cap holds.
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

    A separator is charged only when something already precedes the link, so a URL
    that exactly fills the budget is kept rather than dropped for a newline it does
    not need. The returned spend is the length of the joined result.
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
    UNMATCHED inside the run — ``.../Foo_(bar)`` closes its own group and keeps
    it, while ``(https://host/p)`` does not and loses it. Trimming a balanced
    bracket stores a dead link, which is the thing this function exists to
    avoid.

    Only a TRAILING run is considered, so punctuation inside an address is never
    touched: ``…/a.b,c:d/`` survives whole. And only ``.`` and ``,`` count as
    prose there, because an address really can end in ``!`` or ``?``; a wider set
    turns ``…/wiki/Yahoo!`` into a dead link, which is the failure this function
    exists to prevent.

    The scan is bounded by the scheme rather than by emptiness: ``https://`` is
    never trimmable, so a bare ``http://a`` is returned as it stands and the
    string can never be run down to nothing.
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


@dataclass
class CronRunRecord:
    """Single cron execution record."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    job_id: str = ""
    trigger: str = "scheduled"  # "scheduled" | "manual"
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: int = 0
    status: str = "success"  # "success" | "failure" | "timeout" | "cancelled" | "interrupted"
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
            self._dir.mkdir(parents=True, exist_ok=True)
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
            logger.warning(
                "cron history disabled: %s is unusable (%s). Scheduling is "
                "unaffected; run records will not be persisted.",
                self._dir,
                exc,
            )
            return False

    def _probe_usable(self) -> bool:
        """True when the syscalls this store's own paths depend on all work.

        Opening the lock file is NOT sufficient evidence that a store whose
        mkdir was refused is usable. The read and rotate paths go through
        ``Path.exists()`` (an ``os.stat``) and ``Path.glob()`` (a directory
        scan), and pathlib RE-RAISES EPERM out of both rather than reporting
        False — ``_ignore_error`` covers ENOENT/ENOTDIR/EBADF/ELOOP, not
        EPERM. A probe that only opened the lock file therefore reported a
        stat-denied directory as usable, and ``rotate_all()`` — awaited
        unguarded by ``CronService.start()`` — then raised straight back out
        of service startup, which is the failure this class exists to prevent.

        So require the two capabilities that discriminate: stat the directory
        and open the lock file. Both are O(1) and fail fast. Enumerating the
        directory is deliberately NOT part of the probe — its cost grows with
        the number of jobs, and ``_degrade()`` already turns a directory-scan
        failure at runtime into disabled history rather than a raise, so
        proving ``glob()`` works up front buys nothing.
        """
        try:
            os.stat(self._dir)
            fd = os.open(str(self._lock_path()), os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError:
            return False
        os.close(fd)
        return True

    def _degrade(self, operation: str, exc: OSError) -> None:
        """Handle a runtime failure. Never raises.

        Only a DENIAL disables the store. A denial is a standing condition — the
        sandbox profile that refused us will refuse us for this process's whole
        life — so continuing to attempt writes just logs the same error per run.

        Every other ``OSError`` is treated as transient and costs ONE record:
        a full disk, an fd exhaustion, or a transient I/O error clears on its
        own, and disabling history for the process's remaining lifetime over a
        momentary ENOSPC would lose every subsequent run's record for no reason
        — strictly worse than the pre-existing behaviour, where each call site
        caught its own failure and the next run wrote normally.
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
        """Acquire advisory lock, return fd."""
        fd = os.open(str(self._lock_path()), os.O_WRONLY | os.O_CREAT, 0o600)
        platform_compat.acquire_lock(fd, exclusive=True)
        return fd

    def _unlock(self, fd: int) -> None:
        platform_compat.release_lock(fd)
        os.close(fd)

    async def append(self, record: CronRunRecord) -> None:
        """Write record to job file and index."""
        # Cap fields
        # The ONE truncation site for a summary: cron.py's run, timeout and
        # cancel recorders all hand the whole text over uncut, so the
        # configured cap and the keep-the-facts rule apply once, here. A
        # caller that pre-slices defeats both.
        #
        # One boundary this does not reach: a command cron's failure path caps
        # the JOB FIELD it writes (``job.last_error = err_str[:200]`` in
        # slack/gateway.py), which is a registry-size decision about crons.json,
        # not a decision about this record. The recorder still passes that field
        # over whole, so such a summary is simply as long as the field it came
        # from. Lifting that cap belongs to whoever owns the registry's size.
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
            wfd = os.open(str(job_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(wfd, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(include_trace=True)) + "\n")
            ifd = os.open(str(self._index_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(ifd, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(include_trace=False)) + "\n")
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
            lines = job_path.read_text(encoding="utf-8").strip().splitlines()
        except FileNotFoundError:
            return [], 0
        total = len(lines)
        lines.reverse()
        page = lines[offset : offset + limit]
        results = []
        for line in page:
            try:
                d = json.loads(line)
                d.pop("trace", None)
                results.append(d)
            except json.JSONDecodeError:
                continue
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
            lines = self._index_path.read_text(encoding="utf-8").strip().splitlines()
        except FileNotFoundError:
            return [], 0
        if job_id:
            filtered = []
            for line in lines:
                try:
                    if json.loads(line).get("job_id") == job_id:
                        filtered.append(line)
                except json.JSONDecodeError:
                    continue
            lines = filtered
        total = len(lines)
        lines.reverse()
        page = lines[offset : offset + limit]
        results = []
        for line in page:
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return results, total

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
            lines = job_path.read_text(encoding="utf-8").strip().splitlines()
        except FileNotFoundError:
            return None
        for line in lines:
            try:
                d = json.loads(line)
                if d.get("run_id") == run_id:
                    return d
            except json.JSONDecodeError:
                continue
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
            lines = job_path.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) <= self._max_records_per_job:
                return
            keep = lines[-self._max_records_per_job:]
            tmp = job_path.with_suffix(".tmp")
            wfd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(wfd, "w", encoding="utf-8") as f:
                f.write("\n".join(keep) + "\n")
            os.replace(tmp, job_path)
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
                lines = p.read_text(encoding="utf-8").strip().splitlines()
                if len(lines) > self._max_records_per_job:
                    keep = lines[-self._max_records_per_job:]
                    tmp = p.with_suffix(".tmp")
                    wfd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(wfd, "w", encoding="utf-8") as f:
                        f.write("\n".join(keep) + "\n")
                    os.replace(tmp, p)
            if self._index_path.exists():
                lines = self._index_path.read_text(encoding="utf-8").strip().splitlines()
                if len(lines) > self._max_index_records:
                    keep = lines[-self._max_index_records:]
                    tmp = self._index_path.with_suffix(".tmp")
                    wfd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(wfd, "w", encoding="utf-8") as f:
                        f.write("\n".join(keep) + "\n")
                    os.replace(tmp, self._index_path)
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

    def _delete_job_history_sync(self, job_id: str) -> bool:
        fd = self._lock()
        try:
            job_path = self._job_path(job_id)
            removed = job_path.exists()
            if removed:
                job_path.unlink()
            if self._index_path.exists():
                lines = self._index_path.read_text(encoding="utf-8").strip().splitlines()
                filtered = []
                for line in lines:
                    try:
                        if json.loads(line).get("job_id") != job_id:
                            filtered.append(line)
                    except json.JSONDecodeError:
                        continue
                if len(filtered) != len(lines):
                    tmp = self._index_path.with_suffix(".tmp")
                    content = "\n".join(filtered) + "\n" if filtered else ""
                    wfd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(wfd, "w", encoding="utf-8") as f:
                        f.write(content)
                    os.replace(tmp, self._index_path)
            return removed
        finally:
            self._unlock(fd)
