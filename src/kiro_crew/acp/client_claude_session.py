"""The claude-agent-acp session contract ``ClaudeAcpClient`` keeps.

Adapter version floors, leaving a project-owned ``settings.local.json`` out of the
setting sources (carrying its deny and ask rules inline instead), the ``_meta`` envelope
of ``session/new``, a Side Chat session's sensitive-path deny rules, and the starting-
mode pin.

Each function here whose first parameter is ``self`` is a ``ClaudeAcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_claude_held_asks``, ``_claude_held_mcp``, ``_claude_inline_settings``,
``_claude_local_settings_excluded``, ``_claude_side_read_denies``, ``_claude_worker_builtin_tools``,
``_last_substitution_model``, ``_model``, ``_resolved_model_id``, ``_session_mcp_cache``,
``claude_mode_pinned_notice``.

Instance fields read: ``_agent_version``, ``_agent_version_read``, ``_include_memory``,
``_launch_mcp_servers``, ``_mcp_mount_servers``, ``_permission_mode``, ``_session_id``,
``_session_key``, ``_session_mcp_excludes_local``, ``_session_mcp_inline_settings``,
``_side_read_only``, ``_spec_shell_allowlist``, ``_spec_write_allowlist``,
``_spec_zero_tools``, ``memory_mode``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.claude_settings_sources import (
    CLAUDE_SETTINGS_MAX_BYTES,
    read_claude_settings_bytes,
)
from kiro_crew.acp.client_permissions import _scrub_observed
from kiro_crew.acp.client_timeouts import _INIT_TIMEOUT
from kiro_crew.acp.harness import claude as claude_mod
from kiro_crew.acp.harness_tool_names import CLAUDE_READ_ONLY_BUILTINS
from kiro_crew.acp.raw_sdk_tasks import session_meta_filter
from kiro_crew.acp.spec_tools_allowlists import claude_ask_rules_for_allowlists
from kiro_crew.acp.transport_errors import (
    AcpError,
    AcpProcessDied,
    AcpTimeoutError,
    AcpToolGateUnroutable,
)
from kiro_crew.acp.types import (
    ACP_BACKENDS_FORK_AT_MESSAGE,
    ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP,
    METHOD_SESSION_FORK,
    METHOD_SESSION_NEW,
    METHOD_SET_MODE,
)
from kiro_crew.agent_sdk.backends import resolve_cc_pin_inherited_mode
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
    sandbox_credential_targets,
)
from kiro_crew.user_json import loads_user_json

if TYPE_CHECKING:
    from kiro_crew.acp.client import ClaudeAcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


#: The only filesystem setting source a claude session loads when the project owns
#: its own ``settings.local.json``. Sent as ``_meta.claudeCode.options.settingSources``,
#: which claude-agent-acp spreads over its own ``["user", "project", "local"]``
#: default. Neither project tier loads: not ``local`` (the project's own file) and
#: not ``project`` (a checked-in ``.claude/settings.json``), so no rule a repository
#: carries can pre-approve a Crew tool.
_CLAUDE_CREW_GOVERNED_SETTING_SOURCES: tuple[str, ...] = ("user",)

#: The permission mode Crew pins on a claude session before its first prompt. It asks
#: the host about every call Crew's own settings do not deny.
_CLAUDE_PINNED_MODE = "default"

#: Starting modes that APPROVE Crew's MCP calls without asking the host, and so are
#: pinned away. ``bypassPermissions`` skips every prompt; ``auto`` lets Claude's own
#: classifier approve them. Every other mode is left as the session started it:
#: ``plan`` is stricter than ``default``, ``acceptEdits`` auto-approves only file
#: edits, never an MCP call, and ``dontAsk`` refuses every call that is not
#: pre-approved (the SDK's own contract: "deny if not pre-approved"), so it runs
#: nothing ``default`` would not.
_CLAUDE_GATE_ESCAPING_MODES = frozenset({"bypassPermissions", "auto"})

#: The chat notice for a session that asked for one of
#: :data:`_CLAUDE_GATE_ESCAPING_MODES` and runs in :data:`_CLAUDE_PINNED_MODE`
#: because its folder's ``settings.local.json`` is not Crew's. Shown once, on the
#: session's first turn (``AcpProvider.mode_pinned_notice``).
_CLAUDE_MODE_PINNED_NOTICE = (
    "ℹ This chat runs in Default mode (it asks before tools run), not the {mode} "
    "mode chosen for it. This folder has a .claude/settings.local.json that Crew "
    "does not own, so Crew leaves that file out and cannot keep {mode} without it. "
    "To get {mode} back, remove that file from this folder (or let Crew own it), "
    "or switch to the Kiro CLI harness, then start a new session."
)

#: The ``permissions.ask`` rules a claude Side Chat session carries inline: one per
#: read built-in it keeps. They send every call to the host gate even where an
#: allow would pre-approve it, so each gets a READ_ONLY decision and an SEL row.
#: Without the file-read rules, ``default`` mode runs Read, Grep and Glob inside
#: the session's cwd with no ``session/request_permission`` at all (captured on
#: claude-agent-acp 0.94.0, ``test/fixtures/acp_frames/claude/side-read-no-ask-live.jsonl``).
_CLAUDE_SIDE_READ_ONLY_ASK_RULES: tuple[str, ...] = (
    "Read",
    "Grep",
    "Glob",
    "WebFetch",
    "WebSearch",
)

#: The Claude Code switch that turns its auto-memory off for the whole session: it
#: neither loads ``<config dir>/projects/<project key>/memory/MEMORY.md`` nor writes
#: a memory file. Read by the CLI from its process env, where it wins over the
#: ``autoMemoryEnabled`` setting (Claude Code 2.1.318). Sent in
#: ``_meta.claudeCode.options.env`` and in the flag tier's ``settings.env`` for an
#: incognito or temporary session, and for a sub-agent spawned with
#: ``include_memory=false``.
_CLAUDE_DISABLE_AUTO_MEMORY_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"

#: gitignore's pattern characters. A rule written from a real path escapes them, as
#: Claude Code does for a path the user approves, so a directory named ``[x]`` is
#: fenced as that name rather than read as a character class.
_GITIGNORE_PATTERN_CHARS = re.compile(r"[\\*?\[\]]")


def _claude_root_pattern(path: str, *, windows: bool = os.name == "nt") -> str:
    """*path* as a Claude Code ``//`` rule pattern: absolute from the filesystem root.

    Claude Code matches POSIX spellings, and writes a Windows drive path as
    ``/c/Work/...``, so ``C:\\Work\\a`` becomes ``//c/Work/a``.
    """
    if windows:
        win = PureWindowsPath(path)
        path = win.as_posix()
        if len(win.drive) == 2 and win.drive[1] == ":":
            path = f"/{win.drive[0].lower()}{path[2:]}"
    return "/" + _GITIGNORE_PATTERN_CHARS.sub(lambda m: "\\" + m.group(0), path)


def _claude_side_read_deny_rules() -> tuple[str, ...]:
    """The ``permissions.deny`` rules that fence the host's sensitive paths on a side session.

    A claude Side Chat session keeps the user's settings, and Read, Grep and
    Glob reach the READ_ONLY gate only through the ``ask`` rules it carries
    (:data:`_CLAUDE_SIDE_READ_ONLY_ASK_RULES`). These deny rules refuse a
    sensitive path inside claude itself, before any ask, so that fence does
    not rest on the gate. Claude Code checks deny rules before ask and allow rules from every scope,
    applies Read rules to Grep and Glob (best effort, by its own account), and
    denies a symlink whose target a rule matches, so these rules hold whatever
    the inherited settings say.

    The targets are :func:`~kiro_crew.security.sandbox_credential_targets`, the
    set the sandbox mask denies: every sensitive leaf under both home spellings
    and under each override root. Each is denied as itself and as everything
    under it. Blocking (resolves the roots); call it off the loop.
    """
    rules: list[str] = []
    for target in sandbox_credential_targets():
        pattern = _claude_root_pattern(target)
        rules.append(f"Read({pattern})")
        rules.append(f"Read({pattern}/**)")
    return tuple(rules)


#: The project settings files whose ``permissions.deny`` the exclusion carries
#: inline: the project's own ``settings.local.json`` and a checked-in
#: ``settings.json``, both under ``<work dir>/.claude``. Leaving their tiers out
#: must drop their allows, not their denies.
_CLAUDE_PROJECT_SETTINGS_FILES: tuple[str, ...] = ("settings.local.json", "settings.json")

#: The oldest claude-agent-acp verified to honour ``_meta.claudeCode.options``
#: ``settingSources``: 0.84.0 spreads those options over its own
#: ``["user", "project", "local"]`` default (``dist/acp-agent.js``). Below it, or
#: when the adapter reports no ``agentInfo.version``, a project-owned file is NOT
#: left out: the adapter could load its ``permissions.allow`` anyway, so the session
#: keeps the array withheld.
CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION = (0, 84, 0)

#: The oldest claude-agent-acp whose ``_session/steering`` this client speaks:
#: 0.65.0, the first release that holds a steered turn open until a result answers
#: the steer. Measured live on 0.64.0, an in-flight steer is answered ``injected``
#: and the turn then ends ``end_turn`` without the model ever seeing the text, so
#: reporting it consumed there would lose it; 0.65.0 runs it inside the turn.
#: 0.61.0 to 0.63.x also lack ``_meta.steering.idleBehavior: "promptRequired"`` and
#: answer a late steer by starting a turn of their own. Below the floor, or when
#: the adapter reports no version, the session takes no steer and the caller queues.
CLAUDE_ACP_STEERING_MIN_VERSION = (0, 65, 0)

#: The oldest claude-agent-acp whose ``session/fork`` honours a fork point
#: (``_meta.jetbrains.air.fork.messageId``). Older ones advertise
#: ``sessionCapabilities.fork`` too but copy the whole session, so below it, or
#: when the adapter reports no version, a rewind does not fork.
CLAUDE_ACP_FORK_POINT_MIN_VERSION = (0, 71, 0)


def _adapter_version_at_least(agent_version: str, floor: tuple[int, int, int]) -> bool:
    """Whether the adapter-reported *agent_version* is at or above *floor*."""
    # Bounded digit runs: the version is adapter-reported, and ``int()`` refuses a
    # run past the interpreter's digit limit with ``ValueError``.
    match = re.match(r"(\d{1,9})\.(\d{1,9})\.(\d{1,9})(?!\d)", agent_version or "")
    if not match:
        return False
    found = tuple(int(part) for part in match.groups())
    return found >= floor


def _claude_adapter_honours_setting_sources(agent_version: str) -> bool:
    """Whether *agent_version* is at or above the ``settingSources`` floor."""
    return _adapter_version_at_least(agent_version, CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION)


#: Project settings keys that RESTRICT a session but cannot be carried inline:
#: a ``PreToolUse`` hook can also return ``allow``, and ``sandbox`` carries
#: ``autoAllowBashIfSandboxed``, so either would reopen a pre-approval channel.
#: A project file that sets one keeps its tiers loaded and the array withheld.
_CLAUDE_PROJECT_UNCARRIED_GUARD_KEYS: tuple[str, ...] = ("hooks", "sandbox")

#: The ``permissions`` lists of a project settings file that RESTRICT a session and
#: so ride inline when its tier is left out: ``deny`` refuses a call, ``ask``
#: forces a prompt even where an allow would pre-approve it. Merging either into
#: Crew's inline settings can only narrow what runs.
_CLAUDE_CARRIED_PERMISSION_KEYS: tuple[str, ...] = ("deny", "ask")

#: The shared settings-read ceiling, kept under this name for its importers.
_CLAUDE_PROJECT_SETTINGS_MAX_BYTES = CLAUDE_SETTINGS_MAX_BYTES


def _project_settings_restrictions(path: Path) -> list[tuple[str, str]] | None:
    """The ``(kind, rule)`` pairs a project settings file restricts with.

    ``kind`` is a key of :data:`_CLAUDE_CARRIED_PERMISSION_KEYS`: a
    ``permissions.deny`` rule refuses a call, a ``permissions.ask`` rule forces
    a prompt even where an allow would pre-approve it.

    Read only so the exclusion can carry them inline; nothing else in the file is
    used, and the file is never written. ``[]`` for an absent file, one with no
    such rules, or one whose content claude itself could not take rules from
    (not JSON, not an object, a ``permissions``, ``deny`` or ``ask`` of the
    wrong type): carrying nothing from it drops nothing the CLI would have enforced.
    ``None`` -- the caller then withholds the array -- when the bytes cannot be
    examined at all (a link, a non-regular or oversized file, a read error, or
    JSON nested too deeply for this parser), or when the file sets a key in
    :data:`_CLAUDE_PROJECT_UNCARRIED_GUARD_KEYS`, whose restriction leaving the
    tier out would drop.

    Read through :func:`~kiro_crew.acp.claude_settings_sources.read_claude_settings_bytes`:
    the path is attacker-influenced, so links are refused and the read is bounded
    and non-blocking. A file that changes after this read can only
    have GAINED a rule Crew did not carry; it can never add an allow, because the
    tier still does not load.
    """
    try:
        raw = read_claude_settings_bytes(path, follow_links=False)
    except FileNotFoundError:
        return []
    if raw is None:
        return None
    try:
        # A hand-edited settings file may carry a leading UTF-8 byte-order mark,
        # and its rules must still be carried.
        data = loads_user_json(raw.decode("utf-8"))
    except ValueError:
        return []
    except RecursionError:
        # Valid JSON nested past Python's parser depth: claude's own parser may
        # still take deny rules from it, so it cannot be read as having none.
        return None
    if isinstance(data, dict) and any(
        data.get(key) for key in _CLAUDE_PROJECT_UNCARRIED_GUARD_KEYS
    ):
        return None
    permissions = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(permissions, dict):
        return []
    found: list[tuple[str, str]] = []
    for kind in _CLAUDE_CARRIED_PERMISSION_KEYS:
        rules = permissions.get(kind)
        if isinstance(rules, list):
            found.extend((kind, rule) for rule in rules if isinstance(rule, str))
    return found


def _exclude_foreign_local_settings(
    self: ClaudeAcpClient, local_settings: Path, payload: str
) -> bool:
    """Leave a project-owned ``settings.local.json`` out of this session.

    The project's file stays exactly as it is: never written, and read only
    for its ``permissions.deny`` and ``permissions.ask`` rules (below), never
    for its mode. The
    session loads only the ``user`` setting source and carries Crew's
    payload inline as ``options.settings`` (the flag tier). Neither the
    project's ``settings.local.json`` nor a checked-in
    ``.claude/settings.json`` reaches the CLI, so no ``permissions.allow`` a
    repository carries can pre-approve a call before Crew's gate sees it.
    That is what lets the session carry Crew's tools.

    One thing a file still decides: claude-agent-acp picks the STARTING
    permission mode from every settings file itself, and a file can change
    between any check here and ``session/new``. So nothing here trusts the
    file's mode. :meth:`_pin_claude_starting_mode` reads back the mode the
    session actually started in and pins :data:`_CLAUDE_PINNED_MODE` before
    the first prompt, or stops the session.

    Leaving the project tiers out must not drop what they REFUSE. So the
    ``permissions.deny`` and ``permissions.ask`` rules of both project files
    are read (bounded, never
    through a link, see :func:`_project_settings_restrictions`) and carried inline
    beside Crew's own. A file that changes after that read can only have
    gained a restriction; its allows never load either way. Project ``hooks`` and
    ``sandbox`` settings cannot be carried without reopening an approval
    channel, so a project file that sets either refuses the exclusion.

    Refused, keeping the array withheld, when this session asked for a
    permission mode other than :data:`_CLAUDE_PINNED_MODE` or one of
    :data:`_CLAUDE_GATE_ESCAPING_MODES` (the pin sets only the asking mode, so a
    request for that mode is honoured and a request for ``auto`` runs in it), when
    a project file cannot be examined or sets ``hooks`` or ``sandbox``, or
    when the adapter that
    answered the handshake is below
    :data:`CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION` or reported no version.
    Before the handshake the floor is read from the version the launch read (the
    installed package's own, or for a wrapper script what it reported at its latest
    handshake), so the decision is made before the MCP array is first resolved;
    :meth:`_pin_claude_starting_mode` checks the handshake's version too.

    Returns whether the exclusion was taken.
    """
    whose = self._foreign_settings_owner(local_settings)
    if self._claude_adapter_below_setting_sources_floor():
        self._log_claude_adapter_below_floor(local_settings, whose)
        return False
    requested = getattr(self, "_permission_mode", None)
    # A request for the pinned mode is what the pin below sets anyway, so it
    # needs no file of its own. A request for a mode that approves on its own
    # (``auto``) cannot be kept here, but the pin moves it to the pinned mode, so
    # the session keeps Crew's tools in the asking mode rather than losing them all.
    # It says so once, on the log and in the chat (``claude_mode_pinned_notice``),
    # when the exclusion below is taken.
    if (
        requested
        and requested != _CLAUDE_PINNED_MODE
        and requested not in _CLAUDE_GATE_ESCAPING_MODES
    ):
        logger.warning(
            "%s %s and this session requested permission mode "
            "%r, which cannot be pinned without that file; the session runs without "
            "Crew's MCP tools.",
            local_settings,
            whose,
            self._permission_mode,
        )
        return False
    try:
        inline = json.loads(payload)
    except ValueError:
        return False
    if not isinstance(inline, dict):
        return False
    project_rules: list[tuple[str, str]] = []
    for name in _CLAUDE_PROJECT_SETTINGS_FILES:
        found = _project_settings_restrictions(local_settings.parent / name)
        if found is None:
            logger.warning(
                "%s %s and %s could not be examined, or sets "
                "hooks or sandbox settings, so leaving the project tiers out could "
                "drop a restriction; the session runs without Crew's MCP tools.",
                local_settings,
                whose,
                local_settings.parent / name,
            )
            return False
        project_rules.extend(found)
    if project_rules:
        permissions = inline.setdefault("permissions", {})
        if not isinstance(permissions, dict):
            return False
        for kind in _CLAUDE_CARRIED_PERMISSION_KEYS:
            current = permissions.get(kind)
            merged = list(current) if isinstance(current, list) else []
            merged.extend(r for k, r in project_rules if k == kind and r not in merged)
            if merged:
                permissions[kind] = merged
    self._claude_inline_settings = inline
    self._claude_local_settings_excluded = True
    if requested in _CLAUDE_GATE_ESCAPING_MODES:
        logger.warning(
            "%s %s and this session requested permission mode %r, which cannot be "
            "kept without that file; the session runs with Crew's MCP tools in %r "
            "instead. To get %r back, remove that file from this folder (or let "
            "Crew own it), or use the Kiro CLI harness.",
            local_settings,
            whose,
            requested,
            _CLAUDE_PINNED_MODE,
            requested,
        )
        self.claude_mode_pinned_notice = _CLAUDE_MODE_PINNED_NOTICE.format(mode=requested)
    logger.warning(
        "%s %s; leaving it untouched and out of this session's "
        "setting sources, with Crew's settings and the project's %d deny/ask rule(s) "
        "carried inline instead. The project tiers do not load otherwise, so this "
        "session runs without the repository's env, plugins and CLAUDE.md.",
        local_settings,
        whose,
        len(project_rules),
    )
    return True


def _claude_adapter_below_setting_sources_floor(self: ClaudeAcpClient) -> bool:
    """Whether the adapter may ignore ``settingSources``.

    After a handshake, judged on the ``agentInfo.version`` it reported. Before
    one, on the version the claude adapter's launch (``ClaudeLaunch.resolve_spawn``)
    read before the settings writer runs (``_claude_adapter_disk_version``): the
    package's own, or for a wrapper script what it reported at its latest
    handshake. Unknown reads as below the floor, so the exclusion is never taken
    on a guess.
    """
    if getattr(self, "_agent_version_read", False):
        version = getattr(self, "_agent_version", "")
    else:
        version = getattr(self, "_claude_adapter_disk_version", "")
    return not _claude_adapter_honours_setting_sources(version)


def _note_claude_adapter_version(self: ClaudeAcpClient) -> None:
    """Record the ``agentInfo.version`` this session's adapter argv reported at the handshake.

    Kept for the process (``harness.claude._claude_adapter_handshake_versions``), so a
    later pre-spawn check can read the version a wrapper script runs. An empty
    report is recorded as well: the latest handshake is the answer.
    """
    argv = getattr(self, "_claude_adapter_argv", None)
    if argv:
        claude_mod._claude_adapter_handshake_versions[argv] = self._agent_version


def _claude_session_needs_options(self: ClaudeAcpClient) -> bool:
    """Whether this session is only safe on an adapter that honours the session options.

    Its project tier is left out, it is a Side Chat, its spec declares
    ``"tools": []``, or its worker's built-in tools are narrowed: each of those
    rides ``_meta.claudeCode.options``, which an adapter below the floor may ignore.
    """
    return bool(
        self._claude_session_excludes_local()
        or getattr(self, "_side_read_only", False)
        or getattr(self, "_spec_zero_tools", False)
        or getattr(self, "_claude_worker_builtin_tools", None) is not None
    )


def _refuse_claude_session_below_floor_before_spawn(
    self: ClaudeAcpClient, installed_version: str
) -> None:
    """Refuse such a session before its process starts, when the installed adapter is too old.

    ``ClaudeLaunch.resolve_spawn`` reads the installed package's own version before
    the spawn and passes it as *installed_version*. When that is a known version
    below :data:`CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION`, the handshake check in
    :meth:`_pin_claude_starting_mode` would stop the session anyway, but only after
    an adapter and a Claude Code process had started for nothing. A version that
    could not be read (a wrapper, an override) keeps that handshake check, even when
    an earlier handshake of the same argv was remembered: a session refused here
    never reaches a handshake, so a remembered version could never be corrected.

    :class:`AcpToolGateUnroutable`, so ``ensure_ready`` does not retry it with a
    fresh process: a respawn reads the same version and refuses again.
    """
    version = installed_version
    if not version or _claude_adapter_honours_setting_sources(version):
        return
    if not self._claude_session_needs_options():
        return
    floor = ".".join(str(part) for part in CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION)
    # Two sentences, for the person reading it in a chat: what happened, then the
    # command that fixes it.
    raise AcpToolGateUnroutable(
        f"claude-agent-acp {_scrub_observed(version)} is not verified to honour "
        f"settingSources or the other session options this chat needs ({floor} or newer "
        "is), so the chat was not started. Upgrade it, then start a new chat: "
        f"npm i -g {claude_mod.CLAUDE_ACP_NPM_PKG}",
        transient=False,
        user_worded=True,
    )


def _log_claude_adapter_below_floor(
    self: ClaudeAcpClient, local_settings: Path, whose: str
) -> None:
    floor = ".".join(str(part) for part in CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION)
    logger.warning(
        "%s %s and claude-agent-acp reported version %r, not "
        "verified to honour settingSources (needs %s or newer), so the file cannot "
        "be left out; the session runs without Crew's MCP tools. Upgrade with "
        "'npm i -g %s'.",
        local_settings,
        whose,
        _scrub_observed(getattr(self, "_agent_version", "")),
        floor,
        claude_mod.CLAUDE_ACP_NPM_PKG,
    )


async def _prepare_claude_side_read_denies(self: ClaudeAcpClient) -> None:
    """Build a Side Chat session's sensitive-path deny rules before its launch, off the loop.

    A no-op on any other session. Fail closed: rules that cannot be built (a
    root that cannot be resolved) stop the launch, so the session never starts
    without them.
    """
    if not getattr(self, "_side_read_only", False):
        return
    try:
        rules = await asyncio.to_thread(_claude_side_read_deny_rules)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AcpError(
            "the sensitive-path deny rules of a claude Side Chat session could not "
            f"be built ({type(exc).__name__}), so the session was not started"
        ) from exc
    self._claude_side_read_denies = rules


async def _prepare_claude_held_asks(self: ClaudeAcpClient) -> None:
    """Read the held-limit asks of the Bot behind this session off the loop, before its envelope.

    ``mate_held_rules.claude_ask_rules`` reads the migration ledger and the
    session's execution record from disk. Any session attributed to a Bot with
    held limits gets them, its worker's and a helper it spawned alike; empty on
    every other session.
    """
    from kiro_crew import mate_held_rules

    key = getattr(self, "_session_key", None) or ""
    if not key:
        self._claude_held_asks = ()
        self._claude_held_mcp = False
        return
    servers: list[str] = [
        str(e["name"])
        for e in getattr(self, "_session_mcp_cache", None) or []
        if isinstance(e, dict) and isinstance(e.get("name"), str)
    ]
    asks = await asyncio.to_thread(mate_held_rules.claude_ask_rules, key, servers)
    self._claude_held_asks = tuple(asks)
    self._claude_held_mcp = await asyncio.to_thread(mate_held_rules.claude_holds_mcp, key)


def _bridged_server_asks(client: ClaudeAcpClient) -> list[str]:
    """One ``permissions.ask`` rule per Crew-owned Connection this session's array bridges."""
    session_key = getattr(client, "_session_key", None) or ""
    elements = getattr(client, "_session_mcp_cache", None) or []
    if not session_key or not elements:
        return []
    from kiro_crew.connections import crew_oauth_proxy

    names = [e.get("name") for e in elements if isinstance(e, dict)]
    return [
        f"mcp__{name}"
        for name in names
        if isinstance(name, str) and crew_oauth_proxy.is_connection_server(session_key, name)
    ]


