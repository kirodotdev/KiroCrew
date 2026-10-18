"""Kiro Crew's own managed MCP servers as discovery sees them.

The CLI subcommand and tool module behind each managed server, which of them
a pooled gateway backend may serve across sessions, the in-process read of a
managed server's declared tools, and the repair of a managed server's recorded
command after the install it points at is gone.

:mod:`kiro_crew.mcp_discovery` stays the import path and the patch surface for
every name here; that module's comment lists the owners.
"""

from __future__ import annotations

import importlib
import logging
import os

logger = logging.getLogger("kiro_crew.mcp_discovery")


# Managed server name -> the ``kirocrew`` CLI subcommand that serves it.
_MANAGED_SERVER_SUBCOMMANDS = {
    "kirocrew-core": "mcp-core",
    "kirocrew-cron": "mcp-cron",
    "kirocrew-computer": "mcp-computer",
    "kirocrew-dashboard": "mcp-dashboard",
    "kirocrew-work": "mcp-work",
    "kirocrew-crew-log": "mcp-crew-log",
    "kirocrew-debug": "mcp-debug",
    "kirocrew-panel": "mcp-panel",
    "kirocrew-guide": "mcp-guide",
    "kirocrew-mate": "mcp-mate",
}
_MANAGED_SERVER_NAMES = set(_MANAGED_SERVER_SUBCOMMANDS)

# Managed server name -> the module whose ``_list_tools()`` declares its tools.
# These are the SAME functions the stdio shim serves ``tools/list`` from, so
# calling them in-process returns exactly what a spawn would have returned.
_MANAGED_SERVER_TOOL_MODULES = {
    "kirocrew-core": "kiro_crew.mcp_core",
    "kirocrew-cron": "kiro_crew.mcp_cron",
    "kirocrew-computer": "kiro_crew.mcp_computer",
    "kirocrew-dashboard": "kiro_crew.mcp_dashboard",
    "kirocrew-work": "kiro_crew.mcp_work",
    "kirocrew-crew-log": "kiro_crew.mcp_crew_log",
    "kirocrew-debug": "kiro_crew.mcp_debug",
    "kirocrew-panel": "kiro_crew.mcp_panel",
    "kirocrew-guide": "kiro_crew.mcp_guide",
    "kirocrew-mate": "kiro_crew.mcp_mate",
}


#: Managed servers that advertise ``kirocrew.caller-identity`` AND are safe to
#: classify shareable -- the ones consuming the per-call caller block gatewayd
#: injects instead of reading identity from their own process, whose behaviour
#: for a caller the gateway CANNOT name is also pooling-safe (refusal, or a
#: correctly separated namespace). A name absent from this set reads as
#: session-bound: either it does not consume the block at all, or it is in
#: ``_MANAGED_SERVERS_ADVERTISING_BUT_WITHHELD`` below.
#:
#: ``kirocrew-computer`` qualifies by the second route, and the separation is
#: NEGOTIATED rather than assumed. It tells its unnamed callers apart by the
#: per-connection nonce, so a daemon minting none must not serve it pooled:
#: ``mcp_gateway.gatewayd.REGISTERED_CAPABILITIES`` advertises ``tenant_nonce``
#: and ``mcp_gateway.stub.must_degrade_nonce_blind`` execs a per-session backend
#: when a serving daemon omits it. That is what makes the entry safe even where
#: ``mcp_gateway/manager.py`` adopted a daemon older than this code.
#:
#: A NAME SET rather than a runtime read of each module's own constant. Reading the
#: constant means ``importlib.import_module`` on the request path, which executes
#: package code the gateway does not otherwise run -- the package directory is
#: writable by the same uid the agent runs as, so on an editable checkout every
#: MCP-servers request becomes an execution point for whatever was written there.
#: The sibling in-process tool read accepts that cost only on the fallback path
#: where the sandbox could not have confined a spawn anyway; a classification
#: consulted on every render must not widen it to hosts where the sandbox works.
#:
#: The drift this trades for is already covered:
#: ``test/test_mcp_managed_caller_identity.py`` drives each server's real serve
#: entry point and asserts this set matches the ``advertise_caller_identity``
#: argument actually handed to the shim. That check imports the modules in the
#: TEST process, where running package code is the point rather than a hazard.
_MANAGED_SERVERS_CALLER_AWARE: frozenset[str] = frozenset(
    {
        "kirocrew-core",
        "kirocrew-cron",
        "kirocrew-computer",
        "kirocrew-dashboard",
        "kirocrew-work",
        "kirocrew-crew-log",
        "kirocrew-debug",
        "kirocrew-panel",
        "kirocrew-guide",
        "kirocrew-mate",
    }
)

