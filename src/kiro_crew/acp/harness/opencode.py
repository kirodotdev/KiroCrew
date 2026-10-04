"""OpenCode, launched one process per session by ``AcpClient``.

OpenCode serves ACP from its OWN binary: ``opencode acp``. There is no npm adapter to
resolve and no Node floor to satisfy -- the published package ships an executable --
so its binary comes off the shared self-served ladder
(:func:`kiro_crew.acp.launch.resolve_self_served_launch`), from its
``ACP_BACKEND_LAUNCH`` row.

What makes the launch this host's own is the permission routing. Crew's tool gate
ENFORCES opencode, so the launch carries the OS credential mask, seeds the asking
posture into the child's environment (``OPENCODE_CONFIG_CONTENT``), and then reads the
harness's OWN resolved configuration back from a short-lived child wrapped in the same
sandbox with the same mask -- a session that cannot establish the asking posture is
refused before its process starts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess as subprocess_mod
from typing import Any, Collection

from kiro_crew import acp_tool_gate
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, ProcessSession, SpawnContext, SpawnPlan
from kiro_crew.acp.harness_tool_names import (
    MAX_HARNESS_CONFIG_MCP_SERVERS,
    MAX_HARNESS_TOOL_NAME_LEN,
    opencode_rewrites_name,
)
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.agent_sdk.backends import ACP_BACKEND_OPENCODE, launch_for
from kiro_crew.sandbox import scrub_agent_subprocess_env, wrap_argv, wrap_argv_async

# The client's logger name: these lines have always been filed under it.
logger = logging.getLogger("kiro_crew.acp.client")

__all__ = ["OPENCODE_BIN", "OPENCODE_INSTALL_COMMAND", "OpencodeLaunch"]

# The launch facts live in this harness's ``ACP_BACKEND_LAUNCH`` row, and every shared
# path reads them from there: the resolver, the spawn, the install probe and the
# driver seams. Only the two names a reader in THIS module still needs are bound here,
# for ``_opencode_readback_remedy`` below -- the routing remedy names the binary and its
# installer in prose.
_OPENCODE_LAUNCH = launch_for(ACP_BACKEND_OPENCODE)
OPENCODE_BIN = _OPENCODE_LAUNCH.binary
# The channel Crew's permission routing travels down: inline config JSON in the
# child's environment. It is what makes the routing seed session-scoped -- nothing
# is written into a checked-out repository -- and it resolves ABOVE the project's
# own config file, verified on this harness by resolving a project that declares
# ``permission: "allow"`` and reading ``ask`` back out. The read-back is still what
# establishes the guarantee; this is only how the value gets there.
_ENV_OPENCODE_CONFIG_CONTENT = "OPENCODE_CONFIG_CONTENT"
OPENCODE_INSTALL_COMMAND = _OPENCODE_LAUNCH.install_command
# The subcommand that prints the RESOLVED configuration -- every source merged, the
# way the ACP server itself resolves it. Reading it back is what separates this
# harness's routing from a declared-but-unverified seed.
_OPENCODE_CONFIG_READBACK_ARGS = ("debug", "config")
# Bounded so a wedged harness cannot hold the spawn open: the read-back is a
# short-lived child, measured at ~2.3s on a loaded dev desktop.
_OPENCODE_READBACK_TIMEOUT_S = 30.0


def _opencode_readback_remedy() -> str:
    """What an operator does when the harness's config cannot be read back at all."""
    return (
        f"Run '{OPENCODE_BIN} {' '.join(_OPENCODE_CONFIG_READBACK_ARGS)}' in the "
        "session's working directory to see what fails, and reinstall with "
        f"'{OPENCODE_INSTALL_COMMAND}' if the command itself is broken."
    )