def _claude_session_meta(self: ClaudeAcpClient) -> dict[str, Any]:
    """The ``_meta`` envelope for a claude ``session/new`` or ``session/load``.

    Empty options, unless this session leaves a project-owned
    ``settings.local.json`` out: then ``settingSources`` is ``user`` alone,
    Crew's payload rides ``settings``, and ``allowDangerouslySkipPermissions``
    is off so no settings file can start the session in
    ``bypassPermissions``. The exclusion is read from the
    projection's record as well as the live flag, so an array delivered
    under it always ships beside it; excluding the tier is never the
    widening direction.

    A session whose spec declares ``"tools": []`` (``_spec_zero_tools``, the
    background ``kirocrew-lite`` helpers) mounts no tool at all: ``tools`` is
    empty, so no built-in exists for an allow rule in any settings file to
    pre-approve, ``strictMcpConfig`` keeps the harness's own user, project and
    plugin servers out, and bypass is off. The wire array it ships beside must be
    empty too, which :func:`refuse_claude_zero_tool_mounts` checks first.

    A Bot's worker session (``members.is_crewmate_worker_session``) gets
    ``strictMcpConfig`` too, so a worker runs only the servers its spec grants.
    One whose crewmate file excludes a native tool gets ``tools`` limited to the
    built-ins of the native tools it keeps (``SessionProjection.builtin_tools``).

    A Side Chat session (``side_read_only``) also gets the read-only shape:
    ``tools`` limited to the read built-ins, ``strictMcpConfig``, bypass off,
    and inline settings that add an ``ask`` rule per read built-in, deny the host's
    sensitive paths to the read tools (:func:`_claude_side_read_deny_rules`)
    and turn settings hooks off. Its setting sources are not narrowed further,
    because the user's settings carry the provider routing and model ids the
    session needs; the deny rules are what keep an allow rule or an
    additional directory there from reaching a sensitive path. A side session
    with no deny rules to carry raises ``AcpError`` instead of starting.

    An incognito or temporary session (``memory_mode`` not ``persistent``), and a
    sub-agent spawned with ``include_memory=false`` (``_include_memory``), gets
    ``CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`` in ``env`` and in ``settings.env``, so
    Claude Code's auto-memory neither reads nor writes the project's memory
    files. The adapter spreads ``env`` over its own process env, so nothing it
    sets there is lost; ``settings.env`` is the flag tier, which Claude Code
    applies after the user, project and local tiers' ``env``, so none of those
    can set the switch back.

    Beside ``options``, a backend in ``ACP_BACKENDS_RAW_SDK_TASKS`` asks for the raw SDK
    messages of its background tasks and API retries, ``emitRawSDKMessages``
    (``acp.raw_sdk_tasks.RAW_SDK_MESSAGE_FILTER``).
    """
    options: dict[str, Any] = {}
    excluded = getattr(self, "_claude_local_settings_excluded", False) or getattr(
        self, "_session_mcp_excludes_local", False
    )
    if excluded:
        options["settingSources"] = list(_CLAUDE_CREW_GOVERNED_SETTING_SOURCES)
        options["allowDangerouslySkipPermissions"] = False
        inline = getattr(self, "_claude_inline_settings", None)
        if not isinstance(inline, dict):
            inline = getattr(self, "_session_mcp_inline_settings", None)
        if isinstance(inline, dict) and inline:
            options["settings"] = dict(inline)
    adopted = getattr(self, "_adopted_substitute_model", None)
    if adopted and adopted == self._model:
        # The model a substitution retry names rides the flag tier as well as the
        # seed: a seed a sibling session shares is not rewritten, and the flag tier
        # outranks the user's settings the refused default came from.
        carried_settings = dict(options.get("settings") or {})
        carried_settings["model"] = adopted
        options["settings"] = carried_settings
    # The spec's shell and write allowlists' ask rules ride the envelope whether or
    # not ``settings.local.json`` is Crew's: a declined project file still loads,
    # and its allow rules would otherwise approve those calls without a request.
    # Built from the allowlists this session's requests are stamped with.
    allowlist_asks = claude_ask_rules_for_allowlists(
        getattr(self, "_spec_shell_allowlist", None),
        getattr(self, "_spec_write_allowlist", None),
    )
    if allowlist_asks:
        carried_settings = dict(options.get("settings") or {})
        carried_permissions = dict(carried_settings.get("permissions") or {})
        ask = list(carried_permissions.get("ask") or [])
        ask.extend(rule for rule in allowlist_asks if rule not in ask)
        carried_permissions["ask"] = ask
        carried_settings["permissions"] = carried_permissions
        options["settings"] = carried_settings
    bridged = _bridged_server_asks(self)
    if bridged:
        # A Crew-owned Connection's call reaches the vendor only against the
        # approval minted from its permission request, so an allow rule in the
        # user's or project's settings must not pre-approve it: ask outranks allow.
        carried_settings = dict(options.get("settings") or {})
        carried_permissions = dict(carried_settings.get("permissions") or {})
        ask = list(carried_permissions.get("ask") or [])
        ask.extend(rule for rule in bridged if rule not in ask)
        carried_permissions["ask"] = ask
        carried_settings["permissions"] = carried_permissions
        options["settings"] = carried_settings
        options["allowDangerouslySkipPermissions"] = False
    if getattr(self, "_spec_zero_tools", False):
        # The spec declares ``"tools": []`` (the background ``kirocrew-lite``
        # helpers). Without ``tools`` the adapter mounts Claude Code's whole
        # built-in preset, and an allow rule in the user's or the project's
        # settings runs a call there unasked, so no permission request reaches
        # Crew. Without ``strictMcpConfig`` it mounts the user's, the project's
        # and plugins' servers beside the wire array.
        options["tools"] = []
        options["strictMcpConfig"] = True
        options["allowDangerouslySkipPermissions"] = False
    from kiro_crew.members import is_crewmate_worker_session

    if is_crewmate_worker_session(getattr(self, "_session_key", None)):
        # A Bot's worker runs the servers its per-session spec grants, which the
        # wire array already carries. Without this the harness also starts every
        # server in the user's own config, the project's ``.mcp.json`` and
        # plugins, once per worker.
        options["strictMcpConfig"] = True
    if getattr(self, "_claude_held_mcp", False):
        # The held ``mcp__<server>`` asks name only the servers Crew sends: a
        # server from the user's own config, the project's or a plugin's, which
        # an allow rule there may pre-approve, would run unasked beside them.
        options["strictMcpConfig"] = True
    # Read off the loop before the envelope is built (_prepare_claude_held_asks).
    held_asks = getattr(self, "_claude_held_asks", ())
    if held_asks:
        # The limits a Bot's file could not carry, on any session attributed to
        # that Bot: each call they cover must reach the gate, which asks a person,
        # instead of running unasked.
        carried_settings = dict(options.get("settings") or {})
        carried_permissions = dict(carried_settings.get("permissions") or {})
        ask = list(carried_permissions.get("ask") or [])
        ask.extend(rule for rule in held_asks if rule not in ask)
        carried_permissions["ask"] = ask
        carried_settings["permissions"] = carried_permissions
        options["settings"] = carried_settings
        options["allowDangerouslySkipPermissions"] = False
    worker_tools = getattr(self, "_claude_worker_builtin_tools", None)
    if worker_tools is not None:
        # A worker whose crewmate file excludes a native tool keeps only the
        # built-ins of the native tools it keeps; a narrower list already set
        # (``"tools": []``) stays as narrow.
        options["tools"] = [t for t in options.get("tools", worker_tools) if t in worker_tools]
    if getattr(self, "_side_read_only", False):
        # A Side Chat session: only the read built-ins exist, so no allow rule
        # in the user's settings can pre-approve a write. No MCP server either
        # (the wire array is empty, and strictMcpConfig keeps out the user's,
        # the project's and plugins' servers). Settings and plugin hooks are
        # shell commands claude runs unprompted, so they are off, as in the
        # kiro-cli derived spec. A user's PreToolUse deny hook is off with them.
        options["tools"] = list(CLAUDE_READ_ONLY_BUILTINS)
        options["strictMcpConfig"] = True
        options["allowDangerouslySkipPermissions"] = False
        side_settings = dict(options.get("settings") or {})
        side_permissions = dict(side_settings.get("permissions") or {})
        ask = list(side_permissions.get("ask") or [])
        ask.extend(rule for rule in _CLAUDE_SIDE_READ_ONLY_ASK_RULES if rule not in ask)
        side_permissions["ask"] = ask
        read_denies = getattr(self, "_claude_side_read_denies", ())
        if not read_denies:
            raise AcpError(
                "a claude Side Chat session has no sensitive-path deny rules to "
                "carry, so it was not started"
            )
        deny = list(side_permissions.get("deny") or [])
        carried = set(deny)
        deny.extend(rule for rule in read_denies if rule not in carried)
        side_permissions["deny"] = deny
        side_settings["permissions"] = side_permissions
        side_settings["disableAllHooks"] = True
        options["settings"] = side_settings
    if self.memory_mode != "persistent" or not getattr(self, "_include_memory", True):
        # Claude Code's auto-memory reads the project's MEMORY.md into the
        # session and writes memory files with no permission request. The
        # adapter spreads this env over its own process env. Claude Code then
        # copies each settings tier's ``env`` over the process env, so a user
        # or project ``env`` could set the switch back; the flag tier
        # (``settings``) is copied last, so it carries the switch too. Copied,
        # never mutated: the inline and Side Chat settings are shared dicts.
        options.setdefault("env", {})[_CLAUDE_DISABLE_AUTO_MEMORY_ENV] = "1"
        flag = dict(options.get("settings") or {})
        flag["env"] = {**dict(flag.get("env") or {}), _CLAUDE_DISABLE_AUTO_MEMORY_ENV: "1"}
        options["settings"] = flag
    # What this envelope ships, recorded as it is built: the session freezes
    # these deny and ask rules at creation, and a later re-seed (``_reseed_after_capture``)
    # moves the live inline settings without reaching the session.
    settings = options.get("settings")
    permissions = settings.get("permissions") if isinstance(settings, dict) else None
    shipped: set[tuple[str, str]] = set()
    for kind in _CLAUDE_CARRIED_PERMISSION_KEYS:
        rules = permissions.get(kind) if isinstance(permissions, dict) else None
        if isinstance(rules, list):
            shipped.update((kind, rule) for rule in rules if isinstance(rule, str))
    self._claude_shipped_restrictions = frozenset(shipped)
    claude_code: dict[str, Any] = {"options": options}
    raw_filter = session_meta_filter(self.backend)
    if raw_filter is not None:
        claude_code["emitRawSDKMessages"] = raw_filter
    return {"claudeCode": claude_code}


