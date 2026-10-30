"""Per-template MCP server additions, kept OUTSIDE every agent spec file.

An agent template's ``mcpServers`` map is rewritten by its installer: the conductor
installers rebuild theirs to exactly the Crew servers on every pass, and a
third-party custom agent may be rewritten by its own installer. A server the user approved for a template therefore cannot live in
the spec file -- the next rebuild would wipe it, or a hand edit and an approved
grant would look the same. This module is the record that survives: one JSON file
under the data home, keyed by template name, each entry a GATEWAY-RESOLVED server
entry plus who approved it, when, and on which change card.

What the record never carries is trust. An entry holds how to launch or reach a
server and nothing about which of its tools run without asking: ``autoApprove``,
``allowedTools`` and every other key outside :data:`ENTRY_KEYS` are refused on
write and dropped again on merge, so a hand-edited file cannot smuggle a grant in
either. The merged server's tools prompt like any other until the user grants them
through the existing paths, which still pass ``_filter_auto_approve``.

:func:`merge_into` is the one function a covered template's ``mcpServers`` passes
through on its way to a session (the conductor installers, and the spawn of a
template Kiro Crew does not write). It only ADDS: a server name the template already
carries is left exactly as the template has it.

The default agent and the worker are not covered, by design. ``kirocrew.json``
already holds every server installed through the MCP page, and the worker mirrors
it, so neither needs this record; both are refused (:data:`DEFAULT_REASON`). Every
other template Kiro Crew writes takes no additions either (:data:`BUILT_IN_REASON`,
:data:`CREWMATE_REASON`).

The file is owner-only, every write is atomic, and every read-modify-write holds
an in-process lock plus a cross-process advisory lock on a sidecar file.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import json
import logging
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, overload

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.owner_only_files import mkdirs_owner_only

logger = logging.getLogger(__name__)

#: The record's DIRECTORY under the data home: a sealed leaf (read-only to every
#: sandboxed process and denied to the file-tool gate), because a sandboxed writer
#: could otherwise record a server it chose and have the next rebuild launch it.
#: Every writer runs in the gateway or the CLI, outside the sandbox.
RECORD_DIR = "template-mcp-additions"
#: The record file inside :data:`RECORD_DIR`.
RECORD_FILENAME = "additions.json"
RECORD_VERSION = 1
RECORD_MAX_BYTES = 1024 * 1024

#: The only keys an addition's server entry may carry: how to launch or reach the
#: server. Nothing here grants a tool. ``autoApprove`` / ``allowedTools`` /
#: ``disabledTools`` and anything else are refused on write and dropped on merge;
#: the merge then layers the MCP page's own ``disabledTools`` switch back on.
ENTRY_KEYS: frozenset[str] = frozenset(
    {"command", "args", "env", "url", "headers", "type", "timeout"}
)

#: Keys whose presence on an entry is a trust grant. Named separately only so the
#: refusal can say why; :data:`ENTRY_KEYS` is the rule.
TRUST_KEYS: frozenset[str] = frozenset(
    {"autoApprove", "alwaysAllow", "allowedTools", "tools", "toolsSettings", "trust"}
)

_TEMPLATE_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,62}[A-Za-z0-9]|[A-Za-z0-9])\Z")
_SERVER_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,62}[A-Za-z0-9]|[A-Za-z0-9])\Z")
_TEXT_MAX = 256
_ARG_MAX = 4096
_ARGS_MAX = 128
_MAP_MAX = 64

_lock = threading.RLock()

#: The templates the record refuses because their own servers already come from the
#: MCP page: the default agent's spec holds every server installed there, and the
#: worker mirrors it.
DEFAULT_TEMPLATES: frozenset[str] = frozenset({"kirocrew", "kirocrew-worker"})
#: Templates whose installer rebuilds their spec and merges the record into it.
#: Every template Kiro Crew does not write is merged at spawn instead, never into its
#: spec file (:func:`merge_at_spawn`).
INSTALLER_MERGED_TEMPLATES: frozenset[str] = frozenset(
    {
        "kirocrew-conductor",
        "kirocrew-ledger-conductor",
        "kirocrew-pipeline-conductor",
        "kirocrew-security-conductor",
    }
)
#: A deprecated alias emits its successor's spec, so it reads its successor's record.
_TEMPLATE_ALIASES: dict[str, str] = {"kirocrew-ledger-conductor": "kirocrew-conductor"}


class AdditionError(ValueError):
    """A refused addition: the message is a short machine-readable reason."""


@dataclass(frozen=True)
class McpAddition:
    """One approved server on one template."""

    template: str
    server: str
    entry: Mapping[str, Any] = field(repr=False)
    approved_by: str
    approved_at: str
    card_id: str

    def to_record(self) -> dict[str, Any]:
        return {
            "entry": copy.deepcopy(dict(self.entry)),
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "card_id": self.card_id,
        }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def read_additions(template: str) -> dict[str, McpAddition]:
    """The additions recorded for *template*, server name -> addition.

    Lenient: an absent, unreadable or malformed record reads as no additions, and
    an individual entry that fails validation is skipped (and logged), so a damaged
    file degrades to the template as shipped rather than breaking a spawn.
    """
    if not isinstance(template, str) or not _TEMPLATE_RE.fullmatch(template):
        return {}
    template = _TEMPLATE_ALIASES.get(template, template)
    templates = _read().get("templates", {})
    if not isinstance(templates, dict):
        return {}
    return _parse_template(template, templates.get(template))


def list_additions() -> dict[str, dict[str, McpAddition]]:
    """Every template's additions, template -> server -> addition (lenient)."""
    out: dict[str, dict[str, McpAddition]] = {}
    templates = _read().get("templates", {})
    if not isinstance(templates, dict):
        return out
    for template in sorted(templates):
        if not isinstance(template, str) or not _TEMPLATE_RE.fullmatch(template):
            continue
        parsed = _parse_template(template, templates[template])
        if parsed:
            out[template] = parsed
    return out


