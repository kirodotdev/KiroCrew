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
import logging
import secrets
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable

from kiro_crew import agent_state, mate_capabilities
from kiro_crew.agent import (
    OWNED_KIRO_AGENT_FILES,
    _is_confirmed_managed_dashboard_author,
    _read_spec_capped,
    _spec_permissions_accepted,
    agents_spec_lock,
    kiro_agents_dir_path,
)
from kiro_crew.agent_capabilities_withheld import withheld_approvals as _withheld_approvals
from kiro_crew.agent_files import DASHBOARD_AUTHOR_AGENT_FILENAME
from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions
from kiro_crew.agent_spec_format import (
    agent_spec_candidates,
    iter_agent_spec_files,
    parse_agent_spec_bytes,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    KiroCrewConfig,
    config_local_path,
    default_project_dir,
    update_config_locked,
)
from kiro_crew.config.paths import mate_agents_dir, project_agents_dir
from kiro_crew.mcp_cleanup import mcp_entry_is_muted
from kiro_crew.platform import redact_via_context
from kiro_crew.platform.governance import sanitize_agent_config_governance
from kiro_crew.platform.governance_profiles import governance_answer_generation
from kiro_crew.validation import is_registered_agent_name

logger = logging.getLogger(__name__)

SECTIONS = agent_state.CAPABILITY_SECTIONS
MAX_OPERATIONS = 300
MAX_DOCUMENT_BYTES = 1024 * 1024

#: Managed servers a member's copy may carry when its Parent does not: the
#: crewmate's own record, which every crewmate is meant to have. Its transport
#: is never the request's: it is the entry the default template carries
#: (:func:`_member_platform_transport`), refreshed on every projection.
MEMBER_PLATFORM_SERVERS = frozenset({"kirocrew-mate"})


def _member_platform_transport(name: str) -> dict | None:
    from kiro_crew import agent as agent_mod

    return agent_mod.managed_member_transport(name)


def _platform_entry(transport: dict, disabled: bool) -> dict:
    """*transport* as the copy's entry: ``disabled`` only when it is a mute.

    An enabled entry carries no ``disabled`` key at all, exactly as the default
    template's does. A ``"disabled": false`` would be a key a per-session element
    cannot express, so the session mount would withhold the server's identity
    element and every call would refuse ``identity_unattested``.
    """
    entry = {k: v for k, v in transport.items() if k != "disabled"}
    if disabled:
        entry["disabled"] = True
    return entry


class CapabilityError(ValueError):
    """A bounded machine code, never source bytes or credentials."""

    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code = code
        self.status = status
        #: Crew member whose spec failed, set by the session start seam.
        self.member = ""
        #: Basename of the agent file a refusal is about, as resolved on disk
        #: (``<name>.md`` or a file declaring the name both count). Never a
        #: directory: the agents folder is fixed per scope and a path is not.
        self.file = ""
        #: The draft's own approval reference a shape refusal is about, echoed
        #: back so the pane names it without re-implementing the shape rule.
        self.ref = ""
        #: The project a start refusal names: the saved one for
        #: ``project_identity_changed``, the one holding the shadowing file for
        #: ``private_template_shadowed``.
        self.project = ""


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _read_spec(path: Path) -> dict:
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    try:
        raw = safe_read_file_bytes_nolink(str(path), str(path.parent), max_bytes=MAX_DOCUMENT_BYTES)
        result = parse_agent_spec_bytes(raw, path) if raw is not None else None
    except (ValueError, FileTooLargeError):
        raise CapabilityError("source_unreadable") from None
    if not isinstance(result, dict):
        raise CapabilityError("source_unreadable")
    return result


