"""Shared MCP config cleanup utilities.

KiroCrew does NOT write KiroCrew-managed MCP servers to the user's global
provider MCP config (``~/.kiro/settings/mcp.json``) during normal
operation — the KiroCrew agent file is authoritative, and provider
globals are user-owned.  Remaining helpers here clean up stale
kirocrew-binary entries left over from older install methods.

Extracted from agent.py so both agent.py and cli.py can import at the
top level without circular dependencies (agent.py imports cli.py).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection, Iterable, Sized
from pathlib import Path
from typing import Any

from kiro_crew.agent_sdk.mcp_refs import RESERVED_TOOL_NAMESPACES
from kiro_crew.config.paths import kiro_home

#: kiro-cli's enterprise-governance discriminator. The spec WRITER owns the
#: literal (``agent._MCP_REGISTRY_TYPE``); this is the copy the readers share, and
#: a ratchet test pins them equal.
MCP_REGISTRY_TYPE = "registry"


def mcp_entry_is_registry_governed(entry: Any) -> bool:
    """Whether one ``mcpServers`` entry defers its launch to an admin's catalog.

    A ``"type": "registry"`` entry names a catalog record rather than describing a
    local process: in registry access mode the client resolves it by map key and
    applies the catalog's own command, and OUTSIDE that mode the client drops it.
    Neither outcome is a server this side may launch, which is why the answer does
    not depend on whether registry mode is declared locally -- the marker alone
    settles it.

    Here beside :func:`mcp_entry_is_muted` because every site that decides whether
    a server LAUNCHES has to give the same answer, and the gateway is one of them:
    a marked entry it wraps into a broker stub carries a local command the client
    never asked for, and the wrapped name is what the session projections subtract
    as a "stubbed name" -- so the entry arrives as a live local process and the
    marker governs nothing. The gateway reads this predicate for that reason, not
    only the projections.
    """
    if not isinstance(entry, dict):
        return False
    return entry.get("type") == MCP_REGISTRY_TYPE


def mcp_entry_is_muted(entry: Any) -> bool:
    """Whether one ``mcpServers`` entry asks not to be launched.

    Anything but an absent ``disabled`` or a literal ``False`` is a mute. That is
    FAIL-CLOSED on purpose, and the direction matters because the two mistakes are
    not symmetric: reading an odd value as "enabled" launches a server the user
    tried to silence, while reading it as "muted" withholds one they can un-mute
    by fixing the value. An ill-typed ``disabled`` is also not forwardable -- both
    kiro-cli's spec schema and KAS's wire schema type the field as a boolean and
    reject the document over it -- so there is no reading under which the odd value
    yields a working server.

    Lives here, in the module that already pins the shared managed-server set, so
    the sites that decide whether a server LAUNCHES share one answer instead of
    spelling it each. They did not: ``is True`` in the gateway rewriter against
    ``is not False`` in the session projections meant a server muted with a
    non-boolean was passed over by the rewriter's guard, wrapped into a live
    pooling stub, and so subtracted from the projection as a "stubbed name" before
    any mute check could see it -- muted in the spec, running in the session.

    Not every reader of ``disabled`` belongs here. A roster row or a capability
    listing answers "does the user consider this on", where ordinary truthiness is
    right and a wrong answer costs a chip, not a process. This predicate is for
    the launch decision -- and for every surface that ANNOUNCES it: the dashboard's
    server listing (``mcp_discovery.list_servers``, which also feeds the probe
    fan-out and the sync offer) and the ``GET /api/mcp`` stamping read it too,
    because a row shown enabled beside a launch gate that refuses it is the same
    disagreement as above in the other direction. The listing reports a
    non-boolean as a config error so the operator learns why the row is off; it
    never reads one as "on".
    """
    if not isinstance(entry, dict):
        return False
    return entry.get("disabled", False) is not False


def mcp_entries_muted(entries: Iterable[Any]) -> bool:
    """Whether a server is muted given its entry in EVERY scope that holds it.

    The multi-scope form of :func:`mcp_entry_is_muted`: one mute anywhere mutes
    the server. Scope priority decides which spec launches -- command, args,
    env -- but never un-mutes: a store entry saying ``false`` does not switch on
    a server the shared file muted with ``null``, because that ``null`` is the
    fail-closed read above, and the whole point of it is that a higher-priority
    ``false`` cannot argue it away. The listing already answers this way (a row is
    off when any scope mutes it); the agent rebuild renders the entry it emits
    with ``disabled: true`` on the same answer, so the file kiro-cli parses and
    the row the user sees never disagree -- and a selective ``@srv/tool`` ref the
    rebuild keeps cannot launch a server every surface calls muted.
    """
    return any(mcp_entry_is_muted(entry) for entry in entries)


logger = logging.getLogger(__name__)


#: How many distinct ``(server, value shape)`` pairs the invalid-``disabled``
#: ledger reports individually. Warn-once for a handful of config errors needs a
#: handful of entries; past the cap the ledger says once that it stopped. Small
#: on purpose: the ledger lives for the process and is fed by every read of the
#: shared configs (``GET /api/mcp``, the agent rebuild), so its size is the
#: memory a malformed shared config can pin through nothing but a page load.
INVALID_DISABLED_LEDGER_CAP = 64

#: Characters of a server name a WARNING line -- and the ledger -- carries. The
#: line is retained (the dashboard log ring keeps the last 1000 FORMATTED lines
#: and streams each to every log subscriber, with no bound on a line's size), so
#: the config text a line quotes is what one page load can pin and push. The
#: value is never quoted (:func:`describe_disabled_flag`); the name is
#: operator-authored config as well, so it is cut here. Long enough that any
#: real server name survives whole.
LOG_NAME_MAX = 64

#: ``(name as logged, value as described)`` pairs already reported. Both members
#: are bounded strings -- the name cut to :data:`LOG_NAME_MAX`, the value reduced
#: to its type and size -- and the set stops growing at the cap, so the ledger
#: holds a fixed amount whatever the config holds. It is never pruned: a process
#: says each pair once, and the row's own ``disabledReason`` is the durable
#: signal the table shows for as long as the value is wrong. Shared by every
#: reader of :func:`mcp_entry_is_muted` that also reports -- the listing and the
#: agent rebuild -- so one config error is said once between them.
_invalid_disabled_reported: set[tuple[str, str]] = set()

#: Whether the ledger has said, once, that it is full.
_invalid_disabled_cap_reached = False


def invalid_disabled_flag(spec: Any) -> tuple[bool, Any]:
    """Whether ``spec["disabled"]`` is present and not a boolean, and the value.

    The one inspection of the raw value. Every decision -- list, offer for sync,
    mount, spawn -- goes through :func:`mcp_entry_is_muted`, which is
    FAIL-CLOSED: absent or a literal ``False`` is enabled, ``True`` and every
    non-boolean (``"false"``, ``1``, ``"yes"``, ``null``) is disabled. This helper
    only tells the operator WHY such an entry is off, through
    :func:`warn_invalid_disabled` and the listing's ``disabled_reason``: the fix
    is to repair the value, not to flip a switch. A reader that took the value as
    "enabled" would launch a server the user tried to silence; one that read it as
    "disabled" only where it lists and not where it launches would show a
    Disabled row the sessions still start. Neither happens while there is one
    predicate.
    """
    if not isinstance(spec, dict):
        return False, None
    flag = spec.get("disabled")
    return ("disabled" in spec and not isinstance(flag, bool)), flag


def describe_disabled_flag(value: Any) -> str:
    """The TYPE and size of a non-boolean ``disabled`` -- ``str(len 5)`` -- never its text.

    A log line is retained (the dashboard ring, every log subscriber's stream),
    so quoting the value would carry an arbitrary amount of config into both for
    one bad key. The type names what was written in the shape the operator
    recognises (``str(len 5)`` is a quoted ``"false"``; ``NoneType`` is ``null``;
    ``int`` is ``0`` or ``1``), and the row's own copy already points at the file
    to fix, so nothing the operator needs is lost with the text.
    """
    kind = type(value).__name__
    if isinstance(value, Sized):
        return f"{kind}(len {len(value)})"
    return kind


def _log_name(name: str) -> str:
    """*name* as a WARNING line carries it: whole when it fits, else a prefix.

    The cut says how much is missing, so the operator knows the line is not the
    whole name, and the suffix is a number -- bounded whatever was cut.
    """
    if len(name) <= LOG_NAME_MAX:
        return name
    return f"{name[:LOG_NAME_MAX]}...(+{len(name) - LOG_NAME_MAX} chars)"


def warn_invalid_disabled(name: str, value: Any, where: str) -> None:
    """WARNING once per (server, value shape) for a ``disabled`` that is not a boolean.

    ``"disabled": "false"`` (a string) is not a boolean, so the launch predicate
    (:func:`mcp_entry_is_muted`) reads it FAIL-CLOSED: the server is listed
    Disabled, never offered for sync, never mounted by the agent rebuild and never
    spawned, until the value is a real ``true`` or ``false``. The line says so out
    loud because the operator may have written ``"false"`` and meant "on": a server
    they believe is on is off until the value is a real boolean, and the row's own
    copy points at the same file.

    Called from every path that reads the shared configs and strips or withholds
    on the predicate -- the dashboard listing and the agent rebuild -- so the
    operator learns why from whichever path ran first, and once: the ledger is
    the set of pairs already said, capped at :data:`INVALID_DISABLED_LEDGER_CAP`,
    after which one line, in total, says the rest are read as disabled but not
    named. Everything retained and everything logged is bounded: the name is cut
    to :data:`LOG_NAME_MAX`, the value is its type and size
    (:func:`describe_disabled_flag`), *where* is the caller's scope label.
    """
    global _invalid_disabled_cap_reached
    key = (_log_name(name), describe_disabled_flag(value))
    if key in _invalid_disabled_reported:
        return
    if len(_invalid_disabled_reported) >= INVALID_DISABLED_LEDGER_CAP:
        if not _invalid_disabled_cap_reached:
            _invalid_disabled_cap_reached = True
            logger.warning(
                "MCP config: more than %d servers carry a non-boolean 'disabled'; each is "
                "read as DISABLED, but the rest are not reported individually. Fix the "
                "reported ones and restart to see the others.",
                INVALID_DISABLED_LEDGER_CAP,
            )
        return
    _invalid_disabled_reported.add(key)
    logger.warning(
        "MCP server %r: 'disabled' in the %s config is %s, not a boolean; it is read "
        "as DISABLED -- an invalid value never launches a server. Set it to true or "
        "false.",
        key[0],
        where,
        key[1],
    )


# Override hook + accessor, NOT a resolved constant. Binding `kiro_home()` at
# import time freezes whichever home was active when this module was first
# imported, so conftest's isolation fixture -- which runs after collection has
# already imported it -- cannot redirect it, and a test that reached
# `clean_stale_managed_mcp()` without patching would rewrite the operator's REAL
# mcp.json. Keeping the module-level name means the existing
# `monkeypatch.setattr(mcp_cleanup, "_KIRO_MCP_JSON", tmp)` call sites still work.
_KIRO_MCP_JSON: Path | None = None  # explicit override hook, None = live


def _kiro_mcp_json() -> Path:
    if _KIRO_MCP_JSON is not None:
        return _KIRO_MCP_JSON
    return kiro_home() / "settings" / "mcp.json"


# Managed servers whose command is the kirocrew binary itself.
# Only these are affected by install-method path changes.
# Ordered tuples (not sets) so consumers that iterate — e.g. `kirocrew
# doctor`'s MCP probe — get a deterministic order.
#
# The split matters to every consumer that asks "should this server be in the
# spec?". An ALWAYS_ON server missing from an agent spec is a broken install; an
# OPT_IN one is an assignable set that most agents are simply not granted, so
# demanding its presence — or minting an auto-approve grant for it — would undo
# the assignment. Membership here must track the ``opt_in`` flags in
# ``agent._MANAGED_MCP_SERVERS``; a ratchet test pins the two together.
ALWAYS_ON_BIN_MCP_SERVERS = (
    "kirocrew-cron",
    "kirocrew-core",
    "kirocrew-computer",
)
OPT_IN_BIN_MCP_SERVERS = (
    "kirocrew-dashboard",
    "kirocrew-work",
    "kirocrew-crew-log",
    "kirocrew-debug",
    "kirocrew-panel",
)

# Every managed-binary server name, regardless of how it reaches a spec. This is
# the cleanup view: Kiro Crew never legitimately writes any of them into the
# user's global mcp.json, so a stray entry is purgeable either way.
KIROCREW_BIN_MCP_SERVERS = ALWAYS_ON_BIN_MCP_SERVERS + OPT_IN_BIN_MCP_SERVERS

# Crew's own control plane: the servers a session mounts whether or not its spec
# names them, re-derived from the managed source on every spawn so a stale
# hand-edited command cannot cost a session the tools it needs to report back to
# its channel at all. Re-derived, not read from the spec, is also what keeps them
# out of an admin registry filter: they are the host's own process, not a
# third-party server the admin's catalog governs.
#
# NOT ``ALWAYS_ON_BIN_MCP_SERVERS``, which also carries ``kirocrew-computer``:
# that set answers "must this be in an agent spec", and this one answers "does a
# session get this regardless". Naming the same tuple twice is how two decisions
# that must agree drift apart, so both consumers read this one:
# ``acp.session_mcp`` re-exports it for the spec ceiling and the codex identity
# projection, and ``mcp_gateway.session_servers`` reads it for the broker-stub
# ceiling. It lives in this leaf because the gateway reaches it without putting
# ``kiro_crew.agent`` on the daemon's boot path, the same reason
# ``gatewayd.CONTROL_PLANE_BACKENDS`` reads its set from here.
CONTROL_PLANE_SERVERS = ("kirocrew-core", "kirocrew-cron")

# Every managed-binary server name KiroCrew is responsible for removing from
# the user's global mcp.json (Kiro Crew never legitimately writes these there).
#
# ALWAYS_ON only, and there is no ownership-proven exception. This purge exists
# to reclaim entries an OLDER INSTALL METHOD wrote to the global file — but an
# opt-in server is never written there by any version of Kiro Crew, since the
# only way it is ever granted is by hand. So no legitimate residue can exist
# under that name, and anything found there is necessarily the user's own: to be
# left alone, not reclaimed on a technicality about how it happens to be spelled.
STALE_MANAGED_MCP_SERVERS = frozenset(ALWAYS_ON_BIN_MCP_SERVERS)


def _predecessor_mcp_names() -> frozenset[str]:
    """Managed server names owned by an agent an edition supersedes.

    An edition that replaces a predecessor registers it as an import source, and
    the same descriptor names the MCP servers that agent managed. Those entries
    point at a runtime that is gone, so they are unambiguously stale — but only
    the edition knows they exist, which is why this is read from the registry
    rather than listed here.
    """
    # Deferred, NOT top-level: cli.py imports this module eagerly, and
    # onboarding_import pulls yaml/croniter/vector_memory/embeddings. Hoisting it
    # puts that weight on every CLI invocation, which
    # test_cli_lazy_imports.py::test_cli_import_does_not_load_heavy_modules
    # fails on by design. Same annotation as the call site in agent.py.
    from kiro_crew.onboarding_import import predecessor_mcp_names  # noqa: PLC0415

    return predecessor_mcp_names()


def _stale_mcp_binaries() -> frozenset[str]:
    """Launcher basenames whose leftover MCP entries an edition declared purgeable."""
    # Deferred for the same boot-weight reason as _predecessor_mcp_names above.
    from kiro_crew.onboarding_import import stale_mcp_binaries  # noqa: PLC0415

    return stale_mcp_binaries()


# The argv token the deleted Playwright MCP proxy was registered with. An entry
# still carrying it spawns `kirocrew mcp-playwright-proxy`, a subcommand this
# release removed, so kiro-cli hits ModuleNotFoundError on EVERY session until
# the entry goes. Browsing is gone either way (there is no proxy any more), so
# the only question is whether the operator also gets a crash on every session.
_DELETED_PROXY_ARGV_TOKEN = "mcp-playwright-proxy"


def _invokes_deleted_playwright_proxy(spec: object) -> bool:
    """True if a server spec launches the Playwright MCP proxy this release deleted.

    Matched on the ARGV token, never on the server name. The canonical name was
    ``playwright-mcp``, but that is also what an operator's OWN Playwright server
    is called, and purging by name would delete a server Kiro Crew never wrote --
    the same trap ``_invokes_superseded_agent`` exists to avoid.
    """
    if not isinstance(spec, dict):
        return False
    args = spec.get("args", [])
    if not isinstance(args, list):
        return False
    return any(isinstance(a, str) and a == _DELETED_PROXY_ARGV_TOKEN for a in args)


#: Executable suffixes a Windows console script carries (`...\Scripts\<name>.exe`).
#: Only these are stripped when matching a launcher basename, so an unrelated
#: dotted command is never collapsed onto a registered name.
_WINDOWS_LAUNCHER_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".com")


def _invokes_superseded_agent(spec: object) -> bool:
    """True if a server spec's command is the launcher of a superseded agent.

    Catches stale entries a rename left behind whose *name* is not in the managed
    set — e.g. a leftover ``npm:@playwright/mcp`` proxy pointing at the old
    runtime. Keyed on the command basename so it matches both a bare name and an
    absolute path, and never matches a genuine playwright server (which runs
    ``npx``/``node``). The names come from the import-source registry, so the
    core does not hardcode any superseded product name.
    """
    if not isinstance(spec, dict):
        return False
    cmd = spec.get("command", "")
    if not isinstance(cmd, str) or not cmd:
        return False
    binaries = _stale_mcp_binaries()
    if not binaries:
        return False
    # mcp.json is cross-platform data (a config written on Windows may be read
    # anywhere), so split on BOTH separators rather than the host's os.sep —
    # os.path.basename only honors the local separator.
    leaf = re.split(r"[\\/]", cmd)[-1]
    stem = leaf.casefold()
    # Strip only a real executable suffix. Splitting on the FIRST dot instead
    # would collapse any dotted command onto its first segment, so a server
    # launched by an unrelated `<name>.<something>` binary would match a
    # registered `<name>` and be deleted.
    for suffix in _WINDOWS_LAUNCHER_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem in binaries


def clean_stale_managed_mcp() -> list[str]:
    """Remove stale managed-binary MCP entries from ``~/.kiro/settings/mcp.json``.

    Runs from explicit setup (``kirocrew setup``) and once on first gateway
    start (marker-guarded by ``run_first_run_setup``) — never on every startup,
    which would violate the "KiroCrew owns only the agent file" boundary.

    Removes two classes of stale entry left in the user's global provider
    config; genuine user-installed servers are never touched:

    * **By name** — ``kirocrew-cron`` / ``kirocrew-core`` (written there by an
      older install method; Kiro Crew now keeps these in the agent file), plus
      the managed servers of any agent an edition declares it supersedes.
    * **By command** — any server whose command is a superseded agent's launcher,
      e.g. a leftover ``npm:@playwright/mcp`` proxy pointing at its old runtime.

    Returns names of removed servers (empty list on no-op or error).
    """
    if not _kiro_mcp_json().is_file():
        return []
    try:
        data = json.loads(_kiro_mcp_json().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return []
    removed = sorted(
        name
        for name, spec in servers.items()
        if name in STALE_MANAGED_MCP_SERVERS
        or name in _predecessor_mcp_names()
        or _invokes_superseded_agent(spec)
        or _invokes_deleted_playwright_proxy(spec)
    )
    if not removed:
        return []
    for name in removed:
        del servers[name]
    try:
        _kiro_mcp_json().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info("Removed stale managed MCP entries from kiro mcp.json: %s", removed)
    except OSError:
        logger.debug("Could not clean kiro mcp.json", exc_info=True)
        return []
    return removed


def _ref_server(ref: str) -> str:
    """The server name a ``@server`` / ``@server/tool`` ref addresses.

    One spelling, matching ``agent_capabilities._materialize`` and
    ``apps.bridges``: strip the sigil, then take everything before the first
    ``/``. A second spelling would let the per-tool form (``@notion/search``,
    ``@notion/*``) resolve to a different server here than it does there, and the
    two would disagree about which refs are dangling.
    """
    return ref[1:].split("/", 1)[0]


def prune_dangling_tool_refs(
    config: dict,
    *,
    declared: Collection[str] = (),
    declared_grants: Collection[str] | None = None,
) -> list[str]:
    """Drop ``tools``/``allowedTools`` refs naming a server the map does not hold.

    ``kiro-cli`` mounts what ``mcpServers`` declares, so a ``@ref`` to a name
    absent from that map mounts nothing. The invariant is stated twice elsewhere
    in-tree -- :func:`purge_deleted_proxy_from_config` strips refs alongside its
    own deletes so kiro-cli does not try to mount a server absent from the map,
    and ``agent_capabilities._materialize`` filters ``allowedTools`` on exactly
    this test -- and this is where it holds for the assembled agent config, whose
    server map several passes narrow without touching the ref lists (the
    resolution pass replaces ``mcpServers`` wholesale, dropping unresolvable
    servers by OMISSION; the locked app re-merge ``del``\\ etes app entries whose
    app is not confirmed enabled). One reconcile over the FINAL map covers every
    such pass, including one added later.

    A dangling ``allowedTools`` ref is the one that costs something: that list is
    the path that never reaches the PreToolUse gate, so the grant sits on the NAME
    and a server added under it inherits an auto-approval nobody granted for it.

    *declared* names servers whose absence from the map this caller EXPECTS and a
    later pass reverses, so they are not leftovers. Two such classes exist in the
    rebuild, and both are unrecoverable if their refs are dropped, because an
    existing config deliberately never re-adds a template ref: a server whose
    command fails to resolve on this pass (a binary missing from this rebuild's
    PATH), and a gated-off shipped server whose entry is withheld while its
    ``tools`` ref is retained by design. The resolution case also reaches the
    per-tool grant, which nothing re-emits -- the rebuild's ref sync only ever
    writes whole-server ``@alias`` refs. Refs outlive one bad PATH; a lost grant
    does not.

    A ``@`` name in :data:`RESERVED_TOOL_NAMESPACES` addresses a kiro namespace
    rather than a server, so it is never in the map and is never a leftover.
    ``@builtin`` is the one kiro's configuration reference lists, and it carries
    the whole built-in tool surface plus the ``tool_search`` loader, so reading it
    as a dangling server ref would unmount all of that on every rebuild.

    Fails safe in both directions: a ``mcpServers`` that is not a dict yields no
    readable verdict, so nothing is dropped, and a non-string or sigil-less entry
    is left for the passes that own it.

    Mutates *config* in place. Returns the refs removed, first occurrence order.

    *declared_grants* is the same kind of set for ``allowedTools`` alone, and it
    exists because the two lists fail in opposite directions. Keeping a mount ref
    too long costs a mount attempt against a name that holds nothing; dropping one
    can unmount a server for good, since an existing config never re-adds a
    template ref. Keeping a GRANT too long hands the next server on that name an
    auto-approval nobody granted, on the one list that never reaches the
    PreToolUse gate; dropping one costs an approval a human can give again. So a
    caller that is UNSURE whether a name is still owned should name it in
    *declared* and leave it out of *declared_grants*: the mount survives the doubt
    and the grant does not. Defaults to *declared*, so a caller with no such
    doubt passes one set and both lists read it.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    _base = set(servers) | RESERVED_TOOL_NAMESPACES
    known = {
        "tools": _base | set(declared),
        "allowedTools": _base | set(declared if declared_grants is None else declared_grants),
    }
    dropped: list[str] = []
    unmounted: list[str] = []
    for key in ("tools", "allowedTools"):
        lst = config.get(key)
        if not isinstance(lst, list):
            continue
        kept = []
        for ref in lst:
            if (
                not isinstance(ref, str)
                or not ref.startswith("@")
                or _ref_server(ref) in known[key]
            ):
                kept.append(ref)
                continue
            if key == "tools" and ref not in unmounted:
                unmounted.append(ref)
            if ref not in dropped:
                dropped.append(ref)
        config[key] = kept
    if dropped:
        # A ``tools`` removal takes a tool OUT of the agent's surface, and the
        # shipped ``agent.log_level`` default is WARNING, so recording that at
        # INFO hides it from the operator who then has to debug a tool that
        # stopped being offered. The ref named a server the final map does not
        # hold, so the tool was already unreachable -- but a MISREAD absence
        # here is unrecoverable, because an existing config never re-adds a
        # template ref, and that is the case worth seeing without first raising
        # the log level. A grant-only removal is already in SEL as
        # ``mcp_auto_approve_revoked``, so it stays at INFO.
        logger.log(
            logging.WARNING if unmounted else logging.INFO,
            "Pruned dangling MCP refs from agent config (no such server in mcpServers): %s",
            dropped,
        )
    return dropped


def purge_deleted_proxy_from_config(config: dict) -> list[str]:
    """Drop any MCP server entry whose argv invokes the deleted Playwright proxy.

    Runs on EVERY rebuild of the agent config (not behind the first-run
    marker) because the entry can be re-injected from ~/.kiro/crew/mcp.json
    by the merge passes that precede this call.  Matched by ARGV token, never
    by server name, so an operator's own ``playwright-mcp`` server whose
    command does not invoke the deleted subcommand is left untouched.

    Mutates *config* in place.  Returns server names that were removed.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    to_remove = [name for name, spec in servers.items() if _invokes_deleted_playwright_proxy(spec)]
    for name in to_remove:
        del servers[name]
    if to_remove:
        # Also strip @refs from tools/allowedTools so kiro-cli does not try
        # to mount a server absent from the map. Both spellings
        # the server owns go: the bare ``@name`` and the per-tool
        # ``@name/tool`` -- a per-tool grant left in ``allowedTools`` is an
        # auto-approval on the deleted proxy's name, and that list never
        # reaches the PreToolUse gate. Bounded by the ``/`` so a
        # prefix-sharing name (``@namex``) is untouched. Rebuilt in place
        # rather than ``list.remove`` so a duplicated ref cannot survive.
        for key in ("tools", "allowedTools"):
            lst = config.get(key)
            if isinstance(lst, list):
                for name in to_remove:
                    ref = f"@{name}"
                    owned = f"{ref}/"
                    lst[:] = [
                        t
                        for t in lst
                        if t != ref and not (isinstance(t, str) and t.startswith(owned))
                    ]
        logger.info(
            "Purged deleted-proxy MCP entries from agent config: %s",
            to_remove,
        )
    return to_remove
