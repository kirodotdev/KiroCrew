"""MCP rows of ``kirocrew doctor`` that neither repair a spec nor start a server.

Strict-identity routing, the MCP gateway daemon's code revision, and what each
selectable harness's MCP projection does to the default spec. The MCP Tools repair
and probe and the MCP Governance read stay in :mod:`kiro_crew.cli_doctor`, where
repository gates pin them.
"""

from __future__ import annotations

import platform as _plat
from typing import TYPE_CHECKING

from kiro_crew.doctor_checks import agents, render

if TYPE_CHECKING:
    from kiro_crew.config import KiroCrewConfig


#: The managed default agent, whose spec is the one a stock install runs.
#: Mirrors ``agent._MAIN_AGENT_NAME``, which is private; the doctor row below
#: reports on that spec because it is the one every default session resolves.
_MAIN_AGENT_NAME = "kirocrew"


def _doctor_unresolved_mcp_refs() -> None:
    """One row per selectable harness: would the default spec's ``@server`` refs
    resolve on it?

    The static twin of :mod:`kiro_crew.acp.mcp_ref_guard`, asked before a session
    starts. A spec with ``tools: ["@kirocrew-core", ...]`` and nothing defining
    ``kirocrew-core`` leaves every Crew tool absent while nothing is red
    (``providers/mirrors/README.md``); this row answers "my agent has no tools on
    this backend".

    Reports only and appends no issue: an unadopted harness with no projection yet
    is a known state, and failing the exit code on it would turn every stock host red.

    Asks ``agent_sdk`` because the agent spec and each backend's projection live below
    the agent-sdk-boundary gate: which file a harness reads its servers from is not a
    consumer's knowledge. ``agent_spec_mcp_refs`` reads the mirror seam only, so a
    backend projecting outside ``providers/mirrors/`` (KAS) reads as unprojected;
    ``has_mirror`` tells the two cases apart. kiro-cli resolves its refs against the
    spec it is handed, so a healthy install prints a clean row there.
    """
    from kiro_crew.agent_sdk.drivers.acp import agent_spec_mcp_refs

    try:
        spec_found, rows = agent_spec_mcp_refs(_MAIN_AGENT_NAME)
    except Exception:
        # Triage must survive an unreadable spec or registry; the rows are advisory.
        return
    if not spec_found:
        print("  mcp tool refs: \u23f9 no default agent spec on disk yet")
        return

    for backend, unresolved, has_mirror in rows:
        label = agents._backend_policy_label(backend)
        if not unresolved:
            print(f"  mcp tool refs: \u2705 {label} \u2014 every @server ref resolves")
            continue
        # Read off a hand-editable spec a cloned repo or an installed app can
        # author, so it can carry OSC/ANSI sequences that spoof the lines around
        # it -- the same reason every other spec-derived value in this report is
        # printed through _safe_display.
        refs = ", ".join(render._safe_display(ref) for ref in unresolved)
        if not has_mirror:
            print(f"  mcp tool refs: \u23f9 {label} has no mirror; unprojected: {refs}")
            # Same verdict as the runtime line (acp/mcp_ref_guard.py): the harness
            # may serve a listed ref from its own configuration, so never say "absent".
            render._print_wrapped(
                "Those refs name no server this backend would be handed. Crew's "
                "projection delivers none of them; the harness may mount a "
                "same-named server from its own configuration, so a listed ref may "
                "still be served and this row cannot tell which. The shared MCP "
                "gateway can still deliver a server it wrapped as a broker stub, "
                "which this row does not model. Which of those it is -- a decided "
                "no-channel harness or a projection that lives outside "
                "providers/mirrors/ -- is the KIND on that backend's entry in "
                "providers/mirrors/registry.py (PROJECTIONS); a backend that "
                "projects elsewhere reads as unprojected here."
            )
            continue
        print(f"  mcp tool refs: \u26a0 {label} projects a spec that still misses: {refs}")
        render._print_wrapped(
            "This backend HAS a mirror and its projection dropped these refs "
            "anyway -- a registry-marked entry, an entry with no usable "
            "transport, or a name the spec references but never defines. Compare "
            "the agent spec's mcpServers against its tools list."
        )


