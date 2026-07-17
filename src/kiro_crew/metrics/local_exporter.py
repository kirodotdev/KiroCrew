"""Local-first JSONL metric exporter (OSS-clean).

An OpenTelemetry ``MetricExporter`` that appends one JSON line per export cycle
to ``<dir>/metrics-YYYY-MM-DD-<pid>.jsonl``. This is KiroCrew's default metrics
sink: data stays on the local disk (default ``~/.kirocrew/metrics``) and never
leaves the host. Remote / OTLP egress is a separate, opt-in exporter (deferred).

Per-process shards: the filename includes the PID so each shard has a single
writer. Multiple telemetry-enabled processes (the gateway + spawned agents /
apps that each build their own recorder) therefore never append to the same
file, so a large serialized line cannot interleave with another process's write
and corrupt the "one JSON object per line" contract.

PRIVACY: attribute values are redacted at the ``MetricsRecorder``
facade before they reach the SDK, so the serialized data points carry no secrets
or PII. The directory (0o700) and shards (0o600) are created private -- matching
the ``~/.kirocrew`` file-permission convention -- so no other local user on a
shared host can read another user's metrics.

RETENTION: size/TTL rotation + pruning of old shards is deferred to a later wave;
today shards are per-process, per-day with no eviction floor.

OSS-CLEAN: depends only on ``opentelemetry`` + the stdlib.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry.sdk.metrics import Counter, Histogram, UpDownCounter
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    MetricExporter,
    MetricExportResult,
    MetricsData,
)

logger = logging.getLogger(__name__)


class JsonlMetricExporter(MetricExporter):
    """Append aggregated metrics as newline-delimited JSON under *directory*."""

    def __init__(self, directory: Path) -> None:
        # DELTA temporality: each export cycle writes the delta since the last
        # cycle rather than a growing cumulative snapshot. Daily aggregation over
        # the per-day JSONL shards then reduces to a clean element-wise sum of
        # bucket counts across cycles and PIDs, and stays correct across process
        # restarts and day boundaries (cumulative snapshots would double-count
        # and misattribute a PID's counts to the wrong day).
        super().__init__(
            preferred_temporality={
                Counter: AggregationTemporality.DELTA,
                UpDownCounter: AggregationTemporality.DELTA,
                Histogram: AggregationTemporality.DELTA,
            }
        )
        self._dir = Path(directory)

    def _target_file(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # PID in the name => single-writer shard (no cross-process interleave).
        return self._dir / f"metrics-{day}-{os.getpid()}.jsonl"

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        """Best-effort private-perm enforcement; never fail the export on it."""
        try:
            os.chmod(path, mode)
        except OSError as exc:
            logger.debug("chmod %s -> %o failed: %s", path, mode, exc)

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: object,
    ) -> MetricExportResult:
        """Serialize *metrics_data* to a single JSON line and append it."""
        try:
            line = metrics_data.to_json(indent=None)
            self._dir.mkdir(parents=True, exist_ok=True)
            # ~/.kirocrew convention: telemetry stays private (dir 0o700, file
            # 0o600). mkdir/open modes are masked by umask, so chmod explicitly.
            self._chmod(self._dir, 0o700)
            target = self._target_file()
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._chmod(target, 0o600)
        except Exception as exc:  # an exporter must never raise
            logger.warning("metrics JSONL export failed: %s", exc)
            return MetricExportResult.FAILURE
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        return None
