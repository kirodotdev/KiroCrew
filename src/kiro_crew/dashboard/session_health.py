"""Session health detector — scans recent gateway.log / security_events.jsonl for stalled slots.

A slot is "stalled" when the UI would show it as working but the underlying agent has
already failed silently. Detected patterns:

* ``subagent_timeout``  — ``Injected timeout error for subagent ... into slot KEY``
* ``prompt_stuck``      — ``ACP error in slot KEY: [AcpPromptBusy] ...``, or the
  legacy shape ``ACP error in slot KEY: ... 'Prompt already in progress'``

Two ``prompt_stuck`` patterns exist on purpose. The current one keys off the
exception class that ``chat_runner`` logs, which is the structural
classification and cannot drift when user-facing wording changes; the legacy one
keys off the raw backend text and is retained so log tails written by earlier
gateways (before the message was formatted) still resolve. Either match is the
same signal.

Note: ``Session KEY has dead provider — removing stale entry`` is NOT a stall signal.
It's emitted during healthy self-cleanup: the session manager detected a dead provider
and removed the stale entry so the next request cold-starts a fresh session. The user
sees no interruption.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Dict, List, Tuple

from kiro_crew.config.paths import config_dir

# Only consider log lines from the last STALL_WINDOW_SECONDS to avoid flagging
# long-resolved incidents. 10 minutes is wide enough to catch a stuck session
# before the user would have noticed, narrow enough to avoid stale reports.
STALL_WINDOW_SECONDS = 600
_LOG_TAIL_BYTES = 256 * 1024  # scan last 256 KB of gateway.log — cheap

_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("subagent_timeout", re.compile(r"Injected timeout error for subagent \S+ into slot (\S+)")),
    # Current shape: chat_runner logs the exception class, so this matches the
    # structural verdict rather than any wording the formatter produces.
    ("prompt_stuck", re.compile(r"ACP error in slot (\S+): \[AcpPromptBusy\]")),
    # Legacy shape: raw backend text, for log tails written by earlier gateways.
    ("prompt_stuck", re.compile(r"ACP error in slot (\S+):.*Prompt already in progress")),
]

# gateway.log lines start with "HH:MM:SS " (local time). We can't reliably
# parse that to an absolute ts, so instead we use file mtime as upper bound
# and rely on _LOG_TAIL_BYTES to keep the window small.
_TS_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) ")


def _read_tail(path: Path, nbytes: int) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    try:
        with path.open("rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                f.readline()  # discard possibly-partial line
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _normalize_slot(raw: str) -> str:
    """Strip trailing punctuation; handle 'dashboard:' prefix uniformly."""
    s = raw.rstrip(":;,.")
    return s.split("dashboard:", 1)[-1] if s.startswith("dashboard:") else s


def _line_age_seconds(line: str, file_mtime: float) -> float:
    """Estimate how many seconds ago a log line was written.

    Gateway.log lines start with ``HH:MM:SS``. We compute the delta between
    the line's time-of-day and the file mtime's time-of-day. This breaks
    across midnight but is good enough for a 10-minute window.
    """
    m = _TS_RE.match(line)
    if not m:
        return float("inf")
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    line_sod = h * 3600 + mi * 60 + s  # seconds-of-day for the log line
    import datetime as _dt
    mtime_dt = _dt.datetime.fromtimestamp(file_mtime)
    mtime_sod = mtime_dt.hour * 3600 + mtime_dt.minute * 60 + mtime_dt.second
    delta = mtime_sod - line_sod
    if delta < 0:
        delta += 86400  # crossed midnight
    return delta


def compute_session_health(log_path: Path | None = None, now: float | None = None) -> Dict[str, dict]:
    """Return ``{slot_key: {reason, since_ts}}`` for slots flagged as stalled."""
    if log_path is None:
        log_path = config_dir() / "gateway.log"
    if not log_path.exists():
        return {}
    if now is None:
        now = time.time()

    try:
        file_mtime = log_path.stat().st_mtime
    except OSError:
        return {}

    tail = _read_tail(log_path, _LOG_TAIL_BYTES)
    if not tail:
        return {}

    out: Dict[str, dict] = {}
    for line in tail.splitlines():
        age_from_mtime = _line_age_seconds(line, file_mtime)
        wall_age = (now - file_mtime) + age_from_mtime
        if wall_age > STALL_WINDOW_SECONDS:
            continue  # too old
        for reason, pat in _PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            slot = _normalize_slot(m.group(1))
            if slot.startswith("_") or slot.startswith("cron_") or slot.startswith("cron:"):
                continue
            since_ts = file_mtime - age_from_mtime
            out[slot] = {"reason": reason, "since_ts": since_ts}
    return out
