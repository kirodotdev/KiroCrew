"""Has the config a live chat session was spawned with gone stale?

A chat's agent process reads some of its inputs exactly once, when it is
spawned: the MCP servers it mounts, the agent spec that names them (and the env
each server is handed), and the ACP backend itself. An edit to any of them after
the spawn never reaches that process until a person presses the session's
Reload action, and nothing told them it was owed. This module answers whether a
live chat runs on such outdated config, so the dashboard can show it (a "stale
config" badge) and an agent can ask; it never relaunches anything.

A fingerprint of those inputs is recorded against the provider a turn ran on
and recomputed at each turn's end, right after the gateway's own config writes,
on a stat-guarded periodic sweep and on request. No config writer has to know
which sessions it affects: a refresh re-checks every live chat.

The fingerprint is built from the FIELDS a backend reads at spawn, not from
whole-file digests, because a backend's own hot-reload covers only some fields
of a file, and some fields in the same file are read by no spawn at all. Three
halves:

* ``reconcilable`` -- the ``mcpServers`` entries (with their ``disabled`` and
  ``disabledTools``) of the agent spec kiro-cli loads and of the ``mcp.json``
  files the spec mounts, and the ``@server`` refs ADDED to the spec's ``tools`` (the hot-reload
  contract in :mod:`kiro_crew.mcp_hot_reload` names an added ``tools`` ref and
  nothing in ``allowedTools``). Whether the ``mcp.json`` servers are mounted is
  the spec's ``includeMcpJson`` as the backend reads it (:func:`_mcp_json_mounted`);
  for a spec that opts out only the parts of those files Crew reads at spawn
  anyway are compared (:func:`_restriction_part`), so adding a server there
  does not flag a chat that never mounts it. Toggling the flag itself is a
  spec field like any other, so it is spawn-only. A ref REMOVED from ``tools`` is spawn-only:
  kiro-cli keeps a removed ref's server
  mounted while it runs, and only the dashboard's own MCP sync compensates by
  also writing ``disabled: true``. The refs are kept as a bounded set of
  digests (:attr:`ConfigFingerprint.tool_refs`) so the two directions can be told
  apart. kiro-cli from :data:`MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION` applies these
  live (:mod:`kiro_crew.mcp_hot_reload`), so they are stale only on a provider
  that does NOT reconcile them itself.
* ``spawn_only`` -- every other field of the loaded agent spec (``prompt``, ``hooks``, ``resources``, all of
  ``allowedTools``, the non-``@server`` entries of ``tools``, and the rest) and
  the ACP backend. ``allowedTools`` is here whole because it is the auto-approve
  list: a grant revoked by any writer must not leave a live process
  auto-approving it, and no hot-reload contract covers the field. A field the
  session's backend never receives is left out (:func:`_unconsumed_spec_fields`,
  read from that backend's agent-config mirror): ``hooks`` on claude and codex,
  whose sessions have no channel for it, so editing it there is not stale.

A Kiro Crew server's declaration in either ``mcp.json`` falls in neither half:
Crew itself reads it at each session start (:func:`_identity_part`), so a change
to it is stale on every provider, a hot-reloading one included.

An input that EXISTS but cannot be read -- unreadable (a directory planted at
the path, a permission error) or refused by the credential gate -- has no fields
to assign to a half. It is excluded from the comparison PER INPUT
(:func:`carry_forward` puts the recorded value back), so every other
input is still compared and acted on; the caller reports that input's staleness
as unknown. Treating it as a change would flag a chat stale with no config
change behind it. An ABSENT file is a real change (deleted config) and is
fingerprinted like any other state.

Deliberately absent: the spec's ``model`` field (a model is applied through its
own path, and a default-model rewrite of the spec would otherwise flag every
running chat), the MCP gateway's stub-server set
(it takes effect only at the next gateway start, so a relaunch before then gets
the same topology), reasoning effort, and the process environment (a dashboard
chat's kiro-cli inherits the gateway's, which does not change while it runs; an
MCP server's own env lives in its ``mcpServers`` entry, which is hashed).
"""

from __future__ import annotations

import codecs
import errno
import functools
import hashlib
import heapq
import json
import logging
import os
import stat
import weakref
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.agent_sdk.agent_specs import kas_project_spec_view, mirrored_session_spec
from kiro_crew.agent_sdk.backend_mcp_ability import ability_for, spec_keys
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.agent_spec_format import (
    is_agent_spec_name,
    parse_agent_spec_bytes,
    without_volatile_mcp_env,
)
from kiro_crew.config.paths import mate_agents_dir, project_agents_dir
from kiro_crew.hooks import validate_file_path
from kiro_crew.mcp_cleanup import CONTROL_PLANE_SERVERS, KIROCREW_BIN_MCP_SERVERS
from kiro_crew.user_json import loads_user_jsonc

logger = logging.getLogger(__name__)

#: On Windows the badge scans no config folder and reports unknown. Resolving an
#: agent-writable config path by name there can be redirected to a network share,
#: which makes Windows send the user's login credentials to it (outbound SMB
#: authentication), and the sweep would retry that on every tick. A module
#: attribute so a test can switch it on.
WINDOWS_SCAN_DISABLED = os.name == "nt"


#: Agent-spec fields that are not spawn-only: ``mcpServers`` is the reconcilable
#: half, and ``model`` is applied through its own path and never stale. The
#: per-field table for spec authors is ``src/kiro_crew/docs/agent-spec-fields.md``
#: ("What a running chat does when a field changes"); keep the two in step.
_NON_SPAWN_ONLY_SPEC_FIELDS = frozenset({"mcpServers", "model"})

#: Spec lists whose ``@server`` entries are MCP refs (reconcilable) and whose
#: other entries are built-in tool names (spawn-only). ``allowedTools`` is not
#: one: the auto-approve list is spawn-only in full.
_TOOL_REF_FIELDS = ("tools",)


@dataclass(frozen=True)
class SpawnInputs:
    """What a turn handed the provider factory that the fingerprint depends on.

    Recorded beside the fingerprint so a later check
    (:func:`kiro_crew.dashboard.config_staleness.config_stale_status`), which has
    no crew binding resolution of its own, recomputes from the SAME selection the
    spawn used rather than re-deriving it from slot fields a crew binding may
    have resolved differently.
    """

    agent: str
    project: str


@dataclass(frozen=True)
class ConfigFingerprint:
    """Digests of a session's spawn-time config, split by what a live process can apply.

    ``tool_refs`` is the set of digests of the ``@server`` refs in the loaded
    spec's ``tools`` (bounded, :func:`_tool_refs`): an added ref is
    reconcilable, a removed one is spawn-only.
    """

    reconcilable: str
    spawn_only: str
    tool_refs: frozenset[str] = frozenset()
    #: Digests of the ``@server`` refs whose server is declared and not
    #: ``disabled`` in the spec or either ``mcp.json`` (bounded like
    #: ``tool_refs``): on a hot-reloading provider a ref removed from ``tools``
    #: whose server is still running stays mounted, one whose server is now
    #: switched off or gone is stopped, and only the first is a Reload.
    live_server_refs: frozenset[str] = frozenset()
    #: For each digest in ``tool_refs``, the digest of ``@<server>`` the ref
    #: names: a whole-server ref is its own server, a ``@server/tool`` ref is
    #: not, and this is what lets a removed per-tool ref be looked up in
    #: ``live_server_refs``. Only the kept refs are mapped; an overflow entry is
    #: read as mounted.
    ref_servers: tuple[tuple[str, str], ...] = ()
    #: The per-input texts the two digests are built from, by name, so an
    #: input that could not be read can be carried forward (:func:`carry_forward`).
    parts: tuple[tuple[str, str], ...] = ()
    #: The inputs (``spec``, ``global_mcp``, ``workspace_mcp``) that exist but
    #: could not be read or were refused by the credential gate.
    unreadable: frozenset[str] = frozenset()


