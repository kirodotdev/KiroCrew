"""MiMo Code, launched one process per session by ``AcpClient``.

MiMo Code serves ACP from its own binary, ``mimo acp`` (no npm adapter, no Node
floor), resolved off the shared self-served ladder
(:func:`kiro_crew.acp.launch.resolve_self_served_launch`) from its
``ACP_BACKEND_LAUNCH`` row. Crew's tool gate ENFORCES this harness: the launch
carries the OS credential mask, seeds the asking posture (``MIMOCODE_CONFIG_CONTENT``)
and reads the resolved config back (``mimo debug config``) from a child in the same
sandbox and mask; a session that cannot establish the posture is refused before
its process starts.

What the read-back cannot see is handled around it, each measured on 0.1.15:

* ``MIMOCODE_DANGEROUSLY_SKIP_PERMISSIONS`` and ``MIMOCODE_AUTO_APPROVE_DELETE``
  auto-approve irreversible deletes without changing the printed ``permission``,
  and ``MIMOCODE_PERMISSION`` merges above the inline document: all three stripped;
* ``MIMOCODE_DISABLE_CLAUDE_IMPORT`` is pinned, or each start copies every Claude
  Code transcript under ``~/.claude/projects`` into the harness's database;
* ``MIMOCODE_HOME`` moves the credential where the auth declaration cannot
  re-anchor it, so it refuses the session;
* the harness's credential-less loopback listener takes
  ``POST /permission/skip-all``, which any same-user process can send to switch the
  instance to auto-allow after the read-back ran. So the client's tripwire stops
  the harness on a call that completes unasked (``AcpClient._tripwire_pi_gate``),
  except the tools its ``HARNESS_UNASKED_TOOL_IDS`` row names and edits under its
  memory directory, which never ask. Unasked tools with a switch (``cron``, and the
  experimental ``workflow``, ``session``, ``exec``) are kept out at the spawn
  (``_MIMOCODE_STRIPPED_ENV``, ``_MIMOCODE_PINNED_ENV``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess as subprocess_mod
import sys
from typing import TYPE_CHECKING, Any, Collection, NamedTuple

from kiro_crew import acp_tool_gate
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, ProcessSession, SpawnContext, SpawnPlan
from kiro_crew.acp.harness_tool_names import (
    MAX_HARNESS_CONFIG_MCP_SERVERS,
    MAX_HARNESS_TOOL_NAME_LEN,
    mimocode_rewrites_name,
)
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.agent_sdk.backends import ACP_BACKEND_MIMOCODE, launch_for
from kiro_crew.sandbox import scrub_agent_subprocess_env, wrap_argv, wrap_argv_async

if TYPE_CHECKING:
    from kiro_crew.mcp_discovery import McpServerInfo

# The client's logger name: these lines have always been filed under it.
logger = logging.getLogger("kiro_crew.acp.client")

__all__ = ["MIMO_BIN", "MIMO_INSTALL_COMMAND", "MimoCodeLaunch"]

# The launch facts live in this harness's ``ACP_BACKEND_LAUNCH`` row, and every shared
# path reads them from there: the resolver, the spawn, the install probe and the
# driver seams. Only the names a reader in THIS module needs are bound here, for the
# remedies below, which name the binary and its installer in prose.
_MIMOCODE_LAUNCH = launch_for(ACP_BACKEND_MIMOCODE)
MIMO_BIN = _MIMOCODE_LAUNCH.binary
# The channel Crew's permission routing travels down: inline config JSON in the
# child's environment. It is what makes the routing seed session-scoped -- nothing
# is written into a checked-out repository -- and the harness merges it above the
# project's own config file. ``OPENCODE_CONFIG_CONTENT`` is not read at all. The
# read-back is still what establishes the guarantee; this is only how the value
# gets there.
_ENV_MIMOCODE_CONFIG_CONTENT = "MIMOCODE_CONFIG_CONTENT"
MIMO_INSTALL_COMMAND = _MIMOCODE_LAUNCH.install_command
# The subcommand that prints the RESOLVED configuration -- every source merged, the
# way the ACP server itself resolves it. Reading it back is what separates this
# harness's routing from a declared-but-unverified seed.
_MIMOCODE_CONFIG_READBACK_ARGS = ("debug", "config")
# The flag that prints the resolved binary's own version. Every launch reads it
# back fresh and refuses a version outside ``_MIMOCODE_TESTED_VERSION`` BEFORE the
# read-back runs: the read-back's parser and the session spawn are measured only
# against that line, and a PATH refresh onto another line must refuse rather than
# let an unmeasured binary decide how the permission gate reads.
_MIMOCODE_VERSION_ARGS = ("--version",)
# The line the whole launch has been measured against, named by major AND minor:
# a 0.x release may break between minors. Widen it only after re-measuring the
# spawn, the permission read-back, the MCP mount and the tripwire against the new
# line.
_MIMOCODE_TESTED_VERSION = (0, 1)
# The version child is bounded like the read-back: a binary that hangs on the
# flag must refuse the launch, not stall the gateway loop that awaited it.
_MIMOCODE_VERSION_TIMEOUT_S = 15.0
# Bounded so a wedged harness cannot hold the spawn open: the read-back is a
# short-lived child.
_MIMOCODE_READBACK_TIMEOUT_S = 30.0
# Removed from every child of this harness, the read-back's and the version
# probe's included, because each changes the asking posture in a way the read-back
# cannot see.
_MIMOCODE_STRIPPED_ENV = (
    "MIMOCODE_PERMISSION",
    "MIMOCODE_DANGEROUSLY_SKIP_PERMISSIONS",
    "MIMOCODE_AUTO_APPROVE_DELETE",
    # Each turns on builtin tools that run without asking: the umbrella adds
    # ``workflow`` (and ``codesearch``, ``lsp``), the next two ``session`` and
    # ``workflow``, and the last ``exec`` on a model outside the GPT toolset
    # (``flag/flag.ts``, ``tool/registry.ts``; ``mimo debug agent build`` under
    # the umbrella lists ``workflow``).
    "MIMOCODE_EXPERIMENTAL",
    "MIMOCODE_EXPERIMENTAL_ORCHESTRATOR",
    "MIMOCODE_EXPERIMENTAL_WORKFLOW_TOOL",
    "MIMOCODE_ENABLE_EXEC_TOOL",
)
# Set on every child. ``cron`` is on by default and schedules prompts unasked
# (``tool/cron.ts``): the first cron pin drops it from the toolset (per
# ``mimo debug agent build``), the second stops the scheduler a persisted job would
# start (``session/cron-bridge.ts``, read each tick). ``MIMOCODE_CODEX_MODE=false``,
# read before the GPT-model check (``tool/gpt.ts``, ``usesCodexMode``), keeps every
# model off the GPT toolset (``exec`` and ``wait`` only), where every tool runs inside
# ``exec`` (``tool/tool-script-ref.ts``) and a nested call asks under an id no
# ``tool_call`` names (``<call>:<seq>``, ``tool/tool-script.ts``), so a nested shell
# call would reach the gate with no command; it also keeps out ``apply_patch`` and
# ``view_image``.
_MIMOCODE_PINNED_ENV = (
    ("MIMOCODE_DISABLE_CLAUDE_IMPORT", "1"),
    ("MIMOCODE_EXPERIMENTAL_CRON", "false"),
    ("MIMOCODE_DISABLE_CRON", "1"),
    ("MIMOCODE_CODEX_MODE", "false"),
)
# Variables whose presence refuses the session, with the reason each one does.
_MIMOCODE_REFUSED_ENV = (
    (
        "MIMOCODE_HOME",
        "it moves MiMo Code's credential to a path Kiro Crew cannot fence",
    ),
)


def _apply_mimocode_env(env: dict[str, str]) -> None:
    """Strip and pin this harness's variables on a child environment, in place."""
    for name in _MIMOCODE_STRIPPED_ENV:
        env.pop(name, None)
    for name, value in _MIMOCODE_PINNED_ENV:
        env[name] = value


