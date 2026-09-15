"""Read-only backfill validator: prove the event schema fits real stores.

Derives typed events from the EXISTING stores — transcripts, subagent run
dirs, the cron and autonudge snapshots, and usage shards — without modifying
any of them, then reports what fit and what did not. This is the progressive
validation step for the parallel event-log track: schema problems surface
here, against production-shaped data, before any live emit site exists.

This validator is read-only: it reports, and writes nothing. The report IS the
deliverable, and a write path would serve no reader while the log has none — it
lands with the first consumer that folds the log.

LIFECYCLE — this module is DISPOSABLE. It hand-reads five store formats (nested
cron ``schedule``, the three-way paused split, tombstone ``cause`` semantics)
rather than routing through each store's owning reader, deliberately: the
parallel track must not import the internals of the stores it validates, and a
one-shot schema probe is not worth coupling them. The cost is real — these
parsers can drift from the stores silently, because the fixtures pin today's
formats, not tomorrow's. So this module is NOT a historical-migration path and
must not become one: it is deleted when the live emit sites land, and anything
that needs to read a store in earnest goes through that store's own reader.

Usage::

    python -m kiro_crew.events.backfill            # report
    python -m kiro_crew.events.backfill --limit 200 --home /path/to/home
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from kiro_crew.config.paths import data_home
from kiro_crew.events.base import Event, kind_of, serialize
from kiro_crew.events.kinds import (
    AutonudgeArmed,
    CronRegistered,
    SessionMessage,
    SubagentCompleted,
    SubagentFailed,
    SubagentSpawned,
    TurnUsage,
)
from kiro_crew.history import transcript_stems
from kiro_crew.jsonl_util import bounded_records
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def _to_ms(value: object) -> int | None:
    """Best-effort epoch-milliseconds from ISO strings or numeric epochs.

    Store data is untrusted here by definition (that is what the validator is
    for), so the whole conversion is exception-bounded: non-finite floats,
    arbitrarily large integers (``math.isfinite`` itself overflows on them),
    bools, and unparseable strings all return ``None`` rather than aborting
    the run. A corrupt timestamp degrades one field, never the validation.
    """
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        if isinstance(value, str):
            if not value:
                return None
            return int(datetime.fromisoformat(value).timestamp() * 1000)
        as_float = float(value)
        if not math.isfinite(as_float) or as_float <= 0:
            return None
        # Heuristic: values below ~2001-09 in ms are second-resolution epochs.
        return int(as_float * 1000) if as_float < 1_000_000_000_000 else int(as_float)
    except (ValueError, OverflowError, OSError):
        return None


def _to_float(value: object) -> float | None:
    """Exception-bounded float conversion for untrusted numeric fields."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        as_float = float(value)
    except (ValueError, OverflowError):
        return None
    return as_float if math.isfinite(as_float) else None


def _mtime_ms(path: Path) -> int:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return 0


