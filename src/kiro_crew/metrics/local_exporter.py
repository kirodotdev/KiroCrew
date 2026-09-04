"""Local-first JSONL metric exporter (OSS-clean).

An OpenTelemetry ``MetricExporter`` that appends one JSON line per export cycle
to ``<dir>/metrics-YYYY-MM-DD-<pid>.jsonl``. This is KiroCrew's default metrics
sink: data stays on the local disk (default ``~/.kiro/crew/metrics``) and never
leaves the host. Remote / OTLP egress is a separate, opt-in exporter (deferred).

Per-process shards: the filename includes the PID so each shard has a single
writer. Multiple telemetry-enabled processes (the gateway + spawned agents /
apps that each build their own recorder) therefore never append to the same
file. A private sidecar lock serializes pruning processes only. Appends and
rotation stay lock-free on their per-PID shards; pruning protects canonical
writers owned by live PIDs (and recently modified canonical shards), so it
never unlinks another process's active write.

PRIVACY: attribute values are redacted at the ``MetricsRecorder`` facade before
they reach the SDK. That is defence in depth over the call-site requirement to
pass only low-cardinality constants, not a guarantee that a serialized data
point carries no secret or PII -- see ``schema.py`` for where redaction reaches.
The directory (0o700) and shards (0o600) are created private -- matching
the ``~/.kiro/crew`` file-permission convention -- so no other local user on a
shared host can read another user's metrics.

RETENTION: age + total-size rotation. Before an append would push the live
shard past ``max_total_mb``, it is closed by renaming it and a fresh live shard
is opened. Each export cycle opportunistically prunes shards older than
``retention_days`` and then deletes closed shards oldest-first until the total
is back under budget. Pruning is throttled (at most once per
``_PRUNE_INTERVAL_SECS``) so a short export interval cannot turn every flush
into a full directory scan. Both caps are configurable and independently
disable-able (0). This bounds unbounded on-disk telemetry growth.

OSS-CLEAN: depends only on ``opentelemetry`` + the stdlib.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, NamedTuple

from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)

from kiro_crew import platform_compat
from kiro_crew.metrics.schema import RESOURCE_ATTR_PROCESS_START_TIME
from kiro_crew.metrics.temporality import delta_preference

logger = logging.getLogger(__name__)

# Minimum wall-clock gap between two prune passes. A telemetry-enabled host can
# set export_interval_seconds as low as 1, so pruning on every export would
# re-scan (and re-stat) the whole metrics dir up to once a second. Throttling
# keeps the amortised cost of retention enforcement negligible while still
# reclaiming space promptly (well within a day, the shard rotation granularity).
_PRUNE_INTERVAL_SECS = 300.0

_BYTES_PER_MB = 1024 * 1024
# Canonical live shard: metrics-YYYY-MM-DD-PID.jsonl. Rotated shards append a
# positive time_ns field. Retention must never treat a merely prefix-matching
# user file as exporter-owned when telemetry.local_dir points at a shared dir.
_SHARD_NAME_RE = re.compile(
    r"metrics-(?P<day>\d{4}-\d{2}-\d{2})-(?P<pid>[1-9]\d*)" r"(?:-(?P<rotation>[1-9]\d*))?\.jsonl"
)


def _stamp_process_identity(line: str) -> str:
    """Stamp the writing process's start-time identity at resource level.

    One field per exported record: ``RESOURCE_ATTR_PROCESS_START_TIME`` is set
    on each ``resource_metrics`` entry's resource attributes (in practice one
    per line — one provider per exporter). The dashboard aggregator keys
    cumulative-counter streams by (shard PID, this token, attrs), which makes a
    PID-reuse process boundary deterministic instead of inferable only from a
    value drop. Resource level deliberately — a per-metric attribute would
    multiply every instrument's series cardinality (see ``schema.py``).

    Stamped AFTER serialization, into the JSONL line only, never onto the SDK
    ``Resource``: the provider's one ``Resource`` also feeds the opt-in OTLP
    reader, so a resource attribute would egress this host-local token to
    ``telemetry.otlp_endpoint``. The per-cycle re-serialize is the price of
    keeping the token on-host.

    Fail soft, twice over: when the platform read is unavailable the line is
    returned untouched, so legacy consumers see exactly the shape they already
    parse and the aggregator applies its value heuristic; and a line whose
    payload cannot be parsed and mutated as the exporter's own JSON shape is
    returned untouched rather than raised on — stamping is auxiliary, and it
    must never cost the metric payload that was just serialized.
    """
    identity = platform_compat.own_process_start_time()
    if identity is None:
        return line
    stamped = False
    try:
        payload = json.loads(line)
        for rm in payload.get("resource_metrics") or []:
            resource = rm.get("resource")
            if isinstance(resource, dict):
                resource.setdefault("attributes", {})[RESOURCE_ATTR_PROCESS_START_TIME] = identity
                stamped = True
    except (ValueError, AttributeError, TypeError):
        return line
    return json.dumps(payload) if stamped else line


class _Shard(NamedTuple):
    """One exporter-owned shard as retention sees it.

    ``protected`` is resolved once, while stat'ing, and reused by both caps:
    ``_is_protected_writer`` reads the wall clock, so re-deriving it per cap could
    answer differently on either side of the recency cutoff within one plan.
    """

    mtime_ns: int
    size: int
    path: Path
    protected: bool


class JsonlMetricExporter(MetricExporter):
    """Append aggregated metrics as newline-delimited JSON under *directory*.

    ``retention_days`` and ``max_total_mb`` bound the on-disk footprint (0 on
    either disables that cap). Pruning runs opportunistically after each export,
    throttled to at most once per ``_PRUNE_INTERVAL_SECS``.
    """

    def __init__(
        self,
        directory: Path,
        *,
        retention_days: int = 0,
        max_total_mb: int = 0,
    ) -> None:
        # DELTA temporality: each export cycle writes the delta since the last
        # cycle rather than a growing cumulative snapshot. Daily aggregation over
        # the per-day JSONL shards then reduces to a clean element-wise sum of
        # bucket counts across cycles and PIDs, and stays correct across process
        # restarts and day boundaries (cumulative snapshots would double-count
        # and misattribute a PID's counts to the wrong day).
        #
        # The map itself lives in metrics.temporality, which owns the rule for
        # BOTH sinks -- including why observable counters are deliberately absent
        # from it. Declaring it inline here is what let the OTLP leg ship
        # CUMULATIVE for the same instruments with nothing failing.
        super().__init__(preferred_temporality=delta_preference())
        self._dir = Path(directory)
        # Negative values are meaningless; clamp to 0 (disabled) defensively so a
        # mis-wired caller can never request "prune everything".
        self._retention_days = max(0, int(retention_days))
        self._max_total_bytes = max(0, int(max_total_mb)) * _BYTES_PER_MB
        # Negative infinity makes the first successful export eligible for a
        # retention sweep regardless of host monotonic-clock uptime.
        self._last_prune = float("-inf")
        # The first sweep that would delete data warns and defers once, giving
        # operators a full prune interval to disable retention. Process-local
        # state avoids introducing a persistent migration format.
        self._retention_notice_emitted = False

    def _target_file(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # PID in the name => single-writer shard (no cross-process interleave).
        return self._dir / f"metrics-{day}-{os.getpid()}.jsonl"

    @staticmethod
    def _is_exporter_shard(path: Path) -> bool:
        """Return whether *path* has an exact exporter-generated shard name."""
        match = _SHARD_NAME_RE.fullmatch(path.name)
        if match is None:
            return False
        try:
            datetime.strptime(match.group("day"), "%Y-%m-%d")
        except ValueError:
            return False
        return True

    def _rotate_target_if_needed(self, target: Path, incoming_bytes: int) -> None:
        """Close *target* before the next append would exceed the size budget.

        The renamed shard remains covered by ``metrics-*.jsonl`` retention,
        while the canonical daily/PID path becomes the fresh live writer. A
        single serialized record may itself exceed the configured cap; keeping
        that current record is the minimum lossless overrun and is bounded by
        one export payload rather than a full day of writes.
        """
        if self._max_total_bytes <= 0:
            return
        try:
            current_size = target.stat().st_size
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.debug("metrics shard stat failed before rotation: %s", exc)
            return
        if current_size <= 0 or current_size + incoming_bytes <= self._max_total_bytes:
            return
        rotated = target.with_name(f"{target.stem}-{time.time_ns()}{target.suffix}")
        try:
            target.replace(rotated)
            self._restrict_file(rotated)
        except OSError as exc:
            # Retention is best-effort and must never make a successful metric
            # serialization fail. A later export retries rotation/pruning.
            logger.warning("metrics live-shard rotation failed: %s", exc)

    @contextmanager
    def _directory_lock(self) -> Iterator[bool]:
        """Try to serialize pruning across exporter processes."""
        lock_path = self._dir / ".metrics.lock"
        # Writable binary mode is required by msvcrt.locking on Windows. Seed
        # one byte because that API locks a byte range and may reject an empty
        # file. The sidecar is private and excluded from shard scans.
        with lock_path.open("a+b") as lock_fd:
            lock_fd.seek(0, os.SEEK_END)
            if lock_fd.tell() == 0:
                lock_fd.write(b"\0")
                lock_fd.flush()
            self._restrict_file(lock_path)
            acquired = platform_compat.try_acquire_lock(lock_fd.fileno(), exclusive=True)
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                platform_compat.release_lock(lock_fd.fileno())

    @staticmethod
    def _restrict_file(path: Path) -> None:
        """Best-effort owner-only lockdown of a telemetry FILE.

        ``restrict_to_owner`` rather than ``os.chmod(0o600)``: on Windows chmod
        only toggles the read-only attribute and leaves the inherited DACL
        intact, so the "telemetry stays private" contract in this module's
        docstring held on POSIX and silently did not on Windows.

        Kept best-effort (the helper is fail-loud, so the except stays): an
        exporter must never make a successful metric serialization fail, and a
        readable telemetry shard is a smaller harm than a dropped metric.
        Separate from :meth:`_restrict_dir` because ``restrict_to_owner`` is
        file-shaped -- its grants carry no inheritance flags, so handing it a
        directory leaves files created inside on the creating token's DACL.
        """
        try:
            platform_compat.restrict_to_owner(path)
        except OSError as exc:
            logger.debug("owner-only lockdown of file %s failed: %s", path, exc)

    @staticmethod
    def _restrict_dir(path: Path) -> None:
        """Best-effort owner-only lockdown of the telemetry DIRECTORY.

        The directory counterpart of :meth:`_restrict_file`: it applies the
        inheritable DACL, so shards created inside it later are owner-only from
        birth rather than at the moment the export happens to chmod them.
        """
        try:
            platform_compat.restrict_dir_to_owner(path)
        except OSError as exc:
            logger.debug("owner-only lockdown of dir %s failed: %s", path, exc)

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: object,
    ) -> MetricExportResult:
        """Serialize *metrics_data* to a single JSON line and append it."""
        try:
            line = _stamp_process_identity(metrics_data.to_json(indent=None))
            encoded = (line + "\n").encode("utf-8")
            self._dir.mkdir(parents=True, exist_ok=True)
            # ~/.kiro/crew convention: telemetry stays private (dir 0o700, file
            # 0o600). mkdir/open modes are masked by umask, and on Windows the
            # mode bits do nothing at all, so lock both down explicitly.
            self._restrict_dir(self._dir)
            # Per-PID shards have one writer, so append + rotation need no
            # directory-wide lock. Keeping this path lock-free prevents
            # phase-aligned exporters from permanently dropping DELTA cycles.
            target = self._target_file()
            self._rotate_target_if_needed(target, len(encoded))
            with target.open("ab") as fh:
                fh.write(encoded)
            self._restrict_file(target)
            # Retention is separately serialized and best-effort. A contended
            # prune is skipped without discarding the export that just landed.
            self._maybe_prune()
        except Exception as exc:  # an exporter must never raise
            logger.warning("metrics JSONL export failed: %s", exc)
            return MetricExportResult.FAILURE
        return MetricExportResult.SUCCESS

    def _maybe_prune(self) -> None:
        """Throttle and non-blockingly serialize retention sweeps."""
        if self._retention_days <= 0 and self._max_total_bytes <= 0:
            return  # both caps disabled — nothing to enforce
        now = time.monotonic()
        if now - self._last_prune < _PRUNE_INTERVAL_SECS:
            return
        try:
            with self._directory_lock() as acquired:
                if not acquired:
                    logger.debug("metrics directory lock busy; skipping prune cycle")
                    return
                self._last_prune = now
                self._prune()
        except Exception as exc:  # retention must never break telemetry
            logger.warning("metrics retention prune failed: %s", exc)

    @staticmethod
    def _is_protected_writer(path: Path, mtime_ns: int) -> bool:
        """Return whether a canonical shard may still be actively written."""
        match = _SHARD_NAME_RE.fullmatch(path.name)
        if match is None or match.group("rotation") is not None:
            return False
        # A very recent canonical shard may be between open/write/close even if
        # PID liveness is unavailable or the UTC day has just rolled over.
        recent_cutoff = time.time_ns() - int(_PRUNE_INTERVAL_SECS * 1_000_000_000)
        if mtime_ns >= recent_cutoff:
            return True
        current_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return match.group("day") == current_day and platform_compat.pid_exists(
            int(match.group("pid"))
        )

    def _prune(self) -> None:
        """Apply one retention plan, warning and deferring the first deletion.

        Production callers hold ``_directory_lock`` only for this sweep.
        Canonical shards that may still be active are protected by PID + recency
        checks; rotated shards are closed before they become prune candidates.
        The first destructive plan in each exporter process emits a fixed,
        path-free warning and is deferred for one full prune interval. This
        gives operators a reaction window without creating persistent migration
        state. Tests may call this helper directly.
        """
        plan = self._prune_plan()
        if not plan:
            return
        if not self._retention_notice_emitted:
            logger.warning(
                "local metrics retention will permanently delete exporter-owned "
                "shards outside configured limits on the next eligible sweep; "
                "set telemetry.retention_days and telemetry.max_total_mb to 0 "
                "to preserve all local metric history"
            )
            self._retention_notice_emitted = True
            return
        for path in plan:
            self._unlink(path)

    def _prune_plan(self) -> list[Path]:
        """Return exact exporter-owned shards eligible for deletion."""
        if not self._dir.exists():
            return []
        shards: list[_Shard] = []
        for path in self._dir.glob("metrics-*.jsonl"):
            if not self._is_exporter_shard(path):
                continue
            try:
                shard_stat = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(shard_stat.st_mode):
                continue
            shards.append(
                _Shard(
                    mtime_ns=shard_stat.st_mtime_ns,
                    size=shard_stat.st_size,
                    path=path,
                    protected=self._is_protected_writer(path, shard_stat.st_mtime_ns),
                )
            )

        deletions: list[Path] = []
        # Age cap: plan deletion for anything older than the retention window.
        if self._retention_days > 0:
            cutoff = time.time_ns() - self._retention_days * 86400 * 1_000_000_000
            survivors: list[_Shard] = []
            for shard in shards:
                if shard.mtime_ns < cutoff and not shard.protected:
                    deletions.append(shard.path)
                else:
                    survivors.append(shard)
            shards = survivors

        # Size cap: plan closed-shard deletion oldest-first until the surviving
        # footprint is within budget. Protected active writers may temporarily
        # keep the directory over budget.
        if self._max_total_bytes > 0:
            total = sum(shard.size for shard in shards)
            if total > self._max_total_bytes:
                shards.sort(key=lambda shard: shard.mtime_ns)
                for shard in shards:
                    if total <= self._max_total_bytes:
                        break
                    if shard.protected:
                        continue
                    deletions.append(shard.path)
                    total -= shard.size
        return deletions

    @staticmethod
    def _unlink(path: Path) -> bool:
        """Best-effort shard delete; returns True on success."""
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.debug("metrics shard unlink %s failed: %s", path, exc)
            return False

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        return None
