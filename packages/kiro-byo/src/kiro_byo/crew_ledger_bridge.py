"""Strict, opt-in KiroCrew 0.11 Work Ledger -> BYO ACP correlation adapter.

This is a *host-side integration helper*, NOT a ledger reader or a scheduler.
Only a Crew-owned, authenticated control-plane caller with a confirmed Work
Ledger binding may build metadata for a BYO session/prompt. BYO never treats
this metadata as permission, proof of acceptance, or a replay authorization.

KiroCrew work item ids are scoped to a conductor board, whose generation can
change after a purge. Make a deterministic, nonreversible task id from all
three fields; the item's `round` is NOT an Assignment ID.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from .host_work_link import validate_work_link

_ITEM_ID = re.compile(r"it_[0-9a-f]{8}\Z")
# Conductor slot and generation are *not* included as plain text in BYO traces.
_MAX_KEY = 256
_MAX_GENERATION = 128


class CrewLedgerCorrelationError(ValueError):
    """Crew control-plane evidence is insufficient; do not label the Run."""


def _text(value: Any, field: str, cap: int) -> str:
    if not isinstance(value, str) or not value or len(value) > cap or "\x00" in value:
        raise CrewLedgerCorrelationError(f"invalid {field}")
    return value


def _crew_record_projection(value: Any, fields: tuple[str, ...]) -> Mapping[str, Any] | None:
    """Read only contract fields, including slot-based Crew record objects.

    `vars(record)` is not available on slotted dataclasses and reads unrelated
    fields (potentially sensitive) into the adapter.  Keep the projection
    small and do not silently fabricate required attributes.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {field: value[field] for field in fields if field in value}
    return {field: getattr(value, field) for field in fields if hasattr(value, field)}


def work_link_from_crew_ledger(
    *,
    conductor: Mapping[str, Any],
    item: Mapping[str, Any],
    worker_session_key: str,
    binding: tuple[str, str] | None,
) -> dict[str, str]:
    """Construct a diagnostic link from verified Crew Work Ledger records.

    `binding` must come from Crew's trusted `work_ledger.read_binding(key)`
    executed by the Crew host, not from the agent or the ACP client. Recheck
    the binding immediately before dispatch. A stale, missing, closed or
    ambiguous binding is an error, NOT an excuse to infer a task from a prompt.

    Do not send any other Work Ledger fields (titles, prompts, acceptance,
    member identity, filesystem paths, or unredacted session keys) to BYO.
    """
    if not isinstance(conductor, Mapping) or not isinstance(item, Mapping):
        raise CrewLedgerCorrelationError("Crew Work Ledger records are required")
    slot = _text(conductor.get("slot_key"), "conductor slot", _MAX_KEY)
    if "/" in slot or "\\" in slot:
        raise CrewLedgerCorrelationError("invalid conductor slot")
    generation = _text(conductor.get("generation"), "board generation", _MAX_GENERATION)
    worker = _text(worker_session_key, "worker session key", _MAX_KEY)
    if "/" in worker or "\\" in worker:
        raise CrewLedgerCorrelationError("invalid worker session key")
    item_id = item.get("item_id")
    if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
        raise CrewLedgerCorrelationError("invalid Crew Work Ledger item id")
    if item.get("worker_session_key") != worker:
        raise CrewLedgerCorrelationError("Work Ledger item is not assigned to this worker")
    if binding != (slot, item_id):
        raise CrewLedgerCorrelationError("Crew Work Ledger binding is absent or changed")
    if item.get("state") in {"accepted", "rejected", "abandoned"}:
        raise CrewLedgerCorrelationError("terminal Crew work item cannot admit a new turn")
    if item.get("state") != "open":
        raise CrewLedgerCorrelationError("Crew work item is not in a recognized live state")
    # Avoid mixing records from unexpected schema revisions.
    if conductor.get("schema", 1) != 1 or item.get("schema", 1) != 1:
        raise CrewLedgerCorrelationError("unsupported Work Ledger schema")
    digest = hashlib.sha256(
        b"kirocrew-work-ledger-v1\x00"
        + slot.encode("utf-8") + b"\x00"
        + generation.encode("utf-8") + b"\x00"
        + item_id.encode("ascii")
    ).hexdigest()[:32]
    return validate_work_link({"taskId": f"crew-work:{digest}", "role": "worker"})



def byo_work_correlation_advertised(initialize_response: Any) -> bool:
    """Check the *actual* BYO initialize response; no backend-name guessing.

    KiroCrew must opt in only when the selected ACP child advertised this
    exact diagnostic-only contract, not merely because its model label is BYO.
    """
    if not isinstance(initialize_response, Mapping):
        return False
    agent = initialize_response.get("agentCapabilities")
    meta = agent.get("_meta") if isinstance(agent, Mapping) else None
    kiro = meta.get("kiro") if isinstance(meta, Mapping) else None
    info = kiro.get("workCorrelation") if isinstance(kiro, Mapping) else None
    return bool(
        isinstance(info, Mapping)
        and type(info.get("version")) is int
        and info["version"] == 1
        and info.get("promptMetadata") == "_meta.kiro.byoWorkLink"
        and info.get("bindMethod") == "_kiro/work/bind"
        and info.get("scope") == "diagnostic_only"
    )


