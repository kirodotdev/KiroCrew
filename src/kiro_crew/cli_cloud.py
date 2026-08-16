"""CLI ``kirocrew cloud`` command group — thin dispatchers into :mod:`cloud`.

Every verb here is a small wrapper that calls into the testable ``cloud/``
engine. No AWS logic lives in this file. The verbs are **human/installer
actions, never LLM tools** — they are not registered as MCP tools, and the
destructive AWS CLI verbs (``aws ec2 terminate-instances`` / ``ec2 delete-*`` /
``cloudformation delete-stack``) are blocked for the agent by the
``deniedCommands`` regexes in ``config/defaults.json`` (kiro-cli enforces them
on ``execute_bash``/``shell``), not by ``security.py``'s underscored
``BUILTIN_DENY_PATTERNS``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2, iam
from kiro_crew.cloud import login as login_mod
from kiro_crew.cloud import sizes, ssm, ui, wizard
from kiro_crew.cloud.aws import AWSError, CloudActionDenied
from kiro_crew.cloud.config import DEFAULT_REGION, CloudConfig
from kiro_crew.validation import ValidationError


def _resolve(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve (profile, region) from args, falling back to saved config."""
    cfg = CloudConfig.load()
    profile = getattr(args, "profile", "") or cfg.profile
    region = getattr(args, "region", "") or cfg.region or DEFAULT_REGION
    return profile, region


def _resolve_tag(args: argparse.Namespace) -> str:
    """Resolve the instance tag: explicit --tag, else the last-launched tag."""
    tag = getattr(args, "tag", "") or ""
    if tag:
        return tag
    cfg = CloudConfig.load()
    if not cfg.last_tag:
        ui.fail("No instance tag given and no previous launch found.")
        ui.detail("Pass --tag <tag>, or run `kirocrew cloud list` to see instances.")
        sys.exit(1)
    return cfg.last_tag


def _cloud_launch(args: argparse.Namespace) -> int:
    profile, region = _resolve(args)
    return wizard.launch(
        profile=profile,
        region=region,
        size_key=getattr(args, "size", "") or "",
        subnet_id=getattr(args, "subnet", "") or "",
        spot=getattr(args, "spot", False),
        assume_yes=getattr(args, "yes", False),
        force_new=getattr(args, "new", False),
        keep_on_failure=getattr(args, "keep_on_failure", False),
        hold_tunnel=getattr(args, "hold_tunnel", True),
    )


def _cloud_list(args: argparse.Namespace) -> int:
    profile, region = _resolve(args)
    try:
        rows = ec2.list_instances(profile, region)
    except AWSError as exc:
        ui.fail(str(exc))
        return 1
    if not rows:
        ui.info("No KiroCrew cloud instances found.")
        ui.detail("Launch one with: kirocrew cloud launch")
        return 0
    ui.note(f"{ui.BOLD}KiroCrew cloud instances ({region}):{ui.RESET}")
    for r in rows:
        state = r.get("instance_state", "?")
        ui.note(f"  {ui.BOLD}{r['tag']}{ui.RESET}  {r['instance_id']}  {ui.DIM}{state}{ui.RESET}")
    return 0


def _cloud_status(args: argparse.Namespace) -> int:
    profile, region = _resolve(args)
    tag = _resolve_tag(args)
    st = ec2.describe(tag, profile, region)
    if not st.get("exists"):
        ui.info(f"No instance found for tag '{tag}'.")
        return 0
    ui.note(f"{ui.BOLD}{tag}{ui.RESET}")
    ui.detail(f"stack:    {st.get('stack_name', '')} ({st.get('stack_status', '')})")
    ui.detail(f"instance: {st.get('instance_id', '')} [{st.get('instance_state', '?')}]")
    ui.detail(f"region:   {st.get('region', region)}")
    return 0