def refuse_claude_zero_tool_mounts(spec_zero_tools: bool, mcp_servers: Any) -> None:
    """Refuse a zero-tool claude session whose wire array mounts a server.

    On a session whose spec declares ``"tools": []``, :func:`_claude_session_meta`
    removes every built-in and keeps the harness's own servers out, so the wire
    array is the one channel left that could mount a tool. The mirror withholds
    every server such a spec does not reference, so a non-empty array is a
    composition defect: ``AcpError``, and the session is not started.
    """
    if spec_zero_tools and mcp_servers:
        raise AcpError(
            'a claude session whose spec declares "tools": [] was composed with '
            f"{len(mcp_servers)} MCP server(s), so it was not started"
        )


def _claude_session_excludes_local(self: ClaudeAcpClient) -> bool:
    """Whether this session's array was delivered under the exclusion."""
    return bool(
        getattr(self, "_claude_local_settings_excluded", False)
        or getattr(self, "_session_mcp_excludes_local", False)
    )


async def _claude_set_mode(self: ClaudeAcpClient, session_id: str | None, mode_id: str) -> None:
    """Send ``session/set_mode`` for this session and wait for its answer.

    The one transport call the starting-mode pin makes, named so the pin reads
    no transport of its own: a driver that serves this session over another
    connection answers it there. On ``AcpClient`` it is this client's own
    request on its own process.
    """
    req_id = await self._send_request(METHOD_SET_MODE, {"sessionId": session_id, "modeId": mode_id})
    await self._wait_for_response(req_id, timeout=_INIT_TIMEOUT, method=METHOD_SET_MODE)