def _source(name: str, project: str, *, allow_private: bool = False) -> tuple[Path, dict, dict]:
    """Project scope wins exactly as it does for the provider's cwd."""
    from kiro_crew.agent import OWNED_KIRO_AGENT_FILES, _conflicting_spec_for, agent_spec_path

    if not is_registered_agent_name(name):
        raise CapabilityError("invalid_template_name")
    roots = [(project_agents_dir(project), "project")] if project else []
    roots += [(kiro_agents_dir_path(), "global"), (mate_agents_dir(), "mate")]
    for root, scope in roots:
        try:
            path = agent_spec_path(name, agents_dir=root)
        except ValueError:
            raise CapabilityError("ambiguous_template_name") from None
        if path is None:
            # A broken file claiming this exact filename cannot authorize a
            # fallback to the broader global scope. Unrelated junk is skipped
            # by the shared resolver, just as it is on the normal agent path.
            if any(p.exists() for p in agent_spec_candidates(root, name)):
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

            if scope == "project":
                source = "project"
            elif path.name in OWNED_KIRO_AGENT_FILES:
                source = "builtin"
            elif _global_agent_info(path, spec).source == "package":
                source = "package"
            else:
                source = "custom"
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
        error = CapabilityError("invalid_approval_ref", 400)
        error.ref = ref
        raise error
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
            if old is None:
                kind = "added"
            elif new is None:
                kind = "removed"
            else:
                kind = "changed"
            changes.append(
                {
                    "section": section,
                    "id": key,
                    "kind": kind,
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
    """Review the source block, then emit the target one the installed CLI accepts.

    Two questions share this call site and only the second one is the binary's.
    Whether the SOURCE block was hand-edited away from its derivation is a
    governance question about the template, so that comparison is unconditional.
    What the TARGET spec may carry is a compatibility question: kiro-cli
    validates specs with serde ``deny_unknown_fields``, so a release below
    ``SPEC_PERMISSIONS_MIN_VERSION`` -- or one whose version cannot be
    established -- refuses the WHOLE file this funnel publishes and drops every
    Crew MCP server with it. Routed through ``_write_derived_permissions`` so
    fork and publish answer that question with the same gate the five generated
    writers use, including its removal of an inherited value.
    """
    from kiro_crew.agent import _write_derived_permissions
    from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions

    if _alternate_shortcuts(base.get("toolsSettings", {})) or base.get("autoAllowReadonly"):
        raise CapabilityError("alternate_permissions_require_review")
    if "permissions" in base:
        prior = derived_agent_permissions(base.get("allowedTools"), str(base.get("name", "")))
        if base["permissions"] != prior:
            raise CapabilityError("alternate_permissions_require_review")
        _write_derived_permissions(spec, spec.get("allowedTools"), str(spec.get("name", "")))


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

#: Top-level keys the projection writes itself, so their presence on a saved spec
#: says nothing about a hand edit (``_snapshot`` already refuses a ``name`` that
#: disagrees with the binding).
_STRUCTURAL_FIELDS = frozenset({"name"})

#: Top-level keys outside the reviewed sections that nothing at runtime reads, so a
#: change in one grants nothing. A positive allowlist, deliberately tiny: an
#: unlisted key (``toolsSettings``, ``managedToolPolicy``, ``excludedTools``, or
#: one a later kiro-cli adds) is one the Capabilities review cannot render, so a
#: change there is refused rather than stamped as reviewed.
_INERT_FIELDS = frozenset({"$schema"})

#: Keys ``_refresh_dynamic_fields`` rewrites on every projection of a fork of a
#: Crew-owned template. Their bytes are Crew's own output, not a hand edit, so a
#: projection that reproduced the file (the no-op branch) has vouched for them.
#: On any other parent they pass through untouched and stay unvouched.
_REBUILT_FIELDS = frozenset({"hooks", "includeMcpJson"})


def _parent_is_owned(snap: dict) -> bool:
    """Is the capability projection's parent template one Crew owns?

    A global-scope parent whose filename is in ``OWNED_KIRO_AGENT_FILES`` -- except the
    dashboard-author stem, which was a user-creatable template name before it became owned.
    For that stem the owned classification holds ONLY when the on-disk spec POSITIVELY
    confirms as this installer's own -- its bytes reproduce the installer-recorded ownership
    digest; a pre-existing user template at the stem -- one whose bytes do not reproduce it,
    or an absent/unreadable file -- is NOT an owned parent, so a capability save
    (``_maintain_owned`` / ``_unvouched``) does not refresh its inherited custom guards away
    and publication does not bind the crew to that replacement. Fail-closed: unconfirmed is a
    user file. The install gate and the fork-governance origin check apply the same gate.
    """
    parent_path = snap["parent"].get("path", "")
    name = Path(parent_path).name
    if snap["parent"].get("scope") != "global" or name not in OWNED_KIRO_AGENT_FILES:
        return False
    if name == DASHBOARD_AUTHOR_AGENT_FILENAME:
        spec = _read_spec_capped(Path(parent_path))
        return _is_confirmed_managed_dashboard_author(spec)
    return True


def _fork_refresh_failed_for(name: str) -> bool:
    """Did the last fork refresh record a failure for *name* (or for every fork)?"""
    import kiro_crew.agent as agent_facade

    failed: frozenset[str] = getattr(agent_facade, "_fork_refresh_failed", frozenset())
    return bool(name) and (name in failed or "*" in failed)


def _detach_owned(snap: dict) -> bool:
    """Would the fork refresh re-apply owned plumbing to this member's copy?

    Asks the refresh's own rule (origin NAME, any scope) as well as
    ``_parent_is_owned`` (global scope only): a project-scoped parent at an
    owned name is refreshed as owned too.
    """
    from kiro_crew.agent import fork_origin_is_owned

    return _parent_is_owned(snap) or fork_origin_is_owned(snap["target"])


def _detach_blocker(snap: dict, intent: dict, owned: bool) -> str | None:
    """The code that refuses Stop following for this saved setup, else None.

    Once the intent is gone the member runs as an independent copy, so a
    setup that copy could not keep is refused: single-tool limits (chat
    start checks them against the backend only for a saved setup) and a
    removal under a template Crew owns (the refresh restores it).
    """
    spec = snap["spec"]
    if tool_limited_servers(spec):
        return "detach_tool_limits_unkept"
    if owned and any(
        override.get("action") == "remove"
        for section in SECTIONS
        for override in intent["overrides"].get(section, {}).values()
    ):
        return "detach_removal_unkept"
    return None


def _detach_loses_mcp_json(snap: dict) -> bool:
    """Does the copy also read the global mcp.json (``includeMcpJson`` unset)?

    The fork refresh pins that merge off for a private copy with no saved
    intent, so stopping turns it off. That is the state such a copy had
    before it was enrolled, but it changes which servers the member loads, so
    the owner confirms it (``accept_mcp_json``) instead of it happening silently.
    """
    return snap["spec"].get("includeMcpJson", True) is not False


def _unvouched(snap: dict, spec: dict) -> list[str]:
    """Top-level keys of *spec* the review cannot show and this funnel cannot vouch for.

    Stamping ``materialized`` declares the whole file reviewed. The review renders
    ``SECTIONS`` and ``ORDINARY_FIELDS``; everything else it only passes through.
    A pass-through key is still safe to stamp when its value is the parent
    template's (inherited, not edited), when Crew itself wrote its bytes
    (``_REBUILT_FIELDS`` on an owned-template fork; ``permissions`` equal to the
    derivation ``_align_permissions`` would emit) or when nothing reads it
    (``_INERT_FIELDS``). What remains is a hand edit no one has seen.
    """
    vouched = set(SECTIONS) | set(ORDINARY_FIELDS) | _STRUCTURAL_FIELDS | _INERT_FIELDS
    if _parent_is_owned(snap):
        vouched |= _REBUILT_FIELDS
    if spec.get("permissions") == derived_agent_permissions(
        spec.get("allowedTools"), str(spec.get("name", ""))
    ):
        vouched.add("permissions")
    parent = snap["parent_spec"]
    if "permissions" not in spec and "permissions" in parent:
        # The writer's own gate removed it, so it is Crew's output, not a hand edit.
        if not _spec_permissions_accepted():
            vouched.add("permissions")
    # A deleted key is drift too: kiro-cli reads an absent key as its default
    # (``includeMcpJson`` absent means true), so a dropped parent key can grant.
    return sorted(
        key
        for key in (set(spec) | set(parent)) - vouched
        if key not in spec or key not in parent or spec[key] != parent[key]
    )


def _stamp_reviewed(snap: dict, spec: dict, intent: dict) -> bool:
    """Stamp *spec* as the reviewed bytes, refusing unvouched drift first.

    The one place ``materialized`` is written by a save, so a restamp can only
    follow the vouch check. When the file drifted since its last review and the
    projection carries a key the review cannot show, the save fails closed
    before anything is stamped. Returns whether the file had drifted.
    """
    drifted = bool(snap["intent"]) and snap["intent"].get("materialized") != _digest(snap["spec"])
    if drifted and _unvouched(snap, spec):
        error = CapabilityError("unreviewable_drift")
        error.file = snap["target"] + ".json"
        raise error
    intent["materialized"] = _digest(spec)
    return drifted


def _maintain_owned(snap: dict, spec: dict) -> None:
    """Refresh host plumbing without regranting omitted capability entries."""
    from kiro_crew.agent import (
        _collect_app_mcp_servers,
        _refresh_dynamic_fields,
    )

    selected = copy.deepcopy(spec.get("mcpServers", {}))
    if _parent_is_owned(snap):
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
    else:
        # A platform server the copy carries and its Parent does not is Crew's own
        # entry, so it is kept current here the way an owned Parent's refresh keeps
        # it, and retracted when a fresh build would not emit it.
        parent_servers = snap["parent_spec"].get("mcpServers") or {}
        for key in MEMBER_PLATFORM_SERVERS & selected.keys() - parent_servers.keys():
            current = _member_platform_transport(key)
            if current is None:
                spec.get("mcpServers", {}).pop(key, None)
                continue
            entry = _platform_entry(current, selected[key].get("disabled") is True)
            # A per-tool restriction the copy already holds survives the refresh:
            # the transport is Crew's to keep current, the restriction is not.
            retained = selected[key].get("disabledTools")
            if retained:
                entry["disabledTools"] = list(retained)
            spec.setdefault("mcpServers", {})[key] = entry
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
                shadowed = CapabilityError("private_template_shadowed")
                shadowed.file = path.name
                shadowed.project = project
                raise shadowed
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
        if intent:
            mode = "inherited"
        elif lineage:
            mode = "legacy_snapshot"
        else:
            mode = "shared"
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
            "mode": mode,
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

    def _mate_catalog(self, member: str) -> dict[str, str]:
        return mate_capabilities.skill_catalog(member, self._catalog)

    def get(self, member: str) -> dict:
        if mate_capabilities.binding_is_mate(member):
            return mate_capabilities.get(member, self._mate_catalog(member))
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
                        platform = original is None and key in MEMBER_PLATFORM_SERVERS
                        if platform:
                            original = _member_platform_transport(key)
                        if original is None:
                            raise CapabilityError("managed_transport_unavailable")
                        value = (
                            _platform_entry(original, value["disabled"])
                            if platform
                            else {**original, "disabled": value["disabled"]}
                        )
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
        if (
            reset_parent
            or any(
                op["section"] in ("allowedTools", "autoApprove", "tools")
                for op in request["operations"]
            )
            or spec.get("allowedTools") != snap["spec"].get("allowedTools")
        ):
            _align_permissions(base, spec)
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
                if not override:
                    state = "inherited"
                elif override["action"] == "remove":
                    state = "removed"
                else:
                    state = "local"
                output.append(
                    {
                        "section": section,
                        "id": key,
                        "label": key,
                        "state": state,
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
        # Only a saved (or previewed, which always carries its projected
        # intent) spec is gated at chat start; an unenrolled member runs its
        # template as before and gets no warning.
        backend_warning = member_backend_warning(selected) if intent else None
        detach_blocked = None
        detach_mcp_json_loss = False
        if intent and isinstance(snap.get("spec"), dict) and snap.get("target"):
            try:
                detach_blocked = _detach_blocker(snap, intent, _detach_owned(snap))
                detach_mcp_json_loss = _detach_loses_mcp_json(snap)
            except Exception:
                detach_blocked = None
                detach_mcp_json_loss = False
        return safe_view(
            {
                "backend_warning": backend_warning,
                "detach_blocked": detach_blocked,
                "detach_mcp_json_loss": detach_mcp_json_loss,
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
        if mate_capabilities.binding_is_mate(member):
            return mate_capabilities.preview(member, body, self._mate_catalog(member))
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
                        if before is None:
                            change = "added"
                        elif after is None:
                            change = "removed"
                        else:
                            change = "changed"
                        impact.append(
                            {
                                "member": current["member"],
                                "section": section,
                                "id": key,
                                "change": change,
                                "approval_expanded": section in ("allowedTools", "autoApprove")
                                and after is not None,
                            }
                        )
        view["impact"] = safe_view(impact)
        view["withheld_approvals"] = _withheld_approvals(request, spec, intent, snap["catalog"])
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

    def detach(self, member: str, body: Any) -> dict:
        """Stop following the Parent: the member keeps its saved values as its own.

        The member's private copy already holds the materialized effective spec,
        so dropping the inheritance intent leaves an independent private copy
        (``legacy_snapshot``) with exactly the values the member runs today. No
        value is recomputed or loosened. Only verified saved bytes are kept: a
        pending save or a drifted file is refused, so the member never keeps
        bytes nobody reviewed. The only write is the sidecar entry, so a failure
        changes nothing; a governance answer newer than the save is applied by
        the fork refresh that follows. The Parent itself need not be readable: a
        missing Parent is a reason to leave.
        """
        if (
            not isinstance(body, dict)
            or "revision" not in body
            or not set(body) <= {"revision", "accept_mcp_json"}
        ):
            raise CapabilityError("invalid_request", 400)
        if not isinstance(body["revision"], str):
            raise CapabilityError("invalid_revision", 400)
        accept_mcp_json = body.get("accept_mcp_json", False)
        if not isinstance(accept_mcp_json, bool):
            raise CapabilityError("invalid_request", 400)
        snap = self._snapshot(member)
        if body["revision"] != snap["revision"]:
            raise CapabilityError("stale_revision")
        if not snap["intent"]:
            raise CapabilityError("not_inherited")
        root = kiro_agents_dir_path()
        from kiro_crew.agent import hold_spawn_gate

        stale_governance = [False]
        mcp_json_pinned = [False]
        target_name = [""]
        # Hold the spawn gate across the write and the refresh it triggers, so a
        # session cannot start on not-yet-refiltered grants between them.
        with hold_spawn_gate():
            with ExitStack() as locks:

                def mutate(document: dict) -> None:
                    locks.enter_context(agents_spec_lock(root))
                    locks.enter_context(agent_state._locked())
                    fresh = self._snapshot(member, document, snap)
                    if fresh["revision"] != snap["revision"]:
                        raise CapabilityError("stale_revision")
                    intent = fresh["intent"]
                    if not intent:
                        raise CapabilityError("not_inherited")
                    if intent.get("status") != "saved":
                        raise CapabilityError("materialization_pending")
                    lineage = agent_state.get_fork_info(fresh["target"], strict=True)
                    if not lineage or lineage["private_to"] != member:
                        raise CapabilityError("foreign_private_copy")
                    if _digest(fresh["spec"]) != intent.get("materialized"):
                        raise CapabilityError("materialization_changed")
                    # Refusals decided by the saved setup alone (the pane also gets them
                    # in the view, so it can say so before the user opens the dialog).
                    owned = _detach_owned(fresh)
                    blocker = _detach_blocker(fresh, intent, owned)
                    if blocker:
                        raise CapabilityError(blocker)
                    # Turning the global mcp.json merge off changes which servers the
                    # member loads, so the owner confirms it first.
                    loses_mcp_json = _detach_loses_mcp_json(fresh)
                    if loses_mcp_json and not accept_mcp_json:
                        raise CapabilityError("detach_mcp_json_unkept")
                    # One write: the spec file is never touched, so a failure
                    # leaves the intent and the reviewed bytes exactly as they
                    # were. A governance answer newer than the save is applied by
                    # the fork refresh below, which governs every private copy
                    # that has no saved intent.
                    target_name[0] = fresh["target"]
                    mcp_json_pinned[0] = loses_mcp_json
                    stale_governance[0] = intent.get("governance_generation") != (
                        governance_answer_generation()
                    )
                    state = agent_state._read(strict=True)
                    entry = state[fresh["target"]]
                    entry.pop("capabilities")
                    # A copy of a template Crew owns can still track the shipped
                    # model (``model_managed``); the fork refresh would then
                    # replace a model the member chose. The member's own copy
                    # keeps the model it runs, so stop tracking it, in this write.
                    if (
                        owned
                        and entry.get("model_managed") is True
                        and any(
                            override.get("action") == "set"
                            for override in intent["overrides"].get("model", {}).values()
                        )
                    ):
                        entry["model_managed"] = False
                    agent_state._write(state)
                    return None

                self._write_bindings({member: snap}, mutate)
            from kiro_crew.agent_discovery import clear_list_agents_cache

            clear_list_agents_cache()
            if (
                stale_governance[0]
                or mcp_json_pinned[0]
                or _fork_refresh_failed_for(target_name[0])
            ):
                # The kept copy was last governed under an older answer, or an
                # earlier refresh failed for it while it still had an intent (a
                # missing Parent makes that refresh fail). Re-run the refresh that
                # filters every intent-free private copy; if it fails the copy's
                # sessions stay blocked (fail closed), as for any fork.
                try:
                    from kiro_crew.agent import _refresh_forked_templates

                    _refresh_forked_templates()
                except Exception:
                    logger.warning("fork refresh after stop following failed", exc_info=True)
        return self.get(member)

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
                occupied = {
                    p.stem.lower() for directory in roots for p in iter_agent_spec_files(directory)
                }
                from kiro_crew.agent_discovery import _read_agent_spec

                for directory in roots:
                    for path in iter_agent_spec_files(directory):
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
        if mate_capabilities.binding_is_mate(member):
            return mate_capabilities.put(member, body, self._mate_catalog(member))
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
                # Compared as BYTES, never as ``str``. ``hmac.compare_digest``
                # rejects a str holding a non-ASCII character by raising
                # ``TypeError``, and the token arrives in the request body, so a
                # malformed one would escape the handler's ``CapabilityError``
                # and ``(OSError, ValueError)`` arms as a 500 instead of the
                # ``stale_preview`` refusal a wrong ASCII token produces.
                # ``surrogatepass`` because a lone surrogate must still compare
                # rather than raise on the way in, and it keeps two distinct
                # strings distinct. The expected value is a hex digest by
                # construction, so a token that matched before still matches.
                if not hmac.compare_digest(
                    request["preview_token"].encode("utf-8", "surrogatepass"),
                    self._preview_token(plans, request).encode("utf-8", "surrogatepass"),
                ):
                    raise CapabilityError("stale_preview")
                state = agent_state._read(strict=True)
                changed_binding = False
                for snap, spec, intent, _ in plans:
                    if snap["intent"] and spec == snap["spec"] and intent == snap["intent"]:
                        if _stamp_reviewed(snap, spec, intent):
                            intent["revision"] = secrets.token_hex(16)
                        else:
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
                    _stamp_reviewed(snap, spec, intent)
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


def _parent_identity(descriptor: dict) -> dict:
    """The Parent file a descriptor names, without the project it was resolved from.

    ``_source`` records the project it searched in, but a global Parent is the
    same file from every project that does not shadow its name, so that field
    alone never makes two resolutions different Parents. Name, scope, source and
    resolved path still must all match.
    """
    return {key: value for key, value in descriptor.items() if key != "project"}


def prepare_member_capabilities(member: str, project_dir: str | Path | None = None) -> dict:
    """Read-only startup seam; success proves saved bytes, never runtime loading.

    *project_dir* is the chat's own project. It may differ from the project the
    capabilities were saved in: the start is refused only when that project
    resolves a different Parent than the reviewed one (``project_identity_changed``).
    A project-scoped Parent belongs to its own project; a global Parent stays
    the reviewed Parent until a project template shadows its name.
    """
    cfg = KiroCrewConfig.load()
    binding = cfg.agents.get(member)
    if binding is None:
        raise CapabilityError("agent_not_found", 404)
    intent = agent_state.get_capabilities(binding.kiro_agent)
    if intent is None:
        return {"template": binding.kiro_agent, "revision": "", "status": "unverified"}
    project = str(Path(project_dir).resolve()) if project_dir else ""
    saved_parent = intent["parent"]
    other_project = project != saved_parent["project"]
    if other_project and saved_parent["scope"] != "global":
        moved = CapabilityError("project_identity_changed")
        moved.project = saved_parent["project"]
        raise moved
    if intent.get("status") != "saved":
        raise CapabilityError("materialization_pending")
    if intent.get("governance_generation") != governance_answer_generation():
        raise CapabilityError("governance_reconciliation_pending")
    lineage = agent_state.get_fork_info(binding.kiro_agent, strict=True)
    if not lineage or lineage["private_to"] != member:
        raise CapabilityError("foreign_private_copy")
    _, _, descriptor = _source(saved_parent["name"], project)
    if _parent_identity(descriptor) != _parent_identity(saved_parent):
        changed = CapabilityError(
            "project_identity_changed" if other_project else "parent_identity_changed"
        )
        changed.project = saved_parent["project"] if other_project else ""
        raise changed
    own_path, spec, own = _source(binding.kiro_agent, project, allow_private=True)
    if own["scope"] != "global":
        shadowed = CapabilityError("private_template_shadowed")
        shadowed.file = own_path.name
        shadowed.project = project
        raise shadowed
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
        # Launched servers whose entry turns single tools off. Not every member
        # backend can carry that (``ACP_BACKENDS_MEMBER_TOOL_LIMITS``).
        "tool_limited_servers": tool_limited_servers(spec),
    }


def tool_limited_servers(spec: dict) -> tuple[str, ...]:
    """Launched MCP servers in *spec* whose entry turns single tools off.

    The one definition both the chat-start seam (via
    :func:`prepare_member_capabilities`) and the Capabilities page warning read,
    so the page cannot name a different set than the start refuses on.
    """
    servers = spec.get("mcpServers", {})
    if not isinstance(servers, dict):
        return ()
    return tuple(
        sorted(
            name
            for name, server in servers.items()
            if isinstance(server, dict)
            and server.get("disabledTools")
            and not mcp_entry_is_muted(server)
        )
    )


def member_backend_gap(backend: str, limited_servers: tuple[str, ...]) -> str | None:
    """Why a member chat on *backend* would refuse a saved spec, or ``None``.

    Mirrors the start gate in ``session_allocation``: a backend outside
    ``ACP_BACKENDS_MEMBER_CAPABILITIES`` refuses with
    ``capability_harness_unsupported``; one outside
    ``ACP_BACKENDS_MEMBER_TOOL_LIMITS`` refuses a spec with single-tool limits
    with ``capability_tool_limits_unsupported``. Pure: no config read.
    """
    from kiro_crew.acp_backends import (
        ACP_BACKENDS_MEMBER_CAPABILITIES,
        ACP_BACKENDS_MEMBER_TOOL_LIMITS,
    )

    if backend not in ACP_BACKENDS_MEMBER_CAPABILITIES:
        return "harness_unsupported"
    if limited_servers and backend not in ACP_BACKENDS_MEMBER_TOOL_LIMITS:
        return "tool_limits_unsupported"
    return None


def member_backend_warning(spec: dict) -> dict | None:
    """Non-blocking warning for the Capabilities page, or ``None``.

    Checks both backends a member's chats can start on, resolved as
    ``members.select_provider_backend`` resolves them: the member DM arm
    (``agent.member_acp_backend``) and the default arm (``agent.acp_backend``),
    which carries the member's Slack threads, cron jobs and spawned sessions --
    those also reach the chat-start gate through ``resolve_crew_identity``. The
    first backend that would refuse is named, member DMs first. A spawn with an
    explicit backend override is chosen per call and cannot be known here. A
    warning only: the settings can change after the save, so nothing refuses.
    """
    from kiro_crew.acp_backends import resolve_selected_backend

    cfg = KiroCrewConfig.load()
    limited = tool_limited_servers(spec)
    for setting, value in (
        ("agent.member_acp_backend", cfg.agent.member_acp_backend),
        ("agent.acp_backend", cfg.agent.acp_backend),
    ):
        backend = resolve_selected_backend(value)
        code = member_backend_gap(backend, limited)
        if code is None:
            continue
        warning: dict[str, Any] = {"code": code, "backend": backend, "setting": setting}
        # Also on a harness refusal: switching to a member backend that loads
        # saved specs (kas) would then refuse these, so the page names both fixes.
        if limited:
            warning["servers"] = list(limited)
        return warning
    return None


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