def _opencode_uniform_permission(raw: object) -> object:
    """Collapse this harness's resolved permission to ONE value when it is uniform.

    The harness normalizes a bare ``"ask"`` into a rule map (``{"*": "ask"}``), so
    the read-back has to compare shapes rather than strings. A map whose every rule
    carries the same value IS that value.

    The harness also checks its rules in order and lets the LAST match win, and a
    ``"*"`` key matches every tool and every pattern. So a map whose last entry is
    ``"*": "ask"`` asks for every call, whatever the entries before it say. That is
    the shape the seed produces over a lower source's per-tool rule: the sources are
    merged key by key, so ``"bash": "allow"`` from the operator's global config keeps
    its place and the seed's ``"*"`` is appended after it -- measured on opencode
    1.18.30 and 1.18.32, where such a session asks before running ``bash``. It is
    accepted ONLY when no entry before it denies anything: a ``deny`` the trailing
    ``"*"`` outranks is a rule the operator wrote that would silently stop holding,
    so that map stays refused.

    Any other MIXED map is not reduced and not accepted: one tool left permissive is
    one tool whose calls never reach the host gate, so it is returned as its own JSON
    spelling for the refusal to name.

    ``None`` for anything else, which the gate reads as "the setting is not there".
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and raw:
        values = {value for value in raw.values() if isinstance(value, str)}
        if len(values) == 1 and len(raw) == len(
            [value for value in raw.values() if isinstance(value, str)]
        ):
            return values.pop()
        last_key, last_value = list(raw.items())[-1]
        if last_key == "*" and last_value == "ask" and not _opencode_rules_deny(raw):
            return "ask"
        return json.dumps(raw, sort_keys=True)
    return None


def _opencode_rules_deny(raw: dict) -> bool:
    """True when any rule in *raw* -- top level or one tool's pattern map -- denies."""
    for value in raw.values():
        if value == "deny":
            return True
        if isinstance(value, dict) and "deny" in value.values():
            return True
    return False


def _opencode_config_mcp_server_names(resolved: dict) -> tuple[tuple[str, ...], str]:
    """The MCP server names the harness's resolved config mounts, and any issue.

    opencode mounts these from its own user and project config, beside the
    servers Crew places on the session, and names their tools only by a fused
    ``<server>_<tool>`` title in which a character such as ``.`` became ``_``.
    Knowing the exact names lets that title be split back to the spelling a
    spec hook's ``mcp__server__tool`` matcher is written in. Only a name opencode
    rewrites is kept: any other one the every-``_`` split already reproduces.

    Bounded rather than truncated: a name over :data:`MAX_HARNESS_TOOL_NAME_LEN`,
    or more than :data:`MAX_HARNESS_CONFIG_MCP_SERVERS` rewritten names, is an
    issue the caller refuses the session on, because a name left out would let
    a deny hook written with its exact spelling miss the call.
    """
    servers = resolved.get("mcp")
    if not isinstance(servers, dict):
        return (), ""
    names = [name for name in servers if isinstance(name, str) and name]
    if any(len(name) > MAX_HARNESS_TOOL_NAME_LEN for name in names):
        return (), (
            f"its config mounts an MCP server whose name is over "
            f"{MAX_HARNESS_TOOL_NAME_LEN} characters"
        )
    rewritten = [name for name in names if opencode_rewrites_name(name)]
    if len(rewritten) > MAX_HARNESS_CONFIG_MCP_SERVERS:
        return (), (
            f"its config mounts {len(rewritten)} MCP servers whose names it rewrites, "
            f"more than the {MAX_HARNESS_CONFIG_MCP_SERVERS} Crew can match hooks against"
        )
    return tuple(rewritten), ""


def _opencode_config_mcp_servers_remedy() -> str:
    """What an operator does when opencode's config mounts too many MCP servers."""
    return (
        "Remove MCP servers from opencode's own config, or rename them to letters, "
        "digits, '_' and '-' only, then start a new session."
    )


def _opencode_agent_permissions(resolved: dict, setting_key: str) -> list[tuple[str, object]]:
    """Every per-agent permission the harness's resolved config carries, reduced.

    This harness lets a config source set ``agent.<name>.<setting_key>``, and that
    value applies to the named agent IN PLACE of the top-level one -- the seed does
    not reach it, because the seed writes only the top-level key. A session whose
    top-level value reads ``ask`` while ``agent.build`` reads ``allow`` therefore
    passes the top-level check and runs its build tools past the host gate. So
    every agent entry is walked, not just the top-level key. Legacy ``mode``
    entries are folded into ``agent`` by the harness's own resolution before the
    document is printed, so walking ``agent`` covers both spellings.

    Each entry is returned as ``(agent_name, reduced_value)`` in the same shape
    :func:`_opencode_uniform_permission` gives the top-level key, so the SAME gate
    decides both. Agents that carry no permission of their own are skipped: they
    inherit the top-level value, which the caller has already checked.
    """
    agents = resolved.get("agent")
    if not isinstance(agents, dict):
        return []
    found: list[tuple[str, object]] = []
    for name, entry in sorted(agents.items()):
        if not isinstance(entry, dict) or setting_key not in entry:
            continue
        found.append((str(name), _opencode_uniform_permission(entry.get(setting_key))))
    return found