class SourceReport:
    """Per-source tally: derived events, parse failures, samples."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.events: list[Event] = []
        self.failures = 0
        #: Events whose ``ts_ms`` came from the FILE mtime because the store
        #: row carried no usable timestamp of its own.
        self.ts_fallbacks = 0

    def add(self, event: Event) -> None:
        self.events.append(event)

    def fail(self) -> None:
        self.failures += 1

    def ts_or_mtime(self, value: object, path: Path) -> int:
        """The row's own timestamp, or *path*'s mtime when it has none.

        The fallback is COUNTED. A report claiming "zero failures" while most
        timestamps were invented from file mtimes is measuring the schema's
        tolerance, not how well the store actually fits it -- so the count is
        reported alongside the totals and the caller cannot silently rely on
        the fallback.
        """
        ms = _to_ms(value)
        if ms:
            return ms
        self.ts_fallbacks += 1
        return _mtime_ms(path)


# ── sources (each strictly read-only) ─────────────────────────────────────


def _as_int(value: object) -> int | None:
    """*value* as an int, or ``None``. Rejects bool: a JSON ``true`` in a
    numeric field would serialize as a typed event the reader then refuses
    (its field validation excludes bool from int), so the writer must too.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _no_follow_fd(path: Path) -> int:
    """Open *path* read-only refusing symlinks, and return the raw fd.

    Every store file this validator reads is attacker-influenceable in
    principle (``--home`` may point anywhere), and parsed field values
    surface in the report's samples — so a symlink planted where a store
    file belongs could exfiltrate content from a protected path. O_NOFOLLOW
    makes the OPEN itself refuse the link (raising ``OSError``), which the
    callers' existing per-file error handling already counts as a source
    failure. On platforms without O_NOFOLLOW the flag is 0 and the lstat
    guard below still refuses the link (TOCTOU-tolerant: worst case reads a
    file swapped in at the same path, never a followed link on POSIX).

    The two wrappers below differ only in the layer they put on the fd, so
    the guard is stated once here and cannot drift between them.
    """
    if is_link_or_junction(path):
        raise OSError(f"refusing symlinked store file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _open_no_follow(path: Path) -> "io.TextIOWrapper":
    """Open *path* for TEXT reading, refusing symlinks (see :func:`_no_follow_fd`)."""
    return io.TextIOWrapper(io.FileIO(_no_follow_fd(path), "r"), encoding="utf-8", errors="replace")


def _open_binary_no_follow(path: Path) -> "io.BufferedReader":
    """Open *path* for BINARY reading, refusing symlinks (see :func:`_no_follow_fd`).

    The record readers in :mod:`kiro_crew.jsonl_util` require a binary handle
    so that their cap is a byte bound rather than a code-point count, and
    buffered so one ``readline`` is not one syscall.
    """
    return io.BufferedReader(io.FileIO(_no_follow_fd(path), "r"))


def _read_json_no_follow(path: Path) -> object:
    """``json.loads`` of *path* through the no-follow open."""
    with _open_no_follow(path) as fh:
        return json.loads(fh.read())


def _stem_to_key_index(home: Path) -> dict[str, str]:
    """Map transcript filename stems back to canonical session keys.

    ``session_map.json`` holds the raw session keys; ``transcript_stem`` is the
    exported sanitizer those keys pass through on their way to filenames (its
    docstring designates it for exactly this pairing, so the rule is never
    re-derived here). Stems with no session-map entry fall back to heuristics
    at the call site.
    """
    index: dict[str, str] = {}
    map_path = home / "session_map.json"
    if not map_path.exists():
        return index
    try:
        raw = _read_json_no_follow(map_path)
    except (OSError, ValueError, RecursionError):
        return index
    if not isinstance(raw, dict):
        return index
    for key in raw:
        if isinstance(key, str) and key:
            try:
                # transcript_stems returns EVERY stem a key's files may occupy
                # (canonical first, plus e.g. the bare thread_ts a legacy
                # Slack transcript still lives under) — its docstring
                # designates it over the singular form for exactly this
                # file-pairing decision.
                for stem in transcript_stems(key):
                    index.setdefault(stem, key)
            except Exception:  # noqa: BLE001 - one bad key must not kill the index
                continue
    return index


def _resolve_session_key(stem: str, index: dict[str, str]) -> str:
    """Canonical session key for a transcript filename stem.

    Dashboard keys are emitted WITHOUT the ``dashboard:`` prefix: the
    unmapped fallback below can only produce the bare form (stem strip),
    and usage rows carry bare slots, so the prefixed spelling would split
    one session's events across two correlation keys depending on whether
    its map entry survived.
    """
    if stem in index:
        key = index[stem]
        if key.startswith("dashboard:"):
            return key[len("dashboard:") :]
        return key
    if stem.startswith("dashboard_"):
        return stem[len("dashboard_") :]
    return stem


#: Transcript ``role`` values that represent an actual conversational turn.
#: Everything else a transcript persists (``tool`` records, injected ``nudge`` /
#: ``inject`` turns, ``error`` / ``notice`` / ``file`` / ``subagent`` rows) is a
#: different lifecycle fact and needs its own kind before it can be emitted.
_MESSAGE_ROLES = frozenset({"user", "assistant"})


def backfill_transcripts(home: Path, limit: int | None = None) -> SourceReport:
    """``sessions/*.jsonl`` conversational rows -> ``session/message`` events.

    Includes rotated history: ``sessions/archive/<stem>__<segment>.jsonl``
    segments fold under the same session key as their live transcript.
    """
    rep = SourceReport("transcripts")
    sessions = home / "sessions"
    if not sessions.exists():
        return rep
    index = _stem_to_key_index(home)
    paths: list[tuple[Path, str]] = [(p, p.stem) for p in sorted(sessions.glob("*.jsonl"))]
    archive = sessions / "archive"
    if archive.exists():
        # The segment suffix is appended at rotation time, so it is the LAST
        # __-delimited token; a session key may itself contain __.
        paths.extend((p, p.stem.rsplit("__", 1)[0]) for p in sorted(archive.glob("*.jsonl")))
    for path, stem in paths:
        key = _resolve_session_key(stem, index)
        try:
            with _open_binary_no_follow(path) as fh:
                for line in bounded_records(fh, path, label="backfill"):
                    if limit is not None and len(rep.events) >= limit:
                        return rep
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, RecursionError):
                        rep.fail()
                        continue
                    if not isinstance(row, dict):
                        continue
                    # Transcripts interleave conversation turns with tool
                    # records, injected nudges, and error/notice rows, and a
                    # `tool` row outnumbers real messages several to one. Only
                    # an actual conversational turn is a session/message; the
                    # rest are other lifecycle facts that need their own kinds,
                    # and counting them here would inflate the validator's
                    # totals with events whose payload contract does not hold.
                    if row.get("role") not in _MESSAGE_ROLES:
                        continue
                    ts = rep.ts_or_mtime(row.get("ts"), path)
                    content = row.get("content")
                    rep.add(
                        SessionMessage(
                            key=key,
                            ts_ms=ts,
                            role=str(row.get("role") or ""),
                            content_chars=len(content) if isinstance(content, str) else 0,
                        )
                    )
        except OSError:
            rep.fail()
            continue
    return rep