def _cloud_connect(args: argparse.Namespace) -> int:
    profile, region = _resolve(args)
    if not _ensure_session_manager_plugin():
        return 1
    tag = _resolve_tag(args)
    st = ec2.describe(tag, profile, region)
    if not st.get("exists") or not st.get("instance_id"):
        ui.fail(f"No running instance for tag '{tag}'.")
        return 1
    open_browser = not getattr(args, "no_browser", False)
    local_port = getattr(args, "local_port", 0) or connect_mod.DEFAULT_LOCAL_PORT
    if not 1 <= local_port <= 65535:
        ui.fail(f"--local-port must be 1-65535 (got {local_port}).")
        return 1
    try:
        conn = connect_mod.connect(
            st["instance_id"],
            profile,
            region,
            local_port=local_port,
            open_browser=open_browser,
        )
    except AWSError as exc:
        ui.fail(str(exc))
        return 1
    if conn.ready and conn.url:
        if not conn.token:
            # Tunnel is up but the token mint failed — the URL will hit the
            # dashboard's login wall. Say so instead of implying it's ready.
            ui.warn("Tunnel open, but could not mint a dashboard token.")
            ui.detail("The page will ask for a token. Retry: kirocrew cloud connect")
        elif conn.browser_opened:
            ui.ok("Dashboard tunnel open.")
        else:
            ui.ok("Dashboard tunnel open. Open this URL in your browser:")
        ui.note(f"{ui.CYAN}{conn.url}{ui.RESET}")
        ui.detail("Leave this running to keep the tunnel open; Ctrl+C to close.")
        try:
            if conn.process:
                conn.process.wait()
        except KeyboardInterrupt:
            conn.close()
            ui.info("Tunnel closed.")
    elif conn.error:
        ui.fail("Dashboard tunnel did not become ready.")
        ui.detail(conn.error)
        return 1
    else:
        ui.warn("Connected but could not mint a dashboard token.")
        return 1
    return 0


def _cloud_login(args: argparse.Namespace) -> int:
    """Sign kiro-cli into the instance (device-code) — the backend chats need this.

    Standalone re-entry for the wizard's sign-in step: after a non-interactive
    launch (``--yes``) nobody approved the browser code, so kiro-cli is logged
    out and every new chat errors with 'You are not logged in'. This drives the
    same device-code flow against an already-running instance.
    """
    profile, region = _resolve(args)
    tag = _resolve_tag(args)
    st = ec2.describe(tag, profile, region)
    if not st.get("exists") or not st.get("instance_id"):
        ui.fail(f"No running instance for tag '{tag}'.")
        return 1
    instance_id = st["instance_id"]

    if login_mod.is_logged_in(instance_id, profile, region):
        ui.ok("kiro-cli is already signed in on the instance. Chats should work.")
        return 0

    ui.info("Starting Kiro sign-in on the instance…")
    try:
        prompt = login_mod.start_device_login(
            instance_id, profile, region, open_browser=not getattr(args, "no_browser", False)
        )
    except AWSError as exc:
        ui.fail(str(exc))
        return 1
    if prompt.already_logged_in:
        ui.ok("Signed in.")
        return 0
    if not prompt.url:
        ui.fail("Could not start device sign-in on the instance.")
        ui.detail(login_mod.social_login_hint(prompt))
        return 1

    ui.note(f"Open this URL and approve the code:\n    {ui.CYAN}{prompt.url}{ui.RESET}")
    if prompt.code:
        ui.detail(f"Verification code: {prompt.code}")
    # Keep the login daemon polling on the box so approval completes, then wait.
    login_mod.resume_login_daemon(instance_id, profile, region)
    with ui.Spinner("Waiting for sign-in approval…"):
        signed = login_mod.wait_until_logged_in(instance_id, profile, region)
    if signed:
        ui.ok(
            "Signed in. New chats will work now — restart the gateway if a chat "
            "was already open: kirocrew cloud connect"
        )
        return 0
    ui.warn(
        "Sign-in not detected yet. Approve the code in the browser, then re-run "
        "`kirocrew cloud login`."
    )
    return 1


def _cloud_stop(args: argparse.Namespace) -> int:
    profile, region = _resolve(args)
    tag = _resolve_tag(args)
    try:
        ec2.stop(tag, profile, region)
    except AWSError as exc:
        ui.fail(str(exc))
        return 1
    ui.ok(f"Stopped '{tag}'. Compute billing paused (EBS storage still bills).")
    ui.detail("Resume with: kirocrew cloud start")
    return 0