def _glob_fullmatch(value: str, pattern: str) -> bool:
    """Whether *pattern* matches all of *value*: ``*`` any run, ``?`` one character.

    Every other character is literal. The classic two-pointer walk with one saved
    ``*`` position: it never backtracks past the last ``*``, so it runs in at most
    ``len(value) * len(pattern)`` steps, however many ``*`` and ``?`` the pattern
    holds. A regex built from the same pattern can take exponential time.
    """
    v = p = 0
    star = -1
    mark = 0
    while v < len(value):
        if p < len(pattern) and pattern[p] == "*":
            star, mark = p, v
            p += 1
        elif p < len(pattern) and pattern[p] in ("?", value[v]):
            v += 1
            p += 1
        elif star >= 0:
            mark += 1
            v = mark
            p = star + 1
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def _opencode_wildcard_match(value: str, pattern: str) -> bool:
    """The harness's own wildcard test, transcribed from opencode 1.18.30.

    Both sides have ``\\`` turned into ``/``; in the pattern, ``*`` matches any run
    and ``?`` any one character, everything else is literal, and a trailing `` *``
    also matches nothing after the space. Anchored at both ends.

    The harness compiles this to a regex. Here it is a backtrack-free glob walk
    (:func:`_glob_fullmatch`), because the pattern comes from the operator's opencode
    config: a regex built from ``*?*?*?...`` would take seconds per call while holding
    the GIL, which stalls the whole gateway.
    """
    value = value.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    if _glob_fullmatch(value, pattern):
        return True
    # The trailing " *" also matches the bare prefix, with no space after it.
    return pattern.endswith(" *") and _glob_fullmatch(value, pattern[:-2])


def _opencode_rules(raw: object) -> list[tuple[str, str, str]]:
    """One resolved ``permission`` value as the harness's ordered rule list.

    ``(permission, pattern, action)`` per rule, in the order the harness reads them:
    a string value is one rule for every pattern, a map value is one rule per pattern
    it lists. A bare string for the whole setting is the same as ``{"*": value}``.
    """
    if isinstance(raw, str):
        return [("*", "*", raw)]
    rules: list[tuple[str, str, str]] = []
    if not isinstance(raw, dict):
        return rules
    for key, value in raw.items():
        if isinstance(value, str):
            rules.append((str(key), "*", value))
        elif isinstance(value, dict):
            rules.extend(
                (str(key), str(pattern), action)
                for pattern, action in value.items()
                if isinstance(action, str)
            )
    return rules


def _opencode_denied_in(tool_id: str, rules: list[tuple[str, str, str]]) -> bool:
    """Whether the harness hides *tool_id* under *rules*.

    The harness's own test: the LAST rule whose permission key matches the tool
    decides, and it hides the tool only when that rule denies every pattern.
    """
    last = None
    for rule in rules:
        if _opencode_wildcard_match(tool_id, rule[0]):
            last = rule
    return last is not None and last[1] == "*" and last[2] == "deny"


def _opencode_unenforced_denies(
    resolved: dict, setting_key: str, deny_rules: Collection[str]
) -> frozenset[str]:
    """The seeded deny rules the harness's RESOLVED config does not put in force.

    Evaluated for the top-level setting and again for every agent that carries its
    own, because an agent's rules are appended after the top-level ones and so can
    outrank them. A rule that loses in either place is not in force for a session
    that may run as that agent.
    """
    if not deny_rules:
        return frozenset()
    top = _opencode_rules(resolved.get(setting_key))
    contexts = [top]
    agents = resolved.get("agent")
    if isinstance(agents, dict):
        for entry in agents.values():
            if isinstance(entry, dict) and setting_key in entry:
                contexts.append(top + _opencode_rules(entry.get(setting_key)))
    return frozenset(
        tool_id
        for tool_id in deny_rules
        if not all(_opencode_denied_in(tool_id, rules) for rules in contexts)
    )


