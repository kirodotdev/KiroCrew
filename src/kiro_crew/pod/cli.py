"""``kirocrew pod <verb>`` — kubectl-style control of worktree test pods.

Thin verb layer over :mod:`kiro_crew.pod.runtime` / :mod:`kiro_crew.pod.unit`.
Dispatched from :func:`kiro_crew.cli_commands._pod`.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, NoReturn

from kiro_crew.pod import launchd
from kiro_crew.pod import provision as prov
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# A verb handler: (config, parsed args) -> None.
PodHandler = Callable[[PodConfig, argparse.Namespace], None]


def _audit(operation: str, outcome: str, resources: str = "", error: str = "") -> None:
    """Emit a security-event-log (SEL) entry for a security-relevant pod operation
    (service start/stop, token mint, isolated-gateway boot). Best-effort — never
    let an audit failure break the verb, but LOG the failure so operators can
    detect audit gaps (a silently-dropped audit event defeats the purpose)."""
    try:
        sel().log_api_access(
            caller="cli",
            operation=operation,
            outcome=outcome,
            source="cli",
            resources=resources,
            error=error,
        )
    except Exception as exc:
        logger.warning("SEL audit failed for %s: %s", operation, exc)


def _die(msg: str) -> NoReturn:
    print(f"pod: {msg}", file=sys.stderr)
    sys.exit(1)


def _wait_healthy(cfg: PodConfig, name: str, port: int, tries: int = 45) -> int:
    """Poll until the pod serves (200/401/403), or bail fast if the unit died.

    Returns the HTTP code on success, or a negative sentinel on early failure:
      -1 = the unit's gateway crashed / is crash-looping (a broken worktree build
           — the thing under test won't boot, so there's nothing to wait for). The
           caller surfaces the gateway's own journal as the cause.
    A pod IS the worktree's gateway, so a dead gateway is a real, expected signal —
    we just want it fast and clearly attributed, not a silent 45s timeout.
    """
    for _ in range(tries):
        code = rt.health(port)
        if code in (200, 401, 403):
            return code
        state, restarts = rt.unit_state(cfg, name)
        # failed = exited non-zero and not restarting; restarts>0 = crash-looping.
        if state == "failed" or restarts > 0:
            return -1
        time.sleep(1)
    return rt.health(port)


def _resolve_or_die(cfg: PodConfig, name: str) -> Path:
    try:
        return rt.resolve_checkout(cfg, name, cwd=Path.cwd())
    except rt.PodError as exc:
        _die(str(exc))


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #
def _up(cfg: PodConfig, args: argparse.Namespace) -> None:
    name = rt.validate_name(args.name)
    checkout = _resolve_or_die(cfg, name)

    # Graduated, teaching errors + auto-provisioning. The venv is cheap and
    # idempotent so we build it on demand; the dist is the slow SPA build, so we
    # only run it under explicit --provision consent and otherwise fail loud.
    if getattr(args, "provision", False):
        if not prov.provision(checkout, build=True):
            _die(f"provisioning {name!r} failed (see output above)")
    else:
        if not prov.has_venv(checkout) and not prov.ensure_venv(checkout):
            _die(f"could not build venv for {name!r} (see output above)")
        if not prov.has_dist(checkout):
            _die(
                f"no built dist for {name!r}.\n"
                f"  Build it (slow, one-time):  cd {checkout / 'website'} && npm run build\n"
                f"  Or let pod do the full chain: kirocrew pod up {name} --provision"
            )

    port = rt.derive_port(cfg, name)
    if port == cfg.live_port:
        _audit("pod.up", "denied", f"name={name}", error="derived port is the live plane")
        _die(f"refusing: derived port is the live plane :{cfg.live_port}")

    # Pin the resolved checkout BEFORE starting the unit so the systemd-booted
    # gateway (and any Restart= re-exec) resolves it without shelling git from a
    # clean environment. SEED (if any) is merged in without clobbering the pin.
    # The whole pin -> start transaction holds the per-name mutex (no-op on
    # Linux): without it, a concurrent `down` finishing its sweep after our pin
    # would delete the pin we just wrote and this pod would crash-loop on boot.
    with rt.pod_name_mutex(cfg, name):
        rt.pin_checkout(cfg, name, checkout)
        # Boot-time settings, merged into a single env-file write. ``boot`` reads
        # them once at start, so recording one against a live pod would look
        # applied and change nothing until a restart -- hence the note below.
        # Read defensively, as ``provision`` above is: hand-built Namespaces in
        # tests (and older callers) may not carry every key.
        env_updates: dict[str, str] = {}
        boot_flags: list[str] = []
        if args.seed:
            env_updates["SEED"] = args.seed
        approval = getattr(args, "approval", None)
        if approval:
            env_updates["APPROVAL"] = approval
            boot_flags.append(f"--approval {approval}")
        crons = bool(getattr(args, "crons", False))
        if crons:
            env_updates["CRONS"] = "1"
            boot_flags.append("--crons")
        if env_updates:
            rt.write_env_file(cfg, name, env_updates)

        was_active = rt.is_active(cfg, name)
        if not was_active:
            cp = rt.start_pod(cfg, name)
            if cp.returncode != 0:
                _audit(
                    "pod.up", "failure", f"name={name} port={port}", error="backend start failed"
                )
                _die(f"starting pod {name} failed: {(cp.stderr or '').strip()}")
        elif boot_flags:
            joined = " ".join(boot_flags)
            print(
                f"pod: note: {joined} recorded for {name!r}, but that pod is already "
                f"running, so it applies on the next boot "
                f"(kirocrew pod down {name} && kirocrew pod up {name} {joined}).",
                file=sys.stderr,
            )
        # Record boot-time settings: a pod in `yolo` auto-approves every tool and
        # one with the scheduler on runs work unattended, so the audit trail must
        # say so rather than recording only that a pod came up. Mark the
        # requested-but-not-yet-effective case: `boot` reads these once at start,
        # so a setting recorded against a live pod has not applied yet.
        resources = f"name={name} port={port}"
        if approval:
            resources += f" approval={approval}"
        if crons:
            resources += " crons=on"
        if boot_flags and was_active:
            resources += " applied=next_boot"
        _audit("pod.up", "allowed", resources)

        code = _wait_healthy(cfg, name, port)
        if code not in (200, 401, 403):
            # A pod IS the worktree's own gateway. If it won't boot, that's a broken
            # worktree build (bad import / config / unbuilt dist) — NOT a pod-tooling
            # fault. Surface the gateway's own journal so the dev fixes the real cause,
            # and stop the half-started unit so we don't leak a crash-looping service.
            #
            # This failure cleanup runs INSIDE the same mutex hold as our start:
            # released between the two, a down + replacement up could interleave
            # during the health wait, and this stop_pod would then unload and
            # erase the REPLACEMENT pod (verifier-found High). Holding the lock
            # across the whole boot transaction means the pod we stop here can
            # only be the one we started.
            tail = rt.recent_journal(cfg, name, 30)
            print(tail, file=sys.stderr)
            rt.stop_pod(cfg, name)
            if code == -1:
                _die(
                    f"{name}: the worktree's gateway failed to start (see journal above). "
                    f"This is the worktree build, not pod — fix it, then `kirocrew pod up {name}` again."
                )
            _die(
                f"{name}: gateway never became healthy on :{port} within timeout "
                f"(see journal above; check the worktree's gateway start path)."
            )

    try:
        token = rt.mint_token(cfg, name, args.ttl)
    except rt.PodError as exc:
        _audit("pod.token", "failure", f"name={name} port={port}", error="mint failed")
        _die(str(exc))
    _audit("pod.token", "allowed", f"name={name} port={port} ttl={args.ttl}")
    base = f"http://127.0.0.1:{port}"
    if args.json:
        print(
            json.dumps(
                {
                    "name": name,
                    "status": "up",
                    "port": port,
                    "base_url": base,
                    "token": token,
                    "ttl": args.ttl,
                }
            )
        )
    else:
        print(f"pod '{name}' is up (full stack: API + frontend on one port)")
        print(f"  base_url : {base}")
        print(f"  token    : {token}")
        print(f"  open     : {base}/?token={token}")
        print(f"  stop     : kirocrew pod down {name}")


def _down(cfg: PodConfig, args: argparse.Namespace) -> None:
    name = rt.validate_name(args.name)
    was_up = rt.is_active(cfg, name)
    # The stop and the env-file removal move together under the per-name mutex
    # (no-op on Linux): unlinking the env file after releasing the lock let a
    # concurrent `up` — which pins its checkout under the same mutex — have its
    # fresh pin deleted by this stale teardown.
    with rt.pod_name_mutex(cfg, name):
        cp = rt.stop_pod(cfg, name)
        # A nonzero stop means the pod may still be live — don't claim success or
        # delete the env file. On macOS this must hold even when was_up is False:
        # a loaded-but-dead agent has no pid (is_active False) yet still needs its
        # unload CONFIRMED; swallowing the failure deleted the metadata while
        # leaving the service, plist and HOME behind. An absent service is rc 0 on
        # both backends, so this cannot fire for plain "was not running".
        if cp.returncode != 0 and (was_up or rt.IS_MACOS):
            _audit("pod.down", "failure", f"name={name}", error=f"stop rc={cp.returncode}")
            _die(f"stopping pod {name} failed: {(cp.stderr or '').strip()}")
        if rt.RECLAIMED_MARKER in (cp.stdout or ""):
            # Defense-in-depth for writers that bypass the mutex: a new pod
            # claimed this name mid-teardown. The old pod is gone, but the env
            # file now pins the NEW pod's checkout — leave it alone.
            _audit("pod.down", "allowed", f"name={name} was_up={was_up} reclaimed=1")
            print(
                f"pod '{name}' stopped — the name was immediately reclaimed by a new "
                "pod, whose state was left untouched"
            )
            return
        # Clear the pinned CHECKOUT= / SEED= so the next `up` re-resolves cleanly.
        env_file = cfg.env_file(name)
        if env_file.exists():
            env_file.unlink()
    _audit("pod.down", "allowed", f"name={name} was_up={was_up}")
    if was_up:
        print(f"pod '{name}' stopped — isolated HOME nuked (zero residue), live plane untouched")
    else:
        print(f"pod '{name}' was not running (nothing to stop)")


def _ls(cfg: PodConfig, args: argparse.Namespace) -> None:
    names = sorted(rt.active_names(cfg))
    # macOS only: launchd has no ExecStopPost, so a pod killed without an
    # explicit `down` leaves its isolated HOME behind. Surface those here —
    # the docs promise `ls`/`down` make them visible — but keep the JSON array
    # shape unchanged (three callers parse it); orphans are human-output only.
    if args.json:
        rows = [
            {"name": n, "port": rt.derive_port(cfg, n), "health": rt.health(rt.derive_port(cfg, n))}
            for n in names
        ]
        print(json.dumps(rows))
        return
    orphans: list[str] = []
    if rt.IS_MACOS:
        try:
            orphans = launchd.orphan_homes(cfg)
        except launchd.LaunchdError as exc:
            # Same translation the runtime seam does: the dispatch layer's
            # documented contract is PodError -> one-line `pod: <msg>` + exit 1,
            # not a traceback from the fail-closed probe underneath.
            raise rt.PodError(str(exc)) from exc
    if not names:
        print("no pods running")
        _print_orphans(cfg, orphans)
        return
    print(f"{'POD':<28} {'PORT':<7} HEALTH")
    for n in names:
        p = rt.derive_port(cfg, n)
        print(f"{n:<28} {p:<7} {rt.health(p)}")
    _print_orphans(cfg, orphans)


def _print_orphans(cfg: PodConfig, orphans: list[str]) -> None:
    """Human-readable orphan-HOME report (macOS only; empty elsewhere)."""
    if not orphans:
        return
    print(
        f"\n{len(orphans)} orphaned pod HOME(s) — left by a pod that died without "
        "an explicit `down` (launchd has no post-stop hook):"
    )
    for n in orphans:
        print(f"  {n:<26} reclaim: kirocrew pod down {n}")


def _status(cfg: PodConfig, args: argparse.Namespace) -> None:
    name = rt.validate_name(args.name)
    port = rt.derive_port(cfg, name)
    up = rt.is_active(cfg, name)
    code = rt.health(port) if up else 0
    if args.json:
        print(
            json.dumps(
                {"name": name, "status": "up" if up else "down", "port": port, "health": code}
            )
        )
    else:
        print(f"{name}: {'up' if up else 'down'}  port={port}  health={code}")


def _token(cfg: PodConfig, args: argparse.Namespace) -> None:
    name = rt.validate_name(args.name)
    try:
        tok = rt.mint_token(cfg, name, args.ttl)
    except rt.PodError as exc:
        _audit("pod.token", "failure", f"name={name}", error="mint failed")
        _die(str(exc))
    _audit("pod.token", "allowed", f"name={name} ttl={args.ttl}")
    print(tok)


def _url(cfg: PodConfig, args: argparse.Namespace) -> None:
    name = rt.validate_name(args.name)
    print(f"http://127.0.0.1:{rt.derive_port(cfg, name)}")


def _exec(cfg: PodConfig, args: argparse.Namespace) -> None:
    name = rt.validate_name(args.name)
    argv = list(args.argv or [])
    if not argv:
        _die("nothing to run — usage: kirocrew pod exec <name> -- <args…>")
    # Validate BEFORE auditing: emitting "allowed" and then having the runtime
    # refuse the verb would record the opposite of the decision actually taken,
    # which is worse than no audit trail at all — SEL would attest that a denied
    # `service uninstall` was permitted.
    try:
        rt.require_pod_safe_verb(argv, name)
    except rt.PodError as exc:
        _audit("pod.exec", "denied", f"name={name} argv={argv[0]}", error=str(exc))
        _die(str(exc))
    _audit("pod.exec", "allowed", f"name={name} argv={argv[0]}")
    # execve replaces this process; on success nothing below runs.
    sys.exit(rt.exec_in_pod(cfg, name, argv))


def _logs(cfg: PodConfig, args: argparse.Namespace) -> None:
    name = rt.validate_name(args.name)
    # Gate before exec'ing the log mechanism — on an unsupported host this would
    # otherwise raise a bare FileNotFoundError instead of the documented refusal.
    rt.require_backend()
    if rt.IS_MACOS:
        # launchd has no journal; the plist routes stdout/stderr to files and
        # recent_journal tails them.
        print(rt.recent_journal(cfg, name, args.lines))
        return
    subprocess.run(
        ["journalctl", "--user", "-u", rt.pod_unit(cfg, name), "-n", str(args.lines), "--no-pager"],
        env=rt._systemctl_env(),
    )


def _install(cfg: PodConfig, args: argparse.Namespace) -> None:
    # Writing the service definition (which defines how pods boot + what they
    # exec) is a security-relevant system modification → audit it. The gate is
    # inside install_backend, before anything is written.
    try:
        msg, reload_cp = rt.install_backend(cfg)
    except rt.PodError as exc:
        # Re-raise: the CLI dispatch layer turns PodError into the documented
        # one-line refusal, and swallowing it would hand an unsupported host a
        # SystemExit instead. The gate runs before anything is written.
        _audit("pod.install", "failure", "", error=str(exc)[:120])
        raise
    if reload_cp is not None and reload_cp.returncode != 0:
        # The unit isn't loadable without a successful reload — fail fast rather
        # than telling the user it's "ready" (consistent with _up / _down).
        _audit("pod.install", "failure", msg.splitlines()[0][:120], error="daemon-reload failed")
        _die(f"systemctl --user daemon-reload failed: {(reload_cp.stderr or '').strip()}")
    _audit("pod.install", "allowed", msg.splitlines()[0][:120])
    print(msg)
    print("ready. Next: kirocrew pod up <worktree>")


def _provision(cfg: PodConfig, args: argparse.Namespace) -> None:
    """Build a worktree's venv + dist so it can be podded (the full on-ramp)."""
    name = rt.validate_name(args.name)
    checkout = _resolve_or_die(cfg, name)
    build = not getattr(args, "venv_only", False)
    if not prov.provision(checkout, build=build):
        _die(f"provisioning {name!r} failed (see output above)")
    # Pin so a subsequent `up` (and the systemd boot) resolves the same checkout.
    # Under the per-name mutex: unlocked, a concurrent `down` finishing its
    # teardown could unlink this fresh pin (verifier finding).
    with rt.pod_name_mutex(cfg, name):
        rt.pin_checkout(cfg, name, checkout)


