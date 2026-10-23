"""In-place capability edits for a MATE-owned config -- no fork, no lineage.

A crewmate's ``mates/agents/<id>.json`` is a severed copy with no live parent, so
the fork path of :mod:`kiro_crew.agent_capabilities` (a ``crew-<hex>`` draft,
lineage, parent-drift checks) does not apply; it would also drag the file back
into ``~/.kiro/agents/``. An edit here is a read-modify-write of that file: apply
the operations, pin ``name``, govern, write in place, behind one optimistic lock
(``stale_revision``) against two tabs. ``CapabilityService`` routes a mate
binding here.

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

    ``set`` adds or updates a row and ``remove`` drops it; a severed copy has no
    ``inherit``. Value rules mirror the fork path.
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


#: The capability sections whose rows are approvals.
_APPROVAL_SECTIONS = ("allowedTools", "autoApprove")


def _base_approvals() -> dict[str, set[str]]:
    """The worker base's own approvals by section (none if unreadable). Blocking."""
    from kiro_crew.agent_sdk import agent_specs
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    try:
        base = agent_specs.load_agent_spec(mate_agents_dir(), MATE_WORKER_AGENT)
    except agent_specs.AgentSpecUnreadable:
        return {section: set() for section in _APPROVAL_SECTIONS}
    allowed = base.get("allowedTools")
    raw_servers = base.get("mcpServers")
    servers: dict[str, Any] = raw_servers if isinstance(raw_servers, dict) else {}
    auto = {
        f"@{name}/{tool}"
        for name, entry in servers.items()
        if isinstance(entry, dict) and isinstance(entry.get("autoApprove"), list)
        for tool in entry["autoApprove"]
        if isinstance(tool, str)
    }
    return {
        "allowedTools": (
            {r for r in allowed if isinstance(r, str)} if isinstance(allowed, list) else set()
        ),
        "autoApprove": auto,
    }


def _revoked(path: Any) -> set[str]:
    """The approvals revoked on this crewmate's file; none when unreadable. Blocking."""
    from kiro_crew import mate_approval_revocations as revocations

    try:
        return set(revocations.revoked(path.stem))
    except revocations.RevocationsUnreadable:
        return set()


def _approval_rows(
    path: Any, rows: dict[str, dict], base: dict[str, set[str]] | None = None
) -> dict[str, dict[str, bool]]:
    """Each approval the worker carries or had, and whether it is on. Blocking.

    On when in the file, or granted by the base and not revoked; off when revoked,
    so the owner can switch a base grant off and turn a revoked one back on.
    """
    base = _base_approvals() if base is None else base
    revoked = _revoked(path)
    out: dict[str, dict[str, bool]] = {}
    for section in _APPROVAL_SECTIONS:
        state = dict.fromkeys(rows[section], True)
        for ref in sorted(base[section]):
            state.setdefault(ref, ref not in revoked)
        if section == "allowedTools":  # a revoked file approval, listed once
            others = base["allowedTools"] | base["autoApprove"] | set(rows["autoApprove"])
            for ref in sorted(revoked - others):
                state.setdefault(ref, False)
        out[section] = state
    return out


def _base_toggles(path: Any, body: Any) -> tuple[list[str], list[str]]:
    """``(base approvals *body* switches off, revoked ones it turns on)``. Blocking.

    A base approval is not a file row, so it moves only in the revocation record.
    """
    from kiro_crew.agent_capabilities import validate_request

    base = _base_approvals()
    revoked = _revoked(path)
    off: list[str] = []
    on: list[str] = []
    commit = isinstance(body, dict) and "preview_token" in body
    for op in validate_request(body, commit=commit)["operations"]:
        section, ref, action = op["section"], op["id"], op["action"]
        if section not in _APPROVAL_SECTIONS:
            continue
        if action == "remove" and ref in base[section]:
            off.append(ref)
        elif action == "set" and ref in revoked:
            on.append(ref)
    return list(dict.fromkeys(off)), list(dict.fromkeys(on))


def _view_rows(rows: dict[str, dict], approvals: dict[str, dict[str, bool]]) -> list[dict]:
    """The editor rows for section *rows*, every one ``state="local"``.

    An approval section lists every approval in *approvals*, on or off; ``prompt``
    and ``model`` always carry a row, valued ``""`` when unset.
    """
    output = []
    for section in SECTIONS:
        present = rows[section]
        keys = dict.fromkeys(approvals.get(section, present))
        if section in ("prompt", "model"):
            keys.setdefault(section)
        for key in keys:
            output.append(
                {
                    "section": section,
                    "id": key,
                    "label": key,
                    "state": "local",
                    "present": approvals[section][key] if section in approvals else key in present,
                    "value": present.get(key, "" if section in ("prompt", "model") else None),
                    "managed": False,
                    "shared_reference": section in ("skills", "resources"),
                }
            )
    return output