def backfill_subagents(home: Path, limit: int | None = None) -> SourceReport:
    """``subagents/<id>/`` run dirs -> spawned / completed / failed events."""
    rep = SourceReport("subagents")
    root = home / "subagents"
    if not root.exists():
        return rep
    try:
        run_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        rep.fail()
        return rep
    for d in run_dirs:
        if limit is not None and len(rep.events) >= limit:
            return rep
        key = f"subagent:{d.name}"
        state_path = d / "state.json"
        state: dict = {}
        if state_path.exists():
            try:
                loaded = _read_json_no_follow(state_path)
                state = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError, RecursionError):
                rep.fail()
        task = state.get("task")
        pid = _as_int(state.get("pid"))
        parent = state.get("parent_session")
        # Parent keys must join session/message events, which emit the BARE
        # dashboard spelling; keep both sides on the same form.
        parent_key = str(parent) if parent else None
        if parent_key and parent_key.startswith("dashboard:"):
            parent_key = parent_key[len("dashboard:") :]
        tombstone = d / "tombstone.json"
        result = d / "result.txt"
        # Read the tombstone BEFORE emitting spawned: kinds.py registers no
        # neutral "stopped" terminal kind, so a user-stopped run is skipped
        # whole — a spawned event with no terminal would replay forever as
        # an active run.
        cause: str | None = None
        outcome: str | None = None
        died_ms: int | None = None
        readable = False
        # Stat the tombstone once and let both decisions below read the answer.
        # The terminal branch decides from ``readable`` / ``cause`` / ``died_ms``,
        # which this block's parse is the only source of, so a second stat that
        # answered differently would judge the terminal against a file no other
        # line here ever saw.
        has_tombstone = tombstone.exists()
        if has_tombstone:
            try:
                t = _read_json_no_follow(tombstone)
                if isinstance(t, dict):
                    readable = True
                    cause = str(t.get("cause") or "") or None
                    outcome = str(t.get("outcome") or "") or None
                    died_ms = _to_ms(t.get("died"))
            except (OSError, ValueError, RecursionError):
                pass
            if readable and outcome == "stopped":
                continue
            # Restart reconciliation writes cause="gateway_restart" (legacy
            # paths "interrupted") for runs the gateway itself orphaned; the
            # run may well have delivered (recovery_action records that).
            # kinds.py registers no interrupted terminal kind, so these are
            # skipped whole like stopped runs — recording them as
            # subagent/failed fabricates errors for possibly-successful work.
            if readable and cause in ("gateway_restart", "interrupted"):
                continue
        rep.add(
            SubagentSpawned(
                key=key,
                ts_ms=rep.ts_or_mtime(state.get("started"), d),
                task_preview=(str(task)[:120] if isinstance(task, str) else None),
                pid=pid,
                parent_key=parent_key,
            )
        )
        # Only a tombstone is terminal evidence. ``result.txt`` exists while a
        # subagent is still STREAMING its answer, so its presence alone proves
        # nothing — a run dir with state but no tombstone is in-flight (or was
        # orphaned) and gets only the spawned event.
        if has_tombstone:
            # A tombstone does NOT mean failure: successful delivery writes a
            # ``cause="delivered"`` tombstone instead of deleting the folder
            # (deferred-TTL cleanup). Only a non-delivered cause is abnormal —
            # and an UNREADABLE tombstone proves nothing either way, so it is
            # recorded as a source failure with NO terminal event rather than
            # fabricating a failure for a possibly-successful run.
            if not readable:
                rep.fail()
                continue
            ts = died_ms if died_ms else rep.ts_or_mtime(None, tombstone)
            if cause == "delivered":
                turns = _as_int(state.get("turns"))
                size: int | None = None
                if result.exists():
                    try:
                        size = result.stat().st_size
                    except OSError:
                        size = None
                rep.add(SubagentCompleted(key=key, ts_ms=ts, turns=turns, result_bytes=size))
            else:
                rep.add(SubagentFailed(key=key, ts_ms=ts, reason=cause))
    return rep