def _run_internal(cfg: PodConfig, args: argparse.Namespace) -> None:
    """Hidden: ExecStart body. Boots the pod's gateway (does not return on success)."""
    # Audit BEFORE boot — boot() exec()s the gateway and never returns on success.
    _audit("pod.boot", "allowed", f"name={args.name}")
    rc = rt.boot(cfg, args.name)
    _audit("pod.boot", "failure", f"name={args.name}", error=f"exit={rc}")
    sys.exit(rc)


def _cleanup_internal(cfg: PodConfig, args: argparse.Namespace) -> None:
    """Hidden: ExecStopPost body. Safe-deletes the pod's isolated HOME.

    Re-validates the systemd ``%i`` instance name (which is NOT gated by the CLI's
    validate_name) and refuses ``..``/absolute/empty before deleting, so a unit
    started directly as ``kirocrew-pod@..`` can't ``rm`` outside the pod root.
    """
    rc = rt.cleanup_home(cfg, args.name)
    outcome = "allowed" if rc == 0 else "failure"
    _audit("pod.cleanup", outcome, f"name={args.name}", error="" if rc == 0 else f"rc={rc}")
    sys.exit(rc)


_VERBS: dict[str, PodHandler] = {
    "up": _up,
    "down": _down,
    "ls": _ls,
    "status": _status,
    "token": _token,
    "url": _url,
    "logs": _logs,
    "install": _install,
    "provision": _provision,
    "_run": _run_internal,
    "_cleanup": _cleanup_internal,
    "exec": _exec,
}


def dispatch(args: argparse.Namespace) -> None:
    action = getattr(args, "pod_action", None)
    if not action:
        print(
            "Usage: kirocrew pod "
            "{up|down|ls|status|token|url|logs|exec|install|provision} …"
        )
        sys.exit(2)
    cfg = PodConfig.load()
    handler = _VERBS.get(action)
    if handler is None:
        _die(f"unknown pod verb {action!r}")
    try:
        handler(cfg, args)
    except rt.PodError as exc:
        _die(str(exc))