#: Managed servers that ADVERTISE the capability but are deliberately withheld
#: from ``_MANAGED_SERVERS_CALLER_AWARE`` — advertising is necessary for the
#: not-session-bound classification but not sufficient. The set is empty: every
#: managed server that advertises also meets the second condition.
#:
#: The mechanism stays because that second condition is easy to miss. A name
#: belongs here when its pooled attribution is right for every caller the gateway
#: CAN name, yet its UNNAMED co-tenants are not provably separated on every
#: gateway generation this code can meet. The gap matters because this set feeds
#: ``managed_server_is_session_bound``, which feeds the shareability verdict,
#: which ``mcp_gateway/seed.py`` turns into a CONFIG WRITE (``recommend_share``
#: -> ``apply_seed``): a name wrongly absent from here can switch sharing ON for
#: an operator who never chose it. And the daemon that then serves those shared
#: frames need not be the one this code shipped with —
#: ``mcp_gateway/manager.py`` ADOPTS whatever healthy daemon already holds the
#: socket, so a gatewayd that outlived a package upgrade keeps running and
#: injects no nonce (which is why ``REGISTERED_CAPABILITIES`` exists).
#:
#: Two ways to satisfy the condition, one of each in the tree.
#: ``kirocrew-dashboard`` REFUSES an unidentified caller, so it is safe to
#: classify shareable whatever the daemon's generation. ``kirocrew-computer``
#: NEGOTIATES instead: it separates unnamed co-tenants by the per-connection
#: nonce, the daemon attests that it mints one, and
#: ``mcp_gateway.stub.must_degrade_nonce_blind`` execs a per-session backend when
#: the attestation is missing — so a nonce-blind gateway cannot serve that server
#: pooled at all. Taking the separation on trust instead is the mistake this set
#: exists to hold.
#:
#: ``test_mcp_managed_caller_identity.py`` pins this so an entry can neither
#: silently persist past its reason nor silently widen.
_MANAGED_SERVERS_ADVERTISING_BUT_WITHHELD: frozenset[str] = frozenset()


def managed_server_is_session_bound(name: str) -> bool:
    """True when *name* is one of ours AND resolves identity from its process.

    The shareability assessment needs this WITHOUT a handshake: on a host where
    the probe cannot spawn (any Windows host, macOS >= 26) there is no
    ``initialize`` response to read the capability from, and before the first
    probe cycle there is none yet either.

    False for anything not managed by Kiro Crew: a third-party server's identity
    handling is not knowable from here, which is what the pre-flight measures.
    """
    if name not in _MANAGED_SERVER_NAMES:
        return False
    return name not in _MANAGED_SERVERS_CALLER_AWARE


