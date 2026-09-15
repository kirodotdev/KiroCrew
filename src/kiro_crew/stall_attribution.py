"""Attribute a loop-stall crash dump to the work the gateway was doing.

A dump written by the loop-stall watchdog says WHERE the event loop wedged (the
frames of the main thread) but not on whose behalf. This module answers two
questions from evidence on disk, and says so when it cannot:

1. **Which surface** was driving the loop -- a cron run, a dashboard chat
   turn, a Slack/Discord/Telegram dispatcher, a subagent, the task runner --
   read from the frame FILES of the wedged thread's stack.
2. **Which cron job**, when the surface is cron -- joined by PID to the
   in-flight markers :mod:`kiro_crew.cron_inflight` leaves behind: a marker
   whose ``pid`` equals the dump header's ``# PID:`` and whose ``started_at``
   precedes the dump was a run in progress when that process died.

Attribution never guesses. One matching marker names the job; several name
candidates and no job; none says so. The doctor prints the result, and the
cron service's boot-time breaker acts only on a single-job, cron-surface
attribution. The breaker also records what the markers said before it sweeps
them, so the doctor reaches the same verdict on a later run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew.cron_inflight import RunningMarker, read_markers, read_recorded_attribution
from kiro_crew.dashboard.crash_dump_store import (
    dump_owner_identity,
    dump_wedged_frames,
    get_dumps_dir,
    newest_dump_with_stacks,
)

logger = logging.getLogger(__name__)

#: faulthandler frame line: ``  File "<path>", line <n> in <func>``.
_FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+) in (?P<func>\S+)')

#: Ordered (surface label, predicate over (posix file path, function)). The
#: FIRST frame walked bottom-up that satisfies a predicate decides, because the
#: outermost Kiro Crew frame is the one that says who started the turn: a cron
#: turn also passes through the Slack gateway module and the shared streaming
#: helper, so matching top-down on "slack/" would misname it.
_SURFACE_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # label, path fragments, function names -- either kind of hit qualifies
    ("cron", ("/kiro_crew/cron.py",), ("_cron_callback", "_run_job_isolated")),
    ("heartbeat", ("/kiro_crew/heartbeat.py",), ()),
    ("task runner", ("/kiro_crew/task_executor.py", "/kiro_crew/task_planner.py"), ()),
    ("subagent", ("/kiro_crew/subagent_manager/",), ()),
    ("workflow", ("/kiro_crew/workflows/",), ()),
    ("dashboard chat", ("/kiro_crew/dashboard/chat_runner.py",), ()),
    ("dashboard side panel", ("/kiro_crew/dashboard/handlers/side.py",), ()),
    ("slack", ("/kiro_crew/slack/handler.py", "/kiro_crew/slack/transport_dispatch.py"), ()),
    ("discord", ("/kiro_crew/discord/",), ()),
    ("telegram", ("/kiro_crew/telegram/",), ()),
    ("teams", ("/kiro_crew/teams/",), ()),
    ("webex", ("/kiro_crew/webex/",), ()),
    ("messaging", ("/kiro_crew/messaging/dispatch.py",), ()),
)

#: Gate frames worth naming as "stuck in": the security gate and its callers.
#: The security gate ships as a PACKAGE, so the entry is the directory prefix and
#: every submodule of it counts; a bare module-file spelling would name only one.
_GATE_FILES = ("/kiro_crew/security/", "/kiro_crew/hooks.py")


@dataclass(frozen=True)
class Frame:
    file: str
    line: int
    func: str

    @property
    def short(self) -> str:
        return f"{Path(self.file).name}:{self.line} {self.func}"


@dataclass
class StallAttribution:
    dump: Path
    surface: str = "unknown"
    #: The innermost Kiro Crew frame of the wedged stack (what the loop was
    #: actually executing), when one is recognisable.
    stuck_in: Frame | None = None
    #: The security-gate frame, when the stall is inside the permission gate.
    gate: Frame | None = None
    owner_pid: int | None = None
    #: In-flight cron markers whose PID matches the dump's owner. One entry is
    #: an attribution; more than one is an ambiguity the caller must not
    #: resolve by guessing.
    candidates: list[RunningMarker] = field(default_factory=list)
    #: Markers left by SOME dead process that is not this dump's owner (an
    #: older crash, a SIGKILL, a power loss). Reported, never attributed.
    unrelated_abandoned: list[RunningMarker] = field(default_factory=list)

    @property
    def job(self) -> RunningMarker | None:
        return self.candidates[0] if len(self.candidates) == 1 else None

    @property
    def is_cron(self) -> bool:
        return self.surface == "cron"


def parse_frames(lines: list[str]) -> list[Frame]:
    out: list[Frame] = []
    for ln in lines:
        m = _FRAME_RE.match(ln)
        if m:
            out.append(
                Frame(m.group("file").replace("\\", "/"), int(m.group("line")), m.group("func"))
            )
    return out


def classify_surface(frames: list[Frame]) -> str:
    """Name the surface from the OUTERMOST recognised frame (bottom-up walk)."""
    for fr in reversed(frames):
        for label, fragments, funcs in _SURFACE_RULES:
            if any(fragment in fr.file for fragment in fragments) or fr.func in funcs:
                return label
    return "unknown"


def _innermost_crew_frame(frames: list[Frame]) -> Frame | None:
    for fr in frames:
        if "/kiro_crew/" in fr.file:
            return fr
    return None


def _gate_frame(frames: list[Frame]) -> Frame | None:
    for fr in frames:
        if any(g in fr.file for g in _GATE_FILES):
            return fr
    return None


def attribute_dump(dump_path: Path, cron_base_dir: Path) -> StallAttribution:
    """Attribute one dump. ``cron_base_dir`` is the cron store directory
    (``CronService(base_dir=...)``), under which the in-flight markers live."""
    frames = parse_frames(dump_wedged_frames(dump_path))
    owner = dump_owner_identity(dump_path)
    attribution = StallAttribution(
        dump=dump_path,
        surface=classify_surface(frames),
        stuck_in=_innermost_crew_frame(frames),
        gate=_gate_frame(frames),
        owner_pid=owner[0] if owner is not None else None,
    )
    try:
        dump_mtime = dump_path.stat().st_mtime
    except OSError:
        return attribution
    for marker in read_markers(cron_base_dir):
        alive = marker.owner_alive()
        if alive is True:
            continue  # a run in progress on a live gateway explains no crash
        # The join is the writing PROCESS, not the PID number: a replacement
        # container is PID 1 like the gateway that died in it, and a recycled
        # PID on one host is live while its owner is gone. The dump header and
        # the marker both carry (pid, PID domain, start id); when they agree the
        # dump is that process's death certificate, whether or not this host
        # can probe the PID (``alive is None``). A marker written AFTER the dump
        # belongs to a later session that also died; it cannot be the run this
        # dump interrupted.
        if (
            owner is not None
            and marker.same_process(*owner)
            and marker.started_at <= dump_mtime + 1.0
        ):
            attribution.candidates.append(marker)
        elif alive is False:
            attribution.unrelated_abandoned.append(marker)
        # else: a foreign-domain marker this host cannot judge -- neither
        # evidence for this dump nor a run known to be dead; left alone.
    # The cron service's breaker sweeps the abandoned markers on the boot after
    # the crash, and the doctor runs later: what it read is kept in a record
    # beside them, keyed by the dump. Live markers, if any survived, win.
    recorded = read_recorded_attribution(cron_base_dir, dump_path.name)
    if recorded is not None:
        seen = {m.job_id for m in attribution.candidates + attribution.unrelated_abandoned}
        for marker in recorded[0]:
            if marker.job_id not in seen:
                attribution.candidates.append(marker)
                seen.add(marker.job_id)
        for marker in recorded[1]:
            if marker.job_id not in seen:
                attribution.unrelated_abandoned.append(marker)
                seen.add(marker.job_id)
        attribution.candidates.sort(key=lambda m: m.started_at)
    return attribution


def attribute_latest_stall(
    cron_base_dir: Path, dumps_dir: Path | None = None
) -> StallAttribution | None:
    """Attribute the newest dump that carries stacks, or None when there is none."""
    latest = newest_dump_with_stacks(dumps_dir or get_dumps_dir())
    if latest is None:
        return None
    return attribute_dump(latest, cron_base_dir)


def _inert(text: str) -> str:
    """*text* with every non-printable character escaped, so a string read off
    disk -- a cron job's name from a marker, a frame path from a dump -- cannot
    carry a carriage return or an escape sequence into the terminal
    ``kirocrew doctor`` prints on, or spoof the line around it. Printable text,
    including non-ASCII names, is returned as written."""
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in text)


def _job_label(marker: RunningMarker) -> str:
    return f"'{_inert(marker.name)}' ({_inert(marker.job_id)})"


def describe(attribution: StallAttribution) -> list[str]:
    """Human-readable lines for ``kirocrew doctor`` and the boot notification.

    Every line is a statement of evidence, and the last line is the action the
    evidence supports -- or the statement that it supports none.
    """
    a = attribution
    lines: list[str] = []
    if a.gate is not None:
        lines.append(f"stuck in the tool permission gate ({_inert(a.gate.short)})")
    elif a.stuck_in is not None:
        lines.append(f"stuck in {_inert(a.stuck_in.short)}")
    if a.is_cron:
        if a.job is not None:
            lines.append(
                f"the gateway (PID {a.owner_pid}) was executing cron job "
                f"{_job_label(a.job)} when the watchdog terminated it"
            )
            lines.append(f"recommended: kirocrew cron pause {_inert(a.job.job_id)}")
        elif len(a.candidates) > 1:
            names = ", ".join(_job_label(m) for m in a.candidates)
            lines.append(
                f"a cron turn was on the loop, and {len(a.candidates)} jobs were in "
                f"flight in PID {a.owner_pid}: {names}"
            )
            lines.append("cannot name one job from this evidence; inspect each before pausing")
        else:
            lines.append(
                "a cron turn was on the loop, but no in-flight marker matches the dump's "
                "PID (a build without markers, or the marker directory was cleared); "
                "cannot name the job"
            )
    elif a.surface != "unknown":
        lines.append(f"the loop was serving a {a.surface} turn; no cron job is implicated")
    else:
        lines.append("the wedged stack names no known surface; cannot attribute the stall")
    if a.unrelated_abandoned:
        names = ", ".join(
            f"'{_inert(m.name)}' ({_inert(m.job_id)}, PID {m.pid})" for m in a.unrelated_abandoned
        )
        lines.append(f"other runs cut short by a dead gateway, not by this dump: {names}")
    return lines
