"""The MCP Tools and MCP Governance sections of ``kirocrew doctor``.

:func:`_doctor_mcp_tools` checks the managed servers' entries in the agent config, repairs
them, revokes a grant the governance ceiling now denies and probes each server;
:func:`_doctor_mcp_governance` reports whether governance reaches the managed servers.
Both read one per-run snapshot of the servers whose spec gate is closed
(:func:`_doctor_gated_off_mcps`). New MCP rows go here.

Composed onto ``kiro_crew.cli_doctor``, the command's one import path and patch target;
see :mod:`kiro_crew.cli_doctor_parts`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.cli_doctor import (
        _ALWAYS_ON_MCPS,
        _MANAGED_MCPS,
        _NO_BLANKET_ALLOW_MCPS,
        _OPT_IN_MCPS,
        KiroCrewConfig,
        McpServerInfo,
        _agent,
        _read_agent_spec,
        atomic_write,
        logger,
        may_skip_gate_now,
        mcp_governance_may_apply,
        mcp_ref_owned_by,
        probe_server,
        sel,
        user_json,
        warm_backend,
        withheld_by_confirm_tools,
        without_mcp_refs,
    )


def _spec_gate_closed(name: str) -> bool:
    """Whether *name*'s spec-emission gate reports CLOSED right now.

    Spec emission consults each managed server's ``spec_gate``
    (``agent._MANAGED_MCP_SERVERS``): a closed gate means the ``mcpServers``
    entry is deliberately omitted from every emitted spec — and retracted from
    an existing one on refresh — so on such a host the entry's absence is the
    HEALTHY state, not a broken install. Doctor's static checks must consult
    the same predicate or the two sides drift apart, producing the unfixable
    "missing from mcpServers (re-run `kirocrew setup`)" loop on every host
    where the gate is closed. Resolving the gate through the registry
    keeps them pinned together: a future server gaining a gate needs no edit
    here, and a server without one reports open, exactly as emission treats it.

    The ``except`` covers gate-CONTRACT failures only — a registry entry that
    is not a dict, or a gate callable that raises past its own handling. For
    those, the fail direction is deliberately the OPPOSITE of emission's
    ``agent._gated_off_servers()``: there, a gate that raises is treated as
    closed, because the open position hands out a backend the operator may not
    want running; here it reports NOT closed, because "closed" is what
    silences the missing-entry error. Each side fails toward its own safe
    state. Note the scope honestly: the shipped computer-use gate catches its
    own internal errors and ANSWERS ``False`` (its documented fail-closed
    posture — an unreadable keystone must never hand out the desktop), so an
    unreadable keystone is indistinguishable from policy-closed through the
    boolean, by the gate's own design. That answer is still the
    emission-CONSISTENT one to report: in that state the entry genuinely is
    omitted from every emitted spec, so the ℹ️ line describes what the system
    actually does, even when the underlying cause is a broken enable-state
    read rather than a decision.

    Never loads a native driver: the computer-use gate reads only the enable
    keystone and platform flags (see ``agent._computer_use_spec_gate``), which
    is what makes it safe to evaluate on doctor's diagnostic path.

    The rule lives in ``agent._spec_gate_answers_closed``, which the session's
    unresolved-ref report also calls, so the two cannot drift.
    """
    return _agent._spec_gate_answers_closed(name)


def _doctor_gated_off_mcps() -> frozenset[str]:
    """Doctor's per-run snapshot of managed servers whose spec gate is closed.

    Evaluated ONCE per doctor run and threaded into both MCP sections, for the
    same reason ``agent._gated_off_servers()`` snapshots once per rebuild: the
    reads are cheap, agreeing is the point. A keystone flip landing between
    the `MCP Tools` and `MCP Governance` sections would otherwise produce a
    self-contradicting report — one saying "gated off by design", the other
    "markers missing — re-run `kirocrew setup --agent-only`". Not reused from
    ``_gated_off_servers()`` itself because the two snapshots fail in opposite
    directions on an unreadable gate (see :func:`_spec_gate_closed`).
    """
    return frozenset(name for name in _MANAGED_MCPS if _spec_gate_closed(name))


def _doctor_mcp_tools(
    agent_path: Path, issues: list[str], *, gated_off: "frozenset[str] | None" = None
) -> None:
    """Render the `MCP Tools` section of `kirocrew doctor`.

    Two passes scoped to the managed servers (`kirocrew-core`,
    `kirocrew-cron`, `kirocrew-computer`):

    1. Static coherence check of the agent config: each always-on server whose
       ``spec_gate`` is open — or that has no gate — must be present in
       ``mcpServers`` and ``tools``. A gated-off server (feature disabled, or
       no driver for this platform) is deliberately absent from every emitted
       spec, so its absence is reported as informational, never as an issue —
       and a stale entry left from when the gate was open is neither mounted
       into ``tools`` nor probed (see :func:`_spec_gate_closed`). Missing
       ``tools`` entries — and ``allowedTools`` entries for every server
       outside :data:`_NO_BLANKET_ALLOW_MCPS` — are auto-appended and the file
       is rewritten atomically; an instance that must not own the shared agent
       home writes nothing and reports the repairs as issues. A missing ``mcpServers`` entry cannot be
       auto-added because the command path is install-specific.
    2. Live handshake probe via :func:`mcp_discovery.probe_server`. Reports
       per-server status with tool count on success, and on failure shows
       the error head plus any captured stderr tail from the child — which
       usually contains the real cause (FindupException, ImportError, etc.)
       that would otherwise only exist in kiro-cli's per-session log.

    A spec that cannot be read as a JSON object — unreadable, unparseable,
    or valid JSON that is not an object — degrades to an empty config: every
    managed server then reports as missing and the file is never rewritten.
    """
    try:
        agent_data = user_json.loads_user_json(agent_path.read_text(encoding="utf-8"))
    except Exception:
        agent_data = {}
    if not isinstance(agent_data, dict):
        # Valid JSON that is not an object (a list, a scalar) parses fine but
        # every .get() below would raise. Doctor exists to diagnose a broken
        # config, not die on one — treat it like the unparseable case, but say
        # what is actually wrong so the missing-server lines below make sense.
        print("  ❌ agent spec is not a JSON object — re-run `kirocrew setup`")
        agent_data = {}

    tools = agent_data.get("tools", [])
    allowed = agent_data.get("allowedTools", [])
    mcps = agent_data.get("mcpServers", {})
    config_changed = False
    # Read-only probe (no SEL write): a declined instance never writes the shared spec.
    declined = _agent._decline_shared_agent_home(audit=False) is not None

    probe_targets = []  # type: ignore[var-annotated]  # checked before the facade it reads
    if gated_off is None:
        gated_off = _doctor_gated_off_mcps()
    for name in _MANAGED_MCPS:
        ref = f"@{name}"
        gate_closed = name in gated_off
        if name not in mcps:
            # An opt-in set is granted per agent, so its absence from THIS spec is
            # the normal state, not a broken install. Say nothing and probe
            # nothing; the always-on servers below are the ones whose absence
            # means `kirocrew setup` did not finish.
            if name in _OPT_IN_MCPS:
                if ref in tools:
                    # Half a grant: the ref mounts a server the spec never
                    # defines, so kiro-cli has nothing to launch. Report it —
                    # repairing it either way would decide a grant for the user.
                    print(
                        f"  {ref}: ⚠️  referenced in tools but absent from mcpServers "
                        "— add the server entry, or drop the ref"
                    )
                continue
            if gate_closed:
                # Spec emission consults this same gate and deliberately omits
                # the entry, so absence is the healthy state here — the hard
                # error below would be unfixable ("re-run setup" writes the
                # same gated spec back). Informational, never an issue. A stale
                # `@ref` in ``tools`` is NOT the opt-in "half a grant" warning:
                # emission deliberately leaves the ref alone when it retracts
                # the entry (a dangling ref mounts nothing, and dropping it
                # would destroy a grant the user may have narrowed by hand), so
                # ref-present-entry-absent is the designed steady state on a
                # gated-off host and advising "add the server entry" would
                # defeat the gate. No governance-ceiling revoke is needed on
                # this path either: with no ``mcpServers`` entry kiro-cli has
                # nothing to launch, so a leftover ``allowedTools`` ref cannot
                # auto-approve anything — the stale-ENTRY branch below is the
                # one window where a grant is live, and the revoke runs there.
                print(
                    f"  {ref}: ℹ️  gated off on this host (feature disabled or "
                    "no driver for this platform) — absent from mcpServers by design"
                )
                continue
            print(f"  {ref}: ❌ missing from mcpServers (re-run `kirocrew setup`)")
            issues.append(f"{ref} config")
            continue
        if not isinstance(mcps.get(name), dict):
            # A hand-written entry that is not an object. Every read below —
            # command, args, env — would raise on it, and doctor exists to
            # diagnose a broken config rather than die on one. An opt-in name is
            # the one a human types, so say what is wrong and move on; a
            # malformed ALWAYS-ON entry is a broken install and counts as an issue.
            print(f"  {ref}: ❌ malformed entry in mcpServers (expected an object)")
            if name not in _OPT_IN_MCPS:
                issues.append(f"{ref} config")
            continue
        if gate_closed:
            # A stale entry from before the gate closed (feature turned off, or
            # a config copied from a host that has a driver). The next config
            # refresh retracts it; until then doctor must not deepen the hole:
            # no mounting the ref (kiro-cli would spawn a backend emission
            # decided against), no minting `allowedTools`, no probe (nothing
            # SHOULD launch). The governance-ceiling revoke below still runs —
            # the entry is live in this spec until the retraction, so an
            # auto-approve exemption would be real for exactly that window.
            print(
                f"  {ref}: ℹ️  gated off on this host (feature disabled or no "
                "driver for this platform) — stale mcpServers entry is "
                "retracted on the next `kirocrew setup` or gateway start"
            )
        elif ref not in tools and name not in _OPT_IN_MCPS:
            # Mounting an opt-in server IS granting it: the `@` ref is what makes
            # kiro-cli load it. Doctor repairs a broken always-on mount, but it
            # must never hand an agent a set the user did not assign.
            tools.append(ref)
            config_changed = True
        elif ref not in tools:
            # The other half: an entry with no ref. kiro-cli loads a server only
            # when something references it, so the tools are unreachable and
            # every other check here would still read clean — the same silent
            # unreachability this opt-in shape exists to avoid. Warn without
            # adding an issue: a deliberately staged entry is a legitimate state,
            # and doctor must not mount it to make itself green.
            print(
                f"  {ref}: ⚠️  defined in mcpServers but not referenced in tools "
                "— unreachable until the ref is added"
            )
        # `allowedTools` auto-approves, which is the one path that never reaches
        # the PreToolUse gate — so what the ceiling says about this server decides
        # both whether doctor may mint a grant and whether an existing one stands.
        # A grant is the bare ref or any per-tool ``@server/tool`` spelling of it.
        granted = any(mcp_ref_owned_by(t, (name,)) for t in allowed)
        # The operator's own ``hooks.confirm_tools`` withholds through the same
        # chokepoint; name it, so it does not read as an enterprise ceiling.
        by_confirm = withheld_by_confirm_tools(ref)
        if not may_skip_gate_now(ref) and granted and declined:
            config_changed = True
            cause = "withheld by hooks.confirm_tools" if by_confirm else "forbidden by ceiling"
            issues.append(f"{ref} auto-approve {cause} (repair from the owning install)")
        elif not may_skip_gate_now(ref):
            # REVOKE, not merely "do not add". A grant can predate the ceiling —
            # the policy arrives on a host whose config was written while it was
            # ungoverned — and leaving it in place means the ceiling applies only
            # to installs that were governed before their first launch. Every
            # other writer of this list revokes here too (agent.py's shared sync,
            # both dashboard enable paths); declining to MINT without also
            # revoking would leave `kirocrew doctor` reporting a repaired config
            # that still carried the exemption.
            #
            # This is the one case where doctor removes something from
            # `allowedTools`: the note below about never removing a user's
            # decision holds for user preference, and a ceiling is not one.
            if granted:
                allowed[:] = without_mcp_refs(allowed, (name,))
                config_changed = True
                # Revoking a grant is a permission DECISION; every other writer of
                # this list emits this SEL event when it withholds, and doctor
                # revoking silently would be the one path with no audit trail.
                try:
                    sel().log_api_access(
                        caller="system",
                        operation="mcp_auto_approve_withheld",
                        outcome="ok",
                        source="cli_doctor",
                        resources=(
                            f"{ref} auto-approve revoked "
                            f"({'hooks.confirm_tools' if by_confirm else 'governance ceiling'}); "
                            "calls go through the approval gate"
                        ),
                    )
                except Exception:  # noqa: BLE001 — the audit must not break doctor
                    logger.debug("SEL audit unavailable for doctor revoke", exc_info=True)
            # Governed hosts otherwise give no reason why a server the user
            # enabled still prompts on every call — say it once, here, so
            # `kirocrew doctor` explains it.
            source = "hooks.confirm_tools" if by_confirm else "security policy"
            print(f"  {ref}: 🔒 auto-approve withheld by {source} — calls will prompt")
        elif ref not in allowed and name not in _NO_BLANKET_ALLOW_MCPS and not gate_closed:
            # Computer use is never blanket-allowed here: see _NO_BLANKET_ALLOW_MCPS.
            # A pre-existing user-made grant is left alone (doctor never REMOVES a
            # decision the user owns); doctor simply never mints one. A gated-off
            # server never gets one minted either: granting auto-approve to a
            # server emission has decided against is the wrong direction.
            allowed.append(ref)
            config_changed = True

        if gate_closed:
            # Nothing should launch: no emitted spec defines this server, so a
            # handshake probe would spawn a backend for a capability that is off
            # or has no driver here — and report its result either way.
            continue
        spec = mcps[name]
        probe_targets.append(
            McpServerInfo(
                name=name,
                command=spec.get("command", ""),
                args=list(spec.get("args", []) or []),
                env=dict(spec.get("env", {}) or {}),
            )
        )

    if config_changed:
        # The verdict that decides the write is the one taken under the agent-spec
        # lock every spec writer of that directory holds, so a gateway that took
        # ownership since the probe above is not overwritten by this auto-fix, and
        # that final verdict is the one audited.
        lock_error = ""
        with contextlib.ExitStack() as spec_lock:
            if not declined:
                try:
                    spec_lock.enter_context(_agent.agents_spec_lock(agent_path.parent))
                except OSError as exc:
                    lock_error = f"agent-spec lock unavailable ({exc})"
                else:
                    declined = _agent._decline_shared_agent_home(audit=False) is not None
            try:  # both verdicts on the shared spec are audited; the audit must not break doctor
                if lock_error:
                    sel().log_api_access(
                        caller="system",
                        operation="agent_home_write",
                        outcome="denied",
                        source="cli_doctor",
                        resources=str(agent_path),
                        error=lock_error,
                    )
                else:
                    sel().log_api_access(
                        caller="system",
                        operation="agent_home_write",
                        outcome="denied" if declined else "allowed",
                        source="cli_doctor",
                        resources=str(agent_path),
                    )
            except Exception:  # noqa: BLE001
                logger.debug("SEL audit unavailable for doctor spec write", exc_info=True)
            if lock_error:
                print(f"  → Auto-fix skipped: {lock_error}")
                issues.append("agent config (auto-fix skipped: agent-spec lock unavailable)")
            elif declined:
                print("  → Auto-fix skipped: shared home")
                issues.append("agent config (auto-fix skipped: shared home)")
            else:
                agent_data["tools"] = tools
                agent_data["allowedTools"] = allowed
                agent_data["mcpServers"] = mcps
                atomic_write(agent_path, json.dumps(agent_data, indent=2) + "\n")
                print("  → Auto-fixed agent config")

    if not probe_targets:
        return

    print("  MCP host probe — session tool loading is not verified by this check.")

    # Every probe below spawns its server through the sandbox chokepoint, and
    # asyncio.gather releases them together. On a cold cache the first arrivals
    # therefore land on the on-loop deferral path simultaneously and each logs a
    # transient probe failure — noise that reads as a real sandbox fault during a
    # health check whose subject is MCP, not the sandbox. Warm the cache here,
    # off any loop, so the probes see a settled verdict.
    #
    # The chokepoint helper is deliberately NOT named here: test_spawn_audit
    # classifies a spawn as sandbox-routed by substring-scanning the enclosing
    # function's source, so spelling that identifier even in a comment flips this
    # function's classification and then demands a resource-limit preexec_fn it
    # does not own. The routing genuinely happens inside
    # mcp_discovery.probe_server, not here.
    #
    # Failing to warm is non-fatal BY DESIGN (the cache stays cold and the
    # self-healing transient path applies), so it must not be able to abort the
    # command. `warm_backend` starts a thread, and `Thread.start()` raises when
    # the process is out of threads — precisely the degraded state someone runs
    # `doctor` to diagnose, which is the worst moment for the diagnostic itself
    # to die. Swallow it here rather than inside the probe `try` below, so a warm
    # failure is never misreported as an MCP probe failure.
    try:
        warm_backend()
    except Exception:
        logger.debug("sandbox probe warm failed; probes will re-probe", exc_info=True)

    try:

        async def _probe_all() -> list:
            return await asyncio.gather(*(probe_server(t) for t in probe_targets))

        probed = asyncio.run(_probe_all())
    except Exception as exc:
        print(f"  ⚠️  probe failed: {exc}")
        return

    for server in probed:
        ref = f"@{server.name}"
        if server.status == "ok":
            count = len(server.tools)
            noun = "tool" if count == 1 else "tools"
            print(f"  {ref}: ✅ {count} {noun}")
            continue
        head, _, detail = (server.error or "unknown error").partition("\n")
        print(f"  {ref}: ❌ {head or 'unknown error'}")
        if detail:
            for line in detail.splitlines():
                print(f"      {line}")
        issues.append(f"{ref} probe")


def _doctor_whoami_governance() -> bool | None:
    """Ask kiro-cli itself whether governance can reach the signed-in identity.

    The store probe behind ``mcp_governance_may_apply`` counts the
    ``auth.idc.*`` rows in ``state``, and some kiro-cli builds sign an Identity
    Center user in without writing them: the sign-in then lives only in
    ``auth_kv``, under the same ``kirocli:odic:`` key names a Builder ID sign-in
    uses, so no key-name check can tell the two apart. ``kiro-cli whoami``
    names the account type directly and no store value is read here.

    ``True`` for Identity Center or an API key, ``False`` for Builder ID, a
    social sign-in or no sign-in at all, and ``None`` when nothing is known
    (whoami would not run or answer, or named a type this code does not know).
    """
    from kiro_crew.cloud.login_target import (
        ACCOUNT_TYPE_API_KEY,
        ACCOUNT_TYPE_IDENTITY_CENTER,
        discover_local_identity,
        is_personal_account_type,
    )

    identity = discover_local_identity()
    if identity is None:
        return None
    account_type = str(identity.get("account_type") or "")
    if account_type in (ACCOUNT_TYPE_IDENTITY_CENTER, ACCOUNT_TYPE_API_KEY):
        return True
    if not account_type or is_personal_account_type(account_type):
        return False
    return None


def _doctor_mcp_governance(
    agent_path: Path, issues: list[str], *, gated_off: "frozenset[str] | None" = None
) -> None:
    """Render the `MCP Governance` section of `kirocrew doctor`.

    Speaks up in two situations: governance can reach this identity (Identity
    Center or an API key), where an administrator's registry may be in force, and
    the registry declaration or its markers are present on an identity governance
    CANNOT reach, which is the inverse failure and just as silent. Stays quiet on
    an ordinary personal install, where a governance warning would be pure noise.

    This exists because the section above cannot detect either failure.
    Governance is enforced inside kiro-cli when it assembles a session: it drops
    every ``mcpServers`` entry whose registry marker does not match the account's
    access mode. Kiro Crew's own handshake probe spawns each server directly and
    therefore still reports it healthy, so an affected host reads green here
    while `spawn_run`, `cron_add` and `learn_add` are absent from every session.
    """
    try:
        declared = KiroCrewConfig.load().agent.mcp_registry_mode
    except Exception:
        logger.debug("config load failed in governance check", exc_info=True)
        declared = False

    # Same hardened reader as the Model section's spec reads: the agents dir is
    # user-writable, so an oversized or sensitively-symlinked spec is refused
    # (and audited) rather than parsed. No try/except: the reader's contract is
    # return-``None``-never-raise, which the sibling sites also rely on bare.
    # ``None`` degrades to no declared servers, which is what a blanket
    # ``except`` here would do.
    spec = _read_agent_spec(agent_path, operation="doctor", source="cli")
    servers = (spec or {}).get("mcpServers") or {}
    if not isinstance(servers, dict):
        # `or {}` only replaces a FALSY value, so a string or list here survives
        # and the membership walk below would raise, aborting the whole doctor
        # run — on exactly the malformed spec someone is running doctor to find.
        servers = {}

    # What a governed spec OUGHT to declare: every always-on server, plus the
    # opt-in sets this spec actually grants. Counting an unassigned opt-in server
    # would report every governed install as half-marked; dropping the always-on
    # ones from the denominator would make a spec that declares NOTHING — a
    # malformed or emptied ``mcpServers`` — read as fully marked, which is the
    # exact failure this section exists to catch. One exception, same rule as
    # the MCP Tools section above: an always-on server whose spec gate is
    # closed is deliberately absent from every emitted spec, so demanding a
    # registry marker for it would re-create the unfixable "re-run setup" loop.
    # A STALE entry still counts while it exists — kiro-cli drops an
    # unmarked entry at session assembly, so the marker matters for exactly as
    # long as the entry does.
    if gated_off is None:
        gated_off = _doctor_gated_off_mcps()
    expected = [
        name
        for name in _ALWAYS_ON_MCPS
        if isinstance(servers.get(name), dict) or name not in gated_off
    ] + [name for name in _OPT_IN_MCPS if isinstance(servers.get(name), dict)]
    marked = sorted(
        name
        for name in expected
        if isinstance(servers.get(name), dict) and servers[name].get("type") == "registry"
    )
    names = ", ".join(sorted(expected))
    governed_capable = mcp_governance_may_apply()

    # Nothing to say: an identity governance cannot reach, with no registry
    # declaration and no leftover markers, is the ordinary case.
    if not governed_capable and not declared and not marked:
        return

    if not governed_capable:
        # The store probe found no Identity Center rows and no API key is set.
        # That is not proof of a personal account, so confirm with kiro-cli
        # before advising the declaration off: only reached when a declaration
        # or markers exist, so an ordinary personal install never pays for it.
        via_whoami = _doctor_whoami_governance()
        if via_whoami is None:
            print("\nMCP Governance (enterprise):")
            print(
                "  identity: unknown — no Identity Center rows in kiro-cli's store, "
                "and `kiro-cli whoami` did not say which account this is"
            )
            if declared:
                print("  ⚠️  registry mode is declared")
            else:
                print("  ⚠️  registry markers are present on the spec without the declaration")
            print(
                "      run `kiro-cli whoami`. On a Builder ID or social sign-in kiro-cli "
                "drops these servers; fix:  kirocrew config set agent.mcp_registry_mode false"
            )
            print(f"      affected: {', '.join(marked) if marked else names}")
            issues.append("MCP governance identity unknown")
            return
        governed_capable = via_whoami

    print("\nMCP Governance (enterprise):")

    if not governed_capable:
        # The inverse filter. Outside registry mode a MARKED entry is the one the
        # client drops, so this state breaks the same servers, equally silently —
        # reachable by copying the guide onto a personal account, or by leaving an
        # enterprise account with the declaration still set. Safe to assert only
        # because neither governance-capable signal is present: no Identity Center
        # rows AND no API key, which leaves Builder ID or social sign-in.
        print("  identity: not Identity Center or API key — an admin MCP registry cannot apply")
        if declared:
            print(
                "  ❌ registry mode is declared, so kiro-cli treats these servers as "
                "registry-provided and drops them on an ungoverned account"
            )
        else:
            print("  ❌ registry markers are present on the spec without the declaration")
        print(f"      affected: {', '.join(marked) if marked else names}")
        print("      fix:  kirocrew config set agent.mcp_registry_mode false")
        issues.append("MCP registry mode on non-IDC account")
        return

    print("  identity: Identity Center or API key — an admin MCP registry can apply")
    if declared:
        print(f"  registry mode: on — {len(marked)}/{len(expected)} managed servers marked")
        if len(marked) < len(expected):
            print("  ❌ markers missing — re-run `kirocrew setup --agent-only`")
            issues.append("MCP registry markers")
            return
        # Deliberately not a success line. Whether the administrator actually
        # allow-listed these names is not knowable locally, so claiming green
        # here would repeat the overstatement this section exists to correct.
        print("  cannot verify the registry itself — that lives with your administrator")
        print(f"      these names must be allow-listed, exactly: {names}")
        print(
            "      if tools are still missing in sessions, the account may no longer be "
            "registry-governed — try `kirocrew config set agent.mcp_registry_mode false`"
        )
        return

    print("  registry mode: off")
    print(
        "  ⚠️  If MCP tools are missing in sessions while probing OK above, your "
        "administrator has configured an MCP Registry URL. In that mode kiro-cli "
        "connects only to servers marked 'type': \"registry\"."
    )
    print("      Declare it:  kirocrew config set agent.mcp_registry_mode true")
    print(f"      Then have your admin allow-list, by these exact names: {names}")