def add_addition(
    template: str,
    server: str,
    entry: Mapping[str, Any],
    *,
    approved_by: str,
    card_id: str,
    approved_at: str | None = None,
    only_if_absent: bool = False,
) -> McpAddition | None:
    """Record *server* on *template* and return the stored addition.

    *entry* must be the gateway-resolved server entry (never an agent-supplied
    launch line) and carry only :data:`ENTRY_KEYS`; a trust key is refused with
    ``AdditionError("trust_key:<key>")``. Recording a server the template already
    has an addition for replaces that addition (the newer approval wins), unless
    *only_if_absent*: then an existing addition is left as it is and ``None`` is
    returned, decided under the same lock as the write (a rollback restoring an
    older record never overwrites a newer approval).
    Raises :class:`AdditionError` on a refused input and ``OSError`` /
    ``ValueError`` when an existing record cannot be read (it is never overwritten
    blind).
    """
    _check_template(template)
    _check_server(server)
    template = _TEMPLATE_ALIASES.get(template, template)
    _check_covered(template)
    clean = validate_entry(entry)
    who = _check_text(approved_by, "approved_by")
    card = _check_text(card_id, "card_id")
    when = approved_at if approved_at is not None else _now()
    _check_text(when, "approved_at")
    addition = McpAddition(
        template=template,
        server=server,
        entry=clean,
        approved_by=who,
        approved_at=when,
        card_id=card,
    )
    with _locked():
        data = _read(strict=True)
        templates = data.setdefault("templates", {})
        if not isinstance(templates, dict):
            raise ValueError("template_mcp_additions_invalid")
        bucket = templates.setdefault(template, {})
        if not isinstance(bucket, dict):
            raise ValueError("template_mcp_additions_invalid")
        if only_if_absent and server in bucket:
            return None
        bucket[server] = addition.to_record()
        _write(data)
    return addition


def remove_addition(template: str, server: str, *, card_id: str | None = None) -> bool:
    """Remove *server* from *template*'s additions; whether anything was removed.

    With *card_id*, remove only when the stored addition came from that card, so a
    stale Undo cannot take away a later approval of the same server.
    """
    _check_template(template)
    _check_server(server)
    template = _TEMPLATE_ALIASES.get(template, template)
    with _locked():
        data = _read(strict=True)
        templates = data.get("templates")
        if not isinstance(templates, dict):
            return False
        bucket = templates.get(template)
        if not isinstance(bucket, dict) or server not in bucket:
            return False
        stored = bucket[server]
        if card_id is not None and (
            not isinstance(stored, dict) or stored.get("card_id") != card_id
        ):
            return False
        del bucket[server]
        if not bucket:
            del templates[template]
        _write(data)
    return True


