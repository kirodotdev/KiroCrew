"""What the MCP probes share: the protocol revision every MCP client handshake offers and
steps down to, the timeouts and error cap, the once-only warning ledgers, and the
probe-result cache.

The cache is keyed by server name, and each entry records the config identity it
was probed with (``_probe_identity``), so a stored verdict answers only for that
configuration; ``_cache_probe`` stores a result with its error already redacted.

:mod:`kiro_crew.mcp_discovery` stays the import path and the patch surface for
every name here; that module's comment lists the owners.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.env import MCP_PATH_HINT, describe_search_path
from kiro_crew.mcp_discovery_server_info import McpServerInfo, redact_mcp_error
from kiro_crew.mcp_gateway.hashing import hash_command, hash_effective_env
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kiro_crew.mcp_discovery")


# The newest MCP revision our client handshakes (HTTP and stdio probes, the gateway
# backend) offer in ``initialize``.
MCP_CLIENT_PROTOCOL_VERSION = "2025-06-18"
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def downgrade_protocol_version(reply: object, sent: str) -> str | None:
    """Newest ``data.supported`` version of a -32602 refusal of our first offer, else None."""
    error = reply.get("error") if isinstance(reply, dict) else None
    if sent != MCP_CLIENT_PROTOCOL_VERSION or not isinstance(error, dict):
        return None
    data = error.get("data") if error.get("code") == -32602 else None
    supported = data.get("supported") if isinstance(data, dict) else None
    if not isinstance(supported, list):
        return None
    # Revisions are ISO dates: only an older one is a version this client can speak.
    older = (v for v in supported if isinstance(v, str) and _ISO_DATE.fullmatch(v) and v < sent)
    return max(older, default=None)


def negotiated_protocol_version(reply: object, sent: str) -> str:
    """The version the server answered ``initialize`` with, else the one we sent."""
    result = reply.get("result") if isinstance(reply, dict) else None
    version = result.get("protocolVersion") if isinstance(result, dict) else None
    return version if isinstance(version, str) and version else sent


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
# life of the process), and `probe_all` prunes keys not present in the
# config (so editing a command string does not retain the old one forever).
_unresolvable_warned: set[tuple[str, str]] = set()


def _unresolved_error(command: str, search_path: str = "") -> str:
    """The dashboard-facing string for a command that resolved nowhere.

    Names the count of directories searched, because ``command not found`` alone
    does not distinguish the two causes a reader can act on: the binary is not
    installed, or it is installed somewhere the search path does not cover. The
    full directory list goes to the log (:func:`_warn_unresolvable_once`) rather
    than here -- this string renders in a fixed-width dashboard cell.
    """
    if not search_path:
        return f"command not found: {command}"
    count = len([d for d in search_path.split(os.pathsep) if d])
    return (
        f"command not found: {command} — not in any of the {count} directories "
        "searched (see the gateway log for the list)"
    )


def _warn_unresolvable_once(name: str, command: str, search_path: str = "") -> None:
    """WARNING on first sight of an unresolvable command, DEBUG thereafter.

    *search_path* is the PATH actually searched; naming its directories is what
    lets a reader tell "this install location is not covered" from "this binary
    does not exist" without reading the source.
    """
    key = (name, command)
    searched = f" ({describe_search_path(search_path)})" if search_path else ""
    if key in _unresolvable_warned:
        logger.debug(
            "MCP probe [%s]: command still not found: %s (already reported)", name, command
        )
        return
    _unresolvable_warned.add(key)
    logger.warning(
        "MCP probe failed [%s]: command not found: %s%s; %s",
        name,
        command,
        searched,
        MCP_PATH_HINT,
    )


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
    """Drop ledger keys that the current config does not name.

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
    # MCP's March 2025 revision or later can send these, so an empty list is "not
    # available", never "declared nothing".
    tool_annotations: list[dict[str, Any]] = field(default_factory=list)
    # Wall-clock companion to the monotonic ``probed_at``: monotonic drives the
    # TTL (immune to clock changes), wall-clock is what the API reports so the
    # UI can render "as of <time>". Two clocks, one write, no drift.
    probed_at_wall: float = 0.0
    probe_mode: str = "handshake"
    # Authorization evidence from the probe response. Cached alongside the status
    # because the panel is served from this cache for the whole TTL — a badge that
    # only distinguished sign-in state on the one uncached read would spend almost
    # all of its life showing the vaguer wording.
    auth_challenge: bool = False
    auth_grant_present: bool | None = None
    # The probe's declared-temp refusals (see ``McpServerInfo.temp_refusals``),
    # cached so the panel keeps showing them for the whole TTL.
    temp_refusals: list[dict[str, str]] = field(default_factory=list)
    # Fingerprint of the config inputs this answer was probed UNDER, so an entry
    # cannot outlive the configuration that produced it. The cache is keyed on
    # the server NAME alone, and a name is not an identity: editing a command,
    # an argument or a non-secret env value leaves the name untouched, and two
    # install forms of one logical server can canonicalize to the same name
    # while pointing at different targets. See :func:`_probe_identity`.
    identity: str = ""


