"""Whether a harness's tool decisions reach Kiro Crew's PreToolUse gate.

One place resolves the verdict, so the refusal message, the doctor row and any
future dashboard surface cannot disagree about why a session was allowed.

The gate itself -- the bundled denied-command rules, the sensitive-path block,
the governance ceiling -- runs only from ``HookManager.on_tool_call``, reached
only from the permission-request branch of the dispatch parser. A harness that
does not send ``session/request_permission`` per tool call is a harness where
none of those controls execute, so "does it ask?" is a security question rather
than a compatibility one.

**A LEAF module, deliberately.** It imports the vocabulary from
:mod:`kiro_crew.acp_backends` and nothing from ``kiro_crew.acp``, which the SDK
boundary gate treats as a forbidden root. The callers that need a verdict --
``acp/client.py`` on the session path, and later ``kirocrew doctor`` and the
dashboard -- can therefore all reach it, and a consumer naming a verdict does not
buy a forbidden edge to do it.

**Enforcement scope.** Only :data:`~kiro_crew.acp_backends.Routing.SESSION_CONFIG`
is ENFORCED here today, because it is the only mechanism this core implements end
to end. ``AGENT_SPEC`` needs no enforcement (it holds by construction), and
``SEEDED_SETTINGS`` is declared-but-unenforced, and the reason is a read-back
gap rather than a missing writer. ``AcpClient._write_claude_local_settings`` does
seed ``permissions.defaultMode`` into ``<work_dir>/.claude/settings.local.json``,
but it writes only the file it OWNS -- created this session and still carrying the
bytes Crew wrote -- and declines otherwise, and nothing reads back whether the
adapter honoured the mode. A ``bypassPermissions`` already sitting in a user's own
``settings.local.json`` or ``~/.claude`` is therefore neither detected nor
stripped, so the precondition this mechanism would need is not established.
``routing_verdict`` reports that honestly as INDETERMINATE -- what is scoped is
whether a non-ROUTED verdict REFUSES, not whether it is told truthfully. Widening the scope
means implementing a mechanism, not editing an allowlist.
"""

from __future__ import annotations

import logging
from enum import Enum

from kiro_crew.acp_backends import (
    ACP_BACKEND_CODEX,
    Routing,
    permission_config_for,
    routing_for,
)

logger = logging.getLogger(__name__)

#: Routing mechanisms whose non-ROUTED verdict actually refuses a session.
#:
#: Scoped to what this core implements rather than to a list of harness ids: a
#: harness declaring an implemented mechanism is enforced automatically, and
#: adding a mechanism here without implementing it would assert a guarantee
#: nothing performs.
ENFORCED_ROUTINGS: frozenset = frozenset({Routing.SESSION_CONFIG})

#: What is NOT consulted when a harness's tool calls bypass the gate. Named in
#: full in the refusal, because "bypasses the security gate" does not tell an
#: operator what they are giving up.
UNENFORCED_CONTROLS = (
    "the bundled denied-command rules, the sensitive-path block and the governance ceiling"
)

#: Operator-facing harness labels. Local rather than imported: the refusal text is
#: the only consumer, and a Codex host must never be told to run ``kiro-cli
#: login``-style advice aimed at a different harness.
_LABELS: dict = {
    ACP_BACKEND_CODEX: "OpenAI Codex",
}

#: The credential store each enforced harness must still be able to read.
#:
#: An adapter authenticates itself, so its OWN token is the one thing the mask
#: below must not take away. Everything else on the read-gate floor is denied.
#:
#: Home-relative, matching the floor's own spelling. An operator override
#: (``CODEX_HOME``) moves the real file outside the home anyway, so it is not on
#: the floor and the mask never had it to exclude.
ADAPTER_OWN_CREDENTIAL_LEAVES: dict = {
    ACP_BACKEND_CODEX: (".codex/auth.json",),
}


class Verdict(str, Enum):
    """Whether a harness's tool decisions reach Kiro Crew's gate.

    ``INDETERMINATE`` is deliberately NOT a synonym for "probably fine". A
    guarantee that lapses whenever a file is unreadable is not a guarantee, so
    :func:`enforce_runtime_routing` treats it exactly like ``BYPASSED``. It is a
    distinct value only so the operator-facing message can say "could not
    determine" instead of asserting a policy nothing established.
    """

    ROUTED = "routed"
    BYPASSED = "bypassed"
    INDETERMINATE = "indeterminate"