def _opencode_native_servers_holding(resolved: dict, rules: Collection[str]) -> list[str]:
    """Servers opencode mounts from its OWN config whose tool ids *rules* could name.

    *resolved* is the harness's resolved config; its ``mcp`` map is every server
    opencode mounts itself, apart from Crew's ``session/new`` array. A rule id is
    ``sanitize(server) + "_" + sanitize(tool)``, so a server whose sanitized name plus
    ``_`` starts a rule id is reported. Prefix matching can over-report a server
    whose name is a prefix of another's; that only refuses more, never less.
    """
    from kiro_crew.providers.mirrors.opencode import opencode_tool_id

    mounts = resolved.get("mcp")
    if not rules or not isinstance(mounts, dict):
        return []
    found = []
    for name in sorted(str(n) for n in mounts):
        prefix = opencode_tool_id(name, "")
        if any(rule.startswith(prefix) for rule in rules):
            found.append(name)
    return found


def _opencode_seeded_deny_rules(config_content: str, setting_key: str) -> tuple[str, ...]:
    """The deny rules Crew wrote into *config_content*, in seed order.

    Read back out of the seed itself rather than handed in separately, so the
    read-back judges exactly what the harness was given.
    """
    try:
        seed = json.loads(config_content)
    except ValueError:
        return ()
    value = seed.get(setting_key) if isinstance(seed, dict) else None
    if not isinstance(value, dict):
        return ()
    return tuple(str(key) for key, action in value.items() if key != "*" and action == "deny")


def _opencode_routing_config(backend: str, deny_rules: Collection[str] = ()) -> str:
    """The inline harness config that makes this session ask, as one env value.

    *deny_rules* are the tool ids the session's projection asks the harness itself to
    deny (``SessionProjection.harness_deny_rules``).

    MERGED over an ambient value rather than replacing it: an operator who set
    the variable themselves keeps every key they chose, and only the permission
    setting the host gate depends on is Crew's. A value that is not a JSON
    object is left out of the merge and said so in the log -- silently dropping
    an operator's config would be worse, and honouring an unparseable one is not
    possible.
    """
    setting_key, value = acp_tool_gate.permission_setting_for(backend)
    merged: dict[str, Any] = {}
    ambient = os.environ.get(_ENV_OPENCODE_CONFIG_CONTENT) or ""
    if ambient:
        try:
            parsed = json.loads(ambient)
        except ValueError:
            logger.warning(
                "%s is not valid JSON, so this session's harness config carries only "
                "Crew's permission routing.",
                _ENV_OPENCODE_CONFIG_CONTENT,
            )
        else:
            if isinstance(parsed, dict):
                merged.update(parsed)
            else:
                logger.warning(
                    "%s is not a JSON object, so this session's harness config carries "
                    "only Crew's permission routing.",
                    _ENV_OPENCODE_CONFIG_CONTENT,
                )
    rules = tuple(deny_rules)
    if rules and value == "ask":
        # Each switched-off tool AFTER the "*", because the harness lets the last
        # matching rule win; the read-back checks that order survived the merge.
        merged[setting_key] = {"*": value, **{rule: "deny" for rule in rules}}
    else:
        merged[setting_key] = value
    return json.dumps(merged)


