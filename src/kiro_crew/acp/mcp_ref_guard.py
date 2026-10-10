"""The ACP layer's reporting half of the unresolved-``@server``-ref detector.

The question itself -- which of a spec's ``@server`` refs name nothing in Crew's
projection for the session -- is provider-neutral plain data and lives in
:mod:`kiro_crew.agent_sdk.mcp_refs`, which is what lets ``kirocrew doctor`` ask it
without importing this layer. What lives HERE is the part that is genuinely ACP's:
turning that answer into one structured log line at the point where a session's
wire array becomes final.

Kept as its own module rather than inlined at the call site so ``session/new`` and
the ``session/load`` that resumes the same session cannot drift into wording the
finding differently.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kiro_crew.acp.mcp_session_report import NAME_CAP, sanitize_sink_text
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.mcp_refs import unresolved_server_refs
from kiro_crew.mcp_gateway.secret_uri import (
    REMOTE_HEADER_ALTERNATIVE,
    display_header_names,
    header_secret_refs,
)

logger = logging.getLogger(__name__)

#: Cap on how many refs one warning names. A spec is hand-editable and its
#: ``tools`` list is unbounded; a log line is not. The true count still rides
#: along, so a truncated line never understates the problem.
_REPORT_CAP = 32

#: The characters a ref or an agent name may contribute to the LOG line, plus the
#: two brackets a redaction tag needs to stay readable. Deliberately WITHOUT ``:``
#: and ``/``: a URL needs both, so leaving them out means a ref cannot smuggle an
#: endpoint into the record even if it somehow slipped the redactors.
_LOG_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@._-[] "

#: Longest single ref or agent name the log line spells out. A server name is an
#: identifier and short by convention; the cap stops one pathological name from
#: dominating the record.
_LOG_TOKEN_MAX = 96


def _log_safe(text: str) -> str:
    """A ref or agent name rebuilt from :data:`_LOG_ALPHABET`, for the log line only.

    **Two jobs, and the second is why a redactor alone was not enough.** The first
    is hardening: dropping every character outside the alphabet means the record
    cannot carry URL punctuation whatever the spec put in the name.

    The second is that the result must not be a string DERIVED from the input.
    ``py/clear-text-logging-sensitive-data`` follows the spec-derived dataflow into
    this warning's sink, and it does not model
    :func:`~kiro_crew.acp.mcp_session_report.sanitize_sink_text` as a barrier -- so
    the query reports at high severity even though the redaction is right there.
    Code scanning does not honour a per-line ``lgtm`` suppression, so the barrier
    has to be one the analysis can see, and this repository's own answer to exactly
    this problem is to return characters the module itself owns: see ``_locus``
    and ``_metric_slug``
    in ``apps/builtins/auto_improvement/spine/keeper.py`` ("append the ALPHABET's
    own character object, not the input's"), and the constant tables in
    ``name_grant``. That severs the flow in a way the analysis can verify, where a
    check-then-pass-through cannot.

    Runs AFTER the redactors, never instead of them: a credential-shaped name can
    be pure alphanumerics (``AKIAIOSFODNN7EXAMPLE``), which this alphabet would
    pass through untouched. Redaction is what removes the secret; this is what
    removes the punctuation and the dataflow.

    Two refs differing only in dropped characters log identically. Accepted: the
    log line is a diagnostic pointer, and the session's MCP report carries the
    sanitized names for a reader who needs to tell two apart.
    """
    out: list[str] = []
    for ch in text[: _LOG_TOKEN_MAX * 2]:
        idx = _LOG_ALPHABET.find(ch)
        if idx >= 0:
            # The ALPHABET's own character object, not the input's.
            out.append(_LOG_ALPHABET[idx])
        if len(out) >= _LOG_TOKEN_MAX:
            break
    return "".join(out) or "?"


def warn_unresolved_server_refs(
    spec: Any,
    wire_servers: Any,
    *,
    backend: str,
    agent: str,
    gateway_enabled: bool,
) -> list[str]:
    """Evaluate the detector and log ONE structured line when it finds something.

    Returns the refs -- SANITIZED -- so a caller can also record them where a user
    will see them.

    **A ref is untrusted text, and this is the boundary where it becomes a sink
    payload.** It is the substring of a ``tools`` entry after ``@``, so its content
    is whatever an operator, a cloned repository's ``<project>/.kiro/agents/*.json``
    or an installed app's registered spec put there -- and this warning fires in
    NORMAL operation (codex today), not in some contrived case. So every ref is run
    through the package's one sanitizer before it reaches any sink: credentials and
    exfiltration URLs are redacted, control characters are dropped so a ref cannot
    forge a second log line, and the length is bounded so one pathological name
    cannot dominate the record. Sanitizing here rather than at each sink is what
    makes the RETURN value safe as well, so the next consumer to log it inherits
    the redaction instead of re-opening the hole. The LOG line then adds
    :func:`_log_safe` on top -- a rebuild from this module's own alphabet, which
    both drops URL punctuation and severs the dataflow a taint query follows.

    ``gateway_enabled`` rides along because it decides the REMEDY rather than the
    finding: with the shared MCP gateway on, a wrapped server arrives as a broker
    stub and routing it may be the fix, while with the gateway off the only
    channel is the projection. Reading the line without knowing which world it
    came from is what made this defect take three diagnoses.
    """
    raw = unresolved_server_refs(spec, wire_servers, backend=backend)
    if not raw:
        return []
    unresolved = [safe for safe in (sanitize_sink_text(ref, NAME_CAP) for ref in raw) if safe]
    if not unresolved:
        return []
    shown = unresolved[:_REPORT_CAP]
    # Redacted already (above); rebuilt here so the log line carries characters this
    # module owns rather than a string derived from the spec. See :func:`_log_safe`.
    listed = ", ".join(_log_safe(ref) for ref in shown)
    if len(shown) < len(unresolved):
        listed += f" (+{len(unresolved) - len(shown)} more)"
    safe_agent = _log_safe(sanitize_sink_text(agent, NAME_CAP))
    # The closing sentence claims only what the wire proves, on EVERY backend.
    # "Absent" would be the strong claim, and no backend's array is provably the
    # session's whole MCP surface: Claude Code mounts its own user- and
    # project-scope ``mcpServers`` and plugins beside the array, codex-acp merges
    # the array on top of ``~/.codex/config.toml``, and kiro-cli loads the global
    # ``~/.kiro/settings/mcp.json`` into every agent by default (a spec may opt out
    # of the global file with ``includeMcpJson: false``). So a same-named server
    # may be serving a listed ref on any of them, and the detector, which judges
    # the wire (or the spec) alone by design, cannot tell which. The line says what
    # it knows -- Crew's projection delivers none of them -- and stops short of
    # what it does not.
    logger.warning(
        "agent-spec tool refs name no MCP server in Crew's projection for this "
        "session: backend=%r agent=%r unresolved=%s mcp_gateway=%s. Crew's projection "
        "delivers none of those servers; the harness may mount a same-named server "
        "from its own configuration, so a listed ref may still be served and this "
        "line cannot tell which. The harness still works. "
        "A backend that reads no agent file needs a mirror "
        "(src/kiro_crew/providers/mirrors/) to project the spec onto its "
        "session/new mcpServers array.",
        backend,
        safe_agent,
        listed,
        "on" if gateway_enabled else "off",
    )
    return unresolved


def _remote_entries(spec: Any, wire_servers: Any, *, backend: str) -> list[tuple[str, Any]]:
    """``(server name, headers)`` for every remote server this session is handed.

    kiro-cli reads the spec's own servers from ``--agent``, headers included, so
    it is judged on the spec's definitions; every other host only sees what the
    array carries. KAS also mounts spec servers off the wire, but its projection
    drops every declared header, so a reference there is never sent and is not
    reported.
    """
    out: list[tuple[str, Any]] = []
    if backend == ACP_BACKEND_KIRO:
        servers = spec.get("mcpServers") if isinstance(spec, Mapping) else None
        if isinstance(servers, Mapping):
            for name, entry in servers.items():
                if isinstance(entry, Mapping) and entry.get("url"):
                    out.append((str(name), entry.get("headers")))
    for entry in wire_servers if isinstance(wire_servers, (list, tuple)) else ():
        if isinstance(entry, Mapping) and entry.get("url") and isinstance(entry.get("name"), str):
            out.append((entry["name"], entry.get("headers")))
    return out


def warn_remote_header_secret_refs(
    spec: Any,
    wire_servers: Any,
    *,
    backend: str,
    agent: str,
) -> tuple[list[str], int]:
    """Log ONE line naming remote servers whose headers carry ``secret://``.

    Only an MCP server's ``env`` is resolved against the vault; a remote server's
    headers reach it as written. Without this line the only symptom is an
    authorization failure from the server that names neither the header nor the
    cause. Changes nothing about the session.

    Returns ``(entries, omitted)``: one sanitized ``server (header, ...)`` entry
    per affected server, at most :data:`_REPORT_CAP` of them, with the header
    list bounded by :func:`display_header_names`; and how many affected servers
    did not fit. The caller records both on the session's MCP report.
    """
    found: list[str] = []
    logged: list[str] = []
    seen: set[str] = set()
    omitted = 0
    for name, headers in _remote_entries(spec, wire_servers, backend=backend):
        server = sanitize_sink_text(name, NAME_CAP)
        cleaned = (sanitize_sink_text(h, NAME_CAP) for h in header_secret_refs(headers))
        names = [h for h in cleaned if h]
        if not names or not server or server in seen:
            continue
        seen.add(server)
        if len(found) >= _REPORT_CAP:
            omitted += 1
            continue
        found.append(f"{server} ({display_header_names(names)})")
        logged.append(
            f"{_log_safe(server)} ({display_header_names([_log_safe(h) for h in names])})"
        )
    if not found:
        return [], 0
    servers = "; ".join(logged) + (f" (+{omitted} more servers)" if omitted else "")
    logger.warning(
        "remote MCP server headers use a secret:// reference, which is not resolved: "
        "backend=%r agent=%r servers=%s. Secret references are resolved only in a "
        "stdio server's env, so these servers receive the reference as written. %s.",
        backend,
        _log_safe(sanitize_sink_text(agent, NAME_CAP)),
        servers,
        REMOTE_HEADER_ALTERNATIVE,
    )
    return found, omitted