#: Markers for a file that exists but has no fields to read.
_NO_FIELDS = ("refused", "unreadable")

#: Which ``parts`` each readable input supplies.
#: ``spec`` carries its selection metadata too (which file, which scope), so an
#: input that cannot be read -- an agents directory that cannot be listed --
#: keeps the recorded selection rather than reading as a switch to no spec.
#: ``backend`` is the ACP backend the spawn ran on, so a pure backend switch is
#: named too rather than left with no input to attribute it to.
#: ``mcp_json`` (whether the loaded spec mounts the ``mcp.json`` servers,
#: :func:`_mcp_json_mounted`) belongs to the spec: it is read from it, so an
#: unreadable spec carries the recorded answer forward with the rest of it.
#: Each ``mcp.json`` supplies two views, its whole ``mcpServers`` and the
#: restriction view Crew reads at spawn whatever the flag says
#: (:func:`_restriction_part`); :func:`_effective_mcp` picks the one compared.
#: A third, its Kiro Crew server declarations (:func:`_identity_part`), is
#: compared on its own.
#: ``user_spec`` is the user-level spec's own path and spawn-only fields, an
#: input of its own so that an unreadable project agents directory (``spec``
#: carried forward) still compares the user-level spec a chat with no
#: workspace spec runs on. It is named as ``spec`` (:func:`changed_inputs`).
_INPUT_PARTS = {
    "spec": ("spec_mcp", "spec_fields", "spec_path", "spec_ws", "mcp_json"),
    "user_spec": ("user_spec_path", "user_spec_fields"),
    "global_mcp": ("global_mcp", "global_mcp_restrictions", "global_mcp_identity"),
    "workspace_mcp": ("workspace_mcp", "workspace_mcp_restrictions", "workspace_mcp_identity"),
    "backend": ("backend",),
}

#: The parts holding each ``mcp.json``'s Kiro Crew server declarations
#: (:func:`_identity_part`), compared on every provider (:func:`is_stale`).
_IDENTITY_PARTS = ("global_mcp_identity", "workspace_mcp_identity")

#: The ``mcp.json`` inputs, whose compared part depends on ``mcp_json``.
_MCP_JSON_INPUTS = ("global_mcp", "workspace_mcp")


def _read_json(path: Path) -> tuple[str, Any]:
    """Read the ``mcp.json`` at *path* through the credential gate and a pin of its directory.

    Returns ``(marker, document)``. ``marker`` is ``"ok"`` for a parsed JSON
    document, and otherwise one of ``refused`` / ``absent`` / ``unreadable`` /
    ``unparseable:<digest>``, each distinct so that deleting a file, making it
    unreadable, pointing it somewhere refused and breaking its syntax are all
    changes and none reads as "unchanged".

    The path is screened by :func:`kiro_crew.hooks.validate_file_path`, the gate
    every other reader of these settings files uses: a workspace's
    ``.kiro/settings/mcp.json`` is workspace-controlled, so a link planted there
    must not turn this per-turn probe into a read of a credential file. A path
    the gate refuses is screened before its existence is even probed. After the
    screen the admitted path is never resolved by name again: its directory is
    pinned component by component (:func:`_pin`) and the file is probed and read
    through that handle (:func:`_read_pinned_spec`), so a directory on the way --
    ``.kiro``, ``settings``, or any ancestor -- swapped for a link or a Windows
    junction to a UNC share after the screen is refused, not followed. The whole
    file is read: a change past any prefix must be seen. A file over the spec
    resolver's read budget (``agent.PINNED_SPEC_DIR_MAX_TOTAL_BYTES``), a link,
    a hardlink or a non-regular
    entry is ``unreadable``, never unchanged. Windows never reaches this: the
    entry points short-circuit there (:data:`WINDOWS_SCAN_DISABLED`).
    """
    admitted = _gated_dir(path)
    if admitted is None:
        return "refused", None
    try:
        pinned = _pin(admitted.parent)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None
    with pinned:
        # Parsed as the session's settings reader parses it (JSONC: comments
        # and trailing commas accepted, ``acp/session_mcp._global_settings``),
        # so a comment-only edit digests to the same document and reads as
        # unchanged, as it is for the running session.
        return _read_pinned_spec(pinned, path, name=admitted.name, settings=True)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stable_servers(servers: Any) -> Any:
    """*servers* with the per-launch env nonce values a launcher re-stamps blanked.

    The same normalization the worker-spec fingerprint applies
    (:func:`~kiro_crew.agent_spec_format.without_volatile_mcp_env`): a nonce
    rewritten at every session launch is not a config change, so it must not
    move a digest this module compares.
    """
    return without_volatile_mcp_env({"mcpServers": servers})["mcpServers"]


def _is_server_ref(entry: Any) -> bool:
    return isinstance(entry, str) and entry.startswith("@")


def _mcp_part(marker: str, document: Any) -> str:
    """The reconcilable text of one spec or ``mcp.json``.

    Its ``mcpServers``. A spec's ``@server`` refs in ``tools`` are carried as a
    set beside the digests (:func:`_tool_refs`), since an added and a removed ref
    fall in different halves.
    """
    if marker != "ok":
        return marker
    if not isinstance(document, dict):
        return _canonical(None)
    return _canonical({"mcpServers": _stable_servers(document.get("mcpServers"))})


#: The per-server keys of an ``mcp.json`` entry Crew reads at spawn whether or
#: not the spec mounts the file's servers: the switch-offs
#: ``acp.session_mcp.session_mcp_disabled_tools`` and
#: ``session_mcp_disabled_servers`` union into every session's restrictions.
_RESTRICTION_KEYS = ("disabled", "disabledTools")

#: The Kiro Crew servers whose declaration in either ``mcp.json`` decides, at
#: each session start, whether Crew mounts its per-session element: the set
#: ``acp.session_mcp.IDENTITY_BOUND_SERVERS`` is built from (a test pins the two
#: equal), read from its source so this module stays off the ACP layer.
_IDENTITY_BOUND_SERVERS = frozenset(CONTROL_PLANE_SERVERS + KIROCREW_BIN_MCP_SERVERS)


def _mcp_json_mounted(marker: str, document: Any, backend: str) -> bool:
    """Whether a session on *backend* running the spec *document* mounts the ``mcp.json`` servers.

    The spec's ``includeMcpJson``, read the way each host reads it
    (``docs/agent-spec-fields.md``): kiro-cli takes an absent flag as true --
    the ``spec.get("includeMcpJson", True) is not False`` reading of
    ``agent_capabilities`` -- and KAS as false (its own disk schema; Crew
    forwards only a stated bool, ``acp.kas_agents``). Every other backend is
    handed the spec's own ``mcpServers`` alone (``acp.session_mcp.session_mcp_servers``),
    so the files' servers never reach it. A spec with no fields to read (absent,
    unreadable, unparseable) has no flag, which kiro-cli reads as the default.
    """
    stated = (
        document.get("includeMcpJson") if marker == "ok" and isinstance(document, dict) else None
    )
    if backend == ACP_BACKEND_KIRO:
        return stated is not False
    if backend == ACP_BACKEND_KAS:
        return stated is True
    return False