def _managed_tools_in_process(name: str) -> list[str] | None:
    """Tool names for a managed server, read WITHOUT spawning it.

    A managed server's tool list is a static declaration in this package —
    ``mcp_core._list_tools()`` and friends, the very functions the stdio shim
    answers ``tools/list`` from. Spawning a child to ask ourselves what we
    ourselves declare is pure overhead, and it made the listing depend on a
    sandbox backend: ``sandboxed_spawn_argv`` fail-closes where none exists (any
    Windows host, macOS >= 26), so the built-in tools showed as 0 on the dashboard
    even though kiro-cli was serving them fine.

    Reading them in-process removes that dependency outright — no subprocess, so
    no sandbox to be unavailable and no unsandboxed-execution question to answer.
    That is the whole point: the alternative designs either require an
    ``agent.sandbox_allow_unsandboxed_exec`` opt-in for a read-only listing, or
    exempt an agent-writable package from the sandbox. This needs neither.

    This caller keeps only NAMES, so it prefers a module's ``_list_tool_names()``
    when it offers one: a names-only read that never assembles descriptions, so a
    description reaching for a live value (a directory scan, a config read) never
    runs here. That is what replaced the per-builder ``get_running_loop`` skips —
    the names-only path simply does not reach those reads, rather than each
    builder detecting this caller and opting out. A module without the names-only
    entry point falls back to extracting names from its full ``_list_tools()``;
    among the managed set only ``kirocrew-core`` carries live-valued
    descriptions, and it provides ``_list_tool_names()``.

    Imported lazily: these modules pull in the validation/artifacts graph, which
    cannot be imported at this module's import time (circular). The names-only
    read is a pure read of the static tool set — no I/O of its own, no side
    effects, and cheap enough for a discovery cycle.

    Returns ``None`` when *name* is not managed or the read fails, so the caller
    falls back to the ordinary spawn-and-handshake path rather than reporting a
    wrong answer. An EMPTY list is a real result, not a failure:
    ``mcp_computer._list_tools()`` returns ``[]`` by design while the keystone
    enable is off — which is also what a spawned probe reports.
    """
    module_name = _MANAGED_SERVER_TOOL_MODULES.get(name)
    if module_name is None:
        return None
    try:
        module = importlib.import_module(module_name)
        names_only = getattr(module, "_list_tool_names", None)
        if callable(names_only):
            tool_names = names_only()
            if isinstance(tool_names, list):
                return [n for n in tool_names if isinstance(n, str) and n]
            return None
        tools = module._list_tools()
    except Exception:
        logger.debug("in-process tool read failed for %s; will probe", name, exc_info=True)
        return None
    if not isinstance(tools, list):
        return None
    return [n for t in tools if isinstance(t, dict) and (n := t.get("name"))]


# Cached resolved (command, args) — avoids subprocess.run on every list_servers() call.
_resolved_managed_invocation: dict[str, tuple[str, list[str]]] = {}


def _cached_managed_invocation(name: str) -> tuple[str, list[str]] | None:
    """The cached invocation for *name*, or ``None`` once its command is gone.

    The cache outlives the install it was resolved from: an update that prunes
    the previous version directory leaves an absolute command here that no
    longer exists, and serving it would keep relaunching ``kirocrew-core`` /
    ``kirocrew-cron`` from the pruned tree. A vanished absolute command is
    evicted so the caller re-resolves against the current install.
    """
    invocation = _resolved_managed_invocation.get(name)
    if invocation is None:
        return None
    command = invocation[0]
    if os.path.isabs(command) and not os.path.isfile(command):
        _resolved_managed_invocation.pop(name, None)
        return None
    return invocation