def _doctor_template_mcp_additions() -> None:
    """One row per template that carries MCP servers the user approved on a card.

    Lists each template's additions (``template_mcp_additions``) by server name with
    the card and date that approved them, so a user can see what was added and
    where to take it away. Silent when there are none, which is the stock state.
    Reports only and appends no issue: an addition is a choice the user made, not a
    fault. The values come off a user-writable file, so every one is printed through
    ``_safe_display``.
    """
    from kiro_crew import template_mcp_additions

    try:
        additions = template_mcp_additions.list_additions()
    except Exception:
        # Triage must survive an unreadable record; the rows are advisory.
        return
    for template, servers in additions.items():
        listed = ", ".join(
            f"{render._safe_display(name)} (card {render._safe_display(add.card_id)}, "
            f"{render._safe_display(add.approved_at)})"
            for name, add in servers.items()
        )
        print(f"  mcp additions: {render._safe_display(template)} \u2014 {listed}")


def _doctor_selected_backend_projection(cfg: KiroCrewConfig) -> None:
    """One row for the SELECTED backend when its declaration says ``no-channel``.

    Unlike :func:`_doctor_unresolved_mcp_refs`, this holds for a spec with no refs
    at all: a ``no-channel`` backend has no transport for Crew's servers, so every
    Crew tool is absent whatever the spec says. ``native``, ``mirror`` and
    ``external`` print nothing, because the servers reach the session there.

    The per-tool deny reach is stated once, in :func:`_doctor_backend_ability_cards`;
    a second phrasing would give one declaration two readings. This row carries only
    the gap's own address.

    Reports only and appends no issue: a harness whose transport cannot carry Crew's
    tools is a supported choice, not a broken install. The declaration comes from
    ``agent_sdk`` for the reason :func:`_doctor_backend_ability_cards` gives.
    """
    # circular import -- see agent_sdk.backend_mcp_ability._declaration.
    from kiro_crew.agent_sdk.backend_mcp_ability import ability_for

    try:
        backend = cfg.agent.acp_backend
    except Exception:
        return
    try:
        declared = ability_for(backend)
    except Exception:
        return
    if not declared.projection:
        return
    kind, channel, tracking = declared.projection, declared.channel, declared.tracking
    label = agents._backend_policy_label(backend)
    if kind != "no-channel":
        return
    print(f"  mcp projection: \u23f9 {label} carries none of Kiro Crew's own tools")
    render._print_wrapped(
        "This harness advertises no transport the session MCP array can use, so "
        "Crew's servers are absent by declaration rather than by a "
        "misconfiguration. The shared MCP gateway does not change that: a broker "
        "stub is shaped as a stdio element too, so it lands in the same array. "
        "Switching agent.acp_backend is the operator-side remedy; the line below "
        "is what would have to be built instead."
    )
    # Read off a declaration a plugin-registered backend can author, so it is
    # printed through the same display guard as every other value in this report.
    render._print_wrapped(f"Would need: {render._safe_display(channel)}")
    render._print_wrapped(f"Tracked at: {render._safe_display(tracking)}")