async def _claude_stop_session(self: ClaudeAcpClient) -> None:
    """Stop THIS session before its first prompt, because a check it depends on failed.

    The pin and the project-deny re-read call this and never stop anything
    themselves, so what "stop" reaches is the driver's choice. ``AcpClient``
    runs one process per session, so stopping the session is killing that
    process.
    """
    await self._kill_process(force=True)


async def _verify_claude_project_denies_unchanged(self: ClaudeAcpClient) -> None:
    """Stop a session whose project files gained a restriction after it was read.

    The deny and ask rules the exclusion carried were read by the writer, before
    ``session/new``; the session froze them into its inline settings when it
    was created. Re-reading both project files now, before the first prompt,
    closes that window: every deny or ask rule either file holds must be among the ones
    the envelope shipped (recorded by :meth:`_claude_session_meta` as it was
    built, not re-derived: a re-seed since then never reached the session).
    A file that gained one, or that cannot be examined now, stops the
    harness. A change after this point is the residual the spec names: the
    session's settings are fixed at creation, and the next session reads
    the file again.
    """
    carried: frozenset[tuple[str, str]] = getattr(self, "_claude_shipped_restrictions", frozenset())
    directory = self._claude_local_settings_path().parent

    def _gained() -> bool:
        for name in _CLAUDE_PROJECT_SETTINGS_FILES:
            found = _project_settings_restrictions(directory / name)
            if found is None or not set(found) <= carried:
                return True
        return False

    if not await asyncio.to_thread(_gained):
        return
    await self._claude_stop_session()
    raise AcpError(
        "a project .claude settings file gained deny or ask rules (or became unreadable) "
        "while a claude session that leaves it out was starting; the session was "
        "stopped so that no deny it carries goes unenforced"
    )