def merge_into(template: str, servers: Mapping[str, Any] | None) -> dict[str, Any]:
    """*servers* plus *template*'s recorded additions, as a new map.

    ADD-only: a server name *servers* already declares keeps the template's own entry.
    The one exception is an entry that declares no transport, only a restriction (a
    checkout's or the dashboard's switch-off keys, :data:`RESTRICTION_KEYS`): the
    approved entry is its launch, and the restriction is layered on top, so it still
    narrows the server instead of withholding it whole. Each added entry is rebuilt
    from :data:`ENTRY_KEYS` alone, so no trust key reaches a spec even from a
    hand-edited record, and then takes the MCP page's current tool switch-offs for
    that server (:func:`_page_switch_offs`), so an addition never brings back a tool
    the owner turned off. *servers* is not mutated.
    """
    merged: dict[str, Any] = dict(servers or {})
    recorded = read_additions(template)
    switched_off = _page_switch_offs(recorded) if recorded else {}
    for name, addition in recorded.items():
        existing = merged.get(name)
        if isinstance(existing, dict) and _restriction_only(existing):
            kept = {k: copy.deepcopy(existing[k]) for k in RESTRICTION_KEYS if k in existing}
            merged[name] = _with_switch_offs(
                {**copy.deepcopy(dict(addition.entry)), **kept}, switched_off.get(name)
            )
            continue
        if name in merged:
            continue
        merged[name] = _with_switch_offs(
            copy.deepcopy(dict(addition.entry)), switched_off.get(name)
        )
    return merged


def _page_switch_offs(names: Any) -> dict[str, list[str]]:
    """The tools the owner switched off on the MCP page, per server in *names*. Blocking.

    An approved addition is the gateway's launch line for a server, and the page's
    ``disabledTools`` switch is a guard on that server wherever it runs, so every
    merge layers the page's current switch-offs onto it. The page is read by the
    one reader the crewmate path already uses (``mate_worker.globally_disabled_refs``,
    ``@server/tool`` refs), grouped here per server. Read live, so a tool switched
    off later is off here too.
    """
    from kiro_crew.mate_worker import globally_disabled_refs

    out: dict[str, list[str]] = {}
    for ref in globally_disabled_refs(names):
        server, _, tool = ref[1:].partition("/")
        if server and tool:
            out.setdefault(server, []).append(tool)
    return out


def _with_switch_offs(entry: dict[str, Any], tools: list[str] | None) -> dict[str, Any]:
    """*entry* with *tools* added to its ``disabledTools`` (a union; never fewer)."""
    if not tools:
        return entry
    have = entry.get("disabledTools")
    have = [t for t in have if isinstance(t, str)] if isinstance(have, list) else []
    entry["disabledTools"] = list(dict.fromkeys([*have, *tools]))
    return entry


#: The keys a restriction-only entry may carry onto an approved one. Both only narrow:
#: a muted server, or switched-off tools. Nothing that launches or grants.
RESTRICTION_KEYS: tuple[str, ...] = ("disabled", "disabledTools")


def _restriction_only(entry: dict[str, Any]) -> bool:
    """Whether *entry* declares no transport, only (at most) a restriction.

    Crew's own ``x-`` bookkeeping keys are ignored; any other key means the entry is
    someone's declaration and stands.
    """
    return (
        not entry.get("command")
        and not entry.get("url")
        and {k for k in entry if not str(k).startswith("x-")} <= set(RESTRICTION_KEYS)
    )


def merge_into_spec(template: str, spec: dict[str, Any]) -> list[str]:
    """Merge *template*'s additions into *spec* IN PLACE; the names it added.

    :func:`merge_into` on ``spec["mcpServers"]``, plus the ``@server`` reference in
    ``tools`` that makes the server's tools reachable at all (kiro-cli mounts a
    declared server's tools only when ``tools`` names it). That reference is not a
    grant: ``allowedTools`` and ``autoApprove`` are never touched, so every call
    still reaches the PreToolUse gate. A ``tools`` that is absent or not a list is
    left alone -- an absent one is the harness default and a malformed one is the
    spec owner's to fix, not this merge's to widen.
    """
    servers = spec.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        return []
    merged = merge_into(template, servers)
    before = servers or {}
    added = [name for name in merged if merged[name] is not before.get(name)]
    if not added:
        return []
    spec["mcpServers"] = merged
    tools = spec.get("tools")
    if isinstance(tools, list):
        for name in added:
            ref = f"@{name}"
            if ref not in tools:
                tools.append(ref)
    return added


