"""The governance checks kirocrew-core tools run before they act.

Channel-agent containment (the messaging refusal and the dispatch refusal
``_call_tool_inner`` applies to every call), and the messaging, browse, channel and
memory-write vets with the audit row a denial writes.

:mod:`kiro_crew.mcp_core` stays the import path and the patch surface; see
:mod:`kiro_crew.mcp_core_parts`.
"""

from __future__ import annotations

import os
from typing import Any

from kiro_crew.mcp_core_parts.identity import require_strict_session_key


def _deny_channel_agent_messaging(caller_session: str, tool_name: str) -> str | None:
    """Return an ``Error:`` denial when a channel agent calls a messaging tool.

    Channel agents (session keys ``channel:<channel_id>:<agent_id>``) are
    confined to channel-post communication. The interactive guard in
    ``channel.py`` rejects these tools at the permission-request event, but
    an AUTO-APPROVED call (kirocrew-core is in the default ``allowedTools``)
    never emits that event — so the containment boundary must also hold
    here at MCP dispatch, keyed on the verified caller identity.
    Best-effort SEL audit mirrors channel.py's
    ``rejected_blocked_tool`` outcome; audit failure never unblocks the
    deny.
    """
    if not caller_session.startswith("channel:"):
        return None
    try:
        # Resolved from ``kiro_crew.sel`` at call time, not through the
        # module-level binding, so a substituted SEL factory is observed.
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key=caller_session,
            source="mcp",
            tool_name=tool_name,
            tool_kind="kirocrew-core",
            outcome="rejected_blocked_tool",
        )
    except Exception:
        # File-backed SEL write; stdio-silent (no logger — stderr would
        # corrupt the JSON-RPC stream). The deny below still holds.
        pass
    return (
        f"Error: {tool_name} is not available to channel agents — "
        "communicate through channel posts instead."
    )


def _deny_channel_agent_dispatch(tool_name: str, args: dict[str, Any] | None = None) -> str | None:
    """Return an ``Error:`` denial when a channel agent calls a dispatch verb.

    The dispatch verbs are the ones that start work outside the caller's own
    turn: they create a descendant agent, drive one that is already running, or
    open a context something else can drive later. The full list, and the reason
    each name is on it, live with ``CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS`` in
    ``channel.py`` -- imported here rather than respelled, so the interactive
    permission guard and this one cannot drift apart.

    A name alone does not identify one of these calls when the tool is a
    passthrough carrying a whole API surface, so a second set keyed on the
    operation, ``CHANNEL_AGENT_BLOCKED_DISPATCH_OPERATIONS``, holds those: the
    tool stays callable and only the operations that start work are refused.
    That is why this takes the call's arguments as well as its name.

    Why the refusal lands HERE, on the channel agent's own hop, rather than on
    the descendant: a descendant's session key is ``subagent:<id>``, carrying no
    trace of where its chain began, so a containment check keyed on a
    ``channel:`` identity sees an ordinary sub-agent and allows the call. Marking
    descendants instead would mean an unmarked descendant is indistinguishable
    from a legitimate one, so a path that is missed allows silently. Refusing the
    verb that creates the descendant uses the one identity that is verified at
    this point, and a path that is missed refuses.

    Identity comes from the STRICT resolver, never the lenient ancestor walk: a
    channel agent is launched by the gateway with its session key injected, so
    its key is always resolvable, while the lenient walk would let an
    unattributable caller inherit a parent's identity.

    Best-effort SEL audit mirrors channel.py's ``rejected_blocked_tool``
    outcome; an audit failure never unblocks the deny.
    """
    # The name and operation tests run first because they are the tests with no
    # observable cost. This gate sits on every kirocrew-core call, and each handler
    # resolves its own caller: a resolve here as well means a verb this gate does
    # not hold resolves identity twice, which a read verb's identity contract pins
    # against at exactly one strict resolve. So a call that is not held leaves
    # through the cheapest possible path, having touched nothing. The import is a
    # ``sys.modules`` hit once the first call has paid it.
    from kiro_crew.channel import (
        CHANNEL_AGENT_BLOCKED_DISPATCH_OPERATIONS,
        CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS,
    )

    denied = tool_name in CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS
    # What the denial names. A whole tool when the name is held; the single
    # operation when it is not, because naming the tool there would tell a caller
    # its reads are gone when they are not.
    subject = tool_name
    if not denied:
        operations = CHANNEL_AGENT_BLOCKED_DISPATCH_OPERATIONS.get(tool_name)
        if not operations:
            return None
        call = args or {}
        # Upper-cased so the deny is never spelled more narrowly than the
        # operation it holds; the path is compared as given, because the tool's
        # schema admits only an exact member of its own allowlist there.
        operation = (str(call.get("method", "")).upper(), str(call.get("path", "")))
        if operation not in operations:
            return None
        subject = f"{operation[0]} {operation[1]} on {tool_name}"
    caller_session = require_strict_session_key("channel-agent containment")[0]
    if not caller_session.startswith("channel:"):
        return None
    try:
        # Resolved from ``kiro_crew.sel`` at call time, not through the
        # module-level binding, so a substituted SEL factory is observed.
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key=caller_session,
            source="mcp",
            tool_name=tool_name,
            tool_kind="kirocrew-core",
            outcome="rejected_blocked_tool",
        )
    except Exception:
        # File-backed SEL write; stdio-silent (no logger -- stderr would
        # corrupt the JSON-RPC stream). The deny below still holds.
        pass
    return (
        f"Error: {subject} is not available to channel agents -- a channel "
        "agent may not start work that outlives its own turn. Do the work in "
        "the turn and report it as a channel post."
    )


