"""Idempotency classes for Salesforce write operations (closed set).

The connector-capability-manifest fixes ``retry.idempotency_class`` to a closed
four-value enum: ``base_sha_guard``, ``generate_ids_preallocation``,
``external_id_upsert``, ``none_verify_by_readback``. Of these, only two apply to
Salesforce write operations in the L1 core:

* ``external_id_upsert`` -- a PATCH to ``.../sobjects/{Object}/{ExtIdField}/
  {value}`` keyed on a caller-supplied External Id, which Salesforce treats as an
  upsert. A retried upsert with the same External Id converges on one record
  rather than creating a duplicate. (Corroborated.)
* ``none_verify_by_readback`` -- an operation with no vendor-side idempotency
  key, for which the only safe retry strategy is to read back and check whether
  the prior attempt took effect before retrying.

The first two enum values (``base_sha_guard``, ``generate_ids_preallocation``)
are GitHub-shaped mechanisms and are NOT claimed for Salesforce here; naming them
in the enum below keeps the closed set faithful to the manifest, but
:func:`idempotency_class_for` never returns them.

The invariant this module exists to protect: **a timeout-ambiguous write is
never retried in a way that could create a second record.** An operation with no
External Id key resolves to ``none_verify_by_readback`` -- verify first, then
retry -- and there is no third "just retry it" mechanism to reach for. This is
why the closed set has exactly these classes.
"""

from __future__ import annotations

import enum
from typing import Optional


class IdempotencyClass(str, enum.Enum):
    """The manifest's closed ``retry.idempotency_class`` enum."""

    BASE_SHA_GUARD = "base_sha_guard"
    GENERATE_IDS_PREALLOCATION = "generate_ids_preallocation"
    EXTERNAL_ID_UPSERT = "external_id_upsert"
    NONE_VERIFY_BY_READBACK = "none_verify_by_readback"


#: The full closed set, by vendor-neutral string.
IDEMPOTENCY_CLASSES = frozenset(c.value for c in IdempotencyClass)

#: The subset that applies to Salesforce writes in the L1 core.
_SALESFORCE_APPLICABLE = frozenset(
    {
        IdempotencyClass.EXTERNAL_ID_UPSERT.value,
        IdempotencyClass.NONE_VERIFY_BY_READBACK.value,
    }
)


def idempotency_class_for(*, has_external_id: bool) -> IdempotencyClass:
    """Resolve the idempotency class for a Salesforce write.

    * ``has_external_id=True`` -> :attr:`IdempotencyClass.EXTERNAL_ID_UPSERT`:
      the operation carries a caller-supplied External Id key, so an ambiguous
      retry converges on the same record.
    * ``has_external_id=False`` ->
      :attr:`IdempotencyClass.NONE_VERIFY_BY_READBACK`: there is no vendor-side
      key, so the ONLY safe retry is verify-then-retry. There is deliberately no
      branch that returns a "retry-anyway" class, because none exists in the
      closed set -- a timeout-ambiguous keyless write must never blindly retry
      into a duplicate.
    """

    if has_external_id:
        return IdempotencyClass.EXTERNAL_ID_UPSERT
    return IdempotencyClass.NONE_VERIFY_BY_READBACK


def is_salesforce_applicable(idempotency_class: str) -> Optional[bool]:
    """Whether a class value applies to Salesforce writes.

    Returns ``None`` for a value outside the closed set (unknown to the manifest
    enum), ``True``/``False`` for a known value. Used by tests to assert the core
    never claims a GitHub-shaped class for a Salesforce operation.
    """

    if idempotency_class not in IDEMPOTENCY_CLASSES:
        return None
    return idempotency_class in _SALESFORCE_APPLICABLE