def _mimocode_refused_env_issue(env: dict[str, str]) -> str:
    """The first refused variable *env* sets, as a refusal reason; ``""`` if none."""
    for name, why in _MIMOCODE_REFUSED_ENV:
        if env.get(name):
            return f"{name} is set, and {why}"
    return ""


_MIMOCODE_REFUSED_ENV_REMEDY = (
    "Unset MIMOCODE_HOME in the gateway's environment, or select another harness "
    "under Settings → Agent Harness and start a new chat."
)


def _mimocode_child_env(session: ProcessSession) -> dict[str, str]:
    """The environment every child of this harness starts from, before its strip and pin.

    The session's overlay and credential repair, then the scrub, then the PATH
    augmentation: the version probe, the read-back and the memory root below all
    read it, so each sees the variables the session's own child will see.
    """
    # The session's own credential repair and PATH augmentation, owned by the client
    # and read there at call time, so every child's environment is built by the same
    # bindings as the spawn's.
    from kiro_crew.acp.client import _resolve_spawn_env, augmented_path

    env = scrub_agent_subprocess_env(
        _resolve_spawn_env(
            {**os.environ, **session._agent_env_for_spawn(), **session._extra_env},
            kiro_api_key=False,
        )
    )
    env["PATH"] = augmented_path(env.get("PATH", ""))
    return env


def _mimocode_memory_root(env: dict[str, str]) -> str:
    """The directory under which this harness's edit tools write without asking.

    ``Global.Path.data`` joined with ``memory`` (``tool/external-directory.ts``,
    ``askEditUnlessMemory``; ``tool/apply_patch.ts``), where the data root is
    ``$XDG_DATA_HOME/mimocode``, or ``~/.local/share/mimocode`` without it.
    ``MIMOCODE_HOME``, the other relocation, refuses the session before this runs.
    """
    data_home = env.get("XDG_DATA_HOME") or ""
    if not os.path.isabs(data_home):
        home = env.get("HOME") or os.path.expanduser("~")
        data_home = os.path.join(home, ".local", "share")
    return os.path.join(data_home, "mimocode", "memory")


def _mimocode_tested_label() -> str:
    """The tested line as prose, ``0.1``."""
    return ".".join(str(part) for part in _MIMOCODE_TESTED_VERSION)


def _mimocode_readback_remedy() -> str:
    """What an operator does when the harness's config cannot be read back at all."""
    return (
        f"Run '{MIMO_BIN} {' '.join(_MIMOCODE_CONFIG_READBACK_ARGS)}' in the "
        "session's working directory to see what fails -- it prints the resolved config "
        "unmasked, provider keys included, so keep its output private -- and reinstall "
        f"with '{MIMO_INSTALL_COMMAND}' if the command itself is broken."
    )


def _mimocode_version_remedy() -> str:
    """What an operator does when the resolved binary is not the tested line."""
    return (
        f"Install a tested release with '{MIMO_INSTALL_COMMAND}', or set "
        f"{_MIMOCODE_LAUNCH.bin_env_var} to a binary of version {_mimocode_tested_label()}."
    )


#: The first dotted version pair in a harness banner. The banner's exact wording is
#: the binary's to choose, so the scan looks for the number rather than anchoring the
#: line: "0.1.15" and a build-prefixed string both carry the pair this gate reads.
_VERSION_SHAPE = re.compile(r"(?:^|\D)(\d+)\.(\d+)")