def backfill_crons(home: Path, limit: int | None = None) -> SourceReport:
    """``crons.json`` snapshot -> ``cron/registered`` events."""
    rep = SourceReport("crons")
    path = home / "crons.json"
    if not path.exists():
        return rep
    try:
        store = _read_json_no_follow(path)
    except (OSError, ValueError, RecursionError):
        rep.fail()
        return rep
    jobs = store.get("jobs") if isinstance(store, dict) else None
    if not isinstance(jobs, list):
        rep.fail()
        return rep
    for job in jobs:
        if limit is not None and len(rep.events) >= limit:
            return rep
        if not isinstance(job, dict):
            rep.fail()
            continue
        job_id = str(job.get("id") or job.get("name") or "unknown")
        # Schedule is a NESTED object: {"kind": "every"|"at"|"cron",
        # "every_secs": ..., "at_ts": ..., "cron_expr": ...}.
        sched_val = job.get("schedule")
        sched: dict = sched_val if isinstance(sched_val, dict) else {}
        kind = str(sched.get("kind") or "")
        schedule = next(
            (
                f"{kind}:{sched[f]}"
                for f in ("cron_expr", "every_secs", "at_ts")
                if sched.get(f) is not None
            ),
            kind or None,
        )
        # Paused state is split across three fields by the store writer:
        # disabled entirely, paused by the user, or auto-paused on failures.
        paused: bool | None = None
        if any(f in job for f in ("enabled", "user_paused", "auto_paused")):
            paused = (
                not job.get("enabled", True)
                or bool(job.get("user_paused"))
                or bool(job.get("auto_paused"))
            )
        if job.get("script"):
            kind_label = "script"
        elif job.get("command"):
            kind_label = "command"
        else:
            kind_label = "llm"
        rep.add(
            CronRegistered(
                key=f"cron:{job_id}",
                ts_ms=rep.ts_or_mtime(job.get("created_ts"), path),
                name=(str(job["name"]) if job.get("name") else None),
                schedule=schedule,
                paused=paused,
                kind_label=kind_label,
            )
        )
    return rep


def backfill_autonudge(home: Path, limit: int | None = None) -> SourceReport:
    """``autonudge.json`` snapshot -> ``autonudge/armed`` events."""
    rep = SourceReport("autonudge")
    path = home / "autonudge.json"
    if not path.exists():
        return rep
    try:
        store = _read_json_no_follow(path)
    except (OSError, ValueError, RecursionError):
        rep.fail()
        return rep
    loops = store.get("loops") if isinstance(store, dict) else None
    if not isinstance(loops, list):
        rep.fail()
        return rep
    for loop in loops:
        if limit is not None and len(rep.events) >= limit:
            return rep
        if not isinstance(loop, dict):
            rep.fail()
            continue
        # ``active: false`` marks a stopped loop in the store; the writer's
        # default is live, so only an explicit False is skipped.
        if loop.get("active") is False:
            continue
        loop_id = str(loop.get("id") or "unknown")
        # The store writer's field is ``idle_secs`` (NudgeLoop); it is the
        # loop's re-injection interval.
        rep.add(
            AutonudgeArmed(
                key=f"nudge:{loop_id}",
                ts_ms=rep.ts_or_mtime(loop.get("created_ts"), path),
                interval_secs=_as_int(loop.get("idle_secs")),
                max_cycles=_as_int(loop.get("max_cycles")),
            )
        )
    return rep