async def _pin_claude_starting_mode(self: ClaudeAcpClient, resp: dict) -> None:
    """Hold a claude session that carries Crew's tools to the asking mode.

    The mode is read back from the response, never from the file: the
    ``modes.currentModeId`` claude-agent-acp returns is the mode the session
    really started in, whatever the file held at any earlier moment. Only a
    mode in :data:`_CLAUDE_GATE_ESCAPING_MODES`, or a response that names no
    mode, is pinned: ``session/set_mode`` moves it to
    :data:`_CLAUDE_PINNED_MODE`, and the call must succeed. A stricter or
    narrower mode the operator chose (``plan``, ``acceptEdits``) is kept,
    never widened. No prompt has run yet, so no tool call can happen in
    between. A pin that fails stops the harness: the session never runs with
    Crew's tools under a mode that approves on its own.

    Runs on the exclusion path (:meth:`_claude_session_excludes_local`) and
    on a Side Chat session (``side_read_only``), whose READ_ONLY gate has no
    approver behind it. Any other session keeps the mode it started in unless
    ``KIROCREW_CC_PIN_INHERITED_MODE`` is on (``resolve_cc_pin_inherited_mode``,
    off by default): a user ``~/.claude`` mode reaching it is the
    inherited-config gap the spec names. With the setting on, the pin also
    runs on every session whose start sent Crew's MCP array; a session that
    sent none is left alone. There a reported mode that is the one Crew
    requested (the ``auto`` opt-in) is the session's own and is kept; any
    other approving mode is pinned, so a requested ``auto`` that resumes in
    ``bypassPermissions`` is moved too. With no request the seed names no
    ``defaultMode``, so a ``session/new`` starts in the mode the user's own
    settings name (a ``~/.claude`` ``auto``, for one). A ``session/load``
    starts in the mode its transcript was left in, whatever was requested.

    The exclusion is never taken when Crew requested a mode of its own, or
    when the installed adapter is below
    :data:`CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION`, and Side Chat runs no
    tools on such an adapter; if the adapter that answered the handshake
    reports a version below it anyway, an excluded or Side Chat session is
    stopped here. A session the setting brings in keeps the mode it started
    in on such an adapter, because the pin has not been checked on an
    adapter older than 0.84.0. A session that starts in a mode that asks
    gains no call, and the kiro path never reaches this method
    (harness-parity H13).

    A session whose spec declares ``"tools": []`` is stopped below the same
    floor, since its empty ``tools`` option is what removes the built-ins, but
    is not pinned: it mounts no tool, so its mode approves nothing. A crewmate
    worker whose native-tool excludes narrow ``tools``
    (``_claude_worker_builtin_tools``) is stopped below the floor for the same
    reason; above it, its mode is handled as any other session's.

    Observed on claude-agent-acp 0.84.0
    (``test/fixtures/claude_mode_pin/``): ``session/new`` and
    ``session/load`` report ``modes.currentModeId`` (a project ``plan``
    starts in ``plan``; a project ``auto`` is filtered and starts in
    ``default``), and ``session/set_mode`` ``default`` answers ``{}`` from
    ``auto``.

    On the exclusion path the project files' deny and ask rules are first
    re-read (:meth:`_verify_claude_project_denies_unchanged`), closing the
    window between the writer's read and the session's creation.
    """
    excludes_local = self._claude_session_excludes_local()
    side = getattr(self, "_side_read_only", False)
    zero_tools = getattr(self, "_spec_zero_tools", False)
    depends_on_options = excludes_local or side or zero_tools
    narrowed = getattr(self, "_claude_worker_builtin_tools", None) is not None
    pin_inherited = not depends_on_options and resolve_cc_pin_inherited_mode()
    if not (depends_on_options or narrowed or pin_inherited):
        return
    below_floor = self._claude_adapter_below_setting_sources_floor()
    if below_floor and self._claude_session_needs_options():
        # The adapter that answered the handshake is below the floor, or named no
        # version: it may have ignored the session's options. A fresh process
        # answers the same, so the refusal is not retried.
        await self._claude_stop_session()
        floor = ".".join(str(part) for part in CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION)
        raise AcpToolGateUnroutable(
            "The claude-agent-acp that answered is not verified to honour settingSources "
            f"or the other session options this chat needs ({floor} or newer is), so "
            "the chat was stopped before its first prompt. Upgrade it, then start a new "
            "chat: "
            f"npm i -g {claude_mod.CLAUDE_ACP_NPM_PKG}",
            transient=False,
            user_worded=True,
        )
    # The array this start sent. The re-seed after the capture cleared the
    # projection cache, so deriving it again here would read the spec and the
    # settings files on the event loop.
    if not depends_on_options and (
        below_floor or not pin_inherited or not getattr(self, "_mcp_mount_servers", None)
    ):
        return
    if zero_tools and not (excludes_local or side):
        # A zero-tool session mounts no tool, so its mode has nothing to approve.
        return
    if excludes_local:
        await self._verify_claude_project_denies_unchanged()
    session_id = resp.get("sessionId") or self._session_id
    modes = resp.get("modes")
    current = modes.get("currentModeId") if isinstance(modes, dict) else None
    requested = None if (excludes_local or side) else self._permission_mode
    if isinstance(current, str) and (
        current not in _CLAUDE_GATE_ESCAPING_MODES or current == requested
    ):
        return
    logger.warning(
        "claude session started in mode %r, which approves calls without asking "
        "Crew's gate or was not reported; pinning %r before the first prompt",
        _scrub_observed(current if isinstance(current, str) else ""),
        _CLAUDE_PINNED_MODE,
    )
    try:
        await self._claude_set_mode(session_id, _CLAUDE_PINNED_MODE)
    except (AcpError, AcpTimeoutError) as exc:
        await self._claude_stop_session()
        raise AcpError(
            "could not pin the asking permission mode on a claude session that "
            "started in a mode which approves on its own, or reported none; the "
            "session was stopped"
        ) from exc


