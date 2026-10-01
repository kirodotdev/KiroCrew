"""What an agent spec may auto-approve, under the governance ceiling.

``allowedTools`` and a server's ``autoApprove`` are the two paths that never reach
Kiro Crew's PreToolUse gate, so every writer filters both through the ceiling here:
:func:`_may_auto_approve` is the one predicate, :func:`_apply_allowed_tools_ceiling`
the list filter every template-derived spec inherits, and :func:`final_ceiling_pass`
the rebuild's last pass over the assembled list. Withholding a grant is a permission
decision, so each filter leaves the same ``mcp_auto_approve_withheld`` SEL record.

KAS reads auto-approval as a ``permissions`` block derived from the filtered
``allowedTools``; :func:`_write_derived_permissions` is its one version-gated writer.
"""

from __future__ import annotations

from typing import Any, Mapping

from kiro_crew import agent as agent_mod
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.platform.governance import may_skip_gate_now, strip_ungoverned_auto_approve


def _entry_is_the_declared_server(entry: object, spec: Mapping[str, Any]) -> bool:
    """Whether ``entry`` is still the server whose spec declared its verbs.

    A declaration names a server BY NAME, in a file the user owns and can repoint.
    The verbs were declared for the transport the spec describes, so an entry
    carrying another one is another server and inherits nothing.
    """
    if not isinstance(entry, dict):
        return False
    if "invocation_fn" in spec:
        try:
            command, args = spec["invocation_fn"]()
        except Exception:  # noqa: BLE001 — an unresolvable invocation declares nothing
            return False
    else:
        command, args = spec.get("command"), spec.get("args")
    if command and entry.get("command") != command:
        return False
    return args is None or list(entry.get("args") or []) == list(args)


