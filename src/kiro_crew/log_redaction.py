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


#: Marks an installed factory as a redacting wrapper, and carries the two things it
#: needs: ``base`` (the factory it displaced, to call and to give back) and ``filter``
#: (the live :class:`SecretRedactionFilter`, or ``None`` to pass records through).
#:
#: Both live ON THE INSTALLED FUNCTION rather than in a module global, and that is the
#: invariant the whole module rests on. A module global is resolved at CALL time and can
#: be rewritten — or zeroed by a reload — while the wrapper stays in ``logging``'s slot,
#: so the two disagree and the wrapper ends up calling something other than what it
#: displaced. Held on the function, each wrapper's base is fixed at install:
#:
#: * A CYCLE IS UNREPRESENTABLE. Wrapping our own wrapper makes it the inner link of a
#:   finite chain whose base is still the real original, so the worst case is redacting
#:   twice. Resolved through a global, the same wrap made the wrapper its own base and
#:   every later record recursed until ``RecursionError`` — on a PROCESS-GLOBAL slot, so
#:   no record was emitted again, redacted or not.
#: * OWNERSHIP IS READ OFF THE SLOT. ``install``/``uninstall`` ask the installed object
#:   what it is instead of consulting a sentinel that anything replacing the slot (a test
#:   floor, a reload, another copy of this module) silently invalidates.
_FACTORY_BASE = "_kirocrew_redaction_base"
_FACTORY_FILTER = "_kirocrew_redaction_filter"


def _redacting_factory(base: Callable[..., logging.LogRecord]) -> Callable[..., logging.LogRecord]:
    """Build a record factory that redacts everything *base* produces.

    The wrapper runs at record CREATION, before any handler sees it, which is what
    covers handlers added after installation. It materializes the final message
    (``msg % args``) and clears ``args`` so a handler cannot re-format and leak, and it
    renders ``exc_info`` into ``exc_text`` and clears it so a handler cannot re-render
    an unredacted traceback.
    """

    def factory(*args, **kwargs) -> logging.LogRecord:  # type: ignore[no-untyped-def]
        record = base(*args, **kwargs)
        filt: Optional[SecretRedactionFilter] = getattr(factory, _FACTORY_FILTER, None)
        if filt is None:
            return record

        # Materialize the full formatted message so deferred %s args cannot
        # bypass redaction at handler-format time.
        try:
            materialized = record.getMessage()
        except Exception:
            materialized = str(record.msg) if record.msg else ""
        record.msg = filt.redact(materialized)
        record.args = None  # Already materialized — prevent double-format

        if record.exc_text:
            record.exc_text = filt.redact(record.exc_text)
        if record.exc_info:
            import traceback

            tb_lines = traceback.format_exception(*record.exc_info)
            record.exc_text = filt.redact("".join(tb_lines))
            record.exc_info = None  # Prevent handler from re-formatting
        return record

    setattr(factory, _FACTORY_BASE, base)
    setattr(factory, _FACTORY_FILTER, None)
    return factory


def _installed_factory() -> Optional[Callable[..., logging.LogRecord]]:
    """The redacting wrapper currently in ``logging``'s slot, or ``None``.

    Only the TOP of the chain is inspected. A third party that wrapped ours answers
    ``None``, which is the right answer for both callers: the slot is not theirs to hand
    back, and installing a second wrapper over it is safe because chains terminate.

    Marker-based rather than identity-based so it survives this module being reloaded or
    imported a second time — either of which produces a new function object for a
    wrapper that is already installed and working.
    """
    factory = logging.getLogRecordFactory()
    return factory if hasattr(factory, _FACTORY_BASE) else None


def install_log_redaction(patterns: list[str]) -> SecretRedactionFilter:
    """Install the redaction filter via ``logging.setLogRecordFactory``.

    This intercepts records at creation time — a record-level chokepoint
    that covers ALL handlers, including those added later. The module
    performs ZERO vault I/O — the caller resolves secret values and passes
    them as *patterns*.

    Idempotent: when a redacting wrapper is already installed, its filter is
    re-pointed at *patterns* and the slot is left alone. Re-pointing the INSTALLED
    wrapper, rather than a global only this copy of the module can see, is what keeps
    the new patterns in force after a reload or a second import.

    Parameters
    ----------
    patterns:
        Literal secret values to redact from log output. Values shorter
        than 4 chars are ignored (too likely to cause false positives).
    """
    filt = SecretRedactionFilter(patterns)

    installed = _installed_factory()
    if installed is None:
        installed = _redacting_factory(logging.getLogRecordFactory())
        logging.setLogRecordFactory(installed)
    setattr(installed, _FACTORY_FILTER, filt)

    return filt


def uninstall_log_redaction() -> None:
    """Stop redacting, and give the record factory back.

    ``install_log_redaction`` needs a counterpart because the record factory is
    process-global: a caller that installs one and cannot remove it has changed
    every ``LogRecord`` for the life of the process, including dropping
    ``exc_info``, which the wrapper does on purpose so a handler cannot re-render
    an unredacted traceback.

    Idempotent, and it reverts only the wrapper at the TOP of the slot. If a third
    party wrapped ours, theirs is what the slot holds and ours is unreachable behind
    their closure, so nothing is touched and redaction CONTINUES until they remove
    their own wrapper. That is the safe direction for a control whose job is to keep
    secrets out of the log: the alternative is reaching into a chain this module does
    not own and unlinking whatever they added.
    """
    installed = _installed_factory()
    if installed is None:
        return
    setattr(installed, _FACTORY_FILTER, None)
    logging.setLogRecordFactory(getattr(installed, _FACTORY_BASE))
