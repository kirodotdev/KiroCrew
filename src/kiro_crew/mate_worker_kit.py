"""A crewmate's worker kit: one agent template copied into its file, once.

A crewmate can name ONE agent template as its worker's extra kit
(``rfc-mate-owned-agent-config`` §5.1). Choosing it copies that template's kit
-- its prompt (the SOPs it carries), ``mcpServers`` with their ``autoApprove``,
skills (``resources`` and ``skills``) and ``allowedTools`` -- into the
crewmate's own file under ``~/.kiro/mates/agents/`` once. The file then does
not track the template: a later edit to the template reaches nobody until the
owner asks for a re-copy, which is previewed first and applied only on
confirmation. ``keep_additions`` decides whether what was added to the file
since the last copy survives it.

What the last copy wrote is recorded beside the file in a Crew-owned record,
``~/.kiro/mates/kits/<id>.json`` (``{"template", "copied"}``), outside the
``agents`` folder so no resolver reads it as a spec. That record is what makes
"added since" answerable; with no record (a file whose kit came from an older
create), everything currently in the file's kit counts as added since.

The written file still passes through the governance sanitizer, and the worker
merge (:mod:`kiro_crew.mate_worker`) governs again at projection, so a kit never
lifts the worker above its ceiling. Only the owner's dashboard reaches this
module. Blocking file I/O throughout; callers run it off the event loop. No
module state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from kiro_crew.mate_worker import ADDITION_KEYS
from kiro_crew.user_json import copy_json_document

logger = logging.getLogger(__name__)


class WorkerKitError(Exception):
    """A worker-kit request that cannot be served; ``code`` is the wire code."""

    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def kit_record_path(mate_file: str) -> Path:
    from kiro_crew.config import paths as paths_mod

    return paths_mod.mate_agents_dir().parent / "kits" / f"{mate_file}.json"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _kit_of(spec: dict[str, Any]) -> dict[str, Any]:
    """The kit parts of a spec, normalised: missing parts read empty."""
    prompt = spec.get("prompt")
    servers = spec.get("mcpServers")
    kit: dict[str, Any] = {
        "prompt": prompt if isinstance(prompt, str) else "",
        "mcpServers": (
            {k: copy_json_document(v) for k, v in servers.items() if isinstance(v, dict)}
            if isinstance(servers, dict)
            else {}
        ),
    }
    for key in ("resources", "skills", "allowedTools"):
        value = spec.get(key)
        kit[key] = [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
    return kit


def template_kit(template: str) -> dict[str, Any]:
    """*template*'s kit, read once from the user-level agents folder, prompt inlined."""
    from kiro_crew.mate_config import MateConfigError, _resolved_source_spec

    try:
        return _kit_of(_resolved_source_spec(template))
    except MateConfigError as exc:
        raise WorkerKitError("template_unreadable") from exc


def read_record(mate_file: str) -> dict[str, Any] | None:
    """The last copy's record, or None when there is none or it is unreadable."""
    path = kit_record_path(mate_file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("template"), str):
        return None
    copied = data.get("copied")
    return {
        "template": data["template"],
        "copied": _kit_of(copied if isinstance(copied, dict) else {}),
    }


def write_record(mate_file: str, template: str, copied: dict[str, Any]) -> None:
    from kiro_crew.atomic_write import atomic_write

    path = kit_record_path(mate_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        path,
        json.dumps({"template": template, "copied": copied}, indent=2) + "\n",
        restrict_to_owner=True,
    )


def record_initial_kit(mate_file: str, template: str) -> None:
    """Record that a crewmate's file was created as a copy of *template*. Never raises."""
    try:
        write_record(mate_file, template, template_kit(template))
    except Exception:  # noqa: BLE001 - the create stands; a re-copy then keeps all
        logger.warning("crewmate %s: worker kit record not written", mate_file, exc_info=True)