def _restriction_part(marker: str, document: Any) -> str:
    """The text of one ``mcp.json`` that a session reads whatever its spec's ``includeMcpJson``.

    Every server's :data:`_RESTRICTION_KEYS`, and the whole entry of each server
    in :data:`_IDENTITY_BOUND_SERVERS`, whose declarations in both
    files decide at each session start whether Crew mounts its per-session
    element (``acp.session_mcp.native_mount_withholding``). A file's other
    servers mount only through the flag, so adding or editing one changes
    nothing a spec that opts out runs on. An absent file restricts nothing, as
    one with only ordinary servers does: creating the file with such a server
    is no change to a session that does not mount it.
    """
    if marker == "absent":
        return _canonical({"mcpServers": {}})
    if marker != "ok":
        return marker
    if not isinstance(document, dict):
        return _canonical(None)
    # A file without the key declares no servers, as `{"mcpServers": {}}` and an
    # absent file do: the three are one state to the session, so one view here.
    servers = document.get("mcpServers", {})
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        return _canonical({"mcpServers": servers})
    view: dict[str, Any] = {}
    for name, entry in servers.items():
        if name in _IDENTITY_BOUND_SERVERS:
            view[name] = _stable_servers({name: entry})[name]
        elif isinstance(entry, dict):
            kept = {key: entry[key] for key in _RESTRICTION_KEYS if key in entry}
            if kept:
                view[name] = kept
    return _canonical({"mcpServers": view})


def _identity_part(marker: str, document: Any) -> str:
    """The :data:`_IDENTITY_BOUND_SERVERS` declarations of one ``mcp.json``.

    Crew reads them at each session start to decide whether it mounts its
    per-session element (``acp.session_mcp.native_mount_withholding``), and no
    backend's MCP hot reload asks that again, so :func:`is_stale` compares this
    part on every provider. An absent file declares none, as an empty one does.
    """
    if marker == "absent":
        return _canonical({"mcpServers": {}})
    if marker != "ok":
        return marker
    if not isinstance(document, dict):
        return _canonical(None)
    # A file without the key declares no servers, as `{"mcpServers": {}}` and an
    # absent file do: the three are one state to the session, so one view here.
    servers = document.get("mcpServers", {})
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        return _canonical({"mcpServers": servers})
    view = {
        name: _stable_servers({name: servers[name]})[name]
        for name in sorted(_IDENTITY_BOUND_SERVERS)
        if name in servers
    }
    return _canonical({"mcpServers": view})


def _effective_mcp(parts: dict[str, str], name: str) -> str | None:
    """The part of ``mcp.json`` input *name* that the recorded spec makes effective.

    The whole file when the spec mounts its servers, else its restriction view.
    A record without the ``mcp_json`` part predates the distinction and is read
    as mounting them, the comparison it was taken for.
    """
    if parts.get("mcp_json", "1") == "1":
        return parts.get(name)
    return parts.get(f"{name}_restrictions")


#: At most this many ``@server`` ref digests are kept in a fingerprint's
#: ``tool_refs``. Past it, the rest are folded into one overflow entry -- their
#: count and the XOR of their digests -- so a spec of any size costs a bounded
#: record and an edit past the cap still changes the set (the old overflow entry
#: reads as removed, so it is stale on every provider, never unchanged).
FINGERPRINT_MAX_TOOL_REFS = 256


def _tool_refs(marker: str, document: Any) -> frozenset[str]:
    """Digests of the ``@server`` refs in a spec's ``tools``; none when it has no fields.

    Each ref is held as its :func:`_part_digest`, never its text: the entries are
    agent-controlled and unbounded in length. The
    :data:`FINGERPRINT_MAX_TOOL_REFS` smallest digests are kept; any others are
    folded into one fixed-size overflow entry, which no entry order changes.
    """
    if marker != "ok" or not isinstance(document, dict):
        return frozenset()
    digests: set[str] = set()
    for key in _TOOL_REF_FIELDS:
        value = document.get(key)
        if isinstance(value, list):
            digests.update(_part_digest(e) for e in value if _is_server_ref(e))
    return _bound_tool_refs(digests)


def _server_of_ref(entry: str) -> str:
    """The server a ``@server`` or ``@server/tool`` ref names."""
    return entry[1:].split("/", 1)[0]


def _tool_ref_servers(marker: str, document: Any) -> tuple[tuple[str, str], ...]:
    """``(ref digest, server digest)`` for each ref :func:`_tool_refs` keeps, sorted by ref.

    The same refs :func:`_bound_tool_refs` keeps (the smallest digests, up to the
    cap); the folded rest have no entry and read as mounted.
    """
    if marker != "ok" or not isinstance(document, dict):
        return ()
    pairs: dict[str, str] = {}
    for key in _TOOL_REF_FIELDS:
        value = document.get(key)
        if isinstance(value, list):
            for entry in value:
                if _is_server_ref(entry):
                    pairs[_part_digest(entry)] = _part_digest(f"@{_server_of_ref(entry)}")
    kept = sorted(pairs)[:FINGERPRINT_MAX_TOOL_REFS]
    return tuple((digest, pairs[digest]) for digest in kept)


_OVERFLOW_PREFIX = "overflow:"


def _bound_tool_refs(entries: Iterable[str]) -> frozenset[str]:
    """*entries* held to :data:`FINGERPRINT_MAX_TOOL_REFS` digests plus one overflow entry.

    The smallest digests are kept; the rest, and any overflow entry already among
    *entries* (a bounded set being bounded again, as :func:`adopt_live_refs` does
    with a union of two), are folded into one entry: their count and the XOR of
    their digests. No entry order changes the result, and the size never exceeds
    the cap plus one however many sets are folded in.
    """
    digests = {e for e in entries if not e.startswith(_OVERFLOW_PREFIX)}
    folded = [e for e in entries if e.startswith(_OVERFLOW_PREFIX)]
    if len(digests) <= FINGERPRINT_MAX_TOOL_REFS and len(folded) <= 1:
        return frozenset(digests | set(folded))
    ordered = sorted(digests)
    kept, rest = ordered[:FINGERPRINT_MAX_TOOL_REFS], ordered[FINGERPRINT_MAX_TOOL_REFS:]
    count, overflow_xor = len(rest), 0
    for digest in rest:
        overflow_xor ^= int(digest, 16)
    for entry in folded:
        _, folded_count, folded_xor = entry.split(":")
        count += int(folded_count)
        overflow_xor ^= int(folded_xor, 16)
    return frozenset((*kept, f"{_OVERFLOW_PREFIX}{count}:{overflow_xor:064x}"))


def _live_server_refs(*documents: tuple[str, Any]) -> frozenset[str]:
    """Digests of ``@<name>`` for every server declared and not ``disabled`` in *documents*.

    Each document is ``(marker, document)`` as read; one with no fields
    contributes nothing. A server present and enabled in any of them counts as
    running, which errs toward a Reload where the file that declares it is not
    the one the session mounts. Bounded like :func:`_tool_refs`; see
    :func:`is_stale` for how an overflow entry is read.
    """
    refs: set[str] = set()
    for marker, document in documents:
        if marker != "ok" or not isinstance(document, dict):
            continue
        servers = document.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for name, entry in servers.items():
            if isinstance(name, str) and not (
                isinstance(entry, dict) and entry.get("disabled") is True
            ):
                refs.add(_part_digest(f"@{name}"))
    return _bound_tool_refs(refs)


@functools.lru_cache(maxsize=None)
def _unconsumed_spec_fields(backend: str) -> frozenset[str]:
    """Top-level spec fields a session on *backend* never receives.

    Read from the backend's declared agent-config rulings
    (:func:`kiro_crew.agent_sdk.backend_mcp_ability.ability_for`): a concern
    ruled ``no-channel`` has no path into that harness, so an edit to it changes
    nothing a reload would. ``hooks`` is the exception where Crew carries the
    field itself (``SessionCapabilities.crew_fires_spec_hooks``). A backend with
    no mirror (kiro-cli, KAS), or whose rulings cannot be read, skips nothing.
    """
    keys = spec_keys()
    skipped = {keys[cid] for cid in ability_for(backend).no_channel if cid in keys}
    if capabilities_for(backend).crew_fires_spec_hooks:
        skipped.discard("hooks")
    return frozenset(skipped)