def _cloud_start(args: argparse.Namespace) -> int:
    profile, region = _resolve(args)
    tag = _resolve_tag(args)
    try:
        ec2.start(tag, profile, region)
    except AWSError as exc:
        ui.fail(str(exc))
        # On a --spot crew a failed start is almost always an EC2 interruption
        # stop, and the raw AWS text says nothing a user can act on. Without this
        # the box looks broken, and the obvious "fix" — destroy and relaunch —
        # deletes the root volume the interruption deliberately kept. The lookup
        # runs only HERE, so a successful start still makes exactly the AWS calls
        # it always did.
        for line in ec2.spot_start_failure_hint(tag, profile, region):
            ui.detail(line)
        return 1
    ui.ok(f"Starting '{tag}'. It'll be reachable again shortly.")
    ui.detail("Reopen the dashboard with: kirocrew cloud connect")
    return 0


def _report_spot_sweep(
    sweep: dict,
    tag: str,
    profile: str,
    region: str,
    *,
    stack_is_spot: Optional[bool] = None,
) -> bool:
    """Print what destroy's Spot-request sweep did. True if anything FAILED.

    Only ever says anything for a ``--spot`` stack. A failure here is not
    cosmetic: a live persistent request keeps handing you REPLACEMENT instances,
    and an orphaned instance we couldn't terminate keeps billing — so the caller
    must not print "you won't be billed" when this returns True, and must exit
    non-zero. Every failure is reported with the ids AND the exact command that
    finishes the job.

    The grading itself lives in :func:`ec2.grade_spot_sweep` so the dashboard's
    destroy route reaches the identical verdict and wording — the money leaks
    the same either way. This function is only the rendering: warn + details for
    a problem, a quiet detail for a note.

    ``stack_is_spot`` is the stack's own ``Spot`` parameter, or ``None`` on the
    orphan path where there is no stack to ask. An unanswered lookup (IAM
    denial, agent guard) is a quiet note only when the STACK proves there is no
    request to find; the destroying principal's permissions prove nothing,
    because it need not be the principal that launched.
    """
    for request_id in sweep.get("cancelled", []):
        ui.detail(f"Cancelled Spot request {request_id} (so no replacement instance is launched).")
    for instance_id in sweep.get("terminated", []):
        ui.detail(f"Terminated its Spot instance {instance_id}.")

    grade = ec2.grade_spot_sweep(sweep, tag, profile, region, stack_is_spot=stack_is_spot)
    for problem in grade["problems"]:
        ui.warn(problem["summary"])
        for line in problem["details"]:
            ui.detail(line)
    for note in grade["notes"]:
        ui.detail(note)
    return bool(grade["failed"])


def _confirm_destructive(args: argparse.Namespace, question: str) -> bool:
    """Ask before destroying something. True to go ahead; ``-y/--yes`` skips.

    One helper for every destructive branch of ``cloud destroy`` so they cannot
    drift into asking differently (or, as the orphan sweep once did, not asking
    at all). The decline line is part of it: "Aborted — nothing was deleted."
    must stay true of whichever branch printed it, so callers must not have
    mutated anything before they get here.
    """
    if getattr(args, "yes", False):
        return True
    if ui.confirm(question, default=False):
        return True
    ui.info("Aborted — nothing was deleted.")
    return False