def _doctor_backend_ability_cards(cfg: KiroCrewConfig) -> None:
    """The MCP ability of the harness IN USE, and which others cost a whole server.

    Two facts print, not a per-harness table; the dashboard panel owns the comparison,
    and a six-row table on every run is a section people learn to skip:

    * the in-use harness's card -- its projection kind, the reach of a per-tool MCP
      restriction, and the spec keys it withholds or has no channel for;
    * the harnesses where switching one tool off can withhold CREW'S OWN servers, the
      fact a chooser needs before switching. A ``per-call`` harness withholds a
      third-party server whole but still refuses per tool on Crew's own, so it is
      not named.

    The row prints the declaration's own values, not an English gloss: the panel owns
    the prose and leaves out ``native``/``mirror``/``external``, so this report states
    them. The one consequence sentence is for ``whole-server`` reach: switching a tool
    off withholds its whole server, and where that is ``kirocrew-core`` the session
    cannot report back to its channel. Which reaches carry that cost is
    ``McpAbility.costs_control_plane``. Nothing is authored per harness, so a new
    backend is covered once its ``PROJECTIONS`` entry exists.

    Reports only and appends no issue: a declared difference between harnesses is not
    a fault. Asks ``agent_sdk`` because ``providers/mirrors`` sits below the
    agent-sdk-boundary gate; ``providers/mirrors/README.md`` ("Fill the card") says
    what each surface of the card owes a reader.
    """
    from kiro_crew.acp_backends import selectable_backend_values

    # circular import -- see agent_sdk.backend_mcp_ability._declaration.
    from kiro_crew.agent_sdk.backend_mcp_ability import ability_for, spec_keys

    try:
        selected = cfg.agent.acp_backend
    except Exception:
        return
    try:
        rows = [(backend, ability_for(backend)) for backend in selectable_backend_values()]
        keys = spec_keys()
    except Exception:
        # Triage must survive an unreadable registry; this section is advisory.
        return
    if not rows:
        return
    in_use: str = ""
    costly: list[str] = []
    for backend, ability in rows:
        label = agents._backend_policy_label(backend)
        if ability.costs_control_plane:
            costly.append(label)
        if backend != selected:
            continue
        if not ability.projection:
            continue
        # The declaration's own words; the panel owns the prose. Scrubbed because a
        # plugin-registered backend authors its own values.
        parts = [f"projection: {render._safe_display(ability.projection)}"]
        if ability.per_tool_deny:
            parts.append(f"per-tool deny: {render._safe_display(ability.per_tool_deny)}")
        if ability.withheld:
            named = ", ".join(keys.get(cid, cid) for cid in ability.withheld)
            parts.append(f"not sent from your agent file: {named}")
        if ability.no_channel:
            named = ", ".join(keys.get(cid, cid) for cid in ability.no_channel)
            parts.append(f"no channel yet: {named}")
        in_use = "; ".join(parts)
    if not in_use and not costly:
        return
    print("  mcp ability:")
    if in_use:
        print(f"    {agents._backend_policy_label(selected)} (in use): {in_use}")
    if costly:
        # Name the harnesses: a chooser cannot act on a warning that does not say
        # where it holds.
        render._print_wrapped(
            "On "
            + ", ".join(costly)
            + ", switching a single MCP tool off withholds the whole server that tool "
            "belongs to rather than the tool alone -- and where that server is "
            "kirocrew-core, that session cannot report back to the channel it came "
            "from."
        )


#: MCP servers that host strict-identity tools — the reflexive verbs
#: (``monitor_start``, ``session_ledger_*``, ``set_project``, ``ask_question``)
#: and the authorization-subject ones (session control, ``chat_folder_*``).
#: A doctor-local tuple. On macOS and Windows every member missing from
#: ``mcp_gateway.stub_servers`` is reported; no agent spec is read, so an
#: opt-in server (``kirocrew-dashboard``, ``kirocrew-panel``) is reported even
#: when no agent references it.
_STRICT_IDENTITY_SERVERS = (
    "kirocrew-core",
    "kirocrew-dashboard",
    "kirocrew-work",
    "kirocrew-crew-log",
    "kirocrew-debug",
    "kirocrew-panel",
    "kirocrew-guide",
    "kirocrew-mate",
)


#: How Doctor names a probe that could not verify the daemon. "unreachable"
#: is a transport that did not answer; "unverified" is an answer that could not
#: be trusted or read. Only ``absent`` prints "not running".
_DAEMON_PROBE_REASONS: dict[str, tuple[str, str]] = {
    "connect_timeout": ("unreachable", "connect timed out"),
    "connect_error": ("unreachable", "connect failed"),
    "response_timeout": ("unreachable", "no reply in time"),
    "connection_lost": ("unreachable", "connection closed before a reply"),
    "refused_principal": ("unverified", "endpoint owner not confirmed"),
    "malformed_response": ("unverified", "malformed reply"),
    "not_pong": ("unverified", "reply was not a pong"),
    "invalid_context": ("unverified", "probe ran inside an event loop"),
}


