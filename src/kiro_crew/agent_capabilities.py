"""Owner-reviewed, single-parent capability intent and materialization.

Only this module interprets inheritance. Agent JSON stays a harness document;
provenance, pending publication and local tombstones live in agent_state.
Filesystem entry points run on a worker, never on the inference hot path.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import hmac
import json
import secrets
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable

from kiro_crew import agent_state
from kiro_crew.agent import agents_spec_lock, kiro_agents_dir_path
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    KiroCrewConfig,
    config_local_path,
    default_project_dir,
    update_config_locked,
)
from kiro_crew.config.paths import project_agents_dir
from kiro_crew.platform import redact_via_context
from kiro_crew.platform.governance import sanitize_agent_config_governance
from kiro_crew.platform.governance_profiles import governance_answer_generation

SECTIONS = agent_state.CAPABILITY_SECTIONS
MAX_OPERATIONS = 300
MAX_DOCUMENT_BYTES = 1024 * 1024


class CapabilityError(ValueError):
    """A bounded machine code, never source bytes or credentials."""

    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code = code
        self.status = status


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _read_spec(path: Path) -> dict:
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    try:
        raw = safe_read_file_bytes_nolink(str(path), str(path.parent), max_bytes=MAX_DOCUMENT_BYTES)
        result = json.loads(raw) if raw is not None else None
    except (ValueError, FileTooLargeError):
        raise CapabilityError("source_unreadable") from None
    if not isinstance(result, dict):
        raise CapabilityError("source_unreadable")
    return result


def _source(name: str, project: str, *, allow_private: bool = False) -> tuple[Path, dict, dict]:
    """Project scope wins exactly as it does for the provider's cwd."""
    from kiro_crew.agent import OWNED_KIRO_AGENT_FILES, _conflicting_spec_for, agent_spec_path
    from kiro_crew.validation import _AGENT_NAME_RE

    if not isinstance(name, str) or not _AGENT_NAME_RE.fullmatch(name):
        raise CapabilityError("invalid_template_name")
    roots = [(project_agents_dir(project), "project")] if project else []
    roots.append((kiro_agents_dir_path(), "global"))
    for root, scope in roots:
        try:
            path = agent_spec_path(name, agents_dir=root)
        except ValueError:
            raise CapabilityError("ambiguous_template_name") from None
        if path is None:
            # A broken file claiming this exact filename cannot authorize a
            # fallback to the broader global scope. Unrelated junk is skipped
            # by the shared resolver, just as it is on the normal agent path.
            if (root / (name + ".json")).exists():
                raise CapabilityError("source_unreadable")
            continue
        if _conflicting_spec_for(name, path, root) is not None:
            raise CapabilityError("ambiguous_template_name")
        spec = _read_spec(path)
        if spec.get("name", path.stem) != name:
            raise CapabilityError("source_identity_changed")
        if path is not None:
            if not allow_private and agent_state.get_fork_info(name, strict=True):
                raise CapabilityError("private_parent_forbidden")
            from kiro_crew.agent_discovery import _global_agent_info

            source = (
                "project"
                if scope == "project"
                else (
                    "builtin"
                    if path.name in OWNED_KIRO_AGENT_FILES
                    else (
                        "package"
                        if _global_agent_info(path, spec).source == "package"
                        else "custom"
                    )
                )
            )
            return (
                path,
                spec,
                {
                    "name": name,
                    "scope": scope,
                    "source": source,
                    "path": str(path.resolve()),
                    "project": project,
                },
            )
    raise CapabilityError("parent_missing")


def _rows(spec: dict, catalog: dict[str, str]) -> dict[str, dict]:
    rows: dict[str, dict] = {section: {} for section in SECTIONS}
    servers = spec.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise CapabilityError("invalid_spec")
    for name, transport in servers.items():
        if not agent_state.capability_transport_fields_valid(transport):
            raise CapabilityError("invalid_spec")
        rows["mcpServers"][name] = {
            k: copy.deepcopy(v) for k, v in transport.items() if k != "autoApprove"
        }
        for tool in transport.get("autoApprove", []):
            if not isinstance(tool, str):
                raise CapabilityError("invalid_spec")
            rows["autoApprove"][f"@{name}/{tool}"] = True
    for section in ("tools", "allowedTools"):
        items = spec.get(section, [])
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise CapabilityError("invalid_spec")
        rows[section] = dict.fromkeys(items, True)
    inverse = {uri: key for key, uri in reversed(list(catalog.items()))}
    resources = spec.get("resources", [])
    if not isinstance(resources, list) or not all(isinstance(i, str) for i in resources):
        raise CapabilityError("invalid_spec")
    for uri in resources:
        if uri in inverse:
            rows["skills"][inverse[uri]] = uri
        else:
            rows["resources"][uri] = uri
    for section in ("prompt", "model"):
        if section in spec and spec[section] is not None:
            if not isinstance(spec[section], str):
                raise CapabilityError("invalid_spec")
            rows[section][section] = spec[section]
    return rows


def _materialize(base: dict, rows: dict[str, dict], catalog: dict[str, str]) -> dict:
    result = copy.deepcopy(base)
    original = _rows(base, catalog)
    result["mcpServers"] = copy.deepcopy(rows["mcpServers"])
    for ref in rows["autoApprove"]:
        server, tool = _approval_ref(ref)
        if server in result["mcpServers"]:
            result["mcpServers"][server].setdefault("autoApprove", []).append(tool)
    for section in ("tools", "allowedTools"):
        result[section] = list(rows[section])
    result["allowedTools"] = [
        ref
        for ref in result["allowedTools"]
        if not ref.startswith("@")
        or (
            ref[1:].split("/", 1)[0] in result["mcpServers"]
            and not result["mcpServers"][ref[1:].split("/", 1)[0]].get("disabled")
        )
    ]
    # Preserve the relative order of all surviving custom resource entries.
    wanted = list(rows["resources"].values()) + list(rows["skills"].values())
    result["resources"] = list(
        dict.fromkeys([r for r in base.get("resources", []) if r in wanted] + wanted)
    )
    for section in ("prompt", "model"):
        if section in rows[section]:
            result[section] = rows[section][section]
        else:
            result.pop(section, None)
    preserved = {
        section
        for section in ("tools", "allowedTools", "prompt", "model")
        if original[section] == rows[section]
    }
    if (
        original["mcpServers"] == rows["mcpServers"]
        and original["autoApprove"] == rows["autoApprove"]
    ):
        preserved.add("mcpServers")
    if original["resources"] == rows["resources"] and original["skills"] == rows["skills"]:
        preserved.add("resources")
    for section in preserved:
        if section in base:
            result[section] = copy.deepcopy(base[section])
        else:
            result.pop(section, None)
    return result