def _claude_session_mcp_servers(self: ClaudeAcpClient) -> list:
    """MCP server array passed to a claude ``session/new`` / ``session/load``.

    Overridable seam, FILLED for the public build. The claude-agent-acp adapter
    does not read ``kirocrew.mcp.json`` on its own, so without this array a claude
    session had zero MCP tools. The harness itself worked -- prompts, streaming,
    permissions -- but Crew's own tools were absent, with no error anywhere.

    The translation lives in the mirror
    (:mod:`kiro_crew.providers.mirrors.claude_code`), not here: projecting the
    agent spec onto a backend's native shape is one named contract with one
    implementation per backend, rather than a per-harness override each backend
    author rediscovers. See ``docs/request-for-change/rfc-agent-config-mirror.md``.

    The seam is deliberately KEPT rather than replaced by a capability-set call:
    an edition already overrides this method, and swapping the call site for a
    set membership test would silently stop calling that override.

    In-memory only. The spawn path warms ``_session_mcp_cache`` off the loop, so
    this accessor adds no scheduling or failure point to the shared call site
    (harness-parity H13).
    """
    return self._session_mcp_servers()


def _harness_session_mcp_servers(self: ClaudeAcpClient) -> list:
    """The servers a claude session's MCP array carries ahead of the shared entries.

    A claude Side Chat session mounts no server: READ_ONLY refuses every MCP call,
    and a tool that is not mounted cannot be pre-approved by a settings allow rule.
    Checked here, around the seam rather than in it, so an edition's override of
    :meth:`_claude_session_mcp_servers` cannot mount servers there.
    """
    if getattr(self, "_side_read_only", False):
        return []
    return self._claude_session_mcp_servers()