class ToolGateUnroutable(Exception):
    """Raised when a harness's tool calls would not reach the PreToolUse gate.

    Deliberately NOT retryable: the condition is a configuration fact, so a retry
    re-reads the same answer and refuses again while consuming a reconnect budget
    that exists for transport faults.

    A plain ``Exception`` rather than an ``AcpError`` subclass because ``AcpError``
    lives in ``acp.client``, which imports THIS module -- subclassing would be an
    import cycle, and it would also drag a forbidden-root import into a leaf. The
    session path translates it into its own ACP-error type so branchless callers
    keep degrading through their generic handling.
    """


def adapter_hidden_credential_dirs(backend: str) -> tuple:
    """Absolute paths on the read-gate floor to hide from *backend*'s child.

    DERIVED from ``security.sensitive_home_dirs()`` rather than enumerated, and
    that is the whole point. This mask is the compensating control for a harness
    whose passive reads never reach ``HookManager.on_tool_call``: the floor states
    what an agent may never read, so anything on it the child can still open is a
    hole in the compensation. An enumerated short list left exactly that hole --
    ``.claude/.credentials.json``, ``.netrc``, ``.git-credentials``, ``.pypirc``
    and ``.npmrc`` were all on the floor and still readable by the child, the first
    of them a leaf this very change had just classified. Deriving keeps the two in
    step, and a floor entry added later is covered with no edit here.

    The harness's own credential store is excluded, because the adapter must read
    it to authenticate. That asymmetry is intentional and safe: the floor still
    blocks the AGENT's file tools from that leaf, so the two controls cover
    different readers rather than cancelling each other.

    ``.ssh`` arrives through the floor and it has a cost: git-over-SSH inside such
    a session stops working, because the private key is no longer readable. That
    is accepted rather than worked around -- leaving private keys readable would
    not close what this exists to close, and a harness landing new has no
    established workflow to break.

    Empty for a harness this core does not enforce, so the first-class path and
    every unenforced harness keep byte-identical sandbox arguments. Returns
    ABSOLUTE paths: ``sandbox.wrap_argv`` runs ``os.path.abspath`` over what it is
    handed, which would resolve a bare ``.aws`` against the CWD and silently deny
    nothing. File leaves are fine to pass -- the Linux launcher classifies each
    entry with its own ``isfile``/``isdir``, and the macOS profile emits both a
    ``subpath`` and a ``literal`` deny for each one, so a plain file is covered
    without depending on how Seatbelt treats a subpath over a non-directory.
    """
    if not is_enforced(backend):
        return ()
    # Imported here rather than at module scope: this is a LEAF that
    # ``acp/client.py`` imports at import time, and security.py is a large module
    # whose cost belongs on the one call that needs it.
    from kiro_crew.security import sandbox_credential_targets

    # Delegated rather than projected under ``Path.home()`` here: a credential the
    # operator relocated with ``KIROCREW_HOME`` / ``CLAUDE_CONFIG_DIR`` /
    # ``CLAUDE_HOME`` does NOT live under the real home, so a home-only projection
    # would hand the sandbox a path that denies nothing while the live secret stayed
    # readable. ``sandbox_credential_targets`` owns the same anchor rules as the read
    # gate, so this mask cannot drift from the floor it compensates for.
    return sandbox_credential_targets(tuple(ADAPTER_OWN_CREDENTIAL_LEAVES.get(backend, ())))