def _spawn_only_part(marker: str, document: Any, backend: str = "") -> str:
    """The spawn-only text of one agent spec: every field but MCP and ``model``.

    ``tools`` keeps only its non-``@server`` entries (the ``@server`` refs are
    reconcilable, :func:`_mcp_part`); ``allowedTools`` is kept whole. A field
    *backend* never receives (:func:`_unconsumed_spec_fields`) is left out, so
    editing it does not mark the chat stale.
    """
    if marker != "ok":
        return marker
    if not isinstance(document, dict):
        return _canonical(document)
    unconsumed = _unconsumed_spec_fields(backend)
    part: dict[str, Any] = {}
    for key, value in document.items():
        if key in _NON_SPAWN_ONLY_SPEC_FIELDS or key in unconsumed:
            continue
        if key in _TOOL_REF_FIELDS and isinstance(value, list):
            value = [e for e in value if not _is_server_ref(e)]
        part[key] = value
    return _canonical(part)


def _digest(parts: list[str]) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "surrogatepass"))
        h.update(b"\0")
    return h.hexdigest()


def _part_digest(text: str) -> str:
    """A fixed-size stand-in for one input's canonical field text.

    Every reader only compares parts for equality, so the record a slot or a
    warm-pool process keeps holds a 64-character digest per input instead of
    the text itself, which for a large spec can run to tens of megabytes.
    """
    return _digest([text])


def _gated_dir(directory: Path) -> Path | None:
    """*directory* canonicalized through the dashboard's path gate, or ``None``.

    Every directory this module lists or stats goes through
    :func:`kiro_crew.hooks.validate_file_path` first, the gate the file reads
    already use: a local ``.kiro/agents`` link or junction resolving to a UNC
    share or a sensitive path is refused before any syscall touches it. A refused
    directory is never scanned; its callers report it unreadable. An admitted
    one is then listed and stat'ed only through a pin (:func:`_pin`), never by
    its name again. Windows never reaches this: the entry points short-circuit
    there (:data:`WINDOWS_SCAN_DISABLED`).
    """
    admitted = validate_file_path(str(directory))
    return Path(admitted) if admitted is not None else None


def _pin(admitted: Path) -> platform_compat.PinnedDirectory:
    """Pin the gate-admitted directory *admitted*, refusing a link at ANY component.

    The gate validated a path string, and re-opening that string for the scan
    would follow whatever sits on the way to it by then: a ``.kiro/agents`` --
    or any directory ABOVE it -- swapped for a link or a Windows junction to a
    UNC share after the screen. Opening the whole absolute path at once refuses
    a link at the final name only; every ancestor is still resolved by name.

    So the walk starts at the path's anchor and pins one component at a time,
    each opened through the pin of the one above it
    (:meth:`~kiro_crew.platform_compat.PinnedDirectory.child`), ``dir_fd``-
    relative with ``O_NOFOLLOW``. Each parent is released once its child is held, because the
    child's own handle keeps the chain fixed from there. Every listing and
    per-entry stat after it goes through the final pinned handle. A pin that
    fails raises ``OSError``: its callers report that input unknown, never
    follow it. Windows never reaches this: the entry points short-circuit there
    (:data:`WINDOWS_SCAN_DISABLED`).
    """
    anchor = admitted.anchor
    parts = admitted.relative_to(anchor).parts
    current = platform_compat.pinned_directory(anchor)
    try:
        for index, part in enumerate(parts):
            try:
                below = current.child(part)
            except NotADirectoryError:
                if index == len(parts) - 1 and _is_plain_file_in(current, part):
                    raise _PlainFileAtName(
                        errno.ENOTDIR, "a plain file, not a directory", str(admitted)
                    ) from None
                raise
            current.close()
            current = below
    except BaseException:
        current.close()
        raise
    return current


class _PlainFileAtName(NotADirectoryError):
    """:func:`_pin` found a regular file, not a directory, at the final name.

    Its callers treat that as no spec scope, as they treat a missing one. The
    question is asked of the pinned parent, so the answer never comes from
    resolving the admitted path by name again.
    """


def _is_plain_file_in(pinned: platform_compat.PinnedDirectory, name: str) -> bool:
    """Whether *name* in *pinned* is a regular file, a link at the name not followed."""
    try:
        return stat.S_ISREG(pinned.lstat(name).st_mode)
    except OSError:
        return False


#: Backends that read the user-level agents directory FIRST, then the crewmate
#: folder (``paths.mate_agents_dir``), and the checkout's ``.kiro/agents`` only
#: as a fallback for a name neither declares; a mate service agent's spec is
#: read from the crewmate folder before the user level (``acp/harness/kas.py``,
#: ``resolve_projected_spec``). Every other backend reads the checkout first.
_USER_SCOPE_FIRST_BACKENDS = frozenset({ACP_BACKEND_KAS})


@dataclass(frozen=True)
class _SpecFile:
    """A selected spec file and what its bytes, read through the scope's pin, held."""

    path: Path
    marker: str
    document: Any


@dataclass(frozen=True)
class _SpecSelection:
    """The ``workspace`` and ``user`` spec a backend loads, and whether a scope was unknown."""

    workspace: _SpecFile | None = None
    user: _SpecFile | None = None
    #: The scopes (``workspace``, ``user``, ``mate``) that could not be pinned (a
    #: link or junction at its name, a refused or unreadable directory): their
    #: spec is unknown, never followed.
    unknown: frozenset[str] = frozenset()
    #: The crewmate folder's spec, read by KAS alone (see :func:`_agent_spec_files`).
    mate: _SpecFile | None = None

    @property
    def loaded(self) -> _SpecFile | None:
        """The one spec the backend runs on: the checkout's when read, else the others."""
        return self.workspace or self.mate or self.user


#: JSON's insignificant whitespace (RFC 8259 section 2), the session reader's
#: emptiness test for ``mcp.json``; ``bytes.strip()`` with no argument would also
#: strip bytes a JSON document may not contain.
_JSON_WHITESPACE = b" \t\n\r"


def _read_pinned_spec(
    pinned: platform_compat.PinnedDirectory,
    path: Path,
    *,
    name: str | None = None,
    settings: bool = False,
) -> tuple[str, Any]:
    """The entry *name* (default ``path.name``) of *pinned*, read and parsed as *path*.

    The markers of :func:`_read_json`. A link, a hardlink or a non-regular entry
    is refused by the read itself, and the cap is the spec resolver's read budget,
    :data:`kiro_crew.agent.PINNED_SPEC_DIR_MAX_TOTAL_BYTES`: an agent can plant
    the file and ask for a re-check, so a parse must stay cheap.

    *settings* parses the bytes as an ``mcp.json`` the way the session's own
    reader does (:func:`kiro_crew.user_json.loads_user_jsonc`: comments and
    trailing commas accepted); otherwise the bytes are an agent spec and parse
    by suffix (:func:`parse_agent_spec_bytes`).
    """
    # circular import: agent imports the config package at module load.
    from kiro_crew import agent

    cap = agent.PINNED_SPEC_DIR_MAX_TOTAL_BYTES
    try:
        data = pinned.read_bytes(name or path.name, max_bytes=cap)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None
    # RecursionError is a document nested past the parser's depth: a syntax
    # error like any other, so it reads as unparseable.
    try:
        if settings:
            # The session reader's rule (``acp.session_mcp._read_mcp_settings``):
            # one leading BOM is dropped, and a file left empty or holding only
            # JSON whitespace declares nothing, so it reads as ``{}``.
            data = data.removeprefix(codecs.BOM_UTF8)
            if not data.strip(_JSON_WHITESPACE):
                return "ok", {}
            return "ok", loads_user_jsonc(data.decode("utf-8"))
        return "ok", parse_agent_spec_bytes(data, path)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return "unparseable:" + hashlib.sha256(data).hexdigest(), None