def _cloud_destroy(args: argparse.Namespace) -> int:
    """Full uninstall / remove-from-AWS: delete the whole stack."""
    profile, region = _resolve(args)
    tag = _resolve_tag(args)

    if getattr(args, "dry_run", False):
        res = ec2.destroy(tag, profile, region, dry_run=True)
        ui.info("Dry run — would run:")
        ui.detail("aws " + " ".join(res["argv"]))
        return 0

    st = ec2.describe(tag, profile, region)
    if not st.get("exists"):
        # "No stack" is NOT "nothing to clean up". A rolled-back --spot launch
        # leaves its persistent Spot request behind with no stack at all, and
        # this is the case the docs tell people to run `cloud destroy` for — so
        # the sweep has to happen HERE. ec2.destroy() deliberately does NOT sweep
        # on its own already-absent path (it looks the stack up first so a failed
        # lookup can't abort mid-sweep), and this early return never calls it.
        #
        # LOOK before touching anything. Cancelling is destructive in a way the
        # verb hides: cancelling a `disabled` request makes EC2 terminate its
        # STOPPED instance — the box whose root volume holds ~/.kiro/crew. Doing
        # that on a bare `kirocrew cloud destroy` with no prompt was a delete
        # nobody agreed to, and the tag came from `last_tag` half the time. So
        # the read-only probe runs first and its findings go on screen; the
        # cancel happens only after the same confirmation the stack path asks
        # for. (The dashboard needs no equivalent: its two-step Delete → Confirm
        # UI is answered before the route is ever called.)
        found, sweep = ec2.probe_spot_requests(tag, profile, region)
        if not sweep["error"] and found:
            ui.warn(f"No stack for '{tag}', but it still has {len(found)} live Spot request(s):")
            for req in found:
                instance = req["instance_id"]
                ui.detail(
                    f"{req['id']}"
                    + (f" → instance {instance}" if instance else " (no instance yet)")
                )
            ui.detail(
                "Cancelling them stops EC2 launching replacement instances. A request whose "
                "instance is STOPPED is terminated by EC2 as it is cancelled — any data on it "
                "is lost."
            )
            if not _confirm_destructive(args, f"Cancel the leftover Spot request(s) for '{tag}'?"):
                return 0
            # Re-runs the lookup, deliberately: what gets cancelled is what is
            # live now the user has said yes, not what was live at the prompt.
            sweep = ec2.cancel_spot_requests(tag, profile, region)
        # On a failed sweep _report_spot_sweep already warned with the ids and the
        # command that finishes the job — don't follow it with a cheerful summary,
        # and exit non-zero: an un-cancelled persistent request or an
        # un-terminated instance is still billing, so automation must not read
        # this as a finished teardown. stack_is_spot=None (the default): there is
        # no stack whose Spot parameter could rule a leftover request in or out,
        # so a denied lookup gets the honest "can't prove it either way" note
        # rather than the on-demand stack's "there was never a request".
        if _report_spot_sweep(sweep, tag, profile, region):
            return 1
        if sweep.get("cancelled"):
            ui.ok(f"No stack for '{tag}' — cleaned up its leftover Spot request(s).")
        else:
            ui.info(f"No instance found for tag '{tag}' — nothing to remove.")
        return 0

    ui.warn(f"This will PERMANENTLY delete the '{tag}' stack and everything in it:")
    ui.detail(
        f"instance {st.get('instance_id', '')}, its IAM role, security group, and EBS volume."
    )
    ui.detail("Any data on the instance is lost. This cannot be undone.")
    if not _confirm_destructive(args, f"Remove Kiro Crew instance '{tag}' from AWS?"):
        return 0

    try:
        with ui.Spinner("Deleting the CloudFormation stack…"):
            res = ec2.destroy(tag, profile, region)
    except AWSError as exc:
        ui.fail(str(exc))
        return 1

    # Only ever says anything for a --spot stack. Worth saying out loud: cancelling
    # the persistent request is what stops EC2 from launching a REPLACEMENT
    # instance when delete-stack terminates this one. The stack's own Spot
    # parameter rides along in the result — it, not this profile's permissions,
    # decides whether a lookup we could not run is safe to shrug off (the
    # destroying principal need not be the one that launched: an admin can
    # create a --spot stack that a restricted profile later destroys).
    sweep_failed = _report_spot_sweep(
        res.get("spot_sweep", {}),
        tag,
        profile,
        region,
        stack_is_spot=bool(res.get("stack_is_spot")),
    )

    if res.get("aborted"):
        # The sweep above could not prove this --spot stack's persistent request
        # is gone, so `ec2.destroy` refused to issue the delete: terminating the
        # instance with the request still open is how EC2 hands out a replacement
        # instance outside the stack — untracked and billing forever. Nothing was
        # deleted, so nothing local may be cleaned up either (the registration,
        # the source object and last_tag all still describe a live crew), and rc
        # is 1 like every other "teardown did not finish" exit.
        # "not confirmed gone" rather than "still live": a refused CANCEL proves
        # the request is live, but a lookup that was denied/throttled proves
        # nothing — and both land here, because on a --spot stack an unanswered
        # lookup is exactly the case that can zombie.
        ui.fail(f"Did NOT delete the '{tag}' stack — its Spot request is not confirmed gone.")
        ui.detail(
            "Deleting now would terminate the instance while its persistent request is open, "
            "and EC2 would launch a REPLACEMENT instance outside the stack that nothing "
            "tracks and nothing stops billing."
        )
        ui.detail(
            "Cancel the request(s) — the command above, or the EC2 console under Spot Requests "
            "— then re-run `kirocrew cloud destroy`."
        )
        ui.detail("Your stack, instance and disk are untouched in the meantime.")
        return 1

    if not res.get("destroyed"):
        # Deletion did not confirm (still in progress or DELETE_FAILED). Do NOT
        # report success, do NOT clear last_tag or delete the source object, and
        # exit non-zero — otherwise automation would assume teardown finished
        # while AWS resources may still be billing.
        ui.warn("Delete started but did not confirm completion — resources may still exist.")
        ui.detail("Check `kirocrew cloud status` (and the AWS console); re-run destroy if needed.")
        return 1

    # Confirmed deleted — now it's safe to drop the local Instances
    # registration, the uploaded source object, and the last_tag pointer so
    # nothing is left behind after the remove.
    if st.get("instance_id"):
        connect_mod.unregister_instance(st["instance_id"])
    from kiro_crew.cloud import source as source_mod

    try:
        src = source_mod.delete_source(tag, profile, region)
    except Exception as exc:  # pragma: no cover - defensive
        src = {"removed": False, "uri": "", "error": str(exc)}
    if not src.get("removed"):
        # The stack is gone but the private source tarball may remain (and keep
        # costing storage). Surface it with the exact manual cleanup command
        # rather than swallowing the failure.
        ui.warn("Stack deleted, but the uploaded source object could not be removed.")
        if src.get("uri"):
            ui.detail(f"Remove it manually: aws s3 rm {src['uri']}")
        if src.get("error"):
            ui.detail(src["error"])
    cfg = CloudConfig.load()
    if cfg.last_tag == tag:
        cfg.last_tag = ""
        cfg.save()

    if sweep_failed:
        # The stack is gone, but the Spot sweep above left something live (or we
        # could not even find out). A persistent request that is still open will
        # hand you a REPLACEMENT instance nothing tracks, so "you won't be billed"
        # would be a lie — and rc must be non-zero for the same reason the
        # strictly milder "delete did not confirm" path above returns 1:
        # automation must not assume the teardown finished while AWS resources
        # may still be billing.
        ui.warn(f"Removed the '{tag}' stack, but its Spot cleanup did not fully succeed.")
        ui.detail(
            "Run the command above before assuming the Spot side is clean — "
            "you may still be billed for a Spot instance."
        )
        return 1

    ui.ok(f"Removed '{tag}' — all AWS resources deleted. You won't be billed for it.")
    return 0


