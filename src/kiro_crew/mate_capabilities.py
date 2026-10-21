"""In-place capability edits for a MATE-owned config -- no fork, no lineage.

A crewmate's config lives at ``mates/agents/<id>.json`` as a SEVERED copy: the
eager copy inlined its prompt to literal text and cut the ``file://`` link, so
the file *is* the agent and has no live parent. The ordinary Capabilities editor
(:mod:`kiro_crew.agent_capabilities`) is built around a fork/inheritance
lifecycle -- on first edit it forks a ``crew-<hex>`` draft, records
``forked_from``/``private_to`` lineage, and stages reviewable overrides against
the parent, guarded by parent-drift checks. NONE of that applies to a mate: there
is no parent to follow, and running a mate through the fork path drags it back
into ``~/.kiro/agents/`` (re-exposing it in the ``/set_mode`` picker) and orphans
the owned file.

So a mate edit is a plain read-modify-write of its own file: apply the editor's
operations to the spec, pin ``name``, run the governance sanitizer, write in
place. The binding is never repointed and no ``crew-`` draft is ever minted. The
ONE guard kept is an optimistic lock (``stale_revision``) so two tabs editing the
same mate file cannot clobber each other -- that defends concurrent access, not
parentage.

This module is the no-fork counterpart to ``agent_capabilities``'s fork path;
:class:`kiro_crew.agent_capabilities.CapabilityService` routes a mate binding
here instead of through ``_snapshot``/``_project``/``put``.

Blocking IO throughout; callers run it off the event loop.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable

from kiro_crew import agent_state
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import mate_agents_dir
from kiro_crew.mate_config import has_mate_config, mate_config_path

logger = logging.getLogger(__name__)

SECTIONS = agent_state.CAPABILITY_SECTIONS


def skill_catalog(member: str, catalog: Callable[[str], dict[str, str]]) -> dict[str, str]:
    """The skill catalog for *member*'s in-place path: *catalog* over its project dir.

    *catalog* is ``CapabilityService._catalog``, which takes the resolved project
    directory (``""`` when the binding has none).
    """
    from kiro_crew.config.loader import default_project_dir

    binding = KiroCrewConfig.load().agents.get(member)
    project = default_project_dir(binding.workspace) if binding else ""
    return catalog(str(Path(project).resolve()) if project else "")


def binding_is_mate(member: str) -> bool:
    """Whether *member*'s binding resolves to an owned ``mates/agents/<id>.json``.

    The predicate that routes an edit to the in-place path: the binding's
    ``kiro_agent`` equals the member id AND an owned mate file exists for it.
    A member still on a shared template (``kiro_agent`` != id, no owned file)
    takes the ordinary fork path, exactly as before.
    """
    try:
        binding = KiroCrewConfig.load().agents.get(member)
    except Exception:
        logger.debug("config unreadable deciding mate-ness of %r", member, exc_info=True)
        return False
    if binding is None:
        return False
    kiro_agent = getattr(binding, "kiro_agent", "")
    return bool(kiro_agent) and kiro_agent == member and has_mate_config(member)


def _digest(spec: Any) -> str:
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _revision(spec: Any) -> str:
    """Optimistic-lock token over the owned file's current bytes.

    Not a fork-path ``revision`` (there is no intent/parent to fold in): just a
    digest of the spec on disk, so a save computed against a stale view is
    refused (``stale_revision``) when another writer changed the file first.
    """
    return _digest(spec)


def _load_spec(member: str) -> tuple[Any, dict]:
    """Read the owned mate spec; refuse a path that escapes the mate dir."""
    from kiro_crew.agent import _read_spec_capped, _spec_path_is_safe
    from kiro_crew.agent_capabilities import CapabilityError

    root = mate_agents_dir()
    path = mate_config_path(member)
    if not _spec_path_is_safe(path, root):
        raise CapabilityError("invalid_spec")
    spec = _read_spec_capped(path)
    if not isinstance(spec, dict):
        raise CapabilityError("invalid_spec")
    return path, spec


def _apply_operations(
    rows: dict[str, dict], operations: list[dict], catalog: dict[str, str]
) -> None:
    """Apply the editor's operations to section *rows* IN PLACE.

    The mate-path op grammar. Deliberately simpler than the fork path's
    ``_project`` loop: there is no parent baseline, no ``intent`` overrides, and
    no ``inherit`` action (inherit-from-parent is meaningless for a severed
    copy). ``set`` adds/updates a row, ``remove`` drops it. Section value rules
    mirror the fork path so a mate accepts exactly what a shared agent does.
    """
    from kiro_crew.agent_capabilities import CapabilityError, _contains_mask, _retain_transport

    # The stored transports, before any op runs: a masked leaf the editor sends
    # back is restored from here (revision-bound by ``_edit``'s lock), never
    # written as the mask.
    stored = copy.deepcopy(rows.get("mcpServers", {}))
    for op in operations:
        section, key, action = op["section"], op["id"], op["action"]
        if section not in SECTIONS:
            raise CapabilityError("invalid_operation", 400)
        if section in ("prompt", "model") and key != section:
            raise CapabilityError("invalid_scalar_id", 400)
        if action == "inherit":
            # No parent to inherit from on a severed copy.
            raise CapabilityError("mate_has_no_parent", 400)
        if action == "remove":
            rows[section].pop(key, None)
            continue
        if action != "set":
            raise CapabilityError("invalid_operation", 400)
        value = copy.deepcopy(op.get("value"))
        if "retain_paths" in op:
            original = stored.get(key)
            value = _retain_transport(
                value, op["retain_paths"], original if isinstance(original, dict) else {}
            )
        elif "value" in op and _contains_mask(value):
            raise CapabilityError("redacted_value_not_writable", 400)
        if section == "mcpServers":
            if not isinstance(value, dict) or not value or "autoApprove" in value:
                raise CapabilityError("invalid_transport", 400)
            if not agent_state.capability_transport_fields_valid(value):
                raise CapabilityError("invalid_transport", 400)
            rows[section][key] = value
        elif section in ("tools", "allowedTools", "autoApprove"):
            if value is not True:
                raise CapabilityError("invalid_set_value", 400)
            rows[section][key] = True
        elif section == "skills":
            if value is not True or key not in catalog:
                raise CapabilityError("unknown_skill", 400)
            rows[section][key] = catalog[key]
        elif section == "resources":
            if value is True:
                value = key
            if not isinstance(value, str) or value != key:
                raise CapabilityError("invalid_resource", 400)
            rows[section][key] = key
        elif section in ("prompt", "model"):
            if not isinstance(value, str) or (section == "model" and not value.strip()):
                raise CapabilityError("invalid_scalar", 400)
            rows[section][key] = value
        else:
            raise CapabilityError("invalid_operation", 400)


def get(member: str, catalog: dict[str, str]) -> dict:
    """The editor view for a mate: its own file, flagged ``mode="mate"``.

    No parent descriptor, no inheritance rows -- every row reads ``state="local"``
    because a mate owns every value outright. The ``revision`` is the optimistic
    lock over the current file bytes. The whole view goes through ``safe_view``,
    the same redaction the fork path's ``_view`` uses, so a credential in a
    transport's ``env``/``headers``/``args``/``url`` is shown as ``[REDACTED]``.
    """
    from kiro_crew.agent_capabilities import _rows, safe_view

    _, spec = _load_spec(member)
    rows = _rows(spec, catalog)
    output = []
    for section in SECTIONS:
        present = rows[section]
        keys = dict.fromkeys(present)
        if section in ("prompt", "model"):
            keys.setdefault(section)
        for key in keys:
            output.append(
                {
                    "section": section,
                    "id": key,
                    "label": key,
                    "state": "local",
                    "present": key in present,
                    "value": present.get(key, "" if section in ("prompt", "model") else None),
                    "managed": False,
                    "shared_reference": section in ("skills", "resources"),
                }
            )
    return safe_view(
        {
            "schema_version": 1,
            "member": member,
            "mode": "mate",
            "revision": _revision(spec),
            "template": {"name": member, "scope": "mate", "available": True},
            "rows": output,
            "connections": [],
            "skills": [{"id": k, "label": k, "shared_reference": True} for k in catalog],
            "parent_changes": [],
            "runtime": {"status": "owned", "saved_revision": _revision(spec), "sessions": []},
            "backend_warning": None,
            "worker": worker_summary(member, catalog),
        }
    )


def worker_summary(member: str, catalog: dict[str, str]) -> dict[str, int] | None:
    """How much the crewmate's WORKER carries: tools, MCP servers and skills.

    Read from the worker's effective spec (the worker base merged with this
    file, ``mate_capability_catalog.worker_spec``), because the file holds only
    what the crewmate adds and a count of the file alone reads 0 for skills the
    worker does carry. A skill is a ``skill://`` resource, a ``skills`` entry, or
    a resource the catalog names. ``None`` when the worker spec is unreadable.
    """
    from kiro_crew.mate_capability_catalog import worker_spec

    try:
        spec = worker_spec(member)
    except Exception:  # noqa: BLE001 - a summary is display only
        return None

    def _strs(value: Any) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    uris = set(catalog.values())
    skills = {
        r for r in _strs(spec.get("resources")) if r.startswith("skill://") or r in uris
    } | set(_strs(spec.get("skills")))
    servers = spec.get("mcpServers")
    return {
        "tools": len(_strs(spec.get("tools"))),
        "mcpServers": len(servers) if isinstance(servers, dict) else 0,
        "skills": len(skills),
    }


def _edit(
    member: str, body: Any, catalog: dict[str, str], *, commit: bool = False
) -> tuple[dict, dict, str]:
    """Shared read-apply-materialize for preview and put. Returns (old, new, path-str)."""
    from kiro_crew.agent_capabilities import CapabilityError, _materialize, _rows, validate_request

    request = validate_request(body, commit=commit)
    path, spec = _load_spec(member)
    if request["revision"] != _revision(spec):
        raise CapabilityError("stale_revision")
    rows = _rows(spec, catalog)
    _apply_operations(rows, request["operations"], catalog)
    new_spec = _materialize(spec, rows, catalog)
    new_spec["name"] = member
    return spec, new_spec, str(path)


def preview(member: str, body: Any, catalog: dict[str, str]) -> dict:
    """Preview a mate edit: the resulting view, with a per-row impact diff."""
    from kiro_crew.agent_capabilities import _rows, safe_view
    from kiro_crew.platform.governance import sanitize_agent_config_governance

    old_spec, new_spec, _ = _edit(member, body, catalog)
    sanitize_agent_config_governance(new_spec)
    view = get(member, catalog)
    # Overlay the view onto the PROPOSED spec so the pane shows the edit result.
    proposed_rows = _rows(new_spec, catalog)
    old_rows = _rows(old_spec, catalog)
    impact = []
    for section in SECTIONS:
        for key in dict.fromkeys([*old_rows[section], *proposed_rows[section]]):
            before, after = old_rows[section].get(key), proposed_rows[section].get(key)
            if before != after:
                change = "added" if before is None else "removed" if after is None else "changed"
                impact.append(
                    {
                        "member": member,
                        "section": section,
                        "id": key,
                        "change": change,
                        "approval_expanded": section in ("allowedTools", "autoApprove")
                        and after is not None,
                    }
                )
    # Rebuild the rows off the PROPOSED spec so the preview reflects the edit.
    view_rows = []
    for section in SECTIONS:
        present = proposed_rows[section]
        keys = dict.fromkeys(present)
        if section in ("prompt", "model"):
            keys.setdefault(section)
        for key in keys:
            view_rows.append(
                {
                    "section": section,
                    "id": key,
                    "label": key,
                    "state": "local",
                    "present": key in present,
                    "value": present.get(key, "" if section in ("prompt", "model") else None),
                    "managed": False,
                    "shared_reference": section in ("skills", "resources"),
                }
            )
    view["rows"] = view_rows
    view["impact"] = impact
    view["preview_token"] = _revision(old_spec)
    view["withheld_approvals"] = []
    # ``view`` came masked from ``get``; the rebuilt rows carry the proposed raw
    # values, so mask again (idempotent on the already-masked fields).
    return safe_view(view)


def put(member: str, body: Any, catalog: dict[str, str]) -> dict:
    """Commit a mate edit: write ``mates/agents/<id>.json`` in place.

    No fork, no ``crew-`` draft, no ``forked_from``/``private_to`` lineage, no
    binding repoint. Governance-sanitised, ``name`` pinned, optimistic-locked on
    the file bytes. Held under the mate-dir spec lock so a concurrent reader
    never sees a half-written file.
    """
    from kiro_crew.agent import agents_spec_lock
    from kiro_crew.platform.governance import sanitize_agent_config_governance

    root = mate_agents_dir()
    with agents_spec_lock(root):
        path, spec = _load_spec(member)
        _, new_spec, _ = _edit(member, body, catalog, commit=True)
        sanitize_agent_config_governance(new_spec)
        if _digest(new_spec) == _digest(spec):
            # Nothing to write (a no-op edit, or governance stripped the delta).
            return get(member, catalog)
        atomic_write(path, json.dumps(new_spec, indent=2) + "\n", restrict_to_owner=True)
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="owner",
            operation="member_capabilities.mate_edit",
            outcome="success",
            source="dashboard",
            resources=member,
        )
    except Exception:
        logger.debug("SEL audit unavailable for the mate edit", exc_info=True)
    return get(member, catalog)
