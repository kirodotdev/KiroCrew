"""The claude-agent-acp session contract an ``AcpClient`` keeps.

Adapter version floors, leaving a project-owned ``settings.local.json`` out of the
setting sources (carrying its deny and ask rules inline instead), the ``_meta`` envelope
of ``session/new``, a Side Chat session's sensitive-path deny rules, and the starting-
mode pin.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_claude_inline_settings``,
``_claude_local_settings_excluded``, ``_claude_shipped_restrictions``,
``_claude_side_read_denies``.

Instance fields read: ``_agent_version``, ``_agent_version_read``, ``_permission_mode``,
``_session_id``, ``_session_mcp_excludes_local``, ``_session_mcp_inline_settings``,
``_side_read_only``.
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
from kiro_crew.acp.transport_errors import AcpError, AcpTimeoutError
from kiro_crew.acp.types import METHOD_SET_MODE
from kiro_crew.security import sandbox_credential_targets
from kiro_crew.user_json import loads_user_json

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


#: The only filesystem setting source a claude session loads when the project owns
#: its own ``settings.local.json``. Sent as ``_meta.claudeCode.options.settingSources``,
#: which claude-agent-acp spreads over its own ``["user", "project", "local"]``
#: default. Neither project tier loads: not ``local`` (the project's own file) and
#: not ``project`` (a checked-in ``.claude/settings.json``), so no rule a repository
#: carries can pre-approve a Crew tool.
_CLAUDE_CREW_GOVERNED_SETTING_SOURCES: tuple[str, ...] = ("user",)

#: The permission mode Crew pins on such a session before its first prompt. It asks
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


def _exclude_foreign_local_settings(self: AcpClient, local_settings: Path, payload: str) -> bool:
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
    permission mode of its own (the pin sets only the asking mode), when
    a project file cannot be examined or sets ``hooks`` or ``sandbox``, or
    when the adapter that
    answered the handshake is below
    :data:`CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION` or reported no version.
    Before the handshake the floor is read from the installed package's own
    version, so the decision is made before the MCP array is first resolved;
    :meth:`_pin_claude_starting_mode` checks the handshake's version too.

    Returns whether the exclusion was taken.
    """
    whose = self._foreign_settings_owner(local_settings)
    if self._claude_adapter_below_setting_sources_floor():
        self._log_claude_adapter_below_floor(local_settings, whose)
        return False
    if getattr(self, "_permission_mode", None):
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


def _claude_adapter_below_setting_sources_floor(self: AcpClient) -> bool:
    """Whether the adapter may ignore ``settingSources``.

    After a handshake, judged on the ``agentInfo.version`` it reported. Before
    one, on the installed package's own version, read by the claude adapter's
    launch (``ClaudeLaunch.resolve_spawn``) before the settings writer runs
    (``_claude_adapter_disk_version``). Unknown reads as below the floor, so the
    exclusion is never taken on a guess.
    """
    if getattr(self, "_agent_version_read", False):
        version = getattr(self, "_agent_version", "")
    else:
        version = getattr(self, "_claude_adapter_disk_version", "")
    return not _claude_adapter_honours_setting_sources(version)


def _log_claude_adapter_below_floor(self: AcpClient, local_settings: Path, whose: str) -> None:
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


async def _prepare_claude_side_read_denies(self: AcpClient) -> None:
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


def _claude_session_meta(self: AcpClient) -> dict[str, Any]:
    """The ``_meta`` envelope for a claude ``session/new`` or ``session/load``.

    Empty options, unless this session leaves a project-owned
    ``settings.local.json`` out: then ``settingSources`` is ``user`` alone,
    Crew's payload rides ``settings``, and ``allowDangerouslySkipPermissions``
    is off so no settings file can start the session in
    ``bypassPermissions``. The exclusion is read from the
    projection's record as well as the live flag, so an array delivered
    under it always ships beside it; excluding the tier is never the
    widening direction.

    A Side Chat session (``side_read_only``) also gets the read-only shape:
    ``tools`` limited to the read built-ins, ``strictMcpConfig``, bypass off,
    and inline settings that add an ``ask`` rule per read built-in, deny the host's
    sensitive paths to the read tools (:func:`_claude_side_read_deny_rules`)
    and turn settings hooks off. Its setting sources are not narrowed further,
    because the user's settings carry the provider routing and model ids the
    session needs; the deny rules are what keep an allow rule or an
    additional directory there from reaching a sensitive path. A side session
    with no deny rules to carry raises ``AcpError`` instead of starting.
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
    return {"claudeCode": {"options": options}}


def _claude_session_excludes_local(self: AcpClient) -> bool:
    """Whether this session's array was delivered under the exclusion."""
    return bool(
        getattr(self, "_claude_local_settings_excluded", False)
        or getattr(self, "_session_mcp_excludes_local", False)
    )


async def _verify_claude_project_denies_unchanged(self: AcpClient) -> None:
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
    await self._kill_process(force=True)
    raise AcpError(
        "a project .claude settings file gained deny or ask rules (or became unreadable) "
        "while a claude session that leaves it out was starting; the session was "
        "stopped so that no deny it carries goes unenforced"
    )


async def _pin_claude_starting_mode(self: AcpClient, resp: dict) -> None:
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

    Runs only on the exclusion path (:meth:`_claude_session_excludes_local`)
    and on a Side Chat session (``side_read_only``), whose READ_ONLY gate
    has no approver behind it. A session whose settings file Crew wrote is
    not pinned: a user ``~/.claude`` mode reaching it is the inherited-config
    gap the spec names, not something this path adds. The exclusion is never
    taken when Crew requested a mode of its own, or when the installed adapter
    is below :data:`CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION`, and Side Chat
    runs no tools on such an adapter; if the adapter that answered the
    handshake reports a version below it anyway, the session is stopped here.
    Any other session gains no call (harness-parity H13).

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
    if not (excludes_local or getattr(self, "_side_read_only", False)):
        return
    if self._claude_adapter_below_setting_sources_floor():
        # The installed package passed the floor, but the adapter that answered
        # the handshake did not: it may have ignored the session's options.
        await self._kill_process(force=True)
        raise AcpError(
            "the claude-agent-acp that answered the handshake is not verified to "
            "honour settingSources or the other session options, so a session "
            "that depends on them was stopped before its first prompt"
        )
    if excludes_local:
        await self._verify_claude_project_denies_unchanged()
    session_id = resp.get("sessionId") or self._session_id
    modes = resp.get("modes")
    current = modes.get("currentModeId") if isinstance(modes, dict) else None
    if isinstance(current, str) and current not in _CLAUDE_GATE_ESCAPING_MODES:
        return
    logger.warning(
        "claude session started in mode %r while carrying Crew's tools; "
        "pinning %r before the first prompt",
        _scrub_observed(current if isinstance(current, str) else ""),
        _CLAUDE_PINNED_MODE,
    )
    try:
        req_id = await self._send_request(
            METHOD_SET_MODE, {"sessionId": session_id, "modeId": _CLAUDE_PINNED_MODE}
        )
        await self._wait_for_response(req_id, timeout=_INIT_TIMEOUT, method=METHOD_SET_MODE)
    except (AcpError, AcpTimeoutError) as exc:
        await self._kill_process(force=True)
        raise AcpError(
            "could not pin the asking permission mode on a claude session that "
            "carries Crew's tools; the session was stopped"
        ) from exc