def _harness_session_meta(self: ClaudeAcpClient, mcp_servers: Any) -> dict[str, Any]:
    """The ``_meta`` envelope a claude ``session/new`` or ``session/load`` sends.

    Built AFTER the array it travels with: the envelope carries the setting sources
    that array was authorized under, and a zero-tool session whose array mounts a
    server is refused here (:func:`refuse_claude_zero_tool_mounts`).
    """
    refuse_claude_zero_tool_mounts(getattr(self, "_spec_zero_tools", False), mcp_servers)
    return self._claude_session_meta()


async def _retry_after_model_substitution(
    self: ClaudeAcpClient, new_params: dict[str, Any], session_resp: dict, substitute: str
) -> dict:
    """Name a served model in the seed and re-issue ``session/new`` once after a substitution advisory.

    The admin-tier policy advisory ("Model X is restricted ... Using Y instead.")
    comes back as a ``-32603`` error frame with no sessionId, so the first attempt
    created nothing. ``_wait_for_response`` recorded *substitute* in
    ``self._last_substitution_model``. The refused model is the one the seed left
    to the user's settings, not necessarily this session's pin, so the retry names
    the pin when the backend serves it and *substitute* otherwise
    (``_substitution_retry_model``). This sets ``self._model`` to that model,
    re-seeds ``settings.local.json`` with it (the adapter builds a fresh
    SettingsManager per ``session/new``, so it takes effect), rebuilds the array
    and the envelope, and re-issues. When the seed already names that model,
    the backend refused it as named and a retry cannot help: *session_resp* is
    returned unchanged. When the retry named the pin and the backend refuses that
    too, it falls back ONCE to the model the second advisory offers. Returns the
    last response, which the caller still treats as a failure when it carries no
    sessionId.
    """
    model = self._substitution_retry_model(substitute)
    if model == getattr(self, "_adopted_substitute_model", None):
        return session_resp
    resp = await self._reissue_naming_model(new_params, model)
    fallback = self._last_substitution_model
    if resp.get("sessionId") or model == substitute or not fallback or fallback == model:
        return resp
    # The served pin was refused as well. A session on the model the backend
    # offers beats one that never starts, so fall back to it once; the seed then
    # names it, and nothing retries again.
    return await self._reissue_naming_model(new_params, fallback)