def _verify_opencode_routing(
    session: ProcessSession, backend: str, argv: list[str], config_content: str
) -> tuple[str, str]:
    """Read the harness's OWN resolved permission back, and report any issue.

    This is the half that makes the routing VERIFIED rather than seeded. The
    read-back runs the harness's own config resolution -- every source merged,
    the way the ACP server itself merges them -- so what comes back is the value
    the session will actually use, not the value Crew hoped it had written. A
    precedence change in a future release therefore surfaces as a refusal here
    instead of as a session that silently stops asking.

    What it does NOT establish is that the harness HONOURS the setting per tool
    call; that is the harness's own contract, and no client-side read can prove
    it. The scope is the precondition, and the precondition is the part that was
    missing.

    Returns ``("", "")`` when the required value is in force, else the reason
    and the remedy that can clear it. The two travel together because they are
    decided together: a read-back that could not RUN or could not be PARSED is a
    harness problem, and its remedy is to run the harness's own command and fix
    the install; a value that resolved to something other than the required one
    is a config problem, and its remedy is the gate's -- remove the source that
    outranks the seed. Handing the config remedy to an exec failure would tell
    the operator to edit something that cannot clear the refusal.

    *argv* arrives ALREADY SANDBOX-WRAPPED, and that is a security property
    rather than a convenience: this child is the same third-party binary the
    session spawns, resolving config out of the session work dir, and config
    resolution on this harness can load project plugins. An unwrapped child
    would read the very credential homes the mask exists to deny it, moments
    before the masked session spawn. The caller wraps because it is the async
    side and has the resolved mask in hand.

    Records the config's rewritten MCP server names on *session*, where the
    turn-time hook matcher reads them. Blocking (spawns a short-lived child);
    callers run it off the loop.
    """
    # The session's own credential repair and PATH augmentation, owned by the client
    # and read there at call time, so the read-back's environment is built by the
    # same bindings as the spawn's.
    from kiro_crew.acp.client import _resolve_spawn_env, augmented_path

    setting_key, _required = acp_tool_gate.permission_setting_for(backend)
    # The SAME environment the spawn builds, in both directions. The per-session
    # overlay (``_extra_env``, a cron job's ``env`` among its sources) is applied
    # because this harness reads its config LOCATION from the environment --
    # ``XDG_CONFIG_HOME``, ``OPENCODE_CONFIG`` -- so a read-back without the overlay
    # would resolve a different set of config files than the session it vouches
    # for, and a permissive value in the session's set would pass unseen. And the
    # SAME scrub, for the same reason: this is a foreign harness binary, and the
    # gateway's own environment carries channel tokens, cloud secrets and an agent
    # socket that no harness may see. The read-back runs BEFORE the spawn, so
    # inheriting the environment verbatim would hand a child every one of them a
    # few lines ahead of the code that strips them.
    env = scrub_agent_subprocess_env(
        _resolve_spawn_env({**os.environ, **session._extra_env}, kiro_api_key=False)
    )
    env["PATH"] = augmented_path(env.get("PATH", ""))
    env[_ENV_OPENCODE_CONFIG_CONTENT] = config_content
    try:
        completed = subprocess_mod.run(
            argv,
            cwd=session._spawn_work_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_OPENCODE_READBACK_TIMEOUT_S,
        )
    except (OSError, subprocess_mod.SubprocessError) as exc:
        return (
            f"the resolved configuration could not be read back ({exc})",
            _opencode_readback_remedy(),
        )
    if completed.returncode != 0:
        # The child's own fault, same as the pi read-back: an operator reading this
        # refusal learns both that the harness failed and which recognised fault it
        # hit.
        detail = f"exit {completed.returncode}"
        detail = launch_mod._readback_detail_with_diagnosis(detail, completed.stderr)
        return (
            f"the resolved configuration could not be read back ({detail})",
            _opencode_readback_remedy(),
        )
    # The harness prints a banner before the document, so the object is found
    # rather than assumed to start at byte zero.
    start = completed.stdout.find("{")
    resolved: object = None
    if start >= 0:
        try:
            resolved = json.loads(completed.stdout[start:])
        except ValueError:
            resolved = None
    if not isinstance(resolved, dict):
        return (
            "the resolved configuration could not be parsed",
            _opencode_readback_remedy(),
        )
    config_servers, servers_issue = _opencode_config_mcp_server_names(resolved)
    if servers_issue:
        return servers_issue, _opencode_config_mcp_servers_remedy()
    session._opencode_config_mcp_servers = config_servers
    seeded_denies = _opencode_seeded_deny_rules(config_content, setting_key)
    # Which of Crew's deny rules the harness's own resolution keeps in force. A rule
    # that lost is normally not a refusal: the projection withholds its server.
    session._opencode_denies_unenforced = _opencode_unenforced_denies(
        resolved, setting_key, seeded_denies
    )
    # ...unless opencode mounts that server from its OWN config too. Withholding the
    # array element does not reach that mount, so the switched-off tool would stay
    # callable there: refuse the session instead.
    lost = frozenset(getattr(session, "_session_harness_deny_rules", ()) or ()) - (
        frozenset(seeded_denies) - session._opencode_denies_unenforced
    )
    native = _opencode_native_servers_holding(resolved, lost)
    # A server the projection could not keep narrowed at all (a tool id the harness
    # re-maps to a builtin, so no rule exists) is matched by NAME: opencode registers
    # a native mount under the name its config gives it.
    raw_mounts = resolved.get("mcp")
    mounts: set[str] = {str(n) for n in raw_mounts} if isinstance(raw_mounts, dict) else set()
    # Kept so a later re-projection on this spawn is checked against the same native
    # mounts (see refuse_native_mount_of_unhonoured).
    session._opencode_native_mounts = frozenset(mounts)
    unhonoured: frozenset[str] = getattr(session, "_session_mcp_unhonoured", frozenset())
    native = sorted(set(native) | (mounts & unhonoured))
    if native:
        return _native_mount_refusal(session, native)
    top_level = resolved.get(setting_key)
    if seeded_denies and isinstance(top_level, dict):
        # Crew's own deny rules are judged above, so the routing check sees the value
        # the operator's sources and the seed's "*" leave behind.
        top_level = {k: v for k, v in top_level.items() if k not in seeded_denies}
    observed = _opencode_uniform_permission(top_level)
    issue = acp_tool_gate.seeded_setting_issue(backend, session._scrub_observed(observed))
    if issue:
        return issue, acp_tool_gate.remediation_for(backend)
    # The top-level value is in force; now the per-agent overrides, which the
    # seed does not reach and which replace it for the agent they name. One
    # permissive agent is one agent whose tool calls never reach the host gate,
    # so the first such entry refuses the session and names the agent.
    for agent_name, agent_observed in _opencode_agent_permissions(resolved, setting_key):
        issue = acp_tool_gate.seeded_setting_issue(backend, session._scrub_observed(agent_observed))
        if issue:
            return (
                f"agent {session._scrub_observed(agent_name)!r} overrides it: {issue}",
                acp_tool_gate.remediation_for(backend),
            )
    return "", ""


