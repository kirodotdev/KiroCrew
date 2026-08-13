"""MCP server discovery — detects configured MCP servers and checks liveness.

Scans the agent config (``agents/defaults.json``) for ``mcpServers`` entries,
then optionally probes each server by spawning the command and sending an
MCP ``initialize`` handshake.

Used by the dashboard to show live MCP server badges and by the heartbeat
to auto-sync newly discovered servers into the agent config.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import ntpath
import os
import posixpath
import re
import shutil
import signal
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from kiro_crew import platform_compat
from kiro_crew.config.paths import data_home, kiro_agents_dir
from kiro_crew.env import (
    denied_spec_env_keys,
    emit_env,
    sanitize_spec_env,
    spec_env_path,
    spec_path_key,
)
from kiro_crew.hooks import safe_read_file
from kiro_crew.mcp_provenance import ABSENT, resolve_write
from kiro_crew.mcp_utils import kiro_entry_client_id, kiro_entry_scopes, mcp_server_alias
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    create_subprocess_limited,
    sandboxed_spawn_argv,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# How long to wait for MCP handshake before marking server as unreachable.
# Configurable via dashboard.mcp_probe_timeout_secs in <config_dir>/config.json.
_PROBE_TIMEOUT_SECS = 15  # fallback if config not loaded yet

# Teardown budget for a probed child, paid TWICE on a server that ignores a
# closed stdin: once waiting for a graceful exit, then again after SIGKILL. A
# server that hangs rather than exiting therefore costs 2x this before the
# process-group reap runs, which is why it is a named constant -- tests that
# deliberately probe a never-exiting child shrink it instead of waiting it out.
_PROBE_TEARDOWN_WAIT_SECS = 5

# Cap on a probe error string stored on server.error and surfaced by doctor /
# the dashboard. Sized to hold a full SandboxUnavailableError, whose message
# ends with the ~400-char remedy sentence naming
# agent.sandbox_allow_unsandboxed_exec; the old 200-char cap chopped that tail
# mid-word, so a Windows user saw "…Probe detail: not Linux. I" and no fix.
_PROBE_ERROR_MAX_CHARS = 1200


def _sanitize_probe_error(exc: BaseException) -> str:
    """Redact THEN truncate a probe exception for server.error / doctor / logs.

    A probe exception can carry untrusted, credential-bearing text — e.g. a
    malformed remote MCP URL with an embedded token in the message. Redact
    before truncating (the stderr-tail path already does), so raising the cap to
    hold the sandbox remedy sentence never widens a credential-disclosure hole.
    """
    text = str(exc)
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text[:_PROBE_ERROR_MAX_CHARS]


def _get_probe_timeout() -> int:
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load().dashboard.mcp_probe_timeout_secs
    except Exception:
        return _PROBE_TIMEOUT_SECS


# Probe results expire after 30 minutes → status becomes "outdated"
_PROBE_TTL_SECS = 1800

# An MCP command that does not resolve is a STABLE fact: it stays unresolved
# until someone edits the config or installs the binary, yet the probe re-runs
# on every discovery cycle and re-emits an identical warning each time. A config
# carried between machines (a Linux dev box's servers opened on a Mac, say)
# therefore prints the same handful of warnings forever, burying the transient
# failures that actually deserve attention.
#
# Warn the FIRST time a given (server, command) fails to resolve and demote the
# repeats to DEBUG. Deliberately scoped to unresolvable commands only —
# timeouts and handshake errors stay at WARNING on every occurrence, because a
# server that NEWLY starts timing out is news, whereas one whose binary is
# absent is not.
#
# The ledger is self-healing, which is what keeps it both correct and bounded:
# a key is dropped as soon as that command resolves (so a binary that is
# installed and later disappears warns AGAIN rather than staying silent for the
# life of the process), and `probe_all` prunes keys no longer present in the
# config (so editing a command string does not retain the old one forever).
_unresolvable_warned: set[tuple[str, str]] = set()


def _warn_unresolvable_once(name: str, command: str) -> None:
    """WARNING on first sight of an unresolvable command, DEBUG thereafter."""
    key = (name, command)
    if key in _unresolvable_warned:
        logger.debug(
            "MCP probe [%s]: command still not found: %s (already reported)", name, command
        )
        return
    _unresolvable_warned.add(key)
    logger.warning("MCP probe failed [%s]: command not found: %s", name, command)


#: Servers whose probe has already reported a missing sandbox backend. Keyed by
#: name only (not by command): the cause is the HOST lacking a backend, not
#: anything about the server, so it recurs identically for every server on every
#: discovery cycle. Without this ledger a four-server config logged four
#: identical multi-line remedy paragraphs per cycle, forever.
_probe_sandbox_warned: set[str] = set()


#: Managed servers already served from the in-process declaration. Same shape and
#: reason as _probe_sandbox_warned: the trigger is the HOST having no backend, so
#: it recurs for every managed server on every discovery cycle.
_managed_in_process_warned: set[str] = set()


def _warn_managed_in_process_once(name: str) -> None:
    """Record the in-process fallback once per managed server.

    Logged rather than silent because it is a security-relevant substitution: the
    listing is served WITHOUT the handshake that proves the server can start, so
    ``ok`` here means "this package declares these tools", not "the server
    answered". An operator reading the dashboard should be able to find out which
    of the two they are looking at.
    """
    if name in _managed_in_process_warned:
        logger.debug("MCP probe [%s]: still serving the declared tool list", name)
        return
    _managed_in_process_warned.add(name)
    # WARNING, not info: `ok` on this path does not mean the handshake succeeded,
    # and the default log level is WARNING — at info the substitution would be
    # invisible on exactly the hosts where it always happens.
    logger.warning(
        "MCP probe [%s]: the tool list is read from this package's own "
        "declaration instead of a handshake. The tools are correct (it is the "
        "same declaration the server serves), but this does NOT verify the "
        "server can start. A self-derived managed command is normally probed "
        "for real even with no sandbox backend (the first-party carve-out), so "
        "reaching this fallback means that probe could not run here: a "
        "transient sandbox failure, a foreign outer sandbox, a governance "
        "sandbox floor, or a customized command/args for this server.",
        name,
    )


def _warn_probe_sandbox_unavailable_once(name: str) -> None:
    """WARNING on first sight per server, DEBUG thereafter.

    Mirrors :func:`_warn_unresolvable_once`. The message names the PROBE as the
    thing that could not run, so a reader is not sent debugging a server that
    kiro-cli is launching successfully from the agent config.
    """
    if name in _probe_sandbox_warned:
        logger.debug("MCP probe [%s]: still no sandbox backend (already reported)", name)
        return
    _probe_sandbox_warned.add(name)
    logger.warning(
        "MCP probe skipped [%s]: no OS-level sandbox backend on this host, so "
        "Kiro Crew cannot spawn the server to enumerate its tools. The server "
        "itself is unaffected — kiro-cli launches it from the agent config "
        "without this probe. Set agent.sandbox_allow_unsandboxed_exec=true to "
        "enable probing (the dashboard will otherwise show it with 0 tools).",
        name,
    )


def _clear_unresolvable(name: str, command: str) -> None:
    """Forget a command that now resolves, so a later outage is reported afresh."""
    _unresolvable_warned.discard((name, command))


def _prune_unresolvable(live: set[tuple[str, str]]) -> None:
    """Drop ledger keys that the current config no longer names.

    Without this, editing a server's command to another missing binary would
    keep the superseded string forever, so the ledger would grow with config
    churn instead of staying bounded by config size.
    """
    for stale in _unresolvable_warned - live:
        _unresolvable_warned.discard(stale)


def reset_unresolvable_warnings() -> None:
    """Clear the whole warn-once ledger.

    A test seam, and a manual escape hatch. Production does NOT rely on this:
    routine recovery is handled by `_clear_unresolvable` (on a successful
    probe) and `_prune_unresolvable` (on config churn), both of which run
    automatically inside the probe path.
    """
    _unresolvable_warned.clear()


# Well-known MCP config locations, tagged by scope.  Scope names match
# the dashboard badges (kirocrew / kiroGlobal / ccGlobal) and are the
# source of truth for the ``presence`` field on each server.
SCOPE_KIROCREW = "kirocrew"
SCOPE_KIRO_GLOBAL = "kiroGlobal"
# Surface label carried into the provenance decision so a declined rewrite names
# the file it declined to touch.
_CC_SIDECAR_SURFACE = "~/.mcp.json"
# Well-known label for a provider global (e.g. Claude Code's ~/.claude.json).
# The core does not scan it directly — a companion edition contributes it
# via the extra_mcp_scopes() CPP seam (see :func:`_extra_scope_sources`), so
# discovery scans exactly what apply/uninstall manage.
SCOPE_CC_GLOBAL = "ccGlobal"

# Core (edition-independent) MCP config scopes the build always scans.
# Provider-specific scopes are NOT hardcoded here; they are contributed at
# call time by the platform seam (:func:`_extra_scope_sources`) so discovery
# stays symmetric with the apply/uninstall path — OSS is Kiro-only, and a
# companion re-adds its provider global through the seam rather than the core
# scanning a file it can no longer manage (which would surface un-uninstallable
# "zombie" servers).
#
# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage.py is the reference implementation.
_MCP_SOURCES: tuple[tuple[Path, str], ...] | None = None


def _mcp_sources() -> tuple[tuple[Path, str], ...]:
    """Core MCP config scopes (path, scope), resolved against the live home.

    The tuple is in merge/priority order, highest first: the kirocrew-specific
    file, then the Kiro global. Callers depend on this ordering for scope
    precedence, so the element order and scope constants must stay fixed.
    """
    if _MCP_SOURCES is not None:
        return _MCP_SOURCES
    return (
        (data_home() / "mcp.json", SCOPE_KIROCREW),
        (Path.home() / ".kiro" / "settings" / "mcp.json", SCOPE_KIRO_GLOBAL),
    )


# Legacy name preserved for backward-compat with tests that monkeypatch it.
# Derived from :func:`_mcp_sources` (core scopes only) so the two can never
# drift; seam-contributed scopes are merged in at call time, not baked here.
_MCP_JSON_PATHS: tuple[Path, ...] | None = None


def _mcp_json_paths() -> tuple[Path, ...]:
    """Core MCP config file paths, resolved against the live home."""
    if _MCP_JSON_PATHS is not None:
        return _MCP_JSON_PATHS
    return tuple(p for p, _ in _mcp_sources())


def _extra_scopes() -> list[Any]:
    """Provider MCP config scopes contributed by the edition (CPP seam)."""
    from kiro_crew.platform.context import current_context, safe_context_call

    return safe_context_call(
        lambda: list(current_context().mcp_tooling.extra_mcp_scopes()),
        fallback_factory=list,
        log_message="extra_mcp_scopes lookup failed; discovery using core scopes only",
    )


def _extra_scope_sources() -> list[tuple[Path, str]]:
    """Return edition-contributed provider globals with discovery scope ids.

    Each returned :class:`~kiro_crew.platform.interfaces.McpScope` becomes a
    ``(global_json, f"{id}Global")`` pair, so a companion's Claude Code scope
    (``~/.claude.json`` → ``ccGlobal``) is scanned by discovery exactly as the
    apply/uninstall path writes it. The public Default returns ``[]`` so
    discovery is Kiro-only. Deferred context read so this module never imports
    the platform package at load; failures degrade to no extra scopes.
    """
    scopes = _extra_scopes()
    return [(s.global_json, f"{s.id}Global") for s in scopes]


# Core (edition-independent) scopes in merge/priority order, highest first: the
# kirocrew-specific file, then the Kiro global. ``ccGlobal`` (and every other
# provider global) is NOT a core scope — it is contributed by the edition seam
# and appended AFTER these by :func:`_scope_priority` (lowest priority), so a
# companion's provider global only fills gaps and never outranks the Kiro
# global. This matches ``rebuild_agent_config``'s merge order in agent.py
# (kirocrew > kiro-global > seam provider globals) — discovery, apply, and
# rebuild all agree, so the dashboard never shows a spec the agent won't run.
_CORE_SCOPE_ORDER: tuple[str, ...] = (SCOPE_KIROCREW, SCOPE_KIRO_GLOBAL)


def _scope_priority(by_source: dict[str, dict[str, Any]]) -> list[str]:
    """Return every scope in ``by_source`` in merge/priority order.

    Core scopes come first in their fixed priority (:data:`_CORE_SCOPE_ORDER`);
    every seam-contributed provider scope (including the always-seeded
    ``ccGlobal``) follows in stable insertion order at the lowest priority —
    matching ``rebuild_agent_config`` so discovery/apply/rebuild agree. All
    presence/merge callers derive their scope list from this so a companion
    scope is never silently dropped from the reported ``presence`` (which the
    frontend would misread as ``false`` and delete on the next apply).
    """
    ordered = [s for s in _CORE_SCOPE_ORDER if s in by_source]
    ordered += [s for s in by_source if s not in _CORE_SCOPE_ORDER]
    return ordered


@dataclass
class _ProbeResult:
    """Cached probe result for a single server."""

    status: str
    tools: list[str]
    error: str
    probed_at: float
    # Handshake metadata, captured because the probe already pays for the
    # ``initialize`` round-trip and threw the answer away. Consumed by
    # ``mcp_gateway.shareability`` to decide whether to RECOMMEND stubbing.
    capabilities: dict[str, Any] | None = None
    protocol_version: str = ""
    server_info: dict[str, Any] = field(default_factory=dict)
    # ``annotations`` per tool, in ``tools/list`` order. Only servers speaking
    # MCP 2025-03-26 or later can send these, so an empty list is "not
    # available", never "declared nothing".
    tool_annotations: list[dict[str, Any]] = field(default_factory=list)
    # Wall-clock companion to the monotonic ``probed_at``: monotonic drives the
    # TTL (immune to clock changes), wall-clock is what the API reports so the
    # UI can render "as of <time>". Two clocks, one write, no drift.
    probed_at_wall: float = 0.0
    probe_mode: str = "handshake"


# Module-level probe cache: server name → result
_probe_cache: dict[str, _ProbeResult] = {}


def _get_cached(name: str) -> tuple[str, list[str], str, float, str]:
    """Return (status, tools, error, probed_at_wall, probe_mode) from cache.

    If within TTL: returns original status + tools.
    If expired: returns "outdated" + tools (tools always preserved).
    If not cached: returns ("unknown", [], "", 0.0, "handshake").

    The wall-clock timestamp and probe mode are returned even for an expired
    entry — "outdated" is exactly the state where WHEN it was last true is the
    most useful thing the UI can say.
    """
    cached = _probe_cache.get(name)
    if cached is None:
        return "unknown", [], "", 0.0, "handshake"
    age = time.monotonic() - cached.probed_at
    if age <= _PROBE_TTL_SECS:
        return cached.status, cached.tools, cached.error, cached.probed_at_wall, cached.probe_mode
    # Expired — mark outdated but preserve tools
    return "outdated", cached.tools, "", cached.probed_at_wall, cached.probe_mode


def probe_metadata(name: str) -> _ProbeResult | None:
    """The cached handshake metadata for *name*, or None if never probed.

    Deliberately separate from ``_get_cached`` so adding evidence fields never
    changes that function's tuple shape, and stale-but-present metadata stays
    readable: an expired probe still tells the truth about what the server
    advertised, and the caller decides whether age matters.
    """
    return _probe_cache.get(name)


def _cache_probe(server: McpServerInfo) -> None:
    """Store probe result in cache.

    The error is redacted HERE, with the headers that were live when the
    probe ran: ``list_servers()`` re-attaches cached errors to server objects
    built from the CURRENT config, so redacting only at serialization time
    would mask a rotated credential's NEW value while the cached error still
    carries the OLD one.
    """
    server.probed_at = time.time()
    _probe_cache[server.name] = _ProbeResult(
        status=server.status,
        tools=list(server.tools),
        error=redact_mcp_error(server.error, server.headers),
        probed_at=time.monotonic(),
        capabilities=(
            dict(server.capabilities) if isinstance(server.capabilities, dict) else None
        ),
        protocol_version=server.protocol_version,
        server_info=dict(server.server_info),
        tool_annotations=[dict(a) for a in server.tool_annotations],
        probed_at_wall=server.probed_at,
        probe_mode=server.probe_mode,
    )


MCP_REDACTED_HEADER_VALUE = "[REDACTED: credential]"
# Two regimes, chosen by the only property that matters: whether the value could
# plausibly occur inside ordinary prose by chance.
#
# At or above this length it cannot, so the credential is masked as a BARE
# substring — a server reflecting it glued to other characters
# ("prefix<credential>") must still be caught.
_MCP_CREDENTIAL_UNANCHORED_MIN_LENGTH = 8
# Below that, masking is restricted to a standalone token, because an unanchored
# short value would corrupt unrelated words. One- and two-character values are
# skipped entirely: no boundary rule separates them from prose words like "a".
_MCP_CREDENTIAL_SUFFIX_MIN_LENGTH = 3
_MCP_AUTH_VALUE_RE = re.compile(r"^\S+\s+(.+)$")


def _mcp_credential_token_pattern(value: str) -> str:
    """Match ``value`` with each character in literal or percent-encoded form.

    A remote server can reflect a configured credential URL-encoded — e.g. a
    padded Basic token whose ``=`` comes back as ``%3D`` inside an error URL —
    and a literal-only pattern would hand that encoded copy to the client
    unmasked. Every character therefore matches either itself or its UTF-8
    ``%XX`` escape sequence, with the leading ``%`` itself allowed to be
    percent-escaped any number of times (``%253D``, ``%25253D``, ...) so a
    double-encoded reflection is caught by the same substitution pass. A space
    additionally matches ``+`` (the form-urlencoded spelling) and its escape
    ``%2B``.
    """
    parts: list[str] = []
    for char in value:
        alternatives = [re.escape(char)]
        try:
            # "%(?:25)*XX" per byte: a literal %XX, or the same escape with its
            # percent sign re-encoded one or more times (%25XX, %2525XX, ...).
            alternatives.append(
                "".join(f"%(?:25)*{byte:02X}" for byte in char.encode("utf-8"))
            )
        except UnicodeEncodeError:
            # A lone surrogate (JSON permits unpaired \uD800 escapes) has no
            # UTF-8 spelling; keep the literal alternative so building the
            # pattern never turns a listing request into a 500.
            pass
        if char == " ":
            alternatives.append(re.escape("+"))
            alternatives.append("%(?:25)*2B")
        parts.append("(?:" + "|".join(alternatives) + ")")
    return "".join(parts)


def redact_mcp_headers(headers: object) -> dict[str, str]:
    """Preserve header names while hiding every client-facing value.

    Custom header names can carry credentials too, so only names are safe
    metadata for dashboard responses.
    """
    if not isinstance(headers, dict):
        return {}
    return {
        name: MCP_REDACTED_HEADER_VALUE
        for name in headers
        if isinstance(name, str)
    }


def redact_mcp_error(error: object, headers: object) -> str:
    """Scrub credential material from a probe error before it leaves the backend.

    Two layers, so every consumer (``to_dict``, the probe cache, the probe
    endpoints) satisfies one invariant — no credential-shaped text survives to
    serialized output:

    1. The site-wide scanners (``redact_credentials`` /
       ``redact_exfiltration_urls``) catch anything credential-SHAPED that a
       remote server reflects, whether or not it matches configured values —
       the same pass ``_sanitize_probe_error`` applies to probe exceptions.
    2. The configured-value scrubber below catches the exact header values and
       Authorization suffixes, including encoded spellings the generic
       scanners cannot know about.
    """
    if not isinstance(error, str) or not isinstance(headers, dict):
        return error if isinstance(error, str) else ""

    error, _ = redact_exfiltration_urls(error)
    error, _ = redact_credentials(error)

    # Values map to whether they require lexical boundaries. Full header values
    # and long credentials are bare substring matches; only SHORT credentials
    # need boundaries, since only they could collide with ordinary words.
    values: dict[str, bool] = {}
    for name, raw_value in headers.items():
        if not isinstance(raw_value, str):
            continue
        value = raw_value.strip()
        if not value:
            continue
        values[value] = False

        if not isinstance(name, str) or name.casefold() != "authorization":
            continue
        match = _MCP_AUTH_VALUE_RE.fullmatch(value)
        if match:
            credential = match.group(1).strip()
            if len(credential) >= _MCP_CREDENTIAL_SUFFIX_MIN_LENGTH:
                needs_boundary = (
                    len(credential) < _MCP_CREDENTIAL_UNANCHORED_MIN_LENGTH
                )
                values.setdefault(credential, needs_boundary)

    if not values:
        return error

    # Check the characters outside a suffix instead of using \b: base64 padding
    # ends in a non-word "=", so \b would fail between that padding and ordinary
    # punctuation. Longest-first keeps a full header ahead of its own suffix.
    pattern = "|".join(
        (
            rf"(?<!\w){_mcp_credential_token_pattern(value)}(?!\w)"
            if boundary_safe
            else _mcp_credential_token_pattern(value)
        )
        for value, boundary_safe in sorted(
            values.items(), key=lambda item: len(item[0]), reverse=True
        )
    )
    return re.sub(
        pattern,
        MCP_REDACTED_HEADER_VALUE,
        error,
        flags=re.IGNORECASE,
    )


@dataclass
class McpServerInfo:
    """Metadata for a single MCP server (local stdio or remote HTTP)."""

    name: str
    command: str = ""
    args: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # Remote-only OAuth hints carried verbatim to the runtime, which owns the
    # authorization exchange. Kiro Crew never enforces scopes and never registers
    # a client — it only refuses to lose these fields while syncing.
    scopes: list[str] = field(default_factory=list)
    client_id: str = ""
    status: str = "unknown"  # unknown | ok | error | probing | outdated | disabled | needs_auth
    tools: list[str] = field(default_factory=list)
    error: str = ""
    source: str = "agent"  # agent | mcp.json | discovered  (legacy field, prefer presence)
    presence: dict[str, bool] = field(
        default_factory=lambda: {
            SCOPE_KIROCREW: False,
            SCOPE_KIRO_GLOBAL: False,
            SCOPE_CC_GLOBAL: False,
        }
    )
    disabled_tools: list[str] = field(default_factory=list)
    # True when ANY scope's entry for this server carries ``disabled: true``
    # (a consent-disabled install/custom add, or a server the user switched off
    # in the dashboard — ``/api/mcp/toggle`` writes the flag into the Kiro-global
    # ``mcp.json``). Disabled rows are NEVER probed — probing spawns the server
    # process, which is what consent gates. The refusal is enforced inside
    # ``probe_server`` itself, so setting this flag is sufficient no matter which
    # entry point does the probing.
    disabled: bool = False
    # -- handshake metadata (probe-only; empty on unprobed rows) -----------
    # The server's advertised ``capabilities`` object, verbatim. ``None`` means
    # no handshake happened, which is NOT the same as an empty declaration.
    capabilities: dict[str, Any] | None = None
    # The ``protocolVersion`` the server answered with — not the one requested.
    # Tool annotations only exist from MCP 2025-03-26, so this is what makes
    # their absence interpretable.
    protocol_version: str = ""
    server_info: dict[str, Any] = field(default_factory=dict)
    tool_annotations: list[dict[str, Any]] = field(default_factory=list)
    # How the current ``status``/``tools`` were established. "handshake" is a
    # real spawn + initialize + tools/list round trip; "declared" is the
    # in-process fallback for a managed server whose probe could not spawn —
    # the tool list is correct (same declaration the server serves) but nothing
    # verified the server can start. Surfaced so the UI can tell the two
    # apart instead of rendering both as an identical green badge.
    probe_mode: str = "handshake"
    # Wall-clock time (``time.time()``) of the probe that produced ``status``,
    # or 0.0 when no probe has run. Carried through the caches and into the
    # API payload so a badge can say WHEN it was true — the caches legitimately
    # serve results up to their TTL, and an undated "Online" reads as "now".
    probed_at: float = 0.0

    @property
    def is_remote(self) -> bool:
        """True for Streamable HTTP servers (url-based, no command)."""
        return bool(self.url) and not self.command

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "command": self.command,
            "args": self.args or [],
            "status": self.status,
            "tools": self.tools,
            "error": redact_mcp_error(self.error, self.headers),
            "source": self.source,
            "presence": dict(self.presence),
            "probeMode": self.probe_mode,
            "probedAt": self.probed_at,
        }
        if self.url:
            d["url"] = self.url
            if self.headers:
                d["headers"] = redact_mcp_headers(self.headers)
            # Not redacted: requested scopes and a public OAuth client id are
            # non-secret configuration the user needs to see.
            #
            # These are the INTERNAL key names, matching the dashboard's
            # ``McpServer`` type. This dict is an API response, never a file
            # kiro-cli reads, so it must NOT be translated to the wire names —
            # ``kiro_oauth_wire_entry`` is applied on the emit paths instead.
            if self.scopes:
                d["scopes"] = list(self.scopes)
            if self.client_id:
                d["clientId"] = self.client_id
        if self.disabled_tools:
            d["disabledTools"] = self.disabled_tools
        if self.disabled:
            d["disabled"] = True
        return d


def _load_agent_config(*, user_home: Path | None = None) -> dict[str, Any]:
    """Load the agent config to read mcpServers.

    Merges mcpServers from project-dir (if set), bundled defaults.json,
    AND the installed kirocrew.json — because defaults.json may not have
    mcpServers (they're added dynamically at install time by ``kirocrew setup``).
    """
    configs: list[dict[str, Any]] = []

    # Project-dir override (development)
    proj = os.environ.get("KIROCREW_PROJECT_DIR")
    if proj:
        p = Path(proj) / "agents" / "defaults.json"
        if p.is_file():
            try:
                loaded = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    configs.append(loaded)
            except (json.JSONDecodeError, OSError):
                pass

    # Bundled defaults.json (fallback when no project-dir)
    if not configs:
        bundled = Path(__file__).resolve().parent / "config" / "defaults.json"
        if bundled.is_file():
            try:
                loaded = json.loads(bundled.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    configs.append(loaded)
            except (json.JSONDecodeError, OSError):
                pass

    # Installed agent config (always check for mcpServers)
    from kiro_crew.agent import AGENT_FILENAME  # circular import: agent imports mcp_discovery

    installed = (
        (user_home / ".kiro" / "agents") if user_home else kiro_agents_dir()
    ) / AGENT_FILENAME
    if installed.is_file():
        try:
            loaded = json.loads(installed.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                configs.append(loaded)
        except (json.JSONDecodeError, OSError):
            pass

    if not configs:
        return {}

    # Merge: use first config as base, merge mcpServers from all sources
    merged = dict(configs[0])
    first_servers = merged.get("mcpServers")
    mcp: dict[str, Any] = dict(first_servers) if isinstance(first_servers, dict) else {}
    for cfg in configs[1:]:
        servers = cfg.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for name, spec in servers.items():
            if name not in mcp:
                mcp[name] = spec
    merged["mcpServers"] = mcp
    return merged


def _mcp_names_from_file(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(safe_read_file(str(path)))
    except (json.JSONDecodeError, OSError, TypeError):
        return set()
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return set()
    return {name for name in servers if isinstance(name, str)}


def configured_mcp_aliases(*, data_home: Path, user_home: Path) -> set[str]:
    """Return canonical names reserved by every effective KiroCrew MCP source."""
    names: set[str] = set()
    agent_servers = _load_agent_config(user_home=user_home).get("mcpServers", {})
    if isinstance(agent_servers, dict):
        names.update(name for name in agent_servers if isinstance(name, str))

    names.update(_mcp_names_from_file(data_home / "mcp.json"))
    names.update(_mcp_names_from_file(user_home / ".kiro" / "settings" / "mcp.json"))

    from kiro_crew.platform.context import current_context, safe_context_call

    extra_servers: dict[str, dict] = safe_context_call(
        lambda: dict(current_context().mcp_tooling.extra_mcp_servers()),
        fallback_factory=dict,
        log_message="extra_mcp_servers lookup failed; collision scan using core sources only",
    )
    names.update(name for name in extra_servers if isinstance(name, str))
    for scope in _extra_scopes():
        names.update(_mcp_names_from_file(scope.global_json))
        if scope.agent_mcp_file is not None:
            names.update(_mcp_names_from_file(scope.agent_mcp_file))
    return {mcp_server_alias(name) for name in names}


def _load_mcp_json_by_source() -> dict[str, dict[str, Any]]:
    """Return ``{scope: {name: spec}}`` keyed by scope name.

    Reads every well-known MCP config location and bucketizes servers by
    their origin scope.  Unlike :func:`_load_mcp_json`, no cross-source
    merging happens — callers that need per-scope presence use this.

    Iterates the core :data:`_MCP_SOURCES` (path + scope pairs) PLUS any
    provider scopes contributed by the platform seam
    (:func:`_extra_scope_sources`), so discovery scans exactly what
    apply/uninstall manage and paths/scope labels can never drift.  When tests
    monkeypatch :data:`_MCP_JSON_PATHS` to a shorter tuple for isolation, the
    corresponding scopes are recovered by looking up each patched path; any
    unknown path falls back to :data:`SCOPE_KIROCREW`.
    """
    result: dict[str, dict[str, Any]] = {
        SCOPE_KIROCREW: {},
        SCOPE_KIRO_GLOBAL: {},
        SCOPE_CC_GLOBAL: {},
    }
    extra_sources = _extra_scope_sources()
    for _, scope in extra_sources:
        result.setdefault(scope, {})
    path_to_scope = {p: scope for p, scope in _mcp_sources()}
    path_to_scope.update({p: scope for p, scope in extra_sources})
    scan_paths: tuple[Path, ...] = tuple(_mcp_json_paths()) + tuple(p for p, _ in extra_sources)
    for p in scan_paths:
        scope = path_to_scope.get(p, SCOPE_KIROCREW)
        if not p.is_file():
            continue
        try:
            data = json.loads(safe_read_file(str(p)))
        except (json.JSONDecodeError, OSError) as exc:
            # PermissionError (subclass of OSError) is raised by
            # safe_read_file when is_sensitive_path() blocks the read.
            logger.warning("Failed to load MCP config from %s: %s", p, exc)
            continue
        if not isinstance(data, dict):
            continue
        servers = data.get("mcpServers", {})
        if isinstance(servers, dict):
            # Merge instead of overwriting — if two paths resolve to the
            # same scope (legitimate duplicates, or tests that monkeypatch
            # _MCP_JSON_PATHS with fallback-scoped paths), setdefault keeps
            # first-wins semantics within the scope.
            bucket = result[scope]
            for name, spec in servers.items():
                bucket.setdefault(name, spec)
    return result


def _load_mcp_json() -> dict[str, Any]:
    """Load and merge mcpServers from all well-known mcp.json locations.

    Earlier paths take precedence — if the same server name appears in
    multiple files, the first definition wins (via ``setdefault``).
    Retained for callers that only need a merged view; use
    :func:`_load_mcp_json_by_source` when per-scope presence matters.
    """
    merged: dict[str, Any] = {}
    by_source = _load_mcp_json_by_source()
    # Iteration order = priority (setdefault is a no-op once populated):
    # kirocrew-specific file > kiro global > any seam provider globals.
    # Matches rebuild_agent_config's merge order in agent.py.
    for scope in _scope_priority(by_source):
        for name, spec in by_source.get(scope, {}).items():
            merged.setdefault(name, spec)
    return merged


def _spec_scopes(spec: dict) -> list[str]:
    """Requested OAuth scopes from a spec, dropping anything malformed.

    On-disk specs are untrusted (hand-edited files, other tools), so a
    non-list or a list with non-string members degrades to "no scopes"
    rather than propagating a bad shape into the agent config.

    Reads kiro-cli's ``oauthScopes`` as well as Kiro Crew's internal ``scopes``:
    files we emit for kiro-cli carry the former, so a discovery pass that knew
    only the latter would report a scoped server as unscoped.
    """
    return kiro_entry_scopes(spec)


def _spec_client_id(spec: dict) -> str:
    """Public OAuth client id from a spec, or "" when absent/malformed.

    Accepts kiro-cli's nested ``oauth.clientId`` as well as the internal
    top-level key, for the same round-trip reason as ``_spec_scopes``.
    """
    return kiro_entry_client_id(spec)


def _server_from_spec(name: str, spec: dict, source: str) -> McpServerInfo:
    return McpServerInfo(
        name=name,
        command=spec.get("command", ""),
        args=spec.get("args", []),
        env=spec.get("env", {}),
        url=spec.get("url", ""),
        headers=spec.get("headers", {}),
        scopes=_spec_scopes(spec),
        client_id=_spec_client_id(spec),
        source=source,
    )


# Managed server name -> the ``kirocrew`` CLI subcommand that serves it.
_MANAGED_SERVER_SUBCOMMANDS = {
    "kirocrew-core": "mcp-core",
    "kirocrew-cron": "mcp-cron",
    "kirocrew-computer": "mcp-computer",
    "kirocrew-dashboard": "mcp-dashboard",
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
}


#: Managed servers that advertise ``kirocrew.caller-identity`` -- that is, the ones
#: consuming the per-call caller block gatewayd injects instead of reading identity
#: from their own process. Every other name in ``_MANAGED_SERVER_SUBCOMMANDS``
#: resolves the session from its process and can serve only one at a time.
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
    {"kirocrew-core", "kirocrew-cron"}
)


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

    Imported lazily: these modules pull in the validation/artifacts graph, which
    cannot be imported at this module's import time (circular). ``_list_tools`` is
    a pure read of schemas plus config — no I/O of its own, no side effects, and
    cheap enough for a discovery cycle.

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
        tools = module._list_tools()
    except Exception:
        logger.debug("in-process tool read failed for %s; will probe", name, exc_info=True)
        return None
    if not isinstance(tools, list):
        return None
    return [n for t in tools if isinstance(t, dict) and (n := t.get("name"))]


# Cached resolved (command, args) — avoids subprocess.run on every list_servers() call.
_resolved_managed_invocation: dict[str, tuple[str, list[str]]] = {}


def _fix_stale_managed_command(name: str, spec: dict) -> None:
    """Re-resolve command + args for a managed MCP server to the running install.

    Always re-resolves — the stored path may exist as a file/symlink but still
    crash at runtime (e.g. a path from a previous install). The running gateway
    knows how to invoke itself.

    Delegates to :func:`kiro_crew.agent._kirocrew_mcp_invocation`, the single
    source of truth for the managed invocation. That handles every layout:
    a standalone ``bin/kirocrew`` (POSIX) / ``Scripts\\kirocrew.exe`` (Windows
    pip install) console script when one resolves, the Windows bundle's
    ``bin\\kirocrew.cmd`` shim (unwrapped to ``<root>\\python.exe -P -s -m
    kiro_crew <sub>``), and otherwise the ``<interpreter> -m kiro_crew <sub>``
    fallback. Both ``command`` AND ``args``
    are rewritten — the fallback needs ``["-m", "kiro_crew", <sub>]``, so
    re-resolving the command alone (the old behavior) silently dropped the args
    and spawned a bare ``kirocrew`` that isn't on PATH (Windows: ``command not
    found: kirocrew``; the built-in cron/core tools then never load).
    """
    subcommand = _MANAGED_SERVER_SUBCOMMANDS.get(name)
    if subcommand is None:
        return
    invocation = _resolved_managed_invocation.get(name)
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
    invocation = _resolved_managed_invocation.get(name)
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
    # Refuse the interpreter fallback (`<python> -m kiro_crew <sub>`): `python
    # -m` prepends the child's CWD to sys.path (this package supports 3.10, so
    # `-P`/PYTHONSAFEPATH cannot be assumed), and the probe child inherits the
    # gateway's cwd — a planted `kiro_crew/` tree there would shadow the
    # installed package and run unconfined. Only a resolved console-script
    # binary, whose entrypoint imports from its own install, qualifies.
    if expected_args[:2] == ["-m", "kiro_crew"]:
        return False
    return (
        command == expected_command
        and list(args) == list(expected_args)
        and dict(env or {}) == expected_env
    )


def list_servers() -> list[McpServerInfo]:
    """Return all known MCP servers from agent config + mcp.json + CC global.

    Merges cached probe results so status/tools survive across requests.
    Populates ``presence`` for each server with booleans for whether the
    server appears in each of the three scope config files.

    Servers that live only in a provider global (e.g. a user added one via
    ``kiro-cli mcp add`` or directly to ``~/.claude.json``) still show up
    on the dashboard so users get a full inventory from one page.
    """
    servers: dict[str, McpServerInfo] = {}
    disabled_in_agent: set[str] = set()

    # 1. From agent config (mcpServers key)
    agent_cfg = _load_agent_config()
    for name, spec in agent_cfg.get("mcpServers", {}).items():
        if isinstance(spec, dict):
            if spec.get("disabled"):
                disabled_in_agent.add(name)
            else:
                # Re-resolve stale managed MCP server paths at runtime
                _fix_stale_managed_command(name, spec)
                servers[name] = _server_from_spec(name, spec, "agent")

    # 2. From scope-tagged mcp.json sources, in priority order so highest-
    #    priority scope populates disabled_tools first and lower scopes
    #    don't overwrite it.  Order = kirocrew-specific > Kiro global >
    #    any seam provider globals, matching rebuild_agent_config.
    by_source = _load_mcp_json_by_source()
    disabled_tools_claimed: set[str] = set()
    for scope in _scope_priority(by_source):
        for name, spec in by_source.get(scope, {}).items():
            if not isinstance(spec, dict):
                continue
            # Introduce the server first (if new) so the disabledTools
            # carry below applies to both new and existing entries.  Without
            # this ordering, the highest-priority scope's disabledTools is
            # dropped for new servers because `name in servers` is False
            # before insertion, letting a lower-priority scope's value
            # overwrite the (empty) default on a later iteration.
            if not spec.get("disabled") and name not in servers and name not in disabled_in_agent:
                servers[name] = _server_from_spec(name, spec, "mcp.json")
            elif scope == SCOPE_KIROCREW and spec.get("disabled") and name not in servers:
                # Consent-disabled entries (registry installs and custom adds
                # land with ``disabled: true`` until the user enables them)
                # live ONLY in the KiroCrew scope. They must still get a row:
                # the enable action in this table IS the consent step the
                # install flow points at — an invisible server can never be
                # consented to. The row is marked disabled and excluded from
                # probing (see probe_all).  ``disabled_in_agent`` is NOT
                # consulted here: config sync mirrors this very entry into the
                # agent file as ``disabled: true``, so the agent-side flag is
                # the same signal, not an independent user override.
                info = _server_from_spec(name, spec, "mcp.json")
                info.disabled = True
                servers[name] = info

            # Per-tool disables: first-scope-wins.  Use "disabledTools" in
            # spec (key presence) rather than truthiness so an explicit
            # "disabledTools": [] (user intent: "all tools enabled") is
            # respected and prevents lower-priority scopes from overwriting.
            if name in servers and "disabledTools" in spec and name not in disabled_tools_claimed:
                servers[name].disabled_tools = spec.get("disabledTools", [])
                disabled_tools_claimed.add(name)

    # 3. Compute per-scope presence.
    #
    #    MC presence = "will this load in KiroCrew sessions after the next
    #    rebuild".  Because ``rebuild_agent_config`` inherits from both
    #    provider globals, a server present in any scope source (or already
    #    in the current merged agent config) counts as MC green unless
    #    KiroCrew has an explicit ``disabled: true`` override.
    #    Kiro/CC presence = raw membership in that provider's global config.
    agent_names = set(agent_cfg.get("mcpServers", {}).keys())
    kirocrew_own = by_source.get(SCOPE_KIROCREW, {})
    # Every scope other than kirocrew is a raw-membership global scope
    # (Kiro/CC/any seam-contributed provider). Derive them from by_source so a
    # companion scope is reported in presence rather than omitted — an omitted
    # scope is read as False by the frontend and DELETED on the next apply.
    global_scopes = [s for s in _scope_priority(by_source) if s != SCOPE_KIROCREW]
    for name, server in servers.items():
        mc_disabled = (
            isinstance(kirocrew_own.get(name), dict) and kirocrew_own[name].get("disabled") is True
        )
        in_any_source = name in agent_names or any(
            name in by_source.get(scope, {}) for scope in by_source
        )
        presence: dict[str, bool] = {SCOPE_KIROCREW: in_any_source and not mc_disabled}
        for scope in global_scopes:
            presence[scope] = name in by_source.get(scope, {})
        server.presence = presence

    # 3b. Canonicalize: fold a server keyed by a slash/colon name into its
    #     mcp_server_alias() form so a server registered under BOTH its raw key
    #     (e.g. "npm:@playwright/mcp") and its alias ("playwright-mcp") is
    #     reported as one logical server instead of two rows / two probes. This
    #     is read-only canonicalization — no config file is modified. Slash-free
    #     names alias to themselves, so non-scoped servers are unaffected. When
    #     both forms are present, presence flags are unioned and the entry whose
    #     own key is already the canonical alias is kept as the representative.
    canonical_servers: dict[str, McpServerInfo] = {}
    for name, server in servers.items():
        canon = mcp_server_alias(name)
        rep = canonical_servers.get(canon)
        if rep is None:
            server.name = canon
            canonical_servers[canon] = server
            continue
        union = {
            scope: bool(rep.presence.get(scope)) or bool(server.presence.get(scope))
            for scope in rep.presence
        }
        chosen = server if name == canon else rep
        chosen.name = canon
        chosen.presence = union
        canonical_servers[canon] = chosen
    servers = canonical_servers

    # 3c. Consent is per SCOPE: a ``disabled: true`` ANYWHERE withholds the
    #     spawn, not only the branch above that INTRODUCES a Kiro-Crew-scope row
    #     which exists nowhere else. ``/api/mcp/toggle`` writes the flag into the
    #     Kiro-global ``mcp.json``, and a row that step 1 already introduced from
    #     the agent config would otherwise keep ``disabled = False`` and stay
    #     probeable: the user switches a server off in the dashboard and
    #     discovery still spawns it.
    #
    #     Runs AFTER 3b so both sides are canonical. Scope dicts are keyed by the
    #     RAW name, so a server configured as ``npm:@playwright/mcp`` is reported
    #     as ``playwright-mcp`` — matching before canonicalization would miss the
    #     raw-keyed disable whenever the agent config retained the canonical row.
    #
    #     Only ever SETS the flag, so scope priority is irrelevant: one disable
    #     is enough, and no scope can re-enable what another disabled. The flag
    #     now IS the safety property (``probe_server`` refuses on it), which is
    #     why populating it correctly matters more than when each caller filtered
    #     rows for itself.
    for scope_specs in by_source.values():
        for raw_name, spec in scope_specs.items():
            if not isinstance(spec, dict) or not spec.get("disabled"):
                continue
            row = servers.get(mcp_server_alias(raw_name))
            if row is not None:
                row.disabled = True

    # 4. Merge cached probe results
    for s in servers.values():
        status, tools, error, probed_at, probe_mode = _get_cached(s.name)
        s.status = status
        s.tools = tools
        s.error = error
        s.probed_at = probed_at
        s.probe_mode = probe_mode

    return list(servers.values())


async def _read_jsonrpc_response(resp: aiohttp.ClientResponse) -> dict:
    """Parse a JSON-RPC response from either JSON or SSE content-type.

    MCP Streamable HTTP servers may respond with ``application/json`` (single
    object) or ``text/event-stream`` (SSE with ``data:`` lines containing JSON).
    """
    ct = resp.content_type or ""
    if "text/event-stream" in ct:
        body = await resp.text()
        last: dict = {}
        for line in body.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload:
                    try:
                        parsed = json.loads(payload)
                        if isinstance(parsed, dict) and "id" in parsed:
                            last = parsed
                    except json.JSONDecodeError:
                        pass
        return last
    return await resp.json()


def _needs_authorization(
    status_code: int, resp_headers: Mapping[str, str], sent_headers: Mapping[str, str]
) -> bool:
    """True when a remote probe response means "authenticate", not "broken".

    The runtime completes OAuth and holds the token; the probe does not. So a
    tokenless probe of an OAuth server gets 401 (or 403 with a
    ``WWW-Authenticate`` challenge). Treat that as ``needs_auth``.

    A static ``Authorization`` header in the config is a different case: the
    caller supplied a credential and it was rejected, which is a real error.
    """
    if any(k.lower() == "authorization" for k in sent_headers):
        return False
    if status_code == 401:
        return True
    if status_code == 403 and any(k.lower() == "www-authenticate" for k in resp_headers):
        return True
    return False


async def _probe_remote(server: McpServerInfo) -> McpServerInfo:
    """Probe a remote Streamable HTTP MCP server via POST."""
    server.status = "probing"
    server.probed_at = time.time()
    server.probe_mode = "handshake"
    try:
        init_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kirocrew-probe", "version": "1.0.0"},
            },
        }
        hdrs = {
            **server.headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        timeout = aiohttp.ClientTimeout(total=_get_probe_timeout())
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(server.url, json=init_body, headers=hdrs) as resp:
                if resp.status != 200:
                    if _needs_authorization(resp.status, resp.headers, server.headers):
                        # A remote OAuth server answers a tokenless probe with
                        # 401 (or 403 + WWW-Authenticate). That is the expected
                        # reply, not a fault: the kiro-cli runtime holds the
                        # OAuth token and calls the server fine. The probe never
                        # sees that token (Kiro Crew keeps no credentials), so
                        # report "needs_auth" instead of a misleading error.
                        server.status = "needs_auth"
                        server.error = ""
                    else:
                        server.status = "error"
                        server.error = f"HTTP {resp.status}"
                    _cache_probe(server)
                    return server
                # A stateful Streamable HTTP server issues a session id on
                # initialize and requires it on every later request — without
                # it, tools/list gets a 4xx/error and a HEALTHY server renders
                # errored. Absent header = stateless server; nothing to carry.
                mcp_session_id = resp.headers.get("Mcp-Session-Id", "")
                data = await _read_jsonrpc_response(resp)
                if data.get("error"):
                    server.status = "error"
                    err = data["error"]
                    server.error = (
                        err.get("message", "unknown error") if isinstance(err, dict) else str(err)
                    )
                    _cache_probe(server)
                    return server

            if mcp_session_id:
                hdrs = {**hdrs, "Mcp-Session-Id": mcp_session_id}
            # The spec's lifecycle requires notifications/initialized between
            # initialize and the first request; a conforming stateful server
            # may reject tools/list without it. Notifications get 202/204 and
            # no body — only a hard connection failure matters, and that
            # surfaces on the tools/list call right after.
            initialized_body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            try:
                async with session.post(server.url, json=initialized_body, headers=hdrs):
                    pass
            except aiohttp.ClientError:
                logger.debug(
                    "MCP probe [%s]: initialized notification failed; proceeding to tools/list",
                    server.name,
                )

            list_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            async with session.post(server.url, json=list_body, headers=hdrs) as resp:
                if resp.status != 200:
                    # An initialize that succeeds and a tools/list that does
                    # not is a server no session can get a tool out of — the
                    # badge certifies "tools usable", so this is a failed probe.
                    server.status = "error"
                    server.error = f"tools/list: HTTP {resp.status}"
                    _cache_probe(server)
                    return server
                data = await _read_jsonrpc_response(resp)
                if data.get("error"):
                    err = data["error"]
                    server.status = "error"
                    server.error = "tools/list: " + (
                        err.get("message", "unknown error") if isinstance(err, dict) else str(err)
                    )
                    _cache_probe(server)
                    return server
                result = data.get("result")
                tools_data = result.get("tools") if isinstance(result, dict) else None
                if not isinstance(tools_data, list):
                    # Same malformed-response rule as the stdio path: no tools
                    # LIST is a failed probe, not an empty server.
                    server.status = "error"
                    server.error = "tools/list: malformed response (no tools list)"
                    _cache_probe(server)
                    return server
                server.tools = [
                    name
                    for t in tools_data
                    if isinstance(t, dict) and (name := t.get("name", ""))
                ]

        server.status = "ok"
    except asyncio.TimeoutError:
        server.status = "error"
        server.error = "timeout"
        logger.warning("MCP probe failed [%s]: timeout", server.name)
    except Exception as exc:
        server.status = "error"
        server.error = _sanitize_probe_error(exc)
        logger.warning("MCP probe failed [%s]: %s", server.name, server.error)

    _cache_probe(server)
    return server


# Cap on how many *non-JSON banner* lines to skip while waiting for the
# JSON-RPC handshake. Only undecodable banner/log lines count toward this cap;
# blank lines and well-formed JSON-RPC notifications are bounded by the shared
# timeout budget alone (so a chatty-but-spec-compliant server that emits many
# notifications before its response is not mis-capped). A well-behaved server
# emits its response immediately; a chatty launcher (e.g. ``aim`` mid-self-
# update) may prepend a banner line or two.
_MAX_BANNER_LINES = 50


async def _read_stdio_jsonrpc_response(
    stream: asyncio.StreamReader, timeout: float, name: str = ""
) -> dict | None:
    """Read stdout until a JSON-RPC *response* object appears.

    stdio MCP servers must speak newline-delimited JSON, but some processes —
    or launchers that front them, like ``aim`` while self-updating — print a
    human-readable banner or a blank line to stdout *before* the handshake.
    The probe used to read the first line and ``json.loads`` it directly, so a
    single stray line raised ``Expecting value: line 1 column 1 (char 0)`` and
    a healthy server was reported as errored (cached for up to 30 min).

    This consumes lines within one overall ``timeout`` budget, skipping blank
    lines, non-JSON lines, and JSON-RPC *notifications* (objects without an
    ``id``), and returns the first JSON object that carries an ``id`` (a
    response). Only non-JSON *banner* lines count toward ``_MAX_BANNER_LINES``;
    blanks and notifications are bounded by the timeout alone. Returns ``None``
    on EOF or once more than ``_MAX_BANNER_LINES`` banner lines have arrived
    (the flood case is logged). Raises ``asyncio.TimeoutError`` if the deadline
    elapses, so the caller's existing timeout handling is preserved.

    Note the divergent sibling: the remote HTTP/SSE path uses
    :func:`_read_jsonrpc_response`, which returns ``{}`` (not ``None``) on an
    empty response and does NOT filter notifications. Keep the two straight —
    do not copy one call site's null-handling to the other.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    banner_lines = 0
    first_banner = ""
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        line = await asyncio.wait_for(stream.readline(), timeout=remaining)
        if not line:
            # EOF — process closed stdout without responding. Preserve the
            # "non-JSON was on stdout" signal the old json.loads error used to
            # surface, so a banner-then-EOF probe is still diagnosable.
            if banner_lines:
                logger.debug(
                    "MCP probe [%s]: EOF after %d banner line(s); first banner: %r",
                    name or "?",
                    banner_lines,
                    first_banner,
                )
            return None
        text = line.decode(errors="replace").strip()
        if not text:
            continue  # blank line — bounded by the timeout budget, not the cap
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Non-JSON banner/log line (e.g. `aim` self-update). Only these
            # count toward the flood cap.
            banner_lines += 1
            if not first_banner:
                first_banner = text[:120]
            if banner_lines > _MAX_BANNER_LINES:
                logger.warning(
                    "MCP probe [%s]: no JSON-RPC response after %d banner "
                    "line(s); first banner: %r",
                    name or "?",
                    banner_lines,
                    first_banner,
                )
                return None
            continue
        # A JSON-RPC response always carries "id"; skip notifications (objects
        # with "method" and no "id") and non-object payloads. These do NOT
        # count toward the banner cap — the timeout budget bounds them.
        if isinstance(parsed, dict) and "id" in parsed:
            return parsed


