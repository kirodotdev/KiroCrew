"""The discovered MCP server record: ``McpServerInfo``, the scopes and config
sources a server is read from, and the redaction its serialization applies.

``McpServerInfo.to_dict`` is the serialization boundary of every dashboard MCP
listing: ``redact_mcp_headers`` and ``redact_mcp_error`` mask header values and
reflected credentials there before a payload leaves the backend. The scope names
and the per-call source tables say where a server was declared and which
declaration wins.

:mod:`kiro_crew.mcp_discovery` stays the import path and the patch surface for
every name here; that module's comment lists the owners.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.config.paths import data_home
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

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
# scanning a file it cannot manage (which would surface un-uninstallable
# "zombie" servers).
#
# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage_store.py is the reference implementation.
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
# global. This matches ``rebuild_agent_config``'s merge order in agent_parts/spec_rebuild.py
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
            alternatives.append("".join(f"%(?:25)*{byte:02X}" for byte in char.encode("utf-8")))
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
    return {name: MCP_REDACTED_HEADER_VALUE for name in headers if isinstance(name, str)}


def redact_mcp_error(error: object, headers: object, extra_values: Iterable[str] = ()) -> str:
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
                needs_boundary = len(credential) < _MCP_CREDENTIAL_UNANCHORED_MIN_LENGTH
                values.setdefault(credential, needs_boundary)

    # Individually resolved placeholder values (see _expand_header_placeholders):
    # a PARTIALLY expanded header sends `<resolved>${MISSING}`, so neither the
    # full sent value nor the Authorization suffix matches a server echoing only
    # the resolved fragment. Same length regimes as credential suffixes: below
    # the minimum no boundary rule separates the value from prose words, so it
    # is skipped rather than corrupting unrelated text.
    for raw_extra in extra_values:
        if not isinstance(raw_extra, str):
            continue
        extra = raw_extra.strip()
        if len(extra) < _MCP_CREDENTIAL_SUFFIX_MIN_LENGTH:
            continue
        values.setdefault(extra, len(extra) < _MCP_CREDENTIAL_UNANCHORED_MIN_LENGTH)

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
    # The header map the probe actually SENT: config values with ${VAR}/${env:VAR}
    # references resolved (see :func:`_expand_header_placeholders`). ``None``
    # until a remote probe runs. Redaction keys on these — an error echoing the
    # RESOLVED secret carries bytes the configured map never held, and
    # ``redact_mcp_error``'s layer-2 scrubber matches exact values. Never
    # serialized: ``to_dict``'s headers block goes through ``redact_mcp_headers``,
    # which is name-keyed and value-independent.
    sent_headers: dict[str, str] | None = None
    # Every placeholder value the probe's expansion resolved, individually —
    # the extra_values leg of the redact_mcp_error scrub set. A partially
    # expanded header's sent value would not match a server echoing only the
    # resolved fragment. Never serialized.
    resolved_header_values: list[str] | None = None
    # Remote-only OAuth hints carried verbatim to the runtime, which owns the
    # authorization exchange. Kiro Crew never enforces scopes and never registers
    # a client — it only refuses to lose these fields while syncing.
    scopes: list[str] = field(default_factory=list)
    client_id: str = ""
    # unknown | ok | error | probing | outdated | disabled | needs_auth | registry
    status: str = "unknown"
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
    # True when ANY scope's entry for this server is muted by the shared launch
    # predicate (``mcp_entry_is_muted``): ``disabled: true`` -- a consent-disabled
    # install/custom add, or a server the user switched off in the dashboard
    # (``/api/mcp/toggle`` writes the flag into the Kiro-global ``mcp.json``) --
    # or a ``disabled`` that is not a boolean at all, which is read FAIL-CLOSED.
    # Disabled rows are NEVER probed — probing spawns the server process, which
    # is what consent gates. The refusal is enforced inside ``probe_server``
    # itself, so setting this flag is sufficient no matter which entry point does
    # the probing.
    disabled: bool = False
    # True for a commandless, urlless ``"type": "registry"`` pointer: an entry
    # kiro-cli resolves by map key against the admin's catalog, which only
    # kiro-cli can read. Such a row is never spawned and reports ``registry``
    # rather than "no command" -- there is no local process to check.
    registry_pointer: bool = False
    # ``"invalid"`` when the row is disabled by NOTHING but non-boolean
    # ``disabled`` values (``"false"``, ``1``, ``null``) -- no source carries a
    # literal ``true``. The table then says "invalid value in <file>" rather than
    # "disabled in <file>", because the operator's fix differs: repair the value,
    # not flip a switch. ``None`` when enabled, or when some source really says
    # ``true`` (the row is off either way, and the switch is the honest story).
    disabled_reason: str | None = None
    # True when a scope OTHER than the Kiro Crew store mutes this row -- the
    # Kiro-global ``mcp.json`` or a provider global: a disable the dashboard
    # never lifts by editing the store. Set with ``disabled`` in step 3c and
    # consumed by the ``GET /api/mcp`` stamping, which holds only the Kiro-global
    # and store maps: without it a server disabled in a provider global AND the
    # store would read as a store-only (consent) disable, and Apply would lift
    # the store flag while the provider-global one stood.
    disabled_in_shared: bool = False
    # THIS source's own ``timeout``/``disabled``, verbatim as the scope spec
    # declares them; an absent key is absent here too. Deliberately separate
    # from ``disabled`` above, which is an aggregate across every scope: a sync
    # trigger comparing an aggregate against one generated entry would fire a
    # sync that can never converge. Keyed by ``agent._SOURCE_OWNED_MCP_KEYS``,
    # the set ``_merge_source_owned`` can actually reconcile.
    source_owned: dict[str, Any] = field(default_factory=dict)
    # -- handshake metadata (probe-only; empty on unprobed rows) -----------
    # The server's advertised ``capabilities`` object, verbatim. ``None`` means
    # no handshake happened, which is NOT the same as an empty declaration.
    capabilities: dict[str, Any] | None = None
    # The ``protocolVersion`` the server answered with — not the one requested.
    # Tool annotations only exist from MCP's March 2025 revision, so this is what makes
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
    # -- authorization state (remote probes only) ---------------------------
    # True when the probe response carried a recognisable OAuth challenge, False
    # when it did not. False is genuinely "not known to need OAuth" and not "does
    # not need it": a server can refuse a tokenless probe without saying why.
    #
    # A boolean rather than the scheme name because that is all any consumer asks.
    # The challenge's scope list and RFC 9728 metadata URL are parsed (they are
    # what makes the challenge recognisable) but deliberately not carried: nothing
    # renders them, and an exported field with no reader is surface without a
    # purpose.
    auth_challenge: bool = False
    # Whether the runtime holds a grant for this url: True/False are observations,
    # None means the lookup could not answer. Only meaningful alongside
    # ``auth_challenge``; see :func:`_runtime_grant_present`.
    auth_grant_present: bool | None = None
    # Spec-declared temp keys the local probe refused, one dict per key:
    # ``key``, ``path`` (redacted like the WARNING) and ``cause`` (``sealed``,
    # ``unclassifiable`` or ``check-failed``). Same facts as the journal line,
    # so the dashboard row can say the probe ran with the managed temp instead.
    temp_refusals: list[dict[str, str]] = field(default_factory=list)

    @property
    def is_remote(self) -> bool:
        """True for Streamable HTTP servers (url-based, no command)."""
        return bool(self.url) and not self.command

    @property
    def redaction_headers(self) -> dict[str, str]:
        """The value set a probe error could echo, for ``redact_mcp_error``.

        The headers actually sent when the probe expanded references, else the
        configured map. The sent map is the COMPLETE scrub set: any configured
        value that differs post-expansion never left the process unexpanded,
        so a remote server can only echo the resolved spelling — and a value
        the expansion left alone is byte-identical in both maps.
        """
        return self.headers if self.sent_headers is None else self.sent_headers

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "command": self.command,
            "args": self.args or [],
            "status": self.status,
            "tools": self.tools,
            "error": redact_mcp_error(
                self.error, self.redaction_headers, self.resolved_header_values or ()
            ),
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
            # Gated on ``auth_challenge`` being set, so an absent key means "this
            # probe learned nothing about authorization" and a present
            # ``authGrantPresent`` is always a real observation. A client that
            # cannot tell those apart would render "sign-in required" for every
            # unprobed remote row.
            if self.auth_challenge:
                d["authChallenge"] = True
                # Omitted when the lookup could not answer, so a client never sees
                # "couldn't observe" as "observed absent" -- absence renders as the
                # safe wording, a false would name an action.
                if self.auth_grant_present is not None:
                    d["authGrantPresent"] = self.auth_grant_present
        # Omitted when empty, like ``authChallenge``: absent means no refusal.
        if self.temp_refusals:
            d["tempRefusals"] = [dict(r) for r in self.temp_refusals]
        if self.disabled_tools:
            d["disabledTools"] = self.disabled_tools
        if self.disabled:
            d["disabled"] = True
            if self.disabled_reason:
                d["disabledReason"] = self.disabled_reason
            if self.disabled_in_shared:
                # Consumed (popped) by the handler's stamping, which turns it
                # into ``disabledIn: "shared"``; not part of the table's contract.
                d["disabledInShared"] = True
        return d