def _doctor_mcp_gateway_daemon(issues: list[str]) -> None:
    """Report the MCP gateway daemon's code revision next to this one.

    The daemon pools MCP backends across sessions in its own process. One that
    outlived a code change keeps serving backends from the old checkout, and the
    symptom is far from the cause: a directive tool reports success while the
    gateway logs ``not_derivable``. A mismatch IS an issue. The exception is a
    daemon stamped by the other fingerprint rule (``code_fingerprint.same_scheme``)
    with a live owner: it belongs to another install, so it is information only.
    """
    try:
        from kiro_crew.code_fingerprint import code_fingerprint, same_scheme
        from kiro_crew.mcp_gateway import daemon_control as dc

        probe = dc.describe_daemon_detailed()
    except Exception:
        return
    info = probe.info
    if info is None:
        if probe.outcome == dc.PROBE_ABSENT:
            print(
                "  mcp gateway daemon: ⏹ not running (pooling off, or no session has started one)"
            )
            return
        label, reason = _DAEMON_PROBE_REASONS.get(probe.outcome, ("unverified", probe.outcome))
        print(f"  mcp gateway daemon: ⚠ {label}: {reason} after {probe.elapsed_secs:.1f}s")
        return
    mine = code_fingerprint()
    owner = (
        "no owner recorded"
        if info.owner_pid <= 0
        else f"owner pid {info.owner_pid} {'alive' if info.owner_alive else 'GONE'}"
    )
    if info.fingerprint == mine:
        print(f"  mcp gateway daemon: ✅ pid {info.pid}, same code as this install ({owner})")
        return
    if info.fingerprint and not same_scheme(info.fingerprint, mine) and info.owner_alive:
        # Git revision vs mtime stamp with a live owner: another install's daemon.
        # Without a live owner this gateway may have adopted it, so it falls
        # through to the issue below, where a restart replaces it.
        print(
            f"  mcp gateway daemon: ⏹ pid {info.pid} belongs to another install "
            f"(runs {info.fingerprint}; this install is {mine}) ({owner})"
        )
        render._print_wrapped(
            "A source checkout and an installed build stamp their code differently, "
            "so the two cannot be compared. Run `kirocrew doctor` from the install "
            f"whose gateway started the daemon to check it. If that gateway (pid "
            f"{info.owner_pid}) should no longer run, stop it: the daemon exits "
            "once its owner is gone."
        )
        return
    theirs = info.fingerprint or "unknown (pre-fingerprint build)"
    print(
        f"  mcp gateway daemon: ❌ pid {info.pid} runs code {theirs}; this install is {mine} ({owner})"
    )
    render._print_wrapped(
        "The daemon outlived a code change and its pooled MCP servers speak the "
        "old revision's wire shapes (session directives, app calls). Run "
        "`kirocrew restart`, which stops the daemon along with the gateway so the "
        "replacement spawns its own."
    )
    issues.append("MCP gateway daemon runs a different code revision than this install")


#: What each launch-refusal reason means to the operator, keyed by the reason
#: :mod:`kiro_crew.mcp_gateway.launch_approval` records. A reason missing here is
#: still reported, under its raw name, so a new reason is never dropped silently.
_SHARE_REFUSAL_LABELS = {
    "added_outside_dashboard": "not approved yet",
    "changed_needs_reapproval": "command changed since it was approved",
}


def _doctor_mcp_share_refused(cfg: KiroCrewConfig, issues: list[str]) -> None:
    """Name every server config asks to share whose launch approval is refused.

    A server listed for sharing (``mcp_gateway.stub_servers``, which already folds
    in ``stub_overrides``) runs through the shared gateway only once its launch is
    approved on the dashboard. Until then the gateway records a refusal and every
    session starts its own copy, so config says "shared" while the host runs one
    per chat. Nothing else on the command line says so.

    Reports, never approves: the approval stays a dashboard action, because the
    approval write is what pins the exact launch the gateway resolved. A refusal
    IS an issue -- config and the running host disagree, and only the operator's
    approval (or removing the server from the share list) reconciles them.
    """
    try:
        gw = cfg.mcp_gateway
        if not gw.enabled:
            return
        asked = set(gw.stub_servers)
    except Exception:
        return
    if not asked:
        return
    try:
        from kiro_crew.mcp_gateway.launch_approval import (
            approvals_path,
            approvals_store_problem,
            refused_servers,
            refused_shares,
        )

        # The launch path reads an unreadable store as "nothing refused", so
        # check readability first: this row must never call that healthy.
        problem = approvals_store_problem()
        if problem is not None:
            print(f"  mcp sharing: ⚠ cannot read launch approvals: {problem}")
            render._print_wrapped(
                f"{approvals_path()} could not be read, so whether the servers set to "
                "share are approved is unknown. Every launch it would approve is "
                "treated as not approved, so those servers run one copy per chat."
            )
            issues.append("MCP launch approvals cannot be read: sharing state unknown")
            return
        shares = refused_shares(asked, refused_servers())
    except Exception as exc:
        print(f"  mcp sharing: ⚠ could not read launch approvals ({type(exc).__name__})")
        return
    hits = sorted(shares)
    if not hits:
        print(f"  mcp sharing: ✅ {len(asked)} server(s) set to share, none waiting for approval")
        return
    rows = ", ".join(
        f"{name} ({_SHARE_REFUSAL_LABELS.get(shares[name], shares[name])})" for name in hits
    )
    print(f"  mcp sharing: ⚠ not shared, one copy per chat: {rows}")
    render._print_wrapped(
        "Config asks to share these servers, but the gateway refused their launch "
        "until it is approved on the dashboard. Open Customize \u2192 Connections, the "
        "MCP Servers sub-tab, and scroll to the MCP Management section "
        "(/capabilities?tab=mcp&view=mcp-servers), approve each one, then run "
        "`kirocrew restart` so new chats use the shared copy.",
        keep=("`kirocrew restart`",),
    )
    issues.append(f"{len(hits)} MCP server(s) set to share are not shared: launch not approved")