def _approval_ref(ref: str) -> tuple[str, str]:
    if not ref.startswith("@") or "/" not in ref or not all(ref[1:].split("/", 1)):
        raise CapabilityError("invalid_approval_ref", 400)
    return tuple(ref[1:].split("/", 1))  # type: ignore[return-value]


def _expands(section: str, before: Any, after: Any) -> bool:
    if after is None:
        return False
    if section in ("tools", "allowedTools", "autoApprove", "skills", "resources"):
        return before != after
    if section == "mcpServers":
        return before != after and not (isinstance(after, dict) and after.get("disabled") is True)
    return False


def resolve_effective(
    base: dict,
    parent: dict,
    intent: dict,
    catalog: dict[str, str],
    accept: set[tuple[str, str]] | None = None,
) -> tuple[dict, dict, list[dict]]:
    """Pure resolver shared by previews, writes and background reconciliation."""
    accepted = copy.deepcopy(intent["accepted"])
    current = _rows(parent, catalog)
    overrides = intent["overrides"]
    changes = []
    for section in SECTIONS:
        for key in dict.fromkeys([*accepted[section], *current[section]]):
            old, new = accepted[section].get(key), current[section].get(key)
            if old == new:
                continue
            conflict = key in overrides.get(section, {})
            needs = _expands(section, old, new)
            selected = (section, key) in (accept or set())
            changes.append(
                {
                    "section": section,
                    "id": key,
                    "kind": "added" if old is None else "removed" if new is None else "changed",
                    "conflict": conflict,
                    "requires_approval": needs,
                    "before": old,
                    "after": new,
                }
            )
            if selected or (not conflict and not needs):
                if new is None:
                    accepted[section].pop(key, None)
                else:
                    accepted[section][key] = copy.deepcopy(new)
    rows = copy.deepcopy(accepted)
    for section, items in overrides.items():
        for key, override in items.items():
            if override["action"] == "remove":
                rows[section].pop(key, None)
            else:
                rows[section][key] = copy.deepcopy(override["value"])
    # A negative entry cannot cancel a wildcard in the harness grammar.
    # Refuse that shape rather than presenting an exclusion that never applies.
    for section in ("tools", "allowedTools", "autoApprove"):
        for key, override in overrides.get(section, {}).items():
            if override["action"] != "remove":
                continue
            for ref in rows[section]:
                if fnmatch.fnmatchcase(key, ref) or (
                    ref.startswith("@") and key.startswith(ref + "/")
                ):
                    raise CapabilityError("wildcard_exclusion_unrepresentable")
    for section, other in (("autoApprove", "allowedTools"), ("allowedTools", "autoApprove")):
        for key, override in overrides.get(section, {}).items():
            if override["action"] == "remove" and any(
                fnmatch.fnmatchcase(key, ref)
                or fnmatch.fnmatchcase(ref, key)
                or (ref.startswith("@") and key.startswith(ref + "/"))
                or (key.startswith("@") and ref.startswith(key + "/"))
                for ref in rows[other]
            ):
                raise CapabilityError("approval_exclusion_overlaps")
    return _materialize(base, rows, catalog), accepted, changes


def validate_request(body: Any, *, commit: bool = False) -> dict:
    if not isinstance(body, dict) or set(body) - {
        "revision",
        "enroll",
        "operations",
        "accept_parent",
        "accept_members",
        "preview_token",
    }:
        raise CapabilityError("invalid_body", 400)
    if not isinstance(body.get("revision"), str) or not body["revision"]:
        raise CapabilityError("revision_required", 400)
    if "enroll" in body and type(body["enroll"]) is not bool:
        raise CapabilityError("invalid_enroll", 400)
    result = copy.deepcopy(body)
    for key in ("operations", "accept_parent", "accept_members"):
        result.setdefault(key, [])
        if not isinstance(result[key], list) or len(result[key]) > MAX_OPERATIONS:
            raise CapabilityError("invalid_" + key, 400)
    seen = set()
    for op in result["operations"]:
        if not isinstance(op, dict) or set(op) - {
            "section",
            "id",
            "action",
            "value",
            "connection_id",
            "retain_paths",
        }:
            raise CapabilityError("invalid_operation", 400)
        if op.get("section") not in SECTIONS or not isinstance(op.get("id"), str) or not op["id"]:
            raise CapabilityError("invalid_operation", 400)
        if "retain_paths" in op and (
            op.get("section") != "mcpServers"
            or op.get("action") != "set"
            or "value" not in op
            or "connection_id" in op
        ):
            raise CapabilityError("invalid_retain_paths", 400)
        if "connection_id" in op and (
            op["section"] != "mcpServers"
            or not isinstance(op["connection_id"], str)
            or not op["connection_id"]
        ):
            raise CapabilityError("invalid_connection_id", 400)
        pair = (op["section"], op["id"])
        if pair in seen or len(op["id"]) > 4096:
            raise CapabilityError("duplicate_operation", 400)
        seen.add(pair)
        action = op.get("action")
        if action not in ("inherit", "set", "remove"):
            raise CapabilityError("invalid_action", 400)
        if action == "set":
            if ("value" in op) == ("connection_id" in op) or op.get("value", True) is None:
                raise CapabilityError("set_value_required", 400)
        elif "value" in op or "connection_id" in op:
            raise CapabilityError("unexpected_value", 400)
    for item in result["accept_parent"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"section", "id"}
            or item["section"] not in SECTIONS
            or not isinstance(item["id"], str)
        ):
            raise CapabilityError("invalid_accept_parent", 400)
    if not all(isinstance(name, str) and name for name in result["accept_members"]):
        raise CapabilityError("invalid_accept_members", 400)
    if commit and not isinstance(result.get("preview_token"), str):
        raise CapabilityError("preview_required", 400)
    try:
        if len(json.dumps(result, allow_nan=False).encode()) > MAX_DOCUMENT_BYTES:
            raise CapabilityError("request_too_large", 400)
    except (ValueError, TypeError) as exc:
        raise CapabilityError("invalid_body", 400) from exc
    return result


def _alternate_shortcuts(value: Any) -> bool:
    if not isinstance(value, dict):
        return bool(value)
    return any(
        (key.lower().startswith(("allowed", "trusted", "auto")) and bool(item))
        or (isinstance(item, dict) and _alternate_shortcuts(item))
        for key, item in value.items()
    )


def _align_permissions(base: dict, spec: dict) -> None:
    from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions

    if _alternate_shortcuts(base.get("toolsSettings", {})) or base.get("autoAllowReadonly"):
        raise CapabilityError("alternate_permissions_require_review")
    if "permissions" in base:
        prior = derived_agent_permissions(base.get("allowedTools"), str(base.get("name", "")))
        if base["permissions"] != prior:
            raise CapabilityError("alternate_permissions_require_review")
        spec["permissions"] = derived_agent_permissions(
            spec.get("allowedTools"), str(spec.get("name", ""))
        )