def declared_auto_approve(emitted: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    """Per server, the ``autoApprove`` verbs its own spec DECLARES.

    The governance floor drops a verb nothing declared. The managed registry and the
    edition's contribution are the two sources that may declare one, and both live
    here, so the lookup does too. ``emitted`` is the map about to be written and is
    required: a name whose entry was repointed declares nothing.
    """
    declaring = (*agent_mod._MANAGED_MCP_SERVERS.items(), *agent_mod._extra_mcp_servers().items())
    return {
        n: tuple(s["autoApprove"])
        for n, s in declaring
        if isinstance(s, dict)
        and isinstance(s.get("autoApprove"), list)
        and s["autoApprove"]
        and _entry_is_the_declared_server(emitted.get(n), s)
    }


def _strip_ungoverned_auto_approve(servers: dict[str, Any]) -> dict[str, Any]:
    """Local alias so tests can monkeypatch one name (see governance)."""
    return dict(strip_ungoverned_auto_approve(servers))


def _write_derived_permissions(
    config: dict[str, Any], allowed_tools: object, agent_filename: str
) -> None:
    """Derive ``config["permissions"]`` from *allowed_tools*, but only if kiro-cli accepts it.

    The one version gate every generated spec writer shares. kiro-cli validates
    specs with serde ``deny_unknown_fields`` and also serves the KAS backend, so
    a release whose schema predates ``permissions`` refuses the WHOLE spec and
    falls back to broader default grants -- and cannot be the KAS relay the field
    exists for. An accepting version replaces any inherited value with the fresh
    derivation. A refusing or unknown version removes any inherited value, where
    keeping it costs the spec and withholding it costs nothing that release could
    honour. The default-spec seed guards its existing block before calling here,
    so hand-written seed input remains untouched.

    Generated conductor and worker configs start from ``build_agent_config``,
    which may carry a user override for this field; the version gate therefore
    owns both replacement and removal rather than assuming a fresh config.

    Function-local imports on a boot path, and routed through the agent-sdk
    boundary: ``drivers.acp`` is the one layer permitted to import
    ``kiro_crew.acp``.
    """
    from kiro_crew.kiro_cli import (  # noqa: PLC0415 - boot path
        installed_kiro_cli_version,
        spec_permissions_supported,
    )

    if not spec_permissions_supported(installed_kiro_cli_version()):
        config.pop("permissions", None)
        return

    from kiro_crew.agent_sdk.drivers.acp import (  # noqa: PLC0415 - boot path
        derived_agent_permissions,
    )

    config["permissions"] = derived_agent_permissions(allowed_tools, agent_filename)


def _seed_kas_permissions(config: dict[str, Any]) -> None:
    """Give the spec a KAS ``permissions`` block if it has none. Never edit one.

    **Only when the installed kiro-cli accepts the field.** kiro-cli validates
    agent specs with serde ``deny_unknown_fields`` and serves the KAS backend as
    well as its own, so one binary decides both questions: a release whose schema
    predates ``permissions`` refuses the ENTIRE spec, drops the agent from its
    table, and leaves every Kiro Crew MCP server absent from the session -- and
    that same release cannot be the KAS relay the field exists for. Withholding
    it there gives up nothing that release could have honoured, while writing it
    gives up the whole spec. An UNKNOWN version (no pinned binary, a refused
    spawn, unparseable output) withholds too: a wrong guess costs the whole spec.

    A block already on disk is never removed here, whatever the version says --
    the same seed-never-refresh rule below. A spec an older release already
    refuses is repaired by ``kirocrew setup --agent-only --clean``, which
    rebuilds from defaults and, through this gate, leaves the key out.

    Two things ride on this field, and the second is the surprising one:

    1. It is how the auto-approve list reaches the KAS backend at all, since
       ``allowedTools`` is a kiro-cli-only field there.
    2. Its mere PRESENCE is what makes KAS load this file. KAS classifies a JSON
       agent profile carrying kiro-cli-only fields and no KAS field as written
       for the other runtime and skips it outright — so without ``permissions``
       the agent is not among the modes KAS advertises, and anything that asks
       for it by name (a resumed session, notably) fails to find it.

    That second point is why an empty policy is still written when nothing
    qualifies for auto-approve: ``{"rules": []}`` says "no tool is
    pre-approved", which is both true and enough to keep the file loadable.
    Dropping the key instead would silently un-register the agent. (The wire
    projection makes the opposite choice and omits the field entirely — there,
    presence buys nothing and absence is the honest report.)

    **Seed, never refresh.** Once the key exists it belongs to whoever edits the
    file, and this function does not touch it again. The obvious alternative —
    recognising Crew's own output by its shape and regenerating that — was
    written first and removed: the shapes overlap (a blanket ``allow`` is exactly
    what a user writes too), so the rule that keeps a derived policy current is
    the same rule that silently overwrites a hand-written one, and losing a
    user's policy is the worse failure. What it costs is staleness: a policy
    written before ``allowedTools`` changed keeps describing the old list. That
    is bounded, because the wire projection derives afresh from ``allowedTools``
    on every session and outranks the file — the block on disk is what applies
    when Crew is NOT injecting an agent.
    """
    if config.get("permissions") is not None:
        return

    # The version gate and the derive live in the shared writer; the guard above
    # is what is specific to seeding -- a hand-written block is never edited.
    _write_derived_permissions(config, config.get("allowedTools"), AGENT_FILENAME)


def _may_auto_approve(ref: str) -> bool:
    """Whether ``ref`` may go on an auto-approve list, per the governance ceiling.

    One-line delegate on purpose: the decision AND the ceiling resolution both
    live in ``platform.governance`` so the five writers of an ``allowedTools``
    list cannot drift apart. Kept as a named local so it is monkeypatchable in
    tests without reaching into another module's namespace.
    """
    return may_skip_gate_now(ref)


def _apply_allowed_tools_ceiling(config: dict, *, source: str) -> None:
    """Filter ``config["allowedTools"]`` through the governance ceiling, in place.

    ``allowedTools`` is the ONE path that never reaches the PreToolUse gate, so
    every entry on it must be approved by :func:`_may_auto_approve`. This runs
    inside :func:`build_agent_config` so every installer that derives a spec
    from the template inherits the filter. A filter living only in
    ``rebuild_agent_config``'s final pass would cover only ``kirocrew.json``,
    letting an installer such as ``_install_research_agent`` ship the
    template's ``fs_read``/``code``/``glob``/``grep`` grants verbatim.

    A withheld ref stays MOUNTED (``tools`` is untouched — mounting a tool is
    not auto-approving it); its calls go through the gate, where the
    per-argument rule applies. Non-string entries (a hand-edited config) are
    dropped: they are not valid tool refs and would crash the predicate.

    Withholding a grant is a permission DECISION, so it leaves the same
    ``mcp_auto_approve_withheld`` SEL record every other ``allowedTools``
    writer emits — best-effort, never raising, so an audit failure cannot
    break a build or an install.
    """
    allowed = config.get("allowedTools")
    if not isinstance(allowed, list):
        return
    kept: list[str] = []
    withheld: list[str] = []
    for ref in allowed:
        if not isinstance(ref, str):
            continue
        (kept if _may_auto_approve(ref) else withheld).append(ref)
    config["allowedTools"] = kept
    if withheld:
        try:
            agent_mod.sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source=source,
                resources=(
                    f"{', '.join(withheld)} mounted without auto-approve "
                    "(governance ceiling); calls go through the approval gate"
                ),
            )
        except Exception:  # noqa: BLE001 — the audit must not break the build
            agent_mod.logger.debug("SEL audit unavailable for withheld auto-approve", exc_info=True)