def _fix_stale_managed_command(name: str, spec: dict) -> None:
    """Re-resolve command + args for a managed MCP server to the running install.

    Always re-resolves — the stored path may exist as a file/symlink but still
    crash at runtime (e.g. a path from a previous install). The running gateway
    knows how to invoke itself.

    Delegates to :func:`kiro_crew.agent._kirocrew_mcp_invocation`, the single
    source of truth for the managed invocation. That handles every layout:
    a standalone ``bin/kirocrew`` (POSIX) / ``Scripts\\kirocrew.exe`` (Windows
    pip install) console script when one resolves, the Windows bundle's
    ``bin\\kirocrew.cmd`` shim (unwrapped to ``<root>\\python.exe -s -P -m
    kiro_crew <sub>``), and otherwise the ``<interpreter> [-s] -P -m kiro_crew
    <sub>`` fallback. Both ``command`` AND ``args`` are rewritten — the fallback
    needs its optional isolation prefix plus ``["-P", "-m", "kiro_crew", <sub>]``,
    so re-resolving the command
    alone (the old behavior) silently dropped the args and spawned a bare
    ``kirocrew`` that isn't on PATH (Windows: ``command not found: kirocrew``;
    the built-in cron/core tools then never load).
    """
    subcommand = _MANAGED_SERVER_SUBCOMMANDS.get(name)
    if subcommand is None:
        return
    invocation = _cached_managed_invocation(name)
    if invocation is None:
        try:
            from kiro_crew.agent import _kirocrew_mcp_invocation  # circular import

            invocation = _kirocrew_mcp_invocation(subcommand)
        except Exception:
            logger.debug("managed MCP invocation resolution failed", exc_info=True)
            return
        _resolved_managed_invocation[name] = invocation
    command, args = invocation
    if spec.get("command") != command or spec.get("args") != args:
        logger.info(
            "Re-resolved %s invocation: %s %s → %s %s",
            name,
            spec.get("command"),
            spec.get("args"),
            command,
            args,
        )
        spec["command"] = command
        spec["args"] = args


def _is_first_party_managed_argv(
    name: str, command: str | None, args: list[str], env: dict[str, str] | None
) -> bool:
    """True when (*command*, *args*, *env*) IS the self-derived managed invocation.

    Gates the ``first_party_fixed_argv`` carve-out on the probe spawn. The
    managed NAME alone is deliberately not enough: only agent-config entries are
    force-re-resolved through :func:`_fix_stale_managed_command`, so a row
    introduced from an mcp.json scope could carry user-config command text under
    a managed name. Requiring equality against the freshly re-resolved
    invocation (:func:`kiro_crew.agent._kirocrew_mcp_invocation`, the single
    source of truth) makes "the argv is derived inside this package" a checked
    property rather than an assumption — any customized command or args compares
    unequal and keeps the full fail-close + opt-in behavior.

    *env* must equal the package-derived managed env too
    (:func:`kiro_crew.agent._managed_mcp_env` — ``{}`` on a default install, the
    ``KIROCREW_HOME`` pin under an override home). ``probe_server`` merges the
    spec's ``env`` into the child environment, and env is an execution vector in
    its own right (``LD_PRELOAD``/``LD_LIBRARY_PATH`` change WHAT CODE runs for
    the same argv), so a spec carrying any key this package did not derive is
    not first-party — it keeps the full fail-close + opt-in behavior.
    """
    subcommand = _MANAGED_SERVER_SUBCOMMANDS.get(name)
    if subcommand is None:
        return False
    invocation = _cached_managed_invocation(name)
    try:
        # circular import: agent is loaded during package init
        from kiro_crew.agent import _kirocrew_mcp_invocation, _managed_mcp_env

        expected_env = _managed_mcp_env()
        if invocation is None:
            invocation = _kirocrew_mcp_invocation(subcommand)
            _resolved_managed_invocation[name] = invocation
    except Exception:
        # Fail toward "not first-party": the spawn then keeps the ordinary
        # fail-close path, which is the safe direction.
        logger.debug("managed MCP invocation resolution failed", exc_info=True)
        return False
    expected_command, expected_args = invocation
    # Refuse the interpreter fallback with or without its conditional ``-s``:
    # neither form removes the child's CWD from sys.path, so a planted
    # ``kiro_crew/`` tree in the gateway's cwd could still shadow the installed
    # package and run unconfined. Only a resolved console-script binary, whose
    # entrypoint imports from its own install, qualifies. Keep recognizing both
    # fallback forms defensively.
    if expected_args[:2] == ["-m", "kiro_crew"] or expected_args[:3] == [
        "-s",
        "-m",
        "kiro_crew",
    ]:
        return False
    return (
        command == expected_command
        and list(args) == list(expected_args)
        and dict(env or {}) == expected_env
    )