def _vet_messaging_governance(
    caller_session: str,
    tool_name: str = "send_message",
    fail_closed: bool = False,
) -> str | None:
    """Return a denial reason if governance forbids outbound messaging, else None.

    ``tool_name`` attributes the SEL audit records (denial / degraded) to the
    actual calling tool — the gate is shared by ``send_message`` and
    ``send_notification``, and the persisted audit trail must name the real
    caller.

    ``fail_closed=True`` (the ``send_notification`` posture) makes a
    governance-evaluation ERROR deny instead of degrade-open: it is passed
    through to :func:`governance_permits` (which then returns a denying
    Decision on internal error) AND honored in this helper's own except
    branch, so an exception escaping ``governance_permits`` itself cannot
    fail-open either.  The degrade is still SEL-audited on both paths.

    Proactive/outbound messaging is a ``capabilities.messaging`` gate (an exfil
    surface a policy/profile may disable per surface/app).  Runs in the
    ``kirocrew-core`` stdio subprocess, which DOES boot the platform via
    ``cli.main`` — so ``current_context()`` carries the ceiling.  Best-effort
    for ``send_message`` (fail_closed=False): a ``PlatformCompositionError``
    propagates; any other error returns None.
    Emits no stray stdout/stderr (either would corrupt the JSON-RPC stream); a
    fail-open degrade is audited via the file-backed ``governance_degraded`` SEL
    only (``log_warning=False`` suppresses the logger here).
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import vet_and_audit

        # Shared evaluate+audit seam: the decision AND its SEL record —
        # allowed or denied — come from one code path, so record shapes and
        # fail-closed semantics cannot drift between governed
        # outbound-messaging callers.
        decision = vet_and_audit(
            "capabilities.messaging",
            "",
            session_key=caller_session,
            tool_name=tool_name,
            app=_governance_app(),
            fail_closed=fail_closed,
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            return "outbound messaging blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # No logger here: this runs inside the kirocrew-core stdio MCP server,
        # whose stray stdout/stderr would corrupt the JSON-RPC stream (same
        # constraint as redact_via_context). Still emit the file-backed
        # governance_degraded SEL (no stdout) so the degrade is auditable.
        # Wrapped so a late-import failure cannot raise ImportError out of this
        # except-branch and hard-fail the stdio tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                tool_name,
                session_key=caller_session,
                scope="capabilities.messaging",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        if fail_closed:
            return "governance evaluation failed; denying (fail-closed)"
        return None


def _vet_browse_governance(caller_session: str) -> str | None:
    """Return a denial reason if governance forbids web browsing, else None.

    The ``browser`` MCP tool drives the native panel (and points at the
    playwright-cli fallback), a web-egress surface an enterprise policy may
    disable via ``capabilities.browse``. When DENIED the tool must refuse
    outright and NOT fall back to playwright-cli -- falling back would let
    browsing continue and defeat the control. This is distinct from the
    no-native-panel case (capability ALLOWED, just no Electron), which is the
    only condition that legitimately degrades to playwright-cli.

    Same stdio-silent, best-effort discipline as :func:`_vet_messaging_governance`
    (fail-OPEN on evaluation error: a broken policy eval must not brick browsing
    on a default install). Runs inside the ``kirocrew-core`` stdio subprocess,
    which boots the platform via ``cli.main`` so ``current_context()`` carries
    the ceiling.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import vet_and_audit

        decision = vet_and_audit(
            "capabilities.browse",
            "",
            session_key=caller_session,
            tool_name="browser",
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            return "web browsing is disabled by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "browser",
                session_key=caller_session,
                scope="capabilities.browse",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None


def _vet_channel_governance(
    caller_session: str, transport: str, tool_name: str = "send_message"
) -> str | None:
    """Return a denial reason if governance forbids messaging *via transport*.

    The ``channels`` scope (a ScopedMap) is the per-transport allowlist: which
    chat transports (``slack``, future ``discord``/``telegram``) outbound
    messaging may use.  It is finer-grained than the on/off
    ``capabilities.messaging`` gate above — a policy may permit messaging
    generally but restrict it to specific transports (e.g. Slack only).  We
    query the ScopedMap ``members`` allowlist for *transport*.  ``posture`` (the
    per-transport identity ceiling, policy-only) is enforced at the transport's
    own admission path, not here.

    ``tool_name`` attributes the SEL audit records (denial / degraded) to the
    actual calling tool — the gate is shared by ``send_message`` and
    ``update_message``, and the persisted audit trail must name the real caller.

    Same stdio-silent, fail-closed-CPP discipline as
    :func:`_vet_messaging_governance`.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # A bare member id queries the ScopedMap ``members`` ruleset.
        decision = governance_permits(
            "channels",
            transport,
            session_key=caller_session,
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            _audit_governance_deny(caller_session, f"{tool_name}:{transport}", "channels", decision)
            return f"messaging via transport {transport!r} blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrapped: a late-import failure must not hard-fail the stdio tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                f"{tool_name}:{transport}",
                session_key=caller_session,
                scope="channels",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None


def _audit_governance_deny(session_key: str, tool_name: str, scope: str, decision: object) -> None:
    """Best-effort SEL audit of a governance denial (writes to the JSONL file,
    NOT stdout — safe in the stdio MCP server). Never raises."""
    try:
        # Resolved from ``kiro_crew.sel`` at call time, not through the
        # module-level binding, so a substituted SEL factory is observed.
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=session_key,
            tool_name=tool_name,
            scope=scope,
            outcome="denied",
            rule=getattr(decision, "rule", ""),
            layer=getattr(decision, "layer", ""),
            reason=getattr(decision, "reason", ""),
        )
    except Exception:
        # No stdout/stderr in the stdio server; SEL writes to a file so this is
        # safe, but a failure here must never wedge the deny path.
        pass


def _governance_app() -> str:
    """Best-effort active app slug for per-app profile binding, or "".

    An app backend process carries ``KIROCREW_APP_NAME`` (set in
    ``apps.backend.start_app_backend``); when an app's own tool call reaches a
    governance chokepoint in-process, this lets a per-app profile
    (``bind:{type:"app"}``) resolve.  NOTE: the managed ``kirocrew-core`` MCP
    server is spawned by kiro-cli, NOT by an app backend, so this env var is
    absent there — a per-app profile is therefore only reachable for in-app tool
    calls today, not for the agent's MCP-routed ``learn_add``/``send_message``
    (those still resolve the per-SURFACE profile + policy ceiling, which is the
    enforced path).  Returns "" when not in an app context.
    """
    return os.environ.get("KIROCREW_APP_NAME", "")


def _vet_memory_writes_governance(caller_session: str) -> str | None:
    """Return a denial reason if governance forbids durable memory writes, else None.

    A durable memory/lesson write (``learn_add`` → persisted lesson) is an
    instruction-injection surface: content written here is re-injected into
    every future session's context.  The ``capabilities.memory_writes`` gate
    (default ON in the catalog) lets a policy/profile forbid it for a surface/app
    (e.g. a sandboxed app must not be able to plant a durable instruction).  Same
    stdio-silent, fail-closed-CPP discipline as :func:`_vet_messaging_governance`.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            "capabilities.memory_writes",
            "",
            session_key=caller_session,
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            _audit_governance_deny(
                caller_session, "learn_add", "capabilities.memory_writes", decision
            )
            return "durable memory writes blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrapped: a late-import failure must not hard-fail the stdio tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "learn_add",
                session_key=caller_session,
                scope="capabilities.memory_writes",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None
