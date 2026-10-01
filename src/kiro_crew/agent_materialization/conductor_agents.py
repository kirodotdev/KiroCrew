"""The conductor agent specs: one shape, four charters.

Four specs share one shape -- derived from the default template, no file-writing tool,
``execute_bash`` mounted but never auto-approved, and every MCP server mounted whole
but auto-approved verb by verb -- and differ in charter: the goal conductor (and its
deprecated ledger alias, which emits the same spec under the old name), the pipeline
conductor and the security conductor. Their prompts and the grant tuples each may
auto-approve are spec bytes and stay in :mod:`kiro_crew.agent`, beside the invariant
each is judged by; the installers here read them from there.
"""

from __future__ import annotations

from typing import Any

from kiro_crew import agent as agent_mod
from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME as _CONDUCTOR_AGENT_FILENAME
from kiro_crew.agent_files import (
    LEDGER_CONDUCTOR_AGENT_FILENAME as _LEDGER_CONDUCTOR_AGENT_FILENAME,
)
from kiro_crew.agent_files import (
    PIPELINE_CONDUCTOR_AGENT_FILENAME as _PIPELINE_CONDUCTOR_AGENT_FILENAME,
)
from kiro_crew.agent_files import (
    SECURITY_CONDUCTOR_AGENT_FILENAME as _SECURITY_CONDUCTOR_AGENT_FILENAME,
)
from kiro_crew.agent_materialization import auto_approve, managed_mcp


def _conductor_mcp_servers(config: dict[str, Any], *, work: bool = False) -> dict[str, Any]:
    """The narrowed ``mcpServers`` map every conductor spec carries.

    ``kirocrew-core`` is inherited from ``build_agent_config``; ``kirocrew-dashboard``
    is hand-built here because it is the opt-in per-agent set (folder +
    session-control tools) that neither spec-writing loop emits, and a conductor
    granting it IS the explicit per-agent assignment that set requires.

    The two fields that are easy to forget are why this is a helper rather than
    three copies: without ``"type": "registry"`` a registry-mode client silently
    DROPS the entry, so the granted session-control tools never launch and the
    conductor's whole dispatch/patrol purpose is dead with no local error; and
    without the ``KIROCREW_HOME`` pin the shim reads the DEFAULT data home while the
    gateway runs under an override, so session control would act on a different
    session store than the one it reports on. Both helpers return empty on a
    default install, so the emitted spec is unchanged there.

    ``work`` mounts ``kirocrew-work``, and ``_conductor_spec`` is what passes it —
    so ``kirocrew-conductor`` and its ``kirocrew-ledger-conductor`` alias carry the
    entry and the pipeline and security conductors do not. It stays a parameter
    rather than becoming unconditional because those two specs are what the
    isolation is now for: their children report through their own skills' scripts,
    and a mount they never call is surface their charters cannot account for.
    """
    mcp = config.get("mcpServers", {}) or {}
    core_entry = mcp.get("kirocrew-core")
    narrowed: dict[str, Any] = {}
    if core_entry:
        narrowed["kirocrew-core"] = core_entry
    dash_cmd, dash_args = agent_mod._kirocrew_mcp_invocation("mcp-dashboard")
    dash_entry: dict[str, Any] = {"command": dash_cmd, "args": dash_args}
    if managed_mcp._mcp_registry_mode():
        dash_entry["type"] = managed_mcp._MCP_REGISTRY_TYPE
    dash_env = managed_mcp._managed_mcp_env()
    if dash_env:
        dash_entry["env"] = dash_env
    narrowed["kirocrew-dashboard"] = dash_entry
    if work:
        narrowed["kirocrew-work"] = managed_mcp._managed_opt_in_entry("mcp-work")
    return narrowed


