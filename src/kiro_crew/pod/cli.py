"""``kirocrew pod <verb>`` — kubectl-style control of worktree test pods.

Thin verb layer over :mod:`kiro_crew.pod.runtime` / :mod:`kiro_crew.pod.unit`.
Dispatched from :func:`kiro_crew.cli_commands._pod`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, NoReturn

from kiro_crew.pod import provision as prov
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig
from kiro_crew.sel import sel
from kiro_crew.tips_text import truncate_summary

logger = logging.getLogger(__name__)
_SCENARIOS_TABLE_WIDTH = 100

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
    """Poll until the pod itself serves (200/401/403), or bail fast on failure.

    Returns the HTTP code on success, or a negative sentinel on early failure:
      -1 = the unit's gateway crashed / is crash-looping (a broken worktree build
           — the thing under test won't boot, so there's nothing to wait for). The
           caller surfaces the gateway's own journal as the cause.
      ``rt.HEALTH_FOREIGN`` = the port answers, but another process owns it, so
           this pod's gateway cannot have bound it.
    A pod IS the worktree's gateway, so a dead gateway is a real, expected signal —
    we just want it fast and clearly attributed, not a silent 45s timeout.

    A foreign responder does NOT end the wait on sight. The pod may still be
    starting, and its own gateway may be moments from winning the port back after
    a predecessor releases it, so the loop keeps its existing exit conditions.
    What the flag changes is the ATTRIBUTION: a port already held by somebody else
    is the reason this pod's gateway is crash-looping, so it is reported ahead of
    the generic crash verdict, which would otherwise send the operator to read a
    journal that only says "address already in use".
    """
    saw_foreign = False
    for _ in range(tries):
        code = rt.health(cfg, name, port)
        if code in (200, 401, 403):
            return code
        if code == rt.HEALTH_FOREIGN:
            saw_foreign = True
        state, restarts = rt.unit_state(cfg, name)
        # failed = exited non-zero and not restarting; restarts>0 = crash-looping.
        if state == "failed" or restarts > 0:
            return rt.HEALTH_FOREIGN if saw_foreign else -1
        time.sleep(1)
    final = rt.health(cfg, name, port)
    if final in (200, 401, 403):
        return final
    return rt.HEALTH_FOREIGN if (saw_foreign or final == rt.HEALTH_FOREIGN) else final


def _resolve_or_die(cfg: PodConfig, name: str) -> Path:
    try:
        return rt.resolve_checkout(cfg, name, cwd=Path.cwd())
    except rt.PodError as exc:
        _die(str(exc))


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #
def _home_holds_state(cfg: PodConfig, name: str) -> bool:
    """Return whether the pod home already contains state or is unusable."""
    home = cfg.home_dir(name)
    try:
        return home.exists() and (not home.is_dir() or any(home.iterdir()))
    except OSError:
        return True


def _verify_seed_landed(cfg: PodConfig, name: str, scenario: str, home_was_populated: bool) -> None:
    """Refuse to report success when a requested scenario did not reach the home."""
    landed = rt.seeded_scenario_in_home(cfg, name)
    if home_was_populated:
        held = f"scenario {landed!r}" if landed else "state from an earlier boot"
        _audit(
            "pod.up",
            "failure",
            f"name={name} seed={scenario}",
            error="populated home seed request refused before start",
        )
        _die(
            f"{name!r} already held {held}, so --seed {scenario} was NOT applied — "
            "a populated home is never re-seeded. The pod was not started, because "
            "cleanup after a failed start would otherwise be allowed to delete that "
            "existing state. To restart it unchanged: "
            f"kirocrew pod up {name}. To boot it fresh: "
            f"kirocrew pod down {name} && kirocrew pod up {name} --seed {scenario}"
        )
    if landed == scenario:
        return
    _audit("pod.up", "failure", f"name={name} seed={scenario}", error="seed did not land")
    found = f"it holds scenario {landed!r} instead" if landed else "its home holds no fixture"
    _die(
        f"{name}: the pod is up, but --seed {scenario} did NOT land — {found}.\n"
        f"  The per-pod boot override should point systemd at this checkout: "
        f"{rt.unit_mod.dropin_path(cfg, name)}.\n"
        f"  Its boot log: kirocrew pod logs {name}\n"
        f"  Then retry:   kirocrew pod down {name} && kirocrew pod up {name} "
        f"--seed {scenario}"
    )


def _up(cfg: PodConfig, args: argparse.Namespace) -> None:
    name = rt.validate_name(args.name)
    checkout = _resolve_or_die(cfg, name)

    scenario = ""
    if args.seed and rt.is_scenario_ref(args.seed):
        try:
            rt.resolve_seed_scenario(args.seed)
        except rt.PodError as exc:
            _audit("pod.up", "denied", f"name={name}", error="unknown seed scenario")
            _die(str(exc))
        scenario = args.seed

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

    # Pin the resolved checkout BEFORE starting the unit so the service-booted
    # gateway (and any Restart= re-exec) resolves it without shelling git from a
    # clean environment. SEED (if any) is merged in without clobbering the pin.
    # The whole pin -> start transaction holds the per-name mutex: without it, a
    # concurrent `down` finishing its sweep after our pin would delete the pin we
    # just wrote and this pod would crash-loop on boot. The pod's own boot
    # (`pod _run`) deliberately takes no lock, so holding this across the health
    # wait cannot deadlock against the process we are waiting for.
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

        # Read the unit's state BEFORE choosing a port, and inside the mutex: the
        # two questions are one decision. An `up` against an already-active pod is
        # a restart, and that pod's port is legitimately busy BECAUSE IT OWNS IT --
        # probing would find it taken and move a running pod out from under every
        # reader. Only a pod that is not running is choosing a port at all.
        was_active = rt.is_active(cfg, name)
        home_was_populated = _home_holds_state(cfg, name)
        # State that predates this command must be judged BEFORE `start_pod`:
        # any failed health wait calls `stop_pod`, whose zero-residue cleanup is
        # allowed to delete the pod home. Post-health verification remains for a
        # fresh home, where the state belongs to this start transaction.
        if scenario and home_was_populated:
            _verify_seed_landed(cfg, name, scenario, home_was_populated=True)
        if not was_active:
            # Asked BEFORE allocating, because allocation records a claim that would
            # then look like ours. An operator's deliberate `PORT=` must not acquire
            # the PORT_AUTO marker: that marker is what licenses a later relocation,
            # so stamping it here would silently convert a pin the operator set into
            # one this code may move -- defeating the guarantee that a pin you set is
            # never moved automatically.
            operator_pin = rt.operator_pinned(cfg, name)
            # The plane lock, INSIDE the name lock, covers choose -> start. Two
            # DIFFERENT colliding names hold disjoint NAME locks, so without this
            # both could probe one port free and both boot onto it. See
            # `pod_plane_mutex` for the window this shrinks and the one it leaves.
            with rt.pod_plane_mutex(cfg):
                try:
                    port, displaced_from = rt.allocate_port(cfg, name)
                except rt.PodError as exc:
                    _audit("pod.up", "denied", f"name={name}", error="port unavailable")
                    _die(f"refusing: {exc}")
                # Record the claim on EVERY allocation, not only when the port
                # moved. A unit is `Type=simple`, so `start_pod` returns before the
                # gateway binds; until then a bind probe reports this port free and a
                # concurrently-starting colliding name would be handed the same one.
                # The recorded claim is visible immediately, which is what makes the
                # concurrent case behave like the sequential one.
                #
                # This makes the cksum derivation a default HINT rather than a
                # contract: after its first `up` a pod's port comes from its
                # recorded claim, not from the formula. That is the intended
                # trade -- an explicit ownership claim is what the pod plane needs,
                # and it degrades gracefully, since the formula still chooses the
                # first-preference port for every pod that has never come up.
                env_updates["PORT"] = str(port)
                if operator_pin:
                    # CLEAR any marker left from an earlier automatic allocation.
                    # Without this the stale value outlives the pin it described, and
                    # the trap is a natural user action rather than a coincidence:
                    # having seen the pod moved to :7850, an operator pins :7850 --
                    # which now MATCHES the old marker, so their deliberate pin reads
                    # as machine-made and may be relocated out from under them.
                    # Emptied rather than deleted because `write_env_file` merges and
                    # has no delete; an empty value does not parse as a port, so an
                    # empty marker reads as no marker.
                    env_updates[rt.AUTO_PORT_KEY] = ""
                else:
                    env_updates[rt.AUTO_PORT_KEY] = str(port)
                if displaced_from is not None:
                    print(
                        f"pod: {name!r} moved to :{port} -- :{displaced_from} is "
                        f"already in use. Pinned PORT={port} so every reader "
                        f"agrees; `kirocrew pod url {name}` prints it.",
                        file=sys.stderr,
                    )
                if env_updates:
                    rt.write_env_file(cfg, name, env_updates)
                cp = rt.start_pod(cfg, name)
                if cp.returncode != 0:
                    _audit(
                        "pod.up",
                        "failure",
                        f"name={name} port={port}",
                        error="backend start failed",
                    )
                    _die(f"starting pod {name} failed: {(cp.stderr or '').strip()}")
        else:
            # Re-resolve INSIDE the lock. `port` above was read before we held it,
            # so a concurrent same-name `up` that pinned a fallback in the meantime
            # would leave us holding the OLD port -- and the health wait below
            # stops the unit when it cannot reach `port`, which would tear down the
            # pod that other `up` just successfully started.
            port = rt.derive_port(cfg, name)
            if env_updates:
                rt.write_env_file(cfg, name, env_updates)
            if boot_flags:
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
            # erase the REPLACEMENT pod. Holding the lock across the whole boot
            # transaction means the pod we stop here can only be the one we
            # started.
            tail = rt.recent_journal(cfg, name, 30)
            print(tail, file=sys.stderr)
            rt.stop_pod(cfg, name)
            if code == rt.HEALTH_FOREIGN:
                # Reported ahead of the crash verdict: the port being taken is
                # WHY this gateway could not boot, and it is fixed by choosing a
                # port rather than by debugging the worktree. Without this the
                # operator reads a journal that only says "address already in
                # use" — or, before the identity check existed, was told the pod
                # was up and drove somebody else's instance.
                _audit(
                    "pod.up",
                    "failure",
                    f"name={name} port={port}",
                    error="derived port held by another process",
                )
                _die(
                    f"{name}: :{port} is already held by another process (another pod, "
                    f"or the live gateway), so this pod's gateway could not bind it — "
                    f"the responder on that port is not this pod.\n"
                    f"  Which pods hold which ports: kirocrew pod ls\n"
                    f"  Give this pod its own port:  add PORT=<free port> to "
                    f"{cfg.env_file(name)}, then `kirocrew pod up {name}` again."
                )
            if code == -1:
                _die(
                    f"{name}: the worktree's gateway failed to start (see journal above). "
                    f"This is the worktree build, not pod — fix it, then `kirocrew pod up {name}` again."
                )
            _die(
                f"{name}: gateway never became healthy on :{port} within timeout "
                f"(see journal above; check the worktree's gateway start path)."
            )

        if scenario and not home_was_populated:
            _verify_seed_landed(cfg, name, scenario, home_was_populated=False)

    # The pod is already booted and healthy by here, so the credential is the
    # LAST step, not the point of the command. That asymmetry decides how the two
    # refusals are handled:
    #
    # * FOREIGN is positive knowledge that the port is somebody else's, so there
    #   is nothing truthful left to print — die.
    # * UNPROVEN is not knowledge. On a POSIX host with no lsof there is no way to
    #   prove ownership at all, and dying here would boot the pod and then fail
    #   the command that booted it, turning a hardened credential path into a
    #   `pod up` that never succeeds on that host class. So report what IS known
    #   (the pod, its port, its base_url), withhold only the credential, and say
    #   how to get it. The secret still never goes on the wire — the guard lives
    #   in `mint_token`, which raised before dialling anything.
    token = ""
    unproven = ""
    try:
        token = rt.mint_token(cfg, name, args.ttl)
    except rt.PodOwnershipUnproven as exc:
        unproven = str(exc)
        _audit(
            "pod.token",
            "denied",
            f"name={name} port={port}",
            error="ownership unprovable; credential withheld",
        )
    except rt.PodError as exc:
        _audit("pod.token", "failure", f"name={name} port={port}", error="mint failed")
        _die(str(exc))
    if token:
        _audit("pod.token", "allowed", f"name={name} port={port} ttl={args.ttl}")
    base = f"http://127.0.0.1:{port}"
    if unproven:
        # The reason goes to stderr on BOTH paths rather than into the payload. The
        # empty ``token`` is already the machine-readable signal -- it is what
        # pod-e2e tests, and it is unambiguous because every other mint failure
        # exits instead of returning -- so a payload field would add schema surface
        # that is committed forever for no consumer. stderr reaches a subprocess
        # caller and a human equally.
        print(f"pod: {unproven}", file=sys.stderr)
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
        if token:
            print(f"  token    : {token}")
            print(f"  open     : {base}/?token={token}")
        else:
            print("  token    : (withheld — ownership of the port could not be proven)")
        print(f"  stop     : kirocrew pod down {name}")


def _down(cfg: PodConfig, args: argparse.Namespace) -> None:
    name = rt.validate_name(args.name)
    # The stop, the state it is judged against, and the env-file removal all move
    # together under the per-name mutex: unlinking the env file after releasing
    # the lock let a concurrent `up` — which pins its checkout under the same
    # mutex — have its fresh pin deleted by this stale teardown. Sampling
    # was_up/had_home OUTSIDE the lock had the same shape: a concurrent `up`
    # holding the lock meant we sampled "not running, nothing to reclaim", waited,
    # and then judged a REAL failure against that stale answer — swallowing it and
    # deleting the live pod's checkout pin.
    with rt.pod_name_mutex(cfg, name):
        was_up = rt.is_active(cfg, name)
        # Whether there is residue to reclaim is a separate question from whether
        # the pod is running: `down` is also the documented way to reclaim an
        # orphaned HOME left by a pod that went away without one.
        had_home = cfg.home_dir(name).exists()
        cp = rt.stop_pod(cfg, name)
        # A nonzero stop means the pod may still be live, or its HOME survived —
        # don't claim success or delete the env file. Gated on there being
        # something at stake: an inactive Linux name with no HOME left behind has
        # nothing to lose, and `systemctl stop` on an instance of a template that
        # was never installed reports "unit not loaded" — swallowing that keeps
        # `pod down <never-used-name>` the documented no-op it has always been.
        # When the pod WAS up, or a HOME is there to reclaim, the failure is
        # fatal: a reclaim that could not finish must not report success.
        # On macOS it is always fatal, because a loaded-but-dead agent has no pid
        # (was_up False) yet still needs its unload CONFIRMED before anything is
        # torn down.
        if cp.returncode != 0 and (was_up or had_home or rt.IS_MACOS):
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
    elif had_home:
        print(f"pod '{name}' was not running — reclaimed the isolated HOME it left behind")
    else:
        print(f"pod '{name}' was not running (nothing to stop)")


def _health_label(code: int) -> str:
    """Human rendering of a :func:`rt.health` verdict.

    The JSON stays numeric (three callers parse ``pod ls --json``), but a bare
    ``-2`` on a terminal tells the operator nothing, and "another instance owns
    this port" is precisely the thing they need to read.
    """
    if code == rt.HEALTH_FOREIGN:
        return "foreign (port held by another instance)"
    return str(code)


def _ls(cfg: PodConfig, args: argparse.Namespace) -> None:
    names = sorted(rt.active_names(cfg))
    # Teardown belongs to `pod down` on BOTH platforms, so a pod that went away
    # without one leaves its isolated HOME. Surface those here — the docs promise
    # `ls`/`down` make them visible — but keep the JSON array shape unchanged
    # (three callers parse it); orphans are human-output only.
    if args.json:
        # An unpinned port shells `cksum`, so deriving it twice per row would
        # double the subprocess count for the same answer.
        rows: list[dict[str, object]] = []
        for n in names:
            p = rt.derive_port(cfg, n)
            rows.append({"name": n, "port": p, "health": rt.health(cfg, n, p)})
        print(json.dumps(rows))
        return
    # Any fail-closed probe underneath surfaces as PodError, which the dispatch
    # layer renders as the documented one-line `pod: <msg>` refusal.
    orphans = rt.orphan_homes(cfg)
    if names:
        print(f"{'POD':<28} {'PORT':<7} HEALTH")
        for n in names:
            p = rt.derive_port(cfg, n)
            print(f"{n:<28} {p:<7} {_health_label(rt.health(cfg, n, p))}")
    else:
        print("no pods running")
    _print_orphans(cfg, orphans)


def _print_orphans(cfg: PodConfig, orphans: list[str]) -> None:
    """Human-readable report of pod HOMEs with no live pod behind them."""
    if not orphans:
        return
    now = time.time()
    print(
        f"\n{len(orphans)} orphaned pod HOME(s) — left by a pod that went away "
        "without an explicit `down` (a crash, a raw service stop, a reboot):"
    )
    for n in orphans:
        # Best-effort "last alive" hint. A HOME that vanished or cannot be
        # statted between the enumeration and this loop still gets its row —
        # age is a hint, never a gate, on the read path.
        try:
            age = _relative_age(now - _orphan_last_alive(cfg, n))
        except OSError:
            age = "age unknown"
        print(f"  {n:<26} {age:<12} reclaim: kirocrew pod down {n}")
    print("  bulk reclaim: kirocrew pod prune [--all] [--dry-run] (default keeps the last 3d)")


def _orphan_last_alive(cfg: PodConfig, name: str) -> float:
    """Best-effort "last alive" timestamp for an orphaned HOME.

    The HOME directory's own mtime freezes once the top-level layout exists —
    a gateway writes into ``logs/``, ``sessions/``, ``workspace/`` — so it
    measures creation, not activity, and would age a freshly-crashed pod as its
    boot date (making the crash being debugged the first thing ``--older-than``
    reclaims). Scan two levels down and take the newest mtime: log appends and
    db writes land on level-1/2 files, so this tracks real activity without an
    unbounded walk. Raises OSError only when the HOME itself cannot be statted;
    unreadable children are skipped.
    """
    home = cfg.pod_root / name
    newest = home.stat().st_mtime
    try:
        for child in home.iterdir():
            try:
                newest = max(newest, child.stat().st_mtime)
                if child.is_dir() and not child.is_symlink():
                    for grand in child.iterdir():
                        try:
                            newest = max(newest, grand.stat().st_mtime)
                        except OSError:
                            continue
            except OSError:
                continue
    except OSError:
        pass
    return newest


def _relative_age(seconds: float) -> str:
    """Coarse relative age ("3d ago"): largest whole unit, floored, never negative.

    A clock skew or a just-touched directory can put the mtime in the future;
    clamping to 0 keeps the report readable instead of printing a negative age.
    """
    s = max(0, int(seconds))
    if s >= 86400:
        return f"{s // 86400}d ago"
    if s >= 3600:
        return f"{s // 3600}h ago"
    if s >= 60:
        return f"{s // 60}m ago"
    return f"{s}s ago"


# ``prune --older-than`` accepts a single count+unit token. Deliberately a tiny
# local grammar (no dependency): days/hours/minutes/seconds cover every horizon
# an orphan sweep needs. The digit cap bounds the arithmetic: an unbounded
# count over ~1e308 would overflow the float timestamp subtraction; 9 digits of
# days is ~2.7 million years, ample and safely finite.
_OLDER_THAN_RE = re.compile(r"^(\d{1,9})([dhms])$")
_OLDER_THAN_UNIT_SECS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _parse_older_than(spec: str) -> float:
    """``"3d"`` → seconds. Raises :class:`rt.PodError` for anything else, so the
    caller can audit the refusal and the dispatch layer still renders the
    documented one-line ``pod: …`` message."""
    m = _OLDER_THAN_RE.match(spec.strip())
    if not m:
        raise rt.PodError(
            f"invalid --older-than {spec!r} "
            f"(expected <N>d, <N>h, <N>m or <N>s with at most 9 digits, e.g. 3d)"
        )
    return int(m.group(1)) * _OLDER_THAN_UNIT_SECS[m.group(2)]


def _prune(cfg: PodConfig, args: argparse.Namespace) -> None:
    """Bulk-reclaim orphaned pod HOMEs (the N-at-once form of `pod down <name>`).

    Enumerates the same orphan set ``ls`` reports, optionally keeps anything
    whose last activity is younger than ``--older-than``, and reclaims each
    survivor through the same safe delete path ``down`` uses. Per-name results,
    because a prune where three of nine names succeeded must say which three —
    one aggregate "done" line would hide partial failure.
    """
    # An unusable backend is ONE refusal, not N per-name failures — and a
    # refused bulk-destructive invocation must still reach the audit trail, so
    # every refusal path out of this verb (dead backend, malformed duration,
    # failed orphan enumeration) is recorded as denied before the dispatch
    # layer renders the documented one-line error.
    try:
        rt.require_backend()
        # Age-gated by DEFAULT (3d): a bare `prune` must not sweep the
        # minutes-old crash HOME an operator is still debugging — its logs and
        # sessions are the only postmortem evidence, and the delete is
        # unrecoverable. `--all` is the explicit opt-in for a full sweep.
        threshold: float | None = None
        if not getattr(args, "prune_all", False):
            threshold = time.time() - _parse_older_than(args.older_than)
        orphans = rt.orphan_homes(cfg)
    except rt.PodError as exc:
        _audit(
            "pod.prune", "denied", f"older_than={args.older_than or 'all'}", error=str(exc)[:120]
        )
        raise
    dry_run = bool(getattr(args, "dry_run", False))
    results: list[dict[str, str]] = []
    for name in orphans:
        if threshold is not None:
            # A HOME that cannot be statted cannot be proven old enough —
            # skip it and keep going rather than abort the whole prune.
            try:
                last_alive = _orphan_last_alive(cfg, name)
            except OSError as exc:
                results.append({"name": name, "status": "skipped", "detail": f"stat failed: {exc}"})
                continue
            if last_alive > threshold:
                results.append(
                    {"name": name, "status": "kept", "detail": "younger than --older-than"}
                )
                continue
        if dry_run:
            # Apply the DETERMINISTIC classification so the preview matches a
            # real run: an invalid name is skipped either way. The liveness
            # rechecks are moment-in-time and stay out of the preview.
            try:
                rt.validate_name(name)
            except rt.PodError:
                results.append(
                    {"name": name, "status": "skipped", "detail": "not a valid pod name"}
                )
                continue
            results.append({"name": name, "status": "would-reclaim", "detail": ""})
            continue
        if args.json:
            # The delete path underneath (stop_pod -> cleanup_home) prints its
            # diagnostics to stdout; interleaved with the machine output they
            # would corrupt the JSON document. Reroute them to stderr — kept
            # visible, never parsed.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                row = _prune_one(cfg, name)
            if buf.getvalue():
                print(buf.getvalue(), file=sys.stderr, end="")
            results.append(row)
        else:
            results.append(_prune_one(cfg, name))
    counts = {
        s: sum(1 for r in results if r["status"] == s)
        for s in ("reclaimed", "would-reclaim", "kept", "skipped", "failed")
    }
    failed = counts["failed"]
    # Invocation-level audit: the per-name events say what each delete decided,
    # but a bulk destructive verb must be visible in the trail even when it
    # touched nothing (empty set, all kept, dry run).
    _audit(
        "pod.prune",
        "allowed",
        f"total={len(results)} reclaimed={counts['reclaimed']} kept={counts['kept']} "
        f"skipped={counts['skipped']} failed={failed} dry_run={int(dry_run)}",
    )
    if args.json:
        # prune owns its machine shape; `ls --json` stays live-pods-only.
        print(json.dumps(results))
    elif not results:
        print("no orphaned pod HOMEs to prune")
    else:
        for r in results:
            detail = f"  {r['detail']}" if r["detail"] else ""
            print(f"  {r['name']:<26} {r['status']}{detail}")
        if dry_run:
            print(
                f"dry run: {counts['would-reclaim']} would be reclaimed, "
                f"{counts['kept']} kept, {counts['skipped']} skipped"
            )
        else:
            print(
                f"pruned: {counts['reclaimed']} reclaimed, {counts['kept']} kept, "
                f"{counts['skipped']} skipped, {counts['failed']} failed"
            )
    if failed:
        sys.exit(1)


def _prune_one(cfg: PodConfig, name: str) -> dict[str, str]:
    """Reclaim ONE orphaned HOME through the safe delete path; never raises.

    Structured so the SEL audit CANNOT be skipped: every decision path returns
    through :func:`_prune_one_decide`, and the single audit call here is the
    only exit. A per-name permission decision on a bulk-destructive verb that
    does not reach the trail is invisible to the operator — adding a new
    return path to the decide helper keeps this property by construction.
    """
    status, detail, outcome, err = _prune_one_decide(cfg, name)
    _audit("pod.prune", outcome, f"name={name}", error=err)
    return {"name": name, "status": status, "detail": detail}


def _prune_one_decide(cfg: PodConfig, name: str) -> tuple[str, str, str, str]:
    """The decision half of :func:`_prune_one`: ``(status, detail, outcome, error)``.

    Every delete routes through :func:`rt.stop_pod` — the path that drains the
    unit's processes and verifies the HOME is really gone — NEVER through
    ``cleanup_home`` directly, which would race the pod's own surviving
    processes (the exact defect the hook-based teardown removal fixed).

    Liveness is re-checked HERE, under the per-name mutex, and it must be
    STRICTER than the enumeration's ``--state=active`` filter: a unit in the
    ``Restart=on-failure`` backoff window reports ``activating`` (not active),
    so both the orphan scan and a bare ``is_active`` call miss it — and a
    ``systemctl stop`` would cancel the pending restart and delete a pod the
    operator considers running. Only a terminal state (``inactive``/``failed``
    with no restart pending) may proceed; anything else is refused, the same
    fail-closed reading the macOS plist recheck gives mid-``up`` names. A
    failure on one name is reported and the prune continues — partial progress
    beats an aborted sweep.
    """
    try:
        # A stray directory that is not a valid pod name (spaces, over-long)
        # can never have a unit or be reclaimed by `down`; refuse it by name
        # rather than shelling a bogus systemctl stop and reporting a failure
        # that would make every future prune exit nonzero.
        try:
            rt.validate_name(name)
        except rt.PodError as exc:
            return "skipped", "not a valid pod name", "denied", str(exc)[:120]
        with rt.pod_name_mutex(cfg, name):
            if rt.is_active(cfg, name):
                return "skipped", "pod is now active", "denied", "pod is now active"
            state, restarts = rt.unit_state(cfg, name)
            if state not in ("inactive", "failed") or restarts > 0:
                return (
                    "skipped",
                    f"unit is {state} (mid-transition or restarting, not orphaned)",
                    "denied",
                    f"unit state {state} restarts={restarts}",
                )
            # macOS: a per-pod plist means "installed" (a name mid-`up`), not
            # orphaned — same predicate orphan_homes applies, re-checked at
            # delete time for writers that bypass the mutex.
            if rt.IS_MACOS and rt.launchd.plist_path(cfg, name).exists():
                return "skipped", "pod is now installed", "denied", "pod is now installed"
            cp = rt.stop_pod(cfg, name)
            if cp.returncode != 0:
                err = (cp.stderr or "").strip() or f"stop rc={cp.returncode}"
                return "failed", err, "failure", err[:120]
            if rt.RECLAIMED_MARKER in (cp.stdout or ""):
                # The name was claimed by a new pod mid-teardown; its env file
                # now pins the NEW pod's checkout — leave it alone.
                return "skipped", "name claimed by a new pod", "allowed", ""
            # Clear the pinned CHECKOUT= / SEED= so a later `up` re-resolves
            # cleanly — the same post-reclaim step `down` performs. missing_ok:
            # exists()-then-unlink is a TOCTOU against a concurrent `down`.
            cfg.env_file(name).unlink(missing_ok=True)
    except (rt.PodError, OSError, subprocess.SubprocessError) as exc:
        # One name must never abort the sweep: containment covers the pod
        # error type AND the raw filesystem/subprocess failures underneath it
        # (an unwritable env dir, a timed-out systemctl) — an escaped exception
        # here would hide every result already earned and strand the tail.
        return "failed", str(exc), "failure", str(exc)[:120]
    return "reclaimed", "", "allowed", ""


def _status(cfg: PodConfig, args: argparse.Namespace) -> None:
    name = rt.validate_name(args.name)
    port = rt.derive_port(cfg, name)
    up = rt.is_active(cfg, name)
    code = rt.health(cfg, name, port) if up else 0
    if args.json:
        print(
            json.dumps(
                {"name": name, "status": "up" if up else "down", "port": port, "health": code}
            )
        )
    else:
        print(f"{name}: {'up' if up else 'down'}  port={port}  health={_health_label(code)}")


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


def _api_body(raw: str) -> object:
    """Decode a pod response body without ever raising.

    The fixed-key envelope is this command's output contract, so a body that
    cannot be parsed degrades to its (already scrubbed) text instead of
    replacing the envelope with a traceback. Deciding that per exception TYPE is
    what keeps failing: `json.loads` recurses once per nesting level, so a deep
    response raises RecursionError — a RuntimeError, not the ValueError a
    malformed body raises. Any decode failure is a body problem, never a reason
    to abandon the envelope.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _api_envelope(name: str, method: str, path: str, status: int, ok: bool, body: object) -> str:
    """Render the envelope, degrading a body that cannot be serialized.

    Serialization is the command's last exit, so it must not raise either:
    encoding recurses per nesting level too, and a body deep enough to decode
    but not re-encode would otherwise take the envelope down after the request
    had already succeeded. The fallback document has a fixed shape and depth,
    so it cannot fail in turn.
    """
    document: dict[str, object] = {
        "name": name,
        "method": method,
        "path": path,
        "status": status,
        "ok": ok,
        "body": body,
    }
    try:
        return json.dumps(document, indent=2)
    except Exception:
        document["body"] = "<body omitted: not serializable>"
        return json.dumps(document, indent=2)


