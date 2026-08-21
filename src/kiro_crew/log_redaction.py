"""Log redaction filter for secret values and Bearer tokens.

Intercepts log records at creation time via ``logging.setLogRecordFactory``,
ensuring ALL handlers (including those added after installation) see only
redacted output. This module performs ZERO vault I/O — the caller resolves
any literal secret values and passes them in via ``install_log_redaction``.

Currently the only caller (``cli._setup_cli_logging``) passes an empty list,
so Bearer tokens are the only patterns redacted in practice. Wiring resolved
vault secret values into the filter at gateway boot is a follow-up.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Bearer tokens: RFC 6750 token68 charset (case-insensitive to catch
# serializations that lowercase the scheme, e.g. "bearer <token>").
_BEARER_RE = re.compile(r"Bearer [A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)

# Default patterns applied even when no vault secrets are provided.
DEFAULT_PATTERNS: list[re.Pattern[str]] = [_BEARER_RE]


class SecretRedactionFilter:
    """Redacts secret patterns and Bearer tokens from log records.

    Installed via ``logging.setLogRecordFactory`` so it intercepts EVERY
    record at creation — no handler can see plaintext regardless of when
    it was added. This class performs ZERO I/O at runtime — patterns are
    frozen at construction time.
    """

    def __init__(self, patterns: list[str]) -> None:
        """Initialize with literal secret values to redact.

        Parameters
        ----------
        patterns:
            List of literal strings (secret values) to redact. Each value
            is regex-escaped. Values shorter than 4 chars are skipped to
            avoid false positives.
        """
        escaped = [re.escape(p) for p in patterns if len(p) >= 4]
        self._secret_pattern: Optional[re.Pattern[str]] = (
            re.compile("|".join(escaped)) if escaped else None
        )

    def redact(self, text: str) -> str:
        """Replace secret values and Bearer tokens with [REDACTED].

        Zero I/O — only applies pre-compiled patterns.
        """
        if self._secret_pattern is not None:
            text = self._secret_pattern.sub("[REDACTED]", text)
        text = _BEARER_RE.sub("Bearer [REDACTED]", text)
        return text


# Module-level singleton set by install_log_redaction
_active_filter: Optional[SecretRedactionFilter] = None
_original_factory: Optional[Callable[..., logging.LogRecord]] = None


def _redacting_record_factory(*args, **kwargs) -> logging.LogRecord:  # type: ignore[no-untyped-def]
    """Wrap the standard LogRecord factory to redact msg at creation time.

    This runs BEFORE any handler sees the record, covering all handlers
    including those added after installation.  We materialize the final
    message (msg % args) and redact it, then clear args so the handler
    cannot re-format and leak.  Exception info is also redacted.
    """
    record = (
        _original_factory(*args, **kwargs)
        if _original_factory is not None
        else logging.LogRecord(*args, **kwargs)
    )
    if _active_filter is not None:
        # Materialize the full formatted message so deferred %s args
        # cannot bypass redaction at handler-format time.
        try:
            materialized = record.getMessage()
        except Exception:
            materialized = str(record.msg) if record.msg else ""
        record.msg = _active_filter.redact(materialized)
        record.args = None  # Already materialized — prevent double-format

        # Redact exc_text if already rendered, and format exc_info now
        # so Bearer tokens in tracebacks are caught.
        if record.exc_text:
            record.exc_text = _active_filter.redact(record.exc_text)
        if record.exc_info:
            import traceback

            tb_lines = traceback.format_exception(*record.exc_info)
            record.exc_text = _active_filter.redact("".join(tb_lines))
            record.exc_info = None  # Prevent handler from re-formatting
    return record


def install_log_redaction(patterns: list[str]) -> SecretRedactionFilter:
    """Install the redaction filter via ``logging.setLogRecordFactory``.

    This intercepts records at creation time — a record-level chokepoint
    that covers ALL handlers, including those added later. The module
    performs ZERO vault I/O — the caller resolves secret values and passes
    them as *patterns*.

    Parameters
    ----------
    patterns:
        Literal secret values to redact from log output. Values shorter
        than 4 chars are ignored (too likely to cause false positives).
    """
    global _active_filter, _original_factory

    filt = SecretRedactionFilter(patterns)
    _active_filter = filt

    # Wrap the record factory only once
    if _original_factory is None:
        _original_factory = logging.getLogRecordFactory()
        logging.setLogRecordFactory(_redacting_record_factory)

    return filt