def _conductor_spec(*, name: str, description: str, filename: str, source: str) -> dict[str, Any]:
    """The conductor spec, emitted under *name* — one body, two filenames.

    ``kirocrew-conductor`` and its deprecated alias
    ``kirocrew-ledger-conductor`` differ in ``name`` and ``description`` and in
    nothing else, and that is enforced here rather than trusted: two installers
    that each hand-built the same list are exactly where a grant lands on one
    spec and not the other, and the alias exists so an in-flight session keeps
    working — an alias that emits a DIFFERENT spec silently changes what that
    session can do. ``filename`` and ``source`` are the two per-installer
    values, and neither reaches the emitted JSON: ``filename`` names the KAS
    ``agent_id`` used in the derive's log line, and ``source`` names the
    installer in the withheld-grant audit event.

    The charter, and why each property is a property of the SPEC rather than of
    the prompt. Derived from the kirocrew agent (resolved MCP invocations,
    security hooks) and narrowed to what conducting needs: session control,
    core tools, the work ledger, and shell for the bundled acceptance
    evaluator — and **no tool that can write a file**, not ``fs_write`` and not
    ``code`` either, which governance classes under ``filesystem.write``
    because it writes files and can shell out. That is what makes "never does a
    work item's work itself" true against the tool list and not just against
    the prose.

    ``@kirocrew-core``, ``@kirocrew-dashboard`` and ``@kirocrew-work`` are all
    MOUNTED whole but auto-approved only verb by verb, via
    ``_CONDUCTOR_CORE_GRANTS``, ``_CONDUCTOR_DASHBOARD_GRANTS`` and
    ``_LEDGER_CONDUCTOR_WORK_GRANTS`` (see their comments for the per-verb
    reasoning). Both backends honour a per-tool reference, so the narrowing is
    real rather than cosmetic: kiro-cli's ``is_tool_in_allowlist`` checks
    ``@server`` and then ``@server/<tool>``, and ``allowed_tools_to_permissions``
    maps the same entry to an exact KAS ``server/tool`` resource match.

    The line the split follows is stated as an invariant on those tuples, not as
    a taste call: a granted verb may CREATE or READ, never MUTATE something that
    already exists and is not the conductor's own. Reads and creates are granted
    because the patrol loop is nudge-driven and must not block on an approval
    nobody is there to give. ``session_stop`` (discards a peer's in-flight turn),
    ``session_send`` (runs text as a peer's turn) and ``chat_folder_move_session``
    (writes a peer session's ``folder_id``) are withheld, because the conductor
    ingests untrusted content by design and the server-side gates bound which
    target is reachable, not what is done to it. ``work_report`` is withheld on
    the same rule: it writes into a PARENT's record, across a dispatch
    relationship.

    ``execute_bash`` is withheld for a different reason that is worth keeping
    distinct: ``allowedTools`` is name-scoped with no argument matching, so
    trusting the one bundled script cannot be told apart from trusting arbitrary
    shell. There is no per-argument form of that grant the way there is a
    per-tool form of the MCP one.

    The operating procedure ships as the ``goal-conductor`` builtin skill, NOT
    ``conductor``: that directory name belonged to the delegation skill the
    retired ``agent.conductor_skill`` flag generated, and install cleanup still
    removes a ``<skills>/conductor/SKILL.md`` whose bytes the generator wrote on
    old installs. Sharing the name would let that cleanup erase the packaged
    skill.
    """
    config = agent_mod.build_agent_config()
    config["name"] = name
    config["description"] = description
    config["prompt"] = agent_mod._CONDUCTOR_SYSTEM_PROMPT
    config["tools"] = [
        "execute_bash",
        "fs_read",
        # ``web_fetch`` serves the charter's own worked example (reading an issue
        # list during triage). Deliberately NOT mounted: ``web_search`` (nothing
        # names it), ``grep``/``glob`` (``fs_read`` covers every read the charter
        # describes), and above all ``code`` — governance classes it under
        # ``filesystem.write`` because it "writes files AND can shell out", so
        # mounting it would make this spec's whole no-write property false.
        # An unused grant is surface the charter cannot account for.
        "web_fetch",
        "session",
        "report",
        # Load-bearing, not decoration: with MCP Tool Search active the
        # session-control specs are deferred, so the conductor cannot reach
        # ``session_create`` / ``chat_folder_*`` / ``monitor_start`` at all until
        # it loads them by id. Named in the prompt's tool inventory for that
        # reason, and auto-approved below so the load itself never prompts.
        "tool_search",
        "@kirocrew-core",
        "@kirocrew-dashboard",
        # Mounted whole, auto-approved verb by verb below: the worker half lives
        # on this server too, and a conductor has no reason to auto-approve a
        # tool whose only answer to it is a refusal.
        "@kirocrew-work",
    ]
    # ``allowedTools`` is the ONE path that never reaches the PreToolUse gate, so
    # every grant is filtered through the governance ceiling first — the same
    # predicate ``rebuild_agent_config`` applies to the primary spec's assembled
    # list, and the entry point ``may_skip_gate_now`` exists precisely so a new
    # writer cannot re-open the bypass by restating a literal. A governed ref
    # stays MOUNTED (it is still in ``tools``); it just prompts, and the gate
    # then applies the ceiling's per-tool rule with the real arguments.
    # ``tool_search`` is granted on the same rule as the dashboard verbs below:
    # it only READS a tool spec into context — it cannot act, touch workspace
    # state, or reach the machine — and it is bounded by the mounted catalog.
    # Withholding it made the ONE call that unblocks every deferred
    # session-control tool prompt first, so an unattended patrol cycle stalled
    # on the load rather than on the work. ``execute_bash`` stays withheld for
    # the reason recorded above it: ``allowedTools`` has no argument matching,
    # so trusting the one bundled script cannot be told apart from trusting
    # arbitrary shell.
    config["allowedTools"] = auto_approve._filter_auto_approve(
        (
            "session",
            "report",
            "tool_search",
            *agent_mod._CONDUCTOR_CORE_GRANTS,
            *agent_mod._CONDUCTOR_DASHBOARD_GRANTS,
            *agent_mod._LEDGER_CONDUCTOR_WORK_GRANTS,
        ),
        source=source,
    )
    config["mcpServers"] = _conductor_mcp_servers(config, work=True)
    # Derive the KAS policy from the FILTERED grant list instead of restating it
    # as a literal: the rules come out byte-identical, a later edit to
    # ``allowedTools`` carries through, and a ceiling that strips a grant strips
    # its KAS rule with it (a hand-written ``kirocrew-core/*`` allow would have
    # survived the filter on the KAS backend). The shared writer version-gates it.
    auto_approve._write_derived_permissions(config, config["allowedTools"], filename)
    return config


