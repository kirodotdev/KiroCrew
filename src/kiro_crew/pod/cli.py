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

from kiro_crew.pod import provision as prov
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import unit as unit_mod
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
    rt.pin_checkout(cfg, name, checkout)
    if args.seed:
        rt.write_env_file(cfg, name, {"SEED": args.seed})

    if not rt.is_active(cfg, name):
        # Self-heal a dangling ExecStart binary: the unit bakes an absolute
        # kirocrew path at install time; if the worktree it resolved into was
        # pruned since, every start fails EXEC (203). Re-render with a
        # currently-valid binary and reload before starting.
        if not unit_mod.unit_exec_ok(cfg):
            unit_mod.install_unit(cfg)
            rel = rt.systemctl("daemon-reload")
            if rel.returncode != 0:
                _die(f"unit self-heal daemon-reload failed: {(rel.stderr or '').strip()}")
            _audit("pod.up", "allowed", f"name={name}", error="unit ExecStart healed")
        cp = rt.systemctl("start", rt.pod_unit(cfg, name))
        if cp.returncode != 0:
            _audit("pod.up", "failure", f"name={name} port={port}", error="systemctl start failed")
            _die(f"systemctl start failed for {name}: {cp.stderr.strip()}")
    _audit("pod.up", "allowed", f"name={name} port={port}")

    code = _wait_healthy(cfg, name, port)
    if code not in (200, 401, 403):
        # A pod IS the worktree's own gateway. If it won't boot, that's a broken
        # worktree build (bad import / config / unbuilt dist) — NOT a pod-tooling
        # fault. Surface the gateway's own journal so the dev fixes the real cause,
        # and stop the half-started unit so we don't leak a crash-looping service.
        tail = rt.recent_journal(cfg, name, 30)
        print(tail, file=sys.stderr)
        rt.systemctl("stop", rt.pod_unit(cfg, name))
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
    cp = rt.systemctl("stop", rt.pod_unit(cfg, name))
    # If it was running but stop failed, the pod may still be live — don't claim
    # success or delete the env file (mirrors the rc checks in _up / _install).
    if was_up and cp.returncode != 0:
        _audit("pod.down", "failure", f"name={name}", error=f"stop rc={cp.returncode}")
        _die(f"systemctl stop failed for {name}: {(cp.stderr or '').strip()}")
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
    if args.json:
        rows = [
            {"name": n, "port": rt.derive_port(cfg, n), "health": rt.health(rt.derive_port(cfg, n))}
            for n in names
        ]
        print(json.dumps(rows))
        return
    if not names:
        print("no pods running")
        return
    print(f"{'POD':<28} {'PORT':<7} HEALTH")
    for n in names:
        p = rt.derive_port(cfg, n)
        print(f"{n:<28} {p:<7} {rt.health(p)}")


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
    subprocess.run(
        ["journalctl", "--user", "-u", rt.pod_unit(cfg, name), "-n", str(args.lines), "--no-pager"],
        env=rt._systemctl_env(),
    )


def _install(cfg: PodConfig, args: argparse.Namespace) -> None:
    # Writing the systemd unit (which defines how pods boot + what they exec) and
    # reloading the daemon is a security-relevant system modification → audit it.
    dst = unit_mod.install_unit(cfg)
    print(f"installed pod template unit → {dst}")
    cp = rt.systemctl("daemon-reload")
    if cp.returncode != 0:
        # The unit isn't loadable without a successful reload — fail fast rather
        # than telling the user it's "ready" (consistent with _up / _down).
        _audit("pod.install", "failure", f"dst={dst}", error="daemon-reload failed")
        _die(f"systemctl --user daemon-reload failed: {(cp.stderr or '').strip()}")
    _audit("pod.install", "allowed", f"dst={dst}")
    print("systemctl --user daemon-reload OK")
    print("ready. Next: kirocrew pod up <worktree>")


def _provision(cfg: PodConfig, args: argparse.Namespace) -> None:
    """Build a worktree's venv + dist so it can be podded (the full on-ramp)."""
    name = rt.validate_name(args.name)
    checkout = _resolve_or_die(cfg, name)
    build = not getattr(args, "venv_only", False)
    if not prov.provision(checkout, build=build):
        _die(f"provisioning {name!r} failed (see output above)")
    # Pin so a subsequent `up` (and the systemd boot) resolves the same checkout.
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
