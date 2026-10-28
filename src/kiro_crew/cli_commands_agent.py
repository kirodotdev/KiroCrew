"""``kirocrew agent``: list, create, update and delete crew agents, and reset a model pin.

``_handle_agent`` checks names, labels, ``--model`` (``_agent_model_or_exit``) and
``--memory-store`` (``_memory_store_or_exit``) against a config snapshot. ``create`` and
``update`` persist through ``memory_stores.persist_member_config``; ``delete`` writes
through ``cli_commands_workspace._locked_config_write`` and re-checks its precondition
inside the lock. ``_agent_reset_model`` serves ``agent reset-model``.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import sys

from kiro_crew import crew_teams, model_registry
from kiro_crew.agent import reset_agent_model
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.cli_commands_workspace import _CliConflict, _locked_config_write
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, coerce_dict_section
from kiro_crew.config.sections import normalize_agent_model
from kiro_crew.external_text import external_text_requires_redaction
from kiro_crew.member_memory_auth import require_member_memory_creation
from kiro_crew.member_workspace import (
    MemberWorkspaceUnavailable,
    provision_member_workspace,
    retire_unpublished_workspace,
)
from kiro_crew.members import MemberNameError, check_crew_label, key_new_crew, validate_member_name
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    UnknownMemoryStore,
    memory_store_binding_defect,
    memory_store_namespace_lock,
    persist_member_config,
    provision_member_memory,
    retire_unpublished_allocation,
)
from kiro_crew.terminal_safe import safe_terminal_line
from kiro_crew.validation import _MODEL_NAME_RE, MAX_SHORT_STRING, TEMPLATE_NAME_RE


def _kiro_agent_or_exit(name: str) -> None:
    """Refuse a ``--kiro-agent`` value its pattern does not admit, naming the shape."""
    if TEMPLATE_NAME_RE.fullmatch(name):
        return
    # Read here, not at module level: the facade mirrors every module-level import
    # of its owners, and its import inventory is frozen.
    from kiro_crew.validation import TEMPLATE_NAME_HINT

    print(f"Error: invalid kiro agent name ({TEMPLATE_NAME_HINT})", file=sys.stderr)
    sys.exit(1)


def _memory_store_or_exit(raw: str) -> str:
    """Return *raw* when it may bind a crew to a memory store, else exit 1.

    The same predicate the dashboard verbs apply, so the two surfaces cannot
    persist different sets of values into one ``config.json``. Only the SHAPE is
    refused: an undeclared but well-formed name is accepted and warned about, since
    no verb here can declare a store either.

    ``repr`` on the value, not the bare string: it arrives from a shell argument
    and can carry an OSC/ANSI sequence, and this message goes to a terminal.
    """
    defect = memory_store_binding_defect(raw)
    if defect is not None:
        print(
            f"Error: memory store {raw!r} is not a usable store name ({defect}); use "
            "lowercase letters, digits and hyphens, or '' for the default store",
            file=sys.stderr,
        )
        sys.exit(1)
    return raw


def _agent_list_label(label: str) -> str:
    """*label* as one ``kirocrew agent list`` cell.

    The label is written by the dashboard, the CLI and a crewmate's own
    ``update_self``, so it is printed confined to one line with no live
    terminal controls, and a credential-shaped one is masked the way the
    dashboard roster masks it.
    """
    if not label:
        return ""
    if external_text_requires_redaction(label):
        return "(hidden: credential-shaped)"
    return safe_terminal_line(label)


def _agent_model_or_exit(raw: str, cfg: KiroCrewConfig, crew: str = "") -> str:
    """The ``--model`` value to store on an agent, or exit with the reason.

    ``""`` and ``auto`` both mean "no pin", stored as ``""`` the way the crew
    editor stores them. A CLI call is judged on shape (the same pattern
    ``cron add --model`` checks), then by the gates the crew editor runs
    before it reaches entitlement: a wrong-flavour id kiro-cli does not serve
    (``model_registry.acp_id_correction``), the backend-aware display-only
    check (``_model_rejected_reason``, judged against the same harness the crew
    editor picks with ``_pin_entitlement_backend``), and, on an
    ``acp``-namespace harness, a registered model kiro-cli has no id for
    (``_no_kiro_id_reason``, which the crew editor runs only with no live
    catalog). Entitlement itself needs a live session's advertised list, which
    this offline command does not have, so an unknown id is accepted, as the
    crew editor accepts it with no live session. *crew* names the agent being
    updated, so a crewmate that pins its own harness is judged against it.
    """
    model = normalize_agent_model(raw)
    if not model:
        return ""
    if len(model) > MAX_SHORT_STRING:
        # A plain id's segment is unbounded in the pattern, so a well-formed id can
        # be refused by the length cap alone; name the cap, not the shape it has.
        print(
            f"Error: invalid model format (expected at most {MAX_SHORT_STRING} characters)",
            file=sys.stderr,
        )
        sys.exit(1)
    if not _MODEL_NAME_RE.match(model):
        # Read here, not at module level: the facade mirrors every module-level
        # import of its owners, and its import inventory is frozen.
        from kiro_crew.validation import MODEL_NAME_HINT

        print(f"Error: invalid model format ({MODEL_NAME_HINT})", file=sys.stderr)
        sys.exit(1)
    if is_claude_code(cfg.agent.provider):
        return model
    from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable
    from kiro_crew.dashboard.handlers.agents import _pin_entitlement_backend

    try:
        backend = _pin_entitlement_backend(cfg, crew)
    except CrewmateHarnessUnavailable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    from kiro_crew.crewmate_harness import crewmate_pin_for, kiro_spelling_applies

    # A crewmate pinned to a harness with its own model ids takes those ids as
    # written; every other agent keeps the kiro-cli spelling check.
    correction = (
        model_registry.acp_id_correction(model)
        if kiro_spelling_applies(backend, crewmate_pin_for(cfg, crew) is not None)
        else None
    )
    if correction:
        print(
            f"Error: {model!r} is not a model kiro-cli serves; the registry maps that "
            f"spelling to {correction!r}. Confirm that is the model you want, or use 'auto'.",
            file=sys.stderr,
        )
        sys.exit(1)
    # Imported here, as the crew editor's own validator imports it, so the
    # dashboard modules load only when a --model value is actually judged.
    from kiro_crew.dashboard.chat_handlers import _model_rejected_reason
    from kiro_crew.dashboard.handlers.core import _no_kiro_id_reason

    reason = _model_rejected_reason(model, backend) or _no_kiro_id_reason(model, backend)
    if reason:
        print(f"Error: {reason}", file=sys.stderr)
        sys.exit(1)
    return model


def _agent_list_model(model: str) -> str:
    """*model* as one ``kirocrew agent list`` cell; an unpinned agent inherits."""
    return _agent_list_label(normalize_agent_model(model)) or "(inherit)"


def _handle_agent(args: argparse.Namespace) -> None:
    """Dispatch agent subcommands: list, create, update, delete."""

    action = getattr(args, "agent_action", None)
    cfg = KiroCrewConfig.load()

    if action == "list":
        default = cfg.default_agent
        print(
            f"{'NAME':<20} {'KIRO_AGENT':<20} {'WORKSPACE':<15} "
            f"{'MEMORY_STORE':<15} {'MODEL':<20} {'SOURCE':<12} DISPLAY_NAME"
        )
        for name, agent in cfg.agents.items():
            marker = " *" if name == default else ""
            # The loader keeps a malformed ``memory_store`` binding as written
            # (pinning it to the default would attach the member to the shared
            # store), so show the defect here instead of failing the listing.
            memory_store = agent.memory_store
            if not isinstance(memory_store, str):
                memory_store = f"(invalid: {type(memory_store).__name__})"
            print(
                f"{name + marker:<20} {agent.kiro_agent:<20} "
                f"{agent.workspace:<15} {memory_store:<15} "
                f"{_agent_list_model(agent.model):<20} "
                f"{getattr(agent, 'source', 'kirocrew'):<12} "
                f"{_agent_list_label(agent.display_name)}".rstrip()
            )

    elif action == "create":
        if external_text_requires_redaction(args.name):
            print("Error: invalid Crew Member name (credential-shaped text)", file=sys.stderr)
            sys.exit(1)
        try:
            validate_member_name(args.name)
        except MemberNameError as exc:
            print(f"Error: invalid Crew Member name ({exc})", file=sys.stderr)
            sys.exit(1)
        # Same keying as POST /api/agents.
        keyed = key_new_crew(
            args.name, (getattr(args, "display_name", None) or "").strip(), cfg.agents
        )
        if keyed.taken:
            print(f"Error: agent '{keyed.taken}' already exists", file=sys.stderr)
            sys.exit(1)
        args.name, display_name = keyed.key, keyed.display_name
        _kiro_agent_or_exit(args.kiro_agent)
        from kiro_crew.mate_config import start_template_refusal

        template_refusal = start_template_refusal(args.kiro_agent, args.name)
        if template_refusal:
            print(f"Error: {template_refusal}", file=sys.stderr)
            sys.exit(1)
        memory_store = _memory_store_or_exit(args.memory_store)
        if memory_store not in ("", DEFAULT_MEMORY_STORE):
            print(
                "Error: members receive an empty member memory store automatically",
                file=sys.stderr,
            )
            sys.exit(1)
        model = _agent_model_or_exit(getattr(args, "model", None) or "", cfg)
        from kiro_crew.mate_config import user_agent_named

        if args.kiro_agent == args.name or user_agent_named(args.name):
            # Its own file is resolved under its name; a template of that name
            # would leave it on that template (the create route refuses too).
            print(
                f"Error: a template is already named '{args.name}'; give this crew " "another name",
                file=sys.stderr,
            )
            sys.exit(1)
        # A crew that carried this name before may still be listed on a team
        # (every removal path drops it best-effort). persist_member_config
        # purges that INSIDE the registry's locked mutation, right before the
        # name is registered, on every create path; a purge that cannot be made
        # refuses the create (TeamsUnavailable, answered below).
        cfg.agents[args.name] = KiroCrewAgentConfig(
            kiro_agent=args.kiro_agent,
            workspace=args.workspace,
            memory_store=memory_store,
            display_name=display_name,
            model=model,
        )
        previous_store = cfg.agents[args.name].memory_store
        previous_member_id = cfg.agents[args.name].member_id
        previous_workspace = cfg.agents[args.name].workspace
        new_workspace = ""
        try:
            require_member_memory_creation(args.name)
            provision_member_memory(cfg, args.name)
            new_workspace = provision_member_workspace(cfg, args.name)
            persist_member_config(cfg, args.name, create=True, new_workspace=new_workspace)
        except BaseException as exc:
            retire_unpublished_workspace(
                cfg, args.name, new_workspace, previous_workspace=previous_workspace
            )
            allocated = cfg.agents[args.name].memory_store
            if allocated != previous_store:
                retire_unpublished_allocation(
                    cfg,
                    args.name,
                    allocated,
                    previous_store=previous_store,
                    previous_member_id=previous_member_id,
                )
            if isinstance(exc, crew_teams.TeamsUnavailable):
                print(
                    f"Error: cannot create agent '{args.name}': a previous crew of that "
                    f"name may still be on a team and the crew-teams store is unavailable "
                    f"({exc}); fix or remove {crew_teams.teams_path()} (an absent file "
                    "reads as no teams), then retry",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not isinstance(exc, (OSError, UnknownMemoryStore, MemberWorkspaceUnavailable)):
                raise
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        created = cfg.agents[args.name]
        if display_name:
            print(f"Created agent: {args.name} (display name: {display_name})")
        else:
            print(f"Created agent: {args.name}")
        print(f"Workspace: {created.workspace}")
        if created.model:
            print(f"Model: {created.model}")
        # The same eager copy the create route runs, so a crewmate made here
        # chats on the conductor and hands its work to its worker.
        from kiro_crew.mate_config import MateConfigError, adopt_own_file

        try:
            adopt_own_file(args.name)
        except MateConfigError as exc:
            print(
                f"Warning: {display_name or args.name} keeps running as {args.kiro_agent}: "
                f"its own file was not written ({exc})",
                file=sys.stderr,
            )

    elif action == "update":
        if args.name not in cfg.agents:
            print(f"Error: agent '{args.name}' not found", file=sys.stderr)
            sys.exit(1)
        agent = cfg.agents[args.name]
        prior_memory_store = agent.memory_store
        if args.memory_store is not None and args.memory_store != prior_memory_store:
            print("Error: a member's memory cannot be rebound or shared", file=sys.stderr)
            sys.exit(1)
        if args.kiro_agent is not None:
            _kiro_agent_or_exit(args.kiro_agent)
            agent.kiro_agent = args.kiro_agent
        if args.workspace is not None:
            agent.workspace = args.workspace
        new_model = getattr(args, "model", None)
        if new_model is not None:
            agent.model = _agent_model_or_exit(new_model, cfg, args.name)
        new_label = getattr(args, "display_name", None)
        if new_label is not None:
            try:
                agent.display_name = check_crew_label(args.name, new_label.strip(), cfg.agents)
            except MemberNameError as exc:
                print(f"Error: invalid display name ({exc})", file=sys.stderr)
                sys.exit(1)
        try:
            changed_fields = {
                field
                for field in ("kiro_agent", "workspace", "display_name", "model")
                if getattr(args, field, None) is not None
            }
            if agent.memory_store != prior_memory_store:
                changed_fields.add("memory_store")
            persist_member_config(
                cfg,
                args.name,
                expected_store=prior_memory_store,
                changed_fields=changed_fields,
            )
        except BaseException as exc:
            if not isinstance(exc, (OSError, UnknownMemoryStore)):
                raise
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if new_label is not None:
            shown = agent.display_name or args.name
            print(f"Updated agent: {args.name} (display name: {shown})")
        else:
            print(f"Updated agent: {args.name}")
        if new_model is not None:
            print(f"Model: {agent.model or '(inherit)'}")

    elif action == "delete":
        if args.name not in cfg.agents:
            print(f"Error: agent '{args.name}' not found", file=sys.stderr)
            sys.exit(1)
        if args.name == cfg.default_agent:
            print(
                f"Error: cannot delete default agent '{args.name}'",
                file=sys.stderr,
            )
            sys.exit(1)

        def _mutate_agent_delete(doc: dict) -> dict:
            agents = coerce_dict_section(doc, "agents")
            if args.name not in agents:
                raise _CliConflict(f"agent '{args.name}' not found")
            # Re-check the default in-lock, at the authoritative TOP-LEVEL key
            # (loader reads `data.get("default_agent")`); the agent-section
            # key is the migration-era location and is checked as well.
            agent_section = doc.get("agent")
            if doc.get("default_agent") == args.name or (
                isinstance(agent_section, dict) and agent_section.get("default_agent") == args.name
            ):
                raise _CliConflict(f"cannot delete default agent '{args.name}'")
            del agents[args.name]
            return doc

        # Same best-effort drop as the dashboard delete route, and in the same
        # place: AFTER the registry write has committed and still INSIDE its
        # lock. After the commit, so a config write that fails leaves the
        # membership as it was (the crew stays, on its team); inside the lock,
        # so a same-name create in another process (which needs this lock)
        # cannot land between the delete and the drop. A stale team entry is
        # hidden by every reader and never turns a committed delete into a
        # failure; the recreate-under-the-same-name harm is closed on the
        # create path (release_name), not here.
        def _drop_from_team() -> None:
            crew_teams.drop_member(args.name)

        with memory_store_namespace_lock():
            _locked_config_write(_mutate_agent_delete, after_write=_drop_from_team)
        print(f"Deleted agent: {args.name}")

    elif action == "reset-model":
        _agent_reset_model(args)

    else:
        print("Usage: kirocrew agent {list|create|update|delete|reset-model}")


def _agent_reset_model(args: argparse.Namespace) -> None:
    """Clear an agent spec's pinned model (``kirocrew agent reset-model``).

    The explicit, narrow way back to the shipped default model. It exists
    because ownership of a spec's ``model`` cannot be inferred: a value an older
    build's propagation wrote and one the user typed in by hand are
    byte-identical on disk, so nothing may reclassify a pin behind the user's
    back. Before this, the only ways out were the dashboard's Agent Templates
    editor (clear the model) and ``kirocrew setup --clean``, which also
    regenerates the whole spec and discards every other customization with it.
    """
    name = getattr(args, "agent", None) or "kirocrew"
    try:
        spec_path, previous = reset_agent_model(name)
    except FileNotFoundError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        # Ambiguous: two specs claim the name and the runtime's choice between
        # them is undefined, so resetting either could strip the wrong one.
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"❌ Could not write the agent spec: {exc}", file=sys.stderr)
        sys.exit(1)

    if previous:
        # repr on BOTH the model and the path. Neither is trusted input: an
        # installed app writes specs into the agents directory and a cloned
        # repository can ship one, so an OSC/ANSI sequence can arrive in the
        # `model` value AND in the FILENAME -- the declared-name scan returns
        # whichever file declares the requested name, so its path is attacker-
        # shaped even though *name* itself is grammar-validated.
        print(f"✅ Cleared {name}'s pinned model ({previous!r}) in {str(spec_path)!r}")
    else:
        print(f"✅ {name} had no pinned model in {str(spec_path)!r}")
    print("   It now tracks the shipped default; restart the gateway to apply.")
