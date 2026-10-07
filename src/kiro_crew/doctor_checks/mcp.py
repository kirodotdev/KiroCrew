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

    The static half of the runtime detector in
    :mod:`kiro_crew.acp.mcp_ref_guard`, answering the same question before a
    session rather than during one. The defect it names has shipped on three
    harnesses (``providers/mirrors/README.md``): a session comes up with
    ``tools: ["@kirocrew-core", ...]`` and nothing defining ``kirocrew-core``, so
    every Crew tool is absent while the harness works and nothing anywhere is red.
    A row here is the answer to "my agent has no tools on this backend" that
    otherwise takes a diagnosis.

    Reports only, and appends NO entry to ``issues``, on the terms
    :func:`_doctor_strict_identity` sets: a harness the operator has not adopted
    having no projection yet is a known state of the tree, not a broken install,
    and failing doctor's exit code on it would make every stock host red for a
    backend nobody selected.

    Asks ``agent_sdk`` rather than assembling the answer here. The refs need the
    agent spec and each backend's spec projection, both of which live below the
    boundary, so reaching them from this module would take three new ACP /
    providers edges the agent-sdk-boundary gate refuses -- and correctly: which
    file a harness reads its servers from is exactly the knowledge a consumer is
    not supposed to hold. ``agent_spec_mcp_refs`` reads the mirror seam, so a
    backend projecting outside ``providers/mirrors/`` (KAS) reads as unprojected;
    ``has_mirror`` is what lets the row say which case it is.

    kiro-cli resolves its refs against the spec it is handed, so a healthy install
    prints a clean row there rather than every ref it declares -- the resolver keys
    that on the backend id, not on this function.
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
            # The verdict is the runtime line's (acp/mcp_ref_guard.py), and it is
            # the same on every backend: "absent" is the strong claim, and no
            # backend's array is provably the session's whole MCP surface -- the
            # harness may serve a listed ref from a configuration of its own, which
            # neither this row nor the runtime line reads.
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