def _native_mount_refusal(session: ProcessSession, native: Collection[str]) -> tuple[str, str]:
    """The refusal for a narrowed server opencode mounts from its own config."""
    names = ", ".join(repr(session._scrub_observed(n)) for n in native)
    return (
        f"a switched-off tool on {names} is not denied in the resolved config, and "
        "opencode mounts that server from its own config, where withholding it from "
        "Crew's array cannot reach",
        "remove that tool's key from your opencode config, or stop mounting the server " "there",
    )


def settle_opencode_denies(session: ProcessSession, config_content: str) -> frozenset[str]:
    """Record which deny rules are in force after the read-back; return the lost ones.

    In force means SEEDED and not outranked. Derived from the seed Crew actually
    handed the harness, never from the rules the projection asked for, so a routing
    value that leaves the rules out cannot be read as having put them in. The lost
    rules are the ones the projection asked for that are not in force; the caller
    re-projects, and each one's server is withheld whole.
    """
    setting_key = acp_tool_gate.permission_setting_for(ACP_BACKEND_OPENCODE)[0]
    seeded = frozenset(_opencode_seeded_deny_rules(config_content or "", setting_key))
    unenforced: frozenset[str] = getattr(session, "_opencode_denies_unenforced", frozenset())
    session._opencode_denies_in_force = seeded - unenforced
    asked = frozenset(getattr(session, "_session_harness_deny_rules", ()) or ())
    return asked - session._opencode_denies_in_force


def refuse_native_mount_of_unhonoured(session: ProcessSession) -> None:
    """Refuse the session if a server this projection withholds is mounted natively.

    Withholding the array element cannot reach a server opencode mounts from its
    own config, so a narrowed server there would keep its switched-off tool. The
    native mounts are the ones the read-back resolved for this spawn.
    """
    native = sorted(
        getattr(session, "_opencode_native_mounts", frozenset())
        & getattr(session, "_session_mcp_unhonoured", frozenset())
    )
    if not native:
        return
    issue, remedy = _native_mount_refusal(session, native)
    try:
        acp_tool_gate.enforce_runtime_routing(ACP_BACKEND_OPENCODE, issue, remedy=remedy)
    except acp_tool_gate.ToolGateUnroutable as exc:
        raise AcpToolGateUnroutable(str(exc)) from None