def added_since(current: dict[str, Any], copied: dict[str, Any] | None) -> dict[str, Any]:
    """What *current* (a file's kit) holds that the last copy did not write."""
    base = copied or _kit_of({})
    servers = {
        name: entry
        for name, entry in current["mcpServers"].items()
        if base["mcpServers"].get(name) != entry
    }
    out: dict[str, Any] = {
        "prompt": current["prompt"] if current["prompt"].strip() != base["prompt"].strip() else "",
        "mcpServers": servers,
    }
    for key in ("resources", "skills", "allowedTools"):
        have = set(base[key])
        out[key] = [v for v in current[key] if v not in have]
    return out


def _layered(template: dict[str, Any], kept: dict[str, Any]) -> dict[str, Any]:
    """The template's kit with *kept* additions laid over it (kept wins)."""
    kit = copy_json_document(template)
    if kept["prompt"]:
        kit["prompt"] = kept["prompt"]
    kit["mcpServers"].update(copy_json_document(kept["mcpServers"]))
    for key in ("resources", "skills", "allowedTools"):
        kit[key] = list(dict.fromkeys([*kit[key], *kept[key]]))
    return kit


def _changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    """One row per kit item that the write would add, remove or change."""
    rows: list[dict[str, str]] = []
    if before["prompt"].strip() != after["prompt"].strip():
        rows.append({"section": "prompt", "id": "prompt", "change": "changed"})
    for name in sorted(set(before["mcpServers"]) | set(after["mcpServers"])):
        old, new = before["mcpServers"].get(name), after["mcpServers"].get(name)
        if old != new:
            change = "added" if old is None else "removed" if new is None else "changed"
            rows.append({"section": "mcpServers", "id": name, "change": change})
    for key in ("resources", "skills", "allowedTools"):
        old_set, new_set = set(before[key]), set(after[key])
        for item in sorted(new_set - old_set):
            rows.append({"section": key, "id": item, "change": "added"})
        for item in sorted(old_set - new_set):
            rows.append({"section": key, "id": item, "change": "removed"})
    return rows


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


def _request(body: Any) -> tuple[str, bool, str]:
    if not isinstance(body, dict):
        raise WorkerKitError("invalid_json", 400)
    template = body.get("template")
    keep = body.get("keep_additions", True)
    revision = body.get("revision", "")
    if not isinstance(template, str) or not template.strip():
        raise WorkerKitError("template_required", 400)
    if not isinstance(keep, bool) or not isinstance(revision, str):
        raise WorkerKitError("invalid_request", 400)
    return template.strip(), keep, revision


#: The id a Capabilities-page private copy is minted under (``crew-`` + hex).
_PRIVATE_COPY_RE = re.compile(r"^crew-[0-9a-f]+$")


def _private_copy_origin(template: str) -> tuple[bool, str]:
    """``(is a private copy, the template it was forked from or "")``.

    A private copy is a template the sidecar records as forked
    (``agent_state.get_fork_info``) or one under the ``crew-<hex>`` id the
    Capabilities page mints. Its origin follows ``forked_from`` through a copy
    of a copy, bounded; an origin that is itself only a private copy, or none
    recorded, is ``""``, so the id is never what the user is shown.
    """
    from kiro_crew import agent_state

    private = bool(_PRIVATE_COPY_RE.match(template))
    name, seen = template, {template}
    for _ in range(8):
        try:
            info = agent_state.get_fork_info(name)
        except Exception:  # noqa: BLE001 - display only: no lineage reads as none
            info = None
        if not info:
            break
        private, name = True, info["forked_from"]
        if name in seen:
            break
        seen.add(name)
    if not private:
        return False, template
    is_origin = name != template and not _PRIVATE_COPY_RE.match(name)
    return True, name if is_origin else ""


