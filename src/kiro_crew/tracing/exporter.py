"""Local JSONL span sink: ``<data home>/traces/spans-YYYY-MM-DD-<pid>.jsonl``.

One JSON line per finished span, in the SDK's own ``ReadableSpan.to_json``
shape. It reuses the metrics sink's shard store (per-PID shards, owner-only
files, the ``telemetry.retention_days`` / ``telemetry.max_total_mb`` caps), so
traces stay on the host under the same rules as metrics. Imported only once
tracing is switched on.
"""

from __future__ import annotations

import logging
from typing import Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from kiro_crew.metrics.local_exporter import JsonlShardStore, shard_name_re

logger = logging.getLogger(__name__)


class _SpanShards(JsonlShardStore):
    SHARD_PREFIX = "spans"
    _SHARD_RE = shard_name_re("spans")


class JsonlSpanExporter(SpanExporter):
    """Append each exported span as one JSON line; never raises."""

    def __init__(self, directory, *, retention_days: int = 0, max_total_mb: int = 0) -> None:
        self._shards = _SpanShards(
            directory, retention_days=retention_days, max_total_mb=max_total_mb
        )

    def set_caps(self, *, retention_days: int, max_total_mb: int) -> None:
        """Apply new retention caps to the live sink, without a rebuild."""
        self._shards.set_caps(retention_days=retention_days, max_total_mb=max_total_mb)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            for span in spans:
                self._shards.append_line(span.to_json(indent=None))
        except Exception as exc:  # an exporter must never raise
            logger.warning("span JSONL export failed: %s", exc)
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        return None