@dataclass(frozen=True)
class AdditionSupport:
    """Whether an addition to *template* reaches its sessions on *backend*.

    ``reason`` is empty when ``supported``; otherwise it is the short sentence a
    card refusal shows. ``supported`` says nothing about a RUNNING session: whether
    the change reaches one without a reset is the existing live gate
    (``mcp_hot_reload.live_sessions_apply_mcp_live``), asked by the caller over the
    live providers.
    """

    supported: bool
    reason: str = ""


#: The refusal for a template Crew does not own on a backend that loads it off disk.
UNSUPPORTED_REASON = (
    "This agent's definition is not managed by Kiro Crew, and on this harness the "
    "agent is loaded from its own file. Kiro Crew never writes into a definition it "
    "does not own, so a server cannot be added to it here."
)


#: The refusal for the default agent and the worker.
DEFAULT_REASON = (
    "This agent's servers are managed on the MCP page: a server installed there is "
    "already part of this agent, so add it from the MCP page instead."
)

#: The refusal for a crewmate's own service template, whose servers come from the
#: crewmate's capability path rather than from this record.
CREWMATE_REASON = (
    "This agent belongs to a crewmate. A crewmate's servers are changed through its own "
    "capabilities, not added here."
)


#: The refusal for a built-in template whose installer takes no additions (a fixed
#: server set such as the heartbeat's, or a background agent nobody chats with).
BUILT_IN_REASON = (
    "This is a built-in Kiro Crew agent with a fixed set of servers, so a server cannot "
    "be added to it."
)


def _crew_owned(template: str) -> bool:
    """Whether Kiro Crew writes *template*'s spec (``agent_files.OWNED_KIRO_AGENT_FILES``)."""
    from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

    return f"{template}.json" in OWNED_KIRO_AGENT_FILES


def _spawn_merged(template: str) -> bool:
    """Whether *template* takes the spawn merge: a template Kiro Crew does not write.

    Every template Crew writes is either merged by its installer
    (:data:`INSTALLER_MERGED_TEMPLATES`) or takes no additions at all, so none of them
    is merged here; a crewmate's own service templates are among the latter.
    """
    from kiro_crew.agent_files import is_mate_service_agent

    return not _crew_owned(template) and not is_mate_service_agent(template)


#: The refusal for a backend that runs no server an agent spec declares.
NO_SPEC_SERVERS_REASON = (
    "This harness does not run the servers an agent's definition declares, so a server "
    "added to this agent would not reach its sessions here."
)


def _template_support(template: str) -> AdditionSupport | None:
    """The refusal for a template this record does not cover, or ``None`` if it does."""
    from kiro_crew.agent_files import is_mate_service_agent

    if template in DEFAULT_TEMPLATES:
        return AdditionSupport(False, DEFAULT_REASON)
    if is_mate_service_agent(template):
        return AdditionSupport(False, CREWMATE_REASON)
    if _crew_owned(template) and template not in INSTALLER_MERGED_TEMPLATES:
        return AdditionSupport(False, BUILT_IN_REASON)
    return None


