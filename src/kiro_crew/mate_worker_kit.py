"""A crewmate's worker kit: abilities added to its file from another agent.

``rfc-mate-owned-agent-config`` §5.1. Adding abilities from another agent copies
that agent's servers, skills, resources and approvals into the crewmate's own
file — purely additively. Anything the crewmate already has (in its file or the
worker base) is left alone and not copied again. The agent's behaviour
directions (its prompt beyond the worker's own text) fill the file's one
``prompt`` slot, replacing what an earlier add put there; the worker appends that
slot after its source-defined prompt. The add is previewed, then applied. The file is governed, and the worker merge
governs again. Owner-only; blocking file I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.agent_sdk.mcp_refs import entry_reaches
from kiro_crew.mate_worker import (
    ADDITION_KEYS,
    _server_of,
    base_server_selections,
    collapse_selectors,
    covered_by,
    server_selectors,
)
from kiro_crew.user_json import copy_json_document

logger = logging.getLogger(__name__)

#: The kit keys an add unions in: the additions MINUS prompt, plus the ``tools``
#: selectors for the kit's servers. The prompt is not unioned: it is the one
#: behaviour-directions slot, which an add replaces (``_directions_of``).
_ADDED_KEYS = tuple(k for k in ADDITION_KEYS if k != "prompt")
KIT_KEYS = (*_ADDED_KEYS, "tools")


class WorkerKitError(Exception):
    """A worker-kit request that cannot be served; ``code`` is the wire code.

    ``detail`` names what the refusal is about (a field, a rule), for the
    sentence the dashboard shows; ``""`` when the code says it all.
    """

    def __init__(self, code: str, status: int = 409, *, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _kit_tools(spec: dict[str, Any], servers: Any) -> list[str]:
    """*spec*'s ``@server`` / ``@server/tool`` selectors for *servers*; ``*`` reads
    as each whole (never copied: it would also select every built-in)."""
    return server_selectors(spec, list(servers))


def _kit_of(spec: dict[str, Any]) -> dict[str, Any]:
    """The kit parts of a spec, normalised: missing parts read empty. The prompt
    is not a kit part: an add reads it separately (``_directions_of``)."""
    servers = spec.get("mcpServers")
    kit: dict[str, Any] = {
        "mcpServers": (
            {k: copy_json_document(v) for k, v in servers.items() if isinstance(v, dict)}
            if isinstance(servers, dict)
            else {}
        ),
    }
    for key in ("resources", "skills", "allowedTools"):
        value = spec.get(key)
        kit[key] = [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
    kit["tools"] = _kit_tools(spec, kit["mcpServers"])
    return kit


def template_kit(template: str) -> dict[str, Any]:
    """*template*'s kit, read once from the user-level agents folder."""
    from kiro_crew.mate_config import MateConfigError, _resolved_source_spec

    try:
        return _kit_of(_resolved_source_spec(template))
    except MateConfigError as exc:
        raise WorkerKitError("template_unreadable") from exc


def _owned_file(member: str) -> tuple[Path, dict[str, Any]]:
    """The crewmate's own file and its parsed spec; refuses a binding that owns none."""
    from kiro_crew.agent import _read_spec_capped, _spec_path_is_safe
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.mate_capabilities import binding_is_mate
    from kiro_crew.mate_config import mate_config_path

    if not binding_is_mate(member):
        raise WorkerKitError("not_a_mate_file")
    path = mate_config_path(member)
    if not _spec_path_is_safe(path, paths_mod.mate_agents_dir()):
        raise WorkerKitError("invalid_spec")
    spec = _read_spec_capped(path)
    if not isinstance(spec, dict):
        raise WorkerKitError("invalid_spec")
    return path, spec


def _request(body: Any) -> tuple[str, str]:
    if not isinstance(body, dict):
        raise WorkerKitError("invalid_json", 400)
    template = body.get("template")
    revision = body.get("revision", "")
    if not isinstance(template, str) or not template.strip():
        raise WorkerKitError("template_required", 400)
    if not isinstance(revision, str):
        raise WorkerKitError("invalid_request", 400)
    return template.strip(), revision


def _present_servers(spec: dict[str, Any], base: dict[str, Any] | None) -> set[str]:
    """Server names the crewmate already has — in its file or the worker base."""
    present = set(_kit_of(spec)["mcpServers"])
    if base is not None:
        present |= set(_kit_of(base)["mcpServers"])
    return present


