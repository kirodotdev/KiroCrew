"""The Model section of ``kirocrew doctor`` and the audit of the agent specs' model pins.

:func:`_doctor_effective_model` reports which model a new session starts on and which tier
decided it; :func:`_agent_spec_model_problems` finds the spec pins kiro-cli does not serve,
and :func:`_format_model_pin_problem` renders one. Both read agent specs through the
hardened discovery reader. New Model-section rows go here.

Composed onto ``kiro_crew.cli_doctor``, the command's one import path and patch target;
see :mod:`kiro_crew.cli_doctor_parts`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.cli_doctor import (
        AGENT_FILENAME,
        KiroCrewConfig,
        _agents_dir,
        _read_agent_spec,
        acp_id_correction,
        agent_spec_path,
        agent_state,
        is_agent_spec_name,
        is_claude_code,
        is_registered_agent_name,
        is_sensitive_path,
        kiro_agents_dir,
        normalize_agent_model,
        project_agent_files,
        project_agent_name,
        project_agents_dir,
        resolve_agent_bindings,
        resolve_effective_model,
        shadowed_markdown_specs,
    )


def _doctor_effective_model(cfg: KiroCrewConfig, project_dir: str, issues: list[str]) -> None:
    """Report which model a new session starts on, and which tier decided it.

    The precedence is real and four tiers deep, and the tier that wins is not
    visible from any single file, so a surprising model -- the wrong one, or a
    stale one that outlived the setting that created it -- is otherwise only
    diagnosable by hand-reading config.json, two agent-spec directories and the
    sidecar.

    The tiers are listed as DATA and the first non-deferring one is marked, which
    is ``resolve_effective_model``'s own rule. The marked value is then
    cross-checked against what that function actually returns and a disagreement
    is REPORTED rather than hidden, so this report cannot quietly drift into a
    second, wrong copy of the precedence.

    Read-only: this section never repairs anything, because a spec's ``model``
    cannot be attributed -- a value an older build's propagation wrote and one
    the user typed in are identical on disk -- so the repair has to be the
    user's explicit call (``kirocrew agent reset-model``).
    """
    from kiro_crew.doctor_checks import render

    print("\nModel")
    try:
        effective = resolve_effective_model(cfg)
    except Exception as exc:  # noqa: BLE001 -- diagnostics must not crash the report
        print(f"  effective:   ⚠️  could not resolve ({exc})")
        issues.append("effective model unresolvable")
        return

    def _spec_model(path: Path) -> tuple[str, bool]:
        """Return (normalized model, usable) for a kiro spec file.

        Routed through ``agent_discovery._read_agent_spec``, which the module
        documents as the ONE reader for both agent scopes so every guard applies
        uniformly: it goes through the hardened size-capped read gate (a
        multi-gigabyte "agent config" is refused rather than slurped), and it
        rejects a symlink whose resolved target is sensitive, non-UTF-8 bytes,
        AppleDouble sidecars and JSON that is not an object. Hand-rolling those
        checks here would be a second, weaker copy of a reader that already
        exists.
        """
        data = _read_agent_spec(path, operation="doctor", source="cli")
        if data is None:
            # An ABSENT spec is not a fault -- a clean install has none, and the
            # resolver simply falls through to the bundled default. Only a file
            # that exists and the hardened reader still refuses is reported.
            try:
                exists = path.exists() or path.is_symlink()
            except OSError:
                exists = True
            return "", not exists
        return normalize_agent_model(data.get("model")), True

    # Deliberately kiro_agents_dir() and not _agents_dir(): this section compares
    # tiers against what resolve_effective_model returned, so it has to read the
    # very directory that function reads. Reporting a different directory's spec
    # beside its verdict is how a report starts contradicting itself.
    agents_dir = kiro_agents_dir()

    # A plain session is the default TEMPLATE (`agent.default_agent`), which
    # may be a kiro agent other than the built-in one, and the resolver
    # treats those two differently: a non-default template's own pin is
    # consulted ABOVE the global (tier 2), while the built-in spec is read only
    # after the global defers (tier 4). Reading kirocrew.json in both cases
    # would attribute a custom agent's pin to the wrong file and print a reset
    # command for the wrong agent. The `default` crewmate's own binding is not
    # what a plain session runs; its memory is checked in the bindings section.
    try:
        bindings = resolve_agent_bindings(cfg)
        override = normalize_agent_model(bindings.model)
        bound = bindings.kiro_agent or "kirocrew"
    except Exception as exc:  # noqa: BLE001 -- a broken alias must not kill the report
        print(f"  binding:     unavailable ({render._safe_display(str(exc))})")
        print("               See the member memory binding diagnostics below.")
        issues.append("default agent binding unavailable")
        override = ""
        bound = "kirocrew"
    # `agent.default_agent` is free text in config.json and this name reaches a
    # path join. An ABSOLUTE value would make pathlib discard the directory on
    # the left (`base / "/etc/passwd.json"` is `/etc/passwd.json`), so anything
    # outside the registered agent grammar is reported and treated as unbound.
    # The resolver already degrades a NON-STRING to the built-in template
    # (`default_template`) so the gateway keeps running; the report must still
    # say so, since a hand-edited config is why the operator runs doctor.
    raw_default = getattr(cfg.agent, "default_agent", "")
    default_is_text = raw_default is None or isinstance(raw_default, str)
    if not default_is_text:
        bound = str(raw_default)
    if not default_is_text or not is_registered_agent_name(bound):
        print(f"  bound agent: ⚠️  {render._safe_display(bound)} is not a valid agent name")
        issues.append("configured kiro_agent is not a valid agent name")
        bound = "kirocrew"

    default_spec = agents_dir / AGENT_FILENAME
    default_model, default_readable = _spec_model(default_spec)
    if not default_readable:
        print(f"  user spec:   ⚠️  unreadable ({default_spec})")
        issues.append("agent spec unreadable")

    bound_model = ""
    bound_spec: Path | None = None
    bound_spec_missing = False
    bound_spec_ambiguous = False
    if bound != "kirocrew":
        # Display only, through the same resolver the writers use, so the path
        # shown is the file that holds the agent -- whichever form (``.json``
        # or ``.md``) and whichever filename declares the name -- rather than a
        # ``.json`` join that names a file a markdown agent does not have.
        try:
            bound_spec = agent_spec_path(bound, agents_dir=agents_dir)
        except ValueError:
            # Two or more safe specs declare the name, so no single file IS
            # the bound spec. The model resolver below does not refuse: it
            # reads the first spec, JSON files first, whose declared name or
            # filename matches, and the bound-agent tier shows what that spec
            # pins, if anything. Reported as ambiguous, not as missing: the
            # name is declared.
            bound_spec = None
            bound_spec_ambiguous = True
        bound_spec_missing = bound_spec is None and not bound_spec_ambiguous
        # Read through the resolver's own accessor: it matches on the spec's
        # ``name`` field as well as the filename, which a bare path join misses.
        try:
            bound_model = normalize_agent_model(cfg._resolve_named_agent_model(bound))
        except Exception:  # noqa: BLE001
            bound_model = ""

    # Labelled in resolve_effective_model's own order. Tier 2 is present only
    # when it applies, so the list never shows a tier the resolver skipped.
    tiers: list[tuple[str, str]] = [("agent override", override)]
    if bound != "kirocrew":
        tiers.append((f"bound agent pin ({render._safe_display(bound)})", bound_model))
    tiers.append(("global agent.model", normalize_agent_model(cfg.agent.model)))
    tiers.append(("default spec pin", default_model))

    # Label and value come out of the SAME tier by construction; a second lookup
    # for the value could be filtered differently and mis-attribute the decision.
    decided = next(((label, value) for label, value in tiers if value), None)
    if decided is not None:
        decided_by, decided_value = decided
    else:
        decided_by = "bundled defaults.json"
        # Nothing pinned anything, so the bundled default answered and the
        # resolver's value is legitimately ours -- unless a spec read was
        # REFUSED, in which case the resolver may have followed a link this
        # report would not, and adopting its answer would hide exactly that.
        decided_value = effective if default_readable else ""

    print(
        f"  effective:   {render._safe_display(effective) if effective else 'auto (backend picks)'}"
    )
    print(f"  decided by:  {decided_by}")
    for label, value in tiers:
        print(f"    {label + ':':<26} {render._safe_display(value) if value else '(defers)'}")
    print(f"  spec file:   {render._safe_display(str(default_spec))}")
    if bound_spec is not None:
        print(f"  bound spec:  {render._safe_display(str(bound_spec))}")
    elif bound_spec_missing:
        # The resolver falls back to the built-in template only on removal
        # evidence it saw in THIS process; after a restart a name whose file is
        # simply gone still resolves by name, and every agent-less session --
        # a plain chat, a cron, a channel message, a subagent -- inherits it.
        print(
            f"  bound spec:  ⚠️  no spec for {render._safe_display(bound)} under {render._safe_display(str(agents_dir))}"
        )
        print("               Every new session that picks no mate -- plain chats, crons,")
        print(
            "               channel messages, subagents -- starts from this agent and is refused."
        )
        print("               Pick another default in the chat agent picker (Default for new")
        print("               sessions), or: kirocrew config set agent.default_agent kirocrew")
        issues.append("default custom agent has no installed spec")
    elif bound_spec_ambiguous:
        print(
            f"  bound spec:  ⚠  two specs under {render._safe_display(str(agents_dir))} declare {render._safe_display(bound)}"
        )
        print(
            "               Sessions still start, but the model tier above cannot tell which file"
        )
        print("               pins the model. Rename or remove one of the two files.")
    # A ``<stem>.md`` beside its ``<stem>.json`` twin is never read: the JSON
    # wins. The roster says so in the gateway log only, and an author who edited
    # the markdown copy runs doctor to learn why the agent did not change.
    shadowed = shadowed_markdown_specs(agents_dir)
    if shadowed:
        print(
            f"  shadowed:    ⚠  {len(shadowed)} markdown agent file(s) ignored beside a .json twin:"
        )
        for hidden_md in shadowed:
            print(
                f"                 {render._safe_display(str(hidden_md))}"
                f" ({render._safe_display(hidden_md.stem + '.json')} wins)"
            )

    # Self-check: the marked tier must be what the resolver actually returned.
    if decided_value != effective:
        if not default_readable:
            # Not drift. The resolver reads the spec through its own path, which
            # FOLLOWS a symlink, while this report refuses to; so it can resolve
            # a value this section declined to attribute. Say that, rather than
            # accusing the tier list of being stale.
            print(
                "  ⚠️  the resolver read a spec this report refused to follow, so the "
                "deciding tier above is not attributed"
            )
        else:
            print(
                f"  ⚠️  this report says {decided_value!r} but the resolver returned "
                f"{effective!r} — the precedence shown here is out of date"
            )
            issues.append("doctor model precedence disagrees with the resolver")

    # Which spec is actually deciding, so the tracking state and the repair below
    # describe THAT agent rather than always the built-in one.
    if decided_by.startswith("bound agent pin"):
        pinned_agent, pinned_value = bound, bound_model
    elif decided_by == "default spec pin":
        pinned_agent, pinned_value = "kirocrew", default_model
    else:
        pinned_agent, pinned_value = bound, ""

    try:
        managed = agent_state.get_model_managed(pinned_agent)
    except Exception:  # noqa: BLE001 -- an unreadable sidecar is not fatal here
        managed = None
    if managed is None:
        tracking = "not recorded"
    else:
        tracking = "shipped default" if managed else "frozen (explicit pick)"
    print(f"  tracking:    {tracking} ({render._safe_display(pinned_agent)})")

    # kiro-cli resolves --agent against <project>/.kiro/agents FIRST, with no
    # upward walk, and Kiro Crew's own resolver never reads that directory. So a
    # project-local spec can decide what actually RUNS while every Kiro Crew
    # surface reports something else -- worth naming even though it is rare.
    # *project_dir* is the caller's already-resolved value (env, else the saved
    # project_dir file), so this agrees with the Project section above.
    if project_dir:
        # Resolved the way kiro-cli itself resolves --agent, via the existing
        # helper: the DECLARED name wins and the filename is only the fallback,
        # so a project spec that declares this agent under some other filename is
        # still found. Matching on `<bound>.json` alone would miss exactly that
        # and under-report the shadow.
        proj_spec = next(
            (
                p
                for p in project_agent_files(project_dir, operation="doctor", source="cli")
                if project_agent_name(p) == bound
            ),
            None,
        )
        if proj_spec is not None:
            proj_model, proj_usable = _spec_model(proj_spec)
            if proj_model:
                shown = render._safe_display(proj_model)
            elif not proj_usable:
                shown = "(unreadable)"
            else:
                shown = "(no model)"
            print(f"  project spec: ⚠️  {render._safe_display(str(proj_spec))} -> {shown}")
            print("                kiro-cli loads this one first; not read above")
            issues.append("project-local agent spec shadows the user-level one")

    if pinned_value:
        # pinned_agent is either the literal "kirocrew" or a configured kiro
        # agent name; the flag form is only emitted for a name that matched a
        # spec file on disk, so it is a real agent rather than free text.
        # The name is escaped like every other value read out of config: a
        # control-bearing kiro_agent would otherwise reach the terminal on the
        # one line the user is most likely to copy and run.
        flag = (
            "" if pinned_agent == "kirocrew" else f" --agent {render._safe_display(pinned_agent)}"
        )
        global_shown = render._safe_display(cfg.agent.model) if cfg.agent.model else "unset"
        print(f"  ⚠️  the spec pin decides because the global is {global_shown}")
        print(f"      Fix: kirocrew agent reset-model{flag}   (clears the pin, tracks the default)")


def _strict_agent_json_specs(directory: Path) -> list[Path]:
    """Enumerate real spec candidates while preserving directory-read failures."""
    try:
        with os.scandir(directory) as entries:
            return sorted(
                (
                    Path(entry.path)
                    for entry in entries
                    if is_agent_spec_name(entry.name) and not entry.name.startswith("._")
                ),
                key=lambda path: path.stem,
            )
    except (FileNotFoundError, NotADirectoryError):
        return []


def _agent_spec_model_problems(
    agents_dir: Path | None = None,
    project_dir: str | Path | None = None,
    provider: str = "acp",
) -> list[tuple[str, str, str]] | None:
    """Agent specs whose ``model`` names a model kiro-cli does not serve.

    Returns ``(agent name, pinned value, correct id)`` for each spec the registry
    can positively correct, an EMPTY list when every pin checked out, or ``None``
    when the check could not run at all. That third state is deliberate: a
    diagnostic that reports green for a check it never performed is worse than
    one that admits it could not look, which is the whole failure class this
    audit exists to close.

    *project_dir* is forwarded so project-scoped specs are audited too. A project
    spec SHADOWS a user-level agent of the same name, so a global-only scan can
    miss the exact spec a session in that project runs.

    Read through the hardened spec reader rather than opening files here, so a
    spec symlinked at something sensitive is refused the same way every other
    consumer refuses it.

    Reports only ids the registry recognizes under a different spelling. An
    unrecognized id is deliberately NOT reported: a real-but-unregistered id (a
    regional profile, or a model newer than this build's registry) is
    legitimate, and entitlement cannot be judged offline at all — that needs a
    live session's advertised set.
    """
    # The retained claude_code seam accepts its own registered wire ids. The
    # correction below is specifically an ACP/kiro-cli spelling audit.
    if is_claude_code(provider):
        return []

    problems: list[tuple[str, str, str]] = []
    try:
        global_dir = agents_dir or _agents_dir()
        global_specs = _strict_agent_json_specs(global_dir)
        if project_dir:
            if is_sensitive_path(str(project_dir)):
                return None
            project_specs = _strict_agent_json_specs(project_agents_dir(project_dir))
        else:
            project_specs = []

        # Normal discovery deliberately skips malformed or denied specs so one
        # bad file cannot break the agent picker. Doctor has the opposite
        # contract: a skipped candidate makes the audit incomplete, so read each
        # candidate directly through discovery's hardened reader and fail the
        # check to UNKNOWN when any one is refused.
        for path, project_scoped in (
            *((path, False) for path in global_specs),
            *((path, True) for path in project_specs),
        ):
            data = _read_agent_spec(path, operation="doctor", source="cli")
            if data is None:
                return None
            model = normalize_agent_model(data.get("model"))
            correction = acp_id_correction(model)
            if not correction:
                continue
            raw_name = data.get("name")
            if project_scoped:
                name = raw_name if isinstance(raw_name, str) and raw_name else path.stem
            else:
                name = raw_name if isinstance(raw_name, str) else path.stem
            problems.append((name, model, correction))
    except Exception:
        return None
    return problems


def _format_model_pin_problem(name: str, pin: str, correction: str) -> tuple[str, str]:
    """The two report lines for one unusable pin.

    Every field is repr'd, including the NAME: all three come from an agent
    spec's own contents, so a planted or packaged spec could otherwise carry
    terminal control sequences (cursor moves, screen clears, OSC) and rewrite or
    hide this report. ``repr`` escapes every control character, and is what the
    pin and correction already relied on.

    Separated from the printing so the escaping is a testable contract rather
    than a property of how far ``doctor()`` happens to get.
    """
    return (
        f"  model pin:   ❌ {name!r}: {pin!r} is not a model kiro-cli serves",
        f"                  the registry maps that spelling to {correction!r}",
    )