def _doctor_strict_identity(cfg: KiroCrewConfig) -> None:
    """Report configured routing, not proof of a live session's identity channel.

    On the kiro backend a session's process is an ``AcpRuntime``, which is
    session-UNBOUND by design (one process multiplexes N sessions, so it cannot
    carry one session's key in its environment — ``acp/runtime.py`` injects
    none). The gateway's per-call caller injection is one strict-identity
    channel, and it exists only for servers listed in
    ``mcp_gateway.stub_servers``. It is not the only one:
    :func:`kiro_crew.mcp_core._resolve_session_key_strict` also accepts the
    signed per-session token that ``AcpRuntime`` mints unconditionally for its
    control-plane elements, and the verified host-pid sidecar. An unrouted
    server is therefore "not routed through the gateway", not "without an
    identity channel"; its strict tools are refused only when none of those
    channels resolves, and the refusal then names the cause
    (:func:`kiro_crew.mcp_core.strict_identity_diagnosis`).

    Reports only, and deliberately appends NO entry to doctor's ``issues``:
    ``mcp_gateway.stub_servers`` is empty by default because routing starts a
    broker plus a stub per server, so a hard issue here would make
    ``kirocrew doctor`` exit 1 on every stock install — the same failure the
    speech-to-text section is written to avoid. Parity with
    :func:`_doctor_trust_root`, which also only prints.

    Skipped where the env sources exist by construction: on Linux the sandbox
    launcher exports ``KIROCREW_HOST_PID``, so routing is not what decides
    whether strict identity resolves.
    """
    if _plat.system() not in ("Darwin", "Windows"):
        return
    try:
        routed = set(cfg.mcp_gateway.stub_servers)
    except Exception:
        routed = set()
    unrouted = [s for s in _STRICT_IDENTITY_SERVERS if s not in routed]
    if not unrouted:
        print("  strict identity: ⏹ routing configured — live session identity not verified")
        render._print_wrapped(
            "This checks mcp_gateway.stub_servers, not the running session's "
            "launch command or per-call caller injection. Confirm a strict-identity "
            "tool succeeds in the affected dashboard session."
        )
        return
    names = ", ".join(unrouted)
    print(f"  strict identity: ⏹ not routed through the gateway: {names}")
    render._print_wrapped(
        "This is the default and needs no change. Tools that must know which "
        "session is calling (monitor_start, cron_add, session_ledger_*, "
        "set_project, ask_question, session control, chat_folder_*) identify it "
        "from a signed per-session token on the session's MCP element, which "
        "needs no routing and no gateway. This check reads config only and does "
        "not verify a live session. If one of those tools is refused, its error "
        "names the identity channel that failed; route the server from MCP "
        "Management (or add it to mcp_gateway.stub_servers and restart) only "
        "when that error says the element carried no session token. Leaving "
        "servers unrouted is a valid choice — routing starts a broker and one "
        "stub process per server."
    )