async def probe_server(
    server: McpServerInfo, *, client_info: dict[str, str] | None = None
) -> McpServerInfo:
    """Probe a single MCP server by spawning it and sending initialize.

    Updates server.status and server.tools in place and returns it.

    *client_info* overrides the ``clientInfo`` sent in the handshake. The
    shareability pre-flight uses it to ask the same server twice under two
    identities: a server that negotiates its capabilities from ``clientInfo``
    answers differently, and that is precisely the case a pooled backend cannot
    serve — it caches the first stub's ``initialize`` result and replays it to
    every later stub. Callers that just want status and tools omit it and keep
    the probe's own identity.

    A consent-disabled server is refused HERE, ahead of the local/remote
    dispatch, because probing is the act that runs it: the local branch spawns
    the command and the remote branch opens the connection. Enforcement used to
    live in each caller (``probe_all`` filtered disabled rows before building
    coroutines), which made the guarantee only as good as the newest call
    site's memory — so a second entry point had to restate the check or become
    a way around the consent gate. Keeping the rule in the one function every
    probe must pass through removes that whole class; callers keep their own
    filters and error surfaces as behaviour and UX, not as the safety property.
    """
    if server.disabled:
        server.status = "disabled"
        # Truthy rather than ``is True``: a hand-built McpServerInfo may carry
        # anything here, and any non-empty value should withhold the spawn.
        #
        # No probe ran, so there is nothing to record — deliberately NOT
        # calling _cache_probe(). That cache is keyed by name and shared with
        # ``GET /api/mcp`` via _get_cached(), so writing an empty "disabled"
        # entry would erase the tool list a real probe stored before the user
        # disabled the server. ``tools`` is left untouched for the same reason
        # (last known list, still worth showing); ``error`` is cleared because
        # a stale probe failure is not why this returned.
        server.error = ""
        return server

    if server.is_remote:
        return await _probe_remote(server)

    if not server.command:
        server.status = "error"
        server.error = "no command"
        logger.warning("MCP probe failed [%s]: no command configured", server.name)
        return server

    server.status = "probing"
    # Stamped at probe START so the early error returns below (which skip the
    # cache) still carry an honest "when": the probe DID run at this time.
    # _cache_probe overwrites it with completion time on the paths it covers.
    server.probed_at = time.time()
    # Reset: the object may arrive carrying "declared" from a previous cached
    # result (list_servers merges the cache in), and this pass IS a handshake
    # unless the fallback below says otherwise.
    server.probe_mode = "handshake"
    proc = None
    sandbox_cleanup: str | None = None
    try:
        env = dict(os.environ)
        # The same expression backs command resolution and the PATH emitted into
        # the agent config (``agent.install_agent``) — sharing it is what keeps
        # "probes healthy" and "works in a session" from disagreeing. The pooled
        # backend path resolves a bare command separately
        # (``mcp_gateway.rewriter``), against a spec value this has already
        # expanded.
        # Case-insensitive PATH key: a Windows-authored spec says "Path", and
        # the emitted config normalizes under that same spelling — reading only
        # "PATH" here would probe with a different path than the session gets.
        _path_key = spec_path_key(server.env)
        _declared_path = server.env.get(_path_key, "") if _path_key else ""
        env["PATH"] = spec_env_path(_declared_path if isinstance(_declared_path, str) else "")
        # The declared env is untrusted config text applied to the environment
        # the SANDBOX LAUNCHER starts under, so loader/interpreter injection
        # keys must not pass through — they would execute before confinement
        # exists. See env.sanitize_spec_env; _note_denied_env explains a
        # resulting failure to the dashboard reader.
        env.update(
            sanitize_spec_env(
                (k, v) for k, v in server.env.items() if k != _path_key
            )
        )

        # Resolve command to absolute path using the merged env PATH
        resolved = shutil.which(server.command, path=env.get("PATH"))
        if not resolved:
            server.status = "error"
            server.error = f"command not found: {server.command}"
            _warn_unresolvable_once(server.name, server.command)
            return server

        # The command resolved, so forget any prior "not found" report — keyed on
        # resolvability, NOT on handshake health. Clearing this at the end of the
        # success path instead would skip the four exits that resolve fine but
        # fail later (no response, a JSON-RPC error reply, a timeout, any other
        # exception), leaving a stale key that silences the WARNING if the binary
        # is removed again. `command` is necessarily a str here, since
        # `shutil.which` returned truthy for it.
        _clear_unresolvable(server.name, server.command)

        # A hostile MCP-config entry names the binary spawned here, so route it
        # through the sandbox chokepoint: OS-level isolation plus a
        # credential-scrubbed environment (on top of the augmented PATH built
        # above). ``strip_python_env`` keeps KiroCrew's PYTHONPATH/PYTHONHOME out
        # of a foreign Python MCP server. See the related security-review finding.
        #
        # ``first_party_fixed_argv`` is True ONLY when command+args+env EQUAL
        # the invocation this package derives for its own managed servers
        # (``agent._kirocrew_mcp_invocation`` + ``agent._managed_mcp_env`` via
        # ``_is_first_party_managed_argv``) — self-derived, not user-config text
        # — so on a host with genuinely no sandbox backend the "can the server
        # start?" probe runs for real instead of fail-closing. Third-party
        # probes (and any customized managed command/args/env) pass False and
        # keep the full fail-close + opt-in behavior.
        wrapped_argv, env, sandbox_cleanup = sandboxed_spawn_argv(
            [resolved, *(server.args or [])],
            mode="standard",
            env=env,
            strip_python_env=True,
            first_party_fixed_argv=_is_first_party_managed_argv(
                server.name, server.command, server.args or [], server.env or {}
            ),
        )
        proc = await create_subprocess_limited(
            *wrapped_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=1024 * 1024,  # 1 MB — some MCP servers return large responses
            # POSIX: setsid so the probe owns a dedicated process group and
            # teardown can killpg launcher grandchildren (a leader-only kill
            # leaked ``npx @playwright/mcp`` -> node trees). Windows: silently
            # ignored (mirrors AcpRuntime / AcpClient._spawn).
            start_new_session=platform_compat.IS_POSIX,
        )

        # Send initialize request
        init_req = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": dict(client_info)
                        if client_info
                        else {"name": "kirocrew-probe", "version": "1.0.0"},
                    },
                }
            )
            + "\n"
        )

        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(init_req.encode())
        await proc.stdin.drain()

        # Read initialize response. Skip any leading non-JSON banner/log
        # lines the server (or a launcher like ``aim`` mid-self-update) may
        # emit on stdout before the JSON-RPC handshake — otherwise a single
        # stray line makes json.loads() raise and a healthy server is wrongly
        # marked errored.
        resp = await _read_stdio_jsonrpc_response(
            proc.stdout, _get_probe_timeout(), name=server.name
        )
        if resp is None:
            server.status = "error"
            server.error = "no response"
            return server

        if isinstance(resp, dict) and resp.get("error"):
            server.status = "error"
            err = resp["error"]
            server.error = (
                err.get("message", "unknown error") if isinstance(err, dict) else str(err)
            )
            return server

        # Keep the handshake metadata the probe already paid for. Read from the
        # server's ANSWER, never from what we asked for: a server may negotiate
        # down to an older protocol version, and that answer is exactly what
        # tells us whether tool annotations could have been sent at all.
        init_result = resp.get("result") if isinstance(resp, dict) else None
        if isinstance(init_result, dict):
            caps = init_result.get("capabilities")
            # An absent capabilities object and an empty one are different
            # claims; only a dict counts as "the server declared something".
            server.capabilities = caps if isinstance(caps, dict) else {}
            version = init_result.get("protocolVersion")
            server.protocol_version = version if isinstance(version, str) else ""
            info = init_result.get("serverInfo")
            server.server_info = info if isinstance(info, dict) else {}

        # Send initialized notification
        notif = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }
            )
            + "\n"
        )
        proc.stdin.write(notif.encode())
        await proc.stdin.drain()

        # Request tool list
        list_req = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                }
            )
            + "\n"
        )
        proc.stdin.write(list_req.encode())
        await proc.stdin.drain()

        resp2 = await _read_stdio_jsonrpc_response(
            proc.stdout, _get_probe_timeout(), name=server.name
        )
        if resp2 is None:
            # initialize succeeded but tools/list yielded no response (banner
            # flood or EOF on this read). "ok" here would certify a server no
            # session can get a tool out of — the whole point of the badge is
            # "tools usable", so a failed tools/list is a failed probe. Cached
            # (matching _probe_remote's error paths) so the list overlay shows
            # the same failure the direct probe reports, instead of serving a
            # stale prior "ok" for up to the TTL.
            server.status = "error"
            server.error = "tools/list: no response after a successful initialize"
            logger.warning(
                "MCP probe failed [%s]: tools/list returned no response after "
                "a successful initialize",
                server.name,
            )
            # Same synthetic-identity rule as the trailing cache write: a
            # pre-flight's second-identity run is a diagnostic, not the
            # canonical observation the dashboard renders.
            if client_info is None:
                _cache_probe(server)
            return server
        if isinstance(resp2, dict) and resp2.get("error"):
            err2 = resp2["error"]
            server.status = "error"
            server.error = "tools/list: " + (
                err2.get("message", "unknown error") if isinstance(err2, dict) else str(err2)
            )
            if client_info is None:
                _cache_probe(server)
            return server
        result = resp2.get("result", {}) if isinstance(resp2, dict) else {}
        tools_data = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools_data, list):
            # A response without a tools LIST is malformed, not "no tools":
            # rendering it as a green zero-tool server would certify a server
            # whose one required answer didn't parse. A genuinely tool-less
            # server sends an empty list, which passes.
            server.status = "error"
            server.error = "tools/list: malformed response (no tools list)"
            if client_info is None:
                _cache_probe(server)
            return server
        server.tools = [
            name for t in tools_data if isinstance(t, dict) and (name := t.get("name", ""))
        ]
        # ``annotations`` (MCP 2025-03-26+) is the only spec-native hint
        # about whether a tool mutates anything. Collected as positive
        # evidence only — a server on an older protocol version sends none,
        # and that must never read as "this tool writes".
        server.tool_annotations = [
            ann
            for t in tools_data
            if isinstance(t, dict) and isinstance(ann := t.get("annotations"), dict)
        ]

        server.status = "ok"

    except asyncio.TimeoutError:
        server.status = "error"
        server.error = "timeout"
        logger.warning(
            "MCP probe failed [%s]: timeout after %ds", server.name, _get_probe_timeout()
        )
    except FileNotFoundError:
        server.status = "error"
        server.error = f"command not found: {server.command}"
        _warn_unresolvable_once(server.name, server.command)
    except SandboxUnavailableError as exc:
        # The PROBE could not run — this says nothing about the server, and the
        # two must not be reported alike. Ahead of the generic clause, which would
        # render this as a server fault.
        #
        # For one of OUR OWN managed servers there is a better answer than an
        # error: its tool list is a static declaration in this package
        # (``mcp_core._list_tools()`` and friends — the very functions the stdio
        # shim answers ``tools/list`` from), so read it directly. That is what
        # keeps the built-in tools listed on a host with no sandbox backend (any
        # Windows host, macOS >= 26) without asking the operator for an
        # ``agent.sandbox_allow_unsandboxed_exec`` opt-in for a read-only
        # listing. A managed server whose command+args are self-derived normally
        # never reaches here on such a host — the first-party carve-out lets its
        # probe spawn for real — so this fallback covers the residual cases: a
        # transient sandbox failure, a foreign outer sandbox, a governance
        # sandbox floor, or a customized command.
        #
        # Deliberately a FALLBACK, not the primary path. Two reasons:
        #   * the spawn is the only thing that proves the server can actually
        #     START. `_fix_stale_managed_command` exists because that invocation
        #     does go stale ("command not found: kirocrew; the built-in cron/core
        #     tools then never load"), and short-circuiting on the name alone would
        #     report `ok` for a managed server that cannot run — changing what `ok`
        #     means in the shared `_cache_probe` store, silently, for the one
        #     surface that used to catch it.
        #   * importing these modules runs package code IN THE GATEWAY PROCESS,
        #     which the gateway does not otherwise do (they are absent from
        #     sys.modules at boot). The package dir is writable by the same uid the
        #     agent runs as and is not on the sensitive-path floor, so on a host
        #     where the sandbox DOES work, importing beats the isolation the spawn
        #     provides. Reaching here means the sandbox could not confine anything
        #     anyway, so the import adds no exposure the refused spawn had not
        #     already conceded — and it is the only way to serve the listing there.
        managed_tools = _managed_tools_in_process(server.name)
        if managed_tools is not None:
            server.status = "ok"
            server.tools = managed_tools
            server.error = ""
            # Not a handshake: nothing verified the server can START. The mode
            # rides the cache into the API payload so the UI can distinguish a
            # declared listing from a proven one instead of rendering both as
            # the same green badge.
            server.probe_mode = "declared"
            _warn_managed_in_process_once(server.name)
            if client_info is None:
                _cache_probe(server)
            return server
        #
        # The wrap is deliberately KEPT rather than skipped for Kiro Crew's own
        # managed servers. "It is our own code" is not the same claim as "the code
        # is unmodified": the package directory is writable by the same uid the
        # agent runs as and is not on the sensitive-path floor, so a prompt-injected
        # agent can edit an editable checkout (or the console script) and an
        # unwrapped probe would then execute it outside the sandbox on the next
        # automatic probe_all(). Skipping the wrap for a managed server would make
        # this the one unsandboxed spawn path in the codebase; the sibling paths
        # (script crons, script hooks, Papyrus compile/git) all keep the wrap and
        # require the opt-in on a backendless host, and this now matches them.
        #
        # So what changes is the REPORTING. The `mcp_probe_` prefix is
        # machine-readable, mirroring the `code` field on the dashboard's JSON error
        # bodies, so a presentation layer can tell an unfixable-by-retry probe
        # limitation apart from a genuine handshake failure without parsing prose.
        server.status = "error"
        server.error = (
            f"mcp_probe_sandbox_unavailable: Kiro Crew could not probe this server "
            f"because no OS-level sandbox backend is available on this host. The "
            f"server itself may be fine — kiro-cli launches it from the agent "
            f"config without this probe, so its tools can still work in chat. "
            f"Set agent.sandbox_allow_unsandboxed_exec=true to enable probing. "
            f"({_sanitize_probe_error(exc)})"
        )
        _warn_probe_sandbox_unavailable_once(server.name)
    except Exception as exc:
        server.status = "error"
        server.error = _sanitize_probe_error(exc)
        logger.warning("MCP probe failed [%s]: %s", server.name, server.error)
    finally:
        # When the probe failed, drain any stderr the child wrote and append
        # a redacted tail to the error message. Most MCP servers print a
        # useful diagnostic (Python traceback, ModuleNotFoundError,
        # a build-tool exception, etc.) on startup failure;
        # without this, callers only see opaque strings like "timeout" or
        # "no response" with no hint of the underlying cause.
        #
        # stderr is untrusted process output that could contain leaked
        # credentials or exfiltration URLs, so scrub it with the security
        # redactors before it reaches doctor output / dashboard / Slack.
        if proc is not None and proc.stderr is not None and server.status == "error":
            try:
                stderr_bytes = await asyncio.wait_for(proc.stderr.read(4096), timeout=1.0)
                stderr_tail = stderr_bytes.decode(errors="replace").strip()
                if stderr_tail:
                    clean, _ = redact_exfiltration_urls(stderr_tail)
                    clean, _ = redact_credentials(clean)
                    server.error = f"{server.error}\nstderr: {clean[:500]}"
            except (asyncio.TimeoutError, Exception):
                pass
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=_PROBE_TEARDOWN_WAIT_SECS)
            except (asyncio.TimeoutError, Exception):
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=_PROBE_TEARDOWN_WAIT_SECS)
                except Exception:
                    pass
        if proc is not None and platform_compat.IS_POSIX:
            # Reap the probe's ENTIRE process group. The child was spawned with
            # start_new_session=True, so pgid == proc.pid and the group holds
            # any grandchildren the launcher forked (``npx`` / ``node`` shim ->
            # real MCP server). A leader-only kill — and even a graceful leader
            # exit — leaves those grandchildren alive, accumulating one leaked
            # tree per failed probe per discovery cycle. Race-free even after
            # the leader was reaped: a PID in use as a pgid cannot be recycled
            # while any group member lives, so killpg hits only our group or
            # raises ESRCH on an empty one. The int/>1 guard mirrors
            # _sync_kill_provider: a mock stand-in pid must never coerce this
            # into killpg(1) == init.
            probe_pid = proc.pid
            if isinstance(probe_pid, int) and probe_pid > 1:
                try:
                    os.killpg(probe_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # group already empty — nothing leaked
                except OSError:
                    logger.debug(
                        "Probe group reap failed for %s (pgid %s)",
                        server.name,
                        probe_pid,
                        exc_info=True,
                    )
        elif proc is not None and platform_compat.IS_WINDOWS:
            # Windows has no process groups; reap the probe's whole tree via
            # taskkill /T so the launcher's grandchildren (npx/node -> real MCP
            # server) don't leak one tree per failed probe per discovery cycle.
            probe_pid = proc.pid
            if isinstance(probe_pid, int) and probe_pid > 1:
                try:
                    # OFF the event loop: on Windows kill_process_tree shells
                    # out to ``taskkill /T /F`` via a blocking
                    # ``subprocess.run``. Awaited inline it stalls the loop for
                    # the whole spawn+kill of taskkill — once per failed probe,
                    # and ``probe_all`` fans out across every configured server,
                    # so a discovery pass with several unreachable servers
                    # serializes that many process spawns onto the loop and the
                    # dashboard's health check starts dropping. The POSIX branch
                    # above is a bare ``killpg`` syscall and needs no offload.
                    await asyncio.to_thread(
                        platform_compat.kill_process_tree, probe_pid, platform_compat.SIGKILL
                    )
                except (ProcessLookupError, OSError):
                    logger.debug(
                        "Probe tree reap failed for %s (pid %s)",
                        server.name, probe_pid, exc_info=True,
                    )
        if sandbox_cleanup:
            Path(sandbox_cleanup).unlink(missing_ok=True)

    # A probe run under a SYNTHETIC identity must not become the cached truth:
    # the per-name cache is what ``GET /api/mcp`` renders, and a pre-flight's
    # second-identity handshake is a diagnostic, not the canonical observation.
    if client_info is None:
        _cache_probe(server)
    return server


# Cap how many MCP servers we probe concurrently.  Each probe spawns a
# subprocess (or opens a remote connection) and resolves DNS on the event
# loop's default executor; an unbounded fan-out across 25+ servers floods that
# pool during a network blip and stalls the loop.
PROBE_MAX_CONCURRENCY = 5
#: Public because the shareability pre-flight bounds its own fan-out by the same
#: number: those spawns land in this executor too, so two independent caps would
#: let one pass flood the pool the other is protecting.


async def probe_all() -> list[McpServerInfo]:
    """Discover and probe all configured MCP servers (bounded concurrency).

    Consent-disabled rows are excluded: probing spawns the server process,
    and a disabled server must never run until the user enables it.

    ``probe_server`` now refuses a disabled server on its own, so this filter
    is defense-in-depth (the idiom ``sync_to_agent_config`` already uses) plus
    the thing that shapes the RESULT: disabled rows are left out of the
    returned list entirely rather than reported with ``status="disabled"``,
    which is the response shape ``GET /api/mcp/probe`` has always had.
    """
    servers = [s for s in list_servers() if not s.disabled]
    # Keep the warn-once ledger bounded by the config rather than by config
    # churn: a command edited to a different missing binary must not retain the
    # superseded string. Runs before the early return so emptying the config
    # (or disabling every server) also clears it.
    #
    # `command` is whatever the config JSON held (`spec.get("command", "")`,
    # unvalidated), so a malformed entry can be a dict or list. Those are
    # unhashable and would abort this whole pass — not just their own server —
    # because this runs outside the per-server `gather`. Only string commands
    # can be ledger keys anyway, so skip the rest and let each malformed server
    # keep failing in isolation inside `probe_server`.
    _prune_unresolvable({(s.name, s.command) for s in servers if isinstance(s.command, str)})
    if not servers:
        return []
    # Per-call semaphore: bounds the fan-out within this discovery pass while
    # binding to the currently-running loop (avoids import-time loop capture).
    sem = asyncio.Semaphore(PROBE_MAX_CONCURRENCY)

    async def _guarded(s: McpServerInfo) -> McpServerInfo:
        async with sem:
            return await probe_server(s)

    results = await asyncio.gather(
        *(_guarded(s) for s in servers),
        return_exceptions=True,
    )
    out: list[McpServerInfo] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            servers[i].status = "error"
            servers[i].error = _sanitize_probe_error(r)
            logger.warning("MCP probe failed [%s]: %s", servers[i].name, servers[i].error)
            out.append(servers[i])
        else:
            out.append(r)  # type: ignore[arg-type]
    for s in out:
        _note_denied_env(s)
    return out


def _note_denied_env(server: McpServerInfo) -> None:
    """Name the policy-dropped env keys on a FAILED probe.

    The probe strips loader/interpreter injection keys before spawning, so a
    server configured through one of them (a Python server using
    ``env.PYTHONPATH`` is the realistic case) can fail here while working in a
    session, where kiro-cli spawns it with no launcher of ours in the chain.
    The sanitizer logs each drop, but the person reading a red badge is looking
    at the dashboard, not the gateway log — without this the row reads as a
    probe bug instead of a deliberate boundary. Only annotates errors: on a
    success the drop changed nothing worth reporting.
    """
    if server.status != "error" or not server.error:
        return
    dropped = denied_spec_env_keys(server.env or {})
    if not dropped:
        return
    server.error = (
        f"{server.error} (probe dropped declared env "
        f"{', '.join(sorted(dropped))}: these execute in the sandbox launcher "
        f"before confinement, so the probe cannot honour them — a session still does)"
    )


def _commands_diverged(source_cmd: str, agent_cmd: str) -> bool:
    """Compare MCP commands accounting for path resolution.

    The agent config stores resolved absolute paths (e.g.
    /home/user/.local/bin/deep-research) while mcp.json stores the
    short name (deep-research). These refer to the same binary and
    should not trigger a sync.
    """
    if source_cmd == agent_cmd:
        return False
    # Two RESOLVED paths for one binary, differing only in separator flavour or
    # case (``C:\tools\srv.exe`` vs ``C:/Tools/SRV.exe``). Windows itself treats
    # those as the same file, so comparing the strings re-syncs forever.
    if platform_compat.IS_WINDOWS and _names_a_location(source_cmd) and _names_a_location(agent_cmd):
        if ntpath.normcase(ntpath.normpath(source_cmd)) == ntpath.normcase(
            ntpath.normpath(agent_cmd)
        ):
            return False
    # If one is an absolute resolved path of the other, they match. Test both
    # path flavors regardless of host OS: on Windows ``os.path is ntpath`` and
    # would treat a POSIX-absolute config path (/usr/bin/server) as relative,
    # so a resolved-vs-short pair authored on POSIX would spuriously read as
    # diverged and trigger an endless re-sync.
    if _names_a_location(agent_cmd) and _basenames_match(agent_cmd, source_cmd):
        return False
    if _names_a_location(source_cmd) and _basenames_match(source_cmd, agent_cmd):
        return False
    return True


def _envs_agree(agent_env: dict, source_env: dict) -> bool:
    """True when the agent config's ``env`` already carries *source_env*.

    Every source key must be present in the agent entry, EXCEPT that ``PATH`` is
    compared through :func:`kiro_crew.env.spec_env_path` — the agent config
    stores the expanded effective PATH while mcp.json stores the fragment the
    user authored, exactly as it stores a resolved absolute command against a
    bare name (see :func:`_commands_diverged`). Comparing the raw strings would
    read every already-synced server as diverged and re-sync it forever.

    A superset is fine: keys the agent entry adds on its own (a managed pin) are
    not the source's business.
    """
    for key, val in source_env.items():
        current = agent_env.get(key)
        if key == "PATH" and val:
            if current == val or current == spec_env_path(val):
                continue
            return False
        if current != val:
            return False
    return True


def _names_a_location(cmd: str) -> bool:
    """True when *cmd* is a path rather than a bare ``PATH`` lookup name.

    Broader than :func:`_is_abs_any` by one Windows case: ``ntpath.isabs``
    rejects a DRIVELESS root (``\\tools\\srv``) because it is absolute only
    relative to the current drive — yet such a string still names a location
    whose basename is meaningful. A relative path (``bin/srv``, ``./srv``) is
    deliberately NOT a location for this purpose: it designates a specific file
    relative to the CWD, so it must not match an unrelated rooted path that
    merely shares a basename.
    """
    if _is_abs_any(cmd):
        return True
    return platform_compat.IS_WINDOWS and cmd[:1] in ("/", "\\")


def _basenames_match(resolved: str, bare: str) -> bool:
    """True when *resolved*'s basename names the same binary as *bare*.

    On Windows the resolver (``shutil.which``, via ``agent._resolve_command``)
    appends the extension as ``PATHEXT`` spells it — commonly UPPER case — so
    ``npx`` resolves to ``...\\npx.CMD``. An exact basename comparison therefore
    reports every stdio MCP server as diverged forever, and each discovery pass
    re-syncs it. Fold the executable suffix and the case, both of which Windows
    itself ignores. POSIX keeps the exact comparison: paths are case-sensitive
    there and an extension is part of the name.
    """
    name = _basename_any(resolved)
    if name == bare:
        return True
    if not platform_compat.IS_WINDOWS:
        return False
    name, bare = name.casefold(), bare.casefold()
    if name == bare:
        return True
    stem, ext = ntpath.splitext(name)
    return bool(ext) and ext in _win_exec_suffixes() and stem == bare


# Executable suffixes Windows appends when resolving a bare command name. Read
# live from ``PATHEXT`` so a host that customizes it is honored; the fallback
# mirrors the Windows default for the pathological case of it being unset.
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


def _win_exec_suffixes() -> frozenset[str]:
    """Lower-cased ``PATHEXT`` suffixes."""
    raw = os.environ.get("PATHEXT") or _DEFAULT_PATHEXT
    return frozenset(
        s for s in (part.strip().casefold() for part in raw.split(os.pathsep)) if s.startswith(".")
    )


def _is_abs_any(cmd: str) -> bool:
    """True if ``cmd`` is absolute under POSIX or Windows path rules."""
    return posixpath.isabs(cmd) or ntpath.isabs(cmd)


def _basename_any(cmd: str) -> str:
    """Basename of ``cmd`` under whichever path flavor treats it as absolute.

    A backslash-bearing string takes the Windows flavour even when
    ``ntpath.isabs`` is False, which a DRIVELESS root (``\\tools\\srv``) is.
    ``posixpath.basename`` does not know ``\\`` is a separator, so it would
    return the whole string and the basename comparison could never match.
    """
    if ntpath.isabs(cmd) or "\\" in cmd:
        return ntpath.basename(cmd)
    return posixpath.basename(cmd)


def discover_servers_to_sync() -> list[McpServerInfo]:
    """Find MCP servers in mcp.json that need syncing to the agent config.

    Returns new servers not yet in the agent config, plus existing servers
    whose source-owned transport fields have diverged from mcp.json.
    """
    agent_cfg = _load_agent_config()
    agent_mcp = agent_cfg.get("mcpServers", {})
    agent_names = set(agent_mcp.keys())
    mcp_servers = _load_mcp_json()

    out: list[McpServerInfo] = []
    for name, spec in mcp_servers.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("disabled"):
            continue
        info = McpServerInfo(
            name=name,
            command=spec.get("command", ""),
            args=spec.get("args"),
            env=spec.get("env") or {},
            url=spec.get("url", ""),
            headers=spec.get("headers") or {},
            scopes=_spec_scopes(spec),
            client_id=_spec_client_id(spec),
            source="discovered",
        )
        if name not in agent_names:
            out.append(info)
        else:
            # Args divergence is intentionally excluded: user-customized
            # args (e.g. --include-tools additions) are preserved by
            # install_agent()'s setdefault merge, so triggering a full
            # rebuild on args-only differences is wasted work.
            existing = agent_mcp[name]
            if not isinstance(existing, dict):
                continue
            if info.is_remote:
                existing_headers = existing.get("headers") or {}
                # scopes/clientId are source-owned transport fields like url and
                # headers: a registry Connect that adds or changes them must
                # re-sync, or the agent config keeps authorizing the old shape.
                #
                # Reaching this set is not permission to rewrite anything: each
                # consumer guards its own surface, so the two that Kiro Crew does
                # not own -- the kiro-global file and the Claude Code sidecar --
                # decide for themselves what an existing entry allows.
                if (
                    existing.get("url", "") != info.url
                    or existing_headers != info.headers
                    or _spec_scopes(existing) != info.scopes
                    or _spec_client_id(existing) != info.client_id
                ):
                    out.append(info)
                continue
            existing_env = existing.get("env", {})
            if not isinstance(existing_env, dict):
                existing_env = {}
            if not _envs_agree(existing_env, info.env) or _commands_diverged(
                info.command, existing.get("command", "")
            ):
                out.append(info)
    return out


def sync_to_agent_config(servers: list[McpServerInfo]) -> bool:
    """Sync discovered MCP servers into the agent config.

    Delegates to ``install_agent()`` — the single authoritative merge function
    that reads all source files (``~/.kiro/crew/mcp.json``,
    ``~/.kiro/settings/mcp.json``), merges them with correct priority, resolves
    commands, normalizes each spec's ``env`` (see ``env.emit_env``), and writes
    the final agent config. There is deliberately no second registration path:
    a ``kiro-cli mcp add`` subprocess used to run here for cosmetic parity with
    ``kiro-cli mcp list``, but it was an unsynchronized second writer of the
    same file with its own (unnormalized) env serialization, and everything it
    wrote was rewritten by ``install_agent()`` moments later.

    Returns True if any servers were synced.
    """
    from kiro_crew.agent import install_agent  # circular import

    install_agent()

    # Audit: log which servers triggered the config rebuild
    try:
        from kiro_crew.sel import sel  # circular import

        sel().log_api_access(
            caller="system",
            operation="mcp_server_config_sync",
            outcome="ok",
            source="agent",
            resources=", ".join(s.name for s in servers),
        )
    except Exception:
        logger.debug("SEL audit log failed for mcp_server_config_sync", exc_info=True)

    return bool(servers)


# One mutex for the whole discover→write sequence. Two dashboard handlers run
# it (``POST /api/mcp/sync`` and the sessions-restart pre-sync), and each write
# is a read-modify-write of shared files — unserialized, two concurrent runs
# can both discover, then interleave writes and drop each other's changes.
_SYNC_MUTEX = threading.Lock()


def sync_discovered_servers() -> list[McpServerInfo]:
    """Reconcile the consumed configs with the sources, in one serialized step.

    The single entry point for "make the consumed configs match the sources":
    the agent-config rebuild runs UNCONDITIONALLY — ``install_agent()`` is the
    idempotent reconciler, and skipping it when discovery reports no new or
    diverged servers misses the changes discovery deliberately does not
    report, e.g. a source entry gaining ``disabled: true`` (discovery skips
    disabled entries, but the rebuild is what removes the server from the
    agent config). The Claude Code sidecar write is additive-only, so it runs
    just for the discovered delta. Everything happens under one lock so
    concurrent callers cannot interleave. Blocking file I/O — call via
    ``asyncio.to_thread`` from a handler.

    Returns the servers discovery flagged (new or diverged; empty when none —
    which, deliberately, no longer implies nothing was written).
    """
    with _SYNC_MUTEX:
        to_sync = discover_servers_to_sync()
        sync_to_agent_config(to_sync)
        if to_sync:
            register_servers_for_cc(to_sync)
        return to_sync


def kirocrew_managed_names() -> set[str]:
    """Server names the dashboard store owns.

    A usable dict under the ``kirocrew`` scope (``<data home>/mcp.json``) is the
    one signal that Kiro Crew manages a name -- the same discriminator the
    agent-spec emit path uses for its OAuth hints, so management means one thing
    everywhere.

    This is the store-side half of the ownership predicate and a NECESSARY
    precondition for every write to a config surface we do not own -- the
    kiro-global ``mcp.json``, the Claude Code ``~/.mcp.json`` sidecar. Discovery
    merges ALL scopes, so a name present only in a user's global file reaches the
    sync set exactly like a managed one; without the gate a Kiro Crew sync would
    rewrite a server the user configured by hand.

    It is deliberately NOT sufficient. Managing a NAME says nothing about who
    wrote a given ENTRY, and the two answers differ per file -- an entry can be
    ours in the kiro-global file and the user's in the sidecar. That half is
    :func:`kiro_crew.mcp_provenance.resolve_write`, which reads the marker on the
    entry itself. Keeping this function name-only is what lets a single set answer
    for every surface without silently answering for the wrong one.

    A malformed store value is skipped for the same reason the merge skips it: it
    contributed nothing, so it cannot make the name ours.
    """
    by_source = _load_mcp_json_by_source()
    return {
        name
        for name, spec in by_source.get(SCOPE_KIROCREW, {}).items()
        if isinstance(spec, dict)
    }


def register_servers_for_cc(
    servers: list[McpServerInfo],
    mcp_json_path: Path | None = None,
) -> bool:
    """Register MCP servers in CC format (.mcp.json).

    Adds entries without removing existing ones. CC-side complement
    to sync_to_agent_config() which handles kiro-side registration.

    A remote entry is rewritten only when it carries our authorship marker --
    see the loop below. So is a stdio entry: the marker records who wrote an
    entry, which no transport makes knowable on its own.

    Returns True if any servers were added or updated.
    """
    if mcp_json_path is None:
        mcp_json_path = Path.home() / ".mcp.json"

    existing: dict = {}
    if mcp_json_path.is_file():
        try:
            existing = json.loads(mcp_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    mcp = existing.setdefault("mcpServers", {})
    changed = False
    # OAuth hints ride along when a remote is first registered, and only for a
    # name we own -- see kirocrew_managed_names.
    _managed = kirocrew_managed_names()

    for s in servers:
        if s.is_remote:
            entry: dict = {"url": s.url}
            if s.headers:
                entry["headers"] = s.headers
            if s.name in _managed:
                if s.scopes:
                    entry["scopes"] = list(s.scopes)
                if s.client_id:
                    entry["clientId"] = s.client_id
        else:
            entry = {"command": s.command, "args": s.args or [], "type": "stdio"}
            if s.env:
                # The sidecar is consumed by the external ``claude`` CLI, whose
                # env semantics this repo cannot observe — emit through the
                # shared normalization point so a declared PATH is complete
                # under the strictest (replace-per-key) reading. See emit_env.
                entry["env"] = emit_env(s.env)

        # This writer rebuilds an entry from scratch, so rewriting one we did not
        # author would drop the fields it does not reconstruct. The marker says
        # which ones those are: an entry we wrote re-syncs (its url or command
        # legitimately moves), an unmarked entry is the user's and stays add-only,
        # exactly as this surface behaved before the marker existed. The gate is
        # per ENTRY, not per transport -- a ``command`` makes authorship no more
        # knowable than a ``url`` does, and this loop rewrites a diverging stdio
        # entry in place, so a user's own server sharing a managed name reaches
        # the same collision. ``ABSENT`` rather than ``None``: a hand-edited file
        # can hold ``null`` under a name, and that occupies the name.
        resolved = resolve_write(
            name=s.name,
            on_disk=mcp.get(s.name, ABSENT),
            candidate=entry,
            store_managed=s.name in _managed,
            surface=_CC_SIDECAR_SURFACE,
        )
        if resolved is None:
            continue
        entry = resolved

        if s.name not in mcp or mcp[s.name] != entry:
            mcp[s.name] = entry
            changed = True
            logger.info("Registered MCP server for CC: %s", s.name)

    if changed:
        mcp_json_path.parent.mkdir(parents=True, exist_ok=True)
        from kiro_crew.agent import (
            _atomic_json_write,  # circular import: agent imports mcp_discovery
        )

        _atomic_json_write(mcp_json_path, existing)

    return changed