def _install_conductor_agent() -> None:
    """Generate and install the kirocrew-conductor agent config.

    THE conductor: it owns a goal, and it tracks that goal in the work ledger.
    The ledger flow shipped on a separate ``kirocrew-ledger-conductor`` spec
    first so that migrating every existing conductor user was a decision and not
    a side effect, and the decision has now been taken — the flow ran end to end
    (7 items across 3 rounds, each acceptance settled by the evaluator rather
    than by a transcript read), so it is what this spec emits.
    ``kirocrew-ledger-conductor`` stays for one release as a deprecated alias
    emitting this same spec under its old name, because an in-flight session
    names its agent by string and a deleted name is a broken session.

    Every property ``_conductor_spec`` argues for holds here, and the swap did
    not relax one of them: no file-writing tool at all, ``@kirocrew-core`` /
    ``@kirocrew-dashboard`` / ``@kirocrew-work`` mounted whole and auto-approved
    verb by verb, ``execute_bash`` mounted and never auto-approved, and the KAS
    policy derived from the FILTERED grant list rather than restated.
    """
    config = _conductor_spec(
        name="kirocrew-conductor",
        description=(
            "Owns a long-horizon goal and tracks it in the work ledger: "
            "decomposes it into items, dispatches one session per item, reads "
            "their reported status as data rather than as a transcript, "
            "verifies claims with the acceptance evaluator, and decides each "
            "next round. Never does the work itself."
        ),
        filename=_CONDUCTOR_AGENT_FILENAME,
        source="_install_conductor_agent",
    )
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _CONDUCTOR_AGENT_FILENAME
    agent_mod._atomic_json_write(path, config)
    agent_mod.logger.info("Installed conductor agent config: %s", path)


#: Deprecated agent-spec name -> the current spec that replaced it.
#:
#: One row per installed alias. ``kirocrew doctor`` reads this table to warn
#: any config surface that persists an agent name -- a cron job, a crew
#: binding, a chat slot -- while the old name still resolves, so deleting the
#: alias later breaks nobody silently with ``Mode not found`` at dispatch
#: time. A row is deleted together with its alias installer, never before:
#: the doctor notice is the precondition for the deletion (see
#: ``docs/request-for-change/rfc-conductor-work-ledger.md``, "What retired
#: means for the name").
DEPRECATED_AGENT_SPECS: dict[str, str] = {
    "kirocrew-ledger-conductor": "kirocrew-conductor",
}