def _parse_mimocode_version(version_output: str) -> tuple[int, int] | None:
    """The ``(major, minor)`` *version_output* carries, or ``None`` when unreadable.

    An empty output, a banner with no dotted pair, and any other unreadable shape
    answer ``None`` -- the caller treats unreadable as outside the tested range, so
    the gate's failure direction is closed.
    """
    match = _VERSION_SHAPE.search(version_output or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


#: Deliberately NO cache on this answer. The binary resolution next door caches per
#: gateway life, and a verdict cached the same way -- keyed on the resolved path --
#: would keep reporting the OLD version after an in-place upgrade put a new binary
#: behind the same path: the gate would then wave through the very launch this
#: check exists to refuse. The read-back below already starts one short-lived child
#: per session; the probe is the same order of cost, paid for an answer that is
#: always current.


def _mimocode_reported_version(session: ProcessSession, argv: list[str]) -> tuple[int, int] | None:
    """Run the sandbox-wrapped version child and answer the version it reports.

    Blocking, so callers run it off the loop. Its environment is the read-back's
    (same foreign binary, same trust level) with no config seed. ``None`` for any
    failure (no run, non-zero exit, no readable version), which the caller refuses.
    """
    env = _mimocode_child_env(session)
    _apply_mimocode_env(env)
    try:
        completed = subprocess_mod.run(
            argv,
            cwd=session._spawn_work_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_MIMOCODE_VERSION_TIMEOUT_S,
        )
    except (OSError, subprocess_mod.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _parse_mimocode_version(completed.stdout)


async def _mimocode_version_refusal(
    session: ProcessSession,
    mimocode_bin: str,
    *,
    sandbox_mode: str,
    hidden: tuple[str, ...],
    expose: tuple[str, ...],
) -> tuple[str, str]:
    """Refuse a resolved binary whose version is not the line this launch was
    measured against.

    The read-back and the spawn are measured only against
    ``_MIMOCODE_TESTED_VERSION``; a PATH refresh onto another line would otherwise
    move the session silently, with no failure naming the upgrade. ``("", "")`` when
    in range, else the reason and the remedy. Every launch probes fresh, wrapped in
    the read-back's sandbox and mask, off the loop.
    """
    version_argv, cleanup = await wrap_argv_async(
        [mimocode_bin, *_MIMOCODE_VERSION_ARGS],
        mode=sandbox_mode,
        strip_python_env=True,
        extra_hidden_dirs=hidden,
        extra_expose_files=expose,
        _prepare=wrap_argv,
    )
    try:
        reported = await asyncio.to_thread(_mimocode_reported_version, session, version_argv)
    finally:
        # The same contract as the read-back's artifact: wrap_argv leaves a
        # launcher file the child consumes at exec, reclaimed as soon as the
        # child has exited -- refusal paths included.
        if cleanup:
            await asyncio.to_thread(launch_mod._unlink_readback_launcher, cleanup)
    if reported == _MIMOCODE_TESTED_VERSION:
        return "", ""
    line = _mimocode_tested_label()
    if reported is None:
        return (
            "the resolved binary did not report a readable version, so the launch "
            f"cannot establish it is the tested {line}",
            _mimocode_version_remedy(),
        )
    return (
        f"the resolved binary reports {reported[0]}.{reported[1]}, and this launch has "
        f"only been tested against {line}: the permission read-back and the ACP spawn "
        "have never run against that line",
        _mimocode_version_remedy(),
    )


# -- MiMo Code's permission service, as 0.1.15 runs it ----------------------------
#
# The read-back judges the resolved config with MiMo Code's own rule functions,
# transcribed from its ``permission`` package and its config schema: a rule list in
# config order, the LAST match deciding (``permission/evaluate.ts``), a tool hidden
# when its last match denies every pattern, its edit/read GROUP counted as well as
# its own name (``permission/index.ts:702-722``), and an agent's own rules appended
# after the top-level ones (``agent/agent.ts:486``). Each function names the source
# it follows.


class _MimoRule(NamedTuple):
    """One rule in MiMo Code's ruleset: ``Rule`` in 0.1.15's ``permission/evaluate.ts``."""

    permission: str
    pattern: str
    action: str


def _mimocode_ruleset(value: object) -> list[_MimoRule]:
    """*value*, one resolved ``permission`` setting, as MiMo Code's ordered ruleset.

    ``Permission.fromConfig`` (``permission/index.ts:674-694``): a key holding an
    action is one rule for every pattern, a key holding a pattern map is one rule per
    pattern, in the map's order. A bare action for the whole setting is
    ``{"*": action}`` (``config/permission.ts:63-64``). An empty map is an empty
    ruleset, which is how 0.1.15 prints an agent that sets no permission of its own.
    """
    if isinstance(value, str):
        return [_MimoRule("*", "*", value)]
    if not isinstance(value, dict):
        return []
    rules: list[_MimoRule] = []
    for key, entry in value.items():
        if isinstance(entry, str):
            rules.append(_MimoRule(str(key), "*", entry))
        elif isinstance(entry, dict):
            rules.extend(
                _MimoRule(str(key), str(pattern), action)
                for pattern, action in entry.items()
                if isinstance(action, str)
            )
    return rules


def _mimocode_wildcard(value: str, pattern: str) -> bool:
    """``Wildcard.match`` from 0.1.15's ``util/wildcard.ts:3-19``.

    Both sides have ``\\`` turned into ``/``; the pattern is anchored at both ends,
    ``*`` matches any run and ``?`` any one character, everything else is literal,
    and a trailing `` *`` also matches the bare prefix. MiMo Code compiles the
    pattern to a regex; here it is matched segment by segment instead
    (:func:`_mimocode_segments_match`), because the pattern comes from the operator's
    config and a regex built from ``*?*?*?...`` could stall the gateway loop.
    """
    value = value.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    if sys.platform == "win32":
        # The ``i`` flag MiMo Code adds to the regex on Windows (``:17``).
        value, pattern = value.lower(), pattern.lower()
    if _mimocode_segments_match(value, pattern):
        return True
    return pattern.endswith(" *") and _mimocode_segments_match(value, pattern[:-2])


def _mimocode_literal_at(value: str, at: int, segment: str) -> bool:
    """Whether *segment* (literal, ``?`` = any character) occupies *value* from *at*."""
    if at < 0 or at + len(segment) > len(value):
        return False
    for offset, char in enumerate(segment):
        if char != "?" and char != value[at + offset]:
            return False
    return True


def _mimocode_segments_match(value: str, pattern: str) -> bool:
    """Anchored ``*``/``?`` match by the pattern's ``*``-separated segments.

    The first segment must start *value*, the last must end it, and each one between
    is taken at its leftmost fit in order; taking the leftmost fit never loses a
    match, because every segment has a fixed length. At most
    ``len(value) * len(pattern)`` character tests, whatever the pattern holds.
    """
    first, *rest = pattern.split("*")
    if not rest:
        return len(value) == len(first) and _mimocode_literal_at(value, 0, first)
    last = rest.pop()
    end = len(value) - len(last)
    if end < len(first) or not _mimocode_literal_at(value, 0, first):
        return False
    if not _mimocode_literal_at(value, end, last):
        return False
    at = len(first)
    for segment in rest:
        while at + len(segment) <= end and not _mimocode_literal_at(value, at, segment):
            at += 1
        if at + len(segment) > end:
            return False
        at += len(segment)
    return True


def _mimocode_disabled(tools: Collection[str], ruleset: list[_MimoRule]) -> frozenset[str]:
    """``Permission.disabled`` from 0.1.15's ``permission/index.ts:702-722``.

    A tool is hidden when the LAST rule naming it denies every pattern. A rule names
    a tool by the tool's own id, and a grouped tool also by its group's
    (:data:`~kiro_crew.providers.mirrors.mimocode.MIMOCODE_PERMISSION_GROUPS`), one
    ``findLast`` over both: so a later ``edit`` rule can re-enable ``write``.
    """
    from kiro_crew.providers.mirrors.mimocode import MIMOCODE_PERMISSION_GROUPS

    hidden: set[str] = set()
    for tool in tools:
        names = [tool] + [g for g, members in MIMOCODE_PERMISSION_GROUPS.items() if tool in members]
        decider = None
        for rule in ruleset:
            if any(_mimocode_wildcard(name, rule.permission) for name in names):
                decider = rule
        if decider is not None and decider.pattern == "*" and decider.action == "deny":
            hidden.add(tool)
    return frozenset(hidden)


def _mimocode_rule_contexts(resolved: dict, setting_key: str) -> list[tuple[str, list[_MimoRule]]]:
    """The rulesets a session's calls can be judged by: ``("", top)``, then one per agent.

    An agent's own ``permission`` is APPENDED after the top-level rules
    (``agent/agent.ts:486`` on 0.1.15), so it can outrank them for that agent, and a
    session may run as any agent the config defines. MiMo Code prints
    ``permission: {}`` for an agent that sets none of its own (measured on 0.1.15
    with only ``agent.build.model`` set); that adds no rule and is not a context of
    its own. Legacy ``mode`` entries are already folded into ``agent`` when the
    document is printed (``config/config.ts:979-986``).
    """
    top = _mimocode_ruleset(resolved.get(setting_key))
    contexts = [("", top)]
    agents = resolved.get("agent")
    if isinstance(agents, dict):
        for name, entry in sorted(agents.items(), key=lambda item: str(item[0])):
            own = _mimocode_ruleset(entry.get(setting_key)) if isinstance(entry, dict) else []
            if own:
                contexts.append((str(name), top + own))
    return contexts


def _mimocode_spelled(rules: list[_MimoRule]) -> str:
    """*rules* in config spelling, for a refusal to quote."""
    spelled: dict[str, object] = {}
    for rule in rules:
        spelled[rule.permission] = (
            rule.action if rule.pattern == "*" else {rule.pattern: rule.action}
        )
    return json.dumps(spelled, sort_keys=True)


def _mimocode_posture(ruleset: list[_MimoRule], crew_denies: Collection[str]) -> object:
    """What *ruleset* asks for every call, in the shape the seeded-setting gate reads.

    ``"ask"`` when every call asks or is denied, judged MiMo Code's way: the last
    ``"*"``/``"*"`` rule is the floor and only later rules change a call. A missing
    or non-``ask`` floor is what is in force; a later ALLOW runs calls past the host
    gate; an operator ``deny`` before the floor would silently become an ask (Crew's
    seeded denies are judged by :func:`_mimocode_disabled` instead). ``None`` for an
    empty ruleset ("the setting is not there").
    """
    if not ruleset:
        return None
    floors = [i for i, rule in enumerate(ruleset) if rule.permission == "*" and rule.pattern == "*"]
    if not floors:
        return _mimocode_spelled(ruleset)
    floor = floors[-1]
    if ruleset[floor].action != "ask":
        return ruleset[floor].action
    allowing = [rule for rule in ruleset[floor + 1 :] if rule.action not in ("ask", "deny")]
    if allowing:
        return _mimocode_spelled(allowing)
    cancelled = [
        rule
        for rule in ruleset[:floor]
        if rule.action == "deny" and rule.permission not in crew_denies
    ]
    if cancelled:
        return _mimocode_spelled(cancelled)
    return "ask"


#: Where MiMo Code mounts MCP servers from besides Crew's array, for a remedy to
#: name. 0.1.15 also merges Claude Code's servers from ``~/.claude.json`` and the
#: project's ``.claude.json`` unless ``MIMOCODE_DISABLE_CLAUDE_CODE_MCP`` is set
#: (``config/config.ts:974-977``).
_MIMOCODE_MCP_SOURCES = (
    "MiMo Code's own config (mimocode.json or .mimocode/), or Claude Code's "
    "~/.claude.json and ./.claude.json, which MiMo Code also mounts unless "
    "MIMOCODE_DISABLE_CLAUDE_CODE_MCP is set"
)


def _mimocode_config_mcp_server_names(resolved: dict) -> tuple[tuple[str, ...], str]:
    """The MCP server names the harness's resolved config mounts, and any issue.

    The ``mcp`` map holds every server mounted outside Crew's array, Claude Code's
    included (``config/config.ts:974-977``), whose tools arrive only as a fused
    ``<server>_<tool>`` title. Only names the harness rewrites are kept, so a title
    splits back to a spec hook's ``mcp__server__tool`` spelling; the every-``_``
    split reproduces the rest. Bounded, not truncated: a name over
    :data:`MAX_HARNESS_TOOL_NAME_LEN` or more than
    :data:`MAX_HARNESS_CONFIG_MCP_SERVERS` names is an issue the caller refuses on,
    since a name left out could let a deny hook miss a call.
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
    if len(servers) > MAX_HARNESS_CONFIG_MCP_SERVERS:
        return (), (
            f"its config mounts {len(servers)} MCP servers, more than the "
            f"{MAX_HARNESS_CONFIG_MCP_SERVERS} Crew tracks per session"
        )
    return tuple(name for name in names if mimocode_rewrites_name(name)), ""


def _mimocode_config_mcp_servers_remedy() -> str:
    """What an operator does when MiMo Code mounts too many MCP servers of its own."""
    return (
        f"Remove MCP servers from {_MIMOCODE_MCP_SOURCES}, or shorten their names to "
        f"{MAX_HARNESS_TOOL_NAME_LEN} characters, then start a new session."
    )


def _mimocode_unenforced_denies(
    contexts: list[tuple[str, list[_MimoRule]]], deny_rules: Collection[str]
) -> frozenset[str]:
    """The seeded deny rules MiMo Code does not put in force in every context.

    A rule is in force when :func:`_mimocode_disabled` hides its tool under the
    top-level ruleset AND under every agent's, since a session may run as any agent.
    """
    return frozenset(
        tool_id
        for tool_id in deny_rules
        if not all(tool_id in _mimocode_disabled([tool_id], rules) for _, rules in contexts)
    )


def _mimocode_native_servers_holding(resolved: dict, rules: Collection[str]) -> list[str]:
    """Servers the harness mounts from its OWN config whose tool ids *rules* could name.

    *resolved* is the harness's resolved config; its ``mcp`` map is every server
    the harness mounts itself, apart from Crew's ``session/new`` array. A rule id is
    ``sanitize(server) + "_" + sanitize(tool)``, so a server whose sanitized name plus
    ``_`` starts a rule id is reported. Prefix matching can over-report a server
    whose name is a prefix of another's; that only refuses more, never less.
    """
    from kiro_crew.providers.mirrors.mimocode import mimocode_tool_id

    mounts = resolved.get("mcp")
    if not rules or not isinstance(mounts, dict):
        return []
    return [
        name
        for name in sorted(str(n) for n in mounts)
        if any(rule.startswith(mimocode_tool_id(name, "")) for rule in rules)
    ]


def _mimocode_seeded_deny_rules(config_content: str, setting_key: str) -> tuple[str, ...]:
    """The deny rules Crew wrote into *config_content*, in seed order.

    Read back out of the seed itself rather than handed in separately, so the
    read-back judges exactly what the harness was given. The seed spells each one
    as the bare action (:func:`_mimocode_routing_config`).
    """
    try:
        seed = json.loads(config_content)
    except ValueError:
        return ()
    value = seed.get(setting_key) if isinstance(seed, dict) else None
    if not isinstance(value, dict):
        return ()
    return tuple(str(key) for key, action in value.items() if key != "*" and action == "deny")


def _mimocode_ambient_inline_config() -> dict[str, Any]:
    """The operator's own ``MIMOCODE_CONFIG_CONTENT``, as a JSON object; ``{}`` if unusable.

    A value that is not a JSON object cannot be merged, and is said so in the log
    rather than dropped silently.
    """
    ambient = os.environ.get(_ENV_MIMOCODE_CONFIG_CONTENT) or ""
    if not ambient:
        return {}
    try:
        parsed = json.loads(ambient)
    except ValueError:
        parsed = None
        why = "is not valid JSON"
    else:
        why = "is not a JSON object"
    if isinstance(parsed, dict):
        return parsed
    logger.warning(
        "%s %s, so this session's harness config carries only Crew's permission routing.",
        _ENV_MIMOCODE_CONFIG_CONTENT,
        why,
    )
    return {}


def _mimocode_routing_config(backend: str, deny_rules: Collection[str] = ()) -> str:
    """The ``MIMOCODE_CONFIG_CONTENT`` value that makes this session ask.

    *deny_rules* are the tool ids the session's projection asks MiMo Code itself to
    deny (``SessionProjection.harness_deny_rules``).

    The operator's inline config is kept whole; only the permission setting is
    Crew's. MiMo Code merges this source with others key by key, each keeping its key
    order (``config/config.ts:906-914``, ``config/permission.ts:26-31``), so the
    read-back judges the merged result, not this seed. Each switched-off tool is a
    bare ``"deny"`` AFTER the ``"*"`` (the last matching rule wins), never
    ``{"*": "deny"}``: the schema takes only an action under ``question``,
    ``webfetch``, ``websearch``, ``codesearch`` and ``doom_loop``
    (``config/permission.ts:45-50``), and a map there invalidates the whole document
    (measured with ``"doom_loop": {"*": "deny"}``, a fused ``doom``/``loop`` id).
    """
    setting_key, value = acp_tool_gate.permission_setting_for(backend)
    config = _mimocode_ambient_inline_config()
    rules = tuple(deny_rules)
    config[setting_key] = (
        {"*": value, **dict.fromkeys(rules, "deny")} if rules and value == "ask" else value
    )
    return json.dumps(config)


def _verify_mimocode_routing(
    session: ProcessSession, backend: str, argv: list[str], config_content: str
) -> tuple[str, str]:
    """Read the harness's OWN resolved permission back, and report any issue.

    This makes the routing VERIFIED rather than seeded: the harness's own config
    resolution merges every source as the ACP server does, so a precedence change
    in a later release surfaces as a refusal, not a session that stops asking. It
    does not prove the harness HONOURS the setting per call; that is its contract.

    Returns ``("", "")`` when the required value is in force, else the reason and
    its remedy, decided together: a read-back that could not RUN or be PARSED is a
    harness problem (run its own command, fix the install); a value other than the
    required one is a config problem (remove the source outranking the seed).

    *argv* arrives ALREADY SANDBOX-WRAPPED, a security property: this is the same
    third-party binary, resolving config (and project plugins) from the session work
    dir, and unwrapped it would read the credential homes the mask denies. Records
    the config's rewritten MCP server names on *session* for the turn-time hook
    matcher. Blocking (spawns a short-lived child); callers run it off the loop.
    """
    setting_key, _required = acp_tool_gate.permission_setting_for(backend)
    # The SAME environment and scrub as the spawn. The harness reads its config
    # LOCATION from the environment (``XDG_CONFIG_HOME``, ``MIMOCODE_CONFIG``), so
    # without ``agent.env`` and the per-session overlay (``_extra_env``) it would
    # resolve other files than the session it vouches for; and the gateway's env
    # carries tokens and secrets no foreign harness may see, before the spawn's scrub.
    env = _mimocode_child_env(session)
    # Refused BEFORE the child runs: a refused variable relocates what the host fences
    # by path, so a child that sees it would vouch for an unfenced credential.
    refused = _mimocode_refused_env_issue(env)
    if refused:
        return refused, _MIMOCODE_REFUSED_ENV_REMEDY
    _apply_mimocode_env(env)
    env[_ENV_MIMOCODE_CONFIG_CONTENT] = config_content
    try:
        completed = subprocess_mod.run(
            argv,
            cwd=session._spawn_work_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_MIMOCODE_READBACK_TIMEOUT_S,
        )
    except (OSError, subprocess_mod.SubprocessError) as exc:
        return (
            f"the resolved configuration could not be read back ({exc})",
            _mimocode_readback_remedy(),
        )
    if completed.returncode != 0:
        # The child's own fault, same as the pi read-back: an operator reading this
        # refusal learns both that the harness failed and which recognised fault it
        # hit.
        detail = f"exit {completed.returncode}"
        detail = launch_mod._readback_detail_with_diagnosis(detail, completed.stderr)
        return (
            f"the resolved configuration could not be read back ({detail})",
            _mimocode_readback_remedy(),
        )
    # The harness prints a banner before the document, so the object is found
    # rather than assumed to start at byte zero. 0.1.15 prints the document
    # UNMASKED (``cli/cmd/debug/config.ts:13-14``: a provider ``apiKey`` comes back
    # verbatim), so stdout is parsed here and never logged or quoted; only stderr
    # feeds a refusal, through the diagnosis above.
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
            _mimocode_readback_remedy(),
        )
    config_servers, servers_issue = _mimocode_config_mcp_server_names(resolved)
    if servers_issue:
        return servers_issue, _mimocode_config_mcp_servers_remedy()
    session._mimocode_config_mcp_servers = config_servers
    seeded_denies = _mimocode_seeded_deny_rules(config_content, setting_key)
    contexts = _mimocode_rule_contexts(resolved, setting_key)
    # Which of Crew's deny rules MiMo Code's own disabled() keeps in force in every
    # context. A rule that lost is normally not a refusal: the projection withholds
    # its server.
    session._mimocode_denies_unenforced = _mimocode_unenforced_denies(contexts, seeded_denies)
    # ...unless the harness mounts that server from its OWN config too. Withholding the
    # array element does not reach that mount, so the switched-off tool would stay
    # callable there: refuse the session instead.
    lost = frozenset(getattr(session, "_session_harness_deny_rules", ()) or ()) - (
        frozenset(seeded_denies) - session._mimocode_denies_unenforced
    )
    native = _mimocode_native_servers_holding(resolved, lost)
    # A server the projection could not keep narrowed at all (a tool id that collides
    # with a permission group, so no rule exists) is matched by NAME: the harness
    # registers a native mount under the name its config gives it.
    # One switched off with ``enabled: false`` is never connected (``mcp/index.ts:725``).
    raw_mounts = resolved.get("mcp")
    mounts: set[str] = (
        {
            str(name)
            for name, spec in raw_mounts.items()
            if not (isinstance(spec, dict) and spec.get("enabled") is False)
        }
        if isinstance(raw_mounts, dict)
        else set()
    )
    # Kept so a later re-projection on this spawn is checked against the same native
    # mounts (see refuse_native_mount_of_unhonoured).
    session._mimocode_native_mounts = frozenset(mounts)
    session._mimocode_native_mount_launches = _mimocode_native_mount_launches(raw_mounts)
    unhonoured: frozenset[str] = getattr(session, "_session_mcp_unhonoured", frozenset())
    native = sorted(set(native) | (mounts & unhonoured))
    if native:
        return _native_mount_refusal(session, native)
    disabled = sorted(mounts & getattr(session, "_mimocode_disabled_servers", frozenset()))
    if disabled:
        return _native_disabled_refusal(session, disabled)
    # The asking posture, judged in the top-level ruleset and again in every agent's:
    # one agent whose rules let a call run unasked is one agent whose tool calls never
    # reach the host gate, so the first such context refuses the session.
    for agent_name, rules in contexts:
        observed = _mimocode_posture(rules, seeded_denies)
        issue = acp_tool_gate.seeded_setting_issue(backend, session._scrub_observed(observed))
        if not issue:
            continue
        if agent_name:
            issue = f"agent {session._scrub_observed(agent_name)!r} adds rules after it: {issue}"
        return issue, acp_tool_gate.remediation_for(backend)
    return "", ""


def _native_mount_refusal(session: ProcessSession, native: Collection[str]) -> tuple[str, str]:
    """The refusal for a narrowed server MiMo Code mounts outside Crew's array."""
    names = ", ".join(repr(session._scrub_observed(n)) for n in native)
    return (
        f"a switched-off tool on {names} is not denied in the resolved config, and "
        "MiMo Code mounts that server itself, where withholding it from Crew's array "
        "cannot reach",
        f"Remove that tool's key from your MiMo Code config, or stop mounting the server "
        f"in {_MIMOCODE_MCP_SOURCES}.",
    )


def _native_disabled_refusal(session: ProcessSession, native: Collection[str]) -> tuple[str, str]:
    """The refusal for a server switched off in Crew that MiMo Code mounts itself."""
    names = ", ".join(repr(session._scrub_observed(n)) for n in native)
    return (
        f"{names} is switched off in Kiro Crew, and MiMo Code mounts that server "
        "itself, where withholding it from Crew's array cannot reach",
        f"Stop mounting the server in {_MIMOCODE_MCP_SOURCES}, or switch it back on "
        "in Kiro Crew.",
    )


def settle_mimocode_denies(session: ProcessSession, config_content: str) -> frozenset[str]:
    """Record which deny rules are in force after the read-back; return the lost ones.

    In force means SEEDED and not outranked. Derived from the seed Crew actually
    handed the harness, never from the rules the projection asked for, so a routing
    value that leaves the rules out cannot be read as having put them in. The lost
    rules are the ones the projection asked for that are not in force; the caller
    re-projects, and each one's server is withheld whole.
    """
    setting_key = acp_tool_gate.permission_setting_for(ACP_BACKEND_MIMOCODE)[0]
    seeded = frozenset(_mimocode_seeded_deny_rules(config_content or "", setting_key))
    unenforced: frozenset[str] = getattr(session, "_mimocode_denies_unenforced", frozenset())
    session._mimocode_denies_in_force = seeded - unenforced
    asked = frozenset(getattr(session, "_session_harness_deny_rules", ()) or ())
    return asked - session._mimocode_denies_in_force


def refuse_native_mount_of_unhonoured(session: ProcessSession) -> None:
    """Refuse the session if a server this projection withholds is mounted natively.

    Withholding the array element cannot reach a server the harness mounts from its
    own config, so a narrowed server there would keep its switched-off tool, and one
    switched off whole would stay callable. The native mounts are the ones the
    read-back resolved for this spawn.
    """
    mounts: frozenset[str] = getattr(session, "_mimocode_native_mounts", frozenset())
    native = sorted(mounts & getattr(session, "_session_mcp_unhonoured", frozenset()))
    disabled = sorted(mounts & getattr(session, "_mimocode_disabled_servers", frozenset()))
    if not native and not disabled:
        return
    issue, remedy = (
        _native_mount_refusal(session, native)
        if native
        else _native_disabled_refusal(session, disabled)
    )
    try:
        acp_tool_gate.enforce_runtime_routing(ACP_BACKEND_MIMOCODE, issue, remedy=remedy)
    except acp_tool_gate.ToolGateUnroutable as exc:
        raise AcpToolGateUnroutable(str(exc)) from None


class MimoCodeMountLaunch(NamedTuple):
    """How MiMo Code starts one local MCP server of its own config."""

    command: tuple[str, ...]
    environment: dict[str, str]


def _mimocode_native_mount_launches(raw_mounts: object) -> dict[str, MimoCodeMountLaunch]:
    """The command and environment of each local MCP server the read-back resolved.

    What Crew spawns to read that server's own ``tools/list``
    (:func:`mimocode_mcp_tool_list_target`). The harness starts a ``local`` entry as
    ``command[0]`` with ``command[1:]`` and its ``environment`` on top of its own
    (``mcp/index.ts:658-674``). A ``remote`` entry has none, since the harness holds
    its credentials, and neither has one switched off with ``enabled: false``, which
    it never connects. Kept in memory only: an ``environment`` value may be a secret.
    """
    launches: dict[str, MimoCodeMountLaunch] = {}
    if not isinstance(raw_mounts, dict):
        return launches
    for name, spec in raw_mounts.items():
        if not isinstance(spec, dict) or spec.get("enabled") is False:
            continue
        if spec.get("type") not in ("local", "stdio"):
            continue
        command = spec.get("command")
        if not (isinstance(command, list) and command and all(isinstance(p, str) for p in command)):
            continue
        environment = spec.get("environment")
        env = (
            {str(k): str(v) for k, v in environment.items()}
            if isinstance(environment, dict)
            else {}
        )
        launches[str(name)] = MimoCodeMountLaunch(tuple(command), env)
    return launches


#: The ``clientInfo`` of Crew's own probe of a server's ``tools/list``. A probe under
#: an identity of its own is not the dashboard's observation of that server, so
#: ``probe_server`` keeps its answer out of the shared probe cache.
_MIMOCODE_TOOL_LIST_CLIENT = {"name": "kirocrew-mimocode-tool-names", "version": "1"}


def _acp_element_pairs(element: dict, key: str) -> dict[str, str]:
    """An ACP ``mcpServers`` element's ``[{name, value}]`` list as a mapping."""
    return {
        str(p["name"]): str(p.get("value", ""))
        for p in element.get(key) or []
        if isinstance(p, dict) and p.get("name")
    }


def mimocode_mcp_tool_list_target(
    name: str, placed: Collection[object], native: dict[str, MimoCodeMountLaunch]
) -> McpServerInfo | None:
    """What Crew probes for the ``tools/list`` of the server MiMo Code mounts as *name*.

    The element Crew placed on the array comes first, since the harness lets an
    ACP-placed server replace its own config's entry of the same name
    (``mcp/index.ts:1150``, ``{ ...cfg.mcp, ...HostMcp.get() }``); then the
    read-back's local launch. *placed* is the array as the harness receives it,
    which tells a local element by its having no ``type`` key
    (``acp/agent.ts:1144-1163``). ``None`` when neither can be probed: an SSE
    element, a native remote server, or a name no source holds.
    """
    from kiro_crew.mcp_discovery import McpServerInfo

    for element in placed:
        if not isinstance(element, dict) or element.get("name") != name:
            continue
        # Read the way the harness reads it: no ``type`` key is a local server, so
        # the mirror drops the stdio tag (``_mimocode_local_element``).
        if "type" not in element and element.get("command"):
            return McpServerInfo(
                name=name,
                command=str(element["command"]),
                args=[str(a) for a in element.get("args") or []],
                env=_acp_element_pairs(element, "env"),
            )
        if element.get("type") == "http" and element.get("url"):
            return McpServerInfo(
                name=name,
                url=str(element["url"]),
                headers=_acp_element_pairs(element, "headers"),
            )
        return None
    launch = native.get(name)
    if launch is None:
        return None
    return McpServerInfo(
        name=name,
        command=launch.command[0],
        args=list(launch.command[1:]),
        env=dict(launch.environment),
    )


async def mimocode_mcp_tool_list(target: McpServerInfo) -> tuple[str, ...] | None:
    """The tool names *target* lists, read through Crew's MCP probe, or ``None``.

    The probe is the one spawn path for a configured server (its sandbox, the
    consent refusal and the env scrub; local probes run off the gateway loop). It
    asks under its own ``clientInfo``, which a server that answers ``tools/list``
    by caller could answer differently from the harness; ``None`` when it does not
    answer at all.
    """
    from kiro_crew.mcp_discovery import probe_server

    probed = await probe_server(target, client_info=dict(_MIMOCODE_TOOL_LIST_CLIENT))
    if probed.status != "ok":
        logger.warning(
            "session MCP: the tool list of %r could not be read (%s), so a MiMo Code "
            "tool name the harness may have rewritten cannot be matched to a real one",
            target.name,
            probed.status,
        )
        return None
    return tuple(probed.tools)


class MimoCodeLaunch(ProcessAdapter):
    """MiMo Code: a self-served binary whose asking posture is seeded and read back."""

    backend = ACP_BACKEND_MIMOCODE

    def __init__(self) -> None:
        # The routing seed the read-back verified, carried to the child's environment.
        self._config_content = ""

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the binary, warm the session's MCP array, then establish the routing."""
        session = ctx.session
        assert session is not None, "mimo is launched for one session"
        mimocode_bin, argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
            self.backend
        )
        # Translate the agent spec into this session's MCP array HERE, for exactly
        # the reason the claude adapter does it in its own launch: the translation
        # reads disk, and doing it at the shared session/new call site would put an
        # executor hop and a new failure mode on EVERY backend's construction path,
        # kiro-cli included (harness-parity H13). No ordering constraint of claude's
        # applies -- this harness's array is not conditional on Crew owning a
        # permission file, because its routing is seeded on MIMOCODE_CONFIG_CONTENT
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
        # A refused variable, BEFORE any child of this harness runs, the version
        # probe included: it relocates something the host fences by path, so even a
        # ``--version`` child would start with that credential outside the mask.
        # OFF-LOOP: building it reads the agent.env config and resolves the agent
        # socket and Kerberos cache from the filesystem.
        child_env = await asyncio.to_thread(_mimocode_child_env, session)
        env_issue = _mimocode_refused_env_issue(child_env)
        if env_issue:
            try:
                acp_tool_gate.enforce_runtime_routing(
                    self.backend, env_issue, remedy=_MIMOCODE_REFUSED_ENV_REMEDY
                )
            except acp_tool_gate.ToolGateUnroutable as exc:
                raise AcpToolGateUnroutable(str(exc)) from None
        # The version gate, BEFORE the read-back: every child below this line is
        # this harness's own binary doing work only its tested line's shapes are
        # measured against. A binary outside that line is refused here, so the
        # read-back it was never measured against never runs.
        version_issue, version_remedy = await _mimocode_version_refusal(
            session,
            mimocode_bin,
            sandbox_mode=ctx.sandbox_mode,
            hidden=hidden,
            expose=expose,
        )
        if version_issue:
            # Raised DIRECTLY as the ACP-layer type rather than through
            # ``enforce_runtime_routing``: that wrapper frames every reason as
            # "tool calls would not reach Kiro Crew's security gate", which this
            # gate has not measured -- no routing read-back ran, only
            # ``--version``. The refusal says what happened: the binary is
            # outside the tested line, and everything below was never measured
            # against it. Same type, so ``ensure_ready`` still catches it and the
            # failed spawn's cleanup runs the same way with it.
            raise AcpToolGateUnroutable(
                f"{acp_tool_gate.label_for(self.backend)} launch refused: "
                f"{version_issue}. {version_remedy}"
            ) from None
        # The routing seed, and the READ-BACK that is what this harness's Routing
        # member promises. OFF-LOOP: the read-back spawns a short-lived child, and a
        # synchronous spawn on the gateway loop is the stall this path guards
        # against everywhere else.
        self._config_content = _mimocode_routing_config(
            self.backend, getattr(session, "_session_harness_deny_rules", ())
        )
        # Wrapped in the SAME sandbox, with the SAME credential mask, as the session
        # spawn. The read-back runs the harness's own binary, and this harness
        # resolves its configuration by reading the work dir -- which can load a
        # project's plugins -- so an unwrapped read-back would hand a third-party
        # binary the credential homes the mask denies it, moments before the masked
        # spawn.
        readback_argv, readback_cleanup = await wrap_argv_async(
            [mimocode_bin, *_MIMOCODE_CONFIG_READBACK_ARGS],
            mode=ctx.sandbox_mode,
            strip_python_env=True,
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
            _prepare=wrap_argv,
        )
        try:
            routing_issue, routing_remedy = await asyncio.to_thread(
                _verify_mimocode_routing,
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
        lost = settle_mimocode_denies(session, self._config_content)
        if lost:
            # A deny rule the projection asked for is not in force: the seed left it
            # out, or the harness's resolved config outranks it. Its tool would stay
            # listed, so re-project with only the rules that held: the server it
            # narrows is withheld whole.
            logger.warning(
                "mimocode: %d per-tool deny rule(s) are not in force in the resolved "
                "config, so the servers they narrow are withheld this session: %s",
                len(lost),
                ", ".join(str(session._scrub_observed(rule)) for rule in sorted(lost)),
            )
            session._session_mcp_cache = await asyncio.to_thread(
                session._resolve_session_mcp_servers
            )
            # The re-projection re-reads the spec, so it can narrow a server the
            # read-back never judged. Hold the result to the same native mounts.
            refuse_native_mount_of_unhonoured(session)
        # The routing has been read back and holds: arm the tripwire for the switch
        # the read-back cannot see (the module docstring). A refused launch never
        # reaches this line, so it arms nothing. The memory root is the one place an
        # edit tool of this harness writes without asking, so an unasked edit
        # anywhere else trips it too.
        session._mimocode_memory_root = _mimocode_memory_root(child_env)
        session._unasked_shell_tripwire = True
        return SpawnPlan(
            argv=argv,
            spawn_label=spawn_label,
            stderr_label=stderr_label,
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Apply the seed the read-back verified, unconditionally.

        The merge in :func:`_mimocode_routing_config` already preserved every key the
        operator set, and this value is the one the host gate depends on. The
        variables the read-back cannot see are stripped and pinned here too, the
        same way they were for the read-back's child.
        """
        _apply_mimocode_env(env)
        if self._config_content:
            env[_ENV_MIMOCODE_CONFIG_CONTENT] = self._config_content