# Module-level probe cache: server name → result
_probe_cache: dict[str, _ProbeResult] = {}


def _probe_identity(server: McpServerInfo) -> str:
    """Fingerprint the config inputs a probe's ANSWER depends on.

    Local servers hash command, args and non-secret env through the same
    helpers that build the corresponding ``PoolKey`` dimensions, so a config
    edit the pool would route to a different backend also invalidates the
    entry here. Remote servers hash the url and the header NAMES: a changed
    header VALUE is a credential rotation against the same endpoint, not a
    different server, and ``auth_challenge`` is already re-derived on every
    remote probe. The url is hashed rather than embedded because a url can
    carry a credential in its userinfo or query string (see
    ``redact_exfiltration_urls``), and an identity string travels with the
    cached entry.

    Local and remote are decided by ``McpServerInfo.is_remote`` — the same
    property ``probe_server`` dispatches on. A spec carrying BOTH a url and a
    command is probed as local, so fingerprinting it as remote would leave an
    edit to its command invisible to this cache: the original bug, surviving
    for exactly the shape that looks most like a misconfiguration.

    Every input is coerced before hashing. ``_server_from_spec`` passes
    on-disk JSON through unvalidated, so ``env`` can be null, ``args`` can hold
    non-strings, and ``command`` can be any type; and ``probe_all`` is required
    to fail one malformed server in isolation (see its own docstring). Raising
    here would instead take out ``list_servers()`` and with it every other
    server's probe, turning one bad row into a dead endpoint.

    ``args`` is hashed as the probe will SPAWN it, not as a well-formed spec
    would hold it. ``probe_server`` builds its argv as
    ``[resolved, *(server.args or [])]``, so a string splats into one argument
    per character and a dict into its keys; ``mcp_gateway.evaluate.identity_for``
    takes ``list(server.args or [])``, which splats the same way. Hashing any non-list
    as empty would make every edit to such a value invisible here, which is the
    bug this function exists to close. A non-iterable value is hashed by
    ``repr``, so editing it still changes the identity; the probe's own splat
    raises on it.

    Every string is passed through :func:`_hashable_text` first, because JSON
    permits an unpaired ``\\uD800`` escape and ``str.encode("utf-8")`` raises on
    the lone surrogate ``json.loads`` produces from it. ``str()`` coerces the
    type, not the code points.

    Secret env values are excluded by ``hash_effective_env`` rather than by a
    filter here, so the hashed set is the forwardable set by construction. Only
    ``ENV_SCRUB_PREFIXES`` keys are excluded, so rotating a credential held in
    an unprefixed key does invalidate the entry. That is the trade
    ``PoolKey.effective_env_hash`` already makes.

    It deliberately does NOT pass ``identity_keys``, so a rotating-secret key
    an operator named in ``mcp_gateway.pool_identity_env`` stays out of THIS
    hash. The pool needs that value because the value decides which backend is
    the right backend; a tool list is a claim about what the endpoint
    advertises, and rotating a credential does not change that. So the two
    hashes agree on the default set and diverge only on named keys, on purpose
    — the same split ``mcp_gateway.evaluate.identity_for`` makes, for the same
    reason. The cost of the divergence is bounded to those named keys: a
    changed value re-partitions the pool without invalidating this entry, so
    the tool list served is the one the other value's backend advertised.

    ceiling: identity covers command, args, url, header names and non-secret
    env, NOT the target binary's bytes. An in-place binary upgrade therefore
    does not invalidate a probe entry; the pool's own ``binary_version``
    dimension covers that case for execution. Fold ``binary_fingerprint`` in
    here only off the ``GET /api/mcp`` read path — it content-hashes the
    binary, and this function runs once per server per request.
    """
    if server.is_remote:
        headers = server.headers if isinstance(server.headers, dict) else {}
        url = _hashable_text(server.url or "")
        return f"remote:{hash_command(url, sorted(_hashable_text(k) for k in headers))}"
    raw_args = server.args or []
    try:
        args = [_hashable_text(a) for a in raw_args]
    except TypeError:
        args = [_hashable_text(repr(raw_args))]
    env = server.env if isinstance(server.env, dict) else {}
    command_hash = hash_command(_hashable_text(server.command or ""), args)
    env_hash = hash_effective_env({_hashable_text(k): _hashable_text(v) for k, v in env.items()})
    return f"local:{command_hash}\0{env_hash}"


def _hashable_text(value: object) -> str:
    """*value* as a string that always encodes to UTF-8.

    A lone surrogate becomes its ``\\udXXX`` escape text, so two values that
    differ only in which surrogate they hold still hash differently. The one
    collision is with a config that spells that escape out as literal text,
    and conflating those two costs one extra probe at most.
    """
    return str(value).encode("utf-8", "backslashreplace").decode("utf-8")