def _api(cfg: PodConfig, args: argparse.Namespace) -> None:
    """Make one authenticated pod request and print a stable JSON document.

    Every exit from this function is the envelope. The request, the decode and
    the render all sit inside one guarded region whose handlers cover any
    Exception, so a failure mode nobody enumerated degrades the body rather than
    escaping as a traceback on stdout — which an agent parsing this output reads
    as a protocol violation, not as an error it can act on.
    """
    name = str(args.name)
    method = str(args.method).upper()
    normalized = "/api/<invalid>"
    status = 0
    body: object = ""
    ok = False
    error = ""
    try:
        name = rt.validate_name(name)
        normalized = rt.api_path(args.path)
        status, raw = rt.pod_api(
            cfg,
            name,
            method,
            normalized,
            data=getattr(args, "data", "") or "",
            allow_write=bool(getattr(args, "allow_write", False)),
        )
        body = _api_body(raw)
        ok = 200 <= status < 300
        error = "" if ok else f"status={status}"
    except rt.PodError as exc:
        body = str(exc)
        error = type(exc).__name__
    except Exception as exc:
        body = f"pod api failed ({type(exc).__name__})"
        error = type(exc).__name__
    resources = f"name={name} method={method} path={normalized} status={status}"
    if method not in rt.API_READ_METHODS:
        resources += " write=1"
    _audit("pod.api", "allowed" if ok else "failure", resources, error=error)
    print(_api_envelope(name, method, normalized, status, ok, body))
    if not ok:
        sys.exit(1)


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
    # teardown could unlink this fresh pin.
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
    """Hidden: reclaim ONE pod's isolated HOME by name.

    Not wired to a service-manager hook (see :mod:`kiro_crew.pod.unit` for why
    teardown belongs to the ``down`` path) — this is the single-name safe-delete
    entry point for a manual reclaim, and it re-validates the name, refusing
    ``..``/absolute/empty, so a caller that never went through the CLI's
    ``validate_name`` still cannot ``rm`` outside the pod root. Prefer
    ``kirocrew pod down <name>``, which stops the service first.
    """
    rc = rt.cleanup_home(cfg, args.name)
    outcome = "allowed" if rc == 0 else "failure"
    _audit("pod.cleanup", outcome, f"name={args.name}", error="" if rc == 0 else f"rc={rc}")
    sys.exit(rc)