def _sanitize_projection(spec: dict, intent: dict, catalog: dict[str, str]) -> None:
    """Preview the existing governance funnel without emitting write audits.

    Withheld shortcuts become explicit tombstones, so a later policy relaxation
    cannot restore an approval that the last publication did not grant.
    """
    original = copy.deepcopy(spec)
    before = _rows(spec, catalog)
    sanitize_agent_config_governance(spec, audit=False)
    # Managed computer tools must always traverse the host approval gate,
    # independent of whether this installation has a governance policy.
    computer = spec.get("mcpServers", {}).get("kirocrew-computer")
    if computer is not None:
        computer.pop("autoApprove", None)
    after = _rows(spec, catalog)
    if (
        before["allowedTools"] != after["allowedTools"]
        or before["autoApprove"] != after["autoApprove"]
    ):
        _align_permissions(original, spec)
    for section in ("allowedTools", "autoApprove"):
        for key in before[section].keys() - after[section].keys():
            intent["overrides"][section][key] = {"action": "remove"}


def safe_view(value: Any) -> Any:
    """Keep wire types intact; credential-bearing maps never expose their values."""
    return _safe_view(value, ())


def _argument_secrets(args: Any) -> list[str]:
    """Collect credential option values before masking any transport leaves.

    Keep argv intact: an inline option is masked as one scalar so retain_paths
    restores the exact original argument, not a reconstructed command line.
    """
    import re

    if not isinstance(args, list):
        return []
    values = []
    credential_value = False
    options_ended = False
    for index, arg in enumerate(args):
        if not isinstance(arg, str):
            credential_value = False
            continue
        if credential_value:
            if arg:
                values.append(arg)
            credential_value = False
            continue
        if arg == "--":
            options_ended = True
            continue
        option, separator, value = arg.partition("=")
        # Complete environment assignments are credential-bearing without an
        # option prefix (including values passed to -e/--env). A bare name must
        # never consume the following argv element as a credential value.
        assignment = bool(separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", option))
        # The option terminator does not terminate environment assignments.
        if options_ended and not assignment:
            continue
        # -u is also used for non-auth flags (for example Python unbuffered).
        # Mask only its colon-bearing basic-auth shape, not arbitrary short options.
        if arg in ("-u", "--user") and index + 1 < len(args):
            candidate = args[index + 1]
            if isinstance(candidate, str) and ":" in candidate:
                values.append(candidate)
        elif arg.startswith("-u") and ":" in arg[2:]:
            values.append(arg[2:])
        elif arg.startswith("--user=") and ":" in arg[7:]:
            values.append(arg[7:])
        # Match names, not credential formats or minimum secret lengths.
        if (option.startswith("-") or assignment) and re.search(
            r"(?:^|[-_])(?:key|token|secret|password|passwd|passphrase|credentials?|auth|authorization)$",
            re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", option),
            re.IGNORECASE,
        ):
            if separator:
                if value:
                    values.append(value)
            else:
                credential_value = True
    return values


def _safe_view(value: Any, inherited_secrets: tuple[str, ...]) -> Any:
    """Carry known credentials through their subtree, not unrelated response rows."""
    from urllib.parse import urlsplit

    if isinstance(value, dict):
        private_values = (
            [
                v
                for field in ("env", "headers")
                for v in (value.get(field) or {}).values()
                if isinstance(v, str) and v
            ]
            if all(isinstance(value.get(field, {}), dict) for field in ("env", "headers"))
            else []
        )
        oauth = value.get("oauth")
        if isinstance(oauth, dict):
            secret = oauth.get("clientSecret")
            if isinstance(secret, str) and secret:
                private_values.append(secret)
        private_values.extend(_argument_secrets(value.get("args")))
        private_values.extend(inherited_secrets)
        secrets = tuple(private_values)
        result: dict[str, Any] = {}
        for key, item in value.items():
            field = str(key).lower()
            if field in {"env", "headers"} and isinstance(item, dict):
                result[redact_via_context(str(key))] = {
                    redact_via_context(str(k)): "[REDACTED]" if v else v for k, v in item.items()
                }
            elif field == "url" and isinstance(item, str):
                try:
                    parsed = urlsplit(item)
                    result[key] = (
                        "[REDACTED]"
                        if parsed.username or parsed.password or parsed.query
                        else _safe_view(item, secrets)
                    )
                except ValueError:
                    result[key] = "[REDACTED]"
            else:
                result[redact_via_context(str(key))] = _safe_view(item, secrets)
        return result
    if isinstance(value, list):
        return [_safe_view(v, inherited_secrets) for v in value]
    if isinstance(value, str):
        if any(secret in value for secret in inherited_secrets):
            return "[REDACTED]"
        redacted = redact_via_context(value)
        return "[REDACTED]" if redacted != value else value
    return value


def _contains_mask(value: Any) -> bool:
    """A displayed secret is not a replacement value, even in a direct HTTP call."""
    if isinstance(value, str):
        return any(marker in value.lower() for marker in ("[redacted", "<redacted>", "***", "•••"))
    if isinstance(value, dict):
        return any(_contains_mask(v) for v in value.values())
    return isinstance(value, list) and any(_contains_mask(v) for v in value)


def _retain_transport(value: Any, paths: Any, original: dict) -> dict:
    """Restore only revision-bound redacted leaves in a whole replacement."""
    import re

    if not isinstance(value, dict) or not isinstance(paths, list) or len(paths) > MAX_OPERATIONS:
        raise CapabilityError("invalid_retain_paths", 400)
    result = copy.deepcopy(value)
    shown = safe_view(original)
    decoded: list[tuple[str, ...]] = []

    def locate(document: Any, parts: tuple[str, ...]) -> tuple[Any, Any]:
        current = document
        for part in parts[:-1]:
            if isinstance(current, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", part):
                    raise KeyError(part)
                current = current[int(part)]
            elif isinstance(current, dict):
                current = current[part]
            else:
                raise KeyError(part)
        key: Any = parts[-1]
        if isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise KeyError(key)
            key = int(key)
        elif not isinstance(current, dict):
            raise KeyError(key)
        return current, key

    for path in paths:
        if not isinstance(path, str) or not path.startswith("/") or re.search(r"~(?![01])", path):
            raise CapabilityError("invalid_retain_paths", 400)
        parts = tuple(part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/"))
        if any(parts[: len(old)] == old or old[: len(parts)] == parts for old in decoded):
            raise CapabilityError("overlapping_retain_paths", 400)
        decoded.append(parts)
        try:
            destination, key = locate(result, parts)
            source, source_key = locate(original, parts)
            masked, masked_key = locate(shown, parts)
            leaf = source[source_key]
            if (
                destination[key] != "[REDACTED]"
                or masked[masked_key] != "[REDACTED]"
                or isinstance(leaf, (dict, list))
                or leaf == "[REDACTED]"
            ):
                raise KeyError(key)
            destination[key] = copy.deepcopy(leaf)
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            raise CapabilityError("invalid_retain_path", 400) from None
    # Check the submitted tree, not restored bytes: an original secret can
    # legitimately contain a sequence that resembles a display marker.
    probe = copy.deepcopy(value)
    for parts in decoded:
        container, key = locate(probe, parts)
        container[key] = ""
    if _contains_mask(probe):
        raise CapabilityError("unretained_redacted_value", 400)
    return result


ORDINARY_FIELDS = ("description", "welcomeMessage", "keyboardShortcut")


def _maintain_owned(snap: dict, spec: dict) -> None:
    """Refresh host plumbing without regranting omitted capability entries."""
    from kiro_crew.agent import (
        OWNED_KIRO_AGENT_FILES,
        _collect_app_mcp_servers,
        _refresh_dynamic_fields,
    )

    selected = copy.deepcopy(spec.get("mcpServers", {}))
    if (
        snap["parent"].get("scope") == "global"
        and Path(snap["parent"].get("path", "")).name in OWNED_KIRO_AGENT_FILES
    ):
        preserved = {
            key: copy.deepcopy(spec[key])
            for key in ("prompt", "model", "resources", "tools", "allowedTools")
            if key in spec
        }
        _refresh_dynamic_fields(spec, fork=True)
        for key in ("prompt", "model", "resources", "tools", "allowedTools"):
            if key in preserved:
                spec[key] = preserved[key]
            else:
                spec.pop(key, None)
        spec["mcpServers"] = {
            key: value for key, value in spec.get("mcpServers", {}).items() if key in selected
        }
    namespaced = {key for key in selected if ":" in key}
    if namespaced:
        current = _collect_app_mcp_servers(audit=False)
        for key in namespaced:
            if key not in current:
                spec.get("mcpServers", {}).pop(key, None)
                continue
            value = {k: copy.deepcopy(v) for k, v in current[key].items() if k != "autoApprove"}
            for field in ("autoApprove", "disabled"):
                if field in selected[key]:
                    value[field] = selected[key][field]
            spec.setdefault("mcpServers", {})[key] = value


def _ordinary_parent(base: dict, parent: dict, intent: dict) -> dict:
    result = copy.deepcopy(base)
    local = intent.get("ordinary_local", {})
    for key in ORDINARY_FIELDS:
        if key in local:
            continue
        if key in parent:
            result[key] = copy.deepcopy(parent[key])
        else:
            result.pop(key, None)
    intent["ordinary"] = {
        key: copy.deepcopy(parent[key]) for key in ORDINARY_FIELDS if key in parent
    }
    return result


class CapabilityService:
    """Gateway-owned preview key; no caller identity is retained on the service."""

    def __init__(
        self,
        catalog: Callable[[str], dict[str, str]] | None = None,
        connections: Callable[[], dict[str, dict]] | None = None,
    ):
        self._key = secrets.token_bytes(32)
        self._catalog = catalog or (lambda project: {})
        self._connections = connections or (lambda: {})

    def _token(self, payload: Any) -> str:
        return hmac.new(self._key, _digest(payload).encode(), hashlib.sha256).hexdigest()

    def _snapshot(
        self, member: str, document: dict | None = None, prepared: dict | None = None
    ) -> dict:
        from kiro_crew.config.loader import _deep_merge, read_config_for_update
        from kiro_crew.platform.governance_profiles import poll_profiles_fresh

        if prepared is None:
            cfg = KiroCrewConfig.load()
            poll_profiles_fresh()
            binding = cfg.agents.get(member)
            if binding is None:
                raise CapabilityError("agent_not_found", 404)
            binding_data = vars(binding)
            project = default_project_dir(binding.workspace)
            project = str(Path(project).resolve()) if project else ""
            overlay = read_config_for_update(config_local_path())
            raw = _deep_merge(read_config_for_update(), overlay)
            catalog = self._catalog(project)
            connections = self._connections()
        else:
            overlay = read_config_for_update(config_local_path())
            raw = (
                document if document is not None else _deep_merge(read_config_for_update(), overlay)
            )
            if (
                raw.get("agents", {}).get(member) != prepared["raw_binding"]
                or raw.get("workspaces") != prepared["raw_workspaces"]
                or raw.get("default_workspace") != prepared["raw_default_workspace"]
            ):
                raise CapabilityError("stale_binding")
            binding_data = prepared["binding"]
            project = prepared["project"]
            catalog = prepared["catalog"]
            connections = prepared["connections"]
        target = binding_data["kiro_agent"]
        lineage = agent_state.get_fork_info(target, strict=True)
        intent = agent_state.get_capabilities(target)
        if lineage and lineage["private_to"] != member:
            raise CapabilityError("foreign_private_copy")
        if lineage:
            path, spec, own_descriptor = _source(target, project, allow_private=True)
            if own_descriptor["scope"] != "global":
                raise CapabilityError("private_template_shadowed")
            if spec.get("name") != target:
                raise CapabilityError("source_changed")
            parent_name = lineage["forked_from"]
        else:
            path, spec, _ = _source(target, project)
            parent_name = target
        error = ""
        try:
            parent_path, parent, descriptor = _source(parent_name, project)
            if intent and descriptor != intent["parent"]:
                raise CapabilityError("parent_identity_changed")
        except CapabilityError as exc:
            if not lineage:
                raise
            error = exc.code
            parent_path, parent = path, spec
            descriptor = (intent or {}).get(
                "parent", {"name": parent_name, "scope": "global", "source": "unknown"}
            )
        snapshot = {
            "member": member,
            "target": target,
            "spec": spec,
            "path": path,
            "parent_path": parent_path,
            "parent_spec": parent,
            "parent": descriptor,
            "intent": intent,
            "catalog": catalog,
            "project": project,
            "mode": "inherited" if intent else "legacy_snapshot" if lineage else "shared",
            "error": error,
            "binding": binding_data,
            "connections": connections,
            "raw_binding": raw.get("agents", {}).get(member),
            "overlay_binding": overlay.get("agents", {}).get(member),
            "raw_workspaces": raw.get("workspaces"),
            "raw_default_workspace": raw.get("default_workspace"),
        }
        snapshot["revision"] = self._token(
            {k: str(v) if isinstance(v, Path) else v for k, v in snapshot.items()}
        )
        snapshot["generation"] = governance_answer_generation()
        snapshot["revision"] = self._token([snapshot["revision"], snapshot["generation"]])
        return snapshot

    def get(self, member: str) -> dict:
        return self._view(self._snapshot(member))

    def _project(
        self, snap: dict, request: dict, *, reset_parent: bool = False
    ) -> tuple[dict, dict, list[dict]]:
        from kiro_crew.agent import _MANAGED_MCP_SERVERS

        if snap["error"]:
            raise CapabilityError(snap["error"])
        if snap["spec"].get("includeMcpJson", True) is not False and any(
            op.get("action") == "remove"
            and op.get("section") in ("mcpServers", "tools", "allowedTools", "autoApprove")
            for op in request["operations"]
        ):
            raise CapabilityError("global_mcp_exclusion_unrepresentable")
        intent = copy.deepcopy(snap["intent"])
        if intent is None:
            if not request.get("enroll"):
                raise CapabilityError("enrollment_required")
            intent = {
                "schema_version": 1,
                "parent": snap["parent"],
                "accepted": _rows(snap["parent_spec"], snap["catalog"]),
                "overrides": {section: {} for section in SECTIONS},
                "status": "pending",
            }
            if snap["mode"] == "legacy_snapshot":
                intent["ordinary_local"] = {key: True for key in ORDINARY_FIELDS}
                own = _rows(snap["spec"], snap["catalog"])
                for section in SECTIONS:
                    for key in dict.fromkeys([*intent["accepted"][section], *own[section]]):
                        intent["overrides"][section][key] = (
                            {"action": "set", "value": own[section][key]}
                            if key in own[section]
                            else {"action": "remove"}
                        )
        parent_rows = _rows(snap["parent_spec"], snap["catalog"])
        # Explicit inherit advances the baseline below. Validate acceptance
        # against genuine Parent changes before any operation erases them.
        parent_changes = {
            (section, key)
            for section in SECTIONS
            for key in intent["accepted"][section].keys() | parent_rows[section].keys()
            if intent["accepted"][section].get(key) != parent_rows[section].get(key)
        }
        if reset_parent and intent is not None:
            intent["ordinary_local"] = {}
            intent["overrides"] = {section: {} for section in SECTIONS}
            intent["accepted"] = _rows(snap["parent_spec"], snap["catalog"])
        for op in request["operations"]:
            section, key, action = op["section"], op["id"], op["action"]
            if section in ("prompt", "model") and key != section:
                raise CapabilityError("invalid_scalar_id", 400)
            if action == "inherit":
                intent["overrides"][section].pop(key, None)
                if key in parent_rows[section]:
                    intent["accepted"][section][key] = copy.deepcopy(parent_rows[section][key])
                else:
                    intent["accepted"][section].pop(key, None)
                continue
            value = copy.deepcopy(op.get("value"))
            if "retain_paths" in op:
                original = snap["spec"].get("mcpServers", {}).get(key, {})
                value = _retain_transport(value, op["retain_paths"], original)
            elif "value" in op and _contains_mask(value):
                raise CapabilityError("redacted_value_not_writable", 400)
            if "connection_id" in op:
                if section != "mcpServers" or op["connection_id"] not in snap["connections"]:
                    raise CapabilityError("unknown_connection", 400)
                value = copy.deepcopy(snap["connections"][op["connection_id"]])
                value.pop("autoApprove", None)
            if action == "set":
                if section == "mcpServers":
                    if not isinstance(value, dict) or not value or "autoApprove" in value:
                        raise CapabilityError("invalid_transport", 400)
                    allowed = {
                        "command",
                        "args",
                        "env",
                        "url",
                        "headers",
                        "type",
                        "timeout",
                        "disabled",
                        "disabledTools",
                        "oauthScopes",
                        "oauth",
                    }
                    if set(value) - allowed or ("command" in value and "url" in value):
                        raise CapabilityError("invalid_transport", 400)
                    if "oauth" in value and (
                        not isinstance(value["oauth"], dict)
                        or not all(
                            isinstance(k, str) and (k == "oauthScopes" or isinstance(v, str))
                            for k, v in value["oauth"].items()
                        )
                    ):
                        raise CapabilityError("invalid_transport", 400)
                    if not agent_state.capability_transport_fields_valid(value):
                        raise CapabilityError("invalid_transport", 400)
                    if key in _MANAGED_MCP_SERVERS:
                        if set(value) != {"disabled"} or type(value["disabled"]) is not bool:
                            raise CapabilityError("managed_transport_locked", 400)
                        original = parent_rows[section].get(key)
                        if original is None:
                            raise CapabilityError("managed_transport_unavailable")
                        value = {**original, "disabled": value["disabled"]}
                    elif ":" in key:
                        from kiro_crew.agent import _collect_app_mcp_servers

                        current = _collect_app_mcp_servers(audit=False).get(key)
                        if current is None:
                            raise CapabilityError("app_transport_unavailable", 400)
                        transport = {
                            k: copy.deepcopy(v) for k, v in current.items() if k != "autoApprove"
                        }
                        candidate = {k: v for k, v in value.items() if k != "disabled"}
                        if op.get("connection_id") == key:
                            candidate = {}
                        if candidate and candidate != {
                            k: v for k, v in transport.items() if k != "disabled"
                        }:
                            raise CapabilityError("app_transport_locked", 400)
                        value = {
                            **transport,
                            **({"disabled": value["disabled"]} if "disabled" in value else {}),
                        }
                    elif not isinstance(value.get("command", value.get("url")), str):
                        raise CapabilityError("invalid_transport", 400)
                elif section in ("tools", "allowedTools", "autoApprove"):
                    if value is not True:
                        raise CapabilityError("invalid_set_value", 400)
                elif section == "skills":
                    if value is not True or key not in snap["catalog"]:
                        raise CapabilityError("unknown_skill", 400)
                    value = snap["catalog"][key]
                elif section == "resources":
                    if value is True:
                        value = key
                    if not isinstance(value, str) or value != key:
                        raise CapabilityError("invalid_resource", 400)
                elif not isinstance(value, str) or (section == "model" and not value.strip()):
                    raise CapabilityError("invalid_scalar", 400)
            if section == "autoApprove":
                server, _ = _approval_ref(key)
                if server == "kirocrew-computer" and action == "set":
                    raise CapabilityError("managed_approval_locked", 400)
            intent["overrides"][section][key] = {"action": action}
            if action == "set":
                intent["overrides"][section][key]["value"] = value
        accepted = {(i["section"], i["id"]) for i in request["accept_parent"]}
        base = _ordinary_parent(
            snap["parent_spec"] if reset_parent else snap["spec"], snap["parent_spec"], intent
        )
        base["name"] = snap["target"]
        spec, baseline, changes = resolve_effective(
            base, snap["parent_spec"], intent, snap["catalog"], accepted
        )
        _maintain_owned(snap, spec)
        if not accepted <= parent_changes:
            raise CapabilityError("parent_change_missing")
        intent["accepted"] = baseline
        intent["catalog"] = snap["catalog"]
        if any(
            op["section"] in ("allowedTools", "autoApprove", "tools")
            for op in request["operations"]
        ) or spec.get("allowedTools") != snap["spec"].get("allowedTools"):
            _align_permissions(snap["spec"], spec)
        _sanitize_projection(spec, intent, snap["catalog"])
        if spec.get("includeMcpJson", True) is not False:
            before = _rows(snap["spec"], snap["catalog"])
            after = _rows(spec, snap["catalog"])
            if any(
                before[section].keys() - after[section].keys()
                for section in ("mcpServers", "tools", "allowedTools", "autoApprove")
            ):
                raise CapabilityError("global_mcp_exclusion_unrepresentable")
        return spec, intent, changes

    def _view(
        self,
        snap: dict,
        spec: dict | None = None,
        intent: dict | None = None,
        changes: list[dict] | None = None,
    ) -> dict:
        selected = spec if spec is not None else snap["spec"]
        intent = intent if intent is not None else snap["intent"]
        if changes is None and intent and not snap["error"]:
            _, _, changes = resolve_effective(
                selected, snap["parent_spec"], intent, snap["catalog"]
            )
        rows = _rows(selected, snap["catalog"])
        overrides = intent["overrides"] if intent else {}
        from kiro_crew.agent import _MANAGED_MCP_SERVERS

        output = []
        for section in SECTIONS:
            keys = dict.fromkeys([*rows[section], *overrides.get(section, {})])
            if section in ("prompt", "model"):
                keys.setdefault(section)
            for key in keys:
                override = overrides.get(section, {}).get(key)
                output.append(
                    {
                        "section": section,
                        "id": key,
                        "label": key,
                        "state": (
                            "inherited"
                            if not override
                            else "removed" if override["action"] == "remove" else "local"
                        ),
                        "present": key in rows[section],
                        "value": rows[section].get(
                            key, "" if section in ("prompt", "model") else None
                        ),
                        "managed": section == "mcpServers" and key in _MANAGED_MCP_SERVERS,
                        "shared_reference": section in ("skills", "resources"),
                    }
                )
        descriptor = {k: v for k, v in snap["parent"].items() if k not in ("path", "project")}
        descriptor["available"] = not bool(snap["error"])
        if snap["error"]:
            descriptor["error_code"] = snap["error"]
        runtime: dict[str, Any] = {
            "status": "pending" if intent else "unverified",
            "saved_revision": intent.get("revision", "") if intent else "",
            "sessions": [],
        }
        if spec is None and intent:
            try:
                prepare_member_capabilities(snap["member"], snap["project"])
            except CapabilityError as exc:
                runtime.update(status="failed", error_code=exc.code)
        return safe_view(
            {
                "schema_version": 1,
                "member": snap["member"],
                "mode": "inherited" if intent else snap["mode"],
                "revision": snap["revision"],
                "template": descriptor,
                "rows": output,
                "connections": [
                    {"id": k, "label": k, "managed": k in _MANAGED_MCP_SERVERS}
                    for k in snap["connections"]
                ],
                "skills": [
                    {"id": k, "label": k, "shared_reference": True} for k in snap["catalog"]
                ],
                "parent_changes": changes or [],
                "runtime": runtime,
            }
        )

    def _write_bindings(self, prepared: dict[str, dict], mutate: Callable[[dict], Any]) -> dict:
        """One atomic binding delta, with base then overlay locks on every path."""
        from kiro_crew.config.loader import (
            _config_write_lock,
            _deep_merge,
            config_path,
            read_config_for_update,
        )

        local = any(snap.get("overlay_binding") is not None for snap in prepared.values())
        base_path, overlay_path = config_path(), config_local_path()
        target = overlay_path if local else base_path
        with ExitStack() as locks:
            if local:
                locks.enter_context(_config_write_lock(base_path.resolve()))

            def apply(document: dict) -> dict | None:
                if not local:
                    locks.enter_context(_config_write_lock(overlay_path.resolve()))
                base = read_config_for_update(base_path) if local else document
                overlay = document if local else read_config_for_update(overlay_path)
                effective = _deep_merge(copy.deepcopy(base), overlay)
                previous = copy.deepcopy(effective.get("agents", {}))
                result = mutate(effective)
                if result is None:
                    return None
                changed = False
                for member in prepared:
                    binding = effective["agents"][member]["kiro_agent"]
                    if binding != previous.get(member, {}).get("kiro_agent"):
                        document.setdefault("agents", {}).setdefault(member, {})[
                            "kiro_agent"
                        ] = binding
                        changed = True
                return document if changed else None

            return update_config_locked(target, mutate=apply, stamp_meta=not local)

    def _plans(
        self,
        member: str,
        body: dict,
        document: dict | None = None,
        prepared: dict | None = None,
        *,
        reset_parent: bool = False,
    ) -> list[tuple[dict, dict, dict, list[dict]]]:
        snap = self._snapshot(member, document, (prepared or {}).get(member))
        if body["revision"] != snap["revision"]:
            raise CapabilityError("stale_revision")
        names = list(dict.fromkeys([member, *body["accept_members"]]))
        plans = []
        for name in names:
            other = (
                snap
                if name == member
                else self._snapshot(name, document, (prepared or {}).get(name))
            )
            if other["parent"] != snap["parent"] or (name != member and not other["intent"]):
                raise CapabilityError("member_parent_mismatch")
            request = body if name == member else {**body, "operations": [], "enroll": False}
            spec, intent, changes = self._project(other, request, reset_parent=reset_parent)
            plans.append((other, spec, intent, changes))
        return plans

    def _preview_token(self, plans: list, body: dict) -> str:
        return self._token(
            [
                {k: v for k, v in body.items() if k != "preview_token"},
                [(snap["revision"], spec, intent) for snap, spec, intent, _ in plans],
            ]
        )

    def preview(self, member: str, body: Any, *, reset_parent: bool = False) -> dict:
        request = validate_request(body)
        plans = self._plans(member, request, reset_parent=reset_parent)
        snap, spec, intent, changes = plans[0]
        view = self._view(snap, spec, intent, changes)
        impact = []
        for current, proposed, _, _ in plans:
            old, new = _rows(current["spec"], current["catalog"]), _rows(
                proposed, current["catalog"]
            )
            for section in SECTIONS:
                for key in dict.fromkeys([*old[section], *new[section]]):
                    before, after = old[section].get(key), new[section].get(key)
                    if before != after:
                        impact.append(
                            {
                                "member": current["member"],
                                "section": section,
                                "id": key,
                                "change": (
                                    "added"
                                    if before is None
                                    else "removed" if after is None else "changed"
                                ),
                                "approval_expanded": section in ("allowedTools", "autoApprove")
                                and after is not None,
                            }
                        )
        view["impact"] = safe_view(impact)
        view["preview_token"] = self._preview_token(plans, request)
        return view

    def reset(self, member: str, expected_template: str) -> dict:
        """Explicit owner reset accepts the verified current Parent as a whole."""
        snap = self._snapshot(member)
        if snap["target"] != expected_template or not snap["intent"]:
            raise CapabilityError("stale_binding")
        if snap["error"]:
            raise CapabilityError(snap["error"])
        parent_rows = _rows(snap["parent_spec"], snap["catalog"])
        body = {
            "revision": snap["revision"],
            "operations": [
                {"section": section, "id": key, "action": "inherit"}
                for section in SECTIONS
                for key in dict.fromkeys(
                    [*snap["intent"]["overrides"].get(section, {}), *parent_rows[section]]
                )
            ],
        }
        preview = self.preview(member, body, reset_parent=True)
        return self.put(
            member, {**body, "preview_token": preview["preview_token"]}, reset_parent=True
        )

    def _finish_publish(self, member: str, expected_template: str, name: str) -> dict:
        """Clear only this publication's lineage, with normal config-first locks."""
        snapshot = self._snapshot(member)
        root = kiro_agents_dir_path()

        def finish(document: dict) -> None:
            with agents_spec_lock(root), agent_state._locked():
                receipt = agent_state.get_publish_info(name)
                if (
                    receipt is None
                    or receipt["member"] != member
                    or expected_template not in (receipt["source"], name)
                    or document.get("agents", {}).get(member, {}).get("kiro_agent") != name
                ):
                    raise CapabilityError("stale_binding")
                fresh = self._snapshot(member, document, snapshot)
                if fresh["project"] != receipt["parent"].get("project", ""):
                    raise CapabilityError("parent_identity_changed")
                path, spec = fresh["path"], fresh["spec"]
                if path.resolve() != (root / (name + ".json")).resolve():
                    raise CapabilityError("publish_destination_changed")
                if _digest(spec) != receipt["digest"]:
                    raise CapabilityError("publish_destination_changed")
                governed = copy.deepcopy(spec)
                sanitize_agent_config_governance(governed, audit=False)
                if governed != spec:
                    raise CapabilityError("governance_changed")
                state = agent_state._read(strict=True)
                entry = state[name]
                if "private_to" not in entry and "forked_from" not in entry:
                    return None
                if (
                    entry.get("private_to") != member
                    or entry.get("forked_from") != receipt["parent"]["name"]
                    or "capabilities" in entry
                ):
                    raise CapabilityError("foreign_private_copy")
                entry.pop("private_to")
                entry.pop("forked_from")
                # Keep the receipt: a lost HTTP acknowledgement must remain
                # retryable without re-publishing or changing the binding.
                agent_state._write(state)
            return None

        result = {"ok": True, "template": name, "filename": name + ".json"}
        try:
            self._write_bindings({member: snapshot}, finish)
        except CapabilityError:
            raise
        except (OSError, ValueError):
            # Binding publication already succeeded. Match the legacy endpoint:
            # name the committed target and the pending sharing step truthfully.
            result["warning"] = "publish_incomplete"
        from kiro_crew.agent_discovery import clear_list_agents_cache

        clear_list_agents_cache()
        return result

    def publish(self, member: str, expected_template: str, name: str) -> dict:
        """Flatten one verified saved snapshot without exporting private intent."""
        import re

        from kiro_crew.agent import OWNED_KIRO_AGENT_FILES
        from kiro_crew.constants import WINDOWS_DEVICE_STEMS

        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name)
            or name.split(".", 1)[0].lower() in WINDOWS_DEVICE_STEMS
            or name.lower() in {Path(f).stem.lower() for f in OWNED_KIRO_AGENT_FILES}
        ):
            raise CapabilityError("invalid_template_name", 400)
        snap = self._snapshot(member)
        receipt = agent_state.get_publish_info(name)
        if receipt is not None:
            if receipt["member"] != member or expected_template not in (receipt["source"], name):
                raise CapabilityError("name_taken")
            if snap["target"] == name:
                return self._finish_publish(member, expected_template, name)
            if snap["target"] != receipt["source"] or expected_template != receipt["source"]:
                raise CapabilityError("stale_binding")
        if snap["target"] != expected_template or not snap["intent"]:
            raise CapabilityError("stale_binding")
        prepare_member_capabilities(member, snap["project"])
        root = kiro_agents_dir_path()
        with ExitStack() as locks:

            def mutate(document: dict) -> dict:
                locks.enter_context(agents_spec_lock(root))
                locks.enter_context(agent_state._locked())
                fresh = self._snapshot(member, document, snap)
                if fresh["revision"] != snap["revision"]:
                    raise CapabilityError("stale_revision")
                spec = copy.deepcopy(fresh["spec"])
                spec["name"] = name
                sanitize_agent_config_governance(spec)
                if governance_answer_generation() != snap["generation"]:
                    raise CapabilityError("governance_changed")
                publication = {
                    "member": member,
                    "source": expected_template,
                    "source_digest": _digest(fresh["spec"]),
                    "digest": _digest(spec),
                    "parent": snap["parent"],
                }
                state = agent_state._read(strict=True)
                pending = agent_state.get_publish_info(name)
                if pending is not None:
                    entry = state[name]
                    if (
                        pending != publication
                        or entry.get("private_to") != member
                        or entry.get("forked_from") != snap["parent"]["name"]
                        or "capabilities" in entry
                    ):
                        raise CapabilityError("publish_source_changed")
                    if any(
                        v.get("kiro_agent") == name
                        for v in document.get("agents", {}).values()
                        if isinstance(v, dict)
                    ):
                        raise CapabilityError("name_taken")
                    if document.get("agent", {}).get("default_agent") == name:
                        raise CapabilityError("name_taken")
                    try:
                        path, staged, identity = _source(name, snap["project"], allow_private=True)
                    except CapabilityError as exc:
                        if exc.code != "parent_missing":
                            raise
                        atomic_write(
                            root / (name + ".json"),
                            json.dumps(spec, indent=2) + "\n",
                            restrict_to_owner=True,
                        )
                    else:
                        if (
                            identity["scope"] != "global"
                            or path.resolve() != (root / (name + ".json")).resolve()
                            or _digest(staged) != publication["digest"]
                        ):
                            raise CapabilityError("publish_destination_changed")
                    document["agents"][member]["kiro_agent"] = name
                    return document
                roots = [root]
                if snap["project"]:
                    roots.append(project_agents_dir(snap["project"]))
                occupied = {p.stem.lower() for directory in roots for p in directory.glob("*.json")}
                from kiro_crew.agent_discovery import _read_agent_spec

                for directory in roots:
                    for path in directory.glob("*.json"):
                        declared = _read_agent_spec(
                            path, operation="capability_publish", source="dashboard"
                        )
                        if declared is not None:
                            occupied.add(str(declared.get("name", path.stem)).lower())
                occupied.update(
                    str(v.get("kiro_agent", "")).lower()
                    for v in document.get("agents", {}).values()
                    if isinstance(v, dict)
                )
                legacy_default = document.get("agent", {}).get("default_agent", "")
                if isinstance(legacy_default, str):
                    occupied.add(legacy_default.lower())
                if name.lower() in occupied:
                    raise CapabilityError("name_taken")
                if name in state:
                    raise CapabilityError("name_taken")
                state[name] = {
                    "private_to": member,
                    "forked_from": snap["parent"]["name"],
                    "publish": publication,
                }
                agent_state._write(state)
                atomic_write(
                    root / (name + ".json"),
                    json.dumps(spec, indent=2) + "\n",
                    restrict_to_owner=True,
                )
                document["agents"][member]["kiro_agent"] = name
                return document

            self._write_bindings({member: snap}, mutate)
        return self._finish_publish(member, expected_template, name)

    def put(self, member: str, body: Any, *, reset_parent: bool = False) -> dict:
        request = validate_request(body, commit=True)
        prepared = {
            plan[0]["member"]: plan[0]
            for plan in self._plans(member, request, reset_parent=reset_parent)
        }
        root = kiro_agents_dir_path()
        with ExitStack() as locks:
            committed: list[tuple[dict, dict, str]] = []
            unchanged: dict[str, dict] = {}

            def mutate(document: dict) -> dict | None:
                locks.enter_context(agents_spec_lock(root))
                locks.enter_context(agent_state._locked())
                plans = self._plans(member, request, document, prepared, reset_parent=reset_parent)
                if not hmac.compare_digest(
                    request["preview_token"], self._preview_token(plans, request)
                ):
                    raise CapabilityError("stale_preview")
                state = agent_state._read(strict=True)
                changed_binding = False
                for snap, spec, intent, _ in plans:
                    if snap["intent"] and spec == snap["spec"] and intent == snap["intent"]:
                        intent.setdefault("revision", secrets.token_hex(16))
                        intent["status"] = "saved"
                        intent["governance_generation"] = snap["generation"]
                        unchanged[snap["target"]] = intent
                        continue
                    # Metadata-only choices also need a private identity: the
                    # binding delta commits intent, not just effective bytes.
                    # Failure leaves each old baseline and local choice intact.
                    target = "crew-" + secrets.token_hex(12)
                    if (root / (target + ".json")).exists() or target in state:
                        raise CapabilityError("publication_name_taken")
                    changed_binding = True
                    spec["name"] = target
                    sanitize_agent_config_governance(spec)
                    if governance_answer_generation() != snap["generation"]:
                        raise CapabilityError("governance_changed")
                    intent["revision"] = secrets.token_hex(16)
                    intent["status"] = "pending"
                    intent["materialized"] = _digest(spec)
                    intent["governance_generation"] = snap["generation"]
                    if snap["intent"]:
                        pending = copy.deepcopy(snap["intent"])
                        pending["status"] = "pending"
                        state[snap["target"]]["capabilities"] = pending
                    state.setdefault(target, {}).update(
                        {
                            "private_to": snap["member"],
                            "forked_from": snap["parent"]["name"],
                            "capabilities": intent,
                        }
                    )
                    committed.append((snap, spec, target))
                # Intent first: a failed materialization is retryable and never
                # advertises a half-written spec as a public template.
                agent_state._write(state)
                for snap, spec, target in committed:
                    path = root / (target + ".json")
                    atomic_write(path, json.dumps(spec, indent=2) + "\n", restrict_to_owner=True)
                    document["agents"][snap["member"]]["kiro_agent"] = target
                return document if changed_binding else None

            self._write_bindings(prepared, mutate)
            state = agent_state._read(strict=True)
            for target, intent in unchanged.items():
                state[target]["capabilities"] = intent
            for _, _, target in committed:
                state[target]["capabilities"]["status"] = "saved"
            agent_state._write(state)
        from kiro_crew.agent_discovery import clear_list_agents_cache

        clear_list_agents_cache()
        return self.get(member)


def require_unmanaged_template(name: str) -> None:
    """Legacy write paths must not flatten or bypass an enrolled definition."""
    try:
        intent = agent_state.get_capabilities(name)
    except (ValueError, OSError):
        raise CapabilityError("capabilities_unavailable", 503) from None
    if intent is not None:
        raise CapabilityError("capabilities_editor_required")


def prepare_member_capabilities(member: str, project_dir: str | Path | None = None) -> dict:
    """Read-only startup seam; success proves saved bytes, never runtime loading."""
    cfg = KiroCrewConfig.load()
    binding = cfg.agents.get(member)
    if binding is None:
        raise CapabilityError("agent_not_found", 404)
    intent = agent_state.get_capabilities(binding.kiro_agent)
    if intent is None:
        return {"template": binding.kiro_agent, "revision": "", "status": "unverified"}
    project = str(Path(project_dir).resolve()) if project_dir else ""
    if project != intent["parent"]["project"]:
        raise CapabilityError("project_identity_changed")
    if intent.get("status") != "saved":
        raise CapabilityError("materialization_pending")
    if intent.get("governance_generation") != governance_answer_generation():
        raise CapabilityError("governance_reconciliation_pending")
    lineage = agent_state.get_fork_info(binding.kiro_agent, strict=True)
    if not lineage or lineage["private_to"] != member:
        raise CapabilityError("foreign_private_copy")
    _, _, descriptor = _source(intent["parent"]["name"], project)
    if descriptor != intent["parent"]:
        raise CapabilityError("parent_identity_changed")
    _, spec, own = _source(binding.kiro_agent, project, allow_private=True)
    if own["scope"] != "global":
        raise CapabilityError("private_template_shadowed")
    if _digest(spec) != intent.get("materialized"):
        raise CapabilityError("materialization_changed")
    if intent.get("governance_generation") != governance_answer_generation():
        raise CapabilityError("governance_reconciliation_pending")
    return {
        "template": binding.kiro_agent,
        "revision": intent.get("revision", ""),
        "status": "unverified",
        "mcp_servers": tuple(
            name
            for name, server in spec.get("mcpServers", {}).items()
            if not server.get("disabled", False)
        ),
    }


def reconcile_member_capabilities(member: str) -> None:
    """Publish changed effective bytes as a new generation; leave live specs intact."""
    cfg = KiroCrewConfig.load()
    binding = cfg.agents.get(member)
    if binding is None:
        raise CapabilityError("agent_not_found", 404)
    saved = agent_state.get_capabilities(binding.kiro_agent)
    if saved is None:
        return
    service = CapabilityService(catalog=lambda project: saved.get("catalog", {}))
    snapshot = service._snapshot(member)
    if _digest(snapshot["spec"]) != saved.get("materialized"):
        raise CapabilityError("materialization_changed")
    body = {"revision": snapshot["revision"]}
    preview = service.preview(member, body)
    service.put(member, {**body, "preview_token": preview["preview_token"]})