def _ceiling_filtered_spec(ref: str, spec: dict[str, Any], *, audit: bool = True) -> dict[str, Any]:
    """An app's MCP spec with a ceiling-governed ``autoApprove`` removed.

    ``autoApprove`` is a SECOND way to reach the same exemption ``allowedTools``
    grants, and a more direct one: kiro-cli approves an autoApproved MCP tool
    locally and emits no permission request, so ``hooks.on_tool_call`` — the deny
    floor, the sensitive-path check, the governance ceiling — never runs for it.
    ``agent.py``'s managed-server block states the rule for our own servers
    ("DELIBERATELY NO autoApprove KEY, and none may ever be added"); this applies
    it to app-contributed ones, which were copied verbatim.

    That verbatim copy meant the grant was declared by an app MANIFEST — content
    that can come from outside this repo — rather than by Kiro Crew or the user.
    An app could hand itself a permanent gate exemption by adding three lines to
    its own JSON.

    Only the key is dropped, never the server: the app keeps its tools, they
    simply go through the approval gate, which is where a per-tool ceiling rule is
    actually applied. Unchanged on an ungoverned host, since ``may_skip_gate``
    permits everything when there is no ceiling.
    """
    if "autoApprove" not in spec:
        return spec
    if _may_auto_approve(f"@{mcp_server_alias(ref)}"):
        return spec
    spec.pop("autoApprove", None)
    if not audit:
        return spec
    agent_mod.logger.info(
        "Dropped autoApprove from app MCP server %s: the governance ceiling "
        "constrains it, so its tools go through the approval gate",
        ref,
    )
    # Revoking a gate exemption is a permission DECISION. This fallback drops the
    # grant before the final sanitizer can observe it, so without an event here
    # this would be the one withhold path with no audit trail. Mirror the
    # allowedTools writers' SEL event. Best-effort; never break a rebuild.
    try:
        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_auto_approve_withheld",
            outcome="ok",
            source="_ceiling_filtered_spec",
            resources=(
                f"@{mcp_server_alias(ref)} autoApprove removed (governance ceiling); "
                "calls go through the approval gate"
            ),
        )
    except Exception:  # noqa: BLE001 — audit must not break the filter
        agent_mod.logger.debug("SEL audit unavailable for app autoApprove strip", exc_info=True)
    return spec


