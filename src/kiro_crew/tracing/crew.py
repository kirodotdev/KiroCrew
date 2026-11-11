"""Ultra Mode and Bot attributes on a turn: which crew root it belongs to, and its item (§5.5).

A conductor (an Ultra Mode root, or a Bot thread) and every worker it dispatched
carry the same ``kirocrew.crew.root``, so one filter selects a whole run;
``kirocrew.crew.root_kind`` says which kind (``ultra`` or ``mate``). A worker
also carries the work item it is bound to, so ``kirocrew.work.item_id`` selects
one item across the conductor and the worker. Session keys are hashed; the item
id and round are raw (random and a counter). No goal, title or other ledger text.

The stamps come from the slot list's own rule (``crew_mode.stamp_rows``); this
module only reads a stamped row. The binding read touches disk, so its caller
runs :func:`bound_item` off the event loop.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)

ROOT_KIND_ULTRA = "ultra"
ROOT_KIND_MATE = "mate"


def root_of(slot_key: str, row: Mapping[str, Any]) -> tuple[str, str, str]:
    """``(root key, dispatcher key, root kind)`` of a stamped slot row; empty off every tree."""
    from kiro_crew import crew_mode

    worker_of = str(row.get(crew_mode.WORKER_FIELD) or "")
    if worker_of:
        return worker_of, str(row.get(crew_mode.DISPATCHER_FIELD) or ""), ROOT_KIND_ULTRA
    if row.get(crew_mode.ROOT_FIELD):
        return slot_key, "", ROOT_KIND_ULTRA
    mate_of, mate_by = crew_mode.mate_stamps_of(row)
    if mate_of:
        return mate_of, mate_by, ROOT_KIND_MATE
    if crew_mode._row_is_mate_root(row):
        return slot_key, "", ROOT_KIND_MATE
    return "", "", ""


def bound_item(worker_key: str) -> tuple[str, str, int] | None:
    """``(conductor key, item id, round)`` *worker_key* is bound to. Reads disk."""
    from kiro_crew import work_ledger

    try:
        binding = work_ledger.read_binding(worker_key)
        if binding is None:
            return None
        conductor, item_id = binding
        item = work_ledger.read_work_item(conductor, item_id)
        return conductor, item_id, int(getattr(item, "round", 0) or 0) if item else 0
    except Exception:
        logger.debug("trace work-item read failed", exc_info=True)
        return None


def attrs(
    hash_key: bytes | None,
    slot_key: str,
    row: Mapping[str, Any],
    item: tuple[str, str, int] | None,
) -> dict[str, Any]:
    """The crew attributes of a turn on *slot_key*; empty off every crew tree."""
    from kiro_crew.tracing import correlation

    root, dispatcher, kind = root_of(slot_key, row)
    if not root:
        return {}
    out: dict[str, Any] = {"kirocrew.crew.root_kind": kind}
    if hash_key is not None:
        out["kirocrew.crew.root"] = correlation.keyed_hash(hash_key, root)
        if dispatcher:
            out["kirocrew.crew.dispatcher"] = correlation.keyed_hash(hash_key, dispatcher)
    if item is not None:
        conductor, item_id, round_ = item
        out["kirocrew.work.item_id"] = item_id
        if round_ > 0:
            out["kirocrew.work.round"] = round_
        if hash_key is not None:
            out["kirocrew.work.conductor_hash"] = correlation.keyed_hash(hash_key, conductor)
    return correlation.clean(out)
