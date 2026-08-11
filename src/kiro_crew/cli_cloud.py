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


def _cloud_logout(args: argparse.Namespace) -> int:
    """Sign kiro-cli out on the instance — the way to switch Kiro accounts.

    ``cloud login`` short-circuits when a session already exists, so switching
    accounts otherwise means an SSM console round-trip to run ``kiro-cli
    logout`` by hand. This is that round-trip, as one verb.
    """
    profile, region = _resolve(args)
    tag = _resolve_tag(args)
    st = ec2.describe(tag, profile, region)
    if not st.get("exists") or not st.get("instance_id"):
        ui.fail(f"No running instance for tag '{tag}'.")
        return 1

    with ui.Spinner("Signing kiro-cli out on the instance…"):
        signed_out = login_mod.logout(st["instance_id"], profile, region)
    if not signed_out:
        ui.fail("Could not confirm the instance is signed out.")
        ui.detail("The session may still be active — retry, or check with: kirocrew cloud connect")
        return 1
    ui.ok("Signed out on the instance.")
    ui.detail("Any in-flight chats/cron sessions were stopped (their kiro-cli runtimes were killed).")
    ui.detail("Sign in with another account: kirocrew cloud login")
    return 0


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
        return 1
    ui.ok(f"Starting '{tag}'. It'll be reachable again shortly.")
    ui.detail("Reopen the dashboard with: kirocrew cloud connect")
    return 0


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
        ui.info(f"No instance found for tag '{tag}' — nothing to remove.")
        return 0

    ui.warn(f"This will PERMANENTLY delete the '{tag}' stack and everything in it:")
    ui.detail(
        f"instance {st.get('instance_id', '')}, its IAM role, security group, and EBS volume."
    )
    ui.detail("Any data on the instance is lost. This cannot be undone.")
    if not getattr(args, "yes", False):
        if not ui.confirm(f"Remove KiroCrew instance '{tag}' from AWS?", default=False):
            ui.info("Aborted — nothing was deleted.")
            return 0

    try:
        with ui.Spinner("Deleting the CloudFormation stack…"):
            res = ec2.destroy(tag, profile, region)
    except AWSError as exc:
        ui.fail(str(exc))
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
    "logout": _cloud_logout,
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
        ui.detail("logout      Sign kiro-cli out on the instance (switch Kiro account)")
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
