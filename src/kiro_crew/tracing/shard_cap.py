"""The one byte budget a local span shard may occupy.

The JSONL sink rotates at this cap and ``kirocrew trace show`` reads only shards
within it. Keeping both sides on one constant is what makes every span the sink
writes a span the reader can find; a shard the reader must skip is an error,
never a "No trace matches" answer.
"""

from __future__ import annotations

MAX_SPAN_SHARD_BYTES = 50 * 1024 * 1024
