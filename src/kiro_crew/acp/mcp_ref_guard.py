"""The ACP layer's reporting half of the unresolved-``@server``-ref detector.

The question itself -- which of a spec's ``@server`` refs name nothing a session
receives -- is provider-neutral plain data and lives in
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
from typing import Any

from kiro_crew.acp.mcp_session_report import NAME_CAP, sanitize_sink_text
from kiro_crew.agent_sdk.mcp_refs import unresolved_server_refs

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
    logger.warning(
        "agent-spec tool refs name no MCP server this session receives: "
        "backend=%r agent=%r unresolved=%s mcp_gateway=%s. Those tools are absent "
        "from the session with nothing else to say so -- the harness still works. "
        "A backend that reads no agent file needs a mirror "
        "(src/kiro_crew/providers/mirrors/) to project the spec onto its "
        "session/new mcpServers array.",
        backend,
        safe_agent,
        listed,
        "on" if gateway_enabled else "off",
    )
    return unresolved
