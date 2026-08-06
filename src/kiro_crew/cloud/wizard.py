"""The interactive ``kirocrew cloud launch`` wizard.

One command → answer at most a question or two → KiroCrew running on a
correctly-sized, correctly-configured EC2 box in the user's own AWS account,
backend signed in, browser open on the live dashboard.

The wizard is thin orchestration: it prints the UI (:mod:`cloud.ui`) and calls
into the testable engine modules (:mod:`cloud.ec2`, :mod:`cloud.iam`,
:mod:`cloud.login`, :mod:`cloud.connect`). All AWS work runs through the
``run_aws`` chokepoint.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2, iam, login, sizes, ssm, ui
from kiro_crew.cloud.aws import AWSError
from kiro_crew.cloud.config import DEFAULT_REGION, CloudConfig

_TOTAL_STEPS = 6

# How often to poll CloudFormation events / the on-box log while launching.
_PROGRESS_POLL_SECS = 8
# Indirection so tests can stub the poll sleep.
_sleep = time.sleep


def _deploy_with_progress(
    *,
    tag: str,
    tier: sizes.SizeTier,
    profile: str,
    region: str,
    disable_rollback: bool = False,
) -> ec2.DeployResult:
    """Run ``ec2.deploy`` while streaming live CloudFormation + bootstrap logs.

    ``ec2.deploy`` blocks (the ``aws cloudformation deploy`` call waits on the
    WaitCondition), so we run it in a background thread and, on this thread,
    poll + print: (1) each new stack resource event, and (2) once the instance
    exists, the tail of ``/var/log/kirocrew-setup.log`` over SSM. This replaces
    the opaque spinner with real progress and makes failures diagnosable.
    """
    box: dict[str, object] = {}
    # Capture the detached `aws cloudformation deploy` child so a Ctrl+C on THIS
    # (main) thread can terminate it — otherwise the deploy runs on a daemon
    # thread whose run_aws KeyboardInterrupt handler never fires (the signal hits
    # the main thread), leaving the child (up to 1800s) orphaned.
    deploy_proc: dict[str, object] = {}

    def _run() -> None:
        try:
            box["result"] = ec2.deploy(
                tag=tag,
                tier=tier,
                profile=profile,
                region=region,
                disable_rollback=disable_rollback,
                proc_sink=lambda p: deploy_proc.__setitem__("proc", p),
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced on join
            box["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    seen_events: set[str] = set()
    seen_log_lines: set[str] = set()
    instance_id = ""
    ui.detail("Streaming CloudFormation + bootstrap progress:")
    try:
        _stream_progress(worker, tag, profile, region, seen_events, seen_log_lines, instance_id)
    except BaseException:
        # A Ctrl+C (KeyboardInterrupt) OR any other exception out of the progress
        # poll loop (e.g. a transient AWS error the loop doesn't swallow) would
        # otherwise skip worker.join() and orphan the detached `aws cloudformation
        # deploy` child (~1800s). Terminate that child before propagating, so no
        # failure path leaves it running. Re-raise so handle_cloud renders the
        # abort/error message.
        proc = deploy_proc.get("proc")
        if proc is not None:
            _terminate_deploy(proc)
        raise

    worker.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]  # type: ignore[return-value]


def _terminate_deploy(proc: object) -> None:
    """Best-effort SIGTERM→SIGKILL of an interrupted deploy child."""
    try:
        if proc.poll() is None:  # type: ignore[attr-defined]
            proc.terminate()  # type: ignore[attr-defined]
            proc.wait(timeout=5)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - best effort on interrupt
        try:
            proc.kill()  # type: ignore[attr-defined]
        except Exception:
            pass


def _stream_progress(
    worker: "threading.Thread",
    tag: str,
    profile: str,
    region: str,
    seen_events: set,
    seen_log_lines: set,
    instance_id: str,
) -> None:
    """Poll + print stack events and on-box bootstrap log until the worker ends."""
    while worker.is_alive():
        # 1) New stack resource events.
        try:
            for ev in ec2.list_stack_events(tag, profile, region):
                if ev["id"] in seen_events:
                    continue
                seen_events.add(ev["id"])
                if ev["resource"] in ("", tag) and "IN_PROGRESS" in ev["status"]:
                    continue
                marker = "FAILED" in ev["status"]
                line = f"  {ev['status']:<22} {ev['resource']}"
                (ui.fail if marker else ui.detail)(line.rstrip())
                if ev["reason"] and (marker or "COMPLETE" not in ev["status"]):
                    ui.detail(f"      {ev['reason'][:200]}")
                if ev["resource"] == "Instance" and not instance_id:
                    st = ec2.describe(tag, profile, region)
                    instance_id = str(st.get("instance_id", "") or "")
        except AWSError:
            pass
        # 2) On-box bootstrap log tail (once the instance is up + SSM-managed).
        if instance_id:
            for logln in _fetch_bootstrap_log(instance_id, profile, region):
                if logln in seen_log_lines:
                    continue
                seen_log_lines.add(logln)
                ui.detail(f"      [box] {logln[:180]}")
        _sleep(_PROGRESS_POLL_SECS)


# How long to wait for a resumed instance's SSM agent to come back online.
_SSM_READY_TIMEOUT_SECS = 180
_SSM_READY_POLL_SECS = 6


def _ensure_running_and_ssm_ready(instance_id: str, profile: str, region: str) -> bool:
    """Start a stopped resumed instance and wait for SSM to report Online.

    A resumed stack (`launch` after `stop`) may be stopped; every downstream step
    is over SSM, which fails until the box is running AND its SSM agent has
    re-registered. Returns True when SSM is Online (or was already), False on a
    timeout / start failure (caller aborts with a clear message).
    """
    state = ec2._instance_state(instance_id, profile, region)
    if state in ("stopped", "stopping"):
        ui.info(f"Instance is {state}; starting it…")
        try:
            aws_start_instance(instance_id, profile, region)
        except AWSError as exc:
            ui.fail(f"could not start the instance: {exc}")
            return False
    elif state and state != "running":
        # pending / shutting-down / terminated — can't proceed reliably.
        if state == "terminated":
            ui.fail("the saved instance is terminated — run `kirocrew cloud launch --new`.")
            return False
        ui.info(f"Instance is {state}; waiting for it to run…")

    # Wait for SSM to report the node Online (agent up), so login/tunnel work.
    waited = 0
    with ui.Spinner("Waiting for the instance's SSM agent to come online…"):
        while waited < _SSM_READY_TIMEOUT_SECS:
            if ssm.instance_is_managed(instance_id, profile, region):
                ui.ok("Instance is running and SSM is online.")
                return True
            _sleep(_SSM_READY_POLL_SECS)
            waited += _SSM_READY_POLL_SECS
    ui.fail(
        "the instance did not report SSM Online in time. Check "
        "`kirocrew cloud status`; the agent may still be starting — retry shortly."
    )
    return False


def aws_start_instance(instance_id: str, profile: str, region: str) -> None:
    """Start a single instance by id (thin wrapper over the aws chokepoint)."""
    from kiro_crew.cloud import aws as _aws

    _aws.checked(
        ["ec2", "start-instances", "--instance-ids", instance_id],
        profile,
        region,
        action="ec2:StartInstances",
    )


def _verify_operational(
    instance_id: str, profile: str, region: str, *, assume_yes: bool = False
) -> bool:
    """Confirm the box is FULLY operational: kiro-cli signed in so chats work.

    A gateway that serves HTTP still errors on every new chat if the kiro-cli
    backend is logged out (the ACP session exits with 'not logged in'). This
    checks that state and, when logged out, drives one interactive re-login so
    the user finishes with a box where chat actually works — not just a page
    that loads.
    """
    ui.info("Verifying the Kiro backend is signed in (so new chats work)…")
    try:
        if login.is_logged_in(instance_id, profile, region):
            ui.ok("Kiro backend signed in — new chats will work.")
            return True
    except AWSError as exc:
        # Transient SSM/API failure — we could not *check*, which is not the
        # same as "not signed in". Say so instead of a misleading warning.
        ui.warn("Could not verify sign-in state (transient AWS/SSM error).")
        ui.detail(f"{exc} — check later with: kirocrew cloud login")
        return False

    ui.warn("Kiro backend is NOT signed in — a new chat would error.")
    if not assume_yes and not ui.confirm("Sign in to Kiro now?", default=True):
        return False

    try:
        prompt = login.start_device_login(instance_id, profile, region, open_browser=True)
    except AWSError as exc:
        ui.fail(str(exc))
        return False
    if prompt.already_logged_in:
        ui.ok("Signed in.")
        return True
    if not prompt.url:
        # A social-login prompt can come back with a LIVE port-forward tunnel
        # (prompt.port_forward) but no URL to show. Returning here without
        # prompt.close() orphans that SSM child + its loopback callback port (the
        # launch() no-url branch already closes it — mirror that). close() is a
        # no-op when there's no tunnel, so it's safe on the device-code path too.
        prompt.close()
        ui.detail(login.social_login_hint(prompt))
        return False
    if prompt.browser_opened:
        ui.note(f"Opened {ui.CYAN}{prompt.url}{ui.RESET} — approve the code to finish.")
    else:
        ui.note(f"Open {ui.CYAN}{prompt.url}{ui.RESET} and approve the code.")
    if prompt.code:
        ui.detail(f"Verification code: {prompt.code}")
    login.resume_login_daemon(instance_id, profile, region)
    try:
        with ui.Spinner("Waiting for sign-in approval…"):
            signed = login.wait_until_logged_in(instance_id, profile, region)
    finally:
        prompt.close()
    if signed:
        ui.ok("Signed in — new chats will work now.")
    return signed


def _fetch_bootstrap_log(
    instance_id: str, profile: str, region: str, *, lines: int = 6
) -> list[str]:
    """Best-effort tail of the on-box setup log over SSM (empty on any error)."""
    try:
        res = ssm.run_command(
            instance_id,
            "tail -n %d /var/log/kirocrew-setup.log 2>/dev/null "
            "| tr -d '\\r' "
            "| grep -avE 'Installing (npm|kirocrew and) depend|building React app' || true" % lines,
            profile,
            region,
            run_as="root",
            total_wait=30,
        )
    except AWSError:
        return []
    if not res.ok:
        return []
    return [ln for ln in (res.stdout or "").splitlines() if ln.strip()]


@dataclass(frozen=True)
class _ExistingLaunch:
    """A CloudFormation stack that can be resumed by ``cloud launch``."""

    tag: str
    stack_name: str
    stack_status: str = ""
    saved: bool = False


def _new_tag() -> str:
    """A short, unique discovery tag: ``kc-<6 hex>``."""
    return f"kc-{secrets.token_hex(3)}"


def launch(
    profile: str = "",
    region: str = "",
    *,
    size_key: str = "",
    assume_yes: bool = False,
    force_new: bool = False,
    keep_on_failure: bool = False,
    hold_tunnel: bool = True,
) -> int:
    """Run the full interactive launch flow. Returns a process exit code.

    ``hold_tunnel=False`` closes the SSM tunnel and returns instead of blocking
    on it — used when the wizard is embedded in a larger flow (``kirocrew
    setup``) that still has steps to print after this one.
    """
    cfg = CloudConfig.load()
    profile = profile or cfg.profile
    region = region or cfg.region or DEFAULT_REGION

    print(ui.BANNER)
    steps = ui.Steps(_TOTAL_STEPS)

    # ── 1. AWS account ────────────────────────────────────────────────────
    steps.step("AWS account")
    if not profile:
        ui.info("Using the AWS CLI's default credentials (no --profile).")
        ui.detail("KiroCrew never stores AWS credentials — the aws CLI resolves them.")
    with ui.Spinner("Checking AWS access…"):
        reach = iam.reachability_check(profile, region)
    if not reach["reachable"]:
        ui.fail("Could not resolve AWS credentials.")
        ui.detail(reach.get("detail", ""))
        ui.note(reach.get("note", ""))
        return 1
    who = reach["account"]
    ui.ok(f"account {who} · {region} · reachable")
    if not (
        reach["ec2_reachable"] and reach["cloudformation_reachable"] and reach["ssm_reachable"]
    ):
        ui.warn("Some services weren't reachable (EC2/CloudFormation/SSM).")
        ui.detail(
            "The launch will still try; on AccessDenied the exact missing action is reported."
        )
    if not _ensure_session_manager_plugin(assume_yes=assume_yes):
        return 1

    # ── 2. Permissions ───────────────────────────────────────────────────
    steps.step("Permissions")
    ui.info("Launch creates a CloudFormation stack (EC2 + IAM role + security group).")
    ui.detail("If a launch fails with AccessDenied, apply this policy and retry:")
    ui.detail("  kirocrew cloud iam-policy   → prints the least-privilege policy")
    ui.ok("Reachability confirmed (first launch is the true permission test).")

    # ── 3. Deployment choice + size ───────────────────────────────────────
    steps.step("Deployment choice")
    try:
        result = _select_existing_launch(
            cfg,
            profile,
            region,
            assume_yes=assume_yes,
            force_new=force_new,
        )
    except AWSError as exc:
        ui.fail(str(exc))
        if exc.missing_action:
            ui.detail(f"Grant `{exc.missing_action}` (see `kirocrew cloud iam-policy`) and retry.")
        return 1

    tier = None
    if result is None:
        if force_new:
            ui.info("Creating a new cloud stack because --new was supplied.")
        if size_key:
            try:
                tier = sizes.get_tier(size_key)
            except KeyError as exc:
                # argparse `choices` gate the CLI, but the public launch()
                # entrypoint can be called with an arbitrary size_key — surface a
                # clean error instead of an uncaught KeyError traceback.
                ui.fail(str(exc).strip("'\""))
                return 1
            ui.ok(f"Size: {tier.label} — {tier.summary()}")
        elif assume_yes:
            tier = sizes.default_tier()
            ui.ok(f"Size: {tier.label} — {tier.summary()} (default)")
        else:
            opts = [(t.label, t.summary()) for t in sizes.interactive_tiers()]
            default_idx = [t.key for t in sizes.interactive_tiers()].index(sizes.DEFAULT_TIER_KEY)
            idx = ui.choose("Instance size", opts, default_index=default_idx)
            tier = sizes.interactive_tiers()[idx]
        ui.detail(
            f"~${tier.approx_usd_per_hr:.2f}/hr · "
            f"~${sizes.monthly_estimate(tier):.0f}/mo if left running (approx)"
        )

        if not assume_yes and not size_key:
            if not ui.confirm(f"Launch a {tier.label} instance in {region}?", default=True):
                ui.info("Aborted — nothing was created.")
                return 0
    else:
        ui.detail("Keeping the existing stack; instance size is unchanged.")

    # ── 4. Launch ─────────────────────────────────────────────────────────
    steps.step("Launching")
    if result is None:
        assert tier is not None
        tag = _new_tag()
        ui.info(f"CloudFormation stack: {ec2.stack_name(tag)}")
        # NB: do NOT persist last_tag yet. Saving it BEFORE the deploy succeeds
        # would leave cloud.json pointing at a ROLLBACK_COMPLETE / no-instance
        # stack on a failed first launch, and the NEXT `launch` would then treat
        # that broken stack as the saved deployment and abort at "instance not
        # ready" instead of cleanly creating a new one. We set the in-memory
        # fields (so progress streaming + failure diagnostics have the tag) but
        # only `cfg.save()` AFTER a confirmed-healthy deploy below.
        cfg.profile, cfg.region, cfg.last_tag = profile, region, tag
        ui.info("Provisioning EC2 + installing KiroCrew (this takes a few minutes)…")
        try:
            result = _deploy_with_progress(
                tag=tag,
                tier=tier,
                profile=profile,
                region=region,
                disable_rollback=keep_on_failure,
            )
        except AWSError as exc:
            ui.fail(str(exc))
            if exc.missing_action:
                ui.detail(
                    f"Grant `{exc.missing_action}` (see `kirocrew cloud iam-policy`) and retry."
                )
            else:
                # Surface the detailed on-box failure and how to dig further.
                for f in ec2.get_stack_failures(tag, profile, region)[:4]:
                    ui.detail(f"  {f['resource']}: {f['reason'][:240]}")
                ui.detail(
                    "Full events: "
                    f"aws cloudformation describe-stack-events --stack-name "
                    f"{ec2.stack_name(tag)} --region {region}"
                )
                if not keep_on_failure:
                    ui.detail(
                        "Re-run with --keep-on-failure to keep the instance for "
                        "inspection (`kirocrew cloud launch --keep-on-failure`)."
                    )
            return 1
        # Deploy succeeded (WaitCondition confirmed the gateway healthy) — NOW it
        # is safe to persist the tag as the saved deployment.
        cfg.profile, cfg.region, cfg.last_tag = profile, region, tag
        cfg.save()
        ui.ok(f"Instance {result.instance_id} is up and KiroCrew is healthy.")
    elif not result.instance_id:
        ui.warn("Previous cloud stack exists but the instance is not ready yet.")
        ui.detail("Check progress with: kirocrew cloud status")
        return 1
    else:
        ui.ok(f"Resuming instance {result.instance_id}.")
        # A resumed stack may be STOPPED (after `kirocrew cloud stop`). All the
        # downstream steps (sign-in, tunnel) go over SSM, which fails on a
        # stopped box — so start it and wait for SSM to come back online first.
        if not _ensure_running_and_ssm_ready(result.instance_id, profile, region):
            return 1

    # ── 5. Sign in to Kiro ────────────────────────────────────────────────
    steps.step("Sign in to Kiro")
    instance_id = result.instance_id
    if login.is_logged_in(instance_id, profile, region):
        ui.ok("kiro-cli is already signed in on the instance.")
    else:
        ui.info("Starting kiro-cli sign-in on the instance…")
        prompt = login.start_device_login(instance_id, profile, region, open_browser=True)
        if prompt.already_logged_in:
            ui.ok("Signed in.")
        elif prompt.url:
            if prompt.ports:
                ports = ", ".join(str(port) for port in prompt.ports)
                ui.detail(f"Forwarded Kiro sign-in callback port over SSM: {ports}")
            if prompt.browser_opened:
                ui.note(f"Opened {ui.CYAN}{prompt.url}{ui.RESET} in your browser.")
            else:
                ui.note(f"Open {ui.CYAN}{prompt.url}{ui.RESET} in your browser.")
            if prompt.code:
                ui.detail(f"Verification code: {prompt.code}")
            ui.info("Approve in the browser to finish sign-in…")
            try:
                with ui.Spinner("Waiting for sign-in approval…"):
                    signed = login.wait_until_logged_in(instance_id, profile, region)
            finally:
                prompt.close()
            if signed:
                ui.ok("Signed in.")
            else:
                ui.warn("Sign-in not detected yet — you can finish it later.")
                ui.detail("Re-run: kirocrew cloud connect (then sign in from the dashboard/SSM).")
        else:
            prompt.close()
            ui.warn("Could not start Kiro sign-in automatically.")
            ui.detail(login.social_login_hint(prompt))

    # Verify the box is FULLY operational — not just that the gateway serves
    # HTTP, but that a new chat will actually work (kiro-cli logged in so the
    # ACP backend can start a session). If it's not, re-login before finishing,
    # so the user never lands on a dashboard where every chat errors.
    if not _verify_operational(instance_id, profile, region, assume_yes=assume_yes):
        ui.warn(
            "Kiro backend is not signed in — new chats will error until you "
            "sign in. Run: kirocrew cloud login"
        )

    # ── 6. Open the dashboard ─────────────────────────────────────────────
    steps.step("Open the dashboard")
    try:
        conn = connect_mod.connect(instance_id, profile, region, open_browser=True)
    except AWSError as exc:
        ui.fail(f"Could not open the dashboard: {exc}")
        conn = None
    if conn and conn.url:
        if conn.browser_opened:
            ui.ok("Opening the dashboard in your browser.")
        else:
            ui.ok("Dashboard tunnel open.")
        ui.note(f"{ui.CYAN}{conn.url}{ui.RESET}")
        if not conn.browser_opened:
            ui.detail("Open this URL in your browser.")
    elif conn and conn.error:
        ui.warn("Dashboard tunnel did not become ready.")
        ui.detail(conn.error)
    # Register for the managed /instances experience (best effort). Registers
    # over the native SSM transport, so the dashboard reaches the box with no
    # SSH key / inbound port / ~/.ssh/config.
    connect_mod.register_instance(
        instance_id,
        name=f"Kiro Crew Cloud ({result.tag})",
        profile=profile,
        region=region,
    )

    # ── Done ──────────────────────────────────────────────────────────────
    print()
    dashboard_ready = bool(conn and conn.ready and conn.url)
    if dashboard_ready:
        ui.note(f"{ui.GREEN}{ui.BOLD}KiroCrew is live on AWS.{ui.RESET}")
    else:
        ui.warn("KiroCrew is running on AWS, but the dashboard tunnel is not open.")
        ui.detail("Fix the local SSM tunnel issue, then run: kirocrew cloud connect")
    print()
    ui.note(f"{ui.BOLD}Manage it:{ui.RESET}")
    ui.detail("kirocrew cloud status            # state + cost estimate")
    ui.detail("kirocrew cloud connect           # reopen the dashboard")
    ui.detail("kirocrew cloud stop | start      # pause / resume (saves cost)")
    ui.detail("kirocrew cloud destroy           # remove EVERYTHING from AWS")
    print()

    # Keep the SSM tunnel alive so the dashboard URL we just opened keeps
    # working — the port-forward child dies the moment we return otherwise
    # (it was started with start_new_session=True and would be orphaned).
    # When embedded in `kirocrew setup` (hold_tunnel=False) we must return so
    # the outer flow can finish; close the tunnel and say how to reopen it.
    if conn and conn.ready and conn.process is not None and conn.process.poll() is None:
        if not hold_tunnel:
            conn.close()
            ui.detail("Dashboard tunnel closed — reopen anytime: kirocrew cloud connect")
        else:
            ui.detail(
                "Keeping the dashboard tunnel open — press Ctrl+C to exit "
                "(reopen later with `kirocrew cloud connect`)."
            )
            try:
                conn.process.wait()
            except KeyboardInterrupt:
                conn.close()
                ui.info("Tunnel closed. KiroCrew keeps running on AWS.")
    return 0 if dashboard_ready else 1


def _ensure_session_manager_plugin(*, assume_yes: bool = False) -> bool:
    """Ensure the local Session Manager plugin needed for SSM tunnels exists."""
    if ssm.session_manager_plugin_installed():
        ui.ok("session-manager-plugin found")
        return True

    ui.warn("session-manager-plugin is required for SSM dashboard tunnels.")
    ui.detail("KiroCrew can install AWS's official Session Manager plugin locally.")
    if not assume_yes and not ui.confirm("Install session-manager-plugin now?", default=True):
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


def _select_existing_launch(
    cfg: CloudConfig,
    profile: str,
    region: str,
    *,
    assume_yes: bool = False,
    force_new: bool = False,
) -> ec2.DeployResult | None:
    """Prompt for an existing launch target, if one can be resumed."""
    if force_new:
        return None

    launches = _discover_existing_launches(cfg, profile, region)
    selected = _choose_existing_launch(launches, cfg, assume_yes=assume_yes)
    if selected is None:
        return None

    result = _resume_tag(selected.tag, profile, region)
    if result is None:
        return None
    cfg.profile, cfg.region, cfg.last_tag = profile, region, selected.tag
    if not selected.saved:
        cfg.save()
    return result


def _discover_existing_launches(
    cfg: CloudConfig, profile: str, region: str
) -> list[_ExistingLaunch]:
    """Find resumable stacks from saved state or CloudFormation discovery."""
    launches: list[_ExistingLaunch] = []
    seen: set[str] = set()

    if cfg.last_tag and _saved_launch_matches(cfg, profile, region):
        result = _deploy_result_for_tag(cfg.last_tag, profile, region)
        # Defense in depth for configs written by an OLD build (or any path that
        # persisted last_tag before the deploy confirmed): only treat the saved
        # tag as a resumable launch if its stack is actually usable — has an
        # instance AND is not in a failed/rolled-back terminal state. A stale tag
        # pointing at a ROLLBACK_COMPLETE / instance-less stack is ignored (falls
        # through to fresh discovery / a new launch) instead of being resumed and
        # aborting at "instance not ready".
        if result is not None and _saved_launch_is_usable(result):
            launches.append(
                _ExistingLaunch(
                    tag=result.tag,
                    stack_name=result.stack_name,
                    stack_status=result.status,
                    saved=True,
                )
            )
            seen.add(result.tag)

    if launches:
        return launches

    for row in ec2.list_stacks(profile, region):
        tag = str(row.get("tag", ""))
        if not tag or tag in seen:
            continue
        launches.append(
            _ExistingLaunch(
                tag=tag,
                stack_name=str(row.get("stack_name", ec2.stack_name(tag))),
                stack_status=str(row.get("stack_status", "")),
            )
        )
        seen.add(tag)
    return launches


def _choose_existing_launch(
    launches: list[_ExistingLaunch], cfg: CloudConfig, *, assume_yes: bool = False
) -> _ExistingLaunch | None:
    """Return the stack the user chose to keep, or None to create a new one."""
    if not launches:
        return None

    if assume_yes:
        if len(launches) == 1 or cfg.last_tag:
            chosen = _preferred_existing_launch(launches, cfg)
            ui.info(
                f"Existing KiroCrew cloud stack found: {chosen.stack_name}. "
                "Keeping it by default."
            )
            ui.detail("Use `kirocrew cloud launch --new` to create a separate instance.")
            return chosen
        tags = ", ".join(launch.tag for launch in launches)
        raise AWSError(
            "multiple existing KiroCrew cloud stacks found; rerun interactively "
            f"to choose one ({tags}), or pass `kirocrew cloud launch --new` "
            "to create a separate instance"
        )

    if len(launches) == 1:
        launch = launches[0]
        idx = ui.choose(
            "Existing KiroCrew cloud deployment",
            [
                ("Keep and resume existing", _launch_summary(launch)),
                (
                    "Create a new installation",
                    "Leaves the existing AWS stack untouched.",
                ),
            ],
            default_index=0,
        )
        if idx == 0:
            return launch
        ui.info("Creating a new installation; the existing stack is unchanged.")
        return None

    options = [(f"Keep {launch.tag}", _launch_summary(launch)) for launch in launches]
    options.append(("Create a new installation", "Leaves existing AWS stacks untouched."))
    idx = ui.choose("Existing KiroCrew cloud deployments", options, default_index=0)
    if idx < len(launches):
        return launches[idx]
    ui.info("Creating a new installation; existing stacks are unchanged.")
    return None


def _preferred_existing_launch(
    launches: list[_ExistingLaunch], cfg: CloudConfig
) -> _ExistingLaunch:
    """Prefer the saved launch when non-interactive defaults are accepted."""
    if cfg.last_tag:
        for launch in launches:
            if launch.tag == cfg.last_tag:
                return launch
    return launches[0]


def _launch_summary(launch: _ExistingLaunch) -> str:
    status = f" · {launch.stack_status}" if launch.stack_status else ""
    saved = " · saved" if launch.saved else ""
    return f"{launch.stack_name}{status}{saved}"


def _deploy_result_for_tag(tag: str, profile: str, region: str) -> ec2.DeployResult | None:
    st = ec2.describe(tag, profile, region)
    if not st.get("exists"):
        return None
    return ec2.DeployResult(
        tag=tag,
        stack_name=st.get("stack_name", ec2.stack_name(tag)),
        region=st.get("region", region),
        instance_id=st.get("instance_id", ""),
        public_dns=st.get("public_dns", ""),
        status=st.get("stack_status", ""),
        reused=True,
    )


def _saved_launch_matches(cfg: CloudConfig, profile: str, region: str) -> bool:
    return (cfg.profile or "") == (profile or "") and (cfg.region or DEFAULT_REGION) == region


def _saved_launch_is_usable(result: ec2.DeployResult) -> bool:
    """True if a saved-tag stack is worth resuming (has an instance, not failed).

    Guards against a stale ``last_tag`` (e.g. written by an older build before the
    save-only-on-success fix, or any partial launch) that points at a
    ROLLBACK_COMPLETE / no-instance stack. Such a tag must NOT be presented as the
    resumable saved deployment — otherwise ``launch`` resumes it and aborts at
    "instance not ready" instead of cleanly starting fresh.
    """
    if result.status in ec2._FAILED_STATES:
        return False
    return bool(result.instance_id)


def _resume_tag(tag: str, profile: str, region: str) -> ec2.DeployResult | None:
    result = _deploy_result_for_tag(tag, profile, region)
    if result is None:
        return None

    ui.info(f"Resuming existing CloudFormation stack: {ec2.stack_name(tag)}")
    ui.detail("Use `kirocrew cloud launch --new` to create a separate instance.")
    return result