async def _reissue_naming_model(
    self: ClaudeAcpClient, new_params: dict[str, Any], model: str
) -> dict:
    """Re-seed ``settings.local.json`` naming *model*, rebuild the array and envelope, re-issue."""
    # Redact the substitute name before logging. It is parsed straight from the
    # untrusted ACP backend advisory (data.details "Using X instead") and the
    # gateway log fans out to the dashboard activity feed and Slack -- mirror the
    # dual-redaction discipline applied to the source advisory in _wait_for_response.
    _sub_log, _ = redact_exfiltration_urls(str(model))
    _sub_log, _ = redact_credentials(_sub_log)
    logger.warning(
        "ACP session/new returned a substitution advisory with no session; "
        "adopting gateway-served model %r and retrying session creation.",
        _sub_log,
    )
    self._model = model
    # Named so the re-seed writes ``model`` warm cache or cold: without it the
    # user's own settings still name the restricted model and the retry is
    # refused the same way.
    self._adopted_substitute_model = model
    # Re-seed settings.local.json so the fresh SettingsManager the adapter builds
    # for the retry resolves that model (it merges settings sources each
    # session/new).
    try:
        await asyncio.to_thread(self._write_claude_local_settings)
    except (OSError, ValueError, TypeError):
        # Narrow to realistic re-seed failure modes: OSError covers disk /
        # permission errors on the atomic write; ValueError and TypeError cover
        # registry / json shape surprises. Never let re-seed failure mask the
        # retry -- worst case, the adapter resolves to whatever it had cached and
        # we still retry session/new on the substitute path.
        logger.warning("re-seed of settings.local.json failed", exc_info=True)
    # The writer owns every transition in the native permission surface and
    # invalidates the frozen MCP projection when that state changes. Re-resolve off
    # the event loop, then rebuild the exact array the retry will send so a failed
    # byte re-validation withholds rather than re-delivering the first attempt's
    # stale roster.
    resolved_mcp = await asyncio.to_thread(self._resolve_session_mcp_servers)
    self._session_mcp_cache = resolved_mcp
    new_params["mcpServers"] = [
        *self._harness_session_mcp_servers(),
        *self._adapter_session_mcp_servers(),
        *(await asyncio.to_thread(self._pooled_mcp_servers)),
    ]
    if self.backend in ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP:
        new_params["mcpServers"] = await self._prune_unstartable_mcp(new_params["mcpServers"])
    await self._prepare_claude_held_asks()
    # Rebuilt AFTER the array, from the same re-seed: the envelope carries the
    # setting sources the array was authorized under and the inline settings naming
    # the substitute model.
    new_params["_meta"] = self._harness_session_meta(new_params["mcpServers"])
    # The roster the session starts with: the launch composition when the harness
    # took its servers at launch, else the wire array (``client_start._roster_of``).
    launched = self._launch_mcp_servers
    self._begin_session_report(new_params.get("mcpServers") if launched is None else launched)
    self._guard_unresolved_mcp_refs(new_params.get("mcpServers") if launched is None else launched)
    self._last_substitution_model = None
    retry_id = await self._send_request(METHOD_SESSION_NEW, new_params)
    return await self._wait_for_response(
        retry_id,
        timeout=_INIT_TIMEOUT,
        method=METHOD_SESSION_NEW,
        expected_mcp=new_params.get("mcpServers"),
    )


async def _fork_session(
    self: ClaudeAcpClient, source_sid: str, message_id: str, capabilities: dict
) -> str | None:
    """Fork *source_sid* up to and including *message_id*: the new sid, or None.

    Only a harness in ``ACP_BACKENDS_FORK_AT_MESSAGE``, at a version that reads
    the ``_meta`` fork point sent here: a fork that ignored it would copy the
    WHOLE session, including everything after *message_id* that the caller is
    discarding.
    """
    session_capabilities = capabilities.get("sessionCapabilities")
    if not (
        self.backend in ACP_BACKENDS_FORK_AT_MESSAGE
        and _adapter_version_at_least(self._agent_version, CLAUDE_ACP_FORK_POINT_MIN_VERSION)
        and isinstance(session_capabilities, dict)
        and isinstance(session_capabilities.get("fork"), dict)
    ):
        return None
    params = {
        "sessionId": source_sid,
        "cwd": await self._session_work_dir(),
        "mcpServers": [],
        "_meta": {"jetbrains": {"air": {"fork": {"version": 1, "messageId": message_id}}}},
    }
    try:
        fork_id = await self._send_request(METHOD_SESSION_FORK, params)
        fork_resp = await self._wait_for_response(fork_id, timeout=_INIT_TIMEOUT)
    except (AcpError, AcpTimeoutError):
        logger.info(
            "session/fork of %s at %s failed, falling back to session/new",
            source_sid,
            message_id,
        )
        return None
    forked_sid = fork_resp.get("sessionId")
    if not isinstance(forked_sid, str) or not forked_sid:
        return None
    logger.info("ACP session forked: %s -> %s at %s", source_sid, forked_sid, message_id)
    return forked_sid


def _local_settings_exclusion(self: ClaudeAcpClient) -> tuple[bool, dict[str, Any] | None]:
    """Whether this session leaves the project's ``settings.local.json`` out, and the
    settings it carries inline instead (:meth:`_exclude_foreign_local_settings`)."""
    excludes_local = bool(getattr(self, "_claude_local_settings_excluded", False))
    return excludes_local, (
        getattr(self, "_claude_inline_settings", None) if excludes_local else None
    )


def _note_worker_builtin_tools(
    self: ClaudeAcpClient, builtin_tools: tuple[str, ...] | None
) -> None:
    """Keep the built-ins a crewmate worker's projection allows, carried as claude's ``tools`` option."""
    self._claude_worker_builtin_tools = builtin_tools


async def _reassert_adapter_resolved_model(self: ClaudeAcpClient) -> None:
    """Send an inheriting claude session the model its adapter reports.

    claude-agent-acp resolves an inheriting session's model from
    ``ANTHROPIC_MODEL`` or the user's ``settings.model`` and reports it as the
    ``model`` option's current value. When that value is the setting verbatim it
    does not pass it on, trusting Claude Code to have read the same setting; a
    resumed session can instead run Claude Code's built-in default while the
    adapter still reports the settings model. A custom gateway that does not
    serve that built-in default then refuses every turn. The report cannot tell
    the two cases apart, so the reported id is sent back over
    ``session/set_config_option``: the write the picker and ``/model`` make,
    which the adapter always passes on to Claude Code.

    Nothing is sent when the reported id is the head of the advertised list:
    with no model setting the adapter reports that entry (its ``default``
    pseudo-model), so there is no setting to re-apply. Nothing is sent for an
    id the list does not carry either, since the adapter would refuse it.

    Best effort: the session started without this write, so a refused value or
    a failed request leaves it on the adapter's own model and only logs. A dead
    process still propagates, because the session cannot continue.
    """
    advertised = self._advertised_model_ids()
    reported = self._resolved_model_id or ""
    if reported not in advertised or reported == advertised[0]:
        return
    _reported_log = redact_log_via_context(reported)
    # An advisory belongs to the request that emitted it (see set_model).
    self._last_substitution_model = None
    try:
        sent = await self._push_model_config_option(reported, strict=False)
    except AcpProcessDied:
        raise
    except AcpError as exc:
        logger.warning(
            "ACP model %s reported by the backend could not be re-applied: %s",
            _reported_log,
            redact_log_via_context(str(exc)),
        )
        return
    if not sent:
        return
    self._resolved_model_id = self._last_substitution_model or sent
    logger.info("ACP model: re-applied %s, resolved by the backend's settings", _reported_log)


async def _ensure_served_default(self: ClaudeAcpClient) -> None:
    """Move an inheriting claude session onto the model its adapter reports
    (:meth:`_reassert_adapter_resolved_model`)."""
    await self._reassert_adapter_resolved_model()


def _note_harness_adapter_version(self: ClaudeAcpClient) -> None:
    """Record the claude-agent-acp version the handshake reported (:meth:`_note_claude_adapter_version`)."""
    self._note_claude_adapter_version()


async def _pin_starting_mode(self: ClaudeAcpClient, resp: dict) -> None:
    """Pin the asking mode on a session that started in one that approves on its own
    (:meth:`_pin_claude_starting_mode`)."""
    await self._pin_claude_starting_mode(resp)


def _session_excludes_local_settings(self: ClaudeAcpClient) -> bool:
    """Whether this session left a project-owned settings file out (:meth:`_claude_session_excludes_local`)."""
    return self._claude_session_excludes_local()
