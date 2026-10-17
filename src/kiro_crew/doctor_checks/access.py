"""Access rows of ``kirocrew doctor``: session signing, dashboard signing key, hook
auto-approve, credentials.

Three are advisory: each fail-closed answer is the intended posture, so none of
them joins the issues that decide the exit code. The dashboard signing-key row is
the exception: its ``short`` state is the one the gateway boot preflight refuses,
so it joins ``issues`` and ``kirocrew doctor && kirocrew gateway`` stops where the
boot would.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import render


def _doctor_trust_root() -> None:
    """Report whether session identities can be signed, and from which file.

    A gateway whose SEL trust root stops resolving keeps signing its audit
    chain from bytes cached at init, so nothing looks wrong — while every
    ``session_pid`` mapping goes out unsigned and the MCP tools that need a
    verified session are refused. Publication logs that once per process, but
    only once a session is actually claimed; asking here needs no claim.

    Read-only on purpose: it never constructs ``SecurityEventLog``, so a
    missing key is reported rather than created as a side effect of the
    question.
    """
    ok, key_path = cli_doctor.signing_health()
    if ok:
        print(f"  trust root:  ✅ {key_path}")
        return
    if not key_path.parent.is_dir():
        # The trust dir and the key are created together, on the first
        # SecurityEventLog init. Neither present means no instance has ever run
        # against this home — a fresh install, not a broken one.
        print(f"  trust root:  ⏹ {key_path} not created yet (the gateway writes it on first start)")
        return
    print(f"  ⚠ trust root: {key_path} is unreadable or shorter than 32 bytes.")
    print("               Session identities go out unsigned, so sub-agent " "dispatch and memory")
    print("               writes are refused in sandboxed sessions. Restore the " "key file, or")
    print("               restart the gateway if another process relocated it.")


def _doctor_signing_key(issues: list[str]) -> None:
    """Report the dashboard signing key file's shape: present, absent, or too short.

    A ``token_signing.key`` shorter than 32 bytes is never accepted, so the
    gateway refuses to start on it (see ``slack.gateway.signing_key_preflight``)
    and prints the manual repair steps. This reports the same state where the
    operator is already looking, with the same steps.

    Read-only, like the trust-root check above: ``lstat`` only, never creates or
    moves the file. The macOS kernel's Seatbelt verdict is passed in as extra
    confinement evidence, because ``cli.main()`` pops the launcher marker before
    this runs and the key module's own probe covers only Linux.
    """
    # Read at call time, not bound at module scope: a family binds no project module
    # but the facade (test_cli_doctor_refactor_family_reads.py).
    from kiro_crew.dashboard import token_secret

    state, key_path = token_secret.signing_key_health(
        confined=cli_doctor.sandbox.agent_confinement_evidence() is not None
    )
    if state == "masked":
        # The agent sandbox bind-mounts an empty file over the key, so a size read
        # here would say "truncated" about a healthy key and hand back a remedy
        # that deletes the real one by hand. Say it cannot be seen from here instead.
        print(
            f"  signing key: ⏹ {key_path} is masked inside the agent sandbox; run doctor from the host"
        )
        return
    if state == "ok" and token_secret.open_is_denied(key_path):
        # A whole key this process may not open: the boot's load falls back to an
        # ephemeral secret on it and the gateway still starts, logging every
        # dashboard session out at each restart. Doctor counts it so the operator
        # fixes the mode instead. Never removed.
        issues.append("token signing key cannot be read; the gateway starts on an ephemeral secret")
        print(f"  ⚠ signing key: {key_path} exists but this process cannot open it.")
        print("               The gateway starts on an ephemeral secret instead, which logs")
        print("               every dashboard session out at each restart. Check its owner")
        print("               and mode (a regular file, mode 0600, owned by the gateway's user).")
        return
    if state == "ok":
        print(f"  signing key: ✅ {key_path} (regular file, at least 32 bytes)")
        return
    if state == "unstatable":
        # The lstat itself failed (EIO, ESTALE on a network mount). If the boot's
        # load cannot read past it either, the gateway starts on an ephemeral
        # secret, so it is counted.
        issues.append(
            "token signing key cannot be stat'ed; the gateway may start on an ephemeral secret"
        )
        print(f"  ⚠ signing key: {key_path} could not be stat'ed (an I/O or mount error).")
        print("               If the gateway cannot read the key either, it starts on an")
        print("               ephemeral secret that logs every dashboard session out.")
        print("               Check that the data home is reachable, then retry.")
        return
    if state == "absent":
        print(f"  signing key: ⏹ {key_path} not created yet (the gateway writes it on first start)")
        return
    if state == "short" and token_secret.open_is_denied(key_path):
        # Short AND unreadable by this process: inside an agent sandbox whose
        # confinement signals both missed, this is the mode-0 mask, and a removal
        # step relayed from here would destroy the healthy host key. On the host
        # it is a truncated key nobody can read. The two cannot be told apart from
        # here, so nothing is removed and it is not counted as an issue: counting
        # it would fail doctor against a healthy key in the sandbox case. The boot
        # preflight, which runs outside the sandbox, still refuses a real one.
        print(f"  ⚠ signing key: {key_path} is shorter than 32 bytes and this process")
        print("               cannot open it. Inside an agent sandbox this is the mask over")
        print("               the real key: run doctor from the host. On the host, check the")
        print("               file's owner and mode before changing anything.")
        return
    if state == "short":
        # Counted as an issue so `kirocrew doctor && kirocrew gateway` stops here
        # with a non-zero exit: this is the exact state the boot preflight refuses.
        issues.append("token signing key is truncated; the gateway refuses to start on it")
        print(f"  ⚠ signing key: {key_path} is shorter than 32 bytes (a truncated create or copy).")
        print("               The gateway refuses to start on it; every restart would otherwise")
        print("               log every dashboard session out. Repair it by hand:")
        for step in token_secret.signing_key_remedy(
            key_path, cli_doctor.common_service.restart_command_hint()
        ):
            print(f"               {step}")
        return
    print(f"  ⚠ signing key: {key_path} is not a regular file or cannot be read.")
    print("               Restore the key file, or remove the link or directory at that path.")


def _doctor_name_grant_platform_scope() -> None:
    """Report whether hook auto-approve can be satisfied on this host.

    A user reading decline lines in the log cannot tell a host-wide reason from
    their own misconfiguration; what they would have to read to find out is the
    source of :mod:`kiro_crew.name_grant`. This says it where they are already
    looking for what their install can and cannot do. Three answers:

    * the platform-scope code (Windows could not report the Documents folder,
      so the PowerShell profile check cannot run) -- a property of the host;
    * a Windows environment refusal (a per-user PowerShell profile exists) --
      the user can act on it, so the path is printed;
    * or grants can be satisfied.

    Not a failure, so it never joins *issues*: each fail-closed answer is the
    intended posture. Imported locally to keep ``kirocrew doctor`` from pulling
    a security module in on every invocation just to print one row.
    """

    from kiro_crew import name_grant

    notice = name_grant.platform_scope_notice()
    if notice is not None:
        print(f"  hook auto-approve:  ⏹ declined on this host ({notice})")
        render._print_wrapped(
            "This is the platform's scope, not your configuration. Windows could "
            "not report where the user's Documents folder is, so this check cannot "
            "tell whether a PowerShell profile runs before each command and "
            "declines every name grant. Hooks that auto-approve on macOS and "
            "Linux go to the approval card instead."
        )
        return
    refusal = name_grant.environment_refusal()
    if refusal is not None:
        print(f"  hook auto-approve:  ⚠ declined by this environment ({refusal.code})")
        # The profile is the one state with a remedy a user can be told in a
        # word. The others -- a relative `PATH` entry, an inherited `BASH_ENV`
        # or exported shell functions -- are named in the detail, which says
        # which one it is; a generic sentence there beats naming the wrong file.
        if cli_doctor.platform_compat.IS_WINDOWS and refusal.code == name_grant.AMBIGUOUS_ENV:
            remedy = "remove or rename the profile to restore them."
        else:
            remedy = "clear the environment state named above to restore them."
        render._print_wrapped(
            refusal.detail + ". Hooks that would auto-approve go to the approval "
            "card while this holds; " + remedy
        )
        return
    print("  hook auto-approve:  ✅ name grants can be satisfied on this platform")


#: Where an operator can actually READ the packaged blocked-commands doc.
#: Deliberately a GitHub URL rather than a dashboard page or a repo-relative
#: path: the dashboard has no Docs surface (packaged docs are reached as GitHub
#: links, the base `TipCard` uses), and `src/kiro_crew/...` does not exist on a
#: host that installed the wheel. A pointer an operator cannot follow costs more
#: trust than no pointer, and this line prints on every run where ~/.aws exists.
_BLOCKED_COMMANDS_DOC_URL = (
    "https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/blocked-commands.md"
)


def _doctor_credentials(issues: list[str]) -> None:
    """Report the AWS / credential posture the agent will actually see.

    Exists because "my agent cannot reach AWS" had no self-service answer: the
    agent is allowed to run AWS CLI calls but not to read credential files, so a
    refused read looks identical to having no credentials at all, and nothing on
    either side of that told the operator which one they had.

    Advisory only, like the pod-session-bus and memory-pressure probes: ``issues``
    is doctor's exit-code channel, and an unconfigured AWS profile is not a Kiro
    Crew fault. Reporting it is right; failing on it would make ``doctor`` red on
    every host that simply does not use AWS.

    No secret value is read or printed, and nothing under ``~/.aws`` is OPENED:
    the two files are probed for existence, and the profile set and refresh
    posture come from ``aws configure``, the sanctioned path the guidance itself
    names. A diagnostic that parsed the fenced file would be the "different
    reader" this feature talks the agent out of looking for.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    print("\nCredentials")
    aws_dir = Path.home() / ".aws"
    has_config = (aws_dir / "config").is_file()
    has_creds = (aws_dir / "credentials").is_file()
    if not has_config and not has_creds:
        print("  aws:         ⏹ no ~/.aws config — agents can still run AWS CLI calls")
        render._print_wrapped(
            "once you configure one: the SDK resolves credentials itself, so the agent "
            "never needs to read the files. If you use AWS, run `aws configure sso` or "
            "`aws configure` in your own terminal."
        )
    else:
        # Resolved once and shared by the "could not ask" branches below: the
        # verdicts cannot change between them, and a second probe would only risk
        # the two lines disagreeing with each other.
        #
        # "Could not ask" has exactly four causes, and each needs its own
        # sentence, because every one of them makes a DIFFERENT statement true:
        # no CLI on the host, a CLI the path checks refuse, a CLI on PATH that
        # lives outside the trusted directories altogether (the per-user
        # Windows install), and a CLI that resolved and then would not
        # run. Collapsing any of them onto "install the AWS CLI" tells an
        # operator who has one to install it -- the same confident wrong answer
        # this section was opened to remove.
        declined_cli = cli_doctor.platform_compat.aws_bin_declined_on_ownership()
        resolved_cli = cli_doctor.platform_compat.trusted_aws_bin()
        outside_cli = (
            None
            if declined_cli or resolved_cli
            else cli_doctor.platform_compat.tool_outside_trusted_dirs("aws")
        )
        profiles = cli_doctor._aws_profile_names()
        if profiles:
            shown = ", ".join(render._safe_display(name) for name in profiles[:6])
            extra = f" (+{len(profiles) - 6} more)" if len(profiles) > 6 else ""
            print(f"  profiles:    ✅ {shown}{extra}")
        elif profiles is None:
            # No aws CLI to ask, and the config file is not ours to read — so the
            # honest report is that the files exist and the profile set is unknown.
            # Which of the three causes it was decides the sentence; see the note
            # where `declined_cli` and `resolved_cli` are resolved.
            if declined_cli:
                print(
                    f"  profiles:    ℹ️  ~/.aws present; {render._safe_display(declined_cli)} is not a trusted local copy, so it is not asked"
                )
            elif resolved_cli:
                print(
                    f"  profiles:    ℹ️  ~/.aws present; {render._safe_display(resolved_cli)} did not answer, so the profile set is unknown"
                )
            elif outside_cli:
                # Present on PATH, but outside the trusted system directories
                # (the per-user AWS CLI v2 install on Windows). Not run, by
                # policy -- so the remediation is where to install it, not
                # whether to.
                print(
                    f"  profiles:    ℹ️  ~/.aws present; {render._safe_display(outside_cli)} is outside the trusted system directories, so it is not run"
                )
                render._print_wrapped(
                    "install the AWS CLI system-wide (an elevated, machine-wide install on "
                    "Windows) if you want doctor to list profiles."
                )
            else:
                print("  profiles:    ℹ️  ~/.aws present; install the AWS CLI to list profiles")
        elif has_creds:
            print("  profiles:    ✅ default (from ~/.aws/credentials)")
        else:
            print("  profiles:    ⚠️  ~/.aws present but `aws configure` lists no profile")
        # credential_process is the setup worth calling out: it vends short-lived
        # credentials on demand, so the agent's AWS calls keep working across a
        # token expiry without anyone re-running a login.
        refreshes = cli_doctor._aws_auto_refreshes()
        if refreshes:
            print("  refresh:     ✅ credential_process configured (auto-refreshing)")
        elif refreshes is None:
            # Nothing here establishes whether credentials expire. Printing the ⏹
            # line anyway told operators with a working credential_process that
            # theirs was absent; naming the wrong cause is the same defect.
            if declined_cli:
                print(
                    f"  refresh:     ℹ️  {render._safe_display(declined_cli)} is not a trusted local copy — not asked about credential_process"
                )
            elif resolved_cli:
                print(
                    f"  refresh:     ℹ️  {render._safe_display(resolved_cli)} did not answer — credential_process not established"
                )
            elif outside_cli:
                print(
                    f"  refresh:     ℹ️  {render._safe_display(outside_cli)} is outside the trusted system directories — not asked about credential_process"
                )
            else:
                print("  refresh:     ℹ️  install the AWS CLI to check for credential_process")
        else:
            print("  refresh:     ⏹ no credential_process — credentials may expire mid-task")
    vendor = cli_doctor._credential_vendor_line()
    if vendor:
        print("  vending MCP: ✅ available")
        render._print_wrapped(vendor)
    print("  note:        ℹ️  agents cannot READ credential files; AWS CLI calls are allowed")
    if has_config or has_creds:
        # Only true once something IS configured. On a host with nothing set up the
        # missing setup is the real answer, and steering the operator away from it
        # would contradict the "no ~/.aws config" line printed above.
        render._print_wrapped(
            "So if an agent reports that AWS is unavailable, it most likely hit the "
            "credential-file block rather than a missing setup — see:"
        )
        # Printed OUTSIDE the wrapper on purpose. `_print_wrapped` breaks on width
        # and split this URL across two lines at its hyphen, which an operator
        # cannot copy back out intact — a broken link is barely better than the
        # dead pointer this replaced.
        print(f"    {_BLOCKED_COMMANDS_DOC_URL}")
    else:
        render._print_wrapped(
            "With nothing configured, an agent reporting no AWS access is reporting "
            "the truth — configure a profile first, then re-run this check."
        )