def enforce_sandbox_floor(backend: str, mode: str) -> None:
    """Refuse an enforced adapter whose credential mask would never be applied.

    :func:`adapter_hidden_credential_dirs` is the COMPENSATING control for a
    harness that self-approves its own tool calls: ACP v1 cannot force a prompt for
    a passive read, so that mask is the only thing between the child and the
    credential homes the standard tier deliberately leaves open for kiro-cli's
    sake. ``wrap_argv`` returns from its ``mode == "off"`` branch BEFORE it applies
    ``extra_hidden_dirs``, so in that configuration the mask silently evaporates and
    this adapter becomes strictly WEAKER than the gate-routed harnesses beside it --
    whose reads still reach the PreToolUse gate under the very same setting. The
    route's security argument assumes an OS boundary; this makes the assumption
    explicit instead of letting it fail open.

    Keyed on the EFFECTIVE tier, not the configured one: a governed host whose
    ``sandbox.min_level`` floor raises ``"off"`` still gets the mask, and must not
    be refused over a config value the ceiling already overrode.

    Returns for a harness this core does not enforce, so the first-class path and
    every unenforced harness reach the spawn unchanged.
    """
    if not is_enforced(backend):
        return
    # Local import for the same reason as the mask builder: this is a leaf that
    # ``acp/client.py`` imports at import time.
    from kiro_crew.sandbox import credential_mask_applies

    # Ask whether the mask WILL BE APPLIED, never whether one particular tier is
    # selected. An earlier revision tested ``effective_sandbox_mode(mode) != "off"``
    # and so covered only one of the two paths that hand back an unwrapped child: a
    # host with no sandbox backend and ``sandbox_allow_unsandboxed_exec`` opted in
    # resolves to a non-``off`` tier, passed the guard, and still spawned the adapter
    # with its credential mask dropped.
    if credential_mask_applies(mode):
        return
    raise ToolGateUnroutable(
        "{} routes tool calls through an enforced permission route whose "
        "compensating control is an OS-level credential mask, but this session would "
        "spawn it unsandboxed -- either agent.sandbox is 'off', or no sandbox backend "
        "is available and agent.sandbox_allow_unsandboxed_exec is set -- so the mask "
        "is never applied and the adapter's credential reads are unfenced. Set "
        "agent.sandbox to 'standard' or 'strict' ON A HOST WITH A WORKING BACKEND to "
        "select this harness, or select a harness whose tool calls reach the gate "
        "directly.".format(label_for(backend))
    )


def label_for(backend: str) -> str:
    """An operator-facing name for *backend*, falling back to the raw id."""
    return _LABELS.get(backend) or (backend or "Kiro CLI")


def routing_verdict(backend: str) -> tuple:
    """Report how *backend* routes tool calls, and why.

    Dispatches on the routing MECHANISM rather than the harness id, so a harness
    declaring an already-implemented mechanism needs no change here.

    Read-only and side-effect free: this is what a doctor row or a dashboard GET
    calls, and a probe that wrote a settings file would create one on every
    Settings page load.
    """
    routing = routing_for(backend)

    if routing is Routing.AGENT_SPEC:
        # kiro-cli and KAS are made to ask because the spawn names an agent, so
        # the precondition holds by construction and there is nothing to probe.
        return (Verdict.ROUTED, "the spawn names an agent")

    if routing is Routing.SESSION_CONFIG:
        option_id, value = permission_config_for(backend)
        if not option_id or not value:
            # A harness declaring the mechanism without naming its option is a
            # registration bug, and it must not read as routed.
            return (
                Verdict.INDETERMINATE,
                "the harness declares session-config routing but names no config option",
            )
        # ROUTED on a PROMISE, not a probe: the option lives on a session that
        # does not exist yet, so there is nothing on disk to read. The other half
        # of the guarantee is ``session_config_issue`` + the apply, which MUST run
        # after session/new and before the first prompt. Port this verdict without
        # that caller and the harness reports routed while running its own default
        # mode -- the one silent-bypass hole in this design.
        return (
            Verdict.ROUTED,
            f"the client enforces {option_id}={value} before the first prompt",
        )

    if routing is Routing.SEEDED_SETTINGS:
        # Declared, not enforced here -- and the gap is the READ-BACK, not a missing
        # writer. ``_write_claude_local_settings`` does seed the mode, but only into
        # the file Crew owns (created this session, bytes still Crew's) and declines
        # otherwise, and nothing confirms the adapter honoured it, so a
        # ``bypassPermissions`` already in the user's own settings is neither
        # detected nor stripped. Told truthfully rather than upgraded to ROUTED;
        # whether it REFUSES is a separate decision, and SEEDED_SETTINGS is outside
        # ENFORCED_ROUTINGS.
        return (
            Verdict.INDETERMINATE,
            "this core seeds the harness's permission settings only into a file it "
            "owns, and nothing reads back whether they took effect",
        )

    return (
        Verdict.INDETERMINATE,
        "Kiro Crew has not established how this harness routes tool calls",
    )