def addition_support(template: str, backend: str) -> AdditionSupport:
    """Whether an addition to *template* reaches its sessions on *backend*.

    Refused for a template the record does not cover: the default agent and the
    worker (:data:`DEFAULT_REASON`), a crewmate's own service templates
    (:data:`CREWMATE_REASON`) and every other template Kiro Crew writes whose installer
    takes no additions (:data:`BUILT_IN_REASON`). A conductor
    (:data:`INSTALLER_MERGED_TEMPLATES`) is merged by its installer into its spec, so
    it is supported on every backend that runs the servers a spec declares
    (``agent_sdk.drivers.acp.backend_delivers_spec_servers``) and refused with
    :data:`NO_SPEC_SERVERS_REASON` on one that does not (pi, whose projection is
    broker-only). Any other template is merged at spawn, which only the backends in
    ``ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN`` take (a positive membership test, per
    harness parity); on the rest -- kiro-cli first among them -- the answer is
    unsupported with :data:`UNSUPPORTED_REASON`.
    """
    if not isinstance(template, str) or not _TEMPLATE_RE.fullmatch(template):
        return AdditionSupport(False, "That agent name is not valid.")
    template = _TEMPLATE_ALIASES.get(template, template)
    refused = _template_support(template)
    if refused is not None:
        return refused
    if template in INSTALLER_MERGED_TEMPLATES:
        from kiro_crew.agent_sdk.drivers.acp import backend_delivers_spec_servers

        if backend_delivers_spec_servers(backend):
            return AdditionSupport(True)
        return AdditionSupport(False, NO_SPEC_SERVERS_REASON)
    from kiro_crew.acp_backends import ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN

    if backend in ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN:
        return AdditionSupport(True)
    return AdditionSupport(False, UNSUPPORTED_REASON)


@overload
def merge_at_spawn(template: str | None, spec: dict[str, Any]) -> dict[str, Any]: ...


@overload
def merge_at_spawn(template: str | None, spec: None) -> None: ...


