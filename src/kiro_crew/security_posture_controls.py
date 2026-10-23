"""The security-posture controls: what each one counts, how its items are resolved, and the snapshot.

``_CONTROLS`` lists every control the Settings -> Security panel shows. Each resolves its
items per request from the live object it describes, or from a curated registry
(``_REDACTION_SINKS``, joined here from the two sink modules in registry order,
``_CREDENTIAL_FAMILIES`` and ``_EXFIL_HEURISTICS``) that an inverse test holds against the
code. ``build_posture_snapshot`` and ``posture_counts`` serve the panel and the stats route.

Part of :mod:`kiro_crew.security_posture`, the import path callers, tests and docs use: it
reaches every name here, and a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`). Logs under the ``kiro_crew.security_posture`` logger.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

import kiro_crew.validation as _validation
from kiro_crew import security
from kiro_crew import sel as _sel_mod
from kiro_crew.executors import governance_executor
from kiro_crew.security_posture_sinks_delivery import _DELIVERY_SINKS
from kiro_crew.security_posture_sinks_gateway import _GATEWAY_SINKS

logger = logging.getLogger("kiro_crew.security_posture")


@dataclass(frozen=True)
class PostureItem:
    """One concrete element behind a control's count.

    ``label`` is what the control covers (a path, a sink, a credential family);
    ``detail`` is the optional "how/where" shown as secondary text.
    """

    label: str
    detail: str = ""


@dataclass(frozen=True)
class PostureControl:
    """One expandable security control.

    ``items_fn`` is a callable (not a list) so the items are resolved at request
    time against the LIVE control — a governance profile reload or a user-added
    deny rule is reflected without a gateway restart.

    Deliberately has NO ``items`` field: an eagerly-captured list is the exact
    shape of the drift bug this module exists to prevent (a snapshot taken at
    import time and then quietly outliving the thing it described), and a mutable
    default would also make this "frozen" dataclass unhashable.
    """

    key: str
    label: str
    unit: str
    items_fn: Callable[[], list[PostureItem]]
    summary: str = ""
    source: str = ""


# ── Redaction egress paths ──
# The distinct boundaries where agent/tool output crosses to a human or an
# external service and therefore runs a redaction pass. Named here (rather than
# counted straight from grep) so the count means "egress paths covered" instead
# of "call sites that mention redact" — but see NON_EGRESS_REDACTION_MODULES
# in security_posture.py: a drift guard checks this list against EVERY redactor call site, so an
# egress path cannot be silently omitted. Each entry names the owning module.
# Where a sink runs only ONE of the two scanners, its detail text says so.
_REDACTION_SINKS: tuple[tuple[str, str, str], ...] = (*_GATEWAY_SINKS, *_DELIVERY_SINKS)


# ── Credential families ──
# The credential CLASSES the redaction regex matches. Family names only — never a
# pattern that could be inverted into a generator, and never live secrets.
_CREDENTIAL_FAMILIES: tuple[tuple[str, str], ...] = (
    (
        "AWS access keys",
        "AKIA / ASIA key IDs, plus labelled secret-access-key and session-token forms",
    ),
    (
        "Private keys",
        "PEM blocks (RSA / DSA / EC / OPENSSH), including encrypted and truncated bodies",
    ),
    ("Slack tokens", "xoxb / xoxp / xoxa / xoxs bot, user, and app tokens"),
    (
        "GitHub tokens",
        "ghp / gho / ghu / ghs / ghr classic tokens and github_pat fine-grained tokens",
    ),
    ("GitLab tokens", "glpat personal access tokens"),
    ("Stripe keys", "sk_live / rk_live / sk_test / rk_test secret and restricted keys"),
    ("SendGrid keys", "SG. prefixed API keys"),
    ("OpenAI keys", "sk-proj project keys"),
    ("Anthropic keys", "sk-ant API keys"),
    ("npm tokens", "npm_ access tokens"),
    ("PyPI tokens", "pypi- API tokens"),
    ("DigitalOcean tokens", "dop / doo / dor v1 personal, OAuth, and refresh tokens"),
    ("Google OAuth secrets", "GOCSPX- client secrets"),
    ("Telegram bot tokens", "numeric bot id joined to a URL-safe secret"),
    ("JWT / JWE tokens", "3-segment signed and 5-segment encrypted compact tokens"),
    # Phrased WITHOUT a literal "Authorization: Bearer <token>" sequence: that
    # exact shape is what the scanner matches, so spelling it out here would make
    # this very description self-redacting wherever the payload is itself scanned
    # (the SEL audit log, a Slack-relayed posture summary) — the row would render
    # as "[REDACTED: credential]". A pinned test asserts the whole payload survives
    # redact_credentials() unchanged; keep new descriptions clear of live shapes.
    ("HTTP bearer tokens", "Bearer-scheme HTTP auth headers, including JSON-serialized log dumps"),
    (
        "Database URIs",
        "postgres / mysql / mongodb / redis / amqp connection strings with an embedded password",
    ),
    (
        "Base64-encoded variants",
        "40+ char base64 chunks are decoded and re-scanned against every family above",
    ),
)


# ── URL exfiltration heuristics ──
_EXFIL_HEURISTICS: tuple[tuple[str, str], ...] = (
    (
        "Credential in path or query",
        "Unconditional floor — an AWS key, PEM header, or Slack token anywhere in the "
        "URL is exfiltration regardless of destination.",
    ),
    (
        "Base64-encoded credential",
        "Base64 chunks in the query are decoded and re-scanned, so an encoded secret "
        "cannot slip past the literal-marker floor.",
    ),
    (
        f"Long query string (>= {security.exfil_query_min_len()} chars)",
        "A query large enough to carry a stolen payload.",
    ),
    (
        "Credential-like query data",
        "Base64 blobs and key-shaped values in query parameters.",
    ),
    (
        "Heavy percent-encoding",
        "20+ consecutive percent-encoded characters — an obfuscated payload. Applies "
        "to every host, including exempted ones.",
    ),
)


def _own_namespace_prefixes() -> tuple[str, ...]:
    """Home-relative prefixes that belong to Kiro Crew / kiro-cli itself.

    Derived from the crew data-home prefixes, plus the ``.kiro`` parent that
    holds kiro-cli's own state and the gateway's auth staging dir. Used only to
    label a row in the posture view — a misclassification is cosmetic, never a
    gate decision.
    """
    return tuple({p.split("/", 1)[0] for p in security.crew_home_prefixes()})


def _sensitive_path_items() -> list[PostureItem]:
    """Credential paths blocked at the hook layer, classified by owner."""
    items: list[PostureItem] = []
    own = _own_namespace_prefixes()
    for entry in security.sensitive_home_dirs():
        # Path-boundary match, so a sibling like `.kirocrew-notes` is not counted
        # as ours just because it shares a string prefix.
        first = entry.split("/", 1)[0]
        if first in own:
            detail = (
                "Kiro Crew trust root — agent file tools can neither read nor write it; "
                "shell reach follows its sandbox disposition"
            )
        else:
            detail = "Third-party credential store"
        items.append(PostureItem(label=f"~/{entry}", detail=detail))
    return items


def _write_protected_items() -> list[PostureItem]:
    """Paths readable but not writable by agent tools."""
    return [
        PostureItem(
            label=f"~/{entry}",
            detail=(
                "Reads allowed; the agent's file-edit tool cannot modify it when the "
                "call declares the ACP edit kind. Shell writes are not covered by "
                "this control"
            ),
        )
        for entry in security.write_protected_home_paths()
    ]


def _denied_command_items() -> list[PostureItem]:
    """Built-in deny rules, described (not raw-regex) and grouped by category."""
    return [
        PostureItem(label=rule.description, detail=rule.category)
        for rule in security.BUILTIN_DENIED_RULES
    ]


def _suspicious_pattern_items() -> list[PostureItem]:
    return [PostureItem(label=pattern) for pattern in security.SUSPICIOUS_BASH_PATTERNS]


#: Every MCP tool-schema dispatch registry in ``validation``, by attribute name. A
#: tool is only validated if it is IN one of these, so this list defines what the
#: posture view can see. Keep it complete — ``test_security_posture`` reads the same
#: names, so an omission fails there rather than quietly shrinking the report.
_SCHEMA_REGISTRY_NAMES: tuple[str, ...] = (
    "MCP_CORE_SCHEMAS",
    "MCP_CREW_LOG_SCHEMAS",
    "MCP_CRON_SCHEMAS",
    "MCP_COMPUTER_SCHEMAS",
    "MCP_DASHBOARD_SCHEMAS",
    "MCP_DEBUG_SCHEMAS",
    "MCP_WORK_SCHEMAS",
    "MCP_PANEL_SCHEMAS",
    "MCP_GUIDE_SCHEMAS",
    "MCP_MATE_SCHEMAS",
)


def _tool_schema_items() -> list[PostureItem]:
    """Validated tool schemas, keyed by the tool name they gate.

    Derived from EVERY dispatch registry in ``validation`` — the dicts
    ``mcp_core``/``mcp_cron``/``computer_use.tools`` actually look a tool up in —
    plus the module-level ``*_SCHEMA`` objects that gate dashboard handlers rather
    than MCP tools.

    Deriving from the registries (not the ``*_SCHEMA`` naming convention) makes
    the registry, not the convention, the source of truth. Several registered MCP
    tools (e.g. ``cron_trigger``) are defined as inline or shared ``ToolSchema``
    objects with no module-level name of their own, so a convention-only walk
    validates them but cannot see them here — a tool registered that way would add
    zero to the count.

    ``_SCHEMA_REGISTRY_NAMES`` is enumerated rather than discovered, but the drift
    test derives its expectation from the same module attributes, so a NEW registry
    that is not listed here fails that test instead of silently under-reporting.
    """
    seen: dict[str, str] = {}
    for registry_name in _SCHEMA_REGISTRY_NAMES:
        registry = getattr(_validation, registry_name, None) or {}
        for tool_name in registry:
            seen.setdefault(tool_name, registry_name)
    for name in dir(_validation):
        if not (name.endswith("_SCHEMA") and name.isupper()):
            continue
        tool_name = getattr(getattr(_validation, name, None), "tool_name", "") or ""
        # A registry entry wins the attribution: it names where the schema is
        # actually enforced. Nameless/helper constants are skipped so the count
        # and the list always agree.
        if tool_name:
            seen.setdefault(tool_name, name)
    return [PostureItem(label=tool, detail=source) for tool, source in sorted(seen.items())]


def _redaction_sink_items() -> list[PostureItem]:
    return [
        PostureItem(label=label, detail=f"{module} — {detail}")
        for label, module, detail in _REDACTION_SINKS
    ]


def _credential_family_items() -> list[PostureItem]:
    return [PostureItem(label=label, detail=detail) for label, detail in _CREDENTIAL_FAMILIES]


def _exfil_heuristic_items() -> list[PostureItem]:
    return [PostureItem(label=label, detail=detail) for label, detail in _EXFIL_HEURISTICS]


# Human-readable gloss per SEL ``source`` value. The KEYS are not authoritative —
# ``sel._infer_source`` is — so a source added there without a gloss here still
# gets a row (labelled by its raw token) rather than being silently dropped, and a
# pinned test fails so the gloss gets written.
_AUDIT_SURFACE_DETAIL: dict[str, str] = {
    "slack": "Messages, approvals, and owner-authorization decisions",
    "dashboard": "Tool invocations, permission decisions, and authenticated API access",
    "cron": "Scheduled job fires and their tool calls",
    "subagent": "Spawned agent lifecycle, tool calls, and results",
    "taskrunner": "Autonomous multi-step task execution",
    "background": "Background maintenance work",
    "heartbeat": "Liveness / watchdog activity",
    "cli": "Terminal chat sessions",
    "discord": "Discord messages, approvals, and owner-authorization decisions",
    "telegram": "Telegram messages, approvals, and owner-authorization decisions",
    "wecom": "WeCom messages, approvals, and owner-authorization decisions",
    "weixin": "Weixin messages, approvals, and owner-authorization decisions",
    "whatsapp": "WhatsApp messages, approvals, and owner-authorization decisions",
    "feishu": "Feishu messages, approvals, and owner-authorization decisions",
    "webex": "Webex messages, approvals, and owner-authorization decisions",
    "teams": "Microsoft Teams messages, approvals, and owner-authorization decisions",
    "imessage": "iMessage messages, approvals, and owner-authorization decisions",
    "host": "In-process governance checks not driven by a user-facing surface",
    "unknown": "Events that carry no surface signal (classified rather than misattributed)",
}


def _audit_surface_items() -> list[PostureItem]:
    """Audited surfaces, DERIVED from ``sel._infer_source``'s vocabulary.

    ``_infer_source`` maps a session key to a surface, so its return vocabulary is
    the set of surfaces SEL can infer — deriving from it means adding one moves
    this count automatically.

    SCOPE (deliberate): a caller may pass an explicit ``source=`` that bypasses
    inference entirely (``channel``, ``token_auth``, ``migration``, …; ~70 such
    literals exist, many of which are event *kinds* rather than surfaces). Those
    are NOT enumerable as a clean "audited surfaces" list, so this control is
    scoped to the inferred vocabulary and its unit/summary say so — a floor, not a
    total. Claiming otherwise would be the very overstatement this module exists
    to remove.
    """
    return [
        PostureItem(
            label=source.replace("_", " ").capitalize(),
            detail=_AUDIT_SURFACE_DETAIL.get(source, "Audited SEL event source"),
        )
        for source in sorted(_sel_mod.audit_sources())
    ]


def _token_auth_items() -> list[PostureItem]:
    # circular import: token_auth lives under kiro_crew.dashboard, whose package
    # __init__ imports the server (which imports this module via handlers.core).
    # A top-level import here would close that cycle, so the two TTL constants are
    # read at call time — the documented circular-import exception, and it keeps
    # the advertised windows derived from the enforcing module rather than
    # restated as literals that could drift.
    from kiro_crew.dashboard.token_auth import (
        LINK_WINDOW_SECS,
        MAX_SESSION_TTL_SECS,
        proxied_pin_observed,
    )

    # Tri-state, deliberately, and derived from the LIVE bindings so it recovers
    # on its own. A pin that has collapsed onto a same-host proxy's loopback
    # address is NOT the control this row advertises, and "nothing is
    # pinned right now" is not evidence that pins are effective — rendering
    # either as the plain claim is the failure this row exists to avoid.
    _pinned = proxied_pin_observed()
    if _pinned is None:
        _pin_detail = (
            "A session is bound to the address that first used it. No session is "
            "currently pinned, so the effective scope is not known yet"
        )
    elif _pinned:
        _pin_detail = (
            "SHARED, not per-client: sessions are binding to a proxy's address rather than a "
            "client's — either a same-host tunnel (cloudflared / ngrok / tailscale serve) or a "
            "reverse proxy in front of this gateway — so every client reaching the dashboard "
            "through it satisfies the same pin. Reach the dashboard directly, or over a "
            "transport that preserves the client address, for the pin to identify one client"
        )
    else:
        _pin_detail = (
            "Per-client: a session is bound to the client address — or the "
            "daemon-verified tailnet identity — that first used it"
        )

    return [
        PostureItem(
            label="HMAC-SHA256 signature",
            detail="Tokens are signed with a persisted secret; a forged or tampered token is rejected",
        ),
        PostureItem(
            label="IP pinning",
            detail=_pin_detail,
        ),
        PostureItem(
            label="Single-use link nonce",
            detail=f"A share link is consumed on first click, within a {LINK_WINDOW_SECS // 60}-minute window",
        ),
        PostureItem(
            label="Bounded session lifetime",
            detail=f"Session cookies expire after at most {MAX_SESSION_TTL_SECS // 3600} hours",
        ),
        PostureItem(
            label="Revocation generation",
            detail="Bumping the generation invalidates every outstanding token at once",
        ),
        PostureItem(
            label="App-token scoping",
            detail="An app token reaches only its own namespace plus its manifest-declared API paths (deny-by-default)",
        ),
    ]


# ── The registry ──
# Order is display order. Each entry's count is len(items_fn()).
_CONTROLS: tuple[PostureControl, ...] = (
    PostureControl(
        key="sensitive_paths",
        label="Sensitive path blocking",
        unit="credential paths",
        summary=(
            "Paths agent file tools cannot read or write. Enforced at the PreToolUse gate "
            "on the resolved target, so a symlink into a blocked directory is refused too. "
            "From a shell, each leaf's sandbox disposition decides: a READONLY leaf stays "
            "readable by design, and a VISIBLE leaf rests on this tool gate alone."
        ),
        source="src/kiro_crew/security/paths.py",
        items_fn=_sensitive_path_items,
    ),
    PostureControl(
        key="write_protected_paths",
        label="Write-protected paths",
        unit="config paths",
        summary=(
            "Readable but not writable by agent tools — config carrying resource ceilings "
            "and the data-home migration marker."
        ),
        source="src/kiro_crew/security/paths.py",
        items_fn=_write_protected_items,
    ),
    PostureControl(
        key="denied_commands",
        label="Denied commands",
        unit="built-in rules",
        summary=(
            "Destructive and credential-exfiltrating shell operations blocked at the "
            "PreToolUse gate. Configurable below; policy-pinned rules cannot be turned "
            "off. The count is the SHIPPED catalogue -- the set actually enforced is "
            "this minus any rule disabled below, so it can be smaller."
        ),
        source="src/kiro_crew/security/denied_rules_catalog.py",
        items_fn=_denied_command_items,
    ),
    PostureControl(
        key="suspicious_patterns",
        label="Suspicious bash patterns",
        unit="patterns",
        summary=(
            "Deletion, exfiltration, and pipe-to-interpreter shapes. Advisory: these "
            "are surfaced by the `kirocrew security audit` history scan, NOT blocked at the "
            "PreToolUse gate — the gate enforces the narrower denied-command rules "
            "and exfiltration checks above."
        ),
        source="src/kiro_crew/security/denied_rules.py",
        items_fn=_suspicious_pattern_items,
    ),
    PostureControl(
        key="tool_schemas",
        label="MCP input validation",
        unit="tool schemas",
        summary=(
            "MCP tool calls with a registered schema are checked for unicode "
            "normalization, length limits, enum allow-lists, and unknown-field "
            "rejection. Coverage is per tool, not universal: computer-use refuses an "
            "unregistered tool outright, while the core, cron and dashboard "
            "dispatchers pass one through unvalidated unless its own handler "
            "validates."
        ),
        source="src/kiro_crew/validation.py",
        items_fn=_tool_schema_items,
    ),
    PostureControl(
        key="redaction_paths",
        label="Output redaction",
        unit="output paths",
        summary=(
            "Every boundary where agent output reaches a human or an external "
            "service runs a redaction pass first. Most run both scanners; the few "
            "that run only one, or that the owner can narrow to one for their own "
            "view, say so on their own row."
        ),
        source="src/kiro_crew/security_posture.py",
        items_fn=_redaction_sink_items,
    ),
    PostureControl(
        key="credential_families",
        label="Credential patterns",
        unit="credential families",
        summary=(
            "Credential classes the redaction scanner recognizes, in plaintext and "
            "base64-encoded form."
        ),
        source="src/kiro_crew/security/redaction_patterns.py",
        items_fn=_credential_family_items,
    ),
    PostureControl(
        key="exfil_heuristics",
        label="URL exfiltration detection",
        unit="heuristics",
        summary=(
            "Domain-agnostic — flags the payload, not the destination. A URL matching "
            "any heuristic is replaced with a redaction marker."
        ),
        source="src/kiro_crew/security/exfil.py",
        items_fn=_exfil_heuristic_items,
    ),
    PostureControl(
        key="audit_surfaces",
        label="SEL audit logging",
        unit="session-key surfaces",
        summary=(
            "Append-only, HMAC-chained security event log; the chain is verifiable "
            "for tampering. Redaction is applied on the forward path, and on-disk "
            "records are redacted by each caller before `log`. Listed below are the "
            "surfaces SEL infers from a session key; a call site may also stamp a "
            "more specific source of its own, so this is a floor, not a total."
        ),
        source="src/kiro_crew/sel.py",
        items_fn=_audit_surface_items,
    ),
    PostureControl(
        key="token_auth",
        label="Dashboard token auth",
        unit="auth controls",
        summary="Layered controls on every authenticated dashboard request.",
        source="src/kiro_crew/dashboard/token_auth.py",
        items_fn=_token_auth_items,
    ),
)


def _control_payload(control: PostureControl) -> dict:
    """Serialize one control, degrading to a count-less entry on failure.

    A control whose ``items_fn`` raises must not take down the whole posture
    response — the panel should show the rest of the posture and mark this one
    unavailable, since this endpoint is purely informational.
    """
    try:
        items = control.items_fn()
    except Exception:
        logger.warning("Security posture: items unavailable for %r", control.key, exc_info=True)
        return {
            "key": control.key,
            "label": control.label,
            "unit": control.unit,
            "summary": control.summary,
            "source": control.source,
            "count": None,
            "items": [],
            "unavailable": True,
        }
    return {
        "key": control.key,
        "label": control.label,
        "unit": control.unit,
        "summary": control.summary,
        "source": control.source,
        "count": len(items),
        "items": [{"label": i.label, "detail": i.detail} for i in items],
        "unavailable": False,
    }


def build_posture_snapshot() -> dict:
    """Build the full security-posture detail payload.

    Blocking: ``_denied_command_items`` reads the built-in rule table (in-memory)
    but the caller may also fold in the denied-commands snapshot, which touches
    the filesystem — see ``build_posture_snapshot_async``.
    """
    controls = [_control_payload(c) for c in _CONTROLS]
    return {
        "controls": controls,
        "counts": {c["key"]: c["count"] for c in controls},
    }


def posture_counts() -> dict[str, int | None]:
    """Just the ``key → count`` map, without materializing the item lists.

    For a counts-only caller (``/api/security/stats``) building and serializing the
    full ~45 KB item payload to return three integers is pure waste. Still resolves
    every ``items_fn`` — the count IS ``len(items)``, which is the invariant that
    keeps a count honest — it simply does not carry the items back.
    """
    return {c["key"]: c["count"] for c in (_control_payload(c) for c in _CONTROLS)}


async def posture_counts_async() -> dict[str, int | None]:
    """``posture_counts`` off the event loop (see ``build_posture_snapshot_async``)."""
    return await asyncio.get_running_loop().run_in_executor(governance_executor(), posture_counts)


async def build_posture_snapshot_async() -> dict:
    """Build the snapshot off the event loop.

    ``_tool_schema_items`` walks the validation module and the denied-rule table
    walk is O(137); neither blocks on I/O today, but this is a read-only
    informational endpoint and keeping it off the loop costs nothing while making
    a future filesystem-backed control safe to add.

    Offloaded to the dedicated ``governance_executor`` (``mc-gov``) — NOT the
    shared default pool — for the same reason its sibling
    ``build_governance_policy_snapshot_async`` is: this GET is
    browser-triggerable, so the moment a control DOES read the filesystem (the
    case the paragraph above exists to keep safe), default-pool I/O would
    contend with the workers the event loop shares for DNS. Choosing the right
    pool now means adding that control is a one-line change, not a latent stall.
    """
    return await asyncio.get_running_loop().run_in_executor(
        governance_executor(), build_posture_snapshot
    )
