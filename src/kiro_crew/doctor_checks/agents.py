"""Agent and harness rows of ``kirocrew doctor``.

Crew member names and memory bindings, config surfaces that still name a
deprecated agent spec, the optional Claude Code backend, and one sign-in row per
selectable harness. Names the doctor imports, and the host probes a repository
gate pins to ``cli_doctor.py``, are read through :mod:`kiro_crew.cli_doctor` at call
time, so a patch there reaches these sections.
"""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import render

if TYPE_CHECKING:
    from kiro_crew.config import KiroCrewConfig


#: Printed by the two member sections when the redaction policy that decides
#: dispatchability cannot be consulted. Doctor is the one command that runs on a
#: host whose platform failed to compose (``cli.py`` exempts it from the
#: fail-closed re-raise), and ``is_dispatchable_member_name`` reaches
#: ``platform.context.redact_via_context``, which re-raises that failure rather
#: than degrade. The Platform section already reports the composition error as
#: the blocking issue, so these sections say "not checked" and move on instead
#: of aborting the report -- or double-counting the same issue.
_MEMBER_NAMES_NOT_CHECKED = (
    "  ⚠️  not checked: the platform did not compose, so stored Crew Member names "
    "cannot be vetted (see the Platform section above)"
)


def _member_dispatchability(cfg: KiroCrewConfig) -> dict[str, bool] | None:
    """Dispatchability per configured member, or ``None`` when it cannot be decided.

    ``None`` -- never a partial dict -- when the redaction policy is unavailable:
    a member whose eligibility is unknown must not be printed by name, because
    the redaction that would have masked a credential-shaped one is exactly what
    failed. The two member sections fail closed on disclosure by skipping.
    """
    try:
        return {name: cli_doctor.is_dispatchable_member_name(name) for name in cfg.agents}
    except cli_doctor.PlatformCompositionError:
        return None
    except Exception:  # noqa: BLE001 -- doctor must survive a broken setup
        return None


def _doctor_member_dispatchability(cfg: KiroCrewConfig, issues: list[str]) -> None:
    """Report non-dispatchable stored member names without printing them."""
    dispatchable = _member_dispatchability(cfg)
    if dispatchable is None:
        print("\nCrew Member Names")
        print(_MEMBER_NAMES_NOT_CHECKED)
        return
    count = sum(1 for ok in dispatchable.values() if not ok)
    if not count:
        return
    noun = "name" if count == 1 else "names"
    print("\nCrew Member Names")
    print(f"  count:       {count} stored Crew Member {noun} cannot reach a model")
    print("               Open Crew Manager and create a replacement with a safe name.")
    print("               If needed, make the replacement the default. Then delete the old member.")
    print("               The replacement gets a new member identity. The old member's DM")
    print("               history stays under its old key and is not transferred automatically.")
    issues.append("stored Crew Member names are not dispatchable")


def _missing_v1_store_remedy(cfg: KiroCrewConfig, member: str, store: object) -> str:
    """The create command for a V1 binding whose only defect is a missing store directory."""
    from kiro_crew.memory_stores import (
        DEFAULT_MEMORY_STORE,
        create_store_remedy,
        memory_store_dir_for,
        require_member_memory_store,
    )

    if not isinstance(store, str) or store == DEFAULT_MEMORY_STORE:
        return ""
    record = cfg.memory_stores.get(store)
    if record is None or getattr(record, "memory_version", None) != 1:
        return ""
    try:
        require_member_memory_store(cfg, member, require_directory=False)
        if os.path.lexists(memory_store_dir_for(store)):
            return ""
    except Exception:  # noqa: BLE001 -- a different defect; no create step fixes it
        return ""
    return create_store_remedy(store)


