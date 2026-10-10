"""A log mock that honours the consolidation lease.

``_consolidate`` fences every publication and the marker write on the lease token
it acquired, read back from the session's metadata line under the transcript lock
(``HistoryConsolidator._lease_is_current``). A bare ``MagicMock`` log grants a
lease it never records and then reports no metadata at all, so a publication that
should pass is refused and the test fails somewhere far from the cause — the
fixture, not the code under test.

:func:`lease_aware_log` keeps the in-memory metadata a real ``ConversationLog``
would persist, so the lease compare-and-set, the fence read and the release all
see the same record, without standing up a transcript on disk.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


def lease_aware_log(**overrides: Any) -> MagicMock:
    """A ``ConversationLog`` stand-in whose metadata writes are readable back.

    The lease compare-and-set, renewal and release all go through
    ``update_metadata_if``, whose guard runs against the metadata this mock
    holds. Keyword arguments override individual attributes for a caller that
    needs one of them stubbed.
    """
    metadata: dict[str, Any] = {}
    log = MagicMock()

    def update_metadata_if(key: str, fields: dict, guard: Any, **kwargs: Any) -> bool:
        if not guard(metadata):
            return False
        metadata.update(fields)
        return True

    log.update_metadata_if.side_effect = update_metadata_if
    log.get_metadata.side_effect = lambda key: dict(metadata)
    log.get_metadata_status.side_effect = lambda key: (dict(metadata), True)
    log.thread_transcript_identity.return_value = None
    for name, value in overrides.items():
        setattr(log, name, value)
    return log
