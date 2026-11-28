"""Local JSONL span sink: ``<data home>/traces/spans-YYYY-MM-DD-<pid>.jsonl``.

One JSON line per finished span, in the SDK's own ``ReadableSpan.to_json``
shape. It reuses the metrics sink's shard store (per-PID shards, owner-only
files, the ``telemetry.retention_days`` / ``telemetry.max_total_mb`` caps), so
traces stay on the host under the same rules as metrics. Imported only once
tracing is switched on.
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from kiro_crew.metrics.local_exporter import JsonlShardStore, shard_name_re
from kiro_crew.tracing.shard_cap import MAX_SPAN_SHARD_BYTES

logger = logging.getLogger(__name__)


class _SpanShards(JsonlShardStore):
    SHARD_PREFIX = "spans"
    _SHARD_RE = shard_name_re("spans")
    #: Rotate before the reader's own byte cap, so every written span is readable.
    MAX_SHARD_BYTES = MAX_SPAN_SHARD_BYTES

    def _rotate_target_if_needed(self, target, incoming_bytes):
        """Also close the shard when it would pass the trace reader's cap.

        ``max_total_mb`` bounds the folder; it does not bound one shard, so with
        the default 100 MB cap the writer made shards ``trace show`` skipped.
        The stricter of the two budgets wins here, and a single oversized
        record is still the minimum lossless overrun (see the base method).
        """
        if self.MAX_SHARD_BYTES > 0:
            try:
                size = target.stat().st_size
            except OSError:
                pass  # the base method handles a missing or unstat-able shard
            else:
                if size + incoming_bytes > self.MAX_SHARD_BYTES:
                    rotated = target.with_name(f"{target.stem}-{time.time_ns()}{target.suffix}")
                    try:
                        target.replace(rotated)
                        self._restrict_file(rotated)
                    except OSError as exc:
                        logger.warning(
                            "%s live-shard rotation for the reader cap failed: %s",
                            self.SHARD_PREFIX,
                            exc,
                        )
        super()._rotate_target_if_needed(target, incoming_bytes)


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