def _resolve_in_scope(directory: Path, agent: str, *, json_only: bool) -> _SpecFile | None:
    """The spec *agent* resolves to in *directory*, resolved and read through one pin.

    The gate screens the directory, then the pin opens it without following a
    link at its name, and the resolver lists and reads its entries through that
    handle alone: the screened name is never reopened, so a ``.kiro/agents``
    swapped for a junction to a UNC share after the screen is not scanned.
    Raises ``OSError`` for a scope that is refused or cannot be pinned (other
    than absent) -- its caller reports the spec unknown -- and ``ValueError``
    when two specs declare the name.
    """
    # circular import: agent imports the config package at module load.
    from kiro_crew.agent import pinned_agent_spec_path

    admitted = _gated_dir(directory)
    if admitted is None:
        raise PermissionError(f"refused by the path gate: {directory!r}")
    try:
        pinned = _pin(admitted)
    except FileNotFoundError:
        return None
    except _PlainFileAtName:
        # A plain file at the name is no spec scope, as a missing one is.
        return None
    with pinned:
        path = pinned_agent_spec_path(
            agent, agents_dir=admitted, pinned=pinned, json_only=json_only
        )
        if path is None:
            return None
        marker, document = _read_pinned_spec(pinned, path)
        return _SpecFile(path, marker, document)


def _agent_spec_files(agent: str, project: str, backend: str) -> _SpecSelection:
    """The ``(workspace, user)`` spec files *backend* loads for *agent*.

    The file watched must be the file the backend loads, or an edit to the one
    it runs on goes unseen:

    * kiro-cli (:data:`ACP_BACKEND_KIRO`) opens the spec itself and reads
      ``*.json`` only, the checkout's ``.kiro/agents`` first and then the
      user-level directory (see :func:`kiro_crew.agent.markdown_spec_for_agent`).
      A markdown file is never its spec, in either scope.
    * KAS reads the user-level directory first, either form, a ``<name>.json``
      twin beating ``<name>.md`` (:func:`kiro_crew.acp.kas_agents.load_agent_spec`),
      then the crewmate folder, and a name neither declares falls back to the
      checkout's spec, reduced to the keys KAS honours there; a mate service
      agent's spec is read from the crewmate folder first
      (:func:`kiro_crew.acp.harness.kas.resolve_projected_spec`). Each later
      scope is read only when the earlier ones hold no spec, so at most one is
      found.
    * Every other backend runs on the spec Crew reads for it in
      ``acp/session_mcp._agent_spec_and_snapshot_for``: the checkout's spec
      first, then the user-level one, either form.

    The workspace file, when present, is the one loaded. Each scope is resolved
    and its spec read through a pin of that scope (:func:`_resolve_in_scope`);
    a scope that cannot be pinned leaves its spec unknown.
    """
    if not agent:
        return _SpecSelection()
    json_only = backend == ACP_BACKEND_KIRO
    found: dict[str, _SpecFile | None] = {"workspace": None, "user": None, "mate": None}
    unknown: set[str] = set()
    # circular import: agent imports the config package at module load.
    from kiro_crew.agent import kiro_agents_dir_path

    def resolve(scope: str, directory: Path) -> None:
        try:
            found[scope] = _resolve_in_scope(directory, agent, json_only=json_only)
        except ValueError:
            # Two specs declare the name: which one the backend loaded is
            # undefined, so there is no single file to watch. Treated as "no
            # file", which is stable across turns and so never reads as stale
            # on its own account.
            found[scope] = None
        except OSError:
            unknown.add(scope)

    if backend not in _USER_SCOPE_FIRST_BACKENDS:
        resolve("user", kiro_agents_dir_path())
        if project:
            resolve("workspace", project_agents_dir(project))
    else:
        for scope, directory in _user_first_scopes(agent, project):
            resolve(scope, directory)
            if found[scope] is not None or scope in unknown:
                break
    return _SpecSelection(found["workspace"], found["user"], frozenset(unknown), found["mate"])


def _scopes_read(
    agent: str, project: str, backend: str, specs: _SpecSelection
) -> list[tuple[str, Path]]:
    """The scopes :func:`_agent_spec_files` read for this selection, for the listing check."""
    if backend not in _USER_SCOPE_FIRST_BACKENDS:
        return _spec_scope_unlistable_default_scopes(project)
    read: list[tuple[str, Path]] = []
    for scope, directory in _user_first_scopes(agent, project):
        read.append((scope, directory))
        if getattr(specs, scope) is not None or scope in specs.unknown:
            break
    return read


def _spec_scope_unlistable_default_scopes(project: str) -> list[tuple[str, Path]]:
    # circular import: agent imports the config package at module load.
    from kiro_crew.agent import kiro_agents_dir_path

    scopes = [("user", kiro_agents_dir_path())]
    if project:
        scopes.append(("workspace", project_agents_dir(project)))
    return scopes


def _user_first_scopes(agent: str, project: str) -> list[tuple[str, Path]]:
    """The scopes a :data:`_USER_SCOPE_FIRST_BACKENDS` backend reads for *agent*, in its order."""
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.agent_files import is_mate_service_agent

    scopes = [("user", kiro_agents_dir_path()), ("mate", mate_agents_dir())]
    if is_mate_service_agent(agent):
        scopes.reverse()
    if project:
        scopes.append(("workspace", project_agents_dir(project)))
    return scopes


def _mirrored_document(
    backend: str,
    project: str,
    workspace_spec: _SpecFile | None,
    user_spec: _SpecFile | None,
    marker: str,
    document: Any,
) -> tuple[str, Any]:
    """The spec document a mirrored session consumes, for the spec parts.

    A backend whose declaration says a mirror projects the spec's servers
    (``ProjectionKind.MIRROR``, read through :func:`ability_for` rather than a
    backend set, as the cards are) does not run on a checkout's spec as a
    whole: unless the project is trusted to choose MCP servers, the session
    consumes the USER spec with only the checkout spec's switch-off keys
    layered on, and the checkout spec alone when there is no user spec. Fingerprinting the whole checkout spec there misses an edit to
    the user spec's server arguments, which the live session runs on, and flags
    a checkout-only edit the session never sees. The composition is the SDK's
    (:func:`kiro_crew.agent_sdk.agent_specs.mirrored_session_spec`), the same
    rule the session applies, so the two cannot drift. A native backend
    (kiro-cli) and an external projection (KAS) load their own files, and a
    broker-only one never receives the spec's servers: each keeps the selected
    document.
    """
    if ability_for(backend).projection != "mirror" or workspace_spec is None:
        return marker, document
    if marker != "ok" or not isinstance(document, dict):
        return marker, document
    if user_spec is not None and user_spec.marker != "ok":
        # The session would consume the user spec, which cannot be read: its
        # scope is already unreadable, so the compared document is unknown too.
        return user_spec.marker, None
    user_doc = user_spec.document if user_spec is not None else None
    if user_doc is not None and not isinstance(user_doc, dict):
        user_doc = None
    return "ok", mirrored_session_spec(project, document, user_doc)


def _spec_scope_unlistable(
    project: str, backend: str, scopes: list[tuple[str, Path]] | None = None
) -> frozenset[str]:
    """The scopes whose agents directory the backend searches exists but cannot be listed.

    The spec resolver scans that directory, and a scan of one it cannot read
    finds nothing -- which would read as "no spec here" rather than "unknown",
    so a spec made unreadable by revoking its directory's permissions would
    pass as a change to a missing file instead of an input that cannot be
    checked.
    """
    # circular import: agent imports the config package at module load.
    from kiro_crew.agent import kiro_agents_dir_path

    if scopes is None:
        scopes = [("user", kiro_agents_dir_path())]
        if project:
            scopes.append(("workspace", project_agents_dir(project)))
    unlistable: set[str] = set()
    for scope, directory in scopes:
        admitted = _gated_dir(directory)
        if admitted is None:
            unlistable.add(scope)
            continue
        try:
            with _pin(admitted) as pinned, pinned.scan() as entries:
                next(entries, None)
        except FileNotFoundError:
            continue
        except _PlainFileAtName:
            # A plain file at the name is no spec scope, as a missing one is.
            # Anything else the pin refuses (a link, a junction) is unknown.
            continue
        except OSError:
            unlistable.add(scope)
    return frozenset(unlistable)


