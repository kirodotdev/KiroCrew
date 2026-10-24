"""The KAS relay rows of ``kirocrew doctor``, shown when KAS is the selected backend.

:func:`_doctor_kas` reports whether the installed kiro-cli's ``acp`` relay can serve the
KAS engine: the bundle, the relay's help text, the spec permissions it needs and the
builtin tool vocabulary it was measured against. On the bundle route
(``agent.kas_bundle`` on) :func:`_report_kas_bundle` reports Crew's own installed
engine instead -- its version against the release pin, the ``install.json`` entry
points, the spawn argv and the vault as the only auth owner -- and asks nothing of
kiro-cli. The ACP names these rows read stay imported by ``kiro_crew.cli_doctor``, so
this module imports no ``kiro_crew.acp`` module. New KAS rows go here.

Composed onto ``kiro_crew.cli_doctor``, the command's one import path and patch target;
see :mod:`kiro_crew.cli_doctor_parts`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.cli_doctor import (
        ACP_BACKEND_KAS,
        KAS_RELAY_ENGINE,
        KIRO_CLI_BIN,
        PATH_ONLY_INSTALL_NOTE,
        SPEC_PERMISSIONS_MIN_VERSION,
        KiroCrewConfig,
        build_kas_argv,
        installed_kiro_cli_version,
        kas_engine_gap,
        kas_relay_help,
        kas_verified_releases,
        kas_vocabulary_drift,
        kiro_cli_installed,
        resolve_kiro_cli,
        spec_permissions_supported,
    )


def _kas_relay_help(binary: str) -> str | None:
    """``acp --help`` text for this kiro-cli, or ``None`` when the probe FAILED.

    The probe is :func:`kiro_cli.kas_relay_help` and the reading is
    :func:`agent_sdk.kas_engine_gap`, both shared with the runtime floor
    (``agent_sdk.kas_engine_unsupported_reason``), so the doctor and the gate
    cannot read the help text differently.
    """
    return kas_relay_help(binary)


def _doctor_kas(issues: list[str]) -> None:
    """Report KAS backend readiness, but only when KAS is the selected backend.

    KAS is the default harness (an absent or ``""`` ``agent.acp_backend``);
    when another harness is selected this is silent so a kiro-cli / Claude Code
    install sees no KAS noise. When it IS
    selected, KAS is served by kiro-cli's own ACP relay (see
    :mod:`kiro_crew.acp.kas_transport`), so the thing that makes a selected KAS
    backend fail at session-create time is a kiro-cli whose ``acp`` subcommand
    cannot select the KAS engine. The only credential read here is Crew's own
    vault -- the same read the runtime makes to pick the spawn's auth owner:
    the relay resolves tokens from the vault when it holds a usable identity
    and from kiro-cli's own store otherwise, and the sign-in rows above report
    that same decision.
    """
    # Positive backend test (not ``!= ACP_BACKEND_KAS``): an inequality would
    # silently capture every harness added later — see the harness-parity gate.
    if KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS:
        _report_kas_backend(issues)


def _report_kas_bundle(issues: list[str]) -> None:
    """The bundle route: Crew's own installed engine, no kiro-cli row at all.

    The route never runs kiro-cli, so a missing kiro-cli is not a problem here,
    and reporting one would send the operator to install a binary nothing will
    use. The one place an installed kiro-cli still matters is the auto-approve
    row: it reads the same spec files. Crew is the auth owner unconditionally, so
    the vault row is the only credential row.
    """
    from kiro_crew.agent_sdk import kas_bundle

    installed = kas_bundle.installed_bundle()  # type: ignore[misc]  # checked before kas_bundle
    if installed is None:
        print(f"  bundle:      ❌ nothing installed under {kas_bundle.kas_engines_root()}")  # type: ignore[misc]
        print("               Fix: kirocrew kas install   (or set agent.kas_bundle false)")
        issues.append("agent.kas_bundle is on but no KAS engine bundle is installed")
        _report_kas_bundle_spec_permissions(None, issues)
        return
    where = " (built into the app)" if installed.bundled else ""
    print(f"  bundle:      ✅ KAS {installed.version}, Node {installed.node_version or '?'}{where}")
    _report_kas_bundle_pin(installed.version)
    # The two entry points ``install.json`` records, each on its own row: the
    # spawn row below joins them into one argv, which is the right thing to copy
    # into a terminal and the wrong thing to read a path out of.
    print(f"  install.json: {installed.root / kas_bundle.KAS_BUNDLE_INSTALL_RECORD_NAME}")
    print(f"  engine:      {installed.acp_server}")
    print(f"  node:        {installed.node_bin}")
    print(f"  spawn:       {' '.join(kas_bundle.bundle_spawn_argv(installed))}")
    print("  auth owner:  Kiro Crew vault (the only owner on this route)")
    try:
        from kiro_crew.auth.bridge import describe_vault_identity, vault_holds_identity

        signed_in = vault_holds_identity()
        identity_line = describe_vault_identity()
    except Exception:
        signed_in = False
        identity_line = None
    if identity_line:
        print(f"  crew vault:  {identity_line}")
    # Ownership and health are separate facts: vault_holds_identity reads only
    # the token; the describe verdict is the only read that sees issuer rejection.
    healthy = identity_line is not None and identity_line.endswith("-> usable")
    if signed_in and healthy:
        print("  crew vault:  ✅ signed in through Kiro Crew")
    elif signed_in:
        print(
            "  crew vault:  ⚠️  signed in through Kiro Crew, but the sign-in may not be usable;"
            " see the line above"
        )
        print(f"               {kas_bundle.KAS_BUNDLE_SIGN_IN_REMEDY}")
    else:
        print("  crew vault:  ⚠️  not signed in; the first prompt will ask for a sign-in")
        print(f"               {kas_bundle.KAS_BUNDLE_SIGN_IN_REMEDY}")
    _report_kas_bundle_spec_permissions(installed.version, issues)


def _report_kas_bundle_spec_permissions(version: str | None, issues: list[str]) -> None:
    """Whether the spec ``permissions`` block is written on the bundle route.

    The bundle route's spelling of :func:`_report_kas_spec_permissions`, reporting
    the writer's rule (``auto_approve._spec_permissions_accepted``) from the same
    predicates: the installed engine must pass the pin's
    ``engine_spec_permissions_supported``, and an installed kiro-cli must also pass
    its own gate, since it reads the same files. *version* is the installed
    engine's, or ``None`` for no engine: an unknown version, which the gate reads
    as withheld, so the row says so instead of staying silent -- without a second
    issue, since the bundle row already names the one fix, the way the relay
    route's missing-kiro-cli row stands alone.
    """
    from kiro_crew.agent_sdk import kas_bundle

    if kas_bundle.engine_spec_permissions_supported(version):
        if _report_bundle_kiro_cli_refusal(issues):
            return
        print(
            "  auto-approve: ✅ spec `permissions` block written"
            f" (the bundled engine KAS {version} reads it)"
        )
        return
    floor = ".".join(str(part) for part in kas_bundle.KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION)
    if version is None:
        print("  auto-approve: ⚠  spec `permissions` block withheld: no engine bundle installed")
        print("               It is written once `kirocrew kas install` lays the engine down.")
        return
    print(f"  auto-approve: ❌ withheld: the bundled engine KAS {version} is below {floor},")
    print("               the lowest engine verified to accept the field. Fix: run the")
    print(f"               pinned engine ({kas_bundle.KAS_PIN_VERSION}; unset")
    print(f"               {kas_bundle.KAS_VERSION_OVERRIDE_ENV} or install it).")
    issues.append("the bundled KAS engine is too old to carry the KAS `permissions` block")


def _report_bundle_kiro_cli_refusal(issues: list[str]) -> bool:
    """Print the withheld row when an installed kiro-cli refuses the block; say if it did.

    No kiro-cli installed prints nothing: the engine alone decides. Whether one
    is installed is ``kiro_cli.kiro_cli_installed``, the writer's own predicate,
    so the two cannot disagree.
    """
    if not kiro_cli_installed():
        return False
    cli_version = installed_kiro_cli_version()
    if spec_permissions_supported(cli_version):
        return False
    floor = ".".join(str(part) for part in SPEC_PERMISSIONS_MIN_VERSION)
    shown = "unknown" if cli_version is None else ".".join(str(p) for p in cli_version)
    print(f"  auto-approve: ❌ withheld: kiro-cli ({shown}) is installed and reads the")
    print("               same specs, and it may refuse the field. Fix: update kiro-cli")
    print(f"               to {floor} or newer, or uninstall it.")
    if cli_version is None:
        print(f"               ({PATH_ONLY_INSTALL_NOTE}).")
    issues.append("kiro-cli is installed but cannot carry the KAS `permissions` block")
    return True


def _report_kas_bundle_pin(installed_version: str) -> None:
    """The installed engine against this release's pin.

    ``installed_bundle()`` looks up exactly the version ``pinned_version()`` names,
    so the installed version can differ from ``KAS_PIN_VERSION`` only through the
    ``KIROCREW_KAS_VERSION`` override; that is the one case worth a ⚠️, because it
    is the gateway's environment choosing an engine this release was not tested
    with, and the row names the variable so the operator can find it. Every other
    installed version (an older release's engine, kept so a rollback finds its
    own) is listed, not judged.
    """
    from kiro_crew.agent_sdk import kas_bundle

    pin = kas_bundle.KAS_PIN_VERSION
    if installed_version == pin:
        print(f"  pin:         ✅ KAS {pin} is this release's pin")
    else:
        print(f"  pin:         ⚠️  running KAS {installed_version}; this release pins {pin}")
        print(
            f"               ({kas_bundle.KAS_VERSION_OVERRIDE_ENV} is set in the gateway's"
            " environment; unset it to run the pin)"
        )
    try:
        versions = kas_bundle.installed_versions()  # type: ignore[misc]  # PEP 562 re-export
        others = [v for v in versions if v != installed_version]
    except Exception:
        others = []
    if others:
        print(f"  also installed: {', '.join(others)} (kept for rollback; `kirocrew kas remove`)")


def _report_kas_backend(issues: list[str]) -> None:
    """Print the KAS diagnostic block (relay binary + engine support).

    Split from :func:`_doctor_kas` so the backend-selection check there stays a
    positive ``== ACP_BACKEND_KAS`` rather than an early-return on inequality.
    """
    print("\nKAS backend")
    if KiroCrewConfig.load().agent.kas_bundle:
        _report_kas_bundle(issues)
        return
    binary = resolve_kiro_cli()
    if not binary:
        print(f"  relay:       ❌ {KIRO_CLI_BIN} not found")
        print("               Fix: install kiro-cli; it serves KAS over its acp relay.")
        issues.append("KAS backend selected but kiro-cli is not installed")
        return

    # Same decision the runtime makes at spawn: Crew owns auth when its own
    # vault holds an identity, kiro-cli otherwise. Reported so the operator sees
    # which credential the next KAS process will actually draw on. Deferred
    # import: this module is on the dashboard's boot path and kiro_crew.auth
    # brings the cryptography wheel with it (see kas_host_auth's module doc).
    # An import or probe failure leaves the diagnostic on the kiro-cli path
    # rather than ending the whole doctor report.
    try:
        from kiro_crew.auth.bridge import describe_vault_identity, vault_holds_identity

        host_auth = vault_holds_identity()
        identity_line = describe_vault_identity()
    except Exception:
        host_auth = False
        identity_line = None

    print(f"  relay:       ✅ {' '.join(build_kas_argv(binary, host_auth=host_auth))}")
    print(
        "  auth owner:  "
        + (
            "Kiro Crew vault (signed in through Kiro Crew)"
            if host_auth
            else "kiro-cli credential store (--auth-method cli)"
        )
    )
    # The fields the owner decision reads, so a vault that will fail its first
    # callback (expired, nothing to renew it) is visible here rather than as a
    # broken spawn. Printed whenever something is stored, including the case the
    # probe rejected -- that is exactly the one worth seeing.
    if identity_line:
        print(f"  crew vault:  {identity_line}")
    # Before the help probe, not after: the row reads ``--version``, which is a
    # different spawn from ``acp --help``, so a failed help probe establishes
    # nothing about it. The early ``return`` below is for the ENGINE rows alone;
    # letting it swallow this row would hide a withheld auto-approve on exactly
    # the host where kiro-cli is misbehaving.
    _report_kas_spec_permissions(issues)
    _report_kas_tool_vocabulary(issues)
    help_text = _kas_relay_help(binary)
    if help_text is None:
        # The probe itself failed, so nothing is known either way. Advisory: a
        # diagnostic must not invent a verdict it could not establish.
        print("  engine:      ⚠️  could not read `acp --help`; engine support unknown")
        return
    # The same reading the gateway's boot floor acts on: a gap is definite.
    gap = kas_engine_gap(help_text)
    if gap is None:
        print(f"  engine:      ✅ {KAS_RELAY_ENGINE} supported")
    else:
        print(f"  engine:      ❌ {gap}")
        print("               Fix: update kiro-cli, or switch agent.acp_backend to kiro.")
        issues.append(f"kiro-cli cannot serve the KAS engine: {gap}")
    # Reported from the SAME decision as the ``auth owner:`` line above, so the
    # two cannot disagree: the relay resolves every access token from whichever
    # store owns the spawn -- Crew's vault when it holds a usable identity,
    # kiro-cli's own store otherwise. The detail for each store lives in the
    # sign-in rows and the ``crew vault:`` line rather than being restated here.
    if host_auth:
        print("  token:       ➖ Kiro Crew vault sign-in (see the auth owner line above)")
    else:
        from kiro_crew.agent_sdk import entitlement_label

        print(
            f"  token:       ➖ {entitlement_label(ACP_BACKEND_KAS)} "
            "(see the sign-in rows above)"
        )


def _report_kas_spec_permissions(issues: list[str]) -> None:
    """Whether this kiro-cli can carry the spec ``permissions`` block KAS reads.

    The block is how Crew's auto-approve list reaches KAS's policy engine, and it
    is written only when the installed kiro-cli accepts the field: that binary
    validates specs with serde ``deny_unknown_fields``, so a release predating the
    field refuses the WHOLE spec and drops every Crew MCP server from the session.
    Withholding it is the smaller loss, but it IS a loss, and this is the only
    place it is visible. Reported inside the KAS block rather than
    beside the model rows because it costs nothing until KAS is the selected
    backend -- which is exactly when this block prints.
    """
    version = installed_kiro_cli_version()
    if spec_permissions_supported(version):
        print("  auto-approve: ✅ spec `permissions` block written (KAS reads it)")
        return
    floor = ".".join(str(part) for part in SPEC_PERMISSIONS_MIN_VERSION)
    if version is None:
        # Not "too old": the version could not be read at all, most often because
        # kiro-cli resolves only through PATH and the probe spawns pinned paths
        # only. The writer withholds a NEW block here but keeps one already on
        # disk, so the remedy is to make the binary probeable, not to update it.
        print("  auto-approve: ⚠️  spec `permissions` block not seeded: kiro-cli version unknown")
        print(f"               ({PATH_ONLY_INSTALL_NOTE}). A block already on disk is kept.")
        issues.append("kiro-cli version unknown, so the KAS `permissions` block is not seeded")
        return
    shown = ".".join(str(part) for part in version)
    print(f"  auto-approve: ❌ withheld: this kiro-cli ({shown}) refuses the field")
    print("               It validates specs with deny_unknown_fields, so writing " "`permissions`")
    print("               would make the whole spec unreadable and drop every Kiro " "Crew MCP")
    print(f"               server. Fix: update kiro-cli to {floor} or newer. If the spec")
    print("               already carries the block, `kirocrew setup --agent-only --clean`")
    print("               rebuilds it without the key.")
    issues.append("kiro-cli is too old to carry the KAS `permissions` block")


def _report_kas_tool_vocabulary(issues: list[str]) -> None:
    """Whether the installed engine is one the KAS tool-vocabulary tables were read from.

    Crew mounts a spec's kiro-cli tool names on KAS by translating them to KAS's
    built-in ids and governs KAS ids under their kiro-cli names
    (``platform.tool_names``). Both directions were measured on ONE engine
    release; any other kiro-cli ships a different KAS, which may rename a
    built-in, add one, or (older) lack one. The mount direction fails silent
    (an id the engine does not recognise is dropped from the spec's tool set
    without an error, so a read tool is simply absent from the agent), the
    policy direction fails permissive (an id the table does not know is
    governed only under its own name), so a mismatch in either direction is
    worth one line here. Advisory: another release is not known to have
    drifted, only not known not to have.
    """
    version = installed_kiro_cli_version()
    verified = kas_verified_releases()
    drift = kas_vocabulary_drift(version)
    if drift == "unknown" or version is None:
        print(
            f"  tool names:  ➖ vocabulary verified on kiro-cli {verified}; installed version unknown"
        )
        return
    shown = ".".join(str(part) for part in version)
    if not drift:
        print(f"  tool names:  ✅ vocabulary verified on kiro-cli {verified} (installed {shown})")
        return
    if drift == "older":
        # Unverified, not compatible: the tables were read from the verified
        # releases and an older KAS may lack an id or spell it differently. Same
        # standing as a newer one -- only the direction of the drift risk differs.
        print(
            f"  tool names:  ⚠️  installed kiro-cli {shown} is older than every release ({verified})"
        )
        print("               the KAS tool-name tables were verified on; not verified against it.")
        print("               A built-in the tables name may be absent or spelled differently.")
        return
    relation = "newer than every release" if drift == "newer" else "not among the releases"
    print(f"  tool names:  ⚠️  installed kiro-cli {shown} is {relation} ({verified}) the KAS")
    print("               tool-name tables were verified on. A renamed or added built-in")
    print("               would mount or be governed under a name Crew does not know.")
    print("               Re-measure: test/fixtures/kas_builtin_tool_ids.json (its note says how),")
    print("               then append the release to its verified_kiro_cli_versions.")
    print("               The kas backend logs this once per engine version when it spawns the")
    print("               relay, reading this same pinned binary.")