def is_enforced(backend: str) -> bool:
    """Whether a non-ROUTED verdict for *backend* refuses the session."""
    return routing_for(backend) in ENFORCED_ROUTINGS


def remediation_for(backend: str) -> str:
    """The concrete change an operator can make, or ``""`` when there is none."""
    routing = routing_for(backend)
    if routing is Routing.SESSION_CONFIG:
        option_id, value = permission_config_for(backend)
        if option_id and value:
            return (
                f"Install a {label_for(backend)} adapter that advertises ACP session "
                f"config option {option_id!r} with value {value!r}."
            )
    return ""


def session_config_issue(backend: str, config_options: object) -> str:
    """Why *backend*'s required permission config cannot be applied.

    ``""`` means the exact option AND value were advertised by ``session/new``.

    Permission routing is stricter than optional model or effort configuration: a
    missing option cannot be shrugged off as lazy advertising, because the first
    prompt would then run ungated.
    """
    if routing_for(backend) is not Routing.SESSION_CONFIG:
        return ""
    option_id, required = permission_config_for(backend)
    if not option_id or not required:
        return "the harness declares session-config routing but names no config option"
    if not isinstance(config_options, list):
        return "session/new did not advertise configOptions"
    for option in config_options:
        if not isinstance(option, dict) or option.get("id") != option_id:
            continue
        raw_values = option.get("options")
        if not isinstance(raw_values, list):
            return f"config option {option_id!r} has no values"
        values = {
            entry.get("value")
            for entry in raw_values
            if isinstance(entry, dict) and isinstance(entry.get("value"), str)
        }
        if required in values:
            return ""
        return f"config option {option_id!r} does not advertise required value {required!r}"
    return f"session/new did not advertise config option {option_id!r}"


def enforce_runtime_routing(
    backend: str,
    reason: str,
    *,
    verdict: Verdict = Verdict.BYPASSED,
    remedy: str = "",
) -> None:
    """Act on a routing fact learned after the harness process started.

    Raises :class:`ToolGateUnroutable` before the first prompt can run, or returns
    for a harness this core does not enforce.

    **There is deliberately no opt-out.** An earlier revision carried
    ``agent.acp_backend_allow_ungated_tools``, a LOCAL config bool that started the
    session anyway with a warning and an audit event. That is the precise shape the
    central governance ceiling exists to forbid: a managed fleet could allow this
    harness while a standard user's own config switched the compensating control
    off, so POLICY-intersect-PROFILE would no longer hold for the calls the harness
    self-approves. A security control with a local off-switch is not a control, and
    the escape hatch it offered was never needed -- the refusal names the concrete
    remedy (:func:`remediation_for`), and lowering the sandbox tier remains an
    operator decision that IS clamped by the ceiling.

    A harness outside :data:`ENFORCED_ROUTINGS` returns unchanged: the verdict is
    still reported truthfully by :func:`routing_verdict`, but this core does not
    implement its mechanism and must not refuse a session over a guarantee it
    never attempted.
    """
    if not is_enforced(backend):
        logger.debug(
            "tool-gate routing not enforced for %s (%s): %s",
            label_for(backend),
            routing_for(backend).value,
            reason,
        )
        return

    suffix = f" {remedy}" if remedy else ""
    raise ToolGateUnroutable(
        f"{label_for(backend)} tool calls would not reach Kiro Crew's security gate "
        f"({reason}), so {UNENFORCED_CONTROLS} would not be consulted for them.{suffix}"
    )


__all__ = [
    "ADAPTER_OWN_CREDENTIAL_LEAVES",
    "ENFORCED_ROUTINGS",
    "UNENFORCED_CONTROLS",
    "ToolGateUnroutable",
    "Verdict",
    "adapter_hidden_credential_dirs",
    "enforce_runtime_routing",
    "enforce_sandbox_floor",
    "is_enforced",
    "label_for",
    "remediation_for",
    "routing_verdict",
    "session_config_issue",
]