def backfill_usage(home: Path, limit: int | None = None) -> SourceReport:
    """Usage shards -> ``turn/usage`` events (one per shard row)."""
    rep = SourceReport("usage")
    shard_dir = home / "usage" / "tokens"
    if not shard_dir.exists():
        return rep
    for path in sorted(shard_dir.glob("*.jsonl")):
        try:
            with _open_binary_no_follow(path) as fh:
                for line in bounded_records(fh, path, label="backfill"):
                    if limit is not None and len(rep.events) >= limit:
                        return rep
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, RecursionError):
                        rep.fail()
                        continue
                    if not isinstance(row, dict):
                        rep.fail()
                        continue
                    ts = rep.ts_or_mtime(row.get("ts"), path)
                    rep.add(
                        TurnUsage(
                            key=str(row.get("slot") or "unknown"),
                            ts_ms=ts,
                            model=(str(row["model"]) if row.get("model") else None),
                            provider=(str(row["provider"]) if row.get("provider") else None),
                            credits=_to_float(row.get("credits")),
                            cost=_to_float(row.get("cost")),
                            turns=(_as_int(row.get("turns"))),
                            duration_ms=_as_int(row.get("duration_ms")),
                        )
                    )
        except OSError:
            rep.fail()
            continue
    return rep


ALL_SOURCES = (
    backfill_transcripts,
    backfill_subagents,
    backfill_crons,
    backfill_autonudge,
    backfill_usage,
)


def run_backfill(
    home: Path | None = None,
    *,
    limit: int | None = None,
) -> dict:
    """Run every source read-only; return the report dict.

    Nothing is written. The validator's job is to prove the v1 schema fits
    production-shaped data, and the report is that proof — a write path would
    have no reader while the log has none, so it lands with the first consumer
    that folds the log.

    The report deliberately carries more than a failure count. Every payload
    field is optional and degrades to ``None``, so "zero parse failures" is
    close to structurally guaranteed and on its own measures the schema's
    TOLERANCE rather than how well a store fits it. ``fill`` therefore reports,
    per kind, how many events actually populated each field, and
    ``ts_fallbacks`` how many timestamps had to be taken from a file mtime. A
    field that is always empty is a field this schema has not earned.
    """
    root = home if home is not None else data_home()
    reports = [source(root, limit) for source in ALL_SOURCES]
    kind_counts: Counter[str] = Counter()
    samples: dict[str, str] = {}
    fill: dict[str, dict[str, int]] = {}
    for rep in reports:
        for ev in rep.events:
            kind = kind_of(ev)
            kind_counts[kind] += 1
            slot = fill.setdefault(kind, {})
            for name, value in ev.data().items():
                # setdefault FIRST: a field that is never populated must still
                # appear, at 0, or the gap it represents stays invisible.
                slot.setdefault(name, 0)
                if value is not None:
                    slot[name] += 1
            if kind not in samples:
                # Samples reach CLI stdout; store fields (task previews,
                # cron names) can carry secrets, so redact like any
                # other agent-visible output.
                line = serialize(ev, src="backfill")
                line, _ = redact_credentials(line)
                line, _ = redact_exfiltration_urls(line)
                samples[kind] = line
    return {
        "home": str(root),
        "kinds": dict(sorted(kind_counts.items())),
        "failures": {rep.name: rep.failures for rep in reports},
        "ts_fallbacks": {rep.name: rep.ts_fallbacks for rep in reports},
        "totals": {rep.name: len(rep.events) for rep in reports},
        "fill": {
            kind: {name: fill[kind][name] for name in sorted(fill[kind])} for kind in sorted(fill)
        },
        "samples": samples,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="max events per source")
    parser.add_argument("--home", type=Path, default=None, help="data home override")
    args = parser.parse_args()
    report = run_backfill(args.home, limit=args.limit)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(_main())
