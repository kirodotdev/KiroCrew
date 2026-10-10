"""The per-file ceiling for dashboard uploads: ``dashboard.upload_max_mb``.

One reader for the chat composer's ``POST /api/upload/file`` and the Knowledge
page's ``POST /api/knowledge/ingest``, so each route enforces the figure the
dashboard advertises for it. The Knowledge upload is also bounded by
``knowledge.max_ingest_file_mb``, which ingestion enforces on the staged file,
so its ceiling is the smaller of the two. Video keeps its own, larger ceiling in
the upload handler.

Deliberately separate from ``handlers.files._MAX_UPLOAD_BYTES``: that constant
also bounds the in-memory READ paths (file-raw, file-download, office and sheet
preview), which raising the upload ceiling must not widen.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The ``dashboard.upload_max_mb`` field default, in bytes. Answered when the
#: config cannot be read, so a broken config never refuses every upload.
DEFAULT_UPLOAD_MAX_BYTES = 100 * 1024 * 1024


def upload_max_bytes() -> int:
    """``dashboard.upload_max_mb`` in bytes (loader-bounded to 1-512 MB).

    Read through the mtime-cached config load, so an edit takes effect on the
    next upload with no restart. Blocking -- callers run it off the event loop.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        return int(KiroCrewConfig.load().dashboard.upload_max_mb) * 1024 * 1024
    except Exception:
        logger.debug("upload_max_mb lookup failed; using the default ceiling", exc_info=True)
        return DEFAULT_UPLOAD_MAX_BYTES


def knowledge_ceiling_bytes(upload_max_mb: int, max_ingest_file_mb: float) -> int:
    """The Knowledge upload ceiling in bytes for the two config values: the
    smaller of ``dashboard.upload_max_mb`` and ``knowledge.max_ingest_file_mb``.

    Ingestion refuses a staged file over ``knowledge.max_ingest_file_mb``, so
    accepting a larger upload would only end the source in ``error``. A
    ``max_ingest_file_mb`` of 0 or less (cap disabled) leaves the upload
    ceiling alone.
    """
    ceiling = int(upload_max_mb) * 1024 * 1024
    if max_ingest_file_mb > 0:
        ceiling = min(ceiling, max(1, int(max_ingest_file_mb * 1024 * 1024)))
    return ceiling


def knowledge_upload_max_bytes() -> int:
    """``knowledge_ceiling_bytes`` for the loaded config, falling back to the
    upload default when the config cannot be read. Blocking -- callers run it
    off the event loop.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        cfg = KiroCrewConfig.load()
        return knowledge_ceiling_bytes(
            cfg.dashboard.upload_max_mb, float(cfg.knowledge.max_ingest_file_mb)
        )
    except Exception:
        logger.debug(
            "knowledge upload ceiling lookup failed; using the default ceiling", exc_info=True
        )
        return DEFAULT_UPLOAD_MAX_BYTES


def bytes_to_mb_figure(n: int) -> int | float:
    """``n`` bytes as the MB figure the dashboard shows: whole when exact, else
    one decimal place (never below 0.1)."""
    mb = n / (1024 * 1024)
    if mb == int(mb):
        return int(mb)
    return max(0.1, round(mb, 1))