def merge_at_spawn(template: str | None, spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """*spec* as a session of a template Crew does NOT own should read it at spawn.

    Returns a NEW dict with the additions merged (:func:`merge_into_spec`) for a
    template outside :data:`INSTALLER_MERGED_TEMPLATES`; returns *spec* itself for a
    Crew-owned template (its installer already merged them into the file), for
    ``None``, and when there is nothing to add. Never writes any file. Called only
    from the spawn reads of the backends in ``ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN``.
    """
    if not template or not isinstance(spec, dict) or not _spawn_merged(template):
        return spec
    if not read_additions(template):
        return spec
    out = copy.deepcopy(spec)
    merge_into_spec(template, out)
    return out


def addition_reach(template: str) -> tuple[str, ...]:
    """Every template one addition to *template* reaches, *template* first.

    Two tables decide it, and a test pins both to their owners so this cannot drift:
    a deprecated alias emits its successor's spec, so an addition to either reaches
    both (:data:`_TEMPLATE_ALIASES`, ``conductor_agents.DEPRECATED_AGENT_SPECS``); and
    a template the record does not cover (:func:`addition_support`'s refusals) is
    reached by nothing, so it answers ``()``. Nothing else copies a covered template's
    servers.
    """
    if not isinstance(template, str) or not _TEMPLATE_RE.fullmatch(template):
        return ()
    canonical = _TEMPLATE_ALIASES.get(template, template)
    reach = [template]
    for name in (
        canonical,
        *sorted(a for a, c in _TEMPLATE_ALIASES.items() if c == canonical),
    ):
        if name not in reach:
            reach.append(name)
    if _template_support(canonical) is not None:
        return ()
    return tuple(reach)


def validate_entry(entry: object) -> dict[str, Any]:
    """Return a clean copy of a server entry, or raise :class:`AdditionError`."""
    if not isinstance(entry, Mapping):
        raise AdditionError("entry_not_object")
    for key in entry:
        if key in TRUST_KEYS:
            raise AdditionError(f"trust_key:{key}")
        if key not in ENTRY_KEYS:
            raise AdditionError(f"unknown_key:{key}")
    clean: dict[str, Any] = {}
    command = entry.get("command")
    url = entry.get("url")
    if (command is None) == (url is None):
        raise AdditionError("entry_needs_command_or_url")
    if command is not None:
        clean["command"] = _check_text(command, "command", limit=_ARG_MAX)
        args = entry.get("args", [])
        if not isinstance(args, list) or len(args) > _ARGS_MAX:
            raise AdditionError("args_invalid")
        clean["args"] = [_check_text(a, "args", limit=_ARG_MAX, allow_empty=True) for a in args]
    else:
        if "args" in entry:
            raise AdditionError("args_without_command")
        clean["url"] = _check_text(url, "url", limit=_ARG_MAX)
    for key in ("env", "headers"):
        if key in entry:
            clean[key] = _check_str_map(entry[key], key)
    if "type" in entry:
        clean["type"] = _check_text(entry["type"], "type")
    if "timeout" in entry:
        timeout = entry["timeout"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not 0 < timeout <= 3_600_000
        ):
            raise AdditionError("timeout_invalid")
        clean["timeout"] = timeout
    return clean


def record_path() -> Path:
    """Where the record lives (resolved per call so ``KIROCREW_HOME`` is honoured)."""
    return config_dir() / RECORD_DIR / RECORD_FILENAME


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _parse_template(template: str, bucket: Any) -> dict[str, McpAddition]:
    out: dict[str, McpAddition] = {}
    if not isinstance(bucket, dict):
        return out
    for server in sorted(bucket):
        stored = bucket[server]
        try:
            _check_server(server)
            if not isinstance(stored, dict):
                raise AdditionError("addition_not_object")
            out[server] = McpAddition(
                template=template,
                server=server,
                entry=validate_entry(stored.get("entry")),
                approved_by=_check_text(stored.get("approved_by"), "approved_by"),
                approved_at=_check_text(stored.get("approved_at"), "approved_at"),
                card_id=_check_text(stored.get("card_id"), "card_id"),
            )
        except AdditionError as exc:
            logger.warning(
                "Skipping unusable MCP addition %r on template %r: %s",
                server,
                template,
                exc,
            )
    return out


def _check_template(template: object) -> None:
    if not isinstance(template, str) or not _TEMPLATE_RE.fullmatch(template):
        raise AdditionError("template_invalid")


def _check_covered(template: str) -> None:
    """Refuse a template this record does not cover (``AdditionError``)."""
    support = _template_support(template)
    if support is not None:
        raise AdditionError(f"template_not_covered:{template}")


def _check_server(server: object) -> None:
    if not isinstance(server, str) or not _SERVER_RE.fullmatch(server):
        raise AdditionError("server_invalid")


def _check_text(
    value: object, what: str, *, limit: int = _TEXT_MAX, allow_empty: bool = False
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > limit:
        raise AdditionError(f"{what}_invalid")
    if "\x00" in value:
        raise AdditionError(f"{what}_invalid")
    return value


def _check_str_map(value: object, what: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > _MAP_MAX:
        raise AdditionError(f"{what}_invalid")
    out: dict[str, str] = {}
    for key, item in value.items():
        out[_check_text(key, what)] = _check_text(item, what, limit=_ARG_MAX, allow_empty=True)
    return out


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """In-process lock plus a cross-process advisory lock on a sidecar file."""
    with _lock:
        from kiro_crew.platform_compat import file_lock, open_create_or_existing

        lock_path = record_path().with_suffix(".json.lock")
        # ``0700`` under any umask: the record directory is owner-only, and this is
        # usually the first thing to create it.
        mkdirs_owner_only(lock_path.parent)
        fd = open_create_or_existing(
            lock_path,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            0o600,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("template_mcp_additions_lock_invalid")
            with file_lock(fd, exclusive=True, wait=True):
                yield
        finally:
            os.close(fd)


def _read(*, strict: bool = False) -> dict[str, Any]:
    """Load the record. ``strict`` (mutators) raises on a present-but-unreadable file."""
    path = record_path()
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("template_mcp_additions_invalid")
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > RECORD_MAX_BYTES:
                raise ValueError("template_mcp_additions_invalid")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(RECORD_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > RECORD_MAX_BYTES:
            raise ValueError("template_mcp_additions_too_large")
        data = json.loads(raw)
    except FileNotFoundError:
        return {"version": RECORD_VERSION, "templates": {}}
    except (OSError, ValueError):
        if strict:
            raise
        logger.warning("Template MCP additions record unreadable: %s", path, exc_info=True)
        return {"version": RECORD_VERSION, "templates": {}}
    if not isinstance(data, dict) or data.get("version") != RECORD_VERSION:
        if strict:
            raise ValueError("template_mcp_additions_invalid")
        return {"version": RECORD_VERSION, "templates": {}}
    return data


def _write(data: dict[str, Any]) -> None:
    data["version"] = RECORD_VERSION
    payload = json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) > RECORD_MAX_BYTES:
        raise ValueError("template_mcp_additions_too_large")
    atomic_write(record_path(), payload, restrict_to_owner=True)