def _cloud_iam_policy(_args: argparse.Namespace) -> int:
    print(iam.policy_json())
    return 0


def _cloud_iam_boundary(args: argparse.Namespace) -> int:
    """Pre-create the shared, immutable instance permissions boundary (admin step).

    Normally the first ``launch`` auto-creates this (the launcher policy grants
    only ``iam:CreatePolicy`` on the fixed boundary name). Operators who want to
    eliminate the first-write race entirely run this ONCE as an admin, then drop
    the ``IamInstanceBoundaryCreateOnce`` statement from the applied launcher
    policy — the launcher then only *references* the boundary ARN, never creates
    it. Idempotent: an existing boundary is left untouched (immutability).
    """
    from kiro_crew.cloud import source as source_mod

    profile, region = _resolve(args)
    try:
        arn = source_mod.ensure_instance_boundary(profile, region)
    except AWSError as exc:
        ui.fail(str(exc))
        if exc.missing_action:
            ui.detail(f"Grant `{exc.missing_action}` and retry.")
        return 1
    ui.ok(f"Instance permissions boundary ready: {arn}")
    ui.detail(
        "It is immutable and shared by every launch. To fully close the "
        "first-write race, remove the IamInstanceBoundaryCreateOnce statement "
        "from the applied launcher policy now that the boundary exists."
    )
    return 0