def _doctor_member_memory_bindings(cfg: KiroCrewConfig, issues: list[str]) -> None:
    """Check every configured member's existing binding without initializing memory."""
    from kiro_crew.memory_stores import (
        LEGACY_MEMBER_STORE_REMEDY,
        legacy_member_store_states,
        require_member_memory_store,
    )

    # A V2 store with no owner_member_id predates member identities. The start-of-
    # process upgrade repairs the ones it can attribute to exactly one member;
    # doctor itself repairs nothing (it is exempt from that prologue so it can
    # report the stores), so a repairable store is reported as pending through
    # the one member bound to it -- the resolver refuses it today, but nothing is
    # broken -- and a refused one carries the upgrade's own reason and remedy.
    legacy = legacy_member_store_states(cfg)
    print("\nMember Memory Bindings")
    if not cfg.agents:
        print("  (no configured members)")
    dispatchable = _member_dispatchability(cfg)
    if dispatchable is None:
        # Every binding line below names its member; with no way to vet the
        # names, none of them may be printed.
        print(_MEMBER_NAMES_NOT_CHECKED)
        return
    for name, member in cfg.agents.items():
        if not dispatchable[name]:
            # The dispatchability report counts this record without naming it;
            # building the binding string here would disclose the stored name.
            continue
        store = getattr(member, "memory_store", None)
        binding = f"{render._safe_display(name)} -> {render._safe_display(store)}"
        if isinstance(store, str) and store in legacy and not legacy[store]:
            print(
                f"  {binding}: no member identity yet; the next gateway start or "
                "CLI command upgrades it automatically"
            )
            continue
        try:
            require_member_memory_store(cfg, name, require_directory=True)
        except Exception as exc:  # noqa: BLE001 -- one broken member must not hide healthy peers
            print(f"  {binding}: unavailable ({render._safe_display(str(exc))})")
            issues.append(f"member memory binding unavailable: {binding}")
            remedy = _missing_v1_store_remedy(cfg, name, store)
            if remedy:
                print(f"               To fix: {render._safe_display(remedy)}.")
        else:
            print(f"  {binding}: valid binding")
    for store, reason in legacy.items():
        if not reason:
            # Dispatchable owners report pending stores above. Unsafe owners are
            # counted without names in the Crew Member Names section.
            continue
        print(
            f"  store {render._safe_display(store)}: no member identity and not upgradable "
            f"({render._safe_display(reason)}); to repair it, {LEGACY_MEMBER_STORE_REMEDY}"
        )
        issues.append(f"member memory store without identity: {render._safe_display(store)}")


# kiro-cli is the DEFAULT agent backend; the claude-agent-acp binary below belongs
# to Claude Code, which is also selectable. Doctor reports it as an optional
# backend, and the verdict comes from ``agent_sdk.probe_backend`` so doctor and the
# dashboard cannot give different answers.
_CLAUDE_ACP_BIN = "claude-agent-acp"


def _open_slot_agent_names() -> list[tuple[str, str]]:
    """``(slot key, agent name)`` for every open dashboard tab persisting one.

    Read-only + best-effort: reads ``open_slots.json`` and each open slot's
    transcript metadata line off disk (no running gateway needed), returning an
    empty list on any error. Slot keys pass through the restore path's own
    sanitizer before they reach path construction -- the file is
    attacker-writable, and doctor must not accept a key the restore path would
    reject.
    """
    try:
        from kiro_crew.dashboard.chat_persistence import (
            _read_open_slots_keys,
            _sanitize_open_slot_key,
        )
        from kiro_crew.dashboard.chat_utils import slot_transcript_key
        from kiro_crew.history import ConversationLog

        log = ConversationLog()
        out: list[tuple[str, str]] = []
        for raw in _read_open_slots_keys():
            key = _sanitize_open_slot_key(raw)
            if not key:
                continue
            # slot_transcript_key, not _history_key_for: a channel-born tab's
            # slot key (e.g. slack_<ts>) already addresses its transcript, and
            # an unconditional dashboard: prefix would read a nonexistent file
            # and silently skip that tab.
            agent = log.get_metadata(slot_transcript_key(key)).get("agent")
            if isinstance(agent, str) and agent:
                out.append((key, agent))
        return out
    except Exception:
        cli_doctor.logger.debug("doctor: open-slot agent scan failed", exc_info=True)
        return []