def _install_ledger_conductor_agent() -> None:
    """Install the deprecated ``kirocrew-ledger-conductor`` alias spec.

    The ledger flow is ``kirocrew-conductor`` now, and this name is kept for one
    release because it is a public, user-facing string: it is what a running
    session records as its agent, what a seed prompt names for a second-level
    conductor, and what an operator typed into a cron. Deleting it in the same
    release as the swap would break those in place, so the name still resolves
    and emits the SAME spec — see ``_conductor_spec``, which both installers
    call so the two cannot drift.

    Removed next release; nothing new should name it.
    """
    config = _conductor_spec(
        name="kirocrew-ledger-conductor",
        description=(
            "Deprecated alias of kirocrew-conductor (removed next release). "
            "Owns a long-horizon goal and tracks it in the work ledger: "
            "decomposes it into items, dispatches one session per item, reads "
            "their reported status as data rather than as a transcript, "
            "verifies claims with the acceptance evaluator, and decides each "
            "next round. Never does the work itself."
        ),
        filename=_LEDGER_CONDUCTOR_AGENT_FILENAME,
        source="_install_ledger_conductor_agent",
    )
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _LEDGER_CONDUCTOR_AGENT_FILENAME
    agent_mod._atomic_json_write(path, config)
    agent_mod.logger.info("Installed ledger-conductor alias agent config: %s", path)


def _install_pipeline_conductor_agent() -> None:
    """Generate and install the kirocrew-pipeline-conductor agent config.

    Follows ``_install_conductor_agent`` above deliberately — one standalone
    installer per generated agent is the file's established pattern — and
    keeps every property that installer's docstring argues for: derived from
    the kirocrew agent, **no dedicated file-writing tool** (neither ``fs_write``
    nor ``code``), ``@kirocrew-dashboard`` mounted whole but auto-approved only
    verb by verb, ``execute_bash`` mounted but never auto-approved
    (``allowedTools`` has no argument matching, so trusting the two bundled
    skill scripts cannot be told apart from trusting arbitrary shell), and the
    KAS policy derived from the FILTERED grant list. Where the two agents
    differ is charter, not mechanics: this one supervises a repository
    pipeline's worker fleet (probe / verify / intervene / adjudicate / govern)
    per the ``pipeline-conductor`` builtin skill, rather than decomposing a
    free-form goal.
    """
    config = agent_mod.build_agent_config()
    config["name"] = "kirocrew-pipeline-conductor"
    config["description"] = (
        "Runs one repository pipeline as a supervised fleet: picks up queued "
        "work items, dispatches one worker session per item, probes and "
        "verifies them, intervenes on stalls, adjudicates blocked items, and "
        "governs host resources and per-item credit budgets. Never does a "
        "work item's work itself."
    )
    config["prompt"] = agent_mod._PIPELINE_CONDUCTOR_SYSTEM_PROMPT
    config["tools"] = [
        "execute_bash",
        "fs_read",
        "web_fetch",
        "session",
        "report",
        "tool_search",
        "@kirocrew-core",
        "@kirocrew-dashboard",
    ]
    config["allowedTools"] = auto_approve._filter_auto_approve(
        (
            "session",
            "report",
            "tool_search",
            *agent_mod._PIPELINE_CONDUCTOR_CORE_GRANTS,
            *agent_mod._PIPELINE_CONDUCTOR_DASHBOARD_GRANTS,
        ),
        source="_install_pipeline_conductor_agent",
    )
    config["mcpServers"] = _conductor_mcp_servers(config)
    # Same derive-don't-restate rationale as the conductor above; the shared
    # writer version-gates it.
    auto_approve._write_derived_permissions(
        config, config["allowedTools"], _PIPELINE_CONDUCTOR_AGENT_FILENAME
    )
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _PIPELINE_CONDUCTOR_AGENT_FILENAME
    agent_mod._atomic_json_write(path, config)
    agent_mod.logger.info("Installed pipeline-conductor agent config: %s", path)