def _cloud_doctor(args: argparse.Namespace) -> int:
    """Read-only diagnostics for the cloud launcher prerequisites."""
    import shutil

    profile, region = _resolve(args)
    ui.note(f"{ui.BOLD}KiroCrew cloud — diagnostics{ui.RESET}")
    # Client prerequisites.
    if shutil.which("aws"):
        ui.ok("aws CLI found")
    else:
        ui.fail("aws CLI not found — install it (https://aws.amazon.com/cli/)")
    if ssm.session_manager_plugin_installed():
        ui.ok("session-manager-plugin found")
    else:
        ui.warn("session-manager-plugin not found — needed to connect over SSM")
        ui.detail(ssm.session_manager_plugin_install_hint())
    # AWS reachability.
    reach = iam.reachability_check(profile, region)
    if reach["reachable"]:
        ui.ok(f"AWS reachable — account {reach['account']} · {region}")
        for svc in ("ec2", "cloudformation", "ssm"):
            key = f"{svc}_reachable"
            (ui.ok if reach[key] else ui.warn)(
                f"{svc}: {'reachable' if reach[key] else 'not reachable'}"
            )
    else:
        ui.fail("AWS not reachable")
        ui.detail(reach.get("note", ""))
    return 0


def _ensure_session_manager_plugin() -> bool:
    """Install the local Session Manager plugin when a cloud command needs SSM tunnels."""
    if ssm.session_manager_plugin_installed():
        return True
    ui.warn("session-manager-plugin is required for SSM dashboard tunnels.")
    ui.detail("KiroCrew can install AWS's official Session Manager plugin locally.")
    if not ui.confirm("Install session-manager-plugin now?", default=True):
        ui.detail("Install it later with: kirocrew cloud doctor")
        return False
    ui.info("Installing session-manager-plugin locally. Sudo may ask for your password.")
    result = ssm.install_session_manager_plugin()
    if result.ok:
        ui.ok(result.message or "session-manager-plugin installed")
        return True
    ui.fail("Could not install session-manager-plugin.")
    ui.detail(result.message)
    return False


_DISPATCH = {
    "launch": _cloud_launch,
    "list": _cloud_list,
    "status": _cloud_status,
    "connect": _cloud_connect,
    # `tunnel` is a clear standalone alias for `connect` — open the dashboard
    # SSM tunnel any time, independent of the launch/setup flow.
    "tunnel": _cloud_connect,
    "login": _cloud_login,
    "stop": _cloud_stop,
    "start": _cloud_start,
    "destroy": _cloud_destroy,
    "iam-policy": _cloud_iam_policy,
    "iam-boundary": _cloud_iam_boundary,
    "doctor": _cloud_doctor,
}


def handle_cloud(args: argparse.Namespace) -> int:
    """Entry point for ``kirocrew cloud <action>``."""
    action = getattr(args, "cloud_action", None)
    if not action:
        ui.note(f"{ui.BOLD}kirocrew cloud{ui.RESET} — run KiroCrew on your own AWS EC2")
        print()
        ui.detail("launch      Provision + configure an instance (interactive)")
        ui.detail("list        List your KiroCrew cloud instances")
        ui.detail("status      Show one instance's state")
        ui.detail("tunnel      Open the dashboard SSM tunnel (alias: connect)")
        ui.detail("connect     Open the dashboard over an SSM tunnel")
        ui.detail("login       Sign kiro-cli in on the instance (fixes chat errors)")
        ui.detail("stop|start  Pause / resume (save cost)")
        ui.detail("destroy     Remove everything from AWS")
        ui.detail("iam-policy  Print the least-privilege IAM policy to apply")
        ui.detail("iam-boundary Pre-create the immutable instance permissions boundary (admin)")
        ui.detail("doctor      Check cloud prerequisites + AWS reachability")
        return 0
    fn = _DISPATCH.get(action)
    if not fn:
        ui.fail(f"unknown cloud action: {action}")
        return 1
    try:
        return fn(args)
    except CloudActionDenied as exc:
        # A mutating cloud verb was reached from an agent session (the in-layer
        # preflight fired). Human/installer action only.
        ui.fail(str(exc))
        return 1
    except ValidationError as exc:
        # A malformed user-typed value (e.g. --tag with bad charset) — show the
        # clean one-liner every action gets for AWSError, not a raw traceback.
        ui.fail(str(exc))
        return 1
    except AWSError as exc:
        # Safety net for AWS failures outside an action's own try/except
        # (e.g. the ec2.describe() lookups in status/connect/login) — expired
        # SSO or throttling must render the clean one-liner, not a traceback.
        ui.fail(str(exc))
        return 1
    except KeyboardInterrupt:
        print()
        ui.info("Interrupted.")
        return 130


def add_size_choices() -> list[str]:
    """Valid --size values for the argparse choices list."""
    return list(sizes.TIERS_BY_KEY)