def _doctor_selected_backend_projection(cfg: KiroCrewConfig) -> None:
    """One row for the SELECTED backend when its declaration says ``no-channel``.

    :func:`_doctor_unresolved_mcp_refs` answers this per selectable harness, off
    the default spec's own refs. This answers a different question, about the one
    harness the operator actually configured, and it answers it for a spec that
    references no server at all: a ``no-channel`` backend has no transport that
    can carry Crew's servers, so every Crew tool is absent from its sessions
    whatever the spec says. That is a property of the harness, not of the spec,
    and a spec with an empty ``tools`` list produces no unresolved ref to hang it
    off.

    The no-channel row prints only for that one kind. ``native``, ``mirror`` and
    ``external`` all mean the servers do reach the session, so a row there would be
    noise on every stock install — and the refs row already speaks when a
    projection drops something.

    **The per-tool deny reach is NOT stated here.**
    :func:`_doctor_backend_ability_cards` above states it once -- in this harness's own
    ability row, and in the one sentence that says what the costly reach costs -- and a
    second phrasing for the same harness is how one declaration ends up with two
    readings that can disagree. What is left here is the part no other line carries:
    the gap's own address, which is maintainer-facing detail at maintainer length.

    Reports only, and appends NO entry to ``issues``, on the terms
    :func:`_doctor_strict_identity` and :func:`_doctor_unresolved_mcp_refs` both
    set: choosing a harness whose transport cannot carry Crew's tools is a
    supported configuration with a declared reason, not a broken install, and
    failing doctor's exit code on it would make a deliberate choice read as a
    fault.

    Asks ``agent_sdk`` for the declaration rather than reading it here -- see
    :func:`_doctor_backend_ability_cards` below for why, and
    ``providers/mirrors/README.md`` ("Fill the card") for what the two sections owe a
    reader between them.
    """
    # circular import -- see agent_sdk.backend_mcp_ability._declaration; the same
    # edge, reached from this consumer instead.
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

    The rows above answer for the selected harness only when something about it is
    wrong. This answers what a reader asks before switching, and what the selected
    harness is doing to their agent file right now: these harnesses are not
    interchangeable, and this section is where a terminal reader sees how they
    differ over the spec.

    **Two lines on a stock run, not a table.** The full per-harness comparison is the
    dashboard's job -- it has the room, the labels in every shipped language, and a reader
    who came to compare. A terminal report is read by someone diagnosing one install,
    and a row apiece for six harnesses on every run is a section people learn to skip,
    which costs the report more than the comparison was worth. So exactly two facts
    print here:

    * the ability card of the harness IN USE -- its projection kind, the reach of a
      per-tool MCP restriction, and the spec keys it withholds or has no channel for.
      This one is not a comparison: it is what the reader's own sessions are doing;
    * the harnesses where switching one tool off can withhold CREW'S OWN servers, named
      on one line, because that is the fact a chooser needs before they switch and the
      one an operator otherwise meets by accident. Narrower than "costs a whole server"
      on purpose: a ``per-call`` harness withholds a third-party server whole and still
      refuses per tool on Crew's own, so its session keeps the channel it came from and
      the panel is where that difference has room to be explained.

    Everything else about the harnesses not in use -- their withholds, their gaps,
    their kinds -- is in the panel, which is where a reader comparing harnesses is: this
    report answers for the install in front of it and names the one cross-harness cost
    that a chooser cannot act without.

    **Values, not prose, and the ROUTE lives here.** The row prints what the
    declaration says -- the kind and the reach in the registry's own spelling -- rather
    than an English gloss of it. The panel owns the prose, and it deliberately owns
    LESS: ``native``/``mirror``/``external`` costs a reader choosing a harness nothing,
    so the card does not carry it and this report is where it is stated. The register
    suits that reader anyway: they are in a terminal and the next thing they do is read
    ``providers/mirrors/registry.py``.

    **One consequence sentence, for one reach.** The exception, and it is not a gloss
    of the row: ``whole-server`` means switching a single tool off withholds the whole
    server that tool belongs to, and where that server is ``kirocrew-core`` that
    session cannot report back to the channel it came from. Conditional because the
    condition is real -- a narrowed third-party server costs that server and not the
    channel -- and a reader who only ever sees the declared value recovers neither.
    Which reaches carry the cost at all is the projection's judgement
    (``McpAbility.costs_control_plane``), not this report's.

    Nothing is authored per harness, so a newly onboarded backend is covered the moment
    its ``PROJECTIONS`` entry exists.

    Reports only, and appends NO entry to ``issues``, on the terms every row in this
    neighbourhood sets: a declared difference between harnesses is what the
    declaration is FOR, and failing doctor's exit code on one would make choosing a
    harness read as a fault. It declares; it changes nothing and gates nothing.

    Asks ``agent_sdk`` rather than reading ``providers/mirrors`` here: the declaration
    lives below the boundary and reaching it from a consumer would take an edge the
    agent-sdk-boundary gate refuses. Both surfaces of this card, and what each owes a
    reader, are written down once in ``providers/mirrors/README.md`` ("Fill the card").
    """
    from kiro_crew.acp_backends import selectable_backend_values

    # circular import -- see agent_sdk.backend_mcp_ability._declaration. Every other
    # backend question in this module is asked the same way and for the same reason.
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
        # The DECLARATION's own words, not a second English gloss of them. The panel
        # already phrases these for a reader who wants prose, in every shipped language; a
        # rival wording here would be one declaration with two voices, and the one
        # nobody could review. Scrubbed because a plugin-registered backend authors its
        # own values.
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
        # What the reach COSTS, once, for the one value whose consequence an operator
        # meets by accident -- and naming the harnesses, because a chooser cannot act
        # on a warning that does not say where it holds.
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
)


def _doctor_mcp_gateway_daemon(issues: list[str]) -> None:
    """Report the MCP gateway daemon's code revision next to this one.

    The daemon pools MCP backends across sessions and is a separate process
    from the gateway. One that outlived a code change keeps handing out
    backends built from the old checkout, and the symptom is remote from the
    cause: a directive tool reports success while the gateway logs
    ``not_derivable``. This line puts the two revisions side by side and names
    the command that replaces the daemon. A mismatch IS an issue: nothing about
    it is a valid configuration choice.
    """
    try:
        from kiro_crew.code_fingerprint import code_fingerprint
        from kiro_crew.mcp_gateway.daemon_control import describe_daemon

        info = describe_daemon()
    except Exception:
        return
    if info is None:
        print("  mcp gateway daemon: ⏹ not running (pooling off, or no session has started one)")
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