def _install_security_conductor_agent() -> None:
    """Generate and install the kirocrew-security-conductor agent config.

    A third standalone installer, following ``_install_pipeline_conductor_agent``
    above for the same reason that one follows ``_install_conductor_agent`` — one
    installer per generated agent is this file's established pattern — and
    keeping every property those docstrings argue for: derived from the kirocrew
    agent, **no dedicated file-writing tool** (neither ``fs_write`` nor ``code``,
    which governance classes under ``filesystem.write``), ``@kirocrew-core`` and
    ``@kirocrew-dashboard`` mounted whole but auto-approved only verb by verb,
    ``execute_bash`` mounted but never auto-approved (``allowedTools`` has no
    argument matching, so trusting the skill's bundled scripts cannot be told
    apart from trusting arbitrary shell), and the KAS policy derived from the
    FILTERED grant list.

    Those properties carry more weight here than on either sibling, which is the
    charter difference: this agent's own children probe a security fence, so what
    it ingests on an unattended cycle is hostile by assumption. "Never touches the
    target itself" therefore has to hold as a spec property when nobody is at the
    keyboard, and the two human gates the prompt names (active testing beyond a
    local proof of concept, and any fixer dispatch) are what the withheld
    ``session_send`` / ``spawn_run`` / ``execute_bash`` grants make expensive to
    skip rather than merely discouraged.

    The grant tuples are the pipeline conductor's, REUSED rather than copied. The
    derivation the goal conductor's comment describes — the union of this prompt's
    own "Your tools:" inventory and the skill's real call sites, filtered to what
    registers on each server — lands on exactly that set here: patrol lifecycle,
    reads, the agent's own ledger, and owner reporting, with no ``select_crew``
    (this conductor routes nothing). A third byte-identical copy would be
    duplication whose later divergence nothing could detect, and reuse across
    agents is already this file's practice, and ``_filter_auto_approve`` plus
    ``_conductor_mcp_servers`` are the same argument applied one level down.

    ``@kirocrew-work`` is deliberately NOT mounted, matching
    ``kirocrew-pipeline-conductor``: the work-ledger flow belongs to
    ``kirocrew-conductor`` (``_conductor_spec``), and a conductor gaining tools that
    only make sense under a different procedure is a change to its charter rather
    than an addition to it. This agent's children report findings through the
    ``security-conductor`` skill's ledger scripts, not the work ledger, so the
    mount would grant a flow whose procedure this conductor does not run.
    """
    config = agent_mod.build_agent_config()
    config["name"] = "kirocrew-security-conductor"
    config["description"] = (
        "Runs one security audit as a supervised fleet: decomposes a target "
        "into attack surfaces, dispatches one auditor session per surface and "
        "an independent verifier per finding, adjudicates severity, and gates "
        "any fix behind a human yes. Never touches the target itself."
    )
    config["prompt"] = agent_mod._SECURITY_CONDUCTOR_SYSTEM_PROMPT
    config["tools"] = [
        "execute_bash",
        "fs_read",
        "web_fetch",
        "session",
        "report",
        "tool_search",
        "@kirocrew-core",
        "@kirocrew-dashboard",
    ]
    config["allowedTools"] = auto_approve._filter_auto_approve(
        (
            "session",
            "report",
            "tool_search",
            *agent_mod._PIPELINE_CONDUCTOR_CORE_GRANTS,
            *agent_mod._SECURITY_CONDUCTOR_DASHBOARD_GRANTS,
        ),
        source="_install_security_conductor_agent",
    )
    config["mcpServers"] = _conductor_mcp_servers(config)
    # Derived from the FILTERED grant list rather than restated, so a ceiling
    # that strips a grant strips its KAS rule with it; the shared writer
    # version-gates it.
    auto_approve._write_derived_permissions(
        config, config["allowedTools"], _SECURITY_CONDUCTOR_AGENT_FILENAME
    )
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _SECURITY_CONDUCTOR_AGENT_FILENAME
    agent_mod._atomic_json_write(path, config)
    agent_mod.logger.info("Installed security-conductor agent config: %s", path)