class OpencodeLaunch(ProcessAdapter):
    """OpenCode: a self-served binary whose asking posture is seeded and read back."""

    backend = ACP_BACKEND_OPENCODE

    def __init__(self) -> None:
        # The routing seed the read-back verified, carried to the child's environment.
        self._config_content = ""

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the binary, warm the session's MCP array, then establish the routing."""
        session = ctx.session
        assert session is not None, "opencode is launched for one session"
        opencode_bin, argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
            self.backend
        )
        # Translate the agent spec into this session's MCP array HERE, for exactly
        # the reason the claude adapter does it in its own launch: the translation
        # reads disk, and doing it at the shared session/new call site would put an
        # executor hop and a new failure mode on EVERY backend's construction path,
        # kiro-cli included (harness-parity H13). No ordering constraint of claude's
        # applies -- this harness's array is not conditional on Crew owning a
        # permission file, because its routing is seeded on OPENCODE_CONFIG_CONTENT
        # and then read back out of the harness itself below, so a session that
        # cannot establish the asking posture is refused rather than run.
        await session._prepare_session_mcp()
        # The refuse-then-mask preflight, keyed on the routing question rather than
        # on this harness's identity: it is ENFORCED, so the OS credential mask is
        # the compensating control for the passive reads ACP v1 cannot make it ask
        # about, and several wrap_argv paths return without applying it.
        #
        # FIRST, before the read-back below: that read-back runs a child of this
        # harness, and on a host where the mask cannot be applied the session is
        # refused anyway -- so refusing here means no foreign binary starts at all,
        # rather than one starting and then being told the session is off.
        hidden = await launch_mod._run_preflight_bounded(
            launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
        )
        expose = acp_tool_gate.adapter_expose_files(self.backend, hidden)
        # The routing seed, and the READ-BACK that is what this harness's Routing
        # member promises. OFF-LOOP: the read-back spawns a short-lived child, and a
        # synchronous spawn on the gateway loop is the stall this path guards
        # against everywhere else.
        self._config_content = _opencode_routing_config(
            self.backend, getattr(session, "_session_harness_deny_rules", ())
        )
        # Wrapped in the SAME sandbox, with the SAME credential mask, as the session
        # spawn. The read-back runs the harness's own binary, and this harness
        # resolves its configuration by reading the work dir -- which can load a
        # project's plugins -- so an unwrapped read-back would hand a third-party
        # binary the credential homes the mask denies it, moments before the masked
        # spawn.
        readback_argv, readback_cleanup = await wrap_argv_async(
            [opencode_bin, *_OPENCODE_CONFIG_READBACK_ARGS],
            mode=ctx.sandbox_mode,
            strip_python_env=True,
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
            _prepare=wrap_argv,
        )
        try:
            routing_issue, routing_remedy = await asyncio.to_thread(
                _verify_opencode_routing,
                session,
                self.backend,
                readback_argv,
                self._config_content,
            )
        finally:
            # wrap_argv leaves a launcher/profile file the child consumes at exec.
            # This child has exited by now, so the file is removed here rather than
            # leaking one per session start for the gateway's life (the same
            # contract the spawn's own artifact keeps, which this must not touch).
            if readback_cleanup:
                await asyncio.to_thread(launch_mod._unlink_readback_launcher, readback_cleanup)
        if routing_issue:
            # Refused before the first prompt: this harness asks per tool call only
            # while the setting holds, so a session that cannot establish it is a
            # session where none of Crew's tool controls execute.
            #
            # Translated to the ACP-layer type, and that is not cosmetic:
            # ``ensure_ready`` catches ``AcpToolGateUnroutable``, so a bare gate
            # exception would escape both of its handlers and skip its failed-spawn
            # cleanup -- leaving the refusal untyped and the failed spawn unreaped.
            try:
                acp_tool_gate.enforce_runtime_routing(
                    self.backend,
                    routing_issue,
                    remedy=routing_remedy,
                )
            except acp_tool_gate.ToolGateUnroutable as exc:
                raise AcpToolGateUnroutable(str(exc)) from None
        lost = settle_opencode_denies(session, self._config_content)
        if lost:
            # A deny rule the projection asked for is not in force: the seed left it
            # out, or the harness's resolved config outranks it. Its tool would stay
            # listed, so re-project with only the rules that held: the server it
            # narrows is withheld whole.
            logger.warning(
                "opencode: %d per-tool deny rule(s) are not in force in the resolved "
                "config, so the servers they narrow are withheld this session: %s",
                len(lost),
                ", ".join(sorted(lost)),
            )
            session._session_mcp_cache = await asyncio.to_thread(
                session._resolve_session_mcp_servers
            )
            # The re-projection re-reads the spec, so it can narrow a server the
            # read-back never judged. Hold the result to the same native mounts.
            refuse_native_mount_of_unhonoured(session)
        return SpawnPlan(
            argv=argv,
            spawn_label=spawn_label,
            stderr_label=stderr_label,
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Apply the seed the read-back verified, unconditionally.

        The merge in :func:`_opencode_routing_config` already preserved every key the
        operator set, and this value is the one the host gate depends on.
        """
        if self._config_content:
            env[_ENV_OPENCODE_CONFIG_CONTENT] = self._config_content