def final_ceiling_pass(config: dict) -> None:
    """Run the LAST governance pass over the assembled ``allowedTools`` list."""
    # LAST governance pass over the auto-approve LIST itself. The writers above
    # apply the ceiling to entries THEY add, but a builtin auto-approve (fs_read,
    # execute_bash, …) arrives straight from the agent TEMPLATE into
    # `allowedTools` and no writer ever re-touches it — so a `filesystem.read`
    # ceiling would leave `fs_read` on the blanket auto-approve list and kiro-cli
    # would approve every read WITHOUT reaching the PreToolUse gate that carries
    # the ceiling. Filter the whole assembled list through the one predicate: a
    # governed builtin (or `@server`) loses its blanket grant and its calls go
    # through the gate, where the per-argument rule actually applies; anything the
    # ceiling is silent about is kept (the predicate returns True), and an
    # ungoverned host keeps everything. `tools` is deliberately left intact —
    # mounting a tool is not auto-approving it.
    allowed = config.get("allowedTools")
    if isinstance(allowed, list):
        kept: list[str] = []
        withheld: list[str] = []
        for ref in allowed:
            if not isinstance(ref, str):
                # A malformed non-string entry (e.g. a hand-edited config with
                # `allowedTools: [1]`) would crash may_skip_gate's
                # ref.startswith() and fault the whole rebuild. It is not a valid
                # tool ref, so drop it entirely rather than keep or audit it.
                continue
            (kept if _may_auto_approve(ref) else withheld).append(ref)
        config["allowedTools"] = kept
        if withheld:
            # Withholding a grant is a permission DECISION, and this final pass is
            # the ONLY place a builtin that arrived straight from the shipped
            # template (fs_read, code, …) loses its blanket auto-approve. The
            # per-writer paths already emit this SEL event for the grants they
            # touch; a silent drop here would leave an operator no record of why a
            # template tool now prompts. Same operation name, so it lands in one
            # feed. Auditing must never fail the rebuild.
            try:
                agent_mod.sel().log_api_access(
                    caller="system",
                    operation="mcp_auto_approve_withheld",
                    outcome="ok",
                    source="rebuild_agent_config",
                    resources=(
                        f"{', '.join(withheld)} mounted without auto-approve "
                        "(governance ceiling); calls go through the approval gate"
                    ),
                )
            except Exception:  # noqa: BLE001 — the audit must not break a rebuild
                agent_mod.logger.debug(
                    "SEL audit unavailable for withheld auto-approve", exc_info=True
                )


def _filter_auto_approve(refs: tuple[str, ...], *, source: str) -> list[str]:
    """Filter a conductor's intended grants through the governance ceiling.

    ``allowedTools`` is the ONE path that never reaches the PreToolUse gate, so
    every grant is filtered through the ceiling first — the same predicate
    ``rebuild_agent_config`` applies to the primary spec's assembled list, and the
    entry point ``may_skip_gate_now`` exists precisely so a new writer cannot
    re-open the bypass by restating a literal. A governed ref stays MOUNTED (it is
    still in ``tools``); it just prompts, and the gate then applies the ceiling's
    per-tool rule with the real arguments.

    Withholding a grant is a permission DECISION, and every other writer of an
    ``allowedTools`` list emits the same event for it — see
    ``strip_ungoverned_auto_approve``, whose comment names a silent pop as the one
    withhold path with no audit trail. Filtering silently here would make this the
    same path: on a governed host a ref loses its grant and the operator has no
    record of why the conductor now prompts. Same operation name so every
    installer's withholds land in one feed, and the audit must never break an
    install.

    A helper rather than three copies because the copies are what drift: the
    per-installer difference is ``source`` alone, and the three conductor specs'
    tests pin that the emitted list is unchanged by the extraction.
    """
    granted: list[str] = []
    withheld: list[str] = []
    for ref in refs:
        (granted if _may_auto_approve(ref) else withheld).append(ref)
    if withheld:
        try:
            agent_mod.sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source=source,
                resources=(
                    f"{', '.join(withheld)} mounted without auto-approve "
                    "(governance ceiling); calls go through the approval gate"
                ),
            )
        except Exception:  # noqa: BLE001 — the audit must not break the install
            agent_mod.logger.debug("SEL audit unavailable for withheld auto-approve", exc_info=True)
    return granted