def view(member: str) -> dict[str, Any]:
    """The worker-kit state the advanced setting shows.

    ``origin`` is the template to name for the kit: the recorded template, or,
    when that is a private copy (``private_copy``), the template the copy was
    forked from, ``""`` when no origin is known.
    """
    _, spec = _owned_file(member)
    record = read_record(member)
    since = added_since(_kit_of(spec), record["copied"] if record else None)
    template = record["template"] if record else ""
    private, origin = _private_copy_origin(template) if template else (False, "")
    return {
        "member": member,
        "template": template,
        "origin": origin,
        "private_copy": private,
        "revision": _digest(spec),
        "has_additions_since": any(since[key] for key in ADDITION_KEYS),
    }


def _plan(member: str, body: Any) -> tuple[Path, dict[str, Any], dict[str, Any], str, dict]:
    from kiro_crew.platform.governance import sanitize_agent_config_governance

    template, keep, revision = _request(body)
    path, spec = _owned_file(member)
    if revision and revision != _digest(spec):
        raise WorkerKitError("stale_revision")
    tkit = template_kit(template)
    record = read_record(member)
    current = _kit_of(spec)
    kept = added_since(current, record["copied"] if record else None) if keep else _kit_of({})
    kit = _layered(tkit, kept)
    new_spec = copy_json_document(spec)
    for key in ADDITION_KEYS:
        new_spec[key] = copy_json_document(kit[key])
    tools = [t for t in new_spec.get("tools", []) if isinstance(t, str)]
    for name in kit["mcpServers"]:
        if f"@{name}" not in tools and "*" not in tools:
            tools.append(f"@{name}")
    new_spec["tools"] = tools
    new_spec["name"] = member
    sanitize_agent_config_governance(new_spec)
    return path, spec, new_spec, template, tkit


def _worker_kit(spec: dict[str, Any], base: dict[str, Any] | None) -> dict[str, Any]:
    """What *spec* adds to the worker *base*, in kit shape; the plain kit with no base.

    A file may hold a full template copy (the create path) or only what the
    template adds to the worker (the upgrade migration). Both read the same
    here, so a re-copy is previewed by what it changes in the worker, not by
    the base's own servers and approvals it would write out again.
    """
    if base is None:
        return _kit_of(spec)
    from kiro_crew.mate_worker import worker_additions

    return _kit_of(worker_additions(base, _kit_of(spec)))


def _worker_base_or_none() -> dict[str, Any] | None:
    """The worker's source base, prompt literal; None when it cannot be read."""
    try:
        from kiro_crew.mate_config_migration import _worker_base

        return _worker_base()
    except Exception:  # noqa: BLE001 - a preview then lists the plain kit difference
        logger.warning("worker base unreadable; previewing the plain kit", exc_info=True)
        return None


def preview(member: str, body: Any) -> dict[str, Any]:
    """What applying *body* would change in the crewmate's worker. Writes nothing."""
    _, spec, new_spec, template, _ = _plan(member, body)
    base = _worker_base_or_none()
    return {
        "member": member,
        "template": template,
        "revision": _digest(spec),
        "changes": _changes(_worker_kit(spec, base), _worker_kit(new_spec, base)),
    }


def apply(member: str, body: Any) -> dict[str, Any]:
    """Copy the template's kit into the crewmate's file, once, and record the copy.

    Requires the ``revision`` the preview returned, so an apply never lands on a
    file that changed after the owner looked at the preview.
    """
    from kiro_crew.agent import agents_spec_lock
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.config import paths as paths_mod

    if not isinstance(body, dict) or not body.get("revision"):
        raise WorkerKitError("revision_required", 400)
    with agents_spec_lock(paths_mod.mate_agents_dir()):
        path, spec, new_spec, template, tkit = _plan(member, body)
        if _digest(new_spec) != _digest(spec):
            atomic_write(path, json.dumps(new_spec, indent=2) + "\n", restrict_to_owner=True)
        write_record(member, template, tkit)
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="owner",
            operation="member_worker_kit.copy",
            outcome="success",
            source="dashboard",
            resources=member,
        )
    except Exception:  # noqa: BLE001 - the audit must not undo a landed write
        logger.debug("SEL audit unavailable for the worker kit copy", exc_info=True)
    return view(member)