def _scenario_table_description(description: str, width: int) -> str:
    """Shorten *description* to *width* for one table row, never cutting mid-word.

    Sentence detection lives in ``truncate_summary`` alone. It prefers the last
    complete sentence that fits, which on a description whose second sentence
    does not fit IS the first sentence — so a separate first-sentence pass here
    would be a second spelling of the same rule that discards a second sentence
    the row had room for.
    """
    return truncate_summary(" ".join(description.split()), width)


def _scenarios(cfg: PodConfig, args: argparse.Namespace) -> None:
    """List the named fixtures accepted by ``pod up --seed``."""
    from kiro_crew import seed as seed_mod

    rows = [
        {"name": name, "description": seed_mod.fixture_summary(name)}
        for name in sorted(seed_mod.available_fixtures())
    ]
    if getattr(args, "json", False):
        print(json.dumps(rows))
        return
    if not rows:
        print("no seed scenarios found (the packaged fixtures tree is missing)")
        return

    width = max(len("SCENARIO"), *(len(str(row["name"])) for row in rows))
    description_width = _SCENARIOS_TABLE_WIDTH - width - 2
    print(f"{'SCENARIO':<{width}}  DESCRIPTION")
    for row in rows:
        name = str(row["name"])
        description = str(row["description"] or "(no description)")
        description = _scenario_table_description(description, description_width)
        print(f"{name:<{width}}  {description}")
    print(f"\nseed one with: kirocrew pod up <worktree> --seed {rows[0]['name']}")


_VERBS: dict[str, PodHandler] = {
    "up": _up,
    "down": _down,
    "ls": _ls,
    "prune": _prune,
    "status": _status,
    "token": _token,
    "url": _url,
    "scenarios": _scenarios,
    "api": _api,
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
            "{up|down|ls|prune|status|token|url|scenarios|api|logs|exec|install|provision} …"
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