def cached_probe_is_current(server: McpServerInfo) -> bool:
    """True if the cached probe entry for *server* was probed under its config.

    For callers that keep their OWN longer-lived copy of a probe result and
    fall back to it when this module reports ``outdated`` or ``unknown``. Their
    copy is keyed on the server name too, so without this check gating
    ``_get_cached`` accomplishes nothing: the identity mismatch is reported,
    and then the caller's own name-keyed copy puts the previous target's tools
    straight back onto the row.

    No cached entry returns True — there is no claim to contradict, and a
    caller with a result this module never saw (a probe whose own cache write
    was skipped) keeps its previous behaviour rather than losing the row.
    """
    cached = _probe_cache.get(server.name)
    if cached is None:
        return True
    return cached.identity == _probe_identity(server)


def _get_cached(
    name: str, expected_identity: str | None = None
) -> tuple[str, list[str], str, float, str]:
    """Return (status, tools, error, probed_at_wall, probe_mode) from cache.

    If within TTL: returns original status + tools.
    If expired: returns "outdated" + tools (tools always preserved).
    If not cached: returns ("unknown", [], "", 0.0, "handshake").

    The wall-clock timestamp and probe mode are returned even for an expired
    entry — "outdated" is exactly the state where WHEN it was last true is the
    most useful thing the UI can say.

    An *expected_identity* that does not match the entry's is treated as NOT
    CACHED rather than as expired, because the two states differ in what the
    tool list is evidence OF. An expired entry was true of this server and has
    merely aged, which is why "outdated" still carries its tools. A
    fingerprint mismatch means the entry describes a target the current config
    does not point at, so its tools are not stale evidence about this server
    — they are evidence about a different one, and reporting them would attach
    a real tool list to a server that was never asked. A caller holding only a
    name passes nothing, which skips the identity check.
    """
    cached = _probe_cache.get(name)
    if cached is None:
        return "unknown", [], "", 0.0, "handshake"
    if expected_identity is not None and cached.identity != expected_identity:
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

    A failed probe (``server.status in {"error", "needs_auth"}``) still
    overwrites ``status``/``error`` — a server that just failed to answer
    IS currently failing, and ``mcp_gateway.shareability`` correctly reads a
    fresh "error" as UNKNOWN via ``probe_ok``. What it must NOT overwrite is
    the server's descriptive shape from its last successful handshake:
    ``tools``, ``tool_annotations``, ``capabilities``, ``protocol_version``,
    ``server_info``, and ``probed_at_wall``. Without this, a single transient
    timeout on an otherwise-healthy server collapses its tool count to zero
    for the probe that failed, discarding both lists together even though
    each is only ever populated from that same successful handshake —
    resetting one without the other would leave a caller reading tool
    metadata that does not match the tools actually being reported. ``probed_at_wall`` travels with the preserved shape rather
    than the failed probe's own timestamp, so an "as of" display next to
    the preserved tools points to when they were actually observed, not to
    the unrelated timeout that came later. A server that has never had a
    successful probe has no prior shape to fall back to, so it gets the
    failure's own (empty) shape — there is nothing stale to protect. A prior
    entry probed under a DIFFERENT configuration is not a prior shape for this
    server either: its tools describe whatever the old command or url pointed
    at, so it is dropped rather than preserved. See :func:`_probe_identity`.
    """
    server.probed_at = time.time()
    identity = _probe_identity(server)
    prior = _probe_cache.get(server.name)
    if prior is not None and prior.identity != identity:
        prior = None
    probe_failed = server.status in ("error", "needs_auth")
    if probe_failed and prior is not None:
        tools = list(prior.tools)
        tool_annotations = [dict(a) for a in prior.tool_annotations]
        capabilities = dict(prior.capabilities) if isinstance(prior.capabilities, dict) else None
        protocol_version = prior.protocol_version
        server_info = dict(prior.server_info)
        probed_at_wall = prior.probed_at_wall
    else:
        tools = list(server.tools)
        tool_annotations = [dict(a) for a in server.tool_annotations]
        capabilities = dict(server.capabilities) if isinstance(server.capabilities, dict) else None
        protocol_version = server.protocol_version
        server_info = dict(server.server_info)
        probed_at_wall = server.probed_at
    _probe_cache[server.name] = _ProbeResult(
        status=server.status,
        tools=tools,
        error=redact_mcp_error(
            server.error, server.redaction_headers, server.resolved_header_values or ()
        ),
        probed_at=time.monotonic(),
        capabilities=capabilities,
        protocol_version=protocol_version,
        server_info=server_info,
        tool_annotations=tool_annotations,
        probed_at_wall=probed_at_wall,
        probe_mode=server.probe_mode,
        auth_challenge=server.auth_challenge,
        auth_grant_present=server.auth_grant_present,
        temp_refusals=[dict(r) for r in server.temp_refusals],
        identity=identity,
    )