def _global_mcp_json() -> Path:
    """The user-level ``mcp.json`` a spawned session reads.

    The same fixed ``~/.kiro/settings/mcp.json`` the agent spec writer and the
    dashboard's MCP settings read (``agent._KIRO_MCP_JSON``), which deliberately
    ignores ``KIRO_HOME``: hashing a ``KIRO_HOME``-relative path would watch a
    file the session never reads and miss every edit to the one it does.
    """
    # circular import: agent imports the config package at module load.
    from kiro_crew import agent

    return agent._KIRO_MCP_JSON


def _assemble(
    parts: dict[str, str],
    *,
    tool_refs: frozenset[str],
    unreadable: frozenset[str],
    live_server_refs: frozenset[str] = frozenset(),
    ref_servers: tuple[tuple[str, str], ...] = (),
) -> ConfigFingerprint:
    """Build the two digests from the named per-input texts."""
    workspace = bool(parts.get("spec_ws"))
    reconcilable = [
        "spec",
        parts.get("spec_path", ""),
        parts.get("spec_mcp", "absent"),
        "mcp",
        _effective_mcp(parts, "global_mcp") or "absent",
    ]
    # ``workspace_mcp`` (``<project>/.kiro/settings/mcp.json``) is in no digest:
    # it is the one input whose reconcilable entries an agent in the project can
    # write, and :func:`is_stale` compares its part on its own.
    # A workspace spec shadows the user-level one, so only the loaded spec's
    # spawn-only fields are hashed; a workspace spec appearing or disappearing
    # moves the digest too. The shadowed user spec's path is left out as well:
    # the session never reads that file, so adding or removing it is no change.
    fields = parts.get("spec_fields", "absent")
    if workspace:
        user_part, workspace_part = "shadowed", fields
        user_path, workspace_path = "", parts.get("spec_path", "")
    else:
        user_part = (
            parts.get("user_spec_fields", fields) if parts.get("user_spec_path") else "absent"
        )
        user_path = parts.get("user_spec_path", "")
        workspace_part, workspace_path = "absent", ""
    spawn_only = [
        "backend",
        parts.get("backend", ""),
        "user-spec",
        user_path,
        user_part,
        "workspace-spec",
        workspace_path,
        workspace_part,
    ]
    return ConfigFingerprint(
        reconcilable=_digest(reconcilable),
        spawn_only=_digest(spawn_only),
        tool_refs=tool_refs,
        live_server_refs=live_server_refs,
        ref_servers=ref_servers,
        parts=tuple(sorted(parts.items())),
        unreadable=unreadable,
    )


#: The ``config.json`` key a backend resolves from when the caller names none.
DEFAULT_BACKEND_KEY = "agent.acp_backend"


def compute_fingerprint(
    inputs: SpawnInputs, *, backend: str, backend_key: str = DEFAULT_BACKEND_KEY
) -> ConfigFingerprint:
    """Fingerprint the config a session spawned from *inputs* would read now.

    *backend_key* is the ``config.json`` key *backend* was resolved from, kept
    beside the parts (and in no digest) so a backend change is named by the key
    that actually moved. Blocking file IO: call it off the event loop. An input
    that exists but cannot be read is named in ``unreadable`` and hashed as its
    marker; compare through :func:`carry_forward` so that input does not read
    as a change. With :data:`WINDOWS_SCAN_DISABLED` nothing is read: every
    file input is unreadable, so the reading is unknown.
    """
    if WINDOWS_SCAN_DISABLED:
        unread = {"spec", "user_spec", "global_mcp"} | (
            {"workspace_mcp"} if inputs.project else set()
        )
        return _assemble(
            {
                "spec_path": "",
                "spec_ws": "",
                "user_spec_path": "",
                "backend": backend,
                "backend_key": backend_key,
            },
            tool_refs=frozenset(),
            unreadable=frozenset(unread),
        )
    specs = _agent_spec_files(inputs.agent, inputs.project, backend)
    # The crewmate folder's spec is trusted like a user-level one and read in
    # its place (``kas.resolve_projected_spec``), so it is digested as one.
    workspace_spec, user_spec = specs.workspace, specs.user or specs.mate
    selected = specs.loaded
    spec_marker, spec_doc = (
        (selected.marker, selected.document) if selected is not None else ("absent", None)
    )
    spec_marker, spec_doc = _mirrored_document(
        backend, inputs.project, workspace_spec, user_spec, spec_marker, spec_doc
    )
    if (
        backend in _USER_SCOPE_FIRST_BACKENDS
        and selected is workspace_spec
        and spec_marker == "ok"
        and isinstance(spec_doc, dict)
    ):
        spec_doc = kas_project_spec_view(spec_doc)
    global_marker, global_doc = _read_json(_global_mcp_json())
    user_marker, user_doc = (
        (user_spec.marker, user_spec.document) if user_spec is not None else ("absent", None)
    )
    unreadable: set[str] = set()
    # Per scope: an unknown project scope leaves the user-level spec compared.
    unknown_scopes = specs.unknown | _spec_scope_unlistable(
        inputs.project, backend, _scopes_read(inputs.agent, inputs.project, backend, specs)
    )
    if unknown_scopes or spec_marker in _NO_FIELDS:
        unreadable.add("spec")
    if "user" in unknown_scopes or user_marker in _NO_FIELDS:
        unreadable |= {"spec", "user_spec"}
    if global_marker in _NO_FIELDS:
        unreadable.add("global_mcp")
    parts = {
        "spec_path": str(selected.path) if selected is not None else "",
        "spec_ws": "1" if workspace_spec is not None else "",
        "user_spec_path": str(user_spec.path) if user_spec is not None else "",
        "user_spec_fields": _part_digest(
            _spawn_only_part(user_marker, user_doc, backend) if user_spec is not None else "absent"
        ),
        "backend": backend,
        "backend_key": backend_key,
        "spec_mcp": _part_digest(_mcp_part(spec_marker, spec_doc)),
        "spec_fields": _part_digest(
            _spawn_only_part(spec_marker, spec_doc, backend) if selected is not None else "absent"
        ),
        "mcp_json": "1" if _mcp_json_mounted(spec_marker, spec_doc, backend) else "0",
        "global_mcp": _part_digest(_mcp_part(global_marker, global_doc)),
        "global_mcp_restrictions": _part_digest(_restriction_part(global_marker, global_doc)),
        "global_mcp_identity": _part_digest(_identity_part(global_marker, global_doc)),
    }
    documents = [(spec_marker, spec_doc), (global_marker, global_doc)]
    if inputs.project:
        ws_marker, ws_doc = _read_json(Path(inputs.project) / ".kiro" / "settings" / "mcp.json")
        if ws_marker in _NO_FIELDS:
            unreadable.add("workspace_mcp")
        parts["workspace_mcp"] = _part_digest(_mcp_part(ws_marker, ws_doc))
        parts["workspace_mcp_restrictions"] = _part_digest(_restriction_part(ws_marker, ws_doc))
        parts["workspace_mcp_identity"] = _part_digest(_identity_part(ws_marker, ws_doc))
        documents.append((ws_marker, ws_doc))
    return _assemble(
        parts,
        tool_refs=_tool_refs(spec_marker, spec_doc),
        unreadable=frozenset(unreadable),
        live_server_refs=_live_server_refs(*documents),
        ref_servers=_tool_ref_servers(spec_marker, spec_doc),
    )