def _present_items(spec: dict[str, Any], base: dict[str, Any] | None, key: str) -> set[str]:
    """``resources``/``skills``/``allowedTools`` the crewmate already has."""
    present = set(_kit_of(spec)[key])
    if base is not None:
        present |= set(_kit_of(base)[key])
    return present


def _skipped(
    tkit: dict[str, Any], spec: dict[str, Any], base: dict[str, Any] | None
) -> list[dict[str, str]]:
    """One row per kit item the add leaves out because it is already present."""
    rows: list[dict[str, str]] = []
    present_servers = _present_servers(spec, base)
    for name in sorted(set(tkit["mcpServers"]) & present_servers):
        rows.append({"section": "mcpServers", "id": name, "change": "skipped"})
    for key in ("resources", "skills"):
        have = _present_items(spec, base, key)
        for item in sorted(set(tkit[key]) & have):
            rows.append({"section": key, "id": item, "change": "skipped"})
    # A grant-only tool setting the worker already grants (``_without_held_grants``).
    for path in sorted(tkit.get("held_grants") or []):
        rows.append({"section": "toolsSettings", "id": path, "change": "skipped"})
    return rows


def _added(
    tkit: dict[str, Any],
    spec: dict[str, Any],
    base: dict[str, Any] | None,
    new_spec: dict[str, Any],
) -> list[dict[str, str]]:
    """One row per kit item the add writes (net-new, not already present)."""
    rows: list[dict[str, str]] = []
    present_servers = _present_servers(spec, base)
    for name in sorted(set(tkit["mcpServers"]) - present_servers):
        rows.append({"section": "mcpServers", "id": name, "change": "added"})
    retained = set(_kit_of(spec)["mcpServers"]) - set(_kit_of(base or {})["mcpServers"])
    existing = set(server_selectors(spec, retained))
    for ref in server_selectors(new_spec, retained):
        if ref not in existing and not covered_by(ref, existing):
            rows.append({"section": "tools", "id": ref, "change": "added"})
    # Tools the add selects on a server the base declares, so the owner sees them.
    if base is not None:
        before = set(base_server_selections(base, spec, "tools"))
        for ref in base_server_selections(base, new_spec, "tools"):
            if ref not in before and not covered_by(ref, before):
                rows.append({"section": "tools", "id": ref, "change": "added"})
    for key in ("resources", "skills"):
        have = _present_items(spec, base, key)
        for item in sorted(set(tkit[key]) - have):
            rows.append({"section": key, "id": item, "change": "added"})
    return rows