def _doctor_deprecated_agent_specs(cfg: KiroCrewConfig, issues: list[str]) -> None:
    """Report configs that still name a deprecated agent spec.

    A deprecated spec (``DEPRECATED_AGENT_SPECS`` in
    ``agent_materialization/conductor_agents.py``) still resolves for one
    release, so a config surface naming it -- a cron job, a
    crew binding, an open chat slot, or one of the config's own agent
    selectors -- keeps working today and breaks with ``Mode not found`` at
    dispatch time once the alias is deleted. Each finding names the replacement so the owner
    can migrate inside the window.

    Silent when nothing names one: the installed alias spec by itself is
    expected (the gateway installs it every boot), not a finding.
    """
    from kiro_crew.agent import DEPRECATED_AGENT_SPECS
    from kiro_crew.cron import job_agent_names_from_disk

    # (holder description, deprecated name, replacement). Holder text is
    # user/LLM-writeable (crew names, job names, slot keys) so it goes through
    # _safe_display; the matched name and its replacement are keys and values
    # of our own table, so they print as-is.
    findings: list[tuple[str, str, str]] = []

    # A cron job, chat slot, or config selector may name a CREW rather than a
    # kiro agent spec; the crew row owns that report, so those names are
    # skipped on the leaf surfaces rather than double-flagged through the
    # crew's binding.
    crew_names = set(cfg.agents)

    def _add(holder: str, name: object) -> None:
        # config.json is hand-editable and agent-writable, and the loader
        # preserves some of these values verbatim (e.g. kiro_agent), so a
        # non-string can arrive here. dict.get on an unhashable value raises
        # TypeError, and doctor must diagnose a malformed config, not crash
        # on it -- a non-string never names a deprecated spec, so skip it.
        if not isinstance(name, str) or not name or name in crew_names:
            return
        replacement = DEPRECATED_AGENT_SPECS.get(name)
        if replacement:
            findings.append((holder, name, replacement))

    # Crew bindings: config.json agents.<name>.kiro_agent. A crew name is not
    # skipped here -- crew_names shields only the LEAF surfaces that resolve
    # through a crew, and a kiro_agent that happens to equal a crew name is
    # not resolved again.
    for crew_name, crew in cfg.agents.items():
        name = crew.kiro_agent
        if not isinstance(name, str) or not name:
            continue
        replacement = DEPRECATED_AGENT_SPECS.get(name)
        if replacement:
            findings.append((f"crew {render._safe_display(crew_name)}", name, replacement))

    # The config's own persisted agent selectors.
    _add("agent.default_agent", cfg.agent.default_agent)
    _add("session.pool_agent", cfg.session.pool_agent)
    for channel_id, channel in cfg.slack_channels.items():
        _add(f"slack channel {render._safe_display(channel_id)}", channel.agent)

    # Cron jobs: the agent names dispatch actually runs, read off crons.json
    # (agent_id, or the agent_sequence entries when the sequence dispatches).
    for holder, name in job_agent_names_from_disk():
        _add(f"cron job {render._safe_display(holder)}", name)

    # Chat slots: each open tab's persisted agent from its transcript metadata.
    for slot_key, name in _open_slot_agent_names():
        _add(f"chat slot {render._safe_display(slot_key)}", name)

    if not findings:
        return

    print("\nDeprecated Agent Specs")
    for holder, name, replacement in findings:
        print(
            f"  {holder}:  \u26a0\ufe0f  names deprecated agent spec "
            f"'{name}' -- rename it to '{replacement}'"
        )
    print(
        "               A deprecated spec still resolves this release and is "
        "deleted next release; a config still naming it then fails with "
        "'Mode not found' at dispatch time."
    )
    issues.append("a config names a deprecated agent spec")


def _doctor_claude_backend() -> None:
    """Report Claude Code as an optional agent backend, installed or not.

    Its own function, not an inline block, so a test can exercise the reporting
    without running the whole doctor -- the full ``_doctor()`` shells out to
    ``kiro-cli whoami`` and probes the host, and a test must not reach an
    operator's real installation to check three print statements.

    Claude Code needs TWO binaries and the probe names whichever is absent, so a
    half-install does not read as a total one. Never a hard failure: it is an
    optional backend and kiro-cli is the floor. The verdict comes from
    ``agent_sdk.probe_backend`` -- the same owner ``GET /api/acp-backends`` uses --
    so doctor and the dashboard cannot give different answers.
    """
    try:
        from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE
        from kiro_crew.agent_sdk import INSTALLED, MISSING, probe_backend

        claude_state = probe_backend(ACP_BACKEND_CLAUDE)
    except Exception:
        claude_state = None
    if claude_state is None:
        print("  claude-acp:  ⚠️  could not check")
    elif claude_state.installed == INSTALLED:
        # The probe resolves the adapter through the spawn's own resolver, which honours
        # CLAUDE_AGENT_ACP_BIN, a vendored node_modules and a mise shim -- none of which
        # a plain PATH lookup sees. So `which` is best-effort here and its miss must not
        # print as a location: naming the path only when we actually have one beats
        # printing "✅ None" for a working install.
        #
        # Says "installed", NOT "selectable": this branch reads the INSTALL probe, and
        # whether the deployment may select the backend is a separate answer that
        # ``apply_selectable_denials`` can say no to. Calling an install "selectable"
        # would print the opposite of the truth on a policy-denied deployment.
        where = shutil.which(_CLAUDE_ACP_BIN)
        if where:
            print(f"  claude-acp:  ✅ {where} (Claude Code installed)")
        else:
            print("  claude-acp:  ✅ resolved off PATH (Claude Code installed)")
    elif claude_state.installed == MISSING:
        missing = ", ".join(claude_state.missing_components) or "components"
        print(f"  claude-acp:  ⏭  {missing} not found (optional agent backend)")
        if claude_state.install_command:
            print(f"               {claude_state.install_command}")
    else:
        # UNKNOWN: the check itself failed. Reporting that as "not found" would send
        # someone to install what they may already have -- the exact collapse the
        # probe's three-valued verdict exists to prevent, and which the dashboard
        # also refuses to make.
        print("  claude-acp:  ⚠️  could not check")


