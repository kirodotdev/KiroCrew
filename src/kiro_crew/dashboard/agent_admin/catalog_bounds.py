"""The ``GET /api/models`` catalog cache's retention bound: which rows and fields a parsed ``--list-models`` catalog may keep."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        _CATALOG_CACHE_MAX_FIELDS_PER_ROW,
        logger,
        model_registry,
    )


def _bounded_catalog(models: list[dict]) -> list[dict]:
    """Clamp a parsed catalog to what the cache may safely retain.

    The retention point (:data:`_catalog_cache`) holds the list past the request,
    so it is bounded here before it is stored, through the SAME admission the
    window / advertised stores use — ``model_registry.ADVERTISED_MODELS_MAX_IDS``
    rows, each identifying field (``model_name`` / ``model_id``) at most
    ``ADVERTISED_MODEL_ID_MAX_CHARS`` — so the cache cannot serve an id those
    stores refused. An over-long id is REFUSED (its whole row skipped), never
    truncated, matching :func:`model_registry.admit_catalog_rows`'s own
    invariant; the picker reads the name, so a sliced name would name a different
    model or none.

    Each remaining scalar field (str / int / float / bool / None) is kept, with
    strings clamped to the id-length bound and a row holding at most
    :data:`_CATALOG_CACHE_MAX_FIELDS_PER_ROW`; a non-scalar field (nested object
    or array) is dropped — no reader of the cached list consumes one, and it is
    the unbounded edge the retention bound exists to close. Row ORDER is
    preserved (readers treat catalog order as meaningful). Rows and fields
    discarded for any reason are counted and logged once per snapshot.
    """
    max_rows = model_registry.ADVERTISED_MODELS_MAX_IDS
    max_chars = model_registry.ADVERTISED_MODEL_ID_MAX_CHARS
    bounded: list[dict] = []
    dropped_rows = 0
    dropped_fields = 0
    for row in models:
        if len(bounded) >= max_rows:
            dropped_rows += 1
            continue
        if not isinstance(row, dict):
            dropped_rows += 1
            continue
        # Refuse (skip) a row whose identifying field is over-long, rather than
        # slicing it into an id no store retained.
        ident = row.get("model_name") or row.get("model_id")
        if isinstance(ident, str) and len(ident) > max_chars:
            dropped_rows += 1
            continue
        kept: dict = {}
        for key, value in row.items():
            if len(kept) >= _CATALOG_CACHE_MAX_FIELDS_PER_ROW:
                dropped_fields += 1
                continue
            # The KEY is a retained string too: refuse (skip) an over-long one
            # rather than cache it, so no retained string is unbounded.
            if isinstance(key, str) and len(key) > max_chars:
                dropped_fields += 1
                continue
            if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
                kept[key] = value
            elif isinstance(value, str):
                kept[key] = value[:max_chars]
            else:
                # Non-scalar (nested dict/list): dropped — unbounded and unread.
                dropped_fields += 1
        bounded.append(kept)
    if dropped_rows or dropped_fields:
        logger.warning(
            "api_models: catalog cache dropped %d row(s) and %d field(s) over the "
            "retention bound (max_rows=%d, max_id_chars=%d)",
            dropped_rows,
            dropped_fields,
            max_rows,
            max_chars,
        )
    return bounded