def _stat_in(pinned: platform_compat.PinnedDirectory, name: str) -> tuple[object, ...]:
    """``(mtime_ns, size, ctime_ns)`` of *name* in *pinned*, a link not followed, or a marker.

    ``ctime_ns`` moves on a permission or owner change, which leaves the other
    two as they were: a file made readable again must change the signature, or
    a chat whose reading was unknown because of it is never re-checked.
    """
    try:
        st = pinned.lstat(name)
    except FileNotFoundError:
        return ("absent",)
    except OSError:
        return ("unreadable",)
    return (st.st_mtime_ns, st.st_size, st.st_ctime_ns)


#: At most this many spec entries of one agents directory are kept, by name
#: and stat, in a slot's stat signature. Past it, the rest are folded into one
#: overflow marker -- their count and a digest of their names and stats -- so a
#: directory of any size costs a bounded signature and a change past the cap
#: still changes it.
SIGNATURE_MAX_SPEC_ENTRIES = 256


class _Largest:
    """A name ordered in reverse, so ``heapq`` keeps the largest at its top."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __lt__(self, other: "_Largest") -> bool:
        return self.name > other.name


def _entry_digest(pinned: platform_compat.PinnedDirectory, name: str) -> int:
    """One overflowed entry's 256-bit digest, combined order-independently."""
    text = repr((name, _stat_in(pinned, name))).encode("utf-8", "replace")
    return int.from_bytes(hashlib.sha256(text).digest(), "big")


def _gated_stat(path: Path) -> tuple[object, ...]:
    """:func:`_stat_in` of a top-level input, after the same path gate.

    Stat'ed through a pin of its directory, so a link swapped in at a parent
    after the screen is refused rather than followed.
    """
    admitted = _gated_dir(path)
    if admitted is None:
        return ("refused",)
    try:
        pinned = _pin(admitted.parent)
    except FileNotFoundError:
        return ("absent",)
    except OSError:
        return ("unreadable",)
    with pinned:
        return _stat_in(pinned, admitted.name)


def _stat_spec_dir(directory: Path) -> tuple[object, ...]:
    """The directory's own stat plus one for every spec-shaped entry in it.

    The listing is part of the signature, so a spec file that appears (a new
    workspace spec, a markdown twin) changes it even when no existing file did.

    Bounded however large the directory: the scan is streamed, never listed.
    The lexicographically smallest :data:`SIGNATURE_MAX_SPEC_ENTRIES` names are
    kept with their stats (a bounded heap); every other entry is folded into a
    fixed-size overflow marker -- its count and the XOR of a sha256 per entry
    over name and stat -- which no iteration order changes and any edit, add or
    removal past the cap does.
    """
    admitted = _gated_dir(directory)
    if admitted is None:
        return ("refused",)
    cap = SIGNATURE_MAX_SPEC_ENTRIES
    kept: list[_Largest] = []
    overflow_count = 0
    overflow_xor = 0
    try:
        pinned = _pin(admitted)
    except FileNotFoundError:
        return ("absent",)
    except OSError:
        return ("unreadable",)
    with pinned:
        try:
            own = pinned.stat_self()
            with pinned.scan() as entries:
                for entry in entries:
                    name = entry.name
                    if not is_agent_spec_name(name):
                        continue
                    if len(kept) < cap:
                        heapq.heappush(kept, _Largest(name))
                        continue
                    if cap and name < kept[0].name:
                        name = heapq.heapreplace(kept, _Largest(name)).name
                    overflow_count += 1
                    overflow_xor ^= _entry_digest(pinned, name)
        except FileNotFoundError:
            return ("absent",)
        except OSError:
            return ("unreadable",)
        names = sorted(item.name for item in kept)
        signature: tuple[object, ...] = (
            (own.st_mtime_ns, own.st_size),
            *((n, _stat_in(pinned, n)) for n in names),
        )
    if overflow_count:
        signature += (("overflow", overflow_count, f"{overflow_xor:064x}"),)
    return signature


def input_signature(inputs: SpawnInputs) -> tuple[object, ...]:
    """A cheap stat-only signature of every file the fingerprint for *inputs* reads.

    ``lstat`` and directory listings only, nothing opened: the periodic sweep
    re-fingerprints a chat only when this changed since its last check. It
    covers every backend's spec scopes (the user agents directory, the crewmate
    folder and the project's) and both ``mcp.json`` files, so it errs toward a re-check, never
    toward a missed edit. The gateway config that names the backend is not
    stat'ed here: the process ``ConfigWatch`` owns that file
    (:func:`kiro_crew.dashboard.config_staleness.subscribe_backend_changes`).
    Blocking IO: call it off the event loop. A constant with
    :data:`WINDOWS_SCAN_DISABLED`, which stats nothing.
    """
    if WINDOWS_SCAN_DISABLED:
        return (("windows-scan", "disabled"),)
    # circular import: agent imports the config package at module load.
    from kiro_crew.agent import kiro_agents_dir_path

    parts: list[object] = [
        ("user-agents", _stat_spec_dir(kiro_agents_dir_path())),
        ("mate-agents", _stat_spec_dir(mate_agents_dir())),
        ("global-mcp", _gated_stat(_global_mcp_json())),
    ]
    if inputs.project:
        project = Path(inputs.project)
        parts.append(("project-agents", _stat_spec_dir(project_agents_dir(inputs.project))))
        parts.append(("workspace-mcp", _gated_stat(project / ".kiro" / "settings" / "mcp.json")))
    return tuple(parts)


def changed_inputs(recorded: ConfigFingerprint, current: ConfigFingerprint) -> frozenset[str]:
    """The inputs (``spec``, ``global_mcp``, ``workspace_mcp``, ``backend``) that differ.

    For naming a change to the person; the staleness decision is
    :func:`is_stale`. A fingerprint built without per-input parts names
    nothing.
    """
    if not recorded.parts or not current.parts:
        return frozenset()
    was, now = dict(recorded.parts), dict(current.parts)
    # ``mcp_json`` is derived from the spec AND the backend, so it names neither:
    # a flag edit moves ``spec_fields``, a backend switch ``backend``.
    # A workspace spec shadows the user-level one in both, so the user spec's
    # path is in no digest (:func:`_assemble`) and adding or removing it names
    # nothing.
    shadowed = bool(was.get("spec_ws")) and bool(now.get("spec_ws"))
    ignored = {"mcp_json", "user_spec_path"} if shadowed else {"mcp_json"}
    names = {
        name
        for name, keys in _INPUT_PARTS.items()
        if name not in _MCP_JSON_INPUTS
        and any(was.get(k) != now.get(k) for k in keys if k not in ignored)
    }
    # The user-level spec is the spec to the person; while a workspace spec
    # shadows it in both, it is no input of the session's at all.
    if "user_spec" in names:
        names.discard("user_spec")
        if not shadowed:
            names.add("spec")
    # When the spec or the backend now reads ``includeMcpJson`` differently, that
    # input is named for it, and a file is named only if it was itself edited:
    # its effective part moving between the two views is not an edit.
    same_reading = was.get("mcp_json", "1") == now.get("mcp_json", "1")
    for name in _MCP_JSON_INPUTS:
        if same_reading:
            moved = _effective_mcp(was, name) != _effective_mcp(now, name)
        else:
            moved = any(was.get(k) != now.get(k) for k in _INPUT_PARTS[name])
        if moved:
            names.add(name)
    if was.get("spec_path") != now.get("spec_path") or recorded.tool_refs != current.tool_refs:
        names.add("spec")
    return frozenset(names)