def _backend_policy_label(backend: str) -> str:
    """The human spelling of *backend*, matching the refs row's own labels.

    ``ACP_BACKEND_KIRO`` is the empty string, so a bare id renders as nothing;
    the policy mapping is the one place that already owns a printable name for
    every id this build can spell.
    """
    from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND

    return POLICY_ID_BY_BACKEND.get(backend, backend) or backend


def _doctor_agent_auth() -> None:
    """One sign-in row per selectable harness, projected from its declaration.

    Replaces four per-provider answers that could disagree: an inline
    ``kiro-cli whoami`` row, a Claude report with no auth line at all, no codex
    row whatsoever, and a KAS block asserting in prose whose token it used. Each
    was a separate edit, so a harness that became selectable without one was
    simply silent here -- which is how a signed-out harness could read as ready.
    Now the row comes from ``agent_sdk.host_auth``, the same declaration the
    credential floor and ``GET /api/acp-backends`` read, so doctor cannot say
    something the panel contradicts.

    Iterates ``acp_backends.selectable_backend_values()`` -- the sorted form of
    ``selectable_backends()``, which is already this module's neighbourhood via
    ``_doctor_claude_backend``'s local ``acp_backends`` import. Chosen over
    ``agent_sdk.probe_backends()`` for two reasons: it is the set the operator can
    actually select (a policy-denied harness needs no sign-in advice), and it
    answers without spawning the install probes' subprocesses, which this row does
    not need.

    **This checks only credentials that are the host's own, and deliberately.**
    Two stores qualify: the HOST identity store, probed through ``kiro-cli
    whoami`` (kiro-cli signs in to it, so its state is the host's own and
    readable here), and Crew's own sign-in vault, which a harness in
    ``ACP_BACKENDS_HOST_AUTH_CALLBACK`` draws on whenever the vault holds a
    usable identity -- the same runtime decision the KAS relay makes at spawn,
    so the row reports the store the next spawn will actually use. Every other
    harness keeps its entitlement in a file it owns, and reading that file is
    exactly what the credential floor exists to forbid -- a probe here would be
    the one reader the floor cannot fence. So those rows name the store and
    print the declared remedy unprobed: advice that is always correct beats a
    verdict obtained by breaking the floor.

    Advisory only, which is why it takes no ``issues`` list: a harness the operator
    has not signed into is not a broken installation, and failing doctor's exit code
    on it would make the default host red for an optional backend.
    """
    from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND, selectable_backend_values
    from kiro_crew.agent_sdk import declaration_for, entitlement_label, signs_in_separately
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_HOST_AUTH_CALLBACK

    try:
        backends = selectable_backend_values()
    except Exception:
        # Reading the registry must not break triage; the rows are advisory.
        return

    # Probed at most once even though two harnesses share the host store: kiro and
    # KAS both resolve tokens from it, and spawning ``whoami`` per row would pay
    # twice for one answer.
    host_signed_in: bool | None = None
    host_probed = False
    # The vault too is probed at most once, for the same reason: every
    # host-auth-callback harness draws on the one vault.
    vault_owns = False
    vault_detail: str | None = None
    vault_probed = False

    for backend in backends:
        try:
            declaration = declaration_for(backend)
            separate = signs_in_separately(backend)
        except Exception:
            continue
        label = f"{POLICY_ID_BY_BACKEND.get(backend, backend) or backend} auth:"
        # The LABEL, not the identifier. ``entitlement_source`` is code
        # (``own_credential_file``), and printing it put snake_case internals in a
        # row an operator is meant to read during triage.
        source = entitlement_label(backend)

        if separate:
            # No probe, by the rule above. "not checked here" is load-bearing: it
            # tells the operator this ➖ is an absence of evidence, not a verdict
            # that the harness is signed out.
            print(f"  {label.ljust(13)}➖ {source} (not checked here)")
            # The ACTION, not the state: nothing was measured on this row, and a
            # line reading "is not signed in" under a "not checked here" would
            # contradict the line above it and train the reader to skip both.
            render._print_wrapped(declaration.sign_in_remedy)
            continue

        if backend in ACP_BACKENDS_HOST_AUTH_CALLBACK:
            # The spawn picks this harness's auth owner at runtime -- Crew's vault
            # when it holds a usable identity, kiro-cli's store otherwise (see
            # ``kas_host_auth``) -- so the row mirrors that decision instead of the
            # declaration's compile-time constant, which cannot.
            if not vault_probed:
                vault_probed = True
                try:
                    # Deferred import, same seam as ``_report_kas_backend``:
                    # ``kiro_crew.auth`` brings the cryptography wheel with it, so it
                    # loads only when this row asks. An import or probe failure
                    # degrades to the kiro-cli branch below rather than losing the
                    # row.
                    from kiro_crew.auth.bridge import (
                        describe_vault_identity,
                        vault_holds_identity,
                    )

                    vault_owns = vault_holds_identity()
                    vault_detail = describe_vault_identity()
                except Exception:
                    vault_owns = False
                    vault_detail = None
            if vault_owns:
                # Ownership and health are separate facts: the vault still owns the
                # next spawn when the issuer has REJECTED its refresh token, because
                # ``is_usable`` cannot know that without a network call (see its
                # docstring) and ``vault_holds_identity`` reads only it. The glyph
                # column is what an operator scans, so ✅ requires a verdict that
                # affirms it: a detail line ending "-> usable". A missing detail
                # (the two reads disagree -- a logout landed between them, or the
                # describe probe failed) and an unrecognized verdict both degrade
                # to ⚠️, never to a false ✅.
                healthy = vault_detail is not None and vault_detail.endswith("-> usable")
                glyph = "✅ " if healthy else "⚠️  "
                print(f"  {label.ljust(13)}{glyph}Kiro Crew vault (signed in through Kiro Crew)")
                if vault_detail:
                    render._print_wrapped(f"crew vault: {vault_detail}")
                if host_probed and host_signed_in is True:
                    # Both stores hold a sign-in, and they can be DIFFERENT
                    # accounts (the usage reader's identity checks exist for
                    # exactly that). Reported, not adjudicated: the relay uses the
                    # vault, and which account is "right" is not this row's
                    # question.
                    render._print_wrapped(
                        f"{source} is also present and may be a different account "
                        "(the kiro-cli row reports it); the relay uses the vault."
                    )
                continue
            # Nothing usable in the vault: the kiro-cli branch below is the
            # runtime's fallback owner, so it is this row's report too. A stored
            # identity the probe rejected is still printed beneath the row --
            # that entry is exactly why a spawn is failing when the operator has
            # signed in through Crew and the sign-in has since lapsed.

        if not host_probed:
            host_signed_in = cli_doctor._kiro_cli_signed_in()
            host_probed = True
        if host_signed_in is True:
            print(f"  {label.ljust(13)}✅ {source}")
        elif host_signed_in is None:
            print(f"  {label.ljust(13)}⚠️  {source}; could not check")
        else:
            print(f"  {label.ljust(13)}⏹ {source}; not signed in")
            # The signed-out STATEMENT here, because this row alone has evidence:
            # the host identity store is the one store this core may read.
            # Wrapped, not reflowed: ``textwrap.wrap`` only inserts line breaks, so
            # the operator reads the declared wording, which is what the panel shows.
            render._print_wrapped(declaration.signed_out_message)
        if backend in ACP_BACKENDS_HOST_AUTH_CALLBACK and vault_detail:
            render._print_wrapped(f"crew vault: {vault_detail}")