def add_crew_work_link_to_prompt(
    params: Mapping[str, Any],
    *,
    backend: str,
    extension_advertised: bool,
    work_link: Mapping[str, str],
    allow_detach: bool = False,
) -> dict[str, Any]:
    """Return a new ACP prompt params object; never mutate the caller's data.

    Only the Crew Host may call this after verifying the Work Ledger binding.
    BYO-only extension; legacy/other ACP backend parameters remain unchanged.
    Fail closed if a BYO host planned to send an authoritative link but did
    not negotiate the extension, rather than silently misattribute the work.
    """
    output = dict(params)
    if backend != "kiro-byo":
        return output
    if not extension_advertised:
        raise CrewLedgerCorrelationError("BYO work-link capability not negotiated")
    try:
        link = validate_work_link(work_link)
    except (ValueError, TypeError) as exc:
        raise CrewLedgerCorrelationError("invalid BYO work link") from exc
    if not link:
        if not allow_detach:
            raise CrewLedgerCorrelationError("Work Ledger correlation requires a worker task link")
    elif not link.get("taskId") or link.get("role") != "worker":
        raise CrewLedgerCorrelationError("Work Ledger correlation requires a worker task link")
    old_meta = output.get("_meta")
    if old_meta is not None and not isinstance(old_meta, Mapping):
        raise CrewLedgerCorrelationError("existing ACP _meta is invalid")
    meta = dict(old_meta or {})
    old_kiro = meta.get("kiro")
    if old_kiro is not None and not isinstance(old_kiro, Mapping):
        raise CrewLedgerCorrelationError("existing ACP _meta.kiro is invalid")
    kiro = dict(old_kiro or {})
    if "byoWorkLink" in kiro and kiro["byoWorkLink"] != link and not (allow_detach and not link):
        raise CrewLedgerCorrelationError("conflicting prior BYO work link")
    kiro["byoWorkLink"] = link
    meta["kiro"] = kiro
    output["_meta"] = meta
    return output


def prepare_crew_work_ledger_prompt(
    params: Mapping[str, Any],
    *,
    backend: str,
    initialize_response: Any,
    worker_session_key: str,
    ledger: Any,
    expected_work_item: bool = False,
) -> dict[str, Any]:
    """BYO-only host adapter using Crew's *own* trusted Work Ledger read API.

    `ledger` is the Crew host's `kiro_crew.work_ledger` module (read_binding,
    read_conductor, read_work_item), never a BYO-owned store or model tool.
    This function does no IO for other backends and writes no Crew state.
    The Crew Host still owns transport, session authorization and scheduling.
    """
    plain = dict(params)
    if backend != "kiro-byo":
        return plain
    # The host must check the exact selected child's initialize response.
    if not byo_work_correlation_advertised(initialize_response):
        if expected_work_item:
            raise CrewLedgerCorrelationError("expected Crew work item but BYO has no negotiated extension")
        return plain  # Legacy Crew sessions continue untouched.
    worker = _text(worker_session_key, "worker session key", _MAX_KEY)
    binding = ledger.read_binding(worker)
    if binding is None:
        if expected_work_item:
            raise CrewLedgerCorrelationError("expected Crew work item but no active Worker binding")
        # A reused BYO ACP session may still have the previous Work Ledger
        # identity. An absent extension means "no change" to BYO. Explicitly
        # detach on a trusted *unbound* Worker turn to avoid stale attribution.
        return add_crew_work_link_to_prompt(
            plain, backend=backend, extension_advertised=True,
            work_link={}, allow_detach=True,
        )
    if not (isinstance(binding, tuple) and len(binding) == 2):
        raise CrewLedgerCorrelationError("Crew Work Ledger returned an invalid binding")
    conductor = ledger.read_conductor(binding[0], strict=True)
    item = ledger.read_work_item(binding[0], binding[1], strict=True)
    link = work_link_from_crew_ledger(
        conductor=_crew_record_projection(conductor, ("slot_key", "generation", "schema")),
        item=_crew_record_projection(item, ("item_id", "worker_session_key", "state", "schema")),
        worker_session_key=worker,
        binding=binding,
    )
    # An independent binding read catches a concurrent rebinding during
    # inspection. The Crew Host must still serialize the actual dispatch with
    # its own session ownership/turn lock; this is not a replacement for it.
    if ledger.read_binding(worker) != binding:
        raise CrewLedgerCorrelationError("Crew Worker binding changed while preparing prompt")
    return add_crew_work_link_to_prompt(
        plain, backend=backend, extension_advertised=True, work_link=link,
    )