def carry_forward(recorded: ConfigFingerprint, current: ConfigFingerprint) -> ConfigFingerprint:
    """*current* with each input it could not read replaced by *recorded*'s value.

    What makes an unreadable input "no comparison" rather than "a change": that
    one input is put back as it was, and every other input is compared as read.
    The result keeps *current*'s ``unreadable`` set, so a caller can still say
    that input's staleness is unknown. A *recorded* with no per-input parts (one
    built by hand) is left to compare as it is.
    """
    if not current.unreadable or not recorded.parts:
        return current
    was = dict(recorded.parts)
    parts = dict(current.parts)
    tool_refs, ref_servers = current.tool_refs, current.ref_servers
    for name in current.unreadable:
        for key in _INPUT_PARTS[name]:
            if key in was:
                parts[key] = was[key]
        if name == "spec":
            tool_refs, ref_servers = recorded.tool_refs, recorded.ref_servers
    return _assemble(
        parts,
        tool_refs=tool_refs,
        unreadable=current.unreadable,
        live_server_refs=current.live_server_refs,
        ref_servers=ref_servers,
    )


def adopt_first_reads(recorded: ConfigFingerprint, current: ConfigFingerprint) -> ConfigFingerprint:
    """*recorded* with each input it could not read taken from *current*, where readable now.

    An input that was already unreadable or refused when the spawn fingerprint
    was recorded has no recorded value to compare against. Its first successful
    read is recorded against the provider instead of compared, so it never
    registers as a change, which would flag the chat stale with no config
    change behind it. Returns
    *recorded* itself when there is nothing to adopt.
    """
    first_reads = recorded.unreadable - current.unreadable
    if not first_reads or not recorded.parts or not current.parts:
        return recorded
    was = dict(recorded.parts)
    now = dict(current.parts)
    tool_refs, ref_servers = recorded.tool_refs, recorded.ref_servers
    for name in first_reads:
        for key in _INPUT_PARTS[name]:
            if key in now:
                was[key] = now[key]
        if name == "spec":
            tool_refs, ref_servers = current.tool_refs, current.ref_servers
    return _assemble(
        was,
        tool_refs=tool_refs,
        unreadable=recorded.unreadable - first_reads,
        live_server_refs=recorded.live_server_refs,
        ref_servers=ref_servers,
    )


def adopt_live_refs(recorded: ConfigFingerprint, current: ConfigFingerprint) -> ConfigFingerprint:
    """*recorded* with the ``@server`` refs *current* adds, once a turn has honoured them.

    A hot-reloading provider mounts a ref added to the spec's ``tools`` at the
    next turn boundary, but a ref removed while its server keeps running stays
    mounted (:mod:`kiro_crew.mcp_hot_reload`). The refs a completed turn ran
    with are therefore the live state a later removal is judged against:
    compared with the spawn record alone, a ref added and then removed is in
    neither set and the chat reads current while the tool is still mounted
    until Reload. The union is held to the same bound as any fingerprint's
    refs (:func:`_bound_tool_refs`), so a long-lived chat's record never grows
    past it however many edits its turns adopt. Returns *recorded* itself when
    nothing was added or the spec could not be read now.
    """
    if "spec" in current.unreadable or not recorded.parts:
        return recorded
    added = current.tool_refs - recorded.tool_refs
    if not added:
        return recorded
    tool_refs = _bound_tool_refs(recorded.tool_refs | added)
    servers = dict(recorded.ref_servers)
    servers.update((ref, server) for ref, server in current.ref_servers if ref in added)
    return _assemble(
        dict(recorded.parts),
        tool_refs=tool_refs,
        unreadable=recorded.unreadable,
        live_server_refs=recorded.live_server_refs,
        ref_servers=tuple(sorted((ref, servers[ref]) for ref in tool_refs if ref in servers)),
    )


def _workspace_mcp_moved(recorded: ConfigFingerprint, current: ConfigFingerprint) -> bool:
    """Whether the effective part of the project ``.kiro/settings/mcp.json`` differs between the two."""
    return _effective_mcp(dict(recorded.parts), "workspace_mcp") != _effective_mcp(
        dict(current.parts), "workspace_mcp"
    )


def _removed_refs_still_mounted(
    recorded: ConfigFingerprint, current: ConfigFingerprint, *, hot_reloads: bool
) -> bool:
    """Whether a ``@server`` ref removed from ``tools`` leaves its server mounted.

    Removing a ref never unmounts a running server, so a removal is stale on
    every provider. On one that applies MCP edits itself the dashboard's own
    switch-off (``handlers.mcp_agent_sync``) removes the ref AND writes
    ``disabled: true``: the reconcile stops that server, and demanding a Reload
    for it would be a Reload for a change already in effect. There, a removed
    ref whose server is declared and enabled nowhere (``live_server_refs``)
    is not stale. Each removed ref is looked up through the server it names
    (``ref_servers``), so a ``@server/tool`` ref is judged by its server as a
    whole-server ref is. A live set past the bound (an overflow entry), a
    removed overflow entry, or a ref with no server mapping cannot answer and
    is read as mounted.
    """
    removed = recorded.tool_refs - current.tool_refs
    if not removed:
        return False
    if not hot_reloads:
        return True
    live = current.live_server_refs
    if any(ref.startswith(_OVERFLOW_PREFIX) for ref in live):
        return True
    servers = dict(recorded.ref_servers)
    for ref in removed:
        if ref.startswith(_OVERFLOW_PREFIX):
            return True
        server = servers.get(ref)
        if server is None or server in live:
            return True
    return False


def is_stale(recorded: ConfigFingerprint, current: ConfigFingerprint, *, hot_reloads: bool) -> bool:
    """Whether a session spawned under *recorded* runs on config that differs from *current*.

    A spawn-only change (a spec's non-MCP fields, a ``@server`` ref removed
    from its ``tools``, the backend) is stale on every provider. A
    reconcilable one (MCP server entries in the spec or either ``mcp.json``, an
    added ref) is stale only on a provider that does not apply MCP edits
    itself (*hot_reloads* False, see
    :func:`kiro_crew.mcp_hot_reload.provider_hot_reloads`): one that does has
    already picked it up. A Kiro Crew server's declaration in either
    ``mcp.json`` (:func:`_identity_part`) is stale on every provider: Crew, not
    the backend, reads it at session start. A ref removed together with its
    server's switch-off is the exception on a hot-reloading provider: the
    reconcile stops that server, so nothing stays mounted
    (:func:`_removed_refs_still_mounted`).
    """
    if recorded.spawn_only != current.spawn_only:
        return True
    if _removed_refs_still_mounted(recorded, current, hot_reloads=hot_reloads):
        return True
    was, now = dict(recorded.parts), dict(current.parts)
    if any(was.get(key) != now.get(key) for key in _IDENTITY_PARTS):
        return True
    if hot_reloads:
        return False
    return (
        recorded.reconcilable != current.reconcilable
        or bool(current.tool_refs - recorded.tool_refs)
        or _workspace_mcp_moved(recorded, current)
    )


@dataclass
class SpawnConfigRecord:
    """The fingerprint recorded for ONE provider a slot's turns ran on.

    The provider is held weakly and compared by identity: a record describes the
    process it was taken against, and a successor (after any reset, switch or
    restart) is a different object whose config is re-recorded rather than
    inherited.
    """

    provider_ref: weakref.ReferenceType[Any]
    inputs: SpawnInputs
    fingerprint: ConfigFingerprint

    def describes(self, provider: object) -> bool:
        return provider is not None and self.provider_ref() is provider


def make_record(
    provider: object, inputs: SpawnInputs, fingerprint: ConfigFingerprint
) -> SpawnConfigRecord | None:
    """A record for *provider*, or None when it cannot be weakly referenced."""
    try:
        ref = weakref.ref(provider)
    except TypeError:
        return None
    return SpawnConfigRecord(provider_ref=ref, inputs=inputs, fingerprint=fingerprint)