def _allow_rules(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``allow`` rules in *spec*'s ``permissions`` block."""
    perms = spec.get("permissions")
    rules = perms.get("rules") if isinstance(perms, dict) else None
    return [
        rule
        for rule in (rules if isinstance(rules, list) else [])
        if isinstance(rule, dict) and rule.get("effect") == "allow"
    ]


def _permission_refs(spec: dict[str, Any], base: dict[str, Any] | None) -> set[str]:
    """The ``allowedTools`` refs *spec*'s ``mcp`` and built-in allow rules land as on the worker.

    The worker merge writes an allow its list can express as that list's own
    entries: an ``mcp`` one as its refs (``mate_spec_policy.mcp_allow_refs``), an
    unscoped built-in one as its tool name (``kas_tool_view.builtin_allow_tool``),
    unless the list already grants it (``mate_spec_policy._carry_mcp_allow``,
    ``_carry_builtin_allow``). A rule the worker *base* already grants, and a ref
    the base already lists, are left out the same way.
    """
    from kiro_crew.agent_sdk.kas_tool_view import (
        allow_granted_by_spec,
        builtin_allow_tool,
        permission_allow_derivable,
    )
    from kiro_crew.mate_spec_policy import mcp_allow_refs

    held = base or {}
    listed = held.get("allowedTools")
    have = {t for t in listed if isinstance(t, str)} if isinstance(listed, list) else set()
    refs: set[str] = set()
    for rule in _allow_rules(spec):
        carried = mcp_allow_refs(rule)
        if carried is None:
            name = builtin_allow_tool(rule)
            carried = [name] if name else None
        if carried is None or not permission_allow_derivable(rule):
            continue
        if not allow_granted_by_spec(rule, held):
            refs.update(ref for ref in carried if ref not in have)
    return refs


def _listed_refs(spec: dict[str, Any]) -> set[str]:
    """Approval selectors *spec* lists: ``allowedTools`` and per-server ``autoApprove``."""
    value = spec.get("allowedTools")
    refs = {t for t in value if isinstance(t, str)} if isinstance(value, list) else set()
    servers = spec.get("mcpServers")
    for name, entry in (servers if isinstance(servers, dict) else {}).items():
        auto = entry.get("autoApprove") if isinstance(entry, dict) else None
        refs |= (
            {f"@{name}/{t}" for t in auto if isinstance(t, str)}
            if isinstance(auto, list)
            else set()
        )
    return refs


def _approval_refs(spec: dict[str, Any], base: dict[str, Any] | None = None) -> set[str]:
    """Approval selectors from file-level, per-server and ``permissions`` grants.

    The listed ones are :func:`_listed_refs`; the ``permissions`` ones are
    :func:`_permission_refs`, less what the worker *base* already grants.
    """
    return _listed_refs(spec) | _permission_refs(spec, base)


def _approval_changes(
    before: dict[str, Any], after: dict[str, Any], base: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """One row per auto-approval grant the add writes, so the owner sees it before applying.

    Diffed from the file before and after (the same written result ``apply``
    saves), so the preview cannot list fewer grants than the file will hold. Three
    channels count: an ``allowedTools`` entry, a tool in a server entry's own
    ``autoApprove``, and an ``mcp`` or unscoped built-in allow in ``permissions``,
    which the worker merge writes into the worker's ``allowedTools`` unless the
    worker *base* already grants it. Each lets the worker run that tool with no
    prompt, so all three are listed as ``allowedTools`` rows (``@server`` /
    ``@server/tool``, or a built-in tool name such as ``use_subagent``).
    """

    return [
        {"section": "allowedTools", "id": ref, "change": "added"}
        for ref in sorted(_approval_refs(after, base) - _approval_refs(before, base))
    ]


def _unlisted_allow_added(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Whether *after* holds a ``permissions`` allow that *before* did not, of a
    shape no ``allowedTools`` ref expresses.

    Such an allow grants on its own: KAS keeps a scoped or excluded one in the
    block, and Crew's gate answers an unscoped shell or file one itself
    (``kas_permissions.crew_answered_builtins``). An allow the list can express
    is counted through its refs instead (:func:`_permission_refs`).
    """
    from kiro_crew.agent_sdk.kas_tool_view import permission_allow_derivable

    held = {_digest(rule) for rule in _allow_rules(before)}
    return any(
        _digest(rule) not in held and not permission_allow_derivable(rule)
        for rule in _allow_rules(after)
    )


def _runs_unasked(
    before: dict[str, Any], after: dict[str, Any], base: dict[str, Any] | None = None
) -> bool:
    """Whether the add lets a tool the worker can use run without a prompt.

    A newly selected tool that intersects an approval the worker will hold
    counts. A selector on a server the worker *base* declares counts too, against
    the file's approvals and the base's own, since the worker inherits both. So
    does a new ``permissions`` allow that grants with no ``allowedTools`` ref
    (:func:`_unlisted_allow_added`), and a new unscoped built-in allow, whose tool
    name lands in the worker's list; ``deny`` and ``ask`` rules never count.
    """
    if _unlisted_allow_added(before, after):
        return True
    new_grants = _permission_refs(after, base) - _permission_refs(before, base)
    if any(not ref.startswith("@") for ref in new_grants):
        return True
    existing = set(_kit_of(before)["tools"])
    grants = _approval_refs(after, base)
    new = [
        ref
        for ref in _kit_of(after)["tools"]
        if ref not in existing and not any(entry_reaches(entry, ref) for entry in existing)
    ]
    if base is not None:
        grants |= _approval_refs(base)
        had = set(base_server_selections(base, before, "tools"))
        new += [
            ref
            for ref in base_server_selections(base, after, "tools")
            if ref not in had and not any(entry_reaches(entry, ref) for entry in had)
        ]
    return any(
        any(entry_reaches(grant, ref) or entry_reaches(ref, grant) for grant in grants)
        for ref in new
    )


def _exclusion_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    """One row per tool the add newly excludes, so the owner sees it before applying.

    A template's ``excludedTools`` are restrictions the add carries into the
    file; a newly-added exclusion is a real change to surface.
    """

    def _refs(spec: dict[str, Any]) -> set[str]:
        value = spec.get("excludedTools")
        return {t for t in value if isinstance(t, str)} if isinstance(value, list) else set()

    return [
        {"section": "excludedTools", "id": ref, "change": "added"}
        for ref in sorted(_refs(after) - _refs(before))
    ]


def _muted_server_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    """One row per server the add switches off, so the owner sees it before applying.

    A switch-off is a ``{"disabled": true}`` entry the file did not hold before:
    the template's mute on a server the worker base declares.
    """
    old = before.get("mcpServers")
    old_names = set(old) if isinstance(old, dict) else set()
    new = after.get("mcpServers")
    return [
        {"section": "disabledServers", "id": name, "change": "added"}
        for name, entry in sorted((new if isinstance(new, dict) else {}).items())
        if name not in old_names and entry == {"disabled": True}
    ]


def view(member: str) -> dict[str, Any]:
    """The worker-kit state the Add-abilities surface shows.

    Purely additive: there is no recorded template and no "additions since" to
    report — a crewmate's file is the sum of everything added to it.
    """
    _, spec = _owned_file(member)
    return {
        "member": member,
        "revision": _digest(spec),
    }


def _without_orphan_selectors(refs: Any, added_server_names: set[str]) -> list[str]:
    """Discard references to reintroduced servers instead of restoring stale grants."""
    return [
        ref
        for ref in (refs if isinstance(refs, list) else [])
        if isinstance(ref, str) and _server_of(ref) not in added_server_names
    ]


def _merged_selectors(
    spec: dict[str, Any],
    base: dict[str, Any] | None,
    tkit: dict[str, Any],
    added_server_names: set[str],
) -> list[str]:
    """Select the file's tools and the template's tools on new or retained own servers."""
    existing_servers = _kit_of(spec)["mcpServers"]
    raw = spec.get("tools")
    explicit = (
        [ref for ref in raw if isinstance(ref, str) and ref != "*"] if isinstance(raw, list) else []
    )
    base_names = set(_kit_of(base or {})["mcpServers"])
    # The file's ``*`` is expanded over its own servers only: written out as an
    # explicit ``@name`` for a server the base declares, it would select that
    # whole server on the worker (``mate_worker.base_server_selections``).
    existing = _without_orphan_selectors(
        [*explicit, *server_selectors(spec, set(existing_servers) - base_names)],
        added_server_names,
    )
    retained = set(existing_servers) - base_names
    added = [ref for ref in tkit["tools"] if _server_of(ref) in added_server_names]
    restored = [ref for ref in tkit["tools"] if _server_of(ref) in retained]
    # The template's selectors on a server the base declares: the add skips that
    # server's entry (the base's wins), not what the template selects on it.
    on_base = (
        base_server_selections(base, {**spec, "tools": tkit["tools"]}, "tools") if base else []
    )
    return collapse_selectors([*existing, *added, *restored, *on_base])


def _plan(member: str, body: Any) -> tuple[Path, dict[str, Any], dict[str, Any], str, dict]:
    """Union the template's kit into the crewmate's file, additively.

    Servers/skills/resources/approvals the file or the worker base already holds
    are skipped. The agent's behaviour directions, when it has any, replace the
    file's ``prompt``. The template's limits are carried by
    the same rule as create (``mate_config._carried_restrictions``): its
    ``excludedTools`` and its per-server ``disabledTools`` join the file's
    ``excludedTools``, and a base server it switches off is written as
    ``{"disabled": true}`` unless the file holds its own entry for that server.
    Its ``hooks``, ``permissions`` and ``toolsSettings`` are layered on exactly
    (``mate_spec_policy``); they run on the crewmate's worker only, so the add is
    refused, naming the field, when one cannot be kept exactly or when the
    crewmate's backend cannot apply them. A grant-only tool setting the Bot's
    worker already grants is left out first (:func:`_without_held_grants`) and
    recorded in the kit's ``held_grants``.
    """
    from kiro_crew.mate_config import MateConfigError, _carried_restrictions, _resolved_source_spec
    from kiro_crew.mate_config_migration import uncarried_restrictions
    from kiro_crew.platform.governance import sanitize_agent_config_governance

    template, _revision = _request(body)
    path, spec = _owned_file(member)
    tkit = template_kit(template)
    try:
        source = _resolved_source_spec(template)
    except MateConfigError as exc:
        raise WorkerKitError("template_unreadable") from exc
    source, tkit["held_grants"] = _without_held_grants(source, spec, path.stem)
    lost = uncarried_restrictions(source)
    if lost:
        # A restriction the worker would not keep exactly.
        raise WorkerKitError("template_restrictions_uncarried", detail=", ".join(lost))

    base = _worker_base_or_none()
    new_spec = copy_json_document(spec)

    # Servers: add only names the file and base do not already have.
    present_servers = _present_servers(spec, base)
    existing_servers = new_spec.get("mcpServers")
    if not isinstance(existing_servers, dict):
        existing_servers = {}
    merged_servers = dict(existing_servers)
    added_server_names: set[str] = set()
    for name, entry in tkit["mcpServers"].items():
        if name in present_servers:
            continue  # already present: leave it, do not copy
        merged_servers[name] = copy_json_document(entry)
        added_server_names.add(name)
    if merged_servers:
        new_spec["mcpServers"] = merged_servers

    # resources / skills / allowedTools: union, skipping what is already present.
    for key in ("resources", "skills"):
        have = _present_items(spec, base, key)
        existing = [v for v in (new_spec.get(key) or []) if isinstance(v, str)]
        added = [v for v in tkit[key] if v not in have]
        if existing or added:
            new_spec[key] = list(dict.fromkeys([*existing, *added]))

    # Approvals ride with servers the add writes, and with the template's selectors
    # on a server the base declares; retained servers keep theirs.
    existing_allowed = _without_orphan_selectors(spec.get("allowedTools"), added_server_names)
    added_allowed = [
        *(ref for ref in tkit["allowedTools"] if _server_of(ref) in added_server_names),
        *(
            base_server_selections(
                base, {**spec, "allowedTools": tkit["allowedTools"]}, "allowedTools"
            )
            if base
            else []
        ),
    ]
    if "allowedTools" in spec or added_allowed:
        new_spec["allowedTools"] = list(dict.fromkeys([*existing_allowed, *added_allowed]))

    new_spec["tools"] = _merged_selectors(spec, base, tkit, added_server_names)

    # Behaviour directions: the one slot the worker appends after its own prompt.
    # An agent with directions of its own replaces it; one without leaves it.
    directions = _directions_of(source, base) if _names_a_prompt(template) else ""
    tkit["prompt"] = directions
    if directions:
        new_spec["prompt"] = directions

    # The template's limits are restrictions, not kit: they join what the file
    # already restricts and are never dropped, even on a server the add skips.
    try:
        carried = _carried_restrictions(member, source)
    except Exception as exc:  # noqa: BLE001 - no write without the template's limits
        logger.warning("template limits unreadable for %s", member, exc_info=True)
        raise WorkerKitError("template_unreadable") from exc
    excluded = [
        *(t for t in (new_spec.get("excludedTools") or []) if isinstance(t, str)),
        *(t for t in (carried.get("excludedTools") or []) if isinstance(t, str)),
    ]
    if excluded:
        new_spec["excludedTools"] = list(dict.fromkeys(excluded))
    muted = carried.get("mcpServers") or {}
    if muted:
        servers = new_spec.get("mcpServers")
        servers = dict(servers) if isinstance(servers, dict) else {}
        for name in muted:
            # A server entry the file holds is the owner's own choice: leave it.
            if name not in servers:
                servers[name] = {"disabled": True}
        new_spec["mcpServers"] = servers

    _carry_policy(member, new_spec, source)
    new_spec["name"] = member
    sanitize_agent_config_governance(new_spec)
    return path, spec, new_spec, template, tkit


def _without_held_grants(
    source: dict[str, Any], spec: dict[str, Any], file_id: str
) -> tuple[dict[str, Any], list[str]]:
    """*source* less each grant-only tool setting the Bot's worker already grants,
    and the ``<tool>.<key>`` path of each one left out.

    The keys are ``mate_spec_policy.HELD_GRANT_SETTINGS``, each proved against the
    worker the Bot runs once the add lands: the base and *source* merged without
    them, less the tools the Bot's file *spec* excludes and the approvals its
    owner revoked (``mate_approval_revocations``, read for *file_id*), so the
    worker behaves as the agent declares and no prompt appears or disappears.
    Nothing is left out when the base or the revoked approvals cannot be read,
    that merge refuses something else, or one of those tools is written under
    two kiro-cli spellings; the gates after this then name the key with the rest.
    """
    from kiro_crew import mate_approval_revocations as revocations
    from kiro_crew.mate_spec_policy import (
        SpecPolicyUncarried,
        grant_settings_in,
        held_grant_settings,
        without_settings,
    )
    from kiro_crew.mate_worker import merge_worker_spec, worker_additions

    candidates = grant_settings_in(source)
    base = _worker_base_or_none() if candidates else None
    if base is None:
        return source, []
    trial = without_settings(source, candidates)
    # The Bot's own exclusions narrow the worker as the agent's do (``_plan`` joins both).
    excluded = [
        t
        for value in (trial.get("excludedTools"), spec.get("excludedTools"))
        for t in (value if isinstance(value, list) else [])
        if isinstance(t, str)
    ]
    if excluded:
        trial["excludedTools"] = list(dict.fromkeys(excluded))
    try:
        # Stripped after the merge, as ``mate_worker.resolve_worker_spec`` does.
        merged = revocations.strip(
            merge_worker_spec(base, worker_additions(base, trial)), revocations.revoked(file_id)
        )
    except (SpecPolicyUncarried, revocations.RevocationsUnreadable):
        return source, []
    held = held_grant_settings(source, merged)
    if not held:
        return source, []
    return without_settings(source, held), [f"{tool}.{key}" for tool, key in held]


def _carry_policy(member: str, new_spec: dict[str, Any], source: dict[str, Any]) -> None:
    """Layer *source*'s hooks, permissions and tool settings into the file *new_spec*.

    Refused, naming what, when they cannot be kept exactly on the worker
    (``template_restrictions_uncarried``) or when the crewmate's worker backend
    applies none of them (``template_policy_backend_unsupported``).
    """
    from kiro_crew.mate_spec_policy import (
        SpecPolicyUncarried,
        backend_refusal,
        layer_policy,
        policy_of,
        without_held,
    )
    from kiro_crew.mate_worker import merge_worker_spec, worker_additions

    base = _worker_base_or_none()
    carried = policy_of(source)
    if base is not None:
        # What the worker base already holds (Crew's own hooks, the user's) reaches
        # the worker without the file: only what the template adds is carried.
        carried = without_held(carried, base)
    if not carried:
        return
    try:
        layer_policy(new_spec, carried)
    except SpecPolicyUncarried as exc:
        raise WorkerKitError(
            "template_restrictions_uncarried", detail=", ".join(exc.fields)
        ) from exc
    held = policy_of(new_spec)
    if backend_refusal(held, _crewmate_backend(member, held)):
        raise WorkerKitError("template_policy_backend_unsupported", detail=", ".join(sorted(held)))
    if base is not None:
        try:
            merge_worker_spec(base, worker_additions(base, new_spec))
        except SpecPolicyUncarried as exc:
            raise WorkerKitError(
                "template_restrictions_uncarried", detail=", ".join(exc.fields)
            ) from exc


def _crewmate_backend(member: str, held: dict[str, Any]) -> str:
    """The backend Crew bot *member*'s worker runs on once its file holds *held*.

    Judged on the spec being written: a Bot that follows the Settings harness
    and gains policy runs on KAS (``crewmate_harness.worker_backend_for_spec``).
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.crewmate_harness import worker_backend_for_spec

    try:
        return worker_backend_for_spec(KiroCrewConfig.load(), member, bool(held))
    except Exception as exc:  # noqa: BLE001 - an unstartable pin applies nothing
        logger.warning("backend of %s unresolved; refusing its policy", member, exc_info=True)
        raise WorkerKitError(
            "template_policy_backend_unsupported", detail=", ".join(sorted(held))
        ) from exc


def _policy_ids(spec: dict[str, Any]) -> set[tuple[str, str]]:
    """``(section, id)`` per hook, permission rule and tool setting *spec* holds.

    A hook or rule is keyed on its whole entry, the identity the layering dedupes
    on, so two that share a matcher or a match each get a row; the id is a label
    plus that entry's digest.
    """
    from kiro_crew.mate_spec_policy import policy_of

    out: set[tuple[str, str]] = set()
    policy = policy_of(spec)
    hooks = policy.get("hooks")
    for event, entries in (hooks if isinstance(hooks, dict) else {}).items():
        for entry in entries if isinstance(entries, list) else [entries]:
            matcher = entry.get("matcher") if isinstance(entry, dict) else None
            out.add(("hooks", f"{event}: {matcher or '*'} ({_digest(entry)[:8]})"))
    perms = policy.get("permissions")
    rules = perms.get("rules") if isinstance(perms, dict) else None
    for rule in rules if isinstance(rules, list) else []:
        if isinstance(rule, dict):
            match = ", ".join(str(m) for m in rule.get("match") or []) or "*"
            label = f"{rule.get('effect')} {rule.get('capability')} {match}"
            out.add(("permissions", f"{label} ({_digest(rule)[:8]})"))
    settings = policy.get("toolsSettings")
    for tool in settings if isinstance(settings, dict) else {}:
        out.add(("toolsSettings", str(tool)))
    return out


def _policy_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    """One row per hook, permission rule and tool setting the add writes.

    These run on the crewmate's worker, grants included, so
    the owner sees each one before applying.
    """
    return [
        {"section": section, "id": ident, "change": "added"}
        for section, ident in sorted(_policy_ids(after) - _policy_ids(before))
    ]


def _worker_kit(spec: dict[str, Any], base: dict[str, Any] | None) -> dict[str, Any]:
    """What *spec* adds to the worker *base*, in kit shape; the plain kit with no base.

    A file may hold a full template copy (the create path) or only what the
    template adds to the worker (the upgrade migration). Both read the same
    here, so an add is previewed by what it changes in the worker, not by
    the base's own servers and approvals it would write out again.
    """
    if base is None:
        return _kit_of(spec)
    from kiro_crew.mate_worker import worker_additions

    additions = worker_additions(base, _kit_of_with_prompt(spec))
    kit = _kit_of(additions)
    # The base's own entry wins the merge for a server it declares, so what the
    # file restricts on it is not in ``mcpServers``: list those limits on their own.
    kit["disabledTools"] = [
        ref for ref in additions["disabledTools"] if _server_of(ref) not in kit["mcpServers"]
    ]
    kit["disabledServers"] = list(additions["disabledServers"])
    return kit


def _names_a_prompt(template: str) -> bool:
    """Whether *template*'s own spec names a prompt: the resolved spec answers a
    prompt-less one with the KAS fallback text, which is not the agent's
    directions."""
    from kiro_crew.agent_sdk import agent_specs
    from kiro_crew.mate_config import source_agents_dir

    try:
        raw = agent_specs.load_agent_spec(source_agents_dir(template), template).get("prompt")
    except agent_specs.AgentSpecUnreadable as exc:
        raise WorkerKitError("template_unreadable") from exc
    return isinstance(raw, str) and bool(raw.strip())


def _directions_of(source: dict[str, Any], base: dict[str, Any] | None) -> str:
    """*source*'s behaviour directions: its prompt beyond the worker's own text.

    The subtraction ``worker_additions`` applies: a prompt the worker base already
    opens with adds nothing, and one that opens with the base's text adds only the
    rest. ``""`` when the agent brings none.
    """
    raw = source.get("prompt")
    prompt = raw.strip() if isinstance(raw, str) else ""
    if not prompt or base is None:
        return prompt
    from kiro_crew.mate_worker import worker_additions

    return str(worker_additions(base, {"prompt": prompt}).get("prompt") or "").strip()


def slot_directions(spec: dict[str, Any], base: dict[str, Any] | None, name: str) -> str:
    """The directions crewmate file *spec* (named *name*) holds, as the worker reads them.

    The file's prompt (a ``file://`` one read), less the worker base's own text by
    the subtraction ``worker_additions`` applies (``_directions_of``). A file that
    copies the template's whole prompt therefore holds none. ``""`` when the slot
    is empty; a prompt file that cannot be read counts as its raw text.
    """
    raw = spec.get("prompt")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    from kiro_crew.agent_sdk import agent_specs
    from kiro_crew.config import paths as paths_mod

    try:
        text = agent_specs.resolve_prompt(
            spec, agent_id=name, agents_dir=paths_mod.mate_agents_dir()
        )
    except agent_specs.AgentSpecUnreadable:
        logger.warning("Bot %s: prompt unreadable; reading it raw", name, exc_info=True)
        text = raw
    return _directions_of({"prompt": text}, base)


def _directions_change(current: str, directions: str) -> list[dict[str, str]]:
    """The preview row for the behaviour-directions slot the add fills from
    *directions*, against the slot's *current* directions (``slot_directions``):
    ``added`` into an empty slot, ``replaced`` over different directions,
    ``skipped`` when the slot already holds them; none when the agent brings no
    directions (the slot is left as it is)."""
    old_text = current.strip()
    new_text = directions.strip()
    if not new_text:
        return []
    if new_text == old_text:
        change = "skipped"
    else:
        change = "replaced" if old_text else "added"
    return [{"section": "prompt", "id": "prompt", "change": change}]


def _kit_of_with_prompt(spec: dict[str, Any]) -> dict[str, Any]:
    """``_kit_of`` plus the prompt, for ``worker_additions`` (which reads prompt
    to subtract the base's own text). The kit itself carries no prompt."""
    kit = _kit_of(spec)
    prompt = spec.get("prompt")
    kit["prompt"] = prompt if isinstance(prompt, str) else ""
    return kit


def _worker_base_or_none() -> dict[str, Any] | None:
    """The worker's source base, prompt literal; None when it cannot be read."""
    try:
        from kiro_crew.mate_config_migration import _worker_base

        return _worker_base()
    except Exception:  # noqa: BLE001 - a preview then lists the plain kit difference
        logger.warning("worker base unreadable; previewing the plain kit", exc_info=True)
        return None


def preview(member: str, body: Any) -> dict[str, Any]:
    """What applying *body* would add to the crewmate's worker. Writes nothing.

    ``changes`` lists each net-new item (``added``) and each item left out
    because the crewmate already has it (``skipped``).
    """
    path, spec, new_spec, template, tkit = _plan(member, body)
    base = _worker_base_or_none()
    current = slot_directions(spec, base, path.stem)
    return {
        "member": member,
        "template": template,
        "revision": _plan_revision(spec, new_spec),
        "runs_unasked": _runs_unasked(spec, new_spec, base),
        # The behaviour directions the add writes ("" when the agent has none),
        # so the preview can show the text it fills the slot with.
        "directions": tkit.get("prompt") or "",
        # The directions the slot holds now ("" when empty), so a preview that
        # replaces them can show what gets overwritten.
        "current_directions": current,
        "changes": [
            *_directions_change(current, tkit.get("prompt") or ""),
            *_added(tkit, spec, base, new_spec),
            *_approval_changes(spec, new_spec, base),
            *_skipped(tkit, spec, base),
            *_exclusion_changes(spec, new_spec),
            *_muted_server_changes(spec, new_spec),
            *_policy_changes(spec, new_spec),
        ],
    }


def _plan_revision(spec: dict[str, Any], new_spec: dict[str, Any]) -> str:
    """The preview's lock: the file as it was AND the file the add would write."""
    return _digest([spec, new_spec])


def apply(member: str, body: Any) -> dict[str, Any]:
    """Add the template's kit into the crewmate's file, additively.

    Requires the ``revision`` the preview returned. It binds both the file and
    the result the preview showed, so an apply never lands on a file that changed
    since, nor writes a template that changed since: either asks for a fresh
    preview (``stale_revision``).
    """
    return apply_reporting(member, body)[0]


def apply_reporting(member: str, body: Any) -> tuple[dict[str, Any], bool]:
    """:func:`apply`, plus whether the file's hooks, permissions or tool settings
    changed, so the caller reloads the crewmate's live workers onto them."""
    from kiro_crew import mate_approval_revocations as revocations
    from kiro_crew.agent import agents_spec_lock
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.mate_spec_policy import policy_of

    if not isinstance(body, dict) or not body.get("revision"):
        raise WorkerKitError("revision_required", 400)
    policy_changed = False
    with agents_spec_lock(paths_mod.mate_agents_dir()):
        path, spec, new_spec, _template, _tkit = _plan(member, body)
        if body["revision"] != _plan_revision(spec, new_spec):
            raise WorkerKitError("stale_revision")
        if _digest(new_spec) != _digest(spec):
            atomic_write(path, json.dumps(new_spec, indent=2) + "\n", restrict_to_owner=True)
            base = _worker_base_or_none()
            if base is None:
                # The preview leaves out a ``permissions`` allow the base already
                # grants, which an unread base cannot show: only the listed
                # channels re-grant, so a failed read restores no revoked approval.
                regrants = sorted(_listed_refs(new_spec) - _listed_refs(spec))
            else:
                regrants = [row["id"] for row in _approval_changes(spec, new_spec, base)]
            revocations.record(path.stem, [], regrants)
            policy_changed = _digest(policy_of(new_spec)) != _digest(policy_of(spec))
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="owner",
            operation="member_worker_kit.add",
            outcome="success",
            source="dashboard",
            resources=member,
        )
    except Exception:  # noqa: BLE001 - the audit must not undo a landed write
        logger.debug("SEL audit unavailable for the worker kit add", exc_info=True)
    return view(member), policy_changed