def get(member: str, catalog: dict[str, str]) -> dict:
    """The editor view for a mate: its own file, flagged ``mode="mate"``.

    Every row is ``state="local"``; ``revision`` locks the file bytes. The view
    goes through ``safe_view``, so transport credentials show as ``[REDACTED]``.
    """
    from kiro_crew.agent_capabilities import _rows, safe_view

    path, spec = _load_spec(member)
    rows = _rows(spec, catalog)
    output = _view_rows(rows, _approval_rows(path, rows))
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


def worker_summary(member: str, catalog: dict[str, str]) -> dict[str, list[dict]] | None:
    """What the crewmate's WORKER carries, as the rows Built From lists and counts.

    Read from the merged worker spec (``mate_worker.member_worker_spec``), not
    the file alone, so the summary and the Tools / MCP / Skills lists read one
    set. ``None`` when that spec is unreadable.
    """
    from kiro_crew import mate_worker

    try:
        spec = mate_worker.member_worker_spec(member)
    except Exception:  # noqa: BLE001 - a summary is display only
        return None

    def _strs(value: Any) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    def _row(section: str, key: str, value: Any = True) -> dict:
        return {
            "section": section,
            "id": key,
            # A skill carries no label: the pane names it from its id, a skill
            # URI through ``workerKitLabels.skillLabel``, as the kit preview does.
            "label": "" if section == "skills" else key,
            "state": "inherited",
            "present": True,
            "value": value,
            "managed": False,
            "shared_reference": section == "skills",
        }

    rows = [_row("tools", t) for t in dict.fromkeys(_strs(spec.get("tools")))]
    rows += [_row("allowedTools", t) for t in dict.fromkeys(_strs(spec.get("allowedTools")))]
    servers = spec.get("mcpServers")
    for name, entry in (servers if isinstance(servers, dict) else {}).items():
        if isinstance(name, str) and isinstance(entry, dict):
            rows.append(
                _row("mcpServers", name, {k: v for k, v in entry.items() if k != "autoApprove"})
            )
    names = {uri: key for key, uri in reversed(list(catalog.items()))}
    skills = [
        names[uri] if uri in names else uri
        for uri in _strs(spec.get("resources"))
        if uri in names or uri.startswith("skill://")
    ]
    skills += _strs(spec.get("skills"))
    rows += [_row("skills", key, key) for key in dict.fromkeys(skills)]
    return {"rows": rows}


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
            if before == after:
                continue
            if before is None:
                change = "added"
            elif after is None:
                change = "removed"
            else:
                change = "changed"
            impact.append(
                {
                    "member": member,
                    "section": section,
                    "id": key,
                    "change": change,
                    "approval_expanded": section in _APPROVAL_SECTIONS and after is not None,
                }
            )
    # A base approval switched off or on moves no file row but is an impact.
    path, _ = _load_spec(member)
    base_off, base_on = _base_toggles(path, body)
    approvals = _approval_rows(path, proposed_rows)
    for section in _APPROVAL_SECTIONS:
        for ref in base_off:
            if ref in approvals[section] and ref not in proposed_rows[section]:
                approvals[section][ref] = False
                impact.append(
                    {
                        "member": member,
                        "section": section,
                        "id": ref,
                        "change": "removed",
                        "approval_expanded": False,
                    }
                )
        for ref in base_on:
            if ref in approvals[section] and not approvals[section][ref]:
                approvals[section][ref] = True
                impact.append(
                    {
                        "member": member,
                        "section": section,
                        "id": ref,
                        "change": "added",
                        "approval_expanded": True,
                    }
                )
    # Rebuild the rows off the PROPOSED spec so the preview reflects the edit.
    view["rows"] = _view_rows(proposed_rows, approvals)
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
        # The worker base wins for an approval it grants itself, so a removed one
        # is recorded where the worker merge takes it back out. Revocations land
        # first and re-grants only after the file, so a failed write only narrows.
        from kiro_crew import mate_approval_revocations as revocations
        from kiro_crew.agent_capabilities import _rows

        removed, added = revocations.changed_approvals(
            _rows(spec, catalog), _rows(new_spec, catalog)
        )
        base_off, base_on = _base_toggles(path, body)
        regrants = list(dict.fromkeys([*added, *base_on]))
        revocations.record(path.stem, list(dict.fromkeys([*removed, *base_off])), [])
        if _digest(new_spec) == _digest(spec):
            # Nothing to write (a no-op edit, governance stripped the delta, or
            # only a base approval moved, which lives in the record).
            revocations.record(path.stem, [], regrants)
            return get(member, catalog)
        atomic_write(path, json.dumps(new_spec, indent=2) + "\n", restrict_to_owner=True)
        revocations.record(path.stem, [], regrants)
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
